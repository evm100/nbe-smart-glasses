#pragma once
#include <stdint.h>
#include "esp_err.h"
typedef int gpio_num_t;
typedef enum { GPIO_MODE_INPUT = 1, GPIO_MODE_OUTPUT = 2 } gpio_mode_t;
typedef enum { GPIO_PULLUP_DISABLE = 0, GPIO_PULLUP_ENABLE = 1 } gpio_pullup_t;
typedef enum { GPIO_PULLDOWN_DISABLE = 0, GPIO_PULLDOWN_ENABLE = 1 } gpio_pulldown_t;
typedef enum { GPIO_INTR_DISABLE = 0, GPIO_INTR_NEGEDGE = 2 } gpio_int_type_t;
typedef struct { uint64_t pin_bit_mask; gpio_mode_t mode; gpio_pullup_t pull_up_en;
                 gpio_pulldown_t pull_down_en; gpio_int_type_t intr_type; } gpio_config_t;
typedef void (*gpio_isr_t)(void *);
esp_err_t gpio_config(const gpio_config_t *c);
esp_err_t gpio_set_level(gpio_num_t n, uint32_t level);
int gpio_get_level(gpio_num_t n);
esp_err_t gpio_install_isr_service(int flags);
esp_err_t gpio_isr_handler_add(gpio_num_t n, gpio_isr_t f, void *arg);
