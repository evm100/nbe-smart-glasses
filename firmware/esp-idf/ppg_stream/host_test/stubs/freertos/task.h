#pragma once
#include "freertos/FreeRTOS.h"
typedef void (*TaskFunction_t)(void *);
void vTaskDelay(TickType_t ticks);
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t f, const char *name, uint32_t stack, void *arg,
                                   int prio, void *handle, int core);
typedef void *TaskHandle_t;
void xTaskNotifyGive(TaskHandle_t t);
uint32_t ulTaskNotifyTake(BaseType_t clear, TickType_t wait);
