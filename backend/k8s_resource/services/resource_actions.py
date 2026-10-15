from django.utils import timezone

from .client_factory import get_batch_v1_api, get_core_v1_api, get_dynamic_client
from .resource_reader import list_workloads
from .resource_mutator import RESOURCE_KEY


WORKLOAD_KINDS = {'Deployment', 'StatefulSet', 'DaemonSet'}


def _resource_handle(cluster, kind):
    api_version, resource_kind, namespaced = RESOURCE_KEY.get(kind, ('', '', False))
    if not api_version:
        raise ValueError(f'暂不支持操作 {kind}')
    dynamic = get_dynamic_client(cluster)
    return dynamic.resources.get(api_version=api_version, kind=resource_kind), namespaced


def cordon_node(cluster, name, unschedulable=True):
    """Mark a node schedulable/unschedulable through CoreV1 patch_node."""
    if not name:
        raise ValueError('节点名称不能为空')
    api = get_core_v1_api(cluster)
    body = {'spec': {'unschedulable': bool(unschedulable)}}
    return api.patch_node(name=name, body=body)


def drain_node(cluster, name):
    """Cordon a node and evict non-daemonset pods using policy/v1 eviction."""
    if not name:
        raise ValueError('节点名称不能为空')
    api = get_core_v1_api(cluster)
    cordon_node(cluster, name, True)
    pods = api.list_pod_for_all_namespaces(field_selector=f'spec.nodeName={name}', _request_timeout=10).items
    pdb_warnings = drain_pdb_warnings(cluster, name)
    evicted = []
    skipped = []
    for pod in pods:
        meta = getattr(pod, 'metadata', None)
        namespace = getattr(meta, 'namespace', '') or 'default'
        pod_name = getattr(meta, 'name', '') or ''
        owner_refs = getattr(meta, 'owner_references', None) or []
        if any(getattr(owner, 'kind', '') == 'DaemonSet' for owner in owner_refs):
            skipped.append({'namespace': namespace, 'name': pod_name, 'reason': 'DaemonSet'})
            continue
        if namespace == 'kube-system':
            skipped.append({'namespace': namespace, 'name': pod_name, 'reason': 'kube-system'})
            continue
        body = {
            'apiVersion': 'policy/v1',
            'kind': 'Eviction',
            'metadata': {'name': pod_name, 'namespace': namespace},
        }
        try:
            api.create_namespaced_pod_eviction(name=pod_name, namespace=namespace, body=body)
            evicted.append({'namespace': namespace, 'name': pod_name})
        except Exception as exc:
            skipped.append({'namespace': namespace, 'name': pod_name, 'reason': str(exc)[:120]})
    return {'evicted': evicted, 'skipped': skipped, 'pdb_warnings': pdb_warnings}


def drain_pdb_warnings(cluster, node_name=''):
    """Return PDB entries that may block evictions on a node.

    The blocking PDB set is cluster-wide, so callers that only need the list can omit
    ``node_name``; the returned entries then carry no ``node`` key.
    """
    warnings = []
    try:
        pdbs = [item for item in list_workloads(cluster) if item.get('kind') == 'PodDisruptionBudget']
    except Exception:
        return warnings
    for item in pdbs:
        detail = item.get('detail') or {}
        if (detail.get('disruptions_allowed') or 0) <= 0:
            entry = {
                'namespace': item.get('namespace') or '',
                'name': item.get('name') or '',
                'summary': item.get('summary') or '',
            }
            if node_name:
                entry['node'] = node_name
            warnings.append(entry)
    return warnings


def scale_workload(cluster, item, replicas):
    """Scale Deployment/StatefulSet/DaemonSet style workload by patching spec.replicas."""
    if item.get('kind') not in WORKLOAD_KINDS:
        raise ValueError(f'{item.get("kind")} 暂不支持扩缩容')
    replicas = int(replicas)
    if replicas < 0:
        raise ValueError('副本数不能小于 0')
    resource, _namespaced = _resource_handle(cluster, item['kind'])
    return resource.patch(
        name=item['name'],
        namespace=item.get('namespace') or 'default',
        body={'spec': {'replicas': replicas}},
    )


def restart_workload(cluster, item):
    """Trigger a rolling restart by patching pod template annotation."""
    if item.get('kind') not in WORKLOAD_KINDS:
        raise ValueError(f'{item.get("kind")} 暂不支持滚动重启')
    from django.utils import timezone
    resource, _namespaced = _resource_handle(cluster, item['kind'])
    restarted_at = timezone.now().isoformat()
    body = {
        'spec': {
            'template': {
                'metadata': {
                    'annotations': {
                        'kubectl.kubernetes.io/restartedAt': restarted_at,
                    },
                },
            },
        },
    }
    resource.patch(name=item['name'], namespace=item.get('namespace') or 'default', body=body)
    return {'restarted_at': restarted_at}


def trigger_cronjob(cluster, item):
    """Create one Job from a CronJob template for manual execution."""
    if item.get('kind') != 'CronJob':
        raise ValueError('只有 CronJob 支持手动触发')
    namespace = item.get('namespace') or 'default'
    name = item.get('name') or ''
    if not name:
        raise ValueError('CronJob 名称不能为空')
    api = get_batch_v1_api(cluster)
    cronjob = api.read_namespaced_cron_job(name=name, namespace=namespace, _request_timeout=10)
    template = getattr(getattr(cronjob, 'spec', None), 'job_template', None)
    job_spec = getattr(template, 'spec', None)
    if not job_spec:
        raise ValueError('CronJob 缺少 jobTemplate')
    suffix = timezone.now().strftime('%Y%m%d%H%M%S')
    job_name = f'{name}-manual-{suffix}'[:63].rstrip('-')
    body = {
        'apiVersion': 'batch/v1',
        'kind': 'Job',
        'metadata': {
            'name': job_name,
            'namespace': namespace,
            'labels': {'cronjob.kubernetes.io/manual-trigger': name},
        },
        'spec': job_spec.to_dict() if hasattr(job_spec, 'to_dict') else job_spec,
    }
    api.create_namespaced_job(namespace=namespace, body=body, _request_timeout=10)
    return {'job_name': job_name, 'namespace': namespace}


def suspend_cronjob(cluster, item, suspend=True):
    """Patch CronJob suspend flag."""
    if item.get('kind') != 'CronJob':
        raise ValueError('只有 CronJob 支持暂停/恢复')
    namespace = item.get('namespace') or 'default'
    name = item.get('name') or ''
    if not name:
        raise ValueError('CronJob 名称不能为空')
    api = get_batch_v1_api(cluster)
    body = {'spec': {'suspend': bool(suspend)}}
    api.patch_namespaced_cron_job(name=name, namespace=namespace, body=body, _request_timeout=10)
    return {'suspend': bool(suspend), 'namespace': namespace}


def reveal_secret(cluster, item):
    """Read and decode Secret values for authorized reveal actions."""
    if item.get('kind') != 'Secret':
        raise ValueError('只有 Secret 支持查看敏感值')
    import base64
    api = get_core_v1_api(cluster)
    secret = api.read_namespaced_secret(name=item['name'], namespace=item.get('namespace') or 'default')
    data = getattr(secret, 'data', None) or {}
    decoded = {}
    for key, value in data.items():
        try:
            decoded[key] = base64.b64decode(value).decode('utf-8')
        except Exception:
            decoded[key] = '<binary>'
    return {'type': getattr(secret, 'type', '') or 'Opaque', 'data': decoded}
