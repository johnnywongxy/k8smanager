"""清掉节点上已退出容器的用例：命令拼装、JSON 解析与远程删除。

真实背景：集群装完之后 ``crictl ps -a`` 里永远挂着几行 ``Exited`` —— calico-node 的
``install-cni``、``mount-bpffs`` 这类 initContainer 跑完就退，容器对象却被 containerd
一直留着（见 ``services/node_containers.py`` 开头）。

命令的拼装与解析是纯函数；远程动作经 ``client_factory`` 注入假客户端。这个假客户端把节点上的
容器清单放在内存里、并按生成的命令真的删掉几个，因此「列出 → 挑出已退出的 → 删除 → 计数」
这条链路是被真的跑通的，不只是断言命令字符串。
"""

import json
import re

from django.test import SimpleTestCase

from k8s_deployment.services import node_containers

from .test_node_path import RecordingContext
from .test_node_registry import _Stream, make_node

KUBEASZ_BIN_DIR = '/opt/kube/bin'


def container(identifier, name='install-cni', pod='calico-node-6x6qd', namespace='kube-system',
              state='CONTAINER_EXITED', restart='0'):
    """造一条 ``crictl ps -o json`` 里的容器记录。

    参数：`identifier` 表示容器 id；`name` 表示容器名；`pod` 表示所属 Pod；`namespace` 表示命名空间；
        `state` 表示容器状态；`restart` 表示重启次数。
    返回：返回字典。
    副作用：只做纯计算。

    字段照抄真实输出（114.67.89.90 上 ``crictl ps -a --state exited -o json`` 的实测结果）。
    """
    return {
        'id': identifier,
        'podSandboxId': 'c13d2690232a9574ad74d054fe5b8bf2f9ca4ae711837d3bc9866b77e3652003',
        'metadata': {'name': name, 'attempt': 0},
        'state': state,
        'labels': {
            'io.kubernetes.container.name': name,
            'io.kubernetes.pod.name': pod,
            'io.kubernetes.pod.namespace': namespace,
            'io.kubernetes.pod.uid': 'cb7d5926-a9b1-499e-8fd9-fc2712ddd48d',
        },
        'annotations': {'io.kubernetes.container.restartCount': restart},
    }


def ps_output(containers):
    """把容器记录包成 ``crictl ps -o json`` 的输出（含前面那几行 WARN）。"""
    body = json.dumps({'containers': list(containers)}, indent=2, ensure_ascii=False)
    return f'WARN[0000] image connect using default endpoints: [unix:///run/containerd/containerd.sock]\n{body}'


INSTALL_CNI = ('e' * 8) + '0690f26d4e8cb64d080ee15772855be85efcf8731e530ddb2d2ead31f80e8e2'
MOUNT_BPFFS = ('e' * 8) + '06ffa98213fbf98fb37881c6fd2642a8d785b98778a36fad964cbb74b1d2e37'


class FakeRuntime:
    """把节点上的容器清单放进内存，让 ``crictl`` 命令真的删到东西。

    只认被测代码会生成的那两条命令形状（``ps -a ... -o json`` 与 ``rm --keep-logs ...``），
    认不出来就回 127 —— 命令改了而假客户端没跟着改时用例会立刻失败，而不是静默放过。
    """

    def __init__(self, containers=None, list_code=0, list_error='', remove_code=0,
                 remove_error='', echo=None, raw_list=None):
        """初始化。

        参数：`containers` 表示初始的容器记录；`list_code` 表示列容器命令的退出码；
            `list_error` 表示列容器命令的标准错误；`remove_code` 表示删除命令的退出码；
            `remove_error` 表示删除命令的标准错误；`echo` 表示删除失败时假装已经删掉的 id；
            `raw_list` 表示直接顶替列容器命令的原始输出。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.containers = [dict(item) for item in (containers or [])]
        self.list_code = list_code
        self.list_error = list_error
        self.remove_code = remove_code
        self.remove_error = remove_error
        self.echo = echo
        self.raw_list = raw_list
        self.commands = []

    def exec_command(self, command, timeout=None):
        """按命令文本在内存清单上执行一次。

        参数：`command` 表示命令文本；`timeout` 表示超时秒数（忽略）。
        返回：返回 ``(None, stdout, stderr)``。
        副作用：可能改动 ``containers``。
        """
        self.commands.append(command)
        code, out, err = self._run(command)
        return None, _Stream(out, code), _Stream(err, code)

    def _run(self, command):
        """命令的实际实现，返回 ``(退出码, 标准输出, 标准错误)``。"""
        if ' ps -a ' in command:
            if self.raw_list is not None:
                return 0, self.raw_list, ''
            if self.list_code:
                return self.list_code, '', self.list_error
            return 0, ps_output(self.containers), ''
        match = re.search(r'rm --keep-logs ([^;]*)', command)
        if match:
            identifiers = match.group(1).split()
            if self.remove_code:
                done = [item for item in identifiers if item in (self.echo or [])]
                return self.remove_code, '\n'.join(done), self.remove_error
            for item in identifiers:
                self.containers = [row for row in self.containers if row['id'] != item]
            return 0, '\n'.join(identifiers), ''
        return 127, '', f'command not found: {command}'

    def close(self):
        """关闭连接（假实现只记一笔）。"""
        self.closed = True

    def removals(self):
        """返回所有删除命令。"""
        return [item for item in self.commands if 'rm --keep-logs' in item]


class ParseExitedTest(SimpleTestCase):
    """``parse_exited``：把 ``crictl`` 的 JSON 变成能删的清单。"""

    def test_reads_the_container_identity(self):
        """容器名、Pod、命名空间都要取到：失败提示里要靠它说清删了什么。"""
        items = node_containers.parse_exited(ps_output([container(INSTALL_CNI)]))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], INSTALL_CNI)
        self.assertEqual(items[0]['name'], 'install-cni')
        self.assertEqual(items[0]['pod'], 'calico-node-6x6qd')
        self.assertEqual(items[0]['namespace'], 'kube-system')

    def test_keeps_only_exited_containers(self):
        """正在跑的容器一个都不能碰 —— 宁可少删，不能把活着的删掉。"""
        text = ps_output([
            container(INSTALL_CNI, state='CONTAINER_RUNNING'),
            container(MOUNT_BPFFS, name='mount-bpffs'),
        ])

        items = node_containers.parse_exited(text)

        self.assertEqual([item['id'] for item in items], [MOUNT_BPFFS])

    def test_skips_containers_without_a_usable_id(self):
        """id 缺失或含 shell 特殊字符的一律跳过：它要拼进命令行，不能靠运气。"""
        text = ps_output([
            container('$(reboot)'),
            container('ab'),
            container(''),
            container(INSTALL_CNI),
        ])

        items = node_containers.parse_exited(text)

        self.assertEqual([item['id'] for item in items], [INSTALL_CNI])

    def test_empty_output_means_nothing_to_do(self):
        """``crictl`` 在结果为空时不一定打印什么，空输出不是错误。"""
        self.assertEqual(node_containers.parse_exited(''), [])
        self.assertEqual(node_containers.parse_exited('   \n'), [])

    def test_unparsable_output_is_an_error(self):
        """有 JSON 却读不懂（命令变了、运行时坏了）要说出来，但不能让部署失败。"""
        with self.assertRaises(ValueError):
            node_containers.parse_exited('{"containers": [ oops')

    def test_json_that_is_not_an_object_is_an_error(self):
        """顶层不是对象：多半不是 ``crictl ps`` 的输出，硬着头皮读下去只会删错东西。"""
        with self.assertRaises(ValueError):
            node_containers.parse_exited('["a"]')

    def test_missing_containers_key_is_empty(self):
        """老版本上 ``containers`` 可能整段缺席。"""
        self.assertEqual(node_containers.parse_exited('{"containers": []}'), [])
        self.assertEqual(node_containers.parse_exited('{}'), [])

    def test_describe_names_the_pod(self):
        """日志里的一行要能定位到 Pod 与容器。"""
        item = node_containers.parse_exited(ps_output([container(INSTALL_CNI)]))[0]

        self.assertEqual(node_containers.describe(item), 'kube-system/calico-node-6x6qd:install-cni')


class CommandTest(SimpleTestCase):
    """命令拼装：找哪个 ``crictl``、删的时候带什么参数。"""

    def test_looks_in_the_bin_directory_first(self):
        """kubeasz 把 crictl 拷在 ``bin_dir`` 下；那台机器上未必有别的 crictl。"""
        command = node_containers.list_command(KUBEASZ_BIN_DIR)

        self.assertTrue(command.startswith(f'if [ -x {KUBEASZ_BIN_DIR}/crictl ]; then'), command)
        self.assertIn(f'{KUBEASZ_BIN_DIR}/crictl ps -a --state exited -o json', command)

    def test_falls_back_to_path(self):
        """目录里没有（例如全新的节点还没跑过 prepare）就退回 PATH 上的那个。"""
        command = node_containers.list_command('')

        self.assertEqual(command, 'crictl ps -a --state exited -o json')

    def test_only_lists_exited_containers(self):
        """在节点上就筛掉正在跑的：全量拉回来再筛，既费带宽又给删错留了口子。"""
        command = node_containers.list_command(KUBEASZ_BIN_DIR)

        self.assertIn('--state exited', command)
        self.assertNotIn('--all', command)
        # ``-a`` 只是把已退出的也算进来，真正决定范围的是 ``--state``。
        self.assertIn('ps -a --state exited', command)

    def test_removal_keeps_the_logs(self):
        """``--keep-logs`` 是这条命令的重点：容器删掉，日志留下。"""
        command = node_containers.remove_command(KUBEASZ_BIN_DIR, [INSTALL_CNI, MOUNT_BPFFS])

        self.assertIn('rm --keep-logs', command)
        self.assertIn(INSTALL_CNI, command)
        self.assertIn(MOUNT_BPFFS, command)


class CleanupNodeTest(SimpleTestCase):
    """``cleanup_node``：连上节点、列出、删掉，全程不抛错。"""

    def setUp(self):
        """准备节点快照与上下文。"""
        self.node = make_node('114.67.89.90', cluster_ip='172.30.0.16', roles=['master'])
        self.context = RecordingContext()

    def cleanup(self, client, bin_dir=KUBEASZ_BIN_DIR, **overrides):
        """在打桩前提下跑一次清理。

        参数：`client` 表示假客户端；`bin_dir` 表示二进制目录；`overrides` 表示覆盖的调用参数。
        返回：返回报告字典。
        副作用：在假节点上执行被记录下来的命令。
        """
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_containers.cleanup_node(
            self.context, self.node, bin_dir, {'user': 'root', 'port': 22}, **kwargs,
        )

    def test_removes_every_exited_container(self):
        """两个 initContainer 都该被删掉，报告里要能看出删了几个。"""
        client = FakeRuntime(containers=[container(INSTALL_CNI), container(MOUNT_BPFFS, name='mount-bpffs')])

        report = self.cleanup(client)

        self.assertEqual(report['found'], 2)
        self.assertEqual(report['removed'], 2)
        self.assertEqual(report['error'], '')
        self.assertEqual(client.containers, [])
        self.assertIn(INSTALL_CNI, client.removals()[0])

    def test_uses_the_inventory_bin_dir(self):
        """目录跟着 inventory 走：操作员把 ``bin_dir`` 改到数据盘上时也要找得到。"""
        client = FakeRuntime(containers=[container(INSTALL_CNI)])

        self.cleanup(client, bin_dir='/data/kube/bin')

        self.assertIn('/data/kube/bin/crictl ps -a --state exited -o json', client.commands[0])

    def test_nothing_to_do_issues_no_removal(self):
        """没有已退出的容器时一条删除命令都不发 —— 这个是部署收尾，不该有多余动作。"""
        client = FakeRuntime(containers=[])

        report = self.cleanup(client)

        self.assertEqual((report['found'], report['removed']), (0, 0))
        self.assertEqual(client.removals(), [])

    def test_idempotent(self):
        """第二次执行时已经没有可删的：重复跑不会报错，也不会删到别的东西。"""
        client = FakeRuntime(containers=[container(INSTALL_CNI)])

        first = self.cleanup(client)
        second = self.cleanup(client)

        self.assertEqual(first['removed'], 1)
        self.assertEqual(second['found'], 0)
        self.assertEqual(len(client.removals()), 1)

    def test_listing_failure_is_reported_not_raised(self):
        """列不出来（没有 crictl、运行时没起来）只写进报告：这是收尾打扫，不是部署成败。"""
        client = FakeRuntime(list_code=127, list_error='crictl: command not found')

        report = self.cleanup(client)

        self.assertIn('command not found', report['error'])
        self.assertEqual(report['removed'], 0)

    def test_unparsable_output_is_reported_not_raised(self):
        """输出读不懂时同样只写报告，绝不抛到调用方去。"""
        client = FakeRuntime(raw_list='oops, not json at all')

        report = self.cleanup(client)

        self.assertIn('JSON', report['error'])
        self.assertEqual(client.removals(), [])

    def test_partial_failure_counts_what_was_removed(self):
        """部分失败时：删除命令的退出码非零，但回显出来的那几个确实已经删掉了。"""
        client = FakeRuntime(
            containers=[container(INSTALL_CNI), container(MOUNT_BPFFS, name='mount-bpffs')],
            remove_code=1, remove_error='container is running', echo=[INSTALL_CNI],
        )

        report = self.cleanup(client)

        self.assertEqual(report['found'], 2)
        self.assertEqual(report['removed'], 1)
        self.assertIn('container is running', report['error'])

    def test_large_lists_are_removed_in_batches(self):
        """容器多的时候分批删：一次带几千个 id 会把命令行撑爆。"""
        many = [container(f'{index:064x}') for index in range(node_containers.MAX_BATCH + 3)]
        client = FakeRuntime(containers=many)

        report = self.cleanup(client)

        self.assertEqual(report['removed'], len(many))
        self.assertEqual(len(client.removals()), 2)
        self.assertEqual(client.containers, [])

    def test_log_says_the_logs_are_kept(self):
        """删了容器但留着日志，这件事必须写在日志里 —— 否则没人知道还有什么可查。"""
        client = FakeRuntime(containers=[container(INSTALL_CNI)])

        self.cleanup(client)

        text = self.context.text()
        self.assertIn('install-cni', text)
        self.assertIn('/var/log/pods', text)

    def test_long_lists_are_elided_in_the_log(self):
        """容器很多时日志只列前几个：一行日志写几十个名字没人看。"""
        many = [container(f'{index:064x}', name=f'job-{index}') for index in range(9)]
        client = FakeRuntime(containers=many)

        self.cleanup(client)

        text = self.context.text()
        self.assertIn('job-0', text)
        self.assertNotIn('job-8', text)
        self.assertIn('等 9 个', text)

    def test_nothing_to_do_writes_no_log(self):
        """无事发生时保持安静：每个节点一行「没有已退出的容器」是噪声。"""
        client = FakeRuntime(containers=[])

        self.cleanup(client)

        self.assertEqual(self.context.text(), '')
