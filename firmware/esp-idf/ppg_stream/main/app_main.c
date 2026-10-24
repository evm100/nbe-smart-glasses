// PPG serial protocol v1 streamer (see firmware/PROTOCOL.md)
// ESP32-S3 (Seeed XIAO) + MAX30102, ESP-IDF v6.x
//
// Tasks
//   sensor_task  (core 1): FIFO reads -> D lines, H/E/S lines, start/stop handling
//   cmd_task     (core 0): reads INFO / START / STOP / OTA <token> from the PC or the MQTT cmd topic
//   (MQTT link only: Wi-Fi/MQTT tasks in link_mqtt.c / net.c batch and publish the lines)
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "diag.h"
#include "link.h"
#include "max30102.h"
#include "proto.h"
#include "version.h"

static const char *TAG = "ppg";

#define FW_VERSION PPG_FW_VERSION
#define SYNC_PERIOD_US 10000000  // $E,...,SYNC,<unix ms> every 10 s while streaming (SNTP-synced links)

#ifndef CONFIG_PPG_LED_IR_MA_X10
#define CONFIG_PPG_LED_IR_MA_X10 0  // HR mode: option hidden in menuconfig
#endif

// ---------------------------------------------------------------- state
static max30102_t s_sensor;
static bool s_sensor_ok;
static i2c_master_bus_handle_t s_bus;
static max30102_config_t s_cfg;

static atomic_bool s_req_info, s_req_start, s_req_stop;
static bool s_streaming;
static uint32_t s_seq;
static int64_t s_period_ns;  // output sample period

// statistics reported in S lines
static uint32_t s_ovf_events, s_i2c_errors, s_alc_ovf_events, s_samples_window, s_loop_max_us;
static int32_t s_temp_centi = INT32_MIN;
#if CONFIG_PPG_INT_GPIO >= 0
static SemaphoreHandle_t s_int_sem;
#endif

// ---------------------------------------------------------------- output helpers
static void send_line(pline_t *l)
{
    size_t n = pl_end(l);
    link_write(l->start, n);
}

static void send_header(void)
{
    char buf[PROTO_MAX_LINE];
    pline_t l;
    pl_begin(&l, buf, sizeof buf);
    pl_str(&l, "H,proto=1,dev=");
    pl_str(&l, CONFIG_PPG_DEVICE_ID);
    pl_str(&l, ",fw=" FW_VERSION ",sensor=MAX30102,fs=");
    uint32_t mhz = max30102_output_rate_mhz(&s_cfg);
    if (mhz % 1000 == 0) {
        pl_u64(&l, mhz / 1000);
    } else {
        pl_fixed(&l, mhz, 3);
    }
    if (s_cfg.mode == MAX30102_MODE_SPO2) {
        pl_str(&l, ",ch=ir|red,led_ma=");
        pl_fixed(&l, s_cfg.ir_ma_x10, 1);
        pl_char(&l, '|');
        pl_fixed(&l, s_cfg.red_ma_x10, 1);
    } else {
        pl_str(&l, ",ch=red,led_ma=");
        pl_fixed(&l, s_cfg.red_ma_x10, 1);
    }
    pl_str(&l, ",adc=");
    pl_u64(&l, max30102_adc_bits(s_cfg.pulse_width_us));
    pl_str(&l, ",pw_us=");
    pl_u64(&l, s_cfg.pulse_width_us);
    pl_str(&l, ",avg=");
    pl_u64(&l, s_cfg.sample_avg);
    pl_str(&l, ",sr=");
    pl_u64(&l, s_cfg.sample_rate);
    pl_str(&l, ",range_na=");
    pl_u64(&l, s_cfg.adc_range_na);
    pl_str(&l, ",site=" CONFIG_PPG_SITE ",board=" CONFIG_PPG_BOARD_NAME);
    pl_str(&l, s_sensor_ok ? ",sensor_ok=1" : ",sensor_ok=0");
    send_line(&l);
}

static void send_event(const char *code, const char *text)
{
    char buf[PROTO_MAX_LINE];
    pline_t l;
    pl_begin(&l, buf, sizeof buf);
    pl_str(&l, "E,");
    pl_i64(&l, esp_timer_get_time());
    pl_char(&l, ',');
    pl_str(&l, code);
    pl_char(&l, ',');
    pl_str(&l, text ? text : "");
    send_line(&l);
}

static void send_status(uint32_t window_ms)
{
    char buf[PROTO_MAX_LINE];
    pline_t l;
    pl_begin(&l, buf, sizeof buf);
    pl_str(&l, "S,");
    pl_i64(&l, esp_timer_get_time());
    pl_str(&l, ",fs_meas=");
    pl_fixed(&l, (int64_t)s_samples_window * 10000 / (window_ms ? window_ms : 1), 1);  // Hz, 0.1 resolution
    pl_str(&l, ",ovf=");
    pl_u64(&l, s_ovf_events);
    pl_str(&l, ",alc_ovf=");
    pl_u64(&l, s_alc_ovf_events);
    pl_str(&l, ",i2c_err=");
    pl_u64(&l, s_i2c_errors);
    pl_str(&l, ",tx_drop=");
    pl_u64(&l, link_dropped_bytes());
    pl_str(&l, ",loop_us_max=");
    pl_u64(&l, s_loop_max_us);
    if (s_temp_centi != INT32_MIN) {
        pl_str(&l, ",temp_c=");
        pl_fixed(&l, s_temp_centi, 2);
    }
    pl_str(&l, ",heap=");
    pl_u64(&l, esp_get_free_heap_size());
    send_line(&l);
    s_samples_window = 0;
    s_loop_max_us = 0;
}

// ---------------------------------------------------------------- GPIO helpers
static void led_set(bool on)
{
#if CONFIG_PPG_STATUS_LED_GPIO >= 0
#if CONFIG_PPG_STATUS_LED_ACTIVE_LOW
    gpio_set_level(CONFIG_PPG_STATUS_LED_GPIO, on ? 0 : 1);
#else
    gpio_set_level(CONFIG_PPG_STATUS_LED_GPIO, on ? 1 : 0);
#endif
#else
    (void)on;
#endif
}

#if CONFIG_PPG_INT_GPIO >= 0
static void IRAM_ATTR int_isr(void *arg)
{
    (void)arg;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_int_sem, &woken);
    if (woken) portYIELD_FROM_ISR();
}
#endif

static void gpio_setup(void)
{
#if CONFIG_PPG_STATUS_LED_GPIO >= 0
    gpio_config_t led = {.pin_bit_mask = 1ULL << CONFIG_PPG_STATUS_LED_GPIO, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&led);
    led_set(false);
#endif
#if CONFIG_PPG_BUTTON_GPIO >= 0
    gpio_config_t btn = {.pin_bit_mask = 1ULL << CONFIG_PPG_BUTTON_GPIO, .mode = GPIO_MODE_INPUT,
                         .pull_up_en = GPIO_PULLUP_ENABLE};
    gpio_config(&btn);
#endif
#if CONFIG_PPG_INT_GPIO >= 0
    s_int_sem = xSemaphoreCreateBinary();
    gpio_config_t irq = {.pin_bit_mask = 1ULL << CONFIG_PPG_INT_GPIO, .mode = GPIO_MODE_INPUT,
                         .pull_up_en = GPIO_PULLUP_ENABLE, .intr_type = GPIO_INTR_NEGEDGE};
    gpio_config(&irq);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(CONFIG_PPG_INT_GPIO, int_isr, NULL);
#endif
}

static void poll_button(void)
{
#if CONFIG_PPG_BUTTON_GPIO >= 0
    static int last = 1;
    static int64_t last_change_us;
    int v = gpio_get_level(CONFIG_PPG_BUTTON_GPIO);
    int64_t now = esp_timer_get_time();
    if (v != last && now - last_change_us > 50000) {  // 50 ms debounce
        last_change_us = now;
        if (v == 0 && s_streaming) send_event("BTN", "");
        last = v;
    }
#endif
}

// ---------------------------------------------------------------- sensor
static void build_config(void)
{
    s_cfg = (max30102_config_t){
#if CONFIG_PPG_LED_MODE_SPO2
        .mode = MAX30102_MODE_SPO2,
#else
        .mode = MAX30102_MODE_HR,
#endif
        .sample_rate = CONFIG_PPG_SAMPLE_RATE_HZ,
        .sample_avg = CONFIG_PPG_SAMPLE_AVG_N,
        .pulse_width_us = CONFIG_PPG_PULSE_WIDTH_US,
        .adc_range_na = CONFIG_PPG_ADC_RANGE_NA,
        .red_ma_x10 = CONFIG_PPG_LED_RED_MA_X10 & ~1,  // 0.2 mA steps
        .ir_ma_x10 = CONFIG_PPG_LED_IR_MA_X10 & ~1,
        .fifo_a_full = CONFIG_PPG_FIFO_A_FULL,
        .int_a_full = CONFIG_PPG_INT_GPIO >= 0,
    };
    s_period_ns = (int64_t)1000000000000LL / max30102_output_rate_mhz(&s_cfg);
}

static esp_err_t sensor_open(void)
{
    if (!s_bus) {
        i2c_master_bus_config_t bc = {
            .i2c_port = CONFIG_PPG_I2C_PORT,
            .sda_io_num = CONFIG_PPG_I2C_SDA_GPIO,
            .scl_io_num = CONFIG_PPG_I2C_SCL_GPIO,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
        };
#if CONFIG_PPG_I2C_INTERNAL_PULLUPS
        bc.flags.enable_internal_pullup = 1;
#endif
        esp_err_t err = i2c_new_master_bus(&bc, &s_bus);
        if (err != ESP_OK) return err;
    }
    if (s_sensor.dev) {
        i2c_master_bus_rm_device(s_sensor.dev);
        s_sensor.dev = NULL;
    }
    return max30102_init(&s_sensor, s_bus, CONFIG_PPG_I2C_FREQ_HZ, &s_cfg);
}

// Pairs the device clock (esp_timer, the t_us of every line) with UTC so recordings
// from different nodes can be aligned on the server.
static void send_sync(void)
{
    int64_t ms;
    if (!link_epoch_ms(&ms)) return;
    char buf[PROTO_MAX_LINE];
    pline_t l;
    pl_begin(&l, buf, sizeof buf);
    pl_str(&l, "E,");
    pl_i64(&l, esp_timer_get_time());
    pl_str(&l, ",SYNC,");
    pl_i64(&l, ms);
    send_line(&l);
}

static void note_state(void)
{
    link_note_state(s_streaming, s_sensor_ok, max30102_output_rate_mhz(&s_cfg));
}

static void start_streaming(void)
{
    max30102_clear_fifo(&s_sensor);
    s_seq = 0;
    s_ovf_events = s_i2c_errors = s_alc_ovf_events = 0;
    send_header();
    send_event("START", "");
    send_sync();
    s_streaming = true;
    led_set(true);
    note_state();
}

static void stop_streaming(void)
{
    if (s_streaming) {
        s_streaming = false;
        send_event("STOP", "");
        link_flush();  // MQTT: deliver the tail of the recording right away
    }
    led_set(false);
    note_state();
}

static void read_and_send(void)
{
    static uint32_t red[MAX30102_FIFO_DEPTH], ir[MAX30102_FIFO_DEPTH];
    int n = 0;
    uint8_t ovf = 0;
    esp_err_t err = max30102_read_fifo(&s_sensor, red, ir, &n, &ovf);
    int64_t now_us = esp_timer_get_time();
    if (err != ESP_OK) {
        if (++s_i2c_errors % 50 == 0) send_event("SENSOR_ERR", "i2c read failed repeatedly");
        return;
    }
    if (!s_streaming || n == 0) return;
    if (ovf) {
        s_ovf_events++;
        send_event("FIFO_OVF", "fifo overflowed - samples lost");
    }

    // per-sample time, oldest first: t_k = now - (n-1-k) * period   (PROTOCOL.md timing rule 1)
    static int64_t t_us[MAX30102_FIFO_DEPTH];
    for (int k = 0; k < n; k++) t_us[k] = now_us - ((int64_t)(n - 1 - k) * s_period_ns) / 1000;
    if (s_sensor.n_leds == 2) {
        link_write_samples(s_seq, t_us, ir, red, n, s_period_ns);  // ch order: ir|red
    } else {
        link_write_samples(s_seq, t_us, red, NULL, n, s_period_ns);
    }
    s_seq += (uint32_t)n;
    s_samples_window += (uint32_t)n;
}

// First sensor connection. Runs in sensor_task so the I2C driver's interrupt is allocated on the
// sensor's core (1), not on core 0 next to Wi-Fi / TLS, which can hold it off long enough for
// the 80 ms FIFO to overflow.
static void sensor_first_open(void)
{
    esp_err_t err = sensor_open();
    s_sensor_ok = (err == ESP_OK);
    if (!s_sensor_ok) {
        ESP_LOGW(TAG, "MAX30102 not found (%s) - will keep retrying", esp_err_to_name(err));
    } else {
        const char *w = max30102_timing_warning(&s_cfg);
        if (w) {
            ESP_LOGW(TAG, "%s", w);
            send_event("SENSOR_ERR", w);
        }
    }
    send_header();
    note_state();
}

static void sensor_task(void *arg)
{
    (void)arg;
    if (!s_bus) sensor_first_open();
    int64_t last_status_us = esp_timer_get_time();
    int64_t last_sync_us = last_status_us;
    int64_t last_retry_us = 0;
    bool temp_pending = false;
    (void)temp_pending;  // unused when die-temperature reporting is off

    for (;;) {
        // ---- (re)connect the sensor
        if (!s_sensor_ok) {
            int64_t now = esp_timer_get_time();
            if (now - last_retry_us > 1000000) {
                last_retry_us = now;
                esp_err_t err = sensor_open();
                s_sensor_ok = (err == ESP_OK);
                note_state();
                if (s_sensor_ok) {
                    ESP_LOGI(TAG, "MAX30102 found");
                    const char *w = max30102_timing_warning(&s_cfg);
                    if (w) send_event("SENSOR_ERR", w);
                    if (s_streaming) start_streaming();
                } else {
                    send_event("SENSOR_ERR", "MAX30102 not found - check wiring/power");
                }
            }
        }

        // ---- commands from the PC (set by cmd_task)
        if (atomic_exchange(&s_req_info, false)) send_header();
        if (atomic_exchange(&s_req_stop, false)) stop_streaming();
        if (atomic_exchange(&s_req_start, false)) {
            if (s_sensor_ok) {
                start_streaming();
            } else {
                send_header();
                send_event("SENSOR_ERR", "START ignored - sensor not found");
            }
        }

        // ---- wait for data
#if CONFIG_PPG_INT_GPIO >= 0
        xSemaphoreTake(s_int_sem, pdMS_TO_TICKS(CONFIG_PPG_FIFO_POLL_MS));
#else
        vTaskDelay(pdMS_TO_TICKS(CONFIG_PPG_FIFO_POLL_MS));
#endif
        int64_t t0 = esp_timer_get_time();
        if (s_sensor_ok) {
            read_and_send();
#if CONFIG_PPG_INT_GPIO >= 0
            uint8_t st1, st2;
            if (max30102_read_int_status(&s_sensor, &st1, &st2) == ESP_OK && (st1 & MAX30102_INT_ALC_OVF)) {
                s_alc_ovf_events++;
            }
#endif
            if (s_i2c_errors > 200) {  // sensor unplugged? go back to reconnecting
                s_sensor_ok = false;
                s_i2c_errors = 0;
                note_state();
            }
        }
        poll_button();
        uint32_t loop_us = (uint32_t)(esp_timer_get_time() - t0);
        if (loop_us > s_loop_max_us) s_loop_max_us = loop_us;
        if (s_streaming && t0 - last_sync_us >= SYNC_PERIOD_US) {
            last_sync_us = t0;
            send_sync();
        }

        // ---- once per status period
#if CONFIG_PPG_STATUS_PERIOD_MS > 0
        int64_t now = esp_timer_get_time();
        if (now - last_status_us >= (int64_t)CONFIG_PPG_STATUS_PERIOD_MS * 1000) {
            uint32_t window_ms = (uint32_t)((now - last_status_us) / 1000);
            last_status_us = now;
            if (s_sensor_ok) {
#if CONFIG_PPG_INT_GPIO < 0
                uint8_t st1, st2;  // ALC_OVF is latched: one read per period catches it
                if (max30102_read_int_status(&s_sensor, &st1, &st2) == ESP_OK && (st1 & MAX30102_INT_ALC_OVF)) {
                    s_alc_ovf_events++;
                }
#endif
#if CONFIG_PPG_REPORT_DIE_TEMP
                if (temp_pending) {
                    bool ready = false;
                    int32_t c;
                    if (max30102_read_temp(&s_sensor, &c, &ready) == ESP_OK && ready) s_temp_centi = c;
                }
                temp_pending = (max30102_start_temp(&s_sensor) == ESP_OK);
#endif
            }
            if (s_streaming) send_status(window_ms);
            else s_samples_window = 0;
        }
#else
        (void)last_status_us;
        (void)temp_pending;
#endif
    }
}

// ---------------------------------------------------------------- commands
static void handle_command(const char *c)
{
    if (strcmp(c, "INFO") == 0) {
        atomic_store(&s_req_info, true);
    } else if (strcmp(c, "START") == 0) {
        atomic_store(&s_req_start, true);
    } else if (strcmp(c, "STOP") == 0) {
        atomic_store(&s_req_stop, true);
    } else if (strncmp(c, "OTA ", 4) == 0) {
        atomic_store(&s_req_stop, true);  // the server only sends it outside trials; stop anyway
        link_ota(c + 4);
    }
    // unknown commands are ignored (protocol v1)
}

static void cmd_task(void *arg)
{
    (void)arg;
    char line[80];
    size_t len = 0;
    uint8_t buf[64];
    for (;;) {
        int n = link_read(buf, sizeof buf, 50);
        for (int i = 0; i < n; i++) {
            char ch = (char)buf[i];
            if (ch == '\n' || ch == '\r') {
                if (len) {
                    line[len] = 0;
                    handle_command(line);
                    len = 0;
                }
            } else if (len < sizeof line - 1) {
                if (ch >= 'a' && ch <= 'z') ch = (char)(ch - 32);  // case-insensitive
                line[len++] = ch;
            }
        }
    }
}

// ---------------------------------------------------------------- main
void app_main(void)
{
    ESP_ERROR_CHECK(link_init());
    gpio_setup();
    build_config();

    char boot[48] = FW_VERSION " reset=";  // why the last run ended: brownout, panic, task_wdt...
    strlcat(boot, diag_reset_reason(), sizeof boot);
    send_event("BOOT", boot);  // the sensor is opened by sensor_task (sensor_first_open)

#if CONFIG_PPG_AUTOSTART
    atomic_store(&s_req_start, true);
#endif
#if CONFIG_PPG_QUIET_LOGS_AFTER_BOOT
    esp_log_level_set("*", ESP_LOG_WARN);
    // Wi-Fi/broker progress ("got IP", "connected to broker") comes after boot - keep it visible
    esp_log_level_set("net", ESP_LOG_INFO);
    esp_log_level_set("link_mqtt", ESP_LOG_INFO);
#endif

    xTaskCreatePinnedToCore(sensor_task, "ppg_sensor", 6144, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(cmd_task, "ppg_cmd", 3072, NULL, 5, NULL, 0);
}
