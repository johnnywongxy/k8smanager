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
            name='K8sGrafanaPanel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('name', models.CharField(help_text='K8s 高级能力页面展示的 Grafana 面板名称', max_length=120, verbose_name='面板名称')),
                ('dashboard_url', models.URLField(help_text='Grafana dashboard 或 panel 的可访问 URL', max_length=520, verbose_name='面板地址')),
                ('description', models.CharField(blank=True, max_length=240, verbose_name='说明')),
                ('enabled', models.BooleanField(default=True, verbose_name='是否启用')),
                ('status', models.CharField(choices=[('configured', '已配置'), ('available', '可用'), ('unconfigured', '未配置'), ('unavailable', '不可用'), ('disabled', '已禁用')], default='configured', max_length=32, verbose_name='状态')),
                ('last_message', models.CharField(blank=True, max_length=240, verbose_name='最近检测结果')),
                ('last_tested_at', models.DateTimeField(blank=True, null=True, verbose_name='最近检测时间')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_grafana_panels', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['name'],
                'indexes': [models.Index(fields=['organization', 'enabled'], name='k8s_grafana_scope_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sExecRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('cluster_name', models.CharField(blank=True, max_length=160, verbose_name='集群名称')),
                ('namespace', models.CharField(max_length=160, verbose_name='命名空间')),
                ('pod', models.CharField(max_length=240, verbose_name='Pod')),
                ('container', models.CharField(blank=True, max_length=160, verbose_name='容器')),
                ('command', models.CharField(max_length=500, verbose_name='命令')),
                ('actor', models.CharField(blank=True, max_length=120, verbose_name='操作者')),
                ('status', models.CharField(default='completed', max_length=32, verbose_name='状态')),
                ('output_preview', models.TextField(blank=True, help_text='只保存前 2000 字，避免敏感输出长期落库', verbose_name='输出预览')),
                ('failure_reason', models.CharField(blank=True, max_length=240, verbose_name='失败原因')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_exec_records', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['cluster_name', 'namespace', 'pod'], name='k8s_exec_record_scope_idx')],
            },
        ),
    ]
