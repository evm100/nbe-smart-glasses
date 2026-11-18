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
static bool g_usb_host = true;  // a USB host (PC) on the cable
static jmp_buf g_jmp;

int64_t esp_timer_get_time(void) { return g_now_us; }
uint32_t esp_get_free_heap_size(void) { return 300000; }
esp_reset_reason_t esp_reset_reason(void) { return ESP_RST_POWERON; }

#if CONFIG_PPG_CAMERA  // camera builds (USB binary, hybrid; not the dual-PPG hybrid)
#include "esp_camera.h"
// fake OV2640: 30 frames/s, pixel (x, y) of native frame k = (k + x + 3y) & 0xFF; the firmware's
// 15 fps cap sends every other one. The host "stops reading" for a few USB writes mid-frame
// (writes 100-103: the frame envelope is cut short, the next envelopes must still decode).
#define CAM_W 240
#define CAM_H 240
static uint8_t g_px[CAM_W * CAM_H];
static camera_fb_t g_fb;
static uint32_t g_native;
static int64_t g_next_frame_us = 1050000;
static int g_in_cam;
static int s_dummy_ret(sensor_t *s, int v) { (void)s; (void)v; return 0; }
static sensor_t g_sensor = {s_dummy_ret, s_dummy_ret, s_dummy_ret, s_dummy_ret, s_dummy_ret, s_dummy_ret, s_dummy_ret,
                             NULL, NULL, NULL};  // window calls: not used with PPG_CAM_WINDOW 0
esp_err_t esp_camera_init(const camera_config_t *c) { (void)c; return ESP_OK; }
sensor_t *esp_camera_sensor_get(void) { return &g_sensor; }
camera_fb_t *esp_camera_fb_get(void)
{
    for (int y = 0; y < CAM_H; y++)
        for (int x = 0; x < CAM_W; x++) g_px[y * CAM_W + x] = (uint8_t)(g_native + x + 3 * y);
    g_native++;
    g_fb = (camera_fb_t){.buf = g_px, .len = sizeof g_px, .width = CAM_W, .height = CAM_H,
                         .timestamp = {.tv_sec = g_now_us / 1000000, .tv_usec = g_now_us % 1000000}};
    return &g_fb;
}
void esp_camera_fb_return(camera_fb_t *fb) { (void)fb; }
#endif
#ifdef TEST_USB_BIN
static int g_writes;
static int host_reading(void) { g_writes++; return g_writes < 100 || g_writes > 103; }
#endif

static void advance(TickType_t ms)
{
    g_now_us += (int64_t)ms * 1000;
#ifdef TEST_USB_BIN
    extern void link_usb_bin_test_tick(void);
    extern void camera_test_tick(void);
    static int64_t last_tx;
    if (g_now_us - last_tx >= 10000) {
        last_tx = g_now_us;
        link_usb_bin_test_tick();
    }
    if (!g_in_cam && g_now_us >= g_next_frame_us) {  // camera_step may vTaskDelay -> back in here
        g_in_cam = 1;
        g_next_frame_us += 33333;
        camera_test_tick();
        g_in_cam = 0;
    }
#endif
#ifdef TEST_HYBRID
    // Hybrid link: Wi-Fi from boot; a PC is plugged in at 2.5 s and its gateway speaks (PING every
    // second) from 3.0 s: the board hands over to USB. Unplugged at 6.0 s: back to Wi-Fi.
    extern void link_hybrid_test_tick(void);
    extern void camera_test_tick(void);
    extern void usbgw_test_feed(const uint8_t *buf, int n);
    extern void fake_mqtt_pump(void);
    static int64_t last_tick, last_ping;
    g_usb_host = g_now_us >= 2500000 && g_now_us < 6000000;
    if (g_usb_host && g_now_us >= 3000000 && g_now_us - last_ping >= 1000000) {
        last_ping = g_now_us;
        usbgw_test_feed((const uint8_t *)"PING\n", 5);
    }
#ifdef TEST_OUTAGE
    // broker unreachable 6.5-8.5 s (back on Wi-Fi): a small ring fills, camera frames must give way
    extern void fake_mqtt_connected(bool up);
    if (g_now_us >= 6500000) fake_mqtt_connected(g_now_us >= 8500000);
#endif
    fake_mqtt_pump();
    if (g_now_us - last_tick >= 10000) {
        last_tick = g_now_us;
        link_hybrid_test_tick();
    }
#if CONFIG_PPG_CAMERA
    if (!g_in_cam && g_now_us >= g_next_frame_us) {
        g_in_cam = 1;
        g_next_frame_us += 33333;
        camera_test_tick();
        g_in_cam = 0;
    }
#endif
#endif
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
#ifdef TEST_AUX
    {
        extern void xs_test_tick(void);  // stands in for the aux task (it polls every 10 ms)
        static int64_t last_aux;
        static int in_aux;
        if (!in_aux && g_now_us - last_aux >= 10000) {
            in_aux = 1;
            last_aux = g_now_us;
            xs_test_tick();
            in_aux = 0;
        }
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
int usb_serial_jtag_write_bytes(const void *src, size_t n, uint32_t t)
{
    (void)t;
#ifdef TEST_USB_BIN
    if (!host_reading()) return 0;  // all or nothing, like the real driver
#endif
    fwrite(src, 1, n, stdout);
    return (int)n;
}
bool usb_serial_jtag_is_connected(void) { return g_usb_host; }
esp_err_t uart_driver_install(uart_port_t n, int rx, int tx, int q, void *qq, int f) { (void)n; (void)rx; (void)tx; (void)q; (void)qq; (void)f; return 0; }
esp_err_t uart_param_config(uart_port_t n, const uart_config_t *c) { (void)n; (void)c; return 0; }
esp_err_t uart_set_pin(uart_port_t n, int tx, int rx, int a, int b) { (void)n; (void)tx; (void)rx; (void)a; (void)b; return 0; }
int uart_write_bytes(uart_port_t n, const void *src, size_t size) { (void)n; fwrite(src, 1, size, stdout); return (int)size; }
int uart_read_bytes(uart_port_t n, void *b, uint32_t l, uint32_t t) { (void)n; (void)b; (void)l; (void)t; return 0; }

// ---- simulated MAX30102s: one per I2C port (dual-PPG builds use both). Sensor 1 (the right side of
// dual glasses) runs 1 % fast - its own oscillator - and has its own values.
typedef struct {
    uint8_t R[256];
    uint32_t F_red[32], F_ir[32];
    int f_wr, f_rd, f_cnt, f_ovf;
    int64_t next_due_us;
    uint32_t idx;  // ground-truth sample index
    int present;
    int ppm;       // rate error in parts per million
} simsens_t;
static simsens_t g_s[2] = {{.next_due_us = -1, .present = 1}, {.next_due_us = -1, .present = 1, .ppm = 10000}};
static uint32_t idx;  // ground truth of sensor 0 (printed at the end)
struct i2c_bus_t { int port; };
struct i2c_dev_t { int port; };
static struct i2c_bus_t bus_obj[2] = {{0}, {1}};
static struct i2c_dev_t dev_obj[2] = {{0}, {1}};

static void sim_update(simsens_t *S, int k)
{
    static const int rates[] = {50, 100, 200, 400, 800, 1000, 1600, 3200};
    int mode = S->R[0x09] & 7;
    if (mode != 2 && mode != 3) { S->next_due_us = -1; return; }
    int sr = rates[(S->R[0x0A] >> 2) & 7];
    int avg = 1 << ((S->R[0x08] >> 5) > 5 ? 5 : (S->R[0x08] >> 5));
    int64_t period = 1000000LL * avg * 1000000 / ((int64_t)sr * (1000000 + S->ppm));
    if (S->next_due_us < 0) S->next_due_us = g_now_us + period;
    while (S->next_due_us <= g_now_us) {
        if (S->f_cnt == 32) { S->f_rd = (S->f_rd + 1) & 31; S->f_cnt--; if (S->f_ovf < 31) S->f_ovf++; }
        S->F_red[S->f_wr] = k ? (S->idx * 5 + 1000) & 0x3FFFF : S->idx & 0x3FFFF;
        S->F_ir[S->f_wr] = k ? (S->idx * 7 + 3) & 0x3FFFF : (S->idx * 3 + 1) & 0x3FFFF;
        S->idx++;
        S->f_wr = (S->f_wr + 1) & 31;
        S->f_cnt++;
        S->next_due_us += period;
    }
    if (!k) idx = S->idx;
}

esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *c, i2c_master_bus_handle_t *r)
{ if (c->i2c_port < 0 || c->i2c_port > 1) return ESP_FAIL; *r = &bus_obj[c->i2c_port]; return 0; }
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t b, const i2c_device_config_t *c, i2c_master_dev_handle_t *r)
{ if (c->device_address != 0x57) return ESP_FAIL; *r = &dev_obj[b->port]; return 0; }
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t d) { (void)d; return 0; }

esp_err_t i2c_master_transmit(i2c_master_dev_handle_t d, const uint8_t *w, size_t n, int t)
{
    (void)t;
    simsens_t *S = &g_s[d->port];
    if (!S->present) return ESP_FAIL;
    sim_update(S, d->port);
    if (n < 2) return 0;
    uint8_t reg = w[0], v = w[1];
    if (reg == 0x09 && (v & 0x40)) { memset(S->R, 0, sizeof S->R); S->f_wr = S->f_rd = S->f_cnt = S->f_ovf = 0; S->next_due_us = -1; return 0; }
    if (reg == 0x04) { S->f_wr = v & 31; S->f_cnt = (S->f_wr - S->f_rd) & 31; return 0; }
    if (reg == 0x05) { S->f_ovf = v & 31; return 0; }
    if (reg == 0x06) { S->f_rd = v & 31; S->f_cnt = (S->f_wr - S->f_rd) & 31; return 0; }
    S->R[reg] = v;
    if (reg == 0x09) S->next_due_us = -1;
    return 0;
}

esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t d, const uint8_t *w, size_t wn, uint8_t *r, size_t rn, int t)
{
    (void)t; (void)wn;
    simsens_t *S = &g_s[d->port];
    if (!S->present) return ESP_FAIL;
    sim_update(S, d->port);
    uint8_t reg = w[0];
    if (reg == 0x07) {
        int nleds = (S->R[0x09] & 7) == 3 ? 2 : 1;
        size_t bps = 3u * (size_t)nleds;
        for (size_t i = 0; i + bps <= rn; i += bps) {
            uint32_t red = S->f_cnt ? S->F_red[S->f_rd] : 0, ir = S->f_cnt ? S->F_ir[S->f_rd] : 0;
            if (S->f_cnt) { S->f_rd = (S->f_rd + 1) & 31; S->f_cnt--; }
            r[i] = (uint8_t)(red >> 16); r[i + 1] = (uint8_t)(red >> 8); r[i + 2] = (uint8_t)red;
            if (nleds == 2) { r[i + 3] = (uint8_t)(ir >> 16); r[i + 4] = (uint8_t)(ir >> 8); r[i + 5] = (uint8_t)ir; }
        }
        S->f_ovf = 0;  // reading data clears the overflow counter in this model
        return 0;
    }
    for (size_t i = 0; i < rn; i++, reg++) {
        switch (reg) {
        case 0x04: r[i] = (uint8_t)S->f_wr; break;
        case 0x05: r[i] = (uint8_t)S->f_ovf; break;
        case 0x06: r[i] = (uint8_t)S->f_rd; break;
        case 0x1F: r[i] = 31; break;
        case 0x20: r[i] = 4; break;   // 31.25 C
        case 0x21: r[i] = 0; break;   // conversion done
        case 0xFF: r[i] = 0x15; break;
        default: r[i] = S->R[reg];
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    double secs = argc > 1 ? atof(argv[1]) : 3.0;
    if (argc > 2) g_s[0].present = atoi(argv[2]);
    if (argc > 3) g_s[1].present = atoi(argv[3]);  // dual builds: the right sensor
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
#ifdef TEST_USB_BIN
    extern void link_usb_bin_test_tick(void);
    link_usb_bin_test_tick();
    fprintf(stderr, "usb bin: %lu frames not sent, %lu bytes dropped\n", (unsigned long)link_frames_dropped(),
            (unsigned long)link_dropped_bytes());
#endif
#ifdef TEST_HYBRID
    {
        // after STOP (on Wi-Fi again): keep sending until everything is acknowledged
        extern void link_hybrid_test_tick(void);
        extern void fake_mqtt_pump(void);
        for (int i = 0; i < 4000; i++) {
            g_now_us += 10000;
            fake_mqtt_pump();
            link_hybrid_test_tick();
        }
        extern int g_batches, g_stops, g_starts, g_net_pauses, g_net_resumes;
        fprintf(stderr, "hybrid: %d mqtt frames, broker stops %d starts %d, radio off %d on %d, %lu camera frames "
                        "not taken, %lu bytes lost\n",
                g_batches, g_stops, g_starts, g_net_pauses, g_net_resumes, (unsigned long)link_frames_dropped(),
                (unsigned long)link_dropped_bytes());
#ifdef TEST_OUTAGE
        bool cam_ok = link_frames_dropped() > 0;  // the outage must have cost frames (and only frames)
#else
        bool cam_ok = link_frames_dropped() == 0;
#endif
        if (g_stops != 1 || g_starts != 2 || g_net_pauses != 1 || g_net_resumes != 1 || link_dropped_bytes() ||
            !cam_ok)
            return 1;
    }
#endif
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
    fprintf(stderr, "simulated samples generated: %u (sensor 1: %u)\n", idx, g_s[1].idx);
    return 0;
}
