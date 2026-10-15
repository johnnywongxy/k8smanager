from k8s_audit.models import K8sOperationAudit
from platform_logs.models import AuditLog
from ops.permissions import user_organization

from .client_factory import get_dynamic_client


RESOURCE_KEY = {
    'Namespace': ('v1', 'Namespace', False),
    'Node': ('v1', 'Node', False),
    'Pod': ('v1', 'Pod', True),
    'Service': ('v1', 'Service', True),
    'ConfigMap': ('v1', 'ConfigMap', True),
    'Secret': ('v1', 'Secret', True),
    'PersistentVolumeClaim': ('v1', 'PersistentVolumeClaim', True),
    'PersistentVolume': ('v1', 'PersistentVolume', False),
    'Deployment': ('apps/v1', 'Deployment', True),
    'StatefulSet': ('apps/v1', 'StatefulSet', True),
    'DaemonSet': ('apps/v1', 'DaemonSet', True),
    'Job': ('batch/v1', 'Job', True),
    'CronJob': ('batch/v1', 'CronJob', True),
    'Ingress': ('networking.k8s.io/v1', 'Ingress', True),
    'StorageClass': ('storage.k8s.io/v1', 'StorageClass', False),
    'Event': ('v1', 'Event', True),
}

CREATABLE_KINDS = {
    'Namespace',
    'ConfigMap',
    'Secret',
    'Service',
    'Ingress',
    'PersistentVolumeClaim',
    'StorageClass',
}


def _resource_handle(cluster, kind):
    api_version, resource_kind, namespaced = RESOURCE_KEY.get(kind, ('', '', False))
    if not api_version:
        raise ValueError(f'暂不支持操作 {kind}')
    dynamic = get_dynamic_client(cluster)
    return dynamic.resources.get(api_version=api_version, kind=resource_kind), namespaced


def _metadata_patch(labels, annotations):
    return {
        'metadata': {
            'labels': labels or {},
            'annotations': annotations or {},
        },
    }


def _metadata_body(payload, namespaced):
    metadata = {
        'name': payload.get('name', '').strip(),
        'labels': payload.get('labels') or {},
        'annotations': payload.get('annotations') or {},
    }
    namespace = payload.get('namespace', '').strip()
    if namespaced and namespace:
        metadata['namespace'] = namespace
    return metadata


def build_create_body(kind, payload, namespaced):
    metadata = _metadata_body(payload, namespaced)
    if not metadata['name']:
        raise ValueError('名称不能为空')
    if namespaced and not metadata.get('namespace'):
        raise ValueError('命名空间不能为空')

    if kind == 'Namespace':
        return {'apiVersion': 'v1', 'kind': kind, 'metadata': metadata}
    if kind == 'ConfigMap':
        return {'apiVersion': 'v1', 'kind': kind, 'metadata': metadata, 'data': payload.get('data') or {}}
    if kind == 'Secret':
        return {
            'apiVersion': 'v1',
            'kind': kind,
            'metadata': metadata,
            'type': payload.get('secret_type') or 'Opaque',
            'stringData': payload.get('data') or {},
        }
    if kind == 'Service':
        service_type = payload.get('service_type') or 'ClusterIP'
        port = int(payload.get('port') or 80)
        target_port = int(payload.get('target_port') or port)
        selector = payload.get('selector') or {}
        body = {
            'apiVersion': 'v1',
            'kind': kind,
            'metadata': metadata,
            'spec': {
                'type': service_type,
                'selector': selector,
                'ports': [{'port': port, 'targetPort': target_port, 'protocol': payload.get('protocol') or 'TCP'}],
            },
        }
        if payload.get('node_port') and service_type == 'NodePort':
            body['spec']['ports'][0]['nodePort'] = int(payload['node_port'])
        return body
    if kind == 'Ingress':
        host = payload.get('host', '').strip()
        service_name = payload.get('service_name', '').strip()
        service_port = int(payload.get('service_port') or 80)
        path = payload.get('path') or '/'
        if not host or not service_name:
            raise ValueError('Ingress host 和后端 Service 不能为空')
        body = {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': kind,
            'metadata': metadata,
            'spec': {
                'rules': [{
                    'host': host,
                    'http': {'paths': [{
                        'path': path,
                        'pathType': payload.get('path_type') or 'Prefix',
                        'backend': {'service': {'name': service_name, 'port': {'number': service_port}}},
                    }]},
                }],
            },
        }
        if payload.get('ingress_class_name'):
            body['spec']['ingressClassName'] = payload['ingress_class_name']
        return body
    if kind == 'PersistentVolumeClaim':
        return {
            'apiVersion': 'v1',
            'kind': kind,
            'metadata': metadata,
            'spec': {
                'accessModes': payload.get('access_modes') or ['ReadWriteOnce'],
                'resources': {'requests': {'storage': payload.get('storage_size') or '1Gi'}},
                **({'storageClassName': payload['storage_class_name']} if payload.get('storage_class_name') else {}),
            },
        }
    if kind == 'StorageClass':
        return {
            'apiVersion': 'storage.k8s.io/v1',
            'kind': kind,
            'metadata': metadata,
            'provisioner': payload.get('provisioner') or 'kubernetes.io/no-provisioner',
            'reclaimPolicy': payload.get('reclaim_policy') or 'Retain',
            'volumeBindingMode': payload.get('volume_binding_mode') or 'WaitForFirstConsumer',
        }
    raise ValueError(f'暂不支持新增 {kind}')


def create_resource(cluster, kind, payload):
    if kind not in CREATABLE_KINDS:
        raise ValueError(f'{kind} 暂不支持页面新增')
    resource, namespaced = _resource_handle(cluster, kind)
    body = build_create_body(kind, payload, namespaced)
    kwargs = {'body': body}
    if namespaced:
        kwargs['namespace'] = body['metadata']['namespace']
    return resource.create(**kwargs), body


def patch_resource_metadata(cluster, item, labels, annotations, data=None, update_data=False):
    resource, namespaced = _resource_handle(cluster, item['kind'])
    body = _metadata_patch(labels, annotations)
    if update_data and item['kind'] == 'ConfigMap':
        body['data'] = data or {}
    if update_data and item['kind'] == 'Secret':
        body['stringData'] = data or {}
    kwargs = {'name': item['name'], 'body': body}
    if namespaced:
        kwargs['namespace'] = item.get('namespace') or 'default'
    return resource.patch(**kwargs)


def delete_resource(cluster, item, force=False):
    resource, namespaced = _resource_handle(cluster, item['kind'])
    kwargs = {'name': item['name']}
    if namespaced:
        kwargs['namespace'] = item.get('namespace') or 'default'
    if force and item['kind'] == 'Pod':
        kwargs['body'] = {'gracePeriodSeconds': 0, 'propagationPolicy': 'Background'}
    return resource.delete(**kwargs)


def write_resource_audit(request, cluster, item, action, status='completed', detail=None, failure_reason=''):
    organization = user_organization(request.user)
    actor = getattr(request.user, 'username', 'system') or 'system'
    AuditLog.objects.create(
        organization=organization,
        actor=actor,
        action=f'k8s.resource.{action}',
        resource=f'{item.get("kind", "")}/{item.get("namespace", "")}/{item.get("name", "")}',
        ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() if request.META.get('HTTP_X_FORWARDED_FOR') else request.META.get('REMOTE_ADDR')) or '127.0.0.1',
        detail={
            'cluster_id': cluster.pk,
            'cluster_name': cluster.name,
            'namespace': item.get('namespace', ''),
            'kind': item.get('kind', ''),
            'name': item.get('name', ''),
            'status': status,
            'failure_reason': failure_reason[:240],
            **(detail or {}),
        },
    )
    K8sOperationAudit.objects.create(
        organization=organization,
        cluster_name=cluster.name,
        namespace=item.get('namespace', ''),
        resource_type=item.get('kind', ''),
        resource_name=item.get('name', ''),
        action=action,
        actor=actor,
        status=status,
        request_summary=detail or {},
        result_summary={'cluster_id': cluster.pk, 'uid': item.get('uid', '')},
        failure_reason=failure_reason[:240],
    )
