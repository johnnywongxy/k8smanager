"""总览页的集群取数缓存。

总览页唯一慢的一步是「连每个活跃集群打 7 个只读 API」；其余部分（集群列表、操作趋势、
告警分级）都是毫秒级数据库查询。这里把前者的结果按 15 分钟有效期缓存进
`k8s_resource.models.K8sDashboardSnapshot`，让 15 分钟内除第一次以外的请求全部只读库。

缓存边界切在「连集群取数」这一步，而不是整个响应体。响应是逐用户过滤的（集群可见范围走组织
隔离，集群内计数走 namespace 授权），整体缓存会把第一个访问者的权限固化给所有人 —— 管理员先刷，
受限用户就看到完整的数字；受限用户先刷，管理员反而看到偏少的数字。这类间歇性、随机的错误比
单纯的越权更难排查。而集群 fan-out 本身不依赖请求者（集群凭据来自集群记录），所以它可以安全地
全平台共用；读路径仍按 `scoped_queryset` 取出该用户可见的集群子集，再从缓存里挑对应的计数汇总。

已知情并接受的副作用：同一个可见集群内，计数包含该用户无权查看的 namespace 里的资源。
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import timedelta
from threading import Lock
from time import monotonic

from django.utils import timezone

from k8s_cluster.models import K8sCluster

from ..models import K8sDashboardSnapshot
from . import resource_reader
from .errors import translate_k8s_error
from .namespace_authz import filter_items_for_user


# 缓存作用域。当前只有全局一份，留着这个字段是为了给「按组织/按集群分别缓存」留位置。
CACHE_SCOPE = 'global'

# 有效期：15 分钟内不重复连集群。用户口径是「每 15 分钟请求一次集群，其余的加载表里面的数据」。
CACHE_TTL_MINUTES = 15
CACHE_TTL = timedelta(minutes=CACHE_TTL_MINUTES)

# 不参与实时取数的集群状态：它们本来就连不上或已被管理员停用，计数只会白等一条超时。
INACTIVE_CLUSTER_STATUSES = {'disabled', 'unavailable'}

# 状态为空时按「未知」归类，避免分布里出现空名字的扇区。
UNKNOWN_STATUS = 'Unknown'

# 进程内互斥。缓存过期的那一瞬间可能有多个请求同时进来，没有这把锁它们会各自 fan-out 一遍
# （N 个用户 × 每集群 7 个 API）。锁内会再读一次表，所以只有第一个真的去刷新，其余等它写完直接读。
# 多进程部署时每个进程各刷一次，量级仍然可接受。
_REFRESH_LOCK = Lock()


def _realtime_cluster_counts(cluster, user, apply_namespace_filter=True):
    """读取单个集群的实时资源计数与分布，供总览页跨集群汇总。

    参数：`cluster` 表示待读取的 K8s 集群；`user` 表示发起请求的平台用户，仅在
          `apply_namespace_filter` 为真时参与过滤；`apply_namespace_filter` 表示是否按
          namespace 授权过滤后再统计。
    返回：返回 namespace/pod/workload/network/config/storage 计数、异常 Pod 数、
          Warning 事件数、`cluster_id`，以及 pod_status 与 workload_kind 两个
          `{名称: 数量}` 分布。
    副作用：向目标集群的 Kubernetes API 发起只读查询。

    取数口径与 `K8sResourceListView.get` 保持一致：先按 namespace 授权过滤再统计，
    否则总览会把用户无权查看的命名空间里的资源也计入合计。

    `apply_namespace_filter=False` 供缓存刷新使用：缓存是与请求者无关的全平台共用产物，
    必须在写入侧取下不依赖用户的未过滤口径，过滤改在读取侧按可见集群子集进行（见
    `dashboard_aggregates`）。这里用显式开关而不是传 `user=None`，是因为
    `namespace_authz._policy_matches_subject` 会直接访问 `user.pk`，传 None 会崩。
    """
    def visible(items, resource_type):
        """按调用方选定的口径返回资源条目：需要过滤时走 namespace 授权，否则原样返回。"""
        if not apply_namespace_filter:
            return items
        return filter_items_for_user(items, user, cluster, resource_type=resource_type)

    namespaces = visible(resource_reader.list_namespaces(cluster), 'namespaces')
    pods = visible(resource_reader.list_pods(cluster), 'pods')
    workloads = visible(resource_reader.list_workloads(cluster), 'workloads')
    network = visible(resource_reader.list_network(cluster), 'network')
    configs = visible(resource_reader.list_configs(cluster), 'configs')
    storage = visible(resource_reader.list_storage(cluster), 'storage')
    events = visible(resource_reader.list_events(cluster), 'events')

    pod_status = {}
    workload_kind = {}
    for item in pods:
        status = item.get('status') or UNKNOWN_STATUS
        pod_status[status] = pod_status.get(status, 0) + 1
    for item in workloads:
        kind = item.get('kind') or 'Workload'
        workload_kind[kind] = workload_kind.get(kind, 0) + 1

    return {
        'cluster_id': cluster.id,
        'namespace_count': len(namespaces),
        'pod_count': len(pods),
        'workload_count': len(workloads),
        'network_count': len(network),
        'config_count': len(configs),
        'storage_count': len(storage),
        'abnormal_pod_count': len([item for item in pods if item.get('status') not in {'Running', 'Succeeded'}]),
        'warning_event_count': len([item for item in events if item.get('status') == 'Warning']),
        'pod_status': pod_status,
        'workload_kind': workload_kind,
    }


def _collect_cluster_counts(clusters, user, timeout, max_workers, apply_namespace_filter=True):
    """并发读取多个集群的实时计数，失败的集群降级为错误条目。

    参数：`clusters` 表示待读取的集群列表；`user` 表示发起请求的平台用户；
          `timeout` 表示全部集群共用的等待上限（秒）；`max_workers` 表示并发线程上限；
          `apply_namespace_filter` 表示是否按 namespace 授权过滤后再统计。
    返回：返回 `(aggregates, errors)` 二元组 —— `aggregates` 是成功集群的计数列表，
          `errors` 是失败集群的 `{'cluster', 'cluster_id', 'message'}` 列表。
    副作用：向多个集群的 Kubernetes API 发起只读查询。

    单个集群挂住时，串行取数会把首屏拖到 N 倍超时，因此这里并发取且共用一条截止线：
    逐个 future 只领取「离截止线还剩多少」，避免每个 future 各自重新等一个完整超时。

    错误条目带上 `cluster_id`：缓存是全平台共用的，读路径要按请求者可见的集群集合
    把这些条目筛一遍，否则会把别的组织的集群名带进 `partial_errors`。
    """
    if not clusters:
        return [], []

    aggregates = []
    errors = []
    deadline = monotonic() + timeout
    executor = ThreadPoolExecutor(max_workers=min(max_workers, len(clusters)))
    try:
        futures = {
            executor.submit(_realtime_cluster_counts, cluster, user, apply_namespace_filter): cluster
            for cluster in clusters
        }
        for future, cluster in futures.items():
            remaining = deadline - monotonic()
            if remaining <= 0:
                errors.append({'cluster': cluster.name, 'cluster_id': cluster.id,
                               'message': f'查询超时（超过 {timeout} 秒），数据未计入'})
                continue
            try:
                aggregates.append(future.result(timeout=remaining))
            except FuturesTimeoutError:
                errors.append({'cluster': cluster.name, 'cluster_id': cluster.id,
                               'message': f'查询超时（超过 {timeout} 秒），数据未计入'})
            except Exception as exc:
                errors.append({'cluster': cluster.name, 'cluster_id': cluster.id,
                               'message': translate_k8s_error(exc)[:180]})
    finally:
        # 不等未完成的取数线程：挂住的集群会让 wait=True 一直阻塞到 TCP 超时，
        # 比这个接口自己的截止线还长。未完成的线程在后台自然结束。
        executor.shutdown(wait=False)
    return aggregates, errors


def _is_fresh(snapshot):
    """判断这份缓存是否还在有效期内。"""
    if snapshot is None:
        return False
    return timezone.now() - snapshot.generated_at < CACHE_TTL


def _read_snapshot():
    """读取全局缓存行；还没有任何缓存时返回 None。"""
    return K8sDashboardSnapshot.objects.filter(scope=CACHE_SCOPE).first()


def _load_or_refresh(timeout, max_workers):
    """返回一份可用的缓存 payload，必要时先刷新一次。"""
    snapshot = _read_snapshot()
    if _is_fresh(snapshot):
        return snapshot.payload or {}
    with _REFRESH_LOCK:
        # 锁内重读：等锁期间别人可能刚刷完，那就直接用，不必再来一次。
        snapshot = _read_snapshot()
        if _is_fresh(snapshot):
            return snapshot.payload or {}
        return _refresh(timeout=timeout, max_workers=max_workers)


def _refresh(timeout, max_workers):
    """重新连一次全部活跃集群，写进缓存表并返回这份 payload。

    取的是**跨全部组织**的活跃集群，且 `apply_namespace_filter=False` —— 这份数据与
    请求者无关，可见范围的收窄发生在读路径上（见 `dashboard_aggregates`）。
    """
    clusters = list(
        K8sCluster.objects.exclude(status__in=INACTIVE_CLUSTER_STATUSES).order_by('name')
    )
    aggregates, errors = _collect_cluster_counts(
        clusters, None, timeout, max_workers, apply_namespace_filter=False,
    )
    payload = {
        # 存字符串而不是 datetime：JSONField 的原生类型有限，而响应里的 `generated_at`
        # 本来就被 `_safe_value` 格式化成这个形状，直接透传即可。
        'generated_at': timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S'),
        'clusters': {str(item['cluster_id']): item for item in aggregates},
        'errors': errors,
    }
    # scope 唯一，所以这里是「一行覆盖一行」。两个请求同时刷也只是后写的覆盖先写的，
    # 两份数据同样新鲜，无害。
    K8sDashboardSnapshot.objects.update_or_create(
        scope=CACHE_SCOPE,
        defaults={'payload': payload, 'generated_at': timezone.now()},
    )
    return payload


def dashboard_aggregates(clusters, timeout, max_workers):
    """按可见集群列表取实时计数：优先用 15 分钟内的缓存，过期或缺失时刷新一次。

    参数：`clusters` 表示**该请求者可见的活跃集群**列表；`timeout` 表示全部集群共用的
          取数等待上限（秒）；`max_workers` 表示并发线程上限。
    返回：返回 `(aggregates, errors, generated_at)` —— `aggregates` 是按入参顺序排列的
          各集群计数，`errors` 是 `{'cluster', 'message'}` 列表，`generated_at` 是这批
          数据取自哪个时刻（可能是十几分钟前，不要当成本次请求的时间）。
    副作用：缓存过期或缺失时向全部活跃集群的 Kubernetes API 发起只读查询，并写缓存表。

    传入的 `clusters` 必须已经过组织隔离（视图侧走 `scoped_queryset`）：缓存里装的是跨全部
    组织的集群，只有在这里按可见集合过滤一次，别的组织的集群才不会被读出来。
    """
    payload = _load_or_refresh(timeout, max_workers)
    cached = payload.get('clusters') or {}
    visible_ids = {cluster.id for cluster in clusters}

    aggregates = []
    errors = []
    for cluster in clusters:
        item = cached.get(str(cluster.id))
        if item is None:
            # 缓存是上一轮生成的，而该集群是之后才接入或恢复的：宁可明说它没算进来，
            # 也不能让它静默显示成 0 —— 0 会被读成「这个集群是空的」。
            errors.append({
                'cluster': cluster.name,
                'message': f'实时数据尚未缓存（每 {CACHE_TTL_MINUTES} 分钟自动取一次），本次未计入',
            })
            continue
        aggregates.append(item)
    for item in payload.get('errors') or []:
        if item.get('cluster_id') in visible_ids:
            errors.append({'cluster': item['cluster'], 'message': item['message']})
    return aggregates, errors, payload.get('generated_at')
