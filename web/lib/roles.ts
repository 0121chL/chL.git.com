/** 角色判定：只有审批人能看全部事件、派单、审批。 */

export const APPROVER_ROLES = ["approver", "admin", "operator", "service"];

export function isApprover(user?: { role?: string | null } | null): boolean {
  return Boolean(user?.role && APPROVER_ROLES.includes(user.role));
}

export function roleLabel(role?: string | null): string {
  if (role === "engineer") return "工程师";
  if (role && APPROVER_ROLES.includes(role)) return "审批人";
  return role || "—";
}
