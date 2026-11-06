"""Byte formats shared by the firmware, the USB gateway and the server (firmware/PROTOCOL.md).

    record    type u8 | body length (uvarint) | body
                0x01 text     one protocol line "$...*CS" (no CR/LF)
                0x02 samples  compact delta-coded D samples
                0x03 frame    sid u8 | seq u32 | t_us u64 | w u16 | h u16 | fmt u8 | exposure u16 | gain u8 | data
    envelope  0xA5 0x5A | len u32 | crc32 u32 (of the records) | records      USB binary link (board -> PC)
    NB frame  'N' 'B' | ver 2 | flags 0 | boot u32 | offset u64 | crc32 u32 | records    MQTT data topic

Standard library only, Python >= 3.8: this module ships inside the gateway bundle.
"""
from __future__ import annotations

import struct
import zlib
from typing import Iterator, List, NamedTuple, Optional, Tuple, Union

REC_TEXT, REC_SAMPLES, REC_FRAME = 0x01, 0x02, 0x03
FMT_GRAY8, FMT_JPEG = 0, 1
FMT_NAMES = {FMT_GRAY8: "gray8", FMT_JPEG: "jpeg"}

ENV_MAGIC = b"\xa5\x5a"
ENV_HDR = struct.Struct("<2sII")          # magic, len, crc32
ENV_MAX = 1024 * 1024                 # larger = a false magic match (largest frame: 640x480 = 307 kB): resync

NB_MAGIC = b"NB"
NB_VERSION = 2
NB_HDR = struct.Struct("<2sBBIQI")        # magic, version, flags, boot, offset, crc32

FRAME_HDR = struct.Struct("<BIQHHBHB")    # sid, seq, t_us, w, h, fmt, exposure, gain  (21 bytes)
_PRINTABLE = bytes(range(0x20, 0x7F)) + b"\t\r\n"


class WireError(ValueError):
    pass


# ---------------------------------------------------------------- varints
def uvarint(x: int) -> bytes:
    out = bytearray()
    while x >= 0x80:
        out.append(x & 0x7F | 0x80)
        x >>= 7
    out.append(x)
    return bytes(out)


def read_uvarint(b, i: int) -> Tuple[int, int]:
    """(value, next index); WireError if truncated."""
    x = shift = 0
    while True:
        if i >= len(b) or shift > 63:
            raise WireError("truncated varint")
        c = b[i]
        i += 1
        x |= (c & 0x7F) << shift
        if c < 0x80:
            return x, i
        shift += 7


# ---------------------------------------------------------------- records
def record(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + uvarint(len(body)) + body


def text_record(line: str) -> bytes:
    return record(REC_TEXT, line.rstrip("\r\n").encode("ascii", "replace"))


def split_records(data, base: int = 0) -> List[Tuple[int, int, bytes]]:
    """[(offset of the record, type, body)] for a run of whole records."""
    out, i = [], 0
    while i < len(data):
        start, kind = i, data[i]
        n, i = read_uvarint(data, i + 1)
        if i + n > len(data):
            raise WireError("record runs past the end")
        out.append((base + start, kind, bytes(data[i:i + n])))
        i += n
    return out


class VideoFrame(NamedTuple):
    sid: int
    seq: int
    t_us: int
    w: int
    h: int
    fmt: int
    exposure: int
    gain: int
    data: bytes

    @property
    def fmt_name(self) -> str:
        return FMT_NAMES.get(self.fmt, str(self.fmt))


def decode_frame_record(body: bytes) -> VideoFrame:
    if len(body) < FRAME_HDR.size:
        raise WireError("short frame record")
    sid, seq, t_us, w, h, fmt, exp, gain = FRAME_HDR.unpack_from(body)
    data = bytes(body[FRAME_HDR.size:])
    if fmt == FMT_GRAY8 and len(data) != w * h:
        raise WireError(f"gray8 frame {w}x{h} with {len(data)} bytes")
    return VideoFrame(sid, seq, t_us, w, h, fmt, exp, gain, data)


def frame_record(f: VideoFrame) -> bytes:
    return record(REC_FRAME, FRAME_HDR.pack(f.sid, f.seq, f.t_us, f.w, f.h, f.fmt, f.exposure, f.gain) + f.data)


# ---------------------------------------------------------------- USB envelopes
def envelope(records: bytes) -> bytes:
    return ENV_HDR.pack(ENV_MAGIC, len(records), zlib.crc32(records)) + records


class EnvelopeDecoder:
    """Splits the USB byte stream into envelopes (-> records) and plain text lines.

    Text outside envelopes (boot messages, an older text-protocol board) comes out line by line, so
    one decoder serves both kinds of firmware. A corrupt or cut-short envelope (the board gave up
    writing mid-frame because the PC stopped reading) is skipped by searching for the next magic;
    `stats` counts it.
    """

    def __init__(self):
        self.buf = bytearray()
        self.stats = {"envelopes": 0, "bytes": 0, "bad_crc": 0, "skipped_bytes": 0, "text_lines": 0}

    def feed(self, data: bytes) -> Iterator[Union[Tuple[str, bytes], Tuple[str, str]]]:
        """Yields ("records", bytes) and ("text", line)."""
        self.buf += data
        b = self.buf
        while b:
            if b[0] == 0xA5:
                if len(b) < ENV_HDR.size:
                    if len(b) >= 2 and b[1] != 0x5A:
                        self._skip(1)
                        continue
                    return
                magic, n, crc = ENV_HDR.unpack_from(b)
                if magic != ENV_MAGIC or n > ENV_MAX:
                    self._skip(1)
                    continue
                if len(b) < ENV_HDR.size + n:
                    return
                recs = bytes(b[ENV_HDR.size:ENV_HDR.size + n])
                if zlib.crc32(recs) != crc:
                    self.stats["bad_crc"] += 1
                    self._skip(1)
                    continue
                del b[:ENV_HDR.size + n]
                self.stats["envelopes"] += 1
                self.stats["bytes"] += ENV_HDR.size + n
                yield "records", recs
                continue
            # text up to the next newline or envelope magic
            nl = b.find(b"\n")
            mg = b.find(ENV_MAGIC)
            if nl < 0 and mg < 0:
                if len(b) > 4096:  # binary noise without structure
                    self._skip(len(b) - 1)
                return
            if mg >= 0 and (nl < 0 or mg < nl):
                if mg:
                    self._skip(mg)
                continue
            raw = bytes(b[:nl + 1])
            if raw.translate(None, _PRINTABLE):  # binary: the rest of an envelope cut short, not text
                self._skip(nl + 1)
                continue
            del b[:nl + 1]
            line = raw.decode("ascii").strip()
            if line:
                self.stats["text_lines"] += 1
                yield "text", line

    def _skip(self, n: int) -> None:
        del self.buf[:n]
        self.stats["skipped_bytes"] += n


# ---------------------------------------------------------------- NB frames (MQTT)
def nb_frame(boot: int, offset: int, records: bytes) -> bytes:
    return NB_HDR.pack(NB_MAGIC, NB_VERSION, 0, boot, offset, zlib.crc32(records)) + records


def parse_nb_frame(payload: bytes) -> Optional[Tuple[int, int, bytes]]:
    """(boot, offset, records) or None if not a valid frame."""
    if len(payload) < NB_HDR.size:
        return None
    magic, ver, _flags, boot, offset, crc = NB_HDR.unpack_from(payload)
    data = payload[NB_HDR.size:]
    if magic != NB_MAGIC or ver != NB_VERSION or zlib.crc32(data) != crc:
        return None
    return boot, offset, data


# ---------------------------------------------------------------- protocol lines
def checksum(body: str) -> int:
    cs = 0
    for c in body.encode("ascii", "replace"):
        cs ^= c
    return cs


def line(body: str) -> str:
    return "${}*{:02X}".format(body, checksum(body))


def line_body(raw: str) -> Optional[str]:
    """Body of a valid "$...*CS" line, else None."""
    s = raw.strip()
    star = s.rfind("*")
    if not s.startswith("$") or star < 0 or len(s) - star != 3:
        return None
    try:
        if int(s[star + 1:], 16) != checksum(s[1:star]):
            return None
    except ValueError:
        return None
    return s[1:star]
