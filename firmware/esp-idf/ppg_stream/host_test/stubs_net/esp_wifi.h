#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_event.h"
typedef enum { WIFI_AUTH_OPEN = 0, WIFI_AUTH_WEP, WIFI_AUTH_WPA_PSK, WIFI_AUTH_WPA2_PSK } wifi_auth_mode_t;
typedef enum { WIFI_MODE_NULL = 0, WIFI_MODE_STA } wifi_mode_t;
typedef enum { WIFI_IF_STA = 0 } wifi_interface_t;
typedef enum { WIFI_STORAGE_FLASH, WIFI_STORAGE_RAM } wifi_storage_t;
typedef enum { WIFI_PS_NONE, WIFI_PS_MIN_MODEM, WIFI_PS_MAX_MODEM } wifi_ps_type_t;
typedef enum { WIFI_BW20 = 1, WIFI_BW40 = 2 } wifi_bandwidth_t;  // ESP-IDF v6 names
typedef struct { bool capable; bool required; } wifi_pmf_config_t;
typedef struct { int8_t rssi; wifi_auth_mode_t authmode; } wifi_scan_threshold_t;
typedef enum { WPA3_SAE_PWE_UNSPECIFIED, WPA3_SAE_PWE_HUNT_AND_PECK, WPA3_SAE_PWE_HASH_TO_ELEMENT, WPA3_SAE_PWE_BOTH } wifi_sae_pwe_method_t;
typedef struct { uint8_t ssid[32]; uint8_t password[64]; wifi_scan_threshold_t threshold; wifi_pmf_config_t pmf_cfg; wifi_sae_pwe_method_t sae_pwe_h2e; } wifi_sta_config_t;
typedef union { wifi_sta_config_t sta; } wifi_config_t;
typedef struct { uint8_t ssid[33]; uint8_t primary; int8_t rssi; wifi_auth_mode_t authmode; } wifi_ap_record_t;
typedef struct wifi_scan_config_t wifi_scan_config_t;
typedef struct { int dummy; } wifi_init_config_t;
typedef struct { uint8_t ssid[32]; uint8_t ssid_len; uint8_t bssid[6]; uint8_t reason; int8_t rssi; } wifi_event_sta_disconnected_t;
#define WIFI_INIT_CONFIG_DEFAULT() { 0 }
enum { WIFI_EVENT_SCAN_DONE = 1, WIFI_EVENT_STA_START = 2, WIFI_EVENT_STA_DISCONNECTED = 5 };
enum { WIFI_REASON_NO_AP_FOUND = 201 };
esp_err_t esp_wifi_init(const wifi_init_config_t *c);
esp_err_t esp_wifi_set_storage(wifi_storage_t s);
esp_err_t esp_wifi_set_mode(wifi_mode_t m);
esp_err_t esp_wifi_set_config(wifi_interface_t i, wifi_config_t *c);
esp_err_t esp_wifi_start(void);
esp_err_t esp_wifi_connect(void);
esp_err_t esp_wifi_disconnect(void);
esp_err_t esp_wifi_set_ps(wifi_ps_type_t t);
esp_err_t esp_wifi_set_bandwidth(wifi_interface_t i, wifi_bandwidth_t bw);
esp_err_t esp_wifi_set_max_tx_power(int8_t power);
esp_err_t esp_wifi_get_max_tx_power(int8_t *power);
esp_err_t esp_wifi_sta_get_ap_info(wifi_ap_record_t *ap);
esp_err_t esp_wifi_scan_start(const wifi_scan_config_t *c, bool block);
esp_err_t esp_wifi_scan_get_ap_num(uint16_t *n);
esp_err_t esp_wifi_scan_get_ap_records(uint16_t *n, wifi_ap_record_t *r);
