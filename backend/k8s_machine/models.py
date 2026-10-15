from django.db import models
from django.utils import timezone

from ops.models import Organization, TimeStampedModel


class Machine(TimeStampedModel):
    """登记待部署 Kubernetes 集群的 Linux 机器清单，连接信息明文保存、SSH 密码 SM4 加密保存。"""

    ROLES = [
        ('master', 'master'),
        ('node', 'node'),
        ('etcd', 'etcd'),
        ('loadbalancer', '负载均衡'),
    ]
    STATUSES = [
        ('unknown', '未知'),
        ('available', '可用'),
        ('unavailable', '不可用'),
        ('disabled', '已禁用'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='machines',
        verbose_name='组织',
        help_text='机器所属组织，用于数据隔离。',
    )
    name = models.CharField(max_length=160, verbose_name='主机名', help_text='机器在平台内展示的名称，例如 node-01。')
    ip_address = models.GenericIPAddressField(unique=True, verbose_name='机器 IP', help_text='机器的 IPv4 或 IPv6 地址。')
    ssh_port = models.PositiveIntegerField(default=22, verbose_name='SSH 端口', help_text='SSH 服务监听端口，默认 22。')
    ssh_user = models.CharField(max_length=120, default='root', verbose_name='登录用户名', help_text='后续部署集群时使用的 SSH 登录用户名。')
    roles = models.JSONField(
        default=list,
        blank=True,
        verbose_name='机器角色',
        help_text='该机器在集群中承担的角色，可多选（例如 master 与 etcd 同机部署）；空列表表示尚未规划用途，页面展示为“未分配”。',
    )
    cluster_name = models.CharField(
        max_length=160,
        blank=True,
        verbose_name='所属集群',
        help_text='集群部署向导添加节点时写入的部署名称；为空表示该机器尚未分配给任何集群，页面展示为“无”。',
    )
    status = models.CharField(max_length=32, choices=STATUSES, default='unknown', verbose_name='状态', help_text='最近一次连通性检测得到的机器状态。')
    last_test_message = models.CharField(max_length=240, blank=True, verbose_name='最近检测结果', help_text='最近一次连通性检测的结果说明。')
    last_tested_at = models.DateTimeField(null=True, blank=True, verbose_name='最近检测时间', help_text='最近一次连通性检测的发生时间。')
    remark = models.TextField(blank=True, verbose_name='备注', help_text='用途、机房或负责人等补充说明。')
    encrypted_password = models.TextField(
        blank=True,
        verbose_name='SSH 密码密文',
        help_text='SSH 登录密码经 SM4 加密后存储；不保存明文，也不随任何接口返回。',
    )
    password_rotated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='密码最近更新时间',
        help_text='最近一次设置或更换 SSH 密码的时间。',
    )

    class Meta:
        ordering = ['name', 'pk']
        indexes = [
            models.Index(fields=['organization', 'status'], name='k8s_machine_org_status_idx'),
        ]

    def __str__(self):
        """返回主机名和 IP 组成的机器标识。"""
        return f'{self.name}({self.ip_address})'

    @property
    def role_display(self):
        """返回按角色清单顺序拼接的中文角色名称。

        参数：无。
        返回：返回例如 “master / etcd” 的展示文本，未规划用途时返回未分配。
        副作用：只读取本机对象，不修改持久化数据。
        """
        labels = dict(self.ROLES)
        codes = normalize_machine_roles(self.roles)
        return ' / '.join(labels.get(code, code) for code in codes) or '未分配'

    def normalized_roles(self):
        """返回归一化后的角色代码列表。

        参数：无。
        返回：返回去重并按角色清单顺序排列的合法角色代码。
        副作用：只读取本机对象，不修改持久化数据。
        """
        return normalize_machine_roles(self.roles)

    def set_roles(self, values):
        """把外部输入的角色集合归一化后写入本机对象。

        参数：`values` 表示待写入的角色集合，允许为 None、字符串或任意可迭代对象。
        返回：返回归一化后的角色代码列表。
        副作用：修改本机对象的角色列表，不自动保存。
        """
        self.roles = normalize_machine_roles(values)
        return self.roles

    def mark_test_result(self, success, message):
        """记录连通性检测结果并同步机器状态。

        参数：`success` 表示本次检测是否连通；`message` 表示检测结果说明。
        返回：返回该业务步骤生成、查询或校验后的结果。
        副作用：修改本机对象并写入数据库。
        """
        self.last_test_message = (message or '')[:240]
        self.last_tested_at = timezone.now()
        if self.status != 'disabled':
            self.status = 'available' if success else 'unavailable'
        self.save(update_fields=['last_test_message', 'last_tested_at', 'status', 'updated_at'])
        return self

    @property
    def has_password(self):
        """判断该机器是否已托管 SSH 登录密码。"""
        return bool(self.encrypted_password)

    def set_password(self, raw_password):
        """使用 SM4 加密保存 SSH 登录密码，空值表示保持原密码不变。

        参数：`raw_password` 表示用户提交的明文密码。
        返回：返回本次是否更新了密码。
        副作用：修改本机对象的密码密文和更新时间。
        """
        from data_security.sm4 import encrypt

        if raw_password is None or raw_password == '':
            return False
        self.encrypted_password = encrypt(raw_password)
        self.password_rotated_at = timezone.now()
        return True

    def get_password(self):
        """解密返回 SSH 登录密码明文，仅供服务端部署流程调用。

        参数：无。
        返回：返回解密后的密码明文，未托管密码时返回空串。
        副作用：只读取本机对象，不修改持久化数据。
        """
        from data_security.sm4 import decrypt_if_encrypted

        return decrypt_if_encrypted(self.encrypted_password or '')


class MachineSshSession(TimeStampedModel):
    """一次从平台发起的机器 SSH 会话：谁、哪台机器、什么时候、干了多少条命令。

    机器列表的「SSH 登录」是直连目标机的交互式 shell，比「进入容器」更靠近生产环境，
    因此要能回答「这台机器最近被谁登过」。K8s 侧对应的是 ``k8s_platform.K8sExecRecord``，
    这里就是机器侧的那一张：一次会话一行，起止时间由 ``created_at`` / ``ended_at`` 表达
    （会话的创建时刻就是它的开始时刻，不另设一个必然相同的 started_at）。

    主机名、IP、登录用户都是**快照**：机器被删除、改名或改 IP 之后，这份记录仍然要能说清
    「当时登的是哪台」，所以外键是 SET_NULL 而快照单独存一份，记录不随机器消失。

    ``output_preview`` 只留前 2000 字（与 ``K8sExecRecord.output_preview`` 同一口径）：
    交互式会话的输出可以无限长，全量落库既没有查询入口，也会把密码、token 这类东西长期
    留在库里。
    """

    STATUSES = [
        ('active', '进行中'),
        ('closed', '已关闭'),
        ('failed', '连接失败'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='machine_ssh_sessions',
        verbose_name='组织',
        help_text='发起会话的用户所属组织，用于数据隔离。',
    )
    machine = models.ForeignKey(
        Machine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ssh_sessions',
        verbose_name='机器',
        help_text='会话登录的机器；机器被删除后置空，记录仍然保留。',
    )
    machine_name = models.CharField(max_length=160, blank=True, verbose_name='主机名快照', help_text='会话开始时机器的主机名。')
    ip_address = models.CharField(max_length=64, blank=True, verbose_name='机器 IP 快照', help_text='会话开始时机器的 IP 地址。')
    ssh_user = models.CharField(max_length=120, blank=True, verbose_name='登录用户名快照', help_text='本次登录使用的 SSH 用户名。')
    actor = models.CharField(max_length=120, blank=True, verbose_name='操作者', help_text='发起 SSH 会话的平台用户名。')
    client_ip = models.CharField(max_length=64, blank=True, verbose_name='客户端地址', help_text='发起会话的浏览器地址，用于追溯。')
    status = models.CharField(max_length=16, choices=STATUSES, default='active', verbose_name='状态')
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    command_count = models.PositiveIntegerField(default=0, verbose_name='命令条数', help_text='会话期间识别到的已提交命令条数。')
    output_preview = models.TextField(blank=True, verbose_name='输出预览', help_text='只保存前 2000 字，避免敏感输出长期落库。')
    failure_reason = models.CharField(max_length=240, blank=True, verbose_name='失败原因')

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [
            models.Index(fields=['organization', 'status'], name='k8s_ssh_session_org_status_idx'),
        ]

    def __str__(self):
        """返回「主机名 / 操作者」组成的会话标识。"""
        return f'{self.machine_name or self.ip_address}@{self.actor or "system"} {self.status}'


class MachineMetric(TimeStampedModel):
    """机器水位的缓存：最近一次采集到的 CPU / 内存 / 磁盘使用率。

    与 ``Machine`` 一对一，采集一次就覆盖一次，不保留历史 —— 界面回答的是「这台机器现在
    怎么样」，攒一串时间序列既没有查询入口也没有展示位置，只是白占体积。

    三个使用率都可为空：采不到就是采不到，界面显示「—」。**0 与空是两件事**，存成 0 会让
    「连不上」看起来像「这台机器很闲」。

    ``sampled_at`` 记的是**采集发生的时刻**而不是写入时刻，且**采集失败也照样写一行**
    （``status='failed'``）：失败同样是「这台机器最近一次采集的结论」，缓存它才能让一台
    关机的机器在下一次采集到来之前不会被反复重连（每次都要等一次连接超时）。
    """

    STATUSES = [
        ('ok', '正常'),
        ('failed', '采集失败'),
    ]

    machine = models.OneToOneField(
        Machine,
        on_delete=models.CASCADE,
        related_name='metric',
        verbose_name='机器',
        help_text='该水位属于哪台机器；机器被删除时缓存一并删除。',
    )
    status = models.CharField(max_length=16, choices=STATUSES, default='ok', verbose_name='采集结果')
    error = models.CharField(max_length=240, blank=True, verbose_name='失败原因', help_text='采集失败时的中文原因，供界面悬停显示。')
    hostname = models.CharField(max_length=160, blank=True, verbose_name='实际主机名', help_text='机器上 hostname 命令的真实输出。')
    cpu_percent = models.FloatField(null=True, blank=True, verbose_name='CPU 使用率', help_text='/proc/stat 两次采样（间隔 1 秒）算出的使用率，单位百分比。')
    memory_percent = models.FloatField(null=True, blank=True, verbose_name='内存使用率', help_text='按 MemAvailable 计算的百分比。')
    disk_percent = models.FloatField(null=True, blank=True, verbose_name='根分区使用率', help_text='只统计根分区 / 的百分比。')
    sampled_at = models.DateTimeField(verbose_name='采集时间', help_text='最近一次采集发生的时刻。')

    class Meta:
        ordering = ['-sampled_at', 'pk']
        indexes = [
            models.Index(fields=['sampled_at'], name='k8s_metric_sampled_idx'),
        ]

    def __str__(self):
        """返回机器与采集时刻组成的缓存标识。"""
        return f'{self.machine_id}@{self.sampled_at:%Y-%m-%d %H:%M:%S}'

    def as_item(self):
        """把缓存行转换成接口逐台返回的那一项。

        参数：无。
        返回：返回 ``{'machine_id', 'status', 'error', 'hostname', 'cpu_percent',
            'memory_percent', 'disk_percent', 'sampled_at'}``。
        副作用：只做纯计算，不修改持久化数据。

        ``sampled_at`` 一并返回：缓存意味着不同机器的数据可能不是同一刻采的，界面把
        时刻挂在每行上，比只给一个总时间更准。
        """
        return {
            'machine_id': self.machine_id,
            'status': self.status,
            'error': self.error,
            'hostname': self.hostname,
            'cpu_percent': self.cpu_percent,
            'memory_percent': self.memory_percent,
            'disk_percent': self.disk_percent,
            'sampled_at': timezone.localtime(self.sampled_at).strftime('%Y-%m-%d %H:%M:%S'),
        }


# 合法角色代码清单，供角色归一化、序列化器和批量绑定接口复用。
MACHINE_ROLE_CODES = tuple(code for code, _label in Machine.ROLES)


def normalize_machine_roles(values):
    """把外部输入的角色集合归一化为合法、去重且顺序稳定的角色代码列表。

    参数：`values` 表示待归一化的角色集合，允许为 None、单个角色字符串或任意可迭代对象。
    返回：返回按 Machine.ROLES 顺序排列的角色代码列表，非法值与重复值会被丢弃。
    副作用：只做纯计算，不修改持久化数据。
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    try:
        candidates = list(values)
    except TypeError:
        return []
    picked = {str(item) for item in candidates}
    return [code for code in MACHINE_ROLE_CODES if code in picked]
