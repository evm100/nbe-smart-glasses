// Auxiliary streams (protocol v2.1, see firmware/PROTOCOL.md): further numeric streams next to the
// PPG (sid 0) and the camera (sid 1). One layer for two kinds of stream:
//
//   kind=samples   measurements from a sensor (sweat glucose, cortisol, uric acid, temperature, ...)
//   kind=features  values the board computed itself from another stream (camera frame statistics,
//                  later PPG beat features): the first step of moving processing onto the board
//
// A stream is a descriptor (xs_stream_t: sid, name, channels, units, rate) plus either
//   - a read() callback, polled at the stream's rate by the aux task              (sensors), or
//   - xs_push() / xs_push_at() calls from the task that produces the values     (features).
// Each sample goes out as one text line  $M,<sid>,<seq>,<t_us>,<v1>,<v2>,...*CS  through
// link_write(), so it travels on every link (USB text, USB binary envelopes, MQTT frames) as an
// ordinary text record. The stream describes itself in its own H line; the server needs no
// change to record and show a new one.
//
// Design point: LOW RATE. A text line per sample (~40 bytes) is right for chemistry and features
// at <= ~20 Hz per stream. Polling has a 10 ms tick, so 50 Hz is the hard limit; anything faster
// (an IMU, raw ECG) needs the binary samples record instead (link_write_samples; the second PPG of
// dual glasses does that, app_main.c).
//
// Adding a sensor: copy xstream_template.c, then add one line to register_streams() in xstream.c
// (README.md "Adding a sensor").
//
// (The files are not called aux.c / aux.h: AUX is a reserved device name on Windows, where files with
// that name cannot be created, so the repo could not be checked out there.)
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "proto.h"
#include "sdkconfig.h"

// Stream ids in use. 0 = PPG, 1 = camera (camera.h). Give every new stream its own number here.
// 4 = the second PPG sensor of dual glasses (PPG2_SID in app_main.c, protocol v2.2, kind=ppg).
#define XS_SID_DEMO 2      // "sweat"     synthetic glucose|lactate (xstream_demo.c)
#define XS_SID_CAM_FEAT 3  // "cam_feat"  frame statistics computed on the board (camera.c)
#define XS_SID_TEMPLATE 9  // "chem"      xstream_template.c as shipped: change it in your copy

#define XS_MAX_STREAMS 8
#define XS_MAX_CH 8

typedef enum { XS_KIND_SAMPLES, XS_KIND_FEATURES } xs_kind_t;

// Everything the H line says about a stream. Declare it `const` in the driver file.
typedef struct {
    uint8_t sid;                    // 2..255, unique on the board
    const char *stream;             // short name [a-z0-9_]+: file names, and the prefix of its S-line fields
    xs_kind_t kind;
    const char *sensor;             // part / method name, e.g. "AD5941", "DEMO"
    uint32_t fs_mhz;                // nominal rate in mHz (1000 = 1 Hz, 200 = 0.2 Hz); the polling rate
    uint32_t (*fs_now_mhz)(void);   // optional: the rate when it can change at run time (overrides fs_mhz in the H line)
    uint8_t nch;                    // 1..XS_MAX_CH
    const char *ch[XS_MAX_CH];     // channel names
    const char *unit[XS_MAX_CH];   // one unit per channel; NULL = "-" (unitless)
    uint8_t decimals[XS_MAX_CH];   // values are fixed-point: the int32 1234 with decimals 2 is sent as "12.34"
    const char *scale[XS_MAX_CH];  // optional, as text ("0.001"): sent value x scale = quantity in `unit`.
                                    // All NULL = no scale key; NULL next to others = "1"
    const char *site;               // where it measures; NULL = the board's site (CONFIG_PPG_SITE)
    const char *src;                // kind=features: name of the stream it was computed from ("cam", "ppg")
    const char *algo;               // kind=features: "<name>@<version>"; raise the version when the numbers change
    const char *extra;              // optional further H-line fields, verbatim: ",key=value,key=value"
                                    // (calibration constants, gain, electrode area, ...)
    // Set the sensor up; false = not found. Called at boot and again every second until it succeeds.
    // NULL = nothing to set up (always ok).
    bool (*init)(void);
    // One sample, taken now (t_us = esp_timer time just before the call). Fill vals[0..nch-1]; set
    // valid[c] = false for a channel that has no value this time (sent as an empty field; all are
    // true on entry). false = the read failed: nothing is sent and <stream>_err counts it.
    // Runs in the aux task: may block for a few ms (an I2C transaction), not for a whole period.
    // NULL = push-style stream (xs_push from another task).
    bool (*read)(int64_t t_us, int32_t *vals, bool *valid);
} xs_stream_t;

// Streams defined in other files (registered in xstream.c, register_streams()).
extern const xs_stream_t xs_demo_stream;    // xstream_demo.c
extern const xs_stream_t camera_feat_stream; // camera.c (CONFIG_PPG_CAMERA_EDGE_FEATURES)

#if CONFIG_PPG_AUX
// Registers the streams, runs their init() and starts the aux task. Call once at boot, after the
// link and the camera are up.
void xs_init(void);
// False = table full, a bad descriptor or a duplicate sid (logged). Normally only called by
// register_streams().
bool xs_register(const xs_stream_t *s);
// Sends every registered stream's H line (boot, INFO, START - wherever the PPG's H line goes).
void xs_headers(void);
// START: every stream's seq back to 0, polling begins with a sample right away. STOP: no more M lines.
void xs_start(void);
void xs_stop(void);
// ",<stream>_n=..[,<stream>_err=..]" per stream for the S line: M lines since START, failed reads.
void xs_status(pline_t *l);
// True if any stream's sensor was found (START is worth it even without the PPG sensor).
bool xs_any_ok(void);

// Push one sample of stream `sid` from any task (one task per stream). valid = NULL: all channels
// have a value. The line is built on the caller's stack (~200 bytes) and handed to link_write().
// false = unknown sid or not streaming (nothing sent).
bool xs_push(uint8_t sid, int64_t t_us, const int32_t *vals, const bool *valid);
// Same, with the seq given by the caller: for features computed once per item of the source
// stream, so that the feature line of camera frame k carries seq k (and a line the link lost
// shows as a gap at the right place).
bool xs_push_at(uint8_t sid, uint32_t seq, int64_t t_us, const int32_t *vals, const bool *valid);

// The I2C bus of the MAX30102 (app_main.c), for sensors that share its two wires. NULL until the
// sensor task has created it (a driver's init() then returns false and is called again later).
i2c_master_bus_handle_t app_i2c_bus(void);
#endif
