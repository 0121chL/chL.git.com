#!/usr/bin/env python3
"""Modbus → MQTT 桥：把现场仪表（RS485 / Modbus-TCP）读成 `line1/telemetry`。

    python modbus_bridge.py                 # 读真实设备（config/devices.json）
    python modbus_bridge.py --fake          # 没有硬件时生成模拟读数，链路与真实一致

真实模式下每个点位按 `scale/offset` 线性换算：
    value = raw * scale + offset
换算出后按现有遥测格式发布，规则引擎与后端都不用改。
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
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "devices.json"

BASELINE = {
    "ch4": (0.30, 0.02),
    "temp": (68.0, 0.6),
    "co": (8.0, 0.6),
    "vibration": (3.6, 0.15),
    "airflow": (3.6, 0.04),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def read_modbus(client: Any, device: dict[str, Any], point: dict[str, Any]) -> float | None:
    """读一个保持寄存器并换算工程值；失败返回 None（上层跳过，不阻塞其他点位）。"""
    from pymodbus.client import ModbusTcpClient, ModbusSerialClient  # 延迟导入

    count = int(point.get("count", 1))
    try:
        rr = client.read_holding_registers(point["address"], count=count, slave=device.get("unit", 1))
    except TypeError:  # pymodbus 3.7+ 用 device_id=
        rr = client.read_holding_registers(point["address"], count=count, device_id=device.get("unit", 1))
    if rr.isError():
        return None
    raw = rr.registers[0] if len(rr.registers) == 1 else rr.registers
    try:
        return float(raw) * float(point.get("scale", 1)) + float(point.get("offset", 0))
    except (TypeError, ValueError):
        return None


def fake_value(sensor: str, tick: int, rng: random.Random) -> float:
    base, noise = BASELINE.get(sensor, (1.0, 0.01))
    wave = math.sin(tick / 25.0) * noise * 4
    return max(0.0, base + wave + rng.uniform(-noise, noise))


def main() -> int:
    ap = argparse.ArgumentParser(description="Modbus → MQTT 桥")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--fake", action="store_true", help="无硬件模式：生成模拟读数")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    mq_cfg = config.get("mqtt", {})
    telemetry_topic = mq_cfg.get("telemetry_topic", "line1/telemetry")
    heartbeat_topic = mq_cfg.get("heartbeat_topic", "line1/heartbeat")
    poll_interval = float(config.get("poll_interval_s", 1.0))
    heartbeat_interval = float(config.get("heartbeat_interval_s", 2.0))

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="modbus-bridge")
    client.connect(mq_cfg.get("host", "127.0.0.1"), int(mq_cfg.get("port", 1883)), keepalive=30)
    client.loop_start()

    rng = random.Random(7)
    modbus_clients: dict[str, Any] = {}
    if not args.fake:
        try:
            from pymodbus.client import ModbusSerialClient, ModbusTcpClient  # noqa: F401
        except ImportError:
            print("[bridge] 缺少 pymodbus：pip install pymodbus；或先用 --fake 演示", file=sys.stderr)
            return 2
        for device in config["devices"]:
            if device.get("mode") == "rtu":
                modbus_clients[device["device_id"]] = ModbusSerialClient(
                    port=device["serial"], baudrate=device.get("baudrate", 9600), timeout=1
                )
            else:
                modbus_clients[device["device_id"]] = ModbusTcpClient(
                    device.get("host", "127.0.0.1"), port=device.get("port", 502), timeout=1
                )
            modbus_clients[device["device_id"]].connect()

    print(
        f"[bridge] {'FAKE' if args.fake else 'MODBUS'} → {telemetry_topic} @ "
        f"{mq_cfg.get('host')}:{mq_cfg.get('port')}；设备 {len(config['devices'])} 台"
    )

    tick = 0
    last_heartbeat = 0.0
    while True:
        for device in config["devices"]:
            device_id = device["device_id"]
            modbus = modbus_clients.get(device_id)
            for point in device["points"]:
                sensor = point["sensor"]
                value = fake_value(sensor, tick, rng) if args.fake else read_modbus(modbus, device, point)
                if value is None:
                    continue
                payload = {
                    "ts": now_iso(),
                    "line": device.get("line", "line1"),
                    "device_id": device_id,
                    "sensor": sensor,
                    "value": round(value, 2),
                    "unit": point.get("unit", ""),
                    "tick": tick,
                    "scenario_id": "modbus_fake" if args.fake else "modbus_live",
                }
                client.publish(telemetry_topic, json.dumps(payload, ensure_ascii=False), qos=0)

            if time.time() - last_heartbeat >= heartbeat_interval:
                hb = {"ts": now_iso(), "device_id": device_id, "status": "online", "source": "modbus-bridge"}
                client.publish(heartbeat_topic, json.dumps(hb, ensure_ascii=False), qos=0)

        if time.time() - last_heartbeat >= heartbeat_interval:
            last_heartbeat = time.time()

        tick += 1
        time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[bridge] 退出")
