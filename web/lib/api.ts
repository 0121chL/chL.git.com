import { authHeaders, getToken, type SessionUser } from "./auth";
import type { TelemetryPoint } from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

const SERVICE_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const base = authHeaders(extra);
  if (!getToken() && SERVICE_TOKEN) base.Authorization = `Bearer ${SERVICE_TOKEN}`;
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

/* ----------------------------- 认证 ----------------------------- */

export interface AuthResult {
  token: string;
  expires_at: string;
  user: SessionUser;
}

async function authCall(path: string, username: string, password: string): Promise<AuthResult> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as AuthResult;
}

export const login = (username: string, password: string) =>
  authCall("/api/auth/login", username, password);

export const register = (username: string, password: string) =>
  authCall("/api/auth/register", username, password);

export async function fetchMe(): Promise<SessionUser> {
  const res = await fetch(`${API_BASE}/api/auth/me`, { headers: headers(), cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  const body = (await res.json()) as { user: SessionUser };
  return body.user;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { method: "POST", headers: headers() });
  } catch {
    /* 忽略网络错误，本地清除即可 */
  }
}

/* ----------------------------- 业务 ----------------------------- */

export interface ApprovalPayload {
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

/* --------------------------- 工单流转 / 复议 --------------------------- */

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as T;
}

export const assignWorkOrder = (orderId: number, assignee: string) =>
  postJson<{ ok: boolean }>(`/api/work_orders/${orderId}/assign`, { assignee });

export const closeWorkOrder = (orderId: number, resolution: string) =>
  postJson<{ ok: boolean }>(`/api/work_orders/${orderId}/close`, { resolution });

export const reopenWorkOrder = (orderId: number) =>
  postJson<{ ok: boolean }>(`/api/work_orders/${orderId}/reopen`);

export const resubmitEvent = (eventId: string) =>
  postJson<{ ok: boolean; event_id: string; retry_of: string }>(
    `/api/events/${encodeURIComponent(eventId)}/resubmit`,
  );
