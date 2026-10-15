"""部署任务的日志落盘与诊断包。

任务失败时运维人员最需要三样东西：每个阶段到底跑到哪一步、原始输出里报了什么错、
这次任务用的配置和版本是什么。数据库里的 ``K8sDeploymentLog`` 已经存了逐行日志，
但日志是有保留策略的，因此这里额外把「当前任务的现场」打包成一个 zip 落到工作区的
``diagnostics/`` 目录，供人工下载排查。

**敏感信息必须在这里挡住**：hosts 与 config.yml 理论上不含密码（编译器不写密码），
但任务快照、环境变量与 ansible 输出都可能夹带凭据，所以凡是进诊断包的内容都要先过
``redact_text``。宁可诊断包少一点信息，也不能把 OSS 密钥或 SSH 密码写进一个可下载的文件。
"""

import json
import re
import zipfile
from pathlib import Path

from . import paths

# 每个阶段的原始输出文件后缀，与日志表分开存放：文件保留完整输出，日志表只留可展示的部分。
STAGE_LOG_SUFFIX = '.log'
# 诊断包里单条日志的最大字符数，避免一个失控的阶段把 zip 撑到几百 MB。
MAX_LOG_CHARS = 200000
# 需要打码的键名模式：密码、密钥、令牌、凭据。
SECRET_KEY_PATTERN = re.compile(
    r'(?i)(password|passwd|pass_|_pass|secret|token|access_key|private_key|credential|authorization)'
)
# 形如 KEY=VALUE / KEY: VALUE 的行内赋值，键名命中上面模式时把值替换掉。
SECRET_ASSIGNMENT = re.compile(
    r'(?im)^(\s*[-*]?\s*)([A-Za-z0-9_.-]*(?:password|passwd|secret|token|access_key|private_key)[A-Za-z0-9_.-]*)'
    r'(\s*[:=]\s*)(.+)$'
)
# 行内 JSON 风格赋值 "password": "xxx"，连同引号一起替换。
SECRET_JSON_FIELD = re.compile(
    r'(?i)("(?:[a-z0-9_]*)(?:password|passwd|secret|token|access_key|private_key)(?:[a-z0-9_]*)"\s*:\s*)'
    r'("[^"]*"|\'[^\']*\'|[^,}\s]+)'
)
# OpenSSH 私钥整段打码。
PRIVATE_KEY_BLOCK = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)
# 打码后统一使用的占位文本。
REDACTED = '********'


def redact_text(text):
    """把文本里的密码、密钥与令牌替换成占位符。

    参数：`text` 表示待处理的任意文本。
    返回：返回打码后的文本。
    副作用：纯计算，不修改持久化数据。
    """
    value = str(text or '')
    if not value:
        return ''
    value = PRIVATE_KEY_BLOCK.sub(REDACTED, value)
    value = SECRET_JSON_FIELD.sub(lambda match: f'{match.group(1)}"{REDACTED}"', value)
    value = SECRET_ASSIGNMENT.sub(lambda match: f'{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}', value)
    return value


def redact_structure(value):
    """递归地把结构化数据里的敏感字段替换成占位符。

    参数：`value` 表示字典、列表或标量。
    返回：返回打码后的同构数据。
    副作用：纯计算，不修改持久化数据。
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if SECRET_KEY_PATTERN.search(str(key)) else redact_structure(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_structure(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def stage_log_path(workspace, stage):
    """返回某个阶段的原始输出文件路径。

    参数：`workspace` 表示任务工作区；`stage` 表示阶段标识。
    返回：返回 ``<workspace>/logs/<stage>.log``；阶段标识为空时用 task 兜底。
    副作用：只做路径计算，不创建目录。
    """
    safe_stage = re.sub(r'[^A-Za-z0-9_.-]', '_', str(stage or '').strip()) or 'task'
    return paths.log_dir(workspace) / f'{safe_stage}{STAGE_LOG_SUFFIX}'


def append_stage_log(workspace, stage, text):
    """把一段输出追加到该阶段的原始日志文件。

    参数：`workspace` 表示任务工作区；`stage` 表示阶段标识；`text` 表示要追加的文本。
    返回：返回写入的文件路径。
    副作用：在磁盘上创建日志目录并追加内容；写入的内容已打码。
    """
    target = stage_log_path(workspace, stage)
    paths.ensure_dir(target.parent)
    with target.open('a', encoding='utf-8') as handle:
        handle.write(redact_text(text))
    return target


def read_stage_log(workspace, stage, limit=MAX_LOG_CHARS):
    """读取某个阶段的原始输出。

    参数：`workspace` 表示任务工作区；`stage` 表示阶段标识；`limit` 表示最多读取的字符数。
    返回：返回日志文本；文件不存在时返回空串。
    副作用：只读取磁盘文件，不修改内容。
    """
    target = stage_log_path(workspace, stage)
    if not target.exists():
        return ''
    try:
        text = target.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    # 超长时保留尾部：报错信息总在最后，砍掉开头比砍掉结尾更有用。
    if len(text) > limit:
        return f'（日志过长，仅保留最后 {limit} 字符）\n{text[-limit:]}'
    return text


def _safe_read(path, limit=MAX_LOG_CHARS):
    """读取一个文本文件并打码。

    参数：`path` 表示文件路径；`limit` 表示最多读取的字符数。
    返回：返回打码后的文本；文件缺失或不可读时返回空串。
    副作用：只读取磁盘文件，不修改内容。
    """
    target = Path(path)
    try:
        if not target.is_file():
            return ''
        text = target.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    if len(text) > limit:
        text = f'（文件过长，仅保留前 {limit} 字符）\n{text[:limit]}'
    return redact_text(text)


def deployment_summary(deployment):
    """汇总一次任务的现场信息，作为诊断包的索引文件。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回可直接序列化为 JSON 的字典，内容不含任何密钥。
    副作用：读取数据库中的任务、节点、事件与制品记录。
    """
    from k8s_deployment.models import K8sDeploymentEvent

    nodes = [
        {
            'name': item.name,
            'ip_address': str(item.ip_address),
            'ssh_port': item.ssh_port,
            'ssh_user': item.ssh_user,
            'roles': item.roles,
            'node_name': item.node_name,
            'status': item.status,
            'failure_reason': item.failure_reason,
        }
        for item in deployment.nodes.all()
    ]
    events = [
        {
            'id': item.id,
            'stage': item.stage,
            'level': item.level,
            'event_type': item.event_type,
            'message': redact_text(item.message),
            'created_at': item.created_at.isoformat() if item.created_at else '',
        }
        for item in K8sDeploymentEvent.objects.filter(deployment=deployment).order_by('id')[:500]
    ]
    artifacts = [
        {
            'release': item.release,
            'object_key': item.object_key,
            'sha256': item.sha256,
            'size_bytes': item.size_bytes,
            'verified': item.verified,
            'cache_hit': item.cache_hit,
            'error_message': item.error_message,
        }
        for item in deployment.artifact_uses.all()
    ]
    return {
        'schema': '1',
        'deployment': {
            'id': deployment.pk,
            'task_type': deployment.task_type,
            'status': deployment.status,
            'display_name': deployment.display_name,
            'slug': deployment.cluster_slug,
            'cluster_id': deployment.cluster_id,
            'kubernetes_version': deployment.kubernetes_version,
            'kubeasz_version': deployment.kubeasz_version,
            'topology_mode': deployment.topology_mode,
            'execution_mode': deployment.execution_mode,
            'current_stage': deployment.current_stage,
            'completed_stages': list(deployment.completed_stages or []),
            'error_code': deployment.error_code,
            'failure_reason': redact_text(deployment.failure_reason),
            'expected_config_revision': deployment.expected_config_revision,
            'worker_id': deployment.worker_id,
            'queued_at': deployment.queued_at.isoformat() if deployment.queued_at else '',
            'started_at': deployment.started_at.isoformat() if deployment.started_at else '',
            'finished_at': deployment.finished_at.isoformat() if deployment.finished_at else '',
            'request_snapshot': redact_structure(deployment.request_snapshot or {}),
            'result_summary': redact_structure(deployment.result_summary or {}),
        },
        'nodes': redact_structure(nodes),
        'artifacts': artifacts,
        'events': events,
    }


def collect_diagnostics(deployment, workspace=None):
    """采集一次任务的诊断材料并写入 zip 包。

    参数：`deployment` 表示 K8sDeployment 实例；`workspace` 表示任务工作区，传 None 时按任务主键推导。
    返回：返回诊断包的 Path；工作区不存在时返回 None。
    副作用：在 ``<workspace>/diagnostics/`` 下写入 json 与 zip 文件，内容均已打码。
    """
    root = Path(workspace) if workspace else paths.workspace_root(deployment.pk)
    if not root.exists():
        return None

    target_dir = paths.ensure_dir(paths.diagnostics_dir(root))
    summary = deployment_summary(deployment)
    summary_path = target_dir / 'summary.json'
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    package = target_dir / f'diagnostics-{deployment.pk}.zip'
    # 逐个写入而不是一次性构建：日志可能有几十 MB，一次性进内存会顶不住。
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('summary.json', json.dumps(summary, ensure_ascii=False, indent=2))
        for stage_path in sorted(paths.log_dir(root).glob(f'*{STAGE_LOG_SUFFIX}')):
            text = _safe_read(stage_path)
            if text:
                bundle.writestr(f'logs/{stage_path.name}', text)
        # 集群目录经 kubeasz 安装目录下的 clusters 入口取到，与 ezctl 看到的是同一份。
        cluster_directory = paths.cluster_dir(paths.kubeasz_root(), deployment.cluster_slug)
        for name in ('hosts', 'config.yml'):
            text = _safe_read(cluster_directory / name)
            if text:
                bundle.writestr(f'cluster/{name}', text)

    return package


def diagnostics_listing(deployment, workspace=None):
    """列出某次任务已经生成的诊断包。

    参数：`deployment` 表示 K8sDeployment 实例；`workspace` 表示任务工作区。
    返回：返回 ``[{'name':.., 'size':.., 'modified_at':..}]``，目录不存在时返回空列表。
    副作用：只读取磁盘目录，不修改文件。
    """
    root = Path(workspace) if workspace else paths.workspace_root(deployment.pk)
    target_dir = paths.diagnostics_dir(root)
    if not target_dir.exists():
        return []
    result = []
    for item in sorted(target_dir.glob('*.zip')):
        try:
            stat = item.stat()
        except OSError:
            continue
        result.append({'name': item.name, 'size': stat.st_size, 'modified_at': stat.st_mtime})
    return result
