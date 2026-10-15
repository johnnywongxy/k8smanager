"""把每台集群节点的主机名改成它的**节点名**，并且改一次就永久生效。

节点名（``compiler.node_name_map`` 算出来的那个，也是 inventory 里 ``k8s_nodename`` 的值）
是 kubelet 注册到集群的名字；主机名是操作系统自己的名字。两者不一致时，操作员登上一台机器
``ssh`` 进去看到的是一回事、``kubectl get node`` 看到的是另一回事 —— 排查故障时这是最容易
走错路的岔口（同一个 IP 在两处有两个名字）。

kubeasz 只写 ``k8s_nodename``，从不碰系统主机名：``hostname-*`` 这类 playbook 在它的
prepare 角色里是有开关的（``ENABLE_SETTING_HOSTNAME``），默认不开。平台因此自己来：

- **持久**：写 ``/etc/hostname``（重启后由 init 读它），这是用户明确要求的那一条
  —— 「不要临时修改，机器重启以后 hostname 也不能变」；
- **立即**：同时调一次 ``hostname``（内核里的值），让 kubelet 这次就按正确的名字启动，
  不必等重启；
- **可解析**：``/etc/hosts`` 里没有这个名字时补一行 ``127.0.0.1 <节点名>``。calico 与
  一些组件要能解析自己的名字，而多数机器的 ``/etc/hosts`` 只认一个旧名字。只**追加**，
  不改既有行、不删旧名 —— 别人的监控或证书可能正引用着旧名字。

**幂等**：名字已经对就不写文件，只核对 ``/etc/hosts``；重复执行不会重复追加。

**失败即中止**（``strict=True``）：这不是 PATH 那种「少一项也能用」的便利配置 ——
名字不一致正是要修掉的那个毛病，静默放过等于白做。此刻集群还没开始安装，中止的代价为零。
因此调用方 ``state_machine`` 把它接成 ``TaskAborted``，消息里带可照抄的手工命令。
"""

from . import compiler, credentials, node_path, node_registry

# 系统主机名的两处落点：``/etc/hostname`` 是重启后生效的那一份（持久），
# ``/proc/sys/kernel/hostname`` 是当前内核里的值（``hostname`` 命令读写的就是它）。
HOSTNAME_FILE = '/etc/hostname'
KERNEL_HOSTNAME_PATH = '/proc/sys/kernel/hostname'
HOSTS_FILE = '/etc/hosts'
# 补 ``/etc/hosts`` 时用的地址：回环即可，名字能不能解析只取决于有没有这一行。
HOSTS_ADDRESS = '127.0.0.1'


class NodeHostnameError(Exception):
    """节点主机名没改成，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    与 ``NodePathError`` 同理：``state_machine`` 属于上层，本模块不直接抛它的 ``TaskAborted``，
    由调用方转换。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def manual_command(name):
    """给出一段可以照抄到节点上的手工命令。

    参数：`name` 表示节点名。
    返回：返回一段 shell 命令文本。
    副作用：只做纯计算，不修改任何机器。
    """
    quoted = _single_quote(name)
    return (
        f'hostname {quoted} && printf \'{name}\\n\' > {HOSTNAME_FILE} && '
        f'grep -qw {quoted} {HOSTS_FILE} || echo "{HOSTS_ADDRESS} {name}" >> {HOSTS_FILE}'
    )


def _single_quote(text):
    """把一段文本包成 shell 单引号字面量。

    参数：`text` 表示要包裹的文本。
    返回：返回可直接放进命令的引号文本。
    副作用：只做纯计算。
    """
    return "'" + str(text).replace("'", "'\\''") + "'"


# ---- 命令文本 ----------------------------------------------------------


def probe_command():
    """生成「读回这台机器当前的主机名」的命令。

    参数：无。
    返回：返回一段 shell 命令，输出两行带前缀的值。
    副作用：只做纯计算。

    两个值分开看：``KERNEL`` 是内核里的（``hostname`` 命令报的就是它，kubelet 起来时读的
    也是它），``FILE`` 是 ``/etc/hostname`` 的内容（重启后会变成哪个名字）。两者都取自文件
    系统，不依赖 ``hostname`` 命令是否存在。
    """
    return (
        f'echo "KERNEL $(cat {KERNEL_HOSTNAME_PATH} 2>/dev/null)"; '
        f'echo "FILE $(cat {HOSTNAME_FILE} 2>/dev/null)"'
    )


def parse_probe(text):
    """解析探测命令的输出。

    参数：`text` 表示命令输出。
    返回：返回 ``{'kernel': 内核主机名, 'file': /etc/hostname 的内容}``，缺失的键为空串。
    副作用：只做纯计算。
    """
    result = {'kernel': '', 'file': ''}
    for line in str(text or '').splitlines():
        keyword, _, rest = line.strip().partition(' ')
        if keyword == 'KERNEL':
            result['kernel'] = rest.strip()
        elif keyword == 'FILE':
            result['file'] = rest.strip()
    return result


def set_name_command(name):
    """生成「把主机名改成 ``name``」的命令。

    参数：`name` 表示节点名。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    三步缺一不可：

    1. ``hostname``：改内核里的值，本次就生效（hostnamectl 之后仍要执行它 —— 容器或
       dbus 没起来的机器上 ``hostnamectl`` 会失败，那时只有这一步是真的生效了）；
    2. ``hostnamectl``（有才跑）：让 systemd 自己的记录跟着一致，避免 ``hostnamectl``
       或日志里读到旧名字；
    3. 原子写 ``/etc/hostname``：重启后生效的那一份。**这一份必须由平台自己保证** ——
       hostnamectl 在部分发行版上不写这个文件。

    写文件走 ``node_path.write_text_command``：base64 搬运 + 临时文件 + ``mv`` 原子替换 +
    末尾 ``cat`` 回读，理由见那个函数。
    """
    quoted = _single_quote(name)
    write_file = node_path.write_text_command(HOSTNAME_FILE, f'{name}\n', '644')
    return (
        f'hostname {quoted}; '
        'if command -v hostnamectl >/dev/null 2>&1; then '
        f'hostnamectl set-hostname {quoted} >/dev/null 2>&1 || true; fi; '
        f'{write_file}'
    )


def hosts_lookup_command(name):
    """生成「检查 ``/etc/hosts`` 里有没有这个名字」的命令。

    参数：`name` 表示节点名。
    返回：返回一段 shell 命令，命中输出 ``PRESENT``、否则 ``ABSENT``。
    副作用：只做纯计算。

    用整词匹配：名字里的 ``.`` 会被转义，避免 ``master.01`` 命中 ``masterX01``。注释行
    （``#`` 之后的内容）不参与判断 —— 生产机上 ``/etc/hosts`` 的注释里出现某个名字，
    不代表它能被解析。
    """
    pattern = str(name).replace('.', '\\.')
    return (
        f"sed 's/#.*//' {HOSTS_FILE} 2>/dev/null"
        f" | grep -Eq '(^|[[:space:]]){pattern}([[:space:]]|$)'"
        " && echo PRESENT || echo ABSENT"
    )


def hosts_append_command(name):
    """生成「把节点名追加进 ``/etc/hosts``」的命令。

    参数：`name` 表示节点名。
    返回：返回一段 shell 命令，核对成功输出 ``PRESENT``、否则 ``ABSENT``。
    副作用：只做纯计算。

    追加一整行而不是去改既有那一行：现成的 ``/etc/hosts`` 各家写法不一（有的把主机名写在
    ``127.0.0.1`` 那一行的中间），改它等于替操作员决定他要什么；多一行 ``127.0.0.1 <名字>``
    对解析没有任何副作用。

    **末尾那一句回执不能省。** 核对追加结果最自然的写法是 ``grep -qw <名字> /etc/hosts``，
    但 ``-q`` 正是「不输出、只看退出码」的意思 —— 那条命令即使成功了也没有任何输出。调用方
    拿到的是空字符串，于是「已经写进去了」与「命令根本没跑」在输出上长得一模一样；再按
    「输出里有 PRESENT 才算成功」去判，就会把每一次成功都读成失败（现场三条节点都报
    「写不进 /etc/hosts … 退出码 0」—— 「退出码 0」本身就是成功，是这里读错了）。

    所以显式 ``echo PRESENT / echo ABSENT``：成功有据可查。同时把「写不进去」（``printf``
    的非零退出码）留在整条命令的退出码上，用 ``&&`` 短路而不是 ``|| echo``，免得失败被回执
    自己吃掉。名字里的 ``.`` 与查询命令一样转义，否则 ``master.01`` 会命中 ``masterX01``。
    """
    pattern = _single_quote(str(name).replace('.', '\\.'))
    return (
        f'printf "{HOSTS_ADDRESS} {name}\\n" >> {HOSTS_FILE}'
        f' && (grep -qw {pattern} {HOSTS_FILE} && echo PRESENT || echo ABSENT)'
    )


# ---- 节点侧执行 --------------------------------------------------------


def _read_hostname(client):
    """读回一台节点当前的主机名。

    参数：`client` 表示已连接的客户端。
    返回：返回 ``(探测结果, 错误说明)``，探测失败时结果为空字典。
    副作用：在目标机上执行一条只读命令。
    """
    code, out, err = node_registry.run(client, probe_command())
    if code != 0:
        return {}, (err or out or f'退出码 {code}').strip()
    return parse_probe(out), ''


def apply_node(context, node, name, credential=None, key_path=None, client_factory=None):
    """把一台节点的主机名改成它的节点名。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`name` 表示节点名；
        `credential` 表示连接信息；`key_path` 表示部署私钥路径；
        `client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``{'label', 'previous', 'name', 'changed', 'hosts' }`` 报告。
    副作用：在目标机上改主机名、写 ``/etc/hostname``，必要时往 ``/etc/hosts`` 追加一行。

    幂等：名字已经一致时不写文件，只核对 ``/etc/hosts``。任何一步不符预期（改完回读不一致、
    或者写不进 ``/etc/hosts``）都抛 ``NodeHostnameError``。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    target = str(name or '').strip()
    if not compiler._validate_node_name(target):
        raise NodeHostnameError(
            'node_hostname_invalid',
            f'节点 {label} 的节点名「{target}」不合法：只能包含小写字母、数字、- 和 .，'
            '且必须以字母数字开头和结尾。',
        )

    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        report = _apply_over_client(context, client, label, target)
    finally:
        node_registry.close_session(client)
    return report


def _apply_over_client(context, client, label, target):
    """在已连接的会话上完成改名，返回报告字典。

    参数：`context` 表示任务上下文；`client` 表示已连接的客户端；`label` 表示节点标识；
        `target` 表示目标主机名。
    返回：返回 ``{'label', 'previous', 'name', 'changed', 'hosts'}``。
    副作用：在目标机上改主机名、写 ``/etc/hostname``，必要时改 ``/etc/hosts``。
    """
    before, error = _read_hostname(client)
    if error:
        raise NodeHostnameError('node_hostname_failed', f'读不到节点 {label} 当前的主机名：{error}')
    current = before.get('kernel') or before.get('file')
    changed = before.get('kernel') != target or before.get('file') != target
    if changed:
        code, out, err = node_registry.run(client, set_name_command(target))
        if code != 0:
            raise NodeHostnameError(
                'node_hostname_failed',
                f'把节点 {label} 的主机名改成 {target} 失败：{(err or out or "").strip() or f"退出码 {code}"}。'
                f'可手工执行：{manual_command(target)}',
            )
        context.log('system', f'节点 {label} 的主机名：{current or "（未知）"} → {target}')

    # 回读而不是相信写入命令的退出码：``/etc/hostname`` 被写成什么内容，只有读回来才知道。
    after, error = _read_hostname(client)
    if error:
        raise NodeHostnameError('node_hostname_failed', f'改完之后读不回节点 {label} 的主机名：{error}')
    if after.get('kernel') != target or after.get('file') != target:
        raise NodeHostnameError(
            'node_hostname_failed',
            f'节点 {label} 的主机名与节点名不一致（内核 {after.get("kernel") or "（空）"}、'
            f'{HOSTNAME_FILE} {after.get("file") or "（空）"}，期望 {target}）。'
            f'可手工执行：{manual_command(target)}',
        )

    hosts = _ensure_hosts_entry(context, client, label, target)
    return {'label': label, 'previous': current, 'name': target, 'changed': changed, 'hosts': hosts}


def _ensure_hosts_entry(context, client, label, target):
    """保证 ``/etc/hosts`` 里能解析这个节点名，返回 ``'present'`` 或 ``'appended'``。

    参数：`context` 表示任务上下文；`client` 表示已连接的客户端；`label` 表示节点标识；
        `target` 表示节点名。
    返回：返回处理结果；写不进去时抛 ``NodeHostnameError``。
    副作用：必要时往目标机的 ``/etc/hosts`` 追加一行。
    """
    code, out, _err = node_registry.run(client, hosts_lookup_command(target))
    if code == 0 and 'PRESENT' in out:
        return 'present'
    code, out, err = node_registry.run(client, hosts_append_command(target))
    if code != 0:
        raise NodeHostnameError(
            'node_hostname_failed',
            f'节点 {label} 的 {HOSTS_FILE} 里写不进 {target} 这一行：'
            f'{(err or out or "").strip() or f"退出码 {code}"}。'
            f'可手工执行：echo "{HOSTS_ADDRESS} {target}" >> {HOSTS_FILE}',
        )
    if 'PRESENT' not in out:
        # 命令报成功却没打回执、或回执是 ABSENT：两种都说明「追加了这一行但核对不到」，
        # 与「命令失败」是两件事，分开说 —— 原因不同，操作员在机器上查的东西也不同。
        raise NodeHostnameError(
            'node_hostname_failed',
            f'节点 {label} 的 {HOSTS_FILE} 追加 {target} 的命令成功了（退出码 0），'
            f'但随后核对不到这个名字（命令输出：{out.strip() or "空"}）。'
            f'请在该节点上手工复核：grep -n {target} {HOSTS_FILE}',
        )
    context.log('system', f'节点 {label}：{HOSTS_FILE} 里补了一行 {HOSTS_ADDRESS} {target}')
    return 'appended'


def verify_node(node, credential=None, key_path=None, client_factory=None):
    """只读复核一台节点当前的主机名。

    参数：`node` 表示节点快照；`credential` 表示连接信息；`key_path` 表示部署私钥路径；
        `client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``(内核主机名, 错误说明)``；读不到时主机名为空串、错误说明为中文原因。
    副作用：建立一次 SSH 连接并执行一条只读命令。

    给「kubeasz 的 01.prepare 跑完之后」那次复核用：上游的 prepare 角色带主机名相关的开关，
    万一它按自己的理解改写了一遍，这里要能发现。只读，不改任何东西。
    """
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        result, error = _read_hostname(client)
    finally:
        node_registry.close_session(client)
    if error:
        return '', error
    return result.get('kernel') or result.get('file') or '', ''


def set_hostname(context, nodes, names, credentials_map, strict=True):
    """逐台把节点主机名改成节点名。

    参数：`context` 表示任务上下文；`nodes` 表示节点快照列表；`names` 表示
        ``{ip_address: 节点名}``；`credentials_map` 表示 ``{ip: {'user','port',...}}``；
        `strict` 表示失败时是否直接中止。
    返回：返回 ``(已设置列表, 失败列表)``，失败项形如 ``{'label','name','reason'}``。
    副作用：在目标机上改主机名、写 ``/etc/hostname``，必要时改 ``/etc/hosts``。

    ``names`` 里没有的机器（只有 etcd 或负载均衡角色的机器）会被跳过：它们在集群里没有
    节点名，改它们的系统主机名没有意义。

    ``strict=True`` 时先把每台都试一遍再抛错：一台机器改不动就停下的话，操作员会一台一台地
    发现同样的问题；一次把全部失败列出来，他能一次改完再重试整条任务。
    """
    applied, failed = [], []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        target = str((names or {}).get(ip) or '')
        if not target:
            continue
        credential = (credentials_map or {}).get(ip) or {}
        try:
            report = apply_node(context, node, target, credential)
        except (NodeHostnameError, node_registry.NodeRegistryError, credentials.CredentialError) as exc:
            reason = getattr(exc, 'message', None) or str(exc)
            failed.append({'label': ip, 'name': target, 'reason': reason})
            context.log('system', f'节点 {ip} 的主机名未设置成功：{reason}')
        else:
            applied.append(report)

    if failed and strict:
        detail = '；'.join(f'{item["label"]}（{item["name"]}）：{item["reason"]}' for item in failed)
        raise NodeHostnameError(
            'node_hostname_failed',
            f'有 {len(failed)} 台节点的主机名没能改成它们的节点名 —— {detail}',
        )
    return applied, failed
