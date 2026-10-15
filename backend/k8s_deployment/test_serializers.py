"""部署向导提交体的校验规则与标识派生。

这些规则都是「提交时拦住比中途失败便宜」的那一类：节点角色配错、网段重叠、节点名非法，
如果放过去，症状都会出现在 ansible 跑到一半的时候，排查成本远高于让用户改一次表单。
"""

from unittest import mock

from django.conf import settings
from django.test import TestCase

from k8s_cluster.models import K8sCluster
from k8s_config.models import K8sVersionMapping, SupportedOperatingSystem
from k8s_deployment.models import K8sDeployment, K8sDeploymentEvent, K8sDeploymentNode
from k8s_deployment.serializers import (
    MAX_NODES,
    K8sDeploymentCreateSerializer,
    _load_node_payloads,
    _validate_cidrs,
    _validate_lb,
    _validate_node_names,
    _validate_roles,
    derive_slug,
)
from k8s_machine.models import Machine
from ops.models import Organization
from platform_logs.models import AuditLog
from rest_framework import serializers


class NodePayloadTestBase(TestCase):
    """节点结构相关用例的公共夹具。"""

    def setUp(self):
        """准备组织与若干台机器。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入机器记录。
        """
        self.org = Organization.objects.create(name='校验测试组织', slug='serializer-test')
        self.counter = 0

    def make_machine(self, ip_address='10.0.0.1', **extra):
        """创建一台机器。

        参数：`ip_address` 表示机器 IP；`extra` 表示要覆盖的其它字段。
        返回：返回 Machine 实例。
        副作用：向数据库写入一条机器记录。
        """
        self.counter += 1
        return Machine.objects.create(
            organization=self.org,
            name=extra.pop('name', f'machine-{self.counter}'),
            ip_address=ip_address,
            ssh_port=22,
            ssh_user='root',
            **extra,
        )

    def node(self, roles, node_name=''):
        """构造一条节点结构。

        参数：`roles` 表示角色列表；`node_name` 表示手动命名。
        返回：返回 ``_load_node_payloads`` 同构的字典。
        副作用：无。
        """
        return {
            'machine': self.make_machine(ip_address=f'10.0.0.{self.counter + 1}'),
            'roles': list(roles),
            'node_name': node_name,
        }


class LoadNodePayloadsTest(NodePayloadTestBase):
    """原始节点数组翻译成节点结构。"""

    def test_empty_selection_is_refused(self):
        """一台机器都不选没有意义，直接拒绝。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _load_node_payloads([])
        self.assertIn('至少选择一个节点', str(caught.exception))

    def test_too_many_nodes_are_refused(self):
        """超过上限时拒绝，而不是提交一个跑不完的任务。"""
        raw = [{'ip_address': f'10.0.{index // 250}.{index % 250 + 1}'} for index in range(MAX_NODES + 1)]
        with self.assertRaises(serializers.ValidationError) as caught:
            _load_node_payloads(raw)
        self.assertIn(str(MAX_NODES), str(caught.exception))

    def test_machine_can_be_looked_up_by_ip(self):
        """按 IP 指定节点是允许的（向导里就是从机器列表挑）。"""
        machine = self.make_machine(ip_address='10.0.0.9')
        payloads = _load_node_payloads([{'ip_address': '10.0.0.9', 'roles': ['master']}])
        self.assertEqual(payloads[0]['machine'], machine)
        self.assertEqual(payloads[0]['roles'], ['master'])

    def test_machine_can_be_looked_up_by_id(self):
        """按主键指定节点同样允许。"""
        machine = self.make_machine(ip_address='10.0.0.8')
        payloads = _load_node_payloads([{'machine_id': machine.pk}])
        self.assertEqual(payloads[0]['machine'], machine)

    def test_missing_identifier_is_refused(self):
        """既不给人 machine_id 也不给 ip_address 时拒绝。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _load_node_payloads([{'roles': ['master']}])
        self.assertIn('machine_id', str(caught.exception))

    def test_unknown_machine_is_refused(self):
        """找不到机器时给出可操作的提示。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _load_node_payloads([{'ip_address': '10.9.9.9'}])
        self.assertIn('找不到机器', str(caught.exception))

    def test_duplicated_machine_is_refused(self):
        """同一台机器被选两次会让 inventory 出现重复条目，拒绝。"""
        machine = self.make_machine(ip_address='10.0.0.7')
        with self.assertRaises(serializers.ValidationError) as caught:
            _load_node_payloads([{'machine_id': machine.pk}, {'ip_address': '10.0.0.7'}])
        self.assertIn('重复选择', str(caught.exception))

    def test_invalid_role_is_refused(self):
        """角色白名单外的取值一律拒绝。"""
        machine = self.make_machine(ip_address='10.0.0.6')
        with self.assertRaises(serializers.ValidationError) as caught:
            _load_node_payloads([{'machine_id': machine.pk, 'roles': ['worker']}])
        self.assertIn('不支持的角色', str(caught.exception))

    def test_empty_roles_default_to_node(self):
        """不填角色时默认按工作节点处理，与界面上的默认值一致。"""
        machine = self.make_machine(ip_address='10.0.0.5')
        payloads = _load_node_payloads([{'machine_id': machine.pk}])
        self.assertEqual(payloads[0]['roles'], ['node'])

    def test_roles_are_normalized_in_stable_order(self):
        """角色去重并按固定顺序排列，保证同一份输入编译出同一份 inventory。"""
        machine = self.make_machine(ip_address='10.0.0.4')
        payloads = _load_node_payloads([{'machine_id': machine.pk, 'roles': ['master', 'etcd', 'master']}])
        self.assertEqual(payloads[0]['roles'], ['etcd', 'master'])


class ValidateRolesTest(NodePayloadTestBase):
    """拓扑与角色组合的自洽性。"""

    def test_aio_accepts_exactly_one_node(self):
        """单机模式一台机器同时扮演三个角色。"""
        payloads = [self.node(['etcd', 'master', 'node'])]
        _validate_roles(payloads, 'aio', 'deploy_aio')

    def test_aio_with_two_nodes_is_refused(self):
        """单机模式选两台机器是自相矛盾的配置。"""
        payloads = [self.node(['etcd', 'master']), self.node(['node'])]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_roles(payloads, 'aio', 'deploy_aio')
        self.assertIn('只能选择一台机器', str(caught.exception))

    def test_ha_needs_three_masters(self):
        """高可用至少要三台 Master，否则一台宕机就没有控制面。"""
        payloads = [self.node(['etcd', 'master']), self.node(['etcd', 'master']), self.node(['etcd'])]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_roles(payloads, 'ha', 'deploy_ha')
        self.assertIn('3 台 Master', str(caught.exception))

    def test_ha_needs_three_etcd_nodes(self):
        """etcd 少于三台时多数派无从谈起。"""
        payloads = [
            self.node(['etcd', 'master']),
            self.node(['master']),
            self.node(['master']),
        ]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_roles(payloads, 'ha', 'deploy_ha')
        self.assertIn('3 台 etcd', str(caught.exception))

    def test_even_etcd_count_is_refused(self):
        """偶数台 etcd 在脑裂场景下无法形成多数派。"""
        payloads = [
            self.node(['etcd', 'master']),
            self.node(['etcd', 'master']),
            self.node(['etcd', 'master']),
            self.node(['etcd', 'node']),
        ]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_roles(payloads, 'ha', 'deploy_ha')
        self.assertIn('奇数', str(caught.exception))

    def test_ha_with_three_etcd_masters_passes(self):
        """三台 etcd+master 加工作节点是合法的高可用拓扑。"""
        payloads = [
            self.node(['etcd', 'master']),
            self.node(['etcd', 'master']),
            self.node(['etcd', 'master']),
            self.node(['node']),
        ]
        _validate_roles(payloads, 'ha', 'deploy_ha')

    def test_five_etcd_masters_pass(self):
        """五台同样是奇数，属于扩容后的合法拓扑。"""
        payloads = [self.node(['etcd', 'master']) for _ in range(5)]
        _validate_roles(payloads, 'ha', 'deploy_ha')


class ValidateCidrsTest(TestCase):
    """网段校验。"""

    def test_valid_pair_passes(self):
        """默认的容器网段与 Service 网段互不重叠。"""
        _validate_cidrs('10.244.0.0/16', '10.68.0.0/16')

    def test_overlapping_networks_are_refused(self):
        """重叠会让 Pod 访问不到 Service，且部署阶段毫无征兆。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_cidrs('10.244.0.0/16', '10.244.1.0/24')
        self.assertIn('重叠', str(caught.exception))

    def test_too_small_pod_network_is_refused(self):
        """掩码大于 /24 时可用地址不够用。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_cidrs('10.244.0.0/25', '10.68.0.0/16')
        self.assertIn('/24', str(caught.exception))

    def test_too_small_service_network_is_refused(self):
        """Service 网段同样受 /24 限制。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_cidrs('10.244.0.0/16', '10.68.0.0/26')
        self.assertIn('/24', str(caught.exception))

    def test_mixed_ip_versions_are_refused(self):
        """IPv4 与 IPv6 混用是配置错误，不是双栈。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_cidrs('10.244.0.0/16', 'fd00::/64')
        self.assertIn('同一 IP 版本', str(caught.exception))

    def test_invalid_pod_cidr_is_refused(self):
        """写错格式时报到具体字段上。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_cidrs('不是网段', '10.68.0.0/16')
        self.assertIn('pod_cidr', caught.exception.detail)

    def test_invalid_service_cidr_is_refused(self):
        """Service 网段写错同样报到它自己的字段上。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_cidrs('10.244.0.0/16', '10.68.0.0/99')
        self.assertIn('service_cidr', caught.exception.detail)


class ValidateNodeNamesTest(NodePayloadTestBase):
    """手动命名模式下的节点名校验。"""

    def test_auto_mode_skips_the_check(self):
        """自动命名模式下节点名由编译器现场生成，不校验用户输入。"""
        _validate_node_names([self.node(['node'], node_name='')], 'auto')

    def test_missing_name_is_refused(self):
        """手动命名却留空时拒绝，否则会得到一个没有名字的节点。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names([self.node(['node'], node_name='')], 'manual')
        self.assertIn('必须填写节点名称', str(caught.exception))

    def test_uppercase_name_is_refused(self):
        """大写字母会被 kubelet 拒绝，必须在提交时就拦住。

        ``compiler._validate_node_name`` 是返回真假的判定函数，不会抛错；这里曾经按
        「捕获 CompileError」写，结果非法名字一路通过校验，直到任务跑到安装中途才失败。
        """
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names([self.node(['node'], node_name='Node-01')], 'manual')
        self.assertIn('不合法', str(caught.exception))

    def test_underscore_name_is_refused(self):
        """下划线不在 Kubernetes 节点名字符集里。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names([self.node(['node'], node_name='node_01')], 'manual')
        self.assertIn('不合法', str(caught.exception))

    def test_name_starting_with_dash_is_refused(self):
        """必须以字母数字开头与结尾。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names([self.node(['node'], node_name='-node01')], 'manual')
        self.assertIn('不合法', str(caught.exception))

    def test_too_long_name_is_refused(self):
        """超过 63 个字符的名字会被 API Server 拒绝。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names([self.node(['node'], node_name='a' * 64)], 'manual')
        self.assertIn('63', str(caught.exception))

    def test_duplicated_names_are_refused(self):
        """两个节点同名会让 kubelet 注册时互相顶掉。"""
        payloads = [self.node(['node'], node_name='node-01'), self.node(['node'], node_name='node-01')]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names(payloads, 'manual')
        self.assertIn('重复', str(caught.exception))

    def test_valid_names_pass(self):
        """小写字母、数字、- 与 . 的组合都是合法的。"""
        payloads = [
            self.node(['node'], node_name='node-01'),
            self.node(['node'], node_name='worker.02'),
        ]
        _validate_node_names(payloads, 'manual')

    def test_load_balancer_only_machine_needs_no_name(self):
        """纯负载均衡机器不进集群、没有 k8s_nodename，不该被要求填名字。

        ``compiler.node_name_map`` 只给带 master/node 角色的机器算名字，纯 LB 机器根本不会
        写进 inventory —— 对它强制要求节点名，等于逼用户给一台不加入集群的机器编一个
        永远不会生效的名字，而这正是「手动命名」原先一步都走不下去的原因之一。
        """
        payloads = [self.node(['master', 'node'], node_name='master-01'), self.node(['loadbalancer'])]
        _validate_node_names(payloads, 'manual')

    def test_etcd_only_machine_needs_no_name(self):
        """只做 etcd 的机器同理：它不跑 kubelet，没有节点名。"""
        _validate_node_names([self.node(['etcd'])], 'manual')

    def test_duplicate_between_two_node_machines_still_refused_when_lb_present(self):
        """跳过纯 LB 机器不能把重名校验也一起跳过。"""
        payloads = [
            self.node(['master', 'node'], node_name='master-01'),
            self.node(['node'], node_name='master-01'),
            self.node(['loadbalancer']),
        ]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_node_names(payloads, 'manual')
        self.assertIn('重复', str(caught.exception))


class DeriveSlugTest(TestCase):
    """标识派生：kubeasz 集群目录名只接受小写字母、数字与连字符。"""

    def test_ascii_name_is_normalized(self):
        """空格与特殊字符折成连字符。"""
        self.assertEqual(derive_slug('My Cluster 01'), 'my-cluster-01')

    def test_chinese_name_falls_back_to_ascii(self):
        """中文名归一化后为空，平台自己兜住而不是逼用户改名。"""
        slug = derive_slug('生产集群')
        self.assertRegex(slug, r'^[a-z][a-z0-9-]{1,30}$')
        self.assertTrue(slug.startswith('k8s'))

    def test_prefix_is_applied(self):
        """运维任务用前缀区分同名集群的后续任务。"""
        self.assertTrue(derive_slug('demo', prefix='op').startswith('op-demo'))

    def test_leading_digit_is_fixed(self):
        """以数字开头时不合法，前面补 k8s-。"""
        self.assertTrue(derive_slug('123cluster').startswith('k8s-'))

    def test_length_is_capped(self):
        """标识超长会超出字段长度，必须截断。"""
        self.assertLessEqual(len(derive_slug('a' * 200)), 31)

    def test_collision_gets_a_distinct_slug(self):
        """撞名时自动让位，绝不复用已有标识。"""
        K8sDeployment.objects.create(
            display_name='演示', slug='my-cluster', kubernetes_version='1.29.4',
            os_type='Rocky Linux', os_version='8', pod_cidr='10.244.0.0/16',
            service_cidr='10.68.0.0/16',
        )
        other = derive_slug('My Cluster')
        self.assertNotEqual(other, 'my-cluster')
        self.assertRegex(other, r'^[a-z][a-z0-9-]{1,30}$')

    def test_empty_name_still_yields_usable_slug(self):
        """名称为空时也要给出合法标识，不能返回空串。"""
        self.assertRegex(derive_slug(''), r'^[a-z][a-z0-9-]{1,30}$')


class CreateSerializerTest(TestCase):
    """完整提交体的校验。"""

    def setUp(self):
        """准备组织、版本映射、受支持系统与机器。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入测试数据。
        """
        self.org = Organization.objects.create(name='向导测试组织', slug='wizard-test')
        # 版本与系统都用只属于本用例的取值：测试库由迁移带入了真实配置，
        # 撞上同一对「系统 + 版本」会直接违反唯一约束。
        self.mapping = K8sVersionMapping.objects.create(
            kubernetes_version='1.99.9', kubeasz_version='9.9.9',
        )
        self.system = SupportedOperatingSystem.objects.create(os_type='TestOS', version='9')
        self.mapping.supported_systems.add(self.system)
        self.other_system = SupportedOperatingSystem.objects.create(os_type='OtherOS', version='9')
        self.machine = Machine.objects.create(
            organization=self.org, name='node-01', ip_address='10.0.1.1',
            ssh_port=22, ssh_user='root',
        )
        self.machine2 = Machine.objects.create(
            organization=self.org, name='node-02', ip_address='10.0.1.2',
            ssh_port=22, ssh_user='root',
        )

    def payload(self, **overrides):
        """拼出一份合法的提交体。

        参数：`overrides` 表示要覆盖的字段。
        返回：返回请求体字典。
        副作用：无。
        """
        data = {
            'display_name': '向导测试集群',
            'kubernetes_version': '1.99.9',
            'os_type': 'TestOS',
            'os_version': '9',
            'topology_mode': 'aio',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'nodes': [{'machine_id': self.machine.pk, 'roles': ['etcd', 'master', 'node']}],
        }
        data.update(overrides)
        return data

    def test_valid_payload_passes(self):
        """一份完整合法的提交体通过校验。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_kubeasz_version_comes_from_the_mapping(self):
        """kubeasz 版本由配置中心的对应关系推导，不采信前端传值。"""
        data = self.payload(kubeasz_version='9.9.9')
        serializer = K8sDeploymentCreateSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['kubeasz_version'], '9.9.9')

    def test_unknown_kubernetes_version_is_refused(self):
        """未在配置中心启用的版本不能用，否则任务会找不到制品。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(kubernetes_version='1.98.0'))
        self.assertFalse(serializer.is_valid())
        self.assertIn('kubernetes_version', serializer.errors)

    def test_ipvs_is_refused_when_kubeasz_no_longer_supports_it(self):
        """kubeasz 模板不再支持 ipvs 时，提交这一刻就拦下。

        预检里还有一道同样的闸门，但那是任务跑起来之后才发现的：用户会先看到任务开始、
        再看到任务失败，白等一轮。这条用例钉住的是「提交期也拦」。
        """
        serializer = K8sDeploymentCreateSerializer(data=self.payload(kube_proxy_mode='ipvs'))
        self.assertFalse(serializer.is_valid())
        self.assertIn('kube_proxy_mode', serializer.errors)
        self.assertIn('iptables', str(serializer.errors['kube_proxy_mode']))

    def test_nftables_is_accepted_on_new_kubeasz(self):
        """新版本 kubeasz 的模板支持 nftables，可以提交。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(kube_proxy_mode='nftables'))
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_old_kubeasz_refuses_nftables(self):
        """3.6.x 的模板只认 iptables 与 ipvs，nftables 同样在提交期拒绝。"""
        old = K8sVersionMapping.objects.create(kubernetes_version='1.98.6', kubeasz_version='3.6.10')
        old.supported_systems.add(self.system)
        serializer = K8sDeploymentCreateSerializer(data=self.payload(
            kubernetes_version='1.98.6', kube_proxy_mode='nftables',
        ))
        self.assertFalse(serializer.is_valid())
        self.assertIn('kube_proxy_mode', serializer.errors)

    def test_omitted_proxy_mode_is_not_refused(self):
        """提交体里没带模式时不拦：由模型默认值兜底，而那个值在任何版本上都受支持。"""
        data = self.payload()
        data.pop('kube_proxy_mode', None)
        serializer = K8sDeploymentCreateSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(K8sDeployment._meta.get_field('kube_proxy_mode').get_default(), 'iptables')

    def test_disabled_kubernetes_version_is_refused(self):
        """停用的版本不再允许新建集群。"""
        K8sVersionMapping.objects.filter(pk=self.mapping.pk).update(is_active=False)
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertFalse(serializer.is_valid())
        self.assertIn('kubernetes_version', serializer.errors)

    def test_unknown_os_is_refused(self):
        """未登记的操作系统不能选。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(os_type='UnknownOS', os_version='7'),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('os_type', serializer.errors)

    def test_os_not_paired_with_version_is_refused(self):
        """版本与系统必须成对出现：关系表就是「这个版本有这套系统的离线包」的表达。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(os_type='OtherOS', os_version='9'),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('os_type', serializer.errors)

    def test_data_dir_without_path_is_refused(self):
        """勾了自定义数据目录却不填路径时拒绝。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(data_dir_enabled=True, data_dir='   '),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('data_dir', serializer.errors)

    def test_data_dir_is_cleared_when_disabled(self):
        """没勾选时把路径清空，避免残留值被编译器当成真配置。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(data_dir_enabled=False, data_dir='/data/k8s'),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['data_dir'], '')

    def test_port_out_of_range_is_refused(self):
        """端口超出范围时拒绝。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(api_server_port=70000))
        self.assertFalse(serializer.is_valid())
        self.assertIn('api_server_port', serializer.errors)

    def test_blank_display_name_is_refused(self):
        """部署名称是集群展示名，不能为空。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(display_name='   '))
        self.assertFalse(serializer.is_valid())
        self.assertIn('display_name', serializer.errors)

    def test_occupied_machine_is_refused(self):
        """已属于其它集群的机器不能再次分配。"""
        Machine.objects.filter(pk=self.machine.pk).update(cluster_name='别的集群')
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertFalse(serializer.is_valid())
        self.assertIn('已被其它集群占用', str(serializer.errors))

    def test_machine_bound_to_this_deployment_is_accepted(self):
        """向导第 3 步刚绑定的机器必须能提交。

        向导在提交前就把选中的机器绑到了待部署的名称下，机器表里的 ``cluster_name``
        因此等于本次提交的 ``display_name``。那只是展示投影（规划 5.1），不是真占用；
        若按「cluster_name 非空即占用」拦下，向导里一台机器都提交不了。
        """
        Machine.objects.filter(pk=self.machine.pk).update(cluster_name='向导测试集群')
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_machine_bound_by_slug_is_accepted(self):
        """按标识绑定的机器同样算「自己的」（规划 6.2：新数据保存 slug）。"""
        Machine.objects.filter(pk=self.machine.pk).update(cluster_name='prod-hz-01')
        serializer = K8sDeploymentCreateSerializer(data=self.payload(slug='prod-hz-01'))
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_machine_bound_to_another_slug_is_refused(self):
        """机器归属的是别的标识时仍然拦下，不能因为存的是标识就放宽占用判断。"""
        Machine.objects.filter(pk=self.machine.pk).update(cluster_name='prod-hz-09')
        serializer = K8sDeploymentCreateSerializer(data=self.payload(slug='prod-hz-01'))
        self.assertFalse(serializer.is_valid())
        self.assertIn('已被其它集群占用', str(serializer.errors))

    def test_creation_rewrites_the_machine_owner_to_the_slug(self):
        """任务创建时把机器归属改写成标识：向导里的名称投影随改名失效，标识不会。"""
        Machine.objects.filter(pk=self.machine.pk).update(cluster_name='向导测试集群')
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(slug='prod-hz-01'), context={'request': None},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.machine.refresh_from_db()
        self.assertEqual(self.machine.cluster_name, 'prod-hz-01')
        self.assertTrue(
            AuditLog.objects.filter(action='Machine.assign', resource=str(self.machine)).exists(),
            '机器归属被改写时必须留下审计记录',
        )

    def test_creation_leaves_an_already_correct_owner_alone(self):
        """归属已经是该标识时不重复改写，也就不会多出一条无意义的审计。"""
        Machine.objects.filter(pk=self.machine.pk).update(cluster_name='prod-hz-01')
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(slug='prod-hz-01'), context={'request': None},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertFalse(AuditLog.objects.filter(action='Machine.assign').exists())

    def test_machine_taken_by_another_active_task_is_refused(self):
        """机器写进了别的非终态任务快照时不能重复使用（规划 5.1）。"""
        other = K8sDeployment.objects.create(
            display_name='别的任务', slug='other-task', status='running',
            kubernetes_version='1.99.9', kubeasz_version='9.9.9',
            os_type='TestOS', os_version='9', pod_cidr='10.244.0.0/16',
            service_cidr='10.68.0.0/16',
        )
        K8sDeploymentNode.objects.create(
            organization=self.org, deployment=other, machine=self.machine,
            name=self.machine.name, ip_address=str(self.machine.ip_address),
            roles=['node'],
        )
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertFalse(serializer.is_valid())
        self.assertIn('正在被其它任务使用', str(serializer.errors))

    def test_machine_is_released_when_the_other_task_finishes(self):
        """任务进入终态即释放机器，否则同一批机器再也做不了扩容或重装。"""
        other = K8sDeployment.objects.create(
            display_name='已结束的任务', slug='done-task', status='succeeded',
            kubernetes_version='1.99.9', kubeasz_version='9.9.9',
            os_type='TestOS', os_version='9', pod_cidr='10.244.0.0/16',
            service_cidr='10.68.0.0/16',
        )
        K8sDeploymentNode.objects.create(
            organization=self.org, deployment=other, machine=self.machine,
            name=self.machine.name, ip_address=str(self.machine.ip_address),
            roles=['node'],
        )
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_execution_mode_is_recorded(self):
        """执行模式在提交时固化，不出现「以为在真装、其实只是走流程」。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIn(serializer.validated_data['execution_mode'], ('native', 'simulated'))

    def test_omitted_topology_defaults_to_aio(self):
        """不传拓扑模式时按单机处理，而不是抛 KeyError。

        ``topology_mode`` 与 ``node_naming`` 在模型上有默认值，DRF 因此把它们当成非必填，
        但请求里没带就不会进 ``validated_data``；``validate()`` 又是直接取键的，
        缺了会 500。这两个默认值必须显式写在 serializer 上。
        """
        data = self.payload()
        data.pop('topology_mode')
        serializer = K8sDeploymentCreateSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['topology_mode'], 'aio')
        self.assertEqual(serializer.validated_data['node_naming'], 'auto')

    def test_defaults_are_recorded_in_the_submitted_snapshot(self):
        """补全后的默认值要一起进请求快照，事后追溯看到的是真正生效的那份配置。"""
        data = self.payload()
        data.pop('topology_mode')
        serializer = K8sDeploymentCreateSerializer(data=data, context={'request': None})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        deployment = serializer.save()
        submitted = deployment.request_snapshot['submitted']
        self.assertEqual(submitted['topology_mode'], 'aio')
        self.assertEqual(submitted['node_naming'], 'auto')

    def test_create_writes_an_immutable_node_snapshot(self):
        """落库时同时写节点快照，之后改机器表不影响已提交的任务。"""
        machines = [self.machine, self.machine2] + [
            Machine.objects.create(
                organization=self.org, name=f'ha-{index}', ip_address=f'10.0.2.{index}',
                ssh_port=22, ssh_user='root',
            )
            for index in range(1, 3)
        ]
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(topology_mode='ha', nodes=[
                {'machine_id': machines[0].pk, 'roles': ['etcd', 'master']},
                {'machine_id': machines[1].pk, 'roles': ['etcd', 'master']},
                {'machine_id': machines[2].pk, 'roles': ['etcd', 'master']},
                {'machine_id': machines[3].pk, 'roles': ['node']},
            ]),
            context={'request': None},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        deployment = serializer.save()

        self.assertEqual(deployment.status, 'draft')
        self.assertEqual(deployment.nodes.count(), 4)
        Machine.objects.filter(pk=self.machine.pk).update(name='改名了', ssh_port=2222)
        node = K8sDeploymentNode.objects.get(deployment=deployment, machine=self.machine)
        self.assertEqual(node.name, 'node-01')
        self.assertEqual(node.ssh_port, 22)

    def test_manual_naming_flows_through_the_serializer(self):
        """手动命名的节点名要经校验后落进快照，供编译器直接使用。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(
                node_naming='manual',
                nodes=[{
                    'machine_id': self.machine.pk,
                    'roles': ['etcd', 'master', 'node'],
                    'node_name': 'master-01',
                }],
            ),
            context={'request': None},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        deployment = serializer.save()
        self.assertEqual(deployment.nodes.first().node_name, 'master-01')

    def test_invalid_manual_name_is_rejected_at_submit_time(self):
        """非法节点名必须在提交时就被拒，而不是等任务跑到安装中途。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(
                node_naming='manual',
                nodes=[{
                    'machine_id': self.machine.pk,
                    'roles': ['etcd', 'master', 'node'],
                    'node_name': 'Master_01',
                }],
            ),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('不合法', str(serializer.errors))

    def test_user_defined_slug_is_used(self):
        """用户在确认页填写的标识就是最终目录名，不被派生值覆盖。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(slug='prod-hz-01'), context={'request': None},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        deployment = serializer.save()
        self.assertEqual(deployment.slug, 'prod-hz-01')
        self.assertEqual(deployment.request_snapshot['submitted']['slug'], 'prod-hz-01')

    def test_blank_slug_falls_back_to_derived(self):
        """留空表示交给平台派生：快照里记的仍然是最终生效的那个值。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(slug=''), context={'request': None},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        deployment = serializer.save()
        self.assertRegex(deployment.slug, r'^[a-z][a-z0-9-]{1,30}$')
        self.assertEqual(deployment.request_snapshot['submitted']['slug'], deployment.slug)

    def test_illegal_slug_is_refused(self):
        """不满足 kubeasz 目录名规则的标识一律拒绝，并给出中文原因。"""
        for value in ('Prod-HZ', '1prod', 'prod_hz', 'a', '-' * 3):
            serializer = K8sDeploymentCreateSerializer(data=self.payload(slug=value))
            self.assertFalse(serializer.is_valid(), value)
            self.assertIn('slug', serializer.errors, value)

    def test_slug_taken_by_another_deployment_is_refused(self):
        """标识全局唯一：与已有任务撞名时拒绝，而不是自动改名让用户以为填错了。"""
        K8sDeployment.objects.create(
            display_name='已存在的任务', slug='prod-hz-01', kubernetes_version='1.29.4',
            os_type='TestOS', os_version='9', pod_cidr='10.244.0.0/16',
            service_cidr='10.68.0.0/16',
        )
        serializer = K8sDeploymentCreateSerializer(data=self.payload(slug='prod-hz-01'))
        self.assertFalse(serializer.is_valid())
        self.assertIn('slug', serializer.errors)

    def test_slug_taken_by_an_existing_cluster_is_refused(self):
        """集群表里的同名标识同样拒绝：那是 kubeasz 的集群目录，撞上会覆盖别人的集群。"""
        K8sCluster.objects.create(
            organization=self.org, name='已存在的集群', slug='prod-hz-01',
            origin='deployed', status='configured',
        )
        serializer = K8sDeploymentCreateSerializer(data=self.payload(slug='prod-hz-01'))
        self.assertFalse(serializer.is_valid())
        self.assertIn('slug', serializer.errors)

    def test_cert_validity_years_are_accepted_at_both_ends(self):
        """1 年与 100 年（上下限）都要收，默认值 10/3 也要能落库。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(
            cert_validity_enabled=True, ca_validity_years=100, cert_validity_years=1,
        ))
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['ca_validity_years'], 100)
        self.assertEqual(serializer.validated_data['cert_validity_years'], 1)

    def test_cert_validity_years_defaults_when_omitted(self):
        """请求里不带这三项时按模型默认值补全，而不是留空 —— ``validate()`` 要比大小。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(serializer.validated_data['cert_validity_enabled'])
        self.assertEqual(serializer.validated_data['ca_validity_years'], 10)
        self.assertEqual(serializer.validated_data['cert_validity_years'], 3)

    def test_cert_validity_years_out_of_range_is_refused(self):
        """0 与 101 都要被拒 —— 以前 serializer 一个范围都没写，API 能直接塞 0 或 10**9。"""
        for field in ('ca_validity_years', 'cert_validity_years'):
            for value in (0, 101, 10 ** 9):
                with self.subTest(field=field, value=value):
                    serializer = K8sDeploymentCreateSerializer(data=self.payload(
                        cert_validity_enabled=True, **{field: value},
                    ))
                    self.assertFalse(serializer.is_valid())
                    self.assertIn(field, serializer.errors)

    def test_ca_shorter_than_component_certificate_is_refused(self):
        """根证书年限小于组件证书年限时拒绝签发：CA 先过期会让整条证书链失效。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(
            cert_validity_enabled=True, ca_validity_years=3, cert_validity_years=10,
        ))
        self.assertFalse(serializer.is_valid())
        self.assertIn('ca_validity_years', serializer.errors)

    def test_ca_shorter_than_component_is_allowed_when_customization_is_off(self):
        """不开启自定义时这两个值根本不进 config.yml，不必因此拦住提交。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(
            cert_validity_enabled=False, ca_validity_years=3, cert_validity_years=10,
        ))
        self.assertTrue(serializer.is_valid(), serializer.errors)


class StageProgressTest(TestCase):
    """``stage_progress``：时间轴上只能出现 kubeasz 的七个阶段。

    平台自己的阶段（artifacts、kubeasz-home、compile、images、register）也记在
    ``completed_stages`` 里，但它们不在时间轴上。原样拿去当分子会出现「一个 ezctl 阶段都还没
    跑，界面已经显示 5/7」这种误导 —— 192.168.1.94 上卡在 01.prepare 时看到的正是这个数字。
    """

    def setUp(self):
        """准备一个已完成若干平台阶段的部署任务。"""
        self.org = Organization.objects.create(name='进度测试组织', slug='progress-test')
        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            display_name='进度测试集群',
            slug='progress-01',
            kubernetes_version='1.29.4',
            os_type='TestOS',
            os_version='9',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.68.0.0/16',
            completed_stages=[
                'artifacts', 'kubeasz-home', 'compile', 'preflight', 'cluster-init',
                'images', 'register',
            ],
        )

    def test_only_ezctl_stages_count_towards_the_total(self):
        """平台阶段一个都不算：它们不在时间轴上，算了就是虚报进度。"""
        from k8s_deployment.serializers import K8sDeploymentSerializer

        progress = K8sDeploymentSerializer(self.deployment).data['stage_progress']
        self.assertEqual(progress['completed'], [])
        self.assertEqual(progress['total'], 7)
        self.assertEqual(progress['percent'], 0)

    def test_ezctl_stages_are_counted(self):
        """跑完前两个 ezctl 阶段时是 2/7。"""
        from k8s_deployment.serializers import K8sDeploymentSerializer

        self.deployment.completed_stages = [
            'artifacts', 'kubeasz-home', 'compile', 'preflight', 'cluster-init', 'images',
            '01.prepare', '02.etcd',
        ]
        self.deployment.save(update_fields=['completed_stages'])

        progress = K8sDeploymentSerializer(self.deployment).data['stage_progress']
        self.assertEqual(progress['completed'], ['01.prepare', '02.etcd'])
        self.assertEqual((len(progress['completed']), progress['total']), (2, 7))
        self.assertEqual(progress['percent'], 29)


class StageProgressPerTaskTypeTest(TestCase):
    """``stage_progress``：分母按任务类型取，不是所有任务都套七步部署阶段。

    同一个序列化器服务任务列表接口，升级、清理这些任务的 ``completed_stages`` 里写的是别的键；
    不按 ``task_type`` 取清单的话，它们会画出七个永远不变绿的空框，并恒显示「0%（0/7 阶段）」。
    """

    def setUp(self):
        """准备组织与一个集群。"""
        self.org = Organization.objects.create(name='阶段清单组织', slug='stage-specs-test')
        self.cluster = K8sCluster.objects.create(name='阶段清单集群', slug='stage-specs-cluster')

    def make_deployment(self, task_type, **extra):
        """建一个任务。

        参数：`task_type` 表示任务类型；`extra` 表示额外的字段取值。
        返回：返回 K8sDeployment 实例。
        副作用：向数据库写入记录。
        """
        fields = {
            'organization': self.org,
            'display_name': f'{task_type} 任务',
            'slug': f'stage-specs-{task_type}',
            'kubernetes_version': '1.29.4',
            'os_type': 'TestOS',
            'os_version': '9',
            'task_type': task_type,
        }
        fields.update(extra)
        return K8sDeployment.objects.create(**fields)

    def progress_of(self, deployment):
        """取该任务的阶段进度。

        参数：`deployment` 表示任务实例。
        返回：返回 ``stage_progress`` 字典。
        副作用：只读。
        """
        from k8s_deployment.serializers import K8sDeploymentSerializer

        return K8sDeploymentSerializer(deployment).data['stage_progress']

    def test_destroy_uses_the_cleanup_stages(self):
        """清理任务的时间轴是清理那七个阶段，不是部署的七个。"""
        deployment = self.make_deployment('destroy', cluster=self.cluster)
        progress = self.progress_of(deployment)

        self.assertEqual(progress['total'], 7)
        self.assertEqual(
            [item['key'] for item in progress['stages']],
            ['destroy.targets', 'destroy.components', 'destroy.network', 'destroy.config',
             'destroy.registry', 'destroy.path', 'destroy.records'],
        )
        self.assertEqual(progress['stages'][2]['label'], '清理节点网络残留')
        self.assertEqual(progress['percent'], 0)

    def test_destroy_counts_only_its_own_stages(self):
        """平台阶段与部署阶段的键都不进清理的分子。"""
        deployment = self.make_deployment(
            'destroy', cluster=self.cluster,
            completed_stages=['artifacts', 'compile', 'register', '01.prepare', 'destroy.targets'],
        )
        progress = self.progress_of(deployment)

        self.assertEqual(progress['completed'], ['destroy.targets'])
        self.assertEqual((len(progress['completed']), progress['total']), (1, 7))
        self.assertEqual(progress['percent'], 14)

    def test_deploy_and_destroy_stage_keys_never_overlap(self):
        """两份清单的键没有交集 —— 否则一个任务的阶段会被另一个任务的进度算进去。"""
        from k8s_deployment.services import runner

        deploy_keys = {spec['key'] for spec in runner.stage_specs_for('deploy_aio')}
        destroy_keys = {spec['key'] for spec in runner.stage_specs_for('destroy')}
        self.assertEqual(deploy_keys & destroy_keys, set())

    def test_upgrade_uses_its_own_stages(self):
        """升级任务的时间轴是升级那八个阶段：它耗时最长，最需要知道现在停在哪一步。"""
        deployment = self.make_deployment('upgrade', cluster=self.cluster)
        progress = self.progress_of(deployment)

        self.assertEqual(progress['total'], 8)
        self.assertEqual(
            [item['key'] for item in progress['stages']],
            ['artifacts', 'kubeasz-home', 'upgrade-precheck', 'pre-upgrade-backup',
             'preflight', 'images', 'upgrade', 'register'],
        )
        self.assertEqual(progress['stages'][2]['label'], '升级前检查')
        self.assertEqual(progress['percent'], 0)

    def test_upgrade_without_the_forced_backup_drops_that_stage(self):
        """关掉「升级前强制备份」时清单里不画那个框：阶段根本不会开始，画了就是个幽灵。"""
        deployment = self.make_deployment('upgrade', cluster=self.cluster)
        with mock.patch.object(settings, 'K8S_DEPLOY_UPGRADE_REQUIRE_BACKUP', False):
            progress = self.progress_of(deployment)

        self.assertEqual(progress['total'], 7)
        self.assertNotIn('pre-upgrade-backup', [item['key'] for item in progress['stages']])

    def test_upgrade_counts_only_its_own_stages(self):
        """部署阶段与清理阶段的键都不进升级的分子。"""
        deployment = self.make_deployment(
            'upgrade', cluster=self.cluster,
            completed_stages=['compile', '01.prepare', 'artifacts', 'upgrade-precheck', 'upgrade'],
        )
        progress = self.progress_of(deployment)

        self.assertEqual(progress['completed'], ['artifacts', 'upgrade-precheck', 'upgrade'])
        self.assertEqual((len(progress['completed']), progress['total']), (3, 8))
        self.assertEqual(progress['percent'], 38)

    def test_tasks_without_a_declared_plan_get_an_empty_timeline(self):
        """扩容、备份这类任务没有声明清单，返回空数组 —— 前端据此不画阶段条，而不是画一排空框。"""
        for task_type in ('add_worker', 'backup', 'restore'):
            with self.subTest(task_type=task_type):
                deployment = self.make_deployment(task_type, cluster=self.cluster)
                progress = self.progress_of(deployment)

                self.assertEqual(progress['stages'], [])
                self.assertEqual(progress['total'], 0)
                self.assertEqual(progress['percent'], 0)

    def test_stage_keys_are_labelled_for_the_frontend(self):
        """键与中文标题只该有一处定义，序列化器直接把标题一并给出。"""
        deployment = self.make_deployment('deploy_aio')
        progress = self.progress_of(deployment)
        self.assertTrue(progress['stages'])
        for item in progress['stages']:
            self.assertTrue(item['key'])
            self.assertTrue(item['label'])


class DestroyComponentsFieldTest(TestCase):
    """``destroy_components``：清单按集群生成，状态由事件折叠。"""

    def setUp(self):
        """准备组织、集群与一个清理任务。"""
        self.org = Organization.objects.create(name='清理组件组织', slug='destroy-components-test')
        self.cluster = K8sCluster.objects.create(name='清理组件集群', slug='destroy-components-cluster')
        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            display_name='清理组件任务',
            slug='destroy-components-task',
            kubernetes_version='1.29.4',
            os_type='TestOS',
            os_version='9',
            task_type='destroy',
            cluster=self.cluster,
            cni_plugin='calico',
        )
        K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=self.deployment,
            name='k8s-node-1',
            ip_address='10.0.0.1',
            roles=['etcd', 'master', 'node'],
        )

    def components_of(self, deployment=None):
        """取该任务的组件清单。

        参数：`deployment` 表示任务实例，默认取 ``setUp`` 里那个。
        返回：返回组件列表。
        副作用：只读。
        """
        from k8s_deployment.serializers import K8sDeploymentSerializer

        return K8sDeploymentSerializer(deployment or self.deployment).data['destroy_components']

    def add_event(self, component, state):
        """写一条组件进度事件。

        参数：`component` 表示组件标识；`state` 表示状态。
        返回：无显式返回值。
        副作用：向数据库写入事件记录。
        """
        K8sDeploymentEvent.objects.create(
            organization=self.org,
            deployment=self.deployment,
            stage='destroy.components',
            event_type='destroy_component',
            detail={'component': component, 'state': state},
        )

    def test_non_destroy_task_returns_an_empty_list(self):
        """列表接口上每一行都查一次事件表是不可接受的，非清理任务在这里就短路。"""
        other = K8sDeployment.objects.create(
            organization=self.org,
            display_name='升级任务',
            slug='destroy-components-upgrade',
            kubernetes_version='1.29.4',
            os_type='TestOS',
            os_version='9',
            task_type='upgrade',
            cluster=self.cluster,
        )
        self.assertEqual(self.components_of(other), [])

    def test_components_are_generated_for_the_cluster(self):
        """清单按集群真实用的插件生成（Calico 的残留目录跟着出现）。"""
        components = self.components_of()

        self.assertEqual(
            [item['key'] for item in components],
            ['etcd', 'control-plane', 'node-agents', 'runtime', 'cni', 'node-env', 'hosts-entry'],
        )
        cni = next(item for item in components if item['key'] == 'cni')
        self.assertIn('/etc/calico/', cni['paths'])
        etcd = next(item for item in components if item['key'] == 'etcd')
        self.assertEqual(etcd['scope'], '在 1 台etcd 节点上')

    def test_everything_starts_pending(self):
        """还没开跑时全是待清理。"""
        self.assertEqual({item['state'] for item in self.components_of()}, {'pending'})

    def test_events_drive_the_states(self):
        """事件折叠成状态：正在清理的那一项，前面的都已清理。"""
        self.add_event('runtime', 'running')
        states = {item['key']: item['state'] for item in self.components_of()}

        self.assertEqual(states['etcd'], 'done')
        self.assertEqual(states['control-plane'], 'done')
        self.assertEqual(states['node-agents'], 'done')
        self.assertEqual(states['runtime'], 'running')
        self.assertEqual(states['cni'], 'pending')
        self.assertEqual(states['hosts-entry'], 'pending')

    def test_done_events_are_reflected(self):
        """结束时兜底写下的 done 事件要能读回来。"""
        self.add_event('etcd', 'done')
        self.add_event('control-plane', 'done')
        states = {item['key']: item['state'] for item in self.components_of()}
        self.assertEqual(states['etcd'], 'done')
        self.assertEqual(states['control-plane'], 'done')
        self.assertEqual(states['node-agents'], 'pending')

    def test_states_do_not_depend_on_the_current_stage(self):
        """状态只从事件折叠，不看 ``current_stage``。

        ``claim_next_task`` 在重试时会把 ``current_stage`` 清空，拿它当依据会让重试后的页面
        状态往回退。
        """
        self.add_event('cni', 'running')
        self.deployment.current_stage = ''
        self.deployment.completed_stages = []
        self.deployment.save(update_fields=['current_stage', 'completed_stages'])

        states = {item['key']: item['state'] for item in self.components_of()}
        self.assertEqual(states['cni'], 'running')
        self.assertEqual(states['etcd'], 'done')

    def test_events_of_other_types_are_ignored(self):
        """同名阶段下的别的类型事件（比如日志型）不参与折叠。"""
        K8sDeploymentEvent.objects.create(
            organization=self.org,
            deployment=self.deployment,
            stage='destroy.components',
            event_type='destroy_component_done',
            detail={'component': 'etcd', 'state': 'done'},
        )
        self.assertEqual({item['state'] for item in self.components_of()}, {'pending'})


class ValidateExternalLbTest(NodePayloadTestBase):
    """外部负载均衡的提交级校验：VIP 与负载均衡机器。

    这里挡下的都是「编译期也会再判一遍」的同一批规则，两处都留是有意的：表单级让用户当场改，
    编译期那道防的是任务建好之后有人改库或用别的入口塞进一份不自洽的清单。
    """

    def ha_nodes(self):
        """返回三台 etcd+master 的合法高可用节点。

        参数：无。
        返回：返回节点结构列表。
        副作用：向数据库写入机器记录。
        """
        return [self.node(['etcd', 'master']) for _ in range(3)]

    def lb_nodes(self, count=2):
        """返回若干台负载均衡机器。

        参数：`count` 表示台数。
        返回：返回节点结构列表。
        副作用：向数据库写入机器记录。
        """
        return [self.node(['loadbalancer']) for _ in range(count)]

    def test_disabled_ignores_everything(self):
        """未启用时哪怕填了半个 VIP 也不校验，兼容存量表单不带这两个字段的提交。"""
        _validate_lb(self.ha_nodes(), {'lb_mode': 'none', 'lb_vip': '不是地址'})

    def test_enabled_without_vip_is_refused(self):
        """勾了组件却不填 VIP：平台不生成、不猜测，必须由操作员给出。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_lb(self.ha_nodes() + self.lb_nodes(), {'lb_mode': 'self', 'lb_vip': '   '})
        self.assertIn('lb_vip', caught.exception.detail)

    def test_invalid_vip_is_refused(self):
        """非法地址与环回地址都报在 lb_vip 字段上。"""
        for value in ('192.168.1.256', '127.0.0.1', '224.0.0.1'):
            with self.subTest(vip=value):
                with self.assertRaises(serializers.ValidationError) as caught:
                    _validate_lb(
                        self.ha_nodes() + self.lb_nodes(),
                        {'lb_mode': 'self', 'lb_vip': value},
                    )
                self.assertIn('lb_vip', caught.exception.detail)

    def test_vip_matching_a_selected_machine_is_refused(self):
        """VIP 与已选机器的地址相同：绑定时会与节点网卡直接冲突。"""
        nodes = self.ha_nodes()
        vip = nodes[0]['machine'].ip_address
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_lb(nodes + self.lb_nodes(), {'lb_mode': 'self', 'lb_vip': vip})
        self.assertIn('相同', str(caught.exception))

    def test_single_lb_machine_is_refused(self):
        """只有一台负载均衡机器时 keepalived 没有 VRRP 对端。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_lb(
                self.ha_nodes() + self.lb_nodes(count=1),
                {'lb_mode': 'self', 'lb_vip': '10.0.0.250'},
            )
        self.assertIn('至少需要 2 台', str(caught.exception))

    def test_lb_machine_sharing_a_cluster_role_is_accepted(self):
        """负载均衡机器同时是集群节点：三台机器的拓扑要求它，表单层不该再挡。

        真正的把关不在这里 —— 同机要成立，还得让 l4lb 改绑 VIP 并打开
        ``net.ipv4.ip_nonlocal_bind``，那两件事在编译期（``compiler.lb_listen_address``）
        与运行期（``state_machine._prepare_shared_lb_nodes``）各有一道。表单层只负责
        「选够两台了没有」这件事。
        """
        nodes = self.ha_nodes()
        nodes.append(self.node(['loadbalancer', 'node']))
        nodes.append(self.node(['loadbalancer']))
        _validate_lb(nodes, {'lb_mode': 'self', 'lb_vip': '10.0.0.250'})

    def test_valid_combination_passes(self):
        """三台 etcd+master 加两台独立负载均衡机器是合法的。"""
        _validate_lb(
            self.ha_nodes() + self.lb_nodes(),
            {'lb_mode': 'self', 'lb_vip': '10.0.0.250'},
        )

    def test_cloud_mode_accepts_an_address_without_machines(self):
        """云内网负载均衡：只要地址，一台机器都不用选。"""
        _validate_lb(self.ha_nodes(), {'lb_mode': 'cloud', 'lb_vip': '10.0.0.250'})

    def test_cloud_mode_without_an_address_is_refused(self):
        """云模式下不填地址等于证书 SAN 里什么都不加，外部 kubectl 必然被拒。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_lb(self.ha_nodes(), {'lb_mode': 'cloud', 'lb_vip': '   '})
        self.assertIn('lb_vip', caught.exception.detail)

    def test_cloud_mode_with_an_invalid_address_is_refused(self):
        """云模式同样只支持一个合法的 IPv4 单播地址。"""
        for value in ('10.0.0.256', '127.0.0.1', '224.0.0.1'):
            with self.subTest(address=value):
                with self.assertRaises(serializers.ValidationError) as caught:
                    _validate_lb(self.ha_nodes(), {'lb_mode': 'cloud', 'lb_vip': value})
                self.assertIn('lb_vip', caught.exception.detail)

    def test_cloud_mode_with_balancer_machines_is_refused(self):
        """云模式下带了负载均衡机器要报在 nodes 上：那台机器上什么都不会被安装。"""
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_lb(
                self.ha_nodes() + self.lb_nodes(),
                {'lb_mode': 'cloud', 'lb_vip': '10.0.0.250'},
            )
        self.assertIn('nodes', caught.exception.detail)
        self.assertIn('不需要（也不允许）负载均衡机器', str(caught.exception))

    def test_cloud_mode_on_a_single_node_cluster_is_refused(self):
        """单节点集群不能选云内网负载均衡：一个 apiserver 后面没有可切换的后端。

        自建模式这条由 ``_validate_roles`` 挡下（它看的是「机器里有没有 loadbalancer 角色」），
        云模式没有这样的机器，少了这一判就会「提交成功」然后死在运行时的预检上。
        """
        payloads = [self.node(['etcd', 'master', 'node'])]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_lb(payloads, {'lb_mode': 'cloud', 'lb_vip': '10.0.0.250'})
        self.assertIn('nodes', caught.exception.detail)
        self.assertIn('至少需要 3 个控制面节点', str(caught.exception))

    def test_address_inside_the_cluster_networks_is_refused(self):
        """外部访问地址落在容器网络或 Service 网段里，两种模式都拒。

        与预检 ``_lb_address_failures`` 同口径：那种地址会被路由到某个 Pod 或 Service 上，
        自建的 VIP 与云内网 LB 都救不回来。
        """
        cases = (
            ('pod_cidr', '10.244.0.0/16', '容器网络'),
            ('service_cidr', '10.68.0.0/16', 'Service 网段'),
        )
        for mode in ('self', 'cloud'):
            for field, cidr, label in cases:
                with self.subTest(mode=mode, field=field):
                    payloads = self.ha_nodes() + (self.lb_nodes() if mode == 'self' else [])
                    address = cidr.split('/')[0].rsplit('.', 1)[0] + '.9'
                    with self.assertRaises(serializers.ValidationError) as caught:
                        _validate_lb(payloads, {'lb_mode': mode, 'lb_vip': address, field: cidr})
                    self.assertIn('lb_vip', caught.exception.detail)
                    self.assertIn(label, str(caught.exception))

    def test_invalid_cidr_is_left_to_the_cidr_rule(self):
        """网段本身写错时不在这一项上重复报：那个错由 ``_validate_cidrs`` 一次性说清。"""
        _validate_lb(
            self.ha_nodes(),
            {'lb_mode': 'cloud', 'lb_vip': '10.0.0.250', 'pod_cidr': '不是网段', 'service_cidr': ''},
        )

    def test_unknown_mode_is_ignored_like_disabled(self):
        """认不出来的模式按「不使用」处理，不因为一个脏值把提交变成自建模式。"""
        _validate_lb(self.ha_nodes(), {'lb_mode': '莫名其妙的模式', 'lb_vip': '不是地址'})


class ValidateRolesWithLbTest(NodePayloadTestBase):
    """``loadbalancer`` 角色与既有拓扑的相互作用。"""

    def test_lb_machines_do_not_count_as_masters_or_etcd(self):
        """三台 etcd+master 加两台负载均衡机器仍然是合法的 HA 拓扑。

        负载均衡机器既不是 etcd 成员也不是 master：把它们算进「至少 3 台」的计数里，
        合法的拓扑会被拒，而已有的 3 台会变成 5 台 —— 两处都会错。
        """
        payloads = [self.node(['etcd', 'master']) for _ in range(3)]
        payloads += [self.node(['loadbalancer']) for _ in range(2)]
        _validate_roles(payloads, 'ha', 'deploy_ha')

    def test_aio_with_lb_machine_says_why(self):
        """单节点带负载均衡机器要报出真正的原因，而不是含糊的「只能选择一台机器」。"""
        payloads = [self.node(['etcd', 'master', 'node']), self.node(['loadbalancer'])]
        with self.assertRaises(serializers.ValidationError) as caught:
            _validate_roles(payloads, 'aio', 'deploy_aio')
        self.assertIn('不支持外部负载均衡', str(caught.exception))

    def test_roles_are_sorted_with_lb_last(self):
        """``VALID_ROLES`` 里负载均衡排在最后，既有角色的顺序不因新增角色而改变。"""
        machine = self.make_machine(ip_address='10.0.0.99')
        payloads = _load_node_payloads(
            [{'machine_id': machine.pk, 'roles': ['loadbalancer', 'master']}],
        )
        self.assertEqual(payloads[0]['roles'], ['master', 'loadbalancer'])


class CreateSerializerExternalLbTest(TestCase):
    """完整提交体里的外部负载均衡字段。"""

    def setUp(self):
        """准备组织、版本映射与五台机器。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入测试数据。
        """
        self.org = Organization.objects.create(name='LB 向导组织', slug='lb-wizard-test')
        self.mapping = K8sVersionMapping.objects.create(
            kubernetes_version='1.97.7', kubeasz_version='7.7.7',
        )
        self.system = SupportedOperatingSystem.objects.create(os_type='LbTestOS', version='7')
        self.mapping.supported_systems.add(self.system)
        self.machines = [
            Machine.objects.create(
                organization=self.org, name=f'lb-node-{index}', ip_address=f'10.9.0.{index}',
                ssh_port=22, ssh_user='root',
            )
            for index in range(1, 6)
        ]

    def payload(self, **overrides):
        """拼出一份「三台 etcd+master 加两台负载均衡机器」的合法提交体。

        参数：`overrides` 表示要覆盖的字段。
        返回：返回请求体字典。
        副作用：无。
        """
        data = {
            'display_name': 'LB 向导集群',
            'kubernetes_version': '1.97.7',
            'os_type': 'LbTestOS',
            'os_version': '7',
            'topology_mode': 'ha',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.68.0.0/16',
            'nodes': [
                {'machine_id': item.pk, 'roles': ['etcd', 'master']} for item in self.machines[:3]
            ] + [
                {'machine_id': item.pk, 'roles': ['loadbalancer']} for item in self.machines[3:]
            ],
        }
        data.update(overrides)
        return data

    def test_payload_without_lb_fields_is_not_required(self):
        """不带这两个字段的提交体仍然合法（模型有默认值，但 DRF 不会替它补上）。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['lb_mode'], 'none')
        self.assertEqual(serializer.validated_data['lb_vip'], '')

    def test_enabled_requires_a_vip(self):
        """启用但 VIP 为空时报在 lb_vip 上，而不是等到编译阶段才炸。"""
        serializer = K8sDeploymentCreateSerializer(data=self.payload(lb_mode='self', lb_vip=''))
        self.assertFalse(serializer.is_valid())
        self.assertIn('lb_vip', serializer.errors)

    def test_vip_is_trimmed_and_kept(self):
        """VIP 前后空白去掉后落库。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(lb_mode='self', lb_vip=' 10.9.0.250 '),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['lb_vip'], '10.9.0.250')

    def test_disabled_clears_the_vip(self):
        """没勾选时把 VIP 清空，避免残留值被编译器当成真配置。"""
        serializer = K8sDeploymentCreateSerializer(
            data=self.payload(lb_mode='none', lb_vip='10.9.0.250'),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['lb_vip'], '')


class ExternalLbReportingTest(TestCase):
    """负载均衡机器在节点数与阶段数上的口径。"""

    def setUp(self):
        """准备组织、一个集群与一个启用了外部负载均衡的 HA 任务。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入测试数据。
        """
        self.org = Organization.objects.create(name='LB 口径组织', slug='lb-reporting-test')
        self.cluster = K8sCluster.objects.create(name='LB 口径集群', slug='lb-reporting-cluster')
        self.counter = 0

    def make_deployment(self, **extra):
        """建一个任务并写入三台 master 与两台负载均衡机器的节点快照。

        参数：`extra` 表示要覆盖的字段。
        返回：返回 K8sDeployment 实例。
        副作用：向数据库写入任务与节点记录。
        """
        # 一个用例里可能要建两个任务（启用与未启用各一个），slug 必须各不相同。
        self.counter += 1
        fields = {
            'organization': self.org,
            'display_name': 'LB 口径任务',
            'slug': f'lb-reporting-task-{self.counter}',
            'kubernetes_version': '1.29.4',
            'os_type': 'TestOS',
            'os_version': '9',
            'task_type': 'deploy_ha',
            'topology_mode': 'ha',
        }
        fields.update(extra)
        deployment = K8sDeployment.objects.create(**fields)
        for index in range(3):
            K8sDeploymentNode.objects.create(
                organization=self.org, deployment=deployment, name=f'master-{index}',
                ip_address=f'10.8.0.{index}', roles=['etcd', 'master'],
            )
        for index in range(2):
            K8sDeploymentNode.objects.create(
                organization=self.org, deployment=deployment, name=f'lb-{index}',
                ip_address=f'10.8.0.{10 + index}', roles=['loadbalancer'],
            )
        return deployment

    def data_of(self, deployment):
        """取任务的序列化结果。

        参数：`deployment` 表示任务实例。
        返回：返回序列化后的字典。
        副作用：只读。
        """
        from k8s_deployment.serializers import K8sDeploymentSerializer

        return K8sDeploymentSerializer(deployment).data

    def test_node_count_excludes_load_balancers(self):
        """节点数只数集群节点：算上负载均衡机器会与 ``kubectl get node`` 对不上。"""
        deployment = self.make_deployment()
        self.assertEqual(deployment.cluster_node_count, 3)
        self.assertEqual(self.data_of(deployment)['node_count'], 3)

    def test_node_count_keeps_machines_that_share_a_cluster_role(self):
        """同机的机器照样要算：三机拓扑里那两台既当 master 又当负载均衡，它们出现在
        ``kubectl get node`` 里 —— 按「含有 loadbalancer 就排除」去数，一个三台机器的高可用
        集群会被报成只有 1 个节点。"""
        deployment = self.make_deployment()
        K8sDeploymentNode.objects.filter(
            deployment=deployment, name__in=['master-0', 'master-1'],
        ).update(roles=['etcd', 'master', 'loadbalancer'])

        self.assertEqual(deployment.cluster_node_count, 3)
        self.assertEqual(self.data_of(deployment)['node_count'], 3)

    def test_stage_total_is_seven_when_disabled(self):
        """未启用时时间轴仍是七步 —— 多画一步会永远不亮。"""
        deployment = self.make_deployment()
        progress = self.data_of(deployment)['stage_progress']
        self.assertEqual(progress['total'], 7)
        self.assertNotIn('10.ex-lb', [item['key'] for item in progress['stages']])

    def test_stage_total_is_eight_when_enabled(self):
        """启用后时间轴是八步，最后一步是外部负载均衡。"""
        deployment = self.make_deployment(lb_mode='self', lb_vip='10.8.0.250')
        progress = self.data_of(deployment)['stage_progress']
        self.assertEqual(progress['total'], 8)
        stages = progress['stages']
        self.assertEqual(stages[-1]['key'], '10.ex-lb')
        self.assertEqual(stages[-1]['label'], '部署外部负载均衡')

    def test_lb_stage_counts_towards_the_percentage(self):
        """启用时跑完八步是 100%，不会因为分母只有七而溢出。"""
        deployment = self.make_deployment(
            lb_mode='self', lb_vip='10.8.0.250',
            completed_stages=[
                '01.prepare', '02.etcd', '03.runtime', '04.kube-master', '05.kube-node',
                '06.network', '07.addons', '10.ex-lb',
            ],
        )
        progress = self.data_of(deployment)['stage_progress']
        self.assertEqual(len(progress['completed']), 8)
        self.assertEqual(progress['percent'], 100)

    def test_cloud_lb_keeps_the_stage_total_at_seven(self):
        """云内网负载均衡没有第 8 步：平台不在集群里装任何代理，这一步永远无事可做。

        多画一步的后果与「未启用却画八步」一样 —— 一个永远不会变绿的空框，而且它会一直占着
        分母，让一个已经跑完的集群显示成 87%。
        """
        deployment = self.make_deployment(lb_mode='cloud', lb_vip='10.8.0.250')
        progress = self.data_of(deployment)['stage_progress']
        self.assertEqual(progress['total'], 7)
        self.assertNotIn('10.ex-lb', [item['key'] for item in progress['stages']])

    def test_cloud_lb_address_is_exposed_but_not_a_vip(self):
        """云模式下地址照常进 SAN 与集群记录，但模式是 cloud —— 清理任务据此不列负载均衡组件。"""
        from k8s_deployment.services import compiler

        deployment = self.make_deployment(lb_mode='cloud', lb_vip='10.8.0.250')
        self.assertEqual(compiler.lb_vip(deployment), '10.8.0.250')
        self.assertEqual(compiler.lb_mode(deployment), 'cloud')
        self.assertEqual(self.data_of(deployment)['lb_mode'], 'cloud')

    def test_lb_vip_is_exposed_on_the_cluster_record(self):
        """集群上的入口地址从任务字段回写，概览页据此显示外部访问入口。"""
        from k8s_deployment.services import compiler

        deployment = self.make_deployment(lb_mode='self', lb_vip='10.8.0.250')
        self.assertEqual(compiler.lb_vip(deployment), '10.8.0.250')
        self.assertEqual(compiler.lb_vip(self.make_deployment()), '')
