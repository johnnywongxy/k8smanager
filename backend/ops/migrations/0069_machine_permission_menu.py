from django.db import migrations


MACHINE_GROUP_CODE = 'menu.machine'
MACHINE_PAGES = [
    ('page.machine.servers.view', '机器列表', '/machines/servers', 'ServerCog', ['create', 'update', 'delete', 'test']),
]
# 「机器管理」插在 K8s管理（sort_order 5）之后，原有分组顺延。
TRAILING_GROUP_ORDERS = {
    'menu.user_management': 7,
    'menu.platform': 8,
    'menu.logs': 9,
}


def seed_machine_menu(apps, schema_editor):
    """写入机器管理菜单组、页面和操作权限，并为既有策略回填默认规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单节点、操作和权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    group, _ = MenuNode.objects.get_or_create(
        code=MACHINE_GROUP_CODE,
        defaults={'node_type': 'group', 'name': '机器管理', 'icon': 'ServerCog', 'sidebar': True, 'sort_order': 6},
    )
    group.node_type = 'group'
    group.name = '机器管理'
    group.icon = 'ServerCog'
    group.sidebar = True
    group.sort_order = 6
    group.is_active = True
    group.save()

    for code, sort_order in TRAILING_GROUP_ORDERS.items():
        MenuNode.objects.filter(code=code).update(sort_order=sort_order)

    all_codes = [group.code]
    for index, (code, name, path, icon, actions) in enumerate(MACHINE_PAGES, start=1):
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


def unseed_machine_menu(apps, schema_editor):
    """回滚机器管理菜单节点和权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点、操作和权限规则数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    node = MenuNode.objects.filter(code=MACHINE_GROUP_CODE).first()
    if not node:
        return
    page_codes = [item[0] for item in MACHINE_PAGES]
    action_codes = [
        f'{code.removesuffix(".view")}.{action}'
        for code, _name, _path, _icon, actions in MACHINE_PAGES
        for action in actions
    ]
    MenuAction.objects.filter(page__parent=node).delete()
    MenuNode.objects.filter(code__in=page_codes).delete()
    node.delete()
    PermissionRule.objects.filter(permission_code__in=[MACHINE_GROUP_CODE, *page_codes, *action_codes]).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0068_extend_password_expiry')]

    operations = [migrations.RunPython(seed_machine_menu, unseed_machine_menu)]
