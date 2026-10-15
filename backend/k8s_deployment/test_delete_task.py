"""删除部署任务记录时的机器解绑行为。

规划 6.2 把机器表的「所属集群」定为**集群标识**，写入方就是部署任务的创建
（``K8sDeploymentCreateSerializer._key_machines_to_slug``）。既然归属是任务写进去的，
任务记录被删掉时就必须把归属收回去，否则机器列表里会长期挂着一个已经不存在的部署，
而且这些机器不会再出现在任何新部署的候选里——占用校验正是按 ``cluster_name`` 判断的。

但「收回」有边界：部署成功的任务会按同一个标识落下 ``K8sCluster``，那批机器是**活集群的
成员**，删任务记录不能连它们的归属一起清掉，否则集群还在、机器却显示「未分配」，
再次部署时平台会把同一台机器当成空闲而重复选中。
"""

from django.contrib.auth.models import User
from django.test import TestCase

from k8s_cluster.models import K8sCluster
from k8s_deployment.models import K8sDeployment, K8sDeploymentNode
from k8s_machine.models import Machine
from ops.models import Organization, PermissionPolicy, PermissionRule, UserProfile
from ops.permission_service import clear_permission_cache
from platform_logs.models import AuditLog
from rest_framework.test import APIClient


class DeleteDeploymentTaskTest(TestCase):
    """删除任务：终态才放行，且只在集群不存在时解绑机器。"""

    def setUp(self):
        """准备组织、有删除权限的用户、一台已绑定的机器与一条失败任务。

        参数：无。
        返回：无显式返回值。
        副作用：写入组织、用户、权限策略、机器、任务与节点快照记录。
        """
        self.org = Organization.objects.create(name='删除任务测试组织', slug='delete-task')
        self.user = User.objects.create_user(username='deploy-deleter', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='部署删除权限', subject_type='user', user=self.user, status='available',
        )
        self.grant('menu.k8s')
        self.grant('page.k8s.deploy.view')
        self.grant('page.k8s.deploy.delete')

        self.machine = Machine.objects.create(
            organization=self.org,
            name='node-01',
            ip_address='192.168.1.94',
            ssh_port=22,
            ssh_user='root',
            roles=['master', 'node', 'etcd'],
            cluster_name='k8s1',
        )
        self.deployment = self.create_deployment(slug='k8s1', status='failed')
        K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=self.deployment,
            machine=self.machine,
            name=self.machine.name,
            ip_address=str(self.machine.ip_address),
            ssh_port=self.machine.ssh_port,
            ssh_user=self.machine.ssh_user,
            roles=['master', 'node', 'etcd'],
        )

        self.client = APIClient()
        self.client.force_authenticate(self.user)

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

    def create_deployment(self, slug, status='failed'):
        """建一条部署任务记录。

        参数：`slug` 表示集群标识；`status` 表示任务状态。
        返回：返回新建的 K8sDeployment。
        副作用：向部署任务表写入一行。
        """
        return K8sDeployment.objects.create(
            organization=self.org,
            task_type='deploy_aio',
            status=status,
            display_name=slug,
            slug=slug,
            kubernetes_version='1.35',
            kubeasz_version='3.6.10',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='9',
            topology_mode='aio',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='simulated',
        )

    def url(self, deployment):
        """拼出任务详情地址。

        参数：`deployment` 表示目标任务。
        返回：返回带末尾斜杠的 DELETE 地址。
        副作用：不修改任何状态。
        """
        return f'/api/v1/k8s/deployments/{deployment.pk}/'

    def test_failed_task_delete_releases_the_machines(self):
        """集群不存在（任务中途失败）时，删除记录要清空机器的角色与所属集群。"""
        response = self.client.delete(self.url(self.deployment))

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(K8sDeployment.objects.filter(pk=self.deployment.pk).exists())
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.cluster_name, '')
        self.assertEqual(self.machine.normalized_roles(), [])

    def test_delete_keeps_machines_of_a_live_cluster(self):
        """集群还在时，删任务记录不能把机器从活集群里摘出来。"""
        K8sCluster.objects.create(
            organization=self.org, name='k8s1', slug='k8s1', origin='deployed', status='configured',
        )

        response = self.client.delete(self.url(self.deployment))

        self.assertEqual(response.status_code, 204, response.data)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.cluster_name, 'k8s1')
        self.assertEqual(sorted(self.machine.normalized_roles()), ['etcd', 'master', 'node'])

    def test_other_cluster_machines_are_untouched(self):
        """只按本任务的标识解绑，别的集群名下的机器不受影响。"""
        other = Machine.objects.create(
            organization=self.org, name='node-02', ip_address='192.168.1.95',
            ssh_port=22, ssh_user='root', roles=['node'], cluster_name='other-cluster',
        )

        response = self.client.delete(self.url(self.deployment))

        self.assertEqual(response.status_code, 204, response.data)
        other.refresh_from_db()
        self.assertEqual(other.cluster_name, 'other-cluster')
        self.assertEqual(other.normalized_roles(), ['node'])

    def test_running_task_is_refused(self):
        """非终态任务可能正被 Worker 执行，删记录会让跑着的 ansible 失去归属。"""
        running = self.create_deployment(slug='running-cluster', status='running')

        response = self.client.delete(self.url(running))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('取消任务', str(response.data))
        self.assertTrue(K8sDeployment.objects.filter(pk=running.pk).exists())

    def test_delete_requires_its_own_permission(self):
        """删除是写操作，只给查看权限时必须被拒。"""
        self.grant('page.k8s.deploy.delete', effect='deny')

        response = self.client.delete(self.url(self.deployment))

        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(K8sDeployment.objects.filter(pk=self.deployment.pk).exists())
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.cluster_name, 'k8s1')

    def test_released_machines_are_audited(self):
        """改写机器归属属于越权动作，必须在删除审计里留下被解绑的机器。"""
        self.client.delete(self.url(self.deployment))

        log = AuditLog.objects.filter(action='k8s.delete').latest('created_at')
        self.assertEqual(log.detail['released_machines'], [self.machine.pk])


class BulkDeleteDeploymentTaskTest(TestCase):
    """任务中心的批量删除：要么整批成功，要么一条都不删。

    逐条 try/except 在这里是最糟的做法 —— 被拒绝的那几条还留在列表里、已删的那几条
    连机器归属都清掉了，用户从界面上看不出应该重试哪一批，也无法判断机器是否已经释放。
    所以任何一条不合规就整体拒绝，并在提示里点名是哪条、当前什么状态。
    """

    def setUp(self):
        """准备组织、有删除权限的用户，以及两条终态任务各自的机器。

        参数：无。
        返回：无显式返回值。
        副作用：写入组织、用户、权限策略、机器、任务与节点快照记录。
        """
        self.org = Organization.objects.create(name='批量删除测试组织', slug='bulk-delete')
        self.user = User.objects.create_user(username='bulk-deleter', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.org, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='批量删除权限', subject_type='user', user=self.user, status='available',
        )
        self.grant('menu.k8s')
        self.grant('page.k8s.deploy.view')
        self.grant('page.k8s.deploy.delete')

        self.first = self.build_task('bulk-a', 'failed')
        self.second = self.build_task('bulk-b', 'cancelled')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

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

    def build_task(self, slug, status):
        """建一条任务记录、一台机器和它的节点快照。

        参数：`slug` 表示集群标识；`status` 表示任务状态。
        返回：返回新建的 K8sDeployment。
        副作用：向机器、任务与节点快照表写入记录。
        """
        machine = Machine.objects.create(
            organization=self.org,
            name=f'{slug}-node',
            ip_address=f'10.0.0.{Machine.objects.count() + 10}',
            ssh_port=22,
            ssh_user='root',
            roles=['node'],
            cluster_name=slug,
        )
        deployment = K8sDeployment.objects.create(
            organization=self.org,
            task_type='deploy_aio',
            status=status,
            display_name=slug,
            slug=slug,
            kubernetes_version='1.35',
            kubeasz_version='3.6.10',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='9',
            topology_mode='aio',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='simulated',
        )
        K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=deployment,
            machine=machine,
            name=machine.name,
            ip_address=str(machine.ip_address),
            ssh_port=machine.ssh_port,
            ssh_user=machine.ssh_user,
            roles=['node'],
        )
        machine.refresh_from_db()
        return deployment

    def bulk_delete(self, ids):
        """提交一次批量删除。

        参数：`ids` 表示任务主键列表。
        返回：返回接口响应。
        副作用：可能删除任务记录、清空机器归属并写入审计。
        """
        return self.client.post(
            '/api/v1/k8s/deployments/bulk-delete/', {'ids': ids}, format='json',
        )

    def test_batch_delete_removes_every_task_and_releases_machines(self):
        """整批都是终态时全部删除，并逐条回报解绑了哪些机器。"""
        machine_ids = [item.machine_id for item in K8sDeploymentNode.objects.filter(
            deployment__in=[self.first, self.second],
        )]

        response = self.bulk_delete([self.first.pk, self.second.pk])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(sorted(response.data['deleted']), sorted([self.first.pk, self.second.pk]))
        self.assertFalse(K8sDeployment.objects.filter(pk__in=[self.first.pk, self.second.pk]).exists())
        released = {pk for ids in response.data['released_machines'].values() for pk in ids}
        self.assertEqual(released, set(machine_ids))
        self.assertEqual(Machine.objects.filter(pk__in=machine_ids, cluster_name='').count(), len(machine_ids))

    def test_one_running_task_refuses_the_whole_batch(self):
        """批里混着一条非终态任务时整批拒绝，已经合规的那条也不能被删。"""
        running = self.build_task('bulk-running', 'running')

        response = self.bulk_delete([self.first.pk, running.pk])

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('bulk-running', str(response.data))
        # 关键断言：不能出现「删了一半」。两条都必须在。
        self.assertTrue(K8sDeployment.objects.filter(pk=self.first.pk).exists())
        self.assertTrue(K8sDeployment.objects.filter(pk=running.pk).exists())

    def test_duplicate_ids_are_deduplicated(self):
        """同一主键传两次只删一次，不该被当成「有任务找不到」。"""
        response = self.bulk_delete([self.first.pk, self.first.pk])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['deleted'], [self.first.pk])

    def test_ids_of_another_organization_are_refused(self):
        """别的组织的任务不可见也不可删，且整批一起拒绝。"""
        other_org = Organization.objects.create(name='别的组织', slug='other-org')
        stranger = K8sDeployment.objects.create(
            organization=other_org,
            task_type='deploy_aio',
            status='failed',
            display_name='stranger',
            slug='stranger',
            kubernetes_version='1.35',
            kubeasz_version='3.6.10',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='9',
            topology_mode='aio',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='simulated',
        )

        response = self.bulk_delete([self.first.pk, stranger.pk])

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(K8sDeployment.objects.filter(pk=stranger.pk).exists())
        self.assertTrue(K8sDeployment.objects.filter(pk=self.first.pk).exists())

    def test_oversized_batch_is_refused(self):
        """一次提交超过上限直接拒绝：每条都要解绑机器并逐台写审计，不设限会锁住库。"""
        response = self.bulk_delete(list(range(1, 102)))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('最多删除', str(response.data))

    def test_empty_selection_is_refused(self):
        """没选任何任务时直接拒绝，不要静默返回「删了 0 条」。"""
        response = self.bulk_delete([])

        self.assertEqual(response.status_code, 400, response.data)

    def test_bulk_delete_needs_the_delete_permission(self):
        """批量删除复用单条删除的权限码，只有查看权限时必须被拒。"""
        self.grant('page.k8s.deploy.delete', effect='deny')

        response = self.bulk_delete([self.first.pk])

        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(K8sDeployment.objects.filter(pk=self.first.pk).exists())

    def test_each_task_is_audited(self):
        """批量的每一条都要留下自己的删除审计，事后能一条条追溯。"""
        self.bulk_delete([self.first.pk, self.second.pk])

        logs = AuditLog.objects.filter(action='k8s.delete').order_by('created_at')
        audited = {log.detail.get('slug') for log in logs}
        self.assertEqual(audited, {'bulk-a', 'bulk-b'})
