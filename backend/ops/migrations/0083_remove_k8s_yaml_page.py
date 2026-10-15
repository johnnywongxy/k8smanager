"""删除「YAML管理」页面，连同菜单节点与全部权限规则。

``page.k8s.yaml.view`` 整页下线（前端页面、后端接口、数据表一起删），这个码在所有
环境里都不该再存在。它挂在「应用交付」分组（``page.k8s.group.delivery.view``）下，
那一组是 0077 建的五组里唯一**会被清空**的一组：组里原本只有它和 ``page.k8s.helm.view``，
它一走，「应用交付」下只剩 Helm管理。

分组节点本身保留：留着一个只有一页的分组不影响任何逻辑（``catalog_tree`` 照常渲染），
而删掉分组要连带改 Helm 页的父节点、用户菜单顺序与所有环境的策略，收益是零。

**动作码必须一并清掉**：0067 给这一页配过 ``validate`` 与 ``apply`` 两个动作，两条
``PermissionRule`` 记录按码存在规则表里。``PermissionMenuAction`` 随节点级联删除，
但 ``PermissionRule.permission_code`` 只是普通 CharField —— 只删节点的话，
``page.k8s.yaml.validate`` / ``page.k8s.yaml.apply`` 会变成挂在目录外的野码：
权限策略页里看不到（目录只遍历菜单树），``permission_service`` 计算权限时却仍按码匹配。
所以这里按 ``page.k8s.yaml.`` 前缀清规则，页面码与两个动作码一次清干净（口径同 0082）。

**为什么要清用户菜单顺序**：``order_data`` 按父节点代码分键存有序列表，不认识的代码
一律排到末尾。这个码既可能在 ``page.k8s.group.delivery.view`` 键下（0077 之后），
也可能残留在 ``menu.k8s`` 键下（0077 会清掉 ``menu.k8s``，但别的环境可能在那之后
又存过一次），因此对所有键下的列表都做一次过滤。

**回滚**：把节点、动作与规则按 0067 / 0077 的原值补回去（名称、路径、图标、组内序号 13、
动作清单），规则只补 deny 不补 allow —— ``permission_service`` 的口径是「没有规则就是拒绝」，
补 allow 等于回滚时凭空给所有策略开了这一页的权限。回滚只负责「这个权限项回来了」，
页面本身与 ``K8sYamlApplyRecord`` 表得靠回退代码提交再跑 ``migrate`` 才回得来。
"""

from django.db import migrations


# (页面权限码, 显示名, 路径, 图标, 应用交付分组内的序号, 0067 播种的动作码)
PAGE = ('page.k8s.yaml.view', 'YAML管理', '/k8s/yaml', 'FileCode2', 13, ['validate', 'apply'])
DELIVERY_GROUP_CODE = 'page.k8s.group.delivery.view'
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

    遍历 `order_data` 的每一个键、每一个列表：这个码挂在哪个键下与环境有关。
    用户其它模块调过的顺序原样保留。
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


def remove_yaml_page(apps, schema_editor):
    """删除该页面的菜单节点与其全部权限规则，并清理用户菜单顺序。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点及其从属动作记录、删除权限规则、修改用户菜单排序偏好。

    节点先删、规则后删：`PermissionMenuAction` 的外键是级联删除，页面节点一走，
    它名下那两个动作记录跟着一起没。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    page_code = PAGE[0]
    MenuNode.objects.filter(code=page_code).delete()
    PermissionRule.objects.filter(permission_code__startswith=f'{page_code.removesuffix(".view")}.').delete()
    _drop_menu_order(UserMenuOrderPreference, [page_code] + _action_codes(page_code, PAGE[5]))


def restore_yaml_page(apps, schema_editor):
    """回滚：按 0067 / 0077 的原值补回节点与动作，并给每条策略补 deny 规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：写入菜单节点、页面动作与权限规则记录。

    父节点优先挂回「应用交付」分组；分组不在（0077 被回滚过）就退回 K8s 一级菜单，
    总比补出来的页面挂不上强。两级都不在说明更早的迁移也被回滚了，直接放弃。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    parent = MenuNode.objects.filter(code=DELIVERY_GROUP_CODE).first() or MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not parent:
        return

    code, name, path, icon, order, actions = PAGE
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
        ('ops', '0082_remove_k8s_observability_pages'),
    ]

    operations = [
        migrations.RunPython(remove_yaml_page, restore_yaml_page),
    ]
