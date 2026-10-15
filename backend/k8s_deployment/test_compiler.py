"""配置编译器的单元测试：变量覆盖与「生成之后的自检」。

规划 9 要求编译产物落盘前经过 YAML 解析、IP/角色覆盖与敏感字段扫描。这些是纯函数，
测试里不建 SSH、不连 OSS，只用真的 kubeasz 模板形状（见 ``kubeasz_fixtures``）。
"""

import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import yaml
from django.test import TestCase

from k8s_deployment.models import K8sDeployment, K8sDeploymentNode
from k8s_deployment.services import compiler, state_machine

from . import kubeasz_fixtures


def make_node(ip_address, roles, ssh_port=22, name='', cluster_ip=''):
    """构造一个未落库的节点快照。

    参数：`ip_address` 表示操作员填写的节点 IP；`roles` 表示角色列表；`ssh_port` 表示 SSH 端口；
        `name` 表示主机名；`cluster_ip` 表示探测到的网卡地址，为空表示与填写的 IP 相同。
    返回：返回未保存的 K8sDeploymentNode 实例。
    副作用：只在本机对象上设置字段，不写数据库。
    """
    return K8sDeploymentNode(
        name=name or ip_address.replace('.', '-'),
        ip_address=ip_address,
        cluster_ip=cluster_ip or None,
        ssh_port=ssh_port,
        ssh_user='root',
        roles=list(roles),
    )


class SensitiveFieldScanTest(TestCase):
    """scan_sensitive_fields 的扫描规则测试。"""

    def test_rejects_known_password_keys(self):
        """ansible_password / ansible_ssh_pass / *_pass 三种样式都要被拦下。"""
        text = (
            'ansible_password: "secret"\n'
            'ansible_ssh_pass=secret\n'
            'ansible_become_pass: secret\n'
            'vault_pass: secret\n'
        )
        found = compiler.scan_sensitive_fields('', text)
        keys = {key for _label, key in found}
        self.assertEqual(
            keys, {'ansible_password', 'ansible_ssh_pass', 'ansible_become_pass', 'vault_pass'},
        )

    def test_reports_which_file(self):
        """问题要带上是哪份文件出的，否则用户不知道该改哪个。"""
        found = compiler.scan_sensitive_fields('ansible_password=x\n', '')
        self.assertEqual(found, [('hosts', 'ansible_password')])

    def test_ignores_commented_and_empty_values(self):
        """被注释掉的示例与空值不算问题，否则 kubeasz 模板自带的注释会误报。"""
        text = (
            '# ansible_password: "secret"\n'
            'ansible_password:\n'
            'ansible_ssh_pass=""\n'
            '# ansible_ssh_pass: \n'
        )
        self.assertEqual(compiler.scan_sensitive_fields('', text), [])

    def test_ignores_unrelated_keys(self):
        """普通变量里带 pass 字样但不是密码键（如 kube_proxy）不应误报。"""
        text = 'KUBELET_ROOT_DIR: "/var/lib/kubelet"\nCOMPASS: "north"\n'
        self.assertEqual(compiler.scan_sensitive_fields('', text), [])


class ClusterAddressRenderingTest(TestCase):
    """NAT 之后的节点：inventory 主机名用网卡地址，``ansible_host`` 用操作员填的地址。

    kubeasz 的 etcd/apiserver 全部按 ``{{ inventory_hostname }}`` 做 bind 与 advertise，
    公网地址不在网卡上就会以 ``bind: cannot assign requested address`` 起不来；而 ansible
    又必须连得到节点。两个地址因此要分开写，缺一样就有一头走不通。
    """

    def line(self, entered='116.198.251.203', cluster_ip='172.30.0.7', section='kube_master', name='master-01'):
        """渲染一行 inventory 主机条目。

        参数：`entered` 表示填写的地址；`cluster_ip` 表示探测到的网卡地址；`section` 表示段名；
            `name` 表示节点名。
        返回：返回渲染后的整行文本。
        副作用：只做纯计算。
        """
        node = make_node(entered, ['master', 'node'], cluster_ip=cluster_ip)
        return compiler._host_line(node, section, name, {})

    def test_nat_node_line_starts_with_the_interface_address(self):
        """主机名是网卡地址，紧随其后给出 ansible_host —— 顺序定了才读得懂这一行。"""
        line = self.line()
        self.assertTrue(line.startswith('172.30.0.7 ansible_host=116.198.251.203 '), line)
        self.assertIn("k8s_nodename='master-01'", line)

    def test_plain_node_line_has_no_ansible_host(self):
        """两个地址相同时不写 ansible_host：加一个恒等于主机名的变量只会让人多问一句。"""
        line = self.line(entered='192.168.1.10', cluster_ip='')
        self.assertTrue(line.startswith('192.168.1.10 '), line)
        self.assertNotIn('ansible_host', line)

    def test_etcd_line_carries_the_connection_address_too(self):
        """etcd 段不写 k8s_nodename，但连接地址一样要有，否则 ansible 去连内网地址。"""
        line = self.line(section='etcd', name='')
        self.assertTrue(line.startswith('172.30.0.7 ansible_host=116.198.251.203 '), line)
        self.assertNotIn('k8s_nodename', line)

    def test_cluster_address_falls_back_to_the_entered_ip(self):
        """没探测到网卡地址时退回填写的地址：那正是「节点没有 NAT」这种最常见情况。"""
        node = make_node('192.168.1.10', ['master'])
        self.assertEqual(compiler.cluster_address(node), '192.168.1.10')

    def test_render_hosts_uses_the_interface_address_in_every_section(self):
        """三个托管段都要按网卡地址渲染，漏掉一段就会有一个组件绑到不存在的地址上。"""
        node = make_node('116.198.251.203', ['master', 'node'], cluster_ip='172.30.0.7')
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        text = compiler.render_hosts(kubeasz_fixtures.HOSTS_MULTI_TEMPLATE, deployment, [node], {})
        sections = compiler.parse_inventory_sections(text)
        for section in ('etcd', 'kube_master', 'kube_node'):
            self.assertEqual(sections[section], ['172.30.0.7'], section)

    def test_validation_accepts_the_interface_address(self):
        """校验要认得网卡地址：NAT 之后的节点在 inventory 里写的就是它。"""
        node = make_node('116.198.251.203', ['master', 'node'], cluster_ip='172.30.0.7')
        hosts = (
            '[etcd]\n172.30.0.7\n\n'
            "[kube_master]\n172.30.0.7 ansible_host=116.198.251.203 k8s_nodename='master-01'\n\n"
            "[kube_node]\n172.30.0.7 ansible_host=116.198.251.203 k8s_nodename='master-01'\n"
        )
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        compiler.validate_rendered(hosts, 'K8S_VER: "1.29.4"\n', deployment, [node])

    def test_validation_rejects_two_names_for_one_machine(self):
        """同一台机器在两段里各写一个名字要判失败：ansible 按主机合并变量，后写的覆盖前面的。

        这正是本次修掉的那个缺陷 —— 一台控制面机器在 ``[kube_master]`` 写 ``master-01``、
        在 ``[kube_node]`` 写 ``worker-01``，kubelet 最终注册成 ``worker-01``，而平台以为它
        叫 ``master-01``。渲染阶段有 ``_assert_node_names_consistent`` 拦着，这里验的是
        ``validate_rendered`` 这道关卡也认得（历史上这类文本可能来自别处，比如运维手改过的
        hosts 文件被重新提交）。
        """
        node = make_node('116.198.251.203', ['master', 'node'], cluster_ip='172.30.0.7')
        hosts = (
            '[etcd]\n172.30.0.7\n\n'
            "[kube_master]\n172.30.0.7 ansible_host=116.198.251.203 k8s_nodename='master-01'\n\n"
            "[kube_node]\n172.30.0.7 ansible_host=116.198.251.203 k8s_nodename='worker-01'\n"
        )
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        with self.assertRaises(compiler.CompileError) as caught:
            compiler.validate_rendered(hosts, 'K8S_VER: "1.29.4"\n', deployment, [node])

        self.assertIn('master-01', str(caught.exception))
        self.assertIn('worker-01', str(caught.exception))

    def test_validation_rejects_two_machines_sharing_a_name(self):
        """两台机器共用一个节点名要判失败：集群里会出现两个同名 Node。"""
        nodes = [
            make_node('192.168.1.10', ['master'], cluster_ip=''),
            make_node('192.168.1.11', ['master'], cluster_ip=''),
        ]
        hosts = (
            '[etcd]\n192.168.1.10\n\n'
            "[kube_master]\n192.168.1.10 k8s_nodename='master-01'\n192.168.1.11 k8s_nodename='master-01'\n\n"
            '[kube_node]\n'
        )
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        with self.assertRaises(compiler.CompileError) as caught:
            compiler.validate_rendered(hosts, 'K8S_VER: "1.29.4"\n', deployment, nodes)

        self.assertIn('master-01', str(caught.exception))

    def test_validation_still_accepts_the_entered_address(self):
        """写填写的地址同样算到齐，不要求两个地址都出现在段里。"""
        node = make_node('116.198.251.203', ['master', 'node'], cluster_ip='172.30.0.7')
        hosts = (
            '[etcd]\n116.198.251.203\n\n'
            '[kube_master]\n116.198.251.203\n\n'
            '[kube_node]\n116.198.251.203\n'
        )
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        compiler.validate_rendered(hosts, 'K8S_VER: "1.29.4"\n', deployment, [node])


class NodeNameMapTest(TestCase):
    """``node_name_map``：节点名的唯一来源，同时也是改目标机 hostname 的依据。

    这个映射要同时满足三件事：同一台机器在两段里是同一个名字（否则 kubelet 注册的名字与平台
    以为的不一致）、纯工作节点的编号与「添加节点」算出来的接得上（否则新装的集群与后续扩容
    会撞名）、手动命名模式下用操作员填的值。
    """

    def deployment(self, naming='auto'):
        """构造一个部署任务快照。"""
        return K8sDeployment(
            display_name='测试集群', slug='t', kubernetes_version='1.29', node_naming=naming,
        )

    def render_names(self, nodes, deployment=None):
        """渲染 inventory 并把 ``{地址: 节点名}`` 读回来。

        参数：`nodes` 表示节点列表；`deployment` 表示部署任务。
        返回：返回映射；某台机器在两段里出现两次时，名字不同会覆盖 —— 用
            ``render_names_per_line`` 才能看出重复。
        副作用：只做纯计算。
        """
        text = compiler.render_hosts(
            kubeasz_fixtures.HOSTS_MULTI_TEMPLATE, deployment or self.deployment(), nodes, {},
        )
        names = {}
        for line in text.splitlines():
            match = re.search(r"^(\S+).*\bk8s_nodename='([^']*)'", line.strip())
            if match:
                names[match.group(1)] = match.group(2)
        return names

    def test_control_plane_keeps_one_name_in_both_sections(self):
        """控制面机器同时在 ``[kube_master]`` 与 ``[kube_node]`` 里，两处必须是同一个名字。

        这是本次修掉的缺陷：以前两段各写一个（master-01 / worker-01），ansible 按主机合并
        变量、后写的生效，于是控制面在集群里其实叫 worker-01。
        """
        node = make_node('192.168.1.10', ['master', 'node'])

        names = compiler.node_name_map(self.deployment(), [node])

        self.assertEqual(names, {'192.168.1.10': 'master-01'})

    def test_rendered_inventory_matches_the_map(self):
        """渲染出来的 ``k8s_nodename`` 与映射逐台一致 —— 两边同源，不可能算出不同的名字。"""
        nodes = [
            make_node('192.168.1.10', ['etcd', 'master', 'node']),
            make_node('192.168.1.11', ['etcd', 'master', 'node']),
            make_node('192.168.1.12', ['node']),
            make_node('192.168.1.13', ['node']),
        ]
        deployment = self.deployment()

        names = compiler.node_name_map(deployment, nodes)
        rendered = self.render_names(nodes, deployment)

        self.assertEqual(names, rendered)
        self.assertEqual(rendered, {
            '192.168.1.10': 'master-01',
            '192.168.1.11': 'master-02',
            '192.168.1.12': 'worker-03',
            '192.168.1.13': 'worker-04',
        })

    def test_worker_numbering_matches_the_add_node_algorithm(self):
        """纯工作节点的编号按 ``[kube_node]`` 段的段内序号来，与 ``next_node_name`` 一致。

        口径不一致的后果很具体：新装集群里两台工作节点叫 worker-03/worker-04，之后「添加
        节点」也会从 3 开始数（它数的是段内条目数），加进来的机器就叫 worker-03 —— 集群里
        出现两台同名 Node。
        """
        nodes = [make_node('192.168.1.1%d' % index, ['master', 'node']) for index in range(1, 3)]
        hosts_text = compiler.render_hosts(
            kubeasz_fixtures.HOSTS_MULTI_TEMPLATE, self.deployment(), nodes, {},
        )

        self.assertEqual(state_machine.next_node_name(hosts_text, 'kube_node'), 'worker-03')
        self.assertEqual(
            compiler.node_name_map(self.deployment(), nodes + [make_node('192.168.1.99', ['node'])])['192.168.1.99'],
            'worker-03',
        )

    def test_disabled_machines_get_no_name(self):
        """只有 etcd 角色的机器在集群里没有节点名，不进映射（也就不会被改 hostname）。"""
        nodes = [
            make_node('192.168.1.10', ['etcd']),
            make_node('192.168.1.11', ['node']),
        ]

        names = compiler.node_name_map(self.deployment(), nodes)

        # 只有 etcd 角色的机器不在 ``[kube_node]`` 段里，因此也不占编号 —— 唯一的工作节点是
        # 段内第 1 条，叫 worker-01；这与 ``next_node_name`` 数段内条目的结果一致。
        self.assertEqual(names, {'192.168.1.11': 'worker-01'})

    def test_manual_mode_uses_the_filled_name(self):
        """手动命名模式下用操作员填的值，且不在两段之间再算一遍。"""
        node = make_node('192.168.1.10', ['master', 'node'])
        node.node_name = 'prod-cp-01'

        names = compiler.node_name_map(self.deployment('manual'), [node])

        self.assertEqual(names, {'192.168.1.10': 'prod-cp-01'})

    def test_manual_mode_normalizes_case(self):
        """大写字母统一转小写：kubelet 只接受小写节点名，早点归一比部署中途报错好。"""
        node = make_node('192.168.1.10', ['node'])
        node.node_name = 'Worker-A'

        self.assertEqual(compiler.node_name_map(self.deployment('manual'), [node]), {'192.168.1.10': 'worker-a'})

    def test_manual_mode_rejects_an_illegal_name(self):
        """手动填的名字不合法时在渲染阶段就报错，带上是哪台机器。"""
        node = make_node('192.168.1.10', ['node'])
        node.node_name = 'Worker_X'

        with self.assertRaises(compiler.CompileError) as caught:
            compiler.node_name_map(self.deployment('manual'), [node])

        self.assertIn('192.168.1.10', str(caught.exception))


class ParseInventorySectionsTest(TestCase):
    """parse_inventory_sections 的解析测试。"""

    def test_sections_and_hosts(self):
        """段名与每行的第一个字段（主机地址）要正确取出。"""
        text = (
            '[etcd]\n'
            '192.168.1.10\n'
            '\n'
            '[kube_master]\n'
            "192.168.1.10 k8s_nodename='master-1'\n"
            '# 注释行忽略\n'
            '[all:vars]\n'
            'SECURE_PORT="6443"\n'
        )
        sections = compiler.parse_inventory_sections(text)
        self.assertEqual(sections['etcd'], ['192.168.1.10'])
        self.assertEqual(sections['kube_master'], ['192.168.1.10'])
        self.assertEqual(sections['all:vars'], ['SECURE_PORT="6443"'])

    def test_empty_text(self):
        """空文本返回空映射，不抛异常。"""
        self.assertEqual(compiler.parse_inventory_sections(''), {})
        self.assertEqual(compiler.parse_inventory_sections(None), {})


class ValidateRenderedTest(TestCase):
    """validate_rendered 三种校验的测试。"""

    HOSTS = (
        '[etcd]\n'
        '192.168.1.10\n'
        '192.168.1.11\n'
        '192.168.1.12\n'
        '\n'
        '[kube_master]\n'
        "192.168.1.10 k8s_nodename='master-1'\n"
        "192.168.1.11 k8s_nodename='master-2'\n"
        "192.168.1.12 k8s_nodename='master-3'\n"
        '\n'
        '[kube_node]\n'
        "192.168.1.10 k8s_nodename='master-1'\n"
    )

    CONFIG = 'K8S_VER: "1.29.4"\nSECURE_PORT: "6443"\n'

    def nodes(self):
        """返回与上面 HOSTS 对应的节点快照列表。

        参数：无。
        返回：返回三个节点的列表。
        副作用：只构造本机对象，不写数据库。
        """
        return [
            make_node('192.168.1.10', ['etcd', 'master', 'node']),
            make_node('192.168.1.11', ['etcd', 'master']),
            make_node('192.168.1.12', ['etcd', 'master']),
        ]

    def validate(self, hosts=None, config=None, nodes=None):
        """调用 validate_rendered 并返回是否抛错。

        参数：`hosts`/`config` 表示待校验文本；`nodes` 表示节点列表。
        返回：返回 ``(是否通过, 错误信息)``。
        副作用：只做纯计算。
        """
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        try:
            compiler.validate_rendered(
                hosts if hosts is not None else self.HOSTS,
                config if config is not None else self.CONFIG,
                deployment,
                nodes if nodes is not None else self.nodes(),
            )
        except compiler.CompileError as exc:
            return False, str(exc)
        return True, ''

    def test_valid_configuration_passes(self):
        """角色与段完全对应时通过。"""
        passed, message = self.validate()
        self.assertTrue(passed, message)

    def test_invalid_yaml_rejected(self):
        """config.yml 不是合法 YAML 时判失败：语法错要到部署中段才爆。"""
        passed, message = self.validate(config='K8S_VER: "1.29.4"\n  bad indent: [\n')
        self.assertFalse(passed)
        self.assertIn('YAML', message)

    def test_non_mapping_yaml_rejected(self):
        """config.yml 解析出列表时判失败：kubeasz 需要的是键值结构。"""
        passed, message = self.validate(config='- a\n- b\n')
        self.assertFalse(passed)
        self.assertIn('键值结构', message)

    def test_node_missing_from_role_section_rejected(self):
        """角色里有 node 但没出现在 [kube_node] 段的节点要判失败。"""
        # 只砍掉 [kube_node] 段的最后一行（前面三行还在 [kube_master] 段里，位置相同）。
        lines = self.HOSTS.splitlines(keepends=True)
        hosts = ''.join(line for index, line in enumerate(lines) if index != len(lines) - 1)
        passed, message = self.validate(hosts=hosts)
        self.assertFalse(passed)
        self.assertIn('kube_node', message)
        self.assertIn('192.168.1.10', message)

    def test_ip_in_wrong_section_rejected(self):
        """没有 master 角色的节点出现在 [kube_master] 段里要判失败。"""
        hosts = self.HOSTS.replace(
            "192.168.1.12 k8s_nodename='master-3'\n",
            "192.168.1.12 k8s_nodename='master-3'\n192.168.1.99 k8s_nodename='extra'\n",
        )
        nodes = self.nodes() + [make_node('192.168.1.99', ['node'])]
        passed, message = self.validate(hosts=hosts, nodes=nodes)
        self.assertFalse(passed)
        self.assertIn('192.168.1.99', message)

    def test_unknown_address_rejected(self):
        """inventory 里出现任务节点之外的地址要判失败。"""
        hosts = self.HOSTS + '192.168.1.200\n'
        passed, message = self.validate(hosts=hosts)
        self.assertFalse(passed)
        self.assertIn('192.168.1.200', message)

    def test_etcd_section_filled_by_masters_passes(self):
        """没有 etcd 角色节点时由 master 兜底填充 [etcd] 段，不算错配。"""
        hosts = (
            '[etcd]\n192.168.1.10\n\n'
            '[kube_master]\n192.168.1.10\n\n'
            '[kube_node]\n192.168.1.10\n'
        )
        passed, message = self.validate(hosts=hosts, nodes=[make_node('192.168.1.10', ['master', 'node'])])
        self.assertTrue(passed, message)

    def test_sensitive_field_in_config_rejected(self):
        """生成物里出现明文凭据字段要判失败，绝不能落盘。"""
        passed, message = self.validate(config=self.CONFIG + 'ansible_password: "secret"\n')
        self.assertFalse(passed)
        self.assertIn('明文凭据', message)
        self.assertIn('ansible_password', message)

    def test_missing_platform_interpreter_rejected(self):
        """平台机上没有 inventory 里钉的解释器时要判失败，并给出可照做的修法。"""
        hosts = self.HOSTS + '\n[all:vars]\nansible_python_interpreter=/no/such/python3\n'
        with mock.patch.object(compiler, 'ON_KUBEASZ_HOST', True):
            passed, message = self.validate(hosts=hosts)

        self.assertFalse(passed)
        self.assertIn('/no/such/python3', message)
        self.assertIn('ln -s', message)


class PythonInterpreterCheckTest(TestCase):
    """``missing_python_interpreter``：一个变量，两台机器。

    kubeasz 的证书与 kubeconfig 生成任务都带 ``connection: local``——在**平台机**上执行，
    却沿用目标节点那一组变量。于是 ``ansible_python_interpreter`` 会同时被两端使用：值是
    节点认得的 ``/usr/bin/python3``，平台机上就必须真有这个路径，否则 ``02.etcd`` 在 5 秒内
    以一句 ``/bin/sh`` 的报错中断，跟解释器这件事看不出任何关系。
    """

    HOSTS = '[all:vars]\nansible_python_interpreter=/usr/bin/python3\n'

    def check(self, hosts=None, exists=None):
        """在「这就是平台机」的前提下调用被测函数。

        参数：`hosts` 表示 inventory 文本；`exists` 表示路径存在性判断函数。
        返回：返回被测函数的返回值。
        副作用：只做纯计算，不访问文件系统（``exists`` 由调用方给定）。
        """
        with mock.patch.object(compiler, 'ON_KUBEASZ_HOST', True):
            return compiler.missing_python_interpreter(
                self.HOSTS if hosts is None else hosts, exists=exists,
            )

    def test_missing_path_is_reported(self):
        """路径不存在时把它报出来，交给上层的报错文案。"""
        self.assertEqual(self.check(exists=lambda _path: False), '/usr/bin/python3')

    def test_existing_path_passes(self):
        """路径存在就没问题：这是修好之后的状态。"""
        self.assertEqual(self.check(exists=lambda _path: True), '')

    def test_check_skipped_where_kubeasz_never_runs(self):
        """开发机（Windows）不跑 kubeasz，不能因为缺少 Linux 路径而拦住本地预览。"""
        with mock.patch.object(compiler, 'ON_KUBEASZ_HOST', False):
            self.assertEqual(
                compiler.missing_python_interpreter(
                    self.HOSTS, exists=lambda _path: False,
                ),
                '',
            )

    def test_absent_variable_is_not_checked(self):
        """模板里没这一行时解释器由 ansible 自己探测，与本条无关。"""
        hosts = '[all:vars]\nSECURE_PORT="6443"\n'
        self.assertEqual(self.check(hosts=hosts, exists=lambda _path: False), '')

    def test_relative_value_is_not_a_platform_path(self):
        """不是绝对路径就不做存在性判断——那种值不指向任何固定位置。"""
        hosts = '[all:vars]\nansible_python_interpreter=python3\n'
        self.assertEqual(self.check(hosts=hosts, exists=lambda _path: False), '')


class RenderConfigOverridesTest(TestCase):
    """render_config 对第 8 节变量表的覆盖测试。"""

    def setUp(self):
        """写出一棵合成 kubeasz 树并读出其中声明的版本变量。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里创建文件（用例结束后删除）。
        """
        self.root = Path(tempfile.mkdtemp(prefix='k8s-compiler-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.variables = compiler.read_kubeasz_variables(self.root)
        self.template = (self.root / compiler.CONFIG_TEMPLATE).read_text(encoding='utf-8')

    def render(self, **fields):
        """按给定任务字段渲染一次 config.yml。

        参数：`fields` 表示要覆盖的 K8sDeployment 字段。
        返回：返回渲染后的 config.yml 文本。
        副作用：只做纯计算。

        ``cluster_slug`` 是只读属性，新建类任务取的就是 ``slug``，所以这里只设 slug。
        """
        deployment = K8sDeployment(
            display_name='测试集群',
            slug='demo',
            kubernetes_version='1.29',
            **fields,
        )
        return compiler.render_config(self.template, deployment, self.variables)

    def test_ca_and_component_validity_are_written_separately(self):
        """根证书与组件证书各写各的（单位：年；1 年 = 365 天 = 8760h）。

        kubeasz 的 ``CA_EXPIRY`` 只作用于根，``CERT_EXPIRY`` 作用于它签发的一切
        （apiserver/etcd/kubelet/kube-proxy/calico/harbor 的服务端与客户端证书），
        所以两个值可以不同，也必须能不同。
        """
        text = self.render(cert_validity_enabled=True, ca_validity_years=10, cert_validity_years=3)
        self.assertIn('CA_EXPIRY: "87600h"', text)
        self.assertIn('CERT_EXPIRY: "26280h"', text)

    def test_hundred_years_matches_the_kubeasz_ca_default(self):
        """100 年必须换算成 876000h —— 那正是 kubeasz 自己的 CA 默认值。

        这条同时锁住换算口径：用 365.25 天算年会对不上 kubeasz 的默认值。
        """
        text = self.render(cert_validity_enabled=True, ca_validity_years=100, cert_validity_years=100)
        self.assertIn('CA_EXPIRY: "876000h"', text)
        self.assertIn('CERT_EXPIRY: "876000h"', text)

    def test_cert_validity_off_keeps_template_defaults(self):
        """关闭自定义时继承 kubeasz 默认，不改写模板里的值。"""
        text = self.render(cert_validity_enabled=False, ca_validity_years=1, cert_validity_years=1)
        self.assertNotIn('CA_EXPIRY: "8760h"', text)
        self.assertNotIn('CERT_EXPIRY: "8760h"', text)

    def test_data_dir_sets_all_container_storage_variables(self):
        """dataDir 要同时改写 etcd 与容器运行时的数据目录（规划 8）。"""
        text = self.render(data_dir_enabled=True, data_dir='/data/k8s/')
        for variable, expected in (
            ('ETCD_DATA_DIR', '/data/k8s/etcd'),
            ('CONTAINERD_STORAGE_DIR', '/data/k8s/containerd'),
            ('CONTAINERD_ROOT_DIR', '/data/k8s/containerd'),
            ('KUBELET_ROOT_DIR', '/data/k8s/kubelet'),
        ):
            self.assertIn(f'{variable}: "{expected}"', text, f'{variable} 没有被改写')

    def test_data_dir_disabled_keeps_template_paths(self):
        """未启用 dataDir 时不动这些变量。"""
        text = self.render(data_dir_enabled=False, data_dir='/data/k8s')
        self.assertNotIn('/data/k8s', text)


class BinDirTest(TestCase):
    """``bin_dir``：节点上 kubeasz 命令的落点，往系统 PATH 里加目录时要跟着它走。"""

    def test_reads_quoted_and_unquoted_values(self):
        """两种写法都要认：上游模板带引号，操作员手改时常常把引号去掉。"""
        self.assertEqual(compiler.bin_dir('bin_dir="/opt/kube/bin"'), '/opt/kube/bin')
        self.assertEqual(compiler.bin_dir('bin_dir=/opt/kube/bin'), '/opt/kube/bin')

    def test_falls_back_to_the_upstream_default(self):
        """没写这个变量时用上游两份模板里的值。"""
        self.assertEqual(compiler.bin_dir('[all:vars]\nkube_ver="1.29.4"\n'), compiler.DEFAULT_BIN_DIR)

    def test_ignores_nested_occurrences(self):
        """出现在别的变量值里不算声明（``inventory_variable`` 只认顶格赋值）。"""
        self.assertEqual(
            compiler.bin_dir('cluster_dir="{{ base_dir }}/clusters/bin_dir"\n'),
            compiler.DEFAULT_BIN_DIR,
        )


class CertificateHostsTest(TestCase):
    """``certificate_hosts`` 的取值与去重规则。"""

    def test_entered_address_comes_before_the_interface_address(self):
        """先写操作员填的地址：它是平台与 kubeconfig 会用的那个，排前面便于阅读。"""
        node = make_node('114.67.89.90', ['master'], cluster_ip='172.30.0.16')
        self.assertEqual(compiler.certificate_hosts([node]), ['114.67.89.90', '172.30.0.16'])

    def test_identical_addresses_are_listed_once(self):
        """填的就是网卡地址（纯内网部署）时只留一条，不写两遍同一个 IP。"""
        node = make_node('192.168.1.10', ['master'])
        self.assertEqual(compiler.certificate_hosts([node]), ['192.168.1.10'])

    def test_every_node_contributes_both_addresses(self):
        """多节点：每台的公网与内网都在里面，缺一台就会有节点连不上 apiserver。"""
        nodes = [
            make_node('114.67.89.90', ['master'], cluster_ip='172.30.0.16'),
            make_node('114.67.89.91', ['master'], cluster_ip='172.30.0.17'),
            make_node('192.168.1.20', ['node']),
        ]
        self.assertEqual(compiler.certificate_hosts(nodes), [
            '114.67.89.90', '172.30.0.16',
            '114.67.89.91', '172.30.0.17',
            '192.168.1.20',
        ])

    def test_repeated_address_is_not_duplicated(self):
        """地址重复出现时保留首次出现的位置，不让同一串 IP 出现在多个位置。"""
        nodes = [
            make_node('10.0.0.1', ['master'], cluster_ip='172.30.0.16'),
            make_node('10.0.0.2', ['node'], cluster_ip='172.30.0.16'),
        ]
        self.assertEqual(compiler.certificate_hosts(nodes), ['10.0.0.1', '172.30.0.16', '10.0.0.2'])

    def test_empty_addresses_are_skipped(self):
        """两个地址都取不到时留空，不能往证书里写一条空字符串。"""
        self.assertEqual(compiler.certificate_hosts([make_node('', ['master'])]), [])


class MasterCertHostsRenderingTest(TestCase):
    """渲染后的 config.yml 里 ``MASTER_CERT_HOSTS`` 必须是节点地址，且只剩这些。

    真实背景：节点在 NAT 后面、按公网 IP 部署时，kubeasz 只把 inventory 里的主机名
    （网卡地址）放进 apiserver 证书的 SAN，公网地址不在里面 —— 平台拿公网 IP 连
    apiserver 直接被 ``certificate is not valid for '114.67.89.90'`` 拒掉，
    界面上的集群页于是永远是空的，而部署任务 7/7 全绿。
    """

    def setUp(self):
        """写出一棵合成 kubeasz 树并读出其中声明的版本变量。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里创建文件（用例结束后删除）。
        """
        self.root = Path(tempfile.mkdtemp(prefix='k8s-compiler-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.variables = compiler.read_kubeasz_variables(self.root)
        self.template = (self.root / compiler.CONFIG_TEMPLATE).read_text(encoding='utf-8')

    def render(self, nodes, template=None):
        """按给定节点渲染一次 config.yml。

        参数：`nodes` 表示节点快照列表或 None；`template` 表示模板文本，默认用夹具模板。
        返回：返回渲染后的 config.yml 文本。
        副作用：只做纯计算。
        """
        deployment = K8sDeployment(display_name='测试集群', slug='demo', kubernetes_version='1.29')
        return compiler.render_config(
            template if template is not None else self.template,
            deployment,
            self.variables,
            nodes=nodes,
        )

    def cert_hosts(self, text):
        """从渲染结果里读出 ``MASTER_CERT_HOSTS`` 的条目。

        参数：`text` 表示渲染后的 config.yml 文本。
        返回：返回地址列表。
        副作用：只做纯计算。
        """
        lines = text.splitlines()
        start = lines.index(f'{compiler.MASTER_CERT_HOSTS_KEY}:')
        items = []
        for line in lines[start + 1:]:
            if not line.startswith('  - '):
                break
            items.append(line.strip()[len('- '):].strip('"'))
        return items

    def test_public_and_interface_addresses_are_both_rendered(self):
        """按公网 IP 部署的单节点：公网与网卡地址都进证书。"""
        node = make_node('114.67.89.90', ['master', 'node'], cluster_ip='172.30.0.16')
        self.assertEqual(self.cert_hosts(self.render([node])), ['114.67.89.90', '172.30.0.16'])

    def test_template_example_addresses_are_removed(self):
        """模板自带的 10.1.1.1 / k8s.easzlab.io / 被注释的示例都必须消失。

        这块是列表而不是标量，只改键行的写法会把示例地址原地留下 —— 那正是「证书里始终
        缺公网 IP、却看着像配过了」的成因，所以要连块内条目一起替换掉。
        """
        node = make_node('192.168.1.10', ['master'])
        text = self.render([node])
        self.assertNotIn('10.1.1.1', text)
        self.assertNotIn('k8s.easzlab.io', text)
        self.assertNotIn('www.test.com', text)

    def test_block_keeps_its_neighbours(self):
        """替换只吃掉这一块：紧接着的其它变量不能被误删，否则后面会以「键不存在」静默失效。"""
        node = make_node('192.168.1.10', ['master'])
        text = self.render([node])
        self.assertIn('CA_EXPIRY: "876000h"', text)
        self.assertIn('CERT_EXPIRY: "438000h"', text)
        self.assertIn('ETCD_DATA_DIR: "/var/lib/etcd"', text)

    def test_rendered_config_is_still_valid_yaml_with_a_list(self):
        """最终产物是 ansible 要读的 YAML：这块是列表，不能被写坏。"""
        node = make_node('114.67.89.90', ['master'], cluster_ip='172.30.0.16')
        parsed = yaml.safe_load(self.render([node]))
        self.assertEqual(parsed['MASTER_CERT_HOSTS'], ['114.67.89.90', '172.30.0.16'])

    def test_block_is_appended_when_template_has_none(self):
        """模板里没有这个键时补一块，而不是悄悄不写（否则老版本 kubeasz 上修复失效）。"""
        template = self.template.replace(
            'MASTER_CERT_HOSTS:\n  - "10.1.1.1"\n  - "k8s.easzlab.io"\n  #- "www.test.com"\n',
            '',
        )
        self.assertNotIn(compiler.MASTER_CERT_HOSTS_KEY, template)
        node = make_node('114.67.89.90', ['master'], cluster_ip='172.30.0.16')
        parsed = yaml.safe_load(self.render([node], template=template))
        self.assertEqual(parsed['MASTER_CERT_HOSTS'], ['114.67.89.90', '172.30.0.16'])

    def test_blank_lines_inside_the_block_are_swallowed(self):
        """条目之间夹空行的模板也要整块替换：漏掉半块的后果是示例地址留在证书里。

        真实模板（kubeasz 3.6.10 的 example/config.yml）里没有空行，但这是上游随时可能改的
        格式细节，而判错的方向很不划算 —— 残留一条 ``10.1.1.1`` 不会让任何一步报错。
        """
        template = self.template.replace(
            'MASTER_CERT_HOSTS:\n  - "10.1.1.1"\n  - "k8s.easzlab.io"\n  #- "www.test.com"\n',
            'MASTER_CERT_HOSTS:\n\n  - "10.1.1.1"\n\n  - "k8s.easzlab.io"\n\n  #- "www.test.com"\n\n',
        )
        node = make_node('114.67.89.90', ['master'], cluster_ip='172.30.0.16')
        text = self.render([node], template=template)
        self.assertNotIn('10.1.1.1', text)
        self.assertNotIn('k8s.easzlab.io', text)
        self.assertIn('CA_EXPIRY: "876000h"', text)

    def test_separator_blank_line_before_the_next_block_is_kept(self):
        """块后那个空行属于下一段：替换不能顺手把它删掉，模板的版式要保持原样。"""
        template = self.template.replace(
            '  #- "www.test.com"\n',
            '  #- "www.test.com"\n\n',
        )
        node = make_node('192.168.1.10', ['master'])
        text = self.render([node], template=template)
        self.assertIn(f'{compiler.MASTER_CERT_HOSTS_KEY}:\n  - "192.168.1.10"\n\nCA_EXPIRY:', text)

    def test_nodes_omitted_leaves_the_block_untouched(self):
        """不传 nodes 的调用方（例如只关心变量覆盖的调用）保持原样，不被误改。"""
        text = self.render(None)
        self.assertEqual(self.cert_hosts(text), ['10.1.1.1', 'k8s.easzlab.io'])

    def test_compile_cluster_writes_the_addresses_to_disk(self):
        """端到端：真正落盘的 config.yml 里带着两个地址（状态机走的就是这条路）。"""
        node = make_node('114.67.89.90', ['master', 'node'], cluster_ip='172.30.0.16')
        deployment = K8sDeployment(
            display_name='测试集群',
            slug='cert-demo',
            kubernetes_version='1.29',
            pod_cidr='172.20.0.0/16',
            service_cidr='10.68.0.0/16',
        )
        result = compiler.compile_cluster(deployment, [node], {}, kubeasz_root=self.root)
        text = Path(result['config_path']).read_text(encoding='utf-8')
        self.assertEqual(self.cert_hosts(text), ['114.67.89.90', '172.30.0.16'])


class RenderConfigPlaceholderScopeTest(TestCase):
    """渲染只应索要模板里真正出现的占位符。

    真实背景：``PLACEHOLDER_VARIABLES`` 是全部 kubeasz 版本共用的一张表，其中
    headlamp/higress/node-problem-detector 是 3.7 才引入的、openebs 属更早一批，而
    3.6.7/3.6.8/3.6.10 的 ``example/config.yml`` 里根本没有这些占位符，它们的 ezdown
    自然也不声明对应变量。若按「映射表里有就必须在 ezdown 里找到」来判定，1.33/1.34/1.35
    会全部编译失败，且报错说「制品可能不完整」，把人引向反复重新同步制品这条死路。
    """

    # 刻意不引用 fixtures 的模板：它把全部占位符以注释形式写进文件，'__x__' 这类子串判断
    # 在注释里也会命中，测不出「模板没用到」这个条件。这里显式写出用到与用不到两种模板。
    TEMPLATE = (
        'K8S_VER: "__k8s_ver__"\n'
        'CLUSTER_NAME: "_cluster_name_"\n'
        'INSTALL_SOURCE: "online"\n'
    )

    def setUp(self):
        """写出一棵合成 kubeasz 树并读出其中声明的版本变量。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里创建文件（用例结束后删除）。
        """
        self.root = Path(tempfile.mkdtemp(prefix='k8s-compiler-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        # 夹具的 ezdown 按映射表逐条声明变量，等价于 3.7.0：先制造「变量齐全」的基线。
        self.variables = compiler.read_kubeasz_variables(self.root)
        # 摘掉 headlampVer，等价于 3.6.7/3.6.8/3.6.10 的 ezdown——它们没有这个变量。
        self.legacy_variables = {
            key: value for key, value in self.variables.items() if key != 'headlampVer'
        }

    def render(self, template, variables):
        """按给定模板与变量渲染一次 config.yml。

        参数：`template` 表示 config.yml 模板文本；`variables` 表示版本变量字典。
        返回：返回渲染后的 config.yml 文本。
        副作用：只做纯计算。
        """
        deployment = K8sDeployment(display_name='测试集群', slug='demo', kubernetes_version='1.29')
        return compiler.render_config(template, deployment, variables)

    def test_variable_absent_from_template_is_not_required(self):
        """模板没用到的占位符，其变量在 ezdown 里缺失不该让渲染失败。"""
        self.assertNotIn('__headlamp__', self.TEMPLATE)
        rendered = self.render(self.TEMPLATE, self.legacy_variables)
        # 少一个「模板用不到」的变量，产物必须与变量齐全时逐字一致。
        self.assertEqual(rendered, self.render(self.TEMPLATE, self.variables))

    def test_used_placeholder_without_variable_still_fails(self):
        """模板里真的用到的占位符缺了变量，仍然必须报错。"""
        template = self.TEMPLATE + 'HEADLAMP_TAG: "__headlamp__"\n'
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(template, self.legacy_variables)
        self.assertIn('__headlamp__', str(caught.exception))

    def test_operator_placeholder_is_left_to_the_user(self):
        """kubeasz 留给人工填写的占位符原样保留，不该被判成「映射表过期」。

        3.6.8 的 example/config.yml 里 nacos_mysql_user 就是 ``__dbuser__``，而它自己的
        ezctl 并不替换这个值；平台若不放过它，1.34 会被残留检查卡死。
        """
        template = self.TEMPLATE + 'nacos_mysql_user: "__dbuser__"\n'
        rendered = self.render(template, self.variables)
        self.assertIn('nacos_mysql_user: "__dbuser__"', rendered)

    def test_unknown_placeholder_still_fails(self):
        """映射表没登记、也不属于人工填写类的新占位符仍要报错，保住这道网。"""
        template = self.TEMPLATE + 'SOMETHING: "__brand_new_thing__"\n'
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(template, self.variables)
        self.assertIn('__brand_new_thing__', str(caught.exception))


class ExternalLbInventoryTest(TestCase):
    """``[ex_lb]`` 段的渲染：一主一备的 host vars、不启用时该段为空。

    上游模板里这一段是两行被注释掉的示例（``example/hosts.multi-node``），平台必须把它们
    整体换掉 —— 留着示例会让确认页上出现一个与本集群无关的 VIP 与 8443 端口。
    """

    TEMPLATE = kubeasz_fixtures.HOSTS_MULTI_TEMPLATE

    def nodes(self, lb_addresses=(), lb_ids=None):
        """构造三台 master 加若干台负载均衡机器的节点清单。

        参数：`lb_addresses` 表示负载均衡机器的地址；`lb_ids` 表示要给它们设置的主键（模拟插入序）。
        返回：返回节点快照列表。
        副作用：只构造本机对象，不写数据库。
        """
        items = [
            make_node('192.168.1.10', ['etcd', 'master']),
            make_node('192.168.1.11', ['etcd', 'master']),
            make_node('192.168.1.12', ['etcd', 'master']),
        ]
        for index, address in enumerate(lb_addresses):
            node = make_node(address, ['loadbalancer'])
            if lb_ids:
                # 未落库的节点没有主键，而 LB_ROLE 的分配按主键升序；显式给一个主键来钉住顺序。
                node.id = lb_ids[index]
            items.append(node)
        return items

    def deployment(self, **fields):
        """构造一个部署任务实例（不落库）。

        参数：`fields` 表示要覆盖的字段。
        返回：返回 K8sDeployment 实例。
        副作用：只构造本机对象，不写数据库。
        """
        values = {
            'display_name': '测试集群',
            'slug': 'demo',
            'kubernetes_version': '1.29',
            'api_server_port': 6443,
        }
        values.update(fields)
        return K8sDeployment(**values)

    def render(self, deployment=None, nodes=None):
        """渲染一次 inventory。

        参数：`deployment` 表示部署任务；`nodes` 表示节点清单。
        返回：返回渲染后的 inventory 文本。
        副作用：只做纯计算。
        """
        return compiler.render_hosts(
            self.TEMPLATE,
            deployment if deployment is not None else self.deployment(),
            nodes if nodes is not None else self.nodes(),
            {},
        )

    def lb_lines(self, text):
        """取出渲染结果里 ``[ex_lb]`` 段的主机行。

        参数：`text` 表示 inventory 文本。
        返回：返回该段的主机行列表（不含段头与注释）。
        副作用：只做纯计算。
        """
        lines = text.splitlines()
        start = lines.index(f'[{compiler.LB_SECTION}]')
        items = []
        for line in lines[start + 1:]:
            if line.startswith('['):
                break
            if line.strip() and not line.strip().startswith('#'):
                items.append(line.strip())
        return items

    def test_disabled_leaves_the_section_empty(self):
        """未启用时 [ex_lb] 段头保留但没有任何主机行，模板里的示例也不残留。

        段头必须留着：``clean_lb.yml`` 的 ``when`` 条件里写了 ``groups['ex_lb']``，
        段头不在就是一个未定义变量，清理剧本直接报错。
        """
        text = self.render()
        self.assertIn(f'[{compiler.LB_SECTION}]', text)
        self.assertEqual(self.lb_lines(text), [])
        self.assertNotIn('192.168.1.250', text)
        self.assertNotIn(compiler.LB_VIP_KEY, text)
        self.assertNotIn(compiler.LB_ROLE_KEY, text)

    def test_enabled_renders_master_and_backup(self):
        """启用时两台机器一主一备，三个 host var 逐字落在主机行上。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        text = self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
        lines = self.lb_lines(text)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('192.168.1.6 '), lines[0])
        self.assertTrue(lines[1].startswith('192.168.1.7 '), lines[1])
        self.assertIn(
            'LB_ROLE=master EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=6443', lines[0],
        )
        self.assertIn(
            'LB_ROLE=backup EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=6443', lines[1],
        )

    def test_host_vars_are_on_the_host_line_not_in_all_vars(self):
        """三个变量必须写在主机行上：写进 [all:vars] 会让两台都变成同一个角色。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        text = self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
        all_vars = text.partition('[all:vars]')[2]
        self.assertNotIn(compiler.LB_ROLE_KEY, all_vars)
        self.assertNotIn(compiler.LB_VIP_KEY, all_vars)

    def test_node_name_is_not_written_on_lb_lines(self):
        """LB 机器不是集群节点，主机行上不能出现 k8s_nodename。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250', node_naming='auto')
        text = self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
        for line in self.lb_lines(text):
            self.assertNotIn('k8s_nodename', line)

    def test_master_role_follows_the_snapshot_order(self):
        """谁当 MASTER 只取决于插入序：同一份输入两次编译必须得到同一份 hosts。

        随机会让 ``config_hash`` 抖动、配置版本无谓增长，keepalived 的 MASTER 也会在两台机器
        之间跳来跳去，而用户明确要的是「不要随机」。
        """
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        # 故意把 id 大的排前面：按 id 升序应当是 .7 当 master，而不是入参顺序里的 .6。
        nodes = self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[20, 10])
        lines = self.lb_lines(self.render(deployment, nodes))
        by_role = {line.split()[0]: line for line in lines}
        self.assertIn('LB_ROLE=master', by_role['192.168.1.7'])
        self.assertIn('LB_ROLE=backup', by_role['192.168.1.6'])

    def test_lb_machines_do_not_leak_into_cluster_sections(self):
        """负载均衡机器只能出现在 [ex_lb] 段里。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        text = self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
        sections = compiler.parse_inventory_sections(text)
        for section in ('etcd', 'kube_master', 'kube_node'):
            for address in ('192.168.1.6', '192.168.1.7'):
                self.assertNotIn(address, sections.get(section, []), f'{section} 段里不该有负载均衡机器')
        self.assertEqual(sections['ex_lb'], ['192.168.1.6', '192.168.1.7'])

    def test_port_follows_the_api_server_port(self):
        """对外端口复用 ApiServer 端口，不另设一个旋钮。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250', api_server_port=7443)
        lines = self.lb_lines(
            self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2])),
        )
        self.assertIn('EX_APISERVER_PORT=7443', lines[0])

    def test_single_lb_machine_is_rejected(self):
        """只有一台时 keepalived 没有 unicast 对端，VIP 起不来 —— 编译期就该拦住。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(deployment, self.nodes(['192.168.1.6'], lb_ids=[1]))
        self.assertIn('至少需要 2 台', str(caught.exception))

    def test_missing_vip_is_rejected(self):
        """启用但没填 VIP：VIP 必须由操作员手工输入，平台不生成。"""
        deployment = self.deployment(lb_mode='self', lb_vip='')
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
        self.assertIn('没有填写 VIP', str(caught.exception))

    def test_invalid_vip_is_rejected(self):
        """非法地址、环回与组播都要在编译期拦下。"""
        for value, expected in (
            ('192.168.1.256', '不是合法的 IPv4'),
            ('192.168.1.0/24', '不是合法的 IPv4'),
            ('127.0.0.1', '环回'),
            ('224.0.0.1', '组播'),
        ):
            with self.subTest(vip=value):
                deployment = self.deployment(lb_mode='self', lb_vip=value)
                with self.assertRaises(compiler.CompileError) as caught:
                    self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
                self.assertIn(expected, str(caught.exception))

    def test_vip_matching_a_node_address_is_rejected(self):
        """VIP 与某台机器已有的地址相同：绑定时会直接冲突。"""
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.11')
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(deployment, self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2]))
        self.assertIn('相同', str(caught.exception))

    def test_lb_machine_sharing_a_cluster_role_is_allowed(self):
        """负载均衡机器同时是集群节点：三台机器的拓扑要求它，不该被拦下。

        这条曾经是拒绝的，理由是「清理时两套剧本会在同一台机器上打两遍」。那个理由不成立：
        kubeasz 只有 ``99.clean.yml`` 一个清理入口，它的 ``hosts`` 是四个组取并集并按主机
        去重，而这个渲染出来的两行行首是同一个 ``cluster_address`` —— ansible 认成同一台
        主机，只是同时属于两个组。端口冲突另有解，见下面那条。
        """
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        nodes = self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2])
        nodes[-1].roles = ['loadbalancer', 'node']
        self.assertEqual(len(self.lb_lines(self.render(deployment, nodes))), 2)

    def test_shared_machine_narrows_every_listen_address_to_the_vip(self):
        """只要有一台同机，**所有**负载均衡机器都拿到 ``LB_LISTEN_ADDRESS``。

        收窄覆盖全部而不是只覆盖同机那一台：两台机器的配置保持一致，故障接管时不必先分辨
        「现在顶上的是按哪条路径配的那台」；备机提前监听 VIP 也让接管少一个失败窗口。
        """
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        nodes = self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2])
        nodes[-1].roles = ['loadbalancer', 'master']
        for line in self.lb_lines(self.render(deployment, nodes)):
            self.assertIn(f'{compiler.LB_LISTEN_KEY}=192.168.1.250', line)

    def test_dedicated_machines_do_not_get_a_listen_address(self):
        """专用机器不写这个变量：模板回落成 ``0.0.0.0``，渲染与本改动之前逐字相同。

        这条钉的是「既有拓扑零回归」：没有它，一旦哪天收窄条件写松了，五台机器的集群会
        悄悄多出一个 ``net.ipv4.ip_nonlocal_bind`` 依赖。
        """
        deployment = self.deployment(lb_mode='self', lb_vip='192.168.1.250')
        nodes = self.nodes(['192.168.1.6', '192.168.1.7'], lb_ids=[1, 2])
        self.assertNotIn(compiler.LB_LISTEN_KEY, self.render(deployment, nodes))


class ExternalLbSectionValidationTest(TestCase):
    """``validate_rendered`` 对 [ex_lb] 段与负载均衡机器清单的双向校验。

    少了这道校验，``loadbalancer`` 角色的节点既不会报错、也不会出现在任何段里 —— 被静默丢弃，
    最后表现为「部署全绿、VIP 不通」。
    """

    HOSTS = (
        '[etcd]\n192.168.1.10\n192.168.1.11\n192.168.1.12\n\n'
        '[kube_master]\n192.168.1.10\n192.168.1.11\n192.168.1.12\n\n'
        '[kube_node]\n192.168.1.10\n\n'
        '[ex_lb]\n'
    )
    CONFIG = 'K8S_VER: "1.29.4"\nSECURE_PORT: "6443"\n'

    def masters(self):
        """返回三台 master 快照。

        参数：无。
        返回：返回节点列表。
        副作用：只构造本机对象，不写数据库。
        """
        return [
            make_node('192.168.1.10', ['etcd', 'master', 'node']),
            make_node('192.168.1.11', ['etcd', 'master']),
            make_node('192.168.1.12', ['etcd', 'master']),
        ]

    def validate(self, hosts, nodes):
        """调用 validate_rendered 并返回 ``(是否通过, 错误信息)``。

        参数：`hosts` 表示 inventory 文本；`nodes` 表示节点清单。
        返回：返回二元组。
        副作用：只做纯计算。
        """
        deployment = K8sDeployment(display_name='测试集群', slug='t', kubernetes_version='1.29')
        try:
            compiler.validate_rendered(hosts, self.CONFIG, deployment, nodes)
        except compiler.CompileError as exc:
            return False, str(exc)
        return True, ''

    def test_empty_section_without_lb_machines_passes(self):
        """没有负载均衡机器的集群，空段是正常的。"""
        passed, message = self.validate(self.HOSTS, self.masters())
        self.assertTrue(passed, message)

    def test_lb_machine_missing_from_section_is_rejected(self):
        """机器带 loadbalancer 角色却不在段里：部署时不会有任何动作。"""
        nodes = self.masters() + [make_node('192.168.1.6', ['loadbalancer'])]
        passed, message = self.validate(self.HOSTS, nodes)
        self.assertFalse(passed)
        self.assertIn('192.168.1.6', message)
        self.assertIn('ex_lb', message)

    def test_address_in_section_without_lb_machine_is_rejected(self):
        """段里有地址但机器清单里没有：会装到一台不该装的机器上。"""
        hosts = self.HOSTS + 'LB_ROLE=master EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=6443\n'
        passed, message = self.validate(hosts, self.masters())
        self.assertFalse(passed)
        self.assertIn('ex_lb', message)

    def test_matching_section_and_machines_pass(self):
        """段里的地址与负载均衡机器完全一致时通过。"""
        hosts = (
            self.HOSTS
            + '192.168.1.6 LB_ROLE=master EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=6443\n'
            + '192.168.1.7 LB_ROLE=backup EX_APISERVER_VIP=192.168.1.250 EX_APISERVER_PORT=6443\n'
        )
        nodes = self.masters() + [
            make_node('192.168.1.6', ['loadbalancer']),
            make_node('192.168.1.7', ['loadbalancer']),
        ]
        passed, message = self.validate(hosts, nodes)
        self.assertTrue(passed, message)


class ExternalLbCertHostsTest(TestCase):
    """负载均衡 VIP 必须进 apiserver 证书的 SAN。

    这不是可选打磨：``roles/ex-lb`` 最后一步就是用 admin 证书访问
    ``https://{{ EX_APISERVER_VIP }}:{{ EX_APISERVER_PORT }}`` 自检，VIP 不在 SAN 里时
    这一步自己就会失败，外部 kubectl 同理。
    """

    def setUp(self):
        """写出一棵合成 kubeasz 树并读出变量表。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里创建文件（用例结束后删除）。
        """
        self.root = Path(tempfile.mkdtemp(prefix='k8s-compiler-lb-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.variables = compiler.read_kubeasz_variables(self.root)
        self.template = (self.root / compiler.CONFIG_TEMPLATE).read_text(encoding='utf-8')

    def nodes(self):
        """返回三台 master 与两台负载均衡机器。

        参数：无。
        返回：返回节点快照列表。
        副作用：只构造本机对象，不写数据库。
        """
        return [
            make_node('192.168.1.10', ['etcd', 'master']),
            make_node('192.168.1.11', ['etcd', 'master']),
            make_node('192.168.1.12', ['etcd', 'master']),
            make_node('192.168.1.6', ['loadbalancer']),
            make_node('192.168.1.7', ['loadbalancer']),
        ]

    def cert_hosts(self, **fields):
        """渲染 config.yml 并读出 MASTER_CERT_HOSTS 条目。

        参数：`fields` 表示要覆盖的部署任务字段。
        返回：返回地址列表。
        副作用：只做纯计算。
        """
        values = {
            'display_name': '测试集群',
            'slug': 'demo',
            'kubernetes_version': '1.29',
            'api_server_port': 6443,
        }
        values.update(fields)
        text = compiler.render_config(
            self.template, K8sDeployment(**values), self.variables, nodes=self.nodes(),
        )
        lines = text.splitlines()
        start = lines.index(f'{compiler.MASTER_CERT_HOSTS_KEY}:')
        items = []
        for line in lines[start + 1:]:
            if not line.startswith('  - '):
                break
            items.append(line.strip()[len('- '):].strip('"'))
        return items

    def test_vip_is_appended_after_the_node_addresses(self):
        """VIP 追加在节点地址之后，不插队也不重复。"""
        hosts = self.cert_hosts(lb_mode='self', lb_vip='192.168.1.250')
        self.assertEqual(hosts[-1], '192.168.1.250')
        self.assertEqual(hosts.count('192.168.1.250'), 1)
        self.assertIn('192.168.1.10', hosts)

    def test_disabled_cluster_has_no_extra_entry(self):
        """未启用时不多出任何 SAN：存量集群重新渲染不会因此变化。"""
        hosts = self.cert_hosts()
        self.assertNotIn('192.168.1.250', hosts)

    def test_vip_matching_a_node_address_is_not_duplicated(self):
        """VIP 恰好是某台机器的地址时只留一条（这种配置会在编译期被拦下，这里只保证不去重失败）。"""
        hosts = self.cert_hosts(lb_mode='self', lb_vip='192.168.1.10')
        self.assertEqual(hosts.count('192.168.1.10'), 1)

    def test_certificate_hosts_appends_extra_hosts_at_the_end(self):
        """``certificate_hosts`` 的 extra_hosts 去重后追加在末尾。"""
        nodes = [make_node('10.0.0.1', ['master'], cluster_ip='172.30.0.16')]
        self.assertEqual(
            compiler.certificate_hosts(nodes, extra_hosts=['192.168.1.250']),
            ['10.0.0.1', '172.30.0.16', '192.168.1.250'],
        )
        self.assertEqual(
            compiler.certificate_hosts(nodes, extra_hosts=['172.30.0.16', '192.168.1.250']),
            ['10.0.0.1', '172.30.0.16', '192.168.1.250'],
        )


class CloudLbInventoryTest(TestCase):
    """云内网负载均衡模式：平台什么都不装，只把地址写进证书 SAN。

    这个模式与自建模式的差别全部落在「集群里有没有东西被安装」上：没有 l4lb、没有 keepalived、
    不需要额外的机器、不该出现第 8 个 ezctl 阶段。因此 ``[ex_lb]`` 段必须是空的 —— 写了主机行
    等于让操作员的机器去抢一个属于云负载均衡器的地址。
    """

    TEMPLATE = kubeasz_fixtures.HOSTS_MULTI_TEMPLATE

    def nodes(self):
        """返回三台 master，没有任何负载均衡机器。

        参数：无。
        返回：返回节点快照列表。
        副作用：只构造本机对象，不写数据库。
        """
        return [make_node(f'192.168.1.{10 + index}', ['etcd', 'master']) for index in range(3)]

    def deployment(self, **fields):
        """构造一个云内网负载均衡模式的部署任务。

        参数：`fields` 表示要覆盖的字段。
        返回：返回 K8sDeployment 实例。
        副作用：只构造本机对象，不写数据库。
        """
        values = {
            'display_name': '云 LB 集群',
            'slug': 'cloud-demo',
            'kubernetes_version': '1.29',
            'api_server_port': 6443,
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'lb_mode': 'cloud',
            'lb_vip': '10.0.9.250',
        }
        values.update(fields)
        return K8sDeployment(**values)

    def lb_lines(self, text):
        """取出渲染结果里 ``[ex_lb]`` 段的主机行。

        参数：`text` 表示 inventory 文本。
        返回：返回该段的主机行列表。
        副作用：只做纯计算。
        """
        lines = text.splitlines()
        start = lines.index(f'[{compiler.LB_SECTION}]')
        items = []
        for line in lines[start + 1:]:
            if line.startswith('['):
                break
            if line.strip() and not line.strip().startswith('#'):
                items.append(line.strip())
        return items

    def render(self, deployment=None):
        """渲染一次 inventory。

        参数：`deployment` 表示部署任务。
        返回：返回渲染后的 inventory 文本。
        副作用：只做纯计算。
        """
        return compiler.render_hosts(
            self.TEMPLATE, deployment or self.deployment(), self.nodes(), {},
        )

    def test_cloud_mode_renders_no_lb_host_lines(self):
        """云模式下 [ex_lb] 段头保留、主机行为空、三个 host var 一个都不出现。"""
        text = self.render()
        self.assertIn(f'[{compiler.LB_SECTION}]', text)
        self.assertEqual(self.lb_lines(text), [])
        self.assertNotIn(compiler.LB_ROLE_KEY, text)
        self.assertNotIn(compiler.LB_VIP_KEY, text)
        self.assertNotIn(compiler.LB_PORT_KEY, text)

    def test_cloud_mode_needs_no_balancer_machines(self):
        """一台负载均衡机器都不选也能编译通过 —— 云 LB 不需要集群侧的任何机器。"""
        self.assertEqual(self.lb_lines(self.render()), [])

    def test_cloud_mode_rejects_balancer_machines(self):
        """带了 loadbalancer 角色的机器要报错，而不是默默忽略。

        那台机器既不会被装任何东西、也不会进任何一段：放它过去的结果是「机器选了、什么都没发生」。
        """
        nodes = self.nodes() + [make_node('192.168.1.6', ['loadbalancer'])]
        with self.assertRaises(compiler.CompileError) as caught:
            compiler.render_hosts(self.TEMPLATE, self.deployment(), nodes, {})
        self.assertIn('不需要（也不允许）负载均衡机器', str(caught.exception))

    def test_cloud_mode_requires_an_address(self):
        """没填地址时报错，并且说的是「云负载均衡器的地址」而不是「VIP」。"""
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(self.deployment(lb_vip=''))
        message = str(caught.exception)
        self.assertIn('云内网负载均衡', message)
        self.assertNotIn('VIP', message)

    def test_cloud_mode_rejects_invalid_addresses(self):
        """非法、环回、组播地址都要在编译期拦下。"""
        for value, expected in (
            ('10.0.9.256', '不是合法的 IPv4'),
            ('10.0.9.0/24', '不是合法的 IPv4'),
            ('127.0.0.1', '环回'),
            ('224.0.0.1', '组播'),
        ):
            with self.subTest(address=value):
                with self.assertRaises(compiler.CompileError) as caught:
                    self.render(self.deployment(lb_vip=value))
                self.assertIn(expected, str(caught.exception))

    def test_cloud_mode_rejects_an_address_matching_a_node(self):
        """地址与某台 master 相同：那通常意味着操作员把它当成 VIP 填了。"""
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(self.deployment(lb_vip='192.168.1.11'))
        self.assertIn('相同', str(caught.exception))

    def test_cloud_mode_rejects_an_address_inside_a_cluster_network(self):
        """地址落进 Pod / Service 网段：外部 kubectl 会被路由到集群内部地址上去。"""
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(self.deployment(lb_vip='10.244.5.9'))
        self.assertIn('容器网络', str(caught.exception))

    def test_self_mode_still_requires_machines(self):
        """同一个部署对象换成自建模式后，检查项也换回那一套 —— 分支没有互相污染。"""
        with self.assertRaises(compiler.CompileError) as caught:
            self.render(self.deployment(lb_mode='self'))
        self.assertIn('至少需要 2 台', str(caught.exception))

    def test_lb_mode_defaults_to_none(self):
        """模型默认是不使用；读不出来的值也当不使用。"""
        self.assertEqual(compiler.lb_mode(self.deployment(lb_mode='')), 'none')
        self.assertEqual(compiler.lb_mode(self.deployment(lb_mode='莫名其妙的模式')), 'none')


class CloudLbCertHostsTest(TestCase):
    """云负载均衡器的地址同样要进 apiserver 证书的 SAN。

    不写 SAN 的话，外部 ``kubectl --server=https://<地址>:6443`` 会被
    ``certificate is not valid for`` 拒掉 —— 这个模式唯一的程序侧动作就是这一条。
    """

    def setUp(self):
        """写出一棵合成 kubeasz 树并读出变量表。

        参数：无。
        返回：无显式返回值。
        副作用：在临时目录里创建文件（用例结束后删除）。
        """
        self.root = Path(tempfile.mkdtemp(prefix='k8s-compiler-cloud-lb-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        kubeasz_fixtures.write_kubeasz_tree(self.root)
        self.variables = compiler.read_kubeasz_variables(self.root)
        self.template = (self.root / compiler.CONFIG_TEMPLATE).read_text(encoding='utf-8')

    def cert_hosts(self, **fields):
        """渲染 config.yml 并读出 MASTER_CERT_HOSTS 条目。

        参数：`fields` 表示要覆盖的部署任务字段。
        返回：返回地址列表。
        副作用：只做纯计算。
        """
        values = {
            'display_name': '云 LB 集群',
            'slug': 'cloud-demo',
            'kubernetes_version': '1.29',
            'api_server_port': 6443,
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'lb_mode': 'cloud',
            'lb_vip': '10.0.9.250',
        }
        values.update(fields)
        nodes = [make_node(f'192.168.1.{10 + index}', ['etcd', 'master']) for index in range(3)]
        text = compiler.render_config(
            self.template, K8sDeployment(**values), self.variables, nodes=nodes,
        )
        lines = text.splitlines()
        start = lines.index(f'{compiler.MASTER_CERT_HOSTS_KEY}:')
        items = []
        for line in lines[start + 1:]:
            if not line.startswith('  - '):
                break
            items.append(line.strip()[len('- '):].strip('"'))
        return items

    def test_cloud_address_is_appended_after_the_node_addresses(self):
        """云 LB 地址追加在节点地址之后，不插队也不重复。"""
        hosts = self.cert_hosts()
        self.assertEqual(hosts[-1], '10.0.9.250')
        self.assertEqual(hosts.count('10.0.9.250'), 1)
        self.assertIn('192.168.1.10', hosts)

    def test_none_mode_adds_no_entry(self):
        """不使用外部入口时不多出任何 SAN，残留的地址也不会被带进去。"""
        hosts = self.cert_hosts(lb_mode='none', lb_vip='10.0.9.250')
        self.assertNotIn('10.0.9.250', hosts)
