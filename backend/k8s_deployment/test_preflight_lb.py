"""外部负载均衡预检的用例：VIP 装得上吗。

预检这一项回答的是「这个 VIP 装得上吗」，**不是**「VIP 现在通不通」—— 部署前 VIP 还不存在，
从节点去连它必然失败。所以这里不探端口、不 ping，只看三件事：VIP 本身合法且没被占用、
每台负载均衡机器上都有能落下它的网段、两台机器在同一个二层网段里。

要守住的边界有三个方向：真装不上的（VIP 已被占用、不在机器网段内、机器不够）必须判失败，
不能因为「反正是部署前」就一路放行；运维任务（扩容/升级/清理）从集群继承了 VIP 却没有
负载均衡机器，必须跳过而不是挡死；而「VIP 已被占用」这一条要能认出**本平台上一次自己挂上去
的那个 VIP**（第一次部署没跑完时它就在机器上了，94 上的 k8s-cluster 重试正是卡在这里），
认出来降级成警告，认不出来照旧硬失败。
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from k8s_deployment.services import preflight


class FakeSession:
    """假节点会话：把 ``ip`` 的输出原样喂回去，并记录收到的命令。

    ``outputs`` 用来按命令文本给不同的输出（认领探针要的就是这个）：命中就返回它，没命中
    退回 ``output``。
    """

    def __init__(self, ip, output='', ok=True, roles=('node',), outputs=None):
        """初始化。

        参数：`ip` 表示节点地址；`output` 表示默认的命令标准输出；`ok` 表示命令是否成功；
            `roles` 表示该节点的角色；`outputs` 表示按命令文本给出的输出映射。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.ip = ip
        self.output = output
        self.ok = ok
        self.outputs = dict(outputs or {})
        self.commands = []
        self.node = SimpleNamespace(roles=list(roles), ip_address=ip, cluster_ip=None)

    def run(self, command, timeout=None):
        """记录并「执行」一条命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(ok, 输出, '')``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        return self.ok, self.outputs.get(command, self.output), ''


def self_install(units=(), keepalived_conf=''):
    """按认领探针的命令文本包一份 ``FakeSession.outputs``。

    参数：`units` 表示 ``(单元名, 状态)`` 形式的二元组序列；`keepalived_conf` 表示配置全文。
    返回：返回 ``{命令文本: 输出}``。
    副作用：纯计算。
    """
    lines = [preflight.SELF_INSTALL_PROBE_BEGIN]
    lines.extend(f'UNIT:{name}:{state}' for name, state in units)
    lines.append(preflight.KEEPALIVED_CONF_BEGIN)
    lines.extend(str(keepalived_conf).splitlines())
    lines.append(preflight.KEEPALIVED_CONF_END)
    return {preflight._self_install_probe_command(): '\n'.join(lines)}


def keepalived_conf(vip='192.168.1.250'):
    """拼一份 kubeasz ex-lb 渲染出来的 keepalived 配置。

    参数：`vip` 表示配置里声明的虚拟地址。
    返回：返回配置全文。
    副作用：纯计算。
    """
    return '\n'.join([
        'global_defs {',
        '    router_id lb-192.168.1.6',
        '}',
        'vrrp_instance VI-01 {',
        '    state BACKUP',
        '    interface eth0',
        '    virtual_ipaddress {',
        f'        {vip}',
        '    }',
        '}',
    ])


def addresses(*lines):
    """拼出一台机器上 ``ip -o -f inet addr show scope global`` 的输出。

    参数：`lines` 表示 ``(接口名, 地址/前缀)`` 形式的二元组。
    返回：返回命令输出文本。
    副作用：纯计算。
    """
    return '\n'.join(
        f'2: {name}    inet {cidr} brd 0.0.0.0 scope global {name}' for name, cidr in lines
    )


LB_MACHINE = addresses(('eth0', '192.168.1.6/24'), ('lo', '127.0.0.1/8'))
LB_PEER = addresses(('eth0', '192.168.1.7/24'))
MASTER = addresses(('eth0', '192.168.1.10/24'))


def make_deployment(**fields):
    """构造一个只带预检所需字段的部署对象。

    参数：`fields` 表示要覆盖的字段。
    返回：返回一个属性替身。
    副作用：只在本机对象上设置字段。
    """
    values = {
        'lb_mode': 'self',
        'lb_vip': '192.168.1.250',
        'pod_cidr': '10.244.0.0/16',
        'service_cidr': '10.68.0.0/16',
        'api_server_port': 6443,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def sessions_with_lb(lb_output=LB_MACHINE, peer_output=LB_PEER, masters=3):
    """拼出「若干 master + 两台负载均衡机器」的会话表。

    参数：`lb_output` 表示第一台负载均衡机器的接口输出；`peer_output` 表示第二台的输出；
        `masters` 表示 master 台数。
    返回：返回 ``{ip: 会话}``。
    副作用：只构造本机对象。
    """
    sessions = {}
    for index in range(masters):
        ip = f'192.168.1.{10 + index}'
        sessions[ip] = FakeSession(ip, MASTER, roles=['etcd', 'master'])
    sessions['192.168.1.6'] = FakeSession('192.168.1.6', lb_output, roles=['loadbalancer'])
    sessions['192.168.1.7'] = FakeSession('192.168.1.7', peer_output, roles=['loadbalancer'])
    return sessions


def run_check(deployment=None, sessions=None, reachable=None):
    """跑一次外部负载均衡预检。

    参数：`deployment` 表示部署对象；`sessions` 表示会话表；`reachable` 表示可达节点地址。
    返回：返回检查项字典。
    副作用：调用被测函数。
    """
    sessions = sessions_with_lb() if sessions is None else sessions
    if reachable is None:
        reachable = list(sessions)
    return preflight._check_external_lb(
        make_deployment() if deployment is None else deployment, sessions, reachable,
    )


class ExternalLbSkippedTest(SimpleTestCase):
    """不该管的时候要安静地跳过。"""

    def test_disabled_is_skipped(self):
        """未启用时跳过，且不影响整体是否通过。"""
        result = run_check(make_deployment(lb_mode='none'))
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('未启用外部负载均衡', result['message'])

    def test_task_without_lb_machines_is_skipped(self):
        """节点清单里没有负载均衡机器时跳过 —— 扩容/升级/清理任务会继承 VIP 字段。"""
        sessions = {
            '192.168.1.10': FakeSession('192.168.1.10', MASTER, roles=['etcd', 'master']),
            '192.168.1.11': FakeSession('192.168.1.11', MASTER, roles=['etcd', 'master']),
            '192.168.1.12': FakeSession('192.168.1.12', MASTER, roles=['etcd', 'master']),
        }
        result = run_check(sessions=sessions)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('没有负载均衡机器', result['message'])


class ExternalLbLocalOnlyTest(SimpleTestCase):
    """只看清单就能判定的错误：连不上机器时也要照样报出来。"""

    def test_missing_vip_is_failed(self):
        """勾了组件却不填 VIP：没有任何地址可落地。"""
        result = run_check(make_deployment(lb_vip=''))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没有填写 VIP', result['message'])

    def test_invalid_vip_is_failed(self):
        """非法地址、环回与组播都不能当 VIP。"""
        for value in ('192.168.1.250.1', '127.0.0.1', '224.0.0.1'):
            with self.subTest(vip=value):
                result = run_check(make_deployment(lb_vip=value))
                self.assertEqual(result['status'], 'failed')
                self.assertIn('VIP', result['message'])

    def test_vip_matching_a_node_address_is_failed(self):
        """VIP 与某台机器已有的地址相同：接管时直接冲突。"""
        result = run_check(make_deployment(lb_vip='192.168.1.10'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('与节点已有的地址重复', result['message'])

    def test_vip_inside_pod_cidr_is_failed(self):
        """VIP 落在容器网段里会与集群内部地址打架。"""
        result = run_check(make_deployment(lb_vip='10.244.5.9'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('容器网络', result['message'])

    def test_vip_inside_service_cidr_is_failed(self):
        """Service 网段同理。"""
        result = run_check(make_deployment(lb_vip='10.68.0.9'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Service 网段', result['message'])

    def test_too_few_masters_is_failed(self):
        """一个 apiserver 实例的集群没有可切换的后端。"""
        result = run_check(sessions=sessions_with_lb(masters=1))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('控制面节点', result['message'])

    def test_broken_cidr_is_not_reported_twice(self):
        """网段本身写错由「网段合法性」那一项报，这里不重复报同一个问题。"""
        result = run_check(make_deployment(pod_cidr='不是网段', service_cidr='也不是'))
        self.assertEqual(result['status'], 'passed')


class ExternalLbProbeTest(SimpleTestCase):
    """要连机器才能判定的那几项。"""

    def test_healthy_cluster_passes(self):
        """VIP 合法、未被占用，两台机器都在它的网段里。"""
        result = run_check()
        self.assertEqual(result['status'], 'passed')
        self.assertIn('192.168.1.250', result['message'])
        self.assertEqual(result['detail']['loadbalancer_nodes'], ['192.168.1.6', '192.168.1.7'])

    def test_occupied_vip_is_failed(self):
        """机器上已经有同一个地址，且读不出本平台的痕迹：可能是别的 keepalived 在抢。

        这条守的是认领的反面 —— 机器的输出里没有单元状态、也没有 keepalived 配置（``FakeSession``
        把它那份接口地址输出同时喂给了探针），所以一个字都不能替它认（对照
        ``ExternalLbClaimedVipTest``）。
        """
        occupied = addresses(('eth0', '192.168.1.6/24'), ('eth0', '192.168.1.250/24'))
        result = run_check(sessions=sessions_with_lb(lb_output=occupied))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('已被占用', result['message'])
        self.assertIn('eth0', result['message'])

    def test_vip_outside_the_machine_network_is_failed(self):
        """VIP 不在任何网段里：接管后无法在这台机器上落地。"""
        result = run_check(make_deployment(lb_vip='192.168.9.250'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没有任何网段包含 VIP', result['message'])

    def test_vip_missing_on_one_machine_is_failed(self):
        """两台机器分属不同网段时，VIP 只会在其中一台上生效 —— 备机接管即失联。"""
        other = addresses(('eth0', '10.10.0.7/24'))
        result = run_check(sessions=sessions_with_lb(peer_output=other))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('192.168.1.7', result['message'])

    def test_two_machines_in_different_networks_warn(self):
        """两台机器各自的网段没有交集时告警：VIP 漂移到另一台后应答不到 ARP。

        VRRP 是 IP 协议号 112、没有 TCP 端口可探，所以这里只能告警 —— 判失败会把「两台网段
        写法不同但其实在同一广播域」这种正常配置也拦掉。
        """
        wide = addresses(('eth0', '192.168.0.7/16'))
        result = run_check(make_deployment(lb_vip='192.168.1.250'),
                           sessions=sessions_with_lb(peer_output=wide))
        self.assertEqual(result['status'], 'warned')
        self.assertIn('同一个二层网段', result['message'])
        self.assertEqual(
            result['detail']['loadbalancer_networks'],
            {'192.168.1.6': ['192.168.1.0/24', '127.0.0.0/8'], '192.168.1.7': ['192.168.0.0/16']},
        )

    def test_virtual_interface_candidate_warns(self):
        """只有虚拟网卡的网段包含 VIP 时提示复核：keepalived 会把 VIP 挂到它上面。"""
        only_virtual = addresses(('kube-ipvs0', '192.168.1.6/24'))
        result = run_check(sessions=sessions_with_lb(lb_output=only_virtual))
        self.assertEqual(result['status'], 'warned')
        self.assertIn('kube-ipvs0', result['message'])

    def test_multiple_candidate_interfaces_warn(self):
        """有多个接口的网段包含 VIP 时提示复核 —— keepalived 用哪个网卡由 kubeasz 现探测。"""
        two = addresses(('eth0', '192.168.1.6/24'), ('eth1', '192.168.1.8/24'))
        result = run_check(sessions=sessions_with_lb(lb_output=two))
        self.assertEqual(result['status'], 'warned')
        self.assertIn('多个接口', result['message'])

    def test_unreadable_machine_warns_instead_of_passing_silently(self):
        """读不到接口地址时给警告，不假装查过。"""
        bad = FakeSession('192.168.1.6', ok=False, roles=['loadbalancer'])
        sessions = sessions_with_lb()
        sessions['192.168.1.6'] = bad
        result = run_check(sessions=sessions)
        self.assertEqual(result['status'], 'warned')
        self.assertIn('无法读取接口地址', result['message'])

    def test_only_read_only_commands_are_used(self):
        """预检只跑只读命令，不能在目标机器上留下任何痕迹。"""
        sessions = sessions_with_lb()
        run_check(sessions=sessions)
        for session in sessions.values():
            self.assertEqual(session.commands, ['ip -o -f inet addr show scope global 2>/dev/null'])


class ExternalLbClaimedVipTest(SimpleTestCase):
    """VIP 已经在网卡上：分清是本平台上一次挂的还是别人的。

    第一次部署没跑完时，本平台装下的 keepalived 已经把 VIP 抢在自己网卡上了。重试时若照旧
    判「VIP 已被占用」，这次重试永远过不去 —— 而集群没建起来，清理入口也不可用（它要集群先有
    配置版本，那是在部署成功那一刻才写的），用户只剩重装系统一条路。94 上的 k8s-cluster
    就是这样卡住的。

    认领的证据是「这台机器上 keepalived 的配置里写的就是这个地址」：那份配置由 kubeasz 的
    ex-lb 角色渲染，作者只可能是本平台。认不出来的（配置里是别的地址、单元文件都不在了）
    照旧硬失败。
    """

    # 机器上已经有这个地址了（上一次 keepalived 抢下来的），机器自己的地址也还在。
    VIP_ON_INTERFACE = addresses(('eth0', '192.168.1.6/24'), ('eth0', '192.168.1.250/24'))

    def sessions(self, probe=None, peer_probe=None):
        """拼出「VIP 已经挂在第一台负载均衡机器上」的会话表。

        参数：`probe` 表示第一台机器的认领探针输出映射；`peer_probe` 表示第二台的。
        返回：返回 ``{ip: 会话}``。
        副作用：只构造本机对象。
        """
        sessions = sessions_with_lb(lb_output=self.VIP_ON_INTERFACE)
        sessions['192.168.1.6'] = FakeSession(
            '192.168.1.6', self.VIP_ON_INTERFACE, roles=['loadbalancer'], outputs=probe,
        )
        if peer_probe:
            sessions['192.168.1.7'] = FakeSession(
                '192.168.1.7', LB_PEER, roles=['loadbalancer'], outputs=peer_probe,
            )
        return sessions

    def test_the_vip_our_own_keepalived_holds_is_claimed(self):
        """配置里写的就是这个地址：认领，降级成警告，重试能往下走。"""
        probe = self_install(units=(('keepalived', 'active'),), keepalived_conf=keepalived_conf())
        result = run_check(sessions=self.sessions(probe))

        self.assertEqual(result['status'], 'warned', result['message'])
        self.assertIn('原地接管', result['message'])
        self.assertNotIn('已被占用', result['message'])

    def test_the_claim_names_the_machine_the_interface_and_the_service_state(self):
        """认领的文案要说清是谁挂的、挂在哪、服务现在什么状态 —— 用户要能自己复核。"""
        probe = self_install(units=(('keepalived', 'active'),), keepalived_conf=keepalived_conf())
        result = run_check(sessions=self.sessions(probe))

        self.assertIn('192.168.1.6', result['message'])
        self.assertIn('eth0', result['message'])
        self.assertIn('active', result['message'])
        self.assertTrue(result['detail']['claimed'])

    def test_a_stopped_keepalived_still_holds_the_claim(self):
        """服务停了、地址还留在网卡上：同样是上一次没清干净，重试会重新起同一个 keepalived 接管。

        这里**不要求** keepalived 正在跑：状态只用来把话说清楚，不用来判能不能认领。
        """
        probe = self_install(units=(('keepalived', 'inactive'),), keepalived_conf=keepalived_conf())
        result = run_check(sessions=self.sessions(probe))

        self.assertEqual(result['status'], 'warned', result['message'])
        self.assertIn('inactive', result['message'])

    def test_a_keepalived_declaring_another_address_does_not_claim_it(self):
        """配置里是别的地址：网卡上这个 VIP 不是它挂的，不能替它认。"""
        probe = self_install(units=(('keepalived', 'active'),), keepalived_conf=keepalived_conf('192.168.1.251'))
        result = run_check(sessions=self.sessions(probe))

        self.assertEqual(result['status'], 'failed')
        self.assertIn('已被占用', result['message'])

    def test_a_config_without_our_unit_file_does_not_claim_it(self):
        """配置躺在那里、但 keepalived 的单元文件不在：证据不足，照旧硬失败。

        残留的配置文件证明不了此刻挂地址的是本平台的 keepalived —— 那条路由 kubeasz 装，
        单元文件必须落在组件的固定路径下。
        """
        probe = self_install(keepalived_conf=keepalived_conf())
        result = run_check(sessions=self.sessions(probe))

        self.assertEqual(result['status'], 'failed')
        self.assertIn('已被占用', result['message'])

    def test_only_the_machine_carrying_the_address_is_probed(self):
        """只有真的挂着这个地址的机器才去读它的配置：没冲突的机器一行命令都不多发。"""
        probe = self_install(units=(('keepalived', 'active'),), keepalived_conf=keepalived_conf())
        sessions = self.sessions(probe)
        run_check(sessions=sessions)

        self.assertEqual(sessions['192.168.1.7'].commands, [
            'ip -o -f inet addr show scope global 2>/dev/null',
        ])
        self.assertEqual(len(sessions['192.168.1.6'].commands), 2)


class ExternalLbPortsTest(SimpleTestCase):
    """负载均衡机器的端口清单。"""

    def test_lb_machine_only_frees_the_api_port(self):
        """负载均衡机器上跑的是 l4lb，没有 kubelet，不该去查 10250。"""
        ports = preflight._expected_free_ports(make_deployment(), {'loadbalancer'})
        self.assertEqual(list(ports), [6443])
        self.assertIn('外部负载均衡 l4lb', ports[6443])

    def test_normal_node_keeps_the_kubelet_port(self):
        """普通节点的清单不受影响。"""
        ports = preflight._expected_free_ports(make_deployment(), {'etcd', 'master'})
        self.assertIn(6443, ports)
        self.assertIn(10250, ports)
        self.assertIn(2379, ports)

    def test_machine_with_both_roles_keeps_the_node_list(self):
        """同时挂着集群角色时按节点算：那种配置会在提交时就被拦下。"""
        ports = preflight._expected_free_ports(make_deployment(), {'loadbalancer', 'node'})
        self.assertIn(10250, ports)


class ExternalLbInterfaceAddressesTest(SimpleTestCase):
    """``_interface_addresses``：地址明细的解析。"""

    def test_addresses_and_networks_are_parsed(self):
        """地址、网段与接口名都要读出来，虚拟网卡标注而不是丢掉。"""
        items = preflight._interface_addresses(
            FakeSession('192.168.1.6', addresses(('eth0', '192.168.1.6/24'), ('kube-ipvs0', '10.254.0.1/32'))),
        )
        self.assertEqual(len(items), 2)
        by_name = {item['name']: item for item in items}
        self.assertEqual(by_name['eth0']['address'], '192.168.1.6')
        self.assertEqual(str(by_name['eth0']['network']), '192.168.1.0/24')
        self.assertFalse(by_name['eth0']['virtual'])
        self.assertTrue(by_name['kube-ipvs0']['virtual'])

    def test_failed_command_returns_none(self):
        """命令失败时返回 None，调用方据此给警告而不是当成「没有地址」。"""
        self.assertIsNone(preflight._interface_addresses(FakeSession('192.168.1.6', ok=False)))

    def test_unparsable_line_is_skipped(self):
        """单行解析失败只跳过那一行。"""
        output = addresses(('eth0', '192.168.1.6/24')) + '\n3: eth1    inet 999.1.1.1/24 scope global eth1'
        items = preflight._interface_addresses(FakeSession('192.168.1.6', output))
        self.assertEqual([item['name'] for item in items], ['eth0'])


class CloudLbTest(SimpleTestCase):
    """云内网负载均衡模式下的预检：只验证地址，且**不连任何机器**。

    这个模式下平台不在集群里装东西，代理的监听端口、后端与健康检查都在云控制台上。
    从集群侧能判的只有「写进 apiserver 证书 SAN 的地址可用吗」，其余全部交给一条提示。
    """

    def cloud_deployment(self, **fields):
        """构造一个云内网负载均衡模式的部署对象。

        参数：`fields` 表示要覆盖的字段。
        返回：返回属性替身。
        副作用：只在本机对象上设置字段。
        """
        values = {
            'lb_mode': 'cloud',
            'lb_vip': '10.0.9.250',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'api_server_port': 6443,
            # 新建集群类任务：只有这种任务的节点清单才是整个集群，云模式才会去判控制面数量。
            'task_type': 'deploy_ha',
        }
        values.update(fields)
        return SimpleNamespace(**values)

    def masters_only(self, masters=3):
        """拼出只有控制面节点的会话表（云模式下不会有负载均衡机器）。

        参数：`masters` 表示 master 台数。
        返回：返回 ``{ip: 会话}``。
        副作用：只构造本机对象。
        """
        sessions = {}
        for index in range(masters):
            ip = f'192.168.1.{10 + index}'
            sessions[ip] = FakeSession(ip, MASTER, roles=['etcd', 'master'])
        return sessions

    def run_cloud(self, deployment=None, sessions=None, reachable=None):
        """跑一次云内网负载均衡预检。

        参数：`deployment` 表示部署对象；`sessions` 表示会话表；`reachable` 表示可达节点地址。
        返回：返回检查项字典。
        副作用：调用被测函数。
        """
        sessions = self.masters_only() if sessions is None else sessions
        if reachable is None:
            reachable = list(sessions)
        return preflight._check_external_lb(
            self.cloud_deployment() if deployment is None else deployment, sessions, reachable,
        )

    def test_valid_address_is_warned_not_passed(self):
        """地址合规也只是 warned：平台验不了云上的后端配没配好，不能给出「没问题」的结论。"""
        result = self.run_cloud()
        self.assertEqual(result['status'], 'warned')
        self.assertEqual(result['detail']['mode'], 'cloud')
        self.assertEqual(result['detail']['vip'], '10.0.9.250')
        self.assertEqual(result['detail']['port'], 6443)

    def test_backend_targets_list_the_master_addresses(self):
        """detail 里带出后端清单，供确认页原样抄到云控制台。"""
        result = self.run_cloud()
        self.assertEqual(
            result['detail']['backend_targets'],
            ['192.168.1.10:6443', '192.168.1.11:6443', '192.168.1.12:6443'],
        )
        self.assertIn('TCP', result['message'])
        self.assertIn('6443', result['message'])

    def test_no_remote_command_is_run(self):
        """云模式一条远程命令都不该执行 —— 云 LB 不在节点上落地，没什么可查的。"""
        sessions = self.masters_only()
        self.run_cloud(sessions=sessions)
        for session in sessions.values():
            self.assertEqual(session.commands, [])

    def test_missing_address_is_failed(self):
        """没填地址：证书 SAN 里什么都不会多出来，外部 kubectl 必然被拒。"""
        result = self.run_cloud(self.cloud_deployment(lb_vip=''))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没有填写云负载均衡器的地址', result['message'])

    def test_invalid_addresses_are_failed(self):
        """非法、环回、组播地址都判失败。"""
        for value in ('10.0.9.256', '10.0.9.0/24', '127.0.0.1', '224.0.0.1'):
            with self.subTest(address=value):
                result = self.run_cloud(self.cloud_deployment(lb_vip=value))
                self.assertEqual(result['status'], 'failed')
                self.assertIn(value, result['message'])

    def test_address_matching_a_node_is_failed(self):
        """地址与某台 master 相同：把它写进 SAN 没有意义，多半是填错了。"""
        result = self.run_cloud(self.cloud_deployment(lb_vip='192.168.1.11'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('重复', result['message'])

    def test_address_inside_a_cluster_network_is_failed(self):
        """地址落进 Pod / Service 网段：外部流量会被路由到集群内部地址上。"""
        result = self.run_cloud(self.cloud_deployment(lb_vip='10.244.5.9'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('容器网络', result['message'])
        result = self.run_cloud(self.cloud_deployment(lb_vip='10.68.0.9'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Service 网段', result['message'])

    def test_single_master_is_failed(self):
        """只有一个控制面时，负载均衡后面没有可切换的后端。"""
        result = self.run_cloud(sessions=self.masters_only(masters=1))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('控制面节点', result['message'])

    def test_broken_cidr_is_not_reported_twice(self):
        """网段本身写错由「网段合法性」那一项报，这里不重复报同一个问题。"""
        result = self.run_cloud(self.cloud_deployment(pod_cidr='不是网段', service_cidr='也不是'))
        self.assertEqual(result['status'], 'warned')

    def test_derived_task_is_skipped_instead_of_failed(self):
        """加 Node / 升级这类派生任务的清单里只有本次要动的机器，判它而不是跳过会把扩容挡死。

        加 Node 时清单里一台控制面都没有，加 Master 时只有新那一台 —— 两种情况都必然
        少于 3 台，若照旧判 failed，任务会在预检阶段直接中止，而机器上一行命令都没执行。
        """
        for task_type in ('add_worker', 'add_master', 'upgrade'):
            with self.subTest(task_type=task_type):
                result = self.run_cloud(
                    self.cloud_deployment(task_type=task_type),
                    sessions=self.masters_only(masters=1),
                )
                self.assertEqual(result['status'], 'skipped')
                self.assertIn('不是整个集群', result['message'])

    def test_derived_task_still_carries_the_backend_snapshot(self):
        """跳过也要把 detail 带上：确认页照抄的那份后端清单不因任务类型而缺。"""
        result = self.run_cloud(
            self.cloud_deployment(task_type='add_worker'),
            sessions=self.masters_only(masters=1),
        )
        self.assertEqual(result['detail']['mode'], 'cloud')
        self.assertEqual(result['detail']['backend_targets'], ['192.168.1.10:6443'])
