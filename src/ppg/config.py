"""Configuration for Layer 1 (signal conditioning).

Every tunable lives here so a run is fully described by one JSON file.
Dump the defaults with:  ppg-layer1 config > my_config.json
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Optional


@dataclass
class IOConfig:
    # Column holding timestamps. None -> auto-detect from common names.
    time_col: Optional[str] = None
    # "auto" | "s" | "ms" | "us" | "ns" | "datetime"
    time_unit: str = "auto"
    # Logical channel name -> CSV column, e.g. {"ir": "IR", "red": "RED"}.
    # Empty -> auto-detect ir/red/green columns.
    channels: dict = field(default_factory=dict)
    # Only used when the file has no timestamp column.
    fs_nominal: Optional[float] = None
    # Extra kwargs passed to pandas.read_csv (e.g. {"sep": ";", "comment": "#"}).
    read_csv_kwargs: dict = field(default_factory=dict)


@dataclass
class TimingConfig:
    # Output sample rate. None -> estimate from timestamps (rounded to an
    # integer Hz when within 3%).
    fs_target: Optional[float] = None
    # Holes in the timestamps longer than this are NOT interpolated; they
    # become NaN and split the signal into independent segments.
    max_gap_s: float = 0.25


@dataclass
class ArtifactConfig:
    # MAX30101/30102 ADC is 18-bit.
    adc_max: float = 2**18 - 1
    # Samples >= sat_frac * adc_max (or <= adc_min) count as saturated.
    sat_frac: float = 0.995
    adc_min: float = 0.0
    # Run of identical values at least this long = flatline (sensor stuck).
    flatline_s: float = 0.10
    # DC below this (counts) = no tissue contact. None disables the check;
    # set it per device once you know typical on/off-skin levels.
    min_dc: Optional[float] = None
    # Grow every invalid region by this much on each side.
    mask_dilate_s: float = 0.25


@dataclass
class FilterConfig:
    # Raw MAX3010x counts DROP when blood volume rises; invert so the
    # systolic peak points up (conventional PPG orientation).
    invert: bool = True
    # Low-pass cutoff for the DC (baseline) component.
    dc_cutoff_hz: float = 0.4
    # Narrow band -> robust heart-rate work.
    hr_band_hz: tuple = (0.5, 4.0)
    # Wider band -> keeps waveform morphology (dicrotic notch etc.) for BP features.
    morph_band_hz: tuple = (0.5, 8.0)
    order: int = 4
    # Edge padding for zero-phase filtering (seconds).
    pad_s: float = 2.0
    # Contiguous valid segments shorter than this are discarded.
    min_segment_s: float = 4.0
    # Savitzky-Golay settings for VPG (1st) / APG (2nd) derivatives.
    deriv_window_s: float = 0.09
    deriv_polyorder: int = 3


@dataclass
class QualityConfig:
    # Channel used for the overall good/bad decision. None -> first channel.
    primary_channel: Optional[str] = None
    window_s: float = 8.0
    step_s: float = 2.0
    hr_min_bpm: float = 40.0
    hr_max_bpm: float = 200.0
    # --- thresholds for "good" windows (starting heuristics; calibrate
    #     against your reference finger PPG) ---
    min_valid_frac: float = 0.95
    min_acf_peak: float = 0.5
    max_hr_disagreement_bpm: float = 6.0
    min_skew: float = 0.0
    amp_ratio_range: tuple = (0.33, 3.0)
    min_spec_purity: float = 0.3


@dataclass
class Layer1Config:
    io: IOConfig = field(default_factory=IOConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    artifacts: ArtifactConfig = field(default_factory=ArtifactConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)

    # ---- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str | Path | None = None) -> str:
        s = json.dumps(self.to_dict(), indent=2)
        if path is not None:
            Path(path).write_text(s)
        return s

    @classmethod
    def from_dict(cls, d: dict) -> "Layer1Config":
        return _build(cls, d or {})

    @classmethod
    def from_json(cls, path: str | Path) -> "Layer1Config":
        return cls.from_dict(json.loads(Path(path).read_text()))


def _build(dc_type, d: dict):
    kwargs = {}
    valid = {f.name: f for f in fields(dc_type)}
    for key, val in d.items():
        if key not in valid:
            raise KeyError(f"Unknown config key '{key}' for {dc_type.__name__}")
        default = valid[key].default_factory() if callable(valid[key].default_factory) else valid[key].default  # type: ignore[misc]
        if is_dataclass(default) and isinstance(val, dict):
            kwargs[key] = _build(type(default), val)
        elif isinstance(default, tuple) and isinstance(val, list):
            kwargs[key] = tuple(val)
        else:
            kwargs[key] = val
    return dc_type(**kwargs)


# =============================================================================
# Layer 2: beats, fiducials, features
# =============================================================================
@dataclass
class BeatDetectConfig:
    # Channel to segment beats on. None -> Layer 1 primary channel.
    channel: Optional[str] = None
    # Elgendi et al. 2013 two-moving-average systolic-peak detector.
    w_peak_s: float = 0.111      # ~ systolic-peak duration
    w_beat_s: float = 0.667      # ~ one beat
    beta: float = 0.02           # offset as fraction of mean squared signal
    refractory_s: float = 0.30   # min spacing between peaks (caps HR at 200 bpm)
    # How far before a systolic peak to look for the foot (onset).
    foot_search_s: float = 0.50
    # Min prominence (fraction of beat amplitude) for a dicrotic notch dip.
    notch_min_prominence: float = 0.01
    # Search for the notch only in this first fraction of the beat (foot -> next foot).
    notch_search_frac: float = 0.65


@dataclass
class BeatQualityConfig:
    template_len: int = 100            # samples per normalised beat
    template_neighbors: int = 15       # beats either side used for the local template
    min_template_corr: float = 0.86    # Orphanidou et al. 2015 style threshold
    ibi_ratio_range: tuple = (0.7, 1.3)   # vs. local median IBI
    amp_ratio_range: tuple = (0.5, 2.0)   # vs. local median amplitude
    hr_min_bpm: float = 40.0
    hr_max_bpm: float = 200.0
    require_layer1_good: bool = True   # beat must lie inside a good Layer-1 window
    require_notch: bool = False        # glasses PPG often has no visible notch


@dataclass
class EpochConfig:
    epoch_s: float = 30.0
    step_s: float = 10.0
    min_good_beats: int = 10


@dataclass
class GaussianConfig:
    enabled: bool = True
    # 3 = forward (systolic), early reflected, diastolic wave
    n_components: int = 3
    # fits explaining less of the beat's shape than this give NaN features
    min_r2: float = 0.98
    # component widths, as a fraction of the beat duration
    sigma_min: float = 0.015
    sigma_max: float = 0.30
    # minimum spacing between component centres (fraction of beat, ~40 ms at 72 bpm)
    min_sep: float = 0.05
    # small penalty pulling the middle component(s) to zero when the beat doesn't need them
    ridge_middle: float = 0.01
    # multi-start: stop trying further start points once a fit is this good
    r2_good_enough: float = 0.9995
    max_nfev: int = 200


@dataclass
class Layer2Config:
    beats: BeatDetectConfig = field(default_factory=BeatDetectConfig)
    quality: BeatQualityConfig = field(default_factory=BeatQualityConfig)
    epochs: EpochConfig = field(default_factory=EpochConfig)
    gaussian: GaussianConfig = field(default_factory=GaussianConfig)

    to_dict = Layer1Config.to_dict
    to_json = Layer1Config.to_json

    @classmethod
    def from_dict(cls, d: dict) -> "Layer2Config":
        return _build(cls, d or {})

    @classmethod
    def from_json(cls, path: str | Path) -> "Layer2Config":
        return cls.from_dict(json.loads(Path(path).read_text()))


# =============================================================================
# Layer 3: reference readings + PPG windows -> datasets
# =============================================================================
@dataclass
class CuffConfig:
    # A `bp` result belongs to the last `cuff_start` at most this long before it (repeated
    # presses count once: the last one wins). A reading takes ~30-90 s on our cuff.
    max_cuff_s: float = 150.0
    # No cuff_start found: assume inflation began this long before the result.
    default_cuff_s: float = 60.0
    # PPG window = [cuff_start - pre_s, result + post_s]: the cuff measures during deflation,
    # and BP moves fast after exercise, so the window hugs the measurement.
    pre_s: float = 15.0
    post_s: float = 0.0


@dataclass
class SpO2Config:
    # A pulse-oximeter reading (`ox`) is typed after the display settles: average before it.
    pre_s: float = 20.0
    post_s: float = 0.0


@dataclass
class PulseArrivalConfig:
    enabled: bool = True
    # good beats of each stream are matched to this role's good beats (same heartbeat)
    ref_role: str = "finger"
    max_lag_s: float = 0.6      # search range for the session's typical offset
    tol_s: float = 0.05         # a beat matches if within this of offset + reference beat


@dataclass
class Layer3Config:
    cuff: CuffConfig = field(default_factory=CuffConfig)
    spo2: SpO2Config = field(default_factory=SpO2Config)
    pulse_arrival: PulseArrivalConfig = field(default_factory=PulseArrivalConfig)
    # a row is `valid` with at least this many good beats in its window
    min_good_beats: int = 10

    to_dict = Layer1Config.to_dict
    to_json = Layer1Config.to_json

    @classmethod
    def from_dict(cls, d: dict) -> "Layer3Config":
        return _build(cls, d or {})

    @classmethod
    def from_json(cls, path: str | Path) -> "Layer3Config":
        return cls.from_dict(json.loads(Path(path).read_text()))


# =============================================================================
# Partial overrides (per-site configs, tuner trials)
# =============================================================================
def deep_merge(base: dict, over: dict) -> dict:
    """Copy of base with over's values on top; nested dicts merge, everything else is replaced."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def with_overrides(cfg, over: dict | None):
    """cfg (Layer1Config / Layer2Config) with a partial dict applied, e.g. {"quality": {"min_skew": -1}}."""
    return cfg if not over else type(cfg).from_dict(deep_merge(cfg.to_dict(), over))


def set_path(d: dict, path: str, value) -> None:
    """Set a dotted path in a nested dict, creating levels as needed: set_path(d, "quality.min_skew", -1)."""
    *head, last = path.split(".")
    for k in head:
        d = d.setdefault(k, {})
    d[last] = value
