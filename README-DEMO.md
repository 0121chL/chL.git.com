# 演示验收清单（P5）

从零到「事件卡片 → Agent 分析卡片 → 点击批准 → 工单出现」的完整序列，含每步预期输出与常见失败原因。

> P6 增补（已包含在下面的流程里）：
> 1. **审批留痕** —— 批准/驳回时填写审批人与意见，写入 `approvals` 表，工单列表 join 显示；
> 2. **断点持久化** —— LangGraph 状态存 `data/checkpoints.db`，后端重启后仍可批准，不再 409。
> 界面顶部相应多出「断点：SQLite 持久化」徽章，工单表多出「审批人 / 审批时间」两列。

---

## 0. 前置条件（一次性）

| 依赖 | 检查命令 | 本机现状 |
|---|---|---|
| Python 3.11+ 及依赖 | `python -c "import fastapi, uvicorn, paho.mqtt.client, langgraph"` | ✅ 3.13.14 |
| Mosquitto | `Get-NetTCPConnection -LocalPort 1883` | ✅ 已装为 Windows 服务 |
| Node 22 + 前端依赖 | `cd web; npm run dev` 能起 | ✅ `.tools/node-*`（便携版） |
| Ollama + qwen2.5:7b | `ollama list` | ❌ 未安装 → 自动回退离线模板 |

> Ollama 缺失不阻断演示：后端启动时探测失败会使用 `offline_analyzer`，界面徽章显示
> 「分析引擎：离线模板（非模型）」，分析文本带 `[离线模板]` 前缀。装上后重启后端即可切换。

---

## 1. 一键启动

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent
powershell -ExecutionPolicy Bypass -File .\start_demo.ps1
```

预期输出（节选，实测）：

```
=== 1. 环境体检 ===
  [PASS] Python                     Python 3.13.14
  [PASS] Python 依赖                  fastapi / uvicorn / paho / langgraph
  [PASS] RAG 依赖                    sentence-transformers + faiss
  [PASS] Mosquitto :1883            已监听
  [FAIL] Ollama :11434              未运行 → 将回退离线模板
  [FAIL] 模型 qwen2.5:7b              缺失：ollama pull qwen2.5:7b
  [PASS] Node.js                    v22.20.0
  [PASS] 前端依赖                       web/node_modules 就绪
  [PASS] 端口 8000                    空闲
  [PASS] 端口 3000                    空闲

=== 2. 启动服务 ===
  [跳过] Mosquitto 已在运行 :1883
  [启动] backend_server :8000  pid=28996
  [就绪] http://127.0.0.1:8000/api/status
  [启动] rule_engine        pid=8908  → 订阅 line1/telemetry
  [启动] sensor_simulator   pid=30952  → 1 Hz 发布基线
  [启动] next dev :3000  pid=26688
  [就绪] http://localhost:3000

=== 3. 服务一览 ===
  Mosquitto        mqtt://127.0.0.1:1883
  backend_server   http://127.0.0.1:8000
  rule_engine      订阅 telemetry → events.jsonl + POST /events
  sensor_simulator 正常模式 1 Hz
  看板             http://localhost:3000
  停止全部：powershell -ExecutionPolicy Bypass -File .\stop_demo.ps1
```

> 只想体检不启动：`.\start_demo.ps1 -Check`；不想开模拟器：`.\start_demo.ps1 -NoSimulator`。
> 建议直接在控制台运行；若用管道（`| Select-Object`）包住，脚本退出后子进程仍在跑，属正常。

---

## 2. 现场触发（另开一个终端）

```powershell
.\demo_trigger.ps1
# 或  python sensor_simulator.py --inject ch4_rise
```

预期：

```
[demo] 注入 ch4_rise（约 10 秒，ch4 0.30% -> 1.30%）...
[sim] t=00s ch4=0.30% temp=68.0C airflow=3.60
[sim] t=04s ch4=0.72% temp=68.0C airflow=3.22
[sim] t=06s ch4=1.04% temp=68.0C airflow=2.93
[sim] t=08s ch4=1.27% temp=68.0C airflow=2.73
[sim] 场景结束
```

规则引擎日志（`data\logs\rule_engine.log`）：

```
[rule] 订阅 line1/telemetry @ 127.0.0.1:1883；事件写入 ...\events.jsonl；后端 http://127.0.0.1:8000
[rule] 触发 R-CH4-RISE ch4=0.89% -> evt-r-ch4-rise-...
[rule] POST /events -> 202 {"accepted":true,"duplicate":false,...}
[rule] 触发 R-CH4-HIGH ch4=1.18% -> evt-r-ch4-high-...
[rule] POST /events -> 202 {...}
```

浏览器 <http://localhost:3000> 依次出现：左侧 `R-CH4-RISE / 高危` 卡片 → 右侧分析卡（根因 / 规程依据 / 建议动作 / 风险等级）→ 点「批准」→ 底部工单表新增 `WO-xxxx`。

---

## 3. 验收命令（可直接粘贴）

```powershell
# 3.1 后端契约 + 重复批准幂等 + WS 三类推送（19 条断言）
python scripts\p5_acceptance.py

# 3.2 接口级全链路（不经浏览器）
python scripts\e2e_check.py --scenario gas_surge

# 3.3 浏览器级（Edge headless 自动点击批准）
cd web; node scripts\browser_check.mjs --scenario gas_surge

# 3.4 重复批准（手工版）
$eid = (Get-Content events.jsonl -Tail 1 | ConvertFrom-Json).event_id
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/approve/$eid"   # duplicate=False, work_order.id=X
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/approve/$eid"   # duplicate=True,  work_order.id=X
(Invoke-RestMethod "http://127.0.0.1:8000/work_orders").work_orders | Where-Object event_id -eq $eid
```

预期：第 2 次 `duplicate=True`，两次 `work_order.id` 相同，`work_orders` 中该事件只有 1 行。
实测：

```
duplicate=False work_order_id=3
duplicate=True  work_order_id=3
duplicate=True  work_order_id=3
```

`p5_acceptance.py` 实测输出：

```
P5 OK: 25/25 条断言通过（含重复批准幂等、审批留痕、WS 三类推送）
```

---

## 3.5 审批留痕（P6）

界面上在「批准 / 拒绝」上方填 **审批人**（默认「值班调度员」）与 **审批意见**（可选），提交后落库。

```powershell
$eid = (Get-Content events.jsonl -Tail 1 | ConvertFrom-Json).event_id
$body = @{ approver = "张工"; comment = "同意降载，30 分钟后复测" } | ConvertTo-Json
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/approve/$eid" -Body $body -ContentType "application/json"
```

预期返回里 `work_order.approver = 张工`、`approval_comment = 同意降载，30 分钟后复测`、`decided_at` 非空。
数据库：

```powershell
python -c "import db; print(db.get_approval(db.DEFAULT_DB_PATH, '<event_id>'))"
```

```
{'event_id': '...', 'decision': 'approved', 'approver': '张工',
 'comment': '同意降载，30 分钟后复测', 'decided_at': '2026-11-06T14:51:04Z', 'work_order_id': 9}
```

重复批准不会覆盖第一审批人（`p5_acceptance` 里有这条断言）。

---

## 3.6 重启恢复（P6）

验证「后端重启后，原来停在 interrupt 的事件仍能批准」：

```powershell
# 1) 造一条待审批事件，记下 event_id
$ev = @{ event_id = "evt-restart-demo"; sensor = "ch4"; rule_id = "R-CH4-RISE"; event_type = "gas_surge";
         severity = "high"; triggered_at = (Get-Date).ToString("o");
         observed = @{ value = 1.6; unit = "%"; threshold = 1.0 } } | ConvertTo-Json
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/events" -Body $ev -ContentType "application/json"

# 2) 杀掉后端再启动
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*backend_server.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
python backend_server.py

# 3) 重启后直接批准（不重新投递事件）
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/approve/evt-restart-demo"
```

预期：第 3 步返回 200 且生成工单；`/events/evt-restart-demo` 在重启后是 404（内存字典清空），
但检查点仍在 `data/checkpoints.db`，所以能恢复。实测：

```
approve after restart -> ok=True duplicate=False work_order=10 approver=李工
```

若这里返回 409，说明 `data/checkpoints.db` 被删了或用了 `InMemorySaver`（检查 `/api/status` 的 `checkpointer`）。

---

## 4. 常见失败与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| `Errno 10048` / 脚本提示「端口 8000/3000 已被占用」 | 上次未停干净 | `.\stop_demo.ps1`；或 `Get-NetTCPConnection -LocalPort 8000 \| % { Stop-Process -Id $_.OwningProcess -Force }` |
| 浏览器 `MQTT 未连接` | broker 没起 | `Get-Service mosquitto`；`Start-Service mosquitto`；或 `mosquitto -c config\mosquitto.conf -v` |
| 事件卡片一直不出现 | rule_engine 挂了 / 没订阅上 | `data\logs\rule_engine.err`；确认 `sensor_simulator.py --inject ch4_rise` 与 rule_engine 连的是同一 broker |
| `analysis_error` / 顶部红条 | Ollama 未启动但被选为引擎 | 重启后端；或 `ollama serve` 后 `ollama pull qwen2.5:7b` |
| 分析文本带 `[离线模板]` | 没装 Ollama | 正常降级；要真实模型见 README-P3 |
| `HTTP 409 该事件不在待审批状态` | 该事件已批准过、或已驳回 | 换一条事件；已批准的直接看工单表（重复批准本身是幂等的，返回旧工单） |
| 工单没有审批人 | 用的是 P6 之前写入的历史数据 | 新工单才会写 `approvals`；历史行为空是正常的 |
| `powershell 无法加载文件 … 未数字签名` | 执行策略 | 始终用 `powershell -ExecutionPolicy Bypass -File .\start_demo.ps1` |
| `next: command not found` | PATH 没有便携版 Node | 脚本已自动注入 `.tools\node-*`；手工执行时先 `$env:PATH = "...\.tools\node-v22.20.0-win-x64;" + $env:PATH` |
| 页面空白 / 一直「实时流断开」 | 后端 8000 没起 | `curl http://127.0.0.1:8000/api/status`；看 `data\logs\backend.err` |

---

## 5. 停止

```powershell
.\stop_demo.ps1
```

会按 `data\demo.pids.json` 结束 backend / rule_engine / sensor_simulator / next dev。
Mosquitto 若是 Windows 服务，用 `Stop-Service mosquitto`（演示结束可不管）。

---

## 6. 数据复位（换一场演示）

```powershell
.\stop_demo.ps1
python scripts\reset_demo.py
.\start_demo.ps1
```

`events.jsonl` 是追加写入的，如需清空：`Remove-Item events.jsonl`。
