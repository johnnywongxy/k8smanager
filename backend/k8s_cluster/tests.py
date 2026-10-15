import base64
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.utils import timezone as django_timezone
from rest_framework.test import APIClient

from unittest.mock import patch
from types import SimpleNamespace

from . import crypto
from .models import K8sCluster, K8sClusterCredential
from .serializers import K8sClusterSerializer
from .services import (
    cluster_certificate_summary,
    certificate_expirations,
    normalize_kubeconfig_text,
    parse_kubeconfig_text,
    probe_cluster,
)
from k8s_audit.models import K8sOperationAudit
from k8s_machine.models import Machine
from platform_logs.models import AuditLog


def make_certificate(days, common_name='kubernetes'):
    """生成一张自签证书，返回 PEM 文本。

    参数：`days` 表示从现在起多少天后到期（负数表示已过期）；`common_name` 表示证书主体。
    返回：返回 PEM 文本。
    副作用：只做纯计算（本机生成密钥对，不联网）。

    用 EC 而不是 RSA：RSA 2048 每次生成要几百毫秒，这个用例要造好几张证书。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = django_timezone.now()
    not_after = now + timedelta(days=days)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # 已过期的证书里，签发时间也要跟着往前挪 —— cryptography 不接受「到期早于签发」。
        .not_valid_before(min(now - timedelta(days=1), not_after - timedelta(days=1)))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode('utf-8')


def kubeconfig_with(ca_pem='', client_pem='', token=''):
    """拼一份含指定证书的 kubeconfig 文本。

    参数：`ca_pem` 表示 CA 的 PEM；`client_pem` 表示客户端证书的 PEM；`token` 表示认证令牌。
    返回：返回 kubeconfig 文本。
    副作用：只做纯计算。

    ``*-data`` 字段里放的是 **base64 的 PEM**，与真实 kubeconfig 一致。
    """
    def field(value):
        """把一个 PEM 转成 kubeconfig 里的字段文本。"""
        return base64.b64encode(value.encode('utf-8')).decode('ascii') if value else ''

    user_block = f'    token: {token}\n' if token else (
        f'    client-certificate-data: {field(client_pem)}\n    client-key-data: {field(client_pem)}\n'
    )
    return (
        'apiVersion: v1\n'
        'clusters:\n'
        '- cluster:\n'
        f'    certificate-authority-data: {field(ca_pem)}\n'
        '    server: https://127.0.0.1:6443\n'
        '  name: kubernetes\n'
        'contexts:\n'
        '- context:\n    cluster: kubernetes\n    user: admin\n  name: admin@kubernetes\n'
        'current-context: admin@kubernetes\n'
        'kind: Config\n'
        'users:\n'
        '- name: admin\n'
        '  user:\n'
        f'{user_block}'
    )


class KubeConfigParseTests(SimpleTestCase):
    def test_parse_minimal_kubeconfig(self):
        raw = """
apiVersion: v1
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: kubernetes
contexts:
- context:
    cluster: kubernetes
    user: admin
  name: admin@kubernetes
current-context: admin@kubernetes
kind: Config
users:
- name: admin
  user:
    token: token
"""
        parsed = parse_kubeconfig_text(raw)
        self.assertEqual(parsed['api_server'], 'https://127.0.0.1:6443')
        self.assertEqual(parsed['context_name'], 'admin@kubernetes')

    def test_parse_hard_wrapped_certificate_data(self):
        raw = """
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: AAAA
BBBB
CCCC
    server: https://127.0.0.1:6443
  name: docker-desktop
contexts:
- context:
    cluster: docker-desktop
    user: docker-desktop
  name: docker-desktop
current-context: docker-desktop
kind: Config
users:
- name: docker-desktop
  user:
    client-certificate-data: DDDD
EEEE
    client-key-data: FFFF
GGGG
"""
        normalized = normalize_kubeconfig_text(raw)
        parsed = parse_kubeconfig_text(raw)

        self.assertIn('certificate-authority-data: AAAABBBBCCCC', normalized)
        self.assertIn('client-certificate-data: DDDDEEEE', normalized)
        self.assertEqual(parsed['api_server'], 'https://127.0.0.1:6443')


class K8sClusterProbeTests(SimpleTestCase):
    def test_probe_cluster_uses_shared_client_factory(self):
        cluster = SimpleNamespace(context_name='ctx', network_plugin='')
        node = SimpleNamespace(
            status=SimpleNamespace(
                node_info=SimpleNamespace(container_runtime_version='containerd://1.7.0')
            )
        )

        class FakeVersionApi:
            def __init__(self, api_client):
                self.api_client = api_client

            def get_code(self, _request_timeout=None):
                return SimpleNamespace(git_version='v1.30.0')

        class FakeCoreV1Api:
            def __init__(self, api_client):
                self.api_client = api_client

            # 记录调用时拿到的超时：不给超时的话走的是 urllib3 的「不超时」，一个丢包的
            # 地址能把部署收尾的这次探测挂住几分钟（见 services.probe_cluster 的注释）。
            timeouts = []

            def list_node(self, _request_timeout=None):
                FakeCoreV1Api.timeouts.append(_request_timeout)
                return SimpleNamespace(items=[node])

        fake_client = SimpleNamespace(VersionApi=FakeVersionApi, CoreV1Api=FakeCoreV1Api)
        with patch('k8s_resource.services.client_factory.build_api_client', return_value=(fake_client, object())) as mocked:
            payload = probe_cluster(cluster)

        mocked.assert_called_once_with(cluster)
        self.assertEqual(payload['kube_version'], 'v1.30.0')
        self.assertEqual(payload['node_count'], 1)
        self.assertEqual(FakeCoreV1Api.timeouts, [8])

    def test_probe_cluster_passes_a_timeout_to_the_version_api(self):
        """版本查询同样要给超时，否则它就是那个能挂几分钟的调用。"""
        cluster = SimpleNamespace(context_name='ctx', network_plugin='')
        seen = []

        class FakeVersionApi:
            def __init__(self, api_client):
                self.api_client = api_client

            def get_code(self, _request_timeout=None):
                seen.append(_request_timeout)
                return SimpleNamespace(git_version='v1.30.0')

        class FakeCoreV1Api:
            def __init__(self, api_client):
                self.api_client = api_client

            def list_node(self, _request_timeout=None):
                return SimpleNamespace(items=[])

        fake_client = SimpleNamespace(VersionApi=FakeVersionApi, CoreV1Api=FakeCoreV1Api)
        with patch('k8s_resource.services.client_factory.build_api_client', return_value=(fake_client, object())):
            probe_cluster(cluster)

        self.assertEqual(seen, [8])


class K8sClusterSerializerTests(TestCase):
    def test_invalid_kubeconfig_returns_validation_error(self):
        serializer = K8sClusterSerializer(data={'name': '本机', 'status': 'configured', 'kubeconfig': 'apiVersion: v1\nclusters:\n- cluster:\nBAD'})

        self.assertFalse(serializer.is_valid())
        self.assertIn('kubeconfig', serializer.errors)

    def test_create_with_kubeconfig_fills_api_server_before_model_save(self):
        raw = """
apiVersion: v1
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: kubernetes
contexts:
- context:
    cluster: kubernetes
    user: admin
  name: admin@kubernetes
current-context: admin@kubernetes
kind: Config
users:
- name: admin
  user:
    token: token
"""
        serializer = K8sClusterSerializer(data={'name': '本机', 'status': 'configured', 'remark': '111', 'kubeconfig': raw})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        cluster = serializer.save()

        self.assertEqual(cluster.api_server, 'https://127.0.0.1:6443')
        self.assertEqual(cluster.cluster_name, 'kubernetes')
        self.assertTrue(K8sClusterCredential.objects.filter(cluster=cluster, encrypted_kubeconfig__gt='').exists())

    def test_lb_mode_cannot_be_written_through_the_api(self):
        """外部访问入口模式只读。

        模式在装集群签 ApiServer 证书时就定下来了，事后改它只会让记录与集群实际情况脱节。
        接口能写的话，「cloud 但地址为空」这种自相矛盾的记录就能被造出来：概览页会显示成
        「云内网 LB 」后面没有地址，运维任务也会继承到一个空地址。
        """
        cluster = K8sCluster.objects.create(name='kubernetes', api_server='https://127.0.0.1:6443')
        serializer = K8sClusterSerializer(
            cluster, data={'lb_mode': 'cloud', 'api_server_vip': '10.0.9.250'}, partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        cluster.refresh_from_db()

        self.assertEqual(cluster.lb_mode, 'none')
        self.assertEqual(cluster.api_server_vip, '10.0.9.250')

    def test_serializer_returns_safe_credential_metadata_only(self):
        cluster = K8sCluster.objects.create(name='kubernetes', api_server='https://127.0.0.1:6443')
        K8sClusterCredential.objects.create(
            cluster=cluster,
            encrypted_kubeconfig=crypto.encrypt_secret('secret-kubeconfig'),
            encrypted_client_key=crypto.encrypt_secret('secret-key'),
            source_path='k8s/config-public',
            fingerprint='abcdef1234567890',
        )

        payload = K8sClusterSerializer(cluster).data

        self.assertTrue(payload['has_kubeconfig'])
        self.assertEqual(payload['credential_source_path'], 'k8s/config-public')
        self.assertEqual(payload['credential_fingerprint'], 'abcdef123456')
        self.assertNotIn('encrypted_kubeconfig', payload)
        self.assertNotIn('secret-kubeconfig', str(payload))

    def test_admin_can_reveal_kubeconfig_explicitly(self):
        user = User.objects.create_superuser(username='admin', password='x')
        cluster = K8sCluster.objects.create(name='kubernetes', api_server='https://127.0.0.1:6443')
        K8sClusterCredential.objects.create(cluster=cluster, encrypted_kubeconfig=crypto.encrypt_secret('secret-kubeconfig'))
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(f'/api/v1/k8s/clusters/{cluster.id}/reveal-kubeconfig/', {}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['kubeconfig'], 'secret-kubeconfig')
        self.assertTrue(K8sOperationAudit.objects.filter(action='reveal_kubeconfig', resource_name='kubernetes').exists())


class CertificateExpirationTests(TestCase):
    """证书到期：本地解析平台托管的那份 KubeConfig。

    集群连不上、已禁用都不影响这项展示，因此这些用例全程零网络 —— 数据全部来自本地凭据。
    """

    def setUp(self):
        """准备一个集群记录。"""
        self.cluster = K8sCluster.objects.create(name='prod', api_server='https://127.0.0.1:6443')

    def save_credential(self, **fields):
        """写入（或更新）这个集群的凭据。"""
        credential, _created = K8sClusterCredential.objects.get_or_create(cluster=self.cluster)
        for key, value in fields.items():
            setattr(credential, key, value)
        credential.save()
        return credential

    def test_reports_every_certificate_in_the_kubeconfig(self):
        """CA 与客户端证书各一条，剩下天数按最近的一张算。"""
        self.save_credential(encrypted_kubeconfig=crypto.encrypt_secret(
            kubeconfig_with(make_certificate(365, 'kubernetes'), make_certificate(400, 'admin')),
        ))

        result = certificate_expirations(self.cluster)

        self.assertEqual(result['status'], 'ok')
        self.assertEqual([item['label'] for item in result['certificates']], [
            '证书颁发机构（CA）', '客户端证书',
        ])
        self.assertEqual(result['days_left'], 364)
        self.assertEqual(result['certificates'][0]['common_name'], 'kubernetes')

    def test_warns_inside_ninety_days(self):
        """90 天内算需要注意。"""
        self.save_credential(encrypted_kubeconfig=crypto.encrypt_secret(
            kubeconfig_with(make_certificate(365), make_certificate(60, 'admin')),
        ))

        self.assertEqual(certificate_expirations(self.cluster)['status'], 'warning')

    def test_urgent_inside_thirty_days(self):
        """30 天内算紧急：续期这件事要排在别的事情前面。"""
        self.save_credential(encrypted_kubeconfig=crypto.encrypt_secret(
            kubeconfig_with(make_certificate(20)),
        ))

        self.assertEqual(certificate_expirations(self.cluster)['status'], 'danger')

    def test_expired_certificate_is_reported_as_expired_with_a_negative_count(self):
        """已经过期时状态是 expired，剩余天数为负 —— 界面据此说「已过期 N 天」。

        天数一律向下取整（``//``），因此造证书与读证书之间那几微秒会让 -5 天读成 -6 天。
        这个偏差方向是**保守**的：永远比实际少报一点时间，不会把快到期的说成还早。
        """
        self.save_credential(encrypted_kubeconfig=crypto.encrypt_secret(
            kubeconfig_with(make_certificate(-5)),
        ))

        result = certificate_expirations(self.cluster)

        self.assertEqual(result['status'], 'expired')
        self.assertLessEqual(result['days_left'], -5)
        self.assertGreaterEqual(result['days_left'], -6)
        self.assertEqual(result['certificates'][0]['status'], 'expired')

    def test_token_authentication_is_unknown_not_an_error(self):
        """Token 认证的集群里没有证书可看，给一句中文原因即可，不算失败。"""
        self.save_credential(encrypted_kubeconfig=crypto.encrypt_secret(
            kubeconfig_with(token='eyJhbGciOiJSUzI1NiJ9.fake'),
        ))

        result = certificate_expirations(self.cluster)

        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['certificates'], [])
        self.assertIn('Token', result['message'])

    def test_a_cluster_without_credentials_says_so(self):
        """没有托管凭据时说清是为什么，而不是显示一片空白。"""
        result = certificate_expirations(self.cluster)

        self.assertEqual(result['status'], 'unknown')
        self.assertIn('没有托管 KubeConfig 凭据', result['message'])

    def test_falls_back_to_the_stored_certificate_when_the_kubeconfig_is_broken(self):
        """KubeConfig 坏掉时用凭据里单独存的那张证书顶上来。

        这两个字段是历史遗留（导入时一并存下的），但坏 kubeconfig 时它们是唯一还剩的信息源。
        """
        self.save_credential(
            encrypted_kubeconfig=crypto.encrypt_secret('这不是 YAML: ['),
            encrypted_certificate_authority=crypto.encrypt_secret(
                base64.b64encode(make_certificate(120).encode('utf-8')).decode('ascii'),
            ),
        )

        result = certificate_expirations(self.cluster)

        self.assertEqual(len(result['certificates']), 1)
        self.assertEqual(result['status'], 'ok')
        # 用的是副本这件事要说出来：否则界面上的「剩余 120 天」会被当成 KubeConfig 自己算的。
        self.assertIn('KubeConfig 解析失败', result['message'])
        self.assertIn('副本', result['message'])

    def test_a_broken_credential_does_not_raise(self):
        """解不开的密文降级成「读不到」，绝不让集群列表与详情打不开。"""
        self.save_credential(encrypted_kubeconfig='not-a-valid-ciphertext')

        result = certificate_expirations(self.cluster)

        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['certificates'], [])

    def test_summary_carries_the_earliest_certificate(self):
        """摘要里带上最紧迫的那一张，供列表页一行显示。"""
        self.save_credential(encrypted_kubeconfig=crypto.encrypt_secret(
            kubeconfig_with(make_certificate(200), make_certificate(45, 'admin')),
        ))

        summary = cluster_certificate_summary(self.cluster)

        self.assertEqual(summary['cluster_id'], self.cluster.pk)
        self.assertEqual(summary['next']['label'], '客户端证书')
        self.assertEqual(summary['next']['days_left'], 44)


class ClusterCertificatesEndpointTests(TestCase):
    """``GET /k8s/clusters/certificates/``：列表页「证书到期」列的数据源。"""

    def setUp(self):
        """准备管理员与两个集群。"""
        self.user = User.objects.create_superuser(username='admin', password='x')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.with_cert = K8sCluster.objects.create(name='prod', api_server='https://127.0.0.1:6443')
        self.without_cert = K8sCluster.objects.create(name='dev', api_server='https://127.0.0.2:6443')
        K8sClusterCredential.objects.create(
            cluster=self.with_cert,
            encrypted_kubeconfig=crypto.encrypt_secret(kubeconfig_with(make_certificate(30))),
        )

    def test_lists_every_visible_cluster(self):
        """没有证书的集群也在列表里（状态是「未知」），否则表格里会缺行。"""
        response = self.client.get('/api/v1/k8s/clusters/certificates/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['items']), 2)
        by_name = {item['cluster_name']: item for item in response.data['items']}
        self.assertEqual(by_name['prod']['status'], 'danger')
        self.assertEqual(by_name['dev']['status'], 'unknown')

    def test_sorts_the_most_urgent_first(self):
        """最紧迫的排在最前面：这个列表是给人看「哪张要到期了」的。"""
        response = self.client.get('/api/v1/k8s/clusters/certificates/')

        self.assertEqual(response.data['items'][0]['cluster_name'], 'prod')

    def test_carries_a_timestamp(self):
        """带上生成时间：证书状态是那一刻算出来的快照。"""
        response = self.client.get('/api/v1/k8s/clusters/certificates/')

        self.assertRegex(response.data['generated_at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


class K8sClusterDeployingStateTests(TestCase):
    """「部署中」集群带来的状态约束。

    部署向导不再直接登记集群记录，集群容器改由部署任务成功后回填，
    因此这里直接构造 deploying 状态的集群来验证状态相关的既有约束。
    """

    def setUp(self):
        self.user = User.objects.create_superuser(username='admin', password='x')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def error_message(self, response):
        """把统一错误结构里的 detail 拍平成一句文本，便于断言中文提示。"""
        detail = response.data['detail']
        return '，'.join(str(item) for item in detail) if isinstance(detail, list) else str(detail)

    def create_deploying_cluster(self, **overrides):
        """构造一条部署中的集群记录，API Server 与凭据此时都还不存在。"""
        payload = {
            'name': 'prod-k8s-01',
            'status': 'deploying',
            'kube_version': 'v1.30.0',
            'network_plugin': 'calico',
            'node_count': 3,
        }
        payload.update(overrides)
        return K8sCluster.objects.create(**payload)

    def test_deploying_cluster_rejects_connection_actions(self):
        cluster = self.create_deploying_cluster()

        test_response = self.client.post(f'/api/v1/k8s/clusters/{cluster.id}/test/', {}, format='json')
        sync_response = self.client.post(f'/api/v1/k8s/clusters/{cluster.id}/sync/', {}, format='json')
        reveal_response = self.client.post(f'/api/v1/k8s/clusters/{cluster.id}/reveal-kubeconfig/', {}, format='json')

        for response in (test_response, sync_response, reveal_response):
            self.assertEqual(response.status_code, 400)
            self.assertIn('集群仍在部署中', self.error_message(response))

    def test_deleting_cluster_unbinds_only_its_own_machines(self):
        cluster = self.create_deploying_cluster()
        bound = Machine.objects.create(
            name='node-01', ip_address='192.168.10.1', roles=['master', 'etcd'], cluster_name='prod-k8s-01',
        )
        other = Machine.objects.create(
            name='node-02', ip_address='192.168.10.2', roles=['node'], cluster_name='other-k8s',
        )

        response = self.client.delete(f'/api/v1/k8s/clusters/{cluster.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(K8sCluster.objects.filter(pk=cluster.pk).exists())
        bound.refresh_from_db()
        self.assertEqual(bound.roles, [])
        self.assertEqual(bound.cluster_name, '')
        # 只清本集群名下的机器，别的集群绑定不受影响。
        other.refresh_from_db()
        self.assertEqual(other.roles, ['node'])
        self.assertEqual(other.cluster_name, 'other-k8s')
        self.assertTrue(
            AuditLog.objects.filter(action='Machine.unassign', resource__startswith='node-01').exists()
        )

    def test_summary_counts_deploying_clusters(self):
        self.create_deploying_cluster()
        K8sCluster.objects.create(name='kubernetes', api_server='https://127.0.0.1:6443')

        response = self.client.get('/api/v1/k8s/clusters/summary/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 2)
        self.assertEqual(response.data['deploying'], 1)
        self.assertEqual(response.data['credential_count'], 0)


class ClusterRestoreEndpointTests(TestCase):
    """「备份恢复」接口被拒时的说法。

    94 上「点了恢复没反应」的现场就是这里：``backup.BackupError`` 是个普通异常，DRF 不认识
    它，接口回的是 HTML 500，页面上只剩一句 Server Error —— 用户既看不到原因，也不知道
    下一步该做什么。业务异常必须转成带中文说明的 400。
    """

    def setUp(self):
        """准备管理员、一个已部署集群与其配置版本。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入用户、集群与配置版本。
        """
        from k8s_cluster.models import K8sClusterConfigRevision

        self.user = User.objects.create_superuser(username='admin', password='x')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.cluster = K8sCluster.objects.create(
            name='prod-k8s-01', slug='prod-k8s-01', origin='deployed', status='configured',
            kube_version='1.29.4', network_plugin='calico', node_count=3,
        )
        K8sClusterConfigRevision.objects.create(
            cluster=self.cluster, revision=1, config_hash='a' * 64,
            snapshot={
                'kubeasz_version': '3.6.7',
                'kubernetes_version': '1.29.4',
                'nodes': [{'name': 'n1', 'ip_address': '192.168.20.10', 'roles': ['master']}],
            },
        )

    def make_backup(self, **overrides):
        """造一条成功的备份记录。

        参数：`overrides` 表示要覆盖的字段。
        返回：返回 K8sClusterBackup。
        副作用：向数据库写入一条备份记录。
        """
        from k8s_deployment.models import K8sClusterBackup

        values = {
            'cluster': self.cluster,
            'backup_id': 'bk-endpoint-fixture',
            'slug': self.cluster.slug,
            'kubernetes_version': '1.29.4',
            'kubeasz_version': '3.6.7',
            'config_revision': 1,
            'status': 'succeeded',
            'object_prefix': 'kubeasz/backups/prod-k8s-01/fixture',
            'archive_sha256': 'b' * 64,
            'archive_size': 1,
        }
        values.update(overrides)
        return K8sClusterBackup.objects.create(**values)

    def restore(self, record, confirm=None):
        """提交一次恢复。

        参数：`record` 表示备份记录；`confirm` 表示二次确认输入的集群名，默认与集群同名。
        返回：返回响应对象。
        副作用：可能写入任务记录。
        """
        return self.client.post(
            f'/api/v1/k8s/clusters/{self.cluster.pk}/backups/{record.backup_id}/restore/',
            {'confirm_name': self.cluster.name if confirm is None else confirm}, format='json',
        )

    def error_message(self, response):
        """把统一错误结构里的 detail 拍平成一句文本。

        参数：`response` 表示响应对象。
        返回：返回错误文本。
        副作用：纯计算。
        """
        detail = response.data['detail']
        return '，'.join(str(item) for item in detail) if isinstance(detail, list) else str(detail)

    def test_version_mismatch_returns_a_readable_400(self):
        """版本对不上时回 400 与那句中文原因，而不是 HTML 500。"""
        record = self.make_backup(kubeasz_version='3.7.0')

        response = self.restore(record)

        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertIn('跨版本恢复不受支持', self.error_message(response))

    def test_matching_backup_is_accepted(self):
        """版本对得上的备份能正常提交恢复任务 —— 这正是升级失败后的回滚路径。"""
        record = self.make_backup()

        response = self.restore(record)

        self.assertEqual(response.status_code, 201, response.content[:300])
        self.assertEqual(response.data['task_type'], 'restore')

    def test_cleared_cluster_is_told_it_was_cleared(self):
        """集群已清理时给的是「已经清理过了」，而不是一句标识不一致的胡话。"""
        record = self.make_backup()
        self.cluster.slug = ''
        self.cluster.status = 'destroyed'
        self.cluster.save(update_fields=['slug', 'status'])

        response = self.restore(record)

        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertIn('已经清理过了', self.error_message(response))
