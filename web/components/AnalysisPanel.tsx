"use client";

import { useEffect, useState } from "react";

import { TelemetryChart } from "@/components/TelemetryChart";
import { ruleLabel } from "@/lib/labels";
import type { EventRecord, RejectReasonCode, RiskLevel } from "@/lib/types";

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
  approver,
  reasons,
  defaultRejectReason,
  onDecide,
  onRetry,
  onResubmit,
}: {
  record: EventRecord | null;
  busy: boolean;
  approver: string;
  /** 驳回原因枚举（来自后端 /api/settings，不在前端硬编码） */
  reasons: Record<string, string>;
  defaultRejectReason: RejectReasonCode;
  onDecide: (decision: "approve" | "reject", comment: string, reasonCode?: RejectReasonCode) => void;
  onRetry: () => void;
  onResubmit: () => void;
}) {
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState<RejectReasonCode>(defaultRejectReason);

  // 切换事件时必须清空意见：否则给 A 写的审批意见会跟着提交到 B。
  const currentEventId = record?.event_id;
  useEffect(() => {
    setComment("");
  }, [currentEventId]);

  // 设置页改了默认驳回原因时同步过来
  useEffect(() => {
    setReason(defaultRejectReason);
  }, [defaultRejectReason]);

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
  // awaiting_approval 才是“可以点批准”的状态；analyzing 表示还在算。
  // 老库里遗留的 pending 语义上等于“等审批”，一并兼容，否则历史事件点不动。
  const analyzing = record.status === "analyzing";
  const canDecide = record.status === "awaiting_approval" || record.status === "pending";
  // AI 补充意见失败时仍允许重试，但审批不受影响
  const retryable =
    record.status === "analysis_failed" ||
    record.status === "graph_failed" ||
    (canDecide && Boolean(record.analysis_error));
  const risk = analysis ? RISK[analysis.risk_level] ?? RISK.medium : null;

  const sourceLabel = record.analysis_pending
    ? "规则处置卡 · AI 分析中…"
    : record.analysis_late
      ? "AI 补充（审批后到达）"
      : record.analysis_source === "rule-card"
        ? "规则处置卡"
        : "AI 分析";
  const sourceCls = record.analysis_pending
    ? "border-sky-300 bg-sky-50 text-sky-700"
    : record.analysis_late
      ? "border-slate-300 bg-slate-50 text-slate-500"
      : record.analysis_source === "rule-card"
        ? "border-slate-300 bg-slate-50 text-slate-600"
        : "border-indigo-200 bg-indigo-50 text-indigo-700";

  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-slate-200 bg-white">
      <header className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">Agent 分析</h2>
          <p className="mt-0.5 truncate text-[11px] text-slate-500">
            {ruleLabel(record.event.rule_id)} · {record.event.device_id ?? "未标注设备"} · {record.event_id}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {analysis ? (
            <span
              className={`rounded border px-2 py-1 text-[11px] font-medium ${sourceCls}`}
              data-testid="analysis-source"
              title={
                record.analysis_pending
                  ? "规则处置卡已可审批；AI 结论到达后会追加显示"
                  : record.analysis_error
                    ? `AI 补充意见失败：${record.analysis_error}`
                    : undefined
              }
            >
              {sourceLabel}
            </span>
          ) : null}
          {risk ? (
            <span className={`shrink-0 rounded border px-2 py-1 text-[11px] font-medium ${risk.cls}`}>
              {risk.label}
            </span>
          ) : null}
        </div>
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

            <Section title="遥测趋势">
              <TelemetryChart
                deviceId={record.event.device_id}
                sensor={record.event.sensor}
                unit={record.event.observed?.unit}
              />
            </Section>
          </>
        )}
      </div>

      <footer className="border-t border-slate-200 bg-slate-50 px-4 py-3">
        <div className="grid gap-2 sm:grid-cols-[150px_1fr_160px]">
          <div className="text-[11px] text-slate-500">
            审批人（当前登录用户）
            <div className="mt-1 w-full rounded border border-slate-200 bg-white px-2 py-1 text-sm text-slate-800">
              {approver}
            </div>
          </div>
          <label className="text-[11px] text-slate-500">
            审批意见（可选，与决定一并落库）
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              disabled={!canDecide || busy}
              maxLength={500}
              rows={2}
              className="mt-1 w-full resize-none rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 disabled:bg-slate-100"
              placeholder="例如：同意降载，30 分钟后复测 ch4"
            />
          </label>
          <label className="text-[11px] text-slate-500">
            驳回原因（拒绝时必用）
            <select
              value={reason}
              onChange={(e) => setReason(e.target.value as RejectReasonCode)}
              disabled={!canDecide || busy}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 disabled:bg-slate-100"
              data-testid="reject-reason"
            >
              {Object.entries(reasons).map(([code, label]) => (
                <option key={code} value={code}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-2 flex items-center gap-2">
          {retryable ? (
            <button
              type="button"
              disabled={busy}
              onClick={onRetry}
              className="rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-100 disabled:cursor-not-allowed disabled:text-slate-400"
            >
              {record.status === "awaiting_approval" ? "重试 AI 分析" : "重试分析"}
            </button>
          ) : null}
          {record.status === "rejected" ? (
            <button
              type="button"
              disabled={busy}
              onClick={onResubmit}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:text-slate-400"
            >
              重新提交复核
            </button>
          ) : null}
          <button
            type="button"
            disabled={!canDecide || busy}
            onClick={() => onDecide("approve", comment)}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            批准
          </button>
          <button
            type="button"
            disabled={!canDecide || busy}
            onClick={() => onDecide("reject", comment, reason)}
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
                  : record.status === "expired"
                    ? "审批中断已失效（后端重启或清库导致），只能忽略"
                    : analyzing
                      ? "正在分析，完成后才可审批"
                      : "审批通过后由后端落库，前端不自行添加记录"}
          </span>
        </div>
      </footer>
    </section>
  );
}
