"""给节点补写 kubectl 配置（``/root/.kube/config``）的用例：命令形状、只补不覆盖、字节核对。

真实背景见 ``services/node_kubeconfig.py`` 开头：kubeasz 的 ``roles/prepare`` 既建
``/root/.kube`` 又拷 kubeconfig，但它带幂等标记 ``/opt/kubeasz_prepare_tasks``；标记还在时整个
角色跳过，而平台上一轮的残留清理已经把 ``/root/.kube`` 删掉了 —— 集群是好的，人登上 master
敲 ``kubectl`` 却只会连 ``localhost:8080`` 报 connection refused（2026-10-12 的 JD-k8s）。

命令形状是纯函数，远程动作经 ``client_factory`` 注入假客户端。这个假客户端把节点上的
``/root/.kube/config`` 放在内存里，并**真的**按生成的命令走一遍 base64 解码与落盘，
因此「探测 → 写入 → 按字节数核对」这条链路是被真跑通的，不只是断言命令字符串。
"""

import base64
import re

from django.test import SimpleTestCase

from k8s_deployment.services import node_kubeconfig

from .test_node_registry import _RecordingContext, _Stream, make_node

# 一份典型的 kubeasz kubeconfig：里面**内嵌** admin 客户端证书与私钥，所以下面所有用例都
# 顺带断言这两段不会出现在日志与异常信息里。
KUBECONFIG_TEXT = (
    'apiVersion: v1\n'
    'clusters:\n'
    '- cluster:\n'
    '    certificate-authority-data: SECRET-CA-DATA\n'
    '    server: https://172.30.0.132:6443\n'
    '  name: jd-k8s\n'
    'contexts:\n'
    '- context:\n'
    '    cluster: jd-k8s\n'
    '    user: kubernetes-admin\n'
    '  name: context-jd-k8s\n'
    'current-context: context-jd-k8s\n'
    'kind: Config\n'
    'users:\n'
    '- name: kubernetes-admin\n'
    '  user:\n'
    '    client-certificate-data: SECRET-CLIENT-CERT\n'
    '    client-key-data: SECRET-CLIENT-KEY\n'
)
SECRET_NEEDLE = 'SECRET-CLIENT-KEY'

# ``present_command`` 的形状：``-s``（非空）判定 + 两个回执标记。
PRESENT_COMMAND = re.compile(
    r'^if \[ -s (?P<path>\S+) \]; then echo (?P<present>\S+); else echo (?P<missing>\S+); fi$',
)
# ``write_command`` 的形状：base64 解码落盘、chmod、末尾按 ``wc -c`` 回一个字节数。
# 用反向引用把临时路径绑在一起，命令改动大了会立刻失配（假客户端认不出来就回非零退出码，
# 用例会以「写入失败」失败，而不是静默放过）。
WRITE_COMMAND = re.compile(
    r"mkdir -p (?P<dir>\S+) && printf '%s' '(?P<payload>[A-Za-z0-9+/=]*)' \| base64 -d > (?P<path>\S+) && "
    r'chmod (?P<mode>\d+) (?P=path) && '
    r'echo (?P<landed>\S+):\$\(wc -c < (?P=path)\) \|\| echo (?P=landed):0',
)


class FakeNode:
    """把节点上的 ``/root/.kube/config`` 放进内存，让生成的命令真的改到东西。

    只实现被测代码会用到的那两条命令形状（探测、写入）；认不出来的命令回非零退出码。
    ``fail_present`` / ``fail_write`` / ``landed_override`` 用来造「命令跑不起来」「写进去的
    字节数对不上」这两种失败。

    参数：`size` 表示初始文件的字节数（``None`` 表示没有这个文件，``0`` 表示空文件）；
        `fail_present` 表示探测命令失败；`fail_write` 表示写入命令失败；`landed_override`
        表示写入命令回报的字节数（用来模拟截断）。
    """

    def __init__(self, size=None, content=None, fail_present=False, fail_write=False,
                 landed_override=None):
        """初始化。

        参数：见类文档。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.commands = []
        self.modes = {}
        self.size = size
        self.files = {}
        if content is not None:
            self.files[node_kubeconfig.KUBECONFIG_PATH] = content
            self.size = len(content.encode('utf-8'))
        self.fail_present = fail_present
        self.fail_write = fail_write
        self.landed_override = landed_override
        self.closed = False

    def exec_command(self, command, timeout=None):
        """按命令文本在内存文件上执行一次。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(None, stdout, stderr)``，形状与 paramiko 一致。
        副作用：可能改动内存里的文件。
        """
        self.commands.append(command)
        code, out, err = self._run(command)
        return None, _Stream(out, code), _Stream(err, code)

    def _run(self, command):
        """命令的实际实现，返回 ``(退出码, 标准输出, 标准错误)``。"""
        match = PRESENT_COMMAND.fullmatch(command)
        if match:
            self._check(match.group('path'), node_kubeconfig.KUBECONFIG_PATH)
            self._check(match.group('present'), node_kubeconfig.PRESENT)
            self._check(match.group('missing'), node_kubeconfig.MISSING)
            if self.fail_present:
                return 1, '', 'Permission denied'
            if self.files.get(node_kubeconfig.KUBECONFIG_PATH):
                return 0, f"{match.group('present')}\n", ''
            return 0, f"{match.group('missing')}\n", ''
        match = WRITE_COMMAND.fullmatch(command)
        if match:
            self._check(match.group('dir'), node_kubeconfig.KUBE_DIR)
            self._check(match.group('path'), node_kubeconfig.KUBECONFIG_PATH)
            self._check(match.group('mode'), node_kubeconfig.MODE)
            self._check(match.group('landed'), node_kubeconfig.LANDED)
            if self.fail_write:
                return 1, '', 'Read-only file system'
            text = base64.b64decode(match.group('payload')).decode('utf-8')
            self.files[match.group('path')] = text
            self.modes[match.group('path')] = match.group('mode')
            landed = len(text.encode('utf-8'))
            if self.landed_override is not None:
                landed = self.landed_override
            return 0, f"{match.group('landed')}:{landed}\n", ''
        return 1, '', f'unexpected command: {command}'

    @staticmethod
    def _check(actual, expected):
        """确认命令里的常量与模块导出的常量一致（改了模块这里就跟不上）。"""
        if actual != expected:
            raise AssertionError(f'命令里是 {actual!r}，模块导出的是 {expected!r}')

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True

    def fired(self, needle):
        """取回所有包含给定片段的命令。"""
        return [item for item in self.commands if needle in item]

    def content(self):
        """返回节点上那份文件当前的内容（没有就是 None）。"""
        return self.files.get(node_kubeconfig.KUBECONFIG_PATH)


class PresentCommandTest(SimpleTestCase):
    """``present_command``：只探测，不产生任何副作用。"""

    def test_probes_the_platform_path(self):
        """探的是平台自己会写的那个路径 —— 探错了等于永远重写。"""
        self.assertIn(node_kubeconfig.KUBECONFIG_PATH, node_kubeconfig.present_command())

    def test_treats_an_empty_file_as_missing(self):
        """``-s`` 而不是 ``-e``：0 字节的 config 对 kubectl 等于没有，得当缺失重写。"""
        command = node_kubeconfig.present_command()
        self.assertIn(f'if [ -s {node_kubeconfig.KUBECONFIG_PATH} ]', command)
        self.assertNotIn(' -e ', command)

    def test_both_receipts_are_printable(self):
        """两个回执各是一个词：调用方按包含关系判断，不需要解析更复杂的输出。"""
        command = node_kubeconfig.present_command()
        self.assertIn(node_kubeconfig.PRESENT, command)
        self.assertIn(node_kubeconfig.MISSING, command)

    def test_does_not_touch_the_file(self):
        """探测命令里不能出现写动作。"""
        command = node_kubeconfig.present_command()
        for action in ('rm ', 'mv ', '>', 'chmod', 'mkdir'):
            self.assertNotIn(action, command)


class WriteCommandTest(SimpleTestCase):
    """``write_command``：base64 落盘、权限照抄 kubeasz、回显字节数。"""

    def test_payload_round_trips(self):
        """payload 是 UTF-8 文本的 base64：解码回来必须一字不差（含中文与换行）。"""
        text = KUBECONFIG_TEXT + '# 注释：中文\n'
        match = WRITE_COMMAND.search(node_kubeconfig.write_command(text))
        self.assertIsNotNone(match)
        self.assertEqual(base64.b64decode(match.group('payload')).decode('utf-8'), text)

    def test_source_is_quoted_safely(self):
        """payload 只含 ``[A-Za-z0-9+/=]``：塞进单引号里绝对安全，不必再做转义。"""
        payload = WRITE_COMMAND.search(node_kubeconfig.write_command(KUBECONFIG_TEXT)).group('payload')
        self.assertRegex(payload, r'^[A-Za-z0-9+/=]*$')

    def test_creates_the_directory_and_restricts_the_mode(self):
        """``/root/.kube`` 可能不存在（清理时被删了），权限照 kubeasz 那份：只有 root 能读。"""
        command = node_kubeconfig.write_command(KUBECONFIG_TEXT)
        self.assertIn(f'mkdir -p {node_kubeconfig.KUBE_DIR}', command)
        self.assertIn(f'chmod {node_kubeconfig.MODE} {node_kubeconfig.KUBECONFIG_PATH}', command)
        self.assertEqual(node_kubeconfig.MODE, '400')

    def test_never_echoes_the_content(self):
        """**不回显内容**：这是内置 admin 证书与私钥的 kubeconfig，``cat`` 出来就进任务日志了。

        与 ``node_path.write_text_command`` 的差别就在这里 —— 那边结尾是 ``cat``，
        这边结尾只有 ``wc -c``。
        """
        command = node_kubeconfig.write_command(KUBECONFIG_TEXT)
        self.assertNotIn('cat ', command)
        self.assertNotIn(SECRET_NEEDLE, command)
        self.assertIn(f'wc -c < {node_kubeconfig.KUBECONFIG_PATH}', command)

    def test_reports_zero_when_the_command_fails(self):
        """命令链断了也要吐一个字节数回执，让「命令没跑起来」与「写成了 0 字节」分得开。"""
        command = node_kubeconfig.write_command(KUBECONFIG_TEXT)
        self.assertTrue(command.endswith(f'|| echo {node_kubeconfig.LANDED}:0'))


class ParseLandedTest(SimpleTestCase):
    """``parse_landed``：从回执里取字节数，取不出来返回 -1。"""

    def test_reads_the_receipt(self):
        """正常回执。"""
        self.assertEqual(node_kubeconfig.parse_landed('K8SMANAGER_KUBECONFIG_BYTES:1234\n'), 1234)

    def test_reads_it_among_other_output(self):
        """回执前后可能有别的东西（登录横幅、mount 提示），按行找。"""
        output = 'Welcome to CentOS\nK8SMANAGER_KUBECONFIG_BYTES:42\n'
        self.assertEqual(node_kubeconfig.parse_landed(output), 42)

    def test_missing_receipt_is_minus_one(self):
        """没有回执就是没把握，不能当成功。"""
        self.assertEqual(node_kubeconfig.parse_landed(''), -1)
        self.assertEqual(node_kubeconfig.parse_landed('LEFT: /root/.kube'), -1)

    def test_garbage_value_is_minus_one(self):
        """回执后面不是数字时也算读不出来。"""
        self.assertEqual(node_kubeconfig.parse_landed('K8SMANAGER_KUBECONFIG_BYTES:abc'), -1)


class EnsureNodeTest(SimpleTestCase):
    """``ensure_node``：先探测再决定，只补不覆盖，写完按字节数核对。"""

    def setUp(self):
        """准备节点快照与上下文。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上准备数据。
        """
        self.node = make_node('114.67.203.82', cluster_ip='172.30.0.132', roles=['master'])
        self.context = _RecordingContext()

    def ensure(self, client, text=KUBECONFIG_TEXT, node=None, **overrides):
        """在打桩前提下跑一次。

        参数：`client` 表示假客户端；`text` 表示 kubeconfig 文本；`node` 表示换一个节点快照；
            `overrides` 表示覆盖的调用参数。
        返回：返回报告字典。
        副作用：在假节点上执行被记录下来的命令。
        """
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_kubeconfig.ensure_node(
            self.context, node or self.node, text, {'user': 'root', 'port': 22}, **kwargs,
        )

    def test_writes_when_missing(self):
        """节点上没有配置时就补一份 —— 这正是 JD-k8s 的状态。"""
        client = FakeNode()

        report = self.ensure(client)

        self.assertEqual(report['action'], 'written')
        self.assertEqual(report['label'], '114.67.203.82')
        self.assertEqual(client.content(), KUBECONFIG_TEXT)
        self.assertEqual(client.modes[node_kubeconfig.KUBECONFIG_PATH], '400')
        self.assertEqual(report['bytes'], len(KUBECONFIG_TEXT.encode('utf-8')))

    def test_empty_file_counts_as_missing(self):
        """0 字节的文件当缺失重写：它是「上次写到一半断了」最容易留下的样子。"""
        client = FakeNode(content='')

        report = self.ensure(client)

        self.assertEqual(report['action'], 'written')
        self.assertEqual(client.content(), KUBECONFIG_TEXT)

    def test_keeps_an_existing_config(self):
        """已存在（非空）就一个字都不改：可能是操作员自己那份，指向别的集群。"""
        client = FakeNode(content='operator-owned\n')

        report = self.ensure(client)

        self.assertEqual(report['action'], 'kept')
        self.assertEqual(client.content(), 'operator-owned\n')
        self.assertEqual(client.fired('base64 -d'), [])
        self.assertIn('保持原样', self.context.text())

    def test_is_idempotent(self):
        """跑两次的结果与一次相同：第二次探测到文件在，就不再动它。"""
        client = FakeNode()
        self.ensure(client)

        report = self.ensure(client)

        self.assertEqual(report['action'], 'kept')
        self.assertEqual(client.content(), KUBECONFIG_TEXT)
        self.assertEqual(len(client.fired('base64 -d')), 1)

    def test_opens_and_closes_one_session(self):
        """一条连接干完探测与写入两件事，并确保被关掉（pty/连接泄漏比失败难查）。"""
        client = FakeNode()
        sessions = []

        def factory(**kwargs):
            """记录并返回同一个假客户端。"""
            sessions.append(kwargs)
            return client

        self.ensure(client, client_factory=factory)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['host'], '114.67.203.82')
        self.assertTrue(client.closed)

    def test_closes_the_session_even_when_it_fails(self):
        """出错路径同样要关连接。"""
        client = FakeNode(fail_write=True)

        with self.assertRaises(node_kubeconfig.NodeKubeconfigError):
            self.ensure(client)

        self.assertTrue(client.closed)

    def test_write_is_verified_by_byte_count(self):
        """写完按字节数核对：文件被截断时不能报成功。"""
        client = FakeNode(landed_override=7)

        with self.assertRaises(node_kubeconfig.NodeKubeconfigError) as caught:
            self.ensure(client)

        self.assertEqual(caught.exception.error_code, 'node_kubeconfig_failed')
        self.assertIn('期望', caught.exception.message)

    def test_write_failure_is_reported(self):
        """写入命令非零退出时报失败并带上远端那句话。"""
        client = FakeNode(fail_write=True)

        with self.assertRaises(node_kubeconfig.NodeKubeconfigError) as caught:
            self.ensure(client)

        self.assertIn('Read-only file system', caught.exception.message)

    def test_probe_failure_is_reported(self):
        """连探测都跑不起来时不往下猜，直接报失败。"""
        client = FakeNode(fail_present=True)

        with self.assertRaises(node_kubeconfig.NodeKubeconfigError) as caught:
            self.ensure(client)

        self.assertIn('Permission denied', caught.exception.message)
        self.assertEqual(client.fired('base64 -d'), [])

    def test_missing_source_text_raises_before_connecting(self):
        """平台上还没有 kubeconfig 时提前失败，不白连一台机器。"""
        client = FakeNode()
        sessions = []

        def factory(**kwargs):
            """记录连接请求。"""
            sessions.append(kwargs)
            return client

        with self.assertRaises(node_kubeconfig.NodeKubeconfigError) as caught:
            self.ensure(client, text='   ', client_factory=factory)

        self.assertEqual(caught.exception.error_code, 'node_kubeconfig_missing_source')
        self.assertEqual(sessions, [])

    def test_success_log_names_the_symptom(self):
        """日志要说到点子上：这台机器的 kubectl 之前会连 localhost:8080 报错。"""
        self.ensure(FakeNode())

        logs = self.context.text()
        self.assertIn(node_kubeconfig.KUBECONFIG_PATH, logs)
        self.assertIn('localhost:8080', logs)
        self.assertIn(str(len(KUBECONFIG_TEXT.encode('utf-8'))), logs)

    def test_logs_and_errors_never_carry_the_content(self):
        """任何日志与异常信息里都不出现 kubeconfig 的内容，也不出现它的 base64。"""
        payload = base64.b64encode(KUBECONFIG_TEXT.encode('utf-8')).decode('ascii')
        clients = [
            FakeNode(),
            FakeNode(content='operator-owned\n'),
            FakeNode(fail_write=True),
            FakeNode(landed_override=3),
        ]
        for client in clients:
            self.context = _RecordingContext()
            try:
                self.ensure(client)
            except node_kubeconfig.NodeKubeconfigError as exc:
                self.assertNotIn(SECRET_NEEDLE, exc.message)
                self.assertNotIn(payload, exc.message)
            text = self.context.text()
            self.assertNotIn(SECRET_NEEDLE, text)
            self.assertNotIn(payload, text)

    def test_labels_the_node_by_ip(self):
        """报告里的 label 用 IP：日志要能按机器读。"""
        report = self.ensure(FakeNode(), node=make_node('10.0.0.9'))
        self.assertEqual(report['label'], '10.0.0.9')
