/*
 * 安全监测 Agent —— ESP32-S3 现场网关固件
 * ========================================
 * 硬件（BOM 见 README-HARDWARE.md）：
 *   ESP32-S3 开发板          现场网关，Wi-Fi 直连 MQTT
 *   MQ-4 甲烷模块            模拟量 → 换算成体积分数 %
 *   DS18B20 温度探头          1-Wire → ℃
 *   MPU6050（默认）或 SW-420   I2C 加速度 → 振动速度 mm/s（或开关量）
 *   面包板 + 杜邦线 + USB 电源
 *
 * 发布出去的消息与 sensor_simulator.py / modbus_bridge.py 完全同构，
 * 所以 rule_engine.py、backend_server.py、看板都不用改一行：
 *
 *   line1/telemetry  {"ts","line","device_id","sensor","value","unit","tick","scenario_id","src"}
 *   line1/heartbeat  {"ts","device_id","status":"online","ip","rssi","fw","uptime_s"}  (retained)
 *
 * 关键工程点：
 *   1. MQ-4 的 AO 是 0–5 V，ESP32 ADC 上限约 3.3 V —— **必须加分压**，
 *      默认 10k/10k 对半分压，MQ4_ADC_DIVIDER=2.0。
 *   2. 用 ADC1（GPIO1），ADC2 与 Wi-Fi 冲突，不能用。
 *   3. MQ-4 加热丝要 5 V / ~150 mA，VIN/USB 供电；上电需预热（越久越稳，
 *      理想 24 h 老化），刚上电读数是漂的。
 *   4. 心跳用 retained + LWT：后端一订阅就知道设备在不在线，断线 1 秒内可见。
 *   5. 拿不到 NTP 时间时 ts 留空，服务端（rule_engine）会用自己的时间戳兜底。
 *
 * 依赖库（Arduino IDE → 库管理器）：
 *   PubSubClient            (Nick O'Leary)
 *   OneWire                 (Paul Stoffregen)
 *   DallasTemperature       (Miles Burton)
 *   —— MPU6050 用裸 I2C 驱动，不需要额外库
 */

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <time.h>
#include <math.h>

// ---------------------------------------------------------------- 配置
// 把 config.example.h 复制成 config.h 再填 WiFi 与 MQTT；config.h 已被 .gitignore
#if __has_include("config.h")
#include "config.h"
#else
#error "缺少 config.h：请把 config.example.h 复制成 config.h，并填写 WIFI_SSID / WIFI_PASS / MQTT_HOST"
#endif

#ifndef LINE_ID
#define LINE_ID "line1"
#endif
#ifndef DEVICE_ID
#define DEVICE_ID "esp32s3-line1-01"
#endif
#ifndef TELEMETRY_TOPIC
#define TELEMETRY_TOPIC "line1/telemetry"
#endif
#ifndef HEARTBEAT_TOPIC
#define HEARTBEAT_TOPIC "line1/heartbeat"
#endif

#ifndef PUBLISH_INTERVAL_MS
#define PUBLISH_INTERVAL_MS 1000
#endif
#ifndef HEARTBEAT_INTERVAL_MS
#define HEARTBEAT_INTERVAL_MS 2000
#endif

// ---- MQ-4 标定 ----
#ifndef MQ4_ADC_PIN
#define MQ4_ADC_PIN 1          // GPIO1 = ADC1_CH0
#endif
#ifndef MQ4_VCC
#define MQ4_VCC 5.0f           // 模块供电电压
#endif
#ifndef MQ4_RL_OHM
#define MQ4_RL_OHM 10000.0f    // 模块板载负载电阻
#endif
#ifndef MQ4_R0_OHM
// R0 是「1000 ppm 时的 Rs」= 洁净空气 Rs / 4.4，不是洁净空气的 Rs。
// 用 scripts/hw_check.py --calibrate 得到实际值后填进 config.h。
#define MQ4_R0_OHM 3000.0f
#endif
#ifndef MQ4_ADC_DIVIDER
#define MQ4_ADC_DIVIDER 2.0f   // Vout = Vadc × 该系数（10k/10k）
#endif
#ifndef MQ4_CURVE_A
#define MQ4_CURVE_A 1000.0f    // ppm = A × (Rs/R0)^B
#endif
#ifndef MQ4_CURVE_B
#define MQ4_CURVE_B -2.19f
#endif
#ifndef MQ4_CLEAN_AIR_PCT
#define MQ4_CLEAN_AIR_PCT 0.0f // 洁净空气零点偏移（扣掉本底）
#endif

// ---- DS18B20 ----
#ifndef DS18B20_PIN
#define DS18B20_PIN 4
#endif

// ---- 振动 ----
#ifndef VIBRATION_MODE_MPU6050
#define VIBRATION_MODE_MPU6050 1   // 0 = 改用 SW-420 开关量
#endif
#ifndef MPU_SDA_PIN
#define MPU_SDA_PIN 8
#endif
#ifndef MPU_SCL_PIN
#define MPU_SCL_PIN 9
#endif
#ifndef VIB_FREQ_HZ
#define VIB_FREQ_HZ 25.0f          // 积分用的主频假设，mm/s ≈ a_rms/(2πf)
#endif
#ifndef VIB_WINDOW_SAMPLES
#define VIB_WINDOW_SAMPLES 40
#endif
#ifndef VIB_SAMPLE_MS
#define VIB_SAMPLE_MS 10
#endif
#ifndef SW420_PIN
#define SW420_PIN 5
#endif
#ifndef SW420_TRIGGER_MM_S
#define SW420_TRIGGER_MM_S 6.0f
#endif

#ifndef LED_PIN
#define LED_PIN 2
#endif
#ifndef NTP_SERVER1
#define NTP_SERVER1 "ntp.aliyun.com"
#endif
#ifndef NTP_SERVER2
#define NTP_SERVER2 "pool.ntp.org"
#endif
#ifndef TZ_OFFSET_SEC
#define TZ_OFFSET_SEC (8 * 3600)
#endif
#ifndef FW_VERSION
#define FW_VERSION "1.0.0"
#endif

#ifndef MQTT_USER
#define MQTT_USER ""
#endif
#ifndef MQTT_PASS
#define MQTT_PASS ""
#endif

// ---------------------------------------------------------------- 全局
static WiFiClient netClient;
static PubSubClient mqtt(netClient);
static OneWire oneWire(DS18B20_PIN);
static DallasTemperature ds18b20(&oneWire);

static bool g_mpu_ok = false;
static bool g_ds_ok = false;
static bool g_has_mqtt_user = false;
static bool g_wifi_time_synced = false;

static uint32_t g_tick = 0;
static uint32_t g_last_publish = 0;
static uint32_t g_last_heartbeat = 0;
static uint32_t g_last_reconnect = 0;
static float g_last_temp_c = NAN;
static uint32_t g_sw420_hold_until = 0;   // millis 值：SW-420 触发后的保持窗口
static uint32_t g_publish_errors = 0;

static const uint8_t MPU_ADDR = 0x68;

// ---------------------------------------------------------------- 小工具
static String fnum(float value, int decimals) {
  if (isnan(value) || isinf(value)) return String("0");
  return String(value, decimals);
}

/** ISO8601 带本地时区偏移；NTP 未同步时返回空串（服务端兜底）。

 * 用 gmtime + 手工加偏移，而不是依赖 TZ 环境变量：TZ 字符串的符号约定
 * （CST-8 表示 UTC+8）很容易写反，而且和 TZ_OFFSET_SEC 两处配置会漂移。
 */
static String iso8601_now() {
  time_t now = time(nullptr);
  if (now < 1600000000) return String("");
  time_t shifted = now + (time_t)TZ_OFFSET_SEC;
  struct tm tmv;
  gmtime_r(&shifted, &tmv);
  char buf[24];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tmv);
  int off_min = (int)(TZ_OFFSET_SEC / 60);
  char zone[8];
  int hh = off_min / 60, mm = off_min % 60;
  if (mm < 0) mm = -mm;
  snprintf(zone, sizeof(zone), "%+03d:%02d", hh, mm);
  return String(buf) + zone;
}

// ---------------------------------------------------------------- MPU6050
static bool i2c_write_reg(uint8_t addr, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool i2c_read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, (int)len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = (uint8_t)Wire.read();
  return true;
}

static bool mpu_init() {
  uint8_t who = 0;
  if (!i2c_read_regs(MPU_ADDR, 0x75, &who, 1)) return false;
  if (who != 0x68 && who != 0x70) return false;   // 0x70 = 部分兼容克隆片
  i2c_write_reg(MPU_ADDR, 0x6B, 0x80);            // 复位
  delay(100);
  i2c_write_reg(MPU_ADDR, 0x6B, 0x00);            // 唤醒
  delay(10);
  i2c_write_reg(MPU_ADDR, 0x1C, 0x00);            // ±2 g
  i2c_write_reg(MPU_ADDR, 0x1A, 0x03);            // DLPF ≈ 44 Hz
  return true;
}

/** 读三轴加速度，返回静止时约等于 1.0 g 的合矢量。 */
static bool mpu_read_g(float &ax, float &ay, float &az) {
  uint8_t raw[6];
  if (!i2c_read_regs(MPU_ADDR, 0x3B, raw, 6)) return false;
  int16_t rx = (int16_t)((raw[0] << 8) | raw[1]);
  int16_t ry = (int16_t)((raw[2] << 8) | raw[3]);
  int16_t rz = (int16_t)((raw[4] << 8) | raw[5]);
  const float scale = 16384.0f;   // ±2 g 量程
  ax = rx / scale;
  ay = ry / scale;
  az = rz / scale;
  return true;
}

/**
 * 振动速度有效值（mm/s）。
 * MPU6050 给的是加速度，这里用「去重力后的加速度 RMS ÷ 2πf」做工程近似，
 * 只为驱动演示规则（阈值 4.5 mm/s），不是计量级振动烈度计。
 */
static float measure_vibration_mm_s() {
  if (!g_mpu_ok) return 0.0f;
  double sum_sq = 0.0;
  int good = 0;
  for (int i = 0; i < VIB_WINDOW_SAMPLES; i++) {
    float ax, ay, az;
    if (mpu_read_g(ax, ay, az)) {
      float mag = sqrtf(ax * ax + ay * ay + az * az);
      float dyn = mag - 1.0f;          // 去掉重力分量
      sum_sq += (double)dyn * (double)dyn;
      good++;
    }
    delay(VIB_SAMPLE_MS);
  }
  if (good < 4) return 0.0f;
  float a_rms_g = sqrtf((float)(sum_sq / good));
  float a_rms_si = a_rms_g * 9.80665f;                 // m/s²
  float v_ms = a_rms_si / (2.0f * PI * VIB_FREQ_HZ);   // mm/s
  return v_ms * 1000.0f;
}

static float measure_sw420_mm_s() {
  int level = digitalRead(SW420_PIN);
  bool triggered = (level == LOW);        // SW-420 常开，触发时拉低
  if (triggered) {
    g_sw420_hold_until = millis() + 500;
    return SW420_TRIGGER_MM_S;
  }
  if (millis() < g_sw420_hold_until) return SW420_TRIGGER_MM_S;
  return 0.2f;                            // 静止时的底噪
}

// ---------------------------------------------------------------- MQ-4
/** 返回 {体积分数 %, 原始 mV, Rs}；读不到就返回 0。 */
static void read_mq4(float &pct, float &mv_out, float &rs_out) {
  mv_out = (float)analogReadMilliVolts(MQ4_ADC_PIN);
  float vadc = mv_out / 1000.0f;
  float vout = vadc * MQ4_ADC_DIVIDER;            // 还原分压前的模块输出
  if (vout < 0.05f) vout = 0.05f;                 // 断线/无输出：按“洁净”处理
  if (vout > MQ4_VCC - 0.05f) vout = MQ4_VCC - 0.05f;
  float rs = MQ4_RL_OHM * (MQ4_VCC - vout) / vout;
  float ratio = rs / MQ4_R0_OHM;
  float ppm = MQ4_CURVE_A * powf(ratio, MQ4_CURVE_B);
  pct = ppm / 10000.0f - MQ4_CLEAN_AIR_PCT;       // 1 % vol = 10000 ppm
  if (pct < 0.0f) pct = 0.0f;
  rs_out = rs;
}

// ---------------------------------------------------------------- 发布
static void publish_sensor(const char *sensor, float value, const char *unit,
                           const String &extra) {
  String payload;
  payload.reserve(320);
  payload += "{\"ts\":\"" + iso8601_now() + "\"";
  payload += ",\"line\":\"" LINE_ID "\"";
  payload += ",\"device_id\":\"" DEVICE_ID "\"";
  payload += ",\"sensor\":\"" + String(sensor) + "\"";
  payload += ",\"value\":" + fnum(value, 3);
  payload += ",\"unit\":\"" + String(unit) + "\"";
  payload += ",\"tick\":" + String(g_tick);
  payload += ",\"scenario_id\":\"hardware\"";
  payload += ",\"src\":\"esp32s3\"";
  payload += extra;
  payload += "}";

  if (!mqtt.publish(TELEMETRY_TOPIC, payload.c_str(), false)) {
    g_publish_errors++;
    Serial.printf("[mqtt] 发布失败 sensor=%s (累计 %lu)\n", sensor,
                  (unsigned long)g_publish_errors);
  }
}

static void publish_heartbeat(const char *status, const char *reason) {
  String payload;
  payload.reserve(256);
  payload += "{\"ts\":\"" + iso8601_now() + "\"";
  payload += ",\"device_id\":\"" DEVICE_ID "\"";
  payload += ",\"status\":\"" + String(status) + "\"";
  payload += ",\"source\":\"esp32s3\"";
  payload += ",\"fw\":\"" FW_VERSION "\"";
  payload += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
  payload += ",\"rssi\":" + String(WiFi.RSSI());
  payload += ",\"uptime_s\":" + String((unsigned long)(millis() / 1000));
  if (reason && reason[0]) payload += ",\"reason\":\"" + String(reason) + "\"";
  payload += "}";
  // retained：后端刚订阅就能看到最后已知状态，LWT 离线也能立刻覆盖
  mqtt.publish(HEARTBEAT_TOPIC, payload.c_str(), true);
}

// ---------------------------------------------------------------- 联网
static void ensure_wifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - g_last_reconnect < 5000) return;
  g_last_reconnect = millis();
  Serial.printf("[wifi] 连接 %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // 关省电，避免上报抖动
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

static void ensure_time() {
  if (g_wifi_time_synced) return;
  if (WiFi.status() != WL_CONNECTED) return;
  // 时区偏移由 TZ_OFFSET_SEC 在 iso8601_now() 里手工处理，这里只做授时
  configTime(0, 0, NTP_SERVER1, NTP_SERVER2);
  g_wifi_time_synced = true;
}

static void ensure_mqtt() {
  if (mqtt.connected()) return;
  if (WiFi.status() != WL_CONNECTED) return;
  if (millis() - g_last_reconnect < 3000) return;
  g_last_reconnect = millis();

  char clientId[48];
  snprintf(clientId, sizeof(clientId), "esp32s3-%s", DEVICE_ID);
  char will[160];
  snprintf(will, sizeof(will),
           "{\"device_id\":\"%s\",\"status\":\"offline\",\"reason\":\"lwt\",\"source\":\"esp32s3\"}",
           DEVICE_ID);

  Serial.printf("[mqtt] 连接 %s:%d ...\n", MQTT_HOST, MQTT_PORT);
  bool ok = g_has_mqtt_user
                ? mqtt.connect(clientId, MQTT_USER, MQTT_PASS, HEARTBEAT_TOPIC, 1, true, will)
                : mqtt.connect(clientId, nullptr, nullptr, HEARTBEAT_TOPIC, 1, true, will);
  if (ok) {
    Serial.println("[mqtt] 已连接");
    publish_heartbeat("online", "connect");
    g_last_heartbeat = millis();
  } else {
    Serial.printf("[mqtt] 失败 rc=%d（-2 连不上 broker，检查 PC 防火墙/局域网）\n",
                  mqtt.state());
  }
}

// ---------------------------------------------------------------- setup/loop
void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // MQ-4：12 位 + 默认 11 dB 衰减（满量程约 3.1 V）。
  // 不写死 ADC_11db / ADC_ATTEN_DB_12，避免 Arduino-ESP32 2.x 与 3.x 的枚举改名冲突。
  analogReadResolution(12);

  // DS18B20
  ds18b20.begin();
  g_ds_ok = ds18b20.getDeviceCount() > 0;
  ds18b20.setWaitForConversion(false);   // 异步：本周期发起，下周期读

  // 振动
#if VIBRATION_MODE_MPU6050
  Wire.begin(MPU_SDA_PIN, MPU_SCL_PIN, 400000);
  g_mpu_ok = mpu_init();
  Serial.printf("[mpu6050] %s\n", g_mpu_ok ? "ok" : "未检测到（检查 SDA/SCL/供电）");
#else
  pinMode(SW420_PIN, INPUT);
  Serial.println("[sw420] 开关量模式");
#endif

  g_has_mqtt_user = (strlen(MQTT_USER) > 0);

  Serial.printf("\n=== 安全监测网关 %s ===\n", FW_VERSION);
  Serial.printf("device_id=%s broker=%s:%d\n", DEVICE_ID, MQTT_HOST, MQTT_PORT);
  Serial.printf("mq4: pin=%d r0=%.0f divider=%.2f | ds18b20: pin=%d %s\n",
                MQ4_ADC_PIN, MQ4_R0_OHM, MQ4_ADC_DIVIDER, DS18B20_PIN,
                g_ds_ok ? "ok" : "未检测到");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(15);
  mqtt.setBufferSize(512);

  ensure_time();
  ds18b20.requestTemperatures();   // 先发一次，1 秒后才有值
}

void loop() {
  ensure_wifi();
  ensure_time();
  ensure_mqtt();
  mqtt.loop();

  uint32_t now = millis();

  if (now - g_last_publish >= (uint32_t)PUBLISH_INTERVAL_MS) {
    g_last_publish = now;
    g_tick++;

    // --- 温度（读上一轮发起的那次转换）---
    if (g_ds_ok) {
      float t = ds18b20.getTempCByIndex(0);
      if (t > -50.0f && t < 120.0f && fabsf(t - 85.0f) > 0.001f) {
        g_last_temp_c = t;
      }
      ds18b20.requestTemperatures();   // 为下一轮发起
    }
    if (!isnan(g_last_temp_c)) {
      publish_sensor("temp", g_last_temp_c, "C", "");
    }

    // --- 甲烷 ---
    float pct = 0.0f, mv = 0.0f, rs = 0.0f;
    read_mq4(pct, mv, rs);
    String raw = ",\"raw\":{\"mv\":" + fnum(mv, 0) + ",\"rs\":" + fnum(rs, 0) +
                 ",\"ppm\":" + fnum(pct * 10000.0f, 0) + "}";
    publish_sensor("ch4", pct, "%", raw);

    // --- 振动 ---
#if VIBRATION_MODE_MPU6050
    float vib = measure_vibration_mm_s();
#else
    float vib = measure_sw420_mm_s();
#endif
    publish_sensor("vibration", vib, "mm/s", "");

    if (mqtt.connected()) digitalWrite(LED_PIN, HIGH);
    Serial.printf("[pub] ch4=%.3f%% temp=%.1fC vib=%.2fmm/s rssi=%d\n",
                  pct, isnan(g_last_temp_c) ? -999.0f : g_last_temp_c, vib, WiFi.RSSI());
  }

  if (now - g_last_heartbeat >= (uint32_t)HEARTBEAT_INTERVAL_MS) {
    g_last_heartbeat = now;
    if (mqtt.connected()) {
      publish_heartbeat("online", "");
      digitalWrite(LED_PIN, LOW);
    }
  }
}
