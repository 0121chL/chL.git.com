"""口令哈希、会话令牌与会话令牌摘要（标准库实现，不引入额外依赖）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_b64, hash_b64)。"""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return hmac.compare_digest(digest, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """会话令牌只存摘要：库被拷走也无法直接当令牌用。

    令牌是 32 字节随机数，熵足够，用 SHA-256 即可（不需要慢哈希）。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password(password: str) -> str | None:
    """返回错误文案；None 表示通过。"""
    if len(password) < 8:
        return "密码至少 8 位"
    if not re.search(r"[A-Za-z]", password):
        return "密码需包含字母"
    if not re.search(r"\d", password):
        return "密码需包含数字"
    return None
