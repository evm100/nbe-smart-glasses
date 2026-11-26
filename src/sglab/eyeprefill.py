"""Pre-fill for the eye-labeling tool: a first guess of how open the eye is in every frame.

The guesses only speed labeling up (accept long open stretches at once, jump to likely blinks);
they are never used as ground truth. The method needs no training, so it works on the first session
ever recorded, and it is the baseline the trained detector has to beat.

Per frame, inside the eye box (or the whole frame):
  1. unusable frames -> level 0: washed out (mean > 240) or no contrast (std < 4)
  2. block-average to ~24 x 24 px, then PCA over the session's usable frames ("eigen-eyes")
  3. of the first three components, take the one with the most one-sided excursions (|skewness|):
     blinks are rare, short dips away from the usual open-eye picture. Its sign is set so blinks are negative.
  4. remove slow drift (glasses slipping, lighting) with a ~5 s running median, so only fast changes remain
  5. scale: the eye's usual state (and anything within 3 noise widths, MAD, of it) = open; the deepest
     dips (the 0.5th percentile, but at least 8 noise widths, so a session without blinks doesn't turn
     noise into "closed") = closed
  6. openness in 0..1 in between; level = 1 + round(4 * openness)
Candidates (where a labeler should look closely) are runs with openness < 0.75, widened by 4 frames.

Untested on real IR frames so far: tuned on synthetic data only. Treat it as a starting point.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

VERSION = 1
WORK_PX = 24
MAX_PCA_FRAMES = 4000
CHUNK = 2048


def _small(frames, i0: int, i1: int, crop: dict | None, f: int, ch: int, cw: int) -> np.ndarray:
    a = np.asarray(frames[i0:i1])
    if crop:
        a = a[:, crop["y"]:crop["y"] + crop["h"], crop["x"]:crop["x"] + crop["w"]]
    a = a[:, :ch * f, :cw * f].astype(np.float32)
    return a.reshape(len(a), ch, f, cw, f).mean(axis=(2, 4))


def compute(frames, t: np.ndarray, crop: dict | None = None) -> dict:
    """frames: (n, h, w) uint8 (a memmap is fine, it is read in chunks); t: frame times (s)."""
    n = len(frames)
    h, w = (crop["h"], crop["w"]) if crop else frames.shape[1:3]
    f = max(1, min(h, w) // WORK_PX)
    ch, cw = h // f, w // f
    small = np.empty((n, ch * cw), np.float32)
    for i in range(0, n, CHUNK):
        s = _small(frames, i, min(n, i + CHUNK), crop, f, ch, cw)
        small[i:i + len(s)] = s.reshape(len(s), -1)
    mean, std = small.mean(axis=1), small.std(axis=1)
    usable = (mean <= 240) & (std >= 4)
    level = np.zeros(n, np.int8)
    open_ = np.full(n, np.nan, np.float32)
    out = {"version": VERSION, "n": n, "level": level, "open": open_, "candidates": [], "component": None}
    idx = np.flatnonzero(usable)
    if len(idx) < 20:
        return out

    X = small[idx]
    mu = X.mean(axis=0)
    sub = idx if len(idx) <= MAX_PCA_FRAMES else idx[np.linspace(0, len(idx) - 1, MAX_PCA_FRAMES).astype(int)]
    _, _, vt = np.linalg.svd(small[sub] - mu, full_matrices=False)
    proj = (X - mu) @ vt[:3].T                               # (m, 3)

    dt = np.diff(t[idx]) if len(idx) > 1 else np.array([0.04])
    fps = 1.0 / float(np.median(dt[dt > 0])) if np.any(dt > 0) else 25.0
    win = max(3, int(round(5.0 * fps)) | 1)

    best = None
    for k in range(proj.shape[1]):
        d = proj[:, k] - ndimage.median_filter(proj[:, k], size=win, mode="nearest")
        mad = float(np.median(np.abs(d - np.median(d)))) * 1.4826 or 1e-6
        z = d / mad
        skew = float(np.mean(z ** 3)) / max(float(np.mean(z ** 2)) ** 1.5, 1e-9)
        tail = float(np.percentile(d, 0.5 if skew < 0 else 99.5))
        # the lid: lopsided (rare dips) and the biggest change in the picture; else the most lopsided
        score = (abs(skew) > 1.5, abs(tail), abs(skew))
        if best is None or score > best[0]:
            best = (score, skew, k, d, mad)
    _, skew, k, d, mad = best
    d = d if skew < 0 else -d                                # blinks = negative excursions
    depth = min(float(np.percentile(d, 0.5)), -8.0 * mad)
    dead = 3.0 * mad                                        # within 3 noise widths of usual = open
    op = np.clip(1.0 - np.maximum(0.0, -d - dead) / (-depth - dead), 0.0, 1.0)
    open_[idx] = op
    level[idx] = (1 + np.rint(4 * op)).astype(np.int8)

    low = np.zeros(n, bool)
    low[idx[op < 0.75]] = True
    low = ndimage.binary_dilation(low, iterations=4)
    lab, nr = ndimage.label(low)
    out["candidates"] = [[int(sl[0].start), int(sl[0].stop - 1)] for sl in ndimage.find_objects(lab)]
    out["component"] = {"index": int(k), "skew": round(float(skew), 2), "fps": round(fps, 2)}
    return out
