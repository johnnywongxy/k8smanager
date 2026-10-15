"""节点侧镜像仓库的用例：选点、文本生成、远程落地与 /etc/hosts 对齐。

这些事都不需要真的连机器：选点与文本生成是纯函数，远程动作全部经 ``client_factory`` /
``relay_factory`` / ``prober`` 注入，用假客户端断言命令序列。真实 SSH 通道由 ``credentials``
的用例守着，隧道的转发实现由 ``test_platform_relay.py`` 守着。

真实背景：平台与节点不在同一张网里，kubeasz 写进节点 ``/etc/hosts`` 的仓库地址是 NAT 出口，
节点拉不到 pause 沙箱镜像，一个 Pod 都起不来（见 ``services/node_registry.py`` 开头）。
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from k8s_deployment.models import K8sDeploymentNode
from k8s_deployment.services import node_registry, platform_relay


def make_node(ip_address, node_id=None, roles=None, cluster_ip='', ssh_port=22, ssh_user='root'):
    """构造一个未落库的节点快照。

    参数：`ip_address` 表示操作员填写的地址；`node_id` 表示主键（选点排序用）；`roles` 表示角色列表；
        `cluster_ip` 表示内网地址；`ssh_port` 表示 SSH 端口；`ssh_user` 表示登录用户名。
    返回：返回未保存的 K8sDeploymentNode 实例。
    副作用：只在本机对象上设置字段，不写数据库。
    """
    return K8sDeploymentNode(
        id=node_id, name=ip_address, ip_address=ip_address, cluster_ip=cluster_ip,
        ssh_port=ssh_port, ssh_user=ssh_user, roles=roles or [],
    )


class _Stream:
    """假的 SSH 输出流：``recv_exit_status`` 与 ``read`` 都由预设值喂出来。"""

    def __init__(self, text='', code=0):
        """初始化。

        参数：`text` 表示流内容；`code` 表示退出码。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.text = text
        self.code = code
        self.channel = self

    def recv_exit_status(self):
        """返回预设退出码。"""
        return self.code

    def read(self):
        """返回预设内容的字节形式。"""
        return self.text.encode('utf-8')


class FakeSSHClient:
    """假的 SSH 客户端：按命令前缀匹配处理函数，并记录每一条命令。

    ``exec_command`` 的形状与 paramiko 一致（``(stdin, stdout, stderr)``），这样假的客户端
    能直接喂给 ``credentials.run_command``，不必把被测代码里的调用方式也一起打桩。
    """

    def __init__(self, handlers=None, default=None):
        """初始化。

        参数：`handlers` 表示 ``(前缀, 处理函数)`` 列表，处理函数收到命令文本并返回
            ``(退出码, 标准输出, 标准错误)``；`default` 表示没有前缀匹配时的兜底处理函数。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.commands = []
        self.sftp = None
        self.sftp_error = None
        self.handlers = list(handlers or [])
        self.default = default or (lambda _command: (0, '', ''))

    def exec_command(self, command, timeout=None):
        """执行一条假命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(None, stdout, stderr)``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        for prefix, handler in self.handlers:
            if command.startswith(prefix):
                code, out, err = handler(command)
                return None, _Stream(out, code), _Stream(err, code)
        code, out, err = self.default(command)
        return None, _Stream(out, code), _Stream(err, code)

    def open_sftp(self):
        """返回预设的 SFTP 替身；配了异常时改抛异常。

        参数：无。
        返回：返回 SFTP 替身。
        副作用：无。
        """
        if self.sftp_error is not None:
            raise self.sftp_error
        return self.sftp

    def close(self):
        """关闭连接（假实现只记一笔）。

        参数：无。
        返回：无显式返回值。
        副作用：置上 ``closed`` 标记。
        """
        self.closed = True

    def fired(self, needle):
        """取回所有包含给定片段的命令，便于断言子序列。

        参数：`needle` 表示要匹配的片段。
        返回：返回匹配到的命令列表。
        副作用：只做纯计算。
        """
        return [item for item in self.commands if needle in item]


class FakeSFTP:
    """假的 SFTP 通道：记录 put/chmod，并可按需谎报落地大小。"""

    def __init__(self, size_override=None):
        """初始化。

        参数：`size_override` 表示回读大小时谎报的字节数，用来模拟传输被截断。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.puts = []
        self.modes = {}
        self.size_override = size_override
        self.closed = False
        self._sizes = {}

    def put(self, local_path, remote_path):
        """记录一次上传，并按本机文件大小记账。

        参数：`local_path` 表示本机路径；`remote_path` 表示远端路径。
        返回：无显式返回值。
        副作用：读本机文件元数据。
        """
        self.puts.append((str(local_path), str(remote_path)))
        self._sizes[str(remote_path)] = Path(local_path).stat().st_size

    def chmod(self, remote_path, mode):
        """记录落地权限。"""
        self.modes[str(remote_path)] = mode

    def stat(self, remote_path):
        """返回远端文件的元数据。"""
        size = self.size_override
        if size is None:
            size = self._sizes[str(remote_path)]
        return SimpleNamespace(st_size=size)

    def close(self):
        """关闭通道。"""
        self.closed = True


class _RecordingContext:
    """够用的任务上下文替身：收集日志，不做任何持久化。"""

    def __init__(self):
        """初始化。"""
        self.logs = []

    def log(self, stream, text):
        """收集日志。

        参数：`stream` 表示日志流；`text` 表示日志正文。
        返回：无显式返回值。
        副作用：向 ``logs`` 追加一行。
        """
        self.logs.append(text)

    def text(self):
        """把收集到的日志拼成一段文本，便于断言包含关系。"""
        return '\n'.join(self.logs)


class RegistryHostNodeTest(SimpleTestCase):
    """``registry_host_node``：镜像宿主节点必须选得稳定。"""

    def test_single_node_hosts_itself(self):
        """只有一个节点时就用它自己，不看角色。"""
        only = make_node('192.168.1.10', node_id=7, roles=['node'])
        self.assertIs(node_registry.registry_host_node([only]), only)

    def test_multi_node_prefers_first_master(self):
        """多节点时取第一台 master —— 仓库有固定位置，运维找起来不用猜。"""
        nodes = [
            make_node('192.168.1.10', node_id=1, roles=['node']),
            make_node('192.168.1.11', node_id=2, roles=['etcd', 'master']),
            make_node('192.168.1.12', node_id=3, roles=['master']),
        ]
        self.assertEqual(node_registry.registry_host_node(nodes).ip_address, '192.168.1.11')

    def test_multi_node_orders_by_id(self):
        """同一份清单永远选出同一台：宿主漂移会让镜像全部重传。"""
        nodes = [
            make_node('192.168.1.12', node_id=9, roles=['master']),
            make_node('192.168.1.11', node_id=4, roles=['master']),
        ]
        self.assertEqual(node_registry.registry_host_node(nodes).ip_address, '192.168.1.11')

    def test_multi_node_without_master_falls_back_to_first(self):
        """纯 worker 清单正常流程到不了这里，但也不该抛异常。"""
        nodes = [
            make_node('192.168.1.10', node_id=1, roles=['node']),
            make_node('192.168.1.11', node_id=2, roles=['node']),
        ]
        self.assertEqual(node_registry.registry_host_node(nodes).ip_address, '192.168.1.10')

    def test_load_balancer_machines_are_never_chosen(self):
        """负载均衡机器不是集群节点，镜像仓库不能落在它上面。

        它们装的是 l4lb（nginx）与 keepalived，没有容器运行时；仓库落在那里会以一句
        ``dial tcp ...:5000: i/o timeout`` 表现为集群起不来，而那句话完全指不到宿主节点。
        """
        nodes = [
            make_node('192.168.1.6', node_id=1, roles=['loadbalancer']),
            make_node('192.168.1.7', node_id=2, roles=['loadbalancer']),
            make_node('192.168.1.11', node_id=3, roles=['etcd', 'master']),
        ]
        self.assertEqual(node_registry.registry_host_node(nodes).ip_address, '192.168.1.11')

    def test_empty_nodes_is_reported(self):
        """没有节点就说清楚，不要在远程动作里才炸。"""
        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.registry_host_node([])
        self.assertEqual(caught.exception.error_code, 'no_nodes')


class RegistryAddressTest(SimpleTestCase):
    """``registry_address``：取内网地址，不取操作员填的公网地址。"""

    def test_prefers_cluster_ip(self):
        """有内网地址就用它 —— 节点之间必定通内网，未必通公网。"""
        node = make_node('114.67.76.251', cluster_ip='172.30.0.8')
        self.assertEqual(node_registry.registry_address(node), '172.30.0.8')

    def test_falls_back_to_input_ip(self):
        """没探测出内网地址时退回操作员填的地址。"""
        node = make_node('114.67.76.251')
        self.assertEqual(node_registry.registry_address(node), '114.67.76.251')


class RenderedTextTest(SimpleTestCase):
    """``render_config`` / ``render_unit``：节点上落地的文本内容。"""

    def test_config_binds_all_interfaces(self):
        """隧道从回环拨进来、其余节点从内网拨进来，两种来源都要收。"""
        text = node_registry.render_config()
        self.assertIn('addr: 0.0.0.0:5000', text)
        self.assertIn(f'rootdirectory: {node_registry.DATA_DIR}', text)

    def test_config_uses_custom_data_dir(self):
        """数据目录可替换，便于测试与以后改路径。"""
        self.assertIn('rootdirectory: /tmp/x', node_registry.render_config(data_dir='/tmp/x'))

    def test_config_spells_out_every_upload_purge_field(self):
        """uploadpurging 四个键一个都不能少。

        distribution 缺 ``dryrun`` 时不取默认值而是直接 panic（``Unable to parse upload
        purge configuration: dryrun missing``），服务起不来、部署停在镜像分发那一步，
        报错文本还看不出跟配置有关 —— 真机上就是这么炸的，这里钉住。
        """
        text = node_registry.render_config()
        self.assertIn('    uploadpurging:\n', text)
        for line in ('      enabled: true\n', '      age: 168h\n',
                     '      interval: 24h\n', '      dryrun: false\n'):
            self.assertIn(line, text)

    def test_config_keeps_the_purge_block_indented(self):
        """缩进错一层就会挂到 storage 根上，registry 一样起不来。"""
        lines = node_registry.render_config().splitlines()
        start = lines.index('    uploadpurging:')
        self.assertEqual(lines[start + 1:start + 5], [
            '      enabled: true', '      age: 168h', '      interval: 24h', '      dryrun: false',
        ])
        self.assertEqual(lines[start - 1], '  maintenance:')

    def test_unit_starts_binary_with_config(self):
        """ExecStart 必须把配置路径带上，否则 registry 用默认配置去写 /var/lib。"""
        text = node_registry.render_unit()
        self.assertIn(
            f'ExecStart={node_registry.BINARY_PATH} serve {node_registry.CONFIG_PATH}', text,
        )

    def test_unit_restarts_always(self):
        """仓库是集群的长期依赖：节点重启后它要自己回来。"""
        self.assertIn('Restart=always', node_registry.render_unit())
        self.assertIn('WantedBy=multi-user.target', node_registry.render_unit())


class HostsCommandTest(SimpleTestCase):
    """``hosts_command``：改写 /etc/hosts 的命令文本。"""

    def test_deletes_then_appends_and_prints(self):
        """先删后写再加一句 grep：kubeasz 写的那一行此刻一定在，所以等价于替换。"""
        command = node_registry.hosts_command('172.30.0.8')
        self.assertIn("/easzlab\\.io\\.local/d", command)
        self.assertIn("'172.30.0.8'", command)
        self.assertIn("'easzlab.io.local'", command)
        self.assertTrue(command.index('sed -i') < command.index('>> /etc/hosts'))
        self.assertIn('grep -n easzlab.io.local /etc/hosts', command)


class PutFileTest(SimpleTestCase):
    """``put_file``：二进制经 SFTP 落地的几条路径。"""

    def setUp(self):
        """准备一个真实的小文件与临时目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录与文件。
        """
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.work, ignore_errors=True))
        self.local = self.work / 'registry'
        self.local.write_bytes(b'\x7fELF' + b'\x00' * 32)

    def test_uploads_and_chmods(self):
        """传完后按给定权限落地，并返回实际字节数。"""
        sftp = FakeSFTP()
        client = FakeSSHClient()
        client.sftp = sftp

        size = node_registry.put_file(client, self.local, '/usr/local/bin/x', mode=0o755)

        self.assertEqual(size, self.local.stat().st_size)
        self.assertEqual(sftp.puts, [(str(self.local), '/usr/local/bin/x')])
        self.assertEqual(sftp.modes['/usr/local/bin/x'], 0o755)
        self.assertTrue(sftp.closed)

    def test_truncated_upload_is_reported(self):
        """公网链路断在中间时 put 自己可能返回成功，必须回读大小才发现。"""
        sftp = FakeSFTP(size_override=10)
        client = FakeSSHClient()
        client.sftp = sftp

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.put_file(client, self.local, '/usr/local/bin/x')

        self.assertEqual(caught.exception.error_code, 'upload_incomplete')
        self.assertIn('截断', caught.exception.message)

    def test_missing_sftp_subsystem_is_explained(self):
        """sshd 没开 SFTP 子系统时，报错要直接点出该改哪一行。"""
        client = FakeSSHClient()
        client.sftp_error = OSError('Socket is closed')

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.put_file(client, self.local, '/usr/local/bin/x')

        self.assertEqual(caught.exception.error_code, 'sftp_unavailable')
        self.assertIn('Subsystem sftp', caught.exception.message)


class InstallTest(SimpleTestCase):
    """``install``：幂等、落地内容与失败路径。"""

    def setUp(self):
        """准备宿主节点、临时工作目录与一个假的二进制。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录与文件。
        """
        self.node = make_node('114.67.76.251', cluster_ip='172.30.0.8', roles=['master'])
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.work, ignore_errors=True))
        self.binary = self.work / 'registry'
        self.binary.write_bytes(b'\x7fELF' + b'\x00' * 16)
        self.context = _RecordingContext()

    def install(self, client, **overrides):
        """在打桩的前提下跑一次安装。

        参数：`client` 表示假客户端；`overrides` 表示要覆盖的调用参数。
        返回：返回安装摘要。
        副作用：在目标机上执行被记录下来的假命令。
        """
        kwargs = {
            'client_factory': lambda **_kw: client,
            'key_path': str(self.work / 'id_rsa'),
        }
        kwargs.update(overrides)
        return node_registry.install(
            self.context, self.node, {'user': 'root', 'port': 22},
            self.binary, self.work / 'node-registry', **kwargs,
        )

    def active_client(self, active=True, sftp=None):
        """拼一个「服务状态可控」的假客户端。

        参数：`active` 表示 ``systemctl is-active`` 的结论；`sftp` 表示 SFTP 替身。
        返回：返回假客户端。
        副作用：只在本机对象上记录配置。
        """
        state = {'active': active}
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet firewalld', lambda _c: (1, '', '')),
            ('systemctl is-active --quiet', lambda _c: (0 if state['active'] else 3, '', '')),
            ('systemctl daemon-reload', lambda _c: (0, '', '')),
        ])
        client.sftp = sftp or FakeSFTP()
        return client

    def test_second_run_skips_the_upload(self):
        """服务已经在跑就直接返回：重跑部署时这一步应当接近零开销。"""
        sftp = FakeSFTP()
        client = self.active_client(active=True, sftp=sftp)

        summary = self.install(client)

        self.assertFalse(summary['installed'])
        self.assertEqual(summary['address'], '172.30.0.8')
        self.assertEqual(sftp.puts, [])
        self.assertIn('跳过安装', self.context.text())

    def test_installs_three_files_and_starts_service(self):
        """首次安装：建目录、传三个文件、放行端口、启动并核对状态。"""
        state = {'active': False}
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet firewalld', lambda _c: (1, '', '')),
            ('systemctl is-active --quiet', lambda _c: (0 if state['active'] else 3, '', '')),
            ('systemctl daemon-reload', lambda _c: state.update(active=True) or (0, '', '')),
        ])
        sftp = FakeSFTP()
        client.sftp = sftp

        summary = self.install(client)

        self.assertTrue(summary['installed'])
        self.assertEqual([item[1] for item in sftp.puts], [
            node_registry.BINARY_PATH, node_registry.CONFIG_PATH, node_registry.UNIT_PATH,
        ])
        self.assertEqual(sftp.modes[node_registry.BINARY_PATH], 0o755)
        self.assertIn(f'systemctl enable --now {node_registry.UNIT_NAME}', client.commands[-2])
        self.assertIn(node_registry.DATA_DIR, client.fired('mkdir -p')[0])
        self.assertIn('已启动', self.context.text())

    def test_writes_config_and_unit_to_workspace(self):
        """配置与单元同时留一份本地副本：排查时不必再登录节点。"""
        client = self.active_client(active=True)
        self.install(client)

        work = self.work / 'node-registry'
        self.assertIn('0.0.0.0:5000', (work / 'registry.yml').read_text(encoding='utf-8'))
        self.assertIn(
            'ExecStart=', (work / node_registry.UNIT_NAME).read_text(encoding='utf-8'),
        )

    def test_directory_failure_is_reported(self):
        """建目录失败就停在这一步，不要接着传文件后报一个看不懂的错。"""
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet', lambda _c: (3, '', '')),
            ('mkdir -p', lambda _c: (1, '', 'Permission denied')),
        ])
        client.sftp = FakeSFTP()

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            self.install(client)

        self.assertEqual(caught.exception.error_code, 'registry_dir_failed')
        self.assertIn('Permission denied', caught.exception.message)

    def test_start_failure_carries_journal_tail(self):
        """启动失败的真正原因只在 journal 里，直接带进失败原因。"""
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet', lambda _c: (3, '', '')),
            ('systemctl daemon-reload', lambda _c: (1, '', 'Job for x failed')),
            ('journalctl', lambda _c: (0, 'address already in use', '')),
        ])
        client.sftp = FakeSFTP()

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            self.install(client)

        self.assertEqual(caught.exception.error_code, 'registry_start_failed')
        self.assertIn('address already in use', caught.exception.message)

    def test_enabled_but_not_active_is_reported(self):
        """命令返回 0 不等于服务在跑，状态要单独核对一次。"""
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet', lambda _c: (3, '', '')),
            ('systemctl daemon-reload', lambda _c: (0, '', '')),
            ('journalctl', lambda _c: (0, ' exited, status=203/EXEC', '')),
        ])
        client.sftp = FakeSFTP()

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            self.install(client)

        self.assertEqual(caught.exception.error_code, 'registry_not_active')
        self.assertIn('203/EXEC', caught.exception.message)

    def test_missing_address_is_reported(self):
        """既没有内网地址也没有连接地址时没法决定仓库在哪，直接说清楚。"""
        node = make_node('')
        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.install(
                self.context, node, {}, self.binary, self.work / 'x',
                key_path='k', client_factory=lambda **_kw: FakeSSHClient(),
            )
        self.assertEqual(caught.exception.error_code, 'registry_address_missing')


class UninstallTest(SimpleTestCase):
    """``uninstall``：清理集群时把节点侧的镜像仓库拆掉，但留下镜像数据。"""

    def setUp(self):
        """准备宿主节点与一个记录日志的上下文。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上准备数据。
        """
        self.node = make_node('114.67.76.251', cluster_ip='172.30.0.8', roles=['master'])
        self.context = _RecordingContext()

    def uninstall(self, client, **overrides):
        """在打桩前提下跑一次卸载。

        参数：`client` 表示假客户端；`overrides` 表示要覆盖的调用参数。
        返回：返回卸载摘要。
        副作用：在目标机上执行被记录下来的假命令。
        """
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_registry.uninstall(self.context, self.node, {'user': 'root', 'port': 22}, **kwargs)

    def test_idempotent_when_nothing_is_installed(self):
        """服务与文件都不在时照样算成功：这套命令常在集群已经出问题之后执行。"""
        client = FakeSSHClient(default=lambda _c: (0, node_registry.UNINSTALL_OK, ''))

        summary = self.uninstall(client)

        self.assertTrue(summary['removed'])
        self.assertEqual(summary['data_dir'], node_registry.DATA_DIR)

    def test_stops_service_and_deletes_the_three_files(self):
        """停服务、取消开机自启、删单元/二进制/配置，再让 systemd 重新读一遍。"""
        client = FakeSSHClient(default=lambda _c: (0, node_registry.UNINSTALL_OK, ''))

        self.uninstall(client)

        command = client.commands[0]
        self.assertIn(f'systemctl stop {node_registry.UNIT_NAME}', command)
        self.assertIn(f'systemctl disable {node_registry.UNIT_NAME}', command)
        self.assertIn('rm -f', command)
        for path in (node_registry.UNIT_PATH, node_registry.BINARY_PATH, node_registry.CONFIG_PATH):
            self.assertIn(path, command)
        self.assertIn('systemctl daemon-reload', command)

    def test_keeps_the_image_data_directory(self):
        """数据目录不删：下一次部署就落在这台机器上，留着能省掉一次约 1GB 的重推。"""
        client = FakeSSHClient(default=lambda _c: (0, node_registry.UNINSTALL_OK, ''))

        summary = self.uninstall(client)

        self.assertNotIn(f'rm -rf {node_registry.DATA_DIR}', client.commands[0])
        self.assertEqual(summary['data_dir'], node_registry.DATA_DIR)
        # 目录路径要写进日志，操作员想腾空间时知道该删哪里。
        self.assertIn(node_registry.DATA_DIR, self.context.text())

    def test_removes_only_its_own_hosts_line(self):
        """只删平台自己写的那一行；kubeasz 的 managed block 归 kubeasz 自己管。"""
        client = FakeSSHClient(default=lambda _c: (0, node_registry.UNINSTALL_OK, ''))

        self.uninstall(client)

        command = client.commands[0]
        self.assertIn("/easzlab\\.io\\.local/d", command)
        # 点必须转义，否则正则里的 . 会匹配任意字符，把别人的行也一起删了。
        self.assertIn('\\.', node_registry.HOSTS_PATTERN)
        # 删除动作只针对那一行，不能牵到相邻行或整块 managed block。
        self.assertNotIn('+1d', command)
        self.assertNotIn('KUBEASZ MANAGED BLOCK', command)

    def test_partial_removal_is_reported_without_failing(self):
        """文件没删干净只记提醒，不抛错：残留一个仓库不影响重新部署。"""
        client = FakeSSHClient(default=lambda _c: (0, node_registry.UNINSTALL_PARTIAL, ''))

        summary = self.uninstall(client)

        self.assertFalse(summary['removed'])
        self.assertIn('未能完全卸载', self.context.text())

    def test_command_failure_raises(self):
        """连不上或命令整体失败才抛错，由调用方决定是否容忍（状态机里就容忍）。"""
        client = FakeSSHClient(default=lambda _c: (1, '', 'Permission denied'))

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            self.uninstall(client)

        self.assertEqual(caught.exception.error_code, 'registry_uninstall_failed')
        self.assertIn('Permission denied', caught.exception.message)


class OpenRegistryPortTest(SimpleTestCase):
    """``_open_registry_port``：只处理防火墙，并说清安全组不归平台管。"""

    def test_noop_without_firewalld(self):
        """firewalld 没在跑（kubeasz 的常规状态），入站不受它管，什么都不用做。"""
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet firewalld', lambda _c: (3, '', '')),
        ])
        node_registry._open_registry_port(_RecordingContext(), client, '172.30.0.8')
        self.assertEqual(client.fired('firewall-cmd'), [])

    def test_opens_port_when_firewalld_runs(self):
        """firewalld 在跑就永久放行 5000/tcp 并重载。"""
        context = _RecordingContext()
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet firewalld', lambda _c: (0, '', '')),
            ('firewall-cmd', lambda _c: (0, 'success', '')),
        ])
        node_registry._open_registry_port(context, client, '172.30.0.8')

        self.assertIn('firewall-cmd --add-port=5000/tcp --permanent', client.commands)
        self.assertEqual(context.text(), '节点 172.30.0.8 的 firewalld 已放行 5000/tcp')

    def test_failed_open_mentions_security_groups(self):
        """放行失败时要点明安全组在机器之外 —— 否则会走进死胡同。"""
        context = _RecordingContext()
        client = FakeSSHClient(handlers=[
            ('systemctl is-active --quiet firewalld', lambda _c: (0, '', '')),
            ('firewall-cmd', lambda _c: (1, '', 'not authorized')),
        ])
        node_registry._open_registry_port(context, client, '172.30.0.8')

        self.assertIn('安全组', context.text())
        self.assertIn('firewall-cmd --add-port=5000/tcp --permanent', context.text())


class OpenTunnelTest(SimpleTestCase):
    """``open_tunnel``：通道的方向、两端端口与失败原因。"""

    def setUp(self):
        """准备一台带内网地址的节点。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上设置字段。
        """
        self.node = make_node('114.67.76.251', cluster_ip='172.30.0.8', roles=['master'])

    def test_forwards_loopback_to_node_registry_port(self):
        """平台侧绑回环的隧道端口，转发到节点上的 5000。"""
        calls = {}

        class _Relay:
            def __init__(self, target, **kwargs):
                calls['target'] = target
                calls.update(kwargs)

            def start(self):
                calls['started'] = True

        relay = node_registry.open_tunnel(
            self.node, {'user': 'root', 'port': 2222},
            key_path='/tmp/id_rsa', relay_factory=_Relay,
        )

        self.assertIsInstance(relay, _Relay)
        self.assertEqual(calls['target']['address'], '127.0.0.1')
        self.assertEqual(calls['target']['host'], '114.67.76.251')
        self.assertEqual(calls['target']['user'], 'root')
        self.assertEqual(calls['target']['port'], 2222)
        self.assertEqual(calls['port'], node_registry.TUNNEL_PORT)
        self.assertEqual(calls['destination_port'], node_registry.REGISTRY_PORT)
        self.assertEqual(calls['key_path'], '/tmp/id_rsa')
        self.assertTrue(calls['started'])

    def test_relay_error_becomes_chinese_failure(self):
        """建不起通道时给一句能照着排查的中文原因，而不是 RelayError 原文。"""

        class _Broken:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                """真实的 Relay 在 start 时才连机器，失败也只在这里发生。"""
                raise platform_relay.RelayError('ssh 目标机失败')

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.open_tunnel(self.node, {}, key_path='k', relay_factory=_Broken)

        self.assertEqual(caught.exception.error_code, 'tunnel_failed')
        self.assertIn('114.67.76.251', caught.exception.message)


class TunnelReadyTest(SimpleTestCase):
    """``tunnel_ready``：就绪探测的重试与耗尽。"""

    def test_returns_true_on_first_answer(self):
        """对面一开始就应答时不该再等。"""
        probes = []

        def prober(port):
            """记录并立即返回就绪。"""
            probes.append(port)
            return True

        with mock.patch.object(node_registry.time, 'sleep') as sleep:
            self.assertTrue(node_registry.tunnel_ready(port=15000, prober=prober))

        self.assertEqual(probes, [15000])
        sleep.assert_not_called()

    def test_returns_false_after_exhausting_attempts(self):
        """一直不应答就报未就绪，不无限等下去。"""
        with mock.patch.object(node_registry.time, 'sleep') as sleep:
            ready = node_registry.tunnel_ready(attempts=3, interval=0.5, prober=lambda _p: False)

        self.assertFalse(ready)
        self.assertEqual(sleep.call_count, 3)

    def test_failure_hint_names_all_three_causes(self):
        """通道通了但对面不应答，只可能是三件事，一次说清。"""
        text = node_registry.describe_tunnel_failure(make_node('114.67.76.251'))
        self.assertIn('AllowTcpForwarding', text)
        self.assertIn(node_registry.UNIT_NAME, text)
        self.assertIn('安全组', text)


class AlignResolutionTest(SimpleTestCase):
    """``align_resolution``：把每个节点的 easzlab.io.local 指回内网地址。"""

    def setUp(self):
        """准备两台节点与该地址改写后的 hosts 行。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上设置字段。
        """
        self.nodes = [
            make_node('114.67.76.251', node_id=1, cluster_ip='172.30.0.8', roles=['master']),
            make_node('114.67.76.252', node_id=2, cluster_ip='172.30.0.9', roles=['node']),
        ]
        self.line = '3\t172.30.0.8\teaszlab.io.local'

    def aligned_client(self):
        """拼一个会照实回报 hosts 行的假客户端。"""
        return FakeSSHClient(handlers=[
            ('sed -i', lambda _c: (0, self.line, '')),
        ])

    def test_rewrites_every_node(self):
        """清单里每台机器都要改：漏一台，那台的 Pod 就还卡在原地。"""
        seen = []
        clients = []

        def factory(**_kwargs):
            """每台节点给一个独立的假客户端。"""
            client = self.aligned_client()
            clients.append(client)
            return client

        results = node_registry.align_resolution(
            self.nodes, {}, '172.30.0.8', key_path='k', client_factory=factory,
            on_node=lambda node, line: seen.append((node.ip_address, line)),
        )

        self.assertEqual(sorted(results), ['114.67.76.251', '114.67.76.252'])
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(item[1] == self.line for item in seen))
        self.assertTrue(all(item.closed for item in clients))

    def test_missing_address_in_output_fails(self):
        """命令返回 0 但结果不对时也要报错：这是「改没改对」的唯一判据。"""
        client = FakeSSHClient(handlers=[
            ('sed -i', lambda _c: (0, '3\t58.211.147.130\teaszlab.io.local', '')),
        ])

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.align_node(client, '172.30.0.8', '114.67.76.251')

        self.assertEqual(caught.exception.error_code, 'hosts_align_verify_failed')

    def test_nonzero_exit_fails(self):
        """命令失败时把远端 stderr 原样带出来。"""
        client = FakeSSHClient(handlers=[
            ('sed -i', lambda _c: (1, '', 'Read-only file system')),
        ])

        with self.assertRaises(node_registry.NodeRegistryError) as caught:
            node_registry.align_node(client, '172.30.0.8', '114.67.76.251')

        self.assertEqual(caught.exception.error_code, 'hosts_align_failed')
        self.assertIn('Read-only file system', caught.exception.message)
