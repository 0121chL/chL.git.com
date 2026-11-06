"""Mock LLM：不联网、不加载模型，固定返回结构化 JSON 文本。

第二阶段只为验证「图结构 + 人工审批暂停点」，所以这里刻意做成**常量输出**，
保证测试可重复；第三阶段把 `complete()` 换成 ChatOllama 调用即可，节点代码不变。
"""

from __future__ import annotations

import json
from typing import Any

from contracts import ANALYSIS_FIELDS, VALID_RISK_LEVELS  # noqa: F401  (对外保持原常量名)

# 固定结构化输出（mock）。真实实现中由 qwen2.5:7b 生成。
MOCK_COMPLETION = json.dumps(
    {
        "root_cause_hypothesis": "冷却风机转速不足导致散热能力下降，轴承温升持续累积；"
        "（mock 固定假设，P3 由 qwen2.5:7b 结合检索规程生成）",
        "regulation_basis": "《设备安全操作规程》4.2 条：轴承温度超过 85℃ 须立即降载并停机检查；"
        "（mock 固定引用，P3 改为 FAISS 检索片段的真实条款号）",
        "recommended_action": "立即将产线负荷降至 50%，启动备用冷却回路，5 分钟内复测温度；"
        "若仍高于 85℃ 则停机（mock 固定动作）",
        "risk_level": "high",
    },
    ensure_ascii=False,
)


def build_prompt(event: dict[str, Any]) -> str:
    """构造给 LLM 的提示词（第三阶段直接复用；mock 阶段仅用于留痕）。"""
    observed = event.get("observed", {})
    return (
        "你是工业安全分析助手。只根据下面这个结构化事件对象作答，禁止臆造数据。\n"
        f"事件ID: {event.get('event_id')}\n"
        f"规则: {event.get('rule_id')} / 类型: {event.get('event_type')}\n"
        f"传感器: {event.get('sensor')} 实测: {observed.get('value')} {observed.get('unit')} "
        f"阈值: {observed.get('threshold')}\n"
        "输出严格 JSON，键为 root_cause_hypothesis / regulation_basis / "
        "recommended_action / risk_level(low|medium|high)。\n"
    )


def complete(prompt: str) -> str:  # noqa: ARG001 - mock 刻意忽略输入
    """返回固定结构化文本（等价于一次 LLM 补全）。"""
    return MOCK_COMPLETION


def parse_analysis(raw: str) -> dict[str, Any]:
    """把 LLM 文本解析成 analysis 字典；不合格直接抛错，绝不带病入库。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"LLM 输出必须是 JSON 对象，实际为 {type(data).__name__}")

    missing = [k for k in ANALYSIS_FIELDS if not str(data.get(k, "")).strip()]
    if missing:
        raise ValueError(f"analysis 缺少字段或为空: {missing}")

    risk = str(data["risk_level"]).strip().lower()
    if risk not in VALID_RISK_LEVELS:
        raise ValueError(f"risk_level 非法: {risk!r}，允许 {VALID_RISK_LEVELS}")
    data["risk_level"] = risk
    return {k: data[k] for k in ANALYSIS_FIELDS}
