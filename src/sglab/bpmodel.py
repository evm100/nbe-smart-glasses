"""Use a trained Layer-4 model run to estimate BP: live during a trial, or over a whole recorded
session ("would the model have tracked this session?") before it is used for training.

Features are computed exactly as for training (ppg.epochs.summarise_beats over a window of good
beats, pulse-arrival vs the finger when both are there). Calibrated runs (the default) need a
calibration: a cuff reading in the same session. The estimate is then

    BP(t) = cuff_cal + model(features(t) - features(cal window))

which is what the run was validated on. Non-calibrated runs predict directly.

    run = load_run(settings, name, run_id)           # cached by mtime
    session_prediction(settings, catalog, sid, run)  # sliding-window trace + every cuff reading
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from ppg.layer3 import pulse_arrival, reference_readings, window_rows

from . import model as mdl

LIVE_WINDOW_S = 45.0   # ~ a cuff window (15 s before inflation + ~30 s of inflation)
TRACE_STEP_S = 5.0

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def run_key(m: dict) -> str:
    return f"{m['name']}/{m['run_id']}"


def load_run(settings, name: str, run_id: str) -> dict | None:
    """{"models": {target: fitted}, "features": [...], "config": {...}, "meta": metrics.json} or None."""
    meta = mdl.get_run(settings, name, run_id)
    if meta is None:
        return None
    p = Path(meta["path"]) / "model.joblib"
    if not p.exists():
        return None
    key = str(p)
    mt = p.stat().st_mtime
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == mt:
            return hit[1]
    import joblib

    obj = joblib.load(p)
    run = {**obj, "meta": meta, "key": f"{name}/{run_id}"}
    with _cache_lock:
        _cache[key] = (mt, run)
    return run


def available_runs(settings, role: str | None = None) -> list[dict]:
    """Runs with a saved model, newest first: {key, name, run_id, role, calibrated, mae: {t: (model, base)}}."""
    out = []
    for m in reversed(mdl.list_runs(settings)):
        if role and m.get("role") != role:
            continue
        if not (Path(m["path"]) / "model.joblib").exists():
            continue
        out.append({"key": run_key(m), "name": m["name"], "run_id": m["run_id"], "role": m.get("role"),
                    "calibrated": bool(m.get("calibrated")), "trained_at": m.get("trained_at"),
                    "subjects": m.get("subjects", []), "n_dev": m.get("n_dev"),
                    "mae": {t: [_f(sp["cv"]["model"]["mae"]), _f(sp["cv"]["baseline"]["mae"])]
                            for t, sp in m.get("metrics", {}).items()}})
    return out


def default_run(settings, role: str) -> dict | None:
    """The run to show by default for a site: the one that beats its baseline by the most
    (mean over targets), else the newest."""
    runs = available_runs(settings, role)
    if not runs:
        return None

    def gain(r):
        v = [b - m for m, b in r["mae"].values() if m is not None and b is not None]
        return np.mean(v) if v else -np.inf
    return max(runs, key=gain)


def resolve(settings, key: str | None, role: str) -> dict | None:
    """'name/run_id' -> loaded run, or the site's default run when key is empty/'auto'."""
    if key and key not in ("auto", ""):
        name, _, rid = key.partition("/")
        return load_run(settings, name, rid)
    d = default_run(settings, role)
    return load_run(settings, d["name"], d["run_id"]) if d else None


def _f(v):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else float(v)


def _vec(feats: dict, names: list[str]) -> np.ndarray:
    return np.array([[float(feats.get(f, np.nan)) if feats.get(f) is not None else np.nan for f in names]], float)


def predict(run: dict, feats: dict, cal_feats: dict | None = None, cal_bp: dict | None = None) -> dict | None:
    """{target: mmHg} for one window, None if a calibrated run has no calibration yet."""
    names = run["features"]
    calibrated = bool(run["config"].get("calibrated"))
    X = _vec(feats, names)
    if calibrated:
        if cal_feats is None or cal_bp is None:
            return None
        X = X - _vec(cal_feats, names)
    out = {}
    for t, m in run["models"].items():
        if not np.isfinite(X).any():
            return None
        y = float(m.predict(X)[0])
        if calibrated:
            if cal_bp.get(t) is None:
                return None
            y += float(cal_bp[t])
        out[t] = round(y, 1)
    return out


def feature_coverage(run: dict, feats: dict) -> float:
    names = run["features"]
    v = _vec(feats, names)
    return float(np.isfinite(v).mean()) if names else 0.0


# ------------------------------------------------------------------ offline: a recorded session
def session_prediction(settings, catalog, sid: str, run: dict, step_s: float = TRACE_STEP_S,
                       window_s: float = LIVE_WINDOW_S) -> dict:
    """Run a model over a processed session.

    {"trace": {t, <target>...}, "readings": [{t, cuff..., pred..., is_cal}], "cal": {...},
     "seen": bool (the model trained on this subject), "stream": name, "notes": [...]}"""
    from .dataset import _stream_beats, layer3_config, subject_code
    from .timebase import read_events, session_clocks

    s = catalog.session(sid)
    if s is None:
        raise KeyError(f"unknown session {sid}")
    folder = settings.data_root / s["path"]
    doc, t0_unix = session_clocks(folder)
    cfg3 = layer3_config(settings)
    role = run["config"].get("role")
    targets = list(run["models"])
    runs = {r["stream"]: r for r in catalog.latest_runs(s["session_id"]) if r["status"] == "ok"}
    streams = [st for st in doc.get("streams", []) if st.get("stream") in runs]
    tgt = next((st for st in streams if st.get("role") == role), None)
    notes = []
    if tgt is None:
        have = sorted({st.get("role") for st in doc.get("streams", [])})
        raise ValueError(f"no processed {role} stream in {sid} (has: {', '.join(map(str, have)) or 'none'}); "
                         f"process the session first")
    beats, waves, t_foot, _ = _stream_beats(settings, runs[tgt["stream"]], tgt, t0_unix)
    if not len(beats):
        raise ValueError(f"no beats found in {tgt['stream']}")
    pad = None
    pa = cfg3.pulse_arrival
    ref = next((st for st in streams if st.get("role") == pa.ref_role and st is not tgt), None)
    if pa.enabled and ref is not None:
        rb, _, rt, _ = _stream_beats(settings, runs[ref["stream"]], ref, t0_unix)
        if len(rb):
            pad = pulse_arrival(rt, rb["beat_good"].to_numpy(), t_foot, beats["beat_good"].to_numpy(), pa)
    if "pad_ms_rel" in run["features"] and pad is None:
        notes.append("This model uses the pulse-arrival time vs the finger, but the session has no finger "
                     "stream: that feature is filled with the training median.")

    readings = reference_readings(read_events(folder), cfg3)
    bp = readings[readings["kind"] == "bp"].reset_index(drop=True)
    t_end = float(np.nanmax(t_foot))
    t_start = float(np.nanmin(t_foot))
    grid = np.arange(t_start + window_s, t_end + 1e-9, step_s)
    wins = pd.DataFrame({"t_win_start": grid - window_s, "t_win_end": grid})
    wrows, _ = window_rows(wins, beats, waves, t_foot, cfg3, pad)
    rrows, _ = window_rows(bp, beats, waves, t_foot, cfg3, pad) if len(bp) else (pd.DataFrame(), None)

    cal_feats = cal_bp = None
    cal_idx = None
    calibrated = bool(run["config"].get("calibrated"))
    if calibrated:
        for i in range(len(bp)):  # the first valid reading calibrates (as in training)
            if bool(rrows.iloc[i]["valid"]):
                cal_idx = i
                cal_feats = rrows.iloc[i].to_dict()
                cal_bp = {t: float(bp.iloc[i][t]) for t in targets if t in bp}
                break
        if cal_idx is None:
            notes.append("No cuff reading with enough good beats to calibrate on: a calibrated model "
                         "can't produce numbers for this session.")

    trace = {"t": [], **{t: [] for t in targets}, "n_good": []}
    for i, r in enumerate(wrows.to_dict("records")):
        p = predict(run, r, cal_feats, cal_bp) if r["valid"] else None
        trace["t"].append(round(float(grid[i]), 2))
        trace["n_good"].append(int(r["n_good"]))
        for t in targets:
            trace[t].append(p[t] if p else None)

    out_r = []
    for i in range(len(bp)):
        r = rrows.iloc[i].to_dict()
        p = predict(run, r, cal_feats, cal_bp) if r["valid"] else None
        row = {"t": round(float(bp.iloc[i]["t_ref"]), 1), "t_win_start": round(float(bp.iloc[i]["t_win_start"]), 1),
               "valid": bool(r["valid"]), "n_good": int(r["n_good"]), "is_cal": i == cal_idx}
        for t in targets:
            row[f"cuff_{t}"] = _f(bp.iloc[i].get(t))
            row[f"pred_{t}"] = p[t] if p and i != cal_idx else None
            row[f"err_{t}"] = (round(p[t] - row[f"cuff_{t}"], 1) if p and i != cal_idx and row[f"cuff_{t}"] is not None
                               else None)
        out_r.append(row)
    errs = {t: [r[f"err_{t}"] for r in out_r if r[f"err_{t}"] is not None] for t in targets}
    seen = subject_code(s.get("subject_id")) in set(run["meta"].get("subjects", []))
    if seen:
        notes.append(f"The model was trained on readings from {subject_code(s.get('subject_id'))}: errors here are "
                     f"optimistic. A fair test is a subject (or at least a session) it never saw.")
    return {"session_id": s["session_id"], "stream": tgt["stream"], "role": role, "model": run["key"],
            "calibrated": calibrated, "targets": targets, "trace": trace, "readings": out_r,
            "cal": {"t": out_r[cal_idx]["t"], **cal_bp} if cal_idx is not None else None,
            "mae": {t: round(float(np.mean(np.abs(v))), 1) if v else None for t, v in errs.items()},
            "seen": seen, "notes": notes, "window_s": window_s,
            "features": run["features"]}


def json_safe(v):
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def dumps(v) -> str:
    return json.dumps(json_safe(v))
