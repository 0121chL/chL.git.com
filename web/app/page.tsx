"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  fetchSettings,
  fetchStreamTicket,
  fetchWorkOrders,
  logout,
  postApproval,
  reopenWorkOrder,
  resubmitEvent,
  retryAnalysis,
} from "@/lib/api";
import { clearSession, getToken, type SessionUser } from "@/lib/auth";
import { isApprover } from "@/lib/roles";
import type {
  EventRecord,
  RejectReasonCode,
  StatusPayload,
  StreamMessage,
  UserSettings,
  WorkOrder,
} from "@/lib/types";

const DEFAULT_SETTINGS: UserSettings = {
  retest_window_minutes: 10,
  alert_sound: false,
  event_page_size: 50,
  confirm_before_approve: true,
  default_reject_reason: "false_alarm",
};

// 后端 /api/settings 拉不到时的兜底，保证驳回原因下拉框不会是空的
const DEFAULT_REASONS: Record<string, string> = {
  false_alarm: "误报",
  duplicate: "重复事件",
  handled: "已现场处置",
  no_action: "无需处置",
  sensor_fault: "传感器故障",
  other: "其他",
};

/** 高危事件提示音：用一个很短的振荡器，不需要音频文件。 */
function beep() {
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;
    const ctx = new Ctor();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.value = 0.06;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.18);
    osc.onended = () => void ctx.close();
  } catch {
    /* 浏览器禁止自动播放时静默忽略 */
  }
}

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
  const [settings, setSettings] = useState<UserSettings>(DEFAULT_SETTINGS);
  const [reasons, setReasons] = useState<Record<string, string>>(DEFAULT_REASONS);

  // 未登录直接去 /login；token 失效也清掉
  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchMe()
      .then((u) => {
        setUser(u);
        // 工程师没有事件视图，直接去「我的待办」
        if (!isApprover(u)) router.replace("/todo");
      })
      .catch(() => {
        clearSession();
        router.replace("/login");
      })
      .finally(() => setChecking(false));
  }, [router]);

  // 个人设置（复测窗口 / 提示音 / 首屏条数 / 默认驳回原因）
  useEffect(() => {
    if (!user) return;
    fetchSettings()
      .then((data) => {
        setSettings(data.settings);
        setReasons(data.reject_reasons);
      })
      .catch(() => {
        /* 设置加载失败就用默认值，不阻塞看板 */
      });
  }, [user]);

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
      // 事件正文要合并而不是覆盖：AI 结论后到时不会把 aggregate 覆盖掉
      next[index] = {
        ...prev[index],
        ...record,
        event: { ...prev[index].event, ...record.event },
      };
      return next;
    });
  }, []);

  const patchEvent = useCallback((eventId: string, patch: Partial<EventRecord>) => {
    setEvents((prev) =>
      prev.map((item) => (item.event_id === eventId ? { ...item, ...patch } : item)),
    );
  }, []);

  // 提示音开关放进 ref：切换它不应该触发 SSE 重连
  const alertSoundRef = useRef(settings.alert_sound);
  useEffect(() => {
    alertSoundRef.current = settings.alert_sound;
  }, [settings.alert_sound]);
  const pageSize = settings.event_page_size;

  useEffect(() => {
    // 只有审批人能订阅全局事件流；工程师连票据都拿不到，别白连
    if (!user || !isApprover(user)) return;
    let closed = false;
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;

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
            status: "analyzing",
            received_at: new Date().toISOString(),
          });
          if (message.event.severity === "high" && alertSoundRef.current) beep();
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          setError(null);
          break;

        case "analysis":
          upsertEvent({
            event_id: message.event_id,
            event: message.event,
            analysis: message.analysis,
            citations: message.citations,
            status: "awaiting_approval",
            received_at: new Date().toISOString(),
            analysis_source: message.analysis_source,
            analysis_pending: message.analysis_pending ?? false,
            analysis_late: message.analysis_late,
            analysis_error: undefined,
          });
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
          break;

        case "event_update":
          // 持续告警刷新：只更新事件正文，不动分析/审批状态
          setEvents((prev) =>
            prev.map((item) =>
              item.event_id === message.event_id
                ? { ...item, event: { ...item.event, ...message.event } }
                : item,
            ),
          );
          break;

        case "analysis_error":
          if (message.approval_unaffected) {
            // AI 补充意见失败：事件仍然可以审批，只是没有 AI 结论
            patchEvent(message.event_id, {
              analysis_pending: false,
              analysis_error: message.error,
            } as Partial<EventRecord>);
            setError(`AI 补充意见失败（不影响审批）：${message.error}`);
          } else {
            patchEvent(message.event_id, {
              status: "analysis_failed",
              error: message.error,
            } as Partial<EventRecord>);
            setError(`分析失败：${message.error}`);
          }
          setStatus((prev) => (prev ? { ...prev, counts: message.counts } : prev));
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

    const names = [
      "hello",
      "event",
      "event_update",
      "analysis",
      "analysis_error",
      "work_order",
      "rejected",
    ];

    const detach = () => {
      if (!source) return;
      for (const name of names) source.removeEventListener(name, handle as EventListener);
      source.close();
      source = null;
    };

    const scheduleRetry = () => {
      if (closed || retry) return;
      retry = setTimeout(() => {
        retry = null;
        void connect();
      }, 3000);
    };

    const connect = async () => {
      if (closed) return;
      detach();
      try {
        // 票据走查询参数：EventSource 不能自定义请求头
        const { ticket } = await fetchStreamTicket();
        if (closed) return;
        // limit 来自「首屏事件条数」设置
        const es = new EventSource(
          `${API_BASE}/api/stream?ticket=${encodeURIComponent(ticket)}&limit=${pageSize}`,
        );
        source = es;
        es.onopen = () => setSseConnected(true);
        es.onerror = () => {
          setSseConnected(false);
          detach();
          scheduleRetry(); // 票据过期或后端重启：换一张再连
        };
        for (const name of names) es.addEventListener(name, handle as EventListener);
      } catch {
        setSseConnected(false);
        scheduleRetry();
      }
    };

    void connect();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      detach();
    };
  }, [patchEvent, upsertEvent, pageSize, user]);

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
    async (decision: "approve" | "reject", comment: string, reasonCode?: RejectReasonCode) => {
      if (!selected) return;
      if (
        decision === "approve" &&
        settings.confirm_before_approve &&
        !window.confirm("确认批准该事件？批准后将生成工单。")
      ) {
        return;
      }
      setBusyId(selected.event_id);
      setError(null);
      try {
        // 审批人由后端从登录会话取，前端只提交决定与意见（驳回时带结构化原因）
        await postApproval(selected.event_id, decision, {
          comment,
          reasonCode: reasonCode ?? settings.default_reject_reason,
        });
        // SSE 掉线时也要立刻反映状态，避免按钮一直可点导致重复提交
        patchEvent(selected.event_id, {
          status: decision === "approve" ? "approved" : "rejected",
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusyId(null);
      }
    },
    [selected, patchEvent, settings.confirm_before_approve, settings.default_reject_reason],
  );

  const [woBusy, setWoBusy] = useState<number | null>(null);

  const woAction = useCallback(
    async (action: WorkOrderAction, order: WorkOrder, value?: string) => {
      setWoBusy(order.id);
      setError(null);
      try {
        await reopenWorkOrder(order.id);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setWoBusy(null);
      }
    },
    [],
  );

  /** 派单：从负责人表选人（或手填），错误抛回对话框 */
  const assignOrder = useCallback(
    async (
      order: WorkOrder,
      target: { staffId?: number; assignee?: string; note?: string },
    ) => {
      setWoBusy(order.id);
      setError(null);
      try {
        await assignWorkOrder(order.id, target);
        // 用服务端结果刷新：负责人姓名/账号、状态都以库里为准
        setOrders(await fetchWorkOrders(200));
        window.dispatchEvent(new Event("sm:refresh-notifications"));
      } finally {
        setWoBusy(null);
      }
    },
    [],
  );

  /** 接单/已处理之后重新拉一次工单（SSE 也会推，这里保证离线时也刷新） */
  const refreshOrders = useCallback(async () => {
    try {
      setOrders(await fetchWorkOrders(200));
    } catch {
      /* 刷新失败不打断操作 */
    }
  }, []);

  /** 关闭工单：错误要抛回对话框（验证未通过时展示结论 + 例外理由） */
  const closeOrder = useCallback(
    async (order: WorkOrder, resolution: string, exceptionReason?: string) => {
      setWoBusy(order.id);
      setError(null);
      try {
        await closeWorkOrder(order.id, resolution, exceptionReason);
        setOrders((prev) => {
          const index = prev.findIndex((item) => item.id === order.id);
          if (index === -1) return prev;
          const next = [...prev];
          next[index] = { ...next[index], status: "closed", resolution, exception_reason: exceptionReason ?? null };
          return next;
        });
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
  if (!isApprover(user)) {
    return (
      <main className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        工程师账号只能查看派给自己的工单，正在跳转到「我的待办」…
      </main>
    );
  }

  return (
    <div className="min-h-screen">
      <StatusBar
        status={status}
        sseConnected={sseConnected}
        lastError={error}
        username={user.username}
        role={user.role}
        onSignOut={signOut}
      />

      <main className="mx-auto max-w-[1600px] px-6 py-4">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="h-[620px] lg:col-span-4">
            <EventList events={events} selectedId={selectedId} onSelect={setSelectedId} />
          </div>
          <div className="h-[620px] lg:col-span-8">
            <AnalysisPanel
              record={selected}
              busy={busyId === selected?.event_id}
              approver={user.username}
              reasons={reasons}
              defaultRejectReason={settings.default_reject_reason}
              onDecide={decide}
              onRetry={retry}
              onResubmit={resubmit}
            />
          </div>
        </div>

        <div className="mt-4">
          <WorkOrderTable
            orders={orders}
            onAction={woAction}
            onClose={closeOrder}
            onAssign={assignOrder}
            busyId={woBusy}
            retestWindow={settings.retest_window_minutes}
            onRefresh={refreshOrders}
          />
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
