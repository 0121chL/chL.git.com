"""口令哈希与会话（登录/注册的底座）。"""

from __future__ import annotations

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
