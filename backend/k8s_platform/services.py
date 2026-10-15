import base64
import gzip
import json
import os
import shlex
import subprocess
import tempfile
import time

import yaml
from django.utils import timezone

from k8s_cluster.services import decrypted_kubeconfig
from k8s_resource.services.client_factory import get_core_v1_api
from ops.models import SystemSetting


HELM_TIMEOUT = 90
HELM_RESOURCE_TIMEOUT = 25
HELM_RESOURCE_RETRIES = 2


def _dt(value):
    if not value:
        return ''
    if isinstance(value, str):
        return value
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S')


def _meta(obj):
    metadata = getattr(obj, 'metadata', None)
    return {
        'name': getattr(metadata, 'name', '') or '',
        'namespace': getattr(metadata, 'namespace', '') or '',
        'labels': getattr(metadata, 'labels', None) or {},
        'created_at': _dt(getattr(metadata, 'creation_timestamp', None)),
    }


def _dict_get(payload, path, default=None):
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return value if value is not None else default


def _decode_helm_release(encoded):
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return {}
    try:
        raw = gzip.decompress(base64.b64decode(raw))
    except Exception:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return {}


def _is_transient_k8s_error(exc):
    message = str(exc).lower()
    return any(text in message for text in ['httpsconnection', 'max retries', 'timed out', 'timeout', 'connection reset', 'connection aborted', 'temporarily unavailable'])


def _list_helm_secret_response(api, namespace='', label_selector='owner=helm'):
    for attempt in range(HELM_RESOURCE_RETRIES + 1):
        try:
            if namespace:
                return api.list_namespaced_secret(namespace, label_selector=label_selector, _request_timeout=HELM_RESOURCE_TIMEOUT)
            return api.list_secret_for_all_namespaces(label_selector=label_selector, _request_timeout=HELM_RESOURCE_TIMEOUT)
        except Exception as exc:
            if attempt >= HELM_RESOURCE_RETRIES or not _is_transient_k8s_error(exc):
                raise
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError('Helm Secret 查询失败')


def list_helm_releases(cluster, namespace='', keyword=''):
    """List Helm v3 releases from owner=helm Secrets."""
    api = get_core_v1_api(cluster)
    response = _list_helm_secret_response(api, namespace=namespace)
    releases = []
    for secret in response.items:
        meta = _meta(secret)
        labels = meta['labels']
        data = getattr(secret, 'data', None) or {}
        release = _decode_helm_release(data.get('release', '')) if data.get('release') else {}
        chart = _dict_get(release, ['chart', 'metadata', 'name'], labels.get('name', ''))
        chart_version = _dict_get(release, ['chart', 'metadata', 'version'], '')
        chart_display = f'{chart}-{chart_version}' if chart and chart_version else chart
        app_version = _dict_get(release, ['chart', 'metadata', 'appVersion'], '')
        item = {
            'name': labels.get('name') or release.get('name') or meta['name'],
            'namespace': meta['namespace'],
            'revision': labels.get('version') or release.get('version') or '',
            'status': labels.get('status') or _dict_get(release, ['info', 'status'], ''),
            'chart': chart,
            'chart_version': chart_version,
            'chart_display': chart_display,
            'app_version': app_version,
            'updated_at': _dict_get(release, ['info', 'last_deployed'], '') or meta['created_at'],
            'description': _dict_get(release, ['info', 'description'], ''),
            'secret_name': meta['name'],
        }
        haystack = json.dumps(item, ensure_ascii=False).lower()
        if keyword and keyword.lower() not in haystack:
            continue
        releases.append(item)
    return sorted(releases, key=lambda item: (item['namespace'], item['name']))


def _helm_release_secret(cluster, namespace, name):
    api = get_core_v1_api(cluster)
    response = _list_helm_secret_response(api, namespace=namespace, label_selector=f'owner=helm,name={name}')
    candidates = []
    for secret in response.items:
        meta = _meta(secret)
        labels = meta['labels']
        if labels.get('name') == name:
            candidates.append(secret)
    if not candidates:
        raise ValueError('Helm Release 不存在或当前用户不可访问')
    candidates.sort(key=lambda item: int((getattr(getattr(item, 'metadata', None), 'labels', None) or {}).get('version') or 0), reverse=True)
    return candidates[0]


def helm_manifest_resources(manifest, default_namespace=''):
    """Parse Helm manifest YAML into resource rows for the management page."""
    resources = []
    for doc in yaml.safe_load_all(manifest or ''):
        if not isinstance(doc, dict):
            continue
        metadata = doc.get('metadata') or {}
        resources.append({
            'kind': doc.get('kind') or '',
            'apiVersion': doc.get('apiVersion') or '',
            'name': metadata.get('name') or '',
            'namespace': metadata.get('namespace') or default_namespace or '',
            'labels': metadata.get('labels') or {},
            'annotations': metadata.get('annotations') or {},
        })
    return resources


def helm_release_detail(cluster, namespace, name):
    """Return decoded Helm release details without requiring the helm binary."""
    if not namespace or not name:
        raise ValueError('Release 名称和命名空间不能为空')
    secret = _helm_release_secret(cluster, namespace, name)
    meta = _meta(secret)
    data = getattr(secret, 'data', None) or {}
    release = _decode_helm_release(data.get('release', '')) if data.get('release') else {}
    chart = _dict_get(release, ['chart', 'metadata', 'name'], meta['labels'].get('name', ''))
    chart_version = _dict_get(release, ['chart', 'metadata', 'version'], '')
    config = release.get('config') if isinstance(release.get('config'), dict) else {}
    manifest = release.get('manifest') or ''
    return {
        'name': meta['labels'].get('name') or release.get('name') or name,
        'namespace': namespace,
        'revision': meta['labels'].get('version') or release.get('version') or '',
        'status': meta['labels'].get('status') or _dict_get(release, ['info', 'status'], ''),
        'chart': chart,
        'chart_version': chart_version,
        'chart_display': f'{chart}-{chart_version}' if chart and chart_version else chart,
        'app_version': _dict_get(release, ['chart', 'metadata', 'appVersion'], ''),
        'updated_at': _dict_get(release, ['info', 'last_deployed'], '') or meta['created_at'],
        'description': _dict_get(release, ['info', 'description'], ''),
        'secret_name': meta['name'],
        'values_yaml': yaml.safe_dump(config, allow_unicode=True, sort_keys=False) if config else '{}\n',
        'manifest': manifest,
        'resources': helm_manifest_resources(manifest, default_namespace=namespace),
    }


def configured_helm_path():
    """Read the platform Helm command path, defaulting to helm in PATH."""
    setting = SystemSetting.objects.filter(key='ci.helm_tools').first()
    value = setting.value if setting and isinstance(setting.value, dict) else {}
    path = str(value.get('path') or value.get('helm_path') or 'helm').strip()
    return path or 'helm'


def _run_helm(cluster, args, input_text='', timeout=HELM_TIMEOUT):
    kubeconfig_text = decrypted_kubeconfig(cluster)
    kubeconfig_path = ''
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, suffix='.kubeconfig') as handle:
            handle.write(kubeconfig_text)
            kubeconfig_path = handle.name
        command = [configured_helm_path(), '--kubeconfig', kubeconfig_path, *args]
        completed = subprocess.run(command, input=input_text or None, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError('未找到 Helm 命令，请先在平台设置中配置 ci.helm_tools 路径') from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'Helm 命令执行超时，超过 {timeout} 秒未返回') from exc
    finally:
        if kubeconfig_path:
            try:
                os.remove(kubeconfig_path)
            except OSError:
                pass
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or 'Helm 命令执行失败').strip()
        raise RuntimeError(message[:800])
    return {
        'stdout': (completed.stdout or '').strip(),
        'stderr': (completed.stderr or '').strip(),
        'command': 'helm ' + ' '.join(shlex.quote(str(item)) for item in args),
    }


def helm_upgrade_release(cluster, namespace, name, chart_ref, values_yaml, reuse_values=True):
    """Upgrade an existing Helm release with edited values through helm upgrade."""
    if not namespace or not name:
        raise ValueError('Release 名称和命名空间不能为空')
    chart = str(chart_ref or '').strip()
    if not chart:
        raise ValueError('Chart 引用不能为空，请填写仓库 Chart、OCI 地址或本地 Chart 路径')
    try:
        values = yaml.safe_load(values_yaml or '{}') or {}
    except yaml.YAMLError as exc:
        raise ValueError('Values 不是合法 YAML') from exc
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, suffix='.values.yaml') as handle:
        yaml.safe_dump(values, handle, allow_unicode=True, sort_keys=False)
        values_path = handle.name
    try:
        args = ['upgrade', name, chart, '--namespace', namespace, '-f', values_path, '--history-max', '20']
        if reuse_values:
            args.append('--reuse-values')
        return _run_helm(cluster, args)
    finally:
        try:
            os.remove(values_path)
        except OSError:
            pass


def helm_uninstall_release(cluster, namespace, name, keep_history=False):
    """Uninstall a Helm release from the selected namespace."""
    if not namespace or not name:
        raise ValueError('Release 名称和命名空间不能为空')
    args = ['uninstall', name, '--namespace', namespace]
    if keep_history:
        args.append('--keep-history')
    return _run_helm(cluster, args)
