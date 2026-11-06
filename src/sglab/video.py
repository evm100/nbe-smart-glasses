"""Camera streams (protocol v2 frame records): raw frame files, their index, live previews.

Per camera stream in a session folder (written once, like every raw file):

    <role>_<dev>_<stream>.frames   every frame exactly as the board sent it
    <role>_<dev>_<stream>.csv      one row per frame: t_s, seq, t_dev_us, exposure, gain, offset, nbytes

.frames layout (little-endian):
    file header   b"NBEVID01" | w u16 | h u16 | fmt u8 | 7 bytes zero          (24 bytes)
    per frame     nbytes u32 | seq u32 | t_dev_us i64 | t_s f64 | exposure u16 | gain u8 | fmt u8 | data
fmt 0 = 8-bit grayscale (nbytes = w*h), 1 = JPEG. t_dev_us is on the same device clock as the
board's PPG samples (t_dev_us in <role>_<dev>.csv), so the two line up without the PC clock.

    frames = read_frames(path)          # iterator of FrameRec (index, data)
    arr, idx = gray_array(path)         # (n, h, w) uint8 memmap-backed array + index table
"""
from __future__ import annotations

import csv
import io
import struct
import threading
from collections import deque
from pathlib import Path
from typing import Iterator, NamedTuple

import numpy as np

from nbe_gateway.wire import FMT_GRAY8, FMT_JPEG, FMT_NAMES, VideoFrame

FILE_MAGIC = b"NBEVID01"
FILE_HDR = struct.Struct("<8sHHB7x")
REC_HDR = struct.Struct("<IIqdHBB")
FPS_WINDOW_S = 5.0
FMT_BY_NAME = {v: k for k, v in FMT_NAMES.items()}


class FrameRec(NamedTuple):
    seq: int
    t_dev_us: int
    t_s: float
    exposure: int
    gain: int
    fmt: int
    data: bytes


def read_frames(path: Path) -> Iterator[FrameRec]:
    with open(path, "rb") as f:
        hdr = f.read(FILE_HDR.size)
        magic, _w, _h, _fmt = FILE_HDR.unpack(hdr)
        if magic != FILE_MAGIC:
            raise ValueError(f"{path}: not an NBE frames file")
        while True:
            h = f.read(REC_HDR.size)
            if len(h) < REC_HDR.size:
                return
            n, seq, t_us, t_s, exp, gain, fmt = REC_HDR.unpack(h)
            data = f.read(n)
            if len(data) < n:
                return  # cut short (recording interrupted)
            yield FrameRec(seq, t_us, t_s, exp, gain, fmt, data)


def gray_array(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """All frames of a gray8 stream as an (n, h, w) uint8 array (memory-mapped, no copy) and an
    index table (seq, t_dev_us, t_s, exposure, gain) as a structured array."""
    with open(path, "rb") as f:
        magic, w, h, fmt = FILE_HDR.unpack(f.read(FILE_HDR.size))
    if magic != FILE_MAGIC or fmt != FMT_GRAY8:
        raise ValueError(f"{path}: not a gray8 frames file")
    rec = np.dtype([("nbytes", "<u4"), ("seq", "<u4"), ("t_dev_us", "<i8"), ("t_s", "<f8"), ("exposure", "<u2"),
                    ("gain", "u1"), ("fmt", "u1"), ("px", "u1", (h, w))])
    n = (Path(path).stat().st_size - FILE_HDR.size) // rec.itemsize
    m = np.memmap(path, dtype=rec, mode="r", offset=FILE_HDR.size, shape=(n,))
    return m["px"], m[["seq", "t_dev_us", "t_s", "exposure", "gain"]]


def preview_image(fr: VideoFrame) -> tuple[bytes, str]:
    """(image, media type) of one frame for the live page: grayscale frames as a lossless PNG
    (the operator sees exactly the pixels that are recorded), JPEG frames pass through as sent."""
    if fr.fmt == FMT_JPEG:
        return fr.data, "image/jpeg"
    from PIL import Image  # matplotlib depends on Pillow

    out = io.BytesIO()
    # fast zlib level: an 80x80 crop is ~4-5 kB either way, and this runs per frame per viewer stream
    Image.frombytes("L", (fr.w, fr.h), fr.data).save(out, "PNG", compress_level=1)
    return out.getvalue(), "image/png"


class VideoWriter:
    """One camera stream of a recording (StreamWorker owns it): raw file + index + live state."""

    def __init__(self, folder: Path, name: str, header: dict):
        self.name, self.header = name, dict(header)
        self.sid = int(header.get("sid", 0))
        self.w, self.h = int(header.get("w", 0)), int(header.get("h", 0))
        self.fmt = FMT_BY_NAME.get(header.get("fmt", "gray8"), FMT_GRAY8)
        self.frames_path = folder / f"{name}.frames"
        self.index_path = folder / f"{name}.csv"
        self._f = open(self.frames_path, "wb")
        self._f.write(FILE_HDR.pack(FILE_MAGIC, self.w, self.h, self.fmt))
        self._idx = open(self.index_path, "w", newline="")
        self._csv = csv.writer(self._idx)
        self._csv.writerow(["t_s", "seq", "t_dev_us", "exposure", "gain", "offset", "nbytes"])
        self.lock = threading.Lock()
        self._new_frame = threading.Condition(self.lock)  # wakes preview() waiters (live page long-poll)
        self.latest: VideoFrame | None = None
        self.latest_t_s: float | None = None
        self._preview: tuple[int, bytes, str] | None = None   # (count when made, image, media type)
        self._recent: deque = deque()                    # device times of the last FPS_WINDOW_S
        self.stats = {"n_frames": 0, "n_dropped_seq": 0, "n_duplicates": 0, "n_bad_size": 0, "bytes": 0,
                      "n_seq_resets": 0, "t_first_s": None, "t_last_s": None}
        self._epoch_min: int | None = None
        self._epoch_max: int | None = None
        self._epoch_n = 0
        self._dropped_closed = 0
        self._seen: set[int] = set()
        # live eye tracking (gray8 only): every frame, inline - ~3 ms per 160x160 frame
        self.tracker = None
        if self.fmt == FMT_GRAY8 and self.w >= 32 and self.h >= 32:
            from .eyetrack import EyeTracker
            self.tracker = EyeTracker(self.w, self.h)
        self._overlays: deque = deque(maxlen=64)  # (frame count, tracker output) for the preview
        self.track_error: str | None = None

    def new_epoch(self) -> None:
        """The board started its stream again (START): seq restarts at 0."""
        if self._epoch_min is not None:
            self._dropped_closed += (self._epoch_max - self._epoch_min + 1) - self._epoch_n
            self.stats["n_seq_resets"] += 1
        self._epoch_min = self._epoch_max = None
        self._epoch_n = 0
        self._seen = set()

    def update_header(self, header: dict) -> None:
        self.header = dict(header)  # e.g. new exposure after a CAM command; size/format can't change

    def add(self, fr: VideoFrame, t_s: float, t_dev_us: int) -> None:
        if fr.seq in self._seen:
            self.stats["n_duplicates"] += 1
            return
        if (fr.w, fr.h) != (self.w, self.h) or fr.fmt != self.fmt:
            self.stats["n_bad_size"] += 1
            return
        # track before publishing the frame, so a preview of frame n always has frame n's overlay
        ov = None
        if self.tracker is not None and self.track_error is None:
            try:  # on the session clock, like the PPG samples and the events
                ov = self.tracker.update(np.frombuffer(fr.data, np.uint8), t_s)
            except Exception as e:  # never let tracking break the recording
                self.track_error = f"{type(e).__name__}: {e}"
        with self.lock:
            off = self._f.tell()
            self._f.write(REC_HDR.pack(len(fr.data), fr.seq, t_dev_us, t_s, fr.exposure, fr.gain, fr.fmt))
            self._f.write(fr.data)
            self._csv.writerow([f"{t_s:.6f}", fr.seq, t_dev_us, fr.exposure, fr.gain, off, len(fr.data)])
            self.latest, self.latest_t_s = fr, t_s
            self.stats["n_frames"] += 1
            if ov is not None:
                self._overlays.append((self.stats["n_frames"], ov))
            self._new_frame.notify_all()
        self._seen.add(fr.seq)
        if len(self._seen) > 20000:
            self._seen = {q for q in self._seen if q > fr.seq - 10000}
        self._epoch_min = fr.seq if self._epoch_min is None else min(self._epoch_min, fr.seq)
        self._epoch_max = fr.seq if self._epoch_max is None else max(self._epoch_max, fr.seq)
        self._epoch_n += 1
        st = self.stats
        st["bytes"] += len(fr.data)
        st["n_dropped_seq"] = self._dropped_closed + (self._epoch_max - self._epoch_min + 1) - self._epoch_n
        if st["t_first_s"] is None:
            st["t_first_s"] = t_s
        st["t_last_s"] = t_s
        self._recent.append(t_dev_us)
        while self._recent and self._recent[0] < t_dev_us - FPS_WINDOW_S * 1e6:
            self._recent.popleft()

    def fps(self) -> float:
        """Frames per second of device time (not arrival time: a slow uplink delivers in bursts)."""
        r = self._recent
        if len(r) < 2 or r[-1] <= r[0]:
            return 0.0
        return (len(r) - 1) / ((r[-1] - r[0]) / 1e6)

    def flush(self) -> None:
        with self.lock:
            self._f.flush()
            self._idx.flush()

    def close(self) -> None:
        with self.lock:
            self._f.close()
            self._idx.close()

    def overlay(self, count: int) -> dict | None:
        """Tracker output of frame number `count` (as in preview()), else the newest before it."""
        for n, o in reversed(self._overlays):
            if n <= count:
                return o
        return None

    def eye(self, after: int = -1) -> dict | None:
        """Live eye-tracking readout: summary stats + per-frame points newer than `after`."""
        if self.tracker is None:
            return None
        pts = self.tracker.since(after)
        return {"stream": self.name, "w": self.w, "h": self.h, "error": self.track_error,
                "summary": self.tracker.summary(),
                "points": [{k: p[k] for k in ("n", "t", "state", "closure", "motion", "thr", "event", "gx", "gy",
                                               "cx", "cy", "dur") if k in p} for p in pts],
                "next": pts[-1]["n"] if pts else after}

    def recalibrate_eye(self) -> bool:
        if self.tracker is None:
            return False
        self.tracker.recalibrate()
        return True

    def preview(self, after: int = -1, wait_s: float = 0.0) -> tuple[int, bytes, str] | None:
        """(frame count, image, media type) of the newest frame, encoded once per frame. With
        `after` (the count the caller already shows), waits up to wait_s for a newer frame, so the
        live page gets every frame as it arrives without polling."""
        with self._new_frame:
            if wait_s > 0:
                self._new_frame.wait_for(lambda: self.stats["n_frames"] > after, timeout=wait_s)
            fr, n = self.latest, self.stats["n_frames"]
        if fr is None:
            return None
        p = self._preview
        if p is None or p[0] != n:
            p = self._preview = (n, *preview_image(fr))
        return p

    def live(self) -> dict:
        st = self.stats
        return {"stream": self.name, "sid": self.sid, "w": self.w, "h": self.h, "fmt": FMT_NAMES.get(self.fmt),
                "fs": float(self.header.get("fs", 0) or 0), "fps": round(self.fps(), 2), "frames": st["n_frames"],
                "dropped": st["n_dropped_seq"], "bytes": st["bytes"],
                "sensor_ok": self.header.get("sensor_ok") != "0",
                "exposure": self.latest.exposure if self.latest else None,
                "gain": self.latest.gain if self.latest else None, "tracking": self.tracker is not None}

    def summary(self) -> dict:
        st = self.stats
        dur = (st["t_last_s"] or 0) - (st["t_first_s"] or 0)
        return {"stream": self.name, "kind": "video", "sid": self.sid, "file": self.frames_path.name,
                "index": self.index_path.name, "w": self.w, "h": self.h, "fmt": FMT_NAMES.get(self.fmt),
                "header": self.header, "fs": float(self.header.get("fs", 0) or 0),
                "stats": {**st, "effective_fps": (st["n_frames"] - 1) / dur if dur > 0 and st["n_frames"] > 1 else None}}
