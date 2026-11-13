"""Auxiliary sample streams (protocol v2.1): any numeric sensor next to a board's PPG.

A board declares each extra stream with its own H line (kind=samples: a sensor's measurements;
kind=features: values the board computed itself from another stream) and sends its values as
`$M,<sid>,<seq>,<t_us>,v1,v2,...` lines (firmware/PROTOCOL.md). The server needs no code per
sensor: channel names and units come from the header, display names from config/sensors.toml.

Per stream in a session folder (written once, like every raw file):

    <role>_<dev>_<stream>.csv    t_s, seq, t_dev_us, <channels...>   (empty cell = no value)

listed under "aux" in session.json (not "streams": those are the PPG streams processing reads).
(This module is not called aux.py: AUX is a reserved file name on Windows.)
"""
from __future__ import annotations

import csv
import math
import threading
from collections import deque
from pathlib import Path

KINDS = ("samples", "features")
LIVE_S = 600.0       # history kept for the live page
LIVE_MAX = 6000


class AuxWriter:
    """One auxiliary stream of a recording (StreamWorker owns it): CSV + live state."""

    def __init__(self, folder: Path, name: str, header: dict):
        self.name, self.header = name, dict(header)
        self.sid = int(header.get("sid", 0))
        self.kind = header.get("kind", "samples")
        self.channels = [c for c in str(header.get("ch", "")).split("|") if c] or ["value"]
        units = str(header.get("unit", "")).split("|")
        self.units = [(units[i] if i < len(units) and units[i] not in ("", "-") else "") for i in range(len(self.channels))]
        scale = str(header.get("scale", "")).split("|")
        self.scale = []
        for i in range(len(self.channels)):
            try:
                self.scale.append(float(scale[i]))
            except (IndexError, ValueError):
                self.scale.append(1.0)
        self.fs = _float(header.get("fs"))
        self.csv_path = folder / f"{name}.csv"
        self._f = open(self.csv_path, "w", newline="")
        self._csv = csv.writer(self._f)
        self._csv.writerow(["t_s", "seq", "t_dev_us", *self.channels])
        self.lock = threading.Lock()
        self.live: deque = deque(maxlen=LIVE_MAX)   # (t_s, n, [values])
        self.live_n = 0
        self.last: list = [None] * len(self.channels)
        self.stats = {"n_samples": 0, "n_dropped_seq": 0, "n_duplicates": 0, "n_bad": 0, "n_seq_resets": 0,
                      "t_first_s": None, "t_last_s": None}
        self._epoch_min: int | None = None
        self._epoch_max: int | None = None
        self._epoch_n = 0
        self._dropped_closed = 0
        self._seen: set[int] = set()

    def update_header(self, header: dict) -> None:
        self.header = dict(header)  # e.g. new calibration constants; channels can't change mid-recording

    def new_epoch(self) -> None:
        """The board started its streams again (START): seq restarts at 0."""
        if self._epoch_min is not None:
            self._dropped_closed += (self._epoch_max - self._epoch_min + 1) - self._epoch_n
            self.stats["n_seq_resets"] += 1
        self._epoch_min = self._epoch_max = None
        self._epoch_n = 0
        self._seen = set()

    def add(self, seq: int, t_s: float, t_dev_us: int, values: list) -> None:
        if len(values) != len(self.channels):
            self.stats["n_bad"] += 1
            return
        if seq in self._seen:
            self.stats["n_duplicates"] += 1
            return
        self._seen.add(seq)
        if len(self._seen) > 20000:
            self._seen = {q for q in self._seen if q > seq - 10000}
        vals = [None if v is None else v * k for v, k in zip(values, self.scale)]
        self._epoch_min = seq if self._epoch_min is None else min(self._epoch_min, seq)
        self._epoch_max = seq if self._epoch_max is None else max(self._epoch_max, seq)
        self._epoch_n += 1
        st = self.stats
        with self.lock:
            self._csv.writerow([f"{t_s:.6f}", seq, t_dev_us, *["" if v is None else f"{v:.10g}" for v in vals]])
            self.live.append((t_s, self.live_n, vals))
            self.live_n += 1
            self.last = [v if v is not None else old for v, old in zip(vals, self.last)]
            st["n_samples"] += 1
            st["n_dropped_seq"] = self._dropped_closed + (self._epoch_max - self._epoch_min + 1) - self._epoch_n
            if st["t_first_s"] is None:
                st["t_first_s"] = t_s
            st["t_last_s"] = t_s
            while self.live and self.live[0][0] < t_s - LIVE_S:
                self.live.popleft()

    def flush(self) -> None:
        with self.lock:
            self._f.flush()

    def close(self) -> None:
        with self.lock:
            self._f.close()

    def describe(self) -> dict:
        h = self.header
        return {"stream": self.name, "sid": self.sid, "kind": self.kind, "name": h.get("stream", self.name),
                "sensor": h.get("sensor"), "fs": self.fs, "channels": self.channels, "units": self.units,
                "src": h.get("src"), "algo": h.get("algo"), "sensor_ok": h.get("sensor_ok") != "0"}

    def live_state(self, after: int = -1, max_points: int = 1500) -> dict:
        """For the live page: description, counters, last values and the points newer than `after`."""
        with self.lock:
            pts = [p for p in self.live if p[1] > after]
            st, last, n = dict(self.stats), list(self.last), self.live_n
        pts = pts[:: max(1, len(pts) // max_points)] if after < 0 else pts[-max_points:]
        dur = (st["t_last_s"] or 0) - (st["t_first_s"] or 0)
        return {**self.describe(), "samples": st["n_samples"], "dropped": st["n_dropped_seq"], "last": last,
                "t_last": st["t_last_s"],
                "rate_hz": round((st["n_samples"] - 1) / dur, 3) if dur > 0 and st["n_samples"] > 1 else None,
                "t": [round(p[0], 3) for p in pts], "y": [[p[2][i] for p in pts] for i in range(len(self.channels))],
                "next": n - 1}

    def summary(self) -> dict:
        st = self.stats
        dur = (st["t_last_s"] or 0) - (st["t_first_s"] or 0)
        return {**self.describe(), "file": self.csv_path.name, "header": self.header,
                "stats": {**st, "effective_rate_hz": (st["n_samples"] - 1) / dur if dur > 0 and st["n_samples"] > 1 else None}}


def _float(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def aux_streams(doc: dict) -> list[dict]:
    """Auxiliary streams of a session.json."""
    return list(doc.get("aux") or [])
