from datetime import timedelta

from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from k8s_audit.models import K8sOperationAudit, K8sSecretAccessAudit
from k8s_cluster.models import K8sCluster
from k8s_cluster.services import cluster_certificate_summary, select_preferred_cluster
from k8s_cluster.views import write_k8s_audit
from ops.permission_catalog import action_code
from ops.permission_service import has_permission, is_permission_admin
from ops.permissions import user_organization
from ops.tenancy import assign_organization, scoped_queryset

from .models import K8sNamespacePolicy, K8sResourceSnapshot
from .serializers import K8sNamespacePolicySerializer, K8sResourceSnapshotSerializer
from .services import dashboard_cache
from .services import resource_reader
from .services import resource_actions
from .services.dashboard_cache import INACTIVE_CLUSTER_STATUSES
from .services.errors import translate_k8s_error
from .services.namespace_authz import allowed_namespaces, assert_namespace_permission, filter_items_for_user
from .services.resource_mutator import create_resource, delete_resource, patch_resource_metadata, write_resource_audit


PAGE_SIZE = 10


RESOURCE_READERS = {
    'namespaces': resource_reader.list_namespaces,
    'nodes': resource_reader.list_nodes,
    'images': resource_reader.list_images,
    'pods': resource_reader.list_pods,
    'workloads': resource_reader.list_workloads,
    'network': resource_reader.list_network,
    'configs': resource_reader.list_configs,
    'storage': resource_reader.list_storage,
    'events': resource_reader.list_events,
}

RESOURCE_CREATABLE_KINDS = {
    'namespaces': {'Namespace'},
    'network': {'Service', 'Ingress'},
    'configs': {'ConfigMap', 'Secret'},
    'storage': {'PersistentVolumeClaim', 'StorageClass'},
}

EVENT_ATTACH_RESOURCES = {'pods', 'workloads', 'network', 'configs', 'storage'}

K8S_ACTION_PERMISSION_PAGES = {
    'cordon': 'page.k8s.nodes.view',
    'uncordon': 'page.k8s.nodes.view',
    'drain': 'page.k8s.nodes.view',
    'scale': 'page.k8s.workloads.view',
    'restart': 'page.k8s.workloads.view',
    'trigger': 'page.k8s.workloads.view',
    'suspend': 'page.k8s.workloads.view',
    'reveal': 'page.k8s.configs.view',
    'delete': '',
}

KIND_PERMISSION_PAGES = {
    'Namespace': 'page.k8s.namespaces.view',
    'Node': 'page.k8s.nodes.view',
    'Image': 'page.k8s.images.view',
    'Pod': 'page.k8s.pods.view',
    'Deployment': 'page.k8s.workloads.view',
    'StatefulSet': 'page.k8s.workloads.view',
    'DaemonSet': 'page.k8s.workloads.view',
    'Job': 'page.k8s.workloads.view',
    'CronJob': 'page.k8s.workloads.view',
    'HorizontalPodAutoscaler': 'page.k8s.workloads.view',
    'PodDisruptionBudget': 'page.k8s.workloads.view',
    'Service': 'page.k8s.network.view',
    'Ingress': 'page.k8s.network.view',
    'ConfigMap': 'page.k8s.configs.view',
    'Secret': 'page.k8s.configs.view',
    'ServiceAccount': 'page.k8s.configs.view',
    'Role': 'page.k8s.configs.view',
    'RoleBinding': 'page.k8s.configs.view',
    'ClusterRole': 'page.k8s.configs.view',
    'ClusterRoleBinding': 'page.k8s.configs.view',
    'PersistentVolumeClaim': 'page.k8s.storage.view',
    'PersistentVolume': 'page.k8s.storage.view',
    'StorageClass': 'page.k8s.storage.view',
    'Event': 'page.k8s.events.view',
}


class K8sResourcePagination(PageNumberPagination):
    """K8s resource module pagination: 10 rows per page."""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class K8sNamespacePolicyViewSet(viewsets.ModelViewSet):
    """CRUD for namespace-level K8s business authorization rules."""

    queryset = K8sNamespacePolicy.objects.select_related('organization', 'department', 'cluster').prefetch_related('users').all()
    serializer_class = K8sNamespacePolicySerializer
    pagination_class = K8sResourcePagination

    def get_queryset(self):
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        cluster_id = self.request.query_params.get('cluster', '').strip()
        namespace = self.request.query_params.get('namespace', '').strip()
        status = self.request.query_params.get('status', '').strip()
        keyword = self.request.query_params.get('q', '').strip()
        if cluster_id:
            queryset = queryset.filter(cluster_id=cluster_id)
        if namespace:
            queryset = queryset.filter(namespace=namespace)
        if status:
            queryset = queryset.filter(status=status)
        if keyword:
            queryset = queryset.filter(name__icontains=keyword)
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save()
        assign_organization(instance, self.request.user)
        instance.save()
        write_k8s_audit(self.request, 'namespace_policy_create', instance.cluster, {'policy_id': instance.pk, 'namespace': instance.namespace})

    def perform_update(self, serializer):
        instance = serializer.save()
        assign_organization(instance, self.request.user)
        instance.save()
        write_k8s_audit(self.request, 'namespace_policy_update', instance.cluster, {'policy_id': instance.pk, 'namespace': instance.namespace})

    def perform_destroy(self, instance):
        cluster = instance.cluster
        detail = {'policy_id': instance.pk, 'namespace': instance.namespace}
        instance.delete()
        write_k8s_audit(self.request, 'namespace_policy_delete', cluster, detail)

    @action(detail=False, methods=['get'], url_path='scope')
    def scope(self, request):
        cluster = K8sResourceListView().get_cluster(request)
        namespaces = allowed_namespaces(request.user, cluster)
        return Response({
            'cluster': {'id': cluster.id, 'name': cluster.name, 'status': cluster.status},
            'all_namespaces': namespaces is None,
            'namespaces': namespaces or [],
        })


class K8sResourceSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """Read and refresh cached K8s aggregate snapshots."""

    queryset = K8sResourceSnapshot.objects.select_related('organization', 'cluster').all()
    serializer_class = K8sResourceSnapshotSerializer
    pagination_class = K8sResourcePagination

    def get_queryset(self):
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        cluster_id = self.request.query_params.get('cluster', '').strip()
        if cluster_id:
            queryset = queryset.filter(cluster_id=cluster_id)
        return queryset

    @action(detail=False, methods=['post'])
    def refresh(self, request):
        from k8s_observability.services import resource_statistics
        cluster = K8sResourceListView().get_cluster(request)
        try:
            payload = resource_statistics(cluster)
            snapshot = K8sResourceSnapshot.objects.create(
                organization=user_organization(request.user),
                cluster=cluster,
                snapshot_type='statistics',
                payload=payload,
                status='completed',
                message='资源统计快照已刷新',
            )
        except Exception as exc:
            snapshot = K8sResourceSnapshot.objects.create(
                organization=user_organization(request.user),
                cluster=cluster,
                snapshot_type='statistics',
                payload={},
                status='failed',
                message=translate_k8s_error(exc)[:240],
            )
        return Response(self.get_serializer(snapshot).data)


def _format_datetime(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S')


def _safe_value(value):
    if hasattr(value, 'isoformat'):
        return _format_datetime(value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_value(item) for key, item in value.items()}
    return value


def _page_number(request):
    try:
        return max(1, int(request.query_params.get('page', '1')))
    except ValueError:
        return 1


def _paginate(items, page):
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    return {
        'count': len(items),
        'results': [_safe_value(item) for item in items[start:end]],
    }


def _filters(request):
    return {
        'q': request.query_params.get('q', '').strip(),
        'status': request.query_params.get('status', '').strip(),
        'kind': request.query_params.get('kind', '').strip(),
        'object_kind': request.query_params.get('object_kind', '').strip(),
    }


class K8sResourceListView(APIView):
    """Read Kubernetes resources through the real cluster API without persisting live objects."""

    def get_cluster(self, request):
        queryset = scoped_queryset(K8sCluster.objects.all(), request.user)
        cluster_id = request.query_params.get('cluster') or (request.data.get('cluster') if isinstance(request.data, dict) else '')
        if cluster_id:
            cluster = queryset.filter(pk=cluster_id).first()
        else:
            cluster = select_preferred_cluster(queryset)
        if not cluster:
            raise ValueError('未找到可访问的 K8s 集群，请先在集群接入中导入或启用集群')
        if cluster.status == 'disabled':
            raise ValueError('当前集群已禁用，不能查询资源')
        return cluster

    def _namespace_for_read(self, request, cluster, resource):
        namespace = request.query_params.get('namespace', '').strip()
        allowed = allowed_namespaces(request.user, cluster, action='read', resource_type=resource)
        if allowed is not None:
            if namespace and namespace not in allowed:
                raise PermissionError('当前用户没有该 namespace 的 K8s 业务授权')
            if not namespace and len(allowed) == 1:
                namespace = allowed[0]
        return namespace

    def get(self, request, resource):
        reader = RESOURCE_READERS.get(resource)
        if not reader:
            return Response({'detail': '不支持的 K8s 资源类型'}, status=404)
        try:
            cluster = self.get_cluster(request)
            namespace = self._namespace_for_read(request, cluster, resource)
            filters = _filters(request)
            if resource in {'pods', 'workloads', 'network', 'configs', 'storage', 'events'}:
                items = reader(cluster, namespace=namespace, filters=filters)
            else:
                items = reader(cluster, filters=filters)
            items = filter_items_for_user(items, request.user, cluster, action='read', resource_type=resource)
            if resource in EVENT_ATTACH_RESOURCES:
                items = resource_reader.attach_related_events(cluster, items, namespace=namespace)
            return Response({
                **_paginate(items, _page_number(request)),
                'cluster': {'id': cluster.id, 'name': cluster.name, 'status': cluster.status},
            })
        except Exception as exc:
            return Response({'detail': f'查询 K8s 资源失败：{translate_k8s_error(exc)}'}, status=400)

    def post(self, request, resource):
        try:
            cluster = self.get_cluster(request)
            kind = request.data.get('kind', '').strip() if isinstance(request.data, dict) else ''
            if not kind:
                raise ValueError('缺少资源类型')
            if kind not in RESOURCE_CREATABLE_KINDS.get(resource, set()):
                raise ValueError(f'{resource} 页面不支持新增 {kind}')
            payload = dict(request.data)
            assert_namespace_permission(request.user, cluster, namespace=payload.get('namespace', ''), resource_type=resource, action='create')
            _created, body = create_resource(cluster, kind, payload)
            item = {'kind': kind, 'name': payload.get('name', ''), 'namespace': payload.get('namespace', '')}
            write_resource_audit(request, cluster, item, 'create', detail={'body': body})
            return Response({'detail': '资源已创建'})
        except Exception as exc:
            return Response({'detail': f'创建 K8s 资源失败：{translate_k8s_error(exc)}'}, status=400)


class K8sResourceItemView(K8sResourceListView):
    """Mutate a single Kubernetes resource selected from the read-only list payload."""

    def get_item(self, request, resource):
        reader = RESOURCE_READERS.get(resource)
        if not reader:
            raise ValueError('不支持的 K8s 资源类型')
        cluster = self.get_cluster(request)
        kind = request.query_params.get('kind', '').strip()
        name = request.query_params.get('name', '').strip()
        namespace = request.query_params.get('namespace', '').strip()
        uid = request.query_params.get('uid', '').strip()
        if not kind or not name:
            raise ValueError('缺少 kind 或 name')
        assert_namespace_permission(request.user, cluster, namespace=namespace, resource_type=resource, action='read')
        filters = {'q': '', 'status': '', 'kind': kind}
        if resource in {'pods', 'workloads', 'network', 'configs', 'storage', 'events'}:
            items = reader(cluster, namespace=namespace, filters=filters)
        else:
            items = reader(cluster, filters=filters)
        for item in items:
            if item.get('kind') == kind and item.get('name') == name and (not namespace or item.get('namespace') == namespace) and (not uid or item.get('uid') == uid):
                return cluster, item
        raise ValueError('资源不存在或当前用户不可访问')

    def patch(self, request, resource):
        try:
            cluster, item = self.get_item(request, resource)
            assert_namespace_permission(request.user, cluster, namespace=item.get('namespace', ''), resource_type=resource, action='update')
            labels = request.data.get('labels') if isinstance(request.data, dict) else {}
            annotations = request.data.get('annotations') if isinstance(request.data, dict) else {}
            update_data = isinstance(request.data, dict) and 'data' in request.data and item.get('kind') in {'ConfigMap', 'Secret'}
            data = request.data.get('data') if update_data else None
            patch_resource_metadata(cluster, item, labels or {}, annotations or {}, data=data or {}, update_data=update_data)
            detail = {'labels': labels or {}, 'annotations': annotations or {}}
            if update_data:
                detail['data_keys'] = sorted((data or {}).keys())
            write_resource_audit(request, cluster, item, 'update_metadata', detail=detail)
            return Response({'detail': '资源已更新'})
        except Exception as exc:
            try:
                cluster, item = self.get_item(request, resource)
                write_resource_audit(request, cluster, item, 'update_metadata', status='failed', failure_reason=translate_k8s_error(exc))
            except Exception:
                pass
            return Response({'detail': f'更新 K8s 资源失败：{translate_k8s_error(exc)}'}, status=400)

    def delete(self, request, resource):
        try:
            cluster, item = self.get_item(request, resource)
            assert_namespace_permission(request.user, cluster, namespace=item.get('namespace', ''), resource_type=resource, action='delete')
            force = str(request.query_params.get('force', '')).lower() in {'1', 'true', 'yes'}
            delete_resource(cluster, item, force=force)
            write_resource_audit(request, cluster, item, 'delete', detail={'force': force} if force else None)
            return Response({'detail': '资源已删除'})
        except Exception as exc:
            try:
                cluster, item = self.get_item(request, resource)
                write_resource_audit(request, cluster, item, 'delete', status='failed', failure_reason=translate_k8s_error(exc))
            except Exception:
                pass
            return Response({'detail': f'删除 K8s 资源失败：{translate_k8s_error(exc)}'}, status=400)


class K8sResourceActionView(K8sResourceListView):
    """Execute third-phase operational actions against Kubernetes resources."""

    action_name = ''

    def _payload_item(self, request):
        return {
            'kind': request.data.get('kind', '').strip(),
            'name': request.data.get('name', '').strip(),
            'namespace': request.data.get('namespace', '').strip(),
            'uid': request.data.get('uid', '').strip(),
        }

    def _audit_failure(self, request, cluster, item, action, exc):
        write_resource_audit(request, cluster, item, action, status='failed', failure_reason=translate_k8s_error(exc))

    def _assert_action_permission(self, request, action, item=None):
        if is_permission_admin(request.user):
            return
        item = item or {}
        kind = item.get('kind', '')
        page_code = KIND_PERMISSION_PAGES.get(kind) or K8S_ACTION_PERMISSION_PAGES.get(action, '')
        if action == 'batch':
            page_code = KIND_PERMISSION_PAGES.get(kind, '')
        if not page_code:
            raise PermissionError('无法识别该 K8s 动作对应的页面权限')
        code = action_code(page_code, action)
        if not has_permission(request.user, code):
            raise PermissionError(f'没有该操作权限：{code}')

    def post(self, request, action_name=''):
        cluster = None
        item = {}
        action = action_name or self.action_name
        try:
            cluster = self.get_cluster(request)
            if action == 'batch':
                items = request.data.get('items') if isinstance(request.data, dict) else []
                batch_action = request.data.get('batch_action', '').strip() if isinstance(request.data, dict) else ''
                if batch_action not in {'delete', 'restart'}:
                    raise ValueError('批量操作只支持 delete 或 restart')
                results = []
                for raw in items or []:
                    item = {
                        'kind': raw.get('kind', '').strip(),
                        'name': raw.get('name', '').strip(),
                        'namespace': raw.get('namespace', '').strip(),
                        'uid': raw.get('uid', '').strip(),
                    }
                    try:
                        self._assert_action_permission(request, batch_action, item)
                        assert_namespace_permission(request.user, cluster, namespace=item.get('namespace', ''), resource_type=item.get('kind') or 'batch', action=batch_action)
                        if batch_action == 'delete':
                            delete_resource(cluster, item)
                        else:
                            resource_actions.restart_workload(cluster, item)
                        write_resource_audit(request, cluster, item, f'batch_{batch_action}', detail={'batch_action': batch_action})
                        results.append({**item, 'status': 'completed'})
                    except Exception as item_exc:
                        write_resource_audit(request, cluster, item, f'batch_{batch_action}', status='failed', failure_reason=translate_k8s_error(item_exc))
                        results.append({**item, 'status': 'failed', 'message': translate_k8s_error(item_exc)[:180]})
                return Response({
                    'detail': '批量操作已完成',
                    'results': results,
                    'success_count': len([row for row in results if row['status'] == 'completed']),
                    'failed_count': len([row for row in results if row['status'] == 'failed']),
                })
            item = self._payload_item(request)
            self._assert_action_permission(request, action, item)
            assert_namespace_permission(request.user, cluster, namespace=item.get('namespace', ''), resource_type=item.get('kind') or action, action=action)
            if action == 'cordon':
                item['kind'] = 'Node'
                resource_actions.cordon_node(cluster, item['name'], True)
                detail = {'unschedulable': True}
            elif action == 'uncordon':
                item['kind'] = 'Node'
                resource_actions.cordon_node(cluster, item['name'], False)
                detail = {'unschedulable': False}
            elif action == 'drain':
                item['kind'] = 'Node'
                detail = resource_actions.drain_node(cluster, item['name'])
            elif action == 'scale':
                detail = {'replicas': int(request.data.get('replicas'))}
                resource_actions.scale_workload(cluster, item, detail['replicas'])
            elif action == 'restart':
                detail = resource_actions.restart_workload(cluster, item)
            elif action == 'trigger':
                detail = resource_actions.trigger_cronjob(cluster, item)
            elif action == 'suspend':
                suspend = bool(request.data.get('suspend', True))
                detail = resource_actions.suspend_cronjob(cluster, item, suspend=suspend)
            elif action == 'reveal':
                detail = resource_actions.reveal_secret(cluster, item)
                organization = user_organization(request.user)
                K8sSecretAccessAudit.objects.create(
                    organization=organization,
                    cluster_name=cluster.name,
                    namespace=item.get('namespace', ''),
                    secret_name=item.get('name', ''),
                    access_type='reveal',
                    actor=getattr(request.user, 'username', 'system') or 'system',
                    result='completed',
                    detail={'keys': sorted((detail.get('data') or {}).keys())},
                )
                write_resource_audit(request, cluster, item, 'reveal_secret', detail={'keys': sorted((detail.get('data') or {}).keys())})
                return Response(detail)
            else:
                raise ValueError('不支持的 K8s 动作')
            write_resource_audit(request, cluster, item, action, detail=detail)
            return Response({'detail': '操作已提交', 'result': detail})
        except Exception as exc:
            if cluster:
                self._audit_failure(request, cluster, item or {}, action, exc)
            return Response({'detail': f'执行 K8s 动作失败：{translate_k8s_error(exc)}'}, status=400)


class K8sNamespaceOptionsView(K8sResourceListView):
    """Return namespace options for frontend filters."""

    def get(self, request):
        try:
            cluster = self.get_cluster(request)
            payload = resource_reader.list_namespace_options(cluster)
            allowed = allowed_namespaces(request.user, cluster)
            if allowed is not None:
                payload['namespaces'] = [item for item in payload.get('namespaces', []) if item.get('name') in set(allowed)]
            return Response(_safe_value(payload))
        except Exception as exc:
            return Response({'detail': f'查询 K8s 命名空间失败：{translate_k8s_error(exc)}'}, status=400)


# 总览页的折线图固定看最近 7 天，与用户报表的趋势窗口保持一致。
DASHBOARD_TREND_DAYS = 7

def _merge_counts(distributions):
    """把多个集群的同名计数分布合并成一份。

    参数：`distributions` 表示各集群的 `{名称: 数量}` 字典列表。
    返回：返回按数量降序、同数量按名称升序的 `[{'name', 'value'}]` 列表。
    副作用：不直接修改入参。

    排序里带上名称是为了让两个数量相同的类别每次出场顺序一致，
    否则环形图的图例与配色会随字典顺序抖动。
    """
    merged = {}
    for distribution in distributions:
        for name, value in distribution.items():
            merged[name] = merged.get(name, 0) + value
    return [
        {'name': name, 'value': value}
        for name, value in sorted(merged.items(), key=lambda item: (-item[1], item[0]))
    ]


def _operation_trend(user):
    """统计最近 7 天的 K8s 操作审计趋势。

    参数：`user` 表示发起请求的平台用户，用于组织隔离。
    返回：返回 `[{'date': '09-22', 'value': 3, 'failed': 1}]`，缺失日期补 0。
    副作用：不直接修改持久化数据。

    沿用 `UserReportView` 的手工打点：先按本地日期建满 7 个桶再落桶，
    没有操作的日期也会出现在折线上，不会被折线拉成断点。
    """
    today = timezone.localdate()
    start = today - timedelta(days=DASHBOARD_TREND_DAYS - 1)
    buckets = {
        start + timedelta(days=offset): {'value': 0, 'failed': 0}
        for offset in range(DASHBOARD_TREND_DAYS)
    }
    rows = scoped_queryset(
        K8sOperationAudit.objects.filter(created_at__date__gte=start),
        user,
    ).values_list('created_at', 'status')
    for created_at, status in rows:
        day = timezone.localtime(created_at).date() if timezone.is_aware(created_at) else created_at.date()
        bucket = buckets.get(day)
        if not bucket:
            continue
        bucket['value'] += 1
        if status == 'failed':
            bucket['failed'] += 1
    return [
        {'date': day.strftime('%m-%d'), 'value': bucket['value'], 'failed': bucket['failed']}
        for day, bucket in buckets.items()
    ]


def _certificate_summary(clusters):
    """列出各集群托管凭据里最紧迫的那张证书，供总览页的「证书到期」面板展示。

    参数：`clusters` 表示本次请求可见的集群列表（与其余汇总口径同一份）。
    返回：返回按剩余天数升序排列的条目列表，读不到证书的排在最后。
    副作用：解密各集群的凭据并在本机解析证书；不联网、不修改数据。

    总览页那一格原先是「告警分级」—— 按 `K8sNotification` 的级别数条数。平台并不做告警，
    那一块于是永远只有三个 0 值级别；换成平台**真的在做**的事：证书到期提醒。计算直接复用
    集群接入页那个 `cluster_certificate_summary`，两处给出的剩余天数因此不可能对不上。

    读不到证书的集群（Token 认证、没托管凭据、KubeConfig 坏掉）不是错误：`status` 会是
    ``unknown``，`message` 里带中文原因，页面上照常占一行 —— 用户才知道要去补凭据。
    """
    items = [cluster_certificate_summary(cluster) for cluster in clusters]
    items.sort(key=lambda item: (item['days_left'] is None, item['days_left'] or 0))
    return [
        {
            'cluster_id': item['cluster_id'],
            'cluster_name': item['cluster_name'],
            'status': item['status'],
            'days_left': item['days_left'],
            'message': item['message'],
            'next': item['next'],
        }
        for item in items
    ]


class K8sDashboardView(APIView):
    """Return the cross-cluster aggregate shown by the K8s dashboard page.

    这个接口回的是「不区分集群」的合计与分布 —— 总览页做的是汇总维护，不按集群切分。
    （原先旁边还有一条 `/k8s/resources/multi-cluster-overview/` 回「逐集群一行」的清单，
    它只服务已下线的「集群总览」页，随那一页一起删了。）

    实时取数这一段走 `services.dashboard_cache`：15 分钟内只连一次集群，其余请求直接读缓存表。
    因此本视图只负责「按组织隔离取出该用户可见的集群」与「汇总」，取数的时机由缓存层决定。
    """

    # 全部集群共用的实时取数截止线（秒）。
    CLUSTER_TIMEOUT = 8
    # 并发上限。集群数通常是个位数，瓶颈在远端 API 而不在本机，线程再多也只是排队。
    MAX_WORKERS = 4

    def get(self, request):
        clusters = list(scoped_queryset(K8sCluster.objects.all(), request.user).order_by('name'))
        live_clusters = [
            item for item in clusters if item.status not in INACTIVE_CLUSTER_STATUSES
        ]
        errors = [
            {
                'cluster': item.name,
                'message': '集群已禁用，数据非实时' if item.status == 'disabled' else '集群不可用，数据非实时',
            }
            for item in clusters
            if item.status in INACTIVE_CLUSTER_STATUSES
        ]

        # 缓存是跨全部组织的，读侧必须只挑出这个用户可见的那几个集群。
        aggregates, failures, generated_at = dashboard_cache.dashboard_aggregates(
            live_clusters, self.CLUSTER_TIMEOUT, self.MAX_WORKERS,
        )
        errors.extend(failures)

        def total(field):
            """把各集群的同一个计数字段加成合计值。"""
            return sum(item[field] for item in aggregates)

        status_counts = {}
        for item in clusters:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
        status_labels = dict(K8sCluster.STATUSES)

        return Response(_safe_value({
            # 用缓存里记的那个时刻，不是 now()：这批计数可能是 14 分钟前取的，
            # 回 now() 等于谎报数据新鲜度。前端不展示它，但它会进接口响应，不能是假的。
            'generated_at': generated_at,
            'totals': {
                'cluster_count': len(clusters),
                'available_clusters': status_counts.get('available', 0),
                'unavailable_clusters': status_counts.get('unavailable', 0),
                'disabled_clusters': status_counts.get('disabled', 0),
                'node_count': sum(item.node_count for item in clusters),
                'namespace_count': total('namespace_count'),
                'pod_count': total('pod_count'),
                'abnormal_pod_count': total('abnormal_pod_count'),
                'workload_count': total('workload_count'),
                'warning_event_count': total('warning_event_count'),
            },
            'cluster_status_distribution': [
                {'name': status_labels.get(status, status), 'value': value}
                for status, value in sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            'resource_scale': [
                item for item in [
                    {'name': '命名空间', 'value': total('namespace_count')},
                    {'name': 'Pod', 'value': total('pod_count')},
                    {'name': '工作负载', 'value': total('workload_count')},
                    {'name': '网络资源', 'value': total('network_count')},
                    {'name': '配置与密钥', 'value': total('config_count')},
                    {'name': '存储', 'value': total('storage_count')},
                ] if item['value']
            ],
            'pod_status_distribution': _merge_counts([item['pod_status'] for item in aggregates]),
            'workload_kind_distribution': _merge_counts([item['workload_kind'] for item in aggregates]),
            # 逐集群的证书到期情况（不是分布图）：集群数量是个位数，与其画一张只有两三个
            # 扇区的环形图，不如把「哪个集群、哪张证书、还剩几天」直接列出来。
            'certificate_summary': _certificate_summary(clusters),
            'operation_trend': _operation_trend(request.user),
            'partial_errors': errors,
        }))
