#!/usr/bin/env python3
"""演示事件注入：优先走 MQTT（line1/events），broker 不在时回退到后端 HTTP 注入。

    python scripts/inject_event.py --scenario gas_surge
    python scripts/inject_event.py --scenario temp_high --mqtt 127.0.0.1:1883
    python scripts/inject_event.py --scenario gas_surge --http      # 强制 HTTP
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TOPIC = "line1/events"
BACKEND = "http://127.0.0.1:8000"

SCENARIOS: dict[str, dict] = {
    "gas_surge": {
        "rule_id": "R-CH4-RISE",
        "event_type": "gas_surge",
        "sensor": "ch4",
        "severity": "high",
        "observed": {"value": 1.8, "unit": "%", "threshold": 1.0},
        "window": {"samples": 60, "mean": 0.9, "max": 1.8, "slope_per_min": 0.65},
        "sensors": {
            "ch4": {"value": 1.8, "unit": "%", "delta_1m": 0.62},
            "temp": {"value": 58.3, "unit": "C", "delta_1m": 7.5},
            "co": {"value": 8, "unit": "ppm", "delta_1m": 0.2},
            "airflow": {"value": 3.1, "unit": "m3/s", "delta_1m": -0.9},
        },
    },
    "temp_high": {
        "rule_id": "R-TEMP-HIGH",
        "event_type": "threshold_exceeded",
        "sensor": "temp",
        "severity": "high",
        "observed": {"value": 93.5, "unit": "C", "threshold": 85.0},
        "window": {"samples": 30, "mean": 71.4, "max": 93.5, "slope_per_min": 1.8},
        "sensors": {
            "temp": {"value": 93.5, "unit": "C", "delta_1m": 6.2},
            "vibration": {"value": 4.8, "unit": "mm/s", "delta_1m": 0.6},
            "ch4": {"value": 0.3, "unit": "%", "delta_1m": 0.0},
        },
    },
    "vib_warn": {
        "rule_id": "R-VIB-TREND",
        "event_type": "trend_warning",
        "sensor": "vibration",
        "severity": "medium",
        "observed": {"value": 4.8, "unit": "mm/s", "threshold": 4.5},
        "window": {"samples": 60, "mean": 3.9, "max": 4.8, "slope_per_min": 0.6},
        "sensors": {
            "vibration": {"value": 4.8, "unit": "mm/s", "delta_1m": 0.6},
            "temp": {"value": 62.0, "unit": "C", "delta_1m": 1.1},
        },
    },
}


def build_event(scenario: str) -> dict:
    spec = SCENARIOS[scenario]
    now = datetime.now(timezone.utc).astimezone()
    event_id = f"evt-{scenario}-{int(time.time())}-{random.randint(1000, 9999)}"
    return {
        "event_id": event_id,
        "schema_version": 1,
        "line": "line1",
        "sensor": spec["sensor"],
        "rule_id": spec["rule_id"],
        "event_type": spec["event_type"],
        "severity": spec["severity"],
        "triggered_at": now.isoformat(timespec="seconds"),
        "observed": spec["observed"],
        "window": spec["window"],
        "snapshot": {"sensors": spec["sensors"]},
        "scenario_id": scenario,
        "tick": random.randint(1, 999),
    }


def publish_mqtt(event: dict, host: str, port: int) -> bool:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return False
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="injector")
    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()  # 必须有网络循环，否则收不到 QoS1 的 PUBACK
        info = client.publish(TOPIC, json.dumps(event, ensure_ascii=False), qos=1)
        info.wait_for_publish(timeout=5)
        published = info.is_published()
        client.loop_stop()
        client.disconnect()
        if not published:
            print(f"[inject] MQTT 发布未确认（{host}:{port}）")
        return published
    except Exception as exc:  # noqa: BLE001
        print(f"[inject] MQTT 发布失败（{host}:{port}）：{exc}")
        return False


def publish_http(event: dict, backend: str) -> bool:
    req = urllib.request.Request(
        f"{backend}/api/dev/inject",
        data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[inject] HTTP 注入成功：{resp.status} {resp.read().decode('utf-8')}")
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[inject] HTTP 注入失败（{backend}）：{exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="注入演示事件")
    ap.add_argument("--scenario", default="gas_surge", choices=sorted(SCENARIOS))
    ap.add_argument("--mqtt", default="127.0.0.1:1883", help="host:port")
    ap.add_argument("--backend", default=BACKEND)
    ap.add_argument("--http", action="store_true", help="跳过 MQTT，直接走 HTTP")
    args = ap.parse_args()

    event = build_event(args.scenario)
    print(f"[inject] 事件 {event['event_id']}  scenario={args.scenario}")
    print(json.dumps(event, ensure_ascii=False, indent=2))

    if not args.http:
        host, _, port = args.mqtt.partition(":")
        if publish_mqtt(event, host, int(port or 1883)):
            print(f"[inject] 已发布到 {TOPIC} @ {args.mqtt}")
            return 0

    if publish_http(event, args.backend):
        return 0
    print("[inject] MQTT 与 HTTP 都不可用：请先启动 mosquitto 或 server.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
