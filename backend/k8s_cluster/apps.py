from django.apps import AppConfig


class K8SClusterConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_cluster'
    verbose_name = 'K8s 集群接入'
