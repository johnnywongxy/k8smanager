from django.apps import AppConfig
from django.db.backends.signals import connection_created


def enable_sqlite_pragmas(sender, connection, **kwargs):
    """为 SQLite 连接开启 WAL 日志模式与忙等超时。

    参数：`sender` 表示信号发送方；`connection` 表示刚建立的数据库连接。
    返回：无显式返回值。
    副作用：在当前连接上执行 PRAGMA，切换日志模式并设置写锁等待时长。
    """
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        # WAL 让后台 Worker 的批量写与接口进程的读可以并发，属性写入库文件头后持久生效。
        cursor.execute('PRAGMA journal_mode=WAL;')
        # busy_timeout 是连接级设置，必须在每个新连接上重新声明。
        cursor.execute('PRAGMA busy_timeout=5000;')


class K8sDeploymentConfig(AppConfig):
    """Kubernetes 集群部署任务的应用配置。"""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'k8s_deployment'
    verbose_name = 'K8s 集群部署'

    def ready(self):
        """应用装载完成后注册 SQLite 连接初始化钩子。

        参数：无。
        返回：无显式返回值。
        副作用：为后续每个新建的 SQLite 连接挂上 PRAGMA 初始化。
        """
        connection_created.connect(enable_sqlite_pragmas, dispatch_uid='k8s_deployment_sqlite_pragmas')
