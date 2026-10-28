#!/usr/bin/env bash
# Build ready-to-flash firmware for NBE Wi-Fi nodes on this server (ESP-IDF in Docker),
# so the Raspberry Pi only has to run esptool. Run as your normal user (needs docker).
#
#   deploy/evmserver/build-firmware.sh                  GLS01 HED01 FNG01
#   deploy/evmserver/build-firmware.sh FNG01            just one
#
# Once beforehand:
#   python3 firmware/tools/nbe_node.py login https://nbe.evm100.org      (admin account)
#   ~/.config/nbe-node/wifi.env  (chmod 600):
#       WIFI_SSID=UCSD-PROTECTED
#       WIFI_EAP_USER=<your UCSD login>
#       WIFI_PASSWORD='<your UCSD password>'
#       # WIFI_EAP_DOMAIN=<RADIUS cert name>     optional pin, see README
#       # WIFI2_SSID=<phone hotspot>  WIFI2_PASSWORD='...'   optional fallbacks, tried in order
#       # WIFI3_SSID=<home Wi-Fi>     WIFI3_PASSWORD='...'   when the one before isn't found
#
# Output: ~/nbe-firmware/out/<ID>.bin (contains the node key AND your Wi-Fi password - treat
# like a password) + flash.sh. Per-node build trees: ~/nbe-firmware/work/<ID>/.
# Each node's app image is also uploaded to the server, so it can be installed over Wi-Fi
# (Update on the Nodes page, or nbe_node.py ota <ID>). NBE_NO_UPLOAD=1 skips that.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PROJ="$REPO/firmware/esp-idf/ppg_stream"
TOOL="$REPO/firmware/tools/nbe_node.py"
BASE="${NBE_FW_DIR:-$HOME/nbe-firmware}"
IMAGE="${IDF_IMAGE:-espressif/idf:v6.1}"
WIFI_ENV="${NBE_WIFI_ENV:-$HOME/.config/nbe-node/wifi.env}"
NODES=("$@"); [[ ${#NODES[@]} -gt 0 ]] || NODES=(GLS01 HED01 FNG01)

[[ -f "$WIFI_ENV" ]] || { echo "missing $WIFI_ENV (see the header of this script)"; exit 1; }
[[ $(stat -c %a "$WIFI_ENV") == 600 ]] || { echo "chmod 600 $WIFI_ENV first"; exit 1; }
# shellcheck disable=SC1090
source "$WIFI_ENV"
: "${WIFI_SSID:?set WIFI_SSID in $WIFI_ENV}" "${WIFI_PASSWORD:?set WIFI_PASSWORD in $WIFI_ENV}"
eap=(); [[ -n "${WIFI_EAP_USER:-}" ]] && eap+=(--wifi-eap-user "$WIFI_EAP_USER")
[[ -n "${WIFI_EAP_DOMAIN:-}" ]] && eap+=(--wifi-eap-domain "$WIFI_EAP_DOMAIN")

# Runs the build in the background; on a terminal, draws a bar from ninja's "[step/total]" lines.
progress() {  # progress <pid> <log> <label>
  local pid=$1 log=$2 label=$3 n=0 total=0 pct w=30 last
  [[ -t 1 ]] || { wait "$pid"; return; }
  while kill -0 "$pid" 2>/dev/null; do
    last=$(grep -aoE '^\[[0-9]+/[0-9]+\]' "$log" 2>/dev/null | tail -n 1 | tr -d '[]')
    [[ -n "$last" ]] && { n=${last%/*}; total=${last#*/}; }
    if (( total > 0 )); then
      pct=$(( n * 100 / total ))
      printf '\r   %s [%-*s] %3d%%  %d/%d ' "$label" $w "$(printf '%*s' $(( pct * w / 100 )) '' | tr ' ' '#')" \
             "$pct" "$n" "$total"
    else
      printf '\r   %s [%-*s] preparing... ' "$label" $w ''
    fi
    sleep 0.5
  done
  printf '\r\033[K'
  wait "$pid"
}

umask 077
mkdir -p "$BASE/work" "$BASE/out"
i=0
for id in "${NODES[@]}"; do
  id=${id^^}; i=$((i + 1))
  w="$BASE/work/$id/ppg_stream"
  echo "== $id ($i/${#NODES[@]})"
  mkdir -p "$w"
  # two builds in one tree corrupt each other's build/ folder
  exec {lock}>"$BASE/work/$id/.lock"
  flock -n "$lock" || { echo "another build of $id is running - wait for it to finish"; exit 1; }
  # fresh sources each time; keep build/ + managed_components/ for fast rebuilds
  rsync -a --delete --exclude build/ --exclude managed_components/ --exclude 'host_test/' \
        --exclude sdkconfig --exclude sdkconfig.old --exclude sdkconfig.nbe --exclude 'main/certs/' \
        "$PROJ/" "$w/"
  rm -f "$w/sdkconfig"   # regenerate from sdkconfig.defaults + sdkconfig.nbe
  NBE_WIFI_PASSWORD="$WIFI_PASSWORD" NBE_WIFI2_PASSWORD="${WIFI2_PASSWORD:-}" NBE_WIFI3_PASSWORD="${WIFI3_PASSWORD:-}" \
      python3 "$TOOL" configure "$id" --project "$w" --wifi-ssid "$WIFI_SSID" "${eap[@]}" \
      --wifi2-ssid "${WIFI2_SSID:-}" --wifi3-ssid "${WIFI3_SSID:-}"
  : > "$BASE/work/$id/build.log"   # so the bar doesn't show the previous run
  # no `set-target`: it wipes build/ every time (sdkconfig.defaults already selects esp32s3)
  docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e IDF_TARGET=esp32s3 -v "$w:/project" -w /project "$IMAGE" \
      idf.py build merge-bin > "$BASE/work/$id/build.log" 2>&1 &
  progress $! "$BASE/work/$id/build.log" "building $id" \
      || { tail -n 40 "$BASE/work/$id/build.log"; echo "build of $id failed (full log: $BASE/work/$id/build.log)"; exit 1; }
  cp "$w/build/merged-binary.bin" "$BASE/out/$id.bin"
  grep -E '^CONFIG_PPG_(DEVICE_ID|SITE|WIFI_SSID[23]?|WIFI_AUTH_EAP|WIFI_EAP_USERNAME|MQTT_BROKER_URI)=' "$w/sdkconfig" | sed 's/^/   /'
  echo "   -> $BASE/out/$id.bin ($(stat -c %s "$BASE/out/$id.bin") bytes, USB flash)"
  if [[ -z "${NBE_NO_UPLOAD:-}" ]]; then
    python3 "$TOOL" upload "$id" "$w/build/ppg_stream.bin" | sed 's/^/   /' \
      || echo "   (upload failed - the USB image above is fine; retry: python3 $TOOL upload $id $w/build/ppg_stream.bin)"
  fi
  exec {lock}>&-
done
install -m 700 "$(dirname "$0")/flash.sh" "$BASE/out/flash.sh"
cat <<MSG

Boards already on firmware >= 0.4.0: press Update on https://nbe.evm100.org/admin/nodes - done.

First time (or a board that won't come online): copy to the Pi and flash (one board at a time):
  scp -r $(whoami)@<this server>:$BASE/out ~/nbe-fw        # on the Pi
  ~/nbe-fw/flash.sh GLS01                                   # then HED01, FNG01
MSG
