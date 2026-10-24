"""Timestamp repair: sort, de-duplicate, estimate fs, resample onto a uniform grid.

MAX3010x data logged through a microcontroller usually has host-side
timestamps with FIFO batching jitter, occasional duplicates and dropped
packets. Every downstream filter assumes uniform sampling, so we fix that
first and mark real dropouts as NaN rather than inventing data across them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def clean_timestamps(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Sort by time and average rows that share a timestamp."""
    attrs = dict(df.attrs)
    n0 = len(df)
    df = df.sort_values("t", kind="mergesort")
    df = df.groupby("t", as_index=False, sort=True).mean()
    df.attrs.update(attrs)
    return df.reset_index(drop=True), n0 - len(df)


def estimate_fs(t: np.ndarray) -> float:
    dt = np.diff(t)
    dt = dt[dt > 0]
    if dt.size == 0:
        raise ValueError("Need at least two distinct timestamps.")
    return 1.0 / float(np.median(dt))


def _nice_fs(fs: float) -> float:
    r = round(fs)
    return float(r) if r > 0 and abs(fs - r) / r < 0.03 else fs


def resample_uniform(
    df: pd.DataFrame,
    channels: list[str],
    fs: float | None = None,
    max_gap_s: float = 0.25,
) -> tuple[pd.DataFrame, dict]:
    """Linear-interpolate each channel onto a uniform grid.

    Grid points that fall inside a gap longer than ``max_gap_s`` (between the
    surrounding real samples of that channel) are set to NaN.
    """
    df, n_dup = clean_timestamps(df)
    t = df["t"].to_numpy(dtype=float)
    fs_est = estimate_fs(t)
    fs_used = float(fs) if fs else _nice_fs(fs_est)

    t_new = t[0] + np.arange(int(np.floor((t[-1] - t[0]) * fs_used)) + 1) / fs_used
    out = pd.DataFrame({"t": t_new - t_new[0]})

    gap_mask_any = np.zeros(t_new.size, dtype=bool)
    for ch in channels:
        y = df[ch].to_numpy(dtype=float)
        ok = np.isfinite(y)
        tc, yc = t[ok], y[ok]
        if tc.size < 2:
            out[ch] = np.nan
            continue
        yi = np.interp(t_new, tc, yc)
        idx = np.clip(np.searchsorted(tc, t_new, side="right"), 1, tc.size - 1)
        span = tc[idx] - tc[idx - 1]
        near = np.minimum(t_new - tc[idx - 1], tc[idx] - t_new) <= 0.5 / fs_used
        in_gap = ((span > max_gap_s) & ~near) | (t_new < tc[0]) | (t_new > tc[-1])
        yi[in_gap] = np.nan
        gap_mask_any |= in_gap
        out[ch] = yi

    dt = np.diff(t)
    report = {
        "fs_estimated": fs_est,
        "fs_used": fs_used,
        "n_samples_in": int(len(t) + n_dup),
        "n_samples_out": int(t_new.size),
        "n_duplicate_timestamps": int(n_dup),
        "n_gaps": int(np.sum(dt > max_gap_s)),
        "gap_time_s": float(np.sum(dt[dt > max_gap_s])),
        "jitter_std_ms": float(np.std(dt[dt <= max_gap_s]) * 1e3) if np.any(dt <= max_gap_s) else float("nan"),
        "duration_s": float(t[-1] - t[0]),
    }
    out.attrs.update(df.attrs)
    out.attrs["fs"] = fs_used
    return out, report
