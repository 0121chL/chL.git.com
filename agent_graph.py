"""第二阶段：LangGraph Agent 骨架 + 人工审批暂停点（mock LLM，不接 Ollama）。

图结构
------
    START → analyze_node → human_review_node ─┬─(resume="approve")→ create_work_order_node → END
                                              └─(resume="reject") → reject_node           → END

硬约束落实
----------
1. 写工单只发生在 `create_work_order_node`，它在 `interrupt()` 之后才被执行；
   `human_review_node` 内部没有任何 DB 写工单动作，Agent 无法自主落库。
2. `human_review_node` 在 interrupt() **之前**写了一条审计日志。LangGraph 在
   resume 时会从该节点开头重新执行，所以这条副作用必须幂等 —— 见 upsert_audit()。
3. 未知的 resume 值一律抛错（fail closed），绝不默认放行。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import analyzer as analyzer_mod
import db

APPROVE = "approve"
REJECT = "reject"

APPROVE_TOKENS = {"approve", "approved", "yes", "true", "批准", "同意"}
REJECT_TOKENS = {"reject", "rejected", "no", "false", "驳回", "拒绝"}


class AgentState(TypedDict, total=False):
    """图状态。event 是唯一入口（绝不接触原始传感器流）。"""

    event: dict[str, Any]
    analysis: dict[str, Any]
    approval_status: Literal["pending", "approved", "rejected"]
    approval_decision: str
    work_order: dict[str, Any] | None
    tool_calls: list[str]
    tool_results: dict[str, Any]


# 节点执行计数：用于证明「resume 会重跑 interrupt 之前的代码」，
# 进而证明审计写入必须是 upsert。仅存于进程内存，不参与业务逻辑。
_EXECUTIONS: Counter[str] = Counter()


def node_executions() -> dict[str, int]:
    return dict(_EXECUTIONS)


def reset_executions() -> None:
    _EXECUTIONS.clear()


def thread_config(event_id: str) -> dict[str, Any]:
    """thread_id 使用 event_id：一个事件一条审批线程。"""
    return {"configurable": {"thread_id": event_id}}


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def plan_node(state: AgentState) -> dict[str, Any]:
    """决定是否需要调用工具补充上下文（确定性策略，不依赖模型 function calling）。"""
    _EXECUTIONS["plan_node"] += 1
    event = state["event"]
    calls: list[str] = []
    if event.get("event_type") == "device_offline":
        calls.append("get_device_status")
    if event.get("severity") == "high":
        calls.append("get_recent_events")
    return {"tool_calls": calls}


def route_after_plan(state: AgentState) -> str:
    return "tools_node" if state.get("tool_calls") else "analyze_node"


def make_tools_node(tools_fn: Callable[[dict[str, Any], list[str]], dict[str, Any]] | None):
    def tools_node(state: AgentState) -> dict[str, Any]:
        _EXECUTIONS["tools_node"] += 1
        calls = state.get("tool_calls") or []
        if not tools_fn or not calls:
            return {"tool_results": {}}
        try:
            results = tools_fn(state["event"], calls)
        except Exception as exc:  # noqa: BLE001 - 工具失败不能阻断分析
            results = {"error": f"{type(exc).__name__}: {exc}"}
        return {"tool_results": results}

    return tools_node


def make_analyze_node(db_path: Path, analyze_fn: Callable[[dict[str, Any]], dict[str, Any]]):
    def analyze_node(state: AgentState) -> dict[str, Any]:
        _EXECUTIONS["analyze_node"] += 1
        event = state["event"]
        context = state.get("tool_results") or {}
        event_with_context = {**event, "_context": context} if context else event

        # 工具上下文 → 检索 + LLM（或离线模板）；失败即抛错、不落任何半成品
        analysis = analyze_fn(event_with_context)

        # 幂等副作用：重复分析同一事件不会产生第二条审计
        db.upsert_audit(db_path, event["event_id"], "analysis_generated", analysis)
        return {"analysis": analysis, "approval_status": "pending"}

    return analyze_node


def normalize_decision(value: Any) -> str:
    """把 resume 值归一化为 approved / rejected；非法值直接失败（fail closed）。"""
    if isinstance(value, dict):
        value = value.get("decision", value.get("approval", ""))
    token = str(value).strip().lower()
    if token in APPROVE_TOKENS:
        return "approved"
    if token in REJECT_TOKENS:
        return "rejected"
    raise ValueError(f"非法审批结果: {value!r}；只接受 approve / reject")


def make_human_review_node(db_path: Path):
    def human_review_node(state: AgentState) -> dict[str, Any]:
        _EXECUTIONS["human_review_node"] += 1
        event = state["event"]
        analysis = state["analysis"]

        # ---- interrupt() 之前的副作用：必须幂等（resume 会重跑本节点）----
        db.upsert_audit(
            db_path,
            event["event_id"],
            "approval_requested",
            {"event_id": event["event_id"], "analysis": analysis},
        )

        # 暂停点：前端拿到这段 payload 渲染「双卡片 + 审批」
        decision = interrupt({"analysis": analysis, "event": event})

        status = normalize_decision(decision)
        return {"approval_status": status, "approval_decision": str(decision)}

    return human_review_node


def route_after_review(state: AgentState) -> str:
    status = state.get("approval_status")
    if status == "approved":
        return "create_work_order_node"
    if status == "rejected":
        return "reject_node"
    raise RuntimeError(f"审批状态非法，拒绝继续执行: {status!r}")


def make_create_work_order_node(db_path: Path):
    def create_work_order_node(state: AgentState) -> dict[str, Any]:
        _EXECUTIONS["create_work_order_node"] += 1
        event = state["event"]
        event_id = event["event_id"]
        content = json.dumps(
            {
                "event_id": event_id,
                "rule_id": event.get("rule_id"),
                "sensor": event.get("sensor"),
                "observed": event.get("observed"),
                "analysis": state.get("analysis"),
                "approval": "manual-approved",
                "approval_decision": state.get("approval_decision"),
            },
            ensure_ascii=False,
        )
        # 幂等：同一事件只会有一张工单（event_id UNIQUE）
        row = db.insert_work_order(db_path, event_id, content, status="created")
        db.upsert_audit(db_path, event_id, "work_order_created", {"work_order_id": row["id"]})
        return {"work_order": row, "approval_status": "approved"}

    return create_work_order_node


def make_reject_node(db_path: Path):
    def reject_node(state: AgentState) -> dict[str, Any]:
        _EXECUTIONS["reject_node"] += 1
        event_id = state["event"]["event_id"]
        db.upsert_audit(
            db_path,
            event_id,
            "approval_rejected",
            {"decision": state.get("approval_decision")},
        )
        return {"work_order": None, "approval_status": "rejected"}

    return reject_node


# --------------------------------------------------------------------------- #
# 编译
# --------------------------------------------------------------------------- #
def build_app(
    db_path: str | Path | None = None,
    checkpointer: Any | None = None,
    analyze_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    tools_fn: Callable[[dict[str, Any], list[str]], dict[str, Any]] | None = None,
):
    """默认用「检索 + LLM」的 analyzer；测试可注入 mock 分析函数（接口不变）。

    图结构：START → plan → (tools) → analyze → interrupt → 批准/驳回。
    tools 节点给分析补充设备状态/近期事件等上下文，不改变审批闸门的位置。
    """
    path = db.init_db(db_path)
    analyze = analyze_fn or analyzer_mod.analyze_event

    graph = StateGraph(AgentState)
    graph.add_node("plan_node", plan_node)
    graph.add_node("tools_node", make_tools_node(tools_fn))
    graph.add_node("analyze_node", make_analyze_node(path, analyze))
    graph.add_node("human_review_node", make_human_review_node(path))
    graph.add_node("create_work_order_node", make_create_work_order_node(path))
    graph.add_node("reject_node", make_reject_node(path))

    graph.add_edge(START, "plan_node")
    graph.add_conditional_edges(
        "plan_node",
        route_after_plan,
        {"tools_node": "tools_node", "analyze_node": "analyze_node"},
    )
    graph.add_edge("tools_node", "analyze_node")
    graph.add_edge("analyze_node", "human_review_node")
    graph.add_conditional_edges(
        "human_review_node",
        route_after_review,
        {
            "create_work_order_node": "create_work_order_node",
            "reject_node": "reject_node",
        },
    )
    graph.add_edge("create_work_order_node", END)
    graph.add_edge("reject_node", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


__all__ = [
    "AgentState",
    "APPROVE",
    "REJECT",
    "build_app",
    "thread_config",
    "Command",
    "node_executions",
    "reset_executions",
]
