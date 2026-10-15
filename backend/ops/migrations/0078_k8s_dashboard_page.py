"""在「K8s管理」顶层最前面新增「总览」页面。

总览是跨集群的汇总视图，与已有的「集群总览」（`/k8s/overview`，单集群视角）不是同一页，
因此新增独立菜单项而不是改造旧页：旧页的 code、path 与接口一个都没动。

页面是纯只读，没有操作码；`code` 前缀必须是 ``page.`` —— 写成 ``menu.`` 会被
``parent_codes_for_permission`` 收进权限链，给全平台用户凭空加一个必需权限。
"""

from django.db import migrations
from django.db.models import F


K8S_GROUP_CODE = 'menu.k8s'
DASHBOARD_PAGE_CODE = 'page.k8s.dashboard.view'
DASHBOARD_PAGE_NAME = '总览'
DASHBOARD_PAGE_PATH = '/k8s/dashboard'
DASHBOARD_PAGE_ICON = 'ChartPie'
# 插在「集群总览」（顶层 sort_order 1）之前，是 K8s 菜单的第一个入口。
DASHBOARD_PAGE_ORDER = 1


def _backfill_rules(apps, code):
    """为既有权限策略补上总览页的权限规则。

    参数：`apps` 表示迁移期应用注册表；`code` 表示本次新增的权限代码。
    返回：无显式返回值。
    副作用：向权限规则表批量写入记录。

    沿用 0070/0071 的口径：策略里已有通配符就补 allow，否则补 deny，
    保证新页面不会绕过既有策略被默认放行。
    """
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    for policy in PermissionPolicy.objects.all().iterator():
        existing = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        if code in existing:
            continue
        PermissionRule.objects.create(
            policy=policy,
            permission_code=code,
            effect='allow' if '*' in existing else 'deny',
        )


def _prepend_menu_order(Preference):
    """把总览插到用户个人菜单顺序的最前面。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。

    菜单排序按代码定位、不认识的代码一律排到末尾，不处理的话存过顺序的用户
    会把总览看到最后，而不是设计要的最上面。只在最前面插入这一条，
    用户自己调过的相对顺序原样保留。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(K8S_GROUP_CODE)
        if not isinstance(order, list) or DASHBOARD_PAGE_CODE in order:
            continue
        order_data[K8S_GROUP_CODE] = [DASHBOARD_PAGE_CODE, *order]
        preference.order_data = order_data
        preference.save()


def _drop_menu_order(Preference):
    """从用户个人菜单顺序里摘掉总览。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(K8S_GROUP_CODE)
        if not isinstance(order, list) or DASHBOARD_PAGE_CODE not in order:
            continue
        order_data[K8S_GROUP_CODE] = [code for code in order if code != DASHBOARD_PAGE_CODE]
        preference.order_data = order_data
        preference.save()


def seed_dashboard_page(apps, schema_editor):
    """写入总览页菜单节点，并为既有策略回填权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单节点与权限规则表写入数据，并修改用户菜单排序偏好。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not group:
        return

    node = MenuNode.objects.filter(code=DASHBOARD_PAGE_CODE).first()
    if node is None:
        # 先腾出插入位置。只挪侧边栏可见的顶层项（两个散项加五个分组父节点），
        # 部署向导、清理进度这些 sidebar=False 的页面不参与排序，不动它们。
        # 腾位只在首次写入时做，重复执行不会把顺序越推越大。
        MenuNode.objects.filter(parent=group, sidebar=True).update(sort_order=F('sort_order') + 1)
        node = MenuNode(code=DASHBOARD_PAGE_CODE)

    node.parent = group
    node.node_type = 'page'
    node.name = DASHBOARD_PAGE_NAME
    node.path = DASHBOARD_PAGE_PATH
    node.icon = DASHBOARD_PAGE_ICON
    node.sidebar = True
    node.sort_order = DASHBOARD_PAGE_ORDER
    node.is_active = True
    node.save()

    _backfill_rules(apps, DASHBOARD_PAGE_CODE)
    _prepend_menu_order(UserMenuOrderPreference)


def unseed_dashboard_page(apps, schema_editor):
    """回滚总览页：删除菜单节点与权限规则，并把顶层顺序还原。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点与权限规则记录，并修改用户菜单排序偏好。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    node = MenuNode.objects.filter(code=DASHBOARD_PAGE_CODE).first()
    if node:
        # 总览是只读页，seed 没建操作码；这里照样清一遍，
        # 免得以后有人给它加了操作，回滚时留下悬挂的 MenuAction。
        MenuAction.objects.filter(page=node).delete()
        node.delete()
    PermissionRule.objects.filter(permission_code=DASHBOARD_PAGE_CODE).delete()

    if group:
        # 与腾位对称：只还原侧边栏可见的顶层项，位移量与 seed 完全一致。
        MenuNode.objects.filter(parent=group, sidebar=True, sort_order__gt=DASHBOARD_PAGE_ORDER).update(
            sort_order=F('sort_order') - 1,
        )

    _drop_menu_order(UserMenuOrderPreference)


class Migration(migrations.Migration):

    dependencies = [('ops', '0077_k8s_menu_grouping')]

    operations = [migrations.RunPython(seed_dashboard_page, unseed_dashboard_page)]
