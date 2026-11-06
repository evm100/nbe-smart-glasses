// OV2640 camera stream (XIAO ESP32-S3 Sense), protocol v2 stream sid=1 "cam" (see PROTOCOL.md).
//
// Streams between START and STOP like the PPG: every frame goes to link_write_frame() with its
// own seq (from 0 at START) and the device time of its capture (esp_timer, the clock of the PPG's
// t_us, so frames and pulses line up without any PC clock).
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "proto.h"
#include "sdkconfig.h"

#define CAMERA_SID 1

#if CONFIG_PPG_CAMERA
// esp_camera_init() with the menuconfig settings. Called once at boot; not retried (a failed init
// leaves the SCCB I2C port half set up, see legacy notes in camera.c).
bool camera_init(void);
bool camera_ok(void);
// Header line of the camera stream ("H,proto=2,...,sid=1,stream=cam,kind=video,..."), without
// "$" / checksum handling: appended to an open line.
void camera_header(pline_t *l);
void camera_start(void);  // seq = 0, frames flow
void camera_stop(void);
// ",cam_fps=..,cam_sent=..,cam_drop=.." for the S line; resets the fps window.
void camera_status(pline_t *l, uint32_t window_ms);
// "CAM FPS=10 EXP=300 GAIN=2 AEC=0" (any subset): change the camera at run time. true = changed
// (the caller sends the camera header again so the recording has the new settings).
bool camera_command(const char *args);
#endif
