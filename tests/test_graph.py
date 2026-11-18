"""审批图：延迟分析模式下模型不在图里，但审批闸门的位置不变。

这组测试守的是 P0-4 的两个不变量：
  1. defer 模式下图**绝不**调用模型（否则审批又被拖慢）；
  2. 无论哪种模式，写工单都只发生在 interrupt 之后。
"""

from __future__ import annotations

import agent_graph as ag
import db

CARD = {
    "root_cause_hypothesis": "[规则处置卡] ch4 超限",
    "regulation_basis": "第一百三十八条：瓦斯涌出异常",
    "recommended_action": "停止作业、撤出人员（第一百三十八条）",
    "risk_level": "high",
}


def make_event(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "device_id": "dev-graph",
        "sensor": "ch4",
        "rule_id": "R-CH4-HIGH",
        "event_type": "threshold_exceeded",
        "severity": "high",
        "observed": {"value": 1.5, "unit": "%", "threshold": 1.0},
    }


def test_defer_analysis_keeps_model_out_of_graph(tmp_path):
    calls = {"n": 0}

    def fake_analyze(event: dict) -> dict:
        calls["n"] += 1
        return CARD

    path = tmp_path / "defer.db"
    app = ag.build_app(path, analyze_fn=fake_analyze, defer_analysis=True)
    event = make_event("evt-defer-1")
    config = ag.thread_config(event["event_id"])

    app.invoke({"event": event, "analysis": CARD}, config)
    assert app.get_state(config).next, "应当停在 interrupt 等审批"
    assert calls["n"] == 0, "延迟分析模式下不应该在图里调模型"
    # 停下来时还没有工单
    assert db.get_work_order(path, event["event_id"]) is None

    app.invoke(ag.Command(resume="approve"), config)
    assert calls["n"] == 0
    order = db.get_work_order(path, event["event_id"])
    assert order and order["status"] == "created"


def test_defer_analysis_reject_creates_no_work_order(tmp_path):
    path = tmp_path / "defer-reject.db"
    app = ag.build_app(path, analyze_fn=lambda event: CARD, defer_analysis=True)
    event = make_event("evt-defer-2")
    config = ag.thread_config(event["event_id"])

    app.invoke({"event": event, "analysis": CARD}, config)
    app.invoke(ag.Command(resume="reject"), config)
    assert db.get_work_order(path, event["event_id"]) is None
    assert db.count_audit(path, event["event_id"], "approval_rejected") == 1


def test_defer_analysis_works_without_preliminary_analysis(tmp_path):
    """state 里没有 analysis 也不能卡住：审批闸门不依赖 AI 结论。"""

    def boom(event: dict) -> dict:
        raise AssertionError("不应该调用模型")

    path = tmp_path / "defer-empty.db"
    app = ag.build_app(path, analyze_fn=boom, defer_analysis=True)
    event = make_event("evt-defer-3")
    config = ag.thread_config(event["event_id"])

    app.invoke({"event": event}, config)
    assert app.get_state(config).next
    app.invoke(ag.Command(resume="approve"), config)
    assert db.get_work_order(path, event["event_id"])["status"] == "created"


def test_classic_mode_still_analyzes_in_graph(tmp_path):
    calls = {"n": 0}

    def fake_analyze(event: dict) -> dict:
        calls["n"] += 1
        return CARD

    path = tmp_path / "classic.db"
    app = ag.build_app(path, analyze_fn=fake_analyze)
    event = make_event("evt-classic-1")
    config = ag.thread_config(event["event_id"])

    app.invoke({"event": event}, config)
    assert calls["n"] == 1
    assert app.get_state(config).next
