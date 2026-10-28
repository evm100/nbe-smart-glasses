"""Run the ppg pipeline (Layers 1-2) on catalogued sessions.

Outputs go to data/processed/<session_id>/<stream>/ and every run is logged
in the catalog with the pipeline version and a hash of the configs, so you
always know which code + settings produced a file, and re-running only
happens when something changed.

Pipeline settings: config/pipeline_layer1.json and config/pipeline_layer2.json
(created by `sglab init`). Per-stream I/O settings (columns, time units, fs)
come from session.json, so the configs stay device-independent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

import numpy as np

import ppg
from ppg.config import Layer1Config, Layer2Config

# Bump when the clean-up of the raw CSV before the pipeline (_prepared_csv) changes: it is part of
# config_hash, so existing runs count as out of date and get processed again.
PREP_VERSION = 1


def pipeline_configs(settings) -> tuple[Layer1Config, Layer2Config]:
    c1 = settings.root / "config" / "pipeline_layer1.json"
    c2 = settings.root / "config" / "pipeline_layer2.json"
    cfg1 = Layer1Config.from_json(c1) if c1.exists() else Layer1Config()
    cfg2 = Layer2Config.from_json(c2) if c2.exists() else Layer2Config()
    return cfg1, cfg2


def config_hash(cfg1: Layer1Config, cfg2: Layer2Config) -> str:
    s = json.dumps({"v": ppg.__version__, "prep": PREP_VERSION, "l1": cfg1.to_dict(), "l2": cfg2.to_dict()}, sort_keys=True, default=str)
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def process_session(settings, catalog, session_id: str, force: bool = False, plot: bool = True,
                    log=print) -> list[dict]:
    s = catalog.session(session_id)
    if s is None:
        raise KeyError(f"unknown session {session_id}")
    sid = s["session_id"]
    folder = settings.data_root / s["path"]
    doc = json.loads((folder / "session.json").read_text())
    base1, cfg2 = pipeline_configs(settings)
    h = config_hash(base1, cfg2)
    results = []
    for st in doc.get("streams", []):
        name = st.get("stream")
        if not st.get("file") or not (folder / st["file"]).exists():
            log(f"  [skip] {sid}/{name}: no data file")
            continue
        if not force and catalog.has_run(sid, name, h):
            log(f"  [up-to-date] {sid}/{name}")
            continue
        out_dir = settings.processed_dir / sid / name
        try:
            summary = process_stream(folder / st["file"], st, base1, cfg2, out_dir, plot=plot)
            catalog.add_run(sid, name, ppg.__version__, h, str(out_dir.relative_to(settings.data_root)), "ok", summary)
            s2 = summary["layer2"]
            log(f"  [ok] {sid}/{name}: {s2.get('n_good_beats', 0)}/{s2.get('n_beats', 0)} good beats, "
                f"HR {s2.get('median_hr_bpm') or float('nan'):.1f} bpm")
            results.append({"stream": name, "status": "ok", **summary})
        except Exception as e:  # record the failure, continue with other streams
            catalog.add_run(sid, name, ppg.__version__, h, str(out_dir.relative_to(settings.data_root)), "error",
                            {"error": f"{type(e).__name__}: {e}"})
            log(f"  [error] {sid}/{name}: {e}")
            results.append({"stream": name, "status": "error", "error": str(e)})
    return results


def process_stream(csv_path: Path, stream: dict, base1: Layer1Config, cfg2: Layer2Config, out_dir: Path,
                   plot: bool = True) -> dict:
    """Layers 1-2 on one stream's CSV (stream = its session.json entry); returns the run summary.
    Shared by local processing and the remote worker (sglab.worker)."""
    from ppg.layer2 import process_file_layer2

    cfg1 = copy.deepcopy(base1)
    for k, v in (stream.get("io") or {}).items():
        setattr(cfg1.io, k, v)
    if stream.get("fs") and cfg1.timing.fs_target is None:
        cfg1.timing.fs_target = float(stream["fs"])
    with tempfile.TemporaryDirectory(prefix="sglab-prep-") as tmp:
        csv_path, prep = _prepared_csv(Path(csv_path), Path(tmp))
        l1, l2 = process_file_layer2(csv_path, cfg1, cfg2, out_dir, plot=plot)
        # map pipeline time (0 = first sample) onto the session clock (t_s)
        t0 = _first_sample_session_time(csv_path, stream)
    return {"layer1": l1.meta["summary"], "layer2": l2.meta["summary"],
            "timing": l1.meta["timing"], "t0_session_s": t0, "stem": Path(csv_path).stem, "prep": prep}


def startup_glitch_rows(seq: np.ndarray, t_dev_us: np.ndarray) -> np.ndarray:
    """Rows to drop (bool mask): the bad first FIFO read after a START on firmware <= 0.6.0.

    That firmware reset the sensor's FIFO pointers at START while the sensor kept converting; sometimes
    this left OVF_COUNTER set (a FIFO_OVF event right after START), so the first read returned all 32
    FIFO slots - mostly stale samples, stamped up to 32 periods before START - and the next read
    repeated the newest of them. Signature: seq restarts at 0 on every START, and seq 32 is stamped
    before seq 31. Dropping seq 0-31 of that epoch keeps one copy of every real sample."""
    drop = np.zeros(len(seq), dtype=bool)
    for i in np.flatnonzero(seq == 0):
        if i + 32 < len(seq) and (seq[i:i + 33] == np.arange(33)).all() and t_dev_us[i + 32] < t_dev_us[i + 31]:
            drop[i:i + 32] = True
    return drop


def _prepared_csv(csv_path: Path, tmp: Path) -> tuple[Path, dict]:
    """The CSV the pipeline should read: csv_path itself, or a cleaned copy (same name) in tmp."""
    import pandas as pd

    if not {"seq", "t_dev_us"} <= set(pd.read_csv(csv_path, nrows=0).columns):
        return csv_path, {"startup_glitch_rows": 0}
    df = pd.read_csv(csv_path)
    drop = startup_glitch_rows(df["seq"].to_numpy(), df["t_dev_us"].to_numpy())
    if not drop.any():
        return csv_path, {"startup_glitch_rows": 0}
    out = tmp / csv_path.name
    df[~drop].to_csv(out, index=False)
    return out, {"startup_glitch_rows": int(drop.sum())}


def _first_sample_session_time(csv_path: Path, stream: dict) -> float:
    """Session time (t_s) of the first sample, using the device-clock fit if available."""
    import pandas as pd

    first = pd.read_csv(csv_path, nrows=1)
    fit = stream.get("clock_fit")
    if fit and "t_dev_us" in first:
        return float(fit["a"] + fit["b"] * first["t_dev_us"].iloc[0] / 1e6)
    if "t_s" in first:
        return float(first["t_s"].iloc[0])
    return 0.0
