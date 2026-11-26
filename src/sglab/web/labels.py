"""Eye labeling: the page where lab members label how open the eye is in each camera frame, and its API.

Pages
    GET /labeling                          who labeled what (all sessions), and where to start
    GET /sessions/{sid}/label?stream=      the labeling tool for one camera stream

API (errors are {"error": "..."} like the rest of the server)
    GET  /api/sessions/{sid}/video/{stream}/times          {"n", "w", "h", "fps", "t": [frame times, s]}
    GET  /api/sessions/{sid}/video/{stream}/chunk?i0=&n=&x=&y=&w=&h=&f=
                                     n frames from i0 as raw 8-bit gray bytes (frame after frame, row by row),
                                     optionally cropped to x,y,w,h and block-averaged by f.
                                     Headers X-Frame-I0, X-Frame-N, X-Frame-W, X-Frame-H. Fast enough to scrub.
    GET  /api/sessions/{sid}/eye/labels?stream=[&labeler=][&author=]
                                     {"n", "levels": LEVELS, "mine": TRACK|null, "other": TRACK|null (labeler=),
                                      "tracks": [TRACK without levels, ...], "crop", "can_label", "me"}
    POST /api/sessions/{sid}/eye/labels   {stream, ranges: [[i0, i1, level 0..5 | null], ...], blind?, author?}
                                     -> TRACK without levels
    GET  /api/sessions/{sid}/eye/agreement?stream=&a=&b=   frame agreement of two labelers (sglab.eyelabels.agreement)
    PUT  /api/sessions/{sid}/eye/crop     {stream, box: {x, y, w, h} | null, author?} -> the box
    GET  /api/sessions/{sid}/eye/prefill?stream=           the guesses (sglab.eyeprefill), cached per eye box:
                                     {"version", "n", "level": "5554321...", "open": [0..1 | null], "candidates": [[i0, i1]], ...}
TRACK = {"labeler", "labeler_name", "source", "n_frames", "n_labeled", "blind", "created_at", "updated_at", "levels": "..5543.."}

Labeling needs LABEL_ROLE (operator); anybody who can read sees the page and everybody's progress.
Without accounts, the labeler is the name typed on the page (like the lab board).
"""
from __future__ import annotations

import hashlib
import threading

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import eyejob, eyeprefill
from ..auth import has_role
from ..eyelabels import LEVELS, LabelError, LabelStore, agreement, crop_key
from ..video import frame_times, gray_array
from .review import _cache, _doc, _frames, _session
from .security import require

router = APIRouter()
viewer = Depends(require("viewer"))
LABEL_ROLE = "operator"
MAX_CHUNK_BYTES = 16 << 20
_prefill_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def C(request: Request):
    return request.app.state.ctx


def store(ctx) -> LabelStore:
    s = getattr(ctx, "eye_labels", None)
    if s is None:
        s = ctx.eye_labels = LabelStore(ctx.settings.db_path)
    return s


def _who(ctx, user: dict | None, author: str | None, required: bool = True) -> tuple[str, str] | None:
    """(labeler, name to show): the account, or without accounts the name typed on the page."""
    if ctx.auth:
        return (user["username"], user.get("display") or user["username"]) if user else None
    name = str(author or "").strip()[:80]
    if not name:
        if required:
            raise HTTPException(400, "Please type your name or initials first.")
        return None
    return name, name


def _stream_frames(ctx, sid: str, stream: str):
    s = _session(ctx, sid)
    f = _frames(ctx, s, stream)
    try:
        arr, idx = gray_array(f)
    except ValueError as e:
        raise HTTPException(409, f"labeling needs a grayscale camera stream ({e})") from None
    if not len(arr):
        raise HTTPException(404, "this camera stream has no frames")
    ft = _cache.get(f, "ft", lambda: frame_times(idx["t_s"], idx["t_dev_us"]))
    return s, f, arr, ft


def _gray_videos(doc: dict) -> list[str]:
    return [v["stream"] for v in eyejob.video_streams(doc) if v.get("fmt", "gray8") == "gray8"]


# ------------------------------------------------------------------ pages
@router.get("/labeling", response_class=HTMLResponse)
def labeling_page(request: Request, user=viewer):
    ctx = C(request)
    rows = store(ctx).progress()
    c = ctx.cat()
    try:
        sessions = c.sessions()[:300]
    finally:
        c.close()
    labeled = {(r["session_id"], r["stream"]) for r in rows}
    # sessions with a camera that nobody has labeled yet (newest first)
    todo = []
    for s in sessions:
        try:
            vids = _gray_videos(_doc(ctx, s))
        except (OSError, ValueError):
            continue
        todo += [{"session_id": s["session_id"], "stream": v, "subject_id": s.get("subject_id"),
                  "started": s.get("started_at") or s.get("start_time") or ""}
                 for v in vids if (s["session_id"], v) not in labeled]
        if len(todo) >= 40:
            break
    return ctx.render(request, "labeling.html", {"rows": rows, "todo": todo, "levels": LEVELS,
                                                  "can_label": has_role(user, LABEL_ROLE)})


@router.get("/sessions/{sid}/label", response_class=HTMLResponse)
def label_page(request: Request, sid: str, stream: str | None = None, user=viewer):
    ctx = C(request)
    s = _session(ctx, sid)
    vids = _gray_videos(_doc(ctx, s))
    if not vids:
        raise HTTPException(404, "This recording has no grayscale camera stream to label.")
    stream = stream if stream in vids else vids[0]
    return ctx.render(request, "label.html", {
        "s": s, "stream": stream, "videos": vids, "levels": LEVELS, "can_label": has_role(user, LABEL_ROLE),
        "label_role": LABEL_ROLE, "ask_name": not ctx.auth})


# ------------------------------------------------------------------ frames
@router.get("/api/sessions/{sid}/video/{stream}/times")
def api_times(request: Request, sid: str, stream: str, user=viewer):
    _, _, arr, ft = _stream_frames(C(request), sid, stream)
    dt = np.diff(ft)
    dt = dt[dt > 0]
    return {"n": int(len(arr)), "w": int(arr.shape[2]), "h": int(arr.shape[1]),
            "fps": round(1.0 / float(np.median(dt)), 2) if len(dt) else None,
            "t": [round(float(x), 4) for x in ft]}


@router.get("/api/sessions/{sid}/video/{stream}/chunk")
def api_chunk(request: Request, sid: str, stream: str, i0: int = 0, n: int = Query(256, ge=1, le=4096),
              x: int | None = None, y: int | None = None, w: int | None = None, h: int | None = None,
              f: int = Query(1, ge=1, le=16), user=viewer):
    _, _, arr, _ = _stream_frames(C(request), sid, stream)
    N, H, W = arr.shape
    i0 = max(0, min(int(i0), N - 1))
    i1 = min(N, i0 + n)
    if None not in (x, y, w, h):
        x0, y0 = max(0, min(x, W - 1)), max(0, min(y, H - 1))
        x1, y1 = min(W, x0 + max(1, w)), min(H, y0 + max(1, h))
    else:
        x0, y0, x1, y1 = 0, 0, W, H
    ow, oh = (x1 - x0) // f, (y1 - y0) // f
    if ow < 1 or oh < 1:
        raise HTTPException(400, "the crop is smaller than the downscale factor")
    if (i1 - i0) * ow * oh > MAX_CHUNK_BYTES:
        raise HTTPException(400, "too many frames at once: ask for fewer, or crop / downscale")
    a = np.asarray(arr[i0:i1, y0:y0 + oh * f, x0:x0 + ow * f])
    if f > 1:
        a = a.reshape(len(a), oh, f, ow, f).mean(axis=(2, 4)).round().astype(np.uint8)
    return Response(np.ascontiguousarray(a).tobytes(), media_type="application/octet-stream", headers={
        "X-Frame-I0": str(i0), "X-Frame-N": str(i1 - i0), "X-Frame-W": str(ow), "X-Frame-H": str(oh),
        "Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ labels
@router.get("/api/sessions/{sid}/eye/labels")
def api_labels(request: Request, sid: str, stream: str, labeler: str | None = None, author: str | None = None,
               user=viewer):
    ctx = C(request)
    s, _, arr, _ = _stream_frames(ctx, sid, stream)
    sid, b = s["session_id"], store(ctx)
    me = _who(ctx, user, author, required=False)
    return {"n": int(len(arr)), "levels": LEVELS,
            "me": me[0] if me else None,
            "mine": b.get(sid, stream, me[0]) if me else None,
            "other": b.get(sid, stream, labeler) if labeler else None,
            "tracks": b.tracks(sid, stream), "crop": b.crop(sid, stream),
            "can_label": has_role(user, LABEL_ROLE)}


@router.post("/api/sessions/{sid}/eye/labels")
async def api_label(request: Request, sid: str, user=Depends(require(LABEL_ROLE))):
    ctx = C(request)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON object") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object")
    stream = str(data.get("stream") or "")
    s, _, arr, _ = _stream_frames(ctx, sid, stream)
    who, name = _who(ctx, user, data.get("author"))
    try:
        t = store(ctx).apply(s["session_id"], stream, who, len(arr), data.get("ranges"), labeler_name=name,
                             blind=data.get("blind") if "blind" in data else None)
    except LabelError as e:
        raise HTTPException(400, str(e)) from None
    t.pop("levels", None)
    return t


@router.get("/api/sessions/{sid}/eye/agreement")
def api_agreement(request: Request, sid: str, stream: str, a: str, b: str, user=viewer):
    ctx = C(request)
    s = _session(ctx, sid)
    ta, tb = store(ctx).get(s["session_id"], stream, a), store(ctx).get(s["session_id"], stream, b)
    if not ta or not tb:
        raise HTTPException(404, "one of these people has not labeled this stream")
    return {"a": a, "b": b, **agreement(ta["levels"], tb["levels"])}


@router.put("/api/sessions/{sid}/eye/crop")
async def api_crop(request: Request, sid: str, user=Depends(require(LABEL_ROLE))):
    ctx = C(request)
    data = await request.json()
    stream = str(data.get("stream") or "")
    s, _, arr, _ = _stream_frames(ctx, sid, stream)
    who = _who(ctx, user, data.get("author"), required=False)
    try:
        box = store(ctx).set_crop(s["session_id"], stream, data.get("box"), arr.shape[2], arr.shape[1],
                                  by=who[1] if who else "")
    except LabelError as e:
        raise HTTPException(400, str(e)) from None
    ctx.audit(request, "eye.crop", s["session_id"], {"stream": stream, "box": box})
    return JSONResponse({"crop": box})


@router.get("/api/sessions/{sid}/eye/prefill")
def api_prefill(request: Request, sid: str, stream: str, user=viewer):
    ctx = C(request)
    s, f, arr, ft = _stream_frames(ctx, sid, stream)
    sid = s["session_id"]
    crop = store(ctx).crop(sid, stream)
    st = f.stat()
    key = hashlib.sha1(f"{eyeprefill.VERSION}|{crop_key(crop)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    out = eyejob.out_dir(ctx.settings, sid, stream)
    path = out / f"prefill_{key}.npz"
    with _locks_guard:
        lock = _prefill_locks.setdefault(str(path), threading.Lock())
    with lock:   # one computation per stream and eye box, however many labelers open it at once
        if path.exists():
            z = np.load(path, allow_pickle=False)
            level, op, cand = z["level"], z["open"], z["candidates"].tolist()
            comp = None
        else:
            r = eyeprefill.compute(arr, ft, crop)
            level, op, cand, comp = r["level"], r["open"], r["candidates"], r["component"]
            out.mkdir(parents=True, exist_ok=True)
            for old in out.glob("prefill_*.npz"):   # an older eye box or version
                old.unlink(missing_ok=True)
            tmp = path.with_suffix(".tmp.npz")
            np.savez_compressed(tmp, level=level, open=op, candidates=np.asarray(cand, dtype=np.int64).reshape(-1, 2))
            tmp.replace(path)
    return {"version": eyeprefill.VERSION, "n": int(len(level)), "crop": crop,
            "level": "".join(str(int(v)) for v in level),
            "open": [None if not np.isfinite(v) else round(float(v), 2) for v in op],
            "candidates": cand, "component": comp}
