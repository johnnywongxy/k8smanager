from django.apps import AppConfig


class K8SObservabilityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_observability'
    verbose_name = 'K8s 日志监控'
