"""节点侧 Helm 客户端的用例：命令生成、远程落地与「装完真的能跑」。

真实背景：部署向导里的「Helm」勾选写的是「启用后部署时一并准备 Helm 客户端」，但平台此前
只拿它判过一次制品预检，节点上敲 ``helm`` 还是 ``command not found``（见 ``services/node_helm.py``
开头）。kubeasz 那份「分发二进制」的清单里没有 helm，平台照它的写法补一条。

命令生成是纯函数；远程动作经 ``client_factory`` 注入假客户端。这个假客户端把「节点上那份
helm」放进内存，并真的走一遍探测 → 上传 → 复核，因此判定的不只是命令字符串，还有顺序：
先 ``.part`` 再 ``mv``、装完必须再探一次。
"""

import shutil
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from k8s_deployment.services import node_helm

from .test_node_registry import FakeSFTP, _RecordingContext, _Stream, make_node

# 节点上的 kubeasz 二进制目录（上游默认值，``compiler.DEFAULT_BIN_DIR``）。
BIN_DIR = '/opt/kube/bin'

# ``install`` 的「没传源文件」与「显式传 None」要分开：前者用本类的临时文件，
# 后者才是「控制机上没有那份 helm」。
DEFAULT_SOURCE = object()


class FakeHelmNode:
    """按生成的命令文本在内存里模拟一台节点。

    只认被测代码会用到的那三条命令形状：探测、准备目录、原子替换。认不出来的命令回非零
    退出码 —— 命令改了而这里没跟着改时，用例会以「探测失败」直接失败，而不是静默放过。
    """

    def __init__(self, present=False, version='v3.14.0+g3fc9f4b', size=50331648,
                 probe_code=0, activate_flips=True, sftp=None):
        """初始化。

        参数：`present` 表示机器上是否已有一份 helm；`version` 表示它报出来的版本；
            `size` 表示它的字节数；`probe_code` 表示探测命令的退出码（非零用来模拟读不出来）；
            `activate_flips` 表示 ``mv`` 之后那份文件是否变得可用（假表示传坏了）；
            `sftp` 表示要用的 SFTP 替身，缺省新建一个。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.present = present
        self.version = version
        self.size = size
        self.probe_code = probe_code
        self.activate_flips = activate_flips
        self.commands = []
        self.sftp = sftp if sftp is not None else FakeSFTP()
        self.closed = False
        self.prepared = False

    def _reply(self, code, out='', err=''):
        """拼出 paramiko 形状的返回值。"""
        return None, _Stream(out, code), _Stream(err, code)

    def _probe_text(self):
        """按内存状态拼出探测命令的输出。"""
        if not self.present:
            return f'{node_helm.MISSING}\n'
        return f'{node_helm.PRESENT}\n{node_helm.VERSION_LABEL}:{self.version}\n{self.size}\n'

    def exec_command(self, command, timeout=None):
        """执行一条假命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(None, stdout, stderr)``。
        副作用：可能改动内存里的机器状态。
        """
        self.commands.append(command)
        if command == node_helm.probe_command(BIN_DIR):
            if self.probe_code:
                return self._reply(self.probe_code, '', 'permission denied')
            return self._reply(0, self._probe_text())
        if command == node_helm.prepare_command(BIN_DIR):
            self.prepared = True
            return self._reply(0)
        if command == node_helm.activate_command(BIN_DIR):
            if self.activate_flips:
                self.present = True
            return self._reply(0)
        return self._reply(1, '', f'unexpected command: {command}')

    def open_sftp(self):
        """返回 SFTP 替身。"""
        return self.sftp

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True

    def fired(self, needle):
        """取回所有包含给定片段的命令。"""
        return [item for item in self.commands if needle in item]


class CommandTextTest(SimpleTestCase):
    """命令与回执的文本：这些字符串是节点上真正跑的东西。"""

    def test_binary_path_ignores_a_trailing_slash(self):
        """目录是从 inventory 里读出来的，末尾多一个斜杠不该拼出 ``//helm``。"""
        self.assertEqual(node_helm.binary_path('/opt/kube/bin/'), '/opt/kube/bin/helm')

    def test_probe_looks_for_the_binary_and_runs_it(self):
        """存在性要执行一次 ``version``：传到一半的文件在 ``-x`` 下也是「存在」的。"""
        command = node_helm.probe_command(BIN_DIR)

        self.assertIn(f'-x {BIN_DIR}/helm', command)
        self.assertIn('version --short', command)
        self.assertIn(node_helm.PRESENT, command)
        self.assertIn(node_helm.MISSING, command)

    def test_uploads_land_on_a_part_file_first(self):
        """先 ``.part`` 再 ``mv``：公网链路上断掉时，最终路径上不该出现半截文件。"""
        target = f'{BIN_DIR}/helm'

        self.assertIn(f'{target}{node_helm.PART_SUFFIX}', node_helm.prepare_command(BIN_DIR))
        self.assertEqual(
            node_helm.activate_command(BIN_DIR),
            f'mv -f {target}{node_helm.PART_SUFFIX} {target}',
        )

    def test_local_binary_reads_from_the_kubeasz_bin_directory(self):
        """来源与 kubeasz 取 kubelet、kubectl 的那一处相同（``{{ base_dir }}/bin``）。"""
        directory = Path(tempfile.mkdtemp(prefix='k8s-helm-src-'))
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / 'bin').mkdir()

        self.assertIsNone(node_helm.local_binary(directory))

        (directory / 'bin' / 'helm').write_bytes(b'x')
        self.assertEqual(node_helm.local_binary(directory), directory / 'bin' / 'helm')


class ParseProbeTest(SimpleTestCase):
    """``parse_probe``：把回执里的三个数字读出来。"""

    def test_reads_present_version_and_size(self):
        """装机正常时三样都有。"""
        text = f'{node_helm.PRESENT}\n{node_helm.VERSION_LABEL}:v3.14.0+g3fc9f4b\n50331648\n'

        self.assertEqual(
            node_helm.parse_probe(text),
            {'present': True, 'version': 'v3.14.0+g3fc9f4b', 'bytes': 50331648},
        )

    def test_missing_marker_means_not_installed(self):
        """没有那个标记就是没装 —— 不回退成「大概有」。"""
        report = node_helm.parse_probe(f'{node_helm.MISSING}\n')

        self.assertFalse(report['present'])
        self.assertEqual(report['bytes'], -1)

    def test_garbage_does_not_look_installed(self):
        """命令没跑起来、输出是别的东西时，不能判成「已装好」。"""
        self.assertFalse(node_helm.parse_probe('bash: line 1: helm: not found\n')['present'])
        self.assertFalse(node_helm.parse_probe('')['present'])


class DescribeTest(SimpleTestCase):
    """``describe``：日志里那句「装的是哪个版本、多大」。"""

    def test_uses_the_version_and_size(self):
        """两个数字都读到了就都写出来 —— 操作员自己截断过文件时只有它能看出来。"""
        self.assertEqual(
            node_helm.describe({'version': 'v3.14.0', 'bytes': 123}),
            'v3.14.0，123 字节',
        )

    def test_falls_back_when_the_version_cannot_be_read(self):
        """``helm version`` 失败时回执里是 ``?``，此时不该把问号当版本号报出去。"""
        self.assertEqual(node_helm.describe({'version': '?', 'bytes': -1}), '?')
        self.assertEqual(node_helm.describe({'version': '', 'bytes': -1}), '版本未知')


class InstallNodeTest(SimpleTestCase):
    """``install_node``：探测 → 上传 → 复核这条链路。"""

    def setUp(self):
        """准备一个假的 helm 源文件与一台节点。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里写入一个字节数可控的源文件。
        """
        super().setUp()
        self.directory = Path(tempfile.mkdtemp(prefix='k8s-helm-'))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.source = self.directory / 'helm'
        self.source.write_bytes(b'#!/bin/sh\necho helm\n' * 32)
        self.context = _RecordingContext()
        self.node = make_node('192.168.10.10')

    def install(self, client, local_path=DEFAULT_SOURCE):
        """跑一次 ``install_node``，连接用注入的假客户端。

        参数：`client` 表示假客户端；`local_path` 表示要用的源文件，缺省用本类那个临时文件
            （要测「控制机上没有它」时显式传 ``None``）。
        返回：返回 ``install_node`` 的结果。
        副作用：执行若干远程命令（都在假客户端里）。
        """
        return node_helm.install_node(
            self.context, self.node, BIN_DIR,
            self.source if local_path is DEFAULT_SOURCE else local_path,
            key_path='/tmp/deploy_key',
            client_factory=lambda **_kwargs: client,
        )

    def test_keeps_a_working_copy_untouched(self):
        """机器上已经有一份能用的就一个字都不改：可能是操作员自己选的版本。"""
        client = FakeHelmNode(present=True, version='v3.14.0+g3fc9f4b')

        report = self.install(client)

        self.assertEqual(report['action'], 'kept')
        self.assertEqual(report['version'], 'v3.14.0+g3fc9f4b')
        self.assertEqual(client.sftp.puts, [])
        self.assertFalse(client.prepared)
        self.assertEqual(client.fired('mv -f'), [])
        self.assertIn('保持原样', self.context.text())
        self.assertTrue(client.closed)

    def test_installs_through_a_part_file(self):
        """缺了就装上，且必须落进 ``bin_dir``、带上可执行权限。"""
        client = FakeHelmNode(present=False)

        report = self.install(client)

        self.assertEqual(report['action'], 'installed')
        part = f'{BIN_DIR}/helm{node_helm.PART_SUFFIX}'
        self.assertEqual(client.sftp.puts, [(str(self.source), part)])
        self.assertEqual(client.sftp.modes[part], 0o755)
        self.assertEqual(len(client.fired('mv -f')), 1)
        # 装完要再探一次：``put_file`` 只核对字节数，能跑起来才算真的装好了。
        self.assertEqual(len(client.fired('if [ -x ')), 2)
        self.assertIn('已装好 helm', self.context.text())

    def test_a_binary_that_will_not_run_is_a_failure(self):
        """传完却跑不起来（比如被改写坏了）算失败，不写一条「已装好」把问题盖过去。"""
        client = FakeHelmNode(present=False, activate_flips=False)

        with self.assertRaises(node_helm.NodeHelmError) as caught:
            self.install(client)

        self.assertEqual(caught.exception.error_code, 'node_helm_verify_failed')
        self.assertTrue(client.closed)

    def test_unreadable_probe_is_a_failure(self):
        """连探测都读不出来时必须报错：当成「没装」会去覆盖一份也许好好的文件。"""
        client = FakeHelmNode(present=False, probe_code=1)

        with self.assertRaises(node_helm.NodeHelmError) as caught:
            self.install(client)

        self.assertEqual(caught.exception.error_code, 'node_helm_probe_failed')
        self.assertEqual(client.sftp.puts, [])
        self.assertTrue(client.closed)

    def test_a_truncated_upload_is_a_failure(self):
        """落地大小对不上时要把错误说成 helm 的事，而不是原样抛出镜像仓库那句。"""
        client = FakeHelmNode(present=False, sftp=FakeSFTP(size_override=10))

        with self.assertRaises(node_helm.NodeHelmError) as caught:
            self.install(client)

        self.assertEqual(caught.exception.error_code, 'node_helm_upload_failed')
        self.assertIn('helm', caught.exception.message)

    def test_missing_source_is_reported(self):
        """控制机上没有那份 helm 时不去连机器 —— 这是调用方该先判掉的事。"""
        client = FakeHelmNode(present=False)

        with self.assertRaises(node_helm.NodeHelmError) as caught:
            self.install(client, local_path=None)

        self.assertEqual(caught.exception.error_code, 'node_helm_source_missing')
        self.assertEqual(client.commands, [])
        self.assertFalse(client.closed)
