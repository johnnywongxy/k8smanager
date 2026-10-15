"""用 SSH 只读命令读回每台机器的**主机**水位：CPU、内存、磁盘。

与走 k8s API 的那半个可观测性（Pod / 工作负载计数）刻意分开：这里回答的是「这几台机器
自己还撑得住吗」，Pod 的数字回答不了这个问题 —— 一台节点的 kubelet 起不来时它的 Pod
计数是 0，看起来比谁都健康，而磁盘已经 96%。

两个出口共用同一套命令与解析：机器列表页逐台显示三列水位（``collect_machine_usage``，
按机器主键取数、密码优先），以及按集群看它的节点（``collect_cluster_usage``，走配置版本
快照与平台部署密钥；界面上暂时没有入口，见 ``K8sHostUsageView`` 的说明）。

**全部只读**：``cat /proc/*`` 与 ``df``，不写文件、不改配置、不装任何东西。指标取自
``/proc`` 与 ``df`` 而不是 metrics-server/kubelet summary：主机层面的 CPU 与磁盘占用
本来就不在 k8s 的指标里（cadvisor 报的是容器口径，不含宿主机上别的进程与数据盘）。

**采样口径**：CPU 利用率取自 ``/proc/stat`` 的**两次采样**（间隔 1 秒）差值 —— 单次读出来的
是机器开机以来的累计平均，一台跑了半年的机器永远显示 5%，看不出刚刚跑满。内存用
``MemAvailable``（内核估计的「还能给新进程用多少」），比 ``MemFree`` 贴近真实可用量。
磁盘用 ``df -P -B1``（POSIX 输出、字节为单位），伪文件系统在 Python 侧过滤。

节点清单与凭据来自集群配置版本里固化的那份（``cluster_ops.node_payloads``），因此
**只有平台部署的集群**能采集：导入型集群平台没有它的节点清单与免密通道。
"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from django.utils import timezone

from . import credentials, machine_ssh, node_registry

# 单台节点的采集命令：一条连接、一条命令，先读全部静态量，再隔 1 秒读第二次 CPU 计数。
#
# 标记行（全大写前缀）用来分段解析：meminfo 与 df 都是多行输出，靠这两个标记把它们的边界
# 划出来，就不会因为某台机器少打印了一行而错位到别的段落里去。
COLLECT_COMMAND = (
    'echo "HOSTNAME $(cat /proc/sys/kernel/hostname 2>/dev/null)"; '
    'echo "UPTIME $(cut -d\' \' -f1 /proc/uptime 2>/dev/null)"; '
    'echo "LOADAVG $(cut -d\' \' -f1-3 /proc/loadavg 2>/dev/null)"; '
    'echo "CORES $(grep -c ^processor /proc/cpuinfo 2>/dev/null)"; '
    'echo "MEMINFO"; cat /proc/meminfo 2>/dev/null; '
    'echo "DF"; df -P -B1 2>/dev/null; '
    'echo "STAT1"; grep "^cpu " /proc/stat 2>/dev/null; '
    'sleep 1; '
    'echo "STAT2"; grep "^cpu " /proc/stat 2>/dev/null'
)

# 一次采集总耗时 = 连接 + 「1 秒采样间隔」。命令超时给 12 秒：采集本身只要 2 秒左右，
# 留出的余量给慢速磁盘上的 df。
CONNECT_TIMEOUT = 8
COMMAND_TIMEOUT = 12

# 单次采集最多同时连几台节点。节点数量通常个位数，8 是「够快」与「别在目标机上同时铺开
# 一堆 SSH 会话」之间的折中。
MAX_WORKERS = 8

# 不当成「真实磁盘」统计的文件系统类型与挂载点前缀。kubelet/containerd 会在数据盘上挂出一
# 大堆 bind mount，一条条列进界面只会把真正要看的那几块盘淹掉。
PSEUDO_FILESYSTEMS = frozenset({
    'tmpfs', 'devtmpfs', 'overlay', 'overlay2', 'squashfs', 'shm', 'ramfs', 'none',
    'autofs', 'proc', 'sysfs', 'cgroup', 'cgroup2', 'debugfs', 'tracefs', 'mqueue',
    'devpts', 'securityfs', 'pstore', 'bpf', 'configfs', 'fusectl', 'hugetlbfs',
})
PSEUDO_MOUNT_PREFIXES = (
    '/run', '/sys', '/dev', '/proc', '/var/lib/kubelet', '/var/lib/docker',
    '/var/lib/containerd', '/snap', '/var/lib/rancher',
)

# 一次采集里，超过这个使用率就算「需要注意」，供前端上色（与证书那三档同一个思路）。
DISK_WARNING_PERCENT = 80.0
DISK_DANGER_PERCENT = 90.0


# ---- 纯计算：解析与换算 ------------------------------------------------


def _to_int(text):
    """把一段文本转成整数。

    参数：`text` 表示待转换的文本。
    返回：返回整数；转换不了时返回 0。
    副作用：只做纯计算。
    """
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return 0


def _to_float(text):
    """把一段文本转成浮点数。

    参数：`text` 表示待转换的文本。
    返回：返回浮点数；转换不了时返回 0.0。
    副作用：只做纯计算。
    """
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return 0.0


def _percent(used, total):
    """算使用率百分比，保留一位小数。

    参数：`used` 表示已用量；`total` 表示总量。
    返回：返回百分比浮点数；总量为 0 时返回 0.0。
    副作用：只做纯计算。
    """
    if not total:
        return 0.0
    return round(max(0.0, min(1.0, float(used) / float(total))) * 100, 1)


def parse_cpu_times(text):
    """解析 ``/proc/stat`` 的第一行 ``cpu …``。

    参数：`text` 表示 ``grep '^cpu ' /proc/stat`` 的输出。
    返回：返回 ``(总时间, 空闲时间)``，都是内核的 jiffies；解析不出来时返回 ``(0, 0)``。
    副作用：只做纯计算。

    字段顺序固定：user nice system idle iowait irq softirq steal guest guest_nice。
    「空闲」取 idle + iowait —— iowait 期间 CPU 并没有在干活，算进忙碌里会让一台等磁盘的
    机器看起来满负荷。
    """
    for line in str(text or '').splitlines():
        parts = line.split()
        if not parts or parts[0] != 'cpu' or len(parts) < 5:
            continue
        values = [_to_int(item) for item in parts[1:]]
        # guest/guest_nice 已经计在 user/nice 里，重复计入会让总量偏大，因此只取前 8 项。
        values = values[:8]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle
    return 0, 0


def cpu_percent(first, second):
    """由两次 ``/proc/stat`` 采样算出 CPU 使用率。

    参数：`first` 表示第一次采样文本；`second` 表示第二次采样文本。
    返回：返回百分比浮点数；两次之间没有流逝的时间时返回 ``None``。
    副作用：只做纯计算。
    """
    total_a, idle_a = parse_cpu_times(first)
    total_b, idle_b = parse_cpu_times(second)
    delta_total = total_b - total_a
    if not total_a or not total_b or delta_total <= 0:
        return None
    delta_idle = max(0, idle_b - idle_a)
    return _percent(delta_total - delta_idle, delta_total)


def parse_meminfo(text):
    """解析 ``/proc/meminfo``，返回内存总量与可用量（字节）。

    参数：`text` 表示 ``/proc/meminfo`` 的内容。
    返回：返回 ``{'total_bytes', 'available_bytes', 'percent'}``。
    副作用：只做纯计算。

    优先用 ``MemAvailable``（内核自己估计的「还能拿给新进程用」的量，已经扣掉了不可回收的
    缓存）。老内核（3.14 之前，CentOS 7 是 3.10）没有这个字段，这时按
    ``MemFree + Buffers + Cached`` 估算 —— 对 3.10 来说这个口径偏保守，宁可把占用报高一点，
    也不要让一台真正吃紧的机器看起来空闲。
    """
    values = {}
    for line in str(text or '').splitlines():
        key, _, rest = line.partition(':')
        if not rest:
            continue
        values[key.strip()] = _to_int(rest.split()[0] if rest.split() else 0)
    total = values.get('MemTotal', 0) * 1024
    available = values.get('MemAvailable', 0) * 1024
    if not available:
        available = (values.get('MemFree', 0) + values.get('Buffers', 0) + values.get('Cached', 0)) * 1024
    if not total:
        return {'total_bytes': 0, 'available_bytes': 0, 'percent': 0.0}
    used = max(0, total - available)
    return {
        'total_bytes': total,
        'available_bytes': available,
        'used_bytes': used,
        'percent': _percent(used, total),
    }


def parse_df(text):
    """解析 ``df -P -B1`` 的输出，只留下真实的磁盘。

    参数：`text` 表示 ``df`` 的输出。
    返回：返回 ``[{'device','mount','total_bytes','used_bytes','percent'}, ...]``。
    副作用：只做纯计算。

    ``-P`` 保证每行一条记录、不会折行；挂载点里可能有空格，所以第 6 个字段之后的部分
    整体拼回来当挂载点。
    """
    mounts = []
    for line in str(text or '').splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        device = parts[0]
        mount = ' '.join(parts[5:])
        # ``df`` 的输出里没有文件系统类型，只能从设备名认：真实块设备一定是 ``/dev/...``。
        # 不是本地块设备的一律不要 —— 网络文件系统（CIFS 写成 ``//server/share``、NFS 写成
        # ``host:/export``）、``systemd-1``、``-`` 这类占位条目都不是「这台机器上的盘」，混进来
        # 会让磁盘水位变成一个与硬件无关的数（一台机器挂了个几乎空的 NFS，平均下来就显得很闲）。
        # 注意 ``//server/share`` 也以 ``/`` 开头，必须排在 ``/`` 判断之前。
        if ':' in device or device.startswith('//') or not device.startswith('/'):
            continue
        if device.rsplit('/', 1)[-1] in PSEUDO_FILESYSTEMS:
            continue
        if mount.startswith(PSEUDO_MOUNT_PREFIXES):
            continue
        total = _to_int(parts[1])
        used = _to_int(parts[2])
        if total <= 0:
            continue
        mounts.append({
            'device': device,
            'mount': mount,
            'total_bytes': total,
            'used_bytes': used,
            'percent': _percent(used, total),
        })
    mounts.sort(key=lambda item: item['percent'], reverse=True)
    return mounts


def parse_collect_output(text):
    """把一次采集的原始输出解析成结构化结果。

    参数：`text` 表示命令输出。
    返回：返回 ``{'hostname','uptime_seconds','load','cores','memory','mounts','cpu_percent'}``。
    副作用：只做纯计算。

    按标记行切段：标记之前的散装键值行按前缀取用，``MEMINFO`` 与 ``DF`` 之间的行喂给
    meminfo 解析器，``DF`` 与 ``STAT1`` 之间的行喂给 df 解析器，两次 ``STAT`` 之间的行
    喂给 CPU 采样解析器。任何一段缺失都只是那一项为空，不影响其余项。
    """
    result = {
        'hostname': '', 'uptime_seconds': 0, 'load': [], 'cores': 0,
        'memory': {'total_bytes': 0, 'available_bytes': 0, 'used_bytes': 0, 'percent': 0.0},
        'mounts': [], 'cpu_percent': None,
    }
    section = ''
    buffers = {'meminfo': [], 'df': [], 'stat1': [], 'stat2': []}
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if stripped in ('MEMINFO', 'DF', 'STAT1', 'STAT2'):
            section = stripped.lower()
            continue
        if not stripped:
            continue
        if section in buffers:
            buffers[section].append(stripped)
            continue
        keyword, _, rest = stripped.partition(' ')
        value = rest.strip()
        if keyword == 'HOSTNAME':
            result['hostname'] = value
        elif keyword == 'UPTIME':
            result['uptime_seconds'] = int(_to_float(value))
        elif keyword == 'LOADAVG':
            result['load'] = [_to_float(item) for item in value.split()[:3]]
        elif keyword == 'CORES':
            result['cores'] = _to_int(value)

    result['memory'] = parse_meminfo('\n'.join(buffers['meminfo']))
    result['mounts'] = parse_df('\n'.join(buffers['df']))
    result['cpu_percent'] = cpu_percent(
        '\n'.join(buffers['stat1']), '\n'.join(buffers['stat2']),
    )
    return result


def root_mount(mounts):
    """挑出这台机器的「系统盘」条目。

    参数：`mounts` 表示 ``parse_df`` 的结果。
    返回：返回挂载点为 ``/`` 的那一项；没有则返回使用率最高的那一项；都没有时返回 None。
    副作用：只做纯计算。

    界面上行内显示的是这一项，其余挂载点放在明细里 —— 一台机器有几个数据盘时，
    「哪个数才算这台机器的磁盘占用」这个问题没有唯一答案，把最不会引起误解的那一个摆出来，
    其余的原样列给人看。
    """
    for item in mounts:
        if item['mount'] == '/':
            return item
    return mounts[0] if mounts else None


# ---- 采集 --------------------------------------------------------------


def _key_opener(node, credential):
    """默认连接方式：用平台部署密钥登录。

    参数：`node` 表示节点快照；`credential` 表示 ``{'user','port'}``。
    返回：返回已连接的 paramiko 客户端。
    副作用：发起一次网络连接。

    只有平台部署过的节点才有这把密钥（部署时下发过公钥），因此它是「集群节点」的通道；
    机器列表里那些还没进集群的机器走 ``_machine_opener`` 的密码通道。
    """
    return node_registry.open_session(node, credential, timeout=CONNECT_TIMEOUT)


def _machine_opener(machine, password):
    """构造机器列表专用的连接方式：优先用托管密码，没有密码时回落平台密钥。

    参数：`machine` 表示 Machine 实例；`password` 表示解密后的密码（可为空或 None）。
    返回：返回一个 ``(node, credential) -> client`` 的可调用对象。
    副作用：不发起连接；只在被调用时建立一次网络连接。

    机器列表里的机器不一定进过集群，没有平台公钥；而它们几乎都托管了密码（新增机器时填的），
    所以密码是这里的主通道。反过来，「已进集群但没托管密码」的机器有平台公钥，
    回落一次能让它照样采到，不必逼用户补一份密码。

    这条优先级规则本身住在 ``machine_ssh.open_client`` 里 —— 机器列表的 SSH 终端用的是
    同一条，两边不能各写一份。这里只负责把节点快照里的地址、端口、用户取出来交给它。
    """
    def opener(node, credential):
        """按上面说的优先级建立一条连接。"""
        target = credential or {}
        host = str(getattr(node, 'ip_address', '') or '')
        port = int(target.get('port') or getattr(node, 'ssh_port', 0) or 22)
        user = target.get('user') or getattr(node, 'ssh_user', None) or 'root'
        return machine_ssh.open_client(
            host=host, port=port, user=user, password=password,
            credential=credential, timeout=CONNECT_TIMEOUT,
        )

    return opener


def _collect_one(payload, credential, opener=None):
    """采集一台机器，失败时返回带错误说明的结果。

    参数：`payload` 表示节点快照（``cluster_ops.node_payloads`` 里的一项，或
        ``collect_machine_usage`` 从机器表拼出来的那一份）；`credential` 表示连接信息；
        `opener` 表示建立连接的可调用对象，省略时用平台的部署密钥登录。
    返回：返回该机器的采集结果字典。
    副作用：建立一次 SSH 连接并执行一条只读命令。

    单台失败只体现在它自己这一行上（``status='failed'`` + 中文原因）：一台机器关机不该让
    整块水位表变空 —— 恰恰相反，那种时候更要看得到其余几台。
    """
    node = SimpleNamespace(**payload)
    label = str(payload.get('name') or payload.get('ip_address') or '')
    base = {
        'label': label,
        'ip_address': str(payload.get('ip_address') or ''),
        'node_name': str(payload.get('node_name') or ''),
        'roles': list(payload.get('roles') or []),
        'status': 'ok',
        'error': '',
        'hostname': '',
        'hostname_matches': False,
        'cores': 0,
        'cpu_percent': None,
        # 内存这一项与 CPU、磁盘一样给 None，不给 0.0：失败就是没采到，而 0.0 到了
        # 列表页会被原样显示成「内存 0%」—— 把「读不到」说成了「很空闲」。
        'memory': {'total_bytes': 0, 'used_bytes': 0, 'percent': None},
        'disk': None,
        'mounts': [],
        'load': [],
        'uptime_seconds': 0,
    }
    client = None
    try:
        client = (opener or _key_opener)(node, credential)
        code, out, err = node_registry.run(client, COLLECT_COMMAND, timeout=COMMAND_TIMEOUT)
        if code != 0:
            raise node_registry.NodeRegistryError(
                'host_usage_failed', (err or out or f'退出码 {code}').strip(),
            )
        parsed = parse_collect_output(out)
    except Exception as exc:  # noqa: BLE001 - 一台失败不影响其余节点
        base['status'] = 'failed'
        base['error'] = str(getattr(exc, 'message', None) or exc)[:240] or '采集失败'
        return base
    finally:
        node_registry.close_session(client)

    hostname = parsed['hostname']
    base.update({
        'hostname': hostname,
        'hostname_matches': bool(hostname) and hostname == base['node_name'],
        'cores': parsed['cores'],
        'cpu_percent': parsed['cpu_percent'],
        'memory': parsed['memory'],
        'mounts': parsed['mounts'],
        'disk': root_mount(parsed['mounts']),
        'load': parsed['load'],
        'uptime_seconds': parsed['uptime_seconds'],
    })
    return base


def _average(values):
    """算出平均值，保留一位小数。

    参数：`values` 表示数值列表（可含 None）。
    返回：返回平均值；没有有效值时返回 None。
    副作用：只做纯计算。
    """
    numbers = [item for item in values if isinstance(item, (int, float))]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 1)


def summarize(nodes):
    """把每台节点的结果汇总成集群级水位。

    参数：`nodes` 表示 ``_collect_one`` 的结果列表。
    返回：返回 ``{'node_count', 'ok_count', 'failed_count', 'cpu_avg', 'memory_avg', 'disk_max'}``。
    副作用：只做纯计算。

    CPU 与内存取**平均值**（集群整体还剩多少余量），磁盘取**最高值**（哪台先满就是它先出事，
    平均会把一块快满的盘摊平掉）。
    """
    ok_nodes = [item for item in nodes if item.get('status') == 'ok']
    disk_values = [
        item['disk']['percent'] for item in ok_nodes if isinstance(item.get('disk'), dict)
    ]
    return {
        'node_count': len(nodes),
        'ok_count': len(ok_nodes),
        'failed_count': len(nodes) - len(ok_nodes),
        'cpu_avg': _average([item.get('cpu_percent') for item in ok_nodes]),
        'memory_avg': _average([
            (item.get('memory') or {}).get('percent') for item in ok_nodes
        ]),
        'disk_max': max(disk_values) if disk_values else None,
    }


def collect_cluster_usage(cluster):
    """采集一个集群全部节点的主机水位。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'available', 'reason', 'sampled_at', 'summary', 'nodes'}``。
    副作用：与每台节点建立一次 SSH 连接并执行只读命令。

    只有平台部署的集群能采集（节点清单与免密通道都在平台手上）；导入型集群、已销毁的集群、
    以及没有配置版本的集群一律返回 ``available=False`` 加一句中文原因，由前端原样展示。
    """
    from . import cluster_ops

    payloads = []
    try:
        cluster_ops.assert_deployed(cluster)
        payloads = cluster_ops.node_payloads(cluster)
    except Exception as exc:  # noqa: BLE001 - 取不到清单是一种正常结果，不是错误
        return {
            'available': False,
            'reason': str(getattr(exc, 'message', None) or exc),
            'sampled_at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
            'summary': summarize([]),
            'nodes': [],
        }

    machines = [item['machine'] for item in payloads if item.get('machine')]
    credential_map = credentials.credential_map(machines, include_password=False)
    for item in payloads:
        ip = str(item.get('ip_address') or '')
        credential_map.setdefault(ip, {'user': item.get('ssh_user') or 'root', 'port': int(item.get('ssh_port') or 22)})

    workers = min(MAX_WORKERS, max(1, len(payloads)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        nodes = list(pool.map(
            lambda item: _collect_one(item, credential_map.get(str(item.get('ip_address') or '')) or {}),
            payloads,
        ))

    return {
        'available': True,
        'reason': '',
        'sampled_at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
        'summary': summarize(nodes),
        'nodes': nodes,
    }


def _root_percent(mounts):
    """取根分区的使用率。

    参数：`mounts` 表示 ``parse_df`` 的结果。
    返回：返回挂载点为 ``/`` 的那一项的使用率；没有根分区条目时返回 None。
    副作用：只做纯计算。

    机器列表那一行只答一个问题：「这台机器的系统盘还剩多少」。因此**只认 ``/``**，不像
    集群侧那样回落到最紧张的挂载点（``root_mount``）—— 那台机器上另挂的数据盘跟系统盘
    是不是要满了没有关系，混进来会让人误判。
    """
    for item in mounts:
        if item['mount'] == '/':
            return item['percent']
    return None


def _machine_password(machine):
    """读出机器托管的 SSH 密码，读不出来时返回 None。

    参数：`machine` 表示 Machine 实例。
    返回：返回解密后的密码；未托管或解密失败时返回 None。
    副作用：只读取数据库中的密文字段。

    解密失败按「没有密码」处理而不是抛错：机器列表是流水账，一台机器的密码坏掉不该让
    整页采集失败 —— 它会走平台密钥那条路，走不通就只标这一行失败。
    """
    try:
        return machine.get_password() or None
    except Exception:  # noqa: BLE001 - 解密失败等同于没有密码
        return None


def collect_machine_usage(machines):
    """采集一批机器的主机水位，供机器列表页逐行显示。

    参数：`machines` 表示 Machine 实例列表（顺序即返回顺序）。
    返回：返回 ``{'sampled_at', 'items': [{'machine_id', 'status', 'error', 'hostname',
        'cpu_percent', 'memory_percent', 'disk_percent'}]}``。
    副作用：与每台机器建立一次 SSH 连接并执行只读命令。

    与 ``collect_cluster_usage`` 共用同一条采集命令与同一套解析，差别只在连接通道：
    这里面对的是机器表里的机器（不一定进过集群），密码优先（见 ``_machine_opener``）。
    """
    def collect(entry):
        """采集一台并压成列表页要的那几个数。"""
        machine, payload, credential, opener = entry
        result = _collect_one(payload, credential, opener=opener)
        return {
            'machine_id': machine.pk,
            'status': result['status'],
            'error': result['error'],
            'hostname': result['hostname'],
            'cpu_percent': result['cpu_percent'],
            'memory_percent': (result.get('memory') or {}).get('percent'),
            'disk_percent': _root_percent(result.get('mounts') or []),
        }

    entries = [
        (
            machine,
            {
                'machine': machine,
                'name': machine.name,
                'ip_address': machine.ip_address,
                'ssh_port': machine.ssh_port,
                'ssh_user': machine.ssh_user,
                'roles': list(machine.roles or []),
                # 机器表里的机器还没有集群节点名：hostname 归它自己，只有进了集群才会被改成
                # 节点名（见 ``node_hostname``），因此这里不参与「名字是否一致」的比对。
                'node_name': '',
            },
            {'user': machine.ssh_user, 'port': machine.ssh_port},
            # 密码在派发前于当前线程解密：线程池里再去碰模型字段，连接与事务边界都要另说。
            _machine_opener(machine, _machine_password(machine)),
        )
        for machine in machines
    ]

    workers = min(MAX_WORKERS, max(1, len(entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        items = list(pool.map(collect, entries))

    return {
        'sampled_at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
        'items': items,
    }
