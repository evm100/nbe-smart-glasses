"""Windowed signal-quality indices (SQI) and quick heart-rate estimates.

Each window gets several complementary metrics; a window is "good" only if
all pass. Thresholds are starting heuristics from the PPG-SQI literature
(e.g. Elgendi 2016 skewness SQI, autocorrelation periodicity) and should be
calibrated against your reference finger PPG / pulse-ox recordings.

Metrics (per channel, prefixed ``<ch>_``):
  valid_frac   fraction of samples not masked (gaps, saturation, ...)
  pi_pct       perfusion index = AC amplitude / DC * 100
  skew, kurt   distribution shape of the morphology-band signal
  hr_spec_bpm  dominant spectral frequency in the HR band
  spec_purity  power near f0 and 2*f0 / total in-band power
  hr_acf_bpm   period from the autocorrelation peak
  acf_peak     height of that peak (periodicity strength, 0..1)
  amp_ratio    window AC amplitude / session median (motion -> large)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import periodogram
from scipy.stats import kurtosis, skew

from .config import QualityConfig


def _parabolic(y: np.ndarray, k: int) -> float:
    if 0 < k < y.size - 1:
        a, b, c = y[k - 1], y[k], y[k + 1]
        den = a - 2 * b + c
        if den != 0:
            return k + 0.5 * (a - c) / den
    return float(k)


def acf_hr(x: np.ndarray, fs: float, hr_min: float, hr_max: float) -> tuple[float, float]:
    x = x - np.mean(x)
    n = x.size
    f = np.fft.rfft(x, 2 * n)
    r = np.fft.irfft(np.abs(f) ** 2)[:n]
    if r[0] <= 0:
        return np.nan, np.nan
    r = r / r[0]
    lo = max(1, int(np.floor(fs * 60.0 / hr_max)))
    hi = min(n - 2, int(np.ceil(fs * 60.0 / hr_min)))
    if hi <= lo:
        return np.nan, np.nan
    seg = r[lo:hi + 1]
    k = int(np.argmax(seg))
    lag = lo + _parabolic(seg, k)
    return 60.0 * fs / lag, float(seg[k])


def spectral_hr(x: np.ndarray, fs: float, hr_min: float, hr_max: float, band: tuple) -> tuple[float, float]:
    nfft = max(8192, int(2 ** np.ceil(np.log2(x.size))))
    f, p = periodogram(x - np.mean(x), fs=fs, window="hann", nfft=nfft)
    in_hr = (f >= hr_min / 60) & (f <= hr_max / 60)
    if not in_hr.any() or p[in_hr].sum() == 0:
        return np.nan, np.nan
    k = int(np.argmax(np.where(in_hr, p, -np.inf)))
    f0 = f[0] + _parabolic(p, k) * (f[1] - f[0])
    in_band = (f >= band[0]) & (f <= band[1])
    halfwidth = max(0.25, 2.0 * fs / x.size)  # ~Hann main lobe
    near = (np.abs(f - f0) <= halfwidth) | (np.abs(f - 2 * f0) <= halfwidth)
    total = p[in_band].sum()
    purity = float(p[in_band & near].sum() / total) if total > 0 else np.nan
    return 60.0 * f0, purity


def window_starts(n: int, fs: float, window_s: float, step_s: float) -> np.ndarray:
    w, s = int(round(window_s * fs)), int(round(step_s * fs))
    if n < w:
        return np.array([], dtype=int)
    return np.arange(0, n - w + 1, s)


def compute_quality(signals: pd.DataFrame, channels: list[str], fs: float, cfg: QualityConfig,
                    morph_band: tuple = (0.5, 8.0)) -> pd.DataFrame:
    n = len(signals)
    w = int(round(cfg.window_s * fs))
    starts = window_starts(n, fs, cfg.window_s, cfg.step_s)
    t = signals["t"].to_numpy()
    rows = []
    for s in starts:
        e = s + w
        row = {"t_start": t[s], "t_end": t[e - 1], "t_center": 0.5 * (t[s] + t[e - 1])}
        for ch in channels:
            ac = signals[f"{ch}_ac"].to_numpy()[s:e]
            ac_hr = signals[f"{ch}_ac_hr"].to_numpy()[s:e]
            acn = signals[f"{ch}_ac_norm"].to_numpy()[s:e]
            dc = signals[f"{ch}_dc"].to_numpy()[s:e]
            ok = np.isfinite(ac) & np.isfinite(ac_hr) & np.isfinite(dc)
            m = {"valid_frac": ok.mean()}
            if ok.sum() >= 0.5 * w:
                # metrics computed on the valid samples only (gaps squeezed out;
                # windows with many gaps are rejected anyway by valid_frac)
                a, ah, an, d = ac[ok], ac_hr[ok], acn[ok], dc[ok]
                amp = np.percentile(a, 95) - np.percentile(a, 5)
                m["pi_pct"] = 100.0 * amp / np.mean(d) if np.mean(d) != 0 else np.nan
                m["amp_norm"] = np.percentile(an, 95) - np.percentile(an, 5)
                m["skew"] = float(skew(a))
                m["kurt"] = float(kurtosis(a))
                m["hr_spec_bpm"], m["spec_purity"] = spectral_hr(ah, fs, cfg.hr_min_bpm, cfg.hr_max_bpm, morph_band)
                m["hr_acf_bpm"], m["acf_peak"] = acf_hr(ah, fs, cfg.hr_min_bpm, cfg.hr_max_bpm)
            for k, v in m.items():
                row[f"{ch}_{k}"] = v
        rows.append(row)

    q = pd.DataFrame(rows)
    if q.empty:
        return q
    for ch in channels:
        col = f"{ch}_amp_norm"
        if col in q:
            med = np.nanmedian(q[col])
            q[f"{ch}_amp_ratio"] = q[col] / med if med and np.isfinite(med) else np.nan
        _label(q, ch, cfg)

    primary = cfg.primary_channel or channels[0]
    q["good"] = q[f"{primary}_good"]
    q["reason"] = q[f"{primary}_reason"]
    q["hr_bpm"] = np.where(q["good"], q.get(f"{primary}_hr_acf_bpm"), np.nan)
    return q


def _label(q: pd.DataFrame, ch: str, cfg: QualityConfig) -> None:
    def col(name):
        c = f"{ch}_{name}"
        return q[c].to_numpy(dtype=float) if c in q else np.full(len(q), np.nan)

    checks = {
        "gaps": col("valid_frac") >= cfg.min_valid_frac,
        "aperiodic": col("acf_peak") >= cfg.min_acf_peak,
        "hr_disagree": np.abs(col("hr_spec_bpm") - col("hr_acf_bpm")) <= cfg.max_hr_disagreement_bpm,
        "skew": col("skew") >= cfg.min_skew,
        "amplitude": (col("amp_ratio") >= cfg.amp_ratio_range[0]) & (col("amp_ratio") <= cfg.amp_ratio_range[1]),
        "spectrum": col("spec_purity") >= cfg.min_spec_purity,
    }
    good = np.ones(len(q), dtype=bool)
    reasons = [[] for _ in range(len(q))]
    for name, passed in checks.items():
        passed = np.asarray(passed, dtype=bool)  # NaN comparisons -> False
        good &= passed
        for i in np.flatnonzero(~passed):
            reasons[i].append(name)
    q[f"{ch}_good"] = good
    q[f"{ch}_reason"] = [";".join(r) for r in reasons]
