from rest_framework.response import Response
from rest_framework.views import APIView

from k8s_cluster.models import K8sCluster
from k8s_cluster.services import select_preferred_cluster
from k8s_resource.services.namespace_authz import assert_namespace_permission
from ops.permissions import user_organization
from ops.tenancy import scoped_queryset

from .models import K8sQueryHistory
from .services import query_pod_logs, resource_statistics


class K8sObservabilityBaseView(APIView):
    """Shared cluster selection and query-history helpers for K8s observability APIs."""

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
            raise ValueError('当前集群已禁用，不能查询观测数据')
        return cluster

    def write_history(self, request, query_type, cluster, status='completed', result_summary=None, **extra):
        K8sQueryHistory.objects.create(
            organization=user_organization(request.user),
            query_type=query_type,
            cluster_name=cluster.name,
            namespace=extra.get('namespace', '') or '',
            pod=extra.get('pod', '') or '',
            container=extra.get('container', '') or '',
            source_type=extra.get('source_type', '') or '',
            query=extra.get('query', '') or '',
            time_range=extra.get('time_range', '') or '',
            status=status,
            actor=getattr(request.user, 'username', 'system') or 'system',
            result_summary=result_summary or {},
        )


class K8sPodLogsView(K8sObservabilityBaseView):
    """Query realtime Kubernetes Pod logs through the cluster API."""

    def get(self, request):
        try:
            cluster = self.get_cluster(request)
            namespace = request.query_params.get('namespace', '').strip()
            if namespace:
                assert_namespace_permission(request.user, cluster, namespace=namespace, resource_type='logs', action='read')
            pod = request.query_params.get('pod', '').strip()
            container = request.query_params.get('container', '').strip()
            keyword = request.query_params.get('keyword', '').strip()
            payload = query_pod_logs(
                cluster,
                namespace=namespace,
                pod=pod,
                container=container,
                keyword=keyword,
                tail_lines=request.query_params.get('tail_lines', '200'),
                since_seconds=request.query_params.get('since_seconds', ''),
                previous=request.query_params.get('previous', '') in {'1', 'true', 'True'},
            )
            self.write_history(
                request,
                'logs',
                cluster,
                source_type=payload.get('source_status', {}).get('source_type', 'kubernetes'),
                namespace=namespace,
                pod=pod,
                container=container,
                query=keyword,
                time_range=request.query_params.get('since_seconds', '') or request.query_params.get('minutes', ''),
                result_summary={'line_count': payload.get('line_count', 0), 'status': payload.get('source_status', {}).get('status', '')},
            )
            return Response(payload)
        except Exception as exc:
            try:
                cluster = self.get_cluster(request)
                self.write_history(request, 'logs', cluster, status='failed', query=request.query_params.get('keyword', ''), result_summary={'error': str(exc)[:180]})
            except Exception:
                pass
            return Response({'detail': f'查询 Pod 日志失败：{exc}'}, status=400)


class K8sStatisticsView(K8sObservabilityBaseView):
    """Return resource statistics aggregated from real Kubernetes API objects."""

    def get(self, request):
        try:
            cluster = self.get_cluster(request)
            namespace = request.query_params.get('namespace', '').strip()
            if namespace:
                assert_namespace_permission(request.user, cluster, namespace=namespace, resource_type='statistics', action='read')
            payload = resource_statistics(cluster)
            self.write_history(request, 'statistics', cluster, result_summary=payload.get('summary', {}))
            return Response(payload)
        except Exception as exc:
            return Response({'detail': f'查询资源统计失败：{exc}'}, status=400)


class K8sHostUsageView(K8sObservabilityBaseView):
    """按集群返回它的各节点 CPU / 内存 / 磁盘水位（SSH 只读采集）。

    与上面那个视图刻意分开：``K8sStatisticsView`` 走 k8s API，答的是「集群里跑了什么」；
    这个走 SSH 读 ``/proc`` 与 ``df``，答的是「这几台机器自己还撑得住吗」。分成两个端点
    而不是塞进一个响应里，是因为两者失败条件完全不同：k8s API 连不上时主机水位照样读得到，
    而那正是最需要看它的时候。

    **当前界面上没有入口**：资源统计页顶部那块「主机资源」已按需求撤掉，主机水位改由机器
    列表页逐台展示（``/machines/servers/usage/``，采集器与这里共用 ``host_usage``）。
    这个端点保留下来是因为「按集群看这几台节点」仍然是个成立的问法，接回去只需一个页面区块；
    真要确认它没人用、想删，记得连着 ``k8s_observability/tests.py`` 里那几条用例一起删。

    权限挂在资源统计页既有的页面码上（``ops.permissions.API_PAGE_ALIASES`` 里
    ``/k8s/host-usage`` → ``page.k8s.statistics.view``），与 ``/k8s/statistics/`` 同码。

    不写查询历史：``K8sQueryHistory`` 记录的是「谁查了集群里的什么数据」，这是一次平台到节点的
    资源采集，写进去只会把真正的审计线索淹掉。
    """

    def get(self, request):
        try:
            cluster = self.get_cluster(request)
        except Exception as exc:
            return Response({'detail': f'查询主机资源失败：{exc}'}, status=400)
        # 采集器归 k8s_deployment（SSH 与节点清单都在那边），函数内懒 import：两个 app 互相
        # 引用时顶层 import 会绕成环，这也与 k8s_cluster/views.py 引用 cluster_ops 的既有做法一致。
        from k8s_deployment.services import host_usage

        # 采不到（导入型集群平台没有它的节点清单与免密通道）不是错误，仍返回 200 加一句中文
        # 原因，由前端按空态文案展示 —— 用 4xx 表达「这个集群采集不了」会让界面只能弹一句
        # 报错，而这里真正要说的是「换一种方式看」。
        payload = host_usage.collect_cluster_usage(cluster)
        return Response({'cluster_id': cluster.pk, 'cluster_name': cluster.name, **payload})
