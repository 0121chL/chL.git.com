"""模拟器场景：注入必须同时改变多个参数（ch4 上升时 temp 也要真的上升）。"""

from __future__ import annotations

import random

import sensor_simulator as sim


def value_of(sensor: str, scene: str, eased: float, seed: int = 0) -> float:
    return sim.scenario_value(sensor, eased, sim.SCENARIOS[scene]["delta"], random.Random(seed))


def test_ch4_rise_raises_temp_and_drops_airflow():
    delta = sim.SCENARIOS["ch4_rise"]["delta"]
    assert delta["ch4"] > 0
    assert delta["temp"] > 0, "ch4 上升场景必须同时抬温，否则多参数叙事不成立"
    assert delta["airflow"] < 0

    temp0 = value_of("temp", "ch4_rise", 0.0)
    temp1 = value_of("temp", "ch4_rise", 1.0)
    assert temp1 - temp0 > 5.0

    ch4_0 = value_of("ch4", "ch4_rise", 0.0)
    ch4_1 = value_of("ch4", "ch4_rise", 1.0)
    assert ch4_1 > 1.0 > ch4_0


def test_temp_spike_crosses_threshold():
    assert value_of("temp", "temp_spike", 1.0) > 85.0
    assert value_of("ch4", "temp_spike", 1.0) < 1.0


def test_vib_spike_crosses_threshold():
    assert value_of("vibration", "vib_spike", 1.0) > 4.5
    assert value_of("vibration", "vib_spike", 0.0) < 4.0


def test_all_scenarios_have_baseline_sensors():
    for name, spec in sim.SCENARIOS.items():
        assert spec["duration"] > 0, name
        for sensor in spec["delta"]:
            assert sensor in sim.BASELINE, f"{name} 引用了未知传感器 {sensor}"
