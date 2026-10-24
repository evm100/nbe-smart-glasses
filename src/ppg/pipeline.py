"""Layer 1: raw CSV -> conditioned, quality-labelled PPG.

Per channel, the output DataFrame contains:
  <ch>_raw       resampled raw counts (unmasked; NaN only in gaps)
  <ch>_dc        baseline (low-pass) - needed for perfusion index / SpO2 R-ratio
  <ch>_ac        morphology band (default 0.5-8 Hz), inverted so systole is up
  <ch>_ac_hr     narrow HR band (default 0.5-4 Hz)
  <ch>_ac_norm   ac / dc  (dimensionless; comparable across subjects/LED currents)
  <ch>_vpg       1st derivative of ac_norm (velocity PPG)        [1/s]
  <ch>_apg       2nd derivative of ac_norm (acceleration PPG)    [1/s^2]
  <ch>_mask      why a sample was rejected (0 = ok, bit flags below)

Layer 2 (beat detection / fiducials / features) consumes exactly this.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import filters as F
from .config import Layer1Config
from .io import load_csv
from .quality import compute_quality
from .timing import resample_uniform

# bit flags for <ch>_mask
MASK_GAP = 1
MASK_SATURATED = 2
MASK_FLATLINE = 4
MASK_NO_CONTACT = 8
MASK_SHORT_SEGMENT = 16


@dataclass
class Layer1Result:
    signals: pd.DataFrame
    quality: pd.DataFrame
    meta: dict = field(default_factory=dict)

    @property
    def fs(self) -> float:
        return self.meta["timing"]["fs_used"]

    def save(self, out_dir: str | Path, stem: str = "recording") -> dict:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "signals": out_dir / f"{stem}_signals.csv",
            "quality": out_dir / f"{stem}_quality.csv",
            "meta": out_dir / f"{stem}_meta.json",
        }
        self.signals.to_csv(paths["signals"], index=False, float_format="%.7g")
        self.quality.to_csv(paths["quality"], index=False, float_format="%.6g")
        paths["meta"].write_text(json.dumps(self.meta, indent=2, default=_json_default))
        return paths


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def condition_channel(raw: np.ndarray, fs: float, cfg: Layer1Config) -> dict[str, np.ndarray]:
    a, fc = cfg.artifacts, cfg.filters
    x = raw.astype(float).copy()
    mask = np.zeros(x.size, dtype=np.int32)

    mask[~np.isfinite(x)] |= MASK_GAP
    finite = np.isfinite(x)
    sat = finite & ((x >= a.sat_frac * a.adc_max) | (x <= a.adc_min))
    mask[sat] |= MASK_SATURATED
    flat = np.zeros(x.size, dtype=bool)
    flat[finite] = F.flatline_mask(x[finite], max(2, int(round(a.flatline_s * fs))))
    mask[flat] |= MASK_FLATLINE

    if a.min_dc is not None:
        dc_rough = F.lowpass(np.where(mask == 0, x, np.nan), fs, fc.dc_cutoff_hz, order=2,
                             pad_s=fc.pad_s, min_segment_s=1.0)
        mask[np.isfinite(dc_rough) & (dc_rough < a.min_dc)] |= MASK_NO_CONTACT

    # grow artifact regions (not plain gaps) so filter edges don't ring into data
    bad_core = (mask & (MASK_SATURATED | MASK_FLATLINE | MASK_NO_CONTACT)) > 0
    grown = F.dilate_mask(bad_core, int(round(a.mask_dilate_s * fs))) & ~bad_core
    mask[grown] |= MASK_SATURATED  # treat margin like the artifact it borders

    x[mask > 0] = np.nan
    sign = -1.0 if fc.invert else 1.0
    kw = dict(pad_s=fc.pad_s, min_segment_s=fc.min_segment_s)

    dc = F.lowpass(x, fs, fc.dc_cutoff_hz, order=2, **kw)
    ac = F.bandpass(sign * x, fs, *fc.morph_band_hz, order=fc.order, **kw)
    ac_hr = F.bandpass(sign * x, fs, *fc.hr_band_hz, order=fc.order, **kw)

    short = np.isfinite(x) & ~np.isfinite(ac)
    mask[short] |= MASK_SHORT_SEGMENT

    with np.errstate(divide="ignore", invalid="ignore"):
        ac_norm = ac / dc
    vpg = F.savgol_derivative(ac_norm, fs, 1, fc.deriv_window_s, fc.deriv_polyorder)
    apg = F.savgol_derivative(ac_norm, fs, 2, fc.deriv_window_s, fc.deriv_polyorder)
    return {"raw": raw.astype(float), "dc": dc, "ac": ac, "ac_hr": ac_hr, "ac_norm": ac_norm,
            "vpg": vpg, "apg": apg, "mask": mask}


def run_layer1(df: pd.DataFrame, cfg: Layer1Config | None = None) -> Layer1Result:
    """Run Layer 1 on an already-loaded DataFrame (``t`` + channel columns)."""
    cfg = cfg or Layer1Config()
    channels = df.attrs.get("channels") or [c for c in df.columns if c != "t"]
    uni, timing = resample_uniform(df, channels, cfg.timing.fs_target, cfg.timing.max_gap_s)
    fs = timing["fs_used"]

    sig = pd.DataFrame({"t": uni["t"].to_numpy()})
    for ch in channels:
        for name, arr in condition_channel(uni[ch].to_numpy(), fs, cfg).items():
            sig[f"{ch}_{name}"] = arr

    quality = compute_quality(sig, channels, fs, cfg.quality, cfg.filters.morph_band_hz)

    primary = cfg.quality.primary_channel or channels[0]
    summary = {"primary_channel": primary, "n_windows": int(len(quality))}
    if len(quality):
        good = quality["good"].to_numpy(dtype=bool)
        summary.update(
            good_window_frac=float(good.mean()),
            median_hr_bpm=float(np.nanmedian(quality["hr_bpm"])) if good.any() else None,
        )
    for ch in channels:
        m = sig[f"{ch}_mask"].to_numpy()
        summary[f"{ch}_masked_frac"] = float((m > 0).mean())
        if f"{ch}_pi_pct" in quality and len(quality):
            summary[f"{ch}_median_pi_pct"] = float(np.nanmedian(quality[f"{ch}_pi_pct"]))
    if "ir" in channels and "red" in channels and len(quality):
        # ratio-of-ratios R = (AC/DC)_red / (AC/DC)_ir -> SpO2 calibration input (Layer 2+)
        g = quality["good"].to_numpy(dtype=bool)
        if g.any():
            r = quality.loc[g, "red_pi_pct"] / quality.loc[g, "ir_pi_pct"]
            summary["median_R_ratio"] = float(np.nanmedian(r))

    meta = {
        "source": df.attrs.get("source"),
        "input": {k: df.attrs.get(k) for k in ("time_col", "time_unit", "column_map")},
        "channels": channels,
        "timing": timing,
        "summary": summary,
        "mask_bits": {"gap": MASK_GAP, "saturated": MASK_SATURATED, "flatline": MASK_FLATLINE,
                      "no_contact": MASK_NO_CONTACT, "short_segment": MASK_SHORT_SEGMENT},
        "config": cfg.to_dict(),
    }
    return Layer1Result(sig, quality, meta)


def process_file(path: str | Path, cfg: Layer1Config | None = None, out_dir: str | Path | None = None,
                 plot: bool = True) -> Layer1Result:
    cfg = cfg or Layer1Config()
    path = Path(path)
    res = run_layer1(load_csv(path, cfg.io), cfg)
    if out_dir is not None:
        res.save(out_dir, path.stem)
        if plot:
            from .plotting import qa_figure
            qa_figure(res, Path(out_dir) / f"{path.stem}_qa.png", title=path.name)
    return res
