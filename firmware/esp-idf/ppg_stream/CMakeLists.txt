# PPG serial protocol v1 streamer – ESP32-S3 (Seeed XIAO) + MAX30102
# Build: idf.py set-target esp32s3 && idf.py menuconfig && idf.py build flash
cmake_minimum_required(VERSION 3.16)

# Per-node settings written by firmware/tools/nbe_node.py (node ID, broker, Wi-Fi...).
# Note: defaults only fill values missing from an existing `sdkconfig`; nbe_node.py also
# patches sdkconfig directly, so `idf.py build` picks the new values up either way.
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.nbe")
    set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.nbe")
endif()

# app version (esp_app_desc, shown by the server for uploaded OTA images) = PPG_FW_VERSION
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/main/version.h" _ver REGEX "define PPG_FW_VERSION")
string(REGEX REPLACE ".*\"(.*)\".*" "\\1" PROJECT_VER "${_ver}")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# trim the component list to what we use (faster builds)
idf_build_set_property(MINIMAL_BUILD ON)

project(ppg_stream)
