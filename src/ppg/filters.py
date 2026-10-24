"""NaN-aware, zero-phase filtering primitives.

Design choices:
* Butterworth in second-order sections + ``sosfiltfilt`` -> zero phase delay,
  so peak/foot timing (needed later for pulse-arrival / BP features) is not
  shifted.
* Signals containing NaN (gaps, saturation) are filtered one contiguous
  valid segment at a time; segments that are too short are dropped.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
from scipy.signal import butter, savgol_filter, sosfiltfilt


def contiguous_runs(valid: np.ndarray) -> list[tuple[int, int]]:
    """[(start, stop), ...] of True runs (stop exclusive)."""
    v = np.asarray(valid, dtype=np.int8)
    d = np.diff(np.concatenate(([0], v, [0])))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def apply_segmentwise(x: np.ndarray, func: Callable[[np.ndarray], np.ndarray], min_len: int) -> np.ndarray:
    out = np.full(x.shape, np.nan, dtype=float)
    for s, e in contiguous_runs(np.isfinite(x)):
        if e - s >= max(min_len, 2):
            out[s:e] = func(x[s:e])
    return out


def design_sos(fs: float, low: float | None = None, high: float | None = None, order: int = 4) -> np.ndarray:
    nyq = fs / 2.0
    if high is not None and high >= nyq:
        high = 0.45 * fs  # clamp rather than fail on low-rate recordings
    if low and high:
        return butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    if low:
        return butter(order, low, btype="highpass", fs=fs, output="sos")
    if high:
        return butter(order, high, btype="lowpass", fs=fs, output="sos")
    raise ValueError("Specify low and/or high cutoff.")


def zero_phase(x: np.ndarray, sos: np.ndarray, fs: float, pad_s: float = 2.0, min_segment_s: float = 4.0) -> np.ndarray:
    pad = int(round(pad_s * fs))

    def f(seg):
        return sosfiltfilt(sos, seg, padtype="odd", padlen=min(pad, seg.size - 1))

    return apply_segmentwise(np.asarray(x, dtype=float), f, int(round(min_segment_s * fs)))


def bandpass(x, fs, low, high, order=4, **kw):
    return zero_phase(x, design_sos(fs, low, high, order), fs, **kw)


def lowpass(x, fs, cutoff, order=4, **kw):
    return zero_phase(x, design_sos(fs, None, cutoff, order), fs, **kw)


def savgol_derivative(x: np.ndarray, fs: float, deriv: int, window_s: float = 0.09, polyorder: int = 3) -> np.ndarray:
    """Smoothed n-th derivative (units per second^n)."""
    w = int(round(window_s * fs))
    w_min = polyorder + 1 + (polyorder % 2 == 0)  # smallest odd window > polyorder
    w = max(w, w_min)
    if w % 2 == 0:
        w += 1

    def f(seg):
        if seg.size < w:
            return np.full(seg.size, np.nan)
        return savgol_filter(seg, w, polyorder, deriv=deriv, delta=1.0 / fs)

    return apply_segmentwise(np.asarray(x, dtype=float), f, w)


def dilate_mask(mask: np.ndarray, n: int) -> np.ndarray:
    """Grow True regions by n samples on each side."""
    if n <= 0 or not mask.any():
        return mask.copy()
    k = np.ones(2 * n + 1)
    return np.convolve(mask.astype(float), k, mode="same") > 0


def flatline_mask(x: np.ndarray, n: int) -> np.ndarray:
    """True where the signal is exactly constant for >= n consecutive samples."""
    same = np.concatenate(([False], np.diff(x) == 0))
    mask = np.zeros(x.size, dtype=bool)
    for s, e in contiguous_runs(same):
        # run of equal diffs spans samples s-1 .. e-1
        if e - s + 1 >= n:
            mask[max(s - 1, 0):e] = True
    return mask
