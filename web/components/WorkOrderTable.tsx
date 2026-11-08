"use client";

import { ruleLabel } from "@/lib/labels";
import type { WorkOrder } from "@/lib/types";

const RISK_LABEL: Record<string, string> = { high: "高", medium: "中", low: "低" };

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  created: { text: "待处理", cls: "border-amber-300 bg-amber-50 text-amber-800" },
  assigned: { text: "处理中", cls: "border-sky-300 bg-sky-50 text-sky-800" },
  closed: { text: "已关闭", cls: "border-emerald-300 bg-emerald-50 text-emerald-800" },
};

function ruleOf(order: WorkOrder): string | undefined {
  return order.content?.rule_id as string | undefined;
}

function summary(order: WorkOrder): string {
  const analysis = (order.content?.analysis ?? {}) as Record<string, string>;
  return analysis.regulation_basis ?? "—";
}

function riskOf(order: WorkOrder): string {
  const analysis = (order.content?.analysis ?? {}) as Record<string, string>;
  return RISK_LABEL[analysis.risk_level ?? ""] ?? "—";
}

function localTime(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export type WorkOrderAction = "assign" | "close" | "reopen";

export function WorkOrderTable({
  orders,
  onAction,
  busyId,
}: {
  orders: WorkOrder[];
  onAction?: (action: WorkOrderAction, order: WorkOrder, value?: string) => void;
  busyId?: number | null;
}) {
  const act = (action: WorkOrderAction, order: WorkOrder, value?: string) => {
    if (!onAction) return;
    if (action === "assign") {
      const assignee = window.prompt("指派给谁处理？（姓名/工号）", order.assignee ?? "");
      if (!assignee?.trim()) return;
      onAction(action, order, assignee.trim());
    } else if (action === "close") {
      const resolution = window.prompt("处置结果（至少 2 个字）", "");
      if (!resolution?.trim()) return;
      onAction(action, order, resolution.trim());
    } else {
      onAction(action, order);
    }
  };

  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
        <h2 className="text-sm font-semibold">工单列表</h2>
        <span className="text-[11px] text-slate-500">
          来自 SQLite work_orders ⋈ approvals · {orders.length} 条
        </span>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2 font-medium">工单号</th>
              <th className="px-4 py-2 font-medium">事件</th>
              <th className="px-4 py-2 font-medium">风险</th>
              <th className="px-4 py-2 font-medium">审批人</th>
              <th className="px-4 py-2 font-medium">状态 / 处理人</th>
              <th className="px-4 py-2 font-medium">规程依据</th>
              <th className="px-4 py-2 font-medium">审批时间</th>
              <th className="px-4 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-6 text-center text-slate-400">
                  尚无工单。批准一条待审批事件后，工单会出现在这里。
                </td>
              </tr>
            ) : (
              orders.map((order) => {
                const status = STATUS_LABEL[order.status] ?? {
                  text: order.status,
                  cls: "border-slate-300 bg-slate-50 text-slate-600",
                };
                const busy = busyId === order.id;
                return (
                  <tr key={order.id} className="border-t border-slate-100">
                    <td className="px-4 py-2 font-mono font-semibold">WO-{String(order.id).padStart(4, "0")}</td>
                    <td className="px-4 py-2">
                      <div className="text-slate-700">{ruleLabel(ruleOf(order))}</div>
                      <div className="font-mono text-[10px] text-slate-400">{order.event_id}</div>
                    </td>
                    <td className="px-4 py-2">{riskOf(order)}</td>
                    <td className="px-4 py-2 text-slate-700" title={order.approval_comment ?? "无审批意见"}>
                      {order.approver ?? "—"}
                      {order.approval_comment ? <span className="ml-1 text-slate-400">（有意见）</span> : null}
                    </td>
                    <td className="px-4 py-2">
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] ${status.cls}`}>{status.text}</span>
                      {order.assignee ? (
                        <div className="mt-0.5 text-[10px] text-slate-500">处理人：{order.assignee}</div>
                      ) : null}
                      {order.resolution ? (
                        <div className="mt-0.5 max-w-[180px] truncate text-[10px] text-slate-400" title={order.resolution}>
                          {order.resolution}
                        </div>
                      ) : null}
                    </td>
                    <td className="max-w-[380px] truncate px-4 py-2 text-slate-600" title={summary(order)}>
                      {summary(order)}
                    </td>
                    <td className="px-4 py-2 font-mono text-slate-500">{localTime(order.decided_at)}</td>
                    <td className="px-4 py-2">
                      <div className="flex flex-wrap gap-1">
                        {order.status !== "closed" ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => act("assign", order)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100 disabled:text-slate-300"
                          >
                            指派
                          </button>
                        ) : null}
                        {order.status !== "closed" ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => act("close", order)}
                            className="rounded border border-emerald-300 px-1.5 py-0.5 text-[10px] text-emerald-700 hover:bg-emerald-50 disabled:text-slate-300"
                          >
                            关闭
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => act("reopen", order)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100 disabled:text-slate-300"
                          >
                            重新打开
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
