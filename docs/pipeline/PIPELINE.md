# ppg-pipeline — PPG processing for the NBE smart-glasses project

| Layer | Job | Status |
|---|---|---|
| 1 | Signal conditioning & window quality | done |
| 2 | Beats, fiducial points, per-beat features, epoch feature table | done |
| 3 | Align with reference devices (cuff BP, pulse-ox), build datasets | next |
| 4 | Models (HR/SpO2 calibration, BP regression, deep models on beat templates) | later |

## Layer 1

```
raw CSV ──► load ──► fix timestamps ──► mask artifacts ──► filter ──► derivatives ──► window SQI
           (io)      (timing)           (pipeline)         (filters)                  (quality)
```

## Setup

```bash
cd smart-glasses
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## Use it

```bash
ppg-layer1 synth demo.csv                         # fake MAX30102 recording with known HR
ppg-layer1 run demo.csv -o out/                   # one file
ppg-layer1 run /mnt/recordings/ -o out/           # every *.csv in a folder (batch)
ppg-layer1 config > my_config.json                # dump defaults, edit, then:
ppg-layer1 run data/ -o out/ --config my_config.json
```

If your CSV columns aren't auto-detected:

```bash
ppg-layer1 run rec.csv -o out --time-col timestamp --time-unit ms --channel ir=IR_RAW --channel red=RED_RAW
```

From Python: see `examples/quickstart.py`.

### Outputs (per recording)

| File | Contents |
|---|---|
| `<name>_signals.csv` | Per-sample, per-channel signals (below) |
| `<name>_quality.csv` | Per-window SQI metrics, HR estimates, `good` flag and `reason` for rejection |
| `<name>_meta.json` | Timing report (fs, gaps, jitter), session summary, full config used |
| `<name>_qa.png` | QA figure — look at it for every new device / subject |

Signal columns (for each channel `ir`, `red`, …):

| Column | Meaning |
|---|---|
| `_raw` | Resampled raw counts |
| `_dc` | Baseline (< 0.4 Hz). Needed for perfusion index and SpO2 R-ratio |
| `_ac` | Morphology band 0.5–8 Hz, inverted so systole points up. Keeps dicrotic notch → BP features |
| `_ac_hr` | Narrow 0.5–4 Hz band for robust HR |
| `_ac_norm` | `ac / dc` — dimensionless, comparable across subjects and LED currents |
| `_vpg`, `_apg` | 1st / 2nd derivatives (Savitzky–Golay) of `ac_norm` |
| `_mask` | Bit flags: 1 gap, 2 saturated, 4 flatline, 8 no contact, 16 segment too short |

## Design decisions (and why)

- **Resample first.** Logged MAX3010x data has FIFO-batching jitter, duplicates and dropped packets; every filter assumes uniform sampling. Gaps longer than `max_gap_s` (0.25 s) become NaN instead of being interpolated over.
- **Zero-phase Butterworth (`sosfiltfilt`).** No phase delay, so peak/foot timing is preserved — critical later for pulse-arrival/transit-time BP features.
- **Two bands.** Narrow band for HR robustness; wide band so waveform shape (what BP models need) survives.
- **Segment-wise filtering.** NaN regions split the signal; each valid segment is filtered on its own, and segments shorter than 4 s are dropped.
- **Mark, don't delete, at window level.** Motion is flagged per 8 s window (2 s step) using several SQIs: autocorrelation periodicity, HR agreement between two independent estimators, skewness, spectral purity, amplitude vs. session median. Layer 2 decides what to do with bad windows.

## Things to set once you have real data

1. **CSV format** — confirm column names/time units from the MAX30102 recorder; set `io.channels` / `io.time_col` if auto-detect misses.
2. **`artifacts.min_dc`** — no-contact threshold. Record on/off-skin once per device (finger vs. glasses will differ a lot).
3. **SQI thresholds** — current values are literature-style starting points tuned on synthetic data only. Calibrate by comparing `good` windows against the commercial finger PPG / pulse-ox HR.
4. **Glasses PPG** — expect much lower perfusion index than finger; you may need `min_acf_peak` lower and a wider `max_gap_s`.

## Layout

```
src/ppg/
  config.py     all tunables (dataclasses ↔ JSON)
  io.py         CSV loading, column + time-unit auto-detection
  timing.py     timestamp cleaning, fs estimation, uniform resampling
  filters.py    NaN-aware zero-phase filters, derivatives, mask helpers
  quality.py    windowed SQI + HR estimates
  pipeline.py   run_layer1 / process_file — glues it together
  plotting.py   QA figure
  synthetic.py  realistic fake recordings with ground truth
  cli.py        `ppg-layer1` / `ppg-layer2` commands
  # Layer 2
  beats.py         systolic peaks + fiducial points
  features.py      per-beat features (documented table in the docstring)
  beat_quality.py  per-beat template-matching SQI
  epochs.py        epoch aggregation, HRV, ensemble templates
  layer2.py        run_layer2 / process_file_layer2
  plotting2.py     Layer 2 QA figures
tests/          pytest suite
examples/       quickstart.py
docs/           example QA figures from synthetic data
```

---

## Layer 2: beats → fiducials → features → epochs

```bash
ppg-layer1 synth demo.csv --duration 300 --dia-amp 0.6 0.25   # morphology slowly changes
ppg-layer2 run demo.csv -o out/          # runs Layer 1 + Layer 2
ppg-layer2 run /mnt/recordings/ -o out/  # batch
ppg-layer2 config > layer2_config.json
```

```
Layer-1 ac_norm ─► systolic peaks ─► fiducials ─► per-beat features ─► beat SQI ─► epochs (ML rows)
                   (Elgendi 2013)    foot, slope,   ~40 features        template     medians, IQRs,
                                     peak, notch,                       corr, IBI,   HR, HRV,
                                     dia, APG a–e                       amplitude    templates
```

### Why each step

1. **Systolic peaks** – Elgendi's two-moving-average detector (one of the best-scoring simple detectors in benchmark studies). Run per valid segment, with a 0.3 s refractory period.
2. **Fiducial points** – nearly every BP/vascular feature is a time, height, width or area between landmarks. The **foot** is anchored on the upstroke (tangent at max slope + short search window), because "lowest point before the peak" wanders around in the flat trough and wrecks IBI/HRV. Times are refined to sub-sample precision (parabolic fit), since at 100 Hz one sample is 10 ms.
3. **Per-beat features** – computed on AC/DC with the foot-to-foot baseline removed, mostly as ratios, so they transfer across subjects, LED currents and finger vs. head. Full list with physiological meaning: docstring of `src/ppg/features.py`.
4. **Beat SQI** – a "good" 8 s window can still contain a bad beat. Each beat is compared with the median of its neighbours (template correlation ≥ 0.86), plus IBI and amplitude consistency and the Layer-1 window verdict.
5. **Epochs** – reference labels (cuff BP every few minutes, pulse-ox each second) don't line up with beats, and single beats are noisy. So good beats are summarised in 30 s windows (10 s step): median + IQR of each feature, HR, SDNN/RMSSD/pNN50, and an averaged 100-point beat template. **One epoch row = one ML sample** once Layer 3 attaches labels.

### Gaussian pulse decomposition (added after Finnegan et al. 2023)

Each beat (foot → next foot, scaled 0–1 in time and height) is fitted as a sum of
three Gaussians: **g1** forward/systolic wave, **g2** early reflected
(late-systolic) wave, **g3** diastolic wave. Their relative heights, delays and
widths describe reflected-wave interference, which changes with arterial
stiffness. Finnegan et al. (Sci. Rep. 2023) found Gaussian components among
the top PPG features for estimating changes in systolic BP.

| Columns (per beat, then `_med`/`_iqr` per epoch) | Meaning |
|---|---|
| `g{1,2,3}_amp`, `_mu`, `_sigma` | height (beat peak = 1), centre, width (fraction of beat) |
| `g{1,2,3}_t_s`, `_area` | centre in seconds after the foot; area |
| `g2_amp_ratio`, `g3_amp_ratio` | height relative to g1 (strength of each reflection) |
| `g2_area_ratio`, `g3_area_ratio` | area relative to g1 |
| `g2_dt_s`, `g3_dt_s` | delay after g1 in seconds (reflection timing) |
| `gauss_r2`, `gauss_ok` | fit quality; features are NaN when R² < 0.98 |

Making the fit stable took three things:
- **g1 dominant and ordered.** Other heights are a fraction of g1, and centres are at least 5 % of the beat apart. Without this, one wave gets split in two and the labels swap from beat to beat.
- **A small penalty on g2.** This shrinks g2 toward 0 when the beat doesn't need it.
- **Three start points.** From this beat's fiducials, from the previous good beat, and a textbook layout. The best fit is kept.

Settings: `gaussian` in `config/pipeline_layer2.json` (`n_components`, `min_r2`, …;
`enabled: false` turns it off). Cost: about 2–3 s per 3-minute recording.

On synthetic data (`tests/test_gaussian.py`):
- **Diastolic wave:** with the diastolic wave changing, `g3_amp_ratio` follows it (epoch medians r > 0.95).
- **Late-systolic wave:** with a late-systolic wave growing, `g2_amp_ratio` follows it (r > 0.85).
- **When the notch fills in:** both keep tracking even when a late-systolic wave fills in the dicrotic notch. That case exposed the notch bug described below.

The QA figure shows g1–g3 over the median beat.

Fix made at the same time: the dicrotic-notch search is now limited to the first 65 % of the beat (`beats.notch_search_frac`). Before this, a filled-in notch made the detector pick a wiggle in the end-diastolic trough, and the reflection index collapsed to ~0.

### Outputs (per recording)

| File | Contents |
|---|---|
| `<name>_beats.csv` | One row per beat: fiducial sample indices (`*_idx`), features incl. Gaussian `g1..g3_*`, `template_corr`, `beat_good`, `beat_reason` |
| `<name>_epochs.csv` | One row per epoch: `<feature>_med`, `<feature>_iqr`, `hr_mean_bpm`, HRV, `n_good`, `valid` |
| `<name>_waves.npz` | `beat_waves` (n_beats×100), `epoch_templates` (n_epochs×100) — inputs for 1-D CNNs later |
| `<name>_beats.png` | Fiducials on a clean strip, APG a–e, all accepted vs rejected beats, tachogram |
| `<name>_features.png` | Key features over time (each beat + epoch median) |
| `<name>_feature_corr.png` | Spearman correlation matrix — shows redundant features before modelling |

### How to read the plots

- **Beat ensemble**: accepted beats should form a tight bundle. A fat bundle means the template threshold is too loose or the subject/sensor changed mid-recording.
- **Rejected beats**: should look like junk (motion, missed beats). If they look normal, thresholds are too strict.
- **Tachogram**: isolated drops to ~½ IBI = a false extra beat; jumps to ~2× IBI = a missed beat.
- **Features over time**: during a protocol (rest → exercise → recovery, cold pressor, etc.) you want features that move with the cuff BP and stay flat otherwise.
- **Correlation matrix**: blocks of near-±1 = redundant features (e.g. `notch_rel`, `dia_rel`, `ipa`, `area_dia`). Keep one per block or let regularisation handle it.

### Verified on synthetic data (see `tests/test_layer2.py`)

- Peak detection: >97 % sensitivity, >98 % precision outside artifacts; median timing error < 6 ms at 100 Hz.
- Beats in motion bursts are rejected; >95 % of clean beats accepted (template corr ≈ 0.99).
- `dia_rel` (reflection index) tracks a known simulated morphology change with r > 0.95; HR within 2 bpm, SDNN within 30 %, red/IR ratio within 0.03.
- Works at 50–165 bpm and on a weak, noisy, glasses-like signal.

### Limitations / to check on real data

- **Sample rate.** Timing features (rise time, ΔT, later pulse-transit time) need fine resolution; BP changes of ~10 mmHg may move PTT by only ~10 ms. If you can, run the MAX30102 at **≥ 200–400 Hz** for BP work.
- **APG d and e waves** are found in only a minority of synthetic beats (the synthetic pulse is simpler than a real one). Check detection rates on real finger data before trusting `d_a`, `e_a`, `agi`.
- **Dicrotic notch** often disappears at the head/glasses site and in older or vasoconstricted subjects; then `notch_method = 2` (inflection point) is used. `notch_method` is saved so you can filter on it.
- Synthetic data cannot validate that a feature *predicts BP* — that's what Layer 3 + your cuff data are for.
