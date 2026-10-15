from rest_framework import serializers

from .models import K8sNamespacePolicy, K8sResourceSnapshot


DT_FORMAT = '%Y-%m-%d %H:%M:%S'


class K8sNamespacePolicySerializer(serializers.ModelSerializer):
    """Serializer for namespace-level K8s business authorization rules."""

    cluster_name = serializers.CharField(source='cluster.name', read_only=True, default='')
    department_name = serializers.CharField(source='department.name', read_only=True, default='')
    user_names = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True, default='')
    action_scope_display = serializers.CharField(source='get_action_scope_display', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    valid_from = serializers.DateTimeField(format=DT_FORMAT, required=False)
    valid_until = serializers.DateTimeField(format=DT_FORMAT, required=False, allow_null=True)

    class Meta:
        model = K8sNamespacePolicy
        fields = [
            'id', 'organization', 'name', 'users', 'user_names', 'department',
            'department_name', 'role', 'cluster', 'cluster_name', 'namespace',
            'resource_types', 'action_scope', 'action_scope_display', 'valid_from',
            'valid_until', 'status', 'status_display', 'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = ['organization', 'created_at', 'updated_at']

    def get_user_names(self, obj):
        return [user.username for user in obj.users.all()]


class K8sResourceSnapshotSerializer(serializers.ModelSerializer):
    cluster_name = serializers.CharField(source='cluster.name', read_only=True, default='')
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    generated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sResourceSnapshot
        fields = '__all__'
        read_only_fields = ['organization', 'created_at', 'updated_at', 'generated_at']
