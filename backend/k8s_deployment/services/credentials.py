"""部署期 SSH 凭据：平台部署密钥的生成、下发与清理。

平台给目标机装集群时，ansible 需要免密登录每一台节点。kubeasz 官方也是这个要求。
平台不把明文密码写进 inventory（那会让密码出现在工作区文件、诊断包和进程参数里），
而是走标准做法：用机器上已托管的密码登录一次，把**平台部署公钥**追加到目标机的
``~/.ssh/authorized_keys``，之后的预检与安装都用密钥认证。

部署私钥落在 ``<状态根>/ssh/`` 下并收紧到 0600，全平台共用一份：同一台机器重复部署时
公钥已在位，下发是幂等的。凭据只在**内存**中作为连接参数出现，不落盘、不进日志。

**为什么是 ed25519 而不是 RSA。** kubeasz 的 ``ezctl`` 是 bash 脚本，它调起的是**平台机上
的** ``ssh`` 客户端，这个客户端有多老由平台机决定，平台管不着。规划里平台机是 CentOS 7，
自带 OpenSSH 6.6.1（2013 年），RSA 密钥**只会签 ``ssh-rsa``(SHA-1)** —— rsa-sha2-* 要
OpenSSH 7.2 才有。而目标节点普遍是 Ubuntu 22.04 / sshd 8.9，默认 ``PubkeyAcceptedAlgorithms``
里**没有** ``ssh-rsa``。一头只会发 SHA-1，一头拒收 SHA-1，于是 ansible 连不上节点，
``01.prepare`` 直接 UNREACHABLE。ed25519 两端都支持（OpenSSH 6.5 起），一次选定就绕开了
这条死结；换成在每台目标机上放开 ``ssh-rsa`` 等于把平台的不兼容转嫁出去，不可取。

**为什么 ``verify_key_login`` 用系统 ssh 而不是 paramiko。** 判定必须与执行走同一条路径。
paramiko 会签 ``rsa-sha2-512``，所以 RSA 时代它能连上现代 sshd，而 ansible 用系统 ssh 立刻
被拒 —— ``credentials`` 阶段报「免密登录可用」，几秒后 ``01.prepare`` 报 Permission denied，
两边自相矛盾。凭据判定只要和真正干活的客户端不是同一个，结论就没有意义。
"""

import hashlib
import os
import stat
import subprocess
from pathlib import Path

from django.conf import settings

from . import paths

# 部署密钥对的文件名与前缀，落在 ``<状态根>/ssh/`` 下。
# 文件名带算法：老机器上留着的 deploy_rsa 是历史遗留，不能被误当成当前部署密钥。
SSH_DIR_NAME = 'ssh'
PRIVATE_KEY_NAME = 'deploy_ed25519'
# 验证免密登录时调用的客户端。必须是系统 ssh —— ansible 用的就是它，理由见模块开头。
SSH_CLIENT = 'ssh'
# 一次 ssh 验证在连接超时之上再留多少秒，覆盖握手与认证的耗时。
SSH_VERIFY_GRACE_SECONDS = 10
# 连接、握手与认证的超时秒数。
CONNECT_TIMEOUT = 15
# 认证被服务端拒绝时对外统一的提示语。
LOGIN_REJECTED = 'SSH 登录被拒绝：用户名、密码或密钥不正确。'
# 老 sshd 的回退开关。CentOS 7 自带的 OpenSSH 6.6.1（2015 年）不认 RFC 8332 的 rsa-sha2-*
# 签名算法，也不发 server-sig-algs 扩展；paramiko 收不到该扩展时不做探测，直接取自己列表里
# 的第一个 RSA 算法（rsa-sha2-512），于是被对方以 unsupported public key algorithm 拒掉。
# 子键 ``'pubkeys'`` 对应 paramiko 的 ``_preferred_pubkeys``（即 PubKeyAcceptedAlgorithms），
# 是公钥**认证签名**用的那张表，不是主机密钥那张 ``'keys'`` 表。禁用之后协商落到 ssh-rsa。
# 只在首次认证被拒后才启用：现代 sshd 普遍默认关掉了 ssh-rsa，一上来就禁用反而会连不上。
#
# 部署密钥改用 ed25519 之后（见模块开头），paramiko 这边已经不会再撞上这个问题；留着它是给
# 「运维手工把部署私钥换成 RSA」这种情形兜底，那时这把开关仍然是唯一能救回来的东西。
LEGACY_RSA_ALGORITHMS = {'pubkeys': ['rsa-sha2-256', 'rsa-sha2-512']}
# 追加公钥时使用的标记，便于识别哪些 key 是平台自己装的。
KEY_MARKER = 'k8smanager-deploy'
# authorized_keys 的路径与权限。
AUTHORIZED_KEYS = '.ssh/authorized_keys'
TARGET_FILE_MODE = 0o600
TARGET_DIR_MODE = 0o700


class CredentialError(Exception):
    """凭据准备失败，携带可直接展示给用户的中文原因。"""


class _AuthRejected(CredentialError):
    """认证被服务端拒绝；调用方可以换一种签名算法重试。"""


def ssh_dir():
    """返回存放部署密钥对的目录。

    参数：无。
    返回：返回 ``<状态根>/ssh`` 的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return paths.state_root() / SSH_DIR_NAME


def private_key_path():
    """返回部署私钥的文件路径。

    参数：无。
    返回：返回 ``<状态根>/ssh/deploy_ed25519`` 的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return ssh_dir() / PRIVATE_KEY_NAME


def public_key_path():
    """返回部署公钥的文件路径。

    参数：无。
    返回：返回 ``<状态根>/ssh/deploy_ed25519.pub`` 的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return private_key_path().with_suffix('.pub')


def ensure_keypair():
    """确保平台部署密钥存在，返回私钥路径与公钥文本。

    参数：无。
    返回：返回 ``(私钥 Path, 公钥文本)``。
    副作用：首次调用时生成 ed25519 密钥对并写入 ``<状态根>/ssh/``，私钥权限收紧到 0600。

    paramiko 只有 ``Ed25519Key.from_private_key*``，没有生成器（3.5.1 与 4.0.0 都没有），
    所以这里用 paramiko 自己的依赖 ``cryptography`` 生成，并直接序列化成 OpenSSH 私钥格式 ——
    也就是系统 ``ssh`` 唯一会读的那种格式。PEM/PKCS8 写的 ed25519 私钥 ``ssh`` 不认。
    """
    target = private_key_path()
    if target.exists() and public_key_path().exists():
        return target, public_key_path().read_text(encoding='utf-8').strip()

    paths.ensure_dir(ssh_dir())
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        private = ed25519.Ed25519PrivateKey.generate()
        payload = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        material = private.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode('utf-8')
    except Exception as exc:  # pragma: no cover - 依赖库异常时给出可读原因
        raise CredentialError(f'生成平台部署密钥失败：{exc}') from exc

    # 先按 0600 建文件再写内容，避免私钥在默认权限下存在哪怕一瞬间。
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TARGET_FILE_MODE)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(payload)
    paths.secure_file(target)

    public = f'{material} {KEY_MARKER}@{_host_tag()}'
    public_key_path().write_text(public + '\n', encoding='utf-8')
    return target, public


def _host_tag():
    """返回一个稳定的平台标识，用作公钥注释。

    参数：无。
    返回：返回短标识字符串。
    副作用：只做纯计算，不修改持久化数据。
    """
    raw = getattr(settings, 'K8S_DEPLOY_STATE_ROOT', 'k8smanager')
    return hashlib.sha256(str(raw).encode('utf-8')).hexdigest()[:8]


def key_fingerprint(text):
    """计算公钥的 SHA256 指纹，用于在界面上核对。

    参数：`text` 表示公钥文本。
    返回：返回 OpenSSH 风格指纹，无法解析时返回空串。
    副作用：只做纯计算，不修改持久化数据。

    OpenSSH 的 ``SHA256:`` 指纹就是「公钥 blob 的 SHA-256」，blob 正是 ``.pub`` 里第二段
    base64 解码后的内容。所以这里按算法无关的方式直接算，不必先解析成具体密钥对象 ——
    换密钥类型时少一处会跟丢的分支。
    """
    import base64

    parts = str(text or '').split()
    if len(parts) < 2:
        return ''
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        return ''
    if not blob:
        return ''
    digest = hashlib.sha256(blob).digest()
    return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')


def _new_client():
    """创建一个已设置主机密钥策略的 SSH 客户端。

    参数：无。
    返回：返回尚未连接的 SSHClient。
    副作用：导入 paramiko；只创建本机对象，不发起网络连接。
    """
    import paramiko

    from ops.services.system_tools import ssh_host_key_policy

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(ssh_host_key_policy())
    return client


def _transport_up(client):
    """判断 SSH 传输层是否已经建立，也就是密钥交换成功、只差认证这一步。

    参数：`client` 表示刚建连失败的 SSHClient。
    返回：返回 True 或 False。
    副作用：只读取本机对象状态，不发网络请求。
    """
    transport = client.get_transport()
    return transport is not None and transport.is_active()


def _connect(client, kwargs):
    """按给定参数建立连接，并把 paramiko 的异常翻译成中文原因。

    参数：`client` 表示待连接的 SSHClient；`kwargs` 表示传给 ``connect`` 的参数。
    返回：无返回值；调用正常结束即表示连接已建立。
    副作用：打开网络连接；不写入本地文件，也不把密码写入任何日志。
    """
    import paramiko

    try:
        client.connect(**kwargs)
    except paramiko.AuthenticationException as exc:
        raise _AuthRejected(LOGIN_REJECTED) from exc
    except paramiko.BadHostKeyException as exc:
        raise CredentialError('目标机的主机密钥与预期不一致，已中止连接。') from exc
    except paramiko.SSHException as exc:
        # 传输层已经起来说明协商是通的、卡住的是认证，归到可回退的那一类。
        # paramiko 的 ``SSHClient._auth`` 在用 key_filename 试完所有私钥类之后，抛出的往往是
        # 最后一轮的副产品异常（真机 CentOS 7 上见到的是 ``encountered RSA key, expected
        # OPENSSH key``），而不是 ``AuthenticationException``；只看异常类型会让回退永不触发。
        if _transport_up(client):
            raise _AuthRejected(LOGIN_REJECTED) from exc
        raise CredentialError('SSH 协议协商失败：该端口可能不是 SSH 服务。') from exc
    except OSError as exc:
        raise CredentialError(
            f'无法连接到 {kwargs["hostname"]}:{kwargs["port"]}（{exc}）。'
        ) from exc


def ssh_client(host, port, username, password=None, key_path=None, timeout=CONNECT_TIMEOUT):
    """建立一条到目标节点的 SSH 连接。

    参数：`host` 表示目标地址；`port` 表示 SSH 端口；`username` 表示登录用户名；
        `password` 表示登录密码，仅在首次下发公钥时使用；`key_path` 表示平台部署私钥路径；
        `timeout` 表示连接、握手与认证的超时秒数。
    返回：返回已连接的 paramiko.SSHClient。
    副作用：建立网络连接；不写入本地文件，也不把密码写入任何日志。

    密钥认证首轮被拒时会禁用 rsa-sha2-* 再试一次，以兼容不认识该算法的老 sshd
    （见 ``LEGACY_RSA_ALGORITHMS``）；密码认证不涉及签名协商，只尝试一次。

    这条通道只服务两件事：**用密码下发公钥**，以及备份/恢复时自己搬文件。它不参与
    「密钥能不能登录」的判定 —— 那个必须走系统 ``ssh``（见 ``verify_key_login``），
    否则 paramiko 的算法协商会把 ansible 走不通的链路报成通的。
    """
    kwargs = {
        'hostname': str(host),
        'port': int(port),
        'username': str(username),
        'timeout': timeout,
        'banner_timeout': timeout,
        'auth_timeout': timeout,
    }
    using_key = not password and bool(key_path)
    if password:
        # 有密码时不去翻本地密钥或 ssh-agent，避免连到错误的身份。
        kwargs.update(password=password, allow_agent=False, look_for_keys=False)
    else:
        kwargs.update(key_filename=str(key_path), allow_agent=False, look_for_keys=False)

    client = _new_client()
    try:
        _connect(client, kwargs)
        return client
    except _AuthRejected as exc:
        client.close()
        if not using_key:
            raise CredentialError(LOGIN_REJECTED) from exc
        client = _new_client()
        try:
            _connect(client, dict(kwargs, disabled_algorithms=LEGACY_RSA_ALGORITHMS))
        except CredentialError:
            # 回退也失败时报告首轮的结论：多数情况下那才是真实原因。
            client.close()
            raise CredentialError(LOGIN_REJECTED) from exc
        return client
    except CredentialError:
        client.close()
        raise


def run_command(client, command, timeout=CONNECT_TIMEOUT):
    """在已建立的 SSH 连接上执行一条命令。

    参数：`client` 表示已连接的 SSH 客户端；`command` 表示要执行的命令；`timeout` 表示读取超时秒数。
    返回：返回 ``(退出码, 标准输出, 标准错误)``，输出为去除首尾空白的文本。
    副作用：在目标机上执行命令；不修改本地数据。
    """
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    return code, out, err


def install_public_key(host, port, username, password, public_key=None, key_path=None):
    """把平台部署公钥装到目标机，使后续免密登录可用。

    参数：`host` 表示目标地址；`port` 表示 SSH 端口；`username` 表示登录用户名；
        `password` 表示登录密码；`public_key` 表示要安装的公钥文本，传 None 时自动确保密钥对；
        `key_path` 表示平台部署私钥路径。
    返回：返回 ``{'installed': bool, 'already': bool, 'fingerprint': str}``。
    副作用：在目标机上创建 ``~/.ssh`` 并追加公钥到 ``authorized_keys``；幂等，已存在时不重复写入。
    """
    private_key, public = ensure_keypair()
    remote_key = str(public_key or public).strip()
    client = ssh_client(host, port, username, password=password)
    try:
        home = _remote_home(client)
        target = f'{home}/{AUTHORIZED_KEYS}'
        # 按公钥本体（第二段 base64）比对，注释不同不算重复。
        body = remote_key.split()[1] if len(remote_key.split()) > 1 else remote_key
        code, out, _err = run_command(
            client,
            f"mkdir -p '{home}/.ssh' && chmod {TARGET_DIR_MODE:o} '{home}/.ssh' && "
            f"[ -f '{target}' ] && chmod {TARGET_FILE_MODE:o} '{target}'; "
            f"grep -qF '{body}' '{target}' 2>/dev/null && echo PRESENT || echo ABSENT",
        )
        if code != 0:
            raise CredentialError(f'检查目标机 {host} 的 authorized_keys 失败。')
        if out.strip().endswith('PRESENT'):
            return {'installed': True, 'already': True, 'fingerprint': key_fingerprint(remote_key)}
        code, _out, err = run_command(
            client,
            f"printf '%s\\n' '{remote_key}' >> '{target}' && chmod {TARGET_FILE_MODE:o} '{target}'",
        )
        if code != 0:
            raise CredentialError(f'向目标机 {host} 写入公钥失败：{err[:120]}')
    finally:
        client.close()
    return {'installed': True, 'already': False, 'fingerprint': key_fingerprint(remote_key)}


def _remote_home(client):
    """读取远端用户的 home 目录。

    参数：`client` 表示已连接的 SSH 客户端。
    返回：返回 home 目录路径，取不到时按用户名兜底。
    副作用：在目标机上执行一条只读命令。
    """
    code, out, _err = run_command(client, 'printf %s "$HOME"')
    home = out.strip()
    if code == 0 and home.startswith('/'):
        return home
    return f'/root'


def verify_key_login(host, port, username, key_path=None, timeout=CONNECT_TIMEOUT):
    """用平台部署密钥验证免密登录是否已经可用。

    参数：`host` 表示目标地址；`port` 表示 SSH 端口；`username` 表示登录用户名；
        `key_path` 表示平台部署私钥路径；`timeout` 表示超时秒数。
    返回：返回 True 或 False；认证没通过或连不上都算 False。
    副作用：起一个本机 ``ssh`` 短连接，不修改目标机与本地数据。

    这里刻意**不用 paramiko**，而是调系统 ``ssh``：kubeasz 的 ``ezctl`` 调起的正是它。
    两者能接受的算法不一样（paramiko 会签 rsa-sha2-512，OpenSSH 6.6.1 只会签 ssh-rsa），
    用 paramiko 判定会得出「可用」，而 ansible 紧接着被同一个 sshd 拒掉。理由详见模块开头。

    只有「本机压根没有 ssh 客户端」才抛 ``CredentialError``：那种情况下 ansible 也跑不了，
    必须让用户先把这个装好，而不是误报成「密钥没装」再白跑一遍下发流程。
    """
    private_key = Path(key_path) if key_path else private_key_path()
    if not private_key.exists():
        return False
    connect_timeout = max(int(timeout), 1)
    argv = [
        SSH_CLIENT,
        '-i', str(private_key),
        # 不弹任何交互提示，也不接受密码兜底：问的是「密钥能不能过」。
        '-o', 'BatchMode=yes',
        '-o', 'PreferredAuthentications=publickey',
        '-o', 'PasswordAuthentication=no',
        '-o', 'StrictHostKeyChecking=no',
        '-o', f'ConnectTimeout={connect_timeout}',
        '-p', str(int(port)),
        f'{username}@{host}',
        'true',
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=connect_timeout + SSH_VERIFY_GRACE_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CredentialError(
            f'本机没有 {SSH_CLIENT} 客户端。kubeasz 的 ezctl 会用系统的 {SSH_CLIENT} 连接目标节点，'
            '请先在平台机上装好 OpenSSH 客户端。'
        ) from exc
    except subprocess.SubprocessError as exc:
        raise CredentialError(f'调用本机 {SSH_CLIENT} 验证免密登录失败：{exc}') from exc
    return completed.returncode == 0


def ensure_key_login(node, credentials, key_path=None):
    """确保能免密登录到某台节点，必要时先下发公钥。

    参数：`node` 表示节点快照；`credentials` 表示该节点的连接信息（含可选密码）；
        `key_path` 表示平台部署私钥路径。
    返回：返回 ``{'ok': bool, 'message': str, 'installed': bool}``。
    副作用：可能建立 SSH 连接并向目标机写入公钥。
    """
    host = str(node.ip_address)
    user = credentials.get('user') or node.ssh_user or 'root'
    port = int(credentials.get('port') or node.ssh_port or 22)

    if verify_key_login(host, port, user, key_path=key_path):
        return {'ok': True, 'message': '部署密钥免密登录可用。', 'installed': False}

    password = credentials.get('password') or ''
    if not password:
        return {
            'ok': False,
            'message': '部署密钥尚未安装，且该机器没有托管 SSH 密码，无法自动下发公钥。'
                       '请在机器上预置平台部署公钥，或在机器管理里补充登录密码。',
            'installed': False,
        }
    try:
        result = install_public_key(host, port, user, password, key_path=key_path)
    except CredentialError as exc:
        return {'ok': False, 'message': str(exc), 'installed': False}

    if verify_key_login(host, port, user, key_path=key_path):
        return {
            'ok': True,
            'message': '已下发平台部署公钥，免密登录可用。' if not result['already'] else '免密登录可用。',
            'installed': not result['already'],
        }
    return {
        'ok': False,
        'message': '公钥已写入目标机，但用系统 ssh 仍然登录不上。请检查目标机 sshd 的 '
                   'PubkeyAuthentication 与 AllowUsers 配置、目标用户 home 目录的权限，'
                   '以及两端支持的密钥算法有没有交集（平台机 ssh 版本过老时，可能签不出'
                   '目标机 sshd 肯收的算法）。',
        'installed': True,
    }


def credential_map(machines, include_password=False):
    """把机器记录整理成编译与执行阶段使用的连接信息字典。

    参数：`machines` 表示 Machine 记录的可迭代对象；`include_password` 表示是否解密并带上密码。
    返回：返回 ``{ip: {'user':..., 'port':..., 'password':...}}``；不要求密码时不含该键。
    副作用：`include_password` 为真时会调用机器的解密方法取出明文密码；不写入任何文件。
    """
    result = {}
    for item in machines:
        entry = {'user': item.ssh_user or 'root', 'port': int(item.ssh_port or 22)}
        if include_password:
            try:
                entry['password'] = item.get_password() or ''
            except Exception:
                # 解密失败按「没有密码」处理，由调用方给出可读的失败原因。
                entry['password'] = ''
        result[str(item.ip_address)] = entry
    return result


def public_key_text():
    """返回平台部署公钥文本，供界面展示给用户手工预置。

    参数：无。
    返回：返回公钥文本。
    副作用：密钥不存在时会先生成一对。
    """
    _private, public = ensure_keypair()
    return public


def key_permissions_ok():
    """检查部署私钥的文件权限是否足够严格。

    参数：无。
    返回：返回 True 或 False；Windows 没有等价的权限位，恒返回 True。
    副作用：只读取文件元数据，不修改文件。
    """
    if os.name == 'nt':
        return True
    target = private_key_path()
    if not target.exists():
        return False
    return stat.S_IMODE(target.stat().st_mode) & 0o077 == 0
