#pragma once
// Sent as fw= in the H header line and in the MQTT status message.
// 0.9.0: protocol v2.1 - auxiliary streams (xstream.c: $M lines, CONFIG_PPG_AUX) and camera frame
//        statistics computed on the board (CONFIG_PPG_CAMERA_EDGE_FEATURES). Both off by default:
//        a build without them sends what 0.8.0 sent.
#define PPG_FW_VERSION "0.9.0-idf"
