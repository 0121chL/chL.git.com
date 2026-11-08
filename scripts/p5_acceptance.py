#!/usr/bin/env python3
"""第五阶段验收：REST 契约 + WebSocket 推送 + 重复批准幂等。

    python scripts/p5_acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"
RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   -> {detail}" if detail else ""))
    return ok


TOKEN = ""


def request(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def bootstrap_user(username: str = "测试审批人", password: str = "test123456") -> bool:
    """注册（或登录）一个验收用账号，后续请求都带它的会话 token。"""
    global TOKEN
    status, body = request("POST", "/api/auth/register", {"username": username, "password": password})
    if status == 409:
        status, body = request("POST", "/api/auth/login", {"username": username, "password": password})
    if status != 200 or not body.get("token"):
        print(f"  登录取 token 失败：{status} {body}")
        return False
    TOKEN = body["token"]
    return True


def make_event(tag: str) -> dict[str, Any]:
    eid = f"evt-p5-{tag}-{uuid.uuid4().hex[:8]}"
    return {
        "event_id": eid,
        "schema_version": 1,
        "line": "line1",
        "sensor": "ch4",
        "rule_id": "R-CH4-RISE",
        "event_type": "gas_surge",
        "severity": "high",
        "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "observed": {"value": 1.8, "unit": "%", "threshold": 1.0},
        "window": {"samples": 60, "mean": 0.9, "max": 1.8, "slope_per_min": 0.65},
        "snapshot": {
            "sensors": {
                "ch4": {"value": 1.8, "unit": "%", "delta_1m": 0.62},
                "temp": {"value": 58.3, "unit": "C", "delta_1m": 7.5},
                "co": {"value": 8, "unit": "ppm", "delta_1m": 0.2},
                "airflow": {"value": 3.1, "unit": "m3/s", "delta_1m": -0.9},
            }
        },
        "scenario_id": "p5_acceptance",
    }


def wait_ready(event_id: str, timeout: float = 320) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = request("GET", f"/events/{event_id}", timeout=5)
        if status == 200 and body.get("status") == "awaiting_approval":
            return body
        if status == 200 and str(body.get("status", "")).endswith("failed"):
            print(f"      分析失败：{body.get('error')}")
            return body
        time.sleep(0.5)
    return None


def test_rest_and_idempotency() -> bool:
    print("\n--- A. POST /events → 分析 → 批准 ---")
    event = make_event("a")
    eid = event["event_id"]
    status, body = request("POST", "/events", event)
    check(status == 202, "POST /events 返回 202", f"{status} {body}")
    check(body.get("thread_id") == eid, "thread_id == event_id", str(body.get("thread_id")))

    record = wait_ready(eid)
    if not check(record is not None, "图停在 awaiting_approval（320s 内）", str((record or {}).get("status"))):
        return False

    analysis = (record or {}).get("analysis") or {}
    check(bool(analysis.get("regulation_basis")), "分析含 regulation_basis", str(analysis.get("regulation_basis"))[:40])

    status, body = request(
        "POST",
        f"/approve/{eid}",
        {"approver": "测试审批人", "comment": "自动化验收：同意降载"},
    )
    check(status == 200, "POST /approve 返回 200", str(status))
    first = body.get("work_order") or {}
    check(bool(first.get("id")), "返回工单 JSON", f"wo={first.get('id')}")
    check(first.get("approver") == "测试审批人", "工单带审批人", str(first.get("approver")))
    check(first.get("approval_comment") == "自动化验收：同意降载", "工单带审批意见", str(first.get("approval_comment")))
    check(bool(first.get("decided_at")), "工单带审批时间", str(first.get("decided_at")))

    # 关键：重复批准
    print("\n--- B. 同一 event_id 重复 /approve ---")
    status, body = request("POST", f"/approve/{eid}", {"approver": "另一个人"})
    second = body.get("work_order") or {}
    check(status == 200, "重复批准仍返回 200（幂等）", str(status))
    check(body.get("duplicate") is True, "响应标记 duplicate=true", str(body.get("duplicate")))
    check(second.get("id") == first.get("id"), "返回同一张工单（id 相同）", f"{first.get('id')} vs {second.get('id')}")
    check(second.get("approver") == "测试审批人", "审批人不会被第二次调用覆盖", str(second.get("approver")))

    status, body = request("POST", f"/approve/{eid}", {"approver": "测试审批人"})
    check((body.get("work_order") or {}).get("id") == first.get("id"), "第三次批准仍不新建")

    _, listing = request("GET", "/work_orders")
    same = [o for o in listing["work_orders"] if o["event_id"] == eid]
    check(len(same) == 1, "SQLite 中该事件只有 1 张工单", f"count={len(same)}")
    return True


def test_reject() -> bool:
    print("\n--- C. /reject 与重复驳回 ---")
    event = make_event("b")
    eid = event["event_id"]
    request("POST", "/events", event)
    record = wait_ready(eid)
    if not check(record is not None, "待审批就绪"):
        return False

    status, body = request(
        "POST", f"/reject/{eid}", {"approver": "测试审批人", "comment": "自动化验收：现场已恢复"}
    )
    check(status == 200 and body.get("decision") == "reject", "POST /reject 成功", str(body.get("decision")))
    check(body.get("approver") == "测试审批人", "驳回也记录审批人", str(body.get("approver")))

    status, body = request("POST", f"/reject/{eid}")
    check(status == 409, "重复驳回返回 409（状态机已离开审批点）", str(status))

    _, listing = request("GET", "/work_orders")
    check(not any(o["event_id"] == eid for o in listing["work_orders"]), "驳回不产生工单")
    return True


async def test_websocket() -> bool:
    print("\n--- D. WS /ws 推送 ---")
    import websockets

    event = make_event("ws")
    eid = event["event_id"]

    async with websockets.connect(WS, open_timeout=10) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check(hello.get("type") == "hello", "收到 hello 快照", str(hello.get("type")))

        request("POST", "/events", event)

        seen: set[str] = set()
        work_order_id = None
        deadline = time.time() + 120
        while time.time() < deadline and "work_order" not in seen:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if msg.get("event_id") != eid:
                continue
            seen.add(msg.get("type", ""))
            if msg.get("type") == "analysis":
                request("POST", f"/approve/{eid}", {"approver": "测试审批人"})
            if msg.get("type") == "work_order":
                work_order_id = (msg.get("work_order") or {}).get("id")

        check("event" in seen, "WS 推送 event（新事件）", str(sorted(seen)))
        check("analysis" in seen, "WS 推送 analysis（分析完成）", str(sorted(seen)))
        check("work_order" in seen, "WS 推送 work_order（工单创建）", f"wo={work_order_id}")
    return True


def main() -> int:
    try:
        status, body = request("GET", "/api/status", timeout=5)
        print(f"backend: {status} analyzer={body.get('analyzer')} counts={body.get('counts')}")
        check(body.get("checkpointer") == "sqlite", "断点持久化 = SQLite（重启可恢复）", str(body.get("checkpointer")))
    except Exception as exc:  # noqa: BLE001
        print(f"后端不可用：{exc}")
        return 2

    if not bootstrap_user():
        print("无法登录，后续写操作会 401")
        return 2

    ok = test_rest_and_idempotency()
    ok = test_reject() and ok
    ok = asyncio.run(test_websocket()) and ok

    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 66)
    if failed:
        print(f"P5 FAILED: {len(failed)}/{len(RESULTS)} 条断言未通过")
        for _, name, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print(f"P5 OK: {len(RESULTS)}/{len(RESULTS)} 条断言通过（含重复批准幂等、WS 三类推送）")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
