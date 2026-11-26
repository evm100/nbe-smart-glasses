# Cued sessions

A cued session is a scripted protocol the live trial page plays to the wearer (big instruction text,
a countdown, a beep per cue, an on-screen gaze target) while the glasses record. The time of every cue
is saved with the recording on the **same clock as the camera frames**, so the cue log is approximate
ground truth for training and scoring a blink / eye-openness detector before anyone labels frames.

Code: `src/sglab/cues.py` (protocols, clock maths, the log), `TrialManager.cue_*` in `src/sglab/control.py`,
routes in `src/sglab/web/trials.py` (live) and `src/sglab/web/review.py` (saved sessions), UI in
`trial_live.html` (player) and `session.html` (steps table, purple lines on the eye plot, frame viewer).
Tests: `tests/test_cues.py`.

## Protocols

`config/cue_protocols.json`, checked every time the live page loads it (a mistake shows a readable
message, it never breaks the page). `cued_blink_v1` is the lab's ~8-min plan:

| # | step | cues | label | negative |
|---|------|------|-------|----------|
| 1 | look ahead, blink naturally | none, 60 s | spontaneous_blink | |
| 2 | blink once at each beep | 20 × every 3 s | cued_blink | |
| 3 | close at the beep, open at the low tone | 10 × every 4 s, "open" tone 1 s later | long_blink | |
| 4 | half blink at each beep | 10 × every 3 s | half_blink | |
| 5 | look where the arrow points | down/ahead/up/ahead/left/ahead/right/ahead every 3 s, 90 s | gaze_shift | yes |
| 6 | squint, smile, frown | one each 10 s | facial_expression | yes |
| 7 | talk and turn the head | none, 60 s | talk_head_turn | yes |
| 8a | glasses off, back on | 2 prompts, 30 s | glasses_off_on | yes |
| 8b | step 2 again (`same_as`) | 20 × every 3 s | cued_blink | |

Each step has a 3-s lead-in (instruction shown, no cues) so its first cue is not a surprise. The format
is described at the top of the JSON file: `repeat` (every N s, count times, optional release tone),
`sequence` (items in turn, optionally looping, with `gaze` targets) and `timed` (items at fixed times).

## What is saved

`<session folder>/cues.json` (rewritten after every change, so a closed page or a crash keeps what was
logged so far) and `cues.csv` (one row per entry). Several runs per recording are fine (e.g. two lighting
conditions). Run `status`: `completed`, `aborted` (operator stopped it, or the page was closed),
`partial` (the recording stopped / was saved while it ran, or a new run was started), `running`.

```jsonc
{"schema": 1, "clock": "...", "runs": [{
  "run_id": "3f9a01c2", "protocol_id": "cued_blink_v1", "protocol_name": "...",
  "protocol": { ...the expanded protocol that was played: steps with every cue time... },
  "status": "completed", "status_reason": null, "operator": "olga",
  "created_t_s": 12.4, "ended_t_s": 520.9, "t_start": 13.9, "t_end": 500.9,
  "client": {"user_agent": "...", "audio_method": "getOutputTimestamp", "audio_output_latency_s": 0.02, ...},
  "sync": [{"p0_ms": ..., "p1_ms": ..., "t1_s": ..., "t2_s": ...}, ...],      // raw clock pings
  "clock_fit": {"a": ..., "b": ..., "drift_ppm": 12.3, "n": 60, "n_used": 52,
                "delay_min_ms": 0.7, "err_ms": 1.4, ...},
  "steps": [{"step": 1, "attempt": 0, "step_id": "blink_cued", "label": "cued_blink", "negative": false,
             "t_start": 76.9, "t_end": 139.9, "status": "done", "n_cues": 20, "n_cues_planned": 20}, ...],
  "entries": [
    {"key": "s1.0.c4", "kind": "cue", "step": 1, "attempt": 0, "step_id": "blink_cued", "cue": 4,
     "action": "blink", "text": "Blink", "tone": "beep",
     "t_s": 91.9013,          // when the beep reached the speaker, session clock (s)
     "screen_t_s": 91.8952,   // animation frame that drew the cue on screen
     "t_recv_s": 92.1,        // when the server got the entry (cross-check only)
     "perf_ms": ..., "planned_perf_ms": ..., "screen_perf_ms": ...},
    ...]}]}
```

Entry kinds: `run_start`, `step_start` (with the step-change chime), `cue`, `step_end` (`status` done /
skipped / interrupted / aborted), `pause`, `resume`, `skip`, `abort`, `run_end`, `page_hidden`,
`page_visible`. Cues that were scheduled but then cancelled by Pause/Skip/Stop carry `cancelled: true`;
a cue that played late (page in the background) carries `late_ms` and its real time. Gaze cues carry
`gaze` (ahead / up / down / left / right). Pause ends the step attempt; Resume plays the step again as a
new `attempt`, so each attempt is one clean block.

### API

    GET /api/sessions/{sid}/cues                 the document above ({"runs": []} when none)
    GET /api/sessions/{sid}/cues?stream=<camera>  + frame_i / screen_frame_i: the recorded frame captured
                                                  closest to each entry's t_s (sglab.video.frame_times)
    GET /api/sessions/{sid}/cues?format=csv      cues.csv
    GET /api/cue-protocols                       protocols with every cue expanded (operator)
    POST /api/trials/{tid}/cues/ping | start | {run_id}/log | {run_id}/end,  GET /api/trials/{tid}/cues
                                                  (the live page; see the route docstrings)

For a labeling tool: take `entries` with `kind == "cue"` and not `cancelled`, look up frames with
`?stream=`, and search a window *after* each cue — the wearer's reaction to a beep (~150-400 ms, longer
for a long blink) is behaviour, not clock error. `step.label` / `negative` mark the stretches that must
not count as blinks; `steps[].t_start/t_end` bound them.

## How the timing works

The recorder's clock is `t_s` = seconds since the recording started on the server's monotonic clock.
Camera frames get `t_s` from `sglab.video.frame_times`: device capture time moved onto the server clock by
the smallest (arrival − device-time) seen within ±60 s, i.e. capture time + the least link delay.

The browser only knows `performance.now()`. To convert, the page sends clock pings — 8 at the start, one
every 10 s while running, 4 at the end. The server stamps each ping when the request enters the app (`t1`)
and when the handler returns (`t2`); with the browser's send/receive times `p0`/`p1`:

    offset = ((t1 − p0) + (t2 − p1)) / 2      delay = (p1 − p0) − (t2 − t1)      |error| ≤ delay / 2

`fit_clock` keeps pings whose delay is within 2× (or +4 ms of) the best one, and fits
`t_s = perf + a + b·perf` (b = the two computers' clock drift, fitted once the good pings span ≥ 60 s;
typical drift is tens of ppm = tens of ms over 10 min, so a single start-up offset would not be enough).
The raw pings are saved, so the fit can be redone.

Each cue's browser time is the moment the wearer got it:
* **beeps** are scheduled 250 ms ahead on the WebAudio clock (so a busy page or a slow poll cannot delay
  them); `AudioContext.getOutputTimestamp()` maps that audio time to the `performance.now()` time the
  sound card plays it (fallback: `currentTime` + `baseLatency` + `outputLatency`);
* **screen changes** (step text, cue word, gaze target) are logged with the `requestAnimationFrame`
  time of the frame that drew them (`screen_t_s`).

## Expected error (cue `t_s` vs. camera-frame `t_s`)

| source | size | notes |
|--------|------|-------|
| browser ↔ server offset | ≤ delay/2 of the pings used: ~1-3 ms on the lab LAN, ≤ 10-20 ms over the internet | reported per run as `clock_fit.err_ms` and on both pages |
| drift between pings | < 1 ms | linear fit, pings every 10 s |
| sound output | ~0-10 ms on built-in / wired speakers (unreported buffering); **100-300 ms on Bluetooth** | don't use Bluetooth; `client.audio_*` records what the browser reported |
| screen | +1-2 refresh periods after `screen_t_s` (17-35 ms at 60 Hz) | beeps are the precise cue; screen times are for gaze targets |
| frame times | frames sit late by the least link delay (few ms USB, ~5-20 ms Wi-Fi) + where in the exposure the firmware stamps `t_us` (unverified; up to one frame period, 40 ms at 25 fps) | camera-side, not cue-side |
| `performance.now()` resolution | 0.1 ms (Chrome), 1 ms (Firefox); 100 ms with Firefox `resistFingerprinting` | |

Net: beep times within about **±10 ms** of the frame clock with wired audio on the LAN (plus the
camera-side terms above, which are the same for every cue and so can be calibrated once — e.g. with a
light flashed in front of the camera at a logged time). Measured with headless Chrome against a local
server: ping delay 0.6-3 ms, `err_ms` 0.8-1.7, beep spacing exact to 0.1 ms.

Things that make it worse and how they show up: page in the background (timers throttled — beeps come
late and are flagged `late_ms`, `page_hidden` entries); a slow or busy connection (larger `err_ms`; the
page says so); a reloaded page (run saved as `aborted` with what was sent; at most the last ~2 s of
entries are lost if the final send fails).
