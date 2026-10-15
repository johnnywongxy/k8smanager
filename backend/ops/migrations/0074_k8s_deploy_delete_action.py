from django.db import migrations


DEPLOY_PAGE_CODE = 'page.k8s.deploy.view'
# 部署任务列表新增的删除动作。权限层把 DRF 的 destroy 解析成 delete，页面码由
# ``/k8s/deployments`` 前缀别名落到 ``page.k8s.deploy.view``，因此操作码是
# ``page.k8s.deploy.delete``。不登记这一条，除权限管理员外所有人点删除都会 403。
DELETE_ACTION = ('delete', '删除任务')


def seed_delete_action(apps, schema_editor):
    """为「部署集群」页面补删除操作权限，并回填既有策略规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单操作和权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if not page:
        return

    code, name = DELETE_ACTION
    item, _created = MenuAction.objects.get_or_create(page=page, code=code)
    item.name = name
    # 排在 create/dry_run/retry/cancel（1-4）之后，只影响权限策略页的展示顺序。
    item.sort_order = 5
    item.is_active = True
    item.save()

    action_code = f'{DEPLOY_PAGE_CODE.removesuffix(".view")}.{code}'
    for policy in PermissionPolicy.objects.all().iterator():
        existing = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        if action_code in existing:
            continue
        # 与既有动作码一致：通配策略默认放行，其余默认拒绝，等管理员显式勾选。
        PermissionRule.objects.create(
            policy=policy,
            permission_code=action_code,
            effect='allow' if '*' in existing else 'deny',
        )


def unseed_delete_action(apps, schema_editor):
    """回滚删除操作权限及其权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作和权限规则数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    code, _name = DELETE_ACTION
    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if page:
        MenuAction.objects.filter(page=page, code=code).delete()
    PermissionRule.objects.filter(
        permission_code=f'{DEPLOY_PAGE_CODE.removesuffix(".view")}.{code}',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0073_k8s_cluster_ops_actions')]

    operations = [migrations.RunPython(seed_delete_action, unseed_delete_action)]
