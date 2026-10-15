from django.db import migrations


DEPLOY_PAGE_CODE = 'page.k8s.deploy.view'
# 部署任务在「部署集群」页面下的操作。只登记本轮确实落地的接口动作：先建出没有接口的权限码，
# 权限策略页会显示成可勾选却永不生效的开关，反而误导授权。
#
# 集群运维动作（add_master/add_worker/upgrade/backup/restore）不在这里，它们挂在
# ``page.k8s.clusters.view`` 下，由后续的 0073 迁移登记——因为那些接口的前缀是
# ``/k8s/clusters``，权限层按前缀解析页面码，写成 ``page.k8s.deploy.*`` 永远不会命中。
DEPLOY_ACTIONS = [
    ('create', '提交部署'),
    ('dry_run', '预检'),
    ('retry', '重试'),
    ('cancel', '取消'),
]
# 部署进度页不单独占一个页面码，用路由别名复用「部署集群」的页面权限。
DEPLOY_PROGRESS_PATH = '/k8s/clusters/deployments/:id'
DEPLOY_PROGRESS_ALIAS = {'path': DEPLOY_PROGRESS_PATH, 'name': '部署进度'}


def seed_deploy_actions(apps, schema_editor):
    """为「部署集群」页面补操作权限、进度页路由别名，并回填既有策略规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单操作、菜单节点和权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if not page:
        return

    aliases = [item for item in (page.aliases or []) if item.get('path') != DEPLOY_PROGRESS_PATH]
    aliases.append(DEPLOY_PROGRESS_ALIAS)
    page.aliases = aliases
    page.save(update_fields=['aliases'])

    prefix = DEPLOY_PAGE_CODE.removesuffix('.view')
    all_codes = [DEPLOY_PAGE_CODE]
    for action_index, (code, name) in enumerate(DEPLOY_ACTIONS, start=1):
        item, _created = MenuAction.objects.get_or_create(page=page, code=code)
        item.name = name
        item.sort_order = action_index
        item.is_active = True
        item.save()
        all_codes.append(f'{prefix}.{code}')

    for policy in PermissionPolicy.objects.all().iterator():
        existing = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        wildcard = '*' in existing
        PermissionRule.objects.bulk_create([
            PermissionRule(policy=policy, permission_code=code, effect='allow' if wildcard else 'deny')
            for code in all_codes if code not in existing
        ])


def unseed_deploy_actions(apps, schema_editor):
    """回滚「部署集群」页面的操作权限、进度页路由别名和权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作、菜单节点别名和权限规则数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    action_codes = [f'{DEPLOY_PAGE_CODE.removesuffix(".view")}.{code}' for code, _name in DEPLOY_ACTIONS]
    if page:
        MenuAction.objects.filter(page=page, code__in=[code for code, _name in DEPLOY_ACTIONS]).delete()
        page.aliases = [item for item in (page.aliases or []) if item.get('path') != DEPLOY_PROGRESS_PATH]
        page.save(update_fields=['aliases'])
    PermissionRule.objects.filter(permission_code__in=action_codes).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0071_k8s_config_menu')]

    operations = [migrations.RunPython(seed_deploy_actions, unseed_deploy_actions)]
