from django.db import migrations


def release_orphan_machines(apps, schema_editor):
    """解绑「所属集群已经不存在」的机器，清空角色与所属集群。

    历史上集群删除只删了自己的记录，机器表里的 cluster_name 留了下来，
    机器列表于是显示成一个点不开的集群名，且因为该字段已改为只读而无法手工纠正。
    这里把指向已删除集群的归属统一清掉，语义与集群删除时的解绑保持一致。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：批量修改机器表的 roles 与 cluster_name。
    """
    Machine = apps.get_model('k8s_machine', 'Machine')
    K8sCluster = apps.get_model('k8s_cluster', 'K8sCluster')
    live_names = set(K8sCluster.objects.values_list('name', flat=True))
    # 机器表数据量小，直接迭代即可；边迭代边 save 在部分后端下会与游标冲突，故不用 iterator()。
    for machine in Machine.objects.exclude(cluster_name=''):
        if machine.cluster_name in live_names:
            continue
        machine.roles = []
        machine.cluster_name = ''
        machine.save(update_fields=['roles', 'cluster_name', 'updated_at'])


def noop(apps, schema_editor):
    """回滚时不做恢复：原始归属指向的集群已经不存在，留空是更安全的中间状态。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：不修改任何数据。
    """


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_machine', '0003_machine_multi_roles'),
        ('k8s_cluster', '0002_alter_k8scluster_api_server_alter_k8scluster_status'),
    ]

    operations = [
        migrations.RunPython(release_orphan_machines, noop),
    ]
