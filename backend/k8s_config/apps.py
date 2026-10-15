from django.apps import AppConfig


class K8SConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_config'
    verbose_name = 'K8s 配置中心'
