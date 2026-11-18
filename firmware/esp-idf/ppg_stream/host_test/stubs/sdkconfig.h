// Host-test stand-in for the sdkconfig.h that ESP-IDF generates from Kconfig.projbuild.
// Values = the Kconfig defaults; run.sh overrides single ones with -D, and the TEST_*
// switches select the other Kconfig choices.
#pragma once

#define CONFIG_PPG_DEVICE_ID "FNG01"
#ifdef TEST_DUAL
// dual-PPG glasses (sdkconfig.dual): a second MAX30102 on I2C port 1, no camera
#define CONFIG_PPG_SITE "glasses_l"
#define CONFIG_PPG_PPG2 1
#define CONFIG_PPG_PPG2_SITE "glasses_r"
#define CONFIG_PPG_PPG2_I2C_PORT 1
#define CONFIG_PPG_PPG2_I2C_SDA_GPIO 7
#define CONFIG_PPG_PPG2_I2C_SCL_GPIO 8
#else
#define CONFIG_PPG_SITE "finger"
#endif
#define CONFIG_PPG_BOARD_NAME "XIAO_ESP32S3"

#define CONFIG_PPG_I2C_PORT 0
#define CONFIG_PPG_I2C_SDA_GPIO 5
#define CONFIG_PPG_I2C_SCL_GPIO 6
#define CONFIG_PPG_I2C_FREQ_HZ 400000
#define CONFIG_PPG_I2C_INTERNAL_PULLUPS 1
#ifndef CONFIG_PPG_INT_GPIO
#define CONFIG_PPG_INT_GPIO -1
#endif
#define CONFIG_PPG_BUTTON_GPIO -1
#define CONFIG_PPG_STATUS_LED_GPIO 21
#define CONFIG_PPG_STATUS_LED_ACTIVE_LOW 1

#ifdef TEST_HR_MODE
#define CONFIG_PPG_LED_MODE_HR 1
#else
#define CONFIG_PPG_LED_MODE_SPO2 1
#define CONFIG_PPG_LED_IR_MA_X10 64
#endif
#ifndef CONFIG_PPG_SAMPLE_RATE_HZ
#define CONFIG_PPG_SAMPLE_RATE_HZ 400
#endif
#ifndef CONFIG_PPG_SAMPLE_AVG_N
#define CONFIG_PPG_SAMPLE_AVG_N 1
#endif
#define CONFIG_PPG_PULSE_WIDTH_US 411
#define CONFIG_PPG_ADC_RANGE_NA 16384
#define CONFIG_PPG_LED_RED_MA_X10 64
#ifndef CONFIG_PPG_FIFO_POLL_MS
#define CONFIG_PPG_FIFO_POLL_MS 5
#endif
#define CONFIG_PPG_FIFO_A_FULL 15

#if defined(TEST_UART)
#define CONFIG_PPG_LINK_UART 1
#define CONFIG_PPG_UART_NUM 1
#define CONFIG_PPG_UART_BAUD 921600
#define CONFIG_PPG_UART_TX_GPIO 7
#define CONFIG_PPG_UART_RX_GPIO 8
#elif defined(TEST_HYBRID)
// hybrid camera glasses: Wi-Fi + MQTT, USB gateway takeover, OV2640 (frames in the MQTT ring)
#define CONFIG_PPG_LINK_HYBRID 1
#define CONFIG_PPG_LINK_WIFI 1
#ifndef TEST_DUAL  // dual-PPG glasses: hybrid without the camera
#define CONFIG_PPG_CAMERA 1
#define CONFIG_PPG_CAM_SIZE_240 1
#define CONFIG_PPG_CAM_FPS 15
#define CONFIG_PPG_CAM_CROP 80
#define CONFIG_PPG_CAM_WINDOW 0
#define CONFIG_PPG_CAM_WIN_CLKDIV 1
#define CONFIG_PPG_CAM_EXPOSURE 300
#define CONFIG_PPG_CAM_GAIN 2
#define CONFIG_PPG_CAM_VFLIP 1
#define CONFIG_PPG_CAM_HMIRROR 1
#endif
#define CONFIG_PPG_MQTT_CAM_SHARE_PCT 50
#elif defined(TEST_MQTT)
#define CONFIG_PPG_LINK_MQTT 1
#elif defined(TEST_USB_BIN)
// camera glasses (sdkconfig.camera): USB binary link + OV2640
#define CONFIG_PPG_LINK_USB_BIN 1
#define CONFIG_PPG_CAMERA 1
#define CONFIG_PPG_CAM_SIZE_240 1
#define CONFIG_PPG_CAM_FPS 15
#define CONFIG_PPG_CAM_CROP 80
#define CONFIG_PPG_CAM_WINDOW 0
#define CONFIG_PPG_CAM_WIN_CLKDIV 1
#define CONFIG_PPG_CAM_EXPOSURE 300
#define CONFIG_PPG_CAM_GAIN 2
#define CONFIG_PPG_CAM_VFLIP 1
#define CONFIG_PPG_CAM_HMIRROR 1
#else
#define CONFIG_PPG_LINK_USB_SERIAL_JTAG 1
#endif
#define CONFIG_PPG_TX_BUFFER_BYTES 8192

#ifdef TEST_AUX
// protocol v2.1: auxiliary streams (demo "sweat"), and the camera's frame statistics on camera builds
#define CONFIG_PPG_AUX 1
#define CONFIG_PPG_AUX_DEMO 1
#ifdef CONFIG_PPG_CAMERA
#define CONFIG_PPG_CAMERA_EDGE_FEATURES 1
#endif
#endif

#define CONFIG_PPG_WIFI_SSID "UCSD-DEVICE"
#define CONFIG_PPG_WIFI_PASSWORD ""
#define CONFIG_PPG_WIFI_SSID2 "lab-hotspot"
#define CONFIG_PPG_WIFI_PASSWORD2 "secret"
#define CONFIG_PPG_WIFI_SSID3 ""
#define CONFIG_PPG_WIFI_PASSWORD3 ""
#define CONFIG_PPG_WIFI_DNS ""
#define CONFIG_PPG_WIFI_DNS2 ""
#define CONFIG_PPG_WIFI_DNS3 "192.168.1.99"
#ifdef TEST_EAP
#define CONFIG_PPG_WIFI_AUTH_EAP 1
#define CONFIG_PPG_WIFI_EAP_IDENTITY "anonymous"
#define CONFIG_PPG_WIFI_EAP_USERNAME ""
#define CONFIG_PPG_WIFI_EAP_VALIDATE_SERVER 1
#define CONFIG_PPG_WIFI_EAP_DOMAIN ""
#else
#define CONFIG_PPG_WIFI_AUTH_PSK 1
#endif
#define CONFIG_PPG_SNTP_SERVER "pool.ntp.org"
#define CONFIG_PPG_WIFI_TX_POWER_DBM 20
#define CONFIG_PPG_WIFI_HT20 1

#define CONFIG_PPG_MQTT_BROKER_URI "mqtts://mqtt.example.org:8883"
#define CONFIG_PPG_MQTT_TOPIC_PREFIX "nbe"
#ifdef TEST_CA_PEM
#define CONFIG_PPG_MQTT_CA_PEM 1
#else
#define CONFIG_PPG_MQTT_CA_BUNDLE 1
#endif
#define CONFIG_PPG_MQTT_BATCH_MS 250
#define CONFIG_PPG_MQTT_BATCH_BUFFER_BYTES 8192
#define CONFIG_PPG_MQTT_WINDOW_KB 64
#ifndef CONFIG_PPG_MQTT_RING_KB
#define CONFIG_PPG_MQTT_RING_KB 2048
#endif
#define CONFIG_PPG_MQTT_OUTBOX_LIMIT_KB 64
#define CONFIG_PPG_MQTT_STATUS_PERIOD_S 15
#define CONFIG_PPG_MQTT_STOP_WHEN_SERVER_GONE 1
#define CONFIG_PPG_NET_REBOOT_S 300

#define CONFIG_PPG_STATUS_PERIOD_MS 1000
#define CONFIG_PPG_REPORT_DIE_TEMP 1
#define CONFIG_PPG_QUIET_LOGS_AFTER_BOOT 1

#define CONFIG_ESP_CONSOLE_UART 1
#define CONFIG_ESP_CONSOLE_UART_NUM 0
