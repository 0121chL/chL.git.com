# 安全监测 Agent Demo

本地可运行的一条完整链路：**模拟传感器 → MQTT → 规则引擎 → LangGraph Agent → 人工审批 → SQLite 工单 → Next.js 看板**。
无云服务、无 API Key。

```
safe-monitor-agent
├─ backend_server.py       FastAPI：REST + WS + SSE，审批恢复与幂等
├─ server.py               旧命令别名，直接转给 backend_server（不再有第二套实现）
├─ agent_graph.py          LangGraph：analyze → interrupt → 批准/驳回
├─ analyzer.py              检索 + 接地校验（Ollama / 本地模型共用）
├─ local_llm.py            transformers 本地推理（Qwen2.5-3B-Instruct）
├─ regulation_store.py     18 条模拟规程 + FAISS + BM25
├─ offline_analyzer.py     无模型时的模板兜底
├─ db.py                   SQLite：events / work_orders / approvals / audit_log
├─ rule_engine.py          阈值/趋势规则 → events.jsonl + POST /events（带 device_id）
├─ sensor_simulator.py     正常模式 / --inject / --profile hardware / 心跳
├─ sensor_calibration.py   MQ-4 的 Rs→ppm→%、MPU6050 的 a→mm/s + 标定 CLI
├─ modbus_bridge.py        Modbus → MQTT（--fake 无硬件演示）
├─ mqtt_bridge.py          line1/events + line1/heartbeat 订阅
├─ firmware/               ESP32-S3 现场网关固件（MQ-4 / DS18B20 / MPU6050）
├─ web/                    Next.js 15 App Router + Tailwind
├─ scripts/                p5_acceptance / e2e_check / local_llm_check / hw_check / reset_demo
├─ config/                 mosquitto.conf / devices.json（Modbus）/ hardware.json（接线与标定）
└─ .models/                Qwen2.5-3B-Instruct（约 6.2 GB，已 gitignore）
```

## 数据从哪来（没有真实传感器）

全部是程序合成的，但**合成发生在最底层**，上层判定是真实逻辑：

| 层 | 来源 | 说明 |
|---|---|---|
| 遥测 | `sensor_simulator.py` | 1 Hz 发 ch4/temp/co/airflow/vibration，基线 + 正弦 + 随机噪声；`--inject ch4_rise` 用 smoothstep 同时抬 ch4、抬 temp、降风量 |
| 事件 | `rule_engine.py` | 按 `config/rules.json` 阈值/趋势真实判定，事件里的 `delta_1m` 由历史窗口算出，不是手写 |
| 分析 | `analyzer.py` | 检索 18 条规程 + 本地 3B / Ollama / 离线模板；条条款号可回查 |
| 工单 | 人工批准后写 SQLite | 带审批人、审批意见、审批时间 |

事件上有一个 `origin` 字段区分来源：`rule_engine`（遥测→规则）、`watchdog`（设备离线）、
`manual`（联调用直投）。前端对 `manual` 显示黄色「手工注入」、对 `watchdog` 显示「看门狗」，
避免把联调数据误当成真实判定。

```powershell
python scripts\inject_event.py --scenario gas_surge            # 默认走遥测→规则引擎
python scripts\inject_event.py --scenario gas_surge --direct   # 直投一条事件（origin=manual）
```

## 接真实硬件（ESP32-S3 + MQ-4 + DS18B20 + MPU6050）

除了仿真，也可以直接把真传感器接进来：固件把 MQ-4 的电压换算成 %vol、DS18B20 出 ℃、
MPU6050 出 mm/s，报文与 `sensor_simulator.py` / `modbus_bridge.py` 完全同构，上层不用改一行。

```powershell
.\START.cmd -Hardware                                  # 只起 broker/后端/规则引擎/看板，等板子上报
python scripts\hw_check.py --live                      # 自检：报文/单位/速率/心跳
python scripts\hw_check.py --calibrate --seconds 60    # 洁净空气里算 R0 与零点
python sensor_simulator.py --profile hardware --inject hw_gas   # 没板子时先用同格式跑通
```

**接线、分压、标定口径、排障表，以及这套硬件的边界（MQ-4 不是计量级、DS18B20 到不了 85℃、
MPU6050 的 mm/s 是近似）见 [README-HARDWARE.md](README-HARDWARE.md)。**

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

## 一键启动（推荐）

双击或命令行运行：

| 文件 | 作用 |
|---|---|
| **`START.cmd`** | 体检 → 起 broker/后端/规则引擎/模拟器/前端 → 自动打开浏览器 |
| **`DEMO.cmd`** | 注入一次 `ch4_rise` 异常（看板出现事件卡片） |
| **`STOP.cmd`** | 停止全部应用进程（broker 服务需管理员停） |

```powershell
.\START.cmd                 # 默认离线模板，秒出分析
.\START.cmd -Backend local  # 用本地 Qwen2.5-3B（CPU，约 1 分钟/次）
.\START.cmd -Check          # 只体检
.\START.cmd -NoSimulator    # 不起传感器，只用注入
.\DEMO.cmd                  # 触发一次异常
.\STOP.cmd                  # 停止
```

窗口里会打印每个服务的地址与日志位置；失败时停在窗口里显示原因（`data\logs\*.err`）。

## 登录与账号

看板默认需要登录：未登录访问会被重定向到 `/login`，**所有写操作（审批、拒绝、重试、注入、工单流转）
都要求会话 token；读事件、工单、遥测、告警也一样**（`/api/status`、`/metrics` 只含聚合状态，留给探针）。

- 首次使用点「注册」创建账号（用户名 3–32 位；**密码 ≥8 位，且同时含字母和数字**），注册后自动登录；
- 密码用 PBKDF2-SHA256（20 万轮）加盐存储；**会话令牌只存 SHA-256 摘要**，库里没有明文 token；
- 连续输错 5 次锁定 60 秒，之后逐次翻倍（最多 15 分钟），返回 429；
- 审批人取**当前登录用户**；**不能审批自己注入的事件**（四眼原则，403）；
- SSE/WS 用短时票据：`POST /api/auth/stream-ticket` 换票后连 `/api/stream?ticket=...`
  （浏览器 `EventSource` 不能自定义请求头，这是唯一干净的做法）；
- `POST /api/approvals/{id}` 的 `decision` 是**必填**，缺了返回 422 —— 不再默认批准；
- `POST /api/auth/logout-all` 可吊销该账号全部会话；`GET /api/auth/sessions` 查看会话；
- 账号与会话存在 SQLite（`users` / `sessions` 表），重启不丢；旧库启动时自动迁移成摘要。

事件卡与工单表显示中文名称（甲烷浓度上升 / 甲烷超限 / 温度超限 / 振动超限 / 设备离线 …），
规则编号以小字保留在下方，映射在 `web/lib/labels.ts`。

## 工单流转与复议

- 工单状态：`created`（待处理）→ `assigned`（处理中）→ `closed`（已关闭），可「重新打开」；
- 工单表提供「指派 / 关闭 / 重新打开」，处置结果与操作人写 `audit_log`；
- 事件被拒绝后可在分析卡点「重新提交复核」：生成新事件（`retry_of` 指向原事件），原事件标为 `superseded`；
- 工单关闭/重开都经 SSE 推送，多端同步刷新。

## 数据一致性

- 遥测时间窗用 `julianday()` 比较，兼容 `+08:00` 偏移，**“最近 30 分钟”不再把全天数据算进来**；
- 新写入的遥测时间统一转成 UTC（`...Z`）；
- 内存事件表最多保留 `MAX_MEMORY_EVENTS`（默认 800）条，终止态优先淘汰；
- 遥测按 24h、审计/事件按 30 天清理（规则引擎每小时执行一次）。

```powershell
# 脚本/验收需要会话时可以这么做（或直接设 API_TOKEN 走服务令牌）
curl.exe -s -X POST http://127.0.0.1:8000/api/auth/register ^
  -H "Content-Type: application/json" -d "{\"username\":\"demo\",\"password\":\"demo123456\"}"
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
| 2 | 无原始时序 | 遥测落 SQLite `telemetry` 表（默认保留 24h，每小时清理）；`GET /api/telemetry` 查询；分析卡新增「遥测趋势」折线（最近 30 分钟，15s 刷新） | 注入后 `points=40`，图表 30 点 |
| 3 | 分析串行、慢 | 分析并发信号量 + `ANALYSIS_TIMEOUT_S` 超时降级（超时自动改离线模板）；图操作单独加锁，审批不再被分析阻塞 | `/metrics` 的 `analysis_timeout_total` |
| 4 | 不是真 Agent | 图加 `plan → tools → analyze`：按策略调用 `get_device_status` / `get_recent_events`，工具结果进提示词 | `/metrics` 的 `tool_calls_total` |
| 5 | 审批语义弱 | 审批人 = 登录用户（不可冒名），审批必须带会话；`APPROVAL_TIMEOUT_S` 默认 600s，超时升级告警（不自动批）；重复调用 409 优先于 422 | `p5_acceptance` 25/25 |
| 6 | 规则硬编码 | 阈值/单位/趋势全部移到 `config/rules.json`，新增 co 规则；`--rules` 可换配置 | `tests/test_rules.py` |
| 7 | 失败不闭环 | `POST /events/{id}/reanalyze` 重试；前端「重试分析」按钮；高危/离线事件写 `data/alerts.jsonl`，可配 `ALERT_WEBHOOK` | `GET /api/alerts` |
| 8 | 可观测性弱 | `GET /metrics`（Prometheus 文本）：事件/分析/超时/工具/告警/队列/在线设备/spool | 实测见下表 |
| 9 | 无鉴权 | 登录/注册 + 会话 token（SQLite），写接口需会话；`API_TOKEN` 作为脚本用服务令牌 | 实测 401 / 200 |
| 10 | 无单测 | `tests/` 134 个用例（解析/接地/检索/DB/规则/硬件换算/审批闸门/处置后验证/设置/派单通知/整改闭环/值班排班/SLA/角色边界），`pytest -q` 约 22 s | 134 passed |

新增环境变量：

```
API_TOKEN=             # 非空则写接口需要 Bearer token（脚本/验收用）
ANALYSIS_TIMEOUT_S=240 # >0 时分析超时降级为离线模板（0=不限时）
ANALYSIS_CONCURRENCY=1 # 允许并发的分析数（CPU 3B 建议保持 1）
EVENT_WORKERS=4        # 事件处理线程池上限（防事件洪峰无限开线程）
APPROVAL_TIMEOUT_S=600 # 待审批超时写告警（默认 10 分钟，不自动批准）
AUTH_TTL_DAYS=7        # 登录会话有效期（天）
STREAM_TICKET_TTL_S=600# SSE/WS 票据有效期（秒）
ALERT_WEBHOOK=         # 可选：告警 POST 到该地址
ALERT_ROTATE_BYTES=8388608  # 告警文件超过该大小滚一份 .1
ALLOW_LOCAL_INGEST=1   # 0 = 只认 API_TOKEN；放反向代理后面必须设 0
TRUSTED_INGEST_HOSTS=127.0.0.1,::1,localhost  # 允许直投 /events 的客户端地址
DEVICE_OFFLINE_TIMEOUT=15
```

## P9：接硬件 + 越权/并发加固

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 只有仿真、接不了真传感器 | `firmware/esp32_s3_gateway` 固件 + `config/hardware.json` 接线记录 + `scripts/hw_check.py` 自检与标定，见 [README-HARDWARE.md](README-HARDWARE.md) |
| 2 | 读接口全裸奔 | 事件/工单/遥测/告警都要会话；`/api/status`、`/metrics` 只留聚合状态给探针 |
| 3 | SSE 无法带鉴权头 | `POST /api/auth/stream-ticket` 换短时票据，前后端都已改 |
| 4 | `POST /api/approvals` 缺 decision 时默认批准（fail-open） | `decision` 改为必填，缺了 422；`tests/test_api.py` 有回归 |
| 5 | 事件入口信任 127.0.0.1（代理后等于全开） | `ALLOW_LOCAL_INGEST` / `TRUSTED_INGEST_HOSTS` 可关 |
| 6 | 看门狗遍历 `EVENTS` 无锁，可能被并发改字典打死 | `events_snapshot()` 锁内复制 + 循环体 try/except + 线程不死 |
| 7 | 每事件一个裸线程 | `EVENT_POOL`（`EVENT_WORKERS` 上限）+ 退出时 shutdown |
| 8 | 分析超时默认关闭 | 默认 240 s，超时降级离线模板 |
| 9 | spool 里一个 422 坏事件会永久重投 | 区分 4xx（进 `dead/` 死信）与网络错（重试，上限 5 次） |
| 10 | 内存事件裁剪不回收 `EVENT_LOCKS` | 一起回收；`events_memory_overflow_total` 可观测 |
| 11 | 事件字段直接进提示词（可注入指令） | 白名单 + 数值校验 + `<data>` 隔离 + 系统提示词声明数据不可信 |
| 12 | 风险等级只靠提示词约束 | `analyzer.clamp_risk_to_severity()` 用代码兜底：high 不可能输出 low |
| 13 | `rule_engine` 不读 `MONITOR_DB` | 改用 `db.resolve_db_path()`，与后端同库 |
| 14 | 告警/事件日志只增不减 | `alerts.jsonl` 按大小滚动、`/api/alerts` 改成尾部读取；`events.jsonl` 同样滚动 |

新增接口：`GET /metrics`、`GET /api/telemetry`、`GET /api/alerts`、`POST /events/{id}/reanalyze`。

## P10：业务闭环（处置后验证 + 误报治理 + 设置页）

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 关闭工单只看一段文字，气体还超限也能关 | `POST /api/work_orders/{id}/close` 先做**处置后验证**：回看 `retest_window_minutes` 分钟内该设备该测点的遥测峰值，仍 ≥ 阈值就 409 并返回验证结论 |
| 2 | 验证拦住了，但现场确实没法回传数据 | 允许「例外关闭」，但必须填 ≥4 字理由，写 `audit_log` 的 `closed_with_exception` 并单独计数 |
| 3 | 离线事件没有遥测可比 | `device_offline` 事件改为校验设备是否重新上线（`mode=device_online`） |
| 4 | 驳回只有自由文本，误报率拍脑袋 | `approvals.reason_code` 结构化枚举（误报/重复/已处置/无需处置/传感器故障/其他），`GET /api/stats/rejections` 按规则 × 原因聚合 |
| 5 | 用户没有任何可配置项 | 新增 `/settings` 页面 + `GET/PUT /api/settings`（复测窗口、提示音、首屏条数、批准前确认、默认驳回原因），存在 `user_settings` 表 |
| 6 | 改密码要能自己改 | `POST /api/auth/password`：校验旧密码、吊销**其它**会话、保留当前这枚 |
| 7 | 工单上看不到验证结果 | 工单表显示「已复测 / 例外关闭」徽标，鼠标悬停看验证结论与例外理由 |

新增接口：`GET/PUT /api/settings`、`POST /api/auth/password`、`GET /api/stats/rejections`；
`/api/stream` 增加 `limit` 参数（来自「首屏事件条数」）。

界面上：看板右上角「设置」进入新页面；驳回时多一个原因下拉；关闭工单改成对话框，
验证不通过会在弹窗里显示「窗口 / 样本 / 峰值 / 阈值」并要求填例外理由。

## P11：审批不等 AI + 持续告警（时效性与噪声）

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 高危事件最需要秒级响应，却要等 AI 分析完 | **规则处置卡**先出：不调模型，直接从事件数值 + 检索条款 + 事件类型生成处置建议，事件立刻进入 `awaiting_approval` |
| 2 | 本地 3B 单次 55–100 s，审批被拖住 | AI 分析移到后台线程池（`ANALYSIS_POOL`），到货后追加一帧；**失败/超时都不影响审批** |
| 3 | 分析在图里被跑了两遍（`process_event` 一次、`analyze_node` 一次） | 延迟模式下分析完全移出审批图（`build_app(defer_analysis=True)`），图只剩 `interrupt → 批准/驳回` |
| 4 | 模型挂了事件就卡在"分析失败"没人能批 | AI 失败只记 `analysis_error`，事件状态仍是 `awaiting_approval`；`POST /events/{id}/reanalyze` 可单独重试 AI |
| 5 | 冷启动第一条事件被 embedding 模型加载挡住 | 索引没预热时规则卡走**纯 BM25**（`mode="lexical"`），实测冷启动**137 ms** 即可审批；索引在后台预热 |
| 6 | merge 窗口内的事件直接丢弃，看不到"还在持续" | `SustainedTracker`：首次越限建事件，之后按 `refresh_s` 调 `POST /events/{id}/refresh` 刷新同一条事件的 `aggregate`（持续秒数/触发次数/峰值/是否已恢复），**不新建事件、不新增审批** |
| 7 | 卡在 `analyzing` 时日志里什么都没有 | 线程池任务统一走 `_safe_call`，未捕获异常打印完整栈并计数（原来被 Future 静默吞掉） |

环境变量与接口：

```
DEFER_ANALYSIS=1        # 0 = 退回"分析完再审批"的老行为
refresh_s               # config/rules*.json 里，持续告警刷新间隔（秒）
POST /events/{id}/refresh   # 规则引擎用，合并 aggregate（require_ingest）
```

界面上：事件卡显示「分析来源」徽标（规则处置卡 · AI 分析中… / AI 分析 / AI 补充（审批后到达））；
持续告警会显示「持续 8 分 20 秒 · 触发 512 次 · 峰值 2.3%」，恢复后变「已恢复 · 持续 …」。

## P12：负责人表 + 站内待办 + 通知

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 派单只能手打名字，没有统一名单 | 新增**负责人表** `staff`（姓名/班组/电话/绑定账号），设置页可增删、停用 |
| 2 | 派单是 `window.prompt` | 工单表「指派」改成对话框：从负责人表下拉选人，或手填临时处理人，可写派单说明 |
| 3 | 派单后没人知道 | 绑定了账号的负责人收到**站内通知**（顶部铃铛未读角标），未绑定则通知回到派单人（提醒线下通知） |
| 4 | 操作员没有"我的活"视图 | 新增 `/todo` 页：**分派给我的** + **待派单**两块，可直接派单/关闭（关闭仍走处置后验证） |
| 5 | 只有派单人知道工单归属 | 工单新增 `assignee_username`；「我的待办」按 `assignee_username` 或同名 `assignee` 匹配 |

新增接口：`GET/POST /api/staff`、`PATCH/DELETE /api/staff/{id}`、`GET /api/notifications`、
`POST /api/notifications/{id}/read`、`POST /api/notifications/read-all`；
`POST /api/work_orders/{id}/assign` 改为接受 `{staff_id | assignee, note}`。

## P13：整改闭环 + 值班轮值

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 工单只有"派单/关闭"两态，没有操作员回执 | 状态机补齐：`created(待派单) → assigned(已派单) → accepted(已接单) → handled(已处理·待复测) → closed` |
| 2 | 操作员做完了没法留证据 | `POST .../accept` 接单；`POST .../handle` 提交**整改措施 + 现场照片**（≤3 MB，jpeg/png/webp，存 `data/photos/`，不入库） |
| 3 | 照片要能看 | `GET /api/work_orders/{id}` 单条详情返回 `photo_data_url`；列表不带照片，避免一次拉几 MB |
| 4 | 谁能接单不清楚 | 绑定了账号的工单**只能本人**接单/标记已处理（403）；未绑定的允许任一登录用户代操作并记录 `accepted_by/handled_by` |
| 5 | "已处理"还要能判是否真恢复 | 关闭仍走**处置后验证**：峰值 < 阈值才写 `verified_at`；否则 409，只能填例外理由 |
| 6 | 值班轮值靠口头 | 负责人表加 `on_duty`；派单可选「**按当班自动选人**」= 当班里挑在办工单最少的那个人（可限班组） |

新增字段：`work_orders.accepted_at/accepted_by/handled_at/handled_by/measure/photo_path/verified_at`、`staff.on_duty`。
新增接口：`POST /api/work_orders/{id}/accept`、`POST /api/work_orders/{id}/handle`、`GET /api/work_orders/{id}`。

界面上：工单状态徽标分五档；行内出现「接单 / 已处理 / 照片」；已处理弹窗可传图；
派单弹窗多一个「按当班自动选人」勾选；设置页负责人表可一键「设为当班」。

## P14：排班表（自动值班）+ SLA 超时升级

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 「当班」只能手工点，换班要人盯着 | 新增**排班表** `shifts`（班次名/负责人/开始·结束/星期，`start > end` 表示跨夜）；每 30 秒按当前时间自动置 `on_duty` |
| 2 | 自动同步会把手工人的值班清掉 | 同步**只动出现在排班表里的人**；没排班的人保留手工设置 |
| 3 | 超时只有一个全局 600 秒 | 新增 `config/sla.json` 分级：审批 5/30/120 分钟、处置 30/240/1440 分钟（high/medium/low）。`APPROVAL_TIMEOUT_S=0`（新默认）表示按严重度取值 |
| 4 | 超时了没人知道 | 审批超时 → 写 alert + 站内通知给**当班的人**；工单处置超时 → 通知被指派人，未绑定账号则升级给当班；同一单只提醒一次 |
| 5 | 历史积压会一次性刷几百条通知 | 启动时把「已超时」的历史工单合并成一条「启动检查：N 张历史工单已超时」，之后**新超时**的才逐条升级 |
| 6 | 界面上看不到时限 | 工单行显示「时限 30 分钟 · 剩 18 分钟」，超时变红「SLA 超时 90 分钟」 |

新增接口：`GET/POST /api/shifts`、`PATCH/DELETE /api/shifts/{id}`、`POST /api/shifts/sync`、`POST /api/sla/check`。

## P15：注册时选身份（审批人 / 工程师）

| # | 问题 | 现在的实现 |
|---|---|---|
| 1 | 注册没有身份概念，所有人都能看全部事件 | 注册页二选一：**审批人**（看全部事件、审批、派单）/ **工程师**（只看派给自己的工单）；默认 engineer |
| 2 | 审批人会不会被系统自动生成 | 不会。审批人必须自己在注册页选「审批人」注册，系统不预置、不自动创建 |
| 3 | 工程师越权 | 事件/遥测/告警/负责人表/排班表/驳回统计/派单/审批 → 一律 403；工单列表与详情只返回 `assignee_username` 是自己的 |
| 4 | 工程师能不能关别人的单 | 不能。绑定了账号的单只有本人能接单/已处理/关闭；未派单的单一律不许动；审批人可代为操作 |
| 5 | 老账号怎么办 | 迁移时把旧的 `operator` 统一升级为 `approver`，权限不变 |

对前端：登录后按身份分流（审批人 → 看板，工程师 → 我的待办）；工程师看不到事件流、看不到「指派」按钮、看不到「待派单」；
SSE 票据只发给审批人，工程师的待办走轮询。

**工程师页面（`/todo`）的数据分析**：全部只用「派给自己的工单」计算，不碰事件/遥测接口。

- 4 个 KPI：分派给我 / 已关闭（含平均处置时长）/ SLA 超时 / 按时完成率
- 工单状态分布（待接单 · 处理中 · 待复测 · 已关闭）
- 近 7 天关闭趋势柱状图
- SLA 内完成率环形图
- 右上角补了「设置 / 退出登录」（之前只有看板有）

`GET /api/work-orders` 现在每条都带 `sla`（时限/截止/剩余），列表也能显示「剩 N 分钟 / 已超时」。
截图见 `docs/screenshots/engineer-todo-wide.png`。

### 审批人邀请码（防"自称审批人"）

| 规则 | 说明 |
|---|---|
| 首次启动 | 自动生成 `XXXX-XXXX`，打印到后端控制台，并写入 `data/approver_invite.txt`（不入库、不进 Git） |
| 注册审批人 | 必须填对邀请码，否则 403；工程师注册不需要 |
| **自己设置** | 「设置 → 审批人邀请码」直接改：输入 4–32 位（字母/数字/下划线/横线）→ 保存，旧码立即失效并写审计。仓库里第一个审批人（如 A王五）登录后就能把自己的码设成 `WANGWU-2026` |
| 查看 | 同上，仅审批人可见；或 `GET /api/auth/invite` |
| 随机生成 | 设置页「随机生成一个」/ `POST /api/auth/invite/rotate` |
| 指定 | `POST /api/auth/invite/set`（body `{"code": "..."}`） |
| 生产部署 | 用环境变量 `APPROVER_INVITE_CODE` 指定（此时页面上改不动，避免被人随手改） |

## 环境备注（本机）

- Python 3.13（`E:\python`），依赖见 `requirements.txt`；`paho-mqtt` 用于订阅。
- Node 22 为便携版，解压在 `.tools/node-v22.20.0-win-x64`，未写入系统 PATH。
- Mosquitto 2.0.20 已安装为 Windows 服务（`127.0.0.1:1883`），启动类型需手工改为 Manual。
- 分析引擎：本地 `Qwen2.5-3B-Instruct`（`.models/`，CPU 约 55–100 秒/次）；无模型时退到离线模板。
- Ollama 未安装：本机到 ollama.com 仅 11–30 KB/s、GitHub 直连超时，1.5 GB 安装包不可得。
