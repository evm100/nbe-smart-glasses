#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
typedef struct { uint32_t tx_buffer_size; uint32_t rx_buffer_size; } usb_serial_jtag_driver_config_t;
#define USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT() (usb_serial_jtag_driver_config_t) {\
    .tx_buffer_size = 256,\
    .rx_buffer_size = 256,\
}
esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *c);
int usb_serial_jtag_read_bytes(void *buf, uint32_t length, uint32_t ticks_to_wait);
int usb_serial_jtag_write_bytes(const void *src, size_t size, uint32_t ticks_to_wait);
