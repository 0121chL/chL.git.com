"""analysis 解析与接地校验的单元测试（不依赖模型）。"""

import json

import pytest

import analyzer
from contracts import ANALYSIS_FIELDS


def test_parse_valid_json():
    raw = json.dumps(
        {
            "root_cause_hypothesis": "ch4 快速上升",
            "regulation_basis": "第一百三十八条：瓦斯涌出异常",
            "recommended_action": "撤人（第一百三十八条）",
            "risk_level": "high",
        },
        ensure_ascii=False,
    )
    data = analyzer.parse_analysis(raw)
    assert set(data) == set(ANALYSIS_FIELDS)
    assert data["risk_level"] == "high"


def test_parse_list_values_joined():
    """小模型常把字段输出成列表，解析后应拼成中文分号文本。"""
    raw = json.dumps(
        {
            "root_cause_hypothesis": ["ch4 上升", "temp 同步上升"],
            "regulation_basis": ["第一百三十八条：瓦斯涌出异常"],
            "recommended_action": ["撤人（第一百三十八条）", "加强通风（第一百三十八条）"],
            "risk_level": "HIGH",
        },
        ensure_ascii=False,
    )
    data = analyzer.parse_analysis(raw)
    assert data["regulation_basis"] == "第一百三十八条：瓦斯涌出异常"
    assert "；" in data["recommended_action"]
    assert data["risk_level"] == "high"


def test_parse_code_fence():
    raw = '```json\n{"root_cause_hypothesis":"a","regulation_basis":"第一百三十八条：x","recommended_action":"b（第一百三十八条）","risk_level":"low"}\n```'
    assert analyzer.parse_analysis(raw)["risk_level"] == "low"


def test_parse_bad_json_raises():
    with pytest.raises(analyzer.GroundingError):
        analyzer.parse_analysis("not json at all")


def test_parse_missing_field_raises():
    raw = json.dumps({"root_cause_hypothesis": "a", "regulation_basis": "b", "risk_level": "high"})
    with pytest.raises(analyzer.GroundingError):
        analyzer.parse_analysis(raw)


def test_grounding_rejects_unknown_clause():
    analysis = {
        "root_cause_hypothesis": "x",
        "regulation_basis": "第一千条：编造条款",
        "recommended_action": "停机（第一千条）",
        "risk_level": "high",
    }
    with pytest.raises(analyzer.GroundingError):
        analyzer.validate_grounding(analysis, ["第一百三十八条"])


def test_grounding_requires_action_citation():
    analysis = {
        "root_cause_hypothesis": "x",
        "regulation_basis": "第一百三十八条：瓦斯涌出异常",
        "recommended_action": "停机，无条款标注",
        "risk_level": "high",
    }
    with pytest.raises(analyzer.GroundingError):
        analyzer.validate_grounding(analysis, ["第一百三十八条"])


def test_grounding_ok():
    analysis = {
        "root_cause_hypothesis": "x",
        "regulation_basis": "第一百三十八条：瓦斯涌出异常",
        "recommended_action": "撤人（第一百三十八条）",
        "risk_level": "high",
    }
    assert analyzer.validate_grounding(analysis, ["第一百三十八条", "第一百三十七条"]) == [
        "第一百三十八条"
    ]


def test_extract_clause_ids_dedup():
    ids = analyzer.extract_clause_ids("第一百三十八条、第一百三十八条、第四百三十一条")
    assert ids == ["第一百三十八条", "第四百三十一条"]
