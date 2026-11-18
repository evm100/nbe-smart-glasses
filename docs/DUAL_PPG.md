# Dual PPG glasses: two MAX30102 sensors on one board

Status: implemented on branch `ppg-dual` (2026-11-17), firmware 0.10.0. Built and tested in the host
simulator and the server tests. **Not yet tested on hardware.**

## Why

The separate head board (HED01) is being retired. The glasses get a PPG sensor at **each temple**,
both on one XIAO ESP32-S3: `glasses_l` and `glasses_r`. The finger reference stays a one-sensor
board. Recording both sides at once gives paired left/right pulse waves for the blood-pressure work
(differences between the two sides, rejecting a side with bad contact, two views of the same beat).

Nothing old is removed: `head` sessions, `config/sites/head.json`, HED01 and the existing one-sensor
glasses boards keep working unchanged.

## Hardware

Both MAX30102s answer on I²C address **0x57** and can't share a bus. The ESP32-S3 has two I²C
controllers, so each sensor gets its own:

| sensor | I²C port | SDA | SCL | Kconfig |
|---|---|---|---|---|
| left (primary, stream 0) | 0 | D4 = GPIO5 | D5 = GPIO6 | `PPG_I2C_*` (unchanged) |
| right (stream 4) | 1 | D8 = GPIO7 | D9 = GPIO8 | `PPG_PPG2_I2C_*` |

Pins are Kconfig options (`sdkconfig.dual` sets the defaults above). Both sensors run from 3.3 V and GND.
Both sensors run with the **same settings** (LED mode/current, sample rate, averaging, pulse width, ADC
range), so the two sides can be compared directly.

**No camera on this build.** The camera uses the second I²C controller for its SCCB bus (see
`main/camera.c`), so `PPG_PPG2` and `PPG_CAMERA` exclude each other in Kconfig. A camera + two-PPG board
needs an I²C mux (TCA9548A) or another microcontroller; that is a later step.

## Firmware

New option `CONFIG_PPG_PPG2` ("Second PPG sensor", menu *Sensor*), overlay `sdkconfig.dual`.

* `app_main.c` keeps one sensor struct per side (I²C bus, driver handle, `ok`, `seq`, statistics) and
  reads **both FIFOs in the same loop pass**, left then right. Each sensor's samples get their own `seq`
  (from 0 at `START`) and their own per-sample `t_us`, computed from that sensor's FIFO count and the
  read time (timing rule 1 of PROTOCOL.md). The two chips have separate oscillators and drift apart
  slightly, so their samples are **never merged into one row**. Each is its own stream on the same device
  clock, and the server lines them up by `t_us`.
* Each side connects and reconnects on its own. A missing right sensor doesn't stop the left one, and
  the other way round. `START` works if either sensor is there.
* The INT pin (if wired) belongs to the left sensor. The right FIFO is read in the same pass; it fills at
  the same rate.
* The board's `S` line adds the right sensor's statistics prefixed with its stream name:
  `ppg_r_ok`, `ppg_r_fs_meas`, `ppg_r_ovf`, `ppg_r_alc_ovf`, `ppg_r_i2c_err`.
* MQTT status `sensor_ok` = at least one PPG sensor works.

Bandwidth: a second 100–400 Hz stream doubles the PPG bytes (~2–5 bytes per sample in binary). Over
MQTT or the USB gateway that is a few kB/s, well within what the camera build already sends.

## Protocol v2.2: further PPG streams

Additions only; everything in v1, v2 and v2.1 is unchanged. A one-sensor board sends exactly what it
sent before.

### H line

The left sensor stays the board's **primary stream** (stream id 0): its `H` line, `D` lines and samples
records are unchanged. The right sensor declares itself like the camera and auxiliary streams do, with
`proto=2`, a `sid`, a `stream` name and the new `kind=ppg`:

```
$H,proto=1,dev=GLS04,fw=0.10.0-idf,sensor=MAX30102,fs=100,ch=red,led_ma=6.4,adc=18,pw_us=411,avg=4,sr=400,range_na=4096,site=glasses_l,board=XIAO_ESP32S3,sensor_ok=1*..
$H,proto=2,dev=GLS04,fw=0.10.0-idf,sid=4,stream=ppg_r,kind=ppg,sensor=MAX30102,fs=100,ch=red,led_ma=6.4,adc=18,pw_us=411,avg=4,sr=400,range_na=4096,site=glasses_r,board=XIAO_ESP32S3,sensor_ok=1*..
```

* `kind=ppg`: a full-rate PPG stream that the server records and processes **like a primary stream**.
* `site`: where this sensor sits. The server uses it as the stream's role (below).
* Stream id 4 is reserved for the second PPG (`main/xstream.h` lists all ids: 0 PPG, 1 camera,
  2 demo, 3 cam_feat, 4 second PPG, 9 template).

### Samples, binary: record type 0x04

Samples of a stream other than 0 travel in a new record type next to text (0x01), samples (0x02) and
frame (0x03), in the same record stream (MQTT data frames and USB envelopes alike):

```
record  0x04 | body length (uvarint) | sid u8 | <exactly the body of a 0x02 samples record>
        0x02 body = n u16 | seq0 | t0_us | dt_nom_us | nch u8 | first values
                    | per further sample: zz(dt - dt_nom), zz(delta) per channel
```

Same delta coding, same cost per sample. The firmware builds one samples record per stream at a time
and keeps both open, so interleaved left/right reads don't fragment the records. Both are closed before
any text record (so an `E` or `S` line stays in order with the samples around it) and before a send.

### Samples, text: `M` lines

On the plain USB text link (`sglab record` on a serial port), a sample of stream `sid` ≠ 0 is an `M` line,
the line v2.1 already defines for numbered streams:

```
$M,4,1022,25591234,118734*..        sid, seq, t_us, values in ch= order (integers for kind=ppg)
```

The server's binary decoder turns a 0x04 record into exactly these `M` lines, the same way it turns 0x02
into `D` lines. So the recorder sees one format whichever link the board uses.

### Older servers

A server without v2.2 skips record type 0x04 (unknown records are already skipped) and ignores
`kind=ppg` streams: the left sensor, events and status are recorded as before, and the right side is
lost. **Update the server before flashing dual boards.** The USB gateway (`nbe_gateway`) forwards
records without decoding them, so it needs no update.

## Server

### Decoding (`sglab/framing.py`)

`record_lines()` decodes 0x04: reads the `sid` byte, decodes the rest with the 0x02 decoder, and returns
`$M,<sid>,<seq>,<t_us>,<v...>` lines. Everything downstream of the decoder sees protocol lines.

### Recording (`sglab/recorder.py`)

A board is still read by one `StreamWorker` (one thread per source). New: the worker owns **child
streams**, one per `kind=ppg` header (`StreamWorker.ppg_children`, keyed by `sid`).

* A child is a `PpgSubStream`: the same PPG bookkeeping as a primary stream (CSV writer, seq loss
  accounting, device-clock fit, SNTP fit, live buffer, rate), but no thread and no source of its own.
  The worker routes the child's `M` lines to it as samples.
* The child's **role** = its `H` line's `site` (`glasses_r`), or `<parent role>_<stream>` without one. If
  that role is already in use in the recording, `_b` is appended, as for devices.
* `START` (a new seq epoch, device reboot handling) and `SYNC` (SNTP pairing) reach the children too.
* The child's samples count as activity for the board, so the auto-restart doesn't fire while one
  side is streaming.
* `M` lines of a `kind=ppg` sid before its `H` line make the worker ask for `INFO`, as for other streams.

### Storage (session folder)

The right sensor is an ordinary PPG stream of the session:

```
glasses_l_GLS04.csv        t_s, seq, t_dev_us, red     (left, primary stream)
glasses_r_GLS04.csv        t_s, seq, t_dev_us, red     (right, child stream)
glasses_l_GLS04.rawlog     every line from the board, both sides (one raw log per board)
```

Both get an entry in `session.json` → `"streams"`, with their own `role`, `header`, `stats`,
`clock_fit`, `epoch_fit` and `io`. The child's entry also has `"parent"` (the left stream's name) and
`"sid"`. `t_dev_us` of both is the board's one `esp_timer` clock, so left/right alignment needs no PC
clock at all: compare `t_dev_us` directly.

Because they are ordinary `streams` entries, processing (Layers 1–2), the session page, the catalog, BP
training/evaluation and tuning handle the right side with **no special code**: it is just a stream whose
role is `glasses_r`.

### Sites and tuning

`config/sites/<role>.json` holds per-site pipeline overrides. `glasses_l` and `glasses_r` fall back to
`glasses.json` when they have no file of their own (`processing.site_family`). Tuning a side
separately later is `sglab tune ... --site glasses_r`, then `tune apply` writes `config/sites/glasses_r.json`,
which then overrides the fallback for that side only.

The known sites (web model pages, `sglab tune --site`) are `finger`, `glasses`, `glasses_l`,
`glasses_r`, `head`, in one list (`sglab.processing.SITES`). Live BP picks `glasses`, then `glasses_l`,
then `glasses_r` when no role is chosen.

### Live view

The live page draws every PPG stream of the trial, so the right side appears as its own trace with its
own live HR/quality (`liveppg` runs one analysis per role).

## Build and provisioning

| board | build |
|---|---|
| finger (FNG01) | unchanged |
| one-sensor glasses (GLS01), head (HED01) | unchanged until reflashed |
| camera glasses (GLS03) | unchanged (`--camera [--hybrid]`) |
| **dual-PPG glasses** | `build-firmware.sh --dual <ID>` (Wi-Fi), `--dual --hybrid <ID>` (Wi-Fi + USB gateway). Not with `--camera` |

`nbe_node.py configure <ID> --dual` applies `sdkconfig.dual`. **Register the node with site `glasses_l`**
(Nodes page, or `nbe_node.py register <ID> --site glasses_l`): the registry site is the left stream's role,
and the firmware's `PPG_PPG2_SITE` (default `glasses_r`) is the right one's.

## Tests

* `host_test/run.sh`: new `sim_dual*` builds simulate two MAX30102s on two buses (the right one 1 %
  fast, with its own values) on the USB text, MQTT and hybrid links. `check_dual.py` checks both streams
  complete, gap-free, each with its own seq and monotonic `t_us`, and the right side's measured rate.
  It also checks a run with the right sensor unplugged: the left must be unaffected.
* `tests/`: 0x04 decoding round trip, the recorder writing two CSVs + two `streams` entries from one
  source, child role naming and collision, site fallback.

## Open points

* Hardware test: both sensors on one 3.3 V rail (LED current peaks), I²C at 400 kHz on both ports,
  loop time with two FIFO reads (`loop_us_max`).
* Whether the two sides need different LED currents (skin/fit differs). Kept equal for now on purpose.
* **Sample timestamps use the nominal period** (timing rule 1). A chip whose oscillator runs 1 % fast
  gets a repeated `t_us` about once per 100 samples, and a slow one gets an occasional double step. This is
  existing behaviour of every board, not new here. The simulator's right sensor runs 1 % fast and shows it.
  For left/right timing comparisons at the millisecond level it is worth fixing later, for all boards: stamp
  with each sensor's measured period, or re-time on the server from `seq` and the clock fit.
* Camera + two PPGs: mux or different microcontroller (separate discussion).
