"""analysis 契约：四个字段与风险等级枚举，避免各处重复定义。"""

from __future__ import annotations

ANALYSIS_FIELDS: tuple[str, ...] = (
    "root_cause_hypothesis",
    "regulation_basis",
    "recommended_action",
    "risk_level",
)

VALID_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")
