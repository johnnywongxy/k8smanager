from rest_framework.permissions import BasePermission, SAFE_METHODS

from .permission_catalog import action_code, page_code_for_path, required_codes_for_page
from .permission_service import has_permission, is_permission_admin


API_PAGE_ALIASES = [
    ('/users', 'page.admin_users.view'),
    ('/roles', 'page.admin_users.view'),
    ('/orgs', 'page.admin_departments.view'),
    ('/departments', 'page.admin_departments.view'),
    ('/permission-policies', 'page.permission_policies.view'),
    ('/permission-catalog', 'page.permission_policies.view'),
    ('/user-logs', 'page.logs.users.view'),
    ('/system-logs', 'page.logs.system.view'),
    ('/system-settings', 'page.settings.view'),
    ('/system-tools', 'page.system_tools.view'),
    ('/settings', 'page.settings.view'),
    ('/machines/servers', 'page.machine.servers.view'),
    ('/k8s/dashboard', 'page.k8s.dashboard.view'),
    ('/k8s/config', 'page.k8s.config.view'),
    ('/k8s/deployments', 'page.k8s.deploy.view'),
    ('/k8s/deploy-options', 'page.k8s.deploy.view'),
    ('/k8s/clusters', 'page.k8s.clusters.view'),
    ('/k8s/cluster-health-checks', 'page.k8s.clusters.view'),
    ('/k8s/namespace-policies', 'page.k8s.namespaces.authz.view'),
    ('/k8s/audits/operations', 'page.k8s.audit.view'),
    ('/k8s/audits/secrets', 'page.k8s.audit.view'),
    ('/k8s/actions/scale', 'page.k8s.workloads.view'),
    ('/k8s/actions/restart', 'page.k8s.workloads.view'),
    ('/k8s/actions/trigger', 'page.k8s.workloads.view'),
    ('/k8s/actions/suspend', 'page.k8s.workloads.view'),
    ('/k8s/actions/batch', 'page.k8s.workloads.view'),
    ('/k8s/resource-snapshots', 'page.k8s.statistics.view'),
    ('/k8s/resources/namespace-options', 'page.k8s.namespaces.view'),
    ('/k8s/resources/namespaces', 'page.k8s.namespaces.view'),
    ('/k8s/resources/nodes', 'page.k8s.nodes.view'),
    ('/k8s/resources/images', 'page.k8s.images.view'),
    ('/k8s/resources/workloads', 'page.k8s.workloads.view'),
    ('/k8s/resources/pods', 'page.k8s.pods.view'),
    ('/k8s/resources/network', 'page.k8s.network.view'),
    ('/k8s/resources/configs', 'page.k8s.configs.view'),
    ('/k8s/resources/storage', 'page.k8s.storage.view'),
    ('/k8s/resources/events', 'page.k8s.events.view'),
    # Pod 日志查询原先借「日志查询」页的权限码：那一页整页下线后（见 ops 迁移 0082），
    # 它只剩一个调用方 —— 资源页表格里 Pod 行上的「查看日志」。页面码改挂「Pod管理」，
    # 与同一个 Pod 上的「进入容器」（'/k8s/pods/exec'）保持同一归属：能看 Pod 就能看它的日志。
    ('/k8s/logs/pods', 'page.k8s.pods.view'),
    ('/k8s/statistics', 'page.k8s.statistics.view'),
    # 主机资源（SSH 读各节点 /proc 与 df）与资源统计同一个页面码；前缀更长也不会与上面
    # 那条冲突（最长前缀匹配）。机器列表页那份是另一个接口（``/machines/servers/usage``），
    # 挂在机器管理自己的页面码下。
    ('/k8s/host-usage', 'page.k8s.statistics.view'),
    ('/k8s/platform/helm/releases', 'page.k8s.helm.view'),
    ('/k8s/pods/exec', 'page.k8s.pods.view'),
    ('/k8s/actions/cordon', 'page.k8s.nodes.view'),
    ('/k8s/actions/uncordon', 'page.k8s.nodes.view'),
    ('/k8s/actions/drain', 'page.k8s.nodes.view'),
    ('/k8s/actions/reveal', 'page.k8s.configs.view'),
    ('/k8s/actions', 'page.k8s.workloads.view'),
]


K8S_KIND_PAGE_CODES = {
    'Namespace': 'page.k8s.namespaces.view',
    'Node': 'page.k8s.nodes.view',
    'Image': 'page.k8s.images.view',
    'Pod': 'page.k8s.pods.view',
    'Deployment': 'page.k8s.workloads.view',
    'StatefulSet': 'page.k8s.workloads.view',
    'DaemonSet': 'page.k8s.workloads.view',
    'Job': 'page.k8s.workloads.view',
    'CronJob': 'page.k8s.workloads.view',
    'HorizontalPodAutoscaler': 'page.k8s.workloads.view',
    'PodDisruptionBudget': 'page.k8s.workloads.view',
    'Service': 'page.k8s.network.view',
    'Ingress': 'page.k8s.network.view',
    'ConfigMap': 'page.k8s.configs.view',
    'Secret': 'page.k8s.configs.view',
    'PersistentVolumeClaim': 'page.k8s.storage.view',
    'PersistentVolume': 'page.k8s.storage.view',
    'StorageClass': 'page.k8s.storage.view',
}


def user_organization(user):
    """返回当前用户档案中的所属组织。"""
    profile = getattr(user, 'profile', None)
    return getattr(profile, 'organization', None) if profile else None


class RoleBasedPermission(BasePermission):
    """按数据库权限策略校验平台页面访问和具体业务操作。"""

    message = '没有该功能权限，请联系管理员在用户管理的权限策略中授权。'

    def _normalized_path(self, request):
        """移除 API 前缀和末尾斜线，得到稳定的权限匹配路径。"""
        path = getattr(request, 'path', '')
        if path.startswith('/api/v1'):
            path = path.removeprefix('/api/v1')
        return path.rstrip('/') or '/'

    def _page_code(self, request, view):
        """根据显式声明、接口别名和数据库路由定位页面权限。"""
        explicit = getattr(view, 'permission_code', '')
        if explicit:
            return explicit
        path = self._normalized_path(request)
        if path == '/k8s/actions/batch' and hasattr(request, 'data'):
            try:
                items = request.data.get('items') or []
                kind = (items[0] or {}).get('kind', '') if items else ''
                return K8S_KIND_PAGE_CODES.get(kind, 'page.k8s.workloads.view')
            except Exception:
                return 'page.k8s.workloads.view'
        for prefix, code in sorted(API_PAGE_ALIASES, key=lambda item: len(item[0]), reverse=True):
            if path == prefix or path.startswith(f'{prefix}/'):
                return code
        return page_code_for_path(path)

    def _setting_write_action(self, request, view):
        """区分按键写入平台设置时的新增和编辑操作。"""
        category = (getattr(view, 'kwargs', {}) or {}).get('category')
        key = (getattr(view, 'kwargs', {}) or {}).get('key')
        if not category or not key:
            return 'update'
        from .models import SystemSetting
        return 'update' if SystemSetting.objects.filter(key=f'{category}.{key}').exists() else 'create'

    def _action_name(self, request, view):
        """把平台 DRF 动作和自定义接口转换为数据库操作权限代码。"""
        action = (getattr(view, 'action', '') or '').replace('-', '_')
        path = self._normalized_path(request)

        if request.method not in SAFE_METHODS and path.startswith('/k8s/actions/'):
            if path == '/k8s/actions/batch' and hasattr(request, 'data'):
                try:
                    return request.data.get('batch_action', '').strip() or 'batch'
                except Exception:
                    return 'batch'
            return path.rsplit('/', 1)[-1].replace('-', '_')
        if request.method not in SAFE_METHODS and path.startswith('/k8s/platform/helm/releases/'):
            helm_action = path.rsplit('/', 1)[-1].replace('-', '_')
            return {'uninstall': 'delete'}.get(helm_action, helm_action)
        if request.method not in SAFE_METHODS and path == '/k8s/pods/exec':
            return 'exec'

        if path.startswith('/system-settings/') and path.endswith('/reveal'):
            return 'reveal'
        if path == '/system-settings/platform/logo-upload':
            return 'update'
        if path == '/system-settings/notification/test/email':
            return 'test_email'
        if path == '/system-settings/notification/test/sms':
            return 'test_sms'
        if path == '/system-settings/llm/test':
            return 'update'
        if path == '/system-tools/ping':
            return 'ping'
        if path == '/system-tools/telnet':
            return 'telnet'
        if path == '/system-tools/curl':
            return 'curl'
        if path == '/system-tools/traceroute':
            return 'traceroute'
        if path == '/system-tools/mtr':
            return 'mtr'
        if path.startswith('/users/import/'):
            return 'import'
        if path.endswith('/reveal-kubeconfig'):
            return 'reveal'
        if path == '/k8s/resource-snapshots/refresh':
            return 'view'
        if path in {'/machines/servers/summary', '/machines/servers/usage', '/k8s/clusters/summary'}:
            # 这几个接口都是纯只读的统计与采集，属于「查看」，不该再要一个并不存在的操作权限。
            # 水位采集虽然要 SSH 出去读 /proc 与 df，但对使用它的人来说仍然是「看一眼机器
            # 状态」，与同一页的列表、概要同一个权限级别；不映射的话 DRF 会把动作名 ``usage``
            # 当成操作码，去找从未登记过的 ``page.machine.servers.usage``，非管理员一打开
            # 机器列表就 403。
            return 'view'
        # 部署进度页的增量接口和向导的环境探测都是只读查看。它们挂在 /k8s/deployments
        # 前缀下，页面码已经由别名解析成 page.k8s.deploy.view，但 DRF 的动作名会被原样
        # 当成操作码，于是去找从未登记过的 page.k8s.deploy.events——非管理员打开进度页
        # 后每次轮询都 403，页面永远停在加载中（规划 12）。这三个动作都只绑定 GET，
        # 按「查看」放行不会放开任何写入路径；``dry_run``/``cancel``/``retry`` 不在此列。
        if path.startswith('/k8s/deployments') and action in {'events', 'logs', 'environment'}:
            return 'view'
        if path in {'/machines/servers/assign', '/machines/servers/unassign'}:
            # 集群部署向导添加/移除节点本质是修改机器归属，复用「编辑」权限即可，无需另立权限码。
            return 'update'
        if path.startswith('/system-settings/') and request.method == 'PUT':
            return self._setting_write_action(request, view)
        if path.startswith('/system-settings/') and request.method == 'DELETE':
            return 'delete'
        if action == 'clone':
            return 'create'
        if action in {'members', 'remove_member'} and request.method not in SAFE_METHODS:
            return 'update'
        if action in {'disable', 'enable'}:
            return 'disable'
        # 集群运维：备份列表/能力探测是只读查看，同一个 url_path 上的 POST 才是「执行备份」。
        # 一个 @action 承载两种语义，因此必须按 HTTP 方法分流，否则查看列表会要求备份权限。
        if action in {'backups', 'operations'}:
            return 'view' if request.method in SAFE_METHODS else 'backup'
        # 删除某一份备份本质还是删除资源，复用集群的删除权限，不另立权限码。
        if action == 'delete_backup':
            return 'delete'
        # 任务中心的批量删除同理：删的还是同一批任务，复用单条删除的权限码。
        # 不映射的话 `_standard_action` 会把 DRF 的方法名 `bulk_delete` 原样当成操作码，
        # 去找从未登记过的 page.k8s.deploy.bulk_delete —— 非管理员点批量删除会整体 403。
        if action == 'bulk_delete':
            return 'delete'
        # 机器列表的 SSH 终端没有 REST 接口：它是一条 WebSocket（`k8s_machine.consumers`），
        # 权限码 `page.machine.servers.ssh` 由消费者自己查，不经过这里。
        return self._standard_action(request, action)

    def _standard_action(self, request, action):
        """将标准 REST 方法和动作转换为查看、新增、编辑或删除。"""
        if request.method in SAFE_METHODS and action in {'', 'list', 'retrieve'}:
            return 'view'
        if action == 'create':
            return 'create'
        if action in {'update', 'partial_update'}:
            return 'update'
        if action == 'destroy':
            return 'delete'
        if action:
            return action
        return {
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'update',
            'DELETE': 'delete',
        }.get(request.method, 'view')

    def has_permission(self, request, view):
        """要求用户同时具备菜单组、页面和具体非查看操作权限。"""
        if not request.user or not request.user.is_authenticated:
            return False
        if is_permission_admin(request.user):
            return True

        page_code = self._page_code(request, view)
        if not page_code:
            return False
        for code in required_codes_for_page(page_code):
            if not has_permission(request.user, code):
                return False

        action_name = self._action_name(request, view)
        if action_name == 'view':
            return True
        return has_permission(request.user, action_code(page_code, action_name))


class AdminOnlyPermission(BasePermission):
    """超级管理员直接放行，普通用户仍按数据库功能策略校验。"""

    message = '没有该管理权限，请联系管理员授权。'

    def has_permission(self, request, view):
        """校验登录状态后复用统一的数据库权限策略。"""
        if not request.user or not request.user.is_authenticated:
            return False
        if is_permission_admin(request.user):
            return True
        return RoleBasedPermission().has_permission(request, view)
