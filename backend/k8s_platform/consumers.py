import asyncio
import json
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from django.utils import timezone
from kubernetes import client
from kubernetes.stream import stream

from k8s_audit.models import K8sOperationAudit
from k8s_cluster.models import K8sCluster
from k8s_cluster.services import select_preferred_cluster
from k8s_resource.services.client_factory import build_api_client
from k8s_resource.services.namespace_authz import assert_namespace_permission
from platform_logs.models import AuditLog
from ops.permission_service import has_permission
from ops.permissions import user_organization
from ops.services.tokens import decode_token
from ops.tenancy import scoped_queryset

from .models import K8sExecRecord


class K8sPodExecConsumer(AsyncWebsocketConsumer):
    """Bridge browser xterm.js to an interactive Kubernetes Pod exec session."""

    async def connect(self):
        self.socket = None
        self.reader_task = None
        self.command_buffer = []
        self.closed_by_server = False
        self.accepted = False
        self.output_preview = []
        self.output_preview_size = 0
        self.last_keepalive_at = 0.0

        try:
            self.user = await self._authenticate()
            await self._validate_permission()
            self.cluster = await self._cluster()
            self.namespace = self.scope['url_route']['kwargs']['namespace']
            self.pod = self.scope['url_route']['kwargs']['pod']
            self.container = self._query_param('container')
            await self.accept()
            self.accepted = True
            self.record = await self._create_exec_record()
            await self._audit('k8s.pod_exec.open', {'namespace': self.namespace, 'pod': self.pod, 'container': self.container})
            await self._open_exec_socket()
            self.reader_task = asyncio.create_task(self._read_from_pod())
        except Exception as exc:
            if not self.accepted:
                await self.accept()
                self.accepted = True
            await self.send(text_data=f'\r\nPod Exec 连接失败：{exc}\r\n')
            self.closed_by_server = True
            await self.close(code=4400)

    async def disconnect(self, code):
        self.closed_by_server = True
        if self.reader_task:
            self.reader_task.cancel()
        await self._close_exec_socket()
        if hasattr(self, 'record'):
            await self._finish_exec_record(code)
            await self._audit('k8s.pod_exec.close', {'namespace': self.namespace, 'pod': self.pod, 'container': self.container, 'code': code})

    async def receive(self, text_data=None, bytes_data=None):
        if not self.socket:
            return
        if text_data and text_data.startswith('{'):
            if await self._handle_control_message(text_data):
                return

        data = bytes_data if bytes_data is not None else (text_data or '').encode('utf-8')
        if data:
            await self._audit_terminal_input(data)
            await asyncio.to_thread(self.socket.write_stdin, data.decode('utf-8', errors='ignore'))

    async def _authenticate(self):
        token = self._query_param('token')
        payload = decode_token(token, expected_type='access')
        return await database_sync_to_async(User.objects.select_related('profile').get)(pk=payload['sub'], is_active=True)

    async def _validate_permission(self):
        allowed = await database_sync_to_async(has_permission)(self.user, 'page.k8s.pods.exec')
        if not allowed:
            raise PermissionError('没有 Pod 进入容器权限')

    @database_sync_to_async
    def _cluster(self):
        cluster_id = self._query_param('cluster')
        queryset = scoped_queryset(K8sCluster.objects.all(), self.user)
        cluster = queryset.filter(pk=cluster_id).first() if cluster_id else select_preferred_cluster(queryset)
        if not cluster:
            raise ValueError('未找到可访问的 K8s 集群')
        if cluster.status == 'disabled':
            raise ValueError('当前集群已禁用')
        return cluster

    async def _open_exec_socket(self):
        _config, api_client = await asyncio.to_thread(build_api_client, self.cluster)
        core = client.CoreV1Api(api_client)
        commands = [
            ['/bin/bash', '-i'],
            ['/bin/sh', '-i'],
            ['sh', '-i'],
        ]
        last_error = None
        for command in commands:
            candidate = None
            try:
                candidate = await asyncio.to_thread(
                    stream,
                    core.connect_get_namespaced_pod_exec,
                    self.pod,
                    self.namespace,
                    container=self.container or None,
                    command=command,
                    stderr=True,
                    stdin=True,
                    stdout=True,
                    tty=True,
                    _preload_content=False,
                )
                alive, probe_output = await asyncio.to_thread(self._probe_exec_socket, candidate)
                if not alive:
                    await asyncio.to_thread(candidate.close)
                    last_error = probe_output or f'{command[0]} 启动后立即退出'
                    continue
                self.socket = candidate
                await self.send(text_data=f'\r\n已进入 {self.namespace}/{self.pod}{f" / {self.container}" if self.container else ""}\r\n')
                if probe_output:
                    await self.send(text_data=probe_output)
                return
            except Exception as exc:
                last_error = exc
                if candidate:
                    try:
                        await asyncio.to_thread(candidate.close)
                    except Exception:
                        pass
        raise ValueError(f'容器内没有可用交互式 shell，已尝试 /bin/bash、/bin/sh、sh：{last_error}')

    def _probe_exec_socket(self, sock):
        deadline = time.monotonic() + 0.8
        output = []
        while time.monotonic() < deadline:
            sock.update(timeout=0.2)
            if sock.peek_stdout():
                output.append(sock.read_stdout())
            if sock.peek_stderr():
                output.append(sock.read_stderr())
            if not sock.is_open():
                return False, ''.join(output)
            time.sleep(0.05)
        return sock.is_open(), ''.join(output)

    async def _read_from_pod(self):
        while not self.closed_by_server and self.socket:
            try:
                data = await asyncio.to_thread(self._read_socket_once)
            except Exception as exc:
                await self.send(text_data=f'\r\nPod Exec 已断开：{exc}\r\n')
                break
            if data:
                await self._append_output_preview(data)
                await self.send(text_data=data)
            else:
                await asyncio.sleep(0.03)
        if not self.closed_by_server:
            self.closed_by_server = True
            await self.close(code=1000)

    def _read_socket_once(self):
        if not self.socket:
            return ''
        self.socket.update(timeout=0.2)
        output = []
        if self.socket.peek_stdout():
            output.append(self.socket.read_stdout())
        if self.socket.peek_stderr():
            output.append(self.socket.read_stderr())
        return ''.join(output)

    async def _handle_control_message(self, text_data):
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            return False
        if message.get('type') == 'resize':
            await self._audit('k8s.pod_exec.resize', {
                'namespace': self.namespace,
                'pod': self.pod,
                'container': self.container,
                'cols': int(message.get('cols') or 120),
                'rows': int(message.get('rows') or 36),
            }, throttled=True)
            return True
        return False

    async def _audit_terminal_input(self, data):
        for byte in data:
            char = chr(byte)
            if char in {'\r', '\n'}:
                command = ''.join(self.command_buffer).strip()
                self.command_buffer = []
                if command:
                    await self._record_command(command)
            elif char in {'\x08', '\x7f'}:
                if self.command_buffer:
                    self.command_buffer.pop()
            elif char in {'\x03', '\x15'}:
                self.command_buffer = []
            elif 32 <= byte <= 126:
                self.command_buffer.append(char)

    @database_sync_to_async
    def _create_exec_record(self):
        return K8sExecRecord.objects.create(
            organization=user_organization(self.user),
            cluster_name=self.cluster.name,
            namespace=self.namespace,
            pod=self.pod,
            container=self.container,
            command='interactive shell',
            actor=getattr(self.user, 'username', 'system') or 'system',
            status='running',
        )

    @database_sync_to_async
    def _record_command(self, command):
        K8sOperationAudit.objects.create(
            organization=user_organization(self.user),
            cluster_name=self.cluster.name,
            namespace=self.namespace,
            resource_type='Pod',
            resource_name=self.pod,
            action='exec_command',
            actor=getattr(self.user, 'username', 'system') or 'system',
            status='submitted',
            request_summary={'container': self.container, 'command': command},
            result_summary={'record_id': self.record.pk},
        )

    @database_sync_to_async
    def _finish_exec_record(self, code):
        self.record.status = 'completed' if code in {1000, 1001} else 'closed'
        self.record.output_preview = ''.join(self.output_preview)[:2000]
        self.record.save(update_fields=['status', 'output_preview', 'updated_at'])

    async def _append_output_preview(self, data):
        if self.output_preview_size >= 2000:
            return
        self.output_preview.append(data)
        self.output_preview_size += len(data)

    async def _close_exec_socket(self):
        active = self.socket
        self.socket = None
        if active:
            await asyncio.to_thread(active.close)

    async def _audit(self, action, detail, throttled=False):
        if throttled:
            now = time.monotonic()
            if now < self.last_keepalive_at + 5:
                return
            self.last_keepalive_at = now
        await database_sync_to_async(AuditLog.objects.create)(
            organization=user_organization(self.user),
            actor=getattr(self.user, 'username', 'system') or 'system',
            action=action,
            resource=f'{self.namespace}/{self.pod}',
            ip_address=request_ip_from_scope(self.scope),
            detail=detail,
        )

    def _query_param(self, key):
        params = parse_qs(self.scope.get('query_string', b'').decode('utf-8'))
        return (params.get(key) or [''])[0]


class K8sPodLogConsumer(AsyncWebsocketConsumer):
    """Stream Kubernetes Pod logs over WebSocket, similar to kubectl logs -f."""

    async def connect(self):
        self.closed_by_server = False
        try:
            self.user = await self._authenticate()
            self.cluster = await self._cluster()
            self.namespace = self.scope['url_route']['kwargs']['namespace']
            self.pod = self.scope['url_route']['kwargs']['pod']
            self.container = self._query_param('container')
            await database_sync_to_async(assert_namespace_permission)(self.user, self.cluster, self.namespace, 'logs', 'read')
            await self.accept()
            self.reader_task = asyncio.create_task(self._stream_logs())
        except Exception as exc:
            await self.accept()
            await self.send(text_data=f'日志流连接失败：{exc}\n')
            await self.close(code=4400)

    async def disconnect(self, code):
        self.closed_by_server = True
        if hasattr(self, 'reader_task'):
            self.reader_task.cancel()

    async def receive(self, text_data=None, bytes_data=None):
        if text_data == 'close':
            self.closed_by_server = True
            await self.close(code=1000)

    async def _stream_logs(self):
        _config, api_client = await asyncio.to_thread(build_api_client, self.cluster)
        core = client.CoreV1Api(api_client)
        try:
            text = await asyncio.to_thread(
                core.read_namespaced_pod_log,
                name=self.pod,
                namespace=self.namespace,
                container=self.container or None,
                follow=True,
                timestamps=True,
                _preload_content=False,
                _request_timeout=60,
            )
            for chunk in text:
                if self.closed_by_server:
                    break
                line = chunk.decode('utf-8', errors='ignore') if isinstance(chunk, bytes) else str(chunk)
                if line:
                    await self.send(text_data=line)
        except Exception as exc:
            await self.send(text_data=f'\n日志流已断开：{exc}\n')
        finally:
            if not self.closed_by_server:
                await self.close(code=1000)

    async def _authenticate(self):
        token = self._query_param('token')
        payload = decode_token(token, expected_type='access')
        return await database_sync_to_async(User.objects.select_related('profile').get)(pk=payload['sub'], is_active=True)

    @database_sync_to_async
    def _cluster(self):
        cluster_id = self._query_param('cluster')
        queryset = scoped_queryset(K8sCluster.objects.all(), self.user)
        cluster = queryset.filter(pk=cluster_id).first() if cluster_id else select_preferred_cluster(queryset)
        if not cluster:
            raise ValueError('未找到可访问的 K8s 集群')
        if cluster.status == 'disabled':
            raise ValueError('当前集群已禁用')
        return cluster

    def _query_param(self, key):
        params = parse_qs(self.scope.get('query_string', b'').decode('utf-8'))
        return (params.get(key) or [''])[0]


def request_ip_from_scope(scope):
    headers = dict(scope.get('headers') or [])
    forwarded = headers.get(b'x-forwarded-for', b'').decode('utf-8')
    if forwarded:
        return forwarded.split(',')[0].strip()
    client_addr = scope.get('client') or ('127.0.0.1', 0)
    return client_addr[0] or '127.0.0.1'
