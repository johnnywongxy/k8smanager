"""集群级备份与恢复：etcd snapshot 采集、归档加密、OSS 上传/下载与指定备份恢复。

一个备份要同时满足两个看起来矛盾的要求：**能被平台读回来校验**，并且**不能把密钥
带出去**。做法是分三层：

1. **明文层**：只打包重建集群状态必需的东西——etcd snapshot、inventory、config.yml、
   节点角色与版本、插件清单、以及对应版本的 kubeasz 源码包和 ezctl。**不含**任何明文
   密码、OSS 密钥或 SSH 凭据；CA 只带公钥证书，私钥默认不导出（规划 5.3）。
2. **加密层**：复用仓库既有的 Fernet 能力（``k8s_cluster.crypto``），按固定大小分片加密。
   分片的目的是避免一个几 GB 的 snapshot 一次性进内存（规划 5.4）。密钥派生自
   ``DJANGO_SECRET_KEY``，因此 Worker 与 Web 必须共用同一份 settings。
3. **指针层**：``pointer.json`` 只放非敏感元数据与密文的 SHA-256，不含密钥。残余风险是
   OSS 凭据泄露后攻击者可以替换密文与指针，规划中明确记为已知风险、不做签名。

**为什么密钥强度要单独拦一道**：源码里的默认 ``DJANGO_SECRET_KEY`` 是公开的，用它派生
的 Fernet 密钥人人可算。用这种密钥"加密"后上传到对象存储，等于把集群数据明文放到公网上。
所以检测到占位密钥时直接拒绝创建备份，而不是先备份再提示。
"""

import base64
import hashlib
import json
import re
import shutil
import time
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import artifacts, diagnostics, paths, register, state_machine

try:  # pragma: no cover - 仅在缺少依赖时给出可读提示
    from k8s_cluster.crypto import decrypt_secret, encrypt_secret
except ImportError:  # pragma: no cover
    decrypt_secret = None
    encrypt_secret = None

# 归档内的目录布局，恢复时按这些前缀取件。
ARCHIVE_MANIFEST_NAME = 'manifest.json'
ARCHIVE_CONFIG_PREFIX = 'cluster/'
ARCHIVE_SNAPSHOT_PREFIX = 'snapshot/'
ARCHIVE_SOURCE_PREFIX = 'source/'
ARCHIVE_META_PREFIX = 'meta/'
# 内部 manifest 的结构版本，与模型的 schema_version 同步。
MANIFEST_SCHEMA = '1'
# OSS 上的三个对象名。
ARCHIVE_OBJECT_NAME = 'archive.bin.enc'
POINTER_OBJECT_NAME = 'pointer.json'
CHECKSUMS_OBJECT_NAME = 'checksums.sha256'
# 备份在 OSS 上的顶层目录，可用 storage.oss.backup_dir 覆盖。
#
# 刻意与制品前缀（storage.oss.base_dir，默认 kubeasz）分开：制品同步与清点是按制品前缀
# 列对象的，备份混在里面会被当成制品统计；反过来备份配额也只想数自己的字节。两者的
# 生命周期也完全不同——制品随版本发布替换，备份要按保留策略长期留存。
DEFAULT_BACKUP_DIR = 'k8s-backups'
# 早期版本把备份放在 <制品前缀>/backups/ 下。已上传的备份靠记录里的 object_prefix 定位，
# 恢复与删除不受影响；这个旧值只剩一个用途——把它们算进用量统计。
LEGACY_BACKUP_DIR_NAME = 'backups'
# 密文文件的魔数，用于在解密前确认拿到的确实是平台的备份而不是别的文件。
CIPHERTEXT_MAGIC = b'K8SBK1\n'
# 分片加密与哈希计算的读写块大小。
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
IO_BLOCK_BYTES = 1024 * 1024
# 源码里的默认 SECRET_KEY 占位值；出现这些值一律拒绝备份。
PLACEHOLDER_SECRET_KEYS = (
    'platform-template-local-dev-secret-change-me',
    'django-insecure',
    'changeme',
    'change-me',
)
# kubeasz 在集群目录下约定的备份目录名。
SNAPSHOT_DIR_NAME = 'backup'
# 采集 snapshot 这一步的时长上限（秒）。
#
# 默认的阶段预算是 7200 秒（见 runner.DEFAULT_STAGE_TIMEOUT），那是给安装用的：装一个集群
# 两小时不算离谱。备份不是 —— 一次快照与集群规模成正比，几 GB 的 etcd 也就一两分钟，
# 跑到半小时以上一定是出事了。而它出事的方式很隐蔽：etcdctl 没有默认超时，地址拨不通时
# 它会一直等，既不出错也不结束（现场卡了三十多分钟，只留下一个 0 字节的 ``.part`` 文件）。
# 这里给一个远大于正常耗时、又远小于两小时的上限，让「卡住」最多只消耗半小时。
BACKUP_SNAPSHOT_TIMEOUT = 1800
# 恢复时用于定位目标 snapshot 变量的角色默认文件。
RESTORE_DEFAULTS_PATH = ('roles', 'cluster-restore', 'defaults', 'main.yml')
# 控制节点上需要一并采集的只读元数据：只要公钥材料，不要私钥。
CONTROL_PLANE_FILES = (
    '/etc/kubernetes/ssl/ca.crt',
    '/etc/kubernetes/ssl/ca.pem',
    '/etc/kubernetes/ssl/kube-apiserver.crt',
)


class BackupError(Exception):
    """备份或恢复失败，携带可直接展示给用户的中文原因。"""


# ---- 密钥边界 ----------------------------------------------------------


def assert_backup_key_strength():
    """确认当前 SECRET_KEY 不是源码里的公开占位值。

    参数：无。
    返回：无显式返回值。
    副作用：只读取配置，不修改数据。

    约束来源见规划 5.4：默认密钥是公开的，用它加密再上传等于明文上传。
    """
    value = str(getattr(settings, 'SECRET_KEY', '') or '')
    lowered = value.lower()
    if not value or len(value) < 32 or any(token in lowered for token in PLACEHOLDER_SECRET_KEYS):
        raise BackupError(
            '当前 DJANGO_SECRET_KEY 是源码默认值或强度不足，用它派生的密钥人人可算，'
            '加密后的备份等同于明文外泄。请先在环境变量里设置高强度随机 SECRET_KEY，'
            '并确保 Web 与 Worker 共用同一份配置后再创建备份。'
        )


def backup_capability():
    """判断当前平台是否具备创建备份的条件。

    参数：无。
    返回：返回 ``{'ok': bool, 'message': str}``。
    副作用：只读取配置，不修改数据。

    前端拿它来决定「备份」「升级」「恢复」按钮是否可用，以及给出什么提示。
    升级依赖备份（规划 10.4），所以密钥强度不足时这三个入口必须一起禁用，
    而不是等用户点了升级再在任务里失败。
    """
    try:
        assert_backup_key_strength()
    except BackupError as exc:
        return {'ok': False, 'message': str(exc)}
    if encrypt_secret is None or decrypt_secret is None:
        return {'ok': False, 'message': '当前环境缺少 cryptography 依赖，无法执行加密备份。'}
    return {'ok': True, 'message': ''}


def chunk_bytes():
    """返回分片加密的明文分片大小。

    参数：无。
    返回：返回字节数，未配置时用 4MB。
    副作用：只读取配置，不修改数据。
    """
    try:
        return max(int(getattr(settings, 'K8S_DEPLOY_BACKUP_CHUNK_BYTES', DEFAULT_CHUNK_BYTES)), 64 * 1024)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_BYTES


def max_archive_bytes():
    """返回单个备份允许的明文上限。

    参数：无。
    返回：返回字节数，未配置时用 8GB。
    副作用：只读取配置，不修改数据。
    """
    try:
        return int(getattr(settings, 'K8S_DEPLOY_BACKUP_MAX_BYTES', 8 * 1024 ** 3))
    except (TypeError, ValueError):
        return 8 * 1024 ** 3


def retention_days():
    """返回备份的保留天数。

    参数：无。
    返回：返回天数，未配置时用 30。
    副作用：只读取配置，不修改数据。
    """
    try:
        return int(getattr(settings, 'K8S_DEPLOY_BACKUP_RETENTION_DAYS', 30))
    except (TypeError, ValueError):
        return 30


def retention_min_keep():
    """返回无论如何都要保留的成功备份数量。

    参数：无。
    返回：返回个数，未配置时用 3。
    副作用：只读取配置，不修改数据。
    """
    try:
        return max(int(getattr(settings, 'K8S_DEPLOY_BACKUP_RETENTION_MIN_KEEP', 3)), 1)
    except (TypeError, ValueError):
        return 3


# ---- OSS 路径与工具 ----------------------------------------------------


def backup_base_dir(config):
    """返回备份在 OSS 上的顶层目录。

    参数：`config` 表示 OSS 配置字典。
    返回：返回去掉首尾斜线的目录名，未配置或用得不安全时返回 `DEFAULT_BACKUP_DIR`。
    副作用：只做纯计算，不修改持久化数据。
    """
    value = str(config.get('backup_dir') or DEFAULT_BACKUP_DIR).strip().strip('/')
    if not value or '..' in value.split('/'):
        return DEFAULT_BACKUP_DIR
    return value


def backup_prefix(cluster, backup_id):
    """返回一个备份在 OSS 上的对象前缀。

    参数：`cluster` 表示 K8sCluster 实例；`backup_id` 表示备份标识。
    返回：返回 ``<备份目录>/<组织>/<集群 slug>/<备份ID>``。
    副作用：只做纯计算，不修改持久化数据。

    分层按「谁能一眼找到它」排：组织先隔开租户，集群再聚同类，最后是带时间戳的备份 ID
    ——它的字典序就是时间序，所以列一个前缀得到的就是「某集群的全部备份」且已按时间排好，
    对保留策略清理和排查都够用。
    """
    config = artifacts.oss_config()
    organization_id = getattr(cluster, 'organization_id', None) or 0
    return f'{backup_base_dir(config)}/{organization_id}/{cluster.slug}/{backup_id}'


def new_backup_id():
    """生成一个新的备份标识。

    参数：无。
    返回：返回形如 ``bk-20260915T143000-a1b2c3`` 的标识。
    副作用：读取当前时间与随机数，不修改持久化数据。
    """
    stamp = timezone.localtime().strftime('%Y%m%dT%H%M%S')
    return f'bk-{stamp}-{uuid.uuid4().hex[:6]}'


def sha256_file(path, block=IO_BLOCK_BYTES):
    """计算文件的 SHA-256 摘要。

    参数：`path` 表示文件路径；`block` 表示读取块大小。
    返回：返回十六进制摘要。
    副作用：读取磁盘文件，不修改它。
    """
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(block), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entry(path, archive_name):
    """构造 manifest 里的一个文件条目。

    参数：`path` 表示磁盘文件路径；`archive_name` 表示归档内的名字。
    返回：返回 ``{'name':.., 'size':.., 'sha256':..}``。
    副作用：读取磁盘文件以计算哈希。
    """
    target = Path(path)
    return {
        'name': archive_name,
        'size': target.stat().st_size,
        'sha256': sha256_file(target),
    }


# ---- 分片加密 ----------------------------------------------------------


def encrypt_stream(source_path, target_path, chunk=None):
    """把一个文件分片加密写成密文文件。

    参数：`source_path` 表示明文文件；`target_path` 表示密文输出；`chunk` 表示分片字节数。
    返回：返回 ``{'size': 密文字节数, 'sha256': 密文哈希, 'chunks': 分片数}``。
    副作用：在磁盘上写入密文文件。

    写入格式：先写魔数，随后每个分片写成「一行十进制长度 + 一个 Fernet 令牌 + 换行」。
    长度行让解密端能在不知道分片大小的情况下逐片读取，也让文件本身可以被流式处理。
    """
    size = chunk or chunk_bytes()
    token_count = 0
    ciphertext_digest = hashlib.sha256()
    source = Path(source_path)
    target = Path(target_path)
    paths.ensure_dir(target.parent)

    with source.open('rb') as reader, target.open('wb') as writer:
        writer.write(CIPHERTEXT_MAGIC)
        while True:
            block = reader.read(size)
            if not block:
                break
            token = encrypt_secret(block.decode('latin-1'))
            payload = token.encode('ascii')
            writer.write(f'{len(payload)}\n'.encode('ascii'))
            writer.write(payload)
            writer.write(b'\n')
            token_count += 1

    with target.open('rb') as handle:
        for block in iter(lambda: handle.read(IO_BLOCK_BYTES), b''):
            ciphertext_digest.update(block)
    return {'size': target.stat().st_size, 'sha256': ciphertext_digest.hexdigest(), 'chunks': token_count}


def decrypt_stream(source_path, target_path):
    """把分片密文文件解密还原成明文文件。

    参数：`source_path` 表示密文文件；`target_path` 表示明文输出。
    返回：返回明文字节数。
    副作用：在磁盘上写入明文文件；调用方必须在 finally 里清理它。

    任何一片解密失败都立即停止并删除半成品：部分解密的归档看起来像"能用"，
    但恢复时会在最危险的时候才发现数据是坏的（规划 15「哈希/解密失败即停止」）。
    """
    source = Path(source_path)
    target = Path(target_path)
    paths.ensure_dir(target.parent)
    written = 0
    try:
        with source.open('rb') as reader, target.open('wb') as writer:
            magic = reader.read(len(CIPHERTEXT_MAGIC))
            if magic != CIPHERTEXT_MAGIC:
                raise BackupError('备份密文格式不正确，可能不是本平台生成的归档。')
            while True:
                length_line = reader.readline()
                if not length_line:
                    break
                if not length_line.strip():
                    continue
                try:
                    length = int(length_line.strip())
                except ValueError as exc:
                    raise BackupError('备份密文的分片长度字段损坏，归档无法解密。') from exc
                payload = reader.read(length)
                reader.read(1)
                if len(payload) != length:
                    raise BackupError('备份密文被截断，归档不完整。')
                try:
                    token = payload.decode('ascii')
                except UnicodeDecodeError as exc:
                    # Fernet 令牌一定是 ASCII；解不出来说明字节被改动过。
                    raise BackupError('备份密文在存储上被改动过（第 '
                                      f'{written // max(chunk_bytes(), 1) + 1} 片不是合法的加密分片），已中止解密。') from exc
                plain = decrypt_secret(token)
                if plain is None:
                    raise BackupError('当前环境缺少解密组件，无法读取备份。')
                if not plain:
                    raise BackupError('备份解密失败：密钥已轮换，或密文在存储上被改动过。'
                                      '密钥轮换后，轮换之前创建的备份将无法解密，请改用手工保留的归档。')
                block = plain.encode('latin-1')
                writer.write(block)
                written += len(block)
    except BackupError:
        _unlink(target)
        raise
    except Exception as exc:
        _unlink(target)
        raise BackupError(f'备份解密失败：{exc}。密钥可能已轮换，或密文在存储上被改动过。') from exc
    return written


def _unlink(path):
    """删除单个文件，不存在时静默返回。

    参数：`path` 表示文件路径。
    返回：无显式返回值。
    副作用：删除磁盘文件。
    """
    try:
        Path(path).unlink()
    except OSError:
        pass


# ---- 归档构建 ----------------------------------------------------------


def snapshot_kubeasz_version(cluster, deployment=None):
    """返回一份备份该记在自己名下的 kubeasz 版本。

    参数：`cluster` 表示 K8sCluster 实例；`deployment` 表示产生备份的任务，可为空。
    返回：返回版本号文本，取不到时返回空串。
    副作用：读取配置版本表，不修改数据。

    取集群**当前生效**的版本，不是本次任务的目标版本。升级前的强制备份里装的是**旧集群**的
    etcd 数据与旧 hosts/config，标成目标版本（新）的话，将来按这份备份回滚时
    ``validate_restore_selection`` 会拿它去比集群当前版本，必然不等 —— 而这份备份存在的
    唯一目的就是回滚，等于平台自己把退路锁死了。集群没有版本记录时才退到任务版本。
    """
    from .state_machine import cluster_active_version

    active = ''
    if cluster is not None:
        active = str(cluster_active_version(cluster)['kubeasz'] or '')
    return active or str(getattr(deployment, 'kubeasz_version', '') or '')


def _prune_snapshot_dir(kubeasz_root, slug):
    """清掉已经打进归档的快照文件。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回删掉的条目数量。
    副作用：删除 ``clusters/<slug>/backup`` 下的文件与子目录。

    ``playbooks/94.backup.yml`` 每跑一次就写一份 ``snapshot_<时间戳>.db``，从不清理；而这个目录
    现在跨任务保留（集群目录已挪到工作区之外），于是 ``snapshot_files`` 会把历史快照一并收进
    归档 —— 每个几百 MB 到 GB，还会顶到 ``K8S_DEPLOY_BACKUP_MAX_BYTES``。

    清理放在归档**之后**：内容已经进 zip 了才删源文件，失败重试时也还能拿到上一份。删掉不会
    丢东西 —— ``place_snapshot`` 在恢复时会把选定的那份重新摆回来。
    """
    directory = paths.cluster_dir(kubeasz_root, slug) / SNAPSHOT_DIR_NAME
    if not directory.is_dir():
        return 0
    removed = 0
    for item in list(directory.iterdir()):
        if item.is_dir() and not item.is_symlink():
            paths.remove_tree(item)
        else:
            try:
                item.unlink()
            except OSError:
                # 删不掉的文件不该让备份停住，下次备份再收一次就是。
                continue
        removed += 1
    return removed


def snapshot_files(kubeasz_root, slug):
    """列出 kubeasz 在集群备份目录下产生的文件。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回文件 Path 列表，按修改时间从新到旧。
    副作用：只读取目录，不修改文件。

    kubeasz 各版本对 snapshot 的命名不一致（有的带时间戳，有的带 .db 后缀），因此这里
    一律按目录实际内容取件，不猜文件名（规划 11.1）。
    """
    directory = paths.cluster_dir(kubeasz_root, slug) / SNAPSHOT_DIR_NAME
    if not directory.is_dir():
        return []
    items = [item for item in directory.rglob('*') if item.is_file() and not item.is_symlink()]
    try:
        items.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return items


def control_plane_metadata(nodes, credentials_map):
    """通过 SSH 从控制节点采集公钥材料与目录清单。

    参数：`nodes` 表示节点列表；`credentials_map` 表示连接信息。
    返回：返回 ``{归档内名字: 字节内容}`` 字典。
    副作用：建立 SSH 连接并读取目标机上的只读文件。

    **只读公钥材料**：CA 私钥默认不导出，因此平台不承诺在全新节点上重建集群身份（规划 5.3）。
    控制节点不可达时由调用方决定是硬失败还是继续。
    """
    from . import credentials as credential_service

    result = {}
    entries = [_node_view(item) for item in nodes]
    masters = [item for item in entries if 'master' in (item['roles'] or [])] or entries
    if not masters:
        return result
    node = masters[0]
    ip = node['ip_address']
    connection = credentials_map.get(ip) or {}
    key_path = credential_service.private_key_path()
    try:
        client = credential_service.ssh_client(
            ip,
            int(connection.get('port') or node['ssh_port'] or 22),
            connection.get('user') or node['ssh_user'] or 'root',
            key_path=key_path,
        )
    except Exception as exc:
        raise BackupError(f'无法连接控制节点 {ip} 采集 CA 公钥：{exc}') from exc

    try:
        for remote_path in CONTROL_PLANE_FILES:
            code, out, _err = credential_service.run_command(client, f'base64 -w0 {remote_path} 2>/dev/null || true')
            if code == 0 and out.strip():
                try:
                    result[f'ca/{Path(remote_path).name}'] = base64.b64decode(out.strip())
                except Exception:
                    # 单份材料读取失败不阻断整体备份，缺哪份在 manifest 里看得出来。
                    continue
        code, out, _err = credential_service.run_command(
            client, 'ls -1 /etc/kubernetes/ssl 2>/dev/null | head -200 || true',
        )
        if code == 0 and out.strip():
            result['meta/control-plane-ssl-listing.txt'] = out.strip().encode('utf-8')
    finally:
        try:
            client.close()
        except Exception:
            pass
    return result


def _node_view(item):
    """把节点统一看成字典。

    参数：`item` 表示节点字典或模型实例。
    返回：返回 ``{'ip_address','ssh_port','ssh_user','roles','name'}``。
    副作用：只读取对象属性，不修改数据。

    备份任务的节点可能来自任务快照（模型实例），也可能来自集群配置版本（字典），
    这里抹平差异，避免调用方到处写两套取值逻辑。
    """
    if isinstance(item, dict):
        getter = item.get
    else:
        getter = lambda key, default='': getattr(item, key, default)  # noqa: E731
    roles = getter('roles') or []
    return {
        'name': str(getter('name') or ''),
        'ip_address': str(getter('ip_address') or ''),
        'ssh_port': int(getter('ssh_port') or 22),
        'ssh_user': str(getter('ssh_user') or 'root'),
        'roles': list(roles),
    }


def release_source_archive(deployment, kubeasz_version):
    """找到本任务使用的 kubeasz 源码包在缓存中的位置。

    参数：`deployment` 表示 K8sDeployment 实例；`kubeasz_version` 表示 kubeasz 版本号。
    返回：返回源码包的 Path；找不到时返回 None。
    副作用：只读取制品使用记录与磁盘文件。
    """
    if deployment is None:
        return None
    suffix = f'source/kubeasz-{artifacts.clean_version(kubeasz_version)}.zip'
    record = (
        deployment.artifact_uses.filter(object_key__endswith=suffix, verified=True)
        .order_by('-id')
        .first()
    )
    if record and record.local_path and Path(record.local_path).is_file():
        return Path(record.local_path)
    return None


def build_archive(cluster, deployment, kubeasz_root, backup_id, credentials_map=None, nodes=None):
    """构建备份归档并生成内部 manifest。

    参数：`cluster` 表示 K8sCluster 实例；`deployment` 表示产生备份的任务，可为空；
        `kubeasz_root` 表示 kubeasz 源码根目录；`backup_id` 表示备份标识；
        `credentials_map` 表示节点连接信息；`nodes` 表示节点列表。
    返回：返回 ``{'path': 归档 Path, 'manifest': dict, 'entries': [...]}``。
    副作用：在任务工作区写入归档文件；不删除任何源文件。

    归档里放的是"重建这个集群所需的最小工具包"：即便对应 release 后来被撤销，
    这份备份仍然能恢复（规划 11.1）。
    """
    workspace = paths.ensure_dir(paths.workspace_root(deployment.pk)) if deployment else None
    target_dir = paths.ensure_dir(Path(workspace) / 'backup') if workspace else None
    if target_dir is None:
        raise BackupError('缺少任务工作区，无法构建备份归档。')
    archive_path = target_dir / f'{backup_id}.zip'

    snapshots = snapshot_files(kubeasz_root, cluster.slug)
    if not snapshots:
        raise BackupError(
            'kubeasz 没有在集群备份目录下产生 snapshot 文件，备份未完成。'
            '请确认目标集群的 etcd 可访问后重试。'
        )

    entries = []
    manifest = {
        'schema': MANIFEST_SCHEMA,
        'backup_id': backup_id,
        'cluster_slug': cluster.slug,
        'cluster_name': cluster.name,
        'organization_id': cluster.organization_id,
        'kubernetes_version': str(cluster.kube_version or ''),
        'kubeasz_version': snapshot_kubeasz_version(cluster, deployment),
        # 归档里那份 kubeasz 源码包取自本次任务的制品（升级任务就是目标版本）：它只说明
        # 「将来离线重建时手上有哪一套剧本」，不参与这份快照的版本归属。
        'source_kubeasz_version': str(getattr(deployment, 'kubeasz_version', '') or ''),
        'config_revision': register.current_revision(cluster),
        'created_at': timezone.now().isoformat(),
        'deployment_id': getattr(deployment, 'pk', None),
        'files': entries,
    }

    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for item in snapshots:
            name = f'{ARCHIVE_SNAPSHOT_PREFIX}{item.relative_to(paths.cluster_dir(kubeasz_root, cluster.slug))}'
            bundle.write(item, name)
            entries.append(_file_entry(item, name))

        cluster_directory = paths.cluster_dir(kubeasz_root, cluster.slug)
        for name in ('hosts', 'config.yml'):
            source = cluster_directory / name
            if source.is_file():
                archive_name = f'{ARCHIVE_CONFIG_PREFIX}{name}'
                bundle.write(source, archive_name)
                entries.append(_file_entry(source, archive_name))

        source_archive = release_source_archive(deployment, manifest['source_kubeasz_version'])
        if source_archive:
            archive_name = f'{ARCHIVE_SOURCE_PREFIX}{source_archive.name}'
            bundle.write(source_archive, archive_name)
            entries.append(_file_entry(source_archive, archive_name))

        # ezctl 是恢复时必须的入口脚本，单独收一份，避免依赖源码包解压。
        ezctl = Path(kubeasz_root) / 'ezctl'
        if ezctl.is_file():
            archive_name = f'{ARCHIVE_SOURCE_PREFIX}ezctl'
            bundle.write(ezctl, archive_name)
            entries.append(_file_entry(ezctl, archive_name))

        if nodes:
            # 不写 ssh_user/端口之外的任何凭据字段；恢复时靠公钥登录（规划 5.2）。
            manifest['nodes'] = [_node_view(entry) for entry in nodes]
        manifest['health'] = {
            'last_test_status': cluster.last_test_status,
            'last_test_message': getattr(cluster, 'last_test_message', ''),
            'kube_version': str(cluster.kube_version or ''),
            'network_plugin': str(cluster.network_plugin or ''),
            'node_count': cluster.node_count,
        }

        if credentials_map and nodes:
            # 控制节点读不到就整体失败：没有 CA 公钥的备份在恢复时无法验证集群身份。
            for name, payload in control_plane_metadata(nodes, credentials_map).items():
                bundle.writestr(f'{ARCHIVE_META_PREFIX}{name}', payload)

        manifest['files'] = entries
        manifest['total_bytes'] = sum(item['size'] for item in entries)
        bundle.writestr(ARCHIVE_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    size = archive_path.stat().st_size
    limit = max_archive_bytes()
    if size > limit:
        _unlink(archive_path)
        raise BackupError(
            f'备份归档 {size / 1024 ** 3:.2f} GB 超过单备份上限 {limit / 1024 ** 3:.2f} GB，'
            '已在上传前中止。请缩小备份范围或调整平台配置。'
        )
    return {'path': archive_path, 'manifest': manifest, 'entries': entries}


# ---- 上传与校验 --------------------------------------------------------


def pointer_payload(manifest, backup_id, prefix, cipher):
    """构造非敏感指针文件的内容。

    参数：`manifest` 表示内部 manifest；`backup_id` 表示备份标识；`prefix` 表示 OSS 前缀；
        `cipher` 表示密文大小与哈希。
    返回：返回字典。
    副作用：纯计算，不修改持久化数据。
    """
    return {
        'schema': MANIFEST_SCHEMA,
        'backup_id': backup_id,
        'cluster_slug': manifest.get('cluster_slug', ''),
        'organization_id': manifest.get('organization_id'),
        'kubernetes_version': manifest.get('kubernetes_version', ''),
        'kubeasz_version': manifest.get('kubeasz_version', ''),
        'config_revision': manifest.get('config_revision', 0),
        'created_at': manifest.get('created_at', ''),
        'object_prefix': prefix,
        'archive': {
            'name': ARCHIVE_OBJECT_NAME,
            'size': cipher['size'],
            'sha256': cipher['sha256'],
            'chunks': cipher['chunks'],
            'chunk_bytes': chunk_bytes(),
        },
    }


def upload_backup(bucket, prefix, archive_path, pointer, cipher):
    """把归档、指针与校验文件上传到 OSS。

    参数：`bucket` 表示 OSS Bucket 客户端；`prefix` 表示对象前缀；`archive_path` 表示密文文件；
        `pointer` 表示指针字典；`cipher` 表示密文信息。
    返回：返回上传的对象键列表。
    副作用：向 OSS 写入三个对象。
    """
    keys = [f'{prefix}/{ARCHIVE_OBJECT_NAME}', f'{prefix}/{POINTER_OBJECT_NAME}', f'{prefix}/{CHECKSUMS_OBJECT_NAME}']
    try:
        bucket.put_object_from_file(keys[0], str(archive_path))
        bucket.put_object(keys[1], json.dumps(pointer, ensure_ascii=False, indent=2).encode('utf-8'))
        checksums = f'{cipher["sha256"]}  {ARCHIVE_OBJECT_NAME}\n'
        bucket.put_object(keys[2], checksums.encode('utf-8'))
    except Exception as exc:
        raise BackupError(f'上传备份到 OSS 失败：{exc}') from exc
    return keys


def verify_remote_backup(bucket, prefix, cipher):
    """回读 OSS 上的备份并校验大小与哈希。

    参数：`bucket` 表示 OSS Bucket 客户端；`prefix` 表示对象前缀；`cipher` 表示本地密文信息。
    返回：无显式返回值。
    副作用：从 OSS 完整读取一次密文；校验失败时抛出 ``BackupError``。

    规划 11.1 明确要求回读校验：上传成功不等于存对了，OSS 上的对象可能被截断或覆盖。
    只有回读比对一致，这次备份才允许标记成功。
    """
    key = f'{prefix}/{ARCHIVE_OBJECT_NAME}'
    try:
        meta = bucket.head_object(key)
    except Exception as exc:
        raise BackupError(f'回读 OSS 备份失败：{exc}') from exc
    remote_size = int(meta.content_length or 0)
    if remote_size != int(cipher['size']):
        raise BackupError(f'OSS 上的备份大小 {remote_size} 与本地 {cipher["size"]} 不一致，备份未通过校验。')

    digest = hashlib.sha256()
    try:
        stream = bucket.get_object(key)
        while True:
            block = stream.read(IO_BLOCK_BYTES)
            if not block:
                break
            digest.update(block)
    except Exception as exc:
        raise BackupError(f'回读 OSS 备份内容失败：{exc}') from exc
    if digest.hexdigest() != cipher['sha256']:
        raise BackupError('OSS 上的备份内容哈希与本地不一致，备份未通过校验，已标记为失败。')


# ---- 备份主流程 --------------------------------------------------------


def create_backup(cluster, deployment=None, context=None, kubeasz_root=None, runner_instance=None,
                  credentials_map=None, nodes=None, record=None):
    """执行一次完整的集群备份。

    参数：`cluster` 表示 K8sCluster 实例；`deployment` 表示产生备份的任务；
        `context` 表示任务上下文，可为空；`kubeasz_root` 表示 kubeasz 源码根目录；
        `runner_instance` 表示执行器；`credentials_map` 表示节点连接信息；`nodes` 表示节点列表；
        `record` 表示调用方预先创建的备份记录（独立备份任务会先落一条 pending 记录给用户看）。
    返回：返回更新后的 K8sClusterBackup。
    副作用：在目标集群执行 ``ezctl backup``，加解密归档，上传 OSS，写入备份记录。

    无论成功失败都在 finally 里清理明文归档与临时目录：明文 snapshot 留在平台盘上
    等于绕过了整个加密设计（规划 11.1）。
    """
    from k8s_deployment.models import K8sClusterBackup

    assert_backup_key_strength()
    if encrypt_secret is None or decrypt_secret is None:
        raise BackupError('当前环境缺少 cryptography 依赖，无法执行加密备份。')

    if record is None:
        record = K8sClusterBackup.objects.create(
            organization=cluster.organization,
            cluster=cluster,
            deployment=deployment,
            backup_id=new_backup_id(),
            slug=cluster.slug,
            kubernetes_version=str(cluster.kube_version or ''),
            kubeasz_version=snapshot_kubeasz_version(cluster, deployment),
            config_revision=register.current_revision(cluster),
            status='running',
            schema_version=MANIFEST_SCHEMA,
        )
    else:
        record.status = 'running'
        record.failure_reason = ''
        record.save(update_fields=['status', 'failure_reason', 'updated_at'])

    plaintext = None
    try:
        target_nodes = nodes if nodes is not None else cluster_nodes(cluster, deployment)
        if credentials_map is None and target_nodes:
            credentials_map = credential_map_for(cluster, target_nodes)
        _run_cluster_backup_command(context, deployment, cluster, kubeasz_root, runner_instance)
        built = build_archive(
            cluster, deployment, kubeasz_root, record.backup_id,
            credentials_map=credentials_map, nodes=target_nodes,
        )
        plaintext = built['path']
        # 快照已经进归档了，盘上这份留着只会让下一次备份把历史快照重新打包上传一遍。
        _prune_snapshot_dir(kubeasz_root, cluster.slug)

        prefix = backup_prefix(cluster, record.backup_id)
        cipher_path = Path(plaintext).with_suffix('.bin.enc')
        cipher = encrypt_stream(plaintext, cipher_path)
        pointer = pointer_payload(built['manifest'], record.backup_id, prefix, cipher)

        bucket = artifacts.oss_bucket()
        upload_backup(bucket, prefix, cipher_path, pointer, cipher)
        verify_remote_backup(bucket, prefix, cipher)

        record.object_prefix = prefix
        record.archive_size = int(cipher['size'])
        record.archive_sha256 = cipher['sha256']
        record.config_revision = int(built['manifest'].get('config_revision') or record.config_revision)
        record.kubeasz_version = str(built['manifest'].get('kubeasz_version') or record.kubeasz_version)
        record.kubernetes_version = str(built['manifest'].get('kubernetes_version') or record.kubernetes_version)
        record.status = 'succeeded'
        record.failure_reason = ''
        record.finished_at = timezone.now()
        record.save()
        if context:
            context.log('system', f'备份 {record.backup_id} 已上传并回读校验通过（{cipher["size"]} 字节）')
            context.event('backup_created', f'备份 {record.backup_id} 完成', detail={
                'backup_id': record.backup_id,
                'size': record.archive_size,
                'sha256': record.archive_sha256,
            })
        apply_retention(cluster)
        return record
    except Exception as exc:
        record.status = 'failed'
        record.failure_reason = diagnostics.redact_text(str(exc))
        record.finished_at = timezone.now()
        record.save(update_fields=['status', 'failure_reason', 'finished_at', 'updated_at'])
        raise
    finally:
        # 明文归档、密文与指针文件都不留在平台上。
        if plaintext:
            _unlink(plaintext)
            _unlink(Path(plaintext).with_suffix('.bin.enc'))
            shutil.rmtree(Path(plaintext).parent, ignore_errors=True)


def pending_backup_for(deployment):
    """取出备份任务对应的待执行备份记录。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回 K8sClusterBackup；没有待执行记录时返回 None（此时由服务自行创建）。
    副作用：读取数据库，不修改数据。

    独立备份接口会先落一条 ``pending`` 记录，用户提交后立刻能在列表里看到「待执行」的
    那一行；Worker 真正开跑时把它推进到 ``running``，而不是另起一条新记录。重试时也复用
    那条失败记录，避免每次重试都在列表里多出一行残影。
    """
    from k8s_deployment.models import K8sClusterBackup

    return (
        K8sClusterBackup.objects
        .filter(deployment=deployment, status__in=('pending', 'running', 'failed'))
        .order_by('-id')
        .first()
    )


def cluster_nodes(cluster, deployment):
    """挑选备份要一并记录的节点清单。

    参数：`cluster` 表示 K8sCluster 实例；`deployment` 表示任务，可为空。
    返回：返回 ``[{'name','ip_address','ssh_port','ssh_user','roles','node_name'}, ...]``。
    副作用：读取数据库，不修改数据。

    备份任务通常不携带自己的节点快照（它作用在一个已经存在的集群上），因此回落到集群最新
    配置版本里的节点记录——那正是集群当前生效的拓扑（规划 6.1）。
    """
    if deployment is not None:
        nodes = list(deployment.nodes.order_by('id'))
        if nodes:
            return [
                {
                    'name': item.name,
                    'ip_address': str(item.ip_address),
                    'ssh_port': item.ssh_port,
                    'ssh_user': item.ssh_user,
                    'roles': list(item.roles or []),
                    'node_name': item.node_name,
                }
                for item in nodes
            ]
    return cluster_node_snapshot(cluster)


def cluster_node_snapshot(cluster):
    """从集群最新配置版本快照里取回节点清单。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回节点字典列表，没有配置版本时返回空列表。
    副作用：读取数据库，不修改数据。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('-revision').first()
    if revision is None:
        return []
    result = []
    for item in (revision.snapshot or {}).get('nodes') or []:
        if not isinstance(item, dict) or not item.get('ip_address'):
            continue
        result.append({
            'name': item.get('name') or item.get('ip_address'),
            'ip_address': str(item['ip_address']),
            'ssh_port': int(item.get('ssh_port') or 22),
            'ssh_user': item.get('ssh_user') or 'root',
            'roles': list(item.get('roles') or []),
            'node_name': item.get('node_name') or '',
        })
    return result


def credential_map_for(cluster, nodes):
    """为集群现有节点构造 SSH 连接信息。

    参数：`cluster` 表示 K8sCluster 实例；`nodes` 表示节点字典列表。
    返回：返回 ``{ip: {'user':.., 'port':..}}``。
    副作用：读取数据库，不修改数据。

    只用平台部署公钥免密登录，**不带密码**（规划 5.2）。备份与恢复都不需要密码，
    因此这里不需要（也不应该）去解密机器表里的凭据。
    """
    from k8s_machine.models import Machine

    addresses = [item['ip_address'] for item in nodes if item.get('ip_address')]
    machines = {str(item.ip_address): item for item in Machine.objects.filter(ip_address__in=addresses)}
    result = {}
    for item in nodes:
        ip = item.get('ip_address')
        if not ip:
            continue
        machine = machines.get(str(ip))
        result[str(ip)] = {
            'user': (machine.ssh_user if machine is not None else '') or item.get('ssh_user') or 'root',
            'port': (machine.ssh_port if machine is not None else 0) or item.get('ssh_port') or 22,
        }
    _ = cluster
    return result


def _ensure_etcd_reachable(context, cluster, kubeasz_root):
    """保证平台机拨得到这个集群的 etcd 客户端口。

    参数：`context` 表示任务上下文，可为空；`cluster` 表示集群；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回可达的 etcd 地址列表。
    副作用：可能接管本机地址、建立 SSH 转发通道并写日志。

    备份剧本整本跑在 ``hosts: localhost`` 上，用 etcdctl 直接拨 ``[etcd]`` 段里的内网地址。
    平台机通常只在公网侧连得上节点（SSH 通、内网地址拨不通），于是这一步以前会**安静地**
    卡死：etcdctl 没有默认超时，剧本既不报错也不结束，任务永远停在「集群备份」，集群目录里
    只留一个 0 字节的 ``.part``。这里复用部署期那条转发通道（``platform_relay``），把 etcd
    地址也接管过来；建不起来就当场失败，并把原因写清楚，而不是交给 etcdctl 去等。
    """
    from . import credentials, node_address, platform_relay

    hosts_file = paths.cluster_dir(kubeasz_root, cluster.slug) / 'hosts'
    try:
        text = hosts_file.read_text(encoding='utf-8')
    except OSError as exc:
        raise BackupError(
            f'读不到集群的 inventory（{hosts_file}）：{exc}。备份剧本要按它去连 etcd。'
        ) from exc
    targets = platform_relay.etcd_targets(text)
    if not targets:
        raise BackupError(
            f'集群的 inventory（{hosts_file}）里没有 etcd 节点，无法确定从哪里取快照。'
        )
    outcomes = platform_relay.ensure_from_inventory(
        context, hosts_file, key_path=credentials.private_key_path(),
        route=platform_relay.ETCD_ROUTE,
    ) if context is not None else {}
    unreachable = [
        item['address'] for item in targets
        if not node_address.platform_reaches(item['address'], platform_relay.ETCD_CLIENT_PORT)
    ]
    if unreachable:
        reasons = [
            f'{address}（{outcomes.get(platform_relay.route_key(address, platform_relay.ETCD_CLIENT_PORT), "未接管")}）'
            for address in unreachable
        ]
        raise BackupError(
            '平台机连不上这些 etcd 地址的 '
            f'{platform_relay.ETCD_CLIENT_PORT} 端口：' + '、'.join(reasons) + '。'
            '备份剧本必须在平台机上执行 etcdctl，连不上就会一直等下去（etcdctl 没有默认超时），'
            '任务看起来就像卡死了。请确认平台机到这个地址的网络是通的，'
            '或者让备份 Worker 能以 root 建立转发通道。'
        )
    return [item['address'] for item in targets]


def _run_cluster_backup_command(context, deployment, cluster, kubeasz_root, runner_instance):
    """在目标集群上执行 ``ezctl backup``。

    参数：`context` 表示任务上下文，可为空；`deployment` 表示任务，可为空；
        `cluster` 表示集群；`kubeasz_root` 表示 kubeasz 源码根目录；`runner_instance` 表示执行器。
    返回：无显式返回值。
    副作用：在目标集群上执行 etcd snapshot 备份。

    升级前备份等场景由任务上下文驱动；没有上下文时（独立备份接口拉起的 Worker 任务）
    也必须有一个执行器，否则无法真正产生 snapshot。
    """
    if kubeasz_root is None:
        raise BackupError('缺少 kubeasz 工作区，无法执行集群备份。')
    if runner_instance is None:
        raise BackupError('缺少执行器，无法执行集群备份。')

    from . import runner as runner_module

    argv = runner_module.ezctl_argv(kubeasz_root, ['backup', cluster.slug])
    if context is not None:
        context.begin_stage('backup', '集群备份')
        # 通道要在剧本启动前建好：剧本一跑起来就在拨 etcd，那时候再建已经晚了。
        _ensure_etcd_reachable(context, cluster, kubeasz_root)
        context.log('system', f'执行命令：{runner_module.format_command(argv)}')
        context.log(
            'system',
            '正在从 etcd 取快照：这一步会先逐个成员探活，再从一个健康成员导出数据；'
            '集群越大越久，期间没有输出是正常的。',
        )
        result = runner_instance.run_stage(
            'backup', argv, cwd=kubeasz_root,
            env=state_machine.command_env(context, kubeasz_root),
            on_line=context.on_line,
            timeout=min(context.stage_budget(), BACKUP_SNAPSHOT_TIMEOUT),
            is_cancelled=context.cancelled,
            # 取快照这一步安静得最久（先探活再导出），旁白与空转落库都要挂上：
            # 没有它们，剧本一个字不吐的时候进度页看起来就是「没反应」。
            on_idle=context.narrate_idle,
            on_tick=context.tick,
        )
    else:
        # 没有上下文就建不了转发通道，但「拨不通」这条仍然要当场拦住：放它过去就是一次
        # 没有终点的等待。
        _ensure_etcd_reachable(None, cluster, kubeasz_root)
        result = runner_instance.run_stage(
            'backup', argv, cwd=kubeasz_root,
            env=state_machine.command_env(None, kubeasz_root),
            timeout=None,
        )
    if result.cancelled:
        raise BackupError('备份已被取消。')
    if not result.ok:
        raise BackupError(result.describe('集群备份'))
    if context is not None:
        context.complete_stage('backup', elapsed=result.duration)


# ---- 备份列表、删除与保留策略 ------------------------------------------


def list_backups(cluster, statuses=None):
    """列出某个集群的备份记录。

    参数：`cluster` 表示 K8sCluster 实例；`statuses` 表示要过滤的状态集合，可为空。
    返回：返回 K8sClusterBackup 查询集，按创建时间倒序。
    副作用：只读取数据库，不修改数据。
    """
    from k8s_deployment.models import K8sClusterBackup

    queryset = K8sClusterBackup.objects.filter(cluster=cluster)
    if statuses:
        queryset = queryset.filter(status__in=list(statuses))
    return queryset.order_by('-created_at', '-id')


def validate_restore_selection(cluster, record):
    """校验所选备份是否可用于当前集群的恢复。

    参数：`cluster` 表示 K8sCluster 实例；`record` 表示 K8sClusterBackup 实例。
    返回：无显式返回值。
    副作用：只读取对象字段。

    跨组织、跨集群、跨 Kubernetes/kubeasz 版本的恢复一律拒绝（规划 11.2）：
    etcd snapshot 与集群版本是绑死的，拿旧版本的 snapshot 去恢复新版本集群只会把
    etcd 数据弄坏。
    """
    if record.cluster_id != cluster.pk:
        raise BackupError('所选备份不属于当前集群。')
    if record.organization_id and cluster.organization_id and record.organization_id != cluster.organization_id:
        raise BackupError('所选备份属于其它组织，不能用于当前集群的恢复。')
    # 标识只在两边都有值时才比：集群被清理时 ``slug`` 是按设计清空的（见 K8sCluster 的
    # 状态说明），拿它去比会得出一句 `与当前集群  不一致` 的胡话，把用户引到标识上 ——
    # 那种集群该拦住它的是「已清理、不能运维」，由 ``cluster_ops.assert_deployed`` 说。
    if record.slug and cluster.slug and record.slug != cluster.slug:
        raise BackupError(f'所选备份的集群标识为 {record.slug}，与当前集群 {cluster.slug} 不一致。')
    if record.status != 'succeeded':
        raise BackupError(f'所选备份当前状态为 {record.get_status_display()}，只有成功的备份才能用于恢复。')
    if not record.object_prefix or not record.archive_sha256:
        raise BackupError('所选备份缺少 OSS 对象信息或哈希，无法校验与恢复。')

    from .state_machine import cluster_active_version

    current_kubeasz = cluster_active_version(cluster)['kubeasz']
    if record.kubeasz_version and current_kubeasz and record.kubeasz_version != current_kubeasz:
        raise BackupError(
            f'所选备份的 kubeasz 版本为 {record.kubeasz_version}，集群当前为 {current_kubeasz}，'
            '跨版本恢复不受支持，请新建集群后迁移。'
        )
    current_kube = str(cluster.kube_version or '').lstrip('v')
    backup_kube = str(record.kubernetes_version or '').lstrip('v')
    if current_kube and backup_kube and _minor(current_kube) != _minor(backup_kube):
        raise BackupError(
            f'所选备份的 Kubernetes 版本为 {backup_kube}，集群当前为 {current_kube}，'
            '跨次版本恢复不受支持，请新建集群后迁移。'
        )


def _minor(value):
    """提取版本号的次版本部分。

    参数：`value` 表示版本文本。
    返回：返回 ``主版本.次版本``，解析不出来时返回空串。
    副作用：纯计算，不修改持久化数据。
    """
    parts = str(value or '').split('.')
    return '.'.join(parts[:2]) if len(parts) >= 2 else ''


def delete_backup(record):
    """删除一个备份在 OSS 上的对象并把记录标记为已删除。

    参数：`record` 表示 K8sClusterBackup 实例。
    返回：返回更新后的记录。
    副作用：删除 OSS 对象并更新数据库状态；失败时标记 ``delete_failed`` 供重试。
    """
    if record.status == 'deleted':
        return record
    record.status = 'deleting'
    record.save(update_fields=['status', 'updated_at'])
    try:
        bucket = artifacts.oss_bucket()
        for name in (ARCHIVE_OBJECT_NAME, POINTER_OBJECT_NAME, CHECKSUMS_OBJECT_NAME):
            bucket.delete_object(f'{record.object_prefix}/{name}')
    except Exception as exc:
        record.status = 'delete_failed'
        record.failure_reason = diagnostics.redact_text(f'删除 OSS 对象失败：{exc}')
        record.save(update_fields=['status', 'failure_reason', 'updated_at'])
        return record
    record.status = 'deleted'
    record.finished_at = record.finished_at or timezone.now()
    record.save(update_fields=['status', 'finished_at', 'updated_at'])
    return record


def apply_retention(cluster):
    """按「最近若干次成功备份优先于保留期」的规则清理过期备份。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回被删除的备份数量。
    副作用：删除 OSS 对象并更新备份记录状态。

    规则（规划 5.4）：只有**同时**满足「超过保留期」且「不属于最近 N 次成功备份」时才允许删除。
    这样即使一个集群长时间只有一个备份，它也不会被时间淘汰掉。
    """
    from k8s_deployment.models import K8sClusterBackup

    successful = list(list_backups(cluster, statuses=['succeeded']))
    keep = retention_min_keep()
    protected_ids = {item.pk for item in successful[:keep]}
    K8sClusterBackup.objects.filter(cluster=cluster, is_protected=True).exclude(pk__in=protected_ids).update(is_protected=False)
    if protected_ids:
        K8sClusterBackup.objects.filter(pk__in=protected_ids, is_protected=False).update(is_protected=True)

    deadline = timezone.now() - timedelta(days=retention_days())
    candidates = [
        item for item in successful
        if item.pk not in protected_ids and item.created_at and item.created_at < deadline and item.object_prefix
    ]
    removed = 0
    for item in candidates:
        delete_backup(item)
        if item.status == 'deleted':
            removed += 1
    return removed


# ---- 恢复 --------------------------------------------------------------


def download_backup(record, target_dir):
    """下载并解密一个备份的归档。

    参数：`record` 表示 K8sClusterBackup 实例；`target_dir` 表示解压目标目录。
    返回：返回 ``{'archive': 归档 Path, 'manifest': dict}``。
    副作用：从 OSS 下载密文、解密、解压到目标目录。

    下载后立即比对 OSS 对象的哈希与记录里保存的哈希，两者不一致说明对象被替换过，
    这正是规划 5.4 记录的那项残余风险，必须在解压前挡住。
    """
    from k8s_deployment.models import K8sClusterBackup

    prefix = record.object_prefix
    bucket = artifacts.oss_bucket()
    target = paths.ensure_dir(Path(target_dir))
    pointer_path = target / POINTER_OBJECT_NAME
    cipher_path = target / ARCHIVE_OBJECT_NAME

    try:
        pointer_raw = artifacts.read_json(bucket, f'{prefix}/{POINTER_OBJECT_NAME}')
    except artifacts.ArtifactError as exc:
        raise BackupError(f'无法读取备份指针文件：{exc}') from exc

    if str(pointer_raw.get('backup_id') or '') != record.backup_id:
        raise BackupError('OSS 上的指针文件与备份记录不匹配，已中止恢复。')
    if str(pointer_raw.get('cluster_slug') or '') != record.slug:
        raise BackupError('OSS 上的指针文件属于其它集群，已中止恢复。')

    archive_info = pointer_raw.get('archive') or {}
    expected_sha = str(archive_info.get('sha256') or record.archive_sha256 or '').lower()
    expected_size = int(archive_info.get('size') or record.archive_size or 0)

    digest = hashlib.sha256()
    written = 0
    try:
        stream = bucket.get_object(f'{prefix}/{ARCHIVE_OBJECT_NAME}')
        with cipher_path.open('wb') as handle:
            while True:
                block = stream.read(IO_BLOCK_BYTES)
                if not block:
                    break
                digest.update(block)
                handle.write(block)
                written += len(block)
    except Exception as exc:
        _unlink(cipher_path)
        raise BackupError(f'从 OSS 下载备份失败：{exc}') from exc

    if expected_size and written != expected_size:
        _unlink(cipher_path)
        raise BackupError(f'下载到的备份大小为 {written} 字节，与指针记录的 {expected_size} 字节不一致，已中止恢复。')
    if expected_sha and digest.hexdigest() != expected_sha:
        _unlink(cipher_path)
        raise BackupError('下载到的备份内容哈希与指针记录不一致，OSS 上的对象可能已被替换，已中止恢复。')

    plaintext_path = target / f'{record.backup_id}.zip'
    decrypt_stream(cipher_path, plaintext_path)
    _unlink(pointer_path)
    _unlink(cipher_path)

    manifest = _extract_archive(plaintext_path, target)
    _unlink(plaintext_path)
    return {'archive': target, 'manifest': manifest}


def _extract_archive(archive_path, target_dir):
    """解压备份归档并返回内部 manifest。

    参数：`archive_path` 表示归档文件；`target_dir` 表示解压目标目录。
    返回：返回 manifest 字典。
    副作用：把归档内容写入目标目录。

    逐个成员做路径校验，拒绝绝对路径与向上穿越的条目（zip slip）。
    """
    destination = Path(target_dir)
    manifest = {}
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            if ARCHIVE_MANIFEST_NAME not in bundle.namelist():
                raise BackupError('备份归档里缺少 manifest.json，归档不完整。')
            manifest = json.loads(bundle.read(ARCHIVE_MANIFEST_NAME).decode('utf-8'))
            for item in bundle.infolist():
                if item.is_dir():
                    continue
                relative = Path(item.filename)
                if relative.is_absolute() or '..' in relative.parts:
                    raise BackupError(f'备份归档里存在越界路径：{item.filename}')
                out = destination / relative
                paths.ensure_dir(out.parent)
                with bundle.open(item) as source, out.open('wb') as handle:
                    shutil.copyfileobj(source, handle)
    except zipfile.BadZipFile as exc:
        raise BackupError('备份归档损坏，无法解压。') from exc
    return manifest


def place_snapshot(kubeasz_root, slug, extracted_dir, manifest):
    """把备份里的 snapshot 放回 kubeasz 约定位置。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识；
        `extracted_dir` 表示解压目录；`manifest` 表示内部 manifest。
    返回：返回 ``[{'path':.., 'sha256':.., 'size':..}]``。
    副作用：在 ``clusters/<slug>/backup/`` 下写入文件。

    规划 11.2 要求恢复**严格使用用户选择的 snapshot**，而不是 ``ezctl restore`` 默认的
    「本地最近一份」。因此这里先把选定的文件摆到约定位置，再由调用方显式覆盖目标变量。
    """
    source_dir = Path(extracted_dir) / ARCHIVE_SNAPSHOT_PREFIX.rstrip('/')
    if not source_dir.is_dir():
        raise BackupError('备份归档里没有 snapshot 数据，无法恢复。')

    target_dir = paths.ensure_dir(paths.cluster_dir(kubeasz_root, slug) / SNAPSHOT_DIR_NAME)
    # 先清掉目录里可能存在的其它 snapshot，避免 kubeasz 的"取最近一份"逻辑选错文件。
    for item in target_dir.iterdir():
        if item.is_file():
            _unlink(item)

    placed = []
    for item in sorted(source_dir.rglob('*')):
        if not item.is_file():
            continue
        relative = item.relative_to(source_dir)
        out = target_dir / relative
        paths.ensure_dir(out.parent)
        shutil.copy2(item, out)
        placed.append({'path': str(out), 'sha256': sha256_file(out), 'size': out.stat().st_size})
    if not placed:
        raise BackupError('备份归档里的 snapshot 目录为空，无法恢复。')
    return placed


def override_restore_target(kubeasz_root, snapshot_paths):
    """改写 cluster-restore 角色的默认变量，指向本次选定的 snapshot。

    参数：`kubeasz_root` 表示 kubeasz 源码根目录；`snapshot_paths` 表示 ``place_snapshot`` 的结果。
    返回：返回 ``{'file': 被改写的文件, 'variable': 变量名, 'value': 写入值}``。
    副作用：改写工作区里的角色默认文件。

    找不到可覆盖的变量时**必须失败**：继续跑下去就会落到 ``ezctl restore`` 默认的
    「取本地最近一份」行为，那正是规划明令禁止的（规划 11.2）。
    """
    target = Path(kubeasz_root).joinpath(*RESTORE_DEFAULTS_PATH)
    if not target.is_file():
        raise BackupError(
            f'kubeasz 源码里找不到 {"/".join(RESTORE_DEFAULTS_PATH)}，'
            '无法显式指定要恢复的 snapshot，已中止恢复以避免走到默认的"最近一份"行为。'
        )

    primary = Path(snapshot_paths[0]['path'])
    text = target.read_text(encoding='utf-8')
    pattern = re.compile(r'(?m)^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:\s*)(\S.*)$')
    chosen = None
    output = []
    for line in text.splitlines():
        match = pattern.match(line)
        if match and chosen is None and re.search(r'(?i)(backup|snapshot|restore)', match.group(2)):
            chosen = match.group(2)
            output.append(f'{match.group(1)}{chosen}: "{primary}"')
            continue
        output.append(line)
    if chosen is None:
        raise BackupError(
            f'{"/".join(RESTORE_DEFAULTS_PATH)} 里没有可识别的 backup/snapshot 变量，'
            '无法显式指定要恢复的 snapshot，已中止恢复。'
        )
    target.write_text('\n'.join(output) + '\n', encoding='utf-8')
    paths.secure_file(target)
    return {'file': str(target), 'variable': chosen, 'value': str(primary)}


def _requested_backup_id(deployment):
    """取出任务里指定的备份标识。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回备份标识文本；没有指定时返回空串。
    副作用：只读取对象字段。

    ``request_snapshot`` 有两个写入方：向导式的任务快照把提交内容放在 ``submitted`` 下，
    而手工构造的快照可能把 ``backup_id`` 直接放在顶层。两处都认，避免恢复任务因为
    「提交内容藏在哪一层」这种实现细节而在执行时才失败。
    """
    snapshot = deployment.request_snapshot or {}
    if not isinstance(snapshot, dict):
        return ''
    submitted = snapshot.get('submitted')
    candidates = [snapshot.get('backup_id')]
    if isinstance(submitted, dict):
        candidates.append(submitted.get('backup_id'))
    for item in candidates:
        value = str(item or '').strip()
        if value:
            return value
    return ''


def run_restore(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance):
    """执行一次指定备份的恢复。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录；`runner_instance` 表示执行器。
    返回：返回结果摘要字典。
    副作用：改动目标集群的 etcd 与集群配置，追加配置版本与健康记录。

    恢复开始前必须由用户在 API 层二次确认（手动输入 display_name），这里不再重复确认，
    但会把备份时间、版本与哈希写进事件，保证审计链完整（规划 11.2）。
    """
    from k8s_deployment.models import K8sClusterBackup

    cluster = deployment.cluster
    if cluster is None:
        raise BackupError('恢复任务没有关联集群。')

    requested_id = _requested_backup_id(deployment)
    if not requested_id:
        raise BackupError('恢复任务没有指定要恢复的备份，已中止。请重新选择一份备份后再提交。')
    record = K8sClusterBackup.objects.filter(cluster=cluster, backup_id=requested_id).first()
    if record is None:
        raise BackupError(f'没有找到备份 {requested_id}，它可能已被保留策略清理或被删除。')
    validate_restore_selection(cluster, record)

    if deployment.kubernetes_version and record.kubernetes_version \
            and str(deployment.kubernetes_version).lstrip('v') != str(record.kubernetes_version).lstrip('v'):
        raise BackupError('任务提交时选择的版本与备份记录的版本不一致，已中止恢复。')

    context.event('restore_selected', f'选定备份 {record.backup_id}（{record.kubernetes_version} / {record.kubeasz_version}）', detail={
        'backup_id': record.backup_id,
        'created_at': record.created_at.isoformat() if record.created_at else '',
        'archive_sha256': record.archive_sha256,
        'archive_size': record.archive_size,
    })

    context.begin_stage('restore-health', '恢复前探测集群状态')
    source_state = assess_restore_source(cluster, nodes, credentials_map)
    context.log('system', f'源集群状态：{source_state["message"]}')
    context.event(
        'restore_source_probe', source_state['message'],
        level='info' if source_state['healthy'] else 'warn',
        detail=source_state['detail'], stage='restore-health',
    )
    context.complete_stage('restore-health')

    # 应急备份：默认做一次，但在集群已经不可用时允许用户跳过（规划 11.1、11.2）。
    # 平台**不强制**要求它成功，否则集群一坏、备份必然失败，恢复就永远开不了工。
    if deployment.skip_emergency_backup:
        context.event(
            'restore_emergency_backup_skipped',
            '已按用户要求跳过恢复前的应急备份，集群当前状态不会被保留。'
            + ('探测显示集群当前仍可用，如需保留现状请单独发起一次备份任务。'
               if source_state['healthy'] else ''),
            level='warn',
            detail={
                'cluster_healthy': source_state['healthy'],
                'reason': '用户勾选「跳过应急备份」',
                'requested_by': deployment.requested_by,
                'probe': source_state['detail'],
            },
            stage='restore-health',
        )
        context.log('system', '已跳过恢复前的应急备份（用户要求）。')
    else:
        _try_emergency_backup(
            context, cluster, deployment, kubeasz_root, runner_instance, credentials_map, source_state,
        )

    context.begin_stage('download', '下载并校验备份')
    downloaded = download_backup(record, Path(context.workspace) / 'restore')
    manifest = downloaded['manifest']
    context.log('system', f'备份解密完成，包含 {len(manifest.get("files") or [])} 个文件')
    context.complete_stage('download')

    # 用备份里的配置覆盖工作区：恢复的目标状态就是备份那一刻的集群状态。
    #
    # 这份覆盖是**借**出去的：集群记录上的配置版本要等恢复跑成、``register_deployment_result``
    # 落地才会跟着变。所以它整段包在 try 里 —— 恢复失败时磁盘上的 inventory 必须还原。
    # 不还原就会出现「平台记录里 3 个节点、inventory 里只剩 2 个」的分裂：用户选的备份比集群
    # 现状旧（备份是在扩容之前做的）时，一次失败的恢复就能把新节点从清单里悄悄抹掉，
    # 而此后**备份会照着这份被抹掉的清单去归档**，那份备份再拿去恢复，节点就从记录里永久消失了。
    # 2026-10-12 的 k8s-single 就是这个形态：备份（10:45）早于 worker-02 加入（10:49），
    # 恢复失败后又把 worker-02 从 hosts 里留丢了。
    cluster_dir = paths.ensure_dir(paths.cluster_dir(kubeasz_root, cluster.slug))
    try:
        for name in ('hosts', 'config.yml'):
            source = downloaded['archive'] / ARCHIVE_CONFIG_PREFIX / name
            if source.is_file():
                (cluster_dir / name).write_text(
                    source.read_text(encoding='utf-8', errors='replace'), encoding='utf-8',
                )
                paths.secure_file(cluster_dir / name)

        context.begin_stage('place-snapshot', '放置指定 snapshot')
        placed = place_snapshot(kubeasz_root, cluster.slug, downloaded['archive'], manifest)
        override = override_restore_target(kubeasz_root, placed)
        # 执行前把最终文件路径与哈希写进日志：恢复出问题时这是唯一的对账依据（规划 11.2）。
        for item in placed:
            context.log(
                'system',
                f'待恢复 snapshot：{item["path"]}（sha256 {item["sha256"][:16]}…，{item["size"]} 字节）',
            )
        context.log('system', f'已覆盖 {override["file"]} 的 {override["variable"]} = {override["value"]}')
        context.event('restore_target_locked', '已显式锁定要恢复的 snapshot', detail=override)
        context.complete_stage('place-snapshot')

        _run_preflight(context, deployment, nodes, credentials_map)

        from . import runner as runner_module

        context.begin_stage('restore', '执行集群恢复')
        argv = runner_module.ezctl_argv(kubeasz_root, ['restore', cluster.slug])
        context.log('system', f'执行命令：{runner_module.format_command(argv)}')
        result = runner_instance.run_stage(
            'restore', argv, cwd=kubeasz_root,
            # 恢复剧本整本靠 ansible 登录节点（95.restore.yml 里没有一个 ``connection: local``
            # 的任务），部署私钥必须显式给：漏了它 ansible 就用平台机运维人员的 ``~/.ssh/id_rsa``，
            # 那把钥匙没下发到任何节点，剧本在 ``Gathering Facts`` 上就会
            # ``UNREACHABLE ... Permission denied (publickey,password)``。详见
            # ``state_machine.command_env``。
            env=state_machine.command_env(context, kubeasz_root),
            on_line=context.on_line, timeout=context.stage_budget(), is_cancelled=context.cancelled,
            # 与备份同理：恢复途中 etcd 会重启，剧本可能安静很久。
            on_idle=context.narrate_idle,
            on_tick=context.tick,
        )
        if result.cancelled:
            raise _abort('cancelled', '恢复已被取消。')
        if not result.ok:
            raise _abort(
                'restore_failed',
                result.describe('集群恢复') + '。平台不会自动换备份或降级，请人工确认集群状态。',
            )
        context.complete_stage('restore', elapsed=result.duration)

        register.stage_kubeconfig(kubeasz_root, cluster.slug, context.workspace)
        context.begin_stage('register', '登记恢复结果')
        hosts_path = paths.hosts_path(kubeasz_root, cluster.slug)
        config_path = paths.config_path(kubeasz_root, cluster.slug)
        compile_result = {
            'config_hash': _config_hash(hosts_path, config_path),
            'template': 'restore',
            'resolved_versions': {},
            'hosts_text': hosts_path.read_text(encoding='utf-8', errors='replace') if hosts_path.is_file() else '',
            'config_text': config_path.read_text(encoding='utf-8', errors='replace') if config_path.is_file() else '',
        }
        summary = register.register_deployment_result(
            deployment, compile_result, nodes, kubeasz_root=kubeasz_root,
            note=f'从备份 {record.backup_id} 恢复（任务 #{deployment.pk}）',
        )
        if not summary['health']['ok']:
            raise _abort('health_check_failed', summary['health']['message'])
        context.log('system', f'健康检查通过：{summary["health"]["message"]}')
        context.complete_stage('register')
    except BaseException:
        # 连 KeyboardInterrupt / 进程被信号打断都算：取消一次恢复同样不能把 inventory 留在备份那一版。
        _rollback_cluster_config(context, cluster, kubeasz_root)
        raise
    summary['backup_id'] = record.backup_id
    return summary


def _rollback_cluster_config(context, cluster, kubeasz_root):
    """把集群目录里的 hosts 与 config.yml 还原成集群记录里那一版。

    参数：`context` 表示任务上下文；`cluster` 表示集群；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：无显式返回值。
    副作用：重写 ``clusters/<slug>/hosts`` 与 ``config.yml``，并写一行日志。

    直接复用 ``materialize_cluster_config``（它本来就负责「按记录里最新的配置版本重写这两个
    文件」）：恢复失败时记录没有被推进，写回去的就正好是恢复前那一份。这样不必另外暂存副本，
    也不会在任务工作区里留下凭据无关却又敏感的文件。

    还原失败不能盖掉原来的异常 —— 那个异常带着用户真正需要看的失败原因 —— 所以这里只把
    还原的失败写进日志，让任务结果如实保留。
    """
    try:
        state_machine.materialize_cluster_config(cluster, kubeasz_root, context.workspace)
    except Exception as exc:  # noqa: BLE001 - 还原尽力而为，失败原因写日志即可
        context.log(
            'system',
            f'集群配置（hosts / config.yml）还原失败：{exc}。'
            f'请人工核对 {paths.cluster_dir(kubeasz_root, cluster.slug)} 下的这两个文件。',
        )
        return
    context.log('system', '恢复未成功，已把集群配置（hosts / config.yml）还原成记录里那一版。')


def assess_restore_source(cluster, nodes, credentials_map):
    """恢复前探测源集群是否健康（规划 11.1）。

    参数：`cluster` 表示 K8sCluster 实例；`nodes` 表示节点列表；`credentials_map` 表示连接信息。
    返回：返回 ``{'healthy': bool, 'message': str, 'detail': dict}``。
    副作用：向 Kubernetes API 发起一次请求，并可能建立一次短 SSH 连接读取 etcd 状态。

    「健康」= API Server 能应答 **且** etcd 明确还有仲裁。判不出来的一律算不健康：
    恢复是最后手段，一个说不清状态的集群更应该允许恢复而不是被平台挡在门外。
    """
    from k8s_cluster.services import probe_cluster
    from . import preflight

    detail = {}
    api_ok = False
    try:
        state = probe_cluster(cluster)
        api_ok = True
        detail['api'] = {
            'ok': True,
            'message': str(state.get('message') or ''),
            'kube_version': state.get('kube_version') or '',
            'not_ready': (state.get('node_summary') or {}).get('not_ready') or [],
        }
    except Exception as exc:
        detail['api'] = {'ok': False, 'message': str(exc)}

    etcd_state, etcd_detail = preflight.etcd_quorum_state(nodes, credentials_map)
    detail['etcd'] = dict(etcd_detail)
    if etcd_state is None:
        etcd_status, etcd_message = 'warned', detail['etcd'].get('reason') or '未能查询 etcd 仲裁状态'
    else:
        etcd_status, etcd_message = preflight.etcd_quorum_conclusion(etcd_state)
        detail['etcd'].update(etcd_state)

    healthy = api_ok and etcd_status == 'passed'
    if healthy:
        message = '探测结果：API Server 可应答，etcd 仲裁正常，集群当前健康'
    else:
        parts = []
        if not api_ok:
            parts.append('API Server 无法应答')
        if etcd_status == 'failed':
            parts.append('etcd 已失去仲裁')
        elif etcd_status == 'warned':
            parts.append(f'etcd 状态未能判定（{etcd_message}）')
        message = '探测结果：' + '，'.join(parts or ['集群状态不明确']) + '，将按不健康集群处理'
    return {'healthy': healthy, 'message': message, 'detail': detail}


def _try_emergency_backup(context, cluster, deployment, kubeasz_root, runner_instance,
                          credentials_map, source):
    """尝试在恢复前做一次应急备份，失败只记录不阻断（规划 11.1）。

    参数：`context` 表示任务上下文；`cluster` 表示集群；`deployment` 表示任务；
        `kubeasz_root` 表示 kubeasz 源码根目录；`runner_instance` 表示执行器；
        `credentials_map` 表示连接信息；`source` 表示 ``assess_restore_source`` 的结果。
    返回：返回新建的备份记录；未做成时返回 None。
    副作用：执行 ``ezctl backup`` 并把加密归档上传 OSS；写入备份记录与任务事件。

    集群不健康时备份大概率也做不成，此时**必须**继续恢复——规划 11.1 明确不要为恢复强制
    应急备份，否则「集群坏了 → 备份失败 → 不能恢复」会形成死锁。备份失败本身在备份记录上
    是 failed 状态，不会被误记成成功。
    """
    context.begin_stage('emergency-backup', '恢复前应急备份')
    try:
        record = create_backup(
            cluster, deployment=deployment, context=context, kubeasz_root=kubeasz_root,
            runner_instance=runner_instance, credentials_map=credentials_map,
        )
    except Exception as exc:
        context.log('system', f'应急备份未完成：{exc}')
        context.event(
            'restore_emergency_backup_failed',
            f'恢复前的应急备份未完成：{exc}。恢复将继续进行——平台不为恢复强制要求应急备份，'
            '以免集群已故障时永远无法恢复；本次恢复没有可回退的备份点。',
            level='warn', stage='emergency-backup',
        )
        context.complete_stage('emergency-backup')
        return None
    context.log('system', f'应急备份完成：{record.backup_id}')
    context.event(
        'restore_emergency_backup',
        f'恢复前已备份集群当前状态：{record.backup_id}'
        + ('（集群探测为健康）' if source['healthy'] else '（集群探测为不健康，该备份可能不完整）'),
        detail={'backup_id': record.backup_id, 'archive_sha256': record.archive_sha256},
        stage='emergency-backup',
    )
    context.complete_stage('emergency-backup')
    return record


def _config_hash(hosts_path, config_path):
    """计算两个配置文件的归并哈希。

    参数：`hosts_path` 表示 inventory 路径；`config_path` 表示集群变量路径。
    返回：返回十六进制摘要。
    副作用：只读取磁盘文件，不修改内容。
    """
    from . import compiler

    hosts_text = hosts_path.read_text(encoding='utf-8', errors='replace') if hosts_path.is_file() else ''
    config_text = config_path.read_text(encoding='utf-8', errors='replace') if config_path.is_file() else ''
    return compiler.config_hash(hosts_text, config_text)


def _run_preflight(context, deployment, nodes, credentials_map):
    """恢复前的环境预检。

    参数：`context` 表示任务上下文；`deployment` 表示任务；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息。
    返回：返回预检结果。
    副作用：与目标节点建立 SSH 连接并执行只读命令。

    预检的端口检查会命中已被 etcd 占用的 2379，因此这里只把预检当作参考信息记录，
    失败不阻断恢复：集群已经坏了，能恢复比环境干净更重要（规划 11.1 的同类取舍）。
    """
    from . import preflight

    context.begin_stage('preflight', '恢复前环境检查')
    try:
        result = preflight.run_preflight(deployment, nodes, credentials_map)
    except Exception as exc:
        context.log('system', f'预检未能完成：{exc}（不影响恢复继续进行）')
        context.complete_stage('preflight')
        return {'ok': True, 'checks': []}
    for check in result['checks']:
        context.log('system', f'[{check["status"]}] {check["title"]}：{check["message"]}')
    if not result['ok']:
        context.event(
            'restore_preflight_warning',
            '恢复前的环境检查存在失败项，但恢复操作不因此中止：' + '；'.join(result['hard_failures']),
            level='warn',
            stage='preflight',
        )
    context.complete_stage('preflight')
    return result


def _abort(error_code, message):
    """构造状态机的任务中止异常。

    参数：`error_code` 表示错误码；`message` 表示中文原因。
    返回：返回 ``state_machine.TaskAborted`` 实例。
    副作用：延迟导入状态机模块，不修改持久化数据。

    这里必须抛**状态机自己的** ``TaskAborted``：状态机按它决定任务落到
    ``failed`` 还是 ``rollback_required``，抛别的异常类型会被当成「未预期错误」，
    恢复失败就会被错记成普通失败，而不是需要人工回滚的高危终态（规划 11.2）。
    """
    from .state_machine import TaskAborted

    return TaskAborted(error_code, message)


def oss_usage():
    """统计备份在 OSS 上的对象数量与总大小。

    参数：无。
    返回：返回 ``{'count': 对象数, 'bytes': 总字节数}``。
    副作用：读取 OSS 对象列表，不修改任何对象。

    新旧两套前缀都要数：路径改版之前创建的备份仍在旧前缀下，只看新前缀会让用量凭空
    少一截，保留策略据此做的判断也跟着失真。
    """
    bucket = artifacts.oss_bucket()
    config = artifacts.oss_config()
    prefixes = (
        f'{backup_base_dir(config)}/',
        f'{artifacts.base_dir(config)}/{LEGACY_BACKUP_DIR_NAME}/',
    )
    count = 0
    total = 0
    for prefix in prefixes:
        for item in bucket.list_objects(prefix=prefix).object_list:
            count += 1
            total += int(item.size or 0)
    return {'count': count, 'bytes': total}


def revision_snapshots(cluster, revisions):
    """按版本号批量读取某个集群的配置快照。

    参数：`cluster` 表示 K8sCluster 实例；`revisions` 表示版本号可迭代对象。
    返回：返回 ``{版本号: snapshot}``，查不到的版本号不在结果里。
    副作用：执行一次数据库查询，不修改数据。

    备份列表一次要展示很多条记录，每条都按版本号单查会让列表接口退化成 N+1，
    因此由调用方一次取回。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    wanted = sorted({int(value or 0) for value in revisions} - {0})
    if cluster is None or not wanted:
        return {}
    rows = K8sClusterConfigRevision.objects.filter(
        cluster=cluster, revision__in=wanted,
    ).values_list('revision', 'snapshot')
    return {int(number): (snapshot or {}) for number, snapshot in rows}


def backup_nodes(record, revisions=None):
    """读取备份时那一版配置里记录的节点清单。

    参数：`record` 表示 K8sClusterBackup 实例；`revisions` 表示可选的 ``{版本号: snapshot}``
    映射，列表场景由调用方一次查出后传入。
    返回：返回 ``[{'name', 'ip_address', 'roles'}]``；版本号为 0 或版本记录已不存在时返回空列表。
    副作用：`revisions` 为 None 时执行一次数据库查询，不修改数据。

    规划 11.2 要求恢复确认弹窗显示备份时的节点：备份归档里装的是 kubeasz 快照与
    hosts/config.yml，节点清单在平台侧对应的就是同一版本的配置快照。这里只返回主机名、
    IP 与角色——足够用户确认「恢复的是哪一批机器」，不牵出任何凭据。
    """
    revision = int(getattr(record, 'config_revision', 0) or 0)
    if not revision:
        return []
    if revisions is None:
        revisions = revision_snapshots(getattr(record, 'cluster', None), [revision])
    snapshot = revisions.get(revision) or {}
    result = []
    for item in snapshot.get('nodes') or []:
        result.append({
            'name': str(item.get('name') or ''),
            'ip_address': str(item.get('ip_address') or ''),
            'roles': list(item.get('roles') or []),
        })
    return result


def backup_summary(record, nodes=None):
    """把备份记录整理成前端可直接展示的结构。

    参数：`record` 表示 K8sClusterBackup 实例；`nodes` 表示已查好的备份时节点清单，
    列表场景传入以避免逐条备份各查一次版本快照。
    返回：返回字典；不含任何密钥。
    副作用：`nodes` 为 None 时执行一次数据库查询，不修改数据。
    """
    return {
        'id': record.pk,
        'backup_id': record.backup_id,
        'cluster_id': record.cluster_id,
        'slug': record.slug,
        'kubernetes_version': record.kubernetes_version,
        'kubeasz_version': record.kubeasz_version,
        'config_revision': record.config_revision,
        'archive_size': record.archive_size,
        'archive_sha256': record.archive_sha256,
        'nodes': backup_nodes(record) if nodes is None else nodes,
        'schema_version': record.schema_version,
        'status': record.status,
        'status_display': record.get_status_display(),
        'failure_reason': record.failure_reason,
        'is_protected': record.is_protected,
        'object_prefix': record.object_prefix,
        'deployment_id': record.deployment_id,
        'created_at': record.created_at.isoformat() if record.created_at else '',
        'finished_at': record.finished_at.isoformat() if record.finished_at else '',
    }


def storage_root():
    """返回备份临时文件的根目录。

    参数：无。
    返回：返回 ``<状态根>/backups``。
    副作用：只做路径计算，不创建目录。
    """
    return paths.state_root() / 'backups'


def cleanup_stale_temp(max_age_seconds=86400):
    """清理历史遗留的备份临时目录。

    参数：`max_age_seconds` 表示超过多久未改动的目录算残留。
    返回：返回被清理的目录数量。
    副作用：删除磁盘目录。

    正常路径下 finally 已经清理干净，这里兜住 Worker 被强杀留下的残渣（规划 7.4）：
    明文 snapshot 留在盘上等于绕过了整个加密设计，因此这个兜底必须存在。
    """
    root = storage_root()
    if not root.exists():
        return 0
    removed = 0
    deadline = time.time() - float(max_age_seconds)
    for item in root.iterdir():
        try:
            if item.is_dir() and item.stat().st_mtime < deadline:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
