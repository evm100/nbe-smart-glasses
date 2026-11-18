// USB-C side of the hybrid link, see usbgw.h.
#include "usbgw.h"

#include <string.h>
#include <strings.h>

#include "driver/usb_serial_jtag.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#define CHUNK 4096         // bytes per usb_serial_jtag_write_bytes() (all-or-nothing, <= TX buffer)
#define CHUNK_WAIT_MS 100  // host not reading for this long: give up

static void (*s_on_cmd)(const char *line, size_t len);
static volatile int64_t s_seen_us = -1;  // last line from the gateway (-1 = never)

static void line_in(const char *l, size_t n)
{
    s_seen_us = esp_timer_get_time();
    if (n == 4 && strncasecmp(l, "PING", 4) == 0) return;  // heartbeat only
    if (s_on_cmd) s_on_cmd(l, n);
}

#ifdef TEST_HYBRID
void usbgw_test_feed(const uint8_t *buf, int n)  // host_test/sim.c: bytes "from the PC"
#else
static void usbgw_feed(const uint8_t *buf, int n)
#endif
{
    static char line[80];
    static size_t len;
    for (int i = 0; i < n; i++) {
        char ch = (char)buf[i];
        if (ch == '\n' || ch == '\r') {
            if (len) line_in(line, len);
            len = 0;
        } else if (len < sizeof line - 1) {
            line[len++] = ch;
        }
    }
}

#ifndef TEST_HYBRID
static void rx_task(void *arg)
{
    (void)arg;
    uint8_t buf[64];
    for (;;) {
        int n = usb_serial_jtag_read_bytes(buf, sizeof buf, pdMS_TO_TICKS(50));
        if (n > 0) usbgw_feed(buf, n);
    }
}
#endif

esp_err_t usbgw_init(void (*on_cmd)(const char *line, size_t len))
{
    s_on_cmd = on_cmd;
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = CONFIG_PPG_TX_BUFFER_BYTES;
    cfg.rx_buffer_size = 512;
    esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err != ESP_OK) return err;
#ifndef TEST_HYBRID
    xTaskCreatePinnedToCore(rx_task, "ppg_usb_rx", 3072, NULL, 5, NULL, 0);
#endif
    return ESP_OK;
}

bool usbgw_present(void)
{
    int64_t seen = s_seen_us;
    return seen >= 0 && esp_timer_get_time() - seen < USBGW_TIMEOUT_US && usb_serial_jtag_is_connected();
}

bool usbgw_put(const uint8_t *p, size_t n)
{
    while (n) {
        size_t k = n < CHUNK ? n : CHUNK;
        if (usb_serial_jtag_write_bytes(p, k, pdMS_TO_TICKS(CHUNK_WAIT_MS)) != (int)k) return false;
        p += k;
        n -= k;
    }
    return true;
}
