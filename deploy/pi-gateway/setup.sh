#!/usr/bin/env bash
# NBE Pi gateway: shares the Pi's existing Wi-Fi connection (wlan0) with the lab router on its
# Ethernet port (eth0): DHCP + DNS + NAT on eth0, firewall on wlan0. The Wi-Fi itself is NOT
# touched: connect the Pi to whatever network you like first (raspi-config / nmtui / Imager).
# Raspberry Pi OS (Trixie / Bookworm) with NetworkManager. Re-runnable.
#
#   sudo ./setup.sh                 install / update (reads ./gateway.conf if there)
#   sudo ./setup.sh -c other.conf   another config file
#   sudo ./setup.sh --uninstall     remove LAN sharing, firewall, services
#
# Run it over SSH on Wi-Fi or on the console, NOT over the Ethernet port (that port becomes the lab side).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ORIG_ARGS=("$@")
CONF="$HERE/gateway.conf"
MODE=install
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONF="$2"; shift 2 ;;
    --uninstall|--purge) MODE=uninstall; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || exec sudo bash "$0" "${ORIG_ARGS[@]}"

LIB=/usr/local/lib/nbe-gw
ETC=/etc/nbe-gw
UNITS="nbe-gw-watchdog.timer nbe-gw-watchdog.service nbe-gw-status.service nbe-gw-firewall.service"
NMCONF=/etc/NetworkManager/conf.d/90-nbe-gw.conf
DNSMASQ_CONF=/etc/NetworkManager/dnsmasq-shared.d/nbe-gw.conf

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mxx %s\033[0m\n' "$*"; exit 1; }
online() { timeout 6 bash -c '</dev/tcp/1.1.1.1/443' 2>/dev/null; }

# ------------------------------------------------------------------ uninstall
if [[ $MODE == uninstall ]]; then
  bold "removing NBE gateway (Wi-Fi left as it is)"
  systemctl disable --now $UNITS 2>/dev/null || true
  rm -f /etc/systemd/system/nbe-gw-*.{service,timer}
  systemctl daemon-reload
  nft delete table inet nbe_gw 2>/dev/null || true
  nmcli con delete nbe-lan 2>/dev/null || true
  rm -rf "$LIB" "$ETC" /usr/local/bin/nbe-gw "$NMCONF" "$DNSMASQ_CONF" /etc/sysctl.d/90-nbe-gw.conf /var/lib/nbe-gw
  nmcli general reload conf 2>/dev/null || true
  echo "done. eth0 is an ordinary DHCP client again on the next cable plug-in."
  exit 0
fi

# ------------------------------------------------------------------ config
if [[ -f $CONF ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
  echo "config: $CONF"
fi
: "${UPLINK_IFACE:=wlan0}" "${LAN_IFACE:=eth0}" "${LAN_ADDR:=192.168.77.1/24}"
: "${CHECK_HOST:=nbe.evm100.org}" "${CHECK_PORT:=8884}" "${ALLOW_SSH_FROM_UPLINK:=yes}"
: "${STATUS_PORT:=80}" "${REBOOT_AFTER_MIN:=30}"
LAN_IP=${LAN_ADDR%/*}

[[ -e /sys/class/net/$UPLINK_IFACE ]] || die "no $UPLINK_IFACE"
[[ -e /sys/class/net/$LAN_IFACE ]] || die "no $LAN_IFACE"
command -v nmcli >/dev/null || die "NetworkManager (nmcli) not found - this needs Raspberry Pi OS Bookworm or newer"
systemctl -q is-active NetworkManager || die "NetworkManager isn't running"
python3 -c "import ipaddress,sys; n=ipaddress.ip_interface(sys.argv[1]); sys.exit(n.network.prefixlen>29)" "$LAN_ADDR" \
  || die "LAN_ADDR must look like 192.168.77.1/24"

# the existing Wi-Fi connection is the uplink - just check it's there
UP_CON=$(nmcli -g GENERAL.CONNECTION dev show "$UPLINK_IFACE" 2>/dev/null || true)
[[ -n $UP_CON ]] || die "$UPLINK_IFACE isn't connected to anything. Connect the Pi to Wi-Fi first (sudo nmtui), then re-run."
UP_NET=$(nmcli -g IP4.ADDRESS dev show "$UPLINK_IFACE" 2>/dev/null | head -1 | cut -d'|' -f1 | tr -d ' ')
echo "uplink: $UPLINK_IFACE on '$UP_CON' ($UP_NET)"
online || die "no internet through $UPLINK_IFACE - fix that first (setup needs it for packages)"

# don't saw off the branch we're sitting on
if [[ -n ${SSH_CONNECTION:-} ]]; then
  SSH_LOCAL=$(awk '{print $3}' <<<"$SSH_CONNECTION")
  if ip -4 -o addr show dev "$LAN_IFACE" | grep -q "inet $SSH_LOCAL/"; then
    die "you're logged in over $LAN_IFACE, which this turns into the lab side. SSH in over Wi-Fi ($UP_NET) instead."
  fi
fi

# subnet clash: the lab side must not overlap the Wi-Fi network
if [[ -n $UP_NET ]] && python3 -c "import ipaddress,sys; a=ipaddress.ip_interface(sys.argv[1]).network; b=ipaddress.ip_interface(sys.argv[2]).network; sys.exit(0 if a.overlaps(b) else 1)" "$UP_NET" "$LAN_ADDR"; then
  die "LAN_ADDR $LAN_ADDR overlaps the Wi-Fi subnet $UP_NET - pick another (e.g. 192.168.123.1/24) in gateway.conf and re-run"
fi

LOG=/var/log/nbe-gw-setup.log
trap '' HUP   # keep going if the SSH session drops
exec > >(tee -i --output-error=warn-nopipe -a "$LOG") 2>&1
echo "---- $(date) setup.sh"

# ------------------------------------------------------------------ packages
bold "packages"
NEED=()
command -v nft >/dev/null || NEED+=(nftables)
[[ -x /usr/sbin/dnsmasq ]] || NEED+=(dnsmasq-base)
command -v iw >/dev/null || NEED+=(iw)
if ((${#NEED[@]})); then
  apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${NEED[@]}"
else
  echo "nftables, dnsmasq-base, iw: present"
fi
if systemctl -q is-active dnsmasq 2>/dev/null; then
  warn "a system-wide dnsmasq service is running; it clashes with NetworkManager's - disabling it"
  systemctl disable --now dnsmasq
fi

# ------------------------------------------------------------------ NetworkManager config
bold "NetworkManager config"
mkdir -p "$(dirname "$NMCONF")" "$(dirname "$DNSMASQ_CONF")"
cat > "$NMCONF" <<'EOF'
# NBE gateway (setup.sh): NAT rules for the shared Ethernet port via nftables
[main]
firewall-backend=nftables
EOF
cat > "$DNSMASQ_CONF" <<'EOF'
# NBE gateway (setup.sh): extra options for NetworkManager's dnsmasq on the lab side
dhcp-authoritative
domain-needed
EOF
nmcli general reload conf   # re-reads the config without touching the Wi-Fi connection

# ------------------------------------------------------------------ app + firewall
bold "nbe-gw app, firewall, services"
install -d -m 755 "$LIB" "$ETC" /var/lib/nbe-gw
install -m 755 "$HERE/nbe_gw.py" "$LIB/nbe_gw.py"
ln -sf "$LIB/nbe_gw.py" /usr/local/bin/nbe-gw
cat > "$ETC/runtime.conf" <<EOF
# written by setup.sh - settings read by nbe-gw
UPLINK_IFACE=$UPLINK_IFACE
LAN_IFACE=$LAN_IFACE
LAN_ADDR=$LAN_ADDR
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
# (table nm-shared-$LAN_IFACE); this table only shuts the Wi-Fi side out.
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
systemctl enable nbe-gw-firewall.service
systemctl restart nbe-gw-firewall.service
systemctl enable --now nbe-gw-watchdog.timer
if [[ $STATUS_PORT != 0 ]]; then
  systemctl enable nbe-gw-status.service && systemctl restart nbe-gw-status.service
else
  systemctl disable --now nbe-gw-status.service 2>/dev/null || true
fi

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
