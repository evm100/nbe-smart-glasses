"""Layer 4 runs: train + validate a model config on a Layer-3 dataset, keep every run.

    data/models/<name>/<run_id>/
      config.json       the Layer-4 settings used
      metrics.json      CV (and holdout) scores of the model and the baseline per target,
                        what was used (rows, folds, features), which dataset build (sha256 of rows.csv)
      predictions.csv   one row per (reading, target): cuff value, out-of-fold prediction, baseline
      model.joblib      final models fitted on all non-holdout rows ({target: model}, features)
      report.png        predicted vs cuff, Bland-Altman, weights

Runs are never overwritten, so `sglab model list` shows how the scores move as the dataset grows.

Settings: config/pipeline_layer4.json (ppg.config.Layer4Config; created by `sglab init`).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import ppg
from ppg.config import Layer4Config

NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def layer4_config(settings) -> Layer4Config:
    p = settings.root / "config" / "pipeline_layer4.json"
    return Layer4Config.from_json(p) if p.exists() else Layer4Config()


def models_dir(settings) -> Path:
    return settings.data_root / "models"


def load_dataset(settings, dataset: str) -> tuple[pd.DataFrame, bytes, dict]:
    """(rows, rows.csv bytes for the provenance hash, manifest) of data/datasets/<dataset>/."""
    return read_dataset(settings.data_root / "datasets" / dataset, dataset)


def read_dataset(ds: Path, dataset: str = "bp") -> tuple[pd.DataFrame, bytes, dict]:
    if not (ds / "rows.csv").exists():
        raise FileNotFoundError(f"{ds}/rows.csv: build it first (sglab dataset build --name {dataset})")
    raw = (ds / "rows.csv").read_bytes()
    df = pd.read_csv(ds / "rows.csv", low_memory=False) if raw.strip() else pd.DataFrame()
    manifest = json.loads((ds / "manifest.json").read_text()) if (ds / "manifest.json").exists() else {}
    return df, raw, manifest


def check_name(name: str) -> str:
    if not NAME.fullmatch(name or ""):
        raise ValueError(f"model name {name!r}: use letters, digits, - _ .")
    return name


def default_name(dataset: str, cfg: Layer4Config, sweep: bool = False) -> str:
    return f"{dataset}-{cfg.role}-{'sweep' if sweep else cfg.model.kind}{'-cal' if cfg.calibrated else ''}"


def new_run_id(parent: Path) -> str:
    """UTC time stamp, with -2, -3 ... when a run of that second exists."""
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = base
    for k in range(2, 1000):
        if not (parent / run_id).exists():
            break
        run_id = f"{base}-{k}"
    return run_id


def write_run(out: Path, name: str, run_id: str, dataset: str, raw: bytes, manifest: dict, cfg: Layer4Config,
              res: dict, extra: dict | None = None) -> dict:
    """Write a run folder from layer4.train()'s result. extra: more keys for metrics.json (sweeps)."""
    import joblib

    from ppg.layer4 import coefficients
    from ppg.plotting4 import plot_report

    out.mkdir(parents=True)
    coefs = {t: coefficients(m, res["info"]["features"]) for t, m in res["models"].items()}
    cfg.to_json(out / "config.json")
    meta = {
        "name": name, "run_id": run_id, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ppg_version": ppg.__version__,
        "dataset": {"name": dataset, "built_at": manifest.get("built_at"),
                    "rows_sha256": hashlib.sha256(raw).hexdigest()[:16]},
        "role": cfg.role, "model": cfg.model.kind, "calibrated": cfg.calibrated,
        **res["info"], "metrics": res["metrics"], "coefficients": coefs, **(extra or {}),
    }
    (out / "metrics.json").write_text(json.dumps(meta, indent=2, default=float))
    res["preds"].to_csv(out / "predictions.csv", index=False, float_format="%.4g")
    joblib.dump({"models": res["models"], "features": res["info"]["features"], "config": cfg.to_dict()},
                out / "model.joblib")
    cal = "calibrated, " if cfg.calibrated else ""
    plot_report(res["preds"], res["metrics"], coefs,
                f"{name}  {run_id}  ({cal}{res['info']['n_folds']} {res['info']['cv_unit']} folds, "
                f"n={res['info']['n_dev']}, dataset built {manifest.get('built_at', '?')})", out / "report.png")
    meta["path"] = str(out)
    return meta


def train_run(settings, dataset: str = "bp", cfg: Layer4Config | None = None, name: str | None = None,
              log=print) -> dict:
    """Train + validate one config on data/datasets/<dataset>/, write a run folder. Returns metrics.json."""
    from ppg.layer4 import train

    cfg = cfg or layer4_config(settings)
    name = check_name(name or default_name(dataset, cfg))
    df, raw, manifest = load_dataset(settings, dataset)
    res = train(df, cfg, log=log)
    parent = models_dir(settings) / name
    run_id = new_run_id(parent)
    return write_run(parent / run_id, name, run_id, dataset, raw, manifest, cfg, res)


def list_runs(settings, name: str | None = None) -> list[dict]:
    """metrics.json of every run (oldest first), optionally of one model name."""
    d = models_dir(settings)
    pat = f"{name}/*/metrics.json" if name else "*/*/metrics.json"
    runs = [json.loads(p.read_text()) | {"path": str(p.parent)} for p in d.glob(pat)] if d.is_dir() else []
    return sorted(runs, key=lambda m: (m["trained_at"], m["run_id"], m["name"]))


def get_run(settings, name: str, run_id: str) -> dict | None:
    if not (NAME.fullmatch(name) and NAME.fullmatch(run_id)):
        return None
    p = models_dir(settings) / name / run_id / "metrics.json"
    return json.loads(p.read_text()) | {"path": str(p.parent)} if p.exists() else None


LABEL = {"sys_mmhg": "systolic", "dia_mmhg": "diastolic", "map_mmhg": "mean arterial"}


def explain(m: dict) -> dict:
    """Plain-language reading of a run's metrics.json for the Models page.

    {"targets": [{target, label, mae, base_mae, me, sd, r_change, n, verdict (good|same|bad), text}],
     "notes": [...], "sweep": text | None}"""
    cal = m.get("calibrated")
    base_desc = ("Assuming BP stays at each session's first (calibration) reading" if cal
                 else "Guessing the training subjects' average")
    out, notes = [], []
    for t, sp in m["metrics"].items():
        mm, mb = sp["cv"]["model"], sp["cv"]["baseline"]
        label = LABEL.get(t, t)
        if mm["mae"] < 0.95 * mb["mae"]:
            verdict, why = "good", "The model beats the baseline: the PPG carries information about this."
        elif mm["mae"] > 1.05 * mb["mae"]:
            verdict, why = "bad", "The model is worse than the baseline: it hasn't learned anything useful yet."
        else:
            verdict, why = "same", "The model is no better than the baseline yet."
        text = (f"On readings it never trained on, the model's {label} is off by {mm['mae']:.1f} mmHg on average. "
                f"{base_desc} is off by {mb['mae']:.1f} mmHg. {why}")
        if cal and mb["mae"] < 5:
            text += (f" BP only moved {mb['mae']:.1f} mmHg on average from the calibration reading, so there is "
                     f"little change to learn from: sessions that raise or lower BP will help most.")
        out.append({"target": t, "label": label, "mae": mm["mae"], "base_mae": mb["mae"], "me": mm["me"],
                    "sd": mm["sd"], "r_change": mm.get("r_change"), "n": mm["n"], "verdict": verdict, "text": text,
                    "holdout": sp.get("holdout", {}).get("model")})
    if m.get("cv_unit") == "session":
        n = len(m.get("subjects", []))
        notes.append(f"Only {n} subject{'s' if n != 1 else ''}, so the folds are sessions: the scores say how the "
                     f"model does on a new day of a person it already knows, not on a new person.")
    if m.get("n_dev", 0) < 50:
        notes.append(f"{m.get('n_dev')} readings scored. With this few, scores move a lot from run to run: "
                     f"treat differences under ~1-2 mmHg as noise.")
    if m.get("features_skipped"):
        notes.append(f"Skipped (no values for {m.get('role')}): {', '.join(m['features_skipped'])}.")
    sw, sweep_text = m.get("sweep"), None
    if sw:
        sweep_text = (f"Tried {sw['trials']} settings. The best scored MAE {sw['best_cv_mae']:.1f} mmHg "
                      f"(averaged over targets), but picking the best of many flatters it.")
        nested = sw.get("nested")
        if nested:
            nm = np.mean([v["model"]["mae"] for v in nested["metrics"].values()])
            nb = np.mean([v["baseline"]["mae"] for v in nested["metrics"].values()])
            sweep_text += (f" The honest check (search repeated without each {nested['unit']}, its winner scored "
                           f"on them) gives {nm:.1f} mmHg against the baseline's {nb:.1f}.")
            if nm > 1.2 * sw["best_cv_mae"]:
                sweep_text += (" That gap means the search is partly fitting noise: trust the honest number, "
                               "and use fewer features or more data.")
        else:
            sweep_text += " The honest (nested) check needs 3+ subjects or sessions."
    return {"targets": out, "notes": notes, "sweep": sweep_text}
