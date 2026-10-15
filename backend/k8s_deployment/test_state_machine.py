"""状态机的端到端流程测试：成功、失败、取消、超时、Worker 中断与串行领取。

执行器一律换成 ``FakeRunner``，制品下载、SSH 免密与健康探测全部打桩，
因此这些用例不碰 OSS、不碰目标机、不碰平台机的 ``~/.kube/config``（规划 13 Phase 1）。
被打桩的只有「本平台之外的东西」，状态机本身的阶段推进、日志入库、终态判定都是真实执行。
"""

import io
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from k8s_cluster.models import K8sCluster, K8sClusterConfigRevision
from k8s_deployment.models import (
    K8sDeployment,
    K8sDeploymentEvent,
    K8sDeploymentLog,
    K8sDeploymentNode,
)
from k8s_deployment.services import (
    addon_overrides,
    artifacts,
    compiler,
    credentials,
    destroy_plan,
    node_address,
    node_containers,
    node_helm,
    node_hostname,
    node_kubeconfig,
    node_l4lb,
    node_network,
    node_path,
    node_registry,
    paths,
    platform_relay,
    register,
    runner,
    stale_cleanup,
    state_machine,
)
from k8s_machine.models import Machine
from ops.models import Organization

from . import kubeasz_fixtures

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'


class StateMachineTestBase(TestCase):
    """状态机用例的公共夹具：临时状态目录、组织、任务与一件合成 kubeasz 制品。"""

    def setUp(self):
        """准备临时状态根目录、组织与一个 HA 部署任务。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录并写入数据库；用例结束时删除临时目录。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-fsm-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
            K8S_DEPLOY_AUTOSTART_WORKER=False,
            # kubeasz 安装目录在生产是 /etc/kubeasz。测试必须在临时目录里跑，否则
            # 失败路径上的诊断采集会去读真实机器上的那份配置。
            K8S_DEPLOY_KUBEASZ_ROOT=str(self.state_root / 'kubeasz'),
            K8S_DEPLOY_CLUSTERS_ROOT=str(self.state_root / 'clusters'),
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.org = Organization.objects.create(name='状态机测试组织', slug='fsm-test')
        self.artifacts_root = kubeasz_fixtures.write_kubeasz_tree(self.state_root / 'release')
        self.deployment = self._make_deployment('deploy_ha', 'fsm-ha-cluster')

    def _make_deployment(self, task_type, slug, **extra):
        """创建一条部署任务。

        参数：`task_type` 表示任务类型；`slug` 表示任务标识；`extra` 表示要覆盖的字段。
        返回：返回新建的 K8sDeployment。
        副作用：向数据库写入一条任务记录。
        """
        values = {
            'organization': self.org,
            'task_type': task_type,
            'status': 'queued',
            'display_name': slug,
            'slug': slug,
            'kubernetes_version': '1.29.4',
            'kubeasz_version': '3.6.7',
            'runtime': 'containerd',
            'os_type': 'Rocky Linux',
            'os_version': '8',
            'topology_mode': 'ha' if task_type == 'deploy_ha' else 'aio',
            'kube_proxy_mode': 'ipvs',
            'network_interface': 'eth0',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.96.0.0/12',
            'api_server_port': 6443,
            'cni_plugin': 'calico',
            'execution_mode': 'simulated',
            'queued_at': timezone.now(),
        }
        values.update(extra)
        return K8sDeployment.objects.create(**values)

    def add_nodes(self, count=3, roles=None, ip_prefix='192.168.10.'):
        """为当前任务追加若干节点快照。

        参数：`count` 表示节点数量；`roles` 表示每个节点的角色；`ip_prefix` 表示 IP 前缀。
        返回：返回节点列表。
        副作用：向数据库写入节点快照。
        """
        roles = roles or [['etcd', 'master']] * count
        nodes = []
        for index in range(count):
            nodes.append(K8sDeploymentNode.objects.create(
                organization=self.org,
                deployment=self.deployment,
                name=f'node-{index + 1}',
                ip_address=f'{ip_prefix}{index + 10}',
                ssh_port=22,
                ssh_user='root',
                roles=roles[index],
            ))
        return nodes

    def _patches(self, behaviors=None, stage_delay=0.0):
        """把「平台之外」的依赖全部打桩。

        参数：`behaviors` 表示 FakeRunner 的阶段行为；`stage_delay` 表示每阶段模拟耗时。
        返回：返回已启动的补丁列表。
        副作用：替换制品下载、SSH 免密、健康探测与镜像分发四处外部依赖。

        - ``_download_release``：不访问 OSS，直接返回本地合成制品；
        - ``_prepare_credentials``：不连目标机写公钥；
        - ``_run_preflight``：不连目标机做环境检查（预检本身在 ``tests.py`` 里单独覆盖）；
        - ``_publish_images``：不起 registry 容器、不下载镜像 tar（在 ``test_registry.py`` 里覆盖）；
        - ``node_path.apply_node``：不连目标机改 ``/etc/profile.d`` 与 ``/etc/environment``
          （命令本身在 ``test_node_path.py`` 里覆盖），接线只在 ``NodePathWiringTest`` 里断言调用；
        - ``node_hostname.apply_node`` / ``verify_node``：不连目标机改系统主机名
          （命令本身在 ``test_node_hostname.py`` 里覆盖），接线只在 ``NodeHostnameWiringTest``
          里断言调用；
        - ``node_kubeconfig.ensure_node``：不连目标机补写 ``/root/.kube/config``
          （命令本身在 ``test_node_kubeconfig.py`` 里覆盖），接线只在 ``NodeKubeconfigWiringTest``
          里断言调用；
        - ``node_helm.install_node``：不连目标机传 helm 客户端（命令本身在 ``test_node_helm.py``
          里覆盖），接线只在 ``NodeHelmWiringTest`` 里断言调用；
        - ``node_l4lb.ensure_nonlocal_bind``：不连目标机开内核开关（同机的机器上才会走到，
          命令本身在 ``test_node_l4lb.py`` 里覆盖），接线只在 ``SharedLbNodePrepTest`` 里断言调用；
        - ``node_containers.cleanup_node``：不连目标机删已退出的容器（命令的拼装与解析在
          ``test_node_containers.py`` 里覆盖），接线只在 ``NodeContainerCleanupWiringTest`` 里断言调用；
        - ``register.run_health_check``：不请求 Kubernetes API；
        - ``platform_relay``：不改平台机的网络配置、不连目标机开隧道（在 ``test_platform_relay.py``
          里单独覆盖），接线只在 ``RelayWiringTest`` 里断言调用。
        """
        patches = [
            mock.patch.object(state_machine, '_download_release', return_value=self.artifacts_root),
            mock.patch.object(state_machine, '_prepare_credentials', return_value=None),
            mock.patch.object(
                state_machine, '_run_preflight',
                return_value={'ok': True, 'checks': [], 'hard_failures': []},
            ),
            mock.patch.object(
                state_machine, '_publish_images',
                return_value={'pushed': 0, 'plan': []},
            ),
            mock.patch.object(
                node_path, 'apply_node',
                return_value={'profile': True, 'environment': True},
            ),
            mock.patch.object(
                node_hostname, 'apply_node',
                return_value={'label': 'node', 'previous': 'x', 'name': 'n', 'changed': True, 'hosts': 'present'},
            ),
            mock.patch.object(node_hostname, 'verify_node', return_value=('master-01', '')),
            mock.patch.object(
                node_kubeconfig, 'ensure_node',
                return_value={'label': 'node', 'action': 'kept', 'bytes': 0},
            ),
            mock.patch.object(
                node_helm, 'install_node',
                return_value={'label': 'node', 'action': 'installed', 'version': 'v3.14.0', 'bytes': 1},
            ),
            mock.patch.object(
                node_containers, 'cleanup_node',
                return_value={'label': 'node', 'found': 0, 'removed': 0, 'error': ''},
            ),
            mock.patch.object(
                node_l4lb, 'ensure_nonlocal_bind',
                return_value={'label': 'node', 'changed': False, 'value': '1'},
            ),
            mock.patch.object(
                register, 'run_health_check',
                return_value={'ok': True, 'message': '集群连通性检查通过', 'detail': {}},
            ),
            mock.patch.object(platform_relay, 'ensure', return_value={}),
            mock.patch.object(platform_relay, 'ensure_from_inventory', return_value={}),
            mock.patch.object(platform_relay, 'ensure_lb_vip', return_value={}),
            mock.patch.object(platform_relay, 'teardown', return_value=None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.fake_runner = runner.FakeRunner(behaviors=behaviors, stage_delay=stage_delay)
        return patches

    def execute(self, behaviors=None, stage_delay=0.0):
        """在打桩前提下执行当前任务。

        参数：`behaviors` 表示阶段行为；`stage_delay` 表示每阶段模拟耗时。
        返回：返回执行后的 K8sDeployment。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self._patches(behaviors=behaviors, stage_delay=stage_delay)
        return state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )


class ResolveClusterAddressTest(StateMachineTestBase):
    """``resolve_cluster_address``：地址探测的结果怎么落地、要平台补哪一段链路。

    探测本身在 ``test_node_address.py`` 里覆盖，转发通道在 ``test_platform_relay.py`` 里覆盖，
    这里只关心接线：写回快照、把「平台机到不了这个地址」这件事交给转发通道处理，以及探测不出
    结果时保持原样。
    """

    def setUp(self):
        """准备一台节点与一个只记调用的假上下文。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入一条节点快照。
        """
        super().setUp()
        self.node = self.add_nodes(count=1, roles=[['master', 'node']])[0]
        self.context = mock.MagicMock()

    def resolve(self, outcome, credential=None):
        """在打桩的前提下跑一次地址解析。

        参数：`outcome` 表示 ``node_address.resolve`` 的返回值；`credential` 表示该节点的连接信息。
        返回：返回被替换掉的 ``platform_relay.ensure`` 替身。
        副作用：可能更新节点快照并调用上下文记日志。
        """
        with mock.patch.object(node_address, 'resolve', return_value=outcome), \
                mock.patch.object(platform_relay, 'ensure', return_value={}) as relay:
            state_machine.resolve_cluster_address(self.context, self.node, credential or {'port': 22})
        return relay

    def nat_outcome(self, address='172.30.0.7', source=node_address.SOURCE_PROBE):
        """返回一份「探测到网卡地址」的探测结果。

        参数：`address` 表示探测到的地址；`source` 表示结果来源。
        返回：返回 ``node_address.resolve`` 形状的字典。
        副作用：只做纯计算。
        """
        return {'address': address, 'source': source, 'route_source': address, 'addresses': [address]}

    def relay_target(self, relay):
        """取出传给转发通道的那个目标。

        参数：`relay` 表示 ``platform_relay.ensure`` 的替身。
        返回：返回 ``{'address','host','user','port'}``。
        副作用：纯计算。
        """
        self.assertEqual(len(relay.call_args.args), 2)
        targets = relay.call_args.args[1]
        self.assertEqual(len(targets), 1)
        return targets[0]

    def test_interface_address_is_written_back_to_the_snapshot(self):
        """探测到的网卡地址要落库：编译 inventory 时读的就是它，不能只活在内存里。"""
        self.resolve(self.nat_outcome())
        self.node.refresh_from_db()
        self.assertEqual(self.node.cluster_ip, '172.30.0.7')

    def test_both_addresses_are_named_in_the_log(self):
        """日志要同时给出两个地址，操作员才知道 SSH 走的还是他填的那个。"""
        self.resolve(self.nat_outcome())
        text = ' '.join(str(call.args[1]) for call in self.context.log.call_args_list)
        self.assertIn('172.30.0.7', text)
        self.assertIn(str(self.node.ip_address), text)

    def test_relay_is_asked_for_the_interface_address(self):
        """两个地址不同时把「集群地址 → 填写地址」交给转发通道：平台机到不了前者，
        而 kubeasz 有一批 kubectl 是在平台机上跑的、打的正是它。"""
        relay = self.resolve(self.nat_outcome())
        self.assertEqual(self.relay_target(relay), {
            'address': '172.30.0.7',
            'host': str(self.node.ip_address),
            'user': 'root',
            'port': 22,
        })

    def test_relay_uses_the_credential_port_and_user(self):
        """隧道走的是平台连节点的那条路，端口与用户名取凭据里的，不是想当然的 22/root。"""
        relay = self.resolve(self.nat_outcome(), credential={'port': 2222, 'user': 'ops'})
        target = self.relay_target(relay)
        self.assertEqual((target['user'], target['port']), ('ops', 2222))

    def test_same_address_is_left_alone(self):
        """填写的地址就在网卡上时什么都不做：这是最常见的情况，不该多出日志、写库与隧道。"""
        outcome = self.nat_outcome(address=str(self.node.ip_address), source=node_address.SOURCE_ENTERED)
        relay = self.resolve(outcome)
        self.node.refresh_from_db()
        self.assertIsNone(self.node.cluster_ip)
        self.context.log.assert_not_called()
        relay.assert_not_called()

    def test_probe_failure_warns_but_keeps_the_entered_address(self):
        """探测不出结果时保持原样，只提醒一句：节点在 NAT 后面时 etcd 会绑不上地址。

        这种情况不需要转发通道 —— 用的就是操作员填的地址，平台 SSH 得到它。
        """
        outcome = {
            'address': str(self.node.ip_address),
            'source': node_address.SOURCE_FALLBACK,
            'route_source': '',
            'addresses': [],
        }
        relay = self.resolve(outcome)
        self.node.refresh_from_db()
        self.assertIsNone(self.node.cluster_ip)
        self.assertEqual([call.args[0] for call in self.context.log.call_args_list], ['stderr'])
        self.assertIn('etcd', self.context.log.call_args.args[1])
        relay.assert_not_called()

    def test_missing_address_is_ignored(self):
        """连填写的地址都取不到时直接返回，不写库也不记日志。"""
        relay = self.resolve({'address': '', 'source': '', 'route_source': '', 'addresses': []})
        self.node.refresh_from_db()
        self.assertIsNone(self.node.cluster_ip)
        self.context.log.assert_not_called()
        relay.assert_not_called()


class PrepareCredentialsWiringTest(StateMachineTestBase):
    """``_prepare_credentials``：免密登录验通之后才去确定集群内部地址。"""

    def test_address_resolution_runs_after_every_node_logs_in(self):
        """每一台验通免密的节点都要探测一次地址。"""
        nodes = self.add_nodes(count=2)
        with mock.patch.object(credentials, 'ensure_key_login', return_value={'ok': True, 'message': '免密可用'}), \
                mock.patch.object(state_machine, 'resolve_cluster_address') as resolver:
            state_machine._prepare_credentials(mock.MagicMock(), nodes, {})
        self.assertEqual(resolver.call_count, 2)

    def test_failed_key_login_stops_before_touching_addresses(self):
        """登录都进不去就不用谈集群地址了，直接以凭据失败结束。"""
        nodes = self.add_nodes(count=2)
        with mock.patch.object(credentials, 'ensure_key_login', return_value={'ok': False, 'message': '登录被拒绝'}), \
                mock.patch.object(state_machine, 'resolve_cluster_address') as resolver:
            with self.assertRaises(state_machine.TaskAborted):
                state_machine._prepare_credentials(mock.MagicMock(), nodes, {})
        resolver.assert_not_called()


class RelayWiringTest(StateMachineTestBase):
    """转发通道的接线：每次 ezctl 之前按 inventory 就位，任务收尾一律拆掉。"""

    def test_relay_is_ensured_from_inventory_before_every_ezctl_call(self):
        """每个 ezctl 阶段之前都要按 inventory 确认通道：那批 ``connection: local`` 的 kubectl
        就挂在这些阶段里，而扩容/升级用的节点地址只存在于 hosts 文件。"""
        self.add_nodes(3)
        self._patches()
        keys = [spec['key'] for spec in runner.stage_specs_for(self.deployment.task_type, self.deployment)]
        stages = []
        # 只统计 ezctl 阶段：平台自己插进去的那几步（打 ingress 标签等）不是 ezctl 调用，
        # 它们直接沿用上一个 ezctl 阶段建立的通道，因此不各自去 ensure 一次。
        self.fake_runner.on_stage = lambda stage, argv: stages.append(stage) if stage in keys else None
        with mock.patch.object(platform_relay, 'ensure_from_inventory', return_value={}) as ensure:
            state_machine.execute_task(self.deployment, worker_id='w', runner_instance=self.fake_runner)
        self.assertEqual(stages, keys)
        self.assertEqual(ensure.call_count, len(keys))
        path = Path(str(ensure.call_args.args[1]))
        # 分平台比较路径片段：Windows 开发机上分隔符与 POSIX 不同。
        self.assertEqual(path.parts[-3:], ('clusters', 'fsm-ha-cluster', 'hosts'))

    def test_relay_is_torn_down_even_when_the_task_fails(self):
        """失败也要拆：平台机上留着集群地址，之后对它的访问会被悄悄改道进隧道。"""
        self.add_nodes(3)
        self._patches(behaviors={'04.kube-master': {'returncode': 1}})
        with mock.patch.object(platform_relay, 'teardown') as teardown:
            state_machine.execute_task(self.deployment, worker_id='w', runner_instance=self.fake_runner)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        teardown.assert_called_once_with()

    def test_worker_start_clears_addresses_left_by_a_previous_process(self):
        """通道活在进程里，进程没了只剩临时地址，所以每次领任务之前先按落盘清单清一次。"""
        # 这一条只看清理这一步，队列留空（否则 run_once 会顺手把一个任务跑完）。
        self.deployment.delete()
        with mock.patch.object(platform_relay, 'cleanup_process_leftovers', return_value=1) as cleanup:
            self.assertIsNone(state_machine.run_once(worker_id='test-worker'))
        cleanup.assert_called_once_with()


class RegistryResolutionWiringTest(StateMachineTestBase):
    """节点侧分发的接线：``01.prepare`` 一跑完就把 hosts 改回镜像宿主节点。

    那个 role 用 ``regexp`` 把 ``easzlab.io.local`` 整行替换成「节点看到的 SSH 源地址」，
    平台与节点隔着 NAT 时那是回不去的出口地址 —— 正是集群起不来的原因。改晚一点都不行：
    ``04.kube-master`` 就要拉第一个镜像了。
    """

    def execute_with_distribution(self, distribution):
        """在给定的分发结论下跑完一次任务，记录各阶段的先后。

        参数：`distribution` 表示 ``_publish_images`` 返回的分发结论。
        返回：返回被替换掉的 ``node_registry.align_resolution`` 替身。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self.add_nodes(3)
        self._patches()
        sequence = []
        self.fake_runner.on_stage = lambda stage, argv: sequence.append(stage)
        summary = {'pushed': 0, 'plan': [], 'distribution': distribution}
        with mock.patch.object(state_machine, '_publish_images', return_value=summary), \
                mock.patch.object(
                    node_registry, 'align_resolution',
                    side_effect=lambda *args, **kwargs: sequence.append('registry-dns'),
                ) as align:
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )
        self.sequence = sequence
        return align

    def test_alignment_runs_right_after_prepare(self):
        """改 hosts 必须紧跟在 01.prepare 之后，且早于真正开始拉镜像的阶段。"""
        self.execute_with_distribution({'mode': 'node', 'address': '172.30.0.8'})
        self.assertEqual(self.sequence.index('registry-dns'), self.sequence.index('01.prepare') + 1)
        self.assertLess(self.sequence.index('registry-dns'), self.sequence.index('04.kube-master'))

    def test_alignment_covers_every_node_and_the_host_address(self):
        """每一台节点都要改：漏一台，那台的 Pod 就还卡在原地。"""
        align = self.execute_with_distribution({'mode': 'node', 'address': '172.30.0.8'})
        align.assert_called_once()
        nodes, _credentials_map, address = align.call_args.args
        self.assertEqual(len(nodes), 3)
        self.assertEqual(address, '172.30.0.8')

    def test_platform_mode_never_touches_hosts(self):
        """走平台本机那条路时，01.prepare 写进去的地址本来就是对的，不必多此一举。"""
        align = self.execute_with_distribution({'mode': 'platform'})
        align.assert_not_called()
        self.assertNotIn('registry-dns', self.sequence)

    def test_summary_without_distribution_is_ignored(self):
        """摘要里没有分发结论时什么都不做，老的调用路径不受影响。"""
        align = self.execute_with_distribution(None)
        align.assert_not_called()

    def test_pool_comes_from_the_deployment_that_created_the_cluster(self):
        """宿主候选取自创建集群的那次部署：它的节点快照带内网地址，顺序也与当初挑宿主时一致。"""
        nodes = self.add_nodes(3, ip_prefix='192.168.10.')
        # 三台机器各给一个网卡地址（真实集群里它们必然不同）。以前这里三台都写成
        # 172.30.0.8 —— 那是 NAT 场景下随手取的样例值，但「同一个地址对应三台机器」在
        # inventory 里就是三台机器抢一个主机名，编译期的一致性校验（见
        # ``compiler._assert_node_names_consistent``）会正确地判它失败。
        for index, item in enumerate(nodes):
            item.cluster_ip = f'172.30.0.{8 + index}'
            item.save(update_fields=['cluster_ip'])
        self.execute()

        pool = state_machine._cluster_registry_pool(self.deployment)

        self.assertEqual([item.ip_address for item in pool], [item.ip_address for item in nodes])
        self.assertEqual(pool[0].cluster_ip, '172.30.0.8')

    def test_pool_is_absent_before_the_cluster_exists(self):
        """还没登记过配置版本（集群不存在）时返回 None：调用方退回用提交的节点清单。"""
        self.add_nodes(3)
        self.assertIsNone(state_machine._cluster_registry_pool(self.deployment))

    def test_alignment_failure_aborts_the_task(self):
        """改不动就停下来：继续跑下去只会在几十分钟后换来一堆看不懂的 ImagePullBackOff。"""
        self.add_nodes(3)
        self._patches()
        summary = {
            'pushed': 0, 'plan': [],
            'distribution': {'mode': 'node', 'address': '172.30.0.8'},
        }
        with mock.patch.object(state_machine, '_publish_images', return_value=summary), \
                mock.patch.object(
                    node_registry, 'align_resolution',
                    side_effect=node_registry.NodeRegistryError('hosts_align_failed', '改不动'),
                ):
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'hosts_align_failed')
        self.assertIn('改不动', self.deployment.failure_reason)


class AddonOverrideWiringTest(StateMachineTestBase):
    """附件模板修正的接线：制品一就绪就改，改不动也不拦任务。

    kubeasz 的 ingress-nginx 模板把 ``serviceMonitor``/``prometheusRule`` 写死成 true，
    而平台从不装 Prometheus Operator，于是 ``07.addons`` 的 ``helm upgrade --install``
    报 CRD 找不到、整段失败（94 上 1.34.1/kubeasz 3.6.8 的真实故障）。文本改写本身在
    ``test_addon_overrides`` 里逐条覆盖，这里只钉接线、时机与容错。
    """

    # 照抄上游模板的 metrics 段，只要够触发改写即可。
    TEMPLATE = """\
  metrics:
    enabled: {% if ingress_nginx_metrics_enabled %}true{% else %}false{% endif %}
    serviceMonitor:
      enabled: true
    prometheusRule:
      enabled: true
"""

    def write_template(self):
        """往合成制品里放一份 ingress values 模板。

        参数：无。
        返回：返回模板文件路径。
        副作用：在合成制品树里创建目录并写文件。
        """
        path = self.artifacts_root / addon_overrides.INGRESS_TEMPLATE_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.TEMPLATE, encoding='utf-8')
        return path

    def download_release(self, real, with_template=True):
        """用合成制品跑一次真实的 ``_download_release``。

        参数：`real` 表示**打桩之前**取到的 ``_download_release`` 真身 —— ``_patches`` 会把模块上
            那个名字换成替身，而这里要考的正是真身；`with_template` 表示制品里是否放那份模板。
        返回：返回 ``(返回的根目录, 模板路径或 None)``。
        副作用：写入阶段记录与日志，可能改写制品树里的模板。
        """
        template = self.write_template() if with_template else None
        context = state_machine.TaskContext(self.deployment, self.state_root / 'ws', worker_id='w')
        patchers = [
            mock.patch.object(artifacts, 'ensure_kubeasz_source', return_value=self.artifacts_root),
            mock.patch.object(state_machine, '_prepare_kubeasz_home', return_value={}),
        ]
        for item in patchers:
            item.start()
            self.addCleanup(item.stop)
        root = real(context, self.deployment, self.fake_runner)
        context.flush(force=True)
        return root, template

    def test_template_is_patched_while_preparing_the_source(self):
        """制品就绪即改写，且落在返回给后续步骤的那棵树上。"""
        real = state_machine._download_release
        self._patches()
        root, template = self.download_release(real)

        self.assertEqual(Path(root), self.artifacts_root)
        text = template.read_text(encoding='utf-8')
        self.assertEqual(text.count('enabled: false'), 2)
        self.assertIn('    serviceMonitor:\n      enabled: false\n', text)
        self.assertIn('    prometheusRule:\n      enabled: false\n', text)
        self.assertIn('ingress_nginx_metrics_enabled', text)

    def test_the_correction_is_logged_with_its_reason(self):
        """日志里说明改了什么、为什么改 —— 用户看部署日志就知道平台动过模板。"""
        real = state_machine._download_release
        self._patches()
        self.download_release(real)

        self.assertTrue(K8sDeploymentLog.objects.filter(
            deployment=self.deployment, message__contains='已修正 kubeasz 的 ingress-nginx 模板',
        ).exists())

    def test_a_missing_template_is_reported_too(self):
        """制品里没有这份模板时说一句，不留悬念。"""
        real = state_machine._download_release
        self._patches()
        self.download_release(real, with_template=False)

        self.assertTrue(K8sDeploymentLog.objects.filter(
            deployment=self.deployment, message__contains='未修正 kubeasz 的 ingress-nginx 模板',
        ).exists())

    def test_a_broken_patch_does_not_stop_the_deployment(self):
        """改写本身出错时只记一行，任务照常往下走：旁路动作不该决定成败。"""
        real = state_machine._download_release
        self._patches()
        patcher = mock.patch.object(
            addon_overrides, 'apply_ingress_monitoring',
            side_effect=RuntimeError('正则写炸了'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.download_release(real)

        self.assertTrue(K8sDeploymentLog.objects.filter(
            deployment=self.deployment, message__contains='修正 kubeasz 的 ingress-nginx 模板时出错',
        ).exists())

    def test_both_templates_are_reported_under_their_own_names(self):
        """两条修正在日志里各报各的：只说「模板」而不说哪一份，等于没说。"""
        real = state_machine._download_release
        self._patches()
        self.download_release(real, with_template=False)

        for label in ('ingress-nginx', 'l4lb'):
            with self.subTest(label=label):
                self.assertTrue(K8sDeploymentLog.objects.filter(
                    deployment=self.deployment,
                    message__contains=f'未修正 kubeasz 的 {label} 模板',
                ).exists())

    def test_the_l4lb_template_is_patched_in_the_same_pass(self):
        """l4lb 那份模板在同一个准备点被改成可配：它必须赶在 ``10.ex-lb`` 渲染之前就位，
        而所有任务都经过 ``_download_release``，放这里正好早于任何剧本。"""
        real = state_machine._download_release
        self._patches()
        path = self.artifacts_root / addon_overrides.L4LB_TEMPLATE_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'stream {\n    server {\n'
            '        listen 0.0.0.0:{{ EX_APISERVER_PORT }};\n'
            '    }\n}\n',
            encoding='utf-8',
        )

        self.download_release(real, with_template=False)

        self.assertIn(
            addon_overrides.L4LB_LISTEN_VARIABLE, path.read_text(encoding='utf-8'),
        )
        self.assertTrue(K8sDeploymentLog.objects.filter(
            deployment=self.deployment, message__contains='已修正 kubeasz 的 l4lb 模板',
        ).exists())

    def test_a_full_install_carries_the_correction(self):
        """走一次完整的安装流程（真实 ``_download_release``）：模板在 07 之前就被改好。"""
        self.add_nodes(3)
        real_download = state_machine._download_release
        self._patches()
        patcher = mock.patch.object(state_machine, '_download_release', side_effect=real_download)
        patcher.start()
        self.addCleanup(patcher.stop)
        template = self.write_template()
        patcher = mock.patch.object(artifacts, 'ensure_kubeasz_source', return_value=self.artifacts_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(state_machine, '_prepare_kubeasz_home', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

        state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'succeeded')
        self.assertEqual(template.read_text(encoding='utf-8').count('enabled: false'), 2)


class NodePathWiringTest(StateMachineTestBase):
    """系统 PATH 的接线：``01.prepare`` 一跑完就把 ``bin_dir`` 加进去。

    kubeasz 只把那一行写进连接用户的 ``~/.bashrc`` —— 别的用户、非交互式 SSH（运维最常用的
    ``ssh 节点 "kubectl get pod -A"``）都看不到，命令明明拷在机器上却报 command not found。
    时机也必须是 ``01.prepare`` 之后：那个 role 才把命令拷进 ``bin_dir``。
    """

    def execute_tracked(self):
        """跑完一次任务，并按顺序记下平台自己插进来的那几步。

        参数：无。
        返回：返回被替换掉的 ``node_path.apply_node`` 替身。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self.add_nodes(3)
        self._patches()
        # 基类那份没有句柄，这里再盖一层，只为断言「调了几次、传了哪个目录」。
        sequence = []
        self.fake_runner.on_stage = lambda stage, argv: sequence.append(stage)
        patcher = mock.patch.object(
            node_path, 'apply_node',
            side_effect=lambda *args, **kwargs: sequence.append('node-path') or {'profile': True},
        )
        apply_node = patcher.start()
        self.addCleanup(patcher.stop)
        state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )
        self.sequence = sequence
        return apply_node

    def test_path_is_added_right_after_prepare(self):
        """跟着 01.prepare 走：早了目录还是空的，晚了运维已经登录进去找过一遍命令了。"""
        self.execute_tracked()
        self.assertEqual(self.sequence.index('node-path'), self.sequence.index('01.prepare') + 1)
        self.assertLess(self.sequence.index('node-path'), self.sequence.index('04.kube-master'))

    def test_every_node_gets_the_directory(self):
        """每台节点都要加：漏一台，运维恰好登上那台就还是找不到命令。"""
        apply_node = self.execute_tracked()

        self.assertEqual(apply_node.call_count, 3)
        called_ips = {call.args[1].ip_address for call in apply_node.call_args_list}
        self.assertEqual(called_ips, {'192.168.10.10', '192.168.10.11', '192.168.10.12'})
        # 目录取自渲染后的 inventory（合成制品里写的就是上游默认值），不是写死的常量。
        self.assertEqual(apply_node.call_args.args[2], '/opt/kube/bin')

    def test_failure_does_not_fail_the_task(self):
        """写不进去只记警告：集群本身是好的，为一项便利配置把成功的部署报成失败更糟。"""
        self.add_nodes(3)
        self._patches()
        with mock.patch.object(
            node_path, 'apply_node',
            side_effect=node_path.NodePathError('node_path_failed', '两处都没写成'),
        ):
            task = state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )

        task.refresh_from_db()
        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=task).values_list('event_type', flat=True)
        )
        self.assertIn('node_path_failed', event_types)
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(deployment=task, stream='system').values_list('message', flat=True)
        )
        # 日志里要给出可照抄的手工命令，否则操作员只能自己猜该往哪个文件写什么。
        self.assertIn(node_path.PROFILE_PATH, messages)

    def test_directory_comes_from_the_rendered_inventory(self):
        """操作员把 bin_dir 改到数据盘上时，加进 PATH 的必须是改过的那一个。"""
        nodes = self.add_nodes(1)
        hosts = Path(self.state_root) / 'custom-hosts'
        hosts.write_text('[all:vars]\nbin_dir="/data/kube/bin"\n', encoding='utf-8')
        context = mock.MagicMock()

        with mock.patch.object(
            node_path, 'apply_node', return_value={'profile': True, 'environment': True},
        ) as apply_node:
            state_machine._configure_node_path(context, {'hosts_path': str(hosts)}, nodes, {})

        self.assertEqual(apply_node.call_args.args[2], '/data/kube/bin')


class NodeHelmWiringTest(StateMachineTestBase):
    """节点侧 Helm 客户端的接线：勾了「Helm」才在 ``01.prepare`` 之后把命令补到 ``bin_dir``。

    界面上的勾选写的是「启用后部署时一并准备 Helm 客户端」，而平台此前只拿它判过一次制品
    预检 —— 勾了、部署成功，登上 master 敲 ``helm list`` 仍是 ``command not found``。kubeasz
    那份「分发二进制」的清单里没有 helm，平台在同一个时机照它补一条（见 ``node_helm``）。

    时机、目录与范围三件事都在这里钉住：跟着 ``01.prepare``（那一步之后 ``bin_dir`` 才存在、
    PATH 才指向它）、目录取自渲染后的 inventory、**每台节点都要装**。
    """

    def execute_tracked(self, install_helm_cli=True, with_source=True, error=None):
        """跑完一次任务，并把节点侧那一步换成一个只记账的替身。

        参数：`install_helm_cli` 表示向导里那个「Helm」勾选；`with_source` 表示控制机上有没有
            那份 helm（``<kubeasz 目录>/bin/helm``）；`error` 表示让替身抛出的异常。
        返回：返回 ``node_helm.install_node`` 的替身。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self.add_nodes(3)
        self.deployment.install_helm_cli = install_helm_cli
        self.deployment.save(update_fields=['install_helm_cli', 'updated_at'])
        # 载体镜像解出来的那份 helm 就落在 kubeasz 目录的 bin 下（与 kubelet、kubectl 同一处），
        # 这里按同一个位置放一个 —— 平台就是从这里把它发给节点的。
        self.helm_source = Path(self.artifacts_root) / 'bin' / 'helm'
        if with_source:
            self.helm_source.parent.mkdir(parents=True, exist_ok=True)
            self.helm_source.write_bytes(b'#!/bin/sh\necho helm\n')
        self._patches()
        sequence = []
        self.fake_runner.on_stage = lambda stage, argv: sequence.append(stage)
        side_effect = error or (lambda *args, **kwargs: sequence.append('node-helm') or {
            'label': 'x', 'action': 'installed', 'version': 'v3.14.0', 'bytes': 1,
        })
        patcher = mock.patch.object(node_helm, 'install_node', side_effect=side_effect)
        install_node = patcher.start()
        self.addCleanup(patcher.stop)
        self.task = state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )
        self.sequence = sequence
        return install_node

    def event_types(self):
        """返回本次任务的事件类型集合。"""
        return set(
            K8sDeploymentEvent.objects.filter(deployment=self.task).values_list('event_type', flat=True)
        )

    def test_helm_is_installed_right_after_prepare(self):
        """跟着 01.prepare 走：那一步之后 bin_dir 才存在、PATH 才指向它。"""
        self.execute_tracked()

        self.assertEqual(self.sequence.index('node-helm'), self.sequence.index('01.prepare') + 1)
        self.assertLess(self.sequence.index('node-helm'), self.sequence.index('04.kube-master'))

    def test_every_node_gets_the_client(self):
        """每台节点都要装：漏一台，运维恰好登上那台敲 helm 就还是找不到命令（同 kubectl 的范围）。"""
        install_node = self.execute_tracked()

        self.assertEqual(install_node.call_count, 3)
        self.assertEqual(
            {call.args[1].ip_address for call in install_node.call_args_list},
            {'192.168.10.10', '192.168.10.11', '192.168.10.12'},
        )
        # 目录取自渲染后的 inventory（合成制品里写的就是上游默认值）。
        self.assertEqual(install_node.call_args.args[2], '/opt/kube/bin')
        self.assertIn('node_helm_installed', self.event_types())

    def test_directory_comes_from_the_rendered_inventory(self):
        """操作员把 bin_dir 改到数据盘上时，装进去的必须是改过的那一个 —— 那才在 PATH 里。"""
        nodes = self.add_nodes(1)
        hosts = Path(self.state_root) / 'custom-helm-hosts'
        hosts.write_text('[all:vars]\nbin_dir="/data/kube/bin"\n', encoding='utf-8')
        context = mock.MagicMock()
        source = Path(self.state_root) / 'helm'

        with mock.patch.object(
            node_helm, 'local_binary', return_value=source,
        ), mock.patch.object(
            node_helm, 'install_node',
            return_value={'label': 'x', 'action': 'kept', 'version': 'v3.14.0', 'bytes': 1},
        ) as install_node:
            state_machine._configure_node_helm(
                context, self.artifacts_root, {'hosts_path': str(hosts)}, nodes, {},
            )

        self.assertEqual(install_node.call_args.args[2], '/data/kube/bin')

    def test_unchecked_means_no_helm_step(self):
        """没勾「Helm」的集群不该多出这个命令 —— 否则界面上的勾选不但没实现，还反了。"""
        install_node = self.execute_tracked(install_helm_cli=False)

        install_node.assert_not_called()
        self.assertNotIn('node-helm', self.sequence)

    def test_failure_does_not_fail_the_task(self):
        """单台装不上只记警告：这是给人用的客户端，集群本身是好的。"""
        self.execute_tracked(
            error=node_helm.NodeHelmError('node_helm_verify_failed', '跑不起来'),
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'succeeded', self.task.failure_reason)
        event = K8sDeploymentEvent.objects.filter(
            deployment=self.task, event_type='node_helm_failed',
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.level, 'warn')
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(
                deployment=self.task, stream='system',
            ).values_list('message', flat=True)
        )
        # 日志里要给出可照抄的落点，否则操作员只能自己猜该把文件放到哪儿。
        self.assertIn('/opt/kube/bin/helm', messages)

    def test_a_missing_source_on_the_control_machine_is_reported(self):
        """控制机上没有那份 helm 时如实说一句：不装、也不连机器，任务照常成功。"""
        install_node = self.execute_tracked(with_source=False)

        install_node.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'succeeded', self.task.failure_reason)
        event = K8sDeploymentEvent.objects.filter(
            deployment=self.task, event_type='node_helm_skipped',
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.level, 'warn')


# 一份形状完整的 kubeconfig，供节点侧 kubectl 配置的接线用例使用（新建集群与扩容两条路）。
# 接线用例只关心它被原样传下去，但平台在登记阶段会真的解析它
# （``register.backfill_kubeconfig`` 要 ``clusters[].cluster.server``），所以 server 不能少。
# 里面那几段证书数据是**假值**，只用来钉住「内容不进日志」。
NODE_KUBECONFIG_TEXT = (
    'apiVersion: v1\n'
    'clusters:\n'
    '- cluster:\n'
    '    certificate-authority-data: SECRET-CA-DATA\n'
    '    server: https://192.168.10.10:6443\n'
    '  name: fsm-ha-cluster\n'
    'contexts:\n'
    '- context:\n'
    '    cluster: fsm-ha-cluster\n'
    '    user: kubernetes-admin\n'
    '  name: context-fsm-ha-cluster\n'
    'current-context: context-fsm-ha-cluster\n'
    'kind: Config\n'
    'users:\n'
    '- name: kubernetes-admin\n'
    '  user:\n'
    '    client-certificate-data: SECRET-CLIENT-CERT\n'
    '    client-key-data: SECRET-CLIENT-KEY-DATA\n'
)


class NodeKubeconfigWiringTest(StateMachineTestBase):
    """节点侧 kubectl 配置的接线：``01.prepare`` 一跑完就补一份 ``/root/.kube/config``。

    kubeasz 的 ``roles/prepare`` 自己会拷这份配置，但它带幂等标记
    （``/opt/kubeasz_prepare_tasks``）：标记在、角色整段跳过时这一步就没了。而标记与
    ``/root/.kube`` 都会被「清理上一次安装的残留」碰到，两边不对称就会留下半干净的机器 ——
    集群装得好好的，人登上 master 敲 ``kubectl`` 只会连 ``localhost:8080`` 报连接被拒
    （2026-10-12 的 JD-k8s，114.67.203.82）。标注与命令本身在 ``test_node_kubeconfig.py``
    里覆盖，这里只钉接线：时机、覆盖每台节点、取自 kubeasz 生成的那份、失败只记警告。
    """

    def execute_tracked(self, text=NODE_KUBECONFIG_TEXT, action='kept', error=None):
        """跑完一次任务，并按顺序记下平台自己插进来的那几步。

        参数：`text` 表示「平台上那份 kubeconfig」的内容（空串表示还没生成出来）；
            `action` 表示 ``node_kubeconfig.ensure_node`` 的报告结论；
            `error` 表示让它抛出的异常（``None`` 表示正常返回）。
        返回：返回被替换掉的 ``node_kubeconfig.ensure_node`` 替身。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self.add_nodes(3)
        self._patches()
        sequence = []
        self.fake_runner.on_stage = lambda stage, argv: sequence.append(stage)
        path_patch = mock.patch.object(
            node_path, 'apply_node',
            side_effect=lambda *args, **kwargs: sequence.append('node-path') or {'profile': True},
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)
        # 合成制品里没有 clusters/<slug>/kubectl.kubeconfig（那是 kubeasz 跑完 01.prepare
        # 才生成的），所以这里喂一份；真实读取路径由 register 自己的用例覆盖。
        source = mock.patch.object(register, 'read_generated_kubeconfig', return_value=text)
        source.start()
        self.addCleanup(source.stop)

        def fake_ensure(*args, **kwargs):
            """记录调用顺序，并按预设给出报告或抛错。"""
            sequence.append('node-kubeconfig')
            if error is not None:
                raise error
            return {'label': args[1].ip_address, 'action': action, 'bytes': 4}

        patcher = mock.patch.object(node_kubeconfig, 'ensure_node', side_effect=fake_ensure)
        ensure_node = patcher.start()
        self.addCleanup(patcher.stop)
        state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )
        self.sequence = sequence
        return ensure_node

    def test_config_is_written_right_after_path(self):
        """时机紧接着 PATH 那一步：两者都是「prepare 被跳过」的后果，同一处兜底最好读。

        两步都是逐台跑，所以顺序里各自连着一串（每台一次）；要钉的是**两串挨着**、
        且都在 ``04.kube-master`` 之前 —— 那一步就要拉第一个镜像了。
        """
        self.execute_tracked()
        count = self.sequence.count('node-path')

        self.assertGreater(count, 1)
        self.assertEqual(
            self.sequence[1:1 + 2 * count],
            ['node-path'] * count + ['node-kubeconfig'] * count,
        )
        self.assertLess(self.sequence.index('node-kubeconfig'), self.sequence.index('04.kube-master'))

    def test_every_node_gets_the_config(self):
        """每台节点都要补：漏一台，运维恰好登上那台就还是用不了 kubectl。"""
        ensure_node = self.execute_tracked()

        self.assertEqual(ensure_node.call_count, 3)
        called_ips = {call.args[1].ip_address for call in ensure_node.call_args_list}
        self.assertEqual(called_ips, {'192.168.10.10', '192.168.10.11', '192.168.10.12'})
        # 内容取的是 kubeasz 生成的那一份（与 prepare 会拷的是同一个文件），不是平台自己那份。
        self.assertEqual(ensure_node.call_args.args[2], NODE_KUBECONFIG_TEXT)

    def test_backfilled_nodes_are_reported_as_a_warning(self):
        """只有「真的补了」才报事件：本来就有一份的机器不该在界面上刷出一条警告。"""
        self.execute_tracked(action='written')

        event = K8sDeploymentEvent.objects.filter(
            deployment=self.deployment, event_type='node_kubeconfig_written',
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.level, 'warn')
        self.assertIn('192.168.10.10', event.message)

    def test_keeping_an_existing_config_is_silent(self):
        """全都保持原样时不报事件 —— 这是常态（prepare 正常跑过的机器）。"""
        self.execute_tracked(action='kept')

        self.assertFalse(
            K8sDeploymentEvent.objects.filter(
                deployment=self.deployment, event_type='node_kubeconfig_written',
            ).exists()
        )

    def test_failure_does_not_fail_the_task(self):
        """补不进去只记警告：集群本身是好的，日志里要给出可照抄的手工做法。"""
        self.execute_tracked(error=node_kubeconfig.NodeKubeconfigError(
            'node_kubeconfig_failed', '向节点写入 /root/.kube/config 失败',
        ))
        task = self.deployment
        task.refresh_from_db()

        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(deployment=task, stream='system').values_list('message', flat=True)
        )
        self.assertIn(node_kubeconfig.KUBECONFIG_PATH, messages)
        self.assertIn('手工补上', messages)

    def test_skips_when_the_platform_has_no_kubeconfig(self):
        """平台上还没有这份文件时整个跳过：那说明 01.prepare 没生成出来，属于上游已经出错。"""
        ensure_node = self.execute_tracked(text='')

        self.assertNotIn('node-kubeconfig', self.sequence)
        ensure_node.assert_not_called()
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(
                deployment=self.deployment, stream='system',
            ).values_list('message', flat=True)
        )
        self.assertIn('跳过节点侧补写', messages)

    def test_content_never_lands_in_the_logs(self):
        """kubeconfig 里带着 admin 客户端证书与私钥，**一个字节都不能进日志**。"""
        needle = 'SECRET-CLIENT-KEY-DATA'
        self.assertIn(needle, NODE_KUBECONFIG_TEXT)
        self.execute_tracked()

        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(
                deployment=self.deployment, stream='system',
            ).values_list('message', flat=True)
        )
        self.assertNotIn(needle, messages)


class NodeHostnameWiringTest(StateMachineTestBase):
    """主机名的接线：安装开始之前统一，``01.prepare`` 之后再复核一次。

    节点名是 kubelet 注册到集群的名字，主机名是操作系统自己的名字。两者不一致时，登进机器的
    和一个 ``kubectl get node`` 看到的是两台「不同」的机器 —— 这正是要修掉的毛病，所以改名的
    时机必须在 kubelet 起来之前，且失败即中止（此刻还没装任何东西，重试代价为零）。
    """

    def execute_tracked(self, apply_error=None):
        """跑完一次任务，并按顺序记下平台自己插进来的那几步。

        参数：`apply_error` 表示改名时要抛出的异常，None 表示成功。
        返回：返回 ``(改名替身, 复核替身, 顺序)``。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self.add_nodes(3)
        self._patches()
        sequence = []
        self.fake_runner.on_stage = lambda stage, argv: sequence.append(stage)
        # 「写下去的名字」记一份，复核时按它回答：复核返回别的名字会被当成上游改写，
        # 于是触发一次写回、把调用次数算乱 —— 那是另一件事，由
        # ``test_prepare_is_followed_by_a_read_only_recheck`` 之外的行为覆盖。
        assigned = {}

        def fake_apply(_context, node, name, *_args, **_kwargs):
            """记下这台机器被改成了什么名字。"""
            assigned[str(node.ip_address)] = name
            sequence.append('node-hostname')
            return {'label': 'n', 'previous': 'x', 'name': name, 'changed': True, 'hosts': 'present'}

        patcher = mock.patch.object(
            node_hostname, 'apply_node', side_effect=apply_error or fake_apply,
        )
        apply_node = patcher.start()
        self.addCleanup(patcher.stop)
        verify_patcher = mock.patch.object(
            node_hostname, 'verify_node',
            side_effect=lambda node, *args, **kwargs: sequence.append('node-hostname-verify')
            or (assigned.get(str(getattr(node, 'ip_address', '')), ''), ''),
        )
        verify_node = verify_patcher.start()
        self.addCleanup(verify_patcher.stop)
        state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )
        self.sequence = sequence
        return apply_node, verify_node, sequence

    def test_names_are_set_before_the_cluster_is_installed(self):
        """改名赶在所有集群阶段之前：``01.prepare`` 一开始就往节点上铺东西，装完再改就得等重启。

        ``sequence`` 里只有平台自己插进来的动作与执行器的阶段，因此第一个元素就该是改名 ——
        预检、下载制品、推镜像都不会在节点上留下需要重启才能生效的东西。
        """
        _apply, _verify, sequence = self.execute_tracked()

        self.assertEqual(sequence[0], 'node-hostname')
        self.assertLess(sequence.index('node-hostname'), sequence.index('01.prepare'))

    def test_every_named_node_gets_its_name(self):
        """平台自己算出来的名字逐台落到机器上 —— 与 inventory 里的 ``k8s_nodename`` 同源。"""
        apply_node, _verify, _sequence = self.execute_tracked()

        self.assertEqual(apply_node.call_count, 3)
        names = {call.args[1].ip_address: call.args[2] for call in apply_node.call_args_list}
        self.assertEqual(names, {
            '192.168.10.10': 'master-01',
            '192.168.10.11': 'master-02',
            '192.168.10.12': 'master-03',
        })

    def test_prepare_is_followed_by_a_read_only_recheck(self):
        """``01.prepare`` 之后复核一次：上游的 prepare 角色带主机名开关，万一它改写了一遍要能发现。"""
        _apply, verify_node, sequence = self.execute_tracked()

        self.assertGreaterEqual(verify_node.call_count, 1)
        self.assertGreater(sequence.index('node-hostname-verify'), sequence.index('01.prepare'))

    def test_a_node_that_cannot_be_renamed_aborts_the_task(self):
        """改不动就中止：静默放过等于让「自动命名/手动命名」只是配置里的一行字。"""
        self.add_nodes(3)
        self._patches()
        error = node_hostname.NodeHostnameError('node_hostname_failed', '节点 192.168.10.10 的主机名与节点名不一致')
        ran = []
        self.fake_runner.on_stage = lambda stage, argv: ran.append(stage)
        with mock.patch.object(node_hostname, 'apply_node', side_effect=error):
            task = state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )

        task.refresh_from_db()
        self.assertEqual(task.status, 'failed')
        self.assertIn('主机名', task.failure_reason)
        # 中止发生在安装开始之前：集群阶段一步都没跑，机器上没留下半成品。
        self.assertEqual(ran, [])


class NodeContainerCleanupWiringTest(StateMachineTestBase):
    """已退出容器的清理接线：集群起来之后清一遍。

    calico-node 的 ``install-cni`` / ``mount-bpffs`` 这类 initContainer 跑完就退，容器对象
    却被 containerd 一直留着 —— ``crictl ps -a`` 里永远挂着几行 ``Exited``，占着可写层和日志
    目录。时机必须是集群起来之后：那时候该起来的 Pod 已经起来，删的才是真残留。
    """

    RESULT = {'label': 'node', 'found': 1, 'removed': 1, 'error': ''}

    def execute_tracked(self, result=None, error=None):
        """跑完一次任务，并按顺序记下健康检查与清理两步。

        参数：`result` 表示清理的返回报告；`error` 表示清理时抛出的异常。
        返回：返回 ``(清理替身, 顺序)``。
        副作用：完整推进一次任务并写日志、事件与集群记录。
        """
        self.add_nodes(3)
        self._patches()
        sequence = []
        # 基类已经打过 run_health_check，这里盖一层只为在顺序里留个记号。
        health = mock.patch.object(
            register, 'run_health_check',
            side_effect=lambda *args, **kwargs: sequence.append('health') or {
                'ok': True, 'message': '集群连通性检查通过', 'detail': {},
            },
        )
        health.start()
        self.addCleanup(health.stop)
        side_effect = error if error is not None else (
            lambda *args, **kwargs: sequence.append('cleanup') or (result or self.RESULT)
        )
        patcher = mock.patch.object(node_containers, 'cleanup_node', side_effect=side_effect)
        cleanup = patcher.start()
        self.addCleanup(patcher.stop)
        state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )
        self.sequence = sequence
        return cleanup

    def test_runs_after_every_install_stage(self):
        """01~07 全跑完才打扫：早一步 Pod 还没起全，晚退出的那几个就漏掉了。"""
        self.execute_tracked()

        self.deployment.refresh_from_db()
        stages = list(self.deployment.completed_stages or [])
        self.assertIn('cleanup-containers', stages)
        self.assertLess(stages.index('07.addons'), stages.index('cleanup-containers'))

    def test_stays_out_of_the_current_stage(self):
        """打扫不是阶段条上的一步：任务成功时 ``current_stage`` 仍应是「登记集群」。"""
        self.execute_tracked()

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.current_stage, 'register')

    def test_every_node_is_cleaned_with_the_inventory_bin_dir(self):
        """每台节点都清，``crictl`` 从 inventory 的 ``bin_dir`` 取。"""
        cleanup = self.execute_tracked()

        self.assertEqual(cleanup.call_count, 3)
        called_ips = {call.args[1].ip_address for call in cleanup.call_args_list}
        self.assertEqual(called_ips, {'192.168.10.10', '192.168.10.11', '192.168.10.12'})
        self.assertEqual(cleanup.call_args.args[2], '/opt/kube/bin')

    def test_failure_does_not_fail_the_task(self):
        """清不掉只记警告：这是收尾打扫，为它把一次成功的部署报成失败更糟。"""
        self.execute_tracked(result={
            'label': 'node', 'found': 2, 'removed': 0, 'error': '列不出容器：command not found',
        })

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'succeeded', self.deployment.failure_reason)
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=self.deployment)
            .values_list('event_type', flat=True)
        )
        self.assertIn('node_container_cleanup_failed', event_types)
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(deployment=self.deployment, stream='system')
            .values_list('message', flat=True)
        )
        # 日志里要给出可照抄的手工命令，否则操作员只能自己去查 crictl 的用法。
        self.assertIn('crictl rm --keep-logs', messages)

    def test_connection_failure_does_not_fail_the_task(self):
        """连不上也一样：节点上本来就可能有运维在重启 sshd，那不是部署失败。"""
        self.execute_tracked(
            error=node_registry.NodeRegistryError('ssh_failed', '连接超时'),
        )

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'succeeded', self.deployment.failure_reason)
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=self.deployment)
            .values_list('event_type', flat=True)
        )
        self.assertIn('node_container_cleanup_failed', event_types)

    def test_directory_comes_from_the_rendered_inventory(self):
        """操作员把 ``bin_dir`` 改到数据盘上时，要找的必须是改过的那一个 ``crictl``。"""
        hosts = Path(self.state_root) / 'custom-hosts'
        hosts.write_text('[all:vars]\nbin_dir="/data/kube/bin"\n', encoding='utf-8')
        node = mock.MagicMock()
        node.ip_address = '10.0.0.1'
        context = mock.MagicMock()

        with mock.patch.object(node_containers, 'cleanup_node', return_value=self.RESULT) as cleanup:
            state_machine._cleanup_node_containers(context, {'hosts_path': str(hosts)}, [node], {})

        self.assertEqual(cleanup.call_args.args[2], '/data/kube/bin')
        context.begin_stage.assert_called_once()
        self.assertEqual(context.begin_stage.call_args.args[0], 'cleanup-containers')


class StaleCleanupWiringTest(StateMachineTestBase):
    """残留清理的接线：预检说要清才清，清不掉就停在清理这一步。

    这一环的由来是 94 上 k8s-111 那次失败在 ``02.etcd``：机器上还留着上一个集群的 etcd 数据，
    新集群的 etcd 被它接管后 ``invalid downgrade`` 退出。预检负责判定（见 ``tests.py`` 的
    ``StaleInstallCheckTest``），清理命令本身在 ``test_stale_cleanup.py`` 里覆盖，这里只关心
    「什么时候跑、跑在哪几台上、失败了怎么落」。
    """

    def report(self, will_cleanup=True, targets=('192.168.10.10',), reason=''):
        """构造一份只带 ``stale_install`` 一项的预检报告。

        参数：`will_cleanup` 表示本次是否清理；`targets` 表示要清理的机器；`reason` 表示不清理的原因。
        返回：返回预检报告字典。
        副作用：纯计算。
        """
        return {
            'ok': True,
            'checks': [{
                'name': 'stale_install',
                'title': '上一次安装的残留',
                'status': 'warned',
                'message': '发现上一次安装留下的集群数据',
                'nodes': list(targets),
                'detail': {
                    'cleanup': {
                        'enabled': True,
                        'will_cleanup': will_cleanup,
                        'reason': reason,
                        'targets': list(targets) if will_cleanup else [],
                    },
                },
            }],
            'hard_failures': [],
        }

    def run_cleanup(self, result=None, error=None):
        """直接调 ``_clean_stale_install``，返回上下文替身与调用记录。

        参数：`result` 表示清理的返回报告；`error` 表示清理抛出的异常。
        返回：返回 ``(上下文替身, 清理替身)``。
        副作用：把 ``run_stale_cleanup`` 换掉并调用被测函数。
        """
        nodes = self.add_nodes(3)
        context = mock.MagicMock()
        side_effect = error if error is not None else (lambda *args, **kwargs: result or [])
        with mock.patch.object(stale_cleanup, 'run_stale_cleanup', side_effect=side_effect) as cleanup:
            state_machine._clean_stale_install(
                context, self.deployment, nodes, {}, self.report(),
            )
        return context, cleanup

    def test_nothing_is_cleaned_when_the_preflight_says_so(self):
        """预检没让清（机器干净、或集群已经装出来了）时连阶段都不开。

        用户的要求就是「没有残留就直接部署」：多出一个什么都没做的「清理」阶段，进度页上
        会让人以为平台动了机器。
        """
        nodes = self.add_nodes(3)
        context = mock.MagicMock()
        with mock.patch.object(stale_cleanup, 'run_stale_cleanup') as cleanup:
            state_machine._clean_stale_install(
                context, self.deployment, nodes, {},
                self.report(will_cleanup=False, reason='already_installed'),
            )
        cleanup.assert_not_called()
        context.begin_stage.assert_not_called()
        context.complete_stage.assert_not_called()

    def test_missing_check_is_treated_as_nothing_to_clean(self):
        """报告里没有这一项（老版本、或该项被跳过）时按「不用清」处理，不能报错。"""
        nodes = self.add_nodes(3)
        context = mock.MagicMock()
        with mock.patch.object(stale_cleanup, 'run_stale_cleanup') as cleanup:
            state_machine._clean_stale_install(
                context, self.deployment, nodes, {}, {'ok': True, 'checks': [], 'hard_failures': []},
            )
        cleanup.assert_not_called()
        context.begin_stage.assert_not_called()

    def test_stage_is_opened_and_closed_around_the_cleanup(self):
        """真清理时阶段成对出现，并且跑在 ``01.prepare`` 之前。"""
        context, cleanup = self.run_cleanup(result=[{'label': '192.168.10.10', 'left': []}])

        context.begin_stage.assert_called_once()
        self.assertEqual(context.begin_stage.call_args.args[0], 'stale-cleanup')
        context.complete_stage.assert_called_once_with('stale-cleanup')
        self.assertEqual(cleanup.call_args.args[4], ['192.168.10.10'])
        self.assertIn('保留容器运行时的镜像缓存', context.log.call_args_list[0].args[1])

    def test_failure_aborts_the_task_with_the_cleanup_error(self):
        """清不掉就中止：残留没清干净，往下跑必然又失败在 02.etcd，还是会看到「退出码 1」。"""
        with self.assertRaises(state_machine.TaskAborted) as caught:
            self.run_cleanup(
                error=stale_cleanup.StaleCleanupError('stale_cleanup_failed', '节点 192.168.10.10 清理失败'),
            )
        self.assertEqual(caught.exception.error_code, 'stale_cleanup_failed')
        self.assertIn('192.168.10.10', caught.exception.message)

    def test_full_install_cleans_before_the_first_kubeasz_stage(self):
        """整条流程的接线：清理排在 01.prepare 之前，机器那时还是原样。"""
        self.add_nodes(3)
        self._patches()
        preflight_patch = mock.patch.object(
            state_machine, '_run_preflight', return_value=self.report(),
        )
        preflight_patch.start()
        self.addCleanup(preflight_patch.stop)
        order = []
        with mock.patch.object(
            stale_cleanup, 'run_stale_cleanup',
            side_effect=lambda *args, **kwargs: order.append('stale-cleanup') or [],
        ), mock.patch.object(
            state_machine, '_apply_node_hostname',
            side_effect=lambda *args, **kwargs: order.append('hostname'),
        ):
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )

        self.deployment.refresh_from_db()
        self.assertEqual(order, ['stale-cleanup', 'hostname'])
        self.assertIn('stale-cleanup', self.deployment.completed_stages)

    def test_clean_machine_skips_the_stage_in_a_full_install(self):
        """机器干净时整条流程里看不到清理这一步 —— 阶段清单与事件里都没有它。"""
        self.add_nodes(3)
        self._patches()
        preflight_patch = mock.patch.object(
            state_machine, '_run_preflight',
            return_value=self.report(will_cleanup=False, targets=()),
        )
        preflight_patch.start()
        self.addCleanup(preflight_patch.stop)
        with mock.patch.object(stale_cleanup, 'run_stale_cleanup') as cleanup:
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )

        self.deployment.refresh_from_db()
        cleanup.assert_not_called()
        self.assertNotIn('stale-cleanup', self.deployment.completed_stages)
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=self.deployment)
            .values_list('event_type', flat=True)
        )
        self.assertNotIn('stale_cleanup', event_types)


class EzctlKubeconfigTest(StateMachineTestBase):
    """ezctl 子进程的 ``KUBECONFIG``：kubeasz 那批裸 ``kubectl`` 只靠它才知道该连哪个 apiserver。

    漏掉它时 04.kube-master 会在「创建 user:kubernetes 角色绑定」上失败，
    报 ``dial tcp [::1]:8080: connect: connection refused`` —— 看起来像 apiserver 没起来，
    实际是客户端回退到了 kubectl 的默认地址。

    另一种更隐蔽的失败是**指到了别的集群那份**（2026-09-18 实机：任务重试复用工作区，
    工作区里留着上一次那个集群 ``kk8s`` 的 kubeconfig，这一轮装的是 ``k8s``），报出来是
    ``x509: certificate signed by unknown authority``，而两个集群的 CA 都叫 ``kubernetes-ca``，
    从报错里看不出是拿错了文件。因此下面既钉住优先级，也钉住「别的集群那份一律不用」。
    """

    def _pipeline(self):
        """造一条只用来问路径的流水线。

        参数：无。
        返回：返回 Pipeline，工作区在临时状态根下且此时并不存在。
        副作用：只在临时目录里做路径计算。
        """
        context = mock.Mock()
        context.workspace = str(self.state_root / 'runs' / 'deployment-1')
        context.deployment = self.deployment
        return state_machine.Pipeline(context, runner.FakeRunner(), kubeasz_root=self.artifacts_root)

    @staticmethod
    def _kubeconfig_text(cluster_name):
        """造一份最小可解析的 kubeconfig，``clusters[].name`` 是判断归属的依据。

        参数：`cluster_name` 表示集群名（kubeasz 里等于集群 slug）。
        返回：返回 kubeconfig 文本。
        副作用：纯计算。
        """
        return (
            'apiVersion: v1\n'
            'clusters:\n'
            '- cluster:\n'
            '    server: https://172.30.0.8:6443\n'
            f'  name: {cluster_name}\n'
            'contexts:\n'
            '- context:\n'
            f'    cluster: {cluster_name}\n'
            '    user: admin\n'
            f'  name: context-{cluster_name}\n'
            f'current-context: context-{cluster_name}\n'
            'kind: Config\n'
            'users:\n'
            '- name: admin\n'
            '  user:\n'
            '    token: placeholder\n'
        )

    def _generated(self, cluster_name):
        """在集群目录里写一份 kubeasz 生成的 kubeconfig。

        参数：`cluster_name` 表示写进 ``clusters[].name`` 的集群名。
        返回：返回写入的 Path。
        副作用：创建父目录并写文件。
        """
        generated = paths.kubeconfig_path(self.artifacts_root, self.deployment.cluster_slug)
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(self._kubeconfig_text(cluster_name), encoding='utf-8')
        return generated

    def _workspace_copy(self, cluster_name):
        """在工作区根目录里写一份 kubeconfig 副本（``register.stage_kubeconfig`` 的产物）。

        参数：`cluster_name` 表示写进 ``clusters[].name`` 的集群名。
        返回：返回写入的 Path。
        副作用：创建父目录并写文件。
        """
        workspace = Path(self.state_root / 'runs' / 'deployment-1')
        workspace.mkdir(parents=True, exist_ok=True)
        copy = workspace / 'kubeconfig'
        copy.write_text(self._kubeconfig_text(cluster_name), encoding='utf-8')
        return copy

    def test_cluster_kubeconfig_is_used_before_the_workspace_copy_exists(self):
        """01~07 阶段工作区那份还没生成（要到最后 register 阶段才放进去），先用集群目录里那份。"""
        generated = self._generated('fsm-ha-cluster')
        self.assertEqual(self._pipeline().kubeconfig_path(), generated)
        self.assertEqual(self._pipeline().command_env()['KUBECONFIG'], str(generated))

    def test_generated_kubeconfig_wins_over_a_workspace_copy(self):
        """两份都在时用集群目录那份：kubeasz 每个阶段都按当前 CA 重生成它，工作区那份只是副本。"""
        generated = self._generated('fsm-ha-cluster')
        self._workspace_copy('fsm-ha-cluster')
        self.assertEqual(self._pipeline().kubeconfig_path(), generated)

    def test_workspace_copy_from_another_cluster_is_never_used(self):
        """回归 2026-09-18：工作区里换过集群的陈旧副本必须被拒，宁可不指向任何文件。

        集群目录那份此时还不存在（重试刚开始），旧代码会直接用工作区那份 —— 于是整个
        04.kube-master 拿上次那个集群的 CA 去连新 apiserver。这里真正要钉住的是
        「选出来的不是那份陈旧副本」，而不是它会退化成什么。
        """
        stale = self._workspace_copy('kk8s')
        chosen = self._pipeline().kubeconfig_path()
        self.assertNotEqual(chosen, stale)
        self.assertFalse(chosen.exists())

    def test_same_cluster_workspace_copy_is_still_used_as_a_fallback(self):
        """集群目录被清掉（``destroy``）之后，本集群的工作区副本仍要能兜底。"""
        copy = self._workspace_copy('fsm-ha-cluster')
        self.assertEqual(self._pipeline().kubeconfig_path(), copy)

    def test_workspace_copy_that_cannot_be_parsed_still_falls_back(self):
        """解析不出集群名时按「判断不了」处理，继续沿用工作区那份，不因格式意外断流程。"""
        workspace = Path(self.state_root / 'runs' / 'deployment-1')
        workspace.mkdir(parents=True, exist_ok=True)
        copy = workspace / 'kubeconfig'
        copy.write_text('这不是一份 kubeconfig\n', encoding='utf-8')
        self.assertEqual(self._pipeline().kubeconfig_path(), copy)

    def test_fallback_never_points_at_the_operators_home(self):
        """两份都没有时宁可让 kubectl 报错，也不能让它去读平台机的 ``~/.kube/config``。"""
        chosen = self._pipeline().kubeconfig_path()
        self.assertFalse(chosen.exists())
        self.assertNotEqual(chosen, Path.home() / '.kube' / 'config')


class IngressNodeLabelTest(StateMachineTestBase):
    """新建集群时给节点打 ingress 标签：少了它 controller 会永远 Pending。

    真实背景（2026-09-20，114.67.89.90）：单节点按公网 IP 部署，任务 7/7 全绿，但
    ingress-nginx 的 chart 只挑带 ``ingress-controller/provider=ingress-nginx`` 标签的节点
    （kubeasz 的 cluster-addon 角色里的 nodeSelector），kubeasz 自己不发这个标签、只在找不到
    节点时打印一句 WARNING 就继续 —— 于是 controller 一直 Pending，界面上却是一份「成功」。
    平台因此要在 07.addons 之前自己把标签打上。
    """

    LABEL_STAGE = 'ingress-label'
    # 与 kubeasz chart 里 nodeSelector 的键值逐字一致，写错一个字就是白打。
    LABEL = 'ingress-controller/provider=ingress-nginx'

    def run_tracked(self, deployment=None, behaviors=None):
        """打桩后执行任务，并记录每一次被执行的命令。

        参数：`deployment` 表示要执行的任务，默认用基类那条；`behaviors` 表示 FakeRunner 的阶段行为。
        返回：返回执行后的 K8sDeployment。
        副作用：完整推进一次任务，并在内存里记录阶段、命令、环境与超时。

        FakeRunner 不记录调用，而这一步的关键正是「跑了什么、排在哪两个阶段之间」，
        所以在它外面套一层 spy，而不是断言别的间接证据。
        """
        self._patches(behaviors=behaviors)
        self.commands = []
        original = self.fake_runner.run_stage

        def spy(stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                on_idle=None, on_tick=None):
            """记一笔再原样转交给 FakeRunner。"""
            self.commands.append({
                'stage': stage,
                'argv': [str(item) for item in argv],
                'cwd': str(cwd),
                'env': dict(env or {}),
                'timeout': timeout,
            })
            return original(
                stage, argv, cwd, env=env, on_line=on_line, timeout=timeout, is_cancelled=is_cancelled,
                on_idle=on_idle, on_tick=on_tick,
            )

        self.fake_runner.run_stage = spy
        return state_machine.execute_task(
            deployment or self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )

    def stages(self):
        """返回本次任务执行过的阶段序列。

        参数：无。
        返回：返回阶段标识列表。
        副作用：只读内存。
        """
        return [item['stage'] for item in self.commands]

    def label_calls(self):
        """返回打标签那一步的执行记录。

        参数：无。
        返回：返回记录列表，没跑过时为空。
        副作用：只读内存。
        """
        return [item for item in self.commands if item['stage'] == self.LABEL_STAGE]

    def test_labeling_sits_between_network_and_addons(self):
        """位置卡在 06.network 与 07.addons 之间：早了 apiserver 还没起，晚了 chart 已落地。"""
        self.add_nodes(3)
        result = self.run_tracked()
        self.assertEqual(result.status, 'succeeded')

        stages = self.stages()
        index = stages.index(self.LABEL_STAGE)
        self.assertEqual(stages[index - 1], '06.network')
        self.assertEqual(stages[index + 1], '07.addons')

    def test_label_command_selects_every_node_and_overwrites(self):
        """一条 kubectl 覆盖所有节点并允许覆盖：单节点与多节点是同一条命令。

        ``--overwrite`` 是重试安全的关键：标签已存在时 kubectl 默认报错退出，重跑会无谓地
        卡在这条本已成功的一步上。
        """
        self.add_nodes(3)
        self.run_tracked()
        call = self.label_calls()[0]
        self.assertEqual(call['argv'], [
            'kubectl', 'label', 'node', '--all', self.LABEL, '--overwrite',
        ])

    def test_labeling_reuses_the_pipeline_kubeconfig(self):
        """走的是 kubeasz 那批 connection: local 任务用的同一份 KUBECONFIG，不另开一条路径。"""
        self.add_nodes(3)
        self.run_tracked()
        call = self.label_calls()[0]
        # 开发机是 Windows，路径分隔符与平台机不同，这里统一成 / 再比。
        kubeconfig = call['env']['KUBECONFIG'].replace('\\', '/')
        self.assertTrue(kubeconfig.endswith('fsm-ha-cluster/kubectl.kubeconfig'), kubeconfig)
        self.assertEqual(call['cwd'], str(self.artifacts_root))

    def test_labeling_records_its_own_step(self):
        """这一步要留在任务的已完成阶段里，出问题时能从进度记录看出它跑过没有。"""
        self.add_nodes(3)
        result = self.run_tracked()
        self.assertIn(self.LABEL_STAGE, result.completed_stages)

    def test_labeling_is_skipped_when_ingress_is_disabled(self):
        """不装 ingress 就不打标签：没用的标签只会让节点上多出一个查不出出处的记号。"""
        self.deployment.nginx_ingress = False
        self.deployment.save(update_fields=['nginx_ingress', 'updated_at'])
        self.add_nodes(3)
        result = self.run_tracked()
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(self.label_calls(), [])
        self.assertNotIn(self.LABEL_STAGE, result.completed_stages)

    def test_label_failure_stops_before_the_addon_stage(self):
        """打标签失败必须中止任务：否则换来的是一份「成功」加一个永远 Pending 的 controller。"""
        self.add_nodes(3)
        result = self.run_tracked(behaviors={self.LABEL_STAGE: {'returncode': 1}})
        self.assertEqual(result.status, 'failed')
        self.assertNotIn('07.addons', self.stages())
        self.assertNotIn(self.LABEL_STAGE, result.completed_stages)

    def test_label_failure_says_what_would_have_happened(self):
        """失败原因要写清后果（Pending），否则运维只会看到一句「退出码 1」。"""
        self.add_nodes(3)
        result = self.run_tracked(behaviors={self.LABEL_STAGE: {'returncode': 1}})
        self.assertIn('Pending', result.failure_reason)
        self.assertIn('ingress', result.failure_reason)


class FullInstallFlowTest(StateMachineTestBase):
    """新建集群的整条流水线。"""

    def test_successful_ha_deploy_marks_task_and_cluster(self):
        """成功路径：任务 succeeded、阶段全部完成、集群登记为平台部署并产生 revision 1。"""
        self.add_nodes(3)
        result = self.execute()
        self.deployment.refresh_from_db()

        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(self.deployment.error_code, '')
        # 未启用外部负载均衡的集群只有七步：清单要按任务类型与选项取，不能直接拿
        # ``runner.STAGE_SPECS``（它含那个可选阶段）。
        expected = [spec['key'] for spec in runner.stage_specs_for(self.deployment.task_type, self.deployment)]
        for key in expected:
            self.assertIn(key, self.deployment.completed_stages)
        self.assertEqual(self.deployment.current_stage, 'register')

        cluster = self.deployment.cluster
        self.assertIsNotNone(cluster)
        self.assertEqual(cluster.origin, 'deployed')
        self.assertEqual(cluster.slug, 'fsm-ha-cluster')
        self.assertEqual(cluster.status, 'configured')
        self.assertEqual(
            K8sClusterConfigRevision.objects.filter(cluster=cluster).count(), 1,
        )
        self.assertEqual(self.deployment.expected_config_revision, 0)

    def test_successful_deploy_marks_nodes_ready(self):
        """成功路径下所有节点快照都标记为 ready。"""
        self.add_nodes(3)
        self.execute()
        statuses = {item.status for item in self.deployment.nodes.all()}
        self.assertEqual(statuses, {'ready'})

    def test_successful_deploy_writes_events_and_logs(self):
        """成功路径要留下可增量拉取的事件与日志：前端进度页完全依赖它们。"""
        self.add_nodes(3)
        self.execute()
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=self.deployment)
            .values_list('event_type', flat=True)
        )
        self.assertIn('task_started', event_types)
        self.assertIn('task_finished', event_types)
        logs = K8sDeploymentLog.objects.filter(deployment=self.deployment).order_by('seq')
        self.assertGreater(logs.count(), 0)
        # seq 必须从 1 连续递增，前端按 seq 做增量拉取，跳号会丢日志。
        self.assertEqual([item.seq for item in logs], list(range(1, logs.count() + 1)))

    def test_deploy_uses_compiled_config_with_cluster_slug(self):
        """编译产物落在集群目录名下，且 config.yml 里没有残留占位符。"""
        self.add_nodes(3)
        self.deploy_run = self.execute()
        hosts = paths.hosts_path(self.artifacts_root, 'fsm-ha-cluster')
        config = paths.config_path(self.artifacts_root, 'fsm-ha-cluster')
        self.assertTrue(hosts.is_file())
        self.assertTrue(config.is_file())
        config_text = config.read_text(encoding='utf-8')
        self.assertEqual(compiler.residual_placeholders(config_text), [])
        self.assertIn('K8S_VER: "1.29.4"', config_text)
        self.assertIn('CLUSTER_NAME: "fsm-ha-cluster"', config_text)
        hosts_text = hosts.read_text(encoding='utf-8')
        self.assertIn('192.168.10.10', hosts_text)
        # inventory 里只允许出现用户名与端口，绝不允许把口令写进配置文件（规划 5.4）。
        self.assertNotIn('ansible_ssh_pass', hosts_text)
        self.assertNotIn('password', hosts_text.lower())

    def test_stage_failure_marks_task_failed_and_nodes_failed(self):
        """某个阶段返回非零退出码时任务失败，节点快照标记为失败并保留诊断依据。"""
        self.add_nodes(3)
        self.execute(behaviors={'04.kube-master': {'returncode': 2}})
        self.deployment.refresh_from_db()

        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'stage_failed')
        self.assertIn('部署 Master', self.deployment.failure_reason)
        self.assertEqual({item.status for item in self.deployment.nodes.all()}, {'failed'})
        self.assertNotIn('04.kube-master', self.deployment.completed_stages)

    def test_deploy_failure_is_not_rollback_required(self):
        """新建失败停在 failed：还没有可用集群，谈不上回滚。"""
        self.add_nodes(3)
        self.execute(behaviors={'*': {'returncode': 1}})
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')

    def test_stage_timeout_has_its_own_error_code(self):
        """阶段超时用 stage_timeout 而不是 stage_failed：运维据此判断该查现场而非重跑。"""
        self.add_nodes(3)
        with override_settings(K8S_DEPLOY_STAGE_TIMEOUT_SECONDS=1):
            self.execute(behaviors={'03.runtime': {'hang': True}}, stage_delay=0.0)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'stage_timeout')
        self.assertIn('超时', self.deployment.failure_reason)

    def test_cancel_during_stage_marks_task_cancelled(self):
        """阶段执行中被取消时任务落到 cancelled，而不是失败。"""
        self.add_nodes(3)
        self.deployment.cancel_requested = True
        self.deployment.save(update_fields=['cancel_requested', 'updated_at'])
        self.execute(behaviors={'02.etcd': {'hang': True}}, stage_delay=0.0)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'cancelled')
        self.assertEqual(self.deployment.error_code, 'cancelled')

    def test_ha_topology_requires_three_masters(self):
        """HA 拓扑只给了两个 master 时必须拒绝，而不是装出一个半残集群。"""
        self.add_nodes(2)
        self.execute()
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'ha_requires_three_masters')

    def test_task_without_nodes_is_rejected_before_artifact_download(self):
        """没有节点快照的任务要在下载制品之前就被拒绝。"""
        with mock.patch.object(state_machine, '_download_release') as download:
            with mock.patch.object(
                register, 'run_health_check',
                return_value={'ok': True, 'message': '', 'detail': {}},
            ):
                state_machine.execute_task(self.deployment, worker_id='test-worker')
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.error_code, 'no_target_nodes')
        download.assert_not_called()

    def test_unexpected_error_still_lands_in_terminal_state(self):
        """未预期异常不能让任务永远挂在 running：必须落终态并说明原因。"""
        self.add_nodes(3)
        self._patches()
        with mock.patch.object(state_machine, '_compile', side_effect=RuntimeError('编译炸了')):
            state_machine.execute_task(
                self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
            )
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'unexpected_error')
        self.assertIn('编译炸了', self.deployment.failure_reason)

    def test_result_summary_is_redacted(self):
        """结果摘要要经过脱敏，不能把密钥顺手写进任务摘要。"""
        self.add_nodes(3)
        self._patches()
        with mock.patch.object(
            register, 'register_deployment_result',
            return_value={
                'cluster_id': 1, 'cluster_name': 'x', 'revision': 1,
                'health': {'ok': True, 'message': 'ok'},
                'kubeconfig': 'apiVersion: v1\npassword=secret-pass',
            },
        ):
            state_machine.execute_task(
                self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
            )
        self.deployment.refresh_from_db()
        summary = str(self.deployment.result_summary)
        self.assertNotIn('secret-pass', summary)


class SharedLbNodePrepTest(StateMachineTestBase):
    """「负载均衡与集群节点同机」那两台机器上，赶在 ``10.ex-lb`` 之前必须做完的两件事。

    同机的机器上 apiserver 正绑着对外端口，l4lb 只能改绑 VIP，于是：

    1. **模板要先变成可配的**。还是写死的 ``0.0.0.0`` 时 nginx 会与 apiserver 抢同一个端口，
       而 Linux 下通配地址与具体地址在同一个端口上互斥（两个方向都 EADDRINUSE）—— 起不来。
    2. **内核要先允许绑一个本机还没有的地址**。否则 nginx 直接 ``EADDRNOTAVAIL``。

    两件事都必须在那个剧本**开始之前**就位：nginx 正是在那个剧本里第一次启动的，晚一步就
    来不及；而剧本末尾还有一步拨 VIP 要 200 的验收，keepalived 又挂着
    ``vrrp_track_process check-l4lb`` —— l4lb 不在它就把 VIP 一起丢掉，于是没有「降级」这个
    中间态，只有全好与全坏。
    """

    LB_VIP = '192.168.30.250'

    def make_ha_with_shared_lb(self, shared=2):
        """建一个「三台机器，其中两台兼任负载均衡」的任务。

        参数：`shared` 表示前几台兼任负载均衡（2 就是「省掉两台专用机器」那种三机拓扑）。
        返回：返回 K8sDeployment 实例。
        副作用：向数据库写入任务与三条节点快照。
        """
        self.deployment = self._make_deployment(
            'deploy_ha', 'fsm-shared-lb', lb_mode='self', lb_vip=self.LB_VIP,
            nginx_ingress=False,
        )
        roles = [
            ['etcd', 'master'] + (['loadbalancer'] if index < shared else [])
            for index in range(3)
        ]
        self.add_nodes(3, roles=roles)
        return self.deployment

    def write_l4lb_template(self, text=None):
        """往合成制品里放一份 ex-lb 的 l4lb 模板。

        参数：`text` 表示模板内容，缺省用一份「与上游同形」的最小版本。
        返回：返回模板文件路径。
        副作用：在合成制品树里创建目录并写文件。
        """
        if text is None:
            text = (
                'stream {\n'
                '    server {\n'
                '        listen 0.0.0.0:{{ EX_APISERVER_PORT }};\n'
                '        proxy_connect_timeout 1s;\n'
                '        proxy_pass apiservers;\n'
                '    }\n'
                '}\n'
            )
        path = self.artifacts_root / addon_overrides.L4LB_TEMPLATE_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def run_task(self, observed=None, ensure=None):
        """跑一次完整安装，并把 ``ensure_nonlocal_bind`` 换成可观察的替身。

        参数：`observed` 表示 ``on_stage`` 回调（阶段键、argv），缺省不记录；
            `ensure` 表示替身的行为，缺省是「一切正常」。
        返回：返回执行后的任务。
        副作用：完整推进一次任务；替身实例留在 ``self.ensure_mock`` 上供断言。

        替身必须在这里启动、而不是在调用处的 ``with`` 里：``_patches`` 已经为防真连网络
        放了一个兜底替身，晚启动的那个才生效 —— 放在 ``with`` 里会被它盖回去，计数永远是 0。
        """
        self._patches()
        patcher = mock.patch.object(
            node_l4lb, 'ensure_nonlocal_bind',
            side_effect=ensure or (
                lambda *args, **kwargs: {'label': 'node', 'changed': True, 'value': '1'}
            ),
        )
        self.ensure_mock = patcher.start()
        self.addCleanup(patcher.stop)
        if observed is not None:
            self.fake_runner.on_stage = observed
        return state_machine.execute_task(
            self.deployment, worker_id='w', runner_instance=self.fake_runner,
        )

    def test_the_template_is_made_configurable_here(self):
        """制品里那份模板被改成读 host var，兜底值仍是 ``0.0.0.0``。"""
        self.make_ha_with_shared_lb()
        template = self.write_l4lb_template()

        self.run_task()

        text = template.read_text(encoding='utf-8')
        self.assertIn("listen {{ LB_LISTEN_ADDRESS | default('0.0.0.0') }}", text)

    def test_the_template_is_ready_before_the_lb_stage_starts(self):
        """改模板这件事必须赶在那个阶段**开始**之前 —— 早一步是改一个不存在的文件的反面：
        晚一步就等于让 nginx 在同一个剧本里带着写死的通配地址启动。"""
        self.make_ha_with_shared_lb()
        template = self.write_l4lb_template()
        seen = {}

        self.run_task(
            observed=lambda stage, _argv: seen.setdefault(
                stage, addon_overrides.L4LB_LISTEN_VARIABLE in template.read_text(encoding='utf-8'),
            ),
        )

        self.assertIs(seen['10.ex-lb'], True)

    def test_the_kernel_switch_is_opened_on_exactly_the_shared_machines(self):
        """只有同机的机器需要它，逐台来；第三台不兼任负载均衡，不碰。"""
        self.make_ha_with_shared_lb()
        self.write_l4lb_template()

        self.run_task()

        self.assertEqual(
            [call.args[1].ip_address for call in self.ensure_mock.call_args_list],
            ['192.168.10.10', '192.168.10.11'],
        )

    def test_the_switch_is_opened_before_the_lb_stage_starts(self):
        """内核开关同样要在剧本之前 —— l4lb 在那个剧本里第一次启动、绑的就是 VIP。"""
        self.make_ha_with_shared_lb()
        self.write_l4lb_template()
        seen = []

        self.run_task(
            observed=lambda stage, _argv: seen.append((stage, self.ensure_mock.call_count)),
        )

        self.assertEqual(seen[-1], ('10.ex-lb', 2))
        self.assertEqual([count for _stage, count in seen[:-1]], [0] * (len(seen) - 1))

    def test_dedicated_lb_machines_are_left_alone(self):
        """两台专用负载均衡机器：端口本来就是空的、绑的是通配地址，两件事都不需要做，
        模板也不该被改（改了要多一个 ``ip_nonlocal_bind`` 依赖，白白多一个失败点）。"""
        self.deployment = self._make_deployment(
            'deploy_ha', 'fsm-dedicated-lb', lb_mode='self', lb_vip=self.LB_VIP,
            nginx_ingress=False,
        )
        self.add_nodes(3)
        self.add_nodes(2, roles=[['loadbalancer'], ['loadbalancer']], ip_prefix='192.168.30.')
        template = self.write_l4lb_template()

        self.run_task()

        self.assertEqual(self.ensure_mock.call_count, 0)
        self.assertNotIn(
            addon_overrides.L4LB_LISTEN_VARIABLE, template.read_text(encoding='utf-8'),
        )

    def test_a_missing_template_aborts_before_the_stage(self):
        """制品里没有那份模板：同机时「没改成功」等于必然撞端口，必须原地中止，
        而不是等剧本跑到一半给一句 systemd 启动失败。"""
        self.make_ha_with_shared_lb()

        self.run_task()

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'l4lb_template_not_patchable')
        self.assertIn('端口', self.deployment.failure_reason)
        self.assertNotIn('10.ex-lb', self.deployment.completed_stages)

    def test_a_template_without_the_listen_line_aborts_too(self):
        """模板在、但里面没有可改的那一行（上游结构变了）：同上，报出来并中止。"""
        self.make_ha_with_shared_lb()
        self.write_l4lb_template('stream {\n    server {\n        proxy_pass apiservers;\n    }\n}\n')

        self.run_task()

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.error_code, 'l4lb_template_not_patchable')

    def test_a_failed_switch_aborts_and_names_the_machines(self):
        """内核开关没打开时任务失败，且报错要指名是哪几台 —— 用户得知道去查哪一台。"""
        self.make_ha_with_shared_lb()
        self.write_l4lb_template()

        def boom(_context, node, _credential=None, **_kwargs):
            """模拟那台机器上改不动内核参数。"""
            raise node_l4lb.NodeL4lbError(
                'node_nonlocal_bind_failed',
                f'节点 {node.ip_address} 上没能打开 {node_l4lb.SYSCTL_KEY}',
            )

        self.run_task(ensure=boom)

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'node_nonlocal_bind_failed')
        self.assertIn('192.168.10.10', self.deployment.failure_reason)
        self.assertIn('192.168.10.11', self.deployment.failure_reason)
        self.assertNotIn('10.ex-lb', self.deployment.completed_stages)

    def test_the_shared_inventory_narrows_every_listen_address_to_the_vip(self):
        """编译产物里两台负载均衡机器**都**绑 VIP：两台配置一致，故障时不必先分辨
        现在接管的是按哪条路径配的那台。"""
        self.make_ha_with_shared_lb()
        self.write_l4lb_template()

        self.run_task()

        hosts_text = paths.hosts_path(
            self.artifacts_root, 'fsm-shared-lb',
        ).read_text(encoding='utf-8')
        sections = compiler.parse_inventory_sections(hosts_text)
        self.assertEqual(sections['ex_lb'], ['192.168.10.10', '192.168.10.11'])
        # 同机：那两台同时也是集群节点，两个段里都要有它们。
        self.assertEqual(sections['kube_master'], ['192.168.10.10', '192.168.10.11', '192.168.10.12'])
        self.assertEqual(
            hosts_text.count(f'{compiler.LB_LISTEN_KEY}={self.LB_VIP}'), 2,
        )

    def test_a_three_machine_cluster_is_installed_end_to_end(self):
        """三机拓扑整条路能走完：阶段序列与专用机器那份完全一样，一个都不多。"""
        self.make_ha_with_shared_lb()
        self.write_l4lb_template()
        stages = []

        self.run_task(observed=lambda stage, _argv: stages.append(stage))

        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'succeeded')
        self.assertEqual(stages, ['01.prepare', '02.etcd', '03.runtime', '04.kube-master',
                                  '05.kube-node', '06.network', '07.addons', '10.ex-lb'])
        self.assertEqual(self.deployment.cluster.node_count, 3)
        self.assertEqual(self.deployment.cluster.api_server_vip, self.LB_VIP)


class ExternalLbStageFlowTest(StateMachineTestBase):
    """启用外部负载均衡时多出来的第 8 个 ezctl 阶段。

    这一步只在勾了外部负载均衡的集群上跑：kubeasz 的 ``10.ex-lb.yml`` 把 l4lb（nginx）与
    keepalived 装到 ``[ex_lb]`` 组的两台机器上，并把 VIP 抢起来。不勾的集群跑到七步就结束，
    时间轴上也不该多出一个永远不变绿的第八步。
    """

    LB_VIP = '192.168.30.250'

    def make_ha_with_lb(self):
        """建一个「三台 etcd+master 加两台负载均衡机器」的任务。

        参数：无。
        返回：返回 K8sDeployment 实例。
        副作用：向数据库写入任务与五条节点快照。
        """
        # 关掉 ingress：启用时 ``06.network`` 与 ``07.addons`` 之间会插一个平台自己的打标签
        # 阶段，那是另一条链路上的事（见 ``IngressNodeLabelTest``），混进来会让下面「阶段序列
        # 恰好等于清单」的断言读不出重点。
        self.deployment = self._make_deployment(
            'deploy_ha', 'fsm-lb-cluster', lb_mode='self', lb_vip=self.LB_VIP,
            nginx_ingress=False,
        )
        self.add_nodes(3)
        # 负载均衡机器用另一个网段，避免与 master 的地址撞上。
        self.add_nodes(2, roles=[['loadbalancer'], ['loadbalancer']], ip_prefix='192.168.30.')
        return self.deployment

    def execute_tracking_stages(self, behaviors=None):
        """执行任务并记录实际跑过的阶段与命令行。

        参数：`behaviors` 表示阶段行为。
        返回：返回 ``(任务, [阶段键], {阶段键: argv})``。
        副作用：完整推进一次任务。
        """
        self._patches(behaviors=behaviors)
        stages = []
        commands = {}

        def on_stage(stage, argv):
            """记录阶段标识与命令行。"""
            stages.append(stage)
            commands[stage] = list(argv)

        self.fake_runner = runner.FakeRunner(behaviors=behaviors, on_stage=on_stage)
        deployment = state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )
        return deployment, stages, commands

    def test_eighth_stage_runs_when_enabled(self):
        """启用时最后一步是 10.ex-lb，且步骤码就是上游的 10。"""
        self.make_ha_with_lb()
        _deployment, stages, commands = self.execute_tracking_stages()
        self.deployment.refresh_from_db()

        self.assertEqual(self.deployment.status, 'succeeded')
        self.assertEqual(stages[-1], '10.ex-lb')
        # 命令行尾部就是 ezctl 的三个参数：步骤码是上游的 10，不是「第八步」。
        self.assertEqual(commands['10.ex-lb'][-3:], ['setup', 'fsm-lb-cluster', '10'])
        self.assertIn('10.ex-lb', self.deployment.completed_stages)

    def test_vip_relay_is_set_up_right_before_the_lb_stage(self):
        """第 8 阶段开始前要把 VIP 通道铺好 —— 那句验活是在**平台机**上拨 VIP 的。

        ``roles/ex-lb`` 末尾的 ``uri`` 任务带 ``connection: local``，拨的是
        ``https://<VIP>:<端口>``；VIP 不在 inventory 任何节点段里，前面两条链路覆盖不到它。
        少了这一步，前七个阶段全绿之后第 8 阶段会以 ``urlopen error timed out`` 收场。
        """
        self.make_ha_with_lb()
        self._patches()
        seen = []
        with mock.patch.object(platform_relay, 'ensure_lb_vip', return_value={}) as ensure:
            self.fake_runner.on_stage = lambda stage, argv: seen.append((stage, ensure.call_count))
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )
        self.assertEqual(ensure.call_count, 1)
        # 一次就够，而且在正确的位置：前七个阶段还是 0（那两台 LB 机器上什么都没装），
        # 轮到 10.ex-lb 时通道已经建好了。
        self.assertEqual(seen[-1], ('10.ex-lb', 1))
        self.assertEqual([count for _stage, count in seen[:7]], [0] * 7)

    def test_lb_proxies_are_tuned_right_after_the_lb_stage(self):
        """l4lb 的转发参数在第 8 阶段之后逐台调整，两台都要碰。

        时机是这件事的关键：那份 ``/etc/l4lb/conf/l4lb.conf`` 由该阶段渲染出来，早于它
        动手是在改一个不存在的文件；晚到收尾处则会让「集群装好了、转发却还是 1 秒超时」
        多留一次重跑。
        """
        self.make_ha_with_lb()
        self._patches()
        seen = []
        with mock.patch.object(
            node_l4lb, 'apply_node', return_value={'conf': True, 'reload': True},
        ) as tune:
            self.fake_runner.on_stage = lambda stage, argv: seen.append((stage, tune.call_count))
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )
        # 两台负载均衡机器各一次。10.ex-lb 开始时还是 0 —— 配置那时还不存在。
        self.assertEqual(tune.call_count, 2)
        self.assertEqual(seen[-1], ('10.ex-lb', 0))
        self.assertEqual([count for _stage, count in seen[:7]], [0] * 7)

    def test_lb_proxies_are_not_touched_when_the_stage_is_absent(self):
        """没有外部负载均衡的任务一台机器都不碰：``_tune_lb_proxies`` 连阶段都不开。"""
        self.deployment.nginx_ingress = False
        self.deployment.save(update_fields=['nginx_ingress'])
        self.add_nodes(3)
        self._patches()
        with mock.patch.object(
            node_l4lb, 'apply_node', return_value={'conf': True, 'reload': True},
        ) as tune:
            state_machine.execute_task(
                self.deployment, worker_id='w', runner_instance=self.fake_runner,
            )
        self.assertEqual(tune.call_count, 0)

    def test_eighth_stage_is_absent_when_disabled(self):
        """未启用时一步都不多跑 —— 那个阶段清单是给别的集群看的。"""
        self.deployment.nginx_ingress = False
        self.deployment.save(update_fields=['nginx_ingress'])
        self.add_nodes(3)
        _deployment, stages, _commands = self.execute_tracking_stages()
        self.deployment.refresh_from_db()

        self.assertEqual(self.deployment.status, 'succeeded')
        self.assertEqual(stages, ['01.prepare', '02.etcd', '03.runtime', '04.kube-master',
                                  '05.kube-node', '06.network', '07.addons'])
        self.assertNotIn('10.ex-lb', self.deployment.completed_stages)

    def test_lb_stage_failure_says_the_cluster_is_otherwise_ready(self):
        """负载均衡失败时任务判失败，但失败原因要写明只有这一步没成。"""
        self.make_ha_with_lb()
        self.execute_tracking_stages(behaviors={'10.ex-lb': {'returncode': 3}})
        self.deployment.refresh_from_db()

        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'stage_failed')
        self.assertIn('外部负载均衡', self.deployment.failure_reason)
        self.assertIn('重跑', self.deployment.failure_reason)
        # 前七步确实都成功了：用户不该以为整个集群要重装。
        for key in ('01.prepare', '02.etcd', '03.runtime', '04.kube-master', '05.kube-node',
                    '06.network', '07.addons'):
            self.assertIn(key, self.deployment.completed_stages)
        self.assertNotIn('10.ex-lb', self.deployment.completed_stages)

    def test_compiled_inventory_carries_the_lb_machines_and_the_vip(self):
        """编译产物里 [ex_lb] 段是一主一备，VIP 也进了 apiserver 证书的 SAN。"""
        self.make_ha_with_lb()
        self._patches()
        state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )

        hosts_text = paths.hosts_path(self.artifacts_root, 'fsm-lb-cluster').read_text(encoding='utf-8')
        sections = compiler.parse_inventory_sections(hosts_text)
        self.assertEqual(sections['ex_lb'], ['192.168.30.10', '192.168.30.11'])
        self.assertIn(f'LB_ROLE=master EX_APISERVER_VIP={self.LB_VIP}', hosts_text)
        self.assertIn(f'LB_ROLE=backup EX_APISERVER_VIP={self.LB_VIP}', hosts_text)
        # 负载均衡机器不是集群节点：三个集群段里都不该出现它们。
        for section in ('etcd', 'kube_master', 'kube_node'):
            self.assertNotIn('192.168.30.10', sections.get(section, []))

        config_text = paths.config_path(self.artifacts_root, 'fsm-lb-cluster').read_text(encoding='utf-8')
        self.assertIn(f'  - "{self.LB_VIP}"', config_text)

    def test_cluster_records_the_vip_and_counts_only_cluster_nodes(self):
        """集群记录回写 VIP 与自建模式，节点数不含负载均衡机器。"""
        self.make_ha_with_lb()
        self._patches()
        state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )
        self.deployment.refresh_from_db()

        cluster = self.deployment.cluster
        self.assertEqual(cluster.api_server_vip, self.LB_VIP)
        self.assertEqual(cluster.lb_mode, 'self')
        self.assertEqual(cluster.node_count, 3)


class CloudLbStageFlowTest(StateMachineTestBase):
    """云内网负载均衡：什么都不装，因此阶段清单与不启用时一模一样。

    这个模式唯一的动作是把地址写进 apiserver 证书的 SAN。多排一个 ``10.ex-lb`` 的后果不是
    「多做一件事」，而是让 ``[ex_lb]`` 组为空的那一步去跑 —— 停在一个永远不会变绿的空框上，
    而且分母变成 8，一个跑完的集群显示成 87%。
    """

    CLOUD_ADDRESS = '10.0.20.250'

    def make_ha_with_cloud_lb(self):
        """建一个「三台 etcd+master、云内网负载均衡」的任务（不含负载均衡机器）。

        参数：无。
        返回：返回 K8sDeployment 实例。
        副作用：向数据库写入任务与三条节点快照。
        """
        self.deployment = self._make_deployment(
            'deploy_ha', 'fsm-cloud-lb-cluster', lb_mode='cloud', lb_vip=self.CLOUD_ADDRESS,
            nginx_ingress=False,
        )
        self.add_nodes(3)
        return self.deployment

    def test_cloud_mode_runs_exactly_seven_stages(self):
        """云模式的阶段序列与不启用时逐字相同：没有第 8 步。"""
        self.make_ha_with_cloud_lb()
        self._patches()
        stages = []
        self.fake_runner = runner.FakeRunner(
            behaviors=None, on_stage=lambda stage, argv: stages.append(stage),
        )
        state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )
        self.deployment.refresh_from_db()

        self.assertEqual(self.deployment.status, 'succeeded')
        self.assertEqual(stages, ['01.prepare', '02.etcd', '03.runtime', '04.kube-master',
                                  '05.kube-node', '06.network', '07.addons'])
        self.assertNotIn('10.ex-lb', self.deployment.completed_stages)

    def test_cloud_mode_only_writes_the_address_into_the_certificate_san(self):
        """编译产物里 [ex_lb] 段是空的、地址进了 SAN —— 这是这个模式唯一的程序侧动作。"""
        self.make_ha_with_cloud_lb()
        self._patches()
        state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )

        hosts_text = paths.hosts_path(
            self.artifacts_root, 'fsm-cloud-lb-cluster',
        ).read_text(encoding='utf-8')
        sections = compiler.parse_inventory_sections(hosts_text)
        self.assertEqual(sections['ex_lb'], [])
        self.assertNotIn(compiler.LB_ROLE_KEY, hosts_text)
        self.assertNotIn(self.CLOUD_ADDRESS, hosts_text)

        config_text = paths.config_path(
            self.artifacts_root, 'fsm-cloud-lb-cluster',
        ).read_text(encoding='utf-8')
        self.assertIn(f'  - "{self.CLOUD_ADDRESS}"', config_text)

    def test_cluster_records_the_cloud_mode(self):
        """集群记录回写 cloud 与地址：概览页与后续运维任务都靠它区分模式。"""
        self.make_ha_with_cloud_lb()
        self._patches()
        state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )
        self.deployment.refresh_from_db()

        cluster = self.deployment.cluster
        self.assertEqual(cluster.lb_mode, 'cloud')
        self.assertEqual(cluster.api_server_vip, self.CLOUD_ADDRESS)
        self.assertEqual(cluster.node_count, 3)


class ClaimNextTaskTest(StateMachineTestBase):
    """单 Worker 串行领取。"""

    def setUp(self):
        """清掉基类预置的任务，让每个用例自己决定队列里有什么。

        参数：无。
        返回：无显式返回值。
        副作用：删除基类创建的部署任务。
        """
        super().setUp()
        self.deployment.delete()

    def test_claim_only_returns_one_task_at_a_time(self):
        """已经有一个 running 时不再领取第二个任务：SQLite 上也不能并发推进。"""
        first = self._make_deployment('deploy_aio', 'fsm-claim-1', queued_at=timezone.now())
        second = self._make_deployment('deploy_aio', 'fsm-claim-2', queued_at=timezone.now())

        claimed = state_machine.claim_next_task('worker-a')
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, 'running')
        self.assertEqual(claimed.worker_id, 'worker-a')
        self.assertEqual(claimed.started_at is None, False)

        self.assertIsNone(state_machine.claim_next_task('worker-b'))
        self.assertEqual(
            K8sDeployment.objects.filter(status='running').count(), 1,
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, 'running')
        self.assertEqual(second.status, 'queued')

    def test_claim_returns_none_when_queue_is_empty(self):
        """队列为空时领取返回 None，Worker 据此进入空闲轮询。"""
        self.assertIsNone(state_machine.claim_next_task('worker-a'))

    def test_run_once_returns_none_on_empty_queue(self):
        """队列为空时 run_once 返回 None，Worker 据此休眠后重试，而不是空转报错。"""
        self.assertIsNone(state_machine.run_once(worker_id='worker-a'))

    def test_claim_follows_queue_order(self):
        """按入队时间先进先出，避免后来的任务插队。"""
        older = self._make_deployment('deploy_aio', 'fsm-older', queued_at=timezone.now())
        newer = self._make_deployment('deploy_aio', 'fsm-newer', queued_at=timezone.now())
        claimed = state_machine.claim_next_task('worker-a')
        self.assertEqual(claimed.pk, older.pk)
        older.status = 'succeeded'
        older.save(update_fields=['status', 'updated_at'])
        self.assertEqual(state_machine.claim_next_task('worker-a').pk, newer.pk)


class RunOnceTest(StateMachineTestBase):
    """run_once：常驻 Worker 循环里「领取 + 执行」的合体。"""

    def test_run_once_executes_claimed_task(self):
        """run_once 领取后直接执行到终态。"""
        self.add_nodes(3)
        self._patches()
        with mock.patch.object(
            state_machine.runner, 'select_runner', return_value=self.fake_runner,
        ):
            result = state_machine.run_once(worker_id='worker-a')
        self.assertIsNotNone(result)
        self.assertEqual(K8sDeployment.objects.get(pk=result.pk).status, 'succeeded')


class WorkerRecoveryTest(StateMachineTestBase):
    """Worker 崩溃后的恢复：绝不自动续跑。"""

    def test_recover_marks_running_tasks_as_worker_interrupted(self):
        """遗留的 running 要标成失败并注明 Worker 中断，交人工判断。"""
        self.add_nodes(3)
        self.deployment.status = 'running'
        self.deployment.worker_id = 'dead-worker'
        self.deployment.started_at = timezone.now()
        self.deployment.save(update_fields=['status', 'worker_id', 'started_at', 'updated_at'])

        count = state_machine.recover_interrupted_tasks()
        self.assertEqual(int(count or 0), 1)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'worker_interrupted')
        self.assertIn('Worker', self.deployment.failure_reason)

    def test_recover_leaves_terminal_tasks_untouched(self):
        """终态任务不被动过：恢复只处理 running。"""
        self.deployment.status = 'succeeded'
        self.deployment.save(update_fields=['status', 'updated_at'])
        self.assertEqual(int(state_machine.recover_interrupted_tasks() or 0), 0)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'succeeded')

    def test_recover_reports_requested_cancel_but_still_fails(self):
        """点过取消后 Worker 又崩掉：仍按失败处理，但必须让用户知道取消请求收到了。

        远端可能被改到一半，不能因为「用户本来就想停」就给一个看起来干净的已取消状态。
        """
        self.deployment.status = 'running'
        self.deployment.cancel_requested = True
        self.deployment.save(update_fields=['status', 'cancel_requested', 'updated_at'])
        state_machine.recover_interrupted_tasks()
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.status, 'failed')
        self.assertEqual(self.deployment.error_code, 'worker_interrupted')
        self.assertIn('取消请求已收到', self.deployment.failure_reason)


class WorkerLockTest(StateMachineTestBase):
    """Worker 文件锁排他：第二个 Worker 必须立刻退出而不是并发执行。"""

    def test_second_lock_is_refused(self):
        """同机第二个 Worker 抢不到锁时抛 WorkerBusy 并立即退出，而不是并发跑任务。"""
        first = state_machine.WorkerLock()
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        with self.assertRaises(state_machine.WorkerBusy):
            with state_machine.WorkerLock():
                self.fail('第二个 Worker 不应该拿到锁')

    def test_acquire_returns_false_when_busy(self):
        """acquire 本身返回布尔值，供调用方在不抛异常的场景里判断。"""
        first = state_machine.WorkerLock()
        first.acquire()
        self.addCleanup(first.release)
        self.assertFalse(state_machine.WorkerLock().acquire())

    def test_lock_is_released_after_release(self):
        """释放后可以重新获取，便于 --once 模式反复拉起。"""
        lock = state_machine.WorkerLock()
        lock.acquire()
        lock.release()
        second = state_machine.WorkerLock()
        second.acquire()
        second.release()

    def test_context_manager_releases_on_error(self):
        """异常退出时上下文管理器也要释放锁，否则 Worker 一崩就再也起不来。"""
        with self.assertRaises(RuntimeError):
            with state_machine.WorkerLock():
                raise RuntimeError('boom')
        lock = state_machine.WorkerLock()
        lock.acquire()
        lock.release()

    def test_holder_reports_the_process_holding_the_lock(self):
        """锁文件里记着持有者的 pid，抢锁失败时能据此说明是谁占着。"""
        lock = state_machine.WorkerLock()
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertIn(str(os.getpid()), lock.holder())

    def test_command_exits_immediately_when_lock_is_taken(self):
        """命令行入口抢不到锁必须当场退出。

        这条盯的是一类静默失效：``acquire()`` 返回布尔值而不抛异常，早先入口写成
        ``try: lock.acquire() except WorkerBusy``，于是第二个 Worker 照样进了主循环，
        规划 7.2「获取失败立即退出」与验收标准「文件锁可阻止第二实例」都不成立。
        """
        first = state_machine.WorkerLock()
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)

        out = io.StringIO()
        call_command('run_deploy_worker', '--once', stdout=out)

        self.assertIn('已存在运行中的 Worker', out.getvalue())
        # 命令既不能抢走锁，也不能把别人持有的锁释放掉。
        self.assertFalse(state_machine.WorkerLock().acquire())


class WorkspaceCleanupTest(StateMachineTestBase):
    """过期工作区清理。"""

    def _finish_deployment(self):
        """把任务推进到终态，让它的工作区进入可清理范围。

        参数：无。
        返回：无显式返回值。
        副作用：更新任务状态字段。
        """
        self.deployment.status = 'failed'
        self.deployment.finished_at = timezone.now()
        self.deployment.save(update_fields=['status', 'finished_at', 'updated_at'])

    def test_cleanup_removes_stale_workspace(self):
        """任务已终态且工作区超过 TTL 时删除，避免平台机磁盘被 ansible 日志吃满。"""
        workspace = paths.ensure_workspace(self.deployment.pk)
        (workspace / 'stale.txt').write_text('x', encoding='utf-8')
        self._finish_deployment()
        self.assertEqual(state_machine.cleanup_expired_workspaces(ttl_seconds=0), 1)
        self.assertFalse(workspace.exists())

    def test_cleanup_keeps_fresh_workspace(self):
        """未过期的工作区保留，失败现场还能人工翻看。"""
        workspace = paths.ensure_workspace(self.deployment.pk)
        self._finish_deployment()
        self.assertEqual(state_machine.cleanup_expired_workspaces(ttl_seconds=86400), 0)
        self.assertTrue(workspace.exists())

    def test_cleanup_keeps_workspace_of_active_task(self):
        """任务还在排队或执行中时，无论多旧都不清理它的工作区。"""
        workspace = paths.ensure_workspace(self.deployment.pk)
        self.assertEqual(state_machine.cleanup_expired_workspaces(ttl_seconds=0), 0)
        self.assertTrue(workspace.exists())

    def test_cleanup_ignores_non_workspace_entries(self):
        """工作区父目录里的多余条目不会被误删。"""
        paths.ensure_dir(paths.run_root())
        stray = paths.run_root() / 'README.txt'
        stray.write_text('keep me', encoding='utf-8')
        state_machine.cleanup_expired_workspaces(ttl_seconds=0)
        self.assertTrue(stray.exists())


class MachineAssignmentTest(StateMachineTestBase):
    """部署成功后机器归属同步。"""

    def test_successful_deploy_assigns_machines_to_cluster(self):
        """部署成功后机器表要写上集群标识，否则机器管理页看不到它们属于谁。"""
        machine = Machine.objects.create(
            organization=self.org,
            name='machine-1',
            ip_address='192.168.10.10',
            ssh_port=22,
            ssh_user='root',
        )
        nodes = self.add_nodes(3)
        nodes[0].machine = machine
        nodes[0].save(update_fields=['machine', 'updated_at'])

        self.execute()
        machine.refresh_from_db()
        self.assertEqual(machine.cluster_name, 'fsm-ha-cluster')


CLUSTER_HOSTS_SNAPSHOT = (
    '[etcd]\n192.168.10.10\n192.168.10.11\n192.168.10.12\n\n'
    '[kube_master]\n192.168.10.10\n192.168.10.11\n192.168.10.12\n\n'
    '[kube_node]\n192.168.10.10\n192.168.10.11\n192.168.10.12\n'
)

CLUSTER_CONFIG_SNAPSHOT = 'K8S_VER: "1.29.4"\nCLUSTER_NETWORK: "calico"\n'


ADD_CLUSTER_SNAPSHOT = {
    'hosts': CLUSTER_HOSTS_SNAPSHOT,
    'config': CLUSTER_CONFIG_SNAPSHOT,
    'nodes': [
        {
            'name': f'node-{index}',
            'ip_address': f'192.168.10.1{index}',
            'ssh_port': 22,
            'ssh_user': 'root',
            'roles': ['master', 'etcd'],
            'node_name': f'master-0{index}',
        }
        for index in range(3)
    ],
}


class MergedNodeSnapshotTest(StateMachineTestBase):
    """配置版本里的节点清单必须是**集群全量拓扑**，而不是本次任务碰到的那几台。

    这份清单不只用来显示：``state_machine.cluster_master_count`` 拿它判断还能不能再加
    Master，``cluster_ops.node_payloads`` 拿它决定升级要滚动哪些节点、清理要清哪些节点。
    扩容任务手里按设计只有新加的那一台（``run_add_node`` 开头就断言 ``len(nodes) == 1``），
    直接把它当拓扑写进快照会让集群「缩水」成一台 —— 真实故障：94 上那次 add-node 之后，
    集群明明有三台 master，「添加 Master」却被判成拓扑不够 HA 而置灰，升级与清理也会只
    认那台新机器。
    """

    def _make_cluster(self, nodes=None, slug='fsm-merge-cluster'):
        """建一个部署型集群，可选地带上一条含指定节点清单的配置版本。

        参数：`nodes` 表示首个配置版本里的节点清单，为 None 时不建版本；`slug` 表示集群标识。
        返回：返回新建的 K8sCluster。
        副作用：向数据库写入集群，`nodes` 非空时再写入一条配置版本。
        """
        cluster = K8sCluster.objects.create(
            organization=self.org,
            name='快照合并测试集群',
            slug=slug,
            origin='deployed',
            kube_version='1.29.4',
            status='configured',
        )
        if nodes is not None:
            K8sClusterConfigRevision.objects.create(
                organization=self.org,
                cluster=cluster,
                revision=1,
                config_hash='e' * 64,
                snapshot={'nodes': nodes},
            )
        return cluster

    def test_the_nodes_already_in_the_cluster_are_kept(self):
        """本次任务只带一台新机器时，原有的三台 master 必须一台不少地留下，新机器排在末尾。

        顺序也要稳住：``cluster_ops.node_payloads`` 直接按这份清单的顺序构造任务节点，
        每次扩容都把老节点重新排一遍会让任务详情的节点顺序跳来跳去。
        """
        cluster = self._make_cluster(ADD_CLUSTER_SNAPSHOT['nodes'])
        fresh = self.add_nodes(count=1, roles=[['node']], ip_prefix='192.168.20.')
        merged = register.merged_node_snapshot(cluster, fresh)
        self.assertEqual(
            [item['ip_address'] for item in merged],
            ['192.168.10.10', '192.168.10.11', '192.168.10.12', '192.168.20.10'],
        )

    def test_a_node_that_changed_roles_is_replaced_in_place(self):
        """同一台机器在新任务里换了角色时，条目就地更新，不新增一条、位置也不动。"""
        cluster = self._make_cluster(ADD_CLUSTER_SNAPSHOT['nodes'])
        fresh = self.add_nodes(count=1, roles=[['etcd', 'master', 'loadbalancer']], ip_prefix='192.168.10.')
        merged = register.merged_node_snapshot(cluster, fresh)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]['ip_address'], '192.168.10.10')
        self.assertEqual(merged[0]['roles'], ['etcd', 'master', 'loadbalancer'])
        self.assertEqual(merged[1]['roles'], ['master', 'etcd'])

    def test_a_brand_new_cluster_gets_exactly_its_own_nodes(self):
        """没有历史版本的集群（新建任务）行为不变：快照就是本次任务的节点清单。"""
        cluster = self._make_cluster()
        fresh = self.add_nodes(count=3)
        merged = register.merged_node_snapshot(cluster, fresh)
        self.assertEqual(
            [item['ip_address'] for item in merged],
            ['192.168.10.10', '192.168.10.11', '192.168.10.12'],
        )


class AddNodeFlowTest(StateMachineTestBase):
    """添加 Master / 添加 Node：主机行交给 ezctl 写，平台只当场核对自己那一份清单。

    ``ezctl add-node`` / ``add-master`` 先拿地址做重复检查（``grep -E "^$2$|^$2 "``），再把
    ``${@:2}`` 拼成主机行插进对应段。平台若按老做法先写一行，它直接以「already existed」
    退出 2 —— 扩容必然失败，而报出来的是一句跟地址填法看不出关系的 ezctl 错误。

    另有一条边界同样由这个类守着：**新 Master 不进 etcd 组**。``ezctl add-master`` 的剧本里
    没有 etcd 角色，装 etcd 是独立的 ``ezctl add-etcd``；平台提前把它写进 ``[etcd]`` 的话，
    kubeasz 会拿这份清单拼出 apiserver 的 ``--etcd-servers``，这台新机器就会把自己那个空的
    2379 当成一个 etcd 端点。见 ``test_the_new_master_does_not_join_the_etcd_group``。
    """

    def setUp(self):
        """准备一个已部署的集群、一条扩容任务与一台新节点。

        参数：无。
        返回：无显式返回值。
        副作用：写入集群、配置版本、任务与节点快照。
        """
        super().setUp()
        self.cluster = K8sCluster.objects.create(
            organization=self.org,
            name='扩容测试集群',
            slug='fsm-add-cluster',
            origin='deployed',
            kube_version='1.29.4',
            status='configured',
        )
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=1,
            config_hash='f' * 64,
            snapshot=dict(ADD_CLUSTER_SNAPSHOT),
        )
        self.deployment.delete()
        self.deployment = self._make_deployment('add_master', 'fsm-add-task', cluster=self.cluster)
        # 新节点的地址不在快照里，否则 ezctl 会判它 already existed。
        # 角色与 ``cluster_ops.new_machine_payload`` 给的一致：新 Master 只带 master，
        # 它不会被写进 etcd 组（那个函数另有用例钉住）。
        self.node = K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=self.deployment,
            name='new-master',
            ip_address='192.168.20.10',
            ssh_port=22,
            ssh_user='root',
            roles=['master'],
        )
        # 走的是平台真正用的那一份：``_download_release`` 被打桩成返回合成制品，于是本次
        # 任务的 kubeasz 目录就是它，inventory 也落在这里。
        self.hosts_file = paths.hosts_path(self.artifacts_root, self.deployment.cluster_slug)
        self.calls = []
        self.hosts_before_call = ''

    @staticmethod
    def _insert_into_section(hosts_text, section, entry):
        """复刻 ``ezctl`` 往段里插一行的做法（``sed "/\\[段名/a 行"``）。

        参数：`hosts_text` 表示 hosts 文件内容；`section` 表示段名；`entry` 表示要插入的主机行。
        返回：返回修改后的文本。
        副作用：纯计算，返回新文本，不写文件。

        刻意不调用平台自己的函数：这里扮演的是**平台之外**的那一半（ezctl），借用被测方的
        实现会让「平台没预先写这一行」的判定跟着被测代码一起变形。
        """
        lines = str(hosts_text).splitlines()
        header = f'[{section}]'
        index = lines.index(header)
        lines.insert(index + 1, entry)
        return '\n'.join(lines) + '\n'

    def _run(self, insert=True):
        """在打桩前提下跑一次扩容任务。

        参数：`insert` 表示假设的 ezctl 是否像真的那样把主机行写进 inventory。
        返回：返回执行后的任务。
        副作用：完整推进一次扩容任务；假设的 ezctl 会改写 hosts 文件。
        """
        self._patches()
        section = 'kube_master' if self.deployment.task_type == 'add_master' else 'kube_node'

        def on_stage(stage, argv):
            """记录 ezctl 调用，并在需要时复刻它写 inventory 的行为。

            参数：`stage` 表示阶段标识；`argv` 表示命令行列表。
            返回：无显式返回值。
            副作用：记录调用；``insert`` 为真时改写 hosts 文件。
            """
            parts = [str(item) for item in argv]
            self.calls.append(parts)
            if stage not in ('add-master', 'add-node'):
                return
            # argv 形如 [bash, <kubeasz>/ezctl, add-master, <集群>, <地址>, <字段...>]，
            # ezctl 里 ``$2`` 就是 <地址>，插入的行是 ``${@:2}`` 拼出来的那串。
            self.hosts_before_call = self.hosts_file.read_text(encoding='utf-8')
            if not insert:
                return
            inserted = ' '.join(parts[4:])
            self.hosts_file.write_text(
                self._insert_into_section(self.hosts_before_call, section, inserted),
                encoding='utf-8',
            )

        self.fake_runner.on_stage = on_stage
        state_machine.execute_task(self.deployment, worker_id='w', runner_instance=self.fake_runner)
        self.deployment.refresh_from_db()
        return self.deployment

    def entered_entries(self, section):
        """返回某段里以新节点地址开头的条目。

        参数：`section` 表示段名。
        返回：返回条目列表。
        副作用：只读文本。
        """
        entries = state_machine.section_entries(self.hosts_before_call, section)
        return [item for item in entries if item.split(' ')[0] == '192.168.20.10']

    def test_ezctl_gets_the_address_and_the_host_fields_separately(self):
        """地址当 ``$2``、字段跟在后面：两边拼出来的主机行与平台自己写的一字不差。"""
        self._run()
        argv = self.calls[0]
        self.assertEqual(argv[2], 'add-master')
        self.assertEqual(argv[3], self.deployment.cluster_slug)
        self.assertEqual(argv[4], '192.168.20.10')
        self.assertEqual(argv[5], compiler.host_fields(
            self.node, 'kube_master', 'master-04', {'192.168.20.10': {'user': 'root', 'port': 22}},
        ))
        self.assertIn("k8s_nodename='master-04'", argv[5])

    def test_nat_node_gets_its_interface_address_as_the_hostname(self):
        """节点在 NAT 后面时：ezctl 的 ``$2`` 必须是集群内网地址，公网地址只能出现在
        ``ansible_host`` 里 —— 前者要进 etcd 的 ``--initial-cluster``。"""
        self.node.cluster_ip = '172.30.0.7'
        self.node.save(update_fields=['cluster_ip', 'updated_at'])
        self._run()
        argv = self.calls[0]
        self.assertEqual(argv[4], '172.30.0.7')
        self.assertIn('ansible_host=192.168.20.10', argv[5])

    def test_platform_does_not_prewrite_the_section_ezctl_writes(self):
        """ezctl 判重看的就是这一段：平台先写一行等于把它推进失败分支。"""
        self._run(insert=False)
        self.assertEqual(self.entered_entries('kube_master'), [])

    def test_the_new_master_does_not_join_the_etcd_group(self):
        """新 Master 只进 kube_master 段，etcd 段一个字符都不许动。

        ``ezctl add-master`` 跑的 23.addmaster.yml 里没有 etcd 角色：那台机器上不会装 etcd，
        加 etcd 成员是独立操作 ``ezctl add-etcd``。平台若把这台机器写进 ``[etcd]``，kubeasz
        就会拿 ``groups['etcd']`` 拼出新 Master 的 ``--etcd-servers``，里面多出一个指向它自己
        的空 2379；etcd 成员数也从奇数变成偶数。
        """
        self._run()

        self.assertEqual(self.entered_entries('etcd'), [])
        self.assertEqual(
            [item.split(' ')[0] for item in state_machine.section_entries(self.hosts_before_call, 'etcd')],
            ['192.168.10.10', '192.168.10.11', '192.168.10.12'],
        )

    def test_the_scale_task_does_not_rename_the_cluster(self):
        """运维任务不许改集群名。

        运维任务的 display_name 是 ``cluster_ops`` 拼出来的「<集群名> · 添加 Node」这类**任务
        标题**，拿它覆盖集群名会让集群每做一次运维就多叠一层后缀 —— 真实故障：94 上的集群被
        改名成了「k8s-cluster · 添加 Node」，用户在集群列表里看到的就是这串任务名。
        """
        self._run()
        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.name, '扩容测试集群')

    def test_the_revision_keeps_the_whole_cluster_topology(self):
        """扩容后的配置版本要留下完整拓扑：三台 master 加新机器，master 计数不能掉下来。

        快照里的节点清单是后续运维操作唯一的拓扑来源：``cluster_master_count`` 拿它决定还能
        不能再加 Master，``node_payloads`` 拿它决定升级要滚动哪些节点、清理要清哪些机器。
        扩容任务手里只有新加的那一台，直接覆盖会让集群在快照里缩水成一台 —— 真实故障：
        94 上那次 add-node 之后「添加 Master」被置灰，升级与清理也都只认那台新机器。
        """
        self._run()
        revision = K8sClusterConfigRevision.objects.filter(
            cluster=self.cluster,
        ).order_by('-revision').first()
        self.assertEqual(revision.revision, 2)
        self.assertEqual(
            [item['ip_address'] for item in revision.snapshot['nodes']],
            ['192.168.10.10', '192.168.10.11', '192.168.10.12', '192.168.20.10'],
        )
        # 这次扩容加的就是一台 Master，所以计数从 3 涨到 4；按旧口径写成一台的话这里会是 1。
        self.assertEqual(state_machine.cluster_master_count(self.cluster), 4)

    def test_the_log_tells_the_operator_etcd_membership_did_not_change(self):
        """界面上要有一句实话：这台是 control-plane，etcd 成员没变。

        以前这里写的是「已在 inventory 的 etcd 段加入 X」——一句当时的代码也做不到的承诺
        （机器上并没有 etcd）。扩容完到底动没动 etcd，是运维最需要一眼看清的事。
        """
        self._run()

        text = ' '.join(
            item.message for item in K8sDeploymentLog.objects.filter(deployment=self.deployment)
        )
        self.assertIn('etcd 成员保持原样', text)

    def test_missing_entry_aborts_the_task(self):
        """ezctl 没写进去必须当场失败：否则配置版本里少一台机器，下次任务它凭空消失。"""
        task = self._run(insert=False)
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_code, 'inventory_entry_missing')

    def test_successful_add_master_lands_in_the_next_revision(self):
        """成功后配置版本里必须有这台机器，这才是「扩容生效」真正落地的地方。"""
        task = self._run()
        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        revision = K8sClusterConfigRevision.objects.filter(cluster=self.cluster).order_by('-revision').first()
        self.assertEqual(revision.revision, 2)
        self.assertIn('192.168.20.10', revision.snapshot['hosts'])
        # 快照里的角色也不能比机器上多：记成 etcd 会让后来的任务（清理计划的组件计数、
        # 网段继承）以为 cluster 多了一台跑 etcd 的机器。
        added = [item for item in revision.snapshot['nodes'] if item['ip_address'] == '192.168.20.10']
        self.assertEqual([item['roles'] for item in added], [['master']])

    def test_the_next_revision_records_the_cluster_network(self):
        """网段与端口也要跟着固化进新版本。

        下一个运维任务是从这份快照里把集群的真实网段读回来的（``cluster_network_facts``）；
        这一版记成空串或默认值，后来者就会拿着一个与集群无关的网段去跑预检 ——
        而集群配置本身是从上一版还原的，两边说的不是一回事。
        """
        self._run()
        revision = K8sClusterConfigRevision.objects.filter(cluster=self.cluster).order_by('-revision').first()

        self.assertEqual(revision.snapshot['pod_cidr'], self.deployment.pod_cidr)
        self.assertEqual(revision.snapshot['service_cidr'], self.deployment.service_cidr)
        self.assertEqual(revision.snapshot['api_server_port'], self.deployment.api_server_port)

    def test_registry_host_candidates_come_from_the_cluster(self):
        """宿主候选必须来自集群已有节点，不能是这次提交的新机器。

        扩容任务的节点清单里只有新机器，拿它选宿主等于给集群再造一个仓库，还要把全部镜像
        经公网重推一遍。
        """
        with mock.patch.object(state_machine, '_cluster_registry_pool', return_value=None) as pool:
            self._run()
        pool.assert_called_once_with(self.deployment)

    def test_new_node_gets_the_bin_directory_on_its_system_path(self):
        """新机器也要拿到系统 PATH 上的 bin_dir：这台的 prepare 同样只写了它自己的 ~/.bashrc。"""
        with mock.patch.object(state_machine, '_configure_node_path') as configure:
            self._run()

        configure.assert_called_once()
        context, materialized, configured, _credentials = configure.call_args.args
        self.assertEqual([node.ip_address for node in configured], ['192.168.20.10'])
        # 目录从集群当前生效的 inventory 里读（``_configure_node_path`` 内部做的事）。
        hosts_path = str(materialized['hosts_path']).replace('\\', '/')
        self.assertTrue(hosts_path.endswith('fsm-add-cluster/hosts'), hosts_path)

    def test_new_node_also_gets_the_kubectl_config(self):
        """新机器同样要有 ``/root/.kube/config``：这一步的 prepare 会拷一份，但它带幂等标记。

        新机器若是「以前当过节点、后来被清理过」的那台，标记还在，这份配置又不会写 ——
        运维登上这台新机器敲 ``kubectl`` 就还是 connection refused。见 ``node_kubeconfig``。
        """
        with mock.patch.object(state_machine, '_ensure_node_kubeconfig') as ensure:
            self._run()

        ensure.assert_called_once()
        _context, kubeasz_root, slug, nodes, _credentials = ensure.call_args.args
        self.assertEqual(slug, self.deployment.cluster_slug)
        self.assertEqual([node.ip_address for node in nodes], ['192.168.20.10'])
        # 交给它的必须是本次任务真正在用的那个 kubeasz 目录：kubeconfig 是由那儿生成的，
        # 换了目录就读不到、这一步会静默跳过。
        self.assertTrue(Path(paths.hosts_path(kubeasz_root, slug)).is_file())

    def test_new_node_also_gets_the_helm_client(self):
        """新机器同样要有 ``helm``：同一个集群里，命令清单不该因为它是后加进来的而少一个。"""
        with mock.patch.object(state_machine, '_configure_node_helm') as configure:
            self._run()

        configure.assert_called_once()
        _context, _kubeasz_root, materialized, nodes, _credentials = configure.call_args.args
        self.assertEqual([node.ip_address for node in nodes], ['192.168.20.10'])
        # 目录从集群当前生效的 inventory 里读（``_configure_node_helm`` 内部做的事）。
        hosts_path = str(materialized['hosts_path']).replace('\\', '/')
        self.assertTrue(hosts_path.endswith('fsm-add-cluster/hosts'), hosts_path)

    def test_no_helm_step_for_a_cluster_that_did_not_ask_for_it(self):
        """没勾「Helm」的集群扩容时也不该装：那是集群级的配置，一台机器说了不算。"""
        self.deployment.install_helm_cli = False
        self.deployment.save(update_fields=['install_helm_cli', 'updated_at'])

        with mock.patch.object(state_machine, '_configure_node_helm') as configure:
            self._run()

        configure.assert_not_called()

    def test_new_node_also_gets_its_exited_containers_cleaned(self):
        """新节点上同样有 calico 那批跑完就退的 initContainer，没理由因为它是后加的而例外。"""
        with mock.patch.object(state_machine, '_cleanup_node_containers') as cleanup:
            self._run()

        cleanup.assert_called_once()
        context, materialized, cleaned, _credentials = cleanup.call_args.args
        self.assertEqual([node.ip_address for node in cleaned], ['192.168.20.10'])
        hosts_path = str(materialized['hosts_path']).replace('\\', '/')
        self.assertTrue(hosts_path.endswith('fsm-add-cluster/hosts'), hosts_path)

    def test_add_worker_writes_the_node_section_and_leaves_etcd_alone(self):
        """加 Node 走 add-node、只碰 kube_node 段：它不进 etcd，也不该动那份清单。"""
        self.deployment.task_type = 'add_worker'
        self.deployment.save(update_fields=['task_type', 'updated_at'])
        task = self._run()
        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        self.assertEqual(self.calls[0][2], 'add-node')
        self.assertEqual(self.entered_entries('kube_node'), [])
        self.assertEqual(self.entered_entries('etcd'), [])


class UpgradeFlowTest(StateMachineTestBase):
    """升级流水线的前置检查与顺序（规划 10.4）。"""

    def setUp(self):
        """准备一个已部署的集群与一条升级任务。

        参数：无。
        返回：无显式返回值。
        副作用：写入集群、配置版本与任务节点；替换备份服务。

        这条任务是**跨一个次版本**的升级（集群 1.29.4 → 目标 1.30）：平台能表达出来的
        升级只有这一种 —— 配置中心按次版本登记版本，同一次版本内换不出另一套 kubeasz
        制品，而制品不换就等于原地不动。目标版本与制品声明必须成对（这里让合成制品声明
        ``v1.30.1``），否则升级前检查会在「目标制品」那一项直接判死。
        """
        super().setUp()
        self.cluster = K8sCluster.objects.create(
            organization=self.org,
            name='升级测试集群',
            slug='fsm-upgrade-cluster',
            origin='deployed',
            kube_version='1.29.4',
            status='configured',
        )
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=1,
            config_hash='e' * 64,
            snapshot={'hosts': CLUSTER_HOSTS_SNAPSHOT, 'config': CLUSTER_CONFIG_SNAPSHOT},
        )
        self.artifacts_root = kubeasz_fixtures.write_kubeasz_tree(
            self.state_root / 'release-1.30', version='3.6.8', k8s_bin_version='v1.30.1',
        )
        self.deployment.delete()
        self.deployment = self._make_deployment(
            'upgrade', 'fsm-upgrade-task', cluster=self.cluster,
            kubernetes_version='1.30', kubeasz_version='3.6.8',
        )
        self.add_nodes(3)
        # 升级前的强制备份会真的去连 OSS 与目标机，这里换成一次成功的假备份。
        record = mock.Mock(backup_id='bk-upgrade-1', archive_sha256='a' * 64, archive_size=1024)
        for target in ('k8s_deployment.services.backup.create_backup',):
            patcher = mock.patch(target, return_value=record)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _cluster_state(self, versions):
        """构造 probe_cluster 的返回值。

        参数：`versions` 表示每个节点的 kubelet 版本列表。
        返回：返回探测结果字典。
        副作用：纯计算。
        """
        details = [
            {'name': f'node-{index + 1}', 'kubelet_version': value, 'ready': True}
            for index, value in enumerate(versions)
        ]
        return {
            'status': 'available',
            'kube_version': 'v1.29.4',
            'node_count': len(details),
            'node_summary': {
                'total': len(details),
                'ready': len(details),
                'not_ready': [],
                'versions': {value: versions.count(value) for value in set(versions)},
                'details': details,
            },
        }

    def _run(self, cluster_state):
        """在打桩前提下执行一次升级任务。

        参数：`cluster_state` 表示 probe_cluster 的返回值。
        返回：返回执行后的任务对象。
        副作用：完整推进一次升级任务。
        """
        self._patches()
        # FakeRunner 不记录调用，用 on_stage 自己收一份，用来断言「ansible 到底跑没跑」。
        self.stages = []
        self.fake_runner.on_stage = lambda stage, argv: self.stages.append(
            {'stage': stage, 'command': ' '.join(str(item) for item in argv)},
        )
        with mock.patch('k8s_cluster.services.probe_cluster', return_value=cluster_state), mock.patch(
            'k8s_resource.services.resource_actions.drain_pdb_warnings', return_value=[],
        ):
            state_machine.execute_task(
                self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
            )
        self.deployment.refresh_from_db()
        return self.deployment

    def test_version_skew_aborts_before_backup_and_ansible(self):
        """节点 kubelet 版本不一致时，升级在备份与 ansible 之前就停住（规划 10.4）。"""
        state = self._cluster_state(['v1.29.4', 'v1.29.3', 'v1.29.4'])
        with mock.patch('k8s_deployment.services.backup.create_backup') as backup:
            task = self._run(state)
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_code, 'upgrade_version_skew')
        backup.assert_not_called()
        self.assertEqual(self.stages, [])
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=task).values_list('event_type', flat=True)
        )
        self.assertIn('upgrade_precheck_skew', event_types)
        self.assertIn('upgrade_precheck_artifact', event_types)

    def test_healthy_cluster_creates_backup_then_upgrades(self):
        """前置检查通过后才会做升级前备份并调用 ezctl upgrade。"""
        task = self._run(self._cluster_state(['v1.29.4', 'v1.29.4', 'v1.29.4']))
        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        self.assertIn('pre-upgrade-backup', task.completed_stages)
        self.assertIn('upgrade-precheck', task.completed_stages)
        self.assertIn('upgrade', task.completed_stages)
        commands = [item['command'] for item in self.stages]
        self.assertTrue(any(' upgrade ' in command for command in commands), commands)

    def test_cross_minor_upgrade_reports_the_target_artifact_and_config_gap(self):
        """跨次版本升级要记录「用的是目标版本的制品」与配置模板差异，且差异只警告不中止。

        配置模板差异是跨 kubeasz 版本升级唯一的真实未知量：升级不重渲染 config.yml，
        新模板里的变量在旧配置里就是缺的。它必须出现在事件流里（供人事后核对），
        但不能因此挡住一次本来能成的升级。
        """
        task = self._run(self._cluster_state(['v1.29.4', 'v1.29.4', 'v1.29.4']))
        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        events = {
            item.event_type: item
            for item in K8sDeploymentEvent.objects.filter(deployment=task).order_by('id')
        }
        artifact = events['upgrade_precheck_artifact']
        self.assertEqual(artifact.level, 'info')
        self.assertEqual(artifact.detail['kubeasz_binary_version'], '1.30.1')
        config = events['upgrade_precheck_config']
        self.assertEqual(config.level, 'warn')
        self.assertEqual(config.detail['kubeasz_version'], '3.6.8')
        self.assertTrue(config.detail['template_extra'])

    def test_same_version_target_fails_instead_of_reporting_success(self):
        """目标版本与集群当前版本相同时必须失败：这里以前只发警告，任务会报成功而版本没变。

        这是「空转」那条路的最后一道闸门。任务已经排在队列里，只靠提交期的校验不够 ——
        集群版本可能在排队期间被别的途径改回原样。
        """
        self.deployment.kubernetes_version = '1.29.4'
        self.deployment.save(update_fields=['kubernetes_version', 'updated_at'])
        with mock.patch('k8s_deployment.services.backup.create_backup') as backup:
            task = self._run(self._cluster_state(['v1.29.4', 'v1.29.4', 'v1.29.4']))
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_code, 'upgrade_same_version')
        backup.assert_not_called()
        self.assertEqual(self.stages, [])

    def test_minor_jump_aborts_before_touching_the_cluster(self):
        """一次跨两个次版本要中止：Kubernetes 不允许跳 minor，逐级升级是唯一的路。"""
        self.deployment.kubernetes_version = '1.31'
        self.deployment.save(update_fields=['kubernetes_version', 'updated_at'])
        with mock.patch('k8s_deployment.services.backup.create_backup') as backup:
            task = self._run(self._cluster_state(['v1.29.4', 'v1.29.4', 'v1.29.4']))
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_code, 'upgrade_minor_jump')
        backup.assert_not_called()
        self.assertEqual(self.stages, [])

    def test_downgrade_aborts_before_touching_the_cluster(self):
        """降到更低的版本要中止：回到旧版本只能从备份恢复。"""
        self.deployment.kubernetes_version = '1.29'
        self.deployment.save(update_fields=['kubernetes_version', 'updated_at'])
        with mock.patch('k8s_deployment.services.backup.create_backup') as backup:
            task = self._run(self._cluster_state(['v1.29.4', 'v1.29.4', 'v1.29.4']))
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_code, 'upgrade_downgrade')
        backup.assert_not_called()
        self.assertEqual(self.stages, [])

    def test_registry_host_stays_where_the_cluster_put_it(self):
        """升级时宿主必须还是原来那台：换一台的话新镜像推到了别处，而节点仍在从原地拉。"""
        with mock.patch.object(state_machine, '_cluster_registry_pool', return_value=None) as pool:
            task = self._run(self._cluster_state(['v1.29.4', 'v1.29.4', 'v1.29.4']))
        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        pool.assert_called_once_with(self.deployment)


class UpgradeProxyModeRewriteTest(StateMachineTestBase):
    """升级时改写集群变量文件里的 K8S_VER 与 PROXY_MODE。

    升级**不重新渲染**集群配置，只把工作区里这两个变量换掉。``K8S_VER`` 不换的话
    ``93.upgrade.yml`` 开头的版本守卫会直接 UPDATE ABORT；``PROXY_MODE`` 则决定节点上
    重新生成的 kube-proxy 配置用哪种转发模式 —— 跨次版本升级换的 kubeasz 制品不再支持
    ipvs，用户改选新模式这件事必须落到文件上，只记在任务里等于没改。
    """

    def setUp(self):
        """准备一份集群配置与一个任务上下文。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时 kubeasz 目录并写入集群的 config.yml 与 hosts。
        """
        super().setUp()
        self.kubeasz_root = self.state_root / 'kubeasz-with-cluster'
        self.cluster_dir = paths.cluster_dir(self.kubeasz_root, self.deployment.cluster_slug)
        self.cluster_dir.mkdir(parents=True)
        self.config_file = self.cluster_dir / 'config.yml'
        self.hosts_file = self.cluster_dir / 'hosts'
        self.workspace = self.state_root / 'workspace'
        self.context = state_machine.TaskContext(self.deployment, self.workspace)

    def write_cluster_files(self, hosts_text, mode, config_text='K8S_VER: "1.29.4"\n'):
        """写入集群配置并把任务上的 kube-proxy 模式设成指定值。

        参数：`hosts_text` 表示 hosts 文本；`mode` 表示任务要用的模式；`config_text` 表示 config.yml 内容。
        返回：无显式返回值。
        副作用：写文件并更新任务记录。
        """
        self.config_file.write_text(config_text, encoding='utf-8')
        self.hosts_file.write_text(hosts_text, encoding='utf-8')
        self.deployment.kube_proxy_mode = mode
        self.deployment.save(update_fields=['kube_proxy_mode', 'updated_at'])

    def rewrite(self, target_version='1.30'):
        """执行一次配置改写。

        参数：`target_version` 表示目标 Kubernetes 版本。
        返回：返回被改写后的 config.yml 路径。
        副作用：改写工作区里的集群变量文件并写入事件。
        """
        return state_machine._upgrade_cluster_config(
            self.context, self.kubeasz_root, self.deployment, target_version,
        )

    def test_k8s_ver_is_replaced_with_the_target(self):
        """目标版本要写进 config.yml，否则剧本开头的版本守卫会中止升级。"""
        self.write_cluster_files('[all:vars]\nPROXY_MODE="ipvs"\n', 'ipvs')
        self.rewrite('1.30')
        self.assertIn('K8S_VER: "1.30"', self.config_file.read_text(encoding='utf-8'))

    def test_proxy_mode_is_rewritten_when_the_user_changed_it(self):
        """用户改过模式时，hosts 里那一行要被换成新模式。"""
        self.write_cluster_files('[all:vars]\nPROXY_MODE="ipvs"\n\n[kube_master]\n192.168.10.10\n', 'nftables')
        self.rewrite()
        hosts_text = self.hosts_file.read_text(encoding='utf-8')
        self.assertEqual(compiler.inventory_variable(hosts_text, 'PROXY_MODE'), 'nftables')
        # 只动那一行，别的内容原样保留。
        self.assertIn('[kube_master]\n192.168.10.10', hosts_text)

    def test_proxy_mode_is_left_alone_when_it_did_not_change(self):
        """模式没变时不碰 hosts：升级任务默认继承集群当前模式，不该在文件里反复重写。"""
        original = '[all:vars]\nPROXY_MODE="ipvs"\n\n[kube_master]\n192.168.10.10\n'
        self.write_cluster_files(original, 'ipvs')
        self.rewrite()
        self.assertEqual(self.hosts_file.read_text(encoding='utf-8'), original)

    def test_missing_proxy_mode_records_a_warning(self):
        """hosts 里没有 PROXY_MODE 时改不动，要留下警告事件而不是静默放过。

        平台渲染的 hosts 一定带着这一行，所以这是被人工改过的现场：任务会带着新模式跑完，
        而节点上仍是旧的那套。升级本身不因此中止（kube-proxy 配置由 kubeasz 自己渲染）。
        """
        self.write_cluster_files('[kube_master]\n192.168.10.10\n', 'nftables')
        self.rewrite()
        event = K8sDeploymentEvent.objects.get(deployment=self.deployment, event_type='upgrade_proxy_mode_skipped')
        self.assertEqual(event.level, 'warn')
        self.assertEqual(event.detail['kube_proxy_mode'], 'nftables')
        self.assertEqual(self.hosts_file.read_text(encoding='utf-8'), '[kube_master]\n192.168.10.10\n')

    def test_missing_k8s_ver_aborts_the_task(self):
        """config.yml 里没有 K8S_VER 时中止：这种集群配置改不动，升级注定升不动。"""
        self.write_cluster_files(
            '[all:vars]\nPROXY_MODE="ipvs"\n', 'ipvs', config_text='CLUSTER_NETWORK: "calico"\n',
        )
        with self.assertRaises(state_machine.TaskAborted) as caught:
            self.rewrite()
        self.assertIn('K8S_VER', str(caught.exception))


class DestroyFlowTest(StateMachineTestBase):
    """清理集群：跑 ezctl destroy，再删掉 kubeasz 目录、卸载节点侧仓库、把机器解绑。

    这条流程要解决的是一个很具体的死结：集群装烂了要重装，而重装会被两道闸门同时挡死 ——
    机器表的归属还挂在旧集群上、预检又发现 6443/2379/10250 全在监听。清理就是拆掉这两道闸门。
    """

    def setUp(self):
        """准备一个已登记的集群、三台已绑定的机器与一条清理任务。

        参数：无。
        返回：无显式返回值。
        副作用：写入集群、配置版本、机器与任务节点；把机器挂到节点快照上。
        """
        super().setUp()
        self.cluster = K8sCluster.objects.create(
            organization=self.org,
            name='清理测试集群',
            slug='fsm-destroy-cluster',
            origin='deployed',
            kube_version='1.29.4',
            node_count=3,
            status='configured',
        )
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=1,
            config_hash='d' * 64,
            snapshot={'hosts': CLUSTER_HOSTS_SNAPSHOT, 'config': CLUSTER_CONFIG_SNAPSHOT},
        )
        self.deployment.delete()
        self.deployment = self._make_deployment('destroy', 'fsm-destroy-task', cluster=self.cluster)
        self.nodes = self.add_nodes(3)
        # 机器表的归属是「这批 IP 现在归谁」的唯一记录，清理流程要按它复核（规划 2.5）。
        self.machines = []
        for node in self.nodes:
            machine = Machine.objects.create(
                organization=self.org,
                name=node.name,
                ip_address=node.ip_address,
                ssh_port=22,
                ssh_user='root',
                cluster_name=self.cluster.slug,
            )
            node.machine = machine
            node.save(update_fields=['machine'])
            self.machines.append(machine)

    def _run(self, remove_path_error=None, network_error=None, line_feeder=None):
        """在打桩前提下执行一次清理任务。

        参数：`remove_path_error` 表示让撤销 PATH 配置时抛出的异常，用来验证它是否被容忍；
            `network_error` 表示让删除虚拟网卡时抛出的异常，同样用来验证容忍度；
            `line_feeder` 表示 ``ezctl`` 那一步开跑时收到的逐行回调，用来喂几行 ansible 输出进去。
        返回：返回执行后的任务对象。
        副作用：完整推进一次清理任务，并记录 FakeRunner 收到的阶段命令。
        """
        self._patches()
        self.stages = []
        self.fake_runner.on_stage = lambda stage, argv: self.stages.append(
            {'stage': stage, 'command': ' '.join(str(item) for item in argv)},
        )
        # 真机上组件进度就是从 ``ezctl`` 那一步的输出里解析出来的。想看这条链有没有接上的用例，
        # 从这里把逐行回调截下来喂几行 banner 进去 —— 监听器自己的逻辑由 test_destroy_plan.py 覆盖，
        # 这里只管「它到底挂没挂上去」。
        if line_feeder is not None:
            real_run_stage = self.fake_runner.run_stage

            def run_stage(stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                          on_idle=None, on_tick=None):
                if stage == 'destroy.components':
                    line_feeder(on_line)
                return real_run_stage(
                    stage, argv, cwd, env=env, on_line=on_line, timeout=timeout,
                    is_cancelled=is_cancelled, on_idle=on_idle,
                          on_tick=on_tick,
                )

            self.fake_runner.run_stage = run_stage
        # 卸载节点侧仓库要真连 SSH。它是否被调用、调了几次、传了哪些节点，都在这里收口；
        # 命令本身的幂等性在 test_node_registry.py 里覆盖。
        patcher = mock.patch.object(
            node_registry, 'uninstall',
            return_value={'removed': True, 'data_dir': '/opt/kube/registry'},
        )
        self.uninstall = patcher.start()
        self.addCleanup(patcher.stop)
        # 撤销系统 PATH 配置同样要真连 SSH。命令本身的幂等性在 test_node_path.py 里覆盖，
        # 这里只关心它被调了几次、拿的是哪个 bin_dir。
        patcher = mock.patch.object(
            node_path, 'remove_node',
            return_value={'profile': True, 'environment': True},
            side_effect=remove_path_error,
        )
        self.remove_path = patcher.start()
        self.addCleanup(patcher.stop)
        # 删节点上的集群虚拟网卡也要真连 SSH。命令本身的形状在 test_node_network.py 里覆盖，
        # 这里只关心它被调了几次、拿的是哪些节点、以及失败时任务是否照样走完。
        patcher = mock.patch.object(
            node_network, 'cleanup',
            return_value={'removed': 3, 'left': [], 'clean': True},
            side_effect=network_error,
        )
        self.clean_network = patcher.start()
        self.addCleanup(patcher.stop)
        # _patches 里那份 _download_release 拿不到句柄，这里再盖一层，只为断言 with_home。
        patcher = mock.patch.object(
            state_machine, '_download_release', return_value=self.artifacts_root,
        )
        self.download = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            state_machine, '_run_preflight',
            return_value={'ok': True, 'checks': [], 'hard_failures': []},
        )
        self.preflight = patcher.start()
        self.addCleanup(patcher.stop)

        state_machine.execute_task(
            self.deployment, worker_id='test-worker', runner_instance=self.fake_runner,
        )
        self.deployment.refresh_from_db()
        return self.deployment

    def test_destroy_cleans_nodes_and_marks_cluster_destroyed(self):
        """清理成功后：ezctl 收到 destroy、集群置为已销毁、标识清空、机器解绑。"""
        task = self._run()

        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        commands = [item['command'] for item in self.stages]
        self.assertTrue(
            any(' destroy fsm-destroy-cluster' in command for command in commands), commands,
        )
        # 记账的是平台自己拆出来的那七个动作，不是 ezctl 的步骤码（清理只跑一条 ezctl destroy）。
        self.assertIn('destroy.components', task.completed_stages)
        for key in (
            'destroy.targets', 'destroy.components', 'destroy.network', 'destroy.config',
            'destroy.registry', 'destroy.path', 'destroy.records',
        ):
            self.assertIn(key, task.completed_stages)
        self.assertEqual(task.current_stage, 'destroy.records')

        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.status, 'destroyed')
        # 标识必须清空：留着它，同名重装会捞到这条记录并原地复活（见 _mark_cluster_destroyed）。
        self.assertEqual(self.cluster.slug, '')
        self.assertEqual(self.cluster.node_count, 0)
        for machine in self.machines:
            machine.refresh_from_db()
            self.assertEqual(machine.cluster_name, '')
            self.assertEqual(machine.normalized_roles(), [])

    def test_destroy_does_not_run_preflight(self):
        """清理不跑预检：要清掉的集群端口必然在监听，跑了等于把自己挡在门外。"""
        self._run()
        self.preflight.assert_not_called()

    def test_destroy_does_not_prepare_kubeasz_home(self):
        """清理只用 kubeasz 的源码剧本，不准备载体二进制：集群已经坏了，不该卡在准备步骤上。"""
        self._run()
        self.download.assert_called_once()
        self.assertFalse(self.download.call_args.kwargs.get('with_home', True))

    def test_destroy_removes_cluster_directory_but_keeps_clusters_root(self):
        """删掉的是 clusters 下面那一层；clusters 本身不能碰。"""
        self._run()
        self.assertFalse(paths.cluster_dir(self.artifacts_root, 'fsm-destroy-cluster').exists())
        self.assertTrue((Path(self.artifacts_root) / 'clusters').exists())

    def test_destroy_uninstalls_registry_on_every_target(self):
        """节点侧仓库是平台自己装的，kubeasz 的清理剧本不认识它，必须逐台卸。"""
        task = self._run()

        self.assertEqual(self.uninstall.call_count, len(self.nodes))
        called_ips = {call.args[1].ip_address for call in self.uninstall.call_args_list}
        self.assertEqual(called_ips, {node.ip_address for node in self.nodes})
        self.assertEqual(task.result_summary['registry_data_dir'], '/opt/kube/registry')

    def test_destroy_restores_node_path_on_every_target(self):
        """平台加进系统 PATH 的两处配置要对称地撤掉，否则重装前的机器越攒越多平台痕迹。"""
        task = self._run()

        self.assertEqual(self.remove_path.call_count, len(self.nodes))
        called_ips = {call.args[1].ip_address for call in self.remove_path.call_args_list}
        self.assertEqual(called_ips, {node.ip_address for node in self.nodes})
        # 快照里的 inventory 没写 bin_dir，撤的必须是默认值 —— 与安装时加进去的那个一致。
        self.assertEqual(self.remove_path.call_args.args[2], compiler.DEFAULT_BIN_DIR)
        self.assertEqual(task.result_summary['node_path_uninstall_failed'], [])

    def test_destroy_restores_the_bin_dir_declared_in_the_snapshot(self):
        """操作员把 bin_dir 改到别处时，撤的也得是那一个：用默认值去摘会摘错目录。"""
        K8sClusterConfigRevision.objects.filter(cluster=self.cluster).update(
            snapshot={
                'hosts': f'[all:vars]\nbin_dir="/data/kube/bin"\n\n{CLUSTER_HOSTS_SNAPSHOT}',
                'config': CLUSTER_CONFIG_SNAPSHOT,
            },
        )

        self._run()

        self.assertEqual(self.remove_path.call_args.args[2], '/data/kube/bin')

    def test_destroy_survives_a_node_that_cannot_be_reached(self):
        """连不上某台节点不该判失败：那只说明 PATH 配置留着，集群本身清干净了。"""
        task = self._run(remove_path_error=node_path.NodePathError('node_path_failed', '连不上'))

        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        self.assertEqual(
            task.result_summary['node_path_uninstall_failed'],
            [node.ip_address for node in self.nodes],
        )
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(deployment=task, stream='system').values_list('message', flat=True)
        )
        self.assertIn(node_path.PROFILE_PATH, messages)

    def test_destroy_removes_cluster_virtual_interfaces_on_every_target(self):
        """kubeasz 的清理剧本不删虚拟网卡：平台必须逐台补上，否则下次部署的网段预检会硬失败。"""
        task = self._run()

        self.assertEqual(self.clean_network.call_count, len(self.nodes))
        called_ips = {call.args[1].ip_address for call in self.clean_network.call_args_list}
        self.assertEqual(called_ips, {node.ip_address for node in self.nodes})
        self.assertEqual(task.result_summary['node_network_cleanup_failed'], [])
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=task).values_list('event_type', flat=True)
        )
        # 网卡已经由平台删掉，不能再提示操作员「重启机器」——那是上游剧本的提示，在这里不成立。
        self.assertIn('destroy_completed', event_types)
        self.assertNotIn('destroy_reboot_required', event_types)

    def test_destroy_survives_a_node_whose_interfaces_cannot_be_removed(self):
        """删网卡失败只记警告：残留一张网卡不影响重新部署，为它判失败会让人以为什么都没清掉。"""
        task = self._run(
            network_error=node_network.NodeNetworkError('node_network_cleanup_failed', '连不上'),
        )

        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        self.assertEqual(
            task.result_summary['node_network_cleanup_failed'],
            [node.ip_address for node in self.nodes],
        )
        messages = '\n'.join(
            K8sDeploymentLog.objects.filter(deployment=task, stream='system').values_list('message', flat=True)
        )
        self.assertIn('虚拟网卡', messages)

    def test_destroy_announces_the_component_plan(self):
        """开跑前把这次要清哪几类组件写进事件：页面上的清单要有出处，操作员也能对着它核对现场。"""
        task = self._run()

        event = K8sDeploymentEvent.objects.filter(
            deployment=task, event_type='destroy_plan',
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(
            event.detail['components'],
            ['etcd', 'control-plane', 'node-agents', 'runtime', 'cni', 'node-env', 'hosts-entry'],
        )
        self.assertIn('etcd', event.message)

    def test_destroy_wires_ansible_output_into_the_component_watcher(self):
        """``ezctl`` 那一步的逐行回调上必须挂着组件监听器。

        真机上组件进度就是从这些行里解析出来的。接错位置（挂在别的阶段、或者干脆没传）时任务
        照样成功，只是页面永远停在「待清理」—— 这种断链只有在喂进去一行真输出时才看得出来。
        """
        captured = []

        def feeder(on_line):
            captured.append(on_line)
            on_line('stdout', 'TASK [clean : stop and disable etcd service] ***')

        task = self._run(line_feeder=feeder)

        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], destroy_plan.DestroyProgressWatcher)
        states = [
            (event.detail['component'], event.detail['state'])
            for event in K8sDeploymentEvent.objects.filter(
                deployment=task, event_type='destroy_component',
            ).order_by('id')
        ]
        self.assertIn(('etcd', 'running'), states)
        # ezctl 成功退出时兜底把剩下的全部标成完成，且 etcd 不会被重复写一次 done。
        self.assertIn(('etcd', 'done'), states)
        self.assertEqual([state for key, state in states if key == 'etcd'], ['running', 'done'])
        self.assertEqual(
            {key for key, state in states if state == 'done'},
            {'etcd', 'control-plane', 'node-agents', 'runtime', 'cni', 'node-env', 'hosts-entry'},
        )

    def test_machine_unbinding_is_audited_as_platform_action(self):
        """解绑是平台自己在 Worker 里做的，没有请求也要留痕，执行人记 system。"""
        from platform_logs.models import AuditLog

        self._run()
        records = AuditLog.objects.filter(
            action='Machine.unassign', resource__in=[str(machine) for machine in self.machines],
        )
        self.assertEqual(records.count(), len(self.machines))
        self.assertEqual({item.actor for item in records}, {'system'})

    def test_node_owned_by_another_cluster_is_skipped(self):
        """归属已经转给别的集群的机器不能被清：拿过期快照去清，清掉的是别人的机器。"""
        foreign = self.machines[2]
        foreign.cluster_name = '别人的集群'
        foreign.save(update_fields=['cluster_name'])

        task = self._run()

        self.assertEqual(task.status, 'succeeded', task.failure_reason)
        self.assertEqual(
            task.result_summary['destroyed_nodes'],
            [node.ip_address for node in self.nodes[:2]],
        )
        self.assertEqual(self.uninstall.call_count, 2)
        event_types = set(
            K8sDeploymentEvent.objects.filter(deployment=task).values_list('event_type', flat=True)
        )
        self.assertIn('destroy_nodes_skipped', event_types)

    def test_all_nodes_owned_by_others_aborts_without_touching_anything(self):
        """一台都不该清时必须中止，而不是留下一个「成功但什么都没清」的任务。"""
        Machine.objects.update(cluster_name='别人的集群')

        task = self._run()

        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.error_code, 'no_target_nodes')
        self.uninstall.assert_not_called()
        self.assertEqual([item['command'] for item in self.stages], [])

    def test_destroyed_cluster_cannot_be_destroyed_again(self):
        """第二次清理必须被拒：标识已经清空，拿不到集群目录，也确定不了用哪套 inventory。"""
        self._run()
        self.deployment.refresh_from_db()
        self.deployment.status = 'queued'
        self.deployment.error_code = ''
        self.deployment.failure_reason = ''
        self.deployment.save(update_fields=['status', 'error_code', 'failure_reason'])

        again = self._run()

        self.assertEqual(again.status, 'failed')
        self.assertEqual(again.error_code, 'cluster_destroyed')


class StageNarrationTest(StateMachineTestBase):
    """阶段旁白：进度页要看得懂「现在在做什么、还要等多久」。

    这些旁白回答的是 192.168.1.94 上第一次部署时的问题 —— 任务其实在正常跑，但页面
    两分钟不动、日志一行都没有，用户只能猜是不是卡死了。两类旁白缺一不可：开跑前说清
    「接下来可能安静很久」，真的安静下来之后再说「还在跑、已经等了多久」。
    """

    def context_of(self, workspace='ws'):
        """建一个指向当前任务的上下文。

        参数：`workspace` 表示工作区目录名。
        返回：返回 ``TaskContext`` 实例。
        副作用：读取日志表里已有的最大序号。
        """
        return state_machine.TaskContext(self.deployment, self.state_root / workspace)

    def logs_of_deployment(self):
        """取本任务的全部日志文本。

        参数：无。
        返回：返回按 seq 升序排列的日志文本列表。
        副作用：只读数据库。
        """
        return [
            row.message for row in
            K8sDeploymentLog.objects.filter(deployment=self.deployment).order_by('seq')
        ]

    def test_say_lands_in_the_log_without_waiting_for_more_output(self):
        """旁白必须立刻落库：它出现的时机正是「没有输出」，跟着缓冲走就等于没写。"""
        context = self.context_of()
        context.begin_stage('01.prepare', '系统准备')
        context.say('平台正在准备，请稍候')

        self.assertIn('平台正在准备，请稍候', self.logs_of_deployment())

    def test_narration_does_not_become_an_event(self):
        """旁白只进日志表：每 30 秒一条的静默提示都记成事件，会把真正的进度冲没。"""
        context = self.context_of()
        context.begin_stage('01.prepare', '系统准备')
        context.say('平台正在准备，请稍候')

        event_types = list(
            K8sDeploymentEvent.objects.filter(deployment=self.deployment)
            .values_list('event_type', flat=True)
        )
        self.assertEqual(event_types, ['stage_started'])

    def test_stage_started_event_carries_the_chinese_title(self):
        """阶段开始事件带上中文标题，进度页的「正在执行：X」才有名字可取。"""
        context = self.context_of()
        context.begin_stage('artifacts', '准备 kubeasz 制品')

        event = K8sDeploymentEvent.objects.get(deployment=self.deployment, event_type='stage_started')
        self.assertEqual(event.detail['title'], '准备 kubeasz 制品')
        self.assertEqual(event.message, '开始 准备 kubeasz 制品')

    def test_idle_narration_names_the_stage_and_the_wait(self):
        """静默提示要说清「哪一步、等了多久」，而不是干巴巴一句「仍在执行」。"""
        context = self.context_of()
        context.begin_stage('01.prepare', '系统准备')
        state_machine.Pipeline(context, runner.FakeRunner()).narrate_idle(45)

        message = self.logs_of_deployment()[-1]
        self.assertIn('系统准备', message)
        self.assertIn('已连续 45 秒没有新输出', message)
        self.assertIn('本阶段已运行', message)

    def test_every_ezctl_stage_explains_the_upcoming_silence(self):
        """每条 ezctl 命令开跑前就说清楚：接下来可能几分钟没有输出，这是正常的。"""
        self.add_nodes(3)
        self.execute()

        announced = [text for text in self.logs_of_deployment() if '可能几分钟没有输出' in text]
        expected = runner.stage_specs_for(self.deployment.task_type, self.deployment)
        self.assertEqual(len(announced), len(expected))

    def test_tick_writes_buffered_logs_without_more_output(self):
        """空转一圈就要把攒着的日志落库。

        现场（192.168.1.94，集群备份）：进度页的最后一行停在 ``TASK [get etcd cluster status]``，
        而盘上的原始日志已经写到后面的任务了。原因是 ``log`` 只在**有新行进来**时才检查
        落库的时间阈值，剧本一沉默，最后一批日志就永远留在内存里 —— 恰恰是用户最需要
        看到它的时候。``tick`` 由执行器的读取循环在空转时调用，负责把这一批落下去。
        """
        context = self.context_of()
        context.begin_stage('backup', '集群备份')
        context.log('stdout', 'TASK [get etcd cluster status]')
        context.log('stdout', 'TASK [make a backup on the etcd node]')
        self.assertEqual(self.logs_of_deployment(), [], '没到阈值就先落库了，这条用例的前提不成立')

        context.tick()
        self.assertEqual(
            self.logs_of_deployment(),
            ['TASK [get etcd cluster status]', 'TASK [make a backup on the etcd node]'],
        )

    def test_tick_refreshes_the_heartbeat_while_nothing_arrives(self):
        """远端沉默期间心跳也要继续走：任务看起来得像个还活着的任务。"""
        context = self.context_of()
        context.begin_stage('backup', '集群备份')
        self.deployment.heartbeat_at = None
        self.deployment.save(update_fields=['heartbeat_at', 'updated_at'])

        context.tick()
        self.deployment.refresh_from_db()
        self.assertIsNotNone(self.deployment.heartbeat_at)

    def test_source_download_is_announced_and_reports_progress(self):
        """制品下载前先打招呼，并把进度回调一路传到下载函数上。

        源码包与载体镜像动辄几百 MB，下载期间 ansible 一个字都不会说 —— 这条道歉话与那个
        回调就是那段时间里唯一的动静（下载函数本身在 ``test_stage_feedback`` 里覆盖）。
        """
        context = self.context_of()
        with mock.patch.object(
            artifacts, 'ensure_kubeasz_source', return_value=self.artifacts_root,
        ) as ensure, mock.patch.object(state_machine, '_apply_addon_overrides'):
            state_machine._download_release(context, self.deployment, runner.FakeRunner(), with_home=False)

        announced = [text for text in self.logs_of_deployment() if text.startswith('正在准备 kubeasz 源码包')]
        self.assertEqual(len(announced), 1)
        self.assertTrue(callable(ensure.call_args.kwargs['on_progress']))
        # 回调报出来的每一句话都该带上「正在下的是什么」，否则和没报一样。
        said = []
        context.say = said.append
        ensure.call_args.kwargs['on_progress'](1024, 2048)
        self.assertIn('kubeasz 3.6.7 源码包', said[-1])
