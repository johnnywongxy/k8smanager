"""kubeasz 源码包补丁的落盘行为：行尾、幂等、只动登记过的版本。

线上事故（2026-09-20 清点 OSS 制品时发现）：``kubeasz/releases/3.6.7/commands/ezdown``
被写成了 CRLF，首行是 ``#!/bin/bash\\r`` —— 在 Linux 上直接 ``bad interpreter``，脚本没法执行。
原因是 ``sync_kubeasz_artifacts._patch_ezdown`` 用了 ``Path.write_text``，Windows 上会把 ``\\n``
翻译成 ``\\r\\n``；而只有登记了补丁的 3.6.7 会走到那次文本写回，所以只有它中招（压缩包内的副本
走字节级改写，一直是好的）。

这一组用例钉住三件事：写出去的字节里不能有 ``\\r``；重复打补丁不能把三处改动各插两遍；
没登记补丁的版本一个字节都不许动。补丁的功能部分（版本变量、镜像分支）也一并验证 ——
``ingress_nginx_ver`` 靠前者填充，能力判定靠后者识别 ingress 镜像。
"""

import tempfile
from pathlib import Path

from django.test import TestCase

from ops import kubeasz_patches
from ops.management.commands.sync_kubeasz_artifacts import parse_ezdown


def _ezdown_fixture():
    """逐行拼出 3.6.7 ezdown 关键片段的形状。

    参数：无。
    返回：返回脚本文本（LF 行尾）。

    照上游 3.6.7 的真实结构与缩进写：版本赋值行在行首；usage 文本是 heredoc 里的裸行
    （不是 `echo "..."`）；镜像分支是 4 空格缩进的 `name)`，分支体里 `IMAGES=(` 行尾带
    续行反斜杠，两个分支之间有空行 —— 补丁的锚点与 ``parse_ezdown`` 的解析正则都建立
    在这些形状上，fixture 歪一点测的就不是真实路径了。

    逐行拼装而不是写成一个三引号字符串：三引号里行尾的 ``\\`` 会被 Python 当成续行、
    把换行吞掉，那样拼出来的脚本跟上游不是一回事，且很难看出来。
    """
    return '\n'.join([
        '#!/bin/bash',
        '',
        'flannelVer=v0.26.0',
        'metricsVer=v0.7.2',
        'kubeblocksVer=0.9.3',
        '',
        'function usage-down-ext-img(){',
        '  cat <<EOF',
        'available options:',
        '    flannel                  to download images of flannel',
        '    kubeblocks               to download images of kubeblocks',
        'EOF',
        '}',
        '',
        'function get_extra_images() {',
        '  case "$1" in',
        '    flannel)',
        '      IMAGES=(\\',
        '          "flannel/flannel:$flannelVer" \\',
        '        )',
        '      down_and_save_images flannel',
        '      ;;',
        '',
        '    kubeblocks)',
        '      IMAGES=(\\',
        '          "kubeblocks/kubeblocks:v0.9.3" \\',
        '        )',
        '      down_and_save_images kubeblocks',
        '      ;;',
        '  esac',
        '}',
        '',
    ])


EZDOWN_36X = _ezdown_fixture()


class PatchEzdownTest(TestCase):
    """补丁本身：幂等、内容齐备、锚点没命中要报错。"""

    def test_the_three_changes_are_all_applied(self):
        """版本变量、usage 行、镜像分支三处都要落地。"""
        patched = kubeasz_patches.patch_ezdown(EZDOWN_36X)
        self.assertIn('ingressNginxVer=4.13.0', patched)
        self.assertIn('to download images of ingress-nginx', patched)
        self.assertIn('    ingress-nginx)', patched)
        for image in kubeasz_patches.INGRESS_NGINX_IMAGES:
            self.assertIn(image, patched)

    def test_the_patched_script_is_parseable_by_the_sync_command(self):
        """打完补丁的脚本能被同步器的解析函数读出 ingress 镜像 —— 能力判定的输入。"""
        patched = kubeasz_patches.patch_ezdown(EZDOWN_36X)
        extras = parse_ezdown(patched)['extra_images']
        self.assertEqual(
            sorted(extras.get(kubeasz_patches.INGRESS_NGINX_OPTION) or []),
            sorted(kubeasz_patches.INGRESS_NGINX_IMAGES),
        )

    def test_patching_twice_is_a_no_op(self):
        """幂等：三处锚点打完还在，再打一遍不该把改动插成两份。"""
        once = kubeasz_patches.patch_ezdown(EZDOWN_36X)
        twice = kubeasz_patches.patch_ezdown(once)
        self.assertEqual(twice, once)
        self.assertEqual(twice.count('ingressNginxVer='), 1)
        self.assertEqual(twice.count('    ingress-nginx)'), 1)

    def test_version_variable_declared_tells_the_two_apart(self):
        """判定函数要能把「上游原文」和「打过补丁的」分开。"""
        self.assertFalse(kubeasz_patches.version_variable_declared(EZDOWN_36X))
        self.assertTrue(kubeasz_patches.version_variable_declared(
            kubeasz_patches.patch_ezdown(EZDOWN_36X),
        ))

    def test_a_script_without_the_anchors_is_left_alone(self):
        """上游结构变了（没有 kubeblocksVer 锚点）时不该静默返回半成品，要报错。"""
        with self.assertRaises(ValueError):
            kubeasz_patches.patch_ezdown('#!/bin/bash\necho hi\n')


class CommandPatchEzdownWriteTest(TestCase):
    """落盘：行尾必须是 LF，且只动登记过补丁的版本。"""

    def setUp(self):
        """准备一个临时目录当同步工作区。

        参数：无。
        返回：无显式返回值。
        副作用：创建临时目录，用例结束时删除。
        """
        import shutil

        self.root = Path(tempfile.mkdtemp(prefix='kubeasz-ezdown-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write_script(self, text=EZDOWN_36X):
        """把一段 ezdown 写进工作区。

        参数：`text` 表示脚本内容。
        返回：返回写入后的路径。
        副作用：写文件。
        """
        path = self.root / 'ezdown'
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def command(self):
        """构造同步命令实例（只为调用它的私有方法，不碰 OSS）。

        参数：无。
        返回：返回 Command 实例。
        副作用：无。
        """
        from ops.management.commands.sync_kubeasz_artifacts import Command

        return Command()

    def test_written_script_has_lf_line_endings(self):
        """写出去的文件里不能出现 \\r —— 这就是线上那个 bad interpreter 的成因。"""
        path = self.write_script()
        self.command()._patch_ezdown('3.6.7', path)

        payload = path.read_bytes()
        self.assertNotIn(b'\r', payload)
        self.assertTrue(payload.startswith(b'#!/bin/bash\n'))
        self.assertIn(b'ingressNginxVer=4.13.0', payload)

    def test_running_the_sync_twice_does_not_duplicate_the_patch(self):
        """同步跑两遍（复用同一份工作副本）时补丁只落一次。"""
        path = self.write_script()
        command = self.command()
        command._patch_ezdown('3.6.7', path)
        once = path.read_bytes()
        command._patch_ezdown('3.6.7', path)
        self.assertEqual(path.read_bytes(), once)
        self.assertEqual(once.count(b'ingressNginxVer='), 1)

    def test_versions_without_a_registered_patch_are_untouched(self):
        """没登记补丁的版本（3.6.8 等）原样不动。"""
        path = self.write_script()
        before = path.read_bytes()
        self.command()._patch_ezdown('3.6.8', path)
        self.assertEqual(path.read_bytes(), before)
