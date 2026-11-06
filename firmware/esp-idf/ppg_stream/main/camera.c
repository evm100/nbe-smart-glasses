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

#include "esp_camera.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "frame.h"
#include "link.h"
#include "version.h"

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
// covers less of the scene. WINDOW 250 = the field of view that the 100x100 middle of the stock
// framing had, now 240x240 pixels (2.4x the linear resolution).
// The driver drops frames whose size isn't the frame size, so the window must be >= the frame size
// (the DSP scales down only). SVGA mode has twice the lines of CIF, so WIN_CLKDIV 1 (twice the sensor
// clock the driver uses for CIF, divider 3) keeps the sensor at ~25 fps; 3 would halve it.
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
#define OV2640_MODE_SVGA 1      // ov2640_sensor_mode_t in the driver (private header): UXGA, SVGA, CIF
#define OV2640_CLKRC 0x111      // set_reg(): bit 8 = sensor bank, 0x11 = CLKRC
#define OV2640_CLKRC_2X 0x80    // what the driver sets on the S3 for gray8 (clock doubler on)
static volatile uint32_t s_win = CONFIG_PPG_CAM_WINDOW;
static volatile uint32_t s_clkdiv = CONFIG_PPG_CAM_WIN_CLKDIV;

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
    uint32_t win = s_win;
    if (win) {
        int ox = (SVGA_W - (int)win) / 2, oy = (SVGA_H - (int)win) / 2;
        // set_res_raw(mode, -, -, -, offsetX, offsetY, windowX, windowY, outX, outY, -, -) on the OV2640
        err = s->set_res_raw(s, OV2640_MODE_SVGA, 0, 0, 0, ox, oy, (int)win, (int)win, CAM_W, CAM_H, true, false);
        if (!err) err = s->set_reg(s, OV2640_CLKRC, 0xFF, OV2640_CLKRC_2X | (s_clkdiv & 0x3F));
    } else {
        err = s->set_framesize(s, CAM_FRAMESIZE);
    }
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
        ESP_LOGI(TAG, "sensor window %ux%u SVGA px (%u sensor px) -> %dx%d, clkdiv %u", (unsigned)win,
                 (unsigned)win, (unsigned)win * 2, CAM_W, CAM_H, (unsigned)s_clkdiv);
    }
    return !err;
}

// One pass: wait for a frame, send it (or skip it for the rate cap).
static void camera_step(void)
{
    static int64_t last_us;
    if (!atomic_load(&s_run)) {
        vTaskDelay(pdMS_TO_TICKS(20));
        return;
    }
    if (atomic_exchange(&s_restart, false)) s_seq = 0;
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
    int64_t t_us = (int64_t)fb->timestamp.tv_sec * 1000000 + fb->timestamp.tv_usec;
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

#ifdef TEST_USB_BIN
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
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
#if CONFIG_PPG_CAM_JPEG
        .pixel_format = PIXFORMAT_JPEG,
        .jpeg_quality = CONFIG_PPG_CAM_JPEG_QUALITY,
#else
        .pixel_format = PIXFORMAT_GRAYSCALE,
#endif
        .frame_size = CAM_FRAMESIZE,
        .fb_count = 2,
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
    s_ok = true;
    xTaskCreatePinnedToCore(camera_task, "ppg_camera", 4096, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "OV2640 %dx%d %s (sent %dx%d), cap %d fps", CAM_W, CAM_H, CAM_FMT_NAME, OUT_W, OUT_H,
             CONFIG_PPG_CAM_FPS);
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
}

void camera_stop(void)
{
    atomic_store(&s_run, false);
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
    s_window_sent = 0;
    s_wait_max_us = s_tx_max_us = 0;
}

bool camera_command(const char *args)
{
    bool changed = false, rewindow = false;
    const char *p = args;
    while (*p) {
        while (*p == ' ') p++;
        const char *eq = strchr(p, '=');
        if (!eq) break;
        long v = strtol(eq + 1, NULL, 10);
        if (strncmp(p, "FPS=", 4) == 0 && v >= 0 && v <= 60) {
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
#if !CONFIG_PPG_CAM_JPEG
        } else if (strncmp(p, "WIN=", 4) == 0 && (v == 0 || (v >= (CAM_W > CAM_H ? CAM_W : CAM_H) && v <= SVGA_H))) {
            s_win = (uint32_t)(v & ~3L);  // the OV2640 window registers count in 4s
            changed = rewindow = true;
        } else if (strncmp(p, "CLK=", 4) == 0 && v >= 0 && v <= 63) {
            s_clkdiv = (uint32_t)v;
            changed = rewindow = true;
#endif
        }
        const char *sp = strchr(p, ' ');
        if (!sp) break;
        p = sp;
    }
    if (rewindow && s_ok) {
        apply_window();  // also sets the exposure again
    } else if (changed && s_ok) {
        apply_exposure();
    }
    return changed;
}
#endif
