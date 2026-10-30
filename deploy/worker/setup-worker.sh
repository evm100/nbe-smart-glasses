#!/usr/bin/env bash
# Set up (or update) the NBE processing worker + firmware build host inside the Debian 13 LXC.
# Run as root from a copy of the repo; re-run after copying in new code.
#
#   deploy/worker/setup-worker.sh
#
# Env: NBE_SERVER (default https://nbe.evm100.org), WORKERS (parallel jobs, default 2),
#      BUILD_USER (account that runs build-firmware.sh, default evm100; added to the docker group),
#      NBE_WORKER_TOKEN (token from deploy/evmserver/enable-remote-processing.sh; asked for if unset).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
DEST=/opt/nbe/smart-glasses
SERVER=${NBE_SERVER:-https://nbe.evm100.org}
WORKERS=${WORKERS:-2}
BUILD_USER=${BUILD_USER:-evm100}
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "packages (Python, Docker for ESP-IDF builds)"
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip rsync git curl ca-certificates \
  docker.io sudo >/dev/null
systemctl enable --now docker >/dev/null

step "worker code + venv (/opt/nbe)"
id nbe-worker >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin nbe-worker
install -d -o root -g root -m 755 /opt/nbe
if [[ "$SRC" != "$DEST" ]]; then
  rsync -a --delete --exclude data/ --exclude .venv/ --exclude 'firmware/esp-idf/*/build/' "$SRC/" "$DEST/"
fi
chown -R root:root "$DEST"
PYVER=$(python3 -c 'import sys; print("python%d.%d" % sys.version_info[:2])')
[[ -d /opt/nbe/venv/lib/$PYVER ]] || rm -rf /opt/nbe/venv
[[ -d /opt/nbe/venv ]] || python3 -m venv /opt/nbe/venv
/opt/nbe/venv/bin/pip install -q --upgrade pip
/opt/nbe/venv/bin/pip install -q "$DEST[model]"     # the pipeline + Layer-4 models, no web/MQTT stack
echo "ppg $(/opt/nbe/venv/bin/python -c 'import ppg; print(ppg.__version__)')"

step "server $SERVER"
if curl -fsS --max-time 10 "$SERVER/healthz" >/dev/null; then echo "reachable"; else
  host=${SERVER#https://}; host=${host%%/*}
  cat <<MSG
cannot reach $SERVER/healthz from here.
If this box is on the same LAN as evmserver and the router doesn't hairpin, pin the name to the LAN IP:
  echo "192.168.1.220 $host" >> /etc/hosts && touch /etc/.pve-ignore.hosts   # Proxmox would rewrite it otherwise
then run this script again.
MSG
  exit 1
fi

step "token + config (/etc/nbe-worker)"
install -d -o root -g root -m 755 /etc/nbe-worker
printf 'NBE_SERVER=%s\n' "$SERVER" > /etc/nbe-worker/worker.env
if [[ ! -s /etc/nbe-worker/token || -n "${NBE_WORKER_TOKEN:-}" ]]; then
  tok=${NBE_WORKER_TOKEN:-}
  [[ -n "$tok" ]] || read -rsp "worker token (nbe_..., from enable-remote-processing.sh on evmserver): " tok; echo
  [[ "$tok" == nbe_* ]] || { echo "that doesn't look like an nbe_ token"; exit 1; }
  install -m 600 /dev/null /etc/nbe-worker/token
  printf '%s\n' "$tok" > /etc/nbe-worker/token
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cat /etc/nbe-worker/token)" \
       "$SERVER/api/worker/ping")
case $code in
  200) echo "token OK" ;;
  401) echo "token rejected (401)"; exit 1 ;;
  403) echo "that token's user lacks the worker role (403)"; exit 1 ;;
  409) echo "server still processes locally: run deploy/evmserver/enable-remote-processing.sh there (409)"; exit 1 ;;
  *)   echo "unexpected HTTP $code from /api/worker/ping"; exit 1 ;;
esac

step "systemd: $WORKERS worker(s)"
install -m 644 "$DEST/deploy/worker/nbe-worker@.service" /etc/systemd/system/
systemctl daemon-reload
for u in $(systemctl list-units --plain --no-legend 'nbe-worker@*' | awk '{print $1}'); do
  n=${u#nbe-worker@}; n=${n%.service}
  (( n > WORKERS )) && systemctl disable --now "$u"
done
for i in $(seq 1 "$WORKERS"); do systemctl enable nbe-worker@"$i" >/dev/null; systemctl restart nbe-worker@"$i"; done
sleep 3
systemctl --no-pager --lines=3 status 'nbe-worker@*' | grep -E '●|Active|worker ' || true

step "firmware builds: user $BUILD_USER"
if ! id "$BUILD_USER" >/dev/null 2>&1; then
  adduser --gecos "" "$BUILD_USER"
fi
usermod -aG docker "$BUILD_USER"
echo "$BUILD_USER is in the docker group (log in again for it to apply)"
docker image inspect espressif/idf:v6.1 >/dev/null 2>&1 || { echo "pulling espressif/idf:v6.1 (~5 GB)..."; docker pull -q espressif/idf:v6.1; }

cat <<MSG

Done. Processing jobs from $SERVER now run here ($WORKERS at a time):
  journalctl -fu 'nbe-worker@*'
Firmware: as $BUILD_USER, follow deploy/worker/README.md §3 (copy the repo, nbe_node.py login, wifi.env).
MSG
