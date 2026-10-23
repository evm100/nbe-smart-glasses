# Setup & demo checklist

Work top to bottom. Each step says what to **verify**; if something doesn't
match, the **if not** note says where to look. Steps 1–6 need no hardware.

---

## 0. Requirements

- **Python 3.10 – 3.13** (`python --version`). On Windows install from python.org and tick "Add to PATH".
- Nothing else system-wide. All packages go into a project-local virtual environment.
- Internet only for the one-time `pip install` (the web UI works offline afterwards).

## 1. Install

```bash
cd smart-glasses                      # the unzipped folder
python -m venv .venv
source .venv/bin/activate             # Windows PowerShell: .venv\Scripts\Activate.ps1
                                      # Windows cmd:        .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e ".[dev]"               # code + web UI + test tools
sglab init
```

- **Verify:** `sglab --help` lists the commands. `sglab init` prints the data root and catalog paths, and `config/pipeline_layer1.json` / `pipeline_layer2.json` now exist.
- **If not:**
  - *"sglab: command not found"*: the venv isn't active. Activate it again in every new terminal.
  - PowerShell refuses to run Activate.ps1: run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

```bash
pytest -q
```

- **Verify:** `50 passed` (takes about 1 minute; several tests record from simulated devices in real time). A single deprecation warning from the test client is harmless.
- **If not:** the first failing test name tells you which part (`test_layer1` = filtering, `test_layer2` = features, `test_sglab` = recorder / catalog / web). Send me the output.

## 2. Terminal recording with a simulated device (~1 min)

```bash
sglab record sim sim_glasses
```

1. Answer the questions. At "Subject ID", type `S001`; it then asks a few de-identified details for the new subject.
2. Press Enter to start.
3. Type `?` → status table.
   - **Verify:** both streams show rate ≈ 100 Hz, `dropped 0`, `bad lines 0`.
4. Type `c`, then `bp 120/80 72`, `ox 98 72`, and `m test marker`.
   - **Verify:** each prints "logged … at t=…s".
5. Type `bp 80/120`.
   - **Verify:** it's rejected ("diastolic must be below systolic").
6. Wait ~40 s, then type `q`. Answer quality / notes / keep.
   - **Verify:** it prints `Saved <id>` and a folder under `data/raw/`.

Then:

```bash
sglab list            # verify: your session, 2 streams, BP = 1
sglab show S001       # verify: streams table + your events with t (s)
sglab process --all   # verify: "[ok] … good beats, HR ≈ 72 bpm" for both streams
```

- **Look at:** `data/raw/<id>/`. It should contain `session.json`, `events.csv`, one `.csv` and one `.rawlog` per stream. Open a `.rawlog`: every line looks like the examples in `firmware/PROTOCOL.md`.

## 3. Web UI on your network

```bash
sglab serve --lan --token auto
```

- **Verify:** it prints a `this computer` link and one `local network` link per network interface, each ending in `?token=…`.
- Open the network link from another device (phone or laptop). It should land on the Sessions page; after that the token is remembered by a cookie.
- **If not:**
  - *Page doesn't load from the other device:*
    - Is the server computer's firewall allowing inbound TCP 8000? On Windows, accept the prompt for Python on first run, or allow it in Windows Defender Firewall. On Linux: `sudo ufw allow 8000/tcp`.
    - Are both devices on the same network/VLAN (guest Wi-Fi often isolates clients)?
  - *"Access token required"*: you opened the URL without `?token=…`.
  - *Wrong IP listed*: use the server's address on the network you're actually on (`ipconfig` / `ip addr`).
- Leave this server running for steps 4–6. Ctrl-C stops it.

## 4. Run a trial from the browser

**New trial:**

1. Tick **sim** and **sim_glasses**.
   - **Verify:** the real devices show "○ port not found" until hardware is plugged in.
2. Choose **＋ new subject…**.
   - **Verify:** a sub-form appears with the next free ID (e.g. S002).
3. Pick protocol `cold_pressor`.
   - **Verify:** Trial number updates automatically (1 for a new subject + protocol).
4. Click **Start recording**.

**Live page:**

1. **Verify:** the clock runs, both streams show "● streaming" at ~100 Hz, and the live plot scrolls with pulses visible.
2. Open the Sessions page in another tab or on another device.
   - **Verify:** the red "Recording in progress" banner is there.
3. Click **Cuff inflation started**, then log BP 125/82/74, SpO2 98, and a quick marker.
   - **Verify:** each shows "logged … at … s" and appears at the top of the event log.
4. Enter BP 70 / 90.
   - **Verify:** red error, nothing logged.
5. Let it run ≥ 40 s (Layer 2 needs 30 s for one epoch), then **Stop** → confirm.

**Finish page:**

1. **Verify:** sample counts and 0 dropped / 0 bad lines.
2. Fill in quality / notes, keep "Keep" and "Process now" ticked, then **Save session**.
   - **Verify:** you land on the session page. The streams table first shows "processing: running"; after a few seconds, refresh and both streams are processed.

**If not:**

- *"a recording is already recording"*: another browser started a trial. Use the banner link to get to it.
- *"could not start: … port …"*: the port is busy (another program or a terminal `sglab record` has it) or unplugged.

## 5. Read the results (what good looks like)

On the session page:

| Panel | What to check | Red flags |
|---|---|---|
| Signal (filtered) | Regular pulses with a visible dicrotic notch on the finger. Vertical lines at your events, with BP values labelled. | Flat line, clipped tops, or shading everywhere (Layer 1 rejected it all) |
| Signal (raw counts) | DC level in the tens of thousands with a small ripple | Stuck at 0 or 262143 (saturated: LED current too high) |
| Heart rate | Beat dots scattered ±5 bpm around a smooth epoch line. Reference diamonds sit near the line. | Dots at half or double the HR (missed or false beats) |
| Features vs cuff BP | The feature line (top) and the cuff points (bottom) share the time axis | "No epochs": recording shorter than 30 s |
| QA figures | Open *Beats*: accepted beats form a tight bundle | Fat bundle, or many rejected beats that look normal |

Simulated data shows a slow drift in `dia_rel` by design. On real data, use this panel to see which features move when the cuff BP moves.

## 6. Robustness checks (2 min, worth doing once)

1. **Crash recovery.** Start a trial (web or terminal), then kill the server or close the terminal mid-recording. Then run `sglab recover`.
   - **Verify:** the session appears in `sglab list` with status `incomplete`, and its CSVs have data.
   - Expect: operator events typed during that run are lost; the samples are kept.
2. **Catalog rebuild.** Stop the server, delete `data/catalog.sqlite`, then run `sglab reindex`.
   - **Verify:** the same sessions are listed. Processing history is lost; just run `sglab process --all` again.
3. **Import an old CSV:**

   ```bash
   sglab import path/to/old.csv --subject S001 --protocol rest
   ```

   - **Verify:** it appears in the list and processes.
   - **If not:** add `--channel ir=<IR column name>` and `--time-col <col> --time-unit ms`.

## 7. First real hardware

Flash `firmware/esp-idf/ppg_stream` (see its README: `idf.py set-target esp32s3`, `menuconfig`, `build flash`). Then:

1. Plug in the ESP32 and run `sglab ports`. Note the port (Windows `COMx`, Linux `/dev/ttyUSB0` or `/dev/ttyACM0`, macOS `/dev/cu.usbserial-…`).
2. Put it into `config/lab.toml` under `[devices.finger]` (or `glasses`), and set `baud` to match the firmware.
   - On Linux, if you get "permission denied", run `sudo usermod -aG dialout $USER`, then log out and in.
3. Run a short smoke test:

   ```bash
   sglab record finger --duration 15 --yes --meta subject_id=S001 --meta protocol=other --meta operator=EV --meta quality=usable --meta keep=n
   ```

   (`keep=n` saves it as discard, so the test doesn't clutter the lists.)

   | Verify | If not |
   |---|---|
   | No "no H header" warning | Firmware must answer `START` with an `H` line; wrong baud; wrong port |
   | Rate ≈ the `fs` in the header | Rate too low: baud too slow for fs (use 921600 or native USB) or the ESP32 loop is too slow |
   | `dropped` = 0 | Link loss: cable/USB hub, baud, or the PC is too busy |
   | `bad lines` = 0 | Checksum errors: baud mismatch or the firmware checksum is wrong (compare with the PROTOCOL.md examples) |

4. Check the clock fit in the saved `session.json` (`clock_fit.b` ≈ 1.000, `envelope_resid_ms_p50` of a few ms over USB).
   - If `b` is off by more than ~0.1 %, the firmware's `t_us` doesn't match its claimed `fs`. Check the per-sample time calculation.
5. Identify the channels. Cover the sensor or look at DC levels: on skin, IR normally reads higher than red. If they're swapped, fix it in the firmware (preferred) or in the `ch=` order.
6. Then run a real trial from the web UI. Done.
