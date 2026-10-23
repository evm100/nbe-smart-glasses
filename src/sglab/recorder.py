"""Capture one or more serial streams into a session folder.

Session folder (while recording: data/raw/_incoming/<stamp>/, afterwards
data/raw/<session_id>/):

    session.json            all metadata, device headers, stream statistics
    events.csv              operator entries (cuff BP, pulse-ox, markers) + device events
    <role>_<dev>.csv        one row per sample: t_s, seq, t_dev_us, <channels...>
    <role>_<dev>.rawlog     every line exactly as received, with PC receive time

Time base: ``t_s`` = seconds since the session started, from the PC's monotonic
clock. Every stream and every event uses it, which is what lets us line up the
glasses, the finger sensor and the cuff readings later.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .protocol import Event, Header, ProtocolError, Sample, Status, parse_line
from .sources import open_source

# auto-restart: a stream that stops delivering samples gets START again (node rebooted, Wi-Fi
# came back after the server's last will stopped it, a START/STOP got lost, ...)
IDLE_RESTART_S = 3.0     # node says it is idle (MQTT status) and no samples for this long
STALL_RESTART_S = 20.0   # no samples for this long, whatever the node says...
BACKLOG_RESTART_S = 120.0  # ...unless its live status says it is streaming: its data is late, not gone
INFO_EVERY_S = 5.0       # samples but no H header yet (lost): ask for it again this often
RESTART_EVERY_S = 10.0   # at most one automatic START per this many seconds
RATE_WINDOW_S = 5        # live sample rate: complete seconds of device time it averages

EVENT_COLUMNS = ["t_s", "wall_time", "kind", "source", "sys_mmhg", "dia_mmhg", "hr_bpm", "spo2_pct", "label"]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(s)).strip("-") or "x"


class StreamWorker(threading.Thread):
    """Reads one source in a background thread and writes CSV + raw log."""

    def __init__(self, source, role: str, folder: Path, t0_ns: int):
        super().__init__(daemon=True, name=f"stream-{role}")
        self.src, self.role, self.folder, self.t0_ns = source, role, folder, t0_ns
        self.stop_flag = threading.Event()
        self.header_event = threading.Event()
        self.header: Header | None = None
        self.stream_name: str | None = None
        self.csv_path: Path | None = None
        self.raw_path = folder / f"{_safe(role)}.rawlog.tmp"
        self.device_events: list[dict] = []
        self.status: dict = {}
        self.error: str | None = None
        self.stats = {"n_lines": 0, "n_samples": 0, "n_dropped_seq": 0, "n_seq_resets": 0,
                      "n_out_of_order": 0, "n_duplicates": 0,
                      "n_checksum_err": 0, "n_malformed": 0, "n_text_lines": 0, "n_pre_header": 0,
                      "n_auto_restarts": 0, "n_device_restarts": 0, "n_device_reboots": 0,
                      "t_first_s": None, "t_last_s": None}
        self.last_values: list[int] = []
        self.live: deque = deque(maxlen=2000)  # (t_s, first channel, n) for live plots
        self.live_n = 0  # n = running point number, the cursor for incremental live updates
        self._last_seq: int | None = None
        self._seq_min: int | None = None
        self._seq_max: int | None = None
        self._seq_base = 0
        self._seen_recent: set[int] = set()
        self._n_starts = 0         # START events from the device (the first one opens the stream)
        self._dev_off_us = 0       # added to device times after a reboot, keeps t_dev_us monotonic
        self._last_dev_us: int | None = None
        self._last_sample_mono = time.monotonic()
        self._last_start_mono = 0.0
        self._rate_mark = (time.monotonic(), 0)
        self.rate_hz = 0.0
        self._dev_bins: dict[int, int] = {}  # samples per second of device time, see _device_rate()
        # lower envelope of (host - device) time per 1-s bin of device time: the
        # least-delayed sample in each second (USB: ~1 ms; MQTT: last line of a batch)
        self._clock: dict[int, tuple[float, float, float]] = {}  # bin -> (offset, t_dev_s, t_host_s)
        self._sync: list[tuple[float, float]] = []  # (t_dev_s, unix_s) from $E SYNC (SNTP-synced nodes)
        self._csv = None
        self._writer = None

    # -- helpers ----------------------------------------------------------
    def _now(self) -> float:
        return (time.monotonic_ns() - self.t0_ns) / 1e9

    def _open_csv(self, hdr: Header):
        self.stream_name = f"{_safe(self.role)}_{_safe(hdr.dev)}"
        self.csv_path = self.folder / f"{self.stream_name}.csv"
        self._csv = open(self.csv_path, "w", newline="")
        self._writer = csv.writer(self._csv)
        self._writer.writerow(["t_s", "seq", "t_dev_us", *hdr.channels])

    def _handle(self, line: str, t: float):
        try:
            msg = parse_line(line)
        except ProtocolError as e:
            self.stats["n_checksum_err" if e.kind == "checksum" else "n_malformed"] += 1
            return
        if msg is None:
            self.stats["n_text_lines"] += 1
            return
        if isinstance(msg, Header):
            if self.header is None:
                self.header = msg
                self.live = deque(maxlen=int(max(msg.fs, 1) * 12))
                self._open_csv(msg)
                self.header_event.set()
            elif msg.channels != self.header.channels:
                self.error = f"channel list changed mid-recording: {msg.channels}"
            return
        if isinstance(msg, Sample):
            if self.header is None:
                self.stats["n_pre_header"] += 1
                now = time.monotonic()
                if now - getattr(self, "_last_info_mono", -INFO_EVERY_S) >= INFO_EVERY_S:
                    self._last_info_mono = now
                    self.src.write("INFO")
                return
            if len(msg.values) != len(self.header.channels):
                self.stats["n_malformed"] += 1
                return
            # loss accounting that tolerates re-ordering (MQTT redelivery after a
            # Wi-Fi drop can bring old batches late): dropped = span - unique seen
            if msg.seq in self._seen_recent:
                self.stats["n_duplicates"] += 1
                return
            self._seen_recent.add(msg.seq)
            if len(self._seen_recent) > 50000:
                self._seen_recent = {q for q in self._seen_recent if q > msg.seq - 25000}
            if self._last_seq is not None and msg.seq < self._last_seq:
                if self._last_seq - msg.seq > 100000:  # device restarted its counter
                    self.stats["n_seq_resets"] += 1
                    self._seq_base += self._seq_max - self._seq_min + 1
                    self._seq_min = self._seq_max = None
                else:
                    self.stats["n_out_of_order"] += 1
            self._seq_min = msg.seq if self._seq_min is None else min(self._seq_min, msg.seq)
            self._seq_max = msg.seq if self._seq_max is None else max(self._seq_max, msg.seq)
            self._last_seq = msg.seq
            span = self._seq_base + (self._seq_max - self._seq_min + 1)
            self.stats["n_dropped_seq"] = max(0, span - (self.stats["n_samples"] + 1))
            t_us = msg.t_us + self._dev_off_us
            self._writer.writerow([f"{t:.6f}", msg.seq, t_us, *msg.values])
            self.stats["n_samples"] += 1
            if self.stats["t_first_s"] is None:
                self.stats["t_first_s"] = t
            self.stats["t_last_s"] = t
            self.last_values = msg.values
            self._last_sample_mono = time.monotonic()
            self._last_dev_us = t_us if self._last_dev_us is None else max(self._last_dev_us, t_us)
            dev_s = t_us / 1e6
            k, off = int(dev_s), t - dev_s
            cur = self._clock.get(k)
            if cur is None or off < cur[0]:
                self._clock[k] = (off, dev_s, t)
            self._dev_bins[k] = self._dev_bins.get(k, 0) + 1
            # live plot on the device clock (+ recent least delay), so batched links (MQTT:
            # 20-80 samples arrive at once) don't draw staircases
            ref = min((self._clock[j][0] for j in range(k - 5, k + 1) if j in self._clock), default=off)
            self.live.append((dev_s + ref, msg.values[0], self.live_n))
            self.live_n += 1
            return
        if isinstance(msg, Event):
            if msg.code == "START":
                self._on_device_start(msg.t_us, t)
            if msg.code == "SYNC":  # device clock <-> UTC (ms) from SNTP, for cross-node alignment
                try:
                    self._sync.append(((msg.t_us + self._dev_off_us) / 1e6, float(msg.text) / 1e3))
                except ValueError:
                    pass
                return
            self.device_events.append({"t_s": t, "kind": "device", "source": self.header.dev if self.header else self.role,
                                       "label": f"{msg.code} {msg.text}".strip(), "t_dev_us": msg.t_us + self._dev_off_us})
            return
        if isinstance(msg, Status):
            self.status = msg.values

    def _on_device_start(self, t_us: int, t: float) -> None:
        """The device (re)started its stream: seq starts again at 0. If its clock went
        backwards it rebooted; shift its times onto the old device timeline (via the PC clock)
        so t_dev_us stays monotonic and one clock fit still covers the whole recording."""
        self._n_starts += 1
        if self._n_starts == 1:
            return
        self.stats["n_device_restarts"] += 1
        if self._seq_min is not None:  # close this seq epoch: its loss stays counted, the gap is not
            self._seq_base += self._seq_max - self._seq_min + 1
            self._seq_min = self._seq_max = self._last_seq = None
            self._seen_recent = set()
        if self._last_dev_us is not None and t_us + self._dev_off_us < self._last_dev_us - 1_000_000:
            self.stats["n_device_reboots"] += 1
            recent = sorted(self._clock)[-5:]
            ref = min(self._clock[k][0] for k in recent) if recent else t - self._last_dev_us / 1e6
            self._dev_off_us = int(round((t - ref) * 1e6)) - t_us

    def _maybe_restart(self) -> None:
        """Send START again when the stream has gone quiet (see IDLE_RESTART_S / STALL_RESTART_S)."""
        now = time.monotonic()
        quiet = now - self._last_sample_mono
        idle = getattr(self.src, "idle", None)
        if now - self._last_start_mono < RESTART_EVERY_S:
            return
        # a START while the node is still streaming clears its sensor FIFO and restarts seq: only
        # worth it when the node stopped, not when a slow link delivers its samples late
        streaming = getattr(self.src, "node_streaming", lambda: False)()
        stall = BACKLOG_RESTART_S if streaming else STALL_RESTART_S
        if not (quiet >= stall or (idle is not None and idle.is_set() and quiet >= IDLE_RESTART_S)):
            return
        self._last_start_mono = now
        if idle is not None:
            idle.clear()
        self.stats["n_auto_restarts"] += 1
        self.device_events.append({"t_s": self._now(), "kind": "device", "source": "sglab",
                                   "label": f"AUTO_RESTART no samples for {quiet:.0f} s - sent START"})
        self.src.write("START")

    def _device_rate(self) -> float | None:
        """Samples per second of device time, over the last RATE_WINDOW_S complete seconds.

        Counting by PC arrival time swings with the link's batching (MQTT bursts: ~0 samples in one
        second, ~800 in the next); the sensor timestamps don't, so this is the sensor's real rate
        minus real losses. None until a complete second has arrived."""
        if not self._dev_bins:
            return None
        newest = max(self._dev_bins)  # still filling
        first = min(self._dev_bins)   # the stream started part-way through it
        for k in [k for k in self._dev_bins if k < newest - 60]:
            del self._dev_bins[k]
        full = range(max(first + 1, newest - RATE_WINDOW_S), newest)
        return sum(self._dev_bins.get(k, 0) for k in full) / len(full) if len(full) else None

    def stall_s(self) -> float:
        return time.monotonic() - self._last_sample_mono

    # -- thread -----------------------------------------------------------
    def run(self):
        try:
            with open(self.raw_path, "w") as raw:
                self.src.write("START")
                self._last_start_mono = self._last_sample_mono = time.monotonic()
                last_flush = time.monotonic()
                while not self.stop_flag.is_set():
                    self._read_one(raw)
                    self._maybe_restart()
                    now = time.monotonic()
                    if now - last_flush > 1.0:
                        raw.flush()
                        if self._csv:
                            self._csv.flush()
                        m_t, m_n = self._rate_mark
                        dev_rate = self._device_rate()
                        self.rate_hz = dev_rate if dev_rate is not None else (self.stats["n_samples"] - m_n) / (now - m_t)
                        self._rate_mark = (now, self.stats["n_samples"])
                        last_flush = now
                self.src.write("STOP")
                end = time.monotonic() + 0.3
                while time.monotonic() < end:  # drain what's left
                    self._read_one(raw)
        except Exception as e:  # keep the session alive, report later
            self.error = f"{type(e).__name__}: {e}"
        finally:
            if self._csv:
                self._csv.close()
            self.src.close()

    def _read_one(self, raw):
        line = self.src.readline()
        if not line:
            return
        t = self._now()
        self.stats["n_lines"] += 1
        raw.write(f"{t:.6f}\t{line.rstrip()}\n")
        self._handle(line, t)

    # -- results ----------------------------------------------------------
    def clock_fit(self) -> dict | None:
        """t_s ≈ a + b * t_dev_s, fitted on the per-second lower envelope (least link delay).

        latency_* = how far the per-second minima scatter around the fit; a large
        spread (Wi-Fi) means the PC clock is only a rough reference and cross-device
        alignment should use epoch_fit (SNTP) instead.

        The first and last seconds are partial (the stream starts / stops part-way through them),
        so their minima come from a few samples and sit above the envelope; on a bursty link
        that tilts a short fit by several %. Like _device_rate, they're left out (once 3 full remain)."""
        if len(self._clock) < 3:
            return None
        c = np.array([v for _, v in sorted(self._clock.items())])
        if len(c) >= 5:
            c = c[1:-1]
        dev, host = c[:, 1], c[:, 2]
        b, a = np.polyfit(dev, host, 1)
        resid = host - (a + b * dev)
        return {"a": float(a), "b": float(b), "n_bins": int(len(c)),
                "envelope_resid_ms_p50": float(np.median(np.abs(resid)) * 1e3),
                "envelope_resid_ms_p99": float(np.percentile(np.abs(resid), 99) * 1e3)}

    def epoch_fit(self) -> dict | None:
        """unix_s ≈ a + b * t_dev_s from the node's SNTP SYNC events (None for USB devices)."""
        if len(self._sync) < 2:
            return None
        c = np.array(self._sync)
        b, a = np.polyfit(c[:, 0], c[:, 1], 1)
        resid = c[:, 1] - (a + b * c[:, 0])
        return {"a": float(a), "b": float(b), "n": int(len(c)), "resid_ms_max": float(np.max(np.abs(resid)) * 1e3)}

    def summary(self) -> dict:
        h = self.header
        dur = (self.stats["t_last_s"] or 0) - (self.stats["t_first_s"] or 0)
        return {
            "stream": self.stream_name or _safe(self.role), "role": self.role, "port": self.src.port,
            "file": self.csv_path.name if self.csv_path else None,
            "rawlog": self.raw_path.name.replace(".tmp", ""),
            "header": h.as_dict() if h else None,
            "fs": h.fs if h else None, "channels": h.channels if h else [],
            "dev": h.dev if h else None, "fw": h.fw if h else None,
            "stats": {**self.stats, "effective_rate_hz": self.stats["n_samples"] / dur if dur > 0 else None},
            "clock_fit": self.clock_fit(),
            "epoch_fit": self.epoch_fit(),
            "device_status": self.status,   # last S line from the firmware (fs_meas, ovf, temp_c...)
            "error": self.error,
            # how ppg.io should read this CSV
            "io": {"time_col": "t_dev_us", "time_unit": "us",
                   "channels": {c: c for c in (h.channels if h else [])}},
        }


class Recording:
    def __init__(self, settings, devices: list[dict], sim_kw: dict | None = None):
        self.settings, self.devices = settings, devices
        self.sim_kw = sim_kw or {}
        self.workers: list[StreamWorker] = []
        self.events: list[dict] = []
        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        self.folder: Path | None = None
        self.t0_ns = 0
        self.subject_snapshot: dict | None = None  # copied into session.json

    def now(self) -> float:
        return (time.monotonic_ns() - self.t0_ns) / 1e9

    def start(self, pre_meta: dict | None = None) -> list[str]:
        """Open every device, send START, wait for headers. Returns warnings."""
        self.started_at = datetime.now(timezone.utc)
        stamp = self.started_at.astimezone().strftime("%Y%m%d_%H%M%S")
        self.settings.incoming_dir.mkdir(parents=True, exist_ok=True)
        for i in range(100):  # two recordings started in the same second (e.g. two PCs/terminals)
            self.folder = self.settings.incoming_dir / (stamp if i == 0 else f"{stamp}_{i}")
            try:
                self.folder.mkdir()
                break
            except FileExistsError:
                continue
        self.t0_ns = time.monotonic_ns()
        warnings = []
        roles = set()
        for d in self.devices:
            role = d.get("role", "dev")
            while role in roles:
                role += "_b"
            roles.add(role)
            try:
                src = open_source(d["port"], self.settings.baud, **{**self.sim_kw, **d.get("sim", {})})
            except Exception:
                # e.g. port busy (another program has it open) or unplugged: undo and re-raise
                for w in self.workers:
                    w.src.close()
                self.workers = []
                shutil.rmtree(self.folder, ignore_errors=True)
                raise
            w = StreamWorker(src, role, self.folder, self.t0_ns)
            self.workers.append(w)
        for w in self.workers:
            w.start()
        for w in self.workers:
            if not w.header_event.wait(self.settings.start_timeout_s):
                warnings.append(f"{w.role} ({w.src.port}): no H header after START – check firmware/baud")
        self._write_session_json({"status": "recording", **(pre_meta or {})})
        return warnings

    def add_event(self, kind: str, **values) -> dict:
        ev = {"t_s": round(self.now(), 3), "wall_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "kind": kind, "source": "operator", **values}
        self.events.append(ev)
        return ev

    def live_status(self) -> list[dict]:
        out = []
        for w in self.workers:
            out.append({"role": w.role, "dev": w.header.dev if w.header else "?", "rate_hz": w.rate_hz,
                        "fs": w.header.fs if w.header else None, "samples": w.stats["n_samples"],
                        "dropped": w.stats["n_dropped_seq"], "bad_lines": w.stats["n_checksum_err"] + w.stats["n_malformed"],
                        "last": w.last_values, "error": w.error, "alive": w.is_alive(),
                        "stall_s": round(w.stall_s(), 1), "auto_restarts": w.stats["n_auto_restarts"],
                        "device_restarts": w.stats["n_device_restarts"],
                        "device": dict(w.status)})
        return out

    def stop(self) -> None:
        for w in self.workers:
            w.stop_flag.set()
        for w in self.workers:
            w.join(timeout=5)
        self.ended_at = datetime.now(timezone.utc)
        for w in self.workers:  # rawlog: name it after the device now that we know it
            final = self.folder / f"{w.stream_name or _safe(w.role)}.rawlog"
            if w.raw_path.exists():
                w.raw_path.rename(final)
                w.raw_path = final

    def all_events(self) -> list[dict]:
        dev_events = [e for w in self.workers for e in w.device_events]
        return sorted(self.events + dev_events, key=lambda e: e["t_s"])

    def _write_session_json(self, meta: dict, final: bool = False) -> dict:
        doc = {
            "schema": 1,
            "session_id": meta.get("session_id"),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "started_at_local": self.started_at.astimezone().isoformat(timespec="seconds"),
            "ended_at": self.ended_at.isoformat(timespec="seconds") if self.ended_at else None,
            "duration_s": (self.ended_at - self.started_at).total_seconds() if self.ended_at else None,
            "metadata": {k: v for k, v in meta.items() if k not in ("session_id", "status")},
            "status": meta.get("status", "ok"),
            "subject": self.subject_snapshot,
            "reference_devices": self.settings.lab.get("reference", {}),
            "streams": [w.summary() for w in self.workers] if final else [],
            "n_events": len(self.all_events()),
        }
        (self.folder / "session.json").write_text(json.dumps(doc, indent=2, default=str))
        return doc

    def finalize(self, meta: dict) -> tuple[Path, dict]:
        """Write events + session.json and move the folder to data/raw/<session_id>."""
        with open(self.folder / "events.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for e in self.all_events():
                w.writerow(e)
        sid = make_session_id(self.started_at, meta)
        meta = {**meta, "session_id": sid}
        meta.setdefault("status", "ok" if meta.get("keep", True) else "discard")
        doc = self._write_session_json(meta, final=True)
        dest = self.settings.raw_dir / sid
        if dest.exists():
            dest = self.settings.raw_dir / f"{sid}_{int(time.time())}"
            doc["session_id"] = dest.name
            (self.folder / "session.json").write_text(json.dumps(doc, indent=2, default=str))
        shutil.move(str(self.folder), dest)
        self.folder = dest
        return dest, doc


def make_session_id(started_at: datetime, meta: dict) -> str:
    stamp = started_at.astimezone().strftime("%Y%m%d_%H%M%S")
    subj = _safe(meta.get("subject_id", "UNK"))
    proto = _safe(meta.get("protocol", "na"))
    trial = meta.get("trial")
    t = f"_T{int(trial):02d}" if isinstance(trial, (int, float)) or str(trial).isdigit() else ""
    return f"{stamp}_{subj}_{proto}{t}"
