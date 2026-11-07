# 安全监测 Agent Demo

本地可运行的一条完整链路：**模拟传感器 → MQTT → 规则引擎 → LangGraph Agent → 人工审批 → SQLite 工单 → Next.js 看板**。
无云服务、无 API Key。

```
safe-monitor-agent
├─ backend_server.py       FastAPI：REST + WS + SSE，审批恢复与幂等
├─ server.py               等价入口（兼容旧命令）
├─ agent_graph.py          LangGraph：analyze → interrupt → 批准/驳回
├─ analyzer.py              检索 + 接地校验（Ollama / 本地模型共用）
├─ local_llm.py            transformers 本地推理（Qwen2.5-3B-Instruct）
├─ regulation_store.py     17 条模拟规程 + FAISS + BM25
├─ offline_analyzer.py     无模型时的模板兜底
├─ db.py                   SQLite：events / work_orders / approvals / audit_log
├─ rule_engine.py          阈值/趋势规则 → events.jsonl + POST /events（带 device_id）
├─ sensor_simulator.py     正常模式 / --inject / 心跳
├─ modbus_bridge.py        Modbus → MQTT（--fake 无硬件演示）
├─ mqtt_bridge.py          line1/events + line1/heartbeat 订阅
├─ web/                    Next.js 15 App Router + Tailwind
├─ scripts/                p5_acceptance / e2e_check / local_llm_check / reset_demo
├─ config/                 mosquitto.conf / devices.json（Modbus 点位）
└─ .models/                Qwen2.5-3B-Instruct（约 6.2 GB，已 gitignore）
```

## 阶段与文档

| 阶段 | 内容 | 文档 | 验收 |
|---|---|---|---|
| P2 | LangGraph + 人工审批暂停点 | `README-P2.md` | `python test_approval_flow.py`（28 项） |
| P3 | Ollama + 规程检索 + 接地校验 | `README-P3.md` | `python verify_p3.py`（13 项） |
| P4 | SSE + Next.js 双卡片看板 | `README-P4.md` | `python scripts/e2e_check.py` / `web/scripts/browser_check.mjs` |
| P5 | 集成联调 + 一键演示 | `README-DEMO.md` | `python scripts/p5_acceptance.py`（25 项） |
| P6 | 审批留痕 + 断点持久化 | `README-DEMO.md` §3.5/§3.6 | 同上 + 重启恢复手工验收 |
| P7 | 本地 3B 模型 + 设备接入 | 本文 §P7 / `README-DEMO.md` §3.7 | `python scripts/local_llm_check.py` |

### P6 两个改动

1. **审批留痕**：`POST /approve|/reject/{event_id}` 接受 `approver`、`comment`，写入 `approvals`
   （`event_id` 唯一），工单列表 join 出「审批人 / 审批时间」；重复批准不会覆盖第一审批人。
2. **断点持久化**：`SqliteSaver` 存 `data/checkpoints.db`，后端重启后仍可批准原待审批事件
   （`/api/status` 的 `checkpointer` 字段可查）。

### P7 两个改动

1. **真模型（本地）**：`local_llm.py` 用 transformers 跑 `Qwen2.5-3B-Instruct`（bf16 / CPU），
   与 Ollama 共用同一套提示词与接地校验。启动时按 `Ollama → 本地模型 → 离线模板` 顺序探测，
   界面徽章显示 `Qwen2.5-3B（本地 CPU）`。单次分析约 **55–100 秒**（CPU），可用
   `LLM_BACKEND=offline` 切回模板，或 `LLM_BACKEND=ollama` 指定 Ollama。
2. **设备接入**：遥测/事件带 `device_id`；新增 `line1/heartbeat` 心跳与 15s 离线判定
   （自动生成 `R-DEV-OFFLINE` 事件）；`modbus_bridge.py` 把 Modbus 仪表读成 MQTT
   （`--fake` 可在无硬件时跑通同一条链路）。

## 一键演示（可选）

```powershell
powershell -ExecutionPolicy Bypass -File .\start_demo.ps1   # 体检 + 起 5 个服务
.\demo_trigger.ps1                                          # 注入 ch4_rise
# 浏览器 http://localhost:3000 → 填审批人/意见 → 点批准 → 工单入表
.\stop_demo.ps1                                             # 停止
```

## 手动启动（默认方式，详见 `START-MANUAL.md`）

本项目不注册任何开机自启：没有计划任务、没有启动项、没有守护进程。
唯一需要处理的是 Mosquitto 的 Windows 服务，管理员执行一次：

```powershell
Set-Service -Name mosquitto -StartupType Manual
```

之后每次演示按 5 个终端手工拉起：broker → backend → rule_engine → simulator → 前端，
完整命令、端口、自检与停止方式见 **`START-MANUAL.md`**。

## P8：十项硬化（对应上一轮“硬伤清单”）

| # | 硬伤 | 现在的实现 | 验证 |
|---|---|---|---|
| 1 | 事件会丢 | `rule_engine` POST 失败写 `data/spool/*.json`，后台每 10s 重投，成功即删 | `_spool_test` 实测：暂存→重投→后端 202→文件消失 |
| 2 | 无原始时序 | 遥测落 SQLite `telemetry` 表（默认保留 24h，每小时清理）；`GET /api/telemetry` 查询 | 注入后 `points=40` |
| 3 | 分析串行、慢 | 分析并发信号量 + `ANALYSIS_TIMEOUT_S` 超时降级（超时自动改离线模板）；图操作单独加锁，审批不再被分析阻塞 | `/metrics` 的 `analysis_timeout_total` |
| 4 | 不是真 Agent | 图加 `plan → tools → analyze`：按策略调用 `get_device_status` / `get_recent_events`，工具结果进提示词 | `/metrics` 的 `tool_calls_total` |
| 5 | 审批语义弱 | 审批人必填（≥2 字，否则 422）；`APPROVAL_TIMEOUT_S` 超时升级告警；重复调用幂等/409 优先于 422 | `p5_acceptance` 25/25 |
| 6 | 规则硬编码 | 阈值/单位/趋势全部移到 `config/rules.json`，新增 co 规则；`--rules` 可换配置 | `tests/test_rules.py` |
| 7 | 失败不闭环 | `POST /events/{id}/reanalyze` 重试；前端「重试分析」按钮；高危/离线事件写 `data/alerts.jsonl`，可配 `ALERT_WEBHOOK` | `GET /api/alerts` |
| 8 | 可观测性弱 | `GET /metrics`（Prometheus 文本）：事件/分析/超时/工具/告警/队列/在线设备/spool | 实测见下表 |
| 9 | 无鉴权 | 设置 `API_TOKEN` 后写接口需 `Authorization: Bearer`；前端用 `NEXT_PUBLIC_API_TOKEN` | 实测 401 / 202 |
| 10 | 无单测 | `tests/` 22 个用例（解析/接地/检索/DB/规则），`pytest -q` 8.5s | 22 passed |

新增环境变量：

```
API_TOKEN=             # 非空则写接口需要 Bearer token
ANALYSIS_TIMEOUT_S=0   # >0 时分析超时降级为离线模板
ANALYSIS_CONCURRENCY=1 # 允许并发的分析数（CPU 3B 建议保持 1）
APPROVAL_TIMEOUT_S=0   # >0 时待审批超时写告警（不自动批准）
ALERT_WEBHOOK=         # 可选：告警 POST 到该地址
DEVICE_OFFLINE_TIMEOUT=15
```

新增接口：`GET /metrics`、`GET /api/telemetry`、`GET /api/alerts`、`POST /events/{id}/reanalyze`。

## 环境备注（本机）

- Python 3.13（`E:\python`），依赖见 `requirements.txt`；`paho-mqtt` 用于订阅。
- Node 22 为便携版，解压在 `.tools/node-v22.20.0-win-x64`，未写入系统 PATH。
- Mosquitto 2.0.20 已安装为 Windows 服务（`127.0.0.1:1883`），启动类型需手工改为 Manual。
- 分析引擎：本地 `Qwen2.5-3B-Instruct`（`.models/`，CPU 约 55–100 秒/次）；无模型时退到离线模板。
- Ollama 未安装：本机到 ollama.com 仅 11–30 KB/s、GitHub 直连超时，1.5 GB 安装包不可得。
