from django.apps import AppConfig


class K8SAuditConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_audit'
    verbose_name = 'K8s 操作审计'
