"""K8s 业务审计的统一写入入口。

平台审计与 K8s 业务审计原先只在 k8s_cluster 的视图里实现，部署任务的视图和后台
Worker 需要写同一种审计，因此抽到本模块统一维护：视图侧用 ``write_k8s_audit``
从请求上下文取操作者，Worker 侧没有请求对象，直接用 ``record_k8s_audit`` 指定
操作者与组织。
"""

from k8s_audit.models import K8sOperationAudit
from ops.permissions import user_organization
from platform_logs.models import AuditLog


def request_ip(request):
    """从请求中解析发起方 IP。

    参数：`request` 表示当前 HTTP 请求对象。
    返回：返回去除反向代理链路后的客户端 IP，取不到时回退到 127.0.0.1。
    副作用：只读取请求头，不修改持久化数据。
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')) or '127.0.0.1'


def record_k8s_audit(
    organization,
    actor,
    action,
    resource,
    ip_address='127.0.0.1',
    detail=None,
    resource_type='cluster',
    result_summary=None,
    status='completed',
    failure_reason='',
):
    """写入一条平台审计和一条 K8s 业务审计。

    参数：`organization` 表示归属组织；`actor` 表示操作者名称；`action` 表示业务动作；
        `resource` 表示被操作的资源对象；`ip_address` 表示发起方 IP；`detail` 表示请求摘要；
        `resource_type` 表示资源类型；`result_summary` 表示结果摘要，传 None 时按集群信息生成；
        `status` 表示执行结果；`failure_reason` 表示失败原因。
    返回：返回本次写入的 K8s 业务审计对象。
    副作用：向平台审计表和 K8s 业务审计表各写入一行。
    """
    resource_label = str(resource)
    AuditLog.objects.create(
        organization=organization,
        actor=actor,
        action=f'k8s.{action}',
        resource=resource_label,
        ip_address=ip_address,
        detail=detail or {},
    )
    return K8sOperationAudit.objects.create(
        organization=organization,
        cluster_name=getattr(resource, 'name', resource_label),
        resource_type=resource_type,
        resource_name=getattr(resource, 'cluster_name', '') or getattr(resource, 'name', resource_label),
        action=action,
        actor=actor,
        status=status,
        request_summary=detail or {},
        result_summary=result_summary if result_summary is not None else {
            'cluster_id': getattr(resource, 'pk', None),
            'status': getattr(resource, 'status', ''),
            'api_server': getattr(resource, 'api_server', ''),
        },
        failure_reason=failure_reason[:240],
    )


def write_k8s_audit(request, action, resource, detail=None, resource_type='cluster', result_summary=None):
    """从请求上下文提取操作者与组织后写入审计。

    参数：`request` 表示当前 HTTP 请求对象；`action` 表示业务动作；`resource` 表示被操作的资源对象；
        `detail` 表示请求摘要；`resource_type` 表示资源类型；`result_summary` 表示结果摘要。
    返回：返回本次写入的 K8s 业务审计对象。
    副作用：向平台审计表和 K8s 业务审计表各写入一行。
    """
    return record_k8s_audit(
        organization=user_organization(request.user),
        actor=getattr(request.user, 'username', 'system') or 'system',
        action=action,
        resource=resource,
        ip_address=request_ip(request),
        detail=detail,
        resource_type=resource_type,
        result_summary=result_summary,
    )
