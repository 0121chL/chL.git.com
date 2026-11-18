export type Severity = "high" | "medium" | "low";
export type RiskLevel = "high" | "medium" | "low";

/** 账号身份：审批人看全部事件；工程师只看派给自己的工单 */
export type UserRole = "approver" | "engineer";
export type EventStatus =
  | "analyzing"
  | "awaiting_approval"
  | "pending"
  | "approved"
  | "rejected"
  | "superseded"
  | "expired"
  | "analysis_failed"
  | "graph_failed";

export interface SensorSnapshot {
  value?: number;
  unit?: string;
  delta_1m?: number;
}

/** 持续告警的聚合信息（规则引擎在超限期间按 refresh_s 刷新） */
export interface EventAggregate {
  count?: number;
  sustained_s?: number;
  peak?: number;
  current?: number;
  unit?: string;
  threshold?: number;
  cleared?: boolean;
  first_seen?: string;
}

export interface RuleEvent {
  event_id: string;
  device_id?: string;
  origin?: "rule_engine" | "manual" | "watchdog" | string;
  rule_id?: string;
  event_type?: string;
  severity?: Severity;
  sensor?: string;
  triggered_at?: string;
  observed?: { value?: number; unit?: string; threshold?: number };
  /** 这条规则到底是被什么触发的：阈值超限，还是窗口内涨幅 */
  trigger?: {
    kind: "threshold" | "trend";
    value?: number;
    threshold?: number;
    delta?: number;
    delta_threshold?: number;
    window_s?: number;
    unit?: string;
  };
  window?: { mean?: number; max?: number; slope_per_min?: number };
  snapshot?: { sensors?: Record<string, SensorSnapshot> };
  aggregate?: EventAggregate;
}

export interface Analysis {
  root_cause_hypothesis: string;
  regulation_basis: string;
  recommended_action: string;
  risk_level: RiskLevel;
}

export interface Citation {
  clause_id: string;
  title: string;
  text: string;
}

export interface EventRecord {
  event_id: string;
  event: RuleEvent;
  analysis: Analysis | null;
  citations: Citation[];
  status: EventStatus;
  received_at: string;
  error?: string;
  /** 当前分析来自哪里：rule-card = 规则处置卡，其余是模型后端 */
  analysis_source?: string;
  /** true = 规则卡已出、AI 还在算（不影响审批） */
  analysis_pending?: boolean;
  /** true = AI 结论在审批之后才到 */
  analysis_late?: boolean;
  /** AI 补充意见失败的原因（不影响审批） */
  analysis_error?: string;
}

/** 关闭工单前的「处置后验证」结论（后端 verify_recovery 的返回） */
export interface WorkOrderVerification {
  ok: boolean;
  mode?: "telemetry" | "device_online" | "unknown" | "no_target";
  message?: string;
  reason?: string;
  device_id?: string;
  sensor?: string;
  window_minutes?: number;
  threshold?: number | null;
  unit?: string;
  samples?: number;
  max?: number | null;
  min?: number | null;
  last?: number | null;
  online?: boolean;
}

/** 工单的处置时限（SLA）：从批准时间起算，按事件严重度分级 */
export interface WorkOrderSla {
  kind: string;
  severity: "high" | "medium" | "low";
  minutes: number;
  deadline?: string;
  remaining_s?: number;
  overdue?: boolean;
}

export interface WorkOrder {
  id: number;
  event_id: string;
  content: Record<string, unknown>;
  status: string;
  created_at: string;
  approver?: string | null;
  approval_comment?: string | null;
  approval_reason?: string | null;
  approval_reason_label?: string | null;
  decided_at?: string | null;
  assignee?: string | null;
  assignee_username?: string | null;
  assigned_at?: string | null;
  accepted_at?: string | null;
  accepted_by?: string | null;
  handled_at?: string | null;
  handled_by?: string | null;
  measure?: string | null;
  photo_path?: string | null;
  verified_at?: string | null;
  closed_at?: string | null;
  resolution?: string | null;
  verification?: WorkOrderVerification | null;
  exception_reason?: string | null;
  sla?: WorkOrderSla;
}

export type RejectReasonCode =
  | "false_alarm"
  | "duplicate"
  | "handled"
  | "no_action"
  | "sensor_fault"
  | "other";

export interface UserSettings {
  /** 关闭工单时回看多少分钟的遥测（1–120） */
  retest_window_minutes: number;
  /** 高危事件提示音 */
  alert_sound: boolean;
  /** 首屏事件条数 */
  event_page_size: 20 | 50 | 100;
  /** 批准前二次确认 */
  confirm_before_approve: boolean;
  /** 驳回时默认选中的原因 */
  default_reject_reason: RejectReasonCode;
}

export interface SettingsResponse {
  settings: UserSettings;
  defaults: UserSettings;
  schema: Record<
    string,
    { kind: "int" | "bool" | "str"; min?: number; max?: number; choices?: (number | string)[] }
  >;
  reject_reasons: Record<string, string>;
  user: { id?: number; username?: string; role?: string };
}

/** 负责人表条目：派单时从这里选 */
export interface Staff {
  id: number;
  name: string;
  team?: string | null;
  phone?: string | null;
  username?: string | null;
  active: number;
  /** 1 = 当班（派单可"按当班自动选人"） */
  on_duty: number;
  created_at: string;
}

/** 排班表条目：谁在什么时段值班 */
export interface Shift {
  id: number;
  name: string;
  team?: string | null;
  staff_id: number;
  staff_name?: string | null;
  staff_username?: string | null;
  start_time: string;
  end_time: string;
  weekdays: string;
  active: number;
  current?: boolean;
  created_at: string;
}

/** 站内通知 */
export interface AppNotification {
  id: number;
  recipient: string;
  kind: "dispatch" | "assignee_unbound" | "system" | string;
  title: string;
  body?: string | null;
  event_id?: string | null;
  work_order_id?: number | null;
  created_at: string;
  read_at?: string | null;
}

export interface TelemetryPoint {
  ts: string;
  device_id: string;
  sensor: string;
  value: number | null;
  unit?: string | null;
}

export interface StatusPayload {
  mqtt: {
    connected: boolean;
    available: boolean;
    broker?: string;
    topic?: string;
    received?: number;
    last_error?: string | null;
  };
  analyzer: "ollama" | "ollama-stub" | "offline-template" | string;
  model: string;
  counts: { events: number; pending: number; work_orders: number };
  subscribers?: number;
  checkpointer?: string;
  devices?: DeviceState[];
  offline_timeout_s?: number;
  /** SLA 分级（分钟）：{ approval: {high,medium,low}, handle: {...} } */
  sla?: Record<string, Record<string, number>>;
  /** 当前当班、且绑定了账号的人 */
  on_duty?: string[];
}

export interface DeviceState {
  device_id: string;
  online: boolean;
  last_seen?: string | null;
  silent_seconds?: number;
  offline_events?: number;
  offline_reason?: string | null;
  /** 心跳里带回来的现场信息（ESP32-S3 会上报 ip / rssi / fw） */
  info?: {
    ip?: string;
    rssi?: number;
    fw?: string;
    uptime_s?: number;
    source?: string;
  } | null;
}

export type StreamMessage =
  | { type: "hello"; status: StatusPayload; events: EventRecord[]; work_orders: WorkOrder[] }
  | { type: "event"; event: RuleEvent; counts: StatusPayload["counts"] }
  | {
      type: "analysis";
      event_id: string;
      event: RuleEvent;
      analysis: Analysis;
      citations: Citation[];
      analysis_source?: string;
      analysis_pending?: boolean;
      analysis_late?: boolean;
      analysis_seconds?: number;
      counts: StatusPayload["counts"];
    }
  | {
      type: "analysis_error";
      event_id: string;
      error: string;
      /** true = 只是 AI 补充意见失败，审批不受影响 */
      approval_unaffected?: boolean;
      counts: StatusPayload["counts"];
    }
  | {
      type: "event_update";
      event_id: string;
      event: RuleEvent;
      aggregate?: EventAggregate;
      counts: StatusPayload["counts"];
    }
  | { type: "work_order"; event_id: string; work_order: WorkOrder; counts: StatusPayload["counts"] }
  | {
      type: "rejected";
      event_id: string;
      reason_code?: string;
      reason_label?: string;
      counts: StatusPayload["counts"];
    };
