#include "servo.h"

#include "driver/ledc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SERVO_HZ 50
#define DUTY_BITS LEDC_TIMER_14_BIT
#define PERIOD_US (1000000 / SERVO_HZ)

esp_err_t servo_init(int gpio)
{
    ledc_timer_config_t t = {.speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = DUTY_BITS,
                             .timer_num = LEDC_TIMER_0, .freq_hz = SERVO_HZ, .clk_cfg = LEDC_AUTO_CLK};
    esp_err_t err = ledc_timer_config(&t);
    if (err != ESP_OK) return err;
    ledc_channel_config_t c = {.gpio_num = gpio, .speed_mode = LEDC_LOW_SPEED_MODE, .channel = LEDC_CHANNEL_0,
                               .timer_sel = LEDC_TIMER_0, .duty = 0, .hpoint = 0};
    return ledc_channel_config(&c);  // duty 0 = no pulses = limp
}

void servo_press(int pulse_us, int ms)
{
    uint32_t duty = (uint32_t)((uint64_t)pulse_us * ((1u << DUTY_BITS) - 1) / PERIOD_US);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    vTaskDelay(pdMS_TO_TICKS(ms));
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}
