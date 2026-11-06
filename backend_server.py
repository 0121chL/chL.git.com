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
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
HTTP_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"

BUS = EventBus()          # SSE 订阅者
WSBUS = EventBus()        # WebSocket 订阅者（复用同一套线程安全发布逻辑）
PIPELINE_LOCK = threading.Lock()

EVENTS: dict[str, dict[str, Any]] = {}          # event_id -> 内存状态
EVENTS_LOCK = threading.Lock()
EVENT_LOCKS: dict[str, threading.Lock] = {}

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


def event_lock(event_id: str) -> threading.Lock:
    with EVENTS_LOCK:
        return EVENT_LOCKS.setdefault(event_id, threading.Lock())


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


def pick_analyzer() -> tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]:
    try:
        model = llm_client.ensure_ready()
    except Exception:  # noqa: BLE001
        return "offline-template", "—", offline_analyzer.analyze
    if llm_client.is_stub():
        return "ollama-stub", model, analyzer.analyze_event
    return "ollama", model, analyzer.analyze_event


# --------------------------------------------------------------------------- #
# 管线：事件 → 分析 → 图（停在 interrupt）→ 广播
# --------------------------------------------------------------------------- #
def process_event(event: dict[str, Any]) -> None:
    event_id = event["event_id"]
    graph = STATE["app"]

    with PIPELINE_LOCK:
        _mark(event_id, status="analyzing")
        db.upsert_event(DB_PATH, event_id, event)
        broadcast({"type": "event", "event": event, "event_id": event_id, "counts": counts()})

        try:
            analysis = STATE["analyze"](event)
        except Exception as exc:  # noqa: BLE001
            _mark(event_id, status="analysis_failed", error=str(exc))
            db.set_event_status(DB_PATH, event_id, "analysis_failed")
            broadcast(
                {
                    "type": "analysis_error",
                    "event_id": event_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "counts": counts(),
                }
            )
            return

        citations = resolve_citations(analysis)

        try:
            graph.invoke({"event": event}, ag.thread_config(event_id))
        except Exception as exc:  # noqa: BLE001
            _mark(event_id, status="graph_failed", error=str(exc))
            db.set_event_status(DB_PATH, event_id, "graph_failed")
            broadcast(
                {
                    "type": "analysis_error",
                    "event_id": event_id,
                    "error": f"图执行失败: {exc}",
                    "counts": counts(),
                }
            )
            return

        db.save_event_analysis(DB_PATH, event_id, analysis, citations)
        _mark(event_id, status="awaiting_approval", analysis=analysis, citations=citations)
        broadcast(
            {
                "type": "analysis",
                "event_id": event_id,
                "event": event,
                "analysis": analysis,
                "citations": citations,
                "status": "pending",
                "counts": counts(),
            }
        )


def _mark(event_id: str, **fields: Any) -> None:
    with EVENTS_LOCK:
        record = EVENTS.setdefault(event_id, {"event_id": event_id, "thread_id": event_id})
        record.update(fields)


# --------------------------------------------------------------------------- #
# 审批（幂等）
# --------------------------------------------------------------------------- #
class DecisionBody(BaseModel):
    comment: str | None = None


def _decide(event_id: str, decision: str) -> dict[str, Any]:
    """approve/reject 的公共实现；同一 event_id 重复调用不会产生第二张工单。"""
    graph = STATE["app"]
    config = ag.thread_config(event_id)

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

        result = graph.invoke(Command(resume=decision), config)
        status = "approved" if decision == "approve" else "rejected"
        db.set_event_status(DB_PATH, event_id, status)

        if decision == "approve":
            row = db.get_work_order(DB_PATH, event_id)
            _mark(event_id, status="approved", work_order_id=row["id"] if row else None)
            broadcast(
                {
                    "type": "work_order",
                    "event_id": event_id,
                    "work_order": _work_order_payload(row),
                    "counts": counts(),
                }
            )
            return {
                "ok": True,
                "duplicate": False,
                "decision": "approve",
                "event_id": event_id,
                "thread_id": event_id,
                "work_order": _work_order_payload(row),
            }

        _mark(event_id, status="rejected")
        broadcast({"type": "rejected", "event_id": event_id, "counts": counts()})
        return {
            "ok": True,
            "duplicate": False,
            "decision": "reject",
            "event_id": event_id,
            "thread_id": event_id,
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
    STATE.update(
        {
            "app": ag.build_app(DB_PATH, analyze_fn=analyze_fn),
            "analyzer_source": source,
            "analyzer_model": model,
            "analyze": analyze_fn,
        }
    )

    mqtt = MqttBridge(
        MQTT_HOST,
        MQTT_PORT,
        MQTT_TOPIC,
        lambda ev: _ingest(ev, source="mqtt"),
    )
    mqtt.start()
    STATE["mqtt"] = mqtt

    print(f"[backend] db={DB_PATH}")
    print(f"[backend] analyzer={source} model={model}")
    print(f"[backend] mqtt={MQTT_HOST}:{MQTT_PORT} topic={MQTT_TOPIC}")
    print(f"[backend] http://127.0.0.1:{HTTP_PORT}  ws://127.0.0.1:{HTTP_PORT}/ws")
    yield
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
@app.post("/events", status_code=202)
def post_event(event: dict[str, Any]) -> dict[str, Any]:
    return _ingest(event, source="rule_engine")


@app.post("/approve/{event_id}")
def approve(event_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    return _decide(event_id, "approve")


@app.post("/reject/{event_id}")
def reject(event_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    return _decide(event_id, "reject")


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


@app.post("/api/approvals/{event_id}")
def api_approval(event_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    decision = str((body or {}).get("decision", "approve")).strip().lower()
    if decision in {"approve", "approved", "批准", "同意"}:
        return _decide(event_id, "approve")
    if decision in {"reject", "rejected", "驳回", "拒绝"}:
        return _decide(event_id, "reject")
    raise HTTPException(status_code=422, detail=f"非法 decision: {decision!r}")


@app.post("/api/dev/inject")
def api_inject(event: dict[str, Any]) -> dict[str, Any]:
    return _ingest(event, source="dev")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="info")
