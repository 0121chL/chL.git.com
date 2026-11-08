"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { login, register } from "@/lib/api";
import { clearSession, getToken, saveSession } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (getToken()) router.replace("/");
  }, [router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!username.trim() || !password) {
      setError("请输入用户名和密码");
      return;
    }
    if (mode === "register") {
      if (password.length < 6) {
        setError("密码至少 6 位");
        return;
      }
      if (password !== confirm) {
        setError("两次输入的密码不一致");
        return;
      }
    }

    setBusy(true);
    try {
      const result =
        mode === "login"
          ? await login(username.trim(), password)
          : await register(username.trim(), password);
      saveSession(result.token, result.user);
      router.replace("/");
      router.refresh();
    } catch (err) {
      clearSession();
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-100 px-4">
      <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-lg font-semibold tracking-tight">安全监测 Agent 看板</h1>
        <p className="mt-1 text-xs text-slate-500">登录后才能查看事件、审批工单</p>

        <div className="mt-5 grid grid-cols-2 gap-1 rounded-lg bg-slate-100 p-1 text-sm">
          {(["login", "register"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => {
                setMode(m);
                setError(null);
              }}
              className={`rounded-md px-3 py-1.5 font-medium transition-colors ${
                mode === m ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
              }`}
              data-testid={`tab-${m}`}
            >
              {m === "login" ? "登录" : "注册"}
            </button>
          ))}
        </div>

        <form className="mt-4 space-y-3" onSubmit={submit}>
          <label className="block text-xs text-slate-500">
            用户名
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              placeholder="3–32 位，字母/数字/汉字"
              className="mt-1 w-full rounded border border-slate-300 px-2 py-2 text-sm text-slate-900 outline-none focus:border-slate-500"
              data-testid="username"
            />
          </label>

          <label className="block text-xs text-slate-500">
            密码
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              placeholder={mode === "register" ? "至少 6 位" : ""}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-2 text-sm text-slate-900 outline-none focus:border-slate-500"
              data-testid="password"
            />
          </label>

          {mode === "register" ? (
            <label className="block text-xs text-slate-500">
              确认密码
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                className="mt-1 w-full rounded border border-slate-300 px-2 py-2 text-sm text-slate-900 outline-none focus:border-slate-500"
                data-testid="confirm"
              />
            </label>
          ) : null}

          {error ? (
            <p className="rounded border border-rose-200 bg-rose-50 px-2 py-1.5 text-xs text-rose-700" data-testid="error">
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
            data-testid="submit"
          >
            {busy ? "提交中…" : mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>

        <p className="mt-4 text-center text-[11px] text-slate-400">
          本地演示账号：首次使用请先注册（数据存在 data/monitor.db）
        </p>
      </div>
    </main>
  );
}
