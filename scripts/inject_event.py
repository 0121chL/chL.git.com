#!/usr/bin/env python3
"""演示触发：默认走**真实链路**——发布遥测 → 规则引擎判定 → 事件。

    python scripts/inject_event.py --scenario gas_surge     # 走遥测（推荐）
    python scripts/inject_event.py --scenario gas_surge --direct   # 直投事件，标记为「手工注入」

默认模式只是把场景翻译成 `sensor_simulator --inject`，事件由规则引擎按阈值/趋势
真实产出；`--direct` 是把一条事件直接塞进后端，仅用于联调，事件带 `origin=manual`，
前端会显示「手工注入」徽标，避免和规则引擎产出的事件混淆。
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
TOPIC = "line1/events"
BACKEND = "http://127.0.0.1:8000"

# 场景 → 模拟器脚本（遥测注入）
SIMULATOR_SCENARIO = {
    "gas_surge": "ch4_rise",
    "temp_high": "temp_spike",
    "vib_warn": "vib_spike",
}

# 仅 --direct 时使用的预置事件
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
        "rule_id": "R-VIB-HIGH",
        "event_type": "threshold_exceeded",
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
    event_id = f"evt-manual-{scenario}-{int(time.time())}-{random.randint(1000, 9999)}"
    return {
        "event_id": event_id,
        "schema_version": 1,
        "line": "line1",
        "origin": "manual",
        "sensor": spec["sensor"],
        "device_id": "line1-dev-01",
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


def run_simulator(scene: str, device: str) -> int:
    print(f"[inject] 走遥测链路：sensor_simulator --inject {scene}——事件将由规则引擎产出")
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        str(ROOT / "sensor_simulator.py"),
        "--inject",
        scene,
        "--device",
        device,
    ]
    return subprocess.call(cmd, cwd=str(ROOT))


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
    ap = argparse.ArgumentParser(description="演示触发")
    ap.add_argument("--scenario", default="gas_surge", choices=sorted(SCENARIOS))
    ap.add_argument("--device", default="line1-dev-01")
    ap.add_argument("--mqtt", default="127.0.0.1:1883", help="host:port")
    ap.add_argument("--backend", default=BACKEND)
    ap.add_argument("--direct", action="store_true",
                    help="跳过遥测/规则引擎，直接投一条事件（标 origin=manual）")
    ap.add_argument("--http", action="store_true", help="--direct 时强制走 HTTP")
    args = ap.parse_args()

    if not args.direct:
        return run_simulator(SIMULATOR_SCENARIO[args.scenario], args.device)

    event = build_event(args.scenario)
    print(f"[inject] 直投事件 {event['event_id']}（origin=manual，前端会标「手工注入」）")
    print(json.dumps(event, ensure_ascii=False, indent=2))

    if not args.http:
        host, _, port = args.mqtt.partition(":")
        if publish_mqtt(event, host, int(port or 1883)):
            print(f"[inject] 已发布到 {TOPIC} @ {args.mqtt}")
            return 0

    if publish_http(event, args.backend):
        return 0
    print("[inject] MQTT 与 HTTP 都不可用：请先启动 mosquitto 或 backend_server.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
