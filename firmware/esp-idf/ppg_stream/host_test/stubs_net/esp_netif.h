#pragma once
#include <stdint.h>
#include "esp_err.h"
typedef struct esp_netif_obj esp_netif_t;
typedef struct { uint32_t addr; } esp_ip4_addr_t;
typedef struct { esp_ip4_addr_t ip, netmask, gw; } esp_netif_ip_info_t;
typedef struct { int if_index; esp_netif_t *esp_netif; esp_netif_ip_info_t ip_info; int ip_changed; } ip_event_got_ip_t;
#define IPSTR "%d.%d.%d.%d"
#define IP2STR(a) (int)((a)->addr & 0xff), (int)(((a)->addr >> 8) & 0xff), (int)(((a)->addr >> 16) & 0xff), (int)(((a)->addr >> 24) & 0xff)
enum { IP_EVENT_STA_GOT_IP = 0 };
esp_err_t esp_netif_init(void);
esp_netif_t *esp_netif_create_default_wifi_sta(void);
esp_err_t esp_netif_set_hostname(esp_netif_t *n, const char *hostname);
esp_err_t esp_netif_get_ip_info(esp_netif_t *n, esp_netif_ip_info_t *ip);
