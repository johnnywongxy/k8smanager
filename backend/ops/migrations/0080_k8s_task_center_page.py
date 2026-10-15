"""把「部署任务」抽屉升级成独立页面「任务中心」，排在「集群接入」下面。

改造的是 ``page.k8s.deploy.view`` **同一个节点**，不新建页面码。原因在接口那一侧：
``/k8s/deployments/`` 前缀已经映射到这个页面码（``ops/permissions.py:22``），另立
``page.k8s.tasks.view`` 会造成「菜单要一个码、接口要另一个码」的双码局面 —— 拿到菜单码
的人点开页面，每次请求仍然 403。所以这里只改显示名、路由与排序，code 一个字节都不动，
也因此**不需要回填任何权限规则**。

**原 path 降级成路由别名，不能直接丢弃**：``/k8s/clusters/deploy`` 是部署向导的入口，
``K8sClusterPage`` 的「部署集群」按钮、``K8sDeployTaskListDrawer`` 的权限解析都按它取
页面码。别名同样会被 ``navigation.configureNavigation`` 压进路由表、被
``permissions.registerPath`` 登记路径→页面码，所以那两处照旧可用（0076 用同样的办法
让清理进度页复用本页的权限）。

**别名必须追加**：本页现有两条别名（部署进度 ``/k8s/clusters/deployments/:id``、
清理进度 ``/k8s/clusters/destroy/:id``），整体赋值会一次干掉它们 —— 那两个页面的路由和
前端权限映射会同时消失，且不会有任何报错。照 0076 的写法先按 path 过滤同名项再 append。

**一个 admin 可见的副作用**：``permission_catalog._iter_permissions`` 用
``f'{page["name"]}-{action["name"]}'`` 拼操作权限名，改名后策略页会出现
「任务中心-提交任务／预检／重试／取消／删除任务」。这 5 个动作名是部署向导的语义，
本迁移保留不动 —— 一起改等于一次动 5 个 admin 可见名称，收益不明显。
"""

from django.db import migrations
from django.db.models import F


DEPLOY_PAGE_CODE = 'page.k8s.deploy.view'
DEPLOY_PAGE_NAME = '任务中心'
DEPLOY_PAGE_PATH = '/k8s/tasks'
DEPLOY_PAGE_ICON = 'ListOrdered'
# 紧跟「集群接入」（sort_order 3）。
DEPLOY_PAGE_ORDER = 4
# 回滚要还原的原值。
DEPLOY_LEGACY_NAME = '部署集群'
DEPLOY_LEGACY_PATH = '/k8s/clusters/deploy'
DEPLOY_LEGACY_ICON = 'Rocket'
# 原来排在分组之后（sidebar=False，不参与侧边栏排序，只留个历史编号）。
DEPLOY_LEGACY_ORDER = 21

K8S_GROUP_CODE = 'menu.k8s'
# 部署向导的入口路径，降级成别名后继续注册。
WIZARD_ALIAS_PATH = '/k8s/clusters/deploy'
WIZARD_ALIAS = {'path': WIZARD_ALIAS_PATH, 'name': '部署集群'}

# 0077 建立的五个分组容器：它们原本占 4-8，要给「任务中心」腾出第 4 位。
# 显式列出而不是按 `sort_order >= 4` 过滤：这样回滚时移回来的正好是这五个，
# 不会把以后新增的节点一起挪动。
GROUP_NODE_CODES = [
    'page.k8s.group.cluster.view',
    'page.k8s.group.workload.view',
    'page.k8s.group.network.view',
    'page.k8s.group.delivery.view',
    'page.k8s.group.observe.view',
]


def _insert_menu_order(Preference):
    """把「任务中心」插进用户个人菜单顺序里「集群接入」的后面。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。

    菜单排序按代码定位、不认识的代码一律排到末尾。这个页面的 code 没变（改的是
    sidebar），所以存过顺序的用户不会看到它整个消失，但会看到它掉到菜单最后 ——
    而设计要它在「集群接入」下面。找不到参照项时插到最前，至少不违背「排在前面」。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(K8S_GROUP_CODE)
        if not isinstance(order, list) or DEPLOY_PAGE_CODE in order:
            continue
        if 'page.k8s.clusters.view' in order:
            index = order.index('page.k8s.clusters.view') + 1
            order_data[K8S_GROUP_CODE] = [*order[:index], DEPLOY_PAGE_CODE, *order[index:]]
        else:
            order_data[K8S_GROUP_CODE] = [DEPLOY_PAGE_CODE, *order]
        preference.order_data = order_data
        preference.save()


def _drop_menu_order(Preference):
    """从用户个人菜单顺序里摘掉「任务中心」。

    参数：`Preference` 表示迁移期的用户菜单排序偏好模型。
    返回：无显式返回值。
    副作用：修改用户菜单排序偏好的持久化数据。
    """
    for preference in Preference.objects.all().iterator():
        order_data = preference.order_data
        if not isinstance(order_data, dict):
            continue
        order = order_data.get(K8S_GROUP_CODE)
        if not isinstance(order, list) or DEPLOY_PAGE_CODE not in order:
            continue
        order_data[K8S_GROUP_CODE] = [code for code in order if code != DEPLOY_PAGE_CODE]
        preference.order_data = order_data
        preference.save()


def promote_task_center(apps, schema_editor):
    """把「部署集群」页面改成侧边栏里的「任务中心」，并追加向导路径别名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：修改菜单节点字段与用户菜单排序偏好。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if not page:
        return

    # 先腾出第 4 位。只在首次写入时做，重复执行不会把顺序越推越大。
    MenuNode.objects.filter(code__in=GROUP_NODE_CODES).update(sort_order=F('sort_order') + 1)

    page.name = DEPLOY_PAGE_NAME
    page.path = DEPLOY_PAGE_PATH
    page.icon = DEPLOY_PAGE_ICON
    page.sidebar = True
    page.sort_order = DEPLOY_PAGE_ORDER

    # 追加而不是覆盖：现有两条进度页别名必须原样留着（见模块 docstring）。
    aliases = [item for item in (page.aliases or []) if item.get('path') != WIZARD_ALIAS_PATH]
    aliases.append(WIZARD_ALIAS)
    page.aliases = aliases
    page.save()

    _insert_menu_order(UserMenuOrderPreference)


def demote_task_center(apps, schema_editor):
    """回滚：还原为不进侧边栏的「部署集群」页面，并摘掉向导路径别名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：修改菜单节点字段与用户菜单排序偏好。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    UserMenuOrderPreference = apps.get_model('ops', 'UserMenuOrderPreference')

    page = MenuNode.objects.filter(code=DEPLOY_PAGE_CODE).first()
    if page:
        page.name = DEPLOY_LEGACY_NAME
        page.path = DEPLOY_LEGACY_PATH
        page.icon = DEPLOY_LEGACY_ICON
        page.sidebar = False
        page.sort_order = DEPLOY_LEGACY_ORDER
        page.aliases = [
            item for item in (page.aliases or []) if item.get('path') != WIZARD_ALIAS_PATH
        ]
        page.save()

    # 与腾位对称：把五个分组移回第 4-8 位。
    MenuNode.objects.filter(code__in=GROUP_NODE_CODES).update(sort_order=F('sort_order') - 1)

    _drop_menu_order(UserMenuOrderPreference)


class Migration(migrations.Migration):

    dependencies = [('ops', '0079_overview_top_level_menu')]

    operations = [migrations.RunPython(promote_task_center, demote_task_center)]
