#!/usr/bin/env bash
# Create the Debian 13 LXC for NBE processing + firmware builds (run on the Proxmox host, as root).
#
#   deploy/worker/create-lxc.sh                       defaults below
#   CTID=151 IP=192.168.1.230/24 GW=192.168.1.1 deploy/worker/create-lxc.sh
#
# Unprivileged, with nesting + keyctl so Docker (ESP-IDF builds) runs inside.
# TAILSCALE=1 also passes /dev/net/tun through (for `tailscale up` inside, e.g. so the Pi can scp).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root on the Proxmox host"; exit 1; }
command -v pct >/dev/null || { echo "pct not found - this runs on the Proxmox host"; exit 1; }
CTID=${CTID:-150}
HOSTNAME_=${CT_HOSTNAME:-nbe-proc}
STORAGE=${STORAGE:-local-lvm}        # rootfs storage
TSTORE=${TEMPLATE_STORAGE:-local}    # where templates live
BRIDGE=${BRIDGE:-vmbr0}
IP=${IP:-dhcp}                       # or 192.168.1.230/24 (then also GW=)
CORES=${CORES:-$(( $(nproc) / 2 ))}
MEMORY=${MEMORY:-16384}
DISK=${DISK:-64}                     # GB: the ESP-IDF image alone is ~5 GB, build trees ~1 GB per node

pct status "$CTID" >/dev/null 2>&1 && { echo "CT $CTID already exists"; exit 1; }
pveam update >/dev/null
T=$(pveam available --section system | awk '{print $2}' | grep -E '^debian-13-standard_.*_amd64' | sort -V | tail -n 1)
[[ -n "$T" ]] || { echo "no debian-13-standard template in 'pveam available'"; exit 1; }
pveam list "$TSTORE" | grep -q "$T" || pveam download "$TSTORE" "$T"

net="name=eth0,bridge=$BRIDGE,ip=$IP"; [[ "$IP" != dhcp ]] && net+=",gw=${GW:?set GW= with a static IP}"
pct create "$CTID" "$TSTORE:vztmpl/$T" \
  --hostname "$HOSTNAME_" --unprivileged 1 --features nesting=1,keyctl=1 \
  --cores "$CORES" --memory "$MEMORY" --swap 2048 \
  --rootfs "$STORAGE:$DISK" --net0 "$net" --onboot 1 --ostype debian \
  --password
if [[ -n "${TAILSCALE:-}" ]]; then
  cat >> "/etc/pve/lxc/$CTID.conf" <<CONF
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
CONF
fi
pct start "$CTID"
sleep 5
pct exec "$CTID" -- bash -c 'apt-get update -qq && apt-get install -y -qq openssh-server rsync >/dev/null'
echo
echo "CT $CTID ($HOSTNAME_) is up: $(pct exec "$CTID" -- hostname -I)"
echo "next: copy the repo in and run deploy/worker/setup-worker.sh inside it (deploy/worker/README.md)"
