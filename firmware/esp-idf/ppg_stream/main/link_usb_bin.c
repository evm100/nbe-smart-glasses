// USB binary link (CONFIG_PPG_LINK_USB_BIN): the node's output as binary records in CRC-checked
// envelopes on the XIAO's USB-C port, for the NBE USB gateway (nbe_gateway) or `sglab record`.
//
//   envelope  0xA5 0x5A | len u32 | crc32 u32 (of the records) | records        (frame.h)
//   records   the same ones the MQTT link sends: text (H/E/S lines), samples, camera frames
//
// Why binary: one 240x240 camera frame is 57.6 kB. As text (base64) every frame grows by a third;
// as records it is sent as is, and the PPG samples shrink to 2-5 bytes each like on MQTT.
//
// Two writers share the USB port, each holding s_usb for whole envelopes only:
//   tx_task      every TX_PERIOD_MS: everything in the small-record ring (text + samples) as one
//                envelope. The sensor task only appends to that ring, so a camera frame being
//                written (tens of ms) never delays a FIFO read.
//   camera task  link_write_frame(): one envelope per frame, written in chunks straight from the
//                camera's frame buffer (no copy).
//
// usb_serial_jtag_write_bytes() is all-or-nothing per call and a call can't exceed the driver's
// TX buffer, hence the chunks. When the host stops reading mid-frame the envelope is cut short;
// the PC finds the next 0xA5 0x5A, and the frame's seq is missing (counted as dropped there).
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "frame.h"
#include "link.h"
#include "proto.h"

#define TX_PERIOD_MS 10
#define RING_SIZE (256 * 1024)           // small records waiting for tx_task (PSRAM if present)
#define ENV_MAX (32 * 1024)              // largest small-record envelope
#define CHUNK 4096                       // bytes per usb_serial_jtag_write_bytes()
#define CHUNK_WAIT_MS 100                // host not reading for this long: give up the envelope

static SemaphoreHandle_t s_usb;          // whole envelopes on the wire
static SemaphoreHandle_t s_lock;         // the ring + s_srec
static uint8_t *s_ring, *s_env;
static size_t s_head, s_tail;            // ring positions (bytes, wrap at RING_SIZE)
static srec_t s_srec;
static uint8_t s_rec[SREC_MAX + 16];
static volatile uint32_t s_dropped;      // bytes (ring full, envelope cut short)
static volatile uint32_t s_frames_dropped;

// ---------------------------------------------------------------- ring (s_lock held)
static size_t ring_used(void)
{
    return (s_head + RING_SIZE - s_tail) % RING_SIZE;
}

static void append(const uint8_t *rec, size_t n)
{
    if (ring_used() + n >= RING_SIZE) {  // host gone for a long time: lose the newest
        s_dropped += (uint32_t)n;
        return;
    }
    size_t first = RING_SIZE - s_head < n ? RING_SIZE - s_head : n;
    memcpy(s_ring + s_head, rec, first);
    memcpy(s_ring, rec + first, n - first);
    s_head = (s_head + n) % RING_SIZE;
}

static void close_srec(void)
{
    if (s_srec.len) append(s_rec, srec_emit(&s_srec, s_rec));
}

// Whole records from the ring into s_env + ENV_HDR_LEN, at most ENV_MAX bytes.
static size_t take_records(void)
{
    size_t len = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    close_srec();
    while (s_tail != s_head) {
        uint8_t h[11];
        size_t avail = ring_used(), hn = avail < sizeof h ? avail : sizeof h;
        for (size_t i = 0; i < hn; i++) h[i] = s_ring[(s_tail + i) % RING_SIZE];
        uint64_t body = 0;
        size_t vl = frame_get_uvarint(h + 1, hn - 1, &body);
        size_t rl = 1 + vl + (size_t)body;
        if (!vl || ENV_HDR_LEN + len + rl > ENV_MAX) break;  // small records are < 3 kB
        for (size_t i = 0; i < rl; i++) s_env[ENV_HDR_LEN + len + i] = s_ring[(s_tail + i) % RING_SIZE];
        len += rl;
        s_tail = (s_tail + rl) % RING_SIZE;
    }
    xSemaphoreGive(s_lock);
    return len;
}

// ---------------------------------------------------------------- USB (s_usb held)
// false = the host didn't take it in time (the rest of this envelope is lost)
static bool usb_put(const uint8_t *p, size_t n)
{
    while (n) {
        size_t k = n < CHUNK ? n : CHUNK;
        if (usb_serial_jtag_write_bytes(p, k, pdMS_TO_TICKS(CHUNK_WAIT_MS)) != (int)k) return false;
        p += k;
        n -= k;
    }
    return true;
}

static void drain(void)
{
    size_t len;
    while ((len = take_records()) > 0) {
        env_header(s_env, (uint32_t)len, frame_crc32(0, s_env + ENV_HDR_LEN, len));
        if (!usb_serial_jtag_is_connected()) {  // no host: nothing reads the port
            s_dropped += (uint32_t)len;
            continue;
        }
        xSemaphoreTake(s_usb, portMAX_DELAY);
        bool ok = usb_put(s_env, ENV_HDR_LEN + len);
        xSemaphoreGive(s_usb);
        if (!ok) s_dropped += (uint32_t)len;
    }
}

static void tx_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(TX_PERIOD_MS));
        drain();
    }
}

#ifdef TEST_USB_BIN
void link_usb_bin_test_tick(void)  // host_test/sim.c: stands in for tx_task
{
    drain();
}
#endif

// ---------------------------------------------------------------- link API
esp_err_t link_init(void)
{
    s_usb = xSemaphoreCreateMutex();
    s_lock = xSemaphoreCreateMutex();
    s_ring = heap_caps_malloc(RING_SIZE, MALLOC_CAP_SPIRAM);
    if (!s_ring) s_ring = malloc(RING_SIZE);
    s_env = malloc(ENV_MAX);
    if (!s_usb || !s_lock || !s_ring || !s_env) return ESP_ERR_NO_MEM;
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = CONFIG_PPG_TX_BUFFER_BYTES;
    cfg.rx_buffer_size = 512;
    esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err != ESP_OK) return err;
    xTaskCreatePinnedToCore(tx_task, "ppg_usb_tx", 4096, NULL, 6, NULL, 0);
    return ESP_OK;
}

int link_write(const char *buf, size_t len)
{
    // whole protocol lines -> one text record each (after the samples before them)
    xSemaphoreTake(s_lock, portMAX_DELAY);
    close_srec();
    const char *p = buf, *end = buf + len;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        const char *e = nl ? nl : end;
        size_t n = (size_t)(e - p);
        if (n && p[n - 1] == '\r') n--;
        if (n && n <= PROTO_MAX_LINE) append(s_rec, rec_text(s_rec, p, n));
        p = nl ? nl + 1 : end;
    }
    xSemaphoreGive(s_lock);
    return (int)len;
}

void link_write_samples(uint32_t seq0, const int64_t *t_us, const uint32_t *ch0, const uint32_t *ch1, int n,
                        int64_t period_ns)
{
    uint32_t dt_nom = (uint32_t)((period_ns + 500) / 1000);
    int nch = ch1 ? 2 : 1;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (int k = 0; k < n; k++) {
        uint32_t v[FRAME_MAX_CH] = {ch0[k], ch1 ? ch1[k] : 0};
        if (!srec_add(&s_srec, seq0 + (uint32_t)k, t_us[k], v, nch, dt_nom)) {
            close_srec();
            srec_add(&s_srec, seq0 + (uint32_t)k, t_us[k], v, nch, dt_nom);
        }
    }
    xSemaphoreGive(s_lock);
}

bool link_write_frame(uint8_t sid, uint32_t seq, int64_t t_us, uint16_t w, uint16_t h, uint8_t fmt,
                      uint16_t exposure, uint8_t gain, const uint8_t *data, size_t len)
{
    if (!usb_serial_jtag_is_connected()) {
        s_frames_dropped++;
        return false;
    }
    uint8_t head[ENV_HDR_LEN + 6 + FRAME_REC_HDR_LEN];
    size_t hn = rec_frame_head(head + ENV_HDR_LEN, sid, seq, t_us, w, h, fmt, exposure, gain, len);
    uint32_t crc = frame_crc32(frame_crc32(0, head + ENV_HDR_LEN, hn), data, len);
    env_header(head, (uint32_t)(hn + len), crc);
    xSemaphoreTake(s_usb, portMAX_DELAY);
    bool ok = usb_put(head, ENV_HDR_LEN + hn) && usb_put(data, len);
    xSemaphoreGive(s_usb);
    if (!ok) {
        s_frames_dropped++;
        s_dropped += (uint32_t)(hn + len);
    }
    return ok;
}

uint32_t link_frames_dropped(void)
{
    return s_frames_dropped;
}

int link_read(uint8_t *buf, size_t max, uint32_t timeout_ms)
{
    int n = usb_serial_jtag_read_bytes(buf, (uint32_t)max, pdMS_TO_TICKS(timeout_ms));
    return n < 0 ? 0 : n;
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
    return false;  // the gateway pairs device time with UTC (it is on the internet, the board isn't)
}

void link_status_changed(void)
{
}

void link_ota(const char *token)
{
    (void)token;  // flashed over the cable
}
