"""Layer 2: conditioned PPG -> beats, fiducials, features, epochs.

Input : Layer1Result (signals + window quality)
Output: Layer2Result
  beats      one row per detected beat: fiducial indices (*_idx), ~40
             morphology features, Gaussian decomposition (g1..g3_*),
             beat SQI (template_corr, beat_good, beat_reason)
  epochs     one row per sliding window: feature medians/IQRs, HR, HRV,
             beat counts, `valid` flag  -> the ML feature matrix
  waves      every beat normalised to 100 points (n_beats x 100)
  templates  ensemble-average beat per epoch (n_epochs x 100)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .beat_quality import beat_quality, good_sample_mask, normalised_beats
from .beats import FIDUCIALS, find_fiducials
from .config import Layer2Config
from .epochs import aggregate_epochs
from .features import beat_features
from .gaussian import gaussian_features
from .pipeline import Layer1Result, _json_default


@dataclass
class Layer2Result:
    beats: pd.DataFrame
    epochs: pd.DataFrame
    waves: np.ndarray
    templates: np.ndarray
    meta: dict = field(default_factory=dict)

    def save(self, out_dir: str | Path, stem: str = "recording") -> dict:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "beats": out_dir / f"{stem}_beats.csv",
            "epochs": out_dir / f"{stem}_epochs.csv",
            "waves": out_dir / f"{stem}_waves.npz",
            "meta": out_dir / f"{stem}_layer2_meta.json",
        }
        self.beats.to_csv(paths["beats"], index=False, float_format="%.6g")
        self.epochs.to_csv(paths["epochs"], index=False, float_format="%.6g")
        np.savez_compressed(
            paths["waves"], beat_waves=self.waves, beat_t_foot=self.beats.get("t_foot", pd.Series(dtype=float)).to_numpy(),
            beat_good=self.beats.get("beat_good", pd.Series(dtype=bool)).to_numpy(),
            epoch_templates=self.templates, epoch_t_center=self.epochs.get("t_center", pd.Series(dtype=float)).to_numpy(),
        )
        paths["meta"].write_text(json.dumps(self.meta, indent=2, default=_json_default))
        return paths


def run_layer2(l1: Layer1Result, cfg: Layer2Config | None = None) -> Layer2Result:
    cfg = cfg or Layer2Config()
    sig, fs = l1.signals, l1.fs
    channels = l1.meta["channels"]
    ch = cfg.beats.channel or l1.meta["summary"]["primary_channel"]
    others = [c for c in channels if c != ch]

    x = sig[f"{ch}_ac_norm"].to_numpy()
    fid = find_fiducials(x, sig[f"{ch}_vpg"].to_numpy(), sig[f"{ch}_apg"].to_numpy(), fs, cfg.beats)
    n_tpl = cfg.quality.template_len

    if fid.empty:
        beats = pd.DataFrame()
        waves = np.empty((0, n_tpl))
    else:
        feats = beat_features(fid, sig, fs, ch, others)
        waves = normalised_beats(x, fid, n_tpl)
        good_l1 = good_sample_mask(len(sig), l1.quality, fs) if len(l1.quality) else None
        bq = beat_quality(feats, fid, waves, good_l1, cfg.quality)
        parts = [fid[FIDUCIALS].add_suffix("_idx"), fid[["foot_tan_s", "notch_method"]], feats]
        if cfg.gaussian.enabled:
            parts.append(gaussian_features(waves, fid, feats["ibi_s"].to_numpy(), cfg.gaussian))
        beats = pd.concat(parts + [bq], axis=1)

    duration = float(sig["t"].iloc[-1])
    epochs, templates = aggregate_epochs(beats, waves, duration, cfg.epochs) if len(beats) else (pd.DataFrame(), np.empty((0, n_tpl)))

    summary = {"channel": ch, "n_beats": int(len(beats))}
    if len(beats):
        g = beats["beat_good"].to_numpy(bool)
        summary.update(
            n_good_beats=int(g.sum()), good_beat_frac=float(g.mean()),
            notch_found_frac=float((beats["notch_idx"] >= 0).mean()),
            apg_e_found_frac=float((beats["e_idx"] >= 0).mean()),
            gauss_ok_frac=float(beats["gauss_ok"].mean()) if "gauss_ok" in beats else None,
            gauss_r2_median=float(beats["gauss_r2"].median()) if "gauss_r2" in beats else None,
            median_hr_bpm=float(np.median(beats.loc[g, "hr_bpm"])) if g.any() else None,
            reject_reasons=beats.loc[~g, "beat_reason"].str.split(";").explode().value_counts().to_dict(),
        )
    if len(epochs):
        summary["n_epochs"] = int(len(epochs))
        summary["n_valid_epochs"] = int(epochs["valid"].sum())

    meta = {"source": l1.meta.get("source"), "fs": fs, "summary": summary, "config": cfg.to_dict()}
    return Layer2Result(beats, epochs, waves, templates, meta)


def process_file_layer2(path: str | Path, cfg1=None, cfg2: Layer2Config | None = None,
                        out_dir: str | Path | None = None, plot: bool = True):
    """Raw CSV -> Layer 1 -> Layer 2. Returns (Layer1Result, Layer2Result)."""
    from .pipeline import process_file

    path = Path(path)
    l1 = process_file(path, cfg1, out_dir, plot=plot)
    l2 = run_layer2(l1, cfg2)
    if out_dir is not None:
        l2.save(out_dir, path.stem)
        if plot:
            from .plotting2 import layer2_figures
            layer2_figures(l1, l2, out_dir, path.stem, title=path.name)
    return l1, l2
