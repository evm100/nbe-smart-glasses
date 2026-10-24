// Host simulation: runs the real firmware sources against a simulated MAX30102 and
// a fake clock, printing exactly what the ESP32 would send over USB.
//   ./sim <seconds> > out.txt
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../main/app_main.c"
#include "driver/uart.h"
#include "driver/usb_serial_jtag.h"

static int64_t g_now_us = 1000000;  // "boot" at 1 s
static int64_t g_end_us;
static jmp_buf g_jmp;

int64_t esp_timer_get_time(void) { return g_now_us; }
uint32_t esp_get_free_heap_size(void) { return 300000; }
esp_reset_reason_t esp_reset_reason(void) { return ESP_RST_POWERON; }

static void advance(TickType_t ms)
{
    g_now_us += (int64_t)ms * 1000;
#ifdef TEST_MQTT
    // stands in for the sender task; the broker is unreachable from 1.3 s to 2.9 s
    extern void link_mqtt_test_tick(void);
    extern void fake_mqtt_connected(bool up);
    extern void fake_mqtt_pump(void);
    static int64_t last_tick;
    fake_mqtt_connected(g_now_us < 2300000 || g_now_us >= 3900000);
    fake_mqtt_pump();
    if (g_now_us - last_tick >= CONFIG_PPG_MQTT_BATCH_MS * 1000) {
        last_tick = g_now_us;
        link_mqtt_test_tick();
    }
#endif
    if (g_now_us >= g_end_us) longjmp(g_jmp, 1);
}
void vTaskDelay(TickType_t t) { advance(t ? t : 1); }
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t f, const char *n, uint32_t s, void *a, int p, void *h, int c)
{ (void)f; (void)n; (void)s; (void)a; (void)p; (void)h; (void)c; return 1; }
static int dummy;
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return &dummy; }
SemaphoreHandle_t xSemaphoreCreateBinary(void) { return &dummy; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t s, TickType_t t)
{ if (s != (SemaphoreHandle_t)&dummy || t == portMAX_DELAY) return 1; advance(t); return 0; }
BaseType_t xSemaphoreGive(SemaphoreHandle_t s) { (void)s; return 1; }
BaseType_t xSemaphoreGiveFromISR(SemaphoreHandle_t s, BaseType_t *w) { (void)s; *w = 0; return 1; }
esp_err_t gpio_config(const gpio_config_t *c) { (void)c; return 0; }
esp_err_t gpio_set_level(gpio_num_t n, uint32_t l) { (void)n; (void)l; return 0; }
int gpio_get_level(gpio_num_t n) { (void)n; return 1; }
esp_err_t gpio_install_isr_service(int f) { (void)f; return 0; }
esp_err_t gpio_isr_handler_add(gpio_num_t n, gpio_isr_t f, void *a) { (void)n; (void)f; (void)a; return 0; }

// ---- link
esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *c) { (void)c; return 0; }
int usb_serial_jtag_read_bytes(void *b, uint32_t n, uint32_t t) { (void)b; (void)n; (void)t; return 0; }
int usb_serial_jtag_write_bytes(const void *src, size_t n, uint32_t t) { (void)t; fwrite(src, 1, n, stdout); return (int)n; }
esp_err_t uart_driver_install(uart_port_t n, int rx, int tx, int q, void *qq, int f) { (void)n; (void)rx; (void)tx; (void)q; (void)qq; (void)f; return 0; }
esp_err_t uart_param_config(uart_port_t n, const uart_config_t *c) { (void)n; (void)c; return 0; }
esp_err_t uart_set_pin(uart_port_t n, int tx, int rx, int a, int b) { (void)n; (void)tx; (void)rx; (void)a; (void)b; return 0; }
int uart_write_bytes(uart_port_t n, const void *src, size_t size) { (void)n; fwrite(src, 1, size, stdout); return (int)size; }
int uart_read_bytes(uart_port_t n, void *b, uint32_t l, uint32_t t) { (void)n; (void)b; (void)l; (void)t; return 0; }

// ---- simulated MAX30102
static uint8_t R[256];
static uint32_t F_red[32], F_ir[32];
static int f_wr, f_rd, f_cnt, f_ovf;
static int64_t next_due_us = -1;
static uint32_t idx;  // ground-truth sample index
static int present = 1;
struct i2c_bus_t { int x; };
struct i2c_dev_t { int x; };
static struct i2c_bus_t bus_obj;
static struct i2c_dev_t dev_obj;

static void sim_update(void)
{
    static const int rates[] = {50, 100, 200, 400, 800, 1000, 1600, 3200};
    int mode = R[0x09] & 7;
    if (mode != 2 && mode != 3) { next_due_us = -1; return; }
    int sr = rates[(R[0x0A] >> 2) & 7];
    int avg = 1 << ((R[0x08] >> 5) > 5 ? 5 : (R[0x08] >> 5));
    int64_t period = 1000000LL * avg / sr;
    if (next_due_us < 0) next_due_us = g_now_us + period;
    while (next_due_us <= g_now_us) {
        if (f_cnt == 32) { f_rd = (f_rd + 1) & 31; f_cnt--; if (f_ovf < 31) f_ovf++; }
        F_red[f_wr] = idx & 0x3FFFF;
        F_ir[f_wr] = (idx * 3 + 1) & 0x3FFFF;
        idx++;
        f_wr = (f_wr + 1) & 31;
        f_cnt++;
        next_due_us += period;
    }
}

esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *c, i2c_master_bus_handle_t *r) { (void)c; *r = &bus_obj; return 0; }
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t b, const i2c_device_config_t *c, i2c_master_dev_handle_t *r)
{ (void)b; if (c->device_address != 0x57) return ESP_FAIL; *r = &dev_obj; return 0; }
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t d) { (void)d; return 0; }

esp_err_t i2c_master_transmit(i2c_master_dev_handle_t d, const uint8_t *w, size_t n, int t)
{
    (void)d; (void)t;
    if (!present) return ESP_FAIL;
    sim_update();
    if (n < 2) return 0;
    uint8_t reg = w[0], v = w[1];
    if (reg == 0x09 && (v & 0x40)) { memset(R, 0, sizeof R); f_wr = f_rd = f_cnt = f_ovf = 0; next_due_us = -1; return 0; }
    if (reg == 0x04) { f_wr = v & 31; f_cnt = (f_wr - f_rd) & 31; return 0; }
    if (reg == 0x05) { f_ovf = v & 31; return 0; }
    if (reg == 0x06) { f_rd = v & 31; f_cnt = (f_wr - f_rd) & 31; return 0; }
    R[reg] = v;
    if (reg == 0x09) next_due_us = -1;
    return 0;
}

esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t d, const uint8_t *w, size_t wn, uint8_t *r, size_t rn, int t)
{
    (void)d; (void)t; (void)wn;
    if (!present) return ESP_FAIL;
    sim_update();
    uint8_t reg = w[0];
    if (reg == 0x07) {
        int nleds = (R[0x09] & 7) == 3 ? 2 : 1;
        size_t bps = 3u * (size_t)nleds;
        for (size_t i = 0; i + bps <= rn; i += bps) {
            uint32_t red = f_cnt ? F_red[f_rd] : 0, ir = f_cnt ? F_ir[f_rd] : 0;
            if (f_cnt) { f_rd = (f_rd + 1) & 31; f_cnt--; }
            r[i] = (uint8_t)(red >> 16); r[i + 1] = (uint8_t)(red >> 8); r[i + 2] = (uint8_t)red;
            if (nleds == 2) { r[i + 3] = (uint8_t)(ir >> 16); r[i + 4] = (uint8_t)(ir >> 8); r[i + 5] = (uint8_t)ir; }
        }
        f_ovf = 0;  // reading data clears the overflow counter in this model
        return 0;
    }
    for (size_t i = 0; i < rn; i++, reg++) {
        switch (reg) {
        case 0x04: r[i] = (uint8_t)f_wr; break;
        case 0x05: r[i] = (uint8_t)f_ovf; break;
        case 0x06: r[i] = (uint8_t)f_rd; break;
        case 0x1F: r[i] = 31; break;
        case 0x20: r[i] = 4; break;   // 31.25 C
        case 0x21: r[i] = 0; break;   // conversion done
        case 0xFF: r[i] = 0x15; break;
        default: r[i] = R[reg];
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    double secs = argc > 1 ? atof(argv[1]) : 3.0;
    if (argc > 2) present = atoi(argv[2]);
    g_end_us = g_now_us + 100000;
    app_main();                       // boot: link, sensor init, BOOT event, header
    handle_command("INFO");
    handle_command("START");
    g_end_us = g_now_us + (int64_t)(secs * 1e6);
    if (!setjmp(g_jmp)) sensor_task(NULL);
    handle_command("STOP");
    g_end_us = g_now_us + 20000;
    if (!setjmp(g_jmp)) sensor_task(NULL);
    link_flush();
#ifdef TEST_MQTT
    // after STOP: keep sending / acknowledging until everything is delivered (ACK timeout = 10 s)
    extern void link_mqtt_test_tick(void);
    extern void fake_mqtt_pump(void);
    for (int i = 0; i < 400; i++) {
        g_now_us += CONFIG_PPG_MQTT_BATCH_MS * 1000;
        fake_mqtt_pump();
        link_mqtt_test_tick();
    }
    extern int g_batches, g_max_batch, g_offline_publishes, g_dropped_frames, g_resends_asked;
    fprintf(stderr, "mqtt frames: %d (largest %d bytes, %d tried offline, %d dropped, %d RESENDs, %lu bytes lost)\n",
            g_batches, g_max_batch, g_offline_publishes, g_dropped_frames, g_resends_asked,
            (unsigned long)link_dropped_bytes());
    if (g_batches < 20 || g_dropped_frames != 1 || !g_resends_asked || link_dropped_bytes()) return 1;
#endif
    fprintf(stderr, "simulated samples generated: %u\n", idx);
    return 0;
}
