"""Loading raw PPG recordings (CSV) into a tidy DataFrame.

Output contract: a DataFrame with column ``t`` (seconds, starting at 0) and
one float column per logical channel (``ir``, ``red``, ...). Provenance lives
in ``df.attrs``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import IOConfig

TIME_CANDIDATES = [
    "t", "time", "timestamp", "time_s", "time_ms", "timestamp_ms", "millis",
    "ms", "time_us", "timestamp_us", "micros", "us",
]
CHANNEL_CANDIDATES = {
    "ir": ["ir", "ir_raw", "ir_value", "infrared", "ir_led"],
    "red": ["red", "red_raw", "red_value", "red_led"],
    "green": ["green", "green_raw", "green_value", "green_led"],
}
_UNIT_SCALE = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}


def _find_col(columns, candidates):
    lookup = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    return None


def infer_time_unit(raw_t: np.ndarray) -> str:
    """Guess the unit of numeric timestamps from their typical spacing.

    Assumes the true sample rate is between 5 Hz and 5 kHz.
    """
    raw_t = raw_t[np.isfinite(raw_t)]
    d = np.median(np.diff(raw_t))
    if not d > 0:
        raise ValueError("Timestamps are not increasing; cannot infer unit.")
    for unit, scale in _UNIT_SCALE.items():
        if 1 / 5000 <= d * scale <= 1 / 5:
            return unit
    raise ValueError(
        f"Could not infer time unit (median spacing {d!r}). Set io.time_unit."
    )


def _to_seconds(series: pd.Series, unit: str) -> tuple[np.ndarray, str]:
    if unit == "datetime" or (unit == "auto" and not pd.api.types.is_numeric_dtype(series)):
        dt = pd.to_datetime(series, errors="coerce")
        t = (dt - dt.dropna().iloc[0]).dt.total_seconds().to_numpy()
        return t.astype(float), "datetime"
    raw = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    if unit == "auto":
        unit = infer_time_unit(raw)
    if unit not in _UNIT_SCALE:
        raise ValueError(f"Unknown time unit '{unit}'")
    return raw * _UNIT_SCALE[unit], unit


def load_csv(path: str | Path, cfg: IOConfig | None = None) -> pd.DataFrame:
    """Read a raw recording CSV and normalise columns/time."""
    cfg = cfg or IOConfig()
    path = Path(path)
    df = pd.read_csv(path, **cfg.read_csv_kwargs)

    # --- channels ---
    if cfg.channels:
        chan_map = dict(cfg.channels)
        missing = [c for c in chan_map.values() if c not in df.columns]
        if missing:
            raise KeyError(f"{path.name}: columns {missing} not found. Have: {list(df.columns)}")
    else:
        chan_map = {}
        for name, cands in CHANNEL_CANDIDATES.items():
            col = _find_col(df.columns, cands)
            if col is not None:
                chan_map[name] = col
        if not chan_map:
            raise KeyError(
                f"{path.name}: no PPG channel columns recognised in {list(df.columns)}. "
                "Set io.channels, e.g. {\"ir\": \"IR\"}."
            )

    # --- time ---
    time_col = cfg.time_col or _find_col(df.columns, TIME_CANDIDATES)
    if time_col is not None:
        t, unit = _to_seconds(df[time_col], cfg.time_unit)
    elif cfg.fs_nominal:
        t, unit = np.arange(len(df)) / float(cfg.fs_nominal), "index/fs_nominal"
    else:
        raise KeyError(
            f"{path.name}: no timestamp column found and io.fs_nominal not set."
        )

    out = pd.DataFrame({"t": t})
    for name, col in chan_map.items():
        out[name] = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
    out = out[np.isfinite(out["t"])].reset_index(drop=True)
    out["t"] -= out["t"].iloc[0]

    out.attrs.update(
        source=str(path), time_col=time_col, time_unit=unit, column_map=chan_map,
        channels=list(chan_map),
    )
    return out
