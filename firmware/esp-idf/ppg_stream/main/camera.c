// OV2640 camera stream, see camera.h.
//
// Settings carried over from the MATLAB-era firmware (legacy-camera-code/NOT_Simplified_eye_V7_code.ino),
// where they were measured on these glasses:
//   - grayscale, two frame buffers in PSRAM, grab-latest (one buffer halves the frame rate)
//   - fixed exposure + gain (auto exposure makes the brightness hunt, which the eye tracker reads
//     as motion); vflip + hmirror for the way the module sits in the frame
//   - the camera is initialised once and never retried: esp_camera_init() sets up and tears down
//     an I2C master bus for SCCB, and doing that repeatedly next to working I2C sensors broke them.
//     SCCB gets its own I2C port (CONFIG_SCCB_HARDWARE_I2C_PORT1, sdkconfig.camera); the MAX30102
//     keeps port 0.
#include "sdkconfig.h"

#if CONFIG_PPG_CAMERA
#include "camera.h"

#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_camera.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "frame.h"
#include "link.h"
#include "version.h"
#if CONFIG_PPG_CAMERA_EDGE_FEATURES
#include "xstream.h"
#endif

static const char *TAG = "camera";

// XIAO ESP32-S3 Sense camera connector
#define CAM_PIN_XCLK 10
#define CAM_PIN_SIOD 40
#define CAM_PIN_SIOC 39
#define CAM_PIN_D7 48
#define CAM_PIN_D6 11
#define CAM_PIN_D5 12
#define CAM_PIN_D4 14
#define CAM_PIN_D3 16
#define CAM_PIN_D2 18
#define CAM_PIN_D1 17
#define CAM_PIN_D0 15
#define CAM_PIN_VSYNC 38
#define CAM_PIN_HREF 47
#define CAM_PIN_PCLK 13

#if CONFIG_PPG_CAM_SIZE_96
#define CAM_FRAMESIZE FRAMESIZE_96X96
#define CAM_W 96
#define CAM_H 96
#elif CONFIG_PPG_CAM_SIZE_128
#define CAM_FRAMESIZE FRAMESIZE_128X128
#define CAM_W 128
#define CAM_H 128
#elif CONFIG_PPG_CAM_SIZE_QQVGA
#define CAM_FRAMESIZE FRAMESIZE_QQVGA
#define CAM_W 160
#define CAM_H 120
#elif CONFIG_PPG_CAM_SIZE_QVGA
#define CAM_FRAMESIZE FRAMESIZE_QVGA
#define CAM_W 320
#define CAM_H 240
#elif CONFIG_PPG_CAM_SIZE_VGA
#define CAM_FRAMESIZE FRAMESIZE_VGA
#define CAM_W 640
#define CAM_H 480
#else
#define CAM_FRAMESIZE FRAMESIZE_240X240
#define CAM_W 240
#define CAM_H 240
#endif

// Sensor zoom (CONFIG_PPG_CAM_WINDOW > 0, gray8 only): the stock 240x240 framing reads the sensor in
// CIF mode (1/4 of its 1600x1200 pixels in each direction) and scales a 1200x1200 square of it down
// to 240x240, 5 sensor pixels per frame pixel. Zoomed, the sensor runs in SVGA mode (every 2nd sensor
// pixel, 2x finer than CIF) and only a WINDOW x WINDOW square of SVGA pixels from the middle is
// scaled to the frame, so the frame size and the driver's buffers stay the same but each frame pixel
// covers less of the scene. On paper WINDOW 250 = the view of the stock framing's 100x100 middle at
// 240x240 (2.4x the linear resolution); on GLS03 the view came out wider than that, so check it on
// the image and tune with "CAM WIN=n".
// The driver drops frames whose size isn't the frame size, so the window must be >= the frame size
// (the DSP scales down only). SVGA mode has twice the lines of CIF, so WIN_CLKDIV 1 (twice the sensor
// clock the driver uses for CIF, divider 3) keeps the sensor at ~25 fps; 3 would halve it.
//
// CIF windowing (CONFIG_PPG_CAM_WIN_CIF, "CAM MODE=CIF"): the same, but the sensor stays in CIF mode
// (400x296 px, 4 sensor px each) and WIN counts CIF pixels. Frame timing (OV2640 datasheet v1.6,
// fig. 14-17): Tline = 1190 Tclk (SVGA) / 595 Tclk (CIF), Tframe = 672 / 336 lines. Measured on
// GLS03: stock CIF at sysclk 10 MHz and SVGA at sysclk 20 MHz both gave ~25 fps, i.e. CIF counts
// Tclk at sysclk/2 and a line takes 1190/sysclk in both modes. CIF has half the lines, so at the same
// sensor clock it runs twice as fast: CLKDIV 1 (sysclk = 2 x 20 MHz / 2 = 20 MHz, inside the
// datasheet's 24 MHz for SVGA/CIF) -> 50 fps nominal, line 59.5 us; CLKDIV 0 (40 MHz, above the
// datasheet) -> 100 fps nominal, experimental. WIN=160 CIF px = 640 sensor px across, the same view as
// the earlier SVGA WIN=480 + 160 crop.
// Exposure (AEC) counts lines, and "the maximum exposure time is 1 frame period even if TEX is longer"
// (datasheet, AEC register): at CIF/CLKDIV 1 that is ~336 lines (20 ms); larger values buy nothing.
#ifndef CONFIG_PPG_CAM_WINDOW
#define CONFIG_PPG_CAM_WINDOW 0
#endif
#ifndef CONFIG_PPG_CAM_WIN_CLKDIV
#define CONFIG_PPG_CAM_WIN_CLKDIV 1
#endif
#if CONFIG_PPG_CAM_WINDOW && CONFIG_PPG_CAM_JPEG
#error "PPG_CAM_WINDOW is for gray8 frames"
#endif
#define SVGA_W 800
#define SVGA_H 600
#define CIF_W 400
#define CIF_H 296               // the driver's CIF height (the DSP window must stay inside it)
#define OV2640_MODE_SVGA 1      // ov2640_sensor_mode_t in the driver (private header): UXGA, SVGA, CIF
#define OV2640_MODE_CIF 2
#define OV2640_CLKRC 0x111      // set_reg(): bit 8 = sensor bank, 0x11 = CLKRC
#define OV2640_CLKRC_2X 0x80    // what the driver sets on the S3 for gray8 (clock doubler on)
#define OV2640_DRIVER_CIF_DIV 3 // what the driver sets for CIF framings (stock, WIN=0)
#define OV2640_DRIVER_SVGA_DIV 7
#define CAM_XCLK_HZ 20000000
#define OV2640_TLINE_CLK 1190   // sysclk periods per line (SVGA; CIF the same, see above)
#if CONFIG_PPG_CAM_WIN_CIF
#define CAM_WIN_MODE_DEFAULT OV2640_MODE_CIF
#else
#define CAM_WIN_MODE_DEFAULT OV2640_MODE_SVGA
#endif
static volatile uint32_t s_win = CONFIG_PPG_CAM_WINDOW;
static volatile uint32_t s_clkdiv = CONFIG_PPG_CAM_WIN_CLKDIV;
static volatile uint32_t s_mode = CAM_WIN_MODE_DEFAULT;  // sensor mode while windowed (s_win > 0)

// IR illuminator (CONFIG_PPG_CAM_IR_GPIO >= 0): on while the camera streams, off otherwise. Driven
// with LEDC at ~312 kHz (3.2 us period, far below one 59.5 us sensor line, so the rolling shutter
// shows no bands and the MAX30102's ambient cancellation sees it as DC). 100 % = plain DC.
// Run time: "CAM IR=0..100" (percent). See docs/EYE_CAMERA.md for the LED, circuit and eye limits.
#ifndef CONFIG_PPG_CAM_IR_GPIO
#define CONFIG_PPG_CAM_IR_GPIO -1
#endif
#ifndef CONFIG_PPG_CAM_IR_DUTY
#define CONFIG_PPG_CAM_IR_DUTY 100
#endif
#ifndef CONFIG_PPG_CAM_FB_COUNT
#define CONFIG_PPG_CAM_FB_COUNT 3
#endif
static volatile uint32_t s_ir = CONFIG_PPG_CAM_IR_DUTY;

static uint32_t win_max(uint32_t mode)
{
    return mode == OV2640_MODE_CIF ? CIF_H : SVGA_H;
}

// Sensor timing of the current setting (nominal, from the datasheet + the GLS03 calibration above).
static uint32_t cur_mode(void)
{
    if (s_win) return s_mode;
    return CAM_W <= CIF_W && CAM_H <= CIF_H ? OV2640_MODE_CIF : OV2640_MODE_SVGA;
}

static uint32_t cur_div(void)
{
    if (s_win) return s_clkdiv;
    return cur_mode() == OV2640_MODE_CIF ? OV2640_DRIVER_CIF_DIV : OV2640_DRIVER_SVGA_DIV;
}

static uint32_t frame_lines(void)
{
    return cur_mode() == OV2640_MODE_CIF ? 336 : 672;
}

static uint32_t line_ns(void)  // one sensor line = the exposure unit
{
    uint64_t sysclk = 2ULL * CAM_XCLK_HZ / (cur_div() + 1);
    return (uint32_t)(OV2640_TLINE_CLK * 1000000000ULL / sysclk);
}

static uint32_t nominal_mfps(void)  // sensor frame rate in 1/1000 fps
{
    return (uint32_t)(1000000000000ULL / ((uint64_t)line_ns() * frame_lines()));
}

#if CONFIG_PPG_CAM_IR_GPIO >= 0
#include "driver/ledc.h"
#define IR_TIMER LEDC_TIMER_1      // timer 0 / channel 0 make the camera's XCLK
#define IR_CHANNEL LEDC_CHANNEL_1
#define IR_RES LEDC_TIMER_8_BIT
#define IR_FREQ_HZ 312500          // 80 MHz / 256
static bool s_ir_ok;

static void ir_init(void)
{
    ledc_timer_config_t t = {.speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = IR_RES, .timer_num = IR_TIMER,
                             // the XCLK timer runs from APB too (S3: one clock source for all LEDC timers)
                             .freq_hz = IR_FREQ_HZ, .clk_cfg = LEDC_USE_APB_CLK};
    ledc_channel_config_t c = {.gpio_num = CONFIG_PPG_CAM_IR_GPIO, .speed_mode = LEDC_LOW_SPEED_MODE,
                               .channel = IR_CHANNEL, .timer_sel = IR_TIMER, .duty = 0, .hpoint = 0};
    s_ir_ok = ledc_timer_config(&t) == ESP_OK && ledc_channel_config(&c) == ESP_OK;
    if (!s_ir_ok) ESP_LOGE(TAG, "IR LED on GPIO %d: LEDC setup failed", CONFIG_PPG_CAM_IR_GPIO);
}

static void ir_set(bool on)
{
    if (!s_ir_ok) return;
    uint32_t pct = on ? s_ir : 0, max = (1u << IR_RES);
    uint32_t duty = pct >= 100 ? max : pct * max / 100;  // duty = 2^res: always high (DC)
    ledc_set_duty(LEDC_LOW_SPEED_MODE, IR_CHANNEL, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, IR_CHANNEL);
}
#else
static void ir_init(void) {}
static void ir_set(bool on) { (void)on; }
#endif

// Centre crop (gray8 only): the 80x80 middle of the 240x240 feed at the same pixel scale, only the
// part of the field of view the eye is in. 12 kB/frame -> 6.4 kB: far below what USB moves.
#if !CONFIG_PPG_CAM_JPEG && CONFIG_PPG_CAM_CROP > 0
#define CROP CONFIG_PPG_CAM_CROP
#if CROP > CAM_W || CROP > CAM_H
#error "PPG_CAM_CROP is larger than the frame size"
#endif
#define OUT_W CROP
#define OUT_H CROP
static uint8_t s_crop[CROP * CROP];
#else
#define CROP 0
#define OUT_W CAM_W
#define OUT_H CAM_H
#endif

#if CONFIG_PPG_CAM_JPEG
#define CAM_FMT FMT_JPEG
#define CAM_FMT_NAME "jpeg"
#else
#define CAM_FMT FMT_GRAY8
#define CAM_FMT_NAME "gray8"
#endif

static bool s_ok;
static atomic_bool s_run;
static atomic_bool s_restart;           // START while running: seq back to 0
static volatile uint32_t s_fps_cap = CONFIG_PPG_CAM_FPS;
static volatile uint32_t s_exposure = CONFIG_PPG_CAM_EXPOSURE;
static volatile uint32_t s_gain = CONFIG_PPG_CAM_GAIN;
#if CONFIG_PPG_CAM_AUTO_EXPOSURE
static volatile bool s_aec = true;
#else
static volatile bool s_aec = false;
#endif
static uint32_t s_seq;                  // camera task only
static volatile uint32_t s_sent, s_window_sent;
// diagnostics (S line): where frames go between the driver and the USB port
static volatile uint32_t s_grab;        // frames the driver handed us
static volatile uint32_t s_null;        // esp_camera_fb_get() gave up (4 s without a frame)
static volatile uint32_t s_capped;      // skipped by the fps cap (not lost)
static volatile uint32_t s_wfail;       // link_write_frame() failed (host not reading)
static volatile uint32_t s_wait_max_us, s_tx_max_us;  // per status window
// Sensor frames that never reached us: the driver (grab-latest) replaced them because the camera task
// was late. They never get a seq, so the PC can't see them as seq gaps; they show as gaps in t_us.
// Counted here from the driver's capture times: an interval > 1.5 x the running frame period.
static volatile uint32_t s_miss;
static uint32_t s_per_us;               // running sensor frame period (camera task only; 0 = re-learn)
static int64_t s_prev_grab_us;
static uint32_t s_gap_run;              // consecutive "gaps": the period itself changed
static atomic_bool s_relearn;
static volatile uint32_t s_win_grab;    // frames grabbed in this status window (sensor rate)

static void note_interval(int64_t t_us)
{
    if (atomic_exchange(&s_relearn, false)) {
        s_per_us = 0;
        s_prev_grab_us = 0;
    }
    if (s_prev_grab_us && t_us > s_prev_grab_us) {
        uint32_t dt = (uint32_t)(t_us - s_prev_grab_us);
        if (!s_per_us) {
            s_per_us = dt;
        } else if (dt * 2 > s_per_us * 3) {  // a gap: whole frames missing
            if (++s_gap_run >= 8) {          // every interval long: the rate changed, not frames lost
                s_per_us = dt;
                s_gap_run = 0;
            } else {
                s_miss += (dt + s_per_us / 2) / s_per_us - 1;
            }
        } else {
            s_gap_run = 0;
            s_per_us = s_per_us - s_per_us / 16 + dt / 16;  // slow average: rides out jitter
        }
    }
    s_prev_grab_us = t_us;
}

static void apply_exposure(void)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) return;
    if (s_aec) {
        s->set_gain_ctrl(s, 1);
        s->set_exposure_ctrl(s, 1);
        s->set_aec2(s, 1);
    } else {
        // disable the automatics BEFORE writing the manual values, or they overwrite them
        s->set_gain_ctrl(s, 0);
        s->set_agc_gain(s, (int)s_gain);
        s->set_exposure_ctrl(s, 0);
        s->set_aec2(s, 0);
        s->set_aec_value(s, (int)s_exposure);
    }
}

// Window the sensor (s_win > 0) or go back to the stock framing (s_win == 0). Rewrites the sensor's
// mode registers, so flip/mirror and exposure are set again afterwards.
static bool apply_window(void)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) return false;
    int err;
    uint32_t win = s_win, mode = s_mode;
    if (win) {
        int fw = mode == OV2640_MODE_CIF ? CIF_W : SVGA_W, fh = mode == OV2640_MODE_CIF ? CIF_H : SVGA_H;
        int ox = (fw - (int)win) / 2, oy = (fh - (int)win) / 2;
        // set_res_raw(mode, -, -, -, offsetX, offsetY, windowX, windowY, outX, outY, -, -) on the OV2640
        err = s->set_res_raw(s, (int)mode, 0, 0, 0, ox, oy, (int)win, (int)win, CAM_W, CAM_H, true, false);
        if (!err) err = s->set_reg(s, OV2640_CLKRC, 0xFF, OV2640_CLKRC_2X | (s_clkdiv & 0x3F));
    } else {
        err = s->set_framesize(s, CAM_FRAMESIZE);
    }
    atomic_store(&s_relearn, true);  // the frame period may have changed
#if CONFIG_PPG_CAM_VFLIP
    s->set_vflip(s, 1);
#endif
#if CONFIG_PPG_CAM_HMIRROR
    s->set_hmirror(s, 1);
#endif
    apply_exposure();
    if (err) {
        ESP_LOGE(TAG, "sensor window %u (clkdiv %u) failed: %d", (unsigned)win, (unsigned)s_clkdiv, err);
    } else if (win) {
        unsigned per = mode == OV2640_MODE_CIF ? 4 : 2;
        ESP_LOGI(TAG, "sensor window %ux%u %s px (%u sensor px) -> %dx%d, clkdiv %u, ~%u fps nominal", (unsigned)win,
                 (unsigned)win, mode == OV2640_MODE_CIF ? "CIF" : "SVGA", (unsigned)win * per, CAM_W, CAM_H,
                 (unsigned)s_clkdiv, (unsigned)(nominal_mfps() / 1000));
    }
    return !err;
}

#if CONFIG_PPG_CAMERA_EDGE_FEATURES
// Edge features (protocol v2.1 stream sid=3 "cam_feat", kind=features, src=cam, algo=framestats@1):
// three numbers per frame, computed here instead of on the server. The first step of moving the
// eye tracking onto the board; the server computes the same from the frames it receives and
// compares (host_test/check_xstream.py does that for the simulated camera).
//
// On the frame as it is sent (after the centre crop), on a grid of every FS_STEP-th pixel in x
// and y (N grid pixels, rows y = 0, STEP, .., columns x = 0, STEP, ..):
//   brightness  mean gray level, 0..255                                    (2 decimals, truncated)
//   contrast    p98 - p2 of the gray levels: lo = smallest level v with count(<= v) >= max(1, N/50),
//               hi = smallest v with count(<= v) >= N - N/50 (integer divisions); hi - lo
//   motion      mean |pixel - same pixel of the previous frame of the stream|  (3 decimals, truncated).
//               This is the motion signal of the MATLAB blink counter. Empty for the first frame
//               after START. "Previous" = the previous frame that got a seq (frames skipped by
//               the fps cap don't count), whether or not the link could send it.
// One M line per frame that got a seq, with the frame's seq and t_us, also when the frame itself
// was dropped by the link: the features survive a link that is too slow for the pixels.
//
// Cost (estimate, not measured on hardware): one pass over N pixels with two byte loads, a
// subtract, a histogram increment and a store, ~15-25 CPU cycles each. 160x160 sent frame: N = 6400,
// ~0.5-0.7 ms at 240 MHz; 240x240: N = 14400, ~1.5 ms (the uncropped frame is read from PSRAM,
// allow 2x). At 25 fps that is 1.5-4 % of core 0. RAM: N bytes for the previous grid + 1 kB.
#if CONFIG_PPG_CAM_JPEG
#error "PPG_CAMERA_EDGE_FEATURES needs gray8 frames"
#endif
#if OUT_W * OUT_H > 240 * 240  // larger frames: a coarser grid keeps the cost and the buffer the same
#define FS_STEP 4
#define FS_STEP_STR "4"
#else
#define FS_STEP 2
#define FS_STEP_STR "2"
#endif
#define FS_W ((OUT_W + FS_STEP - 1) / FS_STEP)
#define FS_H ((OUT_H + FS_STEP - 1) / FS_STEP)
#define FS_N (FS_W * FS_H)
static uint8_t s_fs_prev[FS_N];  // the grid of the previous frame
static bool s_fs_have_prev;      // camera task only
static uint32_t s_fs_hist[256];

static void frame_stats(const uint8_t *px, size_t w, size_t h, size_t len, uint32_t seq, int64_t t_us)
{
    int32_t vals[3] = {0, 0, 0};
    bool valid[3] = {false, false, false};
    if (w == (size_t)OUT_W && h == (size_t)OUT_H && len >= w * h) {  // else: not the frame we sized the grid for - a line without values
        uint32_t sum = 0, diff = 0;
        memset(s_fs_hist, 0, sizeof s_fs_hist);
        uint8_t *prev = s_fs_prev;
        for (size_t y = 0; y < h; y += FS_STEP) {
            const uint8_t *row = px + y * w;
            for (size_t x = 0; x < w; x += FS_STEP) {
                uint8_t v = row[x], o = *prev;
                *prev++ = v;
                sum += v;
                diff += v > o ? (uint32_t)(v - o) : (uint32_t)(o - v);
                s_fs_hist[v]++;
            }
        }
        uint32_t tail = FS_N / 50, lo_at = tail ? tail : 1, hi_at = FS_N - tail, cum = 0;
        int lo = -1, hi = 255;
        for (int v = 0; v < 256; v++) {
            cum += s_fs_hist[v];
            if (lo < 0 && cum >= lo_at) lo = v;
            if (cum >= hi_at) {
                hi = v;
                break;
            }
        }
        vals[0] = (int32_t)((uint64_t)sum * 100 / FS_N);
        vals[1] = hi - lo;
        vals[2] = (int32_t)((uint64_t)diff * 1000 / FS_N);
        valid[0] = valid[1] = true;
        valid[2] = s_fs_have_prev;
        s_fs_have_prev = true;
    } else {
        s_fs_have_prev = false;
    }
    xs_push_at(XS_SID_CAM_FEAT, seq, t_us, vals, valid);
}

static uint32_t feat_fs_mhz(void)
{
    uint32_t cap = s_fps_cap;
    return cap ? cap * 1000 : nominal_mfps();  // no cap: the sensor's own rate (nominal; see cam_fps in the S line)
}

static bool feat_init(void)
{
    return s_ok;  // sensor_ok of the feature stream = the camera's
}

const xs_stream_t camera_feat_stream = {
    .sid = XS_SID_CAM_FEAT,
    .stream = "cam_feat",
    .kind = XS_KIND_FEATURES,
    .src = "cam",
    .algo = "framestats@1",
    .sensor = "OV2640",
    .fs_now_mhz = feat_fs_mhz,
    .nch = 3,
    .ch = {"brightness", "contrast", "motion"},
    .decimals = {2, 0, 3},
    .extra = ",step=" FS_STEP_STR,  // the pixel grid the statistics were taken on
    .init = feat_init,
};
#endif

// One pass: wait for a frame, send it (or skip it for the rate cap).
static void camera_step(void)
{
    static int64_t last_us;
    if (!atomic_load(&s_run)) {
        vTaskDelay(pdMS_TO_TICKS(20));
        return;
    }
    if (atomic_exchange(&s_restart, false)) {
        s_seq = 0;
        atomic_store(&s_relearn, true);
#if CONFIG_PPG_CAMERA_EDGE_FEATURES
        s_fs_have_prev = false;  // the first frame of a recording has no motion value
#endif
    }
    int64_t w0 = esp_timer_get_time();
    camera_fb_t *fb = esp_camera_fb_get();  // blocks until the next frame (grab latest)
    uint32_t waited = (uint32_t)(esp_timer_get_time() - w0);
    if (waited > s_wait_max_us) s_wait_max_us = waited;
    if (!fb) {
        s_null++;
        vTaskDelay(pdMS_TO_TICKS(5));
        return;
    }
    s_grab++;
    s_win_grab++;
    int64_t t_us = (int64_t)fb->timestamp.tv_sec * 1000000 + fb->timestamp.tv_usec;
    note_interval(t_us);
    uint32_t cap = s_fps_cap;
    // the cap skips frames before they get a seq: a skipped frame is not a lost one
    if (cap && t_us - last_us < 1000000 / (int64_t)cap - 2000) {
        s_capped++;
        esp_camera_fb_return(fb);
        return;
    }
    last_us = t_us;
    const uint8_t *px = fb->buf;
    size_t w = fb->width, h = fb->height, len = fb->len;
#if CROP
    if (w < CROP || h < CROP || len < w * h) {  // not the frame size we configured: skip it
        esp_camera_fb_return(fb);
        return;
    }
    const uint8_t *src = fb->buf + ((h - CROP) / 2) * w + (w - CROP) / 2;
    for (int y = 0; y < CROP; y++) memcpy(s_crop + y * CROP, src + y * w, CROP);
    px = s_crop;
    w = h = CROP;
    len = CROP * CROP;
#endif
    if (atomic_load(&s_run)) {
#if CONFIG_PPG_CAMERA_EDGE_FEATURES
        frame_stats(px, w, h, len, s_seq, t_us);  // before the frame: it is sent even if the frame is not
#endif
        int64_t x0 = esp_timer_get_time();
        if (!link_write_frame(CAMERA_SID, s_seq, t_us, (uint16_t)w, (uint16_t)h, CAM_FMT,
                              (uint16_t)(s_aec ? 0xFFFF : s_exposure), (uint8_t)(s_aec ? 0xFF : s_gain), px, len)) {
            s_wfail++;
        }
        uint32_t tx = (uint32_t)(esp_timer_get_time() - x0);
        if (tx > s_tx_max_us) s_tx_max_us = tx;
        s_seq++;  // also when not sent: the PC sees the gap and counts the frame as dropped
        s_sent++;
        s_window_sent++;
    }
    esp_camera_fb_return(fb);
}

static void camera_task(void *arg)
{
    (void)arg;
    for (;;) camera_step();
}

#if defined(TEST_USB_BIN) || defined(TEST_HYBRID)
void camera_test_tick(void)  // host_test/sim.c: no tasks there, it calls this per frame period
{
    camera_step();
}
#endif

bool camera_init(void)
{
    camera_config_t c = {
        .pin_pwdn = -1,
        .pin_reset = -1,
        .pin_xclk = CAM_PIN_XCLK,
        .pin_sccb_sda = CAM_PIN_SIOD,
        .pin_sccb_scl = CAM_PIN_SIOC,
        .pin_d7 = CAM_PIN_D7,
        .pin_d6 = CAM_PIN_D6,
        .pin_d5 = CAM_PIN_D5,
        .pin_d4 = CAM_PIN_D4,
        .pin_d3 = CAM_PIN_D3,
        .pin_d2 = CAM_PIN_D2,
        .pin_d1 = CAM_PIN_D1,
        .pin_d0 = CAM_PIN_D0,
        .pin_vsync = CAM_PIN_VSYNC,
        .pin_href = CAM_PIN_HREF,
        .pin_pclk = CAM_PIN_PCLK,
        .xclk_freq_hz = CAM_XCLK_HZ,
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
#if CONFIG_PPG_CAM_JPEG
        .pixel_format = PIXFORMAT_JPEG,
        .jpeg_quality = CONFIG_PPG_CAM_JPEG_QUALITY,
#else
        .pixel_format = PIXFORMAT_GRAYSCALE,
#endif
        .frame_size = CAM_FRAMESIZE,
        // 3+: the driver queues fb_count - 1 frames (grab-latest drops the oldest only when all are
        // full), so one slow link write no longer costs a sensor frame
        .fb_count = CONFIG_PPG_CAM_FB_COUNT,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,
    };
    esp_err_t err = esp_camera_init(&c);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init failed: %s (ribbon seated? PSRAM enabled?)", esp_err_to_name(err));
        return false;
    }
    sensor_t *s = esp_camera_sensor_get();
    if (!s) return false;
#if CONFIG_PPG_CAM_VFLIP
    s->set_vflip(s, 1);
#endif
#if CONFIG_PPG_CAM_HMIRROR
    s->set_hmirror(s, 1);
#endif
    apply_exposure();
    if (s_win) apply_window();
    ir_init();
    ir_set(false);
    s_ok = true;
    xTaskCreatePinnedToCore(camera_task, "ppg_camera", 4096, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "OV2640 %dx%d %s (sent %dx%d), cap %d fps, sensor ~%u fps nominal, %u fb", CAM_W, CAM_H,
             CAM_FMT_NAME, OUT_W, OUT_H, CONFIG_PPG_CAM_FPS, (unsigned)(nominal_mfps() / 1000),
             (unsigned)CONFIG_PPG_CAM_FB_COUNT);
    return true;
}

bool camera_ok(void)
{
    return s_ok;
}

void camera_header(pline_t *l)
{
    pl_str(l, "H,proto=2,dev=" CONFIG_PPG_DEVICE_ID ",fw=" PPG_FW_VERSION ",sid=1,stream=cam,kind=video");
    pl_str(l, ",sensor=OV2640,ch=frame,fs=");
    pl_u64(l, s_fps_cap);
    pl_str(l, ",w=");
    pl_u64(l, OUT_W);
    pl_str(l, ",h=");
    pl_u64(l, OUT_H);
    pl_str(l, ",fmt=" CAM_FMT_NAME);
    // sensor zoom: the frame is a win x win square of SVGA pixels (2 sensor px each) scaled to the
    // frame size; absent = stock framing
    if (s_win) {
        pl_str(l, ",win=");
        pl_u64(l, s_win);
        pl_str(l, ",clkdiv=");
        pl_u64(l, s_clkdiv);
    }
    // sensor mode and timing (nominal): exposure x line_us = exposure time; exposure above exp_max
    // (one frame) is clamped by the sensor. win_min..win_max = the range "CAM WIN=" accepts.
#if !CONFIG_PPG_CAM_JPEG  // (the driver clocks JPEG differently: no nominal timing for it)
    pl_str(l, cur_mode() == OV2640_MODE_CIF ? ",mode=cif" : ",mode=svga");
    pl_str(l, ",fps_sensor=");
    pl_fixed(l, nominal_mfps() / 100, 1);
    pl_str(l, ",line_us=");
    pl_fixed(l, line_ns() / 100, 1);
    pl_str(l, ",exp_max=");
    pl_u64(l, frame_lines());
    pl_str(l, ",win_min=");
    pl_u64(l, CAM_W > CAM_H ? CAM_W : CAM_H);
    pl_str(l, ",win_max=");
    pl_u64(l, win_max(s_win ? s_mode : CAM_WIN_MODE_DEFAULT));
#endif
#if CONFIG_PPG_CAM_IR_GPIO >= 0
    pl_str(l, ",ir=");
    pl_u64(l, s_ir);
#endif
    if (s_aec) {
        pl_str(l, ",aec=1");
    } else {
        pl_str(l, ",aec=0,exposure=");
        pl_u64(l, s_exposure);
        pl_str(l, ",gain=");
        pl_u64(l, s_gain);
    }
    pl_str(l, ",site=" CONFIG_PPG_SITE);
    pl_str(l, s_ok ? ",sensor_ok=1" : ",sensor_ok=0");
}

void camera_start(void)
{
    atomic_store(&s_restart, true);
    atomic_store(&s_run, s_ok);
    s_window_sent = 0;
    s_win_grab = 0;
    ir_set(s_ok);
}

void camera_stop(void)
{
    atomic_store(&s_run, false);
    ir_set(false);
}

void camera_status(pline_t *l, uint32_t window_ms)
{
    pl_str(l, ",cam_fps=");
    pl_fixed(l, (int64_t)s_window_sent * 10000 / (window_ms ? window_ms : 1), 1);
    pl_str(l, ",cam_sent=");
    pl_u64(l, s_sent);
    pl_str(l, ",cam_drop=");
    pl_u64(l, link_frames_dropped());
    // where frames go: grabbed = capped + sent; null = driver timeouts (no frame for 4 s)
    pl_str(l, ",cam_grab=");
    pl_u64(l, s_grab);
    pl_str(l, ",cam_capped=");
    pl_u64(l, s_capped);
    pl_str(l, ",cam_null=");
    pl_u64(l, s_null);
    pl_str(l, ",cam_wfail=");
    pl_u64(l, s_wfail);
    pl_str(l, ",cam_wait_ms=");  // longest wait for a frame in this window
    pl_u64(l, s_wait_max_us / 1000);
    pl_str(l, ",cam_tx_ms=");    // longest USB write of a frame in this window
    pl_u64(l, s_tx_max_us / 1000);
    // sensor side: frames/s the driver delivered, and sensor frames lost before they got a seq
    pl_str(l, ",cam_sfps=");
    pl_fixed(l, (int64_t)s_win_grab * 10000 / (window_ms ? window_ms : 1), 1);
    pl_str(l, ",cam_miss=");
    pl_u64(l, s_miss);
    s_win_grab = 0;
    s_window_sent = 0;
    s_wait_max_us = s_tx_max_us = 0;
}

bool camera_command(const char *args)
{
    bool changed = false, rewindow = false;
#if !CONFIG_PPG_CAM_JPEG
    uint32_t mode = s_mode;
    long win = -1;  // -1 = not given
#endif
    const char *p = args;
    while (*p) {
        while (*p == ' ') p++;
        const char *eq = strchr(p, '=');
        if (!eq) break;
        long v = strtol(eq + 1, NULL, 10);
        if (strncmp(p, "FPS=", 4) == 0 && v >= 0 && v <= 120) {
            s_fps_cap = (uint32_t)v;
            changed = true;
        } else if (strncmp(p, "EXP=", 4) == 0 && v >= 0 && v <= 1200) {
            s_exposure = (uint32_t)v;
            s_aec = false;
            changed = true;
        } else if (strncmp(p, "GAIN=", 5) == 0 && v >= 0 && v <= 30) {
            s_gain = (uint32_t)v;
            s_aec = false;
            changed = true;
        } else if (strncmp(p, "AEC=", 4) == 0) {
            s_aec = v != 0;
            changed = true;
        } else if (strncmp(p, "IR=", 3) == 0 && v >= 0 && v <= 100) {
            s_ir = (uint32_t)v;
            ir_set(atomic_load(&s_run));
            changed = true;
#if !CONFIG_PPG_CAM_JPEG
        } else if (strncmp(p, "MODE=", 5) == 0) {  // MODE=CIF / MODE=SVGA (sensor mode of the window)
            if (strncasecmp(eq + 1, "CIF", 3) == 0) {
                mode = OV2640_MODE_CIF;
            } else if (strncasecmp(eq + 1, "SVGA", 4) == 0) {
                mode = OV2640_MODE_SVGA;
            }
        } else if (strncmp(p, "WIN=", 4) == 0 && v >= 0) {
            win = v;
        } else if (strncmp(p, "CLK=", 4) == 0 && v >= 0 && v <= 63) {
            s_clkdiv = (uint32_t)v;
            changed = rewindow = true;
#endif
        }
        const char *sp = strchr(p, ' ');
        if (!sp) break;
        p = sp;
    }
#if !CONFIG_PPG_CAM_JPEG
    // the window is checked against the mode it will be used in (MODE= may come after WIN=)
    if (mode != s_mode) {
        if (win < 0 && s_win > win_max(mode)) win = (long)win_max(mode);  // keep a window, shrunk to fit
        s_mode = mode;
        changed = rewindow = true;
    }
    if (win == 0 || (win >= (CAM_W > CAM_H ? CAM_W : CAM_H) && win <= (long)win_max(mode))) {
        s_win = (uint32_t)(win & ~3L);  // the OV2640 window registers count in 4s
        changed = rewindow = true;
    }
#endif
    atomic_store(&s_relearn, true);
    if (rewindow && s_ok) {
        apply_window();  // also sets the exposure again
    } else if (changed && s_ok) {
        apply_exposure();
    }
    return changed;
}
#endif
