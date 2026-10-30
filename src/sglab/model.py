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

import pandas as pd

import ppg
from ppg.config import Layer4Config


def layer4_config(settings) -> Layer4Config:
    p = settings.root / "config" / "pipeline_layer4.json"
    return Layer4Config.from_json(p) if p.exists() else Layer4Config()


def models_dir(settings) -> Path:
    return settings.data_root / "models"


def train_run(settings, dataset: str = "bp", cfg: Layer4Config | None = None, name: str | None = None,
              log=print) -> dict:
    """Train + validate one config on data/datasets/<dataset>/, write a run folder. Returns metrics.json."""
    import joblib

    from ppg.layer4 import coefficients, train
    from ppg.plotting4 import plot_report

    cfg = cfg or layer4_config(settings)
    name = name or f"{dataset}-{cfg.role}-{cfg.model.kind}{'-cal' if cfg.calibrated else ''}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"model name {name!r}: use letters, digits, - _ .")
    ds = settings.data_root / "datasets" / dataset
    if not (ds / "rows.csv").exists():
        raise FileNotFoundError(f"{ds}/rows.csv: build it first (sglab dataset build --name {dataset})")
    raw = (ds / "rows.csv").read_bytes()
    df = pd.read_csv(ds / "rows.csv", low_memory=False) if raw.strip() else pd.DataFrame()
    manifest = json.loads((ds / "manifest.json").read_text()) if (ds / "manifest.json").exists() else {}

    res = train(df, cfg, log=log)
    coefs = {t: coefficients(m, res["info"]["features"]) for t, m in res["models"].items()}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = models_dir(settings) / name / run_id
    for k in range(2, 100):  # two runs in the same second
        if not out.exists():
            break
        run_id = f"{run_id.split('-')[0]}-{k}"
        out = out.with_name(run_id)
    out.mkdir(parents=True)
    cfg.to_json(out / "config.json")
    meta = {
        "name": name, "run_id": run_id, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ppg_version": ppg.__version__,
        "dataset": {"name": dataset, "built_at": manifest.get("built_at"),
                    "rows_sha256": hashlib.sha256(raw).hexdigest()[:16]},
        "role": cfg.role, "model": cfg.model.kind, "calibrated": cfg.calibrated,
        **res["info"], "metrics": res["metrics"], "coefficients": coefs,
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


def list_runs(settings, name: str | None = None) -> list[dict]:
    """metrics.json of every run (oldest first), optionally of one model name."""
    d = models_dir(settings)
    pat = f"{name}/*/metrics.json" if name else "*/*/metrics.json"
    runs = [json.loads(p.read_text()) | {"path": str(p.parent)} for p in d.glob(pat)] if d.is_dir() else []
    return sorted(runs, key=lambda m: (m["trained_at"], m["name"]))
