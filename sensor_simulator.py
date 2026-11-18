"""模拟传感器：向 `line1/telemetry` 发布 1 Hz 读数（基线 + 平滑注入 + 噪声）。

    python sensor_simulator.py                    # 正常模式（基线，不触发规则）
    python sensor_simulator.py --inject ch4_rise  # 瓦斯上升 + 温度同步上升 + 风量下降
    python sensor_simulator.py --inject temp_spike
    python sensor_simulator.py --inject vib_spike

    # 没有硬件时，用 --profile hardware 扮演 ESP32-S3 那块板子：
    # 报文结构、单位、量程与固件一致，方便先把整条链路跑通
    python sensor_simulator.py --profile hardware
    python sensor_simulator.py --profile hardware --inject hw_gas

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
from pathlib import Path

import paho.mqtt.client as mqtt

import sensor_calibration as sc

ROOT = Path(__file__).resolve().parent
HW_CONFIG = ROOT / "config" / "hardware.json"

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
            device: str, extra: dict | None = None, topic: str = TOPIC) -> None:
    payload = {
        "ts": now(),
        "line": "line1",
        "device_id": device,
        "sensor": sensor,
        "value": round(value, 3),
        "unit": unit,
        "tick": tick,
        "scenario_id": scenario,
    }
    if extra:
        payload.update(extra)
    client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=0)


def heartbeat(client: mqtt.Client, device: str, extra: dict | None = None, topic: str = HEARTBEAT_TOPIC) -> None:
    payload = {"ts": now(), "device_id": device, "status": "online", "source": "simulator"}
    if extra:
        payload.update(extra)
    client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=0)


# --------------------------------------------------------------------------- #
# hardware 模式：扮演 ESP32-S3 + MQ-4 + DS18B20 + MPU6050
# --------------------------------------------------------------------------- #
def load_hw_config() -> dict:
    """读 config/hardware.json；读不到就用一组与固件默认值一致的参数。"""
    try:
        return json.loads(HW_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def hw_mq4(adc_mv: float, cfg: dict) -> dict:
    sensor_cfg = (cfg.get("sensors") or {}).get("ch4") or {}
    return sc.mq4_reading(adc_mv, {
        "vcc": sensor_cfg.get("vcc", 5.0),
        "rl_ohm": sensor_cfg.get("rl_ohm", 10_000.0),
        "r0_ohm": sensor_cfg.get("r0_ohm", 3_000.0),
        "adc_divider": sensor_cfg.get("adc_divider", 2.0),
        "clean_air_pct": sensor_cfg.get("clean_air_pct", 0.0),
    })


HW_SCENARIOS = {
    "hw_gas": {
        "duration": 10,
        "desc": "打火机气靠近 MQ-4：ADC 1050→2300 mV，甲烷约 0→1.6% vol，越过 1.0% 阈值",
        "adc": (1050.0, 2300.0),
        "temp": (25.0, 26.0),
        "vib": (0.2, 0.3),
    },
    "hw_warm": {
        "duration": 12,
        "desc": "手捏 DS18B20：25→35℃（只让曲线动，不到 85℃ 的设备限值）",
        "adc": (1050.0, 1050.0),
        "temp": (25.0, 35.0),
        "vib": (0.2, 0.2),
    },
    "hw_tap": {
        "duration": 6,
        "desc": "拍桌子：振动 0.2→15 mm/s（越过 4.5 mm/s 规则）",
        "adc": (1050.0, 1050.0),
        "temp": (25.0, 25.2),
        "vib": (0.2, 15.0),
    },
}


def _lerp(pair: tuple[float, float], eased: float) -> float:
    start, end = pair
    return start + (end - start) * eased


def _hw_publish_all(client: mqtt.Client, device: str, cfg: dict, tick: int, scenario: str,
                    adc_mv: float, temp_c: float, vib: float, rng: random.Random,
                    topic: str) -> None:
    reading = hw_mq4(adc_mv, cfg)
    jitter = rng.uniform(-0.01, 0.01)
    publish(client, "ch4", max(0.0, reading["pct"] + jitter), "%", tick, scenario, device,
            extra={"src": "esp32s3-emu",
                   "raw": {"mv": round(adc_mv, 0), "rs": reading["rs"], "ppm": reading["ppm"]}},
            topic=topic)
    publish(client, "temp", temp_c + rng.uniform(-0.2, 0.2), "C", tick, scenario, device,
            extra={"src": "esp32s3-emu"}, topic=topic)
    publish(client, "vibration", max(0.0, vib + rng.uniform(-0.1, 0.1)), "mm/s", tick, scenario, device,
            extra={"src": "esp32s3-emu"}, topic=topic)


def run_hardware(client: mqtt.Client, cfg: dict, name: str | None, seed: int, device: str,
                 hb_topic: str, topic: str) -> None:
    """hardware 模式主循环：无 --inject 时走基线，有则跑对应场景。"""
    rng = random.Random(seed)
    spec = HW_SCENARIOS.get(name) if name else None
    steps = int(spec["duration"]) if spec else 0
    if spec:
        print(f"[sim-hw] 注入场景 {name}：{steps}s，{spec['desc']}；device={device}")
    else:
        print(f"[sim-hw] 基线模式，1 Hz 发布 ch4/temp/vibration（device={device}，Ctrl+C 停止）")

    tick = 0
    while True:
        if spec:
            if tick >= steps:
                print("[sim-hw] 场景结束")
                return
            eased = smoothstep(tick / max(1, steps - 1))
            adc_mv = _lerp(spec["adc"], eased)
            temp_c = _lerp(spec["temp"], eased)
            vib = _lerp(spec["vib"], eased)
        else:
            eased = 0.0
            adc_mv = 1050.0 + math.sin(tick / 30.0) * 8.0
            temp_c = 25.0 + math.sin(tick / 60.0) * 0.8
            vib = 0.2

        _hw_publish_all(client, device, cfg, tick, name or "hardware", adc_mv, temp_c, vib, rng, topic)
        if tick % 2 == 0:
            heartbeat(client, device, extra={"fw": "simulator-hw", "rssi": -55, "ip": "127.0.0.1"},
                      topic=hb_topic)
        if spec and tick % 2 == 0:
            print(f"[sim-hw] t={tick:02d}s adc={adc_mv:.0f}mV → "
                  f"ch4={hw_mq4(adc_mv, cfg)['pct']:.3f}% temp={temp_c:.1f}C vib={vib:.1f}mm/s")
        tick += 1
        time.sleep(1.0)


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
    ap.add_argument("--profile", choices=("sim", "hardware"), default="sim",
                    help="sim=仿真测点；hardware=扮演 ESP32-S3 板子（报文与固件一致）")
    ap.add_argument("--inject", default=None,
                    help="场景名（sim: ch4_rise/temp_spike/vib_spike；hardware: hw_gas/hw_warm/hw_tap）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", default=None,
                    help="设备编号；hardware 模式默认取 config/hardware.json 里的 device_id")
    args = ap.parse_args()

    cfg = load_hw_config() if args.profile == "hardware" else {}
    mq_cfg = cfg.get("mqtt") or {}
    topic = mq_cfg.get("telemetry_topic", TOPIC)
    hb_topic = mq_cfg.get("heartbeat_topic", HEARTBEAT_TOPIC)
    device = args.device or cfg.get("device_id") or "line1-dev-01"

    if args.profile == "hardware":
        if args.inject and args.inject not in HW_SCENARIOS:
            ap.error(f"hardware 模式只支持 {sorted(HW_SCENARIOS)}")
    elif args.inject and args.inject not in SCENARIOS:
        ap.error(f"sim 模式只支持 {sorted(SCENARIOS)}")

    host, _, port = args.mqtt.partition(":")
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="sensor-simulator")
    client.connect(host, int(port or 1883), keepalive=30)
    client.loop_start()

    try:
        if args.profile == "hardware":
            run_hardware(client, cfg, args.inject, args.seed, device, hb_topic, topic)
        elif args.inject:
            run_scenario(client, args.inject, args.seed, device)
        else:
            run_normal(client, args.seed, device)
    except KeyboardInterrupt:
        print("\n[sim] 退出")
    finally:
        time.sleep(0.2)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
