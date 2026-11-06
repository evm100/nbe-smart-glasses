#!/usr/bin/env bash
# NBE MQTT on :443 too (for USB gateways on networks that block :8884). Run on evmserver; re-runnable.
#
#   sudo deploy/evmserver/enable-mqtt-443.sh
#
# nginx's stream module takes over :443 and routes on the TLS ALPN, without decrypting:
#   ALPN "mqtt" (the USB gateway sends it) -> mosquitto :8884, client certs checked by mosquitto as before
#   anything else                          -> the websites, now on 127.0.0.1:8443 with PROXY protocol
# This edits the "listen 443" lines of EVERY enabled site (UltraLights, tdml.org, ... too):
#   listen 443 ssl ...;      ->  listen 127.0.0.1:8443 ssl ... proxy_protocol;
#   listen [::]:443 ssl ...; ->  commented out (the stream router listens on [::]:443)
# /etc/nginx is backed up first and restored if nginx -t or the restart fails.
# ESP32 boards keep using :8884 directly. Undo: restore the backup tarball printed below, restart nginx.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
cd /
REPO="$(cd "$HERE/../.." && pwd)"
DOMAIN=nbe.evm100.org
GW_URI="mqtts://$DOMAIN:443"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "preflight"
for p in 8443 8885; do
  ss -tlnp | grep -E "127\.0\.0\.1:$p\s" | grep -qv nginx && { echo "port $p is taken by something else"; exit 1; }
done
systemctl -q is-active mosquitto || { echo "mosquitto isn't running (setup-root.sh first)"; exit 1; }
dpkg -s libnginx-mod-stream >/dev/null 2>&1 || apt-get install -y libnginx-mod-stream

BACKUP=/root/nginx-backup-$(date +%Y%m%d-%H%M%S).tgz
tar czf "$BACKUP" -C /etc nginx
echo "backup: $BACKUP"
restore() {
  echo "!! $1 - restoring $BACKUP"
  rm -rf /etc/nginx && tar xzf "$BACKUP" -C /etc
  systemctl restart nginx || true
  exit 1
}

step "stream router + realip"
install -m 644 "$HERE/nginx-stream-443.conf" /etc/nginx/nbe-stream.conf
install -m 644 "$HERE/nginx-proxy-protocol.conf" /etc/nginx/conf.d/nbe-proxy-protocol.conf
if ! grep -q '^include /etc/nginx/nbe-stream.conf;' /etc/nginx/nginx.conf; then
  # main context, after the load_module lines (modules-enabled) so the stream module is loaded
  sed -i '\#^include /etc/nginx/modules-enabled/\*\.conf;#a include /etc/nginx/nbe-stream.conf;' /etc/nginx/nginx.conf
  grep -q '^include /etc/nginx/nbe-stream.conf;' /etc/nginx/nginx.conf || restore "no modules-enabled include in nginx.conf"
fi

step "sites: listen 443 -> 127.0.0.1:8443 proxy_protocol"
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [[ -f $f ]] || continue
  real=$(readlink -f "$f")
  tmp=$(mktemp)
  sed -E \
    -e 's/^([[:space:]]*)listen[[:space:]]+443(([[:space:]]+[^;#]*[^;#[:space:]])?)[[:space:]]*;/\1listen 127.0.0.1:8443\2 proxy_protocol;/' \
    -e 's/^([[:space:]]*)(listen[[:space:]]+\[::\]:443([[:space:]]|;).*)$/\1# via the :443 stream router (nbe-stream.conf): \2/' \
    "$real" > "$tmp"
  if ! cmp -s "$real" "$tmp"; then
    cat "$tmp" > "$real"   # in place: keeps symlinks and hard links (sites-available copies) intact
    echo "$f:"; grep -nE 'listen .*8443|stream router' "$real" | sed 's/^/    /'
  fi
  rm -f "$tmp"
done
left=$(grep -lE '^[[:space:]]*listen[[:space:]]+(\[::\]:|0\.0\.0\.0:|\*:)?443\b' /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null || true)
[[ -z $left ]] || restore "still listening on 443 directly: $left"

step "nginx -t + restart"
nginx -t || restore "nginx -t failed"
systemctl restart nginx || restore "nginx didn't restart"   # restart, not reload: :443 moves from http to stream
sleep 1
ss -tlnp | grep -E ':(443|8443|8885|8884)\s'

step "check"
code=$(curl -sS -o /dev/null -w '%{http_code}' "https://$DOMAIN/login" || true)
echo "https://$DOMAIN/login via :443 -> HTTP $code"
for s in $(grep -h '^[[:space:]]*server_name' /etc/nginx/sites-enabled/* | sed -E 's/^[[:space:]]*server_name[[:space:]]+//; s/;.*//' | tr ' ' '\n' | sort -u); do
  [[ $s == "$DOMAIN" || $s == _ ]] && continue
  printf '  %-24s HTTP %s\n' "$s" "$(curl -sS -o /dev/null -w '%{http_code}' --resolve "$s:443:127.0.0.1" "https://$s/" 2>/dev/null || echo ERR)"
done
sudo -u nbe /opt/nbe/venv/bin/python - "$DOMAIN" <<'PY' || { echo "!! MQTT over :443 failed - see journalctl -u mosquitto, /var/log/nginx/nbe-mqtt-443.log"; exit 1; }
import ssl, sys, threading
import paho.mqtt.client as mqtt
host, done, res = sys.argv[1], threading.Event(), {}
ctx = ssl.create_default_context()
ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
ctx.load_cert_chain("/srv/nbe/pki/core.crt", "/srv/nbe/pki/core.key")
ctx.set_alpn_protocols(["mqtt"])
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nbe-443-check")  # not nbe-core: don't kick the server off
c.tls_set_context(ctx)
c.on_connect = lambda cl, u, f, rc, p=None: (res.update(rc=rc), done.set())
c.connect(host, 443, keepalive=10)
c.loop_start(); done.wait(10); c.disconnect(); c.loop_stop()
rc = res.get("rc")
print(f"mqtts://{host}:443 (ALPN mqtt) -> {rc}")
sys.exit(0 if rc is not None and not rc.is_failure else 1)
PY

step "gateway downloads -> $GW_URI"
"$REPO/deploy/install.sh" >/dev/null   # code that knows [gateway] broker_uri + sends ALPN
T=/etc/nbe/server.toml
if sed -n '/^\[gateway\]/,/^\[/p' $T | grep -q '^broker_uri'; then
  sed -i "/^\[gateway\]/,/^\[/ s|^broker_uri *=.*|broker_uri = \"$GW_URI\"   # :443 via nginx ALPN routing (enable-mqtt-443.sh)|" $T
elif grep -q '^\[gateway\]' $T; then
  sed -i "/^\[gateway\]/a broker_uri = \"$GW_URI\"   # :443 via nginx ALPN routing (enable-mqtt-443.sh)" $T
else
  printf '\n[gateway]\nbroker_uri = "%s"   # :443 via nginx ALPN routing (enable-mqtt-443.sh)\n' "$GW_URI" >> $T
fi
sed -n '/^\[gateway\]/,/^$/p' $T
systemctl restart nbe-lab

cat <<MSG

Done. "Download gateway" on the Nodes page now hands out $GW_URI. Existing gateway folders still
say 8884 in gateway.json: send the operator a fresh download (or edit "broker_port": 443 there; the
gateway also needs this version's code for the ALPN). ESP32 boards stay on :8884.
Nothing to change on the router or ufw: 443 already comes here.
MSG
