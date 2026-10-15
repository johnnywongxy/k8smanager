from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('ops', '0067_k8s_permission_menu'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('k8s_cluster', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='K8sYamlApplyRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('cluster_name', models.CharField(blank=True, help_text='执行 YAML 的目标集群名称', max_length=160, verbose_name='集群名称')),
                ('namespace', models.CharField(blank=True, help_text='YAML 内或表单中指定的 namespace', max_length=160, verbose_name='命名空间')),
                ('action', models.CharField(default='apply', help_text='validate 或 apply', max_length=32, verbose_name='动作')),
                ('resource_refs', models.JSONField(blank=True, default=list, help_text='YAML 解析出的 apiVersion/kind/name/namespace 列表', verbose_name='资源引用')),
                ('yaml_text', models.TextField(help_text='用户提交的 YAML 原文', verbose_name='YAML 内容')),
                ('diff_summary', models.JSONField(blank=True, default=dict, help_text='服务端基于资源是否存在生成的轻量摘要', verbose_name='变更摘要')),
                ('status', models.CharField(default='completed', max_length=32, verbose_name='状态')),
                ('message', models.CharField(blank=True, max_length=240, verbose_name='结果消息')),
                ('actor', models.CharField(blank=True, max_length=120, verbose_name='操作者')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_yaml_apply_records', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['cluster_name', 'namespace', 'created_at'], name='k8s_yaml_record_scope_idx'), models.Index(fields=['action', 'status'], name='k8s_yaml_record_action_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sResourceSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('snapshot_type', models.CharField(default='statistics', max_length=32, verbose_name='快照类型')),
                ('payload', models.JSONField(blank=True, default=dict, verbose_name='快照数据')),
                ('status', models.CharField(default='completed', max_length=32, verbose_name='状态')),
                ('message', models.CharField(blank=True, max_length=240, verbose_name='结果消息')),
                ('generated_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='生成时间')),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='resource_snapshots', to='k8s_cluster.k8scluster', verbose_name='集群')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_resource_snapshots', to='ops.organization', verbose_name='组织')),
            ],
            options={
                'ordering': ['-generated_at'],
                'indexes': [models.Index(fields=['cluster', 'snapshot_type', 'generated_at'], name='k8s_snapshot_scope_idx')],
            },
        ),
        migrations.CreateModel(
            name='K8sNamespacePolicy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, help_text='记录创建时间。')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='记录最后更新时间。')),
                ('name', models.CharField(help_text='namespace 授权规则名称', max_length=160, verbose_name='规则名称')),
                ('role', models.CharField(blank=True, help_text='匹配 UserProfile.role，例如 sre、viewer', max_length=64, verbose_name='授权角色')),
                ('namespace', models.CharField(blank=True, help_text='为空或 * 表示全部 namespace', max_length=160, verbose_name='命名空间')),
                ('resource_types', models.JSONField(blank=True, default=list, help_text='为空或包含 * 表示全部资源类型', verbose_name='资源类型')),
                ('action_scope', models.CharField(choices=[('read', '只读'), ('write', '读写'), ('admin', '管理')], default='read', max_length=32, verbose_name='动作范围')),
                ('valid_from', models.DateTimeField(default=django.utils.timezone.now, verbose_name='生效时间')),
                ('valid_until', models.DateTimeField(blank=True, null=True, verbose_name='过期时间')),
                ('status', models.CharField(choices=[('available', '可用'), ('disabled', '已禁用'), ('expired', '已过期')], default='available', max_length=32, verbose_name='状态')),
                ('remark', models.TextField(blank=True, verbose_name='备注')),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='namespace_policies', to='k8s_cluster.k8scluster', verbose_name='集群')),
                ('department', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='k8s_department_namespace_policies', to='ops.organization', verbose_name='授权部门')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='k8s_namespace_policies', to='ops.organization', verbose_name='组织')),
                ('users', models.ManyToManyField(blank=True, related_name='k8s_namespace_policies', to=settings.AUTH_USER_MODEL, verbose_name='授权用户')),
            ],
            options={
                'ordering': ['cluster_id', 'namespace', 'name'],
                'indexes': [models.Index(fields=['organization', 'status'], name='k8s_ns_policy_org_status_idx'), models.Index(fields=['cluster', 'namespace'], name='k8s_ns_policy_scope_idx')],
            },
        ),
    ]
