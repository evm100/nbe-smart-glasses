#!/usr/bin/env bash
# Flash a prebuilt NBE node image over USB (macOS, Linux, Raspberry Pi), then show its log.
#
#   ./flash.sh GLS01 [port]          the image GLS01.bin must be next to this script
#
# Needs python3; the first run installs esptool + pyserial into ~/.nbe-flash-venv.
# Board not found? Hold BOOT, plug in USB, release BOOT, run again.
# (Works with macOS's bash 3.2: no ${x^^}, no GNU-only tools.)
set -euo pipefail
id=$(printf '%s' "${1:?usage: flash.sh <NODE_ID> [port]}" | tr '[:lower:]' '[:upper:]')
here="$(cd "$(dirname "$0")" && pwd)"
img="$here/$id.bin"
[[ -f "$img" ]] || { echo "no $img - put $id.bin next to flash.sh"; exit 1; }

find_port() {  # first USB serial port of an ESP32 board
  local p
  for p in /dev/cu.usbmodem* /dev/ttyACM* /dev/cu.usbserial* /dev/ttyUSB*; do
    [[ -e "$p" ]] && { echo "$p"; return 0; }
  done
  return 0  # nothing found: empty output (a non-zero status would end the script under set -e)
}
port=${2:-$(find_port)}
[[ -n "$port" && -e "$port" ]] || {
  echo "no board found - plug in the XIAO with a data USB-C cable (or give the port: ./flash.sh $id /dev/...)"
  echo "a new board that never shows up: hold BOOT, plug it in, release BOOT, run this again"
  exit 1; }
if [[ "$(uname)" == Linux ]] && ! { [[ -r "$port" ]] && [[ -w "$port" ]]; }; then
  echo "no access to $port: sudo usermod -aG dialout \$USER, then log out/in"; exit 1
fi

command -v python3 >/dev/null || { echo "python3 is missing (macOS: run 'xcode-select --install' first)"; exit 1; }
V="$HOME/.nbe-flash-venv"
if [[ ! -x "$V/bin/esptool" ]]; then
  echo "first run: installing esptool into $V ..."
  python3 -m venv "$V" && "$V/bin/pip" install -q "esptool>=5" pyserial
fi
echo "flashing $id -> $port (erases the whole chip first)"
"$V/bin/esptool" --chip esp32s3 -p "$port" -b 460800 --before default-reset --after hard-reset \
    write-flash --erase-all 0x0 "$img"
echo
if LC_ALL=C grep -aq ppg_camera "$img"; then  # the camera task only exists in camera builds
  # camera board: its USB port carries binary data for the NBE USB gateway, not a readable log
  echo "done. $id is a camera board: its USB port now talks to the NBE USB gateway."
  echo "Next: download the gateway for $id from the NBE server (Nodes page -> USB gateway), unzip it,"
  echo "start it, and keep the glasses plugged in. $id shows as online on the website when it works."
  exit 0
fi
echo "log of $id (Wi-Fi / broker connection); Ctrl-] to quit. Look for 'got IP' and 'connected to broker'."
sleep 2
exec "$V/bin/python" -m serial.tools.miniterm --raw "$port" 115200
