"""口令哈希、会话令牌摘要、密码策略、登录限流与旧库迁移。"""

from __future__ import annotations

import hashlib
import sqlite3

import auth
import db


def test_hash_roundtrip():
    salt, hashed = auth.hash_password("secret123")
    assert auth.verify_password("secret123", salt, hashed)
    assert not auth.verify_password("wrong", salt, hashed)


def test_hash_uses_random_salt():
    s1, h1 = auth.hash_password("same")
    s2, h2 = auth.hash_password("same")
    assert s1 != s2
    assert h1 != h2


def test_tokens_are_unique_and_long():
    t1, t2 = auth.new_token(), auth.new_token()
    assert t1 != t2
    assert len(t1) >= 32


def test_password_policy():
    assert auth.validate_password("abc12345") is None
    assert auth.validate_password("short1") == "密码至少 8 位"
    assert auth.validate_password("12345678") == "密码需包含字母"
    assert auth.validate_password("abcdefgh") == "密码需包含数字"


def test_user_and_session_lifecycle(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)

    salt, hashed = auth.hash_password("secret123")
    user = db.create_user(path, "alice", salt, hashed)
    assert user["username"] == "alice"

    stored = db.get_user(path, "alice")
    assert auth.verify_password("secret123", stored["password_salt"], stored["password_hash"])

    token = auth.new_token()
    db.create_session(path, token, user["id"], ttl_days=7)
    session_user = db.get_session_user(path, token)
    assert session_user and session_user["username"] == "alice"

    db.delete_session(path, token)
    assert db.get_session_user(path, token) is None


def test_expired_session_rejected(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    salt, hashed = auth.hash_password("secret123")
    user = db.create_user(path, "bob", salt, hashed)
    token = auth.new_token()
    db.create_session(path, token, user["id"], ttl_days=-1)
    assert db.get_session_user(path, token) is None


def test_session_token_is_stored_hashed(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    salt, hashed = auth.hash_password("secret123")
    user = db.create_user(path, "carol", salt, hashed)

    token = auth.new_token()
    db.create_session(path, token, user["id"], ttl_days=7)

    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        assert "token" not in columns, "不允许再落明文令牌"
        stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]

    assert stored == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert stored != token


def test_logout_revokes_all_sessions(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    salt, hashed = auth.hash_password("secret123")
    user = db.create_user(path, "dave", salt, hashed)
    t1, t2 = auth.new_token(), auth.new_token()
    db.create_session(path, t1, user["id"])
    db.create_session(path, t2, user["id"])
    assert db.delete_user_sessions(path, user["id"]) == 2
    assert db.get_session_user(path, t1) is None
    assert db.get_session_user(path, t2) is None


def test_login_rate_limit_locks_after_threshold(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    for _ in range(4):
        info = db.record_login_failure(path, "mallory", threshold=5, lock_seconds=60)
        assert info["locked_until"] is None
    info = db.record_login_failure(path, "mallory", threshold=5, lock_seconds=60)
    assert info["failed"] == 5
    assert info["locked_until"] is not None
    assert str(info["locked_until"]) > db.utcnow()

    db.clear_login_failures(path, "mallory")
    assert db.get_login_state(path, "mallory") is None


def test_legacy_sessions_are_migrated_to_hash(tmp_path):
    """旧库里的明文 token 升级后应变成摘要，且原令牌仍可用。"""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            INSERT INTO users (username, password_salt, password_hash, created_at)
                VALUES ('legacy', 'salt', 'hash', '2026-01-01T00:00:00Z');
            INSERT INTO sessions VALUES ('raw-token', 1, '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z');
            """
        )

    db.init_db(path)

    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        assert "token" not in columns
        digest = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert digest == hashlib.sha256(b"raw-token").hexdigest()

    user = db.get_session_user(path, "raw-token")
    assert user and user["username"] == "legacy"
