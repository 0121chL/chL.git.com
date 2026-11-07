#!/usr/bin/env python3
"""规则引擎（配置驱动）：订阅遥测 → 阈值/趋势判定 → 事件对象 → events.jsonl + POST 后端。

相比早期版本的三处硬化：
1. 规则全部来自 `config/rules.json`，换传感器只改配置；
2. 每条遥测落 SQLite `telemetry` 表（按保留时长清理），可回放；
3. POST 失败的事件进 `data/spool/` 本地队列，后台线程定时重投，不再静默丢事件。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import paho.mqtt.client as mqtt

import db

ROOT = Path(__file__).resolve().parent
DEFAULT_RULES = ROOT / "config" / "rules.json"
EVENTS_FILE = ROOT / "events.jsonl"
TELEMETRY_TOPIC = "line1/telemetry"
HISTORY = 180
WINDOW = 90
DEFAULT_DEVICE = "line1-dev-01"

UNIT_BY_SENSOR = {
    "ch4": "%",
    "temp": "C",
    "vibration": "mm/s",
    "co": "ppm",
    "airflow": "m3/s",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_rules(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, default in (
        ("backend", "http://127.0.0.1:8000"),
        ("cooldown_s", 30),
        ("trend_window_s", 60),
        ("merge_window_s", 10),
    ):
        data.setdefault(key, default)
    data.setdefault("sensors", {})
    data.setdefault("telemetry", {"persist": True, "retention_hours": 24})
    data.setdefault("spool", {"dir": "data/spool", "retry_interval_s": 10})
    return data


def append_event(event: dict[str, Any]) -> None:
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def post_event(backend: str, event: dict[str, Any]) -> bool:
    req = urllib.request.Request(
        f"{backend}/events",
        data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")[:120]
            print(f"[rule] POST /events -> {resp.status} {body}", flush=True)
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[rule] POST /events 失败：{exc}", flush=True)
        return False


class Spooler:
    """POST 失败的事件先落盘，后台重投；后端恢复后自动补齐。"""

    def __init__(self, directory: Path, backend: str, interval: float = 10.0) -> None:
        self.dir = directory
        self.backend = backend
        self.interval = interval
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self.pending = 0

    def save(self, event: dict[str, Any]) -> None:
        path = self.dir / f"{event['event_id']}.json"
        path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        self.pending += 1
        print(f"[spool] 已暂存 {path.name}（待重投 {self.pending}）", flush=True)

    def flush_once(self) -> int:
        sent = 0
        for path in sorted(self.dir.glob("*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            if post_event(self.backend, event):
                path.unlink(missing_ok=True)
                sent += 1
        self.pending = len(list(self.dir.glob("*.json")))
        if sent:
            print(f"[spool] 重投成功 {sent} 条，剩余 {self.pending}", flush=True)
        return sent

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    self.flush_once()
                except Exception as exc:  # noqa: BLE001
                    print(f"[spool] 重投异常：{exc}", flush=True)

        self.flush_once()
        threading.Thread(target=loop, name="spool-replayer", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性规则引擎（配置驱动）")
    ap.add_argument("--rules", default=str(DEFAULT_RULES))
    ap.add_argument("--mqtt", default="127.0.0.1:1883")
    ap.add_argument("--backend", default=None)
    ap.add_argument("--cooldown", type=float, default=None)
    ap.add_argument("--trend-window", type=float, default=None)
    ap.add_argument("--merge-window", type=float, default=None)
    args = ap.parse_args()

    rules = load_rules(Path(args.rules))
    backend = args.backend or rules["backend"]
    cooldown = args.cooldown if args.cooldown is not None else float(rules["cooldown_s"])
    trend_window = args.trend_window if args.trend_window is not None else float(rules["trend_window_s"])
    merge_window = args.merge_window if args.merge_window is not None else float(rules["merge_window_s"])
    persist = bool(rules["telemetry"].get("persist", True))
    retention = float(rules["telemetry"].get("retention_hours", 24))

    db.init_db(db.DEFAULT_DB_PATH)
    spool = Spooler(ROOT / rules["spool"]["dir"], backend, float(rules["spool"]["retry_interval_s"]))
    spool.start()

    host, _, port = args.mqtt.partition(":")
    history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY))
    last_fired: dict[str, float] = defaultdict(float)
    last_emitted: dict[str, float] = defaultdict(float)

    def make_event(device: str, sensor: str, value: float, spec: dict[str, Any],
                   event_type: str, severity: str | None = None, rule_id: str | None = None) -> dict[str, Any]:
        key = f"{device}:{sensor}"
        points = list(history[key])[-WINDOW:]
        values = [p[1] for p in points] or [value]
        sensors = {}
        for k, dq in history.items():
            if not dq or not k.startswith(f"{device}:"):
                continue
            name = k.split(":", 1)[1]
            recent = list(dq)
            latest = recent[-1][1]
            cutoff = time.time() - 60
            one_min_ago = next((v for t, v in recent if t >= cutoff), latest)
            sensors[name] = {
                "value": latest,
                "unit": UNIT_BY_SENSOR.get(name, ""),
                "delta_1m": round(latest - one_min_ago, 2),
            }
        return {
            "event_id": f"evt-{(rule_id or spec['rule_id']).lower()}-{device}-{int(time.time())}-{int(value * 100) % 1000:03d}",
            "schema_version": 1,
            "line": "line1",
            "device_id": device,
            "sensor": sensor,
            "rule_id": rule_id or spec["rule_id"],
            "event_type": event_type,
            "severity": severity or spec.get("severity", "high"),
            "triggered_at": now_iso(),
            "observed": {"value": value, "unit": spec.get("unit", ""), "threshold": spec["threshold"]},
            "window": {
                "samples": len(values),
                "mean": round(sum(values) / len(values), 2),
                "max": max(values),
                "slope_per_min": round((values[-1] - values[0]) / max(1, len(values) - 1) * 60, 2),
            },
            "snapshot": {"sensors": sensors},
            "scenario_id": "live_rule_engine",
        }

    def fire(event: dict[str, Any], key: str) -> None:
        now = time.time()
        gap = now - last_emitted[key]
        if gap < merge_window:
            print(f"[rule] 合并：{event['rule_id']} 与 {gap:.1f}s 前的同测点事件同源，抑制", flush=True)
            return
        last_emitted[key] = now
        print(
            f"[rule] 触发 {event['rule_id']} {event.get('device_id')} {event['sensor']}="
            f"{event['observed']['value']}{event['observed']['unit']} -> {event['event_id']}",
            flush=True,
        )
        append_event(event)
        if not post_event(backend, event):
            spool.save(event)

    def on_message(client, userdata, msg):  # noqa: ANN001
        try:
            reading = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        device = str(reading.get("device_id") or DEFAULT_DEVICE)
        sensor = reading.get("sensor")
        value = reading.get("value")
        if sensor is None or value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return

        ts = now_iso()
        if persist:
            try:
                db.insert_telemetry(db.DEFAULT_DB_PATH, ts, device, sensor, value,
                                    reading.get("unit"), reading.get("scenario_id"))
            except Exception as exc:  # noqa: BLE001 - 落库失败不能拖垮判定
                print(f"[rule] telemetry 落库失败：{exc}", flush=True)

        spec = rules["sensors"].get(sensor)
        if not spec:
            return

        now = time.time()
        key = f"{device}:{sensor}"
        history[key].append((now, value))

        cooldown_key = f"{device}:{spec['rule_id']}"
        if value > float(spec["threshold"]) and now - last_fired[cooldown_key] > cooldown:
            last_fired[cooldown_key] = now
            fire(make_event(device, sensor, value, spec, spec.get("event_type", "threshold_exceeded")), key)
            return

        trend = spec.get("trend") or {}
        if trend.get("enabled"):
            points = list(history[key])
            span = points[-1][0] - points[0][0] if points else 0.0
            if span >= trend_window:
                older = [p for p in points if p[0] <= now - trend_window]
                if older:
                    delta = value - older[-1][1]
                    rise_key = f"{device}:{trend['rule_id']}"
                    if delta >= float(trend["delta"]) and now - last_fired[rise_key] > cooldown:
                        last_fired[rise_key] = now
                        fire(
                            make_event(
                                device, sensor, value, spec,
                                trend.get("event_type", "gas_surge"),
                                trend.get("severity", "high"),
                                trend["rule_id"],
                            ),
                            key,
                        )

    def housekeeping() -> None:
        while True:
            time.sleep(3600)
            try:
                removed = db.purge_telemetry(db.DEFAULT_DB_PATH, retention)
                if removed:
                    print(f"[rule] 清理过期遥测 {removed} 行", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[rule] 遥测清理失败：{exc}", flush=True)

    db.purge_telemetry(db.DEFAULT_DB_PATH, retention)
    threading.Thread(target=housekeeping, name="telemetry-housekeeping", daemon=True).start()

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="rule-engine")
    client.on_message = on_message
    client.connect(host, int(port or 1883), keepalive=30)
    client.subscribe(TELEMETRY_TOPIC, qos=0)
    client.loop_start()
    print(
        f"[rule] 规则={args.rules}；订阅 {TELEMETRY_TOPIC} @ {args.mqtt}；遥测落库={'on' if persist else 'off'}；"
        f"后端 {backend}；cooldown={cooldown}s trend={trend_window}s merge={merge_window}s",
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[rule] 退出")
    finally:
        spool.stop()
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
