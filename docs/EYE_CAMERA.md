# Eye camera (GLS03): frame rate, exposure, IR illumination

The eye camera is the camera glasses' OV2640 (Seeed XIAO ESP32-S3 Sense). This page covers the
settings for blink recordings, how to measure them, and the IR LED to fit. Firmware:
`firmware/esp-idf/ppg_stream` (`main/camera.c`, `sdkconfig.camera`).

**Status (2026-11-25):** the firmware builds with ESP-IDF 6.1 (USB-binary and hybrid + IR variants) and
passes the host tests. **None of it has run on the board yet.** Every frame rate below is a prediction
from the OV2640 datasheet, calibrated against two rates measured on GLS03 earlier: about 25 fps in stock
CIF framing at clock divider 3, and about 25 fps in SVGA mode at divider 1. Section 4 is the procedure
that checks the predictions.

## 0. Measured so far (2026-11-25, spare XIAO ESP32-S3 Sense, OV2640, no PPG fitted; not GLS03)

USB binary firmware from this branch, `scripts/cam_bench.py`, 112×112 gray, exposure 320 unless noted:

| setting | sensor fps | lost frames | notes |
|---|---|---|---|
| CIF, CLK=1 (the default in `sdkconfig.camera`) | ~0.2–0.9 | almost all | driver: `EV-VSYNC-OVF`, `FB-SIZE: 0 != 16384`, `FB-OVF`. Pixel clock too fast to receive. |
| SVGA WIN=320, CLK=1 (the clock GLS03 used before) | ~0.9 | almost all | same failure on this board |
| CIF, CLK=2 | 32–33 | 3–5 % | no driver errors; losses come as 2-frame gaps (~90 ms) about once a second |
| CIF, CLK=3 | 24.9 | 0.5–1.6 % | steady; jitter 0.03 ms |

- No frames were lost on the USB link in any run: all losses happen between the sensor and the driver.
- Exposure (100/200/300) and frame buffers (3 vs 5; PSRAM vs internal RAM) did not change the losses.
- The 2-frame gaps do not line up with the once-a-second MAX30102 retry (no PPG on this board).
- The esp32-camera driver starts a capture only when a buffer is free at VSYNC and skips the frame
  silently otherwise (`cam_start_frame` in `cam_hal.c`); the cause of the gaps is not found yet.
- GLS03 ran CLKRC 0x81 before and got ~22 fps of a nominal 25: it may also have been at its limit.

So on this board: **50 fps is not reachable by raising the sensor clock**; 33 fps is, with a few % lost.
Repeat the table on GLS03 before changing the default.

## 1. Recommended camera configuration

| | before | now (`sdkconfig.camera`) |
|---|---|---|
| sensor mode | SVGA (800×600, 2 sensor px per px) | **CIF** (400×296, 4 sensor px per px) |
| window | WIN=480 SVGA px → 240×240, 160 crop | **WIN=160 CIF px → 128×128, 112 crop** |
| view across the frame | 640 sensor px | 640 sensor px in the window, 560 px in the 112 crop |
| sensor clock (CLKRC) | doubler, div 1 = 20 MHz | same |
| frame rate (nominal) | 25 fps (measured 22–25) | **50 fps** |
| bytes per frame / at full rate | 25.6 kB / 640 kB/s | **12.5 kB / 627 kB/s** |
| exposure | 700 (clamped by the sensor to 1 frame ≈ 40 ms) | **320 lines = 19 ms** (max 336 = 20 ms) |
| frame buffers | 2 | 3 (one frame of slack) |

Why CIF: per the OV2640 datasheet (v1.6, fig. 14–17), a frame is 672 lines in SVGA mode and 336 in CIF.
GLS03 ran at about 25 fps both in CIF at sysclk 10 MHz and in SVGA at sysclk 20 MHz. So a line takes
1190/sysclk in both modes, about 59.5 µs at 20 MHz, and CIF at the same 20 MHz clock gives 50 fps. That
clock is inside the datasheet's 24 MHz for SVGA and CIF (which is where its "CIF 60 fps" comes from).
Each CIF pixel is 4×4 sensor pixels, and the eye needs only 64–160 px across, so the coarser pixels cost
nothing.

The bandwidth is what decides the frame size. GLS03's USB Serial/JTAG measured about 0.85 MB/s, and PPG
records share the same port:

| frame sent | bytes | 50 fps | 60 fps | 100 fps |
|---|---|---|---|---|
| 160×160 | 25.6 kB | 1.28 MB/s ✗ | ✗ | ✗ |
| 128×128 | 16.4 kB | 819 kB/s (96 %, too tight) | ✗ | ✗ |
| **112×112** | 12.5 kB | **627 kB/s (74 %)** | 753 kB/s (89 %) | ✗ |
| 96×96 | 9.2 kB | 461 kB/s (54 %) | 553 kB/s | 922 kB/s ✗ |
| 64×64 | 4.1 kB | 205 kB/s | 246 kB/s | 410 kB/s |

To keep the same view at fewer bytes, choose 96×96 in menuconfig with WIN=160 and no crop. Use this for
Wi-Fi if 627 kB/s turns out too much there. `CAM FPS=25` on Wi-Fi sends exactly every other frame.

Run-time controls (from the live page, or typed into a serial terminal):
`CAM MODE=CIF WIN=160 CLK=1 EXP=320 GAIN=4 FPS=0 IR=100`.
- `MODE=CIF|SVGA` sets the sensor mode of the window.
- `WIN` is the window side in pixels of that mode: CIF 128–296, SVGA 128–600, 0 = stock framing. A
  smaller window gives a narrower view with finer pixels.
- `CLK` is the CLKRC divider: sysclk = 40 MHz/(n+1).
- `FPS` is a cap that can only skip whole sensor frames.

The camera H line reports `mode`, `fps_sensor`, `line_us`, `exp_max`, `win_min` and `win_max`. The live
page shows the exposure in ms and limits the exposure slider to one frame.

**Experimental:** `CLK=0` sets sysclk to 40 MHz, about 100 fps nominal with a 29.75 µs line. That is above
the datasheet clock, so treat it as experimental. USB cannot carry 112×112 at 100 fps, but `CLK=0 FPS=50`
gives 50 fps with half the rolling-shutter skew, at most 10 ms of exposure. Try it only after the
`CLK=1` numbers are confirmed.

## 2. Exposure, frame rate and brightness

- Exposure (`EXP`) counts sensor lines. At `CLK=1` a line is 59.5 µs, and a frame is 336 lines (CIF,
  20.0 ms) or 672 lines (SVGA, 40 ms).
- The datasheet (AEC register) says *"the maximum exposure time is 1 frame period even if TEX is longer."*
  If that holds, a long exposure does not slow the frame rate: it saturates at one frame. The old
  `EXP=700` in SVGA was already clamped to about 40 ms. The 22 fps seen then may have another cause. The
  exposure sweep in section 4 settles this. If the frame rate does fall above about 336, keep `EXP` at
  320 or lower.
- Brightness is proportional to exposure × gain × light. Going from SVGA at 25 fps to CIF at 50 fps halves
  the longest possible exposure (40 ms to 20 ms), so at the same gain and without IR the image is about
  2× darker. Raise `GAIN` for now, and fit the IR LED.
- Motion blur (estimate): the upper lid covers about 8–10 mm in the ~80 ms closing phase, about 12 cm/s
  on average with peaks around twice that. During one exposure it moves about 2–5 mm at 20 ms and about
  0.6–1.5 mm at 6 ms. With IR, aim for `EXP` 100–170 (6–10 ms) and enough LED current to keep the mean
  gray level around 80–150 at `GAIN` ≤ 8.
- Rolling shutter: rows are read 59.5 µs apart. The 160-line CIF window spans about 9.5 ms from top to
  bottom (row 0 is about 68 lines after VSYNC). A frame's `t_us` is the driver's VSYNC time, that is,
  the start of readout. This matters for sub-frame timing of the lid.

| EXP (lines) | CIF, CLK=1 | SVGA, CLK=1 | CIF, CLK=0 |
|---|---|---|---|
| 50 | 3.0 ms | 3.0 ms | 1.5 ms |
| 100 | 6.0 ms | 6.0 ms | 3.0 ms |
| 170 | 10.1 ms | 10.1 ms | 5.1 ms |
| 320 | 19.0 ms | 19.0 ms | 9.5 ms (clamped at 336 = 10 ms) |
| ≥ 336 / ≥ 672 | 20 ms (1 frame) | 40 ms (1 frame) | 10 ms |

## 3. IR illumination (to fit by hand)

### IR-cut filter

Seeed does not publish whether the XIAO Sense's OV2640 lens has an IR-cut filter. A forum reply lists
the lens as 1/4", 65° FOV, F2.4, with no filter information. Most 24-pin OV2640 modules of this kind have
a 650 nm IR-cut filter in the lens holder (Arducam's has an *"integral 650nm IR filter"*), which blocks
most 850 and 940 nm light. Until you test it, assume it is there.

- **Test:** point a TV remote (940 nm) or the IR LED at the lens in a dim room and watch the live view.
  A strong bright spot means no IR-cut filter, or a weak one. A faint or absent spot means the filter
  blocks IR. A lens with an IR-cut filter usually also shows a reddish or cyan reflection when you tilt it.
- **Options if the filter is there:**
  1. Replace the camera with an OV2640 24-pin 0.5 mm FPC module sold as "no IR-cut", "night vision" or
     "850 nm". Check the FPC length and orientation against the Sense board, and choose about 65° FOV.
     The 120–160° lenses distort heavily at 2–3 cm.
  2. Take the filter out of the stock lens holder. On some modules it sits at the bottom of the lens
     stack. Keep dust off the sensor. Refocus afterwards: the lens has to be refocused for 2–3 cm anyway,
     and IR focuses slightly behind visible light.
  3. Either way, consider an IR long-pass filter (720–850 nm glass or gel) in front of the lens. With
     only the LED's light reaching the sensor, a fixed exposure gives the same brightness whatever the
     room lights or a screen are doing. This is the main reason to use IR for this study.
- Newer XIAO Sense boards may ship with an OV3660 instead of the OV2640. The window and clock code here
  is OV2640-specific: check the sensor ID in the boot log of any new board.

### Wavelength and part

| | 850 nm | 940 nm |
|---|---|---|
| sensor response | higher, typically about 2–3× that at 940 nm for front-side-illuminated CMOS (OmniVision publishes no curve for the OV2640) | lower: needs about 2–3× the current or exposure |
| visible? | faint red glow when you look straight at it | invisible |
| part (same 0805 footprint) | **Vishay VSMY1850X01**, ±60° | **Vishay VSMY1940X01**, ±60° |

**Recommendation:** start with the **850 nm VSMY1850X01**, mounted off-axis (temple side, aimed at the eye
from about 30° to one side) at 10–20 mA. If participants notice the glow, which could act as a cue in a
cued-blink study, switch to the 940 nm VSMY1940X01 on the same pads with about twice the current.
VSMY1850X01: 850 nm, 2.0 × 1.25 mm, 100 mA DC maximum. Distributors give a radiant intensity of
5 mW/sr minimum and about 10 mW/sr typical at 100 mA. The Vishay eye-safety note gives 15 mW/sr at
absolute maximum ratings.

### Circuit

```
 3V3 ──[R]──►|── LED (anode → cathode)
                 │
                 D
 GPIO1 (D0) ─[100 Ω]─ G   N-MOSFET, logic level (AO3400A / DMG2302U / SI2302)
                 │ S
 100 kΩ G→GND    GND
```

- R = (3.3 V − V_F − ~0.05 V) / I, with V_F about 1.5 V at 850 nm. Use **180 Ω for about 10 mA (start
  here)**, 91 Ω for about 20 mA, or 47 Ω for about 35 mA. Do not go below 33 Ω (about 50 mA) without
  redoing the safety numbers below.
- The 100 kΩ pull-down keeps the LED off during reset and boot.
- For a quick prototype at 10 mA or less, you can drive the LED directly from the GPIO through about
  150–180 Ω.
- Firmware: `CONFIG_PPG_CAM_IR_GPIO=1` (D0, set in `sdkconfig.camera`). The live view's camera settings
  have an IR LED switch and brightness slider (`CAM IR=0` = off). The LED is on from
  START to STOP only. `CAM IR=0..100` sets its brightness with 312 kHz PWM (period 3.2 µs, far below
  one 59.5 µs line, so no rolling-shutter bands). 100 = plain DC, which is preferred.
- Keep the light **steady**, without frame sync or low-frequency PWM. The OV2640 has a rolling shutter
  and the module has no STROBE pin, so a pulse synced to frames would have to cover every row's exposure
  anyway. A low-frequency PWM would draw bands across the image. A steady LED also stays out of the way
  of the MAX30102: its ambient-light cancellation removes DC light, and its own LED is around 880 nm.
  Check that the PPG's DC level and the S line's `alc_ovf` don't change when the IR LED comes on.

### Eye-safety limit

Limits (IEC 62471:2006 exempt group RG0, the same as ICNIRP and EU Directive 2006/25/EC for these terms):
- **Cornea/lens IR hazard, 780–3000 nm:** E_IR ≤ **100 W/m² = 10 mW/cm²** for exposures over 1000 s, which
  covers all-day wear. For t ≤ 1000 s the limit is 18000·t^−0.75 W/m². This is the limit that binds for
  IR LEDs.
- **Retinal thermal, 780–1400 nm, weak visual stimulus:** L_IR ≤ 6000/α W·m⁻²·sr⁻¹. Vishay's eye-safety
  note: for single IREDs above 850 nm this limit "results in not limiting higher values", so the cornea
  limit decides. Vishay also notes that arrays need care against the cornea limit.

Irradiance at the eye from a small LED: E [mW/cm²] = I_e [mW/sr] / d² [cm²].

| distance | intensity at the 10 mW/cm² limit | at a 1 mW/cm² design target (10× margin) |
|---|---|---|
| 2.0 cm | 40 mW/sr | 4 mW/sr |
| 2.5 cm | 62 mW/sr | 6 mW/sr |
| 3.0 cm | 90 mW/sr | 9 mW/sr |

For the VSMY1850X01 or VSMY1940X01 (about 0.1 mW/sr per mA, typical):

| LED current | I_e (approx.) | E at 2 cm | E at 2.5 cm | share of the 10 mW/cm² limit at 2 cm |
|---|---|---|---|---|
| 10 mA | 1 mW/sr | 0.25 mW/cm² | 0.16 | 2.5 % |
| 20 mA | 2 mW/sr | 0.5 mW/cm² | 0.32 | 5 % |
| **40 mA (recommended maximum)** | 4 mW/sr | 1.0 mW/cm² | 0.64 | 10 % |
| 100 mA (absolute maximum, 15 mW/sr worst case) | 15 mW/sr | 3.75 mW/cm² | 2.4 | 37 % |

**Rule:** one LED of this type at up to **40 mA DC** gives at most about 1 mW/cm² at the cornea at 2 cm,
10× below the limit. Even at its absolute maximum of 100 mA a single LED stays below the exempt limit.
The irradiances of several LEDs add. These are calculations from datasheet intensities and the
inverse-square law, which is only approximate at 2 cm. **If the IRB needs a number, measure it**: put an
optical power meter with a photodiode sensor at the eye position and read mW/cm².

## 4. Test procedure (board plugged in)

Close the NBE gateway first, because it holds the serial port. Use the repo's venv
(`.venv/bin/python`); pyserial is the only extra dependency.

1. **Build and flash.** For USB only:
   ```bash
   cd firmware/esp-idf/ppg_stream && rm -f sdkconfig
   idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.camera" build
   idf.py -p /dev/ttyACM0 flash
   ```
   For hybrid (Wi-Fi + USB), as before: `python ../../tools/nbe_node.py configure GLS03 --camera ...`,
   then `idf.py build flash`. On this PC, activate IDF with `source ~/.espressif/tools/activate_idf_v6.1.sh`
   and call `python $IDF_PATH/tools/idf.py ...`.
2. **Baseline, as flashed (30 s):** `python scripts/cam_bench.py --port auto --seconds 30`
   Expected:
   - 49.5–50.5 fps, period 20.0 ms, jitter under 0.5 ms
   - **missing under 1 %** (sensor/driver 0, link 0)
   - 112×112, about 630 kB/s
   - PPG at its configured rate with **0 missing**
   - board: `cam_sfps≈50`, `cam_miss=0`, `cam_tx_ms` well under 20 (USB build), `tx_drop` not growing

   If `cam_sfps` is about 25, the CIF timing prediction is wrong. Check the board log for `FB-SIZE` or
   `EV-VSYNC-OVF`, and try `--cam "MODE=CIF CLK=0 FPS=50"`.
3. **Exposure sweep:** `python scripts/cam_bench.py --port auto --seconds 10 --sweep-exp 50,100,170,250,320,336,400,700`
   Expected: fps stays at about 50 for every value, and mean gray rises roughly in proportion up to about
   336, then stays flat. If fps drops above about 336, the sensor stretches the frame instead of
   clamping. Note the knee and keep `EXP` below it. Then repeat with the IR LED at 10 and 20 mA and pick
   the shortest `EXP` that gives a mean gray of about 80–150 with under 1 % saturated pixels.
4. **Same view in the old mode, for comparison:** `--cam "MODE=SVGA WIN=320 CLK=1"`. Expected: about
   25 fps with the same view and finer pixels. This confirms the 2× speed-up comes from the mode.
5. **Window and view:** with the glasses on, use the live page "Field of view" control: WIN 128–296 in
   CIF. Smaller windows show finer pixels and a narrower view. Keep the eye about 64–100 px wide in the
   112 frame.
6. **Through the gateway (USB, end to end):** start the gateway and record a 2-minute trial. The live
   page should show about 50 fps with "0 dropped". Then run
   `python scripts/cam_bench.py --frames data/sessions/<id>/<role>_GLS03_cam.frames`.
7. **Wi-Fi (hybrid board, gateway closed):** record a 2-minute trial over Wi-Fi and run `--frames` on it.
   Nothing has been measured over Wi-Fi yet. If the result is above 1 % missing or the live view lags
   for seconds, set `CAM FPS=25`, or build the 96×96 variant from section 1. PPG is never dropped for
   frames (`CONFIG_PPG_MQTT_CAM_SHARE_PCT`), so check that the PPG missing count stays at 0.

Pass criteria: ≥ 50 fps (≥ 30 acceptable), under 1 % missing, PPG 0 missing.
`cam_bench.py` exits non-zero if a run misses them. Add `--json runs.jsonl` to keep the numbers.

## Sources

- OV2640 datasheet v1.6 (OmniVision): frame timing (Tline 1190/595 Tclk, Tframe 672/336 lines), CLKRC,
  and the AEC "maximum exposure time is 1 frame period" note. Copy:
  https://files.waveshare.com/wiki/common/OV2640DS_en.pdf
- esp32-camera driver 2.1.8, `sensors/ov2640.c` `set_window()`: CLKRC doubler with divider 3 for CIF
  and 7 for SVGA on the S3 for gray8.
- Vishay, *Eye Safety* application note 81935: IEC 62471 cornea/lens limit of 100 W/m², the retinal
  thermal limit not binding above 850 nm, and the per-part maximum intensities (VSMY1850X01 15 mW/sr).
  https://www.vishay.com/docs/81935/eyesafe.pdf
- VSMY1850X01 product page: https://www.vishay.com/en/product/83317/. Intensity at 100 mA is from
  distributor listings (5 mW/sr minimum, about 10 mW/sr typical): check the datasheet.
- XIAO ESP32-S3 Sense lens specs (forum, unofficial): https://forum.seeedstudio.com/t/what-is-the-specification-of-ov2640-lens-xiao-esp32s3-sense/280341
- Arducam OV2640 module with an integral 650 nm IR filter: https://www.arducam.com/product/arducam-ov2640-camera-module-2mp-mini-ccm-compact-camera-modules-compatible-with-arduino_m0031esp32-esp8266-development-board-with-dvp-24-pin-interface_/
- Seeed wiki, XIAO ESP32-S3 (OV2640 discontinued, OV3660 on newer units): https://wiki.seeedstudio.com/xiao_esp32s3_getting_started
