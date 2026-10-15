from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from ops.permissions import user_organization
from ops.services.client_ip import request_client_ip
from ops.services.system_tools import run_ssh_password_auth, run_telnet
from ops.tenancy import assign_organization, scoped_queryset
from platform_logs.models import AuditLog

from . import metrics
from .models import Machine, normalize_machine_roles
from .serializers import MachineSerializer


class MachinePagination(PageNumberPagination):
    """机器列表分页：每页 10 条。"""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


# 一次最多给几台机器取水位。上限本身在 ``metrics`` 里（采集与后台刷新共用同一个值），
# 这里保留一个同名的别名，避免权限、测试与调用方到处 import 两个名字。
MAX_USAGE_MACHINES = metrics.MAX_COLLECT_MACHINES


def write_machine_audit(request, action, machine, detail=None):
    """写入机器管理业务审计日志。

    参数：`request` 表示当前请求，可以为 None（平台内部发起的改动）；`action` 表示业务动作；
        `machine` 表示目标机器；`detail` 表示补充详情。
    返回：无显式返回值。
    副作用：向审计日志表写入一条记录。

    没有请求时按「平台自身」记账：组织取机器所属组织，执行人记 system。这样由任务创建、
    集群删除等内部动作引起的机器归属变化同样留痕，而不是静默改写。
    """
    user = getattr(request, 'user', None)
    AuditLog.objects.create(
        organization=user_organization(user) or getattr(machine, 'organization', None),
        actor=getattr(user, 'username', '') or 'system',
        action=f'Machine.{action}',
        resource=str(machine),
        ip_address=request_client_ip(request) if request is not None else '127.0.0.1',
        detail=detail or {'id': getattr(machine, 'pk', None)},
    )


def release_cluster_machines(request, *cluster_keys, organization=None, reason=''):
    """解绑归属于指定集群的机器，清空角色与所属集群。

    删除集群时调用：机器表的「所属集群」存的是集群标识（规划 6.2），集群没了就得把机器的
    归属一并清掉，否则机器列表里会留下一批指向不存在集群的角色。

    参数：`request` 表示当前请求，**可以为 None**（部署任务在 Worker 里跑，没有 HTTP 请求）；
        `cluster_keys` 表示该集群可能被记下的名字，按优先级传入（标识在前，展示名称在后）；
        `organization` 表示无请求时用来限定范围的租户，此时**必填**；`reason` 表示写进审计的原因，
        留空时按「集群已删除」记账。
    返回：返回本次实际解绑的机器主键列表。
    副作用：修改机器表的 roles 与 cluster_name，并为每台机器写入审计日志。

    之所以要接受多个名字：规划 6.2 之后新数据存标识，而存量数据（以及按名称手工绑定的
    接入型集群）存的是中文名称。删除集群时两种都要清，不能只认其中一种。

    ``request=None`` 时必须显式给 ``organization``：无请求时 ``scoped_queryset`` 拿不到用户，
    组织范围会退化成「只看无所属组织的机器」，一台都匹配不上 —— 那种失败**不会报错**，
    只是静默地什么都没解绑，是最难查的一种形态。宁可在这里直接拦住。
    """
    keys = [str(key or '').strip() for key in cluster_keys]
    keys = [key for key in dict.fromkeys(keys) if key]
    if not keys:
        return []
    if request is None:
        if organization is None:
            raise ValueError('无请求地解绑集群机器时必须显式指定 organization，否则组织范围会落空。')
        candidates = Machine.objects.filter(organization=organization, cluster_name__in=keys)
    else:
        # 只处理当前用户组织范围内可见的机器，避免跨组织改写别人名下的机器。
        candidates = scoped_queryset(Machine.objects.all(), request.user).filter(cluster_name__in=keys)
    released_ids = []
    for machine in candidates:
        previous_roles = machine.normalized_roles()
        previous_cluster_name = machine.cluster_name
        machine.set_roles([])
        machine.cluster_name = ''
        machine.save(update_fields=['roles', 'cluster_name', 'updated_at'])
        write_machine_audit(request, 'unassign', machine, {
            'id': machine.pk,
            'previous_roles': previous_roles,
            'previous_cluster_name': previous_cluster_name,
            'reason': reason or f'集群 {keys[0]} 已删除',
        })
        released_ids.append(machine.pk)
    return released_ids


class MachineViewSet(viewsets.ModelViewSet):
    """机器管理增删改查，并提供角色统计和 SSH 端口连通性检测。"""

    queryset = Machine.objects.select_related('organization').all()
    serializer_class = MachineSerializer
    pagination_class = MachinePagination
    permission_code = 'page.machine.servers.view'
    search_fields = ('name', 'ip_address', 'ssh_user', 'cluster_name', 'remark')

    def _cluster_slugs_for(self, keyword):
        """返回名称命中关键字的集群标识。

        参数：`keyword` 表示搜索关键字。
        返回：返回集群标识列表（可能是空的）。
        副作用：只读数据库，不修改持久化数据。

        与 ``MachineSerializer.get_cluster_display_name`` 是同一件事的两面：那边把标识翻成
        名称给用户看，这边把用户输入的（可能的）名称翻回标识去查机器。
        """
        from k8s_cluster.models import K8sCluster

        scoped = scoped_queryset(K8sCluster.objects.all(), self.request.user)
        return list(
            scoped.filter(name__icontains=keyword).exclude(slug='').values_list('slug', flat=True)
        )

    def get_queryset(self):
        """按组织范围过滤机器，应用关键字搜索并按状态排序。

        参数：无。
        返回：返回该业务步骤生成、查询或校验后的结果。
        副作用：只读取数据库，不修改持久化数据。
        """
        queryset = scoped_queryset(super().get_queryset(), self.request.user)
        keyword = self.request.query_params.get('q', '').strip()
        if keyword and self.search_fields:
            query = Q()
            for field in self.search_fields:
                query |= Q(**{f'{field}__icontains': keyword})
            # 「所属集群」这一列显示的是集群名称，而机器表里存的是集群标识：只按存下来的
            # 标识搜，用户照着屏幕上看到的名称搜会一台都搜不出来。
            query |= Q(cluster_name__in=self._cluster_slugs_for(keyword))
            queryset = queryset.filter(query)
        return queryset.annotate(
            _status_order=Case(
                When(status='available', then=Value(0)),
                When(status='unknown', then=Value(1)),
                When(status='unavailable', then=Value(2)),
                When(status='disabled', then=Value(3)),
                default=Value(4),
                output_field=IntegerField(),
            ),
        ).order_by('_status_order', 'name', 'pk')

    def perform_create(self, serializer):
        """保存新机器并补全组织归属和审计记录。

        参数：`serializer` 表示通过校验的序列化器。
        返回：无显式返回值。
        副作用：可能请求后端、修改持久化数据或更新全局状态。
        """
        instance = serializer.save()
        assign_organization(instance, self.request.user)
        instance.save()
        write_machine_audit(self.request, 'create', instance, {
            'id': instance.pk,
            'ip_address': instance.ip_address,
            'credential_updated': bool(getattr(serializer, 'password_changed', False)),
        })

    def perform_update(self, serializer):
        """保存机器变更并写入审计记录。

        参数：`serializer` 表示通过校验的序列化器。
        返回：无显式返回值。
        副作用：可能请求后端、修改持久化数据或更新全局状态。
        """
        instance = serializer.save()
        assign_organization(instance, self.request.user)
        instance.save()
        write_machine_audit(self.request, 'update', instance, {
            'id': instance.pk,
            'credential_updated': bool(getattr(serializer, 'password_changed', False)),
        })

    def perform_destroy(self, instance):
        """删除机器前先写入审计记录。

        参数：`instance` 表示待删除的机器记录。
        返回：无显式返回值。
        副作用：可能请求后端、修改持久化数据或更新全局状态。
        """
        write_machine_audit(self.request, 'delete', instance, {'id': instance.pk, 'ip_address': instance.ip_address})
        instance.delete()

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """统计机器总数、各角色数量和连通状态数量。

        参数：`request` 表示当前统计请求。
        返回：返回该业务步骤生成、查询或校验后的结果。
        副作用：只读取数据库，不修改持久化数据。
        """
        queryset = self.filter_queryset(self.get_queryset())
        role_counts = {key: 0 for key, _label in Machine.ROLES}
        status_counts = {key: 0 for key, _label in Machine.STATUSES}
        unassigned = 0
        for item in queryset.values('roles', 'status'):
            # 一台机器可以同时承担多个角色，各角色分别计数，因此这里不能用互斥分支归类。
            codes = normalize_machine_roles(item['roles'])
            if not codes:
                unassigned += 1
            for code in codes:
                role_counts[code] = role_counts.get(code, 0) + 1
            status_counts[item['status']] = status_counts.get(item['status'], 0) + 1
        return Response({
            'total': queryset.count(),
            'master': role_counts.get('master', 0),
            'node': role_counts.get('node', 0),
            'etcd': role_counts.get('etcd', 0),
            'loadbalancer': role_counts.get('loadbalancer', 0),
            'unassigned': unassigned,
            'available': status_counts.get('available', 0),
            'unavailable': status_counts.get('unavailable', 0),
            'unknown': status_counts.get('unknown', 0),
        })

    @action(detail=False, methods=['get'], url_path='usage')
    def usage(self, request):
        """读取指定机器的主机 CPU / 内存 / 磁盘使用率。

        参数：`request` 表示当前请求，查询参数 ``ids`` 为逗号分隔的机器主键。
        返回：返回 ``{'sampled_at', 'items': [...]}``，按请求里的顺序逐台给结果。
        副作用：**正常情况下只读数据库缓存**；只有缓存过期或还没采到的机器会现场 SSH 补采。

        单独一个接口而不是并进列表：列表是数据库查询，几十毫秒就回来了，而这个常态下走缓存、
        偶尔要逐台 SSH（每台至少 1 秒，CPU 采样两次求差）。并进列表会让翻页跟着一起慢，
        也把「机器表的机器」和「机器还活着吗」这两件事的错误处理搅在一起 ——
        一台机器连不上不该让列表 500。采集口径与集群侧完全共用
        （见 ``k8s_deployment.services.host_usage``），磁盘只报根分区。

        缓存、有效期与后台刷新的分工见 ``k8s_machine.metrics`` 的模块说明。
        """
        raw = str(request.query_params.get('ids', '') or '')
        identifiers = []
        for chunk in raw.split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                value = int(chunk)
            except ValueError:
                raise ValidationError({'ids': f'机器主键必须是数字：{chunk}。'}) from None
            if value not in identifiers:
                identifiers.append(value)
        if not identifiers:
            raise ValidationError({'ids': '请指定要采集的机器。'})
        if len(identifiers) > MAX_USAGE_MACHINES:
            raise ValidationError({'ids': f'一次最多采集 {MAX_USAGE_MACHINES} 台机器。'})

        # 按可见范围取机器：不可见的 id 直接当作不存在，不泄漏「这台机器存在」这件事。
        found = {machine.pk: machine for machine in self.get_queryset().filter(pk__in=identifiers)}
        machines = [found[pk] for pk in identifiers if pk in found]
        if not machines:
            return Response({'sampled_at': '', 'items': []})

        # 顺序按请求里的 ids 走；采不到的那几台也在 items 里，带着失败原因，界面据此显示「—」。
        items, sampled_at = metrics.cached_items(machines)
        return Response({'sampled_at': sampled_at, 'items': items})

    @action(detail=False, methods=['post'])
    def assign(self, request):
        """把选中机器批量绑定到某个集群部署，并写入各自的集群角色。

        参数：`request` 表示当前绑定请求，请求体需包含 `cluster_name` 与 `nodes`（元素为 `{'id': 机器主键, 'roles': [角色代码]}）。
        返回：返回本次绑定的机器数量与更新后的机器数据。
        副作用：修改机器表的角色与所属集群，并为每台机器写入审计日志。
        """
        cluster_name = (request.data.get('cluster_name') or '').strip()
        if not cluster_name:
            return Response({'detail': '缺少部署名称，无法绑定节点。'}, status=status.HTTP_400_BAD_REQUEST)

        raw_nodes = request.data.get('nodes')
        if not isinstance(raw_nodes, list) or not raw_nodes:
            return Response({'detail': '请至少选择一台机器。'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = []
        machine_ids = set()
        for item in raw_nodes:
            if not isinstance(item, dict):
                return Response({'detail': '节点数据格式不正确，请刷新页面后重试。'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                machine_id = int(item.get('id'))
            except (TypeError, ValueError):
                return Response({'detail': '节点数据缺少合法的机器 ID。'}, status=status.HTTP_400_BAD_REQUEST)
            roles = normalize_machine_roles(item.get('roles'))
            if not roles:
                return Response({'detail': '每台机器至少要分配一个集群角色。'}, status=status.HTTP_400_BAD_REQUEST)
            if machine_id in machine_ids:
                continue
            machine_ids.add(machine_id)
            parsed.append((machine_id, roles))

        # 只能绑定当前用户组织范围内可见的机器，越权或已删除的机器一律拒绝。
        machines = {item.pk: item for item in self.get_queryset().filter(pk__in=machine_ids)}
        if machine_ids - set(machines):
            return Response({'detail': '部分机器不存在或不在当前组织范围内，请刷新后重试。'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            updated = []
            for machine_id, roles in parsed:
                machine = machines[machine_id]
                previous_roles = machine.normalized_roles()
                previous_cluster_name = machine.cluster_name
                machine.set_roles(roles)
                machine.cluster_name = cluster_name
                machine.save(update_fields=['roles', 'cluster_name', 'updated_at'])
                write_machine_audit(request, 'assign', machine, {
                    'id': machine.pk,
                    'cluster_name': cluster_name,
                    'roles': machine.roles,
                    'previous_roles': previous_roles,
                    'previous_cluster_name': previous_cluster_name,
                })
                updated.append(machine)

        return Response({
            'cluster_name': cluster_name,
            'assigned': len(updated),
            'machines': self.get_serializer(updated, many=True).data,
        })

    @action(detail=False, methods=['post'])
    def unassign(self, request):
        """把机器从集群部署中解绑，清空角色与所属集群。

        参数：`request` 表示当前解绑请求，请求体需包含 `cluster_name` 与 `ids`（机器主键列表）。
        返回：返回本次实际解绑的机器数量与机器主键。
        副作用：修改机器表的角色与所属集群，并为每台机器写入审计日志。
        """
        cluster_name = (request.data.get('cluster_name') or '').strip()
        raw_ids = request.data.get('ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response({'detail': '请至少选择一台机器。'}, status=status.HTTP_400_BAD_REQUEST)

        machine_ids = set()
        for item in raw_ids:
            try:
                machine_ids.add(int(item))
            except (TypeError, ValueError):
                return Response({'detail': '节点数据缺少合法的机器 ID。'}, status=status.HTTP_400_BAD_REQUEST)

        candidates = self.get_queryset().filter(pk__in=machine_ids)
        if cluster_name:
            # 只解绑确实属于该部署的机器，避免误清其它集群的绑定。
            candidates = candidates.filter(cluster_name=cluster_name)

        with transaction.atomic():
            unassigned_ids = []
            for machine in candidates:
                previous_roles = machine.normalized_roles()
                previous_cluster_name = machine.cluster_name
                machine.set_roles([])
                machine.cluster_name = ''
                machine.save(update_fields=['roles', 'cluster_name', 'updated_at'])
                write_machine_audit(request, 'unassign', machine, {
                    'id': machine.pk,
                    'previous_roles': previous_roles,
                    'previous_cluster_name': previous_cluster_name,
                })
                unassigned_ids.append(machine.pk)

        return Response({'unassigned': len(unassigned_ids), 'ids': unassigned_ids})

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """检测机器连通性：已托管密码时做 SSH 登录校验，否则只探测端口。

        参数：`request` 表示当前检测请求；`pk` 表示机器主键。
        返回：返回更新后的机器数据。
        副作用：建立短时 TCP 或 SSH 连接并更新机器状态；检测说明不回显密码。
        """
        machine = self.get_object()
        try:
            password = machine.get_password()
        except Exception:
            password = None

        if password is None:
            success = False
            message = 'SSH 密码解密失败，请重新设置该机器的密码'
            auth_mode = 'decrypt_failed'
        else:
            auth_mode = 'ssh_password' if password else 'port_only'
            try:
                if password:
                    result = run_ssh_password_auth(machine.ip_address, machine.ssh_port, machine.ssh_user, password)
                else:
                    result = run_telnet(machine.ip_address, machine.ssh_port)
                success = bool(result.get('success'))
                message = result.get('detail') or ('检测通过' if success else '检测未通过')
                if auth_mode == 'port_only':
                    message = f'{message}（未托管 SSH 密码，本次只检测端口）'
            except Exception as exc:
                success = False
                message = f'检测失败：{exc}'

        machine.mark_test_result(success, message)
        write_machine_audit(self.request, 'test', machine, {
            'id': machine.pk,
            'auth_mode': auth_mode,
            'status': machine.status,
            'message': machine.last_test_message,
        })
        return Response(self.get_serializer(machine).data)

