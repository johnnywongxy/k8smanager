"""清理进度解析的用例：组件清单怎么生成、ansible 输出怎么折算成进度。

清理只跑一条 ``ezctl destroy``，没有可以分步记账的阶段；组件进度全靠解析 ansible 的
``TASK [...]`` banner（见 ``services/destroy_plan.py``）。这里守的就是那条解析链：

- **清单**必须按集群真实的 CNI 与数据目录生成 —— 路径写错，操作员照着页面核对现场就会核错。
- **marker** 必须与 kubeasz 的任务名逐字对得上，且不能彼此误命中：``remove files and dirs``
  是删 kubelet 目录的，``remove files and dirs2`` 才是 CNI 残留，用子串会张冠李戴。
- **兜底**必须在：模拟执行没有任何 ansible 输出，日志行数也有上限，少了兜底清理成功而页面
  永远停在「待清理」。

真机上那一段无法在这里复现，所以用造出来的 ``TASK [...]`` 输出喂监听器 —— 输入是逐字抄的
banner 形状，不是想象出来的。
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from k8s_deployment.services import destroy_plan, register

# 一份勾了自定义数据目录的 config.yml：compiler 会把三个数据目录变量改写掉，
# 而 destroy 任务上的 data_dir 字段不可信（cluster_ops 不继承它），路径只能从这里读回来。
CUSTOM_DATA_DIR_CONFIG = '\n'.join([
    'ETCD_DATA_DIR: "/var/lib/etcd"',
    'ETCD_WAL_DIR: "/data/etcd-wal"',
    'CONTAINERD_CONFIG_DIR: "/etc/containerd"',
    'CONTAINERD_ROOT_DIR: "/data/containerd"',
    'CONTAINERD_STATE_DIR: "/run/containerd"',
    'KUBELET_ROOT_DIR: "/data/kubelet"',
])

# 默认配置：没勾自定义数据目录时就是 kubeasz 的默认值。
DEFAULT_CONFIG = '\n'.join([
    'ETCD_DATA_DIR: "/var/lib/etcd"',
    'CONTAINERD_ROOT_DIR: "/var/lib/containerd"',
    'KUBELET_ROOT_DIR: "/var/lib/kubelet"',
])


def make_deployment(nodes=None, cni_plugin='calico', lb_mode='none', lb_vip=''):
    """构造一个只带清理所需字段的部署替身。

    参数：`nodes` 表示 ``(角色列表,)`` 序列；`cni_plugin` 表示网络插件；
        `lb_mode` 表示外部访问入口模式；`lb_vip` 表示入口地址。
    返回：返回属性替身。
    副作用：只在本机对象上设置字段。
    """
    node_objects = [SimpleNamespace(roles=list(roles)) for roles in (nodes or [])]
    return SimpleNamespace(
        cni_plugin=cni_plugin,
        lb_mode=lb_mode,
        lb_vip=lb_vip,
        nodes=SimpleNamespace(all=lambda: node_objects),
    )


def config_snapshot(config_text):
    """造一个 ``register.latest_snapshot`` 的返回值。

    参数：`config_text` 表示渲染后的 config.yml 全文。
    返回：返回快照字典。
    副作用：只做纯计算。
    """
    return {'config': config_text} if config_text is not None else {}


def plan_for(deployment, config_text=DEFAULT_CONFIG):
    """在固定的配置快照下生成组件清单。

    参数：`deployment` 表示部署替身；`config_text` 表示 config.yml 全文（None 表示没有快照）。
    返回：返回组件列表。
    副作用：替换 ``register.latest_snapshot``。
    """
    with mock.patch.object(register, 'latest_snapshot', return_value=config_snapshot(config_text)):
        return destroy_plan.components_for(SimpleNamespace(id=1), deployment)


def component(plan, key):
    """从清单里按标识取一项。

    参数：`plan` 表示组件清单；`key` 表示组件标识。
    返回：返回该组件字典。
    副作用：只做纯计算。
    """
    return next(item for item in plan if item['key'] == key)


class ConfigValueTest(SimpleTestCase):
    """``config_value``：从渲染后的 config.yml 里取一个变量的值。"""

    def test_reads_a_quoted_value(self):
        """平台自己渲染出来的是 ``KEY: "value"`` 这种固定形状。"""
        self.assertEqual(
            destroy_plan.config_value(CUSTOM_DATA_DIR_CONFIG, 'ETCD_WAL_DIR'), '/data/etcd-wal',
        )

    def test_missing_key_falls_back_to_the_default(self):
        """取不到就用默认值，而不是给出一串空字符串当路径。"""
        self.assertEqual(
            destroy_plan.config_value(DEFAULT_CONFIG, 'ETCD_WAL_DIR', '/fallback'), '/fallback',
        )

    def test_empty_value_falls_back_to_the_default(self):
        """值为空的含义是「没设」，与缺失一样处理。"""
        self.assertEqual(
            destroy_plan.config_value('ETCD_WAL_DIR: ""', 'ETCD_WAL_DIR', '/fallback'), '/fallback',
        )

    def test_inline_comment_is_stripped(self):
        """值后面跟着注释时只取值本身。"""
        self.assertEqual(
            destroy_plan.config_value('ETCD_DATA_DIR: /var/lib/etcd  # 默认位置', 'ETCD_DATA_DIR'),
            '/var/lib/etcd',
        )

    def test_key_is_matched_whole(self):
        """``ETCD_WAL_DIR`` 不能命中 ``ETCD_WAL_DIR_SUFFIX`` 之类更长的变量名。"""
        text = 'ETCD_WAL_DIR_SUFFIX: "/nope"\nETCD_WAL_DIR: "/yes"'
        self.assertEqual(destroy_plan.config_value(text, 'ETCD_WAL_DIR'), '/yes')


class ResolvedPathsTest(SimpleTestCase):
    """``resolved_paths``：集群实际生效的那组目录。"""

    def test_custom_data_dir_is_read_back_from_the_snapshot(self):
        """勾了自定义数据目录时，三个目录都要从配置快照里读回来。"""
        with mock.patch.object(
            register, 'latest_snapshot', return_value=config_snapshot(CUSTOM_DATA_DIR_CONFIG),
        ):
            paths = destroy_plan.resolved_paths(SimpleNamespace(id=1))
        self.assertEqual(paths['CONTAINERD_ROOT_DIR'], '/data/containerd')
        self.assertEqual(paths['KUBELET_ROOT_DIR'], '/data/kubelet')
        self.assertEqual(paths['ETCD_DATA_DIR'], '/var/lib/etcd')
        self.assertEqual(paths['ETCD_WAL_DIR'], '/data/etcd-wal')

    def test_without_a_snapshot_it_falls_back_to_the_defaults(self):
        """集群还没有配置版本时（``latest_snapshot`` 返回空字典）落回 kubeasz 默认值。"""
        with mock.patch.object(register, 'latest_snapshot', return_value={}):
            paths = destroy_plan.resolved_paths(SimpleNamespace(id=1))
        self.assertEqual(paths, destroy_plan.DEFAULT_PATHS)


class ComponentsForTest(SimpleTestCase):
    """``components_for``：清单的内容与顺序。"""

    def test_plan_follows_the_cleanup_order(self):
        """顺序即 kubeasz ``roles/clean`` 的执行顺序 —— 进度靠它反推，乱了就没意义。"""
        plan = plan_for(make_deployment(nodes=[('etcd', 'master', 'node')]))
        self.assertEqual(
            [item['key'] for item in plan],
            ['etcd', 'control-plane', 'node-agents', 'runtime', 'cni', 'node-env', 'hosts-entry'],
        )

    def test_every_row_carries_what_the_operator_needs(self):
        """每一项都要有中文说明与作用范围，页面直接展示，缺一项就是一片空白。"""
        plan = plan_for(make_deployment(nodes=[('etcd', 'master', 'node')]))
        for item in plan:
            self.assertTrue(item['label'], item['key'])
            self.assertTrue(item['detail'], item['key'])
            self.assertTrue(item['scope'], item['key'])

    def test_etcd_paths_include_the_wal_dir_and_backups(self):
        """etcd 这一项要把 WAL 目录与 /backup/k8s 一起列出来。"""
        plan = plan_for(make_deployment(nodes=[('etcd', 'master', 'node')]), CUSTOM_DATA_DIR_CONFIG)
        self.assertEqual(
            component(plan, 'etcd')['paths'],
            ['/var/lib/etcd', '/data/etcd-wal', '/backup/k8s'],
        )

    def test_wal_dir_is_not_listed_twice_when_it_equals_the_data_dir(self):
        """WAL 与数据目录是同一个（或没设）时只列一次，重复的路径看着像两处。"""
        config = 'ETCD_DATA_DIR: "/var/lib/etcd"\nETCD_WAL_DIR: "/var/lib/etcd"'
        plan = plan_for(make_deployment(nodes=[('etcd',)]), config)
        self.assertEqual(component(plan, 'etcd')['paths'], ['/var/lib/etcd', '/backup/k8s'])

    def test_runtime_paths_come_from_the_snapshot(self):
        """containerd 的数据目录同样受自定义数据目录影响。"""
        plan = plan_for(make_deployment(nodes=[('master', 'node')]), CUSTOM_DATA_DIR_CONFIG)
        self.assertEqual(
            component(plan, 'runtime')['paths'],
            ['/etc/containerd', '/etc/crictl.yaml', '/opt/containerd/', '/data/containerd',
             '/run/containerd'],
        )

    def test_node_agents_warns_when_kubelet_data_is_outside_the_default(self):
        """上游把 kubelet 数据目录写死在 /var/lib/kubelet，自定义目录不在它的删除清单里。

        不说清楚，操作员会以为清干净了。
        """
        plan = plan_for(make_deployment(nodes=[('master', 'node')]), CUSTOM_DATA_DIR_CONFIG)
        self.assertIn('/data/kubelet/', component(plan, 'node-agents')['note'])
        self.assertIn('不会删它', component(plan, 'node-agents')['note'])

    def test_node_agents_has_no_note_on_the_default_layout(self):
        """走默认布局时不该冒出一句用不上的提醒。"""
        plan = plan_for(make_deployment(nodes=[('master', 'node')]))
        self.assertEqual(component(plan, 'node-agents')['note'], '')

    def test_cni_paths_follow_the_plugin(self):
        """网络插件的残留目录按集群实际用的插件列。"""
        calico = plan_for(make_deployment(nodes=[('node',)], cni_plugin='calico'))
        self.assertEqual(
            component(calico, 'cni')['paths'],
            ['/etc/cni/', '/var/lib/cni/', '/etc/calico/', '/var/lib/calico/', '/var/log/calico/'],
        )
        flannel = plan_for(make_deployment(nodes=[('node',)], cni_plugin='flannel'))
        self.assertEqual(
            component(flannel, 'cni')['paths'],
            ['/etc/cni/', '/var/lib/cni/', '/run/flannel/'],
        )

    def test_unknown_plugin_still_lists_the_shared_directories(self):
        """插件认不出来时只列公共目录，而不是猜一个插件的路径。"""
        plan = plan_for(make_deployment(nodes=[('node',)], cni_plugin='cilium'))
        self.assertEqual(component(plan, 'cni')['paths'], ['/etc/cni/', '/var/lib/cni/'])

    def test_scope_counts_the_nodes_that_actually_carry_the_role(self):
        """作用范围按节点角色算：3 台控制面 + 2 台 worker。"""
        nodes = [
            ('etcd', 'master', 'node'), ('etcd', 'master', 'node'), ('etcd', 'master', 'node'),
            ('node',), ('node',),
        ]
        plan = plan_for(make_deployment(nodes=nodes))
        self.assertEqual(component(plan, 'etcd')['scope'], '在 3 台etcd 节点上')
        self.assertEqual(component(plan, 'control-plane')['scope'], '在 3 台控制面节点上')
        # clean_node 对控制面节点同样生效，所以 worker 那一档要把 master 一起算进来。
        self.assertEqual(component(plan, 'runtime')['scope'], '在 5 台集群节点上')
        self.assertEqual(component(plan, 'node-env')['scope'], '在全部 5 台节点上')

    def test_single_node_cluster_counts_everything_as_one(self):
        """单节点集群那一台机器同时是 etcd、master 与 node。"""
        plan = plan_for(make_deployment(nodes=[('etcd', 'master', 'node')]))
        self.assertEqual(component(plan, 'etcd')['scope'], '在 1 台etcd 节点上')
        self.assertEqual(component(plan, 'node-env')['scope'], '在全部 1 台节点上')

    def test_scope_is_still_readable_when_the_role_is_absent(self):
        """纯 worker 集群没有 etcd —— 说「在所有 etcd 节点上」比「0 台」通顺。"""
        plan = plan_for(make_deployment(nodes=[('node',)]))
        self.assertEqual(component(plan, 'etcd')['scope'], '在所有etcd 节点上')


class ExternalLbComponentTest(SimpleTestCase):
    """清单里的「外部负载均衡」一项：只在真装过的时候出现，位置与文案要对。"""

    MASTERS = [('etcd', 'master'), ('etcd', 'master'), ('etcd', 'master')]

    def test_row_is_absent_when_the_cluster_never_had_lb(self):
        """没装过 LB 的集群：``clean_lb.yml`` 的 when 条件一行都不输出，列出来只会永远待清理。"""
        plan = plan_for(make_deployment(nodes=self.MASTERS))
        self.assertNotIn('external-lb', [item['key'] for item in plan])

    def test_row_appears_when_the_snapshot_has_lb_machines(self):
        """快照里带 loadbalancer 角色就是装过的直接证据 —— 清理任务的开关字段不可信。"""
        nodes = self.MASTERS + [('loadbalancer',), ('loadbalancer',)]
        plan = plan_for(make_deployment(nodes=nodes))
        self.assertIn('external-lb', [item['key'] for item in plan])

    def test_row_appears_right_after_cni(self):
        """位置与 ``roles/clean/tasks/main.yml`` 的 import 次序一致：clean_node 之后、ENV PATH 之前。"""
        nodes = self.MASTERS + [('loadbalancer',), ('loadbalancer',)]
        keys = [item['key'] for item in plan_for(make_deployment(nodes=nodes))]
        self.assertEqual(keys.index('external-lb'), keys.index('cni') + 1)
        self.assertEqual(keys[keys.index('external-lb') + 1], 'node-env')

    def test_row_carries_the_lb_scope_and_paths(self):
        """作用范围点名负载均衡节点，路径是上游脚本真的会删的那几个。"""
        nodes = self.MASTERS + [('loadbalancer',), ('loadbalancer',)]
        item = component(plan_for(make_deployment(nodes=nodes)), 'external-lb')
        self.assertEqual(item['scope'], '在 2 台负载均衡节点上')
        self.assertEqual(item['paths'], ['/etc/l4lb/', '/etc/keepalived/', '/usr/local/sbin/keepalived'])
        self.assertIn('keepalived', item['detail'])

    def test_enabled_flag_alone_is_enough(self):
        """任务上勾的是自建模式（还没生成节点快照）时也要列出来。"""
        deployment = make_deployment(nodes=self.MASTERS)
        deployment.lb_mode = 'self'
        deployment.lb_vip = '192.168.1.250'
        plan = plan_for(deployment)
        self.assertIn('external-lb', [item['key'] for item in plan])

    def test_cloud_lb_does_not_get_a_row(self):
        """云内网负载均衡的集群不该有这一项。

        它的入口地址也非空（所以不能只按地址反推模式），但集群里根本没有 l4lb/keepalived：列出来
        只会是一个永远停在「待清理」的空框，最后被兜底刷绿 —— 操作员会以为自己漏看了什么。
        """
        deployment = make_deployment(nodes=self.MASTERS, lb_mode='cloud', lb_vip='10.0.9.250')
        plan = plan_for(deployment)
        self.assertNotIn('external-lb', [item['key'] for item in plan])

    def test_cloud_lb_with_a_snapshot_role_still_gets_a_row(self):
        """快照里真的有负载均衡机器时照列不误。

        模式字段是运维任务从集群记录继承来的，可能落后于现场；节点快照里带着 ``loadbalancer``
        角色才是「这台机器上真装过东西」的直接证据，宁可按实际清理。
        """
        nodes = self.MASTERS + [('loadbalancer',), ('loadbalancer',)]
        deployment = make_deployment(nodes=nodes, lb_mode='cloud', lb_vip='10.0.9.250')
        plan = plan_for(deployment)
        self.assertIn('external-lb', [item['key'] for item in plan])


class MarkerComponentTest(SimpleTestCase):
    """``marker_component``：哪一行算「某个组件开始清理了」。"""

    def test_recognizes_the_upstream_task_names(self):
        """逐字对上 kubeasz 的任务名（3.6.7 / 3.6.8 / 3.6.10 / 3.7.0 的 clean 剧本）。"""
        cases = {
            'TASK [clean : stop and disable etcd service] ***': 'etcd',
            'TASK [clean : stop and disable kube_master service] ***': 'control-plane',
            'TASK [clean : stop and disable kube_node service] ***': 'node-agents',
            'TASK [clean : umount containerd filesystems] ***': 'runtime',
            'TASK [clean : remove files and dirs2] ***': 'cni',
            'TASK [clean : remove service l4lb] ***': 'external-lb',
            "TASK [clean : clean 'ENV PATH'] ***": 'node-env',
            'TASK [clean : 删除 k8s_nodename] ***': 'hosts-entry',
        }
        for line, expected in cases.items():
            self.assertEqual(destroy_plan.marker_component(line), expected, line)

    def test_the_keepalived_task_is_not_a_second_row(self):
        """``remove service keepalived`` 与 l4lb 同属外部负载均衡，不另立一行。

        它在 clean_lb.yml 里紧跟着 l4lb，命中它时那一项已经在清理中，多一行只会让操作员
        以为有两件事要做。
        """
        line = 'TASK [clean : remove service keepalived] ***'
        self.assertEqual(destroy_plan.marker_component(line), '')

    def test_the_kubelet_cleanup_task_does_not_mark_the_cni_row(self):
        """``remove files and dirs``（删 kubelet 目录）不等于 ``remove files and dirs2``。

        两行只差一个字符，用 ``dirs`` 做子串比对会把 CNI 那一项提前点亮。
        """
        self.assertEqual(destroy_plan.marker_component('TASK [clean : remove files and dirs] ***'), '')
        self.assertEqual(
            destroy_plan.marker_component("TASK [clean : remove files and dirs of 'kube_node' nodes] ***"),
            '',
        )

    def test_task_names_with_a_jinja_template_are_not_markers(self):
        """``stop and disable {{ CONTAINERD_SERVICE_NAME }}`` 不能当 marker。

        只有 ansible ≥2.19 才会在 banner 里渲染模板；kubeasz 的运行镜像是 2.14.4，那一行会
        原样打印模板，写死 marker 会静默失效。这一项改用 ``umount containerd filesystems``。
        """
        line = 'TASK [clean : stop and disable {{ CONTAINERD_SERVICE_NAME }}] ***'
        self.assertEqual(destroy_plan.marker_component(line), '')

    def test_only_task_banners_count(self):
        """结果行里的路径不算进度。

        某项的 item 可能就是 ``/etc/cni/``，宽松比对会让它误亮。
        """
        self.assertEqual(destroy_plan.marker_component('changed: [10.0.0.1] => {"item": "/etc/cni/"}'), '')

    def test_unknown_lines_are_ignored(self):
        """认不出来的一律返回空串，不猜、也不抛异常。"""
        for line in ('', None, 'PLAY [clean] ***', 'ok: [10.0.0.1]', 'TASK [clean : something new] ***'):
            self.assertEqual(destroy_plan.marker_component(line), '')

    def test_first_match_wins(self):
        """清单有序，取第一个命中的即可（同名任务不可能同时属于两个组件）。"""
        line = 'TASK [clean : stop and disable etcd service, stop and disable kube_master service] ***'
        self.assertEqual(destroy_plan.marker_component(line), 'etcd')


class FoldComponentStatesTest(SimpleTestCase):
    """``fold_component_states``：按事件折叠出每一项的状态。"""

    def setUp(self):
        """准备一份七行清单。"""
        self.components = plan_for(make_deployment(nodes=[('etcd', 'master', 'node')]))

    def event(self, key, state):
        """造一条组件进度事件。

        参数：`key` 表示组件标识；`state` 表示状态。
        返回：返回事件替身。
        副作用：只做纯计算。
        """
        return SimpleNamespace(detail={'component': key, 'state': state})

    def test_without_events_everything_is_pending(self):
        """一条事件都没有时全是待清理 —— 包括模拟执行这种没有 ansible 输出的情形。"""
        states = destroy_plan.fold_component_states(self.components, [])
        self.assertEqual(set(states.values()), {'pending'})

    def test_starting_a_component_finishes_the_ones_before_it(self):
        """看到第 i 项开始，它之前的都算完成：ansible 的顺序保证了这一点。

        解析可能没赶上前面那一项的收尾（被 ``when`` 跳过的 task 零输出），
        否则页面会永远留着一条「清理中」。
        """
        states = destroy_plan.fold_component_states(
            self.components, [self.event('runtime', 'running')],
        )
        self.assertEqual(states['etcd'], 'done')
        self.assertEqual(states['control-plane'], 'done')
        self.assertEqual(states['node-agents'], 'done')
        self.assertEqual(states['runtime'], 'running')
        self.assertEqual(states['cni'], 'pending')
        self.assertEqual(states['hosts-entry'], 'pending')

    def test_done_events_are_recorded_as_is(self):
        """以「已清理」记的事件不改变其它项。"""
        states = destroy_plan.fold_component_states(
            self.components, [self.event('etcd', 'done')],
        )
        self.assertEqual(states['etcd'], 'done')
        self.assertEqual(states['control-plane'], 'pending')

    def test_the_last_event_wins(self):
        """running 之后跟 done，最终是 done。"""
        states = destroy_plan.fold_component_states(
            self.components, [self.event('etcd', 'running'), self.event('etcd', 'done')],
        )
        self.assertEqual(states['etcd'], 'done')

    def test_a_done_event_does_not_resurrect_an_earlier_row(self):
        """重试时事件是追加的：后面写下的 done 不该把更早那一行的 running 又翻回来。"""
        states = destroy_plan.fold_component_states(
            self.components,
            [self.event('cni', 'running'), self.event('etcd', 'done')],
        )
        self.assertEqual(states['cni'], 'running')

    def test_unknown_components_and_states_are_skipped(self):
        """认不出来的组件名或状态一律跳过，不新增行也不抛异常。"""
        states = destroy_plan.fold_component_states(
            self.components,
            [self.event('nope', 'running'), self.event('etcd', 'weird'), SimpleNamespace(detail=None)],
        )
        self.assertEqual(set(states), {item['key'] for item in self.components})
        self.assertEqual(set(states.values()), {'pending'})

    def test_states_cover_exactly_the_plan(self):
        """返回的键与清单一一对应，页面直接按 key 取用。"""
        states = destroy_plan.fold_component_states(self.components, [])
        self.assertEqual(list(states), [item['key'] for item in self.components])


class RecordingContext:
    """够用的上下文替身：收集事件，不做任何持久化。"""

    def __init__(self, explode=False):
        """初始化。

        参数：`explode` 表示写事件时是否抛异常（模拟事件表不可用）。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.events = []
        self.explode = explode

    def event(self, event_type, message, level='info', detail=None, **kwargs):
        """收集事件。"""
        if self.explode:
            raise RuntimeError('事件表不可用')
        self.events.append({
            'type': event_type, 'message': message, 'level': level, 'detail': detail, **kwargs,
        })

    def states(self):
        """按组件汇总出当前状态，便于断言。"""
        return [
            (event['detail']['component'], event['detail']['state']) for event in self.events
        ]


class DestroyProgressWatcherTest(SimpleTestCase):
    """``DestroyProgressWatcher``：把逐行输出翻译成组件进度事件。"""

    def setUp(self):
        """准备监听器与它要包装的原回调。"""
        from k8s_deployment.services.destroy_plan import DestroyProgressWatcher

        self.watcher_class = DestroyProgressWatcher
        self.components = plan_for(make_deployment(nodes=[('etcd', 'master', 'node')]))
        self.context = RecordingContext()
        self.lines = []

    def make_watcher(self, inner=None, context=None):
        """构造监听器。

        参数：`inner` 表示原回调；`context` 表示上下文替身。
        返回：返回监听器。
        副作用：只做纯计算。
        """
        self.context = context or self.context
        return self.watcher_class(self.context, self.components, inner=inner)

    def test_every_line_is_forwarded_to_the_original_callback(self):
        """必须继续调用原回调，否则日志面板会变成空白。"""
        watcher = self.make_watcher(inner=lambda stream, text: self.lines.append((stream, text)))
        watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        watcher('stdout', 'ok: [10.0.0.1]')
        self.assertEqual(
            self.lines,
            [('stdout', 'TASK [clean : stop and disable etcd service] ***'), ('stdout', 'ok: [10.0.0.1]')],
        )

    def test_it_works_without_an_inner_callback(self):
        """没有原回调时也要能跑（模拟执行与单测里就是这种情形）。"""
        watcher = self.make_watcher()
        watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        self.assertEqual(self.context.states(), [('etcd', 'running')])

    def test_a_marker_finishes_the_previous_components_and_starts_this_one(self):
        """看到第 i 项就开始收尾前 i-1 项，并给这一项记一条「正在清理」。"""
        watcher = self.make_watcher()
        watcher('stdout', 'TASK [clean : stop and disable kube_node service] ***')
        self.assertEqual(
            self.context.states(),
            [('etcd', 'done'), ('control-plane', 'done'), ('node-agents', 'running')],
        )

    def test_each_component_is_announced_once(self):
        """同一个组件的同一种状态只写一次：ansible 每个 task 会对每台主机各打一行。"""
        watcher = self.make_watcher()
        for _ in range(3):
            watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        self.assertEqual(self.context.states(), [('etcd', 'running')])

    def test_events_carry_the_stage_so_the_log_filter_works(self):
        """事件要挂 ``destroy.components`` 阶段，进度页的阶段筛选才认得出它。"""
        watcher = self.make_watcher()
        watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        self.assertEqual(self.context.events[0]['stage'], 'destroy.components')
        self.assertEqual(self.context.events[0]['type'], 'destroy_component')

    def test_finish_marks_everything_done(self):
        """兜底：模拟执行没有 ansible 输出，真机上日志行数也有上限。

        少了这一步，清理明明成功、页面上却永远停在「待清理」。
        """
        watcher = self.make_watcher()
        watcher.finish()
        self.assertEqual(self.context.states(), [(item['key'], 'done') for item in self.components])

    def test_finish_does_not_repeat_what_was_already_announced(self):
        """已经报过的那一条不重复写：「正在清理 etcd」之后只补一条「已清理 etcd」。"""
        watcher = self.make_watcher()
        watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        watcher.finish()
        # 一条 running + 每个组件一条 done。
        self.assertEqual(len(self.context.events), 1 + len(self.components))
        self.assertEqual(self.context.states()[0], ('etcd', 'running'))
        self.assertEqual(
            [state for key, state in self.context.states() if key == 'etcd'],
            ['running', 'done'],
        )

    def test_finish_is_idempotent(self):
        """重试或收尾被调用两次时不追加事件。"""
        watcher = self.make_watcher()
        watcher.finish()
        count = len(self.context.events)
        watcher.finish()
        self.assertEqual(len(self.context.events), count)

    def test_a_broken_event_sink_does_not_break_the_cleanup(self):
        """解析层的异常必须被吞掉。

        ``runner`` 会吞掉回调异常（写错既不会报错也不会留日志），所以这里自己兜住：
        宁可降级成「没有组件进度」，也不能让清理任务受影响。
        """
        watcher = self.make_watcher(context=RecordingContext(explode=True))
        watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        self.assertEqual(self.lines, [])

    def test_the_original_callback_is_called_before_parsing(self):
        """先转交原回调再解析：解析出问题时日志里至少还留着原样的输出。"""
        order = []
        watcher = self.make_watcher(context=RecordingContext(explode=True))
        watcher.inner = lambda stream, text: order.append('inner')
        watcher('stdout', 'TASK [clean : stop and disable etcd service] ***')
        self.assertEqual(order, ['inner'])

    def test_junk_lines_never_produce_events(self):
        """认不出来的行一条事件都不写。"""
        watcher = self.make_watcher()
        for line in ('PLAY [clean] ***', 'ok: [10.0.0.1]', '', 'changed: [10.0.0.1] => {"item": "/etc/cni/"}'):
            watcher('stdout', line)
        self.assertEqual(self.context.events, [])
