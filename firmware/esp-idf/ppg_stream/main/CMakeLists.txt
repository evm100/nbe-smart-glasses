# Sources and embedded certificates depend on the link chosen in menuconfig.
# (Requirements are listed unconditionally: Kconfig values aren't known during the
#  build system's first pass, and unused components cost only build time.)
set(srcs "app_main.c" "max30102.c" "proto.c" "diag.c")
set(embed "")
if(CONFIG_PPG_LINK_MQTT)
    list(APPEND srcs "link_mqtt.c" "frame.c" "net.c" "ota.c")
    set(certs "${CMAKE_CURRENT_LIST_DIR}/certs")
    foreach(f client.crt client.key)
        if(NOT EXISTS "${certs}/${f}")
            message(FATAL_ERROR "main/certs/${f} is missing. Get this node's credentials with\n"
                                "  python ../../tools/nbe_node.py configure <NODE_ID>\n"
                                "(or switch the link back to USB in menuconfig).")
        endif()
    endforeach()
    list(APPEND embed "certs/client.crt" "certs/client.key")
    if(CONFIG_PPG_MQTT_CA_PEM)
        if(NOT EXISTS "${certs}/broker_ca.crt")
            message(FATAL_ERROR "main/certs/broker_ca.crt is missing (Broker certificate check = private CA)")
        endif()
        list(APPEND embed "certs/broker_ca.crt")
    endif()
else()
    list(APPEND srcs "link.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "."
    REQUIRES esp_driver_i2c esp_driver_gpio esp_driver_uart esp_driver_usb_serial_jtag esp_timer
             esp_wifi esp_netif esp_event nvs_flash mbedtls wpa_supplicant
             esp_https_ota esp_http_client app_update esp_app_format
    # espressif/mqtt comes from idf_component.yml (manifest dependencies are added automatically)
    EMBED_TXTFILES ${embed}
)
