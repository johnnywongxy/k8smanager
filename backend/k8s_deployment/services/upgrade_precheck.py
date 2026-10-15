"""升级前的前置检查：版本 skew、驱逐条件、目标制品与配置模板差异（规划 10.4）。

规划要求「升级前强制完成一次可用备份，检查版本 skew（保守要求节点版本一致）、etcd quorum、
驱逐条件和目标制品」。etcd quorum 由预检里的仲裁检查项负责
（``preflight._check_etcd_quorum``），本模块负责其余四项：

* **版本 skew**：读集群里每个节点的 kubelet 版本与 Ready 状态。kubelet 版本不一致的集群
  升级到一半会停在「一部分节点新、一部分节点旧」的状态，而 NotReady 的节点根本滚不动，
  这两件事都必须在动集群之前发现。
* **驱逐条件**：升级要逐台 drain 节点，任何 ``disruptions_allowed`` 已经为 0 的 PDB 都会
  让 drain 卡住。这里只给警告而不是硬失败——PDB 挡住的是一次「安全驱逐」，人为判断
  之后往往仍要继续升级，把它判死会让升级永远做不了。
* **目标制品**：kubeasz 的离线包只能装 ezdown 里声明的那一个 Kubernetes 版本。目标版本
  与制品版本不一致时 ``ezctl upgrade`` 会在下载阶段失败，此时集群已经被改过一部分了。
* **配置模板差异**：跨次版本升级会连着换 kubeasz 版本，而升级不重渲染 config.yml，
  于是「新模板里的变量、旧配置里没有」这件事只在这时看得见。它只警告并列出清单，
  理由见 ``check_config_compatibility``。

所有检查只读，不改目标集群；判断不出来的情况一律给警告而不是失败，避免「查不出来」被
当成「有问题」而挡住一次合规的升级（与预检里的取舍一致）。
"""

import re
from pathlib import Path

from django.utils import timezone

from . import compiler

# 升级目标与节点 kubelet 之间允许跨越的次版本数。
#
# **1，不是 2。** 平台一次只允许升一个次版本（Kubernetes 不允许跳 minor），所以与目标
# 相隔两个及以上次版本本身就是不该发生的输入；这里取 1 是把同一件事在执行期再钉一遍。
# （以前这个值是 2、注释写的是「跨次版本平台本来就不做，这里再兜一道」—— 跨一个次版本
# 现在是允许的，那个取值与理由都不成立了。）
MAX_MINOR_SKEW = 1

# config.yml 顶层键：kubeasz 的集群变量文件是一层平铺的 ``KEY: 值``（夹着注释行）。
# 用正则取键而不是解析 YAML —— 这里只需要「有哪些名字」，解析成对象反而要处理值里嵌的
# 字典与列表、引号、多行标量，一处读不动就什么都拿不到；而且它必须容忍一份被手工改过、
# 并不严格合法的文件（那正是升级要面对的东西）。
CONFIG_KEY_PATTERN = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:', re.MULTILINE)


def parse_version(value):
    """把版本文本解析成 ``(主版本, 次版本, 补丁)``。

    参数：`value` 表示版本文本，例如 ``v1.29.4``、``1.29``。
    返回：返回三元组；无法解析时返回 None。
    副作用：纯计算，不修改任何状态。
    """
    text = str(value or '').strip().lstrip('v')
    if not text:
        return None
    # 语义化版本的预发布（-rc.1）与构建元数据（+k3s1）都不参与版本比较，
    # 而 kubelet 的 ``gitVersion`` 常常带着构建后缀上报。
    parts = text.split('-', 1)[0].split('+', 1)[0].split('.')
    numbers = []
    for index in range(3):
        if index < len(parts) and parts[index].isdigit():
            numbers.append(int(parts[index]))
        else:
            numbers.append(0)
    if not parts[0].isdigit():
        return None
    return (numbers[0], numbers[1], numbers[2])


def _minor_gap(left, right):
    """计算两个版本之间相隔的次版本数。

    参数：`left`/`right` 表示两个版本文本。
    返回：返回相隔的次版本数；任一侧无法解析时返回 None。
    副作用：纯计算，不修改任何状态。
    """
    first = parse_version(left)
    second = parse_version(right)
    if first is None or second is None:
        return None
    return abs((first[0] * 1000 + first[1]) - (second[0] * 1000 + second[1]))


def check_version_skew(cluster_state, target_version):
    """校验节点 kubelet 版本的 skew（规划 10.4）。

    参数：`cluster_state` 表示 ``k8s_cluster.services.probe_cluster`` 的返回值；
        `target_version` 表示本次升级的目标版本。
    返回：返回 ``(是否通过, 中文说明, 明细字典)``。
    副作用：纯计算，不修改任何状态。

    保守要求「节点版本一致」：只要存在两个不同的 kubelet 版本就先停下来。这不代表集群
    一定有问题，而是代表这台集群的升级会以平台无法预测的顺序推进，让人先看一眼比自动
    往下走安全。同一次版本内部的补丁差异（1.29.3 与 1.29.4）同样按不一致处理，理由相同。
    """
    summary = cluster_state.get('node_summary') or {}
    details = summary.get('details') or []
    api_version = str(cluster_state.get('kube_version') or '')
    detail = {
        'api_version': api_version,
        'target_version': target_version,
        'versions': summary.get('versions') or {},
        'node_count': summary.get('total') or 0,
        'not_ready': summary.get('not_ready') or [],
    }
    if not details:
        return True, '未能取得节点列表，跳过版本 skew 检查', detail

    kubelets = sorted({item['kubelet_version'] for item in details if item.get('kubelet_version')})
    detail['kubelet_versions'] = kubelets
    if not kubelets:
        return True, '节点上没有上报 kubelet 版本，跳过版本 skew 检查', detail

    target = parse_version(target_version)
    if target is None:
        return False, f'无法解析升级目标版本「{target_version}」，已中止升级', detail

    # 目标与节点 kubelet 之间最多隔一个次版本。跨一个次版本（1.35 → 1.36）是允许的，
    # 跨两个及以上不允许 —— 与 ``state_machine._assert_upgrade_target``、
    # ``cluster_ops._validate_upgrade_target`` 同一口径，这里再兜一道：
    # 即使调用方绕过了版本校验，也不会让节点一次滚过两个次版本。
    for item in kubelets:
        gap = _minor_gap(item, target_version)
        if gap is None:
            continue
        if gap > MAX_MINOR_SKEW:
            return False, (
                f'节点 kubelet 版本 {item} 与升级目标 {target_version} 相隔 {gap} 个次版本，'
                f'超过允许的 {MAX_MINOR_SKEW} 个，请先升级到中间版本'
            ), detail

    if len(kubelets) > 1:
        return False, (
            '集群内节点的 kubelet 版本不一致（' + '、'.join(kubelets) + '）。'
            '请先把所有节点拉到同一个版本再升级，否则升级会在版本混杂的集群上进行。'
        ), detail

    not_ready = detail['not_ready']
    if not_ready:
        return False, (
            f'以下节点当前不是 Ready 状态：{"、".join(not_ready)}。'
            '升级需要逐台滚动这些节点，请先确认节点状态。'
        ), detail

    only = kubelets[0]
    if api_version and _minor_gap(only, api_version) not in (None, 0):
        # kubelet 与 apiserver 的次版本不同说明集群本身就处在一个中间态，
        # 直接升级会在这个中间态上再叠一层变化。
        return False, (
            f'节点 kubelet 版本 {only} 与 API Server 版本 {api_version} 不在同一个次版本上，'
            '请先把集群拉齐到一致状态再升级'
        ), detail
    return True, f'节点 kubelet 版本一致（{only}），且与升级目标 {target_version} 相容', detail


def check_drain_conditions(cluster):
    """检查升级滚动时的驱逐条件：是否存在已经不允许驱逐的 PDB。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``(是否有风险, 中文说明, 明细字典)``。
    副作用：通过 Kubernetes API 读取工作负载列表，不修改任何状态。
    """
    from k8s_resource.services import resource_actions

    try:
        blockers = resource_actions.drain_pdb_warnings(cluster)
    except Exception as exc:
        return False, f'未能读取 PodDisruptionBudget，无法判断驱逐条件：{exc}', {'error': str(exc)}
    if not blockers:
        return False, '没有阻塞驱逐的 PodDisruptionBudget', {'blockers': []}

    named = [f'{item["namespace"]}/{item["name"]}' for item in blockers if item.get('name')]
    return True, (
        '以下 PodDisruptionBudget 当前不允许驱逐（disruptionsAllowed=0），升级滚动到这些'
        f'工作负载时会停下：{"、".join(named)}。请确认是否需要人工介入。'
    ), {'blockers': blockers}


def check_target_version_artifact(kubeasz_root, deployment, target_version):
    """校验本次使用的 kubeasz 制品是否真的提供升级目标版本。

    参数：`kubeasz_root` 表示本任务解压好的 kubeasz 源码根目录；
        `deployment` 表示 K8sDeployment 实例；`target_version` 表示升级目标版本。
    返回：返回 ``(是否通过, 中文说明, 明细字典)``。
    副作用：读取工作区里的 ezdown 脚本，不修改任何文件。

    kubeasz 的离线包按 ``K8S_BIN_VER`` 下载 Kubernetes 二进制，config.yml 里的 ``K8S_VER``
    只是告诉 ansible 该装哪个版本。两者对不上时 ``ezctl upgrade`` 会在下载阶段失败，
    而那时集群已经被改了一部分——所以这里提前判死。
    """
    version = str(getattr(deployment, 'kubeasz_version', '') or '')
    detail = {'kubeasz_version': version, 'target_version': target_version}
    try:
        variables = compiler.read_kubeasz_variables(kubeasz_root)
    except Exception as exc:
        detail['reason'] = str(exc)
        return True, f'未能读取制品里的版本声明，跳过目标制品检查：{exc}', detail

    binary_version = str(variables.get('K8S_BIN_VER') or '').lstrip('v')
    detail['kubeasz_binary_version'] = binary_version
    if not binary_version:
        return True, '制品里的 ezdown 没有声明 K8S_BIN_VER，跳过目标制品检查', detail
    if compiler.version_matches(target_version, binary_version):
        return True, f'制品提供 Kubernetes v{binary_version}，与升级目标 {target_version} 一致', detail
    return False, (
        f'kubeasz {version} 的制品只能提供 Kubernetes v{binary_version}，与本次的升级目标 '
        f'{target_version} 不是同一个次版本，无法升级。平台是按配置中心的版本映射去取制品的：'
        '升级到某个次版本就会用它对应的那套 kubeasz。请核对配置中心里该版本的 kubeasz 版本号，'
        '并确认那套制品已经同步完成。'
    ), detail


def check_config_compatibility(kubeasz_root, deployment):
    """比对集群现有配置与该 release 的配置模板，列出「模板里有、集群配置里没有」的变量。

    参数：`kubeasz_root` 表示本任务解压好的 kubeasz 源码根目录；`deployment` 表示 K8sDeployment 实例。
    返回：返回 ``(是否有差异, 中文说明, 明细字典)``。
    副作用：读取磁盘与数据库，不修改任何文件。

    升级**不会重新渲染** config.yml：``state_machine.materialize_cluster_config`` 把集群最近一次
    成功任务固化的快照逐字节写回去，只改 ``K8S_VER``（这是刻意的 —— 重新渲染会按当前节点
    重排节点名，kubeasz 会以为集群换了一批机器）。同次版本升级时模板与快照出自同一版
    kubeasz，没有差异；**跨次版本时会用新版本的 playbook 去跑旧版本渲染出来的配置**，
    新模板里的变量在旧配置里就是缺的 —— 这是跨 kubeasz 版本升级最大的未知量，
    所以在这里把它变成一张看得见的清单。

    只警告不失败：kubeasz 的 role 大多在 ``defaults/main.yml`` 里给这些变量留了默认值，
    缺了不等于跑不动；而「缺的这一个是不是必需的」在这里也判断不了 —— 判死会挡住一次
    本来能成的升级（与模块开头「查不出来不当成有问题」的取舍一致）。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    version = str(getattr(deployment, 'kubeasz_version', '') or '')
    detail = {'kubeasz_version': version, 'template_extra': []}
    template_path = Path(kubeasz_root) / compiler.CONFIG_TEMPLATE
    if not template_path.exists():
        return False, f'制品里没有 {compiler.CONFIG_TEMPLATE}，跳过配置模板比对', detail
    cluster = getattr(deployment, 'cluster', None)
    if cluster is None:
        return False, '任务没有关联集群，跳过配置模板比对', detail
    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('-revision').first()
    current_text = str(((revision.snapshot or {}) if revision is not None else {}).get('config') or '')
    if not current_text:
        return False, '集群没有可用的配置版本记录，跳过配置模板比对', detail

    template_keys = set(CONFIG_KEY_PATTERN.findall(template_path.read_text(encoding='utf-8')))
    current_keys = set(CONFIG_KEY_PATTERN.findall(current_text))
    extra = sorted(template_keys - current_keys)
    detail.update({
        'template_extra': extra,
        'template_key_count': len(template_keys),
        'current_key_count': len(current_keys),
    })
    if not extra:
        return False, f'集群现有配置与 kubeasz {version} 的模板没有变量差异', detail
    return True, (
        f'kubeasz {version} 的模板里有 {len(extra)} 个变量是集群现有配置没有的：'
        f'{"、".join(extra)}。升级不会重渲染 config.yml，这些变量会取各自 role 里的默认值；'
        '若本次跨次版本升级涉及你手工调整过的配置项，请对照上游的 example/config.yml 复核一遍。'
    ), detail


def run_upgrade_prechecks(context, deployment, target_version, kubeasz_root):
    """执行升级前的前置检查，硬性失败直接中止升级。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；
        `target_version` 表示升级目标版本；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回 ``{'skew': {...}, 'drain': {...}, 'artifact': {...}, 'config': {...},
        'cluster_state': {...}}``。
    副作用：通过 Kubernetes API 与磁盘读取集群现状；写入任务日志与事件。

    检查顺序按代价从小到大：先读制品声明与配置模板（纯本地），再读集群现状（一次 API 调用），
    最后是两项只警告的检查 —— 它们要给人看的是提醒，不该占用「发现硬性失败」的时间。
    """
    from k8s_cluster.services import probe_cluster

    from .state_machine import TaskAborted

    context.begin_stage('upgrade-precheck', '升级前检查')

    artifact_ok, artifact_message, artifact_detail = check_target_version_artifact(
        kubeasz_root, deployment, target_version,
    )
    _emit(context, 'upgrade_precheck_artifact', '目标制品', artifact_ok, artifact_message, artifact_detail)
    if not artifact_ok:
        raise TaskAborted('upgrade_target_artifact_missing', f'升级前检查未通过：{artifact_message}')

    try:
        cluster_state = probe_cluster(deployment.cluster)
    except Exception as exc:
        raise TaskAborted(
            'cluster_unreachable',
            f'无法连接集群 API 读取升级前的节点状态，已中止升级：{exc}',
        ) from exc

    skew_ok, skew_message, skew_detail = check_version_skew(cluster_state, target_version)
    _emit(context, 'upgrade_precheck_skew', '版本 skew', skew_ok, skew_message, skew_detail)
    if not skew_ok:
        raise TaskAborted('upgrade_version_skew', f'升级前检查未通过：{skew_message}')

    drain_risky, drain_message, drain_detail = check_drain_conditions(deployment.cluster)
    # 驱逐条件只警告：PDB 挡住的是「安全驱逐」，是否继续升级需要人来判断（规划 10.4）。
    _emit(context, 'upgrade_precheck_drain', '驱逐条件', not drain_risky, drain_message, drain_detail)

    # 配置模板差异同样只警告，而且放在最后：跨次版本升级才可能有差异，它要给的是
    # 一份供人对照的清单，不该在最前面拦住一次本来能成的升级。
    config_gap, config_message, config_detail = check_config_compatibility(kubeasz_root, deployment)
    _emit(context, 'upgrade_precheck_config', '配置模板差异', not config_gap, config_message, config_detail)

    context.complete_stage('upgrade-precheck')
    return {
        'skew': skew_detail,
        'drain': drain_detail,
        'artifact': artifact_detail,
        'config': config_detail,
        'cluster_state': {
            'kube_version': cluster_state.get('kube_version'),
            'node_summary': cluster_state.get('node_summary'),
        },
        'checked_at': timezone.now().isoformat(),
    }


def _emit(context, event_code, title, ok, message, detail):
    """把一项前置检查的结论同时写进日志与事件流。

    参数：`context` 表示任务上下文；`event_code` 表示事件代码；`title` 表示中文标题；
        `ok` 表示该项是否通过；`message` 表示中文说明；`detail` 表示结构化明细。
    返回：无显式返回值。
    副作用：追加任务日志与事件记录。
    """
    level = 'info' if ok else 'warn'
    context.log('system', f'[{title}] {message}')
    context.event(
        event_code, f'{title}：{message}', level=level, detail=detail, stage='upgrade-precheck',
    )
