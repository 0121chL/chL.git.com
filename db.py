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

-- 用户与会话（登录/注册）。role: approver=审批人（看全部事件）, engineer=工程师（只看派给自己的）
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_salt TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'engineer',
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

-- 用户偏好（复测窗口、提示音、列表条数…）：一个用户一行 JSON
CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER PRIMARY KEY,
    payload    TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- 负责人表（可选择的派单对象）。username 可空：不绑定账号也能派单，
-- 只是站内通知会回到派单人身上（提醒他线下通知）。
-- on_duty = 当班，派单时可以"按当班自动选人"。
CREATE TABLE IF NOT EXISTS staff (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    team       TEXT,
    phone      TEXT,
    username   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    on_duty    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_staff_active ON staff(active);

-- 站内通知（谁、什么事、读过没有）
CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient     TEXT    NOT NULL,     -- 登录用户名
    kind          TEXT    NOT NULL,     -- dispatch / assignee_unbound / system
    title         TEXT    NOT NULL,
    body          TEXT,
    event_id      TEXT,
    work_order_id INTEGER,
    created_at    TEXT    NOT NULL,
    read_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient, read_at);

-- 排班表：谁在什么时段的哪个班次值班（start > end 表示跨夜）
CREATE TABLE IF NOT EXISTS shifts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,      -- 班次名，如 早班 / 夜班
    team       TEXT,
    staff_id   INTEGER NOT NULL,
    start_time TEXT    NOT NULL,      -- HH:MM
    end_time   TEXT    NOT NULL,      -- HH:MM
    weekdays   TEXT    NOT NULL DEFAULT '1,2,3,4,5,6,7',   -- ISO 星期，逗号分隔
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shifts_staff ON shifts(staff_id);
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
        # 关闭时的「处置后验证」结论（JSON）与例外关闭理由
        ("verification", "ALTER TABLE work_orders ADD COLUMN verification TEXT"),
        ("exception_reason", "ALTER TABLE work_orders ADD COLUMN exception_reason TEXT"),
        # 派单对象的登录账号（负责人表里绑定的那个，用于「我的待办」匹配）
        ("assignee_username", "ALTER TABLE work_orders ADD COLUMN assignee_username TEXT"),
        # 整改闭环：接单 → 已处理（措施 + 照片）→ 复测 → 关闭
        ("accepted_at", "ALTER TABLE work_orders ADD COLUMN accepted_at TEXT"),
        ("accepted_by", "ALTER TABLE work_orders ADD COLUMN accepted_by TEXT"),
        ("handled_at", "ALTER TABLE work_orders ADD COLUMN handled_at TEXT"),
        ("handled_by", "ALTER TABLE work_orders ADD COLUMN handled_by TEXT"),
        ("measure", "ALTER TABLE work_orders ADD COLUMN measure TEXT"),
        ("photo_path", "ALTER TABLE work_orders ADD COLUMN photo_path TEXT"),
        ("verified_at", "ALTER TABLE work_orders ADD COLUMN verified_at TEXT"),
        # SLA 超时提醒去重：只提醒一次，避免每 30 秒刷屏
        ("overdue_notified_at", "ALTER TABLE work_orders ADD COLUMN overdue_notified_at TEXT"),
    ):
        if column not in wo_cols:
            conn.execute(ddl)

    # 3) approvals: 驳回原因结构化（误报/重复/已处置…），用于统计误报率
    ap_cols = _columns(conn, "approvals")
    if "reason_code" not in ap_cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN reason_code TEXT")

    # 4) staff: 值班标记（派单时"按当班自动选人"）
    staff_cols = _columns(conn, "staff")
    if staff_cols and "on_duty" not in staff_cols:
        conn.execute("ALTER TABLE staff ADD COLUMN on_duty INTEGER NOT NULL DEFAULT 0")

    # 5) users: 老库里的 operator 统一升级成 approver（他们本来就在做审批），
    #    新注册一律从 engineer 起步，除非注册时显式选了审批人。
    conn.execute("UPDATE users SET role = 'approver' WHERE role = 'operator'")


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
            SELECT id, event_id, content, status, created_at, assignee, assigned_at,
                   closed_at, resolution, verification, exception_reason, assignee_username,
                   accepted_at, accepted_by, handled_at, handled_by, measure, photo_path,
                   verified_at, overdue_notified_at
            FROM work_orders WHERE id = ?
            """,
            (work_order_id,),
        ).fetchone()
    return dict(row) if row else None


def update_work_order(db_path: str | Path | None, work_order_id: int, **fields: Any) -> dict[str, Any] | None:
    """只允许更新流转相关字段；其它键会被忽略。"""
    allowed = {
        "status",
        "assignee",
        "assigned_at",
        "closed_at",
        "resolution",
        "verification",
        "exception_reason",
        "assignee_username",
        "accepted_at",
        "accepted_by",
        "handled_at",
        "handled_by",
        "measure",
        "photo_path",
        "verified_at",
        "overdue_notified_at",
    }
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


def update_event_payload(db_path: str | Path | None, event_id: str, payload: dict[str, Any]) -> None:
    """就地更新事件正文（持续告警的 aggregate 字段用它刷新，不新增事件）。"""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET payload = ?, updated_at = ? WHERE event_id = ?",
            (json.dumps(payload, ensure_ascii=False), utcnow(), event_id),
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


def list_work_orders(
    db_path: str | Path | None,
    limit: int = 50,
    assignee_username: str | None = None,
) -> list[dict[str, Any]]:
    """列工单。传 assignee_username 时只返回派给该账号的（工程师视角）。"""
    sql = """
        SELECT w.id, w.event_id, w.content, w.status, w.created_at,
               w.assignee, w.assignee_username, w.assigned_at, w.closed_at, w.resolution,
               w.verification, w.exception_reason,
               w.accepted_at, w.accepted_by, w.handled_at, w.handled_by, w.measure,
               w.photo_path, w.verified_at, w.overdue_notified_at,
               a.approver, a.comment AS approval_comment, a.decided_at,
               a.reason_code AS approval_reason
        FROM work_orders w
        LEFT JOIN approvals a ON a.event_id = w.event_id
    """
    params: list[Any] = []
    if assignee_username is not None:
        sql += " WHERE w.assignee_username = ?"
        params.append(assignee_username)
    sql += " ORDER BY w.id DESC LIMIT ?"
    params.append(limit)
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["content"] = json.loads(item["content"])
        except (TypeError, json.JSONDecodeError):
            pass
        if isinstance(item.get("verification"), str):
            try:
                item["verification"] = json.loads(item["verification"])
            except json.JSONDecodeError:
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
    reason_code: str | None = None,
) -> dict[str, Any]:
    """一个事件只保留一条终态审批记录；重复调用返回既有记录。"""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO approvals (event_id, decision, approver, comment, decided_at, work_order_id, reason_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, decision, approver, comment, utcnow(), work_order_id, reason_code),
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
    role: str = "engineer",
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


def delete_user_sessions_except(db_path: str | Path | None, user_id: int, keep_token: str) -> int:
    """吊销某用户除当前令牌之外的会话（改密码后调用）。"""
    digest = hashlib.sha256(keep_token.encode("utf-8")).hexdigest()
    with connect(db_path) as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, digest),
        ).rowcount


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


def update_password(db_path: str | Path | None, user_id: int, salt: str, hashed: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt, hashed, int(user_id)),
        )


# --------------------------------------------------------------------------- #
# user_settings（用户偏好）
# --------------------------------------------------------------------------- #
def get_user_settings(db_path: str | Path | None, user_id: int) -> dict[str, Any]:
    """返回该用户已保存的偏好（原始字典，未合并默认值）。"""
    with connect(db_path) as conn:
        row = conn.execute("SELECT payload FROM user_settings WHERE user_id = ?", (int(user_id),)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_user_settings(db_path: str | Path | None, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, payload, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (int(user_id), body, utcnow()),
        )
    return payload


# --------------------------------------------------------------------------- #
# 处置后验证：最近 N 分钟的遥测统计
# --------------------------------------------------------------------------- #
def telemetry_window(
    db_path: str | Path | None,
    device_id: str,
    sensor: str,
    minutes: int,
    limit: int = 20000,
) -> dict[str, Any]:
    """取最近 minutes 分钟内某测点的统计量；用于「工单关闭前确认已恢复」。

    list_telemetry 是 id 倒序，所以 points[0] 是最新一条。
    """
    points = list_telemetry(db_path, device_id=device_id, sensor=sensor, minutes=minutes, limit=limit)
    values = [p["value"] for p in points if isinstance(p.get("value"), (int, float))]
    return {
        "samples": len(values),
        "max": max(values) if values else None,
        "min": min(values) if values else None,
        "last": values[0] if values else None,
        "first_ts": points[-1]["ts"] if points else None,
        "last_ts": points[0]["ts"] if points else None,
    }


# --------------------------------------------------------------------------- #
# 驳回原因统计（误报治理的数据出口）
# --------------------------------------------------------------------------- #
def rejection_counts(db_path: str | Path | None, days: int = 7) -> list[dict[str, Any]]:
    """按「规则 × 驳回原因」聚合，用于判断哪条规则在误报。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT a.event_id, a.reason_code, e.payload
            FROM approvals a
            LEFT JOIN events e ON e.event_id = a.event_id
            WHERE a.decision = 'rejected'
              AND julianday(a.decided_at) >= julianday('now', ?)
            """,
            (f"-{int(days)} days",),
        ).fetchall()

    buckets: dict[tuple[str, str], int] = {}
    for row in rows:
        rule_id = "—"
        if row["payload"]:
            try:
                rule_id = str(json.loads(row["payload"]).get("rule_id") or "—")
            except (TypeError, json.JSONDecodeError):
                pass
        key = (rule_id, str(row["reason_code"] or "other"))
        buckets[key] = buckets.get(key, 0) + 1

    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0][0]))
    return [{"rule_id": rule, "reason_code": code, "count": count} for (rule, code), count in ordered]


# --------------------------------------------------------------------------- #
# staff（负责人表）：派单时从这里选人
# --------------------------------------------------------------------------- #
STAFF_FIELDS = ("name", "team", "phone", "username", "active", "on_duty")
_STAFF_COLUMNS = "id, name, team, phone, username, active, on_duty, created_at"


def list_staff(db_path: str | Path | None, active_only: bool = False) -> list[dict[str, Any]]:
    sql = f"SELECT {_STAFF_COLUMNS} FROM staff"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY active DESC, on_duty DESC, id ASC"
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql)]


def get_staff(db_path: str | Path | None, staff_id: int) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_STAFF_COLUMNS} FROM staff WHERE id = ?", (int(staff_id),)
        ).fetchone()
    return dict(row) if row else None


def get_staff_by_name(db_path: str | Path | None, name: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_STAFF_COLUMNS} FROM staff WHERE name = ?", (name,)
        ).fetchone()
    return dict(row) if row else None


def create_staff(
    db_path: str | Path | None,
    name: str,
    team: str | None = None,
    phone: str | None = None,
    username: str | None = None,
    on_duty: int = 0,
) -> dict[str, Any]:
    """新增负责人。name 唯一，重复时抛 sqlite3.IntegrityError（由接口层转 409）。"""
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO staff (name, team, phone, username, active, on_duty, created_at)"
            " VALUES (?,?,?,?,1,?,?)",
            (name, team, phone, username, 1 if on_duty else 0, utcnow()),
        )
        row = conn.execute(
            f"SELECT {_STAFF_COLUMNS} FROM staff WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def update_staff(db_path: str | Path | None, staff_id: int, **fields: Any) -> dict[str, Any] | None:
    payload = {k: v for k, v in fields.items() if k in STAFF_FIELDS}
    if payload:
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with connect(db_path) as conn:
            conn.execute(
                f"UPDATE staff SET {assignments} WHERE id = ?",
                (*payload.values(), int(staff_id)),
            )
    return get_staff(db_path, staff_id)


def deactivate_staff(db_path: str | Path | None, staff_id: int) -> bool:
    """软删除：保留历史派单里的姓名，只是不再出现在下拉里。"""
    with connect(db_path) as conn:
        cur = conn.execute("UPDATE staff SET active = 0 WHERE id = ?", (int(staff_id),))
        return cur.rowcount == 1


def pick_on_duty_staff(db_path: str | Path | None, team: str | None = None) -> dict[str, Any] | None:
    """按「当班 + 在办工单最少」挑一个负责人；指定班组优先在该班组里挑。"""
    rows = [row for row in list_staff(db_path, active_only=True) if int(row.get("on_duty") or 0)]
    if team:
        same_team = [row for row in rows if (row.get("team") or "") == team]
        if same_team:
            rows = same_team
    if not rows:
        return None
    with connect(db_path) as conn:
        counts = {
            str(row["assignee"]): int(row["c"])
            for row in conn.execute(
                """
                SELECT assignee, COUNT(*) AS c FROM work_orders
                WHERE assignee IS NOT NULL AND status != 'closed'
                GROUP BY assignee
                """
            )
        }
    rows.sort(key=lambda row: (counts.get(str(row["name"]), 0), int(row["id"])))
    return rows[0]


# --------------------------------------------------------------------------- #
# notifications（站内通知）
# --------------------------------------------------------------------------- #
def add_notification(
    db_path: str | Path | None,
    recipient: str,
    kind: str,
    title: str,
    body: str | None = None,
    event_id: str | None = None,
    work_order_id: int | None = None,
) -> dict[str, Any]:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO notifications (recipient, kind, title, body, event_id, work_order_id, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (recipient, kind, title, body, event_id, work_order_id, utcnow()),
        )
        row = conn.execute("SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_notifications(
    db_path: str | Path | None,
    recipient: str,
    unread_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM notifications WHERE recipient = ?"
    params: list[Any] = [recipient]
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def count_unread_notifications(db_path: str | Path | None, recipient: str) -> int:
    with connect(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE recipient = ? AND read_at IS NULL",
                (recipient,),
            ).fetchone()[0]
        )


def mark_notification_read(db_path: str | Path | None, notification_id: int, recipient: str) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND recipient = ? AND read_at IS NULL",
            (utcnow(), int(notification_id), recipient),
        )
        return cur.rowcount == 1


def mark_all_notifications_read(db_path: str | Path | None, recipient: str) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at = ? WHERE recipient = ? AND read_at IS NULL",
            (utcnow(), recipient),
        )
        return cur.rowcount


# --------------------------------------------------------------------------- #
# shifts（排班表）：按时间自动置 on_duty
# --------------------------------------------------------------------------- #
_SHIFT_COLUMNS = (
    "s.id, s.name, s.team, s.staff_id, s.start_time, s.end_time, s.weekdays, s.active, s.created_at,"
    " st.name AS staff_name, st.username AS staff_username"
)


def list_shifts(db_path: str | Path | None, active_only: bool = False) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_SHIFT_COLUMNS} FROM shifts s LEFT JOIN staff st ON st.id = s.staff_id"
    )
    if active_only:
        sql += " WHERE s.active = 1"
    sql += " ORDER BY s.active DESC, s.start_time ASC, s.id ASC"
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql)]


def get_shift(db_path: str | Path | None, shift_id: int) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_SHIFT_COLUMNS} FROM shifts s LEFT JOIN staff st ON st.id = s.staff_id"
            " WHERE s.id = ?",
            (int(shift_id),),
        ).fetchone()
    return dict(row) if row else None


def create_shift(
    db_path: str | Path | None,
    name: str,
    staff_id: int,
    start_time: str,
    end_time: str,
    team: str | None = None,
    weekdays: str = "1,2,3,4,5,6,7",
) -> dict[str, Any]:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO shifts (name, team, staff_id, start_time, end_time, weekdays, active, created_at)
            VALUES (?,?,?,?,?,?,1,?)
            """,
            (name, team, int(staff_id), start_time, end_time, weekdays, utcnow()),
        )
        row_id = cur.lastrowid
    return get_shift(db_path, int(row_id))  # type: ignore[return-value]


def update_shift(db_path: str | Path | None, shift_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "team", "staff_id", "start_time", "end_time", "weekdays", "active"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if payload:
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with connect(db_path) as conn:
            conn.execute(
                f"UPDATE shifts SET {assignments} WHERE id = ?",
                (*payload.values(), int(shift_id)),
            )
    return get_shift(db_path, shift_id)


def delete_shift(db_path: str | Path | None, shift_id: int) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM shifts WHERE id = ?", (int(shift_id),))
        return cur.rowcount == 1


def _parse_hhmm(value: str) -> int | None:
    try:
        hour, minute = str(value).strip().split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def shift_is_active(shift: dict[str, Any], now: datetime) -> bool:
    """当前时刻是否落在该班次内。start > end 表示跨夜（如 20:00–08:00）。"""
    start = _parse_hhmm(shift.get("start_time"))
    end = _parse_hhmm(shift.get("end_time"))
    if start is None or end is None or start == end:
        return False
    weekdays = {
        int(part)
        for part in str(shift.get("weekdays") or "1,2,3,4,5,6,7").replace(" ", "").split(",")
        if part.isdigit()
    }
    if weekdays and now.isoweekday() not in weekdays:
        return False
    minute = now.hour * 60 + now.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # 跨夜


def set_on_duty(db_path: str | Path | None, staff_ids: set[int], duty: bool) -> int:
    if not staff_ids:
        return 0
    placeholders = ",".join("?" for _ in staff_ids)
    with connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE staff SET on_duty = ? WHERE id IN ({placeholders})",
            (1 if duty else 0, *staff_ids),
        )
        return cur.rowcount


def sync_duty(db_path: str | Path | None, now: datetime) -> dict[str, Any]:
    """按排班表刷新 on_duty。

    只动"出现在排班表里的人"，没排班的人保留手工设置——
    否则手工标的值班会被每 30 秒一次的同步清掉。
    """
    shifts = list_shifts(db_path, active_only=True)
    if not shifts:
        return {"scheduled": 0, "on_duty": [], "turned_off": 0, "configured": False}
    scheduled = {int(row["staff_id"]) for row in shifts}
    active = {int(row["staff_id"]) for row in shifts if shift_is_active(row, now)}
    turned_on = set_on_duty(db_path, active - {0}, True)
    turned_off = set_on_duty(db_path, scheduled - active, False)
    return {
        "scheduled": len(scheduled),
        "on_duty": sorted(active),
        "turned_on": turned_on,
        "turned_off": turned_off,
        "configured": True,
    }


def reset_all(db_path: str | Path | None) -> None:
    """仅供验收脚本使用：清空业务表，让记录数断言可复现。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM work_orders")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM approvals")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('work_orders', 'audit_log', 'approvals')")
