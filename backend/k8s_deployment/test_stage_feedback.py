"""执行期的「还在跑」反馈：静默提示与下载进度旁白。

这两种反馈回答的是同一个问题 —— **进度页为什么半天不动**。ansible 装包、拉镜像、等远端接口
时可能连着几分钟一个字都不吐，而大制品下载（几百 MB 到 1 GB）期间同样没有输出；没有这两条
提示，用户看到的就是一个停在原地的界面，只能自己猜是不是卡死了（192.168.1.94 上第一次部署
就是这样）。这里钉住执行器与下载两个位置。

真正的「写进日志表」由 ``state_machine`` 的旁白用例覆盖，这里只钉住触发时机与文案拼装。
"""

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from k8s_deployment.services import artifacts, runner


class IdleNoticeRunner(SimpleTestCase):
    """``NativeKubeaszRunner.run_stage`` 的 ``on_idle`` 回调。"""

    def setUp(self):
        """准备一个临时工作目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录，用例结束时删除。
        """
        self.workspace = Path(tempfile.mkdtemp(prefix='k8s-idle-'))
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def run_quiet(self, sleep_seconds, notice_seconds=0.2):
        """跑一个「只睡不输出」的命令，收集静默提示。

        参数：`sleep_seconds` 表示命令持续多久；`notice_seconds` 表示判定静默的阈值。
        返回：返回 ``(StageResult, [提示的静默秒数])``。
        副作用：启动一个真实的 Python 子进程。

        ``IDLE_NOTICE_SECONDS`` 按生产值是 30 秒，用例里改成 0.2 秒；读取循环每
        ``POLL_INTERVAL_SECONDS``（0.5 秒）醒一次，因此 1.2 秒的静默足以观察到多次提示。
        """
        quiet = []
        with mock.patch.object(runner, 'IDLE_NOTICE_SECONDS', notice_seconds):
            result = runner.NativeKubeaszRunner().run_stage(
                '01.prepare',
                [sys.executable, '-c', f'import time; time.sleep({sleep_seconds})'],
                cwd=str(self.workspace),
                on_idle=quiet.append,
            )
        return result, quiet

    def test_quiet_stage_gets_repeated_notices(self):
        """没有输出时反复提示，而不是等整个阶段超时才让人知道还在跑。"""
        result, quiet = self.run_quiet(1.2)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertGreaterEqual(len(quiet), 2)
        self.assertTrue(all(item >= 0.2 for item in quiet))

    def test_a_stage_that_prints_is_not_narrated(self):
        """一上来就有输出的阶段不该被判成静默 —— 提示只在真的没动静时出现。"""
        result = runner.NativeKubeaszRunner().run_stage(
            '01.prepare',
            [sys.executable, '-c', 'print("有输出")'],
            cwd=str(self.workspace),
            on_idle=lambda seconds: self.fail('有输出时不该触发静默提示'),
        )
        self.assertEqual(result.returncode, 0)

    def test_a_broken_notice_callback_does_not_break_the_stage(self):
        """提示回调出错只丢一行日志，绝不能把正在跑的部署带崩。"""
        def explode(seconds):
            """故意抛异常，模拟回调自身出错。

            参数：`seconds` 表示静默秒数。
            返回：无返回值，总是抛异常。
            副作用：抛 ``RuntimeError``。
            """
            raise RuntimeError('提示写不出去')

        with mock.patch.object(runner, 'IDLE_NOTICE_SECONDS', 0.2):
            result = runner.NativeKubeaszRunner().run_stage(
                '01.prepare',
                [sys.executable, '-c', 'import time; time.sleep(0.8)'],
                cwd=str(self.workspace),
                on_idle=explode,
            )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.error)

    def test_the_fake_runner_accepts_the_callback(self):
        """模拟执行器收下这个参数即可（它不会触发），调用方不必按执行器分支。"""
        result = runner.FakeRunner(stage_delay=0).run_stage(
            '01.prepare', ['ezctl', 'start'], cwd=str(self.workspace),
            on_idle=lambda seconds: self.fail('模拟执行不触发静默提示'),
        )
        self.assertEqual(result.returncode, 0)


class StageTickRunner(SimpleTestCase):
    """``NativeKubeaszRunner.run_stage`` 的 ``on_tick`` 回调。

    它回答的是另一个问题：远端沉默期间，平台自己还得照常干活。日志缓冲与心跳都由
    「有新输出」驱动，一条永远不再输出的命令会让两者一起停摆 —— 现场就是 94 上一次
    备份卡住之后，进度页的最后一行停在缓冲区边界、心跳也不再更新。这里钉住「空转期间
    照样被调到」，并且不挤掉静默旁白（两者节奏不同，不能二选一）。
    """

    def setUp(self):
        """准备一个临时工作目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录，用例结束时删除。
        """
        self.workspace = Path(tempfile.mkdtemp(prefix='k8s-tick-'))
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def test_quiet_stage_keeps_ticking(self):
        """命令只睡不吐字时，平台仍被反复叫醒去落库与刷心跳。"""
        ticks = []
        result = runner.NativeKubeaszRunner().run_stage(
            'backup',
            [sys.executable, '-c', 'import time; time.sleep(1.2)'],
            cwd=str(self.workspace),
            on_tick=lambda: ticks.append(1),
        )
        self.assertEqual(result.returncode, 0)
        # 读取循环每 0.5 秒醒一次，1.2 秒的静默足够醒来两次以上。
        self.assertGreaterEqual(len(ticks), 2)

    def test_a_broken_tick_callback_does_not_break_the_stage(self):
        """空转回调出错只少落一次库，绝不能把正在跑的部署带崩。"""
        def explode():
            """故意抛异常，模拟回调自身出错。

            参数：无。
            返回：无返回值，总是抛异常。
            副作用：抛 ``RuntimeError``。
            """
            raise RuntimeError('落库失败')

        result = runner.NativeKubeaszRunner().run_stage(
            'backup',
            [sys.executable, '-c', 'import time; time.sleep(0.8)'],
            cwd=str(self.workspace),
            on_tick=explode,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.error)

    def test_ticking_does_not_swallow_the_idle_notice(self):
        """两个回调各管各的：挂上空转不能把旁白顶掉。"""
        ticks = []
        quiet = []
        with mock.patch.object(runner, 'IDLE_NOTICE_SECONDS', 0.2):
            runner.NativeKubeaszRunner().run_stage(
                'backup',
                [sys.executable, '-c', 'import time; time.sleep(1.2)'],
                cwd=str(self.workspace),
                on_tick=lambda: ticks.append(1),
                on_idle=quiet.append,
            )
        self.assertTrue(ticks)
        self.assertGreaterEqual(len(quiet), 2)

    def test_the_fake_runner_accepts_the_callback(self):
        """模拟执行器收下这个参数即可（它不会触发），调用方不必按执行器分支。"""
        result = runner.FakeRunner(stage_delay=0).run_stage(
            'backup', ['ezctl', 'backup', 'demo'], cwd=str(self.workspace),
            on_tick=lambda: self.fail('模拟执行不触发空转回调'),
        )
        self.assertEqual(result.returncode, 0)


class FakeStream:
    """``get_object`` 返回的可读流，可带也可以不带 ``Content-Length``。"""

    def __init__(self, payload, content_length=None):
        """记录要返回的字节与声明的长度。

        参数：`payload` 表示对象字节；`content_length` 表示对外声明的总长度，None 表示不声明。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self._payload = payload
        self._offset = 0
        if content_length is not None:
            self.content_length = content_length

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
    """只实现 ``get_object`` 的内存版 OSS Bucket。"""

    def __init__(self, payload, content_length=None):
        """记录对象内容。

        参数：`payload` 表示对象字节；`content_length` 表示对外声明的总长度。
        返回：无显式返回值。
        副作用：只在本机对象上记录字段。
        """
        self.payload = payload
        self.content_length = content_length

    def get_object(self, key):
        """返回可读流，键名不参与判断。

        参数：`key` 表示对象键。
        返回：返回 ``FakeStream``。
        副作用：无。
        """
        return FakeStream(self.payload, self.content_length)


class DownloadProgressTest(SimpleTestCase):
    """``_download_to_cache`` 的进度回调。"""

    def setUp(self):
        """准备一个临时制品缓存目录。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录并临时改写制品缓存根目录。
        """
        self.root = Path(tempfile.mkdtemp(prefix='k8s-download-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        overrides = override_settings(K8S_DEPLOY_ARTIFACT_ROOT=str(self.root))
        overrides.enable()
        self.addCleanup(overrides.disable)
        # 3 个分块，配合「阈值为 0」正好能观察到多次回调。
        self.payload = b'x' * (artifacts.CHUNK_SIZE * 3)

    def test_progress_is_reported_while_downloading(self):
        """下载过程中按间隔报进度，报告的总量取自对象声明的长度。"""
        seen = []
        with mock.patch.object(artifacts, 'DOWNLOAD_PROGRESS_SECONDS', 0):
            result = artifacts._download_to_cache(
                FakeBucket(self.payload, len(self.payload)), 'kubeasz/3.6.7/source.zip',
                '', lambda received, total: seen.append((received, total)),
            )

        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[-1][0], len(self.payload))
        self.assertTrue(all(total == len(self.payload) for _received, total in seen))
        self.assertEqual(result['size'], len(self.payload))

    def test_progress_survives_a_bucket_without_a_declared_length(self):
        """拿不到总长度时只报已下载量，不能因此算出 None 而报错。"""
        seen = []
        with mock.patch.object(artifacts, 'DOWNLOAD_PROGRESS_SECONDS', 0):
            artifacts._download_to_cache(
                FakeBucket(self.payload), 'kubeasz/3.6.7/source.zip',
                '', lambda received, total: seen.append((received, total)),
            )

        self.assertTrue(seen)
        self.assertTrue(all(total == 0 for _received, total in seen))

    def test_a_broken_progress_callback_does_not_break_the_download(self):
        """回调抛异常时下载照常完成并落进内容寻址缓存。"""
        def explode(received, total):
            """故意抛异常，模拟日志写入失败。

            参数：`received` 表示已下载字节数；`total` 表示总字节数。
            返回：无返回值，总是抛异常。
            副作用：抛 ``RuntimeError``。
            """
            raise RuntimeError('日志写不进去')

        with mock.patch.object(artifacts, 'DOWNLOAD_PROGRESS_SECONDS', 0):
            result = artifacts._download_to_cache(
                FakeBucket(self.payload, len(self.payload)), 'kubeasz/3.6.7/source.zip', '', explode,
            )

        self.assertEqual(result['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertTrue(result['path'].exists())
        self.assertEqual(result['path'].read_bytes(), self.payload)


class DownloadProgressMessageTest(SimpleTestCase):
    """``download_progress`` 拼出来的旁白文案与容量写法。"""

    def test_message_names_the_artifact(self):
        """有总长度时报「已下载 X / Y」，并带上正在下的是什么。"""
        said = []
        context = mock.Mock()
        context.say = said.append
        artifacts.download_progress(context, '镜像 calico/node:v3.26.1')(1024 * 1024, 2 * 1024 * 1024)

        self.assertEqual(said, ['镜像 calico/node:v3.26.1：已下载 1.0 MB / 2.0 MB'])

    def test_message_without_a_total_only_reports_what_arrived(self):
        """拿不到总长度时不能编一个分母出来。"""
        said = []
        context = mock.Mock()
        context.say = said.append
        artifacts.download_progress(context, 'kubeasz 源码包')(2 * 1024 ** 3, 0)

        self.assertEqual(said, ['kubeasz 源码包：已下载 2.00 GB'])

    def test_sizes_are_readable(self):
        """容量按 B/KB/MB/GB 分档，界面上不该出现一长串字节数。"""
        self.assertEqual(artifacts.human_size(0), '0 B')
        self.assertEqual(artifacts.human_size(2048), '2 KB')
        self.assertEqual(artifacts.human_size(5 * 1024 * 1024), '5.0 MB')
        self.assertEqual(artifacts.human_size(3 * 1024 ** 3), '3.00 GB')
        self.assertEqual(artifacts.human_size(None), '0 B')
        self.assertEqual(artifacts.human_size('不是数字'), '大小未知')
