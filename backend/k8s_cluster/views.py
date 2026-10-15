from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from k8s_audit.services import write_k8s_audit
from ops.permission_catalog import action_code
from ops.permission_service import has_permission, is_permission_admin
from k8s_machine.views import release_cluster_machines
from ops.tenancy import assign_organization, scoped_queryset

from .models import K8sCluster, K8sClusterHealthCheck
from .serializers import K8sClusterHealthCheckSerializer, K8sClusterSerializer
from .services import (
    cluster_certificate_summary,
    decrypted_kubeconfig,
    sync_cluster_summary,
    test_cluster_connection,
)


class K8sPagination(PageNumberPagination):
    """K8s module pagination: 10 rows per page."""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


def ensure_not_destroyed(cluster):
    """已清理的集群不能再被任何「连接」或「改状态」的动作碰到。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：无显式返回值。
    副作用：只读取对象字段。

    两个理由：节点上已经没有任何 Kubernetes 组件，测试连通性、同步概览、看 kubeconfig
    都只会超时；而更危险的是**改状态**——把「已销毁」改回「已配置」，它立刻又会以一台
    正常集群的样子出现在运维入口里，配置版本还在、两道闸门都放行，于是能对一台已经不存在的
    集群提交升级或扩容。要收起这条记录，正确做法是删除它，而不是改状态。
    """
    if cluster.status == 'destroyed':
        raise ValidationError({'detail': '该集群已被平台清理，节点上不再有 Kubernetes 组件，请直接重新部署。'})


def ensure_not_deploying(cluster):
    """部署中的集群还没有 API Server 与凭据，连接类操作必然失败，统一给出明确提示。"""
    if cluster.status == 'deploying':
        raise ValidationError({'detail': '集群仍在部署中，暂不能执行该操作。'})
    ensure_not_destroyed(cluster)


def _backup_error_as_validation(exc):
    """把备份服务的业务异常转成 DRF 的 400。

    参数：`exc` 表示 ``backup.BackupError`` 实例。
    返回：返回 ``ValidationError``。
    副作用：纯计算。

    ``BackupError`` 只是个普通 ``Exception``，DRF 不认识它 —— 不转换的话响应是 HTML 500，
    页面上只剩一句 Server Error：用户既看不到原因，也不知道下一步该做什么，看到的现象就是
    「点了恢复没反应」。它的消息本来就是写给用户看的中文（版本不匹配、备份不属于本集群…），
    原样放进 ``detail`` 即可。
    """
    return ValidationError({'detail': str(exc)})


class K8sScopedViewSet(viewsets.ModelViewSet):
    pagination_class = K8sPagination
    search_fields = ()

    def get_queryset(self):
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        keyword = self.request.query_params.get('q', '').strip()
        if keyword and self.search_fields:
            query = Q()
            for field in self.search_fields:
                query |= Q(**{f'{field}__icontains': keyword})
            queryset = queryset.filter(query)
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save()
        assign_organization(instance, self.request.user)
        instance.save()
        write_k8s_audit(self.request, 'create', instance, {'id': instance.pk})

    def perform_update(self, serializer):
        instance = serializer.save()
        assign_organization(instance, self.request.user)
        instance.save()
        write_k8s_audit(self.request, 'update', instance, {'id': instance.pk})

    def perform_destroy(self, instance):
        write_k8s_audit(self.request, 'delete', instance, {'id': instance.pk})
        instance.delete()


class K8sClusterViewSet(K8sScopedViewSet):
    """Cluster access CRUD plus explicit kubeconfig reveal and live connection test."""

    queryset = K8sCluster.objects.select_related('organization', 'credential').all()
    serializer_class = K8sClusterSerializer
    search_fields = ('name', 'cluster_name', 'context_name', 'user_name', 'api_server', 'kube_version', 'runtime_version', 'remark')

    def perform_destroy(self, instance):
        """删除集群时一并解绑它名下的机器。

        机器表的「所属集群」存的是集群标识（规划 6.2），存量数据里则是集群名称，
        两种都要清：集群删除后这些机器就没有归属了，这里统一清空角色与所属集群，
        和向导里移除节点的效果保持一致。
        """
        with transaction.atomic():
            released_ids = release_cluster_machines(self.request, instance.slug, instance.name)
            write_k8s_audit(self.request, 'delete', instance, {
                'id': instance.pk,
                'released_machines': released_ids,
            })
            instance.delete()

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.annotate(
            _availability_order=Case(
                # 部署中的集群排在最前：它是正在进行中的动作，用户点完部署回来要第一时间看到。
                When(status='deploying', then=Value(0)),
                When(status='available', then=Value(1)),
                When(status='configured', then=Value(2)),
                When(status='unconfigured', then=Value(3)),
                When(status='unavailable', then=Value(4)),
                When(status='disabled', then=Value(5)),
                default=Value(6),
                output_field=IntegerField(),
            ),
        ).order_by('_availability_order', 'name', 'pk')

    @action(detail=False, methods=['get'])
    def summary(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        status_counts = {key: 0 for key, _label in K8sCluster.STATUSES}
        for item in queryset.values('status'):
            status_counts[item['status']] = status_counts.get(item['status'], 0) + 1
        credential_count = queryset.filter(credential__encrypted_kubeconfig__gt='').count()
        last_health = (
            scoped_queryset(K8sClusterHealthCheck.objects.select_related('cluster').all(), request.user)
            .order_by('-checked_at')
            .first()
        )
        return Response({
            'total': queryset.count(),
            'available': status_counts.get('available', 0),
            'disabled': status_counts.get('disabled', 0),
            'unavailable': status_counts.get('unavailable', 0),
            'configured': status_counts.get('configured', 0),
            'deploying': status_counts.get('deploying', 0),
            'credential_count': credential_count,
            'node_count': sum(item.node_count for item in queryset),
            'last_checked_at': last_health.checked_at.strftime('%Y-%m-%d %H:%M:%S') if last_health else '',
            'last_check_message': last_health.message if last_health else '',
        })

    @action(detail=False, methods=['get'], url_path='certificates')
    def certificates(self, request):
        """列出各集群托管凭据里证书的到期情况。

        参数：`request` 表示当前请求。
        返回：返回 ``{'generated_at', 'items': [...]}``，按剩余天数升序（读不到的排在最后）。
        副作用：解密各集群的凭据并在本机解析证书；不联网、不修改数据。

        做成列表级动作而不是给 ``K8sClusterSerializer`` 加字段：证书要解密 KubeConfig 才能算出来，
        放进列表序列化器会让每次翻页都替当页的每个集群做一次解析，而按集群详情展示时又只要一条
        —— 一次请求取全量、前端按 cluster_id 取用，两处都省事。

        与 ``summary`` 同一个路径前缀（``/k8s/clusters``），权限沿用「集群接入」页的页面码，
        因此不需要新增权限点。
        """
        queryset = self.filter_queryset(self.get_queryset())
        items = [cluster_certificate_summary(cluster) for cluster in queryset]
        items.sort(key=lambda item: (item['days_left'] is None, item['days_left'] or 0))
        return Response({
            'generated_at': timezone.now().strftime('%Y-%m-%d %H:%M:%S'),
            'items': items,
        })

    @action(detail=True, methods=['post'], url_path='reveal-kubeconfig')
    def reveal_kubeconfig(self, request, pk=None):
        if not is_permission_admin(request.user) and not has_permission(request.user, action_code('page.k8s.clusters.view', 'reveal')):
            self.permission_denied(request, message='没有查看 KubeConfig 明文的权限')
        cluster = self.get_object()
        ensure_not_deploying(cluster)
        kubeconfig = decrypted_kubeconfig(cluster)
        write_k8s_audit(request, 'reveal_kubeconfig', cluster, {'id': cluster.pk, 'field': 'kubeconfig'})
        return Response({'kubeconfig': kubeconfig})

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        cluster = self.get_object()
        ensure_not_deploying(cluster)
        cluster = test_cluster_connection(cluster)
        write_k8s_audit(request, 'test', cluster, {'id': cluster.pk, 'status': cluster.last_test_status, 'message': cluster.last_test_message})
        return Response(self.get_serializer(cluster).data)

    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        cluster = self.get_object()
        ensure_not_deploying(cluster)
        cluster = sync_cluster_summary(cluster)
        write_k8s_audit(request, 'sync', cluster, {'id': cluster.pk, 'status': cluster.status})
        return Response(self.get_serializer(cluster).data)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        cluster = self.get_object()
        ensure_not_destroyed(cluster)
        cluster.status = 'disabled'
        cluster.save(update_fields=['status', 'updated_at'])
        write_k8s_audit(request, 'disable', cluster, {'id': cluster.pk})
        return Response(self.get_serializer(cluster).data)

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        cluster = self.get_object()
        ensure_not_destroyed(cluster)
        cluster.status = 'configured'
        cluster.save(update_fields=['status', 'updated_at'])
        write_k8s_audit(request, 'enable', cluster, {'id': cluster.pk})
        return Response(self.get_serializer(cluster).data)

    # ---- 部署型集群的运维操作 ------------------------------------------
    #
    # 扩容、升级、备份、恢复都只对 ``origin='deployed'`` 的集群开放：导入型集群平台没有
    # 它的 inventory 与 SSH 免密通道，硬跑 ezctl 只会覆盖别人手工维护的配置（规划 6.1）。
    # 所有动作都返回**任务对象**，真正的执行在 Worker 里串行进行，前端拿任务 ID 去进度页轮询。

    @action(detail=True, methods=['post'], url_path='nodes/add-master')
    def add_master(self, request, pk=None):
        """向 HA 集群追加一台 Master 节点。

        参数：`request` 表示当前请求；`pk` 表示集群主键。
        返回：返回新建的扩容任务（HTTP 201）。
        副作用：写入任务与节点快照并入队；可能启动一次性 Worker。
        """
        return self._submit_cluster_task(request, 'add_master')

    @action(detail=True, methods=['post'], url_path='nodes/add-worker')
    def add_worker(self, request, pk=None):
        """向集群追加一台工作节点。

        参数：`request` 表示当前请求；`pk` 表示集群主键。
        返回：返回新建的扩容任务（HTTP 201）。
        副作用：写入任务与节点快照并入队；可能启动一次性 Worker。
        """
        return self._submit_cluster_task(request, 'add_worker')

    @action(detail=True, methods=['post'])
    def upgrade(self, request, pk=None):
        """把集群升到指定版本：同次版本内的补丁版本，或下一个次版本。

        参数：`request` 表示当前请求；`pk` 表示集群主键。
        返回：返回新建的升级任务（HTTP 201）。
        副作用：写入任务并入队；升级前会先自动完成一次备份。

        请求体里的 ``kubernetes_version`` 是**目标版本**，必须显式给出（候选由
        ``GET .../operations/`` 的 ``upgrade_targets`` 提供）。缺了它时
        ``cluster_ops`` 会把它回落成集群当前版本，而那样的任务跑完什么都不会变却报「成功」
        —— 这正是「补丁升级看不出效果」的来源；现在回落出来的相同版本会被直接拒掉。

        升级同样要求手动输入集群名称：它会重启每个节点的 kubelet 与容器运行时，
        写错集群的代价和恢复一样大（规划 12）。

        ``kube_proxy_mode`` 是可选的新 kube-proxy 模式：只在目标 kubeasz 的模板不支持集群
        当前模式时才给（1.35 的 ipvs 集群升 1.36：3.7.0 的模板只认 iptables 与 nftables）。
        给了它，平台就会改写集群 hosts 里的 ``PROXY_MODE``，由升级剧本在每台节点上重新渲染
        kube-proxy 配置并重启 kube-proxy；不给则沿用集群当前模式。
        """
        cluster = self.get_object()
        confirm = str(request.data.get('confirm_name') or '').strip()
        if confirm != cluster.name:
            raise ValidationError({'confirm_name': f'升级会重启节点组件。请输入集群名称「{cluster.name}」以确认。'})
        # 目标版本按标量取出来再交下去：表单编码的请求体里它是 ``['1.36']``，直接流到版本
        # 校验里会报「无法解析升级目标版本「['1.36']」」这种看不懂的话（``_scalar_body_field``）。
        return self._submit_cluster_task(request, 'upgrade', payload={
            'kubernetes_version': self._scalar_body_field(request.data.get('kubernetes_version')),
            'kube_proxy_mode': self._scalar_body_field(request.data.get('kube_proxy_mode')),
        })

    @action(detail=True, methods=['post'], url_path='destroy')
    def destroy_cluster(self, request, pk=None):
        """清理集群：把节点上的 Kubernetes 组件与数据清掉，让这批机器能重新部署。

        参数：`request` 表示当前请求；`pk` 表示集群主键。
        返回：返回新建的清理任务（HTTP 201）。
        副作用：写入任务与节点快照并入队；执行时会在节点上停服务、删数据目录并解绑机器。

        方法名必须叫 ``destroy_cluster`` 而不是 ``destroy``：``ModelViewSet`` 已经占用了
        ``destroy`` 作为 ``DELETE /clusters/{pk}/`` 的处理方法，同名会把「删除集群记录」
        与「清理节点」两件完全不同的事绑在同一个方法上。
        """
        cluster = self.get_object()
        confirm = str(request.data.get('confirm_name') or '').strip()
        if confirm != cluster.name:
            raise ValidationError({
                'confirm_name': f'清理会删除节点上的 Kubernetes 组件与数据。请输入集群名称「{cluster.name}」以确认。',
            })
        return self._submit_cluster_task(request, 'destroy')

    @action(detail=True, methods=['get'], url_path='operations')
    def operations(self, request, pk=None):
        """返回该集群当前允许的运维操作。

        参数：`request` 表示当前请求；`pk` 表示集群主键。
        返回：返回 ``cluster_capabilities`` 的结果，供前端决定按钮可用性。
        副作用：只读数据库与配置，不修改数据。
        """
        from k8s_deployment.services import cluster_ops

        return Response(cluster_ops.cluster_capabilities(self.get_object()))

    @action(detail=True, methods=['get', 'post'], url_path='backups')
    def backups(self, request, pk=None):
        """列出备份（GET）或触发一次备份（POST）。

        参数：`request` 表示当前请求；`pk` 表示集群主键。
        返回：GET 返回备份列表与保留策略；POST 返回新建的备份任务（HTTP 201）。
        副作用：POST 时写入备份任务与待执行备份记录并入队。

        一个 ``url_path`` 同时承载列表与创建，是因为 DRF 的 ``@action`` 一次只能绑定一个
        方法名，而这两件事的权限语义必须分开（查看 vs 备份）。权限层按 HTTP 方法分流，
        见 ``ops/permissions.py::_action_name``。
        """
        if request.method == 'GET':
            return self._list_backups(request, self.get_object())
        return self._submit_cluster_task(request, 'backup')

    @action(detail=True, methods=['delete'], url_path=r'backups/(?P<backup_id>[^/.]+)')
    def delete_backup(self, request, pk=None, backup_id=None):
        """删除一个备份。

        参数：`request` 表示当前请求；`pk` 表示集群主键；`backup_id` 表示备份标识。
        返回：返回更新后的备份记录。
        副作用：删除 OSS 对象并更新备份记录状态。
        """
        from k8s_deployment.services import backup as backup_service

        cluster = self.get_object()
        record = backup_service.list_backups(cluster).filter(backup_id=backup_id).first()
        if record is None:
            raise ValidationError({'backup_id': '找不到该备份记录。'})
        if record.is_protected:
            raise ValidationError({'detail': '该备份是最近的成功备份之一，受保留策略保护，不能手动删除。'})
        try:
            record = backup_service.delete_backup(record)
        except backup_service.BackupError as exc:
            raise _backup_error_as_validation(exc) from exc
        write_k8s_audit(request, 'delete', cluster, {'id': cluster.pk, 'backup_id': record.backup_id})
        return Response(backup_service.backup_summary(record))

    @action(detail=True, methods=['post'], url_path=r'backups/(?P<backup_id>[^/.]+)/restore')
    def restore(self, request, pk=None, backup_id=None):
        """用指定的备份恢复集群。

        参数：`request` 表示当前请求；`pk` 表示集群主键；`backup_id` 表示备份标识。
        返回：返回新建的恢复任务（HTTP 201）。
        副作用：写入恢复任务并入队；真正恢复由 Worker 执行。

        恢复会覆盖 etcd 数据，属于高风险操作，因此要求调用方手动输入集群名称做二次确认
        （规划 4.2）。这道校验放在服务端而不只是前端弹窗：绕过界面直接调接口同样危险。
        """
        from k8s_deployment.services import backup as backup_service

        cluster = self.get_object()
        record = backup_service.list_backups(cluster).filter(backup_id=backup_id).first()
        if record is None:
            raise ValidationError({'backup_id': '找不到该备份记录。'})
        confirm = str(request.data.get('confirm_name') or '').strip()
        if confirm != cluster.name:
            raise ValidationError({
                'confirm_name': f'恢复会覆盖集群当前数据。请输入集群名称「{cluster.name}」以确认。',
            })
        # 先按备份记录的实际版本做一次校验，避免提交一个注定失败的恢复任务。
        try:
            backup_service.validate_restore_selection(cluster, record)
        except backup_service.BackupError as exc:
            raise _backup_error_as_validation(exc) from exc
        return self._submit_cluster_task(request, 'restore', payload={
            'backup_id': record.backup_id,
            'kubernetes_version': record.kubernetes_version,
            # 「跳过应急备份」只在集群已经不健康、没有东西可保护时才允许（规划 11.1）：
            # 集群还活着的时候用户想保留现状，正确做法是先单独提交一次备份任务，
            # 而不是在恢复流程里顺手做一个没人看得见的临时备份。
            'skip_emergency_backup': self._skip_emergency_backup(request, cluster),
        })

    def _skip_emergency_backup(self, request, cluster):
        """解析恢复请求里的「跳过应急备份」勾选。

        参数：`request` 表示当前请求；`cluster` 表示 K8sCluster 实例。
        返回：返回 True 或 False。
        副作用：只读请求体与数据库（健康探测），不修改数据。

        是否允许跳过由**服务端自己探测**决定，不采信前端传来的结论：绕过界面直接调接口
        同样能提交这个字段，而集群还活着的时候跳过备份是不可接受的。探测失败按不健康处理，
        与「恢复是最后手段」的取舍一致（规划 11.1）。
        """
        from k8s_deployment.services import backup as backup_service
        from k8s_deployment.services import cluster_ops

        requested = cluster_ops.as_bool(request.data.get('skip_emergency_backup'))
        if not requested:
            return False
        try:
            healthy = _cluster_reports_healthy(cluster)
        except backup_service.BackupError as exc:
            raise _backup_error_as_validation(exc) from exc
        if healthy:
            raise ValidationError({
                'skip_emergency_backup': '该集群当前是健康的，请先单独提交一次备份任务保护现状，再执行恢复。',
            })
        return True

    # ---- 运维动作的内部实现 --------------------------------------------

    def _list_backups(self, request, cluster):
        """返回某个集群的备份列表与保留策略。

        参数：`request` 表示当前请求；`cluster` 表示 K8sCluster 实例。
        返回：返回备份列表与保留策略。
        副作用：只读数据库，不修改数据。
        """
        from k8s_deployment.services import backup as backup_service

        queryset = backup_service.list_backups(cluster)
        statuses = [item for item in (request.query_params.get('status') or '').split(',') if item]
        if statuses:
            queryset = queryset.filter(status__in=statuses)
        capability = backup_service.backup_capability()
        records = list(queryset[:200])
        # 恢复确认弹窗要显示备份时的节点（规划 11.2），节点清单在各条备份的配置版本里，
        # 这里一次取回，避免列表退化成逐条查询。
        revisions = backup_service.revision_snapshots(
            cluster, [item.config_revision for item in records],
        )
        return Response({
            'results': [
                backup_service.backup_summary(item, nodes=backup_service.backup_nodes(item, revisions))
                for item in records
            ],
            'retention_days': backup_service.retention_days(),
            'min_keep': backup_service.retention_min_keep(),
            'can_backup': capability['ok'],
            'backup_hint': capability['message'],
        })

    @staticmethod
    def _scalar_body_field(value):
        """把请求体里的一个标量字段取成去掉空白的字符串。

        参数：`value` 表示该字段的原始值。
        返回：返回字符串；缺失或为空时返回空串。
        副作用：纯计算，不修改任何状态。

        JSON 请求体里它就是字符串；表单编码（``application/x-www-form-urlencoded``、
        ``multipart/form-data``）的请求体里 Django 把它装进了列表，``dict(request.data)``
        取出来的就是 ``['1.36']``。这种形状流进版本校验只会得到一句谁也看不懂的
        「无法解析升级目标版本「['1.36']」」—— 取第一个值就够了。
        """
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ''
        return str(value or '').strip()

    def _submit_cluster_task(self, request, task_type, payload=None):
        """构造并提交一个作用于现有集群的任务。

        参数：`request` 表示当前请求；`task_type` 表示任务类型；`payload` 表示附加请求字段。
        返回：返回新建任务详情（HTTP 201）。
        副作用：写入任务与节点快照并入队；可能启动一次性 Worker。
        """
        from k8s_deployment.serializers import K8sDeploymentSerializer
        from k8s_deployment.services import cluster_ops
        from k8s_deployment.services import state_machine

        cluster = self.get_object()
        body = dict(request.data or {})
        body.update(payload or {})
        machines = self._machines_from_request(request, task_type)
        try:
            deployment = cluster_ops.submit(
                cluster, task_type, payload=body, machines=machines, user=request.user,
            )
        except cluster_ops.ClusterOpError as exc:
            raise ValidationError({'detail': str(exc)}) from exc

        write_k8s_audit(request, 'create', cluster, {
            'id': cluster.pk,
            'action': task_type,
            'deployment_id': deployment.pk,
            'backup_id': body.get('backup_id', ''),
            # 跳过应急备份是用户对高风险动作的显式取舍，必须留下审计痕迹（规划 11.2）。
            'skip_emergency_backup': bool(getattr(deployment, 'skip_emergency_backup', False)),
        })
        state_machine.spawn_worker(once=True)
        return Response(
            K8sDeploymentSerializer(deployment, context=self.get_serializer_context()).data,
            status=201,
        )

    def _machines_from_request(self, request, task_type):
        """从请求体里解析扩容任务要添加的机器。

        参数：`request` 表示当前请求；`task_type` 表示任务类型。
        返回：返回 Machine 列表；非扩容任务返回空列表。
        副作用：执行若干次数据库查询。

        一次只允许添加一台：kubeasz 的 add-node/add-master 每次只处理一个节点，
        批量添加需要拆成多次串行执行，而当前的任务模型没有表达「一批」的能力。
        """
        from k8s_machine.models import Machine

        if task_type not in ('add_master', 'add_worker'):
            return []
        raw = request.data.get('nodes') or []
        identifiers = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    identifiers.append(item.get('machine_id'))
                    identifiers.append(item.get('ip_address'))
                else:
                    identifiers.append(item)
        identifiers.append(request.data.get('machine_id'))
        identifiers.append(request.data.get('ip_address'))
        identifier = next((item for item in identifiers if item not in (None, '')), None)
        if not identifier:
            raise ValidationError({'nodes': '请指定要添加的机器。'})
        if str(identifier).isdigit():
            machine = Machine.objects.filter(pk=identifier).first()
        else:
            machine = Machine.objects.filter(ip_address=identifier).first()
        if machine is None:
            raise ValidationError({'nodes': f'找不到机器：{identifier}。'})
        return [machine]


def _cluster_reports_healthy(cluster):
    """探测集群当前是否健康。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 True 或 False。
    副作用：向 Kubernetes API 发起一次请求，并可能建立一次短 SSH 连接读取 etcd 状态。

    探测失败一律按「不健康」处理：恢复是集群已经出问题时的最后手段，
    一个说不清状态的集群不应该被平台挡在恢复门外（规划 11.1）。
    """
    from k8s_deployment.services import backup as backup_service

    try:
        nodes = backup_service.cluster_nodes(cluster, None)
        credentials = backup_service.credential_map_for(cluster, nodes) if nodes else {}
        return bool(backup_service.assess_restore_source(cluster, nodes, credentials)['healthy'])
    except Exception:
        return False


class K8sClusterHealthCheckViewSet(viewsets.ReadOnlyModelViewSet):
    """集群健康检查记录，只读。"""

    queryset = K8sClusterHealthCheck.objects.select_related('cluster', 'organization').all()
    serializer_class = K8sClusterHealthCheckSerializer
    pagination_class = K8sPagination

    def get_queryset(self):
        """按组织隔离，并支持按集群过滤。

        参数：无。
        返回：返回过滤后的查询集。
        副作用：只读数据库，不修改数据。
        """
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        cluster_id = self.request.query_params.get('cluster')
        if cluster_id:
            queryset = queryset.filter(cluster_id=cluster_id)
        return queryset
