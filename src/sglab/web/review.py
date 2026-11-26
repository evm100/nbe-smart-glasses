"""Session review API: everything the session page plots, at the resolution the viewer asks for.

Every time series takes ``t0`` / ``t1`` (session seconds) and ``max_points``: the page asks for the
whole recording first (min/max decimated so peaks survive) and again for the visible range on
every zoom, so a 90-minute recording still shows single pulses when you zoom in.

    GET /api/sessions/{sid}/overview                what the session contains (streams, cameras, other sensors)
    GET /api/sessions/{sid}/signal                  PPG: raw counts or filtered, + beats when zoomed in
    GET /api/sessions/{sid}/quality                 why Layer 1 / Layer 2 accepted or rejected what
    GET /api/sessions/{sid}/eye                     eye tracking: status, summary, per-minute table, events
    GET /api/sessions/{sid}/eye/trace               per-frame closure / gaze / motion in a range
    GET /api/sessions/{sid}/video/{stream}/frame    one recorded frame as PNG (+ the tracker's overlay)
    GET /api/sessions/{sid}/aux                     a further sensor's channels (sglab.auxstream)
    GET /api/sessions/{sid}/cues                    the cued-session log (sglab.cues), optionally with frame numbers
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .. import eyejob, sensors
from ..timebase import device_clock, read_events, run_clock, session_clocks, session_t0_unix
from .security import require

router = APIRouter()
viewer = Depends(require("viewer"))

BEATS_MAX_SPAN_S = 180.0   # beat markers are sent when the visible range is at most this long

# plain-language names of the Layer-1 window checks (ppg.quality._label) and Layer-2 beat checks
REASONS = {
    "gaps": "missing samples (gaps, saturation, sensor off the skin)",
    "aperiodic": "no clear repeating pulse (autocorrelation peak too low)",
    "hr_disagree": "two heart-rate estimates disagree (spectrum vs. autocorrelation)",
    "skew": "pulse shape not skewed enough (skewness below the limit)",
    "amplitude": "amplitude far from the session's usual (movement, or signal lost)",
    "spectrum": "energy spread outside the pulse frequency (noise)",
    "ibi": "beat much shorter / longer than its neighbours",
    "rhythm": "beat much shorter / longer than its neighbours",
    "physio": "timing or shape outside what a real pulse can be",
    "template": "beat shape unlike the average beat",
    "layer1": "inside a rejected window",
    "window": "inside a window the window check rejected",
    "no_notch": "no dicrotic notch found",
}


def C(request: Request):
    return request.app.state.ctx


# ------------------------------------------------------------------ small array cache
class _Cache:
    """Parsed files by (path, mtime): zooming re-reads the same CSV many times."""

    def __init__(self, n: int = 8):
        self.n, self.d, self.lock = n, OrderedDict(), threading.Lock()

    def get(self, path: Path, key: str, load):
        k = (str(path), key, path.stat().st_mtime_ns)
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                return self.d[k]
        v = load()
        with self.lock:
            self.d[k] = v
            while len(self.d) > self.n:
                self.d.popitem(last=False)
        return v


_cache = _Cache()


def minmax_decimate(t: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Keep each bucket's min and max so peaks survive downsampling for the browser."""
    n = t.size
    if n <= max_points:
        return t, y
    nb = max(1, max_points // 2)
    edges = np.linspace(0, n, nb + 1).astype(int)
    ti, yi = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = y[a:b]
        if not seg.size:
            continue
        if not np.isfinite(seg).any():
            ti.append(t[a]); yi.append(np.nan)
            continue
        i0, i1 = a + int(np.nanargmin(seg)), a + int(np.nanargmax(seg))
        for i in sorted({i0, i1}):
            ti.append(t[i]); yi.append(y[i])
    return np.array(ti), np.array(yi)


def clean(v, nd: int = 6) -> list:
    """JSON-safe list (NaN -> None)."""
    a = np.asarray(v, dtype=float)
    return [None if not np.isfinite(x) else round(float(x), nd) for x in a]


def _crop(t: np.ndarray, t0: float | None, t1: float | None) -> slice:
    a = 0 if t0 is None else int(np.searchsorted(t, t0, "left"))
    b = t.size if t1 is None else int(np.searchsorted(t, t1, "right"))
    return slice(max(a - 1, 0), min(b + 1, t.size))


# ------------------------------------------------------------------ session lookups
def _session(ctx, sid: str) -> dict:
    c = ctx.cat()
    try:
        s = c.session(sid)
    finally:
        c.close()
    if not s:
        raise HTTPException(404, f"session {sid} not found")
    return s


def _folder(ctx, s: dict) -> Path:
    return ctx.settings.data_root / s["path"]


def _doc(ctx, s: dict) -> dict:
    return json.loads((_folder(ctx, s) / "session.json").read_text())


def _run(s: dict, stream: str) -> dict | None:
    return next((r for r in s["runs"] if r["stream"] == stream and r["status"] == "ok"), None)


def _clock(ctx, s: dict, r: dict) -> tuple[float, float]:
    doc, t0_unix = session_clocks(_folder(ctx, s))
    st = next((x for x in doc.get("streams", []) if x.get("stream") == r["stream"]), {})
    t0, k, _ = run_clock(st, r["summary"], t0_unix)
    return t0, k


# ------------------------------------------------------------------ overview
@router.get("/api/sessions/{sid}/overview")
def api_overview(request: Request, sid: str, user=viewer):
    ctx = C(request)
    s = _session(ctx, sid)
    doc = _doc(ctx, s)
    reg = sensors.load(ctx.settings.root)
    streams = []
    for st in doc.get("streams", []):
        r = _run(s, st.get("stream"))
        streams.append({"stream": st.get("stream"), "role": st.get("role"), "dev": st.get("dev"), "fs": st.get("fs"),
                        "channels": st.get("channels") or [], "processed": r is not None,
                        "good_window_frac": ((r or {}).get("summary") or {}).get("summary", {}).get("good_window_frac")
                        if r else None})
    videos = [{"stream": v["stream"], "w": v.get("w"), "h": v.get("h"), "fmt": v.get("fmt", "gray8"),
               "frames": (v.get("stats") or {}).get("n_frames"), "fps": (v.get("stats") or {}).get("effective_fps")}
              for v in eyejob.video_streams(doc)]
    aux = [{**{k: a.get(k) for k in ("stream", "name", "kind", "sensor", "fs", "channels", "units", "src", "algo", "role")},
            "samples": (a.get("stats") or {}).get("n_samples"), "labels": sensors.label_channels(reg, a)}
           for a in doc.get("aux") or []]
    return {"session_id": s["session_id"], "duration_s": s.get("duration_s"), "streams": streams, "videos": videos,
            "aux": aux, "sensors": sorted(sensors.seen_in_session(doc, reg))}


# ------------------------------------------------------------------ PPG signal
def _load_filtered(path: Path, ch: str):
    def load():
        d = pd.read_csv(path, usecols=["t", f"{ch}_ac_norm"])
        return d["t"].to_numpy(float), d[f"{ch}_ac_norm"].to_numpy(float) * 100
    return _cache.get(path, f"filt:{ch}", load)


def _load_raw(path: Path, ch: str):
    def load():
        head = pd.read_csv(path, nrows=0).columns
        cols = [c for c in ("t_s", "t_dev_us", ch) if c in head]
        d = pd.read_csv(path, usecols=cols)
        return {c: d[c].to_numpy(float) for c in cols}
    return _cache.get(path, f"raw:{ch}", load)


@router.get("/api/sessions/{sid}/signal")
def api_signal(request: Request, sid: str, stream: str, source: str = Query("filtered", pattern="^(raw|filtered)$"),
               channel: str | None = None, t0: float | None = None, t1: float | None = None,
               max_points: int = 8000, user=viewer):
    ctx = C(request)
    s = _session(ctx, sid)
    doc = _doc(ctx, s)
    st = next((x for x in doc["streams"] if x["stream"] == stream), None)
    if st is None:
        raise HTTPException(404, f"stream {stream} not in session")
    ch = channel or (st.get("channels") or ["ir"])[0]
    if ch not in (st.get("channels") or [ch]):
        raise HTTPException(404, f"no channel {ch}")
    folder = _folder(ctx, s)
    bad, beats = [], None
    if source == "filtered":
        r = _run(s, stream)
        if r is None:
            raise HTTPException(409, "not processed yet")
        out = ctx.settings.data_root / r["out_dir"]
        stem = r["summary"]["stem"]
        tp, y = _load_filtered(out / f"{stem}_signals.csv", ch)
        c0, k = _clock(ctx, s, r)
        t = c0 + k * tp
        qp = out / f"{stem}_quality.csv"
        if qp.exists():
            q = _cache.get(qp, "q", lambda: pd.read_csv(qp))
            if "good" in q:
                b = q[~q["good"].astype(bool)]
                for a, e in zip(c0 + k * b["t_start"], c0 + k * b["t_end"]):  # merge overlapping windows
                    if bad and a <= bad[-1][1]:
                        bad[-1][1] = max(bad[-1][1], float(e))
                    else:
                        bad.append([float(a), float(e)])
        unit = "AC/DC (%)"
        lo = t[0] if t0 is None else t0
        hi = t[-1] if t1 is None else t1
        bp = out / f"{stem}_beats.csv"
        if hi - lo <= BEATS_MAX_SPAN_S and bp.exists():
            bt = _cache.get(bp, "beats", lambda: pd.read_csv(bp, usecols=lambda c: c in (
                "t_foot", "t_peak", "notch_idx", "beat_good", "beat_reason", "hr_bpm")))
            if len(bt):
                tpk = c0 + k * bt["t_peak"].to_numpy(float)
                m = (tpk >= lo) & (tpk <= hi)
                yy = np.interp(tpk[m], t, np.nan_to_num(y))
                beats = {"t": clean(tpk[m], 4), "y": clean(yy), "good": [bool(g) for g in bt["beat_good"].to_numpy()[m]],
                         "reason": [("" if isinstance(x, float) else str(x)) for x in bt["beat_reason"].to_numpy()[m]]
                         if "beat_reason" in bt else None,
                         "hr": clean(bt["hr_bpm"].to_numpy(float)[m], 1) if "hr_bpm" in bt else None}
    else:
        d = _load_raw(folder / st["file"], ch)
        fit = device_clock(st, session_t0_unix(doc, read_events(folder)))
        if fit and "t_dev_us" in d:
            t = fit[0] + fit[1] * d["t_dev_us"] / 1e6
        else:
            t = d["t_s"] if "t_s" in d else np.arange(len(d[ch])) / float(st.get("fs") or 100)
        y = d[ch]
        unit = "ADC counts"
    full = [float(t[0]), float(t[-1])] if t.size else [0.0, 0.0]
    sl = _crop(t, t0, t1)
    tc, yc = t[sl], y[sl]
    td, yd = minmax_decimate(tc, yc, max(100, min(max_points, 50000)))
    if t0 is not None or t1 is not None:
        bad = [w for w in bad if w[1] >= (t0 if t0 is not None else -1e18) and w[0] <= (t1 if t1 is not None else 1e18)]
    return {"stream": stream, "channel": ch, "source": source, "unit": unit, "t": clean(td, 4), "y": clean(yd),
            "bad_windows": bad, "n_original": int(t.size), "n_in_range": int(tc.size), "full_range": full,
            "decimated": bool(tc.size > td.size), "beats": beats, "fs": st.get("fs")}


# ------------------------------------------------------------------ quality
@router.get("/api/sessions/{sid}/quality")
def api_quality(request: Request, sid: str, stream: str, user=viewer):
    """Per analysis window: accepted or not and why (Layer 1), the metrics behind it with the limits
    in force, and per 30-s epoch the share of beats that passed the beat checks (Layer 2)."""
    ctx = C(request)
    s = _session(ctx, sid)
    r = _run(s, stream)
    if r is None:
        raise HTTPException(409, "not processed yet")
    out = ctx.settings.data_root / r["out_dir"]
    stem = r["summary"]["stem"]
    c0, k = _clock(ctx, s, r)
    res: dict = {"stream": stream, "reasons_help": REASONS, "windows": None, "beats": None, "limits": {}}
    qp = out / f"{stem}_quality.csv"
    if qp.exists():
        q = pd.read_csv(qp)
        if len(q) and "good" in q:
            good = q["good"].astype(bool).to_numpy()
            counts: dict[str, int] = {}
            for x in q["reason"].to_numpy():
                for name in (str(x).split(";") if isinstance(x, str) else []):
                    if name:
                        counts[name] = counts.get(name, 0) + 1
            ch = next((c[:-5] for c in q.columns if c.endswith("_good")), None)
            metrics = {}
            for m in ("acf_peak", "skew", "spec_purity", "amp_ratio", "pi_pct", "valid_frac"):
                col = f"{ch}_{m}"
                if col in q:
                    metrics[m] = clean(q[col].to_numpy(float), 4)
            both = [f"{ch}_hr_spec_bpm", f"{ch}_hr_acf_bpm"]
            if all(c in q for c in both):
                metrics["hr_disagree"] = clean(np.abs(q[both[0]] - q[both[1]]).to_numpy(float), 2)
            res["windows"] = {"t0": clean(c0 + k * q["t_start"].to_numpy(float), 2),
                              "t1": clean(c0 + k * q["t_end"].to_numpy(float), 2),
                              "good": [bool(g) for g in good], "n": int(len(q)), "good_frac": round(float(good.mean()), 3),
                              "reasons": {n: round(c / len(q), 3) for n, c in sorted(counts.items(), key=lambda x: -x[1])},
                              "metrics": metrics}
    mp = out / f"{stem}_meta.json"
    if mp.exists():
        try:
            qc = json.loads(mp.read_text()).get("config", {}).get("quality", {})
            res["limits"] = {"acf_peak": [qc.get("min_acf_peak"), None], "skew": [qc.get("min_skew"), None],
                             "spec_purity": [qc.get("min_spec_purity"), None], "amp_ratio": qc.get("amp_ratio_range"),
                             "hr_disagree": [None, qc.get("max_hr_disagreement_bpm")],
                             "valid_frac": [qc.get("min_valid_frac"), None]}
        except ValueError:
            pass
    bp = out / f"{stem}_beats.csv"
    if bp.exists():
        b = pd.read_csv(bp, usecols=lambda c: c in ("t_foot", "beat_good", "beat_reason"))
        if len(b):
            g = b["beat_good"].astype(bool).to_numpy()
            tb = c0 + k * b["t_foot"].to_numpy(float)
            edges = np.arange(np.floor(tb[0] / 30) * 30, tb[-1] + 30, 30)
            idx = np.clip(np.digitize(tb, edges) - 1, 0, len(edges) - 2) if len(edges) > 1 else np.zeros(len(tb), int)
            nb = max(len(edges) - 1, 1)
            n = np.bincount(idx, minlength=nb)
            ng = np.bincount(idx, weights=g, minlength=nb)
            counts = {}
            if "beat_reason" in b:
                for x in b["beat_reason"].to_numpy():
                    for name in (str(x).split(";") if isinstance(x, str) else []):
                        if name:
                            counts[name] = counts.get(name, 0) + 1
            res["beats"] = {"n": int(len(b)), "good_frac": round(float(g.mean()), 3),
                            "t": clean(edges[:nb] + 15, 1), "n_bin": [int(x) for x in n],
                            "good_frac_bin": [round(float(a / c), 3) if c else None for a, c in zip(ng, n)],
                            "reasons": {n_: round(c / len(b), 3) for n_, c in sorted(counts.items(), key=lambda x: -x[1])}}
    return res


# ------------------------------------------------------------------ eye
def _frames(ctx, s: dict, stream: str) -> Path:
    f = eyejob.frames_file(_folder(ctx, s), _doc(ctx, s), stream)
    if f is None:
        raise HTTPException(404, f"no camera stream {stream} in {s['session_id']}")
    return f


@router.get("/api/sessions/{sid}/eye")
def api_eye(request: Request, sid: str, stream: str, run: bool = True, user=viewer):
    """{"state": "done", ...eye.json} when tracked; else {"state": queued|running|error, "progress"}.
    A session that was never tracked (or with an older tracker version) starts tracking now."""
    ctx = C(request)
    s = _session(ctx, sid)
    f = _frames(ctx, s, stream)
    sid = s["session_id"]
    d = eyejob.load_summary(ctx.settings, sid, stream, f)
    st = ctx.eye_jobs.status(sid, stream)
    if d is not None and not (st and st["state"] in ("queued", "running")):
        return {"state": "done", **d}
    if st is None or st["state"] == "done":   # done but unreadable -> run again
        if not run:
            return {"state": "missing"}
        try:
            from ..video import gray_array
            gray_array(f)
        except ValueError as e:
            raise HTTPException(409, f"eye tracking needs a grayscale stream ({e})") from None
        st = ctx.eye_jobs.start(sid, stream, f)
    return st


@router.get("/api/sessions/{sid}/eye/trace")
def api_eye_trace(request: Request, sid: str, stream: str, t0: float | None = None, t1: float | None = None,
                  max_points: int = 6000, user=viewer):
    ctx = C(request)
    s = _session(ctx, sid)
    p = eyejob.out_dir(ctx.settings, s["session_id"], stream) / "eye_frames.npz"
    if "/" in stream or ".." in stream or not p.exists():
        raise HTTPException(409, "not tracked yet")
    z = _cache.get(p, "eye", lambda: eyejob.load_frames(ctx.settings, s["session_id"], stream))
    t = z["t"]
    sl = _crop(t, t0, t1)
    tc = t[sl]
    n = max(100, min(max_points, 50000))
    out = {"stream": stream, "n_in_range": int(tc.size), "full_range": [float(t[0]), float(t[-1])] if t.size else [0, 0]}
    td, cd = minmax_decimate(tc, z["closure"][sl].astype(float), n)
    out["closure"] = {"t": clean(td, 3), "y": clean(cd, 1)}
    td, md = minmax_decimate(tc, z["motion"][sl].astype(float), n)
    out["motion"] = {"t": clean(td, 3), "y": clean(md, 4)}
    step = max(1, tc.size // n)   # gaze relative to the eye centre, plainly subsampled (a position, not a pulse)
    gx = (z["gx"][sl] - z["cx"][sl])[::step]
    gy = (z["gy"][sl] - z["cy"][sl])[::step]
    state = z["state"][sl][::step]
    gx = np.where(state == eyejob.STATES.index("open"), gx, np.nan)
    gy = np.where(state == eyejob.STATES.index("open"), gy, np.nan)
    out["gaze"] = {"t": clean(tc[::step], 3), "x": clean(gx, 2), "y": clean(gy, 2)}
    out["decimated"] = bool(tc.size > n)
    # spans without a tracked eye, for shading
    st = z["state"][sl]
    lost = st <= eyejob.STATES.index("calibrating")
    spans = []
    if lost.any():
        d = np.diff(np.r_[0, lost.astype(np.int8), 0])
        for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
            if tc[min(b, tc.size - 1)] - tc[a] >= 0.5:
                spans.append([round(float(tc[a]), 2), round(float(tc[min(b, tc.size - 1)]), 2)])
    out["no_eye"] = spans[:2000]
    return out


@router.get("/api/sessions/{sid}/video/{stream}/frame")
def api_video_frame(request: Request, sid: str, stream: str, i: int | None = None, t: float | None = None,
                    user=viewer):
    """Recorded frame number `i` (or the one captured closest to session time `t`) as a lossless PNG.
    Headers: X-Frame-Index, X-Frame-Count, X-Frame-T and, once the session is tracked, X-Eye (the
    tracker's result for this frame, in frame pixels, for the overlay)."""
    from PIL import Image
    import io

    from ..video import frame_times, gray_array

    ctx = C(request)
    s = _session(ctx, sid)
    f = _frames(ctx, s, stream)
    try:
        arr, idx = gray_array(f)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    n = len(arr)
    if not n:
        raise HTTPException(404, "no frames")
    ft = _cache.get(f, "ft", lambda: frame_times(idx["t_s"], idx["t_dev_us"]))
    if i is None:
        i = int(np.argmin(np.abs(ft - (t if t is not None else ft[0]))))
    i = min(max(int(i), 0), n - 1)
    out = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr[i]), "L").save(out, "PNG", compress_level=1)
    headers = {"Cache-Control": "private, max-age=3600", "X-Frame-Index": str(i), "X-Frame-Count": str(n),
               "X-Frame-T": f"{float(ft[i]):.3f}"}
    p = eyejob.out_dir(ctx.settings, s["session_id"], stream) / "eye_frames.npz"
    if p.exists():
        z = _cache.get(p, "eye", lambda: eyejob.load_frames(ctx.settings, s["session_id"], stream))
        if i < len(z["t"]):
            ov = {"state": eyejob.STATES[int(z["state"][i])]}
            for kk in ("x", "y", "r", "gx", "gy", "cx", "cy", "closure"):
                v = float(z[kk][i])
                ov[kk] = round(v, 1) if np.isfinite(v) else None
            ov["closed"] = ov["state"] == "closed"
            headers["X-Eye"] = json.dumps(ov, separators=(",", ":"))
    return Response(out.getvalue(), media_type="image/png", headers=headers)


# ------------------------------------------------------------------ cued sessions
@router.get("/api/sessions/{sid}/cues")
def api_cues(request: Request, sid: str, stream: str | None = None,
             format: str = Query("json", pattern="^(json|csv)$"), user=viewer):
    """The cue log of the session's cued runs (sglab.cues; {"runs": []} when there were none).
    Every entry has t_s on the session clock (the clock of the frames). With stream=<camera>
    each entry also gets frame_i: the recorded frame captured closest to t_s (and screen_frame_i).
    format=csv: one row per entry (the cues.csv saved with the session)."""
    from .. import cues as cuemod

    ctx = C(request)
    s = _session(ctx, sid)
    folder = _folder(ctx, s)
    doc = cuemod.read_doc(folder) or {"schema": cuemod.SCHEMA, "runs": []}
    if format == "csv":
        return Response(cuemod.to_csv(doc), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{s["session_id"]}_cues.csv"'})
    if stream:
        from ..video import frame_times, gray_array
        f = _frames(ctx, s, stream)
        try:
            _, idx = gray_array(f)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        ft = _cache.get(f, "ft", lambda: frame_times(idx["t_s"], idx["t_dev_us"]))
        order = np.argsort(ft)
        fs = ft[order]

        def nearest(t):
            if t is None or not fs.size:
                return None
            j = int(np.searchsorted(fs, t))
            best = min((k for k in (j - 1, j) if 0 <= k < fs.size), key=lambda k: abs(fs[k] - t))
            return int(order[best])
        for r in doc["runs"]:
            for e in r["entries"]:
                e["frame_i"] = nearest(e.get("t_s"))
                if e.get("screen_t_s") is not None:
                    e["screen_frame_i"] = nearest(e["screen_t_s"])
        doc["frames"] = {"stream": stream, "n": int(ft.size)}
    return doc


# ------------------------------------------------------------------ further sensors
@router.get("/api/sessions/{sid}/aux")
def api_aux(request: Request, sid: str, stream: str, t0: float | None = None, t1: float | None = None,
            max_points: int = 6000, user=viewer):
    ctx = C(request)
    s = _session(ctx, sid)
    doc = _doc(ctx, s)
    a = next((x for x in doc.get("aux") or [] if x.get("stream") == stream), None)
    if a is None or not a.get("file"):
        raise HTTPException(404, f"no stream {stream} in {s['session_id']}")
    p = _folder(ctx, s) / a["file"]
    d = _cache.get(p, "aux", lambda: pd.read_csv(p))
    t = d["t_s"].to_numpy(float)
    sl = _crop(t, t0, t1)
    labels = sensors.label_channels(sensors.load(ctx.settings.root), a)
    chans = []
    for lab in labels:
        if lab["channel"] not in d:
            continue
        y = d[lab["channel"]].to_numpy(float)[sl]
        ok = np.isfinite(y)   # slow channels leave cells empty: plot the values that exist
        td, yd = minmax_decimate(t[sl][ok], y[ok], max(100, min(max_points, 50000)))
        chans.append({**lab, "t": clean(td, 3), "y": clean(yd), "n": int(ok.sum())})
    return {"stream": stream, "name": a.get("name"), "kind": a.get("kind"), "sensor": a.get("sensor"),
            "src": a.get("src"), "algo": a.get("algo"), "channels": chans,
            "full_range": [float(t[0]), float(t[-1])] if t.size else [0, 0]}
