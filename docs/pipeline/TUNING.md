# Tuning Layers 1–2 per sensor site (`sglab tune`)

The pipeline's thresholds and filter settings started as literature defaults, mostly from
fingertip PPG. The glasses and head sites look different: lower perfusion, broader beats and a
large diastolic wave. `sglab tune` searches the settings for one site at a time and scores them
**without blood pressure**.

## Why not tune against the cuff?

A session gives a few cuff readings and thousands of beats. With two subjects, a search over
thousands of settings scored on a few dozen cuff readings would find a setting that fits those
readings by chance (Cawley & Talbot 2010). So Layers 1–2 are judged on how cleanly they recover
**beats**, and the cuff readings stay unused until the BP model is tested.

## What a setting is scored on

| Score | Meaning | Source |
|---|---|---|
| **F1, sensitivity, PPV** | The finger stream recorded at the same time is the reference. Every good finger beat should appear at the glasses/head, shifted by a constant pulse-arrival difference that the tuner estimates. Sensitivity = share of real beats kept as *good*; PPV = share of the target's good beats that are real. | real sessions with a finger stream |
| **IBI err ms** | Beat-to-beat interval error against the finger (consecutive matched beats). | real |
| **foot jit ms** | Spread of the target-minus-finger foot time around its median offset: how precisely beats are timed. Matters later for pulse-arrival features. | real |
| **feat jitter** | Median relative beat-to-beat change of morphology features (rise time, widths, notch/dia ratios, IPA, b/a) over consecutive good beats. | real |
| **shape err** | Synthetic recordings with a known waveform, at low perfusion with real ADC quantisation: RMSE between the median good beat and the noise-free beat. Penalises noise *and* over-smoothing. | synthetic |

Smoothing lowers feature jitter but distorts the waveform, which raises shape error. The two
pull against each other, so neither is optimised alone.

## Stages

1. **`beats`**: Layer-1 window gates (`min_skew`, `min_acf_peak`, spectral purity, HR
   agreement, amplitude range, window length), Layer-2 beat detection (`w_peak_s`, `w_beat_s`,
   `beta`, foot search) and beat gates (template correlation, IBI/amplitude ranges, whether a
   beat must sit in a good Layer-1 window). Objectives: **max F1, min IBI error**.
2. **`morph`**, run with `--base <beats study>`: filter bands, filter order, DC cut-off,
   derivative smoothing, notch search. Objectives: **min shape error, min feature jitter**,
   with F1 allowed to drop by at most `--f1-slack` (0.02).

The search is multi-objective, so the result is a **Pareto front** (trade-offs between the
objectives), not a single best setting. Optuna's TPE sampler picks new settings near the good
ones found so far. The first trial is always the current settings, as a baseline.

**Held-out sessions.** Every 4th session, sorted by id, is never seen by the search. `report`
re-scores the baseline and the Pareto front on those sessions. A setting that is only better on
the sessions it was tuned on was a lucky fit. The recommended setting (★) is chosen on the
held-out scores:
- `beats`: the best F1 whose interval error is no worse than the baseline's.
- `morph`: the lowest shape error whose feature jitter is no worse than the baseline's.

## First result (2026-10-29, two S002 sessions)

The current `min_skew = 0` gate (Elgendi 2016, fingertip PPG) rejects almost every glasses/head
window. Head-site beats are broad with a large diastolic wave, so their amplitude distribution
is negatively skewed (median skew −0.46 glasses, −0.30 head, +0.10 finger). Periodicity was
excellent (spectral purity 0.90, ACF peak 0.80). In the rest session T10, only 7/728 glasses
and 0/726 head beats were kept. A 30-trial run raised held-out glasses F1 from 0.73 to 0.98.

## Running it

On the strong machine (as your user, in a clone of the repo):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[tune]"
.venv/bin/python firmware/tools/nbe_node.py login https://nbe.evm100.org    # admin account
.venv/bin/sglab tune fetch --out tune-data          # sessions with a finger stream (~30 MB each)

# overnight: stage 1 for both sites, then stage 2 on top
.venv/bin/sglab tune run --data tune-data --site glasses --stage beats --hours 4 --jobs 12
.venv/bin/sglab tune run --data tune-data --site head    --stage beats --hours 4 --jobs 12
.venv/bin/sglab tune report runs/tune/glasses-beats
.venv/bin/sglab tune report runs/tune/head-beats
.venv/bin/sglab tune run --data tune-data --site glasses --stage morph --base runs/tune/glasses-beats --hours 4
```

Use `tmux` or `nohup` so the run survives logging out. Ctrl-C keeps finished trials. Running
the same command again resumes the study and adds trials (`--trials` is the total budget).

**Cost.** A trial runs Layers 1–2 on every tuning session of the site, with Gaussian fitting
off. In the `beats` stage, signal conditioning and window metrics are cached, so a trial costs
~1.5 s per 10-minute stream. In the `morph` stage it's ~4 s, because filtering changes each
trial. With 20 sessions, that's roughly 30 s or 80 s per trial per process. Each process holds
the streams in memory (~1–2 GB for 20 ten-minute sessions at 400 Hz), so choose `--jobs` with RAM in mind. A few
hundred trials per stage is plenty: the convergence plot flattens well before that.

**Outputs** in `runs/tune/<site>-<stage>/`:
- `study.journal`: the Optuna study.
- `meta.json`: sessions, split, starting configs.
- `trials.csv`: every trial's settings and scores.
- `pareto.csv`: the front with tuning and held-out scores.
- `pareto.png`: trade-off and convergence plots.
- `recommended.json`: the ★ setting as a site override.

**Applying.** `sglab tune apply runs/tune/glasses-beats` merges the recommended setting (or
`--trial N`) into `config/sites/glasses.json`. Commit that file, and copy it to the server's
`config/sites/`. Processing applies `config/sites/<role>.json` on top of
`pipeline_layer1/2.json` for streams of that role, and the per-stream config hash changes, so
only that site's streams are processed again on the next Process.

## Limits

- The finger reference is processed with the current finger settings. If the finger is noisy
  in a session, that session's scores are noisy too: the tuner only counts beats where the
  finger is clean.
- PPV only checks that a good beat is a real beat, not that its *shape* is clean. Feature
  jitter and shape error cover shape, and synthetic shape is only as realistic as
  `ppg/synthetic.py`.
- Sessions without a finger stream are not used for tuning. They still get the tuned settings.
- All of this is within-subject for now (two subjects). Re-run it when new subjects come in. If
  their tuned settings differ a lot, settings may need to depend on the subject, not only the
  site.

## References

**PPG processing, filtering, beat detection**
- Charlton PH et al. *Wearable photoplethysmography for cardiovascular monitoring.* Proc IEEE 110(3), 2022.
- Liang Y, Elgendi M, Chen Z, Ward R. *An optimal filter for short photoplethysmogram signals.* Sci Data 5:180076, 2018.
- Charlton PH et al. *Detecting beats in the photoplethysmogram: benchmarking open-source algorithms.* Physiol Meas 43(8), 2022.
- Charlton PH et al. *The 2023 wearable photoplethysmography roadmap.* Physiol Meas 44(11), 2023.
- Elgendi M. *On the analysis of fingertip photoplethysmogram signals.* Curr Cardiol Rev 8(1), 2012.
- Elgendi M et al. *Systolic peak detection in acceleration photoplethysmograms measured from emergency responders in tropical conditions.* PLoS ONE 8(10), 2013 (our beat detector).

**Signal-quality indices**
- Elgendi M. *Optimal signal quality index for photoplethysmogram signals.* Bioengineering 3(4):21, 2016.
- Orphanidou C et al. *Signal-quality indices for the electrocardiogram and photoplethysmogram.* IEEE JBHI 19(3), 2015.
- Li Q, Clifford GD. *Dynamic time warping and machine learning for signal quality assessment of pulsatile signals.* Physiol Meas 33(9), 2012.

**Features related to BP**
- Takazawa K et al. *Assessment of vasoactive agents and vascular aging by the second derivative of photoplethysmogram waveform.* Hypertension 32(2), 1998.
- Teng XF, Zhang YT. *Continuous and noninvasive estimation of arterial blood pressure using a photoplethysmographic approach.* IEEE EMBC 2003.
- Mukkamala R et al. *Toward ubiquitous blood pressure monitoring via pulse transit time: theory and practice.* IEEE TBME 62(8), 2015.
- González S, Hsieh WT, Chen TPC. *A benchmark for machine-learning based non-invasive blood pressure estimation using photoplethysmogram.* Sci Data 10:149, 2023.

**Tuning methodology**
- Cawley GC, Talbot NLC. *On over-fitting in model selection and subsequent selection bias in performance evaluation.* JMLR 11, 2010.
- Akiba T et al. *Optuna: a next-generation hyperparameter optimization framework.* KDD 2019.
- Bergstra J, Bengio Y. *Random search for hyper-parameter optimization.* JMLR 13, 2012.
- Deb K et al. *A fast and elitist multiobjective genetic algorithm: NSGA-II.* IEEE TEVC 6(2), 2002.

**Validation (for the BP layer)**
- Bland JM, Altman DG. *Statistical methods for assessing agreement between two methods of clinical measurement.* Lancet 1986.
- Stergiou GS et al. *A universal standard for the validation of blood pressure measuring devices.* Hypertension 71(3), 2018.

**Toolboxes to compare against:** pyPPG (Goda, Charlton & Behar, Physiol Meas 2024), NeuroKit2, HeartPy.
