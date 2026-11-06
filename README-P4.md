# 阶段四：Next.js 看板 + SSE + 人工审批

页面只有一个路由 `/`：顶部状态条、左侧规则事件卡、右侧 Agent 分析卡、底部工单列表。

## 架构

```
注入脚本 ──MQTT(line1/events)──> Mosquitto ──> 后端订阅
                                              │
                              规则事件对象 ──> analyze ──> LangGraph(interrupt)
                                              │                 │
                                         SSE 广播 <─────────────┘
                                              │
                                    Next.js /  (EventSource)
                                              │
                              点击「批准」 ──POST /api/approvals/{id}──> Command(resume="approve")
                                              │
                                        SQLite work_orders ──SSE work_order──> 底部列表
```

关键约束的落实方式：

- **前端不自行添加记录**：点击批准只发 POST；工单卡片与计数全部等后端 `work_order` 事件回来才更新。
- **审批恢复**：`POST /api/approvals/{event_id}` 内部执行
  `graph.invoke(Command(resume="approve"), {"configurable": {"thread_id": event_id}})`。
- **重复批准**：后端先看 `graph.get_state(cfg).next`，已完成/不存在的线程返回 409。

## 启动（4 个进程）

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent

# 1) MQTT broker（已装为 Windows 服务时会自动运行；手动启动如下）
& "C:\Program Files\mosquitto\mosquitto.exe" -c config\mosquitto.conf -v

# 2) 后端
python server.py                     # http://127.0.0.1:8000

# 3) 前端
$env:PATH = "F:\deepseek\wenjian\safe-monitor-agent\.tools\node-v22.20.0-win-x64;" + $env:PATH
cd web; npm run dev                  # http://localhost:3000

# 4) 注入事件（新开一个终端）
cd F:\deepseek\wenjian\safe-monitor-agent
python scripts\inject_event.py --scenario gas_surge
```

浏览器打开 <http://localhost:3000>。

## 演示动线

1. 顶部状态条：`MQTT 已连接`、`实时流已连接`、分析引擎、事件/待审批/工单计数。
2. 左侧出现 `R-CH4-RISE` 卡片（高危、ch4 1.8% / 限值 1.0%，以及 ch4/temp/co/airflow 快照）。
3. 约 1–3 秒后该事件被自动选中，右侧出现 Agent 分析卡：根因假设、规程依据（逐条列出
   条款编号 + 标题 + 原文摘录）、建议动作、风险等级、批准 / 拒绝按钮。
4. 点击「批准」→ 按钮短暂禁用 → 底部工单表新增一行 `WO-0001`，顶部工单计数 +1，卡片状态变为「已批准」。
5. 再注入一条，点「拒绝」→ 卡片变「已拒绝」，工单表不变。

## 没有 Ollama 时

后端启动时探测 Ollama：连不上就退到 `offline_analyzer`（模板 + 检索条款）。
顶部徽章会显示「分析引擎：离线模板（非模型）」，分析文本也带 `[离线模板]` 前缀。
接了 Ollama 后重启 `server.py`，徽章变为「Ollama · qwen2.5:7b」，代码无需改动。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | MQTT 状态、分析引擎、计数 |
| GET | `/api/events` | 事件 + 分析 + 状态 |
| GET | `/api/work-orders` | 工单列表 |
| GET | `/api/stream` | SSE：hello / event / analysis / analysis_error / work_order / rejected |
| POST | `/api/approvals/{event_id}` | `{"decision":"approve"\|"reject"}` |
| POST | `/api/dev/inject` | 无 MQTT 时的 HTTP 注入通道 |

## 验收

```powershell
# 接口级（不需要浏览器）
python scripts\e2e_check.py --scenario gas_surge

# 浏览器级（Edge headless + CDP，自动点击「批准」并截图）
cd web
node scripts\browser_check.mjs --scenario gas_surge
```

`e2e_check` 输出应包含：`✓ event 推送`、`✓ analysis：risk=high citations=[...]`、
`✓ 收到工单 WO-xxxx，SQLite 记录数=1`，最后一行 `E2E OK`。

`browser_check` 输出应包含 4 个 ✓（事件卡 → 分析卡 → 点击批准 → 工单出现在底部），
最后一行 `BROWSER OK`，截图落在 `data/p4-analysis.png` 与 `data/p4-approved.png`。

实测（连续 3 次不同场景）：三次均 `BROWSER OK`。

## 一个真实的并发坑（已修）

最初版本把 `analysis` 先写进 SQLite，再执行 `graph.invoke` 建中断点。于是页面刷新时
SSE 的 `hello` 能从库里拿到分析、立刻渲染出卡片，用户可能在建图完成前就点「批准」——
`get_state().next` 还是空，返回 409。

修复两处：

1. `process_event` 改成「先 invoke 建中断点，成功后才写库 + 推送分析」；
2. `approve` 端点的状态检查放进 `PIPELINE_LOCK`，若该事件还在建图，请求会等它停在审批点，
   而不是立刻判 409。

复现记录：修复前 3 次浏览器验收中 2 次卡在工单表 30s 超时；修复后 3/3 通过。

## 已知限制

- 分析引擎在**启动时**选定，Ollama 中途上线需重启 `server.py`。
- `InMemorySaver` 仍在用：后端重启后，未审批的事件在 SQLite 里仍是 pending，但点批准会返回 409。
- 未做分页/鉴权：本地演示用途，`/api/dev/*` 不设防。
