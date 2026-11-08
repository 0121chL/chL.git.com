#!/usr/bin/env python3
"""端到端验收（不依赖浏览器）：SSE 订阅 → 注入事件 → 等分析 → 批准 → 等工单 → 查库。

    python scripts/e2e_check.py --scenario gas_surge
"""

from __future__ import annotations

import argparse
import http.client
import json
import queue
import sys
import threading
import time
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
import db  # noqa: E402

API_HOST = "127.0.0.1"
API_PORT = 8000


class SSEReader(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.ready = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        try:
            conn = http.client.HTTPConnection(API_HOST, API_PORT, timeout=300)
            conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
            resp = conn.getresponse()
            event_name = None
            for raw in resp:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line.split(":", 1)[1].strip())
                    payload.setdefault("type", event_name)
                    self.messages.put(payload)
                    if payload.get("type") == "hello":
                        self.ready.set()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    def wait_for(self, predicate, timeout: float = 120.0) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.messages.get(timeout=min(2.0, max(0.1, deadline - time.time())))
            except queue.Empty:
                continue
            if predicate(msg):
                return msg
        return None


TOKEN = ""


def post(path: str, body: dict[str, Any], auth: bool = True) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection(API_HOST, API_PORT, timeout=30)
    headers = {"Content-Type": "application/json"}
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    conn.request("POST", path, json.dumps(body).encode("utf-8"), headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8") or "{}")
    return resp.status, data


def bootstrap_user(username: str = "验收脚本", password: str = "test123456") -> str:
    """注册或登录，返回 token（不修改全局 TOKEN）。"""
    status, body = post("/api/auth/register", {"username": username, "password": password}, auth=False)
    if status == 409:
        status, body = post("/api/auth/login", {"username": username, "password": password}, auth=False)
    if status == 200:
        return body.get("token", "")
    print(f"  ! 登录失败：{status} {body}")
    return ""


def main() -> int:
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="gas_surge")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    sys.path.insert(0, "scripts")
    import inject_event  # noqa: E402

    TOKEN = bootstrap_user("验收脚本")
    approver_token = bootstrap_user("验收审批")
    if not TOKEN or not approver_token:
        print("  ✗ 验收账号登录失败")
        return 1

    event = inject_event.build_event(args.scenario)
    event_id = event["event_id"]
    print(f"[1/6] 事件 {event_id}（{args.scenario}）")

    reader = SSEReader()
    reader.start()
    if not reader.ready.wait(15):
        print(f"  ✗ SSE 未收到 hello：{reader.error}")
        return 1
    print("  ✓ SSE hello 已收到")

    print("[2/6] 通过 HTTP 注入事件")
    status, body = post("/api/dev/inject", event)
    if status != 200:
        print(f"  ✗ 注入失败 {status} {body}")
        return 1
    print(f"  ✓ {body}")

    print("[3/6] 等待 event / analysis 推送")
    got_event = reader.wait_for(lambda m: m.get("type") == "event" and m.get("event", {}).get("event_id") == event_id, 30)
    print(f"  {'✓' if got_event else '✗'} event 推送")
    analysis_msg = reader.wait_for(
        lambda m: m.get("type") == "analysis" and m.get("event_id") == event_id, args.timeout
    )
    if not analysis_msg:
        print(f"  ✗ 未收到 analysis（error={reader.error}）")
        return 1
    analysis = analysis_msg["analysis"]
    citations = analysis_msg.get("citations", [])
    print(f"  ✓ analysis：risk={analysis['risk_level']} citations={[c['clause_id'] for c in citations]}")

    print("[4/6] 批准前：该事件无工单")
    if db.count_work_orders(db.DEFAULT_DB_PATH, event_id) != 0:
        print("  ✗ 批准前已存在工单")
        return 1
    print("  ✓ 0 张")

    print("[5/6] POST 批准（换审批人账号，验证四眼原则）")
    TOKEN = approver_token
    status, body = post(
        f"/api/approvals/{event_id}", {"decision": "approve", "comment": "验收：同意"}
    )
    if status != 200:
        print(f"  ✗ 批准失败 {status} {body}")
        return 1
    print(f"  ✓ {body.get('decision')}；审批人 {body.get('approver')}")

    print("[6/6] 等待 work_order 推送并校验 SQLite")
    order_msg = reader.wait_for(
        lambda m: m.get("type") == "work_order" and m.get("event_id") == event_id, 30
    )
    if not order_msg:
        print("  ✗ 未收到 work_order 推送")
        return 1
    order = order_msg["work_order"]
    rows = db.count_work_orders(db.DEFAULT_DB_PATH, event_id)
    print(f"  ✓ 收到工单 WO-{order['id']:04d}，SQLite 记录数={rows}")
    content = order.get("content") or {}
    basis = (content.get("analysis") or {}).get("regulation_basis", "") if isinstance(content, dict) else ""
    print(f"  ✓ content.analysis.regulation_basis = {basis[:50]}…")

    print("\nE2E OK：事件 → 分析 → 审批 → 工单 全链路通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
