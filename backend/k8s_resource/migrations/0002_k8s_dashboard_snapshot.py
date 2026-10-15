from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    """总览页连集群取数结果的 15 分钟缓存。

    `scope` 上的 unique 约束有两个作用：一是保证「全平台共用一份」这个语义不被写坏，
    二是 `update_or_create(scope=...)` 的查重依据。
    """

    dependencies = [
        ('k8s_resource', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='K8sDashboardSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('scope', models.CharField(default='global', help_text='当前固定为 global，全平台共用一份', max_length=64, unique=True, verbose_name='缓存作用域')),
                ('payload', models.JSONField(blank=True, default=dict, help_text='按 cluster_id 索引的各集群计数与失败原因', verbose_name='缓存数据')),
                ('generated_at', models.DateTimeField(default=django.utils.timezone.now, help_text='这份缓存取自哪个时刻，超过有效期即重新连集群取数', verbose_name='生成时间')),
            ],
            options={
                'ordering': ['-generated_at'],
            },
        ),
    ]
