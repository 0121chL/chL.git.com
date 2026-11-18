import { authHeaders, type SessionUser } from "./auth";
import type {
  AppNotification,
  RejectReasonCode,
  SettingsResponse,
  Shift,
  Staff,
  TelemetryPoint,
  UserRole,
  UserSettings,
  WorkOrder,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

// 注意：这里**不能**用 NEXT_PUBLIC_API_TOKEN。带 NEXT_PUBLIC_ 前缀的变量会被
// Next 在编译期内联进浏览器 bundle，等于把服务令牌公开出去。脚本/验收要免登录
// 请直接用后端 API_TOKEN 走 HTTP，不要经过前端。
function headers(extra: Record<string, string> = {}): Record<string, string> {
  return authHeaders(extra);
}

/** 带上后端返回的结构化 detail（例如关闭工单的验证结论），上层可分支处理。 */
export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
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

/** 解析错误体：既能拿到人话，也保留 detail 供调用方判断（如 verification）。 */
async function toApiError(res: Response): Promise<ApiError> {
  let detail: unknown = null;
  try {
    const body = (await res.json()) as { detail?: unknown };
    detail = body?.detail ?? null;
  } catch {
    /* 非 JSON 错误体 */
  }
  let message = `${res.status} ${res.statusText}`;
  if (typeof detail === "string") message = detail;
  else if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.error === "string") message = record.error;
    else message = JSON.stringify(detail);
  }
  return new ApiError(message, res.status, detail);
}

/* ----------------------------- 认证 ----------------------------- */

export interface AuthResult {
  token: string;
  expires_at: string;
  user: SessionUser;
}

async function authCall(
  path: string,
  username: string,
  password: string,
  role?: UserRole,
  inviteCode?: string,
): Promise<AuthResult> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role, invite_code: inviteCode }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as AuthResult;
}

export const login = (username: string, password: string) =>
  authCall("/api/auth/login", username, password);

export const register = (
  username: string,
  password: string,
  role: UserRole = "engineer",
  inviteCode?: string,
) => authCall("/api/auth/register", username, password, role, inviteCode);

/** 审批人邀请码（仅审批人可读） */
export async function fetchInvite(): Promise<{
  invite_code: string;
  source: string;
  file: string;
}> {
  const res = await fetch(`${API_BASE}/api/auth/invite`, { headers: headers(), cache: "no-store" });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { invite_code: string; source: string; file: string };
}

/** 轮换邀请码：旧码立即失效（环境变量控制时后端会 409） */
export async function rotateInvite(): Promise<{ invite_code: string; source: string }> {
  const res = await fetch(`${API_BASE}/api/auth/invite/rotate`, {
    method: "POST",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { invite_code: string; source: string };
}

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

/** 换一张 SSE/WS 订阅票据：EventSource 不能带请求头，只能走查询参数。 */
export async function fetchStreamTicket(): Promise<{ ticket: string; expires_in: number }> {
  const res = await fetch(`${API_BASE}/api/auth/stream-ticket`, {
    method: "POST",
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as { ticket: string; expires_in: number };
}

/* ----------------------------- 业务 ----------------------------- */

export interface ApprovalPayload {
  comment?: string;
  reasonCode?: RejectReasonCode;
}

export async function postApproval(
  eventId: string,
  decision: "approve" | "reject",
  payload: ApprovalPayload = {},
): Promise<{ ok: boolean; decision: string; duplicate?: boolean; reason_code?: string }> {
  const res = await fetch(`${API_BASE}/api/approvals/${encodeURIComponent(eventId)}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      decision,
      comment: payload.comment?.trim() || undefined,
      reason_code: decision === "reject" ? payload.reasonCode : undefined,
    }),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as {
    ok: boolean;
    decision: string;
    duplicate?: boolean;
    reason_code?: string;
  };
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
  const res = await fetch(`${API_BASE}/api/telemetry?${params.toString()}`, {
    cache: "no-store",
    headers: headers(),
  });
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

export const assignWorkOrder = (
  orderId: number,
  target: { staffId?: number; assignee?: string; note?: string; auto?: boolean; team?: string },
) =>
  postJson<{ ok: boolean }>(`/api/work_orders/${orderId}/assign`, {
    staff_id: target.staffId,
    assignee: target.assignee,
    note: target.note,
    auto: target.auto ?? false,
    team: target.team,
  });

/** 接单：assigned → accepted（只有被指派人本人能操作） */
export const acceptWorkOrder = (orderId: number) =>
  postJson<{ ok: boolean; work_order: WorkOrder }>(`/api/work_orders/${orderId}/accept`);

/** 标记已处理：assigned/accepted → handled（整改措施 + 可选照片 data URL） */
export const handleWorkOrder = (orderId: number, measure: string, photoBase64?: string) =>
  postJson<{ ok: boolean; work_order: WorkOrder }>(`/api/work_orders/${orderId}/handle`, {
    measure,
    photo_base64: photoBase64,
  });

/** 单张工单详情（含整改照片 data URL） */
export async function fetchWorkOrderDetail(
  orderId: number,
): Promise<{ work_order: WorkOrder & { photo_data_url?: string } }> {
  const res = await fetch(`${API_BASE}/api/work_orders/${orderId}`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { work_order: WorkOrder & { photo_data_url?: string } };
}

/**
 * 关闭工单。后端会先做「处置后验证」：峰值仍超阈值 → 409，detail 里带 verification；
 * 调用方拿到 ApiError 后可以展示验证结论并要求填例外理由再试一次。
 */
export async function closeWorkOrder(
  orderId: number,
  resolution: string,
  exceptionReason?: string,
): Promise<{ ok: boolean; work_order: Record<string, unknown> }> {
  const res = await fetch(`${API_BASE}/api/work_orders/${orderId}/close`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      resolution,
      exception_reason: exceptionReason?.trim() || undefined,
    }),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { ok: boolean; work_order: Record<string, unknown> };
}

export const reopenWorkOrder = (orderId: number) =>
  postJson<{ ok: boolean }>(`/api/work_orders/${orderId}/reopen`);

export const resubmitEvent = (eventId: string) =>
  postJson<{ ok: boolean; event_id: string; retry_of: string }>(
    `/api/events/${encodeURIComponent(eventId)}/resubmit`,
  );

/* --------------------------- 设置 / 账号 --------------------------- */

export async function fetchSettings(): Promise<SettingsResponse> {
  const res = await fetch(`${API_BASE}/api/settings`, { headers: headers(), cache: "no-store" });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as SettingsResponse;
}

export async function saveSettings(
  patch: Partial<UserSettings>,
): Promise<{ ok: boolean; settings: UserSettings }> {
  const res = await fetch(`${API_BASE}/api/settings`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ settings: patch }),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { ok: boolean; settings: UserSettings };
}

export async function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<{ ok: boolean; revoked_sessions: number }> {
  const res = await fetch(`${API_BASE}/api/auth/password`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { ok: boolean; revoked_sessions: number };
}

export interface RejectStats {
  days: number;
  total: number;
  by_reason: { reason_code: string; label: string; count: number }[];
  by_rule: { rule_id: string; reason_code: string; count: number }[];
}

export async function fetchRejectStats(days = 7): Promise<RejectStats> {
  const res = await fetch(`${API_BASE}/api/stats/rejections?days=${days}`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as RejectStats;
}

export interface SessionRow {
  created_at: string;
  expires_at: string;
  last_used_at?: string | null;
}

export async function fetchSessions(): Promise<SessionRow[]> {
  const res = await fetch(`${API_BASE}/api/auth/sessions`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw await toApiError(res);
  return ((await res.json()) as { sessions: SessionRow[] }).sessions;
}

export async function revokeAllSessions(): Promise<{ ok: boolean; revoked: number }> {
  const res = await fetch(`${API_BASE}/api/auth/logout-all`, {
    method: "POST",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { ok: boolean; revoked: number };
}

/* --------------------------- 负责人表 / 通知 --------------------------- */

export async function fetchWorkOrders(limit = 200): Promise<WorkOrder[]> {
  const res = await fetch(`${API_BASE}/api/work-orders?limit=${limit}`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw await toApiError(res);
  return ((await res.json()) as { work_orders: WorkOrder[] }).work_orders;
}

export async function fetchStaff(activeOnly = true): Promise<Staff[]> {  const res = await fetch(`${API_BASE}/api/staff?active_only=${activeOnly ? "true" : "false"}`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw await toApiError(res);
  return ((await res.json()) as { staff: Staff[] }).staff;
}

export async function createStaff(input: {
  name: string;
  team?: string;
  phone?: string;
  username?: string;
}): Promise<Staff> {
  const res = await fetch(`${API_BASE}/api/staff`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await toApiError(res);
  return ((await res.json()) as { staff: Staff }).staff;
}

export async function patchStaff(
  staffId: number,
  input: Partial<Pick<Staff, "name" | "team" | "phone" | "username">> & {
    active?: boolean;
    on_duty?: boolean;
  },
): Promise<Staff> {
  const res = await fetch(`${API_BASE}/api/staff/${staffId}`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await toApiError(res);
  return ((await res.json()) as { staff: Staff }).staff;
}

export async function disableStaff(staffId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/api/staff/${staffId}`, {
    method: "DELETE",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
}

export async function fetchNotifications(
  unreadOnly = false,
): Promise<{ unread: number; items: AppNotification[] }> {
  const res = await fetch(`${API_BASE}/api/notifications?unread_only=${unreadOnly ? "true" : "false"}`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { unread: number; items: AppNotification[] };
}

export async function markNotificationRead(id: number): Promise<{ unread: number }> {
  const res = await fetch(`${API_BASE}/api/notifications/${id}/read`, {
    method: "POST",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { unread: number };
}

export async function markAllNotificationsRead(): Promise<{ unread: number }> {
  const res = await fetch(`${API_BASE}/api/notifications/read-all`, {
    method: "POST",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { unread: number };
}

/* ------------------------------- 排班表 ------------------------------- */

export interface ShiftList {
  shifts: Shift[];
  now: string;
  on_duty: string[];
  duty: { scheduled?: number; on_duty?: number[]; configured?: boolean };
}

export async function fetchShifts(): Promise<ShiftList> {
  const res = await fetch(`${API_BASE}/api/shifts`, { headers: headers(), cache: "no-store" });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as ShiftList;
}

export async function createShift(input: {
  name: string;
  staff_id: number;
  start_time: string;
  end_time: string;
  team?: string;
  weekdays?: string;
}): Promise<Shift> {
  const res = await fetch(`${API_BASE}/api/shifts`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await toApiError(res);
  return ((await res.json()) as { shift: Shift }).shift;
}

export async function deleteShift(shiftId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/api/shifts/${shiftId}`, {
    method: "DELETE",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
}

export async function syncShifts(): Promise<{ on_duty: string[] }> {
  const res = await fetch(`${API_BASE}/api/shifts/sync`, {
    method: "POST",
    headers: headers(),
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as { on_duty: string[] };
}
