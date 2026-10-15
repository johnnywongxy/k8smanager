from django.db import models
from django.utils import timezone

from k8s_cluster.models import K8sCluster
from k8s_machine.models import Machine
from ops.models import Organization, TimeStampedModel

# 事件与日志共用的级别档位，用于前端着色和按级别过滤。
LOG_LEVELS = [
    ('info', '信息'),
    ('warn', '警告'),
    ('error', '错误'),
]

# 非终态任务状态，用于判断任务是否仍占用机器或集群。
NON_TERMINAL_STATUSES = ('draft', 'validating', 'queued', 'running')

# 任务终态，用于判断是否允许重试或清理。
TERMINAL_STATUSES = ('succeeded', 'failed', 'cancelled', 'rollback_required')

# 作用于「已存在集群」的任务类型。这些任务的 kubeasz 集群目录名必须取自集群记录，
# 而不是任务自身的 slug——因为集群 slug 全局唯一，第二个任务不可能复用同一个值。
CLUSTER_TARGETED_TASKS = ('add_master', 'add_worker', 'upgrade', 'backup', 'restore', 'destroy')


class K8sDeployment(TimeStampedModel):
    """Kubernetes 集群部署任务，保存向导提交时的不可变参数快照与执行进展。"""

    TASK_TYPES = [
        ('deploy_aio', '新建单节点集群'),
        ('deploy_ha', '新建多节点集群'),
        ('add_master', '添加 Master'),
        ('add_worker', '添加 Node'),
        # 「版本升级」而不是「补丁升级」：平台支持的不只是同次版本内打补丁，还包括升到
        # 下一个次版本（1.35 → 1.36）。叫法只写「补丁」时，用户按「升级」去界面上找
        # 是找不到它的（这正是本轮反馈里的原话：「怎么没有这个功能」）。
        ('upgrade', '版本升级'),
        # 备份也走任务队列：它要在目标集群上跑 ezctl backup，同样独占集群（规划 6.1），
        # 如果放在视图里同步执行，就会和正在运行的升级任务抢同一套 kubeasz 工作区。
        ('backup', '集群备份'),
        ('restore', '备份恢复'),
        # 清理集群：跑 kubeasz 的 ``ezctl destroy``，把节点上的 k8s 组件与数据清掉、机器解绑，
        # 让这批机器能重新部署。它作用在集群上（要拿集群的 inventory 与配置版本），因此
        # 与扩容/升级同属「作用于已存在集群」的一类。
        ('destroy', '清理集群'),
    ]
    STATUSES = [
        ('draft', '草稿'),
        ('validating', '预检中'),
        ('queued', '排队中'),
        ('running', '执行中'),
        ('succeeded', '成功'),
        ('failed', '失败'),
        ('cancelled', '已取消'),
        ('rollback_required', '需要人工回滚'),
    ]
    EXECUTION_MODES = [
        ('native', '真实执行'),
        ('simulated', '模拟执行'),
    ]
    TOPOLOGY_MODES = [
        ('aio', '单节点'),
        ('ha', '多节点'),
    ]
    NODE_NAMINGS = [
        ('auto', '自动命名'),
        ('manual', '手动命名'),
    ]
    # 三种模式都留着，但**不是每个 kubeasz 版本都支持**：kubeasz 3.7.0（k8s 1.36）的集群
    # 模板只认 iptables 与 nftables，3.6.x 只认 iptables 与 ipvs。哪一套版本能用哪些模式由
    # ``services.preflight.kubeasz_proxy_modes`` 说了算，提交与预检都要过那一关；这里只列
    # 平台认识的全部取值（历史任务里可能存着任意一种）。
    KUBE_PROXY_MODES = [
        ('iptables', 'iptables'),
        ('ipvs', 'ipvs'),
        ('nftables', 'nftables'),
    ]
    CNI_PLUGINS = [
        ('calico', 'Calico'),
        ('flannel', 'Flannel'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='k8s_deployments',
        verbose_name='组织',
        help_text='任务所属组织，用于数据隔离。',
    )
    cluster = models.ForeignKey(
        K8sCluster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deployments',
        verbose_name='目标集群',
        help_text='任务执行成功后关联的集群记录；新建集群类任务在创建时该字段为空。',
    )
    task_type = models.CharField(max_length=32, choices=TASK_TYPES, default='deploy_aio', verbose_name='任务类型')
    status = models.CharField(max_length=32, choices=STATUSES, default='queued', verbose_name='任务状态')

    display_name = models.CharField(max_length=160, verbose_name='部署名称', help_text='向导填写的展示名称，允许中文。')
    slug = models.CharField(
        max_length=40,
        unique=True,
        verbose_name='集群标识',
        help_text='由后端从部署名称派生，作为 kubeasz 集群目录名，全局唯一且不可变。',
    )
    kubernetes_version = models.CharField(max_length=40, verbose_name='Kubernetes 版本')
    kubeasz_version = models.CharField(
        max_length=40,
        blank=True,
        verbose_name='kubeasz 版本',
        help_text='创建任务时从配置中心的版本对应关系推导并固化，不随配置中心后续改动。',
    )
    runtime = models.CharField(max_length=40, default='containerd', verbose_name='容器运行时')
    os_type = models.CharField(max_length=80, verbose_name='操作系统类型')
    os_version = models.CharField(max_length=80, verbose_name='操作系统版本')
    topology_mode = models.CharField(max_length=16, choices=TOPOLOGY_MODES, default='aio', verbose_name='拓扑模式')
    node_naming = models.CharField(max_length=16, choices=NODE_NAMINGS, default='auto', verbose_name='节点命名模式')
    network_interface = models.CharField(max_length=80, default='eth0', verbose_name='节点网口')
    data_dir_enabled = models.BooleanField(default=False, verbose_name='启用自定义数据目录')
    data_dir = models.CharField(max_length=240, blank=True, verbose_name='数据目录路径')
    # 默认勾选：机器上有上一次安装的残留时，平台先清掉再装（典型是 94 上那次 ——
    # 旧集群的 etcd 数据让新集群的 etcd 以 invalid downgrade 秒退，只报「退出码 1」）。
    # 默认关掉的话，用户碰到的还是同一个哑巴失败，然后再被预检挡回来手动清一遍。
    # 清理范围只限 k8s 自己管的路径，容器运行时的镜像缓存保留，见 services/stale_cleanup.py。
    auto_clean_stale = models.BooleanField(default=True, verbose_name='部署前清理上一次安装的残留')
    # 默认值取 iptables 而不是 ipvs：它是**唯一被每一行 kubeasz 版本都支持**的模式
    # （3.7.0 起模板不再支持 ipvs）。默认值恰恰是调用方不会去多想的那一个，落在 ipvs 上
    # 会让「没传模式的 API 调用」在 1.36 集群上必然被提交校验或预检拦下。
    kube_proxy_mode = models.CharField(max_length=16, choices=KUBE_PROXY_MODES, default='iptables', verbose_name='Kube-proxy 模式')
    pod_cidr = models.CharField(max_length=64, verbose_name='容器网络 CIDR')
    service_cidr = models.CharField(max_length=64, verbose_name='Service IP 段')
    api_server_port = models.PositiveIntegerField(default=6443, verbose_name='ApiServer 端口')
    cni_plugin = models.CharField(max_length=32, choices=CNI_PLUGINS, default='calico', verbose_name='容器网络插件')
    metrics_server = models.BooleanField(default=True, verbose_name='安装 Metrics Server')
    nginx_ingress = models.BooleanField(default=True, verbose_name='安装 Nginx Ingress')
    install_helm_cli = models.BooleanField(default=True, verbose_name='安装 Helm CLI')
    # 取值与 ``compiler.LB_MODE_*`` 一致（models 不反向 import services，因此这里写字面量）：
    #   none  = 不使用，kubeconfig 直连第一台 master；
    #   self  = 平台在两台独立机器上装 l4lb（nginx）+ keepalived，对外提供手工填的 VIP；
    #   cloud = 用云厂商的内网负载均衡器，平台不装任何东西，地址只进 apiserver 证书 SAN。
    lb_mode = models.CharField(
        max_length=16,
        choices=(('none', '不使用'), ('self', '自建 l4lb + keepalived'), ('cloud', '云内网负载均衡')),
        default='none',
        verbose_name='外部访问入口',
        help_text='多节点集群的可选入口层，单节点集群不支持。自建模式需要至少两台独立的负载均衡机器。',
    )
    lb_vip = models.CharField(
        max_length=45,
        blank=True,
        verbose_name='外部访问地址',
        help_text='由操作员手工填写：自建模式填承载 VIP 的机器内网网段（如 eth0）中的一个空闲 IPv4；'
                  '云内网负载均衡模式填云负载均衡器的地址（其监听端口由你自行配置，必须等于本集群的 '
                  'ApiServer 端口）。平台不会自动生成，也不校验云上是否已配好。',
    )
    cert_validity_enabled = models.BooleanField(default=False, verbose_name='自定义证书有效期')
    ca_validity_years = models.PositiveIntegerField(
        default=10,
        verbose_name='根证书有效年限',
        help_text='写进 kubeasz 的 CA_EXPIRY。根证书签发下面所有证书，年限必须不小于组件证书。',
    )
    cert_validity_years = models.PositiveIntegerField(
        default=3,
        verbose_name='客户端与组件证书有效年限',
        help_text='写进 kubeasz 的 CERT_EXPIRY。apiserver、etcd、kubelet、kube-proxy、calico、harbor '
                  '的服务端与客户端证书共用这一个值 —— kubeasz 只提供这一个变量，无法按组件分别设置。',
    )
    cluster_description = models.TextField(blank=True, verbose_name='集群描述')

    request_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='请求快照',
        help_text='向导提交的原始请求体与规范化结果，创建后不再改写，用于追溯部署时的真实输入。',
    )

    execution_mode = models.CharField(
        max_length=16,
        choices=EXECUTION_MODES,
        default='native',
        verbose_name='执行模式',
        help_text='真实执行表示调用本机 kubeasz；模拟执行表示当前环境不具备 Ansible，仅推进流程不触碰目标机器。',
    )
    execution_mode_reason = models.CharField(max_length=240, blank=True, verbose_name='执行模式说明')
    current_stage = models.CharField(max_length=64, blank=True, verbose_name='当前阶段')
    completed_stages = models.JSONField(default=list, blank=True, verbose_name='已完成阶段')
    error_code = models.CharField(max_length=64, blank=True, verbose_name='错误码')
    failure_reason = models.TextField(blank=True, verbose_name='失败原因')
    result_summary = models.JSONField(default=dict, blank=True, verbose_name='结果摘要')

    expected_config_revision = models.PositiveIntegerField(
        default=0,
        verbose_name='预期配置版本号',
        help_text='任务提交时目标集群的配置版本号；真正执行时若已变化则以 stale_cluster_revision 失败。',
    )

    skip_emergency_backup = models.BooleanField(
        default=False,
        verbose_name='跳过恢复前应急备份',
        help_text='仅恢复任务使用：集群已经不可用时允许用户跳过恢复前的应急备份，避免恢复被卡死。',
    )

    worker_id = models.CharField(max_length=120, blank=True, verbose_name='执行 Worker 标识')
    heartbeat_at = models.DateTimeField(null=True, blank=True, verbose_name='心跳时间')
    queued_at = models.DateTimeField(default=timezone.now, verbose_name='入队时间')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    cancel_requested = models.BooleanField(default=False, verbose_name='已请求取消')
    retry_count = models.PositiveIntegerField(default=0, verbose_name='重试次数')
    requested_by = models.CharField(max_length=120, blank=True, verbose_name='提交人')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status'], name='k8s_deploy_org_status_idx'),
            models.Index(fields=['cluster', 'status'], name='k8s_deploy_cluster_status_idx'),
            models.Index(fields=['status', 'queued_at'], name='k8s_deploy_queue_idx'),
        ]

    def __str__(self):
        """返回部署名称与任务的组合标识。"""
        return f'{self.display_name}({self.task_type})'

    @property
    def cluster_slug(self):
        """返回本次任务对应的 kubeasz 集群目录名。

        参数：无。
        返回：作用于已存在集群的任务返回集群记录的 slug，其余（新建类）返回任务自身的 slug。
        副作用：集群未预取时触发一次数据库查询，不修改数据。

        `slug` 字段全局唯一，是**任务**的标识；增量任务与集群是一对多关系，因此不能拿它当
        集群目录名，否则同一个集群的第二个任务就会因为 slug 冲突而无法创建。
        """
        if self.task_type in CLUSTER_TARGETED_TASKS and self.cluster_id:
            return self.cluster.slug
        return self.slug

    @property
    def is_terminal(self):
        """判断任务是否已进入终态。

        参数：无。
        返回：任务处于成功、失败、取消或待回滚时返回真。
        副作用：只读取本机对象，不修改持久化数据。
        """
        return self.status in TERMINAL_STATUSES

    @property
    def is_simulated(self):
        """判断本次任务是否为模拟执行。

        参数：无。
        返回：执行模式为 simulated 时返回真。
        副作用：只读取本机对象，不修改持久化数据。
        """
        return self.execution_mode == 'simulated'

    @property
    def cluster_node_count(self):
        """返回本次任务的**集群节点**数量（不含专用负载均衡机器）。

        参数：无。
        返回：返回「还承担着别的角色、或者压根没填角色」的节点快照数量。
        副作用：触发一次节点快照查询，不修改持久化数据。

        负载均衡机器与集群节点共用 ``K8sDeploymentNode`` 表，但它们不出现在
        ``kubectl get node`` 里。凡是把节点数展示给用户或写进集群记录的地方
        （任务详情、集群列表、``register.ensure_deployed_cluster``）都该用这个值，
        否则会出现「3 master 的集群有 5 个节点」这种对不上的数字。

        判据是「**只**当负载均衡」，不是「含有负载均衡」：三机拓扑里那两台既当 master
        （+etcd）又当负载均衡，它们照样出现在 ``kubectl get node`` 里。按后一种写法数，
        一个三台机器的高可用集群会被报成只有 1 个节点。角色名与
        ``services/compiler.py`` 的 ``CLUSTER_ROLES`` 是一回事，这里用字面量是为了保持
        models 不反向依赖 services 的方向。
        """
        roles_of_cluster = ('master', 'node', 'etcd')
        count = 0
        for item in self.nodes.all():
            roles = item.roles or []
            # 没填角色的照旧算进去（历史数据里有这种记录）；只当负载均衡的不算。
            if not roles or any(role in roles for role in roles_of_cluster):
                count += 1
        return count


class K8sDeploymentNode(TimeStampedModel):
    """部署任务的目标节点快照，创建后不随机器表的后续改动而变化。"""

    STATUSES = [
        ('pending', '待执行'),
        ('ready', '已就绪'),
        ('failed', '失败'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='k8s_deployment_nodes',
        verbose_name='组织',
    )
    deployment = models.ForeignKey(
        K8sDeployment,
        on_delete=models.CASCADE,
        related_name='nodes',
        verbose_name='部署任务',
    )
    machine = models.ForeignKey(
        Machine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deployment_nodes',
        verbose_name='来源机器',
        help_text='机器被删除后置空，任务节点快照仍然保留。',
    )
    name = models.CharField(max_length=160, verbose_name='主机名')
    ip_address = models.GenericIPAddressField(verbose_name='机器 IP')
    cluster_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        verbose_name='集群内部地址',
        help_text='节点网卡上真实存在的地址，kubeasz 用它做 etcd 与 apiserver 的监听和广播。'
                  '节点在 NAT 后面时它与上面填写的 IP 不同，留空表示两者相同。',
    )
    ssh_port = models.PositiveIntegerField(default=22, verbose_name='SSH 端口')
    ssh_user = models.CharField(max_length=120, default='root', verbose_name='登录用户名')
    roles = models.JSONField(default=list, blank=True, verbose_name='集群角色')
    node_name = models.CharField(
        max_length=160,
        blank=True,
        verbose_name='节点名称',
        help_text='写入 kubeasz inventory 的主机别名，自动命名模式下由平台生成。',
    )
    status = models.CharField(max_length=16, choices=STATUSES, default='pending', verbose_name='节点状态')
    failure_reason = models.CharField(max_length=240, blank=True, verbose_name='失败原因')

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['deployment', 'status'], name='k8s_deploy_node_dep_status_idx'),
        ]

    def __str__(self):
        """返回主机名与 IP 组成的节点标识。"""
        return f'{self.name}({self.ip_address})'


class K8sDeploymentEvent(TimeStampedModel):
    """部署任务的阶段事件，前端按自增主键增量拉取。"""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='k8s_deployment_events',
        verbose_name='组织',
    )
    deployment = models.ForeignKey(
        K8sDeployment,
        on_delete=models.CASCADE,
        related_name='events',
        verbose_name='部署任务',
    )
    stage = models.CharField(max_length=64, blank=True, verbose_name='阶段')
    level = models.CharField(max_length=16, choices=LOG_LEVELS, default='info', verbose_name='级别')
    event_type = models.CharField(max_length=64, blank=True, verbose_name='事件类型')
    message = models.TextField(blank=True, verbose_name='事件说明')
    detail = models.JSONField(default=dict, blank=True, verbose_name='事件详情')

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['deployment', 'id'], name='k8s_deploy_event_dep_id_idx'),
        ]

    def __str__(self):
        """返回阶段与事件类型的组合标识。"""
        return f'{self.stage or "任务"}:{self.event_type or "event"}'


class K8sDeploymentLog(TimeStampedModel):
    """部署任务的执行日志，按任务内自增 seq 增量拉取。"""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='k8s_deployment_logs',
        verbose_name='组织',
    )
    deployment = models.ForeignKey(
        K8sDeployment,
        on_delete=models.CASCADE,
        related_name='logs',
        verbose_name='部署任务',
    )
    seq = models.PositiveIntegerField(
        verbose_name='任务内序号',
        help_text='任务内从 1 开始单调自增；批量写入的主键可能有空洞，因此增量拉取以本字段为准。',
    )
    stage = models.CharField(max_length=64, blank=True, verbose_name='阶段')
    level = models.CharField(max_length=16, choices=LOG_LEVELS, default='info', verbose_name='级别')
    stream = models.CharField(
        max_length=16,
        default='stdout',
        verbose_name='输出流',
        help_text='stdout 与 stderr 来自子进程，system 来自平台自身的阶段说明。',
    )
    message = models.TextField(blank=True, verbose_name='日志内容')

    class Meta:
        ordering = ['seq']
        constraints = [
            models.UniqueConstraint(fields=['deployment', 'seq'], name='uniq_k8s_deploy_log_seq'),
        ]
        indexes = [
            models.Index(fields=['deployment', 'seq'], name='k8s_deploy_log_dep_seq_idx'),
        ]

    def __str__(self):
        """返回任务与序号的组合标识。"""
        return f'{self.deployment_id}#{self.seq}'


class K8sDeploymentArtifactUse(TimeStampedModel):
    """部署任务实际使用的 OSS 制品记录，含哈希校验结果。"""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='k8s_deployment_artifact_uses',
        verbose_name='组织',
    )
    deployment = models.ForeignKey(
        K8sDeployment,
        on_delete=models.CASCADE,
        related_name='artifact_uses',
        verbose_name='部署任务',
    )
    release = models.CharField(max_length=40, verbose_name='kubeasz 版本')
    object_key = models.CharField(max_length=500, verbose_name='OSS 对象键')
    local_path = models.CharField(max_length=500, blank=True, verbose_name='本地缓存路径')
    sha256 = models.CharField(max_length=64, blank=True, verbose_name='内容哈希')
    size_bytes = models.BigIntegerField(default=0, verbose_name='字节数')
    verified = models.BooleanField(default=False, verbose_name='校验通过')
    cache_hit = models.BooleanField(default=False, verbose_name='命中缓存')
    error_message = models.CharField(max_length=240, blank=True, verbose_name='错误说明')

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['deployment', 'release'], name='k8s_deploy_art_dep_rel_idx'),
        ]

    def __str__(self):
        """返回版本与对象键的组合标识。"""
        return f'{self.release}:{self.object_key}'


class K8sClusterBackup(TimeStampedModel):
    """集群级备份记录，保存上传到 OSS 的归档元数据（本轮只落地表结构）。"""

    STATUSES = [
        ('pending', '待执行'),
        ('running', '备份中'),
        ('succeeded', '成功'),
        ('failed', '失败'),
        ('deleting', '删除中'),
        ('deleted', '已删除'),
        ('delete_failed', '删除失败'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='k8s_cluster_backups',
        verbose_name='组织',
    )
    cluster = models.ForeignKey(
        K8sCluster,
        on_delete=models.CASCADE,
        related_name='backups',
        verbose_name='集群',
    )
    deployment = models.ForeignKey(
        K8sDeployment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='backups',
        verbose_name='产生备份的任务',
    )
    backup_id = models.CharField(max_length=64, unique=True, verbose_name='备份标识')
    slug = models.CharField(max_length=40, verbose_name='集群标识')
    kubernetes_version = models.CharField(max_length=40, blank=True, verbose_name='Kubernetes 版本')
    kubeasz_version = models.CharField(max_length=40, blank=True, verbose_name='kubeasz 版本')
    config_revision = models.PositiveIntegerField(default=0, verbose_name='配置版本号')
    object_prefix = models.CharField(max_length=500, blank=True, verbose_name='OSS 对象前缀')
    archive_size = models.BigIntegerField(default=0, verbose_name='归档密文字节数')
    archive_sha256 = models.CharField(max_length=64, blank=True, verbose_name='归档密文哈希')
    schema_version = models.CharField(max_length=16, default='1', verbose_name='归档结构版本')
    status = models.CharField(max_length=16, choices=STATUSES, default='pending', verbose_name='备份状态')
    failure_reason = models.TextField(blank=True, verbose_name='失败原因')
    is_protected = models.BooleanField(
        default=False,
        verbose_name='受保护',
        help_text='最近的若干次成功备份会被标记为受保护，不受保留期清理影响。',
    )
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='过期时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['cluster', 'status'], name='k8s_backup_cluster_status_idx'),
        ]

    def __str__(self):
        """返回备份标识与集群标识的组合文本。"""
        return f'{self.slug}:{self.backup_id}'
