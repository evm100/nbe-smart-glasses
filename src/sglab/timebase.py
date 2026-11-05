"""Session clock: map a stream's device time onto session time (t_s, the clock of events.csv).

Two fits are stored per stream in session.json:

  clock_fit   t_s ≈ a + b·t_dev, fitted on PC receive times. Fine over USB. Over Wi-Fi/MQTT,
              buffered bursts after a lost connection tilt it: on real sessions it was off by
              up to 50 s and streams drifted seconds apart (b off by 0.1-5 %).
  epoch_fit   unix ≈ a + b·t_dev from the node's SNTP SYNC events: sub-ms residuals, b within
              ~0.2 ppm. Only MQTT nodes have it.

So a stream is mapped through epoch_fit when it has one (and the fit is clean), anchored to
the session start in unix time (t0_unix); otherwise through clock_fit.

Pipeline time t (0 = the stream's first sample, in device seconds) maps to session time as
    t_session = t0 + b·t
with t0 and b from run_clock().
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import numpy as np

# an SNTP fit whose SYNC points stray further than this from the line is not trusted
# (e.g. a reboot that broke the device-time stitching)
MAX_SYNC_RESID_MS = 50.0


def read_events(folder: Path) -> list[dict]:
    p = Path(folder) / "events.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def session_t0_unix(doc: dict, events: list[dict] | None = None) -> float | None:
    """Unix time of t_s = 0. Newer sessions store it; older ones get it from operator events
    (wall_time - t_s, ms precision), else from started_at (whole seconds, +0.5 s)."""
    if doc.get("t0_unix"):
        return float(doc["t0_unix"])
    offs = []
    for e in events or []:
        try:
            offs.append(datetime.fromisoformat(e["wall_time"]).timestamp() - float(e["t_s"]))
        except (KeyError, TypeError, ValueError):
            continue
    if offs:
        return float(np.median(offs))
    try:
        return datetime.fromisoformat(doc["started_at"]).timestamp() + 0.5
    except (KeyError, TypeError, ValueError):
        return None


def device_clock(stream: dict, t0_unix: float | None) -> tuple[float, float, str] | None:
    """(a, b, source): t_session = a + b·t_dev_s for this stream, or None if it has no fit."""
    ef = stream.get("epoch_fit")
    if ef and t0_unix is not None and (ef.get("resid_ms_max") or 0) <= MAX_SYNC_RESID_MS:
        return float(ef["a"]) - t0_unix, float(ef["b"]), "sntp"
    cf = stream.get("clock_fit")
    if cf:
        return float(cf["a"]), float(cf["b"]), "pc"
    return None


def first_sample_dev_s(summary: dict, stream: dict) -> float | None:
    """Device time of the stream's first processed sample. Runs before t0_dev_s was stored
    only kept t0_session_s, computed through clock_fit, so invert that."""
    if summary.get("t0_dev_s") is not None:
        return float(summary["t0_dev_s"])
    cf, t0 = stream.get("clock_fit"), summary.get("t0_session_s")
    if cf and t0 is not None and (stream.get("io") or {}).get("time_col", "").startswith("t_dev"):
        return (float(t0) - cf["a"]) / cf["b"]
    return None


def run_clock(stream: dict, summary: dict, t0_unix: float | None) -> tuple[float, float, str]:
    """(t0, b, source) for one processing run: t_session = t0 + b·t_pipeline."""
    clk = device_clock(stream, t0_unix)
    t_dev0 = first_sample_dev_s(summary, stream)
    if clk is None or t_dev0 is None:
        return float(summary.get("t0_session_s") or 0.0), 1.0, "t_s"
    a, b, src = clk
    return a + b * t_dev0, b, src


def session_clocks(folder: Path) -> tuple[dict, float | None]:
    """(session.json doc, t0_unix) for a session folder."""
    doc = json.loads((Path(folder) / "session.json").read_text())
    return doc, session_t0_unix(doc, read_events(folder))
