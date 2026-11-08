"""SQLite 持久层：工单表 + 审计表（幂等写入）。

设计要点
--------
1. work_orders.event_id 上有 UNIQUE 约束，工单只能由某事件产生一次；
   重复写入走 `INSERT ... ON CONFLICT(event_id) DO NOTHING`，返回已存在的行。
2. audit_log 的 (event_id, stage) 唯一。LangGraph 的 interrupt 节点在 resume 时
   会**从头重新执行**，因此 interrupt 之前的副作用必须 upsert —— 这一层由
   `upsert_audit()` 保证，重复执行不会产生第二条记录。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "monitor.db"

DDL = """
CREATE TABLE IF NOT EXISTS work_orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT    NOT NULL UNIQUE,
    content    TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'created',
    created_at TEXT    NOT NULL,
    assignee   TEXT,
    assigned_at TEXT,
    closed_at  TEXT,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT    NOT NULL,
    stage      TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (event_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_work_orders_event ON work_orders(event_id);
CREATE INDEX IF NOT EXISTS idx_audit_event_stage ON audit_log(event_id, stage);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    payload     TEXT    NOT NULL,
    analysis    TEXT,
    citations   TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    received_at TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);

-- 人工审批留痕：谁、什么时候、以什么理由批/驳
CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT    NOT NULL UNIQUE,
    decision      TEXT    NOT NULL,
    approver      TEXT    NOT NULL,
    comment       TEXT,
    decided_at    TEXT    NOT NULL,
    work_order_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_approvals_event ON approvals(event_id);

-- 原始遥测（用于回放与复盘；按 retention 清理）
CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    device_id   TEXT    NOT NULL,
    sensor      TEXT    NOT NULL,
    value       REAL,
    unit        TEXT,
    scenario_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_device ON telemetry(device_id, sensor, ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);

-- 用户与会话（登录/注册）
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_salt TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'operator',
    created_at    TEXT    NOT NULL
);

-- 只存令牌的 SHA-256 摘要，不落明文
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT    PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- 登录失败计数（限流/锁定）
CREATE TABLE IF NOT EXISTS login_attempts (
    username     TEXT    PRIMARY KEY,
    failed       INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_at      TEXT    NOT NULL
);
"""


def utcnow() -> str:
    """统一的时间戳格式：2026-11-05T14:03:21Z（UTC，秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_utc_iso(value: str | None) -> str | None:
    """把带偏移的时间（如 2026-11-08T12:21:17+08:00）统一成 UTC 的 ...Z 形式。"""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return value


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """优先级：显式参数 > 环境变量 MONITOR_DB > data/monitor.db。"""
    if db_path:
        return Path(db_path).expanduser().resolve()
    env = os.environ.get("MONITOR_DB")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DB_PATH


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path | None = None) -> Path:
    path = resolve_db_path(db_path)
    with connect(path) as conn:
        conn.executescript(DDL)
        _migrate(conn)
    return path


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """就地升级旧库：会话令牌改存摘要、工单补流转字段。

    迁移是幂等的：字段/表已经对得上就直接返回。
    """
    # 1) sessions: token(明文) → token_hash(摘要)
    cols = _columns(conn, "sessions")
    if "token" in cols and "token_hash" not in cols:
        import hashlib

        conn.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
        conn.execute(
            """
            CREATE TABLE sessions (
                token_hash   TEXT    PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                created_at   TEXT    NOT NULL,
                expires_at   TEXT    NOT NULL,
                last_used_at TEXT
            )
            """
        )
        for row in conn.execute("SELECT token, user_id, created_at, expires_at FROM sessions_legacy"):
            digest = hashlib.sha256(str(row["token"]).encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (digest, row["user_id"], row["created_at"], row["expires_at"]),
            )
        conn.execute("DROP TABLE sessions_legacy")

    # 2) work_orders: 补流转字段
    wo_cols = _columns(conn, "work_orders")
    for column, ddl in (
        ("assignee", "ALTER TABLE work_orders ADD COLUMN assignee TEXT"),
        ("assigned_at", "ALTER TABLE work_orders ADD COLUMN assigned_at TEXT"),
        ("closed_at", "ALTER TABLE work_orders ADD COLUMN closed_at TEXT"),
        ("resolution", "ALTER TABLE work_orders ADD COLUMN resolution TEXT"),
    ):
        if column not in wo_cols:
            conn.execute(ddl)


# --------------------------------------------------------------------------- #
# work_orders
# --------------------------------------------------------------------------- #
def insert_work_order(
    db_path: str | Path | None,
    event_id: str,
    content: str,
    status: str = "created",
) -> dict[str, Any]:
    """幂等写入：同一 event_id 已存在时直接返回既有行，不重复插入。"""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO work_orders (event_id, content, status, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, content, status, utcnow()),
        )
        row = conn.execute(
            "SELECT id, event_id, content, status, created_at FROM work_orders WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return dict(row)


def get_work_order(db_path: str | Path | None, event_id: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, event_id, content, status, created_at FROM work_orders WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return dict(row) if row else None


def get_work_order_by_id(db_path: str | Path | None, work_order_id: int) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, event_id, content, status, created_at, assignee, assigned_at, closed_at, resolution
            FROM work_orders WHERE id = ?
            """,
            (work_order_id,),
        ).fetchone()
    return dict(row) if row else None


def update_work_order(db_path: str | Path | None, work_order_id: int, **fields: Any) -> dict[str, Any] | None:
    """只允许更新流转相关字段；其它键会被忽略。"""
    allowed = {"status", "assignee", "assigned_at", "closed_at", "resolution"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return get_work_order_by_id(db_path, work_order_id)
    assignments = ", ".join(f"{key} = ?" for key in payload)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE work_orders SET {assignments} WHERE id = ?",
            (*payload.values(), work_order_id),
        )
    return get_work_order_by_id(db_path, work_order_id)


def count_work_orders(db_path: str | Path | None, event_id: str | None = None) -> int:
    with connect(db_path) as conn:
        if event_id is None:
            return int(conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0])
        return int(
            conn.execute("SELECT COUNT(*) FROM work_orders WHERE event_id = ?", (event_id,)).fetchone()[0]
        )


# --------------------------------------------------------------------------- #
# audit_log（幂等）
# --------------------------------------------------------------------------- #
def upsert_audit(
    db_path: str | Path | None,
    event_id: str,
    stage: str,
    payload: dict[str, Any] | str,
) -> bool:
    """写入审计记录；已存在 (event_id, stage) 时保持首次写入内容不变。

    返回 True 表示本次真正插入，False 表示命中幂等（重复执行被吞掉）。
    """
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO audit_log (event_id, stage, payload, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, stage) DO NOTHING
            """,
            (event_id, stage, body, utcnow()),
        )
        return cur.rowcount == 1


def count_audit(
    db_path: str | Path | None,
    event_id: str | None = None,
    stage: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM audit_log WHERE 1 = 1"
    params: list[Any] = []
    if event_id is not None:
        sql += " AND event_id = ?"
        params.append(event_id)
    if stage is not None:
        sql += " AND stage = ?"
        params.append(stage)
    with connect(db_path) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def reset_event(db_path: str | Path | None, event_id: str) -> None:
    """仅供测试重复运行使用：清掉某个事件的工单与审计记录。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM work_orders WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM audit_log WHERE event_id = ?", (event_id,))


# --------------------------------------------------------------------------- #
# events（看板用：事件对象 + 分析结果 + 审批状态）
# --------------------------------------------------------------------------- #
def upsert_event(
    db_path: str | Path | None,
    event_id: str,
    payload: dict[str, Any],
    status: str = "pending",
) -> None:
    """同一 event_id 重复投递不新增，也不覆盖已有分析/状态。"""
    now = utcnow()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events (event_id, payload, status, received_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, json.dumps(payload, ensure_ascii=False), status, now, now),
        )


def save_event_analysis(
    db_path: str | Path | None,
    event_id: str,
    analysis: dict[str, Any],
    citations: list[dict[str, Any]],
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET analysis = ?, citations = ?, updated_at = ? WHERE event_id = ?",
            (
                json.dumps(analysis, ensure_ascii=False),
                json.dumps(citations, ensure_ascii=False),
                utcnow(),
                event_id,
            ),
        )


def set_event_status(db_path: str | Path | None, event_id: str, status: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET status = ?, updated_at = ? WHERE event_id = ?",
            (status, utcnow(), event_id),
        )


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event": json.loads(row["payload"]),
        "analysis": json.loads(row["analysis"]) if row["analysis"] else None,
        "citations": json.loads(row["citations"]) if row["citations"] else [],
        "status": row["status"],
        "received_at": row["received_at"],
    }


def list_events(db_path: str | Path | None, limit: int = 50) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_event_row(r) for r in rows]


def get_event(db_path: str | Path | None, event_id: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    return _event_row(row) if row else None


def count_events(db_path: str | Path | None, status: str | None = None) -> int:
    with connect(db_path) as conn:
        if status is None:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return int(
            conn.execute("SELECT COUNT(*) FROM events WHERE status = ?", (status,)).fetchone()[0]
        )


def list_work_orders(db_path: str | Path | None, limit: int = 50) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.event_id, w.content, w.status, w.created_at,
                   w.assignee, w.assigned_at, w.closed_at, w.resolution,
                   a.approver, a.comment AS approval_comment, a.decided_at
            FROM work_orders w
            LEFT JOIN approvals a ON a.event_id = w.event_id
            ORDER BY w.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["content"] = json.loads(item["content"])
        except (TypeError, json.JSONDecodeError):
            pass
        result.append(item)
    return result


# --------------------------------------------------------------------------- #
# approvals（谁批的、什么时候、什么理由）
# --------------------------------------------------------------------------- #
def record_approval(
    db_path: str | Path | None,
    event_id: str,
    decision: str,
    approver: str,
    comment: str | None = None,
    work_order_id: int | None = None,
) -> dict[str, Any]:
    """一个事件只保留一条终态审批记录；重复调用返回既有记录。"""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO approvals (event_id, decision, approver, comment, decided_at, work_order_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, decision, approver, comment, utcnow(), work_order_id),
        )
        row = conn.execute("SELECT * FROM approvals WHERE event_id = ?", (event_id,)).fetchone()
    return dict(row)


def get_approval(db_path: str | Path | None, event_id: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM approvals WHERE event_id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# telemetry（原始时序）
# --------------------------------------------------------------------------- #
def insert_telemetry(
    db_path: str | Path | None,
    ts: str,
    device_id: str,
    sensor: str,
    value: float | None,
    unit: str | None,
    scenario_id: str | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO telemetry (ts, device_id, sensor, value, unit, scenario_id) VALUES (?,?,?,?,?,?)",
            (to_utc_iso(ts), device_id, sensor, value, unit, scenario_id),
        )


def purge_telemetry(db_path: str | Path | None, retention_hours: float) -> int:
    """删除 retention 之前的遥测，返回删除行数。"""
    with connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM telemetry WHERE julianday(ts) < julianday('now', ?)",
            (f"-{retention_hours} hours",),
        )
        return cur.rowcount


def purge_audit(db_path: str | Path | None, retention_days: float = 30) -> int:
    """审计与事件只保留最近 N 天，避免 SQLite 无限膨胀。"""
    with connect(db_path) as conn:
        removed = conn.execute(
            "DELETE FROM audit_log WHERE julianday(created_at) < julianday('now', ?)",
            (f"-{retention_days} days",),
        ).rowcount
        removed += conn.execute(
            "DELETE FROM events WHERE julianday(received_at) < julianday('now', ?)",
            (f"-{retention_days} days",),
        ).rowcount
        return removed


def list_telemetry(
    db_path: str | Path | None,
    device_id: str | None = None,
    sensor: str | None = None,
    minutes: int = 60,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    # julianday 能解析带 T 与 +08:00 的 ISO 时间，避免字符串比较把整天都算进来
    sql = "SELECT ts, device_id, sensor, value, unit FROM telemetry WHERE julianday(ts) >= julianday('now', ?)"
    params: list[Any] = [f"-{int(minutes)} minutes"]
    if device_id:
        sql += " AND device_id = ?"
        params.append(device_id)
    if sensor:
        sql += " AND sensor = ?"
        params.append(sensor)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_telemetry(db_path: str | Path | None) -> int:
    with connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0])


# --------------------------------------------------------------------------- #
# users / sessions
# --------------------------------------------------------------------------- #
def create_user(
    db_path: str | Path | None,
    username: str,
    password_salt: str,
    password_hash: str,
    role: str = "operator",
) -> dict[str, Any]:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_salt, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (username, password_salt, password_hash, role, utcnow()),
        )
        row = conn.execute(
            "SELECT id, username, role, created_at FROM users WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_user(db_path: str | Path | None, username: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def create_session(db_path: str | Path | None, token: str, user_id: int, ttl_days: int = 7) -> dict[str, Any]:
    """只落库令牌摘要；原始令牌只返回给调用方一次。"""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=ttl_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_used_at) VALUES (?,?,?,?,?)",
            (digest, user_id, now.isoformat(timespec="seconds").replace("+00:00", "Z"), expires, None),
        )
    return {"expires_at": expires}


def get_session_user(db_path: str | Path | None, token: str) -> dict[str, Any] | None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.role, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (digest,),
        ).fetchone()
        if not row:
            return None
        if str(row["expires_at"]) < utcnow():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
            return None
        conn.execute("UPDATE sessions SET last_used_at = ? WHERE token_hash = ?", (utcnow(), digest))
    return {"id": row["id"], "username": row["username"], "role": row["role"], "expires_at": row["expires_at"]}


def delete_session(db_path: str | Path | None, token: str) -> None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))


def delete_user_sessions(db_path: str | Path | None, user_id: int) -> int:
    with connect(db_path) as conn:
        return conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount


def list_sessions(db_path: str | Path | None, user_id: int) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT created_at, expires_at, last_used_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 登录限流（防爆破）
# --------------------------------------------------------------------------- #
def get_login_state(db_path: str | Path | None, username: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT username, failed, locked_until, last_at FROM login_attempts WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def record_login_failure(
    db_path: str | Path | None,
    username: str,
    threshold: int = 5,
    lock_seconds: int = 60,
) -> dict[str, Any]:
    """失败次数 +1；达到阈值则锁定一段时间（逐次翻倍，最多 15 分钟）。"""
    state = get_login_state(db_path, username) or {"failed": 0}
    failed = int(state.get("failed") or 0) + 1
    locked_until = None
    if failed >= threshold:
        rounds = min(failed - threshold + 1, 5)
        locked_until = (
            datetime.now(timezone.utc) + timedelta(seconds=lock_seconds * (2 ** (rounds - 1)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO login_attempts (username, failed, locked_until, last_at) VALUES (?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET failed=excluded.failed,
                locked_until=excluded.locked_until, last_at=excluded.last_at
            """,
            (username, failed, locked_until, utcnow()),
        )
    return {"failed": failed, "locked_until": locked_until}


def clear_login_failures(db_path: str | Path | None, username: str) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM login_attempts WHERE username = ?", (username,))


def count_users(db_path: str | Path | None) -> int:
    with connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def reset_all(db_path: str | Path | None) -> None:
    """仅供验收脚本使用：清空业务表，让记录数断言可复现。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM work_orders")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM approvals")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('work_orders', 'audit_log', 'approvals')")
