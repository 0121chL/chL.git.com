"use client";

import { useState } from "react";

import type { EventRecord, RiskLevel } from "@/lib/types";

const RISK: Record<RiskLevel, { label: string; cls: string }> = {
  high: { label: "风险等级 高", cls: "border-rose-300 bg-rose-50 text-rose-700" },
  medium: { label: "风险等级 中", cls: "border-amber-300 bg-amber-50 text-amber-800" },
  low: { label: "风险等级 低", cls: "border-emerald-300 bg-emerald-50 text-emerald-700" },
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-slate-100 px-4 py-3 first:border-t-0">
      <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      {children}
    </div>
  );
}

export function AnalysisPanel({
  record,
  busy,
  onDecide,
}: {
  record: EventRecord | null;
  busy: boolean;
  onDecide: (decision: "approve" | "reject", approver: string, comment: string) => void;
}) {
  const [approver, setApprover] = useState("值班调度员");
  const [comment, setComment] = useState("");

  if (!record) {
    return (
      <section className="flex h-full items-center justify-center rounded-lg border border-dashed border-slate-300 bg-white p-6">
        <p className="max-w-xs text-center text-sm text-slate-400">
          左侧选择一条规则事件，Agent 的分析结论与审批操作会显示在这里。
        </p>
      </section>
    );
  }

  const analysis = record.analysis;
  const pending = record.status === "pending";
  const risk = analysis ? RISK[analysis.risk_level] ?? RISK.medium : null;

  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-slate-200 bg-white">
      <header className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">Agent 分析</h2>
          <p className="mt-0.5 truncate font-mono text-[11px] text-slate-500">
            {record.event.rule_id ?? "—"} · {record.event_id}
          </p>
        </div>
        {risk ? (
          <span className={`shrink-0 rounded border px-2 py-1 text-[11px] font-medium ${risk.cls}`}>
            {risk.label}
          </span>
        ) : null}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {!analysis ? (
          <div className="p-4 text-sm text-slate-500">
            {record.error ? (
              <p className="text-rose-600">分析失败：{record.error}</p>
            ) : (
              <p>正在等待 Agent 输出分析…</p>
            )}
          </div>
        ) : (
          <>
            <Section title="根因假设">
              <p className="text-sm leading-relaxed text-slate-800">{analysis.root_cause_hypothesis}</p>
            </Section>

            <Section title="规程依据">
              <ul className="space-y-2">
                {record.citations.length === 0 ? (
                  <li className="text-sm text-slate-500">{analysis.regulation_basis}</li>
                ) : (
                  record.citations.map((c) => (
                    <li key={c.clause_id} className="rounded border border-slate-200 bg-slate-50 p-2">
                      <div className="flex items-baseline gap-2">
                        <span className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] font-semibold text-slate-700 ring-1 ring-slate-200">
                          {c.clause_id}
                        </span>
                        <span className="text-xs font-medium text-slate-700">{c.title}</span>
                      </div>
                      <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-slate-600">{c.text}</p>
                    </li>
                  ))
                )}
              </ul>
            </Section>

            <Section title="建议动作">
              <p className="text-sm leading-relaxed text-slate-800">{analysis.recommended_action}</p>
            </Section>
          </>
        )}
      </div>

      <footer className="border-t border-slate-200 bg-slate-50 px-4 py-3">
        <div className="grid gap-2 sm:grid-cols-[170px_1fr]">
          <label className="text-[11px] text-slate-500">
            审批人
            <input
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
              disabled={!pending || busy}
              maxLength={60}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 disabled:bg-slate-100"
              placeholder="姓名 / 工号"
            />
          </label>
          <label className="text-[11px] text-slate-500">
            审批意见（可选，与决定一并落库）
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              disabled={!pending || busy}
              maxLength={500}
              rows={2}
              className="mt-1 w-full resize-none rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 disabled:bg-slate-100"
              placeholder="例如：同意降载，30 分钟后复测 ch4"
            />
          </label>
        </div>

        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            disabled={!pending || busy}
            onClick={() => onDecide("approve", approver, comment)}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            批准
          </button>
          <button
            type="button"
            disabled={!pending || busy}
            onClick={() => onDecide("reject", approver, comment)}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:text-slate-400"
          >
            拒绝
          </button>
          <span className="ml-1 text-[11px] text-slate-500">
            {busy
              ? "提交中…"
              : record.status === "approved"
                ? "已批准，工单与审批记录均由后端写入"
                : record.status === "rejected"
                  ? "已拒绝，未生成工单"
                  : "审批通过后由后端落库，前端不自行添加记录"}
          </span>
        </div>
      </footer>
    </section>
  );
}
