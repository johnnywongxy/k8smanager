from django.db import migrations


K8S_PAGES = [
    ('page.k8s.overview.view', '集群总览', '/k8s/overview', 'LayoutDashboard', []),
    ('page.k8s.clusters.view', '集群接入', '/k8s/clusters', 'Server', ['create', 'update', 'delete', 'disable', 'test', 'sync', 'reveal']),
    ('page.k8s.namespaces.view', '命名空间', '/k8s/namespaces', 'Boxes', ['create', 'update', 'delete', 'authz']),
    ('page.k8s.namespaces.authz.view', 'Namespace授权', '/k8s/namespace-authz', 'ShieldCheck', ['create', 'update', 'delete']),
    ('page.k8s.nodes.view', '节点管理', '/k8s/nodes', 'Server', ['update', 'cordon', 'uncordon', 'drain', 'delete']),
    ('page.k8s.images.view', '镜像管理', '/k8s/images', 'Images', ['export']),
    ('page.k8s.workloads.view', '工作负载', '/k8s/workloads', 'Workflow', ['update', 'scale', 'restart', 'trigger', 'suspend', 'batch', 'delete']),
    ('page.k8s.pods.view', 'Pod管理', '/k8s/pods', 'PackagePlus', ['update', 'delete', 'batch']),
    ('page.k8s.network.view', '服务与入口', '/k8s/network', 'Network', ['create', 'update', 'delete']),
    ('page.k8s.configs.view', '配置与密钥', '/k8s/configs', 'Settings', ['create', 'update', 'reveal', 'delete']),
    ('page.k8s.storage.view', '存储管理', '/k8s/storage', 'Database', ['create', 'update', 'delete']),
    ('page.k8s.yaml.view', 'YAML管理', '/k8s/yaml', 'FileCode2', ['validate', 'apply']),
    ('page.k8s.helm.view', 'Helm管理', '/k8s/helm', 'PackageOpen', ['update', 'delete']),
    ('page.k8s.events.view', '事件中心', '/k8s/events', 'Bell', ['update', 'delete']),
    ('page.k8s.logs.view', '日志查询', '/k8s/logs', 'FileText', ['create', 'update', 'exec', 'delete', 'test', 'export']),
    ('page.k8s.monitoring.view', '监控看板', '/k8s/monitoring', 'Activity', ['create', 'update', 'delete', 'test', 'promql']),
    ('page.k8s.statistics.view', '资源统计', '/k8s/statistics', 'Gauge', ['export']),
    ('page.k8s.audit.view', '操作审计', '/k8s/audit', 'ClipboardList', ['export']),
    ('page.k8s.platform.view', '高级能力', '/k8s/platform', 'Wrench', ['create', 'update', 'delete', 'test', 'exec', 'export']),
]


def seed_k8s_menu(apps, schema_editor):
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    group, _ = MenuNode.objects.get_or_create(
        code='menu.k8s',
        defaults={'node_type': 'group', 'name': 'K8s管理', 'icon': 'Boxes', 'sidebar': True, 'sort_order': 5},
    )
    group.node_type = 'group'
    group.name = 'K8s管理'
    group.icon = 'Boxes'
    group.sidebar = True
    group.is_active = True
    group.save()
    all_codes = [group.code]
    for index, (code, name, path, icon, actions) in enumerate(K8S_PAGES, start=1):
        page, _ = MenuNode.objects.get_or_create(code=code, defaults={'parent': group})
        page.parent = group
        page.node_type = 'page'
        page.name = name
        page.path = path
        page.icon = icon
        page.sidebar = True
        page.sort_order = index
        page.is_active = True
        page.save()
        all_codes.append(code)
        prefix = code.removesuffix('.view')
        for action_index, action in enumerate(actions, start=1):
            item, _ = MenuAction.objects.get_or_create(page=page, code=action)
            item.name = action
            item.sort_order = action_index
            item.is_active = True
            item.save()
            all_codes.append(f'{prefix}.{action}')
    for policy in PermissionPolicy.objects.all().iterator():
        existing = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        wildcard = '*' in existing
        PermissionRule.objects.bulk_create([
            PermissionRule(policy=policy, permission_code=code, effect='allow' if wildcard else 'deny')
            for code in all_codes if code not in existing
        ])


class Migration(migrations.Migration):
    dependencies = [('ops', '0066_llm_provider_setting')]
    operations = [migrations.RunPython(seed_k8s_menu, migrations.RunPython.noop)]
