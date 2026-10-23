# Data collection guide

## The goal of the dataset

To learn blood pressure from PPG, the model must see **the same person at
different blood pressures**, each paired with a cuff reading. A dataset where
everyone sits still at rest teaches the model nothing about BP changes – it
can only learn who has high or low BP on average, which is a much harder and
less useful problem with ~15 people.

So each subject should go through conditions that **move BP**, with cuff
readings before, during and after each change.

## Suggested session plan (per subject, adjust with your PI / IRB protocol)

| Protocol (`protocol` field) | What happens | Expected change |
|---|---|---|
| `rest` | 5 min seated, quiet | baseline |
| `paced_breathing` | 6 breaths/min for 3 min | HR/BP oscillation |
| `cold_pressor` | hand in ice water 1–2 min, then recovery | SBP usually rises noticeably |
| `posture_change` | supine → standing | transient drop then rise |
| `post_exercise` | after 2–3 min stairs/squats, recover seated | SBP up then back down |
| `mental_stress` | serial-7 subtraction / Stroop for 3 min | moderate rise |
| `valsalva` | strain 15 s (only if approved) | large swings |

Repeat sessions on different days: the model has to cope with day-to-day
changes (sensor placement, temperature, hydration).

## Reference measurements

- **Cuff BP:** one reading takes ~30–40 s. Type `c` when inflation starts and
  `bp SYS/DIA PULSE` when it finishes; the BP applies to that interval. Aim for
  a reading every 1–2 minutes, more often around a change. Use the same arm
  every time, at heart level.
- **Pulse oximeter:** type `ox SPO2 HR` at the same moments (or whenever it is
  stable). If the oximeter can stream data over USB/Bluetooth, that's better –
  add it as another stream later.
- **Markers:** `m <text>` for anything that happens: "start ice", "subject moved",
  "glasses adjusted", "talking".

## Before each session (checklist)

- [ ] Subject exists (`sglab subjects`); new subjects get a code `S###` – **never names**
- [ ] Sensors connected, `sglab ports` shows them, `config/lab.toml` ports correct
- [ ] Short test: `sglab record finger glasses --duration 10 --yes ...` → rate ≈ fs, 0 dropped
- [ ] Room temperature noted (cold hands = weak finger PPG)
- [ ] Subject seated 5 min before the first baseline cuff reading

## What gets saved

```
data/raw/20261001_190522_S003_cold_pressor_T01/
├── session.json          everything about the session (metadata, devices, stats, clock fit)
├── events.csv            cuff/oximeter readings, markers, device events (t_s = session time)
├── finger_FNG01.csv      t_s, seq, t_dev_us, ir, red   (one row per sample)
├── glasses_GLS01.csv
├── finger_FNG01.rawlog   every serial line exactly as received (for debugging)
└── glasses_GLS01.rawlog
```

`t_s` is seconds since the session started, from the PC clock, shared by every
stream and every event – that is how glasses, finger and cuff line up.
`t_dev_us` is the ESP32's own per-sample time (more precise for intervals).
The recorder fits `t_s ≈ a + b·t_dev` per device (`clock_fit` in session.json).

## Privacy & backup

- The repo only contains subject codes. Keep the code ↔ person key, consent forms
  and anything identifying in the lab's approved location, not here.
- `data/` is git-ignored. Back it up (lab server / drive) after every session day.
  `raw/` is the only irreplaceable part; `processed/` and `catalog.sqlite` can be rebuilt.
