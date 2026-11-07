#!/usr/bin/env bash
# NBE Pi gateway: campus Wi-Fi (wlan0) -> NAT -> Ethernet (eth0) -> lab router -> ESP32 nodes.
# Raspberry Pi OS (Trixie / Bookworm) with NetworkManager. Re-runnable: run it again after
# editing gateway.conf.
#
#   sudo ./setup.sh                 install / update (reads ./gateway.conf, asks for what's missing)
#   sudo ./setup.sh -c other.conf   another config file
#   sudo ./setup.sh --uninstall     remove LAN sharing, firewall, services (keeps the uplink Wi-Fi)
#   sudo ./setup.sh --purge         --uninstall + delete the uplink Wi-Fi connections too
#
# Run it on the Pi's console, or over SSH on Wi-Fi (not over the Ethernet port: that port is
# turned into the lab side). An SSH drop doesn't stop it; output goes to /var/log/nbe-gw-setup.log.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ORIG_ARGS=("$@")
CONF="$HERE/gateway.conf"
MODE=install
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONF="$2"; shift 2 ;;
    --uninstall) MODE=uninstall; shift ;;
    --purge) MODE=purge; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || exec sudo bash "$0" "${ORIG_ARGS[@]}"

LIB=/usr/local/lib/nbe-gw
ETC=/etc/nbe-gw
UNITS="nbe-gw-watchdog.timer nbe-gw-watchdog.service nbe-gw-status.service nbe-gw-firewall.service"
UPLINK_IFACE=wlan0
LAN_IFACE=eth0
NMCONF=/etc/NetworkManager/conf.d/90-nbe-gw.conf
DNSMASQ_CONF=/etc/NetworkManager/dnsmasq-shared.d/nbe-gw.conf

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mxx %s\033[0m\n' "$*"; exit 1; }
online() { timeout 6 bash -c '</dev/tcp/1.1.1.1/443' 2>/dev/null; }

# ------------------------------------------------------------------ uninstall
if [[ $MODE != install ]]; then
  bold "removing NBE gateway"
  systemctl disable --now $UNITS 2>/dev/null || true
  rm -f /etc/systemd/system/nbe-gw-*.{service,timer}
  systemctl daemon-reload
  nft delete table inet nbe_gw 2>/dev/null || true
  nmcli con delete nbe-lan 2>/dev/null || true
  [[ $MODE == purge ]] && { nmcli con delete nbe-uplink 2>/dev/null || true; nmcli con delete nbe-uplink2 2>/dev/null || true; }
  rm -rf "$LIB" /usr/local/bin/nbe-gw "$NMCONF" "$DNSMASQ_CONF" /etc/sysctl.d/90-nbe-gw.conf /var/lib/nbe-gw
  [[ $MODE == purge ]] && rm -rf "$ETC"
  systemctl restart NetworkManager
  echo "done. eth0 falls back to an ordinary DHCP client on the next cable plug-in."
  exit 0
fi

# ------------------------------------------------------------------ config
if [[ ! -f $CONF && -f $ETC/gateway.conf ]]; then CONF=$ETC/gateway.conf; fi
if [[ -f $CONF ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
  echo "config: $CONF"
else
  warn "no gateway.conf (copy gateway.conf.example) - asking instead"
fi
: "${UPLINK_SSID:=}" "${UPLINK_AUTH:=psk}" "${UPLINK_PASSWORD:=}" "${UPLINK_USER:=}"
: "${UPLINK_PEAP_VALIDATE:=yes}" "${UPLINK_PEAP_DOMAIN:=}" "${UPLINK_BAND:=}"
: "${UPLINK2_SSID:=}" "${UPLINK2_PASSWORD:=}" "${LAN_ADDR:=192.168.77.1/24}" "${WIFI_COUNTRY:=US}"
: "${CHECK_HOST:=nbe.evm100.org}" "${CHECK_PORT:=8884}" "${ALLOW_SSH_FROM_UPLINK:=no}"
: "${STATUS_PORT:=80}" "${REBOOT_AFTER_MIN:=30}"

[[ -e /sys/class/net/$UPLINK_IFACE ]] || die "no $UPLINK_IFACE"
[[ -e /sys/class/net/$LAN_IFACE ]] || die "no $LAN_IFACE"
command -v nmcli >/dev/null || die "NetworkManager (nmcli) not found - this needs Raspberry Pi OS Bookworm or newer"
systemctl -q is-active NetworkManager || die "NetworkManager isn't running"

echo "Pi Wi-Fi MAC (register this for UCSD-DEVICE): $(cat /sys/class/net/$UPLINK_IFACE/address)"
if [[ -z $UPLINK_SSID ]]; then read -rp "Campus Wi-Fi SSID [UCSD-DEVICE]: " UPLINK_SSID; UPLINK_SSID=${UPLINK_SSID:-UCSD-DEVICE}; fi
case $UPLINK_AUTH in psk|peap|open) ;; *) die "UPLINK_AUTH must be psk, peap or open";; esac
if [[ $UPLINK_AUTH == peap && -z $UPLINK_USER ]]; then read -rp "Username for $UPLINK_SSID: " UPLINK_USER; fi
if [[ $UPLINK_AUTH != open && -z $UPLINK_PASSWORD ]] && nmcli -t -f NAME con show | grep -qx nbe-uplink; then
  # re-run: reuse the password NetworkManager already has
  UPLINK_PASSWORD=$(nmcli -s -g 802-11-wireless-security.psk con show nbe-uplink 2>/dev/null || true)
  if [[ $UPLINK_AUTH == peap ]]; then UPLINK_PASSWORD=$(nmcli -s -g 802-1x.password con show nbe-uplink 2>/dev/null || true); fi
fi
if [[ $UPLINK_AUTH != open && -z $UPLINK_PASSWORD ]]; then
  read -rsp "Password for $UPLINK_SSID: " UPLINK_PASSWORD; echo
  [[ -n $UPLINK_PASSWORD ]] || die "password needed (or set UPLINK_AUTH=open)"
fi
LAN_IP=${LAN_ADDR%/*}
python3 -c "import ipaddress,sys; n=ipaddress.ip_interface(sys.argv[1]); assert n.network.prefixlen<=29" "$LAN_ADDR" \
  || die "LAN_ADDR must look like 192.168.77.1/24"

# Everything below runs unattended: survive an SSH drop, keep a log.
LOG=/var/log/nbe-gw-setup.log
trap '' HUP
exec > >(tee -i --output-error=warn-nopipe -a "$LOG") 2>&1
echo "---- $(date) setup.sh"

# ------------------------------------------------------------------ radio
bold "Wi-Fi country $WIFI_COUNTRY"
if command -v raspi-config >/dev/null; then raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
else iw reg set "$WIFI_COUNTRY" 2>/dev/null || true; fi
rfkill unblock wifi 2>/dev/null || true
nmcli radio wifi on

# ------------------------------------------------------------------ NetworkManager config
bold "NetworkManager config"
mkdir -p "$(dirname "$NMCONF")" "$(dirname "$DNSMASQ_CONF")"
NEW_NMCONF=$(cat <<'EOF'
# NBE gateway (setup.sh): NAT rules for the shared eth0 via nftables
[main]
firewall-backend=nftables
EOF
)
if [[ "$(cat "$NMCONF" 2>/dev/null)" != "$NEW_NMCONF" ]]; then
  echo "$NEW_NMCONF" > "$NMCONF"
  echo "restarting NetworkManager (Wi-Fi drops for a few seconds)"
  systemctl restart NetworkManager
  sleep 5
fi
# extra options for NM's dnsmasq on the lab side (DHCP + DNS forwarder)
cat > "$DNSMASQ_CONF" <<EOF
# NBE gateway (setup.sh)
dhcp-authoritative
domain-needed
EOF

# ------------------------------------------------------------------ uplink
bold "uplink: $UPLINK_SSID ($UPLINK_AUTH) on $UPLINK_IFACE"
# cloned-mac permanent: always the real MAC (the one registered for UCSD-DEVICE); powersave 2 = off
UP_ARGS=(connection.autoconnect yes connection.autoconnect-priority 100 connection.autoconnect-retries 0
  802-11-wireless.powersave 2 802-11-wireless.cloned-mac-address permanent
  ipv4.method auto ipv4.route-metric 100 ipv6.method auto)
[[ -n $UPLINK_BAND ]] && UP_ARGS+=(802-11-wireless.band "$UPLINK_BAND")
case $UPLINK_AUTH in
  psk)  UP_ARGS+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$UPLINK_PASSWORD") ;;
  peap) UP_ARGS+=(wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2
          802-1x.identity "$UPLINK_USER" 802-1x.anonymous-identity anonymous 802-1x.password "$UPLINK_PASSWORD")
        if [[ $UPLINK_PEAP_VALIDATE == yes ]]; then
          UP_ARGS+=(802-1x.system-ca-certs yes)
          [[ -n $UPLINK_PEAP_DOMAIN ]] && UP_ARGS+=(802-1x.domain-suffix-match "$UPLINK_PEAP_DOMAIN")
        fi ;;
esac
# re-created every run so no setting from an older run (or another auth type) lingers
nmcli con delete nbe-uplink >/dev/null 2>&1 || true
nmcli con add type wifi ifname "$UPLINK_IFACE" con-name nbe-uplink ssid "$UPLINK_SSID" "${UP_ARGS[@]}" >/dev/null

if [[ -n $UPLINK2_SSID ]]; then
  echo "fallback uplink: $UPLINK2_SSID"
  nmcli con delete nbe-uplink2 >/dev/null 2>&1 || true
  UP2=(connection.autoconnect yes connection.autoconnect-priority 50 connection.autoconnect-retries 0
    802-11-wireless.powersave 2 ipv4.method auto ipv4.route-metric 200 ipv6.method auto)
  [[ -n $UPLINK2_PASSWORD ]] && UP2+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$UPLINK2_PASSWORD")
  nmcli con add type wifi ifname "$UPLINK_IFACE" con-name nbe-uplink2 ssid "$UPLINK2_SSID" "${UP2[@]}" >/dev/null
else
  nmcli con delete nbe-uplink2 >/dev/null 2>&1 || true
fi

echo "connecting (up to 60 s)..."
if ! nmcli --wait 60 con up nbe-uplink; then
  warn "couldn't join $UPLINK_SSID"
  if [[ -n $UPLINK2_SSID ]]; then
    echo "trying the fallback $UPLINK2_SSID"
    nmcli --wait 60 con up nbe-uplink2 || warn "fallback didn't connect either"
  fi
fi
for _ in $(seq 20); do online && break; sleep 2; done
if online; then
  echo "online via $(nmcli -g GENERAL.CONNECTION dev show "$UPLINK_IFACE")"
  ONLINE=1
else
  ONLINE=0
  warn "no internet on the uplink yet. Common causes:
     - UCSD-DEVICE: the MAC above isn't registered yet, or registration hasn't activated (can take a while)
     - wrong password / username;  see: journalctl -u NetworkManager -n 60
   Setup continues; the watchdog keeps retrying once installed."
fi

# subnet clash check (campus side vs our lab side)
UP_NET=$(nmcli -g IP4.ADDRESS dev show "$UPLINK_IFACE" 2>/dev/null | head -1 | cut -d'|' -f1 | tr -d ' ')
if [[ -n $UP_NET ]] && python3 -c "import ipaddress,sys; a=ipaddress.ip_interface(sys.argv[1]).network; b=ipaddress.ip_interface(sys.argv[2]).network; sys.exit(0 if a.overlaps(b) else 1)" "$UP_NET" "$LAN_ADDR"; then
  die "LAN_ADDR $LAN_ADDR overlaps the campus subnet $UP_NET - pick another (e.g. 192.168.123.1/24) and re-run"
fi

# ------------------------------------------------------------------ packages
bold "packages"
NEED=()
command -v nft >/dev/null || NEED+=(nftables)
[[ -x /usr/sbin/dnsmasq ]] || NEED+=(dnsmasq-base)
command -v iw >/dev/null || NEED+=(iw)
command -v python3 >/dev/null || NEED+=(python3)
if ((${#NEED[@]})); then
  if ((ONLINE)); then
    apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${NEED[@]}"
  else
    die "missing ${NEED[*]} and no internet to install them. Get the Pi online once (phone hotspot as
   UPLINK2_SSID, or home Wi-Fi) and re-run, or beforehand: sudo apt install -y ${NEED[*]}"
  fi
else
  echo "nftables, dnsmasq-base, iw, python3: present"
fi
if systemctl -q is-active dnsmasq 2>/dev/null; then
  warn "a system-wide dnsmasq service is running; it clashes with NetworkManager's - disabling it"
  systemctl disable --now dnsmasq
fi

# ------------------------------------------------------------------ app + firewall
bold "nbe-gw app, firewall, services"
install -d -m 755 "$LIB" "$ETC" /var/lib/nbe-gw
install -m 755 "$HERE/nbe_gw.py" "$LIB/nbe_gw.py"
ln -sf "$LIB/nbe_gw.py" /usr/local/bin/nbe-gw
# secrets: root-only copy of the config, for re-runs on the Pi
if [[ "$(readlink -f "$CONF" 2>/dev/null)" != "$ETC/gateway.conf" && -f $CONF ]]; then
  install -m 600 "$CONF" "$ETC/gateway.conf"
fi
cat > "$ETC/runtime.conf" <<EOF
# written by setup.sh - non-secret settings read by nbe-gw
UPLINK_IFACE=$UPLINK_IFACE
LAN_IFACE=$LAN_IFACE
LAN_ADDR=$LAN_ADDR
UPLINK_SSID=$UPLINK_SSID
UPLINK2_SSID=$UPLINK2_SSID
CHECK_HOST=$CHECK_HOST
CHECK_PORT=$CHECK_PORT
STATUS_PORT=$STATUS_PORT
REBOOT_AFTER_MIN=$REBOOT_AFTER_MIN
EOF
chmod 644 "$ETC/runtime.conf"

SSH_RULE=""
[[ $ALLOW_SSH_FROM_UPLINK == yes ]] && SSH_RULE="tcp dport 22 accept"
cat > "$ETC/firewall.nft" <<EOF
# NBE gateway (setup.sh). NetworkManager adds the NAT/forward rules for $LAN_IFACE itself
# (table nm-shared-$LAN_IFACE); this table only shuts the campus side out.
table inet nbe_gw {}
delete table inet nbe_gw
table inet nbe_gw {
  chain input {
    type filter hook input priority filter + 10; policy accept;
    iifname != "$UPLINK_IFACE" accept
    ct state established,related accept
    ct state invalid drop
    meta l4proto { icmp, ipv6-icmp } accept
    udp dport { 68, 546 } accept   # DHCP / DHCPv6 client on the uplink
    $SSH_RULE
    counter drop
  }
  chain forward {
    type filter hook forward priority filter + 10; policy accept;
    iifname "$UPLINK_IFACE" ct state new counter drop   # nothing from campus into the lab network
  }
}
EOF
nft -c -f "$ETC/firewall.nft" || die "firewall rules don't parse"
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/90-nbe-gw.conf
sysctl -q -p /etc/sysctl.d/90-nbe-gw.conf

install -m 644 "$HERE"/systemd/nbe-gw-*.service "$HERE"/systemd/nbe-gw-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nbe-gw-firewall.service
systemctl restart nbe-gw-firewall.service
systemctl enable --now nbe-gw-watchdog.timer
if [[ $STATUS_PORT != 0 ]]; then
  systemctl enable nbe-gw-status.service && systemctl restart nbe-gw-status.service
else
  systemctl disable --now nbe-gw-status.service 2>/dev/null || true
fi
systemctl enable --now ssh 2>/dev/null || true

# ------------------------------------------------------------------ LAN side
bold "lab side: $LAN_IFACE = $LAN_ADDR, DHCP + NAT"
LAN_ARGS=(connection.autoconnect yes connection.autoconnect-priority 100 connection.autoconnect-retries 0
  ipv4.method shared ipv4.addresses "$LAN_ADDR" ipv6.method disabled)
if nmcli -t -f NAME con show | grep -qx nbe-lan; then
  nmcli con modify nbe-lan "${LAN_ARGS[@]}"
else
  nmcli con add type ethernet ifname "$LAN_IFACE" con-name nbe-lan "${LAN_ARGS[@]}" >/dev/null
fi
if [[ "$(cat /sys/class/net/$LAN_IFACE/carrier 2>/dev/null)" == 1 ]]; then
  nmcli --wait 20 con up nbe-lan || warn "couldn't start nbe-lan now; the watchdog retries every minute"
else
  echo "no cable in $LAN_IFACE yet - sharing starts by itself when you plug the router in"
fi

# ------------------------------------------------------------------ done
bold "done"
nbe-gw status || true
cat <<EOF

Next:
  1. Router: see README.md (access-point mode -> Pi into a LAN port; or router mode -> Pi into WAN).
  2. Nodes: python firmware/tools/nbe_node.py configure <ID> --wifi-ssid <router SSID>  (then build + flash)
  3. From a laptop on the router's Wi-Fi:  ssh ${SUDO_USER:-pi}@$LAN_IP
     $( [[ $STATUS_PORT != 0 ]] && echo "status page: http://$LAN_IP$( [[ $STATUS_PORT != 80 ]] && echo ":$STATUS_PORT")/" )
  Check any time:  nbe-gw status | sudo nbe-gw test | nbe-gw clients | sudo nbe-gw scan
  Log of this run: $LOG
EOF
