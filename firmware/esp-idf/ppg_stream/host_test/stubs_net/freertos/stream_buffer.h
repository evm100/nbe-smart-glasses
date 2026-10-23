#pragma once
#include <stddef.h>
#include "freertos/FreeRTOS.h"
typedef void *StreamBufferHandle_t;
StreamBufferHandle_t xStreamBufferCreate(size_t size, size_t trigger);
size_t xStreamBufferSend(StreamBufferHandle_t b, const void *data, size_t len, TickType_t wait);
size_t xStreamBufferReceive(StreamBufferHandle_t b, void *data, size_t len, TickType_t wait);
