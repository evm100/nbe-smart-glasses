// Wi-Fi station (WPA2/3-Personal, or WPA2-Enterprise PEAP) + SNTP clock, for the MQTT link.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

// Starts Wi-Fi in the background and keeps reconnecting forever.
// on_ip is called (from the event task) every time an IP address is obtained.
esp_err_t net_start(void (*on_ip)(void));
bool net_has_ip(void);
void net_watchdog(void);                // call every few 100 ms: restarts a stuck reconnect loop
int net_rssi(void);                     // dBm, 0 if not connected
int net_tx_power_dbm(void);             // Wi-Fi transmit power limit in effect
void net_ip_str(char *out, int len);    // "" if none
void net_mac_str(char *out, int len);   // Wi-Fi station MAC "aa:bb:cc:dd:ee:ff", "" if unknown
void net_ssid_str(char *out, int len);  // saved network in use (JSON-safe), "" if none
bool net_time_synced(void);
int64_t net_epoch_ms(void);
