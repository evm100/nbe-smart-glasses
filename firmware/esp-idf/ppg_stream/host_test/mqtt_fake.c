// Host test: fake esp-mqtt + net for link_mqtt.c, plus the server's side of the ACK protocol.
// Every data frame that "arrives" is appended to frames_mqtt.bin (u32 length + frame); run.sh
// reassembles them with sglab.framing into out_mqtt.txt for check_output.py.
// fake_mqtt_connected() simulates the broker going away; one frame is dropped silently and some
// ACKs are lost, so the node's resend paths (reconnect, RESEND, ACK timeout) all run.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_timer.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "mqtt_client.h"
#include "net.h"
#include "sdkconfig.h"

const char client_crt_start[] asm("_binary_client_crt_start") = "-----BEGIN CERTIFICATE-----";
const char client_key_start[] asm("_binary_client_key_start") = "-----BEGIN PRIVATE KEY-----";
#ifdef TEST_CA_PEM
const char broker_ca_start[] asm("_binary_broker_ca_crt_start") = "-----BEGIN CERTIFICATE-----";
#endif

static int client_obj;
static esp_event_handler_t s_handler;
static bool s_up;
int g_batches, g_max_batch, g_offline_publishes, g_dropped_frames, g_resends_asked;
static FILE *s_frames;
static uint64_t s_expected;           // the "server": stream offset it has everything before
static char s_reply[64];              // ACK / RESEND waiting for fake_mqtt_pump()

esp_mqtt_client_handle_t esp_mqtt_client_init(const esp_mqtt_client_config_t *c)
{
    if (!c->credentials.client_id || !c->session.last_will.topic || c->outbox.limit == 0 ||
        c->buffer.out_size < CONFIG_PPG_MQTT_BATCH_BUFFER_BYTES) abort();
    return (esp_mqtt_client_handle_t)&client_obj;
}
esp_err_t esp_mqtt_client_register_event(esp_mqtt_client_handle_t c, esp_mqtt_event_id_t e, esp_event_handler_t h, void *a)
{ (void)c; (void)e; (void)a; s_handler = h; return 0; }
static uint64_t le(const uint8_t *p, int n)
{
    uint64_t v = 0;
    for (int i = n - 1; i >= 0; i--) v = v << 8 | p[i];
    return v;
}

// delivers the server's reply to the node (like a cmd topic message), outside publish()
void fake_mqtt_pump(void)
{
    if (!s_reply[0] || !s_up) return;
    esp_mqtt_event_t ev = {.event_id = MQTT_EVENT_DATA, .client = (esp_mqtt_client_handle_t)&client_obj,
                           .topic = "nbe/dev/FNG01/cmd", .topic_len = 17, .data = s_reply,
                           .data_len = (int)strlen(s_reply)};
    s_handler(NULL, "MQTT", MQTT_EVENT_DATA, &ev);
    s_reply[0] = 0;
}

void fake_mqtt_connected(bool up)
{
    if (up == s_up) return;
    s_up = up;
    esp_mqtt_event_t ev = {.event_id = up ? MQTT_EVENT_CONNECTED : MQTT_EVENT_DISCONNECTED,
                           .client = (esp_mqtt_client_handle_t)&client_obj};
    s_handler(NULL, "MQTT", ev.event_id, &ev);
}
esp_err_t esp_mqtt_client_start(esp_mqtt_client_handle_t c)
{
    (void)c;
    fake_mqtt_connected(true);
    return 0;
}
esp_err_t esp_mqtt_client_reconnect(esp_mqtt_client_handle_t c) { (void)c; return 0; }
int esp_mqtt_client_subscribe(esp_mqtt_client_handle_t c, const char *t, int q) { (void)c; (void)t; (void)q; return 1; }
int esp_mqtt_client_publish(esp_mqtt_client_handle_t c, const char *topic, const char *d, int len, int q, int r)
{
    (void)c;
    if (strcmp(topic, "nbe/dev/FNG01/data")) return 1;  // status
    if (q != 0 || r) abort();
    if (!s_up) {
        g_offline_publishes++;
        return -1;  // like esp-mqtt: QoS 0 while disconnected
    }
    const uint8_t *f = (const uint8_t *)d;
    if (len < 21 || len > CONFIG_PPG_MQTT_BATCH_BUFFER_BYTES || f[0] != 'N' || f[1] != 'B' || f[2] != 2) {
        fprintf(stderr, "bad frame (len %d)\n", len);
        abort();
    }
    g_batches++;
    if (len > g_max_batch) g_max_batch = len;
    if (g_batches == 5) {  // lost on the way (e.g. broker dropped it): the server must notice the gap
        g_dropped_frames++;
        return 0;
    }
    if (!s_frames) s_frames = fopen("frames_mqtt.bin", "wb");
    uint32_t n32 = (uint32_t)len;
    fwrite(&n32, 4, 1, s_frames);
    fwrite(d, 1, (size_t)len, s_frames);
    fflush(s_frames);
    uint64_t off = le(f + 8, 8), end = off + (uint64_t)(len - 20);
    uint32_t boot = (uint32_t)le(f + 4, 4);
    if (off > s_expected) {
        snprintf(s_reply, sizeof s_reply, "RESEND %08lx %llu", (unsigned long)boot, (unsigned long long)s_expected);
        g_resends_asked++;
    } else {
        if (end > s_expected) s_expected = end;
        if (g_batches % 7 != 3)  // some ACKs get lost: the next one covers them
            snprintf(s_reply, sizeof s_reply, "ACK %08lx %llu", (unsigned long)boot, (unsigned long long)s_expected);
    }
    return 0;
}
int esp_mqtt_client_get_outbox_size(esp_mqtt_client_handle_t c) { (void)c; return 0; }
int esp_mqtt_client_enqueue(esp_mqtt_client_handle_t c, const char *topic, const char *d, int len, int q, int r, bool store)
{ (void)c; (void)topic; (void)d; (void)len; (void)q; (void)r; (void)store; abort(); }  // data goes through publish

StreamBufferHandle_t xStreamBufferCreate(size_t s, size_t t) { (void)s; (void)t; return (StreamBufferHandle_t)&client_obj; }
size_t xStreamBufferSend(StreamBufferHandle_t b, const void *d, size_t l, TickType_t w) { (void)b; (void)d; (void)w; return l; }
size_t xStreamBufferReceive(StreamBufferHandle_t b, void *d, size_t l, TickType_t w) { (void)b; (void)d; (void)l; (void)w; return 0; }
void xTaskNotifyGive(TaskHandle_t t) { (void)t; }
uint32_t ulTaskNotifyTake(BaseType_t c, TickType_t w) { (void)c; (void)w; return 0; }

esp_err_t net_start(void (*on_ip)(void)) { on_ip(); return 0; }
bool net_has_ip(void) { return true; }
void net_watchdog(void) {}
void esp_restart(void) { abort(); }
int net_rssi(void) { return -55; }
int net_tx_power_dbm(void) { return 11; }
void net_ip_str(char *out, int len) { snprintf(out, (size_t)len, "10.0.0.2"); }
void net_mac_str(char *out, int len) { snprintf(out, (size_t)len, "dc:da:0c:12:34:56"); }
void net_ssid_str(char *out, int len) { snprintf(out, (size_t)len, "UCSD-DEVICE"); }
bool net_time_synced(void) { return true; }
uint32_t esp_random(void) { return 0x5eed1234u; }
int64_t net_epoch_ms(void) { return 1790000000000LL + esp_timer_get_time() / 1000; }
esp_err_t esp_crt_bundle_attach(void *conf) { (void)conf; return 0; }

// ota.c needs the real ESP-IDF (checked by the Docker build); the status message only needs these
void ota_init(void) {}
void ota_mark_valid(void) {}
const char *ota_state(void) { return ""; }
const char *ota_build_id(void) { return "0123abcd"; }
void link_ota(const char *token) { (void)token; }
