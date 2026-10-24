"""Layer 2a: beat detection and fiducial points.

Why fiducials? Almost every cuffless-BP and vascular feature in the
literature is a time, height, width or area *between named landmarks* of
the pulse wave. Getting the landmarks right is most of the work.

Landmarks found per beat (sample indices; -1 = not found)::

    foot        pulse onset = minimum before the upstroke
    foot_tan    intersecting-tangent onset (time only; more robust to noise,
                standard for pulse-arrival/transit time)
    max_slope   steepest point of the upstroke (VPG maximum)
    peak        systolic peak
    notch       dicrotic notch (aortic valve closure) - local min, or the
                inflection point when no dip is visible
    dia         diastolic peak (reflected wave)
    next_foot   foot of the following beat (beat ends here)
    a,b,c,d,e   second-derivative (APG) waves, Takazawa 1998 / Elgendi 2012

     ac    peak
      |    /\\      dia
      |   /  \\_  _/\\
      |  /     \\/    \\___
      | /     notch       \\
      |/foot                \\next_foot
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.ndimage import uniform_filter1d
from scipy.signal import find_peaks

from .config import BeatDetectConfig
from .filters import contiguous_runs

FIDUCIALS = ["foot", "max_slope", "peak", "notch", "dia", "next_foot", "a", "b", "c", "d", "e"]


# --------------------------------------------------------------------------
# systolic peaks
# --------------------------------------------------------------------------
def elgendi_peaks(x: np.ndarray, fs: float, cfg: BeatDetectConfig) -> np.ndarray:
    """Systolic peaks via Elgendi et al. (2013), run per valid segment.

    Square the positive part of the band-passed PPG, compare a short moving
    average (systolic-peak width) to a long one (beat width) + offset; each
    block where the short one wins contains one systolic peak.
    """
    out = []
    w1 = max(1, int(round(cfg.w_peak_s * fs)))
    w2 = max(w1 + 1, int(round(cfg.w_beat_s * fs)))
    for s, e in contiguous_runs(np.isfinite(x)):
        seg = x[s:e]
        if seg.size < w2 * 2:
            continue
        y = np.clip(seg, 0, None) ** 2
        ma_peak = uniform_filter1d(y, w1, mode="nearest")
        ma_beat = uniform_filter1d(y, w2, mode="nearest")
        thr = ma_beat + cfg.beta * np.mean(y)
        for bs, be in contiguous_runs(ma_peak > thr):
            if be - bs >= w1:
                out.append(s + bs + int(np.argmax(seg[bs:be])))
    peaks = np.array(sorted(out), dtype=int)
    return _enforce_refractory(peaks, x, int(round(cfg.refractory_s * fs)))


def _enforce_refractory(peaks: np.ndarray, x: np.ndarray, min_dist: int) -> np.ndarray:
    if peaks.size < 2:
        return peaks
    keep = [peaks[0]]
    for p in peaks[1:]:
        if p - keep[-1] < min_dist:
            if x[p] > x[keep[-1]]:
                keep[-1] = p
        else:
            keep.append(p)
    return np.array(keep, dtype=int)


# --------------------------------------------------------------------------
# fiducials
# --------------------------------------------------------------------------
def _first(idx: np.ndarray, default=-1) -> int:
    return int(idx[0]) if idx.size else default


def find_fiducials(x: np.ndarray, vpg: np.ndarray, apg: np.ndarray, fs: float,
                   cfg: BeatDetectConfig, peaks: np.ndarray | None = None) -> pd.DataFrame:
    """Locate landmarks for every complete beat.

    x is the (perfusion-normalised, systole-up) PPG; vpg/apg its derivatives.
    """
    if peaks is None:
        peaks = elgendi_peaks(x, fs, cfg)
    n = x.size
    search = int(round(cfg.foot_search_s * fs))

    # 1) feet. A plain "minimum before the peak" wanders around in the flat
    #    end-diastolic trough (noise decides which sample is lowest), which
    #    makes IBI/HRV jittery and misaligns beats. So we anchor on the
    #    upstroke: find the max slope, draw its tangent down to the trough
    #    level (intersecting-tangent onset), and take the minimum only in a
    #    short window just before that point.
    feet = np.full(peaks.size, -1)
    foot_tan = np.full(peaks.size, np.nan)
    margin = max(1, int(round(0.05 * fs)))
    for i, p in enumerate(peaks):
        lo = p - search
        if i > 0:
            lo = max(lo, peaks[i - 1] + (p - peaks[i - 1]) // 2)
        lo = max(lo, 0)
        seg = x[lo:p + 1]
        if seg.size < 3 or not np.all(np.isfinite(seg)):
            continue
        ms = lo + int(np.argmax(vpg[lo:p + 1]))
        m0 = float(np.min(x[lo:ms + 1]))
        slope = vpg[ms]
        if slope > 0:
            tan_idx = ms - (x[ms] - m0) / slope * fs
            foot_tan[i] = tan_idx / fs
            start = int(np.clip(np.floor(tan_idx) - margin, lo, ms))
        else:
            start = lo
        feet[i] = start + int(np.argmin(x[start:ms + 1]))

    rows = []
    for i in range(peaks.size - 1):
        f, p, nf = feet[i], peaks[i], feet[i + 1]
        if f < 0 or nf < 0 or not (f < p < nf):
            continue
        seg = x[f:nf + 1]
        if not np.all(np.isfinite(seg)):
            continue  # beat crosses a masked region
        r = dict.fromkeys(FIDUCIALS, -1)
        r.update(foot=f, peak=p, next_foot=nf)

        # max slope on the upstroke + tangent foot
        ms = f + int(np.argmax(vpg[f:p + 1]))
        r["max_slope"] = ms
        r["foot_tan_s"] = foot_tan[i]

        # baseline-corrected beat (line between the two feet)
        base = np.interp(np.arange(f, nf + 1), [f, nf], [x[f], x[nf]])
        b = seg - base
        amp = b[p - f]

        # dicrotic notch: first clear dip after the peak (before the last 10% of the beat)
        # The notch sits in the first ~2/3 of the beat; searching later picks up
        # noise wiggles in the end-diastolic trough (seen when a late-systolic
        # shoulder fills in the real notch).
        end = f + int(cfg.notch_search_frac * (nf - f))
        dia_end = f + int(min(cfg.notch_search_frac + 0.15, 0.9) * (nf - f))
        post = b[p - f:end - f + 1]
        mins, _ = find_peaks(-post, prominence=cfg.notch_min_prominence * amp)
        if mins.size:
            notch = p + int(mins[0])
            r["notch"], r["notch_method"] = notch, 1
            after = b[notch - f:dia_end - f + 1]
            r["dia"] = notch + int(np.argmax(after))
        else:
            # no dip: use the inflection = first VPG local max after the peak
            vmax, _ = find_peaks(vpg[p:end + 1])
            if vmax.size:
                r["notch"] = r["dia"] = p + int(vmax[0])
                r["notch_method"] = 2
            else:
                r["notch_method"] = 0

        # APG a-e waves: a = APG max before max slope; then alternate min/max
        lo = max(f - int(0.05 * fs), 0)
        a = lo + int(np.argmax(apg[lo:ms + 1]))
        r["a"] = a
        lim = r["notch"] + int(0.1 * fs) if r["notch"] > 0 else f + int(0.6 * (nf - f))
        lim = min(lim, nf)
        seg_apg = apg[a:lim + 1]
        mx, _ = find_peaks(seg_apg)
        mn, _ = find_peaks(-seg_apg)
        bi = _first(mn)
        ci = _first(mx[mx > bi]) if bi >= 0 else -1
        di = _first(mn[mn > ci]) if ci >= 0 else -1
        ei = _first(mx[mx > di]) if di >= 0 else -1
        for k, v in zip("bcde", (bi, ci, di, ei)):
            r[k] = a + v if v >= 0 else -1

        rows.append(r)

    df = pd.DataFrame(rows, columns=FIDUCIALS + ["foot_tan_s", "notch_method"])
    if df.empty:
        return df
    for c in FIDUCIALS:
        df[c] = df[c].astype(int)
    df["notch_method"] = df["notch_method"].fillna(0).astype(int)
    return df
