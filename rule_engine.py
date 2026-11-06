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
HISTORY = 180        # 每个传感器保留的采样点数
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
    sensor: str,
    value: float,
    spec: dict[str, Any],
    history: dict[str, deque],
    event_type: str,
    severity: str | None = None,
) -> dict[str, Any]:
    unit = spec["unit"]
    points = list(history[sensor])[-WINDOW:]
    values = [p[1] for p in points] or [value]
    sensors = {}
    for name, dq in history.items():
        if not dq:
            continue
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
        "event_id": f"evt-{spec['rule_id'].lower()}-{int(time.time())}-{int(value * 100) % 1000:03d}",
        "schema_version": 1,
        "line": "line1",
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
    args = ap.parse_args()

    host, _, port = args.mqtt.partition(":")
    history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY))

    def on_message(client, userdata, msg):  # noqa: ANN001
        try:
            reading = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        sensor = reading.get("sensor")
        value = reading.get("value")
        if sensor is None or value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return

        history[sensor].append((time.time(), value))
        spec = THRESHOLDS.get(sensor)
        if not spec:
            return

        # 1) 阈值规则
        if value > spec["value"] and time.time() - last_fired[spec["rule_id"]] > args.cooldown:
            last_fired[spec["rule_id"]] = time.time()
            event = make_event(sensor, value, spec, history, "threshold_exceeded")
            fire(event, args.backend)
            return

        # 2) 趋势规则：ch4 60 秒内上升 >= 0.5 个百分点
        if sensor == "ch4" and len(history["ch4"]) > 5:
            cutoff = time.time() - 60
            older = [(t, v) for t, v in history["ch4"] if t <= cutoff] or [list(history["ch4"])[0]]
            delta = value - older[-1][1]
            if delta >= 0.5 and time.time() - last_fired["R-CH4-RISE"] > args.cooldown:
                last_fired["R-CH4-RISE"] = time.time()
                spec_rise = dict(spec, rule_id="R-CH4-RISE", severity="high")
                event = make_event(sensor, value, spec_rise, history, "gas_surge")
                fire(event, args.backend)

    def fire(event: dict[str, Any], backend: str) -> None:
        print(
            f"[rule] 触发 {event['rule_id']} {event['sensor']}={event['observed']['value']}"
            f"{event['observed']['unit']} -> {event['event_id']}"
        )
        append_event(event)
        post_event(event, backend)

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
