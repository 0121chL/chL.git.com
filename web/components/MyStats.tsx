"use client";

import { useMemo } from "react";

import type { WorkOrder } from "@/lib/types";

/**
 * 工程师视角的数据分析：只用「派给自己的工单」计算，不碰事件/遥测接口。
 * 纯 CSS + SVG 画图，不引第三方图表库。
 */

const STATUS_META: Array<{ key: string; label: string; bar: string; dot: string }> = [
  { key: "assigned", label: "待接单", bar: "bg-sky-500", dot: "bg-sky-500" },
  { key: "accepted", label: "处理中", bar: "bg-indigo-500", dot: "bg-indigo-500" },
  { key: "handled", label: "待复测", bar: "bg-violet-500", dot: "bg-violet-500" },
  { key: "closed", label: "已关闭", bar: "bg-emerald-500", dot: "bg-emerald-500" },
];

function minutesBetween(from?: string | null, to?: string | null): number | null {
  if (!from || !to) return null;
  const start = new Date(from).getTime();
  const end = new Date(to).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return Math.round((end - start) / 60000);
}

function durationText(minutes: number | null): string {
  if (minutes === null) return "—";
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
  const days = Math.floor(hours / 24);
  return `${days} 天 ${hours % 24} 小时`;
}

function lastSevenDays(): Array<{ key: string; label: string }> {
  const out: Array<{ key: string; label: string }> = [];
  const now = new Date();
  for (let offset = 6; offset >= 0; offset -= 1) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset);
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(
      day.getDate(),
    ).padStart(2, "0")}`;
    out.push({ key, label: `${day.getMonth() + 1}/${day.getDate()}` });
  }
  return out;
}

export function MyStats({ orders }: { orders: WorkOrder[] }) {
  const stats = useMemo(() => {
    const counts: Record<string, number> = { created: 0, assigned: 0, accepted: 0, handled: 0, closed: 0 };
    for (const order of orders) {
      counts[order.status] = (counts[order.status] ?? 0) + 1;
    }
    const total = orders.length;
    const pending = (counts.assigned ?? 0) + (counts.accepted ?? 0) + (counts.handled ?? 0);
    const closed = orders.filter((order) => order.status === "closed");
    const overdue = orders.filter((order) => order.sla?.overdue).length;

    const onTime = closed.filter((order) => {
      const deadline = order.sla?.deadline;
      if (!deadline || !order.closed_at) return false;
      return new Date(order.closed_at).getTime() <= new Date(deadline).getTime();
    }).length;
    const onTimeRate = closed.length > 0 ? Math.round((onTime / closed.length) * 100) : null;

    const durations = closed
      .map((order) => minutesBetween(order.decided_at ?? order.created_at, order.closed_at))
      .filter((value): value is number => value !== null);
    const avgMinutes = durations.length
      ? Math.round(durations.reduce((sum, value) => sum + value, 0) / durations.length)
      : null;

    const days = lastSevenDays().map((day) => ({
      ...day,
      count: closed.filter((order) => (order.closed_at ?? "").slice(0, 10) === day.key).length,
    }));
    const maxDay = Math.max(1, ...days.map((day) => day.count));

    return { counts, total, pending, closed: closed.length, overdue, onTimeRate, avgMinutes, days, maxDay };
  }, [orders]);

  const maxStatus = Math.max(1, ...STATUS_META.map((meta) => stats.counts[meta.key] ?? 0));

  return (
    <section className="space-y-3" data-testid="my-stats">
      {/* KPI */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          { label: "分派给我", value: String(stats.total), hint: `待处理 ${stats.pending}` },
          { label: "已关闭", value: String(stats.closed), hint: `平均 ${durationText(stats.avgMinutes)}` },
          {
            label: "SLA 超时",
            value: String(stats.overdue),
            hint: stats.overdue > 0 ? "需要优先处理" : "暂无超时",
            danger: stats.overdue > 0,
          },
          {
            label: "按时完成率",
            value: stats.onTimeRate === null ? "—" : `${stats.onTimeRate}%`,
            hint: stats.closed > 0 ? `已关闭 ${stats.closed} 张` : "还没有关闭记录",
          },
        ].map((item) => (
          <div
            key={item.label}
            className={`rounded-lg border bg-white px-3 py-2.5 ${
              item.danger ? "border-rose-200" : "border-slate-200"
            }`}
          >
            <div className="text-[11px] text-slate-500">{item.label}</div>
            <div
              className={`mt-0.5 text-xl font-semibold tabular-nums ${
                item.danger ? "text-rose-600" : "text-slate-900"
              }`}
            >
              {item.value}
            </div>
            <div className="mt-0.5 text-[10px] text-slate-400">{item.hint}</div>
          </div>
        ))}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {/* 状态分布 */}
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="mb-2 flex items-baseline justify-between">
            <h3 className="text-xs font-semibold text-slate-700">我的工单状态分布</h3>
            <span className="text-[10px] text-slate-400">共 {stats.total} 张</span>
          </div>
          <div className="space-y-2">
            {STATUS_META.map((meta) => {
              const value = stats.counts[meta.key] ?? 0;
              return (
                <div key={meta.key} className="flex items-center gap-2 text-[11px]">
                  <span className="w-14 shrink-0 text-slate-500">{meta.label}</span>
                  <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                    <div
                      className={`h-full rounded-full ${meta.bar} transition-all`}
                      style={{ width: `${(value / maxStatus) * 100}%` }}
                    />
                  </div>
                  <span className="w-8 shrink-0 text-right tabular-nums text-slate-700">{value}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* 近 7 天关闭趋势 + 及时率 */}
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="mb-2 flex items-baseline justify-between">
            <h3 className="text-xs font-semibold text-slate-700">近 7 天关闭趋势</h3>
            <span className="text-[10px] text-slate-400">按关闭时间</span>
          </div>
          <div className="flex h-24 items-end gap-2">
            {stats.days.map((day) => (
              <div key={day.key} className="flex flex-1 flex-col items-center justify-end gap-1">
                <span
                  className={`text-[10px] tabular-nums ${
                    day.count > 0 ? "font-medium text-slate-600" : "text-slate-300"
                  }`}
                >
                  {day.count}
                </span>
                <div
                  className={`w-full rounded-t ${day.count > 0 ? "bg-emerald-500" : "bg-slate-100"}`}
                  style={{
                    height: `${Math.max(3, Math.round((day.count / stats.maxDay) * 56))}px`,
                  }}
                  title={`${day.key}：${day.count} 张`}
                />
                <span className="text-[9px] text-slate-400">{day.label}</span>
              </div>
            ))}
          </div>

          <div className="mt-3 flex items-center gap-3 border-t border-slate-100 pt-3">
            <div className="relative h-16 w-16 shrink-0">
              <svg viewBox="0 0 36 36" className="h-16 w-16 -rotate-90">
                <circle cx="18" cy="18" r="15.9155" fill="none" stroke="#e2e8f0" strokeWidth="3.6" />
                <circle
                  cx="18"
                  cy="18"
                  r="15.9155"
                  fill="none"
                  stroke="#059669"
                  strokeWidth="3.6"
                  strokeLinecap="round"
                  strokeDasharray={`${stats.onTimeRate ?? 0} ${100 - (stats.onTimeRate ?? 0)}`}
                />
              </svg>
              <span className="absolute inset-0 flex items-center justify-center text-xs font-semibold tabular-nums text-slate-800">
                {stats.onTimeRate === null ? "—" : `${stats.onTimeRate}%`}
              </span>
            </div>
            <div className="text-[11px] leading-relaxed text-slate-500">
              <div className="font-medium text-slate-700">SLA 内完成率</div>
              <div>
                关闭 {stats.closed} 张，其中 {stats.closed - (stats.onTimeRate === null ? 0 : Math.round((stats.onTimeRate / 100) * stats.closed))} 张超过时限。
              </div>
              <div className="text-[10px] text-slate-400">
                时限来自 config/sla.json（按事件严重度分级）
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
