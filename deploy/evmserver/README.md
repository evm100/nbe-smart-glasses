# evmserver deployment (single box, next to UltraLights)

```
browsers ──https──▶ nginx (nbe.evm100.org) ──▶ sglab 127.0.0.1:8001 ──▶ /srv/nbe/{ppg-data,pki}
ESP32 nodes (UCSD-PROTECTED) ──mqtts :8884, client cert──▶ host mosquitto ◀── sglab (nbe-core)
USB gateways ──mqtts :443 (ALPN mqtt), client cert──▶ nginx stream ──▶ host mosquitto
Raspberry Pi: flash.sh <ID>  ◀── scp ── ~/nbe-firmware/out/  ◀── build-firmware.sh (ESP-IDF in Docker)
ESP32 nodes ──https GET /ota/<token>──▶ nginx ──▶ sglab     (over-the-air updates, §4)
```

> **Processing and firmware builds can run on a stronger machine** (Debian 13 LXC): see
> `deploy/worker/README.md`. With that set up, §2 below happens in the LXC instead of here.

UltraLights keeps its uvicorn on :8000 and its Docker mosquitto on :1883.

**:443 is shared.** Some networks (an operator's, for one) block :8884, so the USB gateway connects
on :443. nginx's stream module owns :443 and routes on the TLS ALPN without decrypting (it only reads
the ClientHello): `mqtt` goes to mosquitto, which still checks the client cert. Everything else goes
to the websites, which now all listen on `127.0.0.1:8443 ssl proxy_protocol` (UltraLights, tdml.org and
the rest included). `enable-mqtt-443.sh` makes that change and backs up `/etc/nginx` first. **A new
site must use that listen line, not `listen 443`** (certbot `--nginx` writes `listen 443 ssl`: change it,
or re-run the script). Connection log: `/var/log/nginx/nbe-mqtt-443.log`.

| node | site | board |
|---|---|---|
| GLS01 | glasses | on the glasses frame |
| HED01 | head | fixed to the head |
| FNG01 | finger | finger clip |

## 1. Server (once)

1. Router: forward **TCP 8884 → 192.168.1.220** (80/443 already come here).
   (Only the ESP32 boards use 8884. USB gateways use 443, which already comes here.)
2. `sudo deploy/evmserver/setup-root.sh`. It asks for the web password of admin `evm100`
   (and Let's Encrypt questions if certbot has none on file). It can be re-run safely.
3. Open https://nbe.evm100.org. The header should show **● broker** in green.
   Test without hardware: `nbe node register SIM01 --site finger && nbe sim-node SIM01 --duration 60`
   (`nbe` alias: see `deploy/DEPLOY.md` §3), then `nbe node disable SIM01`.

## 2. Firmware (on this server, no root)

```bash
python3 firmware/tools/nbe_node.py login https://nbe.evm100.org     # once, admin login
mkdir -p ~/.config/nbe-node && install -m 600 /dev/null ~/.config/nbe-node/wifi.env
nano ~/.config/nbe-node/wifi.env
#   WIFI_SSID=UCSD-PROTECTED
#   WIFI_EAP_USER=<UCSD username, without @ucsd.edu>
#   WIFI_PASSWORD='<UCSD password>'
#   WIFI2_SSID=<phone hotspot>   WIFI2_PASSWORD='...'    optional: tried when #1 isn't found
#   WIFI3_SSID=<other network>   WIFI3_PASSWORD='...'    optional: then this one (password/open only)
deploy/evmserver/build-firmware.sh             # GLS01 HED01 FNG01 -> ~/nbe-firmware/out/
```

Re-run it after changing your UCSD password (then re-flash all three), or after firmware changes.

### Camera glasses (USB gateway)

```bash
deploy/evmserver/build-firmware.sh --camera GLS03   # no Wi-Fi file needed -> ~/nbe-firmware/out/GLS03.bin
```

The camera image holds no Wi-Fi password and no node key, so `GLS03.bin` + `flash.sh` can be sent
to whoever flashes the board, on a Mac or Linux: `./flash.sh GLS03` in the folder with both files
(first run installs esptool; macOS may ask to install the command-line tools for `python3`).
On Windows 10/11 send `GLS03.bin` + `flash.bat` + `flash.ps1` instead: double-click `flash.bat`
(it flashes the only `.bin` in the folder and finds the COM port; needs Python 3 from python.org
or `winget install Python.Python.3.12`). Don't
send the rest of `out/`: the Wi-Fi images contain your UCSD password. Then register GLS03 on the
Nodes page and send the operator **Download gateway** for their system (that zip does hold the node key).
The gateway connects to `mqtts://nbe.evm100.org:443` (`[gateway] broker_uri`). An existing setup gets
that with `sudo deploy/evmserver/enable-mqtt-443.sh`. After that, send the operator a fresh download.

## 3. Flash (on the Pi)

```bash
scp -r evm100@<server>:nbe-firmware/out ~/nbe-fw
~/nbe-fw/flash.sh GLS01          # one board on USB at a time; then HED01, FNG01
```

After flashing, the script shows the board's log. Expect `got IP …` and then `connected to broker`.
The node appears on **Devices** within ~15 s. Typical problems:

| log | meaning |
|---|---|
| `Wi-Fi disconnected (reason 15/204/…)` repeating | PEAP rejected: wrong user/password, or the RADIUS cert failed the CA-bundle check |
| `transport error … sock errno` | 8884 not forwarded, or blocked from that network |
| `broker refused the connection` | cert not from this server's CA, or node disabled |

## 4. Updates over Wi-Fi (OTA)

Boards on firmware **0.4.0 or later** never need the Pi again for routine updates:

```bash
deploy/evmserver/build-firmware.sh             # builds, and uploads each node's image to the server
```

Then on **Nodes** (https://nbe.evm100.org/admin/nodes) press **Update** next to each node (or
`python3 firmware/tools/nbe_node.py ota GLS01`). The node stops streaming, downloads its image,
reboots into it and shows the new build in the *Firmware* column, usually within a minute. The
progress is shown under the version (`update: downloading 40%` … `updated`).

* **Update** is greyed out while the node is offline or in a running trial.
* If a new build can't get back on the broker, the node goes back to the previous build by itself at
  its next reboot (crash, or `CONFIG_PPG_NET_REBOOT_S` = 5 min). It then shows `update: rolled back`.
* Each image contains that node's key and your UCSD password, so the server only accepts an image built with
  the node's current certificate and gives it to the node only. It is downloaded with a 10-minute token
  sent over the node's MQTT link.
* The partition table changed in 0.4.0. Boards with older firmware need **one** USB flash with `flash.sh`.
  Re-flash on the Pi, too, after changing your UCSD password: an update can't reach a board that's no longer
  on Wi-Fi.

## Notes

* **Your UCSD password is stored in plain text** in each board's flash and in `~/nbe-firmware/`.
  Anyone holding a board can read it out. Keep the boards with you. If one is lost, change your
  UCSD password right away. UCSD's intended network for devices is UCSD-DEVICE (per-MAC
  password); `firmware/tools/nbe_node.py mac` + `WIFI_SSID=UCSD-DEVICE` without `WIFI_EAP_USER`
  switches to it.
* The RADIUS certificate is checked against public CAs only. To pin UCSD's server name, set
  `WIFI_EAP_DOMAIN=<name>` in `wifi.env` and rebuild.
* Backups: `/srv/nbe` holds the data and the device CA key (`deploy/DEPLOY.md` §8).
