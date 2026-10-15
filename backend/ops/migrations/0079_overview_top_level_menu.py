"""把「总览」从「K8s管理」里提出来，变成侧边栏最顶部的独立直达项。

总览统计的是全部集群的汇总，不区分集群，把它挂在「K8s管理」下面等于要求访问者
同时持有 K8s管理 的权限 —— 而它一个 K8s 页面都不碰。本次把权限链从
``['menu.k8s', page]`` 换成 ``['menu.overview', page]``，实现彻底解绑。

**为什么是新建一个 ``node_type='group'`` 的顶层节点，而不是把页面的 parent 置空**：
``permission_catalog.catalog_tree`` 只把「``node_type='group'`` 且没有父节点」的节点收成
一级分组，无父节点的 ``page`` 会被静默丢弃 —— 不进权限目录、不进侧边栏、不进权限链，
页面上是「菜单里整个消失」。用组节点承载则前后端零改动：``App.vue`` 里
``isDirectGroup`` 早就硬编码了 ``menu.overview``（模板版裁掉了平台原生的总览，
这段分支一直是死代码），命中后整组渲染成一个 RouterLink 直达 ``group.path``。

**为什么这个组节点可以带 path，而 0077 的五个分组必须留空**：这两组 path 走的是完全
不同的读法。0077 的规矩（容器节点 path 留空）是为了不污染 ``page_code_for_path`` 的
接口路径最长匹配与 ``_iter_permissions`` 的权限清单；本迁移建的组节点不走那两条路 ——
``_iter_permissions`` 对分组硬编码 ``'path': ''``（见 ``permission_catalog.py:187``），
``page_code_for_path`` 只遍历 ``group['pages']`` 不碰组本身（见 ``:227-236``）。
这里的 path 只被前端的 ``directGroupPath`` 读走，用来决定点这个直达项跳到哪里。
**不要"顺手修好"成空串**：那样它会退回到 ``group[1][0][0]``，再不行就掉到
``'/overview'`` —— 一个本应用里根本不存在的路径。

**为什么权限回填用"镜像"而不是照抄 0078 的通配符启发式**：0070/0071/0077/0078 用
「策略里有没有 ``*``」决定 allow/deny，那是给**全新页面**准备的 deny-by-default 口径。
总览是**搬家**：今天持有 ``menu.k8s + page.k8s.dashboard.view`` 的普通用户，迁移后
因为缺 ``menu.overview`` 会被直接拒绝，下次登录 ``firstAllowedPath`` 会把他们甩到别的页。
而且 ``*`` 那条启发式在本仓库根本不会命中 —— 没有任何迁移创建字面量 ``'*'`` 的规则
（0033 反倒是清理 ``'*'`` 的那个），照抄等于给几乎每条策略都写 deny。
所以这里改成：策略 allow 了 ``page.k8s.dashboard.view``，才补 ``menu.overview = allow``。

**不动 ``menu.k8s`` 的既有授权**：搬家后它对总览不再是必需的，但那些策略里的 ``menu.k8s``
同时还授权着 K8s 下的另外 20 个页面，摘除的收益远小于风险。
"""

from django.db import migrations


OVERVIEW_GROUP_CODE = 'menu.overview'
OVERVIEW_GROUP_NAME = '总览'
OVERVIEW_GROUP_ICON = 'ChartPie'
# 组节点自己的 path，只给前端直达项指路（见模块 docstring）。
OVERVIEW_GROUP_PATH = '/k8s/dashboard'
# 插在所有一级分组之前。现有五个分组的 sort_order 是 5-9，所以直接占 1 即可，
# 不需要像 0078 那样腾位 —— 那是往已有序列中间插才要做的事。
OVERVIEW_GROUP_ORDER = 1

DASHBOARD_PAGE_CODE = 'page.k8s.dashboard.view'
K8S_GROUP_CODE = 'menu.k8s'
ROOT_MENU_ORDER_KEY = '__root__'


def _mirror_rules(apps, source_code, new_code):
    """按页面自身的授权效果，为既有策略补上新的菜单组权限。

    参数：`apps` 表示迁移期应用注册表；`source_code` 表示判断依据的页面权限码；
        `new_code` 表示本次新增的菜单组权限码。
    返回：无显式返回值。
    副作用：向权限规则表写入 allow 记录。

    只补 allow，不补 deny：``permission_service`` 的口径是「没有规则就是拒绝」，
    补一条 deny 和什么都不补效果一样，却会让策略页多出一行没意义的开关。
    """
    PermissionPolicy = apps.get_model('ops', 'PermissionPolicy')
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    for policy in PermissionPolicy.objects.all().iterator():
        rules = dict(
            PermissionRule.objects.filter(policy=policy).values_list('permission_code', 'effect')
        )
        # 策略没授权这个页面（或明确 deny），就不该因为搬家而凭空拿到总览入口。
        if rules.get(source_code) != 'allow' or new_code in rules:
            continue
        PermissionRule.objects.create(policy=policy, permission_code=new_code, effect='allow')


def _drop_rules(apps, new_code):
    """回滚时清掉新菜单组的全部权限规则。

    参数：`apps` 表示迁移期应用注册表；`new_code` 表示即将消失的菜单组权限码。
    返回：无显式返回值。
    副作用：删除权限规则记录。

    不按 _mirror_rules 的条件挑选着删：节点本身马上就要被删掉，留下任何指向它的规则
    都会变成挂在目录外的野码（权限列表里看不到，却仍然参与鉴权）。
    """
    PermissionRule = apps.get_model('ops', 'PermissionRule')
    PermissionRule.objects.filter(permission_code=new_code).delete()


def _prepend_menu_order(Preference):
    """把新菜单组插到用户个人菜单顺序的最前面。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。

    菜单排序按代码定位、不认识的代码一律排到末尾，所以不处理的话，存过顺序的用户
    会把总览看到侧边栏最下面，而不是设计要的最上面。只在最前面插这一条，
    用户自己调过的相对顺序原样保留。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(ROOT_MENU_ORDER_KEY)
        if not isinstance(order, list) or OVERVIEW_GROUP_CODE in order:
            continue
        order_data[ROOT_MENU_ORDER_KEY] = [OVERVIEW_GROUP_CODE, *order]
        preference.order_data = order_data
        preference.save()


def _drop_menu_order(Preference):
    """从用户个人菜单顺序里摘掉新菜单组。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(ROOT_MENU_ORDER_KEY)
        if not isinstance(order, list) or OVERVIEW_GROUP_CODE not in order:
            continue
        order_data[ROOT_MENU_ORDER_KEY] = [code for code in order if code != OVERVIEW_GROUP_CODE]
        preference.order_data = order_data
        preference.save()


def promote_overview(apps, schema_editor):
    """建立顶层「总览」分组，把总览页挂进去，并回填权限与菜单顺序。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单节点与权限规则表写入数据，并修改用户菜单排序偏好。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    page = MenuNode.objects.filter(code=DASHBOARD_PAGE_CODE).first()
    if not page:
        # 总览页不存在（0078 没跑或已被回滚），无从谈起搬家。
        return

    group, _created = MenuNode.objects.get_or_create(
        code=OVERVIEW_GROUP_CODE,
        defaults={'parent': None},
    )
    group.parent = None
    group.node_type = 'group'
    group.name = OVERVIEW_GROUP_NAME
    # 见模块 docstring：这个 path 是给前端 directGroupPath 用的，不能改成空串。
    group.path = OVERVIEW_GROUP_PATH
    group.icon = OVERVIEW_GROUP_ICON
    group.sidebar = True
    group.sort_order = OVERVIEW_GROUP_ORDER
    group.is_active = True
    group.save()

    page.parent = group
    # path 与 code 一个都不动：接口路径解析和前端权限映射都还指着它们。
    page.save(update_fields=['parent', 'updated_at'])

    _mirror_rules(apps, DASHBOARD_PAGE_CODE, OVERVIEW_GROUP_CODE)
    _prepend_menu_order(UserMenuOrderPreference)


def demote_overview(apps, schema_editor):
    """回滚：把总览页挂回 K8s 一级菜单，删除顶层分组及其权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单节点与权限规则记录，并修改菜单节点字段与用户菜单排序偏好。

    先把子页面挂回一级菜单再删组节点：组节点是自引用外键的父端，先断开关联才不会
    连带删掉或卡住子页面。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    page = MenuNode.objects.filter(code=DASHBOARD_PAGE_CODE).first()
    k8s_group = MenuNode.objects.filter(code=K8S_GROUP_CODE).first()
    if page and k8s_group:
        page.parent = k8s_group
        page.save(update_fields=['parent', 'updated_at'])

    MenuNode.objects.filter(code=OVERVIEW_GROUP_CODE).delete()
    _drop_rules(apps, OVERVIEW_GROUP_CODE)
    _drop_menu_order(UserMenuOrderPreference)


class Migration(migrations.Migration):

    dependencies = [('ops', '0078_k8s_dashboard_page')]

    operations = [migrations.RunPython(promote_overview, demote_overview)]
