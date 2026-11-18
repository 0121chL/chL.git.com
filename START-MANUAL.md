# 手动启动指南

> **懒人版**：双击 `START.cmd` 即可完成下面 1–5 步并自动打开浏览器；`DEMO.cmd` 触发异常，`STOP.cmd` 停止。
> 本文是它背后每一步的手工命令，用于排查或需要逐项控制时。
> 首次打开看板会跳到 `/login`，先点「注册」建账号（密码 ≥8 位且含字母+数字），之后用该账号登录；审批人自动取登录用户。
>
> ⚠️ **注册要选身份**：审批人（看全部事件、审批、派单）/ 工程师（只看派给自己的工单）。
> 注册「审批人」需要**邀请码**：首次启动后端时控制台会打印一行
> `[auth] 审批人注册邀请码：XXXX-XXXX`，同一串也写在 `data/approver_invite.txt`；
> 之后可在「设置 → 审批人邀请码」查看或轮换。工程师注册不需要邀请码。

本项目**不注册任何开机自启**（没有计划任务、没有启动项、没有守护进程）。
唯一的自启项是安装 Mosquitto 时创建的 Windows 服务，需要改成手动；其余 4 个进程全靠手工拉起。

---

## 0. 让 Mosquitto 不再开机自启（一次性，需管理员）

普通 PowerShell 会报 `Access is denied`，请用**管理员** PowerShell：

```powershell
Set-Service -Name mosquitto -StartupType Manual
Get-Service mosquitto | Select-Object Name, Status, StartType   # 应为 Manual / Stopped
```

改完之后：
- 开机不会自动起 broker；
- 需要演示时手动 `Start-Service mosquitto` 或直接跑 `mosquitto.exe`（见下）。

> 不想动服务也可以：保持 `Automatic`，但每次开机后 `Stop-Service mosquitto` 手动停掉。

---

## 1. 启动顺序（5 个终端，各自独立）

| # | 服务 | 端口 | 必需 |
|---|---|---|---|
| 1 | Mosquitto | 1883 | 是 |
| 2 | backend_server | 8000 | 是 |
| 3 | rule_engine | — | 是（要自动出事件） |
| 4 | sensor_simulator | — | 演示用（也可用注入脚本代替） |
| 5 | Next.js 看板 | 3000 | 是 |

### 终端 1：MQTT broker

```powershell
# 方式 A：服务已装（推荐，手动启动）
Start-Service mosquitto

# 方式 B：直接跑二进制（前台，便于看日志；需先停服务，否则端口冲突）
& "C:\Program Files\mosquitto\mosquitto.exe" -c F:\deepseek\wenjian\safe-monitor-agent\config\mosquitto.conf -v
```

验证：`Test-NetConnection 127.0.0.1 -Port 1883` → `TcpTestSucceeded : True`

> 接 ESP32-S3 时 broker 必须监听 `0.0.0.0`（`config/mosquitto.conf` 已改成这样），
> 并且放行防火墙：
> `New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow`
>
> ⚠️ `Start-Service mosquitto` 用的是服务自己那份 `C:\Program Files\mosquitto\mosquitto.conf`，
> 不是项目里的这份。要么合并配置后 `Restart-Service mosquitto`，要么用
> `mosquitto.exe -c config\mosquitto.conf -v` 前台启动（先停服务）。

### 终端 2：后端

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent
python -X utf8 -u backend_server.py
```

看到这三行即成功：

```
[backend] analyzer=local-qwen model=Qwen2.5-3B-Instruct (local/CPU)
[backend] mqtt=127.0.0.1:1883 topic=line1/events heartbeat=line1/heartbeat
[backend] http://127.0.0.1:8000  ws://127.0.0.1:8000/ws
```

- 想用离线模板快速彩排：`$env:LLM_BACKEND="offline"` 再启动（分析秒出）。
- 想强制本地 3B：`$env:LLM_BACKEND="local"`；有 Ollama 时 `"ollama"`。

### 终端 3：规则引擎

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent
python -X utf8 -u rule_engine.py
# 接真实硬件时改用只含 ch4/temp/vibration 的那套规则：
python -X utf8 -u rule_engine.py --rules config\rules.hardware.json
```

```
[rule] 订阅 line1/telemetry @ 127.0.0.1:1883；事件写入 ...\events.jsonl；后端 http://127.0.0.1:8000
```

### 终端 4：传感器（三选一）

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent

# A. 正常模式：1 Hz 基线 + 2 秒一次心跳（Ctrl+C 停止）
python -X utf8 -u sensor_simulator.py

# B. 只在演示时注入一次异常（10 秒，自动退出）
python -X utf8 -u sensor_simulator.py --inject ch4_rise

# C. 扮演 ESP32-S3 板子（报文与固件一致，用于没有硬件时先跑通链路）
python -X utf8 -u sensor_simulator.py --profile hardware --inject hw_gas
```

### 终端 5：前端

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent\web
$env:PATH = "F:\deepseek\wenjian\safe-monitor-agent\.tools\node-v22.20.0-win-x64;" + $env:PATH
npm run dev
```

`Ready in ...` 后打开 <http://localhost:3000>。

---

## 2. 30 秒自检（另开一个终端）

```powershell
curl.exe -s http://127.0.0.1:8000/api/status
curl.exe -s http://127.0.0.1:8000/metrics
```

可选环境变量（启动 backend 前设置）：

```powershell
$env:LLM_BACKEND="offline"        # offline / local / ollama / auto
$env:API_TOKEN="your-token"       # 非空则写接口需要 Bearer token
$env:ANALYSIS_TIMEOUT_S="90"      # 超时降级为离线模板
$env:APPROVAL_TIMEOUT_S="600"     # 待审批超时写告警
$env:ALERT_WEBHOOK="http://..."   # 告警外发地址（可选）
```

关键字段：

| 字段 | 期望 |
|---|---|
| `mqtt.connected` | `true` |
| `analyzer` | `local-qwen` / `ollama` / `offline-template` |
| `checkpointer` | `sqlite` |
| `devices` | 有心跳时能看到 `line1-dev-01`，`online: true` |

前端能打开且状态条显示「实时流已连接」即为全通。

---

## 3. 停止（按终端 Ctrl+C，或按进程）

```powershell
# 逐个终端 Ctrl+C 最干净
# 批量兜底：
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'backend_server|rule_engine|sensor_simulator|modbus_bridge' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match 'next' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# broker 若是服务（需管理员）
Stop-Service mosquitto
```

---

## 4. 可选：Modbus 仪表

```powershell
# 无硬件，模拟读数（与真实链路完全一致）
python -X utf8 -u modbus_bridge.py --fake

# 真实仪表：先改 config/devices.json 的 host/port/寄存器地址/量程
python -X utf8 -u modbus_bridge.py
```

---

## 5. 接 ESP32-S3 硬件（MQ-4 / DS18B20 / MPU6050）

完整接线、分压、标定与排障见 **[README-HARDWARE.md](README-HARDWARE.md)**。最短路径：

```powershell
# 1) broker 已在 0.0.0.0:1883 且防火墙放行（见终端 1）
# 2) 烧固件：firmware/esp32_s3_gateway，先把 config.example.h 复制成 config.h 并填 WiFi/MQTT_HOST
# 3) 自检 + 标定
python scripts\hw_check.py --live
python scripts\hw_check.py --calibrate --seconds 60
# 4) 一键起全套（不启仿真器，规则用 hardware 那套）
.\START.cmd -Hardware
```

`/api/status` 的 `devices` 里能看到 `esp32s3-line1-01` 以及它上报的 `ip / rssi / fw`；
断线时固件的 MQTT LWT 会让后端在 1 秒内标成离线并生成 `R-DEV-OFFLINE` 事件。

---

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `[WinError 10048]` 端口被占 | 先杀旧的 8000/3000 进程，或 `netstat -ano \| findstr :8000` |
| 页面「实时流断开」 | 后端 8000 没起，或被杀；重启 `backend_server.py` |
| 状态条「MQTT 未连接」 | broker 没起 / 服务已停 |
| 分析很久不出 | 本地 3B CPU 约 55–100 秒；彩排用 `LLM_BACKEND=offline` |
| 批准报 409 | 该事件已批准/驳回；换一条事件，或看工单表 |
| broker 服务改不了 | 必须管理员 PowerShell 执行 `Set-Service` |
