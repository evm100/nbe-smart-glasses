"""Layer 3: reference readings (cuff BP, pulse-ox) + Layer-2 beats -> labelled rows.

Inputs, all on one clock (session time t_s, see sglab.timebase):
  events   the session's events.csv: `bp` results, `cuff_start` presses, `spo2` readings
  beats    each stream's Layer-2 beats, foot times mapped to session time

Output: one row per (reading, stream). The PPG side is a summary of the stream's good beats
inside the reading's window (the same medians/IQRs, HR and HRV as Layer-2 epochs, plus the
ensemble-average beat), so a row is one ML sample with its label.

Why windows per reading instead of joining the 30 s epochs: a cuff reading covers its own
interval (inflation -> result, ~30-90 s), not a fixed grid. Summarising exactly that interval
uses every beat the cuff "saw" and no beats from before a fast BP change (e.g. exercise).

Pulse-arrival difference (pad_*): every good beat of a stream is matched to the same
heartbeat at the reference site (finger), and the foot-time difference is kept. Its change
within a session follows pulse-transit time, which shortens as BP rises (Mukkamala et al.
2015). The absolute value also holds the SNTP offset between the two boards (a few ms), so
use pad_ms_rel (window minus session median) when comparing across sessions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Layer3Config, PulseArrivalConfig
from .epochs import feature_columns, summarise_beats
from .tuning import match_beats, pulse_offset

READING_COLS = ["reading", "kind", "t_ref", "t_cuff_start", "cuff_found", "t_win_start", "t_win_end",
                "sys_mmhg", "dia_mmhg", "map_mmhg", "ref_hr_bpm", "spo2_pct"]


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def reference_readings(events: pd.DataFrame | list[dict], cfg: Layer3Config | None = None) -> pd.DataFrame:
    """One row per usable reference reading, with the PPG window it applies to."""
    cfg = cfg or Layer3Config()
    ev = pd.DataFrame(events)
    if ev.empty or "kind" not in ev:
        return pd.DataFrame(columns=READING_COLS)
    ev = ev.assign(t_s=ev["t_s"].map(_num)).dropna(subset=["t_s"]).sort_values("t_s", kind="stable")
    for c in ("sys_mmhg", "dia_mmhg", "hr_bpm", "spo2_pct"):
        ev[c] = ev[c].map(_num) if c in ev else np.nan
    starts = ev.loc[ev["kind"] == "cuff_start", "t_s"].to_numpy()
    rows, prev_bp = [], -np.inf
    cc, oc = cfg.cuff, cfg.spo2
    for e in ev.itertuples():
        if e.kind == "bp" and np.isfinite(e.sys_mmhg) and np.isfinite(e.dia_mmhg):
            # last press before the result, after the previous result, within max_cuff_s
            cand = starts[(starts <= e.t_s) & (starts > prev_bp) & (starts >= e.t_s - cc.max_cuff_s)]
            found = cand.size > 0
            t_cuff = float(cand[-1]) if found else e.t_s - cc.default_cuff_s
            rows.append({"kind": "bp", "t_ref": e.t_s, "t_cuff_start": t_cuff, "cuff_found": found,
                         "t_win_start": t_cuff - cc.pre_s, "t_win_end": e.t_s + cc.post_s,
                         "sys_mmhg": e.sys_mmhg, "dia_mmhg": e.dia_mmhg,
                         "map_mmhg": e.dia_mmhg + (e.sys_mmhg - e.dia_mmhg) / 3, "ref_hr_bpm": e.hr_bpm,
                         "spo2_pct": np.nan})
            prev_bp = e.t_s
        elif e.kind == "spo2" and np.isfinite(e.spo2_pct):
            rows.append({"kind": "spo2", "t_ref": e.t_s, "t_cuff_start": np.nan, "cuff_found": False,
                         "t_win_start": e.t_s - oc.pre_s, "t_win_end": e.t_s + oc.post_s,
                         "sys_mmhg": np.nan, "dia_mmhg": np.nan, "map_mmhg": np.nan, "ref_hr_bpm": e.hr_bpm,
                         "spo2_pct": e.spo2_pct})
    out = pd.DataFrame(rows, columns=READING_COLS[1:])
    out.insert(0, "reading", np.arange(len(out)))
    return out


def pulse_arrival(ref_t: np.ndarray, ref_good: np.ndarray, tgt_t: np.ndarray, tgt_good: np.ndarray,
                  cfg: PulseArrivalConfig | None = None) -> tuple[float, np.ndarray, np.ndarray]:
    """(session offset s, t of each matched reference beat, target - reference foot time s).

    Good beats only, both on the session clock and sorted. The offset is the most common
    difference within +-max_lag_s; beats match when within tol_s of reference + offset."""
    cfg = cfg or PulseArrivalConfig()
    rt = np.asarray(ref_t, float)[np.asarray(ref_good, bool)]
    tt = np.asarray(tgt_t, float)[np.asarray(tgt_good, bool)]
    offset = pulse_offset(rt, tt, cfg.max_lag_s)
    if not np.isfinite(offset):
        return offset, np.array([]), np.array([])
    ri, ti = match_beats(rt, tt - offset, cfg.tol_s)
    return offset, rt[ri], tt[ti] - rt[ri]


def window_rows(readings: pd.DataFrame, beats: pd.DataFrame, waves: np.ndarray, t_foot: np.ndarray,
                cfg: Layer3Config | None = None, pad: tuple[float, np.ndarray, np.ndarray] | None = None
                ) -> tuple[pd.DataFrame, np.ndarray]:
    """PPG summary of one stream for each reading's window.

    t_foot: the beats' foot times on the session clock. pad: pulse_arrival() result against
    the reference site, or None. Returns (rows aligned with readings, templates n x len)."""
    cfg = cfg or Layer3Config()
    n_tpl = waves.shape[1] if waves.ndim == 2 and waves.shape[1] else 100
    rows, templates = [], []
    feat_cols = feature_columns(beats) if len(beats) else []
    for r in readings.itertuples():
        if len(beats):
            sel = (t_foot >= r.t_win_start) & (t_foot < r.t_win_end)
            row, tpl = summarise_beats(beats, waves, sel, feat_cols)
        else:
            row, tpl = {"n_beats": 0, "n_good": 0, "good_frac": 0.0}, np.full(n_tpl, np.nan)
        row["valid"] = row["n_good"] >= cfg.min_good_beats
        if pad is not None:
            offset, pt, d = pad
            m = (pt >= r.t_win_start) & (pt < r.t_win_end)
            dm = d[m]
            row["pad_n"] = int(dm.size)
            row["pad_ms_med"] = float(np.median(dm) * 1e3) if dm.size else np.nan
            row["pad_ms_iqr"] = float(np.subtract(*np.percentile(dm, [75, 25])) * 1e3) if dm.size >= 4 else np.nan
            row["pad_ms_rel"] = float((np.median(dm) - np.median(d)) * 1e3) if dm.size else np.nan
        rows.append(row)
        templates.append(tpl)
    tp = np.array(templates) if templates else np.empty((0, n_tpl))
    return pd.DataFrame(rows, index=readings.index), tp
