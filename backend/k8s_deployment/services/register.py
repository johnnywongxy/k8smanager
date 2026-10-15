"""部署成功后的回填：把一次任务的结果固化进集群、配置版本与健康记录。

kubeasz 跑完只留下工作区里的一堆文件，平台侧真正「可以用」的集群需要额外三件事：

1. **集群记录**：``K8sCluster.origin=deployed``，运维操作只对部署型集群开放（规划 6.1）。
   平台自己生成的 kubeconfig 必须走既有的 Fernet 加密通道落库（``k8s_cluster.crypto``），
   明文只在本函数的内存里存在一次。
2. **配置版本**：每次真正落地一份配置就追加一条 ``K8sClusterConfigRevision``。后续的
   扩容、升级、恢复都读最新 revision，并在开工前比对任务提交时记录的版本号，
   不一致就以 ``stale_cluster_revision`` 失败（规划 6.1），避免用旧配置覆盖新状态。
3. **健康记录**：部署完成后探测一次 API，把结果写进 ``K8sClusterHealthCheck``。

**回填失败不回滚集群**：集群已经真实建出来了，这里只是平台侧的记账。任何一步失败都要
留下明确原因，而不是把已存在的集群当作不存在。

这条承诺对事务的划分有硬要求：**集群与配置版本的写入必须自己提交，不能和后面的联网体检
共用一个事务**。2026-10-08 的实例（94 上的 `jd-k8s`）就是在这里栽的 —— 整个函数被一个
``@transaction.atomic`` 罩住，事务里的第一条语句是 ``ensure_deployed_cluster`` 的
``filter(slug=...)``（读），随后的 INSERT 要把它升级成写锁；库是 **WAL 模式**，只要在
这份读快照之后有别的连接提交过，升级就会**立刻**返回 ``database is locked``（
``busy_timeout`` 对「升级冲突」不生效，这是 WAL 的既定行为，不是 bug）。结果整个事务回滚，
集群记录与配置版本一起消失，集群接入列表里什么都没有，任务只留下一句
「未预期的错误：database is locked」，而节点上的集群其实装得好好的。

所以这里的规矩是：**读在事务外做完，写事务尽可能短且只写，联网探测一律在事务外**；
再叠一层对 ``database is locked`` 的重试兜底 —— 它是瞬时冲突，等一下就能过。
"""

import time

from django.db import OperationalError, transaction
from django.utils import timezone

from . import diagnostics, paths

# ``database is locked`` 是瞬时冲突：重试就能过。次数不必多（每次冲突都是毫秒级的锁升级
# 失败，退避 0.2/0.4/0.8 秒足够让另一个写事务提交完），但也不能没有 —— 没有它的时候，
# 一次冲突就等于整个集群登记丢失。
DB_LOCK_ATTEMPTS = 4
DB_LOCK_BACKOFF_SECONDS = 0.2


def _is_database_locked(exc):
    """判断一个异常是不是 SQLite 的写锁冲突。

    参数：`exc` 表示捕获到的异常。
    返回：返回 True 或 False。
    副作用：只读异常文本，不修改数据。
    """
    return 'database is locked' in str(exc).lower()


def run_with_lock_retry(action, what):
    """执行一个写操作，遇到 SQLite 写锁冲突就退避重试。

    参数：`action` 表示零参可调用对象（每次重试都会重新执行）；`what` 表示中文描述，
        用于重试耗尽后的报错文案。
    返回：返回 ``action()`` 的返回值。
    副作用：执行 `action`；失败时可能休眠退避。

    调用方传进来的 `action` 必须自己是一个**完整的事务单元**（一般是
    ``with transaction.atomic():`` 包好的一段），否则重试时会接着一个已经失败的事务往下走。
    """
    for attempt in range(1, DB_LOCK_ATTEMPTS + 1):
        try:
            return action()
        except OperationalError as exc:
            if not _is_database_locked(exc):
                raise
            if attempt >= DB_LOCK_ATTEMPTS:
                raise RegisterError(
                    f'{what}时数据库被其它进程占用（{exc}），已重试 {DB_LOCK_ATTEMPTS} 次仍未成功。'
                    '集群本身已经装好，稍后重试本任务即可补登记。',
                ) from exc
            time.sleep(DB_LOCK_BACKOFF_SECONDS * (2 ** (attempt - 1)))

# 生成 kubeconfig 的文件名，kubeasz 固定在集群目录下产出这个文件。
GENERATED_KUBECONFIG_NAME = 'kubectl.kubeconfig'
# 配置版本快照里保留的节点字段，避免把整张节点表塞进 JSON。
SNAPSHOT_NODE_FIELDS = ('name', 'ip_address', 'ssh_port', 'ssh_user', 'roles', 'node_name')
# 「新建/重装」类任务。与 ``state_machine.FULL_INSTALL_TASKS`` 同义（那边是执行路径的分类，
# 这里只用来判断「这次任务该不该给集群取名」）。register 被 state_machine 导入，
# 反向导入会成环，所以这份清单在这里另立一份。
FULL_INSTALL_TASK_TYPES = ('deploy_aio', 'deploy_ha')


class RegisterError(Exception):
    """回填失败，携带可直接展示给用户的中文原因。"""


def read_generated_kubeconfig(kubeasz_root, slug):
    """读取 kubeasz 生成的集群 kubeconfig。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回 kubeconfig 文本；文件不存在时返回空串。
    副作用：只读取磁盘文件，不修改内容。
    """
    target = paths.cluster_dir(kubeasz_root, slug) / GENERATED_KUBECONFIG_NAME
    if not target.is_file():
        return ''
    try:
        return target.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''


def stage_kubeconfig(kubeasz_root, slug, workspace):
    """把集群 kubeconfig 复制到任务工作区根目录。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识；`workspace` 表示任务工作区。
    返回：返回复制后的 Path；源文件不存在时返回 None。
    副作用：在工作区根目录写入 ``kubeconfig`` 并收紧到 0600。
    """
    source = paths.cluster_dir(kubeasz_root, slug) / GENERATED_KUBECONFIG_NAME
    if not source.is_file():
        return None
    target = paths.ensure_dir(workspace) / 'kubeconfig'
    target.write_text(source.read_text(encoding='utf-8', errors='replace'), encoding='utf-8')
    # 工作区里的 kubectl 一律显式指定这个文件，避免动到平台机的 ~/.kube/config（规划 6.3）。
    return paths.secure_file(target)


def node_snapshot(nodes):
    """把节点列表整理成可入库的快照结构。

    参数：`nodes` 表示 K8sDeploymentNode 的可迭代对象。
    返回：返回节点字典列表。
    副作用：只读取对象属性，不修改持久化数据。
    """
    result = []
    for item in nodes:
        entry = {field: getattr(item, field, None) for field in SNAPSHOT_NODE_FIELDS}
        entry['ip_address'] = str(entry.get('ip_address') or '')
        result.append(entry)
    return result


def merged_node_snapshot(cluster, nodes):
    """把本次任务的节点并进集群当前拓扑，得到一份完整的节点快照。

    参数：`cluster` 表示 K8sCluster 实例；`nodes` 表示本次任务的 K8sDeploymentNode 可迭代对象。
    返回：返回合并后的节点字典列表。
    副作用：读取配置版本表，不修改数据。

    这份快照是后续运维任务判断集群拓扑的唯一来源 —— 升级要滚动哪些节点、清理要清哪些节点、
    还能不能再加 Master，读的都是它 —— 所以它必须是**集群的全量节点**，而不是**本次任务
    碰到的那几台**。新建任务手里就是全量，两种口径没有区别；扩容任务手里只有新加的那一台
    （``state_machine.run_add_node`` 开头就断言了 ``len(nodes) == 1``），直接覆盖会让集群在
    快照里缩水成一台，原有的三台 master 从此消失：先是「添加 Master」被判成拓扑不够 HA 而
    置灰，接着升级与清理都会漏掉那三台机器。

    合并规则：以集群当前快照为底，用本次任务的条目按 IP 覆盖同一个条目（角色可能变了，
    例如某台机器在新任务里兼任了负载均衡），本次没提到的节点原样保留、位置也不动 ——
    新加入的追加在末尾，顺序与它们加入集群的先后一致。平台没有摘除节点的运维操作，
    所以这里不必处理「节点被移除」的情况。
    """
    def address_of(item):
        """取出一条快照或节点条目的 IP 地址文本，取不到时返回空串。"""
        return str((item or {}).get('ip_address') or '')

    current = [
        item for item in (latest_snapshot(cluster).get('nodes') or []) if isinstance(item, dict)
    ]
    fresh = node_snapshot(nodes)
    incoming = {address_of(item): item for item in fresh if address_of(item)}
    known = {address_of(item) for item in current}

    merged = []
    for item in current:
        address = address_of(item)
        merged.append((incoming.get(address) if address else None) or item)
    merged.extend(item for item in fresh if address_of(item) not in known)
    return merged


def next_revision(cluster):
    """计算集群的下一个配置版本号。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回当前最大版本号加一，没有历史时返回 1。
    副作用：执行一次数据库聚合查询，不修改数据。
    """
    from django.db.models import Max

    from k8s_cluster.models import K8sClusterConfigRevision

    current = K8sClusterConfigRevision.objects.filter(cluster=cluster).aggregate(value=Max('revision'))['value']
    return int(current or 0) + 1


def current_revision(cluster):
    """返回集群当前生效的配置版本号。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回最大版本号，没有历史时返回 0。
    副作用：执行一次数据库查询，不修改数据。
    """
    from django.db.models import Max

    from k8s_cluster.models import K8sClusterConfigRevision

    if cluster is None:
        return 0
    current = K8sClusterConfigRevision.objects.filter(cluster=cluster).aggregate(value=Max('revision'))['value']
    return int(current or 0)


def append_config_revision(cluster, deployment, compile_result, nodes, note=''):
    """追加一条集群配置版本记录。

    参数：`cluster` 表示 K8sCluster 实例；`deployment` 表示产生配置的部署任务；
        `compile_result` 表示编译器返回的结果字典；`nodes` 表示节点快照；
        `note` 表示变更说明。
    返回：返回新建的 K8sClusterConfigRevision。
    副作用：向配置版本表写入一行，并把任务主键回填到 deployment。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    revision = next_revision(cluster)
    snapshot = {
        'schema': '1',
        'cluster_slug': deployment.cluster_slug,
        'kubernetes_version': deployment.kubernetes_version,
        'kubeasz_version': deployment.kubeasz_version,
        'topology_mode': deployment.topology_mode,
        # 操作系统也一并固化：后续的扩容、升级、恢复都要拿它做预检比对与审计留痕，
        # 而平台侧没有别的地方记录目标集群装的是什么系统。
        'os_type': deployment.os_type,
        'os_version': deployment.os_version,
        'runtime': deployment.runtime,
        'network_plugin': deployment.cni_plugin,
        # 网段与 ApiServer 端口是集群级事实，一旦定下就不能再改。后续的扩容、升级都要拿
        # 它们做预检（``preflight._check_cidr`` 读不到就直接判失败），而这两种任务的形式里
        # 根本没有给用户填的地方 —— 不在这里记下来，运维任务就只能凭空编一个值。
        'pod_cidr': deployment.pod_cidr,
        'service_cidr': deployment.service_cidr,
        'api_server_port': deployment.api_server_port,
        # kube-proxy 模式同理：它是集群的既成事实，运维任务要按它决定预检探什么，而升级还
        # 允许改它（3.7.0 的模板不再支持 ipvs）。结构化的键从这里开始写入，读的时候
        # ``cluster_proxy_mode`` 还会退回解析 hosts 文本，老快照照样能读出真实模式。
        'kube_proxy_mode': deployment.kube_proxy_mode,
        # 不是 ``node_snapshot(nodes)``：扩容任务手里只有新加的那一台，直接写会把集群拓扑
        # 截断成「集群里只有这台新机器」，理由见 ``merged_node_snapshot``。
        'nodes': merged_node_snapshot(cluster, nodes),
        'hosts': compile_result.get('hosts_text', ''),
        'config': compile_result.get('config_text', ''),
        'template': compile_result.get('template', ''),
        'resolved_versions': compile_result.get('resolved_versions', {}),
    }
    return K8sClusterConfigRevision.objects.create(
        organization=cluster.organization,
        cluster=cluster,
        revision=revision,
        source_deployment_id=deployment.pk,
        snapshot=snapshot,
        config_hash=str(compile_result.get('config_hash') or '')[:64],
        note=str(note or f'{deployment.get_task_type_display()}（任务 #{deployment.pk}）')[:240],
    )


def latest_snapshot(cluster):
    """返回集群最新配置版本里的快照字典。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回快照字典，没有历史时返回空字典。
    副作用：读取数据库，不修改数据。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    if cluster is None:
        return {}
    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('-revision').first()
    return dict(revision.snapshot or {}) if revision is not None else {}


def cluster_proxy_mode(cluster):
    """从配置版本快照里取回集群在用的 kube-proxy 模式。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回模式文本（小写）；读不到时返回空串。
    副作用：读取数据库，不修改数据。

    沿用 ``cluster_network_facts`` 的两级读法：先看快照的结构化键，再退回解析 inventory
    文本里的 ``PROXY_MODE``（编译 hosts 时一定写在 ``all:vars`` 里）。运维任务必须用集群
    **真实**的模式，而不是一个默认值：模式决定预检要探什么（ipvs 探内核模块、nftables 探
    内核与 nft），拿默认值去探会在一台根本没开 ipvs 的机器上报「缺少 ip_vs 模块」，
    也会让跨次版本升级误判成「目标 kubeasz 不支持集群当前的模式」。
    """
    from . import compiler

    snapshot = latest_snapshot(cluster)
    mode = str(snapshot.get('kube_proxy_mode') or '').strip().lower()
    if mode:
        return mode
    hosts_text = str(snapshot.get('hosts') or '')
    return str(compiler.inventory_variable(hosts_text, 'PROXY_MODE') or '').strip().lower()


def cluster_os_hint(cluster):
    """从配置版本快照里取回集群的操作系统信息。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'os_type':.., 'os_version':..}``，快照里没有时两项为空串。
    副作用：读取数据库，不修改数据。

    扩容、升级、恢复都要用它对目标节点做操作系统比对。历史上（本条字段加入之前）
    部署的集群快照里没有这项，此时返回空，由调用方决定是否要求用户显式指定。
    """
    snapshot = latest_snapshot(cluster)
    return {
        'os_type': str(snapshot.get('os_type') or ''),
        'os_version': str(snapshot.get('os_version') or ''),
    }


def cluster_network_facts(cluster):
    """从配置版本快照里取回集群的网络契约：两个网段与 ApiServer 端口。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'pod_cidr': str, 'service_cidr': str, 'api_server_port': int}``，
        读不到的项为空串 / 0。
    副作用：读取数据库，不修改数据。

    扩容与升级必须把集群真实的网段与端口落到新任务上：这三个值在集群建好那一刻就定了，
    运维表单里没有输入框，用户无从填起。平台侧的记录顺序是：

    1. 快照里的结构化键（本轮开始写入）；
    2. 退回解析快照里的 inventory 文本 —— 老快照没有那三个键，但编译 hosts 时
       ``CLUSTER_CIDR`` / ``SERVICE_CIDR`` / ``SECURE_PORT`` 一定写在 ``all:vars`` 里，
       集群能跑起来就说明这份文本里的值是真的。

    第 2 条不是权宜之计：94 上部署的第一批集群就是这样读到真实值的。
    """
    from . import compiler

    snapshot = latest_snapshot(cluster)
    hosts_text = str(snapshot.get('hosts') or '')

    def from_inventory(key):
        """从 inventory 文本里取一个变量。

        参数：`key` 表示 inventory 变量名。
        返回：返回变量值；没有这一行时返回空串。
        副作用：只做纯计算，不修改数据。
        """
        return str(compiler.inventory_variable(hosts_text, key) or '').strip()

    pod_cidr = str(snapshot.get('pod_cidr') or '').strip() or from_inventory('CLUSTER_CIDR')
    service_cidr = str(snapshot.get('service_cidr') or '').strip() or from_inventory('SERVICE_CIDR')
    try:
        api_server_port = int(snapshot.get('api_server_port') or from_inventory('SECURE_PORT') or 0)
    except (TypeError, ValueError):
        api_server_port = 0
    return {
        'pod_cidr': pod_cidr,
        'service_cidr': service_cidr,
        'api_server_port': api_server_port,
    }


def ensure_deployed_cluster(deployment, kubeasz_root=None):
    """创建或更新任务对应的部署型集群记录。

    参数：`deployment` 表示 K8sDeployment 实例；`kubeasz_root` 表示 kubeasz 源码根目录，用于读取 kubeconfig。
    返回：返回 K8sCluster 实例。
    副作用：向集群表写入或更新一行，可能把 kubeconfig 加密后写入凭据表。
    """
    from k8s_cluster.models import K8sCluster

    from . import compiler

    cluster = deployment.cluster
    if cluster is None:
        cluster = K8sCluster.objects.filter(slug=deployment.cluster_slug).first()
    if cluster is None:
        cluster = K8sCluster(slug=deployment.cluster_slug)

    cluster.organization = cluster.organization or deployment.organization
    # 集群名只在「新建/重装」时跟着任务名走。运维任务的 display_name 是 ``cluster_ops`` 拼出来的
    # 「<集群名> · 添加 Node」这类任务标题，拿它覆盖会把集群改名成一次任务的名字，而且每做一次
    # 运维就再叠一层后缀（真实故障：94 上的集群被改成了「k8s-cluster · 添加 Node」）。
    if deployment.task_type in FULL_INSTALL_TASK_TYPES or not cluster.name:
        cluster.name = deployment.display_name or cluster.name or deployment.cluster_slug
    cluster.slug = deployment.cluster_slug
    cluster.origin = 'deployed'
    cluster.cluster_name = cluster.cluster_name or deployment.cluster_slug
    cluster.context_name = cluster.context_name or f'context-{deployment.cluster_slug}'
    cluster.auth_type = 'kubeconfig'
    cluster.kube_version = deployment.kubernetes_version or cluster.kube_version
    cluster.network_plugin = deployment.cni_plugin or cluster.network_plugin
    # 只数集群节点：负载均衡机器与集群节点同表，但它们不出现在 ``kubectl get node`` 里，
    # 算进去会让集群列表上的节点数与用户自己数出来的对不上。
    cluster.node_count = deployment.cluster_node_count
    # 高可用入口地址与模式。取值口径与编译器一致（模式为 none 时地址是空串），后面用 ``or``
    # 保住旧值 —— 扩容/升级这类运维任务不继承这个模式，净效果就是把已有的入口原样留下。
    cluster.api_server_vip = compiler.lb_vip(deployment) or cluster.api_server_vip
    mode = compiler.lb_mode(deployment)
    if mode != compiler.LB_MODE_NONE:
        cluster.lb_mode = mode
    cluster.remark = deployment.cluster_description or cluster.remark
    if cluster.status != 'disabled':
        cluster.status = 'configured'
    cluster.save()

    if kubeasz_root:
        raw_kubeconfig = read_generated_kubeconfig(kubeasz_root, deployment.cluster_slug)
        if raw_kubeconfig:
            backfill_kubeconfig(cluster, raw_kubeconfig)

    if deployment.cluster_id != cluster.pk:
        deployment.cluster = cluster
        deployment.save(update_fields=['cluster', 'updated_at'])
    return cluster


def backfill_kubeconfig(cluster, raw_kubeconfig, source_path=''):
    """把平台自己生成的 kubeconfig 加密后写入集群凭据。

    参数：`cluster` 表示 K8sCluster 实例；`raw_kubeconfig` 表示 kubeconfig 原文；
        `source_path` 表示来源说明，写进凭据表便于追溯。
    返回：返回更新的 K8sClusterCredential。
    副作用：向凭据表写入密文；明文不落盘、不进日志。
    """
    from k8s_cluster import crypto
    from k8s_cluster.models import K8sClusterCredential
    from k8s_cluster.services import kubeconfig_fingerprint, parse_kubeconfig_text

    text = str(raw_kubeconfig or '').strip()
    if not text:
        raise RegisterError('kubeasz 没有生成 kubeconfig，无法回填集群凭据。')
    try:
        metadata = parse_kubeconfig_text(text)
    except ValueError as exc:
        raise RegisterError(f'平台的 kubeconfig 无法解析：{exc}') from exc

    credential, _created = K8sClusterCredential.objects.get_or_create(cluster=cluster)
    credential.encrypted_kubeconfig = crypto.encrypt_secret(text)
    credential.encrypted_client_certificate = crypto.encrypt_secret(metadata['client_certificate_data'])
    credential.encrypted_client_key = crypto.encrypt_secret(metadata['client_key_data'])
    credential.encrypted_certificate_authority = crypto.encrypt_secret(metadata['certificate_authority_data'])
    credential.encrypted_token = crypto.encrypt_secret(metadata['token'])
    credential.source_path = str(source_path or f'k8s-deployment/{cluster.slug}')[:260]
    credential.fingerprint = kubeconfig_fingerprint(text)
    credential.rotated_at = timezone.now()
    credential.save()

    if metadata.get('api_server'):
        cluster.api_server = metadata['api_server']
        cluster.internal_api_server = cluster.internal_api_server or metadata['api_server']
    if metadata.get('cluster_name'):
        cluster.cluster_name = metadata['cluster_name']
    if metadata.get('context_name'):
        cluster.context_name = metadata['context_name']
    if metadata.get('user_name'):
        cluster.user_name = metadata['user_name']
    cluster.save()
    return credential


def record_test_result(cluster, status, message):
    """把一次连通性检查的结论记到集群档案的「最近检测」三个字段上。

    参数：`cluster` 表示 K8sCluster 实例；`status` 表示检测状态；`message` 表示中文结论。
    返回：无显式返回值。
    副作用：只改内存里的对象，由调用方随它那一次 ``save()`` 一起落库。

    只写字段、**不**像 ``K8sCluster.mark_test_result`` 那样顺手再加一条健康记录：
    调用方（``run_health_check``）刚刚自己写了那一行，再补一条会让人看到两条一模一样的记录。
    手工「连接测试」那条路没有这个问题，于是两条路各写各的、字段口径保持一致。

    这三个字段就是集群列表里「最近检测」那一列读的东西。漏写它们时，一次刚做完的健康检查
    在列表上会显示成「最近检测 -」（2026-10-12 的 JD-k8s 就是这样：状态是「可用」、
    最近检测却是空的，与「部署成功却好像哪儿不对」的观感叠在一起）。
    """
    cluster.last_test_status = str(status or '')
    cluster.last_test_message = str(message or '')[:240]
    cluster.last_tested_at = timezone.now()


def run_health_check(cluster, timeout_detail=None):
    """对新集群执行一次连通性检查并记录结果。

    参数：`cluster` 表示 K8sCluster 实例；`timeout_detail` 表示额外的说明，写进检查详情。
    返回：返回 ``{'ok': bool, 'message': str, 'detail': dict}``。
    副作用：向 Kubernetes API 发起请求，并向健康检查表写入一行。
    """
    from k8s_cluster.models import K8sClusterHealthCheck
    from k8s_cluster.services import probe_cluster

    try:
        result = probe_cluster(cluster)
    except Exception as exc:
        message = f'集群连通性检查失败：{exc}'
        run_with_lock_retry(
            lambda: K8sClusterHealthCheck.objects.create(
                organization=cluster.organization,
                cluster=cluster,
                status='unavailable',
                message=message[:240],
                detail={'error': str(exc), 'extra': timeout_detail or {}},
            ),
            '写入集群连通性检查记录',
        )
        # 失败同样落「最近检测」：这次探测确实发生了，列表上不能还留着上一次的结论。
        record_test_result(cluster, 'unavailable', message)
        run_with_lock_retry(cluster.save, '更新集群检测结果')
        return {'ok': False, 'message': message, 'detail': {'error': str(exc)}}

    detail = dict(result)
    if timeout_detail:
        detail['extra'] = timeout_detail
    # 把没有 Ready 的节点名字写进结论：只说「节点 3 个」无法回答「哪一台没起来」（规划 10.2）。
    not_ready = (result.get('node_summary') or {}).get('not_ready') or []
    message = str(result.get('message') or '')
    if not_ready:
        message = f'{message}；未就绪节点：{"、".join(not_ready)}'
    run_with_lock_retry(
        lambda: K8sClusterHealthCheck.objects.create(
            organization=cluster.organization,
            cluster=cluster,
            status=result.get('status', 'available'),
            latency_ms=int(result.get('latency_ms') or 0),
            message=message[:240],
            detail=detail,
        ),
        '写入集群连通性检查记录',
    )
    cluster.kube_version = result.get('kube_version') or cluster.kube_version
    cluster.node_count = result.get('node_count') or cluster.node_count
    cluster.runtime_version = result.get('runtime_version') or cluster.runtime_version
    cluster.network_plugin = result.get('network_plugin') or cluster.network_plugin
    record_test_result(cluster, result.get('status', 'available'), message)
    cluster.last_synced_at = timezone.now()
    cluster.status = result.get('status', cluster.status)
    run_with_lock_retry(cluster.save, '更新集群档案')
    return {'ok': True, 'message': str(result.get('message') or ''), 'detail': detail}


def sync_machine_assignments(slug, nodes):
    """把节点快照里的机器归属同步成集群标识。

    参数：`slug` 表示集群标识；`nodes` 表示 K8sDeploymentNode 的可迭代对象。
    返回：返回被更新的机器数量。
    副作用：更新机器表的 ``cluster_name`` 字段。
    """
    from k8s_machine.models import Machine

    machine_ids = [item.machine_id for item in nodes if item.machine_id]
    if not machine_ids:
        return 0
    return Machine.objects.filter(pk__in=machine_ids).update(cluster_name=slug)


def register_deployment_result(deployment, compile_result, nodes, kubeasz_root=None, note=''):
    """把一次成功部署的结果回填成集群、配置版本与健康记录。

    参数：`deployment` 表示 K8sDeployment 实例；`compile_result` 表示编译结果（含 hashes）；
        `nodes` 表示节点快照；`kubeasz_root` 表示 kubeasz 源码根目录；`note` 表示配置版本说明。
    返回：返回 ``{'cluster_id':.., 'cluster_name':.., 'revision':.., 'kubeconfig': bool,
        'health': {...}}``，供任务结果摘要与前端展示。
    副作用：写入集群、凭据、配置版本、健康检查表，并更新机器归属。

    事务分两段（理由见模块说明）：**集群与配置版本先各自提交**，联网体检随后在事务外进行。
    这样即使体检或它的写库失败，已经建出来的集群也不会从列表里消失。
    """

    def write_backfill():
        """在一个短事务里写完集群、凭据、配置版本与机器归属。

        参数：无。
        返回：返回 ``(cluster, revision)``。
        副作用：向集群、凭据、配置版本与机器表写入。

        每次重试都先从库里重读 ``cluster`` 字段：上一轮若是在事务中途被写锁冲突打断的，
        ``deployment`` 与 ``cluster`` 在内存里指向的行已经回滚，不清掉会拿一个不存在的
        主键继续往下写。
        """
        deployment.refresh_from_db(fields=['cluster'])
        with transaction.atomic():
            cluster = ensure_deployed_cluster(deployment, kubeasz_root=kubeasz_root)
            revision = append_config_revision(cluster, deployment, compile_result, nodes, note=note)
            sync_machine_assignments(deployment.cluster_slug, nodes)
        return cluster, revision

    cluster, revision = run_with_lock_retry(write_backfill, '登记集群与配置版本')
    health = run_health_check(
        cluster,
        timeout_detail={'deployment_id': deployment.pk, 'revision': revision.revision},
    )
    return {
        'cluster_id': cluster.pk,
        'cluster_name': cluster.name,
        'cluster_slug': cluster.slug,
        'revision': revision.revision,
        'config_hash': revision.config_hash,
        'kubeconfig': bool(read_generated_kubeconfig(kubeasz_root, deployment.cluster_slug)) if kubeasz_root else False,
        'health': health,
    }


def capture_config_texts(kubeasz_root, slug):
    """读取工作区里最终生效的 hosts 与 config.yml 文本。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回 ``{'hosts_text':.., 'config_text':..}``，缺失的项为空串。
    副作用：只读取磁盘文件，不修改内容。
    """
    hosts = paths.hosts_path(kubeasz_root, slug)
    config = paths.config_path(kubeasz_root, slug)
    return {
        'hosts_text': hosts.read_text(encoding='utf-8', errors='replace') if hosts.is_file() else '',
        'config_text': config.read_text(encoding='utf-8', errors='replace') if config.is_file() else '',
    }


def attach_config_texts(compile_result, kubeasz_root, slug):
    """把工作区里最终生效的配置文本补进编译结果。

    参数：`compile_result` 表示编译器返回的结果字典；`kubeasz_root` 表示 kubeasz 源码根目录；
        `slug` 表示集群标识。
    返回：返回补全后的结果字典。
    副作用：只读取磁盘文件，不修改编译结果以外的数据。

    编译器返回的 ``hosts_text``/``config_text`` 在某些调用路径上并不会带上完整正文，
    因此写入配置版本前统一从这里补齐一次。
    """
    if not kubeasz_root:
        return compile_result
    texts = capture_config_texts(kubeasz_root, slug)
    merged = dict(compile_result or {})
    merged.setdefault('hosts_text', texts['hosts_text'])
    merged.setdefault('config_text', texts['config_text'])
    if not merged.get('hosts_text'):
        merged['hosts_text'] = texts['hosts_text']
    if not merged.get('config_text'):
        merged['config_text'] = texts['config_text']
    return merged


def capture_diagnostics(deployment):
    """为任务生成诊断包。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回诊断包路径文本；生成失败时返回空串。
    副作用：在工作区写入诊断包文件；失败只记录不抛出，避免掩盖真正的部署错误。
    """
    try:
        package = diagnostics.collect_diagnostics(deployment)
    except Exception:
        return ''
    return str(package or '')
