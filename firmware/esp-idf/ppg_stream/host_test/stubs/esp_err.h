#pragma once
#include <stdio.h>
#include <stdlib.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_NOT_FOUND 0x105
#define ESP_ERR_TIMEOUT 0x107
static inline const char *esp_err_to_name(esp_err_t e) { return e == 0 ? "ESP_OK" : "ESP_ERR"; }
#define ESP_ERROR_CHECK(x) do { esp_err_t _e = (x); if (_e != ESP_OK) { fprintf(stderr, "ESP_ERROR_CHECK failed %d\n", _e); abort(); } } while (0)
