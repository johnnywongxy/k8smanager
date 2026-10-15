"""为「K8s 集群」页面补上部署型集群的运维操作权限。

这些操作码对应本轮真正落地的接口：
``nodes/add-master``、``nodes/add-worker``、``upgrade``、``backups``、``restore``。
删除某一份备份复用集群的「删除」操作，因此不在这里另立权限码。

权限码只登记**确实存在接口**的动作：先建出权限码但没有接口，权限策略页会显示成可勾选
却永不生效的开关，反而误导授权（与 0072 的取舍一致）。
"""

from django.db import migrations


CLUSTER_PAGE_CODE = 'page.k8s.clusters.view'
CLUSTER_ACTIONS = [
    ('add_master', '添加 Master'),
    ('add_worker', '添加 Node'),
    ('upgrade', '补丁升级'),
    ('backup', '集群备份'),
    ('restore', '备份恢复'),
]


def seed_cluster_actions(apps, schema_editor):
    """为「K8s 集群」页面补操作权限并回填既有策略规则。

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
    # 从现有的最大排序号往后接，避免和既有操作（create/update/delete/view 等）抢位置。
    existing = list(MenuAction.objects.filter(page=page).values_list('code', 'sort_order'))
    sort_order = max([item[1] or 0 for item in existing] or [0])
    all_codes = []
    for code, name in CLUSTER_ACTIONS:
        sort_order += 1
        item, _created = MenuAction.objects.get_or_create(page=page, code=code)
        item.name = name
        item.sort_order = sort_order
        item.is_active = True
        item.save()
        all_codes.append(f'{prefix}.{code}')

    for policy in PermissionPolicy.objects.all().iterator():
        current = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        wildcard = '*' in current
        PermissionRule.objects.bulk_create([
            PermissionRule(policy=policy, permission_code=code, effect='allow' if wildcard else 'deny')
            for code in all_codes if code not in current
        ])


def unseed_cluster_actions(apps, schema_editor):
    """回滚「K8s 集群」页面的运维操作权限。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作与对应的权限规则。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    page = MenuNode.objects.filter(code=CLUSTER_PAGE_CODE).first()
    codes = [code for code, _name in CLUSTER_ACTIONS]
    if page:
        MenuAction.objects.filter(page=page, code__in=codes).delete()
    prefix = CLUSTER_PAGE_CODE.removesuffix('.view')
    PermissionRule.objects.filter(
        permission_code__in=[f'{prefix}.{code}' for code in codes],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0072_k8s_deployment_actions')]

    operations = [migrations.RunPython(seed_cluster_actions, unseed_cluster_actions)]
