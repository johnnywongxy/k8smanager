from rest_framework import serializers
from django.utils import timezone

from . import crypto
from .models import K8sCluster, K8sClusterCredential, K8sClusterHealthCheck
from .services import normalize_kubeconfig_text, parse_kubeconfig_text


DT_FORMAT = '%Y-%m-%d %H:%M:%S'


class K8sClusterSerializer(serializers.ModelSerializer):
    """Cluster serializer; kubeconfig is write-only and never returned."""

    kubeconfig = serializers.CharField(write_only=True, required=False, allow_blank=True)
    api_server = serializers.URLField(required=False, allow_blank=True)
    has_kubeconfig = serializers.SerializerMethodField()
    credential_source_path = serializers.SerializerMethodField()
    credential_fingerprint = serializers.SerializerMethodField()
    credential_rotated_at = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True, default='')
    auth_type_display = serializers.CharField(source='get_auth_type_display', read_only=True, default='')
    # 来源与集群标识要返回给前端：运维入口只对 ``origin='deployed'`` 开放，列表页据此决定
    # 是否渲染这些按钮，而不是先渲染再靠一次额外请求把它藏起来（规划 6.1）。
    origin = serializers.CharField(read_only=True)
    origin_display = serializers.CharField(source='get_origin_display', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    last_tested_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    last_synced_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sCluster
        fields = [
            'id', 'organization', 'name', 'slug', 'origin', 'origin_display',
            'cluster_name', 'context_name', 'user_name', 'api_server',
            'public_api_server', 'internal_api_server', 'api_server_vip', 'lb_mode',
            'auth_type', 'auth_type_display', 'kubeconfig',
            'has_kubeconfig', 'credential_source_path', 'credential_fingerprint', 'credential_rotated_at',
            'kube_version', 'runtime_version', 'network_plugin', 'node_count',
            'status', 'status_display', 'last_test_status', 'last_test_message', 'last_tested_at',
            'last_synced_at', 'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'organization', 'slug', 'origin', 'cluster_name', 'context_name', 'user_name', 'kube_version',
            'runtime_version', 'network_plugin', 'node_count', 'last_test_status',
            'last_test_message', 'last_tested_at', 'last_synced_at', 'created_at', 'updated_at',
            # ``lb_mode`` 只读：入口模式在装集群签 ApiServer 证书时就定下来了，事后改它只会让
            # 记录与集群实际情况脱节（要换模式只能重建）。写回由部署任务自己走模型层
            # （``register.ensure_deployed_cluster``），不经过这里。
            'lb_mode',
        ]

    def get_has_kubeconfig(self, obj):
        credential = getattr(obj, 'credential', None)
        return bool(credential and credential.encrypted_kubeconfig)

    def get_credential_source_path(self, obj):
        credential = getattr(obj, 'credential', None)
        return credential.source_path if credential else ''

    def get_credential_fingerprint(self, obj):
        credential = getattr(obj, 'credential', None)
        if not credential or not credential.fingerprint:
            return ''
        return credential.fingerprint[:12]

    def get_credential_rotated_at(self, obj):
        credential = getattr(obj, 'credential', None)
        if not credential or not credential.rotated_at:
            return ''
        return timezone.localtime(credential.rotated_at).strftime(DT_FORMAT)

    def validate_kubeconfig(self, value):
        if value and value != crypto.MASK:
            try:
                parse_kubeconfig_text(value)
            except Exception as exc:
                raise serializers.ValidationError(f'KubeConfig 解析失败：{exc}')
            return normalize_kubeconfig_text(value)
        return value

    def _save_kubeconfig(self, instance, raw_text):
        if not raw_text or raw_text == crypto.MASK:
            return instance
        metadata = parse_kubeconfig_text(raw_text)
        instance.cluster_name = metadata['cluster_name']
        instance.context_name = metadata['context_name']
        instance.user_name = metadata['user_name']
        instance.api_server = metadata['api_server']
        instance.auth_type = 'kubeconfig'
        instance.save()
        credential, _created = K8sClusterCredential.objects.get_or_create(cluster=instance)
        credential.encrypted_kubeconfig = crypto.encrypt_secret(raw_text)
        credential.encrypted_client_certificate = crypto.encrypt_secret(metadata['client_certificate_data'])
        credential.encrypted_client_key = crypto.encrypt_secret(metadata['client_key_data'])
        credential.encrypted_certificate_authority = crypto.encrypt_secret(metadata['certificate_authority_data'])
        credential.encrypted_token = crypto.encrypt_secret(metadata['token'])
        from .services import kubeconfig_fingerprint
        credential.fingerprint = kubeconfig_fingerprint(raw_text)
        credential.save()
        return instance

    def validate(self, attrs):
        attrs = super().validate(attrs)
        raw_text = attrs.get('kubeconfig')
        if raw_text and raw_text != crypto.MASK:
            try:
                metadata = parse_kubeconfig_text(raw_text)
            except Exception as exc:
                raise serializers.ValidationError({'kubeconfig': f'KubeConfig 解析失败：{exc}'})
            if not attrs.get('api_server'):
                attrs['api_server'] = metadata['api_server']
            attrs['cluster_name'] = metadata['cluster_name']
            attrs['context_name'] = metadata['context_name']
            attrs['user_name'] = metadata['user_name']
            attrs['auth_type'] = 'kubeconfig'
        if not attrs.get('api_server') and not getattr(self.instance, 'api_server', ''):
            raise serializers.ValidationError({'api_server': '请填写 API Server，或粘贴可解析的 KubeConfig'})
        return attrs

    def create(self, validated_data):
        raw_text = validated_data.pop('kubeconfig', '')
        instance = K8sCluster.objects.create(**validated_data)
        self._save_kubeconfig(instance, raw_text)
        return instance

    def update(self, instance, validated_data):
        raw_text = validated_data.pop('kubeconfig', None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        if raw_text:
            self._save_kubeconfig(instance, raw_text)
        return instance


class K8sClusterHealthCheckSerializer(serializers.ModelSerializer):
    cluster_name = serializers.CharField(source='cluster.name', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    checked_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sClusterHealthCheck
        fields = '__all__'
        read_only_fields = ['organization', 'created_at', 'updated_at']
