"""第五阶段后端：REST + WebSocket + 既有 SSE 兼容端点。

    python backend_server.py        # 127.0.0.1:8000

对外契约（P5）：
    POST /events                 规则引擎事件入口（thread_id = event_id）
    WS   /ws                     推送 event / analysis / work_order / rejected
    POST /approve/{event_id}     Command(resume="approve")
    POST /reject/{event_id}      Command(resume="reject")
    GET  /work_orders            SQLite 工单

兼容 P4 看板（SSE）：
    GET  /api/stream  /api/events  /api/status  /api/work-orders
    POST /api/approvals/{event_id}
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import sqlite3
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

import agent_graph as ag
import analyzer
import auth
import db
import llm_client
import offline_analyzer
import regulation_store
from mqtt_bridge import MqttBridge
from realtime import EventBus, sse_frame

DB_PATH = os.environ.get("MONITOR_DB") or str(db.DEFAULT_DB_PATH)
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_EVENT_TOPIC", "line1/events")
HEARTBEAT_TOPIC = os.environ.get("MQTT_HEARTBEAT_TOPIC", "line1/heartbeat")
OFFLINE_TIMEOUT = float(os.environ.get("DEVICE_OFFLINE_TIMEOUT", "15"))
# 审批超时：0 = 使用 config/sla.json 的分级值（high/medium/low 各不同）
APPROVAL_TIMEOUT = float(os.environ.get("APPROVAL_TIMEOUT_S", "0"))
AUTH_TTL_DAYS = int(os.environ.get("AUTH_TTL_DAYS", "7"))
HTTP_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"

# 可选鉴权：设置 API_TOKEN 后，写操作需带 Authorization: Bearer <token>
API_TOKEN = os.environ.get("API_TOKEN", "")
# LLM 分析超时（秒），0=不限制；超时自动降级为离线模板
# LLM 分析超时（秒），0=不限制；超时自动降级为离线模板。
# 默认 240s：本地 3B 单次 55–100s，留够余量，又不至于让一次挂起的推理把管线钉死。
ANALYSIS_TIMEOUT = float(os.environ.get("ANALYSIS_TIMEOUT_S", "240"))
ANALYSIS_CONCURRENCY = int(os.environ.get("ANALYSIS_CONCURRENCY", "1"))
# P0-4：规则卡先让事件可审批，AI 结论后补。设 0 退回"分析完再审批"的老行为。
DEFER_ANALYSIS = os.environ.get("DEFER_ANALYSIS", "1").strip().lower() not in ("0", "false", "no", "off")
# 事件处理线程池上限：事件洪峰时排队，而不是无限开线程
EVENT_WORKERS = int(os.environ.get("EVENT_WORKERS", "4"))
# 可选告警外发
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")
ALERTS_FILE = Path(DB_PATH).parent / "alerts.jsonl"
ALERT_ROTATE_BYTES = int(os.environ.get("ALERT_ROTATE_BYTES", str(8 * 1024 * 1024)))
# 整改照片（存文件，不塞数据库）
PHOTO_DIR = Path(DB_PATH).parent / "photos"
PHOTO_MAX_BYTES = int(os.environ.get("PHOTO_MAX_BYTES", str(3 * 1024 * 1024)))
PHOTO_TYPES = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}

# SLA（分钟）：审批时限 / 处置时限，按事件严重度分级。改 config/sla.json 即可生效。
SLA_FILE = Path(__file__).resolve().parent / "config" / "sla.json"
DEFAULT_SLA: dict[str, dict[str, int]] = {
    "approval": {"high": 5, "medium": 30, "low": 120},
    "handle": {"high": 30, "medium": 240, "low": 1440},
}


def load_sla() -> dict[str, dict[str, int]]:
    merged = {kind: dict(values) for kind, values in DEFAULT_SLA.items()}
    try:
        raw = json.loads(SLA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return merged
    for kind, values in (raw or {}).items():
        if kind not in merged or not isinstance(values, dict):
            continue
        for severity, minutes in values.items():
            if severity not in merged[kind]:
                continue
            try:
                merged[kind][severity] = max(1, int(minutes))
            except (TypeError, ValueError):
                continue
    return merged


SLA = load_sla()


def normalize_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("high", "medium", "low") else "high"


def sla_minutes(kind: str, severity: str) -> int:
    table = SLA.get(kind) or DEFAULT_SLA[kind]
    return int(table.get(normalize_severity(severity), table.get("high", 30)))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
# 本机事件直投的信任开关。放到 nginx / Docker 端口映射后面时必须设成 0，
# 否则 request.client.host 永远是 127.0.0.1，等于任何人可伪造事件。
ALLOW_LOCAL_INGEST = os.environ.get("ALLOW_LOCAL_INGEST", "1").strip().lower() not in ("0", "false", "no", "off")
# 允许直投的客户端地址（测试用 TestClient 时它的 host 是 "testclient"）
TRUSTED_INGEST_HOSTS = {
    item.strip()
    for item in os.environ.get("TRUSTED_INGEST_HOSTS", "127.0.0.1,::1,localhost").split(",")
    if item.strip()
}

# --------------------------------------------------------------------------- #
# 审批人注册邀请码：审批人必须自己注册，但不能让任何人自称审批人
# --------------------------------------------------------------------------- #
APPROVER_INVITE_FILE = Path(DB_PATH).parent / "approver_invite.txt"


def _new_invite_code() -> str:
    return f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"


def _load_invite_code() -> tuple[str, str]:
    """返回 (邀请码, 来源)。环境变量优先，否则读/生成 data/approver_invite.txt。"""
    env_code = (os.environ.get("APPROVER_INVITE_CODE") or "").strip()
    if env_code:
        return env_code, "env"
    try:
        existing = APPROVER_INVITE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing, "file"
    code = _new_invite_code()
    try:
        APPROVER_INVITE_FILE.parent.mkdir(parents=True, exist_ok=True)
        APPROVER_INVITE_FILE.write_text(code + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[auth] 邀请码写入失败（{exc}），仅本次运行有效", flush=True)
    return code, "file"


APPROVER_INVITE, APPROVER_INVITE_SOURCE = _load_invite_code()
# SSE/WS 票据：EventSource 无法带 Authorization，用短时票据换一次订阅权限
TICKET_TTL = float(os.environ.get("STREAM_TICKET_TTL_S", "600"))
STREAM_TICKETS: dict[str, dict[str, Any]] = {}
STREAM_TICKETS_LOCK = threading.Lock()

BUS = EventBus()          # SSE 订阅者
WSBUS = EventBus()        # WebSocket 订阅者（复用同一套线程安全发布逻辑）
PIPELINE_LOCK = threading.Lock()   # 只保护 LangGraph 的 invoke/get_state
ANALYSIS_SEM = threading.Semaphore(ANALYSIS_CONCURRENCY)
EVENT_POOL = ThreadPoolExecutor(max_workers=EVENT_WORKERS, thread_name_prefix="event")
# AI 补充意见单独一个池：它慢，但绝不阻塞审批路径
ANALYSIS_POOL = ThreadPoolExecutor(
    max_workers=max(1, ANALYSIS_CONCURRENCY), thread_name_prefix="analyze"
)

# 事件的终态：看板不再等待，内存裁剪时可回收
TERMINAL_STATUSES = frozenset(
    {"approved", "rejected", "superseded", "expired", "analysis_failed", "graph_failed", "failed"}
)

# 驳回原因（结构化）：唯一目的就是让「哪条规则在误报」变成可统计的数据
REJECT_REASONS: dict[str, str] = {
    "false_alarm": "误报",
    "duplicate": "重复事件",
    "handled": "已现场处置",
    "no_action": "无需处置",
    "sensor_fault": "传感器故障",
    "other": "其他",
}

# 用户可配置项：白名单 + 范围。前端只认这些键，写库前一律按这里归一化。
SETTINGS_SPEC: dict[str, dict[str, Any]] = {
    "retest_window_minutes": {"kind": "int", "min": 1, "max": 120, "default": 10},
    "alert_sound": {"kind": "bool", "default": False},
    "event_page_size": {"kind": "int", "choices": (20, 50, 100), "default": 50},
    "confirm_before_approve": {"kind": "bool", "default": True},
    "default_reject_reason": {"kind": "str", "choices": tuple(REJECT_REASONS), "default": "false_alarm"},
}
DEFAULT_SETTINGS: dict[str, Any] = {key: spec["default"] for key, spec in SETTINGS_SPEC.items()}

METRICS: dict[str, float] = {}
METRICS_LOCK = threading.Lock()

EVENTS: dict[str, dict[str, Any]] = {}          # event_id -> 内存状态
EVENTS_LOCK = threading.Lock()
EVENT_LOCKS: dict[str, threading.Lock] = {}

DEVICES: dict[str, dict[str, Any]] = {}          # device_id -> 最近心跳/在线状态
DEVICES_LOCK = threading.Lock()
_DEVICE_STOP = threading.Event()

STATE: dict[str, Any] = {
    "app": None,
    "mqtt": None,
    "analyzer_source": "offline-template",
    "analyzer_model": "—",
    "analyze": offline_analyzer.analyze,
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def broadcast(message: dict[str, Any]) -> None:
    BUS.publish(message)
    WSBUS.publish(message)


# --------------------------------------------------------------------------- #
# 指标 / 告警 / 鉴权
# --------------------------------------------------------------------------- #
def inc(name: str, value: float = 1.0) -> None:
    with METRICS_LOCK:
        METRICS[name] = METRICS.get(name, 0) + value


def events_snapshot() -> list[dict[str, Any]]:
    """在锁内复制一份事件记录，避免调用方边遍历边被 _ingest 改大小。"""
    with EVENTS_LOCK:
        return [dict(record) for record in EVENTS.values()]


def event_record(event_id: str) -> dict[str, Any] | None:
    with EVENTS_LOCK:
        record = EVENTS.get(event_id)
        return dict(record) if record else None


def metrics_text() -> str:
    with METRICS_LOCK:
        body = "\n".join(f"{k} {v}" for k, v in sorted(METRICS.items()))
    snapshot = events_snapshot()
    open_events = sum(1 for e in snapshot if e.get("status") not in TERMINAL_STATUSES)
    spool_dir = Path(DB_PATH).parent / "spool"
    spool_pending = len(list(spool_dir.glob("*.json"))) if spool_dir.exists() else 0
    extra = [
        f"queue_depth {open_events}",
        f"events_memory {len(snapshot)}",
        f"mqtt_connected {1 if STATE.get('mqtt') and STATE['mqtt'].connected else 0}",
        f"devices_online {sum(1 for d in device_snapshot() if d['online'])}",
        f"spool_pending {spool_pending}",
    ]
    return "# 安全监测 Agent Demo metrics\n" + body + "\n" + "\n".join(extra) + "\n"


def _bearer(authorization: str | None) -> str:
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def require_auth(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """写操作必须登录：接受会话 token 或（脚本用）API_TOKEN。"""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    if API_TOKEN and token == API_TOKEN:
        return {"id": 0, "username": "service", "role": "service"}
    user = db.get_session_user(DB_PATH, token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user


# --------------------------------------------------------------------------- #
# 角色：approver=审批人（看全部事件、派单、审批）；engineer=工程师（只看派给自己的）
# --------------------------------------------------------------------------- #
ROLE_APPROVER = "approver"
ROLE_ENGINEER = "engineer"
# 老库的 operator、服务令牌的 service 都按审批人对待（他们本来就在做审批）
APPROVER_ROLES = frozenset({"approver", "admin", "operator", "service"})


def is_approver(user: dict[str, Any] | None) -> bool:
    return str((user or {}).get("role") or "") in APPROVER_ROLES


def require_approver(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """审批人专属操作。工程师调用一律 403。"""
    if not is_approver(user):
        raise HTTPException(
            status_code=403, detail="该操作仅限审批人；工程师只能处理派给自己的工单"
        )
    return user


def require_ingest(request: Request, authorization: str | None = Header(default=None)) -> None:
    """事件入口：本机进程（规则引擎/看门狗）可直投，其余需要 API_TOKEN。
    ⚠️ 放在反向代理后面时 `request.client.host` 恒为 127.0.0.1，必须设
    ALLOW_LOCAL_INGEST=0 并配置 API_TOKEN，否则等于事件入口完全不设防。
    """
    host = request.client.host if request.client else ""
    if ALLOW_LOCAL_INGEST and host in TRUSTED_INGEST_HOSTS:
        return
    if API_TOKEN and _bearer(authorization) == API_TOKEN:
        return
    raise HTTPException(status_code=401, detail="事件入口仅限本机或携带 API_TOKEN")


def tail_lines(path: Path, limit: int, max_bytes: int = 512 * 1024) -> list[str]:
    """从文件尾部读最近若干行，不把整个文件读进内存（告警文件只增不减）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        return []
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()          # 丢掉可能被截断的半行
        data = fh.read()
    return data.decode("utf-8", "replace").splitlines()[-limit:]


def rotate_if_needed(path: Path, max_bytes: int) -> None:
    """告警文件超过阈值就滚一份 .1，避免它无限膨胀。"""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            path.replace(path.with_suffix(path.suffix + ".1"))
            inc("alerts_rotated_total")
    except OSError:
        pass


def emit_alert(event: dict[str, Any], analysis: dict[str, Any]) -> None:
    """高危/离线事件落 alerts.jsonl；配置了 ALERT_WEBHOOK 时异步转发。"""
    record = {
        "ts": utcnow(),
        "event_id": event.get("event_id"),
        "device_id": event.get("device_id"),
        "rule_id": event.get("rule_id"),
        "event_type": event.get("event_type"),
        "severity": event.get("severity"),
        "risk_level": analysis.get("risk_level"),
        "summary": str(analysis.get("root_cause_hypothesis", ""))[:160],
        "recommended_action": str(analysis.get("recommended_action", ""))[:160],
    }
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        rotate_if_needed(ALERTS_FILE, ALERT_ROTATE_BYTES)
        with ALERTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        inc("alerts_total")
    except OSError as exc:
        print(f"[alert] 写 alerts.jsonl 失败：{exc}", flush=True)

    if not ALERT_WEBHOOK:
        return

    def post() -> None:
        import urllib.request

        req = urllib.request.Request(
            ALERT_WEBHOOK,
            data=json.dumps(record, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5).close()
            inc("alerts_webhook_ok")
        except Exception as exc:  # noqa: BLE001
            inc("alerts_webhook_failed")
            print(f"[alert] webhook 失败：{exc}", flush=True)

    threading.Thread(target=post, daemon=True).start()


def event_lock(event_id: str) -> threading.Lock:
    with EVENTS_LOCK:
        return EVENT_LOCKS.setdefault(event_id, threading.Lock())


# --------------------------------------------------------------------------- #
# 设备在线状态（心跳 → 离线判定）
# --------------------------------------------------------------------------- #
OFFLINE_STATUSES = {"offline", "lwt", "disconnected", "down", "false", "0"}


def _device_record(device: str, now: float) -> dict[str, Any]:
    return DEVICES.setdefault(
        device,
        {"device_id": device, "offline": False, "offline_events": 0, "last_seen": now, "info": {}},
    )


def offline_event(device: str, silent_seconds: float, reason: str) -> dict[str, Any]:
    """构造一条设备离线事件；看门狗与 MQTT LWT 共用同一份字段。"""
    return {
        "event_id": f"evt-offline-{device}-{reason}-{int(time.time())}",
        "schema_version": 1,
        "line": "line1",
        "device_id": device,
        "sensor": "sensor",
        "rule_id": "R-DEV-OFFLINE",
        "event_type": "device_offline",
        "severity": "medium",
        "triggered_at": utcnow(),
        "observed": {"value": round(silent_seconds, 1), "unit": "s", "threshold": OFFLINE_TIMEOUT},
        "window": {"samples": 0, "mean": 0, "max": round(silent_seconds, 1), "slope_per_min": 0},
        "snapshot": {"sensors": {}},
        "scenario_id": reason,
    }


def mark_device_offline(device: str, reason: str, silent_seconds: float) -> None:
    """把设备标成离线并产出离线事件（同一次离线只产一条）。"""
    now = time.time()
    with DEVICES_LOCK:
        record = _device_record(device, now)
        if record["offline"]:
            return
        record["offline"] = True
        record["offline_reason"] = reason
        record["offline_events"] = record.get("offline_events", 0) + 1
    print(f"[watchdog] {device} 离线（{reason}，静默 {silent_seconds:.0f}s）→ 生成离线事件", flush=True)
    try:
        _ingest(offline_event(device, silent_seconds, reason), source="watchdog")
    except Exception as exc:  # noqa: BLE001 - 投递失败要留痕，但不能把看门狗打死
        print(f"[watchdog] 离线事件投递失败: {exc}", flush=True)


def touch_device(payload: dict[str, Any]) -> None:
    """收到心跳：刷新 last_seen，必要时解除离线标记。

    心跳里带 `status` 字段时按其状态处理 —— ESP32 的 MQTT LWT 会在断线瞬间
    发一条 `status=offline`（retained），所以这里能比 15s 轮询更早发现掉线。
    """
    device = str(payload.get("device_id") or "unknown")
    status = str(payload.get("status", "online")).strip().lower()
    now = time.time()

    if status in OFFLINE_STATUSES:
        silent = float(payload.get("silent_seconds") or OFFLINE_TIMEOUT)
        mark_device_offline(device, str(payload.get("reason") or "lwt"), silent)
        return

    with DEVICES_LOCK:
        record = _device_record(device, now)
        was_offline = bool(record["offline"])
        record["last_seen"] = now
        record["last_seen_iso"] = utcnow()
        record["offline"] = False
        record.pop("offline_reason", None)
        info = record.setdefault("info", {})
        for key in ("ip", "rssi", "fw", "uptime_s", "source"):
            value = payload.get(key)
            if value is not None:
                info[key] = value
    if was_offline:
        inc("devices_recovered_total")
        print(f"[watchdog] {device} 心跳恢复", flush=True)


def device_snapshot() -> list[dict[str, Any]]:
    now = time.time()
    with DEVICES_LOCK:
        rows = [
            {
                "device_id": rec["device_id"],
                "online": not rec["offline"],
                "last_seen": rec.get("last_seen_iso"),
                "silent_seconds": round(now - rec.get("last_seen", now), 1),
                "offline_events": rec.get("offline_events", 0),
                "offline_reason": rec.get("offline_reason"),
                "info": dict(rec.get("info") or {}),
            }
            for rec in DEVICES.values()
        ]
    rows.sort(key=lambda row: row["device_id"])
    return rows


def device_watchdog(stop: threading.Event) -> None:
    """每 2 秒一轮：心跳静默超时 → 离线事件；待审批超时 → 升级告警。

    整个循环体包在 try 里：这个线程一旦异常退出，离线检测和审批超时会一起
    静默停摆，而且界面上完全看不出来 —— 所以宁可吞异常也要把线程留住。
    """
    while not stop.wait(2.0):
        try:
            now = time.time()
            with DEVICES_LOCK:
                candidates = [
                    (rec["device_id"], now - rec.get("last_seen", now))
                    for rec in DEVICES.values()
                    if not rec.get("offline")
                ]
            for device, silent in candidates:
                if silent > OFFLINE_TIMEOUT:
                    mark_device_offline(device, "watchdog", silent)

            # 审批超时升级：只告警，不自动批准
            if APPROVAL_TIMEOUT > 0:
                for record in events_snapshot():
                    if record.get("status") != "awaiting_approval" or record.get("escalated"):
                        continue
                    waited = now - record.get("awaiting_since", now)
                    if waited <= APPROVAL_TIMEOUT:
                        continue
                    event_id = str(record.get("event_id"))
                    _mark(event_id, escalated=True)
                    inc("approval_overdue_total")
                    emit_alert(
                        {
                            "event_id": event_id,
                            "device_id": (record.get("event") or {}).get("device_id"),
                            "rule_id": "R-APPROVAL-OVERDUE",
                            "event_type": "approval_overdue",
                            "severity": "high",
                        },
                        {
                            "risk_level": "high",
                            "root_cause_hypothesis": f"事件 {event_id} 待审批已 {waited:.0f}s，超过 {APPROVAL_TIMEOUT}s",
                            "recommended_action": "值班负责人尽快审批或转交",
                        },
                    )
                    print(f"[watchdog] {event_id} 审批超时 {waited:.0f}s，已告警", flush=True)
        except Exception as exc:  # noqa: BLE001 - 守护线程必须活下去
            inc("watchdog_errors_total")
            print(f"[watchdog] 本轮异常（线程继续）: {type(exc).__name__}: {exc}", flush=True)


def backfill_overdue_backlog() -> int:
    """启动时把「已经超时」的历史工单标记为已提醒，避免一次性刷几百条通知。

    只合并成一条汇总通知发给当班；此后新超时的工单仍然逐条升级。
    """
    count = 0
    for row in db.list_work_orders(DB_PATH, limit=500):
        if str(row.get("status")) == "closed" or row.get("overdue_notified_at"):
            continue
        sla = _sla_block(row, row.get("decided_at") or row.get("created_at"))
        if not sla.get("overdue"):
            continue
        db.update_work_order(DB_PATH, int(row["id"]), overdue_notified_at=utcnow())
        count += 1
    if count:
        title = f"启动检查：{count} 张历史工单已超时"
        body = "为避免刷屏，历史积压不逐条通知；新超时的工单会正常升级给被指派人/当班"
        for recipient in dict.fromkeys(on_duty_usernames()):
            db.add_notification(DB_PATH, recipient, "sla_backlog", title, body)
        inc("sla_backlog_total", count)
        print(f"[sla] 历史超时工单 {count} 张，已合并为一条通知", flush=True)
    return count


def check_sla_once() -> dict[str, int]:
    """跑一轮：同步排班 → 工单处置超时 → 审批超时。

    抽成独立函数是为了能被测试直接调用（不必等 30 秒一跳），也方便手动触发。
    两条升级路径都落到「当班的人」身上（这是排班表存在的意义）：
      - 工单超时：先找被指派人，未绑定账号就升级给全体当班；
      - 审批超时：直接升级给当班的人，并写 alert。
    """
    result = {"on_duty": 0, "work_order_overdue": 0, "approval_overdue": 0}
    now = datetime.now(timezone.utc)
    duty = db.sync_duty(DB_PATH, now.astimezone())
    if duty.get("configured"):
        inc("duty_sync_total")
        result["on_duty"] = len(duty.get("on_duty") or [])

    # 1) 工单处置超时
    for row in db.list_work_orders(DB_PATH, limit=300):
        if str(row.get("status")) == "closed" or row.get("overdue_notified_at"):
            continue
        sla = _sla_block(row, row.get("decided_at") or row.get("created_at"))
        if not sla.get("overdue"):
            continue
        minutes_over = max(1, abs(int(sla.get("remaining_s") or 0)) // 60)
        recipients: list[str] = []
        if row.get("assignee_username"):
            recipients.append(str(row["assignee_username"]))
        else:
            recipients.extend(on_duty_usernames())
        title = f"SLA 超时：WO-{int(row['id']):04d} 处置未完成"
        body = (
            f"{row.get('assignee') or '未指派'} · 时限 {sla['minutes']} 分钟，"
            f"已超时约 {minutes_over} 分钟（严重度 {sla['severity']}）"
        )
        for recipient in dict.fromkeys(recipients):
            db.add_notification(
                DB_PATH,
                recipient,
                "sla_overdue",
                title,
                body,
                event_id=row.get("event_id"),
                work_order_id=int(row["id"]),
            )
        db.update_work_order(DB_PATH, int(row["id"]), overdue_notified_at=utcnow())
        inc("sla_overdue_total")
        result["work_order_overdue"] += 1
        print(f"[sla] WO-{row['id']} 处置超时：{body}", flush=True)

    # 2) 审批超时（显式设置 APPROVAL_TIMEOUT_S 时由旧逻辑接管，这里跳过）
    if APPROVAL_TIMEOUT <= 0:
        for record in events_snapshot():
            if record.get("status") != "awaiting_approval" or record.get("escalated"):
                continue
            event = record.get("event") or {}
            severity = normalize_severity(event.get("severity"))
            limit_min = sla_minutes("approval", severity)
            waited = time.time() - float(record.get("awaiting_since") or time.time())
            if waited <= limit_min * 60:
                continue
            event_id = str(record.get("event_id"))
            _mark(event_id, escalated=True)
            inc("approval_overdue_total")
            result["approval_overdue"] += 1
            emit_alert(
                {
                    "event_id": event_id,
                    "device_id": event.get("device_id"),
                    "rule_id": "R-APPROVAL-OVERDUE",
                    "event_type": "approval_overdue",
                    "severity": "high",
                },
                {
                    "risk_level": "high",
                    "root_cause_hypothesis": (
                        f"事件 {event_id} 待审批 {waited / 60:.0f} 分钟，"
                        f"超过 {severity} 级时限 {limit_min} 分钟"
                    ),
                    "recommended_action": "值班负责人尽快审批或转交",
                },
            )
            title = f"审批超时：{event.get('rule_id') or '事件'} 待批 {waited / 60:.0f} 分钟"
            body = f"严重度 {severity}，审批时限 {limit_min} 分钟；事件 {event_id}"
            for recipient in dict.fromkeys(on_duty_usernames()):
                db.add_notification(
                    DB_PATH, recipient, "sla_approval", title, body, event_id=event_id
                )
            print(f"[sla] {event_id} 审批超时 {waited / 60:.0f} 分钟，已升级", flush=True)
    return result


def sla_watchdog(stop: threading.Event) -> None:
    """每 30 秒跑一次 check_sla_once；异常只记日志，绝不让线程死掉。"""
    while not stop.wait(30.0):
        try:
            check_sla_once()
        except Exception as exc:  # noqa: BLE001 - 守护线程必须活下去
            inc("sla_watchdog_errors_total")
            print(f"[sla] 本轮异常（线程继续）: {type(exc).__name__}: {exc}", flush=True)


def reconcile_pending_events(limit: int = 300) -> int:
    """把"看着待审批、其实检查点里已经没有中断"的历史事件标成 expired。

    为什么需要：老库里 status 是 pending，重启多次后检查点可能已经不在了。
    前端把它渲染成"待审批"并给出可点的按钮，用户点了只会拿到 409 ——
    与其让人反复撞墙，不如启动时如实标成"已失效"。
    只降级、不删除；判断依据是 `graph.get_state(...).next` 为空。
    """
    graph = STATE.get("app")
    if graph is None:
        return 0
    checked = expired = 0
    for row in db.list_events(DB_PATH, limit=limit):
        if row.get("status") not in ("pending", "awaiting_approval"):
            continue
        checked += 1
        try:
            snapshot = graph.get_state(ag.thread_config(row["event_id"]))
        except Exception as exc:  # noqa: BLE001 - 读检查点失败就别动它
            print(f"[reconcile] {row['event_id']} 读检查点失败，跳过：{exc}", flush=True)
            continue
        if not snapshot.next:
            db.set_event_status(DB_PATH, row["event_id"], "expired")
            expired += 1
    if expired:
        inc("events_expired_total", expired)
        print(f"[reconcile] {expired}/{checked} 条待审批事件已无中断，标为 expired", flush=True)
    return expired


def counts() -> dict[str, int]:
    return {
        "events": db.count_events(DB_PATH),
        # 待审批 = 已停在 interrupt 的事件；早期版本库里落在 pending
        "pending": db.count_events(DB_PATH, "awaiting_approval") + db.count_events(DB_PATH, "pending"),
        "work_orders": db.count_work_orders(DB_PATH),
    }


def status_payload() -> dict[str, Any]:
    mqtt = STATE["mqtt"]
    return {
        "mqtt": mqtt.status() if mqtt else {"connected": False, "available": False},
        "analyzer": STATE["analyzer_source"],
        "model": STATE["analyzer_model"],
        "counts": counts(),
        "subscribers": BUS.subscriber_count + WSBUS.subscriber_count,
        "memory_events": len(EVENTS),
        "checkpointer": STATE.get("checkpointer", "memory"),
        "devices": device_snapshot(),
        "offline_timeout_s": OFFLINE_TIMEOUT,
        "approval_timeout_s": APPROVAL_TIMEOUT,
        # 审批与 AI 解耦：规则卡先出，AI 后补
        "analysis_mode": "rule-card+ai",
        "analysis_pending": sum(1 for e in events_snapshot() if e.get("analysis_pending")),
        # SLA 分级 + 当前值班（前端据此显示倒计时/超时）
        "sla": SLA,
        "on_duty": on_duty_usernames(),
    }


def order_severity(row: dict[str, Any]) -> str:
    """工单的严重度：优先取事件分析里的 risk_level，退回事件 severity，最后按 high。"""
    content = row.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    analysis = (content or {}).get("analysis") or {}
    if isinstance(analysis, dict) and analysis.get("risk_level"):
        return normalize_severity(analysis.get("risk_level"))
    event_row = db.get_event(DB_PATH, str(row.get("event_id") or ""))
    if event_row:
        return normalize_severity((event_row.get("event") or {}).get("severity"))
    return "high"


def _sla_block(row: dict[str, Any], base_iso: str | None) -> dict[str, Any]:
    """处置时限：从批准时间（没有就用创建时间）起算。"""
    severity = order_severity(row)
    minutes = sla_minutes("handle", severity)
    block: dict[str, Any] = {"kind": "handle", "severity": severity, "minutes": minutes}
    base = _parse_iso(base_iso)
    if base is None:
        return block
    deadline = base + timedelta(minutes=minutes)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    closed = str(row.get("status")) == "closed"
    block.update(
        {
            "deadline": deadline.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "remaining_s": int(remaining),
            "overdue": (remaining < 0) and not closed,
        }
    )
    return block


def on_duty_usernames() -> list[str]:
    """当前当班、且绑定了账号的人（SLA 升级的通知对象）。"""
    return [
        str(row["username"])
        for row in db.list_staff(DB_PATH, active_only=True)
        if int(row.get("on_duty") or 0) and row.get("username")
    ]


def _work_order_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload = dict(row)
    if isinstance(payload.get("content"), str):
        try:
            payload["content"] = json.loads(payload["content"])
        except json.JSONDecodeError:
            pass
    # verification 在库里是 JSON 文本（db.list_work_orders 会解析，单行取出来不会）
    if isinstance(payload.get("verification"), str):
        try:
            payload["verification"] = json.loads(payload["verification"])
        except json.JSONDecodeError:
            pass
    approval = db.get_approval(DB_PATH, payload.get("event_id", ""))
    if approval:
        payload["approver"] = approval.get("approver")
        payload["approval_comment"] = approval.get("comment")
        payload["decided_at"] = approval.get("decided_at")
        payload["approval_reason"] = approval.get("reason_code")
        payload["approval_reason_label"] = reject_reason_label(approval.get("reason_code"))
    payload["sla"] = _sla_block(row, payload.get("decided_at") or row.get("created_at"))
    return payload


def normalize_settings(raw: dict[str, Any] | None, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """按白名单 + 范围归一化设置；非法值直接 422，不做静默降级。"""
    merged = {**DEFAULT_SETTINGS, **(current or {})}
    for key, value in (raw or {}).items():
        spec = SETTINGS_SPEC.get(key)
        if spec is None:
            continue  # 未知键忽略，避免前端拼错把脏数据写进库
        kind = spec["kind"]
        if kind == "bool":
            if isinstance(value, bool):
                merged[key] = value
            elif isinstance(value, (int, str)) and str(value).strip().lower() in ("1", "0", "true", "false"):
                merged[key] = str(value).strip().lower() in ("1", "true")
            else:
                raise HTTPException(status_code=422, detail=f"{key} 必须是布尔值")
        elif kind == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{key} 必须是整数") from None
            if "choices" in spec and number not in spec["choices"]:
                raise HTTPException(status_code=422, detail=f"{key} 只能是 {list(spec['choices'])}")
            if number < spec.get("min", number) or number > spec.get("max", number):
                raise HTTPException(
                    status_code=422, detail=f"{key} 超出范围 {spec.get('min')}–{spec.get('max')}"
                )
            merged[key] = number
        elif kind == "str":
            text = str(value).strip()
            if "choices" in spec and text not in spec["choices"]:
                raise HTTPException(status_code=422, detail=f"{key} 只能是 {list(spec['choices'])}")
            merged[key] = text
    return merged


def settings_for(user: dict[str, Any]) -> dict[str, Any]:
    """取该用户生效的设置（DB 里的值 ∪ 默认值）。服务令牌没有 user_id，直接给默认值。"""
    user_id = user.get("id")
    if not user_id:
        return dict(DEFAULT_SETTINGS)
    return normalize_settings(db.get_user_settings(DB_PATH, int(user_id)))


def reject_reason_label(code: str | None) -> str:
    return REJECT_REASONS.get(str(code or ""), REJECT_REASONS["other"])


def verify_recovery(event_id: str, window_minutes: int) -> dict[str, Any]:
    """关闭工单前的「处置后验证」。

    两类事件分开判：
      - 设备离线：看设备现在是否在线（而不是翻遥测）；
      - 其余事件：看该设备该测点最近 window 分钟内是否已回落到阈值以下。
    没有任何遥测样本时**不能**当作"已恢复"，一律要求例外理由。
    """
    row = db.get_event(DB_PATH, event_id)
    if not row:
        return {
            "ok": False,
            "mode": "unknown",
            "message": "找不到该事件的历史记录（后端清过库？），无法验证是否已恢复",
        }

    event = row.get("event") or {}
    device = event.get("device_id")
    sensor = event.get("sensor")
    observed = event.get("observed") or {}
    threshold = observed.get("threshold")
    unit = observed.get("unit") or ""

    if event.get("event_type") == "device_offline":
        snapshot = {d["device_id"]: d for d in device_snapshot()}
        record = snapshot.get(str(device))
        online = bool(record and record["online"])
        return {
            "ok": online,
            "mode": "device_online",
            "device_id": device,
            "online": online,
            "message": "设备已重新上线" if online else f"设备 {device} 仍不在线",
        }

    if not device or not sensor:
        return {
            "ok": False,
            "mode": "no_target",
            "message": "事件里没有设备/测点信息，无法验证",
        }

    stats = db.telemetry_window(DB_PATH, str(device), str(sensor), int(window_minutes))
    base = {
        "mode": "telemetry",
        "device_id": device,
        "sensor": sensor,
        "window_minutes": int(window_minutes),
        "threshold": threshold,
        "unit": unit,
        **stats,
    }
    if not stats["samples"]:
        return {
            **base,
            "ok": False,
            "reason": "no_data",
            "message": f"最近 {window_minutes} 分钟没有 {sensor} 的遥测，无法确认已恢复",
        }

    if threshold is None:
        return {**base, "ok": True, "message": f"最近 {window_minutes} 分钟有 {stats['samples']} 个样本（该规则无阈值）"}

    try:
        limit = float(threshold)
        peak = float(stats["max"])
    except (TypeError, ValueError):
        return {**base, "ok": True, "message": "阈值不可解析，跳过数值判定"}

    ok = peak < limit
    if ok:
        message = f"最近 {window_minutes} 分钟峰值 {peak:g}{unit} < 阈值 {limit:g}{unit}，已恢复"
    else:
        message = f"最近 {window_minutes} 分钟峰值 {peak:g}{unit} ≥ 阈值 {limit:g}{unit}，尚未恢复"
    return {**base, "ok": ok, "max": peak, "message": message}


def resolve_citations(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    citations = []
    for clause_id in analyzer.extract_clause_ids(analysis.get("regulation_basis", "")):
        clause = regulation_store.get_clause(clause_id)
        if clause:
            citations.append(
                {"clause_id": clause["clause_id"], "title": clause["title"], "text": clause["text"]}
            )
    return citations


def collect_tools(event: dict[str, Any], calls: list[str]) -> dict[str, Any]:
    """Agent 的只读工具：设备在线状态、同设备近期事件。写操作仍走 interrupt。"""
    out: dict[str, Any] = {}
    device = event.get("device_id")

    if "get_device_status" in calls:
        snapshot = device_snapshot()
        out["device"] = next(
            (d for d in snapshot if d["device_id"] == device),
            {"device_id": device, "online": None, "note": "无该设备心跳记录"},
        )

    if "get_recent_events" in calls:
        recent = []
        for row in db.list_events(DB_PATH, limit=30):
            ev = row.get("event") or {}
            if device and ev.get("device_id") != device:
                continue
            recent.append(
                {
                    "event_id": row["event_id"],
                    "rule_id": ev.get("rule_id"),
                    "status": row["status"],
                    "received_at": row.get("received_at"),
                }
            )
            if len(recent) >= 5:
                break
        out["recent_events"] = recent

    inc("tool_calls_total")
    return out


def pick_analyzer() -> tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """选择分析后端：Ollama → 本地 transformers → 离线模板。"""
    mode = os.environ.get("LLM_BACKEND", "auto")

    if mode in ("auto", "ollama"):
        try:
            model = llm_client.ensure_ready()
            if llm_client.is_stub():
                return "ollama-stub", model, analyzer.analyze_event
            return "ollama", model, analyzer.analyze_event
        except Exception:  # noqa: BLE001
            if mode == "ollama":
                return "offline-template", "—", offline_analyzer.analyze

    if mode in ("auto", "local"):
        import local_llm

        if local_llm.available():
            llm_client.set_backend("local")
            return "local-qwen", local_llm.model_name(), analyzer.analyze_event

    return "offline-template", "—", offline_analyzer.analyze


# --------------------------------------------------------------------------- #
# 管线：事件 → 规则处置卡（立刻可审批）→ 异步 AI 补充意见
# --------------------------------------------------------------------------- #
def rule_card(event: dict[str, Any]) -> dict[str, Any]:
    """规则处置卡：不调模型，毫秒级产出，让审批不必等 AI。

    内容全部来自确定性信息：事件数值 + 检索到的条款 + 事件类型对应的处置模板。
    检索失败也不抛错 —— 卡片宁可少一条依据，也不能挡住安全动作。
    """
    observed = event.get("observed") or {}
    sensor = str(event.get("sensor") or "测点")
    value = observed.get("value")
    unit = observed.get("unit") or ""
    threshold = observed.get("threshold")
    event_type = str(event.get("event_type") or "threshold_exceeded")
    rule_id = str(event.get("rule_id") or "—")
    severity = str(event.get("severity") or "high").strip().lower()
    if severity not in ("low", "medium", "high"):
        severity = "high"

    clauses: list[dict[str, Any]] = []
    try:
        # 索引还没预热好就走纯词法（毫秒级）：规则卡绝不等模型加载
        retrieval_mode = None if regulation_store.ready() else "lexical"
        clauses = regulation_store.search_regulation(
            analyzer.build_query(event), top_k=3, mode=retrieval_mode
        )
    except Exception as exc:  # noqa: BLE001 - 检索不可用也不能挡住审批
        inc("rule_card_retrieval_failed_total")
        print(f"[rule-card] 检索失败，退回无条件模板：{exc}", flush=True)

    basis = "；".join(f"{c['clause_id']}：{c['title']}" for c in clauses[:2]) or "（未检索到可用条款）"
    first = clauses[0]["clause_id"] if clauses else None
    second = clauses[1]["clause_id"] if len(clauses) > 1 else first

    def cite(clause_id: str | None) -> str:
        return f"（{clause_id}）" if clause_id else ""

    if event_type == "device_offline":
        cause = f"设备 {event.get('device_id', '—')} 心跳中断，数据不可信（{rule_id}）。"
        action = (
            f"立即停止该区域作业，改用人工检测并挂牌警示{cite(first)}；"
            f"恢复通信后先复查数据有效性，再恢复自动监测{cite(second)}。"
        )
    elif event_type == "gas_surge":
        cause = f"{sensor} 短时间快速上升至 {value}{unit}（{rule_id}），优先怀疑瓦斯涌出或通风异常。"
        action = (
            f"立即停止作业、撤出人员、加强通风{cite(first)}；"
            f"复测 {sensor} 并经人工批准后方可恢复作业{cite(second)}。"
        )
    else:
        cause = f"{sensor} 实测 {value}{unit}，达到/超过阈值 {threshold}{unit}（{rule_id}）。"
        action = (
            f"立即停止相关作业并撤出人员{cite(first)}；"
            f"切断非本质安全型电源并复测 {sensor}{cite(second)}；"
            f"复测合格并经人工批准后方可恢复{cite(first)}。"
        )

    return {
        "root_cause_hypothesis": f"[规则处置卡] {cause}",
        "regulation_basis": basis,
        "recommended_action": action,
        "risk_level": severity,
    }


def enrich_with_ai(event_id: str, event: dict[str, Any]) -> None:
    """AI 补充意见（后台线程，不在审批关键路径上）。"""
    started = time.perf_counter()
    try:
        analysis, source = _analyze_with_timeout(event)
    except Exception as exc:  # noqa: BLE001
        inc("analysis_failed_total")
        _mark(event_id, analysis_error=f"{type(exc).__name__}: {exc}", analysis_pending=False)
        broadcast(
            {
                "type": "analysis_error",
                "event_id": event_id,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
                "approval_unaffected": True,
                "counts": counts(),
            }
        )
        return

    elapsed = round(time.perf_counter() - started, 1)
    citations = resolve_citations(analysis)
    db.save_event_analysis(DB_PATH, event_id, analysis, citations)
    record = event_record(event_id) or {}
    decided = str(record.get("status")) in ("approved", "rejected")
    _mark(
        event_id,
        analysis=analysis,
        citations=citations,
        analysis_source=source,
        analysis_pending=False,
        analysis_seconds=elapsed,
        analysis_late=decided,
    )
    inc("analysis_total")
    inc("analysis_seconds", elapsed)
    broadcast(
        {
            "type": "analysis",
            "event_id": event_id,
            "event": event,
            "analysis": analysis,
            "citations": citations,
            "analysis_source": source,
            "analysis_seconds": elapsed,
            "analysis_pending": False,
            "analysis_late": decided,
            "status": "pending",
            "counts": counts(),
        }
    )


def process_event(event: dict[str, Any]) -> None:
    """事件 → 规则处置卡 → 立刻停在审批点 →（后台）AI 补充意见。

    为什么这么排：本地 3B 单次 55–100 s，而高危事件恰恰最需要秒级响应。
    以前「分析完才能审批」，等于最危险的场景最慢。现在：
      1. 确定性规则卡先出，事件立刻推进到 awaiting_approval；
      2. AI 分析丢到后台线程，到货后追加一帧，失败/超时都不影响审批。
    """
    event_id = event["event_id"]
    graph = STATE["app"]

    _mark(event_id, status="queued")
    # 新入库一律写 analyzing（"还在算"），awaiting_approval 才是"可以审批"。
    # 老库里遗留的 pending 是有歧义的中间态，前端按"待审批"兼容处理。
    db.upsert_event(DB_PATH, event_id, event, status="analyzing")
    inc("events_ingested_total")
    broadcast({"type": "event", "event": event, "event_id": event_id, "counts": counts()})

    card = rule_card(event)
    citations = resolve_citations(card)
    inc("rule_card_total")

    try:
        with PIPELINE_LOCK:  # 只锁图操作；LLM 推理在别的线程，不占这把锁
            graph.invoke({"event": event, "analysis": card}, ag.thread_config(event_id))
    except Exception as exc:  # noqa: BLE001
        inc("graph_failed_total")
        _mark(event_id, status="graph_failed", error=str(exc))
        db.set_event_status(DB_PATH, event_id, "graph_failed")
        broadcast(
            {
                "type": "analysis_error",
                "event_id": event_id,
                "error": f"图执行失败: {exc}",
                "retryable": True,
                "counts": counts(),
            }
        )
        return

    db.save_event_analysis(DB_PATH, event_id, card, citations)
    # 数据库状态同步推进到 awaiting_approval。否则"还在分析中"的事件在看板上
    # 也显示成"待审批"，点了批准只会拿到 409；重试成功后状态同样不会回滚。
    db.set_event_status(DB_PATH, event_id, "awaiting_approval")
    _mark(
        event_id,
        status="awaiting_approval",
        analysis=card,
        citations=citations,
        analysis_source="rule-card",
        analysis_pending=True,
        analysis_seconds=0.0,
        awaiting_since=time.time(),
    )
    broadcast(
        {
            "type": "analysis",
            "event_id": event_id,
            "event": event,
            "analysis": card,
            "citations": citations,
            "analysis_source": "rule-card",
            "analysis_pending": True,
            "analysis_seconds": 0.0,
            "status": "pending",
            "counts": counts(),
        }
    )

    if event.get("severity") == "high" or event.get("event_type") == "device_offline":
        emit_alert(event, card)

    # AI 补充意见：不阻塞审批，也不改变审批状态
    ANALYSIS_POOL.submit(_safe_call, "enrich_with_ai", enrich_with_ai, event_id, event)


def _analyze_with_timeout(event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """并发上限 + 可选超时降级：LLM 太慢时退回离线模板，不把演示卡死。

    注意：这里只负责"算"，不再改事件状态 —— 状态由 process_event 负责，
    否则后台分析会把已经 awaiting_approval 的事件又打回 analyzing。
    """
    with ANALYSIS_SEM:
        if ANALYSIS_TIMEOUT <= 0:
            return STATE["analyze"](event), STATE["analyzer_source"]

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(STATE["analyze"], event)
        try:
            result = future.result(timeout=ANALYSIS_TIMEOUT)
            pool.shutdown(wait=False)
            return result, STATE["analyzer_source"]
        except FuturesTimeout:
            inc("analysis_timeout_total")
            print(
                f"[analyze] 超过 {ANALYSIS_TIMEOUT}s 未完成，降级为离线模板：{event['event_id']}",
                flush=True,
            )
            pool.shutdown(wait=False, cancel_futures=True)
            return offline_analyzer.analyze(event), "offline-fallback"


def _safe_call(name: str, fn: Callable[..., Any], *args: Any) -> None:
    """线程池任务的兜底包装。

    ThreadPoolExecutor 会把异常存在 Future 里，没人 get() 就完全静默 ——
    表现就是"事件卡在 analyzing，日志里什么都没有"。这里统一打印+计数。
    """
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常吞掉
        import traceback

        inc(f"{name}_errors_total")
        print(
            f"[{name}] 未捕获异常：{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            flush=True,
        )


def _mark(event_id: str, **fields: Any) -> None:
    with EVENTS_LOCK:
        record = EVENTS.setdefault(event_id, {"event_id": event_id, "thread_id": event_id})
        record.update(fields)


# --------------------------------------------------------------------------- #
# 审批（幂等）
# --------------------------------------------------------------------------- #
class DecisionBody(BaseModel):
    approver: str | None = None
    comment: str | None = None
    reason_code: str | None = None


def _normalize_approver(value: str | None) -> str:
    name = (value or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="必须填写审批人（至少 2 个字符），审批不可匿名")
    return name[:60]


def _decide(
    event_id: str,
    decision: str,
    approver: str | None = None,
    comment: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """approve/reject 的公共实现；同一 event_id 重复调用不会产生第二张工单。

    reason_code 只对驳回有意义：把"为什么驳回"变成可统计的枚举，
    否则误报率永远是拍脑袋。API 层留了默认值，老脚本不传也不会挂。
    """
    graph = STATE["app"]
    config = ag.thread_config(event_id)
    note = (comment or "").strip()[:500] or None
    code = str(reason_code or "other").strip().lower()
    if decision == "reject" and code not in REJECT_REASONS:
        raise HTTPException(
            status_code=422,
            detail=f"非法 reason_code: {code!r}，可选 {list(REJECT_REASONS)}",
        )

    with event_lock(event_id), PIPELINE_LOCK:
        existing = db.get_work_order(DB_PATH, event_id)
        if decision == "approve" and existing:
            _mark(event_id, status="approved", work_order_id=existing["id"])
            return {
                "ok": True,
                "duplicate": True,
                "decision": "approve",
                "event_id": event_id,
                "thread_id": event_id,
                "approver": (db.get_approval(DB_PATH, event_id) or {}).get("approver"),
                "work_order": _work_order_payload(existing),
            }

        snapshot = graph.get_state(config)
        if not snapshot.next:
            record = event_record(event_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "该事件不在待审批状态",
                    "event_id": event_id,
                    "known_status": (record or {}).get("status"),
                    "has_work_order": bool(existing),
                },
            )

        # 真正要落审批记录时才要求审批人：重复调用/状态冲突优先返回 409
        who = _normalize_approver(approver)

        # 四眼原则：手工注入该事件的人不能自己批准
        if decision == "approve":
            record = event_record(event_id) or {}
            origin_user = (record.get("event") or {}).get("origin_user")
            if origin_user and origin_user == who:
                raise HTTPException(status_code=403, detail="不能审批自己注入的事件，请换一个账号")

        result = graph.invoke(Command(resume=decision), config)
        status = "approved" if decision == "approve" else "rejected"
        db.set_event_status(DB_PATH, event_id, status)

        if decision == "approve":
            row = db.get_work_order(DB_PATH, event_id)
            approval = db.record_approval(
                DB_PATH,
                event_id,
                decision="approved",
                approver=who,
                comment=note,
                work_order_id=row["id"] if row else None,
            )
            _mark(event_id, status="approved", work_order_id=row["id"] if row else None)
            broadcast(
                {
                    "type": "work_order",
                    "event_id": event_id,
                    "work_order": _work_order_payload(row),
                    "approval": approval,
                    "counts": counts(),
                }
            )
            return {
                "ok": True,
                "duplicate": False,
                "decision": "approve",
                "event_id": event_id,
                "thread_id": event_id,
                "approver": who,
                "comment": note,
                "work_order": _work_order_payload(row),
            }

        approval = db.record_approval(
            DB_PATH,
            event_id,
            decision="rejected",
            approver=who,
            comment=note,
            work_order_id=None,
            reason_code=code,
        )
        _mark(event_id, status="rejected")
        broadcast(
            {
                "type": "rejected",
                "event_id": event_id,
                "approval": approval,
                "reason_code": code,
                "reason_label": reject_reason_label(code),
                "counts": counts(),
            }
        )
        return {
            "ok": True,
            "duplicate": False,
            "decision": "reject",
            "event_id": event_id,
            "thread_id": event_id,
            "approver": who,
            "comment": note,
            "reason_code": code,
            "reason_label": reject_reason_label(code),
            "work_order": None,
            "state": result.get("approval_status"),
        }


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    loop = asyncio.get_running_loop()
    BUS.bind_loop(loop)
    WSBUS.bind_loop(loop)
    db.init_db(DB_PATH)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    source, model, analyze_fn = pick_analyzer()

    # 断点持久化：进程重启后仍能从 interrupt() 处恢复审批
    ckpt_path = os.environ.get("CHECKPOINT_DB") or str(Path(DB_PATH).with_name("checkpoints.db"))
    Path(ckpt_path).parent.mkdir(parents=True, exist_ok=True)
    ckpt_conn = sqlite3.connect(ckpt_path, check_same_thread=False)
    ckpt_conn.execute("PRAGMA journal_mode = WAL")
    checkpointer = SqliteSaver(ckpt_conn)

    STATE.update(
        {
            "app": ag.build_app(
                DB_PATH,
                checkpointer=checkpointer,
                analyze_fn=analyze_fn,
                tools_fn=collect_tools,
                # P0-4：默认把分析挪出审批图；想回到"分析完再审批"就设 DEFER_ANALYSIS=0
                defer_analysis=DEFER_ANALYSIS,
            ),
            "analyzer_source": source,
            "analyzer_model": model,
            "analyze": analyze_fn,
            "checkpointer": "sqlite",
            "checkpoint_db": ckpt_path,
            "ckpt_conn": ckpt_conn,
        }
    )

    # 启动时对账：把“看着待审批、检查点里其实没有中断”的历史事件标成 expired
    reconcile_pending_events()
    # 先按排班同步当班，再把历史超时工单合并成一条通知（避免首启刷屏）
    db.sync_duty(DB_PATH, datetime.now().astimezone())
    backfill_overdue_backlog()

    mqtt = MqttBridge(
        MQTT_HOST,
        MQTT_PORT,
        MQTT_TOPIC,
        lambda ev: _ingest(ev, source="mqtt"),
        heartbeat_topic=HEARTBEAT_TOPIC,
        on_heartbeat=touch_device,
    )
    mqtt.start()
    STATE["mqtt"] = mqtt

    watchdog = threading.Thread(target=device_watchdog, args=(_DEVICE_STOP,), name="device-watchdog", daemon=True)
    watchdog.start()

    sla_thread = threading.Thread(target=sla_watchdog, args=(_DEVICE_STOP,), name="sla-watchdog", daemon=True)
    sla_thread.start()

    # 预热检索索引：否则第一条事件要现场加载 embedding 模型，规则卡会慢几秒
    def _warmup() -> None:
        try:
            regulation_store.warmup()
            print("[backend] 规程索引已预热", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[backend] 索引预热失败（不影响启动）：{exc}", flush=True)

    threading.Thread(target=_warmup, name="retrieval-warmup", daemon=True).start()

    print(f"[backend] db={DB_PATH}")
    print(f"[backend] analyzer={source} model={model}")
    print(
        f"[auth] 审批人注册邀请码：{APPROVER_INVITE}（来源 {APPROVER_INVITE_SOURCE}；"
        f"文件 {APPROVER_INVITE_FILE}）",
        flush=True,
    )
    print(f"[backend] mqtt={MQTT_HOST}:{MQTT_PORT} topic={MQTT_TOPIC} heartbeat={HEARTBEAT_TOPIC}")
    print(f"[backend] http://127.0.0.1:{HTTP_PORT}  ws://127.0.0.1:{HTTP_PORT}/ws")
    print(f"[backend] ingest={'local-trusted' if ALLOW_LOCAL_INGEST else 'token-only'} "
          f"event_workers={EVENT_WORKERS} analysis_timeout={ANALYSIS_TIMEOUT or 'off'} "
          f"mode=rule-card+ai")
    try:
        yield
    finally:
        _DEVICE_STOP.set()
        mqtt.stop()
        EVENT_POOL.shutdown(wait=False, cancel_futures=True)
        ANALYSIS_POOL.shutdown(wait=False, cancel_futures=True)
        try:
            ckpt_conn.close()
        except Exception:  # noqa: BLE001 - 关库失败不该影响退出
            pass


def _ingest(event: dict[str, Any], source: str = "http", origin_user: str | None = None) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise HTTPException(status_code=422, detail="事件必须是 JSON 对象")
    event_id = str(event.get("event_id") or f"evt-{uuid.uuid4().hex[:12]}")
    event["event_id"] = event_id

    # 来源标记：规则引擎 / 看门狗 / 手工直投。前端据此渲染徽标，
    # 避免把联调用的直投事件误当成真实规则产出。
    origin = event.get("origin") or {
        "rule_engine": "rule_engine",
        "mqtt": "rule_engine",
        "watchdog": "watchdog",
        "dev": "manual",
    }.get(source, source)
    event["origin"] = origin
    if origin_user:
        event["origin_user"] = origin_user
    if origin == "manual":
        inc("manual_injected_total")

    with EVENTS_LOCK:
        if event_id in EVENTS:
            return {"accepted": False, "duplicate": True, "event_id": event_id, "thread_id": event_id}
        EVENTS[event_id] = {
            "event_id": event_id,
            "thread_id": event_id,
            "event": event,
            "status": "received",
            "source": source,
            "origin": origin,
            "received_at": utcnow(),
        }
        _trim_events_locked()

    EVENT_POOL.submit(_safe_call, "process_event", process_event, event)
    return {"accepted": True, "duplicate": False, "event_id": event_id, "thread_id": event_id}


MAX_MEMORY_EVENTS = int(os.environ.get("MAX_MEMORY_EVENTS", "800"))


def _trim_events_locked() -> None:
    """EVENTS 只保留最近 N 条（终止态优先淘汰），避免长跑内存无限增长。"""
    if len(EVENTS) <= MAX_MEMORY_EVENTS:
        return
    terminal = {"approved", "rejected", "analysis_failed", "graph_failed"}
    removable = [
        (record.get("received_at") or "", event_id)
        for event_id, record in EVENTS.items()
        if record.get("status") in terminal
    ]
    removable.sort()
    for _, event_id in removable[: len(EVENTS) - MAX_MEMORY_EVENTS]:
        EVENTS.pop(event_id, None)
        # 锁对象也要一起回收，否则 EVENT_LOCKS 只增不减（长跑必泄漏）
        EVENT_LOCKS.pop(event_id, None)
    if len(EVENTS) > MAX_MEMORY_EVENTS:
        inc("events_memory_overflow_total")


app = FastAPI(title="安全监测 Agent Demo", version="5.0", lifespan=lifespan)
# 允许的前端来源：默认 3000；换端口调试时用 CORS_ORIGINS 覆盖（逗号分隔）
CORS_ORIGINS = [
    item.strip()
    for item in os.environ.get(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# 登录 / 注册
# --------------------------------------------------------------------------- #
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.\-]{3,32}$")


class AuthBody(BaseModel):
    username: str
    password: str
    role: str | None = None         # 注册时选：approver / engineer；登录时忽略
    invite_code: str | None = None  # 注册审批人时必填


def _issue_token(user: dict[str, Any]) -> dict[str, Any]:
    token = auth.new_token()
    session = db.create_session(DB_PATH, token, int(user["id"]), AUTH_TTL_DAYS)
    return {"token": token, "expires_at": session["expires_at"], "user": user}


def issue_stream_ticket(user: dict[str, Any]) -> dict[str, Any]:
    """签发一枚短时订阅票据。

    EventSource / WebSocket 都没法带 Authorization，这里换成一次性签发、
    有效期内可重复使用的票据；票据只存在内存里，重启即失效。
    """
    ticket = auth.new_token()
    now = time.time()
    with STREAM_TICKETS_LOCK:
        for stale in [k for k, v in STREAM_TICKETS.items() if v["expires"] < now]:
            STREAM_TICKETS.pop(stale, None)
        STREAM_TICKETS[ticket] = {
            "user_id": user.get("id"),
            "username": user.get("username"),
            "expires": now + TICKET_TTL,
        }
    return {"ticket": ticket, "expires_in": int(TICKET_TTL), "username": user.get("username")}


def consume_stream_ticket(ticket: str | None) -> dict[str, Any] | None:
    if not ticket:
        return None
    now = time.time()
    with STREAM_TICKETS_LOCK:
        entry = STREAM_TICKETS.get(ticket)
        if entry is None:
            return None
        if entry["expires"] < now:
            STREAM_TICKETS.pop(ticket, None)
            return None
        return dict(entry)


@app.post("/api/auth/stream-ticket", dependencies=[Depends(require_approver)])
def stream_ticket(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """给 SSE/WS 换一张订阅票据（需先登录）。"""
    return issue_stream_ticket(user)


@app.post("/api/auth/register")
def register(body: AuthBody) -> dict[str, Any]:
    """注册。role 由注册人自己选：approver（审批人）/ engineer（工程师）。

    审批人必须是真实注册的账号——系统不预置、也不自动生成审批人。
    """
    username = body.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=422, detail="用户名 3–32 位，仅限字母/数字/下划线/汉字/点/横线")
    bad_password = auth.validate_password(body.password)
    if bad_password:
        raise HTTPException(status_code=422, detail=bad_password)
    role = str(body.role or ROLE_ENGINEER).strip().lower()
    if role not in (ROLE_APPROVER, ROLE_ENGINEER):
        raise HTTPException(
            status_code=422, detail="身份只能是 approver（审批人）或 engineer（工程师）"
        )
    # 审批人必须有邀请码：审批人只能注册，不能被"自称"出来
    if role == ROLE_APPROVER:
        supplied = str(body.invite_code or "").strip()
        if not supplied or not secrets.compare_digest(supplied, APPROVER_INVITE):
            inc("approver_invite_rejected_total")
            print(f"[auth] 审批人邀请码校验失败：{username}", flush=True)
            raise HTTPException(
                status_code=403,
                detail=(
                    "审批人邀请码不正确。首次启动的邀请码会打印在后端控制台，"
                    f"也写在 {APPROVER_INVITE_FILE}"
                ),
            )
    if db.get_user(DB_PATH, username):
        raise HTTPException(status_code=409, detail="用户名已存在，请直接登录")

    salt, hashed = auth.hash_password(body.password)
    user = db.create_user(DB_PATH, username, salt, hashed, role=role)
    inc("users_registered_total")
    inc(f"users_registered_{role}_total")
    return _issue_token(user)


@app.post("/api/auth/login")
def login(body: AuthBody) -> dict[str, Any]:
    username = body.username.strip()
    # 限流：连续失败 5 次锁 60s，之后每次翻倍，最多 15 分钟
    state = db.get_login_state(DB_PATH, username)
    if state and state.get("locked_until") and str(state["locked_until"]) > utcnow():
        raise HTTPException(
            status_code=429,
            detail=f"失败次数过多，账号已临时锁定至 {state['locked_until']}（UTC），请稍后再试",
        )

    user = db.get_user(DB_PATH, username)
    if not user or not auth.verify_password(body.password, user["password_salt"], user["password_hash"]):
        info = db.record_login_failure(DB_PATH, username)
        inc("login_failed_total")
        suffix = f"，已失败 {info['failed']} 次" if info["failed"] < 5 else "，账号已临时锁定"
        raise HTTPException(status_code=401, detail=f"用户名或密码错误{suffix}")

    db.clear_login_failures(DB_PATH, username)
    inc("users_login_total")
    return _issue_token({"id": user["id"], "username": user["username"], "role": user["role"]})


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    return {"user": user}


@app.get("/api/auth/invite", dependencies=[Depends(require_approver)])
def read_invite() -> dict[str, Any]:
    """查看当前审批人邀请码（仅审批人）。"""
    return {
        "invite_code": APPROVER_INVITE,
        "source": APPROVER_INVITE_SOURCE,
        "file": str(APPROVER_INVITE_FILE),
    }


class InviteSetBody(BaseModel):
    code: str


# 邀请码允许的字符：字母/数字/下划线/横线，4–32 位（方便口头传达，不会打出奇怪符号）
INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")


def _persist_invite(code: str, user: dict[str, Any], action: str) -> dict[str, Any]:
    """落盘 + 内存生效 + 审计。环境变量控制时拒绝（以环境变量为准）。"""
    global APPROVER_INVITE, APPROVER_INVITE_SOURCE
    if APPROVER_INVITE_SOURCE == "env":
        raise HTTPException(
            status_code=409,
            detail="邀请码由环境变量 APPROVER_INVITE_CODE 控制，页面改不动；请改环境变量后重启",
        )
    try:
        APPROVER_INVITE_FILE.parent.mkdir(parents=True, exist_ok=True)
        APPROVER_INVITE_FILE.write_text(code + "\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入邀请码失败：{exc}") from None
    APPROVER_INVITE = code
    APPROVER_INVITE_SOURCE = "file"
    db.upsert_audit(
        DB_PATH,
        "approver_invite",
        f"{action}@{utcnow()}",
        json.dumps({"by": user["username"], "code": code}, ensure_ascii=False),
    )
    print(f"[auth] 审批人邀请码已{action}（by {user['username']}）：{code}", flush=True)
    return {"invite_code": code, "source": "file", "updated_by": user["username"]}


@app.post("/api/auth/invite/set", dependencies=[Depends(require_approver)])
def set_invite(body: InviteSetBody, user: dict[str, Any] = Depends(require_approver)) -> dict[str, Any]:
    """审批人自己指定邀请码（旧码立即失效）。"""
    code = (body.code or "").strip()
    if not INVITE_CODE_RE.match(code):
        raise HTTPException(
            status_code=422,
            detail="邀请码 4–32 位，只能包含字母、数字、下划线、横线（例如 A-WANGWU-2026）",
        )
    result = _persist_invite(code, user, "set")
    inc("approver_invite_set_total")
    return result


@app.post("/api/auth/invite/rotate", dependencies=[Depends(require_approver)])
def rotate_invite(user: dict[str, Any] = Depends(require_approver)) -> dict[str, Any]:
    """随机轮换邀请码：旧码立即失效（环境变量控制时不允许在页面里轮换）。"""
    result = _persist_invite(_new_invite_code(), user, "rotated")
    inc("approver_invite_rotated_total")
    return result


@app.get("/api/auth/sessions")
def my_sessions(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    if not user.get("id"):
        return {"sessions": []}
    return {"sessions": db.list_sessions(DB_PATH, int(user["id"]))}


@app.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _bearer(authorization)
    if token and token != API_TOKEN:
        db.delete_session(DB_PATH, token)
    return {"ok": True}


@app.post("/api/auth/logout-all")
def logout_all(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """吊销该用户全部会话（换机器/怀疑泄露时用）。"""
    removed = db.delete_user_sessions(DB_PATH, int(user["id"])) if user.get("id") else 0
    return {"ok": True, "revoked": removed}


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/password")
def change_password(
    body: PasswordBody,
    user: dict[str, Any] = Depends(require_auth),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """改密码：校验旧密码 → 写新哈希 → 吊销该用户其它会话（当前这枚保留）。"""
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="服务令牌不能改密码，请用普通账号登录")
    record = db.get_user(DB_PATH, str(user["username"]))
    if not record:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not auth.verify_password(body.old_password, record["password_salt"], record["password_hash"]):
        raise HTTPException(status_code=401, detail="旧密码不正确")
    problem = auth.validate_password(body.new_password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if body.new_password == body.old_password:
        raise HTTPException(status_code=422, detail="新密码不能与旧密码相同")

    salt, hashed = auth.hash_password(body.new_password)
    db.update_password(DB_PATH, int(user["id"]), salt, hashed)
    # 其它设备上的会话一律失效，避免改密后旧 token 还能用；当前这枚保留
    token = _bearer(authorization)
    revoked = db.delete_user_sessions_except(DB_PATH, int(user["id"]), token) if token else 0
    inc("password_changes_total")
    return {"ok": True, "revoked_sessions": revoked}


# --------------------------------------------------------------------------- #
# 用户设置（个人偏好 + 账号）
# --------------------------------------------------------------------------- #
class SettingsBody(BaseModel):
    settings: dict[str, Any] = {}


@app.get("/api/settings")
def get_settings(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """返回生效设置 + 可配置项说明，前端据此渲染表单（不必硬编码）。"""
    saved = db.get_user_settings(DB_PATH, int(user["id"])) if user.get("id") else {}
    return {
        "settings": normalize_settings(saved),
        "defaults": dict(DEFAULT_SETTINGS),
        "schema": {
            key: {k: v for k, v in spec.items() if k in ("kind", "min", "max", "choices")}
            for key, spec in SETTINGS_SPEC.items()
        },
        "reject_reasons": REJECT_REASONS,
        "user": {
            "username": user.get("username"),
            "role": user.get("role"),
            "id": user.get("id"),
        },
    }


@app.put("/api/settings")
def put_settings(body: SettingsBody, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="服务令牌没有个人设置，请用账号登录")
    current = db.get_user_settings(DB_PATH, int(user["id"]))
    merged = normalize_settings(body.settings, current)
    db.save_user_settings(DB_PATH, int(user["id"]), merged)
    inc("settings_updates_total")
    return {"ok": True, "settings": merged}


@app.get("/api/stats/rejections", dependencies=[Depends(require_approver)])
def stats_rejections(days: int = 7, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """按「规则 × 驳回原因」聚合：用来回答"哪条规则老误报"。"""
    span = clamp(days, 1, 365)
    rows = db.rejection_counts(DB_PATH, days=span)
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["reason_code"]] = totals.get(row["reason_code"], 0) + row["count"]
    return {
        "days": span,
        "total": sum(totals.values()),
        "by_reason": [
            {"reason_code": code, "label": reject_reason_label(code), "count": count}
            for code, count in sorted(totals.items(), key=lambda kv: -kv[1])
        ],
        "by_rule": rows,
    }


# --------------------------------------------------------------------------- #
# P5 契约
# --------------------------------------------------------------------------- #
@app.post("/events", status_code=202, dependencies=[Depends(require_ingest)])
def post_event(event: dict[str, Any]) -> dict[str, Any]:
    return _ingest(event, source="rule_engine")


@app.post("/events/{event_id}/reanalyze", dependencies=[Depends(require_approver)])
def reanalyze_event(event_id: str) -> dict[str, Any]:
    """重跑：失败事件（analysis_failed/graph_failed）重跑，或给规则卡事件再要一次 AI 意见。"""
    record = event_record(event_id)
    if not record:
        raise HTTPException(status_code=404, detail="未知 event_id（后端重启后内存已清空，请重新投递）")
    status = record.get("status")
    if status == "awaiting_approval" and record.get("analysis_pending"):
        raise HTTPException(status_code=409, detail="AI 分析正在进行，稍等即可")
    if status not in ("analysis_failed", "graph_failed"):
        if status == "awaiting_approval":
            # 已有规则卡、AI 失败/超时：只补 AI 意见，不动审批状态
            inc("analysis_retry_total")
            ANALYSIS_POOL.submit(_safe_call, "enrich_with_ai", enrich_with_ai, event_id, record["event"])
            _mark(event_id, analysis_pending=True, analysis_error=None)
            return {"ok": True, "event_id": event_id, "status": "analysis_retrying"}
        raise HTTPException(status_code=409, detail=f"当前状态 {status} 不需要重试")

    _mark(event_id, status="queued", error=None, escalated=False)
    db.set_event_status(DB_PATH, event_id, "analyzing")   # 让状态机回到“分析中”
    inc("analysis_retry_total")
    EVENT_POOL.submit(_safe_call, "process_event", process_event, record["event"])
    return {"ok": True, "event_id": event_id, "status": "queued"}


class RefreshBody(BaseModel):
    aggregate: dict[str, Any] = {}


@app.post("/events/{event_id}/refresh", dependencies=[Depends(require_ingest)])
def refresh_event(event_id: str, body: RefreshBody) -> dict[str, Any]:
    """持续告警刷新：把 aggregate（持续秒数/次数/峰值/是否已恢复）合并进已有事件。

    由规则引擎在超限持续期间按 refresh_s 调用。不新建事件、不新增审批，
    值班端看到的是同一条事件在"继续变红"，而不是被 merge 窗口吞掉。
    """
    record = event_record(event_id)
    if not record:
        raise HTTPException(status_code=404, detail="未知 event_id（后端重启后内存已清空）")

    event = dict(record.get("event") or {})
    event["aggregate"] = body.aggregate
    db.update_event_payload(DB_PATH, event_id, event)
    _mark(event_id, event=event, aggregate=body.aggregate)
    inc("event_refresh_total")
    broadcast(
        {
            "type": "event_update",
            "event_id": event_id,
            "event": event,
            "aggregate": body.aggregate,
            "counts": counts(),
        }
    )
    return {"ok": True, "event_id": event_id}


@app.post("/approve/{event_id}", dependencies=[Depends(require_approver)])
def approve(
    event_id: str,
    body: DecisionBody | None = None,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    body = body or DecisionBody()
    # 审批人取登录用户，前端不再自由填写，避免冒名
    return _decide(event_id, "approve", user["username"], body.comment)


@app.post("/reject/{event_id}", dependencies=[Depends(require_approver)])
def reject(
    event_id: str,
    body: DecisionBody | None = None,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    body = body or DecisionBody()
    return _decide(event_id, "reject", user["username"], body.comment, body.reason_code)


def clamp(value: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low


@app.get("/work_orders", dependencies=[Depends(require_approver)])
def work_orders(limit: int = 50, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    return {"work_orders": db.list_work_orders(DB_PATH, limit=clamp(limit, 1, 500))}


@app.get("/events", dependencies=[Depends(require_approver)])
def list_memory_events(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    return {"events": events_snapshot()}


@app.get("/events/{event_id}", dependencies=[Depends(require_approver)])
def get_memory_event(event_id: str, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    record = event_record(event_id)
    if not record:
        raise HTTPException(status_code=404, detail="未知 event_id")
    return record


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus 文本。只有聚合计数、不含事件内容，免鉴权留给探针。"""
    return metrics_text()


@app.get("/api/telemetry", dependencies=[Depends(require_approver)])
def api_telemetry(
    device_id: str | None = None,
    sensor: str | None = None,
    minutes: int = 60,
    limit: int = 2000,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    points = db.list_telemetry(
        DB_PATH,
        device_id=device_id,
        sensor=sensor,
        minutes=clamp(minutes, 1, 24 * 60),
        limit=clamp(limit, 1, 20000),
    )
    return {"count": len(points), "points": points}


@app.get("/api/alerts", dependencies=[Depends(require_approver)])
def api_alerts(limit: int = 50, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    if not ALERTS_FILE.exists():
        return {"alerts": []}
    alerts: list[dict[str, Any]] = []
    for line in tail_lines(ALERTS_FILE, clamp(limit, 1, 500)):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            alerts.append(item)
    alerts.reverse()
    return {"alerts": alerts}


# --------------------------------------------------------------------------- #
# 负责人表（派单对象）与站内通知
# --------------------------------------------------------------------------- #
class StaffBody(BaseModel):
    name: str
    team: str | None = None
    phone: str | None = None
    username: str | None = None


class StaffPatch(BaseModel):
    name: str | None = None
    team: str | None = None
    phone: str | None = None
    username: str | None = None
    active: bool | None = None
    on_duty: bool | None = None


def _clean_staff_fields(body: StaffBody | StaffPatch) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if body.name is not None:
        name = body.name.strip()[:20]
        if len(name) < 1:
            raise HTTPException(status_code=422, detail="负责人姓名不能为空")
        out["name"] = name
    for key in ("team", "phone"):
        value = getattr(body, key, None)
        if value is not None:
            out[key] = value.strip()[:40] or None
    if body.username is not None:
        username = body.username.strip()[:32] or None
        if username and not db.get_user(DB_PATH, username):
            raise HTTPException(status_code=422, detail=f"账号 {username} 不存在，请先注册再绑定")
        out["username"] = username
    if isinstance(body, StaffPatch) and body.active is not None:
        out["active"] = 1 if body.active else 0
    if isinstance(body, StaffPatch) and body.on_duty is not None:
        out["on_duty"] = 1 if body.on_duty else 0
    return out


@app.get("/api/staff", dependencies=[Depends(require_approver)])
def list_staff(
    active_only: bool = False, user: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    return {"staff": db.list_staff(DB_PATH, active_only=active_only)}


@app.post("/api/staff", dependencies=[Depends(require_approver)])
def create_staff(body: StaffBody, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    fields = _clean_staff_fields(body)
    if not fields.get("name"):
        raise HTTPException(status_code=422, detail="负责人姓名不能为空")
    if db.get_staff_by_name(DB_PATH, fields["name"]):
        raise HTTPException(status_code=409, detail=f"负责人 {fields['name']} 已存在")
    row = db.create_staff(
        DB_PATH,
        fields["name"],
        fields.get("team"),
        fields.get("phone"),
        fields.get("username"),
    )
    inc("staff_created_total")
    return {"ok": True, "staff": row}


@app.patch("/api/staff/{staff_id}", dependencies=[Depends(require_approver)])
def patch_staff(
    staff_id: int, body: StaffPatch, user: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    if not db.get_staff(DB_PATH, staff_id):
        raise HTTPException(status_code=404, detail="负责人不存在")
    fields = _clean_staff_fields(body)
    if "name" in fields:
        existing = db.get_staff_by_name(DB_PATH, fields["name"])
        if existing and existing["id"] != staff_id:
            raise HTTPException(status_code=409, detail=f"负责人 {fields['name']} 已存在")
    row = db.update_staff(DB_PATH, staff_id, **fields)
    return {"ok": True, "staff": row}


@app.delete("/api/staff/{staff_id}", dependencies=[Depends(require_approver)])
def delete_staff(staff_id: int, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """软停用：历史工单里的姓名保留，只是不再出现在下拉里。"""
    if not db.deactivate_staff(DB_PATH, staff_id):
        raise HTTPException(status_code=404, detail="负责人不存在")
    return {"ok": True, "id": staff_id}


class ShiftBody(BaseModel):
    name: str
    staff_id: int
    start_time: str
    end_time: str
    team: str | None = None
    weekdays: str = "1,2,3,4,5,6,7"


class ShiftPatch(BaseModel):
    name: str | None = None
    staff_id: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    team: str | None = None
    weekdays: str | None = None
    active: bool | None = None


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_WEEKDAY_RE = re.compile(r"^[1-7](,[1-7])*$")


def _clean_shift(body: ShiftBody | ShiftPatch) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if body.name is not None:
        name = body.name.strip()[:20]
        if len(name) < 1:
            raise HTTPException(status_code=422, detail="班次名称不能为空")
        out["name"] = name
    for key in ("start_time", "end_time"):
        value = getattr(body, key, None)
        if value is not None:
            text = value.strip()
            if not _HHMM_RE.match(text):
                raise HTTPException(status_code=422, detail=f"{key} 必须是 HH:MM（00:00–23:59）")
            out[key] = text
    if "start_time" in out and "end_time" in out and out["start_time"] == out["end_time"]:
        raise HTTPException(status_code=422, detail="开始与结束时间不能相同")
    if body.weekdays is not None:
        weekdays = body.weekdays.replace(" ", "")
        if not _WEEKDAY_RE.match(weekdays):
            raise HTTPException(status_code=422, detail="weekdays 只能是 1-7 的逗号分隔，如 1,2,3,4,5")
        out["weekdays"] = weekdays
    if body.team is not None:
        out["team"] = body.team.strip()[:40] or None
    if isinstance(body, ShiftPatch) and body.active is not None:
        out["active"] = 1 if body.active else 0
    return out


@app.get("/api/shifts", dependencies=[Depends(require_approver)])
def list_shifts(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """排班表 + 当前是否在班内；顺带触发一次当班同步。"""
    now = datetime.now().astimezone()
    rows = db.list_shifts(DB_PATH)
    for row in rows:
        row["current"] = bool(row["active"]) and db.shift_is_active(row, now)
    duty = db.sync_duty(DB_PATH, now)
    return {
        "shifts": rows,
        "now": now.isoformat(timespec="seconds"),
        "duty": duty,
        "on_duty": on_duty_usernames(),
    }


@app.post("/api/shifts", dependencies=[Depends(require_approver)])
def create_shift(body: ShiftBody, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    fields = _clean_shift(body)
    if not db.get_staff(DB_PATH, body.staff_id):
        raise HTTPException(status_code=422, detail="负责人不存在，请先在负责人表里添加")
    if fields["start_time"] == fields["end_time"]:
        raise HTTPException(status_code=422, detail="开始与结束时间不能相同")
    row = db.create_shift(
        DB_PATH,
        fields["name"],
        body.staff_id,
        fields["start_time"],
        fields["end_time"],
        fields.get("team"),
        fields["weekdays"],
    )
    db.sync_duty(DB_PATH, datetime.now().astimezone())
    inc("shifts_created_total")
    return {"ok": True, "shift": row}


@app.patch("/api/shifts/{shift_id}", dependencies=[Depends(require_approver)])
def patch_shift(
    shift_id: int, body: ShiftPatch, user: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    if not db.get_shift(DB_PATH, shift_id):
        raise HTTPException(status_code=404, detail="班次不存在")
    fields = _clean_shift(body)
    if "staff_id" in fields and not db.get_staff(DB_PATH, int(fields["staff_id"])):
        raise HTTPException(status_code=422, detail="负责人不存在")
    row = db.update_shift(DB_PATH, shift_id, **fields)
    db.sync_duty(DB_PATH, datetime.now().astimezone())
    return {"ok": True, "shift": row}


@app.delete("/api/shifts/{shift_id}", dependencies=[Depends(require_approver)])
def delete_shift(shift_id: int, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_shift(DB_PATH, shift_id):
        raise HTTPException(status_code=404, detail="班次不存在")
    db.sync_duty(DB_PATH, datetime.now().astimezone())
    return {"ok": True, "id": shift_id}


@app.post("/api/shifts/sync", dependencies=[Depends(require_approver)])
def sync_shifts(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    duty = db.sync_duty(DB_PATH, datetime.now().astimezone())
    return {"ok": True, "duty": duty, "on_duty": on_duty_usernames()}


@app.post("/api/sla/check", dependencies=[Depends(require_approver)])
def run_sla_check(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """手动跑一轮 SLA 检查（演示/排障用）。"""
    return {"ok": True, "result": check_sla_once()}


@app.get("/api/notifications")
def my_notifications(
    unread_only: bool = False,
    limit: int = 50,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    recipient = str(user.get("username"))
    return {
        "unread": db.count_unread_notifications(DB_PATH, recipient),
        "items": db.list_notifications(
            DB_PATH, recipient, unread_only=unread_only, limit=clamp(limit, 1, 200)
        ),
    }


@app.post("/api/notifications/{notification_id}/read")
def read_notification(
    notification_id: int, user: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    ok = db.mark_notification_read(DB_PATH, notification_id, str(user.get("username")))
    return {"ok": ok, "unread": db.count_unread_notifications(DB_PATH, str(user.get("username")))}


@app.post("/api/notifications/read-all")
def read_all_notifications(user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    changed = db.mark_all_notifications_read(DB_PATH, str(user.get("username")))
    return {"ok": True, "changed": changed, "unread": 0}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, ticket: str | None = None) -> None:
    # WS 推送的是全量事件，所以同样要票据；EventSource 那条路见 /api/stream
    if not consume_stream_ticket(ticket):
        await ws.close(code=4401)
        return
    await ws.accept()
    queue = WSBUS.subscribe()
    try:
        await ws.send_json(
            {
                "type": "hello",
                "status": status_payload(),
                "events": list(db.list_events(DB_PATH, limit=50)),
                "work_orders": db.list_work_orders(DB_PATH, limit=50),
            }
        )
        while True:
            message = await queue.get()
            await ws.send_json(message)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 客户端断开的各种形态
        pass
    finally:
        WSBUS.unsubscribe(queue)


# --------------------------------------------------------------------------- #
# P4 看板兼容（SSE）
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def api_status() -> dict[str, Any]:
    """运维探针：只有聚合状态，不含事件正文，保持免鉴权。"""
    return status_payload()


@app.get("/api/events", dependencies=[Depends(require_approver)])
def api_events(limit: int = 50, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    return {"events": db.list_events(DB_PATH, limit=clamp(limit, 1, 500))}


@app.get("/api/work-orders")
def api_work_orders(limit: int = 50, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """审批人看全部；工程师只看派给自己账号的工单。"""
    own = None if is_approver(user) else str(user.get("username"))
    rows = db.list_work_orders(DB_PATH, limit=clamp(limit, 1, 500), assignee_username=own)
    # 列表也带上 SLA 倒计时，前端才能显示「剩 N 分钟 / 已超时」
    for row in rows:
        row["sla"] = _sla_block(row, row.get("decided_at") or row.get("created_at"))
    return {"work_orders": rows}


@app.get("/api/stream")
async def api_stream(ticket: str | None = None, limit: int = 50) -> StreamingResponse:
    # 浏览器 EventSource 不能自定义请求头，所以用短时票据代替 Authorization
    if not consume_stream_ticket(ticket):
        raise HTTPException(status_code=401, detail="缺少或过期的订阅票据，请先登录")
    # limit 来自用户设置里的「事件列表条数」
    hello_limit = clamp(limit, 1, 200)
    queue = BUS.subscribe()

    async def generator():
        try:
            yield sse_frame(
                {
                    "type": "hello",
                    "status": status_payload(),
                    "events": db.list_events(DB_PATH, limit=hello_limit),
                    "work_orders": db.list_work_orders(DB_PATH, limit=hello_limit),
                }
            )
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield sse_frame(message)
        finally:
            BUS.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


class ApprovalDecision(BaseModel):
    decision: str                    # 必填：缺省绝不放行（旧版默认 approve 是 fail-open）
    comment: str | None = None
    reason_code: str | None = None   # 驳回原因（误报/重复/已处置…），缺省 other


@app.post("/api/approvals/{event_id}", dependencies=[Depends(require_approver)])
def api_approval(
    event_id: str,
    body: ApprovalDecision,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    decision = body.decision.strip().lower()
    if decision in {"approve", "approved", "批准", "同意"}:
        return _decide(event_id, "approve", user["username"], body.comment)
    if decision in {"reject", "rejected", "驳回", "拒绝"}:
        return _decide(event_id, "reject", user["username"], body.comment, body.reason_code)
    raise HTTPException(status_code=422, detail=f"非法 decision: {decision!r}")


@app.post("/api/dev/inject", dependencies=[Depends(require_approver)])
def api_inject(event: dict[str, Any], user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    return _ingest(event, source="dev", origin_user=user["username"])


# --------------------------------------------------------------------------- #
# 工单流转：created → assigned → closed（可重新打开）
# --------------------------------------------------------------------------- #
class AssignBody(BaseModel):
    # 三种方式：从负责人表选（staff_id）、手打名字（assignee）、按当班自动选（auto）
    staff_id: int | None = None
    assignee: str | None = None
    note: str | None = None
    auto: bool = False
    team: str | None = None


class CloseBody(BaseModel):
    resolution: str
    # 验证未通过时，只有填了例外理由才允许关闭（会写审计 + 计数）
    exception_reason: str | None = None


class HandleBody(BaseModel):
    measure: str
    # data:image/jpeg;base64,... 形式；也可只传裸 base64
    photo_base64: str | None = None


def _save_photo(order_id: int, data_url: str) -> str:
    """把整改照片存成文件（不塞数据库），返回文件名。"""
    raw = data_url.strip()
    mime = "image/jpeg"
    if raw.startswith("data:"):
        header, _, payload = raw.partition(",")
        mime = header[5:].split(";")[0].strip().lower()
        raw = payload
    ext = PHOTO_TYPES.get(mime)
    if not ext:
        raise HTTPException(status_code=422, detail=f"不支持的图片类型 {mime}，只接受 jpeg/png/webp")
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="图片数据不是合法的 base64") from None
    if not blob:
        raise HTTPException(status_code=422, detail="图片为空")
    if len(blob) > PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"图片超过 {PHOTO_MAX_BYTES // 1024} KB，请压缩后再上传"
        )
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"wo-{order_id}-{int(time.time() * 1000)}.{ext}"
    (PHOTO_DIR / name).write_bytes(blob)
    return name


def _require_assignee(row: dict[str, Any], user: dict[str, Any]) -> None:
    """工程师只能操作派给自己的工单；审批人可以代为操作。

    - 绑定了账号：只有该账号能操作；
    - 手填姓名（未绑定账号）：只有同名账号能操作；
    - 还没派单：工程师一律不许动（先由审批人派单）。
    """
    if is_approver(user):
        return
    who = str(user.get("username"))
    bound = row.get("assignee_username")
    if bound:
        if str(bound) != who:
            raise HTTPException(
                status_code=403, detail=f"该工单已指派给 {row.get('assignee')}，请用对应账号操作"
            )
        return
    name = str(row.get("assignee") or "").strip()
    if name and name == who:
        return
    if not name:
        raise HTTPException(status_code=403, detail="该工单还没有派给你，请联系审批人派单")
    raise HTTPException(
        status_code=403, detail=f"该工单已指派给 {name}，请用对应账号操作"
    )


def _work_order_or_404(order_id: int) -> dict[str, Any]:
    row = db.get_work_order_by_id(DB_PATH, order_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"工单 {order_id} 不存在")
    return row


def _push_work_order(row: dict[str, Any]) -> dict[str, Any]:
    payload = _work_order_payload(row)
    broadcast(
        {
            "type": "work_order",
            "event_id": row["event_id"],
            "work_order": payload,
            "counts": counts(),
        }
    )
    return {"ok": True, "work_order": payload}


@app.post("/api/work_orders/{order_id}/assign", dependencies=[Depends(require_approver)])
def assign_work_order(
    order_id: int,
    body: AssignBody,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """派单：从负责人表选人（或手打姓名），写工单 + 给被派人发站内通知。"""
    row = _work_order_or_404(order_id)
    if row["status"] == "closed":
        raise HTTPException(status_code=409, detail="工单已关闭，先重新打开再指派")

    staff: dict[str, Any] | None = None
    if body.auto:
        staff = db.pick_on_duty_staff(DB_PATH, (body.team or "").strip() or None)
        if not staff:
            raise HTTPException(
                status_code=409,
                detail="当前没有当班负责人，请先在设置→负责人表里把值班的人标为「当班」",
            )
        assignee = str(staff["name"])
        assignee_username = staff.get("username")
    elif body.staff_id is not None:
        staff = db.get_staff(DB_PATH, body.staff_id)
        if not staff or not int(staff.get("active") or 0):
            raise HTTPException(status_code=422, detail="负责人不存在或已停用")
        assignee = str(staff["name"])
        assignee_username = staff.get("username")
    elif (body.assignee or "").strip():
        assignee = body.assignee.strip()[:60]
        assignee_username = None
    else:
        raise HTTPException(status_code=422, detail="请选择负责人或填写处理人")

    updated = db.update_work_order(
        DB_PATH,
        order_id,
        status="assigned",
        assignee=assignee,
        assignee_username=assignee_username,
        assigned_at=utcnow(),
    )
    note = (body.note or "").strip()[:200] or None
    db.upsert_audit(
        DB_PATH,
        f"work_order:{order_id}",
        "assigned",
        json.dumps(
            {
                "assignee": assignee,
                "assignee_username": assignee_username,
                "staff_id": staff["id"] if staff else None,
                "mode": "auto_on_duty" if body.auto else ("roster" if staff else "manual"),
                "team": (body.team or None) if body.auto else None,
                "note": note,
                "by": user["username"],
            },
            ensure_ascii=False,
        ),
    )
    inc("work_order_assigned_total")

    # 站内通知：被派人绑了账号发给他；没绑就发回派单人，提醒线下通知
    content = row.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    rule_id = (content or {}).get("rule_id") or "安全事件"
    wo_tag = f"WO-{order_id:04d}"
    if assignee_username and assignee_username != user["username"]:
        db.add_notification(
            DB_PATH,
            str(assignee_username),
            "dispatch",
            f"新工单 {wo_tag}：{rule_id}",
            note or f"{assignee} 请处理；来源事件 {row['event_id']}",
            event_id=row["event_id"],
            work_order_id=order_id,
        )
        inc("notifications_dispatch_total")
    elif assignee_username == user["username"]:
        # 派给自己：不给自己发通知，避免"自己通知自己"的噪音
        pass
    else:
        db.add_notification(
            DB_PATH,
            str(user["username"]),
            "assignee_unbound",
            f"{assignee} 未绑定账号，请线下通知",
            f"{wo_tag} 已派给 {assignee}（负责人表里没有绑定登录账号，站内通知发不到这个人）",
            event_id=row["event_id"],
            work_order_id=order_id,
        )
        inc("notifications_unbound_total")

    return _push_work_order(updated)


@app.post("/api/work_orders/{order_id}/accept")
def accept_work_order(order_id: int, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """操作员接单：assigned → accepted。"""
    row = _work_order_or_404(order_id)
    if row["status"] != "assigned":
        raise HTTPException(status_code=409, detail=f"只有已派单的工单可以接单，当前状态 {row['status']}")
    _require_assignee(row, user)
    updated = db.update_work_order(
        DB_PATH,
        order_id,
        status="accepted",
        accepted_at=utcnow(),
        accepted_by=user["username"],
    )
    db.upsert_audit(
        DB_PATH,
        f"work_order:{order_id}",
        "accepted",
        json.dumps({"by": user["username"]}, ensure_ascii=False),
    )
    inc("work_order_accepted_total")
    return _push_work_order(updated)


@app.post("/api/work_orders/{order_id}/handle")
def handle_work_order(
    order_id: int,
    body: HandleBody,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """操作员标记已处理（整改措施 + 可选照片）：assigned/accepted → handled（待复测）。"""
    row = _work_order_or_404(order_id)
    if row["status"] not in ("assigned", "accepted"):
        raise HTTPException(
            status_code=409, detail=f"只有待接单/处理中的工单可以标记已处理，当前状态 {row['status']}"
        )
    _require_assignee(row, user)
    measure = (body.measure or "").strip()[:500]
    if len(measure) < 2:
        raise HTTPException(status_code=422, detail="请填写整改措施（至少 2 个字）")

    photo_path = _save_photo(order_id, body.photo_base64) if (body.photo_base64 or "").strip() else None
    fields: dict[str, Any] = {
        "status": "handled",
        "handled_at": utcnow(),
        "handled_by": user["username"],
        "measure": measure,
    }
    if photo_path:
        fields["photo_path"] = photo_path
    if not row.get("accepted_at"):
        # 直接标已处理也视为接单，避免时间线缺一段
        fields["accepted_at"] = utcnow()
        fields["accepted_by"] = user["username"]

    updated = db.update_work_order(DB_PATH, order_id, **fields)
    db.upsert_audit(
        DB_PATH,
        f"work_order:{order_id}",
        "handled",
        json.dumps(
            {"measure": measure, "by": user["username"], "photo": bool(photo_path)},
            ensure_ascii=False,
        ),
    )
    inc("work_order_handled_total")
    return _push_work_order(updated)


@app.get("/api/work_orders/{order_id}")
def get_work_order_detail(order_id: int, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """单张工单详情，带整改照片（data URL）。列表不带照片，避免一次拉几 MB。"""
    row = _work_order_or_404(order_id)
    _require_assignee(row, user)  # 工程师只能看派给自己的
    payload = _work_order_payload(row) or {}
    name = row.get("photo_path")
    if name:
        path = PHOTO_DIR / Path(str(name)).name
        if path.exists() and path.stat().st_size <= PHOTO_MAX_BYTES:
            blob = path.read_bytes()
            mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
                path.suffix.lstrip(".").lower(), "application/octet-stream"
            )
            payload["photo_data_url"] = f"data:{mime};base64,{base64.b64encode(blob).decode()}"
    return {"work_order": payload}


@app.post("/api/work_orders/{order_id}/close")
def close_work_order(
    order_id: int,
    body: CloseBody,
    user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """关闭工单 —— 必须先通过「处置后验证」。

    规程写的是「未处理完毕不得恢复作业」「复测合格并经人工批准后方可恢复」，
    所以这里不能只看一段文字：去查该设备该测点最近 N 分钟的遥测峰值。
    验证不通过时只有两条路——不关，或者填例外理由（留痕 + 计数）。
    """
    row = _work_order_or_404(order_id)
    _require_assignee(row, user)  # 工程师只能关自己的工单
    if row["status"] == "closed":
        raise HTTPException(status_code=409, detail="工单已经关闭")
    resolution = body.resolution.strip()[:500]
    if len(resolution) < 2:
        raise HTTPException(status_code=422, detail="请填写处置结果（至少 2 个字）")

    window = int(settings_for(user).get("retest_window_minutes", 10))
    verification = verify_recovery(row["event_id"], window)
    exception = (body.exception_reason or "").strip()[:200]

    if not verification["ok"] and len(exception) < 4:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "处置后验证未通过，不能直接关闭",
                "verification": verification,
                "hint": f"确认现场已恢复，请填写例外理由（≥4 字）后重试；当前窗口 {window} 分钟",
            },
        )

    payload = json.dumps(verification, ensure_ascii=False)
    if verification["ok"]:
        updated = db.update_work_order(
            DB_PATH,
            order_id,
            status="closed",
            closed_at=utcnow(),
            verified_at=utcnow(),
            resolution=resolution,
            verification=payload,
            exception_reason=None,
        )
        db.upsert_audit(
            DB_PATH,
            f"work_order:{order_id}",
            "closed_verified",
            json.dumps(
                {"resolution": resolution, "by": user["username"], "verification": verification},
                ensure_ascii=False,
            ),
        )
        inc("work_order_closed_verified_total")
    else:
        updated = db.update_work_order(
            DB_PATH,
            order_id,
            status="closed",
            closed_at=utcnow(),
            resolution=resolution,
            verification=payload,
            exception_reason=exception,
        )
        db.upsert_audit(
            DB_PATH,
            f"work_order:{order_id}",
            "closed_with_exception",
            json.dumps(
                {
                    "resolution": resolution,
                    "by": user["username"],
                    "exception_reason": exception,
                    "verification": verification,
                },
                ensure_ascii=False,
            ),
        )
        inc("work_order_closed_exception_total")
        print(
            f"[work_order] {order_id} 例外关闭（{user['username']}）：{exception}；"
            f"验证结论 {verification.get('message')}",
            flush=True,
        )

    return _push_work_order(updated)


@app.post("/api/work_orders/{order_id}/reopen", dependencies=[Depends(require_approver)])
def reopen_work_order(order_id: int, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    row = _work_order_or_404(order_id)
    if row["status"] != "closed":
        raise HTTPException(status_code=409, detail="只有已关闭的工单可以重新打开")
    updated = db.update_work_order(
        DB_PATH,
        order_id,
        status="created",
        closed_at=None,
        resolution=None,
        verification=None,
        exception_reason=None,
    )
    db.upsert_audit(
        DB_PATH,
        f"work_order:{order_id}",
        "reopened",
        json.dumps({"by": user["username"]}, ensure_ascii=False),
    )
    return _push_work_order(updated)


# --------------------------------------------------------------------------- #
# 驳回后重新提交（生成新事件，保留 retry_of 追溯）
# --------------------------------------------------------------------------- #
@app.post("/api/events/{event_id}/resubmit", dependencies=[Depends(require_approver)])
def resubmit_event(event_id: str, user: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    record = event_record(event_id)
    if not record:
        raise HTTPException(status_code=404, detail="未知 event_id（后端重启后内存已清空）")
    if record.get("status") != "rejected":
        raise HTTPException(status_code=409, detail=f"只有已拒绝的事件可以重新提交，当前 {record.get('status')}")

    parent = dict(record.get("event") or {})
    new_event = {
        **parent,
        "event_id": f"evt-resubmit-{uuid.uuid4().hex[:8]}",
        "retry_of": event_id,
        "triggered_at": utcnow(),
        "origin_user": user["username"],
    }
    db.set_event_status(DB_PATH, event_id, "superseded")
    _mark(event_id, status="superseded")
    result = _ingest(new_event, source="http", origin_user=user["username"])
    inc("events_resubmitted_total")
    return {**result, "retry_of": event_id}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="info")
