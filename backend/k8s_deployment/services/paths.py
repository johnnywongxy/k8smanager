"""部署工作区、制品缓存与 Worker 锁的路径解析。

跨平台约定：Linux 平台机落在 ``/var/lib/k8smanager``（规划 6.3），Windows 开发机落在
用户目录下的 ``~/.k8smanager``，两者都由 ``settings.K8S_DEPLOY_*`` 决定，本模块不写死路径。
所有目录创建都集中在这里，避免各处 ``mkdir`` 的权限与 parents 行为不一致。
"""

import os
import shutil
import stat
import time
from pathlib import Path

from django.conf import settings

# 采集到的诊断信息与逐阶段原始输出落在这个子目录。
LOG_DIR_NAME = 'logs'
DIAGNOSTICS_DIR_NAME = 'diagnostics'


def state_root():
    """返回部署状态的根目录。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_STATE_ROOT`` 对应的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return Path(settings.K8S_DEPLOY_STATE_ROOT)


def run_root():
    """返回所有任务工作区的父目录。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_RUN_ROOT`` 对应的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return Path(settings.K8S_DEPLOY_RUN_ROOT)


def workspace_root(deployment_id):
    """返回某个部署任务的工作区目录。

    参数：`deployment_id` 表示部署任务主键。
    返回：返回 ``<run_root>/deployment-<id>``。
    副作用：只做路径计算，不创建目录。
    """
    return run_root() / f'deployment-{int(deployment_id)}'


def kubeasz_root():
    """返回 kubeasz 源码的安装目录。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_KUBEASZ_ROOT`` 对应的 Path，Linux 平台机上即 ``/etc/kubeasz``。
    副作用：只做路径计算，不创建目录。

    这里返回的是**全平台共用、跨任务保留**的固定目录，不是每次运行的工作区：``ezctl`` 把
    ``BASE`` 写死成 ``/etc/kubeasz``，kubeasz 自带的 inventory 里 ``base_dir`` 也是它，两者都
    不受平台控制。把源码解压到工作区里，``ezctl`` 一启动就会 ``invalid dir`` 退出 1。
    """
    return Path(settings.K8S_DEPLOY_KUBEASZ_ROOT)


def clusters_root():
    """返回集群配置与证书的真实存放目录。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_CLUSTERS_ROOT`` 对应的 Path。
    副作用：只做路径计算，不创建目录。

    ``<kubeasz_root>/clusters`` 是指向它的软链，于是 ``ezctl`` 与 ansible 照旧按
    ``clusters/<集群名>/`` 读写，而这份数据不会随源码重解压被删掉。
    """
    return Path(settings.K8S_DEPLOY_CLUSTERS_ROOT)


def dry_run_root(deployment_id):
    """返回深度预检专用的临时目录。

    参数：`deployment_id` 表示临时任务主键。
    返回：返回 ``<state_root>/dryrun/deployment-<id>``。
    副作用：只做路径计算，不创建目录。

    预检同样要渲染配置并写 ``clusters/<slug>/``，而它用的是用户真实填写的集群标识。若共用正式
    的 kubeasz 目录，一次「预览」就会覆盖正在运行的集群的 hosts 与 config.yml，所以给它一份
    用完即删的独立目录。
    """
    return state_root() / 'dryrun' / f'deployment-{int(deployment_id)}'


def cluster_dir(kubeasz, slug):
    """返回 kubeasz 中某个集群的配置目录。

    参数：`kubeasz` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回 ``<kubeasz>/clusters/<slug>``。
    副作用：只做路径计算，不创建目录。
    """
    return Path(kubeasz) / 'clusters' / slug


def hosts_path(kubeasz, slug):
    """返回集群的 ansible inventory 文件路径。

    参数：`kubeasz` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回 ``<kubeasz>/clusters/<slug>/hosts``。
    副作用：只做路径计算，不创建目录。
    """
    return cluster_dir(kubeasz, slug) / 'hosts'


def config_path(kubeasz, slug):
    """返回集群的 kubeasz 变量文件路径。

    参数：`kubeasz` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回 ``<kubeasz>/clusters/<slug>/config.yml``。
    副作用：只做路径计算，不创建目录。
    """
    return cluster_dir(kubeasz, slug) / 'config.yml'


def kubeconfig_path(kubeasz, slug):
    """返回 kubeasz 为集群生成的客户端 kubeconfig 路径。

    参数：`kubeasz` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：返回 ``<kubeasz>/clusters/<slug>/kubectl.kubeconfig``，该文件在部署成功后才存在。
    副作用：只做路径计算，不创建目录。
    """
    return cluster_dir(kubeasz, slug) / 'kubectl.kubeconfig'


def log_dir(workspace):
    """返回任务工作区内存放阶段原始输出的目录。

    参数：`workspace` 表示任务工作区路径。
    返回：返回 ``<workspace>/logs``。
    副作用：只做路径计算，不创建目录。
    """
    return Path(workspace) / LOG_DIR_NAME


def diagnostics_dir(workspace):
    """返回任务工作区内存放诊断包的目录。

    参数：`workspace` 表示任务工作区路径。
    返回：返回 ``<workspace>/diagnostics``。
    副作用：只做路径计算，不创建目录。
    """
    return Path(workspace) / DIAGNOSTICS_DIR_NAME


def artifact_cache_root():
    """返回制品缓存的根目录。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_ARTIFACT_ROOT`` 对应的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return Path(settings.K8S_DEPLOY_ARTIFACT_ROOT)


def artifact_cache_path(sha256):
    """按内容哈希返回制品在缓存中的落盘路径。

    参数：`sha256` 表示制品内容的十六进制 SHA-256 摘要。
    返回：返回 ``<缓存根>/<前两位>/<完整摘要>``，与原文件名无关，天然去重。
    副作用：只做路径计算，不创建目录。
    """
    digest = str(sha256 or '').strip().lower()
    if len(digest) < 4:
        raise ValueError('制品哈希不合法，无法定位缓存路径')
    return artifact_cache_root() / digest[:2] / digest


def worker_lock_path():
    """返回部署 Worker 的文件锁路径。

    参数：无。
    返回：返回 ``settings.K8S_DEPLOY_WORKER_LOCK`` 对应的 Path。
    副作用：只做路径计算，不创建目录。
    """
    return Path(settings.K8S_DEPLOY_WORKER_LOCK)


def ensure_dir(path):
    """确保目录存在并返回该路径。

    参数：`path` 表示待创建的目录。
    返回：返回创建后的 Path，便于链式调用。
    副作用：在磁盘上创建目录及其父目录，已存在时不报错。
    """
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def ensure_workspace(deployment_id):
    """创建任务工作区及其子目录并返回工作区路径。

    参数：`deployment_id` 表示部署任务主键。
    返回：返回创建后的工作区 Path。
    副作用：创建 ``workspace``、``logs``、``diagnostics`` 三个目录。
    """
    workspace = ensure_dir(workspace_root(deployment_id))
    ensure_dir(log_dir(workspace))
    ensure_dir(diagnostics_dir(workspace))
    return workspace


def secure_file(path):
    """把文件权限收紧到仅属主可读写。

    参数：`path` 表示目标文件。
    返回：返回该文件路径。
    副作用：在类 Unix 系统上把权限改为 0600；Windows 没有等价的权限位，跳过。
    """
    target = Path(path)
    if os.name != 'nt':
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def touch_workspace(workspace):
    """把工作区的最近使用时间刷新为当前时间。

    参数：`workspace` 表示任务工作区路径。
    返回：返回该工作区路径。
    副作用：更新目录的访问与修改时间，供过期清理判定使用。
    """
    target = Path(workspace)
    try:
        os.utime(target, None)
    except OSError:
        # 工作区已被并发清理时忽略即可，调用方不依赖这次刷新的结果。
        pass
    return target


def workspace_size(path):
    """递归统计目录占用的字节数。

    参数：`path` 表示待统计的目录。
    返回：返回目录内所有普通文件的字节数之和，目录不存在时返回 0。
    副作用：只读取目录元数据，不修改文件。
    """
    total = 0
    target = Path(path)
    if not target.exists():
        return 0
    for item in target.rglob('*'):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            # 统计期间文件被删除不影响总量，跳过即可。
            continue
    return total


def remove_tree(path):
    """删除目录树，不存在时静默返回。

    参数：`path` 表示待删除的目录。
    返回：无显式返回值。
    副作用：删除磁盘上的整个目录树，且不可恢复。
    """
    shutil.rmtree(path, ignore_errors=True)


def expired_workspaces(ttl_seconds, active_ids=()):
    """列出超过保留期且不再被活动任务占用的工作区。

    参数：`ttl_seconds` 表示工作区的最短保留秒数；`active_ids` 表示当前不允许清理的任务主键集合。
    返回：返回待清理的工作区 Path 列表。
    副作用：只读取目录状态，不删除任何内容。
    """
    deadline = time.time() - max(int(ttl_seconds), 0)
    active = {f'deployment-{int(item)}' for item in active_ids}
    result = []
    root = run_root()
    if not root.exists():
        return result
    for item in root.iterdir():
        if not item.is_dir() or item.name in active:
            continue
        try:
            if item.stat().st_mtime < deadline:
                result.append(item)
        except OSError:
            continue
    return result
