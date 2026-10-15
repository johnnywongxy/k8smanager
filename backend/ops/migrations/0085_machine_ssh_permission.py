from django.db import migrations


PAGE_CODE = 'page.machine.servers.view'
ACTION_CODE = 'ssh'
ACTION_NAME = 'SSH 登录'
PERMISSION_CODE = 'page.machine.servers.ssh'
# 排在既有四个动作（create / update / delete / test）之后。
ACTION_SORT_ORDER = 5


def seed_ssh_action(apps, schema_editor):
    """给机器列表页补上「SSH 登录」动作权限，并为既有策略回填默认规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单操作和权限规则表写入数据。

    权限目录整棵都是数据库驱动的（``ops.permission_catalog`` 从
    ``PermissionMenuNode`` / ``PermissionMenuAction`` 读），所以这里只写一行动作，
    「用户管理 → 权限策略」里就会多出这一项可勾选的权限。

    回填规则与 ``0069_machine_permission_menu`` 一致：**通配策略（``*``）继续放行，
    其余策略一律先拒**。SSH 到生产机器是直连 shell，默认不跟着既有角色一起放开；
    需要的人在权限策略里自己勾。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    page = MenuNode.objects.filter(code=PAGE_CODE).first()
    if page is None:
        # 机器菜单还没种下去（例如从更早的版本一路 migrate 上来时顺序异常），
        # 那就不该在这里凭空造页面；动作留给 0069 的既有结果，下次 migrate 再补。
        return
    action, _ = MenuAction.objects.get_or_create(page=page, code=ACTION_CODE)
    action.name = ACTION_NAME
    action.sort_order = ACTION_SORT_ORDER
    action.is_active = True
    action.save()

    for policy in PermissionPolicy.objects.all().iterator():
        if PermissionRule.objects.filter(policy=policy, permission_code=PERMISSION_CODE).exists():
            continue
        wildcard = PermissionRule.objects.filter(policy=policy, permission_code='*').exists()
        PermissionRule.objects.create(
            policy=policy,
            permission_code=PERMISSION_CODE,
            effect='allow' if wildcard else 'deny',
        )


def unseed_ssh_action(apps, schema_editor):
    """回滚「SSH 登录」动作及其权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作与权限规则数据。
    """
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    MenuAction.objects.filter(page__code=PAGE_CODE, code=ACTION_CODE).delete()
    PermissionRule.objects.filter(permission_code=PERMISSION_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0084_rename_upgrade_action_label')]

    operations = [migrations.RunPython(seed_ssh_action, unseed_ssh_action)]
