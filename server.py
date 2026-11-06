"""第四阶段后端：FastAPI + SSE + 审批恢复。

    python server.py            # 127.0.0.1:8000

对外接口：
    GET  /api/status                  MQTT 状态、计数、分析引擎
    GET  /api/events                  事件对象 + 分析结果（SQLite）
    GET  /api/work-orders             已批准工单（SQLite）
    GET  /api/stream                  SSE：hello / event / analysis / work_order / rejected
    POST /api/approvals/{event_id}    人工审批 → Command(resume=...)
    POST /api/dev/inject              HTTP 注入事件（无 MQTT 时的演示通道）
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException
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

BUS = EventBus()
PIPELINE_LOCK = threading.Lock()

STATE: dict[str, Any] = {
    "app": None,
    "mqtt": None,
    "analyzer_source": "offline-template",
    "analyzer_model": "—",
    "analyze": offline_analyzer.analyze,
}


# --------------------------------------------------------------------------- #
# 分析引擎选择：启动时探测 Ollama，失败则退到离线模板（UI 会显示来源）
# --------------------------------------------------------------------------- #
def pick_analyzer() -> tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]:
    try:
        model = llm_client.ensure_ready()
    except Exception:  # noqa: BLE001
        return "offline-template", "—", offline_analyzer.analyze

    if llm_client.is_stub():
        return "ollama-stub", model, analyzer.analyze_event
    return "ollama", model, analyzer.analyze_event


# --------------------------------------------------------------------------- #
# 管线：事件 → 分析 → 图（停在 interrupt）→ SSE
# --------------------------------------------------------------------------- #
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
        "subscribers": BUS.subscriber_count,
    }


def _work_order_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """DB 里 content 是 JSON 文本，推给前端前解析成对象。"""
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
                {
                    "clause_id": clause["clause_id"],
                    "title": clause["title"],
                    "text": clause["text"],
                }
            )
    return citations


def process_event(event: dict[str, Any]) -> None:
    """串行处理：同一时刻只允许一个事件穿过「分析 + 建图」段。"""
    app = STATE["app"]
    event_id = event.get("event_id")
    if not event_id:
        return

    with PIPELINE_LOCK:
        db.upsert_event(DB_PATH, event_id, event)
        BUS.publish({"type": "event", "event": event, "counts": counts()})

        try:
            analysis = STATE["analyze"](event)
        except Exception as exc:  # noqa: BLE001 - 分析失败要显式告诉前端
            db.set_event_status(DB_PATH, event_id, "analysis_failed")
            BUS.publish(
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
            app.invoke({"event": event}, ag.thread_config(event_id))
            snap = app.get_state(ag.thread_config(event_id))
            print(f"[pipeline] {event_id} invoked, next={snap.next}", flush=True)
        except Exception as exc:  # noqa: BLE001
            db.set_event_status(DB_PATH, event_id, "graph_failed")
            BUS.publish(
                {
                    "type": "analysis_error",
                    "event_id": event_id,
                    "error": f"图执行失败: {exc}",
                    "counts": counts(),
                }
            )
            return

        # 只有图确实停在审批点之后，才把分析写进 DB / 推给前端，
        # 避免前端在检查点就绪之前点「批准」而拿到 409。
        db.save_event_analysis(DB_PATH, event_id, analysis, citations)

        BUS.publish(
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


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class ApprovalBody(BaseModel):
    decision: str = "approve"
    comment: str | None = None


def _decision(value: str) -> str:
    token = (value or "").strip().lower()
    if token in {"approve", "approved", "批准", "同意"}:
        return "approve"
    if token in {"reject", "rejected", "驳回", "拒绝"}:
        return "reject"
    raise HTTPException(status_code=422, detail=f"非法 decision: {value!r}，只接受 approve / reject")


@asynccontextmanager
async def lifespan(_: FastAPI):
    BUS.bind_loop(asyncio.get_running_loop())
    db.init_db(DB_PATH)

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
        lambda ev: threading.Thread(target=process_event, args=(ev,), daemon=True).start(),
    )
    mqtt.start()
    STATE["mqtt"] = mqtt

    print(f"[server] db={DB_PATH}")
    print(f"[server] analyzer={source} model={model}")
    print(f"[server] mqtt={MQTT_HOST}:{MQTT_PORT} topic={MQTT_TOPIC}")
    yield
    mqtt.stop()


app = FastAPI(title="安全监测 Agent Demo", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return status_payload()


@app.get("/api/events")
def get_events(limit: int = 50) -> dict[str, Any]:
    return {"events": db.list_events(DB_PATH, limit=limit)}


@app.get("/api/work-orders")
def get_work_orders(limit: int = 50) -> dict[str, Any]:
    return {"work_orders": db.list_work_orders(DB_PATH, limit=limit)}


@app.get("/api/stream")
async def stream() -> StreamingResponse:
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
def approve(event_id: str, body: ApprovalBody) -> dict[str, Any]:
    decision = _decision(body.decision)
    graph = STATE["app"]
    config = ag.thread_config(event_id)

    # 与管线共用一把锁：若该事件正在「分析 → 建图」中，这里会等它真正停在审批点，
    # 而不是读到空检查点就回 409。
    with PIPELINE_LOCK:
        snapshot = graph.get_state(config)
        if not snapshot.next:
            raise HTTPException(status_code=409, detail="该事件不在待审批状态（可能已批准/驳回）")

        result = graph.invoke(Command(resume=decision), config)
        status = "approved" if decision == "approve" else "rejected"
        db.set_event_status(DB_PATH, event_id, status)

        if decision == "approve":
            row = db.get_work_order(DB_PATH, event_id)
            BUS.publish(
                {
                    "type": "work_order",
                    "event_id": event_id,
                    "work_order": _work_order_payload(row),
                    "counts": counts(),
                }
            )
        else:
            BUS.publish(
                {"type": "rejected", "event_id": event_id, "counts": counts()}
            )

    return {
        "ok": True,
        "decision": decision,
        "event_id": event_id,
        "work_order": _work_order_payload(result.get("work_order")),
        "comment": body.comment,
    }


@app.post("/api/dev/inject")
def dev_inject(event: dict[str, Any]) -> dict[str, Any]:
    if "event_id" not in event:
        raise HTTPException(status_code=422, detail="事件缺少 event_id")
    threading.Thread(target=process_event, args=(event,), daemon=True).start()
    return {"accepted": True, "event_id": event["event_id"], "analyzer": STATE["analyzer_source"]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="info")
