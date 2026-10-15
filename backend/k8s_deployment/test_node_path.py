"""把 kubeasz 的 bin 目录加进节点系统 PATH 的用例：文本生成、远程落地与撤销。

真实背景：kubeasz 把 kubectl、crictl 这些命令拷到节点的 ``/opt/kube/bin``，然后只在
**连接用户**的 ``~/.bashrc`` 里加一行 PATH —— 换个用户、或者 ``ssh 节点 "kubectl ..."``
这种非交互式命令，都还是 ``command not found``（见 ``services/node_path.py`` 开头）。

文本生成是纯函数，远程动作经 ``client_factory`` 注入假客户端。这个假客户端把节点的两个
文件放在内存里、并按生成的命令真的走一遍改写，因此「读出原文 → 改 PATH 那一行 → 原子替换
→ 回读核对」这条链路是被真的跑通的，不只是断言命令字符串。
"""

import base64
import re
import shutil
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from k8s_deployment.services import compiler, node_path

from .test_node_registry import _RecordingContext, _Stream, make_node

# 典型的 Ubuntu ``/etc/environment``：只有一行 PATH。
PLAIN_ENVIRONMENT = 'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'

# write_text_command 的形状。用反向引用把临时文件名与目标路径绑在一起，命令改动大了会立刻失配
# （假客户端对不上的命令返回非零退出码，用例会以「写入失败」失败，而不是静默放过）。
WRITE_COMMAND = re.compile(
    r"printf '%s' '([A-Za-z0-9+/=]*)' \| base64 -d > (?P<tmp>\S+) && "
    r'chmod (?P<mode>\d+) (?P=tmp) && mv -f (?P=tmp) (?P<path>\S+) && cat (?P=path)',
)


class FakeNode:
    """把节点上那两个文件放进内存，让写入命令真的改到东西。

    只实现被测代码会用到的那几条命令形状：``read_text_command``、``write_text_command``
    生成的写入命令、以及 ``profile_remove_command`` 生成的判断命令。认不出来的命令回非零
    退出码，让用例在「命令变了但假客户端没跟着改」时直接失败。
    """

    def __init__(self, files=None, unreadable=()):
        """初始化。

        参数：`files` 表示初始文件内容；`unreadable` 表示读不出来的路径集合。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.files = dict(files or {})
        self.unreadable = set(unreadable)
        self.commands = []
        self.modes = {}

    def exec_command(self, command, timeout=None):
        """按命令文本在内存文件上执行一次。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(None, stdout, stderr)``。
        副作用：可能改动 ``files``。
        """
        self.commands.append(command)
        code, out, err = self._run(command)
        return None, _Stream(out, code), _Stream(err, code)

    def _run(self, command):
        """命令的实际实现，返回 ``(退出码, 标准输出, 标准错误)``。"""
        if command.startswith('base64 '):
            path = command[len('base64 '):].split(' ')[0]
            if path in self.unreadable:
                return 1, '', f'base64: {path}: Permission denied'
            if path not in self.files:
                return 1, '', f'base64: {path}: No such file or directory'
            return 0, base64.b64encode(self.files[path].encode('utf-8')).decode('ascii'), ''
        match = WRITE_COMMAND.fullmatch(command)
        if match:
            path = match.group('path')
            self.files[path] = base64.b64decode(match.group(1)).decode('utf-8')
            self.modes[path] = match.group('mode')
            return 0, self.files[path], ''
        if command.startswith('if [ -f '):
            path = command[len('if [ -f '):].split(' ')[0]
            if path not in self.files:
                return 0, 'ABSENT', ''
            if self.files[path].startswith(node_path.PROFILE_MARKER):
                del self.files[path]
                return 0, 'REMOVED', ''
            return 0, 'KEPT', ''
        return 1, '', f'unexpected command: {command}'

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True

    def fired(self, needle):
        """取回所有包含给定片段的命令。"""
        return [item for item in self.commands if needle in item]


class RecordingContext(_RecordingContext):
    """够用的上下文替身：收集日志、事件与阶段名。"""

    def __init__(self):
        """初始化。"""
        super().__init__()
        self.events = []
        self.stages = []

    def event(self, event_type, message, level='info', detail=None):
        """收集事件。"""
        self.events.append({'type': event_type, 'message': message, 'level': level, 'detail': detail})

    def begin_stage(self, key, title=None, **kwargs):
        """收集阶段名。"""
        self.stages.append(key)

    def complete_stage(self, key, elapsed=0.0, **kwargs):
        """阶段收尾（假实现不记）。"""
        return None

    def event_types(self):
        """返回事件类型集合。"""
        return {item['type'] for item in self.events}


class ProfileContentTest(SimpleTestCase):
    """``profile_content``：所有用户、所有登录 shell 都从这里拿到 PATH。"""

    def test_first_line_is_the_marker(self):
        """首行必须是标记：撤销时靠它确认这个文件是平台写的，不是别人的。"""
        text = node_path.profile_content('/opt/kube/bin')
        self.assertTrue(text.startswith(node_path.PROFILE_MARKER))

    def test_exports_the_directory_in_front(self):
        """写在最前面：kubeasz 自己的 ``.bashrc`` 那一行也是这么放的，行为要一致。"""
        text = node_path.profile_content('/data/kube/bin')
        self.assertIn('export PATH=/data/kube/bin:$PATH', text)

    def test_keeps_path_unexpanded(self):
        """``$PATH`` 必须是字面量：文件是写的时候不展开、被 source 的时候才展开。"""
        line = node_path.profile_content('/opt/kube/bin').splitlines()[-1]
        self.assertTrue(line.endswith(':$PATH'), line)

    def test_declares_how_to_undo(self):
        """文件里自带撤销说明：这机器以后可能换人管。"""
        self.assertIn('删掉本文件', node_path.profile_content('/opt/kube/bin'))


class EnvironmentContentTest(SimpleTestCase):
    """``environment_content``：改 ``/etc/environment`` 是全流程里唯一动别人文件的地方。"""

    def add(self, text, bin_dir='/opt/kube/bin'):
        """跑一次「加入」。"""
        return node_path.environment_content(text, bin_dir, add=True)

    def test_plain_path_gets_the_directory_first(self):
        """没有引号的 PATH 也要能改，且加在最前面。"""
        updated, info = self.add('PATH=/usr/bin:/bin\n')
        self.assertEqual(updated, 'PATH=/opt/kube/bin:/usr/bin:/bin\n')
        self.assertEqual(info['before'], '/usr/bin:/bin')
        self.assertEqual(info['after'], '/opt/kube/bin:/usr/bin:/bin')

    def test_quotes_are_preserved(self):
        """引号风格按原样保留：把操作员写的引号吃掉，diff 里看不出是平台改的。"""
        updated, _info = self.add(PLAIN_ENVIRONMENT)
        self.assertEqual(updated, PLAIN_ENVIRONMENT.replace('PATH="', 'PATH="/opt/kube/bin:'))
        self.assertTrue(updated.startswith('PATH="'))
        self.assertTrue(updated.rstrip('\n').endswith('"'))

    def test_single_quotes_are_preserved_too(self):
        """单引号同理。"""
        updated, _info = self.add("PATH='/usr/bin'\n")
        self.assertEqual(updated, "PATH='/opt/kube/bin:/usr/bin'\n")

    def test_already_present_is_left_alone(self):
        """幂等：重复部署时不能每跑一次就在前面多挂一节。"""
        original = 'PATH="/opt/kube/bin:/usr/bin"\n'
        updated, info = self.add(original)
        self.assertEqual(updated, original)
        self.assertFalse(info['changed'])
        self.assertEqual(info['matched'], 1)

    def test_other_assignments_and_comments_are_untouched(self):
        """别的变量、注释行、空行一律不动 —— 操作员常把用过的配置注释着留档。"""
        text = '# PATH 以前是这样\nLANG=en_US.UTF-8\n\nPATH=/usr/bin\n'
        updated, _info = self.add(text)
        self.assertEqual(updated, '# PATH 以前是这样\nLANG=en_US.UTF-8\n\nPATH=/opt/kube/bin:/usr/bin\n')

    def test_missing_path_line_is_reported_not_invented(self):
        """没有 PATH= 这一行时什么都不做：宁可不动，也不去猜一个默认值塞进系统文件。"""
        updated, info = self.add('LANG=en_US.UTF-8\n')
        self.assertEqual(updated, 'LANG=en_US.UTF-8\n')
        self.assertEqual(info['matched'], 0)
        self.assertFalse(info['changed'])

    def test_indented_line_is_not_an_assignment(self):
        """有缩进的 ``PATH=`` 不是合法赋值，pam_env 不认，平台也不该认。"""
        updated, info = self.add('  PATH=/usr/bin\n')
        self.assertEqual(updated, '  PATH=/usr/bin\n')
        self.assertEqual(info['matched'], 0)

    def test_lowercase_key_is_not_the_same_variable(self):
        """键名大小写敏感：改了 ``path=`` 等于没改，还会留下一条错配置。"""
        updated, info = self.add('path=/usr/bin\n')
        self.assertEqual(updated, 'path=/usr/bin\n')
        self.assertEqual(info['matched'], 0)

    def test_every_path_line_is_covered(self):
        """文件里出现两条 PATH（不该有，但真出现时以最后一条为准），两条都得带上。"""
        updated, info = self.add('PATH=/usr/bin\nPATH=/opt\n')
        self.assertEqual(updated, 'PATH=/opt/kube/bin:/usr/bin\nPATH=/opt/kube/bin:/opt\n')
        self.assertEqual(info['matched'], 2)
        self.assertTrue(info['changed'])

    def test_trailing_newline_is_preserved(self):
        """没有结尾换行时不要凭空加一个：这一行可能就是文件最后一行。"""
        updated, _info = self.add('PATH=/usr/bin')
        self.assertEqual(updated, 'PATH=/opt/kube/bin:/usr/bin')

    def test_removing_takes_the_directory_out(self):
        """撤销时精确摘掉自己那一段，剩下的一节不动。"""
        updated, info = node_path.environment_content(
            'PATH="/opt/kube/bin:/usr/bin:/bin"\n', '/opt/kube/bin', add=False,
        )
        self.assertEqual(updated, 'PATH="/usr/bin:/bin"\n')
        self.assertTrue(info['changed'])

    def test_removing_something_absent_changes_nothing(self):
        """本来就没有自己的目录时不做无谓的改写。"""
        original = 'PATH="/usr/bin"\n'
        updated, info = node_path.environment_content(original, '/opt/kube/bin', add=False)
        self.assertEqual(updated, original)
        self.assertFalse(info['changed'])

    def test_removing_the_only_entry_drops_the_line(self):
        """摘完就空了要整行去掉：留下 ``PATH=`` 会让登录后什么命令都找不到。"""
        updated, info = node_path.environment_content(
            'PATH="/opt/kube/bin"\n', '/opt/kube/bin', add=False,
        )
        self.assertEqual(updated, '')
        self.assertTrue(info['changed'])

    def test_only_the_exact_directory_is_removed(self):
        """前缀相同的目录不能被误摘：``/opt/kube/bin`` 与 ``/opt/kube/bin2`` 是两回事。"""
        original = 'PATH="/usr/bin:/opt/kube/bin2"\n'
        updated, info = node_path.environment_content(original, '/opt/kube/bin', add=False)
        self.assertEqual(updated, original)
        self.assertFalse(info['changed'])


class WriteTextCommandTest(SimpleTestCase):
    """``write_text_command``：内容要原样落到节点上，且不依赖 SFTP。"""

    def test_payload_round_trips(self):
        """带引号、反斜杠、通配符的内容经 base64 往返后逐字节不变。"""
        text = 'PATH="a:\'b\\c*d"\n中文\n'
        command = node_path.write_text_command('/etc/environment', text)

        match = WRITE_COMMAND.fullmatch(command)
        self.assertIsNotNone(match)
        self.assertEqual(base64.b64decode(match.group(1)).decode('utf-8'), text)

    def test_writes_then_renames_atomically(self):
        """先写临时文件再改名：读 /etc/environment 的 pam_env 看不到写了一半的内容。"""
        command = node_path.write_text_command('/etc/environment', 'x\n')
        self.assertIn(f'> /etc/environment{node_path.TEMP_SUFFIX} &&', command)
        self.assertIn(f'mv -f /etc/environment{node_path.TEMP_SUFFIX} /etc/environment', command)

    def test_reads_back_with_cat(self):
        """末尾回显落地结果，调用方据此核对，不必再开一次连接。"""
        self.assertTrue(node_path.write_text_command('/etc/environment', 'x\n').endswith('cat /etc/environment'))


class ReadTextTest(SimpleTestCase):
    """``read_text``：读回原文时不能丢掉首尾空白。"""

    def test_commander_strips_newlines_from_the_base64_stream(self):
        """``run_command`` 会 strip 标准输出，所以输出必须是单行 base64。"""
        self.assertEqual(node_path.read_text_command('/etc/environment'), "base64 /etc/environment | tr -d '\\n'")

    def test_round_trips_whitespace(self):
        """结尾换行、开头空行都要原样带回来，否则回写时那几行会凭空消失。"""
        text = '\n\nPATH="/usr/bin"\n'
        client = FakeNode(files={'/etc/environment': text})

        code, out, err = node_path.read_text(client, '/etc/environment')

        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, text)

    def test_missing_file_reports_the_code(self):
        """文件不在时返回非零退出码，调用方据此跳过而不是当成空文件写回去。"""
        code, out, _err = node_path.read_text(FakeNode(), '/etc/environment')

        self.assertNotEqual(code, 0)
        self.assertEqual(out, '')


class ProfileRemoveCommandTest(SimpleTestCase):
    """``profile_remove_command``：只删自己写的那个文件。"""

    def test_guards_on_the_marker(self):
        """内容不是平台写的时候回 ``KEPT``：这个路径下也可能有运维自己的东西。"""
        command = node_path.profile_remove_command()
        self.assertIn(f'head -n 1 {node_path.PROFILE_PATH}', command)
        self.assertIn('REMOVED', command)
        self.assertIn('KEPT', command)
        self.assertIn('ABSENT', command)

    def test_removes_only_that_file(self):
        """删除动作只针对那个文件，不碰 /etc/profile.d 下的别的东西。"""
        command = node_path.profile_remove_command()
        self.assertIn(f'rm -f {node_path.PROFILE_PATH}', command)
        self.assertNotIn('rm -rf', command)
        self.assertNotIn('profile.d/*', command)


class ApplyNodeTest(SimpleTestCase):
    """``apply_node``：两处配置各走各的，一处断了另一处还有用。"""

    def setUp(self):
        """准备节点快照与上下文。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上准备数据。
        """
        self.node = make_node('114.67.89.90', cluster_ip='172.30.0.16', roles=['master'])
        self.context = RecordingContext()

    def apply(self, client, bin_dir='/opt/kube/bin', **overrides):
        """在打桩前提下跑一次。

        参数：`client` 表示假客户端；`bin_dir` 表示二进制目录；`overrides` 表示覆盖的调用参数。
        返回：返回报告字典。
        副作用：在假节点上执行被记录下来的命令。
        """
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_path.apply_node(
            self.context, self.node, bin_dir, {'user': 'root', 'port': 22}, **kwargs,
        )

    def test_writes_both_places(self):
        """两处都写成：登录 shell 与非交互式 SSH 各取一处。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT})

        report = self.apply(client)

        self.assertIs(report['profile'], True)
        self.assertIs(report['environment'], True)
        self.assertIn('export PATH=/opt/kube/bin:$PATH', client.files[node_path.PROFILE_PATH])
        self.assertIn('/opt/kube/bin', client.files[node_path.ENVIRONMENT_PATH])
        self.assertEqual(client.modes[node_path.PROFILE_PATH], '644')

    def test_is_idempotent(self):
        """跑两次的结果与一次相同：重试、重装都不该在 PATH 里堆出一串重复目录。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT})
        self.apply(client)
        first = client.files[node_path.ENVIRONMENT_PATH]

        self.apply(client)

        self.assertEqual(client.files[node_path.ENVIRONMENT_PATH], first)
        self.assertEqual(first.count('/opt/kube/bin'), 1)

    def test_environment_write_is_skipped_when_it_cannot_be_read(self):
        """读不到就跳过并写警告：这是唯一动别人文件的地方，读不懂就不动。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT},
                          unreadable=[node_path.ENVIRONMENT_PATH])

        report = self.apply(client)

        self.assertIs(report['profile'], True)
        self.assertIn('读不到', report['environment'])
        self.assertIn('node_path_partial', self.context.event_types())
        # 读不懂就别写：那份文件必须一个字节都没动过。
        self.assertEqual(client.files[node_path.ENVIRONMENT_PATH], PLAIN_ENVIRONMENT)

    def test_environment_is_skipped_without_a_path_line(self):
        """没有 ``PATH=`` 这一行时不动它，并在日志里说清还能怎么办。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: 'LANG=en_US.UTF-8\n'})

        report = self.apply(client)

        self.assertIn('PATH=', report['environment'])
        self.assertEqual(client.files[node_path.ENVIRONMENT_PATH], 'LANG=en_US.UTF-8\n')
        self.assertIn('warn', [item['level'] for item in self.context.events])

    def test_write_is_verified_by_reading_back(self):
        """写完回读核对：文件被别的工具改回去时不能报成功。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT})
        # 写入命令照常成功，但假装有个配置管理工具立刻把它改了回去。
        original_run = client._run

        def tampering(command):
            """先按正常路径执行，随后把 /etc/environment 恢复成原样。"""
            result = original_run(command)
            if command.startswith("printf '%s'"):
                client.files[node_path.ENVIRONMENT_PATH] = PLAIN_ENVIRONMENT
            return result

        client._run = tampering

        report = self.apply(client)

        self.assertIn('回读', report['environment'])
        self.assertIn('node_path_partial', self.context.event_types())

    def test_profile_failure_alone_still_reports_and_keeps_the_other(self):
        """一处失败不回滚另一处，也不抛错：两处是独立的两条路。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT})
        original_run = client._run

        def handler(command):
            """只让写 profile.d 的那条命令失败。"""
            if node_path.PROFILE_PATH in command and command.startswith("printf '%s'"):
                return 1, '', 'Read-only file system'
            return original_run(command)

        client._run = handler

        report = self.apply(client)

        self.assertIn('Read-only file system', report['profile'])
        self.assertIs(report['environment'], True)

    def test_both_failures_raise(self):
        """两处都没写成才抛错 —— 这时确实什么都没做到，调用方要记一条警告。"""
        client = FakeNode()

        def handler(_command):
            """所有命令都失败。"""
            return 1, '', 'Permission denied'

        client._run = handler

        with self.assertRaises(node_path.NodePathError) as caught:
            self.apply(client)

        self.assertEqual(caught.exception.error_code, 'node_path_failed')
        self.assertIn('Permission denied', caught.exception.message)

    def test_uses_the_directory_it_was_given(self):
        """跟着调用方给的目录走（它来自渲染后的 inventory），不写死 /opt/kube/bin。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT})

        self.apply(client, bin_dir='/data/kube/bin')

        self.assertIn('export PATH=/data/kube/bin:$PATH', client.files[node_path.PROFILE_PATH])
        self.assertIn('PATH="/data/kube/bin:', client.files[node_path.ENVIRONMENT_PATH])


class RemoveNodeTest(SimpleTestCase):
    """``remove_node``：撤销要对称，但不动 kubeasz 自己的那批命令。"""

    def setUp(self):
        """准备节点快照与上下文。"""
        self.node = make_node('114.67.89.90', cluster_ip='172.30.0.16', roles=['master'])
        self.context = RecordingContext()

    def remove(self, client, bin_dir='/opt/kube/bin', **overrides):
        """在打桩前提下跑一次撤销。"""
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_path.remove_node(
            self.context, self.node, bin_dir, {'user': 'root', 'port': 22}, **kwargs,
        )

    def installed(self):
        """返回一台已经配置过 PATH 的假节点。"""
        client = FakeNode(files={
            node_path.PROFILE_PATH: node_path.profile_content('/opt/kube/bin'),
            node_path.ENVIRONMENT_PATH: 'PATH="/opt/kube/bin:/usr/bin"\n',
        })
        return client

    def test_removes_both_places(self):
        """文件和 PATH 里那一段都要撤掉，删完才是真的回到原样。"""
        client = self.installed()

        report = self.remove(client)

        self.assertIs(report['profile'], True)
        self.assertIs(report['environment'], True)
        self.assertNotIn(node_path.PROFILE_PATH, client.files)
        self.assertEqual(client.files[node_path.ENVIRONMENT_PATH], 'PATH="/usr/bin"\n')

    def test_is_idempotent_when_nothing_was_installed(self):
        """本来就没装过也照样算成功：清理流程常在机器状态不明的时候跑。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT})

        report = self.remove(client)

        self.assertEqual(report['profile'], '本来就不在')
        self.assertIs(report['environment'], True)

    def test_keeps_a_file_it_did_not_write(self):
        """文件内容不是平台写的就留着：这个路径下也可能有运维自己的配置。"""
        client = FakeNode(files={node_path.PROFILE_PATH: 'export FOO=1\n'})

        report = self.remove(client)

        self.assertIn('保留', report['profile'])
        self.assertEqual(client.files[node_path.PROFILE_PATH], 'export FOO=1\n')

    def test_unreadable_environment_does_not_raise(self):
        """读不到只记一行就跳过：残留这一条不影响重新部署。"""
        client = FakeNode(files={node_path.ENVIRONMENT_PATH: PLAIN_ENVIRONMENT},
                          unreadable=[node_path.ENVIRONMENT_PATH])

        report = self.remove(client)

        self.assertIn('跳过', report['environment'])
        self.assertEqual(client.files[node_path.ENVIRONMENT_PATH], PLAIN_ENVIRONMENT)

    def test_does_not_touch_the_bin_directory(self):
        """``bin_dir`` 里的命令归 kubeasz 管，它的清理剧本本来就不删，平台也不越权删。"""
        client = self.installed()

        self.remove(client)

        for command in client.commands:
            self.assertNotIn('rm -rf', command)
            self.assertNotIn('/opt/kube/bin/', command)


class BinDirForTest(SimpleTestCase):
    """``bin_dir_for``：目录取自渲染后的 inventory，读不到时退回上游默认值。"""

    def setUp(self):
        """准备临时目录。"""
        self.directory = Path(tempfile.mkdtemp(prefix='node-path-'))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def write(self, text):
        """写一份 inventory 并返回其路径。"""
        path = self.directory / 'hosts'
        path.write_text(text, encoding='utf-8')
        return path

    def test_reads_the_declared_value(self):
        """操作员改过就跟着改，否则会往 PATH 里加一个空目录。"""
        path = self.write('[all:vars]\nbin_dir="/data/kube/bin"\n')
        self.assertEqual(node_path.bin_dir_for(path), '/data/kube/bin')

    def test_falls_back_to_the_upstream_default(self):
        """没写这个变量时用上游模板的值（``/opt/kube/bin``）。"""
        path = self.write('[all:vars]\nkube_ver="1.29.4"\n')
        self.assertEqual(node_path.bin_dir_for(path), compiler.DEFAULT_BIN_DIR)

    def test_missing_file_falls_back_instead_of_raising(self):
        """文件不在不抛错：这一步是给人用的便利配置，不值得把部署拦下来。"""
        self.assertEqual(
            node_path.bin_dir_for(self.directory / 'nope'), compiler.DEFAULT_BIN_DIR,
        )
