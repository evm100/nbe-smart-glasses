#!/usr/bin/env bash
# One-shot (and re-runnable) NBE deployment on evmserver, next to UltraLights.
#
#   sudo deploy/evmserver/setup-root.sh
#
# web    https://nbe.evm100.org  -> nginx -> sglab on 127.0.0.1:8001
# nodes  mqtts://nbe.evm100.org:8884 -> host mosquitto (mTLS, NBE Device CA)
# Leaves UltraLights alone (uvicorn :8000, Docker mosquitto :1883, its nginx sites).
# Every step checks first, so running it again only does what is missing.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
cd /   # the nbe user can't read your home directory
REPO="$(cd "$HERE/../.." && pwd)"
DOMAIN=nbe.evm100.org
ADMIN=${NBE_ADMIN:-evm100}
NODES=("GLS01 glasses Glasses" "HED01 head Head" "FNG01 finger Finger")
nbe() { sudo -u nbe SGLAB_ROOT=/opt/nbe/smart-glasses SGLAB_SERVER_CONFIG=/etc/nbe/server.toml MPLCONFIGDIR=/tmp/matplotlib \
          /opt/nbe/venv/bin/sglab "$@"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "preflight"
ss -tln | grep -qE ':8001\s' && ! systemctl -q is-active nbe-lab && { echo "port 8001 is taken by something else"; exit 1; }
ss -tln | grep -qE ':8884\s' && ! systemctl -q is-active mosquitto && { echo "port 8884 is taken by something else"; exit 1; }
echo "DNS $DOMAIN -> $(dig +short A $DOMAIN | tr '\n' ' ')"

step "core: user nbe, /opt/nbe, /srv/nbe, venv, systemd units"
"$REPO/deploy/install.sh"
if [[ ! -s /etc/nbe/server.toml ]] || cmp -s /etc/nbe/server.toml /opt/nbe/smart-glasses/deploy/server.example.toml; then
  install -o root -g nbe -m 640 "$HERE/server.toml" /etc/nbe/server.toml
  echo "installed /etc/nbe/server.toml"
else
  echo "/etc/nbe/server.toml exists and was edited - left as is (compare with $HERE/server.toml)"
fi
grep -qE "^127\.0\.0\.1\s+$DOMAIN\b" /etc/hosts || { echo "127.0.0.1 $DOMAIN" >> /etc/hosts; echo "pinned $DOMAIN to 127.0.0.1 in /etc/hosts"; }

step "device CA + server certificate"
[[ -f /srv/nbe/pki/ca.key ]] || nbe pki init
[[ -f /srv/nbe/pki/core.crt ]] || nbe pki core-cert

step "admin account '$ADMIN'"
if nbe user list | grep -qw "$ADMIN"; then echo "exists"; else
  echo "choose the web password for $ADMIN:"; nbe user add "$ADMIN" --role admin --display "$ADMIN"; fi

step "register the three nodes"
for n in "${NODES[@]}"; do
  read -r id site label <<<"$n"
  if nbe node list | grep -qw "$id"; then echo "$id exists"; else nbe node register "$id" --site "$site" --name "$label"; fi
done

step "TLS certificate for $DOMAIN (Let's Encrypt)"
if [[ ! -d /etc/letsencrypt/live/$DOMAIN ]]; then
  certbot certonly --nginx -d "$DOMAIN" --keep-until-expiring
fi
install -m 755 "$HERE/certbot-deploy-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/nbe-mosquitto.sh

step "mosquitto (host service, :8884 only)"
install -d -m 755 /etc/mosquitto/certs
install -m 644 /srv/nbe/pki/ca.crt /etc/mosquitto/certs/nbe-device-ca.crt
install -m 644 "$HERE/mosquitto-nbe.conf" /etc/mosquitto/conf.d/nbe.conf
install -o root -g mosquitto -m 640 "$REPO/deploy/mosquitto/nbe-acl" /etc/mosquitto/nbe-acl
RENEWED_DOMAINS=$DOMAIN /etc/letsencrypt/renewal-hooks/deploy/nbe-mosquitto.sh   # copies cert + restarts
systemctl enable mosquitto
sleep 1; systemctl -q is-active mosquitto || { journalctl -u mosquitto -n 30 --no-pager; exit 1; }
ss -tln | grep -E ':(8884|1883)\s' || true

step "nginx"
install -m 644 "$REPO/deploy/nginx/nbe-ratelimit.conf" /etc/nginx/conf.d/nbe-ratelimit.conf
install -m 644 "$HERE/nginx-nbe.conf" /etc/nginx/sites-available/$DOMAIN
ln -sf ../sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t && systemctl reload nginx

step "firewall"
if ufw status | grep -q "Status: active"; then ufw allow 8884/tcp comment "NBE MQTT nodes"; else echo "ufw inactive - nothing to do"; fi

step "start sglab"
systemctl enable nbe-snapshot.timer >/dev/null
systemctl enable nbe-lab >/dev/null
systemctl restart nbe-lab
sleep 4
nbe doctor || true
cat <<MSG

Done. Open https://$DOMAIN and log in as $ADMIN.
Still needed outside this box: router port-forward TCP 8884 -> $(hostname -I | awk '{print $1}') (80/443 already go here).
MSG
