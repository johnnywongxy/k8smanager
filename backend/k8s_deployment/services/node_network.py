"""删掉集群在节点上留下的虚拟网卡，让这批机器能干净地重新部署。

kubeasz 的清理剧本（``roles/clean``）清的是**组件与数据**：服务、单元文件、``/etc/kubernetes``、
``/etc/cni``、etcd 与 containerd 的数据目录。它最后只打一句提示，让人自己重启机器：

.. code-block:: text

    [重要]: 请重启节点以确保清除系统残留的虚拟网卡、路由信息、iptalbes|ipvs规则等

这不是客套话。kube-proxy 的 ipvs 模式会建一张 ``kube-ipvs0`` 的 dummy 网卡，并把**每个
Service IP 都以 /32 挂在它上面**；nodelocaldns 会建 ``nodelocaldns`` 挂 169.254.20.10；
Calico 会留下 ``cali*`` 的 veth 对与 ``tunl0``。这些网卡在组件被删掉之后仍然留在内核里，
而它们的地址正好落在集群的 Service 网段里 —— 于是下一次部署的「网段合法性」预检会拿
Service 网段跟它自己比，报出一串必然成立的「重叠」而硬失败，现场看起来像是上次没清干净。

平台**不把重启甩给操作员**：这一批网卡的删除是确定性的、只涉及集群自己建的那几类接口名，
删掉它们也就一并带走了挂在这些接口上的路由。``cleanup`` 就是这一件事。

**刻意不碰 iptables。** kube-proxy 与网络插件在下次部署启动时会 ``-F`` 自己的链并重建，
留着旧规则不会被新集群继承；而在一台云主机上按前缀盲刷 iptables，随时可能把操作员自己的
安全策略（乃至 SSH 放行规则）一起刷掉。少做这一步的代价远小于做错的代价。

**刻意不碰 docker0 与 ``br-*``。** 目标机器上可能同时跑着与 k8s 无关的 docker，
删掉它们的网桥会把那部分容器网络连根拔起。

与 ``node_path`` / ``node_registry`` 同一种口吻：单台失败只记警告，不作为清理任务的成败判据 ——
残留一张网卡不影响重新部署（预检已经从判定里排除了它们），为它判失败会让操作员以为节点
什么都没清掉。
"""

from . import node_registry

# 要删的接口名模式。与 ``preflight`` 里那份「不算节点网段」的列表刻意**不是同一份**：
# 那边是「读取时忽略」，宁可多排除（含 docker0，它绝不该被算成节点所在网段）；这边是
# 「真的去删」，所以只列集群自己建的接口，把 docker0 与 br-* 留给机器上其它用途。
REMOVABLE_INTERFACE_PATTERNS = 'kube-*|nodelocaldns|cni[0-9]*|flannel*|tunl*|cali*|veth*'

# 命令末尾的回执：调用方据此判断是否还有残留，不必再开一次连接去核对。
CLEANUP_OK = 'K8SMANAGER_NET_CLEAN'
CLEANUP_PARTIAL = 'K8SMANAGER_NET_LEFT'
# 单条远程命令的超时秒数。删网卡本身是毫秒级的，给足余量是因为桥接口上挂着的 veth
# 要一个个解绑，机器很忙时排队时间比操作本身长得多。
COMMAND_TIMEOUT = 120


class NodeNetworkError(Exception):
    """节点上的虚拟网卡清不掉，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    与 ``NodePathError`` 同理，本模块不直接抛 ``state_machine.TaskAborted``：
    那个类在导入方向上属于上层，由调用方转换。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def cleanup_command():
    """生成清理节点虚拟网卡的命令文本。

    参数：无。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    先清 ipvs 转发表再删网卡：kube-proxy 的 ipvs 模式把每个 Service 写成一条虚拟服务，
    指向的正是 ``kube-ipvs0`` 上的地址，顺序反过来的话中途会有一批规则指着已经不存在的设备。

    命令**全程容错并继续**（``;`` 而不是 ``&&``）：它要在「集群装到一半」「上次清理崩在中间」
    「网卡被人手工删过」等所有状态下跑完 —— 这时候再因为某个前置状态不符而半途退出，
    只会把机器留在更难收拾的位置。

    末尾回显 ``REMOVED:<删掉的个数>`` 与 ``LEFT:<残留的接口名>``，并按是否还有残留打出
    两个回执标记之一。只看回执，调用方就能写出「清了几张、还剩什么」的日志。
    """
    return (
        # ipvsadm 是 ipvs 模式的伴生物，没装（iptables 模式或已经清过）就直接跳过。
        'command -v ipvsadm >/dev/null 2>&1 && ipvsadm -C 2>/dev/null; '
        'REMOVED=0; '
        f'for i in $(ls /sys/class/net 2>/dev/null); do case "$i" in {REMOVABLE_INTERFACE_PATTERNS}) '
        'ip link del "$i" 2>/dev/null && REMOVED=$((REMOVED+1));; '
        'esac; done; '
        'LEFT=""; '
        f'for i in $(ls /sys/class/net 2>/dev/null); do case "$i" in {REMOVABLE_INTERFACE_PATTERNS}) '
        'LEFT="$LEFT $i";; '
        'esac; done; '
        'echo "REMOVED:$REMOVED"; '
        'echo "LEFT:${LEFT# }"; '
        f'test -z "$LEFT" && echo {CLEANUP_OK} || echo {CLEANUP_PARTIAL}'
    )


def parse_result(output):
    """从命令回执里解析出清理结果。

    参数：`output` 表示命令的标准输出。
    返回：返回 ``{'removed': int, 'left': [接口名], 'clean': bool}``。
    副作用：只做纯计算。

    解析失败（命令被打断、输出被截断）时落回「什么都没删、也没有残留可报」：
    调用方据此走 warn 分支，但不会因为一个解析不出来而误报成失败。
    """
    removed = 0
    left = []
    for line in str(output or '').splitlines():
        text = line.strip()
        if text.startswith('REMOVED:'):
            try:
                removed = int(text.split(':', 1)[1].strip() or 0)
            except ValueError:
                removed = 0
        elif text.startswith('LEFT:'):
            left = [item for item in text.split(':', 1)[1].strip().split() if item]
    return {'removed': removed, 'left': left, 'clean': CLEANUP_PARTIAL not in str(output or '')}


def cleanup(context, node, credential=None, key_path=None, client_factory=None):
    """删掉一台节点上的集群虚拟网卡。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`credential` 表示连接信息；
        `key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``parse_result`` 的结果。
    副作用：在目标节点上删网卡、清 ipvs 转发表。

    幂等：网卡不在时 ``ip link del`` 失败即跳过，命令照样跑完并打出 CLEAN 回执。
    只有连不上节点才抛 ``NodeNetworkError``，由调用方决定是否容忍。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        code, out, err = node_registry.run(client, cleanup_command(), timeout=COMMAND_TIMEOUT)
    finally:
        node_registry.close_session(client)

    if code != 0:
        raise NodeNetworkError(
            'node_network_cleanup_failed',
            f'清理节点 {label} 上的集群虚拟网卡失败：{err or out or "退出码 " + str(code)}。',
        )

    result = parse_result(out)
    if result['left']:
        # 有残留就直说，并把名字点出来：这几张网卡是最容易在下次部署时冒出来咬人的那一类，
        # 操作员看到名字就知道该去查什么。
        context.log(
            'system',
            f'注意：节点 {label} 上仍有 {len(result["left"])} 张集群虚拟网卡没删掉'
            f'（{"、".join(result["left"])}），请手工执行 ip link del 删除后再重新部署。',
        )
        context.event(
            'node_network_leftover',
            f'节点 {label} 上仍有集群虚拟网卡未清理：{"、".join(result["left"])}。',
            level='warn',
            detail={'node': label, 'interfaces': result['left']},
        )
    elif result['removed']:
        context.log('system', f'节点 {label}：已删除 {result["removed"]} 张集群虚拟网卡（含 ipvs 转发表）')
    else:
        context.log('system', f'节点 {label}：没有发现集群遗留的虚拟网卡')
    return result
