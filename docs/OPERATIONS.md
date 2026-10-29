# Operations reference

Everything you can run, in one place. Run commands from inside the repo
(the tools find `config/lab.toml` by walking up from the current folder;
or set `SGLAB_ROOT=/path/to/smart-glasses`).

## 1. `sglab` – lab tool

| Command | What it does |
|---|---|
| `sglab init` | Create `data/` folders, the SQLite catalog, and `config/pipeline_layer{1,2}.json` |
| `sglab ports` | List serial ports on this computer (to fill in `config/lab.toml`) |
| `sglab record DEV [DEV…]` | Record from devices named in `lab.toml` (`finger`, `glasses`, `sim`, `sim_glasses`) or raw ports (`COM5`, `/dev/ttyUSB0`) |
| `  --meta key=value` | Pre-fill any metadata field (repeatable) |
| `  --duration 60` | Stop automatically after N seconds |
| `  --yes` | Don't ask anything: use defaults / `--meta` (for scripted tests) |
| `  --process` | Run Layers 1–2 right after saving |
| `sglab subjects` | List subjects with session counts |
| `sglab subject add [--id S005]` | Create a subject interactively |
| `sglab list [--subject S001] [--protocol rest] [--all]` | List sessions (`--all` includes discarded) |
| `sglab show <id-or-part>` | Metadata, streams, events and processing runs of one session |
| `sglab process <id…>` / `--all` | Run Layers 1–2; skips streams already processed with the same code+config |
| `  --force` / `--no-plot` | Re-run anyway / skip PNG figures |
| `sglab dataset build [--name bp] [--sessions T05 …] [--config my.json]` | Layer 3: every cuff/pulse-ox reading × every processed stream → `data/datasets/<name>/` (see `docs/pipeline/PIPELINE.md`) |
| `sglab dataset list` | Datasets built so far, with readings / valid rows per site |
| `sglab import file.csv --subject S001 --protocol rest [--role finger] [--channel ir=IR] [--time-col ms --time-unit ms]` | Bring an existing CSV into the catalog |
| `sglab reindex` | Rebuild the catalog from `data/raw/*/session.json` |
| `sglab recover` | Finish recordings interrupted by a crash/closed terminal (status `incomplete`) |
| `sglab serve` | Web UI on this computer only: http://127.0.0.1:8000 |
| `sglab serve --lan` | Web UI on all network interfaces; prints the LAN URLs |
| `sglab serve --lan --token auto` | …and require a random access token (printed link contains it; a cookie remembers it for 30 days) |
| `  --port 8080` / `--token mysecret` / env `SGLAB_TOKEN` | Other port / fixed token |

### Keys during `sglab record` (terminal)

| Type | Logs |
|---|---|
| `c` | cuff inflation started |
| `bp 118/76 70` | cuff result: SYS/DIA [pulse] |
| `ox 98 72` | pulse-oximeter: SpO2 [HR] |
| `m <text>` | marker |
| Enter or `?` | live status table |
| `help` | key list |
| `q` (or Ctrl-C) | stop → after-recording questions → save |

## 2. Web UI (`sglab serve`)

| Page | Role | What you do there |
|---|---|---|
| **New trial** `/trial/new` | operator | Tick devices (nodes: online / offline / in use by …; local: port found / simulated), pick or create a subject, fill the session fields (last answers remembered per user, trial auto-numbered), **Start recording** |
| **Trial** `/trials/<id>` | operator | while recording: clock, per-stream rate / samples / dropped / bad lines, live waveforms, cuff / pulse-ox / marker buttons, event log, **Stop**; after Stop: after-recording questions, *Process now*, **Save session** |
| **Devices** `/devices` | viewer | live table of nodes + local devices: state, trial / owner, RSSI, last seen, firmware |
| **Sessions** `/` | viewer | all recordings, filter by subject / protocol / *recorded by me*, include discarded |
| **Session** `/sessions/<id>` | viewer | metadata, streams, plots, events, QA figures; Process (operator), Force re-process (admin) |
| **Subjects** `/subjects` | viewer | subject table |
| **Nodes** `/admin/nodes` | admin | register nodes, edit name / site / notes, enable / disable, live state, credentials zip, rotate certificate, stop a stray stream |
| **Users** `/admin/users` | admin | add users, change role, deactivate, reset password, all API tokens |
| **Audit** `/admin/audit` | admin | logins, trials, node / user changes, credential downloads |
| **Account** `/account` | everyone | change password, create / revoke API tokens |
| **API docs** `/docs` | – | every JSON endpoint |

A banner on every page lists running and unsaved trials (operators+), with a link to each.

Rules:
- **Several trials can run at once**; each device (serial port, simulator, node) is claimed by
  one trial from Start until Stop. Any operator can open, stop and save any trial (the audit log
  and the session record who started and who saved it).
- With accounts on, the *operator* field is the logged-in user (`operator` in the catalog,
  `recorded_by` in session.json); without accounts it's the typed initials as before.
- Run **one** server process (no `--workers`). Stopping it while recording leaves the data in
  `data/raw/_incoming/` → `sglab recover`.
- Nothing can be deleted from the web: recordings are saved as *discard* instead; users and nodes
  are deactivated.

### JSON API (for scripts / notebooks)

Auth: with accounts on, send `Authorization: Bearer nbe_…` (token from *Account*); in
single-user mode with `--token`, send `x-sglab-token: <token>`.

| Endpoint | Role | |
|---|---|---|
| `GET /api/sessions`, `GET /api/sessions/{id}` | viewer | catalog |
| `GET /api/sessions/{id}/signal?stream=…&source=raw\|filtered` | viewer | decimated signal on the session clock |
| `GET /api/sessions/{id}/hr`, `…/epochs?stream=…` | viewer | beat HR, epoch features |
| `GET /api/devices` | viewer | devices with live state + active trials |
| `GET /api/trials`, `GET /api/trials/{id}`, `…/live` | operator | active trials, one trial's state, live waveforms |
| `POST /api/trials/{id}/event` `{"kind":"bp","sys_mmhg":120,"dia_mmhg":80}` | operator | log an event |
| `POST /api/trials/{id}/stop` | operator | stop |
| `GET /api/nodes`, `POST /api/nodes`, `PATCH /api/nodes/{id}` | admin | node registry |
| `GET /api/nodes/{id}/credentials`, `POST /api/nodes/{id}/rotate`, `GET /api/provisioning` | admin | flashing (used by `nbe_node.py`) |
| `POST /api/auth/token` `{"username","password","label"}` | – | get an API token (rate-limited) |
| `GET /api/me`, `GET /healthz` | – | who am I / server + broker status |

### Server administration (`sglab`, on the server)

| Command | |
|---|---|
| `sglab user add <name> --role admin\|operator\|viewer` | create an account (asks for the password) |
| `sglab user list` / `passwd <name>` / `set <name> --set-role R [--inactive\|--active]` | manage accounts |
| `sglab pki init` / `core-cert` / `show` | device CA (once), the server's MQTT certificate, overview |
| `sglab node list` / `register <ID> --site finger` / `rotate` / `enable` / `disable` / `zip <ID>` | node registry from the shell |
| `sglab sim-node <ID> [--duration 60]` | simulated ESP32 using that node's certificate, through the real broker |
| `sglab doctor` | checks data folder, accounts, CA, broker connection, provisioning |
| `sglab snapshot [--keep 14]` | consistent catalog copy for backups (the systemd timer runs it nightly) |

Deployment config lives in `config/server.toml` or the file in `$SGLAB_SERVER_CONFIG`
(template: `deploy/server.example.toml`). Without it, sglab runs in single-user lab-PC mode.

## 3. Pipeline-only tools (no catalog needed)

| Command | |
|---|---|
| `ppg-layer1 synth demo.csv [--duration 300 --hr 72 --dia-amp 0.6 0.25]` | synthetic MAX3010x-like recording |
| `ppg-layer1 run <csv-or-folder> -o out/` | Layer 1 only |
| `ppg-layer2 run <csv-or-folder> -o out/` | Layers 1+2 |
| `ppg-layer1 config` / `ppg-layer2 config` | print default settings JSON |

## 4. Files you edit

| File | Controls |
|---|---|
| `config/lab.toml` | device names → ports + roles, baud, simulated devices, reference instrument models, `[ui] quick_markers = [...]` |
| `config/session_fields.toml` | the questions asked before/after each recording (CLI and web) |
| `config/server.toml` / `/etc/nbe/server.toml` | deployment: data root, accounts, public URL, MQTT, PKI, provisioning (not committed) |
| `config/pipeline_layer1.json`, `pipeline_layer2.json` | filter bands, quality thresholds, epoch length… (changing them makes `process` redo affected sessions) |
