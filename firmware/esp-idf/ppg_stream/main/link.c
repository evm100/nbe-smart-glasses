#include "link.h"

#include "proto.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"

#if CONFIG_PPG_LINK_UART
#include "driver/uart.h"
#if CONFIG_ESP_CONSOLE_UART && (CONFIG_ESP_CONSOLE_UART_NUM == CONFIG_PPG_UART_NUM)
#error "PPG data UART is the same as the console UART - pick another UART number or move the console"
#endif
#else
#include "driver/usb_serial_jtag.h"
#endif

static SemaphoreHandle_t s_lock;
static volatile uint32_t s_dropped;

// how long a writer may wait for buffer space before dropping bytes
#define WRITE_WAIT_MS 5

esp_err_t link_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return ESP_ERR_NO_MEM;
#if CONFIG_PPG_LINK_UART
    const uart_config_t uc = {
        .baud_rate = CONFIG_PPG_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t err = uart_driver_install(CONFIG_PPG_UART_NUM, 1024, CONFIG_PPG_TX_BUFFER_BYTES, 0, NULL, 0);
    if (err != ESP_OK) return err;
    if ((err = uart_param_config(CONFIG_PPG_UART_NUM, &uc)) != ESP_OK) return err;
    return uart_set_pin(CONFIG_PPG_UART_NUM, CONFIG_PPG_UART_TX_GPIO, CONFIG_PPG_UART_RX_GPIO,
                        UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
#else
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = CONFIG_PPG_TX_BUFFER_BYTES;
    cfg.rx_buffer_size = 512;
    return usb_serial_jtag_driver_install(&cfg);
#endif
}

int link_write(const char *buf, size_t len)
{
    int n;
    xSemaphoreTake(s_lock, portMAX_DELAY);
#if CONFIG_PPG_LINK_UART
    n = uart_write_bytes(CONFIG_PPG_UART_NUM, buf, len);  // blocks until copied into the TX ring buffer
#else
    n = usb_serial_jtag_write_bytes(buf, len, pdMS_TO_TICKS(WRITE_WAIT_MS));
#endif
    xSemaphoreGive(s_lock);
    if (n < 0) n = 0;
    if ((size_t)n < len) s_dropped += (uint32_t)(len - (size_t)n);
    return n;
}

int link_read(uint8_t *buf, size_t max, uint32_t timeout_ms)
{
#if CONFIG_PPG_LINK_UART
    int n = uart_read_bytes(CONFIG_PPG_UART_NUM, buf, (uint32_t)max, pdMS_TO_TICKS(timeout_ms));
#else
    int n = usb_serial_jtag_read_bytes(buf, (uint32_t)max, pdMS_TO_TICKS(timeout_ms));
#endif
    return n < 0 ? 0 : n;
}

void link_write_samples(uint32_t seq0, const int64_t *t_us, const uint32_t *ch0, const uint32_t *ch1, int n,
                        int64_t period_ns)
{
    (void)period_ns;
    static char buf[32 * 64];  // one FIFO read (<= 32 samples) of D lines
    size_t used = 0;
    for (int k = 0; k < n; k++) {
        if (used + PROTO_MAX_LINE > sizeof buf) {
            link_write(buf, used);
            used = 0;
        }
        pline_t l;
        pl_begin(&l, &buf[used], sizeof buf - used);
        pl_str(&l, "D,");
        pl_u64(&l, seq0 + (uint32_t)k);
        pl_char(&l, ',');
        pl_i64(&l, t_us[k]);
        pl_char(&l, ',');
        pl_u64(&l, ch0[k]);  // ch order: ir|red (two LEDs) or red
        if (ch1) {
            pl_char(&l, ',');
            pl_u64(&l, ch1[k]);
        }
        used += pl_end(&l);
    }
    if (used) link_write(buf, used);  // one write per FIFO read
}

uint32_t link_dropped_bytes(void)
{
    return s_dropped;
}

void link_flush(void)
{
}

void link_note_state(bool streaming, bool sensor_ok, uint32_t fs_mhz)
{
    (void)streaming;
    (void)sensor_ok;
    (void)fs_mhz;
}

bool link_epoch_ms(int64_t *ms)
{
    (void)ms;
    return false;  // the PC timestamps USB data itself
}

void link_status_changed(void)
{
}

void link_ota(const char *token)
{
    (void)token;  // USB/UART boards are flashed over the cable
}
