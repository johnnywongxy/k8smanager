"""清掉节点上已经退出（Exited）的容器。

集群装完之后，节点上一定会留下几个「跑完就退出」的容器 —— calico-node 的 ``install-cni``、
``mount-bpffs`` 这类 initContainer 干完活就退（exitCode 0），containerd 却把容器对象一直留着，
``crictl ps -a`` / ``docker ps -a`` 里永远挂着几行 ``Exited``。它们不占运行资源，但会：

- 占住可写层与日志目录，容器攒起来是实打实的磁盘；
- 让「这台机器上到底跑了什么」多出一堆噪声，排查故障时要先分辨哪几行是活的。

平台在部署收尾时清一遍：**只删运行时自己报告为 ``CONTAINER_EXITED`` 的容器**，并且带上
``--keep-logs`` —— 容器对象没了，日志文件仍留在 ``/var/log/pods/<ns>_<pod>_<uid>/`` 下面，
``kubectl logs -p`` 之外的手工排查还有据可查。这也是本模块不按退出码过滤的原因：留着日志，
删掉一个跑完的容器就不会带走任何排查线索。

删掉一个已经跑完的 initContainer 不会让 kubelet 重跑它、也不会重启 Pod：kubelet 在决定
是否重跑 init container 之前会先看主容器是否正在运行（Kubernetes 1.20 起的行为），而本平台
部署的版本远在那之上。

**这一步不影响任务成败。** 它是收尾打扫，清不掉也只是节点上多留几个退出容器；为它把一次
成功的部署报成失败，代价远大于收益。因此 ``cleanup_node`` 不抛错，失败只写进报告的 ``error``
字段，由调用方逐台记日志（与 ``node_path``、``node_registry.uninstall`` 同一种口吻）。

``crictl`` 从 inventory 里的 ``bin_dir`` 取（见 ``node_path.bin_dir_for``），目录里没有这个文件时
退回 PATH 上的 ``crictl``：kubeasz 会把命令拷到 ``bin_dir``，但那台机器上可能顺手装过系统包。
"""

import json
import re

from . import node_registry

# kubeasz 拷到 ``bin_dir`` 的命令名，也是 PATH 上的命令名。
CRICTL_NAME = 'crictl'
# ``crictl ps`` 的 JSON 里表示「已退出」的取值。只认这一个：``CONTAINER_UNKNOWN`` 之类的
# 状态含义不明，删了可能真的带走现场。
EXITED_STATE = 'CONTAINER_EXITED'
# 一次 ``crictl rm`` 最多带多少个 id。容器多到成千上万时命令行会超长，分批删。
MAX_BATCH = 50
# 容器 id 的形状校验：真正的 id 是十六进制串，但运行时各有各的格式（CRI-O 用短横线）。
# 只用来挡住 ``$(...)``、空格、引号这类会改变命令含义的字符，不做格式强校验。
IDENTIFIER_PATTERN = re.compile(r'^[0-9A-Za-z][0-9A-Za-z_.:-]{3,127}$')
# 日志里最多列几个容器名，多了就省略。
NAMES_IN_LOG = 5


def crictl_command(bin_dir, arguments):
    """生成一条用节点上的 ``crictl`` 执行的命令。

    参数：`bin_dir` 表示 inventory 里的 kubeasz 二进制目录；`arguments` 表示 crictl 的参数。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    写成 ``if [ -x ... ]`` 而不是直接拼路径：目录可能被操作员改到数据盘上，也可能这台机器上
    压根没拷过（例如全新的节点还没跑过 prepare），退回 PATH 上的 ``crictl`` 至少还有一次机会。
    """
    if not bin_dir:
        return f'{CRICTL_NAME} {arguments}'
    candidate = f'{bin_dir}/{CRICTL_NAME}'
    return f'if [ -x {candidate} ]; then {candidate} {arguments}; else {CRICTL_NAME} {arguments}; fi'


def list_command(bin_dir):
    """生成「列出已退出容器」的命令。

    参数：`bin_dir` 表示 kubeasz 二进制目录。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    在节点上过滤（``--state exited``）而不是把全部容器拉回来再筛：一台跑久了的节点上
    容器可能上千，JSON 走 SSH 通道回来是白花的带宽；这里通常只有两三条。
    """
    return crictl_command(bin_dir, 'ps -a --state exited -o json')


def remove_command(bin_dir, identifiers):
    """生成「删掉这些容器」的命令。

    参数：`bin_dir` 表示 kubeasz 二进制目录；`identifiers` 表示容器 id 列表。
    返回：返回一段 shell 命令。
    副作用：只做纯计算。

    ``--keep-logs`` 不能省：默认行为会把容器的日志文件连同轮转一起删掉，而日志恰恰是
    出问题时唯一能回头看的东西。留着日志，删容器就只是清理运行时的对象。
    """
    return crictl_command(bin_dir, 'rm --keep-logs ' + ' '.join(identifiers))


def json_payload(text):
    """从命令输出里截出 JSON 本体。

    参数：`text` 表示命令输出。
    返回：返回 JSON 文本；整段输出是空的时候返回空串。
    副作用：只做纯计算。

    ``crictl`` 会在标准输出前面打若干行 ``WARN[...]``（镜像端点、配置缺失之类），
    直接从第一个 ``{`` 切到最后一个 ``}``，比逐行猜哪一行是正文稳。

    有内容却找不到成对的 ``{}`` 时抛 ``ValueError``：那说明输出被截断了（SSH 读超时）
    或者根本不是这条命令的输出。把它当成「没有容器可删」会安静地什么也不做 ——
    而操作员正以为这一步已经清过了。
    """
    if not text.strip():
        return ''
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('crictl 的输出里没有可解析的 JSON')
    return text[start:end + 1]


def parse_exited(text):
    """把 ``crictl ps`` 的 JSON 解析成已退出容器的清单。

    参数：`text` 表示命令输出。
    返回：返回字典列表，每项含 ``id``、``name``、``pod``、``namespace``、``restart_count``。
    副作用：只做纯计算。

    没有输出、或输出里没有容器时返回空列表（``crictl`` 在结果为空时不一定打印什么）。
    有内容却读不成 JSON 时抛 ``ValueError`` —— 那是命令变了、输出被截断或运行时坏了，
    值得让操作员知道，但不能让部署失败。
    """
    payload = json_payload(text)
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f'crictl 的输出不是可解析的 JSON（{exc}）') from exc
    if not isinstance(data, dict):
        raise ValueError('crictl 的 JSON 顶层不是对象')

    result = []
    for item in data.get('containers') or []:
        if not isinstance(item, dict) or item.get('state') != EXITED_STATE:
            continue
        identifier = str(item.get('id') or '')
        if not IDENTIFIER_PATTERN.match(identifier):
            continue
        labels = item.get('labels') or {}
        annotations = item.get('annotations') or {}
        metadata = item.get('metadata') or {}
        result.append({
            'id': identifier,
            'name': str(metadata.get('name') or labels.get('io.kubernetes.container.name') or ''),
            'pod': str(labels.get('io.kubernetes.pod.name') or ''),
            'namespace': str(labels.get('io.kubernetes.pod.namespace') or ''),
            'restart_count': str(annotations.get('io.kubernetes.container.restartCount') or '0'),
        })
    return result


def describe(item):
    """把一条容器记录写成给人看的一行。

    参数：`item` 表示 ``parse_exited`` 返回的记录。
    返回：返回文本。
    副作用：只做纯计算。
    """
    where = '/'.join(part for part in (item['namespace'], item['pod']) if part)
    return f'{where}:{item["name"]}' if where else (item['name'] or item['id'][:12])


def cleanup_node(context, node, bin_dir_value, credential=None, key_path=None, client_factory=None):
    """清掉一台节点上处于 ``Exited`` 状态的容器。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`bin_dir_value` 表示节点上的 kubeasz
        二进制目录；`credential` 表示连接信息；`key_path` 表示部署私钥路径；`client_factory`
        表示可注入的连接工厂，便于测试。
    返回：返回 ``{'label', 'found', 'removed', 'error'}``；``error`` 非空表示这一步没做成。
    副作用：在目标机上执行 ``crictl``，删除已退出的容器对象（保留日志文件）。

    幂等：第二次执行时已经没有已退出的容器，``found`` 为 0。**不抛错**：连不上、没有 ``crictl``、
    输出看不懂，都只写进 ``error`` 并记一行日志 —— 这是收尾打扫，不是部署成败的判据。
    """
    label = str(getattr(node, 'ip_address', '') or '') or getattr(node, 'name', '节点')
    report = {'label': label, 'found': 0, 'removed': 0, 'error': ''}
    client = node_registry.open_session(
        node, credential, key_path=key_path, client_factory=client_factory,
    )
    try:
        code, out, err = node_registry.run(client, list_command(bin_dir_value))
        if code != 0:
            report['error'] = f'列不出容器：{err or out or "退出码 " + str(code)}'
            return report
        try:
            containers = parse_exited(out)
        except ValueError as exc:
            report['error'] = str(exc)
            return report
        report['found'] = len(containers)
        if not containers:
            return report

        identifiers = [item['id'] for item in containers]
        names = '、'.join(describe(item) for item in containers[:NAMES_IN_LOG])
        if len(containers) > NAMES_IN_LOG:
            names += f' 等 {len(containers)} 个'
        removed, problems = _remove_all(client, bin_dir_value, identifiers)
        report['removed'] = removed
        if problems:
            report['error'] = problems
        context.log(
            'system',
            f'节点 {label}：已清理 {removed}/{len(containers)} 个已退出的容器（{names}）。'
            '容器日志文件保留在 /var/log/pods 下，需要排查时仍可查看。',
        )
        return report
    finally:
        node_registry.close_session(client)


def _remove_all(client, bin_dir_value, identifiers):
    """分批删掉给定的容器 id。

    参数：`client` 表示已连接的客户端；`bin_dir_value` 表示 kubeasz 二进制目录；
        `identifiers` 表示容器 id 列表。
    返回：返回 ``(成功数, 失败说明)``，都成功时失败说明为空串。
    副作用：在目标机上执行删除命令。

    一条命令删一批：容器多起来时逐条一个来回太慢。代价是拿不到单条的成败，所以按命令的
    退出码判整体结果；失败时从输出里数出实际删掉的那几个 —— ``crictl rm`` 每删一个回显一个 id，
    部分失败（例如某个容器刚好又被拉起来）时这个数才对得上。
    """
    removed = 0
    problems = []
    for start in range(0, len(identifiers), MAX_BATCH):
        batch = identifiers[start:start + MAX_BATCH]
        code, out, err = node_registry.run(client, remove_command(bin_dir_value, batch))
        if code == 0:
            removed += len(batch)
            continue
        done = sum(1 for item in batch if item in out)
        removed += done
        problems.append(f'删除失败（{err or out or "退出码 " + str(code)}）')
    return removed, '；'.join(problems)
