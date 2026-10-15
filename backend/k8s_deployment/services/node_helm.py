"""把 Helm 客户端补到节点的 ``bin_dir``（``<bin_dir>/helm``）。

**这个模块为什么存在。** 部署向导里的「Helm」勾选（``K8sDeployment.install_helm_cli``）
在界面上写的是「启用后部署时一并准备 Helm 客户端」，可它此前只喂了一处判断：
``artifacts.missing_addon_artifacts`` 拿它决定「这个 release 的 OSS 目录里要不要有
``helm/helm_<arch>``」。真正把 helm 放到哪儿的动作一处都没有 —— 于是勾了、部署成功、
登上 master 敲 ``helm list``，得到的是 ``helm: command not found``（2026-10-13 的反馈）。

**kubeasz 自己为什么不管这件事。** ``playbooks/07.cluster-addon.yml`` 是 ``hosts: localhost``，
cilium 那批任务还带 ``connection: local`` —— 装 addons 的 ``helm upgrade --install`` 全在
**控制机**（平台机）上跑，用的是 ``{{ base_dir }}/bin/helm``。控制机上那份由
``registry.install_carrier_binaries`` 从 ``easzlab/kubeasz-ext-bin`` 解出来，与这个勾选无关、
一直都有。节点侧正相反：``roles/kube-master`` / ``roles/kube-node`` 里那条「分发二进制」
是**写死的清单**（``copy: src={{ base_dir }}/bin/{{ item }} dest={{ bin_dir }}/{{ item }}
mode=0755``，apiserver、kubelet、kubectl、kube-proxy……），helm 不在里面 —— 节点上自然
也就没有。

**本模块照那条清单的写法补一条。** 目标目录取 ``bin_dir``（默认 ``/opt/kube/bin``）而不是
``/usr/local/bin``：``node_path`` 已经把这个目录写进节点的系统 PATH
（``/etc/profile.d/kubeasz-path.sh``、``/etc/environment``），而且 kubectl 就躺在那里 ——
helm 与它同目录，登上节点敲 ``helm`` 的手感跟 ``kubectl`` 完全一致。范围也照 kubectl：
**每台节点都装**（kubeasz 给工人节点也发 kubectl，还顺手写了 ``kubectl completion``），
不区分 control-plane。

**为什么用控制机上那份，而不是再从 OSS 取一份。** ``sync_kubeasz_artifacts.py --include-helm``
上传的 ``releases/<release>/helm/helm_<arch>``，**就是**从同一个 ``easzlab/kubeasz-ext-bin``
镜像里解出来的同一个文件，两份二进制按定义一致。而控制机上那份由本次任务的前置阶段
``_prepare_kubeasz_home`` 铺好，直接拿来用既不额外下载，也保证了「节点上的 helm」与
「kubeasz 装 addons 用的 helm」是同一个版本 —— 同一件事只留一个来源。

**只装不覆盖。** 目标文件已经存在（且跑得起来）就一个字都不改：那说明上次装过，或者是
操作员自己放的（可能是有意选的版本）—— 60MB 的文件没有理由重传一遍。回执里带上版本与
字节数，是操作员自己截断过一个文件时唯一的线索。

**单台失败只记警告，不作为部署成败的判据**（同 ``node_path`` / ``node_kubeconfig``）：
这是给人用的客户端，集群本身是好的；为它把一次成功的部署报成失败，比少一个命令更难查。
没勾「Helm」时调用方直接跳过，本模块不参与那个判断。

**清理集群不额外处理。** 文件落在 ``bin_dir`` 里，与 kubectl 同等对待：kubeasz 的
``roles/clean`` 只删 ``~/.bashrc`` 里那几行补全（删目录那一条在它自己的源码里是注释掉的），
``node_path.remove_node`` 也只撤 PATH 不动目录 —— 目录里的命令归 kubeasz 管。
"""

from pathlib import Path

from . import node_registry

# 节点上的文件名。目录由调用方传（``node_path.bin_dir_for`` 从渲染后的 inventory 里读），
# 不在这里写死 ``/opt/kube/bin`` —— 操作员可以把 kubeasz 的 bin 目录改到数据盘上。
BINARY_NAME = 'helm'
# 与 kubeasz 分发 kubectl 时用的权限一致（那条 copy 任务写的是 ``mode=0755``）。
MODE = 0o755
# 先传这个后缀再原子换成最终路径，理由见 ``install_node``。
PART_SUFFIX = '.part'
# 单条远程命令的超时秒数。只有 mkdir / mv / wc 这几条，给宽一点是因为忙机器上也会排队。
COMMAND_TIMEOUT = 120

# 探测回执的三个标记。存在与不存在分开写，是为了让「命令没跑起来」与「机器上没有」分得开；
# 版本与字节数各自带前缀，免得从路径里解析出数字来。
PRESENT = 'K8SMANAGER_HELM_PRESENT'
MISSING = 'K8SMANAGER_HELM_MISSING'
VERSION_LABEL = 'K8SMANAGER_HELM_VERSION'


class NodeHelmError(Exception):
    """节点上的 helm 装不上或验不过，携带可直接展示给用户的中文原因与错误码。

    参数：`error_code` 表示机器可读的错误码；`message` 表示中文原因。
    返回：无显式返回值。
    副作用：只承载信息，不执行动作。

    与 ``NodePathError`` 同理，本模块不直接抛 ``state_machine.TaskAborted``：那个类在导入
    方向上属于上层，由调用方转换。
    """

    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def binary_path(bin_dir):
    """返回节点上 helm 的完整路径。

    参数：`bin_dir` 表示节点上的 kubeasz 二进制目录。
    返回：返回路径字符串。
    副作用：只做纯计算。

    目录末尾多写一个斜杠时结果不变：这个值是从 inventory 里读出来的，操作员怎么写都可能。
    """
    return f'{str(bin_dir).rstrip("/")}/{BINARY_NAME}'


def local_binary(kubeasz_root):
    """返回控制机上待分发的 helm 路径，没有时返回 None。

    参数：`kubeasz_root` 表示 kubeasz 安装目录（``base_dir``）。
    返回：返回 ``Path``；文件不存在时返回 None，由调用方记一条日志后跳过本次安装。
    副作用：读一次文件元数据。

    取的就是 kubeasz 自己找二进制的那一处（``{{ base_dir }}/bin``）—— 节点上的 kubelet、
    kubectl 也都是从这里发出去的。
    """
    path = Path(kubeasz_root) / 'bin' / BINARY_NAME
    return path if path.is_file() else None


def probe_command(bin_dir):
    """生成「节点上有没有能用的 helm」的探测命令。

    参数：`bin_dir` 表示节点上的 kubeasz 二进制目录。
    返回：返回一条 shell 命令。
    副作用：在目标机上执行时只读（``-x``、``wc -c``，以及 helm 自己的 ``version``）。

    存在性不只看 ``-x``：一个传到一半的 60MB 文件同样是「存在且可执行」的，跑一下
    ``version --short`` 才知道它到底能不能用。版本号与字节数一起回执，是因为「机器上那份
    是哪儿来的、完不完整」只有这两个数字说得清。

    ``helm version --short`` 不读 kubeconfig、不连集群，在任何节点上都问得出去。
    """
    target = binary_path(bin_dir)
    return (
        f'if [ -x {target} ]; then '
        f'echo {PRESENT}; '
        f'echo {VERSION_LABEL}:$({target} version --short 2>/dev/null || echo "?") ; '
        f'wc -c < {target}; '
        f'else echo {MISSING}; fi'
    )


def prepare_command(bin_dir):
    """生成「备好落地目录、清掉上次没传完的临时文件」的命令。

    参数：`bin_dir` 表示节点上的 kubeasz 二进制目录。
    返回：返回一条 shell 命令。
    副作用：在目标机上执行时创建目录、删除可能残留的 ``<bin_dir>/helm.part``。

    目录通常已经由 ``01.prepare`` 建好，``mkdir -p`` 只是把「平台上这一步先跑了」这个
    顺序问题挡掉；清理残留是因为上一次失败的传输会留下半截 ``.part``，重传时不必先问它。
    """
    return f'mkdir -p {bin_dir} && rm -f {binary_path(bin_dir)}{PART_SUFFIX}'


def activate_command(bin_dir):
    """生成把临时文件原子换到最终路径的命令。

    参数：`bin_dir` 表示节点上的 kubeasz 二进制目录。
    返回：返回一条 shell 命令。
    副作用：在目标机上执行时覆盖 ``<bin_dir>/helm``。

    ``mv`` 在同一个文件系统内是原子的（``bin_dir`` 与 ``.part`` 同目录）。用它的理由与
    ``node_path.write_text_command`` 里那段一样：传输中断时，最终路径上不该出现一个
    「看起来装好了、其实跑不起来」的半截文件。
    """
    return f'mv -f {binary_path(bin_dir)}{PART_SUFFIX} {binary_path(bin_dir)}'


def parse_probe(output):
    """从探测命令的输出里取出存在性、版本与字节数。

    参数：`output` 表示命令的标准输出。
    返回：返回 ``{'present', 'version', 'bytes'}``：``bytes`` 读不出来时为 -1。
    副作用：只做纯计算。
    """
    report = {'present': False, 'version': '', 'bytes': -1}
    for line in str(output or '').splitlines():
        text = line.strip()
        if text == PRESENT:
            report['present'] = True
        elif text.startswith(f'{VERSION_LABEL}:'):
            report['version'] = text.split(':', 1)[1].strip()
        elif text.isdigit():
            # 只有 ``wc -c`` 会单独吐一个数字，其余行都带标记。
            report['bytes'] = int(text)
    return report


def describe(report):
    """把探测回执拼成一句能进日志的话。

    参数：`report` 表示 ``parse_probe`` 的返回值。
    返回：返回形如 ``v3.14.0+g3fc9f4b，50331648 字节`` 的字符串。
    副作用：只做纯计算。
    """
    version = report.get('version') or '版本未知'
    size = report.get('bytes')
    if isinstance(size, int) and size >= 0:
        return f'{version}，{size} 字节'
    return version


def _probe(client, label, bin_dir_value):
    """在节点上探测一次，读不出来就报错。

    参数：`client` 表示已连接的客户端；`label` 表示节点标识（日志与报错里用）；
        `bin_dir_value` 表示节点上的 kubeasz 二进制目录。
    返回：返回 ``parse_probe`` 的结果。
    副作用：在目标机上执行一条只读命令。
    """
    code, out, err = node_registry.run(client, probe_command(bin_dir_value), timeout=COMMAND_TIMEOUT)
    if code != 0:
        raise NodeHelmError(
            'node_helm_probe_failed',
            f'读取节点 {label} 上的 {binary_path(bin_dir_value)} 失败：'
            f'{err or out or "退出码 " + str(code)}。',
        )
    return parse_probe(out)


def install_node(context, node, bin_dir_value, local_path, credential=None,
                 key_path=None, client_factory=None):
    """确保一台节点上有 helm，缺了就补一份。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`bin_dir_value` 表示节点上的
        kubeasz 二进制目录；`local_path` 表示控制机上那份 helm 的路径；`credential` 表示
        连接信息；`key_path` 表示部署私钥路径；`client_factory` 表示可注入的连接工厂，便于测试。
    返回：返回 ``{'label', 'action', 'version', 'bytes'}``：``action`` 为 ``kept``（本来就有
        一份能用的，一个字没动）或 ``installed``（这次装上了）。
    副作用：目标机上缺 helm 时写 ``<bin_dir>/helm``（0755）。

    幂等：先探测再决定，能用的那份不动；装完**再探一次** —— ``put_file`` 自己核对过字节数，
    但只有真的跑起来 ``helm version`` 才算装好了（被中间设备改写过的文件大小可以对得上）。
    """
    if not str(local_path or ''):
        raise NodeHelmError(
            'node_helm_source_missing',
            '没有可用的 helm 源文件（控制机的 kubeasz bin 目录里没有 helm），无法安装到节点上。',
        )
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    source = Path(local_path)
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        report = _probe(client, label, bin_dir_value)
        if report['present']:
            context.log(
                'system',
                f'节点 {label}：{binary_path(bin_dir_value)} 已存在（{describe(report)}），保持原样',
            )
            return {'label': label, 'action': 'kept', **report}
        code, out, err = node_registry.run(
            client, prepare_command(bin_dir_value), timeout=COMMAND_TIMEOUT,
        )
        if code != 0:
            raise NodeHelmError(
                'node_helm_prepare_failed',
                f'节点 {label} 上准备 {bin_dir_value} 失败：'
                f'{err or out or "退出码 " + str(code)}。',
            )
        try:
            node_registry.put_file(
                client, source, f'{binary_path(bin_dir_value)}{PART_SUFFIX}',
                mode=MODE, expected_size=source.stat().st_size,
            )
        except node_registry.NodeRegistryError as exc:
            # 换成带 helm 语境的错误码与说明：``put_file`` 那句话的主语是镜像仓库的二进制，
            # 直接抛上去会让人以为仓库那一步出了问题。
            raise NodeHelmError(
                'node_helm_upload_failed',
                f'把 {source.name}（{source.stat().st_size} 字节）传到节点 {label} 失败：{exc.message}',
            ) from exc
        code, out, err = node_registry.run(
            client, activate_command(bin_dir_value), timeout=COMMAND_TIMEOUT,
        )
        if code != 0:
            raise NodeHelmError(
                'node_helm_activate_failed',
                f'节点 {label} 上把 helm 换到 {binary_path(bin_dir_value)} 失败：'
                f'{err or out or "退出码 " + str(code)}。',
            )
        report = _probe(client, label, bin_dir_value)
        if not report['present']:
            raise NodeHelmError(
                'node_helm_verify_failed',
                f'节点 {label} 上的 helm 传完了却跑不起来（{binary_path(bin_dir_value)}），'
                '这次安装算失败。',
            )
        context.log('system', f'节点 {label}：已装好 helm（{describe(report)}）')
        return {'label': label, 'action': 'installed', **report}
    finally:
        node_registry.close_session(client)
