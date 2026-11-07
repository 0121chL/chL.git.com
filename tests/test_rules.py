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
