// USB-C side of the hybrid link (CONFIG_PPG_LINK_HYBRID, see link_mqtt.c).
//
// The board listens on USB Serial/JTAG for an NBE USB gateway (or `sglab record`). Those send a
// command line ("PING", "INFO", "START"...) at least every second; while they do, the gateway is
// "present" and link_mqtt.c streams the record envelopes over USB instead of Wi-Fi. A PC that only
// powers or charges the board sends nothing, so it never takes the link.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define USBGW_TIMEOUT_US (5 * 1000000LL)  // no line from the gateway for this long = gone

// Installs the USB Serial/JTAG driver and the reader task. Every line except PING is handed to
// on_cmd (without CR/LF), from the reader task.
esp_err_t usbgw_init(void (*on_cmd)(const char *line, size_t len));
// A gateway spoke within USBGW_TIMEOUT_US and the USB host is still there.
bool usbgw_present(void);
// Writes all of p in chunks; false = the host stopped reading (the rest is not sent).
bool usbgw_put(const uint8_t *p, size_t n);
