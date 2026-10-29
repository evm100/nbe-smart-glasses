"""Build Layer-3 datasets from the catalog: reference readings joined to every processed stream.

    data/datasets/<name>/
      rows.csv        one row per (reading, stream): session/subject keys, label (cuff BP or
                      SpO2), window, PPG summary of the good beats in it, pulse-arrival
                      difference vs. the finger, provenance (pipeline version, config hash)
      templates.npz   templates (n_rows x 100): ensemble-average beat per row, same order
      manifest.json   when, from what (each run used) and with which Layer-3 settings

The dataset is derived data: rebuild it any time with `sglab dataset build`. Rows whose
window has too few good beats are kept with valid = False, so coverage per site is visible;
filter on `valid` for modelling.

Settings: config/pipeline_layer3.json (ppg.config.Layer3Config; created by `sglab init`).
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import ppg
from ppg.config import Layer3Config
from ppg.layer3 import pulse_arrival, reference_readings, window_rows

from .timebase import read_events, run_clock, session_clocks

SUBJECT_COLS = ["sex", "age_years", "height_cm", "weight_kg", "fitzpatrick"]


def layer3_config(settings) -> Layer3Config:
    p = settings.root / "config" / "pipeline_layer3.json"
    return Layer3Config.from_json(p) if p.exists() else Layer3Config()


def subject_code(subject_id: str | None) -> str | None:
    """The S### code: some subject ids carry more than the code (e.g. 'S002 - name')."""
    m = re.match(r"\s*(S\d+)", subject_id or "")
    return m.group(1) if m else subject_id


def _stream_beats(settings, run: dict, st: dict, t0_unix: float | None):
    """(beats, waves, session-time foot times, clock source) for one ok run."""
    d = settings.data_root / run["out_dir"]
    stem = run["summary"]["stem"]
    p = d / f"{stem}_beats.csv"
    beats = pd.read_csv(p) if p.exists() and p.stat().st_size > 1 else pd.DataFrame()
    if len(beats):
        beats["beat_good"] = beats["beat_good"].astype(bool)
    wp = d / f"{stem}_waves.npz"
    waves = np.load(wp)["beat_waves"] if wp.exists() else np.empty((0, 100))
    if len(waves) != len(beats):  # outputs from different runs: don't pair them
        waves = np.full((len(beats), waves.shape[1] if waves.ndim == 2 else 100), np.nan)
    t0, k, src = run_clock(st, run["summary"], t0_unix)
    t_foot = t0 + k * beats["t_foot"].to_numpy(float) if len(beats) else np.array([])
    return beats, waves, t_foot, src


def session_rows(settings, catalog, s: dict, cfg: Layer3Config) -> tuple[pd.DataFrame, np.ndarray, dict]:
    """Rows + templates for one catalog session, and what was used (for the manifest)."""
    sid = s["session_id"]
    folder = settings.data_root / s["path"]
    doc, t0_unix = session_clocks(folder)
    readings = reference_readings(read_events(folder), cfg)
    used = {"session_id": sid, "n_readings": int(len(readings)), "runs": []}
    if readings.empty:
        return pd.DataFrame(), np.empty((0, 100)), used
    runs = {r["stream"]: r for r in catalog.latest_runs(sid) if r["status"] == "ok"}
    streams = [st for st in doc.get("streams", []) if st.get("stream") in runs]
    loaded = {st["stream"]: _stream_beats(settings, runs[st["stream"]], st, t0_unix) for st in streams}

    pa = cfg.pulse_arrival
    ref = next((st["stream"] for st in streams if st.get("role") == pa.ref_role), None)
    meta = doc.get("metadata") or {}
    subj = (catalog.subject(s["subject_id"]) if s.get("subject_id") else None) or {}
    keys = {"session_id": sid, "subject": subject_code(s.get("subject_id")), "protocol": s.get("protocol"),
            "trial": s.get("trial"), "posture": meta.get("posture"), "quality": s.get("quality"),
            "started_at": s.get("started_at"), **{c: subj.get(c) for c in SUBJECT_COLS}}

    parts, tpls = [], []
    for st in streams:
        name = st["stream"]
        beats, waves, t_foot, clock = loaded[name]
        pad = None
        if pa.enabled and ref and name != ref and len(beats) and len(loaded[ref][0]):
            rb, _, rt, _ = loaded[ref]
            pad = pulse_arrival(rt, rb["beat_good"].to_numpy(), t_foot, beats["beat_good"].to_numpy(), pa)
        w, tp = window_rows(readings, beats, waves, t_foot, cfg, pad)
        run = runs[name]
        prov = {"stream": name, "role": st.get("role"), "dev": st.get("dev"), "fw": st.get("fw"),
                "clock": clock, "pipeline_version": run["pipeline_version"], "config_hash": run["config_hash"]}
        part = pd.concat([pd.DataFrame([{**keys, **prov}] * len(readings), index=readings.index), readings, w],
                         axis=1)
        parts.append(part)
        tpls.append(tp)
        used["runs"].append({"stream": name, "run_id": run["run_id"], "config_hash": run["config_hash"],
                             "clock": clock})
    if not parts:
        return pd.DataFrame(), np.empty((0, 100)), used
    return pd.concat(parts, ignore_index=True), np.concatenate(tpls), used


def build(settings, catalog, name: str = "bp", cfg: Layer3Config | None = None,
          sessions: list[str] | None = None, log=print) -> dict:
    """Write data/datasets/<name>/ (replaced as a whole). Returns the manifest."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"dataset name {name!r}: use letters, digits, - _ .")
    cfg = cfg or layer3_config(settings)
    frames, tpls, used = [], [], []
    for s in sorted(catalog.sessions(), key=lambda x: x["session_id"]):
        if sessions and not any(q in s["session_id"] for q in sessions):
            continue
        rows, tp, u = session_rows(settings, catalog, s, cfg)
        if u["n_readings"]:
            used.append(u)
        if len(rows):
            frames.append(rows)
            tpls.append(tp)
            log(f"  {s['session_id']}: {u['n_readings']} reading(s) x {len(u['runs'])} stream(s), "
                f"{int(rows['valid'].sum())} valid")
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    templates = np.concatenate(tpls) if tpls else np.empty((0, 100))
    if len(df):
        df = df.copy()  # de-fragment (hundreds of feature columns)
        df.insert(0, "row", np.arange(len(df)))

    out = settings.data_root / "datasets" / name
    tmp = out.with_name(f".{name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    df.to_csv(tmp / "rows.csv", index=False, float_format="%.6g")
    np.savez_compressed(tmp / "templates.npz", templates=templates,
                        row=df["row"].to_numpy() if len(df) else np.array([], int))
    manifest = {
        "name": name, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ppg_version": ppg.__version__, "layer3": cfg.to_dict(), "summary": summarise(df),
        "sessions": used,
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return manifest


def summarise(df: pd.DataFrame) -> dict:
    """Counts per label kind and role: readings, rows, valid rows, subjects."""
    if df.empty:
        return {"n_rows": 0}
    out = {"n_rows": int(len(df)), "kinds": {}}
    for kind, g in df.groupby("kind"):
        per_role = {}
        for role, r in g.groupby("role"):
            v = r[r["valid"].astype(bool)]
            per_role[role] = {"rows": int(len(r)), "valid": int(len(v)),
                              "subjects": int(v["subject"].nunique()), "sessions": int(v["session_id"].nunique())}
        out["kinds"][kind] = {"readings": int(g[["session_id", "reading"]].drop_duplicates().shape[0]),
                              "roles": per_role}
    return out


def list_datasets(settings) -> list[dict]:
    d = settings.data_root / "datasets"
    out = []
    for m in sorted(d.glob("*/manifest.json")) if d.is_dir() else []:
        out.append(json.loads(m.read_text()))
    return out
