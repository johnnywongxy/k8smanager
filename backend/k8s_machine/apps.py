from django.apps import AppConfig


class K8SMachineConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_machine'
    verbose_name = 'K8s 机器管理'
