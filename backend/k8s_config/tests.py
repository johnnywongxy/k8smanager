from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ops.models import Organization, PermissionPolicy, PermissionRule, UserProfile
from ops.permission_service import clear_permission_cache
from platform_logs.models import AuditLog

from .models import K8sVersionMapping, SupportedOperatingSystem


class K8sConfigPermissionTests(TestCase):
    """验证配置中心按数据库权限策略校验菜单、页面和按钮权限。"""

    def setUp(self):
        """创建普通用户及默认拒绝的权限策略，并准备一条版本配置。"""
        organization = Organization.objects.first()
        self.user = User.objects.create_user(username='config-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=organization, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='配置中心权限',
            subject_type='user',
            user=self.user,
            status='available',
        )
        self.version = K8sVersionMapping.objects.create(
            kubernetes_version='1.99',
            kubeasz_version='9.9.9',
        )
        self.system = SupportedOperatingSystem.objects.create(os_type='Test OS', version='1.0')
        self.version.supported_systems.add(self.system)
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

    def grant_view(self):
        """授予配置中心菜单和页面查看权限，作为按钮权限用例的前置条件。"""
        self.grant('menu.k8s')
        self.grant('page.k8s.config.view')

    def test_config_list_requires_group_and_page_permissions(self):
        """普通用户必须同时具备 K8s管理菜单和配置中心页面权限。"""
        self.revoke_all()
        self.assertEqual(self.client.get('/api/v1/k8s/config/versions/').status_code, 403)
        self.grant('menu.k8s')
        self.assertEqual(self.client.get('/api/v1/k8s/config/versions/').status_code, 403)
        self.grant('page.k8s.config.view')
        allowed = self.client.get('/api/v1/k8s/config/versions/')
        self.assertEqual(allowed.status_code, 200)
        self.assertGreaterEqual(allowed.data['count'], 1)

    def test_config_create_requires_create_action(self):
        """新增配置需要额外的按钮权限，否则返回 403。"""
        self.revoke_all()
        self.grant_view()
        payload = {'kubernetes_version': '1.98', 'kubeasz_version': '9.8.0'}
        self.assertEqual(self.client.post('/api/v1/k8s/config/versions/', payload).status_code, 403)
        self.grant('page.k8s.config.create')
        created = self.client.post('/api/v1/k8s/config/versions/', payload)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(K8sVersionMapping.objects.filter(kubernetes_version='1.98').exists())
        self.assertTrue(AuditLog.objects.filter(action='K8sConfig.create').exists())

    def test_config_update_requires_update_action(self):
        """编辑配置需要额外的按钮权限，否则返回 403。"""
        self.revoke_all()
        self.grant_view()
        url = f'/api/v1/k8s/config/versions/{self.version.id}/'
        payload = {'kubernetes_version': '1.99', 'kubeasz_version': '9.9.10'}
        self.assertEqual(self.client.put(url, payload).status_code, 403)
        self.grant('page.k8s.config.update')
        self.assertEqual(self.client.put(url, payload).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action='K8sConfig.update').exists())

    def test_config_delete_requires_delete_action(self):
        """删除配置需要额外的按钮权限，否则返回 403。"""
        self.revoke_all()
        self.grant_view()
        url = f'/api/v1/k8s/config/versions/{self.version.id}/'
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.grant('page.k8s.config.delete')
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(K8sVersionMapping.objects.filter(id=self.version.id).exists())
        self.assertTrue(AuditLog.objects.filter(action='K8sConfig.delete').exists())

    def test_system_list_is_covered_by_the_same_page_permission(self):
        """操作系统清单与版本对应关系共用同一个页面权限。"""
        self.revoke_all()
        self.grant_view()
        response = self.client.get('/api/v1/k8s/config/systems/')
        self.assertEqual(response.status_code, 200)

    def test_deploy_options_only_requires_deploy_page_permission(self):
        """部署向导选项接口只要求部署页面权限，无需配置中心编辑权限。"""
        self.revoke_all()
        self.assertEqual(self.client.get('/api/v1/k8s/deploy-options/').status_code, 403)
        self.grant('menu.k8s')
        self.grant('page.k8s.deploy.view')
        response = self.client.get('/api/v1/k8s/deploy-options/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(item['kubernetes_version'] == '1.99' for item in response.data['versions']))
        version = next(item for item in response.data['versions'] if item['kubernetes_version'] == '1.99')
        self.assertEqual(version['supported_system_ids'], [self.system.id])
        system = next(item for item in response.data['systems'] if item['os_type'] == 'Test OS')
        self.assertEqual(system['version'], '1.0')
        self.assertEqual(system['name'], 'Test OS 1.0')

    def test_same_os_type_accepts_multiple_versions(self):
        """同一操作系统类型下可以维护多个版本，但类型与版本组合不能重复。"""
        self.revoke_all()
        self.grant_view()
        self.grant('page.k8s.config.create')
        self.grant('page.k8s.config.update')
        first = self.client.post('/api/v1/k8s/config/systems/', {'os_type': 'Test OS', 'version': '2.0'})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data['name'], 'Test OS 2.0')
        second = self.client.post('/api/v1/k8s/config/systems/', {'os_type': 'Test OS', 'version': '3.0'})
        self.assertEqual(second.status_code, 201)
        duplicated = self.client.post('/api/v1/k8s/config/systems/', {'os_type': 'Test OS', 'version': '2.0'})
        self.assertEqual(duplicated.status_code, 400)
        conflict = self.client.patch(
            f"/api/v1/k8s/config/systems/{second.data['id']}/",
            {'version': '2.0'},
        )
        self.assertEqual(conflict.status_code, 400)

    def test_deploy_options_skips_inactive_entries(self):
        """停用的配置不会出现在部署向导的候选项中。"""
        self.revoke_all()
        self.grant('menu.k8s')
        self.grant('page.k8s.deploy.view')
        self.version.is_active = False
        self.version.save(update_fields=['is_active'])
        response = self.client.get('/api/v1/k8s/deploy-options/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(item['kubernetes_version'] == '1.99' for item in response.data['versions']))
