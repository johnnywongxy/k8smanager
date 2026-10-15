"""部署密钥：生成成什么样、怎么判定它真的能登录、以及老 sshd 的算法回退。

**密钥本身**（``EnsureKeypairTests``）：必须是 ed25519 且是 OpenSSH 私钥容器格式。
kubeasz 的 ``ezctl`` 调起的是**平台机上**的 ``ssh`` 客户端，规划里那台是 CentOS 7 /
OpenSSH 6.6.1（2013 年），RSA 只会签 ``ssh-rsa``(SHA-1)；而目标节点普遍是 Ubuntu 22.04 /
sshd 8.9，默认 ``PubkeyAcceptedAlgorithms`` 里没有 ``ssh-rsa``。一头只会发 SHA-1、一头
拒收 SHA-1，于是 ``01.prepare`` 直接 UNREACHABLE。这三条都是「错了要到目标机上才炸」的
性质：算法错要等 ansible 连不上，格式错要等本机 ssh 读不懂私钥，权限松了要等别人指出。

**判定路径**（``VerifyKeyLoginTests``）：必须走系统 ``ssh`` 而不是 paramiko。paramiko 会签
``rsa-sha2-512``，所以 RSA 时代它能连上现代 sshd，而 ansible 用系统 ssh 立刻被拒——
``credentials`` 阶段报「免密登录可用」，几秒后 ``01.prepare`` 报 Permission denied。
判定与执行只要不是同一个客户端，结论就没有意义。

**老 sshd 回退**（``LegacyRsaAlgorithmTests`` / ``SSHClientFallbackTests``）：CentOS 7 自带的
OpenSSH 6.6.1 不认 RFC 8332 的 ``rsa-sha2-*`` 签名算法，也不发 ``server-sig-algs`` 扩展；
paramiko 收不到该扩展时不做任何探测，直接取自己列表里的第一个 RSA 算法（``rsa-sha2-512``），
于是被对方以 ``unsupported public key algorithm`` 拒掉。回退必须挂在 paramiko 的
``disabled_algorithms['pubkeys']`` 上（对应 ``_preferred_pubkeys``，即
PubKeyAcceptedAlgorithms）。写成 ``'keys'`` 不会报错，但改的是主机密钥那张表，回退静默
失效——这正是本文件要钉住的第一条。改用 ed25519 之后这条回退已不在主路径上，留着是给
「运维手工把部署私钥换成 RSA」兜底。

需要本机 ``ssh-keygen`` 的两条用例（解析私钥、对拍指纹）在缺工具时跳过：它们问的是
「OpenSSH 自己认不认」，用 python 复刻一遍定义是问不出答案的。
"""

import os
import shutil
import stat
import subprocess
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from k8s_deployment.services import credentials


def fake_client_class(outcomes, transport_up=True):
    """构造一个按脚本返回建连结果的假 SSHClient 类。

    参数：`outcomes` 为 ``connect`` 的结果序列，元素是异常实例（抛出）或 None（成功），
        实例化次数超过序列长度后一律按成功处理；`transport_up` 表示失败之后传输层是否
        已经建立（``_transport_up`` 据此区分「认证没过」与「连都连不上」）。
    返回：返回可传给 ``mock.patch`` 的类；``instances`` 记录本次创建的全部实例。
    副作用：只做纯计算，不发起网络连接。
    """
    queue = list(outcomes)
    created = []

    class _FakeClient:
        """记录建连参数、按脚本决定成功或失败的假客户端。"""

        def __init__(self):
            """初始化假客户端并从脚本里取本次结果。"""
            self.kwargs = {}
            self.closed = False
            self.outcome = queue.pop(0) if queue else None
            created.append(self)

        def set_missing_host_key_policy(self, policy):
            """记录主机密钥策略，保持与 paramiko 接口一致。"""
            self.policy = policy

        def connect(self, **kwargs):
            """记录建连参数，必要时抛出脚本预设的异常。"""
            self.kwargs = kwargs
            if self.outcome is not None:
                raise self.outcome

        def get_transport(self):
            """返回传输层替身；只有 ``is_active()`` 会参与判断。"""
            return types.SimpleNamespace(is_active=lambda: transport_up)

        def close(self):
            """记录连接已关闭。"""
            self.closed = True

    _FakeClient.instances = created
    return _FakeClient


class LegacyRsaAlgorithmTests(TestCase):
    """钉住回退开关本身：它必须挂在 pubkeys 上，且真的只剩 ssh-rsa。"""

    def test_disabled_algorithms_targets_pubkeys(self):
        """子键只能是 pubkeys；写成 keys 会静默失效。"""
        self.assertEqual(set(credentials.LEGACY_RSA_ALGORITHMS), {'pubkeys'})

    def test_disabling_rsa_sha2_leaves_only_ssh_rsa(self):
        """默认挑中 rsa-sha2-*（老 sshd 拒它），禁用后落到 ssh-rsa（老 sshd 认它）。"""
        import socket

        from paramiko.transport import Transport

        def rsa_algorithms(disabled):
            """返回给定禁用配置下 paramiko 会参与协商的 RSA 公钥算法序列。"""
            local, remote = socket.socketpair()
            try:
                transport = Transport(local, disabled_algorithms=disabled)
                return [name for name in transport.preferred_pubkeys if 'rsa' in name]
            finally:
                local.close()
                remote.close()

        default = rsa_algorithms(None)
        self.assertIn(default[0], {'rsa-sha2-512', 'rsa-sha2-256'})
        self.assertEqual(rsa_algorithms(credentials.LEGACY_RSA_ALGORITHMS), ['ssh-rsa'])


class SSHClientFallbackTests(TestCase):
    """ssh_client 只在密钥认证被拒时回退，且只回退一次。"""

    def test_key_auth_retries_without_rsa_sha2(self):
        """密钥首轮被拒时禁用 rsa-sha2 重试，成功后返回新连接。"""
        import paramiko

        fake = fake_client_class([paramiko.AuthenticationException('denied'), None])
        with mock.patch('paramiko.SSHClient', fake):
            client = credentials.ssh_client('10.0.0.1', 22, 'root', key_path='/tmp/deploy_rsa')

        first, second = fake.instances
        self.assertNotIn('disabled_algorithms', first.kwargs)
        self.assertEqual(second.kwargs['disabled_algorithms'], credentials.LEGACY_RSA_ALGORITHMS)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertIs(client, second)

    def test_password_auth_does_not_retry(self):
        """密码认证不涉及签名协商，被拒就是被拒，不做第二次尝试。"""
        import paramiko

        fake = fake_client_class([paramiko.AuthenticationException('denied'), None])
        with mock.patch('paramiko.SSHClient', fake):
            with self.assertRaises(credentials.CredentialError) as ctx:
                credentials.ssh_client('10.0.0.1', 22, 'root', password='secret')

        self.assertEqual(str(ctx.exception), credentials.LOGIN_REJECTED)
        self.assertEqual(len(fake.instances), 1)
        self.assertTrue(fake.instances[0].closed)

    def test_retry_failure_reports_login_rejected(self):
        """回退也失败时按首轮结论报错，并关闭两条连接。"""
        import paramiko

        fake = fake_client_class([
            paramiko.AuthenticationException('denied'),
            paramiko.AuthenticationException('denied'),
        ])
        with mock.patch('paramiko.SSHClient', fake):
            with self.assertRaises(credentials.CredentialError) as ctx:
                credentials.ssh_client('10.0.0.1', 22, 'root', key_path='/tmp/deploy_rsa')

        self.assertEqual(str(ctx.exception), credentials.LOGIN_REJECTED)
        self.assertEqual(len(fake.instances), 2)
        self.assertTrue(all(item.closed for item in fake.instances))

    def test_ssh_exception_after_transport_is_up_still_retries(self):
        """paramiko 密钥被拒后抛的是别的私钥类的副产品异常，回退同样必须触发。

        真机（CentOS 7 / OpenSSH 6.6.1）上第一轮失败抛的不是 ``AuthenticationException``，
        而是 ``SSHException: encountered RSA key, expected OPENSSH key``——那是
        ``SSHClient._auth`` 试完所有私钥类之后留下的最后一轮异常。只认
        ``AuthenticationException`` 会让回退永远不触发，「公钥已写入目标机，但密钥登录
        仍然失败」就是这么来的。
        """
        import paramiko

        fake = fake_client_class([
            paramiko.SSHException('encountered RSA key, expected OPENSSH key'), None,
        ])
        with mock.patch('paramiko.SSHClient', fake):
            client = credentials.ssh_client('10.0.0.1', 22, 'root', key_path='/tmp/deploy_rsa')

        first, second = fake.instances
        self.assertNotIn('disabled_algorithms', first.kwargs)
        self.assertEqual(second.kwargs['disabled_algorithms'], credentials.LEGACY_RSA_ALGORITHMS)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertIs(client, second)

    def test_protocol_failure_does_not_retry(self):
        """传输层根本没起来时与签名算法无关，回退一次也救不回来，不该多试。"""
        import paramiko

        fake = fake_client_class([paramiko.SSHException('negotiation failed'), None], transport_up=False)
        with mock.patch('paramiko.SSHClient', fake):
            with self.assertRaises(credentials.CredentialError) as ctx:
                credentials.ssh_client('10.0.0.1', 22, 'root', key_path='/tmp/deploy_rsa')

        self.assertEqual(str(ctx.exception), 'SSH 协议协商失败：该端口可能不是 SSH 服务。')
        self.assertEqual(len(fake.instances), 1)


class KeypairTestCase(TestCase):
    """把密钥写进临时状态根的公共夹具，避免碰真实部署密钥。"""

    def setUp(self):
        """准备临时状态根并把 ``K8S_DEPLOY_STATE_ROOT`` 指过去。"""
        self.state_root = Path(tempfile.mkdtemp(prefix='deploy-key-'))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        overrides = override_settings(K8S_DEPLOY_STATE_ROOT=str(self.state_root))
        overrides.enable()
        self.addCleanup(overrides.disable)


class EnsureKeypairTests(KeypairTestCase):
    """部署密钥必须是 ed25519，且落盘就是 OpenSSH 格式、0600。"""

    def test_key_is_ed25519_in_openssh_format(self):
        """私钥是 OpenSSH 容器格式，公钥以 ``ssh-ed25519`` 开头并带平台标记。"""
        private, public = credentials.ensure_keypair()

        with private.open(encoding='utf-8') as handle:
            self.assertEqual(handle.readline().strip(), '-----BEGIN OPENSSH PRIVATE KEY-----')
        self.assertTrue(public.startswith('ssh-ed25519 '), public[:40])
        self.assertIn(credentials.KEY_MARKER, public)

    def test_key_pair_lands_under_state_root(self):
        """密钥落在 ``<状态根>/ssh/`` 下，文件名带算法，不会被历史 RSA 密钥冒用。"""
        private, _public = credentials.ensure_keypair()

        self.assertEqual(private, self.state_root / 'ssh' / credentials.PRIVATE_KEY_NAME)
        self.assertTrue(private.exists())
        self.assertTrue(private.with_suffix('.pub').exists())

    def test_private_key_is_not_readable_by_others(self):
        """私钥落地即 0600；Windows 没有等价权限位。"""
        private, _public = credentials.ensure_keypair()
        if os.name == 'nt':
            self.skipTest('Windows 没有等价的权限位')

        self.assertEqual(stat.S_IMODE(private.stat().st_mode) & 0o077, 0)
        self.assertTrue(credentials.key_permissions_ok())

    def test_second_call_keeps_the_same_key(self):
        """已存在时原样返回，绝不重新生成——换密钥会让已下发过的节点全部失联。"""
        private, public = credentials.ensure_keypair()
        before = private.read_bytes()

        again_private, again_public = credentials.ensure_keypair()

        self.assertEqual(again_private, private)
        self.assertEqual(again_public, public)
        self.assertEqual(private.read_bytes(), before)

    def test_generated_key_is_parsable_by_openssh(self):
        """``ssh-keygen -y`` 要能解析这把私钥并还原出公钥。

        PEM/PKCS8 格式的 ed25519 私钥就是在这里露馅的：内容完全合法，只有系统 ``ssh``
        与 ``ssh-keygen`` 不认。用 python 复刻一遍格式定义是问不出这个答案的。
        """
        keygen = shutil.which('ssh-keygen')
        if not keygen:
            self.skipTest('本机没有 ssh-keygen')
        private, public = credentials.ensure_keypair()

        completed = subprocess.run(
            [keygen, '-y', '-f', str(private)], capture_output=True, text=True, check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        # ``ssh-keygen -y`` 只吐密钥本体，不带注释，所以比对前两段。
        self.assertEqual(completed.stdout.split()[:2], public.split()[:2])


class KeyFingerprintTests(KeypairTestCase):
    """指纹是「公钥 blob 的 SHA-256」，必须与 ``ssh-keygen -lf`` 逐字一致。"""

    def test_fingerprint_matches_ssh_keygen(self):
        """和 OpenSSH 自己算的指纹对拍。

        必须显式要 ``-E sha256``：``ssh-keygen`` 直到 OpenSSH 6.8 才有这个开关，更老的版本
        只会输出传统的 ``-E md5`` 形式（``d1:53:cb:...``），两种摘要算法没法互相对拍。
        规划里的平台机是 CentOS 7 自带的 OpenSSH 6.6.1，就是那种老版本——那里这条用例只能跳过，
        但平台上显示的 ``SHA256:`` 依然是对的，它是 OpenSSH 6.8 起才有的标准形式。
        """
        keygen = shutil.which('ssh-keygen')
        if not keygen:
            self.skipTest('本机没有 ssh-keygen')
        _private, public = credentials.ensure_keypair()
        public_file = credentials.public_key_path()
        completed = subprocess.run(
            [keygen, '-lf', str(public_file), '-E', 'sha256'],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f'ssh-keygen 不支持 -E sha256（低于 OpenSSH 6.8）：{completed.stderr.strip()}')

        self.assertEqual(credentials.key_fingerprint(public), completed.stdout.split()[1])

    def test_unparsable_text_gives_empty_string(self):
        """缺段或 base64 不合法时返回空串，让调用方自己去决定怎么显示。"""
        for text in ('', None, 'ssh-ed25519', 'ssh-ed25519 !!!not-base64!!! mark'):
            self.assertEqual(credentials.key_fingerprint(text), '', text)

    def test_fingerprint_ignores_comment(self):
        """注释不同不影响指纹——运维手工粘贴时经常把注释改掉。"""
        _private, public = credentials.ensure_keypair()

        self.assertEqual(
            credentials.key_fingerprint(public),
            credentials.key_fingerprint(public.split()[0] + ' ' + public.split()[1]),
        )


class VerifyKeyLoginTests(KeypairTestCase):
    """判定必须走系统 ``ssh``，命令行本身就是被钉住的对象。"""

    def setUp(self):
        """在临时状态根里准备好一把真实私钥，供 ``verify_key_login`` 检查存在性。"""
        super().setUp()
        self.key, _public = credentials.ensure_keypair()

    def _run(self, returncode=0):
        """替掉 ``subprocess.run``，返回 ``(结果, mock 对象)``。"""
        completed = subprocess.CompletedProcess([], returncode, b'', b'')
        patcher = mock.patch.object(credentials.subprocess, 'run', return_value=completed)
        return patcher.start(), patcher

    def test_success_returns_true_and_uses_system_ssh(self):
        """返回码 0 即可用；命令行必须带齐密钥、禁交互、禁密码兜底。"""
        run, patcher = self._run(0)
        self.addCleanup(patcher.stop)

        self.assertTrue(credentials.verify_key_login('10.0.0.1', 2222, 'root', key_path=self.key))

        argv = run.call_args[0][0]
        self.assertEqual(argv[0], credentials.SSH_CLIENT)
        self.assertEqual(argv[argv.index('-i') + 1], str(self.key))
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('PreferredAuthentications=publickey', argv)
        self.assertIn('PasswordAuthentication=no', argv)
        self.assertIn('StrictHostKeyChecking=no', argv)
        self.assertIn(f'ConnectTimeout={credentials.CONNECT_TIMEOUT}', argv)
        self.assertEqual(argv[argv.index('-p') + 1], '2222')
        self.assertEqual(argv[-2:], ['root@10.0.0.1', 'true'])
        # 不能走 shell（用户名/地址来自用户输入），也不能吞掉输出。
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertIs(run.call_args.kwargs['check'], False)

    def test_rejected_returns_false(self):
        """认证没过（ssh 的 255）就是 False，不抛异常——调用方据此决定是否下发公钥。"""
        run, patcher = self._run(255)
        self.addCleanup(patcher.stop)

        self.assertFalse(credentials.verify_key_login('10.0.0.1', 22, 'root', key_path=self.key))

    def test_missing_private_key_returns_false_without_running_ssh(self):
        """私钥不在就别起进程了：问的是密钥能不能登录，没有密钥就是不能。"""
        run, patcher = self._run(0)
        self.addCleanup(patcher.stop)

        self.assertFalse(
            credentials.verify_key_login('10.0.0.1', 22, 'root', key_path=self.state_root / 'no-key')
        )
        run.assert_not_called()

    def test_missing_ssh_client_raises_credential_error(self):
        """本机没有 ssh 客户端要显式报错。

        这种情况 ansible 同样跑不了，必须让用户先把它装好，而不是误报成「密钥没装」
        再白跑一遍下发流程。
        """
        patcher = mock.patch.object(
            credentials.subprocess, 'run', side_effect=FileNotFoundError('ssh'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertRaises(credentials.CredentialError) as ctx:
            credentials.verify_key_login('10.0.0.1', 22, 'root', key_path=self.key)

        self.assertIn(credentials.SSH_CLIENT, str(ctx.exception))

    def test_timeout_raises_credential_error(self):
        """进程层面超时（ssh 卡住不返回）与「认证被拒」不是一回事，要分开报。"""
        patcher = mock.patch.object(
            credentials.subprocess,
            'run',
            side_effect=subprocess.TimeoutExpired('ssh', credentials.CONNECT_TIMEOUT),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertRaises(credentials.CredentialError) as ctx:
            credentials.verify_key_login('10.0.0.1', 22, 'root', key_path=self.key)

        self.assertIn(credentials.SSH_CLIENT, str(ctx.exception))


class EnsureKeyLoginTests(TestCase):
    """``ensure_key_login`` 的三条出路：已可用 / 下发后可用 / 下发后仍不可用。"""

    def setUp(self):
        """准备一个节点快照替身。"""
        self.node = SimpleNamespace(ip_address='10.0.0.1', ssh_user='root', ssh_port=22)

    def test_existing_key_wins_without_touching_password(self):
        """密钥已经能用时不下发、不用密码。"""
        with mock.patch.object(credentials, 'verify_key_login', return_value=True), \
                mock.patch.object(credentials, 'install_public_key') as install:
            result = credentials.ensure_key_login(self.node, {'user': 'root', 'port': 22})

        self.assertEqual(result, {'ok': True, 'message': '部署密钥免密登录可用。', 'installed': False})
        install.assert_not_called()

    def test_installs_key_when_password_is_available(self):
        """密钥不可用但托管了密码时下发公钥，下发后复验通过即算成功。"""
        with mock.patch.object(
            credentials, 'verify_key_login', side_effect=[False, True],
        ), mock.patch.object(
            credentials, 'install_public_key', return_value={'already': False},
        ) as install:
            result = credentials.ensure_key_login(
                self.node, {'user': 'root', 'port': 22, 'password': 'secret'},
            )

        self.assertTrue(result['ok'])
        self.assertTrue(result['installed'])
        self.assertEqual(result['message'], '已下发平台部署公钥，免密登录可用。')
        self.assertEqual(install.call_args[0][:4], ('10.0.0.1', 22, 'root', 'secret'))

    def test_already_present_key_is_not_reported_as_installed(self):
        """公钥本来就在位时，原文案不谎称「已下发」。"""
        with mock.patch.object(
            credentials, 'verify_key_login', side_effect=[False, True],
        ), mock.patch.object(
            credentials, 'install_public_key', return_value={'already': True},
        ):
            result = credentials.ensure_key_login(
                self.node, {'user': 'root', 'port': 22, 'password': 'secret'},
            )

        self.assertTrue(result['ok'])
        self.assertFalse(result['installed'])
        self.assertEqual(result['message'], '免密登录可用。')

    def test_without_password_it_refuses_instead_of_guessing(self):
        """没有托管密码时明确要求人工预置公钥，不去试空密码。"""
        with mock.patch.object(
            credentials, 'verify_key_login', return_value=False,
        ), mock.patch.object(credentials, 'install_public_key') as install:
            result = credentials.ensure_key_login(self.node, {'user': 'root', 'port': 22})

        self.assertFalse(result['ok'])
        self.assertFalse(result['installed'])
        self.assertIn('没有托管 SSH 密码', result['message'])
        install.assert_not_called()

    def test_install_failure_message_is_surfaced(self):
        """下发本身失败时原样转述原因，不再往下复验。"""
        with mock.patch.object(
            credentials, 'verify_key_login', return_value=False,
        ) as verify, mock.patch.object(
            credentials,
            'install_public_key',
            side_effect=credentials.CredentialError('向目标机 10.0.0.1 写入公钥失败。'),
        ):
            result = credentials.ensure_key_login(
                self.node, {'user': 'root', 'port': 22, 'password': 'secret'},
            )

        self.assertFalse(result['ok'])
        self.assertFalse(result['installed'])
        self.assertEqual(result['message'], '向目标机 10.0.0.1 写入公钥失败。')
        self.assertEqual(verify.call_count, 1)

    def test_installed_but_still_rejected_mentions_algorithm_intersection(self):
        """公钥写进去了却依然连不上——这是算法不匹配的典型症状，提示里必须点到它。"""
        with mock.patch.object(
            credentials, 'verify_key_login', side_effect=[False, False],
        ), mock.patch.object(
            credentials, 'install_public_key', return_value={'already': False},
        ):
            result = credentials.ensure_key_login(
                self.node, {'user': 'root', 'port': 22, 'password': 'secret'},
            )

        self.assertFalse(result['ok'])
        self.assertTrue(result['installed'])
        self.assertIn('算法', result['message'])

    def test_node_defaults_are_used_when_credentials_omit_them(self):
        """凭据里没给用户名/端口时回落到节点记录上的值。"""
        node = SimpleNamespace(ip_address='10.0.0.2', ssh_user='ubuntu', ssh_port=2222)
        with mock.patch.object(
            credentials, 'verify_key_login', return_value=True,
        ) as verify:
            credentials.ensure_key_login(node, {})

        self.assertEqual(verify.call_args[0][:3], ('10.0.0.2', 2222, 'ubuntu'))
