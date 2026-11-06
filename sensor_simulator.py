"""模拟传感器：向 `line1/telemetry` 发布 1 Hz 读数。

    python sensor_simulator.py                    # 正常模式（基线，不触发规则）
    python sensor_simulator.py --inject ch4_rise  # 脚本化注入，十秒内触发 high 事件
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

TOPIC = "line1/telemetry"
BASELINE = {
    "temp": {"value": 68.0, "unit": "C", "noise": 1.2},
    "ch4": {"value": 0.30, "unit": "%", "noise": 0.03},
    "co": {"value": 8.0, "unit": "ppm", "noise": 1.0},
    "airflow": {"value": 3.6, "unit": "m3/s", "noise": 0.05},
}
SCENARIOS = {
    # 8 秒内 ch4 由 0.30 抬到 1.30，同时 temp 上升、airflow 下降
    "ch4_rise": {"duration": 10, "ch4_from": 0.30, "ch4_to": 1.30, "temp_rise": 8.0, "airflow_drop": 0.9},
    # 温度快速越过 85℃
    "temp_spike": {"duration": 8, "temp_from": 70.0, "temp_to": 94.0, "ch4_from": 0.3, "ch4_to": 0.35},
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def publish(client: mqtt.Client, sensor: str, value: float, unit: str, tick: int, scenario: str) -> None:
    payload = {
        "ts": now(),
        "line": "line1",
        "sensor": sensor,
        "value": round(value, 2),
        "unit": unit,
        "tick": tick,
        "scenario_id": scenario,
    }
    client.publish(TOPIC, json.dumps(payload, ensure_ascii=False), qos=0)


def run_scenario(client: mqtt.Client, name: str, seed: int) -> None:
    spec = SCENARIOS[name]
    rng = random.Random(seed)
    steps = int(spec["duration"])
    print(f"[sim] 注入场景 {name}：{steps}s，topic={TOPIC}")

    for tick in range(steps):
        ratio = tick / max(1, steps - 1)
        eased = ratio * ratio * (3 - 2 * ratio)  # smoothstep，避免瞬间跳变

        ch4 = spec.get("ch4_from", BASELINE["ch4"]["value"]) + (
            spec.get("ch4_to", 1.3) - spec.get("ch4_from", 0.3)
        ) * eased
        temp = spec.get("temp_from", BASELINE["temp"]["value"]) + (
            spec.get("temp_to", BASELINE["temp"]["value"]) - spec.get("temp_from", BASELINE["temp"]["value"])
        ) * eased
        airflow = BASELINE["airflow"]["value"] - spec.get("airflow_drop", 0.0) * eased

        publish(client, "ch4", max(0.01, ch4 + rng.uniform(-0.01, 0.01)), "%", tick, name)
        publish(client, "temp", temp + rng.uniform(-0.4, 0.4), "C", tick, name)
        publish(client, "co", BASELINE["co"]["value"] + rng.uniform(-0.8, 0.8), "ppm", tick, name)
        publish(client, "airflow", max(0.1, airflow + rng.uniform(-0.03, 0.03)), "m3/s", tick, name)

        if tick % 2 == 0:
            print(f"[sim] t={tick:02d}s ch4={ch4:.2f}% temp={temp:.1f}C airflow={airflow:.2f}")
        time.sleep(1.0)

    print("[sim] 场景结束")


def run_normal(client: mqtt.Client, seed: int) -> None:
    rng = random.Random(seed)
    tick = 0
    print(f"[sim] 正常模式，1 Hz 发布到 {TOPIC}（Ctrl+C 停止）")
    while True:
        wave = math.sin(tick / 30.0) * 0.6
        publish(client, "temp", BASELINE["temp"]["value"] + wave + rng.uniform(-1, 1), "C", tick, "normal")
        publish(client, "ch4", max(0.01, BASELINE["ch4"]["value"] + rng.uniform(-0.03, 0.03)), "%", tick, "normal")
        publish(client, "co", BASELINE["co"]["value"] + rng.uniform(-1, 1), "ppm", tick, "normal")
        publish(client, "airflow", BASELINE["airflow"]["value"] + rng.uniform(-0.05, 0.05), "m3/s", tick, "normal")
        tick += 1
        time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟传感器")
    ap.add_argument("--mqtt", default="127.0.0.1:1883")
    ap.add_argument("--inject", default=None, choices=sorted(SCENARIOS))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    host, _, port = args.mqtt.partition(":")
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="sensor-simulator")
    client.connect(host, int(port or 1883), keepalive=30)
    client.loop_start()

    try:
        if args.inject:
            run_scenario(client, args.inject, args.seed)
        else:
            run_normal(client, args.seed)
    except KeyboardInterrupt:
        print("\n[sim] 退出")
    finally:
        time.sleep(0.2)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
