"""已有集群的运维任务构造与运维接口的行为。

覆盖两件事：
1. ``cluster_ops`` 把集群既有状态翻译成新任务时的各项拒绝条件——宁可报错也不猜；
2. ``k8s/clusters`` 上那批运维接口的权限语义、二次确认与备份保护，
   确认「绕过界面直接调接口」同样被拦住。
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from k8s_cluster.models import K8sCluster, K8sClusterConfigRevision
from k8s_deployment.models import K8sDeployment, K8sDeploymentNode
from k8s_deployment.services import cluster_ops, register
from k8s_machine.models import Machine
from ops.models import (
    Organization,
    PermissionMenuAction,
    PermissionMenuNode,
    PermissionPolicy,
    PermissionRule,
    UserProfile,
)
from ops.permission_catalog import all_permission_codes, required_codes_for_permission
from ops.permission_service import clear_permission_cache
from rest_framework.test import APIClient

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'

# 渲染后的 inventory 片段。集群变量是顶格写的，与 kubeasz 的 hosts 模板一致 ——
# ``compiler.inventory_variable`` 只认行首的 ``名=值``，缩进一格就读不到。
HOSTS_VARS = """\
[all:vars]
SECURE_PORT="6443"
# K8S Service CIDR, not overlap with node(host) networking
SERVICE_CIDR="10.254.0.0/16"
# Cluster CIDR (Pod CIDR), not overlap with node(host) networking
CLUSTER_CIDR="172.36.0.0/16"
"""

SNAPSHOT = {
    'schema': '1',
    'kubeasz_version': '3.6.7',
    'kubernetes_version': '1.29.4',
    'os_type': 'Rocky Linux',
    'os_version': '8',
    'runtime': 'containerd',
    'network_plugin': 'calico',
    'topology_mode': 'ha',
    'pod_cidr': '172.36.0.0/16',
    'service_cidr': '10.254.0.0/16',
    'api_server_port': 6443,
    'hosts': HOSTS_VARS,
    'nodes': [
        {
            'name': 'node-1', 'ip_address': '192.168.30.10', 'ssh_port': 22,
            'ssh_user': 'root', 'roles': ['etcd', 'master'], 'node_name': 'master-1',
        },
        {
            'name': 'node-2', 'ip_address': '192.168.30.11', 'ssh_port': 22,
            'ssh_user': 'root', 'roles': ['etcd', 'master'], 'node_name': 'master-2',
        },
        {
            'name': 'node-3', 'ip_address': '192.168.30.12', 'ssh_port': 22,
            'ssh_user': 'root', 'roles': ['etcd', 'master'], 'node_name': 'master-3',
        },
        {
            'name': 'node-4', 'ip_address': '192.168.30.13', 'ssh_port': 22,
            'ssh_user': 'root', 'roles': ['node'], 'node_name': 'worker-1',
        },
    ],
}


class ClusterOpsTestBase(TestCase):
    """运维任务构造用例的公共夹具。"""

    def setUp(self):
        """准备一个已部署且带配置版本的集群。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入组织、集群与配置版本。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-ops-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
            K8S_DEPLOY_AUTOSTART_WORKER=False,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.org = Organization.objects.create(name='运维测试组织', slug='cluster-ops-test')
        self.cluster = K8sCluster.objects.create(
            organization=self.org,
            name='运维测试集群',
            slug='ops-cluster',
            origin='deployed',
            kube_version='1.29.4',
            network_plugin='calico',
            status='configured',
        )
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=1,
            config_hash='c' * 64,
            snapshot=SNAPSHOT,
        )

    def make_machine(self, ip_address='192.168.30.50', name='new-machine'):
        """创建一台可加入集群的机器。

        参数：`ip_address` 表示机器 IP；`name` 表示主机名。
        返回：返回 Machine 实例。
        副作用：向数据库写入一条机器记录。
        """
        return Machine.objects.create(
            organization=self.org,
            name=name,
            ip_address=ip_address,
            ssh_port=22,
            ssh_user='root',
        )

    def set_cluster_proxy_mode(self, mode, structured=True):
        """把集群当前生效的配置版本改成某个 kube-proxy 模式。

        参数：`mode` 表示模式文本；`structured` 为假时**只**把模式留在 hosts 文本里，
            用来覆盖「快照里没有 ``kube_proxy_mode`` 字段」的那种历史数据。
        返回：无显式返回值。
        副作用：更新配置版本的快照。
        """
        revision = (
            K8sClusterConfigRevision.objects
            .filter(cluster=self.cluster)
            .order_by('-revision')
            .first()
        )
        snapshot = dict(revision.snapshot)
        if structured:
            snapshot['kube_proxy_mode'] = mode
        else:
            snapshot['hosts'] = f'{snapshot.get("hosts", "")}\nPROXY_MODE="{mode}"\n'
        revision.snapshot = snapshot
        revision.save(update_fields=['snapshot'])

    def register_version(self, kubernetes_version, kubeasz_version):
        """在配置中心登记一条版本对应关系。

        参数：`kubernetes_version` 表示 Kubernetes 次版本号；`kubeasz_version` 表示配套的 kubeasz 版本。
        返回：返回 K8sVersionMapping 实例。
        副作用：向数据库写入或更新一条映射。

        升级目标必须在配置中心登记过：制品是按这份映射同步的，反过来它也是「这个版本存在」
        的唯一凭据。库里有迁移种下的 1.33–1.36，用例要用别的版本对就自己补一行。
        """
        from k8s_config.models import K8sVersionMapping

        mapping, _created = K8sVersionMapping.objects.update_or_create(
            kubernetes_version=kubernetes_version,
            defaults={'kubeasz_version': kubeasz_version, 'is_active': True},
        )
        return mapping


class InheritedOptionsTest(ClusterOpsTestBase):
    """运维任务的公共字段来自集群的既有状态。"""

    def test_inherits_versions_from_active_revision(self):
        """kubeasz 与 Kubernetes 版本沿用集群当前生效的配置版本。"""
        options = cluster_ops.inherited_options(self.cluster, 'add_worker')
        self.assertEqual(options['kubeasz_version'], '3.6.7')
        self.assertEqual(options['kubernetes_version'], '1.29.4')

    def test_inherits_os_and_network_from_snapshot(self):
        """系统与网络插件从配置版本快照里取，不需要用户重新填。"""
        options = cluster_ops.inherited_options(self.cluster, 'add_worker')
        self.assertEqual(options['os_type'], 'Rocky Linux')
        self.assertEqual(options['os_version'], '8')
        self.assertEqual(options['cni_plugin'], 'calico')
        self.assertEqual(options['runtime'], 'containerd')
        self.assertEqual(options['topology_mode'], 'ha')

    def test_lb_fields_are_inherited_from_the_cluster_record(self):
        """集群上的入口模式与地址要继承到运维任务上：清理任务靠它决定列不列「外部负载均衡」。"""
        self.cluster.api_server_vip = '192.168.1.250'
        self.cluster.lb_mode = 'self'
        self.cluster.save(update_fields=['api_server_vip', 'lb_mode'])

        options = cluster_ops.inherited_options(self.cluster, 'add_worker')
        self.assertEqual(options['lb_vip'], '192.168.1.250')
        self.assertEqual(options['lb_mode'], 'self')

    def test_cluster_without_a_vip_stays_disabled(self):
        """没有入口的集群就是从来没启用过外部负载均衡，不能继承出一个模式来。"""
        options = cluster_ops.inherited_options(self.cluster, 'add_worker')
        self.assertEqual(options['lb_vip'], '')
        self.assertEqual(options['lb_mode'], 'none')

    def test_cloud_lb_is_inherited_as_cloud_not_self(self):
        """云内网负载均衡的集群要继承成 cloud。

        只凭 ``api_server_vip`` 非空反推会把云 LB 误判成自建，于是清理清单里会多出一项
        永远不会执行的「外部负载均衡」——云模式下集群里根本没有 l4lb/keepalived 可清。
        """
        self.cluster.api_server_vip = '10.0.9.250'
        self.cluster.lb_mode = 'cloud'
        self.cluster.save(update_fields=['api_server_vip', 'lb_mode'])

        options = cluster_ops.inherited_options(self.cluster, 'add_worker')
        self.assertEqual(options['lb_mode'], 'cloud')
        self.assertEqual(options['lb_vip'], '10.0.9.250')

    def test_lb_fields_are_declared_as_inherited(self):
        """两个字段既要在继承清单里，也要真的能被透传到任务上。

        前一条断言锁的是意图（``INHERITED_FIELDS`` 是给人看的清单），后一条锁的是机制：
        ``build_task`` 按 ``TASK_FIELD_NAMES`` 白名单透传，字段名不在这份白名单里（或落进
        ``EXPLICIT_FIELD_NAMES``）就会在创建任务时被静默丢掉，清理清单也就列不出来。
        """
        for field in ('lb_mode', 'lb_vip'):
            with self.subTest(field=field):
                self.assertIn(field, cluster_ops.INHERITED_FIELDS)
                self.assertIn(field, cluster_ops.TASK_FIELD_NAMES)
                self.assertNotIn(field, cluster_ops.EXPLICIT_FIELD_NAMES)

    def test_restore_uses_explicit_version_and_backup_id(self):
        """恢复任务的版本来自备份记录，且必须把 backup_id 写进快照。"""
        options = cluster_ops.inherited_options(
            self.cluster, 'restore', {'backup_id': 'bk-123', 'kubernetes_version': '1.29.9'},
        )
        self.assertEqual(options['backup_id'], 'bk-123')
        self.assertEqual(options['kubernetes_version'], '1.29.9')

    def test_restore_without_explicit_version_is_rejected(self):
        """恢复没给版本时不猜当前版本，直接拒绝。"""
        options = cluster_ops.inherited_options(self.cluster, 'restore', {'backup_id': 'bk-123'})
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops._validate_options('restore', options)
        self.assertIn('必须显式指定', str(caught.exception))

    def test_missing_kubeasz_version_is_rejected(self):
        """集群没有配置版本时无法确定 kubeasz 版本，拒绝构造任务。"""
        K8sClusterConfigRevision.objects.filter(cluster=self.cluster).delete()
        options = cluster_ops.inherited_options(self.cluster, 'add_worker')
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops._validate_options('add_worker', options)
        self.assertIn('kubeasz', str(caught.exception))


class NetworkFactsInheritanceTest(ClusterOpsTestBase):
    """扩容与升级必须沿用集群真实的网段与 ApiServer 端口。

    这三种任务（添加节点、添加 Master、补丁升级）的表单里没有网段输入框：网段在集群建好
    那一刻就定了，改它等于重建集群。平台侧唯一的事实来源是配置版本快照 —— 94 上「添加 Node」
    报「容器网络 CIDR 未填写」，就是这里给了个空串，把责任推给了一个用户填不了的地方。
    """

    def replace_revision(self, snapshot):
        """把集群的配置版本换成给定的快照。

        参数：`snapshot` 表示新的快照字典。
        返回：无显式返回值。
        副作用：删除旧版本行并写入一条新的。
        """
        K8sClusterConfigRevision.objects.filter(cluster=self.cluster).delete()
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=1,
            config_hash='c' * 64,
            snapshot=snapshot,
        )

    def test_the_cluster_network_is_inherited(self):
        """新任务上的网段与端口就是集群的，不是模型默认值。"""
        options = cluster_ops.inherited_options(self.cluster, 'add_worker')

        self.assertEqual(options['pod_cidr'], '172.36.0.0/16')
        self.assertEqual(options['service_cidr'], '10.254.0.0/16')
        self.assertEqual(options['api_server_port'], 6443)

    def test_the_request_body_cannot_override_the_cluster_network(self):
        """请求体里带了别的网段也不作数。

        扩容与升级执行时用的是集群快照里的配置，不会按请求体重渲染 —— 跟着请求体走只会让
        预检拿一个与集群无关的网段去比对，报出来的却像是用户填错了。
        """
        options = cluster_ops.inherited_options(self.cluster, 'add_worker', {
            'pod_cidr': '10.1.0.0/16',
            'service_cidr': '10.2.0.0/16',
            'api_server_port': 8443,
        })

        self.assertEqual(options['pod_cidr'], '172.36.0.0/16')
        self.assertEqual(options['service_cidr'], '10.254.0.0/16')
        self.assertEqual(options['api_server_port'], 6443)

    def test_an_old_snapshot_without_the_structured_keys_still_resolves(self):
        """没有结构化键的老快照要从 inventory 文本里读回来。

        本轮之前部署的集群（94 上的 jd-k8s 就是）快照里只有渲染后的 hosts 文本，
        三个值写在 ``all:vars`` 里。这条是现场唯一真跑过的路径，不能只认新键。
        """
        self.replace_revision({
            key: value for key, value in SNAPSHOT.items()
            if key not in ('pod_cidr', 'service_cidr', 'api_server_port')
        })

        options = cluster_ops.inherited_options(self.cluster, 'add_worker')

        self.assertEqual(options['pod_cidr'], '172.36.0.0/16')
        self.assertEqual(options['service_cidr'], '10.254.0.0/16')
        self.assertEqual(options['api_server_port'], 6443)

    def test_the_inventory_fallback_reads_a_non_default_port(self):
        """集群建在 8443 上时，运维任务不能悄悄回到 6443。

        端口会进预检、进审计记录，也是云内网负载均衡提示里后端目标的端口 ——
        编一个默认值出来，用户照着配的云 LB 会指向一个没人监听的端口。
        """
        self.replace_revision(dict(
            SNAPSHOT,
            api_server_port=8443,
            hosts=HOSTS_VARS.replace('SECURE_PORT="6443"', 'SECURE_PORT="8443"'),
        ))

        options = cluster_ops.inherited_options(self.cluster, 'upgrade')

        self.assertEqual(options['api_server_port'], 8443)

    def test_a_snapshot_without_any_network_record_is_refused_upfront(self):
        """快照里彻底读不到网段时当场拒绝，而不是提交一个必然失败的任务。

        预检的「网段合法性」读到空值就判失败，原本用户要等到任务跑起来才知道；
        而这两个框他根本填不了。拒绝时必须说清是「集群没记下来」，不是「你没填」。
        """
        self.replace_revision(dict(SNAPSHOT, hosts='', pod_cidr='', service_cidr=''))

        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'add_worker', machines=[self.make_machine()])

        message = str(caught.exception)
        self.assertIn('容器网络 CIDR', message)
        self.assertIn('Service 网段', message)
        self.assertIn('配置版本', message)

    def test_tasks_that_do_not_run_preflight_are_not_blocked(self):
        """备份不跑预检、也不重新生成配置，读不到网段与它无关，不该被拦下。"""
        self.replace_revision(dict(SNAPSHOT, hosts='', pod_cidr='', service_cidr=''))

        deployment = cluster_ops.submit(self.cluster, 'backup')

        self.assertEqual(deployment.task_type, 'backup')

    def test_the_network_fields_are_declared_as_inherited(self):
        """两个网段既要在继承清单里，也要真的能透传到任务上（与 lb 字段同一套机制）。"""
        for field in ('pod_cidr', 'service_cidr', 'api_server_port'):
            with self.subTest(field=field):
                self.assertIn(field, cluster_ops.INHERITED_FIELDS)
                self.assertIn(field, cluster_ops.TASK_FIELD_NAMES)
                self.assertNotIn(field, cluster_ops.EXPLICIT_FIELD_NAMES)


class AssertDeployedTest(ClusterOpsTestBase):
    """只有平台部署的集群才允许运维。"""

    def test_imported_cluster_is_refused(self):
        """导入型集群没有 inventory 与免密通道，一律拒绝。"""
        imported = K8sCluster.objects.create(
            organization=self.org, name='导入集群', slug='', origin='imported',
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.assert_deployed(imported)
        self.assertIn('导入', str(caught.exception))

    def test_cluster_without_revision_is_refused(self):
        """没有配置版本记录的集群，平台不知道它的真实拓扑。"""
        bare = K8sCluster.objects.create(
            organization=self.org, name='无版本集群', slug='bare-cluster', origin='deployed',
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.assert_deployed(bare)
        self.assertIn('配置版本', str(caught.exception))

    def test_deployed_cluster_passes(self):
        """已部署且有配置版本的集群通过检查。"""
        cluster_ops.assert_deployed(self.cluster)


class SubmitTest(ClusterOpsTestBase):
    """提交运维任务时的各项拒绝条件。"""

    def test_backup_task_gets_cluster_slug_not_task_slug(self):
        """任务的 slug 唯一，集群目录名走 cluster_slug：同一集群可反复运维。"""
        first = cluster_ops.submit(self.cluster, 'backup')
        self.assertEqual(first.cluster_slug, 'ops-cluster')
        first.status = 'succeeded'
        first.save(update_fields=['status', 'updated_at'])
        second = cluster_ops.submit(self.cluster, 'backup')
        self.assertNotEqual(first.slug, second.slug)
        self.assertEqual(second.cluster_slug, 'ops-cluster')

    def test_backup_task_snapshots_existing_nodes(self):
        """备份/升级/恢复不新增节点，但要把集群现有清单抄进任务。

        任务执行时用这份快照还原 inventory，而不是现查集群——集群的节点表在任务排队
        期间可能被别的途径改动，用任务自己记下的清单才能保证跑的是提交时那一份配置。
        """
        task = cluster_ops.submit(self.cluster, 'backup')
        self.assertEqual(task.nodes.count(), 4)
        roles = {node.node_name: node.roles for node in task.nodes.all()}
        self.assertEqual(roles['master-1'], ['etcd', 'master'])
        self.assertEqual(roles['worker-1'], ['node'])

    def test_second_task_is_refused_while_one_is_active(self):
        """集群同一时刻只允许一个非终态变更任务。"""
        cluster_ops.submit(self.cluster, 'backup')
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'upgrade')
        self.assertIn('未完成的任务', str(caught.exception))

    def test_new_task_allowed_after_previous_finishes(self):
        """前一个任务终态之后可以提交下一个。"""
        first = cluster_ops.submit(self.cluster, 'backup')
        first.status = 'failed'
        first.save(update_fields=['status', 'updated_at'])
        task = cluster_ops.submit(self.cluster, 'backup')
        self.assertEqual(task.status, 'queued')

    def test_task_records_expected_revision(self):
        """任务记下提交时的配置版本，执行时用它与当前版本比对。"""
        task = cluster_ops.submit(self.cluster, 'backup')
        self.assertEqual(task.expected_config_revision, 1)

    def test_task_snapshot_keeps_submitted_options(self):
        """提交内容进任务快照，恢复所需的 backup_id 就在里面。"""
        task = cluster_ops.submit(
            self.cluster, 'restore',
            payload={'backup_id': 'bk-abc', 'kubernetes_version': '1.29.4'},
        )
        self.assertEqual(task.request_snapshot['submitted']['backup_id'], 'bk-abc')
        self.assertEqual(task.request_snapshot['cluster_slug'], 'ops-cluster')

    def test_backup_id_does_not_leak_into_model_kwargs(self):
        """backup_id 不是模型字段，不能被展开成构造参数（否则直接 TypeError）。"""
        task = cluster_ops.submit(
            self.cluster, 'restore',
            payload={'backup_id': 'bk-abc', 'kubernetes_version': '1.29.4'},
        )
        self.assertFalse(hasattr(task, 'backup_id'))

    def test_options_may_share_names_with_model_fields(self):
        """options 里出现与模型同名的字段时不能重复传参。

        ``inherited_options`` 本来就会带上 ``display_name``，而 ``build_task`` 自己也要给
        这个字段赋值——两边都往 ``create()`` 里塞就会抛 TypeError，运维任务一条都建不出来。
        """
        task = cluster_ops.submit(self.cluster, 'backup')
        self.assertEqual(task.display_name, f'{self.cluster.name} · 集群备份')
        self.assertEqual(task.execution_mode, cluster_ops._execution_mode()[0])

    def test_task_keeps_whole_options_in_snapshot(self):
        """透传给模型的字段被过滤后，完整 options 仍要留在快照里，便于事后追溯。"""
        task = cluster_ops.submit(self.cluster, 'backup')
        submitted = task.request_snapshot['submitted']
        self.assertEqual(submitted['display_name'], self.cluster.name)
        self.assertEqual(submitted['kubeasz_version'], '3.6.7')

    def test_deploy_only_task_types_are_refused(self):
        """新建部署类任务不能走运维入口。"""
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'deploy_ha')
        self.assertIn('不支持', str(caught.exception))


class AddNodeTest(ClusterOpsTestBase):
    """扩容：添加 Master 与添加 Node。"""

    def test_add_worker_creates_single_node_snapshot(self):
        """扩容任务的节点快照只有新增的那一台机器。"""
        machine = self.make_machine()
        task = cluster_ops.submit(self.cluster, 'add_worker', machines=[machine])
        self.assertEqual(task.nodes.count(), 1)
        node = task.nodes.first()
        self.assertEqual(str(node.ip_address), '192.168.30.50')
        self.assertEqual(node.roles, ['node'])

    def test_add_master_records_master_role(self):
        """添加 Master 的角色只有 master：既不是 etcd，也不是 node。

        ``ezctl add-master`` 跑的 23.addmaster.yml 里只有 kube-lb / kube-master / kube-node 与
        CNI，**没有 etcd** —— 加 etcd 成员是独立的 ``ezctl add-etcd``（21.addetcd.yml），
        kubeasz 的 op-master.md 里新增的那台 master 同样不进 etcd 组。角色写多了会让预检去查那台
        机器上根本不存在的 etcd 端口，让清理计划把它算成 etcd 节点，还会让 inventory 的
        ``[etcd]`` 多出一个空成员（新 Master 的 ``--etcd-servers`` 就是这么拼出来的）。
        """
        machine = self.make_machine()
        task = cluster_ops.submit(self.cluster, 'add_master', machines=[machine])

        self.assertEqual(task.nodes.first().roles, ['master'])
        self.assertNotIn('etcd', task.nodes.first().roles)
        self.assertNotIn('node', task.nodes.first().roles)

    def test_add_master_needs_three_existing_masters(self):
        """不是多节点集群（Master 少于 3 台）时，加 Master 没有意义。"""
        snapshot = dict(SNAPSHOT)
        snapshot['nodes'] = [SNAPSHOT['nodes'][0]]
        K8sClusterConfigRevision.objects.filter(cluster=self.cluster).update(snapshot=snapshot)
        machine = self.make_machine()
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'add_master', machines=[machine])
        self.assertIn('多节点', str(caught.exception))
        self.assertIn('3 台', str(caught.exception))

    def test_add_worker_requires_a_machine(self):
        """不指定机器时拒绝，而不是扩展出一个空任务。"""
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'add_worker', machines=[])
        self.assertIn('机器', str(caught.exception))

    def test_machine_already_in_cluster_is_refused(self):
        """已经在集群里的机器不能再添加一次。"""
        machine = self.make_machine(ip_address='192.168.30.10', name='existing')
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'add_worker', machines=[machine])
        self.assertIn('已经在集群中', str(caught.exception))

    def test_a_free_looking_machine_with_a_taken_address_is_refused(self):
        """机器表里显示「未分配」不代表它没在集群里：判据是地址。

        94 上就是这样：一台后来新建的机器填了集群里某个节点正在用的 IP，它的
        ``cluster_name`` 是空的（机器列表显示未分配，于是向导把它列成候选），但集群按
        地址认节点，地址相同就是同一台。拒绝时要把判据说出来，否则用户只会看到一句
        「已经在集群中」而对着一个显示空闲的机器列表发懵。
        """
        machine = self.make_machine(ip_address='192.168.30.10', name='looks-free')
        self.assertEqual(machine.cluster_name, '')

        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'add_worker', machines=[machine])

        self.assertIn('192.168.30.10', str(caught.exception))
        self.assertIn('IP', str(caught.exception))

    def test_upgrade_refuses_extra_machines(self):
        """升级不需要指定节点，带了就报错而不是静默忽略。"""
        machine = self.make_machine()
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'upgrade', machines=[machine])
        self.assertIn('不需要指定节点', str(caught.exception))


class NodePayloadsTest(ClusterOpsTestBase):
    """配置版本里的节点清单翻译。"""

    def test_payloads_keep_snapshot_identity(self):
        """节点信息从快照里取，保证节点名与集群里的完全一致（扩容不能改名）。"""
        payloads = cluster_ops.node_payloads(self.cluster)
        self.assertEqual(len(payloads), 4)
        names = {item['node_name'] for item in payloads}
        self.assertIn('master-1', names)
        self.assertEqual(payloads[0]['roles'], ['etcd', 'master'])

    def test_deleted_machine_still_yields_payload(self):
        """机器从机器表删掉后，节点快照的 IP 与端口仍然可用，任务照跑。"""
        payloads = cluster_ops.node_payloads(self.cluster)
        self.assertIsNone(payloads[0]['machine'])
        self.assertEqual(payloads[0]['ip_address'], '192.168.30.10')

    def test_existing_machine_is_linked(self):
        """机器还在时把实例挂上，供后续同步归属。"""
        machine = self.make_machine(ip_address='192.168.30.10', name='existing')
        payloads = cluster_ops.node_payloads(self.cluster)
        linked = next(item for item in payloads if item['ip_address'] == '192.168.30.10')
        self.assertEqual(linked['machine'], machine)

    def test_empty_node_records_are_refused(self):
        """配置版本里没有节点记录时拒绝，而不是按空清单硬跑。"""
        K8sClusterConfigRevision.objects.filter(cluster=self.cluster).update(
            snapshot=dict(SNAPSHOT, nodes=[]),
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.node_payloads(self.cluster)
        self.assertIn('没有节点记录', str(caught.exception))


class CapabilitiesTest(ClusterOpsTestBase):
    """能力探测：前端据此决定按钮可用性。"""

    def test_deployed_cluster_exposes_all_actions(self):
        """已部署且有版本记录的集群，运维动作全部可用。"""
        result = cluster_ops.cluster_capabilities(self.cluster)
        self.assertTrue(result['deployed'])
        self.assertEqual(result['reason'], '')
        for action in ('add_master', 'add_worker', 'upgrade', 'backup', 'restore', 'destroy'):
            self.assertTrue(result['actions'][action], f'{action} 应当可用')

    def test_imported_cluster_disables_everything(self):
        """导入型集群全部禁用，并给出原因。"""
        imported = K8sCluster.objects.create(
            organization=self.org, name='导入集群', slug='', origin='imported',
        )
        result = cluster_ops.cluster_capabilities(imported)
        self.assertFalse(result['deployed'])
        self.assertIn('导入', result['reason'])
        self.assertFalse(any(result['actions'].values()))

    def test_node_addresses_lists_what_the_cluster_already_has(self):
        """「添加节点」的候选机器要用它排除，因此必须与提交时的判据同源。"""
        result = cluster_ops.cluster_capabilities(self.cluster)

        self.assertEqual(
            sorted(result['node_addresses']),
            ['192.168.30.10', '192.168.30.11', '192.168.30.12', '192.168.30.13'],
        )

    def test_node_addresses_is_empty_when_the_cluster_cannot_be_operated(self):
        """不可运维的集群列不出地址，也不该因此报错。"""
        imported = K8sCluster.objects.create(
            organization=self.org, name='导入集群（无快照）', slug='', origin='imported',
        )

        self.assertEqual(cluster_ops.cluster_capabilities(imported)['node_addresses'], [])
    def test_weak_key_disables_backup_upgrade_and_restore(self):
        """密钥强度不足时备份、升级、恢复一起禁用，而不是等点了再失败。"""
        with override_settings(SECRET_KEY='platform-template-local-dev-secret-change-me'):
            result = cluster_ops.cluster_capabilities(self.cluster)
        self.assertFalse(result['backup_ready'])
        self.assertFalse(result['actions']['backup'])
        self.assertFalse(result['actions']['upgrade'])
        self.assertFalse(result['actions']['restore'])
        self.assertTrue(result['actions']['add_worker'], '扩容不依赖备份，仍应可用')
        # 清理恰恰是在集群已经病到备份跑不动时才需要，不能跟着一起禁用。
        self.assertTrue(result['actions']['destroy'], '清理不依赖备份能力，仍应可用')

    def test_destroyed_cluster_exposes_no_action(self):
        """已清理的集群走「未部署」这条路：所有入口一次性关掉，并给出中文原因。"""
        self.cluster.status = 'destroyed'
        self.cluster.slug = ''
        self.cluster.save(update_fields=['status', 'slug'])

        result = cluster_ops.cluster_capabilities(self.cluster)

        self.assertFalse(result['deployed'])
        self.assertIn('清理', result['reason'])
        self.assertFalse(any(result['actions'].values()))

    def test_a_live_cluster_is_not_reported_as_destroyed(self):
        """正常集群的 ``destroyed`` 为假 —— 前端据此决定给「清理集群」发哪一句提示。"""
        self.assertFalse(cluster_ops.cluster_capabilities(self.cluster)['destroyed'])
        self.assertNotIn('清理过', cluster_ops.cluster_capabilities(self.cluster)['reason'])

    def test_destroyed_flag_survives_the_tombstone(self):
        """已清理的集群要单独把「已经清理过」这一个事实暴露出来。

        它和别的原因共用同一句 reason（「请直接重新部署」），而「清理集群」这个按钮自己需要的
        是一句回答它的问题的提示 —— 否则一台清过的机器点清理，会被告诉「请直接重新部署」。
        """
        self.cluster.status = 'destroyed'
        self.cluster.slug = ''
        self.cluster.save(update_fields=['status', 'slug'])

        result = cluster_ops.cluster_capabilities(self.cluster)

        self.assertTrue(result['destroyed'])
        self.assertFalse(result['actions']['destroy'])
        # 这一句说的是「未部署」的公共原因，对清理按钮自己不管用；前端靠上面的布尔值另给提示。
        self.assertIn('重新部署', result['reason'])

    def test_imported_cluster_is_not_reported_as_destroyed(self):
        """导入型集群是「没部署过」，不是「被清过」—— 两者的提示不是一回事。"""
        imported = K8sCluster.objects.create(
            organization=self.org, name='另一个导入集群', slug='', origin='imported',
        )
        self.assertFalse(cluster_ops.cluster_capabilities(imported)['destroyed'])

    def test_destroyed_cluster_refuses_every_operation(self):
        """配置版本还在、两道旧闸门都会放行，因此必须单独拦一道，否则能对墓碑反复提交销毁。"""
        self.cluster.status = 'destroyed'
        self.cluster.save(update_fields=['status'])

        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'destroy')
        self.assertIn('已经清理过', str(caught.exception))

    def test_destroy_task_carries_the_cluster_nodes(self):
        """清理的节点清单来自集群配置版本：机器归属可能已经不在这台集群上了。"""
        task = cluster_ops.submit(self.cluster, 'destroy')

        self.assertEqual(task.task_type, 'destroy')
        self.assertEqual(task.status, 'queued')
        snapshots = list(K8sDeploymentNode.objects.filter(deployment=task).values_list('ip_address', flat=True))
        self.assertEqual(sorted(snapshots), sorted(item['ip_address'] for item in SNAPSHOT['nodes']))

    def test_destroy_refuses_an_explicit_machine_list(self):
        """清理不接受「要新增哪些机器」：它清理的是集群自带的节点。"""
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'destroy', machines=[self.make_machine()])
        self.assertIn('不需要指定节点', str(caught.exception))

    def test_active_task_is_reported_and_disables_actions(self):
        """有任务在跑时把任务信息一并返回，并禁用其它动作。"""
        task = cluster_ops.submit(self.cluster, 'backup')
        result = cluster_ops.cluster_capabilities(self.cluster)
        self.assertEqual(result['active_task']['id'], task.pk)
        self.assertEqual(result['active_task']['status'], 'queued')
        self.assertFalse(result['actions']['upgrade'])

    def test_non_ha_cluster_cannot_add_master(self):
        """Master 少于 3 台时添加 Master 按钮不可用。"""
        snapshot = dict(SNAPSHOT)
        snapshot['nodes'] = SNAPSHOT['nodes'][:2]
        K8sClusterConfigRevision.objects.filter(cluster=self.cluster).update(snapshot=snapshot)
        result = cluster_ops.cluster_capabilities(self.cluster)
        self.assertFalse(result['actions']['add_master'])
        self.assertTrue(result['actions']['add_worker'])


class UpgradeTargetTest(ClusterOpsTestBase):
    """升级目标版本的候选、拒绝条件与跨次版本时的 kubeasz 切换。

    集群在 1.29.4：这一组用例大多自己往配置中心补一行 1.30 来构造「升一个次版本」的
    合法输入，需要「配置中心里什么都没有」时用 ``deactivate_all_versions`` 清空 ——
    库里本来就有一份迁移种下的 1.33–1.36，不清掉的话「没登记就拒绝」这类断言会被它挡住。
    """

    def deactivate_all_versions(self):
        """停用配置中心里的全部版本映射。

        参数：无。
        返回：无显式返回值。
        副作用：更新版本映射表的启用状态。
        """
        from k8s_config.models import K8sVersionMapping

        K8sVersionMapping.objects.update(is_active=False)

    def test_candidate_is_the_next_minor_only(self):
        """候选只有「当前次版本 + 1」那一项，隔一个次版本的版本不出现在候选里。"""
        self.register_version('1.30', '3.6.8')
        self.register_version('1.31', '3.6.9')

        result = cluster_ops.upgrade_target_options(self.cluster)
        self.assertEqual(result['current'], '1.29.4')
        self.assertEqual([item['kubernetes_version'] for item in result['options']], ['1.30'])
        self.assertEqual(result['options'][0]['kubeasz_version'], '3.6.8')
        self.assertEqual(result['reason'], '')

    def test_no_higher_version_points_at_the_config_center(self):
        """配置中心里没有更高版本时说明原因，并指向登记入口。"""
        self.deactivate_all_versions()

        result = cluster_ops.upgrade_target_options(self.cluster)
        self.assertEqual(result['options'], [])
        self.assertIn('没有比 1.29.4 更高', result['reason'])
        self.assertIn('配置中心', result['reason'])

    def test_a_higher_but_distant_version_explains_the_step(self):
        """有更高的版本但相隔不止一个次版本时，要说清必须先升中间那一级。"""
        self.deactivate_all_versions()
        self.register_version('1.32', '3.6.10')

        result = cluster_ops.upgrade_target_options(self.cluster)
        self.assertEqual(result['options'], [])
        self.assertIn('1.32', result['reason'])
        self.assertIn('一个次版本', result['reason'])

    def test_stopped_versions_are_not_offered(self):
        """停用的版本不进候选：停用正是「这个版本不要再用了」的意思。"""
        self.deactivate_all_versions()
        mapping = self.register_version('1.30', '3.6.8')
        mapping.is_active = False
        mapping.save(update_fields=['is_active', 'updated_at'])

        self.assertEqual(cluster_ops.upgrade_target_options(self.cluster)['options'], [])

    def test_unparsable_cluster_version_is_explained(self):
        """集群版本号读不出来时不猜候选，直接说明无法判断。"""
        self.cluster.kube_version = ''
        self.cluster.save(update_fields=['kube_version', 'updated_at'])

        result = cluster_ops.upgrade_target_options(self.cluster)
        self.assertEqual(result['options'], [])
        self.assertIn('无法解析', result['reason'])

    def test_capabilities_carry_the_candidates(self):
        """候选清单由运维能力接口给出：它要的是升级权限，不是部署页的查看权限。"""
        self.register_version('1.30', '3.6.8')
        capabilities = cluster_ops.cluster_capabilities(self.cluster)
        self.assertEqual(
            [item['kubernetes_version'] for item in capabilities['upgrade_targets']['options']],
            ['1.30'],
        )

    def test_same_version_target_is_rejected(self):
        """目标与当前相同时拒绝：这正是「任务报成功、版本没变」那条空转路径。"""
        options = cluster_ops.inherited_options(
            self.cluster, 'upgrade', {'kubernetes_version': '1.29.4'},
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops._validate_upgrade_target(self.cluster, options)
        self.assertIn('相同', str(caught.exception))

    def test_downgrade_is_rejected(self):
        """降级没有入口，服务端也不接受。"""
        options = cluster_ops.inherited_options(
            self.cluster, 'upgrade', {'kubernetes_version': '1.28'},
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops._validate_upgrade_target(self.cluster, options)
        self.assertIn('降级', str(caught.exception))

    def test_minor_jump_is_rejected(self):
        """一次跨两个次版本拒绝：Kubernetes 不允许跳 minor。"""
        options = cluster_ops.inherited_options(
            self.cluster, 'upgrade', {'kubernetes_version': '1.31'},
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops._validate_upgrade_target(self.cluster, options)
        self.assertIn('一个次版本', str(caught.exception))

    def test_unregistered_target_is_rejected(self):
        """目标没在配置中心登记过时拒绝：制品是按映射同步的，选它只会在下载阶段失败。"""
        self.deactivate_all_versions()
        options = cluster_ops.inherited_options(
            self.cluster, 'upgrade', {'kubernetes_version': '1.30'},
        )
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops._validate_upgrade_target(self.cluster, options)
        self.assertIn('没有登记', str(caught.exception))

    def test_missing_target_version_is_rejected_at_submit(self):
        """只发确认名的老请求形状必须被拒，而不是建一条注定空转的任务。"""
        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'upgrade', payload={'confirm_name': self.cluster.name})
        self.assertIn('相同', str(caught.exception))
        self.assertFalse(K8sDeployment.objects.filter(cluster=self.cluster, task_type='upgrade').exists())

    def test_cross_minor_upgrade_switches_the_kubeasz_artifact(self):
        """跨次版本升级要连制品一起换：任务快照里的 kubeasz 取配置中心给目标版本的版本号。

        这一条是整套改造的关键。``/etc/kubeasz/bin`` 里的 k8s 二进制只由本次使用的
        kubeasz 制品决定，而升级任务的 kubeasz 版本默认继承「集群当前生效版本」——
        不换的话 ``93.upgrade.yml`` 开头的版本守卫会直接 UPDATE ABORT，升级永远升不动。
        """
        self.register_version('1.30', '3.6.8')

        task = cluster_ops.submit(self.cluster, 'upgrade', payload={'kubernetes_version': '1.30'})

        self.assertEqual(task.kubernetes_version, '1.30')
        self.assertEqual(task.kubeasz_version, '3.6.8')
        self.assertEqual(task.request_snapshot['submitted']['kubeasz_version'], '3.6.8')

    def test_same_minor_patch_upgrade_keeps_the_cluster_artifact(self):
        """同一次版本内的补丁升级不换制品：kubeasz 仍是集群当前那一套。"""
        self.register_version('1.29', '3.6.7')

        task = cluster_ops.submit(self.cluster, 'upgrade', payload={'kubernetes_version': '1.29.6'})

        self.assertEqual(task.kubernetes_version, '1.29.6')
        self.assertEqual(task.kubeasz_version, '3.6.7')

    def test_proxy_mode_is_read_from_the_snapshot_field(self):
        """优先读快照里的结构化字段。"""
        self.set_cluster_proxy_mode('nftables')
        self.assertEqual(register.cluster_proxy_mode(self.cluster), 'nftables')

    def test_proxy_mode_falls_back_to_the_hosts_text(self):
        """快照里没有该字段时退回解析 hosts 里的 PROXY_MODE（老配置版本都是这种形状）。"""
        self.set_cluster_proxy_mode('ipvs', structured=False)
        self.assertEqual(register.cluster_proxy_mode(self.cluster), 'ipvs')

    def test_proxy_mode_is_empty_when_nothing_records_it(self):
        """两处都读不到时返回空串，绝不编一个默认值出来。

        编出来的值会一路走到预检里：模式决定预检探什么（ipvs 探内核模块、nftables 探内核与
        nft），拿默认值去探会在一台根本没开 ipvs 的机器上报「缺少 ip_vs 模块」。
        """
        self.assertEqual(register.cluster_proxy_mode(self.cluster), '')

    def test_candidate_marks_the_mode_change_when_the_target_drops_it(self):
        """集群在 ipvs、目标 kubeasz 3.7.0 不认 ipvs 时，候选上要标出「必须改模式」。"""
        self.set_cluster_proxy_mode('ipvs')
        self.register_version('1.30', '3.7.0')

        result = cluster_ops.upgrade_target_options(self.cluster)
        self.assertEqual(result['current_proxy_mode'], 'ipvs')
        option = result['options'][0]
        self.assertTrue(option['proxy_mode_change_required'])
        self.assertEqual(option['proxy_modes'], ['iptables', 'nftables'])
        self.assertEqual(option['proxy_mode_default'], 'iptables')

    def test_candidate_keeps_the_mode_when_the_target_still_supports_it(self):
        """目标 kubeasz 仍然支持集群当前模式时不要求改。"""
        self.set_cluster_proxy_mode('ipvs')
        self.register_version('1.30', '3.6.8')

        option = cluster_ops.upgrade_target_options(self.cluster)['options'][0]
        self.assertFalse(option['proxy_mode_change_required'])
        self.assertEqual(option['proxy_modes'], ['ipvs', 'iptables'])

    def test_submit_refuses_a_target_that_drops_the_cluster_mode(self):
        """目标 kubeasz 不支持集群当前模式、又没给新模式时，提交这一刻就拒绝。"""
        self.set_cluster_proxy_mode('ipvs')
        self.register_version('1.30', '3.7.0')

        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'upgrade', payload={'kubernetes_version': '1.30'})

        message = str(caught.exception)
        self.assertIn('不支持', message)
        self.assertIn('iptables', message)
        self.assertFalse(K8sDeployment.objects.filter(cluster=self.cluster, task_type='upgrade').exists())

    def test_submit_freezes_the_new_mode_into_the_task(self):
        """显式给出新模式时，任务快照里记的是新模式 —— 它会随升级剧本写进集群的 hosts。"""
        self.set_cluster_proxy_mode('ipvs')
        self.register_version('1.30', '3.7.0')

        task = cluster_ops.submit(self.cluster, 'upgrade', payload={
            'kubernetes_version': '1.30',
            'kube_proxy_mode': 'nftables',
        })

        self.assertEqual(task.kube_proxy_mode, 'nftables')
        self.assertEqual(task.request_snapshot['submitted']['kube_proxy_mode'], 'nftables')

    def test_submit_refuses_an_unsupported_new_mode(self):
        """新模式本身不被目标 kubeasz 支持时同样拒绝（3.6.x 不认 nftables）。"""
        self.set_cluster_proxy_mode('ipvs')
        self.register_version('1.30', '3.6.8')

        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'upgrade', payload={
                'kubernetes_version': '1.30',
                'kube_proxy_mode': 'nftables',
            })

        self.assertIn('nftables', str(caught.exception))

    def test_submit_refuses_a_new_mode_when_the_cluster_mode_is_unreadable(self):
        """读不到集群当前模式时不允许改模式：改不动 hosts 里那一行，改了也不会生效。"""
        self.register_version('1.30', '3.7.0')

        with self.assertRaises(cluster_ops.ClusterOpError) as caught:
            cluster_ops.submit(self.cluster, 'upgrade', payload={
                'kubernetes_version': '1.30',
                'kube_proxy_mode': 'nftables',
            })

        self.assertIn('读不到', str(caught.exception))

    def test_same_minor_upgrade_inherits_the_cluster_mode(self):
        """补丁升级不动模式：没显式给新模式时任务沿用集群当前那一个。

        集群当前是 iptables 而不是 ipvs，正是为了钉住「继承的是集群的真实模式」——
        这一处曾经写死回落到 ipvs，于是一台 iptables 集群的升级任务会带着 ipvs 去跑，
        既会被内核模块预检做假的模块探针，跨次版本时还会被目标模板直接拦下。
        """
        self.set_cluster_proxy_mode('iptables')
        self.register_version('1.29', '3.6.7')

        task = cluster_ops.submit(self.cluster, 'upgrade', payload={'kubernetes_version': '1.29.6'})

        self.assertEqual(task.kube_proxy_mode, 'iptables')


class ClusterOpsApiTest(TestCase):
    """运维接口的权限与二次确认。"""

    def setUp(self):
        """准备组织、一个已部署集群、普通用户与默认拒绝的策略。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入测试数据。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-opsapi-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
            K8S_DEPLOY_AUTOSTART_WORKER=False,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        organization = Organization.objects.first()
        self.user = User.objects.create_user(username='ops-user', password='StrongPassword123!')
        UserProfile.objects.create(user=self.user, organization=organization, role='member')
        self.policy = PermissionPolicy.objects.create(
            name='运维权限', subject_type='user', user=self.user, status='available',
        )
        # 集群取 1.35：升级请求要带一个合法目标版本（1.36），而这对版本正是配置中心里
        # 迁移种下的那一组，是平台上唯一能真跑起来的升级 --- 同一次版本内换不出另一套
        # kubeasz 制品，也就升不动。
        self.cluster = K8sCluster.objects.create(
            organization=organization,
            name='接口测试集群',
            slug='api-cluster',
            origin='deployed',
            kube_version='1.35',
            status='configured',
        )
        K8sClusterConfigRevision.objects.create(
            organization=organization,
            cluster=self.cluster,
            revision=1,
            config_hash='d' * 64,
            snapshot=dict(SNAPSHOT, kubernetes_version='1.35', kubeasz_version='3.6.10'),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def grant(self, code, effect='allow'):
        """写入一条权限规则并清缓存。

        参数：`code` 表示权限码；`effect` 表示 allow 或 deny。
        返回：无显式返回值。
        副作用：写入权限规则并清理权限缓存。
        """
        PermissionRule.objects.update_or_create(
            policy=self.policy, permission_code=code, defaults={'effect': effect},
        )
        clear_permission_cache()

    def grant_view(self):
        """授予集群页面查看权限，作为按钮权限用例的前置条件。"""
        self.grant('menu.k8s')
        self.grant('page.k8s.clusters.view')

    def revoke_all(self):
        """清空策略规则，恢复默认拒绝。"""
        PermissionRule.objects.filter(policy=self.policy).delete()
        clear_permission_cache()

    def url(self, suffix):
        """拼出运维接口地址。

        参数：`suffix` 表示动作路径后缀。
        返回：返回完整 API 路径，末尾带斜杠（DRF 路由默认追加，缺了会先 301 再丢方法）。
        """
        return f'/api/v1/k8s/clusters/{self.cluster.pk}/{suffix}/'

    def make_machine(self, ip_address='192.168.30.60'):
        """创建一台机器。

        参数：`ip_address` 表示机器 IP。
        返回：返回 Machine 实例。
        副作用：向数据库写入机器记录。
        """
        return Machine.objects.create(
            organization=self.cluster.organization,
            name='api-machine',
            ip_address=ip_address,
            ssh_port=22,
            ssh_user='root',
        )

    def make_backup_record(self, backup_id='bk-api-1', **extra):
        """创建一条可用于恢复的备份记录。

        参数：`backup_id` 表示备份标识；`extra` 表示要覆盖的字段。
        返回：返回 K8sClusterBackup 实例。
        副作用：向数据库写入备份记录。
        """
        from k8s_deployment.models import K8sClusterBackup

        values = {
            'organization': self.cluster.organization,
            'cluster': self.cluster,
            'backup_id': backup_id,
            'slug': self.cluster.slug,
            'kubernetes_version': '1.35',
            'kubeasz_version': '3.6.10',
            'status': 'succeeded',
            'object_prefix': f'kubeasz/backups/1/api-cluster/{backup_id}',
            'archive_sha256': 'e' * 64,
            'archive_size': 10,
        }
        values.update(extra)
        return K8sClusterBackup.objects.create(**values)

    def test_operations_endpoint_requires_view_permission(self):
        """能力探测是只读接口，只要页面查看权限。"""
        self.revoke_all()
        self.assertEqual(self.client.get(self.url('operations')).status_code, 403)
        self.grant_view()
        response = self.client.get(self.url('operations'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('actions', response.data)

    def test_backups_list_requires_view_but_not_backup_action(self):
        """同一个 url_path 上的 GET 是查看，不应要求备份权限。"""
        self.revoke_all()
        self.grant_view()
        self.assertEqual(self.client.get(self.url('backups')).status_code, 200)

    def test_backups_post_requires_backup_action(self):
        """同一个 url_path 上的 POST 是执行备份，必须单独授权。"""
        self.revoke_all()
        self.grant_view()
        self.assertEqual(self.client.post(self.url('backups'), {}).status_code, 403)
        self.grant('page.k8s.clusters.backup')
        response = self.client.post(self.url('backups'), {})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(K8sDeployment.objects.filter(cluster=self.cluster, task_type='backup').exists())

    def test_add_worker_requires_its_own_action(self):
        """添加 Node 需要 add_worker 权限码。"""
        machine = self.make_machine()
        self.revoke_all()
        self.grant_view()
        payload = {'machine_id': machine.pk}
        self.assertEqual(self.client.post(self.url('nodes/add-worker'), payload).status_code, 403)
        self.grant('page.k8s.clusters.add_worker')
        response = self.client.post(self.url('nodes/add-worker'), payload)
        self.assertEqual(response.status_code, 201, response.data)

    def test_add_master_requires_its_own_action(self):
        """添加 Master 需要 add_master 权限码，与添加 Node 分开。"""
        machine = self.make_machine()
        self.revoke_all()
        self.grant_view()
        self.grant('page.k8s.clusters.add_worker')
        payload = {'machine_id': machine.pk}
        self.assertEqual(self.client.post(self.url('nodes/add-master'), payload).status_code, 403)
        self.grant('page.k8s.clusters.add_master')
        self.assertEqual(self.client.post(self.url('nodes/add-master'), payload).status_code, 201)

    def test_upgrade_requires_its_own_action(self):
        """版本升级需要 upgrade 权限码。"""
        self.revoke_all()
        self.grant_view()
        confirm = {'confirm_name': self.cluster.name, 'kubernetes_version': '1.36'}
        self.assertEqual(self.client.post(self.url('upgrade'), confirm).status_code, 403)
        self.grant('page.k8s.clusters.upgrade')
        response = self.client.post(self.url('upgrade'), confirm)
        self.assertEqual(response.status_code, 201, response.data)
        task = K8sDeployment.objects.get(task_type='upgrade')
        self.assertEqual(task.kubernetes_version, '1.36')
        # 跨次版本要连制品一起换：1.36 对应配置中心里的 kubeasz 3.7.0，而不是集群当前的 3.6.10。
        self.assertEqual(task.kubeasz_version, '3.7.0')

    def test_upgrade_requires_exact_cluster_name(self):
        """升级的二次确认同样在服务端做：不填或填错都拒绝（规划 12）。"""
        self.grant_view()
        self.grant('page.k8s.clusters.upgrade')
        target = {'kubernetes_version': '1.36'}
        self.assertEqual(self.client.post(self.url('upgrade'), target).status_code, 400)
        wrong = self.client.post(
            self.url('upgrade'), {'confirm_name': '随便写的名字', **target},
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertIn('confirm_name', wrong.data['detail'])
        self.assertFalse(K8sDeployment.objects.filter(task_type='upgrade').exists())

    def test_upgrade_without_a_target_version_is_refused(self):
        """不带目标版本的升级请求要被拒：它会回落成集群当前版本，等于原地空转。

        这是界面改造之前的老请求形状（只发 confirm_name）。服务端必须拦住它，而不是建一条
        跑完报「成功」却什么都没做的任务 —— 那种「成功」比失败更难发现。
        """
        self.grant_view()
        self.grant('page.k8s.clusters.upgrade')
        response = self.client.post(self.url('upgrade'), {'confirm_name': self.cluster.name})
        self.assertEqual(response.status_code, 400)
        self.assertIn('相同', response.data['detail'])
        self.assertFalse(K8sDeployment.objects.filter(task_type='upgrade').exists())

    def test_destroy_requires_its_own_action(self):
        """清理集群需要 destroy_cluster 权限码：它是高危动作，不该跟着别的权限一起放开。"""
        confirm = {'confirm_name': self.cluster.name}
        self.revoke_all()
        self.grant_view()
        self.assertEqual(self.client.post(self.url('destroy'), confirm).status_code, 403)
        self.grant('page.k8s.clusters.destroy_cluster')
        self.assertEqual(self.client.post(self.url('destroy'), confirm).status_code, 201)

    def test_destroy_requires_exact_cluster_name(self):
        """清理的二次确认在服务端做：输错集群名一律拒绝，绕过界面直接调接口同样拦得住。"""
        self.grant_view()
        self.grant('page.k8s.clusters.destroy_cluster')
        self.assertEqual(self.client.post(self.url('destroy'), {}).status_code, 400)
        wrong = self.client.post(self.url('destroy'), {'confirm_name': '随便写的名字'})
        self.assertEqual(wrong.status_code, 400)
        self.assertIn('confirm_name', wrong.data['detail'])
        self.assertFalse(K8sDeployment.objects.filter(task_type='destroy').exists())

        correct = self.client.post(self.url('destroy'), {'confirm_name': self.cluster.name})
        self.assertEqual(correct.status_code, 201, correct.data)
        self.assertEqual(K8sDeployment.objects.get(task_type='destroy').status, 'queued')

    def test_destroyed_cluster_refuses_status_changes(self):
        """已清理的集群不能改状态：改回「已配置」会让它带着配置版本重新出现在运维入口里。"""
        self.cluster.status = 'destroyed'
        self.cluster.save(update_fields=['status'])
        self.grant_view()
        self.grant('page.k8s.clusters.disable')

        for suffix in ('enable', 'disable'):
            response = self.client.post(self.url(suffix), {})
            self.assertEqual(response.status_code, 400, suffix)
            self.assertIn('清理', str(response.data))
        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.status, 'destroyed')

    def test_imported_cluster_cannot_be_operated(self):
        """导入型集群即使有权限也拒绝，服务端不依赖界面隐藏。"""
        imported = K8sCluster.objects.create(
            organization=self.cluster.organization,
            name='导入集群', slug='', origin='imported',
        )
        self.grant_view()
        self.grant('page.k8s.clusters.backup')
        response = self.client.post(f'/api/v1/k8s/clusters/{imported.pk}/backups/', {})
        self.assertEqual(response.status_code, 400)
        self.assertIn('导入', str(response.data))

    def test_restore_requires_exact_cluster_name(self):
        """恢复的二次确认在服务端做：输错集群名一律拒绝。"""
        record = self.make_backup_record()
        self.grant_view()
        self.grant('page.k8s.clusters.restore')
        url = self.url(f'backups/{record.backup_id}/restore')
        wrong = self.client.post(url, {'confirm_name': '随便写的名字'})
        self.assertEqual(wrong.status_code, 400)
        self.assertIn('confirm_name', wrong.data['detail'])
        self.assertFalse(K8sDeployment.objects.filter(task_type='restore').exists())

        correct = self.client.post(url, {'confirm_name': self.cluster.name})
        self.assertEqual(correct.status_code, 201, correct.data)
        task = K8sDeployment.objects.get(task_type='restore')
        self.assertEqual(task.request_snapshot['submitted']['backup_id'], record.backup_id)

    def test_skip_emergency_backup_is_refused_on_a_healthy_cluster(self):
        """集群健康时不许跳过应急备份：想保留现状就单独做一次备份（规划 11.1）。"""
        record = self.make_backup_record()
        self.grant_view()
        self.grant('page.k8s.clusters.restore')
        url = self.url(f'backups/{record.backup_id}/restore')
        payload = {'confirm_name': self.cluster.name, 'skip_emergency_backup': True}
        with mock.patch(
            'k8s_cluster.views._cluster_reports_healthy', return_value=True,
        ):
            response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn('skip_emergency_backup', response.data['detail'])
        self.assertFalse(K8sDeployment.objects.filter(task_type='restore').exists())

    def test_skip_emergency_backup_is_recorded_on_an_unhealthy_cluster(self):
        """集群已经不可用时允许跳过，并把这次取舍固化在任务快照里。"""
        record = self.make_backup_record()
        self.grant_view()
        self.grant('page.k8s.clusters.restore')
        url = self.url(f'backups/{record.backup_id}/restore')
        payload = {'confirm_name': self.cluster.name, 'skip_emergency_backup': True}
        with mock.patch(
            'k8s_cluster.views._cluster_reports_healthy', return_value=False,
        ):
            response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 201, response.data)
        task = K8sDeployment.objects.get(task_type='restore')
        self.assertTrue(task.skip_emergency_backup)
        self.assertIs(task.request_snapshot['submitted']['skip_emergency_backup'], True)

    def test_restore_without_skip_flag_still_backs_up(self):
        """不勾选时任务快照里写的是 False，执行时就会照常做一次应急备份。"""
        record = self.make_backup_record()
        self.grant_view()
        self.grant('page.k8s.clusters.restore')
        response = self.client.post(
            self.url(f'backups/{record.backup_id}/restore'), {'confirm_name': self.cluster.name},
        )
        self.assertEqual(response.status_code, 201, response.data)
        task = K8sDeployment.objects.get(task_type='restore')
        self.assertFalse(task.skip_emergency_backup)

    def test_restore_of_unknown_backup_is_refused(self):
        """找不到备份时返回 400，而不是提交一个注定失败的任务。"""
        self.grant_view()
        self.grant('page.k8s.clusters.restore')
        response = self.client.post(
            self.url('backups/bk-does-not-exist/restore'), {'confirm_name': self.cluster.name},
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_backup_requires_delete_permission(self):
        """删除备份复用集群的删除权限。"""
        from k8s_deployment.models import K8sClusterBackup

        record = K8sClusterBackup.objects.create(
            organization=self.cluster.organization,
            cluster=self.cluster,
            backup_id='bk-api-del',
            slug=self.cluster.slug,
            status='succeeded',
            object_prefix='kubeasz/backups/1/api-cluster/bk-api-del',
        )
        self.grant_view()
        url = f'/api/v1/k8s/clusters/{self.cluster.pk}/backups/{record.backup_id}/'
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.grant('page.k8s.clusters.delete')
        with mock.patch(
            'k8s_deployment.services.backup.artifacts.oss_bucket',
            return_value=mock.Mock(),
        ):
            response = self.client.delete(url)
        self.assertEqual(response.status_code, 200, response.data)
        record.refresh_from_db()
        self.assertEqual(record.status, 'deleted')

    def test_protected_backup_cannot_be_deleted(self):
        """受保留策略保护的备份不能手动删除。"""
        from k8s_deployment.models import K8sClusterBackup

        record = K8sClusterBackup.objects.create(
            organization=self.cluster.organization,
            cluster=self.cluster,
            backup_id='bk-api-protected',
            slug=self.cluster.slug,
            status='succeeded',
            is_protected=True,
            object_prefix='kubeasz/backups/1/api-cluster/bk-api-protected',
        )
        self.grant_view()
        self.grant('page.k8s.clusters.delete')
        url = f'/api/v1/k8s/clusters/{self.cluster.pk}/backups/{record.backup_id}/'
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 400)
        self.assertIn('保留策略', str(response.data))
        record.refresh_from_db()
        self.assertEqual(record.status, 'succeeded')

    def test_backups_list_reports_retention_and_capability(self):
        """列表接口把保留策略与当前是否可备份一并返回，前端据此显示提示。"""
        self.grant_view()
        response = self.client.get(self.url('backups'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('retention_days', response.data)
        self.assertIn('min_keep', response.data)
        self.assertTrue(response.data['can_backup'])

    def test_backup_list_carries_the_nodes_of_each_backup_revision(self):
        """备份列表要带上备份时的节点清单（规划 11.2 的恢复确认弹窗依赖它）。

        两条备份分别落在 1 号与 2 号配置版本上，用来钉住「按备份自己的版本号取节点」这条
        路径：若实现改成取集群当前版本的节点，2 号的节点就会串到 1 号备份上。
        """
        second = dict(SNAPSHOT)
        second['nodes'] = [{
            'name': 'node-9', 'ip_address': '192.168.30.90', 'ssh_port': 22,
            'ssh_user': 'root', 'roles': ['node'], 'node_name': 'worker-9',
        }]
        K8sClusterConfigRevision.objects.create(
            organization=self.cluster.organization,
            cluster=self.cluster,
            revision=2,
            config_hash='f' * 64,
            snapshot=second,
        )
        self.make_backup_record(backup_id='bk-api-v1', config_revision=1)
        self.make_backup_record(backup_id='bk-api-v2', config_revision=2)
        self.grant_view()

        response = self.client.get(self.url('backups'))

        self.assertEqual(response.status_code, 200)
        by_id = {item['backup_id']: item for item in response.data['results']}
        self.assertEqual(
            [node['name'] for node in by_id['bk-api-v1']['nodes']],
            ['node-1', 'node-2', 'node-3', 'node-4'],
        )
        self.assertEqual([node['name'] for node in by_id['bk-api-v2']['nodes']], ['node-9'])
        self.assertEqual(by_id['bk-api-v2']['nodes'][0]['ip_address'], '192.168.30.90')
        # 弹窗只需要确认「恢复的是哪一批机器」，清单里不含 SSH 端口、用户名等连接信息。
        self.assertEqual(sorted(by_id['bk-api-v1']['nodes'][0]), ['ip_address', 'name', 'roles'])

    def test_backup_list_tolerates_a_revision_that_no_longer_exists(self):
        """备份指向的配置版本查不到（或为 0）时节点清单为空，接口不能因此报错。

        这种备份来自平台早期版本或已导入的集群，弹窗里少一行「备份时节点」即可，
        不能变成 500 让整个备份列表打不开。
        """
        self.make_backup_record(backup_id='bk-api-unknown', config_revision=99)
        self.make_backup_record(backup_id='bk-api-imported', config_revision=0)
        self.grant_view()

        response = self.client.get(self.url('backups'))

        self.assertEqual(response.status_code, 200)
        by_id = {item['backup_id']: item for item in response.data['results']}
        self.assertEqual(by_id['bk-api-unknown']['nodes'], [])
        self.assertEqual(by_id['bk-api-imported']['nodes'], [])

    def test_missing_machine_is_refused(self):
        """指定了不存在的机器时返回 400 而不是 500。"""
        self.grant_view()
        self.grant('page.k8s.clusters.add_worker')
        response = self.client.post(self.url('nodes/add-worker'), {'machine_id': 999999})
        self.assertEqual(response.status_code, 400)

    def test_add_worker_without_machine_is_refused(self):
        """扩容不带机器时返回 400。"""
        self.grant_view()
        self.grant('page.k8s.clusters.add_worker')
        response = self.client.post(self.url('nodes/add-worker'), {})
        self.assertEqual(response.status_code, 400)

    def test_action_permission_alone_is_not_enough(self):
        """只有操作权限、没有菜单与页面权限时依然拒绝。

        权限链是「菜单 → 页面 → 操作」，缺任一环都不放行；只授权按钮而没给页面入口，
        用户即使拿到按钮权限也不该能调通接口。
        """
        self.revoke_all()
        self.grant('page.k8s.clusters.backup')
        self.assertEqual(self.client.post(self.url('backups'), {}).status_code, 403)
        self.grant('page.k8s.clusters.view')
        self.assertEqual(self.client.post(self.url('backups'), {}).status_code, 403)
        self.grant('menu.k8s')
        self.assertEqual(self.client.post(self.url('backups'), {}).status_code, 201)


class ClusterOpsPermissionCatalogTest(TestCase):
    """运维权限码必须真的登记在权限目录里。

    没有登记的操作码在权限策略页里不会出现，用户根本没法授权——接口写着要权限、
    界面上却找不到开关，是比缺权限更难排查的一类问题。
    """

    def test_cluster_ops_actions_are_seeded(self):
        """五个运维动作都在「K8s 集群」页面的操作列表里。"""
        page = PermissionMenuNode.objects.filter(code='page.k8s.clusters.view').first()
        self.assertIsNotNone(page, '权限目录里应当有 K8s 集群页面')
        codes = set(PermissionMenuAction.objects.filter(page=page).values_list('code', flat=True))
        for action in ('add_master', 'add_worker', 'upgrade', 'backup', 'restore'):
            self.assertIn(action, codes, f'缺少运维操作权限码 {action}')

    def test_actions_are_visible_in_the_catalog(self):
        """操作码要能从目录里枚举出来，否则策略页拿不到可勾选项。"""
        codes = set(all_permission_codes())
        for action in ('add_master', 'add_worker', 'upgrade', 'backup', 'restore'):
            self.assertIn(f'page.k8s.clusters.{action}', codes)

    def test_action_requires_the_whole_permission_chain(self):
        """勾选操作权限时，菜单与页面权限要能一并推导出来。"""
        chain = required_codes_for_permission('page.k8s.clusters.restore')
        self.assertIn('menu.k8s', chain)
        self.assertIn('page.k8s.clusters.view', chain)
        self.assertIn('page.k8s.clusters.restore', chain)

    def test_deleted_backup_reuses_cluster_delete_code(self):
        """删除备份不另立权限码，避免策略页出现功能重复的开关。"""
        page = PermissionMenuNode.objects.filter(code='page.k8s.clusters.view').first()
        codes = set(PermissionMenuAction.objects.filter(page=page).values_list('code', flat=True))
        self.assertIn('delete', codes)
        self.assertNotIn('delete_backup', codes)
