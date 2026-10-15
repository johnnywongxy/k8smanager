"""从 OSS 获取 kubeasz 制品：清单读取、下载与 SHA-256 校验、内容寻址缓存、源码解压自检。

制品由 ``ops/management/commands/sync_kubeasz_artifacts.py`` 同步到 OSS，目录约定为::

    <base_dir>/index.json                                  所有 release 的总览
    <base_dir>/releases/<release>/manifest.json            单个 release 的详细清单
    <base_dir>/releases/<release>/commands/{ezctl,ezdown}  控制脚本
    <base_dir>/releases/<release>/source/kubeasz-<r>.zip   源码包（含 example/ 与 playbooks/）
    <base_dir>/releases/<release>/images/*.tar             离线镜像
    <base_dir>/releases/<release>/helm/helm_<arch>         Helm CLI

下载一律先落到内容寻址缓存 ``<缓存根>/<sha 前两位>/<完整 sha>``，再按需解压到任务工作区，
因此同一个制品被多个任务使用时只下载一次。

**为什么要在解压后做结构自检**：早期同步脚本用的 gitee 归档地址会返回 HTML 页面，
脚本只判断了 HTTP 是否成功，于是把一个 46KB 的网页当成源码包传上了 OSS。这里在解压前
校验 zip 魔数、解压后校验关键文件是否存在，结构不符就在任务早期明确失败，而不是等到
ansible 跑到一半才报一个看不懂的错。
"""

import hashlib
import io
import os
import shutil
import time
import zipfile
from pathlib import Path

from django.conf import settings

from data_security.sm4 import SM4Error

from . import paths

# OSS 上制品目录的默认前缀，与同步脚本的 normalize_base_dir 保持一致。
DEFAULT_BASE_DIR = 'kubeasz'
# 解压后的源码根目录里必须存在的文件，缺任何一个都说明源码包不完整。
REQUIRED_SOURCE_FILES = (
    'ezctl',
    'ezdown',
    'example/config.yml',
    'example/hosts.multi-node',
    'example/hosts.allinone',
    'playbooks/01.prepare.yml',
    'playbooks/07.cluster-addon.yml',
)
# kubeasz 安装目录里的版本标记。共用一份目录之后，光看「结构完整」已经不能说明它就是这个版本，
# 这个文件是解压与装二进制都做完后**最后**写的，它存在就等于「这棵目录装好了」。
SOURCE_MARKER_NAME = '.kubeasz-source.json'
# kubeasz 安装目录下指向持久集群目录的入口名。ezctl 与 ansible 都按 clusters/<集群名>/ 找它。
CLUSTERS_LINK_NAME = 'clusters'
# 下载与解压的分块大小，避免把几百 MB 的镜像 tar 一次读进内存。
CHUNK_SIZE = 1024 * 1024
# 下载进度旁白的间隔（秒）。源码包几十 MB、镜像 tar 几百 MB 到 1 GB，一次下载可能十几分钟
# 不吭声，进度页上就只能显示「什么都没发生」。报得太密又会把日志表刷满，10 秒是这两者之间的取中。
DOWNLOAD_PROGRESS_SECONDS = 10


class ArtifactError(Exception):
    """制品获取失败，携带可直接展示给用户的中文原因。"""


def clean_version(value):
    """把版本号规范成去掉前导 v 的纯数字形式。

    参数：`value` 表示原始版本号文本。
    返回：返回去除首尾空白与前导 v 后的字符串。
    副作用：只做纯计算，不修改持久化数据。
    """
    return str(value or '').strip().lstrip('v')


def sha256_file(path):
    """计算文件的 SHA-256 摘要。

    参数：`path` 表示待计算的文件路径。
    返回：返回十六进制摘要字符串。
    副作用：读取磁盘文件内容，不修改它。
    """
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def oss_config():
    """读取并校验平台的 OSS 配置。

    参数：无。
    返回：返回 `storage.oss` 配置字典。
    副作用：只读取数据库中的平台设置，不修改任何数据。

    ``storage.oss`` 里的 access_key_secret 是 SM4 加密存的，密钥来自
    ``DJANGO_SECRET_KEY`` / ``SM4_MASTER_KEYS``。Web 与 Worker 的密钥不一致时解不开，
    原始异常只有一句 ``SM4 ciphertext authentication failed``，看不出该改哪里，因此这里
    翻译成一句能直接照做的说明。
    """
    from ops.models import SystemSetting

    setting = SystemSetting.objects.filter(key='storage.oss').first()
    try:
        config = setting.value if setting else {}
    except SM4Error as exc:
        raise ArtifactError(
            '平台设置里的 OSS 配置无法解密：库里的密文与当前进程使用的密钥对不上。'
            '最常见的原因是启动 Web/Worker 时没有加载平台的环境变量（于是用了代码里的开发'
            '占位密钥），或者 DJANGO_SECRET_KEY / SM4_MASTER_KEYS 中途换过。请让两端使用同一'
            '套密钥后，在平台设置里重新保存一次 OSS 配置。'
        ) from exc
    if not isinstance(config, dict) or not config.get('enabled'):
        raise ArtifactError('平台设置里的 OSS 存储未启用，无法获取 kubeasz 制品。')
    required = ('access_key_id', 'access_key_secret', 'bucket', 'endpoint')
    missing = [key for key in required if not str(config.get(key) or '').strip()]
    if missing:
        raise ArtifactError('OSS 配置缺少必要项，请在平台设置中补全后重试。')
    return config


def base_dir(config):
    """返回制品在 OSS 上的目录前缀。

    参数：`config` 表示 OSS 配置字典。
    返回：返回去掉首尾斜线的目录前缀，未配置时使用 kubeasz。
    副作用：只做纯计算，不修改持久化数据。
    """
    value = str(config.get('base_dir') or DEFAULT_BASE_DIR).strip().strip('/')
    if not value or '..' in value.split('/'):
        return DEFAULT_BASE_DIR
    return value


def _endpoint(config):
    """把 OSS 配置里的 endpoint 补全成带协议的地址。

    参数：`config` 表示 OSS 配置字典。
    返回：返回完整的 endpoint URL。
    副作用：只做纯计算，不修改持久化数据。
    """
    endpoint = str(config.get('endpoint') or '').strip().rstrip('/')
    if not endpoint:
        raise ArtifactError('OSS 配置缺少 endpoint。')
    if endpoint.startswith(('http://', 'https://')):
        return endpoint
    scheme = 'https' if config.get('use_https', True) else 'http'
    return f'{scheme}://{endpoint}'


def oss_bucket(config=None):
    """按配置构造 OSS Bucket 客户端。

    参数：`config` 表示 OSS 配置字典，传 None 时自动读取平台设置。
    返回：返回 oss2.Bucket 实例。
    副作用：导入 oss2 模块并建立客户端对象，不发起网络请求。
    """
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读原因
        raise ArtifactError('当前环境未安装 oss2，无法访问 OSS 制品。') from exc
    config = config or oss_config()
    auth = oss2.Auth(config['access_key_id'], config['access_key_secret'])
    return oss2.Bucket(auth, _endpoint(config), config['bucket'])


def read_json(bucket, key):
    """读取 OSS 上的一个 JSON 对象。

    参数：`bucket` 表示 OSS Bucket 客户端；`key` 表示对象键。
    返回：返回解析后的 Python 对象。
    副作用：发起一次网络读取，不修改 OSS 上的任何对象。
    """
    import json

    try:
        payload = bucket.get_object(key).read()
    except Exception as exc:
        raise ArtifactError(f'读取 OSS 对象失败：{key}（{exc}）') from exc
    try:
        return json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactError(f'OSS 对象不是合法 JSON：{key}') from exc


def artifact_index(bucket=None):
    """读取 OSS 上的 kubeasz 制品总览。

    参数：`bucket` 表示 OSS Bucket 客户端，传 None 时自动构造。
    返回：返回 index.json 解析后的字典。
    副作用：发起一次网络读取，不修改 OSS 上的任何对象。
    """
    bucket = bucket or oss_bucket()
    config = oss_config()
    return read_json(bucket, f'{base_dir(config)}/index.json')


def release_manifest(release, bucket=None):
    """读取某个 kubeasz 版本在 OSS 上的详细清单。

    参数：`release` 表示 kubeasz 版本号；`bucket` 表示 OSS Bucket 客户端，传 None 时自动构造。
    返回：返回 manifest.json 解析后的字典。
    副作用：发起一次网络读取，不修改 OSS 上的任何对象。
    """
    bucket = bucket or oss_bucket()
    config = oss_config()
    version = clean_version(release)
    return read_json(bucket, f'{base_dir(config)}/releases/{version}/manifest.json')


def supported_system_tokens(release, bucket=None):
    """返回某个 kubeasz 版本提供离线安装包的操作系统 token 集合。

    参数：`release` 表示 kubeasz 版本号；`bucket` 表示 OSS Bucket 客户端，传 None 时自动构造。
    返回：返回形如 {'rocky_8', 'ubuntu_22'} 的集合，清单里没有该信息时返回空集合。
    副作用：发起一次网络读取，不修改数据。
    """
    manifest = release_manifest(release, bucket=bucket)
    tokens = manifest.get('selected_systems') or manifest.get('system_package_tokens') or []
    return {str(item) for item in tokens if str(item).strip()}


# 附加组件到「镜像特征串」的映射：判断某个 release 是否随包发布了该组件的镜像。
# 用镜像名做子串匹配而不是去读 ezdown 里 extra option 的键名，是因为 kubeasz 的镜像名
# （``kubeasz-ingress-nginx-controller``、``calico-node`` 等）跨版本稳定，而 extra option
# 的键名在版本之间改过；匹配错一个键名会让能力判定整体失真。
# 1.33/3.6.7 的制品里没有 Ingress-NGINX，1.34 以后才有，差别就体现在这份镜像清单上。
ADDON_IMAGE_PATTERNS = {
    'calico': ('calico',),
    'flannel': ('flannel',),
    'metrics_server': ('metrics-server',),
    'ingress': ('ingress-nginx',),
}


def _published_image_names(manifest):
    """汇总清单里「确实发布了镜像」的证据。

    参数：`manifest` 表示 release 清单字典。
    返回：返回小写镜像名（或镜像 tar 的文件名）集合。

    只看 ``image_archives`` 和对象键，不看 ``default_images``：后者是 ezdown 声明「需要哪些
    镜像」，不等于同步脚本真的把它们传上了 OSS。把「声明」当「已发布」会让能力判定虚高，
    于是缺失的组件被放行、任务跑到一半才失败——这正是本节要提前拦住的。
    """
    names = set()
    for item in manifest.get('image_archives') or []:
        if isinstance(item, dict) and item.get('image'):
            names.add(str(item['image']).lower())
    for item in manifest.get('objects') or []:
        key = str(item.get('key') or '') if isinstance(item, dict) else ''
        if '/images/' in key:
            # 对象键里的镜像名带分隔符替换（``easzlab_kubeasz-ingress-nginx-controller_v1.11.2.tar``），
            # 但特征串本身不含分隔符，直接子串匹配同样命中。
            names.add(Path(key).name.lower())
    return names


def addon_availability(release, bucket=None):
    """返回某个 release 随包发布了哪些附加组件的镜像。

    参数：`release` 表示 kubeasz 版本号；`bucket` 表示 OSS Bucket 客户端，传 None 时自动构造。
    返回：返回形如 ``{'calico': True, 'ingress': False, 'helm': True, ...}`` 的字典；
        清单里没有任何镜像发布记录时返回 None，表示「无法判定」。
    副作用：发起一次网络读取，不修改任何数据。
    """
    manifest = release_manifest(release, bucket=bucket)
    names = _published_image_names(manifest)
    if not names:
        return None
    availability = {}
    for addon, patterns in ADDON_IMAGE_PATTERNS.items():
        availability[addon] = any(pattern in name for name in names for pattern in patterns)
    # Helm 不是镜像而是解压出来的二进制，单独看 helm/ 目录下的对象。
    availability['helm'] = bool(manifest.get('helm_artifacts')) or any(
        '/helm/helm_' in str(item.get('key') or '')
        for item in manifest.get('objects') or []
        if isinstance(item, dict)
    )
    return availability


def missing_addon_artifacts(release, *, cni_plugin='', metrics_server=False,
                            nginx_ingress=False, install_helm_cli=False, bucket=None):
    """列出本次部署要用、但该 release 没有发布制品的附加组件。

    参数：`release` 表示 kubeasz 版本号；`cni_plugin` 表示所选 CNI 插件；
        `metrics_server`/`nginx_ingress`/`install_helm_cli` 表示本次是否安装这些组件；
        `bucket` 表示 OSS Bucket 客户端，传 None 时自动构造。
    返回：返回 ``(缺失项列表, 无法判定原因)``，缺失项形如 ``(字段名, 中文描述)``。
        两个都为空/空串表示已确认制品齐全；原因非空表示这次没判成，缺失列表无意义。
    副作用：发起一次网络读取，不修改任何数据。

    **读不到清单时不在这里拦人**：创建任务只是把请求记下来，真正的制品下载发生在 Worker 侧。
    若这里 fail-closed，一次 OSS 抖动就会让所有人都提交不了部署；而清单能读到、只是少了某个
    组件的情况，才是规划 4.3 明确要求「创建任务直接 400」的那种确定性缺失。
    """
    try:
        availability = addon_availability(release, bucket=bucket)
    except Exception as exc:
        return [], str(exc)
    if availability is None:
        return [], f'kubeasz {release} 的清单里没有镜像发布记录，无法判定附加组件是否齐全。'

    wanted = []
    plugin = str(cni_plugin or '').strip().lower()
    if plugin in ADDON_IMAGE_PATTERNS:
        wanted.append((plugin, f'CNI 插件 {plugin}'))
    if metrics_server:
        wanted.append(('metrics_server', 'Metrics Server'))
    if nginx_ingress:
        wanted.append(('ingress', 'Nginx Ingress'))
    if install_helm_cli:
        wanted.append(('helm', 'Helm CLI'))

    missing = []
    for addon, label in wanted:
        if availability.get(addon):
            continue
        patterns = ADDON_IMAGE_PATTERNS.get(addon)
        hint = f'（制品里缺少 {"、".join(patterns)}）' if patterns else '（制品里缺少 helm/ 目录）'
        missing.append((addon, f'{label}{hint}'))
    return missing, ''


def _object_item(manifest, key):
    """在清单里按对象键找到对应的条目。

    参数：`manifest` 表示 release 清单；`key` 表示目标对象键。
    返回：返回该条目字典，找不到时返回空字典。
    副作用：只做纯计算，不修改持久化数据。
    """
    for item in manifest.get('objects') or []:
        if isinstance(item, dict) and item.get('key') == key:
            return item
    return {}


def human_size(size):
    """把字节数写成便于阅读的容量文本。

    参数：`size` 表示字节数。
    返回：返回「1.2 GB」这类中文可读文本；取值不合法时返回「大小未知」。
    副作用：纯计算，不修改持久化数据。
    """
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return '大小未知'
    if value >= 1024 ** 3:
        return f'{value / 1024 ** 3:.2f} GB'
    if value >= 1024 ** 2:
        return f'{value / 1024 ** 2:.1f} MB'
    if value >= 1024:
        return f'{value / 1024:.0f} KB'
    return f'{int(value)} B'


def download_progress(context, label):
    """构造一个下载进度回调，把「已经下了多少」写成一行日志。

    参数：`context` 表示任务上下文（只用到它的 ``say``）；`label` 表示正在下载什么（中文）。
    返回：返回签名为 ``(received, total)`` 的回调；``total`` 为 0 时只报已下载量。
    副作用：每次调用都会向任务的日志表写一行。

    放在本模块是因为它服务于下载这件事本身，而两个下载入口（本模块与 ``registry``）都要用；
    精度只到「已下载多少」，够看出「在动」，又不至于把一个几十 MB 的包刷出上百行日志。
    """
    def report(received, total):
        """把一次下载进度写进日志。

        参数：`received` 表示已下载字节数；`total` 表示总字节数，未知时为 0。
        返回：无显式返回值。
        副作用：向任务日志写一行。
        """
        if total:
            context.say(f'{label}：已下载 {human_size(received)} / {human_size(total)}')
        else:
            context.say(f'{label}：已下载 {human_size(received)}')

    return report


def _stream_length(stream):
    """尽力读出 OSS 对象的总字节数。

    参数：`stream` 表示 ``bucket.get_object`` 返回的对象流。
    返回：返回总字节数；拿不到时返回 0（调用方退化成只报已下载量）。
    副作用：只读对象属性，不修改持久化数据。
    """
    value = getattr(stream, 'content_length', None)
    if isinstance(value, int) and value > 0:
        return value
    headers = getattr(stream, 'headers', None) or {}
    try:
        return max(int(headers.get('content-length') or 0), 0)
    except (TypeError, ValueError):
        return 0


def _download_to_cache(bucket, key, expected_sha='', on_progress=None):
    """把一个 OSS 对象下载到内容寻址缓存并校验哈希。

    参数：`bucket` 表示 OSS Bucket 客户端；`key` 表示对象键；`expected_sha` 表示清单里记录的 SHA-256；
        `on_progress` 表示进度回调，签名为 ``(received, total)``，可为空。
    返回：返回 {'path': Path, 'sha256': str, 'size': int, 'cache_hit': bool}。
    副作用：在缓存目录中写入文件；校验失败时会删除已下载的临时文件。

    命中缓存时不会调用 ``on_progress``：那是一次本地哈希比对，没有「下到哪儿了」可言。
    """
    expected = str(expected_sha or '').strip().lower()
    if expected:
        cached = paths.artifact_cache_path(expected)
        if cached.exists() and sha256_file(cached) == expected:
            return {
                'path': cached,
                'sha256': expected,
                'size': cached.stat().st_size,
                'cache_hit': True,
            }

    try:
        stream = bucket.get_object(key)
    except Exception as exc:
        raise ArtifactError(f'从 OSS 下载制品失败：{key}（{exc}）') from exc

    digest = hashlib.sha256()
    buffer = io.BytesIO()
    total = _stream_length(stream)
    reported_at = time.monotonic()
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        buffer.write(chunk)
        if on_progress and (time.monotonic() - reported_at) >= DOWNLOAD_PROGRESS_SECONDS:
            reported_at = time.monotonic()
            try:
                on_progress(buffer.tell(), total)
            except Exception:
                # 回调异常绝不能中断下载：少一行进度日志远比重新下一遍轻。
                pass
    actual = digest.hexdigest()
    size = buffer.tell()

    if expected and actual != expected:
        raise ArtifactError(
            f'制品哈希校验失败：{key}，期望 {expected[:12]}…，实际 {actual[:12]}…。'
            'OSS 上的内容可能已损坏，请重新同步制品后再部署。'
        )

    target = paths.artifact_cache_path(actual)
    paths.ensure_dir(target.parent)
    # 先写临时文件再原子改名，避免并发任务读到写了一半的缓存。
    temporary = target.with_suffix('.partial')
    with temporary.open('wb') as handle:
        handle.write(buffer.getvalue())
    temporary.replace(target)
    return {'path': target, 'sha256': actual, 'size': size, 'cache_hit': False}


def fetch_artifact(bucket, key, expected_sha='', on_progress=None):
    """把一个 OSS 制品取到本机缓存，供部署流水线里的镜像分发使用。

    参数：`bucket` 表示 OSS Bucket 客户端；`key` 表示对象键；`expected_sha` 表示清单里记录的 SHA-256；
        `on_progress` 表示进度回调，签名为 ``(received, total)``，可为空。
    返回：返回 ``_download_to_cache`` 的结果字典。
    副作用：可能从 OSS 下载文件到内容寻址缓存。

    镜像 tar 与源码包走同一套缓存：同一个版本重复部署时只下载一次。
    """
    return _download_to_cache(bucket, key, expected_sha, on_progress)


def _validate_source_layout(root):
    """检查解压后的 kubeasz 源码结构是否完整。

    参数：`root` 表示解压后的 kubeasz 根目录。
    返回：返回缺失文件的相对路径列表，完整时返回空列表。
    副作用：只读取目录结构，不修改文件。
    """
    return [item for item in REQUIRED_SOURCE_FILES if not (Path(root) / item).exists()]


def _extract_zip(archive, destination, wipe=True):
    """把 zip 归并解压到目标目录，并剥掉顶层的版本目录。

    参数：`archive` 表示 zip 文件路径；`destination` 表示解压目标目录；
        `wipe` 表示解压前是否整目录清空。
    返回：返回解压后的目标目录。
    副作用：写入解压出的全部文件；`wipe` 为真时还会删除目标目录里的既有内容。

    共用的 kubeasz 安装目录不能整目录清空 —— 里面的 ``clusters`` 通向集群证书，清空的活由
    调用方用 ``_clear_tree_contents`` 精确地做，这里只负责把文件铺进去。
    """
    if not zipfile.is_zipfile(archive):
        raise ArtifactError(
            'kubeasz 源码包不是合法的 zip 归档，OSS 上的制品可能已损坏。'
            '请重新执行制品同步后再部署。'
        )
    destination = Path(destination)
    if wipe:
        paths.remove_tree(destination)
    paths.ensure_dir(destination)
    with zipfile.ZipFile(archive) as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        # 归档来自 GitHub 打包，顶层固定是 kubeasz-<版本> 一层，剥掉它让工作区结构稳定。
        prefixes = {item.filename.split('/', 1)[0] for item in members if '/' in item.filename}
        strip = prefixes.pop() if len(prefixes) == 1 else ''
        for item in members:
            relative = item.filename
            if strip and relative.startswith(f'{strip}/'):
                relative = relative[len(strip) + 1:]
            if not relative or relative.startswith('/') or '..' in Path(relative).parts:
                # 拒绝绝对路径与向上穿越的成员，防止解压写出目标目录之外。
                continue
            target = destination / relative
            paths.ensure_dir(target.parent)
            with bundle.open(item) as source, target.open('wb') as handle:
                shutil.copyfileobj(source, handle)
            if (item.external_attr >> 16) & 0o111:
                # 保留归档里的可执行位，否则 ezctl/ezdown 解压后无法直接运行。
                try:
                    target.chmod(0o755)
                except OSError:
                    pass
    return destination


def read_marker(path):
    """读取一个状态标记文件。

    参数：`path` 表示标记文件路径。
    返回：返回解析出的字典，文件不存在或内容不可用时返回空字典。
    副作用：只读取磁盘，不修改文件。
    """
    import json

    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_marker(path, payload):
    """原子地写入一个状态标记文件。

    参数：`path` 表示标记文件路径；`payload` 表示要记录的字典。
    返回：返回写入后的 Path。
    副作用：先写临时文件再替换目标，避免别的进程读到半截内容。
    """
    import json

    target = Path(path)
    paths.ensure_dir(target.parent)
    temp = target.with_name(f'{target.name}.partial')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, target)
    return target


def _clear_tree_contents(root, keep=()):
    """清空目录内容，但留下指定的子项。

    参数：`root` 表示目标目录；`keep` 表示要保留的子项名字集合。
    返回：返回删掉的条目数量。
    副作用：删除磁盘上的文件与目录。

    ``clusters`` 必须留在 ``keep`` 里：它通向集群的 CA 与全部证书，扩容与升级都要复用。
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    kept = set(keep)
    removed = 0
    for item in list(root.iterdir()):
        if item.name in kept:
            continue
        if item.is_dir() and not item.is_symlink():
            paths.remove_tree(item)
        else:
            try:
                item.unlink()
            except OSError:
                # 删不掉的文件不该让部署停住，后续解压会覆盖它。
                pass
        removed += 1
    return removed


def _ensure_clusters_link(root):
    """确保 ``<root>/clusters`` 通向持久化的集群目录。

    参数：`root` 表示 kubeasz 安装目录。
    返回：返回集群目录的 Path。
    副作用：创建软链；早先版本遗留在同名位置的真实目录会被搬进持久目录后再替换成软链。

    kubeasz 把 CA 与证书写在控制机的 ``clusters/<集群名>/certs`` 下，扩容与升级必须复用同一套，
    否则会重新签发一份 CA，在跑的节点立刻验证不过。把它留在 kubeasz 安装目录里，任何一次源码
    重解压都会连证书一起删掉，所以这里挪到工作区之外的持久位置，原地只留一个入口。
    """
    root = Path(root)
    link = root / CLUSTERS_LINK_NAME
    if os.name == 'nt':
        # 开发机不做软链（Windows 上创建软链要额外权限）：集群目录就落在 kubeasz 目录下，
        # 由 _clear_tree_contents 的 keep 保证重解压时不被删掉，效果与 Linux 一致。
        return root / CLUSTERS_LINK_NAME
    target = paths.ensure_dir(paths.clusters_root())
    if link.is_symlink():
        if os.path.realpath(link) == os.path.realpath(target):
            return target
        link.unlink()
    elif link.is_dir():
        conflicts = sorted(item.name for item in link.iterdir() if (target / item.name).exists())
        if conflicts:
            # 两处都有同名集群，哪一份算数只有运维知道，绝不替人做主删掉任何一份。
            raise ArtifactError(
                f'kubeasz 目录下的 clusters 与持久目录 {target} 里都有同名集群：'
                f'{"、".join(conflicts)}。请人工确认哪一份有效后再继续部署。'
            )
        for item in list(link.iterdir()):
            shutil.move(str(item), str(target / item.name))
        paths.remove_tree(link)
    elif link.exists():
        link.unlink()
    link.symlink_to(target, target_is_directory=True)
    return target


def _guard_foreign_root(root):
    """拒绝接管一个不是平台装的 kubeasz 目录。

    参数：`root` 表示 kubeasz 安装目录。
    返回：无显式返回值。
    副作用：只读取目录内容。

    运维手工跑过 ``ezdown -D`` 的目录里同样有 ``ezctl``，但版本与 ``bin/`` 内容跟平台的假设无关，
    直接覆盖等于替人删掉一份可用的安装。所以这里停下来让人确认，而不是自作主张。
    """
    root = Path(root)
    if root.is_symlink():
        raise ArtifactError(
            f'{root} 是一个软链，而平台需要它是真实目录。请先把它移走（例如 '
            f'`mv {root} {root}.$(date +%F)`）再重新部署。'
        )
    if not root.is_dir() or (root / SOURCE_MARKER_NAME).exists():
        return
    if not (root / 'ezctl').exists():
        # 空目录，或平台自己上次留下的半成品：可以直接按平台的方式重建。
        return
    raise ArtifactError(
        f'{root} 里已经有一份不是平台安装的 kubeasz（有 ezctl 但没有平台标记）。'
        f'平台不会覆盖别人的安装：请先把它移到别处（例如 `mv {root} {root}.$(date +%F)`）'
        '再重新部署。'
    )


def ensure_kubeasz_source(release, deployment=None, bucket=None, root=None, on_progress=None):
    """确保 kubeasz 源码就位，并返回它的安装目录。

    参数：`release` 表示 kubeasz 版本号；`deployment` 表示部署任务，用于记录制品使用；
        `bucket` 表示 OSS 客户端；`root` 表示改用这个目录（深度预检用），传 None 时用正式目录；
        `on_progress` 表示源码包下载进度回调，签名为 ``(received, total)``，可为空。
    返回：返回 kubeasz 安装目录的 Path。
    副作用：可能下载并解压源码包、建立 ``clusters`` 入口，并记录制品使用情况。

    安装目录是 ``settings.K8S_DEPLOY_KUBEASZ_ROOT``（Linux 平台机上即 ``/etc/kubeasz``）：
    ``ezctl`` 把 ``BASE`` 写死在那里，kubeasz 自带的 inventory 里 ``base_dir`` 也是它，而平台
    绝不调用 ``ezdown``，所以这个目录只能由平台自己建。它跨任务保留，因此**必须先确认版本**——
    上一版留下的源码树结构同样完整，光看结构会拿它去跑新版本。``<目录>/clusters`` 通向持久
    集群目录，重解压只清源码与 ``bin/``，不碰任何集群数据。
    """
    version = clean_version(release)
    if not deployment:
        raise ArtifactError('缺少部署任务上下文，无法准备 kubeasz 目录。')
    paths.ensure_workspace(deployment.pk)

    # 预检拿到的 slug 是用户真实填的那个，绝不能让它写进正式目录去覆盖在跑集群的配置。
    preview = bool(root)
    target = Path(root) if preview else paths.kubeasz_root()
    if not preview:
        _guard_foreign_root(target)
        paths.ensure_dir(target)
        _ensure_clusters_link(target)
        recorded = read_marker(target / SOURCE_MARKER_NAME)
        if recorded.get('release') == version and not _validate_source_layout(target):
            return target

    bucket = bucket or oss_bucket()
    config = oss_config()
    key = f'{base_dir(config)}/releases/{version}/source/kubeasz-{version}.zip'
    manifest = release_manifest(version, bucket=bucket)
    item = _object_item(manifest, key)
    fetched = None
    try:
        fetched = _download_to_cache(bucket, key, item.get('sha256', ''), on_progress)
        if preview:
            _extract_zip(fetched['path'], target)
        else:
            # 只清平台自己的东西，clusters 那个入口留着（开发机上它就是真实目录）。
            _clear_tree_contents(target, keep=(CLUSTERS_LINK_NAME,))
            _extract_zip(fetched['path'], target, wipe=False)
    except ArtifactError as exc:
        # 下载或解压失败时也要留下记录：任务详情页据此说明是哪个制品出的问题。
        record_artifact_use(
            deployment,
            version,
            key,
            str(fetched['path']) if fetched else '',
            fetched['sha256'] if fetched else '',
            size=fetched['size'] if fetched else 0,
            verified=False,
            error=str(exc),
        )
        raise

    record_artifact_use(
        deployment,
        version,
        key,
        str(fetched['path']),
        fetched['sha256'],
        size=fetched['size'],
        verified=True,
        cache_hit=fetched['cache_hit'],
    )

    missing = _validate_source_layout(target)
    if missing:
        raise ArtifactError(
            f'kubeasz {version} 源码包结构不完整，缺少：{"、".join(missing)}。'
            '请重新同步该版本的源码包后再部署。'
        )
    if not preview:
        # 标记最后写：它存在就等于「这棵目录装好了」，中途崩溃留下的半成品会被下次任务重建。
        write_marker(target / SOURCE_MARKER_NAME, {
            'release': version,
            'source_key': key,
            'source_sha256': fetched['sha256'],
        })
    return target


def record_artifact_use(
    deployment,
    release,
    object_key,
    local_path,
    sha256,
    size=0,
    verified=False,
    cache_hit=False,
    error='',
):
    """记录一次制品使用情况，供任务详情展示与排障。

    参数：`deployment` 表示部署任务；`release` 表示 kubeasz 版本；`object_key` 表示 OSS 对象键；
        `local_path` 表示本地缓存路径；`sha256` 表示内容哈希；`size` 表示字节数；
        `verified` 表示是否校验通过；`cache_hit` 表示是否命中缓存；`error` 表示失败原因。
    返回：返回写入的制品使用记录对象。
    副作用：向制品使用表写入一行；deployment 为空时直接返回 None。
    """
    if not deployment:
        return None
    from k8s_deployment.models import K8sDeploymentArtifactUse

    return K8sDeploymentArtifactUse.objects.create(
        organization=getattr(deployment, 'organization', None),
        deployment=deployment,
        release=str(release)[:40],
        object_key=str(object_key)[:500],
        local_path=str(local_path)[:500],
        sha256=str(sha256 or '')[:64],
        size_bytes=int(size or 0),
        verified=bool(verified),
        cache_hit=bool(cache_hit),
        error_message=str(error or '')[:240],
    )


def cache_usage():
    """统计制品缓存占用的空间。

    参数：无。
    返回：返回 {'files': 文件数, 'bytes': 总字节数, 'root': 缓存根目录}。
    副作用：只读取磁盘目录，不修改文件。
    """
    root = paths.artifact_cache_root()
    total = 0
    count = 0
    if root.exists():
        for item in root.rglob('*'):
            if item.is_file() and not item.is_symlink():
                count += 1
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    return {'files': count, 'bytes': total, 'root': str(root)}


def workspace_artifact_budget():
    """返回部署工作区允许使用的磁盘上限。

    参数：无。
    返回：返回字节数上限，用于解压源码前判断剩余空间。
    副作用：只读取配置，不修改数据。
    """
    return int(getattr(settings, 'K8S_DEPLOY_WORKSPACE_MAX_BYTES', 5 * 1024 * 1024 * 1024))
