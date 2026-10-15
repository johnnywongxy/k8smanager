"""部署 Worker：单进程、单任务串行执行 kubeasz 任务。

规划 7.2 定死了这套并发模型：**只允许一个 Worker、一次只跑一个任务**。原因不是偷懒，
而是 kubeasz 的 ``ezctl`` 会在每个任务自己的工作区里跑 ansible，而 ansible 的 inventory
与 playbook 都是进程级共享的文件；两个任务并发跑同一个集群，必然互相覆盖配置。

串行靠两条互补的机制：

1. **文件锁**（``WorkerLock``）：一个进程级独占锁。它挡住「误启动第二个 Worker」——
   第二个进程拿到锁失败后直接退出，而不是傻等着抢任务。
2. **数据库租约**（``claim_next_task``）：任务领取在同一事务里用 ``select_for_update``
   完成，并且只在没有任何 ``running`` 任务时才发牌。它挡住的是「锁泄漏后重复执行」。

启动时还会做一次恢复：上次进程被强杀时留下的 ``running`` 任务不会自己变成终态，
必须显式收尸，否则队列会被一个僵尸任务永久堵死（``recover_interrupted_tasks``）。

除了任务队列，这个进程还顺手负责一件与部署无关的小事：**按需刷新机器列表页的水位缓存**
（``_refresh_metrics``）。它是平台上唯一常驻的进程，让它顺带看一眼「哪些机器该重新采一次」
比为了这件事再单起一个服务省事得多；采集是只读的、按过期条件触发、且失败绝不影响任务。

用法::

    python manage.py run_deploy_worker            # 常驻，直到收到 SIGTERM/SIGINT
    python manage.py run_deploy_worker --once     # 跑完一个任务就退出（开发与测试用）
"""

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from k8s_deployment.services import backup, state_machine

# 空队列时的等待时长。任务本身是分钟级的，轮询间隔再短也没意义，只会白烧 CPU。
IDLE_SLEEP_SECONDS = 2.0
# 每轮循环之间清理一次过期工作区的间隔（秒），避免每个空转周期都去扫目录。
CLEANUP_INTERVAL_SECONDS = 300
# 机器水位缓存的刷新间隔（秒），0 表示这个 Worker 不负责采集。
# 与 ``k8s_machine.metrics.REFRESH_INTERVAL_SECONDS`` 是同一个量：这里显式写一遍，是为了让
# 「Worker 多久采一次」在 Worker 自己的代码里看得见，而不是埋在另一个 app 的常量里。
METRIC_REFRESH_SECONDS = 180


class Command(BaseCommand):
    """部署 Worker 的入口命令。"""

    help = '启动部署 Worker：串行消费 k8s_deployment 任务队列，一次只执行一个任务。'

    def add_arguments(self, parser):
        """声明命令行参数。

        参数：`parser` 表示 argparse 解析器。
        返回：无显式返回值。
        副作用：向解析器注册参数定义。
        """
        parser.add_argument(
            '--once',
            action='store_true',
            help='只领取并执行一个任务后退出；没有待执行任务时也立即退出。',
        )
        parser.add_argument(
            '--worker-id',
            default='',
            help='覆盖 Worker 标识，默认使用“主机名-进程号”。',
        )
        parser.add_argument(
            '--idle-exit',
            type=int,
            default=0,
            help='连续空转多少秒后自动退出，0 表示常驻不退出。用于开发时的临时 Worker。',
        )

    def handle(self, *args, **options):
        """执行 Worker 主循环。

        参数：`args` 与 `options` 来自命令行解析；使用 `once`、`worker_id`、`idle_exit`。
        返回：无显式返回值。
        副作用：加锁、收尸中断任务、循环领取并执行任务、清理过期工作区。
        """
        once = bool(options.get('once'))
        identifier = str(options.get('worker_id') or '') or state_machine.worker_id()
        idle_exit = int(options.get('idle_exit') or 0)

        lock = state_machine.WorkerLock()
        # 注意这里必须判返回值：``acquire()`` 抢不到锁时返回 False，只有上下文管理器
        # 形式才会抛 ``WorkerBusy``。早先这里写成 ``try: lock.acquire() except WorkerBusy``，
        # 结果是第二个 Worker 从不退出，规划 7.2「获取失败立即退出」形同虚设。
        if not lock.acquire():
            # 生产环境由 systemd 常驻一个 Worker；开发时提交任务会拉起一次性 Worker，
            # 此时它抢不到锁是预期结果，安静退出即可，不能算错误。
            holder = lock.holder()
            detail = f'，锁文件记录的持有者：{holder}' if holder else ''
            self.stdout.write(self.style.WARNING(
                f'已存在运行中的 Worker，本进程退出（锁文件 {lock.path}{detail}）。'
            ))
            return

        stopped = {'flag': False}

        def _request_stop(signum, _frame):
            """记录停止请求。

            参数：`signum` 表示信号编号；`_frame` 表示调用栈帧（未使用）。
            返回：无显式返回值。
            副作用：把停止标记置位，让主循环在下一个边界安全退出。
            """
            stopped['flag'] = True
            self.stdout.write(f'收到信号 {signum}，本次任务结束后退出。')

        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[signum] = signal.signal(signum, _request_stop)
            except (ValueError, OSError):  # pragma: no cover - 非主线程或平台不支持
                continue

        idle_since = time.monotonic()
        last_cleanup = 0.0
        try:
            self._recover(identifier)
            while not stopped['flag']:
                now = time.monotonic()
                if now - last_cleanup > CLEANUP_INTERVAL_SECONDS:
                    last_cleanup = now
                    self._cleanup()

                self._refresh_metrics()

                task = state_machine.run_once(worker_id=identifier)
                if task is None:
                    if once:
                        self.stdout.write('没有待执行的任务。')
                        return
                    if idle_exit and time.monotonic() - idle_since > idle_exit:
                        self.stdout.write('长时间无任务，Worker 退出。')
                        return
                    time.sleep(IDLE_SLEEP_SECONDS)
                    continue

                idle_since = time.monotonic()
                self.stdout.write(
                    f'任务 #{task.pk}（{task.get_task_type_display()}）执行结束，状态：'
                    f'{task.get_status_display()}'
                )
                if once:
                    return
        finally:
            for signum, handler in previous.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):  # pragma: no cover
                    continue
            lock.release()

    def _recover(self, identifier):
        """启动时收尸中断的任务并清理残留的备份临时文件。

        参数：`identifier` 表示 Worker 标识。
        返回：无显式返回值。
        副作用：把中断的 running 任务改为失败，并删除过期的备份临时目录。
        """
        try:
            recovered = int(state_machine.recover_interrupted_tasks() or 0)
        except Exception as exc:  # pragma: no cover - 恢复失败不应阻止 Worker 启动
            self.stderr.write(f'恢复中断任务失败：{exc}')
            recovered = 0
        if recovered:
            self.stdout.write(self.style.WARNING(
                f'已把 {recovered} 个中断的 running 任务标记为失败，等待人工确认目标集群后重试。'
            ))

        try:
            removed = backup.cleanup_stale_temp()
        except Exception as exc:  # pragma: no cover
            self.stderr.write(f'清理备份临时目录失败：{exc}')
            removed = 0
        if removed:
            self.stdout.write(f'清理了 {removed} 个残留的备份临时目录。')

        ttl = getattr(settings, 'K8S_DEPLOY_WORKSPACE_TTL_SECONDS', None)
        self.stdout.write(
            f'Worker {identifier} 已就绪，工作区保留时长：'
            f'{ttl if ttl else "不自动清理"}，时间：{timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")}'
        )

    def _refresh_metrics(self):
        """顺带刷新机器列表页的水位缓存。

        参数：无。
        返回：无显式返回值。
        副作用：对缓存过期的机器发起一次只读 SSH 采集并写入缓存表。

        放在部署 Worker 里是因为它是平台上**唯一常驻**的进程：机器水位要有「每 3 分钟采一次」
        就得有人一直醒着，而为了它再单起一个 systemd 服务，等于让平台多一个可能没被拉起来的
        部件。刷新条件（谁过期了）由 ``k8s_machine.metrics.due_machines`` 的查询决定 ——
        没有到期的机器时这里只是一条返回空集的查询，跑得再勤也不会多采一次。

        失败绝不影响部署任务：水位是附注，任务是正事，采集这一侧的异常只记一行日志。
        """
        if METRIC_REFRESH_SECONDS <= 0:
            return
        try:
            from k8s_machine import metrics

            metrics.refresh_due(interval_seconds=METRIC_REFRESH_SECONDS)
        except Exception as exc:  # pragma: no cover - 采集失败不应影响任务队列
            self.stderr.write(f'刷新机器水位缓存失败：{exc}')

    def _cleanup(self):
        """按需清理过期的工作区。

        参数：无。
        返回：无显式返回值。
        副作用：删除超过保留期且不属于活跃任务的工作区目录。
        """
        try:
            removed = int(state_machine.cleanup_expired_workspaces() or 0)
        except Exception as exc:  # pragma: no cover
            self.stderr.write(f'清理过期工作区失败：{exc}')
            return
        if removed:
            self.stdout.write(f'清理了 {removed} 个过期工作区。')
