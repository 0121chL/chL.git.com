"""HTTP 层的回归：审批闸门、鉴权边界、SSE 票据。

这组测试守的是"不会因为改一行就悄悄 fail-open"——之前 `POST /api/approvals`
在 body 缺 `decision` 时默认批准，任何重构都会让这种洞悄悄回来。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import backend_server

    with TestClient(backend_server.app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth(client):
    import backend_server as bs

    name = f"pytest_{uuid.uuid4().hex[:6]}"
    res = client.post(
        "/api/auth/register",
        json={
            "username": name,
            "password": "pytest12345",
            "role": "approver",
            "invite_code": bs.APPROVER_INVITE,
        },
    )
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    WAIT_HEADERS[0] = headers
    return {"headers": headers, "username": name, "role": "approver"}


def make_event(tag: str, device: str = "esp32s3-line1-01", sensor: str = "ch4") -> dict:
    return {
        "event_id": f"evt-test-{tag}-{uuid.uuid4().hex[:6]}",
        "schema_version": 1,
        "line": "line1",
        "device_id": device,
        "sensor": sensor,
        "rule_id": "R-CH4-HIGH",
        "event_type": "threshold_exceeded",
        "severity": "high",
        "triggered_at": "2026-11-08T22:00:00+08:00",
        "observed": {"value": 1.6, "unit": "%", "threshold": 1.0},
        "window": {"samples": 60, "mean": 1.2, "max": 1.6, "slope_per_min": 0.7},
        "snapshot": {"sensors": {"ch4": {"value": 1.6, "unit": "%", "delta_1m": 0.6}}},
        "origin": "rule_engine",
    }


def wait_awaiting(client, event_id: str, timeout: float = 90.0) -> dict:
    """等事件真正停在 interrupt 上；分析中/排队中都不算就绪。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/events/{event_id}", headers=WAIT_HEADERS[0])
        if res.status_code == 200:
            last = res.json()
            if last.get("status") == "awaiting_approval":
                return last
            if str(last.get("status", "")).endswith("failed"):
                raise AssertionError(f"{event_id} 分析失败：{last.get('error')}")
        time.sleep(0.3)
    raise AssertionError(f"{event_id} 在 {timeout}s 内没有停到 awaiting_approval：{last}")


WAIT_HEADERS: list[dict] = [{}]


# --------------------------------------------------------------------------- #
# 鉴权边界
# --------------------------------------------------------------------------- #
def test_status_and_metrics_stay_open(client):
    assert client.get("/api/status").status_code == 200
    text = client.get("/metrics").text
    assert "queue_depth" in text and "events_memory" in text


@pytest.mark.parametrize(
    "path",
    ["/api/events", "/api/work-orders", "/work_orders", "/events", "/api/telemetry", "/api/alerts"],
)
def test_read_endpoints_require_login(client, path):
    assert client.get(path).status_code == 401


def test_stream_requires_ticket(client, auth):
    assert client.get("/api/stream").status_code == 401
    assert client.get("/api/stream", params={"ticket": "nope"}).status_code == 401

    res = client.post("/api/auth/stream-ticket", headers=auth["headers"])
    assert res.status_code == 200
    ticket = res.json()["ticket"]
    assert len(ticket) >= 32


# --------------------------------------------------------------------------- #
# 审批闸门
# --------------------------------------------------------------------------- #
def test_approval_requires_explicit_decision(client, auth):
    """缺 decision 必须 422 —— 绝不默认批准。"""
    for body in ({}, {"comment": "随便"}):
        res = client.post("/api/approvals/evt-whatever", json=body, headers=auth["headers"])
        assert res.status_code == 422, res.text

    res = client.post("/api/approvals/evt-whatever", json={"decision": "maybe"}, headers=auth["headers"])
    assert res.status_code == 422


def test_high_severity_cannot_end_up_low_risk(client, auth):
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("risk")
    assert client.post("/events", json=event).status_code == 202

    record = wait_awaiting(client, event["event_id"])
    assert record["status"] == "awaiting_approval"
    assert record["analysis"]["risk_level"] == "high", record["analysis"]


def test_approve_creates_one_work_order_and_is_idempotent(client, auth):
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("approve")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    first = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "approve", "comment": "回归测试：同意"},
        headers=auth["headers"],
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["duplicate"] is False
    assert body["work_order"]["approver"] == auth["username"]
    work_order_id = body["work_order"]["id"]

    again = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "approve"},
        headers=auth["headers"],
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["work_order"]["id"] == work_order_id

    listed = client.get(f"/work_orders?limit=500", headers=auth["headers"])
    matches = [row for row in listed.json()["work_orders"] if row["event_id"] == event["event_id"]]
    assert len(matches) == 1


def test_reject_then_repeat_is_conflict(client, auth):
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("reject")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    first = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "reject", "comment": "回归测试：现场已恢复"},
        headers=auth["headers"],
    )
    assert first.status_code == 200
    assert first.json()["decision"] == "reject"

    second = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "reject"},
        headers=auth["headers"],
    )
    assert second.status_code == 409


def test_cannot_approve_own_injected_event(client, auth):
    """四眼原则：手工注入的人不能自己批准。"""
    event = make_event("four-eyes")
    res = client.post("/api/dev/inject", json=event, headers=auth["headers"])
    assert res.status_code == 200, res.text

    WAIT_HEADERS[0] = auth["headers"]
    wait_awaiting(client, event["event_id"])

    res = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "approve"},
        headers=auth["headers"],
    )
    assert res.status_code == 403, res.text


# --------------------------------------------------------------------------- #
# 驳回原因结构化（P0-2）
# --------------------------------------------------------------------------- #
def _approve_new_event(client, auth, tag: str, device: str = "esp32s3-line1-01") -> dict:
    """投一条事件 → 等它停在审批点 → 批准 → 返回工单。"""
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event(tag, device=device)
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])
    res = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "approve"},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    order = res.json()["work_order"]
    assert order and order["id"]
    return order


def test_reject_reason_code_is_recorded_and_counted(client, auth):
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("reason")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    res = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "reject", "reason_code": "false_alarm", "comment": "误报：打磨粉尘"},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reason_code"] == "false_alarm"
    assert body["reason_label"] == "误报"

    # 工单列表里也能看到驳回原因（重新提交之前该事件只有审批记录，没有工单）
    stats = client.get("/api/stats/rejections?days=7", headers=auth["headers"])
    assert stats.status_code == 200, stats.text
    payload = stats.json()
    assert payload["total"] >= 1
    assert any(row["reason_code"] == "false_alarm" for row in payload["by_reason"])
    assert any(row["rule_id"] == "R-CH4-HIGH" for row in payload["by_rule"])


def test_reject_rejects_unknown_reason_code(client, auth):
    res = client.post(
        "/api/approvals/evt-does-not-exist",
        json={"decision": "reject", "reason_code": "because-i-say-so"},
        headers=auth["headers"],
    )
    assert res.status_code == 422, res.text


# --------------------------------------------------------------------------- #
# 处置后验证（P0-1）
# --------------------------------------------------------------------------- #
def _insert_ch4(value: float, device: str) -> None:
    import backend_server as bs
    import db

    db.insert_telemetry(bs.DB_PATH, db.utcnow(), device, "ch4", value, "%")


def test_close_without_telemetry_requires_exception(client, auth):
    """没有遥测样本 ≠ 已恢复：必须给例外理由才能关。"""
    order = _approve_new_event(client, auth, "close-nodata", device=f"dev-nodata-{uuid.uuid4().hex[:5]}")

    res = client.post(
        f"/api/work_orders/{order['id']}/close",
        json={"resolution": "已处理，复测正常"},
        headers=auth["headers"],
    )
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert "verification" in detail
    assert detail["verification"]["ok"] is False
    assert detail["verification"]["mode"] == "telemetry"
    assert detail["verification"]["reason"] == "no_data"

    # 太短的例外理由不认
    res = client.post(
        f"/api/work_orders/{order['id']}/close",
        json={"resolution": "已处理，复测正常", "exception_reason": "没事"},
        headers=auth["headers"],
    )
    assert res.status_code == 409

    # 给足理由 → 放行，但必须留痕
    res = client.post(
        f"/api/work_orders/{order['id']}/close",
        json={"resolution": "已处理，复测正常", "exception_reason": "现场复测已合格，遥测通道断线"},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    closed = res.json()["work_order"]
    assert closed["status"] == "closed"
    assert closed["exception_reason"].startswith("现场复测已合格")
    assert closed["verification"]["ok"] is False

    import backend_server as bs
    import db

    assert db.count_audit(bs.DB_PATH, f"work_order:{order['id']}", "closed_with_exception") == 1


def test_close_blocked_while_still_over_threshold(client, auth):
    device = f"dev-hot-{uuid.uuid4().hex[:5]}"
    order = _approve_new_event(client, auth, "close-hot", device=device)
    for _ in range(3):
        _insert_ch4(1.6, device)

    res = client.post(
        f"/api/work_orders/{order['id']}/close",
        json={"resolution": "已处理"},
        headers=auth["headers"],
    )
    assert res.status_code == 409, res.text
    verification = res.json()["detail"]["verification"]
    assert verification["ok"] is False
    assert verification["max"] == 1.6
    assert "尚未恢复" in verification["message"]


def test_close_allowed_after_recovered(client, auth):
    device = f"dev-ok-{uuid.uuid4().hex[:5]}"
    order = _approve_new_event(client, auth, "close-ok", device=device)
    for _ in range(3):
        _insert_ch4(0.2, device)

    res = client.post(
        f"/api/work_orders/{order['id']}/close",
        json={"resolution": "已复测合格"},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    closed = res.json()["work_order"]
    assert closed["status"] == "closed"
    assert closed["verification"]["ok"] is True
    assert closed["exception_reason"] in (None, "")


# --------------------------------------------------------------------------- #
# 用户设置
# --------------------------------------------------------------------------- #
def test_settings_defaults_and_roundtrip(client, auth):
    res = client.get("/api/settings", headers=auth["headers"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["settings"]["retest_window_minutes"] == 10
    assert body["settings"]["confirm_before_approve"] is True
    assert "false_alarm" in body["reject_reasons"]

    res = client.put(
        "/api/settings",
        json={"settings": {"retest_window_minutes": 30, "alert_sound": True, "event_page_size": 100}},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["retest_window_minutes"] == 30

    again = client.get("/api/settings", headers=auth["headers"]).json()
    assert again["settings"]["retest_window_minutes"] == 30
    assert again["settings"]["alert_sound"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {"retest_window_minutes": 0},
        {"retest_window_minutes": 999},
        {"event_page_size": 33},
        {"alert_sound": "maybe"},
        {"default_reject_reason": "not-a-code"},
    ],
)
def test_settings_rejects_invalid_values(client, auth, bad):
    res = client.put("/api/settings", json={"settings": bad}, headers=auth["headers"])
    assert res.status_code == 422, res.text


def test_settings_ignores_unknown_keys(client, auth):
    before = client.get("/api/settings", headers=auth["headers"]).json()["settings"]
    res = client.put(
        "/api/settings",
        json={"settings": {"something_else": "x"}},
        headers=auth["headers"],
    )
    assert res.status_code == 200
    assert "something_else" not in res.json()["settings"]
    assert res.json()["settings"]["retest_window_minutes"] == before["retest_window_minutes"]


def test_settings_require_login(client):
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json={"settings": {}}).status_code == 401


def test_password_change_flow(client):
    name = f"pwd_{uuid.uuid4().hex[:6]}"
    reg = client.post("/api/auth/register", json={"username": name, "password": "origin12345"})
    assert reg.status_code == 200, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}

    # 旧密码错 → 401
    res = client.post(
        "/api/auth/password",
        json={"old_password": "wrong", "new_password": "changed12345"},
        headers=headers,
    )
    assert res.status_code == 401

    # 新密码不合规 → 422
    res = client.post(
        "/api/auth/password",
        json={"old_password": "origin12345", "new_password": "short"},
        headers=headers,
    )
    assert res.status_code == 422

    # 正常改密
    res = client.post(
        "/api/auth/password",
        json={"old_password": "origin12345", "new_password": "changed12345"},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    # 旧密码失效、新密码可登录
    assert (
        client.post("/api/auth/login", json={"username": name, "password": "origin12345"}).status_code
        == 401
    )
    assert (
        client.post("/api/auth/login", json={"username": name, "password": "changed12345"}).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# P0-4：规则卡先出，AI 后补
# --------------------------------------------------------------------------- #
def test_rule_card_is_approvable_before_ai(client, auth, monkeypatch):
    """模型被卡住时，事件照样能立刻审批 —— 这是 P0-4 的核心承诺。"""
    import threading

    import backend_server as bs

    gate = threading.Event()
    calls = {"n": 0}

    def slow_analyze(event: dict) -> dict:
        calls["n"] += 1
        gate.wait(timeout=20)
        return {
            "root_cause_hypothesis": "AI 迟到的结论",
            "regulation_basis": "第一百三十八条：瓦斯涌出异常",
            "recommended_action": "撤人（第一百三十八条）",
            "risk_level": "high",
        }

    monkeypatch.setitem(bs.STATE, "analyze", slow_analyze)

    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("defer")
    assert client.post("/events", json=event).status_code == 202

    record = wait_awaiting(client, event["event_id"])
    assert record["analysis_source"] == "rule-card"
    assert record["analysis_pending"] is True
    assert record["analysis"]["root_cause_hypothesis"].startswith("[规则处置卡]")
    assert record["analysis"]["regulation_basis"]  # 规则卡也带条款

    # 关键：AI 还卡着，审批已经可以走
    approved = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "approve"},
        headers=auth["headers"],
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["work_order"]["id"]

    # 放行 AI，结论后到，并且标记为“审批后到达”
    gate.set()
    deadline = time.time() + 30
    final: dict = {}
    while time.time() < deadline:
        current = client.get(f"/events/{event['event_id']}", headers=auth["headers"]).json()
        if (
            current.get("analysis_pending") is False
            and (current.get("analysis") or {}).get("root_cause_hypothesis") == "AI 迟到的结论"
        ):
            final = current
            break
        time.sleep(0.2)
    assert final, "AI 结论没有补写回来"
    assert final["analysis_late"] is True
    assert final["status"] == "approved"  # 补结论不会把状态打回待审批
    assert calls["n"] >= 1


def test_ai_failure_does_not_block_approval(client, auth, monkeypatch):
    import backend_server as bs

    def broken(event: dict) -> dict:
        raise RuntimeError("模型炸了")

    monkeypatch.setitem(bs.STATE, "analyze", broken)

    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("ai-fail")
    assert client.post("/events", json=event).status_code == 202

    record = wait_awaiting(client, event["event_id"])
    assert record["status"] == "awaiting_approval"

    # 等后台把失败写回来
    deadline = time.time() + 20
    while time.time() < deadline:
        current = client.get(f"/events/{event['event_id']}", headers=auth["headers"]).json()
        if current.get("analysis_error"):
            break
        time.sleep(0.2)
    current = client.get(f"/events/{event['event_id']}", headers=auth["headers"]).json()
    assert "模型炸了" in str(current.get("analysis_error"))
    assert current["status"] == "awaiting_approval"  # 仍然可以审批

    approved = client.post(
        f"/api/approvals/{event['event_id']}",
        json={"decision": "approve"},
        headers=auth["headers"],
    )
    assert approved.status_code == 200, approved.text


def test_retry_ai_on_awaiting_event(client, auth, monkeypatch):
    import backend_server as bs

    monkeypatch.setitem(bs.STATE, "analyze", lambda event: {"root_cause_hypothesis": "重试后的结论",
                                                            "regulation_basis": "第一百三十八条：x",
                                                            "recommended_action": "y（第一百三十八条）",
                                                            "risk_level": "high"})
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("retry-ai")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    res = client.post(f"/events/{event['event_id']}/reanalyze", headers=auth["headers"])
    assert res.status_code == 200, res.text
    assert res.json()["status"] in ("analysis_retrying", "queued")


# --------------------------------------------------------------------------- #
# P0-3：持续告警刷新
# --------------------------------------------------------------------------- #
def test_event_refresh_merges_aggregate_without_new_approval(client, auth):
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("refresh")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    aggregate = {
        "count": 42,
        "sustained_s": 1234.5,
        "peak": 2.1,
        "current": 1.7,
        "unit": "%",
        "threshold": 1.0,
        "cleared": False,
    }
    res = client.post(f"/events/{event['event_id']}/refresh", json={"aggregate": aggregate})
    assert res.status_code == 200, res.text

    record = client.get(f"/events/{event['event_id']}", headers=auth["headers"]).json()
    assert record["event"]["aggregate"]["count"] == 42
    assert record["event"]["aggregate"]["sustained_s"] == 1234.5
    # 不新建事件、不改变审批状态
    assert record["status"] == "awaiting_approval"

    import backend_server as bs

    stored = bs.db.get_event(bs.DB_PATH, event["event_id"])
    assert stored["event"]["aggregate"]["count"] == 42

    # 恢复也要能写回去
    cleared = dict(aggregate, cleared=True, current=0.2)
    assert client.post(f"/events/{event['event_id']}/refresh", json={"aggregate": cleared}).status_code == 200
    again = client.get(f"/events/{event['event_id']}", headers=auth["headers"]).json()
    assert again["event"]["aggregate"]["cleared"] is True


def test_refresh_unknown_event_is_404(client):
    res = client.post("/events/evt-not-there/refresh", json={"aggregate": {"count": 1}})
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# 负责人表 + 站内待办 + 通知
# --------------------------------------------------------------------------- #
def _new_order(client, auth, tag: str) -> dict:
    return _approve_new_event(client, auth, tag, device=f"dev-{tag}-{uuid.uuid4().hex[:5]}")


def test_staff_create_list_disable(client, auth):
    name = f"张三{uuid.uuid4().hex[:4]}"
    created = client.post(
        "/api/staff", json={"name": name, "team": "甲班", "phone": "13800000000"}, headers=auth["headers"]
    )
    assert created.status_code == 200, created.text
    staff = created.json()["staff"]
    assert staff["name"] == name and staff["active"] == 1

    listed = client.get("/api/staff?active_only=true", headers=auth["headers"]).json()["staff"]
    assert any(item["id"] == staff["id"] for item in listed)

    # 重名 → 409
    dup = client.post("/api/staff", json={"name": name}, headers=auth["headers"])
    assert dup.status_code == 409

    # 停用后不再出现在 active 列表，但全量列表里还在
    assert client.delete(f"/api/staff/{staff['id']}", headers=auth["headers"]).status_code == 200
    active = client.get("/api/staff?active_only=true", headers=auth["headers"]).json()["staff"]
    assert all(item["id"] != staff["id"] for item in active)
    everything = client.get("/api/staff?active_only=false", headers=auth["headers"]).json()["staff"]
    assert any(item["id"] == staff["id"] and item["active"] == 0 for item in everything)


def test_staff_binding_requires_existing_account(client, auth):
    res = client.post(
        "/api/staff",
        json={"name": f"无账号{uuid.uuid4().hex[:4]}", "username": "no-such-user"},
        headers=auth["headers"],
    )
    assert res.status_code == 422, res.text


def test_assignment_requires_target(client, auth):
    order = _new_order(client, auth, "assign-empty")
    res = client.post(f"/api/work_orders/{order['id']}/assign", json={}, headers=auth["headers"])
    assert res.status_code == 422


def test_dispatch_to_bound_account_creates_notification(client, auth):
    """派给绑定了账号的人 → 他能收到站内通知，工单进他的「我的待办」。"""
    operator = f"op_{uuid.uuid4().hex[:6]}"
    reg = client.post(
        "/api/auth/register", json={"username": operator, "password": "operator12345"}
    )
    assert reg.status_code == 200, reg.text
    operator_headers = {"Authorization": f"Bearer {reg.json()['token']}"}

    staff = client.post(
        "/api/staff",
        json={"name": f"李四{uuid.uuid4().hex[:4]}", "team": "乙班", "username": operator},
        headers=auth["headers"],
    ).json()["staff"]

    order = _new_order(client, auth, "dispatch")
    res = client.post(
        f"/api/work_orders/{order['id']}/assign",
        json={"staff_id": staff["id"], "note": "4 小时内反馈复测结果"},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    wo = res.json()["work_order"]
    assert wo["assignee"] == staff["name"]
    assert wo["assignee_username"] == operator
    assert wo["status"] == "assigned"

    notes = client.get("/api/notifications", headers=operator_headers).json()
    assert notes["unread"] == 1
    item = notes["items"][0]
    assert item["kind"] == "dispatch"
    assert item["work_order_id"] == order["id"]
    assert "4 小时内反馈" in (item["body"] or "")

    # 我的待办 = 工单里 assignee_username 等于自己
    mine = client.get("/api/work-orders?limit=200", headers=operator_headers).json()["work_orders"]
    assert any(row["id"] == order["id"] and row["assignee_username"] == operator for row in mine)

    # 已读
    assert client.post(f"/api/notifications/{item['id']}/read", headers=operator_headers).status_code == 200
    assert client.get("/api/notifications", headers=operator_headers).json()["unread"] == 0


def test_dispatch_to_unbound_staff_notifies_dispatcher(client, auth):
    """没绑定账号的人收不到站内通知，所以通知回到派单人。"""
    staff = client.post(
        "/api/staff", json={"name": f"王五{uuid.uuid4().hex[:4]}"}, headers=auth["headers"]
    ).json()["staff"]
    order = _new_order(client, auth, "dispatch-unbound")

    res = client.post(
        f"/api/work_orders/{order['id']}/assign",
        json={"staff_id": staff["id"]},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    assert res.json()["work_order"]["assignee_username"] in (None, "")

    notes = client.get("/api/notifications?limit=100", headers=auth["headers"]).json()
    assert any(
        n["kind"] == "assignee_unbound" and n["work_order_id"] == order["id"] for n in notes["items"]
    )


def test_dispatch_manual_name_still_works(client, auth):
    order = _new_order(client, auth, "dispatch-manual")
    res = client.post(
        f"/api/work_orders/{order['id']}/assign",
        json={"assignee": "外部协作-赵六"},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    assert res.json()["work_order"]["assignee"] == "外部协作-赵六"


def test_notifications_require_login(client):
    assert client.get("/api/notifications").status_code == 401
    assert client.get("/api/staff").status_code == 401


# --------------------------------------------------------------------------- #
# 整改闭环：派单 → 接单 → 已处理（措施+照片）→ 复测 → 关闭
# --------------------------------------------------------------------------- #
# 1x1 透明 PNG，用来验证照片落盘与回读
TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _register_operator(client) -> tuple[str, dict]:
    """注册一个工程师账号（派单对象 / 权限边界的测试对象）。"""
    operator = f"op_{uuid.uuid4().hex[:6]}"
    reg = client.post(
        "/api/auth/register",
        json={"username": operator, "password": "operator12345", "role": "engineer"},
    )
    assert reg.status_code == 200, reg.text
    return operator, {"Authorization": f"Bearer {reg.json()['token']}"}


def _staff(client, auth, *, username=None, team=None, on_duty=False) -> dict:
    name = f"负责人{uuid.uuid4().hex[:5]}"
    res = client.post(
        "/api/staff",
        json={"name": name, "username": username, "team": team},
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    person = res.json()["staff"]
    if on_duty:
        patched = client.patch(
            f"/api/staff/{person['id']}", json={"on_duty": True}, headers=auth["headers"]
        )
        assert patched.status_code == 200, patched.text
        person = patched.json()["staff"]
        assert person["on_duty"] == 1
    return person


def test_accept_handle_close_flow(client, auth):
    operator, operator_headers = _register_operator(client)
    person = _staff(client, auth, username=operator)
    order = _new_order(client, auth, "flow")

    assert (
        client.post(
            f"/api/work_orders/{order['id']}/assign",
            json={"staff_id": person["id"]},
            headers=auth["headers"],
        ).status_code
        == 200
    )

    # 别的工程师不能替他接单（审批人例外，可代为操作）
    _other_name, other_headers = _register_operator(client)
    assert (
        client.post(f"/api/work_orders/{order['id']}/accept", headers=other_headers).status_code
        == 403
    )

    accepted = client.post(f"/api/work_orders/{order['id']}/accept", headers=operator_headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["work_order"]["status"] == "accepted"
    assert accepted.json()["work_order"]["accepted_by"] == operator

    # 措施太短
    assert (
        client.post(
            f"/api/work_orders/{order['id']}/handle",
            json={"measure": "好"},
            headers=operator_headers,
        ).status_code
        == 422
    )

    handled = client.post(
        f"/api/work_orders/{order['id']}/handle",
        json={"measure": "已停机更换轴承并复测", "photo_base64": f"data:image/png;base64,{TINY_PNG}"},
        headers=operator_headers,
    )
    assert handled.status_code == 200, handled.text
    wo = handled.json()["work_order"]
    assert wo["status"] == "handled"
    assert wo["handled_by"] == operator
    assert wo["measure"].startswith("已停机更换轴承")
    assert wo["photo_path"]

    detail = client.get(f"/api/work_orders/{order['id']}", headers=operator_headers).json()
    assert detail["work_order"]["photo_data_url"].startswith("data:image/png;base64,")

    # 关闭仍然要过复测：这条事件没有遥测 → 409
    assert (
        client.post(
            f"/api/work_orders/{order['id']}/close",
            json={"resolution": "已复测合格"},
            headers=operator_headers,
        ).status_code
        == 409
    )
    closed = client.post(
        f"/api/work_orders/{order['id']}/close",
        json={"resolution": "已复测合格", "exception_reason": "遥测通道断线，已现场复测"},
        headers=operator_headers,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["work_order"]["status"] == "closed"


def test_handle_rejects_invalid_photo(client, auth):
    operator, operator_headers = _register_operator(client)
    person = _staff(client, auth, username=operator)
    order = _new_order(client, auth, "photo")
    client.post(
        f"/api/work_orders/{order['id']}/assign",
        json={"staff_id": person["id"]},
        headers=auth["headers"],
    )
    res = client.post(
        f"/api/work_orders/{order['id']}/handle",
        json={"measure": "已处理", "photo_base64": "data:image/gif;base64,R0lGODlhAQABAAAAACw="},
        headers=operator_headers,
    )
    assert res.status_code == 422, res.text


def test_accept_requires_assigned_state(client, auth):
    order = _new_order(client, auth, "accept-state")
    res = client.post(f"/api/work_orders/{order['id']}/accept", headers=auth["headers"])
    assert res.status_code == 409, res.text


def test_auto_assign_picks_on_duty_with_least_load(client, auth):
    team = f"班组{uuid.uuid4().hex[:4]}"
    first = _staff(client, auth, team=team, on_duty=True)
    second = _staff(client, auth, team=team, on_duty=True)

    order1 = _new_order(client, auth, "auto1")
    res1 = client.post(
        f"/api/work_orders/{order1['id']}/assign",
        json={"auto": True, "team": team},
        headers=auth["headers"],
    )
    assert res1.status_code == 200, res1.text
    assert res1.json()["work_order"]["assignee"] == first["name"]

    # 第一张在办之后，第二张应该落到更闲的那个人身上
    order2 = _new_order(client, auth, "auto2")
    res2 = client.post(
        f"/api/work_orders/{order2['id']}/assign",
        json={"auto": True, "team": team},
        headers=auth["headers"],
    )
    assert res2.status_code == 200, res2.text
    assert res2.json()["work_order"]["assignee"] == second["name"]


# --------------------------------------------------------------------------- #
# 排班表（自动值班）+ SLA 超时升级
# --------------------------------------------------------------------------- #
def _local_hhmm(offset_minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=offset_minutes)).strftime("%H:%M")


def test_shift_crud_syncs_on_duty(client, auth):
    person = _staff(client, auth, username=auth["username"])
    res = client.post(
        "/api/shifts",
        json={
            "name": "测试班",
            "staff_id": person["id"],
            "start_time": _local_hhmm(-60),
            "end_time": _local_hhmm(60),
            "weekdays": "1,2,3,4,5,6,7",
        },
        headers=auth["headers"],
    )
    assert res.status_code == 200, res.text
    shift = res.json()["shift"]
    assert shift["staff_name"] == person["name"]

    listing = client.get("/api/shifts", headers=auth["headers"]).json()
    mine = [item for item in listing["shifts"] if item["id"] == shift["id"]][0]
    assert mine["current"] is True
    assert auth["username"] in listing["on_duty"]

    staff_rows = client.get("/api/staff?active_only=false", headers=auth["headers"]).json()["staff"]
    row = [item for item in staff_rows if item["id"] == person["id"]][0]
    assert row["on_duty"] == 1

    assert client.delete(f"/api/shifts/{shift['id']}", headers=auth["headers"]).status_code == 200
    after = client.get("/api/shifts", headers=auth["headers"]).json()["shifts"]
    assert all(item["id"] != shift["id"] for item in after)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x", "staff_id": 1, "start_time": "25:00", "end_time": "08:00"},
        {"name": "x", "staff_id": 1, "start_time": "08:00", "end_time": "08:00"},
        {"name": "x", "staff_id": 1, "start_time": "08:00", "end_time": "16:00", "weekdays": "8"},
    ],
)
def test_shift_validation(client, auth, payload):
    res = client.post("/api/shifts", json=payload, headers=auth["headers"])
    assert res.status_code == 422, res.text


def test_sla_marks_overdue_work_order_once(client, auth, monkeypatch):
    import backend_server as bs

    operator, operator_headers = _register_operator(client)
    person = _staff(client, auth, username=operator)
    order = _new_order(client, auth, "sla-over")
    client.post(
        f"/api/work_orders/{order['id']}/assign",
        json={"staff_id": person["id"]},
        headers=auth["headers"],
    )

    # 时限改成 0 分钟：立刻超时
    monkeypatch.setattr(bs, "sla_minutes", lambda kind, severity: 0)
    first = bs.check_sla_once()
    assert first["work_order_overdue"] >= 1

    notes = client.get("/api/notifications?limit=100", headers=operator_headers).json()["items"]
    mine = [n for n in notes if n["kind"] == "sla_overdue" and n["work_order_id"] == order["id"]]
    assert len(mine) == 1
    assert "SLA" in mine[0]["title"]

    # 再跑一轮：不重复通知
    second = bs.check_sla_once()
    assert second["work_order_overdue"] == 0
    notes2 = client.get("/api/notifications?limit=100", headers=operator_headers).json()["items"]
    assert (
        len([n for n in notes2 if n["kind"] == "sla_overdue" and n["work_order_id"] == order["id"]])
        == 1
    )


def test_sla_escalates_stale_pending_approval(client, auth, monkeypatch):
    import backend_server as bs

    person = _staff(client, auth, username=auth["username"])
    client.patch(
        f"/api/staff/{person['id']}", json={"on_duty": True}, headers=auth["headers"]
    )

    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("sla-approval")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    # 把等待时间拨回 1 小时，模拟长期没人批
    bs._mark(event["event_id"], awaiting_since=time.time() - 3600)
    monkeypatch.setattr(bs, "sla_minutes", lambda kind, severity: 1)
    result = bs.check_sla_once()
    assert result["approval_overdue"] >= 1

    record = client.get(f"/events/{event['event_id']}", headers=auth["headers"]).json()
    assert record.get("escalated") is True

    notes = client.get("/api/notifications?limit=100", headers=auth["headers"]).json()["items"]
    assert any(
        n["kind"] == "sla_approval" and n["event_id"] == event["event_id"] for n in notes
    )


def test_work_order_payload_carries_sla(client, auth):
    order = _new_order(client, auth, "sla-field")
    res = client.get(f"/api/work_orders/{order['id']}", headers=auth["headers"])
    sla = res.json()["work_order"]["sla"]
    assert sla["kind"] == "handle"
    assert sla["minutes"] == 30  # high 级默认 30 分钟
    assert sla["deadline"]
    assert sla["overdue"] is False


def test_backfill_marks_backlog_once(client, auth):
    """历史积压只合并成一条通知，不逐条刷屏。"""
    import sqlite3

    import backend_server as bs

    order = _new_order(client, auth, "backlog")
    with sqlite3.connect(bs.DB_PATH) as conn:
        conn.execute(
            "UPDATE approvals SET decided_at = ? WHERE event_id = ?",
            ("2026-01-01T00:00:00Z", order["event_id"]),
        )

    first = bs.backfill_overdue_backlog()
    assert first >= 1
    row = bs.db.get_work_order_by_id(bs.DB_PATH, order["id"])
    assert row["overdue_notified_at"]

    second = bs.backfill_overdue_backlog()
    assert second == 0


# --------------------------------------------------------------------------- #
# 角色边界：审批人 vs 工程师
# --------------------------------------------------------------------------- #
APPROVER_ONLY_PATHS = [
    "/api/events",
    "/events",
    "/work_orders",
    "/api/telemetry",
    "/api/alerts",
    "/api/staff",
    "/api/shifts",
    "/api/stats/rejections",
]


@pytest.mark.parametrize("path", APPROVER_ONLY_PATHS)
def test_engineer_cannot_read_approver_data(client, path):
    _name, headers = _register_operator(client)
    res = client.get(path, headers=headers)
    assert res.status_code == 403, f"{path} -> {res.status_code}"


def test_engineer_cannot_approve(client, auth):
    WAIT_HEADERS[0] = auth["headers"]
    event = make_event("eng-approve")
    assert client.post("/events", json=event).status_code == 202
    wait_awaiting(client, event["event_id"])

    _name, headers = _register_operator(client)
    res = client.post(
        f"/api/approvals/{event['event_id']}", json={"decision": "approve"}, headers=headers
    )
    assert res.status_code == 403, res.text


def test_engineer_only_sees_own_work_orders(client, auth):
    operator, op_headers = _register_operator(client)
    person = _staff(client, auth, username=operator)

    mine = _new_order(client, auth, "eng-mine")
    other = _new_order(client, auth, "eng-other")
    assert (
        client.post(
            f"/api/work_orders/{mine['id']}/assign",
            json={"staff_id": person["id"]},
            headers=auth["headers"],
        ).status_code
        == 200
    )

    rows = client.get("/api/work-orders?limit=500", headers=op_headers).json()["work_orders"]
    ids = {row["id"] for row in rows}
    assert mine["id"] in ids
    assert other["id"] not in ids

    # 单条详情也按归属拦截
    assert client.get(f"/api/work_orders/{other['id']}", headers=op_headers).status_code == 403
    assert client.get(f"/api/work_orders/{mine['id']}", headers=op_headers).status_code == 200


def test_engineer_cannot_assign_or_close_others(client, auth):
    operator, op_headers = _register_operator(client)
    person = _staff(client, auth, username=operator)
    order = _new_order(client, auth, "eng-assign")
    client.post(
        f"/api/work_orders/{order['id']}/assign",
        json={"staff_id": person["id"]},
        headers=auth["headers"],
    )

    # 工程师不能派单
    assert (
        client.post(
            f"/api/work_orders/{order['id']}/assign",
            json={"staff_id": person["id"]},
            headers=op_headers,
        ).status_code
        == 403
    )

    # 别人的单子不能关
    someone_else = _new_order(client, auth, "eng-close-other")
    assert (
        client.post(
            f"/api/work_orders/{someone_else['id']}/close",
            json={"resolution": "已处理"},
            headers=op_headers,
        ).status_code
        == 403
    )


def test_register_rejects_unknown_role(client):
    res = client.post(
        "/api/auth/register",
        json={"username": f"bad_{uuid.uuid4().hex[:6]}", "password": "pass12345", "role": "admin"},
    )
    assert res.status_code == 422, res.text


def test_register_defaults_to_engineer(client):
    name = f"def_{uuid.uuid4().hex[:6]}"
    res = client.post("/api/auth/register", json={"username": name, "password": "pass12345"})
    assert res.status_code == 200, res.text
    assert res.json()["user"]["role"] == "engineer"


def test_approver_sees_everything(client, auth):
    order = _new_order(client, auth, "approver-all")
    rows = client.get("/api/work-orders?limit=500", headers=auth["headers"]).json()["work_orders"]
    assert any(row["id"] == order["id"] for row in rows)
    assert client.get("/api/events", headers=auth["headers"]).status_code == 200
    assert client.get("/api/telemetry", headers=auth["headers"]).status_code == 200


# --------------------------------------------------------------------------- #
# 审批人邀请码
# --------------------------------------------------------------------------- #
def test_approver_register_requires_invite(client):
    res = client.post(
        "/api/auth/register",
        json={"username": f"noinv_{uuid.uuid4().hex[:6]}", "password": "pass12345", "role": "approver"},
    )
    assert res.status_code == 403, res.text
    assert "邀请码" in res.json()["detail"]


def test_approver_register_rejects_wrong_invite(client):
    res = client.post(
        "/api/auth/register",
        json={
            "username": f"badinv_{uuid.uuid4().hex[:6]}",
            "password": "pass12345",
            "role": "approver",
            "invite_code": "WRONG-CODE",
        },
    )
    assert res.status_code == 403, res.text


def test_engineer_register_needs_no_invite(client):
    res = client.post(
        "/api/auth/register",
        json={"username": f"engonly_{uuid.uuid4().hex[:6]}", "password": "pass12345", "role": "engineer"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["user"]["role"] == "engineer"


def test_invite_visible_to_approver_only(client, auth):
    mine = client.get("/api/auth/invite", headers=auth["headers"])
    assert mine.status_code == 200
    assert mine.json()["invite_code"]

    _name, eng_headers = _register_operator(client)
    assert client.get("/api/auth/invite", headers=eng_headers).status_code == 403


def test_invite_rotate_invalidates_old_code(client, auth, monkeypatch, tmp_path):
    import backend_server as bs

    monkeypatch.setattr(bs, "APPROVER_INVITE_FILE", tmp_path / "invite.txt")
    monkeypatch.setattr(bs, "APPROVER_INVITE_SOURCE", "file")
    monkeypatch.setattr(bs, "APPROVER_INVITE", "OLD-CODE")

    rotated = client.post("/api/auth/invite/rotate", headers=auth["headers"])
    assert rotated.status_code == 200, rotated.text
    new_code = rotated.json()["invite_code"]
    assert new_code and new_code != "OLD-CODE"

    old = client.post(
        "/api/auth/register",
        json={
            "username": f"oldinv_{uuid.uuid4().hex[:6]}",
            "password": "pass12345",
            "role": "approver",
            "invite_code": "OLD-CODE",
        },
    )
    assert old.status_code == 403, old.text

    fresh = client.post(
        "/api/auth/register",
        json={
            "username": f"newinv_{uuid.uuid4().hex[:6]}",
            "password": "pass12345",
            "role": "approver",
            "invite_code": new_code,
        },
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["user"]["role"] == "approver"


def test_invite_rotate_blocked_when_env_controlled(client, auth):
    res = client.post("/api/auth/invite/rotate", headers=auth["headers"])
    assert res.status_code == 409, res.text
    assert "环境变量" in res.json()["detail"]
