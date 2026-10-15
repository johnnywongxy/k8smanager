"""节点地址探测的用例：解析、选择规则与失败时的降级。

这三件事都不需要真的连机器：``resolve`` 允许注入探测函数，``parse_probe`` 与
``choose_cluster_address`` 是纯函数。真实 SSH 通道由 ``credentials`` 的用例守着。

真实背景：节点在 NAT 后面，操作员填的是公网地址（平台据此 SSH 进去），网卡上只有内网
地址。kubeasz 的全部 ``--listen-*`` 都取 ``{{ inventory_hostname }}``，于是 etcd 报
``listen tcp 116.198.251.203:2380: bind: cannot assign requested address``。
"""

import socket

from django.test import SimpleTestCase

from k8s_deployment.models import K8sDeploymentNode
from k8s_deployment.services import node_address

PROBE_OUTPUT = (
    'ROUTE 172.30.0.7\n'
    'ADDRS 172.30.0.7 10.0.0.5 \n'
)


def make_node(ip_address, ssh_port=22, ssh_user='root'):
    """构造一个未落库的节点快照。

    参数：`ip_address` 表示操作员填写的地址；`ssh_port` 表示 SSH 端口；`ssh_user` 表示登录用户名。
    返回：返回未保存的 K8sDeploymentNode 实例。
    副作用：只在本机对象上设置字段，不写数据库。
    """
    return K8sDeploymentNode(
        name=ip_address, ip_address=ip_address, ssh_port=ssh_port, ssh_user=ssh_user,
    )


class ParseProbeTest(SimpleTestCase):
    """``parse_probe``：把探测命令的两行输出拆成出口地址与本机地址表。"""

    def test_reads_both_lines(self):
        """ROUTE 是出口地址，ADDRS 是本机全部全局 IPv4。"""
        route, addresses = node_address.parse_probe(PROBE_OUTPUT)
        self.assertEqual(route, '172.30.0.7')
        self.assertEqual(addresses, ['172.30.0.7', '10.0.0.5'])

    def test_empty_output_yields_nothing(self):
        """命令没输出（比如目标机上没有 ip 命令）时两项都为空，不抛异常。"""
        self.assertEqual(node_address.parse_probe(''), ('', []))
        self.assertEqual(node_address.parse_probe(None), ('', []))

    def test_non_ip_values_are_dropped(self):
        """输出里混进非地址的文本时当作没探测到：它会被写进 inventory，不能碰运气。"""
        route, addresses = node_address.parse_probe('ROUTE 未探测到\nADDRS 172.30.0.7 v6::1 算了\n')
        self.assertEqual(route, '')
        self.assertEqual(addresses, ['172.30.0.7'])


class ChooseClusterAddressTest(SimpleTestCase):
    """``choose_cluster_address``：填写的地址与网卡地址之间的取舍。"""

    def test_entered_address_on_the_interface_wins(self):
        """填的就是网卡上的地址（内网直连）时按填写值来：那是操作员明确的选择。"""
        self.assertEqual(
            node_address.choose_cluster_address('172.30.0.7', '172.30.0.7', ['172.30.0.7', '10.0.0.5']),
            '172.30.0.7',
        )

    def test_nat_address_is_replaced_by_the_interface_address(self):
        """填的是 NAT 后的公网地址时换成出口网卡地址，etcd 才有地址可绑。"""
        self.assertEqual(
            node_address.choose_cluster_address('116.198.251.203', '172.30.0.7', ['172.30.0.7']),
            '172.30.0.7',
        )

    def test_falls_back_to_the_interface_list(self):
        """没有默认路由（``ip route get`` 失败）时用地址表里的第一个。"""
        self.assertEqual(
            node_address.choose_cluster_address('116.198.251.203', '', ['172.30.0.7', '10.0.0.5']),
            '172.30.0.7',
        )

    def test_nothing_detected_yields_empty(self):
        """什么都没探测到就返回空串，由调用方决定退回填写的地址。"""
        self.assertEqual(node_address.choose_cluster_address('116.198.251.203', '', []), '')
        self.assertEqual(node_address.choose_cluster_address('', '', []), '')


class ResolveTest(SimpleTestCase):
    """``resolve``：探测失败必须降级，不能把部署拦下来。"""

    def resolve(self, probe, entered='116.198.251.203', credential=None):
        """用注入的探测函数跑一次 resolve。

        参数：`probe` 表示 ``f(host, port, username) -> 输出文本``；`entered` 表示填写的地址；
            `credential` 表示连接信息。
        返回：返回被测函数的返回值。
        副作用：只做纯计算。
        """
        return node_address.resolve(make_node(entered), credential or {}, probe=probe)

    def test_nat_node_reports_probe_source_with_both_addresses(self):
        """探到的是别的地址时记下它，并把探测到的地址表一并交出来，便于写日志。"""
        outcome = self.resolve(lambda *_: PROBE_OUTPUT)
        self.assertEqual(outcome['address'], '172.30.0.7')
        self.assertEqual(outcome['source'], node_address.SOURCE_PROBE)
        self.assertEqual(outcome['route_source'], '172.30.0.7')

    def test_entered_address_kept_when_it_is_on_the_interface(self):
        """填写的地址就在网卡上时来源是 entered，调用方据此不额外记日志。"""
        outcome = self.resolve(lambda *_: 'ROUTE 172.30.0.7\nADDRS 116.198.251.203\n', entered='172.30.0.7')
        self.assertEqual(outcome['address'], '172.30.0.7')
        self.assertEqual(outcome['source'], node_address.SOURCE_ENTERED)

    def test_probe_failure_falls_back_to_the_entered_address(self):
        """探测抛异常（连不上、没有 ip 命令）时退回填写的地址，来源标成 fallback。"""
        def boom(*_args):
            raise RuntimeError('ssh 断了')

        outcome = self.resolve(boom)
        self.assertEqual(outcome['address'], '116.198.251.203')
        self.assertEqual(outcome['source'], node_address.SOURCE_FALLBACK)

    def test_empty_output_falls_back(self):
        """命令跑通但什么都没输出，与探测失败同等处理。"""
        outcome = self.resolve(lambda *_: '')
        self.assertEqual(outcome['address'], '116.198.251.203')
        self.assertEqual(outcome['source'], node_address.SOURCE_FALLBACK)

    def test_credential_overrides_port_and_user(self):
        """探测用的端口与用户名取自凭据表，与平台连节点用的是同一套参数。"""
        seen = {}

        def probe(host, port, username):
            seen.update({'host': host, 'port': port, 'username': username})
            return PROBE_OUTPUT

        self.resolve(probe, credential={'port': 2222, 'user': 'ops'})
        self.assertEqual(seen, {'host': '116.198.251.203', 'port': 2222, 'username': 'ops'})

    def test_node_snapshot_supplies_default_port_and_user(self):
        """凭据表里没有这一台时用节点快照上的 SSH 参数兜底。"""
        seen = {}

        def probe(host, port, username):
            seen.update({'port': port, 'username': username})
            return PROBE_OUTPUT

        node_address.resolve(make_node('116.198.251.203', ssh_port=2200, ssh_user='deploy'), {}, probe=probe)
        self.assertEqual(seen, {'port': 2200, 'username': 'deploy'})


class ProbeCommandTest(SimpleTestCase):
    """探测命令本身的约定：输出前缀必须与解析对齐。"""

    def test_output_prefixes_match_the_parser(self):
        """命令里 echo 的两个前缀就是解析器认的两个关键字，改了一头另一头就会静默失效。"""
        self.assertIn('echo "ROUTE $(', node_address.PROBE_COMMAND)
        self.assertIn('echo "ADDRS $(', node_address.PROBE_COMMAND)

    def test_awk_field_references_are_not_used(self):
        """命令整体包在双引号里，``awk '{print $4}'`` 那种写法会被 shell 提前展开成空串。"""
        self.assertNotIn('awk', node_address.PROBE_COMMAND)


class PlatformReachesTest(SimpleTestCase):
    """``platform_reaches``：平台机与某个地址通不通。"""

    def test_open_port_is_reachable(self):
        """有东西在监听时连得上。"""
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        self.assertTrue(node_address.platform_reaches('127.0.0.1', server.getsockname()[1], timeout=2))

    def test_closed_port_is_not_reachable(self):
        """没人监听时连不上：这条提示要在部署早期就告诉操作员，别等健康检查才发现。"""
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(node_address.platform_reaches('127.0.0.1', port, timeout=2))

    def test_unroutable_address_is_not_reachable(self):
        """地址本身不可达时也要给出 False，而不是抛异常。"""
        self.assertFalse(node_address.platform_reaches('203.0.113.7', 22, timeout=1))
