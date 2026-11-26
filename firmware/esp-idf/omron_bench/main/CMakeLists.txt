idf_component_register(SRCS "app_main.c" "omron_ble.c" "om_record.c" "servo.c"
                       INCLUDE_DIRS "."
                       REQUIRES bt nvs_flash console esp_driver_ledc esp_timer)
