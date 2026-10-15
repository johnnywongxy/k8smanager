"""备份归档的构建、上传校验、保留策略、删除与恢复选择。

OSS 被替换成内存实现 ``FakeBucket``，kubeasz 命令被替换成 ``FakeRunner``，
SSH 采集公钥被替换成固定返回值。被测的是备份服务自己的判断逻辑：
归档内容对不对、明文有没有留在盘上、回读校验能不能挡住被改动的对象、
保留策略会不会误删唯一的一份备份、恢复是不是真的用了用户选的那一份。
"""

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from k8s_cluster.models import K8sCluster
from k8s_deployment.models import K8sClusterBackup
from k8s_deployment.services import backup, node_address, paths, register, runner
from ops.models import Organization, SystemSetting

from . import kubeasz_fixtures

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'


class FakeObject:
    """内存对象存储里的一个对象。"""

    def __init__(self, key, payload):
        """记录对象键与内容。

        参数：`key` 表示对象键；`payload` 表示对象字节。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.key = key
        self.payload = payload
        self.content_length = len(payload)

    @property
    def size(self):
        """返回对象字节数，供 list_objects 使用。"""
        return len(self.payload)


class FakeStream:
    """``get_object`` 返回的可读流。"""

    def __init__(self, payload):
        """记录要流式返回的字节。

        参数：`payload` 表示对象字节。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self._payload = payload
        self._offset = 0

    def read(self, size=-1):
        """读取指定字节数。

        参数：`size` 表示读取长度，-1 表示全部。
        返回：返回读到的字节；到末尾返回空字节。
        副作用：推进本机偏移量。
        """
        if size is None or size < 0:
            block = self._payload[self._offset:]
            self._offset = len(self._payload)
            return block
        block = self._payload[self._offset:self._offset + size]
        self._offset += len(block)
        return block


class FakeBucket:
    """内存版的 OSS Bucket，只实现备份服务实际用到的那几个方法。"""

    def __init__(self):
        self.objects = {}
        self.fail_on = set()

    def put_object_from_file(self, key, path):
        """从本地文件写入一个对象。"""
        if 'put' in self.fail_on:
            raise OSError('模拟上传失败')
        self.objects[key] = FakeObject(key, Path(path).read_bytes())

    def put_object(self, key, payload):
        """写入一段字节。"""
        if 'put' in self.fail_on:
            raise OSError('模拟上传失败')
        self.objects[key] = FakeObject(key, bytes(payload))

    def head_object(self, key):
        """返回对象元信息。"""
        if 'head' in self.fail_on:
            raise OSError('模拟读取元信息失败')
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def get_object(self, key):
        """返回可读流。"""
        if 'get' in self.fail_on:
            raise OSError('模拟下载失败')
        if key not in self.objects:
            raise KeyError(key)
        return FakeStream(self.objects[key].payload)

    def delete_object(self, key):
        """删除对象；不存在时按 OSS 语义静默成功。"""
        if 'delete' in self.fail_on:
            raise OSError('模拟删除失败')
        self.objects.pop(key, None)

    def list_objects(self, prefix=''):
        """按前缀列出对象。"""
        result = mock.Mock()
        result.object_list = [
            item for key, item in sorted(self.objects.items()) if key.startswith(prefix)
        ]
        return result

    def tamper(self, key, payload):
        """把某个对象的内容换掉，用于模拟 OSS 上被替换或截断的情况。"""
        self.objects[key] = FakeObject(key, bytes(payload))


class BackupServiceTestBase(TestCase):
    """备份服务用例的公共夹具。"""

    def setUp(self):
        """准备临时状态目录、组织、集群、一份合成 kubeasz 树与内存 OSS。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录、数据库记录与 OSS 配置。
        """
        self.state_root = Path(tempfile.mkdtemp(prefix='k8s-bk-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_ARTIFACT_ROOT=str(self.state_root / 'artifacts'),
            K8S_DEPLOY_WORKER_LOCK=str(self.state_root / 'worker.lock'),
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        SystemSetting.objects.create(key='storage.oss', value={
            'enabled': True,
            'access_key_id': 'test-ak',
            'access_key_secret': 'test-sk',
            'bucket': 'test-bucket',
            'endpoint': 'oss.example.com',
            'base_dir': 'kubeasz',
        })

        self.org = Organization.objects.create(name='备份测试组织', slug='backup-test')
        self.cluster = K8sCluster.objects.create(
            organization=self.org,
            name='备份测试集群',
            slug='bk-cluster',
            origin='deployed',
            kube_version='1.29.4',
            network_plugin='calico',
            status='configured',
        )
        self.kubeasz_root = kubeasz_fixtures.write_kubeasz_tree(
            self.state_root / 'release', version='3.6.7',
        )
        kubeasz_fixtures.write_cluster_config(self.kubeasz_root, 'bk-cluster')
        self.bucket = FakeBucket()
        patcher = mock.patch.object(backup.artifacts, 'oss_bucket', return_value=self.bucket)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 采集 CA 公钥需要连目标机，这里替换成固定的“读不到”结果，避免用例碰 SSH。
        meta = mock.patch.object(backup, 'control_plane_metadata', return_value={})
        meta.start()
        self.addCleanup(meta.stop)
        # 备份前要确认平台机拨得到 etcd 客户端口。夹具里的集群地址（192.168.1.1）在跑用例的
        # 机器上当然不通，但「通不通」不是这些用例要测的东西 —— 真去拨一次只会让用例既慢又飘。
        # 默认装作通，要测「不通就当场失败」的用例自己再覆盖一遍即可。
        reach = mock.patch.object(node_address, 'platform_reaches', return_value=True)
        reach.start()
        self.addCleanup(reach.stop)
        # 备份流程需要一个任务来提供工作区与版本信息。
        self.deployment = self.make_deployment()

    def add_revision(self, revision=1):
        """为集群补一条配置版本记录。

        参数：`revision` 表示版本号。
        返回：返回配置版本记录。
        副作用：向数据库写入一条配置版本。
        """
        from k8s_cluster.models import K8sClusterConfigRevision

        return K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=revision,
            config_hash='a' * 64,
            snapshot={
                'kubeasz_version': '3.6.7',
                'kubernetes_version': '1.29.4',
                'nodes': [{'name': 'n1', 'ip_address': '192.168.20.10', 'roles': ['master']}],
            },
        )

    def make_backup_record(self, **extra):
        """创建一条备份记录。

        参数：`extra` 表示要覆盖的字段。
        返回：返回新建的 K8sClusterBackup。
        副作用：向数据库写入一条备份记录。
        """
        values = {
            'organization': self.org,
            'cluster': self.cluster,
            'backup_id': backup.new_backup_id(),
            'slug': self.cluster.slug,
            'kubernetes_version': '1.29.4',
            'kubeasz_version': '3.6.7',
            'config_revision': 1,
            'status': 'succeeded',
            'object_prefix': f'kubeasz/backups/{self.org.pk}/bk-cluster/fixture',
            'archive_sha256': 'b' * 64,
            'archive_size': 1,
        }
        values.update(extra)
        return K8sClusterBackup.objects.create(**values)

    def make_deployment(self, task_type='backup', **extra):
        """创建一条与集群关联的任务，供备份流程提供工作区与版本信息。

        参数：`task_type` 表示任务类型；`extra` 表示要覆盖的字段。
        返回：返回新建的 K8sDeployment。
        副作用：向数据库写入一条任务记录。
        """
        from k8s_deployment.models import K8sDeployment

        values = {
            'organization': self.org,
            'cluster': self.cluster,
            'task_type': task_type,
            'status': 'running',
            'display_name': f'{self.cluster.name} · 备份',
            'slug': f'op-{task_type}-fixture',
            'kubernetes_version': '1.29.4',
            'kubeasz_version': '3.6.7',
            'runtime': 'containerd',
            'os_type': 'Rocky Linux',
            'os_version': '8',
            'topology_mode': 'ha',
            'kube_proxy_mode': 'ipvs',
            'network_interface': 'eth0',
            'pod_cidr': '10.244.0.0/16',
            'service_cidr': '10.96.0.0/12',
            'cni_plugin': 'calico',
            'execution_mode': 'simulated',
        }
        values.update(extra)
        return K8sDeployment.objects.create(**values)


class CreateBackupTest(BackupServiceTestBase):
    """``create_backup`` 的完整流程。"""

    def _create(self, **kwargs):
        """执行一次备份，snapshot 文件已由调用方准备好。

        参数：`kwargs` 表示透传给 create_backup 的参数。
        返回：返回 K8sClusterBackup。
        副作用：执行备份服务主流程。
        """
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        return backup.create_backup(
            self.cluster,
            deployment=self.deployment,
            kubeasz_root=self.kubeasz_root,
            runner_instance=runner.FakeRunner(stage_delay=0.0),
            nodes=[],
            **kwargs,
        )

    def test_successful_backup_uploads_three_objects(self):
        """成功路径上传归档、指针与校验文件三个对象，并把记录推到 succeeded。"""
        record = self._create()
        self.assertEqual(record.status, 'succeeded')
        self.assertTrue(record.object_prefix)
        self.assertGreater(record.archive_size, 0)
        self.assertEqual(len(record.archive_sha256), 64)
        keys = sorted(self.bucket.objects)
        self.assertEqual(len(keys), 3)
        self.assertTrue(any(key.endswith(backup.ARCHIVE_OBJECT_NAME) for key in keys))
        self.assertTrue(any(key.endswith(backup.POINTER_OBJECT_NAME) for key in keys))
        self.assertTrue(any(key.endswith(backup.CHECKSUMS_OBJECT_NAME) for key in keys))

    def test_uploaded_content_is_ciphertext(self):
        """OSS 上存的是密文，明文 snapshot 内容不得可直接读出。"""
        self._create()
        key = next(key for key in self.bucket.objects if key.endswith(backup.ARCHIVE_OBJECT_NAME))
        payload = self.bucket.objects[key].payload
        self.assertTrue(payload.startswith(backup.CIPHERTEXT_MAGIC))
        self.assertNotIn(b'snapshot-payload-0', payload)

    def test_pointer_file_carries_no_credentials(self):
        """指针文件只描述归档，不含任何凭据字样。"""
        self._create()
        key = next(key for key in self.bucket.objects if key.endswith(backup.POINTER_OBJECT_NAME))
        pointer = json.loads(self.bucket.objects[key].payload.decode('utf-8'))
        self.assertEqual(pointer['backup_id'], key.split('/')[-2])
        text = json.dumps(pointer, ensure_ascii=False)
        for token in ('password', 'secret', 'access_key', 'private_key'):
            self.assertNotIn(token, text.lower())

    def test_plaintext_archive_is_removed_after_upload(self):
        """上传结束后明文归档与密文文件都不留在平台盘上。"""
        self._create()
        leftovers = list((self.state_root / 'runs').rglob('*.zip'))
        self.assertEqual(leftovers, [], f'平台上残留了明文归档：{leftovers}')
        enc_leftovers = list((self.state_root / 'runs').rglob('*.bin.enc'))
        self.assertEqual(enc_leftovers, [])

    def test_archive_contains_manifest_and_configs(self):
        """归档里必须有 manifest、集群配置与 snapshot，恢复才可能成功。"""
        self._create()
        key = next(key for key in self.bucket.objects if key.endswith(backup.ARCHIVE_OBJECT_NAME))
        cipher_path = self.state_root / 'inspect.bin.enc'
        cipher_path.write_bytes(self.bucket.objects[key].payload)
        plain_path = self.state_root / 'inspect.zip'
        backup.decrypt_stream(cipher_path, plain_path)

        with zipfile.ZipFile(plain_path) as bundle:
            names = bundle.namelist()
            self.assertIn(backup.ARCHIVE_MANIFEST_NAME, names)
            self.assertIn('cluster/hosts', names)
            self.assertIn('cluster/config.yml', names)
            self.assertTrue(any(name.startswith(backup.ARCHIVE_SNAPSHOT_PREFIX) for name in names))
            self.assertTrue(any(name == 'source/ezctl' for name in names))
            manifest = json.loads(bundle.read(backup.ARCHIVE_MANIFEST_NAME).decode('utf-8'))
        self.assertEqual(manifest['backup_id'], key.split('/')[-2])
        self.assertEqual(manifest['cluster_slug'], 'bk-cluster')
        self.assertEqual(manifest['kubeasz_version'], '3.6.7')
        self.assertNotIn('password', json.dumps(manifest, ensure_ascii=False).lower())

    def test_pre_upgrade_backup_records_the_cluster_version_not_the_target(self):
        """升级前的强制备份要记集群**当前**的版本，而不是本次升级的目标版本。

        94 上就是这里出的事：升级任务把强制备份标成目标版本（kubeasz 3.7.0 / 1.36），
        而快照里装的还是旧集群（3.6.10 / 1.35）的数据。真要回滚时
        ``validate_restore_selection`` 拿这个标识去比集群当前版本，必然不等 ——
        平台自己生成的退路，被自己的校验挡住了。
        """
        self.add_revision()
        target = self.make_deployment(
            task_type='upgrade', kubeasz_version='3.7.0', kubernetes_version='1.30.0',
        )
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        record = backup.create_backup(
            self.cluster, deployment=target, kubeasz_root=self.kubeasz_root,
            runner_instance=runner.FakeRunner(stage_delay=0.0), nodes=[],
        )

        self.assertEqual(record.kubeasz_version, '3.6.7')
        self.assertEqual(record.kubernetes_version, '1.29.4')
        # 记对了才回得去：这份备份直接通过恢复校验。
        backup.validate_restore_selection(self.cluster, record)

    def test_archive_keeps_the_source_version_it_bundled(self):
        """归档里那份 kubeasz 源码包来自本次任务的制品，manifest 里如实标出来。"""
        self.add_revision()
        target = self.make_deployment(task_type='upgrade', kubeasz_version='3.7.0')
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        record = backup.create_backup(
            self.cluster, deployment=target, kubeasz_root=self.kubeasz_root,
            runner_instance=runner.FakeRunner(stage_delay=0.0), nodes=[],
        )

        key = next(key for key in self.bucket.objects if key.endswith(backup.ARCHIVE_OBJECT_NAME))
        cipher_path = self.state_root / 'inspect-source.bin.enc'
        cipher_path.write_bytes(self.bucket.objects[key].payload)
        plain_path = self.state_root / 'inspect-source.zip'
        backup.decrypt_stream(cipher_path, plain_path)
        with zipfile.ZipFile(plain_path) as bundle:
            manifest = json.loads(bundle.read(backup.ARCHIVE_MANIFEST_NAME).decode('utf-8'))
        self.assertEqual(manifest['kubeasz_version'], '3.6.7')
        self.assertEqual(manifest['source_kubeasz_version'], '3.7.0')
        self.assertEqual(record.backup_id, manifest['backup_id'])

    def test_missing_snapshot_files_fails_the_record(self):
        """kubeasz 没产出 snapshot 时必须失败，而不是上传一个空备份。"""
        with self.assertRaises(backup.BackupError) as caught:
            backup.create_backup(
                self.cluster,
                deployment=self.deployment,
                kubeasz_root=self.kubeasz_root,
                runner_instance=runner.FakeRunner(stage_delay=0.0),
                nodes=[],
            )
        self.assertIn('snapshot', str(caught.exception))
        record = K8sClusterBackup.objects.filter(cluster=self.cluster).latest('id')
        self.assertEqual(record.status, 'failed')
        self.assertTrue(record.failure_reason)

    def test_placeholder_key_refuses_to_back_up(self):
        """源码默认密钥下拒绝创建备份：用它加密等于明文外泄。"""
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        with override_settings(SECRET_KEY='platform-template-local-dev-secret-change-me'):
            with self.assertRaises(backup.BackupError) as caught:
                backup.create_backup(
                    self.cluster,
                    kubeasz_root=self.kubeasz_root,
                    runner_instance=runner.FakeRunner(stage_delay=0.0),
                    nodes=[],
                )
        self.assertIn('SECRET_KEY', str(caught.exception))
        self.assertEqual(self.bucket.objects, {}, '拒绝备份时不得向 OSS 写入任何对象')

    def test_oversized_archive_is_aborted_before_upload(self):
        """归档超过上限时在上传前中止，避免把一个巨物推上 OSS。"""
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        with override_settings(K8S_DEPLOY_BACKUP_MAX_BYTES=1):
            with self.assertRaises(backup.BackupError) as caught:
                backup.create_backup(
                    self.cluster,
                    deployment=self.deployment,
                    kubeasz_root=self.kubeasz_root,
                    runner_instance=runner.FakeRunner(stage_delay=0.0),
                    nodes=[],
                )
        self.assertIn('上限', str(caught.exception))
        self.assertEqual(self.bucket.objects, {})

    def test_upload_failure_marks_record_failed(self):
        """上传失败时记录落 failed 并保留原因，便于用户在列表里看到。"""
        self.bucket.fail_on.add('put')
        with self.assertRaises(backup.BackupError):
            self._create()
        record = K8sClusterBackup.objects.filter(cluster=self.cluster).latest('id')
        self.assertEqual(record.status, 'failed')
        self.assertIn('上传', record.failure_reason)

    def test_reused_record_is_moved_back_to_running(self):
        """重试时复用待执行记录，不另起一行；失败原因要被清掉。"""
        record = self.make_backup_record(status='failed', failure_reason='上一次失败')
        result = self._create(record=record)
        self.assertEqual(result.pk, record.pk)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(result.failure_reason, '')
        self.assertEqual(K8sClusterBackup.objects.filter(cluster=self.cluster).count(), 1)


class VerifyRemoteBackupTest(BackupServiceTestBase):
    """回读校验必须能挡住被截断或被替换的对象。"""

    def setUp(self):
        """准备一个已上传的备份，供各用例破坏后再校验。

        参数：无。
        返回：无显式返回值。
        副作用：执行一次备份流程。
        """
        super().setUp()
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        self.record = backup.create_backup(
            self.cluster,
            deployment=self.deployment,
            kubeasz_root=self.kubeasz_root,
            runner_instance=runner.FakeRunner(stage_delay=0.0),
            nodes=[],
        )
        self.archive_key = f'{self.record.object_prefix}/{backup.ARCHIVE_OBJECT_NAME}'
        self.cipher = {
            'size': self.record.archive_size,
            'sha256': self.record.archive_sha256,
            'chunks': 1,
        }

    def test_intact_object_passes(self):
        """未被动过的对象校验通过。"""
        backup.verify_remote_backup(self.bucket, self.record.object_prefix, self.cipher)

    def test_truncated_object_is_rejected(self):
        """被截断的对象在大小比对这一步就被拒绝。"""
        self.bucket.tamper(self.archive_key, self.bucket.objects[self.archive_key].payload[:-10])
        with self.assertRaises(backup.BackupError) as caught:
            backup.verify_remote_backup(self.bucket, self.record.object_prefix, self.cipher)
        self.assertIn('大小', str(caught.exception))

    def test_replaced_object_is_rejected(self):
        """同长度但内容不同的对象在哈希比对这一步被拒绝。"""
        payload = self.bucket.objects[self.archive_key].payload
        self.bucket.tamper(self.archive_key, b'X' * len(payload))
        with self.assertRaises(backup.BackupError) as caught:
            backup.verify_remote_backup(self.bucket, self.record.object_prefix, self.cipher)
        self.assertIn('哈希', str(caught.exception))

    def test_missing_object_is_rejected(self):
        """对象不存在时给出可读的失败原因。"""
        del self.bucket.objects[self.archive_key]
        with self.assertRaises(backup.BackupError) as caught:
            backup.verify_remote_backup(self.bucket, self.record.object_prefix, self.cipher)
        self.assertIn('回读', str(caught.exception))


class RetentionTest(BackupServiceTestBase):
    """保留策略：最近 N 次成功备份永远优先于保留期。"""

    def _backup_records(self, count, age_days, status='succeeded'):
        """批量创建指定年龄的备份记录。

        参数：`count` 表示数量；`age_days` 表示创建时间的回溯天数；`status` 表示状态。
        返回：返回记录列表，按创建时间从新到旧。
        副作用：向数据库写入记录，并把创建时间改到过去。
        """
        records = []
        for index in range(count):
            record = self.make_backup_record(
                status=status,
                object_prefix=f'kubeasz/backups/1/bk-cluster/fake-{index}',
                backup_id=f'bk-old-{index}',
            )
            K8sClusterBackup.objects.filter(pk=record.pk).update(
                created_at=timezone.now() - timezone.timedelta(days=age_days),
            )
            record.refresh_from_db()
            records.append(record)
        return records

    def test_recent_backups_are_protected(self):
        """最近 N 次成功备份被标记为受保护，即使已经超过保留期。"""
        self._backup_records(2, age_days=90)
        removed = backup.apply_retention(self.cluster)
        self.assertEqual(removed, 0)
        self.assertEqual(
            K8sClusterBackup.objects.filter(cluster=self.cluster, is_protected=True).count(), 2,
        )
        self.assertEqual(
            K8sClusterBackup.objects.filter(cluster=self.cluster, status='succeeded').count(), 2,
        )

    def test_only_backups_beyond_both_thresholds_are_deleted(self):
        """只有「超过保留期」且「不属于最近 N 次」的备份才会被删除。"""
        for index in range(5):
            record = self.make_backup_record(
                status='succeeded',
                object_prefix=f'kubeasz/backups/1/bk-cluster/old-{index}',
                backup_id=f'bk-slot-{index}',
            )
            K8sClusterBackup.objects.filter(pk=record.pk).update(
                created_at=timezone.now() - timezone.timedelta(days=60 + index),
            )
        removed = backup.apply_retention(self.cluster)
        # 默认保留 3 份，5 份都过期，因此只有 2 份会被清掉。
        self.assertEqual(removed, 2)
        self.assertEqual(
            K8sClusterBackup.objects.filter(cluster=self.cluster, status='succeeded').count(), 3,
        )

    @override_settings(K8S_DEPLOY_BACKUP_RETENTION_DAYS=30)
    def test_fresh_backups_are_not_deleted(self):
        """未超过保留期的备份一律不删，哪怕它不在最近 N 次里。"""
        for index in range(6):
            self.make_backup_record(
                status='succeeded',
                object_prefix=f'kubeasz/backups/1/bk-cluster/new-{index}',
                backup_id=f'bk-fresh-{index}',
            )
        removed = backup.apply_retention(self.cluster)
        self.assertEqual(removed, 0)
        self.assertEqual(
            K8sClusterBackup.objects.filter(cluster=self.cluster, status='succeeded').count(), 6,
        )

    def test_lone_backup_is_never_evicted(self):
        """集群只有一份备份时，无论多旧都必须留着：否则保留策略会删掉唯一的退路。"""
        record = self.make_backup_record(
            status='succeeded',
            object_prefix='kubeasz/backups/1/bk-cluster/only',
            backup_id='bk-only',
        )
        K8sClusterBackup.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=3650),
        )
        self.assertEqual(backup.apply_retention(self.cluster), 0)
        record.refresh_from_db()
        self.assertTrue(record.is_protected)
        self.assertEqual(record.status, 'succeeded')

    def test_protection_flag_is_released_when_backup_falls_out(self):
        """旧的受保护标记要随着新备份出现而释放，不能永久占位。"""
        stale = self.make_backup_record(
            status='succeeded', is_protected=True,
            object_prefix='kubeasz/backups/1/bk-cluster/stale', backup_id='bk-stale',
        )
        for index in range(3):
            self.make_backup_record(
                status='succeeded',
                object_prefix=f'kubeasz/backups/1/bk-cluster/keeper-{index}',
                backup_id=f'bk-keeper-{index}',
            )
        backup.apply_retention(self.cluster)
        stale.refresh_from_db()
        self.assertFalse(stale.is_protected)
        self.assertEqual(
            K8sClusterBackup.objects.filter(cluster=self.cluster, is_protected=True).count(), 3,
        )

    def test_failed_backups_are_not_counted_as_retained(self):
        """失败记录不占保留名额，也不参与保护判定。"""
        self.make_backup_record(status='failed', backup_id='bk-broken')
        self.assertEqual(backup.apply_retention(self.cluster), 0)
        self.assertEqual(
            K8sClusterBackup.objects.filter(cluster=self.cluster, is_protected=True).count(), 0,
        )


class DeleteBackupTest(BackupServiceTestBase):
    """备份删除。"""

    def test_delete_removes_all_three_objects(self):
        """删除一份备份要连带清掉归档、指针与校验文件，不留孤儿对象。"""
        record = self.make_backup_record(object_prefix='kubeasz/backups/1/bk-cluster/x')
        for name in (backup.ARCHIVE_OBJECT_NAME, backup.POINTER_OBJECT_NAME, backup.CHECKSUMS_OBJECT_NAME):
            self.bucket.put_object(f'{record.object_prefix}/{name}', b'x')
        result = backup.delete_backup(record)
        self.assertEqual(result.status, 'deleted')
        self.assertEqual(self.bucket.objects, {})

    def test_delete_failure_is_recorded_for_retry(self):
        """OSS 删除失败时标记 delete_failed 并留下原因，供用户重试。"""
        record = self.make_backup_record(object_prefix='kubeasz/backups/1/bk-cluster/x')
        self.bucket.fail_on.add('delete')
        result = backup.delete_backup(record)
        self.assertEqual(result.status, 'delete_failed')
        self.assertIn('OSS', result.failure_reason)

    def test_delete_is_idempotent(self):
        """重复删除已删除的备份不报错也不改状态。"""
        record = self.make_backup_record(status='deleted')
        result = backup.delete_backup(record)
        self.assertEqual(result.status, 'deleted')


class RestoreSelectionTest(BackupServiceTestBase):
    """恢复选择校验：跨集群、跨版本、未成功的一律拒绝。"""

    def setUp(self):
        """准备两个集群与一条配置版本记录。

        参数：无。
        返回：无显式返回值。
        副作用：向数据库写入集群与配置版本。
        """
        super().setUp()
        self.add_revision()
        self.other = K8sCluster.objects.create(
            organization=self.org, name='另一个集群', slug='other-cluster', origin='deployed',
        )

    def test_same_cluster_backup_passes(self):
        """同集群同版本的备份可以用于恢复。"""
        record = self.make_backup_record()
        backup.validate_restore_selection(self.cluster, record)

    def test_backup_of_other_cluster_is_rejected(self):
        """别的集群的备份不能拿来恢复本集群。"""
        record = self.make_backup_record(cluster=self.other, slug='other-cluster')
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('不属于当前集群', str(caught.exception))

    def test_other_organization_backup_is_rejected(self):
        """组织不一致的备份一律拒绝，避免越权读取别人的数据。"""
        stranger = Organization.objects.create(name='别的组织', slug='stranger-org')
        record = self.make_backup_record(organization=stranger)
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('组织', str(caught.exception))

    def test_unsuccessful_backup_is_rejected(self):
        """只有成功的备份能用于恢复。"""
        record = self.make_backup_record(status='failed')
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('只有成功的备份', str(caught.exception))

    def test_cross_minor_version_is_rejected(self):
        """跨 Kubernetes 次版本的备份拒绝：etcd 数据与版本绑死。"""
        record = self.make_backup_record(kubernetes_version='1.28.9')
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('跨次版本', str(caught.exception))

    def test_feature_version_difference_is_allowed(self):
        """同一个次版本内的补丁差异允许恢复。"""
        record = self.make_backup_record(kubernetes_version='1.29.9')
        backup.validate_restore_selection(self.cluster, record)

    def test_cross_kubeasz_version_is_rejected(self):
        """kubeasz 版本不一致时拒绝恢复。"""
        record = self.make_backup_record(kubeasz_version='3.5.0')
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('kubeasz', str(caught.exception))

    def test_backup_without_object_prefix_is_rejected(self):
        """缺少 OSS 对象信息的备份无法校验，因此不能恢复。"""
        record = self.make_backup_record(object_prefix='', archive_sha256='')
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('OSS', str(caught.exception))

    def test_mismatched_slug_is_still_rejected(self):
        """两边都有标识而标识不同时照旧拒绝：那是备份串了集群。"""
        record = self.make_backup_record(slug='another-cluster')
        with self.assertRaises(backup.BackupError) as caught:
            backup.validate_restore_selection(self.cluster, record)
        self.assertIn('标识', str(caught.exception))

    def test_cleared_cluster_slug_does_not_fake_a_mismatch(self):
        """集群被清理后标识按设计清空，不能反过来把任何备份都判成「标识不一致」。

        报出来的那句话是「所选备份的集群标识为 bk-cluster，与当前集群  不一致」——
        两个空格之间什么都没有，用户只会去翻标识。该拦住这条路的说法是「集群已经清理过了」，
        由 ``cluster_ops.assert_deployed`` 给。
        """
        self.cluster.slug = ''
        self.cluster.status = 'destroyed'
        self.cluster.save(update_fields=['slug', 'status'])

        backup.validate_restore_selection(self.cluster, self.make_backup_record())


class DownloadBackupTest(BackupServiceTestBase):
    """下载、哈希比对与解压。"""

    def setUp(self):
        """准备一份已上传的备份。

        参数：无。
        返回：无显式返回值。
        副作用：执行一次备份流程。
        """
        super().setUp()
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        self.record = backup.create_backup(
            self.cluster,
            deployment=self.deployment,
            kubeasz_root=self.kubeasz_root,
            runner_instance=runner.FakeRunner(stage_delay=0.0),
            nodes=[],
        )
        self.target = self.state_root / 'restore'

    def test_download_round_trip_returns_manifest(self):
        """下载解密解压后能拿到 manifest 与集群配置。"""
        result = backup.download_backup(self.record, self.target)
        self.assertEqual(result['manifest']['cluster_slug'], 'bk-cluster')
        self.assertTrue((self.target / 'cluster' / 'hosts').is_file())
        self.assertTrue((self.target / backup.ARCHIVE_SNAPSHOT_PREFIX.rstrip('/')).is_dir())

    def test_download_leaves_no_ciphertext_or_archive(self):
        """下载目录里不留下密文与明文归档。"""
        backup.download_backup(self.record, self.target)
        self.assertFalse((self.target / backup.ARCHIVE_OBJECT_NAME).exists())
        self.assertFalse((self.target / backup.POINTER_OBJECT_NAME).exists())
        self.assertEqual(list(self.target.glob('*.zip')), [])

    def test_replaced_object_is_rejected(self):
        """OSS 上的对象被替换时下载阶段就要中止。"""
        key = f'{self.record.object_prefix}/{backup.ARCHIVE_OBJECT_NAME}'
        payload = self.bucket.objects[key].payload
        self.bucket.tamper(key, b'Y' * len(payload))
        with self.assertRaises(backup.BackupError) as caught:
            backup.download_backup(self.record, self.target)
        self.assertIn('已被替换', str(caught.exception))

    def test_pointer_of_other_cluster_is_rejected(self):
        """指针文件属于别的集群时中止，避免恢复成另一套配置。"""
        key = f'{self.record.object_prefix}/{backup.POINTER_OBJECT_NAME}'
        pointer = json.loads(self.bucket.objects[key].payload.decode('utf-8'))
        pointer['cluster_slug'] = 'another-cluster'
        self.bucket.tamper(key, json.dumps(pointer).encode('utf-8'))
        with self.assertRaises(backup.BackupError) as caught:
            backup.download_backup(self.record, self.target)
        self.assertIn('其它集群', str(caught.exception))

    def test_missing_pointer_is_rejected(self):
        """指针文件缺失时给出可读原因。"""
        del self.bucket.objects[f'{self.record.object_prefix}/{backup.POINTER_OBJECT_NAME}']
        with self.assertRaises(backup.BackupError) as caught:
            backup.download_backup(self.record, self.target)
        self.assertIn('指针', str(caught.exception))

    def test_extract_rejects_zip_slip(self):
        """包含向上穿越路径的归档必须被拒绝。"""
        evil = self.state_root / 'evil.zip'
        with zipfile.ZipFile(evil, 'w') as bundle:
            bundle.writestr(backup.ARCHIVE_MANIFEST_NAME, '{}')
            bundle.writestr('../../escaped.txt', 'nope')
        with self.assertRaises(backup.BackupError) as caught:
            backup._extract_archive(evil, self.state_root / 'unpack')
        self.assertIn('越界路径', str(caught.exception))

    def test_extract_rejects_archive_without_manifest(self):
        """缺少 manifest 的归档视为不完整。"""
        broken = self.state_root / 'broken.zip'
        with zipfile.ZipFile(broken, 'w') as bundle:
            bundle.writestr('cluster/hosts', 'x')
        with self.assertRaises(backup.BackupError) as caught:
            backup._extract_archive(broken, self.state_root / 'unpack2')
        self.assertIn('manifest.json', str(caught.exception))


class PlaceAndOverrideSnapshotTest(BackupServiceTestBase):
    """恢复时「用哪一份 snapshot」必须由平台显式决定。"""

    def test_place_snapshot_clears_previous_files(self):
        """放置选定 snapshot 前先清空目录，防止 kubeasz 的「取最近一份」选错。"""
        existing = kubeasz_fixtures.write_backup_snapshot(
            self.kubeasz_root, 'bk-cluster', names=('old-snapshot.db',),
        )
        self.assertTrue((existing / 'old-snapshot.db').is_file())

        extracted = self.state_root / 'extracted'
        source = extracted / 'snapshot'
        source.mkdir(parents=True)
        (source / 'chosen.db').write_bytes(b'chosen')
        placed = backup.place_snapshot(
            self.kubeasz_root, 'bk-cluster', extracted, {'files': []},
        )

        directory = paths.cluster_dir(self.kubeasz_root, 'bk-cluster') / backup.SNAPSHOT_DIR_NAME
        names = sorted(item.name for item in directory.iterdir())
        self.assertEqual(names, ['chosen.db'])
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]['sha256'], backup.sha256_file(directory / 'chosen.db'))

    def test_place_snapshot_without_source_dir_fails(self):
        """归档里没有 snapshot 目录时明确失败。"""
        empty = self.state_root / 'empty-extract'
        empty.mkdir(parents=True)
        with self.assertRaises(backup.BackupError) as caught:
            backup.place_snapshot(self.kubeasz_root, 'bk-cluster', empty, {'files': []})
        self.assertIn('没有 snapshot', str(caught.exception))

    def test_override_rewrites_restore_variable(self):
        """默认变量文件存在时，被改写成指向本次选定的文件。"""
        defaults = self.kubeasz_root.joinpath(*backup.RESTORE_DEFAULTS_PATH)
        paths.ensure_dir(defaults.parent)
        defaults.write_text(
            '---\n# 说明注释\nbackup_dir: "/tmp"\nrestore_snapshot_name: ""\n',
            encoding='utf-8',
        )
        target_file = self.state_root / 'chosen.snapshot.db'
        target_file.write_bytes(b'x')
        result = backup.override_restore_target(
            self.kubeasz_root, [{'path': str(target_file)}],
        )
        self.assertEqual(result['variable'], 'backup_dir')
        text = defaults.read_text(encoding='utf-8')
        self.assertIn(f'backup_dir: "{target_file}"', text)
        self.assertIn('# 说明注释', text, '注释必须原样保留')

    def test_override_fails_when_variable_is_absent(self):
        """找不到可覆盖的变量时必须失败：否则会落到 kubeasz 默认的「最近一份」行为。"""
        defaults = self.kubeasz_root.joinpath(*backup.RESTORE_DEFAULTS_PATH)
        paths.ensure_dir(defaults.parent)
        defaults.write_text('---\nsome_other_option: 1\n', encoding='utf-8')
        with self.assertRaises(backup.BackupError) as caught:
            backup.override_restore_target(self.kubeasz_root, [{'path': '/tmp/x'}])
        self.assertIn('没有可识别', str(caught.exception))

    def test_override_fails_when_file_is_missing(self):
        """kubeasz 源码结构不符合预期时失败，而不是静默跳过。"""
        with self.assertRaises(backup.BackupError) as caught:
            backup.override_restore_target(self.kubeasz_root, [{'path': '/tmp/x'}])
        self.assertIn('找不到', str(caught.exception))


class BackupSummaryTest(BackupServiceTestBase):
    """前端摘要结构。"""

    def test_summary_exposes_no_secrets(self):
        """摘要里不出现密钥、密文内容或明文口令。"""
        record = self.make_backup_record(
            object_prefix='kubeasz/backups/1/bk-cluster/x',
            failure_reason='上传失败：access_key_secret=leaked',
        )
        summary = backup.backup_summary(record)
        self.assertEqual(summary['backup_id'], record.backup_id)
        self.assertNotIn('archive.bin.enc', json.dumps(summary))

    def test_summary_carries_the_nodes_recorded_at_backup_time(self):
        """摘要带上备份时那一版配置里的节点，供恢复确认弹窗展示（规划 11.2）。"""
        self.add_revision(revision=1)
        record = self.make_backup_record(config_revision=1)

        summary = backup.backup_summary(record)

        self.assertEqual(
            summary['nodes'],
            [{'name': 'n1', 'ip_address': '192.168.20.10', 'roles': ['master']}],
        )
        self.assertEqual(summary['archive_sha256'], 'b' * 64)

    def test_summary_without_a_known_revision_has_no_nodes(self):
        """版本号为 0 或版本记录不存在时节点清单为空，不能抛错。"""
        for revision in (0, 99):
            with self.subTest(revision=revision):
                record = self.make_backup_record(backup_id=backup.new_backup_id(), config_revision=revision)
                self.assertEqual(backup.backup_summary(record)['nodes'], [])

    def test_summary_accepts_preloaded_nodes_to_avoid_per_record_queries(self):
        """列表场景传入预先查好的节点清单后不再逐条查库，避免 N+1。"""
        record = self.make_backup_record(config_revision=1)
        preloaded = [{'name': 'n1', 'ip_address': '192.168.20.10', 'roles': ['master']}]

        with self.assertNumQueries(0):
            summary = backup.backup_summary(record, nodes=preloaded)

        self.assertEqual(summary['nodes'], preloaded)

    def test_revision_snapshots_reads_several_revisions_at_once(self):
        """批量取快照按版本号建映射，缺的版本号不出现在结果里。"""
        self.add_revision(revision=1)
        self.add_revision(revision=3)

        snapshots = backup.revision_snapshots(self.cluster, [1, 3, 7, 0])

        self.assertEqual(sorted(snapshots), [1, 3])
        self.assertEqual(snapshots[1]['nodes'][0]['name'], 'n1')

    def test_revision_snapshots_without_revisions_returns_empty(self):
        """没有有效版本号时不查库，直接返回空映射。"""
        self.add_revision(revision=1)
        with self.assertNumQueries(0):
            self.assertEqual(backup.revision_snapshots(None, [1]), {})
        with self.assertNumQueries(0):
            self.assertEqual(backup.revision_snapshots(self.cluster, [0, None]), {})

    def test_oss_usage_aggregates_backup_objects(self):
        """OSS 用量只统计备份前缀下的对象。"""
        self.bucket.put_object('kubeasz/backups/1/bk-cluster/a/archive.bin.enc', b'12345')
        self.bucket.put_object('kubeasz/backups/1/bk-cluster/a/pointer.json', b'12')
        self.bucket.put_object('kubeasz/releases/3.6.7/source/x.zip', b'999999')
        usage = backup.oss_usage()
        self.assertEqual(usage['count'], 2)
        self.assertEqual(usage['bytes'], 7)

    def test_cleanup_stale_temp_removes_old_directories(self):
        """被强杀的 Worker 留下的备份临时目录会被兜底清理：明文不入库就必须不留盘。

        这里用 ``os.utime`` 把目录改成「两天前的」，而不是传 ``max_age_seconds=0``：
        Windows 上 ``time.time()`` 的粒度约 15 毫秒，刚建好的目录其 mtime 可能反而「晚于」
        随即取到的 now，判定会不稳定地漏掉本该清理的目录。
        """
        stale = paths.ensure_dir(backup.storage_root() / 'stale-run')
        (stale / 'leftover.zip').write_bytes(b'plaintext')
        two_days_ago = time.time() - 2 * 86400
        os.utime(stale, (two_days_ago, two_days_ago))
        self.assertEqual(backup.cleanup_stale_temp(max_age_seconds=86400), 1)
        self.assertFalse(stale.exists())

    def test_cleanup_stale_temp_keeps_fresh_directories(self):
        """仍在使用的临时目录不会被误删。"""
        fresh = paths.ensure_dir(backup.storage_root() / 'active-run')
        self.assertEqual(backup.cleanup_stale_temp(max_age_seconds=86400), 0)
        self.assertTrue(fresh.exists())


class _SnapshotRunner(runner.FakeRunner):
    """让 ``ezctl backup`` 真的产出快照文件，与 ``playbooks/94.backup.yml`` 一致。

    平台在归档之后会清掉盘上的快照（否则历史 etcd 快照会被一次次重新打包上传），所以
    「预先摆好文件、再调 create_backup」这种夹具在**连跑两次备份**的用例里就不成立了：
    第一次跑完文件已经没了。由命令自己产出才是真的 —— 真实 playbook 就是这么做的。
    """

    def __init__(self, kubeasz_root, slug, **kwargs):
        """初始化。

        参数：`kubeasz_root` 表示 kubeasz 源码根目录；`slug` 表示集群标识；
            `kwargs` 透传给 FakeRunner。
        返回：无显式返回值。
        副作用：只在本机对象上记录配置。
        """
        super().__init__(**kwargs)
        self.kubeasz_root = kubeasz_root
        self.slug = slug

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """跑完 ``ezctl backup`` 后写出快照文件。"""
        result = super().run_stage(
            stage, argv, cwd, env=env, on_line=on_line, timeout=timeout, is_cancelled=is_cancelled,
            on_idle=on_idle, on_tick=on_tick,
        )
        if result.ok and 'backup' in [str(item) for item in argv]:
            kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, self.slug)
        return result


class RestoreFlowTestBase(BackupServiceTestBase):
    """恢复流程用例的公共夹具：一份现成备份 + 一个待执行的恢复任务。"""

    def setUp(self):
        """准备集群、配置版本、一份备份与一个恢复任务。

        参数：无。
        返回：无显式返回值。
        副作用：执行一次备份并创建恢复任务。
        """
        super().setUp()
        from k8s_deployment.models import K8sDeployment, K8sDeploymentNode

        self.add_revision()
        kubeasz_fixtures.write_backup_snapshot(self.kubeasz_root, 'bk-cluster')
        self.record = backup.create_backup(
            self.cluster,
            deployment=self.deployment,
            kubeasz_root=self.kubeasz_root,
            runner_instance=runner.FakeRunner(stage_delay=0.0),
            nodes=[],
        )
        # 恢复流程要求角色默认文件存在，否则无法显式锁定 snapshot。
        defaults = self.kubeasz_root.joinpath(*backup.RESTORE_DEFAULTS_PATH)
        paths.ensure_dir(defaults.parent)
        defaults.write_text('---\nrestore_snapshot_name: ""\n', encoding='utf-8')

        self.deployment = K8sDeployment.objects.create(
            organization=self.org,
            cluster=self.cluster,
            task_type='restore',
            status='running',
            display_name='备份测试集群 · 备份恢复',
            slug='op-restore-1',
            kubernetes_version='1.29.4',
            kubeasz_version='3.6.7',
            runtime='containerd',
            os_type='Rocky Linux',
            os_version='8',
            topology_mode='ha',
            kube_proxy_mode='ipvs',
            network_interface='eth0',
            pod_cidr='10.244.0.0/16',
            service_cidr='10.96.0.0/12',
            cni_plugin='calico',
            execution_mode='simulated',
            expected_config_revision=1,
            request_snapshot={'submitted': {'backup_id': self.record.backup_id}},
        )
        self.node = K8sDeploymentNode.objects.create(
            organization=self.org,
            deployment=self.deployment,
            name='node-1',
            ip_address='192.168.20.10',
            ssh_port=22,
            ssh_user='root',
            roles=['etcd', 'master'],
        )
        self.context = self._make_context()

    def _make_context(self):
        """构造一个最小可用的任务上下文。

        参数：无。
        返回：返回 TaskContext 实例。
        副作用：创建任务工作区。
        """
        from k8s_deployment.services import state_machine

        workspace = paths.ensure_workspace(self.deployment.pk)
        return state_machine.TaskContext(self.deployment, workspace, worker_id='test')

    def _run(self, behaviors=None, preflight=None, source=None, runner_instance=None):
        """执行恢复流程，预检与集群健康探测被替换成固定结果。

        参数：`behaviors` 表示 FakeRunner 行为；`preflight` 表示预检返回值；
            `source` 表示集群健康探测结果；`runner_instance` 表示替换默认模拟执行器的实例。
        返回：返回恢复摘要。
        副作用：执行恢复主流程。

        健康探测会去连 Kubernetes API 与节点 SSH，属于「平台之外」，用例里一律替换掉。
        """
        result = preflight if preflight is not None else {'ok': True, 'checks': [], 'hard_failures': []}
        probe = source if source is not None else {
            'healthy': False, 'message': '探测结果：API Server 无法应答，将按不健康集群处理',
            'detail': {'api': {'ok': False, 'message': '连接失败'}},
        }
        with mock.patch.object(backup, '_run_preflight', return_value=result):
            with mock.patch.object(backup, 'assess_restore_source', return_value=probe):
                with mock.patch.object(
                    register, 'run_health_check',
                    return_value={'ok': True, 'message': '健康检查通过', 'detail': {}},
                ):
                    return backup.run_restore(
                        self.context, self.deployment, [self.node], {}, self.kubeasz_root,
                        runner_instance or _SnapshotRunner(
                            self.kubeasz_root, 'bk-cluster',
                            behaviors=behaviors, stage_delay=0.0,
                        ),
                    )

    def _events(self):
        """返回本次任务的事件，按事件类型索引。

        参数：无。
        返回：返回 ``{event_type: K8sDeploymentEvent}``。
        副作用：只读数据库，不修改数据。
        """
        from k8s_deployment.models import K8sDeploymentEvent

        return {
            item.event_type: item
            for item in K8sDeploymentEvent.objects.filter(deployment=self.deployment)
        }

    @staticmethod
    def _healthy_probe():
        """返回一份「集群健康」的探测结果。

        参数：无。
        返回：返回 ``assess_restore_source`` 形状的字典。
        副作用：纯计算，不修改任何状态。
        """
        return {
            'healthy': True, 'message': '探测结果：API Server 可应答，etcd 仲裁正常，集群当前健康',
            'detail': {'api': {'ok': True}, 'etcd': {'members': 3, 'healthy': 3}},
        }

    @staticmethod
    def _unhealthy_probe():
        """返回一份「集群不健康」的探测结果。

        参数：无。
        返回：返回 ``assess_restore_source`` 形状的字典。
        副作用：纯计算，不修改任何状态。
        """
        return {
            'healthy': False, 'message': '探测结果：API Server 无法应答，将按不健康集群处理',
            'detail': {'api': {'ok': False, 'message': '连接被拒绝'}},
        }

class RunRestoreFlowTest(RestoreFlowTestBase):
    """恢复主流程：必须严格使用任务里指定的那一份备份。"""

    def test_restore_uses_the_requested_backup(self):
        """恢复用的是任务里写死的那一份备份，并把它的 id 写回摘要。"""
        summary = self._run()
        self.assertEqual(summary['backup_id'], self.record.backup_id)

    def test_restore_locks_the_snapshot_variable(self):
        """执行前必须把角色默认变量改写成选定文件的真实路径。"""
        self._run()
        defaults = self.kubeasz_root.joinpath(*backup.RESTORE_DEFAULTS_PATH)
        text = defaults.read_text(encoding='utf-8')
        directory = paths.cluster_dir(self.kubeasz_root, 'bk-cluster') / backup.SNAPSHOT_DIR_NAME
        placed = list(directory.iterdir())
        self.assertEqual(len(placed), 1)
        self.assertIn(str(placed[0]), text)

    def test_restore_writes_backup_identity_into_events(self):
        """事件里要留下备份 id 与哈希，恢复出问题时这是对账依据。"""
        from k8s_deployment.models import K8sDeploymentEvent

        self._run()
        events = {
            item.event_type: item
            for item in K8sDeploymentEvent.objects.filter(deployment=self.deployment)
        }
        self.assertIn('restore_selected', events)
        self.assertIn('restore_target_locked', events)
        self.assertIn(self.record.backup_id, events['restore_selected'].message)

    def test_restore_without_backup_id_is_refused(self):
        """没有指定备份的恢复任务直接失败，绝不回落到「取最新一份」。"""
        self.deployment.request_snapshot = {'submitted': {}}
        self.deployment.save(update_fields=['request_snapshot', 'updated_at'])
        with self.assertRaises(backup.BackupError) as caught:
            self._run()
        self.assertIn('没有指定', str(caught.exception))

    def test_restore_with_unknown_backup_id_is_refused(self):
        """指定的备份不存在时给出明确原因，而不是换一份。"""
        self.deployment.request_snapshot = {'submitted': {'backup_id': 'bk-does-not-exist'}}
        self.deployment.save(update_fields=['request_snapshot', 'updated_at'])
        with self.assertRaises(backup.BackupError) as caught:
            self._run()
        self.assertIn('没有找到备份', str(caught.exception))

    def test_restore_failure_raises_state_machine_abort(self):
        """恢复失败必须抛状态机自己的 TaskAborted，否则会被错记成普通失败。"""
        from k8s_deployment.services import state_machine

        with self.assertRaises(state_machine.TaskAborted) as caught:
            self._run(behaviors={'restore': {'returncode': 3}})
        self.assertEqual(caught.exception.error_code, 'restore_failed')

    def test_restore_cancel_raises_cancelled(self):
        """恢复被取消时错误码为 cancelled，任务按取消而不是「需要回滚」收尾。"""
        from k8s_deployment.services import state_machine

        self.deployment.cancel_requested = True
        self.deployment.save(update_fields=['cancel_requested', 'updated_at'])
        with self.assertRaises(state_machine.TaskAborted) as caught:
            self._run(behaviors={'restore': {'hang': True}})
        self.assertEqual(caught.exception.error_code, 'cancelled')

    def test_restore_preflight_failure_does_not_block(self):
        """预检失败不阻断恢复：集群已经坏了，能恢复比环境干净更重要。"""
        failing = {
            'ok': False,
            'checks': [],
            'hard_failures': ['端口 2379 已被占用'],
        }
        summary = self._run(preflight=failing)
        self.assertEqual(summary['backup_id'], self.record.backup_id)

    def test_restore_appends_a_new_config_revision(self):
        """恢复成功后追加新的配置版本，记录「集群现在等于备份那一刻」。"""
        from k8s_cluster.models import K8sClusterConfigRevision

        before = register.current_revision(self.cluster)
        self._run()
        self.assertEqual(register.current_revision(self.cluster), before + 1)
        latest = K8sClusterConfigRevision.objects.filter(cluster=self.cluster).latest('revision')
        self.assertIn(self.record.backup_id, latest.note)


class _EnvRecordingRunner(_SnapshotRunner):
    """记录每个阶段拿到的环境变量，用来断言部署私钥有没有交给 ansible。"""

    def __init__(self, *args, **kwargs):
        """初始化。

        参数：`args`、`kwargs` 透传给 ``_SnapshotRunner``。
        返回：无显式返回值。
        副作用：在本机对象上多记一个 ``envs`` 字典。
        """
        super().__init__(*args, **kwargs)
        self.envs = {}

    def run_stage(self, stage, argv, cwd, env=None, **kwargs):
        """记下 ``env`` 后调用父类实现。"""
        self.envs[stage] = dict(env or {})
        return super().run_stage(stage, argv, cwd, env=env, **kwargs)


class RestoreAnsibleCredentialsTest(RestoreFlowTestBase):
    """恢复剧本靠 ansible 登录节点，平台必须把部署私钥显式交给它。

    2026-10-12 的「k8s-single · 备份恢复」失败（退出码 1，耗时 2.1 秒）就是这个原因：
    恢复剧本在 ``Gathering Facts`` 上 ``UNREACHABLE ... Permission denied
    (publickey,password)`` —— ansible 退到了平台机运维人员自己的 ``~/.ssh/id_rsa``，
    那把钥匙从来没下发到任何节点上。集群的 inventory 里没有、也不该有凭据（归档明确
    不带 SSH 凭据），所以 ``ANSIBLE_PRIVATE_KEY_FILE`` 是唯一的通道。
    """

    def test_restore_passes_the_platform_private_key_to_ansible(self):
        """恢复阶段必须拿到平台部署私钥与 KUBECONFIG。"""
        from k8s_deployment.services import state_machine

        key = Path(self.kubeasz_root) / 'deploy_ed25519'
        key.write_text('fake-key', encoding='utf-8')
        recorder = _EnvRecordingRunner(self.kubeasz_root, 'bk-cluster', stage_delay=0.0)

        with mock.patch.object(state_machine.credentials, 'private_key_path', return_value=key):
            self._run(runner_instance=recorder)

        env = recorder.envs['restore']
        self.assertEqual(env.get('ANSIBLE_PRIVATE_KEY_FILE'), str(key))
        self.assertIn('KUBECONFIG', env)
        self.assertEqual(env.get('ANSIBLE_HOST_KEY_CHECKING'), 'False')

    def test_backup_stage_gets_the_same_environment(self):
        """备份剧本与恢复共用同一处口径，不能一边有一边没有。

        备份剧本整本跑在 localhost 上，今天还用不到这把钥匙；但它与恢复是同一类调用
        （都不走 ``Pipeline.run_ezctl``），口径分叉就是下一次故障的起点。
        """
        recorder = _EnvRecordingRunner(self.kubeasz_root, 'bk-cluster', stage_delay=0.0)
        backup.create_backup(
            self.cluster,
            deployment=self.deployment,
            context=self.context,
            kubeasz_root=self.kubeasz_root,
            runner_instance=recorder,
            nodes=[],
        )

        self.assertIn('ANSIBLE_HOST_KEY_CHECKING', recorder.envs['backup'])


class RestoreConfigRollbackTest(RestoreFlowTestBase):
    """恢复失败时不能让集群目录里的 inventory 停在备份那一版。

    集群记录上的配置版本要等恢复跑成才推进，所以恢复失败后磁盘上的 hosts 与记录必须还是
    同一版。否则用户选的备份比集群现状旧（例如备份是在扩容之前做的）时，一次失败的恢复
    就把新节点从清单里抹掉了，而此后**备份会照着这份被抹掉的清单去归档**。
    """

    def test_failed_restore_rolls_the_cluster_config_back(self):
        """恢复失败必须走一次配置还原。"""
        from k8s_deployment.services import state_machine

        with mock.patch.object(backup, '_rollback_cluster_config') as rollback:
            with self.assertRaises(state_machine.TaskAborted):
                self._run(behaviors={'restore': {'returncode': 1}})

        rollback.assert_called_once()

    def test_rollback_rewrites_the_files_from_the_latest_revision(self):
        """还原用的是记录里最新那一版配置，不是任何暂存副本。"""
        from k8s_cluster.models import K8sClusterConfigRevision

        hosts_text = '[kube_master]\n192.168.20.10 ansible_host=10.0.0.10 k8s_nodename="master-01"\n'
        K8sClusterConfigRevision.objects.create(
            organization=self.org,
            cluster=self.cluster,
            revision=99,
            config_hash='b' * 64,
            snapshot={'kubeasz_version': '3.6.7', 'kubernetes_version': '1.29.4', 'hosts': hosts_text},
        )
        hosts_path = paths.hosts_path(self.kubeasz_root, self.cluster.slug)
        hosts_path.write_text('# 被恢复覆盖成备份那一版（比现状少一个节点）\n', encoding='utf-8')

        backup._rollback_cluster_config(self.context, self.cluster, self.kubeasz_root)

        self.assertEqual(hosts_path.read_text(encoding='utf-8'), hosts_text)


class RestoreEmergencyBackupTest(RestoreFlowTestBase):
    """恢复前的应急备份：默认做一次，但不阻断恢复（规划 11.1）。"""

    def test_restore_probes_source_before_touching_anything(self):
        """恢复前先探测源集群状态并写事件，用户能看到「跳过」意味着丢掉了什么。"""
        self._run(source=self._healthy_probe())
        probe = self._events()['restore_source_probe']
        self.assertEqual(probe.level, 'info')
        self.assertIn('健康', probe.message)

    def test_emergency_backup_runs_by_default(self):
        """没有勾选跳过时，恢复前先备份一次当前状态，事件里留下备份标识。"""
        from k8s_deployment.models import K8sClusterBackup

        before = K8sClusterBackup.objects.filter(cluster=self.cluster).count()
        summary = self._run(source=self._healthy_probe())
        self.assertEqual(summary['backup_id'], self.record.backup_id)
        self.assertEqual(K8sClusterBackup.objects.filter(cluster=self.cluster).count(), before + 1)
        event = self._events()['restore_emergency_backup']
        self.assertIn('集群探测为健康', event.message)
        self.assertTrue(event.detail['backup_id'])

    def test_emergency_backup_failure_does_not_block_restore(self):
        """应急备份失败只记警告：否则「集群坏了 → 备份失败 → 不能恢复」会形成死锁。"""
        with mock.patch.object(backup, 'create_backup', side_effect=backup.BackupError('磁盘写满')):
            summary = self._run(source=self._unhealthy_probe())
        self.assertEqual(summary['backup_id'], self.record.backup_id)
        events = self._events()
        self.assertNotIn('restore_emergency_backup', events)
        failed = events['restore_emergency_backup_failed']
        self.assertEqual(failed.level, 'warn')
        self.assertIn('磁盘写满', failed.message)
        self.assertIn('恢复将继续进行', failed.message)

    def test_skip_flag_avoids_the_emergency_backup(self):
        """勾选跳过后不再备份，并留下审计事件说明这是用户的选择。"""
        self.deployment.skip_emergency_backup = True
        self.deployment.save(update_fields=['skip_emergency_backup', 'updated_at'])
        with mock.patch.object(backup, 'create_backup') as creator:
            summary = self._run(source=self._unhealthy_probe())
        creator.assert_not_called()
        self.assertEqual(summary['backup_id'], self.record.backup_id)
        events = self._events()
        self.assertNotIn('restore_emergency_backup', events)
        skipped = events['restore_emergency_backup_skipped']
        self.assertEqual(skipped.level, 'warn')
        self.assertFalse(skipped.detail['cluster_healthy'])
        self.assertEqual(skipped.detail['reason'], '用户勾选「跳过应急备份」')
        # 集群已经不健康时不该再提示「先去做一次备份」，那只会误导用户。
        self.assertNotIn('单独发起一次备份任务', skipped.message)

    def test_skipping_a_healthy_cluster_still_says_how_to_keep_it(self):
        """集群还健康却要跳过时，事件里明确给出「想保留就另做一次备份」的出路。"""
        self.deployment.skip_emergency_backup = True
        self.deployment.save(update_fields=['skip_emergency_backup', 'updated_at'])
        self._run(source=self._healthy_probe())
        skipped = self._events()['restore_emergency_backup_skipped']
        self.assertTrue(skipped.detail['cluster_healthy'])
        self.assertIn('单独发起一次备份任务', skipped.message)


class _RecordingContext:
    """记录阶段与日志的假任务上下文，只实现备份流程用到的那几个方法。"""

    def __init__(self, budget=28800):
        """初始化。

        参数：`budget` 表示本阶段可用的秒数上限。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        self.budget = budget
        self.stages = []
        self.lines = []
        self.idle_ticks = []
        self.ticks = 0

    def begin_stage(self, key, title=''):
        """记录一次阶段开始。

        参数：`key` 表示阶段标识；`title` 表示阶段标题。
        返回：无显式返回值。
        副作用：记录调用。
        """
        self.stages.append(('begin', key, title))

    def complete_stage(self, key, elapsed=None, title=''):
        """记录一次阶段完成。

        参数：`key` 表示阶段标识；`elapsed` 表示耗时；`title` 表示阶段标题。
        返回：无显式返回值。
        副作用：记录调用。
        """
        self.stages.append(('complete', key, elapsed, title))

    def log(self, stream, text, stage=None):
        """记录一行日志。

        参数：`stream` 表示输出流；`text` 表示文本；`stage` 表示阶段标识。
        返回：无显式返回值。
        副作用：记录调用。
        """
        self.lines.append((stream, str(text)))

    def on_line(self, stream, text):
        """接收执行器的逐行输出。

        参数：`stream` 表示输出流；`text` 表示文本。
        返回：无显式返回值。
        副作用：记录调用。
        """
        self.lines.append((stream, str(text)))

    def stage_budget(self):
        """返回本阶段可用的秒数。

        参数：无。
        返回：返回构造时给的预算。
        副作用：纯计算。
        """
        return self.budget

    def narrate_idle(self, quiet_seconds):
        """记录一次静默旁白。

        参数：`quiet_seconds` 表示已经连续多少秒没有输出。
        返回：无显式返回值。
        副作用：记录调用。
        """
        self.idle_ticks.append(quiet_seconds)

    def tick(self):
        """记录一次空转回调。

        参数：无。
        返回：无显式返回值。
        副作用：记录调用。
        """
        self.ticks += 1

    @property
    def cancelled(self):
        """返回是否已取消。

        参数：无。
        返回：返回 False。
        副作用：纯计算。
        """
        return False

    def texts(self):
        """返回全部日志文本。

        参数：无。
        返回：返回文本列表。
        副作用：纯计算。
        """
        return [text for _, text in self.lines]


class _RecordedRunner(runner.FakeRunner):
    """记下每次 ``run_stage`` 的参数。"""

    def __init__(self, **kwargs):
        """初始化。

        参数：`kwargs` 透传给 ``FakeRunner``。
        返回：无显式返回值。
        副作用：只在本机对象上记录状态。
        """
        super().__init__(**kwargs)
        self.calls = []

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None, on_idle=None,
                  on_tick=None):
        """记录参数后照常模拟执行。

        参数：与 ``BaseRunner`` 一致。
        返回：返回 ``StageResult``。
        副作用：记录调用并 sleep 模拟耗时。
        """
        self.calls.append({
            'stage': stage, 'argv': [str(item) for item in argv], 'timeout': timeout,
            'on_idle': on_idle, 'on_tick': on_tick,
        })
        return super().run_stage(
            stage, argv, cwd, env=env, on_line=on_line, timeout=timeout, is_cancelled=is_cancelled,
            on_idle=on_idle, on_tick=on_tick,
        )


class EtcdReachabilityTest(BackupServiceTestBase):
    """备份前必须先确认平台机拨得到 etcd：拨不到就要当场失败，而不是让它等下去。

    现场：94 上的一次集群备份卡了三十多分钟，页面一直显示执行中，集群目录里只有一个
    0 字节的 ``.part``。原因是 ``playbooks/94.backup.yml`` 整本跑在 ``hosts: localhost``
    上，用 etcdctl 直接拨 ``[etcd]`` 段里那个平台机根本到不了的内网地址；而 etcdctl
    **没有默认超时**，于是既不报错也不结束。
    """

    def unreachable(self):
        """让「平台机拨得到吗」一律回答「拨不到」。

        参数：无。
        返回：返回已启动的 patch。
        副作用：替换 ``node_address.platform_reaches``。
        """
        patcher = mock.patch.object(node_address, 'platform_reaches', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def run_backup(self, context=None, runner_instance=None):
        """跑一次``ezctl backup``。

        参数：`context` 表示任务上下文，可为空；`runner_instance` 表示执行器，默认新建。
        返回：返回 ``(执行器, 异常或 None)``。
        副作用：执行备份命令。
        """
        runner_instance = runner_instance or _RecordedRunner(stage_delay=0.0)
        caught = None
        try:
            backup._run_cluster_backup_command(
                context, self.deployment, self.cluster, self.kubeasz_root, runner_instance,
            )
        except backup.BackupError as exc:
            caught = exc
        return runner_instance, caught

    def test_unreachable_etcd_fails_before_the_playbook_starts(self):
        """拨不通就在启动剧本之前失败：这正是「卡三十多分钟、页面还显示执行中」的修法。"""
        self.unreachable()
        recorder, caught = self.run_backup(context=_RecordingContext())
        self.assertIsNotNone(caught)
        self.assertEqual(recorder.calls, [], 'etcd 拨不通却仍然启动了剧本')

    def test_failure_names_the_address_the_port_and_the_silent_hang(self):
        """报错要能直接指导处置：哪个地址、哪个端口、为什么「连不上」表现为「卡住」。"""
        self.unreachable()
        _, caught = self.run_backup(context=_RecordingContext())
        text = str(caught)
        self.assertIn('192.168.1.1', text)
        self.assertIn('2379', text)
        self.assertIn('没有默认超时', text)
        self.assertIn('平台机', text)

    def test_without_a_context_an_unreachable_etcd_is_still_refused(self):
        """没有上下文的独立备份任务建不了通道，但「拨不通」这条一样要拦住 —— 放过去就是一次没有终点的等待。"""
        self.unreachable()
        recorder, caught = self.run_backup(context=None)
        self.assertIsNotNone(caught)
        self.assertEqual(recorder.calls, [])

    def test_a_reachable_etcd_lets_the_playbook_run(self):
        """拨得通时一切照旧（夹具已把可达性定为真），不额外接管地址。"""
        recorder, caught = self.run_backup(context=_RecordingContext())
        self.assertIsNone(caught)
        self.assertEqual([call['stage'] for call in recorder.calls], ['backup'])
        # ``ezctl_argv`` 拼出来的是 ``bash <kubeasz_root>/ezctl backup <slug>``。
        self.assertEqual(recorder.calls[0]['argv'][2:], ['backup', self.cluster.slug])

    def test_the_cluster_is_told_what_is_happening_while_etcd_is_silent(self):
        """取快照期间剧本可能长时间没有输出，事先说清楚免得用户以为又卡住了。"""
        context = _RecordingContext()
        self.run_backup(context=context)
        self.assertIn('backup', [item[1] for item in context.stages if item[0] == 'begin'])
        joined = '\n'.join(context.texts())
        self.assertIn('从 etcd 取快照', joined)
        self.assertIn('没有输出是正常的', joined)

    def test_silence_handling_is_wired_into_the_snapshot_stage(self):
        """取快照这一步必须挂上旁白与空转落库。

        现场：94 上的备份卡了半个多小时，进度页的最后一行停在缓冲区边界 —— 剧本的输出
        没有新行进来，日志缓冲与心跳就都不再动，看着像「没反应」。这两个回调是那时的
        全部依靠，所以在这里钉住：少传一个，用户就又开始看不出任务是不是还活着。
        """
        context = _RecordingContext()
        recorder, _ = self.run_backup(context=context)
        call = recorder.calls[0]
        self.assertTrue(callable(call['on_idle']))
        self.assertTrue(callable(call['on_tick']))
        self.assertEqual(call['on_idle'], context.narrate_idle)
        self.assertEqual(call['on_tick'], context.tick)
        # 两个回调确实会出力：旁白写一行，空转把缓冲落库。
        call['on_idle'](42)
        call['on_tick']()
        self.assertEqual(context.idle_ticks, [42])
        self.assertEqual(context.ticks, 1)

    def test_snapshot_stage_gets_its_own_ceiling(self):
        """整任务还有很久时，取快照这一步仍按自己的上限收口：卡住要有个头。"""
        context = _RecordingContext(budget=28800)
        recorder, _ = self.run_backup(context=context)
        self.assertEqual(recorder.calls[0]['timeout'], backup.BACKUP_SNAPSHOT_TIMEOUT)
        self.assertEqual(backup.BACKUP_SNAPSHOT_TIMEOUT, 1800)

    def test_a_shorter_stage_budget_wins(self):
        """整任务快没时间了就以它为准，不能反过来把任务拖过整任务时限。"""
        context = _RecordingContext(budget=60)
        recorder, _ = self.run_backup(context=context)
        self.assertEqual(recorder.calls[0]['timeout'], 60)

    def test_inventory_without_an_etcd_section_is_refused(self):
        """没有 etcd 段就不知道从哪取快照，与其猜不如明说。"""
        path = paths.cluster_dir(self.kubeasz_root, 'bk-cluster') / 'hosts'
        path.write_text('[kube_master]\n192.168.1.1\n', encoding='utf-8')
        _, caught = self.run_backup(context=_RecordingContext())
        self.assertIn('没有 etcd 节点', str(caught))

    def test_missing_inventory_is_refused(self):
        """inventory 读不到同样要当场失败：备份剧本正是按它去连 etcd 的。"""
        (paths.cluster_dir(self.kubeasz_root, 'bk-cluster') / 'hosts').unlink()
        _, caught = self.run_backup(context=_RecordingContext())
        self.assertIn('读不到集群的 inventory', str(caught))
