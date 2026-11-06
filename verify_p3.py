#!/usr/bin/env python3
"""第三阶段验收：Ollama(qwen2.5:7b) + 本地规程检索 → analysis，并校验条款可追溯。

用法：
    python verify_p3.py                 # 固定事件，跑到 interrupt，打印 analysis
    python verify_p3.py --approve       # 额外走完人工批准，落工单
    python verify_p3.py --event my.json # 换成自己的事件对象（单行 JSON）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import agent_graph as ag
import analyzer
import db
import llm_client
import regulation_store
from contracts import ANALYSIS_FIELDS
from langgraph.types import Command

HERE = Path(__file__).resolve().parent

# 固定测试事件：ch4 快速上升 + temp 同步上升 → 应触发「局部瓦斯涌出/通风异常」推理
TEST_EVENT: dict[str, Any] = {
    "event_id": "evt-p3-gas-0001",
    "schema_version": 1,
    "line": "line1",
    "sensor": "ch4",
    "rule_id": "R-CH4-RISE",
    "event_type": "gas_surge",
    "severity": "high",
    "triggered_at": "2026-11-05T22:15:00+08:00",
    "observed": {"value": 1.8, "unit": "%", "threshold": 1.0},
    "window": {
        "start": "2026-11-05T22:14:00+08:00",
        "end": "2026-11-05T22:15:00+08:00",
        "samples": 60,
        "mean": 0.9,
        "max": 1.8,
        "slope_per_min": 0.65,
    },
    "snapshot": {
        "sensors": {
            "ch4": {"value": 1.8, "unit": "%", "delta_1m": 0.62},
            "temp": {"value": 58.3, "unit": "C", "delta_1m": 7.5},
            "co": {"value": 8, "unit": "ppm", "delta_1m": 0.2},
            "airflow": {"value": 3.1, "unit": "m3/s", "delta_1m": -0.9},
        }
    },
    "scenario_id": "gas_surge_demo",
    "tick": 240,
}

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   -> {detail}" if detail else ""))
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="第三阶段验收：Ollama + 规程检索")
    ap.add_argument("--event", default=None, help="事件 JSON 文件路径")
    ap.add_argument("--db", default=None)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--approve", action="store_true", help="跑完后自动批准，验证工单落库")
    args = ap.parse_args(argv)

    if args.event:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    else:
        event = TEST_EVENT

    db_path = db.resolve_db_path(args.db)
    db.init_db(db_path)
    # 默认清掉该事件的历史工单与审计，保证重复运行结果一致
    db.reset_event(db_path, event["event_id"])

    print("=== 第三阶段验收：Ollama + 本地规程检索 ===")
    print(f"事件      : {event['event_id']}  rule={event.get('rule_id')}  "
          f"observed={event['observed']['value']}{event['observed']['unit']}")
    print(f"DB        : {db_path}")

    # ------------------------------------------------------------------
    print("\n[1/5] 检查 Ollama")
    try:
        model = llm_client.ensure_ready()
        check(True, "Ollama 在线且模型存在", model)
    except llm_client.LLMUnavailable as exc:
        check(False, "Ollama 可用", str(exc).replace("\n", " "))
        print("\n第三阶段无法继续：请先安装/启动 Ollama 并拉取模型。")
        return 2

    using_stub = llm_client.is_stub()
    if using_stub:
        print("  " + "!" * 62)
        print("  !! 当前连接的是协议桩 tools/stub_ollama.py，不是 qwen2.5:7b。")
        print("  !! 下面的 analysis 仅用于验证接线，不能当作模型分析结论。")
        print("  " + "!" * 62)

    # ------------------------------------------------------------------
    print("\n[2/5] 构建规程索引并检索")
    t0 = time.perf_counter()
    hits = regulation_store.search_regulation(analyzer.build_query(event), top_k=args.top_k)
    print(f"       embedding 模型: {regulation_store.DEFAULT_EMBEDDING_MODEL}；"
          f"规程 {len(regulation_store.REGULATIONS)} 条；检索耗时 {time.perf_counter() - t0:.2f}s")
    for h in hits:
        print(f"       - {h['clause_id']} [{h['score']:.3f}] {h['title']}")
    check(len(hits) == args.top_k, f"检索返回 top-{args.top_k}", f"实际 {len(hits)} 条")
    retrieved_ids = [h["clause_id"] for h in hits]

    # ------------------------------------------------------------------
    print("\n[3/5] 运行 Agent（检索 → qwen2.5:7b → 图），应在人工审批处暂停")
    ag.reset_executions()
    app = ag.build_app(db_path)
    cfg = ag.thread_config(event["event_id"])

    t0 = time.perf_counter()
    result = app.invoke({"event": event}, cfg)
    elapsed = time.perf_counter() - t0

    interrupts = result.get("__interrupt__")
    if not check(bool(interrupts), "图停在 human_review_node（未自动落库）",
                 f"next={app.get_state(cfg).next}"):
        return 1
    analysis = interrupts[0].value["analysis"]

    print(f"\n----- analysis JSON（{elapsed:.1f}s，含检索）-----")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    print("-" * 46)
    print(f"元信息: {json.dumps(analyzer.last_meta(), ensure_ascii=False)}")

    # ------------------------------------------------------------------
    print("\n[4/5] 校验 analysis 结构与条款可追溯性")
    check(set(analysis) == set(ANALYSIS_FIELDS), "四个字段齐全且无多余字段", str(sorted(analysis)))
    basis = analysis["regulation_basis"]
    check(bool(basis.strip()), "regulation_basis 非空", basis[:60] + "…")
    check(analysis["risk_level"] in ("low", "medium", "high"), "risk_level 合法", analysis["risk_level"])

    cited = analyzer.extract_clause_ids(basis)
    check(bool(cited), "regulation_basis 中含条款编号", "、".join(cited))
    action_cited = analyzer.extract_clause_ids(analysis["recommended_action"])
    check(bool(set(cited) & set(action_cited)), "recommended_action 标注了 basis 中的条款编号",
          "、".join(action_cited))

    print("\n     逐条回查 regulation_store 原文：")
    all_found = True
    for cid in cited:
        clause = regulation_store.get_clause(cid)
        if clause:
            print(f"       ✓ {cid}  《{clause['title']}》")
            print(f"         原文：{clause['text'][:80]}…")
        else:
            all_found = False
            print(f"       ✗ {cid}  —— regulation_store 中不存在该条款")
    check(all_found and bool(cited), f"{len(cited)} 条引用全部能在规程库中查到原文")
    check(set(cited) <= set(retrieved_ids), "引用未超出本次检索结果（无凭空引用）",
          f"cited={cited} retrieved={retrieved_ids}")

    # ------------------------------------------------------------------
    print("\n[5/5] 人工审批链路仍完好")
    check(db.count_work_orders(db_path, event["event_id"]) == 0, "批准前该事件无工单")
    if args.approve:
        resumed = app.invoke(Command(resume="approve"), cfg)
        wo = resumed.get("work_order")
        check(isinstance(wo, dict), "resume approve 后生成工单", f"work_order_id={wo.get('id') if wo else None}")
        row = db.get_work_order(db_path, event["event_id"])
        check(row is not None and json.loads(row["content"])["analysis"]["regulation_basis"] == basis,
              "工单 content 中保留了带条款编号的分析")
    else:
        print("       （加 --approve 可继续走完审批并落库）")

    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 66)
    if failed:
        print(f"PHASE3 FAILED: {len(failed)}/{len(RESULTS)} 条断言未通过")
        for _, name, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print(f"PHASE3 OK: {len(RESULTS)}/{len(RESULTS)} 条断言通过；"
          f"model={model}；cited={'、'.join(cited)}"
          + ("   [STUB：接线验证，非模型结果]" if using_stub else ""))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
