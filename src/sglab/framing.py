"""Binary data frames from MQTT nodes (firmware >= 0.6.0, see firmware/PROTOCOL.md "MQTT data frames").

A node's output is one byte stream of records (protocol v1 text lines, and samples in a compact
delta-coded form); a frame carries whole records starting at a stream offset. ``FrameReceiver``
turns frames back into ordinary protocol lines for the recorder and tells the hub what to answer:

    ACK <boot> <offset>      everything before offset is here (the node frees it)
    RESEND <boot> <offset>   a frame was missing: send again from offset (go-back-N)

Duplicates (the node resends after a reconnect) are dropped by offset, so the recorder sees every
record once and in order.
"""
from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass

from .protocol import ProtocolError, Sample, encode, parse_line

MAGIC = b"NB"
VERSION = 2
HDR = struct.Struct("<2sBBIQI")  # magic, version, flags, boot, offset, crc32
REC_TEXT, REC_SAMPLES = 0x01, 0x02
RESEND_EVERY_S = 1.0     # at most one RESEND per this long (frames already in flight all show the gap)
GIVE_UP_RESENDS = 5      # RESENDs without progress: the node no longer has the data, accept the gap


class FrameError(ValueError):
    pass


def is_frame(payload: bytes) -> bool:
    return payload[:2] == MAGIC


def _uvarint(b: bytes, i: int) -> tuple[int, int]:
    x = shift = 0
    while True:
        if i >= len(b) or shift > 63:
            raise FrameError("truncated varint")
        c = b[i]
        i += 1
        x |= (c & 0x7F) << shift
        if c < 0x80:
            return x, i
        shift += 7


def _zz(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def decode_samples(body: bytes) -> list[str]:
    """A samples record -> D lines."""
    if len(body) < 2:
        raise FrameError("short samples record")
    n = body[0] | body[1] << 8
    seq, i = _uvarint(body, 2)
    t, i = _uvarint(body, i)
    dt_nom, i = _uvarint(body, i)
    if i >= len(body):
        raise FrameError("short samples record")
    nch = body[i]
    i += 1
    v = []
    for _ in range(nch):
        x, i = _uvarint(body, i)
        v.append(x)
    out = [encode(f"D,{seq},{t}," + ",".join(map(str, v)))]
    for _ in range(n - 1):
        d, i = _uvarint(body, i)
        t += dt_nom + _zz(d)
        for c in range(nch):
            d, i = _uvarint(body, i)
            v[c] += _zz(d)
        seq += 1
        out.append(encode(f"D,{seq},{t}," + ",".join(map(str, v))))
    if i != len(body):
        raise FrameError("samples record length mismatch")
    return out


@dataclass
class Frame:
    boot: int
    offset: int
    records: list[tuple[int, int, bytes]]  # (stream offset, type, body)

    @property
    def end(self) -> int:
        return self.records[-1][0] + _rec_len(self.records[-1]) if self.records else self.offset


def _rec_len(r: tuple[int, int, bytes]) -> int:
    n, b = 1, len(r[2])
    while True:
        n += 1
        if b < 0x80:
            return n + len(r[2])
        b >>= 7


def decode_frame(payload: bytes) -> Frame:
    if len(payload) < HDR.size:
        raise FrameError("short frame")
    magic, ver, _flags, boot, offset, crc = HDR.unpack_from(payload)
    if magic != MAGIC or ver != VERSION:
        raise FrameError(f"not a v{VERSION} frame")
    data = payload[HDR.size:]
    if zlib.crc32(data) != crc:
        raise FrameError("CRC mismatch")
    recs, i = [], 0
    while i < len(data):
        start, kind = i, data[i]
        n, i = _uvarint(data, i + 1)
        if i + n > len(data):
            raise FrameError("record runs past the frame")
        recs.append((offset + start, kind, data[i:i + n]))
        i += n
    return Frame(boot, offset, recs)


def record_lines(kind: int, body: bytes) -> list[str]:
    if kind == REC_TEXT:
        return [body.decode("ascii", "replace") + "\r\n"]
    if kind == REC_SAMPLES:
        return decode_samples(body)
    return []  # newer record types: skip


class FrameReceiver:
    """Per-node reassembly. feed() -> (protocol lines, reply for the node's cmd topic or None)."""

    def __init__(self):
        self.boot: int | None = None
        self.expected = 0
        self.stats = {"frames": 0, "bytes": 0, "dup_bytes": 0, "gaps": 0, "lost_bytes": 0, "bad_frames": 0,
                      "boots": 0}
        self._resend_at = 0.0
        self._resends = 0

    def feed(self, payload: bytes, now: float | None = None) -> tuple[list[str], str | None]:
        now = time.monotonic() if now is None else now
        try:
            f = decode_frame(payload)
        except FrameError:
            self.stats["bad_frames"] += 1
            return [], self._resend(now)
        self.stats["frames"] += 1
        self.stats["bytes"] += len(payload)
        if f.boot != self.boot:  # node rebooted (or this server restarted): a new stream
            self.boot, self.expected, self._resends = f.boot, f.offset, 0
            self.stats["boots"] += 1
        if f.offset > self.expected:  # a frame went missing
            if self._resends < GIVE_UP_RESENDS:
                return [], self._resend(now)
            self.stats["lost_bytes"] += f.offset - self.expected
            self.expected = f.offset
        lines = []
        for rec in f.records:
            if rec[0] < self.expected:
                self.stats["dup_bytes"] += _rec_len(rec)
                continue
            try:
                lines += record_lines(rec[1], rec[2])
            except FrameError:
                self.stats["bad_frames"] += 1
            self.expected = rec[0] + _rec_len(rec)
        self._resends = 0
        return lines, f"ACK {self.boot:08x} {self.expected}"

    def _resend(self, now: float) -> str | None:
        if self.boot is None or now - self._resend_at < RESEND_EVERY_S:
            return None
        self._resend_at = now
        self._resends += 1
        self.stats["gaps"] += 1
        return f"RESEND {self.boot:08x} {self.expected}"


# ---------------------------------------------------------------- sender (simulated nodes)
def _uv(x: int) -> bytes:
    out = bytearray()
    while x >= 0x80:
        out.append(x & 0x7F | 0x80)
        x >>= 7
    out.append(x)
    return bytes(out)


def _zze(v: int) -> int:
    return (v << 1) ^ (v >> 63)


class FrameSender:
    """The node side, as in the firmware (main/link_mqtt.c): protocol lines -> records in a buffer,
    frames from the first unsent byte within a window, freed on ACK, go-back-N on reconnect /
    RESEND / ACK timeout. Used by ``sglab sim-node`` and the tests."""

    def __init__(self, boot: int | None = None, frame_max: int = 8192, window: int = 64 * 1024,
                 ack_timeout_s: float = 10.0):
        import os

        self.boot = boot if boot is not None else int.from_bytes(os.urandom(4), "little")
        self.frame_max, self.window, self.ack_timeout_s = frame_max, window, ack_timeout_s
        self.buf = bytearray()          # records from offset `base`
        self.base = self.acked = self.sent = 0
        self.ends: list[int] = []       # record end offsets (frame boundaries)
        self.progress = time.monotonic()
        self.resends = 0

    @property
    def head(self) -> int:
        return self.base + len(self.buf)

    def _append(self, rec: bytes) -> None:
        self.buf += rec
        self.ends.append(self.head)

    def add_lines(self, lines: list[str]) -> None:
        run: list[Sample] = []

        def flush():
            if run:
                nch, dt_nom = len(run[0].values), (run[1].t_us - run[0].t_us) if len(run) > 1 else 0
                body = bytearray(struct.pack("<H", len(run)) + _uv(run[0].seq) + _uv(run[0].t_us) + _uv(dt_nom)
                                 + bytes([nch]) + b"".join(_uv(v) for v in run[0].values))
                for a, b in zip(run, run[1:]):
                    body += _uv(_zze(b.t_us - a.t_us - dt_nom))
                    body += b"".join(_uv(_zze(y - x)) for x, y in zip(a.values, b.values))
                self._append(bytes([REC_SAMPLES]) + _uv(len(body)) + bytes(body))
                run.clear()

        for ln in lines:
            try:
                m = parse_line(ln)
            except ProtocolError:
                continue
            if isinstance(m, Sample) and (not run or (m.seq == run[-1].seq + 1 and len(m.values) == len(run[0].values)
                                                      and len(run) < 400)):
                run.append(m)
                continue
            flush()
            if isinstance(m, Sample):
                run.append(m)
            elif m is not None:
                b = ln.rstrip("\r\n").encode()
                self._append(bytes([REC_TEXT]) + _uv(len(b)) + b)
        flush()

    def frames(self, now: float | None = None) -> list[bytes]:
        now = time.monotonic() if now is None else now
        if self.sent > self.acked and now - self.progress > self.ack_timeout_s:
            self.go_back(now)
        out = []
        while self.sent < self.head and self.sent - self.acked < self.window:
            ends = [e for e in self.ends if self.sent < e <= self.sent + self.frame_max - HDR.size]
            end = ends[-1] if ends else next(e for e in self.ends if e > self.sent)
            data = bytes(self.buf[self.sent - self.base:end - self.base])
            if self.sent == self.acked:
                self.progress = now
            out.append(HDR.pack(MAGIC, VERSION, 0, self.boot, self.sent, zlib.crc32(data)) + data)
            self.sent = end
        return out

    def go_back(self, now: float | None = None) -> None:
        if self.sent > self.acked:
            self.resends += 1
        self.sent = self.acked
        self.progress = time.monotonic() if now is None else now

    def handle_cmd(self, text: str) -> bool:
        """ACK / RESEND from the server; False for other commands."""
        parts = text.split()
        if len(parts) != 3 or parts[0] not in ("ACK", "RESEND"):
            return False
        if int(parts[1], 16) != self.boot:
            return True
        off = int(parts[2])
        if self.acked < off <= self.head:
            self.acked, self.progress = off, time.monotonic()
            cut = off - self.base
            del self.buf[:cut]
            self.base = off
            self.ends = [e for e in self.ends if e > off]
        self.sent = max(self.sent, self.acked)
        if parts[0] == "RESEND":
            self.go_back()
        return True
