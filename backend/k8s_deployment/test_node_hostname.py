"""把节点主机名改成节点名的用例：命令文本、远程动作、失败处理。

真实背景见 ``services/node_hostname.py``：节点名（inventory 里的 ``k8s_nodename``）是 kubelet
注册到集群的名字，主机名是操作系统自己的名字。两者不一致时，操作员登上一台机器看到的是一回事、
``kubectl get node`` 看到的是另一回事。

远程动作经 ``client_factory`` 注入假客户端。这个假客户端把 ``/proc/sys/kernel/hostname`` 与
``/etc/hostname`` 两个值放在内存里，并按生成的命令**真的走一遍**改写、按 ``/etc/hosts`` 的真实
内容回答「有没有这个名字」—— 因此幂等性、回读核对、注释行不算命中这些性质是被跑通的，不是靠
断言命令字符串猜出来的。
"""

import base64
import re

from django.test import SimpleTestCase

from k8s_deployment.services import node_hostname, node_path

from .test_node_path import WRITE_COMMAND, RecordingContext
from .test_node_registry import _Stream, make_node

# ``set_name_command`` 里那一段改内核主机名的前缀。用正则取回目标名字，而不是整串比对命令
# （命令里还嵌着 hostnamectl 的开关与 base64 写文件那一段，整串比对会因为无关的调整一直失配）。
HOSTNAME_COMMAND = re.compile(r"hostname '([^']*)';")

# ``hosts_lookup_command`` 里那个整词匹配的正则，取出模式本身来在内存里跑一遍。
# POSIX 字符类在这里是字面量（``[[:space:]]``），所以按前后缀字符串剥，不用正则去猜。
HOSTS_LOOKUP = re.compile(r"grep -Eq '(.*)' && echo PRESENT")
HOSTS_BOUNDARY = ('(^|[[:space:]])', '([[:space:]]|$)')

# ``hosts_append_command`` 里追加的那一行。只取「写了哪一行」，不解析后面的核对命令 ——
# 核对结果由假客户端按 ``/etc/hosts`` 的真实内容算，见 ``FakeNode._run``。
HOSTS_APPEND = re.compile(r'printf "(\S+) ([^\\]*)\\n" >> (\S+)')


class FakeNode:
    """把节点上的主机名与 hosts 放进内存，让命令真的改到东西。

    只实现被测代码会用到的那几种命令形状：探测、改名（含写 ``/etc/hostname``）、查 hosts、
    追加 hosts。认不出来的命令回非零退出码 —— 命令改了而假客户端没跟着改时，用例会立刻失败。
    """

    def __init__(self, kernel='localhost', hostname_file='localhost\n', hosts='127.0.0.1 localhost\n', ignore_writes=False):
        """初始化。

        参数：`kernel` 表示内核里的主机名；`hostname_file` 表示 ``/etc/hostname`` 的内容；
            `hosts` 表示 ``/etc/hosts`` 的内容；`ignore_writes` 表示是否假装写入没生效
            （模拟「写命令退出码为 0 但文件没变」，用来验回读核对）。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.kernel = kernel
        self.files = {node_hostname.HOSTNAME_FILE: hostname_file, node_hostname.HOSTS_FILE: hosts}
        self.ignore_writes = ignore_writes
        self.commands = []
        self.modes = {}

    def exec_command(self, command, timeout=None):
        """按命令文本在内存里执行一次。"""
        self.commands.append(command)
        code, out, err = self._run(command)
        return None, _Stream(out, code), _Stream(err, code)

    def _run(self, command):
        """命令的实际实现，返回 ``(退出码, 标准输出, 标准错误)``。"""
        if command.startswith('echo "KERNEL'):
            return 0, f'KERNEL {self.kernel}\nFILE {self.files[node_hostname.HOSTNAME_FILE].strip()}', ''
        match = HOSTNAME_COMMAND.search(command)
        if match:
            if not self.ignore_writes:
                self.kernel = match.group(1)
            write = WRITE_COMMAND.search(command)
            if write:
                path = write.group('path')
                self.modes[path] = write.group('mode')
                if not self.ignore_writes:
                    self.files[path] = base64.b64decode(write.group(1)).decode('utf-8')
                return 0, self.files[path], ''
            return 0, '', ''
        match = HOSTS_LOOKUP.search(command)
        if match:
            pattern = match.group(1)
            if not (pattern.startswith(HOSTS_BOUNDARY[0]) and pattern.endswith(HOSTS_BOUNDARY[1])):
                return 1, '', f'unexpected lookup pattern: {pattern}'
            name = pattern[len(HOSTS_BOUNDARY[0]):-len(HOSTS_BOUNDARY[1])].replace('\\.', '.')
            stripped = re.sub(r'#.*', '', self.files[node_hostname.HOSTS_FILE])
            for line in stripped.splitlines():
                if re.search(rf'(^|\s){re.escape(name)}(\s|$)', line):
                    return 0, 'PRESENT', ''
            return 0, 'ABSENT', ''
        match = HOSTS_APPEND.search(command)
        if match:
            address, name = match.group(1), match.group(2)
            self.files[node_hostname.HOSTS_FILE] += f'{address} {name}\n'
            # 回执照抄**真实 shell** 的行为，而不是照抄「本意」：命令末尾自己
            # ``echo PRESENT || echo ABSENT`` 才有输出；若只留一句 ``grep -qw``（``-q``
            # 静默，只有退出码），真机上就是「成功但输出为空」—— 调用方会把空输出读成失败，
            # 现场那个「写不进 /etc/hosts … 退出码 0」的误报正是这么来的。假客户端必须照实
            # 表现，否则它会替被测代码圆场，把这类缺陷一直放过去。
            if 'echo PRESENT' not in command:
                return 0, '', ''
            found = any(
                re.search(rf'(^|\s){re.escape(name)}(\s|$)', line)
                for line in re.sub(r'#.*', '', self.files[node_hostname.HOSTS_FILE]).splitlines()
            )
            return 0, 'PRESENT' if found else 'ABSENT', ''
        return 1, '', f'unexpected command: {command}'

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True

    def hosts_lines(self):
        """返回 ``/etc/hosts`` 里出现某个名字的次数，用来验幂等。"""
        return [line for line in self.files[node_hostname.HOSTS_FILE].splitlines() if line.strip()]


class _ReadOnlyHostsClient(FakeNode):
    """把 ``/etc/hosts`` 当成只读文件系统的客户端。

    真实场景是容器或加固过的机器上 ``/etc/hosts`` 被挂成只读（或 ``chattr +i``）：那条
    ``printf >> /etc/hosts`` 的退出码非零、stderr 里是 sshd 转回来的原话。
    """

    def _run(self, command):
        """凡是往 ``/etc/hosts`` 追加的命令一律失败，其余命令照旧走内存实现。"""
        if f'>> {node_hostname.HOSTS_FILE}' in command:
            return 1, '', f'{node_hostname.HOSTS_FILE}: Read-only file system'
        return super()._run(command)


class CommandTextTest(SimpleTestCase):
    """命令文本：不连机器就能看出来的那几条性质。"""

    def test_set_name_does_both_immediate_and_persistent(self):
        """立即生效与重启后生效，两件都要有：只做一件都会留下「重启就变回去」的坑。"""
        command = node_hostname.set_name_command('master-01')
        self.assertIn("hostname 'master-01'", command)
        self.assertIn('hostnamectl set-hostname', command)
        self.assertIn(node_hostname.HOSTNAME_FILE, command)

    def test_set_name_survives_a_missing_hostnamectl(self):
        """没有 systemd 的机器（容器、老发行版）上 ``hostnamectl`` 不存在，不能因此整条失败。"""
        command = node_hostname.set_name_command('worker-01')
        self.assertIn('command -v hostnamectl', command)
        self.assertIn('|| true', command)

    def test_file_write_goes_through_the_atomic_helper(self):
        """``/etc/hostname`` 的写入复用 ``node_path`` 的原子写，不自己拼一条。"""
        self.assertIn(
            node_path.write_text_command(node_hostname.HOSTNAME_FILE, 'master-01\n', '644'),
            node_hostname.set_name_command('master-01'),
        )

    def test_hosts_lookup_ignores_comments(self):
        """注释行里的名字不算能解析 —— 生产机 ``/etc/hosts`` 上常留着被注释掉的旧条目。"""
        command = node_hostname.hosts_lookup_command('master-01')
        self.assertIn("sed 's/#.*//'", command)
        self.assertIn('PRESENT', command)

    def test_hosts_lookup_escapes_the_dot(self):
        """名字里的 ``.`` 要转义：否则 ``master.01`` 会命中 ``masterX01``。"""
        self.assertIn('master\\.01', node_hostname.hosts_lookup_command('master.01'))

    def test_hosts_append_reports_a_receipt(self):
        """追加命令必须自己打出核对结果。

        ``grep -q`` 是静默的（``-q`` 的字面意思就是「不输出、只看退出码」），只留它一句，
        调用方拿到的输出就是空的 —— 「写进去了」与「压根没跑」长得一模一样，于是每一次
        成功都被读成失败。这不是假设：现场三台节点全部报「写不进 /etc/hosts … 退出码 0」，
        而「退出码 0」正是成功。
        """
        command = node_hostname.hosts_append_command('master-01')

        self.assertIn('echo PRESENT', command)
        self.assertIn('echo ABSENT', command)
        # 写失败要留在整条命令的退出码上：追加与核对之间是 ``&&``（短路），不是 ``;`` ——
        # 否则 ``/etc/hosts`` 只读时那条 ``printf`` 的非零退出码会被后面的回执盖成 0。
        self.assertIn(f'>> {node_hostname.HOSTS_FILE} && (', command)

    def test_hosts_append_escapes_the_dot(self):
        """同上，追加后的核对也不能让 ``master.01`` 被 ``master-01`` 顶替成「已存在」。"""
        self.assertIn('master\\.01', node_hostname.hosts_append_command('master.01'))

    def test_manual_command_is_runnable(self):
        """失败时要给出一条能照抄的命令，且带上了目标名字。"""
        command = node_hostname.manual_command('master-01')
        self.assertIn('hostname', command)
        self.assertIn('master-01', command)
        self.assertIn(node_hostname.HOSTNAME_FILE, command)


class ProbeParseTest(SimpleTestCase):
    """``parse_probe``：两个值分开看。"""

    def test_reads_both_values(self):
        """内核值与文件值各归各位。"""
        result = node_hostname.parse_probe('KERNEL node1\nFILE node2\n')
        self.assertEqual(result['kernel'], 'node1')
        self.assertEqual(result['file'], 'node2')

    def test_missing_lines_are_empty(self):
        """缺失的行留空串，不报错：判不出来的东西不该被当成某个默认值。"""
        self.assertEqual(node_hostname.parse_probe(''), {'kernel': '', 'file': ''})


class ApplyNodeTest(SimpleTestCase):
    """``apply_node``：改名、回读、补 hosts。"""

    def setUp(self):
        """准备节点快照与上下文。"""
        self.node = make_node('192.168.1.94', cluster_ip='172.30.0.16', roles=['master'])
        self.context = RecordingContext()

    def apply(self, client, name='master-01', **overrides):
        """在打桩前提下跑一次 ``apply_node``。"""
        kwargs = {'key_path': 'k', 'client_factory': lambda **_kw: client}
        kwargs.update(overrides)
        return node_hostname.apply_node(self.context, self.node, name, {'user': 'root', 'port': 22}, **kwargs)

    def test_renames_both_places(self):
        """名字不同时：内核值与 ``/etc/hostname`` 都要变成节点名。"""
        client = FakeNode(kernel='localhost', hostname_file='localhost\n')

        report = self.apply(client)

        self.assertTrue(report['changed'])
        self.assertEqual(report['previous'], 'localhost')
        self.assertEqual(client.kernel, 'master-01')
        self.assertEqual(client.files[node_hostname.HOSTNAME_FILE], 'master-01\n')
        self.assertEqual(client.modes[node_hostname.HOSTNAME_FILE], '644')

    def test_is_idempotent(self):
        """名字已经对就不写文件：重复部署不该每次都去动系统文件。"""
        client = FakeNode(kernel='master-01', hostname_file='master-01\n', hosts='')

        report = self.apply(client)

        self.assertFalse(report['changed'])
        self.assertEqual(client.modes, {})
        self.assertEqual(report['hosts'], 'appended')

    def test_hosts_entry_is_not_duplicated(self):
        """``/etc/hosts`` 里已经有了就追加：跑两次只多一行，不是两行。"""
        client = FakeNode(kernel='localhost', hostname_file='localhost\n')
        self.apply(client)
        first = list(client.hosts_lines())
        self.apply(client)

        self.assertEqual(client.hosts_lines(), first)
        self.assertEqual(len(first), 2)

    def test_commented_name_does_not_count_as_present(self):
        """注释掉的旧条目不算能解析，该补还是要补。"""
        client = FakeNode(hosts='127.0.0.1 localhost\n# 127.0.0.1 master-01 master-02\n')

        report = self.apply(client)

        self.assertEqual(report['hosts'], 'appended')
        self.assertIn('127.0.0.1 master-01', client.files[node_hostname.HOSTS_FILE])

    def test_a_successful_append_is_not_reported_as_a_failure(self):
        """现场那个误报：名字补进 ``/etc/hosts`` 明明成功了，却报「写不进 … 退出码 0」。

        「退出码 0」本身就是成功，说明失败是平台自己读出来的 —— 假客户端按真实 shell 表现
        （回执是显式 ``echo`` 出来的），所以这条用例真的能把那个缺陷挡住。
        """
        client = FakeNode(kernel='localhost', hostname_file='localhost\n')

        report = self.apply(client)

        self.assertEqual(report['hosts'], 'appended')
        self.assertIn('127.0.0.1 master-01', client.files[node_hostname.HOSTS_FILE])

    def test_a_real_write_failure_keeps_its_cause(self):
        """真的写不进去（``/etc/hosts`` 只读）时：报错要带 sshd 给的原话，而不是「退出码 0」。"""
        client = _ReadOnlyHostsClient(kernel='master-01', hostname_file='master-01\n')

        with self.assertRaises(node_hostname.NodeHostnameError) as caught:
            self.apply(client)

        self.assertEqual(caught.exception.error_code, 'node_hostname_failed')
        self.assertIn('Read-only file system', caught.exception.message)
        self.assertNotIn('退出码 0', caught.exception.message)

    def test_reads_back_instead_of_trusting_the_exit_code(self):
        """写命令报成功但文件没变时要抛错：这类「静默没生效」正是要修掉的毛病。"""
        client = FakeNode(ignore_writes=True)

        with self.assertRaises(node_hostname.NodeHostnameError) as caught:
            self.apply(client)

        self.assertEqual(caught.exception.error_code, 'node_hostname_failed')
        self.assertIn('master-01', caught.exception.message)
        self.assertIn('hostname', caught.exception.message)

    def test_rejects_an_illegal_name_before_connecting(self):
        """名字不合法时在建连接之前就拒绝，别把一堆命令发到机器上才发现。"""
        client = FakeNode()

        with self.assertRaises(node_hostname.NodeHostnameError) as caught:
            self.apply(client, name='Master_01')

        self.assertEqual(caught.exception.error_code, 'node_hostname_invalid')
        self.assertEqual(client.commands, [])

    def test_logs_the_change(self):
        """改名要留一行日志：任务记录里能看到「从什么变成了什么」。"""
        self.apply(FakeNode(kernel='localhost'))

        self.assertIn('localhost → master-01', self.context.text())


class SetHostnameTest(SimpleTestCase):
    """``set_hostname``：逐台跑，失败怎么处置。"""

    def setUp(self):
        """准备两台节点与上下文。"""
        self.nodes = [
            make_node('192.168.1.94', roles=['master']),
            make_node('192.168.1.95', roles=['node']),
        ]
        self.names = {'192.168.1.94': 'master-01', '192.168.1.95': 'worker-01'}
        self.context = RecordingContext()

    def run_set(self, failing=(), strict=True, names=None):
        """把 ``apply_node`` 打桩成「按 IP 决定成功或失败」再跑一次。

        参数：`failing` 表示要失败的 IP 集合；`strict` 表示失败时是否直接中止；
            `names` 表示要用的名字表，默认用 ``self.names``。
        返回：返回 ``set_hostname`` 的结果。
        副作用：替换模块内的 ``apply_node``，退出时由 patch 还原。
        """
        from unittest.mock import patch

        def fake_apply(_context, node, name, *_args, **_kwargs):
            if str(node.ip_address) in failing:
                raise node_hostname.NodeHostnameError('node_hostname_failed', f'{name} 改不动')
            return {'label': str(node.ip_address), 'previous': 'x', 'name': name, 'changed': True, 'hosts': 'present'}

        with patch.object(node_hostname, 'apply_node', side_effect=fake_apply):
            return node_hostname.set_hostname(
                self.context, self.nodes, self.names if names is None else names, {}, strict=strict,
            )

    def test_applies_to_every_named_node(self):
        """每台有名字的机器都跑一遍。"""
        applied, failed = self.run_set()

        self.assertEqual([item['name'] for item in applied], ['master-01', 'worker-01'])
        self.assertEqual(failed, [])

    def test_strict_raises_after_trying_all_of_them(self):
        """严格模式下：先把每台都试一遍，再一次性抛出全部失败。"""
        with self.assertRaises(node_hostname.NodeHostnameError) as caught:
            self.run_set(failing={'192.168.1.95'})

        self.assertIn('worker-01', caught.exception.message)
        self.assertIn('改不动', caught.exception.message)

    def test_non_strict_collects_instead_of_raising(self):
        """非严格模式返回失败清单，交给调用方决定。"""
        applied, failed = self.run_set(failing={'192.168.1.95'}, strict=False)

        self.assertEqual(len(applied), 1)
        self.assertEqual(failed[0]['label'], '192.168.1.95')
        self.assertIn('worker-01', failed[0]['reason'])

    def test_nodes_without_a_name_are_skipped(self):
        """``names`` 里没有的机器（etcd-only、纯负载均衡）不动它们的 hostname。"""
        applied, failed = self.run_set(names={'192.168.1.94': 'master-01'})

        self.assertEqual([item['name'] for item in applied], ['master-01'])
        self.assertEqual(failed, [])


class VerifyNodeTest(SimpleTestCase):
    """``verify_node``：只读复核。"""

    def verify(self, client):
        """在打桩前提下跑一次 ``verify_node``。"""
        node = make_node('192.168.1.94', roles=['master'])
        return node_hostname.verify_node(
            node, {'user': 'root', 'port': 22}, key_path='k', client_factory=lambda **_kw: client,
        )

    def test_returns_the_kernel_name(self):
        """返回内核里的名字（kubelet 用的是它）。"""
        name, error = self.verify(FakeNode(kernel='master-01', hostname_file='master-01\n'))

        self.assertEqual(name, 'master-01')
        self.assertEqual(error, '')

    def test_reports_an_unreadable_node(self):
        """读不到时给出中文原因，不抛异常：复核失败不该让任务失败。"""
        name, error = node_hostname.verify_node(
            make_node('192.168.1.94'), {}, key_path='k', client_factory=lambda **_kw: _BrokenClient(),
        )

        self.assertEqual(name, '')
        self.assertIn('permission denied', error)

    def test_does_not_write_anything(self):
        """只读：复核不改内核值、不改文件。"""
        client = FakeNode(kernel='stale', hostname_file='stale\n')

        self.verify(client)

        self.assertEqual(client.kernel, 'stale')
        self.assertEqual(client.files[node_hostname.HOSTNAME_FILE], 'stale\n')
        self.assertEqual(client.modes, {})


class _BrokenClient:
    """一个什么命令都执行不了的客户端，用来验「读不到」这条路径。"""

    def exec_command(self, command, timeout=None):
        """恒定失败。"""
        return None, _Stream('', 1), _Stream('unexpected command: permission denied', 1)

    def close(self):
        """关闭连接。"""
        return None
