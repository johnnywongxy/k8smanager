"""把「K8s管理」下的权限项补齐、清掉失效项，并全部改成中文显示名。

改的是「用户管理 → 权限策略」里那一屏能勾什么、每项叫什么。三件事：

1. **补** ``page.k8s.pods.exec``（「进入容器」）。这个动作码从一开始就有人用 ——
   前端 ``K8sResourcePage.vue`` 的「进入容器」按钮按它显隐，容器终端的 WebSocket
   握手（``k8s_platform/consumers.py``）也按它放行 —— 但目录里从来没种过它：
   0067 只在「日志查询 / 高级能力」两页种过 ``exec``，而那两页已被 0082 整页下线。
   结果是除了管理员，谁都不可能在权限策略里勾出这个权限，「进入容器」对普通用户
   永久不可用。这里把它挂回 Pod 管理页。

2. **改** 显示名。0067（K8s 全部页面）与 0069（机器列表）当初写的是
   ``item.name = action``，也就是把动作码本身当显示名，所以权限策略里一直显示
   ``create`` / ``update`` / ``test`` 这种英文。目录的显示名就是数据库里的
   ``PermissionMenuAction.name``，前端没有兜底映射表 —— 想变中文只能改这一列。
   只改 ``name``，权限码一个字不动，因此不影响任何既有授权。

3. **删** ``page.k8s.namespaces.authz``。0067 在「命名空间」页种过这个动作，但全仓
   （前端与后端）没有一处引用它：命名空间授权是独立页 ``/k8s/namespace-authz``，
   用的是 ``page.k8s.namespaces.authz.view`` 那条链。留着它，权限策略里就是一个
   勾了也不管用的开关 —— 比少一个权限项更容易让人误判。

新增的 ``exec`` 按 0069 / 0085 的老规矩回填规则：**通配策略（``*``）继续放行，其余
策略一律先拒**。进容器是在别人机器上开 shell，默认不该跟着既有角色一起放开。
"""

from django.db import migrations


POD_PAGE_CODE = 'page.k8s.pods.view'
POD_EXEC_ACTION = 'exec'
POD_EXEC_NAME = '进入容器'
POD_EXEC_PERMISSION = 'page.k8s.pods.exec'
# 排在 Pod 管理页既有三个动作（update / delete / batch）之后：``PermissionMenuAction.Meta``
# 按 ``sort_order, id`` 排序，这里的顺序就是权限策略里的展示顺序。
POD_EXEC_SORT_ORDER = 4

DEAD_PAGE_CODE = 'page.k8s.namespaces.view'
DEAD_ACTION_CODE = 'authz'
DEAD_PERMISSION_CODE = 'page.k8s.namespaces.authz'
DEAD_ACTION_SORT_ORDER = 4

# 页面权限码 → {动作码: 显示名}。显示名逐条对齐界面上的按钮文字，授权的人看到的
# 名字与他在页面上点到的按钮是同一个词：例如 ``cordon`` 的按钮写「禁止调度」、
# ``reveal`` 在集群页是「查看 KubeConfig」而在配置与密钥页是「查看 Secret」。
ACTION_LABELS = {
    'page.k8s.clusters.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
        'disable': '禁用/启用',
        'test': '连接测试',
        'sync': '同步',
        'reveal': '查看 KubeConfig',
    },
    'page.k8s.namespaces.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
    },
    'page.k8s.namespaces.authz.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
    },
    'page.k8s.nodes.view': {
        'update': '编辑',
        'cordon': '禁止调度',
        'uncordon': '恢复调度',
        'drain': '驱逐',
        'delete': '删除',
    },
    'page.k8s.images.view': {
        'export': '导出',
    },
    'page.k8s.workloads.view': {
        'update': '编辑',
        'scale': '扩缩容',
        'restart': '重启',
        'trigger': '手动触发',
        'suspend': '暂停/恢复',
        'batch': '批量操作',
        'delete': '删除',
    },
    'page.k8s.pods.view': {
        'update': '编辑',
        'delete': '删除',
        'batch': '批量操作',
    },
    'page.k8s.network.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
    },
    'page.k8s.configs.view': {
        'create': '新增',
        'update': '编辑',
        'reveal': '查看 Secret',
        'delete': '删除',
    },
    'page.k8s.storage.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
    },
    'page.k8s.helm.view': {
        'update': '编辑',
        'delete': '删除',
    },
    'page.k8s.events.view': {
        'update': '编辑',
        'delete': '删除',
    },
    'page.k8s.statistics.view': {
        'export': '导出',
    },
    'page.k8s.audit.view': {
        'export': '导出',
    },
    'page.k8s.config.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
    },
    'page.machine.servers.view': {
        'create': '新增',
        'update': '编辑',
        'delete': '删除',
        'test': '连接测试',
    },
}


def _backfill_rules(apps, codes):
    """为既有权限策略补上缺失的权限规则。

    参数：`apps` 表示迁移期应用注册表；`codes` 表示本次新增的权限代码。
    返回：无显式返回值。
    副作用：向权限规则表批量写入记录。

    沿用 0069 / 0085 的口径：策略里已有通配符就补 allow，否则补 deny，
    保证新动作不会绕过既有策略被默认放行。
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


def add_pod_exec_action(apps, schema_editor):
    """给 Pod 管理页补上「进入容器」动作权限，并为既有策略回填默认规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单操作与权限规则表写入数据。

    页面节点不存在时直接返回：那说明这套菜单还没种下去（例如从很旧的版本一路
    migrate 上来），此时凭空造动作只会留下挂不上页面的孤儿权限项。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')

    page = MenuNode.objects.filter(code=POD_PAGE_CODE).first()
    if page is None:
        return
    action, _ = MenuAction.objects.get_or_create(page=page, code=POD_EXEC_ACTION)
    action.name = POD_EXEC_NAME
    action.sort_order = POD_EXEC_SORT_ORDER
    action.is_active = True
    action.save()
    _backfill_rules(apps, [POD_EXEC_PERMISSION])


def remove_pod_exec_action(apps, schema_editor):
    """回滚「进入容器」动作及其权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作与权限规则数据。
    """
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    MenuAction.objects.filter(page__code=POD_PAGE_CODE, code=POD_EXEC_ACTION).delete()
    PermissionRule.objects.filter(permission_code=POD_EXEC_PERMISSION).delete()


def rename_actions_to_chinese(apps, schema_editor):
    """把动作的显示名从英文动作码改成界面上的中文按钮名。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：更新菜单操作表若干行的显示名。

    只在当前显示名还是动作码本身时才改：若这一行已经被别处改成了别的叫法
    （例如有人手工调整过），迁移应当什么都不做，而不是把它覆盖回来。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')

    for page_code, labels in ACTION_LABELS.items():
        page = MenuNode.objects.filter(code=page_code).first()
        if page is None:
            continue
        for action_code, chinese_name in labels.items():
            MenuAction.objects.filter(page=page, code=action_code, name=action_code).update(name=chinese_name)


def restore_action_codes_as_names(apps, schema_editor):
    """回滚显示名：把本次改过的中文名还原成动作码。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：更新菜单操作表若干行的显示名。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')

    for page_code, labels in ACTION_LABELS.items():
        page = MenuNode.objects.filter(code=page_code).first()
        if page is None:
            continue
        for action_code, chinese_name in labels.items():
            MenuAction.objects.filter(page=page, code=action_code, name=chinese_name).update(name=action_code)


def drop_dead_namespace_authz_action(apps, schema_editor):
    """删掉「命名空间」页上从未生效的 ``authz`` 动作及其权限规则。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：删除菜单操作与权限规则数据。
    """
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')
    PermissionRule = apps.get_model('ops', 'PermissionRule')

    MenuAction.objects.filter(page__code=DEAD_PAGE_CODE, code=DEAD_ACTION_CODE).delete()
    PermissionRule.objects.filter(permission_code=DEAD_PERMISSION_CODE).delete()


def restore_dead_namespace_authz_action(apps, schema_editor):
    """回滚：把 ``authz`` 动作与权限规则种回去。

    参数：`apps` 表示迁移期应用注册表；`schema_editor` 表示当前数据库结构编辑器。
    返回：无显式返回值。
    副作用：向菜单操作与权限规则表写入数据。
    """
    MenuNode = apps.get_model('ops', 'PermissionMenuNode')
    MenuAction = apps.get_model('ops', 'PermissionMenuAction')

    page = MenuNode.objects.filter(code=DEAD_PAGE_CODE).first()
    if page is None:
        return
    action, _ = MenuAction.objects.get_or_create(page=page, code=DEAD_ACTION_CODE)
    action.name = DEAD_ACTION_CODE
    action.sort_order = DEAD_ACTION_SORT_ORDER
    action.is_active = True
    action.save()
    _backfill_rules(apps, [DEAD_PERMISSION_CODE])


class Migration(migrations.Migration):

    dependencies = [('ops', '0085_machine_ssh_permission')]

    operations = [
        migrations.RunPython(rename_actions_to_chinese, restore_action_codes_as_names),
        migrations.RunPython(add_pod_exec_action, remove_pod_exec_action),
        migrations.RunPython(drop_dead_namespace_authz_action, restore_dead_namespace_authz_action),
    ]
