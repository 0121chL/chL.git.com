#!/usr/bin/env python3
"""硬件联调自检：订阅 MQTT，校验 ESP32-S3 上报的报文格式、单位、速率与心跳。

    python scripts/hw_check.py                 # 看 20 秒，给报告
    python scripts/hw_check.py --seconds 60
    python scripts/hw_check.py --calibrate     # 洁净空气里跑，给出 R0 / 本底建议
    python scripts/hw_check.py --mqtt 192.168.1.10:1883
    python scripts/hw_check.py --live          # 一边跑一边打印每帧

退出码：0 = 三项测点 + 心跳都正常；1 = 有缺项或报错。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sensor_calibration as sc  # noqa: E402

HW_CONFIG = ROOT / "config" / "hardware.json"
REQUIRED_SENSORS = ("ch4", "temp", "vibration")


def load_config() -> dict[str, Any]:
    try:
        return json.loads(HW_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def check_payload(payload: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """校验单帧遥测，返回问题列表（空 = 合格）。"""
    problems: list[str] = []
    expected_device = cfg.get("device_id")
    if expected_device and payload.get("device_id") != expected_device:
        problems.append(f"device_id={payload.get('device_id')!r} 与配置 {expected_device!r} 不一致")

    sensor = payload.get("sensor")
    spec = (cfg.get("sensors") or {}).get(str(sensor))
    if sensor not in REQUIRED_SENSORS:
        problems.append(f"未知测点 {sensor!r}")
        return problems
    if spec is None:
        return problems

    unit = payload.get("unit")
    if unit != spec.get("unit"):
        problems.append(f"{sensor} 单位 {unit!r} 与配置 {spec.get('unit')!r} 不一致")

    value = payload.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        problems.append(f"{sensor} value 不是数字：{value!r}")
        return problems

    low, high = spec.get("range") or [None, None]
    if low is not None and value < low:
        problems.append(f"{sensor} 值 {value} 低于量程下限 {low}")
    if high is not None and value > high:
        problems.append(f"{sensor} 值 {value} 高于量程上限 {high}")
    return problems


def local_ipv4() -> list[str]:
    """列出本机的局域网 IPv4（不含 127.0.0.1），用来判断板子能不能连进来。"""
    import socket

    addresses: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    return addresses


def lan_reachable(port: int) -> list[tuple[str, bool]]:
    """逐个 LAN 地址试连 broker：只有 127.0.0.1 通、LAN 不通时板子必然连不上。"""
    import socket

    results: list[tuple[str, bool]] = []
    for address in local_ipv4():
        try:
            with socket.create_connection((address, port), timeout=2):
                results.append((address, True))
        except OSError:
            results.append((address, False))
    return results


def run(seconds: float, host: str, port: int, live: bool, calibrate: bool, cfg: dict[str, Any]) -> int:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("缺少 paho-mqtt：pip install paho-mqtt")
        return 1

    mq = cfg.get("mqtt") or {}
    telemetry_topic = mq.get("telemetry_topic", "line1/telemetry")
    heartbeat_topic = mq.get("heartbeat_topic", "line1/heartbeat")

    stats: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    counts: dict[str, int] = {}
    hb_count = 0
    last_hb: dict[str, Any] = {}
    offline_seen: list[str] = []
    ch4_mv: list[float] = []
    started = time.time()
    first_at: dict[str, float] = {}

    def on_message(client, userdata, msg):  # noqa: ANN001
        nonlocal hb_count
        if time.time() - started > seconds:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            problems.append(f"{msg.topic} 收到非 JSON 报文")
            return
        if not isinstance(payload, dict):
            problems.append(f"{msg.topic} 报文不是对象")
            return

        if msg.topic == heartbeat_topic:
            hb_count += 1
            last_hb.clear()
            last_hb.update(payload)
            status = str(payload.get("status", "online")).lower()
            if status in ("offline", "lwt", "disconnected"):
                offline_seen.append(f"{datetime.now().strftime('%H:%M:%S')} status={status}")
            return

        sensor = str(payload.get("sensor"))
        counts[sensor] = counts.get(sensor, 0) + 1
        first_at.setdefault(sensor, time.time())
        for problem in check_payload(payload, cfg):
            if problem not in problems:
                problems.append(problem)

        value = payload.get("value")
        entry = stats.setdefault(sensor, {"min": value, "max": value, "last": value, "unit": payload.get("unit")})
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            entry["min"] = min(entry["min"], value) if entry["min"] is not None else value
            entry["max"] = max(entry["max"], value) if entry["max"] is not None else value
            entry["last"] = value
        entry["unit"] = payload.get("unit")

        raw = payload.get("raw") or {}
        if sensor == "ch4" and isinstance(raw.get("mv"), (int, float)):
            ch4_mv.append(float(raw["mv"]))

        if live:
            print(f"  {datetime.now().strftime('%H:%M:%S')} {sensor:9s} {value!s:>8} {payload.get('unit', ''):5s}"
                  f" raw={raw or '-'}")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="hw-check")
    client.on_message = on_message
    try:
        client.connect(host, port, keepalive=30)
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 broker {host}:{port} —— {exc}")
        print("  1) broker 是否在跑；2) config/mosquitto.conf 是否监听 0.0.0.0；3) Windows 防火墙是否放行 1883")
        return 1
    client.subscribe(telemetry_topic, qos=0)
    client.subscribe(heartbeat_topic, qos=0)
    client.loop_start()

    print(f"监听 {telemetry_topic} / {heartbeat_topic} @ {host}:{port}，共 {seconds:.0f}s …")

    # 最容易踩的坑：broker 只监听 127.0.0.1，本机自检一切正常，板子永远连不上
    lan = lan_reachable(port)
    blocked = [address for address, ok in lan if not ok]
    if blocked:
        print("  ⚠️ 这些本机地址连不上 broker：" + ", ".join(blocked))
        print("     → 板子会一直 rc=-2。改 config/mosquitto.conf 的 `listener 1883 0.0.0.0`，")
        print("       若是 Windows 服务，要改服务用的那份 mosquitto.conf（不是项目里的），")
        print("       并确认防火墙放行 1883。")

    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()

    elapsed = max(time.time() - started, 1e-6)
    print("\n=== 测点汇总 ===")
    print(f"{'测点':<12}{'帧数':>6}{'速率':>9}{'最小':>10}{'最大':>10}{'最新':>10}  单位")
    for name in REQUIRED_SENSORS:
        entry = stats.get(name)
        if not entry:
            print(f"{name:<12}{0:>6}{'-':>9}{'-':>10}{'-':>10}{'-':>10}  —")
            continue
        rate = counts.get(name, 0) / elapsed
        print(f"{name:<12}{counts.get(name, 0):>6}{rate:>8.2f}/s"
              f"{entry['min']!s:>10}{entry['max']!s:>10}{entry['last']!s:>10}  {entry.get('unit') or ''}")

    print(f"\n心跳：{hb_count} 帧" + (f"，最后状态 {last_hb.get('status')}" if last_hb else ""))
    if last_hb.get("ip"):
        print(f"  设备 IP {last_hb.get('ip')}  RSSI {last_hb.get('rssi')} dBm  固件 {last_hb.get('fw')}")
    if offline_seen:
        print(f"  ⚠️ 期间收到过离线心跳：{offline_seen[-3:]}")

    # ---- 判定 ----
    missing = [name for name in REQUIRED_SENSORS if name not in stats]
    if missing:
        problems.append(f"没收到这些测点：{missing}")
    if hb_count == 0:
        problems.append("没收到心跳（检查固件 DEVICE_ID / HEARTBEAT_TOPIC）")
    for name, count in counts.items():
        if count and count / elapsed < 0.5:
            problems.append(f"{name} 速率只有 {count / elapsed:.2f} 帧/秒，怀疑掉线重连")
    if ch4_mv and max(ch4_mv) < 20:
        problems.append("MQ-4 的 raw.mv 一直是 0 —— AO 没接 / 分压断 / 模块没供电")
    if ch4_mv and min(ch4_mv) > 3000:
        problems.append("MQ-4 的 raw.mv 一直贴顶 —— 分压比不对或 RL 选错")
    temp_entry = stats.get("temp") or {}
    if temp_entry.get("last") in (85.0, -127.0):
        problems.append("DS18B20 读数是 85.0 / -127.0（未接好或上电默认值）")

    if calibrate:
        if not ch4_mv:
            print("\n[标定] 没拿到 raw.mv，无法计算 R0 —— 先确认固件版本 ≥1.0.0")
        else:
            result = sc.calibrate_r0(ch4_mv)
            print("\n=== 标定建议（请确保此刻是洁净空气）===")
            print(f"  采样 {result['samples']} 帧，Rs(air)={result['rs_air']} Ω")
            print(f"  #define MQ4_R0_OHM {result['r0']:.0f}.0f")
            print(f"  #define MQ4_CLEAN_AIR_PCT {result['clean_air_pct']:.4f}f")
            print("  把这两行写进 firmware/esp32_s3_gateway/config.h 后重新烧录")

    print("\n=== 结论 ===")
    if problems:
        for item in dict.fromkeys(problems):
            print(f"  ✗ {item}")
        print("\n排障顺序：broker 监听 → 防火墙 → device_id/topic → 接线 → 标定（见 README-HARDWARE.md）")
        return 1
    print("  ✓ 三个测点、单位、速率、心跳全部正常")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ESP32-S3 硬件联调自检")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--mqtt", default="127.0.0.1:1883")
    ap.add_argument("--live", action="store_true", help="逐帧打印")
    ap.add_argument("--calibrate", action="store_true", help="在洁净空气中采样并给出 R0 建议")
    args = ap.parse_args()

    host, _, port = args.mqtt.partition(":")
    return run(args.seconds, host, int(port or 1883), args.live, args.calibrate, load_config())


if __name__ == "__main__":
    sys.exit(main())
