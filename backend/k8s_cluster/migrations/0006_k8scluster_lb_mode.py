"""给集群记录加上外部访问入口的模式。

原来只存 ``api_server_vip``（地址）。要区分「自建 l4lb + keepalived」与「云内网负载均衡」，
地址这一个字段不够用：两者的地址都非空，但前者在集群里装过代理（清理时要列「外部负载均衡」
那一项）、后者什么都没装。只凭地址非空反推会把云 LB 的集群误判成自建。

历史数据在这里先留默认值 ``none``，随后由 ``0007`` 按 ``api_server_vip`` 是否非空回填成
``self`` —— 这个字段出现时平台只有自建一种做法，地址非空就必然是自建。

（不要再指望「下次运维任务回写时补正」：运维任务的 ``lb_mode`` 正是从这个字段读出来的，
读到的 ``none`` 又不会被写回去，是个不会转动的循环。回填只能由迁移自己完成。）
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('k8s_cluster', '0005_k8scluster_api_server_vip'),
    ]

    operations = [
        migrations.AddField(
            model_name='k8scluster',
            name='lb_mode',
            field=models.CharField(
                choices=[('none', '不使用'), ('self', '自建 l4lb + keepalived'), ('cloud', '云内网负载均衡')],
                default='none',
                help_text='与部署任务的 lb_mode 同义。集群记录必须自己存一份：只凭 api_server_vip 非空反推会把云 LB 误判成自建',
                max_length=16,
                verbose_name='外部访问入口',
            ),
        ),
        migrations.AlterField(
            model_name='k8scluster',
            name='api_server_vip',
            field=models.CharField(
                blank=True,
                help_text='高可用集群的外部入口地址：自建模式是 keepalived 抢占的虚拟 IP，云内网负载均衡模式是云负载均衡器的地址；'
                          '不使用外部入口的集群为空',
                max_length=45,
                verbose_name='外部访问地址',
            ),
        ),
    ]
