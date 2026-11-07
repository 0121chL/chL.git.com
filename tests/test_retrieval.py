"""规程检索：三类事件的 top-1 命中（依赖本地 embedding 模型，首次较慢）。"""

from __future__ import annotations

import pytest

import analyzer
import regulation_store

CASES = [
    ({"sensor": "temp", "rule_id": "R-TEMP-HIGH", "event_type": "threshold_exceeded",
      "observed": {"value": 93.5, "unit": "C", "threshold": 85.0}}, "第四百三十条"),
    ({"sensor": "ch4", "rule_id": "R-CH4-RISE", "event_type": "gas_surge",
      "observed": {"value": 1.8, "unit": "%", "threshold": 1.0}}, "第一百三十八条"),
    ({"sensor": "vibration", "rule_id": "R-VIB-HIGH", "event_type": "threshold_exceeded",
      "observed": {"value": 4.8, "unit": "mm/s", "threshold": 4.5}}, "第四百三十一条"),
]


@pytest.mark.parametrize("event,expected", CASES)
def test_top1(event, expected):
    hits = regulation_store.search_regulation(analyzer.build_query(event), top_k=3)
    assert hits[0]["clause_id"] == expected


def test_clause_lookup():
    assert regulation_store.get_clause("第四百三十一条")["title"]
    assert regulation_store.get_clause("第431条")["clause_id"] == "第四百三十一条"
    assert regulation_store.get_clause("第九十九条") is None


def test_every_clause_has_number_and_text():
    for clause in regulation_store.REGULATIONS:
        assert clause["clause_id"].startswith("第")
        assert clause["text"].strip()
