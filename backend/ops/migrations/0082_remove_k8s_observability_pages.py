"""删除「高级能力」「日志查询」「监控看板」三个页面，连同菜单节点与全部权限规则。

三页整页下线（前端页面、后端接口、权限项一起删），`page.k8s.platform.view`、
`page.k8s.logs.view`、`page.k8s.monitoring.view` 三个码在所有环境里都不该再存在。
它们都挂在「可观测性」分组（`page.k8s.group.observe.view`）下，组里还剩事件中心、
资源统计、操作审计三页，分组本身保留。

**动作码必须一并清掉**：这三页与「集群总览」不同 —— 0067 播种时给它们各自配过一批操作
动作（日志页的 test、监控页的 promql、高级能力页的 exec 等），每个动作码在权限规则表里
都有一条对应记录。`PermissionMenuAction` 是级联删除（页面节点一走，动作记录跟着没），
但 `PermissionRule.permission_code` 只是普通 CharField：节点删掉后，`page.k8s.logs.test`
这类动作码会变成挂在目录外的野码 —— 权限策略页里看不到（目录只遍历菜单树），
`permission_service` 计算权限时却仍按码匹配。所以这里按 `page.k8s.logs.` 这样的**前缀**
清规则，页面码与动作码一次清干净。0079 的 `_drop_rules` 注释里把这类残留叫「野码」，
处置口径一致。

**为什么要清用户菜单顺序**：`order_data` 按父节点代码分键存有序列表，不认识的代码一律
排到末尾。这三个码既可能在 `page.k8s.group.observe.view` 键下（0077 之后），也可能残留在
`menu.k8s` 键下（0077 会清掉 `menu.k8s`，但别的环境可能在那之后又存过一次）。因此这里对
所有键下的列表都做一次过滤，而不是只认某一个键。

**排序不重编号**：三页在可观测性组里原占第 2、3、6 位，删掉后留三个空号。`sort_order`
只在同级内比较，空号不影响任何顺序；重编号要动组内另外三页，而下一个往组里插入的迁移
本来也不看空号，所以不做无谓的搬动。

**回滚**：把节点、动作与规则按 0067 / 0077 的原值补回去（名称、路径、图标、组内序号、
动作清单），规则只补 deny 不补 allow —— `permission_service` 的口径是「没有规则就是拒绝」，
补 allow 等于回滚时凭空给所有策略开了这三个页面的权限。回滚只负责「这三个权限项回来了」，
页面本身（前端文件、后端接口、数据源表）得靠回退代码提交才回得来。
"""

from django.db import migrations


# (页面权限码, 显示名, 路径, 图标, 可观测性分组内的序号, 0067 播种的动作码)
#
# 前四项取自 0067_k8s_permission_menu 的 K8S_PAGES，序号取自 0077_k8s_menu_grouping
# 的可观测性分组（事件中心 1、日志查询 2、监控看板 3、资源统计 4、操作审计 5、高级能力 6）。
OBSERVABILITY_PAGES = [
    ('page.k8s.logs.view', '日志查询', '/k8s/logs', 'FileText', 2, ['create', 'update', 'exec', 'delete', 'test', 'export']),
    ('page.k8s.monitoring.view', '监控看板', '/k8s/monitoring', 'Activity', 3, ['create', 'update', 'delete', 'test', 'promql']),
    ('page.k8s.platform.view', '高级能力', '/k8s/platform', 'Wrench', 6, ['create', 'update', 'delete', 'test', 'exec', 'export']),
]
OBSERVE_GROUP_CODE = 'page.k8s.group.observe.view'
K8S_GROUP_CODE = 'menu.k8s'


def _action_codes(page_code, actions):
    """把页面码与动作清单拼成操作权限码，口径同 0067 的播种循环。

    参数：`page_code` 表示页面权限码；`actions` 表示该页面的动作名清单。
    返回：返回该页面全部操作权限码组成的列表。
    副作用：不直接修改持久化数据。
    """
    prefix = page_code.removesuffix('.view')
    return [f'{prefix}.{action}' for action in actions]


def _drop_menu_order(Preference, codes):
    """从所有用户的菜单顺序里摘掉即将消失的页面码。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型；`codes` 表示即将消失的权限码。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。

    遍历 `order_data` 的每一个键、每一个列表：这三个码挂在哪个键下与环境有关，
    只认某一个键（0081 那样）会漏掉另一种存法。用户其它模块调过的顺序原样保留。
    """
    removed = set(codes)
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        changed = False
        for key, order in list(order_data.items()):
            if not isinstance(order, list):
                continue
            kept = [code for code in order if code not in removed]
            if len(kept) != len(order):
                order_data[key] = kept
                changed = True
        if changed:
            preference.order_data = order_data
            preference.save()


def _clear_rules(apps, page_code):
    """清掉指向某个页面及其全部动作的授权规则。

    参数：`apps` 表示迁移期应用注册表；`page_code` 表示即将消失的页面权限码。
    返回：无显式返回值。
    副作用：删除权限规则记录。

    按前缀清：`page.k8s.logs.` 既覆盖页面码 `page.k8s.logs.view`，也覆盖
    `page.k8s.logs.test` 这类动作码。三个前缀都不会误伤别的页面 ——
    没有哪个页面码以 `page.k8s.logs.` / `page.k8s.monitoring.` / `page.k8s.platform.` 开头。
    """
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    PermissionRule.objects.filter(permission_code__startswith=f'{page_code.removesuffix(".view")}.').delete()


def remove_observability_pages(apps, schema_editor):
    """删除三个页面的菜单节点与其全部权限规则，并清理用户菜单顺序。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点及其从属动作记录、删除权限规则、修改用户菜单排序偏好。

    节点先删、规则后删：`PermissionMenuAction` 的外键是级联删除，页面节点一走，
    它名下那批动作（以及别的环境后加的动作）跟着一起没。顺序反过来也不会报错，
    只是「先删规则、节点删失败」会留下半截状态，先删节点更稳。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    for code, _name, _path, _icon, _order, _actions in OBSERVABILITY_PAGES:
        MenuNode.objects.filter(code=code).delete()
        _clear_rules(apps, code)
    _drop_menu_order(UserMenuOrderPreference, [item[0] for item in OBSERVABILITY_PAGES])


def restore_observability_pages(apps, schema_editor):
    """回滚：按 0067 / 0077 的原值补回节点与动作，并给每条策略补 deny 规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：写入菜单节点、页面动作与权限规则记录。

    父节点优先挂回「可观测性」分组；分组不在（0077 被回滚过）就退回 K8s 一级菜单，
    总比补出来的页面挂不上强。两级都不在说明更早的迁移也被回滚了，直接放弃。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    parent = MenuNode.objects.filter(code=OBSERVE_GROUP_CODE).first() or MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not parent:
        return

    for code, name, path, icon, order, actions in OBSERVABILITY_PAGES:
        node, _created = MenuNode.objects.get_or_create(code=code, defaults={'parent': parent})
        node.parent = parent
        node.node_type = 'page'
        node.name = name
        node.path = path
        node.icon = icon
        node.sidebar = True
        node.sort_order = order
        node.is_active = True
        node.save()

        for index, action in enumerate(actions, start=1):
            item, _created = MenuAction.objects.get_or_create(page=node, code=action)
            item.name = action
            item.sort_order = index
            item.is_active = True
            item.save()

        codes = [code] + _action_codes(code, actions)
        for policy in PermissionPolicy.objects.all().iterator():
            existing = set(
                PermissionRule.objects
                .filter(policy=policy, permission_code__in=codes)
                .values_list('permission_code', flat=True)
            )
            PermissionRule.objects.bulk_create([
                PermissionRule(policy=policy, permission_code=item, effect='deny')
                for item in codes if item not in existing
            ])


class Migration(migrations.Migration):

    dependencies = [
        ('ops', '0081_remove_k8s_overview_page'),
    ]

    operations = [
        migrations.RunPython(remove_observability_pages, restore_observability_pages),
    ]
