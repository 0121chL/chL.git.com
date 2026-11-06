"use client";

import type { WorkOrder } from "@/lib/types";

const RISK_LABEL: Record<string, string> = { high: "高", medium: "中", low: "低" };

function summary(order: WorkOrder): string {
  const analysis = (order.content?.analysis ?? {}) as Record<string, string>;
  return analysis.regulation_basis ?? "—";
}

function riskOf(order: WorkOrder): string {
  const analysis = (order.content?.analysis ?? {}) as Record<string, string>;
  return RISK_LABEL[analysis.risk_level ?? ""] ?? "—";
}

function localTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function WorkOrderTable({ orders }: { orders: WorkOrder[] }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
        <h2 className="text-sm font-semibold">工单列表</h2>
        <span className="text-[11px] text-slate-500">来自 SQLite work_orders · {orders.length} 条</span>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2 font-medium">工单号</th>
              <th className="px-4 py-2 font-medium">事件</th>
              <th className="px-4 py-2 font-medium">风险</th>
              <th className="px-4 py-2 font-medium">规程依据</th>
              <th className="px-4 py-2 font-medium">创建时间</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400">
                  尚无工单。批准一条待审批事件后，工单会出现在这里。
                </td>
              </tr>
            ) : (
              orders.map((order) => (
                <tr key={order.id} className="border-t border-slate-100">
                  <td className="px-4 py-2 font-mono font-semibold">WO-{String(order.id).padStart(4, "0")}</td>
                  <td className="px-4 py-2 font-mono text-slate-600">{order.event_id}</td>
                  <td className="px-4 py-2">{riskOf(order)}</td>
                  <td className="max-w-[520px] truncate px-4 py-2 text-slate-600" title={summary(order)}>
                    {summary(order)}
                  </td>
                  <td className="px-4 py-2 font-mono text-slate-500">{localTime(order.created_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
