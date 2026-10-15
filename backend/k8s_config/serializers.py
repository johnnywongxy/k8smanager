from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .models import K8sVersionMapping, SupportedOperatingSystem


DT_FORMAT = '%Y-%m-%d %H:%M:%S'


class K8sVersionMappingSerializer(serializers.ModelSerializer):
    """Kubernetes 版本对应关系序列化器。"""

    kubernetes_version = serializers.CharField(
        max_length=40,
        validators=[UniqueValidator(
            queryset=K8sVersionMapping.objects.all(),
            message='该 Kubernetes 版本已存在，请勿重复添加。',
        )],
    )

    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    supported_system_ids = serializers.PrimaryKeyRelatedField(
        source='supported_systems',
        queryset=SupportedOperatingSystem.objects.all(),
        many=True,
        required=False,
    )
    supported_systems = serializers.SerializerMethodField()

    class Meta:
        model = K8sVersionMapping
        fields = [
            'id', 'kubernetes_version', 'kubeasz_version', 'is_active',
            'supported_system_ids', 'supported_systems', 'sort_order', 'remark',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_kubernetes_version(self, value):
        """校验 Kubernetes 版本号不能为空并去除首尾空白。

        参数：`value` 表示请求提交的版本号。
        返回：返回规范化后的版本号。
        副作用：不直接修改持久化数据。
        """
        version = (value or '').strip()
        if not version:
            raise serializers.ValidationError('请填写 Kubernetes 版本。')
        return version

    def get_supported_systems(self, obj):
        return [
            {
                'id': item.id,
                'name': item.name,
                'os_type': item.os_type,
                'version': item.version,
                'notes_url': item.notes_url,
                'is_active': item.is_active,
            }
            for item in obj.supported_systems.all()
        ]

    def validate_kubeasz_version(self, value):
        """校验 kubeasz 版本号不能为空并去除首尾空白。

        参数：`value` 表示请求提交的版本号。
        返回：返回规范化后的版本号。
        副作用：不直接修改持久化数据。
        """
        version = (value or '').strip()
        if not version:
            raise serializers.ValidationError('请填写 kubeasz 版本。')
        return version


class SupportedOperatingSystemSerializer(serializers.ModelSerializer):
    """支持的操作系统序列化器。"""

    os_type = serializers.CharField(max_length=80)
    version = serializers.CharField(max_length=80)
    # 系统全称由类型和版本自动拼接，接口只读返回给部署向导和列表展示。
    name = serializers.CharField(read_only=True)

    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = SupportedOperatingSystem
        fields = [
            'id', 'name', 'os_type', 'version', 'notes_url', 'is_active',
            'sort_order', 'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = ['name', 'created_at', 'updated_at']

    def validate(self, attrs):
        """校验同一操作系统类型下不会出现重复版本。

        单独实现而不是用 UniqueTogetherValidator，是为了让 PATCH 局部更新也能校验到。

        参数：`attrs` 表示通过字段校验的请求数据。
        返回：返回原始请求数据。
        副作用：只读取数据库，不修改持久化数据。
        """
        os_type = (attrs.get('os_type') or getattr(self.instance, 'os_type', '') or '').strip()
        version = (attrs.get('version') or getattr(self.instance, 'version', '') or '').strip()
        conflicts = SupportedOperatingSystem.objects.filter(os_type=os_type, version=version)
        if self.instance is not None:
            conflicts = conflicts.exclude(pk=self.instance.pk)
        if os_type and version and conflicts.exists():
            raise serializers.ValidationError({'version': '该操作系统类型下的版本已存在，请勿重复添加。'})
        return attrs

    def validate_os_type(self, value):
        """校验操作系统类型不能为空并去除首尾空白。

        参数：`value` 表示请求提交的操作系统类型。
        返回：返回规范化后的操作系统类型。
        副作用：不直接修改持久化数据。
        """
        os_type = (value or '').strip()
        if not os_type:
            raise serializers.ValidationError('请填写操作系统类型。')
        return os_type

    def validate_version(self, value):
        """校验操作系统版本不能为空并去除首尾空白。

        参数：`value` 表示请求提交的版本号。
        返回：返回规范化后的版本号。
        副作用：不直接修改持久化数据。
        """
        version = (value or '').strip()
        if not version:
            raise serializers.ValidationError('请填写版本号。')
        return version
