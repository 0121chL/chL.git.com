#!/usr/bin/env bash
# 现场演示触发：模拟 ch4 快速上升，规则引擎会在数秒内产出 high 事件。
#   ./demo_trigger.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python}"
echo "[demo] 注入 ch4_rise（约 10 秒，ch4 0.30% → 1.30%）..."
"$PY" sensor_simulator.py --inject ch4_rise
echo ""
echo "[demo] 注入结束。浏览器 http://localhost:3000 应已出现："
echo "        1) 左侧 R-CH4-RISE / 高危 事件卡片"
echo "        2) 右侧 Agent 分析卡片（根因 / 规程依据 / 建议动作）"
echo "        3) 点击「批准」后底部工单表新增一行"
echo "       若卡片未出现：检查 backend_server 与 rule_engine 是否在跑（data/logs/）。"
