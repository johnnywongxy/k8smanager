"""负载均衡机器上的两件事：调 l4lb(nginx) 的转发参数、打开同机要用的内核开关。

真实背景（转发参数）：kubeasz 的 ``roles/ex-lb`` 拿来当 L4 代理的是 nginx，配置模板里
``worker_processes 1;`` 与 ``proxy_connect_timeout 1s;`` 是**写死的**，而且**没有配
``proxy_timeout``** —— nginx 的默认值是 10 分钟，走 VIP 的 ``kubectl logs -f`` /
``exec -it`` / ``port-forward`` 会在静默 10 分钟后被无声掐断（见 ``services/node_l4lb.py``）。

真实背景（内核开关）：负载均衡与集群节点同机的机器上，apiserver 占着对外端口，l4lb
只能绑 VIP，而 VIP 是本机还没挂上的地址 —— 不开 ``net.ipv4.ip_nonlocal_bind`` 会直接
``EADDRNOTAVAIL``。这一项必须在那台机器上就位、且要能扛住重启（写 ``/etc/sysctl.d``），
因此幂等判据是「当前值对 **且** 持久化文件写对」，两者缺一都要重新做。

文本改写是纯函数，远程动作经 ``client_factory`` 注入假客户端；那个假客户端复用
``test_node_path`` 里那一份（读写走 base64、写入命令真的改到内存文件），只多认
「试配置 + 重载」与「写 sysctl 并回读」两条命令，因此这两条链路都是**真的被跑通**的，
不只是断言命令字符串。
"""

from unittest import mock

from django.test import SimpleTestCase

from k8s_deployment.services import node_l4lb

from .test_node_path import FakeNode, RecordingContext
from .test_node_registry import make_node

# 一份与真实渲染结果逐字同形的 l4lb.conf（三台 master + ingress 的 80 端口那一段都有，
# 因为「两个 server 块都要补 proxy_timeout」正是要钉住的行为之一）。
RENDERED = """\
user root;
worker_processes 1;

error_log  /etc/l4lb/logs/error.log warn;

events {
    worker_connections  3000;
}

stream {
    upstream apiservers {
        server 172.30.0.12:6443    max_fails=2 fail_timeout=3s;
        server 172.30.0.25:6443    max_fails=2 fail_timeout=3s;
        server 172.30.0.15:6443    max_fails=2 fail_timeout=3s;
    }

    server {
        listen 0.0.0.0:6443;
        proxy_connect_timeout 1s;
        proxy_pass apiservers;
    }

    upstream ingress-nodes {
       server 172.30.0.12:32080    max_fails=2 fail_timeout=3s;
       server 172.30.0.25:32080    max_fails=2 fail_timeout=3s;
    }

    server {
        listen 0.0.0.0:80;
        proxy_connect_timeout 1s;
        proxy_pass ingress-nodes;
    }
}
"""


class FakeLbNode(FakeNode):
    """在 ``test_node_path.FakeNode`` 之上多认一条「试配置 + 重载」的命令。

    别的命令（读文件、写文件）沿用父类的实现，因此「读出原文 → 改写 → 原子替换」这条
    链路走的是与生产同一份命令文本。
    """

    def __init__(self, files=None, unreadable=(), tune_output=None):
        """初始化。

        参数：`files` 表示初始文件内容；`unreadable` 表示读不出来的路径集合；
            `tune_output` 表示「试配置 + 重载」那条命令的输出，缺省为服务正常。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        super().__init__(files=files, unreadable=unreadable)
        self.tune_output = (
            f'{node_l4lb.STATE_PREFIX}active' if tune_output is None else tune_output
        )
        self.tune_calls = 0

    def _run(self, command):
        """命令的实际实现，返回 ``(退出码, 标准输出, 标准错误)``。"""
        if node_l4lb.BINARY_PATH in command:
            self.tune_calls += 1
            return 0, self.tune_output, ''
        return super()._run(command)


class FakeSysctlNode(FakeNode):
    """在 ``FakeNode`` 之上多认 ``bind_commands`` 那一条命令。

    效果照着真实机器来：跑完持久化文件真的被写上、``/proc`` 里的值真的变。给
    ``sysctl_ignored=True`` 时模拟「命令返回 0 但内核没认」—— 有些环境里 ``sysctl -w``
    会静默忽略（只读挂载、容器里没有对应 sysctl 命名空间等），此时回读值停在原处，
    而平台必须把它判成失败：这正是「回读」这一步存在的理由。
    """

    def __init__(self, files=None, unreadable=(), sysctl_ignored=False):
        """初始化。

        参数：`files` 表示初始文件内容；`unreadable` 表示读不出来的路径集合；
            `sysctl_ignored` 表示是否模拟「改了但没生效」。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        super().__init__(files=files, unreadable=unreadable)
        self.sysctl_ignored = sysctl_ignored
        self.bind_calls = 0

    def _run(self, command):
        """命令的实际实现，返回 ``(退出码, 标准输出, 标准错误)``。"""
        if command == node_l4lb.bind_commands():
            self.bind_calls += 1
            if self.sysctl_ignored:
                # 最后那条 ``cat`` 会成功、退出码为 0 —— 与真机上一样，
                # 「命令没报错」不等于「开关改成了」。
                return 0, str(self.files.get(node_l4lb.SYSCTL_KEY_PATH, '')) + '\n', (
                    f'sysctl: setting key "{node_l4lb.SYSCTL_KEY}": Read-only file system'
                )
            self.files[node_l4lb.SYSCTL_FILE] = (
                f'{node_l4lb.SYSCTL_KEY} = {node_l4lb.SYSCTL_VALUE}\n'
            )
            self.files[node_l4lb.SYSCTL_KEY_PATH] = node_l4lb.SYSCTL_VALUE
            return 0, f'{node_l4lb.SYSCTL_VALUE}\n', ''
        return super()._run(command)


class TunedContentTest(SimpleTestCase):
    """``tuned_content``：三处改写，一处都不能漏，也不能多改。"""

    def tune(self, text=RENDERED, **kwargs):
        """跑一次改写。"""
        return node_l4lb.tuned_content(text, **kwargs)

    def test_worker_processes_becomes_auto(self):
        """``1`` 换成 ``auto``：核数由机器定，写死数字在别的规格上一定不对。"""
        updated, info = self.tune()
        self.assertIn('worker_processes auto;', updated.splitlines())
        self.assertNotIn('worker_processes 1;', updated)
        self.assertTrue(info['changed'])

    def test_connect_timeout_is_raised(self):
        """1s → 3s：建连超时，1 秒在跨机房或机器繁忙时会误踢后端。"""
        updated, _info = self.tune()
        self.assertEqual(updated.count('proxy_connect_timeout 3s;'), 2)
        self.assertNotIn('proxy_connect_timeout 1s;', updated)

    def test_proxy_timeout_is_added_to_every_server_block(self):
        """两个 server 块都要补：只补 6443 那一块的话，走 80 端口的业务照样会被掐断。"""
        updated, _info = self.tune()
        self.assertEqual(updated.count('proxy_timeout 1h;'), 2)

    def test_proxy_timeout_keeps_the_indentation_of_its_block(self):
        """缩进跟着同块的 ``proxy_connect_timeout``，diff 里看着像人写的。"""
        updated, _info = self.tune()
        for line in updated.splitlines():
            if line.strip() == 'proxy_timeout 1h;':
                self.assertEqual(line, '        proxy_timeout 1h;')

    def test_upstream_server_lines_are_untouched(self):
        """``server <ip>:<port> max_fails=...`` 既不是那两条指令，也不该被碰到。"""
        updated, _info = self.tune()
        self.assertIn('        server 172.30.0.12:6443    max_fails=2 fail_timeout=3s;', updated)
        self.assertEqual(updated.count('max_fails=2 fail_timeout=3s;'), 5)

    def test_listen_and_proxy_pass_are_untouched(self):
        """只改该改的：这几行是 kubeasz 决定「转发到哪」的地方。"""
        updated, _info = self.tune()
        self.assertIn('        listen 0.0.0.0:6443;', updated)
        self.assertIn('        proxy_pass apiservers;', updated)
        self.assertIn('user root;', updated)

    def test_is_idempotent(self):
        """跑两次结果一样，第二次 ``changed`` 为假：重跑任务不该每次都弹一下 l4lb。"""
        once, _info = self.tune()
        twice, info = self.tune(once)
        self.assertEqual(twice, once)
        self.assertFalse(info['changed'])
        self.assertEqual(once.count('proxy_timeout 1h;'), 2)

    def test_commented_out_directive_is_left_alone(self):
        """注释掉的行是操作员的留档，不是配置。"""
        text = 'worker_processes 1;\n# worker_processes 8;\n'
        updated, _info = self.tune(text)
        self.assertIn('worker_processes auto;', updated)
        self.assertIn('# worker_processes 8;', updated)

    def test_existing_proxy_timeout_is_not_duplicated(self):
        """块里已经有 ``proxy_timeout`` 时不重复插一行：nginx 会为重复指令直接拒绝启动。"""
        text = (
            'stream {\n'
            '    server {\n'
            '        proxy_connect_timeout 1s;\n'
            '        proxy_timeout 30m;\n'
            '        proxy_pass apiservers;\n'
            '    }\n'
            '}\n'
        )
        updated, info = self.tune(text)
        self.assertEqual(updated.count('proxy_timeout'), 1)
        self.assertIn('proxy_timeout 30m;', updated)
        self.assertIn('proxy_connect_timeout 3s;', updated)
        self.assertTrue(info['changed'])

    def test_an_already_proxy_timeout_but_untuned_file_only_reports_the_other_two(self):
        """已经补过 ``proxy_timeout`` 但另外两处没改的文件，改完不该再报「新增」。"""
        text = RENDERED.replace(
            '        proxy_connect_timeout 1s;',
            '        proxy_connect_timeout 1s;\n        proxy_timeout 1h;',
        )
        _updated, info = self.tune(text)
        self.assertFalse([item for item in info['changes'] if '新增' in item])

    def test_proxy_timeout_goes_into_the_right_block(self):
        """插在 ``proxy_connect_timeout`` 的后面、``proxy_pass`` 的前面。"""
        text, _info = self.tune()
        lines = text.splitlines()
        index = lines.index('        proxy_connect_timeout 3s;')
        self.assertEqual(lines[index + 1], '        proxy_timeout 1h;')
        self.assertEqual(lines[index + 2], '        proxy_pass apiservers;')

    def test_trailing_newline_is_preserved_both_ways(self):
        """末尾换行照原样：这个文件归 kubeasz 渲染，平台改过之后不该换一种收尾。"""
        with_newline, _info = self.tune('worker_processes 1;\n')
        self.assertTrue(with_newline.endswith('\n'))
        without, _info = self.tune('worker_processes 1;')
        self.assertEqual(without, 'worker_processes auto;')

    def test_changes_are_described_in_chinese(self):
        """改动清单要能直接写进日志：操作员看日志就知道平台动了哪几个值。"""
        _updated, info = self.tune()
        self.assertIn('worker_processes 1 → auto', info['changes'])
        self.assertIn('proxy_connect_timeout 1s → 3s', info['changes'])
        self.assertIn('新增 proxy_timeout 1h', info['changes'])

    def test_empty_text_is_not_a_crash(self):
        """文件读回来是空的（渲染出问题时）不该抛异常。"""
        updated, info = self.tune('')
        self.assertEqual(updated, '')
        self.assertFalse(info['changed'])


class TuneCommandsTest(SimpleTestCase):
    """``tune_commands``：先试配置，过了才重载。"""

    def test_config_is_tested_before_reloading(self):
        """配置是坏的就不该重载 —— 先 reload 再发现配置坏，白弹一次服务。"""
        command = node_l4lb.tune_commands()
        self.assertLess(command.index('-t -c'), command.index('reload'))

    def test_failure_branch_prints_the_marker(self):
        """试配置失败时打标记，调用方据此把原文写回去。"""
        self.assertIn(f'else echo {node_l4lb.CONFIG_TEST_FAILED}; fi', node_l4lb.tune_commands())

    def test_state_is_reported_behind_a_prefix(self):
        """状态带前缀：``is-active`` 的 ``inactive`` 里也有 ``active``，子串匹配会判错。"""
        self.assertIn(f'{node_l4lb.STATE_PREFIX}$(systemctl is-active', node_l4lb.tune_commands())


class L4lbStateTest(SimpleTestCase):
    """``_l4lb_state``：从命令输出里取服务状态。"""

    def test_reads_the_state(self):
        """正常情况取出 ``active``。"""
        self.assertEqual(node_l4lb._l4lb_state('L4LB_STATE=active\n'), 'active')

    def test_inactive_is_not_mistaken_for_active(self):
        """``inactive`` 不是 ``active``。"""
        self.assertEqual(node_l4lb._l4lb_state('L4LB_STATE=inactive\n'), 'inactive')

    def test_missing_prefix_returns_empty(self):
        """没打出来（命令根本没跑起来）时返回空串，而不是猜一个。"""
        self.assertEqual(node_l4lb._l4lb_state('bash: systemctl: command not found\n'), '')


class SysctlLineMatchesTest(SimpleTestCase):
    """``_sysctl_line_matches``：判断持久化文件里那一项是不是已经设成目标值。"""

    def test_the_target_line_is_a_match(self):
        """照平台自己的写法写出来的一行要认。"""
        self.assertTrue(node_l4lb._sysctl_line_matches(
            f'{node_l4lb.SYSCTL_KEY} = {node_l4lb.SYSCTL_VALUE}\n',
        ))

    def test_extra_spaces_around_the_equals_sign_are_fine(self):
        """``sysctl -w`` 与手工编辑留下的空格差异不该影响判断。"""
        self.assertTrue(node_l4lb._sysctl_line_matches(
            f'  {node_l4lb.SYSCTL_KEY}={node_l4lb.SYSCTL_VALUE}   \n',
        ))

    def test_it_is_found_among_other_lines(self):
        """文件里还有别的设置时照样找得到这一行。"""
        text = 'vm.swappiness = 0\nnet.ipv4.ip_forward = 1\nnet.ipv4.ip_nonlocal_bind = 1\n'
        self.assertTrue(node_l4lb._sysctl_line_matches(text))

    def test_explicit_zero_is_not_a_match(self):
        """写着 ``= 0`` 是「明确关掉」，不是「已经设好」—— 按子串匹配就会看错这一行。"""
        self.assertFalse(node_l4lb._sysctl_line_matches(
            f'{node_l4lb.SYSCTL_KEY} = 0\n',
        ))

    def test_a_longer_value_is_not_a_match(self):
        """``= 10`` 之类要以整行比对，不能靠前缀。"""
        self.assertFalse(node_l4lb._sysctl_line_matches(
            f'{node_l4lb.SYSCTL_KEY} = 10\n',
        ))

    def test_commented_out_line_is_not_a_match(self):
        """注释掉的那一行是留档，不是生效的配置。"""
        self.assertFalse(node_l4lb._sysctl_line_matches(
            f'# {node_l4lb.SYSCTL_KEY} = {node_l4lb.SYSCTL_VALUE}\n',
        ))

    def test_another_key_is_not_a_match(self):
        """只认自己那一项：``ip_nonlocal_bind`` 之外的同族键不算。"""
        self.assertFalse(node_l4lb._sysctl_line_matches('net.ipv4.ip_forward = 1\n'))

    def test_empty_text_is_not_a_match(self):
        """文件不存在时读回来是空串，不能判成已设置。"""
        self.assertFalse(node_l4lb._sysctl_line_matches(''))
        self.assertFalse(node_l4lb._sysctl_line_matches(None))


class BindCommandsTest(SimpleTestCase):
    """``bind_commands``：当下生效与重启后生效，两件事一条命令里做完。"""

    def test_both_halves_are_there(self):
        """写 ``/etc/sysctl.d`` 管重启，``sysctl -w`` 管当下 —— 缺一不可。"""
        command = node_l4lb.bind_commands()
        self.assertIn(f'> {node_l4lb.SYSCTL_FILE}', command)
        self.assertIn(f'sysctl -w {node_l4lb.SYSCTL_KEY}={node_l4lb.SYSCTL_VALUE}', command)

    def test_the_right_hand_side_uses_semicolons(self):
        """三段之间用 ``;`` 而不是 ``&&``：``sysctl -w`` 失败时还要能读回真实值。"""
        command = node_l4lb.bind_commands()
        self.assertNotIn('&&', command)
        self.assertEqual(command.count(';'), 2)

    def test_the_value_is_read_back_last(self):
        """回读放最后：调用方只看最后一个值，它必须是内核当下的真实取值。"""
        command = node_l4lb.bind_commands()
        read_back = f'cat {node_l4lb.SYSCTL_KEY_PATH}'
        self.assertIn(read_back, command)
        self.assertLess(command.index('sysctl -w'), command.index(read_back))
        # 回读之后不该再有别的东西：调用方取的是最后一行。
        self.assertEqual(command.split(read_back)[-1].strip(), '2>/dev/null')

    def test_the_persistent_file_is_platform_owned(self):
        """写的是平台自己的文件，不去动发行版与 kubeasz 那几份。"""
        self.assertEqual(node_l4lb.SYSCTL_FILE, '/etc/sysctl.d/99-k8smanager-lb.conf')
        self.assertIn('k8smanager', node_l4lb.SYSCTL_FILE)

    def test_the_written_line_is_the_one_the_reader_recognizes(self):
        """写出去的那一行必须被自己的判断函数认出来，否则每次部署都要重写一遍文件。"""
        written = f'{node_l4lb.SYSCTL_KEY} = {node_l4lb.SYSCTL_VALUE}\n'
        self.assertIn(written.strip(), node_l4lb.bind_commands())
        self.assertTrue(node_l4lb._sysctl_line_matches(written))


class NonlocalBindStateTest(SimpleTestCase):
    """``nonlocal_bind_state``：当前值与管理文件两个都要看。"""

    def state(self, files=None):
        """读一次现状。"""
        return node_l4lb.nonlocal_bind_state(FakeSysctlNode(files=files or {}))

    def test_both_are_read(self):
        """当前值是 1、文件也写对 → ``('1', True)``。"""
        self.assertEqual(self.state({
            node_l4lb.SYSCTL_KEY_PATH: '1',
            node_l4lb.SYSCTL_FILE: f'{node_l4lb.SYSCTL_KEY} = 1\n',
        }), ('1', True))

    def test_a_live_value_without_the_file_is_not_persisted(self):
        """当下是 1 但文件没写：机器一重启就回到 0，那时候 VIP 再也起不来。"""
        self.assertEqual(self.state({node_l4lb.SYSCTL_KEY_PATH: '1'}), ('1', False))

    def test_a_written_file_that_has_not_taken_effect_yet(self):
        """文件写对了、当下还停在 0（``sysctl -w`` 没跑过或没生效）—— 两个值分开报，
        调用方据此仍然要动一次手。"""
        self.assertEqual(self.state({
            node_l4lb.SYSCTL_KEY_PATH: '0',
            node_l4lb.SYSCTL_FILE: f'{node_l4lb.SYSCTL_KEY} = 1\n',
        }), ('0', True))

    def test_an_unreadable_live_value_reports_empty(self):
        """``/proc`` 里没有这个键（内核不支持）时当前值报空串，而不是猜一个。"""
        self.assertEqual(self.state({
            node_l4lb.SYSCTL_FILE: f'{node_l4lb.SYSCTL_KEY} = 1\n',
        }), ('', True))

    def test_a_file_that_says_zero_is_not_persisted(self):
        """文件里明写着 ``= 0`` 视为没设好。"""
        self.assertEqual(self.state({
            node_l4lb.SYSCTL_KEY_PATH: '1',
            node_l4lb.SYSCTL_FILE: f'{node_l4lb.SYSCTL_KEY} = 0\n',
        }), ('1', False))

    def test_whitespace_around_the_live_value_is_trimmed(self):
        """``/proc`` 读回来的值带着换行，要剥掉。"""
        self.assertEqual(self.state({node_l4lb.SYSCTL_KEY_PATH: ' 1 \n'}), ('1', False))

    def test_read_failures_are_reported_as_empty_not_raised(self):
        """两个文件都读不出来时返回 ``('', False)``，不抛异常。"""
        client = FakeSysctlNode(
            files={node_l4lb.SYSCTL_KEY_PATH: '1'}, unreadable=[node_l4lb.SYSCTL_KEY_PATH],
        )
        self.assertEqual(node_l4lb.nonlocal_bind_state(client), ('', False))


class EnsureNonlocalBindTest(SimpleTestCase):
    """``ensure_nonlocal_bind``：同机的机器上必须打开它，改不动就抛错。"""

    def setUp(self):
        """准备同机节点与上下文。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上准备数据。
        """
        self.node = make_node('172.30.0.12', roles=['master', 'loadbalancer'])
        self.context = RecordingContext()

    def ensure(self, client, **overrides):
        """在打桩前提下跑一次。

        参数：`client` 表示假客户端；`overrides` 表示覆盖的调用参数。
        返回：返回报告字典。
        副作用：在假节点上执行被记录下来的命令。
        """
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_l4lb.ensure_nonlocal_bind(
            self.context, self.node, {'user': 'root', 'port': 22}, **kwargs,
        )

    def test_it_is_turned_on_and_persisted(self):
        """正常路径：命令跑过、``/proc`` 变成 1、``/etc/sysctl.d`` 文件落地。"""
        client = FakeSysctlNode(files={node_l4lb.SYSCTL_KEY_PATH: '0'})

        report = self.ensure(client)

        self.assertIs(report['changed'], True)
        self.assertEqual(report['value'], '1')
        self.assertEqual(report['label'], '172.30.0.12')
        self.assertEqual(client.bind_calls, 1)
        self.assertEqual(client.files[node_l4lb.SYSCTL_KEY_PATH], '1')
        self.assertTrue(node_l4lb._sysctl_line_matches(client.files[node_l4lb.SYSCTL_FILE]))

    def test_already_done_is_a_no_op(self):
        """当前值是 1 且文件写对时不碰机器：重跑任务不该每次都动一遍内核参数。"""
        client = FakeSysctlNode(files={
            node_l4lb.SYSCTL_KEY_PATH: '1',
            node_l4lb.SYSCTL_FILE: f'{node_l4lb.SYSCTL_KEY} = 1\n',
        })

        report = self.ensure(client)

        self.assertIs(report['changed'], False)
        self.assertEqual(client.bind_calls, 0)
        self.assertIn(node_l4lb.SYSCTL_KEY, self.context.text())

    def test_a_live_value_alone_is_not_enough(self):
        """当下是 1 但文件没写：仍然要跑一遍，把重启后的那一半补上。"""
        client = FakeSysctlNode(files={node_l4lb.SYSCTL_KEY_PATH: '1'})

        report = self.ensure(client)

        self.assertIs(report['changed'], True)
        self.assertEqual(client.bind_calls, 1)
        self.assertTrue(node_l4lb._sysctl_line_matches(client.files[node_l4lb.SYSCTL_FILE]))

    def test_a_file_that_says_zero_is_rewritten(self):
        """文件里写着 ``= 0``（有人手工关过）时会被改回来。"""
        client = FakeSysctlNode(files={
            node_l4lb.SYSCTL_KEY_PATH: '1',
            node_l4lb.SYSCTL_FILE: f'{node_l4lb.SYSCTL_KEY} = 0\n',
        })

        self.ensure(client)

        self.assertEqual(
            client.files[node_l4lb.SYSCTL_FILE],
            f'{node_l4lb.SYSCTL_KEY} = {node_l4lb.SYSCTL_VALUE}\n',
        )

    def test_readback_of_zero_is_an_error(self):
        """命令说成功、回读还是 0（内核没认）时必须抛错：这一步是可选的调优，
        但同机场景下它是部署能不能成的条件，静默放过会拖到剧本里才炸。"""
        client = FakeSysctlNode(
            files={node_l4lb.SYSCTL_KEY_PATH: '0'}, sysctl_ignored=True,
        )

        with self.assertRaises(node_l4lb.NodeL4lbError) as caught:
            self.ensure(client)

        self.assertEqual(caught.exception.error_code, 'node_nonlocal_bind_failed')

    def test_the_error_explains_the_consequence(self):
        """报错要说清为什么必须打开它，以及不打开会在哪一步炸。"""
        client = FakeSysctlNode(
            files={node_l4lb.SYSCTL_KEY_PATH: '0'}, sysctl_ignored=True,
        )

        with self.assertRaises(node_l4lb.NodeL4lbError) as caught:
            self.ensure(client)

        message = caught.exception.message
        self.assertIn(node_l4lb.SYSCTL_KEY, message)
        self.assertIn('VIP', message)
        self.assertIn('172.30.0.12', message)
        self.assertIn('Read-only file system', message)

    def test_an_empty_readback_is_an_error_too(self):
        """``/proc`` 里的那个文件不存在（内核不支持？）时读回来是空的，同样报错。"""
        client = FakeSysctlNode(sysctl_ignored=True)

        with self.assertRaises(node_l4lb.NodeL4lbError) as caught:
            self.ensure(client)

        self.assertIn('空', caught.exception.message)

    def test_the_report_names_the_host(self):
        """报告里带机器标识，逐台执行时的日志才分得清是哪一台。"""
        client = FakeSysctlNode(files={node_l4lb.SYSCTL_KEY_PATH: '0'})
        self.assertEqual(self.ensure(client)['label'], '172.30.0.12')

    def test_the_session_is_closed(self):
        """成功与失败都要关连接，别把 SSH 会话留在那里。"""
        client = FakeSysctlNode(files={node_l4lb.SYSCTL_KEY_PATH: '0'})
        self.ensure(client)
        self.assertTrue(client.closed)

        broken = FakeSysctlNode(files={node_l4lb.SYSCTL_KEY_PATH: '0'}, sysctl_ignored=True)
        with self.assertRaises(node_l4lb.NodeL4lbError):
            self.ensure(broken)
        self.assertTrue(broken.closed)

    def test_it_does_not_touch_the_l4lb_config(self):
        """两件事互不越界：这一步只管内核开关，不碰 ``l4lb.conf``。"""
        client = FakeSysctlNode(
            files={node_l4lb.SYSCTL_KEY_PATH: '0', node_l4lb.CONF_PATH: RENDERED},
        )

        self.ensure(client)

        self.assertEqual(client.files[node_l4lb.CONF_PATH], RENDERED)


class ApplyNodeTest(SimpleTestCase):
    """``apply_node``：文件在才改，改了要试，试不过要还回去。"""

    def setUp(self):
        """准备负载均衡机器快照与上下文。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上准备数据。
        """
        self.node = make_node('116.198.237.81', cluster_ip='172.30.0.16', roles=['loadbalancer'])
        self.context = RecordingContext()

    def apply(self, client, **overrides):
        """在打桩前提下跑一次。

        参数：`client` 表示假客户端；`overrides` 表示覆盖的调用参数。
        返回：返回报告字典。
        副作用：在假节点上执行被记录下来的命令。
        """
        kwargs = {'client_factory': lambda **_kw: client, 'key_path': 'k'}
        kwargs.update(overrides)
        return node_l4lb.apply_node(
            self.context, self.node, {'user': 'root', 'port': 22}, **kwargs,
        )

    def test_rewrites_the_file_and_reloads(self):
        """正常路径：文件改到、l4lb 重载过。"""
        client = FakeLbNode(files={node_l4lb.CONF_PATH: RENDERED})

        report = self.apply(client)

        self.assertIs(report['conf'], True)
        self.assertIs(report['reload'], True)
        self.assertEqual(client.tune_calls, 1)
        written = client.files[node_l4lb.CONF_PATH]
        self.assertIn('worker_processes auto;', written)
        self.assertIn('proxy_connect_timeout 3s;', written)
        self.assertIn('proxy_timeout 1h;', written)

    def test_reports_what_changed(self):
        """报告里带上改动清单，供调用方写日志。"""
        client = FakeLbNode(files={node_l4lb.CONF_PATH: RENDERED})

        report = self.apply(client)

        self.assertIn('worker_processes 1 → auto', report['changes'])

    def test_second_run_writes_nothing(self):
        """幂等：已经是平台口径时既不写盘也不重载。"""
        client = FakeLbNode(files={node_l4lb.CONF_PATH: RENDERED})
        self.apply(client)
        writes_before = client.fired('base64 -d >')
        client.tune_calls = 0

        report = self.apply(client)

        self.assertIs(report['conf'], True)
        self.assertEqual(client.tune_calls, 0)
        self.assertEqual(client.fired('base64 -d >'), writes_before)

    def test_missing_file_is_reported_not_raised(self):
        """文件不在（这一步跑在了 ex-lb 阶段之前）不抛异常，只回报 ABSENT。"""
        client = FakeLbNode()

        report = self.apply(client)

        self.assertEqual(report['conf'], 'ABSENT')
        self.assertEqual(client.tune_calls, 0)

    def test_unreadable_file_is_reported_not_raised(self):
        """读不出来（权限不够）同样只回报 ABSENT：改不动不是部署失败的理由。"""
        client = FakeLbNode(
            files={node_l4lb.CONF_PATH: RENDERED}, unreadable=[node_l4lb.CONF_PATH],
        )

        report = self.apply(client)

        self.assertEqual(report['conf'], 'ABSENT')

    def test_broken_config_is_rolled_back(self):
        """试配置不过就把原文写回去：留着坏文件的账要等下次重启才还。"""
        client = FakeLbNode(
            files={node_l4lb.CONF_PATH: RENDERED}, tune_output=node_l4lb.CONFIG_TEST_FAILED,
        )

        with self.assertRaises(node_l4lb.NodeL4lbError) as caught:
            self.apply(client)

        self.assertEqual(caught.exception.error_code, 'node_l4lb_config_invalid')
        self.assertEqual(client.files[node_l4lb.CONF_PATH], RENDERED)

    def test_inactive_service_after_reload_is_an_error(self):
        """重载后服务不在跑：配置写对了但没生效，要报出来。"""
        client = FakeLbNode(
            files={node_l4lb.CONF_PATH: RENDERED},
            tune_output=f'{node_l4lb.STATE_PREFIX}inactive',
        )

        with self.assertRaises(node_l4lb.NodeL4lbError) as caught:
            self.apply(client)

        self.assertEqual(caught.exception.error_code, 'node_l4lb_reload_failed')
        self.assertIn('inactive', caught.exception.message)

    def test_write_failure_is_an_error(self):
        """写不进去要抛错，并说清是哪个文件。"""
        client = FakeLbNode(files={node_l4lb.CONF_PATH: RENDERED})
        original_run = client._run

        def refuse(command):
            """把写入命令一律判失败。"""
            if 'base64 -d >' in command:
                return 1, '', 'Read-only file system'
            return original_run(command)

        client._run = refuse

        with self.assertRaises(node_l4lb.NodeL4lbError) as caught:
            self.apply(client)

        self.assertEqual(caught.exception.error_code, 'node_l4lb_write_failed')
        self.assertIn(node_l4lb.CONF_PATH, caught.exception.message)


class TuneLbProxiesTest(SimpleTestCase):
    """``state_machine._tune_lb_proxies``：逐台跑，单台失败只记警告。

    真连节点的那一层在 ``ApplyNodeTest`` 里已经用假客户端跑通了，这里只钉住「怎么调度」：
    挑哪几台、按什么顺序、失败怎么处理。因此 ``apply_node`` 一律打桩，绝不落网络。
    """

    def setUp(self):
        """准备上下文与节点。

        参数：无。
        返回：无显式返回值。
        副作用：只在本机对象上准备数据。
        """
        from k8s_deployment.services import state_machine

        self.state_machine = state_machine
        self.context = RecordingContext()
        self.balancer = make_node(
            '116.198.237.81', node_id=1, cluster_ip='172.30.0.16', roles=['loadbalancer'],
        )

    def tune(self, nodes, credentials=None, side_effect=None):
        """在打桩前提下跑一次。

        参数：`nodes` 表示节点列表；`credentials` 表示连接信息；`side_effect` 表示桩的返回值。
        返回：返回失败节点 IP 列表。
        副作用：无（``apply_node`` 已被打桩，不会连节点）。
        """
        with mock.patch.object(
            node_l4lb, 'apply_node',
            side_effect=side_effect or (lambda *args, **kwargs: {'conf': True, 'reload': True}),
        ) as patched:
            failed = self.state_machine._tune_lb_proxies(
                self.context, nodes, credentials if credentials is not None else {},
            )
        return failed, patched

    def test_no_balancer_means_no_stage(self):
        """没有负载均衡机器时连阶段都不开：这个阶段在阶段条上不该空转一格。"""
        master = make_node('172.30.0.12', node_id=2, roles=['master'])

        failed, patched = self.tune([master])

        self.assertEqual(failed, [])
        self.assertEqual(self.context.stages, [])
        self.assertEqual(patched.call_count, 0)

    def test_only_balancers_are_touched(self):
        """只碰负载均衡机器：往别的机器上找 l4lb.conf 只会白连一趟。"""
        nodes = [
            make_node('172.30.0.12', node_id=2, roles=['master']),
            self.balancer,
        ]

        _failed, patched = self.tune(nodes)

        self.assertEqual(patched.call_count, 1)
        self.assertEqual(patched.call_args.args[1].ip_address, '116.198.237.81')
        self.assertEqual(self.context.stages, ['lb-tuning'])

    def test_failure_is_collected_not_raised(self):
        """连不上时只收集 IP、写警告：转发参数没调成不该把一次成功的部署判失败。"""
        def boom(*_args, **_kwargs):
            """模拟连不上节点。"""
            raise node_l4lb.NodeL4lbError('node_l4lb_reload_failed', '重载之后 l4lb 没起来')

        failed, _patched = self.tune([self.balancer], side_effect=boom)

        self.assertEqual(failed, ['116.198.237.81'])
        self.assertEqual(self.context.stages, ['lb-tuning'])
        self.assertIn('lb_proxy_tuning_failed', self.context.event_types())

    def test_warning_names_the_proxy_timeout_consequence(self):
        """警告要说清代价：走 VIP 的长连接会被掐断，否则没人知道该不该管它。"""
        def boom(*_args, **_kwargs):
            """模拟连不上节点。"""
            raise node_l4lb.NodeL4lbError('node_l4lb_reload_failed', '重载之后 l4lb 没起来')

        self.tune([self.balancer], side_effect=boom)

        message = [item for item in self.context.events if item['type'] == 'lb_proxy_tuning_failed'][0]
        self.assertIn('proxy_timeout', message['message'])

    def test_message_tells_the_operator_how_to_fix_it_by_hand(self):
        """日志里给出可以照抄的手工做法：CLI 环境里没人在读平台文档。"""
        def boom(*_args, **_kwargs):
            """模拟连不上节点。"""
            raise node_l4lb.NodeL4lbError('node_l4lb_reload_failed', '重载之后 l4lb 没起来')

        self.tune([self.balancer], side_effect=boom)

        self.assertIn('systemctl reload l4lb', self.context.text())

    def test_missing_file_is_quietly_logged(self):
        """文件不在时只留一行日志，不算失败。"""
        def absent(*_args, **_kwargs):
            """模拟节点上还没有那份配置。"""
            return {'label': '116.198.237.81', 'conf': 'ABSENT', 'reload': 'ABSENT', 'changes': []}

        failed, _patched = self.tune([self.balancer], side_effect=absent)

        self.assertEqual(failed, [])
        self.assertNotIn('lb_proxy_tuning_failed', self.context.event_types())
        self.assertIn(node_l4lb.CONF_PATH, self.context.text())
