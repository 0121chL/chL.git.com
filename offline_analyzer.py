"""离线分析模板：没有 Ollama 时的兜底，保证界面链路可演示。

⚠️ 这不是模型推理：文字是模板拼装 + 检索到的条款，输出的四个字段结构与真实
analyzer 完全一致，且同样经过接地校验所需的约束（引用只会来自本次检索结果）。
UI 顶部会明确标注「分析引擎：离线模板」。
"""

from __future__ import annotations

from typing import Any

import analyzer
import regulation_store


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value}{unit}"


def analyze(event: dict[str, Any], top_k: int = 3) -> dict[str, Any]:
    clauses = regulation_store.search_regulation(analyzer.build_query(event), top_k=top_k)
    if not clauses:
        raise analyzer.GroundingError("检索不到规程条款，拒绝在无依据的情况下出分析")

    observed = event.get("observed") or {}
    sensors = (event.get("snapshot") or {}).get("sensors") or {}
    sensor = event.get("sensor", "?")
    unit = observed.get("unit", "")
    value = observed.get("value")
    threshold = observed.get("threshold")

    ch4 = sensors.get("ch4") or {}
    temp = sensors.get("temp") or {}
    co = sensors.get("co") or {}
    airflow = sensors.get("airflow") or {}

    pattern = ""
    if ch4.get("delta_1m") is not None and temp.get("delta_1m") is not None:
        if ch4["delta_1m"] > 0.3 and temp["delta_1m"] > 2:
            pattern = (
                f"甲烷 1 分钟内上升 {ch4['delta_1m']} 个百分点、温度同步上升 {temp['delta_1m']}℃，"
                "符合「瓦斯快速上升 + 温度同步上升」的联合模式，优先怀疑局部瓦斯涌出或通风异常。"
            )
        elif ch4["delta_1m"] > 0.3:
            pattern = "甲烷快速上升但温度未见同步抬升。"
    if co.get("delta_1m") is not None and abs(float(co["delta_1m"])) < 1:
        pattern += f" 一氧化碳平稳（{_fmt(co.get('value'), 'ppm')}，Δ{co['delta_1m']}），可排除燃烧前兆。"
    if airflow.get("delta_1m") is not None and float(airflow["delta_1m"]) < 0:
        pattern += f" 风量下降 {abs(float(airflow['delta_1m']))} m3/s，通风能力同步减弱。"

    rule = event.get("rule_id", "—")
    basis = "；".join(f"{c['clause_id']}：{c['title']}" for c in clauses[:2])
    first = clauses[0]["clause_id"]
    second = clauses[1]["clause_id"] if len(clauses) > 1 else first

    risk = (event.get("severity") or "high").lower()
    if risk not in ("low", "medium", "high"):
        risk = "high"
    try:
        # 实测显著超过限值（>10%）时上调风险，其余沿用事件严重度
        if threshold and value is not None and float(value) > float(threshold) * 1.1:
            risk = "high"
    except (TypeError, ValueError):
        pass

    return {
        "root_cause_hypothesis": (
            f"[离线模板] {rule} 触发：{sensor} 实测 {_fmt(value, unit)}（限值 {_fmt(threshold, unit)}）。"
            f"{pattern or '未提供多参数快照。'} "
            "（离线分析：当前未连接 Ollama，此文本由模板生成，仅用于界面联调。）"
        ),
        "regulation_basis": basis,
        "recommended_action": (
            f"立即停止作业并撤出人员（{first}）；切断非本质安全型电源（{second}）；"
            f"加强通风并复测 {sensor}，复测合格并经人工批准后方可恢复（{first}）。"
        ),
        "risk_level": risk,
    }
