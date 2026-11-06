// Board log -> protocol events (USB binary link, CONFIG_PPG_FORWARD_LOGS).
//
// The console is UART0 (GPIO43/44), which nothing is wired to on the glasses, so driver
// warnings were invisible. This captures both ways the firmware and its components print:
//   ESP_LOGx                      (esp_log_set_vprintf hook; the UART console still gets it)
//   ets_printf / esp_rom_printf   (ROM printf channel 2; esp32-camera's cam_hal prints its
//                                  frame-loss warnings this way, from its own task)
// and queues the lines; the sensor task sends them as  $E,<t_us>,LOG,<text>.
//
// Boot lines (everything up to the first START, including INFO-level driver setup) are also kept
// in a stash and sent again as  $E,<t_us>,BOOTLOG,<text>  on every INFO, because they are printed
// before the gateway has the port open. After boot only W/E lines (and all ROM lines) are sent.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "sdkconfig.h"

#if CONFIG_PPG_FORWARD_LOGS
void logfwd_install(void);   // first thing in app_main
void logfwd_boot_done(void); // first START: stop stashing, forward warnings/errors only
// Next queued line into buf (sanitised, no CR/LF/'$'/'*'); false if none.
bool logfwd_next(char *buf, int size);
// Stashed boot line i (0..); false past the end.
bool logfwd_boot_line(int i, char *buf, int size);
uint32_t logfwd_lost(void);  // lines dropped because the queue was full
#endif
