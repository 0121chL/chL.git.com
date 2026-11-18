"""真实硬件的单位换算、标定口径与配置一致性。

这组测试守的是"换传感器之后数还能对得上"：MQ-4 的电压→浓度、R0 的定义、
MPU6050 的加速度→mm/s，以及 config/hardware.json、rules.hardware.json、
固件 config.example.h 三处不许各说各话。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import rule_engine
import sensor_calibration as sc

ROOT = Path(__file__).resolve().parent.parent
HW_JSON = ROOT / "config" / "hardware.json"
RULES_HW = ROOT / "config" / "rules.hardware.json"
FW_HEADER = ROOT / "firmware" / "esp32_s3_gateway" / "config.example.h"


# --------------------------------------------------------------------------- #
# MQ-4
# --------------------------------------------------------------------------- #
def test_r0_is_defined_at_1000ppm_not_clean_air():
    """R0 = 洁净空气 Rs / 4.4；用洁净空气 Rs 当 R0 会把浓度放大一个量级。"""
    cal = sc.calibrate_r0([1050.0] * 5)
    # 结果按 0.1 Ω 取整，tol 放宽到 0.2 Ω
    assert cal["r0"] == pytest.approx(cal["rs_air"] / sc.MQ4_AIR_RATIO, abs=0.2)
    assert cal["r0"] < cal["rs_air"]


def test_mq4_calibration_zeroes_clean_air():
    """标定之后，同样的 ADC 读数应该读回 0%——否则演示里会一直有本底。"""
    cfg = {"r0_ohm": 3000.0, "clean_air_pct": 0.0}
    reading = sc.mq4_reading(1050.0, cfg)
    cfg["clean_air_pct"] = reading["pct"]
    again = sc.mq4_reading(1050.0, cfg)
    assert again["pct"] == pytest.approx(0.0, abs=1e-3)


def test_mq4_ppm_matches_curve_at_ratio_one():
    """Rs/R0 = 1 就是 1000 ppm，这是曲线的锚点。"""
    assert sc.rs_ratio_to_ppm(1.0) == pytest.approx(1000.0)


def test_mq4_is_monotonic_in_voltage():
    """电压越高（Rs 越小）浓度越高；换算法错了这里立刻会暴露。"""
    cfg = {"r0_ohm": 3000.0}
    values = [sc.mq4_reading(mv, cfg)["pct"] for mv in (900, 1100, 1300, 1500, 1700, 1900)]
    assert values == sorted(values)
    assert values[-1] > values[0]
    assert values[0] >= 0.0


def test_mq4_one_percent_needs_roughly_sensor_range():
    """1 %vol = 10000 ppm 应该落在 MQ-4 量程内，否则演示根本触发不了。"""
    cfg = {"r0_ohm": 3000.0, "clean_air_pct": 0.0}
    # 找到能读到 1 %vol 的 ADC 值，验证它在分压后的可用区间（< 3.0 V）
    mv = 200
    while mv < 3000 and sc.mq4_reading(mv, cfg)["pct"] < 1.0:
        mv += 10
    assert mv < 3000, "MQ-4 在 200–3000 mV 内应当能读到 1 %vol"


def test_mq4_handles_dead_sensor_gracefully():
    """探头断线（0 mV）不能算出负数或抛出异常。"""
    low = sc.mq4_reading(0.0, {"r0_ohm": 3000.0})
    high = sc.mq4_reading(3300.0, {"r0_ohm": 3000.0})
    assert low["pct"] >= 0.0
    assert high["pct"] >= 0.0
    assert high["rs"] >= 0.0
    assert high["pct"] <= 100.0


def test_calibration_takes_median_not_mean():
    """标定时一次毛刺不应该把 R0 带跑偏。"""
    clean = sc.calibrate_r0([1050.0] * 9)
    noisy = sc.calibrate_r0([1050.0] * 9 + [3000.0])
    assert abs(noisy["r0"] - clean["r0"]) < clean["r0"] * 0.05


# --------------------------------------------------------------------------- #
# MPU6050
# --------------------------------------------------------------------------- #
def test_accel_conversion_at_reference_point():
    """0.1 g @ 25 Hz ≈ 6.2 mm/s，与 4.5 mm/s 的阈值同量级。"""
    assert sc.accel_rms_g_to_velocity_mm_s(0.1, 25.0) == pytest.approx(6.24, abs=0.1)


def test_static_board_reports_zero_vibration():
    assert sc.accel_samples_to_mm_s([1.0, 1.0, 1.0, 1.0]) == pytest.approx(0.0)


def test_desk_tap_exceeds_threshold():
    """拍一下桌子（合矢量冲到 1.3 g）应当能触发 4.5 mm/s 的规则。"""
    samples = [1.0, 1.0, 1.3, 1.5, 1.2, 1.0, 1.0]
    assert sc.accel_samples_to_mm_s(samples, 25.0) > 4.5


# --------------------------------------------------------------------------- #
# 三处配置必须一致
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_hardware_json_declares_only_wired_sensors():
    hw = _load(HW_JSON)
    assert set(hw["sensors"]) == {"ch4", "temp", "vibration"}


def test_rules_hardware_covers_every_wired_sensor():
    hw = _load(HW_JSON)
    rules = rule_engine.load_rules(RULES_HW)
    missing = set(hw["sensors"]) - set(rules["sensors"])
    assert not missing, f"这些测点上报了却没有规则：{missing}"
    for name in rules["sensors"]:
        assert rules["sensors"][name]["unit"] == hw["sensors"][name]["unit"]


def test_hardware_thresholds_stay_at_regulation_values():
    """硬件演示不允许偷改规程阈值，否则 AI 引用的条款会和事件对不上。"""
    rules = rule_engine.load_rules(RULES_HW)
    assert rules["sensors"]["ch4"]["threshold"] == 1.0
    assert rules["sensors"]["temp"]["threshold"] == 85.0
    assert rules["sensors"]["vibration"]["threshold"] == 4.5
    assert rules["sensors"]["ch4"]["trend"]["delta"] == 0.5


def test_firmware_device_id_matches_config():
    """固件里的 DEVICE_ID 必须和 config/hardware.json 一致，否则看板认不出设备。"""
    header = FW_HEADER.read_text(encoding="utf-8")
    match = re.search(r'#define\s+DEVICE_ID\s+"([^"]+)"', header)
    assert match, "config.example.h 里找不到 DEVICE_ID"
    assert match.group(1) == _load(HW_JSON)["device_id"]


def test_firmware_topics_match_hardware_json():
    header = FW_HEADER.read_text(encoding="utf-8")
    hw = _load(HW_JSON)["mqtt"]
    for key, macro in (("telemetry_topic", "TELEMETRY_TOPIC"), ("heartbeat_topic", "HEARTBEAT_TOPIC")):
        match = re.search(rf'#define\s+{macro}\s+"([^"]+)"', header)
        assert match, f"config.example.h 里找不到 {macro}"
        assert match.group(1) == hw[key]
