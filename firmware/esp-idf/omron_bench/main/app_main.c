// Omron BLE bench test for the auto-cuff: can an ESP32 do what the Pi does with omblepy?
//
// Serial console on the USB-C port (`idf.py monitor`, 115200 or any baud). Commands:
//   status              MAC, key, bonds, last record seen
//   mac [AA:BB:..]      show / set the monitor's Bluetooth MAC (kept in flash)
//   key [32 hex]        show / set the unlock key (omblepy's default unless you paired with -k)
//   pair                one-time pairing; first hold the monitor's Bluetooth button until "P" blinks
//   read                download every stored record; NEW = newer than the last one seen
//   press               servo presses START once
//   measure             the Pi's full cycle: press, wait, download until a NEW record, press to power off
//   forget              delete the stored bond
//
// Machine-readable lines start with "REC," and "RESULT," (for scripting the bench test).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_console.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "omron_ble.h"
#include "servo.h"

static const char *TAG = "bench";

#define INITIAL_WAIT_MS  35000  // cuff_core.py CUFF_INITIAL_WAIT
#define OVERALL_MS      240000  // CUFF_TIMEOUT
#define MAX_EMPTY_READS 2
#define POWER_OFF_DELAY_MS 2000

static uint8_t s_mac[6];
static uint8_t s_key[16];
static char s_seen[24];  // newest record time already downloaded ("" = never read)
static om_record_t s_recs[OM_MAX_RECORDS];

// ---- settings in NVS -------------------------------------------------------------------------
static bool parse_mac(const char *s, uint8_t out[6])
{
    unsigned v[6];
    if (sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return false;
    for (int i = 0; i < 6; i++) {
        if (v[i] > 255) return false;
        out[i] = v[i];
    }
    return true;
}

static bool parse_key(const char *s, uint8_t out[16])
{
    if (strlen(s) != 32) return false;
    for (int i = 0; i < 16; i++) {
        unsigned b;
        if (sscanf(s + 2 * i, "%2x", &b) != 1) return false;
        out[i] = b;
    }
    return true;
}

static void save_blob(const char *k, const void *v, size_t n)
{
    nvs_handle_t h;
    if (nvs_open("bench", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, k, v, n);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void load_settings(void)
{
    parse_mac(CONFIG_BENCH_OMRON_MAC, s_mac);
    parse_key("deadbeaf12341234deadbeaf12341234", s_key);  // omblepy's pairingKey
    nvs_handle_t h;
    if (nvs_open("bench", NVS_READONLY, &h) != ESP_OK) return;
    size_t n = sizeof s_mac;
    nvs_get_blob(h, "mac", s_mac, &n);
    n = sizeof s_key;
    nvs_get_blob(h, "key", s_key, &n);
    n = sizeof s_seen;
    nvs_get_blob(h, "seen", s_seen, &n);
    nvs_close(h);
}

static void print_mac(void)
{
    printf("%02X:%02X:%02X:%02X:%02X:%02X", s_mac[0], s_mac[1], s_mac[2], s_mac[3], s_mac[4], s_mac[5]);
}

static void print_key(void)
{
    for (int i = 0; i < 16; i++) printf("%02x", s_key[i]);
}

// ---- reading ---------------------------------------------------------------------------------
static int cmp_rec(const void *a, const void *b)
{
    char x[24], y[24];
    om_record_time(a, x, sizeof x);
    om_record_time(b, y, sizeof y);
    return strcmp(x, y);
}

// Download, print every record, return how many are new (cuff_core.SeenStore.take_new).
// On the very first read there is no history, so only the newest record counts as new.
static int read_once(int scan_ms, bool print_all, om_record_t *newest)
{
    int n = 0;
    if (om_read(s_mac, s_key, scan_ms, s_recs, OM_MAX_RECORDS, &n) != ESP_OK) {
        printf("RESULT,error,%s\n", om_last_error());
        return -1;
    }
    qsort(s_recs, n, sizeof s_recs[0], cmp_rec);
    bool first = s_seen[0] == 0;
    int n_new = 0;
    char t[24];
    for (int i = 0; i < n; i++) {
        om_record_time(&s_recs[i], t, sizeof t);
        bool is_new = first ? (i == n - 1) : strcmp(t, s_seen) > 0;
        n_new += is_new;
        if (print_all || is_new)
            printf("REC,%s,%d,%d,%d,%d,%d%s\n", t, s_recs[i].sys, s_recs[i].dia, s_recs[i].bpm, s_recs[i].mov,
                   s_recs[i].ihb, is_new ? ",NEW" : "");
    }
    if (n > 0) {
        om_record_time(&s_recs[n - 1], t, sizeof t);
        if (strcmp(t, s_seen) > 0) {
            strlcpy(s_seen, t, sizeof s_seen);
            save_blob("seen", s_seen, sizeof s_seen);
        }
        if (newest) *newest = s_recs[n - 1];
    }
    printf("RESULT,ok,%d records,%d new\n", n, n_new);
    return n_new;
}

// ---- commands --------------------------------------------------------------------------------
static int cmd_status(int argc, char **argv)
{
    printf("monitor MAC: ");
    print_mac();
    printf("\nunlock key:  ");
    print_key();
    printf("\nbonds:       %d\nlast seen:   %s\nservo:       GPIO%d, %d us for %d ms\n", om_bond_count(),
           s_seen[0] ? s_seen : "(never read)", CONFIG_BENCH_SERVO_GPIO, CONFIG_BENCH_PRESS_US, CONFIG_BENCH_PRESS_MS);
    return 0;
}

static int cmd_mac(int argc, char **argv)
{
    if (argc > 1) {
        if (!parse_mac(argv[1], s_mac)) {
            printf("use: mac AA:BB:CC:DD:EE:FF\n");
            return 1;
        }
        save_blob("mac", s_mac, sizeof s_mac);
    }
    print_mac();
    printf("\n");
    return 0;
}

static int cmd_key(int argc, char **argv)
{
    if (argc > 1) {
        if (!parse_key(argv[1], s_key)) {
            printf("use: key <32 hex digits>\n");
            return 1;
        }
        save_blob("key", s_key, sizeof s_key);
    }
    print_key();
    printf("\n");
    return 0;
}

static int cmd_pair(int argc, char **argv)
{
    printf("Pairing: the monitor must be in pairing mode (hold its Bluetooth button until 'P' blinks).\n");
    esp_err_t err = om_pair(s_mac, s_key);
    if (err == ESP_OK)
        printf("RESULT,ok,paired (bond stored, key programmed)\n");
    else
        printf("RESULT,error,%s\n", om_last_error());
    return err == ESP_OK ? 0 : 1;
}

static int cmd_read(int argc, char **argv)
{
    printf("Reading: the monitor must be advertising (just after a reading, or press its Bluetooth button).\n");
    return read_once(30000, true, NULL) < 0;
}

static int cmd_press(int argc, char **argv)
{
    servo_press(CONFIG_BENCH_PRESS_US, CONFIG_BENCH_PRESS_MS);
    printf("RESULT,ok,pressed\n");
    return 0;
}

static int cmd_measure(int argc, char **argv)
{
    TickType_t t0 = xTaskGetTickCount();
    printf("pressing START\n");
    servo_press(CONFIG_BENCH_PRESS_US, CONFIG_BENCH_PRESS_MS);
    printf("waiting %d s for the cuff cycle\n", INITIAL_WAIT_MS / 1000);
    vTaskDelay(pdMS_TO_TICKS(INITIAL_WAIT_MS));
    int empty = 0, attempt = 0;
    while (pdTICKS_TO_MS(xTaskGetTickCount() - t0) < OVERALL_MS) {
        attempt++;
        om_record_t r;
        int n_new = read_once(30000, false, &r);
        if (n_new < 0) {
            printf("attempt %d: %s\n", attempt, om_last_error());
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        if (n_new > 0) {
            vTaskDelay(pdMS_TO_TICKS(POWER_OFF_DELAY_MS));
            printf("pressing START to power off\n");
            servo_press(CONFIG_BENCH_PRESS_US, CONFIG_BENCH_PRESS_MS);
            printf("RESULT,ok,measured %d/%d pulse %d in %lu s\n", r.sys, r.dia, r.bpm,
                   (unsigned long)(pdTICKS_TO_MS(xTaskGetTickCount() - t0) / 1000));
            return 0;
        }
        if (++empty >= MAX_EMPTY_READS) break;
        printf("attempt %d: read OK but no new record yet\n", attempt);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
    printf("RESULT,error,no new record (measurement error on the cuff?)\n");
    return 1;
}

static int cmd_forget(int argc, char **argv)
{
    om_forget();
    printf("RESULT,ok,bonds deleted; run `pair` again\n");
    return 0;
}

static void reg(const char *name, const char *help, esp_console_cmd_func_t fn)
{
    esp_console_cmd_t c = {.command = name, .help = help, .func = fn};
    ESP_ERROR_CHECK(esp_console_cmd_register(&c));
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    load_settings();
    ESP_ERROR_CHECK(servo_init(CONFIG_BENCH_SERVO_GPIO));
    err = om_init();
    if (err != ESP_OK) ESP_LOGE(TAG, "Bluetooth didn't start: %s", esp_err_to_name(err));

    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t rc = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    rc.prompt = "omron>";
    rc.task_stack_size = 8192;
    esp_console_dev_usb_serial_jtag_config_t hw = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&hw, &rc, &repl));
    esp_console_register_help_command();
    reg("status", "MAC, key, bonds, last record seen", cmd_status);
    reg("mac", "show / set the monitor MAC: mac AA:BB:CC:DD:EE:FF", cmd_mac);
    reg("key", "show / set the 16-byte unlock key (32 hex)", cmd_key);
    reg("pair", "one-time pairing (monitor in pairing mode first)", cmd_pair);
    reg("read", "download every stored record", cmd_read);
    reg("press", "servo presses START once", cmd_press);
    reg("measure", "press, wait, download the new record, press to power off", cmd_measure);
    reg("forget", "delete the stored Bluetooth bond", cmd_forget);
    printf("\nOmron BLE bench. Type `help`. Monitor ");
    print_mac();
    printf(", %d bond(s).\n", om_bond_count());
    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}
