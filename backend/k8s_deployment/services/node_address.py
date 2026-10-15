"""节点地址的两重身份：平台怎么连上去，集群内部怎么互相找到。

操作员填在机器上的那个 IP 只回答一个问题 —— **平台 SSH 得到它吗**。公网地址、内网地址
都行，只要能连上。但 kubeasz 的 inventory 主机名是另一回事：etcd 与 apiserver 的全部
``--listen-*`` / ``--advertise-*`` / ``--initial-cluster`` 都直接取 ``{{ inventory_hostname }}``
（``roles/etcd/templates/etcd.service.j2``），所以它必须是**节点网卡上真实存在的地址**。

节点在 NAT 后面时这两个地址不是同一个：填的是 ``116.198.251.203``，网卡上只有
``172.30.0.7``。inventory 里写公网地址，etcd 起不来，报的是

    listen tcp 116.198.251.203:2380: bind: cannot assign requested address

**分工因此是固定的**：inventory 主机名 = 网卡地址（集群内部通信），``ansible_host`` =
操作员填的地址（平台怎么连上去）。见 ``compiler._host_line``。

这里负责探测网卡地址：连上去只读一条命令，不改目标机上的任何东西。探测不出结果时一律
退回操作员填的地址 —— 那正好是「没有 NAT」这种最常见情况的行为，探测失败不该让部署停下。
"""

import ipaddress
import socket

from . import credentials

# 一次探测的输出约定：两行，前缀固定，便于解析。
#   ROUTE —— 内核往外发包时会用哪个源地址，也就是默认路由所在的网卡地址；
#   ADDRS —— 本机所有全局 IPv4（scope global 排除回环与 link-local）。
#
# 用 ``ip route get 1.1.1.1`` 而不是 ``hostname -I`` 打头：多网卡时前者给出的是**实际承担
# 出站流量的那一张**网卡，后者只是接口顺序。节点没有默认路由时前者会失败，于是回退到地址表。
PROBE_COMMAND = (
    "echo \"ROUTE $(ip -4 route get 1.1.1.1 2>/dev/null"
    " | sed -n 's/.* src \\([0-9.]*\\).*/\\1/p' | head -1)\"; "
    "echo \"ADDRS $(ip -4 -o addr show scope global 2>/dev/null"
    " | sed -n 's/.* inet \\([0-9.]*\\)\\/.*/\\1/p' | tr '\\n' ' ')\""
)

# 探测连接的超时秒数。节点通常就在内网，给得比常规值紧一点也无妨。
PROBE_TIMEOUT = 10

# 判断「平台机与节点内网地址是否互通」时的连接超时秒数。这只是给操作员的提示，
# 连不上不代表集群有问题，所以宁可快速失败也不拖慢部署前的准备。
REACH_TIMEOUT = 3

# 探测结果里地址的来源，用于给操作员一句准确的说明。
SOURCE_ENTERED = 'entered'
SOURCE_PROBE = 'probe'
SOURCE_FALLBACK = 'fallback'


def _is_ipv4(value):
    """判断文本是否是合法 IPv4 地址。

    参数：`value` 表示待判断的文本。
    返回：返回 True 或 False。
    副作用：纯计算。

    探测输出来自目标机的命令行，不能直接当成地址使用；``ip`` 的输出里混进别的东西时
    宁可当作没探测到，也不要把它写进 inventory。
    """
    try:
        ipaddress.IPv4Address(str(value or '').strip())
    except ValueError:
        return False
    return True


def parse_probe(text):
    """解析探测命令的输出。

    参数：`text` 表示命令输出。
    返回：返回 ``(出口地址, 本机地址列表)``，缺失的部分为空串与空列表。
    副作用：纯计算。
    """
    route_source = ''
    addresses = []
    for line in str(text or '').splitlines():
        keyword, _, rest = line.strip().partition(' ')
        if keyword == 'ROUTE':
            route_source = rest.strip()
        elif keyword == 'ADDRS':
            addresses = [item for item in rest.split() if item]
    if not _is_ipv4(route_source):
        route_source = ''
    return route_source, [item for item in addresses if _is_ipv4(item)]


def choose_cluster_address(entered, route_source='', addresses=()):
    """在「操作员填的地址」与「节点网卡地址」之间做出选择。

    参数：`entered` 表示操作员填写的机器 IP；`route_source` 表示探测到的出口地址；
        `addresses` 表示探测到的本机地址列表。
    返回：返回集群内部应使用的地址；无从判断时返回空串。
    副作用：纯计算。

    填写的地址本身就在网卡上（内网直连、或者填的就是内网地址）时**按填写值来**：那是
    操作员明确的选择，探测不该反过来覆盖它。只有填写值不在网卡上（NAT 之后的公网地址）
    才换成探测到的地址。
    """
    entered = str(entered or '').strip()
    if entered and entered in set(addresses):
        return entered
    if route_source:
        return route_source
    for item in addresses:
        return item
    return ''


def probe_over_ssh(host, port, username, key_path=None):
    """用平台部署密钥连上节点，读一次网卡地址。

    参数：`host` 表示平台连接节点的地址；`port` 表示 SSH 端口；`username` 表示登录用户名；
        `key_path` 表示部署私钥路径，为空时取平台默认路径。
    返回：返回探测命令的输出文本。
    副作用：建立一条 SSH 连接并在目标机上执行一条只读命令。
    """
    client = credentials.ssh_client(
        host, port, username,
        key_path=key_path or credentials.private_key_path(),
        timeout=PROBE_TIMEOUT,
    )
    try:
        _code, out, _err = credentials.run_command(client, PROBE_COMMAND, timeout=PROBE_TIMEOUT)
        return out
    finally:
        client.close()


def resolve(node, credential=None, key_path=None, probe=None):
    """确定一台节点在集群内部使用的地址。

    参数：`node` 表示节点快照；`credential` 表示该节点的登录凭据；`key_path` 表示部署私钥路径；
        `probe` 表示探测函数 ``f(host, port, username) -> 输出文本``，便于测试注入。
    返回：返回 ``{'address': 集群内部地址, 'source': SOURCE_*, 'route_source':.., 'addresses':[..]}``；
        ``address`` 为空串表示连操作员填的地址都取不到。
    副作用：建立一条到目标机的 SSH 连接；不修改任何持久化数据。

    探测过程中的任何异常都翻译成「退回填写的地址」：免密登录在这一步之前刚刚验过，连不上
    多半是 ``ip`` 命令缺失或输出不认识，这不该拦住整个部署。
    """
    entered = str(getattr(node, 'ip_address', '') or '').strip()
    credential = credential or {}
    port = int(credential.get('port') or getattr(node, 'ssh_port', 22) or 22)
    username = str(credential.get('user') or getattr(node, 'ssh_user', '') or 'root')

    runner = probe or (lambda host, port, user: probe_over_ssh(host, port, user, key_path=key_path))
    try:
        text = runner(entered, port, username)
    except Exception:  # noqa: BLE001 - 探测失败必须降级，不能让它变成部署失败
        text = ''

    route_source, addresses = parse_probe(text)
    chosen = choose_cluster_address(entered, route_source, addresses)
    if not chosen:
        return {
            'address': entered,
            'source': SOURCE_FALLBACK,
            'route_source': route_source,
            'addresses': addresses,
        }
    return {
        'address': chosen,
        'source': SOURCE_ENTERED if chosen == entered else SOURCE_PROBE,
        'route_source': route_source,
        'addresses': addresses,
    }


def platform_reaches(address, port, timeout=REACH_TIMEOUT):
    """判断平台机能否连到某个地址的端口。

    参数：`address` 表示目标地址；`port` 表示目标端口；`timeout` 表示连接超时秒数。
    返回：返回 True 或 False。
    副作用：发起一次 TCP 连接，随即关闭。

    用来回答「部署完平台还管得到这个集群吗」。kubeasz 的 apiserver 只监听 ``inventory_hostname``
    （``--bind-address={{ inventory_hostname }}``），所以节点在 NAT 后面时，平台即使 SSH 通，
    API 也可能连不上：健康检查、资源查看都会失败。这里探的是节点的 SSH 端口 —— sshd 监听在
    所有网卡地址上，连得上就说明平台与那张网卡所在的网络是通的。
    """
    try:
        with socket.create_connection((str(address), int(port)), timeout=timeout):
            return True
    except OSError:
        return False
