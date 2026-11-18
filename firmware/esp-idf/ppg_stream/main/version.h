#pragma once
// Sent as fw= in the H header line and in the MQTT status message.
// 0.9.0: protocol v2.1 - auxiliary streams (xstream.c: $M lines, CONFIG_PPG_AUX) and camera frame
//        statistics computed on the board (CONFIG_PPG_CAMERA_EDGE_FEATURES). Both off by default:
//        a build without them sends what 0.8.0 sent.
// 0.10.0: protocol v2.2 - a second MAX30102 on the other I2C controller (CONFIG_PPG_PPG2, sdkconfig.dual:
//         dual-PPG glasses, stream ppg_r, sid 4, samples record 0x04). Off by default: a build without it
//         sends what 0.9.0 sent.
#define PPG_FW_VERSION "0.10.0-idf"
