"""机器 SSH 交互式终端：一条 WebSocket 顶掉「下行 SSE + 上行 POST」那套拼装。

**为什么改成 WebSocket**：交互式终端是双向的实时流，拆成 SSE 下行 + 每个按键一个 POST
上行之后，按键要等「HTTP 往返 + DRF 权限解析 + 一次数据库写」才到 pty，回显肉眼可见地慢；
反过来 pty 的输出也要穿过 SSE 分帧。堡垒机那边用的是 WebSocket，一条长连接同时收发，
按键只走一次已建立的连接。平台的依赖里本来就有 ``channels``，ASGI 应用（``backend/asgi.py``）
也已经把 ``/ws/`` 那套路由挂上了，只差一个 ASGI 服务器（daphne）。

**会话就活在这个消费者实例上**：一条 WebSocket 一个 pty，不需要进程级的会话注册表，
也就不存在「多 worker 时会话落到别的进程」这个问题。WebSocket 断开就等于会话结束。

**线程与数据库的分工**：paramiko 的读写一律 ``asyncio.to_thread``（阻塞 IO 不能占着事件
循环），数据库一律 ``database_sync_to_async``（Django 的连接只在同步线程里安全）。
"""

import asyncio
import codecs
import json
import socket
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User

from k8s_deployment.services import machine_ssh
from k8s_deployment.services.machine_ssh import MODE_LABELS
from ops.permission_service import has_permission
from ops.permissions import user_organization
from ops.services.client_ip import scope_client_ip
from ops.services.tokens import decode_token
from ops.tenancy import scoped_queryset

from . import ssh_session
from .models import Machine

# 会话被服务端拒绝时用的关闭码（与 Pod exec 那条保持一致）：一个自定义区间，前端据此知道
# 「不是网络断了，是平台不让连」。
REJECT_CODE = 4400

# pty 读空时的轮询间隔（秒）。真通道靠 ``settimeout`` 抛超时，假通道（测试）返回 None，
# 两条路径都不能把事件循环空转起来。
IDLE_POLL_SECONDS = 0.03

# 同一条会话里 ``resize`` 审计的节流窗口（秒）：拖窗口一秒能发几十次尺寸。
RESIZE_AUDIT_INTERVAL_SECONDS = 5.0


class MachineSshConsumer(AsyncWebsocketConsumer):
    """把浏览器终端接到一台机器的 SSH shell 上，并全程留痕。

    连接地址：``/ws/v1/machines/<机器主键>/ssh?token=<访问令牌>&cols=&rows=``。
    下行用**文本帧**发终端输出，控制消息用**二进制帧**发一段 JSON（``{"type": "ready", ...}``）
    —— 两条通道分开，终端的输出里哪怕整段都是 ``{`` 开头也不会被当成控制消息。
    """

    async def connect(self):
        """校验令牌与权限、建立 SSH 会话并开始转发输出。

        参数：无。
        返回：无显式返回值。
        副作用：连接网络与数据库；失败时给出一句中文说明后关闭连接。
        """
        self.client = None
        self.channel = None
        self.reader_task = None
        self.tracker = ssh_session.CommandTracker()
        self.decoder = None
        self.record = None
        self.output_preview = []
        self.output_preview_size = 0
        self.command_count = 0
        self.closed_by_server = False
        self.accepted = False
        self.last_resize_audit_at = 0.0
        self.started_at = time.monotonic()

        try:
            self.user = await self._authenticate()
            self.machine = await self._machine()
            cols, rows = ssh_session.normalize_size(self._query_param('cols'), self._query_param('rows'))
            await self.accept()
            self.accepted = True
            await self._open_shell(cols, rows)
            self.reader_task = asyncio.create_task(self._read_from_target())
        except Exception as exc:  # noqa: BLE001 - 连不上、没权限、机器禁用都归到「说清楚就关掉」
            await self._reject(exc)

    async def disconnect(self, code):
        """收尾：停读、关 pty、回写会话记录。

        参数：`code` 表示 WebSocket 关闭码。
        返回：无显式返回值。
        副作用：关闭通道与 SSH 连接；回写会话记录并写关闭审计。
        """
        self.closed_by_server = True
        await self._stop_reader()
        await self._close_shell()
        if self.record is not None:
            await self._finalize(status='closed', reason='连接已断开')

    async def receive(self, text_data=None, bytes_data=None):
        """把浏览器敲进来的内容写进 pty，或处理 ``resize`` 控制消息。

        参数：`text_data` 表示文本帧（键盘输入）；`bytes_data` 表示二进制帧（控制消息）。
        返回：无显式返回值。
        副作用：向 pty 写入数据；识别到的命令写审计日志；``resize`` 会改变远端窗口尺寸。

        输入**原样转发，一个字节都不改**：回车、Ctrl-C、方向键的转义序列都由远端 pty 解释，
        前端（这里）做任何「翻译」都会让全屏程序（vi、top）行为异常。
        """
        if not self.channel or self.closed_by_server:
            return
        if text_data and text_data.startswith('{') and await self._handle_control_message(text_data):
            return

        if bytes_data is not None:
            data = bytes_data
        else:
            data = (text_data or '').encode('utf-8')
        if not data:
            return
        await self._audit_input(data)
        try:
            await asyncio.to_thread(self.channel.sendall, data)
        except Exception as exc:  # noqa: BLE001 - 通道坏了的话读线程那边会给结论
            await self._send_notice(f'输入未能送达：{exc}', '31')

    # ---- 连接建立 ------------------------------------------------------

    async def _authenticate(self):
        """按查询串里的访问令牌取出当前用户。

        参数：无。
        返回：返回已激活的用户实例。
        副作用：读取数据库。

        浏览器没法给 WebSocket 加请求头，令牌只能走查询串（与 Pod exec 那条一致）。
        """
        token = self._query_param('token')
        payload = decode_token(token, expected_type='access')
        return await database_sync_to_async(User.objects.select_related('profile').get)(
            pk=payload['sub'], is_active=True,
        )

    @database_sync_to_async
    def _machine(self):
        """取出目标机器，并校验权限、组织范围与启用状态。

        参数：无。
        返回：返回 ``Machine`` 实例。
        副作用：读取数据库。

        权限码是 ``page.machine.servers.ssh`` 而不是机器列表的 view：直连 shell 比看一眼列表
        重得多，必须在「用户管理 → 权限策略」里单独授权。跨组织的机器经 ``scoped_queryset``
        直接当作不存在。
        """
        if not has_permission(self.user, 'page.machine.servers.ssh'):
            raise PermissionError('没有 SSH 登录权限，请让管理员在「用户管理 → 权限策略」里授权。')
        machine = scoped_queryset(Machine.objects.all(), self.user).filter(
            pk=self.scope['url_route']['kwargs']['pk'],
        ).first()
        if not machine:
            raise ValueError('未找到可访问的机器，可能已被删除或不在你的组织范围内。')
        if machine.status == 'disabled':
            raise ValueError('该机器已禁用，不能建立 SSH 会话。')
        return machine

    async def _open_shell(self, cols, rows):
        """解析凭据、建立 SSH 连接并申请 pty。

        参数：`cols` / `rows` 表示浏览器终端的初始尺寸（已收敛）。
        返回：无显式返回值。
        副作用：发起网络连接；写入会话记录与审计日志；启动读线程。

        凭据一律经 ``machine_ssh`` 解析（**托管密码优先、平台部署密钥回落**），本模块不自己
        读密码字段。凭据缺失与连接失败都会留一条 ``failed`` 记录 —— 连着试十几次都失败，
        本身就是要被看见的事。
        """
        target = machine_ssh.resolve_target(self.machine)
        client_ip = self._client_ip()
        try:
            mode, password = machine_ssh.resolve_credentials(self.machine)
        except machine_ssh.SshTerminalError as exc:
            await database_sync_to_async(ssh_session.fail_record)(
                self.user, self.machine, client_ip, exc.message,
            )
            raise
        try:
            self.client = await asyncio.to_thread(
                machine_ssh.open_client,
                host=target['host'], port=target['port'], user=target['user'],
                password=password or None, timeout=machine_ssh.SESSION_TIMEOUT,
            )
            self.channel = await asyncio.to_thread(
                self.client.invoke_shell, term=ssh_session.TERM, width=cols, height=rows,
            )
            # 阻塞式读 + 超时：读线程在 ``to_thread`` 里等，事件循环不跟着一起等。
            await asyncio.to_thread(self.channel.settimeout, ssh_session.READ_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 登录失败的原因（超时、认证被拒、网络不可达）直接说给用户
            await self._close_shell()
            message = f'无法连接到 {target["host"]}:{target["port"]}：{exc}'
            await database_sync_to_async(ssh_session.fail_record)(
                self.user, self.machine, client_ip, message,
            )
            raise ValueError(message) from exc

        # 逐会话的 UTF-8 增量解码器：一个中文字符可能横跨两次 recv，攒住半个字符才不会
        # 在终端里吐出替换字符（``�``）。
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.record = await database_sync_to_async(ssh_session.create_record)(
            self.user, self.machine, target, client_ip,
        )
        await self._send_control({
            'type': 'ready',
            'mode': mode,
            'mode_label': MODE_LABELS.get(mode, mode),
            'host': target['host'],
            'port': target['port'],
            'user': target['user'],
            'cols': cols,
            'rows': rows,
        })
        await self._audit('open', {
            'machine_id': self.machine.pk,
            'session_id': self.session_id,
            'mode': mode,
            'mode_label': MODE_LABELS.get(mode, mode),
            'cols': cols,
            'rows': rows,
        })

    # ---- 输出转发 ------------------------------------------------------

    async def _read_from_target(self):
        """持续把 pty 的输出发给浏览器，直到远端结束或连接被关。

        参数：无。
        返回：无显式返回值。
        副作用：读取 pty、写输出预览；结束时关闭 WebSocket。

        远端 EOF（``recv`` 返回空字节）与通道异常都意味着这条 shell 没了，此时主动关掉
        WebSocket 并留下原因 —— 让浏览器那边的状态点停在「已连接」是最糟的结果。
        """
        reason = ''
        while not self.closed_by_server:
            try:
                chunk = await asyncio.to_thread(self.channel.recv, ssh_session.READ_CHUNK)
            except (socket.timeout, TimeoutError):
                continue
            except Exception as exc:  # noqa: BLE001 - 通道被关、网络断开都归到「会话结束」
                reason = f'SSH 会话中断：{exc}'
                break
            if chunk is None:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            if not chunk:
                reason = '远端已关闭 SSH 会话'
                break
            text = self.decoder.decode(chunk)
            if not text:
                continue
            self._append_preview(text)
            await self.send(text_data=text)
        if self.closed_by_server:
            return
        self.closed_by_server = True
        if reason:
            await self._send_notice(f'\r\n{reason}\r\n', '31')
        # 先收尾再关：浏览器收到关闭帧之后还会回一个断开，那一次收尾会被幂等判断挡掉。否则
        # 记录里留下的关闭原因永远是「连接已断开」，而真正的原因（远端退出、通道中断）就丢了。
        if self.record is not None:
            await self._finalize(status='closed', reason=reason or '远端已关闭 SSH 会话')
        await self.close(code=1000)

    def _append_preview(self, text):
        """把一段输出攒进输出预览（最多前 ``OUTPUT_PREVIEW_LIMIT`` 个字符）。"""
        if self.output_preview_size >= ssh_session.OUTPUT_PREVIEW_LIMIT:
            return
        self.output_preview.append(text)
        self.output_preview_size += len(text)

    # ---- 输入、控制消息与审计 -------------------------------------------

    async def _audit_input(self, data):
        """识别本次输入里提交的命令并写审计。

        参数：`data` 表示本次输入的字节。
        返回：无显式返回值。
        副作用：为识别到的每条命令写一条审计日志。

        识别规则在 ``ssh_session.CommandTracker``（与「进入容器」同一条口径）。
        """
        text = data.decode('utf-8', errors='ignore')
        for command in self.tracker.feed(text):
            self.command_count += 1
            await database_sync_to_async(ssh_session.audit_command)(
                self.record.organization, self.record.actor, ssh_session.resource_label(self.machine),
                self._client_ip(), self.machine.pk, self.session_id, command,
            )

    async def _handle_control_message(self, text_data):
        """处理浏览器发来的控制消息（目前只有 ``resize``）。

        参数：`text_data` 表示一段 JSON 文本。
        返回：返回这条消息是否已被当作控制消息处理。
        副作用：可能改变远端 pty 的窗口尺寸并写审计。

        以 ``{`` 开头不等于控制消息（粘贴一段 JSON 是合法输入），**必须能解析成带已知
        ``type`` 的 JSON** 才吞掉，否则原样当键盘输入发给 pty。
        """
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            return False
        if not isinstance(message, dict) or message.get('type') != 'resize':
            return False
        cols, rows = ssh_session.normalize_size(message.get('cols'), message.get('rows'))
        try:
            await asyncio.to_thread(self.channel.resize_pty, width=cols, height=rows)
        except Exception:  # noqa: BLE001 - 通道已断时忽略，读线程会给结论
            return True
        now = time.monotonic()
        if now - self.last_resize_audit_at >= RESIZE_AUDIT_INTERVAL_SECONDS:
            self.last_resize_audit_at = now
            await self._audit('resize', {
                'machine_id': self.machine.pk,
                'session_id': self.session_id,
                'cols': cols,
                'rows': rows,
            })
        return True

    # ---- 收尾与工具 ----------------------------------------------------

    async def _finalize(self, status, reason):
        """回写会话记录，并在真的改动到记录时补一条关闭审计。

        参数：`status` 表示最终状态；`reason` 表示原因。
        返回：无显式返回值。
        副作用：更新数据库。

        ``disconnect`` 与「远端先关了会话」可能先后触发，只有把记录从 ``active`` 改走的
        那一次写审计，日志里就不会出现两条「关闭」。
        """
        changed = await database_sync_to_async(ssh_session.finalize_record)(
            self.record.pk,
            command_count=self.command_count,
            output_preview=''.join(self.output_preview),
            status=status,
            reason=reason,
        )
        if not changed:
            return
        await database_sync_to_async(ssh_session.audit_close)(
            {
                'organization': self.record.organization,
                'actor': self.record.actor,
                'resource': ssh_session.resource_label(self.machine),
                'client_ip': self._client_ip(),
                'machine_id': self.machine.pk,
                'session_id': self.session_id,
                'command_count': self.command_count,
            },
            status, reason, int(time.monotonic() - self.started_at),
        )

    async def _reject(self, exc):
        """把「连不上」的原因说清楚，然后关掉连接。

        参数：`exc` 表示失败原因。
        返回：无显式返回值。
        副作用：可能接受连接、发一条中文说明并关闭；失败留痕由 ``_open_shell`` 负责。

        先 ``accept`` 再发消息：WebSocket 的握手一旦拒绝，浏览器只会看到一句
        「连接失败」，拿不到后端想说的那句话。
        """
        if not self.accepted:
            await self.accept()
            self.accepted = True
        self.closed_by_server = True
        await self._stop_reader()
        await self._close_shell()
        # 记录已经建起来之后才失败的（比如控制帧发不出去），不能留一条永远 active 的行。
        if self.record is not None:
            await self._finalize(status='failed', reason=str(exc))
        await self._send_notice(f'\r\n连接失败：{exc}\r\n', '31')
        await self.close(code=REJECT_CODE)

    async def _send_notice(self, message, color='33'):
        """用一段带颜色的文字提示用户（写进终端，不是日志）。

        参数：`message` 表示提示内容（自带换行）；`color` 表示 ANSI 颜色码。
        返回：无显式返回值。
        副作用：向浏览器发送一段终端输出。
        """
        if not self.accepted:
            return
        try:
            await self.send(text_data=f'\x1b[{color}m{message}\x1b[0m')
        except Exception:  # noqa: BLE001 - 连接已经没了，提示也就没人收
            pass

    async def _send_control(self, payload):
        """用二进制帧发一条控制消息（与终端输出分开，见类文档）。"""
        await self.send(bytes_data=json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    async def _stop_reader(self):
        """停掉读线程并等它真的退出（重复调用安全）。

        参数：无。
        返回：无显式返回值。
        副作用：取消读任务并等待它结束。

        「取消 + 等它结束」不能只做前半截：只取消不等待的话，这个任务会带着未处理的取消悬在
        事件循环里（进程退出时 Python 会抱怨「Task was destroyed but it is pending」），而且
        它可能正好卡在一次 ``send`` 上。读任务自身抛出的异常已经由它自己消化过（它只负责
        收尾与关闭），这里等它退出就好。
        """
        task = self.reader_task
        self.reader_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 读任务的异常不该挡住断开流程
            pass

    async def _close_shell(self):
        """关闭 pty 通道与 SSH 连接（重复调用安全）。"""
        channel, client = self.channel, self.client
        self.channel = None
        self.client = None
        for resource in (channel, client):
            if resource is None:
                continue
            try:
                await asyncio.to_thread(resource.close)
            except Exception:  # noqa: BLE001 - 关闭失败也要继续收尾
                pass

    async def _audit(self, action, detail):
        """写一条会话级审计（``Machine.ssh.<action>``）。"""
        await database_sync_to_async(ssh_session.write_audit)(
            user_organization(self.user), getattr(self.user, 'username', '') or 'system',
            ssh_session.resource_label(self.machine), self._client_ip(), action, detail,
        )

    @property
    def session_id(self):
        """返回会话标识（就是会话记录主键，前端不需要另造一个 id）。

        参数：无。
        返回：返回记录主键；记录还没建起来时返回 None。
        """
        return getattr(self.record, 'pk', None)

    def _client_ip(self):
        """返回发起方地址（经 ``X-Forwarded-For`` 解析，与 HTTP 那条路同一口径）。"""
        return scope_client_ip(self.scope)

    def _query_param(self, key):
        """读取查询串里的一个参数（浏览器给 WebSocket 带令牌只能靠它）。"""
        params = parse_qs(self.scope.get('query_string', b'').decode('utf-8'))
        return (params.get(key) or [''])[0]
