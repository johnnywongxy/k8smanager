from django.db import models

from ops.models import Organization, TimeStampedModel


class K8sOperationAudit(TimeStampedModel):
    """Business-level K8s operation audit for dangerous and mutating actions."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_operation_audits', verbose_name='组织')
    cluster_name = models.CharField(max_length=160, blank=True, verbose_name='集群名称')
    namespace = models.CharField(max_length=160, blank=True, verbose_name='命名空间')
    resource_type = models.CharField(max_length=80, blank=True, verbose_name='资源类型')
    resource_name = models.CharField(max_length=240, blank=True, verbose_name='资源名称')
    action = models.CharField(max_length=80, verbose_name='操作')
    actor = models.CharField(max_length=120, verbose_name='操作者')
    status = models.CharField(max_length=32, default='completed', verbose_name='状态')
    request_summary = models.JSONField(default=dict, blank=True, verbose_name='请求摘要')
    result_summary = models.JSONField(default=dict, blank=True, verbose_name='结果摘要')
    failure_reason = models.CharField(max_length=240, blank=True, verbose_name='失败原因')

    class Meta:
        ordering = ['-created_at']


class K8sSecretAccessAudit(TimeStampedModel):
    """Sensitive access audit for Secret reveal/download/copy actions."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_secret_access_audits', verbose_name='组织')
    cluster_name = models.CharField(max_length=160, blank=True, verbose_name='集群名称')
    namespace = models.CharField(max_length=160, blank=True, verbose_name='命名空间')
    secret_name = models.CharField(max_length=240, verbose_name='Secret 名称')
    access_type = models.CharField(max_length=80, verbose_name='访问类型')
    actor = models.CharField(max_length=120, verbose_name='操作者')
    result = models.CharField(max_length=32, default='completed', verbose_name='结果')
    detail = models.JSONField(default=dict, blank=True, verbose_name='详情')

    class Meta:
        ordering = ['-created_at']
