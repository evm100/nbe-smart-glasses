#!/usr/bin/env bash
# Flash a prebuilt NBE node image from a Raspberry Pi (or any Linux box), then show its log.
#
#   ./flash.sh GLS01 [/dev/ttyACM0]
#
# Needs esptool + pyserial; the first run installs them into ~/.nbe-flash-venv.
# Board not found? Hold BOOT, plug in USB, release BOOT, run again.
set -euo pipefail
id=${1:?usage: flash.sh <NODE_ID> [port]}; id=${id^^}
port=${2:-$(ls /dev/ttyACM* 2>/dev/null | head -n 1)}
img="$(cd "$(dirname "$0")" && pwd)/$id.bin"
[[ -f "$img" ]] || { echo "no $img"; exit 1; }
[[ -n "$port" && -e "$port" ]] || { echo "no board found on /dev/ttyACM* - plug in the XIAO (or give the port)"; exit 1; }
[[ -r "$port" && -w "$port" ]] || { echo "no access to $port: sudo usermod -aG dialout \$USER, then log out/in"; exit 1; }

V="$HOME/.nbe-flash-venv"
if [[ ! -x "$V/bin/esptool" ]]; then
  python3 -m venv "$V" && "$V/bin/pip" install -q "esptool>=5" pyserial
fi
echo "flashing $id -> $port (erases the whole chip first)"
"$V/bin/esptool" --chip esp32s3 -p "$port" -b 460800 --before default-reset --after hard-reset \
    write-flash --erase-all 0x0 "$img"
echo
echo "log of $id (Wi-Fi / broker connection); Ctrl-] to quit. Look for 'got IP' and 'connected to broker'."
sleep 2
exec "$V/bin/python" -m serial.tools.miniterm --raw "$port" 115200
