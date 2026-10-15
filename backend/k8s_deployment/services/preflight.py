"""部署前的目标节点环境预检。

预检在真正调用 kubeasz 之前跑一遍：连上每一台目标节点，采集只读信息，把
「这批机器现在能不能拿来部署」的判断集中在这里，避免部署跑到一半才因为系统不匹配、
端口被占用、磁盘不够之类的低级问题失败，把机器留在半成品状态。

设计约定：
- 所有 SSH 连接与命令都有超时，任何一台节点的问题只记进对应检查项的 ``detail``，
  不中断整个预检；单台机器的失败也不影响其它节点的检查结论。
- 主机密钥策略刻意复用 ``ops.services.system_tools`` 的实现，全平台只保留一份，
  避免预检自己再写一套把未知主机密钥静默落盘的策略。
- 模块导入阶段不连数据库、不连 SSH、不读写文件，所有外部交互都发生在函数体内。
"""

import ipaddress
import re
import shutil
import time

try:  # pragma: no cover - 仅用于在缺少依赖时给出可读提示，正常环境必然导入成功
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None

# 检查项顺序，前端按这个顺序展示，run_preflight 也按这个顺序产出结果。
CHECKS_ORDER = [
    'ssh',
    'privilege',
    'os_release',
    'disk',
    'kernel',
    'stale_install',
    'ports',
    'clock',
    'network',
    'etcd_network',
    'cidr',
    'external_lb',
    'etcd_quorum',
    'artifact',
]

# 各检查项的中文标题，供安全兜底包装器在异常时仍能拼出可展示的结果。
CHECK_TITLES = {
    'ssh': 'SSH 连通性',
    'privilege': '登录权限',
    'os_release': '操作系统匹配',
    'disk': '磁盘与 inode',
    'kernel': '内核与 ipvs',
    'stale_install': '上一次安装的残留',
    'ports': '端口占用',
    'clock': '时钟同步',
    'network': '网卡与内网互通',
    'etcd_network': 'etcd 内网互通',
    'cidr': '网段合法性',
    'external_lb': '外部负载均衡',
    'etcd_quorum': 'etcd 仲裁',
    'artifact': '制品能力',
}

# 连接 10 秒、命令 30 秒：预检是交互式操作，宁可判定为失败也不能把请求挂死。
SSH_CONNECT_TIMEOUT_SECONDS = 10
SSH_COMMAND_TIMEOUT_SECONDS = 30

# ``ip -o -f inet addr show`` 的一行：``3: kube-ipvs0    inet 10.254.1.109/32 scope global ...``。
# 接口名要单独抓出来，网段合法性检查得按它把虚拟网卡排除掉（见 ``_node_prefixes``）。
VIRTUAL_INTERFACE_LINE = re.compile(
    r'^\s*\d+:\s+(?P<name>\S+)\s+inet\s+(?P<cidr>\d+\.\d+\.\d+\.\d+/\d+)',
    re.MULTILINE,
)

# 容器运行时与 Kubernetes 自己创建的虚拟网卡。它们上面的地址是集群内部实现细节，不是
# 「节点所在网段」，拿来跟 Pod/Service 网段比对只会得到必然成立的「重叠」。
_VIRTUAL_INTERFACE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r'kube-',          # kube-ipvs0：kube-proxy 的 ipvs 模式为每个 Service IP 挂一个 /32
        r'nodelocaldns',   # nodelocaldns：169.254.20.10/32
        r'cali',           # Calico 的 veth 对
        r'tunl',           # Calico 的 IPIP 隧道
        r'flannel',        # flannel.1
        r'cni\d',          # cni0
        r'docker\d',       # docker0
        r'br-',            # docker 自定义网桥
        r'veth',           # 容器 veth
        r'dummy\d',        # dummy 类型网卡
        r'virbr\d',        # libvirt
        r'lxcbr\d',        # LXC
    )
)

# 目标节点可用空间硬阈值（GB），按角色取最严的一档（规划 9）。etcd/control-plane 要落
# etcd 的 WAL 与快照、还要放宽镜像，worker 只跑容器运行时，所以两档要求不同。
DISK_MIN_FREE_GB_CONTROL_PLANE = 40
DISK_MIN_FREE_GB_WORKER = 30
# 平台机制品缓存所在盘的可用空间阈值（GB）。缓存盘写满会让下载阶段失败，所以这里只给警告：
# 目标节点的磁盘不够是「装不上」，缓存盘不够是「清一下就好」，不该把整个预检判死。
PLATFORM_CACHE_MIN_FREE_GB = 20
# inode 空闲数量低于该值、或使用率高于该比例时给警告（不阻断）。
INODE_MIN_FREE = 10000
INODE_USED_WARN_PERCENT = 80
# 节点与平台机的时间偏差超过该秒数给警告（不阻断，但会导致证书校验与 etcd 选主异常）。
CLOCK_MAX_DRIFT_SECONDS = 2
# 单次 TCP 探测的等待秒数。
TCP_PROBE_TIMEOUT_SECONDS = 3

# 负载均衡机器在节点快照里的角色名，与 ``serializers.VALID_ROLES`` 里的那一项一致。
LB_ROLE = 'loadbalancer'
# 外部负载均衡面向多节点集群：只有一台 apiserver 时 VIP 后面没有可切换的后端。
# 阈值与 ``serializers._validate_roles`` 的「多节点至少 3 台 Master」同一口径。
LB_MIN_MASTERS = 3
# 新建集群类的任务：它们的节点清单就是整个集群，控制面数量与入口地址必须当场判。
# 与 ``runner.DEPLOY_TASK_TYPES`` 是同一组值；本模块刻意不 import 项目内的模块
# （见文件头「模块导入阶段不连数据库」那条约定），所以在这里各自声明一份。
DEPLOY_TASK_TYPES = ('deploy_aio', 'deploy_ha')

# ipvs 代理模式所需的全部内核模块（规划 9）。只查 ip_vs 是不够的：调度算法（rr/wrr/sh）
# 各自是独立模块，缺一个 kube-proxy 在 ipvs 模式下就起不来，而报错要到部署中段才出现。
IPVS_REQUIRED_MODULES = ('ip_vs', 'ip_vs_rr', 'ip_vs_wrr', 'ip_vs_sh', 'nf_conntrack')
# 往集群里加机器的任务：``ezctl add-master`` / ``add-node`` 的剧本里没有 etcd 角色，
# 新机器上不会安装 etcd（加 etcd 成员是独立操作 ``ezctl add-etcd``，平台不调用）。
NON_ETCD_TASK_TYPES = ('add_master', 'add_worker')
# etcd 明文与对等通信端口，只有 etcd 角色的节点需要为空。
ETCD_PORTS = (2379, 2380)
# kubelet 端口，所有节点都要为空。
KUBELET_PORT = 10250
# etcd 客户端端口，仲裁检查从这里进。
ETCD_CLIENT_PORT = 2379
# etcd 对等端口。
ETCD_PEER_PORT = 2380
# etcd 节点之间必须互通：集群内部说的「内网互通」有一大半就是指这两对端口。
# 数值取自 ``ETCD_PORTS``，单列出来是为了让「探测哪个端口」这件事在代码里读得出来。
ETCD_REACH_PORTS = (ETCD_CLIENT_PORT, ETCD_PEER_PORT)

# 区分「连接被拒绝」与「包被丢弃」的耗时分界（毫秒）。
#
# 预检要在 etcd 还没安装时回答一句「这两个端口将来通不通」，而 ``nc``/``/dev/tcp`` 只能给出
# 成功或失败，说不清是哪种失败。差别恰恰是全部意义所在：
# - **被拒绝**（对端回 RST）：包一来一回都走通了，只是那个端口上暂时没有进程在听 —— 这是
#   部署前的正常状态，安全组放行了，集群装起来就能通。
# - **被丢弃**（对端或中间设备把包扔掉）：连不上，而且**装完 etcd 一样连不上** —— 云安全组、
#   VPC 网络 ACL 或主机防火墙挡着。这件事必须在部署前拦下来。
# 两者靠**建连耗时**分开：RST 是立刻回来的，被丢弃则要一直等到探测自己超时。
#
# 取 1200ms：云内网的 RST 通常个位数毫秒，留两个数量级的余量；而探测超时是 3 秒，
# 只要没到 1200ms 就不可能是在等超时。跨地域的超长 RTT 理论上会误判成「被丢弃」，
# 那种情况下报出来的结论是「去检查安全组」—— 提示本身仍然是对的。
TCP_DROPPED_THRESHOLD_MS = 1200
# 单次 etcd 端口探测的等待秒数。比 SSH 端口那次（``TCP_PROBE_TIMEOUT_SECONDS``）短一点：
# 一台节点上要连着探好几个地址，被丢弃时每个都要等满，节点多了会拖长整个预检。
ETCD_PROBE_TIMEOUT_SECONDS = 3

# 端口 → 在本平台上会监听它的 systemd 单元。端口被占时靠它认领占用者：单元文件在那台
# 机器上、服务又正在跑，说明占端口的就是本平台上一次装下的组件，而不是别人在抢。
# ApiServer 端口是用户可改的，不在表里，见 ``_port_owner_units``。
PORT_OWNER_UNITS = {
    ETCD_CLIENT_PORT: ('etcd',),
    ETCD_PEER_PORT: ('etcd',),
    KUBELET_PORT: ('kubelet',),
}
# ApiServer 端口按角色有三个可能的持有者：控制面节点上是 kube-apiserver，任意节点上都还有
# 本机代理 kube-lb（监听 127.0.0.1），负载均衡机器上是 l4lb（nginx）。三个都算候选。
API_PORT_OWNER_UNITS = ('kube-apiserver', 'kube-lb', 'l4lb')

# 本平台（kubeasz）在节点上装下的组件与它们留下的文件。名字取自各 role 的
# ``dest=/etc/systemd/system/<名字>.service``，路径取自 ``roles/ex-lb`` 的
# ``dest=/etc/keepalived/keepalived.conf``，都是 kubeasz 的约定，不是平台自己的选择。
SELF_INSTALL_UNITS = ('etcd', 'kube-apiserver', 'kube-lb', 'kubelet', 'kube-proxy', 'l4lb', 'keepalived')
SELF_INSTALL_UNIT_DIR = '/etc/systemd/system'
KEEPALIVED_CONF = '/etc/keepalived/keepalived.conf'
# 探针输出的三段边界标记：命令按固定顺序打出来，读回来才能确认自己读全了。
SELF_INSTALL_PROBE_BEGIN = '__SELF_INSTALL_PROBE_BEGIN__'
KEEPALIVED_CONF_BEGIN = '__KEEPALIVED_CONF_BEGIN__'
KEEPALIVED_CONF_END = '__KEEPALIVED_CONF_END__'

# 「上一次安装的残留」检查项的三个判据（路径 + 中文说明）：
# - etcd 的数据目录里的 ``member`` 子目录：kubeasz 的 02.etcd 只建数据目录本身，``member``
#   由 etcd 首次启动时创建，所以它非空就等于「这台机器上真的跑过 etcd」；
# - kubeasz 生成的集群证书目录（etcd、apiserver、kubelet 共用这一份）；
# - 外部负载均衡的 keepalived 配置（kubeasz 的 ex-lb 角色写死这个路径）。
# 都是 kubeasz 的约定路径，不是平台自己的选择。
DEFAULT_ETCD_DATA_DIR = '/var/lib/etcd'
STALE_PROBE_BEGIN = '__STALE_PROBE_BEGIN__'
STALE_PROBE_END = '__STALE_PROBE_END__'
# 「集群已经装出来了」的判据：这些 kubeasz 阶段跑完任何一个，机器上就已经有这套集群的组件
# 与数据了。命中残留时用它决定敢不敢清 —— 清一台已经装好的机器，等于把可用的集群推倒重装。
INSTALLED_STAGE_KEYS = (
    '02.etcd', '03.runtime', '04.kube-master', '05.kube-node', '06.network', '07.addons', '10.ex-lb',
)

# etcd 的证书目录与 etcdctl 候选路径（kubeasz 把二进制放在 /opt/kube/bin）。
# 这两处都是 kubeasz 的约定路径，不是平台自己的选择。
ETCD_SSL_DIR = '/etc/kubernetes/ssl'
ETCDCTL_CANDIDATES = ('/opt/kube/bin/etcdctl', '/usr/local/bin/etcdctl', '/usr/bin/etcdctl')
# 单次 etcdctl 查询自身的超时：仲裁检查要快，卡住的检查项比没有检查项更糟。
ETCD_QUERY_TIMEOUT = '5s'

# kube-proxy 模式与 kubeasz 版本的对照表，按版本从高到低排列，取第一项命中的。
#
# 上游的集群模板（``example/hosts.multi-node`` 的 ``PROXY_MODE``）每个大版本都只列两种模式：
# 3.6.x 写的是 ``'iptables' or 'ipvs'``，3.7.0 起改成 ``'iptables' or 'nftables'`` 且注明
# 需要 kernel >= 5.13 与 nft >= 1.0.0（见 kubeasz 3.7.0 发布说明）。
# 元组里的第一项是**平台推荐的默认值**，也是升级要改模式时给用户的推荐值。3.7.0 那一行
# 特意没有照抄上游模板的默认（nftables）：nftables 要求节点内核 >= 5.13，平台的机器未必
# 满足，而默认值恰恰是用户不会去多想的那一个 —— 默认落到一个可能被预检拦下的值上，
# 等于让每台内核不达标的机器都要先失败一次才知道该改。iptables 在所有版本上都受支持，
# nftables 留给内核达标的人主动选（选了之后 ``_check_nftables`` 会逐台核对）。
#
# 注意 nftables 是「模板不给」而不是「引擎不认」：k8s v1.36 的 kube-proxy 仍实现 ipvs
# （``--proxy-mode`` 的帮助文本与 ``pkg/proxy/ipvs`` 都还在）。平台按模板口径拦，
# 超出的部分不猜；这也意味着老 ipvs 集群跨次版本升到 1.36 时必须显式改模式。
PROXY_MODE_SUPPORT = (
    ((3, 7), ('iptables', 'nftables'), 'iptables'),
    ((0, 0), ('ipvs', 'iptables'), 'ipvs'),
)

# nftables 模式的内核前提（kubeasz 3.7.0 发布说明原话：kernal >= 5.13 and nft >= 1.0.0）。
NFTABLES_MIN_KERNEL = (5, 13)
NFTABLES_MIN_VERSION = (1, 0, 0)

# 自定义数据目录的白名单：绝对路径，每段只含字母、数字与 _ . -。该路径会被拼进远程
# shell 命令，因此在这里卡死，不允许出现引号、分号、空格等任何可用于拼接命令的字符。
DATA_DIR_PATTERN = re.compile(r'^/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$')

# 实机 /etc/os-release 的 ID 到配置中心展示名称的映射，比对时双向使用。
OS_ID_TO_NAME = {
    'ubuntu': 'Ubuntu',
    'rocky': 'Rocky Linux',
    'almalinux': 'AlmaLinux',
    'centos': 'CentOS',
    'debian': 'Debian',
    'fedora': 'Fedora',
    'opensuse-leap': 'openSUSE Leap',
    'opensuse': 'openSUSE',
    'openeuler': 'openEuler',
    'kylin': 'Kylin',
    'uos': 'UOS',
}


class PreflightError(Exception):
    """预检自身无法进行（例如没有目标节点、目标节点缺少 IP）。"""


def _normalize_token(value):
    """把系统名称或 ID 归一化成只含小写字母与数字的比较键。

    参数：`value` 表示系统 ID 或展示名称，例如 ``Rocky Linux``、``opensuse-leap``。
    返回：返回去掉一切分隔符并转小写后的字符串。
    副作用：纯计算，不修改持久化数据。
    """
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _build_os_alias_index():
    """构建「系统别名 -> 规范 ID」的查表。

    参数：无。
    返回：返回以归一化别名为键、以 ``OS_ID_TO_NAME`` 的 ID 为值的字典。
    副作用：纯计算，在模块导入时执行一次，不访问外部资源。
    """
    index = {}
    for os_id, os_name in OS_ID_TO_NAME.items():
        # ID 与展示名称都要能反查，配置中心既可能存 "rocky" 也可能存 "Rocky Linux"。
        for alias in (os_id, os_name, os_name.replace(' ', '')):
            index[_normalize_token(alias)] = os_id
    return index


# 别名索引是纯计算结果，放在模块级只算一次，避免每次预检重复构建。
OS_ALIAS_INDEX = _build_os_alias_index()


def _major_version(value):
    """提取版本号中的主版本部分。

    参数：`value` 表示版本文本，例如 ``8.8``、``22.04``、``v3.7.0``。
    返回：返回主版本号字符串；无法识别时返回空串。
    副作用：纯计算，不修改持久化数据。
    """
    match = re.match(r'\s*[vV]?(\d+)', str(value or ''))
    return match.group(1) if match else ''


def _version_tuple(value):
    """把版本号解析成可比较的主次版本元组。

    参数：`value` 表示版本文本，例如 ``3.7.0``。
    返回：返回 ``(主版本, 次版本)``；解析不出来时返回 None。
    副作用：纯计算，不修改持久化数据。
    """
    match = re.match(r'\s*[vV]?(\d+)(?:\.(\d+))?', str(value or ''))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0))


def parse_os_release(text):
    """解析 ``/etc/os-release`` 文本。

    参数：`text` 表示 ``/etc/os-release`` 的原始内容，允许带引号、注释与 ``export`` 前缀。
    返回：返回 ``{'id': .., 'version_id': .., 'pretty_name': ..}``，缺失的字段为空串。
    副作用：纯计算，不访问目标机器。
    """
    result = {'id': '', 'version_id': '', 'pretty_name': ''}
    for raw_line in str(text or '').splitlines():
        line = raw_line.strip()
        # 空行与注释直接跳过；不带等号的行不是合法的键值对。
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _separator, value = line.partition('=')
        key = key.strip().lower()
        if key.startswith('export '):
            # 少数裁剪过的镜像会把 os-release 写成 shell 片段，这里顺手兼容。
            key = key[len('export '):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key in result:
            result[key] = value.strip()
    return result


def match_supported_system(os_release_info, expected_os_type, expected_os_version):
    """判断实机操作系统与配置中心所选系统是否匹配。

    比对分两步：先用 ``ID`` 映射到配置中心的展示名称确认系统大类相同，
    再按主版本号前缀归一化比对版本（实机 ``8.8`` 与配置中心 ``8`` 视为同一主版本）。

    参数：`os_release_info` 表示 ``parse_os_release`` 的解析结果；
        `expected_os_type` 表示配置中心记录的系统类型，例如 ``Rocky Linux`` 或 ``rocky``；
        `expected_os_version` 表示配置中心记录的版本，例如 ``8``。
    返回：返回 ``(是否匹配, 中文原因)``；原因无论成败都可直接展示给用户。
    副作用：纯计算，不访问目标机器。
    """
    info = os_release_info or {}
    actual_id = str(info.get('id') or '').strip()
    actual_version = str(info.get('version_id') or '').strip()
    actual_name = OS_ID_TO_NAME.get(OS_ALIAS_INDEX.get(_normalize_token(actual_id), ''), '') or actual_id

    expected_key = OS_ALIAS_INDEX.get(_normalize_token(expected_os_type))
    if not expected_key:
        # 配置中心选了平台映射表以外的系统时给出可读原因，而不是抛异常。
        return False, (
            f'配置中心登记的操作系统类型「{expected_os_type}」不在平台已知的系统映射表内，'
            f'无法与实机比对，已知系统为：{"、".join(sorted(OS_ID_TO_NAME.values()))}'
        )
    expected_name = OS_ID_TO_NAME[expected_key]

    if not actual_id:
        return False, '实机 /etc/os-release 未提供 ID 字段，无法确认操作系统类型'
    actual_key = OS_ALIAS_INDEX.get(_normalize_token(actual_id))
    if not actual_key:
        return False, (
            f'实机 /etc/os-release 的 ID 为「{actual_id}」，不在平台已知的系统映射表内，'
            f'无法确认是否为 {expected_name}'
        )
    if actual_key != expected_key:
        return False, f'实机系统为 {actual_name}，与配置中心选择的 {expected_name} 不一致'

    actual_major = _major_version(actual_version)
    expected_major = _major_version(expected_os_version)
    if not actual_major:
        return False, f'实机 {actual_name} 未提供可识别的 VERSION_ID，无法确认版本是否为 {expected_os_version}'
    if not expected_major:
        # 配置中心没填版本号时只比系统大类，避免把合法部署挡在门外。
        return True, f'实机系统为 {actual_name} {actual_version}，与配置中心选择的系统大类一致'
    if actual_major != expected_major:
        return False, (
            f'实机系统为 {actual_name} {actual_version}（主版本 {actual_major}），'
            f'与配置中心选择的 {expected_name} {expected_os_version}（主版本 {expected_major}）不一致'
        )
    return True, f'实机系统为 {actual_name} {actual_version}，与配置中心选择的主版本一致'


def kubeasz_proxy_modes(kubeasz_version):
    """返回某个 kubeasz 版本支持的 kube-proxy 模式与它的模板默认值。

    参数：`kubeasz_version` 表示 kubeasz 版本号文本，例如 ``3.7.0``。
    返回：返回 ``(模式元组, 默认模式)``；版本号解析不出来时返回 ``(全部模式, '')``。
    副作用：纯计算，不修改持久化数据。

    版本号解析不出来时不下结论而是放开全部模式：这一层的结论是硬失败，凭空把一条命令
    拦掉比漏过去的代价大得多，真正的把关留给下面的内核检查与 kubeasz 自己。
    """
    def all_modes():
        """汇总表里出现过的全部模式，按首次出现的顺序去重。

        参数：无。
        返回：返回模式元组。
        副作用：纯计算，不修改持久化数据。
        """
        seen = []
        for _floor, modes, _default in PROXY_MODE_SUPPORT:
            for mode in modes:
                if mode not in seen:
                    seen.append(mode)
        return tuple(seen)

    parsed = _version_tuple(kubeasz_version)
    if parsed is None:
        return all_modes(), ''
    for floor, modes, default in PROXY_MODE_SUPPORT:
        if parsed >= floor:
            return modes, default
    return all_modes(), ''


def kubeasz_supports_proxy_mode(kubeasz_version, kube_proxy_mode):
    """判断所选代理模式是否被该 kubeasz 版本的集群模板支持。

    参数：`kubeasz_version` 表示固化在部署任务上的 kubeasz 版本号；
        `kube_proxy_mode` 表示 kube-proxy 模式。
    返回：返回 ``(是否支持, 中文说明)``；版本号无法解析时按支持处理并在说明里注明。
    副作用：纯计算，不访问目标机器。

    两个方向都要拦：kubeasz 3.7.0 起模板不再支持 ipvs，而 3.6.x 的模板里根本没有
    nftables —— 只拦 ipvs 的话，一个 1.33 的集群会带着 nftables 一路走到 kubeasz
    自己的配置模板里，报出来的是一句与「版本不支持」无关的渲染错误。
    """
    mode = str(kube_proxy_mode or '').strip().lower()
    modes, _default = kubeasz_proxy_modes(kubeasz_version)
    if not mode:
        # 运维任务的模式取自集群的配置版本，读不到时是空值而不是某个默认值。凭空挑一种模式
        # 判支持与否，等于用一个平台编出来的值去拦用户，宁可跳过（新建部署的向导一定会填）。
        return True, '本次任务没有确定 kube-proxy 模式，跳过版本与代理模式的组合校验'
    if mode in modes:
        return True, f'kubeasz {kubeasz_version} 的集群模板支持 {mode} 代理模式'
    if _version_tuple(kubeasz_version) is None:
        # 版本号缺失或格式异常时不下结论，交给后续的内核检查兜底。
        return True, f'无法解析 kubeasz 版本「{kubeasz_version}」，跳过版本与代理模式的组合校验'
    return (
        False,
        f'kubeasz {kubeasz_version} 的集群模板只支持 {"、".join(modes)}，不支持 {mode or "未设置"}',
    )


def os_token_for(os_type, os_version):
    """把配置中心的系统类型与版本归一化成制品 token。

    参数：`os_type` 表示操作系统类型，例如 ``Rocky Linux`` 或 ``rocky``；
        `os_version` 表示操作系统版本，例如 ``8``。
    返回：返回形如 ``rocky_8``、``opensuse_leap_15`` 的 token；无法归一化时返回空串。
    副作用：纯计算，不访问外部资源。
    """
    canonical_id = OS_ALIAS_INDEX.get(_normalize_token(os_type))
    if not canonical_id:
        return ''
    major = _major_version(os_version)
    if not major:
        return ''
    # 制品包的命名约定是「ID 去掉连字符换成下划线 + 主版本号」，与 OSS 上的对象名一致。
    return f'{canonical_id.replace("-", "_")}_{major}'


def _build_host_key_policy():
    """构造只接受本次握手主机密钥、不落盘 known_hosts 的策略实例。

    参数：无。
    返回：返回 paramiko 的缺失主机密钥策略实例。
    副作用：导入 ``ops.services.system_tools``；不写入任何本地文件。
    """
    # 刻意复用系统工具里的策略实现：全平台只保留一份主机密钥策略，
    # 既避免重复实现，也避免预检这边被顺手改成 AutoAddPolicy 而静默信任未知主机。
    from ops.services.system_tools import _ssh_host_key_policy

    return _ssh_host_key_policy()


def _create_ssh_client():
    """创建一个预检专用的 paramiko SSH 客户端。

    参数：无。
    返回：返回已设置主机密钥策略、尚未连接的 SSHClient。
    副作用：创建本地 socket 对象，不发起网络连接。
    """
    if paramiko is None:
        raise PreflightError('当前环境未安装 paramiko，无法执行 SSH 预检')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_build_host_key_policy())
    return client


def _read_stream(stream):
    """读取 paramiko 的输出流并解码为文本。

    参数：`stream` 表示 ``exec_command`` 返回的 stdout 或 stderr。
    返回：返回解码后的字符串，读取失败时返回空串。
    副作用：只读取连接内的数据，不修改任何持久化状态。
    """
    try:
        data = stream.read()
    except Exception:
        # 命令超时或通道被关闭时可能读失败，取不到内容按空处理即可。
        return ''
    if isinstance(data, bytes):
        return data.decode('utf-8', 'replace')
    return str(data or '')


class _NodeSession:
    """单台节点的 SSH 会话，惰性建连并在本次预检内复用连接与命令结果。"""

    def __init__(self, node, credential):
        """初始化会话并记录连接参数。

        参数：`node` 表示 K8sDeploymentNode 实例；`credential` 表示该节点的登录凭据字典。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段，不发起网络连接。
        """
        self.node = node
        self.ip = str(getattr(node, 'ip_address', '') or '').strip()
        self.credential = credential or {}
        self.client = None
        self.connected = False
        self.error = ''
        self._cache = {}

    @staticmethod
    def _describe_error(exc):
        """把 SSH 相关异常翻译成可读的中文失败原因。

        参数：`exc` 表示捕获到的异常对象。
        返回：返回中文失败原因，返回值中不含密码等敏感信息。
        副作用：纯计算，不修改持久化数据。
        """
        if paramiko is not None:
            if isinstance(exc, paramiko.AuthenticationException):
                return 'SSH 登录被拒绝：用户名或密码不正确'
            if isinstance(exc, paramiko.BadHostKeyException):
                return '服务端主机密钥与预期不一致，已中止连接'
            if isinstance(exc, paramiko.SSHException):
                return f'SSH 协议协商失败：{exc}'
        if isinstance(exc, PreflightError):
            return str(exc)
        return f'无法连接目标 SSH 端口：{exc}'

    def connect(self):
        """建立到目标节点的 SSH 连接。

        参数：无。
        返回：返回中文错误说明；连接成功时返回空串。重复调用不会重复建连。
        副作用：打开一条到目标机器的 TCP 连接并完成 SSH 认证。
        """
        if self.connected:
            return ''
        if self.error:
            return self.error

        username = str(self.credential.get('user') or getattr(self.node, 'ssh_user', '') or '').strip()
        port = int(self.credential.get('port') or getattr(self.node, 'ssh_port', 22) or 22)
        password = self.credential.get('password') or ''
        if not username:
            self.error = '未提供 SSH 登录用户名'
            return self.error
        if not password:
            # 没有托管密码就无法登录，明确区分于网络不通，便于用户排查。
            self.error = '该节点未托管 SSH 登录密码，无法建立连接'
            return self.error

        client = None
        try:
            client = _create_ssh_client()
            client.connect(
                hostname=self.ip,
                port=port,
                username=username,
                password=password,
                allow_agent=False,
                look_for_keys=False,
                timeout=SSH_CONNECT_TIMEOUT_SECONDS,
                banner_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
                auth_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self.error = self._describe_error(exc)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    # 关闭失败不影响失败结论，忽略即可。
                    pass
            return self.error

        self.client = client
        self.connected = True
        return ''

    def run(self, command):
        """在目标节点上执行一条只读命令。

        参数：`command` 表示待执行的命令文本。
        返回：返回 ``(是否执行成功, 标准输出, 标准错误)``；未连接或执行异常时第一项为假。
        副作用：在目标机器上执行该命令；命令结果按命令文本缓存，本次预检内不重复执行。
        """
        if command in self._cache:
            return self._cache[command]
        if not self.connected or self.client is None:
            return (False, '', self.error or 'SSH 连接尚未建立')

        try:
            _stdin, stdout, stderr = self.client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT_SECONDS)
            result = (True, _read_stream(stdout), _read_stream(stderr))
        except Exception as exc:
            # 单条命令失败（超时、渠道关闭）不能让整个检查项崩掉，记成失败继续跑。
            result = (False, '', f'命令执行失败：{exc}')
        self._cache[command] = result
        return result

    def close(self):
        """关闭 SSH 连接。

        参数：无。
        返回：无显式返回值。
        副作用：关闭底层 TCP 连接；关闭异常一律忽略。
        """
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self.connected = False


def _make_check(name, title, status, message, detail=None, nodes=None):
    """组装统一格式的检查项结果。

    参数：`name` 为检查项标识；`title` 为中文标题；`status` 为 passed/failed/warned/skipped；
        `message` 为中文说明；`detail` 为结构化明细；`nodes` 为该项涉及的节点 IP。
    返回：返回可直接入库与展示的检查项字典。
    副作用：纯计算，不修改持久化数据。
    """
    return {
        'name': name,
        'title': title,
        'status': status,
        'message': message,
        'detail': detail or {},
        'nodes': list(nodes or []),
    }


def _skipped_without_nodes(name, sessions, reason='所有目标节点 SSH 均不可用，本项无法检查'):
    """构造「因无可用节点而跳过」的检查项结果。

    参数：`name` 为检查项标识；`sessions` 为全部会话；`reason` 为跳过原因。
    返回：返回 status 为 skipped 的检查项字典。
    副作用：纯计算，不修改持久化数据。
    """
    detail = {ip: {'ok': False, 'message': session.error or 'SSH 不可用'} for ip, session in sessions.items()}
    return _make_check(name, CHECK_TITLES[name], 'skipped', reason, detail, nodes=list(sessions.keys()))


def _check_ssh(sessions):
    """检查项 1：SSH 连通性。

    参数：`sessions` 为 ``{ip: _NodeSession}`` 映射。
    返回：返回 ``(检查项字典, 可达节点 IP 列表)``，后续检查项只处理可达节点。
    副作用：与每台目标节点建立 SSH 连接。
    """
    detail = {}
    failures = []
    reachable = []
    for ip, session in sessions.items():
        error = session.connect()
        if error:
            detail[ip] = {'ok': False, 'message': error}
            failures.append(f'{ip}：{error}')
        else:
            detail[ip] = {'ok': True, 'message': 'SSH 连接成功'}
            reachable.append(ip)

    if failures:
        return _make_check(
            'ssh', CHECK_TITLES['ssh'], 'failed',
            '以下节点 SSH 连接失败：' + '；'.join(failures), detail, nodes=list(detail.keys()),
        ), reachable
    return _make_check(
        'ssh', CHECK_TITLES['ssh'], 'passed',
        f'{len(sessions)} 台节点 SSH 连接全部成功', detail, nodes=list(detail.keys()),
    ), reachable


def _check_privilege(sessions, reachable):
    """检查项 2：登录权限。

    登录用户是 root，或者能在不输入密码的情况下执行 sudo，两者满足其一即可。
    否则部署流程无法写入系统目录、无法安装软件包，属于硬失败。

    参数：`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在目标节点上执行 ``id -u`` 与 ``sudo -n true`` 两条只读命令。
    """
    if not reachable:
        return _skipped_without_nodes('privilege', sessions)

    detail = {}
    failures = []
    for ip in reachable:
        session = sessions[ip]
        ok, out, err = session.run('id -u')
        uid = out.strip().splitlines()[-1].strip() if out.strip() else ''
        if not ok or not uid:
            message = f'无法执行 id -u：{out or err or "无输出"}'
            detail[ip] = {'ok': False, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        if uid == '0':
            detail[ip] = {'ok': True, 'mode': 'root', 'message': '登录用户为 root'}
            continue

        # 非 root 时必须免密 sudo；-n 表示不允许交互输入密码，避免预检卡在密码提示上。
        ok, out, _err = session.run('sudo -n true >/dev/null 2>&1 && echo SUDO_OK || echo SUDO_NO')
        if out.strip() == 'SUDO_OK':
            detail[ip] = {'ok': True, 'mode': 'sudo', 'uid': uid, 'message': '非 root 用户但支持免密 sudo'}
        else:
            message = f'登录用户 uid={uid}，既不是 root 也不支持免密 sudo'
            detail[ip] = {'ok': False, 'uid': uid, 'message': message}
            failures.append(f'{ip}：{message}')

    if failures:
        return _make_check(
            'privilege', CHECK_TITLES['privilege'], 'failed', '；'.join(failures), detail, nodes=list(detail.keys()),
        )
    return _make_check(
        'privilege', CHECK_TITLES['privilege'], 'passed',
        f'{len(detail)} 台节点的登录权限满足部署要求', detail, nodes=list(detail.keys()),
    )


def _check_os_release(deployment, sessions, reachable):
    """检查项 3：操作系统匹配（仅提示，不拦截）。

    读实机 ``/etc/os-release``，用 ``parse_os_release`` 解析后再交给
    ``match_supported_system`` 与配置中心选择的系统比对；实机 ``VERSION_ID``
    常与配置中心存的字面值不同（例如实机 8.8、配置中心 8），因此比对按主版本归一化。

    **不匹配只报警告**：配置中心可选系统与实际装机系统不必一一对应（例如在 CentOS 7 上
    按 Rocky 8 部署，只要 kubeasz 离线包覆盖该内核即可），是否继续由用户判断。这里若判为
    失败，用户就没有任何继续的余地，而真正装不上会在 ``01.prepare`` 阶段以更具体的错误暴露。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典；不匹配时为 ``warned``，不进入 ``hard_failures``。
    副作用：在目标节点上执行 ``cat /etc/os-release``。
    """
    if not reachable:
        return _skipped_without_nodes('os_release', sessions)

    detail = {}
    warnings = []
    for ip in reachable:
        ok, out, err = sessions[ip].run('cat /etc/os-release 2>/dev/null')
        if not ok or not out.strip():
            message = f'无法读取 /etc/os-release：{out or err or "无输出"}'
            detail[ip] = {'ok': False, 'message': message}
            warnings.append(f'{ip}：{message}')
            continue
        info = parse_os_release(out)
        matched, reason = match_supported_system(info, deployment.os_type, deployment.os_version)
        detail[ip] = {'ok': matched, 'message': reason, 'os_release': info}
        if not matched:
            warnings.append(f'{ip}：{reason}')

    if warnings:
        return _make_check(
            'os_release', CHECK_TITLES['os_release'], 'warned',
            '操作系统与配置中心选择不完全一致，请确认可以继续：' + '；'.join(warnings),
            detail, nodes=list(detail.keys()),
        )
    return _make_check(
        'os_release', CHECK_TITLES['os_release'], 'passed',
        f'{len(detail)} 台节点的操作系统与配置中心选择一致', detail, nodes=list(detail.keys()),
    )


def _parse_df_row(text):
    """从 ``df -P`` 输出中取出数据行并切分字段。

    参数：`text` 表示 df 命令的标准输出。
    返回：返回字段列表；输出不完整时返回空列表。
    副作用：纯计算，不修改持久化数据。
    """
    lines = [line for line in str(text or '').splitlines() if line.strip()]
    # 第一行是表头；-P 模式下每个文件系统只占一行，取最后一行即目标挂载点。
    if len(lines) < 2:
        return []
    return lines[-1].split()


def _node_roles(node):
    """读取节点快照里的集群角色。

    参数：`node` 表示 K8sDeploymentNode 实例。
    返回：返回小写角色名集合，例如 ``{'etcd', 'master'}``。
    副作用：纯计算，不修改持久化数据。
    """
    roles = getattr(node, 'roles', None) or []
    if isinstance(roles, str):
        # 兼容手工构造的调用方（模型里是 JSONField，正常读到的是列表）。
        roles = [roles]
    return {str(item).strip().lower() for item in roles if str(item).strip()}


def _ssh_port(session):
    """读取节点快照里记录的 SSH 端口。

    参数：`session` 表示节点会话。
    返回：返回 ``1..65535`` 的端口号；读不到或超出范围时返回 0。
    副作用：纯计算，不修改持久化数据。

    节点间的 TCP 连通性就探这个端口：它此刻一定在监听（预检自己就连在它上面），
    因此不需要在目标机器上临时起进程，也不会把「集群还没部署」混进结论里。
    """
    try:
        port = int(getattr(session.node, 'ssh_port', 0) or 0)
    except (TypeError, ValueError):
        return 0
    return port if 0 < port < 65536 else 0


def _disk_threshold_gb(roles):
    """按角色返回磁盘可用空间的硬阈值。

    参数：`roles` 表示角色名集合。
    返回：返回阈值 GB 数：含 etcd 或 control-plane 的节点取 40，其余取 30。
    副作用：纯计算，不修改持久化数据。
    """
    if roles & {'etcd', 'master', 'control-plane'}:
        return DISK_MIN_FREE_GB_CONTROL_PLANE
    return DISK_MIN_FREE_GB_WORKER


def _data_dir_path(deployment):
    """取出并校验本次部署要用的自定义数据目录。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回 ``(路径, 错误说明)``；未启用 dataDir 或路径被拒时路径为空串。
    副作用：纯计算，不修改持久化数据。

    路径会被拼进远程 shell 命令，因此这里按白名单卡一遍：只允许绝对路径、普通字符段，
    并且拒绝任何一段是 ``..``。串口里带引号或分号就等于把命令注入送到目标机器上。
    """
    if not getattr(deployment, 'data_dir_enabled', False):
        return '', ''
    raw = str(getattr(deployment, 'data_dir', '') or '').strip().rstrip('/')
    if not raw:
        return '', '已启用自定义数据目录但没有填路径'
    if '..' in raw.split('/') or not DATA_DIR_PATTERN.match(raw):
        return '', f'数据目录「{raw}」不合法，必须是绝对路径且只含字母、数字与 _ . - /'
    return raw, ''


def _disk_probe_command(path, inode=False):
    """拼出「量某个路径所在文件系统」的远程命令。

    参数：`path` 表示要量的路径；`inode` 为真时改用 ``df -Pi`` 量 inode。
    返回：返回可交给 SSH 执行的命令文本。
    副作用：纯计算，不修改持久化数据。

    dataDir 要到部署阶段才由 kubeasz 创建，预检时该目录往往还不存在，直接 ``df`` 会报错；
    因此先向上找到一个真实存在的父目录再量——量到的是同一个文件系统。
    """
    flag = '-Pi' if inode else '-P'
    return (
        f"d='{path}'; while [ ! -d \"$d\" ] && [ \"$d\" != / ]; do d=$(dirname \"$d\"); done; "
        f'df {flag} "$d" 2>/dev/null'
    )


def _platform_cache_free_gb():
    """量平台机制品缓存目录所在盘的可用空间。

    参数：无。
    返回：返回 ``(可用空间 GB 或 None, 实际探测的路径, 错误说明)``。
    副作用：只读本机文件系统元信息，不创建目录、不写文件。

    缓存目录可能还没建出来，向上取最近一个存在的父目录：同一个文件系统，量出来一样。
    """
    try:
        from . import paths as paths_module

        root = paths_module.artifact_cache_root()
    except Exception as exc:  # pragma: no cover - 只在 settings 缺少配置时出现
        return None, '', f'无法定位制品缓存目录：{exc}'
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError as exc:
        return None, str(probe), f'无法读取缓存盘容量：{exc}'
    return round(usage.free / 1024 ** 3, 2), str(probe), ''


def _check_disk(deployment, sessions, reachable):
    """检查项 4：磁盘与 inode。

    按角色量不同的盘：etcd/control-plane 节点要求 ``DISK_MIN_FREE_GB_CONTROL_PLANE``，
    纯 worker 要求 ``DISK_MIN_FREE_GB_WORKER``；启用了自定义 dataDir 时量的是该路径所在的
    数据盘，否则量系统盘（规划 8、9）。目标节点空间不足判硬失败——这是装不装得下的问题。
    平台机自己的制品缓存盘同样量一次，但只给警告：缓存盘不够是清理一下就能解决的事，
    不该因为它把一次合规的部署提交判死。inode 使用率超过 ``INODE_USED_WARN_PERCENT`` 也只给警告。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：读取本机磁盘用量，并在目标节点上执行 ``df -P`` 与 ``df -Pi``。
    """
    detail = {}
    failures = []
    warnings = []

    free_gb, probe_path, error = _platform_cache_free_gb()
    if error:
        detail['platform_cache'] = {'ok': None, 'path': probe_path, 'message': error}
        warnings.append(f'平台缓存盘无法判定：{error}')
    else:
        entry = {
            'path': probe_path,
            'free_gb': free_gb,
            'required_gb': PLATFORM_CACHE_MIN_FREE_GB,
        }
        if free_gb < PLATFORM_CACHE_MIN_FREE_GB:
            entry['ok'] = False
            entry['message'] = (
                f'平台机制品缓存盘仅剩 {free_gb} GB，低于建议的 {PLATFORM_CACHE_MIN_FREE_GB} GB，'
                '下载阶段可能写满失败'
            )
            warnings.append(f'平台缓存盘：{entry["message"]}')
        else:
            entry['ok'] = True
            entry['message'] = f'平台机制品缓存盘可用 {free_gb} GB'
        detail['platform_cache'] = entry

    data_dir, data_dir_error = _data_dir_path(deployment)
    detail['data_dir'] = data_dir
    if data_dir_error:
        # 路径本身不合法就不要再去 SSH 了：这条命令会被拼进远程 shell。
        failures.append(f'自定义数据目录不可用：{data_dir_error}')

    if not reachable:
        if failures:
            return _make_check(
                'disk', CHECK_TITLES['disk'], 'failed', '；'.join(failures), detail,
            )
        if warnings:
            return _make_check(
                'disk', CHECK_TITLES['disk'], 'warned',
                '目标节点 SSH 全部不可用，未能检查磁盘；' + '；'.join(warnings), detail,
            )
        return _skipped_without_nodes('disk', sessions)

    # 检查路径：启用了 dataDir 就量数据盘，否则量系统盘（规划 8）。
    path = data_dir or '/'
    for ip in reachable:
        session = sessions[ip]
        roles = _node_roles(session.node)
        required_gb = _disk_threshold_gb(roles)
        entry = {'roles': sorted(roles), 'path': path, 'required_gb': required_gb}

        ok, out, err = session.run(_disk_probe_command(path))
        fields = _parse_df_row(out) if ok else []
        if len(fields) < 4:
            message = f'无法解析 {path} 所在文件系统的容量：{out or err or "无输出"}'
            entry.update({'ok': False, 'message': message})
            detail[ip] = entry
            failures.append(f'{ip}：{message}')
            continue
        try:
            available_kb = int(fields[3])
        except (TypeError, ValueError):
            message = f'{path} 可用空间字段无法解析：{fields[3]}'
            entry.update({'ok': False, 'message': message})
            detail[ip] = entry
            failures.append(f'{ip}：{message}')
            continue
        entry['free_gb'] = round(available_kb / 1024 / 1024, 2)
        entry['mount'] = fields[5] if len(fields) > 5 else path

        # inode 单独取一次，df -Pi 的输出列与容量不同，不能混用。
        ok_i, out_i, _err_i = session.run(_disk_probe_command(path, inode=True))
        inode_fields = _parse_df_row(out_i) if ok_i else []
        if len(inode_fields) >= 5:
            try:
                entry['inode_free'] = int(inode_fields[3])
                entry['inode_used_percent'] = int(str(inode_fields[4]).replace('%', ''))
            except (TypeError, ValueError):
                # inode 数据拿不到不影响空间判定，忽略即可。
                pass

        if entry['free_gb'] < required_gb:
            role_hint = '/'.join(sorted(roles)) or '未分配角色'
            entry['ok'] = False
            entry['message'] = (
                f'{path}（挂载点 {entry["mount"]}，角色 {role_hint}）可用空间仅 {entry["free_gb"]} GB，'
                f'低于要求的 {required_gb} GB'
            )
            failures.append(f'{ip}：{entry["message"]}')
        else:
            entry['ok'] = True
            entry['message'] = f'{path}（挂载点 {entry["mount"]}）可用空间 {entry["free_gb"]} GB'

        inode_free = entry.get('inode_free')
        inode_percent = entry.get('inode_used_percent')
        if inode_free is not None and (inode_free < INODE_MIN_FREE or (inode_percent or 0) >= INODE_USED_WARN_PERCENT):
            entry['inode_warning'] = True
            warnings.append(f'{ip}：inode 空闲 {inode_free}（使用率 {inode_percent}%），建议先清理小文件')
        detail[ip] = entry

    node_ips = list(reachable)
    if failures:
        return _make_check(
            'disk', CHECK_TITLES['disk'], 'failed', '；'.join(failures), detail, nodes=node_ips,
        )
    if warnings:
        return _make_check(
            'disk', CHECK_TITLES['disk'], 'warned',
            '磁盘空间满足要求，但存在需要留意的项：' + '；'.join(warnings), detail, nodes=node_ips,
        )
    return _make_check(
        'disk', CHECK_TITLES['disk'], 'passed',
        f'{len(node_ips)} 台节点在 {path} 上的可用空间均达到按角色要求的阈值', detail, nodes=node_ips,
    )


def _ipvs_module_command():
    """拼出探测 ipvs 所需内核模块的远程命令。

    参数：无。
    返回：返回可交给 SSH 执行的命令文本，输出以 ``MODULES_OK``/``MODULES_LOADED``/
        ``MODULES_MISSING <模块名...>`` 之一结尾。
    副作用：纯计算，不修改持久化数据。

    先看模块是否已加载（``lsmod`` 第一列是模块名），再用 ``modinfo -n`` 看内核里是否
    存在对应的模块文件（未加载但可在部署时 modprobe）。不用 ``find`` 扫整棵模块树：
    在模块多的大内核上要跑好几秒，而 ``modinfo`` 读的是同一份元数据，快得多也准得多。
    """
    modules = ' '.join(IPVS_REQUIRED_MODULES)
    return (
        f'missing=""; loaded=1; '
        'if command -v modinfo >/dev/null 2>&1; then probe=modinfo; else probe=dir; fi; '
        f'for m in {modules}; do '
        'if lsmod 2>/dev/null | awk \'{print $1}\' | grep -qx "$m"; then continue; fi; '
        'loaded=0; '
        'if [ "$probe" = modinfo ]; then '
        'modinfo -n "$m" >/dev/null 2>&1 || missing="$missing $m"; '
        'else '
        # 退路：modinfo 与 lsmod 同属 kmod，缺了它只能按模块目录粗判。
        'base="/lib/modules/$(uname -r)/kernel/net/netfilter"; '
        'case "$m" in '
        'nf_conntrack) [ -d "$base" ] || missing="$missing $m" ;; '
        '*) [ -d "$base/ipvs" ] || missing="$missing $m" ;; '
        'esac; '
        'fi; '
        'done; '
        'if [ -n "$missing" ]; then echo "MODULES_MISSING$missing"; '
        'elif [ "$loaded" = 1 ]; then echo MODULES_LOADED; '
        'else echo MODULES_OK; fi'
    )


def _check_kernel(deployment, sessions, reachable):
    """检查项 5：内核与 kube-proxy 模式的前提。

    分两段：先做不依赖 SSH 的本地判断——该 kubeasz 版本的集群模板认不认这个代理模式
    （3.7.0 起不认 ipvs，3.6.x 不认 nftables），不认的话机器再健康也部署不出来；
    再由模式决定要探什么：ipvs 看 ``IPVS_REQUIRED_MODULES`` 是否齐全，nftables 看内核与
    ``nft`` 是否满足上游写明的 kernel >= 5.13、nft >= 1.0.0。iptables 没有额外前提，
    本项跳过。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在目标节点上执行 ``lsmod``/``modinfo``/``uname``/``nft --version`` 只读探测命令。
    """
    mode = str(deployment.kube_proxy_mode or '').strip().lower()
    version = str(deployment.kubeasz_version or '').strip()
    detail = {'kubeasz_version': version, 'kube_proxy_mode': mode}
    supported, reason = kubeasz_supports_proxy_mode(version, mode)
    detail['local_check'] = reason

    if not supported:
        modes, _default = kubeasz_proxy_modes(version)
        return _make_check(
            'kernel', CHECK_TITLES['kernel'], 'failed',
            f'{reason}，请改用该版本支持的模式（{"、".join(modes)}）后重新提交',
            detail, nodes=[session.ip for session in sessions.values()],
        )

    if mode == 'nftables':
        return _check_nftables(deployment, sessions, reachable, detail)

    if mode != 'ipvs':
        return _make_check(
            'kernel', CHECK_TITLES['kernel'], 'skipped',
            f'当前代理模式为 {mode or "未设置"}，无需检查 ipvs 相关内核模块',
            detail, nodes=[session.ip for session in sessions.values()],
        )

    if not reachable:
        return _skipped_without_nodes('kernel', sessions, '所有目标节点 SSH 均不可用，无法检查 ipvs 相关内核模块')

    detail['required_modules'] = list(IPVS_REQUIRED_MODULES)
    command = _ipvs_module_command()
    failures = []
    nodes_detail = {}
    for ip in reachable:
        ok, out, err = sessions[ip].run(command)
        marker = out.strip().splitlines()[-1].strip() if out.strip() else ''
        if not ok or not marker.startswith('MODULES_'):
            message = f'无法确认 ipvs 相关内核模块状态：{out or err or "无输出"}'
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        missing = [name for name in marker[len('MODULES_MISSING'):].split() if name]
        if missing:
            message = f'缺少内核模块：{"、".join(missing)}，请安装 ipvsadm / 加载这些模块'
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'missing': missing, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        nodes_detail[ip] = {
            'ok': True,
            'marker': marker,
            'loaded': marker == 'MODULES_LOADED',
            'message': 'ipvs 相关模块已全部加载'
            if marker == 'MODULES_LOADED'
            else '内核中存在全部 ipvs 相关模块，可在部署时自动加载',
        }
    detail['nodes'] = nodes_detail

    if failures:
        return _make_check(
            'kernel', CHECK_TITLES['kernel'], 'failed',
            '；'.join(failures) + '；ipvs 模式下缺任一模块 kube-proxy 都起不来，请先补齐模块',
            detail, nodes=list(nodes_detail.keys()),
        )
    return _make_check(
        'kernel', CHECK_TITLES['kernel'], 'passed',
        f'{len(nodes_detail)} 台节点均具备 ipvs 所需的 {len(IPVS_REQUIRED_MODULES)} 个内核模块，'
        f'代理模式 {version} + ipvs 组合受支持',
        detail, nodes=list(nodes_detail.keys()),
    )


def _semver_tuple(value):
    """把三段式版本号解析成可比较的元组。

    参数：`value` 表示版本文本，例如 ``v1.0.6``；缺段时按 0 补齐。
    返回：返回 ``(主, 次, 修订)``；解析不出来时返回 None。
    副作用：纯计算，不修改持久化数据。
    """
    match = re.match(r'\s*[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?', str(value or ''))
    if not match:
        return None
    return tuple(int(match.group(index) or 0) for index in (1, 2, 3))


def _nftables_probe_command():
    """拼出探测 nftables 模式前提的远程命令。

    参数：无。
    返回：返回可交给 SSH 执行的命令文本，输出以 ``NFT_PROBE kernel=.. nft=..`` 结尾。
    副作用：纯计算，不修改持久化数据。

    命令文本必须是**常量**：``_NodeSession.run`` 按命令文本缓存，拼进变量会让同一台机器
    上的探测被当成两件事各跑一次。``nft`` 缺失时 ``nft=none``，与「内核太老」分开报，
    这两种情况的处置完全不同（装包 vs 换模式）。
    """
    return (
        'kr=$(uname -r); '
        'nv=$(nft --version 2>/dev/null | cut -d" " -f2); '
        'echo "NFT_PROBE kernel=${kr:-unknown} nft=${nv:-none}"'
    )


def _probe_fields(marker, prefix):
    """把探测命令输出里的 ``key=value`` 拆成字典。

    参数：`marker` 表示以 ``prefix`` 开头的输出行；`prefix` 表示标记前缀。
    返回：返回字段字典；没有成对的 ``key=value`` 时返回空字典。
    副作用：纯计算，不修改持久化数据。
    """
    fields = {}
    for token in str(marker or '')[len(prefix):].split():
        if '=' not in token:
            continue
        key, _, value = token.partition('=')
        fields[key.strip()] = value.strip()
    return fields


def _check_nftables(deployment, sessions, reachable, detail):
    """nftables 模式的内核前提检查。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表；
        `detail` 为已填好版本与代理模式的明细字典。
    返回：返回检查项字典。
    副作用：在目标节点上执行 ``uname -r`` 与 ``nft --version`` 只读命令。

    门槛来自上游：kubeasz 3.7.0 的集群模板在 ``PROXY_MODE`` 旁写明 nftables 需要
    kernel >= 5.13 与 nft >= 1.0.0，低于这个门槛 kube-proxy 的 nftables 模式起不来 ——
    而 kube-proxy 挂了整个集群的 Service 就没了，属于装完也用不了。
    """
    if not reachable:
        return _skipped_without_nodes('kernel', sessions, '所有目标节点 SSH 均不可用，无法检查 nftables 前提')

    detail['required'] = {
        'kernel': '.'.join(str(part) for part in NFTABLES_MIN_KERNEL),
        'nft': '.'.join(str(part) for part in NFTABLES_MIN_VERSION),
    }
    command = _nftables_probe_command()
    failures = []
    nodes_detail = {}
    for ip in reachable:
        ok, out, err = sessions[ip].run(command)
        marker = out.strip().splitlines()[-1].strip() if out.strip() else ''
        if not ok or not marker.startswith('NFT_PROBE'):
            message = f'无法确认 nftables 前提：{out or err or "无输出"}'
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        fields = _probe_fields(marker, 'NFT_PROBE')
        kernel_text = fields.get('kernel', '')
        nft_text = fields.get('nft', '')
        kernel = _version_tuple(kernel_text)
        if kernel is None:
            message = f'无法解析内核版本「{kernel_text or "（空）"}」'
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        if kernel < NFTABLES_MIN_KERNEL:
            message = (
                f'内核 {kernel_text} 低于 nftables 模式要求的 '
                f'{".".join(str(part) for part in NFTABLES_MIN_KERNEL)}'
            )
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'kernel': kernel_text, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        if nft_text in ('', 'none'):
            message = '没有 nft 命令，nftables 模式要靠它下发规则，请安装 nftables 包'
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'kernel': kernel_text, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        nft = _semver_tuple(nft_text)
        if nft is None:
            message = f'无法解析 nft 版本「{nft_text}」'
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'kernel': kernel_text, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        if nft < NFTABLES_MIN_VERSION:
            message = (
                f'nft {nft_text} 低于 nftables 模式要求的 '
                f'{".".join(str(part) for part in NFTABLES_MIN_VERSION)}，请升级 nftables 包'
            )
            nodes_detail[ip] = {'ok': False, 'marker': marker, 'kernel': kernel_text, 'message': message}
            failures.append(f'{ip}：{message}')
            continue
        nodes_detail[ip] = {
            'ok': True,
            'marker': marker,
            'kernel': kernel_text,
            'nft': nft_text,
            'message': f'内核 {kernel_text} 与 nft {nft_text} 满足 nftables 模式的要求',
        }
    detail['nodes'] = nodes_detail

    if failures:
        return _make_check(
            'kernel', CHECK_TITLES['kernel'], 'failed',
            '；'.join(failures) + '；nftables 模式的前提不满足时 kube-proxy 起不来，'
            '请升级内核/安装 nftables，或改用 iptables 模式后重新提交',
            detail, nodes=list(nodes_detail.keys()),
        )
    return _make_check(
        'kernel', CHECK_TITLES['kernel'], 'passed',
        f'{len(nodes_detail)} 台节点均满足 nftables 模式的前提'
        f'（内核 ≥ {detail["required"]["kernel"]}、nft ≥ {detail["required"]["nft"]}）',
        detail, nodes=list(nodes_detail.keys()),
    )


def _find_listen_port(text, port):
    """在 ss / netstat 输出中查找某个本地端口是否处于 LISTEN 状态。

    参数：`text` 表示命令输出；`port` 表示待查找的端口号。
    返回：返回 ``(是否被占用, 命中的原始行)``。
    副作用：纯计算，不修改持久化数据。
    """
    pattern = re.compile(r'[:.]%d(?:\s|$)' % int(port))
    for line in str(text or '').splitlines():
        if 'LISTEN' not in line.upper():
            continue
        if pattern.search(line):
            return True, line.strip()
    return False, ''


def _api_server_port(deployment):
    """取本次任务要用的 ApiServer 端口。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回端口号（整数）；字段缺失或写坏时退回 kubeasz 的默认 6443。
    副作用：纯计算，不修改持久化数据。
    """
    try:
        return int(getattr(deployment, 'api_server_port', 0) or 6443)
    except (TypeError, ValueError):
        return 6443


def _port_owner_units(deployment, port):
    """列出本平台会在这个端口上起服务的单元名。

    参数：`deployment` 为 K8sDeployment 实例；`port` 为端口号。
    返回：返回单元名元组；本平台不会在这个端口上起服务时返回空元组。
    副作用：纯计算，不修改持久化数据。

    固定端口查 ``PORT_OWNER_UNITS``；ApiServer 端口由用户填，按角色取候选集合。
    这也是某个角色上「端口被占」到底该找谁认领的依据。
    """
    port = int(port)
    if port == _api_server_port(deployment):
        return API_PORT_OWNER_UNITS
    return PORT_OWNER_UNITS.get(port, ())


def _reinstalls_the_cluster(deployment):
    """判断本次任务是不是「在这批机器上（重新）装一遍这套集群」。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回布尔值。
    副作用：纯计算，不修改持久化数据。

    只有全量部署会重跑 01~07，机器上已经跑着的那套组件与它要装的是同一套，重跑就是原地
    接管，所以这类占用可以认领。扩容任务相反：它要加的那台机器上如果已经有本平台的组件，
    说明那台机器本来就是别的集群的一员 —— 那种占用必须拦下来，认领等于把两个集群搅在一起。
    """
    return str(getattr(deployment, 'task_type', '')) in DEPLOY_TASK_TYPES


def _self_install_probe_command():
    """拼出认领「这台机器上占着端口/地址的是不是本平台装的东西」的远程命令。

    参数：无。
    返回：返回可交给 SSH 执行的单行命令文本。
    副作用：纯计算，不修改持久化数据。

    三段输出：各组件单元文件在不在、服务在不在跑（``UNIT:<名字>:<状态>``），
    keepalived 配置全文（夹在两个标记之间，由调用方去比对里面的地址）。

    命令文本必须是**常量**：``_NodeSession.run`` 按命令文本缓存，把 VIP 之类的值拼进
    命令里会让「端口占用」与「外部负载均衡」两项各跑一次同一个探针。
    """
    units = ' '.join(SELF_INSTALL_UNITS)
    return (
        f'echo {SELF_INSTALL_PROBE_BEGIN}; '
        f'for u in {units}; do '
        f'if [ -e {SELF_INSTALL_UNIT_DIR}/$u.service ]; then '
        'echo "UNIT:$u:$(systemctl is-active $u 2>/dev/null)"; fi; done; '
        f'echo {KEEPALIVED_CONF_BEGIN}; cat {KEEPALIVED_CONF} 2>/dev/null; '
        f'echo {KEEPALIVED_CONF_END}'
    )


def _parse_self_install_probe(text):
    """解析认领探针的输出。

    参数：`text` 表示命令输出。
    返回：返回 ``{'units': {单元名: 状态}, 'keepalived_conf': 配置全文}``。
    副作用：纯计算，不修改持久化数据。

    读不到的部分一律按「不是我们的」处理：命令没跑成、单元文件不存在、配置读不出来，
    落到调用方就是照旧硬失败 —— 这一项的方向是宁可拦下来，也不能替别人的进程背书。
    单元文件在、但 ``systemctl is-active`` 给不出状态时同理（状态是空串，不等于 active）。
    """
    units = {}
    conf_lines = []
    in_conf = False
    for line in str(text or '').splitlines():
        if line.startswith('UNIT:'):
            parts = line.split(':', 2)
            if len(parts) == 3 and parts[1]:
                units[parts[1]] = parts[2].strip()
        elif line.strip() == KEEPALIVED_CONF_BEGIN:
            in_conf = True
        elif line.strip() == KEEPALIVED_CONF_END:
            in_conf = False
        elif in_conf:
            conf_lines.append(line)
    return {'units': units, 'keepalived_conf': '\n'.join(conf_lines)}


def _read_self_install_trace(session):
    """读一次「本平台在这台机器上装下的痕迹」。

    参数：`session` 表示节点会话。
    返回：返回 ``_parse_self_install_probe`` 的结果。
    副作用：在目标节点上执行一次只读命令；命令按文本缓存，同一台机器只跑一次。

    只在**已经有冲突**时才调用（见 ``_check_ports`` 与 ``_check_external_lb``）：
    一切正常时不发这条命令，预检的连机器次数与从前一模一样。
    """
    ok, out, _err = session.run(_self_install_probe_command())
    if not ok:
        return {'units': {}, 'keepalived_conf': ''}
    return _parse_self_install_probe(out)


def _unit_state(trace, unit):
    """取某个单元在这台机器上的状态。

    参数：`trace` 表示认领探针的结果；`unit` 表示单元名。
    返回：返回 ``systemctl is-active`` 的状态文本；单元文件不存在时返回空串。
    副作用：纯计算，不修改持久化数据。
    """
    return str((trace or {}).get('units', {}).get(unit, '') or '')


def _own_unit_running_on(trace, deployment, port):
    """认领端口：返回「这个端口上跑着的就是本平台上一次装下的组件」的单元名。

    参数：`trace` 表示认领探针的结果；`deployment` 为 K8sDeployment 实例；`port` 为端口号。
    返回：返回单元名（如 ``etcd``）；不是本平台的组件时返回空串。
    副作用：纯计算，不修改持久化数据。

    认领要「单元文件在那台机器上」与「服务正在跑」两条同时成立。端口被监听是调用方的前提。
    服务没在跑（inactive / failed / 读不出状态）而端口上有人监听，恰恰说明占端口的是别人的
    进程 —— 那种情况一个字都不能替它认。
    """
    for unit in _port_owner_units(deployment, port):
        if _unit_state(trace, unit) == 'active':
            return unit
    return ''


def _own_keepalived_vip_state(trace, vip):
    """认领地址：判断网卡上这个 VIP 是不是本平台上一次装下的 keepalived 挂上去的。

    参数：`trace` 表示认领探针的结果；`vip` 表示地址文本。
    返回：返回这台机器上 keepalived 的状态文本；认不出来时返回空串。
    副作用：纯计算，不修改持久化数据。

    证据是机器上 keepalived 自己的配置里就写着这个地址 —— 那份配置由 kubeasz 的 ex-lb
    角色渲染，作者只可能是本平台；再加上 keepalived 的单元文件必须落在这个组件的固定路径下，
    别人装的 keepalived 不会长在那里。

    这里**不要求** keepalived 正在跑：服务停了、地址还留在网卡上，同样是上一次没清干净，
    而重试会重新起同一个 keepalived 把它接管过来。服务当前的状态只用来把话说清楚。
    """
    vip = str(vip or '')
    if not vip or not _unit_state(trace, 'keepalived'):
        return ''
    if vip in str((trace or {}).get('keepalived_conf') or ''):
        return _unit_state(trace, 'keepalived')
    return ''


def _installs_etcd(deployment, roles):
    """判断本次任务是否会在该节点上安装 etcd。

    参数：`deployment` 为 K8sDeployment 实例；`roles` 为该节点的角色集合。
    返回：返回布尔值。
    副作用：纯计算，不修改持久化数据。

    全量部署时，一份没有任何节点声明 etcd 的清单会让 kubeasz 把 master 当 etcd 用
    （``compiler.render_hosts`` 的同一条兜底），所以 master 一并按 etcd 节点算。

    扩容任务不适用：``ezctl add-master`` / ``add-node`` 对应的剧本（23.addmaster.yml /
    22.addnode.yml）里没有 etcd 角色，加 etcd 成员是独立操作 ``ezctl add-etcd``，本平台
    从不调用它。新机器上的 2379/2380 上什么都不起，也就没有「必须空闲」可言。
    """
    if 'etcd' in roles:
        return True
    if 'master' not in roles and 'control-plane' not in roles:
        return False
    return str(getattr(deployment, 'task_type', '')) not in NON_ETCD_TASK_TYPES


def _expected_free_ports(deployment, roles):
    """列出某台节点上本次部署必须为空闲的端口。

    参数：`deployment` 为 K8sDeployment 实例；`roles` 为该节点的角色集合。
    返回：返回 ``{端口: 中文说明}`` 映射。
    副作用：纯计算，不修改持久化数据。

    ApiServer 端口与 kubelet 的 10250 每台节点都要为空；etcd 的 2379/2380 只对跑 etcd 的
    节点有意义（规划 9），判定见 ``_installs_etcd``。端口被占意味着 kubeasz 起服务时直接
    失败，属于该在部署前拦下来的事。

    负载均衡机器是例外：那个 ApiServer 端口上跑的是 l4lb（nginx），它把请求转发给后端的
    apiserver；机器上没有 kubelet，也就没有 10250 可查。
    """
    api_port = _api_server_port(deployment)
    if LB_ROLE in roles and not roles & {'etcd', 'master', 'control-plane', 'node'}:
        return {api_port: f'外部负载均衡 l4lb 将要监听的端口 {api_port}'}
    ports = {
        api_port: f'kubeasz 将要使用的 ApiServer 端口 {api_port}',
        KUBELET_PORT: f'kubelet 端口 {KUBELET_PORT}',
    }
    if _installs_etcd(deployment, roles):
        for port in ETCD_PORTS:
            ports[port] = f'etcd 端口 {port}'
    return ports


def etcd_data_dir(deployment):
    """返回本次部署会在节点上使用的 etcd 数据目录。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回数据目录的绝对路径。
    副作用：纯计算，不连接任何机器。

    规则与 ``compiler.render_config`` 里改写 ``ETCD_DATA_DIR`` 的那一段逐字一致：
    没启用自定义数据目录时是 kubeasz 的默认值，启用时是 ``<data_dir>/etcd``。
    两边不一致会让这一项查错地方，所以刻意照抄同一个算式而不是自己另立一份。

    路径一律经 ``_data_dir_path`` 过一遍白名单，理由同「磁盘与 inode」那一项：这个值会被
    拼进远程 shell 命令（``stale_probe_command``）。取值不合法的，退回默认目录 ——
    非法本身已经由那一项判硬失败，这里只要保证不把它送进 shell。
    """
    base, error = _data_dir_path(deployment)
    if error or not base:
        return DEFAULT_ETCD_DATA_DIR
    return f'{base}/etcd'


def stale_install_targets(deployment):
    """列出「上一次安装的残留」这一项要看的路径与中文说明。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回 ``((路径, 说明), ...)`` 元组。
    副作用：纯计算，不连接任何机器。
    """
    return (
        (f'{etcd_data_dir(deployment)}/member', 'etcd 数据'),
        (f'{ETCD_SSL_DIR}/ca.pem', '集群证书'),
        (KEEPALIVED_CONF, '外部负载均衡配置'),
    )


def stale_probe_command(deployment):
    """拼出侦测残留的远程命令。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回可交给 SSH 执行的单行命令文本。
    副作用：纯计算，不修改数据。

    每处路径打一行 ``PRESENT:<路径>:<类型>:<条目数>`` 或 ``ABSENT:<路径>``。目录要连条目数
    一起报：kubeasz 的 02.etcd 会先把数据目录本身建出来，空目录说明 etcd 从没在这台机器上
    起来过，不算残留。
    """
    targets = ' '.join(path for path, _label in stale_install_targets(deployment))
    return (
        f'echo {STALE_PROBE_BEGIN}; '
        f'for p in {targets}; do '
        'if [ -d "$p" ]; then echo "PRESENT:$p:dir:$(ls -A "$p" 2>/dev/null | wc -l)"; '
        'elif [ -e "$p" ]; then echo "PRESENT:$p:file:1"; '
        'else echo "ABSENT:$p"; fi; done; '
        f'echo {STALE_PROBE_END}'
    )


def parse_stale_probe(text):
    """解析残留探针的输出。

    参数：`text` 表示命令输出。
    返回：返回 ``{路径: 条目数}``；读到 ``ABSENT`` 的路径不出现在结果里，
        没读全（缺收尾标记）时返回空字典 —— 读不出来按「没有残留」处理，
        这一项的方向是「查不出来不判有问题」（同 ``upgrade_precheck`` 的口径）。
    副作用：纯计算，不修改数据。
    """
    found = {}
    started = False
    finished = False
    for line in str(text or '').splitlines():
        line = line.strip()
        if line == STALE_PROBE_BEGIN:
            started = True
            continue
        if line == STALE_PROBE_END:
            finished = True
            break
        if not started or not line.startswith('PRESENT:'):
            continue
        parts = line.split(':', 3)
        if len(parts) != 4:
            continue
        try:
            count = int(parts[3])
        except ValueError:
            count = 1
        if count > 0:
            found[parts[1]] = count
    if not (started and finished):
        return {}
    return found


def stale_cleanup_enabled(deployment):
    """本次任务是否要在部署前清理上一次安装留下的残留。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回布尔值；字段缺失时按平台默认（清理）处理。
    副作用：纯计算。

    取 ``auto_clean_stale``，向导里的「部署前清理这批机器上的残留」就是它，默认勾选。
    """
    return bool(getattr(deployment, 'auto_clean_stale', True))


def installed_stage_keys(deployment):
    """列出本任务已经成功跑完的 kubeasz 安装阶段。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回 ``INSTALLED_STAGE_KEYS`` 里已完成的那部分（按完成顺序）。
    副作用：纯计算。
    """
    completed = [str(item) for item in (getattr(deployment, 'completed_stages', None) or [])]
    return [key for key in completed if key in INSTALLED_STAGE_KEYS]


def stale_cleanup_plan(deployment, retry, hit_nodes):
    """决定命中残留时是「清理」还是「只警告」，并给出可展示的判据。

    参数：`deployment` 为 K8sDeployment 实例；`retry` 表示本次是否重试；
        `hit_nodes` 表示命中残留的节点 IP 列表（仅用于计数）。
    返回：返回 ``{'enabled', 'will_cleanup', 'reason', 'message_tail'}``。
    副作用：纯计算。

    三条判据：

    1. 勾选了开关、且本任务**还没有装出过集群** —— 清理。这是 94 上 k8s-111 那种情形：
       机器上的残留只可能来自别处（本任务这时还没在节点上写过任何东西，预检排在
       01.prepare 之前），清掉它才能装上。
    2. 勾选了开关，但本任务已经有 02.etcd 及以后的阶段跑完 —— **不清理，只警告**。
       集群已经装出来了，这份残留多半就是它自己留下的（典型是 10.ex-lb 失败后的重试），
       清掉等于把可用的集群推倒重装，代价远大于收益。
    3. 没勾选 —— 维持原来那条路：首次执行硬拦，重试只警告（留出「靠重试收敛」的余地）。

    没有命中任何节点时另走一条：机器是干净的，没有可清的东西，``will_cleanup`` 一律为假。
    这条排在最前 —— 少了它，「勾选 + 没装出过集群」会让执行期在一批干净的机器上开出一个
    空的清理阶段，而用户要的恰恰是「没有残留就直接装」。
    """
    enabled = stale_cleanup_enabled(deployment)
    if not hit_nodes:
        return {'enabled': enabled, 'will_cleanup': False, 'reason': 'no_residue', 'message_tail': ''}
    finished = installed_stage_keys(deployment)
    if enabled and not finished:
        return {
            'enabled': True,
            'will_cleanup': True,
            'reason': '',
            'message_tail': (
                f'平台会在部署前先清理这 {len(hit_nodes)} 台机器上的残留'
                '（etcd 数据目录、集群证书、旧 k8s 服务单元与 CNI 配置；'
                '保留容器运行时的镜像缓存），然后按新建集群继续安装。'
                '不想清理的话，可以在「信息确认」页取消勾选「部署前清理上一次安装的残留」。'
            ),
        }
    if enabled:
        return {
            'enabled': True,
            'will_cleanup': False,
            'reason': 'already_installed',
            'message_tail': (
                f'本任务已经装出过集群（已完成 {"、".join(finished)}），这份残留多半就是它自己留下的，'
                '平台不会清理它 —— 清掉等于把集群推倒重装。要继续这次重试可以直接重跑；'
                '确实要推倒重来，请先在「集群接入」里对集群执行「清理集群」。'
            ),
        }
    if retry:
        return {
            'enabled': False,
            'will_cleanup': False,
            'reason': 'disabled',
            'message_tail': (
                '本次是重试，也可能就是本任务自己上一次写下的；若这次重试仍然失败在部署 etcd，'
                '请先清理这批机器上的残留再重试，或在提交时勾选「部署前清理上一次安装的残留」。'
            ),
        }
    return {
        'enabled': False,
        'will_cleanup': False,
        'reason': 'disabled',
        'message_tail': (
            '本次是新建集群，etcd 会以新集群参数启动并被这份旧数据接管（版本不一致时直接以 '
            'invalid downgrade 退出），部署必然失败在 02.etcd。请在提交时勾选'
            '「部署前清理上一次安装的残留」让平台先清干净，或自行清理这批机器：'
            '在「集群接入」里对原集群执行「清理集群」，它会清掉 etcd 数据、证书与容器运行时；'
            '原集群记录已删除、没有清理入口时，需确认这些机器上没有要保留的数据后手工清理。'
        ),
    }


def _check_stale_install(deployment, sessions, reachable):
    """检查项：上一次安装的残留。

    新建集群的机器上不该还留着上一套集群的东西。这一项的由来是 94 上 k8s-111 那次失败：
    三台机器上还留着上一次安装（别的集群）的 etcd 数据，平台以「新建集群」的身份下发
    ``--initial-cluster-state=new``，etcd 却按旧成员表启动，读到比自己新的数据版本后
    直接 ``invalid downgrade`` 退出 —— 表现是 ``systemctl restart etcd`` 返回 1、
    服务卡在 ``activating``，界面上只有一句「部署 etcd：执行失败，退出码 1」。
    那时 01.prepare 已经改过机器（主机名、/etc/hosts、镜像仓库、kubeasz 组件），
    所以这一项要在预检里先把话说清楚，让机器保持原样。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在可达节点上执行一次只读的存在性命令。

    「清理还是硬拦」由 ``stale_cleanup_plan`` 按三条判据决定：勾了开关且本任务还没装出过
    集群就清理（执行期由 ``stale_cleanup`` 落到机器上），勾了但已经装出过集群只警告，
    没勾则退回「首次硬拦、重试警告」的老路。结论放在 ``detail['cleanup']`` 里，
    部署流程据此决定跑不跑清理阶段。
    """
    if str(getattr(deployment, 'task_type', '')) not in DEPLOY_TASK_TYPES:
        return _make_check(
            'stale_install', CHECK_TITLES['stale_install'], 'skipped',
            '只有新建集群会重装整套组件，本次任务不重装，本项不适用',
            {'reason': 'not_a_full_deploy'},
        )
    if not reachable:
        return _skipped_without_nodes('stale_install', sessions)

    command = stale_probe_command(deployment)
    labels = dict(stale_install_targets(deployment))
    retry = int(getattr(deployment, 'retry_count', 0) or 0) > 0
    detail = {'etcd_data_dir': etcd_data_dir(deployment), 'retry': retry}
    nodes_detail = {}
    hits = []
    unknown = []
    for ip in reachable:
        ok, out, err = sessions[ip].run(command)
        if not ok:
            message = f'无法读取机器上的安装痕迹：{out or err or "无输出"}'
            nodes_detail[ip] = {'ok': False, 'message': message}
            unknown.append(f'{ip}：{message}')
            continue
        found = parse_stale_probe(out)
        entry = {
            'ok': not found,
            'found': [],
            'message': '未发现上一次安装留下的集群数据',
        }
        for path, count in sorted(found.items()):
            label = labels.get(path, path)
            entry['found'].append({'path': path, 'label': label, 'entries': count})
        if found:
            items = '、'.join(f'{item["label"]}（{item["path"]}）' for item in entry['found'])
            entry['message'] = items
            hits.append(f'{ip}：{items}')
        nodes_detail[ip] = entry
    # 逐节点明细与其它检查项一致地摊在 detail 顶层（``detail[<IP>]``），界面上按节点取用。
    detail.update(nodes_detail)

    hit_nodes = [ip for ip, entry in nodes_detail.items() if entry.get('found')]
    plan = stale_cleanup_plan(deployment, retry, hit_nodes)
    detail['cleanup'] = {
        'enabled': plan['enabled'],
        'will_cleanup': plan['will_cleanup'],
        'reason': plan['reason'],
        'targets': [ip for ip in hit_nodes] if plan['will_cleanup'] else [],
    }

    if hits:
        if plan['will_cleanup']:
            # 会清干净再装，所以不是「拦下」而是「这次会多做一步」，状态记警告即可。
            status = 'warned'
            prefix = '发现上一次安装留下的集群数据（本次将在部署前自动清理）'
        elif plan['reason'] == 'already_installed':
            # 集群是本任务自己装出来的，这份残留多半就是它 —— 不拦，免得把可用的集群堵在门外。
            status = 'warned'
            prefix = '发现上一次安装留下的集群数据（本任务已经装出过集群，不清理）'
        elif retry:
            status = 'warned'
            prefix = '发现上一次安装留下的集群数据（本次是重试，不拦截）'
        else:
            status = 'failed'
            prefix = '发现上一次安装留下的集群数据'
        return _make_check(
            'stale_install', CHECK_TITLES['stale_install'], status,
            f'{prefix}：' + '；'.join(hits) + '。' + plan['message_tail'],
            detail, nodes=list(nodes_detail.keys()),
        )
    if unknown:
        return _make_check(
            'stale_install', CHECK_TITLES['stale_install'], 'warned',
            '部分节点无法确认有没有上一次安装的残留：' + '；'.join(unknown),
            detail, nodes=list(nodes_detail.keys()),
        )
    return _make_check(
        'stale_install', CHECK_TITLES['stale_install'], 'passed',
        f'{len(nodes_detail)} 台节点上都没有上一次安装留下的 etcd 数据、集群证书与负载均衡配置',
        detail, nodes=list(nodes_detail.keys()),
    )


def _check_ports(deployment, sessions, reachable):
    """检查项 6：端口占用。

    检查 ApiServer ``SECURE_PORT``、etcd 的 2379/2380（仅 etcd/control-plane 节点）与
    kubelet 的 10250 是否已被节点上的其它进程监听；被占用时 kubeasz 起对应服务会直接
    失败，因此判硬失败。节点上没有 ss / netstat 时只给警告。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在目标节点上执行只读的端口列表命令。

    升级是例外：它打的是集群自己的节点，etcd、ApiServer、kubelet 本来就该在监听。
    拿「这些端口必须空闲」去量它们，等于把每一次升级都拦下来 —— 94 上的升级正是
    失败在「etcd 端口 2379 已被占用」这一句上，而占用它的就是该节点上正在跑的 etcd。
    升级不新装任何节点，这一项没有它要查的东西。

    全量部署重试是同一件事的另一面：第一次没跑完，etcd / apiserver / kubelet 已经在那批
    机器上跑着了，再点「重试」会被这一项按同样的理由拦死（94 上的 k8s-cluster 就卡在这里：
    占着端口的正是本平台上一次装下的那几个服务，而集群没建起来，清理入口也没得用）。
    这类占用不再判失败，而是认领下来记一条警告，任务原地接管它们。认领的门槛见
    ``_own_unit_running_on``：单元文件在那台机器上、服务正在跑，缺一条就照旧硬失败。
    """
    if str(getattr(deployment, 'task_type', '')) == 'upgrade':
        return _make_check(
            'ports', CHECK_TITLES['ports'], 'skipped',
            '升级在集群自己的节点上进行，etcd / ApiServer / kubelet 端口正被现有集群监听属于正常，'
            '本项不适用。',
            {'reason': 'upgrade_existing_cluster'},
            nodes=list(sessions.keys()),
        )
    if not reachable:
        return _skipped_without_nodes('ports', sessions)

    command = 'ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null || echo __NO_LISTEN_TOOL__'
    detail = {'checked_ports': []}
    occupied = []
    owned = []
    unknown = []
    nodes_detail = {}
    checked = set()
    claimable = _reinstalls_the_cluster(deployment)
    for ip in reachable:
        roles = _node_roles(sessions[ip].node)
        ports = _expected_free_ports(deployment, roles)
        checked.update(ports)
        ok, out, err = sessions[ip].run(command)
        if not ok:
            message = f'无法执行端口检查命令：{out or err or "无输出"}'
            nodes_detail[ip] = {'ok': False, 'roles': sorted(roles), 'message': message}
            unknown.append(f'{ip}：{message}')
            continue
        if '__NO_LISTEN_TOOL__' in out:
            nodes_detail[ip] = {
                'ok': False, 'roles': sorted(roles),
                'message': '节点缺少 ss 与 netstat 命令，无法确认端口占用情况',
            }
            unknown.append(f'{ip}：节点缺少 ss 与 netstat 命令')
            continue

        entry = {'ok': True, 'roles': sorted(roles), 'ports': {}}
        listening = []
        for port in sorted(ports):
            used, line = _find_listen_port(out, port)
            entry['ports'][str(port)] = line if used else ''
            if used:
                listening.append((port, line))
        # 只有真的有占用、且这次任务就是在这批机器上装集群时才发探针：端口都空着时，
        # 这一项连机器的次数与从前一模一样。
        trace = _read_self_install_trace(sessions[ip]) if (listening and claimable) else None
        node_occupied = []
        node_owned = []
        for port, line in listening:
            unit = _own_unit_running_on(trace, deployment, port)
            if unit:
                message = f'{ports[port]} 已被本平台上一次安装留下的 {unit} 占着（本次会原地接管）：{line}'
                node_owned.append(message)
                owned.append(f'{ip}：{message}')
                continue
            message = f'{ports[port]} 已被占用：{line}'
            node_occupied.append(message)
            occupied.append(f'{ip}：{message}')
        if node_owned:
            entry['claimed'] = node_owned
        if node_occupied:
            entry['ok'] = False
            entry['message'] = '；'.join(node_occupied)
        elif node_owned:
            entry['message'] = '；'.join(node_owned)
        else:
            entry['message'] = f'端口 {"、".join(str(port) for port in sorted(ports))} 均未被监听'
        nodes_detail[ip] = entry
    detail['nodes'] = nodes_detail
    detail['checked_ports'] = sorted(checked)

    if occupied:
        return _make_check(
            'ports', CHECK_TITLES['ports'], 'failed', '；'.join(occupied), detail, nodes=list(nodes_detail.keys()),
        )
    if owned:
        return _make_check(
            'ports', CHECK_TITLES['ports'], 'warned',
            '以下端口上正跑着本平台上一次安装留下的组件，本次会在原地接管，不再要求它们空闲：'
            + '；'.join(owned),
            detail, nodes=list(nodes_detail.keys()),
        )
    if unknown:
        return _make_check(
            'ports', CHECK_TITLES['ports'], 'warned',
            '部分节点无法确认端口占用情况：' + '；'.join(unknown), detail, nodes=list(nodes_detail.keys()),
        )
    return _make_check(
        'ports', CHECK_TITLES['ports'], 'passed',
        f'{len(nodes_detail)} 台节点上待用端口（{"、".join(str(port) for port in sorted(checked))}）均空闲',
        detail, nodes=list(nodes_detail.keys()),
    )


def _clock_sync_command():
    """拼出探测节点时间同步状态的远程命令。

    参数：无。
    返回：返回可交给 SSH 执行的命令文本，输出为若干 ``键:值`` 行。
    副作用：纯计算，不修改持久化数据。

    不假定节点装的是 chrony 还是 systemd-timesyncd：先把几个可观测项都采回来，再由
    ``_clock_sync_state`` 判定。系统的 ``timedatectl`` 在不同发行版上键名不一样，所以
    这里只认「输出里出现过 synchronized: yes」这种更稳的特征。
    """
    return (
        'command -v chronyc >/dev/null 2>&1 && chronyc tracking >/dev/null 2>&1 '
        '&& echo CHRONY:yes || echo CHRONY:no; '
        # chronyc 能跑通不代表已锁定时间源，leap status 为 Normal 才是。
        'echo LEAP:$(chronyc tracking 2>/dev/null | sed -n "s/^Leap status *: *//p"); '
        'echo CHRONYD:$(systemctl is-active chronyd 2>/dev/null || echo inactive); '
        'echo TIMESYNCD:$(systemctl is-active systemd-timesyncd 2>/dev/null || echo inactive); '
        'echo NTPD:$(systemctl is-active ntpd 2>/dev/null || echo inactive); '
        'echo SYNCED:$(timedatectl status 2>/dev/null | grep -ci "synchronized: yes" || echo 0)'
    )


def _clock_sync_state(text):
    """把采集到的键值对判定成「有没有同步服务、是否已同步」。

    参数：`text` 表示 ``_clock_sync_command`` 的输出。
    返回：返回 ``(服务名或空串, 是否已同步)``。
    副作用：纯计算，不修改持久化数据。

    ``chronyc tracking`` 能跑通就说明 chrony 正在守时，此时 leap status 为 Normal 即已同步；
    否则退回到 systemd 时间服务的 active 状态与 ``timedatectl`` 的 synchronized 标志。
    """
    values = {}
    for line in str(text or '').splitlines():
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        values[key.strip().upper()] = value.strip()

    # timedatectl 的 synchronized 标志对任何守时服务都成立，是最直接的口径。
    synced = values.get('SYNCED') == '1' or values.get('LEAP') == 'Normal'
    if values.get('CHRONY') == 'yes':
        return 'chrony', synced
    for key, service in (('CHRONYD', 'chronyd'), ('TIMESYNCD', 'systemd-timesyncd'), ('NTPD', 'ntpd')):
        if values.get(key) == 'active':
            return service, synced
    return '', False


def _check_clock(deployment, sessions, reachable):
    """检查项 7：时钟同步。

    两件事一起看（规划 9）：节点上有没有在跑的 chrony/timesyncd 之类的守时服务，以及节点
    时间与平台机的偏差是否在 ``CLOCK_MAX_DRIFT_SECONDS`` 以内。偏差大或没有同步服务时只给
    警告——时钟漂移不会让部署立刻失败，但会让证书校验和 etcd 选主出问题，值得在动手前提醒。

    参数：`deployment` 为 K8sDeployment 实例（本项不读它的字段，仅为保持各检查项签名一致）；
        `sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在目标节点上执行 ``date -u +%s`` 与时间服务探测命令，并读取平台机当前时间。
    """
    if not reachable:
        return _skipped_without_nodes('clock', sessions)

    local_epoch = int(time.time())
    detail = {'local_epoch': local_epoch, 'threshold_seconds': CLOCK_MAX_DRIFT_SECONDS}
    nodes_detail = {}
    problems = []
    for ip in reachable:
        session = sessions[ip]
        ok, out, err = session.run('date -u +%s')
        raw = out.strip().splitlines()[-1].strip() if out.strip() else ''
        entry = {}
        try:
            remote_epoch = int(raw)
        except (TypeError, ValueError):
            nodes_detail[ip] = {'ok': False, 'message': f'无法读取节点时间：{out or err or "无输出"}'}
            problems.append(f'{ip}：无法读取节点时间，建议人工确认时钟同步状态')
            continue

        drift = abs(remote_epoch - local_epoch)
        entry.update({'remote_epoch': remote_epoch, 'drift_seconds': drift})

        ok_s, out_s, _err_s = session.run(_clock_sync_command())
        service, synced = _clock_sync_state(out_s) if ok_s else ('', False)
        entry['sync_service'] = service
        entry['service_synced'] = synced

        issues = []
        if not service:
            issues.append('未发现 chrony/timesyncd/ntpd 等守时服务')
        elif not synced:
            issues.append(f'{service} 在运行但未完成时间同步')
        if drift > CLOCK_MAX_DRIFT_SECONDS:
            issues.append(f'与平台机时间相差 {drift} 秒，超过 {CLOCK_MAX_DRIFT_SECONDS} 秒')

        if issues:
            entry['ok'] = False
            entry['message'] = '；'.join(issues)
            problems.append(f'{ip}：{entry["message"]}')
        else:
            entry['ok'] = True
            entry['message'] = f'{service} 已同步，与平台机时间相差 {drift} 秒'
        nodes_detail[ip] = entry
    detail['nodes'] = nodes_detail

    if problems:
        return _make_check(
            'clock', CHECK_TITLES['clock'], 'warned',
            '时钟可能不同步：' + '；'.join(problems), detail, nodes=list(nodes_detail.keys()),
        )
    return _make_check(
        'clock', CHECK_TITLES['clock'], 'passed',
        f'{len(nodes_detail)} 台节点均有守时服务在运行且与平台机时间偏差在 '
        f'{CLOCK_MAX_DRIFT_SECONDS} 秒以内',
        detail, nodes=list(nodes_detail.keys()),
    )


def _node_prefixes(session, ignored=None):
    """读取节点上处于 global 作用域的 IPv4 网段。

    参数：`session` 表示节点会话；`ignored` 给定时，把跳过的虚拟网卡名追加进去，供调用方展示。
    返回：返回 ``ipaddress.IPv4Network`` 列表，读取失败时返回空列表。
    副作用：在目标节点上执行 ``ip -o -f inet addr show`` 并读取本机接口信息。

    **必须按接口名排除容器与 Kubernetes 自己建的虚拟网卡。** 它们的地址不属于「节点所在网段」，
    但作用域同样是 global，`ip -o -f inet addr show scope global` 会一并列出来：kube-proxy 的
    ipvs 模式会给每个 Service IP 在 ``kube-ipvs0`` 上挂一个 /32，nodelocaldns 会在 ``nodelocaldns``
    上挂 169.254.20.10，Calico 会留下 ``cali*``/``tunl0``。不排除的话，「网段合法性」就是拿
    Service 网段跟它自己比，报出一串必然成立的「重叠」——集群明明装得好好的，重新部署的预检
    却硬失败，而现象看起来像是上次清理没清干净。
    """
    ok, out, _err = session.run('ip -o -f inet addr show scope global 2>/dev/null')
    if not ok:
        return []
    prefixes = []
    for match in VIRTUAL_INTERFACE_LINE.finditer(out):
        if match.group('name') and any(
            pattern.match(match.group('name')) for pattern in _VIRTUAL_INTERFACE_PATTERNS
        ):
            if ignored is not None and match.group('name') not in ignored:
                ignored.append(match.group('name'))
            continue
        try:
            prefixes.append(ipaddress.ip_network(match.group('cidr'), strict=False))
        except ValueError:
            # 单条网段解析失败不影响其它网段，跳过即可。
            continue
    return prefixes


def _interface_state_command(interface):
    """拼出探测网卡状态的远程命令。

    参数：`interface` 表示网卡名。
    返回：返回可交给 SSH 执行的命令文本，输出以 ``IFACE_UP``/``IFACE_DOWN``/
        ``IFACE_MISSING`` 之一结尾。
    副作用：纯计算，不修改持久化数据。

    看 ``ip -o link show`` 的 flags 而不是 ``state``：物理网卡断网线时会显示 ``state DOWN``
    而 flags 里仍有 ``UP``（管理态是开的），而真正会导致部署失败的是管理态被关掉。
    """
    return (
        f'out=$(ip -o link show {interface} 2>/dev/null) || true; '
        'if [ -z "$out" ]; then echo IFACE_MISSING; '
        'elif echo "$out" | grep -Eq "(<UP,|,UP,|,UP>)"; then echo IFACE_UP; '
        'else echo IFACE_DOWN; fi'
    )


def _tcp_probe_command(target_ip, port):
    """拼出「从本节点 TCP 连到对端某端口」的远程命令。

    参数：`target_ip` 表示对端 IP；`port` 表示对端端口。
    返回：返回可交给 SSH 执行的命令文本，输出以 ``TCP_OK``/``TCP_FAIL``/``TCP_UNKNOWN`` 之一结尾。
    副作用：建立一条到对端的 TCP 连接，不修改任何状态。
    """
    return (
        'if command -v nc >/dev/null 2>&1; then '
        f'nc -z -w {TCP_PROBE_TIMEOUT_SECONDS} {target_ip} {port} >/dev/null 2>&1 && echo TCP_OK || echo TCP_FAIL; '
        'elif command -v bash >/dev/null 2>&1; then '
        f'timeout {TCP_PROBE_TIMEOUT_SECONDS} bash -c "exec 3<>/dev/tcp/{target_ip}/{port}" >/dev/null 2>&1 '
        '&& echo TCP_OK || echo TCP_FAIL; '
        'else echo TCP_UNKNOWN; fi'
    )


# 探测结果的三种取值。它们描述的是「包有没有走到」而不是「端口开没开」：
# TCP_OPEN 有进程在听、TCP_REFUSED 网络通但端口空着（部署前的正常状态）、
# TCP_DROPPED 包被丢了（安全组/防火墙挡着）。
PROBE_OPEN = 'TCP_OPEN'
PROBE_REFUSED = 'TCP_REFUSED'
PROBE_DROPPED = 'TCP_DROPPED'
PROBE_UNKNOWN = 'TCP_UNKNOWN'
# 一行结果的形状：``PROBE <地址>:<端口> <结论>``。前缀用来把探测输出与命令里别的杂音分开。
PROBE_LINE = re.compile(
    r'^PROBE\s+(?P<target>\d+\.\d+\.\d+\.\d+:\d+)\s+(?P<marker>TCP_\w+)\s*$',
    re.MULTILINE,
)


def _etcd_probe_snippet(target_ip, port):
    """拼出「探一个地址的一个端口，并按耗时判断是拒绝还是丢弃」的 shell 片段。

    参数：`target_ip` 表示对端地址；`port` 表示对端端口。
    返回：返回一段可直接拼进远程命令的 shell 代码。
    副作用：纯计算，不修改持久化数据。

    只用「bash 的 ``/dev/tcp`` + ``date`` + ``timeout``」这三样：``date +%s%N`` 是 GNU
    coreutils 的能力，Ubuntu 与 CentOS 都自带；不依赖 ``awk``/``nc``，也不在目标机上
    起任何进程。时间戳取不到（极老的 coreutils）时算不出耗时，按「未知」报出去而不是
    猜一个 —— 判断成「被拒绝」会让一个真正被挡住的集群照样通过预检。

    变量名带前缀是为了不踩目标机上同名的环境变量（``$s``、``$m`` 这种名字在别人的
    shell 里太常见了）。
    """
    # 只接受 IP 字面量与合法端口，避免把任意字符串拼进远程命令。
    try:
        ipaddress.IPv4Address(str(target_ip))
        port = int(port)
    except (ValueError, TypeError):
        return ''
    if not 0 < port < 65536:
        return ''
    label = f'{target_ip}:{port}'
    return (
        f'_k8sm_s=$(date +%s%N 2>/dev/null); '
        f'timeout {ETCD_PROBE_TIMEOUT_SECONDS} bash -c "exec 3<>/dev/tcp/{target_ip}/{port}" '
        '>/dev/null 2>&1; '
        '_k8sm_r=$?; '
        '_k8sm_e=$(date +%s%N 2>/dev/null); '
        'case "$_k8sm_s$_k8sm_e" in '
        "*[!0-9]*|'') _k8sm_m=-1;; "
        '*) _k8sm_m=$(( (_k8sm_e - _k8sm_s) / 1000000 ));; '
        'esac; '
        f'if [ "$_k8sm_r" = "0" ]; then echo "PROBE {label} {PROBE_OPEN}"; '
        'elif [ "$_k8sm_m" -ge 0 ] 2>/dev/null && '
        f'[ "$_k8sm_m" -lt {TCP_DROPPED_THRESHOLD_MS} ]; then echo "PROBE {label} {PROBE_REFUSED}"; '
        f'elif [ "$_k8sm_m" -lt 0 ] 2>/dev/null; then echo "PROBE {label} {PROBE_UNKNOWN}"; '
        f'else echo "PROBE {label} {PROBE_DROPPED}"; fi'
    )


def _etcd_reach_command(targets):
    """拼出「一次探完所有目标」的远程命令。

    参数：`targets` 表示 ``[(地址, 端口), ...]`` 列表。
    返回：返回可交给 SSH 执行的命令文本，输出为若干行 ``PROBE <地址>:<端口> <结论>``。
    副作用：对每个目标建立一条 TCP 连接，不修改对端与自身状态。

    一次连接探完一台节点要探的全部目标：被丢弃的目标个个都要等满超时，逐个发 SSH 命令
    会让连接建立的往返叠加在等待之上，节点一多预检就明显变慢。

    没有 bash 时整段探测都做不了（``/dev/tcp`` 是 bash 的扩展），直接报 ``无 BASH``：
    这与 ``_tcp_probe_command`` 的 ``TCP_UNKNOWN`` 同一处理，结论记为「未能验证」而不是
    「不通」——把「检查不了」当成「有问题」会让一堆正常的机器过不了预检。
    """
    snippets = [_etcd_probe_snippet(address, port) for address, port in targets]
    snippets = [item for item in snippets if item]
    if not snippets:
        return 'echo NO_BASH'
    return (
        'if command -v bash >/dev/null 2>&1; then '
        + '; '.join(snippets)
        + '; else echo NO_BASH; fi'
    )


def _parse_etcd_probe(output):
    """解析 ``_etcd_reach_command`` 的输出。

    参数：`output` 表示命令输出。
    返回：返回 ``{'地址:端口': 结论}`` 映射；``NO_BASH`` 时返回空字典。
    副作用：纯计算，不修改持久化数据。

    只认形状对得上的行，其余（motd、登录提示、别处的 echo）一律忽略 —— 探测输出会与
    shell 的其它输出混在一条流里，按位置取第几行迟早会错位。
    """
    found = {}
    for match in PROBE_LINE.finditer(str(output or '')):
        found[match.group('target')] = match.group('marker')
    return found


def _check_etcd_network(deployment, sessions, reachable):
    """检查项：etcd 节点之间的内网互通。

    etcd 是个 quorum 协议：成员之间既要能互相发 Raft 消息（2380 对等端口），也要能互相
    读写（2379 客户端端口）。少一条链路，那个成员就一直选不上主，集群表现为「起得来但
    只有部分节点在工作」，而 kubeasz 那边的报错是 etcd 的选举日志，跟「安全组没放行」看不出
    关系。云上的安全组默认只放行 SSH，所以这是新建集群最常见的一类环境问题。

    **这一项与 ``_check_network`` 的分工**：那一项探的是 SSH 端口，端口上一定有 sshd 在听，
    所以只能回答「网络通不通」，而且不通只给警告；这一项探的是 etcd 的两个端口，部署前
    它们必然是空的，因此要回答的是「将来装上去之后通不通」—— 靠建连耗时区分「被拒绝」
    （网络通，只是还没装）与「被丢弃」（安全组/防火墙挡着）。被丢弃是**硬失败**：那种
    情况下装完照样不通，让部署继续跑下去只会把机器留在一个半成品状态。

    只在有两个以上 etcd 节点时执行：单节点集群没有成员间通信，探它自己到自己的 2379
    没有信息量（探出来必然是「被拒绝」，因为 etcd 还没装）。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在可达节点上执行只读的 ``ip`` 与 ``date`` 命令，并建立若干条到其它节点的 TCP 连接。
    """
    title = CHECK_TITLES['etcd_network']
    if not reachable:
        return _skipped_without_nodes('etcd_network', sessions)

    members = [
        ip for ip in reachable
        if _installs_etcd(deployment, _node_roles(sessions[ip].node))
    ]
    if len(members) < 2:
        return _make_check(
            'etcd_network', title, 'skipped',
            'etcd 成员少于两台，成员之间没有需要打通的内网链路，本项跳过',
            {'etcd_nodes': members}, nodes=reachable,
        )

    # 每台节点在集群内部用哪个地址，要按部署时的同一套规则算出来：kubeasz 的
    # ``--listen-peer-urls`` / ``--advertise-client-urls`` 直接取 inventory 主机名，
    # 那个名字是节点网卡上真实存在的地址，未必是操作员填进来那一个（见 ``node_address``）。
    # 拿填写的公网地址去探，探到的是「公网到公网」，而 etcd 走的是内网那一条 ——
    # 安全组规则通常也只管后者，探错了等于没探。
    from . import node_address as node_address_module

    addresses = {}
    for ip in members:
        node = sessions[ip].node
        entered = str(getattr(node, 'ip_address', '') or ip).strip()
        ok, out, _err = sessions[ip].run(node_address_module.PROBE_COMMAND)
        route_source, probed = node_address_module.parse_probe(out if ok else '')
        chosen = node_address_module.choose_cluster_address(entered, route_source, probed)
        addresses[ip] = {
            'entered': entered,
            'cluster_address': chosen or entered,
            'addresses': probed,
        }

    detail = {'etcd_nodes': members, 'probe_ports': list(ETCD_REACH_PORTS), 'nodes': addresses}
    failures = []
    unverified = []
    checked = []

    for source_ip in members:
        targets = []
        for target_ip in members:
            if target_ip == source_ip:
                continue
            for port in ETCD_REACH_PORTS:
                targets.append((addresses[target_ip]['cluster_address'], port))
        if not targets:
            continue
        ok, out, err = sessions[source_ip].run(_etcd_reach_command(targets))
        if not ok:
            unverified.append(f'{source_ip} 上无法执行探测（{out or err or "无输出"}）')
            continue
        if 'NO_BASH' in out:
            unverified.append(f'{source_ip} 上没有 bash，无法做端口可达性探测')
            continue
        found = _parse_etcd_probe(out)
        for target_ip in members:
            if target_ip == source_ip:
                continue
            address = addresses[target_ip]['cluster_address']
            for port in ETCD_REACH_PORTS:
                label = f'{source_ip}->{address}:{port}'
                marker = found.get(f'{address}:{port}', '')
                checked.append(label)
                if marker == PROBE_DROPPED:
                    failures.append(label)
                elif marker not in (PROBE_OPEN, PROBE_REFUSED):
                    unverified.append(f'{label} 探测结果无法判定（{marker or "无输出"}）')

    detail['checked_pairs'] = checked
    ports = '、'.join(str(port) for port in ETCD_REACH_PORTS)
    # 提示里两个地址都给出：规则要按**内网地址**写（那才是 etcd 之间实际走的那条路），
    # 而操作员在云控制台上认得的是自己填进平台的公网地址，只给一个都要多绕一圈。
    members_text = '、'.join(
        addresses[ip]['cluster_address']
        if addresses[ip]['cluster_address'] == addresses[ip]['entered']
        else f'{addresses[ip]["entered"]}（内网 {addresses[ip]["cluster_address"]}）'
        for ip in sorted(members)
    )

    if failures:
        listed = '；'.join(failures[:6])
        more = f'（共 {len(failures)} 处）' if len(failures) > 6 else ''
        return _make_check(
            'etcd_network', title, 'failed',
            f'etcd 成员之间的 {ports} 端口有不通的链路，包被丢弃而不是被拒绝，'
            f'说明沿途有安全组、VPC 网络 ACL 或主机防火墙挡着：{listed}{more}。'
            f'请在云安全组/VPC 里放行 {members_text} 这些地址之间（或整个内网网段）的 {ports} 端口，'
            f'放行后重新预检。装完 etcd 也不会自动通 —— 那两条链路是 etcd 选主与读写依赖的。',
            detail, nodes=members,
        )
    if unverified:
        return _make_check(
            'etcd_network', title, 'warned',
            'etcd 节点之间的端口可达性未能全部验证：' + '；'.join(unverified[:6]),
            detail, nodes=members,
        )
    return _make_check(
        'etcd_network', title, 'passed',
        f'{len(members)} 台 etcd 成员之间的 {ports} 端口均可达'
        f'（部署前端口上还没有进程，返回「连接被拒绝」即表示网络已放行）',
        detail, nodes=members,
    )


def _check_network(deployment, sessions, reachable):
    """检查项 8：网卡与节点间 TCP 互通。

    先确认配置的网卡在节点上真实存在且管理态是 UP——kubeasz 会把该网卡名写进 inventory，
    写错或网卡没启用都是部署中途失败，因此判硬失败。再逐个有序节点对做一次 TCP 连通性
    探测，不通只给警告。

    这里探的是对端的 SSH 端口而不是 ICMP：生产环境禁 ping 很常见，而 kubeasz 需要的本来
    就是对端某个 TCP 端口可达；SSH 端口是此刻唯一确定在监听、且不依赖集群是否已部署的端口。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在目标节点上执行 ``ip link show``、``ip -o -f inet addr show`` 与一次 TCP 连接。
    """
    if not reachable:
        return _skipped_without_nodes('network', sessions)

    interface = str(deployment.network_interface or '').strip()
    # 网卡名会被拼进远程命令，先做白名单校验，避免命令注入。
    if not re.match(r'^[A-Za-z0-9_.:@-]+$', interface):
        return _make_check(
            'network', CHECK_TITLES['network'], 'failed',
            f'节点网口名称「{interface}」不合法，只允许字母、数字与 _ . : @ -',
            {'network_interface': interface}, nodes=reachable,
        )

    command = _interface_state_command(interface)
    detail = {'network_interface': interface}
    nodes_detail = {}
    missing = []
    for ip in reachable:
        ok, out, err = sessions[ip].run(command)
        if not ok:
            message = f'无法检查网卡：{out or err or "无输出"}'
            nodes_detail[ip] = {'ok': False, 'message': message}
            missing.append(f'{ip}：{message}')
            continue
        if 'IFACE_UP' in out:
            nodes_detail[ip] = {'ok': True, 'interface_state': 'UP', 'message': f'网卡 {interface} 存在且处于 UP'}
        elif 'IFACE_DOWN' in out:
            message = f'网卡 {interface} 存在但未启用（管理态非 UP），请先 ip link set {interface} up'
            nodes_detail[ip] = {'ok': False, 'interface_state': 'DOWN', 'message': message}
            missing.append(f'{ip}：{message}')
        else:
            message = f'网卡 {interface} 在节点上不存在，请确认网口名称'
            nodes_detail[ip] = {'ok': False, 'interface_state': 'MISSING', 'message': message}
            missing.append(f'{ip}：{message}')

    # 顺带把节点的网段记下来，供网段合法性检查复用（命令结果在会话内有缓存）。
    prefixes = {}
    for ip in reachable:
        networks = _node_prefixes(sessions[ip])
        prefixes[ip] = [str(network) for network in networks]
    detail['node_networks'] = prefixes

    tcp_failures = []
    checked_pairs = []
    for source_ip in reachable:
        source_port = _ssh_port(sessions[source_ip])
        for target_ip in reachable:
            if source_ip == target_ip:
                continue
            # 只校验 IP 字面量与端口范围，避免把任意字符串拼进远程命令。
            try:
                ipaddress.ip_address(target_ip)
            except ValueError:
                continue
            target_port = _ssh_port(sessions[target_ip])
            pair = f'{source_ip}->{target_ip}:{target_port}'
            if not source_port or not target_port:
                tcp_failures.append(f'{pair} 两端 SSH 端口未知，未能验证')
                continue
            probe_command = _tcp_probe_command(target_ip, target_port)
            ok, out, _err = sessions[source_ip].run(probe_command)
            marker = out.strip().splitlines()[-1].strip() if out.strip() else ''
            checked_pairs.append(pair)
            if not ok:
                tcp_failures.append(f'{pair} 无法执行 TCP 探测')
            elif marker == 'TCP_UNKNOWN':
                tcp_failures.append(f'{pair} 节点缺少 nc 与 bash，未能验证')
            elif marker == 'TCP_FAIL':
                tcp_failures.append(f'{pair} TCP 不通')
    detail['nodes'] = nodes_detail
    detail['tcp_checked_pairs'] = checked_pairs

    if missing:
        return _make_check(
            'network', CHECK_TITLES['network'], 'failed', '；'.join(missing), detail, nodes=list(nodes_detail.keys()),
        )
    if tcp_failures:
        return _make_check(
            'network', CHECK_TITLES['network'], 'warned',
            '网卡检查通过，但存在 TCP 不通的节点组合：' + '；'.join(tcp_failures),
            detail, nodes=list(nodes_detail.keys()),
        )
    return _make_check(
        'network', CHECK_TITLES['network'], 'passed',
        f'{len(nodes_detail)} 台节点的网卡 {interface} 均为 UP，'
        f'{len(checked_pairs)} 组节点对的双向 TCP 均可达',
        detail, nodes=list(nodes_detail.keys()),
    )


def _check_cidr(deployment, sessions, reachable):
    """检查项 9：网段合法性。

    校验 ``pod_cidr`` / ``service_cidr`` / ``api_server_port`` 本身合法、pod 与 service
    两段互不重叠，并且都不与节点所在网段重叠。重叠会让容器网段和主机网段打架，
    属于硬失败。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在可达节点上读取接口网段；不修改目标机器状态。
    """
    detail = {
        'pod_cidr': deployment.pod_cidr,
        'service_cidr': deployment.service_cidr,
        'api_server_port': deployment.api_server_port,
    }
    node_ips = [session.ip for session in sessions.values()]
    failures = []

    try:
        port = int(deployment.api_server_port)
        if port < 1 or port > 65535:
            failures.append(f'ApiServer 端口 {port} 超出 1-65535 范围')
        else:
            detail['api_server_port'] = port
    except (TypeError, ValueError):
        failures.append(f'ApiServer 端口「{deployment.api_server_port}」不是合法整数')

    networks = {}
    for field, label in (('pod_cidr', '容器网络 CIDR'), ('service_cidr', 'Service IP 段')):
        raw = str(getattr(deployment, field) or '').strip()
        if not raw:
            failures.append(f'{label} 未填写')
            continue
        try:
            networks[field] = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            failures.append(f'{label}「{raw}」不是合法的网段（形如 10.244.0.0/16）')

    pod = networks.get('pod_cidr')
    service = networks.get('service_cidr')
    if pod is not None and service is not None and pod.overlaps(service):
        failures.append(f'容器网络 {pod} 与 Service 网段 {service} 重叠，请拆分为互不包含的两段')

    # 节点所在网段优先取实机接口地址；读不到时按 /24 估算，并在明细里注明。
    node_networks = {}
    ignored_interfaces = []
    for ip in reachable:
        prefixes = _node_prefixes(sessions[ip], ignored=ignored_interfaces)
        if prefixes:
            node_networks[ip] = prefixes
            continue
        try:
            node_networks[ip] = [ipaddress.ip_network(f'{ip}/24', strict=False)]
            detail.setdefault('estimated_node_networks', []).append(ip)
        except ValueError:
            continue
    if not reachable:
        detail['node_network_note'] = '所有节点 SSH 不可用，未能获取节点所在网段，仅校验 CIDR 自身合法性'

    for ip, prefixes in node_networks.items():
        for field, label in (('pod_cidr', '容器网络'), ('service_cidr', 'Service 网段')):
            network = networks.get(field)
            if network is None:
                continue
            for prefix in prefixes:
                if network.overlaps(prefix):
                    failures.append(f'{label} {network} 与节点 {ip} 所在网段 {prefix} 重叠')

    detail['node_networks'] = {ip: [str(item) for item in prefixes] for ip, prefixes in node_networks.items()}
    if ignored_interfaces:
        # 把被排除的虚拟网卡写进明细：这一项通过时操作员能看出「那不是没查，是刻意不算」，
        # 否则同一台机器上次部署报重叠、这次不报，会被当成时好时坏。
        detail['ignored_virtual_interfaces'] = sorted(ignored_interfaces)

    if failures:
        return _make_check(
            'cidr', CHECK_TITLES['cidr'], 'failed', '；'.join(failures), detail, nodes=node_ips,
        )
    ignored_note = ''
    if ignored_interfaces:
        ignored_note = f'（已忽略集群虚拟网卡 {", ".join(sorted(ignored_interfaces))} 上的地址）'
    return _make_check(
        'cidr', CHECK_TITLES['cidr'], 'passed',
        f'CIDR 与端口合法，且与节点所在网段无重叠{ignored_note}', detail, nodes=node_ips,
    )


def _loadbalancer_sessions(sessions):
    """挑出本次任务节点里的负载均衡机器会话。

    参数：`sessions` 为 ``{ip: _NodeSession}`` 映射。
    返回：返回按 IP 升序排列的会话列表；没有负载均衡机器时返回空列表。
    副作用：纯计算，不修改持久化数据。
    """
    items = [
        (ip, session) for ip, session in sessions.items() if LB_ROLE in _node_roles(session.node)
    ]
    return [session for _ip, session in sorted(items)]


def _loadbalancer_vip(deployment):
    """返回本次任务的外部访问地址（自建模式是 VIP，云模式是云负载均衡器的地址）。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回去掉空白后的地址；未启用时为（或读不到时）空串。
    副作用：纯计算，不修改持久化数据。
    """
    return str(getattr(deployment, 'lb_vip', '') or '').strip()


def _lb_mode(deployment):
    """返回本次任务的外部访问入口模式。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回 ``none``/``self``/``cloud`` 之一；读不到时返回 ``none``。
    副作用：纯计算，不修改持久化数据。

    与 ``compiler.lb_mode`` 同一口径，但这里不 import 编译器：预检只想知道「要不要连机器」，
    不想把整条渲染链路拖进来。认不出来的值当 ``none``，与编译器一致。
    """
    value = str(getattr(deployment, 'lb_mode', '') or '').strip().lower()
    if value in ('self', 'cloud'):
        return value
    return 'none'


def _lb_address_failures(deployment, sessions, vip, label, missing):
    """校验外部访问地址本身，返回失败原因列表。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`vip` 为地址文本；
        `label` 为报错时用的中文说法（「VIP」或「云内网负载均衡地址」）；
        `missing` 为地址为空时的整句说明（两种模式该做的事不一样，不能共用一句话）。
    返回：返回 ``(失败原因列表, IPv4 地址对象或 None)``。
    副作用：纯计算，不修改持久化数据。

    两种模式共用：不管后面是 keepalived 还是云 LB，地址本身都要是一个能用的 IPv4 单播地址、
    不能与任何节点已有的地址重复、不能落在 Pod/Service 网段里。自建模式接下去还要连机器查
    网段与占用；云模式到这里就结束了 —— 云 LB 不在节点上落地，那些检查没有对象。
    """
    failures = []
    if not vip:
        failures.append(missing)
        return failures, None
    try:
        address = ipaddress.ip_address(vip)
    except ValueError:
        failures.append(f'{label}「{vip}」不是合法的 IP 地址')
        return failures, None
    if address.version != 4:
        failures.append(f'{label}「{vip}」不是 IPv4 地址，平台目前只支持 IPv4 的地址')
        return failures, None
    if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local:
        failures.append(f'{label}「{vip}」不能是环回、组播、未指定或链路本地地址')
        return failures, None

    used = {}
    for session in sessions.values():
        for field in ('ip_address', 'cluster_ip'):
            value = str(getattr(session.node, field, '') or '').strip()
            if value:
                used.setdefault(value, []).append(f'{session.ip}（{field}）')
    if vip in used:
        failures.append(f'{label} {vip} 与节点已有的地址重复：{"、".join(used[vip])}')

    for field, name in (('pod_cidr', '容器网络'), ('service_cidr', 'Service 网段')):
        raw = str(getattr(deployment, field) or '').strip()
        if not raw:
            continue
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            # 网段本身不合法由「网段合法性」那一项去报，这里不重复报同一个问题。
            continue
        if address in network:
            failures.append(f'{label} {vip} 落在{name} {network} 里，会与集群内部地址冲突')
    return failures, address


def _check_cloud_lb(deployment, sessions, reachable):
    """检查项：云内网负载均衡。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：不执行任何远程命令，只读本地清单。

    云内网负载均衡模式下平台什么都不装，这一项能验的只有「写进 apiserver 证书 SAN 的地址
    是否可用」：地址合法、不与节点重复、不落进集群内部网段。代理本身（监听端口、后端、
    健康检查）在云控制台上，平台从集群侧看不到也管不着，因此只给一条 ``warned`` 提示，
    把该核对的三件事写清楚，不阻断部署。

    **这里刻意不连任何机器**：自建模式那些检查（VIP 是否被占用、机器上有没有包含 VIP 的网段、
    两台机器是否同二层）都是为了让 keepalived 能把 VIP 挂到正确的网卡上，云 LB 不在节点上
    落地，这些项要么必然通过要么没有对象。``reachable`` 参数留着是为了与其它检查项同一签名。
    """
    title = CHECK_TITLES['external_lb']
    vip = _loadbalancer_vip(deployment)
    node_ips = [session.ip for session in sessions.values()]
    port = int(getattr(deployment, 'api_server_port', 0) or 6443)
    masters = [
        session for session in sessions.values()
        if _node_roles(session.node) & {'master', 'control-plane'}
    ]
    # 后端清单给操作员照抄到云控制台：地址取自节点的 ``ip_address``（用户填的内网地址），
    # 不是 ``cluster_address``（那可能是为 SSH 指定的旁路地址）。
    targets = []
    for session in sorted(masters, key=lambda item: item.ip):
        address = str(getattr(session.node, 'ip_address', '') or session.ip).strip()
        if address:
            targets.append(f'{address}:{port}')
    detail = {'mode': 'cloud', 'vip': vip, 'port': port, 'backend_targets': targets}

    if str(getattr(deployment, 'task_type', '') or '') not in DEPLOY_TASK_TYPES:
        # 扩容/升级是从集群派生的任务，节点清单里只有本次要动的机器（加 Node 时甚至一台控制面
        # 都没有），拿它数控制面必然不足；地址在装集群签证书时就定下来了，这类任务也改不了它。
        # 与自建模式「清单里没有负载均衡机器就跳过」是同一个理由，否则一次正常扩容会被预检挡死。
        return _make_check(
            'external_lb', title, 'skipped',
            '本次任务的节点清单不是整个集群，云负载均衡的地址与控制面数量在新建集群时校验，本项跳过',
            detail, nodes=node_ips,
        )

    failures, _address = _lb_address_failures(
        deployment, sessions, vip, '云内网负载均衡地址',
        '选择了云内网负载均衡但没有填写云负载均衡器的地址',
    )
    if failures:
        return _make_check(
            'external_lb', title, 'failed', '；'.join(failures), detail, nodes=node_ips,
        )

    if len(masters) < LB_MIN_MASTERS:
        # 只有一台控制面时，负载均衡后面只有一个后端，地址进 SAN 也换不来高可用。
        return _make_check(
            'external_lb', title, 'failed',
            f'云内网负载均衡面向多节点集群，至少需要 {LB_MIN_MASTERS} 个控制面节点，'
            f'当前只有 {len(masters)} 个',
            detail, nodes=node_ips,
        )

    described = '、'.join(targets) or '（没有可用的控制面地址）'
    return _make_check(
        'external_lb', title, 'warned',
        f'云内网负载均衡由云厂商提供，平台不安装任何代理、也无法从集群侧验证：'
        f'请确认已在云控制台把三台 master 的 {described} 配成 TCP 后端（监听端口必须是 {port}），'
        f'健康检查用 TCP {port}；部署后在集群外执行 '
        f'curl -k https://{vip}:{port}/healthz 应返回 ok',
        detail, nodes=node_ips,
    )


def _interface_addresses(session):
    """读取节点上处于 global 作用域的 IPv4 地址明细。

    参数：`session` 表示节点会话。
    返回：返回 ``[{'name', 'address', 'network', 'virtual'}]``；命令执行失败时返回 None。
    副作用：在目标节点上执行 ``ip -o -f inet addr show scope global``，只读。

    与 ``_node_prefixes`` 看着像，但用途不同、返回的东西也不同：那个函数把虚拟网卡滤掉、
    只返回网段，用于判断「节点属于哪个网段」；这里要的是**接口名**与**地址本身** ——
    keepalived 把 VIP 挂到哪个接口上由 kubeasz 在机器上现探测，平台只能把「哪些接口的网段
    包含 VIP」数出来提示操作员；判断 VIP 是否已被占用也必须比地址、不能比网段
    （同网段里有别的地址是正常的）。两件事各跑一次命令是浪费，所以合并在这里，
    虚拟网卡按同一个 ``_VIRTUAL_INTERFACE_PATTERNS`` 标注出来而不是丢掉。
    """
    ok, out, _err = session.run('ip -o -f inet addr show scope global 2>/dev/null')
    if not ok:
        return None
    entries = []
    for match in VIRTUAL_INTERFACE_LINE.finditer(out):
        name = match.group('name') or ''
        cidr = match.group('cidr')
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        entries.append({
            'name': name,
            'address': cidr.split('/', 1)[0],
            'network': network,
            'virtual': any(pattern.match(name) for pattern in _VIRTUAL_INTERFACE_PATTERNS),
        })
    return entries


def _check_external_lb(deployment, sessions, reachable):
    """检查项：外部负载均衡（VIP 与负载均衡机器）。

    检查 VIP 本身合法、未被任何节点占用、不与节点地址及 Pod/Service 网段冲突；每台负载均衡
    机器上都有包含 VIP 的网段（否则备机接管时 VIP 无处落地）；两台负载均衡机器在同一个二层
    网段里。前几项只看清单，后几项要连机器。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在可达节点上执行只读的接口地址查询，不修改目标机器状态。

    这一项回答的是「这个 VIP 装得上吗」，**不是**「VIP 现在通不通」：部署前 VIP 还不存在，
    从节点去连它必然失败。keepalived 走的是 VRRP（IP 协议号 112），没有 TCP 端口可探，
    所以负载均衡机器之间的连通性交给 ``network`` 项的对等 TCP 探测，这里不再重复。

    「VIP 已被占用」这一条要分清是谁挂上去的：第一次部署没跑完时，本平台装下的 keepalived
    已经把 VIP 抢在自己网卡上了，重试再看到它就判失败，等于让一次重试永远过不去（94 上的
    k8s-cluster 正是这样）。这种能认出来的占用改成警告；认不出来的照旧硬失败，理由见
    ``_own_keepalived_vip_state``。
    """
    title = CHECK_TITLES['external_lb']
    detail = {'vip': _loadbalancer_vip(deployment)}
    node_ips = [session.ip for session in sessions.values()]
    mode = _lb_mode(deployment)
    if mode == 'none':
        return _make_check(
            'external_lb', title, 'skipped', '未启用外部负载均衡，本项跳过', detail, nodes=node_ips,
        )
    if mode == 'cloud':
        # 云模式是另一套检查：平台不装东西，也不该为了「验证」去连机器。
        return _check_cloud_lb(deployment, sessions, reachable)
    lb_sessions = _loadbalancer_sessions(sessions)
    if not lb_sessions:
        # 扩容/升级/清理是从集群派生的任务，``cluster_ops.inherited_options`` 会把 VIP 继承
        # 下来，但它们的节点清单里只有本次要动的机器，没有负载均衡机器。这里跳过而不是判失败，
        # 否则一个正常的扩容会被预检挡死。
        return _make_check(
            'external_lb', title, 'skipped',
            '本次任务的节点清单里没有负载均衡机器，本项跳过', detail, nodes=node_ips,
        )

    failures = []
    warnings = []
    detail['loadbalancer_nodes'] = [session.ip for session in lb_sessions]

    vip = detail['vip']
    # 地址本身的三类问题（合法/不重复/不落在集群网段里）两种模式一样，收在共用的辅助函数里。
    address_failures, address = _lb_address_failures(
        deployment, sessions, vip, 'VIP', '启用了外部负载均衡但没有填写 VIP',
    )
    failures.extend(address_failures)

    masters = [item for item in sessions.values() if _node_roles(item.node) & {'master', 'control-plane'}]
    if len(masters) < LB_MIN_MASTERS:
        failures.append(
            f'外部负载均衡面向多节点集群，至少需要 {LB_MIN_MASTERS} 个控制面节点，'
            f'当前只有 {len(masters)} 个'
        )

    # 下面要连机器。只读可达节点：连不上的机器由 SSH 那一项说明，这里不重复报同一件事。
    entries = {}
    unreadable = []
    for ip in reachable:
        items = _interface_addresses(sessions[ip])
        if items is None:
            unreadable.append(ip)
            continue
        entries[ip] = items
    if unreadable:
        warnings.append('以下节点无法读取接口地址，相关检查未完成：' + '、'.join(sorted(unreadable)))

    occupied = []
    owned = []
    for ip in sorted(entries):
        hits = [item for item in entries[ip] if vip and item['address'] == vip]
        if not hits:
            continue
        # 地址真的挂在这台机器上时才去读它自己的 keepalived 配置：没有冲突就不连机器。
        state = _own_keepalived_vip_state(_read_self_install_trace(sessions[ip]), vip)
        if state:
            owned.append(
                f'{ip} 的接口 {"、".join(item["name"] for item in hits)} 上这个地址是本平台上一次'
                f'安装留下的（keepalived 配置里写的就是它，服务当前 {state}），重新部署会原地接管'
            )
            continue
        for item in hits:
            occupied.append(f'{ip} 的接口 {item["name"]}')
    if occupied:
        failures.append(
            f'VIP {vip} 已被占用（{"、".join(occupied)}）：这台机器上已经有了同一个地址，'
            '可能是别的 keepalived 正在抢，或上次清理没清干净'
        )
    warnings.extend(owned)

    candidates = {}
    lb_networks = {}
    for session in lb_sessions:
        if session.ip not in entries:
            continue
        items = entries[session.ip]
        hit = [item for item in items if address is not None and address in item['network']]
        candidates[session.ip] = [
            {'interface': item['name'], 'network': str(item['network']), 'virtual': item['virtual']}
            for item in hit
        ]
        lb_networks[session.ip] = [str(item['network']) for item in items if not item['virtual']]
        if address is not None and not hit:
            failures.append(
                f'负载均衡机器 {session.ip} 上没有任何网段包含 VIP {vip}，'
                'VIP 接管后无法在这台机器上落地（VIP 必须是承载它的内网网段里的地址）'
            )

    # keepalived 用哪个网卡由 kubeasz 在机器上用 ``ip a | grep <地址>`` 现探测（``set_fact`` 是无
    # 条件的，平台没有变量可以覆盖它），所以这里只能把可疑情况提示出来，不能替它决定。
    for ip in sorted(candidates):
        items = candidates[ip]
        if not items:
            continue
        names = '、'.join(item['interface'] for item in items)
        if len(items) > 1:
            warnings.append(
                f'{ip} 上有多个接口的网段包含 VIP（{names}），keepalived 可能把 VIP 挂到非内网网卡上，'
                '部署后用 ip a 复核'
            )
        elif items[0]['virtual']:
            warnings.append(
                f'{ip} 上包含 VIP 的只有虚拟网卡 {names}，keepalived 可能把 VIP 挂到它上面，'
                '部署后用 ip a 复核'
            )

    shared = None
    for ip in sorted(lb_networks):
        prefixes = set(lb_networks[ip])
        shared = prefixes if shared is None else (shared & prefixes)
    if lb_networks and shared is not None and not shared:
        described = '；'.join(
            f'{ip}: {"、".join(sorted(lb_networks[ip])) or "无内网网段"}' for ip in sorted(lb_networks)
        )
        warnings.append(
            f'负载均衡机器不在同一个二层网段（{described}）：VIP 漂移到另一台后无法在它所在的'
            '网段应答 ARP，只有两台都在同一个广播域里 VIP 才真的可用'
        )

    detail['vip_candidates'] = candidates
    detail['loadbalancer_networks'] = lb_networks
    if occupied:
        detail['occupied'] = occupied
    if owned:
        detail['claimed'] = owned
    if failures:
        return _make_check(
            'external_lb', title, 'failed', '；'.join(failures + warnings), detail, nodes=node_ips,
        )
    if warnings:
        return _make_check(
            'external_lb', title, 'warned', '；'.join(warnings), detail, nodes=node_ips,
        )
    return _make_check(
        'external_lb', title, 'passed',
        f'VIP {vip} 合法且未被占用，{len(lb_sessions)} 台负载均衡机器都在它的网段内',
        detail, nodes=node_ips,
    )


def _etcd_quorum_command():
    """拼出读取 etcd 仲裁状态的远程命令。

    参数：无。
    返回：返回可在目标节点上执行的单行 shell 命令文本。
    副作用：纯计算，不访问网络。

    在**节点本机**用 etcdctl 查，而不是从平台机直连 2379：etcd 的客户端证书是给集群内部
    签发的，平台机没有对应的客户端身份；而且很多部署只对集群内网开放 2379。

    输出被压成便于解析的标记行：``ETCDCTL_MISSING``/``ETCD_CERT_MISSING`` 表示查不了，
    ``HEALTHY:n``/``UNHEALTHY:n`` 是集群视角的健康成员数（etcdctl 支持 ``--cluster`` 时），
    ``MEMBERS:n`` 是成员总数，``SELF:`` 是本机自身端点的健康结论（作为兜底）。
    """
    candidates = ' '.join(ETCDCTL_CANDIDATES)
    return (
        f'SSL="{ETCD_SSL_DIR}"; ETCDCTL=""; '
        f'for c in etcdctl {candidates}; do '
        'if command -v "$c" >/dev/null 2>&1; then ETCDCTL=$(command -v "$c"); break; fi; '
        'done; '
        'if [ -z "$ETCDCTL" ]; then echo ETCDCTL_MISSING; exit 0; fi; '
        'if [ ! -f "$SSL/ca.pem" ] || [ ! -f "$SSL/etcd.pem" ] || [ ! -f "$SSL/etcd-key.pem" ]; then '
        'echo ETCD_CERT_MISSING; exit 0; fi; '
        f'TLS="--cacert=$SSL/ca.pem --cert=$SSL/etcd.pem --key=$SSL/etcd-key.pem"; '
        f'EP="https://127.0.0.1:{ETCD_CLIENT_PORT}"; '
        f'OUT=$($ETCDCTL $TLS --endpoints="$EP" --command-timeout={ETCD_QUERY_TIMEOUT} '
        'endpoint health --cluster 2>/dev/null); '
        'if [ -n "$OUT" ]; then '
        'echo "HEALTHY:$(echo "$OUT" | grep -c "is healthy")"; '
        'echo "UNHEALTHY:$(echo "$OUT" | grep -c "is unhealthy")"; '
        'fi; '
        f'MEMBERS=$($ETCDCTL $TLS --endpoints="$EP" --command-timeout={ETCD_QUERY_TIMEOUT} '
        'member list 2>/dev/null | grep -c "started"); '
        'echo "MEMBERS:$MEMBERS"; '
        f'SELF=$($ETCDCTL $TLS --endpoints="$EP" --command-timeout={ETCD_QUERY_TIMEOUT} '
        'endpoint health 2>/dev/null); '
        'case "$SELF" in '
        '*"is healthy"*) echo "SELF:healthy";; '
        '*"is unhealthy"*) echo "SELF:unhealthy";; '
        '*) echo "SELF:unknown";; '
        'esac'
    )


def parse_etcd_quorum(text):
    """解析 etcd 仲裁探测的输出。

    参数：`text` 表示远程命令的标准输出。
    返回：返回 ``{'members': int|None, 'healthy': int|None, 'unhealthy': int|None,
        'self': 'healthy'|'unhealthy'|'unknown', 'reason': str}``。
    副作用：纯计算，不修改任何状态。

    ``members`` 为 ``None`` 表示连成员数量都没读到，此时结论只能是「无法判定」——
    集群已经不可用时也会是这个结果，不能把它当成「仲裁丢失」，否则恢复类任务会被
    一个本就坏掉的集群永远挡在门外（规划 11.1）。
    """
    text = str(text or '')
    state = {'members': None, 'healthy': None, 'unhealthy': None, 'self': 'unknown', 'reason': ''}
    if 'ETCDCTL_MISSING' in text:
        state['reason'] = '目标节点上找不到 etcdctl，无法查询 etcd 成员状态'
        return state
    if 'ETCD_CERT_MISSING' in text:
        state['reason'] = f'目标节点缺少 {ETCD_SSL_DIR} 下的 etcd 客户端证书，无法查询 etcd 成员状态'
        return state

    for line in text.splitlines():
        line = line.strip()
        if line.startswith('MEMBERS:'):
            try:
                state['members'] = int(line.split(':', 1)[1].strip())
            except ValueError:
                state['members'] = None
        elif line.startswith('HEALTHY:'):
            try:
                state['healthy'] = int(line.split(':', 1)[1].strip())
            except ValueError:
                state['healthy'] = None
        elif line.startswith('UNHEALTHY:'):
            try:
                state['unhealthy'] = int(line.split(':', 1)[1].strip())
            except ValueError:
                state['unhealthy'] = None
        elif line.startswith('SELF:'):
            state['self'] = line.split(':', 1)[1].strip() or 'unknown'

    if state['members'] is None:
        state['reason'] = '未能通过 etcdctl 读到成员列表，etcd 可能已经不可用'
        return state
    if state['healthy'] == 0 and state['unhealthy'] == 0:
        # etcdctl 版本不支持 endpoint health --cluster：HEALTHY/UNHEALTHY 两行都不会出现。
        state['healthy'] = None
        state['unhealthy'] = None
    if state['healthy'] is None and state['self'] == 'unknown':
        state['reason'] = 'etcdctl 未返回可判定的健康结论，可能是版本过旧或本机 etcd 无响应'
    return state


def etcd_quorum_conclusion(state):
    """把仲裁探测结果收敛成一句结论与严重程度。

    参数：`state` 表示 ``parse_etcd_quorum`` 的返回值。
    返回：返回 ``(status, message)``，status 取 ``passed``/``warned``/``failed``。
    副作用：纯计算，不修改任何状态。

    健康成员数少于「成员数 / 2 + 1」才算真正丢失仲裁——那是一个已经不能写入的集群，
    继续升级或扩容只会让它更难看。成员不全但仍有仲裁只给警告：HA 集群的常见运维动作
    （换机、加机器）本来就会短暂少一个成员，把它判死会让正常运维无法进行。
    """
    members = state.get('members')
    if members is None:
        return 'warned', state.get('reason') or '未能判定 etcd 仲裁状态'

    quorum = members // 2 + 1
    healthy = state.get('healthy')
    if healthy is None:
        if state.get('self') == 'healthy':
            # 只确认了本机自己，成员级结论缺失，不足以判死。
            return 'warned', (
                f'etcd 有 {members} 个成员，本机端点健康，但未能取得各成员的健康明细；'
                f'仲裁需要 {quorum} 个成员，请人工确认'
            )
        if state.get('self') == 'unhealthy':
            return 'failed', (
                f'etcd 有 {members} 个成员，仲裁需要 {quorum} 个，但本机端点报告不健康，'
                '本机所在的 etcd 很可能已失去仲裁'
            )
        return 'warned', state.get('reason') or '未能判定 etcd 仲裁状态'

    if healthy >= quorum:
        if healthy == members:
            return 'passed', f'etcd {members} 个成员全部健康，仲裁正常（需要 {quorum} 个）'
        return 'warned', (
            f'etcd {members} 个成员中有 {healthy} 个健康，仍满足仲裁所需的 {quorum} 个，'
            '但成员数不全，请确认异常成员是否需要处理'
        )
    return 'failed', (
        f'etcd {members} 个成员中只有 {healthy} 个健康，低于仲裁所需的 {quorum} 个，'
        '集群当前已无法保证一致性写入，请先把 etcd 恢复到有仲裁的状态'
    )


def _check_etcd_quorum(deployment, sessions, reachable):
    """检查项 10：etcd 仲裁。

    只在**已存在的集群**上做（新建集群还没有 etcd），并且只在本次任务涉及 etcd 或
    control-plane 节点时做——kubeasz 的 etcd 成员都在这些机器上，从一台 worker 上
    查 etcd 既没有证书也连不上（规划 10.2、10.4、11.2）。

    参数：`deployment` 为 K8sDeployment 实例；`sessions` 为会话映射；`reachable` 为可达节点 IP 列表。
    返回：返回检查项字典。
    副作用：在可达的 etcd/control-plane 节点上执行只读的 etcdctl 查询命令。
    """
    if not deployment.cluster_id:
        return _make_check(
            'etcd_quorum', CHECK_TITLES['etcd_quorum'], 'skipped',
            '新建集群此时还没有 etcd，无需检查仲裁',
        )

    candidates = etcd_candidates(sessions, reachable)
    if not candidates:
        return _make_check(
            'etcd_quorum', CHECK_TITLES['etcd_quorum'], 'skipped',
            '本次任务的节点都不是 etcd/control-plane，无法就近查询 etcd 仲裁，请确认控制面状态',
            {'cluster': deployment.cluster_slug},
        )

    state, detail = probe_etcd_from_sessions(candidates)
    detail['cluster'] = deployment.cluster_slug
    node_ips = [session.ip for session in candidates]
    if state is None:
        return _make_check(
            'etcd_quorum', CHECK_TITLES['etcd_quorum'], 'warned',
            detail.get('reason') or '所有 etcd/control-plane 节点都未能执行 etcdctl 查询',
            detail, nodes=node_ips,
        )

    detail.update(state)
    status, message = etcd_quorum_conclusion(state)
    return _make_check(
        'etcd_quorum', CHECK_TITLES['etcd_quorum'], status, message, detail, nodes=node_ips,
    )


def etcd_candidates(sessions, reachable):
    """挑出可以就近查询 etcd 的会话。

    参数：`sessions` 为 ``{ip: _NodeSession}`` 映射；`reachable` 为可达节点 IP 列表。
    返回：返回候选会话列表。
    副作用：纯计算，不建立连接。
    """
    return [
        sessions[ip] for ip in reachable
        if {'etcd', 'master', 'control-plane'} & set(_node_roles(sessions[ip].node))
    ]


def probe_etcd_from_sessions(candidates):
    """依次在候选节点上查询 etcd 仲裁，返回第一个可判定的结论。

    参数：`candidates` 为 ``_NodeSession`` 列表。
    返回：返回 ``(state, detail)``；``state`` 为 ``parse_etcd_quorum`` 的返回值，
        所有节点都查不出来时为 None。
    副作用：在候选节点上执行只读的 etcdctl 查询命令。

    读不到成员列表是**这台机器**的问题（缺 etcdctl、缺证书、本机 etcd 无响应），换下一台
    etcd 节点接着查；一旦读到成员列表，结论就是集群级的，不必再问下一台。
    """
    detail = {'probed': []}
    state = None
    reason = ''
    for session in candidates:
        ok, out, err = session.run(_etcd_quorum_command())
        if not ok:
            detail['probed'].append({'ip': session.ip, 'error': err})
            continue
        parsed = parse_etcd_quorum(out)
        entry = {'ip': session.ip, 'output': out.strip()[:400]}
        if parsed['reason']:
            entry['reason'] = parsed['reason']
        detail['probed'].append(entry)
        if parsed['members'] is not None:
            state = parsed
            break
        reason = parsed['reason'] or reason
    if state is None:
        detail['reason'] = reason or '所有 etcd/control-plane 节点都未能执行 etcdctl 查询'
    return state, detail


def etcd_quorum_state(nodes, ssh_credentials):
    """给预检之外的调用方直接用的一次 etcd 仲裁探测。

    参数：`nodes` 为 K8sDeploymentNode 列表；`ssh_credentials` 为 ``{ip: {...}}`` 映射。
    返回：返回 ``(state, detail)``，含义与 ``probe_etcd_from_sessions`` 一致。
    副作用：建立 SSH 连接、执行只读命令并在结束时关闭连接。

    恢复流程要在动手之前判断集群健不健康（规划 11.1），而那时预检还没跑，因此这里
    单独开一次短连接。连不上任何节点时返回 ``(None, detail)``，调用方按「判不了」处理。
    """
    sessions = {}
    candidates = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '').strip()
        if not ip:
            continue
        session = _NodeSession(node, (ssh_credentials or {}).get(ip) or {})
        sessions[ip] = session
        if {'etcd', 'master', 'control-plane'} & set(_node_roles(node)):
            candidates.append(session)
    if not candidates:
        return None, {'reason': '本次任务的节点都不是 etcd/control-plane，无法查询 etcd 仲裁'}
    try:
        return probe_etcd_from_sessions(candidates)
    finally:
        for session in sessions.values():
            session.close()


def _supported_system_tokens(kubeasz_version):
    """向制品模块询问某个 kubeasz 版本支持的系统 token 集合。

    参数：`kubeasz_version` 表示 kubeasz 版本号。
    返回：返回 ``(token 集合, 错误说明)``；制品模块不可用时集合为 None，错误说明可直接展示。
    副作用：只导入并调用制品模块，不建立网络连接、不写文件。
    """
    try:
        from k8s_deployment.services.artifacts import supported_system_tokens
    except ImportError as exc:
        # 制品模块可能尚未发布或依赖缺失，此时降级为 skipped，不能让预检整体崩掉。
        return None, f'制品模块暂不可用（{exc}）'
    try:
        return {str(item).strip().lower() for item in supported_system_tokens(kubeasz_version)}, ''
    except Exception as exc:
        return None, f'制品模块调用失败（{exc}）'


def _missing_addon_artifacts(deployment):
    """向制品模块询问本次要装的附加组件是否都有制品。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回 ``(缺失项列表, 无法判定原因)``，含义与 ``artifacts.missing_addon_artifacts`` 一致。
    副作用：发起一次网络读取；制品模块不可用时降级为「无法判定」，不抛异常。
    """
    try:
        from k8s_deployment.services.artifacts import missing_addon_artifacts
    except ImportError as exc:
        return [], f'制品模块暂不可用（{exc}）'
    try:
        return missing_addon_artifacts(
            str(deployment.kubeasz_version or '').strip(),
            cni_plugin=deployment.cni_plugin,
            metrics_server=bool(deployment.metrics_server),
            nginx_ingress=bool(deployment.nginx_ingress),
            install_helm_cli=bool(deployment.install_helm_cli),
        )
    except Exception as exc:
        return [], f'制品能力查询失败（{exc}）'


def _check_artifact(deployment):
    """检查项 11：制品能力。

    两件事，判定强度刻意不同：

    - **所选操作系统在该 kubeasz 版本的离线包清单里**：只报警告。两重原因。其一，这个清单
      取自上游 ezdown 的声明文本，会过时——kubeasz 3.6.7 的 ezdown 仍写着 ``centos_7``，
      但对应的 ``easzlab/kubeasz-sys-pkg:1.0.3_centos_7`` 上游根本没发布过，按它判失败等于
      拿一句过期的帮助文本挡住一次合规部署。其二，平台编译集群时 ``INSTALL_SOURCE`` 恒为
      ``online``（``compiler.compile_cluster`` 的默认值，调用方从不覆盖），系统包走目标机
      自己的 yum/apt 源，离线包根本用不到。系统与版本的配对关系由配置中心的
      ``K8sVersionMapping.supported_systems`` 在创建任务时就卡住（``serializers.py:352``），
      那道闸才是权威的，这里再硬拦一次只会与它冲突。
    - **本次要装的 Ingress/CNI/Metrics/Helm 在制品里有没有镜像**：缺了判硬失败。这一条拦的
      是真实后果——镜像确实要由制品提供，缺了会跑到 ``07.addons`` 才炸（规划 4.3、9）。

    本项不依赖 SSH，纯本地判断。清单读不到（OSS 抖动等）时判 skipped 而不是 failed：
    判不了不等于没有，不能因为一次网络抖动就挡住一次合规的部署。

    参数：`deployment` 为 K8sDeployment 实例。
    返回：返回检查项字典。
    副作用：尝试导入制品模块并读取其支持的系统清单与镜像清单。
    """
    version = str(deployment.kubeasz_version or '').strip()
    detail = {
        'kubeasz_version': version,
        'os_type': deployment.os_type,
        'os_version': deployment.os_version,
    }
    failures = []
    warnings = []

    tokens, error = _supported_system_tokens(version)
    if tokens is None:
        detail['reason'] = error
        return _make_check(
            'artifact', CHECK_TITLES['artifact'], 'skipped',
            f'未能确认制品支持情况，已跳过本项：{error}', detail,
        )

    token = os_token_for(deployment.os_type, deployment.os_version)
    detail['token'] = token
    detail['supported_tokens'] = sorted(tokens)
    if not token:
        warnings.append(
            f'无法把 {deployment.os_type} {deployment.os_version} 归一化成制品标识，'
            f'该 kubeasz 版本声明的系统为：{"、".join(sorted(tokens)) or "无记录"}'
        )
    elif token not in tokens:
        warnings.append(
            f'kubeasz {version} 未声明 {deployment.os_type} {deployment.os_version}（{token}）的离线安装包，'
            f'该版本声明的系统为：{"、".join(sorted(tokens))}'
        )

    missing, addon_error = _missing_addon_artifacts(deployment)
    detail['missing_addons'] = [{'field': field, 'message': message} for field, message in missing]
    if addon_error:
        detail['addon_reason'] = addon_error
    else:
        failures.extend(message for _field, message in missing)

    if failures:
        return _make_check(
            'artifact', CHECK_TITLES['artifact'], 'failed', '；'.join(failures), detail,
        )
    if warnings or addon_error:
        reasons = list(warnings)
        if addon_error:
            reasons.append(f'附加组件制品未能确认：{addon_error}')
        return _make_check(
            'artifact', CHECK_TITLES['artifact'], 'warned',
            '本次所选附加组件制品齐全，但存在需要确认的项：' + '；'.join(reasons), detail,
        )
    return _make_check(
        'artifact', CHECK_TITLES['artifact'], 'passed',
        f'kubeasz {version} 声明了 {token} 的离线安装包，本次所选附加组件制品齐全', detail,
    )


def _guard_check(name, factory, *args):
    """执行单个检查项，把意料之外的异常收敛成本项的警告结果。

    参数：`name` 为检查项标识；`factory` 为检查函数；`args` 为传给该函数的参数。
    返回：返回检查函数的结果；抛出异常时返回一条 warned 记录。
    副作用：执行该检查函数，可能建立 SSH 连接或执行远程命令。
    """
    try:
        return factory(*args)
    except PreflightError:
        raise
    except Exception as exc:
        # 预检自身的缺陷不应该把部署挡死，记成警告并在 message 里说明，人工可判断。
        return _make_check(name, CHECK_TITLES[name], 'warned', f'预检项执行异常，结果不可用：{exc}')


def run_preflight(deployment, nodes, ssh_credentials):
    """对目标节点执行环境预检。

    参数：`deployment` 是 K8sDeployment 实例；`nodes` 是 K8sDeploymentNode 列表；
        `ssh_credentials` 是 ``{ip: {'user': str, 'port': int, 'password': str}}`` 的映射。
    返回：返回形如 {
        'ok': bool,                 # 全部硬性检查是否通过
        'checks': [                 # 每一项检查
            {'name': 'ssh', 'title': 'SSH 连通性', 'status': 'passed'|'failed'|'warned'|'skipped',
             'message': '中文说明', 'detail': {...}, 'nodes': ['192.168.1.94']}, ...
        ],
        'hard_failures': ['中文失败原因', ...],   # 硬性失败的可读原因列表
        'summary': '通过 8 项，失败 1 项',        # 一句话汇总
    }；`nodes` 为空或节点缺少 IP 时抛出 PreflightError。
    副作用：建立到目标节点的 SSH 连接并执行只读命令；不修改目标机器的任何状态。
    """
    node_list = [node for node in (nodes or []) if node is not None]
    if not node_list:
        raise PreflightError('没有可用于预检的目标节点')

    credentials = ssh_credentials or {}
    sessions = {}
    for node in node_list:
        ip = str(getattr(node, 'ip_address', '') or '').strip()
        if not ip:
            raise PreflightError('目标节点缺少 IP 地址，无法执行预检')
        sessions[ip] = _NodeSession(node, credentials.get(ip) or {})

    checks = []
    try:
        # 先跑 SSH 连通性：只有连上的节点才参与后面的节点级检查。
        try:
            ssh_check, reachable = _check_ssh(sessions)
        except Exception as exc:
            # SSH 层自身出错（例如运行环境缺 paramiko）等价于连不上，按硬失败处理。
            ssh_check = _make_check(
                'ssh', CHECK_TITLES['ssh'], 'failed', f'SSH 连通性检查未能完成：{exc}',
            )
            reachable = []
        checks.append(ssh_check)
        # 其余检查项严格按 CHECKS_ORDER 的顺序产出。
        checks.append(_guard_check('privilege', _check_privilege, sessions, reachable))
        checks.append(_guard_check('os_release', _check_os_release, deployment, sessions, reachable))
        checks.append(_guard_check('disk', _check_disk, deployment, sessions, reachable))
        checks.append(_guard_check('kernel', _check_kernel, deployment, sessions, reachable))
        checks.append(_guard_check('stale_install', _check_stale_install, deployment, sessions, reachable))
        checks.append(_guard_check('ports', _check_ports, deployment, sessions, reachable))
        checks.append(_guard_check('clock', _check_clock, deployment, sessions, reachable))
        checks.append(_guard_check('network', _check_network, deployment, sessions, reachable))
        checks.append(_guard_check('etcd_network', _check_etcd_network, deployment, sessions, reachable))
        checks.append(_guard_check('cidr', _check_cidr, deployment, sessions, reachable))
        checks.append(_guard_check('external_lb', _check_external_lb, deployment, sessions, reachable))
        checks.append(_guard_check('etcd_quorum', _check_etcd_quorum, deployment, sessions, reachable))
        checks.append(_guard_check('artifact', _check_artifact, deployment))
    finally:
        # 无论中途发生什么都要断开连接，避免预检结束后残留 SSH 会话。
        for session in sessions.values():
            session.close()

    hard_failures = [
        f'{check["title"]}：{check["message"]}' for check in checks if check['status'] == 'failed'
    ]
    return {
        'ok': not hard_failures,
        'checks': checks,
        'hard_failures': hard_failures,
        'summary': _build_summary(checks),
    }


def _build_summary(checks):
    """按各检查项的状态统计出一句话汇总。

    参数：`checks` 表示检查项列表。
    返回：返回形如「通过 8 项，失败 1 项」的中文汇总。
    副作用：纯计算，不修改持久化数据。
    """
    counts = {}
    for check in checks:
        counts[check['status']] = counts.get(check['status'], 0) + 1
    parts = [f'通过 {counts.get("passed", 0)} 项', f'失败 {counts.get("failed", 0)} 项']
    if counts.get('warned'):
        parts.append(f'警告 {counts["warned"]} 项')
    if counts.get('skipped'):
        parts.append(f'跳过 {counts["skipped"]} 项')
    return '，'.join(parts)
