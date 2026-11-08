"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { AnalysisPanel } from "@/components/AnalysisPanel";
import { EventList } from "@/components/EventList";
import { StatusBar } from "@/components/StatusBar";
import { WorkOrderTable, type WorkOrderAction } from "@/components/WorkOrderTable";
import {
  API_BASE,
  assignWorkOrder,
  closeWorkOrder,
  fetchMe,
  logout,
  postApproval,
  reopenWorkOrder,
  resubmitEvent,
  retryAnalysis,
} from "@/lib/api";
import { clearSession, getToken, type SessionUser } from "@/lib/auth";
import type { EventRecord, StatusPayload, StreamMessage, WorkOrder } from "@/lib/types";

export default function DashboardPage() {
  const router = useRouter();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [sseConnected, setSseConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 未登录直接去 /login；token 失效也清掉
  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchMe()
      .then((u) => setUser(u))
      .catch(() => {
        clearSession();
        router.replace("/login");
      })
      .finally(() => setChecking(false));
  }, [router]);

  const signOut = useCallback(async () => {
    await logout();
    clearSession();
    router.replace("/login");
  }, [router]);

  const upsertEvent = useCallback((record: EventRecord) => {
    setEvents((prev) => {
      const index = prev.findIndex((item) => item.event_id === record.event_id);
      if (index === -1) return [record, ...prev];
      const next = [...prev];
      next[index] = { ...prev[index], ...record };
      return next;
    });
  }, []);

  const patchEvent = useCallback((eventId: string, patch: Partial<EventRecord>) => {
    setEvents((prev) =>
      prev.map((item) => (item.event_id === eventId ? { ...item, ...patch } : item)),
    );
  }, []);

  useEffect(() => {
    const source = new EventSource(`${API_BASE}/api/stream`);

    source.onopen = () => setSseConnected(true);
    source.onerror = () => setSseConnected(false);

    const handle = (raw: MessageEvent) => {
      let message: StreamMessage;
      try {
        message = JSON.parse(raw.data) as StreamMessage;
      } catch {
        return;
      }

      switch (message.type) {
        case "hello":
          setStatus(message.status);
          setEvents(message.events);
          setOrders(message.work_orders);
          break;

        case "event":
          upsertEvent({
            event_id: message.event.event_id,
            event: message.event,
            analysis: null,
            citations: [],
            status: "pending",
            received_at: new Date().toISOString(),
          });
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          setError(null);
          break;

        case "analysis":
          upsertEvent({
            event_id: message.event_id,
            event: message.event,
            analysis: message.analysis,
            citations: message.citations,
            status: "pending",
            received_at: new Date().toISOString(),
          });
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          break;

        case "analysis_error":
          patchEvent(message.event_id, {
            status: "analysis_failed",
            error: message.error,
          } as Partial<EventRecord>);
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          setError(`分析失败：${message.error}`);
          break;

        case "work_order":
          setOrders((prev) =>
            prev.some((order) => order.id === message.work_order.id)
              ? prev
              : [message.work_order, ...prev],
          );
          patchEvent(message.event_id, { status: "approved" });
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          break;

        case "rejected":
          patchEvent(message.event_id, { status: "rejected" });
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          break;

        default:
          break;
      }
    };

    const names = ["hello", "event", "analysis", "analysis_error", "work_order", "rejected"];
    for (const name of names) {
      source.addEventListener(name, handle as EventListener);
    }

    return () => {
      for (const name of names) {
        source.removeEventListener(name, handle as EventListener);
      }
      source.close();
    };
  }, [patchEvent, upsertEvent]);

  useEffect(() => {
    if (!selectedId && events.length > 0) {
      const firstPending = events.find((item) => item.status === "pending");
      setSelectedId((firstPending ?? events[0]).event_id);
    }
  }, [events, selectedId]);

  const selected = useMemo(
    () => events.find((item) => item.event_id === selectedId) ?? null,
    [events, selectedId],
  );

  const retry = useCallback(async () => {
    if (!selected) return;
    setBusyId(selected.event_id);
    setError(null);
    try {
      await retryAnalysis(selected.event_id);
      patchEvent(selected.event_id, { status: "pending", error: undefined } as Partial<EventRecord>);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }, [selected, patchEvent]);

  const decide = useCallback(
    async (decision: "approve" | "reject", comment: string) => {
      if (!selected) return;
      setBusyId(selected.event_id);
      setError(null);
      try {
        // 审批人由后端从登录会话取，前端只提交决定与意见
        await postApproval(selected.event_id, decision, { comment });
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusyId(null);
      }
    },
    [selected],
  );

  const [woBusy, setWoBusy] = useState<number | null>(null);

  const woAction = useCallback(
    async (action: WorkOrderAction, order: WorkOrder, value?: string) => {
      setWoBusy(order.id);
      setError(null);
      try {
        if (action === "assign") await assignWorkOrder(order.id, value ?? "");
        else if (action === "close") await closeWorkOrder(order.id, value ?? "");
        else await reopenWorkOrder(order.id);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setWoBusy(null);
      }
    },
    [],
  );

  const resubmit = useCallback(async () => {
    if (!selected) return;
    setBusyId(selected.event_id);
    setError(null);
    try {
      const result = await resubmitEvent(selected.event_id);
      setSelectedId(result.event_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }, [selected]);

  if (checking) {
    return (
      <main className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        正在校验登录状态…
      </main>
    );
  }
  if (!user) return null;

  return (
    <div className="min-h-screen">
      <StatusBar
        status={status}
        sseConnected={sseConnected}
        lastError={error}
        username={user.username}
        onSignOut={signOut}
      />

      <main className="mx-auto max-w-[1400px] px-5 py-4">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="h-[620px] lg:col-span-4">
            <EventList events={events} selectedId={selectedId} onSelect={setSelectedId} />
          </div>
          <div className="h-[620px] lg:col-span-8">
            <AnalysisPanel
              record={selected}
              busy={busyId === selected?.event_id}
              approver={user.username}
              onDecide={decide}
              onRetry={retry}
              onResubmit={resubmit}
            />
          </div>
        </div>

        <div className="mt-4">
          <WorkOrderTable orders={orders} onAction={woAction} busyId={woBusy} />
        </div>

        <p className="mt-3 text-[11px] text-slate-400">
          注入演示事件：{" "}
          <code className="rounded bg-white px-1 py-0.5 ring-1 ring-slate-200">
            python scripts/inject_event.py --scenario gas_surge
          </code>
        </p>
      </main>
    </div>
  );
}
