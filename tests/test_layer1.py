import json

import numpy as np
import pandas as pd
import pytest

from ppg import Layer1Config, load_csv, process_file, run_layer1
from ppg.filters import bandpass, contiguous_runs, flatline_mask
from ppg.io import infer_time_unit
from ppg.synthetic import make_recording
from ppg.timing import resample_uniform


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    df, truth = make_recording(duration_s=120, hr_bpm=72, seed=1)
    p = tmp_path_factory.mktemp("data") / "rec.csv"
    df.to_csv(p, index=False)
    return p, truth


# ---------------- io / timing ----------------
@pytest.mark.parametrize("unit,scale", [("s", 1), ("ms", 1e3), ("us", 1e6)])
def test_infer_time_unit(unit, scale):
    t = np.arange(500) / 100.0 * scale
    assert infer_time_unit(t) == unit


def test_load_autodetect(demo):
    p, _ = demo
    df = load_csv(p)
    assert set(df.attrs["channels"]) == {"ir", "red"}
    assert df.attrs["time_unit"] == "ms"
    assert df["t"].iloc[0] == 0


def test_load_explicit_mapping(tmp_path):
    p = tmp_path / "x.csv"
    pd.DataFrame({"sample": range(300), "LED1": np.random.rand(300)}).to_csv(p, index=False)
    cfg = Layer1Config.from_dict({"io": {"channels": {"green": "LED1"}, "fs_nominal": 50}})
    df = load_csv(p, cfg.io)
    assert list(df.columns) == ["t", "green"]
    assert np.isclose(df["t"].iloc[-1], 299 / 50)


def test_resample_marks_gaps_and_dups():
    t = np.concatenate([np.arange(0, 5, 0.01), np.arange(6, 10, 0.01)])
    t = np.insert(t, 10, t[10])  # duplicate timestamp
    df = pd.DataFrame({"t": t, "ir": np.sin(t)})
    out, rep = resample_uniform(df, ["ir"], max_gap_s=0.25)
    assert rep["fs_used"] == 100 and rep["n_gaps"] == 1 and rep["n_duplicate_timestamps"] == 1
    gap = (out["t"] > 4.995) & (out["t"] < 5.995)
    assert out.loc[gap, "ir"].isna().all()
    assert out.loc[~gap, "ir"].notna().all()


# ---------------- filters ----------------
def test_bandpass_passes_cardiac_rejects_drift():
    fs = 100
    t = np.arange(0, 30, 1 / fs)
    x = np.sin(2 * np.pi * 1.2 * t) + 5 * np.sin(2 * np.pi * 0.05 * t) + 2 * np.sin(2 * np.pi * 20 * t)
    y = bandpass(x, fs, 0.5, 8.0)
    core = slice(5 * fs, -5 * fs)
    ref = np.sin(2 * np.pi * 1.2 * t)
    assert np.corrcoef(y[core], ref[core])[0, 1] > 0.99
    # zero phase: no lag between output and the 1.2 Hz component
    lag = np.argmax(np.correlate(y[core], ref[core], "full")) - (len(ref[core]) - 1)
    assert abs(lag) <= 1


def test_bandpass_respects_nan_segments():
    x = np.random.randn(3000)
    x[1000:1050] = np.nan
    x[2900:] = np.nan
    y = bandpass(x, 100, 0.5, 8.0, min_segment_s=4.0)
    assert np.isnan(y[1000:1050]).all()
    assert np.isfinite(y[:1000]).all() and np.isfinite(y[1050:2900]).all()
    short = x.copy(); short[200:] = np.nan  # 2 s segment < min_segment_s
    assert np.isnan(bandpass(short, 100, 0.5, 8.0, min_segment_s=4.0)).all()


def test_helpers():
    assert contiguous_runs(np.array([0, 1, 1, 0, 1], bool)) == [(1, 3), (4, 5)]
    m = flatline_mask(np.array([1, 2, 3, 3, 3, 3, 4, 5.0]), 4)
    assert m.tolist() == [False, False, True, True, True, True, False, False]


# ---------------- full pipeline ----------------
def test_pipeline_hr_and_quality(demo):
    p, truth = demo
    res = process_file(p)
    q = res.quality
    assert res.fs == 100
    # heart rate recovered on good windows
    assert abs(res.meta["summary"]["median_hr_bpm"] - truth["hr_bpm"]) < 2
    assert np.nanmax(np.abs(q.loc[q.good, "hr_bpm"] - truth["hr_bpm"])) < 8
    # every window fully inside the motion burst is rejected
    a, b = truth["motion"][0]
    inside = (q.t_start >= a) & (q.t_end <= b)
    assert inside.any() and not q.loc[inside, "good"].any()
    # clean windows far from artifacts are mostly accepted
    clean = q.t_end < a - 1
    assert q.loc[clean, "good"].mean() > 0.9
    # ratio of ratios ~ truth (perfusion RED/IR)
    assert abs(res.meta["summary"]["median_R_ratio"] - truth["R_ratio"]) < 0.05


def test_inversion_gives_positive_skew(demo):
    p, _ = demo
    q = process_file(p).quality
    assert q.loc[q.good, "ir_skew"].median() > 0.3


def test_saturation_masked(tmp_path):
    df, _ = make_recording(duration_s=60, saturation=[(20, 23)], motion=[], dropouts=[], seed=3)
    p = tmp_path / "sat.csv"; df.to_csv(p, index=False)
    res = process_file(p)
    s = res.signals
    region = (s.t > 20.1) & (s.t < 22.9)
    assert (s.loc[region, "ir_mask"] & 2).all()
    assert s.loc[region, "ir_ac"].isna().all()
    assert s.loc[s.t > 30, "ir_ac"].notna().all()


def test_outputs_written(demo, tmp_path):
    p, _ = demo
    process_file(p, out_dir=tmp_path)
    for suffix in ["_signals.csv", "_quality.csv", "_meta.json", "_qa.png"]:
        assert (tmp_path / f"rec{suffix}").exists()
    meta = json.loads((tmp_path / "rec_meta.json").read_text())
    assert meta["timing"]["fs_used"] == 100


def test_config_roundtrip(tmp_path):
    cfg = Layer1Config()
    cfg.filters.morph_band_hz = (0.4, 10.0)
    cfg.quality.primary_channel = "red"
    cfg.to_json(tmp_path / "c.json")
    cfg2 = Layer1Config.from_json(tmp_path / "c.json")
    assert cfg2 == cfg
    with pytest.raises(KeyError):
        Layer1Config.from_dict({"filters": {"typo": 1}})


@pytest.mark.parametrize("hr", [48, 110, 160])
def test_hr_range(hr, tmp_path):
    df, truth = make_recording(duration_s=60, hr_bpm=hr, motion=[], dropouts=[], seed=hr)
    p = tmp_path / "r.csv"; df.to_csv(p, index=False)
    res = process_file(p)
    assert abs(res.meta["summary"]["median_hr_bpm"] - hr) < 3


def test_low_sample_rate(tmp_path):
    df, _ = make_recording(duration_s=60, fs=25, motion=[], dropouts=[], seed=7)
    p = tmp_path / "r.csv"; df.to_csv(p, index=False)
    res = process_file(p)
    assert res.fs == 25
    assert abs(res.meta["summary"]["median_hr_bpm"] - 72) < 3
