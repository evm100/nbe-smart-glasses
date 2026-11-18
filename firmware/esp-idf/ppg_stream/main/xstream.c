// Auxiliary streams, see xstream.h.
//
// Tasks
//   xs_task (core 0, below the command and camera tasks): every 10 ms, reads the polled streams
//            that are due and sends their M lines; retries init() of sensors that weren't found.
//   xs_push() / xs_push_at(): run in the caller's task (camera task for the frame statistics).
// The H lines and the S-line fields are produced in the sensor task (app_main.c calls
// xs_headers() / xs_status()). Everything leaves through link_write(), which is thread-safe.
#include "sdkconfig.h"

#if CONFIG_PPG_AUX
#include "xstream.h"

#include <stdatomic.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "link.h"
#include "version.h"

static const char *TAG = "aux";

#define XS_TICK_MS 10             // polling resolution: a sample is taken <= 10 ms after it is due
#define XS_INIT_RETRY_US 1000000  // a sensor that wasn't found is tried again once per second
#define XS_M_LINE 192             // "$M,255,4294967295,<t_us>" + 8 x ",-2147483.648" + "*CS\r\n" < 160

// ---------------------------------------------------------------- the streams of this build
// One line per stream. The descriptor lives in the stream's own file (extern in xstream.h).
static void register_streams(void)
{
#if CONFIG_PPG_AUX_DEMO
    xs_register(&xs_demo_stream);
#endif
#if CONFIG_PPG_CAMERA_EDGE_FEATURES
    xs_register(&camera_feat_stream);
#endif
    // xs_register(&my_sensor_stream);   <- your sensor (xstream_template.c)
}

// ---------------------------------------------------------------- state
typedef struct {
    const xs_stream_t *d;
    volatile bool ok;           // init() succeeded
    atomic_bool restart;        // START: the next xs_push() is seq 0
    uint32_t seq;               // pushing task only
    volatile uint32_t n, err;   // since START: M lines made, failed reads
    int64_t period_us, next_due_us;  // aux task only
} xs_state_t;

static xs_state_t s_st[XS_MAX_STREAMS];
static int s_count;
static atomic_bool s_run;
static atomic_bool s_started;  // START seen: the aux task schedules the first sample now

static xs_state_t *find(uint8_t sid)
{
    for (int i = 0; i < s_count; i++) {
        if (s_st[i].d->sid == sid) return &s_st[i];
    }
    return NULL;
}

static bool name_ok(const char *s)
{
    if (!s || !*s) return false;
    for (; *s; s++) {
        if (!((*s >= 'a' && *s <= 'z') || (*s >= '0' && *s <= '9') || *s == '_')) return false;
    }
    return true;
}

bool xs_register(const xs_stream_t *d)
{
    if (s_count >= XS_MAX_STREAMS || !d || d->sid < 2 || d->nch < 1 || d->nch > XS_MAX_CH || !name_ok(d->stream) ||
        find(d->sid) || (d->read && !d->fs_mhz)) {
        ESP_LOGE(TAG, "stream %s (sid %d) not registered: table full, bad descriptor or sid in use",
                 d && d->stream ? d->stream : "?", d ? (int)d->sid : -1);
        return false;
    }
    xs_state_t *st = &s_st[s_count];
    memset(st, 0, sizeof *st);
    st->d = d;
    st->period_us = d->fs_mhz ? (int64_t)1000000000LL / d->fs_mhz : 0;
    s_count++;  // last: xs_push() from another task only ever sees complete entries
    return true;
}

// ---------------------------------------------------------------- output
static void send_line(pline_t *l)
{
    size_t n = pl_end(l);
    link_write(l->start, n);
}

static void put_fs(pline_t *l, uint32_t mhz)
{
    if (mhz % 1000 == 0) {
        pl_u64(l, mhz / 1000);
    } else {
        pl_fixed(l, mhz, 3);  // 200 mHz -> "0.200"
    }
}

static void header(const xs_state_t *st)
{
    const xs_stream_t *d = st->d;
    char buf[PROTO_MAX_LINE];
    pline_t l;
    pl_begin(&l, buf, sizeof buf);
    pl_str(&l, "H,proto=2,dev=" CONFIG_PPG_DEVICE_ID ",fw=" PPG_FW_VERSION ",sid=");
    pl_u64(&l, d->sid);
    pl_str(&l, ",stream=");
    pl_str(&l, d->stream);
    if (d->kind == XS_KIND_FEATURES) {
        pl_str(&l, ",kind=features,src=");
        pl_str(&l, d->src ? d->src : "-");
        pl_str(&l, ",algo=");
        pl_str(&l, d->algo ? d->algo : "-");
    } else {
        pl_str(&l, ",kind=samples");
    }
    pl_str(&l, ",sensor=");
    pl_str(&l, d->sensor ? d->sensor : "-");
    pl_str(&l, ",fs=");
    put_fs(&l, d->fs_now_mhz ? d->fs_now_mhz() : d->fs_mhz);
    pl_str(&l, ",ch=");
    for (int c = 0; c < d->nch; c++) {
        if (c) pl_char(&l, '|');
        pl_str(&l, d->ch[c] ? d->ch[c] : "-");
    }
    pl_str(&l, ",unit=");
    bool scaled = false;
    for (int c = 0; c < d->nch; c++) {
        if (c) pl_char(&l, '|');
        pl_str(&l, d->unit[c] ? d->unit[c] : "-");
        if (d->scale[c]) scaled = true;
    }
    if (scaled) {
        pl_str(&l, ",scale=");
        for (int c = 0; c < d->nch; c++) {
            if (c) pl_char(&l, '|');
            pl_str(&l, d->scale[c] ? d->scale[c] : "1");
        }
    }
    if (d->extra) pl_str(&l, d->extra);
    pl_str(&l, ",site=");
    pl_str(&l, d->site ? d->site : CONFIG_PPG_SITE);
    pl_str(&l, st->ok ? ",sensor_ok=1" : ",sensor_ok=0");
    send_line(&l);
}

void xs_headers(void)
{
    for (int i = 0; i < s_count; i++) header(&s_st[i]);
}

static void emit(xs_state_t *st, uint32_t seq, int64_t t_us, const int32_t *vals, const bool *valid)
{
    const xs_stream_t *d = st->d;
    char buf[XS_M_LINE];
    pline_t l;
    pl_begin(&l, buf, sizeof buf);
    pl_str(&l, "M,");
    pl_u64(&l, d->sid);
    pl_char(&l, ',');
    pl_u64(&l, seq);
    pl_char(&l, ',');
    pl_i64(&l, t_us);
    for (int c = 0; c < d->nch; c++) {
        pl_char(&l, ',');
        if (!valid || valid[c]) pl_fixed(&l, vals[c], d->decimals[c]);  // no value: the field stays empty
    }
    send_line(&l);
    st->n++;
}

bool xs_push_at(uint8_t sid, uint32_t seq, int64_t t_us, const int32_t *vals, const bool *valid)
{
    xs_state_t *st = find(sid);
    if (!st || !atomic_load(&s_run)) return false;
    atomic_store(&st->restart, false);  // the caller owns the numbering
    st->seq = seq + 1;
    emit(st, seq, t_us, vals, valid);
    return true;
}

bool xs_push(uint8_t sid, int64_t t_us, const int32_t *vals, const bool *valid)
{
    xs_state_t *st = find(sid);
    if (!st || !atomic_load(&s_run)) return false;
    if (atomic_exchange(&st->restart, false)) st->seq = 0;
    emit(st, st->seq++, t_us, vals, valid);
    return true;
}

// ---------------------------------------------------------------- start / stop / status
void xs_start(void)
{
    for (int i = 0; i < s_count; i++) {
        s_st[i].n = s_st[i].err = 0;
        atomic_store(&s_st[i].restart, true);
    }
    atomic_store(&s_started, true);
    atomic_store(&s_run, true);
}

void xs_stop(void)
{
    atomic_store(&s_run, false);
}

void xs_status(pline_t *l)
{
    for (int i = 0; i < s_count; i++) {
        pl_char(l, ',');
        pl_str(l, s_st[i].d->stream);
        pl_str(l, "_n=");
        pl_u64(l, s_st[i].n);
        if (s_st[i].d->read) {
            pl_char(l, ',');
            pl_str(l, s_st[i].d->stream);
            pl_str(l, "_err=");
            pl_u64(l, s_st[i].err);
        }
    }
}

bool xs_any_ok(void)
{
    for (int i = 0; i < s_count; i++) {
        if (s_st[i].ok) return true;
    }
    return false;
}

// ---------------------------------------------------------------- polling
// One pass of the aux task: late sensors, then every polled stream whose sample is due.
static void xs_poll(void)
{
    static int64_t last_retry_us;
    int64_t now = esp_timer_get_time();
    if (now - last_retry_us >= XS_INIT_RETRY_US) {
        last_retry_us = now;
        for (int i = 0; i < s_count; i++) {
            xs_state_t *st = &s_st[i];
            if (!st->ok && st->d->init && st->d->init()) {
                st->ok = true;
                ESP_LOGI(TAG, "%s: sensor found", st->d->stream);
                header(st);  // the recording learns that it is there now
            }
        }
    }
    if (!atomic_load(&s_run)) return;
    bool first = atomic_exchange(&s_started, false);
    for (int i = 0; i < s_count; i++) {
        xs_state_t *st = &s_st[i];
        if (!st->d->read) continue;
        if (first) st->next_due_us = now;  // START: a sample right away, then one per period
        if (!st->ok || now < st->next_due_us) continue;
        st->next_due_us += st->period_us;
        if (st->next_due_us <= now) st->next_due_us = now + st->period_us;  // fell behind: don't catch up in a burst
        int32_t vals[XS_MAX_CH] = {0};
        bool valid[XS_MAX_CH];
        for (int c = 0; c < XS_MAX_CH; c++) valid[c] = true;
        int64_t t_us = esp_timer_get_time();
        if (st->d->read(t_us, vals, valid)) {
            xs_push(st->d->sid, t_us, vals, valid);
        } else {
            st->err++;
        }
    }
}

static void xs_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(XS_TICK_MS));
        xs_poll();
    }
}

#ifdef TEST_AUX
void xs_test_tick(void)  // host_test/sim.c: no tasks there, it calls this as its clock advances
{
    xs_poll();
}
#endif

void xs_init(void)
{
    register_streams();
    for (int i = 0; i < s_count; i++) {
        xs_state_t *st = &s_st[i];
        st->ok = st->d->init ? st->d->init() : true;
        if (!st->ok) ESP_LOGW(TAG, "%s: sensor not found - will keep retrying", st->d->stream);
    }
    xTaskCreatePinnedToCore(xs_task, "ppg_aux", 4096, NULL, 4, NULL, 0);
}
#endif
