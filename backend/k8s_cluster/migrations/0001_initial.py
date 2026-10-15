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
            name='K8sCluster',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('name', models.CharField(help_text='平台内展示的 Kubernetes 集群名称', max_length=160, verbose_name='集群名称')),
                ('cluster_name', models.CharField(blank=True, help_text='kubeconfig clusters[].name', max_length=160, verbose_name='KubeConfig 集群名')),
                ('context_name', models.CharField(blank=True, help_text='kubeconfig current-context', max_length=200, verbose_name='当前 Context')),
                ('user_name', models.CharField(blank=True, help_text='kubeconfig contexts[].context.user', max_length=160, verbose_name='认证用户')),
                ('api_server', models.URLField(help_text='当前用于连接 Kubernetes API 的 server 地址', max_length=320, verbose_name='API Server')),
                ('public_api_server', models.URLField(blank=True, help_text='公网访问地址，例如阿里云 ECS 公网 IP', max_length=320, verbose_name='公网 API Server')),
                ('internal_api_server', models.URLField(blank=True, help_text='内网访问地址，用于平台部署在同 VPC 时连接', max_length=320, verbose_name='内网 API Server')),
                ('auth_type', models.CharField(choices=[('kubeconfig', 'KubeConfig'), ('token', 'ServiceAccount Token'), ('certificate', 'Client Certificate')], default='kubeconfig', max_length=32, verbose_name='认证方式')),
                ('kube_version', models.CharField(blank=True, max_length=80, verbose_name='K8s 版本')),
                ('runtime_version', models.CharField(blank=True, max_length=120, verbose_name='容器运行时')),
                ('network_plugin', models.CharField(blank=True, max_length=80, verbose_name='网络插件')),
                ('node_count', models.PositiveIntegerField(default=0, verbose_name='节点数量')),
                ('status', models.CharField(choices=[('available', '可用'), ('configured', '已配置'), ('unconfigured', '未配置'), ('unavailable', '不可用'), ('disabled', '已禁用')], default='configured', max_length=32, verbose_name='状态')),
                ('last_test_status', models.CharField(blank=True, max_length=32, verbose_name='最近检测状态')),
                ('last_test_message', models.CharField(blank=True, max_length=240, verbose_name='最近检测结果')),
                ('last_tested_at', models.DateTimeField(blank=True, null=True, verbose_name='最近检测时间')),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='最近同步时间')),
                ('remark', models.TextField(blank=True, verbose_name='备注')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_clusters', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='K8sClusterCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('encrypted_kubeconfig', models.TextField(blank=True, help_text='完整 kubeconfig 加密后存储', verbose_name='KubeConfig 密文')),
                ('encrypted_client_certificate', models.TextField(blank=True, verbose_name='客户端证书密文')),
                ('encrypted_client_key', models.TextField(blank=True, verbose_name='客户端私钥密文')),
                ('encrypted_certificate_authority', models.TextField(blank=True, verbose_name='CA 证书密文')),
                ('encrypted_token', models.TextField(blank=True, verbose_name='Token 密文')),
                ('source_path', models.CharField(blank=True, max_length=260, verbose_name='来源文件路径')),
                ('fingerprint', models.CharField(blank=True, help_text='kubeconfig 原文 SHA256，用于判断是否重复导入', max_length=64, verbose_name='凭据指纹')),
                ('rotated_at', models.DateTimeField(blank=True, null=True, verbose_name='最近轮换时间')),
                ('cluster', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='credential', to='k8s_cluster.k8scluster', verbose_name='集群')),
            ],
            options={
                'ordering': ['cluster_id'],
            },
        ),
        migrations.CreateModel(
            name='K8sClusterHealthCheck',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('status', models.CharField(max_length=32, verbose_name='检测状态')),
                ('latency_ms', models.PositiveIntegerField(default=0, verbose_name='耗时毫秒')),
                ('message', models.CharField(blank=True, max_length=240, verbose_name='检测结果')),
                ('detail', models.JSONField(blank=True, default=dict, verbose_name='检测详情')),
                ('checked_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='检测时间')),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='health_checks', to='k8s_cluster.k8scluster', verbose_name='集群')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_cluster_health_checks', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-checked_at'],
                'indexes': [models.Index(fields=['cluster', 'checked_at'], name='k8s_health_cluster_time_idx')],
            },
        ),
        migrations.AddIndex(
            model_name='k8scluster',
            index=models.Index(fields=['organization', 'status'], name='k8s_cluster_org_status_idx'),
        ),
        migrations.AddIndex(
            model_name='k8scluster',
            index=models.Index(fields=['api_server'], name='k8s_cluster_api_idx'),
        ),
    ]
