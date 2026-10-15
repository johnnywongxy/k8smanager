"""网段合法性预检的用例：虚拟网卡必须排除，真重叠必须拦住。

真实背景：集群被清理之后重新部署，预检报出一串「Service 网段 10.254.0.0/16 与节点
114.67.89.90 所在网段 10.254.1.109/32 重叠」，现场看起来是上次没清干净。

两层原因叠在一起：kube-proxy 的 ipvs 模式把 **每个 Service IP 都以 /32 挂在 ``kube-ipvs0``**
上，nodelocaldns 在 ``nodelocaldns`` 上挂 169.254.20.10，Calico 留下 ``cali*``/``tunl0``；
这些网卡的作用域同样是 ``global``，``ip -o -f inet addr show scope global`` 会把它们一并列出来。
于是「节点所在网段」里混进了集群自己实现的地址，拿 Service 网段去比对，必然成立。

所以要守住的是**两侧**：虚拟网卡上的地址不算节点网段（少报），而物理网卡上的真重叠一个都不能
放过（不能因为怕误报就把这一项做成永远通过）。
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from k8s_deployment.services import preflight

# 一台清理过的节点上 ``ip -o -f inet addr show scope global`` 的典型输出：物理网卡之外，还挂着
# 整段 Service 网段的 /32（kube-ipvs0）、nodelocaldns 的链路本地地址、Calico 的 veth 与 docker0。
CLEANED_NODE_ADDRESSES = '\n'.join([
    '2: eth0    inet 192.168.1.94/24 brd 192.168.1.255 scope global eth0',
    '5: kube-ipvs0    inet 10.254.0.1/32 scope global kube-ipvs0',
    '5: kube-ipvs0    inet 10.254.104.236/32 scope global kube-ipvs0',
    '5: kube-ipvs0    inet 10.254.230.141/32 scope global kube-ipvs0',
    '6: nodelocaldns    inet 169.254.20.10/32 scope global nodelocaldns',
    '9: cali1234abcd    inet 10.244.1.1/32 scope global cali1234abcd',
    '12: tunl0    inet 192.168.1.94/32 scope global tunl0',
    '14: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0',
])

# 物理网卡真的落在 Service 网段里 —— 这个才是必须拦下的那种重叠。
NODE_INSIDE_SERVICE_CIDR = '\n'.join([
    '2: eth0    inet 10.254.1.109/32 scope global eth0',
])


class FakeSession:
    """假节点会话：把 ``ip`` 的输出原样喂回去，并记录收到的命令。"""

    def __init__(self, ip, output='', ok=True):
        """初始化。

        参数：`ip` 表示节点地址；`output` 表示命令的标准输出；`ok` 表示命令是否成功。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.ip = ip
        self.output = output
        self.ok = ok
        self.commands = []

    def run(self, command, timeout=None):
        """记录并「执行」一条命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(ok, 输出, '')``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        return self.ok, self.output, ''


def make_deployment(pod_cidr='10.244.0.0/16', service_cidr='10.68.0.0/16', api_server_port=6443):
    """构造一个只带网段字段的部署对象。

    参数：`pod_cidr`/`service_cidr`/`api_server_port` 表示待校验的三个取值。
    返回：返回一个属性替身。
    副作用：只在本机对象上设置字段。

    ``_check_cidr`` 只读这三个字段，用替身比造一条真的部署记录更贴近它实际做的事。
    """
    return SimpleNamespace(
        pod_cidr=pod_cidr, service_cidr=service_cidr, api_server_port=api_server_port,
    )


def run_check(sessions, pod_cidr='10.244.0.0/16', service_cidr='10.68.0.0/16', reachable=None):
    """跑一次网段合法性检查。

    参数：`sessions` 表示 ``{ip: 会话}``；`pod_cidr`/`service_cidr` 表示待校验的网段；
        `reachable` 表示可达节点地址，默认取 ``sessions`` 的全部键。
    返回：返回检查项字典。
    副作用：调用被测函数。
    """
    if reachable is None:
        reachable = list(sessions)
    return preflight._check_cidr(
        make_deployment(pod_cidr=pod_cidr, service_cidr=service_cidr), sessions, reachable,
    )


class NodePrefixesTest(SimpleTestCase):
    """``_node_prefixes``：读接口网段时排掉哪些。"""

    def test_cluster_interfaces_are_skipped_and_named(self):
        """集群自己建的接口一律不算节点网段，名字回报给调用方展示。"""
        ignored = []
        prefixes = preflight._node_prefixes(
            FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES), ignored=ignored,
        )
        # strict=False 归一成网络号：192.168.1.94/24 → 192.168.1.0/24。
        self.assertEqual([str(item) for item in prefixes], ['192.168.1.0/24'])
        self.assertEqual(
            ignored,
            ['kube-ipvs0', 'nodelocaldns', 'cali1234abcd', 'tunl0', 'docker0'],
        )

    def test_each_interface_is_named_once(self):
        """同一张网卡挂了多少个地址只报一次名 —— kube-ipvs0 上可能挂着几百个 /32。"""
        ignored = []
        preflight._node_prefixes(FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES), ignored=ignored)
        self.assertEqual(ignored.count('kube-ipvs0'), 1)

    def test_physical_interfaces_are_all_kept(self):
        """物理网卡上有几个地址就返回几个网段，不合并也不省略。"""
        output = '\n'.join([
            '2: eth0    inet 192.168.1.94/24 scope global eth0',
            '3: eth1    inet 10.10.0.5/24 scope global eth1',
        ])
        prefixes = preflight._node_prefixes(FakeSession('192.168.1.94', output))
        self.assertEqual(sorted(str(item) for item in prefixes), ['10.10.0.0/24', '192.168.1.0/24'])

    def test_ignored_list_is_optional(self):
        """不给 ``ignored`` 时也要照常工作（其它调用点不关心被排除了哪些）。"""
        prefixes = preflight._node_prefixes(FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES))
        self.assertEqual([str(item) for item in prefixes], ['192.168.1.0/24'])

    def test_unreadable_command_yields_no_prefix(self):
        """命令失败返回空列表，由调用方落回按 /24 估算，而不是抛异常。"""
        session = FakeSession('192.168.1.94', ok=False)
        self.assertEqual(preflight._node_prefixes(session), [])

    def test_unparsable_line_does_not_drop_the_others(self):
        """单行网段解析失败只跳过那一行，其余照常返回。"""
        output = '\n'.join([
            '2: eth0    inet 192.168.1.94/24 scope global eth0',
            '3: eth1    inet 999.1.1.1/24 scope global eth1',
        ])
        prefixes = preflight._node_prefixes(FakeSession('192.168.1.94', output))
        self.assertEqual([str(item) for item in prefixes], ['192.168.1.0/24'])

    def test_this_list_is_broader_than_the_one_node_network_deletes(self):
        """这份名单与清理用的那份刻意不同源，方向是「预检排除得多、清理删得少」。

        预检把 ``docker0`` 也排除掉（它带着 172.17.0.1，绝不该被算成节点所在网段），清理却绝
        不能删它 —— 机器上可能跑着与 k8s 无关的 docker。合成一份之后，往这边补一个接口名就会
        连带在真机上删掉它。
        """
        from .test_node_network import pattern_matches

        ignored = []
        preflight._node_prefixes(FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES), ignored=ignored)

        # 两边都对 kube-ipvs0 这类集群接口有说法，但对 docker0 的结论不同 —— 这就是两份名单
        # 不能合并的证据。
        self.assertIn('kube-ipvs0', ignored)
        self.assertIn('docker0', ignored)
        self.assertFalse(pattern_matches('docker0'))
        self.assertTrue(pattern_matches('kube-ipvs0'))


class CheckCidrTest(SimpleTestCase):
    """``_check_cidr``：通过还是硬失败。"""

    def test_virtual_interfaces_do_not_fail_the_check(self):
        """需求 1 的正题：清理后残留的虚拟网卡不该让预检硬失败。

        Service 网段 10.254.0.0/16 与节点物理网卡 192.168.1.94/24 毫无关系，让这一项失败的是
        ``kube-ipvs0`` 上那一串 10.254.x.x/32 —— 那是集群自己实现的地址。
        """
        sessions = {'192.168.1.94': FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES)}
        result = run_check(sessions, service_cidr='10.254.0.0/16')

        self.assertEqual(result['status'], 'passed')
        self.assertIn('无重叠', result['message'])
        # 通过的原因要写在页面上，否则「上次报重叠、这次不报」会被当成时好时坏。
        self.assertIn('已忽略集群虚拟网卡', result['message'])
        self.assertIn('kube-ipvs0', result['message'])
        self.assertEqual(
            result['detail']['ignored_virtual_interfaces'],
            ['cali1234abcd', 'docker0', 'kube-ipvs0', 'nodelocaldns', 'tunl0'],
        )
        self.assertEqual(result['detail']['node_networks'], {'192.168.1.94': ['192.168.1.0/24']})

    def test_real_overlap_on_a_physical_interface_still_fails(self):
        """反过来：节点物理网卡真的落在 Service 网段里，必须拦住。

        排除虚拟网卡不等于把这一项做成永远通过 —— 一个跑在 Service 网段里的节点，
        容器网段与主机网段会正面打架。
        """
        sessions = {'10.254.1.109': FakeSession('10.254.1.109', NODE_INSIDE_SERVICE_CIDR)}
        result = run_check(sessions, service_cidr='10.254.0.0/16')

        self.assertEqual(result['status'], 'failed')
        self.assertIn('Service 网段 10.254.0.0/16 与节点 10.254.1.109 所在网段 10.254.1.109/32 重叠',
                      result['message'])
        self.assertNotIn('ignored_virtual_interfaces', result['detail'])

    def test_overlap_is_judged_per_node(self):
        """多节点时按台判定：一台干净、一台重叠，结论仍然是失败，且点名的是那一台。"""
        sessions = {
            '192.168.1.94': FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES),
            '10.254.1.109': FakeSession('10.254.1.109', NODE_INSIDE_SERVICE_CIDR),
        }
        result = run_check(sessions, service_cidr='10.254.0.0/16')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('10.254.1.109', result['message'])
        self.assertNotIn('192.168.1.94 所在网段', result['message'])

    def test_pod_and_service_overlap_is_still_refused(self):
        """pod 与 service 两段互不重叠这一条不受影响。"""
        sessions = {'192.168.1.94': FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES)}
        result = run_check(sessions, pod_cidr='10.244.0.0/16', service_cidr='10.244.0.0/16')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('重叠', result['message'])

    def test_unreadable_interfaces_fall_back_to_a_slash_24_estimate(self):
        """读不到接口时按 /24 估算，并在明细里注明 —— 不能让这一项悄悄变成「一定通过」。"""
        sessions = {'192.168.1.94': FakeSession('192.168.1.94', ok=False)}
        result = run_check(sessions, service_cidr='192.168.1.0/24')

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['detail']['estimated_node_networks'], ['192.168.1.94'])
        self.assertEqual(result['detail']['node_networks'], {'192.168.1.94': ['192.168.1.0/24']})

    def test_no_reachable_node_says_so_instead_of_passing_silently(self):
        """一台都连不上时写明「仅校验了 CIDR 自身」，不假装查过节点网段。"""
        result = run_check({}, reachable=[])
        self.assertEqual(result['status'], 'passed')
        self.assertIn('未能获取节点所在网段', result['detail']['node_network_note'])

    def test_invalid_cidr_and_port_are_reported_together(self):
        """几处错误一次全报出来，而不是改一个再提交一次又冒出下一个。"""
        sessions = {'192.168.1.94': FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES)}
        result = run_check(sessions, pod_cidr='10.244.0.0/33', service_cidr='not-a-cidr')

        self.assertEqual(result['status'], 'failed')
        self.assertIn('容器网络 CIDR「10.244.0.0/33」不是合法的网段', result['message'])
        self.assertIn('Service IP 段「not-a-cidr」不是合法的网段', result['message'])

    def test_out_of_range_port_is_refused(self):
        """端口越界是这一项的一部分，报错要写成中文并带上原值。"""
        sessions = {'192.168.1.94': FakeSession('192.168.1.94', CLEANED_NODE_ADDRESSES)}
        result = preflight._check_cidr(
            make_deployment(api_server_port=70000), sessions, ['192.168.1.94'],
        )
        self.assertEqual(result['status'], 'failed')
        self.assertIn('ApiServer 端口 70000 超出 1-65535 范围', result['message'])
