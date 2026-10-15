"""k8s_deployment 部署前预检模块的单元测试。

测试里所有 SSH 都被替换成假实现（``_create_ssh_client`` 被打桩），
按命令文本返回预设输出，绝不在测试中连接真实机器。
"""

import json
import sys
import time
import types
from unittest import mock

from django.test import TestCase

from k8s_cluster.models import K8sCluster
from k8s_deployment.models import K8sDeployment, K8sDeploymentNode
from k8s_deployment.services import preflight, stale_cleanup
from ops.models import Organization

# 一份典型的 Rocky 实机系统信息：带引号、带注释，VERSION_ID 是 8.8 而配置中心存的是 8。
OS_RELEASE_ROCKY_8 = (
    'NAME="Rocky Linux"\n'
    '# 这是一行注释，解析时应被忽略\n'
    'VERSION="8.8 (Green Obsidian)"\n'
    'ID="rocky"\n'
    'ID_LIKE="rhel centos fedora"\n'
    'VERSION_ID="8.8"\n'
    'PRETTY_NAME="Rocky Linux 8.8 (Green Obsidian)"\n'
)

OS_RELEASE_UBUNTU_22 = (
    'NAME="Ubuntu"\n'
    'VERSION="22.04.3 LTS (Jammy Jellyfish)"\n'
    'ID=ubuntu\n'
    'VERSION_ID="22.04"\n'
    'PRETTY_NAME="Ubuntu 22.04.3 LTS"\n'
)

DF_ROOT = 'Filesystem     1024-blocks     Used Available Capacity Mounted on\n/dev/sda1        104744960 12345678  87000000      13% /\n'
DF_INODE = 'Filesystem       Inodes  IUsed    IFree IUse% Mounted on\n/dev/sda1       6553600  50000  6503600    1% /\n'
SS_IDLE = 'State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nLISTEN 0      128          0.0.0.0:22        0.0.0.0:*\n'
SS_PORT_OCCUPIED = SS_IDLE + 'LISTEN 0      128          0.0.0.0:6443      0.0.0.0:*\n'
IP_ADDR = '2: eth0    inet 192.168.1.94/24 brd 192.168.1.255 scope global eth0\\       valid_lft forever preferred_lft forever\n'
# 时间服务探测的正常输出：chrony 在跑且 leap status 为 Normal。
TIMESYNC_OK = 'CHRONY:yes\nLEAP:Normal\nCHRONYD:active\nTIMESYNCD:inactive\nNTPD:inactive\nSYNCED:1\n'
# etcd 仲裁探测的正常输出：3 个成员全部健康，本机端点也健康。
ETCD_QUORUM_OK = 'HEALTHY:3\nUNHEALTHY:0\nMEMBERS:3\nSELF:healthy\n'


def stale_probe_output(present=None, targets=None):
    """构造「上一次安装的残留」探针的输出。

    参数：`present` 为 ``{路径: 条目数}``，只列有残留的路径，其余按不存在报；
        `targets` 为探针要看的路径序列，省略即默认的三处。
    返回：返回探针命令会打印的整段文本。
    副作用：纯计算，不访问任何机器。
    """
    present = present or {}
    paths = targets or (
        f'{preflight.DEFAULT_ETCD_DATA_DIR}/member',
        f'{preflight.ETCD_SSL_DIR}/ca.pem',
        preflight.KEEPALIVED_CONF,
    )
    lines = ['__STALE_PROBE_BEGIN__']
    for path in paths:
        if path in present:
            kind = 'dir' if path.endswith('/member') else 'file'
            lines.append(f'PRESENT:{path}:{kind}:{present[path]}')
        else:
            lines.append(f'ABSENT:{path}')
    lines.append('__STALE_PROBE_END__')
    return '\n'.join(lines) + '\n'


# 残留探针在「机器干净」时的输出：三处痕迹都没有。
STALE_PROBE_CLEAN = stale_probe_output()


def classify_command(command):
    """把远程命令归类成测试里的输出类别。

    参数：`command` 表示预检将要执行的命令文本。
    返回：返回类别名，未识别的命令返回 other。
    副作用：纯计算，不修改任何状态。
    """
    text = str(command or '').strip()
    if text == 'id -u':
        return 'id'
    if text.startswith('sudo -n true'):
        return 'sudo'
    if text.startswith('cat /etc/os-release'):
        return 'os_release'
    # 磁盘探测会先向上找存在的父目录再 df，命令以 d=' 开头。
    if text.startswith("d='"):
        return 'df_inode' if '-Pi' in text else 'df'
    if text.startswith('df -Pi'):
        return 'df_inode'
    if text.startswith('df -P'):
        return 'df'
    # nftables 前提探测：命令里同时含 uname -r 与 nft --version，靠输出标记认出来。
    if 'NFT_PROBE' in text:
        return 'nft'
    # 残留探针的命令里没有别处会出现的关键字，同样靠标记认。
    if '__STALE_PROBE_BEGIN__' in text:
        return 'stale'
    if 'ip_vs' in text:
        return 'ipvs'
    if text.startswith('ss -lnt'):
        return 'ports'
    if text.startswith('date -u +%s'):
        return 'clock'
    if 'CHRONY:' in text:
        return 'timesync'
    if 'TCP_OK' in text or '/dev/tcp/' in text:
        return 'tcp'
    # etcd 仲裁探测的命令里同时含 member list 与 endpoint health，靠 SSL 变量名识别。
    if 'etcdctl' in text and 'ETCDCTL_MISSING' in text:
        return 'etcd_quorum'
    if 'IFACE_' in text:
        return 'link'
    if text.startswith('ip -o -f inet addr show'):
        return 'addr'
    return 'other'


def make_responder(clock_offset=0, **overrides):
    """构造一个按命令类别返回预设输出的 responder。

    参数：`clock_offset` 表示节点时间相对平台机的偏移秒数；
        `overrides` 用于覆盖默认输出，键为命令类别、值为 ``(标准输出, 标准错误)``。
    返回：返回 ``responder(command) -> (标准输出, 标准错误)`` 函数。
    副作用：纯计算，不访问网络。
    """
    table = {
        'id': ('0\n', ''),
        'sudo': ('SUDO_NO\n', ''),
        'os_release': (OS_RELEASE_ROCKY_8, ''),
        'df': (DF_ROOT, ''),
        'df_inode': (DF_INODE, ''),
        'ipvs': ('MODULES_LOADED\n', ''),
        # 默认给一台满足 nftables 前提的机器（内核 6.1、nft 1.0.9），要造不达标的场景再覆盖。
        'nft': ('NFT_PROBE kernel=6.1.0 nft=1.0.9\n', ''),
        'ports': (SS_IDLE, ''),
        'link': ('IFACE_UP\n', ''),
        'addr': (IP_ADDR, ''),
        'timesync': (TIMESYNC_OK, ''),
        'tcp': ('TCP_OK\n', ''),
        'etcd_quorum': (ETCD_QUORUM_OK, ''),
        # 默认给一台没有上一次安装痕迹的机器，要造残留场景再覆盖。
        'stale': (STALE_PROBE_CLEAN, ''),
    }
    table.update(overrides)

    def respond(command):
        """按命令类别返回预设输出。

        参数：`command` 表示命令文本。
        返回：返回 ``(标准输出, 标准错误)``。
        副作用：纯计算。
        """
        kind = classify_command(command)
        if kind == 'clock':
            return (f'{int(time.time()) + clock_offset}\n', '')
        return table.get(kind, ('', ''))

    return respond


class FakeSSHClient:
    """预检测试用的假 SSH 客户端，按主机名选择预设响应。"""

    def __init__(self, responses=None, errors=None):
        """初始化假客户端。

        参数：`responses` 为 ``{主机: responder}`` 映射；`errors` 为 ``{主机: 异常}`` 映射。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.responses = responses or {}
        self.errors = errors or {}
        self.host = ''
        self.closed = False
        self.commands = []

    def set_missing_host_key_policy(self, policy):
        """记录主机密钥策略，保持与 paramiko 接口一致。

        参数：`policy` 表示主机密钥策略。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.policy = policy

    def connect(self, hostname=None, **kwargs):
        """模拟建连，按主机名决定是否抛异常。

        参数：`hostname` 为目标主机；`kwargs` 为其余连接参数。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.host = hostname
        error = self.errors.get(hostname)
        if error is not None:
            raise error

    def exec_command(self, command, timeout=None):
        """模拟执行命令，按主机名选择 responder。

        参数：`command` 为命令文本；`timeout` 为超时秒数。
        返回：返回 ``(stdin, stdout, stderr)`` 三个带 read() 的假流。
        副作用：只在本机对象上记录命令。
        """
        self.commands.append(command)
        responder = self.responses.get(self.host, make_responder())
        out, err = responder(command)
        return _FakeStream(b''), _FakeStream(out.encode('utf-8')), _FakeStream(err.encode('utf-8'))

    def close(self):
        """模拟关闭连接。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.closed = True


class _FakeStream:
    """假输出流，只实现预检用到的 read()。"""

    def __init__(self, data=b''):
        """保存待读取的字节。

        参数：`data` 为字节内容。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self._data = data

    def read(self):
        """返回全部字节。

        参数：无。
        返回：返回初始化时传入的字节内容。
        副作用：纯读取。
        """
        return self._data


class PreflightBaseTest(TestCase):
    """预检测试的公共夹具：一个组织、一个部署任务与若干节点。"""

    def setUp(self):
        """准备测试用的数据库数据。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入测试数据（用例结束后回滚）。
        """
        self.org = Organization.objects.create(name='预检测试组织', slug='preflight-test')
        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            display_name='预检集群',
            slug='preflight-cluster',
            kubernetes_version='1.29',
            kubeasz_version='3.6.7',
            os_type='Rocky Linux',
            os_version='8',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
        )
        self.node = K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=self.deployment,
            name='node-1',
            ip_address='192.168.1.94',
            ssh_port=22,
            ssh_user='root',
        )
        self.credentials = {'192.168.1.94': {'user': 'root', 'port': 22, 'password': 'secret'}}

    def add_node(self, name, ip_address):
        """为当前部署任务追加一个节点。

        参数：`name` 为主机名；`ip_address` 为节点 IP。
        返回：返回新建的 K8sDeploymentNode。
        副作用：向数据库写入一条节点记录。
        """
        node = K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=self.deployment,
            name=name,
            ip_address=ip_address,
            ssh_port=22,
            ssh_user='root',
        )
        self.credentials[ip_address] = {'user': 'root', 'port': 22, 'password': 'secret'}
        return node

    def run_with_ssh(self, responses=None, errors=None, deployment=None):
        """在打桩 SSH 的前提下执行预检。

        参数：`responses` 为 ``{主机: responder}``；`errors` 为 ``{主机: 异常}``；
            `deployment` 为可选的任务替换。
        返回：返回 run_preflight 的报告字典。
        副作用：执行预检逻辑并替换模块内的 SSH 客户端工厂。
        """
        target = deployment or self.deployment
        nodes = list(target.nodes.all())
        with mock.patch.object(
            preflight, '_create_ssh_client',
            side_effect=lambda: FakeSSHClient(responses or {}, errors or {}),
        ):
            return preflight.run_preflight(target, nodes, self.credentials)

    @staticmethod
    def check_of(report, name):
        """从报告里取出指定检查项。

        参数：`report` 为预检报告；`name` 为检查项标识。
        返回：返回对应的检查项字典。
        副作用：纯读取。
        """
        for check in report['checks']:
            if check['name'] == name:
                return check
        raise AssertionError(f'报告里没有检查项 {name}')


class ParseOsReleaseTest(TestCase):
    """parse_os_release 的解析测试。"""

    def test_parse_typical_content(self):
        """典型内容：带引号、带注释、带 PRETTY_NAME 都要解析正确。"""
        info = preflight.parse_os_release(OS_RELEASE_ROCKY_8)
        self.assertEqual(info['id'], 'rocky')
        self.assertEqual(info['version_id'], '8.8')
        self.assertEqual(info['pretty_name'], 'Rocky Linux 8.8 (Green Obsidian)')

    def test_parse_unquoted_and_export_lines(self):
        """无引号、带 export 前缀与单引号的写法都要兼容。"""
        text = "export ID=fedora\nVERSION_ID=37\nPRETTY_NAME='Fedora Linux 37'\n"
        info = preflight.parse_os_release(text)
        self.assertEqual(info['id'], 'fedora')
        self.assertEqual(info['version_id'], '37')
        self.assertEqual(info['pretty_name'], 'Fedora Linux 37')

    def test_parse_empty_and_garbage(self):
        """空内容与无关键值对的内容返回空字段，不抛异常。"""
        for text in ('', None, '这不是 os-release 内容\n'):
            info = preflight.parse_os_release(text)
            self.assertEqual(info, {'id': '', 'version_id': '', 'pretty_name': ''})


class MatchSupportedSystemTest(TestCase):
    """match_supported_system 的系统与版本比对测试。"""

    def test_major_version_prefix_matched(self):
        """实机 8.8 与配置中心 8 的主版本一致，应匹配成功。"""
        info = preflight.parse_os_release(OS_RELEASE_ROCKY_8)
        matched, reason = preflight.match_supported_system(info, 'Rocky Linux', '8')
        self.assertTrue(matched, reason)
        self.assertIn('Rocky Linux', reason)

    def test_display_name_matched(self):
        """配置中心用展示名称 Rocky Linux、实机 ID 是 rocky，也要匹配成功。"""
        info = {'id': 'rocky', 'version_id': '8', 'pretty_name': 'Rocky Linux 8'}
        matched, reason = preflight.match_supported_system(info, 'Rocky Linux', '8')
        self.assertTrue(matched, reason)

    def test_os_id_as_expected_type_matched(self):
        """配置中心直接存 ID（rocky）时同样匹配成功。"""
        info = {'id': 'rocky', 'version_id': '9.3', 'pretty_name': 'Rocky Linux 9.3'}
        matched, _reason = preflight.match_supported_system(info, 'rocky', '9')
        self.assertTrue(matched)

    def test_ubuntu_against_rocky_failed(self):
        """Ubuntu 实机对 Rocky 配置必须判失败并给出可读原因。"""
        info = preflight.parse_os_release(OS_RELEASE_UBUNTU_22)
        matched, reason = preflight.match_supported_system(info, 'Rocky Linux', '8')
        self.assertFalse(matched)
        self.assertIn('Ubuntu', reason)
        self.assertIn('Rocky Linux', reason)

    def test_major_version_mismatch_failed(self):
        """主版本不同（实机 8 对配置 9）必须判失败。"""
        info = {'id': 'rocky', 'version_id': '8.9', 'pretty_name': 'Rocky Linux 8.9'}
        matched, reason = preflight.match_supported_system(info, 'Rocky Linux', '9')
        self.assertFalse(matched)
        self.assertIn('主版本', reason)

    def test_unknown_os_id_gives_readable_reason(self):
        """映射表里没有的 ID 要给出可读原因而不是抛异常。"""
        info = {'id': 'unknownos', 'version_id': '1.0', 'pretty_name': 'Unknown OS'}
        matched, reason = preflight.match_supported_system(info, 'Rocky Linux', '8')
        self.assertFalse(matched)
        self.assertIn('unknownos', reason)

    def test_unknown_expected_type_gives_readable_reason(self):
        """配置中心存了平台不认识系统类型时也要给出可读原因。"""
        info = preflight.parse_os_release(OS_RELEASE_ROCKY_8)
        matched, reason = preflight.match_supported_system(info, ' 某个自研系统 ', '1')
        self.assertFalse(matched)
        self.assertIn('某个自研系统', reason)

    def test_opensuse_leap_alias(self):
        """带连字符的 ID（opensuse-leap）也能映射到 openSUSE Leap。"""
        info = {'id': 'opensuse-leap', 'version_id': '15.5', 'pretty_name': 'openSUSE Leap 15.5'}
        matched, reason = preflight.match_supported_system(info, 'openSUSE Leap', '15')
        self.assertTrue(matched, reason)


class ProxyModeCombinationTest(TestCase):
    """kubeasz 版本与 kube-proxy 模式组合的纯本地判断。"""

    def test_kubeasz_37_rejects_ipvs(self):
        """kubeasz 3.7.0 不支持 ipvs，必须判不支持并说明原因。"""
        supported, reason = preflight.kubeasz_supports_proxy_mode('3.7.0', 'ipvs')
        self.assertFalse(supported)
        self.assertIn('不支持', reason)

    def test_kubeasz_36_accepts_ipvs(self):
        """kubeasz 3.6.7 支持 ipvs。"""
        supported, _reason = preflight.kubeasz_supports_proxy_mode('3.6.7', 'ipvs')
        self.assertTrue(supported)

    def test_iptables_not_limited_by_version(self):
        """非 ipvs 模式不受版本限制。"""
        for version in ('3.6.7', '3.7.0', '3.9.1', ''):
            supported, _reason = preflight.kubeasz_supports_proxy_mode(version, 'iptables')
            self.assertTrue(supported, version)

    def test_kubeasz_37_accepts_nftables(self):
        """kubeasz 3.7.0 的集群模板支持 nftables。"""
        supported, _reason = preflight.kubeasz_supports_proxy_mode('3.7.0', 'nftables')
        self.assertTrue(supported)

    def test_kubeasz_36_rejects_nftables(self):
        """kubeasz 3.6.x 的模板只认 iptables 与 ipvs，nftables 必须判不支持。"""
        supported, reason = preflight.kubeasz_supports_proxy_mode('3.6.7', 'nftables')
        self.assertFalse(supported)
        self.assertIn('nftables', reason)

    def test_model_default_mode_is_supported_by_every_version(self):
        """模型的默认模式必须在任何 kubeasz 版本上都受支持。

        ``K8sDeployment.kube_proxy_mode`` 的默认值会被「API 调用没传这个字段」这类场景直接
        采用，所以它不能是只有部分版本认的值 —— 默认落在 ipvs 上，1.36 的部署就会必然被
        提交校验或预检拦下，而调用方根本没做错什么。
        """
        default_mode = K8sDeployment._meta.get_field('kube_proxy_mode').get_default()
        self.assertEqual(default_mode, 'iptables')
        for version in ('3.6.7', '3.7.0', '3.9.1', 'unknown'):
            supported, reason = preflight.kubeasz_supports_proxy_mode(version, default_mode)
            self.assertTrue(supported, f'{version}: {reason}')

    def test_unparsable_version_is_not_blocking(self):
        """版本号解析不出来时不下否定结论。"""
        supported, _reason = preflight.kubeasz_supports_proxy_mode('unknown', 'ipvs')
        self.assertTrue(supported)


class RunPreflightTest(PreflightBaseTest):
    """run_preflight 的端到端行为测试（SSH 全部打桩）。"""

    def setUp(self):
        """在公共夹具上补充制品模块的打桩。

        参数：无。
        返回：无显式返回值。
        副作用：替换模块内的制品查询函数。
        """
        super().setUp()
        # 制品模块由其它人并行开发，这里统一打桩，避免依赖其落地进度。
        for name, value in (
            ('_supported_system_tokens', ({'rocky_8', 'ubuntu_22'}, '')),
            # 附加组件清单要读 OSS，测试里一律按「齐全」处理，缺制品的分支单独打桩。
            ('_missing_addon_artifacts', ([], '')),
        ):
            patcher = mock.patch.object(preflight, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_all_checks_passed(self):
        """SSH 全部正常时预检整体通过。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        self.assertEqual(report['hard_failures'], [])
        self.assertEqual([check['name'] for check in report['checks']], preflight.CHECKS_ORDER)
        for check in report['checks']:
            self.assertIn(check['status'], ('passed', 'warned', 'skipped'))
        self.assertEqual(report['checks'][0]['status'], 'passed')
        self.assertIn('通过', report['summary'])
        self.assertIn('失败 0 项', report['summary'])

    def test_ssh_failure_is_hard_failure(self):
        """某台节点 SSH 抛异常时整体判失败，其余检查项跳过而不是崩掉。"""
        self.add_node('node-2', '192.168.1.95')
        report = self.run_with_ssh(
            responses={'192.168.1.94': make_responder()},
            errors={'192.168.1.95': OSError('connection refused')},
        )
        self.assertFalse(report['ok'])
        self.assertTrue(report['hard_failures'])
        ssh_check = self.check_of(report, 'ssh')
        self.assertEqual(ssh_check['status'], 'failed')
        self.assertIn('192.168.1.95', ssh_check['message'])
        self.assertFalse(ssh_check['detail']['192.168.1.95']['ok'])
        self.assertTrue(ssh_check['detail']['192.168.1.94']['ok'])
        # SSH 不通的节点不应让其它检查项抛异常，它们照常在可用节点上执行。
        self.assertEqual(self.check_of(report, 'os_release')['status'], 'passed')

    def test_all_nodes_unreachable_skips_node_checks(self):
        """全部节点连不上时，节点级检查项记为跳过。"""
        report = self.run_with_ssh(errors={'192.168.1.94': OSError('timeout')})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'privilege')['status'], 'skipped')
        self.assertEqual(self.check_of(report, 'os_release')['status'], 'skipped')

    def test_missing_password_reported(self):
        """未托管密码的节点要给出可读原因而不是抛异常。"""
        self.credentials['192.168.1.94'] = {'user': 'root', 'port': 22, 'password': ''}
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        self.assertIn('密码', self.check_of(report, 'ssh')['message'])

    def test_privilege_failure(self):
        """非 root 且 sudo -n 失败时登录权限判硬失败。"""
        responder = make_responder(id=('1000\n', ''), sudo=('SUDO_NO\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'privilege')['status'], 'failed')
        self.assertIn('sudo', self.check_of(report, 'privilege')['message'])

    def test_passwordless_sudo_passes(self):
        """非 root 但免密 sudo 可用时登录权限通过。"""
        responder = make_responder(id=('1000\n', ''), sudo=('SUDO_OK\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertEqual(self.check_of(report, 'privilege')['status'], 'passed')
        self.assertEqual(self.check_of(report, 'privilege')['detail']['192.168.1.94']['mode'], 'sudo')

    def test_os_release_mismatch_only_warns(self):
        """实机系统与配置中心不一致只报警告，不拦部署。"""
        responder = make_responder(os_release=(OS_RELEASE_UBUNTU_22, ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        os_check = self.check_of(report, 'os_release')
        self.assertEqual(os_check['status'], 'warned')
        self.assertIn('Ubuntu', os_check['message'])
        # 逐节点信息仍要完整，否则界面上看不出到底差在哪。
        self.assertEqual(os_check['detail']['192.168.1.94']['os_release']['id'], 'ubuntu')

    def test_disk_low_space_failed(self):
        """未分配角色的节点按 worker 档（30GB）判，可用空间不足即硬失败。"""
        low = 'Filesystem     1024-blocks     Used Available Capacity Mounted on\n/dev/sda1 104744960 100000000 4744960 96% /\n'
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder(df=(low, ''))})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'disk')['status'], 'failed')
        message = self.check_of(report, 'disk')['message']
        self.assertIn('低于', message)
        self.assertIn(str(preflight.DISK_MIN_FREE_GB_WORKER), message)

    def test_control_plane_node_requires_more_space(self):
        """同一个 35GB 的盘：worker 角色够用，etcd/control-plane 角色不够。"""
        # 35 GB = 36700160 KB，介于 worker 的 30GB 与 control-plane 的 40GB 之间。
        mid = 'Filesystem     1024-blocks     Used Available Capacity Mounted on\n/dev/sda1 104744960 70000000 36700160 67% /\n'
        responder = make_responder(df=(mid, ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        self.assertEqual(
            self.check_of(report, 'disk')['detail']['192.168.1.94']['required_gb'],
            preflight.DISK_MIN_FREE_GB_WORKER,
        )

        self.node.roles = ['etcd', 'master', 'node']
        self.node.save(update_fields=['roles', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        message = self.check_of(report, 'disk')['message']
        self.assertIn(str(preflight.DISK_MIN_FREE_GB_CONTROL_PLANE), message)

    def test_data_dir_disk_is_checked_when_enabled(self):
        """启用自定义数据目录时量的是那个盘，并在 detail 里记下路径。"""
        self.deployment.data_dir_enabled = True
        self.deployment.data_dir = '/data/k8s'
        self.deployment.save(update_fields=['data_dir_enabled', 'data_dir', 'updated_at'])
        seen = []

        def responder(command):
            seen.append(command)
            return make_responder()(command)

        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        self.assertTrue(all("/data/k8s" in command for command in seen if command.startswith("d='")))
        detail = self.check_of(report, 'disk')['detail']
        self.assertEqual(detail['data_dir'], '/data/k8s')
        self.assertEqual(detail['192.168.1.94']['path'], '/data/k8s')

    def test_illegal_data_dir_is_hard_failure(self):
        """数据目录里带 shell 特殊字符时直接判失败，不把它拼进远程命令。"""
        self.deployment.data_dir_enabled = True
        self.deployment.data_dir = '/data; rm -rf /'
        self.deployment.save(update_fields=['data_dir_enabled', 'data_dir', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'disk')['status'], 'failed')
        self.assertIn('不合法', self.check_of(report, 'disk')['message'])

    def test_platform_cache_shortage_only_warns(self):
        """平台机缓存盘不足只给警告：清一下就好，不该挡住合规的部署。"""
        with mock.patch.object(preflight, '_platform_cache_free_gb', return_value=(3.5, '/cache', '')):
            report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'disk')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('缓存', check['message'])
        self.assertEqual(check['detail']['platform_cache']['free_gb'], 3.5)

    def test_inode_tight_only_warns(self):
        """inode 紧张只给警告，不阻断部署。"""
        tight = 'Filesystem       Inodes  IUsed    IFree IUse% Mounted on\n/dev/sda1       6553600 6233600   320000   96% /\n'
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder(df_inode=(tight, ''))})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'disk')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('inode', check['message'])

    def test_kubeasz_37_ipvs_is_hard_failure(self):
        """kubeasz 3.7.0 + ipvs：纯本地判断即失败，且 message 写明不支持。"""
        self.deployment.kubeasz_version = '3.7.0'
        self.deployment.save(update_fields=['kubeasz_version', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('不支持', check['message'])
        self.assertIn('iptables', check['message'])
        self.assertIn('3.7.0', check['message'])

    def test_kubeasz_36_ipvs_passes(self):
        """kubeasz 3.6.7 + ipvs 且 ip_vs 已加载时通过。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertEqual(self.check_of(report, 'kernel')['status'], 'passed')

    def test_ipvs_modules_missing_is_hard_failure(self):
        """ipvs 模式下节点缺少任一必需模块（不只 ip_vs）时判硬失败并点名缺哪个。"""
        responder = make_responder(
            ipvs=('MODULES_MISSING ip_vs_rr ip_vs_sh\n', ''),
        )
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('ipvsadm', check['message'])
        self.assertIn('ip_vs_rr', check['message'])
        self.assertIn('ip_vs_sh', check['message'])
        self.assertIn('nf_conntrack', ' '.join(check['detail']['required_modules']))

    def test_ipvs_modules_only_in_kernel_passes(self):
        """模块没加载但内核里有，部署时可以自动加载，应判通过。"""
        responder = make_responder(ipvs=('MODULES_OK\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['detail']['nodes']['192.168.1.94']['loaded'], False)

    def test_kernel_skipped_for_iptables(self):
        """非 ipvs 模式下内核项跳过。"""
        self.deployment.kube_proxy_mode = 'iptables'
        self.deployment.save(update_fields=['kube_proxy_mode', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        self.assertEqual(self.check_of(report, 'kernel')['status'], 'skipped')

    def test_kubeasz_37_nftables_passes_when_kernel_and_nft_are_new_enough(self):
        """3.7.0 + nftables：内核与 nft 都达标时通过，明细里记下各自的版本。"""
        self.deployment.kubeasz_version = '3.7.0'
        self.deployment.kube_proxy_mode = 'nftables'
        self.deployment.save(update_fields=['kubeasz_version', 'kube_proxy_mode', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['detail']['nodes']['192.168.1.94']['nft'], '1.0.9')
        self.assertEqual(check['detail']['required']['kernel'], '5.13')

    def test_kubeasz_37_nftables_with_old_kernel_is_hard_failure(self):
        """内核低于 5.13 时判失败，并点名内核版本与门槛。"""
        self.deployment.kubeasz_version = '3.7.0'
        self.deployment.kube_proxy_mode = 'nftables'
        self.deployment.save(update_fields=['kubeasz_version', 'kube_proxy_mode', 'updated_at'])
        responder = make_responder(nft=('NFT_PROBE kernel=4.19.90 nft=1.0.9\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('4.19.90', check['message'])
        self.assertIn('5.13', check['message'])

    def test_kubeasz_37_nftables_without_nft_is_hard_failure(self):
        """没有 nft 命令时判失败，且提示的是装包而不是内核太老。"""
        self.deployment.kubeasz_version = '3.7.0'
        self.deployment.kube_proxy_mode = 'nftables'
        self.deployment.save(update_fields=['kubeasz_version', 'kube_proxy_mode', 'updated_at'])
        responder = make_responder(nft=('NFT_PROBE kernel=6.1.0 nft=none\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('nft', check['message'])
        self.assertIn('安装', check['message'])

    def test_kubeasz_37_nftables_with_old_nft_is_hard_failure(self):
        """nft 版本低于 1.0.0 时判失败并给出实际版本。"""
        self.deployment.kubeasz_version = '3.7.0'
        self.deployment.kube_proxy_mode = 'nftables'
        self.deployment.save(update_fields=['kubeasz_version', 'kube_proxy_mode', 'updated_at'])
        responder = make_responder(nft=('NFT_PROBE kernel=6.1.0 nft=0.9.3\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('0.9.3', check['message'])

    def test_kubeasz_36_nftables_is_hard_failure_without_probing(self):
        """3.6.x + nftables：模板不支持，纯本地判断即失败，不去节点上探 nft。"""
        self.deployment.kube_proxy_mode = 'nftables'
        self.deployment.save(update_fields=['kube_proxy_mode', 'updated_at'])
        seen = []

        def responder(command):
            seen.append(command)
            return make_responder()(command)

        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'kernel')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('nftables', check['message'])
        self.assertFalse([command for command in seen if 'nft --version' in command])

    def test_port_occupied_is_hard_failure(self):
        """ApiServer 端口被占用时判硬失败。"""
        responder = make_responder(ports=(SS_PORT_OCCUPIED, ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'ports')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('6443', check['message'])

    def test_etcd_and_kubelet_ports_are_checked(self):
        """etcd 的 2379/2380 与 kubelet 的 10250 都在检查范围内。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        checked = self.check_of(report, 'ports')['detail']['checked_ports']
        self.assertIn(preflight.KUBELET_PORT, checked)
        # 未分配角色的节点不跑 etcd，不检查 etcd 端口。
        self.assertNotIn(2379, checked)

        self.node.roles = ['etcd', 'master']
        self.node.save(update_fields=['roles', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        checked = self.check_of(report, 'ports')['detail']['checked_ports']
        self.assertIn(2379, checked)
        self.assertIn(2380, checked)

    def test_etcd_port_occupied_is_hard_failure(self):
        """etcd 端口被占用时同样判硬失败并点名端口。"""
        self.node.roles = ['etcd', 'master']
        self.node.save(update_fields=['roles', 'updated_at'])
        occupied = SS_IDLE + 'LISTEN 0      128          0.0.0.0:2379      0.0.0.0:*\n'
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder(ports=(occupied, ''))})
        self.assertFalse(report['ok'])
        message = self.check_of(report, 'ports')['message']
        self.assertIn('2379', message)
        self.assertIn('etcd', message)

    def test_port_tool_missing_only_warns(self):
        """节点没有 ss/netstat 时只给警告，不阻断部署。"""
        responder = make_responder(ports=('__NO_LISTEN_TOOL__\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        self.assertEqual(self.check_of(report, 'ports')['status'], 'warned')

    def test_clock_drift_only_warns(self):
        """时钟偏差超过 2 秒只给警告。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder(clock_offset=300)})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'clock')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('300', check['message'])
        self.assertEqual(check['detail']['threshold_seconds'], 2)

    def test_clock_within_two_seconds_passes(self):
        """偏差在 2 秒以内且守时服务已同步时通过。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder(clock_offset=1)})
        check = self.check_of(report, 'clock')
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['detail']['nodes']['192.168.1.94']['sync_service'], 'chrony')

    def test_missing_time_sync_service_warns(self):
        """节点上没有 chrony/timesyncd/ntpd 时给警告。"""
        idle = 'CHRONY:no\nLEAP:\nCHRONYD:inactive\nTIMESYNCD:inactive\nNTPD:inactive\nSYNCED:0\n'
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder(timesync=(idle, ''))})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'clock')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('守时服务', check['message'])

    def test_network_interface_missing_is_hard_failure(self):
        """网卡不存在时判硬失败。"""
        responder = make_responder(link=('IFACE_MISSING\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'network')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('eth0', check['message'])

    def test_network_illegal_interface_name_rejected(self):
        """网卡名带 shell 特殊字符时直接判失败，避免命令注入。"""
        self.deployment.network_interface = 'eth0; rm -rf /'
        self.deployment.save(update_fields=['network_interface', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'network')['status'], 'failed')

    def test_network_interface_down_is_hard_failure(self):
        """网卡存在但管理态不是 UP 时判硬失败。"""
        responder = make_responder(link=('IFACE_DOWN\n', ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'network')
        self.assertEqual(check['status'], 'failed')
        self.assertEqual(check['detail']['nodes']['192.168.1.94']['interface_state'], 'DOWN')

    def test_tcp_unreachable_only_warns(self):
        """节点间 TCP 不通只给警告（禁 ping 的环境不影响这个结论）。"""
        self.add_node('node-2', '192.168.1.95')
        responder = make_responder(tcp=('TCP_FAIL\n', ''))
        report = self.run_with_ssh(responses={
            '192.168.1.94': responder,
            '192.168.1.95': make_responder(),
        })
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'network')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('TCP 不通', check['message'])

    def test_tcp_probe_targets_peer_ssh_port(self):
        """TCP 探测打的是对端的 SSH 端口，双向各探一次。"""
        self.add_node('node-2', '192.168.1.95')
        report = self.run_with_ssh(responses={
            '192.168.1.94': make_responder(),
            '192.168.1.95': make_responder(),
        })
        check = self.check_of(report, 'network')
        self.assertEqual(check['status'], 'passed')
        self.assertCountEqual(
            check['detail']['tcp_checked_pairs'],
            ['192.168.1.94->192.168.1.95:22', '192.168.1.95->192.168.1.94:22'],
        )

    def test_pod_service_cidr_overlap_is_hard_failure(self):
        """pod 与 service 网段重叠时判硬失败。"""
        self.deployment.service_cidr = '10.244.1.0/24'
        self.deployment.save(update_fields=['service_cidr', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'cidr')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('重叠', check['message'])

    def test_illegal_cidr_is_hard_failure(self):
        """CIDR 不合法时判硬失败。"""
        self.deployment.pod_cidr = '10.244.0.0/33'
        self.deployment.save(update_fields=['pod_cidr', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'cidr')['status'], 'failed')

    def test_cidr_overlapping_node_network_is_hard_failure(self):
        """容器网段与节点所在网段重叠时判硬失败。"""
        # 把节点整体挪到容器网段里：IP、凭据与 SSH 响应都要跟着换。
        self.node.ip_address = '10.244.0.5'
        self.node.save(update_fields=['ip_address', 'updated_at'])
        self.credentials = {'10.244.0.5': {'user': 'root', 'port': 22, 'password': 'secret'}}
        addr = '2: eth0    inet 10.244.0.5/24 brd 10.244.0.255 scope global eth0\n'
        report = self.run_with_ssh(responses={'10.244.0.5': make_responder(addr=(addr, ''))})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'cidr')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('重叠', check['message'])

    def test_artifact_unsupported_only_warns(self):
        """制品清单里没有该系统的离线包时只报警告，并列出清单声明的系统。

        不判失败：该清单取自上游 ezdown 的声明文本，会过时（例如 3.6.7 仍写着 centos_7 而
        对应镜像从未发布），且平台恒以 online 模式编译，离线包并不参与部署。系统与版本的
        配对由配置中心的 supported_systems 在创建任务时把关。
        """
        with mock.patch.object(
            preflight, '_supported_system_tokens', return_value=({'ubuntu_22'}, ''),
        ):
            report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'artifact')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('rocky_8', check['message'])
        self.assertIn('ubuntu_22', check['message'])

    def test_artifact_module_unavailable_is_skipped(self):
        """制品模块导入失败时本项降级为跳过，不影响整体结论。"""
        with mock.patch.object(
            preflight, '_supported_system_tokens', return_value=(None, '制品模块暂不可用（No module named）'),
        ):
            report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'artifact')
        self.assertEqual(check['status'], 'skipped')
        self.assertIn('制品模块', check['message'])

    def test_missing_addon_artifact_is_hard_failure(self):
        """要装的附加组件在制品里没有镜像时判硬失败（规划 4.3）。"""
        missing = [('ingress', 'Nginx Ingress（制品里缺少 ingress-nginx）')]
        with mock.patch.object(preflight, '_missing_addon_artifacts', return_value=(missing, '')):
            report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'artifact')
        self.assertEqual(check['status'], 'failed')
        self.assertIn('ingress-nginx', check['message'])
        self.assertEqual(check['detail']['missing_addons'][0]['field'], 'ingress')

    def test_addon_manifest_unreadable_only_warns(self):
        """清单读不到时不能判失败：判不了不等于没有，一次 OSS 抖动不该挡住部署。"""
        with mock.patch.object(
            preflight, '_missing_addon_artifacts', return_value=([], '磁盘上找不到清单'),
        ):
            report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'artifact')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('磁盘上找不到清单', check['message'])

    def test_missing_addon_artifacts_passes_deployment_switches(self):
        """查询附加组件制品时要把本次的组件开关与 CNI 一起带过去。"""
        from k8s_deployment.services import artifacts

        self.deployment.metrics_server = True
        self.deployment.nginx_ingress = True
        self.deployment.install_helm_cli = True
        self.deployment.save(update_fields=[
            'metrics_server', 'nginx_ingress', 'install_helm_cli', 'updated_at',
        ])
        # 本用例直接调内部函数，必须先把基类打的桩摘掉，否则测到的是桩本身。
        mock.patch.stopall()
        with mock.patch.object(artifacts, 'missing_addon_artifacts', return_value=([], '')) as probe:
            self.assertEqual(preflight._missing_addon_artifacts(self.deployment), ([], ''))
        probe.assert_called_once_with(
            self.deployment.kubeasz_version,
            cni_plugin=self.deployment.cni_plugin,
            metrics_server=True,
            nginx_ingress=True,
            install_helm_cli=True,
        )

    def test_multi_node_aio_report_shape(self):
        """多节点时报告结构完整，nodes 字段给出涉及的主机。"""
        self.add_node('node-2', '192.168.1.95')
        report = self.run_with_ssh(responses={
            '192.168.1.94': make_responder(),
            '192.168.1.95': make_responder(),
        })
        self.assertTrue(report['ok'], report['hard_failures'])
        for check in report['checks']:
            self.assertIn('detail', check)
            self.assertIsInstance(check['nodes'], list)
            self.assertTrue(check['message'])
        self.assertCountEqual(self.check_of(report, 'ssh')['nodes'], ['192.168.1.94', '192.168.1.95'])
        # 报告要能直接写进 JSONField，序列化失败说明 detail 里混进了非 JSON 类型。
        self.assertIsInstance(json.dumps(report, ensure_ascii=False), str)

    def test_unexpected_check_error_degrades_to_warning(self):
        """某个检查项内部异常时收敛成警告，不让整个预检崩掉。"""
        with mock.patch.object(preflight, '_check_cidr', side_effect=RuntimeError('内部错误')):
            report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'cidr')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('内部错误', check['message'])

    def test_no_nodes_raises_preflight_error(self):
        """没有目标节点时抛 PreflightError。"""
        with self.assertRaises(preflight.PreflightError):
            preflight.run_preflight(self.deployment, [], {})

    def test_ssh_layer_error_is_hard_failure(self):
        """SSH 层自身出错（例如缺 paramiko）按硬失败处理，而不是抛给调用方。"""
        with mock.patch.object(
            preflight, '_create_ssh_client',
            side_effect=preflight.PreflightError('当前环境未安装 paramiko，无法执行 SSH 预检'),
        ):
            report = preflight.run_preflight(
                self.deployment, list(self.deployment.nodes.all()), self.credentials,
            )
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'ssh')['status'], 'failed')
        self.assertIn('paramiko', self.check_of(report, 'ssh')['message'])


class ParseStaleProbeTest(TestCase):
    """parse_stale_probe 的解析测试。"""

    def test_clean_output_has_no_residue(self):
        """三处路径都报 ABSENT 时结果为空。"""
        self.assertEqual(preflight.parse_stale_probe(STALE_PROBE_CLEAN), {})

    def test_present_dir_is_reported_with_entry_count(self):
        """目录有内容时按条目数报出来，路径就是键。"""
        text = stale_probe_output({f'{preflight.DEFAULT_ETCD_DATA_DIR}/member': 4})
        self.assertEqual(preflight.parse_stale_probe(text), {f'{preflight.DEFAULT_ETCD_DATA_DIR}/member': 4})

    def test_empty_dir_is_not_residue(self):
        """kubeasz 会先建出空的数据目录：条目数为 0 说明 etcd 没起来过，不算残留。"""
        text = stale_probe_output({f'{preflight.DEFAULT_ETCD_DATA_DIR}/member': 0})
        self.assertEqual(preflight.parse_stale_probe(text), {})

    def test_missing_end_marker_is_not_residue(self):
        """输出被截断（缺收尾标记）时按「没有残留」处理，不误报。"""
        text = f'__STALE_PROBE_BEGIN__\nPRESENT:{preflight.DEFAULT_ETCD_DATA_DIR}/member:dir:4\n'
        self.assertEqual(preflight.parse_stale_probe(text), {})

    def test_present_without_begin_marker_is_ignored(self):
        """没有开始标记时，混进来的 PRESENT 行不作数。"""
        self.assertEqual(preflight.parse_stale_probe('PRESENT:/var/lib/etcd/member:dir:4\n'), {})

    def test_non_numeric_count_falls_back_to_one(self):
        """条目数读不出来时按「有」，宁可提示也不要漏报。"""
        text = (
            '__STALE_PROBE_BEGIN__\n'
            'PRESENT:/etc/keepalived/keepalived.conf:file:unknown\n'
            '__STALE_PROBE_END__\n'
        )
        self.assertEqual(preflight.parse_stale_probe(text), {'/etc/keepalived/keepalived.conf': 1})

    def test_empty_and_none_input(self):
        """空输入或 None 都不能抛异常。"""
        self.assertEqual(preflight.parse_stale_probe(''), {})
        self.assertEqual(preflight.parse_stale_probe(None), {})


class StaleInstallCheckTest(PreflightBaseTest):
    """预检项「上一次安装的残留」的判定测试。

    这一项由 94 上 k8s-111 那次失败而来：机器上留着上一个集群的 etcd 数据，新建集群的
    etcd 以 ``--initial-cluster-state=new`` 启动却被旧数据接管，直接 ``invalid downgrade``
    退出，界面上只看到「部署 etcd：执行失败，退出码 1」。
    """

    def test_clean_machine_passes(self):
        """机器上没有上一次安装的痕迹时通过，并记录查了哪些路径。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'passed')
        self.assertIn('没有上一次安装留下的', check['message'])
        self.assertEqual(check['detail']['etcd_data_dir'], preflight.DEFAULT_ETCD_DATA_DIR)
        self.assertFalse(check['detail']['retry'])
        self.assertTrue(check['detail']['192.168.1.94']['ok'])
        # 机器干净时就没有「要清什么」可言，清理判定照旧落在 detail 里，供界面与流程读取。
        self.assertFalse(check['detail']['cleanup']['will_cleanup'])
        self.assertEqual(check['detail']['cleanup']['targets'], [])

    def test_first_attempt_with_etcd_data_will_cleanup(self):
        """首次执行、机器上留着上一套集群的 etcd 数据 —— 默认勾选清理，所以不拦，改为在部署前清掉。"""
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        responder = make_responder(stale=(stale_probe_output({path: 4}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('自动清理', check['message'])
        self.assertIn(path, check['message'])
        self.assertEqual(check['nodes'], ['192.168.1.94'])
        found = check['detail']['192.168.1.94']['found']
        self.assertEqual(found, [{'path': path, 'label': 'etcd 数据', 'entries': 4}])
        cleanup = check['detail']['cleanup']
        self.assertTrue(cleanup['enabled'])
        self.assertTrue(cleanup['will_cleanup'])
        self.assertEqual(cleanup['targets'], ['192.168.1.94'])

    def test_cleanup_disabled_on_first_attempt_is_hard_failure(self):
        """取消勾选后回到硬拦：说清「部署必然失败在 02.etcd」以及怎么清。"""
        self.deployment.auto_clean_stale = False
        self.deployment.save(update_fields=['auto_clean_stale', 'updated_at'])
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        responder = make_responder(stale=(stale_probe_output({path: 4}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'failed')
        self.assertIn(path, check['message'])
        self.assertIn('invalid downgrade', check['message'])
        self.assertIn('清理集群', check['message'])
        cleanup = check['detail']['cleanup']
        self.assertFalse(cleanup['enabled'])
        self.assertFalse(cleanup['will_cleanup'])
        self.assertEqual(cleanup['reason'], 'disabled')
        self.assertEqual(cleanup['targets'], [])

    def test_installed_cluster_is_never_cleaned(self):
        """本任务已经装出过集群时不清理 —— 那份残留多半是集群自己，清掉等于把它推平重装。"""
        self.deployment.completed_stages = ['01.prepare', '02.etcd']
        self.deployment.save(update_fields=['completed_stages', 'updated_at'])
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        responder = make_responder(stale=(stale_probe_output({path: 4}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('推倒重装', check['message'])
        cleanup = check['detail']['cleanup']
        self.assertTrue(cleanup['enabled'])
        self.assertFalse(cleanup['will_cleanup'])
        self.assertEqual(cleanup['reason'], 'already_installed')

    def test_uninstalled_stages_do_not_block_cleanup(self):
        """只完成到 01.prepare（还没装出集群）时照旧清理 —— 这正是 94 上重试要走的路径。"""
        self.deployment.completed_stages = ['01.prepare']
        self.deployment.retry_count = 1
        self.deployment.save(update_fields=['completed_stages', 'retry_count', 'updated_at'])
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        responder = make_responder(stale=(stale_probe_output({path: 4}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'warned')
        self.assertTrue(check['detail']['cleanup']['will_cleanup'])

    def test_keepalived_conf_leftover_triggers_cleanup(self):
        """负载均衡配置是外部负载均衡器的接管依据，单独命中也要清。"""
        responder = make_responder(stale=(stale_probe_output({preflight.KEEPALIVED_CONF: 1}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('外部负载均衡配置', check['message'])
        self.assertTrue(check['detail']['cleanup']['will_cleanup'])

    def test_multiple_nodes_report_each_hit(self):
        """多台机器各自报出自己的残留，message 里要能分清是哪台，清理目标只列命中的那台。"""
        self.add_node('node-2', '192.168.1.95')
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        dirty = make_responder(stale=(stale_probe_output({path: 2}), ''))
        report = self.run_with_ssh(responses={
            '192.168.1.94': dirty,
            '192.168.1.95': make_responder(),
        })
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertIn('192.168.1.94：', check['message'])
        self.assertNotIn('192.168.1.95：', check['message'])
        self.assertTrue(check['detail']['192.168.1.95']['ok'])
        self.assertFalse(check['detail']['192.168.1.94']['ok'])
        # 干净的那台不进清理清单：清理命令不要往没问题的机器上发。
        self.assertEqual(check['detail']['cleanup']['targets'], ['192.168.1.94'])

    def test_retry_with_cleanup_disabled_only_warns(self):
        """取消勾选后，重试时机器上的残留可能就是本任务上一次写下的，只告警不拦截。"""
        self.deployment.retry_count = 1
        self.deployment.auto_clean_stale = False
        self.deployment.save(update_fields=['retry_count', 'auto_clean_stale', 'updated_at'])
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        responder = make_responder(stale=(stale_probe_output({path: 4}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'warned')
        self.assertIn('重试', check['message'])
        self.assertIn('不拦截', check['message'])
        self.assertTrue(check['detail']['retry'])
        self.assertFalse(check['detail']['cleanup']['will_cleanup'])

    def test_non_deploy_task_is_skipped(self):
        """扩容这类任务不重装 etcd 数据，机器上留着痕迹与本任务无关。"""
        self.deployment.task_type = 'add_worker'
        self.deployment.save(update_fields=['task_type', 'updated_at'])
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        responder = make_responder(stale=(stale_probe_output({path: 4}), ''))
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'skipped')
        self.assertEqual(check['detail']['reason'], 'not_a_full_deploy')

    def test_custom_data_dir_is_probed_instead_of_default(self):
        """启用自定义数据目录时探的是那个目录，默认路径不再查。"""
        self.deployment.data_dir_enabled = True
        self.deployment.data_dir = '/data/k8s'
        self.deployment.save(update_fields=['data_dir_enabled', 'data_dir', 'updated_at'])
        seen = []

        def responder(command):
            seen.append(command)
            return make_responder()(command)

        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertTrue(report['ok'], report['hard_failures'])
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['detail']['etcd_data_dir'], '/data/k8s/etcd')
        probe = [command for command in seen if '__STALE_PROBE_BEGIN__' in command]
        self.assertEqual(len(probe), 1)
        self.assertIn('/data/k8s/etcd/member', probe[0])
        self.assertNotIn(f'{preflight.DEFAULT_ETCD_DATA_DIR}/member', probe[0])

    def test_illegal_data_dir_never_reaches_the_shell(self):
        """数据目录里带 shell 元字符时退回默认路径：它会被拼进远程命令，绝不能原样下发。

        非法值本身由「磁盘与 inode」那一项硬失败拦下，这里只保证预检与清理两条路径都不会
        把用户输入的这串东西送进 shell。
        """
        evil = '/data; rm -rf /'
        self.deployment.data_dir_enabled = True
        self.deployment.data_dir = evil
        self.deployment.save(update_fields=['data_dir_enabled', 'data_dir', 'updated_at'])
        seen = []

        def responder(command):
            seen.append(command)
            return make_responder()(command)

        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        check = self.check_of(report, 'stale_install')
        self.assertEqual(check['detail']['etcd_data_dir'], preflight.DEFAULT_ETCD_DATA_DIR)
        probes = [command for command in seen if '__STALE_PROBE_BEGIN__' in command]
        self.assertTrue(probes)
        for command in probes:
            self.assertNotIn('rm -rf /;', command)
            self.assertNotIn(evil, command)
            self.assertIn(f'{preflight.DEFAULT_ETCD_DATA_DIR}/member', command)
        # 清理命令走的是同一个算式，也要确认那里没有把非法目录拼出去。
        self.assertEqual(stale_cleanup.removable_paths(self.deployment)[0], preflight.DEFAULT_ETCD_DATA_DIR)
        self.assertNotIn(evil, stale_cleanup.cleanup_command(self.deployment))

    def test_unreadable_probe_only_warns(self):
        """单台机器上探针执行失败时只告警，不把它当成「有残留」拦下。"""

        class BrokenSession:
            """run() 一律返回失败的最小会话替身。"""

            def run(self, command):
                """无论什么命令都回报执行失败。"""
                return (False, '', '命令执行失败：channel closed')

        check = preflight._check_stale_install(self.deployment, {'192.168.1.94': BrokenSession()}, ['192.168.1.94'])
        self.assertEqual(check['status'], 'warned')
        self.assertIn('无法确认', check['message'])
        self.assertIn('channel closed', check['message'])
        # 三处痕迹都要查：etcd 数据、集群证书、外部负载均衡配置。
        self.assertEqual(
            [path for path, _label in preflight.stale_install_targets(self.deployment)],
            [
                f'{preflight.DEFAULT_ETCD_DATA_DIR}/member',
                f'{preflight.ETCD_SSL_DIR}/ca.pem',
                preflight.KEEPALIVED_CONF,
            ],
        )


class ParseEtcdQuorumTest(TestCase):
    """parse_etcd_quorum 与 etcd_quorum_conclusion 的解析与判定测试。"""

    def test_parse_all_healthy(self):
        """成员数与健康数都读到时要如实解析，并给出正常结论。"""
        state = preflight.parse_etcd_quorum(ETCD_QUORUM_OK)
        self.assertEqual(state['members'], 3)
        self.assertEqual(state['healthy'], 3)
        self.assertEqual(state['self'], 'healthy')
        self.assertEqual(preflight.etcd_quorum_conclusion(state)[0], 'passed')

    def test_missing_etcdctl_is_not_a_failure(self):
        """节点上没有 etcdctl 只是查不了，不能据此判集群仲裁丢失。"""
        state = preflight.parse_etcd_quorum('ETCDCTL_MISSING\n')
        self.assertIsNone(state['members'])
        self.assertIn('etcdctl', state['reason'])
        self.assertEqual(preflight.etcd_quorum_conclusion(state)[0], 'warned')

    def test_cert_missing_reported(self):
        """缺少客户端证书时给出可读原因，且不判失败。"""
        state = preflight.parse_etcd_quorum('ETCD_CERT_MISSING\n')
        self.assertIn('证书', state['reason'])
        self.assertEqual(preflight.etcd_quorum_conclusion(state)[0], 'warned')

    def test_empty_output_is_unknown_not_lost(self):
        """命令没有任何输出（集群已经不可用）只能算「无法判定」。"""
        status, message = preflight.etcd_quorum_conclusion(preflight.parse_etcd_quorum(''))
        self.assertEqual(status, 'warned')
        self.assertIn('成员列表', message)

    def test_lost_quorum_failed(self):
        """3 个成员只有 1 个健康时仲裁已丢失，必须判失败。"""
        state = preflight.parse_etcd_quorum('HEALTHY:1\nUNHEALTHY:2\nMEMBERS:3\nSELF:healthy\n')
        status, message = preflight.etcd_quorum_conclusion(state)
        self.assertEqual(status, 'failed')
        self.assertIn('2 个', message)

    def test_partial_membership_still_passes_with_warning(self):
        """5 个成员健康 4 个时仍有仲裁，只给警告。"""
        state = preflight.parse_etcd_quorum('HEALTHY:4\nUNHEALTHY:1\nMEMBERS:5\nSELF:healthy\n')
        self.assertEqual(preflight.etcd_quorum_conclusion(state)[0], 'warned')

    def test_cluster_wide_health_unsupported_falls_back_to_self(self):
        """etcdctl 不支持 --cluster 时只有本机结论，按本机健康给警告而不是判死。"""
        state = preflight.parse_etcd_quorum('MEMBERS:3\nSELF:healthy\n')
        self.assertIsNone(state['healthy'])
        status, message = preflight.etcd_quorum_conclusion(state)
        self.assertEqual(status, 'warned')
        self.assertIn('人工确认', message)

    def test_self_unhealthy_with_unknown_membership_failed(self):
        """成员数读到了但本机端点不健康，说明本机很可能已失去仲裁。"""
        state = preflight.parse_etcd_quorum('MEMBERS:3\nSELF:unhealthy\n')
        self.assertEqual(preflight.etcd_quorum_conclusion(state)[0], 'failed')

    def test_member_count_excludes_unstarted_members(self):
        """成员数只数 started 的那些：未启动成员不参与仲裁。"""
        state = preflight.parse_etcd_quorum('MEMBERS:2\nSELF:healthy\n')
        self.assertEqual(state['members'], 2)


class EtcdQuorumCheckTest(PreflightBaseTest):
    """预检里的 etcd 仲裁检查项的适用范围测试。"""

    def attach_cluster(self):
        """给当前任务挂上一个已存在的集群，并让节点承担 etcd 角色。

        参数：无。
        返回：返回新建的 K8sCluster。
        副作用：向数据库写入集群记录并更新节点角色。
        """
        cluster = K8sCluster.objects.create(
            organization=self.org, name='仲裁测试集群', slug='quorum-cluster', origin='deployed',
        )
        self.deployment.cluster = cluster
        self.deployment.save(update_fields=['cluster', 'updated_at'])
        self.node.roles = ['etcd', 'master', 'node']
        self.node.save(update_fields=['roles', 'updated_at'])
        return cluster

    def test_new_cluster_is_skipped(self):
        """新建集群还没有 etcd，本项跳过（基类任务就没有 cluster）。"""
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        check = self.check_of(report, 'etcd_quorum')
        self.assertEqual(check['status'], 'skipped')
        self.assertIn('新建集群', check['message'])

    def test_worker_only_task_is_skipped(self):
        """只提交一台 worker 的扩容任务不查 etcd，但要提示确认控制面状态。"""
        self.attach_cluster()
        self.node.roles = ['node']
        self.node.save(update_fields=['roles', 'updated_at'])
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        check = self.check_of(report, 'etcd_quorum')
        self.assertEqual(check['status'], 'skipped')
        self.assertIn('control-plane', check['message'])

    def test_existing_cluster_quorum_healthy_passes(self):
        """已存在集群上的 etcd 角色节点要真的查一次仲裁，健康时通过。"""
        self.attach_cluster()
        report = self.run_with_ssh(responses={'192.168.1.94': make_responder()})
        check = self.check_of(report, 'etcd_quorum')
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['detail']['members'], 3)
        self.assertEqual(check['detail']['probed'][0]['ip'], '192.168.1.94')

    def test_lost_quorum_is_hard_failure(self):
        """仲裁丢失时整个预检判失败，任务不会在坏集群上继续。"""
        self.attach_cluster()
        responder = make_responder(
            etcd_quorum=('HEALTHY:1\nUNHEALTHY:2\nMEMBERS:3\nSELF:unhealthy\n', ''),
        )
        report = self.run_with_ssh(responses={'192.168.1.94': responder})
        self.assertFalse(report['ok'])
        self.assertEqual(self.check_of(report, 'etcd_quorum')['status'], 'failed')

    def test_second_node_used_when_first_cannot_answer(self):
        """第一台 etcd 节点查不出来时换下一台继续查，不直接放弃。"""
        self.attach_cluster()
        self.add_node('node-2', '192.168.1.95')
        self.node.roles = ['etcd', 'master']
        self.node.save(update_fields=['roles', 'updated_at'])
        second = self.deployment.nodes.get(ip_address='192.168.1.95')
        second.roles = ['etcd', 'master']
        second.save(update_fields=['roles', 'updated_at'])
        report = self.run_with_ssh(responses={
            '192.168.1.94': make_responder(etcd_quorum=('ETCDCTL_MISSING\n', '')),
            '192.168.1.95': make_responder(),
        })
        check = self.check_of(report, 'etcd_quorum')
        self.assertEqual(check['status'], 'passed')
        self.assertEqual([item['ip'] for item in check['detail']['probed']], ['192.168.1.94', '192.168.1.95'])


class ArtifactTokenTest(TestCase):
    """os_token_for 的 token 归一化测试。"""

    def test_token_from_display_name(self):
        """展示名称与主版本号拼出的 token 符合制品命名约定。"""
        self.assertEqual(preflight.os_token_for('Rocky Linux', '8'), 'rocky_8')
        self.assertEqual(preflight.os_token_for('Ubuntu', '22.04'), 'ubuntu_22')
        self.assertEqual(preflight.os_token_for('openSUSE Leap', '15.5'), 'opensuse_leap_15')
        self.assertEqual(preflight.os_token_for('AlmaLinux', '9.2'), 'almalinux_9')
        self.assertEqual(preflight.os_token_for('openEuler', '24.03'), 'openeuler_24')
        self.assertEqual(preflight.os_token_for('Debian', '12'), 'debian_12')

    def test_token_from_os_id(self):
        """直接传 ID 也能得到同样的 token。"""
        self.assertEqual(preflight.os_token_for('rocky', '8'), 'rocky_8')
        self.assertEqual(preflight.os_token_for('opensuse-leap', '15'), 'opensuse_leap_15')

    def test_token_empty_for_unknown_system(self):
        """未知系统或空版本返回空串，不抛异常。"""
        self.assertEqual(preflight.os_token_for('自研系统', '1'), '')
        self.assertEqual(preflight.os_token_for('Rocky Linux', ''), '')


class SupportedSystemTokensTest(TestCase):
    """制品模块不可用时的降级行为测试。"""

    def test_import_error_returns_none_and_reason(self):
        """制品模块导入失败时返回 (None, 可读原因)，不把异常抛给调用方。"""
        # sys.modules 里放 None 会让 import 语句直接抛 ImportError，与模块缺失等价。
        with mock.patch.dict(sys.modules, {'k8s_deployment.services.artifacts': None}):
            tokens, error = preflight._supported_system_tokens('3.6.7')
        self.assertIsNone(tokens)
        self.assertIn('制品模块', error)

    def test_tokens_are_normalized(self):
        """制品模块返回的 token 统一小写去空白。"""
        # 往 sys.modules 里塞一个假制品模块，无论真实模块是否已落地都能稳定测试。
        fake_module = types.ModuleType('k8s_deployment.services.artifacts')
        fake_module.supported_system_tokens = lambda version: [' Rocky_8 ', 'ubuntu_22']
        with mock.patch.dict(sys.modules, {'k8s_deployment.services.artifacts': fake_module}):
            tokens, error = preflight._supported_system_tokens('3.6.7')
        self.assertEqual(error, '')
        self.assertEqual(tokens, {'rocky_8', 'ubuntu_22'})
