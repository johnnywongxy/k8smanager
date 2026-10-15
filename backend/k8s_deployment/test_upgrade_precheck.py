"""升级前置检查的单元测试（规划 10.4）。

这些用例只打桩「平台之外」的东西：Kubernetes API（``probe_cluster``）与工作负载列表
（``drain_pdb_warnings``）。版本解析、skew 判定、制品版本比对与「该中止还是该警告」的
判断全部走真实代码，因为那些判断本身就是这里要守护的对象。
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from k8s_cluster.models import K8sCluster, K8sClusterConfigRevision
from k8s_deployment.models import K8sDeployment, K8sDeploymentEvent, K8sDeploymentNode
from k8s_deployment.services import state_machine, upgrade_precheck
from ops.models import Organization

from . import kubeasz_fixtures


def node_state(name, version, ready=True):
    """构造一个节点级探测结果。

    参数：`name` 表示节点名；`version` 表示 kubelet 版本；`ready` 表示是否 Ready。
    返回：返回 ``summarize_nodes`` 形状的字典。
    副作用：纯计算，不访问 API。
    """
    return {'name': name, 'kubelet_version': version, 'ready': bool(ready)}


def cluster_state(nodes, api_version='v1.29.4'):
    """按节点列表拼出一个 ``probe_cluster`` 形状的返回值。

    参数：`nodes` 表示节点级探测结果列表；`api_version` 表示 API Server 版本。
    返回：返回探测结果字典。
    副作用：纯计算，不访问 API。
    """
    versions = {}
    for item in nodes:
        key = item['kubelet_version']
        versions[key] = versions.get(key, 0) + 1
    return {
        'status': 'available',
        'kube_version': api_version,
        'node_count': len(nodes),
        'node_summary': {
            'total': len(nodes),
            'ready': sum(1 for item in nodes if item['ready']),
            'not_ready': [item['name'] for item in nodes if not item['ready']],
            'versions': versions,
            'details': nodes,
        },
    }


class ParseVersionTest(TestCase):
    """parse_version 的解析测试。"""

    def test_plain_and_prefixed_versions(self):
        """带不带 v、带不带补丁号都要能解析。"""
        self.assertEqual(upgrade_precheck.parse_version('v1.29.4'), (1, 29, 4))
        self.assertEqual(upgrade_precheck.parse_version('1.29'), (1, 29, 0))
        self.assertEqual(upgrade_precheck.parse_version(' 1.30.12 '), (1, 30, 12))

    def test_build_suffix_ignored(self):
        """kubelet 会上报 v1.29.4+k3s1 这样的构建后缀，比对时不能算成不同版本。"""
        self.assertEqual(upgrade_precheck.parse_version('v1.29.4+k3s1'), (1, 29, 4))

    def test_garbage_returns_none(self):
        """无法解析时返回 None，由调用方决定是警告还是失败。"""
        for value in ('', None, 'latest', 'v'):
            self.assertIsNone(upgrade_precheck.parse_version(value))


class MinorGapTest(TestCase):
    """次版本跨度的计算测试。"""

    def test_same_minor(self):
        """同一次版本内相差 0。"""
        self.assertEqual(upgrade_precheck._minor_gap('1.29.4', '1.29.9'), 0)

    def test_across_minors(self):
        """跨次版本按差值计算。"""
        self.assertEqual(upgrade_precheck._minor_gap('1.27.1', '1.29.4'), 2)

    def test_unparsable_returns_none(self):
        """任一侧解析不了就返回 None，不做猜测。"""
        self.assertIsNone(upgrade_precheck._minor_gap('', '1.29.4'))


class VersionSkewTest(TestCase):
    """check_version_skew 的判定测试。"""

    def test_consistent_nodes_pass(self):
        """所有节点 kubelet 版本一致且与 API Server 同次版本时通过。"""
        state = cluster_state([node_state('n1', 'v1.29.4'), node_state('n2', 'v1.29.4')])
        ok, message, detail = upgrade_precheck.check_version_skew(state, '1.29.6')
        self.assertTrue(ok, message)
        self.assertEqual(detail['kubelet_versions'], ['v1.29.4'])

    def test_mixed_kubelet_versions_failed(self):
        """节点版本不一致时判失败：升级会在版本混杂的集群上滚动。"""
        state = cluster_state([node_state('n1', 'v1.29.4'), node_state('n2', 'v1.29.3')])
        ok, message, _detail = upgrade_precheck.check_version_skew(state, '1.29.6')
        self.assertFalse(ok)
        self.assertIn('不一致', message)
        self.assertIn('v1.29.3', message)

    def test_not_ready_node_failed(self):
        """有节点没 Ready 时判失败：滚不动的节点必须先处理。"""
        state = cluster_state([node_state('n1', 'v1.29.4'), node_state('n2', 'v1.29.4', ready=False)])
        ok, message, _detail = upgrade_precheck.check_version_skew(state, '1.29.6')
        self.assertFalse(ok)
        self.assertIn('n2', message)
        self.assertIn('Ready', message)

    def test_span_over_two_minors_failed(self):
        """与目标相隔超过两个次版本时判失败。"""
        state = cluster_state([node_state('n1', 'v1.24.4')])
        ok, message, _detail = upgrade_precheck.check_version_skew(state, '1.29.4')
        self.assertFalse(ok)
        self.assertIn('次版本', message)

    def test_kubelet_behind_api_server_failed(self):
        """kubelet 与 API Server 不同次版本说明集群处在中间态，也要拦。"""
        state = cluster_state([node_state('n1', 'v1.28.4')], api_version='v1.29.4')
        ok, message, _detail = upgrade_precheck.check_version_skew(state, '1.29.6')
        self.assertFalse(ok)
        self.assertIn('API Server', message)

    def test_one_minor_gap_to_the_target_passes(self):
        """与目标相隔一个次版本是允许的：1.35 的集群升到 1.36。"""
        state = cluster_state([node_state('n1', 'v1.35.2')], api_version='v1.35.2')
        ok, message, _detail = upgrade_precheck.check_version_skew(state, '1.36')
        self.assertTrue(ok, message)
        self.assertIn('1.36', message)

    def test_two_minor_gap_to_the_target_fails(self):
        """与目标相隔两个次版本判失败：平台一次只允许升一个次版本。

        这道闸门在执行期再钉一遍是必要的 —— 提交期的校验挡不住「任务排队期间目标版本被改」
        这类输入，而节点一次滚过两个次版本是修不回来的。
        """
        state = cluster_state([node_state('n1', 'v1.34.2')], api_version='v1.34.2')
        ok, message, detail = upgrade_precheck.check_version_skew(state, '1.36')
        self.assertFalse(ok)
        self.assertIn('相隔 2 个次版本', message)
        self.assertEqual(detail['target_version'], '1.36')

    def test_unknown_node_state_is_not_a_failure(self):
        """取不到节点列表时给通过并说明跳过，不能把「查不到」当成「有问题」。"""
        ok, message, _detail = upgrade_precheck.check_version_skew({'node_summary': {}}, '1.29.6')
        self.assertTrue(ok)
        self.assertIn('跳过', message)

    def test_unparsable_target_failed(self):
        """目标版本解析不了时判失败：后面无法判断该滚到哪个版本。"""
        state = cluster_state([node_state('n1', 'v1.29.4')])
        ok, _message, _detail = upgrade_precheck.check_version_skew(state, 'latest')
        self.assertFalse(ok)


class DrainConditionTest(TestCase):
    """check_drain_conditions 的取数测试。"""

    def test_blockers_reported_as_risk(self):
        """disruptionsAllowed=0 的 PDB 要带名字报出来。"""
        with mock.patch(
            'k8s_resource.services.resource_actions.drain_pdb_warnings',
            return_value=[{'namespace': 'web', 'name': 'web-pdb', 'summary': ''}],
        ):
            risky, message, detail = upgrade_precheck.check_drain_conditions(object())
        self.assertTrue(risky)
        self.assertIn('web/web-pdb', message)
        self.assertEqual(len(detail['blockers']), 1)

    def test_no_blockers_is_clean(self):
        """没有阻塞 PDB 时不算风险。"""
        with mock.patch(
            'k8s_resource.services.resource_actions.drain_pdb_warnings', return_value=[],
        ):
            risky, message, _detail = upgrade_precheck.check_drain_conditions(object())
        self.assertFalse(risky)
        self.assertIn('没有阻塞', message)

    def test_api_error_is_not_a_failure(self):
        """读不到工作负载列表时只给警告，不能因此挡住升级。"""
        with mock.patch(
            'k8s_resource.services.resource_actions.drain_pdb_warnings',
            side_effect=RuntimeError('connection refused'),
        ):
            risky, message, _detail = upgrade_precheck.check_drain_conditions(object())
        self.assertFalse(risky)
        self.assertIn('connection refused', message)


class TargetArtifactTest(TestCase):
    """check_target_version_artifact 的版本比对测试。"""

    def setUp(self):
        """写出一棵合成 kubeasz 树，其中的 ezdown 声明 K8S_BIN_VER=v1.29.4。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里创建文件（用例结束后删除）。
        """
        self.root = tempfile.mkdtemp(prefix='k8s-upgrade-')
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.deployment = K8sDeployment(display_name='升级集群', slug='up', kubeasz_version='3.6.7')

    def test_matching_target_passes(self):
        """目标版本与制品声明的二进制版本一致时通过。"""
        ok, message, detail = upgrade_precheck.check_target_version_artifact(
            self.root, self.deployment, '1.29.4',
        )
        self.assertTrue(ok, message)
        self.assertEqual(detail['kubeasz_binary_version'], '1.29.4')

    def test_other_patch_version_failed(self):
        """制品只提供 1.29.4 时不允许升级到 1.29.9，并要说清制品里有哪个版本。"""
        ok, message, _detail = upgrade_precheck.check_target_version_artifact(
            self.root, self.deployment, '1.29.9',
        )
        self.assertFalse(ok)
        self.assertIn('1.29.4', message)
        self.assertIn('1.29.9', message)

    def test_unreadable_artifact_is_not_a_failure(self):
        """读不到 ezdown 时给通过并说明跳过，交给预检的制品能力检查去报错。"""
        empty = tempfile.mkdtemp(prefix='k8s-empty-')
        self.addCleanup(shutil.rmtree, empty, True)
        ok, message, _detail = upgrade_precheck.check_target_version_artifact(
            empty, self.deployment, '1.29.9',
        )
        self.assertTrue(ok)
        self.assertIn('跳过', message)


class ConfigCompatibilityTest(TestCase):
    """check_config_compatibility 的变量差异比对测试。

    跨次版本升级用的是目标版本的 playbook 去跑旧版本渲染出来的 config.yml，所以「新模板
    里有、集群配置里没有」的变量只在这时看得见。这里守护的是「有没有列出来」以及
    「列出来之后是不是只警告」——判死会挡住一次本来能成的升级。
    """

    def setUp(self):
        """准备一棵合成制品树、一个集群与一条升级任务。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录并写入数据库；用例结束时删除目录。
        """
        self.root = tempfile.mkdtemp(prefix='k8s-config-gap-')
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.template = (Path(self.root) / 'example' / 'config.yml').read_text(encoding='utf-8')

        self.org = Organization.objects.create(name='配置差异组织', slug='config-gap')
        self.cluster = K8sCluster.objects.create(
            organization=self.org, name='配置差异集群', slug='config-gap-cluster',
            origin='deployed', kube_version='1.35', status='configured',
        )
        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            cluster=self.cluster,
            task_type='upgrade',
            status='running',
            display_name='配置差异集群',
            slug='config-gap-task',
            kubernetes_version='1.36',
            kubeasz_version='3.7.0',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='8',
            topology_mode='ha',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='simulated',
        )

    def set_cluster_config(self, text):
        """把集群快照里的 config.yml 文本换成给定内容。

        参数：`text` 表示新的配置文件文本。
        返回：无显式返回值。
        副作用：写入配置版本表。
        """
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=1,
            config_hash='f' * 64,
            snapshot={'config': text},
        )

    def test_new_template_variables_are_listed(self):
        """新模板里有、集群配置里没有的变量要列出来，并给出可对照的说明。"""
        self.set_cluster_config('K8S_VER: "1.35.1"\nCLUSTER_NETWORK: "calico"\n')

        gap, message, detail = upgrade_precheck.check_config_compatibility(self.root, self.deployment)
        self.assertTrue(gap)
        self.assertEqual(detail['kubeasz_version'], '3.7.0')
        self.assertIn('CA_EXPIRY', detail['template_extra'])
        # 集群配置里已有的键不算差异，否则清单会淹没在「全都缺」的噪声里。
        self.assertNotIn('K8S_VER', detail['template_extra'])
        self.assertNotIn('CLUSTER_NETWORK', detail['template_extra'])
        self.assertIn(str(len(detail['template_extra'])), message)

    def test_identical_config_reports_no_gap(self):
        """集群配置就是这份模板渲染出来的（同次版本升级）时没有差异。"""
        self.set_cluster_config(self.template)

        gap, message, detail = upgrade_precheck.check_config_compatibility(self.root, self.deployment)
        self.assertFalse(gap)
        self.assertEqual(detail['template_extra'], [])
        self.assertIn('没有变量差异', message)

    def test_missing_template_is_not_a_failure(self):
        """制品里没有模板时说明跳过，不把「查不出来」当成有问题。"""
        empty = tempfile.mkdtemp(prefix='k8s-config-empty-')
        self.addCleanup(shutil.rmtree, empty, True)
        self.set_cluster_config('K8S_VER: "1.35.1"\n')

        gap, message, _detail = upgrade_precheck.check_config_compatibility(empty, self.deployment)
        self.assertFalse(gap)
        self.assertIn('跳过', message)

    def test_cluster_without_a_config_revision_is_not_a_failure(self):
        """集群没有配置版本记录时说明跳过：本轮之前部署的集群可能没有快照。"""
        gap, message, _detail = upgrade_precheck.check_config_compatibility(self.root, self.deployment)
        self.assertFalse(gap)
        self.assertIn('跳过', message)

    def test_task_without_a_cluster_is_not_a_failure(self):
        """任务没有关联集群时说明跳过（新建集群的任务就是这种形状）。"""
        self.deployment.cluster = None
        gap, message, _detail = upgrade_precheck.check_config_compatibility(self.root, self.deployment)
        self.assertFalse(gap)
        self.assertIn('跳过', message)


class RunUpgradePrechecksTest(TestCase):
    """run_upgrade_prechecks 的流程与中止测试（使用真实的任务上下文）。"""

    def setUp(self):
        """准备组织、任务与一棵合成制品树，并打桩制品下载之外的依赖。

        参数：无。
        返回：无显式返回值。
        副作用：写入数据库并创建临时目录；用例结束时删除目录。
        """
        self.root = tempfile.mkdtemp(prefix='k8s-upgrade-flow-')
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.org = Organization.objects.create(name='升级检查组织', slug='upgrade-precheck')
        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            task_type='upgrade',
            status='running',
            display_name='升级检查集群',
            slug='upgrade-precheck-cluster',
            kubernetes_version='1.29.4',
            kubeasz_version='3.6.7',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='8',
            topology_mode='ha',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='simulated',
        )
        self.context = state_machine.TaskContext(self.deployment, self.root)

    def events(self, event_type):
        """取出指定类型的事件消息列表。

        参数：`event_type` 表示事件类型。
        返回：返回消息字符串列表。
        副作用：只读数据库。
        """
        return list(
            K8sDeploymentEvent.objects.filter(
                deployment=self.deployment, event_type=event_type,
            ).order_by('id').values_list('message', flat=True)
        )

    def test_all_checks_pass(self):
        """三项检查都通过时返回结构化结论并完成阶段。"""
        state = cluster_state([node_state('n1', 'v1.29.4')])
        with mock.patch('k8s_cluster.services.probe_cluster', return_value=state), mock.patch(
            'k8s_resource.services.resource_actions.drain_pdb_warnings', return_value=[],
        ):
            summary = upgrade_precheck.run_upgrade_prechecks(
                self.context, self.deployment, '1.29.4', self.root,
            )
        self.assertIn('upgrade-precheck', self.deployment.completed_stages)
        self.assertEqual(summary['skew']['kubelet_versions'], ['v1.29.4'])
        self.assertTrue(self.events('upgrade_precheck_skew'))
        # 配置模板比对是第四项检查：这条任务没有关联集群，它说明跳过而不是判失败。
        self.assertTrue(self.events('upgrade_precheck_config'))
        self.assertIn('跳过', self.events('upgrade_precheck_config')[0])

    def test_artifact_mismatch_aborts_before_touching_cluster(self):
        """目标制品不匹配时直接中止，且不去连集群 API。"""
        with mock.patch('k8s_cluster.services.probe_cluster') as probe:
            with self.assertRaises(state_machine.TaskAborted) as caught:
                upgrade_precheck.run_upgrade_prechecks(
                    self.context, self.deployment, '1.29.9', self.root,
                )
        self.assertEqual(caught.exception.error_code, 'upgrade_target_artifact_missing')
        probe.assert_not_called()

    def test_version_skew_aborts(self):
        """节点版本不一致时以 upgrade_version_skew 中止升级。"""
        state = cluster_state([node_state('n1', 'v1.29.4'), node_state('n2', 'v1.29.3')])
        with mock.patch('k8s_cluster.services.probe_cluster', return_value=state):
            with self.assertRaises(state_machine.TaskAborted) as caught:
                upgrade_precheck.run_upgrade_prechecks(
                    self.context, self.deployment, '1.29.4', self.root,
                )
        self.assertEqual(caught.exception.error_code, 'upgrade_version_skew')

    def test_unreachable_cluster_aborts(self):
        """读不到集群状态时中止：升级不能在一个说不清状态的集群上开始。"""
        with mock.patch(
            'k8s_cluster.services.probe_cluster', side_effect=RuntimeError('connection refused'),
        ):
            with self.assertRaises(state_machine.TaskAborted) as caught:
                upgrade_precheck.run_upgrade_prechecks(
                    self.context, self.deployment, '1.29.4', self.root,
                )
        self.assertEqual(caught.exception.error_code, 'cluster_unreachable')

    def test_drain_blockers_only_warn(self):
        """PDB 阻塞驱逐只写警告，不中止升级（规划 10.4 的取舍）。"""
        state = cluster_state([node_state('n1', 'v1.29.4')])
        with mock.patch('k8s_cluster.services.probe_cluster', return_value=state), mock.patch(
            'k8s_resource.services.resource_actions.drain_pdb_warnings',
            return_value=[{'namespace': 'web', 'name': 'web-pdb', 'summary': ''}],
        ):
            summary = upgrade_precheck.run_upgrade_prechecks(
                self.context, self.deployment, '1.29.4', self.root,
            )
        self.assertEqual(len(summary['drain']['blockers']), 1)
        events = K8sDeploymentEvent.objects.filter(
            deployment=self.deployment, event_type='upgrade_precheck_drain',
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().level, 'warn')
        self.assertIn('web-pdb', events.first().message)
