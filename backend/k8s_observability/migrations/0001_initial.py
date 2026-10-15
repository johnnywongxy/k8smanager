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
            name='K8sSourceHealthCheck',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('source_kind', models.CharField(choices=[('metrics', '监控数据源'), ('logs', '日志数据源')], max_length=32, verbose_name='数据源类别')),
                ('source_type', models.CharField(blank=True, max_length=32, verbose_name='数据源类型')),
                ('source_name', models.CharField(max_length=120, verbose_name='数据源名称')),
                ('status', models.CharField(default='completed', max_length=32, verbose_name='检测状态')),
                ('latency_ms', models.PositiveIntegerField(default=0, verbose_name='耗时毫秒')),
                ('message', models.CharField(blank=True, max_length=240, verbose_name='检测结果')),
                ('actor', models.CharField(blank=True, max_length=120, verbose_name='检测人')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_source_health_checks', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['source_kind', 'source_type', 'created_at'], name='k8s_source_health_scope_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sQueryHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('query_type', models.CharField(choices=[('logs', '日志查询'), ('metrics', '监控查询'), ('statistics', '资源统计')], max_length=32, verbose_name='查询类型')),
                ('cluster_name', models.CharField(blank=True, max_length=160, verbose_name='集群名称')),
                ('namespace', models.CharField(blank=True, max_length=160, verbose_name='命名空间')),
                ('pod', models.CharField(blank=True, max_length=240, verbose_name='Pod')),
                ('container', models.CharField(blank=True, max_length=160, verbose_name='容器')),
                ('source_type', models.CharField(blank=True, max_length=32, verbose_name='数据源类型')),
                ('query', models.TextField(blank=True, verbose_name='查询语句或关键字')),
                ('time_range', models.CharField(blank=True, max_length=120, verbose_name='时间范围')),
                ('status', models.CharField(default='completed', max_length=32, verbose_name='状态')),
                ('actor', models.CharField(blank=True, max_length=120, verbose_name='操作者')),
                ('result_summary', models.JSONField(blank=True, default=dict, verbose_name='结果摘要')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_query_histories', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['query_type', 'cluster_name', 'created_at'], name='k8s_query_history_scope_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('cluster_name', models.CharField(blank=True, max_length=160, verbose_name='集群名称')),
                ('namespace', models.CharField(blank=True, max_length=160, verbose_name='命名空间')),
                ('resource_type', models.CharField(blank=True, max_length=80, verbose_name='资源类型')),
                ('resource_name', models.CharField(blank=True, max_length=240, verbose_name='资源名称')),
                ('level', models.CharField(choices=[('info', '信息'), ('warning', '警告'), ('critical', '严重')], default='warning', max_length=32, verbose_name='级别')),
                ('title', models.CharField(max_length=160, verbose_name='标题')),
                ('message', models.CharField(blank=True, max_length=500, verbose_name='消息')),
                ('status', models.CharField(choices=[('unread', '未读'), ('acknowledged', '已确认')], default='unread', max_length=32, verbose_name='状态')),
                ('acknowledged_by', models.CharField(blank=True, max_length=120, verbose_name='确认人')),
                ('acknowledged_at', models.DateTimeField(blank=True, null=True, verbose_name='确认时间')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_notifications', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['cluster_name', 'status', 'created_at'], name='k8s_notice_scope_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sMetricsSource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('name', models.CharField(help_text='监控看板展示的数据源名称', max_length=120, verbose_name='数据源名称')),
                ('source_type', models.CharField(choices=[('metrics_server', 'metrics-server'), ('prometheus', 'Prometheus'), ('victoriametrics', 'VictoriaMetrics')], default='prometheus', max_length=32, verbose_name='数据源类型')),
                ('endpoint', models.URLField(blank=True, help_text='Prometheus/VictoriaMetrics HTTP API 地址', max_length=320, verbose_name='访问地址')),
                ('auth_type', models.CharField(choices=[('none', '无认证'), ('bearer', 'Bearer Token'), ('basic', 'Basic Auth')], default='none', max_length=32, verbose_name='认证方式')),
                ('username', models.CharField(blank=True, max_length=120, verbose_name='用户名')),
                ('encrypted_secret', models.TextField(blank=True, help_text='Bearer token 或 Basic Auth 密码加密后保存', verbose_name='密钥密文')),
                ('enabled', models.BooleanField(default=True, verbose_name='是否启用')),
                ('status', models.CharField(choices=[('configured', '已配置'), ('available', '可用'), ('unconfigured', '未配置'), ('unavailable', '不可用'), ('disabled', '已禁用')], default='configured', max_length=32, verbose_name='状态')),
                ('last_message', models.CharField(blank=True, max_length=240, verbose_name='最近检测结果')),
                ('last_tested_at', models.DateTimeField(blank=True, null=True, verbose_name='最近检测时间')),
                ('config', models.JSONField(blank=True, default=dict, verbose_name='扩展配置')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_metrics_sources', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['name'],
                'indexes': [models.Index(fields=['organization', 'source_type', 'enabled'], name='k8s_metric_src_scope_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sLogSource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('name', models.CharField(help_text='日志查询页面展示的数据源名称', max_length=120, verbose_name='数据源名称')),
                ('source_type', models.CharField(choices=[('kubernetes', 'Kubernetes 实时日志'), ('loki', 'Loki'), ('opensearch', 'OpenSearch')], default='loki', max_length=32, verbose_name='数据源类型')),
                ('endpoint', models.URLField(blank=True, help_text='Loki/OpenSearch HTTP API 地址', max_length=320, verbose_name='访问地址')),
                ('auth_type', models.CharField(choices=[('none', '无认证'), ('bearer', 'Bearer Token'), ('basic', 'Basic Auth')], default='none', max_length=32, verbose_name='认证方式')),
                ('username', models.CharField(blank=True, max_length=120, verbose_name='用户名')),
                ('encrypted_secret', models.TextField(blank=True, help_text='Bearer token 或 Basic Auth 密码加密后保存', verbose_name='密钥密文')),
                ('enabled', models.BooleanField(default=True, verbose_name='是否启用')),
                ('status', models.CharField(choices=[('configured', '已配置'), ('available', '可用'), ('unconfigured', '未配置'), ('unavailable', '不可用'), ('disabled', '已禁用')], default='configured', max_length=32, verbose_name='状态')),
                ('last_message', models.CharField(blank=True, max_length=240, verbose_name='最近检测结果')),
                ('last_tested_at', models.DateTimeField(blank=True, null=True, verbose_name='最近检测时间')),
                ('config', models.JSONField(blank=True, default=dict, verbose_name='扩展配置')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_log_sources', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['name'],
                'indexes': [models.Index(fields=['organization', 'source_type', 'enabled'], name='k8s_log_src_scope_idx')],
            },
        ),
    ]
