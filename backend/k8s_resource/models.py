from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from ops.models import Organization, TimeStampedModel


class K8sNamespacePolicy(TimeStampedModel):
    """Namespace-level business authorization for K8s resources.

    Platform menu permissions decide whether a user can enter a K8s page; this
    model narrows the actual cluster/namespace/resource/action scope.
    """

    ACTION_SCOPES = [
        ('read', '只读'),
        ('write', '读写'),
        ('admin', '管理'),
    ]
    STATUSES = [
        ('available', '可用'),
        ('disabled', '已禁用'),
        ('expired', '已过期'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_namespace_policies', verbose_name='组织')
    name = models.CharField(max_length=160, verbose_name='规则名称', help_text='namespace 授权规则名称')
    users = models.ManyToManyField(User, blank=True, related_name='k8s_namespace_policies', verbose_name='授权用户')
    department = models.ForeignKey(Organization, on_delete=models.SET_NULL, null=True, blank=True, related_name='k8s_department_namespace_policies', verbose_name='授权部门')
    role = models.CharField(max_length=64, blank=True, verbose_name='授权角色', help_text='匹配 UserProfile.role，例如 sre、viewer')
    cluster = models.ForeignKey('k8s_cluster.K8sCluster', on_delete=models.CASCADE, related_name='namespace_policies', verbose_name='集群')
    namespace = models.CharField(max_length=160, blank=True, verbose_name='命名空间', help_text='为空或 * 表示全部 namespace')
    resource_types = models.JSONField(default=list, blank=True, verbose_name='资源类型', help_text='为空或包含 * 表示全部资源类型')
    action_scope = models.CharField(max_length=32, choices=ACTION_SCOPES, default='read', verbose_name='动作范围')
    valid_from = models.DateTimeField(default=timezone.now, verbose_name='生效时间')
    valid_until = models.DateTimeField(null=True, blank=True, verbose_name='过期时间')
    status = models.CharField(max_length=32, choices=STATUSES, default='available', verbose_name='状态')
    remark = models.TextField(blank=True, verbose_name='备注')

    class Meta:
        ordering = ['cluster_id', 'namespace', 'name']
        indexes = [
            models.Index(fields=['organization', 'status'], name='k8s_ns_policy_org_status_idx'),
            models.Index(fields=['cluster', 'namespace'], name='k8s_ns_policy_scope_idx'),
        ]

    def __str__(self):
        return f'{self.cluster_id}:{self.namespace or "*"}:{self.action_scope}'


class K8sResourceSnapshot(TimeStampedModel):
    """Cached aggregate resource snapshot for overview/statistics acceleration."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_resource_snapshots', verbose_name='组织')
    cluster = models.ForeignKey('k8s_cluster.K8sCluster', on_delete=models.CASCADE, related_name='resource_snapshots', verbose_name='集群')
    snapshot_type = models.CharField(max_length=32, default='statistics', verbose_name='快照类型')
    payload = models.JSONField(default=dict, blank=True, verbose_name='快照数据')
    status = models.CharField(max_length=32, default='completed', verbose_name='状态')
    message = models.CharField(max_length=240, blank=True, verbose_name='结果消息')
    generated_at = models.DateTimeField(default=timezone.now, verbose_name='生成时间')

    class Meta:
        ordering = ['-generated_at']
        indexes = [models.Index(fields=['cluster', 'snapshot_type', 'generated_at'], name='k8s_snapshot_scope_idx')]

    def __str__(self):
        return f'{self.cluster_id} {self.snapshot_type} {self.status}'


class K8sDashboardSnapshot(TimeStampedModel):
    """总览页连集群取数结果的缓存，每 15 分钟才允许重新取一次。

    缓存的是「对全部活跃集群的未过滤计数」，与请求者无关 —— 因此可以全平台共用一行；
    请求侧仍按组织隔离取出该用户可见的集群子集，再从这份 payload 里挑对应的条目。
    权限相关的部分（可见集群集合、命名空间计数口径）不落这张表，避免把第一个访问者的
    权限范围固化给所有人。
    """

    scope = models.CharField(max_length=64, unique=True, default='global', verbose_name='缓存作用域', help_text='当前固定为 global，全平台共用一份')
    payload = models.JSONField(default=dict, blank=True, verbose_name='缓存数据', help_text='按 cluster_id 索引的各集群计数与失败原因')
    generated_at = models.DateTimeField(default=timezone.now, verbose_name='生成时间', help_text='这份缓存取自哪个时刻，超过有效期即重新连集群取数')

    class Meta:
        ordering = ['-generated_at']

    def __str__(self):
        return f'{self.scope} {self.generated_at}'
