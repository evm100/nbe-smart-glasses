// Host-test stand-in for espressif/esp32-camera: a fake OV2640 (sim.c) whose pixels encode the
// frame number, so the PC side can check every byte of every frame.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <sys/time.h>
#include "esp_err.h"
typedef enum { PIXFORMAT_GRAYSCALE, PIXFORMAT_JPEG } pixformat_t;
typedef enum { FRAMESIZE_96X96, FRAMESIZE_QQVGA, FRAMESIZE_240X240, FRAMESIZE_QVGA, FRAMESIZE_VGA } framesize_t;
typedef enum { CAMERA_FB_IN_PSRAM } camera_fb_location_t;
typedef enum { CAMERA_GRAB_LATEST } camera_grab_mode_t;
typedef enum { LEDC_TIMER_0 } ledc_timer_t;
typedef enum { LEDC_CHANNEL_0 } ledc_channel_t;
typedef struct {
    int pin_pwdn, pin_reset, pin_xclk, pin_sccb_sda, pin_sccb_scl;
    int pin_d7, pin_d6, pin_d5, pin_d4, pin_d3, pin_d2, pin_d1, pin_d0, pin_vsync, pin_href, pin_pclk;
    int xclk_freq_hz;
    ledc_timer_t ledc_timer;
    ledc_channel_t ledc_channel;
    pixformat_t pixel_format;
    framesize_t frame_size;
    int jpeg_quality;
    size_t fb_count;
    camera_fb_location_t fb_location;
    camera_grab_mode_t grab_mode;
} camera_config_t;
typedef struct {
    uint8_t *buf;
    size_t len, width, height;
    pixformat_t format;
    struct timeval timestamp;
} camera_fb_t;
typedef struct sensor {
    int (*set_vflip)(struct sensor *, int);
    int (*set_hmirror)(struct sensor *, int);
    int (*set_gain_ctrl)(struct sensor *, int);
    int (*set_agc_gain)(struct sensor *, int);
    int (*set_exposure_ctrl)(struct sensor *, int);
    int (*set_aec2)(struct sensor *, int);
    int (*set_aec_value)(struct sensor *, int);
} sensor_t;
esp_err_t esp_camera_init(const camera_config_t *config);
camera_fb_t *esp_camera_fb_get(void);
void esp_camera_fb_return(camera_fb_t *fb);
sensor_t *esp_camera_sensor_get(void);
