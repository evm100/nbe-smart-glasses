"""`sglab tune`: search Layer 1-2 settings for one sensor site, scored without BP labels.

    sglab tune fetch --out tune-data                       sessions with a finger stream, from the server
    sglab tune run --data tune-data --site glasses --stage beats --hours 8 --jobs 12
    sglab tune report runs/tune/glasses-beats              Pareto front, held-out check, plots
    sglab tune apply runs/tune/glasses-beats [--trial N]   -> config/sites/glasses.json
    sglab tune run ... --stage morph --base runs/tune/glasses-beats   (second stage on top of the first)

The finger stream is the reference (see ppg.tuning): every finger beat should appear at the
glasses/head too, so a setting is scored on how many of the target's *good* beats are real
beats (PPV), how many real beats it keeps (sensitivity) and how precisely it times them.
Sessions without a finger stream are skipped.

Stages (search spaces in SPACES):
  beats   Layer-1 window gates + Layer-2 beat detection and beat gates.
          Objectives: max F1 vs. finger, min beat-to-beat interval error.
  morph   Filter bands and derivative smoothing (waveform shape).
          Objectives: min synthetic shape error, min feature jitter; F1 must not drop
          more than --f1-slack below the starting point.

Every 4th session (sorted by id) is held out: the search never sees it, and `report`
re-scores the Pareto-optimal settings on it, so you can tell real gains from lucky fits.
Gaussian decomposition is switched off while tuning (it is ~90% of the run time and has
no settings in these stages).

Studies are Optuna journal files (resumable: run the same command again to add trials).
Needs the tune extra: pip install -e ".[tune]".
"""
from __future__ import annotations

import copy
import json
import multiprocessing as mp
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import ppg
from ppg.config import Layer1Config, Layer2Config, deep_merge, set_path, with_overrides
from ppg.io import load_csv
from ppg.layer2 import run_layer2
from ppg.pipeline import conditioned_signals, df_channels, layer1_result
from ppg.quality import label_quality, quality_metrics
from ppg.tuning import (feature_jitter, median_wave, pool_agreement, pool_jitter, reference_agreement,
                        shape_error)

from .processing import (_first_sample_dev_s, _first_sample_session_time, _prepared_csv, pipeline_configs, site_overrides,
                         stream_configs, stream_layer1)

from .timebase import run_clock, session_clocks

REF_ROLE = "finger"


# --------------------------------------------------------------------------
# search spaces: (dotted path into {"layer1": ..., "layer2": ...}, kind, args)
# a trailing .0/.1 sets one element of a 2-tuple setting
# --------------------------------------------------------------------------
SPACES = {
    "beats": [
        ("layer1.quality.window_s", "cat", [4.0, 6.0, 8.0, 10.0]),
        ("layer1.quality.min_skew", "float", (-1.0, 0.5)),
        ("layer1.quality.min_acf_peak", "float", (0.2, 0.8)),
        ("layer1.quality.min_spec_purity", "float", (0.1, 0.8)),
        ("layer1.quality.max_hr_disagreement_bpm", "float", (2.0, 15.0)),
        ("layer1.quality.amp_ratio_range.0", "float", (0.2, 0.6)),
        ("layer1.quality.amp_ratio_range.1", "float", (1.5, 4.0)),
        ("layer2.beats.w_peak_s", "float", (0.05, 0.2)),
        ("layer2.beats.w_beat_s", "float", (0.4, 1.0)),
        ("layer2.beats.beta", "float", (0.0, 0.1)),
        ("layer2.beats.foot_search_s", "float", (0.25, 0.6)),
        ("layer2.quality.min_template_corr", "float", (0.6, 0.98)),
        ("layer2.quality.ibi_ratio_range.0", "float", (0.6, 0.9)),
        ("layer2.quality.ibi_ratio_range.1", "float", (1.1, 1.5)),
        ("layer2.quality.amp_ratio_range.0", "float", (0.3, 0.8)),
        ("layer2.quality.amp_ratio_range.1", "float", (1.3, 3.0)),
        ("layer2.quality.require_layer1_good", "cat", [True, False]),
    ],
    "morph": [
        ("layer1.filters.morph_band_hz.0", "float", (0.3, 1.0)),
        ("layer1.filters.morph_band_hz.1", "float", (4.0, 20.0)),
        ("layer1.filters.dc_cutoff_hz", "float", (0.1, 0.8)),
        ("layer1.filters.order", "int", (2, 6)),
        ("layer1.filters.deriv_window_s", "float", (0.03, 0.2)),
        ("layer1.filters.deriv_polyorder", "cat", [2, 3, 4]),
        ("layer2.beats.notch_min_prominence", "float", (0.0, 0.05)),
        ("layer2.beats.notch_search_frac", "float", (0.5, 0.8)),
    ],
}
SPACES["all"] = SPACES["beats"] + SPACES["morph"]

OBJECTIVES = {  # (metric, direction)
    "beats": [("f1", "maximize"), ("ibi_mae_ms", "minimize")],
    "morph": [("shape_err", "minimize"), ("jitter", "minimize")],
    "all": [("f1", "maximize"), ("ibi_mae_ms", "minimize"), ("shape_err", "minimize"), ("jitter", "minimize")],
}


def params_to_overrides(params: dict, base: dict) -> dict:
    """Optuna params -> partial {"layer1": ..., "layer2": ...}; base = full to_dict()s (for tuple partners)."""
    out: dict = {}
    for path, v in sorted(params.items()):
        parts = path.split(".")
        if parts[-1].isdigit():
            parent = ".".join(parts[:-1])
            cur = _get(out, parent) or _get(base, parent)
            tup = list(cur)
            tup[int(parts[-1])] = v
            set_path(out, parent, tuple(tup))
        else:
            set_path(out, path, v)
    return out


def _get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def current_params(stage: str, base: dict) -> dict:
    """The starting point's values for the stage's parameters (always trial 0, the baseline)."""
    out = {}
    for path, kind, args in SPACES[stage]:
        parts = path.split(".")
        v = _get(base, ".".join(parts[:-1]))[int(parts[-1])] if parts[-1].isdigit() else _get(base, path)
        if kind == "cat" and v not in args:
            v = min(args, key=lambda a: abs(a - v)) if not isinstance(v, bool) else args[0]
        elif kind in ("float", "int"):
            v = min(max(v, args[0]), args[1])
        out[path] = v
    return out


# --------------------------------------------------------------------------
# data: session folders with a finger stream and a target-site stream
# --------------------------------------------------------------------------
@dataclass
class Pair:
    session_id: str
    folder: Path
    ref: dict
    tgt: dict
    t0_unix: float | None = None   # session start, anchors the SNTP clock (sglab.timebase)


def find_pairs(data: Path, site: str, include: list[str] | None = None, skip_bad: bool = True) -> list[Pair]:
    pairs = []
    for sj in sorted(Path(data).rglob("session.json")):
        if "_incoming" in sj.parts:
            continue
        doc, t0_unix = session_clocks(sj.parent)
        sid = doc.get("session_id", sj.parent.name)
        if include and not any(s in sid for s in include):
            continue
        if doc.get("status") == "discard":
            continue
        if skip_bad and (doc.get("metadata") or {}).get("quality") == "bad":
            continue
        by_role = {st.get("role"): st for st in doc.get("streams", [])
                   if st.get("file") and (sj.parent / st["file"]).exists()}
        if REF_ROLE in by_role and site in by_role:
            pairs.append(Pair(sid, sj.parent, by_role[REF_ROLE], by_role[site], t0_unix))
    return pairs


def split_pairs(pairs: list[Pair], holdout_every: int) -> tuple[list[Pair], list[Pair]]:
    if holdout_every <= 1 or len(pairs) < 2:
        return pairs, []
    tune = [p for i, p in enumerate(pairs) if i % holdout_every != holdout_every - 1]
    hold = [p for i, p in enumerate(pairs) if i % holdout_every == holdout_every - 1]
    return tune, hold


class Stream:
    """One stream's loaded CSV plus small caches of the expensive Layer-1 steps, keyed by the
    settings they depend on: conditioning (timing/artifacts/filters) and window metrics
    (window length, step, HR range). A trial that only moves thresholds reuses both."""

    def __init__(self, folder: Path, entry: dict, cfg1: Layer1Config, t0_unix: float | None = None):
        cfg1 = stream_layer1(cfg1, entry)
        with tempfile.TemporaryDirectory(prefix="sglab-tune-") as tmp:
            csv, _ = _prepared_csv(folder / entry["file"], Path(tmp))
            self.df = load_csv(csv, cfg1.io)
            summary = {"t0_session_s": _first_sample_session_time(csv, entry),
                       "t0_dev_s": _first_sample_dev_s(csv, entry)}
        # SNTP clock when the node has one: the PC-side clock_fit drifts on bursty Wi-Fi
        self.t0, self.b, self.clock = run_clock(entry, summary, t0_unix)
        self.entry = entry
        self._sig: OrderedDict = OrderedDict()
        self._q: OrderedDict = OrderedDict()

    @staticmethod
    def _put(cache: OrderedDict, key, val, size: int):
        cache[key] = val
        cache.move_to_end(key)
        while len(cache) > size:
            cache.popitem(last=False)

    def run(self, cfg1: Layer1Config, cfg2: Layer2Config):
        cfg1 = stream_layer1(cfg1, self.entry)
        d = cfg1.to_dict()
        k_sig = json.dumps([d["timing"], d["artifacts"], d["filters"]], sort_keys=True, default=str)
        if k_sig not in self._sig:
            self._put(self._sig, k_sig, conditioned_signals(self.df, cfg1), 2)
        sig, timing = self._sig[k_sig]
        qc = cfg1.quality
        k_q = (k_sig, qc.window_s, qc.step_s, qc.hr_min_bpm, qc.hr_max_bpm, qc.primary_channel)
        if k_q not in self._q:
            self._put(self._q, k_q, quality_metrics(sig, df_channels(self.df), timing["fs_used"], qc,
                                                    cfg1.filters.morph_band_hz), 6)
        quality = label_quality(self._q[k_q], df_channels(self.df), qc)
        l1 = layer1_result(self.df, sig, quality, timing, cfg1)
        return l1, run_layer2(l1, cfg2)

    def session_times(self, t: np.ndarray) -> np.ndarray:
        return self.t0 + self.b * np.asarray(t, float)


def _beat_times(stream: Stream, l2) -> tuple[np.ndarray, np.ndarray]:
    if l2.beats.empty:
        return np.array([]), np.array([], bool)
    return stream.session_times(l2.beats["t_foot"].to_numpy()), l2.beats["beat_good"].to_numpy(bool)


# --------------------------------------------------------------------------
# synthetic recordings with a known waveform (shape_error)
# --------------------------------------------------------------------------
SYNTH = [dict(seed=101, dia_amp=(0.55, 0.85), hr_bpm=68), dict(seed=202, dia_amp=(0.8, 0.5), hr_bpm=82)]
# low perfusion and real ADC quantisation, like the head sites (PI ~0.2-0.5 %)
SYNTH_CHANNELS = {"IR": (26_000, 0.004), "RED": (15_000, 0.002)}
TRUTH_L1 = {"filters": {"morph_band_hz": (0.3, 30.0), "dc_cutoff_hz": 0.2}}


@dataclass
class SynthCase:
    stream: Stream
    truth_wave: np.ndarray


def synth_cases(tmp: Path) -> list[SynthCase]:
    from ppg.synthetic import make_recording

    out = []
    entry = {"io": {}, "fs": 400.0}
    for i, kw in enumerate(SYNTH):
        common = dict(duration_s=150, fs=400.0, channels=SYNTH_CHANNELS, motion=((70.0, 76.0),),
                      dropouts=(), **kw)
        clean, _ = make_recording(noise_frac=0.0, jitter_ms=0.0, **{**common, "motion": ()})
        noisy, _ = make_recording(noise_frac=0.08, **common)
        paths = []
        for name, df in (("clean", clean), ("noisy", noisy)):
            folder = tmp / f"synth{i}"
            folder.mkdir(parents=True, exist_ok=True)
            df.to_csv(folder / f"{name}.csv", index=False)
            paths.append(folder)
        cs = Stream(paths[0], {**entry, "file": "clean.csv"}, Layer1Config())
        cfg2 = Layer2Config()
        cfg2.gaussian.enabled = False
        _, l2 = cs.run(with_overrides(Layer1Config(), TRUTH_L1), cfg2)
        truth = median_wave(l2.waves, np.ones(len(l2.waves), bool))
        out.append(SynthCase(Stream(paths[1], {**entry, "file": "noisy.csv"}, Layer1Config()), truth))
    return out


# --------------------------------------------------------------------------
# evaluation of one setting
# --------------------------------------------------------------------------
@dataclass
class Evaluator:
    pairs: list[Pair]
    site: str
    base1: Layer1Config            # starting point for the target site (base + site overrides + --base)
    base2: Layer2Config
    ref1: Layer1Config             # the finger reference: the current finger settings, fixed
    ref2: Layer2Config
    synth: bool = False
    _tgt: dict = field(default_factory=dict)
    _ref: dict = field(default_factory=dict)
    _synth: list = field(default_factory=list)
    _tmp: tempfile.TemporaryDirectory | None = None

    def load(self, log=print):
        t = time.monotonic()
        ref2 = copy.deepcopy(self.ref2)
        ref2.gaussian.enabled = False  # only beat times and flags are used
        for p in self.pairs:
            rs = Stream(p.folder, p.ref, self.ref1, p.t0_unix)
            _, l2 = rs.run(self.ref1, ref2)
            self._ref[p.session_id] = _beat_times(rs, l2)
            self._tgt[p.session_id] = Stream(p.folder, p.tgt, self.base1, p.t0_unix)
        if self.synth:
            self._tmp = tempfile.TemporaryDirectory(prefix="sglab-tune-synth-")
            self._synth = synth_cases(Path(self._tmp.name))
        log(f"  loaded {len(self.pairs)} sessions ({time.monotonic() - t:.0f} s)")
        return self

    def configs(self, overrides: dict) -> tuple[Layer1Config, Layer2Config]:
        cfg1 = with_overrides(self.base1, overrides.get("layer1"))
        cfg2 = with_overrides(self.base2, overrides.get("layer2"))
        cfg2.gaussian.enabled = False
        return cfg1, cfg2

    def __call__(self, overrides: dict) -> dict:
        cfg1, cfg2 = self.configs(overrides)
        agree, jit, per_session, n_good, n_beats = [], [], {}, 0, 0
        for p in self.pairs:
            s = self._tgt[p.session_id]
            _, l2 = s.run(cfg1, cfg2)
            t, g = _beat_times(s, l2)
            ref_t, ref_good = self._ref[p.session_id]
            span = (s.t0, s.session_times(s.df["t"].iloc[-1] - s.df["t"].iloc[0]))
            a = reference_agreement(ref_t, ref_good, t, g, span=span)
            agree.append(a)
            jit.append(feature_jitter(l2.beats))
            per_session[p.session_id] = round(pool_agreement([a])["f1"], 4)
            n_good += int(g.sum())
            n_beats += int(g.size)
        m = pool_agreement(agree)
        m.update(jitter=pool_jitter(jit), n_good_beats=n_good, n_beats=n_beats, per_session_f1=per_session)
        if self._synth:
            errs = []
            for c in self._synth:
                _, l2 = c.stream.run(cfg1, cfg2)
                errs.append(shape_error(l2.waves, l2.beats["beat_good"].to_numpy(bool) if len(l2.beats) else
                                        np.array([], bool), c.truth_wave))
            m["shape_err"] = float(np.mean(errs))
        return m


def starting_configs(root: Path, site: str, base_overlay: dict | None) -> tuple:
    """(base1, base2, ref1, ref2): the server-side configs as processing uses them."""
    from .settings import load_settings

    settings = load_settings(root)
    b1, b2 = pipeline_configs(settings)
    sites = site_overrides(settings)
    t1, t2 = stream_configs(b1, b2, sites, {"role": site})
    r1, r2 = stream_configs(b1, b2, sites, {"role": REF_ROLE})
    if base_overlay:
        t1, t2 = with_overrides(t1, base_overlay.get("layer1")), with_overrides(t2, base_overlay.get("layer2"))
    return t1, t2, r1, r2


# --------------------------------------------------------------------------
# the study
# --------------------------------------------------------------------------
def _optuna():
    try:
        import optuna
    except ImportError:
        raise ValueError('sglab tune needs Optuna: pip install -e ".[tune]"') from None
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    return optuna


def _storage(study_dir: Path):
    optuna = _optuna()
    from optuna.storages.journal import JournalFileBackend

    return optuna.storages.JournalStorage(JournalFileBackend(str(study_dir / "study.journal")))


def _constraints(trial):
    return trial.user_attrs.get("constraint", [0.0])


def _evaluator_for(meta: dict, split: str, log=print) -> Evaluator:
    pairs = [p for p in find_pairs(Path(meta["data"]), meta["site"], meta.get("sessions"), meta["skip_bad"])
             if p.session_id in set(meta[split])]
    b1 = Layer1Config.from_dict(meta["start"]["layer1"])
    b2 = Layer2Config.from_dict(meta["start"]["layer2"])
    r1 = Layer1Config.from_dict(meta["ref"]["layer1"])
    r2 = Layer2Config.from_dict(meta["ref"]["layer2"])
    return Evaluator(pairs, meta["site"], b1, b2, r1, r2, synth=meta["stage"] in ("morph", "all")).load(log)


def _worker(study_dir: str, n_trials: int, deadline: float, wid: int) -> None:
    optuna = _optuna()
    study_dir = Path(study_dir)
    meta = json.loads((study_dir / "meta.json").read_text())
    log = lambda m: print(f"[w{wid}] {m}", flush=True)  # noqa: E731
    ev = _evaluator_for(meta, "tune", log)
    space, objs = SPACES[meta["stage"]], OBJECTIVES[meta["stage"]]
    base = {"layer1": ev.base1.to_dict(), "layer2": ev.base2.to_dict()}
    f1_floor = meta.get("f1_floor")

    def objective(trial):
        params = {}
        for path, kind, args in space:
            if kind == "float":
                params[path] = trial.suggest_float(path, *args)
            elif kind == "int":
                params[path] = trial.suggest_int(path, *args)
            else:
                params[path] = trial.suggest_categorical(path, args)
        t = time.monotonic()
        m = ev(params_to_overrides(params, base))
        for k, v in m.items():
            trial.set_user_attr(k, v if not isinstance(v, float) or np.isfinite(v) else None)
        trial.set_user_attr("seconds", round(time.monotonic() - t, 1))
        if f1_floor is not None:
            trial.set_user_attr("constraint", [f1_floor - m["f1"]])  # <= 0 is feasible
        vals = [m[k] for k, _ in objs]
        if not all(np.isfinite(v) for v in vals):
            raise ValueError(f"non-finite objective {vals}")
        log(f"trial {trial.number}: " + "  ".join(f"{k} {m[k]:.4g}" for k, _ in objs)
            + f"  good beats {m['n_good_beats']}  ({time.monotonic() - t:.0f} s)")
        return vals

    sampler = optuna.samplers.TPESampler(multivariate=True, constant_liar=True, n_startup_trials=20,
                                         constraints_func=_constraints if f1_floor is not None else None,
                                         seed=None)
    study = optuna.load_study(study_name="tune", storage=_storage(study_dir), sampler=sampler)
    remaining = max(1.0, deadline - time.time())
    study.optimize(objective, n_trials=n_trials, timeout=remaining, catch=(Exception,), gc_after_trial=True,
                   callbacks=[optuna.study.MaxTrialsCallback(
                       n_trials, states=(optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.FAIL))])


def run(data: Path, site: str, stage: str, out: Path, trials: int, hours: float, jobs: int,
        holdout_every: int = 4, sessions: list[str] | None = None, base: Path | None = None,
        f1_slack: float = 0.02, skip_bad: bool = True, root: Path | None = None, log=print) -> Path:
    optuna = _optuna()
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if (meta["site"], meta["stage"]) != (site, stage):
            raise ValueError(f"{out} is a {meta['site']}/{meta['stage']} study; use another --out")
        log(f"resuming {out} ({meta['site']}/{meta['stage']})")
    else:
        pairs = find_pairs(data, site, sessions, skip_bad)
        if not pairs:
            raise ValueError(f"no session in {data} has both a '{REF_ROLE}' and a '{site}' stream")
        tune_p, hold_p = split_pairs(pairs, holdout_every)
        base_overlay = recommended_overlay(base) if base else None
        t1, t2, r1, r2 = starting_configs(root or Path.cwd(), site, base_overlay)
        meta = {"site": site, "stage": stage, "data": str(Path(data).resolve()), "sessions": sessions,
                "skip_bad": skip_bad, "ppg_version": ppg.__version__,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "base_study": str(base) if base else None,
                "base_overlay": base_overlay,
                "tune": [p.session_id for p in tune_p], "holdout": [p.session_id for p in hold_p],
                "start": {"layer1": t1.to_dict(), "layer2": t2.to_dict()},
                "ref": {"layer1": r1.to_dict(), "layer2": r2.to_dict()},
                "objectives": OBJECTIVES[stage], "f1_floor": None}
        log(f"{len(pairs)} sessions with {REF_ROLE} + {site}: {len(tune_p)} to tune on, {len(hold_p)} held out")
        meta_path.write_text(json.dumps(meta, indent=2, default=list))
        ev = _evaluator_for(meta, "tune", log)
        start = {"layer1": ev.base1.to_dict(), "layer2": ev.base2.to_dict()}
        m0 = ev(params_to_overrides(current_params(stage, start), start))
        log("starting point: " + ", ".join(f"{k} {m0[k]:.4g}" for k in ("f1", "sensitivity", "ppv", "ibi_mae_ms",
                                                                         "foot_jitter_ms", "jitter")
                                           if m0.get(k) is not None))
        if stage == "morph":
            meta["f1_floor"] = m0["f1"] - f1_slack
        meta["start_metrics"] = {k: v for k, v in m0.items() if k != "per_session_f1"}
        meta_path.write_text(json.dumps(meta, indent=2, default=list))
        study = optuna.create_study(study_name="tune", storage=_storage(out),
                                    directions=[d for _, d in OBJECTIVES[stage]])
        study.enqueue_trial(current_params(stage, start), user_attrs={"baseline": True})
    deadline = time.time() + hours * 3600 if hours else time.time() + 10 * 365 * 86400
    jobs = max(1, jobs)
    log(f"{jobs} worker process(es), up to {trials} trials" + (f" or {hours:g} h" if hours else ""))
    ctx = mp.get_context("spawn")  # fresh interpreters: no shared numpy/BLAS state
    procs = [ctx.Process(target=_worker, args=(str(out), trials, deadline, i)) for i in range(jobs)]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        log("stopping (finished trials are kept; run again to continue)")
        for p in procs:
            p.terminate()
    return out


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------
def load_study(study_dir: Path):
    optuna = _optuna()
    meta = json.loads((study_dir / "meta.json").read_text())
    return meta, optuna.load_study(study_name="tune", storage=_storage(study_dir))


def trials_frame(meta: dict, study) -> pd.DataFrame:
    rows = []
    for t in study.trials:
        if t.state.name != "COMPLETE":
            continue
        r = {"trial": t.number, "baseline": bool(t.user_attrs.get("baseline"))}
        r.update({k: v for k, v in t.user_attrs.items() if k not in ("per_session_f1", "constraint", "baseline")})
        r["feasible"] = all(c <= 0 for c in t.user_attrs.get("constraint", [0.0]))
        r.update({f"p:{k}": v for k, v in t.params.items()})
        rows.append(r)
    return pd.DataFrame(rows)


def pareto(df: pd.DataFrame, objectives: list) -> pd.DataFrame:
    """Non-dominated feasible trials."""
    d = df[df["feasible"]].dropna(subset=[k for k, _ in objectives])
    v = np.column_stack([d[k].to_numpy(float) * (-1 if s == "maximize" else 1) for k, s in objectives])
    keep = [not np.any(np.all(v <= v[i], axis=1) & np.any(v < v[i], axis=1)) for i in range(len(v))]
    return d[np.array(keep, bool)] if len(d) else d


def trial_overlay(meta: dict, params: dict) -> dict:
    """Site overlay for a trial: the --base study's overlay + this trial's params."""
    start = meta["start"]
    return deep_merge(meta.get("base_overlay") or {}, params_to_overrides(params, start))


def recommended_overlay(study_dir: Path) -> dict:
    rec = json.loads((Path(study_dir) / "recommended.json").read_text()) if (Path(study_dir) / "recommended.json").exists() \
        else None
    if rec is None:
        raise ValueError(f"{study_dir}: run `sglab tune report {study_dir}` first (it picks the recommended trial)")
    return rec["overlay"]


def _eval_holdout(args):
    meta, overlays = args
    ev = _evaluator_for(meta, "holdout", log=lambda m: None)
    start = {"layer1": ev.base1.to_dict(), "layer2": ev.base2.to_dict()}
    out = []
    for params in overlays:
        m = ev(params_to_overrides(params, start))
        out.append({k: v for k, v in m.items() if k != "per_session_f1"})
    return out


def report(study_dir: Path, jobs: int = 4, top: int = 12, log=print) -> dict:
    meta, study = load_study(study_dir)
    objs = meta["objectives"]
    df = trials_frame(meta, study)
    if df.empty:
        raise ValueError("no finished trials yet")
    df.to_csv(study_dir / "trials.csv", index=False)
    front = pareto(df, objs)
    base_row = df[df["baseline"]].iloc[0] if df["baseline"].any() else None
    # held-out check: baseline + the Pareto front (best first by the first objective)
    k0, s0 = objs[0]
    front = front.sort_values(k0, ascending=(s0 == "minimize")).head(top)
    check = ([base_row] if base_row is not None else []) + [r for _, r in front.iterrows()
                                                            if base_row is None or r["trial"] != base_row["trial"]]
    # rounded to 4 significant digits: what gets applied, so the held-out scores are for exactly that
    params = [{c[2:]: _round(r[c]) for c in df.columns if c.startswith("p:") and pd.notna(r[c])} for r in check]
    hold = [None] * len(check)
    if meta.get("holdout"):
        log(f"re-scoring {len(check)} settings on {len(meta['holdout'])} held-out sessions...")
        n = max(1, min(jobs, len(check)))
        chunks = [list(range(i, len(check), n)) for i in range(n)]
        with mp.get_context("spawn").Pool(n) as pool:
            res = pool.map(_eval_holdout, [(meta, [params[i] for i in c]) for c in chunks])
        for c, r in zip(chunks, res):
            for i, m in zip(c, r):
                hold[i] = m
    rows = []
    for r, h, p in zip(check, hold, params):
        row = {"trial": int(r["trial"]), "baseline": bool(r["baseline"])}
        for k in ("f1", "sensitivity", "ppv", "ibi_mae_ms", "foot_jitter_ms", "jitter", "shape_err", "n_good_beats"):
            if k in r and pd.notna(r[k]):
                row[f"tune_{k}"] = r[k]
            if h and h.get(k) is not None:
                row[f"hold_{k}"] = h[k]
        row["params"] = p
        rows.append(row)
    res = pd.DataFrame(rows)
    pick = _recommend(res, objs, meta)
    res.drop(columns=["params"]).to_csv(study_dir / "pareto.csv", index=False)
    rec = {"trial": int(pick["trial"]), "site": meta["site"], "stage": meta["stage"],
           "overlay": trial_overlay(meta, pick["params"]),
           "tune": {k[5:]: pick[k] for k in pick.index if k.startswith("tune_") and pd.notna(pick[k])},
           "holdout": {k[5:]: pick[k] for k in pick.index if k.startswith("hold_") and pd.notna(pick[k])}}
    (study_dir / "recommended.json").write_text(json.dumps(rec, indent=2, default=_jsonable))
    _plots(study_dir, df, front, objs, meta)
    return {"meta": meta, "n_trials": len(df), "table": res, "recommended": rec}


def _round(v):
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    return float(f"{float(v):.4g}")


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(type(o))


def _recommend(res: pd.DataFrame, objs: list, meta: dict) -> pd.Series:
    """Judged on the held-out sessions when there are any (else on the tuning ones):
    beats: best F1 whose interval error is no worse than the baseline's (+0.5 ms);
    morph: lowest shape error whose jitter is no worse than the baseline's."""
    pre = "hold_" if any(c.startswith("hold_") for c in res.columns) else "tune_"
    base = res[res["baseline"]]
    cand = res
    if meta["stage"] in ("beats", "all") and f"{pre}f1" in res:
        if len(base) and f"{pre}ibi_mae_ms" in res and pd.notna(base[f"{pre}ibi_mae_ms"].iloc[0]):
            ok = cand[cand[f"{pre}ibi_mae_ms"] <= base[f"{pre}ibi_mae_ms"].iloc[0] + 0.5]
            cand = ok if len(ok) else cand
        return cand.sort_values(f"{pre}f1", ascending=False).iloc[0]
    if len(base) and f"{pre}jitter" in res:
        ok = cand[cand[f"{pre}jitter"] <= base[f"{pre}jitter"].iloc[0]]
        cand = ok if len(ok) else cand
    if f"{pre}f1" in res and meta.get("f1_floor") is not None:
        ok = cand[cand[f"{pre}f1"] >= meta["f1_floor"] - 0.02]
        cand = ok if len(ok) else cand
    return cand.sort_values(f"{pre}shape_err").iloc[0]


def _plots(study_dir: Path, df: pd.DataFrame, front: pd.DataFrame, objs: list, meta: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (a, sa), (b, sb) = objs[0], objs[1]
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.6))
    ok = df[df["feasible"]]
    ax[0].scatter(ok[a], ok[b], s=10, c=ok["trial"], cmap="viridis", alpha=0.6, label="trials")
    if (~df["feasible"]).any():
        bad = df[~df["feasible"]]
        ax[0].scatter(bad[a], bad[b], s=8, c="0.75", label="infeasible (F1 dropped)")
    f = front.sort_values(a)
    ax[0].plot(f[a], f[b], "o-", color="C3", ms=5, label="Pareto front")
    if df["baseline"].any():
        r = df[df["baseline"]].iloc[0]
        ax[0].plot(r[a], r[b], "*", color="k", ms=14, label="starting point")
    ax[0].set_xlabel(f"{a} ({'higher' if sa == 'maximize' else 'lower'} is better)")
    ax[0].set_ylabel(f"{b} ({'higher' if sb == 'maximize' else 'lower'} is better)")
    ax[0].set_title(f"{meta['site']} / {meta['stage']}: {len(df)} trials")
    ax[0].legend(fontsize=8)
    # best-so-far of the first objective vs trial number
    s = df.sort_values("trial")
    best = s[a].cummax() if sa == "maximize" else s[a].cummin()
    ax[1].plot(s["trial"], s[a], ".", color="0.6", ms=4)
    ax[1].plot(s["trial"], best, color="C0")
    ax[1].set_xlabel("trial")
    ax[1].set_ylabel(a)
    ax[1].set_title("convergence")
    fig.tight_layout()
    fig.savefig(study_dir / "pareto.png", dpi=120)
    plt.close(fig)


def apply(study_dir: Path, root: Path, trial: int | None = None) -> tuple[Path, dict]:
    """Merge a trial's overlay (default: the recommended one) into config/sites/<site>.json."""
    meta, study = load_study(study_dir)
    if trial is None:
        overlay = recommended_overlay(study_dir)
    else:
        t = study.trials[trial]
        if t.state.name != "COMPLETE":
            raise ValueError(f"trial {trial} did not finish")
        overlay = trial_overlay(meta, {k: _round(v) for k, v in t.params.items()})
    path = root / "config" / "sites" / f"{meta['site']}.json"
    cur = json.loads(path.read_text()) if path.exists() else {}
    new = deep_merge(cur, overlay)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new, indent=2, default=_jsonable) + "\n")
    return path, new


# --------------------------------------------------------------------------
# fetch: copy sessions with a finger stream from the lab server (nbe_node.py admin login)
# --------------------------------------------------------------------------
def fetch(out: Path, server: str | None = None, token: str | None = None, include: list[str] | None = None,
          log=print) -> int:
    if not (server and token):
        conf_dir = Path(os.environ.get("NBE_NODE_CONFIG") or Path.home() / ".config" / "nbe-node")
        conf = json.loads((conf_dir / "config.json").read_text()) if (conf_dir / "config.json").exists() else {}
        server, token = server or conf.get("server"), token or conf.get("token")
    if not (server and token):
        raise ValueError("log in first: python firmware/tools/nbe_node.py login https://<server> (admin)")
    q = urllib.parse.quote

    def get(path: str):
        req = urllib.request.Request(server.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"})
        return urllib.request.urlopen(req, timeout=300)

    with get("/api/sessions") as r:
        sessions = json.loads(r.read())
    n = 0
    for s in sessions:
        sid = s["session_id"]
        if include and not any(i in sid for i in include):
            continue
        d = out / sid
        with get(f"/files/{q(s['path'])}/session.json") as r:
            doc_bytes = r.read()
        doc = json.loads(doc_bytes)
        roles = {st.get("role") for st in doc.get("streams", []) if st.get("file")}
        if REF_ROLE not in roles:
            continue
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_bytes(doc_bytes)
        try:  # its wall times anchor the SNTP clock to the session (sglab.timebase)
            with get(f"/files/{q(s['path'])}/events.csv") as r:
                (d / "events.csv").write_bytes(r.read())
        except urllib.error.HTTPError:
            pass
        for st in doc["streams"]:
            if not st.get("file"):
                continue
            f = d / st["file"]
            if f.exists() and f.stat().st_size > 0:
                continue
            tmp = f.with_suffix(".part")
            with get(f"/files/{q(s['path'])}/{q(st['file'])}") as r, tmp.open("wb") as w:
                while chunk := r.read(1 << 20):
                    w.write(chunk)
            tmp.rename(f)
        n += 1
        log(f"  {sid}: {', '.join(sorted(r for r in roles if r))}")
    return n
