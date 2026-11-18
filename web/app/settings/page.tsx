"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  changePassword,
  createShift,
  createStaff,
  deleteShift,
  disableStaff,
  fetchInvite,
  fetchRejectStats,
  fetchSessions,
  fetchSettings,
  fetchShifts,
  fetchStaff,
  patchStaff,
  revokeAllSessions,
  rotateInvite,
  saveSettings,
  syncShifts,
  type RejectStats,
  type SessionRow,
  type ShiftList,
} from "@/lib/api";
import { clearSession, getToken } from "@/lib/auth";
import { isApprover } from "@/lib/roles";
import type { RejectReasonCode, Staff, UserSettings } from "@/lib/types";

const PAGE_SIZES = [20, 50, 100] as const;

function Card({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <header className="border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {desc ? <p className="mt-0.5 text-[11px] text-slate-500">{desc}</p> : null}
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-xs text-slate-600">
      <span className="font-medium text-slate-700">{label}</span>
      {hint ? <span className="ml-1 text-slate-400">{hint}</span> : null}
      <div className="mt-1">{children}</div>
    </label>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [defaults, setDefaults] = useState<UserSettings | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [profile, setProfile] = useState<{ username?: string; role?: string }>({});

  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [stats, setStats] = useState<RejectStats | null>(null);
  const [canManage, setCanManage] = useState(false);
  const [invite, setInvite] = useState<{ invite_code: string; source: string } | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);

  const [staffList, setStaffList] = useState<Staff[]>([]);
  const [newName, setNewName] = useState("");
  const [newTeam, setNewTeam] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [newAccount, setNewAccount] = useState("");
  const [staffError, setStaffError] = useState<string | null>(null);
  const [staffBusy, setStaffBusy] = useState(false);

  // 排班表
  const [shiftInfo, setShiftInfo] = useState<ShiftList | null>(null);
  const [shiftName, setShiftName] = useState("早班");
  const [shiftStaffId, setShiftStaffId] = useState("");
  const [shiftStart, setShiftStart] = useState("08:00");
  const [shiftEnd, setShiftEnd] = useState("16:00");
  const [shiftWeekdays, setShiftWeekdays] = useState("1,2,3,4,5");
  const [shiftError, setShiftError] = useState<string | null>(null);
  const [shiftBusy, setShiftBusy] = useState(false);

  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [pwdBusy, setPwdBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchSettings();
      setSettings(data.settings);
      setDefaults(data.defaults);
      setReasons(data.reject_reasons);
      setProfile(data.user);
      const manage = isApprover(data.user);
      setCanManage(manage);
      const [sessionRows, statRows] = await Promise.all([
        fetchSessions().catch(() => []),
        // 工程师没有处置质量统计权限，失败就是空
        manage ? fetchRejectStats(7).catch(() => null) : Promise.resolve(null),
      ]);
      setSessions(sessionRows);
      setStats(statRows);
      if (manage) {
        setStaffList(await fetchStaff(false).catch(() => []));
        setShiftInfo(await fetchShifts().catch(() => null));
        setInvite(await fetchInvite().catch(() => null));
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearSession();
        router.replace("/login");
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  }, [router]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void load();
  }, [load, router]);

  const patch = <K extends keyof UserSettings>(key: K, value: UserSettings[K]) => {
    setSettings((prev) => (prev ? { ...prev, [key]: value } : prev));
    setNotice(null);
  };

  async function submitSettings() {
    if (!settings) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await saveSettings(settings);
      setSettings(result.settings);
      setNotice("设置已保存");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function submitPassword() {
    setError(null);
    setNotice(null);
    if (newPwd !== confirmPwd) {
      setError("两次输入的新密码不一致");
      return;
    }
    setPwdBusy(true);
    try {
      const result = await changePassword(oldPwd, newPwd);
      setOldPwd("");
      setNewPwd("");
      setConfirmPwd("");
      setNotice(
        result.revoked_sessions > 0
          ? `密码已更新，其它 ${result.revoked_sessions} 个会话已失效`
          : "密码已更新",
      );
      void load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPwdBusy(false);
    }
  }

  async function revokeAll() {
    if (!window.confirm("确定吊销该账号的全部会话？当前页面会被登出。")) return;
    try {
      await revokeAllSessions();
    } catch {
      /* 即使接口失败也要清本地 */
    }
    clearSession();
    router.replace("/login");
  }

  async function addStaff() {
    setStaffError(null);
    if (!newName.trim()) {
      setStaffError("请填写负责人姓名");
      return;
    }
    setStaffBusy(true);
    try {
      await createStaff({
        name: newName.trim(),
        team: newTeam.trim() || undefined,
        phone: newPhone.trim() || undefined,
        username: newAccount.trim() || undefined,
      });
      setNewName("");
      setNewTeam("");
      setNewPhone("");
      setNewAccount("");
      setStaffList(await fetchStaff(false));
      setNotice("负责人已添加");
    } catch (err) {
      setStaffError(err instanceof Error ? err.message : String(err));
    } finally {
      setStaffBusy(false);
    }
  }

  async function removeStaff(person: Staff) {
    if (!window.confirm(`停用负责人「${person.name}」？历史工单里的姓名会保留。`)) return;
    setStaffError(null);
    try {
      await disableStaff(person.id);
      setStaffList(await fetchStaff(false));
    } catch (err) {
      setStaffError(err instanceof Error ? err.message : String(err));
    }
  }

  async function toggleDuty(person: Staff) {
    setStaffError(null);
    try {
      await patchStaff(person.id, { on_duty: !person.on_duty });
      setStaffList(await fetchStaff(false));
    } catch (err) {
      setStaffError(err instanceof Error ? err.message : String(err));
    }
  }

  async function addShift() {
    setShiftError(null);
    if (!shiftStaffId) {
      setShiftError("请选择值班负责人");
      return;
    }
    setShiftBusy(true);
    try {
      await createShift({
        name: shiftName.trim() || "班次",
        staff_id: Number(shiftStaffId),
        start_time: shiftStart,
        end_time: shiftEnd,
        weekdays: shiftWeekdays.trim() || "1,2,3,4,5,6,7",
      });
      setShiftInfo(await fetchShifts().catch(() => null));
      setStaffList(await fetchStaff(false));
      setNotice("班次已添加，当班状态已按当前时间同步");
    } catch (err) {
      setShiftError(err instanceof Error ? err.message : String(err));
    } finally {
      setShiftBusy(false);
    }
  }

  async function removeShift(shiftId: number) {
    setShiftError(null);
    try {
      await deleteShift(shiftId);
      setShiftInfo(await fetchShifts().catch(() => null));
      setStaffList(await fetchStaff(false));
    } catch (err) {
      setShiftError(err instanceof Error ? err.message : String(err));
    }
  }

  async function runDutySync() {
    setShiftError(null);
    try {
      await syncShifts();
      setShiftInfo(await fetchShifts().catch(() => null));
      setStaffList(await fetchStaff(false));
      setNotice("已按当前时间同步当班");
    } catch (err) {
      setShiftError(err instanceof Error ? err.message : String(err));
    }
  }

  async function rotateApproverInvite() {
    if (!window.confirm("轮换后旧邀请码立即失效，确定继续？")) return;
    setInviteError(null);
    try {
      const fresh = await rotateInvite();
      setInvite(fresh);
      setNotice("邀请码已轮换，旧码立即失效");
    } catch (err) {
      setInviteError(err instanceof Error ? err.message : String(err));
      if (err instanceof ApiError && err.status === 409) {
        setInvite(await fetchInvite().catch(() => null));
      }
    }
  }

  if (checking || !settings) {
    return (
      <main className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        {error ? `加载失败：${error}` : "正在加载设置…"}
      </main>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-4xl items-center gap-4 px-5 py-3">
          <Link href="/" className="text-sm text-slate-600 underline-offset-2 hover:underline">
            ← 返回看板
          </Link>
          <h1 className="text-base font-semibold tracking-tight">设置</h1>
          <span className="ml-auto text-xs text-slate-500" data-testid="settings-user">
            {profile.username ?? "—"}
            {profile.role ? ` · ${profile.role}` : ""}
          </span>
        </div>
        {error ? (
          <div className="border-t border-rose-200 bg-rose-50 px-5 py-1.5 text-xs text-rose-700">
            {error}
          </div>
        ) : null}
        {notice ? (
          <div className="border-t border-emerald-200 bg-emerald-50 px-5 py-1.5 text-xs text-emerald-700">
            {notice}
          </div>
        ) : null}
      </header>

      <main className="mx-auto max-w-4xl space-y-4 px-5 py-4">
        <Card
          title="审批与处置偏好"
          desc="只影响你自己的操作习惯，不会改动规则阈值或他人的审批记录。"
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="复测窗口"
              hint="（分钟，关闭工单时回看这段遥测）"
            >
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={120}
                  value={settings.retest_window_minutes}
                  onChange={(e) => patch("retest_window_minutes", Number(e.target.value))}
                  className="w-24 rounded border border-slate-300 px-2 py-1 text-sm"
                  data-testid="retest-window"
                />
                <span className="text-[11px] text-slate-500">
                  关闭工单前会检查最近 {settings.retest_window_minutes} 分钟的峰值
                </span>
              </div>
            </Field>

            <Field label="首屏事件条数">
              <select
                value={settings.event_page_size}
                onChange={(e) =>
                  patch("event_page_size", Number(e.target.value) as UserSettings["event_page_size"])
                }
                className="w-32 rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid="page-size"
              >
                {PAGE_SIZES.map((size) => (
                  <option key={size} value={size}>
                    {size} 条
                  </option>
                ))}
              </select>
            </Field>

            <Field label="默认驳回原因" hint="（减少每次手选）">
              <select
                value={settings.default_reject_reason}
                onChange={(e) =>
                  patch("default_reject_reason", e.target.value as RejectReasonCode)
                }
                className="w-40 rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid="default-reason"
              >
                {Object.entries(reasons).map(([code, label]) => (
                  <option key={code} value={code}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>

            <div className="space-y-2 pt-1">
              <label className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={settings.confirm_before_approve}
                  onChange={(e) => patch("confirm_before_approve", e.target.checked)}
                  data-testid="confirm-approve"
                />
                批准前二次确认
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={settings.alert_sound}
                  onChange={(e) => patch("alert_sound", e.target.checked)}
                  data-testid="alert-sound"
                />
                新高危事件提示音
              </label>
            </div>
          </div>

          <div className="mt-4 flex items-center gap-2">
            <button
              type="button"
              onClick={submitSettings}
              disabled={saving}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
              data-testid="save-settings"
            >
              {saving ? "保存中…" : "保存设置"}
            </button>
            <button
              type="button"
              onClick={() => {
                if (defaults) setSettings(defaults);
                setNotice("已恢复默认值，记得保存");
              }}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
            >
              恢复默认
            </button>
          </div>
        </Card>

        <Card title="账号" desc="密码用 PBKDF2-SHA256 加盐存储；改密后其它设备的会话会立即失效。">
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="旧密码">
              <input
                type="password"
                value={oldPwd}
                onChange={(e) => setOldPwd(e.target.value)}
                autoComplete="current-password"
                className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid="old-password"
              />
            </Field>
            <Field label="新密码" hint="（≥8 位，含字母和数字）">
              <input
                type="password"
                value={newPwd}
                onChange={(e) => setNewPwd(e.target.value)}
                autoComplete="new-password"
                className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid="new-password"
              />
            </Field>
            <Field label="确认新密码">
              <input
                type="password"
                value={confirmPwd}
                onChange={(e) => setConfirmPwd(e.target.value)}
                autoComplete="new-password"
                className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
                data-testid="confirm-password"
              />
            </Field>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={submitPassword}
              disabled={pwdBusy || !oldPwd || !newPwd}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
              data-testid="change-password"
            >
              {pwdBusy ? "提交中…" : "修改密码"}
            </button>
            <button
              type="button"
              onClick={revokeAll}
              className="rounded-md border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50"
            >
              吊销全部会话
            </button>
          </div>

          <div className="mt-4 border-t border-slate-100 pt-3">
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              当前会话
            </div>
            {sessions.length === 0 ? (
              <p className="text-xs text-slate-400">没有可显示的会话（服务令牌没有会话记录）。</p>
            ) : (
              <ul className="space-y-1 text-xs text-slate-600">
                {sessions.map((row) => (
                  <li key={`${row.created_at}-${row.expires_at}`} className="font-mono">
                    创建 {row.created_at} · 到期 {row.expires_at}
                    {row.last_used_at ? ` · 最近使用 ${row.last_used_at}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>

        {canManage ? (
          <>
        <Card
          title="审批人邀请码"
          desc="别人注册「审批人」时必须填写这个码；工程师注册不需要。"
        >
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <code
              className="rounded bg-slate-100 px-2 py-1 font-mono text-sm tracking-widest text-slate-800"
              data-testid="invite-code"
            >
              {invite?.invite_code ?? "—"}
            </code>
            <span className="text-slate-400">
              来源：{invite?.source === "env" ? "环境变量" : "data/approver_invite.txt"}
            </span>
            <button
              type="button"
              onClick={rotateApproverInvite}
              className="rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
              data-testid="invite-rotate"
            >
              轮换邀请码
            </button>
          </div>
          <p className="mt-1.5 text-[11px] text-slate-400">
            轮换后旧码立即失效。生产环境建议用环境变量 <code>APPROVER_INVITE_CODE</code> 控制
            （此时页面不能轮换）。首次启动的邀请码会打印在后端控制台。
          </p>
          {inviteError ? (
            <p className="mt-1 text-xs text-rose-600" data-testid="invite-error">
              {inviteError}
            </p>
          ) : null}
        </Card>

        <Card
          title="负责人表"
          desc="派单时的可选名单。绑定了账号的人会收到站内通知，并在「我的待办」里看到这张工单。"
        >
          {staffList.length === 0 ? (
            <p className="text-xs text-slate-400">还没有负责人，先在下面添加一条。</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="text-[11px] uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="py-1 font-medium">姓名</th>
                  <th className="py-1 font-medium">班组</th>
                  <th className="py-1 font-medium">电话</th>
                  <th className="py-1 font-medium">绑定账号</th>
                  <th className="py-1 font-medium">状态</th>
                  <th className="py-1 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {staffList.map((person) => (
                  <tr key={person.id} className="border-t border-slate-100">
                    <td className="py-1.5 font-medium text-slate-800">{person.name}</td>
                    <td className="py-1.5 text-slate-600">{person.team ?? "—"}</td>
                    <td className="py-1.5 text-slate-600">{person.phone ?? "—"}</td>
                    <td className="py-1.5 text-slate-600">
                      {person.username ? (
                        <span className="font-mono" data-testid="staff-account">
                          {person.username}
                        </span>
                      ) : (
                        <span className="text-amber-600">未绑定</span>
                      )}
                    </td>
                    <td className="py-1.5">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] ${
                          person.active
                            ? "border-emerald-300 bg-emerald-50 text-emerald-700"
                            : "border-slate-300 bg-slate-50 text-slate-400"
                        }`}
                      >
                        {person.active ? "在用" : "已停用"}
                      </span>
                      {person.on_duty ? (
                        <span
                          className="ml-1 rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800"
                          data-testid="staff-on-duty"
                        >
                          当班
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1.5">
                      <div className="flex flex-wrap gap-1">
                        <button
                          type="button"
                          onClick={() => toggleDuty(person)}
                          className="rounded border border-amber-300 px-1.5 py-0.5 text-[10px] text-amber-800 hover:bg-amber-50"
                          data-testid="staff-toggle-duty"
                        >
                          {person.on_duty ? "取消当班" : "设为当班"}
                        </button>
                        {person.active ? (
                          <button
                            type="button"
                            onClick={() => removeStaff(person)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100"
                            data-testid="staff-disable"
                          >
                            停用
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_1fr_1fr_auto]">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              maxLength={20}
              placeholder="姓名 *"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="staff-name"
            />
            <input
              value={newTeam}
              onChange={(e) => setNewTeam(e.target.value)}
              maxLength={40}
              placeholder="班组"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
            />
            <input
              value={newPhone}
              onChange={(e) => setNewPhone(e.target.value)}
              maxLength={40}
              placeholder="电话"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
            />
            <input
              value={newAccount}
              onChange={(e) => setNewAccount(e.target.value)}
              maxLength={32}
              placeholder="绑定账号（可选）"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="staff-account-input"
            />
            <button
              type="button"
              onClick={addStaff}
              disabled={staffBusy}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
              data-testid="staff-add"
            >
              {staffBusy ? "添加中…" : "添加"}
            </button>
          </div>
          <p className="mt-1.5 text-[11px] text-slate-400">
            绑定账号必须是<strong>已注册</strong>的用户名，否则收不到站内通知；不填也能派单，
            只是通知会回到派单人（提醒线下通知）。
          </p>
          {staffError ? (
            <p className="mt-1 text-xs text-rose-600" data-testid="staff-error">
              {staffError}
            </p>
          ) : null}
        </Card>

        <Card
          title="排班表"
          desc="按时间自动置「当班」；SLA 超时的通知会发给当班的人。开始时间晚于结束时间表示跨夜班。"
        >
          <div className="mb-2 flex flex-wrap items-center gap-3 text-[11px] text-slate-600">
            <span>本地时间：{shiftInfo?.now ?? "—"}</span>
            <span data-testid="on-duty-list">
              当前当班：
              {shiftInfo?.on_duty && shiftInfo.on_duty.length > 0
                ? shiftInfo.on_duty.join("、")
                : "（无）"}
            </span>
            <button
              type="button"
              onClick={runDutySync}
              className="rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
              data-testid="shift-sync"
            >
              立即同步当班
            </button>
          </div>

          {(shiftInfo?.shifts.length ?? 0) === 0 ? (
            <p className="text-xs text-slate-400">还没有班次，先在下面添加一条。</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="text-[11px] uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="py-1 font-medium">班次</th>
                  <th className="py-1 font-medium">负责人</th>
                  <th className="py-1 font-medium">时段</th>
                  <th className="py-1 font-medium">星期</th>
                  <th className="py-1 font-medium">当前</th>
                  <th className="py-1 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {(shiftInfo?.shifts ?? []).map((shift) => (
                  <tr key={shift.id} className="border-t border-slate-100">
                    <td className="py-1.5 font-medium text-slate-800">{shift.name}</td>
                    <td className="py-1.5 text-slate-600">
                      {shift.staff_name ?? `#${shift.staff_id}`}
                      {shift.team ? `（${shift.team}）` : ""}
                    </td>
                    <td className="py-1.5 font-mono text-slate-600">
                      {shift.start_time}–{shift.end_time}
                    </td>
                    <td className="py-1.5 font-mono text-slate-500">{shift.weekdays}</td>
                    <td className="py-1.5">
                      {shift.current ? (
                        <span
                          className="rounded border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700"
                          data-testid="shift-current"
                        >
                          在班
                        </span>
                      ) : (
                        <span className="text-[10px] text-slate-400">—</span>
                      )}
                    </td>
                    <td className="py-1.5">
                      <button
                        type="button"
                        onClick={() => removeShift(shift.id)}
                        className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_90px_90px_1fr_auto]">
            <input
              value={shiftName}
              onChange={(e) => setShiftName(e.target.value)}
              maxLength={20}
              placeholder="班次名"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="shift-name"
            />
            <select
              value={shiftStaffId}
              onChange={(e) => setShiftStaffId(e.target.value)}
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="shift-staff"
            >
              <option value="">— 负责人 —</option>
              {staffList
                .filter((person) => person.active)
                .map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.name}
                    {person.team ? `（${person.team}）` : ""}
                  </option>
                ))}
            </select>
            <input
              value={shiftStart}
              onChange={(e) => setShiftStart(e.target.value)}
              placeholder="08:00"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="shift-start"
            />
            <input
              value={shiftEnd}
              onChange={(e) => setShiftEnd(e.target.value)}
              placeholder="16:00"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="shift-end"
            />
            <input
              value={shiftWeekdays}
              onChange={(e) => setShiftWeekdays(e.target.value)}
              placeholder="1,2,3,4,5"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid="shift-weekdays"
            />
            <button
              type="button"
              onClick={addShift}
              disabled={shiftBusy}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
              data-testid="shift-add"
            >
              {shiftBusy ? "添加中…" : "添加"}
            </button>
          </div>
          <p className="mt-1.5 text-[11px] text-slate-400">
            星期用 1–7（1=周一，7=周日）；只填 1,2,3,4,5 = 工作日。跨夜班示例：20:00 → 08:00。
            出现在排班表里的人，其「当班」状态由排班自动决定；没排班的人保留手工设置。
          </p>
          {shiftError ? (
            <p className="mt-1 text-xs text-rose-600" data-testid="shift-error">
              {shiftError}
            </p>
          ) : null}
        </Card>

        <Card
          title="处置质量（最近 7 天驳回）"
          desc="驳回原因是结构化枚举——这里就是「哪条规则在误报」的数据出口。"
        >
          {!stats || stats.total === 0 ? (
            <p className="text-xs text-slate-400">最近 7 天没有驳回记录。</p>
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                {stats.by_reason.map((row) => (
                  <span
                    key={row.reason_code}
                    className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-700"
                  >
                    {row.label}
                    <span className="ml-1 font-mono font-semibold">{row.count}</span>
                  </span>
                ))}
              </div>
              <table className="w-full text-left text-xs">
                <thead className="text-[11px] uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="py-1 font-medium">规则</th>
                    <th className="py-1 font-medium">驳回原因</th>
                    <th className="py-1 font-medium">次数</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.by_rule.map((row) => (
                    <tr key={`${row.rule_id}-${row.reason_code}`} className="border-t border-slate-100">
                      <td className="py-1 font-mono">{row.rule_id}</td>
                      <td className="py-1">{reasons[row.reason_code] ?? row.reason_code}</td>
                      <td className="py-1 font-mono">{row.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
          </>
        ) : (
          <Card title="管理功能" desc="负责人表、排班表、处置质量统计仅对审批人开放。">
            <p className="text-xs text-slate-500">
              你是工程师账号：可以在这里改密码和个人偏好；派单与值班由审批人维护。
            </p>
          </Card>
        )}

        <p className="pb-6 text-[11px] text-slate-400">
          提示：复测窗口只影响关闭工单时的验证范围；阈值仍由 <code>config/rules.json</code> 决定，
          页面里改不了——避免有人在界面上把报警阈值调松。
        </p>
      </main>
    </div>
  );
}
