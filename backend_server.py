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
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

import agent_graph as ag
import analyzer
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
APPROVAL_TIMEOUT = float(os.environ.get("APPROVAL_TIMEOUT_S", "600"))  # 默认 10 分钟未审批升级告警
HTTP_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"

# 可选鉴权：设置 API_TOKEN 后，写操作需带 Authorization: Bearer <token>
API_TOKEN = os.environ.get("API_TOKEN", "")
# LLM 分析超时（秒），0=不限制；超时自动降级为离线模板
ANALYSIS_TIMEOUT = float(os.environ.get("ANALYSIS_TIMEOUT_S", "0"))
ANALYSIS_CONCURRENCY = int(os.environ.get("ANALYSIS_CONCURRENCY", "1"))
# 可选告警外发
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")
ALERTS_FILE = Path(DB_PATH).parent / "alerts.jsonl"

BUS = EventBus()          # SSE 订阅者
WSBUS = EventBus()        # WebSocket 订阅者（复用同一套线程安全发布逻辑）
PIPELINE_LOCK = threading.Lock()   # 只保护 LangGraph 的 invoke/get_state
ANALYSIS_SEM = threading.Semaphore(ANALYSIS_CONCURRENCY)

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


def metrics_text() -> str:
    with METRICS_LOCK:
        body = "\n".join(f"{k} {v}" for k, v in sorted(METRICS.items()))
    extra = [
        f"queue_depth {max(0, len(EVENTS) - sum(1 for e in EVENTS.values() if e.get('status') in ('approved', 'rejected', 'failed')))}",
        f"mqtt_connected {1 if STATE.get('mqtt') and STATE['mqtt'].connected else 0}",
        f"devices_online {sum(1 for d in device_snapshot() if d['online'])}",
        f"spool_pending {len(list((Path(DB_PATH).parent / 'spool').glob('*.json')))}" if (Path(DB_PATH).parent / 'spool').exists() else "spool_pending 0",
    ]
    return "# 安全监测 Agent Demo metrics\n" + body + "\n" + "\n".join(extra) + "\n"


def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="缺少或非法 Authorization: Bearer <token>")


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
def touch_device(payload: dict[str, Any]) -> None:
    """收到心跳或任意遥测：刷新 last_seen，必要时解除离线标记。"""
    device = str(payload.get("device_id") or "unknown")
    now = time.time()
    with DEVICES_LOCK:
        record = DEVICES.setdefault(
            device, {"device_id": device, "offline": False, "offline_events": 0, "last_seen": now}
        )
        record["last_seen"] = now
        record["last_seen_iso"] = utcnow()
        if record["offline"]:
            record["offline"] = False
            print(f"[watchdog] {device} 心跳恢复", flush=True)


def device_snapshot() -> list[dict[str, Any]]:
    now = time.time()
    with DEVICES_LOCK:
        return [
            {
                "device_id": rec["device_id"],
                "online": not rec["offline"],
                "last_seen": rec.get("last_seen_iso"),
                "silent_seconds": round(now - rec.get("last_seen", now), 1),
                "offline_events": rec.get("offline_events", 0),
            }
            for rec in DEVICES.values()
        ]


def device_watchdog(stop: threading.Event) -> None:
    """每 2 秒检查一次：超过 OFFLINE_TIMEOUT 没有心跳就产出一条“设备离线”事件。"""
    while not stop.wait(2.0):
        now = time.time()
        for device, record in list(DEVICES.items()):
            silent = now - record.get("last_seen", now)
            if silent <= OFFLINE_TIMEOUT or record.get("offline"):
                continue
            record["offline"] = True
            record["offline_events"] = record.get("offline_events", 0) + 1
            event = {
                "event_id": f"evt-offline-{device}-{int(now)}",
                "schema_version": 1,
                "line": "line1",
                "device_id": device,
                "sensor": "sensor",
                "rule_id": "R-DEV-OFFLINE",
                "event_type": "device_offline",
                "severity": "medium",
                "triggered_at": utcnow(),
                "observed": {"value": round(silent, 1), "unit": "s", "threshold": OFFLINE_TIMEOUT},
                "window": {"samples": 0, "mean": 0, "max": round(silent, 1), "slope_per_min": 0},
                "snapshot": {"sensors": {}},
                "scenario_id": "watchdog",
            }
            print(f"[watchdog] {device} 已 {silent:.0f}s 无心跳 → 生成离线事件", flush=True)
            try:
                _ingest(event, source="watchdog")
            except Exception as exc:  # noqa: BLE001
                print(f"[watchdog] 离线事件投递失败: {exc}", flush=True)

        # 审批超时升级：只告警，不自动批准
        if APPROVAL_TIMEOUT > 0:
            for event_id, record in list(EVENTS.items()):
                if record.get("status") != "awaiting_approval" or record.get("escalated"):
                    continue
                waited = now - record.get("awaiting_since", now)
                if waited <= APPROVAL_TIMEOUT:
                    continue
                record["escalated"] = True
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


def counts() -> dict[str, int]:
    return {
        "events": db.count_events(DB_PATH),
        "pending": db.count_events(DB_PATH, "pending"),
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
    }


def _work_order_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload = dict(row)
    if isinstance(payload.get("content"), str):
        try:
            payload["content"] = json.loads(payload["content"])
        except json.JSONDecodeError:
            pass
    approval = db.get_approval(DB_PATH, payload.get("event_id", ""))
    if approval:
        payload["approver"] = approval.get("approver")
        payload["approval_comment"] = approval.get("comment")
        payload["decided_at"] = approval.get("decided_at")
    return payload


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
# 管线：事件 → 分析 → 图（停在 interrupt）→ 广播
# --------------------------------------------------------------------------- #
def process_event(event: dict[str, Any]) -> None:
    event_id = event["event_id"]
    graph = STATE["app"]

    _mark(event_id, status="queued")
    db.upsert_event(DB_PATH, event_id, event)
    inc("events_ingested_total")
    broadcast({"type": "event", "event": event, "event_id": event_id, "counts": counts()})

    started = time.perf_counter()
    source = STATE["analyzer_source"]
    try:
        analysis, source = _analyze_with_timeout(event)
    except Exception as exc:  # noqa: BLE001
        inc("analysis_failed_total")
        _mark(event_id, status="analysis_failed", error=str(exc))
        db.set_event_status(DB_PATH, event_id, "analysis_failed")
        broadcast(
            {
                "type": "analysis_error",
                "event_id": event_id,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
                "counts": counts(),
            }
        )
        return

    elapsed = round(time.perf_counter() - started, 1)
    inc("analysis_total")
    inc("analysis_seconds", elapsed)
    citations = resolve_citations(analysis)

    try:
        with PIPELINE_LOCK:  # 只锁图操作，LLM 推理在此期间不再占锁
            graph.invoke({"event": event}, ag.thread_config(event_id))
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

    db.save_event_analysis(DB_PATH, event_id, analysis, citations)
    _mark(
        event_id,
        status="awaiting_approval",
        analysis=analysis,
        citations=citations,
        analysis_source=source,
        analysis_seconds=elapsed,
        awaiting_since=time.time(),
    )
    broadcast(
        {
            "type": "analysis",
            "event_id": event_id,
            "event": event,
            "analysis": analysis,
            "citations": citations,
            "analysis_source": source,
            "analysis_seconds": elapsed,
            "status": "pending",
            "counts": counts(),
        }
    )

    if event.get("severity") == "high" or event.get("event_type") == "device_offline":
        emit_alert(event, analysis)


def _analyze_with_timeout(event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """并发上限 + 可选超时降级：LLM 太慢时退回离线模板，不把演示卡死。"""
    _mark(event["event_id"], status="analyzing")
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
) -> dict[str, Any]:
    """approve/reject 的公共实现；同一 event_id 重复调用不会产生第二张工单。"""
    graph = STATE["app"]
    config = ag.thread_config(event_id)
    note = (comment or "").strip()[:500] or None

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
            record = EVENTS.get(event_id)
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
        )
        _mark(event_id, status="rejected")
        broadcast(
            {
                "type": "rejected",
                "event_id": event_id,
                "approval": approval,
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
                DB_PATH, checkpointer=checkpointer, analyze_fn=analyze_fn, tools_fn=collect_tools
            ),
            "analyzer_source": source,
            "analyzer_model": model,
            "analyze": analyze_fn,
            "checkpointer": "sqlite",
            "checkpoint_db": ckpt_path,
            "ckpt_conn": ckpt_conn,
        }
    )

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

    print(f"[backend] db={DB_PATH}")
    print(f"[backend] analyzer={source} model={model}")
    print(f"[backend] mqtt={MQTT_HOST}:{MQTT_PORT} topic={MQTT_TOPIC} heartbeat={HEARTBEAT_TOPIC}")
    print(f"[backend] http://127.0.0.1:{HTTP_PORT}  ws://127.0.0.1:{HTTP_PORT}/ws")
    yield
    _DEVICE_STOP.set()
    mqtt.stop()


def _ingest(event: dict[str, Any], source: str = "http") -> dict[str, Any]:
    if not isinstance(event, dict):
        raise HTTPException(status_code=422, detail="事件必须是 JSON 对象")
    event_id = str(event.get("event_id") or f"evt-{uuid.uuid4().hex[:12]}")
    event["event_id"] = event_id

    with EVENTS_LOCK:
        if event_id in EVENTS:
            return {"accepted": False, "duplicate": True, "event_id": event_id, "thread_id": event_id}
        EVENTS[event_id] = {
            "event_id": event_id,
            "thread_id": event_id,
            "event": event,
            "status": "received",
            "source": source,
            "received_at": utcnow(),
        }

    threading.Thread(target=process_event, args=(event,), daemon=True).start()
    return {"accepted": True, "duplicate": False, "event_id": event_id, "thread_id": event_id}


app = FastAPI(title="安全监测 Agent Demo", version="5.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# P5 契约
# --------------------------------------------------------------------------- #
@app.post("/events", status_code=202, dependencies=[Depends(require_auth)])
def post_event(event: dict[str, Any]) -> dict[str, Any]:
    return _ingest(event, source="rule_engine")


@app.post("/events/{event_id}/reanalyze", dependencies=[Depends(require_auth)])
def reanalyze_event(event_id: str) -> dict[str, Any]:
    """失败事件重跑：仅允许 analysis_failed / graph_failed，重跑前清掉旧状态。"""
    record = EVENTS.get(event_id)
    if not record:
        raise HTTPException(status_code=404, detail="未知 event_id（后端重启后内存已清空，请重新投递）")
    if record.get("status") not in ("analysis_failed", "graph_failed"):
        raise HTTPException(status_code=409, detail=f"当前状态 {record.get('status')} 不需要重试")

    _mark(event_id, status="queued", error=None)
    inc("analysis_retry_total")
    threading.Thread(target=process_event, args=(record["event"],), daemon=True).start()
    return {"ok": True, "event_id": event_id, "status": "queued"}


@app.post("/approve/{event_id}", dependencies=[Depends(require_auth)])
def approve(event_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    body = body or DecisionBody()
    return _decide(event_id, "approve", body.approver, body.comment)


@app.post("/reject/{event_id}", dependencies=[Depends(require_auth)])
def reject(event_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    body = body or DecisionBody()
    return _decide(event_id, "reject", body.approver, body.comment)


@app.get("/work_orders")
def work_orders(limit: int = 50) -> dict[str, Any]:
    return {"work_orders": db.list_work_orders(DB_PATH, limit=limit)}


@app.get("/events")
def list_memory_events() -> dict[str, Any]:
    with EVENTS_LOCK:
        return {"events": list(EVENTS.values())}


@app.get("/events/{event_id}")
def get_memory_event(event_id: str) -> dict[str, Any]:
    record = EVENTS.get(event_id)
    if not record:
        raise HTTPException(status_code=404, detail="未知 event_id")
    return record


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return metrics_text()


@app.get("/api/telemetry")
def api_telemetry(
    device_id: str | None = None,
    sensor: str | None = None,
    minutes: int = 60,
    limit: int = 2000,
) -> dict[str, Any]:
    points = db.list_telemetry(DB_PATH, device_id=device_id, sensor=sensor, minutes=minutes, limit=limit)
    return {"count": len(points), "points": points}


@app.get("/api/alerts")
def api_alerts(limit: int = 50) -> dict[str, Any]:
    if not ALERTS_FILE.exists():
        return {"alerts": []}
    lines = ALERTS_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    alerts: list[dict[str, Any]] = []
    for line in lines:
        try:
            alerts.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    alerts.reverse()
    return {"alerts": alerts}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
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
    return status_payload()


@app.get("/api/events")
def api_events(limit: int = 50) -> dict[str, Any]:
    return {"events": db.list_events(DB_PATH, limit=limit)}


@app.get("/api/work-orders")
def api_work_orders(limit: int = 50) -> dict[str, Any]:
    return {"work_orders": db.list_work_orders(DB_PATH, limit=limit)}


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    queue = BUS.subscribe()

    async def generator():
        try:
            yield sse_frame(
                {
                    "type": "hello",
                    "status": status_payload(),
                    "events": db.list_events(DB_PATH, limit=50),
                    "work_orders": db.list_work_orders(DB_PATH, limit=50),
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


@app.post("/api/approvals/{event_id}", dependencies=[Depends(require_auth)])
def api_approval(event_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = body or {}
    decision = str(payload.get("decision", "approve")).strip().lower()
    approver = payload.get("approver")
    comment = payload.get("comment")
    if decision in {"approve", "approved", "批准", "同意"}:
        return _decide(event_id, "approve", approver, comment)
    if decision in {"reject", "rejected", "驳回", "拒绝"}:
        return _decide(event_id, "reject", approver, comment)
    raise HTTPException(status_code=422, detail=f"非法 decision: {decision!r}")


@app.post("/api/dev/inject", dependencies=[Depends(require_auth)])
def api_inject(event: dict[str, Any]) -> dict[str, Any]:
    return _ingest(event, source="dev")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="info")
