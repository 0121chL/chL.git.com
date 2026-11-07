"use client";

import { useEffect, useState } from "react";

import { fetchTelemetry } from "@/lib/api";
import type { TelemetryPoint } from "@/lib/types";

const W = 100;
const H = 34;

export function TelemetryChart({
  deviceId,
  sensor,
  unit,
}: {
  deviceId?: string;
  sensor?: string;
  unit?: string;
}) {
  const [points, setPoints] = useState<TelemetryPoint[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!deviceId || !sensor) return;
    let alive = true;
    const load = async () => {
      try {
        const data = await fetchTelemetry(deviceId, sensor, 30);
        if (alive) {
          setPoints(data);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    setLoading(true);
    void load();
    const timer = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [deviceId, sensor]);

  if (!deviceId || !sensor) {
    return <p className="text-xs text-slate-400">该事件未标注设备/传感器，没有可画的遥测。</p>;
  }
  if (error) {
    return <p className="text-xs text-rose-600">遥测加载失败：{error}</p>;
  }

  const values = points
    .map((p) => p.value)
    .filter((v): v is number => typeof v === "number");
  if (values.length < 2) {
    return (
      <p className="text-xs text-slate-400">
        {loading ? "加载中…" : "最近 30 分钟没有遥测（规则引擎需运行并落库）"}
      </p>
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = W / (values.length - 1);
  const line = values
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(2)},${(H - ((v - min) / span) * H).toFixed(2)}`)
    .join(" ");
  const last = values[values.length - 1];

  return (
    <div>
      <div className="flex items-baseline justify-between text-[11px] text-slate-500">
        <span>
          最近 30 分钟 · {sensor} · {deviceId}
        </span>
        <span>{values.length} 点</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="mt-1 h-14 w-full rounded bg-slate-50 ring-1 ring-slate-200"
        role="img"
        aria-label="遥测趋势"
      >
        <path d={line} fill="none" stroke="#0f172a" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between font-mono text-[11px] text-slate-600">
        <span>最低 {min.toFixed(2)}{unit ?? ""}</span>
        <span>当前 {last.toFixed(2)}{unit ?? ""}</span>
        <span>最高 {max.toFixed(2)}{unit ?? ""}</span>
      </div>
    </div>
  );
}
