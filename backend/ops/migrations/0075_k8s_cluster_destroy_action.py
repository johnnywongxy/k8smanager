"""为「K8s 集群」页面补上「清理集群」的操作权限。

本轮新增了 ``POST /k8s/clusters/{pk}/destroy``，权限码取视图方法名 ``destroy_cluster``
（``K8sClusterViewSet.destroy_cluster``，路径是 ``destroy`` 但权限码跟着方法名走，见
``ops/permissions.py::_action_name``）。不 seed 的话，按「有规则才允许」的策略语义，
**所有人都提交不了清理任务**，界面上会看到一个点了没反应的按钮。

与 0073 的取舍一致：只登记确实存在接口的动作。命名上刻意与「删除集群记录」
（``delete``，对应 ``ModelViewSet`` 的 ``DELETE``）区分开——那是两件完全不同的事。
"""

from django.db import migrations


CLUSTER_PAGE_CODE = 'page.k8s.clusters.view'
DESTROY_ACTION = ('destroy_cluster', '清理集群')


def seed_destroy_action(apps, schema_editor):
    """为「K8s 集群」页面补上清理操作权限并回填既有策略规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单操作与权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    page = MenuNode.objects.filter(code=CLUSTER_PAGE_CODE).first()
    if not page:
        return

    prefix = CLUSTER_PAGE_CODE.removesuffix('.view')
    existing = list(MenuAction.objects.filter(page=page).values_list('code', 'sort_order'))
    sort_order = max([item[1] or 0 for item in existing] or [0])
    code, name = DESTROY_ACTION
    sort_order += 1
    item, _created = MenuAction.objects.get_or_create(page=page, code=code)
    item.name = name
    item.sort_order = sort_order
    item.is_active = True
    item.save()
    permission_code = f'{prefix}.{code}'

    for policy in PermissionPolicy.objects.all().iterator():
        current = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        if permission_code in current:
            continue
        # 通配策略跟着放开，其余策略默认拒绝：清理是高危操作，不该被自动授权。
        PermissionRule.objects.create(
            policy=policy,
            permission_code=permission_code,
            effect='allow' if '*' in current else 'deny',
        )


def unseed_destroy_action(apps, schema_editor):
    """回滚「清理集群」的操作权限。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作与对应的权限规则。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    code, _name = DESTROY_ACTION
    page = MenuNode.objects.filter(code=CLUSTER_PAGE_CODE).first()
    if page:
        MenuAction.objects.filter(page=page, code=code).delete()
    prefix = CLUSTER_PAGE_CODE.removesuffix('.view')
    PermissionRule.objects.filter(permission_code=f'{prefix}.{code}').delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0074_k8s_deploy_delete_action')]

    operations = [migrations.RunPython(seed_destroy_action, unseed_destroy_action)]
