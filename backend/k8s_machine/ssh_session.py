"""机器 SSH 交互式会话的支撑件：命令识别、尺寸收敛、会话记录与审计。

交互式终端本身是一条 WebSocket（``k8s_machine.consumers.MachineSshConsumer``），
**一条连接一个会话对象**，会话就挂在消费者实例上 —— 不再需要进程级的会话注册表，
也就没有「多 worker 时会话落到别的进程」这种前提。本模块只放与传输无关的那部分：

- 把键盘输入翻译成「看起来提交了哪些命令」（审计用）；
- 把浏览器给的尺寸收敛到合法区间（外部输入，直接送给 pty 会出事）；
- 会话记录的建立、失败留痕与收尾。

**线程与数据库的分工**：这里的函数都是同步的，由消费者用 ``database_sync_to_async``
扔到线程里执行；paramiko 的读写也在 ``asyncio.to_thread`` 里，事件循环不碰阻塞 IO。
"""

from datetime import timedelta

from django.utils import timezone

from ops.permissions import user_organization
from platform_logs.models import AuditLog

from .models import MachineSshSession

# 交互式会话申请的终端类型。xterm-256color 是 xterm.js 那一侧的常用值，远端据此决定
# 提示符与颜色转义，比裸的 ``xterm`` 更像本机登录。
TERM = 'xterm-256color'

# 浏览器终端的默认与合法尺寸。尺寸来自浏览器，属于外部输入，必须收敛后再送给远端
# （一个负数列数会让 resize_pty 抛异常）。
DEFAULT_COLS = 120
DEFAULT_ROWS = 36
MIN_COLS, MAX_COLS = 20, 500
MIN_ROWS, MAX_ROWS = 5, 200

# 读线程每次读取的上限，以及读超时。超时设成 0.2 秒是让读循环既能及时把输出搬走，
# 又能隔一会儿回头看一眼「WebSocket 是不是已经断了」—— 阻塞读会让断开延迟到下一次有输出。
READ_CHUNK = 4096
READ_TIMEOUT = 0.2

# 输出预览上限（字符）。与 ``K8sExecRecord.output_preview`` 同一口径：交互式输出可以无限长，
# 全量落库既没有查询入口，也会把密码之类的东西长期留在库里。
OUTPUT_PREVIEW_LIMIT = 2000

# 单条命令在审计里保留的长度。超长的粘贴会带出一整屏乱码，截断比原样落库有用。
COMMAND_TEXT_LIMIT = 500

# 孤儿会话的判定：平台进程被 kill 掉时，正在进行的会话记录会一直停在 active。
# 下一次开终端时顺手把超过这个时长的 active 记录收掉（惰性清理，不额外起定时线程）。
STALE_SESSION_SECONDS = 8 * 3600


def normalize_size(cols, rows):
    """把浏览器给的终端尺寸收敛到合法区间。

    参数：`cols` / `rows` 表示浏览器上报的列数与行数（可能是字符串、None 或离谱的值）。
    返回：返回 ``(cols, rows)`` 整数对。
    副作用：只做纯计算。
    """
    def clamp(value, low, high, fallback):
        """把一个外部值收敛到区间内，非法值退回默认值。"""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(low, min(high, number))

    return (
        clamp(cols, MIN_COLS, MAX_COLS, DEFAULT_COLS),
        clamp(rows, MIN_ROWS, MAX_ROWS, DEFAULT_ROWS),
    )


class CommandTracker:
    """把终端输入流翻译成「看起来提交了哪些命令」，供审计记录。

    规则与「进入容器」那条一致（``k8s_platform.consumers.K8sPodExecConsumer._audit_terminal_input``）：
    可打印 ASCII 累积成命令，回车提交，退格删一个字符，Ctrl-C / Ctrl-U 清空当前行。
    **多了一条**：方向键之类的转义序列整段丢弃 —— 交互式 shell 里「↑ + 回车」重跑上一条命令
    太常见了，不认转义序列的话审计里会记下一堆 ``[A``、``[B`` 这种噪音，而真正的那条命令本来
    就看不见（它藏在 shell 的历史缓冲里），多记一条垃圾只会让人以为用户敲过这个。

    这不是逐字节录像，也做不到：用户可能先按上翻、再改两个字，平台看到的只有按键本身。
    它的用途是「这台机器上大致被敲过什么」，不是命令级取证。
    """

    def __init__(self):
        """初始化空的字符缓冲与转义序列状态。"""
        self.chars = []
        # '' 正常 / 'pending' 刚读到 ESC / 'csi' 正在吞一段 CSI 或 SS3 序列。
        self.escape = ''

    def feed(self, text):
        """吃进一段输入，返回其中已经提交的命令。

        参数：`text` 表示本次输入文本。
        返回：返回本轮提交的命令列表（已去首尾空白，空命令不计）。
        副作用：修改内部缓冲。
        """
        commands = []
        for char in text:
            if self.escape == 'pending':
                # ESC 后面是 '[' 或 'O' 就进入一段序列，否则只是一个孤立的 ESC。
                self.escape = 'csi' if char in {'[', 'O'} else ''
                continue
            if self.escape == 'csi':
                # CSI / SS3 序列以 0x40–0x7E 的字节收尾（'[A' 的方向键、'[3~' 的 Delete 都算）。
                if char.isascii() and '@' <= char <= '~':
                    self.escape = ''
                continue
            if char == '\x1b':
                self.escape = 'pending'
            elif char in {'\r', '\n'}:
                command = self.text().strip()
                self.chars = []
                if command:
                    commands.append(command)
            elif char in {'\x08', '\x7f'}:
                if self.chars:
                    self.chars.pop()
            elif char in {'\x03', '\x15'}:
                self.chars = []
            elif char.isascii() and char.isprintable():
                self.chars.append(char)
        return commands

    def text(self):
        """返回当前累积的字符（还没提交的那一行）。"""
        return ''.join(self.chars)


def resource_label(machine):
    """返回审计日志里用的资源标识：``名字(IP)``。"""
    return f'{getattr(machine, "name", "") or ""}({getattr(machine, "ip_address", "") or ""})'


def write_audit(organization, actor, resource, client_ip, action, detail):
    """写一条机器 SSH 审计日志。

    参数：`organization` 表示所属组织；`actor` 表示平台用户名；`resource` 表示「主机名(IP)」；
        `client_ip` 表示发起方地址；`action` 表示动作名（不含 ``Machine.ssh.`` 前缀）；
        `detail` 表示详情字典。
    返回：无显式返回值。
    副作用：向审计日志表写入一条记录。

    动作名统一加 ``Machine.ssh.`` 前缀，与同模块其它动作（``Machine.test``、``Machine.assign``）
    在系统日志页里成一组。**详情里不放密码、不放密钥、不放开终端原始输出**。

    ``client_ip`` 为空时落回环地址：审计表的这一列是非空 IP 字段，存一个空串会留下一行
    谁也序列化不了的数据。
    """
    AuditLog.objects.create(
        organization=organization,
        actor=actor or 'system',
        action=f'Machine.ssh.{action}',
        resource=resource,
        ip_address=client_ip or '127.0.0.1',
        detail=detail,
    )


def create_record(user, machine, target, client_ip, organization=None):
    """为一条新会话写入 ``active`` 记录，并顺手收掉同一用户留下的孤儿记录。

    参数：`user` 表示发起会话的平台用户；`machine` 表示目标机器；`target` 表示解析后的目标
        （``{'host','port','user'}``）；`client_ip` 表示发起方地址；`organization` 表示所属组织，
        省略时按用户推断。
    返回：返回新建的 ``MachineSshSession``。
    副作用：可能把同一用户超时的 ``active`` 记录改写成 ``closed``，并写入一条新记录。

    孤儿记录来自平台进程被 kill：WebSocket 断开时收尾代码没机会跑，记录就停在 ``active``。
    惰性清理只扫当前用户、且只扫超过 ``STALE_SESSION_SECONDS`` 的，不会误伤正在用的终端。
    """
    organization = organization or user_organization(user)
    actor = getattr(user, 'username', '') or 'system'
    stale_before = timezone.now() - timedelta(seconds=STALE_SESSION_SECONDS)
    MachineSshSession.objects.filter(
        actor=actor, status='active', created_at__lt=stale_before,
    ).update(status='closed', ended_at=timezone.now(), failure_reason='平台进程重启后遗留的会话')
    return MachineSshSession.objects.create(
        organization=organization,
        machine=machine,
        machine_name=getattr(machine, 'name', '') or '',
        ip_address=getattr(machine, 'ip_address', '') or '',
        ssh_user=target['user'],
        actor=actor,
        client_ip=client_ip or '',
        status='active',
    )


def fail_record(user, machine, client_ip, message):
    """把一次「连不起来」的会话也记下来。

    参数：`user` 表示发起用户；`machine` 表示目标机器；`client_ip` 表示发起方地址；
        `message` 表示失败原因（中文）。
    返回：无显式返回值。
    副作用：写入一条 ``failed`` 状态的会话记录与一条审计日志。

    连不上也要留痕：连着试十几次都失败，本身就是需要被看见的事。
    """
    organization = user_organization(user)
    actor = getattr(user, 'username', '') or 'system'
    reason = (message or '')[:240]
    MachineSshSession.objects.create(
        organization=organization,
        machine=machine,
        machine_name=getattr(machine, 'name', '') or '',
        ip_address=getattr(machine, 'ip_address', '') or '',
        ssh_user=getattr(machine, 'ssh_user', '') or '',
        actor=actor,
        client_ip=client_ip or '',
        status='failed',
        ended_at=timezone.now(),
        failure_reason=reason,
    )
    write_audit(
        organization, actor, resource_label(machine), client_ip, 'open',
        {'machine_id': getattr(machine, 'pk', None), 'success': False, 'error': reason},
    )


def finalize_record(record_id, *, command_count, output_preview, status='closed', reason=''):
    """把会话的最终状态回写到记录表。

    参数：`record_id` 表示会话记录主键；`command_count` 表示识别到的命令条数；
        `output_preview` 表示输出预览；`status` 表示最终状态（``closed`` / ``failed``）；
        `reason` 表示关闭或失败原因。
    返回：返回本次是否真的改动了记录（重复收尾返回 False）。
    副作用：更新会话记录。

    幂等：WebSocket 断开、远端 EOF、用户点关闭可能先后到达，只有把记录从 ``active`` 改走的
    那一次才算数 —— 返回值让调用方决定要不要跟着写一条关闭审计。
    """
    now = timezone.now()
    updated = MachineSshSession.objects.filter(pk=record_id, status='active').update(
        status=status,
        ended_at=now,
        command_count=command_count,
        output_preview=(output_preview or '')[:OUTPUT_PREVIEW_LIMIT],
        failure_reason=(reason or '')[:240] if status == 'failed' else '',
        updated_at=now,
    )
    return bool(updated)


def audit_command(organization, actor, resource, client_ip, machine_id, session_id, command):
    """为一条识别到的命令写审计日志。

    参数：`organization` / `actor` / `resource` / `client_ip` 同 ``write_audit``；
        `machine_id` / `session_id` 表示归属；`command` 表示命令原文。
    返回：无显式返回值。
    副作用：向审计日志表写入一条记录。
    """
    write_audit(organization, actor, resource, client_ip, 'command', {
        'machine_id': machine_id,
        'session_id': session_id,
        'command': (command or '')[:COMMAND_TEXT_LIMIT],
    })


def audit_close(session, status, reason, duration_seconds):
    """写一条会话关闭审计。

    参数：`session` 表示消费者持有的会话状态字典（含 organization / actor / resource /
        client_ip / machine_id / session_id）；`status` 表示最终状态；`reason` 表示关闭原因；
        `duration_seconds` 表示会话存活秒数。
    返回：无显式返回值。
    副作用：向审计日志表写入一条记录。
    """
    write_audit(
        session['organization'], session['actor'], session['resource'], session['client_ip'], 'close',
        {
            'machine_id': session['machine_id'],
            'session_id': session['session_id'],
            'status': status,
            'reason': reason or '',
            'command_count': session['command_count'],
            'duration_seconds': duration_seconds,
        },
    )
