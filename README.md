# 安全监测 Agent Demo

本地可运行的一条完整链路：**模拟传感器 → MQTT → 规则引擎 → LangGraph Agent → 人工审批 → SQLite 工单 → Next.js 看板**。
无云服务、无 API Key。

```
F:\deepseek\wenjian\safe-monitor-agent
├─ server.py              FastAPI + SSE + 审批恢复
├─ agent_graph.py         LangGraph：analyze → interrupt → 批准/驳回
├─ analyzer.py            检索 + Ollama + 接地校验
├─ regulation_store.py    16 条模拟规程 + FAISS + BM25
├─ offline_analyzer.py    无 Ollama 时的模板兜底
├─ db.py                  SQLite：events / work_orders / audit_log
├─ mqtt_bridge.py         line1/events 订阅
├─ web/                   Next.js 15 App Router + Tailwind
├─ scripts/               inject_event.py / e2e_check.py / reset_demo.py
└─ config/mosquitto.conf
```

## 阶段与文档

| 阶段 | 内容 | 文档 | 验收 |
|---|---|---|---|
| P2 | LangGraph + 人工审批暂停点 | `README-P2.md` | `python test_approval_flow.py`（28 项） |
| P3 | Ollama + 规程检索 + 接地校验 | `README-P3.md` | `python verify_p3.py`（13 项） |
| P4 | SSE + Next.js 双卡片看板 | `README-P4.md` | `python scripts/e2e_check.py` / `web/scripts/browser_check.mjs` |
| P5 | 集成联调 + 一键演示 | `README-DEMO.md` | `python scripts/p5_acceptance.py`（19 项，含幂等/WS） |

## 一键演示（P5）

```powershell
powershell -ExecutionPolicy Bypass -File .\start_demo.ps1   # 体检 + 起 5 个服务
.\demo_trigger.ps1                                          # 注入 ch4_rise
# 浏览器 http://localhost:3000 → 点批准 → 工单入表
.\stop_demo.ps1                                             # 停止
```

新增文件：`backend_server.py`（REST + WS + SSE 兼容）、`rule_engine.py`（阈值/趋势规则 → events.jsonl + POST）、
`sensor_simulator.py`（正常模式 / `--inject ch4_rise|temp_spike`）、`start_demo.ps1`、`stop_demo.ps1`、
`demo_trigger.ps1` / `demo_trigger.sh`。`server.py` 仍可用（等价入口）。

## 快速启动

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent

# 1. MQTT（程序已装为服务；手动启动用）
& "C:\Program Files\mosquitto\mosquitto.exe" -c config\mosquitto.conf -v

# 2. 后端
python server.py

# 3. 前端
$env:PATH = "F:\deepseek\wenjian\safe-monitor-agent\.tools\node-v22.20.0-win-x64;" + $env:PATH
cd web; npm run dev

# 4. 注入演示事件
python scripts\inject_event.py --scenario gas_surge
```

浏览器打开 <http://localhost:3000>：事件卡 → 分析卡 → 点批准 → 工单入表。

## 环境备注（本机）

- Python 3.13（`E:\python`），依赖见 `requirements.txt`；`paho-mqtt` 用于订阅。
- Node 22 为便携版，解压在 `.tools/node-v22.20.0-win-x64`，未写入系统 PATH。
- Mosquitto 2.0.20 已安装并以 Windows 服务运行（`127.0.0.1:1883`）。
- Ollama 未安装：本机到 ollama.com 仅 11–30 KB/s、GitHub 直连超时，1.5 GB 安装包不可得。
  当前后端自动退到 `offline_analyzer`，界面明确标注「分析引擎：离线模板（非模型）」。
