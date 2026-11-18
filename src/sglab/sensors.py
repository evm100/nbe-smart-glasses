"""The sensor / insight registry (config/sensors.toml): what the platform measures and what it
turns the measurements into. Drives the Platform page and the display names and units of
auxiliary streams (sglab.auxstream) on the live and session pages.

Nothing here is needed to *record* a new sensor: a board describes its streams itself
(firmware/PROTOCOL.md, protocol v2.1). The registry adds the human side: a name, a family,
where the project stands with it.
"""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

STATUS = {"live": "In use", "pilot": "In validation", "ready": "Platform ready", "planned": "Planned"}
STATUS_HELP = {
    "live": "in routine use",
    "pilot": "works end to end, still being validated - treat its numbers with care",
    "ready": "the platform can already record and show it (try it with a simulated device); the sensor is not on a board yet",
    "planned": "on the roadmap",
}


def load(root: Path) -> dict:
    """{"sensors": [...], "insights": [...]} from config/sensors.toml ({} lists if the file is missing)."""
    p = Path(root) / "config" / "sensors.toml"
    doc = tomllib.loads(p.read_text()) if p.exists() else {}
    sensors = [dict(s) for s in doc.get("sensor", []) if s.get("id")]
    insights = [dict(i) for i in doc.get("insight", []) if i.get("id")]
    for x in sensors + insights:
        x.setdefault("status", "planned")
        x.setdefault("name", x["id"])
    return {"sensors": sensors, "insights": insights}


def channel_index(reg: dict) -> dict[str, dict]:
    """channel name (lower case) -> sensor entry, for auxiliary streams."""
    return {c.lower(): s for s in reg["sensors"] for c in s.get("channels", [])}


def label_channels(reg: dict, aux: dict) -> list[dict]:
    """Display info per channel of an auxiliary stream description (AuxWriter.describe / session.json "aux")."""
    idx = channel_index(reg)
    out = []
    for i, ch in enumerate(aux.get("channels") or []):
        s = idx.get(ch.lower())
        unit = (aux.get("units") or [])[i] if i < len(aux.get("units") or []) else ""
        out.append({"channel": ch, "name": s["name"] if s else ch.replace("_", " "), "unit": unit or (s or {}).get("unit", ""),
                    "sensor_id": s["id"] if s else None, "family": (s or {}).get("family"),
                    "range": (s or {}).get("range")})
    return out


def seen_in_session(doc: dict, reg: dict) -> set[str]:
    """Sensor ids present in one session.json."""
    ids = set()
    if doc.get("streams"):
        ids.add("ppg")
    if doc.get("video") or any(st.get("video") for st in doc.get("streams", [])):
        ids.add("eye_camera")
    idx = channel_index(reg)
    for a in doc.get("aux") or []:
        for ch in a.get("channels") or []:
            if ch.lower() in idx:
                ids.add(idx[ch.lower()]["id"])
    return ids
