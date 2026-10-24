// Over-the-air update.
//
//   server -> <prefix>/dev/<ID>/cmd  "OTA <token>"   (only the NBE server may publish there, see the ACL)
//   node   -> GET <CONFIG_PPG_OTA_URL>/<token>       HTTPS, server cert checked against the CA bundle
//
// The image goes into the idle app slot (partitions.csv) and the node reboots into it. The new image
// is only kept once it has reached the broker (ota_mark_valid); a build that can't get back online
// is undone by the bootloader at the next reboot (CONFIG_PPG_NET_REBOOT_S, or a crash), so a bad
// update never needs a trip to the USB cable. Progress shows up as "ota" in the status message.
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "link.h"
#include "ota.h"

static const char *TAG = "ota";

static char s_state[48];
static char s_build[12];
static char s_url[192];
static atomic_bool s_running;

static void set_state(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_state, sizeof s_state, fmt, ap);
    va_end(ap);
    ESP_LOGI(TAG, "%s", s_state);
    link_status_changed();
}

const char *ota_state(void)
{
    return s_state;
}

const char *ota_build_id(void)
{
    if (!s_build[0]) esp_app_get_elf_sha256(s_build, 9);
    return s_build;
}

void ota_init(void)
{
    if (esp_ota_get_last_invalid_partition()) {
        snprintf(s_state, sizeof s_state, "rolled back");
        ESP_LOGW(TAG, "the last update did not come online and was rolled back");
    }
}

void ota_mark_valid(void)
{
    esp_ota_img_states_t st;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_ota_mark_app_valid_cancel_rollback();
        set_state("updated");
    }
}

static void ota_task(void *arg)
{
    (void)arg;
    esp_http_client_config_t http = {
        .url = s_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 20000,
        .buffer_size = 4096,
        .keep_alive_enable = true,
    };
    esp_https_ota_config_t cfg = {.http_config = &http};
    esp_https_ota_handle_t h = NULL;
    set_state("downloading");
    esp_err_t err = esp_https_ota_begin(&cfg, &h);
    if (err != ESP_OK) {
        set_state("failed: download refused (%s)", esp_err_to_name(err));  // expired link, server down...
        goto done;
    }
    esp_app_desc_t desc;
    if (esp_https_ota_get_img_desc(h, &desc) != ESP_OK || strcmp(desc.project_name, esp_app_get_description()->project_name)) {
        esp_https_ota_abort(h);
        set_state("failed: not a ppg_stream image");
        goto done;
    }
    int total = esp_https_ota_get_image_size(h), shown = 0;
    while ((err = esp_https_ota_perform(h)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        int pct = total > 0 ? (int)(100LL * esp_https_ota_get_image_len_read(h) / total) : 0;
        if (pct >= shown + 10) {
            shown = pct - pct % 10;
            set_state("downloading %d%%", shown);
        }
    }
    if (err == ESP_OK && !esp_https_ota_is_complete_data_received(h)) err = ESP_ERR_INVALID_SIZE;
    if (err != ESP_OK) {
        esp_https_ota_abort(h);
        set_state("failed: %s", esp_err_to_name(err));
        goto done;
    }
    err = esp_https_ota_finish(h);  // checks the image and switches the boot slot
    if (err != ESP_OK) {
        set_state("failed: %s", esp_err_to_name(err));
        goto done;
    }
    set_state("rebooting into %s", desc.version);
    vTaskDelay(pdMS_TO_TICKS(2000));  // let the status message go out
    esp_restart();
done:
    atomic_store(&s_running, false);
    vTaskDelete(NULL);
}

void link_ota(const char *token)
{
    size_t n = strlen(token);
    if (n < 16 || n > 64 || strspn(token, "0123456789ABCDEF") != n) {
        set_state("failed: bad OTA command");
        return;
    }
    if (!CONFIG_PPG_OTA_URL[0]) {
        set_state("failed: no OTA URL in this build");
        return;
    }
    if (atomic_exchange(&s_running, true)) return;  // one update at a time
    snprintf(s_url, sizeof s_url, "%s/%s", CONFIG_PPG_OTA_URL, token);
    if (xTaskCreatePinnedToCore(ota_task, "ppg_ota", 8192, NULL, 4, NULL, 0) != pdPASS) {
        atomic_store(&s_running, false);
        set_state("failed: out of memory");
    }
}
