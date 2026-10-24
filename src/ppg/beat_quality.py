"""Layer 2c: beat-level signal quality.

Layer 1 judged 8-second windows. Features are per beat, so each beat needs
its own verdict. A single ectopic beat, a mis-detected peak or a small bump
can poison morphology features even inside a "good" window.

Checks (all must pass):
  physio     IBI within the HR limits
  rhythm     IBI within ±30 % of the local median (catches missed/extra beats)
  amplitude  within 0.5x-2x of the local median amplitude
  template   Pearson correlation with the local median beat >= 0.86
             (template matching, Li & Clifford 2012; Orphanidou 2015)
  window     beat lies entirely inside good Layer-1 windows
  notch      a dicrotic notch was found (optional, off by default)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import BeatQualityConfig


def normalised_beats(x: np.ndarray, fid: pd.DataFrame, n: int) -> np.ndarray:
    """Each beat foot->next foot, baseline-removed, resampled to n points, scaled 0..1."""
    out = np.full((len(fid), n), np.nan)
    grid = np.linspace(0, 1, n)
    for i, (f, nf) in enumerate(zip(fid["foot"].to_numpy(), fid["next_foot"].to_numpy())):
        seg = x[f:nf + 1]
        seg = seg - np.linspace(seg[0], seg[-1], seg.size)
        lo, hi = seg.min(), seg.max()
        if hi > lo:
            out[i] = np.interp(grid, np.linspace(0, 1, seg.size), (seg - lo) / (hi - lo))
    return out


def _rolling_median(v: np.ndarray, k: int) -> np.ndarray:
    return pd.Series(v).rolling(2 * k + 1, center=True, min_periods=1).median().to_numpy()


def beat_quality(feats: pd.DataFrame, fid: pd.DataFrame, waves: np.ndarray, good_sample: np.ndarray | None,
                 cfg: BeatQualityConfig) -> pd.DataFrame:
    n = len(feats)
    q = pd.DataFrame(index=feats.index)
    ibi = feats["ibi_s"].to_numpy()
    amp = feats["amp"].to_numpy()

    # physiological + rhythm
    checks = {}
    checks["physio"] = (ibi >= 60 / cfg.hr_max_bpm) & (ibi <= 60 / cfg.hr_min_bpm)
    ibi_med = _rolling_median(np.where(checks["physio"], ibi, np.nan), 5)
    q["ibi_ratio"] = ibi / ibi_med
    checks["rhythm"] = (q["ibi_ratio"] >= cfg.ibi_ratio_range[0]) & (q["ibi_ratio"] <= cfg.ibi_ratio_range[1])
    amp_med = _rolling_median(np.where(checks["physio"], amp, np.nan), 5)
    q["amp_ratio"] = amp / amp_med
    checks["amplitude"] = (q["amp_ratio"] >= cfg.amp_ratio_range[0]) & (q["amp_ratio"] <= cfg.amp_ratio_range[1])

    # template correlation: template = median of neighbouring beats that pass the basic checks
    basic = checks["physio"] & checks["rhythm"].to_numpy() & checks["amplitude"].to_numpy()
    corr = np.full(n, np.nan)
    k = cfg.template_neighbors
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        idx = [j for j in range(lo, hi) if basic[j] and j != i]
        if len(idx) >= 3 and np.isfinite(waves[i]).all():
            tmpl = np.nanmedian(waves[idx], axis=0)
            corr[i] = np.corrcoef(waves[i], tmpl)[0, 1]
    q["template_corr"] = corr
    checks["template"] = corr >= cfg.min_template_corr

    if good_sample is not None and cfg.require_layer1_good:
        f, nf = fid["foot"].to_numpy(), fid["next_foot"].to_numpy()
        csum = np.concatenate(([0], np.cumsum(~good_sample)))
        checks["window"] = (csum[nf + 1] - csum[f]) == 0
    if cfg.require_notch:
        checks["notch"] = fid["notch"].to_numpy() >= 0

    good = np.ones(n, dtype=bool)
    reasons = [[] for _ in range(n)]
    for name, ok in checks.items():
        ok = np.asarray(ok, dtype=bool)
        good &= ok
        for i in np.flatnonzero(~ok):
            reasons[i].append(name)
    q["beat_good"] = good
    q["beat_reason"] = [";".join(r) for r in reasons]
    return q


def good_sample_mask(n: int, quality_l1: pd.DataFrame, fs: float) -> np.ndarray:
    """Per-sample mask: True if the sample is covered by at least one good Layer-1 window."""
    m = np.zeros(n, dtype=bool)
    for _, w in quality_l1[quality_l1["good"].astype(bool)].iterrows():
        s = int(round(w["t_start"] * fs))
        e = int(round(w["t_end"] * fs)) + 1
        m[s:e] = True
    return m
