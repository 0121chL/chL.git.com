"use client";

import type { StatusPayload } from "@/lib/types";

const ANALYZER_LABEL: Record<string, { text: string; tone: "ok" | "warn" }> = {
  ollama: { text: "Ollama · qwen2.5:7b", tone: "ok" },
  "ollama-stub": { text: "协议桩（非模型）", tone: "warn" },
  "offline-template": { text: "离线模板（非模型）", tone: "warn" },
};

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${ok ? "bg-emerald-500" : "bg-rose-500"}`}
      aria-hidden
    />
  );
}

function Pill({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "ok" | "warn" | "bad";
}) {
  const tones = {
    neutral: "border-slate-300 bg-white text-slate-700",
    ok: "border-emerald-200 bg-emerald-50 text-emerald-700",
    warn: "border-amber-200 bg-amber-50 text-amber-800",
    bad: "border-rose-200 bg-rose-50 text-rose-700",
  } as const;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function StatusBar({
  status,
  sseConnected,
  lastError,
}: {
  status: StatusPayload | null;
  sseConnected: boolean;
  lastError: string | null;
}) {
  const mqtt = status?.mqtt;
  const analyzer = ANALYZER_LABEL[status?.analyzer ?? "offline-template"] ?? {
    text: status?.analyzer ?? "—",
    tone: "warn" as const,
  };

  return (
    <header className="sticky top-0 z-10 border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-2 px-5 py-3">
        <div className="mr-2">
          <h1 className="text-base font-semibold tracking-tight">安全监测 Agent 看板</h1>
          <p className="text-[11px] text-slate-500">MQTT → 规则引擎 → LangGraph → 人工审批 → 工单</p>
        </div>

        <div className="flex items-center gap-2">
          <Pill tone={mqtt?.connected ? "ok" : "bad"}>
            <Dot ok={Boolean(mqtt?.connected)} />
            {mqtt?.connected ? "MQTT 已连接" : "MQTT 未连接"}
            {mqtt?.broker ? <span className="text-slate-500">· {mqtt.broker}</span> : null}
          </Pill>
          <Pill tone={sseConnected ? "ok" : "bad"}>
            <Dot ok={sseConnected} />
            {sseConnected ? "实时流已连接" : "实时流断开"}
          </Pill>
          <Pill tone={analyzer.tone}>
            分析引擎：{analyzer.text}
            {status?.model && status.analyzer === "ollama" ? <span className="text-slate-500">· {status.model}</span> : null}
          </Pill>
        </div>

        <div className="ml-auto flex items-center gap-4 text-sm">
          <div className="text-right">
            <div className="text-[11px] text-slate-500">事件</div>
            <div className="font-mono text-base font-semibold">{status?.counts.events ?? 0}</div>
          </div>
          <div className="text-right">
            <div className="text-[11px] text-slate-500">待审批</div>
            <div className="font-mono text-base font-semibold text-amber-600">{status?.counts.pending ?? 0}</div>
          </div>
          <div className="text-right">
            <div className="text-[11px] text-slate-500">工单</div>
            <div className="font-mono text-base font-semibold text-emerald-700">
              {status?.counts.work_orders ?? 0}
            </div>
          </div>
        </div>
      </div>

      {lastError ? (
        <div className="border-t border-rose-200 bg-rose-50 px-5 py-1.5 text-xs text-rose-700">{lastError}</div>
      ) : null}
      {mqtt && !mqtt.connected && mqtt.last_error ? (
        <div className="border-t border-amber-200 bg-amber-50 px-5 py-1.5 text-xs text-amber-800">
          MQTT：{mqtt.last_error}（可改用 HTTP 注入通道演示）
        </div>
      ) : null}
    </header>
  );
}
