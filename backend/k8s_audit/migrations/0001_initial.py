from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('ops', '0067_k8s_permission_menu'),
    ]

    operations = [
        migrations.CreateModel(
            name='K8sSecretAccessAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('cluster_name', models.CharField(blank=True, max_length=160, verbose_name='集群名称')),
                ('namespace', models.CharField(blank=True, max_length=160, verbose_name='命名空间')),
                ('secret_name', models.CharField(max_length=240, verbose_name='Secret 名称')),
                ('access_type', models.CharField(max_length=80, verbose_name='访问类型')),
                ('actor', models.CharField(max_length=120, verbose_name='操作者')),
                ('result', models.CharField(default='completed', max_length=32, verbose_name='结果')),
                ('detail', models.JSONField(blank=True, default=dict, verbose_name='详情')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_secret_access_audits', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='K8sOperationAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('cluster_name', models.CharField(blank=True, max_length=160, verbose_name='集群名称')),
                ('namespace', models.CharField(blank=True, max_length=160, verbose_name='命名空间')),
                ('resource_type', models.CharField(blank=True, max_length=80, verbose_name='资源类型')),
                ('resource_name', models.CharField(blank=True, max_length=240, verbose_name='资源名称')),
                ('action', models.CharField(max_length=80, verbose_name='操作')),
                ('actor', models.CharField(max_length=120, verbose_name='操作者')),
                ('status', models.CharField(default='completed', max_length=32, verbose_name='状态')),
                ('request_summary', models.JSONField(blank=True, default=dict, verbose_name='请求摘要')),
                ('result_summary', models.JSONField(blank=True, default=dict, verbose_name='结果摘要')),
                ('failure_reason', models.CharField(blank=True, max_length=240, verbose_name='失败原因')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_operation_audits', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
