"""机器水位的数据库缓存：采集一次、到处读，别让每次翻页都去 SSH 一遍。

采集本身是慢的（每台一条 SSH 连接、CPU 还要隔 1 秒采两次），而机器列表是**看一眼**的动作。
早先的做法是「页面加载 → 逐台 SSH → 显示」，于是每翻一页、每刷新一次就把所有机器重新
连一遍，机器多一点就明显卡；更要命的是有一台关机的话，每次打开页面都要等它一次连接超时。

现在分两条路，都落在 ``MachineMetric`` 这张缓存表上：

- **读穿**（``cached_items``）：界面要数据时先看缓存。缓存比 ``CACHE_TTL_SECONDS`` 新就直接
  用，过期了才现场采一次再写回去。失败的结果**同样缓存** —— 一台关机的机器在上限之内只会
  被重连一次，而不是每次刷新都重连。
- **周期刷新**（``refresh_due``）：部署 Worker 的空转循环里顺带调用。它选的是「缓存比自己
  该有的新鲜度更旧」的机器，所以**3 分钟这个间隔是查出来的，不是靠定时器攒出来的** ——
  没有过期的机器时它只是一条空查询，多调几次也不会多采一次。

两个常数分工明确：``REFRESH_INTERVAL_SECONDS``（3 分钟）是后台多久采一轮，
``CACHE_TTL_SECONDS``（5 分钟）是「界面能接受多旧的数据」。前者更短，所以正常情况下
界面永远读到缓存；后台没在跑（没起 Worker）时，用户翻到第 5 分钟那次也会自己补上。

**只有这一个进程会写这张表**（Web 进程只在读穿时写自己错过的那几台），因此不需要额外的
锁：``update_or_create`` 本身就是原子的，最坏情况是两台机器被同时采两次。
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Machine, MachineMetric

# 缓存新鲜度上限（秒）：超过它，界面这一次请求会现场补采。
CACHE_TTL_SECONDS = 300
# 后台周期采集的间隔（秒）：缓存比这个更旧的机器才会被 Worker 选中。
REFRESH_INTERVAL_SECONDS = 180
# 一次最多采集几台。列表页每页 10 条，留出余量；上限的意义在于别让一次请求（或一轮后台
# 刷新）变成对几十上百台机器的 SSH 扫描 —— 那既是慢，也是在机器上同时铺开一堆会话。
MAX_COLLECT_MACHINES = 30


def _number(value):
    """把采集结果里的数值收敛成 ``float`` 或 ``None``。

    参数：`value` 表示采集器给出的值。
    返回：返回浮点数；空值或非数值一律返回 ``None``。
    副作用：只做纯计算。

    **不把空值变成 0**：0% 与「没采到」在界面上是两个完全相反的结论。
    """
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _store(items, sampled_at):
    """把一次采集的结果逐台写进缓存。

    参数：`items` 表示 ``host_usage.collect_machine_usage`` 返回的逐台结果；
        `sampled_at` 表示这批结果的采集时刻。
    返回：返回写入（新建或覆盖）的行数。
    副作用：写入或更新 ``MachineMetric`` 表。

    每个字段都用 ``.get`` 兜底：采集器是别的 app 的实现，这里只认自己要的那几个键，
    少一个键不该让整批写入失败 —— 缺的那一项存成空，界面显示「—」。
    """
    written = 0
    with transaction.atomic():
        for item in items or []:
            machine_id = item.get('machine_id')
            if not machine_id:
                continue
            MachineMetric.objects.update_or_create(
                machine_id=machine_id,
                defaults={
                    'status': str(item.get('status') or 'ok')[:16],
                    'error': str(item.get('error') or '')[:240],
                    'hostname': str(item.get('hostname') or '')[:160],
                    'cpu_percent': _number(item.get('cpu_percent')),
                    'memory_percent': _number(item.get('memory_percent')),
                    'disk_percent': _number(item.get('disk_percent')),
                    'sampled_at': sampled_at,
                },
            )
            written += 1
    return written


def collect_now(machines, sampled_at=None):
    """现场采集这几台机器并写入缓存。

    参数：`machines` 表示 Machine 实例列表；`sampled_at` 表示采集时刻，省略时取当前时间。
    返回：返回本次写入的行数。
    副作用：逐台 SSH 登录机器执行一条只读命令，并写入缓存表。

    采集器放在 ``k8s_deployment``（SSH 与节点通道都在那边），函数内懒 import：
    两个 app 互相引用时顶层 import 会绕成环。
    """
    machines = list(machines)
    if not machines:
        return 0
    from k8s_deployment.services import host_usage

    payload = host_usage.collect_machine_usage(machines)
    return _store(payload.get('items') or [], sampled_at or timezone.now())


def _is_fresh(row, now, ttl_seconds):
    """判断一行缓存是否还在有效期内。

    参数：`row` 表示缓存行（可为 None）；`now` 表示当前时间；`ttl_seconds` 表示有效期。
    返回：还在有效期内时返回真。
    副作用：只做纯计算。

    没有缓存行、或者缓存行的时间戳来自未来（改过系统时钟）都算「不新鲜」，宁可多采一次。
    """
    if row is None:
        return False
    age = (now - row.sampled_at).total_seconds()
    return 0 <= age < ttl_seconds


def cached_items(machines, ttl_seconds=CACHE_TTL_SECONDS):
    """读出这几台机器的水位，缓存过期或缺失的现场补采一次。

    参数：`machines` 表示 Machine 实例列表（顺序即返回顺序）；`ttl_seconds` 表示缓存有效期。
    返回：返回 ``(items, sampled_at)`` —— ``items`` 与入参同序，``sampled_at`` 是这批数据里
        **最旧**的采集时刻（界面显示「采集于 …」时，这个值才代表「数据至少新到这一刻」）。
    副作用：对缓存过期或缺失的机器发起一次 SSH 采集并写入缓存。

    失败的那几台不会重试到天亮：失败结果同样落库，下一次要等到它过期才会再连。
    """
    machines = list(machines)
    if not machines:
        return [], ''
    now = timezone.now()
    rows = {row.machine_id: row for row in MachineMetric.objects.filter(machine__in=machines)}
    due = [machine for machine in machines if not _is_fresh(rows.get(machine.pk), now, ttl_seconds)]
    if due:
        collect_now(due, sampled_at=now)
        rows = {row.machine_id: row for row in MachineMetric.objects.filter(machine__in=machines)}

    items = [rows[machine.pk].as_item() for machine in machines if machine.pk in rows]
    if not items:
        return [], ''
    oldest = min(rows[machine.pk].sampled_at for machine in machines if machine.pk in rows)
    return items, timezone.localtime(oldest).strftime('%Y-%m-%d %H:%M:%S')


def due_machines(interval_seconds=REFRESH_INTERVAL_SECONDS, limit=MAX_COLLECT_MACHINES):
    """挑出该重新采集的机器：没有缓存、或缓存比给定间隔更旧。

    参数：`interval_seconds` 表示后台采集间隔；`limit` 表示一次最多挑几台。
    返回：返回按「缓存最旧优先」排序的 Machine 查询集（已切片）。
    副作用：只读取数据库，不修改持久化数据。

    从没采过的机器排在最前（关联为空），其次是缓存最旧的 —— 机器多于一轮能采的量时，
    多轮下来每台都会被轮到，而不是前几台反复被采、后面的永远排不上。
    """
    cutoff = timezone.now() - timedelta(seconds=interval_seconds)
    return (
        Machine.objects
        .filter(Q(metric__isnull=True) | Q(metric__sampled_at__lt=cutoff))
        .order_by('metric__sampled_at', 'pk')[:limit]
    )


def refresh_due(interval_seconds=REFRESH_INTERVAL_SECONDS, limit=MAX_COLLECT_MACHINES):
    """按需刷新一批机器的水位缓存，供后台周期调用。

    参数：`interval_seconds` 表示采集间隔；`limit` 表示一次最多采几台。
    返回：返回本次实际采集的机器台数。
    副作用：对到期的机器发起 SSH 采集并写入缓存。

    间隔由 :func:`due_machines` 的查询条件保证，因此调用方可以放心地每轮循环都调一次：
    没有到期的机器时它只是一条返回空集的查询，不会重复采集。
    """
    machines = list(due_machines(interval_seconds=interval_seconds, limit=limit))
    if not machines:
        return 0
    collect_now(machines)
    return len(machines)
