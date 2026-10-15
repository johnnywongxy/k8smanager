from django.contrib import admin

from .models import K8sOperationAudit, K8sSecretAccessAudit


@admin.register(K8sOperationAudit)
class K8sOperationAuditAdmin(admin.ModelAdmin):
    list_display = ('cluster_name', 'namespace', 'resource_type', 'resource_name', 'action', 'actor', 'status', 'created_at')
    search_fields = ('cluster_name', 'namespace', 'resource_name', 'actor', 'action')


@admin.register(K8sSecretAccessAudit)
class K8sSecretAccessAuditAdmin(admin.ModelAdmin):
    list_display = ('cluster_name', 'namespace', 'secret_name', 'access_type', 'actor', 'result', 'created_at')
    search_fields = ('cluster_name', 'namespace', 'secret_name', 'actor')
