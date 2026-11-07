#!/usr/bin/env python3
"""验证本地 transformers 模型（无 Ollama 时的替代运行时）。

    python scripts/local_llm_check.py

期望：打印 analysis JSON、命中的条款编号与耗时；断言四个字段齐全、引用可在
regulation_store 中找到原文。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_BACKEND", "local")

from contracts import ANALYSIS_FIELDS  # noqa: E402

import analyzer  # noqa: E402
import llm_client  # noqa: E402
import local_llm  # noqa: E402
import regulation_store  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

EVENT = {
    "event_id": "evt-local-llm-check",
    "schema_version": 1,
    "line": "line1",
    "device_id": "line1-dev-01",
    "sensor": "ch4",
    "rule_id": "R-CH4-RISE",
    "event_type": "gas_surge",
    "severity": "high",
    "triggered_at": "2026-11-07T12:00:00+08:00",
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
    "scenario_id": "local_llm_check",
}


def main() -> int:
    if not local_llm.available():
        print(f"本地模型不完整：{local_llm.MODEL_DIR}")
        return 2

    llm_client.set_backend("local")
    print(f"模型：{local_llm.model_name()}")
    print(f"目录：{local_llm.MODEL_DIR}")

    started = time.perf_counter()
    analysis = analyzer.analyze_event(EVENT)
    elapsed = time.perf_counter() - started

    print(f"\n耗时：{elapsed:.1f}s\n")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))

    assert set(analysis) == set(ANALYSIS_FIELDS), f"字段不符：{sorted(analysis)}"
    cited = analyzer.extract_clause_ids(analysis["regulation_basis"])
    assert cited, "regulation_basis 中没有条款编号"

    print("\n条款回查：")
    for cid in cited:
        clause = regulation_store.get_clause(cid)
        assert clause, f"{cid} 不在规程库"
        print(f"  ✓ {cid} {clause['title']}")

    print(f"\nLOCAL LLM OK：{elapsed:.1f}s，引用 {'、'.join(cited)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
