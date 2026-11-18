# Auto-cuff MQTT contract

Any device that speaks this contract can be the auto-cuff: the Raspberry Pi (`cuff_node.py`)
today, an ESP32 later. The server side is `src/sglab/autocuff.py`.

## Identity

The device is a registered node with **site `cuff`** (Admin → Nodes → register, e.g. `CUF01`).
It connects to the broker with that node's client certificate (CN = node ID), the same mTLS as the
sensor boards, so the broker ACL (`deploy/mosquitto/nbe-acl`) needs no changes. Client ID = node ID.

Cuff nodes are not streams: the trial form doesn't list them. The operator attaches one on the
trial's live page.

## Topics (prefix `nbe`)

| topic | direction | QoS | payload |
|---|---|---|---|
| `nbe/dev/<ID>/status` | device → server | 1, retained | status JSON (below), every 15 s and on every state change. Last will: `{"online": false, "kind": "cuff"}` |
| `nbe/dev/<ID>/cmd` | server → device | 1 | `MEASURE <req>` · `ABORT` · `INFO` (the server sends commands in upper case) |
| `nbe/dev/<ID>/data` | device → server | 1 | one JSON object per message (first byte must be `{`) |

## Status

```json
{"online": true, "kind": "cuff", "fw": "auto-cuff-pi 0.1.0",
 "cuff_state": "idle", "phase": null, "req": null,
 "monitor": "HEM-7150T", "monitor_mac": "28:FF:B2:31:CA:D5",
 "ts": 1763412345, "uptime_s": 3600,
 "last_error": null, "last_reading": {"sys": 118, "dia": 76, "bpm": 70, "mov": 0, "ihb": 0, "device_time": "..."}}
```

- `cuff_state`: `idle` | `measuring`. Don't call it `state`: the server uses that key for online/offline.
- `phase` while measuring: `pressing` → `started` → `downloading` → `powering_off`.
- `ts`: device clock, unix seconds. The server uses it to tell a fresh retained status from an old one,
  so the device needs a synced clock (NTP on the Pi, SNTP on an ESP32).

## Commands

- `MEASURE <req>`: press START, wait for the cuff, download the new record over Bluetooth, press START
  again to switch the monitor off. `<req>` is an opaque ID to copy into every data message for this
  reading. If already measuring, answer `{"type": "failed", "req": <req>, "error": "already measuring"}`.
- `ABORT`: stop the reading in progress. Press START again only if the cuff is still inflating
  (the Pi: within 25 s of the first press). Later a press could start a *new* measurement, so just stop
  waiting and let the cuff finish on its own. Then send `aborted`.
- `INFO`: publish the status now.
- Ignore anything else.

## Data messages

```json
{"type": "started",  "req": "A1B2C3", "t_unix": 1763412345.2}
{"type": "reading",  "req": "A1B2C3", "t_unix": 1763412401.0, "sys": 118, "dia": 76, "bpm": 70,
                     "mov": 0, "ihb": 0, "device_time": "2026-11-17 14:03:22"}
{"type": "failed",   "req": "A1B2C3", "t_unix": 1763412590.0, "error": "omblepy timed out"}
{"type": "aborted",  "req": "A1B2C3", "t_unix": 1763412360.0, "error": "aborted by the operator ..."}
```

- `started`: send it right after the first press. The server logs a `cuff_start` event at `t_unix`.
- `reading`: send it as soon as the record is downloaded. The server logs a `bp` event at `t_unix`.
  That marks the end of the measurement window for Layer 3, so use the download time, not the time the
  monitor was switched off. `mov` = body-movement flag, `ihb` = irregular-heartbeat flag (0/1).
  `device_time` is the monitor's own clock, kept only for reference.
- Every reading must be *new*: compare against records already downloaded (the monitor keeps 60).
  On a device's first run, return only the newest record.
- The server corrects event times by `t_unix` only when the device clock looks right (0-600 s old).
  Otherwise it uses the arrival time.

## Server behaviour (for reference)

- The scheduler runs on the server. Readings are spaced start to start at the operator's interval
  (1.5-60 min), with at least 60 s of rest after each reading ends.
- No `started` within 20 s of `MEASURE` → failed ("no answer"). No result within 5.5 min of
  `started` → failed.
- When the trial stops, no new readings start. A reading already running is still logged until the
  trial is saved.
- Messages for an old `req`, or from a cuff that isn't attached to a trial, are ignored.
