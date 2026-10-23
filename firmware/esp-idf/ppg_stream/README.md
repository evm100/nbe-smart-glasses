# ppg_stream – ESP-IDF firmware (XIAO ESP32-S3 + MAX30102)

Streams MAX30102 PPG data in **PPG protocol v1** (`../../PROTOCOL.md`), either over
**USB** to a PC running `sglab`, or over **Wi-Fi + MQTT (TLS, client certificate)** to the
NBE server as a *node*. Same data either way; the link is a menuconfig choice.

- Target: **ESP-IDF v6.x** (written against the v6.0/v6.1 APIs: `driver/i2c_master.h`,
  `esp_driver_*` components). It should also build on v5.3+, but that hasn't been tried.
- Board: Seeed **XIAO ESP32-S3** (any ESP32-S3 works; change the pins in menuconfig).
- Status: **builds cleanly with ESP-IDF v6.1** (`espressif/idf:v6.1` Docker image, MQTT link with
  WPA2-Enterprise PEAP, 2026-10-08); not yet run on hardware. The C sources also run on a PC against
  stub ESP-IDF headers and a simulated MAX30102 (`host_test/`).

## Wiring (XIAO ESP32-S3 defaults)

| MAX30102 breakout | XIAO pin | GPIO |
|---|---|---|
| VIN / 3V3 | 3V3 | – |
| GND | GND | – |
| SDA | D4 | GPIO5 |
| SCL | D5 | GPIO6 |
| INT (optional) | e.g. D0 | GPIO1 → set "INT pin GPIO" = 1 |
| marker button to GND (optional) | e.g. D1 | GPIO2 → set "Marker button GPIO" = 2 |

The XIAO's user LED (GPIO21) is on while streaming.

## Build & flash

1. Install ESP-IDF v6.x by following Espressif's *Get Started* guide (installer or VS Code
   extension), then open an ESP-IDF terminal.
2. Build and flash:

```bash
cd firmware/esp-idf/ppg_stream
idf.py set-target esp32s3          # once
idf.py menuconfig                  # → "PPG stream (MAX30102)"
idf.py build
idf.py -p <PORT> flash             # Linux /dev/ttyACM0, macOS /dev/cu.usbmodem*, Windows COMx
```

If the board isn't detected or flashing fails: hold **BOOT**, plug in USB, release, and flash again.

### Wi-Fi node (MQTT link)

Don't set this up by hand: `python ../../tools/nbe_node.py configure <ID>` writes the
certificates, node ID, broker address, Wi-Fi and link choice (see `../../README.md`), then
`idf.py build flash monitor`. The first build downloads `espressif/mqtt` (ESP-IDF v6 moved
it to the component registry; see `main/idf_component.yml`) – needs internet once.

In MQTT mode the log console moves to USB-C, so `idf.py monitor` shows what the node is doing:
`got IP`, `connected to broker`, or the reason it isn't (Wi-Fi reason code, TLS error, broker
refusal). Status for the server: retained JSON every 15 s; data: compact binary frames every 250 ms,
acknowledged by the server (`../../PROTOCOL.md`, "MQTT data frames"). Everything stays in a 2 MB
PSRAM buffer (~15–30 min at 400 sps) until the server has it, and is sent again after a lost
connection, so Wi-Fi drops and slow hotspots delay data but don't lose it. The `BOOT` event and
the status say why the board last reset (`reset=brownout`, `panic`, ...). 100–200 sps is plenty over Wi-Fi unless
you need 400 for pulse-timing features.

Don't use `idf.py monitor` to judge the data: it shows the protocol lines, but sglab
is the real client. Boot and debug logs go to **UART0 (D6/D7)**, not USB, so the
USB-C port carries only protocol lines. To get logs on USB instead, change
*Component config → ESP System Settings → Channel for console output*, but they will
then mix with the data (sglab ignores non-`$` lines, but a log line can split a data line).

## menuconfig → "PPG stream (MAX30102)"

| Menu | Settings | Defaults |
|---|---|---|
| Identity | Device ID (`dev=`), measurement site, board name | FNG01, finger |
| Wiring | I2C port, SDA/SCL GPIO, I2C clock, internal pull-ups, INT GPIO, marker button GPIO, status LED GPIO + polarity | 0, 5/6, 400 kHz, on, −1, −1, 21 active-low |
| Sensor | LED mode (SpO2 red+IR / HR red-only), sample rate 50–3200, on-chip averaging 1–32, pulse width 69–411 µs, ADC range 2048–16384 nA, red/IR LED current (0.1 mA units), FIFO poll period, FIFO almost-full level | SpO2, 400 sps, 1, 411 µs, 16384 nA, 6.4 mA, 5 ms |
| Link | USB Serial/JTAG (USB-C), UART (+ number, baud, TX/RX GPIO), or Wi-Fi + MQTT; TX buffer size | USB, 8192 B |
| Link → Wi-Fi | SSID, password / WPA2-Enterprise (PEAP), modem sleep, max transmit power, 20 MHz only, SNTP server | UCSD-DEVICE, PSK, off, 20 dBm, off, pool.ntp.org |
| Link → MQTT | broker URI, topic prefix, broker CA (public bundle / private PEM), send period, largest frame, unacknowledged window, store-and-forward buffer, outbox limit (status only), status period, stop when the server goes offline | written by nbe_node.py; 250 ms, 8 kB, 64 kB, 2 MB with PSRAM, 64 kB, 15 s, on |
| Behaviour | Autostart without `START`, status-line period, die-temperature reporting, quiet logs after boot | off, 1000 ms, on, on |

**Give every board its own Device ID** (e.g. `GLS01` for the glasses, `FNG01` for the finger).
sglab uses it to name files and streams.

Output rate = sample rate ÷ averaging. Not every rate/pulse-width combination is allowed
(the LED pulses must fit in one sample period). The firmware sends a `SENSOR_ERR` warning
for clearly impossible settings, and **reports the real rate as `fs_meas` once per second**.
Always check that `fs_meas` ≈ `fs`.

## What it sends

```
$E,1002000,BOOT,0.2.0-idf*38
$H,proto=1,dev=FNG01,fw=0.2.0-idf,sensor=MAX30102,fs=400,ch=ir|red,led_ma=6.4|6.4,adc=18,pw_us=411,avg=1,sr=400,range_na=16384,site=finger,board=XIAO_ESP32S3,sensor_ok=1*51
$E,1002000,START,*1A
$D,0,1004500,118734,90211*..
...
$S,2002000,fs_meas=400.0,ovf=0,alc_ovf=0,i2c_err=0,tx_drop=0,loop_us_max=180,temp_c=31.25,heap=300000*..
```

| S-line key | Meaning | Healthy |
|---|---|---|
| `fs_meas` | samples actually read in the last period, per second | ≈ `fs` |
| `ovf` | FIFO overflow events since START (samples lost on the sensor side) | 0 |
| `alc_ovf` | ambient-light cancellation overflows (too much ambient light, e.g. sunlight) | 0 |
| `i2c_err` | failed I²C reads | 0 |
| `tx_drop` | bytes the PC didn't read in time (USB stalled / PC busy) | 0 |
| `temp_c` | sensor die temperature | stable |

The sglab live page shows these under **Device report**, and the last report is saved in
`session.json` (`streams[].device_status`).

Commands from the PC: `INFO`, `START`, `STOP` (case-insensitive). If the sensor is missing,
the firmware keeps retrying once per second and answers `START` with a `SENSOR_ERR` event.

## First test on hardware

1. Flash, open the repo's venv, and run `sglab ports`. The XIAO appears as a USB serial device.
2. Quick manual look (optional): run `python -m serial.tools.miniterm <PORT>`, type `START`
   and Enter. `$D,...` lines should scroll. Type `STOP`. Exit with Ctrl-].
3. Put the port into `config/lab.toml` under `[devices.finger]`. Baud doesn't matter for
   USB-C; for UART, match "UART baud rate".
4. Run a 15 s recording:

   ```bash
   sglab record finger --duration 15 --yes --meta subject_id=S001 --meta protocol=other --meta operator=EV --meta quality=usable --meta keep=n
   ```

   Check: rate ≈ fs, 0 dropped, 0 bad lines. In the saved `session.json`, check
   `device_status.fs_meas` ≈ fs and `clock_fit.b` ≈ 1.000.
5. Finger on the sensor:
   - **Raw IR counts:** should sit well below 262143. If they're pinned at the top, lower the LED current or raise the ADC range. If they're tiny (< ~10 000), raise the LED current.
   - **IR vs red:** IR normally reads higher than red on skin. If they look swapped, swap the `ir[k]`/`red[k]` order in `read_and_send()`; the comment in `max30102.c` marks LED1 = red, LED2 = IR, per the datasheet.

### If something's wrong

| Symptom | Likely cause |
|---|---|
| `SENSOR_ERR MAX30102 not found` | Wiring/power; wrong SDA/SCL GPIO; some cheap breakouts pull SDA/SCL up to 1.8 V. Check with a meter and consider 4.7 kΩ pull-ups to 3.3 V. |
| `fs_meas` far below `fs` | Rate/pulse-width combination not supported → lower the rate or the pulse width |
| `ovf` increasing | FIFO poll period too long, or the task is starved → lower "FIFO poll period" or connect INT |
| `tx_drop` increasing | PC isn't reading (sglab not running, laptop asleep) or the USB hub is overloaded |
| `bad lines` in sglab with UART link | Baud mismatch between menuconfig and `config/lab.toml` |

## Host test (no ESP32 needed)

```bash
firmware/esp-idf/ppg_stream/host_test/run.sh    # also runs inside `pytest`
```

This builds `main/*.c` for the PC against `host_test/stubs/` (stand-in ESP-IDF headers)
and a simulated MAX30102 with a fake clock. It runs six configurations (default, INT pin,
UART link, HR mode at 25 Hz output, too-slow polling, MQTT link with a fake esp-mqtt that
checks every batch holds whole lines), type-checks the Wi-Fi code (PSK, PEAP) against
`host_test/stubs_net/`, and checks every output line with sglab's parser:
- checksums
- sequence continuity
- sample spacing
- channel order
- FIFO-overflow detection

It catches logic and C errors, not ESP-IDF API mismatches; only a real `idf.py build` does that.

## Files

```
CMakeLists.txt          project (ESP-IDF)
sdkconfig.defaults      target esp32s3, 1 kHz tick, console on UART0, 8 MB flash, PSRAM, OTA partitions + rollback
partitions.csv          two ~4 MB app slots for over-the-air updates
sdkconfig.nbe           (generated, not committed) per-node values from nbe_node.py
main/Kconfig.projbuild  the menuconfig menu
main/idf_component.yml  managed components (espressif/mqtt)
main/app_main.c         tasks: sensor (FIFO → D lines, status, SYNC, start/stop) + commands
main/max30102.c/.h      register-level driver on i2c_master
main/proto.c/.h         line builder with XOR checksum (no printf)
main/link.h             transport interface used by app_main
main/link.c             USB Serial/JTAG or UART transport
main/link_mqtt.c        MQTT transport: batching, store-and-forward, status/LWT, commands
main/ota.c/.h           over-the-air update ("OTA <token>" command), rollback if the new build can't reach the broker
main/net.c/.h           Wi-Fi station (PSK or PEAP), reconnect back-off, SNTP
main/certs/             (generated, not committed) node certificate + key
host_test/              PC build + simulator (USB, UART, MQTT batching) + output checker
```
