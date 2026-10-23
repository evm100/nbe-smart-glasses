"""Operator commands typed while a recording is running.

    bp 120/80 [72]   cuff reading: systolic/diastolic [pulse]   (also: bp 120 80 72)
    c                cuff inflation started (pairs with the next bp entry)
    ox 98 [72]       pulse-oximeter reading: SpO2 % [HR]
    m <text>         free-text marker, e.g. "m start cold pressor"
    ?  or Enter      show live status
    q                stop recording
    help             this list
"""
from __future__ import annotations

import re

HELP = __doc__.split("\n", 2)[2]


class CommandError(ValueError):
    pass


def _f(x: str, name: str, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except ValueError:
        raise CommandError(f"{name} must be a number, got {x!r}") from None
    if not lo <= v <= hi:
        raise CommandError(f"{name}={v:g} outside plausible range {lo:g}-{hi:g}")
    return v


def parse_command(text: str) -> tuple[str, dict]:
    """Return (action, payload). action in: event, status, stop, help."""
    t = text.strip()
    if t in ("", "?", "s", "status"):
        return "status", {}
    low = t.lower()
    if low in ("q", "quit", "stop", "exit"):
        return "stop", {}
    if low in ("h", "help"):
        return "help", {}
    if low in ("c", "cuff"):
        return "event", {"kind": "cuff_start", "label": "cuff inflation started"}

    head, _, rest = t.partition(" ")
    head = head.lower()
    if head in ("m", "mark", "marker"):
        if not rest.strip():
            raise CommandError("marker needs text, e.g. 'm start exercise'")
        return "event", {"kind": "marker", "label": rest.strip()}
    if head == "bp":
        nums = re.split(r"[\s/]+", rest.strip())
        nums = [n for n in nums if n]
        if len(nums) not in (2, 3):
            raise CommandError("use: bp SYS/DIA [PULSE], e.g. bp 118/76 70")
        sys_ = _f(nums[0], "systolic", 50, 260)
        dia = _f(nums[1], "diastolic", 25, 160)
        if dia >= sys_:
            raise CommandError("diastolic must be below systolic")
        p = {"kind": "bp", "sys_mmhg": sys_, "dia_mmhg": dia}
        if len(nums) == 3:
            p["hr_bpm"] = _f(nums[2], "pulse", 25, 240)
        return "event", p
    if head in ("ox", "spo2"):
        nums = rest.split()
        if len(nums) not in (1, 2):
            raise CommandError("use: ox SPO2 [HR], e.g. ox 98 72")
        p = {"kind": "spo2", "spo2_pct": _f(nums[0], "SpO2", 50, 100)}
        if len(nums) == 2:
            p["hr_bpm"] = _f(nums[1], "HR", 25, 240)
        return "event", p
    raise CommandError(f"unknown command {t!r} – type 'help'")


EVENT_KINDS = ("bp", "cuff_start", "spo2", "marker")


def validate_event(kind: str, values: dict) -> dict:
    """Same checks as the terminal commands, for events coming from the web UI."""
    v = {k: x for k, x in (values or {}).items() if x not in (None, "")}
    if kind == "bp":
        text = f"bp {v.get('sys_mmhg', '')}/{v.get('dia_mmhg', '')} {v.get('hr_bpm', '')}"
    elif kind == "spo2":
        text = f"ox {v.get('spo2_pct', '')} {v.get('hr_bpm', '')}"
    elif kind == "marker":
        text = f"m {v.get('label', '')}"
    elif kind == "cuff_start":
        text = "c"
    else:
        raise CommandError(f"unknown event kind {kind!r}")
    action, payload = parse_command(text.strip())
    return payload
