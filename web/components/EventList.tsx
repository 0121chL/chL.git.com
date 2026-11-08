"use client";

import type { EventRecord, RuleEvent } from "@/lib/types";
import { ruleLabel } from "@/lib/labels";

const SEVERITY: Record<string, { label: string; cls: string }> = {
  high: { label: "高危", cls: "border-rose-300 bg-rose-50 text-rose-700" },
  medium: { label: "中危", cls: "border-amber-300 bg-amber-50 text-amber-800" },
  low: { label: "低危", cls: "border-emerald-300 bg-emerald-50 text-emerald-700" },
};

const STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: "待审批", cls: "border-amber-300 bg-amber-50 text-amber-800" },
  approved: { label: "已批准", cls: "border-emerald-300 bg-emerald-50 text-emerald-700" },
  rejected: { label: "已拒绝", cls: "border-slate-300 bg-slate-100 text-slate-600" },
  superseded: { label: "已重提", cls: "border-slate-300 bg-slate-50 text-slate-500" },
  analysis_failed: { label: "分析失败", cls: "border-rose-300 bg-rose-50 text-rose-700" },
  graph_failed: { label: "执行失败", cls: "border-rose-300 bg-rose-50 text-rose-700" },
};

function clock(iso?: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function snapshots(event: RuleEvent): Array<[string, string]> {
  const sensors = event.snapshot?.sensors ?? {};
  return Object.entries(sensors).map(([name, s]) => {
    const delta =
      typeof s.delta_1m === "number" ? ` ${s.delta_1m > 0 ? "↑" : "↓"}${Math.abs(s.delta_1m)}` : "";
    return [name, `${s.value ?? "—"}${s.unit ?? ""}${delta}`];
  });
}

export function EventCard({
  record,
  selected,
  onSelect,
}: {
  record: EventRecord;
  selected: boolean;
  onSelect: () => void;
}) {
  const event = record.event;
  const severity = SEVERITY[event.severity ?? ""] ?? {
    label: event.severity ?? "未知",
    cls: "border-slate-300 bg-slate-50 text-slate-600",
  };
  const status = STATUS[record.status] ?? STATUS.pending;
  const observed = event.observed ?? {};
  const rows = snapshots(event);

  return (
    <button
      type="button"
      onClick={onSelect}
      data-selected={selected}
      className="event-card w-full rounded-lg border border-slate-200 bg-white p-3 text-left transition-colors"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className={`rounded border px-1.5 py-0.5 text-[11px] ${severity.cls}`}>{severity.label}</span>
          {event.origin === "manual" ? (
            <span className="whitespace-nowrap rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800">
              手工注入
            </span>
          ) : null}
          {event.origin === "watchdog" ? (
            <span className="whitespace-nowrap rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
              看门狗
            </span>
          ) : null}
        </div>
        <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[11px] ${status.cls}`}>{status.label}</span>
      </div>

      <div className="mt-2 flex items-baseline justify-between gap-2">
        <span className="truncate text-sm font-semibold">{ruleLabel(event.rule_id)}</span>
        <span className="shrink-0 font-mono text-[11px] text-slate-500">{clock(event.triggered_at)}</span>
      </div>
      <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-500">
        <span className="truncate">{event.rule_id ?? "—"}</span>
        {event.device_id ? (
          <span className="whitespace-nowrap rounded bg-slate-100 px-1 py-0.5 font-mono text-[10px] text-slate-600">
            {event.device_id}
          </span>
        ) : null}
        <span className="truncate">· {record.event_id}</span>
      </div>

      <div className="mt-2 rounded bg-slate-50 px-2 py-1.5 text-xs">
        <span className="text-slate-500">{event.sensor ?? "—"} </span>
        <span className="font-mono font-semibold">
          {observed.value ?? "—"}
          {observed.unit ?? ""}
        </span>
        <span className="text-slate-400"> / 限值 </span>
        <span className="font-mono">
          {observed.threshold ?? "—"}
          {observed.unit ?? ""}
        </span>
      </div>

      {rows.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {rows.map(([name, text]) => (
            <span key={name} className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600 ring-1 ring-slate-200">
              {name} {text}
            </span>
          ))}
        </div>
      ) : null}
    </button>
  );
}

export function EventList({
  events,
  selectedId,
  onSelect,
}: {
  events: EventRecord[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-slate-200 bg-white">
      <header className="flex items-center justify-between border-b border-slate-200 px-3 py-2">
        <h2 className="text-sm font-semibold">规则事件</h2>
        <span className="text-[11px] text-slate-500">{events.length} 条</span>
      </header>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
        {events.length === 0 ? (
          <p className="px-1 py-6 text-center text-xs text-slate-400">
            暂无事件。运行注入脚本后，规则事件会实时出现在这里。
          </p>
        ) : (
          events.map((record) => (
            <EventCard
              key={record.event_id}
              record={record}
              selected={record.event_id === selectedId}
              onSelect={() => onSelect(record.event_id)}
            />
          ))
        )}
      </div>
    </section>
  );
}
