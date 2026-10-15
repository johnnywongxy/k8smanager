from django.apps import AppConfig


class K8SResourceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_resource'
    verbose_name = 'K8s 资源管理'
