"""Append-only record log on the gateway's disk: the local raw copy AND the send buffer.

Everything the board sends goes in here first, as the same records it arrived as (wire.py).
The uplink sends from it and frees nothing until the server has acknowledged it, so a dropped
internet connection, a server restart or a gateway restart only delays data.

    <data_dir>/<NODE>/state.json                 {"boot": ..., "acked": ...}
    <data_dir>/<NODE>/seg_<first offset>.bin     records, back to back (64 MB per file)

Offsets count bytes since this log's `boot` (a random id, new only when the log is started from
scratch), exactly like the firmware's MQTT link, so the server's FrameReceiver can't tell a
gateway from a Wi-Fi node. Old acknowledged segments are deleted once the log is over keep_gb.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .wire import WireError, read_uvarint

SEGMENT_BYTES = 64 * 1024 * 1024


class RecordLog:
    def __init__(self, folder: Path, keep_gb: float = 20.0):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_bytes = int(keep_gb * 1024 ** 3)
        self.lock = threading.Lock()
        self.ends: List[int] = []           # record end offsets after `acked` (frame boundaries)
        self.segments: List[Tuple[int, Path]] = []
        self._w = None                      # current segment, open for appending
        self._w_start = 0
        self._readers = {}
        self._dirty_state = False
        self._last_state_write = 0.0
        st = self._load_state()
        self.boot: int = st["boot"]
        self.acked: int = st["acked"]
        self._open_segments()

    # ---------------------------------------------------------------- startup
    def _load_state(self) -> dict:
        p = self.dir / "state.json"
        try:
            st = json.loads(p.read_text())
            return {"boot": int(st["boot"]), "acked": int(st["acked"])}
        except (OSError, ValueError, KeyError):
            st = {"boot": int.from_bytes(os.urandom(4), "little"), "acked": 0}
            for old in self.dir.glob("seg_*.bin"):  # segments without state: a different stream
                old.unlink()
            self._write_state(st)
            return st

    def _write_state(self, st: Optional[dict] = None) -> None:
        st = st or {"boot": self.boot, "acked": self.acked}
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.dir / "state.json")
        self._last_state_write = time.monotonic()
        self._dirty_state = False

    def _open_segments(self) -> None:
        segs = []
        for p in self.dir.glob("seg_*.bin"):
            try:
                segs.append((int(p.stem[4:]), p))
            except ValueError:
                continue
        segs.sort()
        self.segments = segs
        if not segs:
            self.head = self.acked
            self._new_segment(self.acked)
            return
        last_start, last = segs[-1]
        self.head = last_start + last.stat().st_size
        if self.acked > self.head:  # state ahead of the files (deleted by hand): start over there
            self.acked = self.head
        self._index_from(self.acked)
        self._w_start = last_start
        self._w = open(last, "ab")

    def _index_from(self, start: int) -> None:
        """Rebuild record boundaries from `start` to the end (cuts a record torn by a crash)."""
        data = self._read_raw(start, self.head - start)
        i, ends = 0, []
        while i < len(data):
            try:
                n, j = read_uvarint(data, i + 1)
            except WireError:
                break
            if j + n > len(data):
                break
            i = j + n
            ends.append(start + i)
        if start + i < self.head:  # torn tail: truncate the last segment
            seg_start, seg = self.segments[-1]
            keep = start + i - seg_start
            if keep >= 0:
                with open(seg, "r+b") as f:
                    f.truncate(keep)
                self.head = start + i
        self.ends = ends

    def _new_segment(self, start: int) -> None:
        if self._w:
            self._w.close()
        p = self.dir / f"seg_{start:016d}.bin"
        self._w = open(p, "ab")
        self._w_start = start
        self.segments.append((start, p))

    # ---------------------------------------------------------------- writing
    def append(self, records: bytes, ends_rel: List[int]) -> None:
        """Whole records; ends_rel = each record's end, relative to the start of `records`."""
        if not records:
            return
        with self.lock:
            if self.head - self._w_start >= SEGMENT_BYTES:
                self._new_segment(self.head)
            self._w.write(records)
            base = self.head
            self.ends.extend(base + e for e in ends_rel)
            self.head += len(records)

    def flush(self) -> None:
        with self.lock:
            if self._w:
                self._w.flush()
            if self._dirty_state and time.monotonic() - self._last_state_write > 1.0:
                self._write_state()

    # ---------------------------------------------------------------- reading
    def _read_raw(self, start: int, n: int) -> bytes:
        out = bytearray()
        for i, (s0, p) in enumerate(self.segments):
            s1 = self.segments[i + 1][0] if i + 1 < len(self.segments) else None
            if s1 is not None and s1 <= start:
                continue
            if s0 > start + n or len(out) >= n:
                break
            f = self._readers.get(p)
            if f is None:
                f = self._readers[p] = open(p, "rb")
            f.seek(max(0, start + len(out) - s0))
            out += f.read(n - len(out))
        return bytes(out)

    def read(self, start: int, max_bytes: int) -> Tuple[bytes, int]:
        """Whole records from `start` (a record boundary): at most max_bytes, but at least one
        record. Returns (data, end offset); empty when nothing is there yet."""
        with self.lock:
            if self._w:
                self._w.flush()
            end = start
            for e in self.ends:
                if e <= start:
                    continue
                if e - start > max_bytes and end > start:
                    break
                end = e
                if e - start >= max_bytes:
                    break
            if end == start:
                return b"", start
            return self._read_raw(start, end - start), end

    # ---------------------------------------------------------------- acknowledgement
    def ack(self, offset: int) -> bool:
        """The server has everything before offset. False if it isn't a record boundary we know."""
        with self.lock:
            if offset <= self.acked or offset > self.head:
                return offset <= self.acked
            self.acked = offset
            k = 0
            while k < len(self.ends) and self.ends[k] <= offset:
                k += 1
            del self.ends[:k]
            self._dirty_state = True
            self._prune()
            return True

    def _prune(self) -> None:
        """Delete the oldest fully acknowledged segments while over keep_bytes (lock held)."""
        total = sum(p.stat().st_size for _, p in self.segments if p.exists())
        while len(self.segments) > 1 and total > self.keep_bytes:
            s0, p = self.segments[0]
            s1 = self.segments[1][0]
            if s1 > self.acked:
                break
            f = self._readers.pop(p, None)
            if f:
                f.close()
            try:
                total -= p.stat().st_size
                p.unlink()
            except OSError:
                pass
            self.segments.pop(0)

    # ---------------------------------------------------------------- info
    def unacked(self) -> int:
        return self.head - self.acked

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for _, p in self.segments if p.exists())

    def close(self) -> None:
        with self.lock:
            if self._w:
                self._w.close()
                self._w = None
            for f in self._readers.values():
                f.close()
            self._readers.clear()
            self._write_state()
