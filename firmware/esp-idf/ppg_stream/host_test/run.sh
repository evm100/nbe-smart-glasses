#!/usr/bin/env bash
# Build the firmware sources for the PC against stub ESP-IDF headers + a simulated
# MAX30102, run 5 configurations, and check the output with the PC-side parser.
# Needs gcc and the repo's Python env (sglab). No ESP-IDF required.
set -euo pipefail
cd "$(dirname "$0")"
CF="-std=gnu17 -Wall -Wextra -Werror -Wno-unused-parameter -Istubs -I. -I../main"
SRC="sim.c ../main/max30102.c ../main/proto.c ../main/diag.c ../main/link.c"
gcc $CF -o sim $SRC
gcc $CF -DCONFIG_PPG_INT_GPIO=3 -o sim_int $SRC
gcc $CF -DTEST_UART -o sim_uart $SRC
gcc $CF -DTEST_HR_MODE -DCONFIG_PPG_SAMPLE_RATE_HZ=100 -DCONFIG_PPG_SAMPLE_AVG_N=4 -o sim_hr $SRC
gcc $CF -DCONFIG_PPG_FIFO_POLL_MS=100 -o sim_slow $SRC
# MQTT link: run the real batching code against a fake esp-mqtt (each batch must hold whole lines)
MQ="-DTEST_MQTT -Istubs_net"
gcc $CF $MQ -o sim_mqtt sim.c ../main/max30102.c ../main/proto.c ../main/diag.c ../main/frame.c ../main/link_mqtt.c mqtt_fake.c
# compile-check the Wi-Fi code and the other Kconfig variants against stub headers
gcc $CF $MQ -fsyntax-only ../main/net.c
gcc $CF $MQ -DTEST_EAP -fsyntax-only ../main/net.c
gcc $CF $MQ -DTEST_CA_PEM -fsyntax-only ../main/link_mqtt.c
./sim 3 > out_default.txt 2>/dev/null
./sim_int 3 > out_int.txt 2>/dev/null
./sim_uart 2 > out_uart.txt 2>/dev/null
./sim_hr 3 > out_hr.txt 2>/dev/null
./sim_slow 3 > out_slow.txt 2>/dev/null
rm -f frames_mqtt.bin
./sim_mqtt 6 > out_mqtt_usb.txt 2>mqtt_log.txt || { cat mqtt_log.txt; exit 1; }
grep "mqtt frames" mqtt_log.txt
# the server side: reassemble the frames as sglab does (duplicates dropped, gap -> RESEND)
"${PYTHON:-python3}" - <<'PY'
import struct
from sglab.framing import FrameReceiver
rx, out, raw = FrameReceiver(), [], open("frames_mqtt.bin", "rb").read()
i = 0
while i < len(raw):
    n, = struct.unpack_from("<I", raw, i); i += 4
    lines, _ = rx.feed(raw[i:i + n], now=i); i += n
    out += lines
open("out_mqtt.txt", "w", newline="").write("".join(out))
print("framing:", rx.stats)
assert rx.stats["gaps"] >= 1 and rx.stats["lost_bytes"] == 0 and rx.stats["bad_frames"] == 0
PY
"${PYTHON:-python3}" check_output.py
rm -f sim sim_int sim_uart sim_hr sim_slow sim_mqtt mqtt_log.txt frames_mqtt.bin out_mqtt_usb.txt
