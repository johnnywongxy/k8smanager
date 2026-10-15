from django.db import models
from django.utils import timezone

from ops.models import Organization, TimeStampedModel


class K8sCluster(TimeStampedModel):
    """Kubernetes cluster access profile stored by the CMDB platform."""

    AUTH_TYPES = [
        ('kubeconfig', 'KubeConfig'),
        ('token', 'ServiceAccount Token'),
        ('certificate', 'Client Certificate'),
    ]
    STATUSES = [
        ('available', '可用'),
        ('configured', '已配置'),
        ('deploying', '部署中'),
        ('unconfigured', '未配置'),
        ('unavailable', '不可用'),
        ('disabled', '已禁用'),
        # 已销毁：平台跑过 ``ezctl destroy`` 把这个集群从节点上清掉了。记录与配置版本保留作审计，
        # 但标识（``slug``）会被清空——留着它会让同名重装捞到这条记录并原地复活（见
        # ``k8s_deployment.services.register.ensure_deployed_cluster``）。
        ('destroyed', '已销毁'),
    ]
    ORIGINS = [
        ('imported', '导入'),
        ('deployed', '平台部署'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_clusters', verbose_name='组织')
    name = models.CharField(max_length=160, verbose_name='集群名称', help_text='平台内展示的 Kubernetes 集群名称')
    slug = models.CharField(max_length=40, blank=True, db_index=True, verbose_name='集群标识', help_text='平台部署的集群才有：kubeasz 集群目录名，接入型集群留空')
    origin = models.CharField(
        max_length=16,
        choices=ORIGINS,
        default='imported',
        db_index=True,
        verbose_name='来源',
        help_text='导入表示手工接入的已有集群；平台部署表示由部署任务创建，运维入口仅对后者开放',
    )
    cluster_name = models.CharField(max_length=160, blank=True, verbose_name='KubeConfig 集群名', help_text='kubeconfig clusters[].name')
    context_name = models.CharField(max_length=200, blank=True, verbose_name='当前 Context', help_text='kubeconfig current-context')
    user_name = models.CharField(max_length=160, blank=True, verbose_name='认证用户', help_text='kubeconfig contexts[].context.user')
    api_server = models.URLField(max_length=320, blank=True, verbose_name='API Server', help_text='当前用于连接 Kubernetes API 的 server 地址，部署中的集群尚未产出该地址')
    public_api_server = models.URLField(max_length=320, blank=True, verbose_name='公网 API Server', help_text='公网访问地址，例如阿里云 ECS 公网 IP')
    internal_api_server = models.URLField(max_length=320, blank=True, verbose_name='内网 API Server', help_text='内网访问地址，用于平台部署在同 VPC 时连接')
    api_server_vip = models.CharField(
        max_length=45,
        blank=True,
        verbose_name='外部访问地址',
        help_text='高可用集群的外部入口地址：自建模式是 keepalived 抢占的虚拟 IP，云内网负载均衡模式是云负载均衡器的地址；'
                  '不使用外部入口的集群为空',
    )
    lb_mode = models.CharField(
        max_length=16,
        choices=(('none', '不使用'), ('self', '自建 l4lb + keepalived'), ('cloud', '云内网负载均衡')),
        default='none',
        verbose_name='外部访问入口',
        help_text='与部署任务的 lb_mode 同义。集群记录必须自己存一份：只凭 api_server_vip 非空反推会把云 LB 误判成自建',
    )
    auth_type = models.CharField(max_length=32, choices=AUTH_TYPES, default='kubeconfig', verbose_name='认证方式')
    kube_version = models.CharField(max_length=80, blank=True, verbose_name='K8s 版本')
    runtime_version = models.CharField(max_length=120, blank=True, verbose_name='容器运行时')
    network_plugin = models.CharField(max_length=80, blank=True, verbose_name='网络插件')
    node_count = models.PositiveIntegerField(default=0, verbose_name='节点数量')
    status = models.CharField(max_length=32, choices=STATUSES, default='configured', verbose_name='状态')
    last_test_status = models.CharField(max_length=32, blank=True, verbose_name='最近检测状态')
    last_test_message = models.CharField(max_length=240, blank=True, verbose_name='最近检测结果')
    last_tested_at = models.DateTimeField(null=True, blank=True, verbose_name='最近检测时间')
    last_synced_at = models.DateTimeField(null=True, blank=True, verbose_name='最近同步时间')
    remark = models.TextField(blank=True, verbose_name='备注')

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['organization', 'status'], name='k8s_cluster_org_status_idx'),
            models.Index(fields=['api_server'], name='k8s_cluster_api_idx'),
        ]

    def __str__(self):
        return self.name

    def mark_test_result(self, status, message, latency_ms=0, detail=None):
        """Persist the latest connectivity test result and append a health record."""
        self.last_test_status = status
        self.last_test_message = message[:240]
        self.last_tested_at = timezone.now()
        if self.status not in ('disabled', 'destroyed'):
            self.status = status
        self.save(update_fields=['last_test_status', 'last_test_message', 'last_tested_at', 'status', 'updated_at'])
        K8sClusterHealthCheck.objects.create(
            organization=self.organization,
            cluster=self,
            status=status,
            latency_ms=latency_ms,
            message=message[:240],
            detail=detail or {},
        )


class K8sClusterCredential(TimeStampedModel):
    """Encrypted Kubernetes credential bound one-to-one to a cluster."""

    cluster = models.OneToOneField(K8sCluster, on_delete=models.CASCADE, related_name='credential', verbose_name='集群')
    encrypted_kubeconfig = models.TextField(blank=True, verbose_name='KubeConfig 密文', help_text='完整 kubeconfig 加密后存储')
    encrypted_client_certificate = models.TextField(blank=True, verbose_name='客户端证书密文')
    encrypted_client_key = models.TextField(blank=True, verbose_name='客户端私钥密文')
    encrypted_certificate_authority = models.TextField(blank=True, verbose_name='CA 证书密文')
    encrypted_token = models.TextField(blank=True, verbose_name='Token 密文')
    source_path = models.CharField(max_length=260, blank=True, verbose_name='来源文件路径')
    fingerprint = models.CharField(max_length=64, blank=True, verbose_name='凭据指纹', help_text='kubeconfig 原文 SHA256，用于判断是否重复导入')
    rotated_at = models.DateTimeField(null=True, blank=True, verbose_name='最近轮换时间')

    class Meta:
        ordering = ['cluster_id']

    def __str__(self):
        return f'{self.cluster.name} credential'


class K8sClusterConfigRevision(TimeStampedModel):
    """Versioned snapshot of the configuration a deployed cluster currently runs with.

    每次部署任务真正落地一份配置就追加一条：部署任务提交时记录当时的 version，
    开工前再比对一次，不一致说明配置已被别的任务改过，本次执行必须中止，
    否则会把一份过期的 hosts/config.yml 写到已经变化的集群上。
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_cluster_config_revisions', verbose_name='组织')
    cluster = models.ForeignKey(K8sCluster, on_delete=models.CASCADE, related_name='config_revisions', verbose_name='集群')
    revision = models.PositiveIntegerField(verbose_name='版本号', help_text='同一集群内从 1 开始单调递增')
    source_deployment_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='来源部署任务',
        help_text='产生本次配置的部署任务主键；用整型而非外键，避免与 k8s_deployment 形成迁移循环依赖',
    )
    snapshot = models.JSONField(default=dict, blank=True, verbose_name='配置快照', help_text='本次生效的 hosts 与 config.yml 内容及节点清单')
    config_hash = models.CharField(max_length=64, blank=True, verbose_name='配置哈希', help_text='hosts 与 config.yml 归并后的 SHA256，用于判断配置是否真的变化')
    note = models.CharField(max_length=240, blank=True, verbose_name='变更说明')

    class Meta:
        ordering = ['-revision']
        constraints = [
            models.UniqueConstraint(fields=['cluster', 'revision'], name='uniq_k8s_cluster_revision'),
        ]
        indexes = [
            models.Index(fields=['cluster', '-revision'], name='k8s_cfg_rev_cluster_idx'),
        ]

    def __str__(self):
        """Return the cluster name together with its configuration revision number."""
        return f'{self.cluster.name}#{self.revision}'


class K8sClusterHealthCheck(TimeStampedModel):
    """Connectivity test history for a Kubernetes cluster."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='k8s_cluster_health_checks', verbose_name='组织')
    cluster = models.ForeignKey(K8sCluster, on_delete=models.CASCADE, related_name='health_checks', verbose_name='集群')
    status = models.CharField(max_length=32, verbose_name='检测状态')
    latency_ms = models.PositiveIntegerField(default=0, verbose_name='耗时毫秒')
    message = models.CharField(max_length=240, blank=True, verbose_name='检测结果')
    detail = models.JSONField(default=dict, blank=True, verbose_name='检测详情')
    checked_at = models.DateTimeField(default=timezone.now, verbose_name='检测时间')

    class Meta:
        ordering = ['-checked_at']
        indexes = [models.Index(fields=['cluster', 'checked_at'], name='k8s_health_cluster_time_idx')]
