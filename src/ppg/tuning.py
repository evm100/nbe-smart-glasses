"""Label-free scores for tuning Layers 1-2 (no blood pressure involved).

A cuff gives one label every few minutes, a recording gives thousands of beats. So the
pipeline settings are judged on beats, and the cuff readings stay untouched for testing
the BP model later (tuning against them would fit their noise; Cawley & Talbot 2010).

reference_agreement  A target stream (glasses, head) against the finger stream recorded at
                     the same time. Both see the same heart beats, so every finger beat
                     should show up at the target, shifted by a constant pulse-arrival
                     difference. Scores: sensitivity, PPV, F1 of the *good* beats, the
                     beat-to-beat interval error, and foot-timing jitter (cf. Charlton et al.
                     2022, "Detecting beats in the PPG: benchmarking open-source algorithms").
feature_jitter       Beat-to-beat noise of morphology features over consecutive good beats.
                     Lower = cleaner, but heavy smoothing also lowers it, so it is always
                     paired with shape_error.
shape_error          Synthetic recording with known waveform: RMSE between the median good
                     beat and the same recording's noise-free beat. Penalises both noise and
                     over-smoothing (a too-low cut-off rounds off the notch).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

JITTER_FEATURES = ["t_rise_s", "pw25_s", "pw50_s", "sys_frac", "notch_rel", "dia_rel", "ipa",
                   "max_slope_norm", "b_a"]


def pulse_offset(ref_t: np.ndarray, tgt_t: np.ndarray, max_lag_s: float = 0.6, bin_s: float = 0.004) -> float:
    """Most common (target - reference) beat time difference within +-max_lag_s: the pulse
    arrival difference between the two sites plus any clock offset. NaN if no beats."""
    ref_t, tgt_t = np.sort(ref_t), np.sort(tgt_t)
    if ref_t.size < 3 or tgt_t.size < 3:
        return np.nan
    lo = np.searchsorted(ref_t, tgt_t - max_lag_s)
    hi = np.searchsorted(ref_t, tgt_t + max_lag_s)
    d = np.concatenate([t - ref_t[a:b] for t, a, b in zip(tgt_t, lo, hi)])
    if d.size == 0:
        return np.nan
    edges = np.arange(-max_lag_s, max_lag_s + bin_s, bin_s)
    h, _ = np.histogram(d, edges)
    h = np.convolve(h, np.ones(5), mode="same")  # +-2 bins: robust to foot jitter
    mode = 0.5 * (edges[np.argmax(h)] + edges[np.argmax(h) + 1])
    near = d[np.abs(d - mode) <= 0.03]
    return float(np.median(near)) if near.size else float(mode)


def match_beats(ref_t: np.ndarray, tgt_t: np.ndarray, tol_s: float) -> tuple[np.ndarray, np.ndarray]:
    """One-to-one nearest matches |tgt - ref| <= tol_s (both sorted). Returns (ref_idx, tgt_idx)."""
    if ref_t.size == 0 or tgt_t.size == 0:
        return np.array([], int), np.array([], int)
    k = np.clip(np.searchsorted(ref_t, tgt_t), 1, ref_t.size - 1) if ref_t.size > 1 else np.zeros(tgt_t.size, int)
    if ref_t.size > 1:
        k = np.where(np.abs(ref_t[k - 1] - tgt_t) <= np.abs(ref_t[k] - tgt_t), k - 1, k)
    d = np.abs(ref_t[k] - tgt_t)
    ok = d <= tol_s
    ri, ti, d = k[ok], np.flatnonzero(ok), d[ok]
    # a reference beat claimed twice keeps its closest target beat
    order = np.lexsort((d, ri))
    ri, ti = ri[order], ti[order]
    first = np.r_[True, ri[1:] != ri[:-1]]
    ri, ti = ri[first], ti[first]
    order = np.argsort(ti)
    return ri[order], ti[order]


def reference_agreement(ref_t: np.ndarray, ref_good: np.ndarray, tgt_t: np.ndarray, tgt_good: np.ndarray,
                        span: tuple[float, float] | None = None, tol_s: float = 0.1,
                        offset: float | None = None) -> dict:
    """Good target beats vs. good reference beats, all times on one (session) clock.

    ref_t/tgt_t: foot times of *all* detected beats (sorted); *_good: their beat_good flags.
    span: the target's recording time range (reference beats outside it can't be found).
    Counts are returned (not only rates) so recordings can be pooled: see pool_agreement().
    """
    ref_t, ref_good = np.asarray(ref_t, float), np.asarray(ref_good, bool)
    tgt_t, tgt_good = np.asarray(tgt_t, float), np.asarray(tgt_good, bool)
    if offset is None:
        offset = pulse_offset(ref_t[ref_good], tgt_t)
    out = {"offset_s": offset, "n_ref": 0, "n_tgt": 0, "tp": 0, "ibi_abs_err_sum": 0.0, "n_ibi": 0,
           "timing_err": []}
    rg = np.flatnonzero(ref_good)
    if span is not None:
        rg = rg[(ref_t[rg] >= span[0]) & (ref_t[rg] <= span[1])]
    out["n_ref"] = int(rg.size)
    if not np.isfinite(offset) or rg.size == 0:
        out["n_tgt"] = int(tgt_good.sum())
        return out
    t_shift = tgt_t - offset
    tg = np.flatnonzero(tgt_good)
    # only target beats where the reference is clean count against precision
    r_clean = ref_t[rg]
    j = np.clip(np.searchsorted(r_clean, t_shift[tg]), 0, r_clean.size - 1)
    near = np.minimum(np.abs(r_clean[j] - t_shift[tg]), np.abs(r_clean[np.maximum(j - 1, 0)] - t_shift[tg]))
    tg = tg[near <= 1.5]
    out["n_tgt"] = int(tg.size)
    mi_r, mi_t = match_beats(ref_t[rg], t_shift[tg], tol_s)
    out["tp"] = int(mi_r.size)
    if mi_r.size == 0:
        return out
    r_idx, t_idx = rg[mi_r], tg[mi_t]           # indices into the full beat lists
    out["timing_err"] = list(t_shift[t_idx] - ref_t[r_idx])
    # beat-to-beat intervals: consecutive target beats matched to consecutive reference beats
    nxt = (np.diff(t_idx) == 1) & (np.diff(r_idx) == 1)
    if nxt.any():
        e = np.diff(tgt_t[t_idx])[nxt] - np.diff(ref_t[r_idx])[nxt]
        out["ibi_abs_err_sum"], out["n_ibi"] = float(np.abs(e).sum()), int(e.size)
    return out


def pool_agreement(parts: list[dict]) -> dict:
    """Pooled scores over several recordings' reference_agreement() results."""
    n_ref = sum(p["n_ref"] for p in parts)
    n_tgt = sum(p["n_tgt"] for p in parts)
    tp = sum(p["tp"] for p in parts)
    n_ibi = sum(p["n_ibi"] for p in parts)
    sens = tp / n_ref if n_ref else 0.0
    ppv = tp / n_tgt if n_tgt else 0.0
    errs = []
    for p in parts:  # timing jitter around each recording's own offset
        e = np.asarray(p["timing_err"], float)
        if e.size:
            errs.append(e - np.median(e))
    errs = np.concatenate(errs) if errs else np.array([])
    return {
        "sensitivity": sens, "ppv": ppv, "f1": 2 * sens * ppv / (sens + ppv) if sens + ppv else 0.0,
        "ibi_mae_ms": 1000 * sum(p["ibi_abs_err_sum"] for p in parts) / n_ibi if n_ibi else np.nan,
        "foot_jitter_ms": 1000 * 1.4826 * float(np.median(np.abs(errs))) if errs.size else np.nan,
        "n_ref": n_ref, "n_tgt": n_tgt, "tp": tp, "n_ibi": n_ibi,
    }


def feature_jitter(beats: pd.DataFrame, features: list[str] = JITTER_FEATURES) -> dict[str, np.ndarray]:
    """Per feature: |change| between consecutive good beats, relative to the feature's median size
    in this recording. Pool over recordings with pool_jitter()."""
    out = {}
    if beats.empty or "beat_good" not in beats:
        return out
    g = beats["beat_good"].to_numpy(bool)
    pair = g[:-1] & g[1:]
    for f in features:
        if f not in beats:
            continue
        x = beats[f].to_numpy(float)
        scale = np.nanmedian(np.abs(x[g])) if g.any() else np.nan
        if not np.isfinite(scale) or scale == 0:
            continue
        d = np.abs(np.diff(x))[pair] / scale
        out[f] = d[np.isfinite(d)]
    return out


def pool_jitter(parts: list[dict[str, np.ndarray]]) -> float:
    """Median over features of the pooled median relative beat-to-beat change."""
    per = []
    for f in {k for p in parts for k in p}:
        d = np.concatenate([p[f] for p in parts if f in p])
        if d.size:
            per.append(np.median(d))
    return float(np.median(per)) if per else np.nan


def median_wave(waves: np.ndarray, good: np.ndarray) -> np.ndarray | None:
    w = waves[np.asarray(good, bool)] if len(waves) else waves
    w = w[np.isfinite(w).all(axis=1)] if len(w) else w
    return np.median(w, axis=0) if len(w) >= 5 else None


def shape_error(waves: np.ndarray, good: np.ndarray, truth_wave: np.ndarray) -> float:
    """RMSE (0..1 units of the normalised beat) between the median good beat and truth_wave."""
    m = median_wave(waves, good)
    return float(np.sqrt(np.mean((m - truth_wave) ** 2))) if m is not None else 1.0
