"""Layer 4 sweeps: search model settings and feature subsets, keep the best as a model run.

    sglab model sweep [--trials 60] [--name ...]      on this computer
    Models page -> "Sweep"                            queued for `sglab worker` in remote mode

Each trial is one Layer-4 config (model kind + its settings + which features), scored by the
cross-validated MAE on the non-holdout subjects. The best trial's config is then trained like
`sglab model train` and saved as a normal run, with the sweep in metrics.json and trials.csv.

The best trial's score is optimistic: with few people, some of 60 settings look good by luck.
So the sweep also scores the *search itself* (nested CV): for each subject, the whole search
runs without them, and its winner predicts them. That nested score is the honest estimate of
"tune, then use on a new person". If it is much worse than the best trial's score, the search
is fitting noise: use fewer features / fewer trials, or collect more data.

Spec: config/sweep_layer4.json (keys as DEFAULT_SPEC; missing keys use the defaults).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ppg.config import Layer4Config, deep_merge, set_path

DEFAULT_SPEC = {
    "trials": 60,            # per search; nested CV runs one search per outer fold on top
    "nested": True,
    "seed": 0,
    # each trial uses a subset of these (those with values for the site)
    "features": ["hr_mean_bpm", "pad_ms_rel", "t_rise_s_med", "t_max_slope_s_med", "pw25_s_med", "pw50_s_med",
                 "pw75_s_med", "max_slope_norm_med", "shape_factor_med", "sys_frac_med", "notch_rel_med",
                 "ipa_med", "agi_med", "sdnn_ms", "rmssd_ms"],
    # [dotted path into Layer4Config, "categorical" | "float" | "int", args]; float args may end in "log"
    "space": [
        ["model.kind", "categorical", ["ridge", "hgb"]],
        ["model.alpha", "float", [0.01, 100, "log"]],
        ["model.learning_rate", "float", [0.01, 0.3, "log"]],
        ["model.max_iter", "int", [50, 400]],
        ["model.max_depth", "int", [2, 5]],
        ["model.min_samples_leaf", "int", [3, 20]],
    ],
}
# settings that only mean something for one model kind
ONLY_FOR = {"model.alpha": "ridge", "model.learning_rate": "hgb", "model.max_iter": "hgb",
            "model.max_depth": "hgb", "model.min_samples_leaf": "hgb"}


def sweep_spec(settings) -> dict:
    p = settings.root / "config" / "sweep_layer4.json"
    return deep_merge(DEFAULT_SPEC, json.loads(p.read_text())) if p.exists() else dict(DEFAULT_SPEC)


def _optuna():
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    return optuna


def _suggest(trial, space: list, features: list[str], base: dict) -> tuple[dict, dict]:
    """(Layer4Config dict for this trial, the trial's parameters)."""
    params, cfg = {}, json.loads(json.dumps(base))
    kind = base["model"]["kind"]
    for path, typ, args in space:
        if path in ONLY_FOR and ONLY_FOR[path] != kind:
            continue
        if typ == "categorical":
            v = trial.suggest_categorical(path, list(args))
        elif typ == "float":
            v = trial.suggest_float(path, float(args[0]), float(args[1]), log="log" in args[2:])
        elif typ == "int":
            v = trial.suggest_int(path, int(args[0]), int(args[1]))
        else:
            raise ValueError(f"space {path}: type {typ!r} (use categorical, float, int)")
        set_path(cfg, path, v)
        params[path] = v
        if path == "model.kind":
            kind = v
    use = [f for f in features if trial.suggest_categorical(f"use.{f}", [True, False])]
    cfg["features"] = use
    params["features"] = use
    return cfg, params


def search(dev: pd.DataFrame, groups: np.ndarray, base: Layer4Config, features: list[str], spec: dict,
           n_trials: int, seed: int, tick=None) -> tuple[Layer4Config, list[dict]]:
    """Best config by out-of-fold MAE over `groups`, and every trial (number, mae, params)."""
    from ppg.layer4 import cv_mae

    optuna = _optuna()
    base_d = base.to_dict()
    trials = []

    def objective(trial):
        cfg_d, params = _suggest(trial, spec["space"], features, base_d)
        if not params["features"]:
            raise optuna.TrialPruned("no features")
        mae = cv_mae(dev, Layer4Config.from_dict(cfg_d), params["features"], groups)
        trials.append({"trial": trial.number, "mae": mae, **params})
        return mae

    sampler = optuna.samplers.TPESampler(seed=seed, n_startup_trials=min(15, max(5, n_trials // 4)))
    study = optuna.create_study(direction="minimize", sampler=sampler)
    study.optimize(objective, n_trials=n_trials, callbacks=[lambda *_: tick and tick()])
    if not trials:
        raise ValueError("every trial failed")
    best = min(trials, key=lambda t: t["mae"])
    cfg_d = base.to_dict()
    for k, v in best.items():
        if k.startswith("model."):
            set_path(cfg_d, k, v)
    cfg_d["features"] = best["features"]
    return Layer4Config.from_dict(cfg_d), trials


def run(df: pd.DataFrame, base: Layer4Config, spec: dict, log=print, progress=None) -> dict:
    """The sweep: {"best": Layer4Config, "trials": [...], "nested": {...} | None, "best_cv_mae": float}.

    progress(done, total) is called after every trial (workers use it to keep their lease)."""
    from ppg.layer4 import cv_groups, error_metrics, fit_predict, prepare

    cand = Layer4Config.from_dict({**base.to_dict(), "features": list(spec["features"])})
    rows, features, _, _ = prepare(df, cand, log)
    dev = rows[~rows["subject"].isin(list(base.holdout_subjects))].reset_index(drop=True)
    unit, groups = cv_groups(dev, base)
    outer = list(dict.fromkeys(groups))
    if len(outer) < 2:
        raise ValueError(f"a sweep needs 2+ {unit}s, have {len(outer)}")
    n = int(spec["trials"])
    nested_ok = bool(spec.get("nested", True)) and len(outer) >= 3
    total = n * (1 + (len(outer) if nested_ok else 0))
    done = [0]

    def tick():
        done[0] += 1
        if progress:
            progress(done[0], total)

    log(f"  sweep: {n} trials over {len(features)} candidate features, {len(outer)} {unit} folds"
        + (f", nested ({total} trials in all)" if nested_ok else ""))
    t0 = time.monotonic()
    best, trials = search(dev, groups, base, features, spec, n, int(spec.get("seed", 0)), tick)
    log(f"  best: MAE {min(t['mae'] for t in trials):.2f} mmHg with {best.model.kind}, "
        f"{len(best.features)} feature(s) ({time.monotonic() - t0:.0f} s)")

    nested = None
    if nested_ok:
        preds = {t: np.full(len(dev), np.nan) for t in base.targets}
        chosen = []
        for g in outer:
            te = groups == g
            inner, inner_groups = dev[~te].reset_index(drop=True), groups[~te]
            cfg_g, _ = search(inner, inner_groups, base, features, spec, n, int(spec.get("seed", 0)), tick)
            for t, p in fit_predict(inner, dev[te], cfg_g, list(cfg_g.features)).items():
                preds[t][te] = p
            chosen.append({"left_out": str(g), "model": cfg_g.model.kind, "features": list(cfg_g.features)})
        metrics = {}
        for t in base.targets:
            y = dev[t].to_numpy(float)
            cal = dev[f"cal_{t}"].to_numpy(float) if base.calibrated else None
            if base.calibrated:
                b = cal
            else:  # each outer fold's baseline: the mean of the other folds
                b = np.array([np.mean(y[groups != g]) for g in groups])
            metrics[t] = {"model": error_metrics(y, preds[t], cal), "baseline": error_metrics(y, b, cal)}
        nested = {"unit": unit, "n_outer": len(outer), "metrics": metrics, "chosen": chosen}
        log("  nested: " + ", ".join(f"{t} MAE {m['model']['mae']:.2f} (baseline {m['baseline']['mae']:.2f})"
                                     for t, m in metrics.items()))
    elif spec.get("nested", True):
        log(f"  nested score skipped: needs 3+ {unit}s")
    return {"best": best, "trials": trials, "nested": nested, "best_cv_mae": min(t["mae"] for t in trials),
            "n_candidates": len(features), "seconds": round(time.monotonic() - t0, 1)}


def sweep_to(parent: Path, name: str, df: pd.DataFrame, raw: bytes, manifest: dict, dataset: str,
             base: Layer4Config, spec: dict, log=print, progress=None, run_id: str | None = None) -> dict:
    """Sweep, train the winner, write parent/<run_id>/ (a model run + trials.csv). Returns metrics.json."""
    from ppg.layer4 import train

    from .model import new_run_id, write_run

    sw = run(df, base, spec, log, progress)
    res = train(df, sw["best"], log=log)
    run_id = run_id or new_run_id(parent)
    trials = sorted(sw["trials"], key=lambda t: t["mae"])
    extra = {"sweep": {"trials": len(trials), "best_cv_mae": sw["best_cv_mae"], "nested": sw["nested"],
                       "n_candidates": sw["n_candidates"], "seconds": sw["seconds"], "spec": spec,
                       "top": trials[:10]}}
    meta = write_run(parent / run_id, name, run_id, dataset, raw, manifest, sw["best"], res, extra)
    pd.DataFrame([{**t, "features": " ".join(t["features"])} for t in trials]).to_csv(
        parent / run_id / "trials.csv", index=False, float_format="%.4g")
    return meta


def sweep_run(settings, dataset: str = "bp", spec: dict | None = None, name: str | None = None,
              base: Layer4Config | None = None, log=print, progress=None) -> dict:
    """Local sweep on data/datasets/<dataset>/ into data/models/<name>/<run_id>/."""
    from .model import check_name, default_name, layer4_config, load_dataset, models_dir

    base = base or layer4_config(settings)
    spec = spec or sweep_spec(settings)
    name = check_name(name or default_name(dataset, base, sweep=True))
    df, raw, manifest = load_dataset(settings, dataset)
    return sweep_to(models_dir(settings) / name, name, df, raw, manifest, dataset, base, spec, log, progress)
