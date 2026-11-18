"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { NotificationBell } from "@/components/NotificationBell";
import { MyStats } from "@/components/MyStats";
import { WorkOrderTable, type WorkOrderAction } from "@/components/WorkOrderTable";
import {
  ApiError,
  assignWorkOrder,
  closeWorkOrder,
  fetchMe,
  fetchSettings,
  fetchWorkOrders,
  logout,
  reopenWorkOrder,
} from "@/lib/api";
import { clearSession, getToken } from "@/lib/auth";
import { isApprover } from "@/lib/roles";
import type { WorkOrder } from "@/lib/types";

function isMine(order: WorkOrder, username: string): boolean {
  if (order.assignee_username) return order.assignee_username === username;
  return (order.assignee ?? "") === username;
}

export default function TodoPage() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);
  const [approver, setApprover] = useState(false);
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [retestWindow, setRetestWindow] = useState(10);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [me, list, settings] = await Promise.all([
        fetchMe(),
        fetchWorkOrders(200),
        fetchSettings().catch(() => null),
      ]);
      setUsername(me.username);
      setApprover(isApprover(me));
      setOrders(list);
      if (settings) setRetestWindow(settings.settings.retest_window_minutes);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearSession();
        router.replace("/login");
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void load();
    const timer = setInterval(() => void load(), 30000);
    return () => clearInterval(timer);
  }, [load, router]);

  const mine = useMemo(
    () => (username ? orders.filter((o) => o.status !== "closed" && isMine(o, username)) : []),
    [orders, username],
  );
  // 统计要看全部（含已关闭），和上面的"未完成列表"分开
  const myAll = useMemo(
    () => (username ? orders.filter((o) => isMine(o, username)) : []),
    [orders, username],
  );
  const unassigned = useMemo(
    () => orders.filter((o) => o.status !== "closed" && !o.assignee),
    [orders],
  );

  const signOut = useCallback(async () => {
    try {
      await logout();
    } catch {
      /* 网络失败也要能退出 */
    } finally {
      clearSession();
      router.replace("/login");
    }
  }, [router]);

  const onAction = useCallback(
    async (action: WorkOrderAction, order: WorkOrder) => {
      setBusyId(order.id);
      try {
        await reopenWorkOrder(order.id);
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  const onClose = useCallback(
    async (order: WorkOrder, resolution: string, exceptionReason?: string) => {
      setBusyId(order.id);
      try {
        await closeWorkOrder(order.id, resolution, exceptionReason);
        window.dispatchEvent(new Event("sm:refresh-notifications"));
        await load();
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  const onAssign = useCallback(
    async (
      order: WorkOrder,
      target: { staffId?: number; assignee?: string; note?: string; auto?: boolean; team?: string },
    ) => {
      setBusyId(order.id);
      try {
        await assignWorkOrder(order.id, target);
        window.dispatchEvent(new Event("sm:refresh-notifications"));
        await load();
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  if (loading) {
    return (
      <main className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        正在加载待办…
      </main>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-[1600px] items-center gap-3 px-6 py-3">
          <Link href="/" className="text-sm text-slate-600 underline-offset-2 hover:underline">
            ← 返回看板
          </Link>
          <h1 className="text-base font-semibold tracking-tight">我的待办</h1>
          <span className="text-[11px] text-slate-500" data-testid="todo-user">
            {username ? `当前用户：${username}（${approver ? "审批人" : "工程师"}）` : ""}
          </span>
          <div className="ml-auto flex items-center gap-2">
            <NotificationBell />
            <Link
              href="/settings"
              className="rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
              data-testid="todo-settings"
            >
              设置
            </Link>
            <button
              type="button"
              onClick={() => void load()}
              className="rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
            >
              刷新
            </button>
            <button
              type="button"
              onClick={() => void signOut()}
              className="rounded border border-slate-300 px-2 py-1 text-[11px] text-rose-600 hover:bg-rose-50"
              data-testid="todo-signout"
            >
              退出登录
            </button>
          </div>
        </div>
        {error ? (
          <div className="border-t border-rose-200 bg-rose-50 px-5 py-1.5 text-xs text-rose-700">
            {error}
          </div>
        ) : null}
      </header>

      <main className="mx-auto max-w-[1600px] space-y-5 px-6 py-4">
        <MyStats orders={myAll} />

        <section>
          <div className="mb-2 flex items-baseline gap-2">
            <h2 className="text-sm font-semibold">分派给我的（{mine.length}）</h2>
            <span className="text-[11px] text-slate-500">
              只有负责人表里绑定了你账号的工单才会出现在这里
            </span>
          </div>
          {mine.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-300 bg-white px-4 py-8 text-center text-xs text-slate-400">
              暂无分派给你的工单。派单时选择绑定你账号的负责人即可。
            </div>
          ) : (
            <WorkOrderTable
              orders={mine}
              onAction={onAction}
              onClose={onClose}
              onAssign={approver ? onAssign : undefined}
              canAssign={approver}
              busyId={busyId}
              retestWindow={retestWindow}
              onRefresh={load}
            />
          )}
        </section>

        {approver ? (
          <section>
            <div className="mb-2 flex items-baseline gap-2">
              <h2 className="text-sm font-semibold">待派单（{unassigned.length}）</h2>
              <span className="text-[11px] text-slate-500">
                已批准但还没指定负责人；派单后对应的人会在「分派给我的」里看到
              </span>
            </div>
            {unassigned.length === 0 ? (
              <div className="rounded-lg border border-dashed border-slate-300 bg-white px-4 py-8 text-center text-xs text-slate-400">
                没有待派单的工单。
              </div>
            ) : (
              <WorkOrderTable
                orders={unassigned}
                onAction={onAction}
                onClose={onClose}
                onAssign={onAssign}
                canAssign
                busyId={busyId}
                retestWindow={retestWindow}
                onRefresh={load}
              />
            )}
          </section>
        ) : null}

        <p className="pb-6 text-[11px] text-slate-400">
          {approver
            ? `关闭工单仍会先做「处置后验证」：最近 ${retestWindow} 分钟该测点峰值必须回落到阈值以下，否则要填例外理由（会留痕）。`
            : "你是工程师账号：这里只会显示派给你自己的工单。完成后点「已处理」提交整改措施，由审批人复测关闭。"}
        </p>
      </main>
    </div>
  );
}
