"""kubeasz 运行环境的准备：载体二进制与 ``clusters`` 入口。

``ezctl`` 把 ``BASE`` 写死成 ``/etc/kubeasz``，kubeasz 自带的 inventory 里 ``base_dir`` 也是它，
而平台绝不调用 ``ezdown``（建这个目录的正是那条命令）。于是这个目录只能由平台自己建：源码解压
进去、``bin/`` 用载体镜像铺好、``clusters`` 通向持久目录。三者缺一个，``01.prepare`` 就会以
一句 ``invalid dir:/etc/kubeasz`` 退出 1 —— 0.0 秒、看不出所以然，正是 192.168.1.94 上发生的事。

这里的载体用例沿用 ``test_registry`` 的替身（记录型执行器与上下文本就是同一批），只在必要处
补一个会把文件真写进挂载目录的执行器。
"""

import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from data_security.sm4 import SM4Error
from k8s_deployment.services import artifacts, paths, registry, runner

from .test_registry import _RecordingRunner, _StubContext, _manifest

STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8M3sD1gB5cE0uQ7iO9aS2dF6gH'

# 载体镜像的版本号就写在 ezdown 的这两行里，平台据它决定该装哪个 tag，不能再靠猜镜像名。
# 取值照抄 3.6.10 的真实 ezdown（第 19、21 行）。
CARRIER_EZDOWN = """#!/usr/bin/env bash
KUBEASZ_VER=3.6.10
K8S_BIN_VER=v1.35.6
EXT_BIN_VER=1.13.3
"""

# 少了 EXT_BIN_VER 的脚本：制品与源码对不上时必须在准备阶段就说清楚。
CARRIER_EZDOWN_NO_EXT = """#!/usr/bin/env bash
KUBEASZ_VER=3.6.10
K8S_BIN_VER=v1.35.6
"""

# 每个载体镜像在容器里的目录，以及它应当铺出哪些文件。
CARRIER_CONTENT = {
    '/k8s': ('kubelet', 'kubectl'),
    '/extra': ('etcdctl', 'crictl'),
}


def _carrier_manifest(k8s_platform='linux/amd64', ext_platform='linux/amd64', k8s_key=None):
    """造一份只含两个载体镜像的清单。

    参数：`k8s_platform`/`ext_platform` 表示两个载体在清单里标注的平台；
        `k8s_key` 表示 k8s 载体 tar 的对象键，传 None 表示用默认值。
    返回：返回清单字典。
    副作用：只做纯计算。
    """
    payload = _manifest()
    payload['image_archives'] = [
        {
            'image': 'easzlab/kubeasz-k8s-bin:v1.35.6',
            'key': 'kubeasz/releases/3.6.10/images/k8s-bin.tar' if k8s_key is None else k8s_key,
            'sha256': 'a' * 64,
            'platform': k8s_platform,
        },
        {
            'image': 'easzlab/kubeasz-ext-bin:1.13.3',
            'key': 'kubeasz/releases/3.6.10/images/ext-bin.tar',
            'sha256': 'b' * 64,
            'platform': ext_platform,
        },
    ]
    return payload


class _CarrierRunner(_RecordingRunner):
    """``docker run`` 成功时真的往挂载目录里写文件，模拟载体镜像展开。

    不这么做就只能断言「跑过一条命令」，而这条命令是否真的把 kubelet 铺到 ``{{ base_dir }}/bin``
    （整个改动的目的）反倒没被验证。
    """

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """记录命令，并在 ``docker run`` 成功时把该镜像应产出的文件写进挂载目录。"""
        result = super().run_stage(
            stage, argv, cwd, env=env, on_line=on_line, timeout=timeout, is_cancelled=is_cancelled,
            on_idle=on_idle, on_tick=on_tick,
        )
        if argv[:2] == ['docker', 'run'] and result.returncode == 0:
            # 从右边切：挂载是 "<宿主目录>:/tmp/out"，而宿主目录在 Windows 上是 "C:\..."。
            host = str(argv[argv.index('-v') + 1]).rsplit(':', 1)[0]
            script = str(argv[-1])
            for source, names in CARRIER_CONTENT.items():
                if source in script:
                    for name in names:
                        (Path(host) / name).write_bytes(b'carrier-binary')
        return result


class _MountDeniedRunner(_RecordingRunner):
    """挂载被拒（SELinux 强制模式）的执行器：``docker run`` 失败，``docker cp`` 成功。"""

    def run_stage(self, stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                  on_idle=None, on_tick=None):
        """``docker run`` 一律失败，``docker cp`` 则把文件写进目标目录。"""
        if argv[:2] == ['docker', 'run']:
            self.commands.append([str(item) for item in argv])
            return runner.StageResult(stage, returncode=1, error='permission denied')
        result = super().run_stage(
            stage, argv, cwd, env=env, on_line=on_line, timeout=timeout, is_cancelled=is_cancelled,
            on_idle=on_idle, on_tick=on_tick,
        )
        if argv[:2] == ['docker', 'cp']:
            source = str(argv[2]).split(':', 1)[1]
            for container_path, names in CARRIER_CONTENT.items():
                if source.startswith(container_path):
                    for name in names:
                        (Path(argv[-1]) / name).write_bytes(b'carrier-binary')
        return result


class InstallCarrierBinariesTest(TestCase):
    """``install_carrier_binaries``：把载体镜像里的二进制铺进 kubeasz 的 bin 目录。"""

    def setUp(self):
        """准备一棵只含 ezdown 的源码树与替身上下文。"""
        self.root = Path(tempfile.mkdtemp(prefix='carrier-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / 'ezdown').write_text(CARRIER_EZDOWN, encoding='utf-8')
        self.context = _StubContext(self.root)

    def _install(self, runner_instance=None, manifest=None, machine='x86_64', release='3.6.10'):
        """在打桩前提下跑一次载体准备。

        参数：`runner_instance` 表示执行器替身；`manifest` 表示制品清单；
            `machine` 表示本机架构；`release` 表示 kubeasz 版本号。
        返回：返回 ``(runner_instance, summary)``。
        副作用：向临时目录写入 bin 下的文件与标记。
        """
        runner_instance = runner_instance or _CarrierRunner()
        with mock.patch.object(artifacts, 'release_manifest', return_value=manifest or _carrier_manifest()), \
                mock.patch.object(artifacts, 'oss_bucket', return_value=object()), \
                mock.patch.object(
                    artifacts, 'fetch_artifact',
                    side_effect=lambda bucket, key, sha='', on_progress=None: {'path': self.root / Path(key).name},
                ), \
                mock.patch.object(registry.platform, 'machine', return_value=machine):
            summary = registry.install_carrier_binaries(self.context, runner_instance, self.root, release)
        return runner_instance, summary

    def test_command_mirrors_ezdown(self):
        """两条 docker run 必须与 ezdown 的 get_k8s_bin/get_ext_bin 逐字一致。

        产出要和 ``ezdown -D`` 完全一样才敢说「平台自己复刻了这一步」，所以连 ``cp -f``、
        ``/k8s/*`` 这种细节都按原样钉住。
        """
        runner_instance, summary = self._install()
        runs = runner_instance.subcommands('docker', 'run')

        self.assertEqual(runs, [
            ['docker', 'run', '--rm', '--platform', 'linux/amd64',
             '-v', f'{self.root / "bin"}:/tmp/out', 'easzlab/kubeasz-k8s-bin:v1.35.6',
             'sh', '-c', 'cp -f /k8s/* /tmp/out/'],
            ['docker', 'run', '--rm', '--platform', 'linux/amd64',
             '-v', f'{self.root / "bin"}:/tmp/out', 'easzlab/kubeasz-ext-bin:1.13.3',
             'sh', '-c', 'cp -rf /extra/* /tmp/out/'],
        ])
        self.assertEqual(summary['installed'], [
            'easzlab/kubeasz-k8s-bin:v1.35.6', 'easzlab/kubeasz-ext-bin:1.13.3',
        ])
        for name in ('kubelet', 'kubectl', 'etcdctl', 'crictl'):
            self.assertTrue((self.root / 'bin' / name).exists(), name)

    def test_versions_come_from_the_source_tree(self):
        """版本号取自源码树的 ezdown：猜镜像名会在换版本时静默装错。"""
        (self.root / 'ezdown').write_text(
            CARRIER_EZDOWN.replace('K8S_BIN_VER=v1.35.6', 'K8S_BIN_VER=v1.34.1')
            .replace('EXT_BIN_VER=1.13.3', 'EXT_BIN_VER=1.12.0'),
            encoding='utf-8',
        )
        manifest = _carrier_manifest()
        manifest['image_archives'] = [
            {'image': 'easzlab/kubeasz-k8s-bin:v1.34.1', 'key': 'k8s.tar',
             'sha256': 'a' * 64, 'platform': 'linux/amd64'},
            {'image': 'easzlab/kubeasz-ext-bin:1.12.0', 'key': 'ext.tar',
             'sha256': 'b' * 64, 'platform': 'linux/amd64'},
        ]
        runner_instance, _ = self._install(manifest=manifest)
        self.assertEqual(
            [item[7] for item in runner_instance.subcommands('docker', 'run')],
            ['easzlab/kubeasz-k8s-bin:v1.34.1', 'easzlab/kubeasz-ext-bin:1.12.0'],
        )

    def test_second_run_skips_every_docker_command(self):
        """装完再跑一次：标记命中就整段跳过，不 load、不解包，重跑部署不用再等十几分钟。"""
        self._install()
        runner_instance, summary = self._install()

        self.assertTrue(summary['skipped'])
        self.assertEqual(runner_instance.commands, [])
        self.assertTrue((self.root / registry.CARRIER_MARKER_NAME).exists())

    def test_ready_marker_without_files_is_rebuilt(self):
        """标记在而 bin 里的文件没了（换版本时整目录重解压）时必须重装，不能只看标记。"""
        self._install()
        for names in CARRIER_CONTENT.values():
            for name in names:
                (self.root / 'bin' / name).unlink()

        runner_instance, summary = self._install()
        self.assertFalse(summary['skipped'])
        self.assertTrue(runner_instance.subcommands('docker', 'run'))

    def test_declared_files_are_probed_after_extraction(self):
        """解出来没有 kubelet 就是制品不对：此时失败，而不是留一个空 bin 继续往下跑。"""
        with self.assertRaises(registry.RegistryError) as caught:
            self._install(runner_instance=_RecordingRunner())
        self.assertEqual(caught.exception.error_code, 'carrier_extract_failed')
        self.assertIn('kubelet', caught.exception.message)

    def test_missing_image_entry_is_reported(self):
        """清单里没有载体镜像时给出可执行的解法，而不是等到 01.prepare 报 invalid dir。"""
        manifest = _carrier_manifest()
        manifest['image_archives'] = manifest['image_archives'][1:]
        with self.assertRaises(registry.RegistryError) as caught:
            self._install(manifest=manifest)
        self.assertEqual(caught.exception.error_code, 'carrier_artifact_missing')
        self.assertIn('--include-images', caught.exception.message)

    def test_other_architecture_is_refused(self):
        """本机是 amd64 而清单里只有 arm64 时必须报错。

        跨架构的 tar 在装了 binfmt 的机器上一样能 ``docker load`` 成功，然后把 arm64 的
        kubelet 铺进 bin，直到节点上执行失败才暴露 —— 那时看起来像集群问题。
        """
        manifest = _carrier_manifest(k8s_platform='linux/arm64', ext_platform='linux/arm64')
        with self.assertRaises(registry.RegistryError) as caught:
            self._install(manifest=manifest)
        self.assertEqual(caught.exception.error_code, 'carrier_platform_unavailable')
        self.assertIn('linux/arm64', caught.exception.message)

    def test_platform_is_matched_exactly_when_both_are_present(self):
        """两种架构都在清单里时挑本机那一份，不能随手取第一个。"""
        manifest = _carrier_manifest()
        manifest['image_archives'].insert(0, {
            'image': 'easzlab/kubeasz-k8s-bin:v1.35.6', 'key': 'arm.tar',
            'sha256': 'c' * 64, 'platform': 'linux/arm64',
        })
        runner_instance, _ = self._install(manifest=manifest)
        loads = [Path(item[3]).name for item in runner_instance.subcommands('docker', 'load')]
        self.assertEqual(len(loads), 2)
        self.assertNotIn('arm.tar', loads)

    def test_unlabelled_single_entry_is_accepted(self):
        """早期清单没有 platform 字段，只有唯一候选时按它处理，免得老制品直接不可用。"""
        manifest = _carrier_manifest()
        for item in manifest['image_archives']:
            item.pop('platform')
        runner_instance, summary = self._install(manifest=manifest)
        self.assertEqual(len(summary['installed']), 2)
        self.assertEqual(len(runner_instance.subcommands('docker', 'run')), 2)

    def test_archive_without_object_key_is_reported(self):
        """清单里的载体镜像没有对应 tar 对象时说清是哪个镜像，而不是静默少装一半。"""
        with self.assertRaises(registry.RegistryError) as caught:
            self._install(manifest=_carrier_manifest(k8s_key=''))
        self.assertEqual(caught.exception.error_code, 'carrier_artifact_missing')
        self.assertIn('easzlab/kubeasz-k8s-bin:v1.35.6', caught.exception.message)

    def test_missing_version_variable_is_reported(self):
        """源码树的 ezdown 里没有 EXT_BIN_VER 时说明制品与源码对不上。"""
        (self.root / 'ezdown').write_text(CARRIER_EZDOWN_NO_EXT, encoding='utf-8')
        with self.assertRaises(registry.RegistryError) as caught:
            self._install()
        self.assertEqual(caught.exception.error_code, 'carrier_version_missing')
        self.assertIn('EXT_BIN_VER', caught.exception.message)

    def test_unknown_architecture_is_reported(self):
        """认不出本机架构时不能猜：猜错就是把另一种架构的二进制发给节点。"""
        with self.assertRaises(registry.RegistryError) as caught:
            self._install(machine='mips64')
        self.assertEqual(caught.exception.error_code, 'carrier_platform_unknown')

    def test_loading_is_skipped_when_the_image_is_already_local(self):
        """本机已有该镜像时跳过 docker load，但仍要解包到 bin。"""
        present = {'easzlab/kubeasz-k8s-bin:v1.35.6', 'easzlab/kubeasz-ext-bin:1.13.3'}
        runner_instance, _ = self._install(runner_instance=_CarrierRunner(present=present))
        self.assertEqual(runner_instance.subcommands('docker', 'load'), [])
        self.assertEqual(len(runner_instance.subcommands('docker', 'run')), 2)

    def test_docker_unavailable_is_reported(self):
        """本机没有 Docker 时给出可读原因：这一步没有别的替代路径。"""
        runner_instance = _RecordingRunner()

        def fail_docker(stage, argv, cwd, env=None, on_line=None, timeout=None, is_cancelled=None,
                        on_tick=None):
            runner_instance.commands.append([str(item) for item in argv])
            return runner.StageResult(stage, returncode=1, error='command not found')

        runner_instance.run_stage = fail_docker
        with self.assertRaises(registry.RegistryError) as caught:
            self._install(runner_instance=runner_instance)
        self.assertEqual(caught.exception.error_code, 'docker_unavailable')

    def test_mount_denied_falls_back_to_docker_cp(self):
        """SELinux 强制模式下挂载被拒时退回 docker create + docker cp：内容一样，只是不走挂载。"""
        runner_instance, summary = self._install(runner_instance=_MountDeniedRunner())
        copies = runner_instance.subcommands('docker', 'cp')

        self.assertEqual(len(summary['installed']), 2)
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0][2], f'{registry.CARRIER_CONTAINER_NAME}:/k8s/.')
        self.assertEqual(copies[1][2], f'{registry.CARRIER_CONTAINER_NAME}:/extra/.')
        # 每个载体各建一次；删两次是因为建之前也先清一次同名容器（照 ezdown 的写法）。
        self.assertEqual(len(runner_instance.subcommands('docker', 'create')), 2)
        self.assertEqual(len(runner_instance.subcommands('docker', 'rm', '-f')), 4)
        for name in ('kubelet', 'kubectl', 'etcdctl', 'crictl'):
            self.assertTrue((self.root / 'bin' / name).exists(), name)

    def test_oss_failure_gets_a_named_error_code(self):
        """取清单就失败（OSS 配置解不开、对象存储不通）时要给明确错误码。

        这一步是任务里第一次碰 OSS，失败原因必须原样带到任务详情页；漏出去只会变成
        ``unexpected_error``，运维看不出是哪里的事 —— 192.168.1.94 上就这么发生过一次。
        """
        with mock.patch.object(
            artifacts, 'release_manifest',
            side_effect=artifacts.ArtifactError('平台设置里的 OSS 配置无法解密：密钥对不上。'),
        ), mock.patch.object(registry.platform, 'machine', return_value='x86_64'):
            with self.assertRaises(registry.RegistryError) as caught:
                registry.install_carrier_binaries(self.context, _CarrierRunner(), self.root, '3.6.10')

        self.assertEqual(caught.exception.error_code, 'carrier_source_unavailable')
        self.assertIn('无法解密', caught.exception.message)


class _BrokenSetting:
    """``value`` 一读就抛 SM4 错：模拟密文与当前进程的密钥对不上。"""

    @property
    def value(self):
        """读取设置值。

        参数：无。
        返回：不返回，必定抛错。
        副作用：无。
        """
        raise SM4Error('SM4 ciphertext authentication failed')


class OssConfigTest(TestCase):
    """``oss_config``：把「解不开」翻译成能照着做的说明。"""

    def test_unreadable_ciphertext_names_the_cause(self):
        """原始异常只有一句 SM4 报错，看不出该改哪里；必须换成一句可执行的说明。"""
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = _BrokenSetting()
        with mock.patch('ops.models.SystemSetting.objects', manager):
            with self.assertRaises(artifacts.ArtifactError) as caught:
                artifacts.oss_config()

        message = str(caught.exception)
        self.assertIn('无法解密', message)
        self.assertIn('重新保存', message)


class EnsureKubeaszSourceTest(TestCase):
    """``ensure_kubeasz_source``：共用安装目录里的版本标记与 ``clusters`` 入口。"""

    def setUp(self):
        """准备临时状态根与一份合成的源码包。"""
        self.state_root = Path(tempfile.mkdtemp(prefix='kubeasz-home-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(
            SECRET_KEY=STRONG_KEY,
            K8S_DEPLOY_STATE_ROOT=str(self.state_root),
            K8S_DEPLOY_RUN_ROOT=str(self.state_root / 'runs'),
            K8S_DEPLOY_KUBEASZ_ROOT=str(self.state_root / 'kubeasz'),
            K8S_DEPLOY_CLUSTERS_ROOT=str(self.state_root / 'clusters'),
        )
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.archive = self._make_archive('3.6.10')

    def _make_archive(self, version):
        """造一个结构与真实源码包一致的 zip（顶层带 kubeasz-<版本> 目录）。

        参数：`version` 表示 kubeasz 版本号。
        返回：返回 zip 文件路径。
        副作用：在临时目录里写出一个 zip 文件。
        """
        archive = self.state_root / f'kubeasz-{version}.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for name in artifacts.REQUIRED_SOURCE_FILES:
                bundle.writestr(f'kubeasz-{version}/{name}', f'{version}:{name}\n')
        return archive

    def _ensure(self, version='3.6.10', deployment=None):
        """在打桩前提下跑一次源码准备。

        参数：`version` 表示 kubeasz 版本号；`deployment` 表示部署任务替身。
        返回：返回安装目录 Path。
        副作用：下载缓存由打桩替换，解压会真的写盘。
        """
        from types import SimpleNamespace

        deployment = deployment or SimpleNamespace(pk=1)
        archive = self.archive if version == '3.6.10' else self._make_archive(version)
        fetched = {'path': archive, 'sha256': 'd' * 64, 'size': archive.stat().st_size, 'cache_hit': False}
        with mock.patch.object(artifacts, 'oss_bucket', return_value=object()), \
                mock.patch.object(artifacts, 'release_manifest', return_value={}), \
                mock.patch.object(artifacts, 'oss_config', return_value={'base_dir': 'kubeasz'}), \
                mock.patch.object(artifacts, '_download_to_cache', return_value=fetched), \
                mock.patch.object(artifacts, 'record_artifact_use'):
            return artifacts.ensure_kubeasz_source(version, deployment=deployment)

    def test_source_lands_in_the_shared_root(self):
        """源码解到 ``K8S_DEPLOY_KUBEASZ_ROOT`` 本身：ezctl 与 inventory 的 base_dir 就是这里。"""
        root = self._ensure()
        self.assertEqual(root, paths.kubeasz_root())
        self.assertTrue((root / 'ezctl').exists())
        self.assertTrue((root / 'example' / 'hosts.allinone').exists())
        self.assertTrue((root / artifacts.SOURCE_MARKER_NAME).exists())

    def test_same_version_is_not_downloaded_twice(self):
        """标记版本一致且结构完整时直接返回，不重新解压。"""
        root = self._ensure()
        (root / 'ezctl').write_text('platform-owned\n', encoding='utf-8')
        self._ensure()
        self.assertEqual((root / 'ezctl').read_text(encoding='utf-8'), 'platform-owned\n')

    def test_other_version_replaces_the_tree(self):
        """换版本时重解压：上一版留下的树结构同样完整，光看结构会拿它去跑新版本。"""
        root = self._ensure()
        (root / 'example' / 'hosts.allinone').write_text('3.6.10-残留\n', encoding='utf-8')
        self._ensure(version='3.6.11')

        marker = artifacts.read_marker(root / artifacts.SOURCE_MARKER_NAME)
        self.assertEqual(marker['release'], '3.6.11')
        self.assertIn('3.6.11', (root / 'example' / 'hosts.allinone').read_text(encoding='utf-8'))

    def test_cluster_data_survives_a_reextract(self):
        """重解压后集群证书原封不动 —— 这是把 clusters 挪出源码树换来的东西。

        丢了这份 CA，扩容与升级会重新签发一套，在跑的节点立刻验证不过；而工作区随时会被
        TTL 清理，所以它必须落在源码树之外。

        Linux 上靠软链（``<安装目录>/clusters`` → 持久目录），Windows 开发机没有软链权限，
        集群目录就落在安装目录下、由重解压时的保留名单挡住 —— 两条路的结论都是「证书活着」。
        """
        root = self._ensure()
        cluster = paths.cluster_dir(root, 'prod-hz-01')
        paths.ensure_dir(cluster / 'certs')
        (cluster / 'certs' / 'ca.crt').write_text('CA-CONTENT\n', encoding='utf-8')
        (cluster / 'certs' / 'ca.key').write_text('CA-KEY\n', encoding='utf-8')
        (cluster / 'hosts').write_text('[etcd]\n192.168.1.1\n', encoding='utf-8')
        if os.name != 'nt':
            # POSIX 上还要确认数据真的落在持久目录里，而不是只有一份藏在源码树里的副本。
            self.assertTrue((paths.clusters_root() / 'prod-hz-01' / 'certs' / 'ca.crt').exists())
            self.assertTrue((root / artifacts.CLUSTERS_LINK_NAME).is_symlink())

        self._ensure(version='3.6.11')

        self.assertEqual((cluster / 'certs' / 'ca.crt').read_text(encoding='utf-8'), 'CA-CONTENT\n')
        self.assertEqual((cluster / 'certs' / 'ca.key').read_text(encoding='utf-8'), 'CA-KEY\n')
        self.assertIn('192.168.1.1', (cluster / 'hosts').read_text(encoding='utf-8'))

    def test_foreign_installation_is_never_overwritten(self):
        """运维手工 ``ezdown -D`` 装出来的目录有 ezctl 但没平台标记：停下来让人确认，不覆盖。"""
        root = paths.kubeasz_root()
        paths.ensure_dir(root)
        (root / 'ezctl').write_text('运维自己的\n', encoding='utf-8')

        with self.assertRaises(artifacts.ArtifactError) as caught:
            self._ensure()
        self.assertIn('不是平台安装', str(caught.exception))
        self.assertEqual((root / 'ezctl').read_text(encoding='utf-8'), '运维自己的\n')

    def test_preview_mode_leaves_the_shared_root_untouched(self):
        """深度预检给的是独立目录：预览绝不能写进正式目录去覆盖在跑集群的配置。"""
        from types import SimpleNamespace

        preview = self.state_root / 'dryrun' / 'deployment-1'
        fetched = {'path': self.archive, 'sha256': 'd' * 64,
                   'size': self.archive.stat().st_size, 'cache_hit': False}
        with mock.patch.object(artifacts, 'oss_bucket', return_value=object()), \
                mock.patch.object(artifacts, 'release_manifest', return_value={}), \
                mock.patch.object(artifacts, 'oss_config', return_value={'base_dir': 'kubeasz'}), \
                mock.patch.object(artifacts, '_download_to_cache', return_value=fetched), \
                mock.patch.object(artifacts, 'record_artifact_use'):
            root = artifacts.ensure_kubeasz_source(
                '3.6.10', deployment=SimpleNamespace(pk=1), root=preview,
            )

        self.assertEqual(root, preview)
        self.assertTrue((preview / 'ezctl').exists())
        self.assertFalse(paths.kubeasz_root().exists())
        self.assertFalse((preview / artifacts.SOURCE_MARKER_NAME).exists())
