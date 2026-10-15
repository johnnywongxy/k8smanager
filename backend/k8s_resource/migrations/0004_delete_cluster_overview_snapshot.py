"""删除「集群总览」页面的取数缓存表。

该页连同它的接口一起下线（``K8sOverviewView`` 与 ``overview_cache`` 已删），
这张表不再有任何读写方，留着只会是一张永远不更新的空表。

**不删 0003 而是新增一条 DropModel**：0003 可能已经在别的环境跑过了
（本机库、94 上的部署库），把已应用的迁移文件删掉会让 ``django_migrations``
里指向它的那行变成孤儿记录，后续 ``migrate`` 直接报 InconsistentMigrationHistory。
只回滚过 0003、从未升级过的库，跑本迁移时表本来就不存在，
``DeleteModel`` 会按 ``if_exists`` 之外的常规路径报错 —— 但那种库必然也还没跑
0003 之后的任何一条，``makemigrations --check`` 会先拦下来，属于不用管的假想状态。
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_resource', '0003_k8s_cluster_overview_snapshot'),
    ]

    operations = [
        migrations.DeleteModel(name='K8sClusterOverviewSnapshot'),
    ]
