// Subset of espressif/mqtt's public API (same names / types as the real header).
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_event.h"
typedef struct esp_mqtt_client *esp_mqtt_client_handle_t;
typedef enum { MQTT_EVENT_ANY = -1, MQTT_EVENT_ERROR = 0, MQTT_EVENT_CONNECTED, MQTT_EVENT_DISCONNECTED,
               MQTT_EVENT_SUBSCRIBED, MQTT_EVENT_UNSUBSCRIBED, MQTT_EVENT_PUBLISHED, MQTT_EVENT_DATA } esp_mqtt_event_id_t;
typedef enum { MQTT_ERROR_TYPE_NONE = 0, MQTT_ERROR_TYPE_TCP_TRANSPORT, MQTT_ERROR_TYPE_CONNECTION_REFUSED } esp_mqtt_error_type_t;
typedef struct { esp_err_t esp_tls_last_esp_err; int esp_tls_stack_err; int esp_tls_cert_verify_flags;
                 esp_mqtt_error_type_t error_type; int connect_return_code; int esp_transport_sock_errno; } esp_mqtt_error_codes_t;
typedef struct { esp_mqtt_event_id_t event_id; esp_mqtt_client_handle_t client; char *data; int data_len;
                 int total_data_len; int current_data_offset; char *topic; int topic_len; int msg_id;
                 int session_present; esp_mqtt_error_codes_t *error_handle; bool retain; int qos; bool dup; } esp_mqtt_event_t;
typedef esp_mqtt_event_t *esp_mqtt_event_handle_t;
struct esp_tls_stub;
typedef struct {
    struct { struct { const char *uri; } address;
             struct { const char *certificate; esp_err_t (*crt_bundle_attach)(void *conf); } verification; } broker;
    struct { const char *client_id; struct { const char *certificate; const char *key; } authentication; } credentials;
    struct { struct { const char *topic; const char *msg; int msg_len; int qos; int retain; } last_will;
             bool disable_clean_session; int keepalive; int message_retransmit_timeout; } session;
    struct { int reconnect_timeout_ms; int timeout_ms; } network;
    struct { int priority; int stack_size; } task;
    struct { int size; int out_size; } buffer;
    struct { uint64_t limit; } outbox;
} esp_mqtt_client_config_t;
esp_mqtt_client_handle_t esp_mqtt_client_init(const esp_mqtt_client_config_t *config);
esp_err_t esp_mqtt_client_start(esp_mqtt_client_handle_t client);
esp_err_t esp_mqtt_client_reconnect(esp_mqtt_client_handle_t client);
int esp_mqtt_client_subscribe(esp_mqtt_client_handle_t client, const char *topic, int qos);
int esp_mqtt_client_publish(esp_mqtt_client_handle_t client, const char *topic, const char *data, int len, int qos, int retain);
int esp_mqtt_client_enqueue(esp_mqtt_client_handle_t client, const char *topic, const char *data, int len, int qos, int retain, bool store);
int esp_mqtt_client_get_outbox_size(esp_mqtt_client_handle_t client);
esp_err_t esp_mqtt_client_register_event(esp_mqtt_client_handle_t client, esp_mqtt_event_id_t event,
                                         esp_event_handler_t event_handler, void *event_handler_arg);
