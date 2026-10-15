from collections import Counter, defaultdict

from django.utils import timezone

from k8s_resource.services import resource_reader
from k8s_resource.services.client_factory import get_core_v1_api


RESOURCE_TIMEOUT = 10


def _resource_quantity_to_cpu_millicores(value):
    text = str(value or '0').strip()
    if not text:
        return 0
    if text.endswith('n'):
        return int(float(text[:-1]) / 1_000_000)
    if text.endswith('u'):
        return int(float(text[:-1]) / 1000)
    if text.endswith('m'):
        return int(float(text[:-1]))
    return int(float(text) * 1000)


def _resource_quantity_to_bytes(value):
    text = str(value or '0').strip()
    if not text:
        return 0
    units = {
        'Ki': 1024,
        'Mi': 1024 ** 2,
        'Gi': 1024 ** 3,
        'Ti': 1024 ** 4,
        'K': 1000,
        'M': 1000 ** 2,
        'G': 1000 ** 3,
        'T': 1000 ** 4,
    }
    for suffix, factor in units.items():
        if text.endswith(suffix):
            return int(float(text[:-len(suffix)]) * factor)
    return int(float(text))


def _bytes_to_mib(value):
    return round((value or 0) / 1024 / 1024, 2)


def _quantity_to_plain(value):
    if value is None:
        return ''
    return str(value)


def _filter_log_lines(lines, keyword=''):
    if not keyword:
        return lines
    lowered = keyword.lower()
    return [line for line in lines if lowered in line.lower()]


def _merge_container_logs(api, namespace, pod, containers, keyword='', tail_lines=200, since_seconds=None, previous=False):
    merged = []
    for container_name in containers:
        try:
            text = api.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container_name,
                tail_lines=tail_lines,
                since_seconds=since_seconds,
                timestamps=True,
                previous=previous,
                _request_timeout=RESOURCE_TIMEOUT,
            ) or ''
        except Exception as exc:
            text = f'{timezone.localtime().isoformat()} [{container_name}] 日志读取失败：{exc}'
        for line in _filter_log_lines(text.splitlines(), keyword):
            merged.append({'sort_key': line[:35], 'line': f'[{container_name}] {line}'})
    return [item['line'] for item in sorted(merged, key=lambda item: item['sort_key'])]


def query_pod_logs(cluster, namespace, pod, container='', keyword='', tail_lines=200, since_seconds='', previous=False):
    """Read realtime Pod logs through Kubernetes API and filter locally by keyword."""
    if not namespace:
        raise ValueError('命名空间不能为空')
    if not pod:
        raise ValueError('Pod 不能为空')
    try:
        tail_lines = min(max(int(tail_lines or 200), 1), 2000)
    except (TypeError, ValueError):
        tail_lines = 200
    since_value = None
    if since_seconds:
        try:
            since_value = max(int(since_seconds), 1)
        except (TypeError, ValueError):
            since_value = None

    api = get_core_v1_api(cluster)
    if container == '__all__':
        pod_obj = api.read_namespaced_pod(name=pod, namespace=namespace, _request_timeout=RESOURCE_TIMEOUT)
        containers = [getattr(item, 'name', '') for item in (getattr(getattr(pod_obj, 'spec', None), 'containers', None) or []) if getattr(item, 'name', '')]
        lines = _merge_container_logs(api, namespace, pod, containers, keyword=keyword, tail_lines=tail_lines, since_seconds=since_value, previous=previous)
    else:
        text = api.read_namespaced_pod_log(
            name=pod,
            namespace=namespace,
            container=container or None,
            tail_lines=tail_lines,
            since_seconds=since_value,
            timestamps=True,
            previous=previous,
            _request_timeout=RESOURCE_TIMEOUT,
        ) or ''
        lines = _filter_log_lines(text.splitlines(), keyword)
    return {
        'cluster': {'id': cluster.id, 'name': cluster.name, 'status': cluster.status},
        'namespace': namespace,
        'pod': pod,
        'container': container,
        'previous': bool(previous),
        'keyword': keyword,
        'tail_lines': tail_lines,
        'line_count': len(lines),
        'logs': '\n'.join(lines),
        'queried_at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
        'source_status': {'source_type': 'kubernetes', 'status': 'available', 'message': '实时日志来自 Kubernetes API'},
    }


def _container_request_limit_totals(containers):
    totals = {'cpu_request_m': 0, 'cpu_limit_m': 0, 'memory_request_mib': 0, 'memory_limit_mib': 0}
    for container in containers or []:
        resources = getattr(container, 'resources', None)
        requests_map = getattr(resources, 'requests', None) or {}
        limits_map = getattr(resources, 'limits', None) or {}
        totals['cpu_request_m'] += _resource_quantity_to_cpu_millicores(requests_map.get('cpu', '0'))
        totals['cpu_limit_m'] += _resource_quantity_to_cpu_millicores(limits_map.get('cpu', '0'))
        totals['memory_request_mib'] += _bytes_to_mib(_resource_quantity_to_bytes(requests_map.get('memory', '0')))
        totals['memory_limit_mib'] += _bytes_to_mib(_resource_quantity_to_bytes(limits_map.get('memory', '0')))
    return totals


def resource_statistics(cluster):
    """Aggregate real Kubernetes API objects for the statistics page."""
    core_api = get_core_v1_api(cluster)
    namespaces = resource_reader.list_namespaces(cluster)
    nodes = resource_reader.list_nodes(cluster)
    pods_payload = core_api.list_pod_for_all_namespaces(_request_timeout=RESOURCE_TIMEOUT).items
    workloads = resource_reader.list_workloads(cluster)
    network = resource_reader.list_network(cluster)
    configs = resource_reader.list_configs(cluster)
    storage = resource_reader.list_storage(cluster)
    events = resource_reader.list_events(cluster)

    namespace_stats = defaultdict(lambda: {
        'name': '',
        'pod_count': 0,
        'abnormal_pod_count': 0,
        'cpu_request_m': 0,
        'cpu_limit_m': 0,
        'memory_request_mib': 0,
        'memory_limit_mib': 0,
    })
    pod_status = Counter()
    restart_risks = []
    resource_risks = []
    image_usage = Counter()
    image_namespaces = defaultdict(set)
    for pod in pods_payload:
        metadata = getattr(pod, 'metadata', None)
        status = getattr(getattr(pod, 'status', None), 'phase', '') or 'Unknown'
        namespace = getattr(metadata, 'namespace', '') or 'default'
        namespace_stats[namespace]['name'] = namespace
        namespace_stats[namespace]['pod_count'] += 1
        if status not in {'Running', 'Succeeded'}:
            namespace_stats[namespace]['abnormal_pod_count'] += 1
        pod_status[status] += 1
        containers = getattr(getattr(pod, 'spec', None), 'containers', None) or []
        totals = _container_request_limit_totals(containers)
        container_statuses = getattr(getattr(pod, 'status', None), 'container_statuses', None) or []
        restart_count = sum(getattr(item, 'restart_count', 0) or 0 for item in container_statuses)
        if restart_count >= 3:
            restart_risks.append({'namespace': namespace, 'pod': getattr(metadata, 'name', '') or '', 'restart_count': restart_count, 'status': status})
        if not totals['cpu_request_m'] or not totals['memory_request_mib'] or not totals['cpu_limit_m'] or not totals['memory_limit_mib']:
            resource_risks.append({
                'namespace': namespace,
                'pod': getattr(metadata, 'name', '') or '',
                'missing_cpu_request': not bool(totals['cpu_request_m']),
                'missing_memory_request': not bool(totals['memory_request_mib']),
                'missing_cpu_limit': not bool(totals['cpu_limit_m']),
                'missing_memory_limit': not bool(totals['memory_limit_mib']),
            })
        for key, value in totals.items():
            namespace_stats[namespace][key] += value
        for container in containers:
            image = getattr(container, 'image', '') or ''
            if image:
                image_usage[image] += 1
                image_namespaces[image].add(namespace)

    resource_type = Counter()
    for name, items in [
        ('Namespace', namespaces),
        ('Node', nodes),
        ('Pod', pods_payload),
        ('Workload', workloads),
        ('Network', network),
        ('Config/Secret', configs),
        ('Storage', storage),
        ('Event', events),
    ]:
        resource_type[name] = len(items)

    workload_kind = Counter(item.get('kind') or 'Workload' for item in workloads)
    warning_count = sum(1 for item in events if item.get('status') == 'Warning')
    warning_reasons = Counter((item.get('detail') or {}).get('reason') or 'Unknown' for item in events if item.get('status') == 'Warning')
    image_pull_events = [
        {
            'namespace': item.get('namespace') or '',
            'object_kind': (item.get('detail') or {}).get('object_kind') or '',
            'object_name': (item.get('detail') or {}).get('object_name') or '',
            'reason': (item.get('detail') or {}).get('reason') or '',
            'message': item.get('summary') or '',
        }
        for item in events
        if ((item.get('detail') or {}).get('reason') or '') in {'Failed', 'ErrImagePull', 'ImagePullBackOff', 'BackOff'}
    ][:10]
    risk_summary = {
        'pods_without_cpu_request': sum(1 for item in resource_risks if item['missing_cpu_request']),
        'pods_without_memory_request': sum(1 for item in resource_risks if item['missing_memory_request']),
        'pods_without_cpu_limit': sum(1 for item in resource_risks if item['missing_cpu_limit']),
        'pods_without_memory_limit': sum(1 for item in resource_risks if item['missing_memory_limit']),
        'high_restart_pods': len(restart_risks),
        'image_pull_events': len(image_pull_events),
    }
    return {
        'cluster': {'id': cluster.id, 'name': cluster.name, 'status': cluster.status},
        'summary': {
            'namespace_count': len(namespaces),
            'node_count': len(nodes),
            'pod_count': len(pods_payload),
            'workload_count': len(workloads),
            'network_count': len(network),
            'config_count': len(configs),
            'storage_count': len(storage),
            'event_count': len(events),
            'warning_event_count': warning_count,
        },
        'namespace_stats': sorted(namespace_stats.values(), key=lambda item: item['pod_count'], reverse=True),
        'resource_type_distribution': [{'name': key, 'value': value} for key, value in resource_type.items() if value],
        'pod_status_distribution': [{'name': key, 'value': value} for key, value in sorted(pod_status.items())],
        'workload_kind_distribution': [{'name': key, 'value': value} for key, value in sorted(workload_kind.items())],
        'image_usage': [
            {'image': image, 'count': count, 'namespaces': sorted(image_namespaces[image])}
            for image, count in image_usage.most_common(20)
        ],
        'risk_summary': risk_summary,
        'restart_risks': sorted(restart_risks, key=lambda item: item['restart_count'], reverse=True)[:10],
        'resource_risks': resource_risks[:20],
        'warning_reason_distribution': [{'name': key, 'value': value} for key, value in warning_reasons.most_common(10)],
        'image_pull_events': image_pull_events,
        'request_limit_by_namespace': [
            {
                **item,
                'memory_request_mib': round(item['memory_request_mib'], 2),
                'memory_limit_mib': round(item['memory_limit_mib'], 2),
            }
            for item in sorted(namespace_stats.values(), key=lambda row: row['cpu_request_m'], reverse=True)
        ],
        'generated_at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
    }
