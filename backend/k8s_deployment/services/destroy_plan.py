"""清理集群的组件清单与实时进度解析。

清理只跑一条命令（``ezctl destroy <slug>``，底下是 ``playbooks/99.clean.yml``），
它不像部署那样有七个可以分步记账的阶段。但操作员真正想知道的是「节点上的东西清到哪了」，
而 ansible 的输出里恰好有这份信息：每个 task 开跑时会打一行 ``TASK [clean : <任务名>]``。

于是这里的做法是：**声明一份有序的组件清单**，按 kubeasz ``roles/clean`` 的执行顺序排列，
每项挂一个「任务名子串」；跑的时候逐行比对，看到第 i 项的 marker 就说明它之前的都清完了。
这比反过来「猜哪个任务标记了某项完成」稳：上游改了任务名只会让那一项不再提前变绿，
不会解析出错误的状态，更不会影响任务本身。

两个必须记住的前提：

- **被 ``when`` 跳过的 task 一行都不输出**（``ansible.cfg`` 里 ``display_skipped_hosts = False``）。
  所以「只在某组存在的 task」不会打 banner —— 这正是这里不列 chrony 的原因：平台生成的
  inventory 里那一段是空的，清理时零输出。外部负载均衡（``ex_lb``）本来是同一类，但自平台
  支持 VIP 之后它可能真的有内容，于是改成**按集群实际情况条件性列出**（见 ``components_for``）：
  没启用过外部负载均衡的集群不会多出这一行，启用过的才不会永远停在「待清理」。
- **marker 里不能有 Jinja 模板**。3.6.10 的 ``clean_node.yml`` 里停 containerd 那个 task 叫
  ``stop and disable {{ CONTAINERD_SERVICE_NAME }}``，只有 ansible ≥2.19 才会在 banner 里把
  模板渲染成实际值；kubeasz 自带的运行镜像是 ansible 2.14.4（``Dockerfile`` 里钉的），
  在 ≤2.18 上这一行原样打印模板，写死 marker 会静默失效。所以那一项改用同一分支里
  ``umount containerd filesystems``（3.6.7 :85 / 3.6.10 :86 逐字相同）。
"""

import re

# 组件清单，顺序即 kubeasz ``roles/clean`` 的执行顺序（main.yml 的 import 次序：
# clean_etcd → clean_master → clean_node → clean_lb → clean_chrony → ENV PATH → hosts）。
# ``scope`` 是作用范围的类型，展示时按集群实际的节点角色翻译成「在 N 台…上」。
DESTROY_COMPONENTS = [
    {
        'key': 'etcd',
        'label': 'etcd',
        'detail': '停止并禁用 etcd 服务，删除数据目录、/backup/k8s 与 systemd 单元文件',
        'scope': 'etcd',
        # 同一文件里还有 remove files and dirs（删 etcd 数据），但那一项与清服务是同一个组件，
        # 用服务的 banner 当 marker 能让这一项更早变绿，也不必区分两个同名任务。
        'marker': 'stop and disable etcd service',
    },
    {
        'key': 'control-plane',
        'label': '控制面',
        'detail': '停止并禁用 apiserver / controller-manager / scheduler，删除单元文件与 /var/run/kubernetes',
        'scope': 'master',
        'marker': 'stop and disable kube_master service',
    },
    {
        'key': 'node-agents',
        'label': '节点组件',
        'detail': '停止并禁用 kubelet / kube-proxy / kube-lb，卸载 kubelet 挂载，删除 /etc/kubernetes 与 /root/.kube/config',
        'scope': 'workload',
        'marker': 'stop and disable kube_node service',
    },
    {
        'key': 'runtime',
        'label': '容器运行时 containerd',
        'detail': '停止并禁用 containerd，卸载它的挂载，删除配置目录、数据目录与 crictl 配置',
        'scope': 'workload',
        'marker': 'umount containerd filesystems',
    },
    {
        'key': 'cni',
        'label': '网络插件残留',
        'detail': '删除 /etc/cni、/var/lib/cni 与所用插件留下的目录',
        'scope': 'workload',
        # 必须用完整串：同文件里还有 remove files and dirs（kubelet 目录）与
        # remove files and dirs of 'kube_node' nodes，拿 "dirs" 做子串会误命中那两行。
        'marker': 'remove files and dirs2',
    },
    {
        'key': 'external-lb',
        'label': '外部负载均衡',
        'detail': '停止并禁用 l4lb（nginx）与 keepalived，删除 /etc/l4lb、/etc/keepalived、'
                  '两个 systemd 单元与 /usr/local/sbin/keepalived',
        'scope': 'lb',
        # ``clean_lb.yml`` 的任务名逐字如此（kubeasz 3.6.7~3.7.0 一致）。它排在 clean_node
        # 之后、'ENV PATH' 之前，与本清单的位置一致。
        'marker': 'remove service l4lb',
    },
    {
        'key': 'node-env',
        'label': '节点环境变量',
        'detail': '删除 ~/.bashrc 里 kubeasz、kubectl、helm、crictl 的补全与 HELM_TLS_ENABLE',
        'scope': 'all',
        'marker': "clean 'ENV PATH'",
    },
    {
        'key': 'hosts-entry',
        'label': '主机名解析',
        'detail': '删除 /etc/hosts 里 kubeasz 托管的主机名解析块',
        'scope': 'all',
        'marker': '删除 k8s_nodename',
    },
]

# 作用范围的中文说法（``all`` 单独处理：它想强调「这台机器上的所有节点」而不是某类角色）。
SCOPE_LABELS = {
    'etcd': 'etcd 节点',
    'master': '控制面节点',
    'workload': '集群节点',
    'lb': '负载均衡节点',
}

# kubeasz 的默认目录。集群没用自定义数据目录时就是这些值；用了 ``dataDir`` 时
# ``compiler`` 会把 ETCD_DATA_DIR / CONTAINERD_ROOT_DIR 改写掉，因此要从配置快照里读回来。
DEFAULT_PATHS = {
    'ETCD_DATA_DIR': '/var/lib/etcd',
    'ETCD_WAL_DIR': '',
    'CONTAINERD_CONFIG_DIR': '/etc/containerd',
    'CONTAINERD_ROOT_DIR': '/var/lib/containerd',
    'CONTAINERD_STATE_DIR': '/run/containerd',
    'KUBELET_ROOT_DIR': '/var/lib/kubelet',
}

# 各网络插件在节点上留下的目录（``clean_node.yml`` 的 ``remove files and dirs2``）。
CNI_PATHS = {
    'calico': ['/etc/calico/', '/var/lib/calico/', '/var/log/calico/'],
    'flannel': ['/run/flannel/'],
}


def config_value(config_text, key, default=''):
    """从渲染后的 config.yml 文本里取一个变量的值。

    参数：`config_text` 表示 config.yml 全文；`key` 表示变量名；`default` 表示取不到时的回落值。
    返回：返回去掉引号的字符串值；变量不存在或值为空时返回 ``default``。
    副作用：纯字符串处理，不修改任何状态。

    不引 yaml 解析器：这份文本是平台自己渲染出来的，格式固定为 ``KEY: "value"``，
    正则足够，而且不必为「取一个路径」把整个文件建成对象。
    """
    match = re.search(rf'^{re.escape(key)}\s*:\s*(.*)$', str(config_text or ''), re.MULTILINE)
    if not match:
        return default
    value = match.group(1).strip()
    # 值后面可能跟着行内注释，或者本来就是带引号的字符串。
    value = value.split('#', 1)[0].strip().strip('"').strip("'")
    return value or default


def resolved_paths(cluster):
    """返回该集群实际生效的那组目录变量。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{变量名: 路径}``。
    副作用：读取数据库（配置版本快照），不修改数据。

    为什么不能直接用 kubeasz 的默认值：``compiler`` 在勾了自定义数据目录时会把
    ``ETCD_DATA_DIR`` / ``CONTAINERD_ROOT_DIR`` / ``KUBELET_ROOT_DIR`` 改写成
    ``<data_dir>/…``，而运维任务的 ``data_dir`` 字段**不可信** ——
    ``cluster_ops.inherited_options`` 没有继承它，任务上落的是模型默认值。配置快照里的
    ``config`` 是渲染后的全文，是唯一还留着真实路径的地方。
    """
    from . import register

    config_text = str((register.latest_snapshot(cluster) or {}).get('config') or '')
    return {
        key: config_value(config_text, key, default)
        for key, default in DEFAULT_PATHS.items()
    }


def _node_counts(deployment):
    """按角色统计任务快照里的节点数量。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回 ``{'etcd': n, 'master': n, 'workload': n, 'all': n}``。
    副作用：读取数据库（节点快照），不修改数据。
    """
    counts = {'etcd': 0, 'master': 0, 'workload': 0, 'lb': 0, 'all': 0}
    for node in deployment.nodes.all():
        roles = {str(item) for item in (node.roles or [])}
        # ``all`` 有意把负载均衡机器算进去：scope 为 all 的两项（环境变量、主机名解析）在
        # ``99.clean.yml`` 里对 play 的**全部**主机执行，而 ex_lb 也在那个 play 里。
        counts['all'] += 1
        if 'etcd' in roles:
            counts['etcd'] += 1
        if 'master' in roles:
            counts['master'] += 1
        # kubeasz 的 clean_node 对控制面节点同样生效（``inventory_hostname in groups['kube_master']
        # or ... in groups['kube_node']``），所以这里把两类都算进去。
        if roles & {'master', 'node'}:
            counts['workload'] += 1
        # 负载均衡机器不是集群节点，单独计数：它们上面只跑 nginx 与 keepalived。
        if 'loadbalancer' in roles:
            counts['lb'] += 1
    return counts


def _has_external_lb(deployment, counts):
    """判断这个集群是否装过外部负载均衡。

    参数：`deployment` 表示 K8sDeployment 实例；`counts` 表示 ``_node_counts`` 的返回。
    返回：装过返回真。
    副作用：只读取入参，不修改数据。

    只看模式是不够的：清理任务虽然会从集群记录继承 ``lb_mode``，但更早的任务（或快照本身）
    可能没有这个值。真正可靠的是**节点快照**——清理任务的节点清单来自最新配置版本的快照，
    里面带着 ``loadbalancer`` 角色，那是集群装过 LB 的直接证据。

    反过来，云内网负载均衡的集群没有负载均衡机器、集群里也没有 l4lb/keepalived 可清：
    它的模式是 ``cloud`` 而不是 ``self``，所以不会被前一条判真，而它的节点快照里也不会有
    ``loadbalancer`` 角色。列出来只会是一个永远停在「待清理」的空框。
    """
    # ``'self'`` 与 ``compiler.LB_MODE_SELF`` 是同一个值。本模块刻意不 import compiler
    # （它只做「清单长什么样」这一件事，不碰 kubeasz 的渲染契约），两处都改了才算改对。
    if str(getattr(deployment, 'lb_mode', '') or '').strip().lower() == 'self':
        return True
    return counts.get('lb', 0) > 0


def _scope_text(scope, counts):
    """把作用范围翻译成一句中文。

    参数：`scope` 表示范围类型；`counts` 表示 ``_node_counts`` 的返回。
    返回：返回中文说明文本。
    副作用：纯计算，不修改任何状态。
    """
    if scope == 'all':
        return f'在全部 {counts["all"]} 台节点上'
    label = SCOPE_LABELS.get(scope, '节点')
    count = counts.get(scope, 0)
    if count <= 0:
        return f'在所有{label}上'
    return f'在 {count} 台{label}上'


def components_for(cluster, deployment):
    """生成该集群本次清理的组件清单。

    参数：`cluster` 表示 K8sCluster 实例；`deployment` 表示 K8sDeployment 实例。
    返回：返回有序的组件列表，每项含 ``key``/``label``/``detail``/``scope``/``paths``/``note``。
    副作用：读取数据库，不修改数据。

    ``paths`` 只列 **kubeasz 清理脚本真的会删** 的路径，不复述配置里的变量名：清单是给
    操作员核对现场用的，写成「ETCD_DATA_DIR」等于没说。因此 node-agents 那一项列的是
    脚本里写死的 ``/var/lib/kubelet/``，而不是 ``KUBELET_ROOT_DIR`` —— 这两者在勾了自定义
    数据目录时并不相等，那时脚本清不掉真正的 kubelet 数据，这一点由 ``note`` 点出来。

    外部负载均衡那一项按集群实际情况决定列不列：kubeasz 里它被 ``when: inventory_hostname
    in groups['ex_lb']`` 守着，没启用过 LB 的集群跑清理时那一组是空的、一行 banner 都没有，
    列出来就只会一直停在「待清理」，最后被 ``DestroyProgressWatcher.finish`` 兜底刷绿。
    """
    paths = resolved_paths(cluster)
    counts = _node_counts(deployment)
    plugin = str(getattr(deployment, 'cni_plugin', '') or 'calico')
    cni_extra = CNI_PATHS.get(plugin, [])
    has_lb = _has_external_lb(deployment, counts)

    plan = []
    for spec in DESTROY_COMPONENTS:
        if spec['key'] == 'external-lb' and not has_lb:
            continue
        item = {
            'key': spec['key'],
            'label': spec['label'],
            'detail': spec['detail'],
            'scope': _scope_text(spec['scope'], counts),
            'paths': [],
            'note': '',
        }
        if spec['key'] == 'etcd':
            item['paths'] = [paths['ETCD_DATA_DIR']]
            if paths['ETCD_WAL_DIR'] and paths['ETCD_WAL_DIR'] != paths['ETCD_DATA_DIR']:
                item['paths'].append(paths['ETCD_WAL_DIR'])
            item['paths'].append('/backup/k8s')
        elif spec['key'] == 'control-plane':
            item['paths'] = ['/var/run/kubernetes']
        elif spec['key'] == 'node-agents':
            item['paths'] = ['/var/lib/kubelet/', '/var/lib/kube-proxy/', '/etc/kube-lb/', '/etc/kubernetes/']
            kubelet_root = paths['KUBELET_ROOT_DIR'].rstrip('/')
            if kubelet_root and kubelet_root != DEFAULT_PATHS['KUBELET_ROOT_DIR']:
                # 上游的清理脚本把 kubelet 数据目录写死在 /var/lib/kubelet，自定义数据目录不在
                # 它的删除清单里。说清楚比让操作员以为清干净了强。
                item['note'] = (
                    f'该集群用了自定义数据目录，kubelet 数据实际在 {kubelet_root}/，'
                    'kubeasz 的清理脚本不会删它，需要人工确认后处理。'
                )
        elif spec['key'] == 'runtime':
            item['paths'] = [
                paths['CONTAINERD_CONFIG_DIR'], '/etc/crictl.yaml',
                '/opt/containerd/', paths['CONTAINERD_ROOT_DIR'], paths['CONTAINERD_STATE_DIR'],
            ]
        elif spec['key'] == 'cni':
            item['paths'] = ['/etc/cni/', '/var/lib/cni/'] + cni_extra
        elif spec['key'] == 'external-lb':
            # 上游 clean_lb.yml 删的就是这几个：两个配置目录、两个单元文件、二进制。
            item['paths'] = ['/etc/l4lb/', '/etc/keepalived/', '/usr/local/sbin/keepalived']
        plan.append(item)
    return plan


def marker_component(line):
    """判断一行输出是否标记了某个组件开始清理。

    参数：`line` 表示 ansible 的一行输出。
    返回：返回命中的组件标识；没命中返回空串。
    副作用：纯计算，不修改任何状态。

    顺序敏感：清单是按 kubeasz 的执行顺序排的，同名任务不可能同时属于两个组件，
    所以取第一个命中即可。
    """
    text = str(line or '')
    if 'TASK [' not in text:
        # 只有 task banner 才是 marker。收窄到这里既省去逐条比对，也避免结果行里
        # 偶然出现的路径（比如某项的 item 是 /etc/cni/）被误当成提前进度。
        return ''
    for spec in DESTROY_COMPONENTS:
        if spec['marker'] in text:
            return spec['key']
    return ''


def fold_component_states(components, events):
    """按任务事件折叠出每个组件当前的状态。

    参数：`components` 表示组件清单；`events` 表示 ``destroy_component`` 事件的可迭代对象。
    返回：返回 ``{组件标识: 'pending'|'running'|'done'}``。
    副作用：只读取传入的事件，不修改任何状态。

    只看事件、不看 ``current_stage``：``claim_next_task`` 在重试时会把 ``current_stage``
    清空，拿它当依据会让重试后的页面状态往回退。事件是追加写、从不清的。
    """
    order = [item['key'] for item in components]
    states = {key: 'pending' for key in order}
    for event in events:
        detail = getattr(event, 'detail', None) or {}
        key = str(detail.get('component') or '')
        state = str(detail.get('state') or '')
        if key not in states or state not in ('running', 'done'):
            continue
        if state == 'running':
            # 看到第 i 个组件开始，它之前的都算完成：解析可能没赶上前面那一项的收尾，
            # 而 ansible 的执行顺序保证了「开始清第 i 项」隐含「前 i-1 项已经清完」。
            for earlier in order[:order.index(key)]:
                if states[earlier] != 'done':
                    states[earlier] = 'done'
            states[key] = 'running'
        else:
            states[key] = 'done'
    return states


class DestroyProgressWatcher:
    """把 ezctl 的逐行输出翻译成组件进度事件。

    它是一个 ``on_line`` 回调的包装层：**必须把每一行继续交给原来的回调**，否则日志面板
    会变成空白。整体再套一层 try/except —— ``runner`` 会吞掉回调异常（``runner.py`` 里
    ``on_line`` 与 ``_drain`` 都包了 ``except Exception: pass``），解析写错既不会报错也不会
    留日志，只能靠单测发现。宁可解析失败静默降级成「没有组件进度」，也不能让清理任务受影响。
    """

    def __init__(self, context, components, inner=None):
        """初始化监听器。

        参数：`context` 表示任务上下文；`components` 表示组件清单；`inner` 表示原有的逐行回调。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.context = context
        self.components = list(components or [])
        self.inner = inner
        self._order = [item['key'] for item in self.components]
        # 已经发过「开始清理」事件的组件。
        self._announced = set()
        # 已经发过「已清理」事件的组件。两者分开：同一个组件只会各发一次。
        self._done = set()

    def __call__(self, stream, text):
        """接收一行输出：先转交原回调，再尝试解析组件进度。

        参数：`stream` 表示输出流名称；`text` 表示这一行的内容。
        返回：无显式返回值。
        副作用：写日志与事件。
        """
        if self.inner:
            self.inner(stream, text)
        try:
            self._observe(text)
        except Exception:
            pass

    def _observe(self, line):
        """按一行输出推进组件状态。

        参数：`line` 表示这一行的内容。
        返回：无显式返回值。
        副作用：写入组件进度事件。
        """
        key = marker_component(line)
        if not key or key in self._announced:
            return
        index = self._order.index(key)
        for spec in self.components[:index]:
            self._finish(spec)
        spec = self.components[index]
        self._emit(spec, 'running', f'正在清理 {spec["label"]}（{spec["detail"]}）')

    def finish(self):
        """把还没标完成的组件全部标记为已清理。

        参数：无。
        返回：无显式返回值。
        副作用：写入组件进度事件。

        ezctl 成功退出时调用。必须有的兜底：模拟执行不产生任何 ansible 输出；真机上
        日志行数也受 ``runner.MAX_CAPTURED_LINES`` 限制，超限之后不再喂回调。少了这一步，
        清理明明成功、页面上却永远停在「待清理」。
        """
        for spec in self.components:
            self._finish(spec)

    def _finish(self, spec):
        """把单个组件标记为已清理。

        参数：`spec` 表示组件定义。
        返回：无显式返回值。
        副作用：写入组件进度事件；同一个组件重复调用只写一次。
        """
        self._emit(spec, 'done', f'已清理 {spec["label"]}')

    def _emit(self, spec, state, message):
        """写入一条组件进度事件。

        参数：`spec` 表示组件定义；`state` 表示状态（running/done）；`message` 表示中文说明。
        返回：无显式返回值。
        副作用：向事件表写入一行；每个组件的每种状态都只写一次。
        """
        seen = self._announced if state == 'running' else self._done
        if spec['key'] in seen:
            return
        seen.add(spec['key'])
        self.context.event(
            'destroy_component',
            message,
            level='info',
            stage='destroy.components',
            detail={'component': spec['key'], 'label': spec['label'], 'state': state},
        )
