"""Eye tracking over recorded camera streams, as a re-creatable processing output.

    processed/<session_id>/<video stream>/eye.json         summary, per-minute table, every blink
                                                           and gaze shift (sglab.eyetrack)
    processed/<session_id>/<video stream>/eye_frames.npz   one row per frame at full rate:
                                                           t, state, closure, motion, pupil, gaze...

Raw frames are kept, so this can be redone for every old session whenever the tracker changes
(eyetrack.VERSION is stored; an older result is recomputed on the next request).

EyeJobs runs one tracking job at a time in a background thread (a 90-min recording at 25 fps is
~135 000 frames, a few minutes) and reports progress to the session page.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from . import eyetrack
from .video import frame_times, gray_array

STATES = ("no_eye", "calibrating", "open", "closed")
NUM_COLS = ("t", "closure", "motion", "x", "y", "r", "gx", "gy", "cx", "cy", "brightness", "contrast")


def video_streams(doc: dict) -> list[dict]:
    """Camera streams of a session.json (top-level "video", or per stream in older files)."""
    vids = list(doc.get("video") or []) + [v for st in doc.get("streams", []) for v in (st.get("video") or [])]
    seen, out = set(), []
    for v in vids:
        if v.get("stream") and v["stream"] not in seen:
            seen.add(v["stream"])
            out.append(v)
    return out


def frames_file(folder: Path, doc: dict, stream: str) -> Path | None:
    if "/" in stream or "\\" in stream or ".." in stream:
        return None
    v = next((x for x in video_streams(doc) if x.get("stream") == stream), None)
    f = folder / (v.get("file") if v and v.get("file") else f"{stream}.frames")
    return f if f.exists() else None


def out_dir(settings, sid: str, stream: str) -> Path:
    return settings.data_root / "processed" / sid / stream


def load_summary(settings, sid: str, stream: str, frames: Path | None = None) -> dict | None:
    """eye.json if it is current (tracker version, not older than the frames file)."""
    p = out_dir(settings, sid, stream) / "eye.json"
    if not p.exists() or not (p.parent / "eye_frames.npz").exists():
        return None
    try:
        d = json.loads(p.read_text())
    except ValueError:
        return None
    if d.get("version") != eyetrack.VERSION or (frames is not None and p.stat().st_mtime < frames.stat().st_mtime):
        return None
    return d


def load_frames(settings, sid: str, stream: str) -> dict[str, np.ndarray]:
    with np.load(out_dir(settings, sid, stream) / "eye_frames.npz") as z:
        return {k: z[k] for k in z.files}


def run(settings, sid: str, stream: str, frames: Path, progress=None) -> dict:
    """Track every frame of one recorded stream and write both output files. Returns eye.json's content."""
    arr, idx = gray_array(frames)
    if not len(arr):
        raise ValueError("the camera stream has no frames")
    t = frame_times(idx["t_s"], idx["t_dev_us"])
    rows, summary = eyetrack.track_frames(arr, t, progress)
    cols = {c: np.array([np.nan if r.get(c) is None else r[c] for r in rows], dtype=np.float32 if c != "t" else np.float64)
            for c in NUM_COLS}
    cols["state"] = np.array([STATES.index(r["state"]) if r["state"] in STATES else 0 for r in rows], dtype=np.uint8)
    cols["seq"] = np.asarray(idx["seq"])
    out = out_dir(settings, sid, stream)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "eye_frames.npz", **cols)
    doc = {"stream": stream, "version": eyetrack.VERSION, "n_frames": len(rows),
           "t_first": float(t[0]), "t_last": float(t[-1]),
           "fps": round((len(t) - 1) / (t[-1] - t[0]), 2) if len(t) > 1 and t[-1] > t[0] else None,
           "units": {"gaze": "px of the frame scaled to 80 px", "gaze_speed": "px/s (frame scaled to 80 px)"},
           **{k: summary.pop(k) for k in ("minutes", "blinks", "shifts", "motion_blinks")}, "summary": summary}
    (out / "eye.json").write_text(json.dumps(doc))
    return doc


class EyeJobs:
    """Background tracking jobs, one at a time; state per (session, stream) for the session page."""

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.state: dict[tuple[str, str], dict] = {}
        self._sem = threading.Semaphore(1)

    def status(self, sid: str, stream: str) -> dict | None:
        with self.lock:
            st = self.state.get((sid, stream))
            return dict(st) if st else None

    def start(self, sid: str, stream: str, frames: Path) -> dict:
        key = (sid, stream)
        with self.lock:
            st = self.state.get(key)
            if st and st["state"] in ("queued", "running"):
                return dict(st)
            st = self.state[key] = {"state": "queued", "progress": 0.0}

        def job():
            with self._sem:
                self._set(key, state="running")
                try:
                    run(self.settings, sid, stream, frames,
                        lambda i, n: self._set(key, progress=round(i / max(n, 1), 3)))
                    self._set(key, state="done", progress=1.0)
                except Exception as e:  # shown on the page
                    self._set(key, state="error", error=f"{type(e).__name__}: {e}")

        threading.Thread(target=job, daemon=True, name=f"eye-{sid}").start()
        return dict(st)

    def _set(self, key, **kw) -> None:
        with self.lock:
            self.state[key].update(kw)

    def start_session(self, sid: str, folder: Path, doc: dict) -> int:
        """Queue every gray8 camera stream of a session that has no current result (after a trial is saved)."""
        n = 0
        for v in video_streams(doc):
            if v.get("fmt", "gray8") != "gray8":
                continue
            f = frames_file(folder, doc, v["stream"])
            if f is not None and load_summary(self.settings, sid, v["stream"], f) is None:
                self.start(sid, v["stream"], f)
                n += 1
        return n
