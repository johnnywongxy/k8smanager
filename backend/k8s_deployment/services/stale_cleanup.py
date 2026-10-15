"""把「上一次安装」在节点上留下的集群痕迹清掉，让这批机器能干净地重新部署。

**这个模块为什么存在。** 94 上「111 / k8s-111」那次新建集群失败在 ``02.etcd``：机器上还留着
上一个集群（kubeasz 3.7.0 装的）的 ``/var/lib/etcd``，新集群的 etcd 以
``--initial-cluster-state=new`` 启动却被这份旧数据接管，读到比自己新的数据版本后
``fatal: invalid downgrade`` 直接退出 —— 界面上只有一句「部署 etcd：执行失败，退出码 1」。
预检的「上一次安装的残留」会先把这件事查出来（``preflight._check_stale_install``），
本模块负责在部署前把它清掉，让用户不必自己登机器。

**与「清理集群」（destroy）的区别。** 那条路跑 kubeasz 的 ``99.clean.yml``，前提是平台上
有一个 ``origin == 'deployed'`` 的集群记录、还得有配置版本可依 —— 而这里的场景恰恰是
「机器上有一套集群，平台上没有（或已经不是）对应记录」。所以这里不碰 kubeasz、不需要
inventory，直接在节点上执行确定性的清理命令。

**清理范围：只清 k8s 自己管的路径，保留容器运行时的镜像缓存。** 删 ``/var/lib/etcd``、
``/etc/kubernetes``、``/etc/etcd``、旧服务的单元文件、kubelet 目录与 CNI 配置；**不删**
``/var/lib/docker``、``/var/lib/containerd`` 与 ``/etc/containerd`` —— 镜像层留在机器上，
下一次部署推送镜像走「已存在」的快路径。同机上与 k8s 无关的 docker 容器也因此不受影响。
``/etc/hosts`` 同理不动：kubeasz 的 ``01.prepare`` 会重写它自己那块主机名解析。

**清理必须成对：删掉 kubeasz 的产物，也要删掉它「我来过」的标记。** 见 ``PREPARE_MARKER``：
``/root/.kube`` 与 ``/opt/kubeasz_prepare_tasks`` 是一个整体，只剩后者会让下一次
``01.prepare`` 整段跳过，节点上的 kubectl 配置再也拿不回来。节点的系统 PATH 那一半由
``node_path`` 在 ``01.prepare`` 之后补偿，kubeconfig 这一半由 ``node_kubeconfig`` 补偿 ——
两处都是「prepare 可能被跳过」的后果，各自兜各自的底。

**调用方只对预检命中的机器调用它**（``_check_stale_install`` 的逐节点明细），
所以「没有残留的机器」既不会连也不会跑这条命令。清完之后用**同一个探针**复验一遍，
仍有残留就报失败并点名机器与路径 —— 不能把问题留到 ``02.etcd`` 再让人猜。

与 ``node_network`` / ``node_registry`` 同一套口吻：幂等、全程 ``;`` 容错、靠回执判断结果。
那两块各管一摊（集群虚拟网卡、上一次部署留下的镜像仓库），本模块在每台机器上把它们也顺带
叫一遍 —— 三者都幂等，重复跑不会更糟。
"""

from . import node_network, node_registry, preflight

# 要停用并删除的 systemd 单元：kubeasz 装在节点上的全部组件。口径与 ``destroy_plan`` 里
# kubeasz ``roles/clean`` 的那份清单一致（clean_etcd / clean_master / clean_node / clean_lb）。
# 少停一个就会留一个占着端口的老服务：下一次部署的 02/04/05 阶段起不来，而现场只会看到
# 「6443 已被占用」这类警告。
UNIT_NAMES = (
    'etcd',
    'kube-apiserver', 'kube-controller-manager', 'kube-scheduler',
    'kubelet', 'kube-proxy', 'kube-lb',
    'l4lb', 'keepalived',
)

# 单元文件可能落在两处：kubeasz 自己写的是 /etc/systemd/system，发行版自带的在 /usr/lib。
UNIT_DIRS = ('/etc/systemd/system', '/usr/lib/systemd/system')

# kubeasz 的「prepare 已跑过」幂等标记。``roles/prepare/tasks/main.yml`` 一开头就 stat 它，
# 存在就把**整个 prepare 角色**跳过；写它的是同一个角色的最后一步。
#
# 它必须跟 ``/root/.kube`` 一起删，否则会留下一个**半干净的机器**：kubeasz 费心建出来的
# ``/root/.kube/config``（节点上的 kubectl 配置）被这个模块删掉，而标记还在 —— 紧接着的
# ``01.prepare`` 整段跳过，那份配置就再也回不来了。集群本身装得好好的（平台用自己的
# kubeconfig 做健康检查、查资源都不受影响），只有人登到 master 上敲 ``kubectl`` 时会连
# ``localhost:8080`` 报 connection refused —— 「部署显示成功、登机器却用不了」。
# 2026-10-12 的 JD-k8s（114.67.203.82）就是这么来的：清理回执里明写着删掉了 ``/root/.kube``，
# 而 ``01.prepare`` 的 PLAY RECAP 是 ``ok=3 changed=0 skipped=154``。
#
# 位置上紧跟 ``/root/.kube``：kubeasz 自己的清理清单（``roles/clean/tasks/clean_node.yml``）
# 也是把这两行连在一起列的（第 25、26 行），照抄才不会再次漏掉。
PREPARE_MARKER = '/opt/kubeasz_prepare_tasks'

# 要删的目录与文件。**刻意不含** /var/lib/docker、/var/lib/containerd 与 /etc/containerd：
# 那是容器运行时的镜像缓存，重装时还要用，删了就得把镜像重新拉一遍。
REMOVABLE_PATHS = (
    '/etc/etcd',
    '/etc/kubernetes',
    '/root/.kube',
    PREPARE_MARKER,
    '/var/lib/kubelet',
    '/var/run/kubernetes',
    '/run/kubernetes',
    '/backup/k8s',
    '/etc/cni',
    '/var/lib/cni',
    '/etc/crictl.yaml',
    '/etc/keepalived',
    '/etc/l4lb',
    '/usr/local/sbin/keepalived',
)

# 各网络插件在节点上留下的目录，口径与 ``destroy_plan.CNI_PATHS`` 一致。
CNI_PATHS = {
    'calico': ('/etc/calico', '/var/lib/calico', '/var/log/calico'),
    'flannel': ('/run/flannel',),
}

# 命令末尾的回执：调用方据此判断是否还有没删掉的路径。
CLEAN_OK = 'K8SMANAGER_STALE_CLEAN'
CLEAN_PARTIAL = 'K8SMANAGER_STALE_PARTIAL'
# 单条远程命令的超时秒数。删数据目录本身很快，给足余量是因为卸载挂载点、停服务在忙机器上
# 会排队。
COMMAND_TIMEOUT = 300


class StaleCleanupError(Exception):
    """节点上的残留清不掉，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    与 ``NodeNetworkError`` 同理，本模块不直接抛 ``state_machine.TaskAborted``：
    那个类在导入方向上属于上层，由调用方转换。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def removable_paths(deployment):
    """列出本次清理要删掉的路径。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回路径元组，第一个是 etcd 数据目录。
    副作用：纯计算。

    etcd 数据目录取 ``preflight.etcd_data_dir``（与预检探针、与 ``compiler`` 渲染 config.yml
    用的是同一个算式）；CNI 目录按集群选的插件取。
    """
    cni = str(getattr(deployment, 'cni_plugin', '') or '')
    return (
        preflight.etcd_data_dir(deployment),
        *REMOVABLE_PATHS,
        *CNI_PATHS.get(cni, ()),
    )


def cleanup_command(deployment):
    """生成清理节点集群残留的命令文本。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    顺序上先停服务再删文件：服务还在跑的时候删数据目录，进程会把文件重新写出来。

    命令**全程容错并继续**（``;`` 而不是 ``&&``）：它要在「集群装到一半」「上次清理崩在中间」
    「组件被人手工删过」等所有状态下跑完 —— 这时候再因为某个前置状态不符而半途退出，
    只会把机器留在更难收拾的位置。

    末尾回显 ``LEFT:<还有哪些路径没删掉>``，并按是否还有残留打出两个回执标记之一。
    """
    units = ' '.join(UNIT_NAMES)
    unit_files = ' '.join(f'{directory}/$u.service' for directory in UNIT_DIRS)
    paths = ' '.join(removable_paths(deployment))
    return (
        f'for u in {units}; do '
        'systemctl stop $u 2>/dev/null; '
        'systemctl disable $u 2>/dev/null; '
        f'rm -f {unit_files}; '
        'done; '
        'systemctl daemon-reload 2>/dev/null; '
        # kubelet 挂载的 pod 目录与 containerd 的 k8s 命名空间挂载不先卸载，下面的 rm -rf
        # 会「删掉挂载点里的内容却删不掉目录」，重启后旧数据又冒出来。
        'for m in $(awk \'$2 ~ "^/var/lib/kubelet" || $2 ~ "^/var/lib/containerd/io.containerd[.]" '
        '{print $2}\' /proc/mounts 2>/dev/null); do umount -f "$m" 2>/dev/null; done; '
        f'rm -rf {paths}; '
        'LEFT=""; '
        f'for p in {paths}; do [ -e "$p" ] && LEFT="$LEFT $p"; done; '
        'echo "LEFT:${LEFT# }"; '
        f'test -z "$LEFT" && echo {CLEAN_OK} || echo {CLEAN_PARTIAL}'
    )


def parse_result(output):
    """从命令回执里解析出清理结果。

    参数：`output` 表示命令的标准输出。
    返回：返回 ``{'left': [路径…], 'clean': bool}``。
    副作用：只做纯计算。

    输出被截断（两个标记都没读到）时 ``clean`` 为假：这是不可逆的删除动作，
    读不出来就该当成「没把握清干净」，由调用方报失败让人来看，而不是当成功放过去。
    """
    left = []
    for line in str(output or '').splitlines():
        text = line.strip()
        if text.startswith('LEFT:'):
            left = [item for item in text.split(':', 1)[1].split() if item]
    return {'left': left, 'clean': CLEAN_OK in str(output or '')}


def verify_command(deployment):
    """生成清理后复验用的命令。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回与预检完全相同的那条残留探针命令。
    副作用：只做纯计算。

    复验刻意用预检那一套探针而不是另写一份：判据只有一处定义，才不会出现「预检说有问题、
    清理后说没问题」这种两边口径不一致的情况。
    """
    return preflight.stale_probe_command(deployment)


def _clean_one(context, deployment, node, credential, client_factory=None):
    """清理一台节点上的集群残留，并复验。

    参数：`context` 表示任务上下文；`deployment` 表示任务；`node` 表示节点快照；
        `credential` 表示连接信息；`client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``{'label', 'left', 'removed_paths', 'warnings'}``。
    副作用：在目标节点上停服务、删目录与文件，并另开连接清理虚拟网卡与镜像仓库。

    只有「连不上」「清理命令非零退出」「复验仍能探到残留」算失败并向上抛；
    虚拟网卡与镜像仓库那两步的异常只记警告 —— 它们没清掉不影响重新部署（预检的网段判定
    已经排除集群虚拟网卡，镜像仓库下一次部署会重新装），为它们判失败会让操作员以为
    整台机器都没清干净。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    paths = removable_paths(deployment)
    client = node_registry.open_session(node, credential, client_factory=client_factory)
    try:
        code, out, err = node_registry.run(client, cleanup_command(deployment), timeout=COMMAND_TIMEOUT)
        if code != 0:
            raise StaleCleanupError(
                'stale_cleanup_failed',
                f'清理节点 {label} 上的集群残留失败：{err or out or "退出码 " + str(code)}。',
            )
        result = parse_result(out)
        probe_code, probe_out, probe_err = node_registry.run(client, verify_command(deployment))
        if probe_code != 0:
            raise StaleCleanupError(
                'stale_cleanup_failed',
                f'节点 {label} 清理后无法复验（读取安装痕迹失败）：{probe_err or probe_out or "无输出"}。',
            )
        leftover = preflight.parse_stale_probe(probe_out)
    finally:
        node_registry.close_session(client)

    removed = [path for path in paths if path not in result['left']]
    # 复验探到的三处痕迹单列出来：它们就是「部署一定会失败」的那几个原因，比一般路径严重。
    left = list(result['left'])
    for path in leftover:
        if path not in left:
            left.append(path)
    report = {'label': label, 'left': left, 'removed_paths': removed, 'warnings': []}

    context.log(
        'system',
        f'节点 {label}：已清理 {len(removed)} 处集群残留（'
        + '、'.join(removed[:6])
        + ('…' if len(removed) > 6 else '')
        + '）',
    )
    context.event(
        'stale_cleanup',
        f'节点 {label}：已清理上一次安装留下的集群残留（{len(removed)} 处）',
        detail={'node': label, 'removed': removed, 'left': left},
    )

    if left:
        context.log('system', f'节点 {label}：仍有残留未清掉 —— {"、".join(left)}')
        context.event(
            'stale_cleanup_leftover',
            f'节点 {label} 上仍有集群残留未清掉：{"、".join(left)}。',
            level='warn',
            detail={'node': label, 'left': left},
        )

    report['warnings'] = _release_node_extras(context, node, credential, client_factory=client_factory)
    return report


def _release_node_extras(context, node, credential, client_factory=None):
    """顺带清掉上一次部署在这台机器上留下的虚拟网卡与镜像仓库。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`credential` 表示连接信息；
        `client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回中文警告列表（清不掉的项）。
    副作用：删除节点上的集群虚拟网卡、ipvs 转发表与镜像仓库的单元/二进制。

    两条都只在「机器上有上一次安装的残留」时才被调到，且都是幂等的。
    失败只记警告，见 ``_clean_one`` 的说明。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    warnings = []
    for action, target in (
        (node_network.cleanup, '集群虚拟网卡'),
        (node_registry.uninstall, '上一次部署留下的镜像仓库'),
    ):
        try:
            action(context, node, credential, client_factory=client_factory)
        except Exception as exc:  # noqa: BLE001 - 清不掉只记警告，不因此判整台机器失败
            message = f'节点 {label} 上的{target}未能清理：{exc}'
            warnings.append(message)
            context.log('system', f'注意：{message}')
            context.event('stale_cleanup_leftover', message, level='warn', detail={'node': label})
    return warnings


def run_stale_cleanup(context, deployment, nodes, credentials_map, targets, client_factory=None):
    """在命中残留的机器上执行清理。

    参数：`context` 表示任务上下文；`deployment` 表示任务；`nodes` 表示节点快照列表；
        `credentials_map` 表示 ``{ip: 连接信息}``；`targets` 表示要清理的节点 IP 列表；
        `client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``_clean_one`` 的报告列表（清理失败时抛异常，不返回）。
    副作用：在目标节点上执行删除动作。

    逐台清理而不是一次性并行：日志要能按机器读，而且一台失败不该影响其它机器的收尾 ——
    先把所有能清的清掉，再把失败集中报出来，操作员一次就能看到全部要处理的地方。
    """
    by_ip = {str(getattr(node, 'ip_address', '') or ''): node for node in nodes}
    failures = []
    reports = []
    for ip in targets:
        node = by_ip.get(str(ip))
        if node is None:
            # 预检用的是同一批节点，正常走不到这里；真到了说明任务参数被改过，不能装作清理过。
            failures.append(f'节点 {ip} 不在本次任务的节点清单里，无法清理。')
            continue
        try:
            report = _clean_one(
                context, deployment, node, (credentials_map or {}).get(str(ip)),
                client_factory=client_factory,
            )
        except StaleCleanupError as exc:
            failures.append(exc.message)
            continue
        reports.append(report)
        if report['left']:
            failures.append(f'节点 {ip} 清理后仍有残留：{"、".join(report["left"])}。')
    if failures:
        raise StaleCleanupError(
            'stale_cleanup_failed',
            '；'.join(failures)
            + '请手工处理这些路径后重新提交（不确定是否可以删除时，先确认这台机器上没有要保留的数据）。',
        )
    return reports
