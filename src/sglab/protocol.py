"""Parser for PPG serial protocol v1 (see firmware/PROTOCOL.md).

    $<TYPE>,<fields...>*<CS>\\r\\n     CS = XOR of bytes between '$' and '*'
"""
from __future__ import annotations

from dataclasses import dataclass, field


def checksum(body: str) -> int:
    cs = 0
    for b in body.encode("ascii", "replace"):
        cs ^= b
    return cs


def encode(body: str) -> str:
    """Build a protocol line (used by the simulator and tests)."""
    return f"${body}*{checksum(body):02X}\r\n"


class ProtocolError(ValueError):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind  # "checksum" | "malformed"


@dataclass
class Header:
    dev: str
    fs: float
    channels: list[str]
    fw: str = ""
    proto: int = 1
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"dev": self.dev, "fs": self.fs, "channels": self.channels, "fw": self.fw,
                "proto": self.proto, **self.extra}


@dataclass
class Sample:
    seq: int
    t_us: int
    values: list[int]


@dataclass
class Event:
    t_us: int
    code: str
    text: str = ""


@dataclass
class Status:
    t_us: int
    values: dict


def _kv(fields: list[str]) -> dict:
    out = {}
    for f in fields:
        if "=" in f:
            k, v = f.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_line(raw: str):
    """Return Header | Sample | Event | Status, None for non-protocol text.

    Raises ProtocolError on bad checksum / malformed protocol lines.
    """
    s = raw.strip()
    if not s.startswith("$"):
        return None
    star = s.rfind("*")
    if star < 0 or len(s) - star - 1 != 2:
        raise ProtocolError("malformed", f"no checksum: {s[:60]!r}")
    body, cs_txt = s[1:star], s[star + 1:]
    try:
        cs = int(cs_txt, 16)
    except ValueError:
        raise ProtocolError("malformed", f"bad checksum field: {s[:60]!r}") from None
    if checksum(body) != cs:
        raise ProtocolError("checksum", f"checksum mismatch: {s[:60]!r}")

    parts = body.split(",")
    kind, fields = parts[0], parts[1:]
    try:
        if kind == "D":
            return Sample(int(fields[0]), int(fields[1]), [int(v) for v in fields[2:]])
        if kind == "H":
            kv = _kv(fields)
            hdr = Header(
                dev=kv.pop("dev"), fs=float(kv.pop("fs")), channels=kv.pop("ch").split("|"),
                fw=kv.pop("fw", ""), proto=int(kv.pop("proto", 1)), extra=kv,
            )
            return hdr
        if kind == "E":
            return Event(int(fields[0]), fields[1] if len(fields) > 1 else "",
                         ",".join(fields[2:]) if len(fields) > 2 else "")
        if kind == "S":
            return Status(int(fields[0]), _kv(fields[1:]))
    except (IndexError, KeyError, ValueError) as e:
        raise ProtocolError("malformed", f"{kind} line: {e}: {s[:60]!r}") from None
    raise ProtocolError("malformed", f"unknown type {kind!r}")
