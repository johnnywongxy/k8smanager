"""一次性修复 OSS 上被写成 HTML 页面的 kubeasz 源码包。

历史缺陷：sync_kubeasz_artifacts 早期使用的 Gitee archive 地址返回 HTML 页面，
导致 kubeasz/releases/<版本>/source/kubeasz-<版本>.zip 里存的是报错页面。
本脚本只重传这几个源码包对象，并且只更新对应 manifest 里那一条 source 条目的
sha256/size，其余字段（image_archives、helm_artifacts、selected_systems 等）
一律原样保留，因此不能用 sync_kubeasz_artifacts 命令整体重跑来替代。

用法：
    python scripts/repair_kubeasz_source_archives.py --dry-run
    python scripts/repair_kubeasz_source_archives.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import oss2
import requests


BACKEND_ROOT = Path(__file__).resolve().parents[1]
# 已确认被写坏的 4 个版本，顺序与 OSS 上现存的 release 目录一致
DEFAULT_RELEASES = ('3.6.7', '3.6.8', '3.6.10', '3.7.0')
DOWNLOAD_TIMEOUT = (20, 600)
# 修复清单时需要原样保留、仅用于对比的字段
PRESERVED_FIELDS = ('image_archives', 'image_failures', 'helm_artifacts', 'selected_systems')

sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

import django  # noqa: E402

django.setup()

from ops.management.commands.sync_kubeasz_artifacts import (  # noqa: E402
    endpoint_url,
    normalize_base_dir,
    sha256_file,
    source_urls,
    verify_download,
)
from ops.models import SystemSetting  # noqa: E402


def oss_bucket():
    """按配置中心 storage.oss 的配置创建 OSS Bucket，并返回 Bucket 与 base_dir。"""
    setting = SystemSetting.objects.filter(key='storage.oss').first()
    config = setting.value if setting else {}
    if not isinstance(config, dict) or not config.get('enabled'):
        raise SystemExit('storage.oss 未启用，无法修复 OSS 对象')
    required = ('access_key_id', 'access_key_secret', 'bucket', 'endpoint')
    if any(not str(config.get(key) or '').strip() for key in required):
        raise SystemExit('storage.oss 缺少必要配置，无法修复 OSS 对象')
    auth = oss2.Auth(config['access_key_id'], config['access_key_secret'])
    bucket = oss2.Bucket(auth, endpoint_url(config), config['bucket'])
    return bucket, normalize_base_dir(config.get('base_dir'))


def source_key(base_dir, release):
    """返回某个 kubeasz 版本源码包在 OSS 上的对象键。"""
    return f'{base_dir}/releases/{release}/source/kubeasz-{release}.zip'


def manifest_key(base_dir, release):
    """返回某个 kubeasz 版本制品清单在 OSS 上的对象键。"""
    return f'{base_dir}/releases/{release}/manifest.json'


def object_head(bucket, key, length=16):
    """读取 OSS 对象开头若干字节，用于判断对象是 zip 还是 HTML 页面。"""
    return bucket.get_object(key, byte_range=(0, length - 1)).read()


def local_head(path, length=4):
    """读取本地文件开头若干字节，用于打印校验通过后的 zip 魔数。"""
    with path.open('rb') as handle:
        return handle.read(length)


def remote_state(bucket, key):
    """读取 OSS 对象的大小、自定义 sha256 元数据和开头字节，对象不存在时返回 None。"""
    try:
        metadata = bucket.head_object(key)
    except oss2.exceptions.NoSuchKey:
        return None
    return {
        'size': int(getattr(metadata, 'content_length', 0) or 0),
        'sha256': metadata.headers.get('x-oss-meta-sha256', ''),
        'head': object_head(bucket, key),
    }


def download_source(url, destination):
    """下载源码包并校验是真正的 zip，返回 (字节数, sha256)。"""
    response = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
    response.raise_for_status()
    with destination.open('wb') as handle:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                handle.write(chunk)
    verify_download(destination, url, 'zip')
    return destination.stat().st_size, sha256_file(destination)


def preserved_snapshot(manifest):
    """摘取清单中必须原样保留的字段，用于修复前后对比。"""
    return {name: manifest.get(name) for name in PRESERVED_FIELDS if name in manifest}


def summarize(value):
    """把保留字段压缩成一行摘要，避免打印整份镜像列表。"""
    if isinstance(value, list):
        return f'{len(value)} 项'
    return repr(value)


def upload_source(bucket, key, path, digest):
    """把校验通过的源码包覆盖上传到指定对象键，并写入 sha256 元数据。"""
    bucket.put_object_from_file(key, str(path), headers={'Content-Type': 'application/zip', 'x-oss-meta-sha256': digest})


def update_manifest(bucket, key, manifest):
    """写回清单对象，序列化方式与 sync_kubeasz_artifacts._put_json 保持一致。"""
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    headers = {'Content-Type': 'application/json; charset=utf-8', 'x-oss-meta-sha256': hashlib.sha256(payload).hexdigest()}
    bucket.put_object(key, payload, headers=headers)


def repair_release(bucket, base_dir, release, work_root, dry_run):
    """修复单个版本的源码包对象及清单条目，返回结果字典；抛出的异常由调用方汇报。"""
    key = source_key(base_dir, release)
    mkey = manifest_key(base_dir, release)
    url = source_urls(release)['source_archive']
    archive_path = work_root / f'kubeasz-{release}.zip'
    print(f'[{release}] 下载 {url}')
    size, digest = download_source(url, archive_path)
    before = remote_state(bucket, key)
    if before is None:
        print(f'[{release}] OSS 现状: 对象不存在，将新建 {key}')
    else:
        print(
            f'[{release}] OSS 现状: size={before["size"]} '
            f'sha256={(before["sha256"] or "-")[:16]}... 开头字节={before["head"]!r}'
        )
    print(f'[{release}] 真实源码包: size={size} sha256={digest} 开头字节={local_head(archive_path)!r}')

    manifest = json.loads(bucket.get_object(mkey).read().decode('utf-8'))
    objects = manifest.get('objects')
    if not isinstance(objects, list):
        raise SystemExit(f'[{release}] 清单 objects 字段不是数组，停止修复以免破坏清单')
    matched = [item for item in objects if isinstance(item, dict) and item.get('key') == key]
    before_preserved = preserved_snapshot(manifest)
    print(
        f'[{release}] 清单: objects={len(objects)} 命中 source 条目={len(matched)} '
        + ' '.join(f'{name}={summarize(value)}' for name, value in before_preserved.items())
    )
    if not matched:
        print(f'[{release}] 警告: 清单里没有 key 等于 {key} 的条目，跳过清单更新（对象仍会重传）')

    if dry_run:
        print(f'[{release}] dry-run: 将覆盖上传 {key}，并更新 {len(matched)} 条清单条目后写回 {mkey}')
        return {'release': release, 'size': size, 'sha256': digest, 'updated': len(matched), 'dry_run': True}

    upload_source(bucket, key, archive_path, digest)
    after_remote = remote_state(bucket, key)
    if after_remote is None or after_remote['head'][:4] != b'PK\x03\x04':
        raise SystemExit(f'[{release}] 上传后校验失败: 远端开头字节={None if after_remote is None else after_remote["head"]!r}')
    if after_remote['size'] != size or after_remote['sha256'] != digest:
        raise SystemExit(f'[{release}] 上传后校验失败: 远端 size={after_remote["size"]} sha256={(after_remote["sha256"] or "-")[:16]}...')
    print(f'[{release}] 已覆盖上传 {key}: size={after_remote["size"]} 开头字节={after_remote["head"][:4]!r}')

    for item in matched:
        item['sha256'] = digest
        item['size'] = size
        item['reused'] = False
    update_manifest(bucket, mkey, manifest)

    check = json.loads(bucket.get_object(mkey).read().decode('utf-8'))
    after_preserved = preserved_snapshot(check)
    if after_preserved != before_preserved:
        raise SystemExit(f'[{release}] 清单保留字段被改动，需人工介入: {after_preserved} != {before_preserved}')
    checked = [item for item in check.get('objects', []) if item.get('key') == key]
    print(
        f'[{release}] 已写回清单 {mkey}: objects={len(check.get("objects") or [])} '
        f'source 条目={checked} 保留字段未变化 '
        + ' '.join(f'{name}={summarize(value)}' for name, value in after_preserved.items())
    )
    return {
        'release': release,
        'size': size,
        'sha256': digest,
        'updated': len(matched),
        'preserved': {name: summarize(value) for name, value in after_preserved.items()},
        'dry_run': False,
    }


def main():
    """解析参数，逐个修复目标版本的源码包并打印结果汇总。"""
    parser = argparse.ArgumentParser(description='重传 OSS 上损坏的 kubeasz 源码包，只更新清单中的 source 条目')
    parser.add_argument('--releases', default=','.join(DEFAULT_RELEASES), help='逗号分隔的 kubeasz 版本，默认修复已确认损坏的 4 个版本')
    parser.add_argument('--dry-run', action='store_true', help='只下载校验并打印将要做的改动，不写 OSS')
    parser.add_argument('--work-dir', help='临时文件目录，默认使用系统临时目录')
    options = parser.parse_args()

    releases = [item.strip() for item in str(options.releases).split(',') if item.strip()]
    if not releases:
        raise SystemExit('没有指定要修复的版本')
    bucket, base_dir = oss_bucket()
    # 只打印对象前缀和开关，不打印任何密钥内容
    print(f'OSS 目标: base_dir={base_dir} dry_run={options.dry_run} 版本={releases}')
    work_root = Path(options.work_dir or tempfile.mkdtemp(prefix='kubeasz-repair-'))
    work_root.mkdir(parents=True, exist_ok=True)
    results = []
    failures = []
    for release in releases:
        try:
            results.append(repair_release(bucket, base_dir, release, work_root, options.dry_run))
        except Exception as exc:
            failures.append((release, exc))
            print(f'[{release}] 修复失败: {type(exc).__name__}: {exc}')
    print(f'完成: 成功 {len(results)} 个，失败 {len(failures)} 个，dry_run={options.dry_run}')
    for item in results:
        print(f'- {item["release"]}: size={item["size"]} sha256={item["sha256"]} 更新清单条目={item["updated"]}')
    for release, exc in failures:
        print(f'- {release}: 失败 {type(exc).__name__}: {exc}')
    if failures:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
