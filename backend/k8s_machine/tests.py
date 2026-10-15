import asyncio
import json
import time
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from backend.asgi import application
from k8s_cluster.models import K8sCluster
from k8s_deployment.services import credentials, machine_ssh, node_registry
from ops.models import Organization, PermissionPolicy, PermissionRule, UserProfile
from ops.permission_service import clear_permission_cache
from ops.services.tokens import issue_token
from platform_logs.models import AuditLog

from . import metrics, ssh_session
from .models import Machine, MachineMetric, MachineSshSession
from .views import release_cluster_machines


class MachinePermissionTests(TestCase):
    """验证机器管理接口按数据库权限策略校验菜单、页面和操作权限。"""

    def setUp(self):
        """创建普通用户及默认拒绝的权限策略，并准备一台机器。"""
        organization = Organization.objects.first()
        self.user = User.objects.create_user(username='machine-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=organization, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='机器管理权限',
            subject_type='user',
            user=self.user,
            status='available',
        )
        self.machine = Machine.objects.create(
            organization=organization,
            name='node-01',
            ip_address='10.0.0.11',
            ssh_port=22,
            ssh_user='root',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def grant(self, code):
        """向当前测试用户策略写入单项允许规则。"""
        PermissionRule.objects.update_or_create(
            policy=self.policy,
            permission_code=code,
            defaults={'effect': 'allow'},
        )
        clear_permission_cache()

    def revoke_all(self):
        """清空策略规则并重建缓存，恢复默认拒绝状态。"""
        PermissionRule.objects.filter(policy=self.policy).delete()
        clear_permission_cache()

    def test_machine_list_requires_group_and_page_permissions(self):
        """普通用户必须同时具备机器管理菜单和机器列表页面权限。"""
        self.revoke_all()
        denied = self.client.get('/api/v1/machines/servers/')
        self.grant('menu.machine')
        still_denied = self.client.get('/api/v1/machines/servers/')
        self.grant('page.machine.servers.view')
        allowed = self.client.get('/api/v1/machines/servers/')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(still_denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data['count'], 1)

    def test_summary_requires_view_permission_only(self):
        """概要卡接口按查看权限校验，不应要求目录中不存在的 summary 操作权限。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')

        response = self.client.get('/api/v1/machines/servers/summary/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 1)
        self.assertEqual(response.data['unassigned'], 1)

    def test_create_requires_create_action_permission(self):
        """新增机器必须额外具备新增操作权限。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        payload = {'name': 'node-02', 'ip_address': '10.0.0.12', 'ssh_port': 22, 'ssh_user': 'root'}

        denied = self.client.post('/api/v1/machines/servers/', payload, format='json')
        self.grant('page.machine.servers.create')
        allowed = self.client.post('/api/v1/machines/servers/', payload, format='json')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(allowed.data['roles'], [])
        self.assertEqual(allowed.data['cluster_name'], '')
        self.assertEqual(allowed.data['role_display'], '未分配')

    def test_duplicate_ip_is_rejected_with_chinese_message(self):
        """重复的机器 IP 必须被拒绝并给出中文提示。"""
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.create')

        response = self.client.post(
            '/api/v1/machines/servers/',
            {'name': 'node-03', 'ip_address': '10.0.0.11', 'ssh_port': 22, 'ssh_user': 'root'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('该 IP 地址已存在', str(response.data))

    def test_connection_test_requires_test_action_permission(self):
        """连接测试必须额外具备测试操作权限，并据检测结果更新机器状态。"""
        self.machine.ip_address = '127.0.0.1'
        self.machine.ssh_port = 1
        self.machine.save(update_fields=['ip_address', 'ssh_port'])
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')

        denied = self.client.post(f'/api/v1/machines/servers/{self.machine.id}/test/')
        self.grant('page.machine.servers.test')
        allowed = self.client.post(f'/api/v1/machines/servers/{self.machine.id}/test/')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data['status'], 'unavailable')
        self.assertTrue(allowed.data['last_tested_at'])

    def test_password_is_encrypted_at_rest_and_never_returned(self):
        """SSH 密码必须 SM4 加密落库，且任何接口响应都不返回密码本身。"""
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.create')

        response = self.client.post(
            '/api/v1/machines/servers/',
            {'name': 'node-04', 'ip_address': '10.0.0.14', 'ssh_port': 22, 'ssh_user': 'root', 'password': 'S3cret-中文密码'},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['has_password'])
        self.assertNotIn('password', response.data)
        self.assertNotIn('encrypted_password', response.data)

        machine = Machine.objects.get(pk=response.data['id'])
        self.assertTrue(machine.encrypted_password.startswith('sm4:v1:'))
        self.assertNotIn('S3cret', machine.encrypted_password)
        self.assertEqual(machine.get_password(), 'S3cret-中文密码')
        self.assertIsNotNone(machine.password_rotated_at)

        listing = self.client.get('/api/v1/machines/servers/')
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn('S3cret', str(listing.data))

    def test_blank_password_on_update_keeps_existing_password(self):
        """编辑时密码留空表示不修改，已托管密码不会被清空。"""
        self.machine.set_password('origin-password')
        self.machine.save()
        original_ciphertext = self.machine.encrypted_password
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.update')

        response = self.client.patch(
            f'/api/v1/machines/servers/{self.machine.id}/',
            {'remark': '换机房', 'password': ''},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['has_password'])
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.encrypted_password, original_ciphertext)
        self.assertEqual(self.machine.get_password(), 'origin-password')

    def test_password_whitespace_is_preserved(self):
        """密码首尾空格属于密码内容，不能被自动去除。"""
        self.machine.set_password('  padded  ')
        self.machine.save()

        self.machine.refresh_from_db()
        self.assertEqual(self.machine.get_password(), '  padded  ')

    @patch('k8s_machine.views.run_ssh_password_auth')
    def test_connection_test_authenticates_with_stored_password(self, mocked_ssh):
        """已托管密码时必须用解密后的密码做真实 SSH 登录校验。"""
        self.machine.set_password('stored-secret')
        self.machine.save()
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.test')
        mocked_ssh.return_value = {
            'success': True,
            'detail': 'SSH 登录成功，用户名和密码有效',
            'resolved_address': '10.0.0.11',
        }

        response = self.client.post(f'/api/v1/machines/servers/{self.machine.id}/test/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked_ssh.call_args.args, ('10.0.0.11', 22, 'root', 'stored-secret'))
        self.assertEqual(response.data['status'], 'available')
        self.assertIn('SSH 登录成功', response.data['last_test_message'])
        self.assertNotIn('stored-secret', str(response.data))

        log = AuditLog.objects.filter(action='Machine.test').latest('created_at')
        self.assertEqual(log.detail['auth_mode'], 'ssh_password')
        self.assertNotIn('stored-secret', str(log.detail))

    @patch('k8s_machine.views.run_telnet')
    @patch('k8s_machine.views.run_ssh_password_auth')
    def test_connection_test_falls_back_to_port_probe_without_password(self, mocked_ssh, mocked_telnet):
        """未托管密码时只探测端口，不尝试 SSH 登录。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.test')
        mocked_telnet.return_value = {'success': True, 'detail': '目标 TCP 端口已开放'}

        response = self.client.post(f'/api/v1/machines/servers/{self.machine.id}/test/')

        self.assertEqual(response.status_code, 200)
        mocked_ssh.assert_not_called()
        mocked_telnet.assert_called_once_with('10.0.0.11', 22)
        self.assertIn('只检测端口', response.data['last_test_message'])
        self.assertEqual(response.data['status'], 'available')

    @patch('k8s_machine.models.Machine.get_password')
    def test_password_decrypt_failure_is_reported_without_crashing(self, mocked_get_password):
        """密码解密失败时给出可操作提示，而不是抛出 500。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.test')
        mocked_get_password.side_effect = ValueError('SM4 ciphertext authentication failed')

        response = self.client.post(f'/api/v1/machines/servers/{self.machine.id}/test/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'unavailable')
        self.assertIn('密码解密失败', response.data['last_test_message'])

        log = AuditLog.objects.filter(action='Machine.test').latest('created_at')
        self.assertEqual(log.detail['auth_mode'], 'decrypt_failed')

    def test_assign_node_updates_roles_and_cluster_with_update_permission(self):
        """绑定节点必须额外具备编辑权限，成功后写入多角色与所属集群。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        payload = {
            'cluster_name': 'prod-k8s-01',
            'nodes': [{'id': self.machine.id, 'roles': ['etcd', 'master', 'master']}],
        }

        denied = self.client.post('/api/v1/machines/servers/assign/', payload, format='json')
        self.grant('page.machine.servers.update')
        allowed = self.client.post('/api/v1/machines/servers/assign/', payload, format='json')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data['assigned'], 1)

        self.machine.refresh_from_db()
        # 角色按模型清单顺序归一化，重复项被去掉。
        self.assertEqual(self.machine.roles, ['master', 'etcd'])
        self.assertEqual(self.machine.cluster_name, 'prod-k8s-01')
        self.assertEqual(allowed.data['machines'][0]['role_display'], 'master / etcd')

        log = AuditLog.objects.filter(action='Machine.assign').latest('created_at')
        self.assertEqual(log.detail['cluster_name'], 'prod-k8s-01')
        self.assertEqual(log.detail['roles'], ['master', 'etcd'])

    def test_assign_rejects_invalid_payload(self):
        """缺少部署名称、缺少角色或角色非法时都必须拒绝且不落库。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.update')
        url = '/api/v1/machines/servers/assign/'

        no_cluster = self.client.post(url, {'nodes': [{'id': self.machine.id, 'roles': ['node']}]}, format='json')
        no_roles = self.client.post(
            url, {'cluster_name': 'prod-k8s-01', 'nodes': [{'id': self.machine.id, 'roles': []}]}, format='json'
        )
        bad_role = self.client.post(
            url, {'cluster_name': 'prod-k8s-01', 'nodes': [{'id': self.machine.id, 'roles': ['worker']}]}, format='json'
        )

        self.assertEqual(no_cluster.status_code, 400)
        self.assertEqual(no_roles.status_code, 400)
        self.assertEqual(bad_role.status_code, 400)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.roles, [])
        self.assertEqual(self.machine.cluster_name, '')

    def test_assign_rebinds_machine_owned_by_another_cluster(self):
        """已属于其它集群的机器允许改绑，旧绑定被覆盖并记入审计。"""
        self.machine.set_roles(['node'])
        self.machine.cluster_name = 'old-cluster'
        self.machine.save()
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.update')

        response = self.client.post(
            '/api/v1/machines/servers/assign/',
            {'cluster_name': 'prod-k8s-01', 'nodes': [{'id': self.machine.id, 'roles': ['master']}]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.cluster_name, 'prod-k8s-01')
        self.assertEqual(self.machine.roles, ['master'])
        log = AuditLog.objects.filter(action='Machine.assign').latest('created_at')
        self.assertEqual(log.detail['previous_cluster_name'], 'old-cluster')
        self.assertEqual(log.detail['previous_roles'], ['node'])

    def test_unassign_restores_machine_and_keeps_other_clusters(self):
        """解绑把机器还原为未分配，且不会误清属于其它集群的机器。"""
        self.machine.set_roles(['node'])
        self.machine.cluster_name = 'prod-k8s-01'
        self.machine.save()
        other = Machine.objects.create(
            organization=self.machine.organization,
            name='node-09',
            ip_address='10.0.0.19',
            ssh_port=22,
            ssh_user='root',
            roles=['etcd'],
            cluster_name='other-cluster',
        )
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        self.grant('page.machine.servers.update')

        response = self.client.post(
            '/api/v1/machines/servers/unassign/',
            {'cluster_name': 'prod-k8s-01', 'ids': [self.machine.id, other.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['unassigned'], 1)
        self.machine.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.machine.roles, [])
        self.assertEqual(self.machine.cluster_name, '')
        # 不属于该部署的机器保持原绑定。
        self.assertEqual(other.roles, ['etcd'])
        self.assertEqual(other.cluster_name, 'other-cluster')
        self.assertTrue(AuditLog.objects.filter(action='Machine.unassign').exists())

    def test_summary_counts_each_role_of_a_multi_role_machine(self):
        """概要统计按角色分别计数，同一台机器承担的多个角色都计入。"""
        self.machine.set_roles(['master', 'etcd'])
        self.machine.save()
        Machine.objects.create(
            organization=self.machine.organization,
            name='node-10',
            ip_address='10.0.0.20',
            ssh_port=22,
            ssh_user='root',
            roles=['node'],
        )
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')

        response = self.client.get('/api/v1/machines/servers/summary/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 2)
        self.assertEqual(response.data['master'], 1)
        self.assertEqual(response.data['etcd'], 1)
        self.assertEqual(response.data['node'], 1)
        self.assertEqual(response.data['unassigned'], 0)

    def test_usage_collects_only_the_requested_machines(self):
        """水位接口按 ids 逐台采集，采集口径交给 ``host_usage``，接口只负责取机器与校验入参。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        seen = []

        def fake_collect(machines):
            seen.extend(machine.pk for machine in machines)
            return {
                'sampled_at': '2026-09-30 09:10:03',
                'items': [
                    {
                        'machine_id': machine.pk, 'status': 'ok', 'error': '', 'hostname': machine.name,
                        'cpu_percent': 12.5, 'memory_percent': 60.0, 'disk_percent': 41.0,
                    }
                    for machine in machines
                ],
            }

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', side_effect=fake_collect):
            response = self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [self.machine.pk])
        self.assertEqual(response.data['items'][0]['machine_id'], self.machine.pk)
        self.assertEqual(response.data['items'][0]['cpu_percent'], 12.5)
        # 采样时刻由缓存行决定（采集那一刻），而不是沿用采集器自己那个字符串。
        self.assertEqual(response.data['sampled_at'], MachineMetric.objects.get().as_item()['sampled_at'])

    def test_usage_serves_the_second_request_from_cache(self):
        """缓存没过期就不再连机器：第二次打开页面不该再 SSH 一遍。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        calls = []

        def fake_collect(machines):
            calls.append([machine.pk for machine in machines])
            return {'sampled_at': '', 'items': [{'machine_id': machine.pk, 'status': 'ok'} for machine in machines]}

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', side_effect=fake_collect):
            first = self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')
            second = self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')

        self.assertEqual(len(calls), 1)
        self.assertEqual(first.data['items'], second.data['items'])

    def test_usage_recollects_after_the_cache_expires(self):
        """缓存过了 5 分钟有效期就重新采一次。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        calls = []

        def fake_collect(machines):
            calls.append(len(machines))
            return {'sampled_at': '', 'items': [{'machine_id': machine.pk, 'status': 'ok'} for machine in machines]}

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', side_effect=fake_collect):
            self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')
            MachineMetric.objects.update(
                sampled_at=timezone.now() - timedelta(seconds=metrics.CACHE_TTL_SECONDS + 30),
            )
            self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')

        self.assertEqual(calls, [1, 1])

    def test_usage_keeps_a_failed_sample_cached(self):
        """采集失败同样算「最近一次的结论」，在有效期内不会反复重连一台关机的机器。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        calls = []

        def fake_collect(machines):
            calls.append(len(machines))
            return {
                'sampled_at': '',
                'items': [
                    {'machine_id': machine.pk, 'status': 'failed', 'error': '连接超时'}
                    for machine in machines
                ],
            }

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', side_effect=fake_collect):
            first = self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')
            second = self.client.get(f'/api/v1/machines/servers/usage/?ids={self.machine.pk}')

        self.assertEqual(len(calls), 1)
        self.assertEqual(first.data['items'][0]['status'], 'failed')
        self.assertEqual(first.data['items'][0]['error'], '连接超时')
        self.assertIsNone(second.data['items'][0]['cpu_percent'])

    def test_usage_requires_view_permission_only(self):
        """看机器列表就能看它的水位，不必额外操作权限。"""
        self.revoke_all()
        denied = self.client.get('/api/v1/machines/servers/usage/?ids=1')
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', return_value={'sampled_at': '', 'items': []}):
            allowed = self.client.get('/api/v1/machines/servers/usage/?ids=1')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_usage_rejects_missing_or_malformed_ids(self):
        """没给机器、给了非数字都按中文原因回 400，而不是悄悄去采集全表。"""
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')

        missing = self.client.get('/api/v1/machines/servers/usage/')
        malformed = self.client.get('/api/v1/machines/servers/usage/?ids=abc')

        self.assertEqual(missing.status_code, 400)
        self.assertIn('请指定要采集的机器', str(missing.data))
        self.assertEqual(malformed.status_code, 400)
        self.assertIn('必须是数字', str(malformed.data))

    def test_usage_ignores_machines_outside_the_visible_scope(self):
        """不可见的机器一律当不存在：既不采集，也不在响应里透露它存在。"""
        other = Machine.objects.create(
            organization=Organization.objects.create(name='别的组织', slug='other-org'),
            name='hidden-node',
            ip_address='10.9.9.9',
            ssh_port=22,
            ssh_user='root',
        )
        self.revoke_all()
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')
        seen = []

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', side_effect=lambda machines: seen.extend(machines) or {'sampled_at': '', 'items': []}):
            response = self.client.get(f'/api/v1/machines/servers/usage/?ids={other.pk}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [])
        self.assertEqual(response.data['items'], [])

    def test_disable_status_is_not_overwritten_by_test_result(self):
        """已禁用机器在做连通性检测后仍保持禁用状态。"""
        self.machine.status = 'disabled'
        self.machine.save(update_fields=['status'])

        self.machine.mark_test_result(True, '目标 TCP 端口已开放')

        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status, 'disabled')
        self.assertEqual(self.machine.last_test_message, '目标 TCP 端口已开放')
        self.assertIsNotNone(self.machine.last_tested_at)


class MachineMetricRefreshTests(TestCase):
    """后台周期刷新只采「到期」的机器，采集间隔由查询条件保证而不是靠定时器攒。"""

    def setUp(self):
        """准备三台机器：一台新鲜、一台过期、一台从没采过。"""
        organization = Organization.objects.first()
        self.fresh = Machine.objects.create(organization=organization, name='fresh', ip_address='10.1.0.1')
        self.stale = Machine.objects.create(organization=organization, name='stale', ip_address='10.1.0.2')
        self.missing = Machine.objects.create(organization=organization, name='missing', ip_address='10.1.0.3')
        now = timezone.now()
        MachineMetric.objects.create(machine=self.fresh, sampled_at=now, status='ok')
        MachineMetric.objects.create(
            machine=self.stale,
            sampled_at=now - timedelta(seconds=metrics.REFRESH_INTERVAL_SECONDS + 10),
            status='ok',
        )

    def test_due_machines_skip_the_fresh_one(self):
        """新鲜的那台不入选；没采过的排在最前，其次是缓存最旧的。"""
        due = list(metrics.due_machines())

        self.assertEqual([machine.pk for machine in due], [self.missing.pk, self.stale.pk])

    def test_refresh_due_collects_once_and_writes_back(self):
        """一轮刷新把到期的那几台采回来写进缓存，新鲜的那台不动。"""
        seen = []

        def fake_collect(machines):
            seen.append([machine.pk for machine in machines])
            return {
                'sampled_at': '',
                'items': [
                    {
                        'machine_id': machine.pk, 'status': 'ok', 'hostname': machine.name,
                        'cpu_percent': 3.0, 'memory_percent': 4.0, 'disk_percent': 5.0,
                    }
                    for machine in machines
                ],
            }

        with patch('k8s_deployment.services.host_usage.collect_machine_usage', side_effect=fake_collect):
            collected = metrics.refresh_due()
            # 刚采完，缓存全都新鲜，再调一次不该重复采集。
            again = metrics.refresh_due()

        self.assertEqual(collected, 2)
        self.assertEqual(again, 0)
        self.assertEqual(seen, [[self.missing.pk, self.stale.pk]])
        self.assertEqual(MachineMetric.objects.get(machine=self.missing).cpu_percent, 3.0)

    def test_refresh_due_respects_the_batch_limit(self):
        """机器多于一轮能采的量时只取前几台，其余的留给下一轮。"""
        with patch('k8s_deployment.services.host_usage.collect_machine_usage', return_value={'sampled_at': '', 'items': []}):
            collected = metrics.refresh_due(limit=1)

        self.assertEqual(collected, 1)

    def test_cached_items_returns_the_oldest_sample_time(self):
        """一批数据里的时刻取最旧的那个：界面说「采集于 …」时不能报新不报旧。"""
        items, sampled_at = metrics.cached_items([self.fresh, self.stale])

        self.assertEqual(len(items), 2)
        self.assertEqual(
            sampled_at,
            timezone.localtime(MachineMetric.objects.get(machine=self.stale).sampled_at).strftime('%Y-%m-%d %H:%M:%S'),
        )


class ReleaseClusterMachinesTests(TestCase):
    """删除集群时按标识或名称解绑机器。

    规划 6.2 之后新数据的归属存的是集群标识，而存量数据存的是中文名称；
    删集群必须两种都清干净，否则机器列表里会留下一批指向不存在集群的角色。
    """

    def setUp(self):
        """准备一个普通用户与三台归属各异的机器。

        参数：无。
        返回：无显式返回值。
        副作用：创建数据库记录。
        """
        organization = Organization.objects.first()
        self.user = User.objects.create_user(username='release-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=organization, role='member')
        self.request = RequestFactory().post('/api/v1/k8s/clusters/1/')
        self.request.user = self.user
        self.by_slug = Machine.objects.create(
            organization=organization, name='node-a', ip_address='10.0.1.1', ssh_port=22,
            ssh_user='root', roles=['master', 'etcd'], cluster_name='prod-hz-01',
        )
        self.by_name = Machine.objects.create(
            organization=organization, name='node-b', ip_address='10.0.1.2', ssh_port=22,
            ssh_user='root', roles=['node'], cluster_name='生产集群',
        )
        self.other = Machine.objects.create(
            organization=organization, name='node-c', ip_address='10.0.1.3', ssh_port=22,
            ssh_user='root', roles=['node'], cluster_name='别的集群',
        )

    def test_both_the_slug_and_the_legacy_name_are_released(self):
        """标识与名称两个键都能命中，其余集群的机器不受影响。"""
        released = release_cluster_machines(self.request, 'prod-hz-01', '生产集群')

        self.assertEqual(sorted(released), sorted([self.by_slug.pk, self.by_name.pk]))
        for machine in (self.by_slug, self.by_name):
            machine.refresh_from_db()
            self.assertEqual(machine.cluster_name, '')
            self.assertEqual(machine.normalized_roles(), [])
        self.other.refresh_from_db()
        self.assertEqual(self.other.cluster_name, '别的集群')
        self.assertEqual(self.other.normalized_roles(), ['node'])

    def test_empty_keys_release_nothing(self):
        """没有可用的键时直接返回，不能把 filter(cluster_name__in=[]) 之外的机器也清掉。"""
        self.assertEqual(release_cluster_machines(self.request, '', None), [])
        self.by_slug.refresh_from_db()
        self.assertEqual(self.by_slug.cluster_name, 'prod-hz-01')

    def test_release_is_audited(self):
        """解绑属于越权改写机器归属的动作，必须留下审计记录。"""
        release_cluster_machines(self.request, 'prod-hz-01')

        log = AuditLog.objects.filter(action='Machine.unassign', resource=str(self.by_slug)).latest('created_at')
        self.assertEqual(log.actor, 'release-user')
        self.assertEqual(log.detail['previous_cluster_name'], 'prod-hz-01')


class MachineClusterDisplayNameTests(TestCase):
    """机器列表里的「所属集群」显示的是集群名称，而机器表里存的是集群标识。

    两者对不上，用户就会看到一串 kubeasz 的目录名（``prod-hz-01``），而他在集群列表里认识的
    是「生产集群」—— 既对不上号，也没法照着屏幕上的字去搜机器。
    """

    def setUp(self):
        """准备一个组织、一个集群、两台机器与一个有查看权限的用户。

        参数：无。
        返回：无显式返回值。
        副作用：创建数据库记录。
        """
        organization = Organization.objects.first()
        self.organization = organization
        self.cluster = K8sCluster.objects.create(
            organization=organization,
            name='生产集群',
            slug='prod-hz-01',
            origin='deployed',
            kube_version='1.36',
            status='configured',
        )
        self.bound = Machine.objects.create(
            organization=organization, name='node-a', ip_address='10.0.2.1', ssh_port=22,
            ssh_user='root', roles=['master', 'etcd'], cluster_name='prod-hz-01',
        )
        self.orphan = Machine.objects.create(
            organization=organization, name='node-b', ip_address='10.0.2.2', ssh_port=22,
            ssh_user='root', roles=['node'], cluster_name='已经不存在的集群',
        )
        self.user = User.objects.create_user(username='machine-viewer', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=organization, role='member')
        policy = PermissionPolicy.objects.create(
            name='机器查看权限', subject_type='user', user=self.user, status='available',
        )
        for code in ('menu.machine', 'page.machine.servers.view'):
            PermissionRule.objects.update_or_create(
                policy=policy, permission_code=code, defaults={'effect': 'allow'},
            )
        clear_permission_cache()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def list_machines(self, keyword=''):
        """按关键字查询机器列表。

        参数：`keyword` 表示搜索关键字，留空表示不带关键字。
        返回：返回接口响应的机器列表。
        副作用：发起一次 HTTP 请求。
        """
        response = self.client.get('/api/v1/machines/servers/', {'q': keyword} if keyword else {})
        self.assertEqual(response.status_code, 200)
        return response.data['results']

    def test_display_name_is_resolved_from_the_cluster_record(self):
        """标识能解析出集群时给出展示名称，同时保留原始标识给调用方自己比对。"""
        item = self.list_machines()[0]
        self.assertEqual(item['cluster_name'], 'prod-hz-01')
        self.assertEqual(item['cluster_display_name'], '生产集群')

    def test_display_name_is_empty_when_the_cluster_is_gone(self):
        """集群记录已经不在了就返回空串，由调用方回落到标识照实显示，不编名字。"""
        item = next(entry for entry in self.list_machines() if entry['name'] == 'node-b')
        self.assertEqual(item['cluster_name'], '已经不存在的集群')
        self.assertEqual(item['cluster_display_name'], '')

    def test_search_matches_the_display_name(self):
        """照着屏幕上看到的名称搜索要能搜到，否则那列显示的就不是一个能用的名字。"""
        names = [item['name'] for item in self.list_machines('生产集群')]
        self.assertEqual(names, ['node-a'])

    def test_search_still_matches_the_slug(self):
        """标识本身仍可作为搜索词：它出现在导入型集群与历史数据里，也是机器归属的真身。"""
        names = [item['name'] for item in self.list_machines('prod-hz-01')]
        self.assertEqual(names, ['node-a'])

    def test_search_does_not_leak_other_organizations(self):
        """别的组织里同名的集群不该把本组织的机器搜出来。"""
        other_org = Organization.objects.create(name='别的组织', slug='other-org')
        K8sCluster.objects.create(
            organization=other_org, name='生产集群', slug='other-prod', origin='deployed',
            kube_version='1.36', status='configured',
        )
        Machine.objects.create(
            organization=other_org, name='node-x', ip_address='10.0.3.1', ssh_port=22,
            ssh_user='root', cluster_name='other-prod',
        )

        names = [item['name'] for item in self.list_machines('生产集群')]
        self.assertEqual(names, ['node-a'])


class FakeChannel:
    """假 pty 通道：记下写进来的字节与窗口变更，按预置脚本吐出输出。

    真实通道要一台能登录的机器，但这里要验的是「字节有没有原样送进 pty」「尺寸有没有真的
    发给远端」「远端退出时会不会收尾」—— 一个列表加几个计数器就够了。

    `recv` 的三种语义都要有，因为消费者对它们走三条不同的分支：预置的字节段、``None``
    （真通道非阻塞轮询时的手空）、空字节（远端退出）。
    """

    def __init__(self, chunks=(), eof=False):
        """初始化空的预置输出与记录容器。

        参数：`chunks` 表示读线程依次会拿到的字节段；`eof` 表示预置输出取完之后远端是否已退出。
        返回：无显式返回值。
        副作用：无。
        """
        self.chunks = list(chunks)
        self.eof = eof
        self.sent = []
        self.resizes = []
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        """记录会话设置的读超时。

        参数：`value` 表示超时秒数。
        返回：无显式返回值。
        副作用：修改当前对象状态。
        """
        self.timeout = value

    def recv(self, _size):
        """按非阻塞语义取一段输出：没数据返回 None，远端结束返回空字节。

        参数：`_size` 表示本次最多读多少字节（假通道不需要）。
        返回：返回一段字节、``None`` 或空字节。
        副作用：弹出已取走的预置输出。
        """
        if self.chunks:
            return self.chunks.pop(0)
        return b'' if self.eof else None

    def sendall(self, payload):
        """记下写进通道的字节（真通道的写入入口就叫 `sendall`）。

        参数：`payload` 表示要写进 pty 的字节。
        返回：无显式返回值。
        副作用：追加到已写记录。
        """
        if self.closed:
            raise OSError('通道已关闭')
        self.sent.append(payload)

    def resize_pty(self, width=None, height=None):
        """记下窗口变更请求。

        参数：`width` / `height` 表示新的列数与行数。
        返回：无显式返回值。
        副作用：追加到尺寸记录。
        """
        self.resizes.append((width, height))

    def close(self):
        """标记通道已关闭。

        参数：无。
        返回：无显式返回值。
        副作用：修改当前对象状态。
        """
        self.closed = True


class FakeSshClient:
    """假 SSH 客户端：`invoke_shell` 返回给定的假通道，并记下申请 pty 时的参数。"""

    def __init__(self, channel):
        """初始化。

        参数：`channel` 表示 `invoke_shell` 要返回的通道。
        返回：无显式返回值。
        副作用：无。
        """
        self.channel = channel
        self.invocations = []
        self.closed = False

    def invoke_shell(self, **kwargs):
        """记录申请 pty 的参数并返回假通道。

        参数：`kwargs` 表示终端类型与初始尺寸。
        返回：返回构造时给定的假通道。
        副作用：追加到申请记录。
        """
        self.invocations.append(kwargs)
        return self.channel

    def close(self):
        """标记连接已关闭。

        参数：无。
        返回：无显式返回值。
        副作用：修改当前对象状态。
        """
        self.closed = True


class MachineSshTerminalTestBase(TestCase):
    """SSH 终端的公共夹具：一台托管了密码的机器、一个有权限的用户、一条假 pty。

    终端已经是一条 WebSocket（``k8s_machine.consumers.MachineSshConsumer``），用例经
    ``WebsocketCommunicator`` 直连真实的 ASGI 应用 —— 路由挂没挂对、事件循环里碰数据库
    会不会出事，只有这样才测得出来。连不出去的那一环（paramiko 客户端）换成假的；凭据选择、
    pty 申请参数、会话记录与审计全是真的。

    用例要写成 ``async def``：Django 会把异步用例放进事件循环里跑，消费者就在同一个循环里
    收发消息。异步用例里**不能直接读数据库**（Django 会抛 SynchronousOnlyOperation），
    所以夹具提供了 `query` / `wait_for` 两个助手。
    """

    def setUp(self):
        """建组织、用户、策略、机器与假 pty，并签发一张访问令牌。

        参数：无。
        返回：无显式返回值。
        副作用：创建数据库记录。
        """
        self.organization = Organization.objects.first()
        self.user = User.objects.create_user(username='ssh-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.organization, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='SSH 终端权限', subject_type='user', user=self.user, status='available',
        )
        self.machine = Machine.objects.create(
            organization=self.organization,
            name='ssh-node',
            ip_address='10.9.0.11',
            ssh_port=22,
            ssh_user='root',
        )
        self.machine.set_password('S3cret-Pa55')
        self.machine.save()
        self.channel = FakeChannel()
        self.ssh_client = FakeSshClient(self.channel)
        # 浏览器没法给 WebSocket 带 Authorization 头，令牌只能走查询串。
        self.token = issue_token(self.user, token_type='access')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.grant('menu.machine')
        self.grant('page.machine.servers.view')

    def grant(self, code):
        """向当前测试用户策略写入单项允许规则。

        参数：`code` 表示权限码。
        返回：无显式返回值。
        副作用：写权限规则并清空权限缓存。
        """
        PermissionRule.objects.update_or_create(
            policy=self.policy, permission_code=code, defaults={'effect': 'allow'},
        )
        clear_permission_cache()

    def drop_password(self):
        """清掉托管密码（`set_password('')` 是「保持原密码」的意思，只能直接改字段）。"""
        self.machine.encrypted_password = ''
        self.machine.save(update_fields=['encrypted_password', 'updated_at'])

    async def grant_async(self, code):
        """在异步用例里授权。

        参数：`code` 表示权限码。
        返回：无显式返回值。
        副作用：写权限规则并清空权限缓存。

        `grant` 本身是同步的，异步用例里直接调用会被 Django 拦住（SynchronousOnlyOperation），
        必须借 `query` 回到同步执行器。
        """
        await self.query(lambda: self.grant(code))

    async def drop_password_async(self):
        """在异步用例里清掉托管密码。

        参数：无。
        返回：无显式返回值。
        副作用：修改机器记录。
        """
        await self.query(self.drop_password)

    def use_password_login(self, error=None):
        """把密码通道换成假客户端，并记下每次登录用的参数。

        参数：`error` 表示改成抛出该异常（模拟登录失败），省略表示登录成功。
        返回：返回调用参数列表（每建立一条连接追加一项）。
        副作用：打补丁替换凭据服务里的密码通道，用例结束时自动恢复。
        """
        calls = []

        def opener(**kwargs):
            """假的密码登录入口：记下参数并返回假客户端。"""
            calls.append(kwargs)
            if error is not None:
                raise error
            return self.ssh_client

        patcher = patch.object(credentials, 'ssh_client', side_effect=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def use_key_login(self):
        """把部署密钥通道换成假客户端（没托管密码时才会走到这条通道）。

        参数：无。
        返回：无显式返回值。
        副作用：打补丁替换密钥通道，用例结束时自动恢复。
        """
        patcher = patch.object(node_registry, 'open_session', return_value=self.ssh_client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_key_available(self, available):
        """声明平台部署密钥是否在位上（只影响凭据选择，不去碰真实文件）。

        参数：`available` 表示密钥是否存在。
        返回：无显式返回值。
        副作用：打补丁替换密钥存在性判断，用例结束时自动恢复。
        """
        patcher = patch.object(machine_ssh, 'key_available', return_value=available)
        patcher.start()
        self.addCleanup(patcher.stop)

    def socket_url(self, **params):
        """返回终端 WebSocket 的地址。

        参数：`params` 表示查询串参数（`cols` / `rows` / `token` / `machine_id`）。
        返回：返回形如 `/ws/v1/machines/<id>/ssh?...` 的路径。
        副作用：无。
        """
        machine_id = params.pop('machine_id', self.machine.pk)
        params.setdefault('token', self.token)
        return f'/ws/v1/machines/{machine_id}/ssh?{urlencode(params)}'

    async def open_terminal(self, timeout=10, **params):
        """连上终端 WebSocket。

        参数：`timeout` 表示握手等待上限（秒）；`params` 表示查询串参数。
        返回：返回已握手的 ``WebsocketCommunicator``。
        副作用：建立连接；成功时写下会话记录与审计日志。

        握手**总是**被接受：拒绝的原因（没权限、机器禁用、凭据缺失）要说进终端里给用户看，
        一旦在握手阶段拒掉，浏览器只会收到一句「连接失败」，后端的解释就没地方说了。
        """
        communicator = WebsocketCommunicator(application, self.socket_url(**params))
        connected, _ = await communicator.connect(timeout=timeout)
        self.assertTrue(connected, 'WebSocket 握手应当被接受，拒绝原因走终端输出')
        return communicator

    async def close_terminal(self, communicator):
        """从浏览器这一侧关掉终端，并等消费者收尾完毕。

        参数：`communicator` 表示终端连接。
        返回：无显式返回值。
        副作用：发出断开消息；消费者由此收尾 pty 与会话记录。
        """
        await communicator.disconnect(timeout=10)

    async def receive(self, communicator, timeout=10):
        """取下一条服务端消息。

        参数：`communicator` 表示终端连接；`timeout` 表示等待上限（秒）。
        返回：返回 channels 的输出消息字典（`websocket.send` 或 `websocket.close`）。
        副作用：无。
        """
        return await communicator.receive_output(timeout=timeout)

    async def read_text(self, communicator, timeout=10):
        """取下一条终端输出。

        参数：`communicator` 表示终端连接；`timeout` 表示等待上限（秒）。
        返回：返回这段输出文本。
        副作用：无。

        输出必须是**文本帧**：控制消息走二进制帧，两条通道分开，终端的输出里哪怕整段都是
        `{` 开头也不会被误当成控制消息。
        """
        message = await self.receive(communicator, timeout=timeout)
        self.assertEqual(message['type'], 'websocket.send', f'期望终端输出，实际收到 {message}')
        self.assertIn('text', message, '终端输出必须是文本帧')
        return message['text']

    async def read_control(self, communicator, timeout=10):
        """取下一条控制消息（二进制帧里的 JSON）。

        参数：`communicator` 表示终端连接；`timeout` 表示等待上限（秒）。
        返回：返回解析后的控制消息字典。
        副作用：无。
        """
        message = await self.receive(communicator, timeout=timeout)
        self.assertEqual(message['type'], 'websocket.send', f'期望控制消息，实际收到 {message}')
        self.assertIn('bytes', message, '控制消息必须是二进制帧')
        return json.loads(message['bytes'].decode('utf-8'))

    async def query(self, func):
        """在异步用例里读数据库。

        参数：`func` 表示一段同步的数据库操作（一般是 lambda）。
        返回：返回该操作的结果。
        副作用：读数据库。

        异步上下文里直接查库会被 Django 拦住，必须借 ``database_sync_to_async`` 回到同步
        执行器 —— 这也正是消费者自己写记录时走的同一条路。
        """
        return await database_sync_to_async(func)()

    async def records(self):
        """返回全部会话记录（按建立顺序）。

        参数：无。
        返回：返回会话记录列表。
        副作用：读数据库。
        """
        return await self.query(lambda: list(MachineSshSession.objects.order_by('pk')))

    async def wait_for(self, check, timeout=5, message='条件在超时前没有成立'):
        """轮询等待条件成立。

        参数：`check` 表示同步或异步的条件函数；`timeout` 表示等待上限（秒）；`message` 表示
            超时时给出的断言信息。
        返回：无显式返回值。
        副作用：按 20 毫秒间隔轮询。

        消费者的处理是异步的：``send_to`` 只把消息投进队列，这条消息要等事件循环下一轮才被
        处理，发完立刻断言会读到还没动过的状态。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = check()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.02)
        self.fail(message)


class MachineSshPermissionTests(MachineSshTerminalTestBase):
    """进入终端要 `page.machine.servers.ssh`：它独立于机器列表的查看权限。

    直连 shell 比看一眼列表重得多，所以它必须是一个能单独勾选的权限项；反过来，多了一个新
    权限码也不能把「看列表」卡住 —— 只授 view 的账号应当照常能用机器列表。
    """

    def test_list_still_works_without_the_ssh_action(self):
        """只有查看权限时列表照常能看 —— 新权限码不能把列表一起卡住。"""
        listing = self.client.get('/api/v1/machines/servers/')

        self.assertEqual(listing.status_code, 200)

    async def test_opening_without_the_ssh_action_is_refused(self):
        """没授 `page.machine.servers.ssh` 时给一句中文说明并断开，且根本不碰 SSH。"""
        communicator = await self.open_terminal(cols=100, rows=30)

        notice = await self.read_text(communicator)
        closing = await self.receive(communicator)

        self.assertIn('没有 SSH 登录权限', notice)
        self.assertIn('权限策略', notice)
        self.assertEqual(closing['type'], 'websocket.close')
        self.assertEqual(closing['code'], 4400)
        self.assertEqual(self.ssh_client.invocations, [])
        self.assertEqual(await self.query(MachineSshSession.objects.count), 0)

    async def test_granting_the_ssh_action_allows_opening(self):
        """授了权限就能拿到会话：控制帧里有目标、凭据模式与初始尺寸，记录落成 active。"""
        await self.grant_async('page.machine.servers.ssh')
        self.use_password_login()

        communicator = await self.open_terminal(cols=100, rows=30)
        ready = await self.read_control(communicator)

        self.assertEqual(ready['type'], 'ready')
        self.assertEqual(ready['mode'], 'password')
        self.assertEqual(ready['mode_label'], '托管密码')
        self.assertEqual((ready['host'], ready['port'], ready['user']), ('10.9.0.11', 22, 'root'))
        self.assertEqual((ready['cols'], ready['rows']), (100, 30))
        record = (await self.records())[0]
        self.assertEqual(record.status, 'active')
        self.assertEqual(record.actor, 'ssh-user')
        # 用 `organization_id` 而不是 `record.organization`：后者要回一次数据库，而异步用例里
        # 任何没被 `query` 包住的数据库访问都会被 Django 拦下。
        self.assertEqual(record.organization_id, self.organization.pk)
        self.assertEqual(record.machine_id, self.machine.pk)
        await self.close_terminal(communicator)

    async def test_a_bad_token_is_refused_without_touching_the_machine(self):
        """令牌无效就断掉：没登录的人不能借终端摸到任何一台机器。"""
        communicator = await self.open_terminal(token='not-a-token')

        notice = await self.read_text(communicator)
        closing = await self.receive(communicator)

        self.assertIn('连接失败', notice)
        self.assertEqual(closing['code'], 4400)
        self.assertEqual(self.ssh_client.invocations, [])
        self.assertEqual(await self.query(MachineSshSession.objects.count), 0)

    async def test_machine_outside_the_organization_is_not_found(self):
        """别的组织里的机器一律当作不存在，不能拿机器主键去摸。"""
        await self.grant_async('page.machine.servers.ssh')
        other_org = await self.query(
            lambda: Organization.objects.create(name='别的组织', slug='ssh-other-org'),
        )
        foreign = await self.query(lambda: Machine.objects.create(
            organization=other_org, name='foreign-node', ip_address='10.9.9.9',
            ssh_port=22, ssh_user='root',
        ))

        communicator = await self.open_terminal(machine_id=foreign.pk)
        notice = await self.read_text(communicator)

        self.assertIn('未找到可访问的机器', notice)
        self.assertEqual(await self.query(MachineSshSession.objects.count), 0)

    async def test_disabled_machine_is_refused(self):
        """已禁用的机器不建立会话 —— 禁用就是禁用，终端不能绕过去。"""
        await self.grant_async('page.machine.servers.ssh')
        await self.query(
            lambda: Machine.objects.filter(pk=self.machine.pk).update(status='disabled'),
        )

        communicator = await self.open_terminal()
        notice = await self.read_text(communicator)

        self.assertIn('已禁用', notice)
        self.assertEqual(await self.query(MachineSshSession.objects.count), 0)


class MachineSshSessionTests(MachineSshTerminalTestBase):
    """建连这一环：凭据怎么选、pty 怎么申请、记录与审计怎么写。

    连接本身用假客户端顶替（真连要一台能登录的机器），但凭据解析、pty 申请参数、会话记录与
    审计日志全是真的 —— 那才是这些用例要守住的东西。
    """

    async def test_open_uses_the_stored_password_and_leaks_no_secret(self):
        """托管了密码就用密码登进去，pty 按浏览器报的尺寸申请，任何地方都不出现密码。"""
        await self.grant_async('page.machine.servers.ssh')
        logins = self.use_password_login()

        communicator = await self.open_terminal(cols=132, rows=43)
        await self.read_control(communicator)

        self.assertEqual(len(logins), 1)
        self.assertEqual(logins[0]['password'], 'S3cret-Pa55')
        self.assertEqual(logins[0]['host'], '10.9.0.11')
        self.assertEqual(logins[0]['port'], 22)
        self.assertEqual(logins[0]['username'], 'root')
        # pty 按浏览器报上来的尺寸申请，终端类型固定为 xterm-256color。
        self.assertEqual(self.ssh_client.invocations[0]['width'], 132)
        self.assertEqual(self.ssh_client.invocations[0]['height'], 43)
        self.assertEqual(self.ssh_client.invocations[0]['term'], 'xterm-256color')
        self.assertEqual(self.channel.timeout, ssh_session.READ_TIMEOUT)

        record = (await self.records())[0]
        self.assertEqual(record.status, 'active')
        self.assertEqual((record.machine_name, record.ip_address, record.ssh_user), ('ssh-node', '10.9.0.11', 'root'))
        opened = await self.query(lambda: AuditLog.objects.get(action='Machine.ssh.open'))
        self.assertEqual(opened.detail['mode'], 'password')
        self.assertEqual(opened.detail['cols'], 132)
        self.assertEqual(opened.resource, 'ssh-node(10.9.0.11)')
        self.assertEqual(opened.actor, 'ssh-user')
        self.assertEqual(opened.ip_address, '127.0.0.1')
        # 密码只用于建连，任何一条日志里都不许出现。
        for audit in await self.query(lambda: list(AuditLog.objects.all())):
            self.assertNotIn('S3cret-Pa55', str(audit.detail))
        await self.close_terminal(communicator)

    async def test_open_falls_back_to_the_platform_key_when_there_is_no_password(self):
        """没托管密码的机器回落平台部署密钥，模式要如实报给界面。"""
        await self.grant_async('page.machine.servers.ssh')
        await self.drop_password_async()
        self.set_key_available(True)
        self.use_key_login()

        communicator = await self.open_terminal()
        ready = await self.read_control(communicator)

        self.assertEqual(ready['mode'], 'key')
        self.assertEqual(ready['mode_label'], '平台部署密钥')
        self.assertEqual((await self.records())[0].status, 'active')
        await self.close_terminal(communicator)

    async def test_open_without_any_credential_asks_for_a_password(self):
        """既没有托管密码也没有部署密钥时，让用户去补一份密码，并留下失败记录。"""
        await self.grant_async('page.machine.servers.ssh')
        await self.drop_password_async()
        self.set_key_available(False)

        communicator = await self.open_terminal()
        notice = await self.read_text(communicator)

        self.assertIn('补一份 SSH 密码', notice)
        record = (await self.records())[0]
        self.assertEqual(record.status, 'failed')
        self.assertIn('SSH 密码', record.failure_reason)
        self.assertIsNotNone(record.ended_at)
        # 连不上也要留痕：连着试十几次都失败，本身就是要被看见的事。
        failed = await self.query(lambda: AuditLog.objects.get(action='Machine.ssh.open'))
        self.assertFalse(failed.detail['success'])

    async def test_connection_failure_is_explained_and_recorded(self):
        """连不上时把目标地址和原因一起说清楚，并留一条失败记录。"""
        await self.grant_async('page.machine.servers.ssh')
        self.use_password_login(error=OSError('用户名或密码错误'))

        communicator = await self.open_terminal()
        notice = await self.read_text(communicator)

        self.assertIn('10.9.0.11:22', notice)
        self.assertIn('用户名或密码错误', notice)
        record = (await self.records())[0]
        self.assertEqual(record.status, 'failed')
        self.assertIsNotNone(record.ended_at)
        self.assertIn('用户名或密码错误', record.failure_reason)

    async def test_stale_active_record_from_a_previous_run_is_swept(self):
        """平台进程被 kill 后留下的 active 记录，在下一次开终端时被收掉，不留僵尸行。"""
        await self.grant_async('page.machine.servers.ssh')
        self.use_password_login()
        stale = await self.query(lambda: MachineSshSession.objects.create(
            organization=self.organization, machine=self.machine, machine_name='ssh-node',
            ip_address='10.9.0.11', ssh_user='root', actor='ssh-user', status='active',
        ))
        # created_at 是 auto_now_add，只能绕过模型层往前拨。
        await self.query(lambda: MachineSshSession.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(seconds=ssh_session.STALE_SESSION_SECONDS + 60),
        ))

        communicator = await self.open_terminal()
        await self.read_control(communicator)

        swept = await self.query(lambda: MachineSshSession.objects.get(pk=stale.pk))
        self.assertEqual(swept.status, 'closed')
        self.assertIn('遗留', swept.failure_reason)
        self.assertEqual((await self.records())[1].status, 'active')
        await self.close_terminal(communicator)


class MachineSshInteractionTests(MachineSshTerminalTestBase):
    """会话开起来之后：输入、尺寸、输出与收尾。"""

    async def opened(self, **params):
        """开一条已经连上的会话，并吃掉 ready 控制帧。

        参数：`params` 表示查询串参数（尺寸等）。
        返回：返回已握手的 ``WebsocketCommunicator``。
        副作用：写会话记录与审计日志。
        """
        await self.grant_async('page.machine.servers.ssh')
        self.use_password_login()
        communicator = await self.open_terminal(**params)
        await self.read_control(communicator)
        return communicator

    async def test_input_reaches_the_channel_and_is_audited(self):
        """敲进去的字节原样送进 pty，认出来的命令单独留一条审计。"""
        communicator = await self.opened()

        await communicator.send_to(text_data='uptime\r')
        await self.wait_for(lambda: self.channel.sent == [b'uptime\r'], message='输入没有送到 pty')

        # 审计写在转发之前：pty 收到字节时它一定已经落库了。
        audit = await self.query(lambda: AuditLog.objects.get(action='Machine.ssh.command'))
        self.assertEqual(audit.detail['command'], 'uptime')
        self.assertEqual(audit.resource, 'ssh-node(10.9.0.11)')
        self.assertEqual(audit.actor, 'ssh-user')
        self.assertEqual(audit.ip_address, '127.0.0.1')

        await self.close_terminal(communicator)
        record = (await self.records())[0]
        self.assertEqual(record.status, 'closed')
        self.assertEqual(record.command_count, 1)
        self.assertEqual(audit.detail['session_id'], record.pk)
        # 关闭审计只写一条：断开与收尾可能先后触发，重复的那次不算数。
        closes = await self.query(lambda: AuditLog.objects.filter(action='Machine.ssh.close').count())
        self.assertEqual(closes, 1)

    async def test_pasted_json_is_input_but_resize_is_a_control_message(self):
        """以 `{` 开头不等于控制消息：粘贴一段 JSON 是合法输入，只有 resize 才被吞掉。"""
        communicator = await self.opened()

        await communicator.send_to(text_data='{"a": 1}')
        await self.wait_for(lambda: self.channel.sent == [b'{"a": 1}'], message='JSON 输入被当成控制消息吞掉了')

        await communicator.send_to(text_data=json.dumps({'type': 'resize', 'cols': 90, 'rows': 24}))
        await self.wait_for(lambda: self.channel.resizes == [(90, 24)], message='尺寸没有发给远端')

        self.assertEqual(len(self.channel.sent), 1, 'resize 控制消息不该被当成键盘输入送进 pty')
        await self.close_terminal(communicator)

    async def test_resize_is_clamped_before_reaching_the_channel(self):
        """尺寸来自浏览器，属于外部输入：离谱的值必须收敛后再发给远端。"""
        communicator = await self.opened()

        await communicator.send_to(text_data=json.dumps({'type': 'resize', 'cols': -5, 'rows': 99999}))
        await self.wait_for(
            lambda: self.channel.resizes == [(ssh_session.MIN_COLS, ssh_session.MAX_ROWS)],
            message='尺寸没有收敛就发给了远端',
        )

        await self.close_terminal(communicator)

    async def test_resize_audit_is_throttled(self):
        """拖窗口一秒能发几十条尺寸，审计不能被它刷满。"""
        communicator = await self.opened()

        for cols in (90, 91, 92):
            await communicator.send_to(text_data=json.dumps({'type': 'resize', 'cols': cols, 'rows': 24}))
        await self.wait_for(lambda: len(self.channel.resizes) == 3, message='尺寸没有全部转发给远端')

        resizes = await self.query(lambda: AuditLog.objects.filter(action='Machine.ssh.resize').count())
        self.assertEqual(resizes, 1)
        self.assertEqual(self.channel.resizes[-1], (92, 24))
        await self.close_terminal(communicator)

    async def test_output_is_forwarded_without_breaking_multibyte_characters(self):
        """远端输出原样转发；一个中文字符横跨两次读取也不能在终端里吐出替换字符。"""
        await self.grant_async('page.machine.servers.ssh')
        self.use_password_login()
        text = '中文输出\r\n'.encode('utf-8')
        # 把一个多字节字符劈成两半喂进来，再让远端退出。
        self.channel.chunks = [text[:2], text[2:]]
        self.channel.eof = True

        communicator = await self.open_terminal()
        await self.read_control(communicator)
        output = await self.read_text(communicator)
        notice = await self.read_text(communicator)
        closing = await self.receive(communicator)

        self.assertEqual(output, '中文输出\r\n')
        self.assertIn('远端已关闭 SSH 会话', notice)
        self.assertEqual((closing['type'], closing['code']), ('websocket.close', 1000))
        # 远端先关了，浏览器随后也会回一个断开：记录在这一步收尾。
        await self.close_terminal(communicator)
        record = (await self.records())[0]
        self.assertEqual(record.status, 'closed')
        self.assertEqual(record.output_preview, '中文输出\r\n')
        self.assertNotIn('�', record.output_preview)
        closed = await self.query(lambda: AuditLog.objects.get(action='Machine.ssh.close'))
        self.assertIn('远端已关闭', closed.detail['reason'])

    async def test_closing_the_browser_side_finishes_the_record(self):
        """浏览器关掉窗口：pty 与 SSH 连接都要关掉，记录收尾并只留一条关闭审计。"""
        communicator = await self.opened()
        await communicator.send_to(text_data='ls /tmp\r')
        await self.wait_for(lambda: self.channel.sent, message='输入没有送到 pty')

        await self.close_terminal(communicator)

        self.assertTrue(self.channel.closed)
        self.assertTrue(self.ssh_client.closed)
        record = (await self.records())[0]
        self.assertEqual(record.status, 'closed')
        self.assertIsNotNone(record.ended_at)
        self.assertEqual(record.command_count, 1)
        closed = await self.query(lambda: AuditLog.objects.get(action='Machine.ssh.close'))
        self.assertEqual(closed.detail['reason'], '连接已断开')
        self.assertEqual(closed.detail['command_count'], 1)


class SshSessionHelperTests(SimpleTestCase):
    """纯函数：命令识别、尺寸收敛、凭据选择（不碰数据库，也不建连）。"""

    def test_command_tracker_collects_submitted_commands(self):
        """回车提交一条命令，之后缓冲清空。"""
        tracker = ssh_session.CommandTracker()

        commands = tracker.feed('uptime\r')

        self.assertEqual(commands, ['uptime'])
        self.assertEqual(tracker.text(), '')

    def test_command_tracker_keeps_unsubmitted_characters(self):
        """没回车的输入不算命令，跨批次的字符要拼起来。"""
        tracker = ssh_session.CommandTracker()

        self.assertEqual(tracker.feed('ls -l'), [])
        # 前后空白去掉，中间的空格保留。
        self.assertEqual(tracker.feed('  /var/log\r'), ['ls -l  /var/log'])

    def test_command_tracker_handles_backspace_and_ctrl_c(self):
        """退格删一个字符，Ctrl-C 丢掉当前这一行。"""
        tracker = ssh_session.CommandTracker()

        self.assertEqual(tracker.feed('lss\x08\r'), ['ls'])
        self.assertEqual(tracker.feed('rm -rf /\x03\r'), [])
        self.assertEqual(tracker.feed('\r'), [])

    def test_command_tracker_drops_arrow_key_sequences(self):
        """方向键这类转义序列整段丢弃：按「↑ + 回车」重跑命令不该记成一条 `[A`。"""
        tracker = ssh_session.CommandTracker()

        self.assertEqual(tracker.feed('\x1b[A\r'), [])
        self.assertEqual(tracker.feed('ls\x1b[D\x1b[D\r'), ['ls'])
        self.assertEqual(tracker.text(), '')

    def test_command_tracker_ignores_non_ascii(self):
        """粘贴进去的中文不进缓冲 —— 命令行里的中文多半是粘贴文本，记成命令只会污染审计。"""
        tracker = ssh_session.CommandTracker()

        self.assertEqual(tracker.feed('echo 中文\r'), ['echo'])

    def test_normalize_size_clamps_and_falls_back(self):
        """尺寸收敛：区间内原样保留，越界截断，非法值退回默认。"""
        self.assertEqual(ssh_session.normalize_size(132, 43), (132, 43))
        self.assertEqual(ssh_session.normalize_size('-5', 'abc'), (ssh_session.MIN_COLS, ssh_session.DEFAULT_ROWS))
        self.assertEqual(ssh_session.normalize_size(99999, 99999), (ssh_session.MAX_COLS, ssh_session.MAX_ROWS))

    def test_resolve_credentials_prefers_the_stored_password(self):
        """有托管密码就用密码，密钥存不存在都不影响这个结论。"""
        machine = Machine(
            name='cred-node', ip_address='10.9.0.12', ssh_port=22, ssh_user='root',
        )
        machine.set_password('S3cret-Pa55')

        mode, password = machine_ssh.resolve_credentials(machine)

        self.assertEqual(mode, machine_ssh.MODE_PASSWORD)
        self.assertEqual(password, 'S3cret-Pa55')

    def test_resolve_credentials_falls_back_to_the_key(self):
        """没有密码、但部署密钥在位时走密钥通道，且不返回任何密码。"""
        machine = Machine(name='cred-node', ip_address='10.9.0.12', ssh_port=22, ssh_user='root')

        with patch.object(machine_ssh, 'key_available', return_value=True):
            mode, password = machine_ssh.resolve_credentials(machine)

        self.assertEqual(mode, machine_ssh.MODE_KEY)
        self.assertEqual(password, '')

    def test_resolve_credentials_without_anything_is_a_clear_error(self):
        """两样都没有时抛出可展示的中文错误，而不是让调用方去猜。"""
        machine = Machine(name='cred-node', ip_address='10.9.0.12', ssh_port=22, ssh_user='root')

        with patch.object(machine_ssh, 'key_available', return_value=False):
            with self.assertRaises(machine_ssh.SshTerminalError) as caught:
                machine_ssh.resolve_credentials(machine)

        self.assertIn('补一份 SSH 密码', caught.exception.message)
        self.assertEqual(caught.exception.status_code, 400)

    def test_decrypt_failure_counts_as_no_password(self):
        """密文坏了等同于没托管密码（回落密钥），不能把打开终端这件事整成 500。"""
        machine = Machine(name='cred-node', ip_address='10.9.0.12', ssh_port=22, ssh_user='root')
        machine.encrypted_password = 'not-a-cipher'

        with patch.object(Machine, 'get_password', side_effect=ValueError('密文不合法')):
            self.assertEqual(machine_ssh.stored_password(machine), '')
            with patch.object(machine_ssh, 'key_available', return_value=True):
                mode, password = machine_ssh.resolve_credentials(machine)

        self.assertEqual((mode, password), (machine_ssh.MODE_KEY, ''))

    def test_resolve_target_uses_sane_defaults(self):
        """目标解析：缺省的端口与登录用户与其它通道一致。"""
        machine = Machine(name='cred-node', ip_address='10.9.0.12', ssh_port=None, ssh_user='')

        self.assertEqual(
            machine_ssh.resolve_target(machine),
            {'host': '10.9.0.12', 'port': 22, 'user': 'root'},
        )

    def test_open_client_routes_by_credential(self):
        """有密码走密码通道，没密码走部署密钥通道 —— 两条通道各自被调用一次。"""
        with patch.object(credentials, 'ssh_client') as password_login:
            machine_ssh.open_client(host='10.9.0.12', port=22, user='root', password='S3cret-Pa55')
        password_login.assert_called_once()

        with patch.object(node_registry, 'open_session') as key_login:
            machine_ssh.open_client(host='10.9.0.12', port=22, user='root')
        key_login.assert_called_once()
