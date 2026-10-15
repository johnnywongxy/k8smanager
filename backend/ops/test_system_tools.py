from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from platform_logs.models import AuditLog

from ops import serializers
from ops.models import Organization, PermissionPolicy, PermissionRule, UserProfile
from ops.permission_service import clear_permission_cache
from ops.services.system_tools import (
    normalize_target,
    run_curl,
    run_mtr,
    run_ping,
    run_ssh_password_auth,
    stream_traceroute,
)
from ops.views import (
    SystemToolCurlView,
    SystemToolMtrView,
    SystemToolPingView,
    SystemToolTelnetView,
    SystemToolTracerouteView,
)


class SystemToolServiceTests(TestCase):
    """验证平台网络工具拒绝命令注入，并限制外部数据读取范围。"""

    def test_target_validation_rejects_command_injection_characters(self):
        """主机名只允许 DNS 标签或 IP 地址。"""
        for target in ['127.0.0.1 && whoami', '-n', 'host;reboot', 'host name']:
            with self.assertRaises(ValueError):
                normalize_target(target)

    @patch('ops.services.system_tools.subprocess.run')
    @patch('ops.services.system_tools.resolve_target')
    def test_ping_uses_argument_array_without_shell(self, mocked_resolve, mocked_run):
        """Ping 必须以参数数组执行并显式关闭 shell。"""
        mocked_resolve.return_value = [(2, 2, 17, ('127.0.0.1', 0), '127.0.0.1')]
        mocked_run.return_value.returncode = 0
        mocked_run.return_value.stdout = b'reply'
        mocked_run.return_value.stderr = b''

        result = run_ping('127.0.0.1', count=2, timeout_seconds=1)

        self.assertTrue(result['success'])
        command = mocked_run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[-1], '127.0.0.1')
        self.assertFalse(mocked_run.call_args.kwargs['shell'])

    @patch('ops.services.system_tools.subprocess.Popen')
    @patch('ops.services.system_tools.resolve_target')
    def test_traceroute_streams_lines_and_never_uses_shell(self, mocked_resolve, mocked_popen):
        """Traceroute 必须逐行返回内容并以参数数组关闭 shell。"""
        mocked_resolve.return_value = [(2, 2, 17, ('127.0.0.1', 0), '127.0.0.1')]
        process = mocked_popen.return_value
        process.stdout.readline.side_effect = [b'first hop\n', b'second hop\n', b'']
        process.wait.return_value = 0
        process.poll.return_value = 0

        events = list(stream_traceroute('127.0.0.1', max_hops=30, timeout_seconds=1))

        self.assertEqual([event for event, _payload in events], ['start', 'output', 'output', 'done'])
        self.assertTrue(events[-1][1]['success'])
        self.assertIn('first hop', events[-1][1]['output'])
        command = mocked_popen.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertIn('30', command)
        self.assertEqual(command[-1], '127.0.0.1')
        self.assertFalse(mocked_popen.call_args.kwargs['shell'])

    def test_curl_rejects_unsafe_schemes_and_credentials(self):
        """Curl 必须拒绝非 HTTP 协议以及 URL 内嵌凭据。"""
        for url in ('file:///etc/passwd', 'https://admin:secret@example.com/'):
            with self.assertRaises(ValueError):
                run_curl(url)

    @patch('ops.services.system_tools.requests.Session')
    def test_curl_streams_limited_preview_without_file_writes(self, mocked_session_class):
        """Curl 只读取受限内存预览，不跟随重定向也不调用文件接口。"""
        response = MagicMock()
        response.status_code = 200
        response.reason = 'OK'
        response.ok = True
        response.headers = {'Content-Type': 'text/plain'}
        response.encoding = 'utf-8'
        response.iter_content.return_value = [b'a' * (64 * 1024 + 1)]
        mocked_session_class.return_value.request.return_value = response

        with patch('builtins.open') as mocked_open:
            result = run_curl('https://example.com/download?token=sensitive')

        self.assertTrue(result['success'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['bytes_read'], 64 * 1024)
        self.assertNotIn('token=', result['target'])
        mocked_open.assert_not_called()
        request_kwargs = mocked_session_class.return_value.request.call_args.kwargs
        self.assertTrue(request_kwargs['stream'])
        self.assertFalse(request_kwargs['allow_redirects'])

    @patch('ops.services.system_tools.icmp_traceroute')
    @patch('ops.services.system_tools.resolve_target')
    def test_mtr_maps_per_hop_quality_statistics(self, mocked_resolve, mocked_traceroute):
        """MTR 必须限制参数并返回每跳丢包和延迟统计。"""
        mocked_resolve.return_value = [(2, 2, 17, ('127.0.0.1', 0), '127.0.0.1')]
        mocked_traceroute.return_value = [SimpleNamespace(
            distance=2, address='127.0.0.1', packet_loss=0.25,
            packets_sent=4, packets_received=3, min_rtt=1.1, avg_rtt=2.2, max_rtt=3.3,
        )]

        result = run_mtr('127.0.0.1', count=10, max_hops=30, timeout_seconds=5)

        self.assertTrue(result['success'])
        self.assertEqual(result['hops'][0]['packet_loss'], 100.0)
        self.assertEqual(result['hops'][1]['packet_loss'], 25.0)
        self.assertIn('25.0%', result['output'])
        self.assertEqual(mocked_traceroute.call_args.kwargs['count'], 10)


class SystemToolApiTests(TestCase):
    """验证平台工具的数据库权限、频率限制和安全系统日志。"""

    def setUp(self):
        """创建普通用户及只允许 Ping 的数据库权限策略。"""
        cache.clear()
        self.organization = Organization.objects.create(name='平台工具测试部门')
        self.user = User.objects.create_user(username='network-operator', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=self.organization, role='member')
        self.policy = PermissionPolicy.objects.create(
            organization=self.organization,
            name='平台工具权限',
            subject_type='user',
            user=self.user,
        )
        PermissionRule.objects.bulk_create([
            PermissionRule(policy=self.policy, permission_code='menu.platform', effect='allow'),
            PermissionRule(policy=self.policy, permission_code='page.system_tools.view', effect='allow'),
            PermissionRule(policy=self.policy, permission_code='page.system_tools.ping', effect='allow'),
            PermissionRule(policy=self.policy, permission_code='page.system_tools.telnet', effect='deny'),
            PermissionRule(policy=self.policy, permission_code='page.system_tools.curl', effect='deny'),
            PermissionRule(policy=self.policy, permission_code='page.system_tools.traceroute', effect='deny'),
            PermissionRule(policy=self.policy, permission_code='page.system_tools.mtr', effect='deny'),
        ])
        clear_permission_cache()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_views_declare_the_system_tools_page_permission(self):
        """五个执行接口都必须归属平台工具页面权限。"""
        self.assertEqual(SystemToolPingView.permission_code, 'page.system_tools.view')
        self.assertEqual(SystemToolTelnetView.permission_code, 'page.system_tools.view')
        self.assertEqual(SystemToolCurlView.permission_code, 'page.system_tools.view')
        self.assertEqual(SystemToolTracerouteView.permission_code, 'page.system_tools.view')
        self.assertEqual(SystemToolMtrView.permission_code, 'page.system_tools.view')

    def test_policy_save_expands_action_to_parent_permission_chain(self):
        """保存单个工具动作时自动补齐菜单和页面权限，其他工具保持拒绝。"""
        serializer = serializers.PermissionPolicySerializer(
            self.policy,
            data={
                'rules': [{
                    'permission_code': 'page.system_tools.ping',
                    'effect': 'allow',
                }],
            },
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        effects = dict(self.policy.rules.values_list('permission_code', 'effect'))
        self.assertEqual(effects['menu.platform'], 'allow')
        self.assertEqual(effects['page.system_tools.view'], 'allow')
        self.assertEqual(effects['page.system_tools.ping'], 'allow')
        self.assertEqual(effects['page.system_tools.curl'], 'deny')

    @patch('ops.views.run_ping')
    def test_ping_requires_action_permission_and_writes_audit(self, mocked_ping):
        """允许 Ping 后返回结果，并写入不含命令输出的系统日志。"""
        mocked_ping.return_value = {
            'tool': 'ping', 'target': 'example.com', 'resolved_address': '93.184.216.34',
            'success': True, 'duration_ms': 21, 'detail': '目标主机响应正常', 'output': 'reply',
        }
        response = self.client.post('/api/v1/system-tools/ping/', {
            'target': 'example.com', 'count': 2, 'timeout_seconds': 1,
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        log = AuditLog.objects.filter(action='SystemTool.ping').latest('created_at')
        self.assertEqual(log.resource, '平台工具')
        self.assertEqual(log.detail['result'], 'success')
        self.assertNotIn('output', log.detail)

    def test_denied_actions_cannot_bypass_independent_permissions(self):
        """没有独立操作权限时不能直接调用其诊断接口。"""
        requests = (
            ('/api/v1/system-tools/telnet/', {'target': '127.0.0.1', 'port': 22}),
            ('/api/v1/system-tools/curl/', {'target': 'https://example.com/', 'method': 'HEAD'}),
            ('/api/v1/system-tools/traceroute/', {'target': '127.0.0.1', 'max_hops': 10}),
            ('/api/v1/system-tools/mtr/', {'target': '127.0.0.1', 'count': 2}),
        )
        for path, payload in requests:
            with self.subTest(path=path):
                response = self.client.post(path, payload, format='json')
                self.assertEqual(response.status_code, 403)

    @patch('ops.views.stream_traceroute')
    def test_traceroute_returns_sse_and_audits_after_stream_finishes(self, mocked_stream):
        """允许 Traceroute 后必须返回事件流，并在消费完成后写入系统日志。"""
        PermissionRule.objects.filter(
            policy=self.policy, permission_code='page.system_tools.traceroute',
        ).update(effect='allow')
        clear_permission_cache()
        mocked_stream.return_value = iter([
            ('start', {
                'tool': 'traceroute', 'target': '127.0.0.1', 'resolved_address': '127.0.0.1',
                'success': False, 'duration_ms': 0, 'detail': '正在追踪路由', 'output': '',
            }),
            ('output', {'text': '1 127.0.0.1\n'}),
            ('done', {
                'tool': 'traceroute', 'target': '127.0.0.1', 'resolved_address': '127.0.0.1',
                'success': True, 'duration_ms': 12, 'detail': '路由追踪已完成',
                'output': '1 127.0.0.1',
            }),
        ])

        response = self.client.post('/api/v1/system-tools/traceroute/', {
            'target': '127.0.0.1', 'max_hops': 10, 'timeout_seconds': 1,
        }, format='json')
        content = b''.join(response.streaming_content).decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('event: output', content)
        self.assertIn('event: done', content)
        log = AuditLog.objects.filter(action='SystemTool.traceroute').latest('created_at')
        self.assertEqual(log.detail['result'], 'success')
        self.assertNotIn('output', log.detail)

    @patch('ops.views.run_ping')
    def test_repeated_diagnostics_are_throttled(self, mocked_ping):
        """同一用户连续执行诊断时第二次请求应被限流。"""
        mocked_ping.return_value = {
            'tool': 'ping', 'target': '127.0.0.1', 'resolved_address': '127.0.0.1',
            'success': True, 'duration_ms': 1, 'detail': '目标主机响应正常', 'output': '',
        }
        payload = {'target': '127.0.0.1', 'count': 1, 'timeout_seconds': 1}
        first = self.client.post('/api/v1/system-tools/ping/', payload, format='json')
        second = self.client.post('/api/v1/system-tools/ping/', payload, format='json')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)


class SshPasswordAuthTests(TestCase):
    """验证机器 SSH 密码登录校验不会误用本机凭据，也不会回显密码。"""

    def _fake_client(self, mocked_client_class):
        """构造已连接的 SSH 客户端替身并补齐主机密钥指纹。

        参数：`mocked_client_class` 表示被替换的 paramiko.SSHClient 类。
        返回：返回可断言的客户端替身。
        副作用：不建立真实连接。
        """
        client = mocked_client_class.return_value
        client.get_transport.return_value.get_remote_server_key.return_value = SimpleNamespace(
            asbytes=lambda: b'host-key-bytes',
        )
        return client

    def test_empty_password_is_rejected_before_connecting(self):
        """未托管密码时必须在建立连接前直接拒绝。"""
        with self.assertRaises(ValueError):
            run_ssh_password_auth('127.0.0.1', 22, 'root', '')

    @patch('paramiko.SSHClient')
    def test_valid_credentials_report_success_and_host_key_fingerprint(self, mocked_client_class):
        """凭据有效时返回成功，并给出服务端主机密钥指纹。"""
        client = self._fake_client(mocked_client_class)

        result = run_ssh_password_auth('127.0.0.1', 22, 'root', 'SuperSecret123', timeout_seconds=3)

        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'ssh-password')
        self.assertEqual(result['resolved_address'], '127.0.0.1')
        self.assertTrue(result['host_key_fingerprint'].startswith('SHA256:'))
        client.close.assert_called_once()

    @patch('paramiko.SSHClient')
    def test_wrong_credentials_are_reported_in_chinese(self, mocked_client_class):
        """认证被拒时给出中文说明而不是抛出异常。"""
        import paramiko

        client = self._fake_client(mocked_client_class)
        client.connect.side_effect = paramiko.AuthenticationException('Authentication failed.')

        result = run_ssh_password_auth('127.0.0.1', 22, 'root', 'wrong-password')

        self.assertFalse(result['success'])
        self.assertIn('用户名或密码不正确', result['detail'])

    @patch('paramiko.SSHClient')
    def test_password_is_never_echoed_in_result(self, mocked_client_class):
        """返回值中绝不能出现密码明文。"""
        self._fake_client(mocked_client_class)

        result = run_ssh_password_auth('127.0.0.1', 22, 'root', 'SuperSecret123')

        self.assertNotIn('SuperSecret123', str(result))

    @patch('paramiko.SSHClient')
    def test_local_keys_and_agent_are_disabled(self, mocked_client_class):
        """校验必须只用登记的密码，禁止回落到本机私钥或 ssh-agent。"""
        client = self._fake_client(mocked_client_class)

        run_ssh_password_auth('127.0.0.1', 22, 'root', 'SuperSecret123', timeout_seconds=4)

        kwargs = client.connect.call_args.kwargs
        self.assertEqual(kwargs['username'], 'root')
        self.assertEqual(kwargs['password'], 'SuperSecret123')
        self.assertFalse(kwargs['allow_agent'])
        self.assertFalse(kwargs['look_for_keys'])
        self.assertEqual(kwargs['timeout'], 4)

    @patch('paramiko.SSHClient')
    def test_unknown_host_key_is_accepted_without_persisting(self, mocked_client_class):
        """未知主机密钥只在本次连接内接受，不能写入 known_hosts。"""
        import paramiko

        client = self._fake_client(mocked_client_class)

        run_ssh_password_auth('127.0.0.1', 22, 'root', 'SuperSecret123')

        policy = client.set_missing_host_key_policy.call_args.args[0]
        self.assertIsInstance(policy, paramiko.MissingHostKeyPolicy)
        self.assertNotIsInstance(policy, paramiko.AutoAddPolicy)
