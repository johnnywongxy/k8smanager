"""在集群节点上托管镜像仓库，让镜像只在节点内网里流动。

kubeasz 的镜像一律从 ``easzlab.io.local:5000`` 拉，这个名字由
``roles/prepare/tasks/main.yml`` 写进每个节点的 ``/etc/hosts``，值取
``ansible_env.SSH_CLIENT.split(' ')[0]`` —— **节点看到的 SSH 源地址**。平台与节点不在同一张网里
时，那是中间 NAT 的出口地址，节点的 5000 端口回不去：containerd 连 pause 沙箱镜像都拉不到，
sandbox 起不来，CNI 不初始化，**一个 Pod 都起不来**。日志里只有一句
``dial tcp <出口地址>:5000: i/o timeout``，从部署记录里完全看不出跟网络拓扑有关系。

平台原本把 registry 跑在自己本机（见 ``registry`` 模块），那条路要求节点能反向连上平台，
只在「平台与节点同网」时成立。本模块补上不成立的那一半：把 registry 挪到**节点侧**，
镜像经平台的 SSH 正向隧道推进去（隧道复用 ``platform_relay.Relay``），其余节点从集群内网拉。

**为什么是跑二进制而不是起容器。** 节点上没有 docker —— kubeasz 只装 containerd，而
``ctr run`` 要自己拼 OCI spec、还要跟容器运行时打交道，同样的问题在节点上比一个 systemd 单元
难查得多。registry 的官方镜像里本来就是一个静态链接的 Go 二进制（``/bin/registry``，
``CGO_ENABLED=0``，所以在 CentOS 7 的 glibc 2.17 上直接能跑），平台本机已有该镜像，
用 ``docker create`` + ``docker cp`` 取出来交给 systemd 托管即可。

**这不是 kubeasz 的标准做法**，所以每一步都按「可解释、可手工复核」来写：二进制、配置、
数据目录、单元文件全部落在固定路径，启动失败时把 journal 的尾巴一起报给操作员。

**/etc/hosts 只能在 01.prepare 之后改。** 那个 role 用 ``regexp`` 匹配 ``easzlab.io.local``
整行替换，提前写进去的会被它覆盖（见 ``registry`` 模块开头的同类说明），所以
``align_resolution`` 必须排在 ``01.prepare`` 之后调用。
"""

import time
import urllib.error
import urllib.request
from pathlib import Path

from . import compiler, credentials, platform_relay

# 节点上 registry 监听的端口。与 kubeasz 的全局约定一致，containerd 的 certs.d 已按这个名字配好。
REGISTRY_PORT = 5000
# 平台本机上隧道监听的端口。刻意不用 5000：平台自己的 registry 可能正占着它。
TUNNEL_PORT = 15000
# 隧道在两端都只走回环：平台侧 docker 推到 127.0.0.1:TUNNEL_PORT，节点侧 registry 只在
# 127.0.0.1:5000 被隧道拨到 —— 集群里其它节点用的是另一条路（见 align_resolution）。
LOOPBACK = '127.0.0.1'
# 节点上的落点。二进制放 /usr/local/bin（在 PATH 上，且不与 kubeasz 的 /opt/kube/bin 抢地方），
# 数据目录沿用平台本机那份的名字，便于运维按同一份文档排查。
BINARY_PATH = '/usr/local/bin/k8smanager-registry'
CONFIG_DIR = '/etc/k8smanager'
CONFIG_PATH = f'{CONFIG_DIR}/registry.yml'
DATA_DIR = '/opt/kube/registry'
UNIT_NAME = 'k8smanager-registry.service'
UNIT_PATH = f'/etc/systemd/system/{UNIT_NAME}'
# registry 二进制在官方镜像里的固定路径。
BINARY_IN_IMAGE = '/bin/registry'
# 单条远程命令的超时秒数。
COMMAND_TIMEOUT = 300
# 隧道就绪探测的次数与间隔（秒）。
READY_ATTEMPTS = 15
READY_INTERVAL = 2.0
# hosts 里 kubeasz 约定的名字。
REGISTRY_HOSTNAME = 'easzlab.io.local'
# 删除那一行用的正则（点要转义，否则 . 会匹配任意字符）。写入与擦除都走这一份，避免两边不一致。
HOSTS_PATTERN = r'easzlab\.io\.local'


class NodeRegistryError(Exception):
    """节点侧镜像仓库无法就绪，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    与 ``registry.RegistryError`` 同理，本模块不直接抛 ``state_machine.TaskAborted``：
    那个类在导入方向上属于上层，由调用方转换。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# ---- 选点与地址 --------------------------------------------------------


def registry_host_node(nodes):
    """选出托管镜像仓库的那台节点。

    参数：`nodes` 表示本次部署的节点快照列表。
    返回：返回被选中的节点快照。
    副作用：只做纯计算。

    单节点就用它自己；多节点取第一台 master —— 镜像仓库是集群级设施，放在控制面节点上，
    运维找起来有固定位置。没有 master（纯 worker 清单，正常流程到不了这里）时退回第一台。

    按 ``id`` 排序再选，是为了让同一份节点清单永远选出同一台：宿主一旦漂移，新宿主上
    没有旧数据，每次部署都要重传一遍全部镜像。
    """
    items = sorted(nodes or [], key=lambda item: getattr(item, 'id', 0) or 0)
    if not items:
        raise NodeRegistryError('no_nodes', '本次部署没有任何节点，无法准备镜像仓库。')
    if len(items) == 1:
        return items[0]
    for item in items:
        if 'master' in (getattr(item, 'roles', None) or []):
            return item
    return items[0]


def registry_address(node):
    """返回镜像仓库在集群内网里的地址。

    参数：`node` 表示托管镜像仓库的节点快照。
    返回：返回内网地址文本。
    副作用：只做纯计算。

    取的是 ``compiler.cluster_address``，也就是 inventory 的主机名 —— 节点网卡上真实存在的
    地址，集群里每台机器都到得了。用操作员填的公网地址会失败：节点之间未必通公网，
    而它们一定通内网。
    """
    return compiler.cluster_address(node)


# ---- 配置文本 ----------------------------------------------------------


def render_config(data_dir=DATA_DIR):
    """生成 registry 的配置文件文本。

    参数：`data_dir` 表示镜像数据目录。
    返回：返回 YAML 文本。
    副作用：只做纯计算。

    ``http.addr`` 绑 ``0.0.0.0``：隧道从回环拨进来，其余节点从内网地址拨进来，两种来源都要收。
    明文 HTTP 是刻意的 —— containerd 侧 ``easzlab.io.local:5000`` 的 certs.d 就是这么配的，
    换成 TLS 反而要重新分发证书。
    """
    return (
        'version: 0.1\n'
        'log:\n'
        '  level: info\n'
        '  formatter: text\n'
        '  fields:\n'
        '    service: k8smanager-registry\n'
        'storage:\n'
        '  filesystem:\n'
        f'    rootdirectory: {data_dir}\n'
        '  delete:\n'
        '    enabled: true\n'
        '  cache:\n'
        '    blobdescriptor: inmemory\n'
        '  maintenance:\n'
        '    uploadpurging:\n'
        '      enabled: true\n'
        '      age: 168h\n'
        '      interval: 24h\n'
        # 这个键不能省：distribution 解析 uploadpurging 时缺 dryrun 会直接
        # panic（Unable to parse upload purge configuration: dryrun missing），
        # 服务起不来、部署停在镜像分发这一步，而报错里看不出跟配置有关。
        '      dryrun: false\n'
        'http:\n'
        f'  addr: 0.0.0.0:{REGISTRY_PORT}\n'
        '  headers:\n'
        '    X-Content-Type-Options: [nosniff]\n'
    )


def render_unit(binary_path=BINARY_PATH, config_path=CONFIG_PATH):
    """生成 systemd 单元文本。

    参数：`binary_path` 表示 registry 二进制路径；`config_path` 表示配置文件路径。
    返回：返回单元文件文本。
    副作用：只做纯计算。

    ``Restart=always``：这台机器重启后 registry 要自己回来。镜像仓库是集群的长期依赖 ——
    节点重启、镜像被 GC、以后加 addon 都会再拉一次，它不能只活在部署的那几十分钟里。
    """
    return (
        '[Unit]\n'
        'Description=k8smanager node image registry\n'
        'After=network-online.target\n'
        'Wants=network-online.target\n'
        '\n'
        '[Service]\n'
        'Type=simple\n'
        f'ExecStart={binary_path} serve {config_path}\n'
        'Restart=always\n'
        'RestartSec=3\n'
        'LimitNOFILE=65536\n'
        '\n'
        '[Install]\n'
        'WantedBy=multi-user.target\n'
    )


# ---- 远程动作 ----------------------------------------------------------


def open_session(node, credential=None, key_path=None, client_factory=None, timeout=None):
    """建立一条到节点的 SSH 连接。

    参数：`node` 表示节点快照；`credential` 表示 ``{'user','port','password'}``；`key_path` 表示
        平台部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试；`timeout` 表示超时秒数。
    返回：返回已连接的 paramiko.SSHClient。
    副作用：发起一次网络连接。

    一律用部署密钥登录（``credentials.ensure_key_login`` 在凭据阶段已经铺好），不走密码：
    密码只用于第一次下发公钥，这里再带一份没有意义，还会让密码留在调用链上。
    """
    target = credential or {}
    factory = client_factory or credentials.ssh_client
    user = target.get('user') or getattr(node, 'ssh_user', None) or 'root'
    port = int(target.get('port') or getattr(node, 'ssh_port', None) or 22)
    kwargs = {
        'host': str(getattr(node, 'ip_address', '') or ''),
        'port': port,
        'username': user,
        'key_path': key_path or credentials.private_key_path(),
    }
    if timeout is not None:
        kwargs['timeout'] = timeout
    return factory(**kwargs)


def close_session(client):
    """关闭一条 SSH 连接，忽略重复关闭。

    参数：`client` 表示已连接的客户端或 None。
    返回：无显式返回值。
    副作用：关闭连接。
    """
    if client is None:
        return
    try:
        client.close()
    except Exception:  # noqa: BLE001 - 关闭失败没有补救手段，也不该影响别的节点
        pass


def run(client, command, timeout=COMMAND_TIMEOUT):
    """在节点上执行一条命令。

    参数：`client` 表示已连接的客户端；`command` 表示命令文本；`timeout` 表示读取超时秒数。
    返回：返回 ``(退出码, 标准输出, 标准错误)``。
    副作用：在目标机上执行命令。
    """
    return credentials.run_command(client, command, timeout=timeout)


def put_file(client, local_path, remote_path, mode=0o644, expected_size=None):
    """把平台上的文件传到一个节点。

    参数：`client` 表示已连接的客户端；`local_path` 表示本机文件路径；`remote_path` 表示
        目标路径；`mode` 表示落地权限；`expected_size` 表示期望字节数，给定时回读核对。
    返回：返回目标文件在节点上的字节数。
    副作用：在目标机上创建或覆盖文件。

    用 SFTP 而不是 ``cat > file``：二进制里有空字节与控制字符，经 shell 搬运要么走 base64
    放大体积，要么被引号规则咬到。传完回读大小 —— 几十 MB 的公网链路中途断掉时，
    ``put`` 自己可能已经返回成功，而截断的二进制要到 systemd 启动失败才会暴露。
    """
    local = Path(local_path)
    size = expected_size if expected_size is not None else local.stat().st_size
    try:
        sftp = client.open_sftp()
    except Exception as exc:  # noqa: BLE001 - 各类 SFTP 不可用的表现不同，统一成一句人话
        raise NodeRegistryError(
            'sftp_unavailable',
            f'无法通过 SFTP 向节点传文件（{exc}）。节点的 sshd 需要启用 SFTP 子系统'
            '（``Subsystem sftp``），否则无法把镜像仓库的二进制装上去。',
        ) from exc
    try:
        sftp.put(str(local), str(remote_path))
        sftp.chmod(str(remote_path), mode)
        landed = sftp.stat(str(remote_path)).st_size
    except Exception as exc:  # noqa: BLE001 - 同上，远端行为不可控
        raise NodeRegistryError(
            'upload_failed',
            f'向节点写 {remote_path} 失败：{exc}。',
        ) from exc
    finally:
        try:
            sftp.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已经落地的内容
            pass
    if landed != size:
        raise NodeRegistryError(
            'upload_incomplete',
            f'{remote_path} 传完后大小是 {landed} 字节，平台侧是 {size} 字节，传输被截断，已中止。',
        )
    return landed


def _service_active(client):
    """判断节点上的镜像仓库服务是否已经在跑。

    参数：`client` 表示已连接的客户端。
    返回：active 返回 True，否则 False。
    副作用：在目标机上执行一条只读命令。
    """
    code, _out, _err = run(client, f'systemctl is-active --quiet {UNIT_NAME}')
    return code == 0


def _journal_tail(client, lines=20):
    """取回服务日志的尾巴，用于拼装可自解释的失败原因。

    参数：`client` 表示已连接的客户端；`lines` 表示取多少行。
    返回：返回日志文本，取不到时返回空串。
    副作用：在目标机上执行一条只读命令。
    """
    code, out, _err = run(client, f'journalctl -u {UNIT_NAME} -n {int(lines)} --no-pager 2>&1')
    return out if code == 0 else ''


def install(context, node, credential, binary_path, work_dir, key_path=None,
            client_factory=None, relay_factory=None):
    """确保镜像宿主节点上有一个装好并在跑的 registry。

    参数：`context` 表示任务上下文；`node` 表示宿主节点快照；`credential` 表示连接信息；
        `binary_path` 表示平台侧已取出的 registry 二进制；`work_dir` 表示存放配置与单元副本的
        本地目录；`key_path` 表示部署私钥路径；`client_factory` / `relay_factory` 便于测试注入。
    返回：返回 ``{'address': 内网地址, 'installed': bool, 'port': int}`` 摘要。
    副作用：在目标节点上写三个文件、创建数据目录、可能放行防火墙端口并启动 systemd 服务。

    幂等：服务已经在跑就直接返回，不重传二进制。重跑部署时这一步应当接近零开销 ——
    registry 的数据目录是持久的（``DATA_DIR``），镜像还在里面，后面的 push 是增量的。
    """
    address = registry_address(node)
    if not address:
        raise NodeRegistryError(
            'registry_address_missing',
            f'节点 {getattr(node, "ip_address", "")} 既没有内网地址也没有连接地址，无法确定镜像仓库的位置。',
        )

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    config_file = work / 'registry.yml'
    unit_file = work / UNIT_NAME
    config_file.write_text(render_config(), encoding='utf-8')
    unit_file.write_text(render_unit(), encoding='utf-8')

    client = open_session(node, credential, key_path=key_path, client_factory=client_factory)
    try:
        if _service_active(client):
            context.log('system', f'镜像仓库已在 {address}:{REGISTRY_PORT} 上运行，跳过安装')
            return {'address': address, 'installed': False, 'port': REGISTRY_PORT}

        context.log('system', f'在节点 {address} 上安装镜像仓库')
        code, _out, err = run(client, f'mkdir -p {CONFIG_DIR} {DATA_DIR}')
        if code != 0:
            raise NodeRegistryError(
                'registry_dir_failed',
                f'在节点 {address} 上创建 {CONFIG_DIR} / {DATA_DIR} 失败：{err or "退出码 " + str(code)}。',
            )
        put_file(client, binary_path, BINARY_PATH, mode=0o755)
        put_file(client, config_file, CONFIG_PATH, mode=0o644)
        put_file(client, unit_file, UNIT_PATH, mode=0o644)

        _open_registry_port(context, client, address)

        code, _out, err = run(
            client,
            'systemctl daemon-reload && '
            f'systemctl enable --now {UNIT_NAME}',
        )
        if code != 0:
            raise NodeRegistryError(
                'registry_start_failed',
                f'在节点 {address} 上启动镜像仓库失败：{err or "退出码 " + str(code)}。'
                + _journal_hint(client),
            )
        if not _service_active(client):
            raise NodeRegistryError(
                'registry_not_active',
                f'节点 {address} 上的镜像仓库服务没有进入运行状态。' + _journal_hint(client),
            )
        context.log('system', f'镜像仓库已启动：{address}:{REGISTRY_PORT}（systemd 托管）')
        return {'address': address, 'installed': True, 'port': REGISTRY_PORT}
    finally:
        close_session(client)


def _journal_hint(client):
    """拼一句带 journal 尾巴的排查提示。

    参数：`client` 表示已连接的客户端。
    返回：返回提示文本，取不到日志时返回空串。
    副作用：在目标机上执行一条只读命令。

    registry 启动失败的原因（端口被占、SELinux 拒绝执行、配置文件写错）都只在 journal 里，
    而操作员此刻未必拿得到那台机器的登录方式 —— 把尾巴直接带进任务的失败原因里。
    """
    tail = _journal_tail(client)
    if not tail:
        return ''
    return f'\n节点上的服务日志（journalctl -u {UNIT_NAME}）：\n{tail}'


def _open_registry_port(context, client, address):
    """在节点上放行镜像仓库端口。

    参数：`context` 表示任务上下文；`client` 表示已连接的客户端；`address` 表示节点地址。
    返回：无显式返回值。
    副作用：可能修改目标机防火墙规则并重载。

    kubeasz 的 ``roles/prepare`` 只是 ``ignore_errors: true`` 卸载 firewalld 软件包，
    既不停服务也不加放行规则。卸载一旦静默失败，同一个集群里别的节点就被拦在门外，
    而现象是那些节点上 Pod 全部 ImagePullBackOff —— 从部署日志里看不出是防火墙。

    这里只处理防火墙，**云主机的安全组不归它管**：安全组在宿主机之外，节点上没有命令能改。
    所以放行之后还要留一句提示，否则「防火墙已放行、还是拉不到」会变成一个死胡同。
    """
    code, _out, _err = run(client, 'systemctl is-active --quiet firewalld')
    if code != 0:
        # firewalld 没在跑（kubeasz 的常规状态），入站不受它管，什么都不用做。
        return
    opened = run(client, f'firewall-cmd --add-port={REGISTRY_PORT}/tcp --permanent')[0] == 0
    reloaded = run(client, 'firewall-cmd --reload')[0] == 0 if opened else False
    if opened and reloaded:
        context.log('system', f'节点 {address} 的 firewalld 已放行 {REGISTRY_PORT}/tcp')
        return
    context.log(
        'system',
        f'注意：未能确认节点 {address} 已放行 {REGISTRY_PORT}/tcp。若别的节点拉不到镜像，'
        f'请在该节点上手工执行 firewall-cmd --add-port={REGISTRY_PORT}/tcp --permanent && '
        'firewall-cmd --reload；若用的是云主机，还要在安全组里放行同一端口（安全组在机器之外，'
        '平台改不了）。',
    )


# ---- 卸载 --------------------------------------------------------------

# 卸载命令末尾的回执标记：让调用方不必再开一次连接去核对文件是否真的没了。
UNINSTALL_OK = 'K8SMANAGER_REGISTRY_REMOVED'
UNINSTALL_PARTIAL = 'K8SMANAGER_REGISTRY_PARTIAL'


def uninstall_command():
    """生成卸载节点侧镜像仓库的命令文本。

    参数：无。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    每一步都容错并继续（``;`` 而不是 ``&&``）：这套命令要能在「服务从来没装过」「装到一半崩了」
    「服务在跑但 unit 文件被人删了」等所有状态下跑完 —— 它常在集群已经出问题之后执行，
    这时候再因为某个前置状态不符而半途退出，只会把机器留在更难收拾的位置。

    **数据目录刻意不删**：调用方通常是「清掉重装」，``DATA_DIR`` 里就是全部镜像层，
    留着能让下一次推送走「已存在」的快路径。要腾空间是另一件事，不该由清理流程替操作员决定。
    """
    return (
        f'systemctl stop {UNIT_NAME} 2>/dev/null; '
        f'systemctl disable {UNIT_NAME} 2>/dev/null; '
        f'rm -f {UNIT_PATH} {BINARY_PATH} {CONFIG_PATH}; '
        f'rmdir {CONFIG_DIR} 2>/dev/null; '
        'systemctl daemon-reload; '
        # 只删平台自己写的那一行：kubeasz 的 ``### BEGIN/END KUBEASZ MANAGED BLOCK`` 块归它自己管，
        # 平台在上面那串 sed 里连碰都不碰。
        f"sed -i '/{HOSTS_PATTERN}/d' /etc/hosts; "
        f'test ! -e {UNIT_PATH} -a ! -e {BINARY_PATH} && echo {UNINSTALL_OK} || echo {UNINSTALL_PARTIAL}'
    )


def uninstall(context, node, credential=None, key_path=None, client_factory=None):
    """把节点侧的镜像仓库从一台节点上卸掉。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`credential` 表示连接信息；
        `key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``{'removed': bool, 'data_dir': str}``；``removed`` 为假表示有文件没删掉。
    副作用：在目标节点上停服务、删文件并改动其 ``/etc/hosts``。

    幂等：服务与文件都不在时，命令照样跑完并返回成功（``removed`` 仍为真）。
    不删 ``DATA_DIR``（见 ``uninstall_command``）。连不上节点才会抛错，由调用方决定是否容忍 ——
    残留一个 registry 不影响重新部署，下次部署的 ``install`` 会把它带起来。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    client = open_session(node, credential, key_path=key_path, client_factory=client_factory)
    try:
        code, out, err = run(client, uninstall_command())
    finally:
        close_session(client)
    if code != 0:
        raise NodeRegistryError(
            'registry_uninstall_failed',
            f'卸载节点 {label} 上的镜像仓库失败：{err or out or "退出码 " + str(code)}。',
        )
    removed = UNINSTALL_OK in out
    if removed:
        context.log('system', f'节点 {label} 上的镜像仓库已卸载（镜像数据保留在 {DATA_DIR}）')
    else:
        context.log(
            'system',
            f'注意：节点 {label} 上的镜像仓库未能完全卸载（服务的单元或二进制还在）。'
            f'这不影响重新部署，但请手工复核 {UNIT_PATH}、{BINARY_PATH} 与 {CONFIG_PATH}。',
        )
    return {'removed': removed, 'data_dir': DATA_DIR}


# ---- 转发隧道 ----------------------------------------------------------


def open_tunnel(node, credential=None, key_path=None, relay_factory=None, port=TUNNEL_PORT):
    """把平台本机的一个回环端口接到节点上的 registry。

    参数：`node` 表示宿主节点快照；`credential` 表示连接信息；`key_path` 表示部署私钥路径；
        `relay_factory` 表示可注入的通道工厂，便于测试；`port` 表示平台侧的监听端口。
    返回：返回已启动的 ``platform_relay.Relay``。
    副作用：建立一条 SSH 连接，并在本机回环上开一个监听端口。

    方向是「平台 → 节点」的，因为只有平台能主动发起连接：节点反向连不上平台（这正是本模块
    存在的原因）。通道复用部署密钥，与 ansible 走的是同一把私钥。
    """
    target = {
        'address': LOOPBACK,
        'host': str(getattr(node, 'ip_address', '') or ''),
        'user': (credential or {}).get('user') or getattr(node, 'ssh_user', None) or 'root',
        'port': int((credential or {}).get('port') or getattr(node, 'ssh_port', None) or 22),
    }
    factory = relay_factory or platform_relay.Relay
    relay = factory(
        target,
        key_path=key_path or credentials.private_key_path(),
        port=port,
        destination_port=REGISTRY_PORT,
    )
    try:
        relay.start()
    except platform_relay.RelayError as exc:
        raise NodeRegistryError(
            'tunnel_failed',
            f'无法建立到 {target["host"]} 的镜像转发通道：{exc}'
            '平台需要经 SSH 把镜像推进节点上的仓库，请确认该节点 sshd 允许转发。',
        ) from exc
    return relay


def tunnel_ready(port=TUNNEL_PORT, attempts=READY_ATTEMPTS, interval=READY_INTERVAL, prober=None):
    """探测隧道另一端的 registry 是否已经能响应。

    参数：`port` 表示平台侧监听端口；`attempts` 表示重试次数；`interval` 表示重试间隔秒数；
        `prober` 表示可注入的探测函数，便于测试。
    返回：就绪返回 True，重试耗尽返回 False。
    副作用：发起若干次本机 HTTP 请求，可能睡眠。
    """
    check = prober or _http_ready
    for _ in range(max(1, int(attempts))):
        if check(port):
            return True
        time.sleep(interval)
    return False


def _http_ready(port):
    """请求 registry 的版本接口，判断它是否在应答。

    参数：`port` 表示平台侧监听端口。
    返回：应答返回 True，否则 False。
    副作用：发起一次本机 HTTP 请求。
    """
    try:
        with urllib.request.urlopen(f'http://{LOOPBACK}:{int(port)}/v2/', timeout=3) as response:
            return response.status in (200, 401)
    except (urllib.error.URLError, OSError):
        return False


def describe_tunnel_failure(node, port=TUNNEL_PORT):
    """拼一句隧道没通时的中文原因。

    参数：`node` 表示宿主节点快照；`port` 表示平台侧监听端口。
    返回：返回中文说明文本。
    副作用：只做纯计算。

    通道建起来、但对面一直不应答，只可能是三件事：节点的 sshd 不允许端口转发、
    节点上的服务没起来、或者方向被防火墙挡了。三者在日志里长得一模一样，一次说清。
    """
    host = str(getattr(node, 'ip_address', '') or '')
    return (
        f'平台已把 {LOOPBACK}:{int(port)} 接到 {host} 上的 {REGISTRY_PORT} 端口，'
        '但那边一直没有应答。请依次确认：该节点 sshd 允许端口转发（AllowTcpForwarding）、'
        f'节点上 systemctl status {UNIT_NAME} 是 active、'
        f'以及从平台到该节点 {REGISTRY_PORT} 端口没有被安全组拦下。'
    )


# ---- /etc/hosts 对齐 ---------------------------------------------------


def hosts_command(address):
    """生成改写节点 ``/etc/hosts`` 的命令文本。

    参数：`address` 表示镜像仓库在集群内网里的地址。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    先删后写，不判断是否已存在：kubeasz 写进去的那一行此刻**一定**在（``01.prepare`` 刚跑完），
    所以「删除 + 追加」与「替换」等价，而少一个分支就少一种在目标机上出错的可能。
    命令末尾把结果打出来，调用方据此核对，不必再开一次连接。
    """
    return (
        f"sed -i '/{HOSTS_PATTERN}/d' /etc/hosts; "
        f"printf '%s\\t%s\\n' '{address}' '{REGISTRY_HOSTNAME}' >> /etc/hosts; "
        f'grep -n {REGISTRY_HOSTNAME} /etc/hosts'
    )


def align_node(client, address, node_label):
    """把一个节点的 ``easzlab.io.local`` 指到镜像宿主节点。

    参数：`client` 表示已连接的客户端；`address` 表示内网地址；`node_label` 表示用于报错的节点标识。
    返回：返回改写后 ``/etc/hosts`` 里的那一行。
    副作用：修改目标机的 ``/etc/hosts``；失败时抛出 ``NodeRegistryError``。
    """
    code, out, err = run(client, hosts_command(address))
    if code != 0:
        raise NodeRegistryError(
            'hosts_align_failed',
            f'改写 {node_label} 的 /etc/hosts 失败：{err or out or "退出码 " + str(code)}。',
        )
    if address not in out:
        raise NodeRegistryError(
            'hosts_align_verify_failed',
            f'{node_label} 的 /etc/hosts 里 {REGISTRY_HOSTNAME} 没有指向 {address}，实际内容是：{out or "空"}。',
        )
    return out


def align_resolution(nodes, credentials_map, address, key_path=None, client_factory=None,
                     on_node=None):
    """把本次部署的所有节点都指向镜像宿主节点。

    参数：`nodes` 表示节点快照列表；`credentials_map` 表示连接信息；`address` 表示仓库内网地址；
        `key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂；`on_node` 表示每台
        节点处理完成后的回调 ``f(node, line)``，便于把结果写进任务日志。
    返回：返回 ``{节点 IP: /etc/hosts 里的行}``。
    副作用：逐台建立 SSH 连接并修改其 ``/etc/hosts``。

    必须在 ``01.prepare`` 之后调用：那个 role 会把这一行整行替换成「节点看到的 SSH 源地址」，
    提前写进去的留不下来。调用方负责挑时机，本函数只负责把结果改对、并当场核对。

    连不上任何一台节点都直接失败：少改一台，那台机器上所有 Pod 就会以完全相同的方式卡住，
    而失败发生在部署中段，比在这里停下难查得多。
    """
    results = {}
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        client = open_session(node, credential, key_path=key_path, client_factory=client_factory)
        try:
            line = align_node(client, address, ip or getattr(node, 'name', '节点'))
        finally:
            close_session(client)
        results[ip] = line
        if on_node is not None:
            on_node(node, line)
    return results
