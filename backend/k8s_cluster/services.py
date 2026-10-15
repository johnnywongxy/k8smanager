import base64
import hashlib
import time

import yaml
from django.utils import timezone

from ops.tenancy import assign_organization

from . import crypto
from .models import K8sCluster, K8sClusterCredential


WRAPPABLE_KUBECONFIG_KEYS = {
    'certificate-authority-data',
    'client-certificate-data',
    'client-key-data',
    'token',
}
BASE64_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')


def select_preferred_cluster(queryset):
    """Select a deterministic cluster, preferring healthy connections."""
    for status in ('available', 'configured', 'unconfigured', 'unavailable'):
        cluster = queryset.filter(status=status).order_by('name', 'pk').first()
        if cluster:
            return cluster
    return queryset.exclude(status='disabled').order_by('name', 'pk').first()


def normalize_kubeconfig_text(raw_text):
    """Join hard-wrapped kubeconfig scalar values before YAML parsing."""
    lines = (raw_text or '').splitlines()
    normalized = []
    pending_index = None
    for line in lines:
        stripped = line.strip()
        if (
            pending_index is not None and
            stripped and
            not line.startswith((' ', '\t', '-')) and
            ':' not in stripped and
            set(stripped) <= BASE64_CHARS
        ):
            normalized[pending_index] = f'{normalized[pending_index].rstrip()}{stripped}'
            continue
        normalized.append(line.rstrip())
        pending_index = None
        if ':' in stripped:
            key = stripped.split(':', 1)[0].lstrip('- ').strip()
            value = stripped.split(':', 1)[1].strip()
            if key in WRAPPABLE_KUBECONFIG_KEYS and value:
                pending_index = len(normalized) - 1
    return '\n'.join(normalized).strip() + '\n' if normalized else ''


def parse_kubeconfig_text(raw_text):
    """Parse kubeconfig text and return safe metadata plus certificate material."""
    normalized_text = normalize_kubeconfig_text(raw_text)
    try:
        payload = yaml.safe_load(normalized_text) or {}
    except yaml.YAMLError:
        raise ValueError('KubeConfig 格式不是合法 YAML，请确认粘贴完整 kubeconfig，并保持原始缩进')
    current_context = payload.get('current-context') or ''
    clusters = payload.get('clusters') or []
    contexts = payload.get('contexts') or []
    users = payload.get('users') or []

    selected_context = next((item for item in contexts if item.get('name') == current_context), contexts[0] if contexts else {})
    context_data = selected_context.get('context') or {}
    cluster_name = context_data.get('cluster') or (clusters[0].get('name') if clusters else '')
    user_name = context_data.get('user') or (users[0].get('name') if users else '')
    cluster_item = next((item for item in clusters if item.get('name') == cluster_name), clusters[0] if clusters else {})
    cluster_data = cluster_item.get('cluster') or {}
    user_item = next((item for item in users if item.get('name') == user_name), users[0] if users else {})
    user_data = user_item.get('user') or {}
    server = cluster_data.get('server') or ''
    if not server:
        raise ValueError('kubeconfig 中缺少 clusters[].cluster.server')

    return {
        'cluster_name': cluster_name or cluster_item.get('name') or 'kubernetes',
        'context_name': current_context or selected_context.get('name') or '',
        'user_name': user_name or user_item.get('name') or '',
        'api_server': server,
        'certificate_authority_data': cluster_data.get('certificate-authority-data') or '',
        'client_certificate_data': user_data.get('client-certificate-data') or '',
        'client_key_data': user_data.get('client-key-data') or '',
        'token': user_data.get('token') or '',
    }


def kubeconfig_fingerprint(raw_text):
    """Return a SHA256 fingerprint for duplicate import detection."""
    return hashlib.sha256(raw_text.encode('utf-8')).hexdigest()


def import_kubeconfig(raw_text, user, source_path='', name=''):
    """Create or update a cluster from kubeconfig text without exposing secrets."""
    metadata = parse_kubeconfig_text(raw_text)
    fingerprint = kubeconfig_fingerprint(raw_text)
    cluster = K8sCluster.objects.filter(credential__fingerprint=fingerprint).first()
    if not cluster:
        cluster = K8sCluster(api_server=metadata['api_server'])
    cluster.name = name or cluster.name or metadata['cluster_name'] or 'kubernetes'
    cluster.cluster_name = metadata['cluster_name']
    cluster.context_name = metadata['context_name']
    cluster.user_name = metadata['user_name']
    cluster.api_server = metadata['api_server']
    if metadata['api_server'].startswith('https://47.120.40.124'):
        cluster.public_api_server = metadata['api_server']
    cluster.auth_type = 'kubeconfig'
    if cluster.status != 'disabled':
        cluster.status = 'configured'
    cluster.remark = cluster.remark or '从 k8s/config-public 导入'
    cluster.save()
    assign_organization(cluster, user)
    cluster.save()

    credential, _created = K8sClusterCredential.objects.get_or_create(cluster=cluster)
    credential.encrypted_kubeconfig = crypto.encrypt_secret(raw_text)
    credential.encrypted_client_certificate = crypto.encrypt_secret(metadata['client_certificate_data'])
    credential.encrypted_client_key = crypto.encrypt_secret(metadata['client_key_data'])
    credential.encrypted_certificate_authority = crypto.encrypt_secret(metadata['certificate_authority_data'])
    credential.encrypted_token = crypto.encrypt_secret(metadata['token'])
    credential.source_path = source_path
    credential.fingerprint = fingerprint
    credential.rotated_at = timezone.now()
    credential.save()
    return cluster


def decrypted_kubeconfig(cluster):
    """Return the decrypted kubeconfig for a cluster."""
    credential = getattr(cluster, 'credential', None)
    if not credential or not credential.encrypted_kubeconfig:
        raise ValueError('集群未配置 kubeconfig 凭据')
    return crypto.decrypt_secret(credential.encrypted_kubeconfig)


# 证书剩余天数的两档提醒线：30 天内算紧急、90 天内算需要注意。前端按同一组阈值上色。
CERTIFICATE_DANGER_DAYS = 30
CERTIFICATE_WARNING_DAYS = 90

# 证书在界面上的名字，与 kubeconfig 里的字段一一对应。
CERTIFICATE_LABELS = {
    'certificate_authority_data': '证书颁发机构（CA）',
    'client_certificate_data': '客户端证书',
}

# kubeconfig 字段 → 凭据模型里对应的密文字段。导入集群时平台把每一张证书单独也存了一份，
# KubeConfig 本体坏掉（手改过、被截断、加密时用的密钥轮换过）时它们是唯一还剩的信息源。
# 名字对不上不是拼错，是两边的历史命名：kubeconfig 用 ``*-data``、模型用 ``encrypted_*``。
CERTIFICATE_CREDENTIAL_FIELDS = {
    'certificate_authority_data': 'encrypted_certificate_authority',
    'client_certificate_data': 'encrypted_client_certificate',
}


def _decode_pem(value):
    """把 kubeconfig 里的 ``*-data`` 字段还原成 PEM 文本。

    参数：`value` 表示 base64 的 PEM（或已经是 PEM 的文本）。
    返回：返回 PEM 文本；不是证书内容时返回空串。
    副作用：只做纯计算。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    if text.startswith('-----BEGIN'):
        return text
    try:
        decoded = base64.b64decode(text, validate=False)
    except (ValueError, TypeError):
        return ''
    if b'-----BEGIN' not in decoded:
        return ''
    return decoded.decode('utf-8', 'replace')


def _decrypt_credential_field(credential, name):
    """读一份凭据密文，解不开时返回空串。

    参数：`credential` 表示凭据实例；`name` 表示字段名。
    返回：返回明文；没有这份凭据或解不开时返回空串。
    副作用：只做纯计算。
    """
    value = getattr(credential, name, '') if credential is not None else ''
    if not value:
        return ''
    try:
        return crypto.decrypt_secret(value)
    except Exception:  # noqa: BLE001 - 证书展示不该因为一份坏密文整块不可用
        return ''


def certificate_status(days_left):
    """把剩余天数翻译成状态码。

    参数：`days_left` 表示剩余天数，可以为负（已过期）。
    返回：返回 ``expired`` / ``danger`` / ``warning`` / ``ok``。
    副作用：只做纯计算。
    """
    if days_left < 0:
        return 'expired'
    if days_left <= CERTIFICATE_DANGER_DAYS:
        return 'danger'
    if days_left <= CERTIFICATE_WARNING_DAYS:
        return 'warning'
    return 'ok'


def certificate_expirations(cluster):
    """读取一个集群托管凭据里各张证书的有效期。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'status', 'message', 'days_left', 'certificates': [...]}``。
    副作用：解密凭据并做本地解析；不联网、不修改任何数据。

    只用平台自己托管的那份 KubeConfig：它同时覆盖导入型与部署型集群，且**零网络** ——
    集群连不上、已停用都不影响这项展示。节点上由 kubeasz 签发的 apiserver/etcd 证书不在
    这里（那要登节点读 ``/etc/kubernetes/ssl``）。

    任何异常都降级成 ``status='unknown'`` + 一句中文原因：证书是给人看的参考信息，
    不该让集群列表或详情因为一份坏凭据打不开。
    """
    credential = getattr(cluster, 'credential', None)
    result = {'status': 'unknown', 'message': '', 'days_left': None, 'certificates': []}
    if credential is None or not (
        credential.encrypted_kubeconfig
        or credential.encrypted_certificate_authority
        or credential.encrypted_client_certificate
    ):
        result['message'] = '该集群没有托管 KubeConfig 凭据，平台读不到它的证书。'
        return result

    metadata = {}
    parse_error = ''
    if credential.encrypted_kubeconfig:
        try:
            metadata = parse_kubeconfig_text(decrypted_kubeconfig(cluster))
        except Exception as exc:  # noqa: BLE001 - 坏 kubeconfig 时回落到凭据里的单张证书
            parse_error = str(exc)

    if parse_error:
        notes = [f'KubeConfig 解析失败（{parse_error}），下列证书取自凭据里单独存储的副本']
    else:
        notes = []

    certificates = []
    unavailable = []
    for field, label in CERTIFICATE_LABELS.items():
        pem_text = _decode_pem(
            metadata.get(field)
            or _decrypt_credential_field(credential, CERTIFICATE_CREDENTIAL_FIELDS[field])
        )
        if not pem_text:
            unavailable.append(f'{label}缺失')
            continue
        try:
            certificates.append(_certificate_info(label, pem_text))
        except Exception:  # noqa: BLE001 - 单张证书记不出来时保留其余几张
            unavailable.append(f'{label}无法解析')
    notes.extend(unavailable)

    if not certificates:
        result['message'] = (
            f'读不到可用的证书（{"；".join(notes) or "KubeConfig 里没有客户端证书"}）。'
            '使用 Token 认证或只导入了访问凭据的集群会出现这种情况。'
        )
        return result

    days = min(item['days_left'] for item in certificates)
    result['days_left'] = days
    result['status'] = certificate_status(days)
    result['certificates'] = certificates
    if notes:
        result['message'] = f'注意：{"；".join(notes)}。'
    return result


def _certificate_info(label, pem_text):
    """解析一张证书的到期信息。

    参数：`label` 表示界面上显示的名字；`pem_text` 表示 PEM 文本。
    返回：返回 ``{'label', 'common_name', 'not_before', 'not_after', 'days_left', 'status'}``。
    副作用：只做纯计算；不通过时报出异常由调用方降级。

    用 ``cryptography`` 在平台本机解析（``requirements.txt`` 里已有这个依赖，备份加密走的就是它），
    因此不需要连集群、也不需要节点上有 openssl。
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    certificate = x509.load_pem_x509_certificate(pem_text.encode('utf-8'))
    attributes = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    common_name = str(attributes[0].value) if attributes else certificate.subject.rfc4514_string()
    not_after = certificate.not_valid_after_utc
    not_before = certificate.not_valid_before_utc
    days_left = int((not_after - timezone.now()).total_seconds() // 86400)
    return {
        'label': label,
        'common_name': common_name,
        'not_before': not_before.strftime('%Y-%m-%d %H:%M:%S'),
        'not_after': not_after.strftime('%Y-%m-%d %H:%M:%S'),
        'days_left': days_left,
        'status': certificate_status(days_left),
    }


def cluster_certificate_summary(cluster):
    """证书信息的一份紧凑摘要，供列表与跨集群视图使用。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'cluster_id', 'cluster_name', 'status', 'days_left', 'next', 'certificates'}``。
    副作用：与 ``certificate_expirations`` 相同。
    """
    detail = certificate_expirations(cluster)
    certificates = detail['certificates']
    next_item = min(certificates, key=lambda item: item['days_left']) if certificates else None
    return {
        'cluster_id': cluster.pk,
        'cluster_name': cluster.name,
        'status': detail['status'],
        'message': detail['message'],
        'days_left': detail['days_left'],
        'next': (
            {'label': next_item['label'], 'common_name': next_item['common_name'],
             'not_after': next_item['not_after'], 'days_left': next_item['days_left']}
            if next_item else None
        ),
        'certificates': certificates,
    }


def probe_cluster(cluster):
    """Probe API server, version and node/runtime information through the real K8s API."""
    from k8s_resource.services.client_factory import build_api_client

    started = time.perf_counter()
    client, api_client = build_api_client(cluster)
    version_api = client.VersionApi(api_client)
    core_api = client.CoreV1Api(api_client)
    # 两次调用都要给超时：不给的话走的是 urllib3 的默认「不超时」，一个不可达但又不拒绝连接的
    # 地址（丢包、安全组静默丢弃）能把这次探测挂住几分钟，而它出现在部署收尾的登记步骤里。
    version_info = version_api.get_code(_request_timeout=8)
    nodes = core_api.list_node(_request_timeout=8).items
    latency_ms = int((time.perf_counter() - started) * 1000)
    runtime_versions = sorted({
        node.status.node_info.container_runtime_version
        for node in nodes
        if node.status and node.status.node_info and node.status.node_info.container_runtime_version
    })
    return {
        'status': 'available',
        'message': f'连接成功，Kubernetes {version_info.git_version}，节点 {len(nodes)} 个',
        'latency_ms': latency_ms,
        'kube_version': version_info.git_version,
        'node_count': len(nodes),
        'runtime_version': ', '.join(runtime_versions[:3]),
        'network_plugin': cluster.network_plugin or 'Calico',
        'node_summary': summarize_nodes(nodes),
    }


def summarize_nodes(nodes):
    """Collect per-node kubelet version and Ready state from K8s node objects.

    Upgrade prechecks need both: a cluster whose kubelets disagree on version rolls out
    node by node into an inconsistent state, and a NotReady node cannot be drained at all.
    Health checks also use this to name the nodes that are not Ready instead of only
    reporting how many nodes answered.
    """
    details = []
    for node in nodes or []:
        meta = getattr(node, 'metadata', None)
        status = getattr(node, 'status', None)
        info = getattr(status, 'node_info', None) if status else None
        conditions = (getattr(status, 'conditions', None) or []) if status else []
        ready = any(
            getattr(condition, 'type', '') == 'Ready' and getattr(condition, 'status', '') == 'True'
            for condition in conditions
        )
        details.append({
            'name': str(getattr(meta, 'name', '') or ''),
            'kubelet_version': str(getattr(info, 'kubelet_version', '') or ''),
            'ready': bool(ready),
        })

    versions = {}
    for item in details:
        if item['kubelet_version']:
            versions[item['kubelet_version']] = versions.get(item['kubelet_version'], 0) + 1
    return {
        'total': len(details),
        'ready': sum(1 for item in details if item['ready']),
        'not_ready': [item['name'] for item in details if not item['ready']],
        'versions': versions,
        'details': details,
    }


def test_cluster_connection(cluster):
    """Run a connectivity test and persist the result."""
    from k8s_resource.services.errors import translate_k8s_error

    if cluster.status == 'disabled':
        cluster.mark_test_result('disabled', '集群已禁用，不能测试连接')
        return cluster
    try:
        result = probe_cluster(cluster)
        cluster.kube_version = result['kube_version']
        cluster.node_count = result['node_count']
        cluster.runtime_version = result['runtime_version']
        cluster.network_plugin = result['network_plugin']
        cluster.last_synced_at = timezone.now()
        cluster.save(update_fields=['kube_version', 'node_count', 'runtime_version', 'network_plugin', 'last_synced_at', 'updated_at'])
        cluster.mark_test_result(result['status'], result['message'], result['latency_ms'], result)
    except Exception as exc:
        cluster.mark_test_result('unavailable', f'连接失败：{translate_k8s_error(exc)}')
    return cluster


def sync_cluster_summary(cluster):
    """Refresh cluster summary metadata from Kubernetes API."""
    result = probe_cluster(cluster)
    cluster.kube_version = result['kube_version']
    cluster.node_count = result['node_count']
    cluster.runtime_version = result['runtime_version']
    cluster.network_plugin = result['network_plugin']
    cluster.status = result['status']
    cluster.last_synced_at = timezone.now()
    cluster.last_test_status = result['status']
    cluster.last_test_message = result['message'][:240]
    cluster.last_tested_at = timezone.now()
    cluster.save()
    return cluster
