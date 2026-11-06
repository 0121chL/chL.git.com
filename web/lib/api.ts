export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export async function postApproval(
  eventId: string,
  decision: "approve" | "reject",
): Promise<{ ok: boolean; decision: string }> {
  const res = await fetch(`${API_BASE}/api/approvals/${encodeURIComponent(eventId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* 保持状态码文案 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as { ok: boolean; decision: string };
}
