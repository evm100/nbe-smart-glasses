"""Layer 4: BP models on a Layer-3 dataset, validated on people (or sessions) they never saw.

  rows      valid cuff readings of one site (cfg.role), sorted by session and time
  calibrate (calibrated mode) per session: the first reading is the calibration, every later
            one becomes (features - calibration's, target - calibration's); the model learns
            changes and predicts calibration + change
  CV        folds are whole subjects (or sessions): rows of one session are strongly
            correlated, so a random split would score how well the model remembers people
  baseline  what the model has to beat: "no change from the calibration" (calibrated) or the
            training folds' mean (not calibrated)
  holdout   cfg.holdout_subjects: never in CV, scored once by the model fitted on the rest

Needs scikit-learn (pip install -e ".[model]").
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Layer4Config, ModelConfig

KEY_COLS = ["row", "session_id", "subject", "protocol", "t_ref"]


def model_rows(df: pd.DataFrame, cfg: Layer4Config) -> pd.DataFrame:
    """Valid bp rows of cfg.role with every target present, in session/time order."""
    if df.empty:
        return df
    m = (df["kind"] == "bp") & (df["role"] == cfg.role) & df["valid"].astype(bool)
    for t in cfg.targets:
        m &= np.isfinite(pd.to_numeric(df[t], errors="coerce"))
    return df[m].sort_values(["session_id", "t_ref"]).reset_index(drop=True)


def usable_features(df: pd.DataFrame, cfg: Layer4Config) -> tuple[list[str], list[str]]:
    """(features with at least one value, configured features that are missing or all NaN)."""
    ok = [f for f in cfg.features if f in df and pd.to_numeric(df[f], errors="coerce").notna().any()]
    return ok, [f for f in cfg.features if f not in ok]


def calibrate(df: pd.DataFrame, cfg: Layer4Config, features: list[str]) -> pd.DataFrame:
    """Rows after each session's first one, with cal_<target> and features as changes from it.

    Sessions with a single reading have nothing to predict and are dropped."""
    parts = []
    for _, g in df.groupby("session_id", sort=False):
        if len(g) < 2:
            continue
        cal, rest = g.iloc[0], g.iloc[1:].copy()
        for t in cfg.targets:
            rest[f"cal_{t}"] = float(cal[t])
        for f in features:
            rest[f] = pd.to_numeric(rest[f], errors="coerce") - pd.to_numeric(cal[f], errors="coerce")
        rest["cal_row"] = int(cal["row"]) if "row" in g else -1
        parts.append(rest)
    return pd.concat(parts, ignore_index=True) if parts else df.iloc[0:0].copy()


def make_model(mcfg: ModelConfig):
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if mcfg.kind == "ridge":
        return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                             Ridge(alpha=mcfg.alpha))
    if mcfg.kind == "hgb":  # handles NaN itself
        return HistGradientBoostingRegressor(max_iter=mcfg.max_iter, learning_rate=mcfg.learning_rate,
                                             max_depth=mcfg.max_depth, min_samples_leaf=mcfg.min_samples_leaf)
    raise ValueError(f"model.kind {mcfg.kind!r}: use 'ridge' or 'hgb'")


def _xy(df: pd.DataFrame, features: list[str], target: str, calibrated: bool) -> tuple[np.ndarray, np.ndarray]:
    X = df[features].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    y = df[target].to_numpy(float) - (df[f"cal_{target}"].to_numpy(float) if calibrated else 0.0)
    return X, y


def cv_groups(df: pd.DataFrame, cfg: Layer4Config) -> tuple[str, np.ndarray]:
    """('subject' | 'session', group label per row)."""
    if cfg.cv not in ("subject", "session"):
        raise ValueError(f"cv {cfg.cv!r}: use 'subject' or 'session'")
    if cfg.cv == "subject" and df["subject"].nunique() >= cfg.min_groups:
        return "subject", df["subject"].astype(str).to_numpy()
    return "session", df["session_id"].astype(str).to_numpy()


def error_metrics(y: np.ndarray, pred: np.ndarray, cal: np.ndarray | None = None) -> dict:
    """Mean error (bias) and its SD (the AAMI pair: <= 5 +- 8 mmHg), MAE, Pearson r, and with a
    calibration: r between predicted and measured change (does it track BP at all?)."""
    e = pred - y
    out = {"n": int(len(y)), "me": float(np.mean(e)) if len(e) else np.nan,
           "sd": float(np.std(e, ddof=1)) if len(e) > 1 else np.nan,
           "mae": float(np.mean(np.abs(e))) if len(e) else np.nan, "r": np.nan, "r_change": np.nan}
    if len(y) > 2 and np.std(y) > 0 and np.std(pred) > 0:
        out["r"] = float(np.corrcoef(y, pred)[0, 1])
    if cal is not None and len(y) > 2:
        dy, dp = y - cal, pred - cal
        if np.std(dy) > 0 and np.std(dp) > 0:
            out["r_change"] = float(np.corrcoef(dy, dp)[0, 1])
    return out


def prepare(df: pd.DataFrame, cfg: Layer4Config, log=print) -> tuple[pd.DataFrame, list[str], list[str], int]:
    """(rows ready to model - calibrated when cfg.calibrated -, features used, features skipped,
    number of readings before calibration)."""
    rows = model_rows(df, cfg)
    if rows.empty:
        raise ValueError(f"no valid bp readings for role {cfg.role!r} in the dataset")
    features, dropped = usable_features(rows, cfg)
    if not features:
        raise ValueError(f"none of the features {list(cfg.features)} have values for role {cfg.role!r}")
    if dropped:
        log(f"  features without values for {cfg.role}, skipped: {', '.join(dropped)}")
    n_readings = len(rows)
    if cfg.calibrated:
        rows = calibrate(rows, cfg, features)
    return rows, features, dropped, n_readings


def out_of_fold(dev: pd.DataFrame, cfg: Layer4Config, features: list[str], groups: np.ndarray
                ) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """{target: (y, prediction, baseline)} with every row predicted by a model that never saw its
    group. In the model's units: the change from the calibration when cfg.calibrated."""
    out = {}
    for t in cfg.targets:
        X, y = _xy(dev, features, t, cfg.calibrated)
        pred, base = np.full(len(dev), np.nan), np.full(len(dev), np.nan)
        for g in dict.fromkeys(groups):
            te = groups == g
            pred[te] = make_model(cfg.model).fit(X[~te], y[~te]).predict(X[te])
            base[te] = 0.0 if cfg.calibrated else float(np.mean(y[~te]))
        out[t] = (y, pred, base)
    return out


def cv_mae(dev: pd.DataFrame, cfg: Layer4Config, features: list[str], groups: np.ndarray) -> float:
    """Mean over targets of the out-of-fold MAE in mmHg (the sweep objective)."""
    return float(np.mean([np.mean(np.abs(p - y)) for y, p, _ in out_of_fold(dev, cfg, features, groups).values()]))


def fit_predict(train_rows: pd.DataFrame, test_rows: pd.DataFrame, cfg: Layer4Config, features: list[str]
                ) -> dict[str, np.ndarray]:
    """{target: prediction in mmHg for test_rows} from models fitted on train_rows."""
    out = {}
    for t in cfg.targets:
        X, y = _xy(train_rows, features, t, cfg.calibrated)
        Xt, _ = _xy(test_rows, features, t, cfg.calibrated)
        out[t] = make_model(cfg.model).fit(X, y).predict(Xt) + (
            test_rows[f"cal_{t}"].to_numpy(float) if cfg.calibrated else 0.0)
    return out


def train(df: pd.DataFrame, cfg: Layer4Config, log=print) -> dict:
    """Cross-validate, score the holdout, fit the final models.

    Returns {"preds": DataFrame (one row per reading x target: y, pred, baseline, split, fold),
    "metrics": {target: {split: {"model": ..., "baseline": ...}}}, "models": {target: fitted},
    "info": what was used}. Raises ValueError when there is nothing to validate on."""
    rows, features, dropped, n_readings = prepare(df, cfg, log)
    hold = rows["subject"].isin(list(cfg.holdout_subjects)).to_numpy()
    dev = rows[~hold].reset_index(drop=True)
    test = rows[hold].reset_index(drop=True)
    if dev.empty:
        raise ValueError(f"no {cfg.role} readings to train on"
                         + (" (sessions need 2+ valid readings when calibrated)" if cfg.calibrated else ""))
    unit, groups = cv_groups(dev, cfg)
    folds = list(dict.fromkeys(groups))
    if len(folds) < 2:
        raise ValueError(f"cross-validation needs 2+ {unit}s, have {len(folds)}")
    if unit != cfg.cv:
        log(f"  only {dev['subject'].nunique()} subject(s): folds are sessions, so the scores say how well "
            f"it does on a new day of a known person, not on a new person")

    preds, metrics, models = [], {}, {}
    keys = [c for c in KEY_COLS if c in rows]
    for t, (y, pred, base) in out_of_fold(dev, cfg, features, groups).items():
        X, _ = _xy(dev, features, t, cfg.calibrated)
        models[t] = make_model(cfg.model).fit(X, y)
        splits = [("cv", dev, pred, base, groups)]
        if len(test):
            Xt, _ = _xy(test, features, t, cfg.calibrated)
            splits.append(("holdout", test, models[t].predict(Xt),
                           np.zeros(len(test)) if cfg.calibrated else np.full(len(test), float(np.mean(y))),
                           np.full(len(test), "holdout")))
        metrics[t] = {}
        for split, part, p, b, fold in splits:
            cal = part[f"cal_{t}"].to_numpy(float) if cfg.calibrated else np.zeros(len(part))
            yy = part[t].to_numpy(float)
            p, b = p + cal, b + cal  # back to mmHg
            c = cal if cfg.calibrated else None
            metrics[t][split] = {"model": error_metrics(yy, p, c), "baseline": error_metrics(yy, b, c)}
            preds.append(part[keys].assign(target=t, split=split, fold=fold, y=yy, pred=p, baseline=b,
                                           cal=cal if cfg.calibrated else np.nan))

    info = {"n_readings": int(n_readings), "n_scored": int(len(rows)), "n_dev": int(len(dev)),
            "n_holdout": int(len(test)), "subjects": sorted(dev["subject"].astype(str).unique()),
            "holdout_subjects": sorted(test["subject"].astype(str).unique()), "cv_unit": unit,
            "n_folds": len(folds), "features": features, "features_skipped": dropped}
    return {"preds": pd.concat(preds, ignore_index=True), "metrics": metrics, "models": models, "info": info}


def coefficients(model, features: list[str]) -> dict[str, float] | None:
    """Ridge weights per standardised feature (mmHg per SD), None for other models."""
    last = model.steps[-1][1] if hasattr(model, "steps") else None
    if last is None or not hasattr(last, "coef_"):
        return None
    return {f: float(c) for f, c in zip(features, np.ravel(last.coef_))}
