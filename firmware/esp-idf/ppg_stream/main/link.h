// Link to the PC / server, chosen in menuconfig:
//   USB Serial/JTAG (XIAO USB-C) or UART  -> link.c       (byte stream to a PC running sglab)
//   Wi-Fi + MQTT over TLS                 -> link_mqtt.c  (batches of protocol lines to the NBE server)
// app_main.c doesn't care which: it writes whole protocol lines and reads command bytes.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t link_init(void);
// Thread-safe. Writes whole buffer or as much as fits; returns bytes written. Whole protocol lines.
int link_write(const char *buf, size_t len);
// n consecutive samples (seq0, seq0+1, ...) with their device times; ch1 = NULL for one channel.
// USB/UART: D lines; MQTT: a compact binary record (frame.h). Never blocks on the network.
void link_write_samples(uint32_t seq0, const int64_t *t_us, const uint32_t *ch0, const uint32_t *ch1, int n,
                        int64_t period_ns);
// Returns bytes read (0 on timeout).
int link_read(uint8_t *buf, size_t max, uint32_t timeout_ms);
// Bytes that could not be sent because the PC wasn't reading fast enough
// (MQTT: because the outbox was full during a long Wi-Fi outage).
uint32_t link_dropped_bytes(void);

// Send buffered lines now (MQTT batches; no-op on USB/UART).
void link_flush(void);

// State shown in the node's MQTT status message (no-op on USB/UART).
void link_note_state(bool streaming, bool sensor_ok, uint32_t fs_mhz);
// Something in the status message changed (OTA progress): publish it soon (no-op on USB/UART).
void link_status_changed(void);
// "OTA <token>" command: download and install new firmware (MQTT link only, see ota.c).
void link_ota(const char *token);
// Current UTC time in ms if the clock is synchronised (SNTP); false on USB/UART.
bool link_epoch_ms(int64_t *ms);
