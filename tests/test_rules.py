"""规则配置：加载、默认值、传感器阈值合法性。"""

from __future__ import annotations

import json

import rule_engine


def test_load_rules_defaults(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"sensors": {}}), encoding="utf-8")
    rules = rule_engine.load_rules(path)
    assert rules["cooldown_s"] == 30
    assert rules["merge_window_s"] == 10
    assert rules["telemetry"]["retention_hours"] == 24


def test_repo_rules_file_valid():
    rules = rule_engine.load_rules(rule_engine.DEFAULT_RULES)
    assert "ch4" in rules["sensors"]
    for name, spec in rules["sensors"].items():
        assert spec["threshold"] > 0, name
        assert spec["rule_id"].startswith("R-")
        assert spec.get("unit")


def test_ch4_has_trend_rule():
    rules = rule_engine.load_rules(rule_engine.DEFAULT_RULES)
    trend = rules["sensors"]["ch4"].get("trend") or {}
    assert trend.get("enabled") is True
    assert trend["rule_id"] == "R-CH4-RISE"


def test_refresh_s_has_default():
    rules = rule_engine.load_rules(rule_engine.DEFAULT_RULES)
    assert rules["refresh_s"] > 0


# --------------------------------------------------------------------------- #
# 持续告警状态机（P0-3）
# --------------------------------------------------------------------------- #
def test_sustained_tracker_start_update_clear():
    tracker = rule_engine.SustainedTracker(refresh_s=10)

    start = tracker.observe("dev:ch4", 1.2, 1.0, now=100.0)
    assert start["action"] == "start"
    assert start["count"] == 1
    assert start["event_id"] is None
    tracker.attach("dev:ch4", "evt-1")

    # 刷新窗口内：累计次数，但不返回动作（避免刷屏）
    assert tracker.observe("dev:ch4", 1.3, 1.0, now=105.0) is None

    update = tracker.observe("dev:ch4", 1.5, 1.0, now=111.0)
    assert update["action"] == "update"
    assert update["count"] == 3
    assert update["peak"] == 1.5
    assert update["event_id"] == "evt-1"
    assert update["sustained_s"] == 11.0

    clear = tracker.observe("dev:ch4", 0.4, 1.0, now=130.0)
    assert clear["action"] == "clear"
    assert clear["count"] == 3
    assert clear["event_id"] == "evt-1"
    assert clear["sustained_s"] == 30.0

    # 恢复之后不应残留活动态
    assert tracker.observe("dev:ch4", 0.3, 1.0, now=131.0) is None


def test_sustained_tracker_drop_resets_state():
    tracker = rule_engine.SustainedTracker(refresh_s=1)
    assert tracker.observe("k", 2.0, 1.0, now=0.0)["action"] == "start"
    tracker.drop("k")
    # 冷却期不建事件时调用 drop，下一次越限必须重新 start
    assert tracker.observe("k", 2.0, 1.0, now=5.0)["action"] == "start"
    # 计数从 1 重新开始；过了刷新窗口才会返回 update
    update = tracker.observe("k", 2.0, 1.0, now=7.0)
    assert update["action"] == "update"
    assert update["count"] == 2


def test_aggregate_payload_shape():
    tracker = rule_engine.SustainedTracker(refresh_s=10)
    transition = tracker.observe("k", 1.5, 1.0, now=0.0)
    payload = rule_engine.aggregate_payload(transition, 1.5, {"unit": "%", "threshold": 1.0})
    assert payload["count"] == 1
    assert payload["unit"] == "%"
    assert payload["threshold"] == 1.0
    assert payload["cleared"] is False
    assert payload["first_seen"]  # 有可读的 ISO 时间

    cleared = rule_engine.aggregate_payload(
        transition, 0.3, {"unit": "%", "threshold": 1.0}, cleared=True
    )
    assert cleared["cleared"] is True
    assert cleared["current"] == 0.3
