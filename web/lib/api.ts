export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

import type { TelemetryPoint } from "./types";

/** 设置 API_TOKEN 时，前端用这个头访问写接口 */
const TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json", ...extra };
  if (TOKEN) base.Authorization = `Bearer ${TOKEN}`;
  return base;
}

async function readError(res: Response): Promise<string> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body?.detail === "string") detail = body.detail;
    else if (body?.detail) detail = JSON.stringify(body.detail);
  } catch {
    /* 保持状态码文案 */
  }
  return detail;
}

export interface ApprovalPayload {
  approver?: string;
  comment?: string;
}

export async function postApproval(
  eventId: string,
  decision: "approve" | "reject",
  payload: ApprovalPayload = {},
): Promise<{ ok: boolean; decision: string; duplicate?: boolean }> {
  const res = await fetch(`${API_BASE}/api/approvals/${encodeURIComponent(eventId)}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      decision,
      approver: payload.approver?.trim() || undefined,
      comment: payload.comment?.trim() || undefined,
    }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as { ok: boolean; decision: string; duplicate?: boolean };
}

/** 分析失败后重跑（仅 analysis_failed / graph_failed 可用） */
export async function retryAnalysis(eventId: string): Promise<{ ok: boolean; status: string }> {
  const res = await fetch(`${API_BASE}/events/${encodeURIComponent(eventId)}/reanalyze`, {
    method: "POST",
    headers: headers(),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as { ok: boolean; status: string };
}

/** 最近 N 分钟的原始遥测（升序返回，便于直接画折线） */
export async function fetchTelemetry(
  deviceId: string,
  sensor: string,
  minutes = 30,
): Promise<TelemetryPoint[]> {
  const params = new URLSearchParams({
    device_id: deviceId,
    sensor,
    minutes: String(minutes),
    limit: "600",
  });
  const res = await fetch(`${API_BASE}/api/telemetry?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  const body = (await res.json()) as { points: TelemetryPoint[] };
  return [...body.points].reverse();
}
