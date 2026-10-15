from django.db import migrations, models


class Migration(migrations.Migration):
    """给节点快照加上「集群内部地址」。

    kubeasz 的 etcd/apiserver 按 inventory 里的主机名做 bind，节点在 NAT 后面时操作员填的
    公网地址不在网卡上，bind 会直接失败；这一列记录平台探测到的网卡地址，编译 inventory 时
    用它当主机名、用填写的地址当 ansible_host。留空表示两者相同。
    """

    dependencies = [
        ('k8s_deployment', '0003_k8sdeployment_skip_emergency_backup'),
    ]

    operations = [
        migrations.AddField(
            model_name='k8sdeploymentnode',
            name='cluster_ip',
            field=models.GenericIPAddressField(
                blank=True,
                null=True,
                verbose_name='集群内部地址',
                help_text='节点网卡上真实存在的地址，kubeasz 用它做 etcd 与 apiserver 的监听和广播。'
                          '节点在 NAT 后面时它与上面填写的 IP 不同，留空表示两者相同。',
            ),
        ),
    ]
