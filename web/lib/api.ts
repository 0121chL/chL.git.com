export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      decision,
      approver: payload.approver?.trim() || undefined,
      comment: payload.comment?.trim() || undefined,
    }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* 保持状态码文案 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as { ok: boolean; decision: string; duplicate?: boolean };
}
