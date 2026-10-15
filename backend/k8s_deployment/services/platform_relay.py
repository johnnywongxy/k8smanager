"""平台机到集群内网地址的转发通道。

kubeasz 的角色里有十几处 ``connection: local``（``roles/kube-master/tasks/main.yml`` 5 处、
``roles/flannel`` 4 处、``roles/calico`` 2 处）：那些任务**在平台机上执行**，用的却是节点
变量表里的 ``inventory_hostname`` —— 也就是集群内网地址。它们的 kubeconfig 指向的同样是
这个地址（``roles/deploy/vars/main.yml`` 的 ``KUBE_APISERVER`` 取
``groups['kube_master'][0]``，04 阶段每台 master 还用自己那份 ``<nodename>-kubectl.kubeconfig``）。

于是出现一条不对等的链路：平台 SSH 得到节点（操作员填的公网地址），却拨不到集群 API
（节点网卡上的内网地址）。实测 192.168.1.94 到 172.30.0.7:6443 发出的 SYN 无人应答，
kubeasz 的 ``轮询等待master服务启动完成``（``retries: 5, delay: 6``，每次连接要等约 127 秒
才超时）会一路重试到失败，日志里只有 ansible 的重试行，跟「地址怎么填的」看不出关系。

本模块补上这一段：部署与运维期间，把集群内网地址临时挂到平台机的回环接口上，并在该地址
的 6443 端口监听，把连接经**已有的部署密钥通道**（与 ansible 用同一把私钥）转到节点自己的
``127.0.0.1:6443`` —— kube-lb 就监听在那里。

**为什么是地址接管而不是改 kubeconfig。** apiserver 证书的 SAN 就是集群内网地址，kubectl
按地址校验 TLS，把 server 改成 127.0.0.1 之类会直接撞上证书校验，除非把校验关掉（不可接受）。
地址接管让证书、kubeconfig、inventory 三处一个字都不用动，而且平台自己的健康检查、资源查看
走的也是同一份 kubeconfig，一并被救活。

**两条链路：控制面 API（6443）与 etcd（2379）。** 前者是 kubeasz 那批 ``connection: local``
的 kubectl 要用的，随任务起落；后者是 ``ezctl backup`` 要用的 —— 备份剧本（``94.backup.yml``）
整本跑在 ``hosts: localhost`` 上，用 etcdctl 逐个成员探活并取快照，打的是 ``[etcd]`` 段里的
内网地址。少了它，备份不会报错，只会永远等下去（现场：192.168.1.94 上的备份任务卡了三十多
分钟，集群备份目录里只落下一个 0 字节的 ``.part``）。ansible 对节点的其余访问全部走 SSH
（填写的地址），不需要隧道。

**生命周期。** 通道随任务起落：``execute_task`` 一开始就按 inventory 建好，任务结束（成功、
失败、取消都算）在 ``finally`` 里全部拆掉，并把临时地址从本机摘掉。地址清单另有一份落盘记录
（``<状态根>/relay/addresses.json``），Worker 每次启动先按它清理一遍：进程被强杀时留下的地址
不会一直挂在平台机上。
"""

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

from . import credentials, node_address, paths

# 平台机唯一会直接拨的集群端口：API Server。别的都走 SSH。
API_PORT = 6443
# etcd 客户端口。备份剧本（``ezctl backup`` → ``playbooks/94.backup.yml``）整本跑在
# ``hosts: localhost`` 上，用 ``etcdctl --endpoints=https://<集群内网地址>:2379`` 取快照，
# 于是平台机也得拨得到这个端口 —— 和 API 是完全一样的处境（见模块说明）。
ETCD_CLIENT_PORT = 2379
# 节点上接受这条连接的地址：kube-lb 监听在节点本机回环上，由它转到 apiserver。
FORWARD_HOST = '127.0.0.1'
# 转发目标写成这个值时，表示「转到被接管地址它自己」。
#
# etcd 必须这么转：kubeasz 的 ``roles/etcd/templates/etcd.service.j2`` 让它只监听
# ``https://{{ inventory_hostname }}:2379``（TLS）与 ``http://127.0.0.1:2379``（**明文**）。
# 剧本拨的是前者，所以隧道要落到节点自己的内网地址上；落到回环会因为对端讲明文 http 而
# 握手失败。
ROUTE_SELF = 'self'
# 临时挂地址用的本机接口。挂 /32 到 lo 只让内核把它认作本机地址，不动任何路由表。
ALIAS_DEVICE = 'lo'
# inventory 里承载集群 API 的段。只有这些段里的地址会被 ansible 在平台机上直连。
API_SECTIONS = ('kube_master', 'kube_control_plane')
# inventory 里承载 etcd 的段：备份剧本在平台机上直接拨这里的地址。
ETCD_SECTIONS = ('etcd',)
# 一条要接管的「集群内网地址:端口」。
#
# ``key`` 用于日志与注册表的去重（同一个地址可能有两条通道：6443 与 2379）；
# ``probe_port`` 为空表示拿目标的 SSH 端口探「网络通不通」，否则直接探业务端口
# —— 后者对 etcd 更准：平台机本来就能拨到 2379 时（平台机就在集群网段里）根本不用接管。
API_ROUTE = {
    'key': 'api',
    'label': 'API Server',
    'sections': API_SECTIONS,
    'port': API_PORT,
    'destination_host': FORWARD_HOST,
    'probe_port': None,
}
ETCD_ROUTE = {
    'key': 'etcd',
    'label': 'etcd',
    'sections': ETCD_SECTIONS,
    'port': ETCD_CLIENT_PORT,
    'destination_host': ROUTE_SELF,
    'probe_port': ETCD_CLIENT_PORT,
}
# inventory 里承载外部负载均衡的段，以及它的三个行内变量。
#
# VIP **不是这个段里任何一台机器的地址**：它是 keepalived 在段内两台 LB 机器之间漂移的虚拟
# 地址，写在每台机器那一行的 ``EX_APISERVER_VIP`` 上。所以它没法像上面两条链路那样按「段里
# 的主机行地址」挑出来（见 ``lb_vip_targets``）。
LB_SECTION = 'ex_lb'
LB_ROLE_KEY = 'LB_ROLE'
LB_VIP_KEY = 'EX_APISERVER_VIP'
LB_PORT_KEY = 'EX_APISERVER_PORT'
# keepalived 配置里主节点那一台的角色值。VIP 正常漂在它身上，隧道优先落在它这里。
LB_ROLE_MASTER = 'master'
# 第三条链路：外部负载均衡的 VIP。
#
# kubeasz 的 ex-lb 剧本末尾要在**平台机**上拨 VIP 验活（``roles/ex-lb`` 最后那个 ``uri``
# 任务带 ``run_once: true`` + ``connection: local``，打的是 ``https://<VIP>:6443``）。它是
# 全套剧本里唯一一个「平台机会直连、却不在 inventory 任何节点段里」的地址，于是前面两条链路
# 都覆盖不到它 —— 现场表现是 01-07 阶段全绿、第 8 阶段收尾以
# ``Status code was -1 and not [200]: Request failed: <urlopen error timed out>`` 失败
# （每次 30 秒、``retries: 2``，约 65 秒），而集群本身完全正常。
#
# 隧道落在**一台真的 LB 机器**上而不是某台 master 上：VIP 后面站的就是 LB 机器上的 l4lb
# （它监听 ``0.0.0.0:6443``，回环自然也通），于是「VIP → l4lb → apiserver」这条真实链路被
# 完整保留，只有「平台机 → VIP」这一跳换成了隧道。落到 master 也能让这句检查返回 200，
# 但那样 LB 全挂也会「验收通过」，等于把这一步的意义抹掉了。
VIP_ROUTE = {
    'key': 'lb_vip',
    'label': '外部负载均衡 VIP',
    'sections': (LB_SECTION,),
    'port': API_PORT,
    'destination_host': FORWARD_HOST,
    'probe_port': API_PORT,
}
# SSH 连接与握手超时秒数。
CONNECT_TIMEOUT = 15
# SSH 传输层的 keepalive 间隔。部署期一条命令可能跑十几分钟，心跳太稀会被中间的 NAT 静默丢弃。
KEEPALIVE_SECONDS = 30
# 本地监听的 accept 超时：只为让线程有机会看到停止标志，不是业务超时。
ACCEPT_TIMEOUT = 1.0
LISTEN_BACKLOG = 8
# 一次搬运的字节数。
BUFFER_SIZE = 65536
# 调用 ``ip`` 的超时秒数。这条命令只读或只改本机接口，不该慢。
IP_TIMEOUT = 10
# ``ip`` 的位置：平台机是 CentOS 7，它只在 /sbin 下；PATH 里有没有 /sbin 由 systemd 决定。
IP_CANDIDATES = ('/sbin/ip', '/usr/sbin/ip', '/bin/ip', '/usr/bin/ip')
# 记录「平台自己挂上去的地址」的目录与文件名。
RELAY_DIR_NAME = 'relay'
STATE_FILE_NAME = 'addresses.json'


class RelayError(Exception):
    """建不起转发通道，携带可直接展示给用户的中文原因。"""


def _ip_tool():
    """返回本机 ``ip`` 命令的位置。

    参数：无。
    返回：返回绝对路径，都不存在时返回裸命令名交给 PATH。
    副作用：只读文件系统。
    """
    for candidate in IP_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return 'ip'


def _run(argv):
    """执行一条本机命令，把异常收敛成退出码。

    参数：`argv` 表示命令行列表。
    返回：返回 ``(退出码, 标准输出, 标准错误)``。
    副作用：起一个子进程；不通过 shell，参数不会被二次解释。

    这里刻意不抛异常：调用方需要区分「命令失败」和「命令不存在」两类结果，
    而两者都只是「这次转发没建成」，不该让任务崩在别处。
    """
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=IP_TIMEOUT, check=False,
        )
    except FileNotFoundError:
        return 127, '', f'本机没有 {argv[0]} 命令'
    except subprocess.SubprocessError as exc:
        return 1, '', str(exc)
    return completed.returncode, completed.stdout or '', completed.stderr or ''


def is_ipv4(value):
    """判断文本是否是合法 IPv4 地址。

    参数：`value` 表示待判断的文本。
    返回：返回 True 或 False。
    副作用：纯计算。

    转发通道只对 IP 地址有意义：inventory 里也允许写域名，那种情况平台本来就靠 DNS 解析，
    不需要接管地址。
    """
    return node_address._is_ipv4(value)


def route_key(address, port):
    """拼出「一条通道」的标识。

    参数：`address` 表示被接管的集群内网地址；`port` 表示平台机上监听的端口。
    返回：返回 ``<地址>:<端口>`` 文本。
    副作用：纯计算。
    """
    return f'{address}:{int(port)}'


def _section_lines(hosts_text, sections):
    """把 inventory 指定段里的主机行拆成「行首地址 + 行内变量」。

    参数：`hosts_text` 表示 inventory 文本；`sections` 表示要读的段名。
    返回：返回 ``[(地址, {变量名: 值})]``，顺序与文件一致，重复地址只留第一次出现的那行。
    副作用：纯计算。

    两条链路要的东西不一样：``section_targets`` 拿行首地址当集群内网地址，``lb_vip_targets``
    行首地址是 LB 机器、真正要接管的却是同一行上的 ``EX_APISERVER_VIP``。解析本身（分段、
    跳注释、拆 ``key=value``、去重）两边完全一样，放在这里各取所需。
    """
    current = ''
    entries = []
    seen = set()
    for line in str(hosts_text or '').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            current = stripped[1:-1]
            continue
        if current not in sections:
            continue
        head, _, rest = stripped.partition(' ')
        if not is_ipv4(head) or head in seen:
            continue
        seen.add(head)
        fields = {}
        for token in rest.split():
            key, _, value = token.partition('=')
            if key and value:
                fields[key] = value.strip('\'"')
        entries.append((head, fields))
    return entries


def section_targets(hosts_text, sections, default_user='root', default_port=22):
    """从 inventory 的指定段里挑出平台机要直接访问的地址。

    参数：`hosts_text` 表示 inventory 文本；`sections` 表示要看的段名；`default_user` 表示
        没写 ``ansible_user`` 时的用户名；`default_port` 表示没写 ``ansible_port`` 时的 SSH 端口。
    返回：返回 ``[{'address','host','user','port'}]``，``address`` 是集群内网地址，
        ``host`` 是平台连得上的地址（写在同一行的 ``ansible_host``，没写就是它自己）。
    副作用：纯计算。

    节点段的地址平台机从不去拨（连节点一律走 SSH），因此只认承载「平台机会直连的服务」的段：
    控制面（ansible 在平台机上执行的 kubectl）与 etcd（备份剧本在平台机上执行的 etcdctl）。
    """
    targets = []
    for head, fields in _section_lines(hosts_text, sections):
        # ansible_host 写成域名时隧道也连得上（ssh 自己解析），照收。
        host = fields.get('ansible_host') or head
        try:
            port = int(fields.get('ansible_port') or default_port)
        except (TypeError, ValueError):
            port = int(default_port)
        targets.append({
            'address': head,
            'host': host,
            'user': fields.get('ansible_user') or default_user,
            'port': port,
        })
    return targets


def lb_vip_targets(hosts_text, default_user='root', default_port=22):
    """从 inventory 的 ``[ex_lb]`` 段里挑出平台机要直接访问的外部负载均衡 VIP。

    参数：`hosts_text` 表示 inventory 文本；`default_user` 表示没写 ``ansible_user`` 时的用户名；
        `default_port` 表示没写 ``ansible_port`` 时的 SSH 端口。
    返回：返回 ``section_targets`` 形式的目标列表（至多一项），``address`` 是 VIP，``host`` 是
        承接隧道的 LB 机器，``listen_port`` 是平台机上要监听的端口；段里没有合法 VIP 时返回空列表。
    副作用：纯计算。

    VIP 和另外两条链路都不一样：它不是任何一台机器的地址，而是 keepalived 在段内两台 LB 机器
    之间漂移的虚拟地址，写在每台机器那一行的 ``EX_APISERVER_VIP`` 上。隧道没法「连到 VIP
    本身」，只能落到一台真的 LB 机器上 —— 选 ``LB_ROLE=master`` 那台（正常持有 VIP 的就是它），
    角色都没写时用第一台。落点机器上的转发目标是它自己的回环 6443，l4lb 就监听在那里，于是
    「VIP → l4lb → apiserver」这条真实链路完整保留，只有「平台机 → VIP」这一跳换成隧道。
    """
    entries = _section_lines(hosts_text, (LB_SECTION,))
    if not entries:
        # 云内网负载均衡与「不使用」两种模式的 ``[ex_lb]`` 段是空的：没有 VIP 要接管。
        return []
    chosen = entries[0]
    for entry in entries:
        if str(entry[1].get(LB_ROLE_KEY) or '').strip().lower() == LB_ROLE_MASTER:
            chosen = entry
            break
    head, fields = chosen
    vip = str(fields.get(LB_VIP_KEY) or '').strip()
    if not is_ipv4(vip):
        # 段里没写 VIP 时 kubeasz 的 ex-lb 剧本自己也跑不起来（keepalived 模板缺变量），
        # 那是编译期该挡住的事，这里只当作「没有需要接管的地址」。
        return []
    try:
        listen_port = int(str(fields.get(LB_PORT_KEY) or '').strip() or API_PORT)
    except (TypeError, ValueError):
        listen_port = API_PORT
    # 两种写法都认：``compiler.host_fields`` 生成的是 ``ansible_ssh_user`` / ``ansible_ssh_port``
    # （只在不是 root/22 时才写），而 ansible 两种都接受，手写的 inventory 里也常见不带 ``ssh_``
    # 的那一种。只认其中一种时，非默认端口或非 root 用户的集群会连错地方。
    user = fields.get('ansible_user') or fields.get('ansible_ssh_user') or default_user
    raw_port = fields.get('ansible_port') or fields.get('ansible_ssh_port')
    try:
        ssh_port = int(raw_port or default_port)
    except (TypeError, ValueError):
        ssh_port = int(default_port)
    return [{
        'address': vip,
        'host': fields.get('ansible_host') or head,
        'user': user,
        'port': ssh_port,
        'listen_port': listen_port,
    }]


def api_targets(hosts_text, default_user='root', default_port=22):
    """从 inventory 里挑出平台机需要直接访问的控制面地址。

    参数：`hosts_text` 表示 inventory 文本；`default_user` 表示没写 ``ansible_user`` 时的用户名；
        `default_port` 表示没写 ``ansible_port`` 时的 SSH 端口。
    返回：返回 ``section_targets`` 形式的目标列表。
    副作用：纯计算。

    ansible 在平台机上执行的 kubectl 打的是这些地址（04.kube-master 与网络插件角色里那十几处
    ``connection: local``），没有通道它们会一路重试到超时。
    """
    return section_targets(hosts_text, API_SECTIONS, default_user=default_user, default_port=default_port)


def etcd_targets(hosts_text, default_user='root', default_port=22):
    """从 inventory 里挑出平台机需要直接访问的 etcd 地址。

    参数：`hosts_text` 表示 inventory 文本；`default_user` 表示没写 ``ansible_user`` 时的用户名；
        `default_port` 表示没写 ``ansible_port`` 时的 SSH 端口。
    返回：返回 ``section_targets`` 形式的目标列表。
    副作用：纯计算。

    备份剧本整本跑在平台机上：``etcdctl endpoint health`` 逐个成员探活、``snapshot save`` 从
    第一个健康成员取快照，两次都是平台机直拨集群内网地址的 2379。平台机拨不通时，剧本不会
    报错 —— etcdctl 没有默认超时，它会一直等下去，任务就永远停在「集群备份」这一步
    （现场：192.168.1.94 上的备份任务卡了三十多分钟，只落下一个 0 字节的 ``.part`` 文件）。
    """
    return section_targets(hosts_text, ETCD_SECTIONS, default_user=default_user, default_port=default_port)


def local_addresses(device=ALIAS_DEVICE):
    """列出本机某个接口上已有的 IPv4 地址。

    参数：`device` 表示接口名。
    返回：返回地址字符串集合，读不到时返回空集合。
    副作用：执行一条只读命令。
    """
    code, out, _err = _run([_ip_tool(), '-o', '-4', 'addr', 'show', 'dev', device])
    if code != 0:
        return set()
    result = set()
    for line in out.splitlines():
        for token in line.split():
            head, sep, _tail = token.partition('/')
            if sep and is_ipv4(head):
                result.add(head)
    return result


def address_owner(address):
    """判断某个地址是不是已经在本机某个接口上。

    参数：`address` 表示待查询的地址。
    返回：返回接口名，没有时返回空串。
    副作用：执行一条只读命令。

    本机已经有这个地址时绝不能再往 lo 上挂一份：重复地址会让内核选路变得说不清楚，
    而且那种情况下（平台机自己就在集群网段里）本来也不需要隧道。
    """
    code, out, _err = _run([_ip_tool(), '-o', '-4', 'addr', 'show'])
    if code != 0:
        return ''
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[2] != 'inet':
            continue
        head, sep, _tail = fields[3].partition('/')
        if sep and head == str(address):
            return fields[1]
    return ''


def _state_path():
    """返回本机临时地址清单的落盘路径。

    参数：无。
    返回：返回 ``<状态根>/relay/addresses.json`` 的 Path。
    副作用：只做路径计算。
    """
    return paths.state_root() / RELAY_DIR_NAME / STATE_FILE_NAME


def _read_state():
    """读取本机临时地址清单。

    参数：无。
    返回：返回 ``{'device':.., 'addresses':[..]}``，文件缺失或损坏时返回空记录。
    副作用：只读文件系统。
    """
    target = _state_path()
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'device': ALIAS_DEVICE, 'addresses': []}
    if not isinstance(data, dict):
        return {'device': ALIAS_DEVICE, 'addresses': []}
    addresses = [item for item in (data.get('addresses') or []) if is_ipv4(item)]
    return {'device': str(data.get('device') or ALIAS_DEVICE), 'addresses': addresses}


def _write_state(state):
    """写入本机临时地址清单。

    参数：`state` 表示 ``{'device':.., 'addresses':[..]}``。
    返回：无显式返回值。
    副作用：在状态目录下写一个小 JSON 文件，先写临时文件再原子替换。

    这份清单只回答一个问题：**平台自己往本机挂过哪些地址**。它是进程被强杀之后唯一能
    还原现场的东西，所以宁可多写一次也不能省。
    """
    target = _state_path()
    paths.ensure_dir(target.parent)
    payload = json.dumps(
        {'device': state.get('device') or ALIAS_DEVICE,
         'addresses': sorted(set(state.get('addresses') or []))},
        ensure_ascii=False,
    )
    partial = target.with_name(target.name + '.partial')
    partial.write_text(payload, encoding='utf-8')
    os.replace(partial, target)


def _remember_address(address):
    """把地址记进落盘清单。

    参数：`address` 表示刚挂上的地址。
    返回：无显式返回值。
    副作用：写磁盘。
    """
    state = _read_state()
    if address not in state['addresses']:
        state['addresses'].append(address)
        _write_state(state)


def _forget_address(address):
    """把地址从落盘清单里去掉。

    参数：`address` 表示已摘掉的地址。
    返回：无显式返回值。
    副作用：写磁盘。
    """
    state = _read_state()
    remaining = [item for item in state['addresses'] if item != address]
    if remaining != state['addresses']:
        _write_state({'device': state['device'], 'addresses': remaining})


def add_local_address(address, device=ALIAS_DEVICE):
    """把集群内网地址临时挂到本机接口上。

    参数：`address` 表示集群内网地址；`device` 表示接口名。
    返回：返回 True 表示这次真的新建了，False 表示本来就在。
    副作用：修改本机网络配置（需要 root 权限），并把地址记进落盘清单。
    """
    address = str(address or '').strip()
    if not is_ipv4(address):
        raise RelayError(f'不是合法的 IPv4 地址：{address}')
    if address in local_addresses(device):
        _remember_address(address)
        return False
    code, _out, err = _run([_ip_tool(), 'addr', 'add', f'{address}/32', 'dev', device])
    if code != 0:
        raise RelayError(
            f'在本机 {device} 上添加 {address} 失败：{err.strip() or "退出码 " + str(code)}。'
            '部署 Worker 需要以 root 运行才有权限改本机网络配置。'
        )
    _remember_address(address)
    return True


def remove_local_address(address, device=ALIAS_DEVICE):
    """把临时挂上的地址从本机接口上摘掉。

    参数：`address` 表示要摘掉的地址；`device` 表示接口名。
    返回：返回 True 表示命令执行成功。
    副作用：修改本机网络配置，并从落盘清单里移除该地址。
    """
    address = str(address or '').strip()
    if not is_ipv4(address):
        return False
    code, _out, _err = _run([_ip_tool(), 'addr', 'del', f'{address}/32', 'dev', device])
    _forget_address(address)
    return code == 0


def cleanup_stale_addresses():
    """摘掉上一次进程留在本机接口上的地址。

    参数：无。
    返回：返回摘掉的地址数量。
    副作用：按落盘清单修改本机网络配置并重写清单。

    Worker 每次启动跑一遍：进程被 kill -9 时 ``finally`` 不会执行，地址会留在 lo 上。
    它本身无害，但下一次任务若碰上同一个地址就没法判断是自己挂的还是上一次残留的。
    """
    state = _read_state()
    device = state['device']
    removed = 0
    for address in list(state['addresses']):
        code, _out, _err = _run([_ip_tool(), 'addr', 'del', f'{address}/32', 'dev', device])
        if code == 0:
            removed += 1
    if state['addresses']:
        _write_state({'device': device, 'addresses': []})
    return removed


def _close(handle):
    """关闭一个套接字或通道，忽略重复关闭。

    参数：`handle` 表示套接字、通道或 None。
    返回：无显式返回值。
    副作用：关闭句柄。
    """
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # noqa: BLE001 - 关闭失败没有补救手段，也不该影响别的连接
        pass


def _copy(source, sink):
    """把一个方向的数据搬完。

    参数：`source` 表示读取端；`sink` 表示写入端。
    返回：无显式返回值。
    副作用：从 ``source`` 读、向 ``sink`` 写，直到任一端关闭。
    """
    try:
        while True:
            data = source.recv(BUFFER_SIZE)
            if not data:
                break
            sink.sendall(data)
    except Exception:  # noqa: BLE001 - 对端关闭或通道断开都是正常结束方式
        pass


class Relay:
    """一条到节点的 SSH 连接，以及挂在它上面的集群地址监听。"""

    def __init__(self, target, key_path=None, port=API_PORT, destination_port=None,
                 destination_host=None):
        """初始化一条转发通道。

        参数：`target` 表示 ``{'address','host','user','port'}``；`key_path` 表示部署私钥路径；
            `port` 表示在平台机地址上监听的端口；`destination_port` 表示节点上被转发的端口；
            `destination_host` 表示节点上被转发的地址，``ROUTE_SELF`` 表示转给被接管地址本身。
        返回：无显式返回值。
        副作用：只记录参数，不发起连接。
        """
        self.target = dict(target or {})
        self.key_path = key_path
        self.port = int(port)
        self.destination_port = int(destination_port or port)
        host = destination_host or FORWARD_HOST
        # ``ROUTE_SELF`` 在这里落成具体地址：注册表只管「哪一条通道」，隧道自己知道往哪转。
        self.destination_host = self.address if host == ROUTE_SELF else str(host)
        self._client = None
        self._transport = None
        self._listener = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def address(self):
        """返回被接管的集群内网地址。

        参数：无。
        返回：返回地址文本。
        副作用：纯计算。
        """
        return str(self.target.get('address') or '')

    @property
    def route_key(self):
        """返回这条通道在注册表里的键。

        参数：无。
        返回：返回 ``<地址>:<端口>`` 文本。
        副作用：纯计算。

        同一个集群内网地址上可能挂着两条通道（控制面 6443 与 etcd 2379，单节点集群两者同址），
        只用地址做键会让后建的那条把前一条顶掉 —— 被顶掉的是部署期真正在用的 API 通道。
        """
        return route_key(self.address, self.port)

    def start(self):
        """建立 SSH 连接并在集群地址上开始监听。

        参数：无。
        返回：返回自身，便于链式调用。
        副作用：发起一次 SSH 连接，在本机地址上开一个监听端口，并起一个接受连接的线程。
        """
        self._client = credentials.ssh_client(
            self.target.get('host'), int(self.target.get('port') or 22),
            self.target.get('user') or 'root',
            key_path=self.key_path or credentials.private_key_path(),
            timeout=CONNECT_TIMEOUT,
        )
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            self.stop()
            raise RelayError(f'到 {self.target.get("host")} 的 SSH 通道没有建立起来。')
        transport.set_keepalive(KEEPALIVE_SECONDS)
        self._transport = transport

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.address, self.port))
            listener.listen(LISTEN_BACKLOG)
        except OSError as exc:
            _close(listener)
            self.stop()
            raise RelayError(f'在 {self.address}:{self.port} 上监听失败：{exc}。') from exc
        listener.settimeout(ACCEPT_TIMEOUT)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def _accept_loop(self):
        """接受本地连接，为每条连接开一条到节点的 SSH 通道。

        参数：无。
        返回：无显式返回值。
        副作用：起转发线程；随通道停止而退出。
        """
        while not self._stop.is_set():
            try:
                connection, _peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._pump, args=(connection,), daemon=True).start()

    def _pump(self, connection):
        """把一条本地连接与一条到节点的 SSH 通道对接起来。

        参数：`connection` 表示本地已接受的套接字。
        返回：无显式返回值。
        副作用：发起到节点的通道转发；连接结束后关闭两端。
        """
        transport = self._transport
        if transport is None or not transport.is_active():
            _close(connection)
            return
        try:
            channel = transport.open_channel(
                'direct-tcpip',
                (self.destination_host, self.destination_port),
                connection.getpeername(),
            )
        except Exception:  # noqa: BLE001 - 节点拒绝转发时这条连接只能作废
            _close(connection)
            return
        threading.Thread(target=_copy, args=(connection, channel), daemon=True).start()
        _copy(channel, connection)
        _close(connection)
        _close(channel)

    def alive(self):
        """判断这条通道是否还能用。

        参数：无。
        返回：返回 True 或 False。
        副作用：只读本机对象状态。
        """
        transport = self._transport
        return bool(self._listener) and transport is not None and transport.is_active()

    def stop(self):
        """关掉监听并断开 SSH 连接。

        参数：无。
        返回：无显式返回值。
        副作用：关闭监听套接字与 SSH 客户端，唤醒转发线程自行退出。
        """
        self._stop.set()
        _close(self._listener)
        self._listener = None
        _close(self._client)
        self._client = None
        self._transport = None

    def describe(self):
        """返回一句可读的通道说明。

        参数：无。
        返回：返回中文说明文本。
        副作用：纯计算。
        """
        return (
            f'{self.address}:{self.port} → {self.target.get("user")}@{self.target.get("host")}'
            f' 的 {self.destination_host}:{self.destination_port}'
        )


class RelayManager:
    """进程内所有转发通道的注册表。

    平台是「单 Worker、严格串行」，一个进程同时只跑一个任务，所以进程级单例就够用，
    不必把通道挂在任务对象上 —— 那样反而要担心任务提前返回时通道没人拆。
    """

    def __init__(self):
        """初始化注册表。

        参数：无。
        返回：无显式返回值。
        副作用：只创建锁与空字典。
        """
        self._lock = threading.Lock()
        self._relays = {}
        self._targets = {}
        self._logged = {}

    def ensure(self, targets, key_path=None, prober=None, route=None):
        """按需为一批目标建立转发通道。

        参数：`targets` 表示 ``section_targets`` 形式的列表；`key_path` 表示部署私钥路径；
            `prober` 表示可达性判定函数 ``f(address, port) -> bool``，便于测试注入；
            `route` 表示要接管哪条链路（``API_ROUTE``、``ETCD_ROUTE`` 或 ``VIP_ROUTE``）。
        返回：返回 ``{'<地址>:<端口>': 结论}``，结论为 ``active`` / ``reachable`` / ``local`` /
            ``failed:<原因>``。
        副作用：可能修改本机网络配置、建立 SSH 连接、开监听端口。

        已经连上平台机的地址不碰（那种集群不需要隧道）；本机已有的地址也不碰（平台机自己
        就在那个网段里）；已经在用的通道若还活着就继续用，死了就重建。

        注册表按「地址:端口」记，不按地址记：单节点集群的 etcd 与控制面同址，按地址记的话
        后建的 etcd 通道会把正在用的 API 通道顶掉 —— 那种错误只在「先备份、后健康检查」的
        顺序里显形，排查成本极高。

        平台自己压着的地址必须先判出来再决定要不要探：探针问的是「平台机能不能够到这条
        链路」，而地址已经挂在平台机的 ``lo`` 上时，这一问探的是平台机自己 —— 地址是本机的、
        监听 22 的 sshd 也是本机的，答案必然是「通」，于是这条通道被静默跳过（``reachable``
        不写日志）。升级任务正是这样漏掉 6443 的：强制备份先把 172.30.0.x 挂上 ``lo``，
        随后剧本里那些 ``connection: local`` 的 kubectl 打到本机无人监听的 6443 上，
        SYN 直接 ECONNREFUSED，``轮询等待master服务启动完成`` 五次重试后失败。
        """
        spec = route or API_ROUTE
        check = prober or node_address.platform_reaches
        outcomes = {}
        with self._lock:
            for target in targets or []:
                address = str(target.get('address') or '')
                if not address:
                    continue
                # 监听端口默认取链路定义，但目标可以自己带一个：VIP 的端口写在 inventory 的
                # ``EX_APISERVER_PORT`` 上，以那一行为准才能与 kubeasz 实际监听的一致。
                port = int(target.get('listen_port') or spec.get('port') or API_PORT)
                key = route_key(address, port)
                existing = self._relays.get(key)
                if existing is not None:
                    if existing.alive():
                        self._targets[key] = dict(target)
                        outcomes[key] = 'active'
                        continue
                    existing.stop()
                    self._relays.pop(key, None)
                    self._retreat_address(address)
                # 地址是平台自己挂上去的（别的通道正压在上面，或落盘清单里还留着）时，
                # 探针与归属判定都不能信：那一刻问的是平台机自己。
                if not self._holds_address(address):
                    probe_port = spec.get('probe_port')
                    if not probe_port:
                        try:
                            probe_port = int(target.get('port') or 22)
                        except (TypeError, ValueError):
                            probe_port = 22
                    if check(address, probe_port):
                        outcomes[key] = 'reachable'
                        continue
                    # 平台机本来就有这个地址（同一网段）时别接管：那会把真实流量改道进隧道。
                    if address_owner(address):
                        outcomes[key] = 'local'
                        continue
                self._targets[key] = dict(target)
                try:
                    add_local_address(address)
                    relay = Relay(
                        target, key_path=key_path, port=port, destination_port=port,
                        destination_host=spec.get('destination_host'),
                    ).start()
                except Exception as exc:  # noqa: BLE001 - 建不起来只影响这一条通道
                    # 刚挂上的地址要撤掉：这里只看「还有没有别的通道用它」，不看落盘清单
                    # —— 清单里那条正是几行之前自己写进去的。
                    if not self._relays_hold(address):
                        remove_local_address(address)
                    outcomes[key] = f'failed:{exc}'
                    continue
                self._relays[key] = relay
                outcomes[key] = 'active'
        return outcomes

    def _relays_hold(self, address):
        """判断还有没有别的通道压在这个地址上。

        参数：`address` 表示集群内网地址。
        返回：返回 True 或 False。
        副作用：纯计算。
        """
        return any(item.address == address for item in self._relays.values())

    def _holds_address(self, address):
        """判断这个地址是不是平台自己挂上去的。

        参数：`address` 表示集群内网地址。
        返回：返回 True 或 False。
        副作用：可能读一次落盘清单。

        落盘清单也算：进程重启后被 ``cleanup_process_leftovers`` 摘掉之前，那个地址仍在 lo 上，
        这时问 ``address_owner`` 会答「本机有」，据此判成 ``local`` 会把该建的通道漏掉。
        """
        if self._relays_hold(address):
            return True
        return address in _read_state().get('addresses', [])

    def _retreat_address(self, address):
        """在没人再用这个地址时把它从本机摘掉。

        参数：`address` 表示集群内网地址。
        返回：无显式返回值。
        副作用：可能修改本机网络配置。

        同一个地址上可能同时有 API 与 etcd 两条通道，只在最后一条也退出时才摘地址 ——
        提前摘掉会让还活着的那条通道的监听悬在本机已经不认的地址上。
        """
        if self._holds_address(address):
            return
        remove_local_address(address)

    def unlogged(self, outcomes):
        """挑出还没向用户说明过的结论。

        参数：`outcomes` 表示 ``ensure`` 的返回值。
        返回：返回 ``[(address, 结论)]``。
        副作用：更新内部记录。

        ``run_ezctl`` 每次调用前都会 rebuild 一遍通道，同一个结论会反复出现；日志里只该有
        一条。
        """
        fresh = []
        with self._lock:
            for address, outcome in (outcomes or {}).items():
                if self._logged.get(address) == outcome:
                    continue
                self._logged[address] = outcome
                fresh.append((address, outcome))
        return fresh

    def target_of(self, address):
        """返回某个地址对应的目标信息。

        参数：`address` 表示集群内网地址。
        返回：返回目标字典，没有时返回 None。
        副作用：纯计算。
        """
        return self._targets.get(address)

    def active(self):
        """返回当前存活的通道说明列表。

        参数：无。
        返回：返回中文说明文本列表。
        副作用：只读本机对象状态。
        """
        with self._lock:
            return [item.describe() for item in self._relays.values() if item.alive()]

    def teardown(self):
        """停掉全部通道，并把平台机上临时挂的地址摘掉。

        参数：无。
        返回：无显式返回值。
        副作用：断开 SSH 连接、关闭监听端口、修改本机网络配置。
        """
        with self._lock:
            for relay in self._relays.values():
                relay.stop()
            self._relays.clear()
            self._targets.clear()
            self._logged.clear()
        cleanup_stale_addresses()


_MANAGER = RelayManager()


def ensure(context, targets, key_path=None, prober=None, route=None):
    """在任务上下文里按需建立转发通道，并把结论写进日志。

    参数：`context` 表示任务上下文；`targets` 表示 ``section_targets`` 形式的列表；
        `key_path` 表示部署私钥路径；`prober` 表示可达性判定函数；`route` 表示链路定义。
    返回：返回 ``{'<地址>:<端口>': 结论}``。
    副作用：可能修改本机网络配置与建立 SSH 连接，并写日志。
    """
    spec = route or API_ROUTE
    outcomes = _MANAGER.ensure(targets, key_path=key_path, prober=prober, route=spec)
    for key, outcome in _MANAGER.unlogged(outcomes):
        target = _MANAGER.target_of(key) or {'address': key.rpartition(':')[0]}
        address = target.get('address') or key
        if outcome == 'active':
            context.log('system', _active_message(target, spec))
        elif outcome.startswith('failed'):
            context.log('stderr', _failed_message(address, outcome.partition(':')[2] or '原因不明', spec))
        elif outcome == 'local':
            context.log(
                'system',
                f'{address}：平台机自己就有这个地址，不再做转发。'
                '（平台机与集群节点在同一网段时会出现这种情况。）',
            )
    return outcomes


def ensure_from_inventory(context, hosts_file, key_path=None, prober=None, route=None):
    """按 inventory 里的地址建立转发通道。

    参数：`context` 表示任务上下文；`hosts_file` 表示 inventory 路径；`key_path` 表示部署私钥路径；
        `prober` 表示可达性判定函数；`route` 表示要接管哪条链路（默认控制面 API）。
    返回：返回 ``{'<地址>:<端口>': 结论}``，inventory 不存在时返回空字典。
    副作用：可能修改本机网络配置与建立 SSH 连接，并写日志。

    默认那条挂在 ``Pipeline.run_ezctl`` 上：ezctl 一启动就要跑那批 ``connection: local`` 的
    kubectl，通道必须在那之前就位。扩容与升级这类任务走的是「集群现有节点」，它们的地址只
    存在于 inventory 里，所以判断依据取文件而不是本次任务的节点清单。

    ``route=ETCD_ROUTE`` 是备份任务用的：``ezctl backup`` 的剧本整本跑在平台机上，
    拨的是 ``[etcd]`` 段的地址。
    """
    spec = route or API_ROUTE
    target = Path(hosts_file)
    try:
        text = target.read_text(encoding='utf-8')
    except OSError:
        return {}
    return ensure(
        context, section_targets(text, spec.get('sections') or API_SECTIONS),
        key_path=key_path, prober=prober, route=spec,
    )


def ensure_lb_vip(context, hosts_file, key_path=None, prober=None):
    """按 inventory 里 ``[ex_lb]`` 段登记的 VIP 建立转发通道。

    参数：`context` 表示任务上下文；`hosts_file` 表示 inventory 路径；`key_path` 表示部署私钥路径；
        `prober` 表示可达性判定函数。
    返回：返回 ``{'<VIP>:<端口>': 结论}``，inventory 不存在或没有 VIP 时返回空字典。
    副作用：可能修改本机网络配置、建立 SSH 连接并写日志。

    只在第 8 阶段（``10.ex-lb``）之前调用：前面七个阶段用不到 VIP（那两台 LB 机器上还什么都
    没装），而这条通道要连的正是 LB 机器 —— 早了打电话没人接。挂在这里而不是 ``run_ezctl``
    上，也是为了让「每跑一个阶段就探一次 VIP」这笔开销只付一次。
    """
    target = Path(hosts_file)
    try:
        text = target.read_text(encoding='utf-8')
    except OSError:
        return {}
    return ensure(
        context, lb_vip_targets(text), key_path=key_path, prober=prober, route=VIP_ROUTE,
    )


def teardown():
    """停掉本进程的全部通道。

    参数：无。
    返回：无显式返回值。
    副作用：断开 SSH 连接、关闭监听端口、摘掉本机临时地址。

    由 ``execute_task`` 的 ``finally`` 调用：任务成功、失败、取消都要走到这里。留着地址不摘，
    平台机就会一直把集群内网地址当成自己的，后面任何一次真实访问都会被悄悄改道。
    """
    _MANAGER.teardown()


def cleanup_process_leftovers():
    """清理上一次进程留下的痕迹。

    参数：无。
    返回：返回摘掉的地址数量。
    副作用：按落盘清单修改本机网络配置。

    Worker 启动时调用一次。本进程内的通道随进程退出就没了（监听线程与 SSH 连接都在进程里），
    唯一会留下的是挂在 lo 上的地址。
    """
    return cleanup_stale_addresses()


def _active_message(target, route=None):
    """拼出「通道已建立」的日志文本。

    参数：`target` 表示目标信息；`route` 表示链路定义。
    返回：返回中文文本。
    副作用：纯计算。
    """
    spec = route or API_ROUTE
    address = target.get('address')
    host = target.get('host')
    port = target.get('port') or 22
    listen = int(target.get('listen_port') or spec.get('port') or API_PORT)
    destination = spec.get('destination_host')
    destination = address if destination == ROUTE_SELF else (destination or FORWARD_HOST)
    key = spec.get('key')
    if key == VIP_ROUTE['key']:
        why = (
            '它是 keepalived 在 LB 机器之间漂移的虚拟地址，kubeasz 的 ex-lb 剧本收尾要在平台机上'
            '拨这个 VIP 验活（connection: local）；没有这条通道，第 8 阶段会以「urlopen error '
            'timed out」失败 —— 而两台机器上的组件其实都装好了。隧道落在 LB 机器的回环 6443 上，'
            'l4lb 就监听在那里，所以「VIP → l4lb → apiserver」这条链路是完整走通的。'
        )
    elif key == ETCD_ROUTE['key']:
        why = (
            '备份剧本（playbooks/94.backup.yml）整本跑在平台机上，用 etcdctl 探活并取快照，'
            '打的就是这个地址；没有这条通道，etcdctl 会一直等下去，任务看起来就是「卡住不动」。'
        )
    else:
        why = (
            'kubeasz 有十几处 connection: local 的 kubectl 是在平台机上跑的、打的就是这个地址，'
            '没有这条通道，04.kube-master 会一路重试到超时。'
        )
    return (
        f'{address}：平台机到它不通，已把它临时接管到本机 {ALIAS_DEVICE} 上，'
        f'并把 {address}:{listen} 经部署密钥通道转到 {host} 的 {destination}:{listen}。'
        f'{why}'
    ) + (f'（隧道 SSH 走 {host}:{port}）' if port != 22 else '')


def _failed_message(address, reason, route=None):
    """拼出「通道建不起来」的日志文本。

    参数：`address` 表示集群内网地址；`reason` 表示失败原因；`route` 表示链路定义。
    返回：返回中文文本。
    副作用：纯计算。
    """
    spec = route or API_ROUTE
    if spec.get('key') == VIP_ROUTE['key']:
        return (
            f'{address}：平台机连不上它，本机也没能建起转发通道（{reason}）。'
            'kubeasz 的 ex-lb 剧本最后一步在**平台机**上拨这个 VIP 验活（connection: local），'
            f'所以第 8 阶段会以「Request failed: <urlopen error timed out>」失败（重试 2 次、'
            '每次约 30 秒）—— 那时 01-07 阶段与两台 LB 机器上的组件都已经装好了，集群本身可用。'
            f'要让平台管得了这个集群，需要平台机与 {address} 所在网络互通，'
            '或者让平台机能以 root 建立转发（挂地址需要权限）。'
        )
    if spec.get('key') == ETCD_ROUTE['key']:
        return (
            f'{address}：平台机连不上它，本机也没能建起转发通道（{reason}）。'
            '集群备份的剧本在**平台机**上执行 etcdctl（hosts: localhost），'
            f'它打的就是 {address}，所以备份会在这一步失败 —— 而且是安静地失败：'
            'etcdctl 没有默认超时，它会一直等下去，任务既不出错也不结束。'
            f'要在这种网络下备份，需要平台机与 {address} 所在网络互通，'
            '或者让平台机能以 root 建立转发（挂地址需要权限）。'
        )
    return (
        f'{address}：平台机连不上它，本机也没能建起转发通道（{reason}）。'
        'kubeasz 的 kube-master 与网络插件角色会在**平台机**上执行 kubectl（connection: local），'
        f'它们打的就是 {address}，所以部署会在那里失败，部署完的健康检查也连不上 API Server。'
        f'要平台管得了这个集群，需要平台机与 {address} 所在网络互通，'
        '或者让平台机能以 root 建立转发（挂地址需要权限）。'
    )
