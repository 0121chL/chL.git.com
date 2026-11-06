"""提示词构建：系统提示词（安全工程逻辑约束）+ 用户提示词（事件 + 检索条款）。"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """你是安全工程决策辅助 Agent。你的输出必须严格基于给定的事件快照和检索到的规程条款。
根因假设必须考虑多参数联合模式：若 ch4 快速上升且 temp 同步上升，优先怀疑局部瓦斯涌出或通风异常；若 ch4 上升但 co 平稳，排除燃烧前兆。
每一条 recommended_action 必须可追溯到至少一条 regulation_basis 中的条款编号。
你的角色是提议，不是执行；工单必须等待人工批准。

输出要求（违反即视为无效回答）：
1. 只输出一个 JSON 对象，不要 Markdown 代码块、不要解释文字。
2. 键固定为四个：root_cause_hypothesis、regulation_basis、recommended_action、risk_level。
3. risk_level 只能是 low / medium / high 之一。
4. regulation_basis 必须逐条写出条款编号与要点，例如「第一百三十八条：瓦斯涌出异常判定与撤人」；
   只能引用我提供的条款，不得编造条款编号或原文。
5. recommended_action 中每个动作后面用括号标注依据的条款编号，例如「停止作业并撤出人员（第一百三十八条）」。
6. 不得虚构事件快照中不存在的传感器或读数。"""


def build_user_prompt(event: dict[str, Any], clauses: list[dict[str, Any]]) -> str:
    """事件快照 + 检索到的规程原文 → 用户消息。"""
    clause_block = "\n".join(
        f"[{c['clause_id']}] {c['title']}\n{c['text']}" for c in clauses
    )
    snapshot = json.dumps(event, ensure_ascii=False, indent=2)
    return (
        "【事件快照】\n"
        f"{snapshot}\n\n"
        "【检索到的规程条款（只能引用这些）】\n"
        f"{clause_block}\n\n"
        "请给出分析 JSON。"
    )


def build_repair_prompt(error: str, valid_ids: list[str]) -> str:
    """校验失败时的重试指令。"""
    return (
        f"上一次输出未通过校验：{error}\n"
        f"可引用的条款编号只有：{'、'.join(valid_ids)}。\n"
        "请严格按四个键重新输出 JSON。"
    )
