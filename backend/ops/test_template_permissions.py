from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ops.models import (
    Organization,
    PermissionMenuAction,
    PermissionMenuNode,
    PermissionPolicy,
    PermissionRule,
    UserProfile,
)
from ops.permission_catalog import catalog_payload, page_code_for_path, required_codes_for_page
from ops.permission_service import clear_permission_cache, has_permission


EXPECTED_PAGE_ACTIONS = {
    'page.admin_users.view': {'create', 'update', 'delete', 'disable', 'reset_otp', 'import'},
    'page.admin_departments.view': {'create', 'update', 'delete'},
    'page.admin_user_report.view': {'export'},
    'page.permission_policies.view': {'create', 'update', 'delete'},
    'page.settings.view': {'create', 'update', 'delete', 'reveal', 'test_email', 'test_sms'},
    'page.menu_order.view': {'update'},
    'page.system_status.view': set(),
    'page.logs.users.view': {'export'},
    'page.logs.system.view': {'export'},
}


class TemplatePermissionCatalogTests(TestCase):
    """验证模板仅保留总览、K8s 管理、机器管理、用户管理、平台和日志相关权限目录。"""

    def test_catalog_contains_only_template_groups(self):
        """权限目录的一级菜单，顺序即侧边栏顺序。

        「总览」排在最前是 0079 刻意为之：它是跨集群的汇总页，权限与 K8s管理 解绑后
        必须自己成为一个一级分组（``catalog_tree`` 会丢弃没有父节点的页面）。
        顺序断言比集合断言更值钱 —— 它顺带钉住了「总览在最顶上」这个需求本身。
        """
        payload = catalog_payload()['items']
        self.assertEqual(
            [group['code'] for group in payload],
            ['menu.overview', 'menu.k8s', 'menu.machine', 'menu.user_management', 'menu.platform', 'menu.logs'],
        )
        self.assertFalse(PermissionMenuNode.objects.filter(code__startswith='page.bastion.').exists())

    def test_page_actions_match_template_contract(self):
        """每个模板页面的数据库操作项必须与前端真实按钮契约一致。"""
        actual = {
            node.code: set(node.actions.values_list('code', flat=True))
            for node in PermissionMenuNode.objects.filter(code__in=EXPECTED_PAGE_ACTIONS).prefetch_related('actions')
        }
        self.assertEqual(actual, EXPECTED_PAGE_ACTIONS)

    def test_required_codes_include_database_parent(self):
        """页面权限链必须包含数据库配置的一级菜单权限。"""
        self.assertEqual(
            required_codes_for_page('page.admin_users.view'),
            ['menu.user_management', 'page.admin_users.view'],
        )
        self.assertEqual(
            required_codes_for_page('page.settings.view'),
            ['menu.platform', 'page.settings.view'],
        )
        self.assertEqual(
            required_codes_for_page('page.logs.system.view'),
            ['menu.logs', 'page.logs.system.view'],
        )


# 0077 建立的分组父节点：(code, 显示名)。
K8S_GROUP_NODES = {
    'page.k8s.group.cluster.view': '集群资源',
    'page.k8s.group.workload.view': '工作负载',
    'page.k8s.group.network.view': '网络与存储',
    'page.k8s.group.delivery.view': '应用交付',
    'page.k8s.group.observe.view': '可观测性',
}

# 分组下挂的页面，顺序即展示顺序。
K8S_GROUPED_PAGES = {
    'page.k8s.group.cluster.view': [
        'page.k8s.nodes.view',
        'page.k8s.namespaces.view',
        'page.k8s.namespaces.authz.view',
        'page.k8s.config.view',
    ],
    'page.k8s.group.workload.view': [
        'page.k8s.workloads.view',
        'page.k8s.pods.view',
        'page.k8s.images.view',
    ],
    'page.k8s.group.network.view': [
        'page.k8s.network.view',
        'page.k8s.configs.view',
        'page.k8s.storage.view',
    ],
    'page.k8s.group.delivery.view': [
        'page.k8s.helm.view',
    ],
    'page.k8s.group.observe.view': [
        'page.k8s.events.view',
        'page.k8s.statistics.view',
        'page.k8s.audit.view',
    ],
}

# 已整页下线的 K8s 页面，以及 0067 当初给它们播种的动作码。
#
# 「集群总览」是 0081 删的、一个动作都没配；另外四页是 0082 / 0083 删的，各自带着
# 一批动作 —— `PermissionMenuAction` 随节点级联删除，但 `PermissionRule` 只是普通
# CharField，动作码（`page.k8s.logs.test` 这类）必须显式清，否则会留下参与鉴权的野码。
REMOVED_K8S_PAGES = [
    ('page.k8s.overview.view', []),
    ('page.k8s.logs.view', ['create', 'update', 'exec', 'delete', 'test', 'export']),
    ('page.k8s.monitoring.view', ['create', 'update', 'delete', 'test', 'promql']),
    ('page.k8s.platform.view', ['create', 'update', 'delete', 'test', 'exec', 'export']),
    ('page.k8s.yaml.view', ['validate', 'apply']),
]


class K8sMenuGroupingTests(TestCase):
    """验证 K8s 菜单的分组父节点只是容器，不进路由也不进鉴权。

    0077 把 20 个平铺页面挂到 5 个分组下。父节点一旦写错 code 前缀或配上 path，
    会连带改变全平台用户的权限链和接口路径解析 —— 那比菜单显示错位难查得多，
    所以这几条约定必须由测试钉住，而不是只写在迁移的注释里。
    """

    def test_top_level_keeps_the_high_frequency_pages_first(self):
        """一级菜单下是 2 个散项加 5 个分组：「集群接入」与「任务中心」在最前。

        取 `sidebar=True` 的节点，顺序即侧边栏顺序。0080 起「任务中心」
        （`page.k8s.deploy.view`）也进侧边栏、排在「集群接入」之后 —— 它此前由集群接入
        工具栏的按钮打开，那个入口已经去掉了，菜单是唯一入口。它管的正是集群接入那批
        部署任务，所以紧跟其后而不是排在一级菜单末尾。

        「集群总览」（`page.k8s.overview.view`）已整页下线，见 `0081_remove_k8s_overview_page`；
        「总览」（`page.k8s.dashboard.view`）不在这个列表里：0079 把它提到了一级菜单
        之外，见 `OverviewTopLevelMenuTests`。两个名字很像，但它们不是同一页。
        """
        top = list(
            PermissionMenuNode.objects.filter(parent__code='menu.k8s', sidebar=True)
            .order_by('sort_order', 'id')
            .values_list('code', flat=True)
        )
        self.assertEqual(top, [
            'page.k8s.clusters.view',
            'page.k8s.deploy.view',
            *K8S_GROUP_NODES,
        ])

    def test_removed_pages_leave_no_trace(self):
        """已下线的页面在菜单节点与权限规则里都不该再留下任何码，动作码也一样。

        漏掉任何一边都是静默故障：节点留着是菜单里一个点开空白的入口；
        规则留着是权限策略页看不见、却仍然参与鉴权的野码。

        动作码是最容易漏的那一半：`PermissionMenuAction` 是级联删除，删页面就把动作
        记录带走了，于是「动作没了」看起来像是「规则也没了」—— 而规则表里那些
        `page.k8s.logs.test` 还一条不少地留着。所以这里连同动作码一起断言。
        """
        for page_code, actions in REMOVED_K8S_PAGES:
            prefix = page_code.removesuffix('.view')
            self.assertFalse(PermissionMenuNode.objects.filter(code=page_code).exists(), page_code)
            for code in [page_code] + [f'{prefix}.{action}' for action in actions]:
                self.assertFalse(PermissionRule.objects.filter(permission_code=code).exists(), code)

    def test_group_nodes_are_containers(self):
        """分组父节点挂在 K8s 一级菜单下，且没有 path。"""
        group = PermissionMenuNode.objects.get(code='menu.k8s')
        for code, name in K8S_GROUP_NODES.items():
            node = PermissionMenuNode.objects.get(code=code)
            self.assertEqual(node.parent_id, group.id, f'{name} 应当挂在 K8s 一级菜单下')
            self.assertEqual(node.node_type, 'page', f'{name} 是页面型容器节点')
            self.assertEqual(node.path, '', f'{name} 是容器节点，不能配 path')
            self.assertTrue(node.sidebar, f'{name} 要在侧边栏显示')

    def test_group_nodes_do_not_enter_the_permission_chain(self):
        """子页面所需的权限仍然只有一级菜单加自己，一个码都没变。

        这是本次改动的核心前提：前后端都只保留 `menu.` 开头的父链，
        分组节点因此被过滤掉。若父节点误用 `menu.` 前缀，这里会立刻变红。
        """
        self.assertEqual(
            required_codes_for_page('page.k8s.nodes.view'),
            ['menu.k8s', 'page.k8s.nodes.view'],
        )
        self.assertEqual(
            required_codes_for_page('page.k8s.config.view'),
            ['menu.k8s', 'page.k8s.config.view'],
        )

    def test_pages_are_grouped_under_the_expected_parent(self):
        """页面挂在预期分组下，且按登记顺序排列。"""
        for group_code, page_codes in K8S_GROUPED_PAGES.items():
            actual = list(
                PermissionMenuNode.objects.filter(parent__code=group_code)
                .order_by('sort_order', 'id')
                .values_list('code', flat=True)
            )
            self.assertEqual(actual, page_codes, f'{K8S_GROUP_NODES[group_code]} 的分组内容不符')

    def test_config_page_is_renamed(self):
        """「配置中心」改名「部署基线」，路径保持不变。"""
        node = PermissionMenuNode.objects.get(code='page.k8s.config.view')
        self.assertEqual(node.name, '部署基线')
        self.assertEqual(node.path, '/k8s/config')


class OverviewTopLevelMenuTests(TestCase):
    """验证 0079 把「总览」提成一级分组后，前后端约定的那几个字面量都对得上。

    这条链路最容易静默失效，因为它跨了三层硬编码：
    `catalog_tree` 只收「group 且无父节点」的节点、`App.vue` 的 `isDirectGroup` 认
    `menu.overview` 这个字面量、`directGroupPath` 取组自己的 `path`。任何一处错位都不会
    报错 —— 表现是「侧边栏里总览整个不见了」或「点开跳到不存在的 /overview」。
    前端没有单元测试，所以这里只能把后端这一侧的值钉死。
    """

    def test_group_is_a_top_level_direct_entry(self):
        """总览是一个顶层分组节点，排在所有一级菜单的最前。"""
        group = PermissionMenuNode.objects.get(code='menu.overview')

        self.assertIsNone(group.parent_id, '顶层分组不能挂在别的菜单下')
        self.assertEqual(group.node_type, 'group', 'catalog_tree 只收 group，写成 page 会被丢弃')
        self.assertEqual(group.name, '总览')
        # 这个 path 只给前端 directGroupPath 用；改成空串直达项会跳到不存在的 /overview。
        self.assertEqual(group.path, '/k8s/dashboard')
        self.assertEqual(group.icon, 'ChartPie')
        self.assertTrue(group.sidebar)
        self.assertTrue(group.is_active)
        self.assertEqual(group.sort_order, 1)

        first = (
            PermissionMenuNode.objects.filter(parent__isnull=True, sidebar=True)
            .order_by('sort_order', 'id')
            .first()
        )
        self.assertEqual(first.code, 'menu.overview', '总览必须是一级菜单里的第一个')

    def test_page_hangs_under_the_overview_group(self):
        """总览页挂在新的顶层分组下，字段本身一个都没改。"""
        group = PermissionMenuNode.objects.get(code='menu.overview')
        node = PermissionMenuNode.objects.get(code='page.k8s.dashboard.view')

        self.assertEqual(node.parent_id, group.id)
        self.assertEqual(node.node_type, 'page')
        self.assertEqual(node.name, '总览')
        self.assertEqual(node.path, '/k8s/dashboard')
        self.assertTrue(node.sidebar)

    def test_group_is_visible_in_the_catalog(self):
        """分组必须真的出现在权限目录里，否则侧边栏不会渲染它。"""
        codes = [group['code'] for group in catalog_payload()['items']]
        self.assertIn('menu.overview', codes)


class K8sDashboardPageTests(TestCase):
    """验证「总览」页的权限链在 0079 之后彻底与 K8s管理 解绑。

    总览是跨集群的汇总页，一个 K8s 页面都不碰，所以它不该要求访问者同时持有
    K8s管理 权限。最容易出错的不是页面本身，而是 `code` 前缀写成 `menu.` ——
    那样每个平台用户会凭空多出一个必需权限。这里把前缀和权限链一起钉住。
    """

    def test_page_code_keeps_the_menu_prefix(self):
        """权限链是它自己的菜单组加自己，且 `code` 用的是 `page.` 前缀。

        写成 `menu.overview.dashboard.view` 的话，`parent_codes_for_permission` 会把它
        当成父级菜单收进权限链，全平台用户都会多出一个必须持有的权限码。

        链里**不该**再出现 `menu.k8s`：0079 之后总览与 K8s管理 是两个互不相干的入口，
        留着它等于把一个不碰 K8s 的页面继续绑在 K8s 权限上。
        """
        self.assertTrue(
            PermissionMenuNode.objects.filter(code='page.k8s.dashboard.view').exists(),
            '总览的 code 必须是 page. 前缀',
        )
        self.assertFalse(PermissionMenuNode.objects.filter(code__startswith='menu.overview.').exists())
        self.assertEqual(
            required_codes_for_page('page.k8s.dashboard.view'),
            ['menu.overview', 'page.k8s.dashboard.view'],
        )
        self.assertNotIn('menu.k8s', required_codes_for_page('page.k8s.dashboard.view'))

    def test_api_url_still_resolves_to_the_page_code(self):
        """搬家不能动 path：`/k8s/dashboard` 仍要解析到总览页的权限码。

        接口鉴权与前端路由都按 path 反查页面码，path 一变，总览页的接口就会去找
        一个不存在的页面码。
        """
        self.assertEqual(page_code_for_path('/k8s/dashboard'), 'page.k8s.dashboard.view')

    def test_page_has_no_actions(self):
        """总览是只读页，不登记任何操作码。"""
        node = PermissionMenuNode.objects.get(code='page.k8s.dashboard.view')
        self.assertEqual(list(node.actions.values_list('code', flat=True)), [])

    def test_api_path_resolves_to_the_page_code(self):
        """`/k8s/dashboard` 归到总览页权限，普通用户没有该权限时接口必须拒绝。"""
        organization = Organization.objects.first()
        user = User.objects.create_user(username='dashboard-user', password='StrongPassword123!')
        UserProfile.objects.create(user=user, organization=organization, role='member')
        PermissionPolicy.objects.create(
            name='总览页测试策略',
            subject_type='user',
            user=user,
            status='available',
        )
        client = APIClient()
        client.force_authenticate(user)

        self.assertEqual(client.get('/api/v1/k8s/dashboard/').status_code, 403)


class K8sTaskCenterTests(TestCase):
    """验证 0080 把「部署集群」升级成独立页面「任务中心」后，向导与进度页都没被打断。

    这里改的是 `page.k8s.deploy.view` **同一个节点**，不新增权限码 —— 接口
    `/k8s/deployments/` 早就映射到这个码上，另立页面码会造成「菜单一个码、接口另一个码」
    的局面。代价是原 path `/k8s/clusters/deploy` 必须降级成别名，而别名是部署向导、
    部署进度页、清理进度页三条路由连同它们的前端权限映射的唯一来源：
    `permissions.registerPath` 是别名唯一的登记点，漏一条，对应的页面就静默不可达。
    """

    def test_page_becomes_a_sidebar_entry_after_the_cluster_page(self):
        """任务中心进侧边栏，紧跟在「集群接入」后面。"""
        node = PermissionMenuNode.objects.get(code='page.k8s.deploy.view')

        self.assertEqual(node.name, '任务中心')
        self.assertEqual(node.path, '/k8s/tasks')
        self.assertEqual(node.icon, 'ListOrdered')
        self.assertTrue(node.sidebar, '去掉工具栏按钮后，菜单是唯一入口')
        self.assertTrue(node.is_active)

        ordered = list(
            PermissionMenuNode.objects.filter(parent__code='menu.k8s', sidebar=True)
            .order_by('sort_order', 'id')
            .values_list('code', flat=True)
        )
        self.assertIn('page.k8s.deploy.view', ordered)
        self.assertEqual(ordered[ordered.index('page.k8s.deploy.view') - 1], 'page.k8s.clusters.view')
        # 它抢的是第 4 位，五个分组容器整体顺延到 5-9，没有两个节点共用同一个序号。
        self.assertEqual(len(ordered), len(set(ordered)))
        for group_code in K8S_GROUP_NODES:
            self.assertGreater(
                PermissionMenuNode.objects.get(code=group_code).sort_order, node.sort_order,
            )

    def test_no_new_permission_code_was_introduced(self):
        """任务中心复用部署页的权限码，目录里不该冒出 page.k8s.tasks.*。"""
        self.assertFalse(PermissionMenuNode.objects.filter(code__startswith='page.k8s.tasks').exists())
        self.assertTrue(PermissionMenuNode.objects.filter(code='page.k8s.deploy.view').exists())

    def test_every_route_alias_still_resolves_to_the_deploy_page(self):
        """三条别名路径都要解析到部署页的权限码：向导、部署进度、清理进度。

        少一条的后果分别是：向导页无权限被守卫拦下、部署进度页打不开、清理进度页打不开。
        三种都不会报错，只是点不动。
        """
        node = PermissionMenuNode.objects.get(code='page.k8s.deploy.view')
        aliases = {item['path']: item.get('name') for item in node.aliases}

        self.assertEqual(
            set(aliases),
            {
                '/k8s/clusters/deploy',
                '/k8s/clusters/deployments/:id',
                '/k8s/clusters/destroy/:id',
            },
        )
        # 路由表的 meta.title 取 alias.name，缺了会显示成 undefined。
        for path, name in aliases.items():
            self.assertTrue(name, f'{path} 的别名缺少 name')
            self.assertEqual(
                page_code_for_path(path), 'page.k8s.deploy.view', f'{path} 没能解析回部署页',
            )

    def test_progress_pages_are_not_reachable_through_the_other_prefix(self):
        """进度页的 path 带主键，解析要能吃到具体的一条，而不只是字面量。"""
        self.assertEqual(
            page_code_for_path('/k8s/clusters/deployments/17'), 'page.k8s.deploy.view',
        )
        self.assertEqual(
            page_code_for_path('/k8s/clusters/destroy/42'), 'page.k8s.deploy.view',
        )


class TemplatePermissionApiTests(TestCase):
    """验证模板普通用户只能访问数据库策略明确允许的页面。"""

    def setUp(self):
        """创建普通用户及默认拒绝的用户权限策略。"""
        organization = Organization.objects.first()
        self.user = User.objects.create_user(username='template-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=organization, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='模板用户权限',
            subject_type='user',
            user=self.user,
            status='available',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def grant(self, code):
        """向当前测试用户策略写入单项允许规则。"""
        PermissionRule.objects.update_or_create(
            policy=self.policy,
            permission_code=code,
            defaults={'effect': 'allow'},
        )
        clear_permission_cache()

    def test_user_log_requires_group_and_page_permissions(self):
        """普通用户必须同时具备日志菜单和用户日志页面权限。"""
        denied = self.client.get('/api/v1/user-logs/')
        self.grant('menu.logs')
        still_denied = self.client.get('/api/v1/user-logs/')
        self.grant('page.logs.users.view')
        allowed = self.client.get('/api/v1/user-logs/')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(still_denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)


class MachineSshActionPermissionTests(TestCase):
    """机器列表的「SSH 登录」必须是一个能在权限策略里单独勾选的动作权限。

    直连目标机器的 shell 比「看一眼机器列表」重得多，所以它得能单独授予、单独收回 ——
    但反过来，多了这一个动作码不能把「看列表」卡住：`page.machine.servers.view` 的权限链
    里不许出现它（出现在链里就等于单授 view 也看不见页面）。

    权限目录整棵是数据库驱动的（`permission_catalog` 从 `PermissionMenuNode` /
    `PermissionMenuAction` 读），所以这条约定最终落在迁移写下的那一行动作记录上。
    """

    def test_action_is_registered_under_the_machine_page(self):
        """目录里有这一项，名字与权限码和前端按钮、后端映射对得上。"""
        page = PermissionMenuNode.objects.get(code='page.machine.servers.view')
        action = page.actions.get(code='ssh')

        self.assertEqual(action.name, 'SSH 登录')
        self.assertTrue(action.is_active)
        codes = {item['code'] for item in catalog_payload()['flat']}
        self.assertIn('page.machine.servers.ssh', codes)

    def test_view_permission_does_not_carry_the_ssh_action(self):
        """机器列表的权限链仍然是「菜单 + 页面」两个码，SSH 不在其中。"""
        required = required_codes_for_page('page.machine.servers.view')

        self.assertEqual(required, ['menu.machine', 'page.machine.servers.view'])
        self.assertNotIn('page.machine.servers.ssh', required)

    def test_permission_admin_owns_it_and_a_plain_policy_does_not(self):
        """唯一 admin 自动拥有；普通用户的策略里它是默认拒绝的，勾上才生效。"""
        admin = User.objects.create_superuser(
            username='admin', email='admin@example.com', password='StrongPassword123!',
        )
        organization = Organization.objects.first()
        user = User.objects.create_user(username='ssh-policy-user', password='StrongPassword123!')
        UserProfile.objects.create(user=user, organization=organization, role='member')
        policy = PermissionPolicy.objects.create(
            name='SSH 授权测试策略', subject_type='user', user=user, status='available',
        )
        PermissionRule.objects.update_or_create(
            policy=policy, permission_code='menu.machine', defaults={'effect': 'allow'},
        )
        PermissionRule.objects.update_or_create(
            policy=policy, permission_code='page.machine.servers.view', defaults={'effect': 'allow'},
        )
        clear_permission_cache()

        self.assertTrue(has_permission(admin, 'page.machine.servers.ssh'))
        self.assertFalse(has_permission(user, 'page.machine.servers.ssh'))

        PermissionRule.objects.update_or_create(
            policy=policy, permission_code='page.machine.servers.ssh', defaults={'effect': 'allow'},
        )
        clear_permission_cache()

        self.assertTrue(has_permission(user, 'page.machine.servers.ssh'))


# 「K8s管理」下每个页面在权限策略里能勾到的动作，与界面按钮、后端鉴权逐一对齐。
#
# 用集合断言而不是包含断言，两个方向都是故障：
# 少一项 = 页面上有按钮，权限策略里却勾不出来（0086 之前 ``page.k8s.pods.exec``
# 就是这样，「进入容器」对非管理员永久不可用）；多一项 = 权限策略里多一个勾了也不管用的
# 开关，授权的人会以为它管着什么（0067 给「命名空间」页种的 ``authz``）。
EXPECTED_K8S_PAGE_ACTIONS = {
    'page.k8s.clusters.view': {
        'create', 'update', 'delete', 'disable', 'test', 'sync', 'reveal',
        'add_master', 'add_worker', 'upgrade', 'backup', 'restore', 'destroy_cluster',
    },
    'page.k8s.deploy.view': {'create', 'dry_run', 'retry', 'cancel', 'delete'},
    'page.k8s.config.view': {'create', 'update', 'delete'},
    'page.k8s.nodes.view': {'update', 'cordon', 'uncordon', 'drain', 'delete'},
    'page.k8s.namespaces.view': {'create', 'update', 'delete'},
    'page.k8s.namespaces.authz.view': {'create', 'update', 'delete'},
    'page.k8s.workloads.view': {'update', 'scale', 'restart', 'trigger', 'suspend', 'batch', 'delete'},
    'page.k8s.pods.view': {'update', 'delete', 'batch', 'exec'},
    'page.k8s.images.view': {'export'},
    'page.k8s.network.view': {'create', 'update', 'delete'},
    'page.k8s.configs.view': {'create', 'update', 'reveal', 'delete'},
    'page.k8s.storage.view': {'create', 'update', 'delete'},
    'page.k8s.helm.view': {'update', 'delete'},
    'page.k8s.events.view': {'update', 'delete'},
    'page.k8s.statistics.view': {'export'},
    'page.k8s.audit.view': {'export'},
}


class K8sPermissionCompletenessTests(TestCase):
    """验证 0086 之后 K8s 权限项的「齐」与「中文」两条约定。

    权限目录的显示名就是数据库里的 ``PermissionMenuAction.name``（``permission_catalog``
    直接把它塞进 ``actions[].name``，前端没有兜底映射表），所以「哪个动作能勾、叫什么名字」
    最终都落在迁移写下的那些行上 —— 这里钉的就是那些行。
    """

    def test_page_actions_match_the_ui_contract(self):
        """每个 K8s 页面能勾到的动作，与界面按钮一一对应，不多不少。"""
        actual = {
            node.code: set(node.actions.values_list('code', flat=True))
            for node in PermissionMenuNode.objects.filter(
                code__in=EXPECTED_K8S_PAGE_ACTIONS,
            ).prefetch_related('actions')
        }
        self.assertEqual(actual, EXPECTED_K8S_PAGE_ACTIONS)

    def test_group_nodes_have_no_actions(self):
        """5 个分组父节点是纯容器，一个动作都不该挂。"""
        for group_code in K8S_GROUP_NODES:
            node = PermissionMenuNode.objects.get(code=group_code)
            self.assertFalse(node.actions.exists(), f'{K8S_GROUP_NODES[group_code]} 不该有动作项')

    def test_every_action_has_a_chinese_label(self):
        """目录里不许再有「拿动作码当显示名」的权限项。

        0067 / 0069 当初写的是 ``item.name = action``，于是权限策略里显示 create /
        update / test 这类英文。这里不逐个断言中文用词（那会跟着界面文案改动而反复变红），
        只钉住最要紧的一条：显示名不能再等于动作码本身。
        """
        placeholders = [
            f'{action.page.code}.{action.code}'
            for action in PermissionMenuAction.objects.select_related('page').all()
            if action.name == action.code
        ]
        self.assertEqual(placeholders, [], '这些权限项的显示名还是动作码本身，需要改成中文')

    def test_pod_exec_action_is_registered(self):
        """「进入容器」是可授权的，且不混进 Pod 管理页的权限链。

        ``page.k8s.pods.exec`` 是前端按钮显隐、后端 WebSocket 握手都在用的码；
        目录里没有它，普通用户就永远勾不出这个权限。反过来它也不能进
        ``required_codes_for_page``：进了链就等于把「看 Pod 列表」也一起卡住。
        """
        page = PermissionMenuNode.objects.get(code='page.k8s.pods.view')
        action = page.actions.get(code='exec')

        self.assertEqual(action.name, '进入容器')
        self.assertTrue(action.is_active)
        codes = {item['code'] for item in catalog_payload()['flat']}
        self.assertIn('page.k8s.pods.exec', codes)
        self.assertEqual(
            required_codes_for_page('page.k8s.pods.view'),
            ['menu.k8s', 'page.k8s.pods.view'],
        )

    def test_dead_namespace_authz_action_is_gone(self):
        """「命名空间」页上那个从未生效的 ``authz`` 动作，节点与规则都不该留。

        它与命名空间授权页的权限链 ``page.k8s.namespaces.authz.view`` 只差一个后缀，
        留着最容易被误当成「授权开关」—— 全仓没有任何一处鉴权读它。
        """
        self.assertFalse(
            PermissionMenuAction.objects.filter(
                page__code='page.k8s.namespaces.view', code='authz',
            ).exists(),
        )
        self.assertFalse(
            PermissionRule.objects.filter(permission_code='page.k8s.namespaces.authz').exists(),
        )
