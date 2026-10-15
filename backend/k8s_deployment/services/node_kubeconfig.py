"""确保节点上有 kubectl 的配置（``/root/.kube/config``）。

**这个模块为什么存在。** kubeasz 的 ``roles/prepare`` 里有两件跟 kubectl 有关的事：
建 ``/root/.kube`` 目录、把 ``<cluster_dir>/kubectl.kubeconfig`` 拷成 ``/root/.kube/config``
（``roles/prepare/tasks/main.yml`` 第 28-37 行）。但同一个角色还带一个幂等标记：
节点上存在 ``/opt/kubeasz_prepare_tasks`` 时，整个 role 直接跳过（第 2-11 行、第 105-108 行）。

标记与 kubeconfig 是两处独立的落点，删掉后者不删前者，这台机器就再也拿不回 kubectl 配置：

- 部署前的「清理上一次安装的残留」（``stale_cleanup``）会删 ``/root/.kube`` ——
  kubeasz 自己的清理清单也是这么列的 —— 但它的清单从前漏了 ``/opt/kubeasz_prepare_tasks``；
- 于是紧接着的 ``01.prepare`` 里，节点侧的 ``prepare`` 角色整段跳过：现场回执是
  ``172.30.0.132 : ok=3 changed=0 skipped=154``，日志里只有一句「节点已完成prepare_tasks，跳过」；
- 集群本身装得好好的 —— 平台用自己的 kubeconfig 做健康检查、看资源都不受影响，界面上是绿
  ——  但人登上 master 敲 ``kubectl`` 只会连 ``localhost:8080`` 报 connection refused。

2026-10-12 的 JD-k8s（114.67.203.82）就是这三个条件叠出来的「部署显示成功、登机器却用不了」。
标记那一半已在 ``stale_cleanup`` 修掉（下次清理不会再有这种半干净状态），本模块补的是另一半：
**已经装好的集群不该因为这个标记而永远缺一份配置** —— 与 ``node_path`` 补偿同一个角色里
被跳过的 PATH 那一行同理、同一时机（``01.prepare`` 之后，那时 kubeconfig 刚生成出来）。

**只补不覆盖。** 目标文件已存在（非空）就一个字都不改：那说明 prepare 正常跑过，
或者是操作员自己放的一份（可能指向别的集群），平台没有理由把它换掉。
写的是**同一份** ``clusters/<slug>/kubectl.kubeconfig`` —— kubeasz 的 prepare 拷的也是它，
内容与集群内网地址一致（``server: https://<master 内网地址>:6443``），登在节点上直接用得上。

**写不进去只记警告，不作为部署成败的判据**（同 ``node_path``）：这是给人用的便利配置，
集群本身是好的；为它把一次成功的部署报成失败，比少一份 kubeconfig 更难查。
"""

import base64

from . import node_registry

# 节点上的落点。kubeasz 的约定是 ``~/.kube/config``（连接用户是 root，就是这两个路径），
# 权限也照抄它那份：只有 root 能读。
KUBE_DIR = '/root/.kube'
KUBECONFIG_PATH = f'{KUBE_DIR}/config'
MODE = '400'
# 单条远程命令的超时秒数。文件只有几 KB，给宽一点是因为忙机器上 mkdir/chmod 也会排队。
COMMAND_TIMEOUT = 60

# 存在性探测的回执标记，调用方据此决定要不要写。
PRESENT = 'K8SMANAGER_KUBECONFIG_PRESENT'
MISSING = 'K8SMANAGER_KUBECONFIG_MISSING'
# 写入后回读字节数的回执前缀（``wc -c`` 的输出就是一个数字，单独留一个标记是为了让
# 「命令没跑起来」和「文件写成了 0 字节」两种情况分得开）。
LANDED = 'K8SMANAGER_KUBECONFIG_BYTES'


class NodeKubeconfigError(Exception):
    """节点上的 kubectl 配置写不进去，携带可直接展示给用户的中文原因与错误码。

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


def present_command():
    """生成「节点上有没有可用的 kubectl 配置」的探测命令。

    参数：无。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    ``-s``（非空）而不是 ``-e``：0 字节的 config 对 kubectl 等于没有，而它恰恰是
    「上次写到一半断了」最容易留下的样子，得当缺失处理重写一遍。
    """
    return (
        f'if [ -s {KUBECONFIG_PATH} ]; then echo {PRESENT}; '
        f'else echo {MISSING}; fi'
    )


def write_command(text):
    """生成「把 kubeconfig 写到节点上」的命令。

    参数：`text` 表示 kubeconfig 文本。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    走 base64 而不是 SFTP 或 heredoc，与 ``node_path.write_text_command`` 同一理由：
    payload 只含 ``[A-Za-z0-9+/=]``，塞进单引号里绝对安全，也不依赖节点的 SFTP 子系统。

    **刻意不回显内容**（``node_path`` 那边会 ``cat`` 出来核对）：这是一份内置了 admin
    客户端证书与私钥的 kubeconfig，回显出来就会跟着命令输出进入任务日志。核对的活儿交给
    末尾那行 ``wc -c``：只回一个数字，同样能发现「写空了」「写截断了」。
    """
    payload = base64.b64encode(str(text).encode('utf-8')).decode('ascii')
    return (
        f'mkdir -p {KUBE_DIR} && '
        f"printf '%s' '{payload}' | base64 -d > {KUBECONFIG_PATH} && "
        f'chmod {MODE} {KUBECONFIG_PATH} && '
        f'echo {LANDED}:$(wc -c < {KUBECONFIG_PATH}) '
        f'|| echo {LANDED}:0'
    )


def parse_landed(output):
    """从写入命令的输出里取出落地字节数。

    参数：`output` 表示命令的标准输出。
    返回：返回字节数；读不出来时返回 -1（调用方据此判失败）。
    副作用：只做纯计算。
    """
    for line in str(output or '').splitlines():
        text = line.strip()
        if text.startswith(f'{LANDED}:'):
            try:
                return int(text.split(':', 1)[1].strip())
            except ValueError:
                return -1
    return -1


def ensure_node(context, node, text, credential=None, key_path=None, client_factory=None):
    """确保一台节点上有 kubectl 配置，缺了就补一份。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`text` 表示要写入的 kubeconfig 文本；
        `credential` 表示连接信息；`key_path` 表示部署私钥路径；`client_factory` 表示可注入的
        连接工厂，便于测试。
    返回：返回 ``{'label', 'action', 'bytes'}``：``action`` 为 ``kept``（本来就有一份，没动）
        或 ``written``（这次补上了）。
    副作用：目标机上缺配置时创建 ``/root/.kube`` 并写入 ``/root/.kube/config``。

    幂等：先探测再决定，存在即不动；写完之后按字节数核对，对不上算失败。
    **任何日志、事件里都不出现 kubeconfig 的内容** —— 只报字节数与路径。
    """
    if not str(text or '').strip():
        raise NodeKubeconfigError(
            'node_kubeconfig_missing_source',
            '平台上还没有生成这个集群的 kubeconfig，无法补到节点上（01.prepare 可能没跑完）。',
        )
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    expected = len(str(text).encode('utf-8'))
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        code, out, err = node_registry.run(client, present_command(), timeout=COMMAND_TIMEOUT)
        if code != 0:
            raise NodeKubeconfigError(
                'node_kubeconfig_failed',
                f'读取节点 {label} 上的 {KUBECONFIG_PATH} 失败：{err or out or "退出码 " + str(code)}。',
            )
        if PRESENT in out:
            context.log(
                'system',
                f'节点 {label}：{KUBECONFIG_PATH} 已存在，保持原样（kubeasz 的 prepare 正常跑过，'
                '或用的是操作员自己那份）',
            )
            return {'label': label, 'action': 'kept', 'bytes': 0}

        code, out, err = node_registry.run(
            client, write_command(text), timeout=COMMAND_TIMEOUT,
        )
        landed = parse_landed(out)
        if code != 0 or landed != expected:
            raise NodeKubeconfigError(
                'node_kubeconfig_failed',
                f'向节点 {label} 写入 {KUBECONFIG_PATH} 失败'
                f'（期望 {expected} 字节，实际 {landed if landed >= 0 else "读不到"} 字节）：'
                f'{err or out.strip() or "退出码 " + str(code)}。',
            )
    finally:
        node_registry.close_session(client)

    context.log(
        'system',
        f'节点 {label}：已补写 {KUBECONFIG_PATH}（{landed} 字节，权限 {MODE}）——'
        '这台机器的 kubectl 之前没有配置，登上去敲会连 localhost:8080 报连接被拒'
        '（kubeasz 的 prepare 被它自己的幂等标记跳过，没执行这一步）',
    )
    return {'label': label, 'action': 'written', 'bytes': landed}
