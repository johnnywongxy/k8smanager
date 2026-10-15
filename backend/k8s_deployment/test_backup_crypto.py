"""备份归档的分片加解密与密钥强度边界测试。

这里只覆盖纯本地、不依赖 OSS 与目标机的部分：密钥强度判定、分片加密往返、
密文被改动时的中止行为、以及密钥轮换后旧备份不可解密的既定语义。
所有用例都在临时目录里造文件，不会读写平台状态目录。
"""

import base64
import hashlib
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings
from django.test import TestCase, override_settings

from k8s_cluster import crypto as secret_crypto
from k8s_deployment.services import backup

# 源码里的默认占位密钥，真实环境必须拒绝用它创建备份。
PLACEHOLDER_KEY = 'platform-template-local-dev-secret-change-me'
# 一个满足长度与随机性要求的高强度密钥。
STRONG_KEY = 'a7Fq2xN9pLwZ4vRt6YhJ8kM3sD1gB5cE0uQ7iO9aS2dF6gH'


def _fernet_for(key):
    """按平台的口径（SECRET_KEY 的 sha256 摘要）从某个密钥造一把 Fernet。

    这里不调用 `crypto` 自己的私有函数：要验的是「换密钥之后旧密文还能读」，
    造旧密文这一步必须独立于被测代码，否则两边一起错就测不出来了。
    """
    digest = hashlib.sha256(key.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))



class BackupKeyStrengthTest(TestCase):
    """备份密钥强度判定的边界。"""

    @override_settings(SECRET_KEY=PLACEHOLDER_KEY)
    def test_rejects_source_placeholder(self):
        """源码默认密钥必须被拒绝，并给出可操作的中文原因。"""
        with self.assertRaises(backup.BackupError) as caught:
            backup.assert_backup_key_strength()
        self.assertIn('DJANGO_SECRET_KEY', str(caught.exception))

    def test_empty_key_is_impossible_at_runtime(self):
        """空 SECRET_KEY 由 Django 自己在启动阶段就拦下，走不到备份逻辑里。

        这条断言存在的意义是说明 ``assert_backup_key_strength`` 里的空值分支是兜底：
        真正拦住空密钥的是框架本身，因此不要指望靠这里的检查去补救配置错误。
        """
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            with override_settings(SECRET_KEY=''):
                backup.assert_backup_key_strength()

    @override_settings(SECRET_KEY='short-key-1234')
    def test_rejects_short_key(self):
        """长度不足 32 位的密钥被拒绝：太短容易被穷举。"""
        with self.assertRaises(backup.BackupError):
            backup.assert_backup_key_strength()

    @override_settings(SECRET_KEY='x' * 40 + 'changeme')
    def test_rejects_key_containing_placeholder_token(self):
        """长度够但含 changeme 这类占位词，仍然按占位密钥处理。"""
        with self.assertRaises(backup.BackupError):
            backup.assert_backup_key_strength()

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_accepts_strong_key(self):
        """高强度密钥通过校验，且能力探测返回可用。"""
        backup.assert_backup_key_strength()
        self.assertEqual(backup.backup_capability(), {'ok': True, 'message': ''})

    @override_settings(SECRET_KEY=PLACEHOLDER_KEY)
    def test_capability_reports_reason_instead_of_raising(self):
        """能力探测不抛异常：前端需要拿到原因来禁用按钮并显示提示。"""
        result = backup.backup_capability()
        self.assertFalse(result['ok'])
        self.assertIn('SECRET_KEY', result['message'])

    @override_settings(K8S_DEPLOY_BACKUP_CHUNK_BYTES='not-a-number')
    def test_bad_chunk_setting_falls_back(self):
        """分片大小配置成非法值时回落到默认值，而不是让任务崩掉。"""
        self.assertEqual(backup.chunk_bytes(), backup.DEFAULT_CHUNK_BYTES)

    @override_settings(K8S_DEPLOY_BACKUP_CHUNK_BYTES='1')
    def test_chunk_size_has_floor(self):
        """分片大小有下限：太小会让令牌数量爆炸。"""
        self.assertEqual(backup.chunk_bytes(), 64 * 1024)

    @override_settings(K8S_DEPLOY_BACKUP_RETENTION_MIN_KEEP=0)
    def test_min_keep_has_floor(self):
        """最少保留数至少为 1：否则保留策略会把所有备份都删光。"""
        self.assertEqual(backup.retention_min_keep(), 1)


class ChunkedCipherTest(TestCase):
    """分片密文格式与往返一致性。"""

    def setUp(self):
        """准备一个临时目录作为明密文的工作区。

        参数：无。
        返回：无显式返回值。
        副作用：在系统临时目录下创建目录，用例结束时删除。
        """
        self.temp = Path(tempfile.mkdtemp(prefix='k8sbk-test-'))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def _round_trip(self, payload, chunk=None, key=STRONG_KEY):
        """把一段字节加密再解密，返回解密结果与加密统计。

        参数：`payload` 表示明文字节；`chunk` 表示分片大小；`key` 表示加密使用的密钥。
        返回：返回 ``(解密后的字节, encrypt_stream 的统计字典)``。
        副作用：在临时目录内写入两个文件。
        """
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        out_path = self.temp / 'out.bin'
        plain_path.write_bytes(payload)
        with override_settings(SECRET_KEY=key):
            stats = backup.encrypt_stream(plain_path, cipher_path, chunk=chunk)
            written = backup.decrypt_stream(cipher_path, out_path)
        self.assertEqual(written, len(payload))
        return out_path.read_bytes(), stats

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_empty_file_round_trip(self):
        """空文件也能往返：写入魔数、零个分片，解出零字节。"""
        plain, stats = self._round_trip(b'')
        self.assertEqual(plain, b'')
        self.assertEqual(stats['chunks'], 0)

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_small_file_round_trip(self):
        """小于一个分片的文件只有一个分片。"""
        plain, stats = self._round_trip(b'kubeasz-backup-payload')
        self.assertEqual(plain, b'kubeasz-backup-payload')
        self.assertEqual(stats['chunks'], 1)

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_multi_chunk_round_trip_is_byte_exact(self):
        """跨多个分片的文件必须逐字节还原，分片边界不能丢数据。

        这里刻意用「整片 + 余数」的长度，覆盖最后一片不满的情形。
        """
        chunk = 64 * 1024
        payload = bytes(range(256)) * 1024 + b'tail-marker' * 1234
        self.assertGreater(len(payload), chunk * 2)
        plain, stats = self._round_trip(payload, chunk=chunk)
        self.assertEqual(plain, payload)
        self.assertEqual(stats['chunks'], (len(payload) + chunk - 1) // chunk)

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_binary_payload_with_high_bytes_survives(self):
        """全部 0x00-0xFF 的字节都要能正确往返，避免 latin-1 编码丢字节。"""
        payload = bytes(range(256)) * 512
        plain, _stats = self._round_trip(payload, chunk=4096)
        self.assertEqual(plain, payload)

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_ciphertext_carries_magic_header(self):
        """密文自带魔数，便于识别归档格式。"""
        payload = b'x' * 100
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        plain_path.write_bytes(payload)
        backup.encrypt_stream(plain_path, cipher_path, chunk=4096)
        self.assertTrue(cipher_path.read_bytes().startswith(backup.CIPHERTEXT_MAGIC))

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_plaintext_never_appears_in_ciphertext(self):
        """明文不得出现在密文里：这是「不落明文口令」的底线检查。"""
        secret_text = b'P@ssw0rd-of-the-target-machine'
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        plain_path.write_bytes(secret_text * 100)
        backup.encrypt_stream(plain_path, cipher_path, chunk=4096)
        self.assertNotIn(secret_text, cipher_path.read_bytes())

    def test_rotated_key_cannot_decrypt_old_backup(self):
        """密钥轮换后旧备份不可解密，且错误信息必须点明这一点。

        这是规划明确要求写进文档的破坏性行为：把失败原因说清楚，用户才不会
        以为是归档坏了而反复重试。
        """
        payload = b'before-rotation'
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        out_path = self.temp / 'out.bin'
        plain_path.write_bytes(payload)
        with override_settings(SECRET_KEY=STRONG_KEY):
            backup.encrypt_stream(plain_path, cipher_path, chunk=4096)
        with override_settings(SECRET_KEY='Z' + STRONG_KEY[1:]):
            with self.assertRaises(backup.BackupError) as caught:
                backup.decrypt_stream(cipher_path, out_path)
        self.assertIn('密钥已轮换', str(caught.exception))
        self.assertFalse(out_path.exists(), '解密失败后不得留下半成品明文文件')

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_magic_mismatch_is_rejected(self):
        """格式不对的文件要在早期被拒绝，而不是解出垃圾。"""
        cipher_path = self.temp / 'cipher.bin'
        out_path = self.temp / 'out.bin'
        cipher_path.write_bytes(b'not-a-platform-archive')
        with self.assertRaises(backup.BackupError) as caught:
            backup.decrypt_stream(cipher_path, out_path)
        self.assertIn('格式不正确', str(caught.exception))

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_tampered_chunk_stops_decryption(self):
        """改动过密文的一个字节必须中止解密，不能静默产出坏数据。"""
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        out_path = self.temp / 'out.bin'
        plain_path.write_bytes(b'A' * 8192)
        backup.encrypt_stream(plain_path, cipher_path, chunk=2048)

        raw = bytearray(cipher_path.read_bytes())
        # 跳过魔数与第一段长度行，落在第一个令牌的中间字节上。
        offset = len(backup.CIPHERTEXT_MAGIC)
        line_end = raw.index(b'\n', offset)
        raw[line_end + 10] = raw[line_end + 10] ^ 0xFF
        cipher_path.write_bytes(bytes(raw))

        with self.assertRaises(backup.BackupError):
            backup.decrypt_stream(cipher_path, out_path)
        self.assertFalse(out_path.exists(), '解密失败后必须删除半成品')

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_truncated_ciphertext_stops_decryption(self):
        """密文被截断必须报「不完整」，而不是当作正常结束。"""
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        out_path = self.temp / 'out.bin'
        plain_path.write_bytes(b'B' * 8192)
        backup.encrypt_stream(plain_path, cipher_path, chunk=2048)
        raw = cipher_path.read_bytes()
        cipher_path.write_bytes(raw[:-30])

        with self.assertRaises(backup.BackupError):
            backup.decrypt_stream(cipher_path, out_path)
        self.assertFalse(out_path.exists())

    @override_settings(SECRET_KEY=STRONG_KEY)
    def test_corrupted_length_line_stops_decryption(self):
        """长度字段被写成非数字时给出明确的格式错误。"""
        plain_path = self.temp / 'plain.bin'
        cipher_path = self.temp / 'cipher.bin'
        out_path = self.temp / 'out.bin'
        plain_path.write_bytes(b'C' * 4096)
        backup.encrypt_stream(plain_path, cipher_path, chunk=2048)
        raw = cipher_path.read_bytes()
        offset = len(backup.CIPHERTEXT_MAGIC)
        line_end = raw.index(b'\n', offset)
        cipher_path.write_bytes(raw[:offset] + b'abcdef\n' + raw[line_end + 1:])

        with self.assertRaises(backup.BackupError) as caught:
            backup.decrypt_stream(cipher_path, out_path)
        self.assertIn('分片长度字段损坏', str(caught.exception))

    def test_round_trip_works_with_platform_secret_key_helper(self):
        """加解密走的是平台的加密组件，往返结果与直接调用它一致。"""
        with override_settings(SECRET_KEY=STRONG_KEY):
            token = secret_crypto.encrypt_secret('hello')
            self.assertEqual(secret_crypto.decrypt_secret(token), 'hello')
            self.assertEqual(secret_crypto.decrypt_secret(token[:-2] + 'aa'), '')

    def test_ciphertext_written_with_legacy_default_key_still_decrypts(self):
        """旧默认密钥写下的密文仍能解开：settings 已把它挂在解密回退链上。

        平台在没有 DJANGO_SECRET_KEY 的情况下跑过，kubeconfig 与客户端证书就是用当时的
        默认密钥加密落库的。换默认值时若把旧值从解密链上摘掉，`decrypt_secret` 会落进
        InvalidToken 分支静默返回空串 —— 表现成「凭据凭空丢了」而不是报错，比直接失败更难查。

        这里只覆盖 SECRET_KEY，LEGACY_SECRET_KEY 取 settings 里的真实值：要钉住的正是
        生产配置本身，而不是测试临时拼出来的组合。
        """
        legacy_token = _fernet_for(PLACEHOLDER_KEY).encrypt(
            'kubeconfig-material'.encode('utf-8')
        ).decode('utf-8')
        with override_settings(SECRET_KEY=STRONG_KEY):
            self.assertEqual(settings.LEGACY_SECRET_KEY, PLACEHOLDER_KEY)
            self.assertEqual(secret_crypto.decrypt_secret(legacy_token), 'kubeconfig-material')
            fresh_token = secret_crypto.encrypt_secret('new-material')
        # 新密文只认当前密钥：回退链是单向的，旧密钥解不开新数据。
        self.assertEqual(
            _fernet_for(STRONG_KEY).decrypt(fresh_token.encode('utf-8')).decode('utf-8'),
            'new-material',
        )

    @override_settings(K8S_DEPLOY_BACKUP_MAX_BYTES=1024)
    def test_max_archive_bytes_reads_setting(self):
        """归档上限来自平台设置，便于按环境调整。"""
        self.assertEqual(backup.max_archive_bytes(), 1024)

    def test_backup_id_is_unique_and_prefixed(self):
        """备份标识形如 bk-时间戳-随机串，且反复生成不重复。"""
        identifiers = {backup.new_backup_id() for _ in range(50)}
        self.assertEqual(len(identifiers), 50)
        self.assertTrue(all(item.startswith('bk-') for item in identifiers))

    def test_sha256_file_matches_hashlib(self):
        """文件摘要是标准 SHA-256，便于与 OSS 上的校验值对照。"""
        import hashlib

        target = self.temp / 'data.bin'
        target.write_bytes(b'checksum-me' * 100)
        self.assertEqual(backup.sha256_file(target), hashlib.sha256(b'checksum-me' * 100).hexdigest())

    def test_unlink_is_quiet_on_missing_file(self):
        """删除不存在的文件不抛异常：清理路径必须在失败分支里也能安全调用。"""
        with mock.patch.object(Path, 'unlink', side_effect=OSError('locked')):
            backup._unlink(self.temp / 'missing.bin')
