from datetime import timezone as datetime_timezone

from kubernetes import client
from django.utils import timezone

from .client_factory import get_apps_v1_api, get_autoscaling_v2_api, get_batch_v1_api, get_core_v1_api, get_networking_v1_api, get_policy_v1_api, get_rbac_v1_api, get_storage_v1_api


RESOURCE_TIMEOUT = 10
KIND_API_VERSIONS = {
    'Namespace': 'v1',
    'Node': 'v1',
    'Pod': 'v1',
    'Service': 'v1',
    'ConfigMap': 'v1',
    'Secret': 'v1',
    'ServiceAccount': 'v1',
    'Event': 'v1',
    'Deployment': 'apps/v1',
    'StatefulSet': 'apps/v1',
    'DaemonSet': 'apps/v1',
    'Job': 'batch/v1',
    'CronJob': 'batch/v1',
    'HorizontalPodAutoscaler': 'autoscaling/v2',
    'PodDisruptionBudget': 'policy/v1',
    'Ingress': 'networking.k8s.io/v1',
    'Role': 'rbac.authorization.k8s.io/v1',
    'RoleBinding': 'rbac.authorization.k8s.io/v1',
    'ClusterRole': 'rbac.authorization.k8s.io/v1',
    'ClusterRoleBinding': 'rbac.authorization.k8s.io/v1',
    'PersistentVolumeClaim': 'v1',
    'PersistentVolume': 'v1',
    'StorageClass': 'storage.k8s.io/v1',
}
METADATA_DROP_FIELDS = {
    'creationTimestamp',
    'deletionGracePeriodSeconds',
    'deletionTimestamp',
    'finalizers',
    'generation',
    'managedFields',
    'ownerReferences',
    'resourceVersion',
    'selfLink',
    'uid',
}
ANNOTATION_DROP_KEYS = {
    'kubectl.kubernetes.io/last-applied-configuration',
}


def _clean_empty(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            next_item = _clean_empty(item)
            if next_item in ({}, [], None):
                continue
            cleaned[key] = next_item
        return cleaned
    if isinstance(value, list):
        cleaned = []
        for item in value:
            next_item = _clean_empty(item)
            if next_item not in ({}, [], None):
                cleaned.append(next_item)
        return cleaned
    return value


def _plain_value(value):
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    if isinstance(value, tuple):
        return [_plain_value(item) for item in value]
    if hasattr(value, 'to_dict'):
        return _plain_value(value.to_dict())
    if hasattr(value, '__dict__'):
        return {
            key: _plain_value(item)
            for key, item in vars(value).items()
            if not key.startswith('_')
        }
    return value


def _manifest(kind, obj):
    """Return a clean Kubernetes manifest suitable for YAML export/edit backfill."""
    try:
        payload = client.ApiClient().sanitize_for_serialization(obj) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if not payload.get('metadata') and getattr(obj, 'metadata', None):
        payload['metadata'] = _plain_value(getattr(obj, 'metadata', None))
    if not payload.get('spec') and getattr(obj, 'spec', None):
        payload['spec'] = _plain_value(getattr(obj, 'spec', None))
    payload.pop('status', None)
    payload['apiVersion'] = payload.get('apiVersion') or getattr(obj, 'api_version', '') or KIND_API_VERSIONS.get(kind, 'v1')
    payload['kind'] = payload.get('kind') or kind
    metadata = payload.setdefault('metadata', {})
    if isinstance(metadata, dict):
        for field in METADATA_DROP_FIELDS:
            metadata.pop(field, None)
        annotations = metadata.get('annotations') or {}
        if isinstance(annotations, dict):
            for key in ANNOTATION_DROP_KEYS:
                annotations.pop(key, None)
            if annotations:
                metadata['annotations'] = annotations
            else:
                metadata.pop('annotations', None)
    return _clean_empty(payload)


def _dt(value):
    """Return a timezone-aware datetime or None for API timestamps."""
    if not value:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, datetime_timezone.utc)
    return value


def _metadata(obj):
    meta = getattr(obj, 'metadata', None)
    return {
        'name': getattr(meta, 'name', '') or '',
        'namespace': getattr(meta, 'namespace', '') or '',
        'labels': getattr(meta, 'labels', None) or {},
        'annotations': getattr(meta, 'annotations', None) or {},
        'created_at': _dt(getattr(meta, 'creation_timestamp', None)),
        'uid': getattr(meta, 'uid', '') or '',
    }


def _conditions(conditions):
    return [
        {
            'type': getattr(item, 'type', '') or '',
            'status': getattr(item, 'status', '') or '',
            'reason': getattr(item, 'reason', '') or '',
            'message': getattr(item, 'message', '') or '',
        }
        for item in (conditions or [])
    ]


def _selector(selector):
    return ','.join(f'{key}={value}' for key, value in (selector or {}).items())


def _resource(kind, obj, **extra):
    meta = _metadata(obj)
    item = {
        'kind': kind,
        'name': meta['name'],
        'namespace': meta['namespace'],
        'labels': meta['labels'],
        'annotations': meta['annotations'],
        'created_at': meta['created_at'],
        'uid': meta['uid'],
        'status': '',
        'status_display': '',
        'summary': '',
        'detail': {},
        'yaml_summary': {},
    }
    item.update(extra)
    item['yaml_summary'] = _manifest(kind, obj)
    return item


def _empty_resource(kind, name, namespace='', **extra):
    item = {
        'kind': kind,
        'name': name or '',
        'namespace': namespace or '',
        'labels': {},
        'annotations': {},
        'created_at': None,
        'uid': extra.pop('uid', '') or '',
        'status': '',
        'status_display': '',
        'summary': '',
        'detail': {},
        'yaml_summary': {
            'apiVersion': 'node.status/v1',
            'kind': kind,
            'metadata': {
                'name': name or '',
                'namespace': namespace or '',
                'labels': {},
                'annotations': {},
            },
        },
    }
    item.update(extra)
    return item


def _format_bytes(value):
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        size = 0
    if size >= 1024 ** 3:
        return f'{size / (1024 ** 3):.2f} GiB'
    if size >= 1024 ** 2:
        return f'{size / (1024 ** 2):.2f} MiB'
    if size >= 1024:
        return f'{size / 1024:.2f} KiB'
    return f'{size} B'


def _matches(item, keyword='', status=''):
    keyword = (keyword or '').lower()
    if keyword:
        haystack = ' '.join([
            item.get('name', ''),
            item.get('namespace', ''),
            item.get('kind', ''),
            item.get('summary', ''),
            str(item.get('detail', {})),
        ]).lower()
        if keyword not in haystack:
            return False
    if status and item.get('status') != status:
        return False
    return True


def _filter(items, namespace='', keyword='', status=''):
    result = []
    for item in items:
        if namespace and item.get('namespace') and item.get('namespace') != namespace:
            continue
        if _matches(item, keyword, status):
            result.append(item)
    return result


def list_namespaces(cluster, filters=None):
    """List namespaces from Kubernetes API without persisting them as CMDB master data."""
    filters = filters or {}
    api = get_core_v1_api(cluster)
    items = []
    for ns in api.list_namespace(_request_timeout=RESOURCE_TIMEOUT).items:
        status = getattr(getattr(ns, 'status', None), 'phase', '') or 'Unknown'
        items.append(_resource(
            'Namespace',
            ns,
            status=status,
            status_display=status,
            summary=f'{len(getattr(getattr(ns, "metadata", None), "labels", None) or {})} 个标签',
            detail={
                'conditions': _conditions(getattr(getattr(ns, 'status', None), 'conditions', None)),
            },
        ))
    return _filter(items, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_nodes(cluster, filters=None):
    """List nodes and expose runtime, kubelet version and condition summary."""
    filters = filters or {}
    api = get_core_v1_api(cluster)
    items = []
    for node in api.list_node(_request_timeout=RESOURCE_TIMEOUT).items:
        status_data = getattr(node, 'status', None)
        info = getattr(status_data, 'node_info', None)
        conditions = _conditions(getattr(status_data, 'conditions', None))
        ready = next((item for item in conditions if item['type'] == 'Ready'), {})
        status = 'Ready' if ready.get('status') == 'True' else 'NotReady'
        addresses = getattr(status_data, 'addresses', None) or []
        internal_ip = next((item.address for item in addresses if getattr(item, 'type', '') == 'InternalIP'), '')
        items.append(_resource(
            'Node',
            node,
            status=status,
            status_display='可用' if status == 'Ready' else '不可用',
            summary=f'{internal_ip or "-"} / {getattr(info, "container_runtime_version", "") or "-"}',
            detail={
                'internal_ip': internal_ip,
                'kubelet_version': getattr(info, 'kubelet_version', '') or '',
                'runtime_version': getattr(info, 'container_runtime_version', '') or '',
                'os_image': getattr(info, 'os_image', '') or '',
                'architecture': getattr(info, 'architecture', '') or '',
                'conditions': conditions,
            },
        ))
    return _filter(items, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_images(cluster, filters=None):
    """List every image cached on every Kubernetes node from Node.status.images."""
    filters = filters or {}
    api = get_core_v1_api(cluster)
    items = []
    for node in api.list_node(_request_timeout=RESOURCE_TIMEOUT).items:
        node_name = getattr(getattr(node, 'metadata', None), 'name', '') or ''
        images = getattr(getattr(node, 'status', None), 'images', None) or []
        for image in images:
            names = [name for name in (getattr(image, 'names', None) or []) if name]
            if not names:
                names = ['<none>']
            image_name = names[0]
            size_bytes = getattr(image, 'size_bytes', 0) or 0
            size_text = _format_bytes(size_bytes)
            items.append(_empty_resource(
                'Image',
                image_name,
                uid=f'{node_name}:{image_name}',
                status='Cached',
                status_display='已缓存',
                summary=f'{node_name} / {size_text}',
                detail={
                    'node_name': node_name,
                    'image': image_name,
                    'aliases': names,
                    'alias_count': len(names),
                    'size_bytes': size_bytes,
                    'size': size_text,
                },
            ))
    items.sort(key=lambda item: (item['detail'].get('node_name') or '', item['name']))
    return _filter(items, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_pods(cluster, namespace='', filters=None):
    """List pods, including restart count and container names."""
    filters = filters or {}
    api = get_core_v1_api(cluster)
    response = api.list_namespaced_pod(namespace, _request_timeout=RESOURCE_TIMEOUT) if namespace else api.list_pod_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT)
    items = []
    for pod in response.items:
        status_data = getattr(pod, 'status', None)
        container_statuses = getattr(status_data, 'container_statuses', None) or []
        restart_count = sum(getattr(item, 'restart_count', 0) or 0 for item in container_statuses)
        containers = [getattr(item, 'name', '') for item in (getattr(getattr(pod, 'spec', None), 'containers', None) or [])]
        phase = getattr(status_data, 'phase', '') or 'Unknown'
        pod_ip = getattr(status_data, 'pod_ip', '') or ''
        node_name = getattr(getattr(pod, 'spec', None), 'node_name', '') or ''
        items.append(_resource(
            'Pod',
            pod,
            status=phase,
            status_display=phase,
            summary=f'{pod_ip or "-"} / 重启 {restart_count} 次',
            detail={
                'pod_ip': pod_ip,
                'node_name': node_name,
                'containers': containers,
                'restart_count': restart_count,
                'conditions': _conditions(getattr(status_data, 'conditions', None)),
            },
        ))
    return _filter(items, namespace=namespace, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_workloads(cluster, namespace='', filters=None):
    """List Deployment, StatefulSet, DaemonSet, Job and CronJob resources."""
    filters = filters or {}
    apps_api = get_apps_v1_api(cluster)
    batch_api = get_batch_v1_api(cluster)
    items = []

    deployments = apps_api.list_namespaced_deployment(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else apps_api.list_deployment_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in deployments:
        status_data = getattr(obj, 'status', None)
        desired = getattr(getattr(obj, 'spec', None), 'replicas', 0) or 0
        ready = getattr(status_data, 'ready_replicas', 0) or 0
        items.append(_resource('Deployment', obj, status='Ready' if ready == desired else 'Updating', status_display=f'{ready}/{desired}', summary=f'副本 {ready}/{desired}', detail={'selector': _selector(getattr(getattr(obj, "spec", None), "selector", None).match_labels if getattr(getattr(obj, "spec", None), "selector", None) else {}), 'conditions': _conditions(getattr(status_data, 'conditions', None))}))

    statefulsets = apps_api.list_namespaced_stateful_set(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else apps_api.list_stateful_set_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in statefulsets:
        status_data = getattr(obj, 'status', None)
        desired = getattr(getattr(obj, 'spec', None), 'replicas', 0) or 0
        ready = getattr(status_data, 'ready_replicas', 0) or 0
        items.append(_resource('StatefulSet', obj, status='Ready' if ready == desired else 'Updating', status_display=f'{ready}/{desired}', summary=f'副本 {ready}/{desired}', detail={'conditions': _conditions(getattr(status_data, 'conditions', None))}))

    daemonsets = apps_api.list_namespaced_daemon_set(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else apps_api.list_daemon_set_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in daemonsets:
        status_data = getattr(obj, 'status', None)
        desired = getattr(status_data, 'desired_number_scheduled', 0) or 0
        ready = getattr(status_data, 'number_ready', 0) or 0
        items.append(_resource('DaemonSet', obj, status='Ready' if ready == desired else 'Updating', status_display=f'{ready}/{desired}', summary=f'节点 {ready}/{desired}', detail={'conditions': _conditions(getattr(status_data, 'conditions', None))}))

    jobs = batch_api.list_namespaced_job(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else batch_api.list_job_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in jobs:
        status_data = getattr(obj, 'status', None)
        succeeded = getattr(status_data, 'succeeded', 0) or 0
        failed = getattr(status_data, 'failed', 0) or 0
        status = 'Failed' if failed else ('Succeeded' if succeeded else 'Running')
        items.append(_resource('Job', obj, status=status, status_display=status, summary=f'成功 {succeeded} / 失败 {failed}', detail={'conditions': _conditions(getattr(status_data, 'conditions', None))}))

    cronjobs = batch_api.list_namespaced_cron_job(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else batch_api.list_cron_job_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in cronjobs:
        spec = getattr(obj, 'spec', None)
        status = 'Suspended' if getattr(spec, 'suspend', False) else 'Active'
        items.append(_resource('CronJob', obj, status=status, status_display=status, summary=getattr(spec, 'schedule', '') or '-', detail={'schedule': getattr(spec, 'schedule', '') or '', 'suspend': getattr(spec, 'suspend', False)}))

    try:
        autoscaling_api = get_autoscaling_v2_api(cluster)
        hpas = autoscaling_api.list_namespaced_horizontal_pod_autoscaler(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else autoscaling_api.list_horizontal_pod_autoscaler_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
        for obj in hpas:
            spec = getattr(obj, 'spec', None)
            status_data = getattr(obj, 'status', None)
            target = getattr(spec, 'scale_target_ref', None)
            current = getattr(status_data, 'current_replicas', 0) or 0
            desired = getattr(status_data, 'desired_replicas', 0) or 0
            min_replicas = getattr(spec, 'min_replicas', 0) or 0
            max_replicas = getattr(spec, 'max_replicas', 0) or 0
            items.append(_resource(
                'HorizontalPodAutoscaler',
                obj,
                status='Ready' if current == desired else 'Scaling',
                status_display=f'{current}/{desired}',
                summary=f'副本 {current}/{desired} / 范围 {min_replicas}-{max_replicas}',
                detail={
                    'target_kind': getattr(target, 'kind', '') or '',
                    'target_name': getattr(target, 'name', '') or '',
                    'min_replicas': min_replicas,
                    'max_replicas': max_replicas,
                    'current_replicas': current,
                    'desired_replicas': desired,
                    'conditions': _conditions(getattr(status_data, 'conditions', None)),
                },
            ))
    except Exception:
        pass

    try:
        policy_api = get_policy_v1_api(cluster)
        pdbs = policy_api.list_namespaced_pod_disruption_budget(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else policy_api.list_pod_disruption_budget_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
        for obj in pdbs:
            status_data = getattr(obj, 'status', None)
            disruptions = getattr(status_data, 'disruptions_allowed', 0) or 0
            expected = getattr(status_data, 'expected_pods', 0) or 0
            current = getattr(status_data, 'current_healthy', 0) or 0
            items.append(_resource(
                'PodDisruptionBudget',
                obj,
                status='Ready' if disruptions else 'Limited',
                status_display=f'允许 {disruptions}',
                summary=f'健康 {current}/{expected} / 可驱逐 {disruptions}',
                detail={
                    'disruptions_allowed': disruptions,
                    'current_healthy': current,
                    'desired_healthy': getattr(status_data, 'desired_healthy', 0) or 0,
                    'expected_pods': expected,
                    'selector': _selector(getattr(getattr(obj, 'spec', None), 'selector', None).match_labels if getattr(getattr(obj, 'spec', None), 'selector', None) else {}),
                    'conditions': _conditions(getattr(status_data, 'conditions', None)),
                },
            ))
    except Exception:
        pass

    kind = filters.get('kind', '')
    if kind:
        items = [item for item in items if item['kind'].lower() == kind.lower()]
    return _filter(items, namespace=namespace, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_network(cluster, namespace='', filters=None):
    """List Service and Ingress resources."""
    filters = filters or {}
    core_api = get_core_v1_api(cluster)
    networking_api = get_networking_v1_api(cluster)
    items = []
    services = core_api.list_namespaced_service(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else core_api.list_service_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in services:
        spec = getattr(obj, 'spec', None)
        ports = [f'{port.port}:{port.target_port}/{port.protocol}' for port in (getattr(spec, 'ports', None) or [])]
        cluster_ip = getattr(spec, 'cluster_ip', '') or ''
        items.append(_resource('Service', obj, status=getattr(spec, 'type', '') or 'Service', status_display=getattr(spec, 'type', '') or 'Service', summary=f'{cluster_ip or "-"} / {", ".join(ports) or "-"}', detail={'cluster_ip': cluster_ip, 'ports': ports, 'selector': getattr(spec, 'selector', None) or {}}))

    ingresses = networking_api.list_namespaced_ingress(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else networking_api.list_ingress_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in ingresses:
        spec = getattr(obj, 'spec', None)
        rules = getattr(spec, 'rules', None) or []
        hosts = [getattr(rule, 'host', '') for rule in rules if getattr(rule, 'host', '')]
        items.append(_resource('Ingress', obj, status='Configured', status_display='已配置', summary=', '.join(hosts) or '-', detail={'hosts': hosts, 'class_name': getattr(spec, 'ingress_class_name', '') or ''}))
    return _filter(items, namespace=namespace, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_configs(cluster, namespace='', filters=None):
    """List ConfigMap and Secret metadata. Secret values are never returned."""
    filters = filters or {}
    api = get_core_v1_api(cluster)
    items = []
    configmaps = api.list_namespaced_config_map(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else api.list_config_map_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in configmaps:
        data = getattr(obj, 'data', None) or {}
        items.append(_resource('ConfigMap', obj, status='Configured', status_display='已配置', summary=f'{len(data)} 个键', detail={'key_count': len(data), 'keys': sorted(data.keys()), 'data': data}))

    secrets = api.list_namespaced_secret(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else api.list_secret_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in secrets:
        data = getattr(obj, 'data', None) or {}
        secret_type = getattr(obj, 'type', '') or 'Opaque'
        items.append(_resource('Secret', obj, status='Configured', status_display='已配置', summary=f'{secret_type} / {len(data)} 个键', detail={'type': secret_type, 'key_count': len(data), 'keys': sorted(data.keys()), 'values_hidden': True}))

    try:
        rbac_api = get_rbac_v1_api(cluster)
        service_accounts = api.list_namespaced_service_account(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else api.list_service_account_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
        for obj in service_accounts:
            secrets = getattr(obj, 'secrets', None) or []
            items.append(_resource('ServiceAccount', obj, status='Configured', status_display='已配置', summary=f'{len(secrets)} 个 Secret 引用', detail={'secret_count': len(secrets), 'secrets': [getattr(item, "name", "") for item in secrets]}))

        roles = rbac_api.list_namespaced_role(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else rbac_api.list_role_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
        for obj in roles:
            rules = getattr(obj, 'rules', None) or []
            items.append(_resource('Role', obj, status='Configured', status_display='已配置', summary=f'{len(rules)} 条规则', detail={'rule_count': len(rules), 'rules': [getattr(rule, 'to_dict', lambda: {})() for rule in rules]}))

        role_bindings = rbac_api.list_namespaced_role_binding(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else rbac_api.list_role_binding_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
        for obj in role_bindings:
            subjects = getattr(obj, 'subjects', None) or []
            role_ref = getattr(obj, 'role_ref', None)
            items.append(_resource('RoleBinding', obj, status='Configured', status_display='已配置', summary=f'{getattr(role_ref, "kind", "")}/{getattr(role_ref, "name", "")} / {len(subjects)} 个主体', detail={'subjects': [getattr(subject, 'to_dict', lambda: {})() for subject in subjects], 'role_ref': getattr(role_ref, 'to_dict', lambda: {})()}))

        for obj in rbac_api.list_cluster_role(_request_timeout=RESOURCE_TIMEOUT).items:
            rules = getattr(obj, 'rules', None) or []
            items.append(_resource('ClusterRole', obj, status='Configured', status_display='已配置', summary=f'{len(rules)} 条规则', detail={'rule_count': len(rules), 'rules': [getattr(rule, 'to_dict', lambda: {})() for rule in rules]}))

        for obj in rbac_api.list_cluster_role_binding(_request_timeout=RESOURCE_TIMEOUT).items:
            subjects = getattr(obj, 'subjects', None) or []
            role_ref = getattr(obj, 'role_ref', None)
            items.append(_resource('ClusterRoleBinding', obj, status='Configured', status_display='已配置', summary=f'{getattr(role_ref, "kind", "")}/{getattr(role_ref, "name", "")} / {len(subjects)} 个主体', detail={'subjects': [getattr(subject, 'to_dict', lambda: {})() for subject in subjects], 'role_ref': getattr(role_ref, 'to_dict', lambda: {})()}))
    except Exception:
        pass

    kind = filters.get('kind', '')
    if kind:
        items = [item for item in items if item['kind'].lower() == kind.lower()]
    return _filter(items, namespace=namespace, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_storage(cluster, namespace='', filters=None):
    """List PVC, PV and StorageClass resources."""
    filters = filters or {}
    core_api = get_core_v1_api(cluster)
    storage_api = get_storage_v1_api(cluster)
    items = []
    pvcs = core_api.list_namespaced_persistent_volume_claim(namespace, _request_timeout=RESOURCE_TIMEOUT).items if namespace else core_api.list_persistent_volume_claim_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in pvcs:
        status_data = getattr(obj, 'status', None)
        spec = getattr(obj, 'spec', None)
        capacity = (getattr(status_data, 'capacity', None) or {}).get('storage', '')
        items.append(_resource('PersistentVolumeClaim', obj, status=getattr(status_data, 'phase', '') or 'Unknown', status_display=getattr(status_data, 'phase', '') or 'Unknown', summary=f'{capacity or "-"} / {getattr(spec, "storage_class_name", "") or "-"}', detail={'capacity': capacity, 'storage_class_name': getattr(spec, 'storage_class_name', '') or '', 'access_modes': getattr(spec, 'access_modes', None) or []}))

    pvs = core_api.list_persistent_volume(_request_timeout=RESOURCE_TIMEOUT).items
    for obj in pvs:
        status_data = getattr(obj, 'status', None)
        spec = getattr(obj, 'spec', None)
        capacity = (getattr(spec, 'capacity', None) or {}).get('storage', '')
        claim = getattr(spec, 'claim_ref', None)
        claim_ns = getattr(claim, 'namespace', '') or ''
        if namespace and claim_ns != namespace:
            continue
        items.append(_resource('PersistentVolume', obj, status=getattr(status_data, 'phase', '') or 'Unknown', status_display=getattr(status_data, 'phase', '') or 'Unknown', summary=f'{capacity or "-"} / {claim_ns or "-"}', detail={'capacity': capacity, 'claim_namespace': claim_ns, 'claim_name': getattr(claim, 'name', '') or '', 'storage_class_name': getattr(spec, 'storage_class_name', '') or ''}))

    for obj in storage_api.list_storage_class(_request_timeout=RESOURCE_TIMEOUT).items:
        items.append(_resource('StorageClass', obj, status='Configured', status_display='已配置', summary=getattr(obj, 'provisioner', '') or '-', detail={'provisioner': getattr(obj, 'provisioner', '') or '', 'reclaim_policy': getattr(obj, 'reclaim_policy', '') or ''}))
    return _filter(items, namespace=namespace, keyword=filters.get('q', ''), status=filters.get('status', ''))


def list_events(cluster, namespace='', filters=None):
    """List Kubernetes Events with Warning highlighted by the frontend."""
    filters = filters or {}
    api = get_core_v1_api(cluster)
    response = api.list_namespaced_event(namespace, _request_timeout=RESOURCE_TIMEOUT) if namespace else api.list_event_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT)
    items = []
    for event in response.items:
        involved = getattr(event, 'involved_object', None)
        event_type = getattr(event, 'type', '') or 'Normal'
        object_kind = getattr(involved, 'kind', '') or ''
        object_name = getattr(involved, 'name', '') or ''
        if filters.get('object_kind') and object_kind != filters['object_kind']:
            continue
        items.append(_resource(
            'Event',
            event,
            status=event_type,
            status_display=event_type,
            summary=getattr(event, 'message', '') or '',
            detail={
                'reason': getattr(event, 'reason', '') or '',
                'message': getattr(event, 'message', '') or '',
                'count': getattr(event, 'count', 0) or 0,
                'object_kind': object_kind,
                'object_name': object_name,
                'last_timestamp': _dt(getattr(event, 'last_timestamp', None)),
            },
        ))
    return _filter(items, namespace=namespace, keyword=filters.get('q', ''), status=filters.get('status', ''))


def attach_related_events(cluster, items, namespace=''):
    """Attach recent events to resources for the read-only detail drawer."""
    try:
        events = list_events(cluster, namespace=namespace)
    except Exception:
        events = []
    events_by_ref = {}
    for event in events:
        detail = event.get('detail') or {}
        key = (event.get('namespace') or '', detail.get('object_kind') or '', detail.get('object_name') or '')
        events_by_ref.setdefault(key, []).append({
            'type': event.get('status') or '',
            'reason': detail.get('reason') or '',
            'message': detail.get('message') or '',
            'count': detail.get('count') or 0,
            'last_timestamp': detail.get('last_timestamp') or '',
        })
    for item in items:
        detail = item.setdefault('detail', {})
        key = (item.get('namespace') or '', item.get('kind') or '', item.get('name') or '')
        detail['events'] = events_by_ref.get(key, [])[:5]
    return items


def list_namespace_options(cluster):
    """Return namespace options for frontend filters without resource counters."""
    namespaces = list_namespaces(cluster)
    return {
        'cluster': {'id': cluster.id, 'name': cluster.name, 'status': cluster.status},
        'namespaces': [
            {
                'name': item['name'],
                'status': item['status'],
            }
            for item in namespaces
        ],
    }
