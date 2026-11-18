// Demo auxiliary stream (CONFIG_PPG_AUX_DEMO): a synthetic two-channel "sweat" stream at 1 Hz,
// to exercise the whole path (H line, M lines, recorder, live page) without any hardware.
// NOT a measurement: sensor=DEMO, and the values are slow triangle waves of the device clock:
//
//   glucose  80.0 .. 110.0 uM   up and down once per 120 s   (0.5 uM steps)
//   lactate  1.00 .. 2.50 mM    up and down once per 60 s    (0.05 mM steps)
//            and no value in every 10th second (sec % 10 == 9): shows a channel without a value
//            (an empty field in the M line)
//
// where sec = t_us / 1 000 000. host_test/check_xstream.py recomputes them from t_us.
// A real driver looks the same with a sensor behind init() and read(): see xstream_template.c.
#include "sdkconfig.h"

#if CONFIG_PPG_AUX && CONFIG_PPG_AUX_DEMO
#include "xstream.h"

// 0, 1, .. period/2, .. 1, 0, 1, ..
static int32_t triangle(int64_t sec, int32_t period)
{
    int32_t x = (int32_t)(sec % period);
    return x <= period / 2 ? x : period - x;
}

static bool demo_read(int64_t t_us, int32_t *vals, bool *valid)
{
    int64_t sec = t_us / 1000000;
    vals[0] = 800 + 5 * triangle(sec, 120);  // glucose, uM x 10
    vals[1] = 100 + 5 * triangle(sec, 60);   // lactate, mM x 100
    if (sec % 10 == 9) valid[1] = false;
    return true;
}

const xs_stream_t xs_demo_stream = {
    .sid = XS_SID_DEMO,
    .stream = "sweat",
    .kind = XS_KIND_SAMPLES,
    .sensor = "DEMO",
    .fs_mhz = 1000,
    .nch = 2,
    .ch = {"glucose", "lactate"},
    .unit = {"uM", "mM"},
    .decimals = {1, 2},
    .read = demo_read,
};
#endif
