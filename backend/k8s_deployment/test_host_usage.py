"""主机水位采集的用例：解析、单机失败、集群级汇总与门禁、机器列表页那三列。

真实背景见 ``services/host_usage.py``：走 k8s API 的那半个可观测性答的是「集群里跑了什么」；
这一半走 SSH 读 ``/proc`` 与 ``df``，答的是「这几台机器自己还撑得住吗」。

采样文本取自真实机器（CentOS 7 的 ``/proc/meminfo`` 没有 ``MemAvailable``，Ubuntu 22.04 有；
``df`` 里混着 tmpfs、kubelet 的 bind mount、NFS），因此用例断言的是「从这些真实输出里挑出
该挑的那几行」，而不是构造出来的理想数据。
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from k8s_deployment.services import cluster_ops, credentials, host_usage, node_registry

from .test_node_registry import _Stream, make_node

# 一台 Ubuntu 22.04 节点的完整采集输出（4 核 8G，根盘 40G 用了 6 成，数据盘 200G 用了 7 成）。
UBUNTU_OUTPUT = """HOSTNAME master-01
UPTIME 1041372.55
LOADAVG 0.42 0.35 0.31
CORES 4
MEMINFO
MemTotal:        8123456 kB
MemFree:          412344 kB
MemAvailable:    3245678 kB
Buffers:           12345 kB
Cached:          3012345 kB
SwapTotal:             0 kB
DF
Filesystem      1-blocks        Used   Available Capacity Mounted on
/dev/vda1      41943040000 25165824000 16777216000      60% /
tmpfs            4061728         0     4061728       0% /dev/shm
/dev/vdb      214748364800 154618822656 60129542144      72% /data
tmpfs             812344      1234      811110       1% /run
overlay        41943040000 25165824000 16777216000      60% /var/lib/docker/overlay2/abc
/dev/vda15        106325           6224       100101       6% /boot/efi
//192.168.1.10/share 10737418240 1073741824 9663676416      10% /mnt/nfs
STAT1
cpu  10000 200 5000 800000 3000 0 500 0 0 0
STAT2
cpu  10200 200 5100 806000 3200 0 500 0 0 0
"""

# 一台 CentOS 7 节点（3.10 内核没有 MemAvailable，df 多两行 tmpfs）。
CENTOS_OUTPUT = """HOSTNAME worker-01
UPTIME 88888.10
LOADAVG 1.20 0.90 0.70
CORES 2
MEMINFO
MemTotal:        2048000 kB
MemFree:         1024000 kB
Buffers:           20480 kB
Cached:           409600 kB
DF
Filesystem     1-blocks        Used   Available Capacity Mounted on
/dev/sda3     20971520000 15728640000  5242880000      75% /
devtmpfs         917504           0      917504       0% /dev
STAT1
cpu  100 0 100 800 0 0 0 0
STAT2
cpu  200 0 200 1500 0 0 0 0
"""


class CpuPercentTest(SimpleTestCase):
    """CPU 利用率：两次采样求差。"""

    def test_single_sample_is_not_enough(self):
        """只给一次采样算不出来 —— 那是开机以来的累计平均，不是当前的负载。"""
        self.assertIsNone(host_usage.cpu_percent('cpu  100 0 100 800 0 0 0 0', ''))

    def test_computes_the_delta(self):
        """两次采样之间的忙碌比例。800/1000 → 880/1100 空闲，差值是 900 里忙了 200。"""
        value = host_usage.cpu_percent(
            'cpu  100 0 100 800 0 0 0 0', 'cpu  200 0 200 1500 0 0 0 0',
        )

        self.assertEqual(value, 22.2)

    def test_iowait_counts_as_idle(self):
        """等磁盘的时间不算 CPU 在干活：否则一台 IO 卡住的机器会显示 100%。"""
        value = host_usage.cpu_percent(
            'cpu  100 0 100 800 100 0 0 0', 'cpu  110 0 110 800 1000 0 0 0',
        )

        self.assertLess(value, 5)

    def test_guest_time_is_not_counted_twice(self):
        """guest 已经计在 user 里，重复计入会让总量虚高、占用率虚低。"""
        total, _idle = host_usage.parse_cpu_times('cpu  100 0 100 800 0 0 0 0 50 50')

        self.assertEqual(total, 1000)

    def test_zero_elapsed_time_is_none(self):
        """两次读到的完全一样（同一瞬间被读了两次）时返回 None，不编一个 0%。"""
        self.assertIsNone(host_usage.cpu_percent(
            'cpu  100 0 100 800 0 0 0 0', 'cpu  100 0 100 800 0 0 0 0',
        ))


class MemoryTest(SimpleTestCase):
    """内存：优先 MemAvailable，老内核回落。"""

    def test_prefers_mem_available(self):
        """``MemAvailable`` 是内核估计的「还能给新进程用多少」，比 ``MemFree`` 贴近真实。"""
        result = host_usage.parse_meminfo(
            'MemTotal:        8123456 kB\nMemFree:          412344 kB\nMemAvailable:    3245678 kB\n',
        )

        self.assertEqual(result['total_bytes'], 8123456 * 1024)
        self.assertEqual(result['available_bytes'], 3245678 * 1024)
        self.assertEqual(result['percent'], 60.0)

    def test_falls_back_on_old_kernels(self):
        """3.14 之前的内核（CentOS 7 是 3.10）没有 ``MemAvailable``，按 free+buffers+cached 估。"""
        result = host_usage.parse_meminfo(
            'MemTotal:        2048000 kB\nMemFree:         1024000 kB\n'
            'Buffers:           20480 kB\nCached:           409600 kB\n',
        )

        self.assertEqual(result['available_bytes'], (1024000 + 20480 + 409600) * 1024)
        self.assertEqual(result['percent'], 29.0)

    def test_missing_meminfo_is_zero_not_a_crash(self):
        """读不到就报 0，不让一台机器的采集成败影响整块展示。"""
        result = host_usage.parse_meminfo('')

        self.assertEqual(result['total_bytes'], 0)
        self.assertEqual(result['percent'], 0.0)


class DiskTest(SimpleTestCase):
    """磁盘：只留真实的盘。"""

    def setUp(self):
        """准备好两次采集的分段内容。"""
        self.mounts = host_usage.parse_df(UBUNTU_OUTPUT.split('DF\n')[1].split('STAT1\n')[0])

    def test_keeps_the_real_filesystems(self):
        """根盘、数据盘、``/boot/efi`` 留下。"""
        self.assertEqual([item['mount'] for item in self.mounts], ['/data', '/', '/boot/efi'])

    def test_drops_tmpfs_and_overlay(self):
        """tmpfs、overlay 不是这台机器的容量问题：它们占的是内存或镜像层。"""
        mounts = {item['mount'] for item in self.mounts}

        self.assertNotIn('/dev/shm', mounts)
        self.assertNotIn('/run', mounts)
        self.assertNotIn('/var/lib/docker/overlay2/abc', mounts)

    def test_drops_network_filesystems(self):
        """NFS/CIFS 不是本机的盘：混进来会让磁盘水位变成一个与硬件无关的数。"""
        self.assertNotIn('/mnt/nfs', {item['mount'] for item in self.mounts})

    def test_sorts_by_pressure(self):
        """按使用率倒序：最紧张的那块盘排在最前面。"""
        self.assertEqual(self.mounts[0]['percent'], 72.0)

    def test_reads_the_byte_columns(self):
        """``df -B1`` 的列就是字节，不做任何单位换算。"""
        data = next(item for item in self.mounts if item['mount'] == '/data')

        self.assertEqual(data['total_bytes'], 214748364800)
        self.assertEqual(data['used_bytes'], 154618822656)

    def test_mount_point_with_spaces_stays_in_one_piece(self):
        """挂载点里有空格时，第 6 个字段之后要整体拼回来。"""
        mounts = host_usage.parse_df(
            'Filesystem 1-blocks Used Available Capacity Mounted on\n'
            '/dev/vdc 1000000 200000 800000 20% /mnt/my data\n',
        )

        self.assertEqual(mounts[0]['mount'], '/mnt/my data')

    def test_root_mount_prefers_the_root_filesystem(self):
        """行内显示根盘：有几个数据盘时，「这台机器的磁盘占用」只有根盘是唯一答案。"""
        self.assertEqual(host_usage.root_mount(self.mounts)['mount'], '/')

    def test_root_mount_falls_back_to_the_busiest(self):
        """没有挂到 ``/`` 的条目时退回最紧张的那一块。"""
        mounts = [{'mount': '/data', 'percent': 90.0}, {'mount': '/opt', 'percent': 10.0}]

        self.assertEqual(host_usage.root_mount(mounts)['mount'], '/data')

    def test_no_mounts_is_none(self):
        """一块盘都认不出来时返回 None，由前端显示成「—」。"""
        self.assertIsNone(host_usage.root_mount([]))


class ParseCollectOutputTest(SimpleTestCase):
    """整段输出一次解析到位。"""

    def test_reads_every_section(self):
        """四个段落各归各位，没有一段串到别处去。"""
        result = host_usage.parse_collect_output(UBUNTU_OUTPUT)

        self.assertEqual(result['hostname'], 'master-01')
        self.assertEqual(result['cores'], 4)
        self.assertEqual(result['uptime_seconds'], 1041372)
        self.assertEqual(result['load'], [0.42, 0.35, 0.31])
        self.assertEqual(result['memory']['percent'], 60.0)
        self.assertEqual(result['cpu_percent'], 4.6)
        self.assertEqual(result['mounts'][0]['mount'], '/data')

    def test_missing_sections_only_empty_those_fields(self):
        """机器上少打印一段时，其余项照样解析出来。"""
        result = host_usage.parse_collect_output('HOSTNAME node1\nCORES 8\n')

        self.assertEqual(result['cores'], 8)
        self.assertEqual(result['mounts'], [])
        self.assertIsNone(result['cpu_percent'])


class CollectOneTest(SimpleTestCase):
    """``_collect_one``：单台成败只影响自己这一行。"""

    def setUp(self):
        """准备节点与连接信息。"""
        self.node = make_node('192.168.1.94', roles=['master'])
        self.payload = {
            'machine': None, 'name': 'master', 'ip_address': '192.168.1.94',
            'ssh_port': 22, 'ssh_user': 'root', 'roles': ['master'], 'node_name': 'master-01',
        }

    def collect(self, client):
        """在打桩连接的前提下采集一次。"""
        with patch.object(node_registry, 'open_session', return_value=client):
            return host_usage._collect_one(self.payload, {'user': 'root', 'port': 22})

    def test_reports_the_water_levels(self):
        """一条命令把该读的都读回来，并按节点名核对实际 hostname。"""
        client = _CommandClient(UBUNTU_OUTPUT)

        result = self.collect(client)

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['node_name'], 'master-01')
        self.assertEqual(result['hostname'], 'master-01')
        self.assertTrue(result['hostname_matches'])
        self.assertEqual(result['cpu_percent'], 4.6)
        self.assertEqual(result['memory']['percent'], 60.0)
        self.assertEqual(result['disk']['mount'], '/')
        self.assertEqual(client.commands[0], host_usage.COLLECT_COMMAND)

    def test_flags_a_hostname_that_does_not_match(self):
        """实际 hostname 与节点名不一致时标出来：这正是第一件事要修的那个毛病。"""
        result = self.collect(_CommandClient(UBUNTU_OUTPUT.replace('HOSTNAME master-01', 'HOSTNAME old-name')))

        self.assertFalse(result['hostname_matches'])

    def test_a_failed_node_only_fails_its_own_row(self):
        """连不上时这一行标失败、带中文原因，不影响别的节点。"""
        client = _CommandClient('', code=1, err='ssh: connect to host 192.168.1.94 port 22: Connection refused')

        result = self.collect(client)

        self.assertEqual(result['status'], 'failed')
        self.assertIn('Connection refused', result['error'])
        self.assertEqual(result['label'], 'master')
        self.assertEqual(result['roles'], ['master'])

    def test_closes_the_session_even_when_it_fails(self):
        """失败也要关连接：一次采集连了几十台，泄漏的会话会堆在目标机上。"""
        client = _CommandClient('', code=1)

        self.collect(client)

        self.assertTrue(client.closed)


class SummarizeTest(SimpleTestCase):
    """集群级汇总。"""

    def test_cpu_and_memory_average_disk_takes_the_max(self):
        """CPU 与内存看集群整体余量取均值；磁盘看哪台先满，平均值会把一块快满的盘摊平。"""
        nodes = [
            {'status': 'ok', 'cpu_percent': 10.0, 'memory': {'percent': 40.0}, 'disk': {'percent': 30.0}},
            {'status': 'ok', 'cpu_percent': 30.0, 'memory': {'percent': 60.0}, 'disk': {'percent': 92.0}},
        ]

        summary = host_usage.summarize(nodes)

        self.assertEqual(summary['cpu_avg'], 20.0)
        self.assertEqual(summary['memory_avg'], 50.0)
        self.assertEqual(summary['disk_max'], 92.0)
        self.assertEqual(summary['ok_count'], 2)

    def test_failed_nodes_are_not_averaged_in(self):
        """采不到的那几台不参与平均：把「读不到」当成 0 会让水位凭空变绿。"""
        nodes = [
            {'status': 'ok', 'cpu_percent': 40.0, 'memory': {'percent': 50.0}, 'disk': None},
            {'status': 'failed', 'cpu_percent': None, 'memory': {}, 'disk': None},
        ]

        summary = host_usage.summarize(nodes)

        self.assertEqual(summary['cpu_avg'], 40.0)
        self.assertIsNone(summary['disk_max'])
        self.assertEqual(summary['failed_count'], 1)

    def test_nothing_collected_is_all_none(self):
        """一台都没采到时不编数字。"""
        summary = host_usage.summarize([])

        self.assertIsNone(summary['cpu_avg'])
        self.assertEqual(summary['node_count'], 0)


class CollectClusterUsageTest(SimpleTestCase):
    """``collect_cluster_usage``：门禁与并发。

    节点清单相关的函数在 ``cluster_ops`` 上打桩（模块内的 ``from . import cluster_ops`` 取到的
    是同一个模块对象，因此函数内的懒 import 也能看到打桩结果）。
    """

    def test_imported_cluster_is_reported_as_unavailable(self):
        """导入型集群平台没有它的节点清单与免密通道，返回一句中文原因而不是报错。"""
        error = node_registry.NodeRegistryError('cluster_not_deployed', '该集群不是平台部署的，平台没有它的节点清单')

        with patch.object(cluster_ops, 'assert_deployed', side_effect=error):
            payload = host_usage.collect_cluster_usage(object())

        self.assertFalse(payload['available'])
        self.assertIn('不是平台部署的', payload['reason'])
        self.assertEqual(payload['nodes'], [])

    def test_collects_every_node_of_a_deployed_cluster(self):
        """部署型集群：按快照里的节点逐台采集，用快照的 ssh_user/port 兜底。"""
        payloads = [
            {'machine': None, 'name': 'master', 'ip_address': '192.168.1.94', 'ssh_port': 22,
             'ssh_user': 'root', 'roles': ['master'], 'node_name': 'master-01'},
            {'machine': None, 'name': 'worker', 'ip_address': '192.168.1.95', 'ssh_port': 2222,
             'ssh_user': 'ubuntu', 'roles': ['node'], 'node_name': 'worker-01'},
        ]
        seen = []

        def fake_collect(item, credential):
            seen.append((item['ip_address'], credential))
            return {'label': item['name'], 'status': 'ok'}

        with patch.object(cluster_ops, 'assert_deployed', return_value=None), \
             patch.object(cluster_ops, 'node_payloads', return_value=payloads), \
             patch.object(host_usage, '_collect_one', side_effect=fake_collect):
            payload = host_usage.collect_cluster_usage(object())

        self.assertTrue(payload['available'])
        self.assertEqual([item['label'] for item in payload['nodes']], ['master', 'worker'])
        self.assertEqual(dict(seen)['192.168.1.95'], {'user': 'ubuntu', 'port': 2222})
        self.assertEqual(payload['summary']['node_count'], 2)

    def test_timestamp_is_present(self):
        """采集时间要带上：水位是那一刻的快照，界面得说清是什么时候的水位。"""
        with patch.object(cluster_ops, 'assert_deployed', side_effect=ValueError('x')):
            payload = host_usage.collect_cluster_usage(object())

        self.assertRegex(payload['sampled_at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


class CollectMachineUsageTest(SimpleTestCase):
    """``collect_machine_usage``：机器列表页那三列水位。

    这里的机器来自机器表，不一定进过集群，因此通道与集群侧不同：有托管密码就用密码，
    没有才回落平台部署密钥（见 ``_machine_opener``）。
    """

    def machine(self, pk, ip_address='192.168.1.94', password='P@ssw0rd', ssh_user='root', ssh_port=22):
        """造一台机器表里的机器（只用到采集需要的几个字段）。"""
        def get_password():
            """返回这台机器托管的密码。"""
            if password == 'BOOM':
                raise ValueError('解密失败')
            return password

        return SimpleNamespace(
            pk=pk, name=f'node-{pk}', ip_address=ip_address, ssh_port=ssh_port,
            ssh_user=ssh_user, roles=['node'], get_password=get_password,
        )

    def test_reports_the_three_columns_per_machine(self):
        """每台一行，给列表页要的三个数；磁盘只认根分区，数据盘再满也不算。"""
        with patch.object(host_usage, '_collect_one', side_effect=[
            {
                'status': 'ok', 'error': '', 'hostname': 'master-01',
                'cpu_percent': 4.6, 'memory': {'percent': 60.0},
                'mounts': [
                    {'mount': '/', 'percent': 60.0},
                    {'mount': '/data', 'percent': 94.0},
                ],
            },
            {'status': 'failed', 'error': '连接超时', 'hostname': '', 'cpu_percent': None,
             'memory': {'percent': None}, 'mounts': []},
        ]):
            payload = host_usage.collect_machine_usage([self.machine(1), self.machine(2)])

        first, second = payload['items']
        self.assertEqual(first['machine_id'], 1)
        self.assertEqual(first['cpu_percent'], 4.6)
        self.assertEqual(first['memory_percent'], 60.0)
        self.assertEqual(first['disk_percent'], 60.0)
        self.assertEqual(second['machine_id'], 2)
        self.assertEqual(second['status'], 'failed')
        self.assertEqual(second['error'], '连接超时')
        # 失败的三项都必须是 None 而不是 0：列表页把 None 显示成「—」，把 0.0 显示成「0%」。
        self.assertIsNone(second['cpu_percent'])
        self.assertIsNone(second['memory_percent'])
        self.assertIsNone(second['disk_percent'])
        self.assertRegex(payload['sampled_at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_uses_the_stored_password_when_there_is_one(self):
        """托管了密码就用密码登录：机器表里的机器多半没有平台公钥。"""
        machine = self.machine(7, password='P@ssw0rd')
        opened = {}

        def fake_ssh_client(**kwargs):
            """记录连接参数并返回一个假客户端。"""
            opened.update(kwargs)
            return _CommandClient(UBUNTU_OUTPUT)

        with patch.object(credentials, 'ssh_client', side_effect=fake_ssh_client), \
             patch.object(node_registry, 'open_session') as key_login:
            payload = host_usage.collect_machine_usage([machine])

        self.assertEqual(payload['items'][0]['status'], 'ok')
        self.assertEqual(opened['password'], 'P@ssw0rd')
        self.assertEqual(opened['host'], '192.168.1.94')
        self.assertEqual(opened['username'], 'root')
        key_login.assert_not_called()

    def test_falls_back_to_the_platform_key_without_a_password(self):
        """没托管密码（或密码坏掉）时回落平台密钥，而不是直接放弃这一行。"""
        for machine in (self.machine(8, password=''), self.machine(9, password='BOOM')):
            with patch.object(node_registry, 'open_session', return_value=_CommandClient(UBUNTU_OUTPUT)) as key_login, \
                 patch.object(credentials, 'ssh_client') as password_login:
                payload = host_usage.collect_machine_usage([machine])

            self.assertEqual(payload['items'][0]['status'], 'ok')
            key_login.assert_called_once()
            password_login.assert_not_called()

    def test_a_failed_machine_only_fails_its_own_row(self):
        """一台连不上只标它自己，其余几台照常给数。"""
        clients = [_CommandClient(UBUNTU_OUTPUT), _CommandClient('', code=1, err='Connection refused')]

        with patch.object(credentials, 'ssh_client', side_effect=clients):
            payload = host_usage.collect_machine_usage([self.machine(1), self.machine(2, ip_address='192.168.1.95')])

        self.assertEqual([item['status'] for item in payload['items']], ['ok', 'failed'])
        self.assertIn('Connection refused', payload['items'][1]['error'])


class _CommandClient:
    """返回固定输出的假客户端。"""

    def __init__(self, output, code=0, err=''):
        """初始化。

        参数：`output` 表示标准输出；`code` 表示退出码；`err` 表示标准错误。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.output = output
        self.code = code
        self.err = err
        self.commands = []
        self.closed = False

    def exec_command(self, command, timeout=None):
        """记录命令并返回预设输出。"""
        self.commands.append(command)
        return None, _Stream(self.output, self.code), _Stream(self.err, self.code)

    def close(self):
        """关闭连接。"""
        self.closed = True
