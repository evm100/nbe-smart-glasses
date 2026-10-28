// Minimal MAX30102 driver on the ESP-IDF i2c_master API (IDF >= 5.2, tested target v6.x).
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

#define MAX30102_I2C_ADDR   0x57
#define MAX30102_PART_ID    0x15
#define MAX30102_FIFO_DEPTH 32
#define MAX30102_ADC_MAX    0x3FFFF   // 18-bit, data is left-justified

typedef enum {
    MAX30102_MODE_HR = 0x02,    // red only
    MAX30102_MODE_SPO2 = 0x03,  // red + IR
} max30102_mode_t;

typedef struct {
    max30102_mode_t mode;
    uint16_t sample_rate;     // 50, 100, 200, 400, 800, 1000, 1600, 3200
    uint8_t sample_avg;       // 1, 2, 4, 8, 16, 32
    uint16_t pulse_width_us;  // 69, 118, 215, 411
    uint16_t adc_range_na;    // 2048, 4096, 8192, 16384
    uint16_t red_ma_x10;      // LED current in 0.1 mA (0-510)
    uint16_t ir_ma_x10;
    uint8_t fifo_a_full;      // 0-15 empty slots left when A_FULL fires
    bool int_a_full;          // enable the A_FULL interrupt on the INT pin
} max30102_config_t;

typedef struct {
    i2c_master_dev_handle_t dev;
    max30102_config_t cfg;
    uint8_t n_leds;  // 1 (HR) or 2 (SpO2)
} max30102_t;

// Add the device to an existing bus, check the part ID, reset and configure.
esp_err_t max30102_init(max30102_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz,
                        const max30102_config_t *cfg);
esp_err_t max30102_configure(max30102_t *s, const max30102_config_t *cfg);

// Read every sample waiting in the FIFO (max MAX30102_FIFO_DEPTH), oldest first.
// red[]/ir[] receive raw 18-bit counts (ir[] untouched in HR mode).
// *ovf = OVF_COUNTER before the read (> 0 means samples were lost).
esp_err_t max30102_read_fifo(max30102_t *s, uint32_t *red, uint32_t *ir, int *n, uint8_t *ovf);

esp_err_t max30102_read_int_status(max30102_t *s, uint8_t *status1, uint8_t *status2);
esp_err_t max30102_start_temp(max30102_t *s);
// *ready = false if the conversion started by max30102_start_temp() hasn't finished
esp_err_t max30102_read_temp(max30102_t *s, int32_t *centi_c, bool *ready);

// Helpers
uint8_t max30102_adc_bits(uint16_t pulse_width_us);
// Output rate after averaging, in milli-Hz (e.g. 400000 = 400 Hz)
uint32_t max30102_output_rate_mhz(const max30102_config_t *c);
// Returns a warning string if the settings look physically impossible
// (LED pulses don't fit in one sample period), else NULL.
const char *max30102_timing_warning(const max30102_config_t *c);

// INT status bits
#define MAX30102_INT_A_FULL  0x80
#define MAX30102_INT_PPG_RDY 0x40
#define MAX30102_INT_ALC_OVF 0x20
#define MAX30102_INT_PWR_RDY 0x01
