export type Severity = "high" | "medium" | "low";
export type RiskLevel = "high" | "medium" | "low";
export type EventStatus = "pending" | "approved" | "rejected" | "analysis_failed" | "graph_failed";

export interface SensorSnapshot {
  value?: number;
  unit?: string;
  delta_1m?: number;
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
  window?: { mean?: number; max?: number; slope_per_min?: number };
  snapshot?: { sensors?: Record<string, SensorSnapshot> };
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
}

export interface WorkOrder {
  id: number;
  event_id: string;
  content: Record<string, unknown>;
  status: string;
  created_at: string;
  approver?: string | null;
  approval_comment?: string | null;
  decided_at?: string | null;
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
}

export interface DeviceState {
  device_id: string;
  online: boolean;
  last_seen?: string | null;
  silent_seconds?: number;
  offline_events?: number;
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
      counts: StatusPayload["counts"];
    }
  | { type: "analysis_error"; event_id: string; error: string; counts: StatusPayload["counts"] }
  | { type: "work_order"; event_id: string; work_order: WorkOrder; counts: StatusPayload["counts"] }
  | { type: "rejected"; event_id: string; counts: StatusPayload["counts"] };
