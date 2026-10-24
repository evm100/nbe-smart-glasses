"""Layer 2d: epoch aggregation -> one row per time window, ready for ML.

Why epochs? Reference labels (cuff BP, pulse-ox SpO2) arrive every few
seconds to minutes, not every beat, and single-beat features are noisy.
So we summarise good beats in sliding windows (default 30 s, 10 s step):

  * median and IQR of every beat feature  (robust centre + spread)
  * HR and HRV (SDNN, RMSSD, pNN50) from consecutive good beats only
  * ensemble-average beat template (100 points) -> input for deep models

Layer 3 will join these rows to the reference device by timestamp.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import EpochConfig

META_COLS = {"t_foot", "t_peak", "t_max_slope", "foot_tan_s", "beat_good", "beat_reason", "dc"}


def hrv(ibi: np.ndarray, consecutive: np.ndarray) -> dict:
    """ibi in s; consecutive[i] True when beat i+1 directly follows beat i and both are good."""
    out = {"sdnn_ms": np.nan, "rmssd_ms": np.nan, "pnn50": np.nan}
    if ibi.size >= 3:
        out["sdnn_ms"] = float(np.std(ibi, ddof=1) * 1e3)
    d = np.diff(ibi)[consecutive[:-1]] if ibi.size > 1 else np.array([])
    if d.size >= 2:
        out["rmssd_ms"] = float(np.sqrt(np.mean(d ** 2)) * 1e3)
        out["pnn50"] = float(np.mean(np.abs(d) > 0.05))
    return out


def aggregate_epochs(beats: pd.DataFrame, waves: np.ndarray, duration_s: float,
                     cfg: EpochConfig) -> tuple[pd.DataFrame, np.ndarray]:
    feat_cols = [c for c in beats.columns
                 if c not in META_COLS and not c.endswith("_idx") and pd.api.types.is_numeric_dtype(beats[c])
                 and c not in ("ibi_ratio", "amp_ratio", "template_corr", "notch_method", "gauss_ok")]
    starts = np.arange(0, max(duration_s - cfg.epoch_s, 0) + 1e-9, cfg.step_s)
    rows, templates = [], []
    good_all = beats["beat_good"].to_numpy(dtype=bool) if len(beats) else np.array([], bool)
    foot_idx = beats["foot_idx"].to_numpy() if len(beats) else np.array([])
    next_idx = beats["next_foot_idx"].to_numpy() if len(beats) else np.array([])

    for s in starts:
        e = s + cfg.epoch_s
        sel = (beats["t_foot"] >= s) & (beats["t_foot"] < e) if len(beats) else np.array([], bool)
        sel = np.asarray(sel, dtype=bool)
        g = sel & good_all
        row = {"t_start": s, "t_end": e, "t_center": s + cfg.epoch_s / 2,
               "n_beats": int(sel.sum()), "n_good": int(g.sum()),
               "good_frac": float(g.sum() / sel.sum()) if sel.sum() else 0.0}
        row["valid"] = row["n_good"] >= cfg.min_good_beats
        gb = beats.loc[g]
        for c in feat_cols:
            v = gb[c].to_numpy(dtype=float)
            v = v[np.isfinite(v)]
            row[f"{c}_med"] = float(np.median(v)) if v.size else np.nan
            row[f"{c}_iqr"] = float(np.subtract(*np.percentile(v, [75, 25]))) if v.size >= 4 else np.nan
        gi = np.flatnonzero(g)
        consecutive = np.zeros(gi.size, dtype=bool)
        if gi.size > 1:
            consecutive[:-1] = (np.diff(gi) == 1) & (next_idx[gi[:-1]] == foot_idx[gi[1:]])
        ibis = gb["ibi_s"].to_numpy()
        # mean-IBI HR is less quantised than the median of per-beat HR
        row["hr_mean_bpm"] = float(60.0 / np.mean(ibis)) if ibis.size else np.nan
        row.update(hrv(ibis, consecutive))
        rows.append(row)
        templates.append(np.nanmean(waves[gi], axis=0) if gi.size else np.full(waves.shape[1], np.nan))

    return pd.DataFrame(rows), np.array(templates) if templates else np.empty((0, waves.shape[1]))
