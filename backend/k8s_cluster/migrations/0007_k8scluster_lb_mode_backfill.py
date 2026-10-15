"""把存量集群记录的外部访问入口补成 ``self``。

``lb_mode`` 是 0006 才加的字段，加的时候只知道「平台过去只做过自建 l4lb + keepalived」，
因此默认值给了 ``none``；那些真的配过 VIP 的老记录（``api_server_vip`` 非空）实际是自建模式，
留成 ``none`` 会让概览页把它们显示成「未启用」、审计记录里也看不见入口模式。

判据只有「地址非空」这一条，且在 0005 那个时期它是充分必要的：当时 ``api_server_vip`` 唯一的
写入点是 ``register.ensure_deployed_cluster``，只在部署任务的 ``compiler.lb_vip()`` 非空时写入，
而那个函数在没有启用外部负载均衡时返回空串。所以地址非空 ⟺ 当年启用了自建负载均衡。
"""

from django.db import migrations


def forwards(apps, schema_editor):
    """把有地址的存量记录标成自建模式。

    参数：`apps` 表示迁移期的应用注册表；`schema_editor` 表示 schema 编辑器。
    返回：无显式返回值。
    副作用：更新 ``k8scluster`` 表中 ``api_server_vip`` 非空且模式仍为 ``none`` 的行。
    """
    cluster = apps.get_model('k8s_cluster', 'K8sCluster')
    cluster.objects.filter(lb_mode='none').exclude(api_server_vip='').update(lb_mode='self')


def backwards(apps, schema_editor):
    """回滚时把所有自建模式退回 ``none``。

    参数：`apps` 表示迁移期的应用注册表；`schema_editor` 表示 schema 编辑器。
    返回：无显式返回值。
    副作用：更新 ``k8scluster`` 表中模式为 ``self`` 的行。

    这个方向是有损的：回滚只要求回到 0006 的语义（存量数据一律 ``none``），因此不区分
    「当年就是自建」与「0006 之后新建的自建集群」，后者会连真实模式一起丢掉。地址本身不会
    被删，所以澄清的依据还在 —— 需要时把本迁移再跑一遍就能补回来。
    """
    cluster = apps.get_model('k8s_cluster', 'K8sCluster')
    cluster.objects.filter(lb_mode='self').update(lb_mode='none')


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_cluster', '0006_k8scluster_lb_mode'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
