#include "max30102.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// Register map (MAX30102 datasheet)
#define REG_INT_STATUS1   0x00
#define REG_INT_STATUS2   0x01
#define REG_INT_ENABLE1   0x02
#define REG_INT_ENABLE2   0x03
#define REG_FIFO_WR_PTR   0x04
#define REG_OVF_COUNTER   0x05
#define REG_FIFO_RD_PTR   0x06
#define REG_FIFO_DATA     0x07
#define REG_FIFO_CONFIG   0x08
#define REG_MODE_CONFIG   0x09
#define REG_SPO2_CONFIG   0x0A
#define REG_LED1_PA       0x0C  // red
#define REG_LED2_PA       0x0D  // IR
#define REG_TEMP_INT      0x1F
#define REG_TEMP_FRAC     0x20
#define REG_TEMP_CONFIG   0x21
#define REG_PART_ID       0xFF

#define MODE_RESET        0x40
#define FIFO_ROLLOVER_EN  0x10
#define INT2_DIE_TEMP_RDY 0x02

#define I2C_TIMEOUT_MS 20

static esp_err_t wr(max30102_t *s, uint8_t reg, uint8_t val)
{
    uint8_t b[2] = {reg, val};
    return i2c_master_transmit(s->dev, b, sizeof b, I2C_TIMEOUT_MS);
}

static esp_err_t rd(max30102_t *s, uint8_t reg, uint8_t *buf, size_t len)
{
    return i2c_master_transmit_receive(s->dev, &reg, 1, buf, len, I2C_TIMEOUT_MS);
}

uint8_t max30102_adc_bits(uint16_t pw)
{
    return pw <= 69 ? 15 : pw <= 118 ? 16 : pw <= 215 ? 17 : 18;
}

static uint8_t code_avg(uint8_t n)
{
    switch (n) {
    case 1: return 0; case 2: return 1; case 4: return 2; case 8: return 3; case 16: return 4;
    default: return 5;  // 32
    }
}

static uint8_t code_sr(uint16_t sr)
{
    static const uint16_t rates[] = {50, 100, 200, 400, 800, 1000, 1600, 3200};
    for (uint8_t i = 0; i < 8; i++) {
        if (rates[i] == sr) return i;
    }
    return 1;  // 100
}

static uint8_t code_pw(uint16_t pw)
{
    return pw <= 69 ? 0 : pw <= 118 ? 1 : pw <= 215 ? 2 : 3;
}

static uint8_t code_range(uint16_t na)
{
    return na <= 2048 ? 0 : na <= 4096 ? 1 : na <= 8192 ? 2 : 3;
}

static uint8_t led_pa(uint16_t ma_x10)
{
    uint32_t v = (ma_x10 + 1) / 2;  // 0.2 mA per LSB, round
    return v > 255 ? 255 : (uint8_t)v;
}

uint32_t max30102_output_rate_mhz(const max30102_config_t *c)
{
    return (uint32_t)c->sample_rate * 1000u / (c->sample_avg ? c->sample_avg : 1);
}

const char *max30102_timing_warning(const max30102_config_t *c)
{
    // Each sample fires one pulse per active LED. If the pulses alone take more
    // than ~80 % of the sample period the combination cannot work.
    uint32_t n_leds = (c->mode == MAX30102_MODE_SPO2) ? 2 : 1;
    uint32_t busy_us = n_leds * c->pulse_width_us;
    uint32_t period_us = 1000000u / c->sample_rate;
    if (busy_us * 10 > period_us * 8) {
        return "sample rate too high for this pulse width (see datasheet SpO2/HR settings tables)";
    }
    return NULL;
}

esp_err_t max30102_configure(max30102_t *s, const max30102_config_t *cfg)
{
    esp_err_t err;
    s->cfg = *cfg;
    s->n_leds = (cfg->mode == MAX30102_MODE_SPO2) ? 2 : 1;

    // reset, wait for the RESET bit to self-clear
    if ((err = wr(s, REG_MODE_CONFIG, MODE_RESET)) != ESP_OK) return err;
    for (int i = 0; i < 50; i++) {
        uint8_t m = MODE_RESET;
        vTaskDelay(pdMS_TO_TICKS(2));
        if (rd(s, REG_MODE_CONFIG, &m, 1) == ESP_OK && !(m & MODE_RESET)) break;
    }
    uint8_t st[2];
    rd(s, REG_INT_STATUS1, st, 2);  // clear PWR_RDY

    uint8_t fifo = (uint8_t)((code_avg(cfg->sample_avg) << 5) | FIFO_ROLLOVER_EN | (cfg->fifo_a_full & 0x0F));
    uint8_t spo2 = (uint8_t)((code_range(cfg->adc_range_na) << 5) | (code_sr(cfg->sample_rate) << 2) |
                             code_pw(cfg->pulse_width_us));
    const uint8_t seq[][2] = {
        {REG_FIFO_CONFIG, fifo},
        {REG_SPO2_CONFIG, spo2},
        {REG_LED1_PA, led_pa(cfg->red_ma_x10)},
        {REG_LED2_PA, cfg->mode == MAX30102_MODE_SPO2 ? led_pa(cfg->ir_ma_x10) : 0},
        {REG_INT_ENABLE1, cfg->int_a_full ? MAX30102_INT_A_FULL : 0},
        {REG_INT_ENABLE2, INT2_DIE_TEMP_RDY},
        {REG_MODE_CONFIG, (uint8_t)cfg->mode},
    };
    for (size_t i = 0; i < sizeof seq / sizeof seq[0]; i++) {
        if ((err = wr(s, seq[i][0], seq[i][1])) != ESP_OK) return err;
    }
    return max30102_clear_fifo(s);
}

esp_err_t max30102_init(max30102_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz,
                        const max30102_config_t *cfg)
{
    memset(s, 0, sizeof *s);
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = MAX30102_I2C_ADDR,
        .scl_speed_hz = scl_hz,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &dc, &s->dev);
    if (err != ESP_OK) return err;

    uint8_t id = 0;
    if ((err = rd(s, REG_PART_ID, &id, 1)) != ESP_OK || id != MAX30102_PART_ID) {
        i2c_master_bus_rm_device(s->dev);
        s->dev = NULL;
        return err != ESP_OK ? err : ESP_ERR_NOT_FOUND;
    }
    return max30102_configure(s, cfg);
}

esp_err_t max30102_clear_fifo(max30102_t *s)
{
    esp_err_t err;
    if ((err = wr(s, REG_FIFO_WR_PTR, 0)) != ESP_OK) return err;
    if ((err = wr(s, REG_OVF_COUNTER, 0)) != ESP_OK) return err;
    return wr(s, REG_FIFO_RD_PTR, 0);
}

esp_err_t max30102_read_fifo(max30102_t *s, uint32_t *red, uint32_t *ir, int *n, uint8_t *ovf)
{
    uint8_t ptr[3];  // WR_PTR, OVF_COUNTER, RD_PTR (consecutive registers, auto-increment)
    *n = 0;
    *ovf = 0;
    esp_err_t err = rd(s, REG_FIFO_WR_PTR, ptr, 3);
    if (err != ESP_OK) return err;
    uint8_t wr_ptr = ptr[0] & 0x1F, ovf_cnt = ptr[1] & 0x1F, rd_ptr = ptr[2] & 0x1F;
    int count = (wr_ptr - rd_ptr) & 0x1F;
    if (ovf_cnt) count = MAX30102_FIFO_DEPTH;  // FIFO full (and samples were overwritten)
    *ovf = ovf_cnt;
    if (count == 0) return ESP_OK;

    // FIFO_DATA does not auto-increment: one burst returns consecutive FIFO bytes
    uint8_t buf[MAX30102_FIFO_DEPTH * 6];
    size_t bps = 3u * s->n_leds;
    if ((err = rd(s, REG_FIFO_DATA, buf, (size_t)count * bps)) != ESP_OK) return err;

    for (int k = 0; k < count; k++) {
        const uint8_t *p = &buf[k * bps];
        red[k] = (((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]) & MAX30102_ADC_MAX;  // LED1
        if (s->n_leds == 2) {
            ir[k] = (((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 8) | p[5]) & MAX30102_ADC_MAX;  // LED2
        }
    }
    *n = count;
    return ESP_OK;
}

esp_err_t max30102_read_int_status(max30102_t *s, uint8_t *status1, uint8_t *status2)
{
    uint8_t st[2] = {0, 0};
    esp_err_t err = rd(s, REG_INT_STATUS1, st, 2);
    *status1 = st[0];
    *status2 = st[1];
    return err;
}

esp_err_t max30102_start_temp(max30102_t *s)
{
    return wr(s, REG_TEMP_CONFIG, 0x01);  // TEMP_EN, self-clearing
}

esp_err_t max30102_read_temp(max30102_t *s, int32_t *centi_c, bool *ready)
{
    uint8_t cfg = 0;
    esp_err_t err = rd(s, REG_TEMP_CONFIG, &cfg, 1);
    if (err != ESP_OK) return err;
    *ready = !(cfg & 0x01);
    if (!*ready) return ESP_OK;
    uint8_t t[2];
    if ((err = rd(s, REG_TEMP_INT, t, 2)) != ESP_OK) return err;  // 0x1F int (2's compl.), 0x20 frac (1/16)
    *centi_c = (int32_t)(int8_t)t[0] * 100 + (int32_t)(t[1] & 0x0F) * 625 / 100;
    return ESP_OK;
}
