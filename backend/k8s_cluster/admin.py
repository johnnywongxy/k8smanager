from django.contrib import admin

from .models import K8sCluster, K8sClusterCredential, K8sClusterHealthCheck


@admin.register(K8sCluster)
class K8sClusterAdmin(admin.ModelAdmin):
    list_display = ('name', 'api_server', 'kube_version', 'runtime_version', 'status', 'last_tested_at')
    search_fields = ('name', 'api_server', 'context_name', 'user_name')
    list_filter = ('status', 'auth_type')


@admin.register(K8sClusterCredential)
class K8sClusterCredentialAdmin(admin.ModelAdmin):
    list_display = ('cluster', 'source_path', 'rotated_at', 'updated_at')
    search_fields = ('cluster__name', 'source_path', 'fingerprint')


@admin.register(K8sClusterHealthCheck)
class K8sClusterHealthCheckAdmin(admin.ModelAdmin):
    list_display = ('cluster', 'status', 'latency_ms', 'message', 'checked_at')
    list_filter = ('status',)
    search_fields = ('cluster__name', 'message')
