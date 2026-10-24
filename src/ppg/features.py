"""Layer 2b: per-beat morphology features.

All amplitude features are computed on the perfusion-normalised signal
(AC/DC) after subtracting the straight line joining the beat's two feet, so
they are comparable across subjects, LED currents and sensor positions.
Most are also expressed as *ratios* (to amplitude or to beat length),
because absolute values depend heavily on contact pressure and site.

Feature                What it is                            Why it may track BP / vascular state
---------------------  ------------------------------------  ------------------------------------------
ibi_s, hr_bpm          foot-to-foot interval                 HR; BP co-varies with sympathetic tone
t_rise_s               foot -> systolic peak ("crest time")  arterial stiffness; shorter with stiffer arteries
t_rise_tan_s           tangent-foot -> peak                  same, less noise-sensitive
t_max_slope_s          foot -> steepest upstroke             ventricular ejection dynamics
t_sys_s, t_dia_s       foot->notch, notch->next foot          systolic ejection vs diastolic run-off
sys_frac               t_sys / ibi                           duty cycle (changes with HR & afterload)
delta_t_s              systolic peak -> diastolic peak       reflected-wave timing (stiffness index basis)
pw25/50/75_s           pulse width at 25/50/75 % height      Teng & Zhang 2003: DW strongly correlated w/ BP
sw*/dw*_s              systolic / diastolic part of widths
amp                    peak height (AC/DC) = beat PI         perfusion / vasoconstriction
max_slope_norm         VPG max / amp  [1/s]                  normalised upstroke speed
notch_rel, dia_rel     notch / diastolic-peak height / amp   reflection index (RI) - vascular tone
area_sys, area_dia     areas split at the notch (norm.)      stroke volume / peripheral resistance proxies
ipa                    area_dia / area_sys                   inflection-point area ratio (Wang 2009, TPR)
shape_factor           total area / (amp * ibi)              overall fullness of the pulse
b_a, c_a, d_a, e_a     APG wave ratios                       Takazawa 1998: vascular ageing, stiffness
agi                    (b - c - d - e) / a                   APG ageing index
<ch>_amp, R_red_ir     other channels' amplitude & ratio     SpO2 calibration input (ratio of ratios)

Gaussian decomposition features (g1..g3_*) are computed separately in gaussian.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_trapz = getattr(np, "trapezoid", None) or np.trapz  # numpy 2.x / 1.x


def _vertex(y: np.ndarray, i: int) -> float:
    """Sub-sample position of the extremum at sample i (parabolic fit through i-1, i, i+1).

    At 100 Hz one sample is 10 ms, which is coarse for timing features
    (crest time, IBI, later pulse-transit time), so we interpolate.
    """
    if 0 < i < y.size - 1 and np.isfinite(y[i - 1:i + 2]).all():
        a, b, c = y[i - 1], y[i], y[i + 1]
        den = a - 2 * b + c
        if den != 0:
            off = 0.5 * (a - c) / den
            if abs(off) <= 1:
                return i + off
    return float(i)


def _crossings(b: np.ndarray, p: int, level: float) -> tuple[float, float]:
    """Fractional sample positions where b crosses `level` before and after p."""
    left = np.flatnonzero(b[:p + 1] < level)
    right = np.flatnonzero(b[p:] < level)
    if not left.size or not right.size:
        return np.nan, np.nan
    i = left[-1]  # b[i] < level <= b[i+1]
    lx = i + (level - b[i]) / (b[i + 1] - b[i])
    j = p + right[0]  # b[j-1] >= level > b[j]
    rx = (j - 1) + (b[j - 1] - level) / (b[j - 1] - b[j])
    return lx, rx


def beat_features(fid: pd.DataFrame, signals: pd.DataFrame, fs: float, channel: str,
                  other_channels: list[str] | None = None) -> pd.DataFrame:
    x = signals[f"{channel}_ac_norm"].to_numpy()
    vpg = signals[f"{channel}_vpg"].to_numpy()
    apg = signals[f"{channel}_apg"].to_numpy()
    dc = signals[f"{channel}_dc"].to_numpy()
    t = signals["t"].to_numpy()
    others = {ch: signals[f"{ch}_ac_norm"].to_numpy() for ch in (other_channels or [])}

    rows = []
    for _, r in fid.iterrows():
        f, p, nf, ms = int(r.foot), int(r.peak), int(r.next_foot), int(r.max_slope)
        notch, dia = int(r.notch), int(r.dia)
        seg = x[f:nf + 1]
        base = np.interp(np.arange(f, nf + 1), [f, nf], [x[f], x[nf]])
        b = seg - base
        amp = b[p - f]
        # sub-sample landmark times (t[0] == 0, uniform grid)
        tf, tnf = _vertex(x, f) / fs, _vertex(x, nf) / fs
        tp, tms = _vertex(x, p) / fs, _vertex(vpg, ms) / fs
        ibi = tnf - tf
        o = {
            "t_foot": tf, "t_peak": tp, "t_max_slope": tms,
            "ibi_s": ibi, "hr_bpm": 60.0 / ibi,
            "t_rise_s": tp - tf,
            "t_rise_tan_s": tp - r.foot_tan_s if np.isfinite(r.foot_tan_s) else np.nan,
            "t_max_slope_s": tms - tf,
            "amp": amp,
            "dc": dc[f],
            "max_slope_norm": vpg[ms] / amp if amp > 0 else np.nan,
        }

        for lvl in (25, 50, 75):
            lx, rx = _crossings(b, p - f, amp * lvl / 100)
            o[f"pw{lvl}_s"] = (rx - lx) / fs
            o[f"sw{lvl}_s"] = (p - f - lx) / fs
            o[f"dw{lvl}_s"] = (rx - (p - f)) / fs

        if notch > 0:
            k = notch - f
            o["t_sys_s"] = k / fs
            o["t_dia_s"] = (nf - notch) / fs
            o["sys_frac"] = k / (nf - f)
            o["notch_rel"] = b[k] / amp
            o["dia_rel"] = b[dia - f] / amp
            o["delta_t_s"] = _vertex(x, dia) / fs - tp if r.notch_method == 1 else np.nan
            a_sys = _trapz(b[:k + 1]) / fs
            a_dia = _trapz(b[k:]) / fs
            o["area_sys"] = a_sys / amp
            o["area_dia"] = a_dia / amp
            o["ipa"] = a_dia / a_sys if a_sys > 0 else np.nan
        o["shape_factor"] = _trapz(b) / (amp * (nf - f)) if amp > 0 else np.nan

        a_val = apg[int(r.a)] if r.a >= 0 else np.nan
        for k in "bcde":
            idx = int(r[k])
            o[f"{k}_a"] = apg[idx] / a_val if idx >= 0 and a_val > 0 else np.nan
        o["agi"] = o["b_a"] - o["c_a"] - o["d_a"] - o["e_a"]

        for ch, y in others.items():
            ys = y[f:nf + 1] - np.interp(np.arange(f, nf + 1), [f, nf], [y[f], y[nf]])
            o[f"{ch}_amp"] = ys[p - f]  # same fiducials -> same beat
        rows.append(o)

    feats = pd.DataFrame(rows)
    if "red" in (other_channels or []) and channel == "ir" and not feats.empty:
        feats["R_red_ir"] = feats["red_amp"] / feats["amp"]
    return feats
