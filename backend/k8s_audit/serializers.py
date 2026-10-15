from rest_framework import serializers

from .models import K8sOperationAudit, K8sSecretAccessAudit


DT_FORMAT = '%Y-%m-%d %H:%M:%S'


class K8sOperationAuditSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sOperationAudit
        fields = '__all__'
        read_only_fields = ['organization', 'created_at', 'updated_at']


class K8sSecretAccessAuditSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)
    updated_at = serializers.DateTimeField(format=DT_FORMAT, read_only=True)

    class Meta:
        model = K8sSecretAccessAudit
        fields = '__all__'
        read_only_fields = ['organization', 'created_at', 'updated_at']
