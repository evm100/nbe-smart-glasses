// Omron HEM-7150T over BLE with NimBLE. Mirrors auto-cuff/omblepy/omblepy.py + sharedDriver.py +
// deviceSpecific/hem-7150t.py; comments name the omblepy function each piece comes from.
//
// Threading: NimBLE callbacks run in the host task and only copy data / set event bits; the
// blocking API (om_pair / om_read) runs in the caller's task and waits on those bits.
#include "omron_ble.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/ble_store.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "store/config/ble_store_config.h"

void ble_store_config_init(void);  // not in the header (same as the IDF blecent example)

static const char *TAG = "omron";

// ---- UUIDs (omblepy LEGACY_*) -------------------------------------------------------------
static const char *SVC_UUID = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b";
static const char *RX_UUIDS[4] = {
    "49123040-aee8-11e1-a74d-0002a5d5c51b", "4d0bf320-aee8-11e1-a0d9-0002a5d5c51b",
    "5128ce60-aee8-11e1-b84b-0002a5d5c51b", "560f1420-aee8-11e1-8184-0002a5d5c51b"};
static const char *TX_UUIDS[4] = {
    "db5b55e0-aee7-11e1-965e-0002a5d5c51b", "e0b8a060-aee7-11e1-92f4-0002a5d5c51b",
    "0ae12b00-aee8-11e1-a192-0002a5d5c51b", "10e1ba60-aee8-11e1-89e5-0002a5d5c51b"};
static const char *UNLOCK_UUID = "b305b680-aee7-11e1-a730-0002a5d5c51b";

// ---- HEM-7150T layout (deviceSpecific/hem-7150t.py) -----------------------------------------
#define USER_START_ADDR 0x0098
#define RECORD_SIZE     0x10
#define BLOCK_SIZE      0x10

// ---- event bits ------------------------------------------------------------------------------
#define EV_SYNC       BIT0
#define EV_FOUND      BIT1   // target seen while scanning
#define EV_SCAN_END   BIT2
#define EV_CONNECTED  BIT3
#define EV_DISCONN    BIT4
#define EV_ENC        BIT5   // encryption change (s_enc_status)
#define EV_OP         BIT6   // GATT procedure finished (s_op_status)
#define EV_RX         BIT7   // complete RX packet (s_pkt) or RX error (s_rx_err)
#define EV_UNLOCK     BIT8   // notification on the unlock characteristic (s_unlock)

static EventGroupHandle_t s_ev;
static uint8_t s_own_addr_type;
static char s_err[160];

static uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static ble_addr_t s_target;  // address (with type) as seen in the advertisement
static uint8_t s_target_val[6];
static int s_conn_status, s_disc_reason, s_enc_status, s_op_status;

// discovered handles
#define MAX_CHRS 16
static struct { uint16_t def, val; uint8_t props; ble_uuid_any_t uuid; } s_chrs[MAX_CHRS];
static int s_n_chrs;
static uint16_t s_svc_start, s_svc_end;
static uint16_t s_rx_val[4], s_tx_val[4], s_unlock_val;
static uint16_t s_rx_cccd[4], s_unlock_cccd;
static uint8_t s_rx_props[4], s_unlock_props;
static uint16_t s_dsc_found;  // CCCD handle found by the descriptor discovery in progress

// RX assembly (bluetoothTxRxHandler._callbackForRxChannels)
static uint8_t s_chbuf[4][32];
static uint8_t s_chlen[4];
static uint8_t s_have;  // bitmask of channels received
static uint8_t s_pkt[128];
static int s_pkt_len;
static bool s_rx_err;
static uint8_t s_unlock[32];
static int s_unlock_len;

static void fail(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_err, sizeof s_err, fmt, ap);
    va_end(ap);
    ESP_LOGW(TAG, "%s", s_err);
}

const char *om_last_error(void) { return s_err; }

static bool wait_bits(EventBits_t bits, int ms)
{
    return (xEventGroupWaitBits(s_ev, bits, pdTRUE, pdFALSE, pdMS_TO_TICKS(ms)) & bits) != 0;
}

// ---- GAP ---------------------------------------------------------------------------------------
static void on_notify(uint16_t handle, struct os_mbuf *om)
{
    uint8_t buf[32];
    int len = OS_MBUF_PKTLEN(om);
    if (len > (int)sizeof buf) len = sizeof buf;
    os_mbuf_copydata(om, 0, len, buf);

    if (handle == s_unlock_val) {  // _callbackForUnlockChannel
        memcpy(s_unlock, buf, len);
        s_unlock_len = len;
        xEventGroupSetBits(s_ev, EV_UNLOCK);
        return;
    }
    int ch = -1;
    for (int i = 0; i < 4; i++)
        if (handle == s_rx_val[i]) ch = i;
    if (ch < 0) return;
    memcpy(s_chbuf[ch], buf, len);
    s_chlen[ch] = len;
    s_have |= 1 << ch;
    if (!(s_have & 1)) return;  // the size byte lives in channel 0

    int size = s_chbuf[0][0];
    int need = (size + 15) / 16;
    if (need < 1 || need > 4) {
        s_have = 0;
        s_rx_err = true;
        xEventGroupSetBits(s_ev, EV_RX);
        return;
    }
    for (int i = 0; i < need; i++)
        if (!(s_have & (1 << i))) return;  // wait for the other channels
    int n = 0;
    for (int i = 0; i < need && n < (int)sizeof s_pkt; i++) {
        int k = s_chlen[i];
        if (n + k > (int)sizeof s_pkt) k = sizeof s_pkt - n;
        memcpy(s_pkt + n, s_chbuf[i], k);
        n += k;
    }
    s_have = 0;
    if (n > size) n = size;
    uint8_t x = 0;
    for (int i = 0; i < n; i++) x ^= s_pkt[i];
    s_pkt_len = n;
    s_rx_err = (x != 0 || n < 6);
    xEventGroupSetBits(s_ev, EV_RX);
}

static int gap_cb(struct ble_gap_event *e, void *arg)
{
    struct ble_gap_conn_desc desc;
    switch (e->type) {
    case BLE_GAP_EVENT_DISC:
        if (memcmp(e->disc.addr.val, s_target_val, 6) == 0) {
            s_target = e->disc.addr;
            ble_gap_disc_cancel();
            xEventGroupSetBits(s_ev, EV_FOUND);
        }
        return 0;
    case BLE_GAP_EVENT_DISC_COMPLETE:
        xEventGroupSetBits(s_ev, EV_SCAN_END);
        return 0;
    case BLE_GAP_EVENT_CONNECT:
        s_conn_status = e->connect.status;
        if (e->connect.status == 0) {
            s_conn = e->connect.conn_handle;
            xEventGroupSetBits(s_ev, EV_CONNECTED);
        } else {
            xEventGroupSetBits(s_ev, EV_DISCONN);
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        s_disc_reason = e->disconnect.reason;
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        xEventGroupSetBits(s_ev, EV_DISCONN | EV_OP | EV_RX | EV_UNLOCK | EV_ENC);  // unblock any waiter
        return 0;
    case BLE_GAP_EVENT_ENC_CHANGE:
        s_enc_status = e->enc_change.status;
        xEventGroupSetBits(s_ev, EV_ENC);
        return 0;
    case BLE_GAP_EVENT_NOTIFY_RX:
        on_notify(e->notify_rx.attr_handle, e->notify_rx.om);
        return 0;
    case BLE_GAP_EVENT_REPEAT_PAIRING:
        // the monitor forgot us (re-paired elsewhere): drop the old bond and pair again
        if (ble_gap_conn_find(e->repeat_pairing.conn_handle, &desc) == 0)
            ble_store_util_delete_peer(&desc.peer_id_addr);
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "MTU %d", e->mtu.value);
        return 0;
    default:
        return 0;
    }
}

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_own_addr_type);
    xEventGroupSetBits(s_ev, EV_SYNC);
}

static void on_reset(int reason) { ESP_LOGW(TAG, "host reset, reason %d", reason); }

static void host_task(void *p)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t om_init(void)
{
    s_ev = xEventGroupCreate();
    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) return err;
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    // "Just Works" bonding, as BlueZ does with the monitor
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 0;
    ble_hs_cfg.sm_sc = 1;  // falls back to legacy pairing if the monitor doesn't do Secure Connections
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_store_config_init();
    nimble_port_freertos_init(host_task);
    return wait_bits(EV_SYNC, 5000) ? ESP_OK : ESP_ERR_TIMEOUT;
}

void om_forget(void) { ble_store_clear(); }

int om_bond_count(void)
{
    int n = 0;
    ble_store_util_count(BLE_STORE_OBJ_TYPE_PEER_SEC, &n);
    return n;
}

// ---- connection ------------------------------------------------------------------------------
static void disconnect(void)
{
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) {
        xEventGroupClearBits(s_ev, EV_DISCONN);
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
        wait_bits(EV_DISCONN, 3000);
    }
    s_conn = BLE_HS_CONN_HANDLE_NONE;
}

static bool connected(void) { return s_conn != BLE_HS_CONN_HANDLE_NONE; }

static esp_err_t scan_and_connect(const uint8_t mac[6], int scan_ms)
{
    // NimBLE keeps addresses little-endian: "28:FF:..:D5" -> val[5] = 0x28 ... val[0] = 0xD5
    for (int i = 0; i < 6; i++) s_target_val[i] = mac[5 - i];
    xEventGroupClearBits(s_ev, EV_FOUND | EV_SCAN_END | EV_CONNECTED | EV_DISCONN);
    struct ble_gap_disc_params dp = {.passive = 0, .filter_duplicates = 1};  // active, like bleak
    int rc = ble_gap_disc(s_own_addr_type, scan_ms, &dp, gap_cb, NULL);
    if (rc != 0) {
        fail("scan failed to start (rc=%d)", rc);
        return ESP_FAIL;
    }
    EventBits_t b = xEventGroupWaitBits(s_ev, EV_FOUND | EV_SCAN_END, pdTRUE, pdFALSE, pdMS_TO_TICKS(scan_ms + 2000));
    if (!(b & EV_FOUND)) {
        ble_gap_disc_cancel();
        fail("monitor not advertising within %d s (take a reading or press its Bluetooth button)", scan_ms / 1000);
        return ESP_ERR_NOT_FOUND;
    }
    vTaskDelay(pdMS_TO_TICKS(50));  // let the scan stop
    ESP_LOGI(TAG, "found monitor (addr type %d), connecting", s_target.type);
    rc = ble_gap_connect(s_own_addr_type, &s_target, 10000, NULL, gap_cb, NULL);
    if (rc != 0) {
        fail("connect failed to start (rc=%d)", rc);
        return ESP_FAIL;
    }
    b = xEventGroupWaitBits(s_ev, EV_CONNECTED | EV_DISCONN, pdTRUE, pdFALSE, pdMS_TO_TICKS(12000));
    if (!(b & EV_CONNECTED)) {
        fail("connection failed (status=%d)", s_conn_status);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "connected");
    return ESP_OK;
}

// Encrypt the link: with a stored bond this just restores encryption; without one it pairs
// (which the monitor only accepts in pairing mode).
static esp_err_t secure(void)
{
    xEventGroupClearBits(s_ev, EV_ENC);
    int rc = ble_gap_security_initiate(s_conn);
    if (rc == BLE_HS_EALREADY) return ESP_OK;
    if (rc != 0) {
        fail("could not start encryption (rc=%d)", rc);
        return ESP_FAIL;
    }
    if (!wait_bits(EV_ENC, 15000) || !connected()) {
        fail("no encryption: %s", connected() ? "timed out" : "monitor disconnected");
        return ESP_FAIL;
    }
    if (s_enc_status != 0) {
        fail("encryption/pairing failed (status=%d). Not bonded yet? Put the monitor in pairing mode and run `pair`",
             s_enc_status);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "link encrypted");
    return ESP_OK;
}

// ---- GATT discovery ------------------------------------------------------------------------
static int svc_cb(uint16_t c, const struct ble_gatt_error *err, const struct ble_gatt_svc *svc, void *arg)
{
    if (err->status == 0 && svc) {
        s_svc_start = svc->start_handle;
        s_svc_end = svc->end_handle;
        return 0;
    }
    s_op_status = err->status == BLE_HS_EDONE ? 0 : err->status;
    xEventGroupSetBits(s_ev, EV_OP);
    return 0;
}

static int chr_cb(uint16_t c, const struct ble_gatt_error *err, const struct ble_gatt_chr *chr, void *arg)
{
    if (err->status == 0 && chr) {
        if (s_n_chrs < MAX_CHRS) {
            s_chrs[s_n_chrs].def = chr->def_handle;
            s_chrs[s_n_chrs].val = chr->val_handle;
            s_chrs[s_n_chrs].props = chr->properties;
            s_chrs[s_n_chrs].uuid = chr->uuid;
            s_n_chrs++;
        }
        return 0;
    }
    s_op_status = err->status == BLE_HS_EDONE ? 0 : err->status;
    xEventGroupSetBits(s_ev, EV_OP);
    return 0;
}

static int dsc_cb(uint16_t c, const struct ble_gatt_error *err, uint16_t chr_val, const struct ble_gatt_dsc *dsc, void *arg)
{
    if (err->status == 0 && dsc) {
        if (ble_uuid_cmp(&dsc->uuid.u, BLE_UUID16_DECLARE(BLE_GATT_DSC_CLT_CFG_UUID16)) == 0) s_dsc_found = dsc->handle;
        return 0;
    }
    s_op_status = err->status == BLE_HS_EDONE ? 0 : err->status;
    xEventGroupSetBits(s_ev, EV_OP);
    return 0;
}

static bool wait_op(int ms)
{
    if (!wait_bits(EV_OP, ms) || !connected()) {
        fail("GATT operation %s", connected() ? "timed out" : "interrupted: monitor disconnected");
        return false;
    }
    return true;
}

static bool uuid_is(const ble_uuid_any_t *u, const char *s)
{
    ble_uuid_any_t want;
    return ble_uuid_from_str(&want, s) == 0 && ble_uuid_cmp(&u->u, &want.u) == 0;
}

static uint16_t find_cccd(uint16_t val)
{
    uint16_t end = s_svc_end;
    for (int i = 0; i < s_n_chrs; i++)
        if (s_chrs[i].def > val && s_chrs[i].def - 1 < end) end = s_chrs[i].def - 1;
    if (end <= val) return 0;
    s_dsc_found = 0;
    xEventGroupClearBits(s_ev, EV_OP);
    if (ble_gattc_disc_all_dscs(s_conn, val, end, dsc_cb, NULL) != 0 || !wait_op(5000)) return 0;
    return s_dsc_found;
}

static esp_err_t discover(void)
{
    ble_uuid_any_t svc;
    ble_uuid_from_str(&svc, SVC_UUID);
    s_svc_start = s_svc_end = 0;
    xEventGroupClearBits(s_ev, EV_OP);
    if (ble_gattc_disc_svc_by_uuid(s_conn, &svc.u, svc_cb, NULL) != 0 || !wait_op(8000)) return ESP_FAIL;
    if (!s_svc_start) {
        fail("Omron service not found: wrong device?");
        return ESP_ERR_NOT_FOUND;
    }
    s_n_chrs = 0;
    xEventGroupClearBits(s_ev, EV_OP);
    if (ble_gattc_disc_all_chrs(s_conn, s_svc_start, s_svc_end, chr_cb, NULL) != 0 || !wait_op(8000)) return ESP_FAIL;
    memset(s_rx_val, 0, sizeof s_rx_val);
    memset(s_tx_val, 0, sizeof s_tx_val);
    s_unlock_val = 0;
    for (int i = 0; i < s_n_chrs; i++) {
        for (int ch = 0; ch < 4; ch++) {
            if (uuid_is(&s_chrs[i].uuid, RX_UUIDS[ch])) { s_rx_val[ch] = s_chrs[i].val; s_rx_props[ch] = s_chrs[i].props; }
            if (uuid_is(&s_chrs[i].uuid, TX_UUIDS[ch])) s_tx_val[ch] = s_chrs[i].val;
        }
        if (uuid_is(&s_chrs[i].uuid, UNLOCK_UUID)) { s_unlock_val = s_chrs[i].val; s_unlock_props = s_chrs[i].props; }
    }
    for (int ch = 0; ch < 4; ch++) {
        if (!s_rx_val[ch] || !s_tx_val[ch]) {
            fail("channel %d characteristics missing (found %d characteristics)", ch, s_n_chrs);
            return ESP_ERR_NOT_FOUND;
        }
        s_rx_cccd[ch] = find_cccd(s_rx_val[ch]);
        if (!s_rx_cccd[ch]) {
            fail("no notification descriptor on RX channel %d", ch);
            return ESP_ERR_NOT_FOUND;
        }
    }
    if (!s_unlock_val || !(s_unlock_cccd = find_cccd(s_unlock_val))) {
        fail("unlock characteristic missing");
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "service 0x%04x-0x%04x, %d characteristics", s_svc_start, s_svc_end, s_n_chrs);
    return ESP_OK;
}

// ---- GATT writes ---------------------------------------------------------------------------
static int write_cb(uint16_t c, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg)
{
    s_op_status = err->status;
    xEventGroupSetBits(s_ev, EV_OP);
    return 0;
}

static esp_err_t secure(void);

static esp_err_t gatt_write_once(uint16_t handle, const void *data, int len, bool *auth_err)
{
    *auth_err = false;
    xEventGroupClearBits(s_ev, EV_OP);
    int rc = ble_gattc_write_flat(s_conn, handle, data, len, write_cb, NULL);
    if (rc != 0) {
        fail("write to 0x%04x failed to start (rc=%d)", handle, rc);
        return ESP_FAIL;
    }
    if (!wait_op(5000)) return ESP_FAIL;
    if (s_op_status != 0) {
        int att = s_op_status - BLE_HS_ERR_ATT_BASE;
        *auth_err = att == BLE_ATT_ERR_INSUFFICIENT_AUTHEN || att == BLE_ATT_ERR_INSUFFICIENT_ENC;
        fail("write to 0x%04x refused (status=%d%s)", handle, s_op_status,
             (att == BLE_ATT_ERR_INSUFFICIENT_AUTHEN || att == BLE_ATT_ERR_INSUFFICIENT_ENC)
                 ? ": not paired/encrypted" : "");
        return ESP_FAIL;
    }
    return ESP_OK;
}

// Like BlueZ: a write refused for lack of encryption -> encrypt (pairs if no bond) and retry once.
static esp_err_t gatt_write(uint16_t handle, const void *data, int len)
{
    bool auth_err;
    esp_err_t err = gatt_write_once(handle, data, len, &auth_err);
    if (err != ESP_OK && auth_err && connected()) {
        ESP_LOGI(TAG, "write needs encryption: securing the link");
        if (secure() == ESP_OK) err = gatt_write_once(handle, data, len, &auth_err);
    }
    return err;
}

static esp_err_t subscribe(uint16_t cccd, uint8_t props, bool on)
{
    // bleak's start_notify: notifications if the characteristic has them, else indications
    uint8_t v[2] = {0, 0};
    if (on) v[0] = (props & BLE_GATT_CHR_PROP_NOTIFY) ? 1 : 2;
    return gatt_write(cccd, v, 2);
}

// ---- packets (bluetoothTxRxHandler._waitForRxOrRetry) ----------------------------------------
typedef struct {
    uint8_t type[2], addr[2];
    uint8_t data[64];
    int n;
} om_resp_t;

static esp_err_t command(const uint8_t *cmd, int len, om_resp_t *r)
{
    for (int attempt = 0; attempt < 5; attempt++) {
        s_have = 0;
        xEventGroupClearBits(s_ev, EV_RX);
        for (int ch = 0, off = 0; off < len; ch++, off += 16) {
            int k = len - off < 16 ? len - off : 16;
            if (ch > 3 || gatt_write(s_tx_val[ch], cmd + off, k) != ESP_OK) return ESP_FAIL;
        }
        if (!wait_bits(EV_RX, 1000)) {
            if (!connected()) {
                fail("monitor disconnected");
                return ESP_FAIL;
            }
            ESP_LOGW(TAG, "no answer, retry %d/5", attempt + 1);
            continue;
        }
        if (!connected()) {
            fail("monitor disconnected");
            return ESP_FAIL;
        }
        if (s_rx_err) {
            fail("corrupt packet (checksum)");
            return ESP_FAIL;
        }
        const uint8_t *p = s_pkt;
        memcpy(r->type, p + 1, 2);
        memcpy(r->addr, p + 3, 2);
        int expected = p[5];
        if (r->type[0] == 0x8f && r->type[1] == 0x00) {  // end of transmission: error code at [6]
            r->data[0] = s_pkt_len > 6 ? p[6] : 0xff;
            r->n = 1;
        } else if (expected > s_pkt_len - 8 || expected > (int)sizeof r->data) {
            memset(r->data, 0xff, sizeof r->data);
            r->n = expected < (int)sizeof r->data ? expected : (int)sizeof r->data;
        } else {
            memcpy(r->data, p + 6, expected);
            r->n = expected;
        }
        return ESP_OK;
    }
    fail("same command failed 5 times");
    return ESP_FAIL;
}

static bool type_is(const om_resp_t *r, uint8_t a, uint8_t b) { return r->type[0] == a && r->type[1] == b; }

static esp_err_t rx_notify_all(bool on)
{
    for (int ch = 0; ch < 4; ch++)
        if (subscribe(s_rx_cccd[ch], s_rx_props[ch], on) != ESP_OK) return ESP_FAIL;
    return ESP_OK;
}

static esp_err_t start_transmission(void)
{
    static const uint8_t cmd[] = {0x08, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x18};
    om_resp_t r;
    if (rx_notify_all(true) != ESP_OK || command(cmd, sizeof cmd, &r) != ESP_OK) return ESP_FAIL;
    if (!type_is(&r, 0x80, 0x00)) {
        fail("bad answer to start of readout (%02x%02x)", r.type[0], r.type[1]);
        return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t end_transmission(void)
{
    static const uint8_t cmd[] = {0x08, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x07};
    om_resp_t r;
    if (command(cmd, sizeof cmd, &r) != ESP_OK) return ESP_FAIL;
    if (!type_is(&r, 0x8f, 0x00)) {
        fail("bad answer to end of readout (%02x%02x)", r.type[0], r.type[1]);
        return ESP_FAIL;
    }
    if (r.data[0]) {
        fail("monitor reported error %d at end of readout", r.data[0]);
        return ESP_FAIL;
    }
    rx_notify_all(false);
    return ESP_OK;
}

static esp_err_t read_block(uint16_t addr, uint8_t size, uint8_t *out)
{
    uint8_t cmd[8] = {0x08, 0x01, 0x00, addr >> 8, addr & 0xff, size, 0x00, 0};
    for (int i = 0; i < 7; i++) cmd[7] ^= cmd[i];
    om_resp_t r;
    if (command(cmd, sizeof cmd, &r) != ESP_OK) return ESP_FAIL;
    if (r.addr[0] != cmd[3] || r.addr[1] != cmd[4]) {
        fail("EEPROM read: answer for 0x%02x%02x, asked 0x%04x", r.addr[0], r.addr[1], addr);
        return ESP_FAIL;
    }
    if (!type_is(&r, 0x81, 0x00) || r.n < size) {
        fail("EEPROM read: bad answer (%02x%02x, %d bytes)", r.type[0], r.type[1], r.n);
        return ESP_FAIL;
    }
    memcpy(out, r.data, size);
    return ESP_OK;
}

// ---- unlock key (writeNewUnlockKey / unlockWithUnlockKey) -------------------------------------
static esp_err_t unlock_cmd(uint8_t op, const uint8_t key[16], uint8_t want0, int timeout_ms, uint8_t got[2])
{
    uint8_t msg[17] = {op};
    if (key) memcpy(msg + 1, key, 16);
    s_unlock_len = 0;
    xEventGroupClearBits(s_ev, EV_UNLOCK);
    if (gatt_write(s_unlock_val, msg, sizeof msg) != ESP_OK) return ESP_FAIL;
    if (!wait_bits(EV_UNLOCK, timeout_ms) || !connected() || s_unlock_len < 2) {
        fail("no answer on the unlock channel");
        return ESP_ERR_TIMEOUT;
    }
    got[0] = s_unlock[0];
    got[1] = s_unlock[1];
    return (got[0] == want0 && got[1] == 0x00) ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
}

static esp_err_t unlock(const uint8_t key[16])
{
    uint8_t got[2];
    if (subscribe(s_unlock_cccd, s_unlock_props, true) != ESP_OK) return ESP_FAIL;
    esp_err_t err = unlock_cmd(0x01, key, 0x81, 5000, got);
    if (err == ESP_ERR_INVALID_RESPONSE)
        fail("unlock key rejected (%02x%02x): the monitor was paired with a different key", got[0], got[1]);
    if (err != ESP_OK) return err;
    subscribe(s_unlock_cccd, s_unlock_props, false);
    return ESP_OK;
}

// ---- public API ----------------------------------------------------------------------------
// force_secure: pair/encrypt now. Without it (read with no bond stored) the link is encrypted only
// if the monitor asks for it, which NimBLE answers on its own - the same as BlueZ under omblepy.
static esp_err_t open_link(const uint8_t mac[6], int scan_ms, bool force_secure)
{
    disconnect();
    esp_err_t err = scan_and_connect(mac, scan_ms);
    if (err == ESP_OK && force_secure) err = secure();
    if (err == ESP_OK) err = discover();
    return err;
}

esp_err_t om_pair(const uint8_t mac[6], const uint8_t key[16])
{
    s_err[0] = 0;
    esp_err_t err = open_link(mac, 30000, true);
    uint8_t got[2] = {0};
    if (err == ESP_OK) err = subscribe(s_rx_cccd[0], s_rx_props[0], true);
    if (err == ESP_OK) err = subscribe(s_unlock_cccd, s_unlock_props, true);
    if (err == ESP_OK) {
        static const uint8_t zeros[16] = {0};
        err = ESP_FAIL;
        for (int i = 0; i < 10 && connected(); i++) {  // enter key programming mode
            if (unlock_cmd(0x02, zeros, 0x82, 5000, got) == ESP_OK) {
                err = ESP_OK;
                break;
            }
            ESP_LOGI(TAG, "key programming mode attempt %d/10: %02x%02x", i + 1, got[0], got[1]);
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
        if (err != ESP_OK)
            fail("could not enter key programming mode (last answer %02x%02x). Is the monitor in pairing mode?", got[0], got[1]);
    }
    if (err == ESP_OK) {
        err = unlock_cmd(0x00, key, 0x80, 5000, got);
        if (err != ESP_OK) fail("programming the key failed (%02x%02x)", got[0], got[1]);
    }
    if (err == ESP_OK) {
        subscribe(s_unlock_cccd, s_unlock_props, false);
        subscribe(s_rx_cccd[0], s_rx_props[0], false);
        // omblepy: "seems to be necessary when the device has not been paired to any device"
        err = start_transmission();
        if (err == ESP_OK) err = end_transmission();
    }
    disconnect();
    return err;
}

esp_err_t om_read(const uint8_t mac[6], const uint8_t key[16], int scan_ms, om_record_t *out, int max, int *n_out)
{
    s_err[0] = 0;
    *n_out = 0;
    int64_t t0 = esp_log_timestamp();
    esp_err_t err = open_link(mac, scan_ms, om_bond_count() > 0);
    if (err == ESP_OK) err = unlock(key);
    if (err == ESP_OK) err = start_transmission();
    if (err == ESP_OK) {
        uint8_t rec[RECORD_SIZE];
        for (int i = 0; i < OM_MAX_RECORDS && err == ESP_OK; i++) {
            err = read_block(USER_START_ADDR + i * RECORD_SIZE, BLOCK_SIZE, rec);
            if (err == ESP_OK && *n_out < max && om_parse_record(rec, &out[*n_out])) (*n_out)++;
        }
    }
    if (err == ESP_OK) err = end_transmission();
    disconnect();
    if (err == ESP_OK) ESP_LOGI(TAG, "read %d records in %lu ms", *n_out, (unsigned long)(esp_log_timestamp() - t0));
    return err;
}
