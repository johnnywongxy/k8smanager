"""删除「集群总览」页面，连同它的菜单节点与全部权限规则。

这一页整个下线（前端页面、后端接口、权限项一起删），不是改名也不是搬家：
`page.k8s.overview.view` 这个码在所有环境里都不该再存在。

**为什么必须连 `PermissionRule` 一起删，而不能只删菜单节点**：`PermissionRule.permission_code`
是一个普通 CharField，不是指向 `PermissionMenuNode` 的外键 —— 删节点不会级联带走规则。
留下的规则会变成挂在目录外的野码：权限策略页里看不到它（目录只遍历菜单树）、
但 `permission_service` 计算权限时仍然按码匹配。0079 的 `_drop_rules` 注释里把这类
残留叫「野码」，处置口径一致。

**为什么要清用户菜单顺序**：`UserMenuOrderPreference.order_data['menu.k8s']` 里存着
当时那批页面代码，菜单排序按代码定位、不认识的代码一律排到末尾。只摘掉这一个码、
`menu.k8s` 这个键与其它模块的排序原样保留。

**排序不重编号**：「集群总览」原占 `menu.k8s` 下的第 2 位，删掉后 3-9 留一个空号。
`sort_order` 只在同级内比较，空号不影响任何顺序；重编号要动 8 个节点，
而下一个往第 2 位插入的迁移用 0079 那样「直接占 1」的写法本来也不看空号，
所以这里不做无谓的搬动。

**回滚**：把节点与规则按 0067 的原始数据补回去（`sort_order=2`、图标 `LayoutDashboard`），
规则只补 deny 不补 allow —— `permission_service` 的口径是「没有规则就是拒绝」，
补 allow 等于回滚时凭空给所有策略开了这个页面的权限。
"""

from django.db import migrations


OVERVIEW_PAGE_CODE = 'page.k8s.overview.view'
OVERVIEW_PAGE_NAME = '集群总览'
OVERVIEW_PAGE_PATH = '/k8s/overview'
OVERVIEW_PAGE_ICON = 'LayoutDashboard'
# 0067 播种时的位置：`menu.k8s` 下的第 2 项（第 1 项是 0079 之前的总览页位置编号）。
OVERVIEW_PAGE_ORDER = 2
K8S_GROUP_CODE = 'menu.k8s'


def _drop_menu_order(Preference):
    """从用户个人菜单顺序里摘掉即将消失的页面码。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。

    只动 `menu.k8s` 这一个键、只删这一个码：用户其它模块调过的顺序原样保留。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(K8S_GROUP_CODE)
        if not isinstance(order, list) or OVERVIEW_PAGE_CODE not in order:
            continue
        order_data[K8S_GROUP_CODE] = [code for code in order if code != OVERVIEW_PAGE_CODE]
        preference.order_data = order_data
        preference.save()


def _clear_rules(apps, code):
    """清掉指向某个权限码的全部授权规则。

    参数：`apps` 表示迁移期应用注册表；`code` 表示即将消失的权限码。
    返回：无显式返回值。
    副作用：删除权限规则记录。

    不按 allow/deny 挑选着删：节点马上要被删掉，留下任何指向它的规则都会变成野码。
    """
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    PermissionRule.objects.filter(permission_code=code).delete()


def remove_overview_page(apps, schema_editor):
    """删除「集群总览」的菜单节点、页面动作与全部权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点及其从属记录、删除权限规则、修改用户菜单排序偏好。

    节点先删、规则后删：`PermissionMenuAction` 的 `page` 外键是级联删除，
    页面节点一走，它名下那批操作动作（本页 0067 播种时一个动作都没有，
    但别的环境可能有后加的）跟着一起没。顺序反过来也不会报错，
    只是「先删规则、节点删失败」会留下半截状态，先删节点更稳。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    MenuNode.objects.filter(code=OVERVIEW_PAGE_CODE).delete()
    _clear_rules(apps, OVERVIEW_PAGE_CODE)
    _drop_menu_order(UserMenuOrderPreference)


def restore_overview_page(apps, schema_editor):
    """回滚：把菜单节点按 0067 的原值补回 `menu.k8s` 下，并给每条策略补 deny 规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：写入菜单节点与权限规则记录。

    这个回滚只负责「页面的权限项回来了」。页面本身（前端文件、后端接口、缓存表）
    是代码，得靠回退代码提交才回得来 —— 迁移单独回滚会得到一个有权限、
    但点开是空白的菜单项。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not group:
        # K8s 一级菜单都不在（更早的迁移被回滚过），补一个页面也挂不上，直接放弃。
        return
    node, _created = MenuNode.objects.get_or_create(
        code=OVERVIEW_PAGE_CODE,
        defaults={'parent': group},
    )
    node.parent = group
    node.node_type = 'page'
    node.name = OVERVIEW_PAGE_NAME
    node.path = OVERVIEW_PAGE_PATH
    node.icon = OVERVIEW_PAGE_ICON
    node.sidebar = True
    node.sort_order = OVERVIEW_PAGE_ORDER
    node.is_active = True
    node.save()

    for policy in PermissionPolicy.objects.all().iterator():
        exists = PermissionRule.objects.filter(policy=policy, permission_code=OVERVIEW_PAGE_CODE).exists()
        if exists:
            continue
        PermissionRule.objects.create(policy=policy, permission_code=OVERVIEW_PAGE_CODE, effect='deny')


class Migration(migrations.Migration):

    dependencies = [
        ('ops', '0080_k8s_task_center_page'),
    ]

    operations = [
        migrations.RunPython(remove_overview_page, restore_overview_page),
    ]
