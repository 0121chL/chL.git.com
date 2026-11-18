"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchNotifications, markAllNotificationsRead, markNotificationRead } from "@/lib/api";
import type { AppNotification } from "@/lib/types";

const KIND_STYLE: Record<string, string> = {
  dispatch: "border-sky-300 bg-sky-50 text-sky-700",
  assignee_unbound: "border-amber-300 bg-amber-50 text-amber-800",
  system: "border-slate-300 bg-slate-50 text-slate-600",
};

const KIND_LABEL: Record<string, string> = {
  dispatch: "派单",
  assignee_unbound: "未绑定账号",
  system: "系统",
};

function clock(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchNotifications(false);
      setUnread(data.unread);
      setItems(data.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
    // 站内待办：别的动作（派单/审批/关闭）会派发这个事件，立即刷新
    const onChange = () => void load();
    window.addEventListener("sm:refresh-notifications", onChange);
    const timer = setInterval(() => void load(), 20000);
    return () => {
      window.removeEventListener("sm:refresh-notifications", onChange);
      clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const readOne = async (id: number) => {
    try {
      const result = await markNotificationRead(id);
      setUnread(result.unread);
      setItems((prev) =>
        prev.map((item) =>
          item.id === id ? { ...item, read_at: new Date().toISOString() } : item,
        ),
      );
    } catch {
      /* 标已读失败不影响查看 */
    }
  };

  const readAll = async () => {
    try {
      await markAllNotificationsRead();
      setUnread(0);
      setItems((prev) => prev.map((item) => ({ ...item, read_at: item.read_at ?? "now" })));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="relative rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
        data-testid="notification-bell"
        aria-label="站内通知"
      >
        通知
        {unread > 0 ? (
          <span
            className="absolute -right-1.5 -top-1.5 rounded-full bg-rose-600 px-1 text-[10px] font-semibold text-white"
            data-testid="notification-badge"
          >
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </button>

      {open ? (
        <div
          className="absolute right-0 z-40 mt-1 w-80 rounded-lg border border-slate-200 bg-white shadow-lg"
          data-testid="notification-panel"
        >
          <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
            <span className="text-xs font-semibold">站内通知</span>
            <button
              type="button"
              onClick={readAll}
              className="text-[11px] text-slate-500 hover:text-slate-800"
              data-testid="notification-read-all"
            >
              全部标为已读
            </button>
          </div>
          <div className="max-h-80 overflow-y-auto">
            {error ? (
              <p className="px-3 py-2 text-[11px] text-rose-600">{error}</p>
            ) : items.length === 0 ? (
              <p className="px-3 py-6 text-center text-[11px] text-slate-400">暂无通知</p>
            ) : (
              items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => readOne(item.id)}
                  className={`block w-full border-b border-slate-50 px-3 py-2 text-left last:border-b-0 hover:bg-slate-50 ${
                    item.read_at ? "opacity-60" : ""
                  }`}
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className={`rounded border px-1 py-0.5 text-[10px] ${
                        KIND_STYLE[item.kind] ?? KIND_STYLE.system
                      }`}
                    >
                      {KIND_LABEL[item.kind] ?? item.kind}
                    </span>
                    <span className="truncate text-[11px] font-medium text-slate-800">
                      {item.title}
                    </span>
                    {item.read_at ? null : (
                      <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-rose-500" />
                    )}
                  </div>
                  {item.body ? (
                    <p className="mt-0.5 line-clamp-2 text-[11px] text-slate-500">{item.body}</p>
                  ) : null}
                  <p className="mt-0.5 font-mono text-[10px] text-slate-400">
                    {clock(item.created_at)}
                  </p>
                </button>
              ))
            )}
          </div>
          <div className="border-t border-slate-100 px-3 py-1.5 text-right">
            <a href="/todo" className="text-[11px] text-slate-500 hover:text-slate-800">
              打开我的待办 →
            </a>
          </div>
        </div>
      ) : null}
    </div>
  );
}
