from django.db import migrations
from django.db.models import F


K8S_GROUP_CODE = 'menu.k8s'
CONFIG_PAGE_CODE = 'page.k8s.config.view'
CONFIG_PAGE_NAME = '配置中心'
CONFIG_PAGE_PATH = '/k8s/config'
CONFIG_PAGE_ICON = 'Wrench'
# 「配置中心」插在「集群接入」（sort_order 2）之后，其后页面顺延。
CONFIG_PAGE_ORDER = 3
CONFIG_ACTIONS = ['create', 'update', 'delete']


def seed_config_menu(apps, schema_editor):
    """写入配置中心页面和操作权限，并为既有策略回填默认规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单节点、操作和权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not group:
        return

    # 先腾出插入位置，避免与「集群接入」之后的页面发生顺序冲突。
    MenuNode.objects.filter(parent=group, sort_order__gte=CONFIG_PAGE_ORDER).update(
        sort_order=F('sort_order') + 1,
    )

    page, _ = MenuNode.objects.get_or_create(code=CONFIG_PAGE_CODE, defaults={'parent': group})
    page.parent = group
    page.node_type = 'page'
    page.name = CONFIG_PAGE_NAME
    page.path = CONFIG_PAGE_PATH
    page.icon = CONFIG_PAGE_ICON
    page.sidebar = True
    page.sort_order = CONFIG_PAGE_ORDER
    page.is_active = True
    page.save()

    all_codes = [CONFIG_PAGE_CODE]
    prefix = CONFIG_PAGE_CODE.removesuffix('.view')
    for action_index, action in enumerate(CONFIG_ACTIONS, start=1):
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


def unseed_config_menu(apps, schema_editor):
    """回滚配置中心页面、操作权限和权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点、操作和权限规则数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    node = MenuNode.objects.filter(code=CONFIG_PAGE_CODE).first()
    if not node:
        return

    action_codes = [f'{CONFIG_PAGE_CODE.removesuffix(".view")}.{action}' for action in CONFIG_ACTIONS]
    MenuAction.objects.filter(page=node).delete()
    node.delete()
    PermissionRule.objects.filter(permission_code__in=[CONFIG_PAGE_CODE, *action_codes]).delete()

    if group:
        MenuNode.objects.filter(parent=group, sort_order__gt=CONFIG_PAGE_ORDER).update(
            sort_order=F('sort_order') - 1,
        )


class Migration(migrations.Migration):

    dependencies = [('ops', '0070_k8s_cluster_deploy_page')]

    operations = [migrations.RunPython(seed_config_menu, unseed_config_menu)]
