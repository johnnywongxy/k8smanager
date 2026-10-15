"""单 Worker 串行调度：领取任务、推进阶段、写日志与事件、超时取消与崩溃恢复。

本模块是整个部署功能的执行心脏，围绕一个硬约束设计：**SQLite + 单 Worker + 串行**
（规划 1、7.2）。由此推出的几条规则贯穿全文：

- **同一时刻只允许一个 ``running``**。领取任务前先确认没有别的任务在跑，抢到的任务
  在同一个事务里改成 ``running``，避免两个 Worker 同时推进同一台机器。
- **Worker 靠文件锁排他**。systemd 只允许一个实例，开发机上误开第二个也会因为抢不到
  锁而立即退出，而不是并发改坏数据。
- **日志批量入库**。ansible 一秒能吐几十行，逐行 INSERT 会把 SQLite 的写锁占满，
  因此积累到 100 行或超过 1 秒才落库一次。
- **崩溃不自动续跑**。启动时把遗留的 ``running`` 标成 ``failed/worker_interrupted``，
  让人来判断该重试还是该清理，而不是自动在未知状态下继续（规划 7.2）。

阶段推进的真实动作交给 ``runner``，成功后的记账交给 ``register``，本模块只负责
「什么时候做什么、做完怎么记、出错怎么停」。
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import (
    addon_overrides,
    artifacts,
    compiler,
    credentials,
    destroy_plan,
    diagnostics,
    node_address,
    node_containers,
    node_helm,
    node_hostname,
    node_kubeconfig,
    node_l4lb,
    node_network,
    node_path,
    node_registry,
    paths,
    platform_relay,
    preflight,
    register,
    registry,
    runner,
    stale_cleanup,
)

# 任务状态常量，与模型里的 choices 对应，写死在这里避免各处拼字符串。
STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_SUCCEEDED = 'succeeded'
STATUS_FAILED = 'failed'
STATUS_CANCELLED = 'cancelled'
STATUS_ROLLBACK_REQUIRED = 'rollback_required'
# 真正进入串行队列的状态：Worker 会按 queued_at 依次把 queued 领成 running（规划 7.2）。
# draft/validating 只是还没提交完，不占队列位置，因此不计入「前方任务数」。
QUEUE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
# 出错时希望人工回滚而不是简单失败的任务类型（规划 10.4、11.2）。
ROLLBACK_ON_FAILURE_TASKS = ('upgrade', 'restore')
# 新建集群类任务，走 01→07 的完整安装流程。
FULL_INSTALL_TASKS = ('deploy_aio', 'deploy_ha')
# 需要先比对集群配置版本的任务类型（规划 6.1）。
REVISION_GUARDED_TASKS = ('add_master', 'add_worker', 'upgrade', 'restore')

# 日志批量入库阈值：行数与时间任一到达就先落盘（规划 7.2）。
LOG_BATCH_LINES = 100
LOG_BATCH_SECONDS = 1.0
# 取消标记的轮询间隔：太频繁会不停打断 SQLite，太慢则取消响应迟钝。
CANCEL_POLL_SECONDS = 2.0
# 任务结束前的最大等待，超过就放弃收尾，避免 Worker 卡死。
SHUTDOWN_GRACE_SECONDS = 30


class WorkerBusy(Exception):
    """已经有另一个 Worker 在运行。"""


class TaskAborted(Exception):
    """任务被取消或超出预算，用于从任意阶段直接跳出。"""

    def __init__(self, error_code, message):
        """初始化中止异常。

        参数：`error_code` 表示写入任务的错误码；`message` 表示可展示的中文原因。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class WorkerLock:
    """基于文件的排他锁，保证平台机同时只有一个部署 Worker。

    Linux 用 ``flock``，Windows 用 ``msvcrt.locking``。两者都是「进程退出即自动释放」
    的内核级锁，因此 Worker 被 kill -9 也不会留下需要人工清理的僵尸锁文件。
    """

    # 加锁的字节偏移。内容写在文件开头，锁却打在第 4096 字节上，原因是 Windows 的
    # 字节区间锁连「读」一起挡：锁住第 0 字节之后，``read_text`` 会因为读不到那一字节
    # 直接抛 PermissionError，``holder()`` 就永远只能返回空。挪开一段后两者互不干扰。
    # Linux 的 ``flock`` 是整文件锁，不看偏移，因此这一改动不影响 Linux 行为。
    LOCK_BYTE_OFFSET = 4096

    def __init__(self, path=None):
        """初始化锁对象。

        参数：`path` 表示锁文件路径，传 None 时用 ``settings.K8S_DEPLOY_WORKER_LOCK``。
        返回：无显式返回值。
        副作用：只记录路径，不创建文件。
        """
        self.path = Path(path) if path else paths.worker_lock_path()
        self.handle = None

    def acquire(self):
        """尝试获取锁。

        参数：无。
        返回：获取成功返回 True，已被占用返回 False。
        副作用：创建并打开锁文件，获取内核级文件锁。
        """
        paths.ensure_dir(self.path.parent)
        self.handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt

                self.handle.seek(self.LOCK_BYTE_OFFSET)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        # 把持有者写进锁文件，出问题时能看出是谁占着。
        try:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(f'{os.getpid()} {timezone.now().isoformat()}\n'.encode('utf-8'))
            self.handle.flush()
        except OSError:
            # 写不进去不影响排他性，忽略即可。
            pass
        return True

    def holder(self):
        """读取锁文件里记录的持有者，用于在抢锁失败时说明是谁占着。

        参数：无。
        返回：返回锁文件里的那一行文本，读不到或为空时返回空串。
        副作用：只读锁文件，不加锁也不修改文件。

        文件锁本身不会在读取时阻塞（flock 是劝告锁），因此这里可以直接读；
        读到的内容只用于展示，不能作为「锁是否被持有」的判断依据。
        """
        try:
            return self.path.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return ''

    def release(self):
        """释放锁并关闭文件。

        参数：无。
        返回：无显式返回值。
        副作用：释放文件锁并关闭句柄；重复调用无副作用。
        """
        if self.handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt

                # 解锁必须落在加锁时的同一个字节上，否则留下一个悬空的区间锁。
                self.handle.seek(self.LOCK_BYTE_OFFSET)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None

    def __enter__(self):
        """进入上下文时获取锁。

        参数：无。
        返回：返回自身。
        副作用：获取不到锁时抛出 WorkerBusy。
        """
        if not self.acquire():
            raise WorkerBusy('已有另一个部署 Worker 在运行，本实例退出。')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """退出上下文时释放锁。

        参数：`exc_type`、`exc_value`、`traceback` 表示上下文中的异常信息。
        返回：返回 False，不吞掉异常。
        副作用：释放文件锁。
        """
        self.release()
        return False


class TaskContext:
    """一次任务的执行上下文：阶段推进、日志批量入库、事件写入与取消判定。

    日志与事件是两个不同的东西，不能混：**日志**是 ansible 的原始输出，量大、按 seq
    游标增量拉取；**事件**是平台对「现在到哪一步了」的说明，量小、前端直接展示。
    """

    def __init__(self, deployment, workspace, worker_id=''):
        """初始化任务上下文。

        参数：`deployment` 表示 K8sDeployment 实例；`workspace` 表示任务工作区路径；
            `worker_id` 表示当前 Worker 标识。
        返回：无显式返回值。
        副作用：读取数据库中已有的最大日志序号，不修改数据。
        """
        from k8s_deployment.models import K8sDeploymentLog

        self.deployment = deployment
        self.workspace = Path(workspace)
        self.worker_id = worker_id
        self.stage_key = ''
        # 当前阶段的中文名与开始时刻。旁白与静默提示都要用它们说清「现在在哪一步、跑了多久」，
        # 而 ``begin_stage`` 已经拿到了标题，没必要让调用方再查一次阶段表。
        self.stage_title = ''
        self.stage_started = time.monotonic()
        self._buffer = []
        self._buffer_at = time.monotonic()
        self._lock = threading.Lock()
        self._last_cancel_check = 0.0
        self._cancel_requested = False
        self.started_at = time.monotonic()
        current = K8sDeploymentLog.objects.filter(deployment=deployment).order_by('-seq').values_list('seq', flat=True).first()
        self.seq = int(current or 0)

    # ---- 阶段与事件 ----------------------------------------------------

    def begin_stage(self, key, title=''):
        """标记一个新阶段开始。

        参数：`key` 表示阶段标识；`title` 表示阶段中文标题。
        返回：无显式返回值。
        副作用：刷新任务的当前阶段字段并写入一条事件。

        标题同时写进事件的 ``detail``：进度页要在「正在执行：X」里显示它，而那时任务详情
        只能读到 ``current_stage`` 这个键（``artifacts``、``preflight`` 这类平台阶段不在
        `runner.STAGE_TITLES` 里，光有键换不出中文）。标题是在这里定的，就在这里一并留下，
        免得序列化器另抄一份阶段表。
        """
        self.flush(force=True)
        self.stage_key = key
        self.stage_started = time.monotonic()
        label = title or runner.STAGE_TITLES.get(key, key)
        self.stage_title = label
        self.deployment.current_stage = key
        self.deployment.save(update_fields=['current_stage', 'updated_at'])
        self.event('stage_started', f'开始 {label}', stage=key, detail={'title': label})

    def complete_stage(self, key, elapsed=None, title=''):
        """标记一个阶段完成。

        参数：`key` 表示阶段标识；`elapsed` 表示耗时秒数，可为空；`title` 表示阶段中文标题，
            为空时按 ezctl 阶段表查，再查不到就退回标识本身。
        返回：无显式返回值。
        副作用：把阶段追加进任务的已完成阶段列表并写入事件。
        """
        completed = list(self.deployment.completed_stages or [])
        if key not in completed:
            completed.append(key)
        self.deployment.completed_stages = completed
        self.deployment.save(update_fields=['completed_stages', 'updated_at'])
        label = title or runner.STAGE_TITLES.get(key, key)
        suffix = f'，耗时 {elapsed:.1f} 秒' if elapsed else ''
        self.event('stage_finished', f'完成 {label}{suffix}', stage=key)

    def event(self, event_type, message, level='info', detail=None, stage=None):
        """写入一条任务事件。

        参数：`event_type` 表示事件类型；`message` 表示中文说明；`level` 表示级别；
            `detail` 表示结构化明细；`stage` 表示所属阶段，传 None 时用当前阶段。
        返回：返回新建的事件对象。
        副作用：向事件表写入一行，内容已打码。
        """
        from k8s_deployment.models import K8sDeploymentEvent

        return K8sDeploymentEvent.objects.create(
            organization=self.deployment.organization,
            deployment=self.deployment,
            stage=str(stage or self.stage_key or '')[:64],
            level=level,
            event_type=str(event_type or '')[:64],
            message=diagnostics.redact_text(message)[:2000],
            detail=diagnostics.redact_structure(detail or {}),
        )

    # ---- 日志 ----------------------------------------------------------

    def log(self, stream, text, stage=None):
        """缓冲一行日志，达到阈值时批量入库。

        参数：`stream` 表示输出流（stdout/stderr/system）；`text` 表示日志内容；
            `stage` 表示所属阶段，传 None 时用当前阶段。
        返回：无显式返回值。
        副作用：缓冲到内存；每满 100 行或超过 1 秒时写入日志表。
        """
        line = diagnostics.redact_text(text)
        if not line.strip():
            return
        key = str(stage or self.stage_key or '')
        with self._lock:
            self._buffer.append((key, stream, line))
            due = len(self._buffer) >= LOG_BATCH_LINES or (time.monotonic() - self._buffer_at) >= LOG_BATCH_SECONDS
        if due:
            self.flush()

    def flush(self, force=False):
        """把缓冲区里的日志写入数据库。

        参数：`force` 表示忽略时间阈值，立即落库。
        返回：无显式返回值。
        副作用：向日志表批量写入，并清空缓冲区。
        """
        from k8s_deployment.models import K8sDeploymentLog

        with self._lock:
            if not self._buffer:
                self._buffer_at = time.monotonic()
                return
            if not force and (time.monotonic() - self._buffer_at) < LOG_BATCH_SECONDS:
                return
            pending = self._buffer
            self._buffer = []
            self._buffer_at = time.monotonic()

        rows = []
        for stage, stream, line in pending:
            self.seq += 1
            rows.append(K8sDeploymentLog(
                organization=self.deployment.organization,
                deployment=self.deployment,
                seq=self.seq,
                stage=stage[:64],
                level='error' if stream == 'stderr' else 'info',
                stream=str(stream)[:16],
                message=line[:4000],
            ))
        if rows:
            K8sDeploymentLog.objects.bulk_create(rows, ignore_conflicts=True)

    def on_line(self, stream, text):
        """执行器逐行输出时的回调。

        参数：`stream` 表示输出流；`text` 表示一行内容。
        返回：无显式返回值。
        副作用：写阶段原始日志文件并缓冲进日志表；顺带刷新心跳。
        """
        diagnostics.append_stage_log(self.workspace, self.stage_key, f'[{stream}] {text}\n')
        self.log(stream, text)
        self.heartbeat(force=False)

    def say(self, text):
        """写一行平台旁白：进度页能看到的「现在在做什么」。

        参数：`text` 表示中文说明正文。
        返回：无显式返回值。
        副作用：向日志表写入一行（阶段为空时归到当前阶段），并立即落库。

        与 ``on_line`` 的区别是：那是 ansible 的原始输出，这是平台自己说的话。批量入库的
        阈值是「满 100 行或超过 1 秒」，而旁白恰恰出现在「没有输出」的时候 —— 只缓冲不落库，
        用户就还是什么都看不到，所以这里强制冲一次。旁白不写事件表：静默提示每 30 秒一条，
        都记成事件会把真正代表进度的那几条冲没。
        """
        self.log('system', text)
        self.flush(force=True)

    def tick(self):
        """读取循环空转一圈时调用的收尾动作。

        参数：无。
        返回：无显式返回值。
        副作用：把缓冲的日志落库，并按间隔刷新心跳。

        日志与心跳原本都靠「有新输出」驱动：``log`` 只在有新行时才检查时间阈值，
        ``heartbeat`` 也只在 ``on_line`` 里被调到。于是一条永远不再输出的命令（现场就是
        卡住的 etcdctl）会让两者同时停摆 —— 进度页最后一行停在缓冲区边界，任务心跳也不再
        更新，看着既不像在跑、也看不到它跑到哪了。

        这里必须强制落库：静默期间攒着的那一批正等着一个「下一次输出」来触发，而它的
        阈值是 1 秒，等下去没有意义。代价也接近于零 —— 落过之后缓冲区就空了，之后的每一圈
        ``flush`` 都在空缓冲区上直接返回，不查库（心跳另有自己的 5 秒间隔）。
        """
        self.flush(force=True)
        self.heartbeat(force=False)

    def narrate_idle(self, quiet_seconds):
        """长时间没有输出时写一行旁白。

        参数：`quiet_seconds` 表示已经连续多少秒没有读到输出。
        返回：无显式返回值。
        副作用：向日志表写入一行并刷新心跳。

        放在上下文上而不是各调用点：备份、恢复这些阶段是直接调执行器的，不经
        ``Pipeline.run_ezctl``；而它们恰恰是最容易出现「远端安静几分钟」的（取 etcd 快照、
        等 etcd 重启）。一处实现，谁需要谁挂上去。
        """
        label = self.stage_title or self.stage_key
        self.say(
            f'{label}：已连续 {int(quiet_seconds)} 秒没有新输出，'
            f'本阶段已运行 {int(self.stage_elapsed())} 秒，命令仍在目标节点上执行，请继续等待。'
        )
        self.heartbeat()

    def stage_elapsed(self):
        """返回当前阶段已运行的秒数。

        参数：无。
        返回：返回秒数，至少为 0。
        副作用：纯计算，不修改持久化数据。
        """
        return max(time.monotonic() - self.stage_started, 0.0)

    # ---- 心跳、取消与预算 ----------------------------------------------

    def heartbeat(self, force=True):
        """刷新任务心跳，供前端判断 Worker 是否还活着。

        参数：`force` 表示是否忽略间隔直接写库。
        返回：无显式返回值。
        副作用：更新任务的 ``heartbeat_at`` 字段。
        """
        try:
            interval = int(getattr(settings, 'K8S_DEPLOY_HEARTBEAT_SECONDS', 5))
        except (TypeError, ValueError):
            interval = 5
        now = time.monotonic()
        if not force and (now - self._last_cancel_check) < max(interval, 1):
            return
        self._last_cancel_check = now
        self.deployment.heartbeat_at = timezone.now()
        self.deployment.save(update_fields=['heartbeat_at', 'updated_at'])

    def cancelled(self):
        """判断任务是否已被请求取消。

        参数：无。
        返回：已请求取消时返回 True。
        副作用：读取数据库中的取消标记，不修改数据。
        """
        if self._cancel_requested:
            return True
        now = time.monotonic()
        if (now - self._last_cancel_check) < CANCEL_POLL_SECONDS:
            return self._cancel_requested
        self._last_cancel_check = now
        self.deployment.refresh_from_db(fields=['cancel_requested'])
        self._cancel_requested = bool(self.deployment.cancel_requested)
        return self._cancel_requested

    def elapsed(self):
        """返回任务已执行的秒数。

        参数：无。
        返回：返回本进程内已执行的秒数。
        副作用：纯计算，不修改持久化数据。
        """
        return time.monotonic() - self.started_at

    def task_budget(self):
        """返回整任务剩余的可执行秒数。

        参数：无。
        返回：返回剩余秒数，至少为 0。
        副作用：只读取配置，不修改数据。
        """
        try:
            limit = int(getattr(settings, 'K8S_DEPLOY_TASK_TIMEOUT_SECONDS', 28800))
        except (TypeError, ValueError):
            limit = 28800
        return max(limit - self.elapsed(), 0)

    def stage_budget(self):
        """返回本阶段可用的秒数，取阶段上限与整任务剩余时间的较小值。

        参数：无。
        返回：返回秒数，至少为 0。
        副作用：只读取配置，不修改数据。
        """
        return min(runner.stage_timeout_seconds(), self.task_budget())

    def check_budget(self):
        """确认整任务预算还没用完。

        参数：无。
        返回：无显式返回值。
        副作用：预算耗尽时抛出 ``TaskAborted``。
        """
        if self.task_budget() <= 0:
            raise TaskAborted('task_timeout', '任务执行超过整任务时限，已终止进程组并停止后续阶段。')

    def cleanup(self):
        """任务收尾：把剩余日志落库。

        参数：无。
        返回：无显式返回值。
        副作用：强制刷新日志缓冲。
        """
        self.flush(force=True)


def kubeconfig_cluster_names(text):
    """读出一份 kubeconfig 里登记的集群名。

    参数：`text` 表示 kubeconfig 的 YAML 文本。
    返回：返回集群名集合；解析不了、没有 ``clusters`` 段时返回空集合，表示「判断不了」。
    副作用：纯解析，不读写文件。

    用来分辨「这份 kubeconfig 是不是本集群的」。kubeasz 生成 kubeconfig 时把 ``CLUSTER_NAME``
    写进 ``clusters[].name``，而平台渲染 ``config.yml`` 时让 ``CLUSTER_NAME`` 等于集群 slug
    （见 ``compiler.render_config``），所以拿它跟任务的 ``cluster_slug`` 比即可。
    """
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    names = set()
    for entry in data.get('clusters') or []:
        if isinstance(entry, dict) and entry.get('name'):
            names.add(str(entry['name']))
    return names


def command_env(context, kubeasz_root=None):
    """构造 ezctl 运行时的环境变量。

    参数：`context` 表示任务上下文，可为空（没有任务上下文的独立备份只有本机剧本要跑，
        此时不给 ``KUBECONFIG``）；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回环境变量字典。
    副作用：只做路径计算与 ``exists`` 判断，不修改持久化数据。

    显式指定私有 SSH 密钥，既让 ansible 免密登录目标机，也避免它去翻平台机上
    运维人员自己的 ``~/.ssh`` 而连成别的身份。私钥本身不写进任何配置文件。

    密钥类型必须是 ed25519：ansible 用的是**平台机上这个** ``ssh`` 客户端，而它可能
    很老（CentOS 7 自带 OpenSSH 6.6.1，RSA 只会签 SHA-1），现代节点的 sshd 又不收
    SHA-1 签名。详见 ``credentials`` 模块开头。

    **调用方必须把返回值交给 ``runner.run_stage(env=...)``。** 集群目录里的 inventory
    里没有、也不该有任何凭据（备份归档明确不带 SSH 凭据），所以这个环境变量是平台把
    部署私钥交给 ansible 的**唯一**通道：漏了它，ansible 会退到平台机运维人员自己的
    ``~/.ssh/id_rsa``——那把钥匙从来没下发到节点上，剧本第一个 play 就会
    ``UNREACHABLE ... Permission denied (publickey,password)``。

    放在模块级而不是 ``Pipeline`` 里，是为了让不走流水线的调用方（备份、恢复两个剧本
    直接调执行器）复用同一处口径。2026-10-12 的「k8s-single · 备份恢复」失败就是漏了
    这一步：恢复剧本在目标机上什么都还没做，就在 ``Gathering Facts`` 上被拒了。
    """
    env = {'ANSIBLE_HOST_KEY_CHECKING': 'False'}
    # 没有工作区的上下文（只读诊断、单元测试里的简化替身）给不出 kubeconfig 路径，
    # 也就不需要 KUBECONFIG：会用到它的只有 kubeasz 那些 ``connection: local`` 的 kubectl。
    if context is not None and getattr(context, 'workspace', None) is not None:
        env['KUBECONFIG'] = str(kubeconfig_path(context, kubeasz_root))
    key_path = credentials.private_key_path()
    if key_path.exists():
        env['ANSIBLE_PRIVATE_KEY_FILE'] = str(key_path)
    return env


def kubeconfig_path(context, kubeasz_root=None):
    """返回 ezctl 该用的 ``KUBECONFIG`` 路径。

    参数：`context` 表示任务上下文；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回一个 Path。优先用集群目录里 kubeasz 生成的那份，它缺失时才退到工作区里的副本。
    副作用：只做路径计算、``exists`` 判断与一次小文件读取。

    kubeasz 有十几处 ``connection: local`` 的任务在**平台机**上跑裸 ``kubectl``
    （kube-master 的 clusterrolebinding、网络插件的 apply、cluster-addon 的各种
    create），它们既不给 ``--kubeconfig`` 也不设环境变量，只能靠这个环境变量知道该连
    哪个 apiserver、该信哪个 CA。没设时它们会落到 ``http://localhost:8080``，报出来是
    ``dial tcp [::1]:8080: connect: connection refused``。

    **为什么集群目录那份优先**（2026-09-18 的故障实证）：任务重试会复用同一个工作区，
    而工作区根目录里的 ``kubeconfig`` 是 ``register.stage_kubeconfig`` 在上一次**跑完
    之后**放进去的副本，于是重试时它属于上一次那个集群、带的是上一套 CA。实测
    ``runs/deployment-3/kubeconfig`` 里是 ``kk8s`` 的 CA（A3:27:EB…），而这一轮要装的是
    ``k8s``（CA CE:E6:16…，两者 CN 都叫 ``kubernetes-ca``）。KUBECONFIG 指着它，04.kube-master
    里那批裸 ``kubectl`` 就会拿别的集群的 CA 去连新 apiserver，报
    ``x509: certificate signed by unknown authority (… candidate authority certificate
    "kubernetes-ca")`` —— 从报错里完全看不出是「拿错了文件」；同一个剧本里显式写了
    ``--kubeconfig`` 的任务反而全绿，因为那份文件是对的。kubeasz 每个阶段都会用集群当前
    的 CA 重新生成 ``clusters/<slug>/kubectl.kubeconfig``（``roles/kube-master`` 的证书
    任务），它指向 inventory 里的内网地址，也就是 ``platform_relay`` 接管过的那个地址，
    所以它才是「这一次安装的真相」。

    工作区那份留着兜底（集群目录被 ``destroy`` 删掉之后还剩它），但兜底前要确认它确实是
    本集群的：里面登记的是别的集群名时，宁可返回那个不存在的集群目录路径让 ``kubectl``
    明说「文件不存在」，也不能让它拿着别的集群的 CA 连过去。解析不出集群名时按「判断
    不了」处理，继续用工作区那份，避免因为格式意外把本来能跑的流程弄挂。
    """
    workspace_copy = Path(context.workspace) / 'kubeconfig'
    slug = getattr(context.deployment, 'cluster_slug', '')
    if not (kubeasz_root and slug):
        return workspace_copy
    generated = paths.kubeconfig_path(kubeasz_root, slug)
    if generated.exists():
        return generated
    if _workspace_copy_belongs_to(workspace_copy, slug):
        return workspace_copy
    return generated


def _workspace_copy_belongs_to(workspace_copy, slug):
    """判断工作区里的 kubeconfig 副本是不是 ``slug`` 这个集群的。

    参数：`workspace_copy` 表示工作区里的 kubeconfig 路径；`slug` 表示本次任务的集群标识。
    返回：返回 bool。文件不存在或读不了时返回 False。
    副作用：读一次文件。
    """
    try:
        text = workspace_copy.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    names = kubeconfig_cluster_names(text)
    return not names or slug in names


class Pipeline:
    """一次任务的可执行步骤集合，按顺序跑到底或在中途明确失败。"""

    def __init__(self, context, runner_instance, kubeasz_root=None, credentials_map=None, compile_result=None):
        """初始化流水线。

        参数：`context` 表示任务上下文；`runner_instance` 表示执行器；
            `kubeasz_root` 表示 kubeasz 源码根目录；`credentials_map` 表示节点连接信息；
            `compile_result` 表示编译结果。
        返回：无显式返回值。
        副作用：只记录引用，不执行动作。
        """
        self.context = context
        self.runner = runner_instance
        self.kubeasz_root = kubeasz_root
        self.credentials_map = credentials_map or {}
        self.compile_result = compile_result or {}

    def run_ezctl(self, stage_key, arguments, title='', on_line=None):
        """执行一次 ezctl 调用。

        参数：`stage_key` 表示阶段标识；`arguments` 表示 ezctl 参数列表；`title` 表示阶段标题；
            `on_line` 表示逐行输出的额外回调，传 None 时只走上下文的日志回调。
        返回：返回 ``StageResult``。
        副作用：在工作区执行 kubeasz 命令、可能建立本机转发通道，实时写入日志与事件。

        ``on_line`` 是给需要从输出里解析进度的调用方（清理集群的组件进度）留的钩子。
        它必须由调用方自己负责把行继续交给 ``context.on_line``，否则日志面板会空 ——
        这里不替它做这件事，是为了让「一边解析一边照常记日志」这件事在调用点看得见。
        """
        self._ensure_relay()
        self.context.begin_stage(stage_key, title)
        self.context.check_budget()
        argv = runner.ezctl_argv(self.kubeasz_root, arguments)
        self.context.log('system', f'执行命令：{runner.format_command(argv)}')
        self.context.say('该阶段由 ansible 在目标节点上执行，安装软件包或等待远端接口时可能几分钟没有输出。')
        self.context.heartbeat()
        result = self.runner.run_stage(
            stage_key,
            argv,
            cwd=self.kubeasz_root,
            env=self.command_env(),
            on_line=on_line or self.context.on_line,
            timeout=self.context.stage_budget(),
            is_cancelled=self.context.cancelled,
            on_idle=self.narrate_idle,
            on_tick=self.context.tick,
        )
        self.context.log('system', result.describe())
        return result

    def narrate_idle(self, quiet_seconds):
        """长时间没有输出时写一行旁白。

        参数：`quiet_seconds` 表示已经连续多少秒没有读到输出。
        返回：无显式返回值。
        副作用：向日志表写入一行并刷新心跳。

        只提示、不干预：安静不等于卡住，ansible 装包与传镜像都是这样。真要判定卡死有阶段
        超时兜底（``K8S_DEPLOY_STAGE_TIMEOUT_SECONDS``），这里提前终止反而会毁掉一个
        正在正常推进的集群安装。文案与判定都收在上下文里（``TaskContext.narrate_idle``），
        这里只负责把它挂到执行器上。
        """
        self.context.narrate_idle(quiet_seconds)

    def _ensure_relay(self):
        """确保平台机能访问 inventory 里的控制面地址。

        参数：无。
        返回：无显式返回值。
        副作用：可能修改本机网络配置、建立 SSH 连接并写日志。

        挂在这里而不是某一条执行流程里：新建、扩容、升级、恢复都要跑 ezctl，而它们启动的
        ansible 都会执行那批 ``connection: local`` 的 kubectl。这些任务里只有新建会在前面
        解析节点地址（见 ``resolve_cluster_address``），扩容与升级用的是集群现有节点，它们的
        地址只存在于 inventory 里，所以统一以文件为准。
        """
        if not self.kubeasz_root:
            return
        slug = getattr(self.context.deployment, 'cluster_slug', '')
        if not slug:
            return
        platform_relay.ensure_from_inventory(
            self.context, paths.hosts_path(self.kubeasz_root, slug),
        )

    def ensure_lb_relay(self):
        """确保平台机拨得到外部负载均衡的 VIP。

        参数：无。
        返回：无显式返回值。
        副作用：可能修改本机网络配置、建立 SSH 连接并写日志。

        只该在 ``10.ex-lb`` 阶段之前调用。``ex-lb`` 剧本的收尾验活（``roles/ex-lb`` 最后那个
        ``uri`` 任务）带 ``connection: local``，是在**平台机**上拨 ``https://<VIP>:<端口>`` 的
        —— VIP 是整套剧本里唯一一个「平台机会直连、却不在 inventory 任何节点段里」的地址，
        ``_ensure_relay`` 那批控制面地址覆盖不到它。少了这一步，前七个阶段全绿之后，第 8 阶段
        会卡在最后一句验收上超时失败，而集群本身完全正常。详见 ``platform_relay``。
        """
        if not self.kubeasz_root:
            return
        slug = getattr(self.context.deployment, 'cluster_slug', '')
        if not slug:
            return
        platform_relay.ensure_lb_vip(
            self.context, paths.hosts_path(self.kubeasz_root, slug),
        )

    def command_env(self):
        """构造 ezctl 运行时的环境变量。

        参数：无。
        返回：返回环境变量字典。
        副作用：纯计算，不修改持久化数据。

        口径收在模块级 ``command_env``：备份与恢复剧本不走 ``run_ezctl``，它们也要拿到
        同一份环境变量（尤其是 ``ANSIBLE_PRIVATE_KEY_FILE``），不能在这里另写一套。
        """
        return command_env(self.context, self.kubeasz_root)

    def kubeconfig_path(self):
        """返回 ezctl 该用的 ``KUBECONFIG`` 路径。

        参数：无。
        返回：返回一个 Path。
        副作用：只做路径计算与 ``exists`` 判断。

        判定与理由见模块级 ``kubeconfig_path``：备份、恢复两个剧本也要用同一份口径，
        所以这里只是把它接到本流水线上。
        """
        return kubeconfig_path(self.context, self.kubeasz_root)


# ---- 工作区准备 --------------------------------------------------------


def materialize_cluster_config(cluster, kubeasz_root, workspace):
    """把集群当前生效的配置写回新的任务工作区。

    参数：`cluster` 表示 K8sCluster 实例；`kubeasz_root` 表示 kubeasz 源码根目录；`workspace` 表示任务工作区。
    返回：返回 ``{'hosts_path':.., 'config_path':.., 'found': bool}``。
    副作用：在 ``clusters/<slug>/`` 下写入 hosts 与 config.yml 并收紧权限。

    扩容与升级是**在已有集群上继续操作**，因此不能重新渲染一份配置：kubeasz 会根据
    hosts 的内容决定要做什么，任何一处被改写都可能触发非预期的动作。这里严格使用最近
    一次成功任务固化的配置版本快照，保证除本次要新增的节点外，文件逐字节不变。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('-revision').first()
    if revision is None:
        raise artifacts.ArtifactError(
            f'集群「{cluster.name}」还没有任何配置版本记录，无法在它上面执行运维操作。'
        )

    snapshot = revision.snapshot or {}
    cluster_directory = paths.ensure_dir(paths.cluster_dir(kubeasz_root, cluster.slug))
    hosts_file = paths.hosts_path(kubeasz_root, cluster.slug)
    config_file = paths.config_path(kubeasz_root, cluster.slug)
    hosts_file.write_text(str(snapshot.get('hosts') or ''), encoding='utf-8')
    config_file.write_text(str(snapshot.get('config') or ''), encoding='utf-8')
    paths.secure_file(hosts_file)
    paths.secure_file(config_file)
    return {'hosts_path': str(hosts_file), 'config_path': str(config_file), 'revision': revision.revision, 'found': True}


def section_entries(hosts_text, section):
    """解析 inventory 中某个主机段已有的条目。

    参数：`hosts_text` 表示 hosts 文件内容；`section` 表示段名。
    返回：返回该段的非注释主机行列表，段不存在时返回空列表。
    副作用：纯计算，不修改持久化数据。
    """
    current = ''
    entries = []
    known = set(compiler.SECTION_ROLES) | {'all', 'all:vars', 'harbor', 'ex_lb', 'chrony', 'new-master', 'new-node'}
    for line in str(hosts_text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            current = stripped[1:-1]
            continue
        if current != section:
            continue
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            continue
        entries.append(stripped)
        _ = known
    return entries


def next_node_name(hosts_text, section, explicit_name='', node_naming='auto'):
    """为即将加入 inventory 的节点计算节点名。

    参数：`hosts_text` 表示 hosts 文件内容；`section` 表示段名；`explicit_name` 表示用户指定的节点名；
        `node_naming` 表示命名模式。
    返回：返回节点名，etcd 段返回空串。
    副作用：纯计算，不修改持久化数据。
    """
    if section == 'etcd':
        # etcd 段在 kubeasz 模板里是裸 IP，写 k8s_nodename 会污染该组的变量作用域。
        return ''
    if node_naming == 'manual' and explicit_name:
        candidate = str(explicit_name).strip().lower()
        if not compiler._validate_node_name(candidate):
            raise compiler.CompileError(
                f'节点名「{explicit_name}」不合法：只能包含小写字母、数字、- 和 .，且必须以字母数字开头和结尾。'
            )
        return candidate
    return f'{compiler.SECTION_PREFIXES.get(section, "node")}-{len(section_entries(hosts_text, section)) + 1:02d}'


# ---- 各任务的执行流程 --------------------------------------------------


def node_credentials(nodes, include_password=True):
    """组装节点的 SSH 连接信息。

    参数：`nodes` 表示 K8sDeploymentNode 列表；`include_password` 表示是否带上解密后的密码。
    返回：返回 ``{ip: {'user':.., 'port':.., 'password':..}}``。
    副作用：`include_password` 为真时解密机器密码；不写入任何文件。
    """
    machines = [item.machine for item in nodes if item.machine_id]
    mapping = credentials.credential_map(machines, include_password=include_password)
    # 机器已被删除时用节点快照兜底，保证预检仍能给出「没有密码」这类准确原因。
    for item in nodes:
        mapping.setdefault(str(item.ip_address), {'user': item.ssh_user or 'root', 'port': int(item.ssh_port or 22)})
    return mapping


def resolve_cluster_address(context, node, credential):
    """探测节点网卡上的地址，作为它在集群内部通信用的地址。

    参数：`context` 表示任务上下文；`node` 表示节点快照；`credential` 表示该节点的连接信息。
    返回：无显式返回值。
    副作用：建立一条 SSH 连接读一次网卡地址，把结果写回节点快照；地址与填写值不同且平台机
        到不了时，还会在平台机上为它建一条转发通道（见 ``platform_relay``）。

    操作员填的地址只用来连节点（公网、内网都行），inventory 的主机名必须是网卡上真实存在的
    地址，否则 etcd 以 ``bind: cannot assign requested address`` 起不来。探测不出来时保持
    原样 —— 那正是「节点没有 NAT」这种最常见情况下的正确行为。
    """
    entered = str(node.ip_address)
    outcome = node_address.resolve(node, credential)
    address = outcome['address']
    if not address:
        return
    if address == entered:
        if outcome['source'] == node_address.SOURCE_FALLBACK:
            context.log(
                'stderr',
                f'{entered}：没能读到节点的网卡地址，仍按填写的地址部署。'
                '节点在 NAT 后面时 etcd 会因为绑定不到这个地址而启动失败。',
            )
        return

    if node.cluster_ip != address:
        node.cluster_ip = address
        # TimeStampedModel 的 updated_at 是 auto_now，要一起写进 update_fields 才会刷新。
        node.save(update_fields=['cluster_ip', 'updated_at'])
    context.log('system', f'{entered}：网卡地址是 {address}，集群内部按它通信（平台仍走 {entered} 连它）。')
    port = int(credential.get('port') or node.ssh_port or 22)
    # 这一步必须在部署开头就做。kubeasz 不只靠 ansible 连节点：它的 kube-master 与各网络插件
    # 角色都带 ``connection: local``，那些 kubectl 是在**平台机**上跑的，打的是 inventory 里的
    # 地址。平台机到不了这个地址，部署会在 04.kube-master 卡到超时，报出来的是 ansible 的重试
    # 日志，跟「地址填法」看不出关系。详见 ``platform_relay``。
    platform_relay.ensure(context, [{
        'address': address,
        'host': entered,
        'user': str(credential.get('user') or node.ssh_user or 'root'),
        'port': port,
    }])


def _prepare_credentials(context, nodes, credentials_map):
    """确保每一台目标节点都能用平台部署密钥免密登录，并确定它在集群内部使用的地址。

    参数：`context` 表示任务上下文；`nodes` 表示节点列表；`credentials_map` 表示连接信息。
    返回：无显式返回值。
    副作用：可能向目标机写入公钥；读取节点网卡地址并写回快照；失败时抛出 ``TaskAborted``。
    """
    context.begin_stage('credentials', '准备 SSH 免密登录')
    for item in nodes:
        ip = str(item.ip_address)
        credential = credentials_map.get(ip) or {}
        result = credentials.ensure_key_login(item, credential)
        if result['ok']:
            context.log('system', f'{ip}：{result["message"]}')
            resolve_cluster_address(context, item, credential)
            continue
        context.log('stderr', f'{ip}：{result["message"]}')
        raise TaskAborted('ssh_credential_failed', f'{ip} {result["message"]}')
    # 有始就要有终：只 begin 不 complete 会在事件流里留下一个永远不结束的阶段，
    # 事后看日志像是卡在那里，掩盖了「其实全都通了」这个事实。
    context.complete_stage('credentials', title='准备 SSH 免密登录')


def cluster_active_version(cluster):
    """返回集群当前生效的 kubeasz 与 Kubernetes 版本。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回 ``{'kubeasz':.., 'kubernetes':.., 'revision':..}``，没有配置版本时各项为空。
    副作用：读取配置版本表，不修改数据。

    扩容类任务必须沿用集群**当前生效**的 kubeasz 版本，而不是配置中心此刻登记的值：
    配置中心在任务排队期间可能已经改过，用新版本去扩一个旧集群会引入不一致（规划 4.1）。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    if cluster is None:
        return {'kubeasz': '', 'kubernetes': '', 'revision': 0}
    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('-revision').first()
    if revision is None:
        return {'kubeasz': '', 'kubernetes': '', 'revision': 0}
    snapshot = revision.snapshot or {}
    return {
        'kubeasz': str(snapshot.get('kubeasz_version') or ''),
        'kubernetes': str(snapshot.get('kubernetes_version') or ''),
        'revision': revision.revision,
    }


def cluster_master_count(cluster):
    """统计集群当前的控制面节点数量。

    参数：`cluster` 表示 K8sCluster 实例。
    返回：返回承担 master 角色的节点数量，取不到快照时返回 0。
    副作用：读取配置版本表，不修改数据。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    if cluster is None:
        return 0
    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('-revision').first()
    if revision is None:
        return 0
    nodes = (revision.snapshot or {}).get('nodes') or []
    return sum(1 for item in nodes if 'master' in (item.get('roles') or []))


def release_version_for(deployment):
    """决定本次任务要下载哪个 kubeasz 版本的制品。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回 kubeasz 版本号文本。
    副作用：读取配置版本表，不修改数据。

    新建与升级使用任务快照里固化的目标版本；扩容、清理沿用集群当前生效的版本 ——
    清理必须用**当初装它的那套**剧本，版本对不上会清错地方；恢复使用备份里记录的版本
    （由备份服务回填到任务上）。
    """
    if deployment.task_type in ('deploy_aio', 'deploy_ha', 'upgrade', 'restore'):
        return deployment.kubeasz_version
    active = cluster_active_version(deployment.cluster)
    return active['kubeasz'] or deployment.kubeasz_version


def _apply_addon_overrides(context, kubeasz_root):
    """对刚解出来的 kubeasz 树做平台侧的模板修正。

    参数：`context` 表示任务上下文；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：无显式返回值。
    副作用：可能改写源码树里的两份模板（ingress-nginx 的 values、ex-lb 的 l4lb 配置）。

    两件事，容错口径不一样：

    - **ingress-nginx**：关掉对 Prometheus Operator CRD 的依赖，理由见 ``addon_overrides``
      模块开头（kubeasz 把 ``serviceMonitor``/``prometheusRule`` 写死成 true，而平台从不装
      Operator，``07.addons`` 的 helm 会因此报 CRD 找不到而整段失败）。改不动只影响一个附件。
    - **l4lb 的监听地址**：把写死的 ``0.0.0.0`` 改成可配，同机的集群靠它避开与 apiserver 的
      端口冲突。**改不动的后果是部署失败**，但「这次部署需不需要它」在这里判断不了 ——
      本函数在所有任务里都会跑（这样扩容、升级这些不以 07 结尾的任务也不会漏），手里只有
      kubeasz 根目录。所以这里只尽力改并记日志，真正的把关放在 ``_prepare_shared_lb_nodes``：
      那里拿得到节点清单，同机时模板没改好就在原地中止，报错也更贴切。

    两层容错：模块内部已经只报告不抛错，这里再兜一层 —— 给上游擦屁股的旁路动作，
    不能成为「整台集群部署失败」的原因。真没改成功时，07 会在原处报出更准确的原因。
    """
    overrides = (
        (
            'ingress-nginx',
            addon_overrides.apply_ingress_monitoring,
            lambda report: (
                f'已修正 kubeasz 的 ingress-nginx 模板（{report["path"]}）：'
                f'把 {"、".join(report["changed"])} 关掉 —— 集群里没有 Prometheus Operator，'
                'chart 会渲染出集群不认识的 CRD 对象，helm 会因此装不上。'
            ),
        ),
        (
            'l4lb',
            addon_overrides.apply_l4lb_listen,
            lambda report: (
                f'已修正 kubeasz 的 l4lb 模板（{report["path"]}）：把写死的监听地址改成可配的'
                f'{addon_overrides.L4LB_LISTEN_VARIABLE} —— 负载均衡与集群节点同机时，l4lb 得'
                '绑 VIP 才能避开与 apiserver 的端口冲突；不同机的集群不写这个变量，渲染结果不变。'
            ),
        ),
    )
    for label, apply, describe in overrides:
        try:
            report = apply(kubeasz_root)
        except Exception as exc:  # noqa: BLE001 - 旁路动作，任何异常都只记录
            context.log('system', f'修正 kubeasz 的 {label} 模板时出错（不影响部署）：{exc!r}')
            continue
        if report['changed']:
            context.log('system', describe(report))
        elif report['reason']:
            context.log('system', f'未修正 kubeasz 的 {label} 模板：{report["reason"]}。')


def _download_release(context, deployment, runner_instance, with_home=True):
    """从 OSS 准备本任务需要的 kubeasz 源码，并把它的运行环境装好。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；
        `runner_instance` 表示执行器；`with_home` 表示是否解出载体二进制，清理任务传假。
    返回：返回 kubeasz 安装目录的 Path。
    副作用：可能从 OSS 下载并解压源码包、解出载体镜像里的二进制，并记录制品使用情况。

    源码与运行环境一起准备，是因为它们必须成对：``ezctl`` 要求源码在那个固定目录里，
    而 kubeasz 的角色要从同一个目录的 ``bin/`` 取 kubelet、etcdctl 等文件发给节点。
    两者都就位了，后续任何一次 ``ezctl`` 调用才有意义。

    ``with_home=False`` 是给清理任务留的口子：解载体二进制要拉镜像、要在本机装运行环境，
    而清理只需要 kubeasz 的源码剧本 —— 一个「集群已经坏了、要推倒重来」的任务，不该在
    这种纯准备步骤上被卡住。其余任务一律保持默认。
    """
    context.begin_stage('artifacts', '准备 kubeasz 制品')
    version = release_version_for(deployment)
    if not version:
        raise TaskAborted(
            'release_version_missing',
            '无法确定本任务应使用的 kubeasz 版本：集群还没有配置版本记录，或任务快照缺少版本号。',
        )
    context.log('system', f'目标 kubeasz 版本：{version}')
    context.say('正在准备 kubeasz 源码包：本地缓存命中时是秒级，否则要从 OSS 下载并解压，请稍候。')
    root = artifacts.ensure_kubeasz_source(
        version,
        deployment=deployment,
        on_progress=artifacts.download_progress(context, f'kubeasz {version} 源码包'),
    )
    context.log('system', f'kubeasz 源码已就绪：{root}')
    # 源码就绪后立刻修正模板：07.addons 里 kubeasz 会拿模板渲染出 values 交给 helm，
    # 修正必须赶在那之前。放在这里而不是各任务自己的流程里，是因为所有任务都经过本函数
    # ——只有一处需要记得改，也不会漏掉扩容、升级这类不以 07 结尾的任务。
    _apply_addon_overrides(context, root)
    context.complete_stage('artifacts')
    if with_home:
        _prepare_kubeasz_home(context, root, version, runner_instance)
    else:
        context.log('system', '本次是清理任务，跳过载体二进制的准备。')
    return root


def _prepare_kubeasz_home(context, kubeasz_root, release, runner_instance):
    """从载体镜像里解出 kubeasz 需要的二进制。

    参数：`context` 表示任务上下文；`kubeasz_root` 表示 kubeasz 安装目录；
        `release` 表示 kubeasz 版本号；`runner_instance` 表示执行器。
    返回：返回分发摘要字典。
    副作用：可能从 OSS 下载载体镜像 tar、在本机导入镜像并写入 kubeasz 目录。

    这一步以前由 ``ezdown -D`` 完成，平台不走 ezdown，于是 ``/etc/kubeasz/bin`` 一直空着。
    它和镜像分发一样属于「先决条件」：没准备好就往下走，只会换来更晚、更难懂的失败。
    """
    context.begin_stage('kubeasz-home', '准备 kubeasz 运行环境')
    context.say('正在准备 kubeasz 运行环境：需要下载载体镜像并在本机导入，首次执行通常要几分钟。')
    try:
        summary = registry.install_carrier_binaries(context, runner_instance, kubeasz_root, release)
    except registry.RegistryError as exc:
        raise TaskAborted(exc.error_code, exc.message) from exc
    context.complete_stage('kubeasz-home')
    return summary


def _compile(context, deployment, nodes, credentials_map, kubeasz_root):
    """编译集群配置。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：返回编译结果字典。
    副作用：在工作区写入 hosts 与 config.yml。
    """
    context.begin_stage('compile', '生成集群配置')
    result = compiler.compile_cluster(deployment, nodes, credentials_map, kubeasz_root=kubeasz_root)
    result = register.attach_config_texts(result, kubeasz_root, deployment.cluster_slug)
    context.log('system', f'使用模板：{result["template"]}，配置哈希 {result["config_hash"][:12]}…')
    for warning in result.get('warnings') or []:
        context.log('system', f'注意：{warning}')
        context.event('compile_warning', warning, level='warn', stage='compile')
    context.complete_stage('compile')
    context.compile_result = result
    return result


def _run_preflight(context, deployment, nodes, credentials_map):
    """执行环境预检，硬性失败直接中止任务。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息。
    返回：返回预检结果字典。
    副作用：与目标节点建立 SSH 连接并执行只读命令。
    """
    context.begin_stage('preflight', '目标节点预检')
    result = preflight.run_preflight(deployment, nodes, credentials_map)
    for check in result['checks']:
        stage_level = 'error' if check['status'] == 'failed' else 'warn' if check['status'] == 'warned' else 'info'
        context.log('system', f'[{check["status"]}] {check["title"]}：{check["message"]}')
        context.event(
            'preflight_check',
            f'{check["title"]}：{check["message"]}',
            level=stage_level,
            detail={'status': check['status'], 'detail': check['detail']},
            stage='preflight',
        )
    if not result['ok']:
        raise TaskAborted('preflight_failed', '；'.join(result['hard_failures']))
    context.complete_stage('preflight')
    return result


def _clean_stale_install(context, deployment, nodes, credentials_map, preflight_result):
    """按预检结论清理机器上「上一次安装」留下的集群残留。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`preflight_result` 表示本次预检的报告。
    返回：返回清理报告列表；不需要清理时返回空列表。
    副作用：在目标节点上停用并删除上一次安装留下的组件与数据。

    预检的「上一次安装的残留」那一项已经判过「该不该清」—— 勾了「部署前清理上一次安装的
    残留」、且本任务还没有装出过集群时才清（判据见 ``preflight.stale_cleanup_plan``）。
    这里只负责执行：不满足条件时**连阶段都不开**，进度页与日志里不会出现一个什么都没做的
    「清理」。位置排在 ``01.prepare`` 之前，机器这时还是原样（预检与清理都是最先做的事），
    失败中止的代价为零。

    清不掉就中止：残留没清干净的话，接着往下跑必然又失败在 ``02.etcd``，让人再看一遍
    「退出码 1」；不如停在这里把「哪台机器的哪个路径没清掉」说清楚。
    """
    check = None
    for item in (preflight_result or {}).get('checks', []):
        if item.get('name') == 'stale_install':
            check = item
            break
    plan = (check or {}).get('detail', {}).get('cleanup') or {}
    targets = [str(ip) for ip in (plan.get('targets') or [])] if plan.get('will_cleanup') else []
    if not targets:
        return []

    context.begin_stage('stale-cleanup', '清理上一次安装的残留')
    context.log(
        'system',
        f'开始清理 {len(targets)} 台节点上的集群残留：{"、".join(targets)}'
        '（只清 k8s 组件与数据，保留容器运行时的镜像缓存）',
    )
    try:
        reports = stale_cleanup.run_stale_cleanup(context, deployment, nodes, credentials_map, targets)
    except stale_cleanup.StaleCleanupError as exc:
        raise TaskAborted(exc.error_code, exc.message)
    context.complete_stage('stale-cleanup')
    context.log('system', f'残留清理完成：{len(reports)} 台节点已清理干净')
    return reports


def _cluster_registry_pool(deployment):
    """取集群已有的节点，用作镜像宿主的候选。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回节点列表；取不到时返回 None。
    副作用：执行两次数据库查询。

    扩容任务的节点快照里只有刚提交的那台新机器（``cluster_ops.submit`` 就是这么构造的），
    而镜像仓库是集群级设施：拿新机器当宿主等于给集群再造一个仓库，还得把全部镜像经公网重推
    一遍。宿主的候选只能来自集群本身。

    取自**最早那条配置版本**的产出任务，也就是创建这个集群的那次部署：它的节点快照带
    ``cluster_ip``（内网地址），而且顺序与当初挑宿主时逐条一致 —— 换用别处的节点清单，
    两次算出的「第一台 master」可能不是同一台，那会让宿主漂移、镜像全部重传。
    """
    from k8s_cluster.models import K8sClusterConfigRevision

    from k8s_deployment.models import K8sDeployment

    cluster = getattr(deployment, 'cluster', None)
    if cluster is None:
        return None
    revision = K8sClusterConfigRevision.objects.filter(cluster=cluster).order_by('revision').first()
    if revision is None or not revision.source_deployment_id:
        return None
    source = K8sDeployment.objects.filter(pk=revision.source_deployment_id).first()
    if source is None:
        return None
    rows = list(source.nodes.order_by('id'))
    return rows or None


def _publish_images(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance,
                    host_pool=None):
    """把本次部署需要的镜像分发到节点拉得到的地方。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点快照列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录；
        `runner_instance` 表示执行器；`host_pool` 表示挑镜像宿主的候选节点。
    返回：返回分发摘要字典，其中 ``distribution`` 给出本次走的是哪条路。
    副作用：可能启动 registry 容器、从 OSS 下载镜像 tar 并推送；走节点侧时还会在节点上装
        registry、开关一条转发隧道。

    kubeasz 的镜像一律从 ``easzlab.io.local:5000`` 拉。所以本步骤必须排在第一个 ezctl 阶段之前：
    节点一上线就要能用，否则 Pod 全部卡在 ImagePullBackOff —— 连 pause 沙箱镜像都拉不到，
    一个 Pod 都起不来。

    分发到平台本机还是分发到节点，由 ``registry.choose_distribution`` 探测决定；走节点侧时
    还要在 ``01.prepare`` 之后调一次 ``_align_registry_resolution``，因为那个 role 会把
    ``/etc/hosts`` 里的地址改回「节点看到的 SSH 源地址」。
    """
    context.begin_stage('images', '准备镜像仓库')
    try:
        summary = registry.publish_images(
            context, runner_instance, deployment, kubeasz_root, release_version_for(deployment),
            nodes=nodes, credentials_map=credentials_map, host_pool=host_pool,
        )
    except registry.RegistryError as exc:
        # 镜像没备好就往下走，只会换来一堆更晚、更难懂的失败，不如在这里停住。
        raise TaskAborted(exc.error_code, exc.message) from exc
    context.complete_stage('images')
    return summary


def _align_registry_resolution(context, distribution, nodes, credentials_map):
    """把各节点的 ``easzlab.io.local`` 指向托管镜像仓库的那台节点。

    参数：`context` 表示任务上下文；`distribution` 表示 ``_publish_images`` 给出的分发结论；
        `nodes` 表示节点快照列表；`credentials_map` 表示连接信息。
    返回：无显式返回值。
    副作用：逐台建立 SSH 连接并修改其 ``/etc/hosts``。

    只在节点侧分发时才有事做：走平台本机那条路时，``01.prepare`` 写进去的地址本来就是对的。

    **必须排在 ``01.prepare`` 之后。** 那个 role 用 ``regexp`` 匹配 ``easzlab.io.local``
    整行替换成「节点看到的 SSH 源地址」，在此之前写的留不下来；而节点在 NAT 后面时那个地址
    是回不去的出口地址，正是集群起不来的原因。
    """
    if not distribution or distribution.get('mode') != 'node':
        return
    address = distribution.get('address') or ''
    context.begin_stage('registry-dns', '修正镜像仓库解析')
    try:
        def report(node, line):
            """把每台节点的改写结果写进阶段日志，出问题时不必再登机器确认。"""
            context.log('system', f'{node.ip_address} 的 {node_registry.REGISTRY_HOSTNAME} 已指向 {address}：{line}')

        node_registry.align_resolution(nodes, credentials_map, address, on_node=report)
    except (node_registry.NodeRegistryError, credentials.CredentialError) as exc:
        # 少改一台，那台机器上所有 Pod 就会以完全相同的方式卡住，而且现象指不到 hosts。
        raise TaskAborted(
            getattr(exc, 'error_code', 'hosts_align_failed'),
            str(getattr(exc, 'message', exc)),
        ) from exc
    context.complete_stage('registry-dns')


# ingress-nginx 的 chart 只挑带这个标签的节点（kubeasz 的
# roles/cluster-addon/templates/ingress-nginx/values.yaml.j2 里的 nodeSelector），而 kubeasz
# 自己发现没有节点匹配时只打印一句 WARNING 就继续 —— 于是 controller 永远 Pending，
# 任务却报「7/7 成功」，界面上一片绿、集群里 ingress 用不了。
INGRESS_NODE_LABEL = 'ingress-controller/provider=ingress-nginx'
# 打标签只是一次 API 调用。给足网络抖动的时间，但没必要占用一整个阶段（默认两小时）的预算。
INGRESS_LABEL_TIMEOUT_SECONDS = 120


def _label_ingress_nodes(context, pipeline, runner_instance, kubeasz_root):
    """给集群里所有节点打上 ingress-nginx 需要的标签。

    参数：`context` 表示任务上下文；`pipeline` 表示当前流水线（借它的 ``KUBECONFIG``）；
        `runner_instance` 表示执行器；`kubeasz_root` 表示 kubeasz 源码根目录。
    返回：无显式返回值。
    副作用：在**平台机**上执行一次 kubectl，修改集群里的节点标签。

    位置卡在 ``06.network`` 之后、``07.addons`` 之前：07 才会部署 ingress-nginx，标签必须
    赶在它前面存在；而 kubectl 又要等 apiserver 真的能连（06 起 cluster-addon 才依赖它）。
    单节点部署同样适用 —— 那个唯一的节点既是 master 也是 node，``--all`` 正好把它选上。

    命令走的是平台机的裸 ``kubectl`` 加 ``pipeline`` 指定的 ``KUBECONFIG``，与 kubeasz 那批
    ``connection: local`` 的任务是同一套（见 ``Pipeline.kubeconfig_path``），不额外引入
    连节点、找 kubectl 的第二条路径。为此它也不需要自己确认转发通道：紧邻的 06.network
    已经在 ``Pipeline.run_ezctl`` 里 ensure 过一次，而通道要到任务收尾才拆。
    ``--overwrite`` 让重试安全：标签已存在时 kubectl 默认报错退出，没有它，一次重跑就会卡在
    这条本已成功的一步上。
    """
    context.begin_stage('ingress-label', '标记 ingress 节点')
    argv = ['kubectl', 'label', 'node', '--all', INGRESS_NODE_LABEL, '--overwrite']
    context.log('system', f'执行命令：{runner.format_command(argv)}')
    context.heartbeat()
    result = runner_instance.run_stage(
        'ingress-label',
        argv,
        cwd=str(kubeasz_root),
        env=pipeline.command_env(),
        on_line=context.on_line,
        timeout=INGRESS_LABEL_TIMEOUT_SECONDS,
        is_cancelled=context.cancelled,
        on_idle=context.narrate_idle,
        on_tick=context.tick,
    )
    context.log('system', result.describe('标记 ingress 节点'))
    if result.cancelled:
        raise TaskAborted('cancelled', '任务已被取消。')
    if not result.ok:
        # 标签没打上时放任 07 继续跑，换来的是一份「全部成功」的假象和一个永远 Pending 的
        # controller —— 正是这次要根治的那个故障形态，所以这里宁可让任务失败。
        raise TaskAborted(
            result.error_code,
            f'给节点打 ingress 标签失败（{result.describe("标记 ingress 节点")}）。'
            f'没有 {INGRESS_NODE_LABEL} 标签，ingress-nginx 的 controller 匹配不到节点、'
            '会一直处于 Pending，因此在这里中止，避免报出一份不可用的「成功」。',
        )
    context.complete_stage('ingress-label')


def _check_revision(context, deployment):
    """比对任务提交时记录的集群配置版本与当前版本。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例。
    返回：无显式返回值。
    副作用：只读取配置版本表。

    不一致说明集群已经被别的任务改过，用一份过期配置去操作会覆盖掉别人的变更，
    因此以 ``stale_cluster_revision`` 直接失败（规划 6.1）。
    """
    if deployment.task_type not in REVISION_GUARDED_TASKS:
        return
    if deployment.cluster is None:
        raise TaskAborted('cluster_missing', '任务没有关联集群，无法执行运维操作。')
    current = register.current_revision(deployment.cluster)
    if deployment.expected_config_revision and current != deployment.expected_config_revision:
        raise TaskAborted(
            'stale_cluster_revision',
            f'集群配置已从版本 {deployment.expected_config_revision} 变为 {current}，'
            '本次任务基于过期配置提交，已中止以避免覆盖新的集群状态。请重新发起任务。',
        )
    context.log('system', f'集群配置版本校验通过：revision {current}')


def _guard_deployed_cluster(context, deployment):
    """确认集群是平台部署型且状态允许运维。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例。
    返回：无显式返回值。
    副作用：只读取集群记录。

    接入型（imported）集群不是平台装出来的，平台既没有它的 hosts 也没有配置版本，
    任何 add/upgrade/restore 都无从下手，因此一律拒绝（规划 6.1）。
    """
    cluster = deployment.cluster
    if cluster is None:
        raise TaskAborted('cluster_missing', '任务没有关联集群，无法执行运维操作。')
    if cluster.origin != 'deployed':
        raise TaskAborted('cluster_not_deployed', f'集群「{cluster.name}」是接入型集群，平台不提供运维操作。')
    if cluster.status == 'disabled':
        raise TaskAborted('cluster_disabled', f'集群「{cluster.name}」已禁用，不能执行运维操作。')
    if cluster.status == 'destroyed':
        # 已经清过的集群没有任何节点在跑 k8s，再清一次只会连上机器跑一遍空剧本；
        # 真正危险的是 slug 已被清空，拿不到集群目录，无从确定该用哪套 inventory。
        raise TaskAborted(
            'cluster_destroyed',
            f'集群「{cluster.name}」已经清理过了，无需重复清理。请直接重新部署。',
        )
    if cluster.status == 'deploying':
        raise TaskAborted('cluster_deploying', f'集群「{cluster.name}」仍在部署中，请等待当前任务结束。')


def run_full_install(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance):
    """执行新建集群：01→07 全部阶段加健康检查，启用外部负载均衡时再加一步 10。

    单节点（``topology_mode='aio'``）与多节点（``'ha'``）走的是同一条路径，区别只在
    配置编译出来的 inventory 与节点数量 —— 下面那道「多节点至少 3 个控制面」的校验
    是唯一的拓扑差异。启用外部负载均衡的集群会多跑一个 ``10.ex-lb``：它由阶段清单里的
    ``requires`` 决定，不在这里写 if；负载均衡与集群节点同机的集群还会在它之前多一步
    ``_prepare_shared_lb_nodes``（理由见那个函数）。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录；`runner_instance` 表示执行器。
    返回：返回结果摘要字典。
    副作用：在目标节点上完整安装 Kubernetes 集群，并回填集群、配置版本与健康记录。
    """
    pipeline = Pipeline(context, runner_instance, kubeasz_root=kubeasz_root, credentials_map=credentials_map)
    compile_result = _compile(context, deployment, nodes, credentials_map, kubeasz_root)
    pipeline.compile_result = compile_result
    preflight_result = _run_preflight(context, deployment, nodes, credentials_map)
    # 预检说「机器上还有上一次安装的残留，本次要清」时，先清干净再往下装。放在这里是因为
    # 它必须在 01.prepare 之前：那一刻机器还没被本任务改过，清的是别人的东西，失败中止也不可惜。
    _clean_stale_install(context, deployment, nodes, credentials_map, preflight_result)

    if deployment.topology_mode == 'ha':
        masters = [item for item in nodes if 'master' in (item.roles or [])]
        if len(masters) < 3:
            raise TaskAborted(
                'ha_requires_three_masters',
                f'多节点集群要求至少 3 个 control-plane/etcd 节点，当前只有 {len(masters)} 个。',
            )

    # 节点名先落到目标机的 hostname 上，再开始装：kubelet 按 inventory 里的 ``k8s_nodename``
    # 注册，而系统主机名 kubeasz 默认不动它（prepare 角色里那个开关默认是关的）。两处名字不
    # 一致时，登机器看到的名字与集群里的节点名对不上 —— 这正是操作员要修的那个毛病。此刻还
    # 没装任何东西，失败中止的代价为零（见 ``node_hostname``）。
    names = compiler.node_name_map(deployment, nodes)
    _apply_node_hostname(context, nodes, names, credentials_map)

    # ezctl new 会用模板覆盖集群目录，平台已经编译好配置，因此这里只补出目录骨架。
    context.begin_stage('cluster-init', '初始化集群目录')
    context.log('system', f'集群目录已由平台生成：{paths.cluster_dir(kubeasz_root, deployment.cluster_slug)}')
    context.complete_stage('cluster-init')

    image_summary = _publish_images(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance)

    for spec in runner.stage_specs_for(deployment.task_type, deployment):
        if spec['key'] == '10.ex-lb':
            # 同机的机器先把模板与内核开关办妥，再让剧本去启动 l4lb —— 它一启动就要绑端口，
            # 那两件事晚一步就来不及了。
            _prepare_shared_lb_nodes(context, deployment, nodes, credentials_map, kubeasz_root)
            # 这一步的验活是在平台机上拨 VIP，得先把通道铺好。放在循环里而不是任务开头：
            # 通道要连的是那两台 LB 机器，前面七个阶段它们还没装东西，建了也只是空转。
            pipeline.ensure_lb_relay()
        result = pipeline.run_ezctl(spec['key'], ['setup', deployment.cluster_slug, spec['step']], spec['title'])
        if result.cancelled:
            raise TaskAborted('cancelled', '任务已被取消。')
        if not result.ok:
            # 可选阶段失败时补一句上下文：集群本身已经装好了，用户不该以为要重装。
            hint = spec.get('failure_hint') or ''
            raise TaskAborted(result.error_code, f'{result.describe(spec["title"])}{hint}')
        context.complete_stage(spec['key'], elapsed=result.duration)
        if spec['key'] == '01.prepare':
            # 这一步刚把 /etc/hosts 里的 easzlab.io.local 改成「节点看到的 SSH 源地址」，
            # 节点侧分发时要在这里改回来，且必须赶在 04.kube-master 拉第一个镜像之前。
            _align_registry_resolution(context, image_summary.get('distribution'), nodes, credentials_map)
            # 也是这一步才把命令拷进节点的 bin_dir，PATH 要在这之后才加得有意义。
            _configure_node_path(context, compile_result, nodes, credentials_map)
            # 同一个角色还会把 kubeconfig 拷成节点上的 /root/.kube/config，而它带幂等标记
            # （``/opt/kubeasz_prepare_tasks``）—— 标记在、角色整段跳过时这一步就没了。
            # 集群本身不受影响，但登机器的人用不了 kubectl，见 ``node_kubeconfig``。
            _ensure_node_kubeconfig(
                context, kubeasz_root, deployment.cluster_slug, nodes, credentials_map,
            )
            # 勾了「Helm」才装：kubeasz 那份分发清单里没有 helm，向导里这个勾选此前只喂了
            # 一处制品预检，落不到节点上（见 ``node_helm``）。
            if deployment.install_helm_cli:
                _configure_node_helm(context, kubeasz_root, compile_result, nodes, credentials_map)
            # 同一个角色里还可能有主机名相关的动作（kubeasz 的 ENABLE_SETTING_HOSTNAME）。
            # 平台要的名字必须赢，所以在这一步之后再只读复核一次，被改回去就写回来。
            _verify_node_hostname(context, nodes, names, credentials_map)
        if spec['key'] == '06.network' and deployment.nginx_ingress:
            # 07.addons 才装 ingress-nginx，标签必须卡在这两步之间：早了 apiserver 还没起，
            # 晚了 chart 已经带着空 nodeSelector 的结果落地。
            _label_ingress_nodes(context, pipeline, runner_instance, kubeasz_root)
        if spec['key'] == '10.ex-lb':
            # l4lb 的那份 conf 是这一步刚渲染出来的，参数只能在这之后改；又必须赶在本轮
            # 跑完，否则「集群装好了、转发却还是 1 秒超时」要等下次重跑才修。放在循环里
            # 而不是收尾处：收尾那几件事都跑完之后才动手，日志里会看不出来是谁改的。
            _tune_lb_proxies(context, nodes, credentials_map)

    # 收尾打扫放在「登记集群」之前：``current_stage`` 记的是最后一次 ``begin_stage``，
    # 任务成功时那一步该是「登记集群」，而不是一个不在阶段条上的收尾动作（规划 8.2 的任务详情页）。
    _cleanup_node_containers(context, compile_result, nodes, credentials_map)

    register.stage_kubeconfig(kubeasz_root, deployment.cluster_slug, context.workspace)
    context.begin_stage('register', '登记集群与配置版本')
    try:
        summary = register.register_deployment_result(
            deployment, compile_result, nodes, kubeasz_root=kubeasz_root,
            note=f'{deployment.get_task_type_display()}（任务 #{deployment.pk}）',
        )
    except register.RegisterError as exc:
        # 站点已经装好了，失败的只是平台侧的记账。用专门的错误码把它与「部署失败」区分开：
        # 该做的不是重装，而是稍后重试本任务补登记（见 register 模块说明）。
        raise TaskAborted('cluster_register_failed', str(exc)) from exc
    if not summary['health']['ok']:
        # 集群已经建出来了，但探测不到 API 就不能对用户说「成功」。
        raise TaskAborted('health_check_failed', summary['health']['message'])
    context.log('system', f'健康检查通过：{summary["health"]["message"]}')
    context.complete_stage('register')
    return summary


def _verify_hosts_entry(context, hosts_file, section, address):
    """核对 ezctl 是否真的把节点写进了 inventory。

    参数：`context` 表示任务上下文；`hosts_file` 表示 inventory 路径；`section` 表示目标段名；
        `address` 表示节点在集群内部使用的地址。
    返回：无显式返回值。
    副作用：只读取文件；缺行时抛出 ``TaskAborted``。

    扩容的主机行现在由 ezctl 自己写（它用 ``sed "/\\[kube_master/a ..."`` 插行）。上游哪天改了
    段名或插入方式，平台会一路「成功」下去，而集群配置版本里从此少一台机器 —— 下一次任务把
    inventory 恢复回来时那台机器就凭空消失了。所以这里必须当场核对。
    """
    entries = section_entries(hosts_file.read_text(encoding='utf-8'), section)
    matched = [item for item in entries if item.split(' ')[0] == address]
    if not matched:
        raise TaskAborted(
            'inventory_entry_missing',
            f'ezctl 没有把 {address} 写进 inventory 的 [{section}] 段，本次变更没有生效，'
            '集群配置版本保持不变。请人工检查平台机上的 hosts 文件与 kubeasz 版本。',
        )
    if len(matched) > 1:
        # ezctl 用的是不带锚点的 sed，inventory 里出现 ``[<section>:vars]`` 这类同前缀段时
        # 它会插到每一处。重复行对 ansible 无害，但得让人知道文件长歪了。
        context.log(
            'stderr',
            f'{address} 在 [{section}] 段里出现了 {len(matched)} 次，ezctl 的插入方式可能把主机行'
            '写进了同名前缀的其他段（例如 [<段名>:vars]）。建议人工核对 hosts 文件。',
        )


def run_add_node(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance, role):
    """执行添加 Master 或添加 Node。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录；
        `runner_instance` 表示执行器；`role` 表示 ``master`` 或 ``node``。
    返回：返回结果摘要字典。
    副作用：改动集群配置并在目标节点上加入集群，追加配置版本与健康记录。
    """
    if len(nodes) != 1:
        raise TaskAborted('single_node_required', '添加节点类任务一次只能提交一台机器。')
    node = nodes[0]
    section = 'kube_master' if role == 'master' else 'kube_node'

    _guard_deployed_cluster(context, deployment)
    if role == 'master' and cluster_master_count(deployment.cluster) < 3:
        # 单节点或两个控制面的集群没有控制面冗余，加 Master 只会制造一个状态说不清楚的集群。
        raise TaskAborted('add_master_requires_ha', '只有多节点拓扑（至少 3 个 control-plane）的部署型集群才支持添加 Master。')

    materialized = materialize_cluster_config(deployment.cluster, kubeasz_root, context.workspace)
    context.log('system', '已恢复集群当前生效的 hosts 与 config.yml')
    _prepare_credentials(context, nodes, credentials_map)
    _run_preflight(context, deployment, nodes, credentials_map)
    # 新机器上什么镜像都没有，解析必须先改对，否则新节点一上线就拉不到沙箱镜像。
    # 宿主候选取自集群已有节点：这份清单里只有新机器，拿它选宿主会给集群再造一个仓库。
    image_summary = _publish_images(
        context, deployment, nodes, credentials_map, kubeasz_root, runner_instance,
        host_pool=_cluster_registry_pool(deployment),
    )

    hosts_file = paths.hosts_path(kubeasz_root, deployment.cluster_slug)
    hosts_text = hosts_file.read_text(encoding='utf-8')
    node_name = next_node_name(hosts_text, section, node.node_name, deployment.node_naming)
    # 新节点的系统主机名同样要先改成它的节点名：它一加入集群就会被按 k8s_nodename 登记，
    # 之后再去改主机名就得等下次重启才彻底生效（见 ``node_hostname``）。严格：改不动就中止，
    # 此刻这台机器还没有任何集群组件。
    _apply_node_hostname(
        context, [node], ({str(node.ip_address): node_name} if node_name else {}), credentials_map,
    )
    # ezctl 要的是 **inventory 里的主机名**，不是平台连接用的地址：它拿这个参数去 hosts 里找
    # 节点，并作为 ansible 的主机模式（``-e NODE_TO_ADD=``）去匹配。节点在 NAT 后面时两个地址
    # 不同，传错了 playbook 会一台机器都匹配不到。
    address = compiler.cluster_address(node)

    # 主机行由 ezctl 自己写：``add-node``/``add-master`` 先拿 ``$2`` 做重复检查，再把 ``${@:2}``
    # 拼成整行插进对应段。平台若先写一行，它直接以「already existed」退出 2（判重用的
    # ``sed -n '/^\[kube_master/,...' | grep -E "^$2$|^$2 "``），所以地址与字段要分开传给它。
    # 字段正是平台本来要写的那份（见 ``compiler.host_fields``），两边拼出来的行逐字相同。
    fields = compiler.host_fields(node, section, node_name, credentials_map)
    if role == 'master':
        # 新 Master 不进 ``[etcd]``：etcd 成员是另一回事。``ezctl add-master`` 跑的
        # 23.addmaster.yml 里没有 etcd 角色，装 etcd 的是独立操作 ``ezctl add-etcd``。
        # 平台若提前把这台机器写进 ``[etcd]``，那份清单就多出一个根本没有 etcd 的成员 ——
        # ``roles/kube-master/vars/main.yml`` 的 ``ETCD_ENDPOINTS`` 正是拿 ``groups['etcd']``
        # 拼 ``--etcd-servers`` 的，于是新机器自己的 apiserver 会把它那个空的 2379 当成一个
        # etcd 端点；备份、启动/停止、恢复、换证书这些 ``hosts: etcd`` 的剧本也会照着清单去找
        # 它；etcd 台数还从奇数变偶数，与平台自己强制的「etcd 必须奇数台」打架。
        # kubeasz 也是这么分的：op-master.md 里新增的那台 master 并不进 etcd 组。
        context.log(
            'system',
            f'{address} 以 control-plane 身份加入，etcd 成员保持原样（kubeasz 的 add-master 不安装 etcd）',
        )
    context.log('system', f'待加入集群：{address}（{node.ip_address}，节点名 {node_name or "-"}）')

    pipeline = Pipeline(context, runner_instance, kubeasz_root=kubeasz_root, credentials_map=credentials_map)
    command = 'add-master' if role == 'master' else 'add-node'
    stage_key = 'add-master' if role == 'master' else 'add-node'
    title = '添加 Master' if role == 'master' else '添加 Node'
    arguments = [command, deployment.cluster_slug, address]
    if fields:
        arguments.append(fields)
    result = pipeline.run_ezctl(stage_key, arguments, title)
    if result.cancelled:
        raise TaskAborted('cancelled', '任务已被取消。')
    if not result.ok:
        # 已经产生一部分资源时不自动删除，交给人工判断（规划 10.2）。
        raise TaskAborted(result.error_code, result.describe(title) + '。已产生的集群资源不会被自动删除，请人工确认后再重试。')
    _verify_hosts_entry(context, hosts_file, section, address)
    # 这一步里新节点的 prepare 跑过了，与新建集群一样复核一次主机名有没有被上游改动。
    _verify_node_hostname(
        context, [node], ({str(node.ip_address): node_name} if node_name else {}), credentials_map,
    )
    hosts_text = hosts_file.read_text(encoding='utf-8')
    context.complete_stage(stage_key, elapsed=result.duration)
    # 这一步里的 prepare 会把新节点的 easzlab.io.local 改回「它看到的 SSH 源地址」。
    # 在这里改回来时新节点的 kubelet 可能已经抢跑并失败了一两次，containerd 会自动重试，
    # 对齐之后就拉得到 —— 所以顺序上晚一步是可以接受的，但不能不做。
    _align_registry_resolution(context, image_summary.get('distribution'), nodes, credentials_map)
    # 新节点同样要拿到系统 PATH 上的 bin_dir：这一步的 prepare 也只在它的 ~/.bashrc 里加一行，
    # 运维登上这台新机器时不该有和别的节点不一样的手感。目录取自集群当前生效的 inventory。
    _configure_node_path(context, materialized, nodes, credentials_map)
    # 新节点上的 kubeconfig 同理：这一步的 prepare 会拷一份，但它带幂等标记 —— 机器若是
    # 「以前当过节点、后来被清理过」的那台，标记还在，这一份就又不会写。见 ``node_kubeconfig``。
    _ensure_node_kubeconfig(
        context, kubeasz_root, deployment.cluster_slug, nodes, credentials_map,
    )
    # helm 同理：同一个集群里，新机器的命令清单不该与老机器不一样 —— 扩进来的这台同样是
    # 「运维会登上去干活」的机器，而它的 prepare 一样只发 kubeasz 清单里那几样。
    if deployment.install_helm_cli:
        _configure_node_helm(context, kubeasz_root, materialized, nodes, credentials_map)
    # 新节点上同样有 calico 那批跑完就退的 initContainer —— 同一个集群里的同一件事，
    # 不该因为「这台是后加进来的」而例外。同样放在「登记配置版本」之前。
    _cleanup_node_containers(context, materialized, nodes, credentials_map)

    compile_result = {
        'config_hash': compiler.config_hash(hosts_text, paths.config_path(kubeasz_root, deployment.cluster_slug).read_text(encoding='utf-8')),
        'template': 'incremental',
        'resolved_versions': {},
    }
    compile_result = register.attach_config_texts(compile_result, kubeasz_root, deployment.cluster_slug)
    register.stage_kubeconfig(kubeasz_root, deployment.cluster_slug, context.workspace)
    context.begin_stage('register', '登记配置版本')
    summary = register.register_deployment_result(
        deployment, compile_result, nodes, kubeasz_root=kubeasz_root,
        note=f'{title} {node.ip_address}（任务 #{deployment.pk}）',
    )
    if not summary['health']['ok']:
        raise TaskAborted('health_check_failed', summary['health']['message'])
    context.log('system', f'健康检查通过：{summary["health"]["message"]}')
    context.complete_stage('register')
    return summary


def run_upgrade(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance):
    """执行升级：同次版本的补丁升级，或升到下一个次版本。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录；`runner_instance` 表示执行器。
    返回：返回结果摘要字典。
    副作用：升级目标集群，追加配置版本与健康记录；失败进入 ``rollback_required``。

    跨次版本时 ``kubeasz_root`` 这一棵树本身就是目标版本的制品（``cluster_ops`` 在构造任务时
    把 ``kubeasz_version`` 换成了目标版本对应的那一版），所以 ``bin/`` 里已经是新版本的
    二进制，``ezctl upgrade`` 才有东西可滚。
    """
    from k8s_deployment.services import backup as backup_service
    from k8s_deployment.services import upgrade_precheck

    _guard_deployed_cluster(context, deployment)
    cluster = deployment.cluster
    current_version = str(cluster.kube_version or '').lstrip('v')
    target_version = str(deployment.kubernetes_version or '').lstrip('v')
    _assert_upgrade_target(context, current_version, target_version)

    # 先跑便宜的前置检查（版本 skew、驱逐条件、目标制品），再花时间做备份：
    # 一台版本已经混杂的集群不值得先耗掉一次完整备份（规划 10.4）。
    # etcd quorum 不在这里，它由后面的预检里的仲裁检查项负责。
    upgrade_precheck.run_upgrade_prechecks(context, deployment, target_version, kubeasz_root)

    # 升级前强制完成一次可用备份（规划 10.4）：没有退路的升级不允许开始。
    # 备份失败必须让整个升级失败并落到 rollback_required：备份没成说明退路不存在，
    # 此时继续升级等于把集群置于不可回退的状态。
    if getattr(settings, 'K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP', True):
        context.begin_stage('pre-upgrade-backup', '升级前强制备份')
        try:
            record = backup_service.create_backup(
                cluster, deployment=deployment, context=context, kubeasz_root=kubeasz_root,
                runner_instance=runner_instance, credentials_map=credentials_map,
            )
        except backup_service.BackupError as exc:
            raise TaskAborted('pre_upgrade_backup_failed', f'升级前备份未完成，升级已中止：{exc}') from exc
        context.log('system', f'升级前备份完成：{record.backup_id}')
        context.event('pre_upgrade_backup', f'升级前备份已完成：{record.backup_id}', detail={
            'backup_id': record.backup_id,
            'archive_sha256': record.archive_sha256,
            'archive_size': record.archive_size,
        })
        context.complete_stage('pre-upgrade-backup')
    else:
        context.event(
            'pre_upgrade_backup_skipped',
            '平台配置已关闭「升级前强制备份」，本次升级没有可回退的备份点。',
            level='warn',
        )

    materialize_cluster_config(cluster, kubeasz_root, context.workspace)
    _upgrade_cluster_config(context, kubeasz_root, deployment, target_version)
    _run_preflight(context, deployment, nodes, credentials_map)
    # 升级会引入新版本的镜像，先把它们备进 registry，再让节点去拉。宿主必须还是原来那台：
    # 换一台的话，新镜像推到了别处，而升级过程中节点仍在从原来那台拉 —— 拉不到就是一堆
    # ImagePullBackOff。候选因此取自集群已有节点，而不是本次任务的节点清单。
    image_summary = _publish_images(
        context, deployment, nodes, credentials_map, kubeasz_root, runner_instance,
        host_pool=_cluster_registry_pool(deployment),
    )

    pipeline = Pipeline(context, runner_instance, kubeasz_root=kubeasz_root, credentials_map=credentials_map)
    result = pipeline.run_ezctl('upgrade', ['upgrade', deployment.cluster_slug], '版本升级')
    if result.cancelled:
        raise TaskAborted('cancelled', '升级已被取消。')
    if not result.ok:
        raise TaskAborted('upgrade_failed', result.describe('版本升级') + '。平台不会自动降级，请人工确认集群状态。')
    context.complete_stage('upgrade', elapsed=result.duration)
    # 升级不重跑 prepare，这一行本该早就对了；再对一次是廉价的兜底，防止有人手工改过它。
    _align_registry_resolution(context, image_summary.get('distribution'), nodes, credentials_map)

    compile_result = {
        'config_hash': compiler.config_hash(
            paths.hosts_path(kubeasz_root, deployment.cluster_slug).read_text(encoding='utf-8'),
            paths.config_path(kubeasz_root, deployment.cluster_slug).read_text(encoding='utf-8'),
        ),
        'template': 'upgrade',
        'resolved_versions': {},
    }
    compile_result = register.attach_config_texts(compile_result, kubeasz_root, deployment.cluster_slug)
    register.stage_kubeconfig(kubeasz_root, deployment.cluster_slug, context.workspace)
    context.begin_stage('register', '登记配置版本')
    summary = register.register_deployment_result(
        deployment, compile_result, nodes, kubeasz_root=kubeasz_root,
        note=f'升级到 {target_version}（任务 #{deployment.pk}）',
    )
    cluster.kube_version = target_version
    cluster.save(update_fields=['kube_version', 'updated_at'])
    if not summary['health']['ok']:
        raise TaskAborted('health_check_failed', summary['health']['message'])
    # 升级前已经确认过所有节点 Ready，升级后又有节点掉出 Ready 说明这次滚动没走完，
    # 不能记成一次成功的升级（规划 10.4：升级后要检查节点状态）。
    not_ready = (summary['health'].get('detail') or {}).get('node_summary', {}).get('not_ready') or []
    if not_ready:
        raise TaskAborted(
            'upgrade_nodes_not_ready',
            f'升级完成后以下节点仍未就绪：{"、".join(not_ready)}。'
            '升级可能没有走完，请人工确认这些节点上的 kubelet 与容器运行时状态。',
        )
    context.log('system', f'健康检查通过：{summary["health"]["message"]}')
    context.complete_stage('register')
    return summary


def run_destroy(context, deployment, nodes, credentials_map, kubeasz_root, runner_instance):
    """清理集群：把 k8s 组件与数据从节点上清掉，让这批机器能重新部署。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；`nodes` 表示节点列表；
        `credentials_map` 表示连接信息；`kubeasz_root` 表示 kubeasz 源码根目录；`runner_instance` 表示执行器。
    返回：返回结果摘要字典。
    副作用：在目标节点上跑 kubeasz 的清理剧本、删除节点上的虚拟网卡、删除 kubeasz 集群目录、
        卸载节点侧镜像仓库、撤掉平台加进系统 PATH 的两处配置，并把集群与机器记录改成
        「已销毁 / 未分配」。

    用 kubeasz 自带的 ``ezctl destroy``（``playbooks/99.clean.yml``）：它停掉并删除 etcd、
    apiserver、controller-manager、scheduler、kubelet、kube-proxy，卸掉 kubelet 挂载，
    删 ``/etc/kubernetes``、``/etc/cni``、各 CNI 目录、containerd 与 etcd 的数据目录。
    平台不自己拼这套清理命令：少删一样，下次部署就会踩到它。

    **它清不掉的由平台补上。** 剧本只清组件与数据，虚拟网卡（``kube-ipvs0`` 上挂着整段
    Service 地址、``nodelocaldns``、Calico 的 ``cali*``）它一条都不删，最后只提示操作员
    自己重启机器。留着那些网卡，下一次部署的「网段合法性」预检就会硬失败，因此
    ``destroy.network`` 这一步把它们删掉（见 ``node_network``）。

    **不跑预检。** 要清理的集群端口（6443/2379/10250）必然是被占着的，而那正是 ``_check_ports``
    判硬失败的条件 —— 跑预检等于把自己挡在门外。清理的对象还可能已经病到起不来，预检更没意义。

    七个阶段按 ``runner.DESTROY_STAGE_SPECS`` 记账，它们就是这里真实做的事：复核节点 →
    ezctl 清组件 → 删节点虚拟网卡 → 删集群目录 → 卸节点镜像仓库 → 撤 PATH → 解绑机器与登记。
    只有 ezctl 那一步失败才判任务失败：那说明节点上什么都没清掉，重试是唯一的出路。
    其余几步都是「尽力而为」，问题记进日志给操作员。

    组件级进度（第 2 步里清掉了哪几类东西）由 ``destroy_plan`` 解析 ansible 的 TASK banner
    得出来，它挂在 ``run_ezctl`` 的 ``on_line`` 上，不参与成败判定。

    ``bin_dir`` 里的那批命令（kubectl、crictl、etcdctl…）**不删** —— 删除动作在 kubeasz 的
    ``roles/clean`` 里本来就是注释掉的，那批文件与集群状态无关，留着下次部署还能用。
    平台只把自己加的那两处 PATH 配置撤掉，并在日志里说明这一点。
    """
    _guard_deployed_cluster(context, deployment)
    cluster = deployment.cluster
    slug = str(getattr(cluster, 'slug', '') or '').strip()
    if not slug:
        raise TaskAborted(
            'cluster_slug_missing',
            f'集群「{cluster.name}」没有集群标识，无法确定该用哪一套 kubeasz 配置去清理。',
        )

    context.begin_stage('destroy.targets', '复核待清理节点')
    targets = _destroy_target_nodes(context, cluster, nodes)
    _prepare_credentials(context, targets, credentials_map)
    materialized = materialize_cluster_config(cluster, kubeasz_root, context.workspace)
    context.complete_stage('destroy.targets')

    # 组件进度由 ansible 的 TASK banner 推出来（见 destroy_plan 的模块说明）。清单在这里生成：
    # 它要按集群真实的 CNI 与数据目录来列，而这些只在配置快照里。
    components = destroy_plan.components_for(cluster, deployment)
    watcher = destroy_plan.DestroyProgressWatcher(context, components, inner=context.on_line)
    context.event(
        'destroy_plan',
        f'本次将清理 {len(components)} 类组件：' + '、'.join(item['label'] for item in components),
        detail={'components': [item['key'] for item in components]},
    )

    pipeline = Pipeline(context, runner_instance, kubeasz_root=kubeasz_root, credentials_map=credentials_map)
    result = pipeline.run_ezctl(
        'destroy.components', ['destroy', slug], '清理节点上的组件', on_line=watcher,
    )
    if result.cancelled:
        raise TaskAborted('cancelled', '清理已被取消。')
    if not result.ok:
        raise TaskAborted(
            'destroy_failed',
            result.describe('清理节点上的组件') + '。节点上的残留不会被自动删除，请人工确认后再重试。',
        )
    context.complete_stage('destroy.components', elapsed=result.duration)
    # ezctl 成功收尾时把所有组件兜底标成已清理：真机上日志行数受 runner.MAX_CAPTURED_LINES
    # 限制，模拟执行则一行 ansible 输出都没有，少了这一步页面会永远停在「待清理」。
    watcher.finish()

    context.begin_stage('destroy.network', '清理节点网络残留')
    network_failures = _cleanup_node_networks(context, targets, credentials_map)
    context.complete_stage('destroy.network')

    context.begin_stage('destroy.config', '删除集群配置目录')
    # bin_dir 取的是当初那份 inventory 里的值：操作员可能改过这个变量，用当前的默认值去撤
    # 会摘错目录（摘不掉，还往 /etc/environment 里留一条自己认不出来的记录）。
    # **必须在删集群目录之前读**：那一行就在下面那个函数要删的 hosts 文件里。
    bin_dir_value = node_path.bin_dir_for(materialized.get('hosts_path'))
    _remove_cluster_dir(context, kubeasz_root, slug)
    context.complete_stage('destroy.config')

    context.begin_stage('destroy.registry', '卸载节点镜像仓库')
    registry_failures = _uninstall_node_registries(context, targets, credentials_map)
    context.complete_stage('destroy.registry')

    context.begin_stage('destroy.path', '撤销节点 PATH 配置')
    path_failures = _restore_node_path(context, targets, credentials_map, bin_dir_value)
    context.complete_stage('destroy.path')

    context.begin_stage('destroy.records', '解绑机器与登记')
    released = _mark_cluster_destroyed(cluster, slug, targets)
    context.complete_stage('destroy.records')

    # kubeasz 的清理剧本最后自己会提示「请重启节点以确保清除虚拟网卡、路由与 iptables|ipvs 规则」，
    # 但那个提示混在几百行 ansible 输出里，而且平台已经把网卡这一半做掉了（见 destroy.network）。
    # 剩下的 iptables 规则不用管，说清楚免得操作员为一句上游提示白重启一次机器。
    context.log(
        'system',
        '清理完成。集群虚拟网卡已由平台删除，iptables/ipvs 规则会由下次部署的 kube-proxy 与网络插件'
        '重建，无需为此重启节点。',
    )
    context.event(
        'destroy_completed',
        '集群已清理，这批机器可以直接重新部署。',
        level='info',
        detail={
            'nodes': [str(getattr(item, 'ip_address', '') or '') for item in targets],
            'network_cleanup_failed': network_failures,
        },
    )
    return {
        'cluster_id': cluster.pk,
        'cluster_name': cluster.name,
        'cluster_slug': slug,
        'destroyed_nodes': [str(getattr(item, 'ip_address', '') or '') for item in targets],
        'released_machines': released,
        'registry_uninstall_failed': registry_failures,
        'registry_data_dir': node_registry.DATA_DIR,
        'node_path_uninstall_failed': path_failures,
        'node_network_cleanup_failed': network_failures,
    }


def _destroy_target_nodes(context, cluster, nodes):
    """挑出本次可以清理的节点，把已经归属别的集群的剔除。

    参数：`context` 表示任务上下文；`cluster` 表示 K8sCluster 实例；`nodes` 表示任务节点列表。
    返回：返回保留下来的节点列表。
    副作用：写日志与事件；一个都不剩时抛出 ``TaskAborted``。

    任务里的节点清单来自提交那一刻的配置版本快照，而**机器表的归属可能已经变了**（IP 被释放、
    分给了别的集群）。拿一份过期的 IP 去清，清掉的就是别人的机器，而且不可恢复（规划 2.5）。
    因此逐台按机器表的 ``cluster_name`` 复核：指向别的集群的剔除并告警。

    机器表里查不到、或者归属为空的不剔除：那台机器没有任何一方声称拥有，而它就在这个集群的
    inventory 里，属于该清的对象。归属判定要同时认「标识」与「中文名称」两种写法（规划 6.2）。
    """
    keys = {str(getattr(cluster, 'slug', '') or '').strip(), str(getattr(cluster, 'name', '') or '').strip()}
    keys.discard('')
    kept, foreign = [], []
    for node in nodes or []:
        owner = str(getattr(getattr(node, 'machine', None), 'cluster_name', '') or '').strip()
        if owner and owner not in keys:
            foreign.append(f'{getattr(node, "ip_address", "")}（已属于 {owner}）')
            continue
        kept.append(node)
    if foreign:
        context.log('system', '以下机器已归属其它集群，本次不清理：' + '、'.join(foreign))
        context.event(
            'destroy_nodes_skipped',
            '部分机器已归属其它集群，已被排除在本次清理之外：' + '、'.join(foreign),
            level='warn',
            detail={'skipped': foreign},
        )
    if not kept:
        raise TaskAborted(
            'no_target_nodes',
            '本次要清理的机器都已经归属其它集群，一台都不该清，已中止。请刷新机器列表后重试。',
        )
    return kept


def _remove_cluster_dir(context, kubeasz_root, slug):
    """删掉 kubeasz 里的集群配置目录。

    参数：`context` 表示任务上下文；`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识。
    返回：无显式返回值。
    副作用：删除 ``<kubeasz>/clusters/<slug>`` 整棵目录树。

    配置版本快照里留着 hosts 与 config.yml，下次要重建随时能从那里还原，所以这里可以放手删：
    留着它的唯一后果是下次用同一个标识部署时目录里混着旧文件。**只删这个子目录**，
    ``clusters`` 本身是软链，不能碰。
    """
    target = paths.cluster_dir(kubeasz_root, slug)
    paths.remove_tree(target)
    if target.exists():
        context.log('system', f'注意：未能删除集群配置目录 {target}，请在下次部署前手工确认。')
    else:
        context.log('system', f'已删除集群配置目录 {target}')


def _prepare_shared_lb_nodes(context, deployment, nodes, credentials_map, kubeasz_root):
    """为「负载均衡与集群节点同机」的机器做两件必须赶在 ``10.ex-lb`` 之前完成的事。

    参数：`context` 表示任务上下文；`deployment` 表示 K8sDeployment 实例；
        `nodes` 表示节点快照列表；`credentials_map` 表示连接信息；
        `kubeasz_root` 表示 kubeasz 源码根目录。
    返回：无显式返回值。
    副作用：必要时改写 kubeasz 的 l4lb 模板，并在同机的机器上打开 ``ip_nonlocal_bind``。

    两件事都必须赶在那个阶段**开始之前**，因为它渲染出 ``/etc/l4lb/conf/l4lb.conf`` 之后
    就在同一个剧本里把 l4lb 启动起来，末尾还有一步连 VIP 要求 200 的验收：

    1. **模板要已经是可配的**。还是写死的 ``0.0.0.0`` 时，同机的机器上它会与 apiserver 抢
       同一个端口，nginx 起不来。``_download_release`` 里已经尽力改过一次，这里再确认一次 ——
       那个函数幂等，而这里拿得到节点清单，「需不需要」判得准，也就有资格在中止任务时报出
       确切原因（而不是等剧本跑到一半给一句 systemd 的启动失败）。
    2. **内核开关要打开**。nginx 绑的 VIP 是本机还没挂上的地址，不开 ``ip_nonlocal_bind``
       会直接 ``EADDRNOTAVAIL``。

    没有同机的机器时整件事跳过：那种机器上端口本来就是空的、l4lb 绑的是通配地址，两件事
    都不需要，也就不该去动它的内核参数。
    """
    shared = compiler.shared_lb_nodes(nodes)
    if not shared:
        return

    report = addon_overrides.apply_l4lb_listen(kubeasz_root)
    if report['reason']:
        raise TaskAborted(
            'l4lb_template_not_patchable',
            f'本次有 {len(shared)} 台机器同时承担负载均衡与集群节点角色，l4lb 必须改绑 VIP '
            f'才能避开与 apiserver 抢同一个端口，但平台的模板修正没能生效：{report["reason"]}。'
            f'需要 kubeasz 的 {addon_overrides.L4LB_TEMPLATE_RELATIVE} 里那一行监听地址改成读 '
            f'{addon_overrides.L4LB_LISTEN_VARIABLE} 变量、并以 0.0.0.0 为兜底值。',
        )

    context.begin_stage('lb-shared-prep', '准备同机的负载均衡机器')
    failures = []
    for node in shared:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            node_l4lb.ensure_nonlocal_bind(context, node, credential)
        except (node_l4lb.NodeL4lbError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            failures.append(f'{ip}：{getattr(exc, "message", exc)}')
    context.complete_stage('lb-shared-prep')
    if failures:
        raise TaskAborted(
            'node_nonlocal_bind_failed',
            '以下机器上没能打开 net.ipv4.ip_nonlocal_bind —— ' + '；'.join(failures)
            + '。同机部署时 l4lb 绑的是 VIP，内核不允许绑一个本机还没有的地址，'
              '这个开关必须打开（平台会写 /etc/sysctl.d/99-k8smanager-lb.conf 并立即生效）。',
        )


def _tune_lb_proxies(context, nodes, credentials_map):
    """逐台把负载均衡机器上的 l4lb 转发参数改成平台的口径。

    参数：`context` 表示任务上下文；`nodes` 表示节点快照列表；`credentials_map` 表示连接信息。
    返回：返回调整失败的节点 IP 列表。
    副作用：逐台建立 SSH 连接，改写 ``/etc/l4lb/conf/l4lb.conf`` 并重载 l4lb。

    在 ``10.ex-lb`` 阶段跑完之后立刻调用（见 ``run_full_install``）：那份配置是这个阶段
    渲染出来的，早于它调用时文件还不存在。没有负载均衡机器时（``_stage_enabled`` 已经把
    这个阶段挡掉了）什么都不做。

    改的是三处 kubeasz 模板里写死、无法通过变量覆盖的参数，理由见 ``node_l4lb`` 模块开头。
    其中最要紧的不是那两个「调优」，而是补上 ``proxy_timeout``：不补的话 nginx 会在连接
    静默 10 分钟后掐断它，``kubectl logs -f`` / ``exec -it`` / ``port-forward`` 都活不过
    那 10 分钟，而且是毫无提示地断。

    **单台失败只记警告，不影响任务结果**：没改成时转发照样能用，只是留在 kubeasz 的默认
    值上；为它判失败会把一次成功的部署报成失败（与 ``_configure_node_path`` 同一种口吻）。
    """
    targets = compiler.lb_nodes(nodes)
    if not targets:
        return []
    context.begin_stage('lb-tuning', '调整负载均衡的转发参数')
    failed = []
    for node in targets:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            report = node_l4lb.apply_node(context, node, credential)
        except (node_l4lb.NodeL4lbError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            failed.append(ip)
            context.log(
                'system',
                f'节点 {ip} 的 l4lb 转发参数未调整（不影响集群使用）：'
                f'{getattr(exc, "message", exc)}。'
                f'可手工在节点上把 {node_l4lb.CONF_PATH} 里的 {node_l4lb.WORKER_PROCESSES_KEY} '
                f'改成 {node_l4lb.WORKER_PROCESSES_VALUE}、{node_l4lb.CONNECT_TIMEOUT_KEY} 改成 '
                f'{node_l4lb.CONNECT_TIMEOUT_VALUE} 并补一行 {node_l4lb.PROXY_TIMEOUT_KEY} '
                f'{node_l4lb.PROXY_TIMEOUT_VALUE}，然后 systemctl reload l4lb。',
            )
            continue
        if report.get('conf') == 'ABSENT':
            context.log(
                'system',
                f'节点 {ip} 上找不到 {node_l4lb.CONF_PATH}，跳过转发参数调整。',
            )
    if failed:
        context.event(
            'lb_proxy_tuning_failed',
            f'有 {len(failed)} 台负载均衡机器的 l4lb 转发参数没能调整，集群本身不受影响；'
            f'但没有 {node_l4lb.PROXY_TIMEOUT_KEY} 时 nginx 会按默认的 10 分钟掐断静默连接，'
            f'走 VIP 的 kubectl logs -f / exec -it / port-forward 会中途断开。',
            level='warn',
            detail={'nodes': failed},
        )
    context.complete_stage('lb-tuning')
    return failed


def _apply_node_hostname(context, nodes, names, credentials_map):
    """把各节点的主机名改成它们的节点名。

    参数：`context` 表示任务上下文；`nodes` 表示节点快照列表；`names` 表示
        ``{ip_address: 节点名}``；`credentials_map` 表示连接信息。
    返回：返回已设置的报告列表。
    副作用：逐台建立 SSH 连接，改系统主机名并写 ``/etc/hostname``。

    与 ``_configure_node_path`` 的「尽力而为」相反，这里**失败即中止**：主机名与节点名一致
    是这套命名模式的前提，静默放过会让「自动命名/手动命名」再次变成只是配置里的一行字。
    中止发生在安装开始之前，节点上还没有任何集群组件，操作员改完重试即可。

    ``names`` 里没有的机器（只有 etcd 或负载均衡角色的机器）不改：它们在集群里没有节点名。
    """
    if not names:
        return []
    context.begin_stage('node-hostname', '统一节点主机名为节点名')
    try:
        applied, _failed = node_hostname.set_hostname(
            context, nodes, names, credentials_map, strict=True,
        )
    except node_hostname.NodeHostnameError as exc:
        raise TaskAborted(exc.error_code, exc.message) from exc
    context.complete_stage('node-hostname')
    return applied


def _verify_node_hostname(context, nodes, names, credentials_map):
    """复核各节点的主机名仍是它的节点名，被改回去就重新写回。

    参数：`context` 表示任务上下文；`nodes` 表示节点快照列表；`names` 表示
        ``{ip_address: 节点名}``；`credentials_map` 表示连接信息。
    返回：返回主机名被上游改动过的节点 IP 列表。
    副作用：每台建一次 SSH 连接执行只读命令；发现不一致时再写一次。

    **只读复核 + 尽力修正，不判任务失败**：走到这里集群已经装了一半，为一个名字把一次
    可能是好的部署报成失败得不偿失。名字真的写不回去时日志里会留下这台的 IP，操作员手工
    补一条命令即可（``node_hostname.manual_command`` 生成的那一条）。
    """
    drifted = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        target = str((names or {}).get(ip) or '')
        if not target:
            continue
        credential = (credentials_map or {}).get(ip) or {}
        actual, error = node_hostname.verify_node(node, credential)
        if error:
            context.log('system', f'节点 {ip} 的主机名复核未成功（不影响集群）：{error}')
            continue
        if actual == target:
            continue
        drifted.append(ip)
        context.log('system', f'节点 {ip} 的主机名在部署过程中变成了 {actual or "（空）"}，正在写回 {target}')
        try:
            node_hostname.apply_node(context, node, target, credential)
        except (node_hostname.NodeHostnameError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            context.log(
                'system',
                f'节点 {ip} 的主机名没能写回 {target}（不影响集群）：'
                f'{getattr(exc, "message", exc)}。可手工执行：{node_hostname.manual_command(target)}',
            )
    if drifted:
        context.event(
            'node_hostname_drifted',
            f'有 {len(drifted)} 台节点的主机名在部署过程中被改动，平台已写回它们的节点名。',
            level='warn',
            detail={'nodes': drifted},
        )
    return drifted


def _configure_node_path(context, compile_result, nodes, credentials_map):
    """把 kubeasz 的 bin 目录加进各节点的系统 PATH。

    参数：`context` 表示任务上下文；`compile_result` 表示编译产物（取其中的 inventory 路径）；
        `nodes` 表示节点快照列表；`credentials_map` 表示连接信息。
    返回：返回写入失败的节点 IP 列表。
    副作用：逐台建立 SSH 连接，写 ``/etc/profile.d/kubeasz-path.sh`` 并改 ``/etc/environment``。

    与 ``_align_registry_resolution`` 同一时机（``01.prepare`` 之后）：那个 role 才把命令拷到
    节点的 ``bin_dir``，在那之前目录还不存在，先加进 PATH 也只是个空目录。它同时也会写
    ``bin_dir`` 那一行到 ``~/.bashrc``，平台补的是它没覆盖到的那一半 —— 其它用户、
    非交互式 shell、以及以后被人重写掉的 ``.bashrc``（详见 ``node_path`` 模块）。

    目录从**渲染后的 inventory** 里读，不写死 ``/opt/kube/bin``：操作员可以把这个变量改到
    数据盘上，跟着走才能加到真正放着命令的那个目录。

    **单台失败只记警告，不影响任务结果。** 这是给人用的便利配置，集群本身是好的；
    为它判失败会把一次成功的部署报成失败，比 PATH 少一项难查得多。
    """
    bin_dir_value = node_path.bin_dir_for(compile_result.get('hosts_path'))
    context.begin_stage('node-path', '把 kubeasz 的 bin 目录加进节点 PATH')
    failed = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            node_path.apply_node(context, node, bin_dir_value, credential)
        except (node_path.NodePathError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            failed.append(ip)
            context.log(
                'system',
                f'节点 {ip} 的 PATH 未配置成功（不影响集群）：{getattr(exc, "message", exc)}。'
                f'可手工在节点上执行：echo "export PATH={bin_dir_value}:$PATH" > {node_path.PROFILE_PATH}',
            )
    if failed:
        context.event(
            'node_path_failed',
            f'有 {len(failed)} 台节点没能把 {bin_dir_value} 加进系统 PATH，集群本身不受影响。',
            level='warn',
            detail={'nodes': failed, 'bin_dir': bin_dir_value},
        )
    context.complete_stage('node-path')
    return failed


def _ensure_node_kubeconfig(context, kubeasz_root, slug, nodes, credentials_map):
    """确保各节点上有 kubectl 的配置（``/root/.kube/config``）。

    参数：`context` 表示任务上下文；`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识；
        `nodes` 表示节点快照列表；`credentials_map` 表示连接信息。
    返回：返回补写了配置的节点 IP 列表。
    副作用：逐台建立 SSH 连接，缺失时在节点上写 ``/root/.kube/config``。

    时机与理由都同 ``_configure_node_path``：``01.prepare`` 里那个 role 自己会做这件事，
    但它带幂等标记（``/opt/kubeasz_prepare_tasks``），节点上留着标记时整段跳过。标记与
    kubeconfig 都会被「清理上一次安装的残留」碰到，而那边从前只删了后者 —— 于是机器处于
    「标记在、配置没了」的半干净状态，再也不会自愈。详见 ``node_kubeconfig`` 模块说明。

    内容就取 kubeasz 生成的那份（``clusters/<slug>/kubectl.kubeconfig``，与 prepare 拷贝的
    是同一个文件），因此写的不是平台自己的那一份、也不需要另做地址改写。读不到就跳过：
    这时 ``01.prepare`` 还没把 kubeconfig 生成出来，属于上游已经出问题，不该在这里报第二次。

    **单台失败只记警告，不影响任务结果**（同 ``_configure_node_path``）：节点上的 kubectl
    配置是给操作员用的便利，集群本身是好的；平台自己的健康检查、资源查看走的是平台机上
    那份 kubeconfig，与它无关。
    """
    text = register.read_generated_kubeconfig(kubeasz_root, slug)
    if not text:
        context.log(
            'system',
            '平台上还没有这个集群的 kubeconfig（本轮 01.prepare 没有生成出来），'
            '跳过节点侧补写',
        )
        return []
    context.begin_stage('node-kubeconfig', '确保节点上有 kubectl 配置')
    written = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            report = node_kubeconfig.ensure_node(context, node, text, credential)
        except (node_kubeconfig.NodeKubeconfigError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            context.log(
                'system',
                f'节点 {ip} 的 kubectl 配置未补写成功（不影响集群）：{getattr(exc, "message", exc)}。'
                f'可把平台上的 {paths.kubeconfig_path(kubeasz_root, slug)} 复制到'
                f'{node_kubeconfig.KUBECONFIG_PATH} 手工补上',
            )
            continue
        if report['action'] == 'written':
            written.append(ip)
    if written:
        context.event(
            'node_kubeconfig_written',
            f'有 {len(written)} 台节点的 kubectl 配置是平台补写的（kubeasz 的 prepare 被跳过了）'
            f'：{"、".join(written)}。',
            level='warn',
            detail={'nodes': written, 'path': node_kubeconfig.KUBECONFIG_PATH},
        )
    context.complete_stage('node-kubeconfig')
    return written


def _configure_node_helm(context, kubeasz_root, compile_result, nodes, credentials_map):
    """把 Helm 客户端补到各节点的 ``bin_dir``。

    参数：`context` 表示任务上下文；`kubeasz_root` 表示 kubeasz 源码根目录（取其中的 ``bin/helm``）；
        `compile_result` 表示编译产物（取其中的 inventory 路径）；`nodes` 表示节点快照列表；
        `credentials_map` 表示连接信息。
    返回：返回没装成的节点 IP 列表。
    副作用：逐台建立 SSH 连接，缺 helm 时把控制机上那份传过去。

    时机与 ``_configure_node_path`` 相同（``01.prepare`` 之后）：kubeasz 的角色正是在这一步把
    apiserver、kubelet、kubectl 这些拷进节点的 ``bin_dir``，helm 只是它那份清单漏掉的一个，在这里
    补齐最自然 —— 而且 PATH 也是这一步刚指向那个目录的，运维登上去就能直接用。

    目录从**渲染后的 inventory** 里读（``bin_dir``），与 ``_configure_node_path`` 一致：操作员可以
    把 kubeasz 的 bin 目录改到别处，跟着走才能放进真正在 PATH 里的那个目录。

    **单台失败只记警告，不影响任务结果**（同 ``_configure_node_path``）：这是给人用的客户端，
    集群本身是好的。调用方按 ``install_helm_cli`` 决定要不要走这一步 —— 没勾「Helm」的集群
    不该多出这个命令，那样界面上的勾选又成了一句空话（只是反着来的）。
    """
    bin_dir_value = node_path.bin_dir_for(compile_result.get('hosts_path'))
    context.begin_stage('node-helm', '给节点补上 Helm 客户端')
    source = node_helm.local_binary(kubeasz_root)
    if source is None:
        context.log(
            'system',
            f'控制机上没有 {kubeasz_root}/bin/helm（载体镜像没解出来？），跳过节点侧的安装',
        )
        context.event(
            'node_helm_skipped',
            '控制机的 kubeasz bin 目录里没有 helm，本次没有给节点安装 Helm 客户端。',
            level='warn',
        )
        context.complete_stage('node-helm', title='给节点补上 Helm 客户端')
        return []
    failed = []
    installed = []
    version = ''
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            report = node_helm.install_node(context, node, bin_dir_value, source, credential)
        except (node_helm.NodeHelmError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            failed.append(ip)
            context.log(
                'system',
                f'节点 {ip} 的 helm 未装成功（不影响集群）：{getattr(exc, "message", exc)}。'
                f'可手工把控制机上的 {source} 复制成该节点的 {bin_dir_value}/helm。',
            )
            continue
        if report['action'] == 'installed':
            installed.append(ip)
        version = version or report.get('version') or ''
    if installed:
        context.event(
            'node_helm_installed',
            f'已给 {len(installed)} 台节点装上 Helm 客户端（{version or "版本未知"}）：'
            f'{"、".join(installed)}。',
            detail={'nodes': installed, 'bin_dir': bin_dir_value, 'version': version},
        )
    if failed:
        context.event(
            'node_helm_failed',
            f'有 {len(failed)} 台节点没能装上 Helm 客户端（集群本身不受影响）：'
            f'{"、".join(failed)}。可把这台机器从「Helm」勾选改为手工安装，或重跑本任务。',
            level='warn',
            detail={'nodes': failed, 'bin_dir': bin_dir_value},
        )
    context.complete_stage('node-helm', title='给节点补上 Helm 客户端')
    return failed


def _cleanup_node_containers(context, compile_result, nodes, credentials_map):
    """清掉各节点上已经退出的容器。

    参数：`context` 表示任务上下文；`compile_result` 表示编译产物（取其中的 inventory 路径）；
        `nodes` 表示节点快照列表；`credentials_map` 表示连接信息。
    返回：返回没清成的节点 IP 列表。
    副作用：逐台建立 SSH 连接，删除节点上处于 ``Exited`` 状态的容器（日志文件保留）。

    时机在 ``01``~``07`` 全部跑完之后：这时集群里该起的 Pod 已经起来，calico 的
    ``install-cni``、``mount-bpffs`` 这类 initContainer 也都退完了，删的是真正的残留，
    而不是正在跑的容器；再早（例如 ``06.network`` 之后就动手）晚退出的那几个就漏掉了。
    又必须赶在 ``begin_stage('register')`` 之前：``current_stage`` 记的是最后一次
    ``begin_stage``，任务成功时它该是「登记集群」，不该变成一个不在阶段条上的收尾动作。

    目录同样从渲染后的 inventory 里读（``bin_dir``），与 ``_configure_node_path`` 一致。

    **单台失败只记警告，不影响任务结果。** 这是打扫卫生，不是部署的一部分：为它把一次
    成功的部署报成失败，比节点上多留几个退出容器糟得多（详见 ``node_containers`` 模块）。
    """
    bin_dir_value = node_path.bin_dir_for(compile_result.get('hosts_path'))
    context.begin_stage('cleanup-containers', '清理节点上已退出的容器')
    failed = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            report = node_containers.cleanup_node(context, node, bin_dir_value, credential)
        except (node_registry.NodeRegistryError, credentials.CredentialError) as exc:
            failed.append(ip)
            context.log(
                'system',
                f'节点 {ip} 上已退出的容器未能清理（不影响集群）：{getattr(exc, "message", exc)}',
            )
            continue
        if report['error']:
            failed.append(ip)
            context.log(
                'system',
                f'节点 {ip} 上已退出的容器未能清理（不影响集群）：{report["error"]}。'
                f'可手工执行：{bin_dir_value}/crictl rm --keep-logs '
                f'$({bin_dir_value}/crictl ps -a --state exited -q)',
            )
        elif not report['found']:
            context.log('system', f'节点 {ip}：没有已退出的容器')
    if failed:
        context.event(
            'node_container_cleanup_failed',
            f'有 {len(failed)} 台节点没能清理已退出的容器，集群本身不受影响。',
            level='warn',
            detail={'nodes': failed, 'bin_dir': bin_dir_value},
        )
    context.complete_stage('cleanup-containers')
    return failed


def _restore_node_path(context, nodes, credentials_map, bin_dir_value):
    """逐台撤掉平台加进节点 PATH 的那两项配置。

    参数：`context` 表示任务上下文；`nodes` 表示节点列表；`credentials_map` 表示连接信息；
        `bin_dir_value` 表示当初加进去的目录。
    返回：返回处理失败的节点 IP 列表。
    副作用：逐台建立 SSH 连接，删文件并改 ``/etc/environment``。

    与 ``_uninstall_node_registries`` 同样的口吻：尽力而为，单台失败只记警告。
    ``bin_dir`` 里的命令不删（kubeasz 的清理剧本也不删它），所以日志里要说清这一点 ——
    否则操作员会以为「清理完了机器就是干净的」，实际上 ``/opt/kube/bin`` 还在。
    """
    failed = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            node_path.remove_node(context, node, bin_dir_value, credential)
        except (node_path.NodePathError, node_registry.NodeRegistryError,
                credentials.CredentialError) as exc:
            failed.append(ip)
            context.log('system', f'节点 {ip} 的 PATH 配置未能撤销（不影响重新部署）：{exc}')
    if failed:
        context.log(
            'system',
            f'注意：有 {len(failed)} 台节点仍留着 PATH 配置，请手工复核 {node_path.PROFILE_PATH} '
            f'与 {node_path.ENVIRONMENT_PATH}。',
        )
    return failed


def _cleanup_node_networks(context, nodes, credentials_map):
    """逐台删掉集群留在节点上的虚拟网卡。

    参数：`context` 表示任务上下文；`nodes` 表示节点列表；`credentials_map` 表示连接信息。
    返回：返回处理失败的节点 IP 列表。
    副作用：逐台建立 SSH 连接，删网卡并清 ipvs 转发表。

    kubeasz 的清理剧本只清组件与数据，剩下的虚拟网卡它自己在最后提示「请重启节点」。
    平台不把重启甩给操作员：``kube-ipvs0`` 上挂着集群 Service 网段的整段地址，
    留着它下一次部署的「网段合法性」预检就会拿 Service 网段跟它自己比而硬失败。

    与 ``_uninstall_node_registries`` 同样的口吻：单台失败只记警告。残留一张网卡不影响
    重新部署（预检已经把这类接口排除在判定之外），为它判失败会让操作员以为节点什么都没清掉。
    """
    failed = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            node_network.cleanup(context, node, credential)
        except Exception as exc:  # noqa: BLE001 - 清理是尽力而为，连不上也不该让整个任务失败
            failed.append(ip)
            context.log('system', f'清理节点 {ip} 上的虚拟网卡失败（不影响重新部署）：{exc}')
    return failed


def _uninstall_node_registries(context, nodes, credentials_map):
    """逐台卸载节点侧的镜像仓库。

    参数：`context` 表示任务上下文；`nodes` 表示节点列表；`credentials_map` 表示连接信息。
    返回：返回卸载失败的节点 IP 列表。
    副作用：逐台建立 SSH 连接，停服务、删文件、改动其 ``/etc/hosts``。

    **单台失败只记警告，不影响任务结果。** 集群级设施是平台自己装的，kubeasz 的清理剧本不认识它；
    但残留一个 registry 也不妨碍重新部署 —— 下次部署的 ``install`` 会把它带起来。
    为这个判失败会让操作员以为节点没清干净，实际清得很干净。
    """
    failed = []
    for node in nodes or []:
        ip = str(getattr(node, 'ip_address', '') or '')
        credential = (credentials_map or {}).get(ip) or {}
        try:
            node_registry.uninstall(context, node, credential)
        except Exception as exc:  # noqa: BLE001 - 清理是尽力而为，连不上也不该让整个任务失败
            failed.append(ip)
            context.log('system', f'卸载节点 {ip} 上的镜像仓库失败（不影响重新部署）：{exc}')
    return failed


def _mark_cluster_destroyed(cluster, slug, targets):
    """把集群记录改成「已销毁」并解绑它名下的机器。

    参数：`cluster` 表示 K8sCluster 实例；`slug` 表示清理前的集群标识；`targets` 表示本次清理的节点。
    返回：返回实际解绑的机器主键列表。
    副作用：写集群表与机器表，并为每台机器写审计日志。

    与机器解绑放在同一个事务里：集群置成「已销毁」而机器还挂着它，机器列表上就会显示成
    属于一个已经不存在的集群，而那批机器正是要拿去重新部署的。

    **标识必须清空。** 留着它，一旦用户删掉旧的部署任务记录（基础标识因此被释放），
    同名重装就会算出同一个标识，``register.ensure_deployed_cluster`` 按标识查库会捞到这条
    「已销毁」的记录并**原地复活**（状态写回已配置、配置版本从 N+1 接着记）。清空之后
    同名重装会得到一条干净的新记录，这条墓碑只留作审计。
    """
    from k8s_machine.views import release_cluster_machines

    with transaction.atomic():
        cluster.status = 'destroyed'
        cluster.slug = ''
        cluster.node_count = 0
        cluster.save(update_fields=['status', 'slug', 'node_count', 'updated_at'])
        released = release_cluster_machines(
            None, slug, cluster.name,
            organization=cluster.organization,
            reason=f'集群 {slug} 已被平台清理',
        )
    return released


def _assert_upgrade_target(context, current_version, target_version):
    """校验升级目标版本：不许原地不动、不许降级、不许跳次版本。

    参数：`context` 表示任务上下文；`current_version` 表示集群当前版本；`target_version` 表示目标版本。
    返回：无显式返回值。
    副作用：目标不合法时抛出 ``TaskAborted``；合法时写一条阶段日志。

    规则与提交期的 ``cluster_ops._validate_upgrade_target`` 保持一致，这里再判一次是给
    执行期兜底：任务可能排了很久才轮到自己，集群版本也可能被别的途径改过。升级范围是
    「同一次版本内的补丁版本，或**下一个**次版本」（1.35 → 1.36 可以，1.33 → 1.35 不行，
    Kubernetes 自己也不允许跳 minor）。

    **目标与当前相同时必须中止，不能再放过。** 以前这里只发一条 ``upgrade_noop`` 警告，
    于是「目标版本回落成当前版本」的任务会一路跑到 ``ezctl upgrade``，被 kubeasz 的
    ``93.upgrade.yml`` 用「running version is the same as the update version」挡下来 ——
    在有些路径上甚至什么都没做就报成功，任务中心写着「升级成功」而版本一点没变。
    这种「成功」比失败更难发现，所以在这里判死。
    """
    from k8s_deployment.services import upgrade_precheck

    current = upgrade_precheck.parse_version(current_version)
    target = upgrade_precheck.parse_version(target_version)
    if current is None or target is None:
        raise TaskAborted(
            'unknown_cluster_version',
            f'无法解析版本号（当前 {current_version}，目标 {target_version}），已中止升级。',
        )
    if target < current:
        raise TaskAborted(
            'upgrade_downgrade',
            f'平台不支持降级：集群当前 {current_version}，目标 {target_version} 比它更低。'
            '如果集群确实需要回到更低的版本，请从备份恢复。',
        )
    if target == current:
        raise TaskAborted(
            'upgrade_same_version',
            f'目标版本与集群当前版本相同（{current_version}），这次升级不会改变任何东西，已中止。'
            '请选择一个更高的目标版本：同一次版本内的补丁版本，或下一个次版本。',
        )
    current_key = current[0] * 1000 + current[1]
    target_key = target[0] * 1000 + target[1]
    if target_key - current_key > 1:
        raise TaskAborted(
            'upgrade_minor_jump',
            f'一次只能升一个次版本：当前 {current_version} 到目标 {target_version} 跨了 '
            f'{target_key - current_key} 个次版本（Kubernetes 不允许跳 minor），请先升级到中间版本。',
        )
    if target_key == current_key:
        context.log('system', f'升级版本校验通过：{current_version} → {target_version}（同次版本的补丁升级）')
    else:
        context.log(
            'system',
            f'升级版本校验通过：{current_version} → {target_version}'
            '（跨一个次版本，将改用目标版本对应的 kubeasz 制品）',
        )


def _upgrade_cluster_config(context, kubeasz_root, deployment, target_version):
    """把集群 config.yml / hosts 里本次升级要改的两个变量换掉。

    参数：`context` 表示任务上下文（只用于在改不动时记一条警告）；`kubeasz_root` 表示 kubeasz
        源码根目录；`deployment` 表示 K8sDeployment 实例；`target_version` 表示目标 Kubernetes 版本。
    返回：返回被改写后的 config.yml 路径。
    副作用：改写工作区里的集群变量文件。

    两个变量：

    - ``config.yml`` 的 ``K8S_VER``：升级目标版本，``93.upgrade.yml`` 拿它与自己 bin 里的
      版本比对（相同直接 ``UPDATE ABORT``）。
    - ``hosts`` 的 ``PROXY_MODE``：只在**用户显式改过模式**时才动。跨次版本升级会连着换
      kubeasz 制品，而 3.7.0 的模板不再支持 ipvs，老集群要么改 iptables/nftables 要么升不上去
      （口径见 ``preflight.PROXY_MODE_SUPPORT``）。改写是安全的：``roles/kube-node`` 里的
      kube-proxy 配置模板与重启都打了 ``upgrade_k8s`` 标签，``ezctl upgrade`` 会在每台节点上
      重新渲染 ``/var/lib/kube-proxy/kube-proxy-config.yaml`` 并重启 kube-proxy。
    """
    import re

    config_file = paths.config_path(kubeasz_root, deployment.cluster_slug)
    text = config_file.read_text(encoding='utf-8')
    pattern = re.compile(r'(?m)^(K8S_VER\s*:\s*).*$')
    if not pattern.search(text):
        raise TaskAborted('config_missing_k8s_ver', '集群 config.yml 中没有 K8S_VER 字段，无法执行升级。')
    updated = pattern.sub(lambda match: f'{match.group(1)}"{target_version}"', text, count=1)
    config_file.write_text(updated, encoding='utf-8')
    paths.secure_file(config_file)

    hosts_file = paths.hosts_path(kubeasz_root, deployment.cluster_slug)
    hosts_text = hosts_file.read_text(encoding='utf-8')
    mode = str(getattr(deployment, 'kube_proxy_mode', '') or '').strip().lower()
    current_mode = str(compiler.inventory_variable(hosts_text, 'PROXY_MODE') or '').strip().lower()
    if mode and current_mode and mode != current_mode:
        hosts_pattern = re.compile(r'(?m)^(PROXY_MODE\s*=\s*).*$')
        updated_hosts = hosts_pattern.sub(
            lambda match: f'{match.group(1)}"{mode}"', hosts_text, count=1,
        )
        hosts_file.write_text(updated_hosts, encoding='utf-8')
        paths.secure_file(hosts_file)
    elif mode and not current_mode:
        # hosts 里没有 PROXY_MODE 这一行：平台渲染的 hosts 一定带着它，所以这是被人工改过的
        # 现场。改不动就得让操作员看见 —— 任务会带着这个模式跑完，而节点上仍是旧的那套。
        # 升级本身不因此中止：节点上的 kube-proxy 配置由 kubeasz 按它自己的模板渲染，
        # 集群不会因为这一行缺失而升不动。
        context.event(
            'upgrade_proxy_mode_skipped',
            f'集群 hosts 里没有 PROXY_MODE 字段，无法把 kube-proxy 模式改成 {mode}，'
            '本次升级仍按节点现有的模式执行。',
            level='warn',
            detail={'kube_proxy_mode': mode},
            stage='upgrade',
        )
    return config_file


# ---- Worker 主流程 -----------------------------------------------------


def recover_interrupted_tasks():
    """把上次进程遗留的 running 任务标记为失败。

    参数：无。
    返回：返回被标记的任务数量。
    副作用：更新任务状态与失败原因，并为每个任务写入一条事件。

    Worker 崩溃或被强杀时，任务会停在 ``running``。这时远端集群状态未知，自动续跑
    很可能在错误的前提下继续操作，因此一律标成 ``worker_interrupted`` 交人工处理（规划 7.2）。
    """
    from k8s_deployment.models import K8sDeployment, K8sDeploymentEvent

    stale = list(K8sDeployment.objects.filter(status=STATUS_RUNNING))
    for item in stale:
        item.status = STATUS_FAILED
        item.error_code = 'worker_interrupted'
        reason = '部署 Worker 在上一次运行中中断，任务执行状态未知，请人工确认目标集群后再决定是否重试。'
        if item.cancel_requested:
            # 用户点过取消，Worker 随后死掉。**仍然按失败处理**：远端可能被改到一半，
            # 不能因为「用户本来就想停」就给他一个看起来干净的已取消状态。但必须告诉他
            # 取消请求收到了，否则他会以为取消没生效而反复点。
            reason = ('该任务在执行中收到过取消请求，随后部署 Worker 中断；'
                      '取消请求已收到，任务不会自动续跑。远端可能被改动到一半，请人工确认目标集群状态。')
        item.failure_reason = reason
        item.finished_at = timezone.now()
        item.current_stage = ''
        item.save(update_fields=['status', 'error_code', 'failure_reason', 'finished_at', 'current_stage', 'updated_at'])
        K8sDeploymentEvent.objects.create(
            organization=item.organization,
            deployment=item,
            stage='',
            level='error',
            event_type='worker_interrupted',
            message='部署 Worker 中断，任务被标记为失败，不会自动续跑。',
            detail={'cancel_requested': bool(item.cancel_requested)},
        )
    return len(stale)


def cleanup_expired_workspaces(ttl_seconds=None):
    """清理超过保留期且没有活动任务的工作区。

    参数：`ttl_seconds` 表示保留秒数，传 None 时读配置。
    返回：返回被删除的工作区数量。
    副作用：删除磁盘上的工作区目录，不可恢复。

    只清理已经进入终态且闲置超过 24 小时的工作区（规划 7.2），正在跑的任务目录
    与最近的失败现场一律保留，供人工排查。
    """
    from k8s_deployment.models import K8sDeployment, NON_TERMINAL_STATUSES

    if ttl_seconds is None:
        try:
            ttl_seconds = int(getattr(settings, 'K8S_DEPLOY_WORKSPACE_TTL_SECONDS', 86400))
        except (TypeError, ValueError):
            ttl_seconds = 86400

    active_ids = list(
        K8sDeployment.objects.filter(status__in=NON_TERMINAL_STATUSES).values_list('id', flat=True)
    )
    removed = 0
    for item in paths.expired_workspaces(ttl_seconds, active_ids=active_ids):
        paths.remove_tree(item)
        removed += 1
    return removed


def queue_snapshot(deployment):
    """返回任务在串行队列中的位置。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回 ``{'ahead': 前方任务数, 'total': 队列任务总数}``。
    副作用：只读数据库，不修改数据。

    规划 7.4 要求队列页显示「前方任务数量」。这里的前方指的是按 ``claim_next_task``
    的取牌顺序（``queued_at``、``id``）排在它之前、且尚未进入终态的任务——正在跑的那个
    也算一个，因为串行模型下它不结束就不会轮到我。任务本身已终止、或已经在跑时返回 0。
    """
    from k8s_deployment.models import K8sDeployment

    if deployment.status not in QUEUE_STATUSES:
        return {'ahead': 0, 'total': 0}

    peers = K8sDeployment.objects.filter(status__in=QUEUE_STATUSES)
    total = peers.count()
    if deployment.status == STATUS_RUNNING:
        return {'ahead': 0, 'total': total}

    others = peers.exclude(pk=deployment.pk)
    if deployment.queued_at is None:
        # 还没写排队时间（正常流程不会有），只能退化成「除我之外都在前面」。
        return {'ahead': others.count(), 'total': total}
    ahead = others.filter(
        Q(queued_at__isnull=True)
        | Q(queued_at__lt=deployment.queued_at)
        | Q(queued_at=deployment.queued_at, id__lt=deployment.pk)
    ).count()
    return {'ahead': ahead, 'total': total}


def claim_next_task(worker_id):
    """领取下一个待执行任务。

    参数：`worker_id` 表示当前 Worker 标识。
    返回：返回被领取的 K8sDeployment 实例；没有可领任务时返回 None。
    副作用：把任务的 ``status`` 改写为 ``running`` 并记录 Worker 与开始时间。

    领取动作放在一个事务里并先确认没有其它 ``running``，保证 SQLite 上也不会出现
    两个任务同时在跑（规划 7.2）。
    """
    from k8s_deployment.models import K8sDeployment

    with transaction.atomic():
        if K8sDeployment.objects.filter(status=STATUS_RUNNING).exists():
            # 有任务在跑就不再领取，避免并发推进两台机器。
            return None
        task = (
            K8sDeployment.objects.select_for_update()
            .filter(status=STATUS_QUEUED)
            .order_by('queued_at', 'id')
            .first()
        )
        if task is None:
            return None
        task.status = STATUS_RUNNING
        task.worker_id = worker_id
        task.started_at = timezone.now()
        task.heartbeat_at = timezone.now()
        task.current_stage = ''
        task.save(update_fields=['status', 'worker_id', 'started_at', 'heartbeat_at', 'current_stage', 'updated_at'])
    return task


def _load_nodes(deployment):
    """读取任务的节点快照。

    参数：`deployment` 表示 K8sDeployment 实例。
    返回：返回节点列表，按主键排序。
    副作用：执行一次数据库查询。
    """
    return list(deployment.nodes.select_related('machine').order_by('id'))


def _finish(deployment, status, error_code='', message='', summary=None, context=None):
    """把任务落到终态并做收尾。

    参数：`deployment` 表示 K8sDeployment 实例；`status` 表示终态；`error_code` 表示错误码；
        `message` 表示失败原因；`summary` 表示结果摘要；`context` 表示任务上下文，可为空。
    返回：返回更新后的任务。
    副作用：更新任务字段、刷新日志缓冲、生成诊断包并更新节点状态。
    """
    deployment.status = status
    deployment.error_code = str(error_code or '')[:64]
    deployment.failure_reason = diagnostics.redact_text(message)
    deployment.finished_at = timezone.now()
    if summary:
        deployment.result_summary = diagnostics.redact_structure(summary)
    deployment.save(update_fields=[
        'status', 'error_code', 'failure_reason', 'finished_at', 'result_summary', 'updated_at',
    ])
    if context is not None:
        context.cleanup()
        if status in (STATUS_FAILED, STATUS_ROLLBACK_REQUIRED):
            # 失败现场必须留下来：诊断包是人工排查的主要依据（规划 7.4）。
            package = register.capture_diagnostics(deployment)
            if package:
                context.event('diagnostics_ready', f'诊断包已生成：{package}', stage='')
    return deployment


def execute_task(deployment, worker_id='', fake_behaviors=None, runner_instance=None):
    """执行一个已经处于 running 状态的任务。

    参数：`deployment` 表示 K8sDeployment 实例；`worker_id` 表示 Worker 标识；
        `fake_behaviors` 表示模拟执行脚本；`runner_instance` 表示外部注入的执行器（测试用）。
    返回：返回更新后的任务。
    副作用：完整执行一次部署/运维任务，写日志、事件、集群记录与诊断包。
    """
    from k8s_deployment.models import K8sDeployment

    workspace = paths.ensure_workspace(deployment.pk)
    context = TaskContext(deployment, workspace, worker_id=worker_id)
    context.event('task_started', f'任务开始执行（{deployment.get_task_type_display()}）')
    context.log('system', f'执行模式：{deployment.get_execution_mode_display()}，工作区：{workspace}')
    context.heartbeat()

    active_runner = runner_instance or runner.select_runner(deployment, fake_behaviors=fake_behaviors)
    context.log('system', f'执行器：{active_runner.name}')

    nodes = _load_nodes(deployment)
    credentials_map = node_credentials(nodes)
    kubeasz_root = None
    try:
        # 备份任务不带自己的节点快照：它作用在集群现有节点上，节点清单从集群配置版本里取。
        if not nodes and deployment.task_type != 'backup':
            raise TaskAborted('no_target_nodes', '任务没有目标节点，无法执行。')
        # 先做本地可判定的校验，再去 OSS 下载：明显的非法请求不该先消耗一次制品下载。
        if deployment.task_type not in FULL_INSTALL_TASKS:
            _guard_deployed_cluster(context, deployment)
        _check_revision(context, deployment)
        if deployment.task_type in FULL_INSTALL_TASKS:
            kubeasz_root = _download_release(context, deployment, active_runner)
            _prepare_credentials(context, nodes, credentials_map)
            summary = run_full_install(
                context, deployment, nodes, credentials_map, kubeasz_root, active_runner,
            )
        elif deployment.task_type == 'add_master':
            kubeasz_root = _download_release(context, deployment, active_runner)
            summary = run_add_node(
                context, deployment, nodes, credentials_map, kubeasz_root, active_runner, role='master',
            )
        elif deployment.task_type == 'add_worker':
            kubeasz_root = _download_release(context, deployment, active_runner)
            summary = run_add_node(
                context, deployment, nodes, credentials_map, kubeasz_root, active_runner, role='node',
            )
        elif deployment.task_type == 'upgrade':
            kubeasz_root = _download_release(context, deployment, active_runner)
            summary = run_upgrade(
                context, deployment, nodes, credentials_map, kubeasz_root, active_runner,
            )
        elif deployment.task_type == 'destroy':
            # 清理不准备载体二进制：它只用 kubeasz 的源码剧本，没必要为此拉镜像、装本机运行环境。
            kubeasz_root = _download_release(context, deployment, active_runner, with_home=False)
            summary = run_destroy(
                context, deployment, nodes, credentials_map, kubeasz_root, active_runner,
            )
        elif deployment.task_type == 'backup':
            from k8s_deployment.services import backup as backup_service

            kubeasz_root = _download_release(context, deployment, active_runner)
            # 备份不走配置版本守卫：它读的是集群当前生效的配置，而不是要改它，
            # 因为别的原因让 revision 前进不该阻止运维人员先把现场存下来。
            record = backup_service.pending_backup_for(deployment)
            result = backup_service.create_backup(
                deployment.cluster,
                deployment=deployment,
                context=context,
                kubeasz_root=kubeasz_root,
                runner_instance=active_runner,
                record=record,
            )
            summary = {
                'backup_id': result.backup_id,
                'archive_size': result.archive_size,
                'archive_sha256': result.archive_sha256,
                'object_prefix': result.object_prefix,
            }
        elif deployment.task_type == 'restore':
            from k8s_deployment.services import backup as backup_service

            kubeasz_root = _download_release(context, deployment, active_runner)
            summary = backup_service.run_restore(
                context, deployment, nodes, credentials_map, kubeasz_root, active_runner,
            )
        else:
            raise TaskAborted('unsupported_task_type', f'暂不支持的任务类型：{deployment.task_type}')

        for item in nodes:
            item.status = 'ready'
            item.failure_reason = ''
            item.save(update_fields=['status', 'failure_reason', 'updated_at'])
        context.event('task_finished', '任务执行成功')
        return _finish(deployment, STATUS_SUCCEEDED, summary=summary, context=context)

    except TaskAborted as exc:
        if exc.error_code == 'cancelled':
            context.event('task_cancelled', exc.message, level='warn')
            return _finish(deployment, STATUS_CANCELLED, 'cancelled', exc.message, context=context)
        status = STATUS_ROLLBACK_REQUIRED if (
            deployment.task_type in ROLLBACK_ON_FAILURE_TASKS
            and exc.error_code in (
                'stage_failed', 'stage_timeout', 'upgrade_failed',
                'pre_upgrade_backup_failed', 'restore_failed',
                # 升级/恢复的执行本身成功了，但收尾的健康检查没通过：集群此刻处在一个
                # 平台说不上是好的状态，必须由人决定是否回滚（规划 10.4、11.2）。
                'health_check_failed', 'upgrade_nodes_not_ready',
            )
        ) else STATUS_FAILED
        context.event('task_failed', exc.message, level='error', detail={'error_code': exc.error_code})
        _mark_nodes_failed(nodes, exc.message)
        return _finish(deployment, status, exc.error_code, exc.message, context=context)
    except Exception as exc:
        # 未预期的异常同样要让任务落到终态，否则会一直挂在 running（规划 7.2）。
        message = f'任务执行过程中出现未预期的错误：{exc}'
        context.event('task_failed', message, level='error', detail={'error_code': 'unexpected_error'})
        _mark_nodes_failed(nodes, message)
        return _finish(deployment, STATUS_FAILED, 'unexpected_error', message, context=context)
    finally:
        # 转发通道必须先拆：平台机上临时挂着的集群地址不摘掉，这台机器就会一直把集群地址
        # 当成自己的，之后对它的任何访问都会被悄悄改道。成功、失败、取消都要走到这里。
        platform_relay.teardown()
        context.cleanup()
        if deployment.pk:
            K8sDeployment.objects.filter(pk=deployment.pk).update(heartbeat_at=timezone.now())


def _mark_nodes_failed(nodes, message):
    """把未完成的节点标记为失败。

    参数：`nodes` 表示节点列表；`message` 表示失败原因。
    返回：无显式返回值。
    副作用：更新节点状态字段。
    """
    for item in nodes:
        if item.status == 'ready':
            continue
        item.status = 'failed'
        item.failure_reason = str(message)[:240]
        item.save(update_fields=['status', 'failure_reason', 'updated_at'])


def _cleanup_relay_leftovers():
    """清理转发通道在平台机上留下的痕迹。

    参数：无。
    返回：无显式返回值。
    副作用：可能修改本机网络配置。

    通道本身活在进程里，进程退出就没了；会留下来的只有挂在回环接口上的临时地址（Worker 被
    kill -9 时 ``execute_task`` 的 ``finally`` 跑不到）。清理失败不能影响 Worker 干活 ——
    那最多是下一次任务重建通道时多一次判断。
    """
    try:
        platform_relay.cleanup_process_leftovers()
    except Exception:  # noqa: BLE001 - 清理失败不构成任务失败
        pass


def run_once(worker_id=''):
    """领取并执行一个任务，供常驻 Worker 循环与一次性开发模式共用。

    参数：`worker_id` 表示 Worker 标识。
    返回：有任务被执行时返回该任务，没有可领任务时返回 None。
    副作用：执行一次完整任务；领任务前先清理上一次进程留在平台机上的临时地址。
    """
    _cleanup_relay_leftovers()
    task = claim_next_task(worker_id)
    if task is None:
        return None
    try:
        return execute_task(task, worker_id=worker_id)
    except Exception as exc:
        # claim 之后任何异常都要把任务放回终态，不能让它永远停在 running。
        _finish(task, STATUS_FAILED, 'worker_error', f'Worker 执行任务时发生异常：{exc}')


def worker_id():
    """返回当前 Worker 的标识文本。

    参数：无。
    返回：返回形如 ``hostname-pid`` 的标识。
    副作用：读取主机名与进程号，不修改持久化数据。
    """
    import socket

    return f'{socket.gethostname()}-{os.getpid()}'


def spawn_worker(once=True):
    """在当前进程之外拉起一个部署 Worker。

    参数：`once` 表示是否只执行一个任务后退出。
    返回：返回子进程对象；未开启自动拉起或启动失败时返回 None。
    副作用：启动一个脱离当前进程的子进程。

    仅用于单机开发：提交任务后立刻有个 Worker 来跑它。生产环境由 systemd 常驻一个
    Worker，这里拉起的进程会因为抢不到文件锁而立即退出，属于设计内行为（规划 7.2）。
    """
    if not getattr(settings, 'K8S_DEPLOY_AUTOSTART_WORKER', False):
        return None
    command = [sys.executable, 'manage.py', 'run_deploy_worker']
    if once:
        command.append('--once')
    try:
        return subprocess.Popen(
            command,
            cwd=str(Path(settings.BASE_DIR)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name != 'nt'),
        )
    except (OSError, ValueError):
        # 拉不起来不影响接口已经受理的任务：下次常驻 Worker 启动时仍会执行它。
        return None
