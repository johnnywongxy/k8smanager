"""端口占用预检的用例：全新安装要硬查，补丁升级要放过，全量重试要认领自家残留。

真实背景有两件，是同一类缺陷的两个面：

1. 94 上「补丁升级」失败在「端口占用：114.67.124.144：etcd 端口 2379 已被占用
   （LISTEN 172.30.0.14:2379）、ApiServer 端口 6443 已被占用、kubelet 端口 10250 已被占用」，
   而占用这些端口的**就是那台节点上正在跑的集群自己**。升级的任务节点清单与集群节点清单是
   同一份，于是「这些端口必须空闲」这条规则把每一次升级都判成失败。
2. 94 上「新建集群 k8s-cluster」第一次部署没跑完，etcd / apiserver / kube-lb / kubelet 已经
   在那批机器上跑着了，点「重试」被同一条规则按同样的理由拦死 —— 而集群没建起来，连「清理
   集群」的入口都没有（那条路要集群先有配置版本）。占端口的就是本平台上一轮自己装下的东西。

要守住的是**三侧**：升级不再被自己拦下；重试能认领自己上一轮留下的组件、原地接管；而认不出来
的占用（别人的进程、单元文件躺着但服务没跑、扩容任务要加的那台机器）仍然一个都不能放过 ——
「认领」必须是有证据的判断，不能退化成「部署任务一律放行」。
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from k8s_deployment.services import preflight

# 一台正在跑集群的节点上 ``ss -lnt`` 的形态：etcd 的两个端口、ApiServer、kubelet 都在监听。
CLUSTER_NODE_LISTENING = '\n'.join([
    'State  Recv-Q Send-Q Local Address:Port  Peer Address:Port',
    'LISTEN 0      65535    172.30.0.14:2379       0.0.0.0:*',
    'LISTEN 0      65535    172.30.0.14:2380       0.0.0.0:*',
    'LISTEN 0      65535    172.30.0.14:6443       0.0.0.0:*',
    'LISTEN 0      65535              *:10250            *:*',
])

# 一台除了 SSH 什么都没跑的机器。
IDLE_NODE_LISTENING = '\n'.join([
    'State  Recv-Q Send-Q Local Address:Port  Peer Address:Port',
    'LISTEN 0      128          0.0.0.0:22         0.0.0.0:*',
])

# 一台已经在跑 etcd 的机器（比如上一个集群没清干净的）：2379/2380 在监听，apiserver 与
# kubelet 的端口空着。把它加为 Master 时，真正会起服务的只有后两个端口。
ETCD_ONLY_LISTENING = '\n'.join([
    'State  Recv-Q Send-Q Local Address:Port  Peer Address:Port',
    'LISTEN 0      65535    172.30.0.14:2379       0.0.0.0:*',
    'LISTEN 0      65535    172.30.0.14:2380       0.0.0.0:*',
])


class FakeSession:
    """假节点会话：把 ``ss`` 的输出原样喂回去，并记录收到的命令。

    ``outputs`` 用来按命令文本给不同的输出（认领探针要的就是这个）：命中就返回它，没命中
    退回 ``output``。绝大多数用例只跑一条 ``ss``，用不着它。
    """

    def __init__(self, ip, output='', ok=True, roles=(), outputs=None):
        """初始化。

        参数：`ip` 表示节点地址；`output` 表示默认的命令标准输出；`ok` 表示命令是否成功；
            `roles` 表示该节点在任务快照里的集群角色；`outputs` 表示按命令文本给出的输出映射。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.ip = ip
        self.output = output
        self.ok = ok
        self.outputs = dict(outputs or {})
        self.node = SimpleNamespace(roles=list(roles))
        self.commands = []

    def run(self, command, timeout=None):
        """记录并「执行」一条命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(ok, 输出, '')``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        return self.ok, self.outputs.get(command, self.output), ''


def probe_output(units=(), keepalived_conf=''):
    """拼出认领探针的输出。

    参数：`units` 表示 ``(单元名, 状态)`` 形式的二元组序列；`keepalived_conf` 表示配置全文。
    返回：返回命令输出文本。
    副作用：纯计算。
    """
    lines = [preflight.SELF_INSTALL_PROBE_BEGIN]
    lines.extend(f'UNIT:{name}:{state}' for name, state in units)
    lines.append(preflight.KEEPALIVED_CONF_BEGIN)
    lines.extend(str(keepalived_conf).splitlines())
    lines.append(preflight.KEEPALIVED_CONF_END)
    return '\n'.join(lines)


def self_install(units=(), keepalived_conf=''):
    """按探针命令文本包一份 ``FakeSession.outputs``。

    参数：`units` 表示 ``(单元名, 状态)`` 形式的二元组序列；`keepalived_conf` 表示配置全文。
    返回：返回 ``{命令文本: 输出}``。
    副作用：纯计算。
    """
    return {preflight._self_install_probe_command(): probe_output(units, keepalived_conf)}


def make_deployment(task_type='deploy_aio', **fields):
    """构造一个只带预检所需字段的部署对象。

    参数：`task_type` 表示任务类型；`fields` 表示要覆盖的字段。
    返回：返回一个属性替身。
    副作用：只在本机对象上设置字段。
    """
    values = {'task_type': task_type, 'api_server_port': 6443}
    values.update(fields)
    return SimpleNamespace(**values)


def run_check(task_type, roles=('etcd', 'master', 'node'), output=IDLE_NODE_LISTENING, outputs=None):
    """跑一次端口占用检查。

    参数：`task_type` 表示任务类型；`roles` 表示节点的集群角色；`output` 表示 ``ss`` 的输出；
        `outputs` 表示按命令文本给出的输出映射（认领探针用）。
    返回：返回 ``(检查项字典, 假会话)``。
    副作用：调用被测函数。
    """
    session = FakeSession('114.67.124.144', output, roles=roles, outputs=outputs)
    check = preflight._check_ports(
        make_deployment(task_type), {'114.67.124.144': session}, ['114.67.124.144'],
    )
    return check, session


class UpgradePortsCheckTest(SimpleTestCase):
    """补丁升级打的是集群自己的节点，这一项对它不适用。"""

    def test_the_clusters_own_listeners_do_not_fail_an_upgrade(self):
        """etcd / ApiServer / kubelet 在监听是升级的前提，不是障碍。"""
        check, _session = run_check('upgrade', output=CLUSTER_NODE_LISTENING)

        self.assertEqual(check['name'], 'ports')
        self.assertEqual(check['status'], 'skipped')

    def test_a_skipped_check_does_not_even_connect(self):
        """跳过就要真的跳过：连 ``ss`` 都不查。

        让它在「跳过」的名义下继续 SSH 一遍，等于把一份不再被采信的判断留在代码里 ——
        下一个人会以为那些命令的输出仍然有用。
        """
        _check, session = run_check('upgrade', output=CLUSTER_NODE_LISTENING)

        self.assertEqual(session.commands, [])

    def test_the_message_says_the_cluster_is_expected_to_be_running(self):
        """跳过也要说清原因，否则界面上会是一个不解释的「已跳过」。"""
        check, _session = run_check('upgrade', output=CLUSTER_NODE_LISTENING)

        self.assertIn('现有集群', check['message'])
        self.assertEqual(check['detail']['reason'], 'upgrade_existing_cluster')

    def test_the_check_still_names_the_nodes_it_covers(self):
        """节点清单照旧带出来，前端才知道这一项落在谁身上。"""
        check, _session = run_check('upgrade', output=CLUSTER_NODE_LISTENING)

        self.assertEqual(check['nodes'], ['114.67.124.144'])


class FreshInstallPortsCheckTest(SimpleTestCase):
    """新装节点仍然必须端口空闲 —— 不能因为升级要放过就把这一项做成永远通过。"""

    def test_a_busy_port_fails_a_new_deployment(self):
        """全新部署时端口被占就是硬失败：kubeasz 起服务会直接撞上去。"""
        check, _session = run_check('deploy_aio', output=CLUSTER_NODE_LISTENING)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('2379', check['message'])
        self.assertIn('6443', check['message'])

    def test_an_idle_node_passes(self):
        """什么都没跑的机器必须判通过。"""
        check, _session = run_check('deploy_aio')

        self.assertEqual(check['status'], 'passed')

    def test_the_new_machine_of_an_expansion_is_still_checked(self):
        """扩容任务里只有那台新机器，它的端口必须空闲。

        这一条是与升级那条一起守的：``add_worker`` 的任务节点清单里没有集群已有节点，
        所以「不适用」的理由（节点上跑着集群）在这里并不成立。
        """
        listening = '\n'.join([
            'LISTEN 0      128          0.0.0.0:22         0.0.0.0:*',
            'LISTEN 0      128          0.0.0.0:6443       0.0.0.0:*',
        ])
        session = FakeSession('114.67.89.57', listening, roles=('node',))
        check = preflight._check_ports(
            make_deployment('add_worker'), {'114.67.89.57': session}, ['114.67.89.57'],
        )

        self.assertEqual(check['status'], 'failed')
        self.assertIn('6443', check['message'])
        self.assertNotIn('2379', check['message'])

    def test_a_master_without_the_etcd_role_still_has_its_etcd_ports_checked(self):
        """全量部署里 master 不带 etcd 角色时**仍然**查 2379/2380。

        这是 ``compiler.render_hosts`` 的兜底：一份没有任何节点声明 etcd 的清单会被 kubeasz
        当成「etcd 就跑在 master 上」，那台机器上真的会起 etcd。不能因为扩容任务要放过，
        就把这条一起放过 —— 那会把一次注定失败的部署放行。
        """
        check, _session = run_check('deploy_ha', roles=('master',), output=CLUSTER_NODE_LISTENING)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('2379', check['message'])


class FullDeployRetryPortsCheckTest(SimpleTestCase):
    """全量部署重试：认领本平台上一轮自己装下的组件，别人的进程一个字都不认。

    94 上「新建集群 k8s-cluster」第一次部署没跑完，重试时 etcd / apiserver / kube-lb / kubelet
    都在那批机器上跑着。占端口的是本平台自己，而它的清理入口要集群先有配置版本才可用（配置版本
    在部署成功那一刻才写），用户只剩重装系统一条路 —— 这里就是要把那条路省掉。
    """

    # 一次「本平台装了一半」的现场：etcd 的两个端口、apiserver 侧（kube-lb）与 kubelet 全在跑。
    # 6443 归 kube-lb 而不是 kube-apiserver，因为 kubeasz 的 kube-lb 监听 127.0.0.1:6443，
    # 节点上对外那个 6443 由它代理 —— 94 的日志里出现的就是 127.0.0.1:6443。
    OWN_RESIDUE = self_install(units=(('etcd', 'active'), ('kube-lb', 'active'), ('kubelet', 'active')))

    def test_ports_held_by_our_own_services_are_claimed(self):
        """端口上跑着的是本平台上一次的组件：降级成警告，任务原地接管。"""
        check, _session = run_check('deploy_ha', output=CLUSTER_NODE_LISTENING, outputs=self.OWN_RESIDUE)

        self.assertEqual(check['status'], 'warned', check['message'])
        self.assertIn('原地接管', check['message'])
        self.assertIn('etcd', check['message'])
        self.assertIn('kube-lb', check['message'])

    def test_a_claimed_port_is_not_reported_as_a_failure(self):
        """认领过的端口不能同时出现在「已被占用」的硬失败文案里，否则用户还是不知道该信哪句。"""
        check, _session = run_check('deploy_ha', output=CLUSTER_NODE_LISTENING, outputs=self.OWN_RESIDUE)

        self.assertNotIn('已被占用', check['message'])

    def test_the_claim_is_recorded_per_node_and_per_port(self):
        """认领要留痕：哪台机器的哪个端口、被哪个单元占着，界面才有得展示。"""
        check, _session = run_check('deploy_ha', output=CLUSTER_NODE_LISTENING, outputs=self.OWN_RESIDUE)

        entry = check['detail']['nodes']['114.67.124.144']
        self.assertTrue(entry['ok'])
        self.assertEqual(len(entry['claimed']), 4)
        self.assertIn('2379', ' '.join(entry['claimed']))
        self.assertIn('10250', ' '.join(entry['claimed']))

    def test_a_foreign_process_still_fails(self):
        """端口被占、机器上读不出本平台的任何痕迹：照旧硬失败。"""
        check, _session = run_check('deploy_ha', output=CLUSTER_NODE_LISTENING)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('已被占用', check['message'])

    def test_a_stopped_unit_does_not_get_claimed(self):
        """单元文件躺着但服务没在跑：占端口的就不是它，不能替它认。

        ``inactive`` 是这一条的关键：组件装过、现在停了，此时端口上监听的是别的进程 ——
        认下来等于把一次注定失败的部署放行。
        """
        residue = self_install(units=(('etcd', 'inactive'), ('kube-lb', 'inactive'), ('kubelet', 'inactive')))
        check, _session = run_check('deploy_ha', output=CLUSTER_NODE_LISTENING, outputs=residue)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('已被占用', check['message'])

    def test_a_claimed_port_does_not_cover_a_foreign_one(self):
        """同一台机器上一边是自家残留、一边是外来占用：仍然硬失败。

        这里探针里只有 etcd 与 kubelet，6443 上那个监听的持有者（kube-lb / kube-apiserver）
        认不出来 —— 只认自己认得出来的端口，不能因为「这台机器上有我们的东西」就整台放行。
        """
        residue = self_install(units=(('etcd', 'active'), ('kubelet', 'active')))
        check, _session = run_check('deploy_ha', output=CLUSTER_NODE_LISTENING, outputs=residue)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('6443', check['message'])
        self.assertNotIn('2379', check['message'])
        self.assertIn('2379', ' '.join(check['detail']['nodes']['114.67.124.144']['claimed']))

    def test_clean_ports_do_not_trigger_the_probe(self):
        """端口都空着时只跑 ``ss`` 一条命令：干净路径的连机器次数与从前一模一样。"""
        _check, session = run_check('deploy_ha')

        self.assertEqual(len(session.commands), 1)
        self.assertTrue(session.commands[0].startswith('ss -lnt'), session.commands)

    def test_an_expansion_never_claims(self):
        """扩容要加的那台机器上有本平台的组件 = 它本来就是别的集群的一员，必须拦下来。

        认领会把两个集群搅在一起，所以这类任务连探针都不发。
        """
        for task_type in ('add_master', 'add_worker'):
            with self.subTest(task_type=task_type):
                check, session = run_check(task_type, roles=('master', 'node'),
                                           output=CLUSTER_NODE_LISTENING, outputs=self.OWN_RESIDUE)

                self.assertEqual(check['status'], 'failed', check['message'])
                self.assertEqual(len(session.commands), 1)
                self.assertTrue(session.commands[0].startswith('ss -lnt'), session.commands)


class SelfInstallProbeTest(SimpleTestCase):
    """认领探针本身：命令文本与输出的解析。

    探针的输出是这一整套判断的唯一证据来源，读错一个字节就会把别人的进程认成自家的 ——
    所以解析的方向统一是「读不出来就不认领」，下面每一条都在钉这个方向。
    """

    def test_units_and_keepalived_config_are_parsed(self):
        """单元状态与配置全文都要读出来。"""
        parsed = preflight._parse_self_install_probe(probe_output(
            units=(('etcd', 'active'), ('kubelet', 'inactive')),
            keepalived_conf='virtual_ipaddress {\n    192.168.1.250\n}',
        ))

        self.assertEqual(parsed['units'], {'etcd': 'active', 'kubelet': 'inactive'})
        self.assertIn('192.168.1.250', parsed['keepalived_conf'])

    def test_a_unit_without_a_state_is_not_active(self):
        """单元文件在、但 ``systemctl is-active`` 什么都没给：状态是空串，不等于 active。"""
        parsed = preflight._parse_self_install_probe(probe_output(units=(('etcd', ''),)))

        self.assertEqual(parsed['units'], {'etcd': ''})
        self.assertEqual(preflight._unit_state(parsed, 'etcd'), '')
        self.assertFalse(preflight._own_unit_running_on(parsed, make_deployment('deploy_ha'), 2379))

    def test_garbage_yields_nothing_claimable(self):
        """命令没跑成、输出是垃圾（比如登录提示）：什么都认不出来。"""
        for text in ('', 'Permission denied', 'UNIT:etcd', 'UNIT::active', 'bash: ss: command not found'):
            with self.subTest(text=text):
                parsed = preflight._parse_self_install_probe(text)
                self.assertEqual(parsed['units'], {})
                self.assertEqual(parsed['keepalived_conf'], '')

    def test_a_failed_command_returns_no_trace(self):
        """命令失败时直接返回空结果，不去解析可能只写了一半的输出。"""
        session = FakeSession('114.67.124.144', ok=False, outputs=self_install(units=(('etcd', 'active'),)))

        self.assertEqual(preflight._read_self_install_trace(session), {'units': {}, 'keepalived_conf': ''})

    def test_the_command_text_is_a_constant(self):
        """命令文本必须与任务参数无关。

        ``_NodeSession.run`` 按命令文本缓存结果，把 VIP 之类的值拼进去会让「端口占用」与
        「外部负载均衡」两项各跑一次同一个探针。
        """
        command = preflight._self_install_probe_command()

        self.assertEqual(command, preflight._self_install_probe_command())
        for unit in preflight.SELF_INSTALL_UNITS:
            self.assertIn(unit, command)
        self.assertIn(preflight.KEEPALIVED_CONF, command)
        self.assertIn(preflight.SELF_INSTALL_UNIT_DIR, command)

    def test_the_api_port_is_owned_by_the_api_side_units(self):
        """ApiServer 端口由用户填写，认领时按「apiserver 侧」的候选单元找。"""
        deployment = make_deployment('deploy_ha', api_server_port=8443)

        self.assertEqual(preflight._port_owner_units(deployment, 8443), preflight.API_PORT_OWNER_UNITS)
        self.assertEqual(preflight._port_owner_units(deployment, 2379), ('etcd',))

    def test_a_deployment_task_is_claimable_and_an_expansion_is_not(self):
        """认领只对「在这批机器上装一遍集群」的任务开放。"""
        self.assertTrue(preflight._reinstalls_the_cluster(make_deployment('deploy_ha')))
        self.assertTrue(preflight._reinstalls_the_cluster(make_deployment('deploy_aio')))
        for task_type in ('add_master', 'add_worker', 'upgrade', 'destroy'):
            with self.subTest(task_type=task_type):
                self.assertFalse(preflight._reinstalls_the_cluster(make_deployment(task_type)))


class AddMasterPortsCheckTest(SimpleTestCase):
    """添加 Master：那台机器上不会装 etcd，2379/2380 不该再被要求空闲。

    ``ezctl add-master`` 的剧本里没有 etcd 角色（etcd 成员由独立的 ``ezctl add-etcd`` 添加），
    那两个端口上听着什么都与本次扩容无关 —— 真机上常见的情形就是那台机器本来就跑着一套 etcd。
    """

    def test_a_machine_that_already_runs_etcd_can_still_join_as_a_master(self):
        """已经跑着 etcd 的机器照样能加为 Master：平台上不会在那台机器上再起一个 etcd。"""
        check, _session = run_check('add_master', roles=('master',), output=ETCD_ONLY_LISTENING)

        self.assertEqual(check['status'], 'passed', check['message'])

    def test_the_apiserver_port_of_that_machine_is_still_required_to_be_free(self):
        """放过 etcd 端口不等于放过全部：6443 上真的会有 apiserver 起来。"""
        listening = ETCD_ONLY_LISTENING + '\n' + 'LISTEN 0      128          0.0.0.0:6443       0.0.0.0:*'
        check, _session = run_check('add_master', roles=('master',), output=listening)

        self.assertEqual(check['status'], 'failed')
        self.assertIn('6443', check['message'])

    def test_the_ports_it_asks_about_are_exactly_the_ones_kubeasz_will_use(self):
        """端口清单本身就是断言：扩容的机器上只会起 apiserver 与 kubelet。"""
        ports = preflight._expected_free_ports(make_deployment('add_master'), {'master'})

        self.assertEqual(sorted(ports), [6443, 10250])
