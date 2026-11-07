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

import json
import os
import sqlite3
from datetime import datetime, timezone
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
    created_at TEXT    NOT NULL
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
"""


def utcnow() -> str:
    """统一的时间戳格式：2026-11-05T14:03:21Z（UTC，秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    return path


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
            (ts, device_id, sensor, value, unit, scenario_id),
        )


def purge_telemetry(db_path: str | Path | None, retention_hours: float) -> int:
    """删除 retention 之前的遥测，返回删除行数。"""
    with connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM telemetry WHERE ts < datetime('now', ?)",
            (f"-{retention_hours} hours",),
        )
        return cur.rowcount


def list_telemetry(
    db_path: str | Path | None,
    device_id: str | None = None,
    sensor: str | None = None,
    minutes: int = 60,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    sql = "SELECT ts, device_id, sensor, value, unit FROM telemetry WHERE ts >= datetime('now', ?)"
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


def reset_all(db_path: str | Path | None) -> None:
    """仅供验收脚本使用：清空业务表，让记录数断言可复现。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM work_orders")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM approvals")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('work_orders', 'audit_log', 'approvals')")
