# auto-cuff

A servo presses the START button of an Omron BP7250 (HEM-7150T). The Pi then downloads the new
record over Bluetooth with [omblepy](https://github.com/userx14/omblepy) and sends it to the NBE
server. On a trial's live page the operator picks the cuff, sets the interval, and switches
automatic readings on or off. Each reading becomes a `cuff_start` + `bp` event in the session, the
same as a typed one.

```
cuff_node.py      MQTT node: what runs in production (systemd)
cuff_server.py    measurement logic (servo + omblepy + duplicate filter); also a standalone HTTP
                  server / --once test that doesn't need the NBE server
servo_cuff.py     SG90 on GPIO18
PROTOCOL.md       the MQTT contract. An ESP32 replacement must speak the same one
systemd/          auto-cuff.service (installed by install-service.sh)
node/             the node's credentials from the server (git-ignored)
data/             seen.json (records already downloaded), readings.csv (local log of every reading)
```

## Setup on the Pi (Zero 2 W, Pi OS Trixie)

1. **Pair the monitor once** (already done if `cuff_server.py --once` works):
   `cd omblepy && ../.venv/bin/python omblepy.py -p -d HEM-7150T`
2. **Venv:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. **Register the node** on the server: Admin → Nodes → register `CUF01`, **site `cuff`**.
   Download its *credentials.zip* and unzip it here into `node/`:
   `mkdir -p node && unzip CUF01_credentials.zip -d node/`
   (gives `node/sdkconfig.nbe` + `node/main/certs/client.{crt,key}`)
4. **Check:** `.venv/bin/python cuff_node.py --check` prints the broker and node ID and does a TLS handshake.
5. **Service:** `sudo ./install-service.sh`, then follow it with `journalctl -u auto-cuff -f`.
   Overrides (monitor MAC, timings: see the top of `cuff_server.py`) go in `/etc/default/auto-cuff`,
   e.g. `CUFF_MAC=28:FF:B2:31:CA:D5`.
6. The Pi's clock must be right (NTP; `timedatectl` should say *synchronized: yes*). The server uses
   it to place the events in time.

The Pi only needs outbound access to the broker (the same as the ESP32 nodes), so it can sit on the
lab router.

## Using it

On the live page of a running trial, under **Automatic cuff**:

- **Device**: the cuff on this subject (one trial at a time per cuff).
- **Every N min** (1.5-60): readings are spaced start to start, with at least 1 min of rest after each one.
- **Automatic readings**: on = the first reading starts right away, then one every N min. Off = no
  new readings (a reading already running finishes and is logged).
- **Measure now**: one reading, on or off.
- **Abort reading**: within ~25 s of the press the Pi presses START again, which stops and deflates
  the cuff. After that it only stops waiting, because another press could start a new measurement.

Failures show on the page and are logged as markers in the session.
