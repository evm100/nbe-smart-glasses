#include "net.h"

#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#if CONFIG_PPG_WIFI_AUTH_EAP
#include "esp_eap_client.h"
#endif

static const char *TAG = "net";

static esp_netif_t *s_netif;
static volatile bool s_has_ip;
static volatile bool s_synced;
static void (*s_on_ip)(void);
static int s_backoff_ms = 500;
static esp_timer_handle_t s_retry_timer;
static int s_fails;  // consecutive connection failures
static volatile int64_t s_last_progress_us;  // last Wi-Fi event / retry: see net_watchdog()
static bool s_scan_next;
static wifi_ap_record_t s_scan[20];

#define NET_STUCK_US (30 * 1000000LL)  // no IP and no Wi-Fi event for this long = retry chain broke

static void retry_connect(void *arg)
{
    (void)arg;
    s_last_progress_us = esp_timer_get_time();
    if (s_scan_next) {
        s_scan_next = false;
        esp_err_t err = esp_wifi_scan_start(NULL, false);
        if (err == ESP_OK) return;  // connects again on SCAN_DONE
        ESP_LOGW(TAG, "diagnostic scan not started: %s", esp_err_to_name(err));
    }
    esp_wifi_connect();
}

static void time_synced_cb(struct timeval *tv)
{
    (void)tv;
    s_synced = true;
}

// After a failed connect, list what the radio sees: tells a wrong SSID / 5 GHz-only network /
// missing antenna (nothing, or only very weak signals) / security mismatch ("auth" column) apart.
static void log_scan(void)
{
    uint16_t total = 0, n = sizeof s_scan / sizeof s_scan[0];
    esp_wifi_scan_get_ap_num(&total);
    if (esp_wifi_scan_get_ap_records(&n, s_scan) != ESP_OK) n = 0;
    ESP_LOGW(TAG, "scan: %u networks visible on 2.4 GHz, looking for \"%s\"%s", total, CONFIG_PPG_WIFI_SSID,
             total ? "" : " - is the antenna attached?");
    for (int i = 0; i < n; i++) {
        ESP_LOGW(TAG, "  ch %2d %4d dBm auth %d  \"%s\"", s_scan[i].primary, s_scan[i].rssi, s_scan[i].authmode,
                 (const char *)s_scan[i].ssid);
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    s_last_progress_us = esp_timer_get_time();
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = data;
        s_has_ip = false;
        ESP_LOGW(TAG, "Wi-Fi disconnected (reason %d), retrying in %d ms", d ? d->reason : -1, s_backoff_ms);
        if (++s_fails % 10 == 1) s_scan_next = true;  // the retry runs a scan first
        esp_timer_start_once(s_retry_timer, (uint64_t)s_backoff_ms * 1000);  // don't block the event task
        if (s_backoff_ms < 8000) s_backoff_ms *= 2;
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
        log_scan();
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&e->ip_info.ip));
        s_has_ip = true;
        s_backoff_ms = 500;
        s_fails = 0;
        if (s_on_ip) s_on_ip();
    }
}

esp_err_t net_start(void (*on_ip)(void))
{
    s_on_ip = on_ip;
    ESP_LOGW(TAG, "node %s, built " __DATE__ " " __TIME__ ", Wi-Fi \"%s\"%s", CONFIG_PPG_DEVICE_ID,
             CONFIG_PPG_WIFI_SSID,
#if CONFIG_PPG_WIFI_AUTH_EAP
             " (PEAP user \"" CONFIG_PPG_WIFI_EAP_USERNAME "\")"
#else
             ""
#endif
    );
    const esp_timer_create_args_t ta = {.callback = retry_connect, .name = "wifi_retry"};
    ESP_ERROR_CHECK(esp_timer_create(&ta, &s_retry_timer));
    esp_err_t err = nvs_flash_init();  // Wi-Fi driver keeps calibration data in NVS
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(s_netif, "nbe-" CONFIG_PPG_DEVICE_ID);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));  // credentials come from sdkconfig

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, CONFIG_PPG_WIFI_SSID, sizeof wc.sta.ssid);
#if CONFIG_PPG_WIFI_AUTH_PSK
    strlcpy((char *)wc.sta.password, CONFIG_PPG_WIFI_PASSWORD, sizeof wc.sta.password);
    wc.sta.threshold.authmode = strlen(CONFIG_PPG_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable = true;
    wc.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;  // WPA3 phone hotspots / routers often require hash-to-element
#endif
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
#if CONFIG_PPG_WIFI_AUTH_EAP
    ESP_ERROR_CHECK(esp_eap_client_set_identity((const uint8_t *)CONFIG_PPG_WIFI_EAP_IDENTITY,
                                                strlen(CONFIG_PPG_WIFI_EAP_IDENTITY)));
    ESP_ERROR_CHECK(esp_eap_client_set_username((const uint8_t *)CONFIG_PPG_WIFI_EAP_USERNAME,
                                                strlen(CONFIG_PPG_WIFI_EAP_USERNAME)));
    ESP_ERROR_CHECK(esp_eap_client_set_password((const uint8_t *)CONFIG_PPG_WIFI_PASSWORD,
                                                strlen(CONFIG_PPG_WIFI_PASSWORD)));
#if CONFIG_PPG_WIFI_EAP_VALIDATE_SERVER
    ESP_ERROR_CHECK(esp_eap_client_use_default_cert_bundle(true));  // RADIUS cert must chain to a public CA
    if (CONFIG_PPG_WIFI_EAP_DOMAIN[0]) ESP_ERROR_CHECK(esp_eap_client_set_domain_name(CONFIG_PPG_WIFI_EAP_DOMAIN));
#endif
    ESP_ERROR_CHECK(esp_wifi_sta_enterprise_enable());
#endif
#if CONFIG_PPG_WIFI_HT20
    // 20 MHz channel: a phone hotspot's 40 MHz mode is easily disturbed on crowded 2.4 GHz
    esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20);
#endif
    ESP_ERROR_CHECK(esp_wifi_start());
    // a nearby hotspot doesn't need the full 20 dBm: smaller current spikes on the 3.3 V rail the
    // sensor shares, less self-interference (unit 0.25 dBm, rounded down to the PHY's steps)
    esp_err_t txp = esp_wifi_set_max_tx_power((int8_t)(CONFIG_PPG_WIFI_TX_POWER_DBM * 4));
    if (txp != ESP_OK) ESP_LOGW(TAG, "TX power not set: %s", esp_err_to_name(txp));
#if !CONFIG_PPG_WIFI_POWER_SAVE
    esp_wifi_set_ps(WIFI_PS_NONE);  // lower latency / fewer stalls while streaming, more current
#endif

    esp_sntp_config_t sc = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_PPG_SNTP_SERVER);
    sc.sync_cb = time_synced_cb;
    esp_netif_sntp_init(&sc);  // starts by itself once there is an IP
    return ESP_OK;
}

void net_watchdog(void)
{
    // The retry chain is event driven: a disconnect schedules retry_connect(), whose connect
    // attempt ends in the next GOT_IP or DISCONNECTED event. If an esp_wifi_connect() fails
    // without an event (driver busy, EAP state machine wedged...), nothing would ever retry.
    if (s_has_ip) return;
    int64_t now = esp_timer_get_time();
    if (now - s_last_progress_us < NET_STUCK_US) return;
    s_last_progress_us = now;
    ESP_LOGW(TAG, "no Wi-Fi progress for %lld s - restarting the connection attempt", NET_STUCK_US / 1000000);
    esp_wifi_disconnect();  // resets a half-done association; may itself produce DISCONNECTED
    esp_wifi_connect();
}

bool net_has_ip(void)
{
    return s_has_ip;
}

int net_rssi(void)
{
    wifi_ap_record_t ap;
    if (!s_has_ip || esp_wifi_sta_get_ap_info(&ap) != ESP_OK) return 0;
    return ap.rssi;
}

int net_tx_power_dbm(void)
{
    int8_t q = 0;
    return esp_wifi_get_max_tx_power(&q) == ESP_OK ? q / 4 : 0;
}

void net_ip_str(char *out, int len)
{
    esp_netif_ip_info_t ip;
    out[0] = 0;
    if (s_has_ip && esp_netif_get_ip_info(s_netif, &ip) == ESP_OK) {
        snprintf(out, (size_t)len, IPSTR, IP2STR(&ip.ip));
    }
}

bool net_time_synced(void)
{
    return s_synced;
}

int64_t net_epoch_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}
