"""真实硬件的单位换算与标定（MQ-4 → %vol，MPU6050 → mm/s）。

为什么需要这一层
----------------
仿真器里 `ch4` 直接就是 0.30% 这种数；真实 MQ-4 只给你一个模拟电压。
不做换算就会得到"电压当浓度"的伪数据，规则引擎和规程条款全对不上。

MQ-4 的标定口径（与数据手册一致，别搞反）
    Rs      传感器电阻 = RL × (Vcc − Vout) / Vout
    R0      **1000 ppm 时的 Rs**，不是洁净空气的 Rs
    洁净空气中 Rs/R0 ≈ 4.4，1000 ppm 时 Rs/R0 ≈ 1.0
    曲线    ppm = A × (Rs/R0)^B，A=1000, B≈-2.19（手册曲线两点拟合）
    换算    %vol = ppm / 10000（甲烷 1 %vol = 10000 ppm）

所以标定流程是：洁净空气里测 Rs → R0 = Rs / 4.4 → 填进固件。
本模块与固件里的算法逐行对应，改一处要同步改另一处。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from typing import Any, Iterable, Sequence

# 甲烷爆炸下限 5 %vol；1 %vol = 10000 ppm
VOL_PCT_PER_PPM = 1.0 / 10000.0
# 洁净空气中 Rs/R0 的典型值（数据手册曲线）
MQ4_AIR_RATIO = 4.4
# 默认曲线：ppm = A × (Rs/R0)^B
MQ4_CURVE_A = 1000.0
MQ4_CURVE_B = -2.19

DEFAULT_MQ4: dict[str, Any] = {
    "vcc": 5.0,          # 模块供电
    "rl_ohm": 10_000.0,  # 板载负载电阻
    "r0_ohm": 3_000.0,   # 1000 ppm 时的 Rs（必须现场标定）
    "adc_divider": 2.0,  # Vout = Vadc × 该值（10k/10k 分压）
    "curve_a": MQ4_CURVE_A,
    "curve_b": MQ4_CURVE_B,
    "clean_air_pct": 0.0,  # 洁净空气本底偏移，标定后填
}


# --------------------------------------------------------------------------- #
# MQ-4
# --------------------------------------------------------------------------- #
def adc_mv_to_module_volts(adc_mv: float, divider: float = 2.0) -> float:
    """把 ADC 读数还原成分压前的模块输出电压。"""
    return max(0.0, float(adc_mv)) / 1000.0 * float(divider)


def rs_from_output(vout: float, vcc: float = 5.0, rl_ohm: float = 10_000.0) -> float:
    """传感器电阻 Rs = RL × (Vcc − Vout) / Vout（Vout 被夹在安全区间内）。"""
    vout = min(max(float(vout), 0.05), float(vcc) - 0.05)
    return float(rl_ohm) * (float(vcc) - vout) / vout


def rs_ratio_to_ppm(ratio: float, a: float = MQ4_CURVE_A, b: float = MQ4_CURVE_B) -> float:
    """Rs/R0 → ppm，幂律曲线。ratio ≤ 0 视为无读数。"""
    ratio = float(ratio)
    if ratio <= 0:
        return 0.0
    return float(a) * ratio ** float(b)


def ppm_to_vol_pct(ppm: float, clean_air_pct: float = 0.0) -> float:
    """ppm → 体积分数 %，扣掉洁净空气本底，负数归零。

    上限夹在 100 %（纯甲烷也不可能是 100 %vol 的"读数"），避免探头短路/脱焊时
    把外推到几百万 ppm 的曲线值直接当浓度上报。
    """
    pct = float(ppm) * VOL_PCT_PER_PPM - float(clean_air_pct)
    return min(max(0.0, pct), 100.0)


def mq4_reading(adc_mv: float, cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """一次完整换算，返回 {vout, rs, ratio, ppm, pct}，便于日志与自检。"""
    c = {**DEFAULT_MQ4, **(cfg or {})}
    vout = adc_mv_to_module_volts(adc_mv, c["adc_divider"])
    rs = rs_from_output(vout, c["vcc"], c["rl_ohm"])
    ratio = rs / float(c["r0_ohm"]) if c["r0_ohm"] else 0.0
    ppm = rs_ratio_to_ppm(ratio, c["curve_a"], c["curve_b"])
    return {
        "vout": round(vout, 4),
        "rs": round(rs, 1),
        "ratio": round(ratio, 3),
        "ppm": round(ppm, 1),
        "pct": round(ppm_to_vol_pct(ppm, c["clean_air_pct"]), 4),
    }


def calibrate_r0(adc_mvs: Sequence[float], cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """洁净空气采样 → 建议的 R0、本底偏移、以及校验用的 Rs。

    取中位数而不是均值：偶尔一次毛刺不应该污染标定结果。
    """
    if not adc_mvs:
        raise ValueError("至少需要一个 ADC 采样值（mV）")
    c = {**DEFAULT_MQ4, **(cfg or {})}
    rss = [rs_from_output(adc_mv_to_module_volts(mv, c["adc_divider"]), c["vcc"], c["rl_ohm"]) for mv in adc_mvs]
    rs_air = statistics.median(rss)
    r0 = rs_air / MQ4_AIR_RATIO
    ppm_air = rs_ratio_to_ppm(rs_air / r0, c["curve_a"], c["curve_b"])
    return {
        "rs_air": round(rs_air, 1),
        "r0": round(r0, 1),
        # 标定后洁净空气应读 0，所以本底就取此刻算出来的值
        "clean_air_pct": round(ppm_air * VOL_PCT_PER_PPM, 4),
        "samples": len(rss),
    }


# --------------------------------------------------------------------------- #
# MPU6050
# --------------------------------------------------------------------------- #
def accel_rms_g_to_velocity_mm_s(a_rms_g: float, freq_hz: float = 25.0) -> float:
    """加速度有效值 → 振动速度有效值（工程近似）。

    v_rms = a_rms / (2πf)。这是把"拍桌子"这种宽带冲击折算成 mm/s 的常用做法，
    只为驱动 4.5 mm/s 的演示规则，**不是** ISO 10816 的计量结果。
    """
    freq = max(float(freq_hz), 1.0)
    return float(a_rms_g) * 9.80665 / (2.0 * 3.141592653589793 * freq) * 1000.0


def accel_samples_to_mm_s(magnitudes_g: Iterable[float], freq_hz: float = 25.0) -> float:
    """一串合矢量加速度（静止时约 1.0 g）→ 去重力后的 RMS → mm/s。"""
    dyn = [abs(float(m) - 1.0) for m in magnitudes_g]
    if not dyn:
        return 0.0
    rms = (sum(d * d for d in dyn) / len(dyn)) ** 0.5
    return accel_rms_g_to_velocity_mm_s(rms, freq_hz)


# --------------------------------------------------------------------------- #
# CLI：标定时用
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MQ-4 / MPU6050 单位换算与标定")
    ap.add_argument("--mv", action="append", type=float, default=[],
                    help="洁净空气下的 ADC 读数（mV，可重复多次）")
    ap.add_argument("--r0", type=float, default=None, help="覆盖 R0 后算一个浓度点")
    ap.add_argument("--accel", type=float, default=None, help="加速度 RMS（g）→ mm/s")
    ap.add_argument("--freq", type=float, default=25.0, help="振动积分主频（Hz）")
    ap.add_argument("--adc", type=float, default=None, help="单点换算：ADC mV → %vol")
    ap.add_argument("--table", action="store_true", help="打印 ADC→%vol 对照表")
    args = ap.parse_args(argv)

    if args.accel is not None:
        print(f"a_rms={args.accel} g @ {args.freq} Hz → "
              f"{accel_rms_g_to_velocity_mm_s(args.accel, args.freq):.2f} mm/s")
        return 0

    if args.table:
        cfg = {**DEFAULT_MQ4}
        if args.r0:
            cfg["r0_ohm"] = args.r0
        print(f"{'ADC(mV)':>8}  {'Rs(Ω)':>8}  {'Rs/R0':>6}  {'ppm':>7}  {'%vol':>7}")
        for mv in range(200, 2001, 100):
            r = mq4_reading(mv, cfg)
            print(f"{mv:8d}  {r['rs']:8.0f}  {r['ratio']:6.2f}  {r['ppm']:7.0f}  {r['pct']:7.3f}")
        return 0

    if args.mv:
        result = calibrate_r0(args.mv)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n把下面两行填进 firmware/esp32_s3_gateway/config.h：")
        print(f"  #define MQ4_R0_OHM {result['r0']:.0f}.0f")
        print(f"  #define MQ4_CLEAN_AIR_PCT {result['clean_air_pct']:.4f}f")
        return 0

    if args.adc is not None:
        cfg = {**DEFAULT_MQ4}
        if args.r0:
            cfg["r0_ohm"] = args.r0
        print(json.dumps(mq4_reading(args.adc, cfg), ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(_main())
