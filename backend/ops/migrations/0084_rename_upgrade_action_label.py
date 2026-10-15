"""把「K8s 集群」页里升级操作的显示名从「补丁升级」改成「版本升级」。

0073 建这个权限码时平台还只能做同次版本内的补丁升级，所以名字叫「补丁升级」。现在
平台支持升到下一个次版本（1.35 → 1.36），前后端与任务类型都已改叫「版本升级」，
权限策略页里的这一个开关若还写着旧名字，同一个动作在两处有两个叫法 —— 授权的人
会以为它是另一个功能。权限码 ``page.k8s.clusters.upgrade`` 一个字没动，改的只是
``PermissionMenuAction.name`` 这一份展示文案，因此没有回填权限规则的问题。

只改文案、不动 ``sort_order`` 与 ``is_active``：这一条迁移不该顺带调整别人的排序。
"""

from django.db import migrations

CLUSTER_PAGE_CODE = 'page.k8s.clusters.view'
UPGRADE_ACTION_CODE = 'upgrade'
OLD_NAME = '补丁升级'
NEW_NAME = '版本升级'


def rename_upgrade_action(apps, schema_editor):
    """把升级操作的显示名改成「版本升级」。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：更新菜单操作表的一行文案。

    只在当前值确实还是旧名字时才改：若这一行已经被别处改成别的叫法（例如有人手工
    调整过），迁移应当什么都不做，而不是把它覆盖回来。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')

    page = MenuNode.objects.filter(code=CLUSTER_PAGE_CODE).first()
    if not page:
        return
    action = MenuAction.objects.filter(page=page, code=UPGRADE_ACTION_CODE).first()
    if not action or action.name != OLD_NAME:
        return
    action.name = NEW_NAME
    action.save(update_fields=['name'])


def restore_upgrade_action(apps, schema_editor):
    """把显示名改回「补丁升级」。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：更新菜单操作表的一行文案。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')

    page = MenuNode.objects.filter(code=CLUSTER_PAGE_CODE).first()
    if not page:
        return
    action = MenuAction.objects.filter(page=page, code=UPGRADE_ACTION_CODE).first()
    if not action or action.name != NEW_NAME:
        return
    action.name = OLD_NAME
    action.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [('ops', '0083_remove_k8s_yaml_page')]

    operations = [migrations.RunPython(rename_upgrade_action, restore_upgrade_action)]
