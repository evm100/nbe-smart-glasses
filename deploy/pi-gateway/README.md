# NBE Pi gateway: lab router on campus Wi-Fi

The ESP32 nodes can't use the campus Wi-Fi and the phone hotspots are too weak in the lab. The
Raspberry Pi 5 is already on the campus Wi-Fi (it does 5 GHz and has a better radio), so it shares
that connection with the lab router over Ethernet. **This setup never touches the Pi's Wi-Fi.** It
uses whatever network the Pi is already connected to.

```
 ESP32 nodes ──2.4 GHz──▶ lab router (SSID e.g. NBE-LAB) ──Ethernet──▶ Pi 5 ──Wi-Fi──▶ UCSD-DEVICE ──▶ internet ──▶ nbe.evm100.org:8884
 operator laptops ──────▶        192.168.77.x                   eth0 192.168.77.1   wlan0 (NAT)
```

**What the Pi does:** DHCP + DNS for the lab side, NAT (all lab traffic leaves as the Pi), a
firewall that keeps the campus side out (SSH stays open), and a watchdog that nudges the Wi-Fi
to reconnect if it drops.
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

## Setup

1. The Pi is on Wi-Fi with internet and you're SSH'd in **over Wi-Fi** (not over Ethernet: that
   port becomes the lab side, and the script refuses to run if that's how you're logged in).
2. Copy this folder over and run it:
   ```bash
   scp -r deploy/pi-gateway <user>@<pi>:        # or unzip pi-gateway.zip on the Pi
   ssh <user>@<pi>
   cd ~/pi-gateway && sudo ./setup.sh
   ```
   It installs `nftables`/`dnsmasq-base`/`iw` if missing, then sets up the Ethernet side. A
   `gateway.conf` is optional (`cp gateway.conf.example gateway.conf` to change the lab subnet,
   status-page port, etc.).
3. Plug the router in (see above), connect a laptop to `NBE-LAB`, then `sudo nbe-gw test` on the Pi
   and open <http://192.168.77.1>. Every test line should be OK.

The Pi can be set up at home and moved to the lab. The lab side keeps working on whichever Wi-Fi
the Pi joins there, as long as its subnet isn't `192.168.77.x`.

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
| `nbe-gw mac` | Pi Wi-Fi MAC (for network registration, e.g. UCSD-DEVICE) |
| `journalctl -u nbe-gw-watchdog -n 50` | what the watchdog did |
| `sudo ./setup.sh` | re-apply after editing `gateway.conf` |
| `sudo ./setup.sh --uninstall` | undo everything (the Wi-Fi is left as it is) |

The watchdog runs every minute. It restarts LAN sharing if that dropped. After 2 failed internet
checks it asks NetworkManager to reconnect `wlan0` to its usual saved network. After 6 it restarts
NetworkManager, and after `REBOOT_AFTER_MIN` (30) offline minutes it reboots the Pi.

## What setup.sh changes

- **Wi-Fi: nothing.** Its existing connection is used as is.
- NetworkManager connection `nbe-lan` on `eth0`: `ipv4.method shared`, `192.168.77.1/24`.
- `/etc/NetworkManager/conf.d/90-nbe-gw.conf` (NAT via nftables), `dnsmasq-shared.d/nbe-gw.conf`.
- `nbe-gw-firewall.service`: nftables table `inet nbe_gw`. Nothing new gets in from `wlan0`
  except SSH (turn that off with `ALLOW_SSH_FROM_UPLINK=no`), and nothing from campus is forwarded into the lab.
- `nbe-gw-watchdog.timer`, `nbe-gw-status.service` (unprivileged, port 80), `/usr/local/bin/nbe-gw`.

## Troubleshooting

- **Wi-Fi keeps dropping**: the Pi's Wi-Fi power saving is a common cause. Optional fix for your
  connection: `sudo nmcli con mod "$(nmcli -g GENERAL.CONNECTION dev show wlan0)" 802-11-wireless.powersave 2`
  (it takes effect on the next reconnect). Logs: `journalctl -u NetworkManager -n 80`.
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
