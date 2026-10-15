"""向导「完整预检」：生成配置、跑完预检，但绝不留下正式任务。

这一层要守住的是规划 9 那条边界（``k8s.txt`` 第 199 行）：dry-run 可以生成配置、可以连目标
节点做预检，但**不能**执行 Ansible，也**不能**在任务表里留下一条记录。实现上为此临时写了一条
任务记录（工作区与编译器都按任务主键组织），因此「跑完之后什么都不剩」这件事必须有用例钉住。

平台之外的依赖（OSS 制品、目标机 SSH）在这里替换掉：制品换成合成 kubeasz 树，预检换成固定结论。
配置编译是真的跑的——它正是这条链路上最值得验证的一环。
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from k8s_config.models import K8sVersionMapping, SupportedOperatingSystem
from k8s_deployment.models import K8sDeployment, K8sDeploymentNode
from k8s_deployment.serializers import K8sDeploymentCreateSerializer, derive_slug
from k8s_deployment.services import dry_run, paths, preflight_token
from k8s_machine.models import Machine
from ops.models import Organization, PermissionPolicy, PermissionRule, UserProfile
from ops.permission_service import clear_permission_cache
from rest_framework.test import APIClient

from . import kubeasz_fixtures

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'

# 合成制品里声明的二进制版本，用它当目标版本可以避免编译告警，让断言只关注真正被测的东西。
K8S_VERSION = '1.29.4'

PREFLIGHT_OK = {
    'ok': True,
    'checks': [
        {'name': 'ssh', 'title': 'SSH 连通性', 'status': 'passed', 'message': '全部节点可登录',
         'detail': {}, 'nodes': []},
    ],
    'hard_failures': [],
    'summary': '通过 1 项，失败 0 项',
}
PREFLIGHT_FAILED = {
    'ok': False,
    'checks': [
        {'name': 'ports', 'title': '端口占用', 'status': 'failed', 'message': '6443 已被占用',
         'detail': {}, 'nodes': ['10.0.9.1']},
    ],
    'hard_failures': ['端口占用：6443 已被占用'],
    'summary': '通过 0 项，失败 1 项',
}


class DryRunTestBase(TestCase):
    """预检用例的公共夹具：临时状态目录、版本映射、受支持系统与两台机器。"""

    def setUp(self):
        """准备状态目录与向导提交所需的配置数据。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录与数据库记录。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-dryrun-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
            K8S_DEPLOY_AUTOSTART_WORKER=False,
            # 生产上 kubeasz 安装在 /etc/kubeasz，测试一律落在临时目录里。
            K8S_DEPLOY_KUBEASZ_ROOT=str(self.state_root / 'kubeasz'),
            K8S_DEPLOY_CLUSTERS_ROOT=str(self.state_root / 'clusters'),
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.org = Organization.objects.create(name='预检测试组织', slug='dryrun-test')
        self.mapping = K8sVersionMapping.objects.create(
            kubernetes_version=K8S_VERSION, kubeasz_version='3.6.7',
        )
        self.system = SupportedOperatingSystem.objects.create(os_type='DryRunOS', version='9')
        self.mapping.supported_systems.add(self.system)
        self.machine = Machine.objects.create(
            organization=self.org, name='dry-01', ip_address='10.0.9.1',
            ssh_port=22, ssh_user='root',
        )

    def payload(self, **overrides):
        """拼出一份合法的向导提交体。

        参数：`overrides` 表示要覆盖的字段。
        返回：返回请求体字典。
        副作用：无。
        """
        data = {
            'display_name': '预检演示集群',
            'kubernetes_version': K8S_VERSION,
            'os_type': 'DryRunOS',
            'os_version': '9',
            'topology_mode': 'aio',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'nodes': [{'machine_id': self.machine.pk, 'roles': ['etcd', 'master', 'node']}],
        }
        data.update(overrides)
        return data

    def validated(self, **overrides):
        """跑一遍创建序列化器，拿到校验结果与节点结构。

        参数：`overrides` 表示要覆盖的字段。
        返回：返回 ``(values, node_payloads)``。
        副作用：只读数据库，不写入数据。
        """
        serializer = K8sDeploymentCreateSerializer(data=self.payload(**overrides))
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data, serializer._context_nodes

    def fixture_artifacts(self):
        """把制品准备换成「在工作区里写出一棵合成 kubeasz 树」。

        参数：无。
        返回：返回已启动的 mock.patch 对象。
        副作用：替换 ``artifacts.ensure_kubeasz_source``。

        mock 的 side_effect 与真实实现落在同一个位置：预检给的是它专用的临时目录
        （``<状态根>/dryrun/deployment-<id>``），源码树就写在那里，这样编译器的路径推导
        与预检收尾时的目录清理都是真的在跑。
        """
        def fake_ensure(release, deployment=None, bucket=None, root=None):
            target = Path(root) if root else paths.kubeasz_root()
            return kubeasz_fixtures.write_kubeasz_tree(target, version='3.6.7')

        patcher = mock.patch.object(
            dry_run.artifacts, 'ensure_kubeasz_source', side_effect=fake_ensure,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def stub_preflight(self, result=None):
        """把预检换成固定结论。

        参数：`result` 表示要返回的预检结果。
        返回：返回已启动的 mock.patch 对象。
        副作用：替换 ``preflight.run_preflight``。
        """
        patcher = mock.patch.object(
            dry_run.preflight, 'run_preflight', return_value=result or PREFLIGHT_OK,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher


class DeepPreflightServiceTest(DryRunTestBase):
    """``run_deep_preflight``：生成配置、跑预检，然后把临时痕迹全部收干净。"""

    def test_no_task_is_left_behind(self):
        """跑完之后任务表与节点快照表里不留任何记录——这是规划 9 的硬要求。"""
        self.fixture_artifacts()
        self.stub_preflight()
        values, nodes = self.validated()
        result = dry_run.run_deep_preflight(values, nodes)

        self.assertTrue(result['ok'])
        self.assertFalse(K8sDeployment.objects.exists())
        self.assertFalse(K8sDeploymentNode.objects.exists())

    def test_config_is_generated_with_the_users_slug(self):
        """用户在确认页填的标识会出现在生成的 config.yml 里，也就是 CLUSTER_NAME。"""
        self.fixture_artifacts()
        self.stub_preflight()
        values, nodes = self.validated(slug='prod-hz-01')
        result = dry_run.run_deep_preflight(values, nodes)

        self.assertIn('CLUSTER_NAME: "prod-hz-01"', result['config']['config_text'])
        self.assertIn('10.0.9.1', result['config']['hosts_text'])
        self.assertTrue(result['config']['config_hash'])

    def test_derived_slug_matches_what_creation_would_use(self):
        """留空时用的是与正式创建同一套派生规则，预览与真正部署的目录名一致。"""
        self.fixture_artifacts()
        self.stub_preflight()
        values, nodes = self.validated()
        result = dry_run.run_deep_preflight(values, nodes)

        self.assertIn(f'CLUSTER_NAME: "{derive_slug("预检演示集群")}"', result['config']['config_text'])

    def test_transient_record_never_occupies_the_real_slug(self):
        """临时记录用内部标识：预检期间提交正式任务不会因为「标识被占用」被挡住。"""
        self.fixture_artifacts()
        seen = {}

        def probe(deployment, nodes, credentials):
            seen['slug'] = deployment.slug
            seen['occupied'] = K8sDeployment.objects.filter(slug='prod-hz-01').exists()
            return PREFLIGHT_OK

        with mock.patch.object(dry_run.preflight, 'run_preflight', side_effect=probe):
            values, nodes = self.validated(slug='prod-hz-01')
            dry_run.run_deep_preflight(values, nodes)

        self.assertEqual(seen['slug'], 'prod-hz-01')
        self.assertFalse(seen['occupied'])

    def test_workspace_is_removed_afterwards(self):
        """工作区（含解压出来的 kubeasz 源码）必须删掉，否则每次预检都会漏几百 MB。"""
        self.fixture_artifacts()
        workspaces = []
        shared_root = paths.kubeasz_root()

        def probe(deployment, nodes, credentials):
            workspaces.append(deployment.pk)
            return PREFLIGHT_OK

        with mock.patch.object(dry_run.preflight, 'run_preflight', side_effect=probe):
            values, nodes = self.validated()
            dry_run.run_deep_preflight(values, nodes)

        self.assertTrue(workspaces)
        for pk in workspaces:
            self.assertFalse(paths.workspace_root(pk).exists())
            self.assertFalse(paths.dry_run_root(pk).exists())
        # 预检用的是它自己那棵树，共享的 kubeasz 目录连碰都不该碰。
        self.assertFalse(shared_root.exists())

    def test_workspace_is_removed_even_when_preflight_explodes(self):
        """预检自己抛异常时同样要收干净：临时目录不能因为一次失败就永久留在盘上。"""
        self.fixture_artifacts()
        workspaces = []

        def boom(deployment, nodes, credentials):
            workspaces.append(deployment.pk)
            raise RuntimeError('预检炸了')

        with mock.patch.object(dry_run.preflight, 'run_preflight', side_effect=boom):
            values, nodes = self.validated()
            with self.assertRaises(RuntimeError):
                dry_run.run_deep_preflight(values, nodes)

        for pk in workspaces:
            self.assertFalse(paths.workspace_root(pk).exists())
        self.assertFalse(K8sDeployment.objects.exists())

    def test_preflight_failures_are_returned_not_hidden(self):
        """预检失败照原样返回：用户要看到是哪一项、为什么失败。"""
        self.fixture_artifacts()
        self.stub_preflight(PREFLIGHT_FAILED)
        values, nodes = self.validated()
        result = dry_run.run_deep_preflight(values, nodes)

        self.assertFalse(result['ok'])
        self.assertEqual(result['hard_failures'], ['端口占用：6443 已被占用'])
        self.assertFalse(K8sDeployment.objects.exists())


class StaleCleanupFieldTest(DryRunTestBase):
    """``auto_clean_stale`` 在提交体里的默认值与回显。

    这一项决定预检会不会给出「将在部署前清理」的结论，进而决定部署流程跑不跑清理阶段。
    默认值必须在**请求没带**时就成立：向导勾选框默认勾上走的是前端，而 API 直连、
    历史任务重试走的是这一层。
    """

    def test_defaults_to_true_when_absent(self):
        """请求里没带这一项时按「勾选」处理 —— 与模型默认值、与向导的默认勾选一致。"""
        values, _nodes = self.validated()
        self.assertIs(values['auto_clean_stale'], True)

    def test_explicit_false_is_honoured(self):
        """显式关掉要照办：关掉意味着放弃自动清理，回到「首次硬拦」那条路。"""
        values, _nodes = self.validated(auto_clean_stale=False)
        self.assertIs(values['auto_clean_stale'], False)

    def test_default_reaches_the_saved_task(self):
        """落库后的任务上也是勾选状态：执行期与预检读的是库里这一份，不是提交体。"""
        self.fixture_artifacts()
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        serializer.is_valid(raise_exception=True)
        deployment = serializer.save()
        self.assertTrue(deployment.auto_clean_stale)
        # 请求快照里也是补全后的值：审计时不该看到一个缺失的字段。
        self.assertIs(deployment.request_snapshot['submitted']['auto_clean_stale'], True)


class DryRunEndpointTest(DryRunTestBase):
    """dry-run 接口：默认只做本地校验，``deep=true`` 才生成配置并连节点。"""

    def setUp(self):
        """准备一个拥有预检权限的普通用户。

        参数：无。
        返回：无显式返回值。
        副作用：创建用户、策略与权限规则。
        """
        super().setUp()
        self.user = User.objects.create_user(username='dry-run-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='预检权限', subject_type='user', user=self.user, status='available',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        for code in ('menu.k8s', 'page.k8s.deploy.view', 'page.k8s.deploy.dry_run'):
            PermissionRule.objects.update_or_create(
                policy=self.policy, permission_code=code, defaults={'effect': 'allow'},
            )
        clear_permission_cache()

    def post(self, deep=False, **overrides):
        """请求 dry-run 接口。

        参数：`deep` 表示是否要求完整预检；`overrides` 表示要覆盖的提交字段。
        返回：返回响应对象。
        副作用：可能触发制品准备与 SSH 连接（用例里都已替换）。
        """
        body = self.payload(**overrides)
        if deep:
            body['deep'] = True
        return self.client.post('/api/v1/k8s/deployments/dry-run/', body, format='json')

    def test_default_call_stays_local_only(self):
        """不带 deep 时不许连节点、不许下载制品：向导进确认页就会调它。"""
        with mock.patch.object(dry_run, 'run_deep_preflight') as deep_call:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.json())
        deep_call.assert_not_called()
        self.assertFalse(response.json()['deep'])
        self.assertNotIn('checks', response.json()['summary'])
        self.assertEqual(response.json()['summary']['candidate_slug'], derive_slug('预检演示集群'))

    def test_deep_call_returns_checks_and_config(self):
        """deep=true 时把逐项预检结论与生成的配置一并回给向导。"""
        self.fixture_artifacts()
        self.stub_preflight()
        response = self.post(deep=True, slug='prod-hz-01')
        payload = response.json()

        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['deep'])
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['summary']['candidate_slug'], 'prod-hz-01')
        self.assertEqual(len(payload['summary']['checks']), 1)
        self.assertIn('CLUSTER_NAME: "prod-hz-01"', payload['summary']['config']['config_text'])
        self.assertIn('已完成配置生成与全部预检', payload['summary']['notice'])
        self.assertFalse(K8sDeployment.objects.exists())

    def test_deep_call_reports_hard_failures(self):
        """预检有硬性失败时接口整体判为不通过，向导据此拦住提交。"""
        self.fixture_artifacts()
        self.stub_preflight(PREFLIGHT_FAILED)
        payload = self.post(deep=True).json()

        self.assertFalse(payload['ok'])
        self.assertEqual(payload['summary']['hard_failures'], ['端口占用：6443 已被占用'])
        self.assertIn('存在失败项', payload['summary']['notice'])

    def test_deep_call_issues_a_credential_when_it_passes(self):
        """预检通过时回一份凭据，提交部署任务必须把它带回来（见 preflight_token）。"""
        self.fixture_artifacts()
        self.stub_preflight()
        payload = self.post(deep=True).json()

        token = payload['preflight_token']
        self.assertTrue(token)
        values, nodes = self.validated()
        self.assertTrue(preflight_token.verify(token, values, nodes))

    def test_deep_call_issues_nothing_when_a_check_fails(self):
        """有硬失败项时不签凭据：凭空签一份等于把这道闸拆了。"""
        self.fixture_artifacts()
        self.stub_preflight(PREFLIGHT_FAILED)
        payload = self.post(deep=True).json()

        self.assertFalse(payload['ok'])
        self.assertEqual(payload['preflight_token'], '')

    def test_a_local_only_call_issues_nothing(self):
        """没跑完整预检时不签凭据。"""
        self.assertEqual(self.post().json()['preflight_token'], '')

    def test_deep_call_failure_is_a_readable_400(self):
        """预检自身起不来时给出中文原因，而不是 500。"""
        failure = dry_run.DryRunError('没有可用于预检的节点。')
        with mock.patch.object(dry_run, 'run_deep_preflight', side_effect=failure):
            response = self.post(deep=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn('没有可用于预检的节点', str(response.json()))

    def ha_payload(self, with_balancers=True, **overrides):
        """拼一份「三台 etcd+master（可选加两台负载均衡机器）」的提交体。

        参数：`with_balancers` 表示是否附带两台负载均衡机器；`overrides` 表示要覆盖的字段。
        返回：返回请求体字典。
        副作用：向数据库写入三台新机器，以及可选的负载均衡机器。

        负载均衡机器必须是可选的：云内网负载均衡模式不需要它们，附带就会被提交校验拒掉 ——
        那正是这个模式要守住的边界（选了机器却什么都不会被安装）。
        """
        masters = [
            Machine.objects.create(
                organization=self.org, name=f'ha-{index}', ip_address=f'10.0.9.{index}',
                ssh_port=22, ssh_user='root',
            )
            for index in range(2, 5)
        ]
        balancers = [
            Machine.objects.create(
                organization=self.org, name=f'lb-{index}', ip_address=f'10.0.9.{index}',
                ssh_port=22, ssh_user='root',
            )
            for index in range(6, 8)
        ]
        nodes = [{'machine_id': item.pk, 'roles': ['etcd', 'master']} for item in masters]
        if with_balancers:
            nodes += [{'machine_id': item.pk, 'roles': ['loadbalancer']} for item in balancers]
        return self.payload(topology_mode='ha', nodes=nodes, **overrides)

    def test_lb_summary_lists_the_vip_and_the_master_backup_roles(self):
        """确认页要单独展示 VIP 与每台负载均衡机器的主备角色，而不是让用户从 nodes 里筛。"""
        response = self.client.post(
            '/api/v1/k8s/deployments/dry-run/',
            self.ha_payload(lb_mode='self', lb_vip='10.0.9.250'),
            format='json',
        )
        summary = response.json()['summary']

        self.assertEqual(summary['lb']['mode'], 'self')
        self.assertTrue(summary['lb']['enabled'])
        self.assertEqual(summary['lb']['vip'], '10.0.9.250')
        self.assertEqual(summary['lb']['port'], 6443)
        self.assertEqual(
            summary['lb']['nodes'],
            [{'ip_address': '10.0.9.6', 'lb_role': 'master'},
             {'ip_address': '10.0.9.7', 'lb_role': 'backup'}],
        )
        # 节点数只数集群节点：确认页要写「3 个集群节点 + 2 台负载均衡机器」。
        self.assertEqual(summary['node_count'], 3)

    def test_cloud_lb_summary_has_an_address_and_no_machines(self):
        """云内网负载均衡：确认页显示地址与模式，但机器清单是空的 —— 平台不装东西。"""
        response = self.client.post(
            '/api/v1/k8s/deployments/dry-run/',
            self.ha_payload(with_balancers=False, lb_mode='cloud', lb_vip='10.0.9.250'),
            format='json',
        )
        summary = response.json()['summary']

        self.assertEqual(summary['lb']['mode'], 'cloud')
        self.assertTrue(summary['lb']['enabled'])
        self.assertEqual(summary['lb']['vip'], '10.0.9.250')
        self.assertEqual(summary['lb']['port'], 6443)
        self.assertEqual(summary['lb']['nodes'], [])
        # 云模式不需要负载均衡机器，那两台机器也不该混进集群节点数里。
        self.assertEqual(summary['node_count'], 3)

    def test_cloud_lb_plan_keeps_eight_entries(self):
        """云模式确认页仍然只有「镜像仓库 + 七步 ezctl」：平台不在集群里装代理。"""
        response = self.client.post(
            '/api/v1/k8s/deployments/dry-run/',
            self.ha_payload(with_balancers=False, lb_mode='cloud', lb_vip='10.0.9.250'),
            format='json',
        )
        stages = [item['stage'] for item in response.json()['summary']['plan']]

        self.assertEqual(len(stages), 8)
        self.assertNotIn('10.ex-lb', stages)

    def test_cloud_lb_on_a_single_node_cluster_is_refused_before_submitting(self):
        """单节点集群选云内网负载均衡：提交时就要被拒，不能「提交成功但注定失败」。

        预检的 ``external_lb`` 项对单控制面集群判 ``failed``，而预检失败是 ``TaskAborted``：
        放过去的话用户会拿到一条已入队、跑起来立刻终止的任务，机器上一行命令都不执行。
        """
        response = self.client.post(
            '/api/v1/k8s/deployments/dry-run/',
            self.payload(lb_mode='cloud', lb_vip='10.0.9.250'),
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('至少需要 3 个控制面节点', str(response.json()))

    def test_lb_summary_is_disabled_by_default(self):
        """没勾组件时这一块是关的，地址为空、机器为空。"""
        response = self.post()
        lb = response.json()['summary']['lb']
        self.assertEqual(lb['mode'], 'none')
        self.assertFalse(lb['enabled'])
        self.assertEqual(lb['vip'], '')
        self.assertEqual(lb['nodes'], [])

    def test_plan_has_eight_steps_when_lb_is_enabled(self):
        """确认页的「将要执行的阶段」也必须多出那一步，否则与实际执行不符。"""
        response = self.client.post(
            '/api/v1/k8s/deployments/dry-run/',
            self.ha_payload(lb_mode='self', lb_vip='10.0.9.250'),
            format='json',
        )
        plan = response.json()['summary']['plan']
        stages = [item['stage'] for item in plan]

        self.assertEqual(len(stages), 9)  # 镜像仓库 + 八步 ezctl
        self.assertEqual(stages[-1], '10.ex-lb')
        self.assertEqual(plan[-1]['command'], f'ezctl setup {derive_slug("预检演示集群")} 10')

    def test_plan_has_seven_steps_when_lb_is_disabled(self):
        """未启用时确认页不多画一步。"""
        response = self.client.post(
            '/api/v1/k8s/deployments/dry-run/', self.ha_payload(), format='json',
        )
        stages = [item['stage'] for item in response.json()['summary']['plan']]
        self.assertEqual(len(stages), 8)
        self.assertNotIn('10.ex-lb', stages)
