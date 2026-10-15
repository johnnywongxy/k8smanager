import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


MASK = '********'


def _fernet_candidates():
    """Build Fernet instances in decryption order: current key first, legacy key last.

    旧默认密钥必须留在解密链上：它派生过的密文已经落在库里（kubeconfig、客户端证书、
    token）。只认当前密钥时 `decrypt_secret` 会落到 InvalidToken 分支返回空串，表现出来
    是「凭据凭空丢了」而不是报错——比直接失败更难查。加密只用第一个。
    """
    values = [str(getattr(settings, 'SECRET_KEY', '') or '')]
    legacy = str(getattr(settings, 'LEGACY_SECRET_KEY', '') or '')
    if legacy and legacy not in values:
        values.append(legacy)
    candidates = []
    for value in values:
        if not value:
            continue
        digest = hashlib.sha256(value.encode('utf-8')).digest()
        candidates.append(Fernet(base64.urlsafe_b64encode(digest)))
    if not candidates:
        raise ValueError('SECRET_KEY 未配置，无法派生本地加密密钥')
    return candidates


def encrypt_secret(value):
    """Encrypt kubeconfig and certificate material before storing them."""
    if not value:
        return ''
    return _fernet_candidates()[0].encrypt(value.encode('utf-8')).decode('utf-8')


def decrypt_secret(value):
    """Return decrypted secret text; invalid tokens are treated as empty secrets."""
    if not value:
        return ''
    for fernet in _fernet_candidates():
        try:
            return fernet.decrypt(value.encode('utf-8')).decode('utf-8')
        except InvalidToken:
            continue
    return ''
