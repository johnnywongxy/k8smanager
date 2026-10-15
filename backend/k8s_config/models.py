from django.db import models

from ops.models import TimeStampedModel


class K8sVersionMapping(TimeStampedModel):
    """维护 Kubernetes 版本与 kubeasz 版本的对应关系，供集群部署时选择。

    该配置属于平台级公共配置：所有部门部署集群时共用同一份版本清单。
    """

    kubernetes_version = models.CharField(
        max_length=40,
        unique=True,
        verbose_name='Kubernetes 版本',
        help_text='部署向导中展示的 Kubernetes 版本号，例如 1.36。',
    )
    kubeasz_version = models.CharField(
        max_length=40,
        verbose_name='kubeasz 版本',
        help_text='与该 Kubernetes 版本配套的 kubeasz 版本号，例如 3.7.0。',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='是否启用',
        help_text='停用后不再出现在集群部署向导的版本列表中，历史部署记录不受影响。',
    )
    sort_order = models.IntegerField(
        default=0,
        verbose_name='排序',
        help_text='数值越小越靠前，部署向导按该顺序展示版本。',
    )
    remark = models.TextField(blank=True, verbose_name='备注', help_text='版本适配说明或升级注意事项。')
    supported_systems = models.ManyToManyField(
        'SupportedOperatingSystem',
        blank=True,
        related_name='supported_k8s_versions',
        verbose_name='支持的操作系统',
        help_text='该 Kubernetes/kubeasz 版本具备离线系统包并允许部署的操作系统。',
    )

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Kubernetes 版本对应关系'
        verbose_name_plural = verbose_name

    def __str__(self):
        """返回便于在日志和后台中识别的版本对应关系说明。

        参数：无。
        返回：返回 Kubernetes 与 kubeasz 的版本组合文本。
        副作用：不直接修改持久化数据。
        """
        return f'Kubernetes {self.kubernetes_version} / kubeasz {self.kubeasz_version}'


class SupportedOperatingSystem(TimeStampedModel):
    """维护部署 Kubernetes 集群时支持的操作系统清单。

    该配置属于平台级公共配置：所有部门部署集群时共用同一份系统清单。
    """

    name = models.CharField(
        max_length=160,
        blank=True,
        verbose_name='系统全称',
        help_text='由「操作系统类型 + 版本」自动拼接，用于部署向导展示和部署记录留痕，无需手工填写。',
    )
    os_type = models.CharField(
        max_length=80,
        verbose_name='操作系统类型',
        help_text='部署向导左侧展示的系统大类，例如 Ubuntu、Rocky Linux。',
    )
    version = models.CharField(
        max_length=80,
        verbose_name='版本',
        help_text='部署向导右侧展示的版本号，例如 22.04、8，需与该类型的软件源匹配。',
    )
    notes_url = models.URLField(
        blank=True,
        verbose_name='说明链接',
        help_text='该系统的安装注意事项文档地址，留空表示没有额外说明。',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='是否启用',
        help_text='停用后不再出现在集群部署向导的系统列表中，历史部署记录不受影响。',
    )
    sort_order = models.IntegerField(
        default=0,
        verbose_name='排序',
        help_text='数值越小越靠前，部署向导按该顺序展示系统。',
    )
    remark = models.TextField(blank=True, verbose_name='备注', help_text='系统适配范围或版本限制说明。')

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = '支持的操作系统'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=['os_type', 'version'],
                name='uniq_k8s_os_type_and_version',
            ),
        ]

    def save(self, *args, **kwargs):
        """保存前按类型和版本同步系统全称。

        参数：`args`、`kwargs` 表示传递给模型保存的原始参数。
        返回：无显式返回值。
        副作用：向配置表写入数据。
        """
        self.name = f'{self.os_type} {self.version}'.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        """返回便于在日志和后台中识别的操作系统名称。

        参数：无。
        返回：返回当前操作系统名称。
        副作用：不直接修改持久化数据。
        """
        return self.name

