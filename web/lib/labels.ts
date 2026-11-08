/** 规则编号 → 简短中文名（未知规则回退到原编号） */
export const RULE_LABELS: Record<string, string> = {
  "R-CH4-RISE": "甲烷浓度上升",
  "R-CH4-HIGH": "甲烷超限",
  "R-TEMP-HIGH": "温度超限",
  "R-VIB-HIGH": "振动超限",
  "R-VIB-TREND": "振动趋势异常",
  "R-CO-HIGH": "一氧化碳超限",
  "R-DEV-OFFLINE": "设备离线",
  "R-APPROVAL-OVERDUE": "审批超时",
};

export const RULE_DETAILS: Record<string, string> = {
  "R-CH4-RISE": "瓦斯涌出趋势预警",
  "R-CH4-HIGH": "甲烷浓度越过 1.0% 限值",
  "R-TEMP-HIGH": "设备温度越过 85℃ 限值",
  "R-VIB-HIGH": "振动烈度越过 4.5 mm/s",
  "R-VIB-TREND": "振动持续上升趋势",
  "R-CO-HIGH": "一氧化碳越过 24 ppm",
  "R-DEV-OFFLINE": "心跳中断，数据不可信",
  "R-APPROVAL-OVERDUE": "待审批超过阈值",
};

export function ruleLabel(ruleId?: string | null): string {
  if (!ruleId) return "未知事件";
  return RULE_LABELS[ruleId] ?? ruleId;
}

export function ruleDetail(ruleId?: string | null): string {
  if (!ruleId) return "";
  return RULE_DETAILS[ruleId] ?? "";
}
