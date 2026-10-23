// MQTT link: the node's output as acknowledged binary frames on <prefix>/dev/<ID>/data, commands
// from <prefix>/dev/<ID>/cmd.
//
//   status  <prefix>/dev/<ID>/status  retained JSON every CONFIG_PPG_MQTT_STATUS_PERIOD_S and on
//                                     every change; last will = {"online":false}
//   data    <prefix>/dev/<ID>/data    QoS 0 frames (frame.h): protocol lines as compact records
//   cmd     <prefix>/dev/<ID>/cmd     INFO | START | STOP | OTA <token>  -> fed to cmd_task through link_read()
//                                     ACK <boot> <offset> | RESEND <boot> <offset>  -> handled here
//   server  <prefix>/core/status      {"online":false} (server gone) -> STOP, so a node never streams
//                                     into the void for hours
//
// Reliable store-and-forward with end-to-end acknowledgement (the same idea as TCP, one level up,
// so it also survives reconnects, broker restarts and server restarts):
//   - The sensor task appends records to a large ring in PSRAM (CONFIG_PPG_MQTT_RING_KB, minutes of
//     data). It never waits for the network.
//   - tx_task sends frames from the first unsent byte every CONFIG_PPG_MQTT_BATCH_MS, as long as
//     less than CONFIG_PPG_MQTT_WINDOW_KB is unacknowledged.
//   - The server answers each frame with ACK <offset> (everything before it is stored); only then
//     is the space freed. After a reconnect, a RESEND from the server (it saw a gap) or ACK_TIMEOUT
//     without progress, the node goes back to the first unacknowledged byte and sends it all again
//     (go-back-N); the server drops what it already has.
//   - Only an outage longer than the ring loses data (tx_drop in the S line and the status).
// Frames are QoS 0: the acknowledgement above replaces esp-mqtt's outbox, which resends only one
// message per round trip after a reconnect.
//
// Security: TLS to the broker (server cert checked against the ESP-IDF CA bundle or an embedded CA)
// and a client certificate (CN = node ID) issued by the NBE server; the broker's ACL only lets this
// node use its own topics.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "mqtt_client.h"
#include "sdkconfig.h"

#if CONFIG_PPG_MQTT_CA_BUNDLE
#include "esp_crt_bundle.h"
#endif

#include "diag.h"
#include "frame.h"
#include "link.h"
#include "net.h"
#include "ota.h"
#include "proto.h"
#include "version.h"

static const char *TAG = "link_mqtt";

extern const char client_crt_start[] asm("_binary_client_crt_start");
extern const char client_key_start[] asm("_binary_client_key_start");
#if CONFIG_PPG_MQTT_CA_PEM
extern const char broker_ca_start[] asm("_binary_broker_ca_crt_start");
#endif

#define TOPIC_LEN 96
#define ACK_TIMEOUT_US (10 * 1000000LL)  // unacknowledged data and no ACK for this long: send it again
#define FRAME_MAX CONFIG_PPG_MQTT_BATCH_BUFFER_BYTES
#define WINDOW ((uint64_t)CONFIG_PPG_MQTT_WINDOW_KB * 1024)
static char s_t_status[TOPIC_LEN], s_t_data[TOPIC_LEN], s_t_cmd[TOPIC_LEN], s_t_core[TOPIC_LEN];

static esp_mqtt_client_handle_t s_client;
static volatile bool s_connected;
static StreamBufferHandle_t s_cmd;          // command bytes for link_read()
static TaskHandle_t s_tx_task;
static volatile bool s_streaming, s_sensor_ok;
static volatile uint32_t s_fs_mhz;
static volatile bool s_status_dirty = true;
static volatile bool s_flush_req;
static uint32_t s_boot;                     // random per boot: tells the server a new stream began

// ---- the stream: byte offsets since boot; [acked, head) is in the ring, [sent, head) not sent yet
static SemaphoreHandle_t s_lock;            // guards everything in this block
static uint8_t *s_ring;
static size_t s_ring_size;
static uint64_t s_acked, s_sent, s_head, s_peak;
static int64_t s_progress_us;               // last ACK progress (or start of waiting for one)
static srec_t s_srec;                       // samples record being built
static uint8_t s_rec[SREC_MAX + 16];        // one record on its way into the ring
static uint32_t s_dropped, s_resends;
static uint8_t *s_frame;                    // tx_task only: one frame

static const char *WILL = "{\"online\":false}";

// ---------------------------------------------------------------- ring (s_lock held)
static void ring_copy(uint64_t off, uint8_t *dst, const uint8_t *src, size_t n)
{
    size_t i = (size_t)(off % s_ring_size), first = s_ring_size - i < n ? s_ring_size - i : n;
    if (src) {
        memcpy(s_ring + i, src, first);
        memcpy(s_ring, src + first, n - first);
    } else {
        memcpy(dst, s_ring + i, first);
        memcpy(dst + first, s_ring, n - first);
    }
}

static void append(const uint8_t *rec, size_t n)
{
    if (s_head - s_acked + n > s_ring_size) {  // outage longer than the ring: lose the newest
        s_dropped += (uint32_t)n;
        return;
    }
    ring_copy(s_head, NULL, rec, n);
    s_head += n;
    if (s_head - s_acked > s_peak) s_peak = s_head - s_acked;
}

static void close_srec(void)
{
    if (s_srec.len) append(s_rec, srec_emit(&s_srec, s_rec));
}

// ---------------------------------------------------------------- status
static void publish_status(void)
{
    if (!s_connected) return;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    unsigned long buf = (unsigned long)s_peak, unacked = (unsigned long)(s_sent - s_acked);
    s_peak = s_head - s_acked;
    uint32_t dropped = s_dropped, resends = s_resends;
    xSemaphoreGive(s_lock);
    char ip[20], js[640];
    net_ip_str(ip, sizeof ip);
    int64_t ts = net_time_synced() ? net_epoch_ms() / 1000 : 0;
    // buf = most bytes held for the server since the last status (a few kB = the link keeps up)
    int n = snprintf(js, sizeof js,
                     "{\"online\":true,\"fw\":\"%s\",\"sensor\":\"MAX30102\",\"sensor_ok\":%s,\"site\":\"%s\","
                     "\"streaming\":%s,\"fs\":%lu.%03lu,\"rssi\":%d,\"ip\":\"%s\",\"uptime_s\":%lld,\"ts\":%lld,"
                     "\"heap\":%lu,\"buf\":%lu,\"unacked\":%lu,\"rtx\":%lu,\"tx_drop\":%lu,\"txp\":%d,"
                     "\"reset\":\"%s\",\"link\":\"mqtt\",\"build\":\"%s\",\"ota\":\"%s\"}",
                     PPG_FW_VERSION, s_sensor_ok ? "true" : "false", CONFIG_PPG_SITE, s_streaming ? "true" : "false",
                     (unsigned long)(s_fs_mhz / 1000), (unsigned long)(s_fs_mhz % 1000), net_rssi(), ip,
                     (long long)(esp_timer_get_time() / 1000000), (long long)ts,
                     (unsigned long)esp_get_free_heap_size(), buf, unacked, (unsigned long)resends,
                     (unsigned long)dropped, net_tx_power_dbm(), diag_reset_reason(), ota_build_id(), ota_state());
    if (n > 0 && n < (int)sizeof js) {
        esp_mqtt_client_publish(s_client, s_t_status, js, n, 1, 1);
        s_status_dirty = false;
    }
}

void link_note_state(bool streaming, bool sensor_ok, uint32_t fs_mhz)
{
    if (streaming != s_streaming || sensor_ok != s_sensor_ok || fs_mhz != s_fs_mhz) {
        s_streaming = streaming;
        s_sensor_ok = sensor_ok;
        s_fs_mhz = fs_mhz;
        s_status_dirty = true;
        if (s_tx_task) xTaskNotifyGive(s_tx_task);
    }
}

void link_status_changed(void)
{
    s_status_dirty = true;
    if (s_tx_task) xTaskNotifyGive(s_tx_task);
}

bool link_epoch_ms(int64_t *ms)
{
    if (!net_time_synced()) return false;
    *ms = net_epoch_ms();
    return true;
}

// ---------------------------------------------------------------- MQTT events
static bool says_offline(const char *p, int len)
{
    // tolerant check for {"online": false} without a JSON parser
    static const char key[] = "\"online\"";
    const char *e = p + len;
    for (const char *k = p; k + sizeof key - 1 <= e; k++) {
        if (memcmp(k, key, sizeof key - 1) != 0) continue;
        for (k += sizeof key - 1; k < e && (*k == ' ' || *k == ':'); k++) {
        }
        return k < e && *k == 'f';
    }
    return false;
}

// "ACK <boot hex> <offset>" / "RESEND <boot hex> <offset>": the server has everything before offset
static bool handle_ack(const char *p, int len)
{
    char s[64];
    bool resend = len > 7 && memcmp(p, "RESEND ", 7) == 0;
    if (!(resend || (len > 4 && memcmp(p, "ACK ", 4) == 0)) || len >= (int)sizeof s) return false;
    memcpy(s, p, (size_t)len);
    s[len] = 0;
    char *q = s + (resend ? 7 : 4), *e;
    uint32_t boot = (uint32_t)strtoul(q, &e, 16);
    uint64_t off = strtoull(e, NULL, 10);
    if (boot != s_boot) return true;  // for an earlier boot of this node
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (off > s_acked && off <= s_head) {
        s_acked = off;
        s_progress_us = esp_timer_get_time();
    }
    if (s_sent < s_acked) s_sent = s_acked;
    if (resend && s_sent > s_acked) {  // the server saw a gap: go back
        s_sent = s_acked;
        s_resends++;
    }
    xSemaphoreGive(s_lock);
    return true;
}

static void on_mqtt(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    esp_mqtt_event_handle_t ev = data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "connected to broker");
        xSemaphoreTake(s_lock, portMAX_DELAY);  // what was in flight may be lost: send it again
        if (s_sent > s_acked) s_resends++;
        s_sent = s_acked;
        s_progress_us = esp_timer_get_time();
        xSemaphoreGive(s_lock);
        s_connected = true;
        ota_mark_valid();  // a freshly updated image made it back online: keep it
        esp_mqtt_client_subscribe(s_client, s_t_cmd, 1);
        esp_mqtt_client_subscribe(s_client, s_t_core, 1);
        s_status_dirty = true;
        if (s_tx_task) xTaskNotifyGive(s_tx_task);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "broker connection lost (auto-reconnect)");
        s_connected = false;
        break;
    case MQTT_EVENT_DATA:
        if (ev->topic_len == (int)strlen(s_t_cmd) && memcmp(ev->topic, s_t_cmd, ev->topic_len) == 0) {
            if (handle_ack(ev->data, ev->data_len)) break;
            xStreamBufferSend(s_cmd, ev->data, ev->data_len, 0);
            xStreamBufferSend(s_cmd, "\n", 1, 0);
        } else if (ev->topic_len == (int)strlen(s_t_core) && memcmp(ev->topic, s_t_core, ev->topic_len) == 0) {
#if CONFIG_PPG_MQTT_STOP_WHEN_SERVER_GONE
            if (s_streaming && says_offline(ev->data, ev->data_len)) {
                ESP_LOGW(TAG, "server went offline - stopping the stream");
                xStreamBufferSend(s_cmd, "STOP\n", 5, 0);
            }
#endif
        }
        break;
    case MQTT_EVENT_ERROR:
        if (ev->error_handle && ev->error_handle->error_type == MQTT_ERROR_TYPE_CONNECTION_REFUSED) {
            ESP_LOGE(TAG, "broker refused the connection (code %d): certificate not accepted / ACL?",
                     ev->error_handle->connect_return_code);
        } else if (ev->error_handle && ev->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            ESP_LOGE(TAG, "transport error: tls 0x%x, sock errno %d (wrong broker URI / port blocked / CA?)",
                     ev->error_handle->esp_tls_last_esp_err, ev->error_handle->esp_transport_sock_errno);
        }
        break;
    default:
        break;
    }
}

// ---------------------------------------------------------------- sender
// Next frame: whole records from s_sent, at most FRAME_MAX bytes and within the window.
static size_t build_frame(uint64_t *off)
{
    size_t len = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);  // copying under the lock: an ACK may free ring space meanwhile
    close_srec();
    uint64_t pos = s_sent;
    while (pos < s_head && pos - s_acked < WINDOW) {
        uint8_t h[11];
        size_t hn = s_head - pos < sizeof h ? (size_t)(s_head - pos) : sizeof h;
        ring_copy(pos, h, NULL, hn);
        uint64_t body = 0;
        size_t vl = frame_get_uvarint(h + 1, hn - 1, &body);
        size_t rl = 1 + vl + (size_t)body;
        if (!vl || FRAME_HDR_LEN + len + rl > FRAME_MAX) break;  // records are < FRAME_MAX (Kconfig range)
        ring_copy(pos, s_frame + FRAME_HDR_LEN + len, NULL, rl);
        len += rl;
        pos += rl;
    }
    *off = s_sent;
    if (len && s_sent == s_acked) s_progress_us = esp_timer_get_time();  // starts waiting for an ACK
    s_sent = pos;
    xSemaphoreGive(s_lock);
    if (!len) return 0;
    frame_header(s_frame, s_boot, *off, s_frame + FRAME_HDR_LEN, len);
    return FRAME_HDR_LEN + len;
}

static void send_frames(void)
{
    uint64_t off;
    size_t n;
    while ((n = build_frame(&off)) > 0) {
        // QoS 0: written to TLS now, or -1 (not connected / the write failed): send it again later
        if (esp_mqtt_client_publish(s_client, s_t_data, (const char *)s_frame, (int)n, 0, 0) < 0) {
            xSemaphoreTake(s_lock, portMAX_DELAY);
            if (s_sent > off && off >= s_acked) s_sent = off;
            xSemaphoreGive(s_lock);
            break;
        }
    }
}

static void check_ack_timeout(int64_t now)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_sent > s_acked && now - s_progress_us > ACK_TIMEOUT_US) {
        ESP_LOGW(TAG, "no ACK for %lld s - sending %llu bytes again", ACK_TIMEOUT_US / 1000000,
                 (unsigned long long)(s_sent - s_acked));
        s_sent = s_acked;
        s_progress_us = now;
        s_resends++;
    }
    xSemaphoreGive(s_lock);
}

void link_flush(void)
{
    // STOP: send the tail of the recording now instead of at the next batch
    s_flush_req = true;
    if (s_tx_task) xTaskNotifyGive(s_tx_task);
}

#ifdef TEST_MQTT
// host_test/sim.c has no tasks: it calls this once per batch period instead of tx_task
void link_mqtt_test_tick(void)
{
    if (!s_connected) return;
    check_ack_timeout(esp_timer_get_time());
    send_frames();
}
#endif

static void tx_task(void *arg)
{
    (void)arg;
    int64_t last_status = 0, last_batch = 0;
    int64_t last_up = esp_timer_get_time();
    for (;;) {
        int64_t wait_ms = CONFIG_PPG_MQTT_BATCH_MS - (esp_timer_get_time() - last_batch) / 1000;
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(wait_ms > 0 ? wait_ms : 0));
        int64_t now = esp_timer_get_time();
        if (s_flush_req || now - last_batch >= (int64_t)CONFIG_PPG_MQTT_BATCH_MS * 1000) {
            last_batch = now;
            s_flush_req = false;
            if (s_connected) {  // offline: everything waits in the ring
                check_ack_timeout(now);
                send_frames();
            }
        }
        net_watchdog();
        if (s_connected) last_up = now;
#if CONFIG_PPG_NET_REBOOT_S > 0
        // last resort: a fresh boot clears whatever wedged Wi-Fi / TLS / esp-mqtt. The server
        // sends START again when the node comes back, so a running trial continues by itself.
        if (now - last_up > (int64_t)CONFIG_PPG_NET_REBOOT_S * 1000000) {
            ESP_LOGE(TAG, "no broker connection for %d s - rebooting", CONFIG_PPG_NET_REBOOT_S);
            esp_restart();
        }
#endif
        if (s_connected && (s_status_dirty || now - last_status >= (int64_t)CONFIG_PPG_MQTT_STATUS_PERIOD_S * 1000000)) {
            last_status = now;
            publish_status();
        }
    }
}

// ---------------------------------------------------------------- link API
static void on_ip(void)
{
    // esp-mqtt reconnects by itself; this just shortens the first connection after boot
    if (s_client && !s_connected) esp_mqtt_client_reconnect(s_client);
}

esp_err_t link_init(void)
{
    snprintf(s_t_status, TOPIC_LEN, "%s/dev/%s/status", CONFIG_PPG_MQTT_TOPIC_PREFIX, CONFIG_PPG_DEVICE_ID);
    snprintf(s_t_data, TOPIC_LEN, "%s/dev/%s/data", CONFIG_PPG_MQTT_TOPIC_PREFIX, CONFIG_PPG_DEVICE_ID);
    snprintf(s_t_cmd, TOPIC_LEN, "%s/dev/%s/cmd", CONFIG_PPG_MQTT_TOPIC_PREFIX, CONFIG_PPG_DEVICE_ID);
    snprintf(s_t_core, TOPIC_LEN, "%s/core/status", CONFIG_PPG_MQTT_TOPIC_PREFIX);

    ota_init();
    s_boot = esp_random();
    s_lock = xSemaphoreCreateMutex();
    s_cmd = xStreamBufferCreate(256, 1);
    // large buffers land in PSRAM (CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL); boards without it get a smaller ring
    s_ring_size = (size_t)CONFIG_PPG_MQTT_RING_KB * 1024;
    s_ring = malloc(s_ring_size);
    if (!s_ring) {
        s_ring_size = 64 * 1024;
        s_ring = malloc(s_ring_size);
        ESP_LOGW(TAG, "no PSRAM: send buffer %u kB", (unsigned)(s_ring_size / 1024));
    }
    s_frame = malloc(FRAME_MAX);
    if (!s_lock || !s_cmd || !s_ring || !s_frame) return ESP_ERR_NO_MEM;

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = CONFIG_PPG_MQTT_BROKER_URI,
#if CONFIG_PPG_MQTT_CA_BUNDLE
        .broker.verification.crt_bundle_attach = esp_crt_bundle_attach,
#else
        .broker.verification.certificate = broker_ca_start,
#endif
        .credentials.client_id = CONFIG_PPG_DEVICE_ID,
        .credentials.authentication.certificate = client_crt_start,
        .credentials.authentication.key = client_key_start,
        .session.keepalive = 15,  // notice a dead connection within ~20 s, then resend from the last ACK
        .session.disable_clean_session = false,
        .session.last_will.topic = s_t_status,
        .session.last_will.msg = WILL,
        .session.last_will.msg_len = 0,  // 0 = strlen(msg)
        .session.last_will.qos = 1,
        .session.last_will.retain = 1,
        .session.message_retransmit_timeout = 5000,  // status messages (QoS 1); data has its own ACKs
        .network.reconnect_timeout_ms = 2000,
        .network.timeout_ms = 10000,
        .buffer.size = 1024,
        .buffer.out_size = FRAME_MAX + 256,
        .outbox.limit = (uint64_t)CONFIG_PPG_MQTT_OUTBOX_LIMIT_KB * 1024,
        .task.stack_size = 6144,
    };
    s_client = esp_mqtt_client_init(&cfg);
    if (!s_client) return ESP_FAIL;
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, on_mqtt, NULL);

    esp_err_t err = net_start(on_ip);
    if (err != ESP_OK) return err;
    err = esp_mqtt_client_start(s_client);  // connects once Wi-Fi is up, keeps retrying
    if (err != ESP_OK) return err;
    xTaskCreatePinnedToCore(tx_task, "ppg_mqtt_tx", 6144, NULL, 6, &s_tx_task, 0);
    ESP_LOGI(TAG, "node %s -> %s (topics %s/dev/%s/..., boot %08lx, buffer %u kB)", CONFIG_PPG_DEVICE_ID,
             CONFIG_PPG_MQTT_BROKER_URI, CONFIG_PPG_MQTT_TOPIC_PREFIX, CONFIG_PPG_DEVICE_ID, (unsigned long)s_boot,
             (unsigned)(s_ring_size / 1024));
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

int link_read(uint8_t *buf, size_t max, uint32_t timeout_ms)
{
    return (int)xStreamBufferReceive(s_cmd, buf, max, pdMS_TO_TICKS(timeout_ms));
}

uint32_t link_dropped_bytes(void)
{
    return s_dropped;  // only read for reporting: a torn read is harmless
}
