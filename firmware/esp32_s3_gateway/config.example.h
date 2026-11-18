/*
 * ESP32-S3 网关配置模板
 * ======================
 * 用法：复制本文件为同目录下的 config.h，按你的现场填写。
 *      cp config.example.h config.h
 * config.h 含 Wi-Fi 口令，已在 .gitignore 中排除，不要提交。
 */
#pragma once

// ---------------------------------------------------------------- 网络
// 2.4 GHz 频段；ESP32-S3 不支持 5 GHz
#define WIFI_SSID "你的WiFi名"
#define WIFI_PASS "你的WiFi密码"

// 运行 Mosquitto 的那台 PC 的局域网 IP（不是 127.0.0.1）。
// 在 PC 上执行 ipconfig，找「无线局域网适配器 WLAN → IPv4 地址」。
#define MQTT_HOST "192.168.1.10"
#define MQTT_PORT 1883
#define MQTT_USER ""    // broker 开了密码认证才填
#define MQTT_PASS ""

// ---------------------------------------------------------------- 身份
#define DEVICE_ID "esp32s3-line1-01"
#define LINE_ID "line1"

#define TELEMETRY_TOPIC "line1/telemetry"
#define HEARTBEAT_TOPIC "line1/heartbeat"

#define PUBLISH_INTERVAL_MS 1000
#define HEARTBEAT_INTERVAL_MS 2000

// ---------------------------------------------------------------- MQ-4 标定
// ADC 引脚必须是 ADC1（GPIO1–GPIO10）；ADC2 与 Wi-Fi 冲突。
#define MQ4_ADC_PIN 1
#define MQ4_VCC 5.0f
#define MQ4_RL_OHM 10000.0f
// R0 的口径是「1000 ppm 时的 Rs」= 洁净空气 Rs / 4.4，**不是**洁净空气的 Rs。
// 用 scripts/hw_check.py --calibrate 在洁净空气里跑一次就能得到建议值。
#define MQ4_R0_OHM 3000.0f
// 分压系数：Vout = Vadc × 该值。10k/10k 对半分压就是 2.0。
#define MQ4_ADC_DIVIDER 2.0f
// ppm = A × (Rs/R0)^B；DEFAULT 来自数据手册曲线的两点拟合，换批次可重新标定。
#define MQ4_CURVE_A 1000.0f
#define MQ4_CURVE_B -2.19f
// 洁净空气本底（% vol）。跑一次 --calibrate 会给出建议值。
#define MQ4_CLEAN_AIR_PCT 0.0f

// ---------------------------------------------------------------- DS18B20
#define DS18B20_PIN 4

// ---------------------------------------------------------------- 振动
// 1 = MPU6050（I2C，能画曲线）；0 = SW-420（开关量，只报“振了/没振”）
#define VIBRATION_MODE_MPU6050 1
#define MPU_SDA_PIN 8
#define MPU_SCL_PIN 9
// 积分近似用的主频假设：mm/s ≈ a_rms / (2πf)。桌面敲击取 25 Hz 比较合适，
// 真实机泵按工频（50 Hz 机器取 50）调。
#define VIB_FREQ_HZ 25.0f
#define VIB_WINDOW_SAMPLES 40
#define VIB_SAMPLE_MS 10

#define SW420_PIN 5
#define SW420_TRIGGER_MM_S 6.0f   // 触发时上报的等效值，仅用于驱动演示规则

#define LED_PIN 2

// ---------------------------------------------------------------- 时间
// ts 用本地时区偏移；NTP 不通时留空，服务端会用它自己的时间戳兜底。
#define NTP_SERVER1 "ntp.aliyun.com"
#define NTP_SERVER2 "pool.ntp.org"
#define TZ_OFFSET_SEC (8 * 3600)
