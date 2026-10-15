import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import oss2
import requests
from django.core.management.base import BaseCommand, CommandError

from k8s_config.models import K8sVersionMapping, SupportedOperatingSystem
from ops import kubeasz_patches
from ops.models import SystemSetting


# Gitee 的 repository/archive 地址已不再返回压缩包（返回 HTML 页面），源码包改用 GitHub codeload 直链；tag 名不带 v 前缀
CODELOAD_BASE = 'https://codeload.github.com/easzlab/kubeasz/zip/refs/tags/'
RAW_BASE = 'https://gitee.com/easzlab/kubeasz/raw/'
DOCKER_MIRROR = 'docker.1ms.run'
DEFAULT_TIMEOUT = (15, 900)
MASKED_KEYS = {'access_key_id', 'access_key_secret', 'secret', 'token', 'password'}
ZIP_MAGIC = b'PK\x03\x04'
GZIP_MAGIC = b'\x1f\x8b'
HTML_PREFIXES = (b'<!doctype', b'<html')


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def clean_version(value):
    return str(value or '').strip().lstrip('v')


def normalize_base_dir(value):
    value = str(value or 'kubeasz').strip().strip('/')
    if not value or value in {'.', '..'} or '..' in value.split('/'):
        raise CommandError('OSS base_dir 不是安全的对象路径')
    return value


def endpoint_url(config):
    endpoint = str(config.get('endpoint') or '').strip().rstrip('/')
    if not endpoint:
        raise CommandError('storage.oss.endpoint 未配置')
    if not endpoint.startswith(('http://', 'https://')):
        endpoint = f"{'https' if config.get('use_https', True) else 'http'}://{endpoint}"
    return endpoint


def parse_ezdown(script):
    values = {}
    for name, value in re.findall(r'^([A-Za-z][A-Za-z0-9_]*)=([^\r\n#]+)', script, re.MULTILINE):
        values[name] = value.strip().strip('"').strip("'")

    def expand(value):
        for _ in range(3):
            value = re.sub(r'\$\{?([A-Za-z][A-Za-z0-9_]*)\}?', lambda match: values.get(match.group(1), match.group(0)), value)
        return value

    def refs(section):
        return [expand(item) for item in re.findall(r'"([^"\n]+:[^"\n]+)"', section)]

    default_start = script.find('function get_default_images')
    extra_start = script.find('function get_extra_images')
    default_section = script[default_start:extra_start if extra_start >= 0 else len(script)]
    extra_section = script[extra_start:] if extra_start >= 0 else ''
    extras = {}
    for match in re.finditer(r'^\s{4}([A-Za-z0-9_-]+)\)\s*(.*?)(?=^\s{4}[A-Za-z0-9_-]+\)|^\s{2}esac)', extra_section, re.MULTILINE | re.DOTALL):
        extras[match.group(1)] = refs(match.group(2))
    os_tokens = re.findall(r'^\s{4}([a-z][a-z0-9_]+)\s+to download package', script, re.MULTILINE)
    return {
        'kubeasz_version': values.get('KUBEASZ_VER', ''),
        'kubernetes_binary_version': values.get('K8S_BIN_VER', ''),
        'ext_binary_version': values.get('EXT_BIN_VER', ''),
        'system_package_version': values.get('SYS_PKG_VER', ''),
        'docker_version': values.get('DOCKER_VER', ''),
        'harbor_version': values.get('HARBOR_VER', ''),
        'default_images': refs(default_section),
        'extra_images': extras,
        'system_package_tokens': os_tokens,
    }


def source_urls(release):
    return {
        'source_archive': f'{CODELOAD_BASE}{release}',
        'ezctl': f'{RAW_BASE}{release}/ezctl',
        'ezdown': f'{RAW_BASE}{release}/ezdown',
    }


def safe_name(image):
    return image.replace('://', '_').replace('/', '_').replace(':', '_').replace('@', '_')


def describe_payload(payload):
    # 用中文描述下载内容的真实形态，便于定位「取到 HTML 页面」这类异常
    head = payload[:512].lstrip(b'\xef\xbb\xbf').lstrip()
    if head.startswith(ZIP_MAGIC):
        return 'zip 压缩包'
    if any(head[:32].lower().startswith(prefix) for prefix in HTML_PREFIXES):
        return 'HTML 页面'
    try:
        head.decode('utf-8')
    except UnicodeDecodeError:
        return '二进制数据'
    return '文本内容'


def verify_download(path, url, expect):
    # 按期望类型校验下载到本地的制品，避免把 HTML 报错页当成制品上传到 OSS
    if expect == 'zip':
        with path.open('rb') as handle:
            head = handle.read(len(ZIP_MAGIC))
        if head != ZIP_MAGIC:
            with path.open('rb') as handle:
                sample = handle.read(512)
            raise CommandError(
                f'下载内容校验失败 {url}: 期望 zip 压缩包（{ZIP_MAGIC!r} 开头），'
                f'实际是 {describe_payload(sample)}，文件大小 {path.stat().st_size} 字节'
            )
        return
    if expect == 'text':
        payload = path.read_bytes()
        head = payload[:512].lstrip(b'\xef\xbb\xbf').lstrip()
        if any(head[:32].lower().startswith(prefix) for prefix in HTML_PREFIXES):
            raise CommandError(
                f'下载内容校验失败 {url}: 期望文本脚本，'
                f'实际是 HTML 页面，文件大小 {path.stat().st_size} 字节'
            )
        try:
            payload.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise CommandError(
                f'下载内容校验失败 {url}: 期望 UTF-8 文本脚本，'
                f'实际不是有效的 UTF-8 文本（{exc}），文件大小 {path.stat().st_size} 字节'
            ) from exc
        return
    if expect == 'gzip':
        with path.open('rb') as handle:
            head = handle.read(2)
        if head != GZIP_MAGIC:
            with path.open('rb') as handle:
                sample = handle.read(512)
            raise CommandError(
                f'下载内容校验失败 {url}: 期望 gzip 压缩包（{GZIP_MAGIC!r} 开头），'
                f'实际是 {describe_payload(sample)}，文件大小 {path.stat().st_size} 字节'
            )
        return
    raise CommandError(f'未知的下载内容校验类型: {expect}')


class Command(BaseCommand):
    help = '同步 kubeasz 版本制品、命令和校验清单到已配置的 OSS'

    def add_arguments(self, parser):
        parser.add_argument('--versions', help='逗号分隔的 kubeasz 版本，默认使用配置中心启用版本')
        parser.add_argument('--systems', help='逗号分隔的系统 token，默认使用配置中心且被 ezdown 支持的系统')
        parser.add_argument('--platforms', default='linux/amd64', help='Docker 平台，逗号分隔，例如 linux/amd64,linux/arm64')
        parser.add_argument('--include-images', action='store_true', help='拉取并上传核心镜像 tar 包')
        parser.add_argument('--include-extra-images', action='store_true', help='同时拉取 ezdown -X 支持的可选镜像')
        parser.add_argument('--include-helm', action='store_true', help='从 kubeasz-ext-bin 提取并上传 Helm CLI（按 Docker 平台）')
        parser.add_argument('--extra-options', help='限定 -X 插件，逗号分隔；默认包含 ezdown 支持的全部插件')
        parser.add_argument('--include-harbor', action='store_true', help='上传 Harbor 离线安装镜像包')
        parser.add_argument('--force', action='store_true', help='覆盖同名 OSS 对象')
        parser.add_argument('--dry-run', action='store_true', help='只生成计划，不下载或上传')
        parser.add_argument('--work-dir', help='临时文件目录，默认使用系统临时目录')

    def handle(self, *args, **options):
        config = self._oss_config()
        mappings = self._mappings(options.get('versions'))
        if not mappings:
            raise CommandError('配置中心没有启用的 Kubernetes/kubeasz 版本映射')
        platforms = [item.strip() for item in str(options['platforms']).split(',') if item.strip()]
        if any(item not in {'linux/amd64', 'linux/arm64'} for item in platforms):
            raise CommandError('platforms 只支持 linux/amd64 和 linux/arm64')
        systems_override = self._systems_override(options.get('systems'))
        base_dir = normalize_base_dir(config.get('base_dir'))
        endpoint = endpoint_url(config)
        bucket = oss2.Bucket(oss2.Auth(config['access_key_id'], config['access_key_secret']), endpoint, config['bucket'])
        if options['dry_run']:
            for mapping in mappings:
                self.stdout.write(f'计划: Kubernetes {mapping.kubernetes_version} / kubeasz {mapping.kubeasz_version}')
            return
        work_root = Path(options.get('work_dir') or tempfile.mkdtemp(prefix='kubeasz-sync-'))
        owns_work_root = not options.get('work_dir')
        work_root.mkdir(parents=True, exist_ok=True)
        releases = []
        try:
            for mapping in mappings:
                releases.append(self._sync_release(bucket, base_dir, mapping, systems_override, platforms, options, work_root))
            index = {
                'schema_version': 1,
                'generated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                'source_repository': 'https://gitee.com/easzlab/kubeasz',
                'local_execution_mode': True,
                'releases': releases,
            }
            self._put_json(bucket, f'{base_dir}/index.json', index, options['force'])
            self.stdout.write(self.style.SUCCESS(f'kubeasz OSS 同步完成: {len(releases)} 个版本'))
        finally:
            if owns_work_root:
                shutil.rmtree(work_root, ignore_errors=True)

    def _oss_config(self):
        setting = SystemSetting.objects.filter(key='storage.oss').first()
        config = setting.value if setting else {}
        if not isinstance(config, dict) or not config.get('enabled'):
            raise CommandError('storage.oss 未启用')
        required = ('access_key_id', 'access_key_secret', 'bucket', 'endpoint')
        if any(not str(config.get(key) or '').strip() for key in required):
            raise CommandError('storage.oss 缺少必要配置')
        return config

    def _mappings(self, raw_versions):
        queryset = K8sVersionMapping.objects.filter(is_active=True).order_by('sort_order', 'id')
        requested = {clean_version(item) for item in str(raw_versions or '').split(',') if item.strip()}
        return [item for item in queryset if not requested or clean_version(item.kubeasz_version) in requested]

    def _systems_override(self, raw_systems):
        if not raw_systems:
            return None
        return {item.strip().lower() for item in str(raw_systems).split(',') if item.strip()}

    def _sync_release(self, bucket, base_dir, mapping, systems_override, platforms, options, work_root):
        release = clean_version(mapping.kubeasz_version)
        release_dir = work_root / release
        release_dir.mkdir(parents=True, exist_ok=True)
        urls = source_urls(release)
        script_path = release_dir / 'ezdown'
        archive_path = release_dir / f'kubeasz-{release}.zip'
        self._download(urls['ezdown'], script_path, 'text')
        self._patch_ezdown(release, script_path)
        metadata = parse_ezdown(script_path.read_text(encoding='utf-8'))
        metadata['kubernetes_version'] = mapping.kubernetes_version
        metadata['release'] = release
        metadata['source_urls'] = urls
        object_items = []
        object_items.append(self._upload_file(bucket, f'{base_dir}/releases/{release}/commands/ezdown', script_path, 'text/plain', options['force']))
        for command in ('ezctl',):
            command_path = release_dir / command
            self._download(urls[command], command_path, 'text')
            object_items.append(self._upload_file(bucket, f'{base_dir}/releases/{release}/commands/{command}', command_path, 'text/plain', options['force']))
        self._download(urls['source_archive'], archive_path, 'zip')
        archive_path = self._patch_source_archive(release, archive_path, release_dir)
        object_items.append(self._upload_file(bucket, f'{base_dir}/releases/{release}/source/kubeasz-{release}.zip', archive_path, 'application/zip', options['force']))
        selected_systems = self._select_systems(metadata['system_package_tokens'], systems_override)
        selected_systems, unavailable_systems = self._available_systems(metadata['system_package_version'], selected_systems)
        metadata['selected_systems'] = selected_systems
        metadata['unavailable_systems'] = unavailable_systems
        metadata['unsupported_configured_systems'] = self._configured_unsupported(metadata['system_package_tokens'])
        image_refs = list(metadata['default_images'])
        if options['include_extra_images']:
            extra_options = [item.strip() for item in str(options.get('extra_options') or '').split(',') if item.strip()]
            selected_options = extra_options or list(metadata['extra_images'])
            metadata['selected_extra_options'] = [item for item in selected_options if item in metadata['extra_images']]
            for option in metadata['selected_extra_options']:
                image_refs.extend(metadata['extra_images'][option])
        if options['include_images']:
            image_refs.extend([
                f'easzlab/kubeasz:{release}',
                f'easzlab/kubeasz-k8s-bin:{metadata["kubernetes_binary_version"]}',
                f'easzlab/kubeasz-ext-bin:{metadata["ext_binary_version"]}',
            ])
            image_refs.extend(
                f'easzlab/kubeasz-sys-pkg:{metadata["system_package_version"]}_{system}'
                for system in selected_systems
            )
        metadata['image_archives'] = []
        metadata['image_failures'] = []
        metadata['helm_artifacts'] = []
        if options['include_images']:
            image_refs = list(dict.fromkeys(image_refs))
            for platform in platforms:
                for image in image_refs:
                    tar_path = release_dir / 'images' / f'{safe_name(image)}_{platform.rsplit("/", 1)[1]}.tar'
                    tar_path.parent.mkdir(parents=True, exist_ok=True)
                    object_key = f'{base_dir}/releases/{release}/images/{tar_path.name}'
                    existing = self._remote_item(bucket, object_key) if not options['force'] else None
                    if existing:
                        metadata['image_archives'].append({'image': image, 'platform': platform, **existing})
                        object_items.append(existing)
                        continue
                    try:
                        self._docker_save(image, platform, tar_path)
                        item = self._upload_file(bucket, object_key, tar_path, 'application/x-tar', options['force'])
                        metadata['image_archives'].append({'image': image, 'platform': platform, **item})
                        object_items.append(item)
                    except CommandError as exc:
                        metadata['image_failures'].append({'image': image, 'platform': platform, 'error': str(exc)})
                        self.stderr.write(str(exc))
                    finally:
                        tar_path.unlink(missing_ok=True)
        if options['include_helm']:
            helm_image = f'easzlab/kubeasz-ext-bin:{metadata["ext_binary_version"]}'
            for platform in platforms:
                arch = platform.rsplit('/', 1)[1]
                helm_path = release_dir / 'helm' / f'helm_{arch}'
                helm_path.parent.mkdir(parents=True, exist_ok=True)
                object_key = f'{base_dir}/releases/{release}/helm/helm_{arch}'
                existing = self._remote_item(bucket, object_key) if not options['force'] else None
                if existing:
                    metadata['helm_artifacts'].append({'platform': platform, **existing})
                    object_items.append(existing)
                    continue
                try:
                    self._extract_helm(helm_image, platform, helm_path)
                    item = self._upload_file(bucket, object_key, helm_path, 'application/octet-stream', options['force'])
                    metadata['helm_artifacts'].append({'platform': platform, **item})
                    object_items.append(item)
                except CommandError as exc:
                    metadata['image_failures'].append({'image': f'{helm_image}:/extra/helm', 'platform': platform, 'error': str(exc)})
                    self.stderr.write(str(exc))
                finally:
                    helm_path.unlink(missing_ok=True)
        if options['include_harbor']:
            for platform in platforms:
                image = f'easzlab/harbor-offline:{metadata["harbor_version"]}'
                if platform.endswith('arm64'):
                    image += '-aarch64'
                tar_path = release_dir / 'harbor' / f'harbor-offline_{platform.rsplit("/", 1)[1]}.tar'
                tar_path.parent.mkdir(parents=True, exist_ok=True)
                object_key = f'{base_dir}/releases/{release}/packages/{tar_path.name}'
                existing = self._remote_item(bucket, object_key) if not options['force'] else None
                if existing:
                    object_items.append(existing)
                    tar_path.unlink(missing_ok=True)
                    continue
                try:
                    self._docker_save(image, platform, tar_path)
                    object_items.append(self._upload_file(bucket, object_key, tar_path, 'application/x-tar', options['force']))
                except CommandError as exc:
                    metadata.setdefault('image_failures', []).append({'image': image, 'platform': platform, 'error': str(exc)})
                    self.stderr.write(str(exc))
                tar_path.unlink(missing_ok=True)
        manifest = {**metadata, 'objects': object_items}
        self._put_json(bucket, f'{base_dir}/releases/{release}/manifest.json', manifest, options['force'])
        self.stdout.write(f'已同步 kubeasz {release}: {len(object_items)} 个对象')
        return {
            'kubernetes_version': mapping.kubernetes_version,
            'kubeasz_version': release,
            'manifest': f'{base_dir}/releases/{release}/manifest.json',
            'kubernetes_binary_version': metadata['kubernetes_binary_version'],
            'system_package_version': metadata['system_package_version'],
            'selected_systems': selected_systems,
            'default_images': metadata['default_images'],
            'extra_image_options': sorted(metadata['extra_images']),
            'image_archives': len(metadata['image_archives']),
            'helm_artifacts': metadata['helm_artifacts'],
        }

    def _select_systems(self, supported_tokens, override):
        configured = set()
        for item in SupportedOperatingSystem.objects.filter(is_active=True):
            token = self._system_token(item.os_type, item.version)
            if token:
                configured.add(token)
        if override:
            return sorted(set(supported_tokens) & override)
        return sorted(set(supported_tokens) & configured)

    def _configured_unsupported(self, supported_tokens):
        supported = set(supported_tokens)
        result = []
        for item in SupportedOperatingSystem.objects.filter(is_active=True):
            token = self._system_token(item.os_type, item.version)
            if token and token not in supported:
                result.append(token)
        return sorted(set(result))

    def _available_systems(self, package_version, systems):
        if not systems:
            return [], []
        try:
            response = requests.get(
                'https://hub.docker.com/v2/repositories/easzlab/kubeasz-sys-pkg/tags',
                params={'page_size': 100, 'name': f'{package_version}_'},
                timeout=(10, 30),
            )
            response.raise_for_status()
            tags = {str(item.get('name') or '') for item in response.json().get('results', [])}
            available = [item for item in systems if f'{package_version}_{item}' in tags]
            return available, [item for item in systems if item not in available]
        except (requests.RequestException, ValueError, AttributeError):
            return systems, []

    def _system_token(self, os_type, version):
        family = str(os_type or '').strip().lower()
        version_text = str(version or '').strip().lower()
        major_match = re.search(r'\d+', version_text)
        if not major_match:
            return ''
        major = major_match.group(0)
        aliases = {
            'ubuntu': 'ubuntu',
            'debian': 'debian',
            'fedora': 'fedora',
            'rocky linux': 'rocky',
            'rocky': 'rocky',
            'alma linux': 'almalinux',
            'almalinux': 'almalinux',
            'centos': 'centos',
            'openeuler': 'openeuler',
            'openEuler'.lower(): 'openeuler',
            'opensuse leap': 'opensuse_leap',
            'opensuse': 'opensuse_leap',
        }
        normalized = aliases.get(family)
        return f'{normalized}_{major}' if normalized else ''

    def _patch_ezdown(self, release, script_path):
        # 必须在 parse_ezdown 之前调用：manifest 的 extra_images 来自对 ezdown 的解析，
        # 补丁没落地就不会有 ingress 镜像，能力判定照旧为空。
        patch = kubeasz_patches.release_patch(release)
        if not patch:
            return
        original = script_path.read_text(encoding='utf-8')
        if kubeasz_patches.version_variable_declared(original):
            # 已经带过补丁（上一次同步留下的工作副本）时直接返回：patch_ezdown 的锚点
            # 打完之后仍然存在，再打一遍会把三处改动各插一份。
            return
        patched = kubeasz_patches.patch_ezdown(original)
        if patched != original:
            # 必须用 write_bytes：write_text 在 Windows 上会把 \n 翻成 \r\n，脚本传到 Linux
            # 后首行成了 ``#!/bin/bash\r``，直接报 bad interpreter 没法执行。第一版同步出去的
            # kubeasz/releases/3.6.7/commands/ezdown 就是这么坏的（zip 内的副本走字节级改写，
            # 所以没问题），而 _upload_file 默认跳过已存在的对象 —— 修完要带 --force 重传。
            script_path.write_bytes(patched.encode('utf-8'))
            self.stdout.write(f'kubeasz {release}: ezdown 已补上 {patch["option"]} 镜像声明')

    def _patch_source_archive(self, release, archive_path, release_dir):
        # 返回可上传的压缩包路径；该版本没有登记补丁时原样返回，不碰压缩包。
        patch = kubeasz_patches.release_patch(release)
        if not patch:
            return archive_path
        staged = release_dir / 'patch'
        staged.mkdir(parents=True, exist_ok=True)
        payloads = {}
        for item in patch['files']:
            target = staged / Path(item['path']).name
            self._download_best(kubeasz_patches.file_urls(item['path']), target, item['expect'])
            payloads[item['path']] = target.read_bytes()
        patched_zip = release_dir / f'kubeasz-{release}-patched.zip'
        kubeasz_patches.repack_source_zip(archive_path, patched_zip, release, payloads)
        self.stdout.write(
            f'kubeasz {release}: 源码包已打补丁（回移自 {patch["source_release"]}，'
            f'新增 {len(patch["files"])} 个文件）'
        )
        return patched_zip

    def _download_best(self, urls, destination, expect):
        # 同一制品按顺序尝试多个来源（Gitee 优先、GitHub 兜底），全部失败才报错。
        errors = []
        for url in urls:
            try:
                self._download(url, destination, expect)
                return url
            except Exception as exc:
                errors.append(f'{url}: {exc}')
        raise CommandError(f'所有来源都下载失败：{" | ".join(errors)[-900:]}')

    def _download(self, url, destination, expect):
        temporary = destination.with_name(f'.{destination.name}.part')
        try:
            response = requests.get(url, stream=True, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
            with temporary.open('wb') as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            verify_download(temporary, url, expect)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(destination)

    def _docker_save(self, image, platform, destination):
        docker = shutil.which('docker')
        if not docker:
            raise CommandError('未找到 Docker，无法生成镜像 tar 包')
        candidates = [image]
        if not image.startswith(f'{DOCKER_MIRROR}/'):
            candidates.insert(0, f'{DOCKER_MIRROR}/{image}')
        pull = None
        selected = image
        errors = []
        for candidate in candidates:
            for attempt in range(1, 4):
                try:
                    pull = subprocess.run([docker, 'pull', '--platform', platform, candidate], capture_output=True, text=True, timeout=300)
                except subprocess.TimeoutExpired:
                    pull = None
                    errors.append(f'{candidate}: timeout after 300 seconds')
                    break
                if pull.returncode == 0:
                    selected = candidate
                    break
                errors.append(f'{candidate}: {pull.stderr[-240:]}')
                if attempt < 3:
                    import time
                    time.sleep(attempt * 3)
            if pull and pull.returncode == 0:
                break
        if not pull or pull.returncode:
            raise CommandError(f'Docker 拉取失败 {image} ({platform}): {" | ".join(errors)[-900:]}')
        if selected != image:
            tag = subprocess.run([docker, 'tag', selected, image], capture_output=True, text=True, timeout=120)
            if tag.returncode:
                raise CommandError(f'Docker 标记失败 {image} ({platform}): {tag.stderr[-500:]}')
        saved = subprocess.run([docker, 'save', '--platform', platform, '-o', str(destination), image], capture_output=True, text=True, timeout=1800)
        if saved.returncode:
            raise CommandError(f'Docker 导出失败 {image} ({platform}): {saved.stderr[-500:]}')

    def _extract_helm(self, image, platform, destination):
        docker = shutil.which('docker')
        if not docker:
            raise CommandError('未找到 Docker，无法提取 Helm CLI')
        create = subprocess.run(
            [docker, 'create', '--platform', platform, image],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if create.returncode:
            raise CommandError(f'创建 Helm 提取容器失败 {image} ({platform}): {create.stderr[-500:]}')
        container = create.stdout.strip()
        try:
            copy = subprocess.run(
                [docker, 'cp', f'{container}:/extra/helm', str(destination)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if copy.returncode:
                raise CommandError(f'提取 Helm CLI 失败 {image} ({platform}): {copy.stderr[-500:]}')
            if not destination.exists() or destination.stat().st_size == 0:
                raise CommandError(f'Helm CLI 文件为空 {image} ({platform})')
        finally:
            subprocess.run([docker, 'rm', container], capture_output=True, text=True, timeout=120)

    def _upload_file(self, bucket, key, path, content_type, force):
        if not force:
            for attempt in range(1, 6):
                try:
                    bucket.head_object(key)
                    return {'key': key, 'size': path.stat().st_size, 'sha256': sha256_file(path), 'reused': True}
                except oss2.exceptions.NoSuchKey:
                    break
                except Exception:
                    if attempt == 5:
                        raise
                    import time
                    time.sleep(attempt * 2)
        digest = sha256_file(path)
        headers = {'Content-Type': content_type, 'x-oss-meta-sha256': digest}
        for attempt in range(1, 6):
            try:
                bucket.put_object_from_file(key, str(path), headers=headers)
                return {'key': key, 'size': path.stat().st_size, 'sha256': digest, 'reused': False}
            except Exception as exc:
                if attempt == 5:
                    raise CommandError(f'OSS 上传失败 {key}: {exc}') from exc
                import time
                time.sleep(attempt * 3)

    def _remote_item(self, bucket, key):
        try:
            metadata = bucket.head_object(key)
        except oss2.exceptions.NoSuchKey:
            return None
        return {
            'key': key,
            'size': int(getattr(metadata, 'content_length', 0) or 0),
            'sha256': metadata.headers.get('x-oss-meta-sha256', ''),
            'reused': True,
        }

    def _put_json(self, bucket, key, value, force):
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
        bucket.put_object(key, payload, headers={'Content-Type': 'application/json; charset=utf-8', 'x-oss-meta-sha256': hashlib.sha256(payload).hexdigest()})
