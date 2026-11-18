"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, acceptWorkOrder, fetchStaff, fetchWorkOrderDetail, handleWorkOrder } from "@/lib/api";
import { ruleLabel } from "@/lib/labels";
import type { Staff, WorkOrder, WorkOrderVerification } from "@/lib/types";

const RISK_LABEL: Record<string, string> = { high: "高", medium: "中", low: "低" };

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  created: { text: "待派单", cls: "border-amber-300 bg-amber-50 text-amber-800" },
  assigned: { text: "已派单·待接单", cls: "border-sky-300 bg-sky-50 text-sky-800" },
  accepted: { text: "已接单·处理中", cls: "border-indigo-300 bg-indigo-50 text-indigo-700" },
  handled: { text: "已处理·待复测", cls: "border-violet-300 bg-violet-50 text-violet-700" },
  closed: { text: "已关闭", cls: "border-emerald-300 bg-emerald-50 text-emerald-800" },
};

function ruleOf(order: WorkOrder): string | undefined {
  return order.content?.rule_id as string | undefined;
}

function summary(order: WorkOrder): string {
  const analysis = (order.content?.analysis ?? {}) as Record<string, string>;
  return analysis.regulation_basis ?? "—";
}

function riskOf(order: WorkOrder): string {
  const analysis = (order.content?.analysis ?? {}) as Record<string, string>;
  return RISK_LABEL[analysis.risk_level ?? ""] ?? "—";
}

function localTime(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export type WorkOrderAction = "assign" | "close" | "reopen";

export function WorkOrderTable({
  orders,
  onAction,
  onClose,
  onAssign,
  busyId,
  retestWindow,
  onRefresh,
  canAssign = true,
}: {
  orders: WorkOrder[];
  onAction?: (action: WorkOrderAction, order: WorkOrder, value?: string) => void;
  /** 关闭走独立回调：后端会先做处置后验证，失败要留在对话框里展示结论 */
  onClose?: (order: WorkOrder, resolution: string, exceptionReason?: string) => Promise<void>;
  /** 派单：从负责人表选人、手填、或按当班自动选；错误要抛回对话框 */
  onAssign?: (
    order: WorkOrder,
    target: { staffId?: number; assignee?: string; note?: string; auto?: boolean; team?: string },
  ) => Promise<void>;
  busyId?: number | null;
  /** 来自设置页的复测窗口（分钟），仅用于文案提示 */
  retestWindow?: number;
  /** 接单/已处理之后让父组件重新拉一次列表 */
  onRefresh?: () => void | Promise<void>;
  /** 只有审批人能派单；工程师界面上不显示「指派」 */
  canAssign?: boolean;
}) {
  const [closing, setClosing] = useState<WorkOrder | null>(null);
  const [resolution, setResolution] = useState("");
  const [exception, setException] = useState("");
  const [verification, setVerification] = useState<WorkOrderVerification | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 派单对话框
  const [assigning, setAssigning] = useState<WorkOrder | null>(null);
  const [staffList, setStaffList] = useState<Staff[]>([]);
  const [staffId, setStaffId] = useState<string>("");
  const [manualName, setManualName] = useState("");
  const [assignNote, setAssignNote] = useState("");
  const [assignAuto, setAssignAuto] = useState(false);
  const [assignTeam, setAssignTeam] = useState("");
  const [assignError, setAssignError] = useState<string | null>(null);
  const [assignBusy, setAssignBusy] = useState(false);

  // 已处理对话框（整改措施 + 照片）
  const [handling, setHandling] = useState<WorkOrder | null>(null);
  const [measure, setMeasure] = useState("");
  const [photo, setPhoto] = useState<string | null>(null);
  const [photoName, setPhotoName] = useState<string>("");
  const [handleError, setHandleError] = useState<string | null>(null);
  const [handleBusy, setHandleBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 照片查看
  const [photoView, setPhotoView] = useState<{ id: number; dataUrl: string } | null>(null);
  const [photoError, setPhotoError] = useState<string | null>(null);
  const [acceptingId, setAcceptingId] = useState<number | null>(null);

  useEffect(() => {
    // 负责人表变动不频繁，进页面拉一次即可；工程师没有派单权限，不用拉
    if (!canAssign) return;
    fetchStaff(true)
      .then(setStaffList)
      .catch(() => setStaffList([]));
  }, [canAssign]);

  const openAssign = (order: WorkOrder) => {
    setAssigning(order);
    setStaffId("");
    setManualName(order.assignee ?? "");
    setAssignNote("");
    setAssignAuto(false);
    setAssignTeam("");
    setAssignError(null);
  };

  const submitAssign = async () => {
    if (!assigning || !onAssign) return;
    const chosen = assignAuto ? undefined : staffId ? Number(staffId) : undefined;
    if (!assignAuto && !chosen && manualName.trim().length < 1) {
      setAssignError("请选择负责人、填写临时处理人，或勾选「按当班自动选人」");
      return;
    }
    setAssignBusy(true);
    setAssignError(null);
    try {
      await onAssign(assigning, {
        staffId: chosen,
        assignee: chosen || assignAuto ? undefined : manualName.trim(),
        note: assignNote.trim() || undefined,
        auto: assignAuto,
        team: assignAuto ? assignTeam.trim() || undefined : undefined,
      });
      setAssigning(null);
    } catch (err) {
      setAssignError(err instanceof Error ? err.message : String(err));
    } finally {
      setAssignBusy(false);
    }
  };

  const accept = async (order: WorkOrder) => {
    setAcceptingId(order.id);
    try {
      await acceptWorkOrder(order.id);
      setPhotoError(null);
      window.dispatchEvent(new Event("sm:refresh-notifications"));
      await onRefresh?.();
    } catch (err) {
      setPhotoError(err instanceof Error ? err.message : String(err));
    } finally {
      setAcceptingId(null);
    }
  };

  const openHandle = (order: WorkOrder) => {
    setHandling(order);
    setMeasure(order.measure ?? "");
    setPhoto(null);
    setPhotoName("");
    setHandleError(null);
  };

  const pickPhoto = (file: File | null) => {
    if (!file) return;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      setHandleError("只支持 jpeg / png / webp");
      return;
    }
    if (file.size > 3 * 1024 * 1024) {
      setHandleError("图片超过 3 MB，请压缩后再传");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setPhoto(String(reader.result));
      setPhotoName(file.name);
      setHandleError(null);
    };
    reader.onerror = () => setHandleError("读取图片失败");
    reader.readAsDataURL(file);
  };

  const submitHandle = async () => {
    if (!handling) return;
    if (measure.trim().length < 2) {
      setHandleError("请填写整改措施（至少 2 个字）");
      return;
    }
    setHandleBusy(true);
    setHandleError(null);
    try {
      await handleWorkOrder(handling.id, measure.trim(), photo ?? undefined);
      setHandling(null);
      window.dispatchEvent(new Event("sm:refresh-notifications"));
      await onRefresh?.();
    } catch (err) {
      setHandleError(err instanceof Error ? err.message : String(err));
    } finally {
      setHandleBusy(false);
    }
  };

  const openPhoto = async (order: WorkOrder) => {
    setPhotoError(null);
    try {
      const detail = await fetchWorkOrderDetail(order.id);
      if (!detail.work_order.photo_data_url) {
        setPhotoError("这张工单没有上传照片");
        return;
      }
      setPhotoView({ id: order.id, dataUrl: detail.work_order.photo_data_url });
    } catch (err) {
      setPhotoError(err instanceof Error ? err.message : String(err));
    }
  };

  const openClose = (order: WorkOrder) => {
    setClosing(order);
    setResolution("");
    setException("");
    setVerification(null);
    setDialogError(null);
  };

  const submitClose = async () => {
    if (!closing || !onClose) return;
    if (resolution.trim().length < 2) {
      setDialogError("请填写处置结果（至少 2 个字）");
      return;
    }
    setSubmitting(true);
    setDialogError(null);
    try {
      await onClose(closing, resolution.trim(), exception.trim() || undefined);
      setClosing(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const detail = err.detail as
          | { error?: string; verification?: WorkOrderVerification; hint?: string }
          | undefined;
        setVerification(detail?.verification ?? null);
        setDialogError(detail?.hint || detail?.error || err.message);
      } else {
        setDialogError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const act = (action: WorkOrderAction, order: WorkOrder, value?: string) => {
    if (!onAction) return;
    onAction(action, order, value);
  };

  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
        <h2 className="text-sm font-semibold">工单列表</h2>
        <span className="text-[11px] text-slate-500">
          来自 SQLite work_orders ⋈ approvals · {orders.length} 条
        </span>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2 font-medium">工单号</th>
              <th className="px-4 py-2 font-medium">事件</th>
              <th className="px-4 py-2 font-medium">风险</th>
              <th className="px-4 py-2 font-medium">审批人</th>
              <th className="px-4 py-2 font-medium">状态 / 处理人</th>
              <th className="px-4 py-2 font-medium">规程依据</th>
              <th className="px-4 py-2 font-medium">审批时间</th>
              <th className="px-4 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-6 text-center text-slate-400">
                  尚无工单。批准一条待审批事件后，工单会出现在这里。
                </td>
              </tr>
            ) : (
              orders.map((order) => {
                const status = STATUS_LABEL[order.status] ?? {
                  text: order.status,
                  cls: "border-slate-300 bg-slate-50 text-slate-600",
                };
                const busy = busyId === order.id;
                return (
                  <tr key={order.id} className="border-t border-slate-100">
                    <td className="px-4 py-2 font-mono font-semibold">WO-{String(order.id).padStart(4, "0")}</td>
                    <td className="px-4 py-2">
                      <div className="text-slate-700">{ruleLabel(ruleOf(order))}</div>
                      <div className="font-mono text-[10px] text-slate-400">{order.event_id}</div>
                    </td>
                    <td className="px-4 py-2">{riskOf(order)}</td>
                    <td className="px-4 py-2 text-slate-700" title={order.approval_comment ?? "无审批意见"}>
                      {order.approver ?? "—"}
                      {order.approval_comment ? <span className="ml-1 text-slate-400">（有意见）</span> : null}
                    </td>
                    <td className="px-4 py-2">
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] ${status.cls}`}>{status.text}</span>
                      {/* 处置后验证的结论：绿色=已复测通过，琥珀=例外关闭（留痕） */}
                      {order.verification ? (
                        order.verification.ok ? (
                          <span
                            className="ml-1 rounded border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700"
                            title={order.verification.message}
                            data-testid="verified-badge"
                          >
                            已复测
                          </span>
                        ) : (
                          <span
                            className="ml-1 rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800"
                            title={order.exception_reason ?? order.verification.message}
                            data-testid="exception-badge"
                          >
                            例外关闭
                          </span>
                        )
                      ) : null}
                      {order.status !== "closed" && order.sla?.deadline ? (
                        <div
                          className={`mt-0.5 text-[10px] ${
                            order.sla.overdue ? "font-medium text-rose-600" : "text-slate-500"
                          }`}
                          data-testid="sla-badge"
                        >
                          {order.sla.overdue
                            ? `SLA 超时 ${Math.max(1, Math.floor(Math.abs(order.sla.remaining_s ?? 0) / 60))} 分钟`
                            : `时限 ${order.sla.minutes} 分钟 · 剩 ${Math.max(
                                0,
                                Math.floor((order.sla.remaining_s ?? 0) / 60),
                              )} 分钟`}
                        </div>
                      ) : null}
                      {order.assignee ? (
                        <div className="mt-0.5 text-[10px] text-slate-500">处理人：{order.assignee}</div>
                      ) : null}
                      {order.accepted_by ? (
                        <div className="mt-0.5 text-[10px] text-slate-500">接单人：{order.accepted_by}</div>
                      ) : null}
                      {order.measure ? (
                        <div
                          className="mt-0.5 max-w-[200px] truncate text-[10px] text-slate-500"
                          title={order.measure}
                        >
                          措施：{order.measure}
                        </div>
                      ) : null}
                      {order.resolution ? (
                        <div className="mt-0.5 max-w-[180px] truncate text-[10px] text-slate-400" title={order.resolution}>
                          {order.resolution}
                        </div>
                      ) : null}
                    </td>
                    <td className="max-w-[380px] truncate px-4 py-2 text-slate-600" title={summary(order)}>
                      {summary(order)}
                    </td>
                    <td className="px-4 py-2 font-mono text-slate-500">{localTime(order.decided_at)}</td>
                    <td className="px-4 py-2">
                      <div className="flex flex-wrap gap-1">
                        {order.status === "assigned" ? (
                          <button
                            type="button"
                            disabled={busy || acceptingId === order.id}
                            onClick={() => accept(order)}
                            className="rounded border border-indigo-300 px-1.5 py-0.5 text-[10px] text-indigo-700 hover:bg-indigo-50 disabled:text-slate-300"
                            data-testid="accept-order"
                          >
                            {acceptingId === order.id ? "接单中…" : "接单"}
                          </button>
                        ) : null}
                        {order.status === "assigned" || order.status === "accepted" ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => openHandle(order)}
                            className="rounded border border-violet-300 px-1.5 py-0.5 text-[10px] text-violet-700 hover:bg-violet-50 disabled:text-slate-300"
                            data-testid="handle-order"
                          >
                            已处理
                          </button>
                        ) : null}
                        {order.photo_path ? (
                          <button
                            type="button"
                            onClick={() => openPhoto(order)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100"
                            data-testid="view-photo"
                          >
                            照片
                          </button>
                        ) : null}
                        {canAssign && order.status !== "closed" ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => openAssign(order)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100 disabled:text-slate-300"
                          >
                            指派
                          </button>
                        ) : null}
                        {order.status !== "closed" ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => openClose(order)}
                            className="rounded border border-emerald-300 px-1.5 py-0.5 text-[10px] text-emerald-700 hover:bg-emerald-50 disabled:text-slate-300"
                          >
                            关闭
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => act("reopen", order)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100 disabled:text-slate-300"
                          >
                            重新打开
                          </button>
                        )}
                      </div>
                      {photoError ? (
                        <div className="mt-1 text-[10px] text-rose-600">{photoError}</div>
                      ) : null}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {assigning ? (
        <div className="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-lg bg-white shadow-xl">
            <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h3 className="text-sm font-semibold">
                派单 WO-{String(assigning.id).padStart(4, "0")}
              </h3>
              <button
                type="button"
                onClick={() => setAssigning(null)}
                className="text-slate-400 hover:text-slate-600"
                aria-label="关闭对话框"
              >
                ✕
              </button>
            </header>

            <div className="space-y-3 px-4 py-3">
              <label className="flex items-center gap-2 rounded bg-slate-50 px-2 py-1.5 text-xs text-slate-700">
                <input
                  type="checkbox"
                  checked={assignAuto}
                  onChange={(e) => setAssignAuto(e.target.checked)}
                  data-testid="assign-auto"
                />
                按当班自动选人（当班负责人里挑在办工单最少的）
              </label>

              {assignAuto ? (
                <label className="block text-xs text-slate-600">
                  限定班组（可选）
                  <input
                    value={assignTeam}
                    onChange={(e) => setAssignTeam(e.target.value)}
                    maxLength={40}
                    className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                    placeholder="留空 = 全体当班人员"
                    data-testid="assign-team"
                  />
                </label>
              ) : (
                <>
                  <label className="block text-xs text-slate-600">
                    负责人
                    <select
                      value={staffId}
                      onChange={(e) => setStaffId(e.target.value)}
                      className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                      data-testid="assign-staff"
                    >
                      <option value="">— 从负责人表选择 —</option>
                      {staffList.map((person) => (
                        <option key={person.id} value={person.id}>
                          {person.name}
                          {person.team ? `（${person.team}）` : ""}
                          {person.on_duty ? " ★当班" : ""}
                          {person.username ? "" : " · 未绑定账号"}
                        </option>
                      ))}
                    </select>
                  </label>

                  {!staffId ? (
                    <label className="block text-xs text-slate-600">
                      或手填处理人
                      <input
                        value={manualName}
                        onChange={(e) => setManualName(e.target.value)}
                        maxLength={60}
                        className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                        placeholder="负责人表里没有的人（不会收到站内通知）"
                        data-testid="assign-manual"
                      />
                    </label>
                  ) : null}
                </>
              )}

              <label className="block text-xs text-slate-600">
                派单说明（可选）
                <input
                  value={assignNote}
                  onChange={(e) => setAssignNote(e.target.value)}
                  maxLength={200}
                  className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                  placeholder="例如：先停机再复测，4 小时内反馈"
                  data-testid="assign-note"
                />
              </label>

              <p className="rounded bg-slate-50 px-2 py-1.5 text-[11px] text-slate-500">
                绑定了账号的负责人会收到站内通知，并能在「我的待办」里看到这张工单；
                未绑定账号的只记录姓名，通知会回到派单人（提醒线下通知）。
              </p>

              {assignError ? (
                <p className="text-xs text-rose-600" data-testid="assign-error">
                  {assignError}
                </p>
              ) : null}
            </div>

            <footer className="flex justify-end gap-2 border-t border-slate-200 px-4 py-3">
              <button
                type="button"
                onClick={() => setAssigning(null)}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submitAssign}
                disabled={assignBusy}
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
                data-testid="assign-submit"
              >
                {assignBusy ? "派单中…" : "确认派单"}
              </button>
            </footer>
          </div>
        </div>
      ) : null}

      {handling ? (
        <div className="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-lg bg-white shadow-xl">
            <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h3 className="text-sm font-semibold">
                标记已处理 WO-{String(handling.id).padStart(4, "0")}
              </h3>
              <button
                type="button"
                onClick={() => setHandling(null)}
                className="text-slate-400 hover:text-slate-600"
                aria-label="关闭对话框"
              >
                ✕
              </button>
            </header>

            <div className="space-y-3 px-4 py-3">
              <label className="block text-xs text-slate-600">
                整改措施
                <textarea
                  value={measure}
                  onChange={(e) => setMeasure(e.target.value)}
                  rows={3}
                  maxLength={500}
                  className="mt-1 w-full resize-none rounded border border-slate-300 px-2 py-1 text-sm"
                  placeholder="例如：已停机更换轴承，复测振动 2.1 mm/s"
                  data-testid="handle-measure"
                />
              </label>

              <div className="text-xs text-slate-600">
                现场照片（可选，≤3 MB）
                <div className="mt-1 flex items-center gap-2">
                  <input
                    ref={fileRef}
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    className="hidden"
                    onChange={(e) => pickPhoto(e.target.files?.[0] ?? null)}
                    data-testid="handle-photo-input"
                  />
                  <button
                    type="button"
                    onClick={() => fileRef.current?.click()}
                    className="rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
                  >
                    选择照片
                  </button>
                  <span className="text-[11px] text-slate-500">{photoName || "未选择"}</span>
                  {photo ? (
                    <button
                      type="button"
                      onClick={() => {
                        setPhoto(null);
                        setPhotoName("");
                      }}
                      className="text-[11px] text-rose-600"
                    >
                      移除
                    </button>
                  ) : null}
                </div>
              </div>
              {photo ? (
                <img
                  src={photo}
                  alt="整改照片预览"
                  className="max-h-40 w-auto rounded border border-slate-200"
                />
              ) : null}

              <p className="rounded bg-slate-50 px-2 py-1.5 text-[11px] text-slate-500">
                标记已处理后工单进入「待复测」：必须等遥测回落到阈值以下才能关闭（否则要填例外理由）。
              </p>

              {handleError ? (
                <p className="text-xs text-rose-600" data-testid="handle-error">
                  {handleError}
                </p>
              ) : null}
            </div>

            <footer className="flex justify-end gap-2 border-t border-slate-200 px-4 py-3">
              <button
                type="button"
                onClick={() => setHandling(null)}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submitHandle}
                disabled={handleBusy}
                className="rounded-md bg-violet-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-violet-700 disabled:bg-slate-300"
                data-testid="handle-submit"
              >
                {handleBusy ? "提交中…" : "确认已处理"}
              </button>
            </footer>
          </div>
        </div>
      ) : null}

      {photoView ? (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/60 p-4"
          onClick={() => setPhotoView(null)}
        >
          <div className="max-w-2xl rounded-lg bg-white p-3 shadow-xl">
            <div className="mb-2 flex items-center justify-between gap-4">
              <span className="text-xs font-semibold">
                WO-{String(photoView.id).padStart(4, "0")} 整改照片
              </span>
              <button
                type="button"
                onClick={() => setPhotoView(null)}
                className="text-slate-400 hover:text-slate-600"
              >
                ✕
              </button>
            </div>
            <img
              src={photoView.dataUrl}
              alt="整改照片"
              className="max-h-[70vh] w-auto rounded"
              data-testid="photo-viewer"
            />
          </div>
        </div>
      ) : null}

      {closing ? (
        <div className="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-lg rounded-lg bg-white shadow-xl">
            <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h3 className="text-sm font-semibold">
                关闭 WO-{String(closing.id).padStart(4, "0")}
              </h3>
              <button
                type="button"
                onClick={() => setClosing(null)}
                className="text-slate-400 hover:text-slate-600"
                aria-label="关闭对话框"
              >
                ✕
              </button>
            </header>

            <div className="space-y-3 px-4 py-3">
              <p className="rounded bg-slate-50 px-2 py-1.5 text-[11px] text-slate-600">
                关闭前会校验最近 <strong>{retestWindow ?? 10} 分钟</strong>
                该测点的遥测峰值；仍超阈值时必须填写例外理由（会写入审计，可追溯）。
              </p>

              <label className="block text-xs text-slate-600">
                处置结果
                <textarea
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                  rows={2}
                  maxLength={500}
                  className="mt-1 w-full resize-none rounded border border-slate-300 px-2 py-1 text-sm"
                  placeholder="例如：已停机更换轴承，复测振动 2.1 mm/s"
                  data-testid="close-resolution"
                />
              </label>

              <label className="block text-xs text-slate-600">
                例外理由
                <span className="ml-1 text-slate-400">（验证未通过时必填，≥4 字）</span>
                <input
                  value={exception}
                  onChange={(e) => setException(e.target.value)}
                  maxLength={200}
                  className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                  placeholder="例如：现场复测已合格，遥测通道断线无法回传"
                  data-testid="close-exception"
                />
              </label>

              {verification ? (
                <div
                  className={`rounded border px-2 py-1.5 text-[11px] ${
                    verification.ok
                      ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                      : "border-amber-200 bg-amber-50 text-amber-800"
                  }`}
                  data-testid="close-verification"
                >
                  <div className="font-medium">
                    验证结论：{verification.ok ? "已恢复" : "未通过"}
                  </div>
                  <div className="mt-0.5">{verification.message}</div>
                  {verification.mode === "telemetry" ? (
                    <div className="mt-0.5 font-mono">
                      窗口 {verification.window_minutes ?? "—"} 分钟 · 样本{" "}
                      {verification.samples ?? 0} · 峰值 {verification.max ?? "—"}
                      {verification.unit ?? ""} · 阈值 {verification.threshold ?? "—"}
                      {verification.unit ?? ""}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {dialogError ? (
                <p className="text-xs text-rose-600" data-testid="close-error">
                  {dialogError}
                </p>
              ) : null}
            </div>

            <footer className="flex justify-end gap-2 border-t border-slate-200 px-4 py-3">
              <button
                type="button"
                onClick={() => setClosing(null)}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submitClose}
                disabled={submitting}
                className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:bg-slate-300"
                data-testid="close-submit"
              >
                {submitting ? "提交中…" : "确认关闭"}
              </button>
            </footer>
          </div>
        </div>
      ) : null}
    </section>
  );
}
