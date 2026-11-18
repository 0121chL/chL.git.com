# 接真实硬件：ESP32-S3 + MQ-4 + DS18B20 + MPU6050

把"全仿真"链路换成"真传感器 → 真 MQTT"的现场网关。上层（规则引擎、LangGraph、审批、看板）一行不用改，
因为固件发出去的报文和 `modbus_bridge.py` / `sensor_simulator.py` 完全同构。

```
MQ-4 ─┐
DS18B20 ─┼─ ESP32-S3 ──WiFi(MQTT)──> Mosquitto ──> rule_engine ──> backend ──> 看板
MPU6050 ─┘                              (PC)        判定           Agent+审批
```

---

## 0. 这次为硬件改了哪些东西

| 文件 | 作用 |
|---|---|
| `firmware/esp32_s3_gateway/esp32_s3_gateway.ino` | 固件：采样、换算、发布、LWT、NTP、重连 |
| `firmware/esp32_s3_gateway/config.example.h` | 配置模板（Wi-Fi / broker / 引脚 / 标定），复制成 `config.h` |
| `config/hardware.json` | 接线与标定的单一记录，`hw_check.py` 用它校验上报 |
| `config/rules.hardware.json` | 硬件版规则：只有 ch4 / temp / vibration，阈值仍取规程值 |
| `sensor_calibration.py` | MQ-4 的 Rs→ppm→%、MPU6050 的 a→mm/s，以及标定 CLI |
| `scripts/hw_check.py` | 板子烧好后的第一件事：校验报文/单位/速率/心跳，并给标定值 |
| `sensor_simulator.py --profile hardware` | 没有硬件时扮演同一块板子（报文与固件一致） |
| `backend_server.py` | 支持心跳 `status=offline`（LWT 秒级离线）、设备信息展示 |
| `config/mosquitto.conf` | 监听改成 `0.0.0.0`，板子才连得进来 |

---

## 1. BOM 与接线

| 模块 | 接到 | 说明 |
|---|---|---|
| ESP32-S3 开发板 | USB 5V | 现场网关，2.4 GHz Wi-Fi |
| MQ-4 VCC | **5V / VIN** | 加热丝约 150 mA，接 3.3V 会读数偏低 |
| MQ-4 GND | GND | |
| MQ-4 AO | **分压后** → GPIO1 | ⚠️ AO 最高 5 V，直连会烧 ADC |
| DS18B20 VDD / GND | 3V3 / GND | |
| DS18B20 DQ | GPIO4 | DQ 与 3V3 之间加 **4.7 kΩ 上拉** |
| MPU6050 VCC / GND | 3V3 / GND | 不要接 5 V |
| MPU6050 SDA / SCL | GPIO8 / GPIO9 | 模块自带 I²C 上拉即可 |
| SW-420 VCC / DO | 3V3 / GPIO5 | 仅在 `VIBRATION_MODE_MPU6050 0` 时使用 |
| 状态 LED | GPIO2 | 发布成功时闪，便于判断在不在跑 |

分压（MQ-4 AO → ADC）：

```
MQ-4 AO ──[ 10kΩ ]──┬── GPIO1 (ADC1_CH0)
                    │
                 [ 10kΩ ]
                    │
                   GND
```

`Vout(模块) = Vadc × 2`，对应固件里的 `MQ4_ADC_DIVIDER 2.0`。
**必须用 ADC1（GPIO1–GPIO10）**：ESP32-S3 的 ADC2 与 Wi-Fi 冲突。

---

## 2. 让板子能连上 broker

1. 改 `config/mosquitto.conf`（已改好）：`listener 1883 0.0.0.0`。
   只监听 `127.0.0.1` 时，手机和开发板永远连不上——这是最常见的坑。
2. **如果用的是 Windows 服务**（`Start-Service mosquitto`）：服务读的是它自己那份
   `C:\Program Files\mosquitto\mosquitto.conf`，**不是**项目里的这份。两个办法：
   - 把项目这份的内容合并进服务那份，然后 `Restart-Service mosquitto`；
   - 或者不走服务，直接用项目配置前台启动：
     `& "C:\Program Files\mosquitto\mosquitto.exe" -c config\mosquitto.conf -v`
     （先 `Stop-Service mosquitto`，否则端口冲突）。

   `scripts/hw_check.py` 会自动探测这件事：只要 127.0.0.1 通而 LAN 地址不通，就会直接提示。
3. 放行防火墙（管理员 PowerShell，只需一次）：

```powershell
New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow
```

4. 查本机局域网 IP，填进固件的 `MQTT_HOST`：

```powershell
ipconfig | Select-String "IPv4"
```

5. 在**另一台设备**上验证（手机装个 MQTT 客户端也行）：

```powershell
Test-NetConnection 192.168.1.10 -Port 1883   # TcpTestSucceeded 应为 True
```

---

## 3. 烧录固件

> **本机（Windows，用户名含中文）请直接用 `firmware\build.ps1`，不要用 IDE 的「验证 / 上传」**——
> 原因见 §10：IDE 在这台机器上会卡在链接阶段。首次先跑 `.\build.ps1 -Setup`。

Arduino IDE 里装三个库（库管理器搜索）：`PubSubClient`、`OneWire`、`DallasTemperature`。
MPU6050 用裸 I²C 驱动，不需要额外库。（本机这三个库已经装好了。）

```powershell
cd firmware\esp32_s3_gateway
copy config.example.h config.h      # 然后改 WiFi / MQTT_HOST
```

`config.h` 里至少要改这几行：

```c
#define WIFI_SSID  "你的WiFi名"
#define WIFI_PASS  "你的WiFi密码"
#define MQTT_HOST  "192.168.1.10"   // 跑 Mosquitto 的那台 PC，不是 127.0.0.1
#define DEVICE_ID  "esp32s3-line1-01"
```

板子选 **ESP32S3 Dev Module**，Flash 4 MB 以上；烧完打开串口监视器 115200，正常应看到：

```
=== 安全监测网关 1.0.0 ===
device_id=esp32s3-line1-01 broker=192.168.1.10:1883
mq4: pin=1 r0=3000 divider=2.00 | ds18b20: pin=4 ok
[mpu6050] ok
[mqtt] 已连接
[pub] ch4=0.004% temp=25.3C vib=0.21mm/s rssi=-52
```

`config.h` 已加入 `.gitignore`（里面是 Wi-Fi 口令）。

---

## 4. 标定 MQ-4（这一步不能省）

MQ-4 输出的是模拟电压，必须换算成体积分数；**换算是否正确取决于 R0**。

### 4.1 口径别搞反

```
Rs    = RL × (Vcc − Vout) / Vout
R0    = 1000 ppm 时的 Rs = 洁净空气 Rs / 4.4     ← 数据手册的定义
ppm   = 1000 × (Rs/R0)^-2.19
%vol  = ppm / 10000                              ← 甲烷 1 %vol = 10000 ppm
```

把洁净空气的 Rs 直接当 R0 是最常见的错，会让读数整体偏大约一个量级。

### 4.2 操作

1. 板子上电预热 **至少 5 分钟**（MQ-4 冷机漂移很大，理想老化 24 h），放在通风、洁净的室内；
2. 跑：

```powershell
python scripts\hw_check.py --calibrate --seconds 60
```

3. 把输出两行写进 `config.h`，重新烧录：

```c
#define MQ4_R0_OHM 3140.0f
#define MQ4_CLEAN_AIR_PCT 0.0038f
```

4. 复查：

```powershell
python scripts\hw_check.py --seconds 20
```

洁净空气里 `ch4` 应稳定在 `0.000%` 附近（±0.003%）。

> `sensor_calibration.py` 也是同一套算法的参考实现，可直接 `python sensor_calibration.py --table` 看
> ADC → %vol 对照表；固件里的算法与它逐行对应，改一处要同步改另一处。

---

## 5. 跑起来

```powershell
# 硬件模式：只起 broker/后端/规则引擎/看板，规则换成 hardware 那套，不起仿真器
.\START.cmd -Hardware

# 板子自检（另开一个终端）
python scripts\hw_check.py --live
```

看板 `GET /api/status` 里能看到设备的上报信息：

```json
"devices": [
  { "device_id": "esp32s3-line1-01", "online": true, "silent_seconds": 1.2,
    "info": { "ip": "192.168.1.23", "rssi": -52, "fw": "1.0.0", "source": "esp32s3" } }
]
```

没有硬件时，先用同一份报文把链路跑通：

```powershell
python sensor_simulator.py --profile hardware --inject hw_gas
```

---

## 6. 现场演示怎么触发

| 动作 | 预期读数 | 会命中哪条规则 |
|---|---|---|
| 打火机气对着 MQ-4（3–5 秒） | ch4 由 0% 抬到 0.3–2%（看距离） | 1 分钟内涨 ≥0.5 → `R-CH4-RISE`；持续 >1.0% → `R-CH4-HIGH` |
| 手捏 / 哈气 DS18B20 | 25 → 30~36 ℃ | 到不了 85 ℃，**只会画曲线不报警**（要演示温度告警就把 `config/rules.hardware.json` 里的 85 改成 32，并明白这是演示值） |
| 拍一下桌子 | vibration 0.2 → 10~60 mm/s | >4.5 → `R-VIB-HIGH`（severity=medium） |
| 拔掉 USB / 关 Wi-Fi | 心跳停止 | LWT 秒级 → `status=offline` → `R-DEV-OFFLINE` |

阈值没有为了好看而改：`1.0% / 85℃ / 4.5 mm/s` 都是规程值，改了 AI 引用的条款会和事件对不上。

---

## 7. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 串口反复 `[mqtt] 失败 rc=-2` | broker 只监听 127.0.0.1 / 防火墙 | 改 `0.0.0.0`、加防火墙规则、确认 `MQTT_HOST` 是 LAN IP |
| 串口 `rc=-3` | broker 拒绝连接（认证/ClientID 冲突） | 关掉其它同名客户端；检查 `allow_anonymous` |
| `hw_check.py` 一帧都收不到 | 板子没连上 / topic 不一致 / `DEVICE_ID` 不同 | 先看串口，再对 `config/hardware.json` 的 topic |
| ch4 恒为 0 且 `raw.mv = 0` | AO 没接、分压断、模块没供电 | 万用表量 AO 对 GND；确认 5 V 供电 |
| ch4 恒在 0.3–0.6% 不归零 | 没标定 / 预热不足 / 有酒精挥发物 | 重新 `--calibrate`，远离酒精、涂料 |
| ch4 读数贴顶 | 分压比不对 | 确认 10k/10k，`MQ4_ADC_DIVIDER 2.0` |
| temp 恒为 85.0 或 -127 | DS18B20 没接好 / 上拉缺失 | 补 4.7 kΩ 上拉，换三线接法 |
| 看板"设备离线" | 正常：心跳停了 | 恢复后 1–2 秒自动转在线 |
| 有遥测但不出事件 | 没到阈值 | 看 `data/logs/rule_engine.log`；用趋势规则或注气更久 |
| 时间戳空 | NTP 不通 | 不影响判定：rule_engine 用自己的时间落库 |

---

## 8. 这套硬件的边界（必须知道）

1. **MQ-4 是演示级传感器**，不是计量级甲烷检测仪：对酒精、丁烷、烟雾都敏感，长期漂移大，
   不能用于任何真实安全联锁。规程库本身也是模拟文本（见 `regulation_store.py` 开头）。
2. **DS18B20 测的是探头所在环境温度**，装在轴承上也要考虑导热与响应时间；手工加热到 85 ℃ 不现实。
3. **MPU6050 的 mm/s 是工程近似**（`a_rms / 2πf`，f 默认 25 Hz），不是 ISO 10816 的振动烈度计量。
   要真做，得上 IEPE 加速度计 + 频谱积分。
4. **MQ-4 断线会读成"洁净"**（AO 归零 → 0%）：这是这类模拟传感器的固有缺陷。`hw_check.py` 会用
   `raw.mv < 20` 把它标出来；上位机目前没有"传感器故障"事件类型，这是下一步该补的。
5. 板子到 broker 是**明文、匿名**的。演示局域网可以，接到真实网络前请开 `password_file` + TLS，
   并把 `ALLOW_LOCAL_INGEST` 关掉（见下）。

---

## 9. 相关的后端/安全变更

接硬件的同时顺手补了这些（因为它们都会被"真实部署"放大）：

- **读接口要登录**：`/api/events`、`/work_orders`、`/api/telemetry`、`/api/alerts`、`/events` 等
  现在都需要会话 token；`/api/status`、`/metrics` 只含聚合状态，仍免鉴权给探针。
- **SSE/WS 用短时票据**：`POST /api/auth/stream-ticket` 换票，`/api/stream?ticket=...`；
  浏览器 `EventSource` 不能带请求头，这是唯一干净的做法。
- **审批不再 fail-open**：`POST /api/approvals/{id}` 的 `decision` 变成必填，缺了直接 422。
- **事件入口信任边界可关**：反向代理/容器后面必须 `ALLOW_LOCAL_INGEST=0` + `API_TOKEN`，
  否则 `request.client.host` 恒为 127.0.0.1，任何人可伪造事件。
- **设备离线秒级可见**：固件用 MQTT LWT + retained 心跳，后端 `touch_device` 认 `status=offline`。
- 其他：事件状态贯通 `awaiting_approval`、`EVENTS` 加锁（看门狗不再被并发改字典打死）、
  分析默认 240 s 超时降级、spool 加死信目录、告警文件滚动。

新增环境变量：

```
ALLOW_LOCAL_INGEST=1     # 0 = 只认 API_TOKEN（放代理后面必须设 0）
TRUSTED_INGEST_HOSTS=127.0.0.1,::1,localhost
EVENT_WORKERS=4          # 事件处理线程池上限
ANALYSIS_TIMEOUT_S=240   # 超时自动降级为离线模板
ALERT_ROTATE_BYTES=8388608
STREAM_TICKET_TTL_S=600
```

---

## 10. 本机编译/烧录环境（2026-11 实测）

### 10.1 已经装好的（不用再装）

| 组件 | 版本 / 位置 | 状态 |
|---|---|---|
| Arduino IDE | 2.3.10，`E:\Arduino\Arduino IDE\` | ✅ |
| 内置 arduino-cli | 1.5.1，`…\resources\app\lib\backend\resources\arduino-cli.exe` | ✅ |
| ESP32 核心 | `esp32:esp32 3.3.10-cn`（含 `esp32s3` 板型定义） | ✅ |
| xtensa 工具链 | `esp-x32 2601-cn`（gcc 14.2.0） | ✅ |
| IDF 预编译库 | `esp32s3-libs 3.3.10-cn`（含 qio_qspi 等 6 个变体） | ✅ |
| esptool | 5.3.0（`esptool_py`），也能烧录 | ✅ |
| USB CDC 驱动 | Windows 内置 `usbser.inf`（ESP32-S3 原生 USB 口免驱） | ✅ |
| Arduino 库 | `PubSubClient 2.8` / `OneWire 2.3.8` / `DallasTemperature 4.0.6` | ✅ 已补装 |

### 10.2 这台机器独有的坑：中文用户名会让链接器失败

Windows 用户名是 `机械革命`，Arduino 数据目录落在
`C:\Users\机械革命\AppData\Local\Arduino15\...`。ESP32 的 MinGW 链接器（ld）
在**经由 g++ 调用**时打不开这个中文路径下的库，表现为：

```
ld.exe: cannot find -lbootloader_support: No such file or directory
ld.exe: cannot find -lspi_flash: No such file or directory
…
collect2.exe: error: ld returned 1 exit status
```

注意：**编译（gcc）是过的，只有链接（ld）挂**，所以看起来像"代码有问题"，其实是路径问题。
已实测验证：把 `-L` 换成 ASCII 路径后，这 5 个库立刻被找到，只剩工具链自带的
`-lm / -lstdc++ / -lgcc` 仍找不到 → 结论是**工具链自身也必须在 ASCII 路径下**。

因此 **Arduino IDE 里点「验证 / 上传」在这台机器上会失败**（IDE 用默认数据目录）。

### 10.3 解决办法：项目自带的 ASCII 工具链 + 构建脚本

已在本机搭好 `C:\esptoolchain`（约 1.7 GB，纯 ASCII 路径），内容是从 IDE 数据目录
复制出来的「核心 + 工具链 + 3 个库 + 索引」，与 IDE 里的那份互不影响。

```powershell
cd F:\deepseek\wenjian\safe-monitor-agent\firmware

.\build.ps1                  # 编译（首次会自动检查工具链）
.\build.ps1 -Setup           # 从 Arduino IDE 重建/修复 C:\esptoolchain
.\build.ps1 -Upload -Port COM5   # 编译并烧录
.\build.ps1 -Upload          # 自动探测串口再烧
```

已实测结果（本机真实编译）：

```
Sketch uses 971133 bytes (74%) of program storage space. Maximum is 1310720 bytes.
Global variables use 47420 bytes (14%) of dynamic memory.
固件: C:\esptoolchain\build\esp32_s3_gateway.ino.bin (949 KB)
```

### 10.4 还没装、等板子到了再处理

| 项目 | 说明 |
|---|---|
| CH340 / CH343 驱动 | 若板子的 UART 口用 WCH 芯片，需装 [WCH 驱动](https://www.wch.cn/downloads/CH341SER_EXE.html)；**原生 USB 口不用装** |
| CP210x 驱动 | 若用 Silicon Labs 桥接芯片，需装对应 VCP 驱动 |
| 板子本身 | 未插入，当前无任何 COM 口 |

判断方法：插上板子后在设备管理器看
- `USB 串行设备 (COMx)` → 原生 USB，免驱，直接能烧；
- `USB-SERIAL CH340 (COMx)` → 已装 CH340；
- `未知设备` / 带黄色感叹号 → 缺驱动，按上面表格装。

### 10.5 两个使用提醒

1. **74% 的 Flash 占用**是按默认 4 MB / 默认分区算的；若买的是 8 MB 版，可以改用
   `-Fqbn "esp32:esp32:esp32s3:FlashSize=8M,PartitionScheme=default_8MB"` 换来更大空间。
2. `firmware\esp32_s3_gateway\config.h` 里有 Wi-Fi 口令，已在 `.gitignore` 中；
   **不要**为了让别人 copy 方便就把真实口令提交上去。
