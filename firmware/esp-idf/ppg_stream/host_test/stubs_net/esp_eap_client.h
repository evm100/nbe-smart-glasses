#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
esp_err_t esp_eap_client_set_identity(const unsigned char *identity, int len);
esp_err_t esp_eap_client_set_username(const unsigned char *username, int len);
esp_err_t esp_eap_client_set_password(const unsigned char *password, int len);
esp_err_t esp_eap_client_use_default_cert_bundle(bool use_default_bundle);
esp_err_t esp_eap_client_set_domain_name(const char *domain_name);
esp_err_t esp_wifi_sta_enterprise_enable(void);
esp_err_t esp_wifi_sta_enterprise_disable(void);
