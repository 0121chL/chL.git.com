"""规则引擎（确定性）：订阅 `line1/telemetry`，按阈值/趋势产出事件对象。

    python rule_engine.py                    # 订阅 MQTT，规则命中后写 events.jsonl 并 POST 后端
    python rule_engine.py --cooldown 20      # 同一规则 20 秒内只触发一次

规则是纯阈值/趋势判断，不涉及任何模型调用；产出的事件对象是 Agent 的唯一输入。
"""

from __future__ import annotations

import argparse
import json
import sys
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

ROOT = Path(__file__).resolve().parent
EVENTS_FILE = ROOT / "events.jsonl"
TELEMETRY_TOPIC = "line1/telemetry"

# 规则表：阈值 + 等级
THRESHOLDS = {
    "ch4": {"value": 1.0, "unit": "%", "rule_id": "R-CH4-HIGH", "severity": "high"},
    "temp": {"value": 85.0, "unit": "C", "rule_id": "R-TEMP-HIGH", "severity": "high"},
    "vibration": {"value": 4.5, "unit": "mm/s", "rule_id": "R-VIB-HIGH", "severity": "medium"},
}

WINDOW = 90          # 秒，趋势窗口
HISTORY = 180        # 每个测点保留的采样点数
DEFAULT_DEVICE = "line1-dev-01"
last_fired: dict[str, float] = defaultdict(float)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_event(event: dict[str, Any]) -> None:
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def post_event(event: dict[str, Any], backend: str) -> bool:
    req = urllib.request.Request(
        f"{backend}/events",
        data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")[:160]
            print(f"[rule] POST /events -> {resp.status} {body}")
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[rule] POST /events 失败（后端没起？）：{exc}")
        return False


def make_event(
    device_id: str,
    sensor: str,
    value: float,
    spec: dict[str, Any],
    history: dict[str, deque],
    event_type: str,
    severity: str | None = None,
) -> dict[str, Any]:
    unit = spec["unit"]
    points = list(history[f"{device_id}:{sensor}"])[-WINDOW:]
    values = [p[1] for p in points] or [value]
    sensors = {}
    for key, dq in history.items():
        if not dq or not key.startswith(f"{device_id}:"):
            continue
        name = key.split(":", 1)[1]
        recent = list(dq)
        latest = recent[-1][1]
        cutoff = time.time() - 60
        one_min_ago = next((v for t, v in recent if t >= cutoff), latest)
        sensors[name] = {
            "value": latest,
            "unit": {"ch4": "%", "temp": "C", "vibration": "mm/s", "co": "ppm", "airflow": "m3/s"}.get(name, ""),
            "delta_1m": round(latest - one_min_ago, 2),
        }

    return {
        "event_id": f"evt-{spec['rule_id'].lower()}-{device_id}-{int(time.time())}-{int(value * 100) % 1000:03d}",
        "schema_version": 1,
        "line": "line1",
        "device_id": device_id,
        "sensor": sensor,
        "rule_id": spec["rule_id"],
        "event_type": event_type,
        "severity": severity or spec["severity"],
        "triggered_at": now_iso(),
        "observed": {"value": value, "unit": unit, "threshold": spec["value"]},
        "window": {
            "samples": len(values),
            "mean": round(sum(values) / len(values), 2),
            "max": max(values),
            "slope_per_min": round((values[-1] - values[0]) / max(1, len(values) - 1) * 60, 2),
        },
        "snapshot": {"sensors": sensors},
        "scenario_id": "live_rule_engine",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性规则引擎")
    ap.add_argument("--mqtt", default="127.0.0.1:1883")
    ap.add_argument("--backend", default="http://127.0.0.1:8000")
    ap.add_argument("--cooldown", type=float, default=30.0)
    ap.add_argument("--trend-window", type=float, default=60.0, help="趋势规则的观察窗口（秒）")
    ap.add_argument("--merge-window", type=float, default=10.0, help="同传感器事件合并窗口（秒）")
    args = ap.parse_args()

    host, _, port = args.mqtt.partition(":")
    history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY))
    last_emitted: dict[str, float] = defaultdict(float)  # 合并窗用：每设备+传感器上次生效事件

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

        now = time.time()
        key = f"{device}:{sensor}"
        history[key].append((now, value))
        spec = THRESHOLDS.get(sensor)
        if not spec:
            return

        # 1) 阈值规则优先（同一读数只产出一条事件）
        cooldown_key = f"{device}:{spec['rule_id']}"
        if value > spec["value"] and now - last_fired[cooldown_key] > args.cooldown:
            last_fired[cooldown_key] = now
            fire(make_event(device, sensor, value, spec, history, "threshold_exceeded"), key)
            return

        # 2) 趋势规则：必须真的有窗口长度的历史，避免用首样本当基线造成早报
        if sensor == "ch4":
            points = list(history[key])
            span = points[-1][0] - points[0][0] if points else 0.0
            if span >= args.trend_window:
                older = [p for p in points if p[0] <= now - args.trend_window]
                if older:
                    delta = value - older[-1][1]
                    rise_key = f"{device}:R-CH4-RISE"
                    if delta >= 0.5 and now - last_fired[rise_key] > args.cooldown:
                        last_fired[rise_key] = now
                        spec_rise = dict(spec, rule_id="R-CH4-RISE", severity="high")
                        fire(make_event(device, sensor, value, spec_rise, history, "gas_surge"), key)

    def fire(event: dict[str, Any], key: str) -> None:
        now = time.time()
        gap = now - last_emitted[key]
        if gap < args.merge_window:
            print(
                f"[rule] 合并：{event['rule_id']} 与 {gap:.1f}s 前的同测点事件同源，抑制重复事件"
            )
            return
        last_emitted[key] = now
        print(
            f"[rule] 触发 {event['rule_id']} {event.get('device_id')} {event['sensor']}="
            f"{event['observed']['value']}{event['observed']['unit']} -> {event['event_id']}"
        )
        append_event(event)
        post_event(event, args.backend)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="rule-engine")
    client.on_message = on_message
    client.connect(host, int(port or 1883), keepalive=30)
    client.subscribe(TELEMETRY_TOPIC, qos=0)
    client.loop_start()
    print(f"[rule] 订阅 {TELEMETRY_TOPIC} @ {args.mqtt}；事件写入 {EVENTS_FILE}；后端 {args.backend}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[rule] 退出")
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
