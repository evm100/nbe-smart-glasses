# NBE Pi gateway: lab router on campus Wi-Fi

The ESP32 nodes can't use the campus Wi-Fi and the phone hotspots are too weak in the lab. A
Raspberry Pi 5 can join the campus Wi-Fi (it does 5 GHz and has a better radio), so it becomes the
lab's internet connection:

```
 ESP32 nodes ──2.4 GHz──▶ lab router (SSID e.g. NBE-LAB) ──Ethernet──▶ Pi 5 ──Wi-Fi──▶ UCSD-DEVICE ──▶ internet ──▶ nbe.evm100.org:8884
 operator laptops ──────▶        192.168.77.x                   eth0 192.168.77.1   wlan0 (NAT)
```

**What the Pi does:** DHCP + DNS for the lab side, NAT (all lab traffic leaves as the Pi), a
firewall that keeps the campus side out, and a watchdog that reconnects the Wi-Fi.
NetworkManager's built-in "shared" mode does the DHCP/DNS/NAT, so there's no hand-written routing.

**Why this works with the nodes as they are:** a node only ever opens outbound connections:
MQTT over TLS to the broker (`:8884`), plus DNS and NTP. NAT handles that. The nodes need no
public or campus IPs and no port forwarding, and their certificates work as before. The broker
sees them all come from the Pi's campus address, which doesn't matter because they're told apart
by their certificates/client IDs. UCSD blocking device-to-device traffic doesn't matter either:
the nodes share the router's own network, and anything between them never touches UCSD.

## Router: pick one mode

| | **A. Access-point mode** (recommended) | **B. Router mode** (zero router config) |
|---|---|---|
| Router setting | Operation mode → *Access Point* (ASUS, TP-Link, Netgear all have it). No AP mode? Turn the router's DHCP server **off**, set its LAN IP to `192.168.77.2`. | Leave as is. WAN / Internet type: *Automatic IP (DHCP)*. |
| Cable | Pi `eth0` → router **LAN** port (some routers in AP mode use the WAN port; either works then) | Pi `eth0` → router **WAN / Internet** port |
| Nodes get | `192.168.77.x` from the Pi | `192.168.0.x` (or similar) from the router |
| NAT | once (Pi) | twice (router, then Pi): fine for MQTT |
| `nbe-gw clients` / status page | lists every node | lists only the router |

Either way, on the router:
- **SSID** e.g. `NBE-LAB`, **WPA2-Personal (AES)**, a strong password. Avoid WPA3-only. WPA2/WPA3 mixed is fine.
- **2.4 GHz on**, **fixed channel** 1, 6 or 11 (run `sudo nbe-gw scan` on the Pi to choose), **20 MHz** width.
  Turn off "Smart Connect" / band steering if it merges 2.4 and 5 GHz under one name, or give 2.4 GHz its own SSID.
- Change the router's admin password. Leave "AP/client isolation" off.
- In mode B the router's LAN must not be `192.168.77.x` (defaults like `192.168.0/1/50.x` are fine).

## Before you go (at home / your desk)

1. **Flash** Raspberry Pi OS Lite (Trixie, 64-bit) with Raspberry Pi Imager: set hostname,
   user, **enable SSH**, and add your home Wi-Fi so you can get in.
2. **Copy this folder** to the Pi (the whole `pi-gateway/`):
   `scp -r deploy/pi-gateway <user>@<pi>.local:` or put `pi-gateway.zip` on a USB stick.
3. **Get the Pi's Wi-Fi MAC and register it for UCSD-DEVICE**, the same way as for the ESP32s:
   `cat /sys/class/net/wlan0/address`. Registration can take a while to activate, so do this first.
4. **Fill in** `gateway.conf` (`cp gateway.conf.example gateway.conf`): the UCSD-DEVICE password.
   Put your **home Wi-Fi as `UPLINK2_SSID`**: then you can test everything at home (the Pi falls
   back to it) and on campus it picks UCSD-DEVICE by itself.
5. **Run it once at home** (it installs `nftables`/`dnsmasq-base` if missing, which needs internet):
   ```bash
   cd ~/pi-gateway && sudo ./setup.sh
   ```
   Over SSH on Wi-Fi your session will drop for a minute while it tries UCSD-DEVICE and falls
   back to home. The script keeps running. Log back in and check `/var/log/nbe-gw-setup.log`.
   Plug the router in, connect a laptop to `NBE-LAB`, open <http://192.168.77.1>, and point one
   node at it. Do a full dry run if you can.

## On campus

1. Pi + router powered, Ethernet between them. The Pi joins UCSD-DEVICE by itself (higher priority
   than the fallback). If it was set up at home, nothing else is needed. Otherwise run `sudo ./setup.sh`.
2. From a laptop on `NBE-LAB`: `ssh <user>@192.168.77.1`, then `sudo nbe-gw test`. Every line should be OK.
3. Status page: <http://192.168.77.1> (Pi uplink signal, broker reachability, connected devices,
   watchdog actions). Reachable only from the lab side.

## Pointing the nodes at the router

Re-provision each node with the router as network 1 (or as a fallback, so it still works elsewhere):

```bash
python firmware/tools/nbe_node.py configure FNG02 --wifi-ssid NBE-LAB          # asks for the password
python firmware/tools/nbe_node.py configure FNG02 --wifi2-ssid NBE-LAB         # ...or as fallback 2
cd firmware/esp-idf/ppg_stream && idf.py build flash monitor
```

Nothing changes on the server. The node appears on the Devices page as usual. The USB-gateway
operator PCs can join `NBE-LAB` too.

## Commands on the Pi

| | |
|---|---|
| `nbe-gw status` | uplink (SSID, band, signal, IP), lab side, DHCP clients, broker check |
| `sudo nbe-gw test` | step by step: uplink → gateway → DNS → internet → broker `:8884` / `:443` → NTP → LAN/NAT/firewall |
| `nbe-gw clients` | who has an address on the lab side |
| `sudo nbe-gw scan` | 2.4 GHz channel crowding → recommended router channel |
| `nbe-gw mac` | Pi Wi-Fi MAC |
| `journalctl -u nbe-gw-watchdog -n 50` | what the watchdog did |
| `sudo ./setup.sh` | re-apply after editing `gateway.conf` (a copy lives in `/etc/nbe-gw/`) |
| `sudo ./setup.sh --uninstall` / `--purge` | undo (purge also forgets the uplink Wi-Fi) |

The watchdog (every minute) restarts LAN sharing if it dropped, reconnects the uplink after 2 failed
checks, restarts NetworkManager after 6, and reboots the Pi after `REBOOT_AFTER_MIN` (30) offline
minutes. When the main uplink comes back into range it switches back from the fallback.

## What setup.sh changes

- NetworkManager connections `nbe-uplink` (+ `nbe-uplink2`) on `wlan0`: Wi-Fi power save off,
  real MAC always (needed for UCSD-DEVICE). `nbe-lan` on `eth0`: `ipv4.method shared`, `192.168.77.1/24`.
- `/etc/NetworkManager/conf.d/90-nbe-gw.conf` (NAT via nftables), `dnsmasq-shared.d/nbe-gw.conf`.
- `nbe-gw-firewall.service`: nftables table `inet nbe_gw`. Nothing new gets in from `wlan0`
  (including SSH, unless `ALLOW_SSH_FROM_UPLINK=yes`), and nothing from campus is forwarded into the lab.
- `nbe-gw-watchdog.timer`, `nbe-gw-status.service` (unprivileged, port 80), `/usr/local/bin/nbe-gw`.
- Enables SSH. Your other Wi-Fi connections (e.g. the home one from Imager) are left alone.

## Troubleshooting

- **Uplink won't connect**: `journalctl -u NetworkManager -n 80`. UCSD-DEVICE: is the MAC from
  `nbe-gw mac` the registered one, and has it activated? Wrong password shows as "secrets were required".
- **Online, but nodes can't reach the broker**: `sudo nbe-gw test` on the Pi. If `:8884` fails but
  `:443` works, campus is blocking the port. Ask IT, or move the nodes to the `:443` route (the server
  already has it for the USB gateways, see `deploy/evmserver/enable-mqtt-443.sh`).
- **Nodes get no address** (mode A): `nbe-gw status` shows LAN `connected (nbe-lan)` and the cable plugged in?
  Check that the router's own DHCP is off. Two DHCP servers on one network fight each other.
- **Nodes connect but drop**: bad 2.4 GHz channel (`sudo nbe-gw scan`), or 40 MHz width. Also check
  the Pi's signal (aim for > 60 % / > −65 dBm) and move the Pi, not the router, toward the campus AP.
- **NTP fails**: the node log shows no time sync. Rare on campus. Tell IT that UDP 123 outbound is needed.

## IT note

To UCSD, everything appears as **one registered device (the Pi's MAC)**. IT will care about that
and about the extra 2.4 GHz access point in the lab. Tell them it's a closed lab network for
research sensors, outbound-only, firewalled, on a fixed channel. Ask whether they prefer a
specific channel or an activated wired campus port (with one of those, the router's WAN can plug
straight into the wall and the Pi isn't needed).
