from django.db import models

from ops.models import Organization, TimeStampedModel


class K8sQueryHistory(TimeStampedModel):
    """Persist user query history for K8s logs, metrics and statistics."""

    QUERY_TYPES = [
        ('logs', '日志查询'),
        ('metrics', '监控查询'),
        ('statistics', '资源统计'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_query_histories', verbose_name='组织')
    query_type = models.CharField(max_length=32, choices=QUERY_TYPES, verbose_name='查询类型')
    cluster_name = models.CharField(max_length=160, blank=True, verbose_name='集群名称')
    namespace = models.CharField(max_length=160, blank=True, verbose_name='命名空间')
    pod = models.CharField(max_length=240, blank=True, verbose_name='Pod')
    container = models.CharField(max_length=160, blank=True, verbose_name='容器')
    source_type = models.CharField(max_length=32, blank=True, verbose_name='数据源类型')
    query = models.TextField(blank=True, verbose_name='查询语句或关键字')
    time_range = models.CharField(max_length=120, blank=True, verbose_name='时间范围')
    status = models.CharField(max_length=32, default='completed', verbose_name='状态')
    actor = models.CharField(max_length=120, blank=True, verbose_name='操作者')
    result_summary = models.JSONField(default=dict, blank=True, verbose_name='结果摘要')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['query_type', 'cluster_name', 'created_at'], name='k8s_query_history_scope_idx'),
        ]

    def __str__(self):
        return f'{self.query_type} {self.cluster_name} {self.status}'


class K8sNotification(TimeStampedModel):
    """In-app K8s alert/notification generated from real cluster state."""

    LEVELS = [
        ('info', '信息'),
        ('warning', '警告'),
        ('critical', '严重'),
    ]
    STATUSES = [
        ('unread', '未读'),
        ('acknowledged', '已确认'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_notifications', verbose_name='组织')
    cluster_name = models.CharField(max_length=160, blank=True, verbose_name='集群名称')
    namespace = models.CharField(max_length=160, blank=True, verbose_name='命名空间')
    resource_type = models.CharField(max_length=80, blank=True, verbose_name='资源类型')
    resource_name = models.CharField(max_length=240, blank=True, verbose_name='资源名称')
    level = models.CharField(max_length=32, choices=LEVELS, default='warning', verbose_name='级别')
    title = models.CharField(max_length=160, verbose_name='标题')
    message = models.CharField(max_length=500, blank=True, verbose_name='消息')
    status = models.CharField(max_length=32, choices=STATUSES, default='unread', verbose_name='状态')
    acknowledged_by = models.CharField(max_length=120, blank=True, verbose_name='确认人')
    acknowledged_at = models.DateTimeField(null=True, blank=True, verbose_name='确认时间')

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['cluster_name', 'status', 'created_at'], name='k8s_notice_scope_idx')]
