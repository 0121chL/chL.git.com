#!/usr/bin/env python3
"""第二阶段验收脚本：LangGraph 审批暂停点 + SQLite 工单落库。

运行：
    python test_approval_flow.py

覆盖 5 组断言：
    A. invoke 后在 human_review_node 暂停，__interrupt__ 内含 analysis，且未写工单
    B. Command(resume="approve") 恢复后写工单，且审计写入幂等（节点重跑但只 1 条）
    C. 同一 event_id 重复写工单不新增（DB 层幂等）
    D. 另一事件 resume="reject" 后不写工单、总工单数不变
    E. 非法审批值 fail-closed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import agent_graph as ag
import db
import mock_llm
from langgraph.types import Command

HERE = Path(__file__).resolve().parent
RESULTS: list[tuple[bool, str, str]] = []

# Windows 控制台默认 GBK，中文会乱码；显式切 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"   -> {detail}"
    print(line)
    return ok


def banner(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 62 - len(title)))


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"events.jsonl 第 {lineno} 行不是合法 JSON: {exc}") from exc
            if "event_id" not in ev:
                raise SystemExit(f"events.jsonl 第 {lineno} 行缺少 event_id")
            events.append(ev)
    if len(events) < 2:
        raise SystemExit(f"events.jsonl 至少需要 2 个事件，实际 {len(events)} 个")
    return events


def mock_analyzer(event: dict[str, Any]) -> dict[str, Any]:
    """本测试只验收图结构与审批语义，故注入固定输出，不依赖 Ollama。"""
    return mock_llm.parse_analysis(mock_llm.complete(""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="第二阶段验收：人工审批暂停点")
    ap.add_argument("--db", default=None, help="SQLite 路径，默认 data/monitor.db")
    ap.add_argument("--events", default=str(HERE / "events.jsonl"))
    ap.add_argument("--keep", action="store_true", help="保留既有记录（默认清空以保证计数可复现）")
    args = ap.parse_args(argv)

    db_path = db.resolve_db_path(args.db)
    events = load_events(Path(args.events))
    ev_approve, ev_reject = events[0], events[1]
    eid_a, eid_b = ev_approve["event_id"], ev_reject["event_id"]

    print("=== 阶段二验收：LangGraph 审批暂停点 ===")
    print(f"DB      : {db_path}")
    print(f"事件 A  : {eid_a}  (rule={ev_approve.get('rule_id')}, expect=approve)")
    print(f"事件 B  : {eid_b}  (rule={ev_reject.get('rule_id')}, expect=reject)")

    if not args.keep:
        db.init_db(db_path)
        db.reset_all(db_path)
        print("[init] 已清空 work_orders / audit_log，保证记录数可复现")

    ag.reset_executions()
    app = ag.build_app(db_path, analyze_fn=mock_analyzer)

    # =====================================================================
    banner("A. invoke -> 应停在 human_review_node")
    cfg_a = ag.thread_config(eid_a)
    result = app.invoke({"event": ev_approve}, cfg_a)

    interrupts = result.get("__interrupt__")
    check(bool(interrupts), "invoke 返回 __interrupt__（确实暂停了）", f"count={len(interrupts or [])}")
    payload = interrupts[0].value if interrupts else {}
    check(isinstance(payload, dict) and "analysis" in payload, "__interrupt__ 载荷含 analysis")
    analysis = payload.get("analysis", {}) if isinstance(payload, dict) else {}
    check(
        set(analysis) == set(mock_llm.ANALYSIS_FIELDS),
        "analysis 字段完整且无多余字段",
        f"keys={sorted(analysis)}",
    )
    check(
        bool(analysis.get("root_cause_hypothesis")),
        "analysis.root_cause_hypothesis 非空",
        str(analysis.get("root_cause_hypothesis", ""))[:40] + "…",
    )
    check(
        payload.get("event", {}).get("event_id") == eid_a,
        "__interrupt__ 载荷携带原始 event",
        str(payload.get("event", {}).get("event_id")),
    )

    snapshot = app.get_state(cfg_a)
    check(snapshot.next == ("human_review_node",), "图停在 human_review_node", f"next={snapshot.next}")
    check(
        snapshot.values.get("approval_status") == "pending",
        "暂停时 approval_status == pending",
        str(snapshot.values.get("approval_status")),
    )
    check(
        db.count_work_orders(db_path, eid_a) == 0,
        "暂停期间未写工单（Agent 无法自主落库）",
        f"count={db.count_work_orders(db_path, eid_a)}",
    )

    # =====================================================================
    banner('B. Command(resume="approve") -> 写工单')
    resumed = app.invoke(Command(resume="approve"), cfg_a)
    work_order = resumed.get("work_order")
    check(isinstance(work_order, dict) and "id" in work_order, "resume 后状态里带 work_order", str(work_order))
    check(resumed.get("approval_status") == "approved", "approval_status == approved", str(resumed.get("approval_status")))

    row = db.get_work_order(db_path, eid_a)
    check(row is not None, "SQLite work_orders 中查到该事件工单")
    if row:
        body = json.loads(row["content"])
        check(body.get("analysis", {}).get("root_cause_hypothesis"), "工单 content 内含分析结论")
        check(row["status"] == "created", "工单 status == created", row["status"])
        print(f"         工单行: id={row['id']} event={row['event_id']} created_at={row['created_at']}")

    check(db.count_work_orders(db_path) == 1, "work_orders 总数 == 1", f"count={db.count_work_orders(db_path)}")

    # 关键：resume 会从 human_review_node 开头重跑，副作用必须幂等
    execs = ag.node_executions()
    check(
        execs.get("human_review_node") == 2,
        "human_review_node 实际执行 2 次（interrupt 前代码被重跑）",
        f"count={execs.get('human_review_node')}",
    )
    check(
        db.count_audit(db_path, eid_a, "approval_requested") == 1,
        "approval_requested 审计仅 1 条（upsert 生效，未重复记账）",
        f"count={db.count_audit(db_path, eid_a, 'approval_requested')}",
    )

    # =====================================================================
    banner("C. 同一 event_id 重复写工单 -> 不新增")
    dup = db.insert_work_order(db_path, eid_a, '{"duplicate": true}', status="duplicate-attempt")
    check(dup["id"] == row["id"] if row else False, "重复写返回既有工单 id", f"id={dup['id']}")
    check(db.count_work_orders(db_path) == 1, "工单总数仍为 1（UNIQUE(event_id) 兜底）", f"count={db.count_work_orders(db_path)}")

    # =====================================================================
    banner('D. 另一事件 Command(resume="reject") -> 不写工单')
    cfg_b = ag.thread_config(eid_b)
    app.invoke({"event": ev_reject}, cfg_b)
    rejected = app.invoke(Command(resume="reject"), cfg_b)
    check(rejected.get("work_order") is None, "reject 后 work_order 为 None", str(rejected.get("work_order")))
    check(rejected.get("approval_status") == "rejected", "approval_status == rejected", str(rejected.get("approval_status")))
    check(db.count_work_orders(db_path, eid_b) == 0, "该事件无工单", f"count={db.count_work_orders(db_path, eid_b)}")
    check(db.count_work_orders(db_path) == 1, "work_orders 总数仍 == 1", f"count={db.count_work_orders(db_path)}")
    check(
        db.count_audit(db_path, eid_b, "approval_rejected") == 1,
        "驳回审计 1 条",
        f"count={db.count_audit(db_path, eid_b, 'approval_rejected')}",
    )

    # =====================================================================
    banner("E. 非法审批值 -> fail closed")
    raised = False
    try:
        ag.normalize_decision("maybe")
    except ValueError:
        raised = True
    check(raised, 'resume="maybe" 被拒绝（绝不默认放行）')
    check(ag.normalize_decision({"decision": "approve"}) == "approved", "支持 dict 形式 resume 载荷（HTTP 层用）")
    check(ag.normalize_decision("批准") == "approved", "中文审批词被归一化为 approved")

    # =====================================================================
    total_orders = db.count_work_orders(db_path)
    total_audit = db.count_audit(db_path)
    print("\n=== 记录数核对 ===")
    print(f"work_orders 总数        = {total_orders}   (预期 1)")
    print(f"audit_log  总数         = {total_audit}   (预期 6)")
    print(f"  analysis_generated    = {db.count_audit(db_path, stage='analysis_generated')}   (预期 2)")
    print(f"  approval_requested    = {db.count_audit(db_path, stage='approval_requested')}   (预期 2)")
    print(f"  work_order_created    = {db.count_audit(db_path, stage='work_order_created')}   (预期 1)")
    print(f"  approval_rejected     = {db.count_audit(db_path, stage='approval_rejected')}   (预期 1)")

    check(total_orders == 1, "最终 work_orders 记录数 == 1", f"actual={total_orders}")
    check(total_audit == 6, "最终 audit_log 记录数 == 6", f"actual={total_audit}")

    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 66)
    if failed:
        print(f"PHASE2 FAILED: {len(failed)}/{len(RESULTS)} 条断言未通过")
        for _, name, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print(f"PHASE2 OK: {len(RESULTS)}/{len(RESULTS)} 条断言通过；"
          f"approved=1 rejected=1 work_orders=1")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
