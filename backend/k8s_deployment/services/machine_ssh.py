"""机器列表的 SSH 登录通道：目标解析、凭据选择与客户端建立。

机器表里的机器有两类：**还没进过集群、只托管了密码**的（新增机器时填的），以及
**平台部署过、机器上有平台公钥**的节点。所以连接方式必须在两套凭据之间按固定优先级选：
托管的密码优先，没有密码时回落平台部署密钥。这条口径原先只写在
``host_usage._machine_opener`` 里（只服务水位采集），而机器列表的 SSH 终端要用的是同一条 ——
收在这个模块里，两边共用一份实现，免得哪天只改了一处。

与 ``credentials.ssh_client`` 的分工：那边管「怎么连」（算法回退、异常翻译），这边管
「用哪套凭据连」。部署密钥仍然一律经 ``node_registry.open_session`` 走，保持
「平台的部署密钥只有一条通道」这件事成立。

本模块**不含任何交互式会话逻辑**（pty、读写循环在 ``k8s_machine.ssh_session``），
这里只回答「能不能连、用哪套凭据连」。
"""

from types import SimpleNamespace

from . import credentials, node_registry

# 交互式登录的连接与认证超时（秒）。比水位采集的 8 秒宽一点，但也不能太长：用户点
# 「SSH 登录」时人在等着「到底连不连得上」这个结论，不是把不可达的机器挂在那里几十秒。
SESSION_TIMEOUT = 12

# 凭据模式，随会话一起返回给界面，让用户知道这次是用密码还是部署密钥登进去的。
MODE_PASSWORD = 'password'
MODE_KEY = 'key'

MODE_LABELS = {
    MODE_PASSWORD: '托管密码',
    MODE_KEY: '平台部署密钥',
}


class SshTerminalError(Exception):
    """SSH 终端相关的可展示错误。

    参数：`message` 表示给用户看的中文说明；`status_code` 表示接口要返回的 HTTP 状态码。
    副作用：无。

    带上状态码是为了让视图层一句 ``except`` 就能收口 —— 「凭据没配」是 400（用户能改），
    「会话不属于你」是 403、「会话已失效」是 404，三者在界面上的处理不一样。
    """

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_target(machine):
    """从机器记录里解析出 SSH 目标。

    参数：`machine` 表示 Machine 实例。
    返回：返回 ``{'host', 'port', 'user'}``。
    副作用：只做纯计算。

    缺省值与 ``node_registry.open_session`` 的取法一致（端口 22、用户 root），
    这样「连哪台」这件事在两条通道上不会有第二种解释。
    """
    return {
        'host': str(getattr(machine, 'ip_address', '') or ''),
        'port': int(getattr(machine, 'ssh_port', None) or 22),
        'user': str(getattr(machine, 'ssh_user', '') or 'root'),
    }


def stored_password(machine):
    """取出机器托管的 SSH 密码明文，解密失败等同于没有密码。

    参数：`machine` 表示 Machine 实例。
    返回：返回明文密码；没有托管或解密失败时返回空串。
    副作用：只读取本机对象，不修改持久化数据。

    与 ``host_usage._machine_password`` 同一口径：密钥轮换、密文损坏这类问题会让解密抛异常，
    而「解不开」对连接这件事来说就是「没有密码可用」，此时应当回落部署密钥，而不是把这一行
    变成错误。返回值**只用于建立连接**，不写日志、不回显、不落任何记录。
    """
    try:
        return machine.get_password() or ''
    except Exception:  # noqa: BLE001 - 解密失败等同于没有密码，回落部署密钥
        return ''


def key_available(key_path=None):
    """判断平台部署私钥是否就位（只 stat，不连接、不读内容）。

    参数：`key_path` 表示私钥路径，省略时取平台默认位置。
    返回：文件存在返回真。
    副作用：只读取文件系统状态。
    """
    try:
        return bool((key_path or credentials.private_key_path()).exists())
    except Exception:  # noqa: BLE001 - 状态根不可读时按「没有密钥」处理
        return False


def resolve_credentials(machine, key_path=None):
    """决定登录这台机器用哪套凭据。

    参数：`machine` 表示 Machine 实例；`key_path` 表示平台部署私钥路径，省略时取默认位置。
    返回：返回 ``(mode, password)``：mode 为 ``MODE_PASSWORD`` 时 password 是明文，
        mode 为 ``MODE_KEY`` 时 password 为空串（连接时走部署密钥）。
    副作用：只读机器记录与文件系统状态，不建立连接。

    两样都没有时抛 ``SshTerminalError``：交互式登录没有凭据可回落，必须让用户先去补一份，
    而不是让他对着一个空白黑框猜。水位采集不查这一步 —— 那边一台连不上只影响自己那一行。
    """
    password = stored_password(machine)
    if password:
        return MODE_PASSWORD, password
    if key_available(key_path):
        return MODE_KEY, ''
    raise SshTerminalError(
        '这台机器没有托管 SSH 密码，平台部署密钥也不存在（多半是还没被平台部署过）；'
        '请先在「编辑机器」里补一份 SSH 密码，再打开终端。'
    )


def open_client(*, host, port, user, password=None, credential=None, key_path=None,
                client_factory=None, timeout=SESSION_TIMEOUT):
    """按「密码优先、部署密钥回落」建立一条 SSH 连接。

    参数：`host` / `port` / `user` 表示目标与登录用户；`password` 表示托管密码明文，为空时
        走部署密钥；`credential` 表示调用方给的连接信息（``{'user','port'}``，可为 None）；
        `key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试；
        `timeout` 表示连接超时秒数。
    返回：返回已连接的 paramiko.SSHClient。
    副作用：发起一次网络连接；不写文件，也不把密码写进任何日志。

    两条通道分别交代给既有的实现：密码走 ``credentials.ssh_client``（只尝试一次，不做
    rsa-sha2 回退），部署密钥走 ``node_registry.open_session``（含老 sshd 的算法回退）。
    ``credential`` 原样透传给后者，保持与水位采集完全相同的取用户/端口顺序。
    """
    if password:
        factory = client_factory or credentials.ssh_client
        return factory(host=host, port=port, username=user, password=password, timeout=timeout)
    node = SimpleNamespace(ip_address=host, ssh_port=port, ssh_user=user)
    return node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory, timeout=timeout,
    )
