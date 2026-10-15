"""作用于「已存在集群」的运维任务构造：扩容、升级、备份、恢复。

这些任务与新建部署最大的区别是**参数来源不同**：向导部署时参数全部来自用户填写，
而运维操作的大部分参数是集群已经固化的状态——用哪个 kubeasz 版本、装的是什么系统、
有哪些节点。用户只需要提供真正的增量信息（新加的机器、升级的目标版本、要恢复的备份）。

因此这里的职责是「把集群的既有状态翻译成一条新任务的字段」，并且**宁可报错也不猜**：
- 集群没有配置版本记录时，说明平台不知道它的真实拓扑，直接拒绝而不是按空的清单硬跑；
- 配置文件（hosts/config.yml）在任务开始时由状态机从最新配置版本还原，不在这里重新渲染，
  否则自动生成的节点名会被重新分配，kubeasz 就会以为集群换了一批机器。
"""

from django.conf import settings
from django.utils import timezone

from k8s_deployment.models import (
    CLUSTER_TARGETED_TASKS,
    NON_TERMINAL_STATUSES,
    K8sDeployment,
    K8sDeploymentNode,
)
from k8s_machine.models import Machine

from . import register, state_machine

# 运维任务默认继承的字段名单：这些值不参与真正执行（配置从快照还原），
# 但会进入预检与审计记录，因此必须填成集群的真实情况。
# ``lb_mode``/``lb_vip`` 也在其中：清理任务要靠它决定组件清单里列不列「外部负载均衡」，
# 审计记录里也该看得见这个集群的入口模式与地址。
#
# ``pod_cidr``/``service_cidr``/``api_server_port`` 同样在其中，而且必须**真的**从集群读回来：
# 这三个值在集群建好那一刻就定了（改它们等于重建集群），运维表单里没有输入框。谁都不是
# 它们的主人，只有集群配置版本快照是 —— 编一个默认值出来，预检会拿它去比真实网段。
#
# 注意这只是一份「意图清单」：真正决定某个键能不能写进任务的是下面那个
# ``TASK_FIELD_NAMES`` 白名单（``inherited_options`` 与 ``build_task`` 都按字段名透传，
# 不读这个元组）。它的作用是让人一眼看出哪些字段属于「从集群继承」，并由
# ``test_cluster_ops`` 钉住，避免有人顺手把其中一个从 ``inherited_options`` 里删掉。
INHERITED_FIELDS = (
    'topology_mode', 'network_interface', 'kube_proxy_mode', 'api_server_port', 'cni_plugin',
    'pod_cidr', 'service_cidr',
    'lb_mode', 'lb_vip',
)

# 会跑预检的运维任务类型：``state_machine`` 里三处 ``_run_preflight`` 调用点对应
# 「添加节点」「升级」，加上向导部署本身。备份、恢复、清理不跑预检，因此也不该因为
# 读不到网段被拦下 —— 那三个任务不重新生成集群配置，网段对它们没有意义。
PREFLIGHT_TASK_TYPES = frozenset({'add_master', 'add_worker', 'upgrade'})

# 允许从 options 透传给模型的字段名，避免把只用于快照的键（如 backup_id）也展开成参数。
TASK_FIELD_NAMES = frozenset(field.name for field in K8sDeployment._meta.fields)

# ``build_task`` 自己显式赋值的字段：这些键即使出现在 options 里也不能透传，
# 否则会以「重复关键字」在创建任务时抛 TypeError。
EXPLICIT_FIELD_NAMES = frozenset({
    'organization', 'cluster', 'task_type', 'status', 'slug', 'display_name',
    'expected_config_revision', 'execution_mode', 'execution_mode_reason',
    'request_snapshot', 'queued_at', 'requested_by',
})


class ClusterOpError(Exception):
    """运维任务无法构造，携带可直接展示给用户的中文原因。"""


def active_task_for(cluster):
    """返回集群当前未完成的运维任务。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 K8sDeployment 或 None。
    副作用：只读数据库，不修改数据。

    一个集群同一时刻只允许一个非终态变更任务：备份也独占集群（规划 6.1）。
    """
    return (
        K8sDeployment.objects
        .filter(cluster=cluster, status__in=NON_TERMINAL_STATUSES)
        .order_by('queued_at', 'id')
        .first()
    )


def assert_deployed(cluster):
    """确认集群由平台部署，才允许运维操作。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：无显式返回值。
    副作用：只读对象字段。

    导入型集群平台没有它的 inventory 与 SSH 免密通道，硬跑 ezctl 会用一份凭想象生成的
    配置覆盖别人手工维护的集群（规划 6.1）。

    已销毁的集群同样拦截。它的配置版本还在、标识已被清空，两道旧闸门都会放行，于是同一条
    墓碑记录可以被反复提交清理 —— 每次都在节点上跑一遍空剧本，还拿不到集群目录名。
    """
    if getattr(cluster, 'origin', 'imported') != 'deployed':
        raise ClusterOpError(
            '该集群是导入的，平台没有它的部署配置与免密通道，因此不提供扩容、升级、备份与恢复操作。'
        )
    if getattr(cluster, 'status', '') == 'destroyed':
        raise ClusterOpError(
            '该集群已经清理过了，不能重复清理。请直接对这批机器重新部署，或先删除这条集群记录。'
        )
    if register.current_revision(cluster) <= 0:
        raise ClusterOpError(
            '该集群没有配置版本记录，平台无法确定它的真实拓扑与版本，出于安全考虑不提供运维操作。'
        )


def node_payloads(cluster):
    """把集群配置版本里的节点清单翻译成任务节点结构。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``[{'machine': Machine|None, 'name':.., 'ip_address':.., 'ssh_port':..,
        'ssh_user':.., 'roles':[..], 'node_name':..}, ...]``。
    副作用：读取数据库，不修改数据。

    机器可能已经从机器表里删掉了（节点快照仍然有效），此时 ``machine`` 为 None，
    但 IP、端口与用户名还在，任务仍然能连上并执行。
    """
    snapshot = register.latest_snapshot(cluster)
    entries = [item for item in (snapshot.get('nodes') or []) if isinstance(item, dict) and item.get('ip_address')]
    if not entries:
        raise ClusterOpError('该集群的配置版本里没有节点记录，无法构造运维任务。')

    machines = {
        str(item.ip_address): item
        for item in Machine.objects.filter(ip_address__in=[str(c['ip_address']) for c in entries])
    }
    result = []
    for item in entries:
        ip = str(item['ip_address'])
        result.append({
            'machine': machines.get(ip),
            'name': str(item.get('name') or ip),
            'ip_address': ip,
            'ssh_port': int(item.get('ssh_port') or 22),
            'ssh_user': str(item.get('ssh_user') or 'root'),
            'roles': list(item.get('roles') or []),
            'node_name': str(item.get('node_name') or ''),
        })
    return result


def new_machine_payload(machine, role):
    """把一台新机器翻译成任务节点结构。

    参数：`machine` 表示 Machine 实例；`role` 表示要赋予的角色。
    返回：返回与 ``node_payloads`` 同构的字典。
    副作用：只读对象字段。
    """
    return {
        'machine': machine,
        'name': machine.name,
        'ip_address': str(machine.ip_address),
        'ssh_port': machine.ssh_port,
        'ssh_user': machine.ssh_user,
        'roles': [role],
        'node_name': '',
    }


def as_bool(value):
    """把请求体里的布尔值归一化。

    参数：`value` 表示请求体里的原始值。
    返回：返回 True 或 False。
    副作用：纯计算，不修改任何状态。

    JSON 请求体里这个字段可能是真布尔值，也可能被前端传成字符串 ``"true"``/
    ``"on"``；只有明确表达「是」的几个值算 True，其余一律按 False 处理——
    勾选框漏传时应当落到**会做备份**的那一侧。
    """
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def inherited_options(cluster, task_type, payload=None):
    """汇出运维任务的公共字段。

    参数：`cluster` 表示 K8sCluster 实例；`task_type` 表示任务类型；`payload` 表示请求体。
    返回：返回可直接写入 K8sDeployment 的字段字典。
    副作用：读取配置版本与配置中心，不修改数据。
    """
    from k8s_config.models import K8sVersionMapping

    payload = payload or {}
    snapshot = register.latest_snapshot(cluster)
    version = state_machine.cluster_active_version(cluster)
    os_hint = register.cluster_os_hint(cluster)
    network = register.cluster_network_facts(cluster)

    os_type = str(payload.get('os_type') or os_hint['os_type'] or '')
    os_version = str(payload.get('os_version') or os_hint['os_version'] or '')
    if not os_type or not os_version:
        # 配置版本里没有系统信息（本条字段加入之前部署的集群）。回落成该 kubeasz 版本
        # 支持的系统只是为了把字段填满；预检会拿实机 /etc/os-release 硬比对，
        # 真不一致时任务会以清晰的预检失败中止，而不是带着错信息跑下去。
        mapping = K8sVersionMapping.objects.filter(kubeasz_version=version['kubeasz']).first()
        first_system = mapping.supported_systems.filter(is_active=True).order_by('sort_order', 'id').first() \
            if mapping else None
        if first_system is not None:
            os_type = os_type or first_system.os_type
            os_version = os_version or first_system.version

    options = {
        'display_name': cluster.name,
        'kubeasz_version': version['kubeasz'] or str(snapshot.get('kubeasz_version') or ''),
        'runtime': str(snapshot.get('runtime') or 'containerd'),
        'os_type': os_type or '未知',
        'os_version': os_version or '未知',
        'topology_mode': str(payload.get('topology_mode') or snapshot.get('topology_mode') or 'ha'),
        'node_naming': str(payload.get('node_naming') or 'auto'),
        'network_interface': str(payload.get('network_interface') or 'eth0'),
        # 与网段同理：kube-proxy 模式是集群建好那一刻定下的事实（写在这份 hosts 的
        # ``all:vars`` 里），运维表单里没有这个输入框。请求体只在升级里才允许覆盖
        # （见下面的 upgrade 分支）：改模式要连 ``PROXY_MODE`` 一起改写并发给节点，
        # 不是改个字段就算数。
        #
        # 集群读不到模式时**留空而不是回落 ipvs**：空值表示「不知道」，预检会跳过模式相关的
        # 判断；编一个 ipvs 出来则会让一台没开 ipvs 的机器被要求加载 ip_vs 模块，
        # 也会让「用户是否显式改过模式」这件事没法分辨（改没改都长得一样）。
        'kube_proxy_mode': (
            register.cluster_proxy_mode(cluster)
            or str(payload.get('kube_proxy_mode') or '').strip().lower()
        ),
        # 集群自己说了算，请求体只用来补集群没记下来的缺口（本轮之前部署的集群）。
        # 反过来让请求体优先是不行的：它一旦被填错，预检就会拿一个与集群无关的网段去比对，
        # 报出来的还是「网段合法性」这种像是用户填错的口径（94 上添加 Node 失败就是这样）。
        'pod_cidr': network['pod_cidr'] or str(payload.get('pod_cidr') or ''),
        'service_cidr': network['service_cidr'] or str(payload.get('service_cidr') or ''),
        'api_server_port': network['api_server_port'] or int(payload.get('api_server_port') or 6443),
        'cni_plugin': str(payload.get('cni_plugin') or snapshot.get('network_plugin') or 'calico'),
        'cluster_description': str(payload.get('remark') or cluster.remark or ''),
    }
    # 外部访问入口的模式与地址取自集群记录（部署成功时由 ``register.ensure_deployed_cluster``
    # 回写，见 ``K8sCluster.lb_mode`` / ``api_server_vip``）。有了它们，清理任务就能知道自己
    # 该不该列「外部负载均衡」那一项组件，而不必去猜：模式为 ``self`` 才真在集群里装过代理。
    #
    # 只凭地址非空反推是不够的 —— 云内网负载均衡的集群也有地址，但集群里没有任何 l4lb/keepalived
    # 要清；反过来，快照里的模式优先于集群记录，让用户当次的选择能覆盖记录下来的事实。
    lb_vip = str(payload.get('lb_vip') or getattr(cluster, 'api_server_vip', '') or '').strip()
    options['lb_vip'] = lb_vip
    options['lb_mode'] = str(
        payload.get('lb_mode') or getattr(cluster, 'lb_mode', '') or 'none'
    ).strip().lower() or 'none'
    if task_type == 'restore':
        # 恢复是回到备份那一刻的版本，而不是当前版本。
        options['kubernetes_version'] = str(payload.get('kubernetes_version') or '')
        # 要恢复哪一份备份必须写进任务快照：恢复执行时只能用它，
        # 不允许回落到「取最新一份」这种默认行为（规划 11.2）。
        options['backup_id'] = str(payload.get('backup_id') or '')
        # 「跳过应急备份」是用户对高风险动作的显式选择，必须固化在任务快照里：
        # 恢复执行时要按当时的决定做，不能事后读一个可以改的开关（规划 11.2）。
        options['skip_emergency_backup'] = as_bool(payload.get('skip_emergency_backup'))
    else:
        options['kubernetes_version'] = str(
            payload.get('kubernetes_version') or version['kubernetes'] or cluster.kube_version or ''
        )
    if task_type == 'upgrade':
        # 跨次版本升级要连着换 kubeasz 制品，理由见 ``_cross_minor_kubeasz``。
        options['kubeasz_version'] = _cross_minor_kubeasz(options, cluster) or options['kubeasz_version']
        # 升级是唯一允许改 kube-proxy 模式的入口，且只在目标 kubeasz 不认集群当前模式时才
        # 用得上（1.35 的 ipvs 集群升 1.36：3.7.0 的模板只认 iptables 与 nftables）。
        # 这里只把用户的选择落到任务快照上，合法性由 ``_validate_upgrade_target`` 判。
        requested_mode = str(payload.get('kube_proxy_mode') or '').strip().lower()
        if requested_mode:
            options['kube_proxy_mode'] = requested_mode
    return options


# ---- 升级目标版本 ------------------------------------------------------
#
# 「能升到哪些版本」只有一份口径：``upgrade_target_options`` 供前端出下拉，
# ``_validate_upgrade_target`` 供提交时把关。两处各算一遍的话，界面上能选中的版本
# 有可能在提交时被拒（或者反过来，提交时放过一个界面根本没给过的版本）。


def _mapping_rows():
    """返回配置中心里已启用的版本映射，按版本号升序。

    参数：无。
    返回：返回字典列表，每项含 ``version_key``、``kubernetes_version``、``kubeasz_version``。
    副作用：只读数据库。

    版本比较一律用 ``version_key``（主版本 × 1000 + 次版本）而不是字符串：按字符串比
    ``'1.9'`` 会排在 ``'1.10'`` 后面，次版本号一旦到两位数就全错。解析不出来的行直接丢掉
    —— 配置中心允许手工录入，一行写坏的版本号不该让整份候选清单算成别的东西。
    """
    from k8s_config.models import K8sVersionMapping

    from . import upgrade_precheck

    rows = []
    for mapping in K8sVersionMapping.objects.filter(is_active=True).order_by('sort_order', 'id'):
        text = str(mapping.kubernetes_version or '').strip()
        parsed = upgrade_precheck.parse_version(text)
        if parsed is None:
            continue
        rows.append({
            'version_key': parsed[0] * 1000 + parsed[1],
            'kubernetes_version': text,
            'kubeasz_version': str(mapping.kubeasz_version or '').strip(),
        })
    return sorted(rows, key=lambda item: item['version_key'])


def upgrade_target_options(cluster):
    """列出该集群当前可以升级到的目标版本。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'current', 'current_proxy_mode', 'options', 'reason'}``；
        ``options`` 为空时 ``reason`` 说明原因。
    副作用：只读数据库。

    **平台一次只允许升一个次版本**（Kubernetes 自身也不允许跳 minor），因此候选只有
    「当前次版本 + 1」那一行，通常也就一项。集群当前版本取 ``cluster.kube_version``，
    与 ``state_machine.cluster_active_version`` 同源：部署时写进任务快照的就是配置中心
    的次版本号（``1.35`` 这种粒度），两边量的是同一件事。

    ``reason`` 分两种：配置中心里压根没有更高的版本（要先去登记），或者有但相隔不止
    一个次版本（必须先升中间那一级）。两种情况操作员要做的事不一样，所以分开说。

    每项候选都带上 ``proxy_modes``（该目标的 kubeasz 模板支持哪几种 kube-proxy 模式）与
    ``proxy_mode_change_required``（集群当前模式是否已经不被它支持）。要改模式时用户必须在
    升级确认框里给出新的模式 —— 跨次版本升级会连着换 kubeasz 制品，而 1.35 的 ipvs 在
    3.7.0 的模板里已经没有了，不说清楚就会卡在目标节点预检上白跑一轮。
    """
    from . import preflight, upgrade_precheck

    current = str(getattr(cluster, 'kube_version', '') or '').strip().lstrip('v')
    cluster_mode = register.cluster_proxy_mode(cluster)
    result = {'current': current, 'current_proxy_mode': cluster_mode, 'options': [], 'reason': ''}
    parsed = upgrade_precheck.parse_version(current)
    if parsed is None:
        result['reason'] = f'无法解析集群当前版本「{current or "（空）"}」，平台无法判断它能升到哪个版本。'
        return result

    current_key = parsed[0] * 1000 + parsed[1]
    rows = _mapping_rows()
    options = []
    for row in rows:
        if row['version_key'] != current_key + 1:
            continue
        modes, default_mode = preflight.kubeasz_proxy_modes(row['kubeasz_version'])
        options.append({
            'kubernetes_version': row['kubernetes_version'],
            'kubeasz_version': row['kubeasz_version'],
            'proxy_modes': list(modes),
            'proxy_mode_default': default_mode,
            'proxy_mode_change_required': bool(cluster_mode) and cluster_mode not in modes,
        })
    if options:
        result['options'] = options
        return result

    higher = [row for row in rows if row['version_key'] > current_key]
    if not higher:
        result['reason'] = (
            f'配置中心里没有比 {current} 更高的已启用版本，无法确定升级目标。'
            '请先在 平台设置 → K8s 配置中心 登记更高版本（含它的 kubeasz 版本）并同步该版本制品。'
        )
    else:
        listing = '、'.join(row['kubernetes_version'] for row in higher)
        result['reason'] = (
            f'配置中心里比 {current} 更高的版本是 {listing}，与当前相隔不止一个次版本。'
            '平台一次只能升一个次版本（Kubernetes 不允许跳 minor），请先登记并升级到中间的版本。'
        )
    return result


def _cross_minor_kubeasz(options, cluster):
    """跨次版本升级时返回目标版本对应的 kubeasz 版本，同次版本升级返回空串。

    参数：`options` 表示已算好的公共字段；`cluster` 表示 K8sCluster 实例。
    返回：返回 kubeasz 版本号文本；不需要换版本时返回空串。
    副作用：只读数据库。

    ``/etc/kubeasz/bin`` 里的 k8s 二进制只由**本次使用的那套制品**决定：
    ``registry._carrier_plan`` 读制品里 ``ezdown`` 的 ``K8S_BIN_VER`` 去拉载体镜像
    （``easzlab/kubeasz-k8s-bin:<tag>``），再把里面的 kubelet、kube-apiserver 等铺进 bin/。
    升级任务的 kubeasz 版本默认继承「集群当前生效版本」，制品于是与现网同版本 ——
    而 kubeasz 的 ``93.upgrade.yml`` 开头就有一道守卫：控制机 bin 里的版本与集群在跑的
    版本相同时直接 ``UPDATE ABORT``。所以跨次版本必须连制品一起换；配置中心里本来就是
    这个配对关系（1.35 → kubeasz 3.6.10、1.36 → 3.7.0）。

    换 release 会重解压 ``/etc/kubeasz``，但只清源码与 ``bin/``、不碰 ``clusters/``
    （见 ``artifacts.ensure_kubeasz_source``），集群配置不会丢。
    """
    from . import upgrade_precheck

    target = upgrade_precheck.parse_version(options.get('kubernetes_version'))
    current = upgrade_precheck.parse_version(
        str(getattr(cluster, 'kube_version', '') or '') or options.get('kubernetes_version')
    )
    if target is None or current is None:
        return ''
    target_key = target[0] * 1000 + target[1]
    if target_key <= current[0] * 1000 + current[1]:
        return ''
    for row in _mapping_rows():
        if row['version_key'] == target_key:
            return row['kubeasz_version']
    return ''


def build_task(cluster, task_type, options, payloads, user=None):
    """写入一条运维任务及其节点快照。

    参数：`cluster` 表示 K8sCluster 实例；`task_type` 表示任务类型；`options` 表示公共字段；
        `payloads` 表示节点结构；`user` 表示提交人。
    返回：返回新建的 K8sDeployment。
    副作用：向任务表与节点快照表写入数据。

    任务的 ``slug`` 是**任务**标识，集群目录名走 ``cluster_slug`` 属性；
    这样同一个集群的第二次运维任务不会因为 slug 唯一约束而创建失败（规划 5.1）。
    """
    from k8s_deployment.serializers import derive_slug

    display_name = str(options.get('display_name') or cluster.name)
    label = dict(K8sDeployment.TASK_TYPES).get(task_type, task_type)
    # 只把真正的模型字段透传下去：options 里还带着 backup_id 这类只用于快照的键，
    # 直接展开会撞上 model 的构造签名；display_name 之类下面已经显式赋值的字段也要排除，
    # 否则同一个关键字会传两次。
    forwarded = {
        key: value for key, value in options.items()
        if key in TASK_FIELD_NAMES and key not in EXPLICIT_FIELD_NAMES
    }
    deployment = K8sDeployment.objects.create(
        organization=cluster.organization,
        cluster=cluster,
        task_type=task_type,
        status='queued',
        slug=derive_slug(f'{cluster.slug}-{task_type}', prefix='op'),
        display_name=f'{display_name} · {label}',
        expected_config_revision=register.current_revision(cluster),
        execution_mode=str(options.get('execution_mode') or 'native'),
        execution_mode_reason=str(options.get('execution_mode_reason') or ''),
        request_snapshot={
            'submitted': dict(options),
            'cluster_id': cluster.pk,
            'cluster_slug': cluster.slug,
            'submitted_at': timezone.now().isoformat(),
        },
        queued_at=timezone.now(),
        requested_by=getattr(user, 'username', '') or '',
        **forwarded,
    )
    for item in payloads:
        K8sDeploymentNode.objects.create(
            organization=cluster.organization,
            deployment=deployment,
            machine=item.get('machine'),
            name=item['name'],
            ip_address=item['ip_address'],
            ssh_port=item['ssh_port'],
            ssh_user=item['ssh_user'],
            roles=item['roles'],
            node_name=item.get('node_name') or '',
        )
    return deployment


def submit(cluster, task_type, payload=None, machines=None, user=None):
    """构造并提交一个运维任务。

    参数：`cluster` 表示 K8sCluster 实例；`task_type` 表示任务类型；`payload` 表示请求体；
        `machines` 表示新增机器列表（扩容任务使用）；`user` 表示提交人。
    返回：返回新建的 K8sDeployment。
    副作用：写入任务与节点快照；真正执行由 Worker 串行进行。
    """
    if task_type not in CLUSTER_TARGETED_TASKS:
        raise ClusterOpError(f'不支持对现有集群执行 {task_type} 操作。')

    assert_deployed(cluster)
    active = active_task_for(cluster)
    if active is not None:
        raise ClusterOpError(
            f'该集群当前有未完成的任务（#{active.pk} {active.get_task_type_display()}），'
            '请等它结束或先取消后再提交。'
        )

    payload = payload or {}
    if task_type in ('add_master', 'add_worker'):
        role = 'master' if task_type == 'add_master' else 'node'
        if task_type == 'add_master' and state_machine.cluster_master_count(cluster) < 3:
            raise ClusterOpError('该集群不是多节点集群（Master 少于 3 台），不支持添加 Master。')
        if not machines:
            raise ClusterOpError('请指定要添加的机器。')
        payloads = [new_machine_payload(item, role) for item in machines]
        existing = {item['ip_address'] for item in node_payloads(cluster)}
        duplicated = [item['ip_address'] for item in payloads if item['ip_address'] in existing]
        if duplicated:
            # 判据说明要带上：机器列表里那台机器可能显示成「未分配」（机器表没有归属），
            # 用户据此认为它是空闲的。真正的判据是地址 —— 集群按 IP 认节点，地址一样就是
            # 同一台，机器表里写没写归属不参与判断。
            raise ClusterOpError(
                f'以下机器已经在集群中：{"、".join(duplicated)}。'
                '集群按 IP 地址识别节点：地址与集群里某个节点相同就会被当作同一台，'
                '机器列表里显示的归属不作为判据。请换一台地址不同的机器，'
                '或先确认机器管理里的 IP 是否与部署时一致。'
            )
    else:
        if machines:
            raise ClusterOpError(f'{dict(K8sDeployment.TASK_TYPES).get(task_type, task_type)}不需要指定节点。')
        payloads = node_payloads(cluster)

    options = inherited_options(cluster, task_type, payload)
    _validate_options(task_type, options)
    if task_type == 'upgrade':
        _validate_upgrade_target(cluster, options)
    options['execution_mode'], options['execution_mode_reason'] = _execution_mode()
    return build_task(cluster, task_type, options, payloads, user=user)


def _execution_mode():
    """返回当前平台机的执行模式。

    参数：无。
    返回：返回 ``(mode, reason)``。
    副作用：只探测本机命令是否存在。
    """
    from k8s_deployment.serializers import detect_execution_mode

    return detect_execution_mode()


def _validate_options(task_type, options):
    """校验运维任务的关键字段。

    参数：`task_type` 表示任务类型；`options` 表示公共字段。
    返回：无显式返回值。
    副作用：只读入参。

    版本号缺失时直接拒绝：状态机在下载制品时会以 ``release_version_missing`` 失败，
    但那已经是任务开始之后的事，用户白等一轮。

    网段同理，但要求更硬：``PREFLIGHT_TASK_TYPES`` 里的任务（添加节点、升级）要跑预检，
    预检的「网段合法性」一读到空值就判失败 —— 而它们的表单里根本没有网段输入框，失败信息
    会指向一个用户填不了的地方（94 上「添加 Node」就是这样白等一轮）。宁可在提交这一刻说清楚。

    升级目标版本的合法性（是否与当前相同、是否跳级、是否在配置中心登记）不在这里判：
    它要读集群记录与配置中心，放在 ``_validate_upgrade_target`` 里，由 ``submit`` 调用。
    """
    if not options.get('kubeasz_version'):
        raise ClusterOpError(
            '无法确定集群当前生效的 kubeasz 版本（配置版本与任务记录里都没有），无法构造运维任务。'
        )
    if task_type in PREFLIGHT_TASK_TYPES:
        missing = [
            label for field, label in (('pod_cidr', '容器网络 CIDR'), ('service_cidr', 'Service 网段'))
            if not str(options.get(field) or '').strip()
        ]
        if missing:
            raise ClusterOpError(
                f'集群的配置版本里读不到{"、".join(missing)}。扩容与升级会沿用集群现有的网段，'
                '平台不会替它新编一个；请先确认该集群的配置版本里有完整的 hosts 记录。'
            )
    if task_type == 'restore':
        if not options.get('kubernetes_version'):
            raise ClusterOpError('恢复任务必须显式指定要恢复到的 Kubernetes 版本。')
        return
    if not options.get('kubernetes_version'):
        raise ClusterOpError('无法确定集群当前生效的 Kubernetes 版本，无法构造运维任务。')


def _validate_upgrade_target(cluster, options):
    """校验升级目标版本，把「白跑一轮」挡在提交这一刻。

    参数：`cluster` 表示 K8sCluster 实例；`options` 表示公共字段。
    返回：无显式返回值。
    副作用：只读数据库；目标不合法时抛 ``ClusterOpError``。

    只做提交期判得了的事；集群侧的门槛（节点版本 skew、目标制品是否真提供该版本、
    新模式的 nftables/ipvs 前提）留给执行前的预检。五条规则：

    - **目标与当前相同**：这正是「任务报成功、版本一点没变」那条路。以前执行期只发一条
      ``upgrade_noop`` 警告就放行，提交时不拦，用户等完一整轮才发现升了个寂寞。
    - **降级**：平台不支持，也不该给出这种入口。
    - **跨度大于一个次版本**：Kubernetes 不允许跳 minor，必须逐级升。
    - **目标不在启用的映射表里**：制品是按映射表同步的，选一个没登记的版本只会在
      下载阶段失败。
    - **代理模式**：要么沿用集群当前模式（目标 kubeasz 的模板必须支持它），要么显式给一个
      目标支持的模式 —— 跨次版本时 3.6.10 的 ipvs 不在 3.7.0 的模板里，不提就等着预检失败。
    """
    from . import preflight, upgrade_precheck

    target_text = str(options.get('kubernetes_version') or '').strip()
    current_text = str(getattr(cluster, 'kube_version', '') or '').strip()
    target = upgrade_precheck.parse_version(target_text)
    current = upgrade_precheck.parse_version(current_text)
    if target is None:
        raise ClusterOpError(
            f'无法解析升级目标版本「{target_text or "（空）"}」，请从平台给出的版本里选择。'
        )
    if current is None:
        raise ClusterOpError(
            f'无法解析集群当前版本「{current_text or "（空）"}」，平台无法判断它该升到哪个版本。'
        )

    target_key = target[0] * 1000 + target[1]
    current_key = current[0] * 1000 + current[1]
    if target < current:
        raise ClusterOpError(
            f'平台不支持降级：集群当前 {current_text}，目标 {target_text} 比它更低。请选择更高的目标版本。'
        )
    if target == current:
        raise ClusterOpError(
            f'目标版本与集群当前版本相同（{current_text}），这次升级不会改变任何东西。'
            '升级必须指定一个更高的目标版本：同一次版本内的补丁版本，或下一个次版本。'
        )
    if target_key - current_key > 1:
        raise ClusterOpError(
            f'一次只能升一个次版本：当前 {current_text} 到目标 {target_text} 跨了 '
            f'{target_key - current_key} 个次版本（Kubernetes 不允许跳 minor）。请先升级到中间的版本。'
        )
    if target_key not in {row['version_key'] for row in _mapping_rows()}:
        raise ClusterOpError(
            f'配置中心里没有登记 {target_text} 所在的次版本，无法升级到它。'
            '请先在 平台设置 → K8s 配置中心 登记该版本（含它的 kubeasz 版本）并同步制品后再升级。'
        )

    # 代理模式：目标 kubeasz 的模板只认它自己那一套（3.7.0 起是 iptables 与 nftables）。
    # 集群当前模式不被支持时必须显式改成支持的模式 —— 光靠预检拦是不够的，那是任务跑起来
    # 之后的事，用户等一轮才知道自己还得回去改一个运维表单里根本没有的选项。
    mode = str(options.get('kube_proxy_mode') or '').strip().lower()
    target_kubeasz = str(options.get('kubeasz_version') or '')
    supported, reason = preflight.kubeasz_supports_proxy_mode(target_kubeasz, mode)
    if not supported:
        modes, default_mode = preflight.kubeasz_proxy_modes(target_kubeasz)
        hint = (
            f'{reason}。集群当前用的是「{mode or "未知"}」模式，升级到 {target_text} 会连 kubeasz '
            f'一起换成 {target_kubeasz}，请改为该版本支持的模式（{"、".join(modes)}）后重新提交'
        )
        if default_mode:
            hint += f'（推荐 {default_mode}）'
        if 'nftables' in modes:
            hint += '。nftables 需要节点内核 ≥ 5.13 且装有 nft，平台会在预检里逐台核对。'
        raise ClusterOpError(hint)
    current_mode = register.cluster_proxy_mode(cluster)
    if mode and not current_mode:
        # 集群的配置版本里读不到 PROXY_MODE（人工改过 hosts）。改模式要改写那一行，
        # 改不动就得在提交时拒绝，而不是让任务带着新模式跑完、节点上却还是旧的。
        raise ClusterOpError(
            f'集群的配置版本里读不到它在用的 kube-proxy 模式（hosts 里没有 PROXY_MODE），'
            f'无法在升级时改成 {mode}。请先人工确认该集群的 hosts 配置。'
        )


def cluster_capabilities(cluster):
    """返回某个集群当前允许的运维操作，供前端决定按钮可用性。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'deployed': bool, 'reason': str, 'active_task': {...}|None, 'actions': {...}}``。
    副作用：读取数据库与配置，不修改数据。

    ``upgrade_targets`` 一并返回，前端的升级确认弹窗据此出「目标版本」下拉。放在这里而不是
    让前端去调部署向导的 ``/k8s/deploy-options/``：那个接口的权限码是
    ``page.k8s.deploy.view``，而运维抽屉要的是 ``page.k8s.clusters.upgrade`` ——
    有升级权限、没有部署权限的操作员会拿到一个空下拉。候选的算法只此一份，
    与 ``_validate_upgrade_target`` 同源。
    """
    from . import backup as backup_service

    deployed = getattr(cluster, 'origin', 'imported') == 'deployed'
    has_revision = register.current_revision(cluster) > 0
    # 已销毁的集群走「未部署」这条路：所有运维入口一次性关掉，前端现成的横幅与
    # clusterOpBlockedReason 兜底会自动把它显示成不可运维，不必为「已销毁」另加分支。
    destroyed = getattr(cluster, 'status', '') == 'destroyed'
    available = deployed and has_revision and not destroyed
    reason = ''
    if not deployed:
        reason = '该集群是导入的，平台不提供部署类运维操作。'
    elif destroyed:
        reason = '该集群已被平台清理，节点上不再有 Kubernetes 组件，请直接重新部署。'
    elif not has_revision:
        reason = '该集群没有配置版本记录，平台无法确定它的真实拓扑。'

    active = active_task_for(cluster) if available else None
    backup_ok = backup_service.backup_capability()
    return {
        'deployed': available,
        'reason': reason,
        # 单独暴露「已经清理过」这一个事实：已销毁的集群走的是「未部署」那条路，所有入口拿到
        # 的都是同一句 reason，而「清理集群」这个按钮自己需要一句回答它的问题的提示 ——
        # 否则一台清过的机器点清理，会被告诉「请直接重新部署」。
        'destroyed': destroyed,
        'active_task': (
            {
                'id': active.pk,
                'task_type': active.task_type,
                'display_name': active.get_task_type_display(),
                'status': active.status,
            }
            if active else None
        ),
        'actions': {
            'add_master': available and active is None and _master_count(cluster) >= 3,
            'add_worker': available and active is None,
            'upgrade': available and active is None and backup_ok['ok'],
            'backup': available and active is None and backup_ok['ok'],
            'restore': available and active is None and backup_ok['ok'],
            # 清理不需要备份能力，也不看备份是否就绪：它的目的是把这台机器清回能重装的状态，
            # 集群可能已经病到备份跑不动了，这时候更需要它。
            'destroy': available and active is None,
        },
        'backup_ready': backup_ok['ok'],
        'backup_hint': backup_ok['message'],
        'upgrade_requires_backup': bool(getattr(settings, 'K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP', True)),
        # 升级的目标版本候选（见本函数 docstring）。不可运维的集群仍然照算：前端那时
        # 显示的是「不支持运维」的横幅，多一组数据不会误导谁，反而少一个分支。
        'upgrade_targets': upgrade_target_options(cluster),
        # 集群当前已有的节点地址，「添加节点」的候选机器要用它排除。
        'node_addresses': _node_addresses(cluster, available),
    }


def _node_addresses(cluster, available):
    """返回集群当前已有的节点地址清单。

    参数：`cluster` 表示 K8sCluster 实例；`available` 表示该集群是否可运维。
    返回：返回地址字符串列表；取不到时返回空列表。
    副作用：读取数据库，不修改数据。

    「添加节点」的候选机器原来只按机器表的 ``cluster_name`` 过滤，那是一个投影，与集群
    真正装了什么会不一致 —— 94 上就出现过一台机器在向导里从来不是集群成员、却被列成
    候选（它的 ``cluster_name`` 是空的），用户选完提交，后端按快照判「已经在集群中」拒掉。
    让前端用**后端拒绝时用的同一份判据**（同一个 ``node_payloads``）来过滤，这种
    「列出来让用户选、选完又被拒」的自相矛盾才会消失。
    """
    if not available:
        return []
    try:
        return [str(item['ip_address']) for item in node_payloads(cluster)]
    except Exception:
        # 快照缺失或结构异常不该让整个运维抽屉打不开 —— 这一项只用于过滤候选机器，
        # 拿不到就退回「不过滤」，提交时后端仍然会拦。
        return []


def _master_count(cluster):
    """返回集群配置版本里的 Master 数量。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回数量，取不到时返回 0。
    副作用：读取数据库，不修改数据。
    """
    try:
        return state_machine.cluster_master_count(cluster)
    except Exception:
        return 0
