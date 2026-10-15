from django.db import models

from ops.models import Organization, TimeStampedModel


class K8sExecRecord(TimeStampedModel):
    """Audit-style history for one-shot Pod exec commands from the platform."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_exec_records', verbose_name='组织')
    cluster_name = models.CharField(max_length=160, blank=True, verbose_name='集群名称')
    namespace = models.CharField(max_length=160, verbose_name='命名空间')
    pod = models.CharField(max_length=240, verbose_name='Pod')
    container = models.CharField(max_length=160, blank=True, verbose_name='容器')
    command = models.CharField(max_length=500, verbose_name='命令')
    actor = models.CharField(max_length=120, blank=True, verbose_name='操作者')
    status = models.CharField(max_length=32, default='completed', verbose_name='状态')
    output_preview = models.TextField(blank=True, verbose_name='输出预览', help_text='只保存前 2000 字，避免敏感输出长期落库')
    failure_reason = models.CharField(max_length=240, blank=True, verbose_name='失败原因')

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['cluster_name', 'namespace', 'pod'], name='k8s_exec_record_scope_idx')]

    def __str__(self):
        return f'{self.namespace}/{self.pod} {self.status}'
