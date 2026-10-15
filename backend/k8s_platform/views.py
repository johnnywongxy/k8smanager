from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from k8s_audit.models import K8sOperationAudit
from k8s_cluster.models import K8sCluster
from k8s_cluster.services import select_preferred_cluster
from k8s_resource.services.errors import translate_k8s_error
from k8s_resource.services.namespace_authz import assert_namespace_permission, filter_items_for_user
from ops.permissions import user_organization
from ops.tenancy import scoped_queryset

from .services import helm_release_detail, helm_uninstall_release, helm_upgrade_release, list_helm_releases


class K8sPlatformPagination(PageNumberPagination):
    """K8s platform API pagination: 10 rows per page."""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class K8sPlatformBaseView(APIView):
    def get_cluster(self, request):
        queryset = scoped_queryset(K8sCluster.objects.all(), request.user)
        cluster_id = request.query_params.get('cluster') or (request.data.get('cluster') if isinstance(request.data, dict) else '')
        cluster = queryset.filter(pk=cluster_id).first() if cluster_id else select_preferred_cluster(queryset)
        if not cluster:
            raise ValueError('未找到可访问的 K8s 集群')
        if cluster.status == 'disabled':
            raise ValueError('当前集群已禁用')
        return cluster


class K8sHelmReleaseView(K8sPlatformBaseView):
    """List, inspect and mutate Helm releases in the selected K8s cluster."""

    def _paginate(self, request, items):
        paginator = K8sPlatformPagination()
        page = paginator.paginate_queryset(items, request, view=self)
        if page is None:
            return Response({'count': len(items), 'results': items})
        return paginator.get_paginated_response(page)

    def _write_audit(self, request, cluster, namespace, name, action, status='completed', detail=None, failure_reason=''):
        K8sOperationAudit.objects.create(
            organization=user_organization(request.user),
            cluster_name=cluster.name,
            namespace=namespace,
            resource_type='HelmRelease',
            resource_name=name,
            action=action,
            actor=getattr(request.user, 'username', 'system') or 'system',
            status=status,
            request_summary=detail or {},
            result_summary={} if status == 'failed' else (detail or {}),
            failure_reason=(failure_reason or '')[:240],
        )

    def get(self, request):
        try:
            cluster = self.get_cluster(request)
            namespace = request.query_params.get('namespace', '').strip()
            name = request.query_params.get('name', '').strip()
            if namespace:
                assert_namespace_permission(request.user, cluster, namespace=namespace, resource_type='helm', action='read')
            if name:
                return Response(helm_release_detail(cluster, namespace=namespace, name=name))
            payload = list_helm_releases(cluster, namespace=namespace, keyword=request.query_params.get('q', '').strip())
            if not namespace:
                payload = filter_items_for_user(payload, request.user, cluster, action='read', resource_type='helm')
            response = self._paginate(request, payload)
            response.data['cluster'] = {'id': cluster.id, 'name': cluster.name, 'status': cluster.status}
            return response
        except Exception as exc:
            return Response({'detail': f'查询 Helm Release 失败：{translate_k8s_error(exc)}'}, status=400)

    def post(self, request, action_name=''):
        cluster = None
        namespace = request.data.get('namespace', '').strip() if isinstance(request.data, dict) else ''
        name = request.data.get('name', '').strip() if isinstance(request.data, dict) else ''
        action = action_name or (request.data.get('action', '').strip() if isinstance(request.data, dict) else '')
        try:
            cluster = self.get_cluster(request)
            if action == 'update':
                assert_namespace_permission(request.user, cluster, namespace=namespace, resource_type='helm', action='update')
                result = helm_upgrade_release(
                    cluster,
                    namespace=namespace,
                    name=name,
                    chart_ref=request.data.get('chart_ref', '').strip(),
                    values_yaml=request.data.get('values_yaml', ''),
                    reuse_values=bool(request.data.get('reuse_values', True)),
                )
                detail = {'chart_ref': request.data.get('chart_ref', ''), 'reuse_values': bool(request.data.get('reuse_values', True))}
                self._write_audit(request, cluster, namespace, name, 'helm_update', detail=detail)
                return Response({'detail': 'Helm Release 已更新', 'result': result})
            if action in {'delete', 'uninstall'}:
                assert_namespace_permission(request.user, cluster, namespace=namespace, resource_type='helm', action='delete')
                result = helm_uninstall_release(cluster, namespace=namespace, name=name, keep_history=bool(request.data.get('keep_history', False)))
                detail = {'keep_history': bool(request.data.get('keep_history', False))}
                self._write_audit(request, cluster, namespace, name, 'helm_uninstall', detail=detail)
                return Response({'detail': 'Helm Release 已删除', 'result': result})
            return Response({'detail': '不支持的 Helm 操作'}, status=404)
        except Exception as exc:
            if cluster:
                self._write_audit(request, cluster, namespace, name, f'helm_{action or "unknown"}', status='failed', detail=dict(request.data), failure_reason=translate_k8s_error(exc))
            return Response({'detail': f'Helm 操作失败：{translate_k8s_error(exc)}'}, status=400)
