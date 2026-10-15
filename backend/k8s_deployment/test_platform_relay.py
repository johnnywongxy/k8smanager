"""平台侧转发通道的用例：inventory 解析、本机地址挂摘、连接搬运与生命周期。

真实背景：kubeasz 有十几处 ``connection: local``，它们的 kubectl 在**平台机**上执行、打的却是
节点变量表里的 ``inventory_hostname`` —— 节点在 NAT 后面时那是集群内网地址。实测平台机
192.168.1.94 到 172.30.0.7:6443 的 SYN 无人应答，``04.kube-master`` 的
``轮询等待master服务启动完成`` 会一路重试到超时。

这些用例不碰真的网络配置，也不连目标机：

- ``ip`` 命令一律用假的 ``_run`` 顶掉，只断言命令行与落盘清单；
- 连接搬运用 ``socket.socketpair()`` 造一条「假的 SSH 通道」，两端都在测试进程里；
- 可达性判定与通道对象都能注入，容器编排的用例不依赖真实 SSH。
"""

import json
import socket
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.test import SimpleTestCase, override_settings

from k8s_deployment.services import credentials, paths, platform_relay

HOSTS_TEXT = (
    '# 注释行\n'
    '[etcd]\n'
    '172.30.0.7\n'
    '\n'
    '[kube_master]\n'
    "172.30.0.7 ansible_host=116.198.251.203 ansible_ssh_common_args='-o StrictHostKeyChecking=no' k8s_nodename='master-01'\n"
    '172.30.0.8 ansible_host=116.198.251.204 ansible_user=ops ansible_port=2222\n'
    'k8s-master.example.com ansible_host=116.198.251.205\n'
    '\n'
    '[kube_node]\n'
    '172.30.0.9 ansible_host=116.198.251.206\n'
)


class FakeIpcall:
    """假的 ``ip`` 命令执行器：记下命令行，按预设脚本回答。"""

    def __init__(self, responses=None):
        """初始化假执行器。

        参数：`responses` 表示 ``{命令行元组: (退出码, 输出, 错误)}``。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.responses = dict(responses or {})
        self.calls = []

    def __call__(self, argv):
        """记录并回答一次调用。

        参数：`argv` 表示命令行列表。
        返回：返回 ``(退出码, 标准输出, 标准错误)``。
        副作用：记录调用。
        """
        key = tuple(str(item) for item in argv)
        self.calls.append(key)
        for pattern, answer in self.responses.items():
            if all(str(item) in key for item in pattern):
                return answer
        return 0, '', ''

    def ran(self, *parts):
        """判断是否执行过包含这些片段的命令。

        参数：`parts` 表示要匹配的命令行片段。
        返回：返回 True 或 False。
        副作用：纯计算。
        """
        return any(all(str(item) in call for item in parts) for call in self.calls)


class RelayTestCase(SimpleTestCase):
    """公共夹具：独立的临时状态目录。"""

    def setUp(self):
        """准备临时状态根目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录并覆盖状态根设置。
        """
        self.temp = TemporaryDirectory(prefix='k8s-relay-')
        self.addCleanup(self.temp.cleanup)
        overrides = override_settings(K8S_DEPLOY_STATE_ROOT=self.temp.name)
        overrides.enable()
        self.addCleanup(overrides.disable)


class ApiTargetsTest(RelayTestCase):
    """``api_targets``：从 inventory 里挑出平台机真的会去拨的地址。"""

    def test_control_plane_lines_are_picked_with_their_connection_details(self):
        """主机名是集群内网地址，连它用 ``ansible_host``；用户名与端口取该行的覆盖值。"""
        targets = platform_relay.api_targets(HOSTS_TEXT)
        self.assertEqual([item['address'] for item in targets], ['172.30.0.7', '172.30.0.8'])
        self.assertEqual(targets[0]['host'], '116.198.251.203')
        self.assertEqual((targets[0]['user'], targets[0]['port']), ('root', 22))
        self.assertEqual(targets[1]['host'], '116.198.251.204')
        self.assertEqual((targets[1]['user'], targets[1]['port']), ('ops', 2222))

    def test_ignores_sections_the_platform_never_dials(self):
        """etcd 段与 kube_node 段的地址平台机从不直连（连节点一律走 SSH）。"""
        addresses = {item['address'] for item in platform_relay.api_targets(HOSTS_TEXT)}
        self.assertNotIn('172.30.0.9', addresses)

    def test_ignores_domain_names(self):
        """主机名写成域名时不需要接管地址：那种情况下本来就靠 DNS。"""
        addresses = {item['address'] for item in platform_relay.api_targets(HOSTS_TEXT)}
        self.assertNotIn('k8s-master.example.com', addresses)

    def test_line_without_ansible_host_connects_to_itself(self):
        """没写 ``ansible_host`` 时主机名就是连接地址（没有 NAT 的集群）。"""
        text = '[kube_master]\n192.168.1.10\n'
        target = platform_relay.api_targets(text)[0]
        self.assertEqual((target['host'], target['user'], target['port']), ('192.168.1.10', 'root', 22))


class EtcdTargetsTest(RelayTestCase):
    """``etcd_targets``：备份剧本在平台机上要拨的 etcd 地址。"""

    def test_etcd_section_is_picked(self):
        """备份剧本拨的是 ``[etcd]`` 段的地址，平台机同样要能到。"""
        targets = platform_relay.etcd_targets(HOSTS_TEXT)
        self.assertEqual([item['address'] for item in targets], ['172.30.0.7'])
        self.assertEqual(targets[0]['host'], '172.30.0.7')

    def test_control_plane_and_node_sections_are_not_picked(self):
        """控制面与工作节点段不进这份清单：它们的 2379 平台机从不去拨。"""
        text = (
            '[kube_master]\n172.30.0.11 ansible_host=10.0.0.11\n'
            '[kube_node]\n172.30.0.12 ansible_host=10.0.0.12\n'
        )
        self.assertEqual(platform_relay.etcd_targets(text), [])

    def test_ansible_host_is_used_for_the_tunnel(self):
        """inventory 里写了 ``ansible_host`` 时通道连的是它，接管的是内网地址。"""
        text = '[etcd]\n172.30.0.21 ansible_host=114.67.94.104 ansible_user=ops ansible_port=2222\n'
        target = platform_relay.etcd_targets(text)[0]
        self.assertEqual(target['address'], '172.30.0.21')
        self.assertEqual(target['host'], '114.67.94.104')
        self.assertEqual((target['user'], target['port']), ('ops', 2222))

    def test_the_two_routes_read_different_sections(self):
        """同一份 inventory 上，两条链路取各自的段，互不串味。"""
        api = {item['address'] for item in platform_relay.api_targets(HOSTS_TEXT)}
        etcd = {item['address'] for item in platform_relay.etcd_targets(HOSTS_TEXT)}
        self.assertEqual((api, etcd), ({'172.30.0.7', '172.30.0.8'}, {'172.30.0.7'}))


class LbVipTargetsTest(RelayTestCase):
    """``lb_vip_targets``：从 ``[ex_lb]`` 段里挑出要接管的 VIP。

    这一段的格式与别处不同：行首地址是 **LB 机器**，要接管的却是同一行上的
    ``EX_APISERVER_VIP`` —— 那是 keepalived 在机器之间漂移的虚拟地址，不在任何网卡上。
    """

    # 与 ``compiler._lb_host_line`` 生成的格式逐字同构：行首内网地址、``ansible_host`` 是
    # 操作员填的公网地址、三个 kubeasz 变量在行尾。
    LB_HOSTS = (
        '[etcd]\n172.30.0.12 ansible_host=116.198.238.74\n'
        '\n'
        '[ex_lb]\n'
        "172.30.0.16 ansible_host=116.198.237.81 ansible_ssh_common_args='-o StrictHostKeyChecking=no' "
        'LB_ROLE=master EX_APISERVER_VIP=172.30.0.22 EX_APISERVER_PORT=6443\n'
        "172.30.0.14 ansible_host=116.198.239.76 ansible_ssh_common_args='-o StrictHostKeyChecking=no' "
        'LB_ROLE=backup EX_APISERVER_VIP=172.30.0.22 EX_APISERVER_PORT=6443\n'
    )

    def test_vip_is_taken_over_and_the_master_machine_carries_the_tunnel(self):
        """接管的是 VIP，隧道落在 ``LB_ROLE=master`` 那台上（正常持有 VIP 的就是它）。"""
        targets = platform_relay.lb_vip_targets(self.LB_HOSTS)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]['address'], '172.30.0.22')
        self.assertEqual(targets[0]['host'], '116.198.237.81')
        self.assertEqual(targets[0]['listen_port'], 6443)

    def test_master_role_wins_even_when_it_is_not_the_first_line(self):
        """示例 inventory 里 backup 写在前面，接管点不能跟着行序走。"""
        text = (
            '[ex_lb]\n'
            '172.30.0.14 ansible_host=116.198.239.76 LB_ROLE=backup EX_APISERVER_VIP=172.30.0.22\n'
            '172.30.0.16 ansible_host=116.198.237.81 LB_ROLE=master EX_APISERVER_VIP=172.30.0.22\n'
        )
        self.assertEqual(platform_relay.lb_vip_targets(text)[0]['host'], '116.198.237.81')

    def test_first_line_is_used_when_no_role_is_written(self):
        """没写 ``LB_ROLE`` 时退到第一台：kubeasz 自己也会算一个默认角色出来。"""
        text = '[ex_lb]\n172.30.0.16 ansible_host=116.198.237.81 EX_APISERVER_VIP=172.30.0.22\n'
        self.assertEqual(platform_relay.lb_vip_targets(text)[0]['host'], '116.198.237.81')

    def test_the_listen_port_comes_from_the_inventory_line(self):
        """端口以 inventory 为准：操作员把 ApiServer 端口改掉时，监听端口要跟着改。"""
        text = (
            '[ex_lb]\n'
            '172.30.0.16 ansible_host=116.198.237.81 LB_ROLE=master '
            'EX_APISERVER_VIP=172.30.0.22 EX_APISERVER_PORT=8443\n'
        )
        self.assertEqual(platform_relay.lb_vip_targets(text)[0]['listen_port'], 8443)

    def test_ansible_ssh_user_and_port_are_honoured(self):
        """``compiler.host_fields`` 写的是 ``ansible_ssh_user``/``ansible_ssh_port``，要认。"""
        text = (
            '[ex_lb]\n'
            '172.30.0.16 ansible_host=116.198.237.81 ansible_ssh_user=ops ansible_ssh_port=2222 '
            'LB_ROLE=master EX_APISERVER_VIP=172.30.0.22\n'
        )
        target = platform_relay.lb_vip_targets(text)[0]
        self.assertEqual((target['user'], target['port']), ('ops', 2222))

    def test_empty_section_means_nothing_to_take_over(self):
        """云内网负载均衡与「不使用」两种模式下这一段是空的，没有 VIP 要接管。"""
        self.assertEqual(platform_relay.lb_vip_targets('[ex_lb]\n\n[kube_node]\n172.30.0.9\n'), [])

    def test_line_without_ansible_host_connects_to_itself(self):
        """没写 ``ansible_host`` 时行首地址就是连接地址（没有 NAT 的集群）。"""
        text = '[ex_lb]\n192.168.1.16 LB_ROLE=master EX_APISERVER_VIP=192.168.1.250\n'
        target = platform_relay.lb_vip_targets(text)[0]
        self.assertEqual((target['address'], target['host']), ('192.168.1.250', '192.168.1.16'))

    def test_section_without_a_usable_vip_yields_nothing(self):
        """行里没写 VIP（或写了个不是地址的值）时当作没有可接管的地址。"""
        text = '[ex_lb]\n172.30.0.16 ansible_host=116.198.237.81 LB_ROLE=master\n'
        self.assertEqual(platform_relay.lb_vip_targets(text), [])
        broken = '[ex_lb]\n172.30.0.16 ansible_host=116.198.237.81 EX_APISERVER_VIP=lb.example.com\n'
        self.assertEqual(platform_relay.lb_vip_targets(broken), [])

    def test_the_vip_is_not_picked_up_by_the_other_two_routes(self):
        """VIP 不会被控制面/etcd 那两条链路顺手接管：它既不是节点的地址，也不在它们的段里。

        这正是第 8 阶段那一句验活当初过不去的原因 —— 前面两条链路覆盖的是节点地址，
        而验活打的是这个虚拟地址。
        """
        api = {item['address'] for item in platform_relay.api_targets(self.LB_HOSTS)}
        etcd = {item['address'] for item in platform_relay.etcd_targets(self.LB_HOSTS)}
        self.assertEqual(etcd, {'172.30.0.12'})
        for addresses in (api, etcd):
            self.assertNotIn('172.30.0.22', addresses)


class LocalAddressTest(RelayTestCase):
    """在本机接口上挂地址、摘地址，以及留下痕迹的落盘清单。"""

    def test_reads_addresses_from_the_interface(self):
        """``ip -o -4 addr show`` 的输出里只取地址部分。"""
        calls = FakeIpcall({('addr', 'show', 'dev', 'lo'): (
            0,
            '1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n'
            '1: lo    inet 172.30.0.7/32 scope global lo\\       valid_lft forever\n',
            '',
        )})
        with mock.patch.object(platform_relay, '_run', calls):
            self.assertEqual(platform_relay.local_addresses(), {'127.0.0.1', '172.30.0.7'})

    def test_add_uses_a_host_prefix_and_records_the_address(self):
        """挂的是 /32，只让内核把这一个地址认作本机地址，不动路由表。"""
        calls = FakeIpcall()
        with mock.patch.object(platform_relay, '_run', calls):
            self.assertTrue(platform_relay.add_local_address('172.30.0.7'))
        self.assertTrue(calls.ran('addr', 'add', '172.30.0.7/32', 'dev', 'lo'), calls.calls)
        state = json.loads(Path(platform_relay._state_path()).read_text(encoding='utf-8'))
        self.assertEqual(state['addresses'], ['172.30.0.7'])

    def test_add_is_idempotent(self):
        """已经在接口上的地址不再挂一次，但仍然记进清单（它确实是平台挂的）。"""
        calls = FakeIpcall({('addr', 'show', 'dev', 'lo'): (0, 'lo inet 172.30.0.7/32 scope global lo', '')})
        with mock.patch.object(platform_relay, '_run', calls):
            self.assertFalse(platform_relay.add_local_address('172.30.0.7'))
        self.assertFalse(calls.ran('addr', 'add'), calls.calls)
        state = json.loads(Path(platform_relay._state_path()).read_text(encoding='utf-8'))
        self.assertEqual(state['addresses'], ['172.30.0.7'])

    def test_add_failure_explains_the_permission_requirement(self):
        """挂地址要 root：失败时给出的原因必须指向权限，而不是一句命令退出码。"""
        calls = FakeIpcall({('addr', 'add'): (2, '', 'RTNETLINK answers: Operation not permitted')})
        with mock.patch.object(platform_relay, '_run', calls):
            with self.assertRaises(platform_relay.RelayError) as caught:
                platform_relay.add_local_address('172.30.0.7')
        self.assertIn('root', str(caught.exception))

    def test_remove_takes_the_address_off_and_forgets_it(self):
        """摘地址之后清单里不能留着它，否则下次启动会去摘一个不存在的地址。"""
        calls = FakeIpcall()
        with mock.patch.object(platform_relay, '_run', calls):
            platform_relay.add_local_address('172.30.0.7')
            self.assertTrue(platform_relay.remove_local_address('172.30.0.7'))
        self.assertTrue(calls.ran('addr', 'del', '172.30.0.7/32'), calls.calls)
        state = json.loads(Path(platform_relay._state_path()).read_text(encoding='utf-8'))
        self.assertEqual(state['addresses'], [])

    def test_cleanup_removes_what_a_previous_process_left_behind(self):
        """进程被强杀时 ``finally`` 跑不到，地址会留在 lo 上 —— 下次启动按清单清掉。"""
        calls = FakeIpcall()
        with mock.patch.object(platform_relay, '_run', calls):
            platform_relay.add_local_address('172.30.0.7')
            platform_relay.add_local_address('172.30.0.8')
            self.assertEqual(platform_relay.cleanup_stale_addresses(), 2)
        self.assertTrue(calls.ran('addr', 'del', '172.30.0.7/32'), calls.calls)
        self.assertTrue(calls.ran('addr', 'del', '172.30.0.8/32'), calls.calls)
        self.assertEqual(platform_relay.cleanup_stale_addresses(), 0)

    def test_address_owner_finds_the_interface_holding_it(self):
        """本机已经有这个地址时不再往 lo 上挂第二份：重复地址会让选路说不清楚。"""
        calls = FakeIpcall({('addr', 'show'): (
            0,
            '2: eno1    inet 172.30.0.7/24 scope global eno1\\       valid_lft forever\n',
            '',
        )})
        with mock.patch.object(platform_relay, '_run', calls):
            self.assertEqual(platform_relay.address_owner('172.30.0.7'), 'eno1')
            self.assertEqual(platform_relay.address_owner('172.30.0.8'), '')


class _FakeChannel:
    """假的 SSH 通道：另一端接着一个本机套接字，用来观察数据怎么流动。"""

    def __init__(self, handle):
        """记录通道另一端。

        参数：`handle` 表示本机套接字。
        返回：无显式返回值。
        副作用：只记录引用。
        """
        self.handle = handle

    def recv(self, size):
        """读数据。

        参数：`size` 表示最多读取的字节数。
        返回：返回字节串。
        副作用：消费套接字缓冲区。
        """
        return self.handle.recv(size)

    def sendall(self, data):
        """写数据。

        参数：`data` 表示字节串。
        返回：无显式返回值。
        副作用：写入套接字。
        """
        self.handle.sendall(data)

    def close(self):
        """关闭通道。

        参数：无。
        返回：无显式返回值。
        副作用：关闭套接字。
        """
        self.handle.close()


class _FakeTransport:
    """假的 SSH 传输层：``open_channel`` 直接把「节点上的端口」接到本机套接字。"""

    def __init__(self, node_side):
        """记录节点侧套接字。

        参数：`node_side` 表示扮演「节点上被转发的服务」的那一端。
        返回：无显式返回值。
        副作用：只记录引用。
        """
        self.node_side = node_side
        self.active = True
        self.keepalive = None
        self.requests = []

    def is_active(self):
        """判断传输层是否可用。

        参数：无。
        返回：返回 True 或 False。
        副作用：纯计算。
        """
        return self.active

    def set_keepalive(self, seconds):
        """记录心跳间隔。

        参数：`seconds` 表示秒数。
        返回：无显式返回值。
        副作用：只记录。
        """
        self.keepalive = seconds

    def open_channel(self, kind, destination, source):
        """模拟一次端口转发。

        参数：`kind` 表示通道类型；`destination` 表示节点侧目标地址；`source` 表示来源地址。
        返回：返回 ``_FakeChannel``。
        副作用：记录请求；把节点侧套接字交给调用方。
        """
        self.requests.append((kind, destination, source))
        return _FakeChannel(self.node_side)


def _free_port():
    """取一个当前空闲的本机端口。

    参数：无。
    返回：返回端口号。
    副作用：短暂占用再释放一个端口。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _recv_exact(handle, size, timeout=5):
    """读满指定字节数，便于断言。

    参数：`handle` 表示套接字；`size` 表示字节数；`timeout` 表示最长等待秒数。
    返回：返回读到的字节串。
    副作用：消费套接字缓冲区。
    """
    handle.settimeout(timeout)
    data = b''
    while len(data) < size:
        chunk = handle.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


class RelayForwardingTest(RelayTestCase):
    """一条通道本身：连上节点、在本机地址上听、把字节搬过去。"""

    def _start(self, port=None):
        """起一条接到假传输层的通道。

        参数：`port` 表示本机监听端口，为空时取一个空闲端口。
        返回：返回 ``(Relay, _FakeTransport, 节点侧套接字, 端口)``。
        副作用：占用一个本机端口并起接受连接的线程。
        """
        relay_side, node_side = socket.socketpair()
        self.addCleanup(node_side.close)
        transport = _FakeTransport(relay_side)
        client = mock.MagicMock()
        client.get_transport.return_value = transport
        chosen = int(port or _free_port())
        with mock.patch.object(credentials, 'ssh_client', return_value=client) as connect:
            relay = platform_relay.Relay(
                {'address': '127.0.0.1', 'host': '116.198.251.203', 'user': 'root', 'port': 22},
                port=chosen,
                # 与生产接线一致：在临时端口上听，转给节点上的 API Server 端口。
                destination_port=platform_relay.API_PORT,
            ).start()
        self.addCleanup(relay.stop)
        self.connect = connect
        return relay, transport, node_side, chosen

    def test_bytes_travel_between_the_local_port_and_the_node(self):
        """本地连到集群地址的字节，经 SSH 通道原样到达节点，回程也一样。"""
        relay, transport, node_side, port = self._start()
        with socket.create_connection(('127.0.0.1', port), timeout=5) as local:
            local.sendall(b'ping')
            self.assertEqual(_recv_exact(node_side, 4), b'ping')
            node_side.sendall(b'pong')
            self.assertEqual(_recv_exact(local, 4), b'pong')
        kind, destination, _source = transport.requests[0]
        # 通道打到节点自己的回环上：kube-lb 就监听在那里。
        self.assertEqual((kind, destination), ('direct-tcpip', (platform_relay.FORWARD_HOST, platform_relay.API_PORT)))
        self.assertEqual(transport.keepalive, platform_relay.KEEPALIVE_SECONDS)

    def test_connecting_uses_the_deploy_key_and_the_entered_address(self):
        """隧道走的还是平台连节点那条路：同一个地址、同一把部署密钥。"""
        _relay, _transport, _node_side, _port = self._start()
        args, kwargs = self.connect.call_args
        self.assertEqual(args[:3], ('116.198.251.203', 22, 'root'))
        self.assertEqual(kwargs['key_path'], credentials.private_key_path())

    def test_stop_closes_the_listening_port(self):
        """停掉之后端口要放掉，否则地址摘了但端口还占着，下一次任务可能撞上。"""
        relay, _transport, _node_side, port = self._start()
        relay.stop()
        self.assertFalse(relay.alive())
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=1)

    def test_inactive_transport_is_refused(self):
        """SSH 通道没建起来时直接报错，不要把监听留在那里骗人。"""
        client = mock.MagicMock()
        transport = mock.MagicMock()
        transport.is_active.return_value = False
        client.get_transport.return_value = transport
        with mock.patch.object(credentials, 'ssh_client', return_value=client):
            with self.assertRaises(platform_relay.RelayError):
                platform_relay.Relay({'address': '127.0.0.1', 'host': '10.0.0.1', 'user': 'root', 'port': 22},
                                     port=_free_port()).start()
        client.close.assert_called()

    def test_bind_failure_is_reported_with_the_address(self):
        """端口被占时给出的原因要带上地址与端口，运维才知道自己在看哪一条通道。"""
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        relay_side, node_side = socket.socketpair()
        self.addCleanup(node_side.close)
        client = mock.MagicMock()
        client.get_transport.return_value = _FakeTransport(relay_side)
        with mock.patch.object(credentials, 'ssh_client', return_value=client):
            with self.assertRaises(platform_relay.RelayError) as caught:
                platform_relay.Relay({'address': '127.0.0.1', 'host': '10.0.0.1', 'user': 'root', 'port': 22},
                                     port=port).start()
        self.assertIn(f'127.0.0.1:{port}', str(caught.exception))


class FakeRelay:
    """假的通道对象：只回答「活着吗」，并把每次调用记下来。"""

    instances = []

    def __init__(self, target, key_path=None, port=platform_relay.API_PORT, destination_port=None,
                 destination_host=None):
        """记录目标。

        参数：`target` 表示目标信息；其余参数与真实通道一致。
        返回：无显式返回值。
        副作用：把自己登记到类级列表，便于用例断言。
        """
        self.target = dict(target or {})
        self.key_path = key_path
        self.port = port
        self.destination_port = destination_port or port
        self.destination_host = destination_host
        self.alive_flag = True
        self.stopped = False
        FakeRelay.instances.append(self)

    @property
    def address(self):
        """返回被接管的地址。

        参数：无。
        返回：返回地址文本。
        副作用：纯计算。
        """
        return str(self.target.get('address') or '')

    def start(self):
        """假装连上节点。

        参数：无。
        返回：返回自身。
        副作用：无。
        """
        return self

    def alive(self):
        """返回是否还活着。

        参数：无。
        返回：返回 True 或 False。
        副作用：纯计算。
        """
        return self.alive_flag

    def stop(self):
        """假装断开。

        参数：无。
        返回：无显式返回值。
        副作用：把状态改成已停止。
        """
        self.stopped = True
        self.alive_flag = False

    def describe(self):
        """返回可读说明。

        参数：无。
        返回：返回地址。
        副作用：纯计算。
        """
        return self.address


class RelayManagerTest(RelayTestCase):
    """容器编排：什么时候建、什么时候复用、什么时候拆。"""

    def setUp(self):
        """准备一个干净的管理器。

        参数：无。
        返回：无显式返回值。
        副作用：清空假通道的登记表。
        """
        super().setUp()
        FakeRelay.instances = []
        self.manager = platform_relay.RelayManager()
        self.removed = []
        self.added = []
        for target, replacement in (
            ('add_local_address', self._add),
            ('remove_local_address', self._remove),
            ('address_owner', lambda address, device=platform_relay.ALIAS_DEVICE: ''),
            ('Relay', FakeRelay),
        ):
            patcher = mock.patch.object(platform_relay, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _add(self, address, device=platform_relay.ALIAS_DEVICE):
        """记下一次挂地址。

        参数：`address` 表示地址；`device` 表示接口名。
        返回：返回 True。
        副作用：记录调用。
        """
        self.added.append(address)
        return True

    def _remove(self, address, device=platform_relay.ALIAS_DEVICE):
        """记下一次摘地址。

        参数：`address` 表示地址；`device` 表示接口名。
        返回：返回 True。
        副作用：记录调用。
        """
        self.removed.append(address)
        return True

    def target(self, address='172.30.0.7', host='116.198.251.203', user='root', port=22):
        """构造一个目标。

        参数：`address` 表示集群内网地址；`host` 表示连接地址；`user` 表示用户名；`port` 表示 SSH 端口。
        返回：返回目标字典。
        副作用：纯计算。
        """
        return {'address': address, 'host': host, 'user': user, 'port': port}

    def test_reachable_address_is_not_touched(self):
        """平台机连得上的地址什么都不做：绝大多数集群就是这个样子。"""
        outcomes = self.manager.ensure([self.target()], prober=lambda address, port: True)
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'reachable'})
        self.assertEqual((self.added, FakeRelay.instances), ([], []))

    def test_unreachable_address_is_taken_over_and_forwarded(self):
        """平台机到不了时挂地址 + 开通道，两步缺一不可（绑定需要地址先在本机存在）。"""
        outcomes = self.manager.ensure([self.target()], prober=lambda address, port: False)
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'active'})
        self.assertEqual(self.added, ['172.30.0.7'])
        self.assertEqual([item.address for item in FakeRelay.instances], ['172.30.0.7'])
        self.assertEqual(self.manager.active(), ['172.30.0.7'])

    def test_address_already_on_this_machine_is_left_alone(self):
        """平台机自己就在那个网段里时不能接管：那会把真实流量改道进隧道。"""
        with mock.patch.object(platform_relay, 'address_owner', return_value='eno1'):
            outcomes = self.manager.ensure([self.target()], prober=lambda address, port: False)
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'local'})
        self.assertEqual(self.added, [])

    def test_address_the_platform_already_holds_is_never_probed(self):
        """平台自己压着这个地址时，探针问的是平台机自己，答案必然是「通」—— 通道照建。

        这是 94 上升级失败的那条路径：强制备份先把 172.30.0.x 挂上 ``lo``，随后 API 链路
        探 172.30.0.x:22 打到平台机自己的 sshd 上（``*:22``），被当成「平台机够得到这个
        地址」而静默跳过（``reachable`` 不写日志）；剧本里那些 ``connection: local`` 的
        kubectl 于是打到本机没人监听的 6443，SYN 立刻 ECONNREFUSED。
        """
        prober = mock.MagicMock(return_value=True)
        self.manager.ensure(
            [self.target()], prober=lambda address, port: False, route=platform_relay.ETCD_ROUTE,
        )
        outcomes = self.manager.ensure([self.target()], prober=prober)

        self.assertEqual(outcomes, {'172.30.0.7:6443': 'active'})
        self.assertEqual(
            [item.port for item in FakeRelay.instances],
            [platform_relay.ETCD_CLIENT_PORT, platform_relay.API_PORT],
        )
        prober.assert_not_called()

    def test_address_left_in_the_state_file_is_rebuilt_without_probing(self):
        """落盘清单里的地址同样是平台自己挂的：进程重启后也要照建通道，不能靠探针。"""
        with mock.patch.object(platform_relay, '_read_state', return_value={'addresses': ['172.30.0.7']}):
            outcomes = self.manager.ensure([self.target()], prober=lambda address, port: True)
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'active'})
        self.assertEqual([item.address for item in FakeRelay.instances], ['172.30.0.7'])

    def test_live_channel_is_reused(self):
        """通道还活着就不再建：每个 ezctl 阶段之前都会过一遍这里。"""
        prober = mock.MagicMock(return_value=False)
        self.manager.ensure([self.target()], prober=prober)
        outcomes = self.manager.ensure([self.target()], prober=prober)
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'active'})
        self.assertEqual(len(FakeRelay.instances), 1)
        self.assertEqual(prober.call_count, 1)

    def test_dead_channel_is_rebuilt_after_clearing_its_address(self):
        """SSH 掉线后重建：先把地址摘掉再挂一遍，避免留下半截状态。"""
        self.manager.ensure([self.target()], prober=lambda address, port: False)
        FakeRelay.instances[0].alive_flag = False
        outcomes = self.manager.ensure([self.target()], prober=lambda address, port: False)
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'active'})
        self.assertEqual(self.removed, ['172.30.0.7'])
        self.assertEqual(len(FakeRelay.instances), 2)

    def test_failing_channel_rolls_the_address_back(self):
        """通道建不起来时地址也要摘掉：留着它会把平台机自己的流量引进死胡同。"""

        class Broken(FakeRelay):
            """起不来的假通道。"""

            def start(self):
                """抛出与真实实现一致的异常。

                参数：无。
                返回：无返回值。
                副作用：无。
                """
                raise platform_relay.RelayError('SSH 连不上')

        with mock.patch.object(platform_relay, 'Relay', Broken):
            outcomes = self.manager.ensure([self.target()], prober=lambda address, port: False)
        self.assertEqual(self.added, ['172.30.0.7'])
        self.assertEqual(self.removed, ['172.30.0.7'])
        self.assertTrue(outcomes['172.30.0.7:6443'].startswith('failed:'))

    def test_teardown_stops_channels_and_clears_addresses(self):
        """收尾必须两步都做：通道断掉，本机地址摘掉。"""
        self.manager.ensure([self.target()], prober=lambda address, port: False)
        with mock.patch.object(platform_relay, 'cleanup_stale_addresses', return_value=1) as cleanup:
            self.manager.teardown()
        self.assertTrue(FakeRelay.instances[0].stopped)
        self.assertEqual(self.manager.active(), [])
        cleanup.assert_called_once_with()


class RouteResolutionTest(RelayTestCase):
    """一条通道往节点上的哪个地址转。"""

    def build(self, destination_host=None, address='172.30.0.21'):
        """构造一条（不启动的）通道。

        参数：`destination_host` 表示转发目标地址；`address` 表示被接管的集群内网地址。
        返回：返回 ``Relay`` 实例。
        副作用：只创建对象，不连任何东西。
        """
        return platform_relay.Relay(
            {'address': address, 'host': '114.67.94.104', 'user': 'root', 'port': 22},
            destination_host=destination_host,
        )

    def test_default_target_is_the_node_loopback(self):
        """不指定时转到节点回环：kube-lb 监听在那里，控制面走的就是这条路。"""
        self.assertEqual(self.build().destination_host, platform_relay.FORWARD_HOST)

    def test_self_target_is_the_taken_over_address(self):
        """etcd 要转到节点自己的内网地址上：回环那份监听讲的是明文 http，接不了 TLS。"""
        self.assertEqual(
            self.build(platform_relay.ROUTE_SELF).destination_host, '172.30.0.21',
        )

    def test_explicit_target_is_kept(self):
        """显式给的地址原样使用。"""
        self.assertEqual(self.build('10.0.0.5').destination_host, '10.0.0.5')


class EtcdRouteTest(RelayTestCase):
    """etcd 链路：端口、探测口，以及与 API 通道同址共存。"""

    def setUp(self):
        """准备一个干净的管理器与假的本机网络操作。

        参数：无。
        返回：无显式返回值。
        副作用：清空假通道登记表并替换模块级函数。
        """
        super().setUp()
        FakeRelay.instances = []
        self.manager = platform_relay.RelayManager()
        self.added = []
        self.removed = []
        for target, replacement in (
            ('add_local_address', self._add),
            ('remove_local_address', self._remove),
            ('address_owner', lambda address, device=platform_relay.ALIAS_DEVICE: ''),
            ('Relay', FakeRelay),
        ):
            patcher = mock.patch.object(platform_relay, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _add(self, address, device=platform_relay.ALIAS_DEVICE):
        """记下一次挂地址。

        参数：`address` 表示地址；`device` 表示接口名。
        返回：返回 True。
        副作用：记录调用。
        """
        self.added.append(address)
        return True

    def _remove(self, address, device=platform_relay.ALIAS_DEVICE):
        """记下一次摘地址。

        参数：`address` 表示地址；`device` 表示接口名。
        返回：返回 True。
        副作用：记录调用。
        """
        self.removed.append(address)
        return True

    def target(self, address='172.30.0.21'):
        """构造一个 etcd 目标。

        参数：`address` 表示集群内网地址。
        返回：返回目标字典。
        副作用：纯计算。
        """
        return {'address': address, 'host': '114.67.94.104', 'user': 'root', 'port': 22}

    def test_etcd_route_listens_on_2379_and_backs_up_the_api_channel(self):
        """单节点集群上 etcd 与控制面同址：2379 那条通道不能把 6443 那条顶掉。

        注册表若按地址记，第二条通道会覆盖第一条 —— 后果是备份跑完之后 API 通道没了，
        健康检查跟着失败。这一条就是钉住那次覆盖。
        """
        self.manager.ensure([self.target()], prober=lambda address, port: False)
        outcomes = self.manager.ensure(
            [self.target()], prober=lambda address, port: False, route=platform_relay.ETCD_ROUTE,
        )

        self.assertEqual(outcomes, {'172.30.0.21:2379': 'active'})
        self.assertEqual(len(FakeRelay.instances), 2)
        self.assertEqual(
            [(item.target['address'], item.port) for item in FakeRelay.instances],
            [('172.30.0.21', platform_relay.API_PORT),
             ('172.30.0.21', platform_relay.ETCD_CLIENT_PORT)],
        )
        self.assertEqual(sorted(self.manager.active()), ['172.30.0.21', '172.30.0.21'])

    def test_etcd_route_does_not_re_add_an_address_it_already_holds(self):
        """地址已经因为 API 通道挂上去了，再挂一次会去问归属 —— 那会把通道漏建。"""
        self.manager.ensure([self.target()], prober=lambda address, port: False)
        with mock.patch.object(platform_relay, 'address_owner', return_value='lo'):
            outcomes = self.manager.ensure(
                [self.target()], prober=lambda address, port: False, route=platform_relay.ETCD_ROUTE,
            )
        self.assertEqual(outcomes, {'172.30.0.21:2379': 'active'})

    def test_etcd_route_probes_the_etcd_port_not_ssh(self):
        """平台机本来就能拨到 2379 时不接管：那说明它就在集群网段里，隧道只会添乱。"""
        seen = []

        def prober(address, port):
            """记录探测的端口并回答「通路是好的」。

            参数：`address` 表示地址；`port` 表示端口。
            返回：返回 True。
            副作用：记录调用。
            """
            seen.append((address, port))
            return True

        outcomes = self.manager.ensure([self.target()], prober=prober, route=platform_relay.ETCD_ROUTE)
        self.assertEqual(outcomes, {'172.30.0.21:2379': 'reachable'})
        self.assertEqual(seen, [('172.30.0.21', platform_relay.ETCD_CLIENT_PORT)])
        self.assertEqual(self.added, [])

    def test_api_route_still_probes_the_ssh_port(self):
        """控制面那条链路的口径不变：探 SSH 端口，那是「平台机与这张网卡通不通」的代理。"""
        seen = []
        self.manager.ensure(
            [self.target()], route=platform_relay.API_ROUTE,
            prober=lambda address, port: seen.append((address, port)) or True,
        )
        self.assertEqual(seen, [('172.30.0.21', 22)])

    def test_teardown_stops_both_channels_on_the_same_address(self):
        """同一个地址上的两条通道都要断：留下任何一条，收尾就等于没做。

        单节点集群里 etcd 与控制面同址，这是常态；漏掉一条的表现是任务结束后平台机仍在
        监听那个端口，或者地址没被摘掉 —— 后者更糟，本机从此会把集群地址当成自己的。
        """
        self.manager.ensure([self.target()], prober=lambda address, port: False)
        self.manager.ensure(
            [self.target()], prober=lambda address, port: False, route=platform_relay.ETCD_ROUTE,
        )
        with mock.patch.object(platform_relay, 'cleanup_stale_addresses', return_value=1) as cleanup:
            self.manager.teardown()
        self.assertEqual([item.stopped for item in FakeRelay.instances], [True, True])
        self.assertEqual(self.manager.active(), [])
        cleanup.assert_called_once_with()

    def test_failing_etcd_channel_takes_only_its_own_address_away(self):
        """2379 建不起来时不能把 API 通道还压着的地址摘掉。"""

        class Broken(FakeRelay):
            """起不来的假通道。"""

            def start(self):
                """抛出与真实实现一致的异常。

                参数：无。
                返回：无返回值。
                副作用：无。
                """
                raise platform_relay.RelayError('SSH 连不上')

        self.manager.ensure([self.target()], prober=lambda address, port: False)
        with mock.patch.object(platform_relay, 'Relay', Broken):
            outcomes = self.manager.ensure(
                [self.target()], prober=lambda address, port: False, route=platform_relay.ETCD_ROUTE,
            )
        self.assertTrue(outcomes['172.30.0.21:2379'].startswith('failed:'))
        self.assertEqual(self.removed, [])


class VipRouteTest(RelayTestCase):
    """VIP 链路：监听端口、探测口，以及与别的通道互不干扰。"""

    def setUp(self):
        """准备一个干净的管理器与假的本机网络操作。

        参数：无。
        返回：无显式返回值。
        副作用：清空假通道登记表并替换模块级函数。
        """
        super().setUp()
        FakeRelay.instances = []
        self.manager = platform_relay.RelayManager()
        self.added = []
        self.removed = []
        for target, replacement in (
            ('add_local_address', self._add),
            ('remove_local_address', self._remove),
            ('address_owner', lambda address, device=platform_relay.ALIAS_DEVICE: ''),
            ('Relay', FakeRelay),
        ):
            patcher = mock.patch.object(platform_relay, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _add(self, address, device=platform_relay.ALIAS_DEVICE):
        """记下一次挂地址。

        参数：`address` 表示地址；`device` 表示接口名。
        返回：返回 True。
        副作用：记录调用。
        """
        self.added.append(address)
        return True

    def _remove(self, address, device=platform_relay.ALIAS_DEVICE):
        """记下一次摘地址。

        参数：`address` 表示地址；`device` 表示接口名。
        返回：返回 True。
        副作用：记录调用。
        """
        self.removed.append(address)
        return True

    def vip_target(self, listen_port=None):
        """构造一个 VIP 目标。

        参数：`listen_port` 表示平台机上要监听的端口，None 表示不带这一项。
        返回：返回目标字典。
        副作用：纯计算。
        """
        target = {'address': '172.30.0.22', 'host': '116.198.237.81', 'user': 'root', 'port': 22}
        if listen_port:
            target['listen_port'] = listen_port
        return target

    def test_vip_channel_listens_on_the_port_from_the_inventory(self):
        """监听端口取目标自带的那一个，而不是链路定义里的 6443。"""
        outcomes = self.manager.ensure(
            [self.vip_target(8443)], prober=lambda address, port: False,
            route=platform_relay.VIP_ROUTE,
        )
        self.assertEqual(outcomes, {'172.30.0.22:8443': 'active'})
        self.assertEqual([item.port for item in FakeRelay.instances], [8443])

    def test_vip_route_probes_the_vip_port_not_ssh(self):
        """探的是 VIP:6443：平台机能拨通它时（它就在集群网段里）不必接管。"""
        seen = []
        self.manager.ensure(
            [self.vip_target()], route=platform_relay.VIP_ROUTE,
            prober=lambda address, port: seen.append((address, port)) or True,
        )
        self.assertEqual(seen, [('172.30.0.22', platform_relay.API_PORT)])

    def test_vip_channel_coexists_with_the_control_plane_channels(self):
        """VIP 与三台 master 的通道同时在册：地址不同，各占一个键。"""
        masters = [
            {'address': address, 'host': host, 'user': 'root', 'port': 22}
            for address, host in (('172.30.0.12', '116.198.238.74'),
                                  ('172.30.0.25', '116.198.238.75'),
                                  ('172.30.0.15', '116.198.238.76'))
        ]
        self.manager.ensure(masters, prober=lambda address, port: False)
        outcomes = self.manager.ensure(
            [self.vip_target()], prober=lambda address, port: False,
            route=platform_relay.VIP_ROUTE,
        )
        self.assertEqual(outcomes, {'172.30.0.22:6443': 'active'})
        self.assertEqual(
            [item.address for item in FakeRelay.instances],
            ['172.30.0.12', '172.30.0.25', '172.30.0.15', '172.30.0.22'],
        )

    def test_the_vip_is_forwarded_to_the_lb_machines_loopback(self):
        """转发目标是 LB 机器的回环：l4lb 监听在那里，「VIP → l4lb → apiserver」原样保留。

        落到某台 master 也能让那句验活返回 200，但那样两台 LB 全挂也算通过 ——
        这一步就白做了。
        """
        self.manager.ensure(
            [self.vip_target()], prober=lambda address, port: False,
            route=platform_relay.VIP_ROUTE,
        )
        self.assertEqual(FakeRelay.instances[0].destination_host, platform_relay.FORWARD_HOST)

    def test_ensure_lb_vip_reads_the_inventory_file(self):
        """端到端：从 inventory 文件里读 VIP 并接管。"""
        hosts = Path(self.temp.name) / 'hosts'
        hosts.write_text(
            '[ex_lb]\n'
            '172.30.0.16 ansible_host=116.198.237.81 LB_ROLE=master '
            'EX_APISERVER_VIP=172.30.0.22 EX_APISERVER_PORT=6443\n',
            encoding='utf-8',
        )
        with mock.patch.object(platform_relay, '_MANAGER', self.manager):
            outcomes = platform_relay.ensure_lb_vip(
                mock.MagicMock(), hosts, prober=lambda address, port: False,
            )
        self.assertEqual(outcomes, {'172.30.0.22:6443': 'active'})
        self.assertEqual(self.added, ['172.30.0.22'])
        self.assertEqual(FakeRelay.instances[0].target['host'], '116.198.237.81')

    def test_missing_inventory_file_is_not_an_error(self):
        """inventory 读不到时返回空字典：调用方按「没有通道」继续走，不额外拦一道。"""
        outcomes = platform_relay.ensure_lb_vip(
            mock.MagicMock(), Path(self.temp.name) / '不存在', prober=lambda address, port: False,
        )
        self.assertEqual(outcomes, {})
        self.assertEqual(self.added, [])


class RelayLoggingTest(RelayTestCase):
    """日志：建立、建不起来各说一句，且同一个地址只说一次。"""

    def setUp(self):
        """准备假上下文与一个总是建不起通道的管理器。

        参数：无。
        返回：无显式返回值。
        副作用：替换模块级管理器。
        """
        super().setUp()
        FakeRelay.instances = []
        self.context = mock.MagicMock()
        self.manager = platform_relay.RelayManager()
        patcher = mock.patch.object(platform_relay, '_MANAGER', self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def messages(self):
        """返回全部日志文本。

        参数：无。
        返回：返回 ``[(流, 文本)]``。
        副作用：纯计算。
        """
        return [(call.args[0], str(call.args[1])) for call in self.context.log.call_args_list]

    def test_active_channel_explains_why_the_address_was_taken_over(self):
        """日志要说清「平台机不通、已接管」，否则运维看到 lo 上多一个地址会以为中了病毒。"""
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(self.context, [{'address': '172.30.0.7', 'host': '116.198.251.203',
                                                  'user': 'root', 'port': 22}],
                                  prober=lambda address, port: False)
        streams = dict(self.messages())
        self.assertIn('system', streams)
        self.assertIn('172.30.0.7', streams['system'])
        self.assertIn('116.198.251.203', streams['system'])

    def test_failure_names_the_consequence_and_the_way_out(self):
        """建不起来时必须说清后果（04 会失败）与出路（让平台机与内网互通 / 给 root）。"""
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(self.context, [{'address': '172.30.0.7', 'host': '116.198.251.203',
                                                  'user': 'root', 'port': 22}],
                                  prober=lambda address, port: False)
        # 先让通道活着，再把它弄死，模拟 SSH 中途掉线后重建失败。
        FakeRelay.instances[0].alive_flag = False
        with mock.patch.object(platform_relay, 'add_local_address', side_effect=platform_relay.RelayError('没有 root 权限')), \
                mock.patch.object(platform_relay, 'remove_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(self.context, [{'address': '172.30.0.7', 'host': '116.198.251.203',
                                                  'user': 'root', 'port': 22}],
                                  prober=lambda address, port: False)
        streams = dict(self.messages())
        self.assertIn('stderr', streams)
        self.assertIn('API Server', streams['stderr'])
        self.assertIn('没有 root 权限', streams['stderr'])

    def test_the_same_conclusion_is_only_told_once(self):
        """每个 ezctl 阶段之前都会 rebuild 一遍，同一个结论在日志里只该出现一次。"""
        target = {'address': '172.30.0.7', 'host': '116.198.251.203', 'user': 'root', 'port': 22}
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            for _ in range(3):
                platform_relay.ensure(self.context, [target], prober=lambda address, port: False)
        self.assertEqual(self.context.log.call_count, 1)

    def etcd_target(self):
        """返回一个 etcd 成员目标。

        参数：无。
        返回：返回目标字典。
        副作用：纯计算。
        """
        return {'address': '172.30.0.21', 'host': '114.67.94.104', 'user': 'root', 'port': 22}

    def test_etcd_channel_says_it_is_the_backup_that_needs_it(self):
        """etcd 那条通道的日志不能复用 API 的措辞：运维要一眼知道是备份卡住、不是部署。"""
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(
                self.context, [self.etcd_target()], prober=lambda address, port: False,
                route=platform_relay.ETCD_ROUTE,
            )
        text = dict(self.messages())['system']
        self.assertIn('etcdctl', text)
        self.assertIn('94.backup.yml', text)
        self.assertIn('172.30.0.21:2379', text)
        # 转发目标是**节点自己的**内网地址，不是回环：回环那份监听讲的是明文 http。
        self.assertIn('114.67.94.104 的 172.30.0.21:2379', text)

    def test_etcd_failure_names_the_silent_hang(self):
        """建不起来时要直说「会安静地失败」：这正是 94 上卡三十多分钟、页面还显示执行中的成因。"""
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(
                self.context, [self.etcd_target()], prober=lambda address, port: False,
                route=platform_relay.ETCD_ROUTE,
            )
        FakeRelay.instances[0].alive_flag = False
        with mock.patch.object(platform_relay, 'add_local_address',
                              side_effect=platform_relay.RelayError('没有 root 权限')), \
                mock.patch.object(platform_relay, 'remove_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(
                self.context, [self.etcd_target()], prober=lambda address, port: False,
                route=platform_relay.ETCD_ROUTE,
            )
        text = dict(self.messages())['stderr']
        self.assertIn('etcd', text)
        self.assertIn('没有默认超时', text)
        self.assertIn('没有 root 权限', text)


    def vip_target(self):
        """返回一个 VIP 目标。

        参数：无。
        返回：返回目标字典。
        副作用：纯计算。
        """
        return {'address': '172.30.0.22', 'host': '116.198.237.81', 'user': 'root', 'port': 22,
                'listen_port': 6443}

    def test_vip_channel_says_which_stage_it_unblocks(self):
        """VIP 那条的措辞不能复用 04 的说法：卡住的是第 8 阶段收尾那句验活。"""
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(
                self.context, [self.vip_target()], prober=lambda address, port: False,
                route=platform_relay.VIP_ROUTE,
            )
        text = dict(self.messages())['system']
        self.assertIn('172.30.0.22:6443', text)
        self.assertIn('ex-lb', text)
        # 转发目标是 LB 机器的回环 6443：l4lb 就在那里，这一跳之外链路是完整走通的。
        self.assertIn('116.198.237.81 的 127.0.0.1:6443', text)

    def test_vip_failure_names_the_consequence_and_that_the_cluster_is_fine(self):
        """建不起来时要直说「第 8 阶段会超时失败，但集群本身是好的」—— 否则用户会去重装。"""
        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(
                self.context, [self.vip_target()], prober=lambda address, port: False,
                route=platform_relay.VIP_ROUTE,
            )
        FakeRelay.instances[0].alive_flag = False
        with mock.patch.object(platform_relay, 'add_local_address',
                              side_effect=platform_relay.RelayError('没有 root 权限')), \
                mock.patch.object(platform_relay, 'remove_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''):
            platform_relay.ensure(
                self.context, [self.vip_target()], prober=lambda address, port: False,
                route=platform_relay.VIP_ROUTE,
            )
        text = dict(self.messages())['stderr']
        self.assertIn('VIP', text)
        self.assertIn('ex-lb', text)
        # 不能串到控制面那条的措辞上：那会让运维去查 04.kube-master。
        self.assertNotIn('04.kube-master', text)
        self.assertIn('urlopen error timed out', text)
        self.assertIn('已经装好了', text)
        self.assertIn('没有 root 权限', text)


class RelayThreadingTest(RelayTestCase):
    """并发保护：一次性把多个地址接进来时，通道不会互相覆盖。"""

    def test_each_address_gets_its_own_channel(self):
        """多台控制面各自一条通道：HA 集群里每台 master 的 kubeconfig 都指向自己。"""
        manager = platform_relay.RelayManager()
        started = []
        lock = threading.Lock()

        def fake_relay(target, key_path=None, port=platform_relay.API_PORT, destination_port=None,
                       destination_host=None):
            item = FakeRelay(target)
            with lock:
                started.append(item)
            return item

        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''), \
                mock.patch.object(platform_relay, 'Relay', fake_relay):
            outcomes = manager.ensure(
                [{'address': '172.30.0.7', 'host': '1.1.1.1', 'user': 'root', 'port': 22},
                 {'address': '172.30.0.8', 'host': '1.1.1.2', 'user': 'root', 'port': 22}],
                prober=lambda address, port: False,
            )
        self.assertEqual(outcomes, {'172.30.0.7:6443': 'active', '172.30.0.8:6443': 'active'})
        self.assertEqual(sorted(manager.active()), ['172.30.0.7', '172.30.0.8'])
        self.assertEqual(len(started), 2)


class InventoryEntryTest(RelayTestCase):
    """``ensure_from_inventory``：以 hosts 文件为准，缺文件时不报错。"""

    def setUp(self):
        """每条用例换一个干净的管理器。

        参数：无。
        返回：无显式返回值。
        副作用：替换模块级管理器。

        ``_MANAGER`` 是进程级单例，兄弟用例留下的通道会被下一条看见：上一轮那条 etcd 通道
        会让 172.30.0.7 变成「平台自己压着的地址」，这一条于是不再去探它。
        """
        super().setUp()
        patcher = mock.patch.object(platform_relay, '_MANAGER', platform_relay.RelayManager())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_is_not_an_error(self):
        """任务早期 hosts 还不存在，这不是异常路径。"""
        self.assertEqual(
            platform_relay.ensure_from_inventory(mock.MagicMock(), Path(self.temp.name) / 'nope'),
            {},
        )

    def test_targets_come_from_the_file(self):
        """扩容与升级用的节点地址只存在于 hosts 文件里，所以判断依据取文件。"""
        hosts = Path(self.temp.name) / 'hosts'
        hosts.write_text(HOSTS_TEXT, encoding='utf-8')
        seen = {}

        def prober(address, port):
            """记录探测调用并一律回答「不通」。

            参数：`address` 表示地址；`port` 表示端口。
            返回：返回 False。
            副作用：记录调用。
            """
            seen[address] = port
            return False

        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''), \
                mock.patch.object(platform_relay, 'Relay', FakeRelay):
            outcomes = platform_relay.ensure_from_inventory(mock.MagicMock(), hosts, prober=prober)
        self.assertEqual(sorted(outcomes), ['172.30.0.7:6443', '172.30.0.8:6443'])
        self.assertEqual(seen, {'172.30.0.7': 22, '172.30.0.8': 2222})

    def test_etcd_route_reads_the_etcd_section_and_probes_2379(self):
        """备份的场景里要的是 etcd 端口，不是 SSH 端口 —— 探错了就会白白接管一个地址。"""
        hosts = Path(self.temp.name) / 'hosts'
        hosts.write_text(HOSTS_TEXT, encoding='utf-8')
        seen = {}
        recorded = {}

        def prober(address, port):
            """记录探测调用并一律回答「不通」。

            参数：`address` 表示地址；`port` 表示端口。
            返回：返回 False。
            副作用：记录调用。
            """
            seen[address] = port
            return False

        def fake_relay(target, key_path=None, port=platform_relay.API_PORT, destination_port=None,
                       destination_host=None):
            """记录通道参数并返回假通道。

            参数：与真实通道一致。
            返回：返回假通道。
            副作用：记录调用。
            """
            recorded['port'] = port
            recorded['destination_host'] = destination_host
            return FakeRelay(target, port=port, destination_host=destination_host)

        with mock.patch.object(platform_relay, 'add_local_address', return_value=True), \
                mock.patch.object(platform_relay, 'address_owner', return_value=''), \
                mock.patch.object(platform_relay, 'Relay', fake_relay):
            outcomes = platform_relay.ensure_from_inventory(
                mock.MagicMock(), hosts, prober=prober, route=platform_relay.ETCD_ROUTE,
            )
        self.assertEqual(sorted(outcomes), ['172.30.0.7:2379'])
        self.assertEqual(seen, {'172.30.0.7': platform_relay.ETCD_CLIENT_PORT})
        self.assertEqual(recorded['port'], platform_relay.ETCD_CLIENT_PORT)
        self.assertEqual(recorded['destination_host'], platform_relay.ROUTE_SELF)
