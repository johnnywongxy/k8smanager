"""清理节点虚拟网卡的用例：命令形状、回执解析与逐台告警。

真实背景：kubeasz 的清理剧本清组件与数据，但**不删** kube-proxy 的 ``kube-ipvs0``（上面挂着
每个 Service IP 的 /32）、nodelocaldns 的 ``nodelocaldns``、Calico 的 ``cali*`` —— 它只在最后
提示一句「请重启节点」。这几张网卡留着，下次部署的「网段合法性」预检就会拿 Service 网段跟
它自己比，报出一串必然成立的重叠而硬失败（见 ``services/node_network.py`` 开头）。

命令是纯函数，远程动作全部经 ``client_factory`` 注入假客户端。这里**不**断言整条命令的全文：
那样上游随便加一个分号都会让用例失配，而它真正要守住的是两件事 —— 删的是不是只有集群自己建的
那几类接口，以及回执解析在各种残缺输出下会不会误报。
"""

import fnmatch
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from k8s_deployment.services import credentials, node_network

from .test_node_registry import _Stream, make_node


def pattern_matches(interface):
    """按清理命令里的接口名模式判断一个接口该不该被删。

    参数：`interface` 表示接口名。
    返回：返回布尔值。
    副作用：只做纯计算。

    命令里用的是 shell ``case`` 的模式表，语义与 glob 一致（含 ``[0-9]`` 区间），
    所以直接拿 ``fnmatch`` 逐段比对 —— 比起在用例里另抄一份正则，这样测的是**真的那条命令里
    写的模式**，改了模式就会立刻反映到断言上。
    """
    for pattern in node_network.REMOVABLE_INTERFACE_PATTERNS.split('|'):
        if fnmatch.fnmatchcase(interface, pattern):
            return True
    return False


class CleanupCommandTest(SimpleTestCase):
    """``cleanup_command``：删哪些接口、按什么顺序、失败怎么办。"""

    def test_ipvs_table_is_flushed_before_interfaces_are_removed(self):
        """先清 ipvs 转发表再删网卡 —— 反过来的话中途会有一批服务指着不存在的设备。"""
        command = node_network.cleanup_command()
        self.assertIn('ipvsadm -C', command)
        self.assertLess(command.index('ipvsadm -C'), command.index('ip link del'))

    def test_ipvsadm_is_probed_before_use(self):
        """没装 ipvsadm（iptables 模式或已经清过）要直接跳过，而不是报错中断。"""
        self.assertIn('command -v ipvsadm', node_network.cleanup_command())

    def test_removes_cluster_interfaces(self):
        """集群自己建的这几类接口是本次清理的对象。"""
        for name in ('kube-ipvs0', 'nodelocaldns', 'cali1234abcd', 'tunl0', 'cni0', 'flannel.1'):
            self.assertTrue(pattern_matches(name), name)

    def test_leaves_foreign_interfaces_alone(self):
        """机器上其它用途的接口一张都不碰。

        docker0 与 ``br-*`` 尤其重要：目标机可能同时跑着与 k8s 无关的 docker，删掉它们的网桥
        会把那部分容器网络连根拔起。``virbr*`` 是 libvirt 的 NAT 网桥，同理。
        """
        for name in ('eth0', 'ens192', 'docker0', 'br-1a2b3c', 'virbr0', 'lo'):
            self.assertFalse(pattern_matches(name), name)

    def test_pattern_does_not_carry_iptables_commands(self):
        """刻意不刷 iptables：在一台云主机上按前缀盲刷会连放行 SSH 的规则一起刷掉。"""
        command = node_network.cleanup_command()
        self.assertNotIn('iptables', command)
        self.assertNotIn('ipset', command)

    def test_command_survives_partial_states(self):
        """删除动作是「失败就继续」：集群装到一半、上次清理崩在中间时也要跑到最后。

        只有 ipvsadm 那一步用 ``&&``（``command -v`` 成功才执行），删除循环里的每一次
        ``ip link del`` 都是独立的一条 —— 某张网卡删不掉不该让后面的网卡都留着。
        """
        command = node_network.cleanup_command()
        # 删除循环的每一步都是独立的一条：某张网卡删不掉，不该让后面的网卡都留着。
        self.assertIn('ip link del "$i" 2>/dev/null && REMOVED=$((REMOVED+1));;', command)
        self.assertIn('esac; done;', command)

    def test_command_echoes_a_verdict(self):
        """末尾必须打出回执，调用方据此判断还有没有残留，不必再开一次连接。"""
        command = node_network.cleanup_command()
        self.assertIn(f'echo {node_network.CLEANUP_OK} ||', command)
        self.assertIn(node_network.CLEANUP_PARTIAL, command)
        self.assertIn('echo "REMOVED:', command)
        self.assertIn('echo "LEFT:', command)


class ParseResultTest(SimpleTestCase):
    """``parse_result``：命令回执的解析，以及解析不出来时怎么降级。"""

    def test_clean_node(self):
        """干净的节点：一张都没删，也没有残留。"""
        result = node_network.parse_result(
            f'REMOVED:0\nLEFT:\n{node_network.CLEANUP_OK}'
        )
        self.assertEqual(result, {'removed': 0, 'left': [], 'clean': True})

    def test_reports_removed_count_and_leftovers(self):
        """有残留时要把名字带回来 —— 操作员看到名字就知道该去查什么。"""
        result = node_network.parse_result(
            'REMOVED:2\nLEFT: cali0 kube-ipvs0\n' + node_network.CLEANUP_PARTIAL
        )
        self.assertEqual(result['removed'], 2)
        self.assertEqual(result['left'], ['cali0', 'kube-ipvs0'])
        self.assertFalse(result['clean'])

    def test_partial_is_judged_by_the_marker_not_the_left_line(self):
        """判据是回执标记：``LEFT:`` 那行缺失时也不能当成清理干净。"""
        result = node_network.parse_result(f'REMOVED:3\n{node_network.CLEANUP_PARTIAL}')
        self.assertFalse(result['clean'])
        self.assertEqual(result['removed'], 3)

    def test_empty_output_falls_back_to_a_harmless_default(self):
        """输出为空时落回「什么都没删、也没有残留」，而不是抛异常。"""
        for text in ('', None):
            result = node_network.parse_result(text)
            self.assertEqual(result['removed'], 0)
            self.assertEqual(result['left'], [])
            # 没有残留标记时按「干净」处理：调用方据此走 warn 分支，但不会误判成失败。
            self.assertTrue(result['clean'])

    def test_unparsable_count_does_not_break_the_leftover_report(self):
        """计数行坏掉只影响那句日志，残留清单仍然要读出来 —— 后者才是操作员要处理的东西。"""
        result = node_network.parse_result(
            'REMOVED:abc\nLEFT: cali0\n' + node_network.CLEANUP_PARTIAL
        )
        self.assertEqual(result['removed'], 0)
        self.assertEqual(result['left'], ['cali0'])
        self.assertFalse(result['clean'])

    def test_surrounding_whitespace_is_tolerated(self):
        """每行都 strip：ansible 与 ssh 都可能带上多余空白。"""
        result = node_network.parse_result(
            f'  REMOVED: 1  \n  LEFT:   \n  {node_network.CLEANUP_OK}  '
        )
        self.assertEqual(result['removed'], 1)
        self.assertEqual(result['left'], [])


class FakeSSHClient:
    """假的 SSH 客户端：固定回一段输出与退出码，并记下收到的命令。"""

    def __init__(self, code=0, out='', err=''):
        """初始化。

        参数：`code` 表示退出码；`out` 表示标准输出；`err` 表示标准错误。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.code = code
        self.out = out
        self.err = err
        self.commands = []
        self.closed = False

    def exec_command(self, command, timeout=None):
        """记录并「执行」一条命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回与 paramiko 同形的 ``(stdin, stdout, stderr)``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        return None, _Stream(self.out, self.code), _Stream(self.err, self.code)

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True


class RecordingContext:
    """够用的上下文替身：收集日志与事件，不做任何持久化。"""

    def __init__(self):
        """初始化。"""
        self.logs = []
        self.events = []

    def log(self, stream, text):
        """收集日志。"""
        self.logs.append(text)

    def event(self, event_type, message, level='info', detail=None, **kwargs):
        """收集事件。"""
        self.events.append({'type': event_type, 'message': message, 'level': level, 'detail': detail})

    def text(self):
        """把收集到的日志拼成一段文本，便于断言包含关系。"""
        return '\n'.join(self.logs)


class CleanupTest(SimpleTestCase):
    """``cleanup``：逐台清理的结果与告警。"""

    def setUp(self):
        """准备上下文与节点。"""
        self.context = RecordingContext()
        self.node = make_node('192.168.1.11', node_id=1, roles=['master'])
        self.factory = mock.Mock()

    def run_cleanup(self, code=0, out='', err=''):
        """用假客户端跑一次清理。

        参数：`code` 表示退出码；`out` 表示标准输出；`err` 表示标准错误。
        返回：返回 ``(结果, 假客户端)``。
        副作用：调用被测函数。
        """
        client = FakeSSHClient(code=code, out=out, err=err)
        self.factory.return_value = client
        result = node_network.cleanup(
            self.context, self.node, key_path='/tmp/fake-platform-key', client_factory=self.factory,
        )
        return result, client

    def test_connection_is_closed_even_when_the_command_fails(self):
        """连上就要收尾：失败路径上漏掉 close，清理一批机器会把连接数耗光。"""
        with self.assertRaises(node_network.NodeNetworkError):
            self.run_cleanup(code=1, err='permission denied')
        self.assertTrue(self.factory.return_value.closed)

    def test_failure_carries_a_readable_reason(self):
        """非零退出码要带中文原因，调用方会把它写进任务日志。"""
        with self.assertRaises(node_network.NodeNetworkError) as caught:
            self.run_cleanup(code=255, err='ssh: connect timed out')
        self.assertEqual(caught.exception.error_code, 'node_network_cleanup_failed')
        self.assertIn('192.168.1.11', caught.exception.message)
        self.assertIn('ssh: connect timed out', caught.exception.message)

    def test_clean_node_is_reported_without_an_event(self):
        """没有虚拟网卡是常态（尤其是刚装完还没跑过 Service 的机器），不该产生事件。"""
        result, client = self.run_cleanup(out=f'REMOVED:0\nLEFT:\n{node_network.CLEANUP_OK}')
        self.assertEqual(result['removed'], 0)
        self.assertEqual(self.context.events, [])
        self.assertIn('没有发现集群遗留的虚拟网卡', self.context.text())
        self.assertIn('for i in $(ls /sys/class/net', client.commands[0])

    def test_removed_interfaces_are_counted_in_the_log(self):
        """删了几张要写进日志 —— 这是操作员核对「是不是真清了」的唯一凭据。"""
        self.run_cleanup(out=f'REMOVED:3\nLEFT:\n{node_network.CLEANUP_OK}')
        self.assertIn('已删除 3 张集群虚拟网卡', self.context.text())
        self.assertEqual(self.context.events, [])

    def test_leftovers_raise_a_warning_event_naming_the_interfaces(self):
        """清不掉的接口要点名告警，但不判失败：残留不影响重新部署。"""
        result, _client = self.run_cleanup(
            out='REMOVED:1\nLEFT: cali0 kube-ipvs0\n' + node_network.CLEANUP_PARTIAL
        )
        self.assertEqual(result['left'], ['cali0', 'kube-ipvs0'])
        self.assertEqual(len(self.context.events), 1)
        event = self.context.events[0]
        self.assertEqual(event['type'], 'node_network_leftover')
        self.assertEqual(event['level'], 'warn')
        self.assertEqual(event['detail']['interfaces'], ['cali0', 'kube-ipvs0'])
        self.assertIn('ip link del', self.context.text())

    def test_credential_overrides_the_node_snapshot(self):
        """凭据里的连接信息优先：节点快照上的端口可能已经过时。"""
        self.node.ssh_port = 22
        client = FakeSSHClient(out=f'REMOVED:0\nLEFT:\n{node_network.CLEANUP_OK}')
        self.factory.return_value = client
        node_network.cleanup(
            self.context, self.node, credential={'user': 'ops', 'port': 2222},
            key_path='/tmp/fake-platform-key', client_factory=self.factory,
        )
        self.assertEqual(self.factory.call_args.kwargs['username'], 'ops')
        self.assertEqual(self.factory.call_args.kwargs['port'], 2222)
        self.assertEqual(self.factory.call_args.kwargs['key_path'], '/tmp/fake-platform-key')

    def test_command_timeout_is_passed_through(self):
        """超时按模块常量传下去，不能落成 credentials 的默认连接超时。"""
        with mock.patch.object(credentials, 'run_command', return_value=(0, '', '')) as runner:
            node_network.cleanup(
                self.context, self.node, key_path='/tmp/fake-platform-key',
                client_factory=self.factory,
            )
        self.assertEqual(runner.call_args.kwargs['timeout'], node_network.COMMAND_TIMEOUT)

    def test_node_without_ip_uses_the_hostname_in_the_log(self):
        """``label`` 的取值链：IP 为空时退回主机名。"""
        node = SimpleNamespace(ip_address='', name='k8s-node-1', ssh_port=22, ssh_user='root')
        client = FakeSSHClient(out=f'REMOVED:0\nLEFT:\n{node_network.CLEANUP_OK}')
        self.factory.return_value = client
        node_network.cleanup(
            self.context, node, key_path='/tmp/fake-platform-key', client_factory=self.factory,
        )
        self.assertIn('k8s-node-1', self.context.text())


class ModuleBoundaryTest(SimpleTestCase):
    """这个模块的边界：只删网卡，不碰别的。"""

    def test_does_not_delete_docker_or_bridge_interfaces(self):
        """``docker0`` / ``br-*`` 明确不在模式表里（见 ``REMOVABLE_INTERFACE_PATTERNS`` 注释）。

        这不是一份「读取时忽略」的名单：``preflight`` 那份可以多排除，这边多写一个名字就是
        真的会去删人家的网桥。两份名单的对照在 ``test_preflight_cidr.py`` 里。
        """
        self.assertNotIn('docker', node_network.REMOVABLE_INTERFACE_PATTERNS)
        self.assertNotIn('br-', node_network.REMOVABLE_INTERFACE_PATTERNS)
        self.assertNotIn('virbr', node_network.REMOVABLE_INTERFACE_PATTERNS)
