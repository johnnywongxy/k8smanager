"""向导的「完整预检」：生成集群配置并检查全部目标节点，但不执行 Ansible、不落正式任务。

规划 9（``k8s.txt`` 第 199 行）要求 dry-run「生成配置并完成全部预检，不执行 Ansible、不落正式
部署任务」。配置生成与预检都需要一个任务工作区——kubeasz 源码要解压到某处、``clusters/<slug>/``
要落盘，而现有的制品与编译器 API 都是按任务主键组织的。因此这里的做法是**临时**写一条任务记录：

1. 建一条 ``status='draft'`` 的任务，快照里带 ``dry_run`` 标记，用它跑完制品准备、配置编译与预检；
2. 跑完（无论成功失败）把它连同节点快照、制品使用记录一起删掉，工作区目录也一并清掉。

于是任务列表、审计与集群记录里都不会留下任何痕迹，而 OSS 制品缓存（按内容哈希寻址）会保留下来，
用户真正提交时直接命中缓存，不会重新下载一遍。

刻意**不做**的三件事：
- 一次 ``ezctl``/ansible 都不调用（预检只跑只读命令，也不会安装平台公钥）；
- 不登记集群、不写配置版本、不写健康检查记录；
- 不向目标机器写入任何东西。
"""

import logging
import uuid
from pathlib import Path

from django.db import IntegrityError

from ..models import K8sDeployment, K8sDeploymentNode
from . import artifacts, cluster_ops, compiler, paths, preflight, state_machine

logger = logging.getLogger(__name__)

# 快照里的标记：清洗任务时据此识别「这是预检留下的临时记录」。
DRY_RUN_MARKER = 'dry_run'


class DryRunError(Exception):
    """完整预检无法完成，携带可直接展示给用户的中文原因。"""


def run_deep_preflight(values, node_payloads):
    """生成集群配置并完成全部预检。

    参数：`values` 表示向导提交体的校验结果；`node_payloads` 表示 ``_load_node_payloads``
        产出的节点结构（含 ``machine`` 与 ``roles``）。
    返回：返回 ``{'ok', 'checks', 'hard_failures', 'summary', 'config'}``。
    副作用：临时写入并删除一条任务记录与它的节点快照；解压 kubeasz 源码到临时工作区（用完删除）；
        与目标节点建立只读 SSH 连接。

    ``values`` 里已经含有 ``slug``（用户填的或留空的）。用户留空时这里要用与正式创建同一套
    派生规则算一个标识，否则生成出来的 ``CLUSTER_NAME`` 与真正部署时的那个不一致，
    这份「预览」就失去意义了。
    """
    from ..serializers import derive_slug

    slug = str(values.get('slug') or '') or derive_slug(values['display_name'])
    deployment = _open_transient_task(values, slug)
    try:
        _write_node_snapshots(deployment, node_payloads)
        nodes = list(deployment.nodes.select_related('machine').order_by('id'))
        if not nodes:
            raise DryRunError('没有可用于预检的节点。')
        credentials = state_machine.node_credentials(nodes)
        # 预检用的是用户真实填写的集群标识，而编译会写 clusters/<标识>/。若共用正式目录，
        # 一次预览就会覆盖正在运行的集群的 hosts 与 config.yml，所以给它一份独立的临时目录。
        root = artifacts.ensure_kubeasz_source(
            deployment.kubeasz_version,
            deployment=deployment,
            root=paths.dry_run_root(deployment.pk),
        )
        compiled = compiler.compile_cluster(
            deployment, nodes, credentials, kubeasz_root=root,
        )
        result = preflight.run_preflight(deployment, nodes, credentials)
        return {
            'ok': result['ok'],
            'checks': result['checks'],
            'hard_failures': result['hard_failures'],
            'summary': result['summary'],
            'config': _config_payload(compiled),
        }
    finally:
        _discard_transient_task(deployment)


def _open_transient_task(values, slug):
    """写入一条用完即删的临时任务记录。

    参数：`values` 表示向导提交体的校验结果；`slug` 表示本次预览要使用的集群标识。
    返回：返回新建的 K8sDeployment。
    副作用：向任务表写入一条记录；若该标识正被另一次同名预检占用，会换一个内部标识。

    任务表上的 ``slug`` 是全局唯一的，而这条记录只活一次预检的时间。为了让**生成出来的配置**
    仍然带着用户真正会得到的 ``CLUSTER_NAME``，这里把记录存成内部标识，然后把内存里的 ``slug``
    改回真实标识——``cluster_slug`` 与编译器读的都是内存字段，盘上那条记录从头到尾不会被更新。
    这样既不会和正式任务抢标识，也不会出现「预检通过、提交时说标识被占用」的假冲突。
    """
    internal = f'dryrun-{uuid.uuid4().hex[:12]}'
    payload = {
        key: value for key, value in values.items()
        if key in cluster_ops.TASK_FIELD_NAMES
    }
    payload.pop('slug', None)
    payload.pop('nodes', None)
    try:
        deployment = K8sDeployment.objects.create(
            status='draft',
            slug=internal,
            request_snapshot={DRY_RUN_MARKER: True, 'submitted': dict(values)},
            **payload,
        )
    except IntegrityError as exc:  # pragma: no cover - 只有在内部标识撞车时才会走到
        logger.exception('临时预检任务创建失败')
        raise DryRunError('预检任务无法创建，请稍后重试。') from exc

    deployment.slug = slug
    return deployment


def _write_node_snapshots(deployment, node_payloads):
    """把向导里选中的机器写成临时任务的节点快照。

    参数：`deployment` 表示临时任务；`node_payloads` 表示节点结构。
    返回：无显式返回值。
    副作用：向节点快照表写入记录。

    快照与正式创建时同构：预检看到的就是真正执行时会看到的那份节点清单。
    """
    for item in node_payloads:
        machine = item['machine']
        K8sDeploymentNode.objects.create(
            deployment=deployment,
            machine=machine,
            name=machine.name,
            ip_address=str(machine.ip_address),
            ssh_port=machine.ssh_port,
            ssh_user=machine.ssh_user,
            roles=item['roles'],
            node_name=item.get('node_name', ''),
        )


def _discard_transient_task(deployment):
    """删除临时任务及其工作区。

    参数：`deployment` 表示临时任务。
    返回：无显式返回值。
    副作用：删除任务（级联删除节点快照与制品使用记录）与 ``runs/<task-id>``、预检专用目录。

    制品缓存按内容哈希单独存放，因此不会被一起删掉：用户确认后真正开始部署时会直接命中缓存。
    """
    for target in (paths.workspace_root(deployment.pk), paths.dry_run_root(deployment.pk)):
        try:
            paths.remove_tree(target)
        except Exception:  # pragma: no cover - 清理失败不该影响预检结论
            logger.warning('临时预检目录清理失败：%s', target, exc_info=True)
    try:
        K8sDeployment.objects.filter(pk=deployment.pk).delete()
    except Exception:  # pragma: no cover - 同上
        logger.warning('临时预检任务删除失败：%s', deployment.pk, exc_info=True)


def _config_payload(compiled):
    """挑出前端要展示的编译结果。

    参数：`compiled` 表示 ``compiler.compile_cluster`` 的返回值。
    返回：返回只含展示字段的字典。
    副作用：读取刚生成的 ``hosts.yml`` 与 ``config.yml``。

    只回传配置文本本身，不回传工作区路径：那是一个马上会被删掉的临时目录，
    写进响应里只会让用户以为可以照着去磁盘上找。因此这里把两份文件读成文本再带走——
    调用点保证它发生在工作区被清理之前。
    """
    return {
        'template': compiled.get('template', ''),
        'config_hash': compiled.get('config_hash', ''),
        'hosts_text': _read_text(compiled.get('hosts_path'), 'hosts.yml'),
        'config_text': _read_text(compiled.get('config_path'), 'config.yml'),
        'resolved_versions': compiled.get('resolved_versions') or {},
        'warnings': list(compiled.get('warnings') or []),
    }


def _read_text(path, label):
    """读取刚生成的配置文件。

    参数：`path` 表示文件路径；`label` 表示出错时展示给用户的文件名。
    返回：返回文件内容。
    副作用：读取磁盘文件。

    读不出来就当作这次预检失败：一份「预览」里配置是空的，比直接报错更容易误导用户
    ——他会以为平台生成不出配置。
    """
    if not path:
        raise DryRunError(f'预检没有生成 {label}。')
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError as exc:
        logger.exception('预检生成的配置读取失败：%s', path)
        raise DryRunError(f'预检生成的 {label} 无法读取：{exc}') from exc
