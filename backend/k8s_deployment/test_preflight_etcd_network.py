"""etcd 内网互通预检的用例：成员之间那两个端口将来通不通。

这一项与「网卡与内网互通」的分工是要守住的边界：那一项探 SSH 端口（此刻一定有 sshd 在听），
只能回答「网络通不通」，不通也只给警告；这一项探 etcd 的 2379/2380（部署前必然是空的），
要回答的是「装上去之后通不通」—— 靠**建连耗时**把两种失败分开：

- 秒回失败（对端回 RST）= 包一来一回都走通了，只是端口上暂时没进程在听 → 部署前的正常状态；
- 等满超时 = 包被丢弃 → 云安全组、VPC 网络 ACL 或主机防火墙挡着，**装完 etcd 照样不通**。

后者必须硬失败：让部署继续跑下去，只会把机器留在一个 etcd 选不出主、但外表看着装完了的
半成品状态。反过来，把「检查不了」（节点上没有 bash）判成「不通」会让一堆正常的机器过不了
预检，所以那一条只给警告。

另有一条容易探错的地方要钉住：探的必须是**集群内部通信用的地址**，不是操作员填进来那一个。
节点在 NAT 后面时前者是内网地址、后者是公网地址，而安全组规则通常只管内网那一条。
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from k8s_deployment.services import node_address, preflight


class EtcdSession:
    """假节点会话：认三类命令 —— 地址探测、etcd 端口探测、其它（一律失败）。

    端口探测的输出由 ``markers`` 决定，键是 ``(对端地址, 端口)``；调用方只放自己要探的
    那几项进去，因此「探了不该探的地址」会表现为那条命令的输出里少一行、结论降级成
    「未能验证」，用例据此看得出来。
    """

    def __init__(self, ip, cluster_address, roles=('etcd', 'master'), probe_ok=True, no_bash=False):
        """初始化。

        参数：`ip` 表示平台连接该节点用的地址；`cluster_address` 表示它集群内部通信用的地址；
            `roles` 表示角色；`probe_ok` 表示地址探测命令是否成功；`no_bash` 表示节点上没有 bash。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.ip = ip
        self.cluster_address = cluster_address
        self.probe_ok = probe_ok
        self.no_bash = no_bash
        self.markers = {}
        self.commands = []
        self.node = SimpleNamespace(roles=list(roles), ip_address=ip, cluster_ip=None)

    def run(self, command, timeout=None):
        """记录并「执行」一条命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(ok, 输出, 错误)``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        if command == node_address.PROBE_COMMAND:
            if not self.probe_ok:
                return False, '', 'ip: command not found'
            return True, f'ROUTE {self.cluster_address}\nADDRS {self.cluster_address}', ''
        if command.startswith('if command -v bash'):
            if self.no_bash:
                return True, 'NO_BASH', ''
            lines = [
                f'PROBE {address}:{port} {marker}'
                for (address, port), marker in self.markers.items()
                if f'{address}:{port}' in command
            ]
            return True, '\n'.join(lines), ''
        return False, '', f'unexpected command: {command}'


def make_deployment(task_type='deploy_ha', **fields):
    """构造一个只带预检所需字段的部署对象。

    参数：`task_type` 表示任务类型；`fields` 表示要覆盖的字段。
    返回：返回一个属性替身。
    副作用：只在本机对象上设置字段。
    """
    values = {'task_type': task_type, 'api_server_port': 6443}
    values.update(fields)
    return SimpleNamespace(**values)


def build_sessions(members, default=preflight.PROBE_REFUSED, overrides=None, roles=None, **kwargs):
    """拼出若干 etcd 成员的会话表。

    参数：`members` 表示 ``[(填写的地址, 集群内部地址), ...]``；`default` 表示未特别指定的
        探测结论；`overrides` 表示 ``{(来源地址, 对端地址, 端口): 结论}`` 的覆盖表；
        `roles` 表示 ``{地址: 角色列表}``；`kwargs` 传给每个会话。
    返回：返回 ``{ip: 会话}``。
    副作用：只构造本机对象。
    """
    sessions = {}
    for ip, address in members:
        session = EtcdSession(
            ip, address,
            roles=(roles or {}).get(ip, ('etcd', 'master')),
            **kwargs,
        )
        for other_ip, other_address in members:
            if other_ip == ip:
                continue
            # 两个地址都登记上：读不出网卡地址时被探的是填写值，读得出时是内网地址。
            # 真机无论被探哪个地址都会照样回一个结论，假实现要跟上这一条。
            for port in preflight.ETCD_REACH_PORTS:
                session.markers[(other_address, port)] = default
                session.markers[(other_ip, port)] = default
        sessions[ip] = session
    for (source_ip, target_address, port), marker in (overrides or {}).items():
        sessions[source_ip].markers[(target_address, port)] = marker
    return sessions


# 三台等常见拓扑：填的是公网地址，网卡上是内网地址（节点在 NAT 后面），
# 这正是「探哪个地址」会出错的那种情形。
THREE = [
    ('116.198.237.12', '172.30.0.12'),
    ('116.198.237.25', '172.30.0.25'),
    ('116.198.237.15', '172.30.0.15'),
]


class TwoNodesTest(SimpleTestCase):
    """两台以上成员时的常规判定。"""

    def run_check(self, sessions, deployment=None):
        """跑一次检查。

        参数：`sessions` 表示会话表；`deployment` 表示部署对象。
        返回：返回检查项字典。
        副作用：在假节点上执行被记录下来的命令。
        """
        return preflight._check_etcd_network(
            deployment or make_deployment(), sessions, list(sessions),
        )

    def test_refused_counts_as_reachable(self):
        """秒回失败是部署前的正常状态：端口上还没进程，但网络已经放行了。"""
        sessions = build_sessions(THREE[:2])

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['title'], 'etcd 内网互通')

    def test_open_also_counts_as_reachable(self):
        """重试已有集群时 etcd 就在跑，连得上当然也算通。"""
        sessions = build_sessions(THREE[:2], default=preflight.PROBE_OPEN)

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'passed')

    def test_dropped_packets_are_a_hard_failure(self):
        """包被丢弃必须挡住部署：装完 etcd 也不会自动通。"""
        sessions = build_sessions(
            THREE[:2],
            overrides={('116.198.237.12', '172.30.0.25', 2380): preflight.PROBE_DROPPED},
        )

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('116.198.237.12->172.30.0.25:2380', check['message'])

    def test_failure_message_names_the_firewall_and_the_ports(self):
        """提示要能直接照着改：放行哪些地址、哪些端口、去哪儿放。"""
        sessions = build_sessions(
            THREE[:2],
            overrides={('116.198.237.12', '172.30.0.25', 2379): preflight.PROBE_DROPPED},
        )

        check = self.run_check(sessions)

        self.assertIn('安全组', check['message'])
        self.assertIn('2379、2380', check['message'])
        self.assertIn('172.30.0.12', check['message'])
        self.assertIn('172.30.0.25', check['message'])

    def test_failure_message_says_returns_dropped_not_refused(self):
        """说清是「被丢弃」而不是「被拒绝」—— 这两句话指向完全不同的排查方向。"""
        sessions = build_sessions(
            THREE[:2],
            overrides={('116.198.237.12', '172.30.0.25', 2380): preflight.PROBE_DROPPED},
        )

        self.assertIn('被丢弃', self.run_check(sessions)['message'])

    def test_only_one_direction_dropped_is_still_a_failure(self):
        """etcd 要双向通信，一个方向被挡住就是不通。"""
        sessions = build_sessions(
            THREE[:2],
            overrides={('116.198.237.25', '172.30.0.12', 2380): preflight.PROBE_DROPPED},
        )

        self.assertEqual(self.run_check(sessions)['status'], 'failed')

    def test_no_bash_is_a_warning_not_a_failure(self):
        """节点上没有 bash 是「检查不了」，不是「有问题」。"""
        sessions = build_sessions(THREE[:2], no_bash=True)

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'warned')
        self.assertIn('bash', check['message'])

    def test_unreadable_address_probe_still_checks_the_entered_address(self):
        """读不出网卡地址时退回填写的地址，与部署时的行为一致。"""
        sessions = build_sessions(THREE[:2], probe_ok=False)

        check = self.run_check(sessions)
        command = [item for item in sessions['116.198.237.12'].commands
                   if item.startswith('if command -v bash')][0]

        self.assertEqual(check['status'], 'passed')
        self.assertIn('116.198.237.25:2379', command)

    def test_three_members_produce_all_ordered_pairs(self):
        """三台成员 = 6 个有序对，每个对两个端口，一条都不能少。"""
        sessions = build_sessions(THREE)

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'passed')
        self.assertEqual(len(check['detail']['checked_pairs']), 12)


class AddressChoiceTest(SimpleTestCase):
    """探的是集群内部通信用的地址，不是操作员填进来那一个。"""

    def commands(self, sessions, ip):
        """取回某台节点收到的全部命令。"""
        return sessions[ip].commands

    def test_private_address_is_probed_instead_of_the_entered_one(self):
        """节点在 NAT 后面时必须探内网地址：安全组规则管的正是那一条。"""
        sessions = build_sessions(THREE[:2])

        preflight._check_etcd_network(make_deployment(), sessions, list(sessions))
        command = [item for item in self.commands(sessions, '116.198.237.12')
                   if item.startswith('if command -v bash')][0]

        self.assertIn('172.30.0.25:2380', command)
        self.assertNotIn('116.198.237.25', command)

    def test_a_node_never_probes_itself(self):
        """自己到自己没有信息量：etcd 还没装，必然是「被拒绝」。"""
        sessions = build_sessions(THREE[:2])

        preflight._check_etcd_network(make_deployment(), sessions, list(sessions))
        command = [item for item in self.commands(sessions, '116.198.237.12')
                   if item.startswith('if command -v bash')][0]

        self.assertNotIn('172.30.0.12:', command)

    def test_addresses_are_recorded_in_the_detail(self):
        """明细里留下「填的地址 → 集群地址」的对照，排查地址填法时用得上。"""
        sessions = build_sessions(THREE[:2])

        check = preflight._check_etcd_network(make_deployment(), sessions, list(sessions))

        self.assertEqual(
            check['detail']['nodes']['116.198.237.12']['cluster_address'], '172.30.0.12',
        )
        self.assertEqual(
            check['detail']['nodes']['116.198.237.12']['entered'], '116.198.237.12',
        )


class ScopeTest(SimpleTestCase):
    """这一项在哪些任务、哪些节点上跑。"""

    def run_check(self, sessions, deployment=None):
        """跑一次检查。"""
        return preflight._check_etcd_network(
            deployment or make_deployment(), sessions, list(sessions),
        )

    def test_single_member_is_skipped(self):
        """单节点集群没有成员间通信，探自己到自己只会得到「被拒绝」。"""
        sessions = build_sessions(THREE[:1])

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'skipped')
        self.assertEqual(check['detail']['etcd_nodes'], ['116.198.237.12'])

    def test_no_etcd_node_is_skipped(self):
        """一台 etcd 都没有（例如只加 Node 的扩容任务）时跳过。"""
        sessions = build_sessions(
            [('192.168.1.10', '192.168.1.10'), ('192.168.1.11', '192.168.1.11')],
            roles={'192.168.1.10': ['node'], '192.168.1.11': ['node']},
        )

        check = self.run_check(sessions, make_deployment(task_type='add_worker'))

        self.assertEqual(check['status'], 'skipped')

    def test_masters_count_as_etcd_members(self):
        """清单里没写 etcd 时 kubeasz 把 master 当 etcd 用，检查口径要跟上。"""
        sessions = build_sessions(
            [('192.168.1.10', '192.168.1.10'), ('192.168.1.11', '192.168.1.11')],
            roles={'192.168.1.10': ['master'], '192.168.1.11': ['master']},
        )

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'passed')
        self.assertEqual(sorted(check['detail']['etcd_nodes']), ['192.168.1.10', '192.168.1.11'])

    def test_load_balancer_machines_are_not_members(self):
        """负载均衡机器上不装 etcd，不该被拉进成员清单里。"""
        sessions = build_sessions(
            THREE[:2],
            roles={
                '116.198.237.12': ['etcd', 'master'],
                '116.198.237.25': ['loadbalancer'],
            },
        )

        check = self.run_check(sessions)

        self.assertEqual(check['status'], 'skipped')
        self.assertEqual(check['detail']['etcd_nodes'], ['116.198.237.12'])

    def test_no_reachable_node_is_skipped(self):
        """一台都连不上时沿用统一的「跳过」口径，不重复报 SSH 的错。"""
        check = preflight._check_etcd_network(make_deployment(), {}, [])

        self.assertEqual(check['status'], 'skipped')


class CommandShapeTest(SimpleTestCase):
    """命令文本本身：注入防护与「不依赖 nc/awk」。"""

    def test_snippet_rejects_a_non_ip_target(self):
        """地址必须是 IP 字面量：它会被拼进远程命令。"""
        self.assertEqual(preflight._etcd_probe_snippet('example.com; rm -rf /', 2379), '')
        self.assertEqual(preflight._etcd_probe_snippet('172.30.0.12', 0), '')
        self.assertEqual(preflight._etcd_probe_snippet('172.30.0.12', 70000), '')

    def test_command_does_not_depend_on_nc_or_awk(self):
        """只用 bash 的 ``/dev/tcp`` 与 ``date``：目标机上装了什么不该决定检查做不做得了。"""
        command = preflight._etcd_reach_command([('172.30.0.12', 2379)])

        self.assertNotIn('/bin/awk', command)
        self.assertIn('/dev/tcp/172.30.0.12/2379', command)

    def test_command_has_one_probe_per_target(self):
        """一次连上去把所有目标探完：被丢弃的目标个个都要等满超时，逐个发命令太慢。"""
        targets = [('172.30.0.12', 2379), ('172.30.0.12', 2380), ('172.30.0.25', 2379)]

        command = preflight._etcd_reach_command(targets)

        # 每个目标只建一次连接。打印那一行会在四种结论的分支里各出现一次，所以数连接
        # 而不是数 ``PROBE`` 这个词。
        self.assertEqual(command.count('/dev/tcp/'), 3)
        self.assertEqual(command.count('/dev/tcp/172.30.0.12/2379'), 1)
        self.assertEqual(command.count('/dev/tcp/172.30.0.12/2380'), 1)
        self.assertEqual(command.count('/dev/tcp/172.30.0.25/2379'), 1)

    def test_command_without_usable_targets_says_so(self):
        """一个能用的目标都没有时给出 NO_BASH 标记，调用方记成「未能验证」。"""
        self.assertEqual(preflight._etcd_reach_command([]), 'echo NO_BASH')


class ParseTest(SimpleTestCase):
    """``_parse_etcd_probe``：只认形状对得上的行。"""

    def test_reads_probe_lines(self):
        """正常解析。"""
        found = preflight._parse_etcd_probe(
            'PROBE 172.30.0.25:2379 TCP_REFUSED\nPROBE 172.30.0.25:2380 TCP_DROPPED\n',
        )

        self.assertEqual(found, {'172.30.0.25:2379': 'TCP_REFUSED', '172.30.0.25:2380': 'TCP_DROPPED'})

    def test_ignores_login_noise(self):
        """motd、欢迎语、别处的 echo 都不能被当成结论。"""
        found = preflight._parse_etcd_probe(
            'Welcome to Ubuntu 22.04\nLast login: Mon Sep 22\n'
            'PROBE 172.30.0.25:2379 TCP_OPEN\nsome other output\n',
        )

        self.assertEqual(found, {'172.30.0.25:2379': 'TCP_OPEN'})

    def test_empty_output(self):
        """没有输出时返回空表，调用方据此判「未能验证」。"""
        self.assertEqual(preflight._parse_etcd_probe(''), {})
        self.assertEqual(preflight._parse_etcd_probe('NO_BASH'), {})
