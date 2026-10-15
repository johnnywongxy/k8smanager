"""部署任务接口对非管理员用户的权限语义。

规划 7.3 要求 ``/k8s/deployments`` 使用 ``page.k8s.deploy.view/create/retry/cancel``，
规划 12 要求进度页每 1~2 秒轮询任务详情、按游标拉事件与日志。这两条放一起意味着：
**只读的增量接口必须由 ``view`` 放行**，否则被授权「查看部署进度」的用户打开进度页
后，轮询全部 403，页面停在加载中。

权限层把 DRF 动作名直接当作操作码用（``_action_name``）。``events``/``logs``/
``environment`` 从来没有对应的操作码——它们只是挂在 ``/k8s/deployments`` 前缀下的只读
接口，页面码已经由别名解析成 ``page.k8s.deploy.view``。因此这里逐个钉住：
只看不改的接口不需要额外授权，而真正会改状态的动作（``dry-run``/``cancel``/``retry``）
仍然必须显式授权，不能被顺手放宽。
"""

import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from k8s_cluster.models import K8sCluster
from k8s_deployment.models import K8sDeployment
from ops.models import (
    Organization,
    PermissionPolicy,
    PermissionRule,
    UserProfile,
)
from ops.permission_service import clear_permission_cache
from rest_framework.test import APIClient

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'


class DeployReadOnlyEndpointPermissionTests(TestCase):
    """只读的部署接口只需要「查看部署」权限。"""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='部署权限测试组织', slug='deploy-perm')
        cls.deployment = K8sDeployment.objects.create(
            organization=cls.org,
            task_type='deploy_ha',
            status='running',
            display_name='权限测试集群',
            slug='perm-cluster',
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

    def setUp(self):
        """建一个只有「查看部署」权限的普通用户。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时运行目录与数据库记录；用例结束时清理。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-api-perm-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
            K8S_DEPLOY_AUTOSTART_WORKER=False,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.user = User.objects.create_user(username='deploy-viewer', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='部署查看权限', subject_type='user', user=self.user, status='available',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.grant_view()

    def grant(self, code, effect='allow'):
        """写入一条权限规则并清缓存。

        参数：`code` 表示权限码；`effect` 表示 allow 或 deny。
        返回：无显式返回值。
        副作用：写入权限规则并清理权限缓存。
        """
        PermissionRule.objects.update_or_create(
            policy=self.policy, permission_code=code, defaults={'effect': effect},
        )
        clear_permission_cache()

    def grant_view(self):
        """只授予菜单和页面的查看权限，不授予任何操作权限。"""
        self.grant('menu.k8s')
        self.grant('page.k8s.deploy.view')

    def url(self, suffix):
        """拼出部署接口地址。

        参数：`suffix` 表示动作路径后缀。
        返回：返回完整 API 路径，末尾带斜杠（DRF 路由默认追加，缺了会先 301 再丢方法）。
        """
        return f'/api/v1/k8s/deployments/{self.deployment.pk}/{suffix}/'

    def test_events_endpoint_needs_only_view_permission(self):
        """进度页按主键增量拉事件时，只该校验「查看」。"""
        response = self.client.get(self.url('events'), {'since': 0})
        self.assertEqual(response.status_code, 200, response.json())

    def test_logs_endpoint_needs_only_view_permission(self):
        """进度页按 seq 游标拉日志时，只该校验「查看」。"""
        response = self.client.get(self.url('logs'), {'after': 0})
        self.assertEqual(response.status_code, 200, response.json())

    def test_environment_endpoint_needs_only_view_permission(self):
        """向导提交前的平台环境探测是只读的，只该校验「查看」。"""
        response = self.client.get('/api/v1/k8s/deployments/environment/')
        self.assertEqual(response.status_code, 200, response.json())

    def test_detail_endpoint_needs_only_view_permission(self):
        """任务详情（进度页轮询的主接口）只该校验「查看」。"""
        response = self.client.get(f'/api/v1/k8s/deployments/{self.deployment.pk}/')
        self.assertEqual(response.status_code, 200, response.json())

    def test_cancel_still_requires_its_own_permission(self):
        """取消会改状态，只给查看权限时必须仍然被拒。"""
        response = self.client.post(self.url('cancel'), {}, format='json')
        self.assertEqual(response.status_code, 403, response.json())

    def test_retry_still_requires_its_own_permission(self):
        """重试会改状态，只给查看权限时必须仍然被拒。"""
        response = self.client.post(self.url('retry'), {}, format='json')
        self.assertEqual(response.status_code, 403, response.json())

    def test_dry_run_still_requires_its_own_permission(self):
        """dry-run 要跑完整预检，只给查看权限时必须仍然被拒。"""
        response = self.client.post('/api/v1/k8s/deployments/dry-run/', {}, format='json')
        self.assertEqual(response.status_code, 403, response.json())

    def test_granting_cancel_permission_opens_cancel(self):
        """补上取消权限后，取消接口就该放行——钉住的是映射，不是一律拒绝。"""
        self.grant('page.k8s.deploy.cancel')
        response = self.client.post(self.url('cancel'), {}, format='json')
        self.assertEqual(response.status_code, 200, response.json())


class ClusterSummaryPermissionTests(TestCase):
    """集群列表的汇总接口只读，只需要「查看集群」权限。"""

    def setUp(self):
        """建一个只有「查看集群」权限的普通用户。

        参数：无。
        返回：无显式返回值。
        副作用：创建组织和用户记录。
        """
        self.org = Organization.objects.create(name='集群汇总权限组织', slug='cluster-summary')
        K8sCluster.objects.create(
            organization=self.org,
            name='汇总测试集群',
            slug='summary-cluster',
            origin='deployed',
            kube_version='1.29.4',
            status='configured',
        )
        self.user = User.objects.create_user(username='cluster-viewer', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        policy = PermissionPolicy.objects.create(
            name='集群查看权限', subject_type='user', user=self.user, status='available',
        )
        PermissionRule.objects.update_or_create(
            policy=policy, permission_code='menu.k8s', defaults={'effect': 'allow'},
        )
        PermissionRule.objects.update_or_create(
            policy=policy, permission_code='page.k8s.clusters.view', defaults={'effect': 'allow'},
        )
        clear_permission_cache()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_summary_needs_only_view_permission(self):
        """列表页顶部的汇总卡片属于「查看」，不该再要一个操作权限。"""
        response = self.client.get('/api/v1/k8s/clusters/summary/')
        self.assertEqual(response.status_code, 200, response.json())
