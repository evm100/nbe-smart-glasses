// TEMPLATE: how to add a sensor. Copy this file, don't edit it. It is NOT part of the build
// (not in main/CMakeLists.txt); host_test/run.sh only compile-checks it so it stays correct.
//
// The example is an electrochemical front end on I2C (a potentiostat / amperometric AFE chip in
// front of a sweat cortisol + uric acid electrode pair) that returns a raw 16-bit ADC code per
// channel. The chip, its address and its registers are MADE UP: replace everything marked "TODO"
// with your part's datasheet. Nothing here has run on hardware.
//
// ---------------------------------------------------------------------------------------------
// CHECKLIST
//
//  1. Copy this file to main/xstream_<name>.c (e.g. xstream_cortisol.c). Rename `template_` / `TEMPLATE`.
//  2. Pick a stream id: add `#define XS_SID_<NAME> <n>` to the list in xstream.h. 2..255,
//     never reuse a number for something else (0 = PPG, 1 = camera, 2 = demo, 3 = cam_feat).
//  3. Pick the stream name (`.stream`): short, [a-z0-9_]+. It becomes the file name on the server
//     (<role>_<dev>_<stream>.csv) and the prefix of the S-line fields (<stream>_n, <stream>_err).
//  4. Channels: `.ch` names and `.unit`, one unit per channel ("-" / NULL = unitless).
//     Decide WHAT you send:
//       a) raw counts (recommended while the calibration is still moving): integers, `.unit` =
//          "counts" or "-", and the calibration constants in `.extra` so every recording carries
//          the numbers needed to convert it later. Or give `.scale` when one multiplier per
//          channel converts the sent value to the unit (sent value x scale = quantity in unit).
//       b) calibrated values: fixed-point, `.decimals` per channel (the int32 1234 with
//          decimals 2 is sent as "12.34"). No floats in the data path.
//  5. Rate: `.fs_mhz` in mHz (1000 = 1 Hz, 200 = one sample per 5 s). Chemistry is slow; this
//     layer is meant for <= ~20 Hz (xstream.h). read() is called at that rate.
//  6. Calibration and settings in the H line: put everything that changes the meaning of the
//     numbers into `.extra` (",gain=..,rtia_ohm=..,bias_mv=..,cal=<id or date>"). The server
//     stores unknown H keys as metadata of the recording.
//  7. Register it: add `extern const xs_stream_t <name>_stream;` to xstream.h and one
//     `xs_register(&<name>_stream);` line to register_streams() in xstream.c, inside
//     `#if CONFIG_PPG_AUX_<NAME>`.
//  8. Kconfig: add `config PPG_AUX_<NAME>` (bool, default n, depends on PPG_AUX) to the menu
//     "Auxiliary sensor streams" in Kconfig.projbuild, plus its settings (I2C address, GPIOs).
//  9. Build: add the file name to `srcs` in main/CMakeLists.txt (next to xstream_demo.c). The whole
//     file body sits in `#if CONFIG_PPG_AUX && CONFIG_PPG_AUX_<NAME>`, so it is empty otherwise.
// 10. S line: you get `<stream>_n` (M lines since START) and `<stream>_err` (failed reads) for
//     free. Healthy: _n grows by fs every second, _err stays 0.
// 11. Host test: add a build with your option to host_test/run.sh if the driver has logic worth
//     testing (see how TEST_AUX builds the demo), with a fake of your chip in sim.c.
// 12. On the server: nothing. It reads the H line, records the M lines to
//     <role>_<dev>_<stream>.csv with the channel names as columns and shows the stream on the
//     live page. (PROTOCOL.md "Protocol v2.1".)
//
// WIRING NOTES
//  - I2C: app_i2c_bus() is the MAX30102's bus (port 0, CONFIG_PPG_I2C_SDA/SCL_GPIO, 400 kHz).
//    Sharing it is the simple way, but every transaction of yours holds the bus while the PPG
//    task waits: keep one read() under ~2 ms (a few bytes), never clock-stretch for a conversion.
//    Start the conversion in one read() and fetch the result in the next instead of waiting.
//    The camera's SCCB is on port 1; leave it alone.
//  - ADC instead of I2C (an analog potentiostat output on a GPIO): use esp_adc/adc_oneshot.h,
//    create the unit + channel in init(), adc_oneshot_read() in read(). Add esp_adc to REQUIRES in
//    main/CMakeLists.txt. Average a few readings; the ESP32-S3 ADC is noisy (~ +-10 counts).
//  - The MAX30102's LED pulses and Wi-Fi transmissions put ripple on the 3.3 V rail. A high
//    impedance electrode input will see it: check the raw signal with the PPG running.
// ---------------------------------------------------------------------------------------------
#include "sdkconfig.h"

#if CONFIG_PPG_AUX && CONFIG_PPG_AUX_TEMPLATE  // TODO: your Kconfig option
#include "xstream.h"

#include "driver/i2c_master.h"
#include "esp_err.h"

// TODO: from the datasheet
#define TEMPLATE_I2C_ADDR 0x48
#define TEMPLATE_I2C_HZ 400000
#define TEMPLATE_REG_ID 0x00      // reads TEMPLATE_ID
#define TEMPLATE_ID 0xA5
#define TEMPLATE_REG_CONFIG 0x01  // gain, bias, continuous conversion
#define TEMPLATE_REG_DATA 0x10    // 4 bytes: channel 0 MSB, LSB, channel 1 MSB, LSB (signed)
#define TEMPLATE_CONFIG 0x83

static i2c_master_dev_handle_t s_dev;

// Set the sensor up; false = not found. Called at boot and then once per second until it
// returns true, so it must be safe to call again after a failure (don't leak the device handle).
static bool template_init(void)
{
    i2c_master_bus_handle_t bus = app_i2c_bus();
    if (!bus) return false;  // the PPG task hasn't created the bus yet: we are called again
    if (!s_dev) {
        i2c_device_config_t dc = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = TEMPLATE_I2C_ADDR,
            .scl_speed_hz = TEMPLATE_I2C_HZ,
        };
        if (i2c_master_bus_add_device(bus, &dc, &s_dev) != ESP_OK) {
            s_dev = NULL;
            return false;
        }
    }
    uint8_t reg = TEMPLATE_REG_ID, id = 0;
    if (i2c_master_transmit_receive(s_dev, &reg, 1, &id, 1, 20) != ESP_OK || id != TEMPLATE_ID) return false;
    const uint8_t cfg[2] = {TEMPLATE_REG_CONFIG, TEMPLATE_CONFIG};
    return i2c_master_transmit(s_dev, cfg, sizeof cfg, 20) == ESP_OK;
}

// One sample. t_us is the time just before this call (the M line's time): if your value is older
// than that (a conversion that finished a known time ago), that is fine at these rates; say so
// in the H line (`.extra = ",lag_ms=.."`) rather than adjusting it here.
// false = the read failed: nothing is sent, <stream>_err counts it.
static bool template_read(int64_t t_us, int32_t *vals, bool *valid)
{
    (void)t_us;
    uint8_t reg = TEMPLATE_REG_DATA, b[4];
    if (i2c_master_transmit_receive(s_dev, &reg, 1, b, sizeof b, 20) != ESP_OK) return false;
    int16_t c0 = (int16_t)((uint16_t)b[0] << 8 | b[1]);
    int16_t c1 = (int16_t)((uint16_t)b[2] << 8 | b[3]);
    vals[0] = c0;
    vals[1] = c1;
    // a channel without a usable value this time (electrode saturated, still settling): send an
    // empty field instead of a made-up number
    if (c1 == INT16_MIN || c1 == INT16_MAX) valid[1] = false;
    return true;
}

const xs_stream_t xs_template_stream = {
    .sid = XS_SID_TEMPLATE,  // TODO: your own id
    .stream = "chem",
    .kind = XS_KIND_SAMPLES,
    .sensor = "TEMPLATE",     // TODO: part name
    .fs_mhz = 200,            // one sample per 5 s
    .nch = 2,
    .ch = {"cortisol", "uric_acid"},
    // raw ADC codes; 1 code = 0.25 nA with this (made-up) gain, so value x 0.25 = current in nA
    .unit = {"nA", "nA"},
    .decimals = {0, 0},
    .scale = {"0.25", "0.25"},
    .extra = ",rtia_ohm=1000000,bias_mv=350,cal=none",
    .site = "forehead",       // NULL = the board's site
    .init = template_init,
    .read = template_read,
};
#endif
