from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .models import MACHINE_ROLE_CODES, Machine, normalize_machine_roles


DT_FORMAT = '%Y-%m-%d %H:%M:%S'


class MachineSerializer(serializers.ModelSerializer):
    """机器序列化器，连接信息可读写，SSH 密码只写不读。"""

    ip_address = serializers.IPAddressField(
        validators=[UniqueValidator(queryset=Machine.objects.all(), message='该 IP 地址已存在，请勿重复添加机器。')],
    )
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=MACHINE_ROLE_CODES, error_messages={
            'invalid_choice': '角色 {input} 不是合法的集群角色。',
        }),
        allow_empty=True,
        required=False,
        error_messages={'not_a_list': '机器角色必须是角色代码组成的列表。'},
    )
    role_display = serializers.CharField(read_only=True, default='未分配')
    cluster_display_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True, default='')
    ssh_port = serializers.IntegerField(min_value=1, max_value=65535, default=22)
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=200,
        trim_whitespace=False,
        error_messages={'max_length': 'SSH 密码不能超过 200 个字符。'},
    )
    has_password = serializers.BooleanField(read_only=True)
    password_rotated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    last_tested_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = Machine
        fields = [
            'id', 'organization', 'name', 'ip_address', 'ssh_port', 'ssh_user',
            'password', 'has_password', 'password_rotated_at',
            'roles', 'role_display', 'cluster_name', 'cluster_display_name', 'status', 'status_display',
            'last_test_message', 'last_tested_at', 'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'organization', 'has_password', 'password_rotated_at',
            # 所属集群由「部署集群」向导的节点绑定写入（assign/unassign 直接改模型），
            # 不允许走机器增删改接口手工改动，否则会和机器角色脱节。
            'cluster_name',
            'last_test_message', 'last_tested_at', 'created_at', 'updated_at',
        ]

    def get_cluster_display_name(self, obj):
        """返回「所属集群」这一列该显示的名称。

        参数：`obj` 表示 Machine 实例。
        返回：返回集群在平台里的展示名称（用户在部署向导里写的那个）；集群记录找不到时返回空串。
        副作用：按需查询 K8sCluster 表，同一次请求内按标识缓存。

        ``cluster_name`` 存的是集群标识（kubeasz 的集群目录名，例如 ``jd``），它是机器归属的
        比对键 —— 向导绑定、解绑、扩容候选过滤、集群删除时的解绑都拿它与 ``K8sCluster.slug``
        对，不能改成展示名。但它是给机器用的标识，不是给用户看的名字：机器列表当初显示 ``jd``，
        用户在集群列表里看到的是「JD云-二次」，两处对不上，于是无从判断这台机器属于哪个集群。
        展示名在这里解析出来，字段本身（以及所有比对）保持不动。

        解析不到就返回空串，由调用方回落到 ``cluster_name``：那说明集群记录已经被删（而机器
        还留着归属），把标识照原样显示出来比编一个名字诚实。
        """
        from k8s_cluster.models import K8sCluster

        slug = str(obj.cluster_name or '').strip()
        if not slug:
            return ''
        cache = self.context.setdefault('_machine_cluster_names', {})
        if slug not in cache:
            name = (
                K8sCluster.objects
                .filter(organization_id=obj.organization_id, slug=slug)
                .order_by('-updated_at')
                .values_list('name', flat=True)
                .first()
            )
            cache[slug] = str(name or '')
        return cache[slug]

    def create(self, validated_data):
        """创建机器并加密保存 SSH 密码。

        参数：`validated_data` 表示通过校验的请求数据。
        返回：返回新建的机器对象。
        副作用：向数据库写入一条机器记录。
        """
        raw_password = validated_data.pop('password', '')
        machine = Machine(**validated_data)
        self.password_changed = machine.set_password(raw_password)
        machine.save()
        return machine

    def update(self, instance, validated_data):
        """更新机器，密码留空时保持原密码不变。

        参数：`instance` 表示待更新的机器对象；`validated_data` 表示通过校验的请求数据。
        返回：返回更新后的机器对象。
        副作用：修改数据库中的机器记录。
        """
        raw_password = validated_data.pop('password', '')
        for field, value in validated_data.items():
            setattr(instance, field, value)
        self.password_changed = instance.set_password(raw_password)
        instance.save()
        return instance

    def validate_name(self, value):
        """校验机器名称不能为空串。

        参数：`value` 表示待处理的输入值。
        返回：返回该业务步骤生成、查询或校验后的结果。
        副作用：不直接修改持久化数据。
        """
        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('请填写主机名。')
        return name

    def validate_roles(self, value):
        """归一化机器角色列表，去重并按角色清单顺序排列。

        参数：`value` 表示待处理的输入值。
        返回：返回该业务步骤生成、查询或校验后的结果。
        副作用：不直接修改持久化数据。
        """
        return normalize_machine_roles(value)

    def validate_ssh_user(self, value):
        """校验 SSH 登录用户名不能为空串。

        参数：`value` 表示待处理的输入值。
        返回：返回该业务步骤生成、查询或校验后的结果。
        副作用：不直接修改持久化数据。
        """
        user = (value or '').strip()
        if not user:
            raise serializers.ValidationError('请填写登录用户名。')
        return user
