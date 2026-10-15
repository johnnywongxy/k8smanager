"""集群登记（``services/register``）在 SQLite 写锁冲突下的行为。

背景是 2026-10-08 的 ``jd-k8s`` 故障：整个登记函数被一个 ``@transaction.atomic`` 罩住，
事务里的第一条语句是读（``ensure_deployed_cluster`` 的 ``filter(slug=...)``），随后的
INSERT 要把它升级成写锁 —— 库跑在 WAL 模式，只要在这份读快照之后有别的连接提交过，
升级就**立刻**返回 ``database is locked``（``busy_timeout`` 对升级冲突不生效，这是 WAL 的
既定行为）。结果整个事务回滚：节点上的集群装得好好的，平台侧却连一条集群记录都没留下，
集群接入列表里空空如也，任务只留一句「未预期的错误：database is locked」。

这里盯住四件事：

1. 写锁冲突被识别出来并重试，重试成功后集群与配置版本都在，且**不会写出两条集群**；
2. 重试耗尽时报的是中文的 ``RegisterError``，而不是把 SQLite 原文抛给用户；
3. **体检失败不回滚已经提交的集群** —— 「回填失败不回滚集群」是模块文档里的承诺；
4. 非锁类的 ``OperationalError`` 不被吞掉、不被重试；
5. 体检的结论同时写进集群档案的「最近检测」三列，成功与失败都写 —— 列表上那一列不能一直空着
   （见 ``HealthCheckResultFieldsTest``）。
"""

import io
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from k8s_cluster.models import K8sCluster, K8sClusterConfigRevision, K8sClusterHealthCheck
from k8s_deployment.models import K8sDeployment, K8sDeploymentNode
from k8s_deployment.services import register
from ops.models import Organization

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'


class DatabaseLockRetryTest(SimpleTestCase):
    """``run_with_lock_retry`` 的判据与退避：不碰数据库。"""

    def test_lock_message_is_recognized_regardless_of_case(self):
        """SQLite 的锁冲突原文大小写不定，判据不能只认小写那一种。

        ``_is_database_locked`` 只判文本，不判异常类型 —— 「只有 ``OperationalError``
        才可能是锁冲突」这道闸门在 ``run_with_lock_retry`` 的 ``except`` 上（下面那条用例
        覆盖的就是它）。两处分工不同，别指望这个判据自己挡住别的异常类型。
        """
        self.assertTrue(register._is_database_locked(OperationalError('database is locked')))
        self.assertTrue(register._is_database_locked(OperationalError('DATABASE IS LOCKED')))
        self.assertFalse(register._is_database_locked(OperationalError('no such table: k8s_cluster')))
        self.assertFalse(register._is_database_locked(OperationalError('disk I/O error')))

    def test_retries_until_the_lock_clears(self):
        """前两次冲突、第三次成功：结果照常返回，且一共试了三次。"""
        calls = []

        def action():
            calls.append(1)
            if len(calls) < 3:
                raise OperationalError('database is locked')
            return 'ok'

        with mock.patch.object(register.time, 'sleep') as sleep:
            result = register.run_with_lock_retry(action, '登记集群与配置版本')

        self.assertEqual(result, 'ok')
        self.assertEqual(len(calls), 3)
        # 退避是 0.2 / 0.4：等得太密等于没等（另一个写事务还没提交完）。
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.2, 0.4])

    def test_non_lock_operational_error_is_not_retried(self):
        """表不存在之类的错误重试一万次也没用，必须原样抛出。"""
        calls = []

        def action():
            calls.append(1)
            raise OperationalError('no such column: k8s_deployment.auto_clean_stale')

        with mock.patch.object(register.time, 'sleep') as sleep:
            with self.assertRaises(OperationalError):
                register.run_with_lock_retry(action, '登记集群与配置版本')

        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_exhausted_retries_report_a_readable_chinese_reason(self):
        """重试耗尽要报中文原因，并说清「集群已经装好、重试任务即可补登记」。"""
        calls = []

        def action():
            calls.append(1)
            raise OperationalError('database is locked')

        with mock.patch.object(register.time, 'sleep'):
            with self.assertRaises(register.RegisterError) as captured:
                register.run_with_lock_retry(action, '登记集群与配置版本')

        message = str(captured.exception)
        self.assertEqual(len(calls), register.DB_LOCK_ATTEMPTS)
        self.assertIn('登记集群与配置版本', message)
        self.assertIn('database is locked', message)
        self.assertIn('集群本身已经装好', message)


class RegisterBackfillTestBase(TestCase):
    """登记用例的公共夹具：一个组织、一条 HA 任务与三台节点快照。"""

    def setUp(self):
        """准备临时状态根目录、组织、任务与节点快照。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录并写入数据库；用例结束时删除临时目录。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-register-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
            K8S_DEPLOY_KUBEASZ_ROOT=str(self.state_root / 'kubeasz'),
            K8S_DEPLOY_CLUSTERS_ROOT=str(self.state_root / 'clusters'),
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.org = Organization.objects.create(name='登记测试组织', slug='register-test')
        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            task_type='deploy_ha',
            status='running',
            display_name='jd-k8s',
            slug='jd-k8s',
            kubernetes_version='1.29.4',
            kubeasz_version='3.7.0',
            runtime='containerd',
            os_type='CentOS',
            os_version='7',
            topology_mode='ha',
            kube_proxy_mode='iptables',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            api_server_port=6443,
            cni_plugin='calico',
            execution_mode='real',
        )
        self.nodes = [
            K8sDeploymentNode.objects.create(
                organization=self.org,
                deployment=self.deployment,
                name=f'node-{index + 1}',
                ip_address=f'192.168.1.{index + 1}',
                roles=['etcd', 'master'],
            )
            for index in range(3)
        ]
        self.compile_result = {
            'config_hash': 'a' * 64,
            'template': 'reregister',
            'resolved_versions': {},
            'hosts_text': '[etcd]\n192.168.1.1\n',
            'config_text': 'CLUSTER_CIDR: "10.244.0.0/16"\n',
        }

    def clusters(self):
        """返回本任务 slug 对应的集群记录（正常情况下最多一条）。"""
        return K8sCluster.objects.filter(slug=self.deployment.slug)

    def revisions(self):
        """返回本任务 slug 对应集群的配置版本记录。"""
        return K8sClusterConfigRevision.objects.filter(cluster__slug=self.deployment.slug)

    def _register_without_health_probe(self):
        """登记一次但不做体检，返回 ``(cluster, revision)``。

        参数：无。
        返回：返回 ``(K8sCluster, K8sClusterConfigRevision)``。
        副作用：写入集群、配置版本与机器归属（体检被打桩）。
        """
        with mock.patch.object(
            register, 'run_health_check',
            return_value={'ok': True, 'message': '打桩', 'detail': {}},
        ):
            register.register_deployment_result(
                self.deployment, self.compile_result, self.nodes, kubeasz_root=None,
            )
        return self.clusters().first(), self.revisions().first()


class RegisterBackfillRetryTest(RegisterBackfillTestBase):
    """写阶段撞上写锁冲突时，登记要重试成功而不是整个丢失。"""

    def test_backfill_survives_a_transient_lock_conflict(self):
        """第一次写配置版本时冲突、重试后成功：集群与版本都在，且集群只有一条。"""
        real_append = register.append_config_revision
        attempts = []

        def flaky_append(*args, **kwargs):
            """第一次调用模拟写锁冲突，之后走真实实现。"""
            attempts.append(1)
            if len(attempts) == 1:
                raise OperationalError('database is locked')
            return real_append(*args, **kwargs)

        with mock.patch.object(register, 'append_config_revision', side_effect=flaky_append):
            with mock.patch.object(register.time, 'sleep'):
                summary = register.register_deployment_result(
                    self.deployment, self.compile_result, self.nodes,
                    kubeasz_root=None, note='补登记用例',
                )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.clusters().count(), 1)
        self.assertEqual(self.revisions().count(), 1)
        self.assertEqual(summary['revision'], 1)
        self.assertEqual(summary['cluster_id'], self.clusters().first().pk)
        # 集群记录要挂回任务上，否则任务详情里仍然看不到它指向哪个集群。
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.cluster_id, self.clusters().first().pk)

    def test_exhausted_lock_conflict_leaves_no_half_written_cluster(self):
        """重试全失败时不能留下半条集群：要么完整登记，要么什么都没有。"""
        with mock.patch.object(
            register, 'append_config_revision',
            side_effect=OperationalError('database is locked'),
        ):
            with mock.patch.object(register.time, 'sleep'):
                with self.assertRaises(register.RegisterError):
                    register.register_deployment_result(
                        self.deployment, self.compile_result, self.nodes, kubeasz_root=None,
                    )

        # write_backfill 每次重跑都在自己的事务里回滚，集群行不会残留。
        self.assertEqual(self.clusters().count(), 0)
        self.assertEqual(self.revisions().count(), 0)


class RegisterHealthCheckIsolationTest(RegisterBackfillTestBase):
    """体检在写事务之外：它失败不能把已经提交的集群一起带走。"""

    def test_health_check_failure_does_not_roll_back_the_cluster(self):
        """体检抛异常时集群与配置版本仍然留在库里，异常照常往上抛。"""
        with mock.patch.object(
            register, 'run_health_check', side_effect=RuntimeError('体检炸了'),
        ):
            with self.assertRaises(RuntimeError):
                register.register_deployment_result(
                    self.deployment, self.compile_result, self.nodes, kubeasz_root=None,
                )

        self.assertEqual(self.clusters().count(), 1)
        self.assertEqual(self.revisions().count(), 1)

    def test_unreachable_cluster_is_recorded_as_unavailable(self):
        """集群连不上（这里没有凭据）时体检返回 ok=False 并留一条不可用记录，不抛异常。"""
        cluster, _revision = self._register_without_health_probe()

        with mock.patch('k8s_cluster.services.probe_cluster', side_effect=RuntimeError('连接被拒绝')):
            health = register.run_health_check(cluster, timeout_detail={'deployment_id': self.deployment.pk})

        self.assertFalse(health['ok'])
        self.assertIn('连接被拒绝', health['message'])
        record = K8sClusterHealthCheck.objects.filter(cluster=cluster).latest('pk')
        self.assertEqual(record.status, 'unavailable')
        self.assertEqual(record.detail['extra']['deployment_id'], self.deployment.pk)


class HealthCheckResultFieldsTest(RegisterBackfillTestBase):
    """体检结论要同时落到集群档案的「最近检测」三列上。

    背景是 2026-10-12 的 JD-k8s：集群装成功、登记也成功，列表里状态是「可用」，**最近检测
    却是空的**（显示成「-」）—— 与「部署成功却好像哪儿不对」的观感叠在一起，用户就是照着
    这一列来怀疑集群的。原因是这里从前只往健康检查表写了一行，没写集群上那三个字段
    （``k8s_cluster.models.K8sCluster.mark_test_result`` 会写，手工「连接测试」那条路于是
    是好的，只有自动登记这条路漏了）。

    两个方向都要写：探测成功要写，探测失败同样要写 —— 一次刚做过的失败探测，列表上不能还
    留着上一次的旧结论。
    """

    # 一次成功探测的返回值。字段口径同 ``probe_cluster``。
    STATE = {
        'status': 'available',
        'message': '集群连通性检查通过',
        'latency_ms': 248,
        'kube_version': 'v1.29.4',
        'node_count': 3,
        'runtime_version': 'containerd://1.7.13',
        'network_plugin': 'calico',
        'node_summary': {'total': 3, 'ready': 3, 'not_ready': [], 'versions': {}, 'details': []},
    }

    def probe(self, **overrides):
        """在一份成功探测结果上打桩，跑一次体检。

        参数：`overrides` 表示要覆盖的探测结果字段。
        返回：返回 ``(健康检查返回值, 重新读出来的集群)``。
        副作用：写入健康检查表并更新集群档案。
        """
        state = dict(self.STATE)
        state.update(overrides)
        cluster, _revision = self._register_without_health_probe()

        with mock.patch('k8s_cluster.services.probe_cluster', return_value=state):
            health = register.run_health_check(
                cluster, timeout_detail={'deployment_id': self.deployment.pk},
            )

        cluster.refresh_from_db()
        return health, cluster

    def test_successful_check_fills_the_columns(self):
        """成功探测后三列都有值：列表上的「最近检测」不该还显示成「-」。"""
        _health, cluster = self.probe()

        self.assertEqual(cluster.last_test_status, 'available')
        self.assertEqual(cluster.last_test_message, '集群连通性检查通过')
        self.assertIsNotNone(cluster.last_tested_at)

    def test_failed_check_fills_the_columns_too(self):
        """探测失败也要写：这次探测确实发生了，留着上一次的旧结论比空着更误导人。"""
        cluster, _revision = self._register_without_health_probe()

        with mock.patch('k8s_cluster.services.probe_cluster', side_effect=RuntimeError('连接被拒绝')):
            health = register.run_health_check(
                cluster, timeout_detail={'deployment_id': self.deployment.pk},
            )

        cluster.refresh_from_db()
        self.assertFalse(health['ok'])
        self.assertEqual(cluster.last_test_status, 'unavailable')
        self.assertIn('连接被拒绝', cluster.last_test_message)
        self.assertIsNotNone(cluster.last_tested_at)

    def test_the_columns_agree_with_the_health_record(self):
        """两处口径要一致：列表列与详情里的最新一条记录说的必须是同一句话。"""
        _health, cluster = self.probe()

        record = K8sClusterHealthCheck.objects.filter(cluster=cluster).latest('pk')
        self.assertEqual(cluster.last_test_status, record.status)
        self.assertEqual(cluster.last_test_message, record.message)

    def test_a_check_writes_exactly_one_health_record(self):
        """一次体检只留一条记录。

        这里盯着的是「顺手再补一条」的写法：``mark_test_result`` 会额外写一行，直接拿来用
        就会让每次体检在详情里留下两条一模一样的记录。
        """
        _health, cluster = self.probe()

        self.assertEqual(K8sClusterHealthCheck.objects.filter(cluster=cluster).count(), 1)

    def test_not_ready_nodes_are_named_in_the_recent_test_message(self):
        """结论里要点名没就绪的节点：只说「3 个节点」回答不了「哪一台没起来」。"""
        _health, cluster = self.probe(
            node_summary={
                'total': 3, 'ready': 2, 'not_ready': ['node-2'],
                'versions': {}, 'details': [],
            },
        )

        self.assertIn('node-2', cluster.last_test_message)


class RegisterDeploymentCommandTest(RegisterBackfillTestBase):
    """补登记命令：集群已经装好、平台却没记上时的恢复入口。"""

    def _write_cluster_dir(self, with_kubeconfig=False):
        """在临时 kubeasz 根目录里造出任务对应的集群目录。

        参数：`with_kubeconfig` 表示要不要一并写出 kubeasz 生成的 kubeconfig。
        返回：返回写好的集群目录 Path。
        副作用：写入 hosts / config.yml（以及可选的 kubectl.kubeconfig）。
        """
        cluster_dir = Path(self.state_root) / 'kubeasz' / 'clusters' / self.deployment.slug
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / 'hosts').write_text(self.compile_result['hosts_text'], encoding='utf-8')
        (cluster_dir / 'config.yml').write_text(self.compile_result['config_text'], encoding='utf-8')
        if with_kubeconfig:
            (cluster_dir / 'kubectl.kubeconfig').write_text('apiVersion: v1\n', encoding='utf-8')
        return cluster_dir

    def test_missing_cluster_dir_points_at_the_external_import_route(self):
        """集群目录不在了就补不了登记，报错要指向「添加外部集群」而不是含糊其辞。"""
        with self.assertRaises(CommandError) as captured:
            call_command('register_deployment', self.deployment.pk, stdout=io.StringIO())

        self.assertIn('添加外部集群', str(captured.exception))

    def test_dry_run_checks_preconditions_without_writing(self):
        """--dry-run 只检查前置条件，不写集群记录。"""
        self._write_cluster_dir()
        out = io.StringIO()

        call_command('register_deployment', self.deployment.pk, '--dry-run', stdout=out)

        self.assertIn('--dry-run', out.getvalue())
        self.assertEqual(self.clusters().count(), 0)

    def test_command_registers_the_cluster(self):
        """正常补登记：写出集群与配置版本，体检不通也不影响退出码。"""
        self._write_cluster_dir()
        out = io.StringIO()

        with mock.patch('k8s_cluster.services.probe_cluster', side_effect=RuntimeError('暂时连不上')):
            call_command('register_deployment', self.deployment.pk, stdout=out)

        self.assertEqual(self.clusters().count(), 1)
        self.assertEqual(self.revisions().count(), 1)
        self.assertIn('已登记集群', out.getvalue())
        # 体检不通是警告而不是失败：集群记录已经写进去了。
        self.assertIn('连通性体检未通过', out.getvalue())

    def test_already_registered_task_is_left_alone(self):
        """已经登记过的任务不重复写，免得叠出第二个配置版本。"""
        self._write_cluster_dir()
        with mock.patch('k8s_cluster.services.probe_cluster', side_effect=RuntimeError('暂时连不上')):
            call_command('register_deployment', self.deployment.pk, stdout=io.StringIO())

        out = io.StringIO()
        call_command('register_deployment', self.deployment.pk, stdout=out)

        self.assertIn('已经登记过', out.getvalue())
        self.assertEqual(self.clusters().count(), 1)
        self.assertEqual(self.revisions().count(), 1)
