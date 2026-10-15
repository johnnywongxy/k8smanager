"""把外部负载均衡的「开关」换成「模式」。

原先是 ``lb_enabled``（布尔）配 ``lb_vip``：只有「自建 l4lb + keepalived」一种做法。
现在要多一种「用云厂商的内网负载均衡器」，两种做法的输入、阶段、清理清单都不一样，
一个布尔说不清，于是换成 ``lb_mode`` 三态（none/self/cloud）。

三步顺序不能颠倒：先加新列（默认 none）→ 再把原来开着开关的行标成 self → 最后删旧列。
反过来先删列就没法把数据搬过去，已经建过带 LB 集群的环境会变成「配置里写着 VIP、
模式却是 none」，那种集群重新执行时 ``[ex_lb]`` 段会是空的。

``lb_vip`` 不改名也不改语义：两种模式共用它（自建是 keepalived 抢的 VIP，云是云 LB 的地址），
只更新 help_text。
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    """把开着外部负载均衡的旧任务标成「自建」模式。

    参数：`apps` 表示历史模型注册表；`schema_editor` 表示当前数据库的 schema 编辑器。
    返回：无显式返回值。
    副作用：更新 ``k8s_deployment`` 表里 ``lb_enabled`` 为真的行。

    只认 ``lb_enabled=True``：这个字段存在期间平台只可能装出「自建」这一种入口，
    云内网负载均衡是本次才有的，历史数据里不可能出现。
    """
    deployment = apps.get_model('k8s_deployment', 'K8sDeployment')
    deployment.objects.filter(lb_enabled=True).update(lb_mode='self')


def backwards(apps, schema_editor):
    """回滚时把 self 模式还原成开关为真。

    参数：`apps` 表示历史模型注册表；`schema_editor` 表示当前数据库的 schema 编辑器。
    返回：无显式返回值。
    副作用：更新 ``k8s_deployment`` 表里 ``lb_mode='self'`` 的行。

    云内网负载均衡（cloud）还原不了 —— 旧模型里没有能表示它的字段，只能落成「未启用」。
    这是可逆迁移里唯一有损的方向，回滚前请自行记录哪些集群用了云 LB。
    """
    deployment = apps.get_model('k8s_deployment', 'K8sDeployment')
    deployment.objects.filter(lb_mode='self').update(lb_enabled=True)


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_deployment', '0007_k8sdeployment_lb_enabled_k8sdeployment_lb_vip'),
    ]

    operations = [
        migrations.AddField(
            model_name='k8sdeployment',
            name='lb_mode',
            field=models.CharField(
                choices=[('none', '不使用'), ('self', '自建 l4lb + keepalived'), ('cloud', '云内网负载均衡')],
                default='none',
                help_text='多节点集群的可选入口层，单节点集群不支持。自建模式需要至少两台独立的负载均衡机器。',
                max_length=16,
                verbose_name='外部访问入口',
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(
            model_name='k8sdeployment',
            name='lb_enabled',
        ),
        migrations.AlterField(
            model_name='k8sdeployment',
            name='lb_vip',
            field=models.CharField(
                blank=True,
                help_text='由操作员手工填写：自建模式填承载 VIP 的机器内网网段（如 eth0）中的一个空闲 IPv4；'
                          '云内网负载均衡模式填云负载均衡器的地址（其监听端口由你自行配置，必须等于本集群的 '
                          'ApiServer 端口）。平台不会自动生成，也不校验云上是否已配好。',
                max_length=45,
                verbose_name='外部访问地址',
            ),
        ),
    ]
