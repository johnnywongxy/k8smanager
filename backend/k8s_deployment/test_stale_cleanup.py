"""清理「上一次安装的残留」的用例：删什么、留什么、清完怎么复验。

真实背景：94 上「111 / k8s-111」新建集群失败在 ``02.etcd`` —— 机器上还留着上一个集群的
``/var/lib/etcd``，新集群的 etcd 以 ``--initial-cluster-state=new`` 启动却被这份旧数据接管，
读到比自己新的数据版本后 ``fatal: invalid downgrade`` 直接退出。预检把这件事查出来
（``preflight._check_stale_install``），本模块负责在部署前把它清掉。

这里**不断言整条命令的全文**：上游随便加一个分号都会让用例失配。真正要守住的是三件事 ——
删的清单里有没有混进不该删的（尤其是容器运行时的镜像缓存）、命令在残缺状态下是不是还能跑完、
以及回执读不出来时会不会被当成「清干净了」放过去。
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from k8s_deployment.models import K8sDeployment
from k8s_deployment.services import preflight, stale_cleanup

from .test_node_network import RecordingContext
from .test_node_registry import _Stream, make_node


def make_deployment(**overrides):
    """构造一个未落库的部署任务，字段按新建集群的常态给。

    参数：`**overrides` 表示要覆盖的字段。
    返回：返回未保存的 K8sDeployment 实例。
    副作用：只在本机对象上设置字段，不写数据库。

    不落库：这些用例只关心命令文本与逐台流程，用不着数据库；而 ``SimpleTestCase``
    也正好挡住「顺手 save 一下」这种把用例变慢的写法。
    """
    values = {
        'display_name': '残留清理测试',
        'slug': 'stale-cleanup-test',
        'kubernetes_version': '1.29',
        'kubeasz_version': '3.6.7',
        'os_type': 'Rocky Linux',
        'data_dir_enabled': False,
        'data_dir': '',
        'cni_plugin': 'calico',
    }
    values.update(overrides)
    return K8sDeployment(**values)


class CleanupCommandTest(SimpleTestCase):
    """``cleanup_command``：删什么、留什么。"""

    def setUp(self):
        """准备一个默认参数的任务。"""
        self.deployment = make_deployment()

    def test_removes_the_etcd_data_dir_and_cluster_config(self):
        """etcd 数据是这次的病根，集群配置与证书是重新签发前的旧账，都要删。"""
        command = stale_cleanup.cleanup_command(self.deployment)
        for path in (
            preflight.DEFAULT_ETCD_DATA_DIR,
            '/etc/etcd',
            '/etc/kubernetes',
            '/var/lib/kubelet',
            '/etc/crictl.yaml',
            '/etc/keepalived',
            '/etc/l4lb',
        ):
            self.assertIn(path, command)

    def test_removes_every_kubeasz_unit(self):
        """少停一个服务就留一个占着端口的老进程，下一次部署起不来。"""
        command = stale_cleanup.cleanup_command(self.deployment)
        for unit in stale_cleanup.UNIT_NAMES:
            self.assertIn(unit, command)
        self.assertIn('systemctl stop', command)
        self.assertIn('systemctl disable', command)
        self.assertIn('systemctl daemon-reload', command)

    def test_services_are_stopped_before_files_are_removed(self):
        """先停服务再删文件：服务还在跑的时候删数据目录，进程会把文件重新写出来。"""
        command = stale_cleanup.cleanup_command(self.deployment)
        self.assertLess(command.index('systemctl stop'), command.index('rm -rf'))

    def test_removes_the_kubeasz_prepare_marker(self):
        """kubeasz 的「prepare 已跑过」标记必须跟着 ``/root/.kube`` 一起删。

        这是 2026-10-12 JD-k8s（114.67.203.82）那次的病根：只删了 kubeasz 的产物、留下了它
        「我来过」的标记，于是紧接着的 ``01.prepare`` 里 prepare 角色整段跳过
        （``ok=3 changed=0 skipped=154``），节点上的 kubectl 配置再也拿不回来 ——
        集群装得好好的，人登上去敲 ``kubectl`` 却只会连 ``localhost:8080``。
        规范出处是 kubeasz 自己的清理清单（``roles/clean/tasks/clean_node.yml`` 第 25、26 行），
        那两行也是连在一起列的。
        """
        paths = stale_cleanup.removable_paths(self.deployment)
        self.assertIn(stale_cleanup.PREPARE_MARKER, paths)
        self.assertIn('/root/.kube', paths)
        self.assertIn(stale_cleanup.PREPARE_MARKER, stale_cleanup.cleanup_command(self.deployment))

    def test_prepare_marker_is_also_verified(self):
        """标记也要进 ``LEFT`` 复验：没删掉时得报出来，不能当清干净放过去。

        命令里删一次、复验循环里再判一次，所以至少要出现两次。
        """
        command = stale_cleanup.cleanup_command(self.deployment)
        self.assertEqual(command.count(stale_cleanup.PREPARE_MARKER), 2)

    def test_prepare_marker_does_not_take_the_binaries_with_it(self):
        """删的是标记文件本身，不是 ``/opt/kube`` —— 那底下是 kubeasz 装的 kubectl 与镜像。"""
        command = stale_cleanup.cleanup_command(self.deployment)
        self.assertNotIn('/opt/kube/bin', command)
        self.assertNotIn('rm -rf /opt/kube ', command)

    def test_image_cache_is_never_deleted(self):
        """容器运行时的镜像缓存必须留住 —— 这是用户明确的口径。

        删掉它不会让部署更可靠，只会让下一次部署把所有镜像重新拉一遍。同机上与 k8s 无关的
        docker 容器也靠它活着。这里同时从两个角度守：删除清单里没有这些路径，整条命令里
        也不出现 ``rm -rf /var/lib/docker`` 这种写法。
        """
        paths = stale_cleanup.removable_paths(self.deployment)
        for kept in ('/var/lib/docker', '/var/lib/containerd', '/etc/containerd'):
            self.assertNotIn(kept, paths)
        command = stale_cleanup.cleanup_command(self.deployment)
        self.assertNotIn('/var/lib/docker', command)
        self.assertNotIn('rm -rf /var/lib/containerd', command)
        self.assertNotIn('rm -rf /etc/containerd', command)
        # containerd 只在「卸载挂载点」那一步出现：kubelet 与 containerd 的 k8s 命名空间挂载
        # 不先卸载，rm -rf 只会删掉挂载点里的内容却删不掉目录。
        self.assertIn('/var/lib/containerd/io.containerd', command)

    def test_hosts_file_is_left_alone(self):
        """``/etc/hosts`` 不动：kubeasz 的 01.prepare 会重写它自己那块主机名解析。"""
        self.assertNotIn('/etc/hosts', stale_cleanup.cleanup_command(self.deployment))

    def test_command_survives_partial_states(self):
        """全程容错：集群装到一半、上次清理崩在中间时也要跑到最后，不能半途退出。"""
        command = stale_cleanup.cleanup_command(self.deployment)
        # 删除是独立的一条一条来，不是「前面成了才做后面」。
        self.assertIn('rm -rf', command)
        self.assertNotIn(' && rm -rf', command)
        self.assertIn('2>/dev/null', command)

    def test_command_echoes_a_verdict(self):
        """末尾必须打出回执，调用方据此判断还有没有残留。"""
        command = stale_cleanup.cleanup_command(self.deployment)
        self.assertIn('echo "LEFT:', command)
        self.assertIn(stale_cleanup.CLEAN_OK, command)
        self.assertIn(stale_cleanup.CLEAN_PARTIAL, command)

    def test_custom_data_dir_is_used(self):
        """启用自定义数据目录时删的是那个目录，默认路径不再出现。"""
        deployment = make_deployment(data_dir_enabled=True, data_dir='/data/k8s')
        command = stale_cleanup.cleanup_command(deployment)
        self.assertIn('/data/k8s/etcd', command)
        self.assertNotIn(f'rm -rf {preflight.DEFAULT_ETCD_DATA_DIR}', command)

    def test_cni_specific_paths_follow_the_plugin(self):
        """按集群选的插件决定多删哪几个目录，别把另一套插件的目录也带上。"""
        calico = stale_cleanup.cleanup_command(make_deployment(cni_plugin='calico'))
        self.assertIn('/var/lib/calico', calico)
        self.assertNotIn('/run/flannel', calico)
        flannel = stale_cleanup.cleanup_command(make_deployment(cni_plugin='flannel'))
        self.assertIn('/run/flannel', flannel)
        self.assertNotIn('/var/lib/calico', flannel)

    def test_illegal_data_dir_falls_back_to_the_default(self):
        """数据目录是用户输入且会被拼进 shell：非法值退回默认路径，绝不原样下发。"""
        deployment = make_deployment(data_dir_enabled=True, data_dir='/data; rm -rf /')
        self.assertEqual(stale_cleanup.removable_paths(deployment)[0], preflight.DEFAULT_ETCD_DATA_DIR)
        self.assertNotIn('/data; rm -rf /', stale_cleanup.cleanup_command(deployment))

    def test_verify_reuses_the_preflight_probe(self):
        """复验用预检那一套探针：判据只有一处定义，才不会出现两边口径不一致。"""
        self.assertEqual(
            stale_cleanup.verify_command(self.deployment),
            preflight.stale_probe_command(self.deployment),
        )


class ParseResultTest(SimpleTestCase):
    """``parse_result``：回执解析与读不出来时的降级。"""

    def test_clean_node(self):
        """没有残留的节点：``LEFT`` 为空并打出清理成功标记。"""
        result = stale_cleanup.parse_result(f'LEFT:\n{stale_cleanup.CLEAN_OK}')
        self.assertEqual(result, {'left': [], 'clean': True})

    def test_leftovers_are_named(self):
        """有残留时要把路径带回来 —— 操作员看到路径就知道该去查什么。"""
        result = stale_cleanup.parse_result(
            f'LEFT: /etc/etcd /var/lib/etcd\n{stale_cleanup.CLEAN_PARTIAL}'
        )
        self.assertEqual(result['left'], ['/etc/etcd', '/var/lib/etcd'])
        self.assertFalse(result['clean'])

    def test_truncated_output_is_not_a_success(self):
        """输出被截断（两个标记都没读到）时按「没清干净」处理。

        这是不可逆的删除动作，读不出来就该让人来看，而不是当成功放过去。
        """
        for text in ('', None, 'LEFT: /etc/etcd\n'):
            self.assertFalse(stale_cleanup.parse_result(text)['clean'], repr(text))

    def test_whitespace_is_tolerated(self):
        """每行都 strip：ssh 与 ansible 都可能带上多余空白。"""
        result = stale_cleanup.parse_result(f'  LEFT:  /etc/etcd \n  {stale_cleanup.CLEAN_PARTIAL}  ')
        self.assertEqual(result['left'], ['/etc/etcd'])
        self.assertFalse(result['clean'])


class FakeSSHClient:
    """假的 SSH 客户端：按命令内容给出预设输出，并记下收到的每一条命令。"""

    def __init__(self, responder):
        """初始化。

        参数：`responder` 表示 ``(command) -> (code, out, err)``。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        self.responder = responder
        self.commands = []
        self.closed = False

    def exec_command(self, command, timeout=None):
        """记录并「执行」一条命令。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回与 paramiko 同形的 ``(stdin, stdout, stderr)``。
        副作用：把命令记进 ``commands``。
        """
        self.commands.append(command)
        code, out, err = self.responder(command)
        return None, _Stream(out, code), _Stream(err, code)

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True


def probe_output(present=None):
    """构造一段全 ABSENT、或指定路径 PRESENT 的残留探针输出。

    参数：`present` 表示 ``{路径: 条目数}``，留空表示机器干净。
    返回：返回探针的完整输出文本。
    副作用：只做纯计算。
    """
    present = present or {}
    paths = (
        f'{preflight.DEFAULT_ETCD_DATA_DIR}/member',
        f'{preflight.ETCD_SSL_DIR}/ca.pem',
        preflight.KEEPALIVED_CONF,
    )
    lines = [preflight.STALE_PROBE_BEGIN]
    for path in paths:
        if path in present:
            lines.append(f'PRESENT:{path}:dir:{present[path]}')
        else:
            lines.append(f'ABSENT:{path}')
    lines.append(preflight.STALE_PROBE_END)
    return '\n'.join(lines) + '\n'


class RunStaleCleanupTest(SimpleTestCase):
    """``run_stale_cleanup``：逐台清理的结果、复验与失败上报。"""

    def setUp(self):
        """准备上下文、任务与两台节点。"""
        self.context = RecordingContext()
        self.deployment = make_deployment()
        self.nodes = [make_node('192.168.1.94', node_id=1), make_node('192.168.1.95', node_id=2)]
        self.credentials = {
            '192.168.1.94': {'user': 'root', 'port': 22},
            '192.168.1.95': {'user': 'root', 'port': 22},
        }

    def make_factory(self, leftover=None, clean_code=0):
        """造一个假连接工厂。

        参数：`leftover` 表示复验时仍然探到的路径集合（``None`` 表示清得干净）；
            `clean_code` 表示清理命令的退出码。
        返回：返回 ``(factory, clients)``，``clients`` 按连接顺序记录假客户端。
        副作用：只构造对象。

        清理命令与复验探针共用一条连接（就是 ``_clean_one`` 的开销），另外两个清理动作
        （虚拟网卡、镜像仓库）各自再开一条 —— 这里一并交给同一个假客户端应答，用得上的
        只有前面两条命令。
        """
        clients = []
        hosts = []

        def responder(command):
            if preflight.STALE_PROBE_BEGIN in command:
                return 0, probe_output(leftover), ''
            if stale_cleanup.CLEAN_OK in command or 'rm -rf' in command:
                if clean_code != 0:
                    return clean_code, '', 'rm: cannot remove'
                return 0, f'LEFT:\n{stale_cleanup.CLEAN_OK}', ''
            return 0, '', ''

        def factory(**kwargs):
            client = FakeSSHClient(responder)
            client.host = kwargs.get('host')
            clients.append(client)
            hosts.append(client.host)
            return client

        factory.hosts = hosts
        return factory, clients

    def run_cleanup(self, targets, **kwargs):
        """用假连接跑一次清理。

        参数：`targets` 表示要清理的节点 IP 列表；`**kwargs` 传给 ``make_factory``。
        返回：返回 ``(报告列表, 假客户端列表)``。
        副作用：调用被测函数。
        """
        factory, clients = self.make_factory(**kwargs)
        reports = stale_cleanup.run_stale_cleanup(
            self.context, self.deployment, self.nodes, self.credentials, targets,
            client_factory=factory,
        )
        return reports, clients

    def test_clean_node_is_reported_with_the_removed_paths(self):
        """清干净时返回报告：删了哪些路径要一条条记下来，这是操作员核对的凭据。"""
        reports, _clients = self.run_cleanup(['192.168.1.94'])
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report['label'], '192.168.1.94')
        self.assertEqual(report['left'], [])
        self.assertIn(preflight.DEFAULT_ETCD_DATA_DIR, report['removed_paths'])
        self.assertIn('/etc/kubernetes', report['removed_paths'])
        self.assertEqual(report['warnings'], [])
        self.assertIn('已清理', self.context.text())

    def test_only_the_targets_are_touched(self):
        """预检没命中的机器一条连接都不开 —— 「没有残留就直接装」是用户要求的底线。"""
        factory, clients = self.make_factory()
        stale_cleanup.run_stale_cleanup(
            self.context, self.deployment, self.nodes, self.credentials, ['192.168.1.94'],
            client_factory=factory,
        )
        self.assertTrue(clients)
        self.assertNotIn('192.168.1.95', factory.hosts)
        self.assertTrue(all(client.closed for client in clients))

    def test_leftovers_after_cleanup_raise_with_the_paths(self):
        """清完复验仍有残留就判失败，并把机器与路径点名报出来，不把问题带到 02.etcd。"""
        path = f'{preflight.DEFAULT_ETCD_DATA_DIR}/member'
        with self.assertRaises(stale_cleanup.StaleCleanupError) as caught:
            self.run_cleanup(['192.168.1.94'], leftover={path: 4})
        self.assertEqual(caught.exception.error_code, 'stale_cleanup_failed')
        self.assertIn('192.168.1.94', caught.exception.message)
        self.assertIn(path, caught.exception.message)
        self.assertEqual(self.context.events[0]['type'], 'stale_cleanup')

    def test_command_failure_is_reported(self):
        """清理命令非零退出要如实报失败，不能靠复验「正好探不到」蒙混过去。"""
        with self.assertRaises(stale_cleanup.StaleCleanupError) as caught:
            self.run_cleanup(['192.168.1.94'], clean_code=1)
        self.assertIn('cannot remove', caught.exception.message)

    def test_all_targets_are_attempted_before_reporting(self):
        """一台失败不影响另一台：先把能清的都清掉，再把失败集中报出来。

        逐台清理而不是一遇错就退出，是为了让操作员一次就能看到全部要处理的地方，
        而不是修一台、重跑一次、再报下一台。
        """
        cleaned = []

        def factory(**kwargs):
            host = kwargs.get('host')

            def responder(command):
                if preflight.STALE_PROBE_BEGIN in command:
                    return 0, probe_output(), ''
                if 'rm -rf' in command:
                    # 第一台删不掉，第二台正常：命令文本对每台都一样，只能按连接的主机区分。
                    if host == '192.168.1.94':
                        return 1, '', 'rm: cannot remove'
                    cleaned.append(host)
                return 0, f'LEFT:\n{stale_cleanup.CLEAN_OK}', ''

            return FakeSSHClient(responder)

        with self.assertRaises(stale_cleanup.StaleCleanupError) as caught:
            stale_cleanup.run_stale_cleanup(
                self.context, self.deployment, self.nodes, self.credentials,
                ['192.168.1.94', '192.168.1.95'], client_factory=factory,
            )
        self.assertIn('192.168.1.94', caught.exception.message)
        self.assertNotIn('192.168.1.95', caught.exception.message)
        # 95 明明排在 94 后面，照样被清到了 —— 失败没有中断后面的机器。
        self.assertEqual(cleaned, ['192.168.1.95'])

    def test_unknown_target_is_reported_instead_of_silently_skipped(self):
        """节点清单里没有这台机器时要报出来，不能装作清理过。"""
        with self.assertRaises(stale_cleanup.StaleCleanupError) as caught:
            self.run_cleanup(['192.168.1.96'])
        self.assertIn('192.168.1.96', caught.exception.message)
        self.assertIn('不在本次任务的节点清单里', caught.exception.message)

    def test_connection_is_closed_on_every_path(self):
        """连上就要收尾：失败路径上漏掉 close，清理一批机器会把连接数耗光。"""
        _reports, clients = self.run_cleanup(['192.168.1.94'])
        self.assertTrue(all(client.closed for client in clients))

    def test_extra_cleanups_are_warned_not_failed(self):
        """虚拟网卡与镜像仓库清不掉只记警告：它们不影响重新部署。"""
        factory, clients = self.make_factory()

        def boom(context, node, credential, client_factory=None):
            raise RuntimeError('连接超时')

        with mock.patch.object(stale_cleanup.node_network, 'cleanup', boom), \
                mock.patch.object(stale_cleanup.node_registry, 'uninstall', boom):
            reports = stale_cleanup.run_stale_cleanup(
                self.context, self.deployment, self.nodes, self.credentials,
                ['192.168.1.94'], client_factory=factory,
            )
        self.assertEqual(len(reports[0]['warnings']), 2)
        self.assertIn('集群虚拟网卡', self.context.text())
        self.assertEqual(self.context.events[-1]['level'], 'warn')


class ModuleBoundaryTest(SimpleTestCase):
    """这个模块的边界：只清 k8s 自己管的路径。"""

    def test_removable_paths_start_with_the_etcd_data_dir(self):
        """第一条永远是 etcd 数据目录 —— 那是必须删掉、也是最该被看见的一项。"""
        deployment = make_deployment()
        self.assertEqual(
            stale_cleanup.removable_paths(deployment)[0], preflight.etcd_data_dir(deployment),
        )
        self.assertEqual(preflight.etcd_data_dir(deployment), preflight.DEFAULT_ETCD_DATA_DIR)

    def test_error_carries_a_code_and_a_chinese_reason(self):
        """异常与另外两个节点侧模块同形：错误码给机器读，message 给人读。"""
        error = stale_cleanup.StaleCleanupError('stale_cleanup_failed', '清理失败')
        self.assertEqual(error.error_code, 'stale_cleanup_failed')
        self.assertEqual(error.message, '清理失败')
        self.assertEqual(str(error), '清理失败')

    def test_docker_is_not_referenced_anywhere(self):
        """整条命令里不出现 docker：机器上可能有与 k8s 无关的容器在用同一套运行时。"""
        command = stale_cleanup.cleanup_command(make_deployment(runtime='docker'))
        self.assertNotIn('docker', command)
        self.assertNotIn('/var/lib/docker', command)


class LabellessNodeTest(SimpleTestCase):
    """节点快照缺字段时的降级：报错文案里的机器名要能退回主机名。"""

    def test_node_without_ip_uses_the_hostname(self):
        """``label`` 的取值链：IP 为空时退回主机名，日志里不能出现空白的机器名。"""

        def factory(**kwargs):
            return FakeSSHClient(lambda command: (1, '', 'rm: cannot remove'))

        node = SimpleNamespace(ip_address='', name='k8s-node-1', ssh_port=22, ssh_user='root')
        with self.assertRaises(stale_cleanup.StaleCleanupError) as caught:
            stale_cleanup.run_stale_cleanup(
                RecordingContext(), make_deployment(), [node], {'': {'user': 'root'}}, [''],
                client_factory=factory,
            )
        self.assertIn('k8s-node-1', caught.exception.message)
