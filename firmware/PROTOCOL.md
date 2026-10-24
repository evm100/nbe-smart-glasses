# PPG serial protocol v1 (ESP32 → PC)

Implementations: `esp-idf/ppg_stream/` (main, ESP-IDF v6) and `arduino/ppg_stream/` (reference).

This is what the firmware sends over USB serial so `sglab record` can capture it.
It is plain ASCII, so you can still read it in the Arduino serial monitor while
debugging. It was chosen to make three things easy:
**detecting corrupted lines, detecting lost samples, and timing every sample.**

## Line format

```
$<TYPE>,<field>,<field>,...*<CS>\r\n
```

* Every line starts with `$` and ends with `*` + two hex digits + newline.
* `CS` = XOR of every byte **between** `$` and `*` (same checksum as NMEA/GPS).
* Lines that don't start with `$` are ignored by the PC (they're saved as debug
  text), so `Serial.println("debug...")` is harmless.

```cpp
uint8_t cs = 0; for (const char* p = body; *p; ++p) cs ^= *p;
Serial.printf("$%s*%02X\r\n", body, cs);
```

## Message types

### `H` – header (device description)

Sent on boot, in reply to `INFO`, and as the first line after `START`.
Key=value pairs, any order, unknown keys are kept as metadata.

```
$H,proto=1,dev=FNG01,fw=0.1.0,sensor=MAX30102,fs=400,ch=ir|red,adc=18,led_ma=6.4|6.4,pw_us=411,avg=1*5C
```

| key | meaning | required |
|---|---|---|
| `proto` | protocol version (`1`) | yes |
| `dev` | unique device ID, written on the board label (e.g. `GLS01`, `FNG01`) | yes |
| `fw` | firmware version | yes |
| `fs` | **actual** output sample rate in Hz (after on-chip averaging) | yes |
| `ch` | channel names in the order they appear in `D` lines, `|`-separated | yes |
| `sensor`, `adc`, `led_ma`, `pw_us`, `avg`, `site`... | sensor settings | recommended |

LED current, pulse width and averaging change the signal, so **always** report them.

### `D` – one sample

```
$D,<seq>,<t_us>,<ch1>,<ch2>,...*CS
$D,10234,25591234,118734,90211*4C
```

| field | meaning |
|---|---|
| `seq` | sample counter, starts at 0 on `START`, +1 for **every sample** (uint32). A jump = samples lost on the serial link. |
| `t_us` | ESP32 time of **this sample** in µs (`esp_timer_get_time()`), see timing below |
| `chN` | raw ADC counts, integers, same order as `ch=` |

### `E` – event

```
$E,<t_us>,<CODE>,<optional text>*CS
$E,25600000,BTN,*30
```

Codes: `START`, `STOP`, `BTN` (marker button), `FINGER_OFF`, `FINGER_ON`,
`FIFO_OVF` (sensor FIFO overflowed → samples lost), `SENSOR_ERR`, `BOOT`, `SYNC`.

`SYNC` (Wi-Fi nodes with SNTP time, at START and every 10 s while streaming) pairs the
device clock with UTC: the text is Unix time in milliseconds at `t_us`.

```
$E,25610000,SYNC,1790978387123*5B
```

The recorder stores the fit `unix_s = a + b·t_dev_s` as `epoch_fit` in session.json, so
streams from different nodes can be put on one clock (PC receive time over Wi-Fi jitters
by tens of ms; SNTP-to-SNTP agreement is typically a few ms on the same network).

### `S` – status (about once per second, optional)

```
$S,<t_us>,key=value,...*CS
$S,26000000,vbat=3.91,ovf=0,loop_us=180*4D
```

Free-form key=value pairs; sglab shows them live and stores the last one in
`session.json`. The ESP-IDF firmware sends `fs_meas` (real sample rate), `ovf`,
`alc_ovf`, `i2c_err`, `tx_drop`, `loop_us_max`, `temp_c`, `heap`.
Avoid slow reads here (e.g. a blocking MAX30102 temperature read takes ~30 ms; start
the conversion and read it on the next status cycle instead).

## Commands (PC → ESP32)

Plain text lines ending in `\n`:

| command | device must |
|---|---|
| `INFO` | reply with an `H` line |
| `START` | reset `seq` to 0, send `H`, send `E,...,START`, then stream `D` lines |
| `STOP` | stop streaming, send `E,...,STOP` |
| `OTA <token>` | MQTT link only: stop streaming, download `<CONFIG_PPG_OTA_URL>/<token>` over HTTPS, install it, reboot. Progress in the status message's `ota` field |

Stream only between `START` and `STOP`, so the PC controls when a trial begins.

## Timing rules (important for blood-pressure work)

1. **The sensor's sample clock is the truth.** The MAX30102 samples on its own
   clock into a FIFO; the ESP32 reads several samples at once. Do **not** stamp
   all of them with the read time. If `n` samples were read at time `now`:

   ```
   t_us[k] = now - (n - 1 - k) * (1e6 / fs)      // k = 0 .. n-1, oldest first
   ```
2. Read the FIFO at least every 10 ms (it holds 32 samples: 80 ms at 400 Hz).
   If it overflows, send `E,...,FIFO_OVF` – the PC will see the time gap.
3. `seq` gaps = link loss, `t_us` gaps = sensor/FIFO loss. Both are detected.
4. **Sample rate:** 100 Hz is enough for heart rate. Pulse-shape timing features for
   blood pressure benefit from **≥ 200–400 Hz** (at 100 Hz, one sample = 10 ms).

## Bandwidth

A `D` line is ~40 bytes. 400 Hz × 40 B = 16 kB/s ≈ 160 kbit/s.

* ESP32 with a USB-UART bridge chip: use **921600 baud** (115200 is too slow above ~250 Hz).
* ESP32-S3/C3 native USB (CDC): baud rate is irrelevant, it's fast.

## MQTT transport (Wi-Fi nodes)

The same messages travel over MQTT (TLS, client certificate per node) instead of USB:

| topic | direction | payload |
|---|---|---|
| `nbe/dev/<ID>/data` | node → server, QoS 0 | binary frames (below), every 250 ms; firmware < 0.6 sent text lines (QoS 1), still accepted |
| `nbe/dev/<ID>/cmd` | server → node | `INFO` / `START` / `STOP` / `OTA <token>` (QoS 1); `ACK <boot> <offset>` / `RESEND <boot> <offset>` (QoS 0) |
| `nbe/dev/<ID>/status` | node → server, retained | JSON: online, fw, sensor_ok, streaming, fs, rssi, ip, uptime_s, ts, heap, buf (most bytes held for the server since the last status), unacked, rtx (resends), tx_drop, txp (Wi-Fi dBm), reset (why the board last reset); last will `{"online":false}` |
| `nbe/core/status` | server → nodes, retained | `{"online":true}`; last will `{"online":false}` → nodes stop streaming |

### MQTT data frames (firmware ≥ 0.6)

Reliable store-and-forward with end-to-end acknowledgement. The node's output (the same `H` / `E`
/ `S` lines and the samples) is one byte stream of records, kept in a PSRAM ring (2 MB, ~15–30 min)
until the server acknowledges it. A frame carries whole records from a stream offset:

```
frame   'N' 'B' | version 2 | flags 0 | boot u32 | offset u64 | crc32 u32 (of the records) | records
record  type u8 | body length (uvarint) | body
  0x01 text     one protocol line "$...*CS" (no CR/LF)
  0x02 samples  n u16 | seq0 | t0_us | dt_nom_us | nch u8 | first values
                | per further sample: zz(dt - dt_nom), zz(delta) per channel     (uvarints, zz = zigzag)
```

Little-endian; `boot` is random per boot (a new stream, offsets from 0). A sample takes ~2–5 bytes
instead of ~35 as a text line, so the uplink carries 7–16× less. The server
(`sglab.framing.FrameReceiver`) checks the CRC, drops records it already has, turns the rest back
into protocol lines for the recorder and answers on the cmd topic:

* `ACK <boot hex> <offset>` – everything before offset is stored; the node frees it.
* `RESEND <boot hex> <offset>` – a frame was missing (or corrupt): the node goes back to offset.
  After 5 RESENDs without progress the server accepts the gap (the node no longer has the data).

The node sends at most 64 kB ahead of the last ACK. After a reconnect, a `RESEND`, or 10 s
without ACK progress it goes back to the first unacknowledged byte (go-back-N). The sensor task
only appends to the ring, so a slow network never makes the sensor FIFO overflow; only an outage
longer than the ring loses data (`tx_drop`).

If a recording node stops delivering samples (rebooted, or stopped by the server's last will),
the recorder sends `START` again: at once when the node's status says `"streaming":false`,
otherwise after 20 s of silence (120 s while its live status still says `"streaming":true`: the
samples are late, not gone, and a `START` would clear the sensor FIFO). Samples without an `H`
header (it was lost) make the recorder send `INFO` every 5 s until one arrives.
A `START` event after the first one begins a new `seq` epoch;
if the device clock went backwards (reboot), later device times are shifted onto the old
timeline via the PC clock so `t_dev_us` stays monotonic. Nodes that cannot reach the broker
for `CONFIG_PPG_NET_REBOOT_S` (default 300 s) reboot themselves.

## One ESP32, two sensors?

Two MAX30102s share the I²C address 0x57, so they need two I²C buses (or a mux).
If both run at the same `fs`, put all channels in one `D` line, e.g.
`ch=gl_ir|gl_red|fi_ir|fi_red`. Otherwise use one ESP32 per sensor; the recorder
captures several serial ports at once and aligns them by PC receive time.

## Checklist for the firmware

- [ ] `H` line with real `fs`, LED current, pulse width, averaging
- [ ] `seq` + per-sample `t_us` (back-calculated within each FIFO batch)
- [ ] checksum on every line
- [ ] `INFO` / `START` / `STOP` commands
- [ ] `FIFO_OVF` event, optional `BTN` marker button
- [ ] confirm which value is IR vs red (cover the sensor / check which LED
      reads higher DC on skin; libraries differ)
