import json
import os
import unittest

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from data_security.sm4 import SM4Error, crypt_block, decrypt, encrypt
from ops.models import Organization, SystemSetting, UserProfile
from ops.serializers import SystemSettingSerializer
from ops.services.instance_identity import get_instance_id
from ops.test_license_support import signed_test_license, trusted_test_key


@override_settings(SM4_MASTER_KEYS=['unit-test-sm4-master-key'])
class SM4AlgorithmTests(SimpleTestCase):
    def test_standard_block_cipher_vector(self):
        key = bytes.fromhex('0123456789abcdeffedcba9876543210')
        plaintext = bytes.fromhex('0123456789abcdeffedcba9876543210')
        ciphertext = crypt_block(plaintext, key)
        self.assertEqual(ciphertext.hex(), '681edf34d206965e86b3e94f536e4246')
        self.assertEqual(crypt_block(ciphertext, key, decrypt=True), plaintext)

    def test_authenticated_text_roundtrip_and_tamper_detection(self):
        ciphertext = encrypt('敏感数据-13800138000')
        self.assertTrue(ciphertext.startswith('sm4:v1:'))
        self.assertNotIn('13800138000', ciphertext)
        self.assertEqual(decrypt(ciphertext), '敏感数据-13800138000')
        replacement = 'A' if ciphertext[-1] != 'A' else 'B'
        with self.assertRaises(SM4Error):
            decrypt(ciphertext[:-1] + replacement)

    def test_ciphertext_written_with_legacy_master_key_still_decrypts(self):
        """旧主密钥写下的字段仍能解开：解密按 SM4_MASTER_KEYS 顺序逐个试。

        平台一度在没有 DJANGO_SECRET_KEY 的情况下运行，那批字段（手机号、对象存储密钥
        等）就是用旧默认值加密落库的。settings 把旧值挂在列表末尾**只用于解密**，
        这条用例把「末尾那个位置还有人试」钉住 —— 一旦有人顺手把它换成当前密钥的副本，
        历史数据会变成不可解密的乱码，而不是报错。
        """
        with override_settings(SM4_MASTER_KEYS=['legacy-master-key']):
            legacy_ciphertext = encrypt('历史数据-13800138000')
        self.assertTrue(legacy_ciphertext.startswith('sm4:v1:'))

        with override_settings(SM4_MASTER_KEYS=['current-master-key', 'legacy-master-key']):
            self.assertEqual(decrypt(legacy_ciphertext), '历史数据-13800138000')
            # 加密只用第一个：新数据不该能被旧密钥单独解开，否则回退链就变成了双向的。
            fresh_ciphertext = encrypt('新数据')
        with override_settings(SM4_MASTER_KEYS=['legacy-master-key']):
            with self.assertRaises(SM4Error):
                decrypt(fresh_ciphertext)


@unittest.skipIf(os.getenv('SM4_MASTER_KEYS'), '环境变量显式覆盖了主密钥，不验默认组合')
class SM4MasterKeyDefaultsTest(SimpleTestCase):
    """默认配置下主密钥列表的组成：当前密钥在前用于加密，旧默认值在后用于解密。"""

    def test_current_key_first_and_legacy_key_last(self):
        self.assertEqual(settings.SM4_MASTER_KEYS[0], settings.SECRET_KEY)
        self.assertIn(settings.LEGACY_SECRET_KEY, settings.SM4_MASTER_KEYS)
        self.assertNotEqual(settings.SM4_MASTER_KEYS[0], settings.LEGACY_SECRET_KEY)

    def test_sms_lookup_key_is_pinned_to_legacy_default(self):
        """手机号查询索引不跟随 SECRET_KEY：换值等于让已绑定的手机号全部查不出来。"""
        self.assertEqual(settings.SMS_SECURITY_KEY, settings.LEGACY_SECRET_KEY)


@override_settings(SM4_MASTER_KEYS=['unit-test-sm4-master-key'])
class EncryptedModelFieldTests(TestCase):
    def setUp(self):
        """临时安装只用于测试的 SM2 公钥。"""
        self.trusted_key = trusted_test_key()
        self.trusted_key.start()

    def tearDown(self):
        """恢复后端生产可信公钥表。"""
        self.trusted_key.stop()

    def test_phone_is_encrypted_at_rest_and_decrypted_for_application(self):
        organization = Organization.objects.create(name='SM4测试公司', slug='sm4-test', is_default=True)
        user = User.objects.create_user(username='sm4-user', password='StrongPassword123!')
        profile = UserProfile.objects.create(user=user, organization=organization, phone='13800138000')

        with connection.cursor() as cursor:
            cursor.execute('SELECT phone FROM ops_userprofile WHERE id = %s', [profile.id])
            stored = cursor.fetchone()[0]
        self.assertTrue(stored.startswith('sm4:v1:'))
        self.assertNotIn('13800138000', stored)

        profile.refresh_from_db()
        self.assertEqual(profile.phone, '13800138000')

    def test_sensitive_system_setting_values_are_encrypted_at_rest(self):
        setting = SystemSetting.objects.create(
            key='sm4.test.setting',
            value={
                'access_key_id': 'public-id',
                'access_key_secret': 'private-secret',
                'max_output_tokens': 4096,
                'nested': {'recording_sftp_password': 'sftp-password'},
            },
        )

        with connection.cursor() as cursor:
            cursor.execute('SELECT value FROM ops_systemsetting WHERE id = %s', [setting.id])
            stored = json.loads(cursor.fetchone()[0])
        self.assertEqual(stored['access_key_id'], 'public-id')
        self.assertEqual(stored['max_output_tokens'], 4096)
        self.assertTrue(stored['access_key_secret'].startswith('sm4:v1:'))
        self.assertTrue(stored['nested']['recording_sftp_password'].startswith('sm4:v1:'))
        self.assertNotIn('private-secret', json.dumps(stored))

        setting.refresh_from_db()
        self.assertEqual(setting.value['max_output_tokens'], 4096)
        self.assertEqual(setting.value['access_key_secret'], 'private-secret')
        self.assertEqual(setting.value['nested']['recording_sftp_password'], 'sftp-password')

    def test_notification_credentials_are_encrypted_masked_and_preserved(self):
        """通知凭据必须使用 SM4 落库，接口掩码更新不得覆盖原秘密。"""
        setting = SystemSetting.objects.get(key='notification.delivery')
        serializer = SystemSettingSerializer(setting, data={'value': {
            'email': {
                'enabled': True,
                'smtp_host': 'smtp.example.com',
                'smtp_port': 465,
                'security': 'ssl',
                'sender_email': 'notice@example.com',
                'username': 'notice@example.com',
                'password': 'smtp-private-password',
            },
            'sms': {
                'enabled': True,
                'provider': 'aliyun',
                'access_key_id': 'LTAI-public-id',
                'access_key_secret': 'aliyun-private-secret',
                'sign_name': '泰出科技',
                'template_code': 'SMS_123456789',
            },
        }}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        setting = serializer.save()
        with connection.cursor() as cursor:
            cursor.execute('SELECT value FROM ops_systemsetting WHERE id = %s', [setting.id])
            stored = json.loads(cursor.fetchone()[0])
        represented = SystemSettingSerializer(setting).data['value']

        self.assertTrue(stored['email']['password'].startswith('sm4:v1:'))
        self.assertTrue(stored['sms']['access_key_secret'].startswith('sm4:v1:'))
        self.assertEqual(setting.value['email']['password'], 'smtp-private-password')
        self.assertEqual(setting.value['sms']['access_key_secret'], 'aliyun-private-secret')
        self.assertEqual(represented['email']['password'], '********')
        self.assertEqual(represented['sms']['access_key_secret'], '********')

        update = SystemSettingSerializer(setting, data={'value': {
            **represented,
            'email': {**represented['email'], 'sender_email': 'ops@example.com', 'password': ''},
            'sms': {**represented['sms'], 'access_key_secret': '********'},
        }}, partial=True)
        self.assertTrue(update.is_valid(), update.errors)
        updated = update.save()
        self.assertEqual(updated.value['email']['sender_email'], 'ops@example.com')
        self.assertEqual(updated.value['email']['password'], 'smtp-private-password')
        self.assertEqual(updated.value['sms']['access_key_secret'], 'aliyun-private-secret')

    def test_notification_settings_reject_invalid_or_incomplete_channels(self):
        """通知配置必须拒绝未知短信渠道和缺少凭据的启用状态。"""
        setting = SystemSetting.objects.get(key='notification.delivery')
        invalid_provider = SystemSettingSerializer(setting, data={'value': {
            'email': {'enabled': False},
            'sms': {'enabled': False, 'provider': 'unknown'},
        }}, partial=True)
        incomplete_email = SystemSettingSerializer(setting, data={'value': {
            'email': {'enabled': True, 'sender_email': 'notice@example.com'},
            'sms': {'enabled': False, 'provider': 'aliyun'},
        }}, partial=True)
        incomplete_sms = SystemSettingSerializer(setting, data={'value': {
            'email': {'enabled': False},
            'sms': {'enabled': True, 'provider': 'aliyun', 'access_key_id': 'LTAI-public-id'},
        }}, partial=True)

        self.assertFalse(invalid_provider.is_valid())
        self.assertIn('平台支持的短信服务商', str(invalid_provider.errors))
        self.assertFalse(incomplete_email.is_valid())
        self.assertIn('SMTP 主机', str(incomplete_email.errors))
        self.assertFalse(incomplete_sms.is_valid())
        self.assertIn('AccessKey Secret', str(incomplete_sms.errors))

    def test_multi_provider_sms_secrets_are_encrypted_masked_and_preserved(self):
        """新增短信服务商的密钥和令牌必须沿用通知配置的 SM4 保护。"""
        setting = SystemSetting.objects.get(key='notification.delivery')
        serializer = SystemSettingSerializer(setting, data={'value': {
            'email': {'enabled': False},
            'sms': {
                'enabled': True,
                'provider': 'netease',
                'app_key': 'netease-app-key',
                'app_secret': 'netease-app-secret',
                'template_code': '9876543',
                'secret_id': 'retained-secret-id',
                'secret_key': 'retained-secret-key',
                'authorization_token': 'retained-upyun-token',
                'api_key': 'retained-yunpian-key',
            },
        }}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        setting = serializer.save()
        with connection.cursor() as cursor:
            cursor.execute('SELECT value FROM ops_systemsetting WHERE id = %s', [setting.id])
            stored = json.loads(cursor.fetchone()[0])['sms']
        represented = SystemSettingSerializer(setting).data['value']['sms']

        for field in ('app_secret', 'secret_id', 'secret_key', 'authorization_token', 'api_key'):
            self.assertTrue(stored[field].startswith('sm4:v1:'))
            self.assertEqual(represented[field], '********')

        update = SystemSettingSerializer(setting, data={'value': {
            'email': {'enabled': False},
            'sms': {**represented, 'app_secret': '********'},
        }}, partial=True)
        self.assertTrue(update.is_valid(), update.errors)
        updated = update.save()
        self.assertEqual(updated.value['sms']['app_secret'], 'netease-app-secret')

    def test_nested_sensitive_values_are_masked_and_preserved_on_update(self):
        setting = SystemSetting.objects.create(
            key='sm4.masked.setting',
            value={
                'max_output_tokens': 4096,
                'providers': [{'name': 'primary', 'api_key': 'provider-secret'}],
            },
        )
        represented = SystemSettingSerializer(setting).data
        self.assertEqual(represented['value']['max_output_tokens'], 4096)
        self.assertEqual(represented['value']['providers'][0]['api_key'], '********')

        serializer = SystemSettingSerializer(
            setting,
            data={
                'value': {
                    'max_output_tokens': 8192,
                    'providers': [{'name': 'primary', 'api_key': '********'}],
                },
            },
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        self.assertEqual(updated.value['max_output_tokens'], 8192)
        self.assertEqual(updated.value['providers'][0]['api_key'], 'provider-secret')

    def test_license_content_is_encrypted_at_rest_and_only_metadata_is_exposed(self):
        """Licence 原文必须使用 SM4 落库，接口序列化只允许返回掩码和授权元数据。"""
        content = signed_test_license(instance_id=get_instance_id())
        setting = SystemSetting.objects.get(key='license.management')
        serializer = SystemSettingSerializer(
            setting,
            data={'value': {'license_secret': content}},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        setting = serializer.save()
        with connection.cursor() as cursor:
            cursor.execute('SELECT value FROM ops_systemsetting WHERE id = %s', [setting.id])
            stored = json.loads(cursor.fetchone()[0])
        represented = SystemSettingSerializer(setting).data['value']

        self.assertTrue(stored['license_secret'].startswith('sm4:v1:'))
        self.assertNotIn('TEST0001', stored['license_secret'])
        self.assertEqual(setting.value['license_secret'], content)
        self.assertEqual(represented['license_secret'], '********')
        self.assertEqual(represented['status'], 'valid')
        self.assertEqual(represented['customer'], '测试客户')

    def test_invalid_or_oversized_license_is_rejected(self):
        """Licence 非 JSON 或超过 64 KB 时不得进入数据库。"""
        setting = SystemSetting.objects.get(key='license.management')
        invalid = SystemSettingSerializer(
            setting,
            data={'value': {'license_secret': 'not-json'}},
            partial=True,
        )
        oversized = SystemSettingSerializer(
            setting,
            data={'value': {'license_secret': 'x' * (64 * 1024 + 1)}},
            partial=True,
        )

        self.assertFalse(invalid.is_valid())
        self.assertFalse(oversized.is_valid())
        self.assertIn('SM2 三段式签名格式', str(invalid.errors))
        self.assertIn('64 KB', str(oversized.errors))


@override_settings(SM4_MASTER_KEYS=['unit-test-sm4-master-key'])
class LocalAssetPolicyTests(TestCase):
    def test_platform_filing_rejects_unsafe_link_scheme(self):
        """备案配置必须拒绝可执行脚本协议，避免登录页出现不安全跳转。"""
        setting = SystemSetting.objects.get(key='platform.filing')
        serializer = SystemSettingSerializer(setting, data={
            'key': 'platform.filing',
            'value': {
                'icp_record': '冀ICP备2026010543号-3',
                'icp_url': 'javascript:alert(1)',
                'public_security_record': '',
                'public_security_url': '',
            },
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn('http 或 https', str(serializer.errors))

    def test_platform_logo_accepts_local_asset_path(self):
        setting = SystemSetting.objects.get(key='platform')
        serializer = SystemSettingSerializer(setting, data={
            'key': 'platform',
            'value': {'name': '本地品牌', 'logo_url': '/assets/brand/logo.png'},
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['value']['logo_url'], '/assets/brand/logo.png')

    def test_platform_logo_rejects_remote_asset_url(self):
        setting = SystemSetting.objects.get(key='platform')
        serializer = SystemSettingSerializer(setting, data={
            'key': 'platform',
            'value': {'name': '外网品牌', 'logo_url': 'https://example.com/logo.png'},
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn('禁止引用外网地址', str(serializer.errors))

    def test_public_platform_hides_legacy_remote_logo(self):
        setting = SystemSetting.objects.get(key='platform')
        setting.value = {'name': '历史品牌', 'logo_url': 'https://example.com/legacy-logo.png'}
        setting.save(update_fields=['value', 'updated_at'])

        response = APIClient().get('/api/v1/public/platform')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['name'], '历史品牌')
        self.assertEqual(response.json()['logo_url'], '')
