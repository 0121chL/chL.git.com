"""检索增强分析器：检索规程 → 构造提示词 → 调 Ollama → 解析 → 接地校验。

相对第二阶段的变化：`mock_llm.complete()` 的固定文本换成了 qwen2.5:7b 的真实生成，
但节点的输入输出契约不变（仍是一个含四个字段的 analysis 字典）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import llm_client
import prompts
import regulation_store
from contracts import ANALYSIS_FIELDS, VALID_RISK_LEVELS

CLAUSE_RE = re.compile(r"第[零一二三四五六七八九十百]{1,10}条")
MAX_ATTEMPTS = 2
_LAST_META: dict[str, Any] = {}


def last_meta() -> dict[str, Any]:
    """最近一次 analyze_event 的元信息（命中条款、耗时、重试次数），供日志与验证脚本使用。"""
    return dict(_LAST_META)


class GroundingError(ValueError):
    """模型输出无法追溯到检索到的规程条款 —— 宁可失败，也不放行无依据的工单。"""


# 事件字段是机器语言（ch4 / R-CH4-RISE），规程是中文书面语；
# 检索前做一次领域词扩展，否则 BM25 与中文 embedding 都抓不到语义。
SENSOR_TERMS = {
    "temp": "温度 轴承 温升 超温 停机 检查 机电设备",
    "temperature": "温度 轴承 温升 超温 停机 检查",
    "ch4": "瓦斯 甲烷 浓度 超限 瓦斯涌出 通风 撤人",
    "methane": "瓦斯 甲烷 浓度 超限 瓦斯涌出 通风 撤人",
    "co": "一氧化碳 中毒 火灾 自然发火",
    "vibration": "振动 轴承 设备 故障 检查",
    "airflow": "风量 通风 微风 无风 局部通风机",
    "o2": "氧气 有限空间 检测",
    "h2s": "硫化氢 有限空间 中毒",
}

EVENT_TYPE_TERMS = {
    "threshold_exceeded": "超限 停止作业 撤出人员",
    "trend_warning": "持续上升 轴承 温升 趋势",
    "gas_surge": "瓦斯涌出 异常 撤人 加强通风",
    "sensor_fault": "传感器 故障 校验",
}


def build_query(event: dict[str, Any]) -> str:
    """把事件对象压成检索语句（只含事件字段 + 领域词扩展，不接触原始数据流）。"""
    observed = event.get("observed") or {}
    sensor = str(event.get("sensor", ""))
    event_type = str(event.get("event_type", ""))
    parts = [
        sensor,
        str(event.get("rule_id", "")),
        event_type,
        str(observed.get("unit", "")),
        f"实测{observed.get('value')} 阈值{observed.get('threshold')}",
    ]
    window = event.get("window") or {}
    if window:
        parts.append(f"窗口均值{window.get('mean')} 最大值{window.get('max')}")
    parts.append(SENSOR_TERMS.get(sensor, ""))
    parts.append(EVENT_TYPE_TERMS.get(event_type, ""))
    return " ".join(p for p in parts if p and p != "None")


def extract_clause_ids(text: str) -> list[str]:
    """按出现顺序去重提取条款编号。"""
    seen: list[str] = []
    for match in CLAUSE_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise GroundingError(f"模型输出中没有 JSON 对象：{raw[:200]}")
    return text[start : end + 1]


def parse_analysis(raw: str) -> dict[str, Any]:
    """解析并做结构校验；结构不合格直接抛错。"""
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise GroundingError(f"模型输出不是合法 JSON：{exc}；原文：{raw[:200]}") from exc

    if not isinstance(data, dict):
        raise GroundingError("模型输出必须是 JSON 对象")

    missing = [k for k in ANALYSIS_FIELDS if not str(data.get(k, "")).strip()]
    if missing:
        raise GroundingError(f"缺少字段或为空：{missing}")

    risk = str(data["risk_level"]).strip().lower()
    if risk not in VALID_RISK_LEVELS:
        raise GroundingError(f"risk_level 非法：{risk!r}，允许 {VALID_RISK_LEVELS}")

    return {
        "root_cause_hypothesis": str(data["root_cause_hypothesis"]).strip(),
        "regulation_basis": str(data["regulation_basis"]).strip(),
        "recommended_action": str(data["recommended_action"]).strip(),
        "risk_level": risk,
    }


def validate_grounding(analysis: dict[str, Any], allowed_ids: list[str]) -> list[str]:
    """可追溯性校验：引用必须落在检索结果内，动作必须挂到 basis 的条款上。"""
    basis_ids = extract_clause_ids(analysis["regulation_basis"])
    if not basis_ids:
        raise GroundingError(
            "regulation_basis 未包含任何条款编号；必须从 %s 中引用" % "、".join(allowed_ids)
        )

    unknown = [cid for cid in basis_ids if cid not in allowed_ids]
    if unknown:
        raise GroundingError(
            f"引用了未检索到的条款 {unknown}；只允许 {allowed_ids}"
        )

    action_ids = [cid for cid in extract_clause_ids(analysis["recommended_action"]) if cid in basis_ids]
    if not action_ids:
        raise GroundingError(
            "recommended_action 没有标注任何条款编号；每个动作后需括注依据（如「第一百三十八条」）"
        )
    return basis_ids


def analyze_event(
    event: dict[str, Any],
    *,
    top_k: int = 3,
    model: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    """事件对象 → analysis（四个字段）。失败会重试一次，仍失败则抛 GroundingError。"""
    clauses = regulation_store.search_regulation(build_query(event), top_k=top_k)
    if not clauses:
        raise GroundingError("检索不到任何规程条款，拒绝在无依据的情况下出分析")

    allowed_ids = [c["clause_id"] for c in clauses]
    base_user = prompts.build_user_prompt(event, clauses)
    user = base_user
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        raw = llm_client.chat(prompts.SYSTEM_PROMPT, user, model=model, temperature=0.1)
        try:
            analysis = parse_analysis(raw)
            cited = validate_grounding(analysis, allowed_ids)
            _LAST_META.update(
                {
                    "attempt": attempt,
                    "cited": cited,
                    "retrieved": [
                        {"clause_id": c["clause_id"], "score": c["score"]} for c in clauses
                    ],
                    "model": llm_client.ensure_ready(model),
                    "latency_s": llm_client.last_call_stats().get("seconds"),
                }
            )
            return analysis
        except GroundingError as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            user = base_user + "\n\n" + prompts.build_repair_prompt(str(exc), allowed_ids)

    raise GroundingError(f"连续 {max_attempts} 次输出未通过接地校验：{last_error}")
