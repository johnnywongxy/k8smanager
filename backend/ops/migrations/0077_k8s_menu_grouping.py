"""把「K8s管理」下平铺的 20 个菜单归成 5 个分组加 2 个高频散项。

分组父节点是纯容器：没有 path、不参与前端路由，也不进权限链（前后端都只保留 ``menu.``
开头的父链），因此子页面所需的权限一个码都没变。这个前提是整条迁移能安全上线的原因，
改动时务必守住：只动父子关系、排序和显示名，不碰任何 code 与 path。
"""

from django.db import migrations


K8S_GROUP_CODE = 'menu.k8s'

# 顶层散项：最高频的两个入口，留在顶层一点直达。
LOOSE_PAGES = [
    ('page.k8s.overview.view', 1),
    ('page.k8s.clusters.view', 2),
]

# 分组父节点：(code, 显示名, 图标, 顶层顺序)。
#
# code 前缀必须是 ``page.`` 而不能是 ``menu.``：``parent_codes_for_permission`` 会把
# ``menu.`` 开头的父节点收进权限链，用 ``menu.k8s.*`` 命名会让每个用户凭空多出一个
# 必须持有的权限，前端路由守卫同样会拦；``_iter_permissions`` 也按前缀判类型。
# path 必须留空：``page_code_for_path`` 拿所有页面的 path 做最长匹配来解析接口路径，
# 容器节点配上 path 就会参与进来。
GROUP_NODES = [
    ('page.k8s.group.cluster.view', '集群资源', 'ServerCog', 3),
    ('page.k8s.group.workload.view', '工作负载', 'Layers', 4),
    ('page.k8s.group.network.view', '网络与存储', 'HardDrive', 5),
    ('page.k8s.group.delivery.view', '应用交付', 'Rocket', 6),
    ('page.k8s.group.observe.view', '可观测性', 'ChartSpline', 7),
]

# 每个分组下挂哪些页面，列表顺序就是展示顺序。排序只在同一父节点下比较，
# 所以子页面从 1 重新编号即可，不影响顶层。
GROUPED_PAGES = [
    ('page.k8s.group.cluster.view', [
        'page.k8s.nodes.view',
        'page.k8s.namespaces.view',
        'page.k8s.namespaces.authz.view',
        'page.k8s.config.view',
    ]),
    ('page.k8s.group.workload.view', [
        'page.k8s.workloads.view',
        'page.k8s.pods.view',
        'page.k8s.images.view',
    ]),
    ('page.k8s.group.network.view', [
        'page.k8s.network.view',
        'page.k8s.configs.view',
        'page.k8s.storage.view',
    ]),
    ('page.k8s.group.delivery.view', [
        'page.k8s.helm.view',
        'page.k8s.yaml.view',
    ]),
    ('page.k8s.group.observe.view', [
        'page.k8s.events.view',
        'page.k8s.logs.view',
        'page.k8s.monitoring.view',
        'page.k8s.statistics.view',
        'page.k8s.audit.view',
        'page.k8s.platform.view',
    ]),
]

# 「配置中心」管的是 K8s 版本映射与支持的操作系统，却和集群内的「配置与密钥」撞名。
# 只改显示名，code 与 path 都不动。值为 (新名, 旧名)，旧名供回滚使用。
RENAMED_PAGES = {
    'page.k8s.config.view': ('部署基线', '配置中心'),
}

# 回滚要还原的顶层顺序：0067 的原始编号，加上 0071 把「配置中心」插到第 3 位后整体顺延的结果。
LEGACY_ORDER = {
    'page.k8s.overview.view': 1,
    'page.k8s.clusters.view': 2,
    'page.k8s.config.view': 3,
    'page.k8s.namespaces.view': 4,
    'page.k8s.namespaces.authz.view': 5,
    'page.k8s.nodes.view': 6,
    'page.k8s.images.view': 7,
    'page.k8s.workloads.view': 8,
    'page.k8s.pods.view': 9,
    'page.k8s.network.view': 10,
    'page.k8s.configs.view': 11,
    'page.k8s.storage.view': 12,
    'page.k8s.yaml.view': 13,
    'page.k8s.helm.view': 14,
    'page.k8s.events.view': 15,
    'page.k8s.logs.view': 16,
    'page.k8s.monitoring.view': 17,
    'page.k8s.statistics.view': 18,
    'page.k8s.audit.view': 19,
    'page.k8s.platform.view': 20,
}


def _backfill_rules(apps, codes):
    """为既有权限策略补上缺失的权限规则。

    参数：`apps` 表示迁移期应用注册表；`codes` 表示本次新增的权限代码。
    返回：无显式返回值。
    副作用：向权限规则表批量写入记录。

    沿用 0071 的口径：策略里已有通配符就补 allow，否则补 deny，
    保证新权限不会绕过既有策略被默认放行。
    """
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    for policy in PermissionPolicy.objects.all().iterator():
        existing = set(PermissionRule.objects.filter(policy=policy).values_list('permission_code', flat=True))
        wildcard = '*' in existing
        PermissionRule.objects.bulk_create([
            PermissionRule(policy=policy, permission_code=code, effect='allow' if wildcard else 'deny')
            for code in codes if code not in existing
        ])


def _clear_k8s_menu_order(Preference):
    """清掉用户存过的 K8s 顶层菜单顺序。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。

    `order_data['menu.k8s']` 里存的是分组之前那 20 个页面代码，而菜单排序会按代码定位、
    不认识的代码一律排到末尾 —— 不清掉的话五个新分组会全部掉到菜单最后。
    只删这一个键，用户其它模块的排序偏好原样保留。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict) or K8S_GROUP_CODE not in order_data:
            continue
        order_data.pop(K8S_GROUP_CODE)
        preference.order_data = order_data
        preference.save()


def group_k8s_menu(apps, schema_editor):
    """建立分组父节点，把页面挂进去并改名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单节点与权限规则表写入数据，并修改用户菜单排序偏好。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not group:
        return

    parents = {}
    for code, name, icon, order in GROUP_NODES:
        node, _ = MenuNode.objects.get_or_create(code=code, defaults={'parent': group})
        node.parent = group
        node.node_type = 'page'
        node.name = name
        node.path = ''
        node.icon = icon
        node.sidebar = True
        node.sort_order = order
        node.is_active = True
        node.save()
        parents[code] = node

    for code, order in LOOSE_PAGES:
        MenuNode.objects.filter(code=code).update(parent=group, sort_order=order)

    for group_code, page_codes in GROUPED_PAGES:
        parent = parents.get(group_code)
        if not parent:
            continue
        for index, page_code in enumerate(page_codes, start=1):
            MenuNode.objects.filter(code=page_code).update(parent=parent, sort_order=index)

    for code, (name, _legacy_name) in RENAMED_PAGES.items():
        MenuNode.objects.filter(code=code).update(name=name)

    _backfill_rules(apps, [code for code, _name, _icon, _order in GROUP_NODES])
    _clear_k8s_menu_order(UserMenuOrderPreference)


def ungroup_k8s_menu(apps, schema_editor):
    """还原分组：页面挂回一级菜单、恢复原顺序与原名，并删除分组父节点。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点与权限规则记录，并修改菜单节点字段。

    先把子页面挂回一级菜单再删父节点：父节点是自引用外键的父端，先断开关联
    才不会连带删掉或卡住子节点。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if not group:
        return

    for code, legacy_order in LEGACY_ORDER.items():
        MenuNode.objects.filter(code=code).update(parent=group, sort_order=legacy_order)

    for code, (_name, legacy_name) in RENAMED_PAGES.items():
        MenuNode.objects.filter(code=code).update(name=legacy_name)

    group_codes = [code for code, _name, _icon, _order in GROUP_NODES]
    MenuNode.objects.filter(code__in=group_codes).delete()
    PermissionRule.objects.filter(permission_code__in=group_codes).delete()


class Migration(migrations.Migration):

    dependencies = [('ops', '0076_k8s_cluster_destroy_progress_page')]

    operations = [migrations.RunPython(group_k8s_menu, ungroup_k8s_menu)]
