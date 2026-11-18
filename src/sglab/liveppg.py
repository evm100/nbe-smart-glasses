"""Live PPG features during a trial: the offline pipeline (Layers 1-2, same settings and site
overrides as `sglab process`) re-run every ~1.5 s on the last WIN_S seconds of each stream.

Per stream it keeps
  readout     HR, HRV (RMSSD), perfusion index, signal quality, beat markers for the live plot,
              the ensemble-average beat with its landmarks, a few morphology features
  beat log    every beat once ("committed" when it is clear of the window's filter edges), so
              any interval of the trial can be summarised exactly like a Layer-3 window -> live
              BP from a trained model (sglab.bpmodel), calibrated on the trial's cuff reading

Runs in one background thread per trial (LiveAnalysis), so beats are logged even when nobody
has the live page open.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque

import numpy as np
import pandas as pd

from ppg.epochs import feature_columns, hrv, summarise_beats
from ppg.layer2 import run_layer2
from ppg.layer3 import pulse_arrival, reference_readings
from ppg.pipeline import run_layer1

WIN_S = 16.0
STEP_S = 1.5
EDGE_START_S = 3.0   # beats this close to the window start: filter transient, not committed yet
EDGE_END_S = 1.0
MIN_SPAN_S = 8.0
TREND_S = 600.0
SHOW_FEATURES = ["t_rise_s", "pw50_s", "sys_frac", "notch_rel", "max_slope_norm", "ipa", "agi"]


def _f(v, nd=3):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


class LivePPG:
    def __init__(self, role: str, fs: float, channel: str, cfg1, cfg2):
        self.role, self.fs, self.channel = role, float(fs), channel
        self.cfg1 = copy.deepcopy(cfg1)
        self.cfg1.timing.fs_target = self.fs
        self.cfg2 = copy.deepcopy(cfg2)
        self.cfg2.gaussian.enabled = False  # slow curve fits; no live feature needs them
        self.rows: list[dict] = []          # committed beats (session-time t_foot etc.)
        self.waves: list[np.ndarray] = []
        self.last_commit = -math.inf
        self.readout: dict = {"state": "warming up"}
        self.trend: deque = deque()         # (t, hr, pi, sqi)
        self.lock = threading.Lock()
        self.error: str | None = None
        self.ms = None

    def step(self, samples: list[tuple]) -> None:
        t0p = time.perf_counter()
        if len(samples) < 10:
            return
        a = np.asarray(samples, dtype=float)
        t_end = a[-1, 0]
        a = a[a[:, 0] >= t_end - WIN_S]
        span = a[-1, 0] - a[0, 0]
        if span < MIN_SPAN_S:
            with self.lock:
                self.readout = {"state": "warming up", "progress": round(span / MIN_SPAN_S, 2)}
            return
        t0 = a[0, 0]
        df = pd.DataFrame({"t": a[:, 0] - t0, self.channel: a[:, 1]})
        df.attrs["channels"] = [self.channel]
        l1 = run_layer1(df, self.cfg1)
        l2 = run_layer2(l1, self.cfg2)
        b = l2.beats
        fs = l1.fs
        q = l1.quality
        win_good = float(q["good"].mean()) if len(q) and "good" in q else None
        if b.empty:
            with self.lock:
                self.readout = {"state": "no beats", "t": _f(t_end), "window_good": _f(win_good, 2)}
                self.ms = round((time.perf_counter() - t0p) * 1e3)
            return
        b = b.copy()
        for c in ("t_foot", "t_peak", "t_max_slope"):
            b[c] = b[c] + t0
        b["t_notch"] = np.where(b["notch_idx"] >= 0, b["notch_idx"] / fs + t0, np.nan)
        b["t_dia"] = np.where(b["dia_idx"] >= 0, b["dia_idx"] / fs + t0, np.nan)
        b["t_next_foot"] = b["next_foot_idx"] / fs + t0
        good = b["beat_good"].to_numpy(bool)

        # commit beats clear of the filter edges, once each
        ok = (b["t_foot"] >= t0 + EDGE_START_S) & (b["t_next_foot"] <= t_end - EDGE_END_S) & (b["t_foot"] > self.last_commit + 0.2)
        new = np.flatnonzero(ok.to_numpy())
        with self.lock:
            for i in new:
                self.rows.append(b.iloc[i].to_dict())
                self.waves.append(l2.waves[i])
            if new.size:
                self.last_commit = float(b["t_foot"].iloc[new[-1]])

        # readout from the current window
        recent = good & (b["t_foot"].to_numpy() >= t_end - 10.0)
        g = b[good]
        hr = 60.0 / float(b.loc[recent, "ibi_s"].mean()) if recent.sum() >= 2 else (
            60.0 / float(g["ibi_s"].mean()) if len(g) >= 2 else None)
        pi = float(np.median(g["amp"])) * 100 if len(g) else None
        sqi = float(good.mean())
        tc = float(np.nanmedian(b["template_corr"])) if "template_corr" in b else None
        with self.lock:
            hist = self._history_df(t_end - 60.0)
        rmssd = sdnn = None
        if len(hist) >= 4:
            gi = hist[hist["beat_good"].astype(bool)]
            if len(gi) >= 4:
                fi, nf = gi["foot_idx_abs"].to_numpy(), gi["next_foot_idx_abs"].to_numpy()
                cons = np.zeros(len(gi), bool)
                cons[:-1] = np.abs(fi[1:] - nf[:-1]) < 0.02
                h = hrv(gi["ibi_s"].to_numpy(), cons)
                rmssd, sdnn = _f(h["rmssd_ms"], 1), _f(h["sdnn_ms"], 1)
        tpl = fid = None
        if len(g):
            gw = l2.waves[good]
            tpl = [_f(v, 3) for v in np.nanmean(gw, axis=0)]
            span_i = (g["next_foot_idx"] - g["foot_idx"]).to_numpy(float)
            fid = {}
            for k in ("max_slope", "peak", "notch", "dia"):
                v = g[f"{k}_idx"].to_numpy(float)
                ok_k = v >= 0
                if ok_k.any():
                    fid[k] = _f(np.median((v[ok_k] - g["foot_idx"].to_numpy(float)[ok_k]) / span_i[ok_k]), 3)
        feats = {c: _f(np.nanmedian(g[c]), 4) if len(g) and c in g and g[c].notna().any() else None
                 for c in SHOW_FEATURES}
        sqi_label = "good" if sqi >= 0.7 else "fair" if sqi >= 0.4 else "poor"
        markers = [{"t": _f(r.t_peak, 3), "foot": _f(r.t_foot, 3), "notch": _f(r.t_notch, 3), "good": bool(r.beat_good)}
                   for r in b.itertuples()]
        with self.lock:
            self.trend.append((round(float(t_end), 2), _f(hr, 1), _f(pi, 3), _f(sqi, 2)))
            while self.trend and self.trend[0][0] < t_end - TREND_S:
                self.trend.popleft()
            self.readout = {
                "state": "ok", "t": _f(t_end), "hr_bpm": _f(hr, 1), "rmssd_ms": rmssd, "sdnn_ms": sdnn,
                "pi_pct": _f(pi, 3), "sqi": _f(sqi, 2), "sqi_label": sqi_label, "template_corr": _f(tc, 3),
                "window_good": _f(win_good, 2), "n_beats": int(len(b)), "n_good": int(good.sum()),
                "markers": markers, "template": tpl, "fiducials": fid, "features": feats,
                "beats_logged": len(self.rows),
            }
            self.ms = round((time.perf_counter() - t0p) * 1e3)

    def _history_df(self, t_from: float) -> pd.DataFrame:
        rows = [r for r in self.rows[-2000:] if r["t_foot"] >= t_from]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["foot_idx_abs"] = df["t_foot"]
        df["next_foot_idx_abs"] = df["t_next_foot"]
        return df

    def beats(self) -> tuple[pd.DataFrame, np.ndarray]:
        """Every committed beat (session time) and its normalised wave, for window summaries."""
        with self.lock:
            rows, waves = list(self.rows), list(self.waves)
        if not rows:
            return pd.DataFrame(), np.empty((0, 100))
        df = pd.DataFrame(rows)
        df["beat_good"] = df["beat_good"].astype(bool)
        # summarise_beats checks consecutiveness on foot/next-foot sample indices: use times
        # quantised to 20 ms so the same check works across analysis windows (their sample grids
        # differ by a fraction of a sample; beats are >= 300 ms apart)
        df["foot_idx"] = np.round(df["t_foot"] * 50).astype(int)
        df["next_foot_idx"] = np.round(df["t_next_foot"] * 50).astype(int)
        return df, np.vstack(waves)

    def state(self) -> dict:
        with self.lock:
            return {"role": self.role, "fs": self.fs, "channel": self.channel, **self.readout,
                    "trend": list(self.trend), "error": self.error, "ms": self.ms}


def window_features(beats: pd.DataFrame, waves: np.ndarray, t0: float, t1: float,
                    pad: tuple | None = None, pad_all: np.ndarray | None = None) -> dict:
    """Layer-3-style summary of the good beats with t_foot in [t0, t1) (+ pad_ms_rel)."""
    if beats.empty:
        return {"n_good": 0}
    sel = (beats["t_foot"].to_numpy() >= t0) & (beats["t_foot"].to_numpy() < t1)
    row, _ = summarise_beats(beats, waves, sel, feature_columns(beats))
    if pad is not None:
        _, pt, d = pad
        m = (pt >= t0) & (pt < t1)
        if m.any() and d.size:
            row["pad_ms_med"] = float(np.median(d[m]) * 1e3)
            row["pad_ms_rel"] = float((np.median(d[m]) - np.median(d)) * 1e3)
    return row


class LiveAnalysis(threading.Thread):
    """One per trial: steps every stream's LivePPG and keeps the live BP estimate."""

    def __init__(self, trial, settings):
        super().__init__(daemon=True, name=f"live-{trial.id}")
        self.trial, self.settings = trial, settings
        self.stop_flag = threading.Event()
        self.ppg: dict[str, LivePPG] = {}
        self.bp_lock = threading.Lock()
        self.bp_model: str = "auto"      # 'name/run_id' or auto (the site's best run)
        self.bp_role: str | None = None   # site to estimate on (None: glasses if present, else first)
        self.bp: dict = {"state": "waiting"}
        self.bp_trend: deque = deque()
        self._cfg = None

    def configs(self):
        if self._cfg is None:
            from .processing import pipeline_configs, site_overrides
            self._cfg = (*pipeline_configs(self.settings), site_overrides(self.settings))
        return self._cfg

    def run(self):
        while not self.stop_flag.wait(STEP_S):
            rec = self.trial.rec
            if rec is None or self.trial.state != "recording":
                continue
            for w in list(rec.workers):
                h = w.header
                if h is None or not h.fs:
                    continue
                key = w.role
                lp = self.ppg.get(key)
                if lp is None:
                    from .processing import stream_configs
                    b1, b2, sites = self.configs()
                    c1, c2 = stream_configs(b1, b2, sites, {"role": w.role})
                    lp = self.ppg[key] = LivePPG(w.role, h.fs, h.channels[0], c1, c2)
                if lp.error:
                    continue
                try:
                    lp.step(list(w.live))
                except Exception as e:  # shown on the page; analysis stops for this stream only
                    lp.error = f"{type(e).__name__}: {e}"
            try:
                self._bp_step(rec)
            except Exception as e:
                with self.bp_lock:
                    self.bp = {"state": "error", "error": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------ live BP
    def _bp_step(self, rec) -> None:
        from . import bpmodel
        from .dataset import layer3_config

        roles = list(self.ppg)
        if not roles:
            return
        role = self.bp_role if self.bp_role in roles else ("glasses" if "glasses" in roles else roles[0])
        run = bpmodel.resolve(self.settings, self.bp_model, role)
        base = {"role": role, "roles": roles, "model": run["key"] if run else None,
                "calibrated": bool(run["config"].get("calibrated")) if run else None}
        if run is None:
            with self.bp_lock:
                self.bp = {**base, "state": "no_model",
                           "message": f"No trained model for the {role} site yet (Models → Train)."}
            return
        if run["config"].get("role") != role:
            base["note"] = f"model trained on {run['config'].get('role')} data, applied to {role}"
        lp = self.ppg[role]
        beats, waves = lp.beats()
        now = rec.now()
        if beats.empty:
            with self.bp_lock:
                self.bp = {**base, "state": "warming_up", "message": "Collecting beats…"}
            return
        pad = None
        cfg3 = layer3_config(self.settings)
        ref_role = cfg3.pulse_arrival.ref_role
        if ref_role in self.ppg and ref_role != role:
            rb, _ = self.ppg[ref_role].beats()
            if len(rb):
                pad = pulse_arrival(rb["t_foot"].to_numpy(), rb["beat_good"].to_numpy(),
                                    beats["t_foot"].to_numpy(), beats["beat_good"].to_numpy(), cfg3.pulse_arrival)
                if not np.isfinite(pad[0]) or not pad[2].size:
                    pad = None
        feats = window_features(beats, waves, now - bpmodel.LIVE_WINDOW_S, now, pad)
        cal = None
        events = [e for e in rec.all_events() if e.get("kind") in ("bp", "cuff_start")]
        readings = reference_readings(events, cfg3)
        bp_rows = readings[readings["kind"] == "bp"] if len(readings) else readings
        calibrated = bool(run["config"].get("calibrated"))
        if calibrated and len(bp_rows):
            # the latest cuff reading with enough good beats calibrates
            for r in bp_rows.iloc[::-1].itertuples():
                cf = window_features(beats, waves, r.t_win_start, r.t_win_end, pad)
                if cf.get("n_good", 0) >= cfg3.min_good_beats:
                    cal = {"t": round(float(r.t_ref), 1), "sys_mmhg": float(r.sys_mmhg), "dia_mmhg": float(r.dia_mmhg),
                           "map_mmhg": float(r.map_mmhg), "feats": cf, "n_good": cf["n_good"]}
                    break
            if cal is None:
                with self.bp_lock:
                    self.bp = {**base, "state": "cal_pending",
                               "message": "The cuff reading's window doesn't have enough good beats yet."}
                return
        if calibrated and cal is None:
            with self.bp_lock:
                self.bp = {**base, "state": "needs_cal",
                           "message": "Log a cuff reading to calibrate (Cuff inflation started → Log BP)."}
            return
        n_good = feats.get("n_good", 0)
        if n_good < max(5, cfg3.min_good_beats // 2):
            with self.bp_lock:
                self.bp = {**base, "state": "low_quality", "cal": _cal_brief(cal), "n_good": n_good,
                           "message": f"Only {n_good} good beats in the last {bpmodel.LIVE_WINDOW_S:.0f} s."}
            return
        p = bpmodel.predict(run, feats, cal["feats"] if cal else None,
                            {k: cal[k] for k in ("sys_mmhg", "dia_mmhg", "map_mmhg")} if cal else None)
        if p is None:
            with self.bp_lock:
                self.bp = {**base, "state": "no_features", "cal": _cal_brief(cal),
                           "message": "The model's features couldn't be computed from this signal."}
            return
        with self.bp_lock:
            self.bp_trend.append({"t": round(now, 1), **p})
            while self.bp_trend and self.bp_trend[0]["t"] < now - TREND_S * 3:
                self.bp_trend.popleft()
            self.bp = {**base, "state": "ok", "t": round(now, 1), **p, "cal": _cal_brief(cal), "n_good": n_good,
                       "coverage": round(bpmodel.feature_coverage(run, feats), 2),
                       "window_s": bpmodel.LIVE_WINDOW_S}

    def bp_state(self) -> dict:
        with self.bp_lock:
            return {**self.bp, "trend": list(self.bp_trend), "selected": self.bp_model}

    def set_model(self, key: str | None, role: str | None = None) -> None:
        with self.bp_lock:
            self.bp_model = key or "auto"
            if role:
                self.bp_role = role
            self.bp_trend.clear()
            self.bp = {"state": "waiting"}


def _cal_brief(cal: dict | None) -> dict | None:
    if not cal:
        return None
    return {"t": cal["t"], "sys_mmhg": cal["sys_mmhg"], "dia_mmhg": cal["dia_mmhg"], "n_good": cal["n_good"]}
