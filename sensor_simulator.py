"""模拟传感器：向 `line1/telemetry` 发布 1 Hz 读数（基线 + 平滑注入 + 噪声）。

    python sensor_simulator.py                    # 正常模式（基线，不触发规则）
    python sensor_simulator.py --inject ch4_rise  # 瓦斯上升 + 温度同步上升 + 风量下降
    python sensor_simulator.py --inject temp_spike
    python sensor_simulator.py --inject vib_spike

注入用 smoothstep 过渡，并**同时改变多个参数**，这样规则引擎产出的
快照里 ch4/temp/airflow 的 delta 是真实算出来的，而不是手写的。
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
HEARTBEAT_TOPIC = "line1/heartbeat"

BASELINE = {
    "ch4": {"value": 0.30, "unit": "%", "noise": 0.03, "floor": 0.0},
    "temp": {"value": 68.0, "unit": "C", "noise": 1.2, "floor": 0.0},
    "co": {"value": 8.0, "unit": "ppm", "noise": 1.0, "floor": 0.0},
    "airflow": {"value": 3.6, "unit": "m3/s", "noise": 0.05, "floor": 0.1},
    "vibration": {"value": 3.6, "unit": "mm/s", "noise": 0.12, "floor": 0.0},
}

# delta = 场景结束时相对基线的变化量（按 smoothstep 逐步逼近）
SCENARIOS = {
    "ch4_rise": {
        "duration": 10,
        "delta": {"ch4": 1.0, "temp": 8.0, "airflow": -0.9},
        "desc": "ch4 0.30→1.30%，temp +8℃，风量 -0.9（局部瓦斯涌出 + 通风异常）",
    },
    "temp_spike": {
        "duration": 8,
        "delta": {"temp": 24.0, "ch4": 0.05},
        "desc": "temp 68→92℃（轴承温度超限）",
    },
    "vib_spike": {
        "duration": 8,
        "delta": {"vibration": 1.6, "temp": 3.0},
        "desc": "振动 3.6→5.2 mm/s（设备振动超限）",
    },
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def smoothstep(ratio: float) -> float:
    return ratio * ratio * (3 - 2 * ratio)


def publish(client: mqtt.Client, sensor: str, value: float, unit: str, tick: int, scenario: str,
            device: str) -> None:
    payload = {
        "ts": now(),
        "line": "line1",
        "device_id": device,
        "sensor": sensor,
        "value": round(value, 2),
        "unit": unit,
        "tick": tick,
        "scenario_id": scenario,
    }
    client.publish(TOPIC, json.dumps(payload, ensure_ascii=False), qos=0)


def heartbeat(client: mqtt.Client, device: str) -> None:
    payload = {"ts": now(), "device_id": device, "status": "online"}
    client.publish(HEARTBEAT_TOPIC, json.dumps(payload, ensure_ascii=False), qos=0)


def scenario_value(sensor: str, eased: float, delta: dict[str, float], rng: random.Random) -> float:
    """当前 tick 的读数 = 基线 + 变化量*eased + 噪声；纯函数，便于单测。"""
    base = BASELINE[sensor]
    value = base["value"] + float(delta.get(sensor, 0.0)) * eased + rng.uniform(-base["noise"], base["noise"])
    return max(base["floor"], value)


def run_scenario(client: mqtt.Client, name: str, seed: int, device: str) -> None:
    spec = SCENARIOS[name]
    rng = random.Random(seed)
    steps = int(spec["duration"])
    print(f"[sim] 注入场景 {name}：{steps}s，{spec['desc']}；device={device}")

    for tick in range(steps):
        eased = smoothstep(tick / max(1, steps - 1))
        values = {sensor: scenario_value(sensor, eased, spec["delta"], rng) for sensor in BASELINE}
        for sensor, value in values.items():
            publish(client, sensor, value, BASELINE[sensor]["unit"], tick, name, device)
        if tick % 2 == 0:
            heartbeat(client, device)
            print(
                f"[sim] t={tick:02d}s ch4={values['ch4']:.2f}% temp={values['temp']:.1f}C "
                f"vib={values['vibration']:.2f} airflow={values['airflow']:.2f}"
            )
        time.sleep(1.0)

    print("[sim] 场景结束")


def run_normal(client: mqtt.Client, seed: int, device: str) -> None:
    rng = random.Random(seed)
    tick = 0
    print(f"[sim] 正常模式，1 Hz 发布到 {TOPIC}（device={device}，Ctrl+C 停止）")
    while True:
        wave = math.sin(tick / 30.0) * 0.6
        publish(client, "temp", BASELINE["temp"]["value"] + wave + rng.uniform(-1, 1), "C", tick, "normal", device)
        publish(client, "ch4", max(0.01, BASELINE["ch4"]["value"] + rng.uniform(-0.03, 0.03)), "%", tick, "normal", device)
        publish(client, "co", BASELINE["co"]["value"] + rng.uniform(-1, 1), "ppm", tick, "normal", device)
        publish(client, "airflow", BASELINE["airflow"]["value"] + rng.uniform(-0.05, 0.05), "m3/s", tick, "normal", device)
        publish(client, "vibration", BASELINE["vibration"]["value"] + rng.uniform(-0.2, 0.2), "mm/s", tick, "normal", device)
        if tick % 2 == 0:
            heartbeat(client, device)
        tick += 1
        time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟传感器")
    ap.add_argument("--mqtt", default="127.0.0.1:1883")
    ap.add_argument("--inject", default=None, choices=sorted(SCENARIOS))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", default="line1-dev-01", help="设备编号，事件与心跳都带这个字段")
    args = ap.parse_args()

    host, _, port = args.mqtt.partition(":")
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="sensor-simulator")
    client.connect(host, int(port or 1883), keepalive=30)
    client.loop_start()

    try:
        if args.inject:
            run_scenario(client, args.inject, args.seed, args.device)
        else:
            run_normal(client, args.seed, args.device)
    except KeyboardInterrupt:
        print("\n[sim] 退出")
    finally:
        time.sleep(0.2)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
