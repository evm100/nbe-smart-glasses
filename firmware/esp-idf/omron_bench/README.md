# omron_bench: can an ESP32 replace the auto-cuff Pi?

A bench test for a XIAO ESP32-S3. It talks to the Omron BP7250 (HEM-7150T) over Bluetooth the way
omblepy does on the Pi, and presses START with the same SG90 servo. There's no Wi-Fi or MQTT yet:
this only answers "does the Bluetooth part work on an ESP32?". If it does, the next step is an
MQTT node that speaks `auto-cuff/PROTOCOL.md`, so the server can't tell it from the Pi.

```
main/omron_ble.c   omblepy's protocol in C on NimBLE: scan, bond, unlock key, EEPROM reads
main/om_record.c   record decoding (hem-7150t.py), checked against omblepy by host_test/
main/servo.c       SG90 press (50 Hz PWM, 2.4 ms for 150 ms, then no pulses = limp; like servo_cuff.py)
main/app_main.c    serial console
host_test/check_records.py   C decoder vs omblepy's Python on 10k random records (no board needed)
```

## Build and flash

```bash
. ~/.espressif/tools/activate_idf_v6.1.sh
cd firmware/esp-idf/omron_bench
idf.py build flash monitor -p /dev/ttyACM0
```

The settings are in `idf.py menuconfig` → *Omron bench* (monitor MAC, servo GPIO, press timing).
MAC and key can also be changed at runtime and are saved to flash.

**Servo wiring (XIAO ESP32-S3):** orange → D0 (GPIO1), red → 5V, brown → GND. On USB power, an SG90
stalling against the button can brown out the board. If it resets during a press, give the servo
its own 5 V supply with a common ground.

## Console

| command | does |
|---|---|
| `status` | MAC, key, number of stored bonds, newest record seen |
| `mac AA:BB:..` | set the monitor's MAC (default `28:FF:B2:31:CA:D5`, the Pi's) |
| `key <32 hex>` | the unlock key. The default is omblepy's (`deadbeaf12341234...`), which the Pi used |
| `pair` | one-time pairing. Hold the monitor's Bluetooth button until **P** blinks, then run it |
| `read` | download all stored records. `REC,<time>,sys,dia,bpm,mov,ihb[,NEW]` per record |
| `press` | one servo press |
| `measure` | the Pi's full cycle: press → 35 s → download until a NEW record → press to power off |
| `forget` | delete the stored bond |

Each command ends with `RESULT,ok,...` or `RESULT,error,<reason>`.

## Test plan (in order)

1. **`press`**: the servo pushes START and the monitor wakes. Press again to switch it off.
2. **`read` without pairing.** Take a reading by hand, then run `read` while the monitor still shows
   the Bluetooth symbol. Two outcomes are possible:
   - It works. The unlock key alone is enough, and you can skip `pair`.
   - `RESULT,error,... not paired/encrypted` or a disconnect. Go on to step 3.
3. **`pair`**: monitor in pairing mode (P blinking), then `pair`. Expect `RESULT,ok,paired`. Then repeat
   step 2. ⚠ This re-pairs the monitor with the ESP32. The Pi may lose its Bluetooth bond and need
   `omblepy.py -p -d HEM-7150T` again afterwards. It reprograms the *same* unlock key, so nothing
   else changes for the Pi.
4. **Compare with the Pi.** The `REC` lines should match `omblepy` / `cuff_core.py --once` on the Pi,
   record for record.
5. **`measure`** ×3, a few minutes apart: each one should end in `RESULT,ok,measured ...`. Note the
   total time (the Pi takes ~1-2 min).
6. Power-cycle the ESP32 and run `read`: the bond should survive in flash (`status` shows 1 bond).

If something fails, the log lines just before `RESULT,error` say where: scan, connect,
encryption, service discovery, unlock, or an EEPROM read. Send those lines to me.

## What's verified so far

- Builds with ESP-IDF 6.1 for esp32s3 (`-Werror`).
- The record decoder matches omblepy's Python on 10,048 random valid records
  (`python3 host_test/check_records.py`).
- Nothing has run on a board yet: the Bluetooth parts (bonding, unlock, readout) are untested.
