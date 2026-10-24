import json

import numpy as np
import pytest

from ppg import Layer1Config, process_file
from ppg.config import Layer2Config
from ppg.epochs import hrv
from ppg.features import _vertex
from ppg.layer2 import process_file_layer2, run_layer2
from ppg.synthetic import make_recording


@pytest.fixture(scope="module")
def drift(tmp_path_factory):
    """3-minute recording whose diastolic wave shrinks 0.6 -> 0.25 (known morphology trend)."""
    df, truth = make_recording(duration_s=180, dia_amp=(0.6, 0.25), seed=11)
    p = tmp_path_factory.mktemp("d") / "drift.csv"
    df.to_csv(p, index=False)
    l1 = process_file(p)
    return p, truth, l1, run_layer2(l1)


def _clean(t, truth):
    a, b = truth["motion"][0]
    d0, d1 = truth["dropouts"][0]
    return ~(((t > a - 2) & (t < b + 2)) | ((t > d0 - 2) & (t < d1 + 2)))


def test_peak_detection_sensitivity_and_accuracy(drift):
    _, truth, _, l2 = drift
    det = l2.beats["t_peak"].to_numpy()
    ref = truth["sys_peak_s"]
    ref = ref[_clean(ref, truth) & (ref > 2) & (ref < 178)]
    err = np.array([np.min(np.abs(det - r)) for r in ref])
    sensitivity = np.mean(err < 0.05)
    assert sensitivity > 0.97
    assert np.median(err[err < 0.05]) < 0.006   # sub-sample (fs = 100 Hz)


def test_no_false_beats_in_clean_signal(drift):
    _, truth, _, l2 = drift
    b = l2.beats
    det = b.loc[_clean(b["t_peak"].to_numpy(), truth), "t_peak"].to_numpy()
    ref = truth["sys_peak_s"]
    ppv = np.mean([np.min(np.abs(ref - d)) < 0.05 for d in det])
    assert ppv > 0.98


def test_beat_quality(drift):
    _, truth, _, l2 = drift
    b = l2.beats
    clean = _clean(b["t_foot"].to_numpy(), truth)
    assert b.loc[clean, "beat_good"].mean() > 0.95
    a, e = truth["motion"][0]
    inside = (b["t_foot"] > a + 1) & (b["t_foot"] < e - 1)
    assert not b.loc[inside, "beat_good"].any()
    assert b.loc[b.beat_good, "template_corr"].median() > 0.98


def test_morphology_feature_tracks_truth(drift):
    _, truth, _, l2 = drift
    g = l2.beats[l2.beats.beat_good]
    true_da = np.interp(g["t_foot"], truth["beat_onsets_s"], truth["dia_amp"])
    assert np.corrcoef(g["dia_rel"], true_da)[0, 1] > 0.95
    assert np.corrcoef(g["ipa"], true_da)[0, 1] > 0.9
    assert (g["notch_idx"] >= 0).mean() > 0.95


def test_hr_hrv_and_spo2_ratio(drift):
    _, truth, _, l2 = drift
    e = l2.epochs[l2.epochs.valid]
    assert abs(e["hr_mean_bpm"].median() - truth["hr_bpm"]) < 2
    true_sdnn = np.std(truth["rr_s"], ddof=1) * 1e3
    assert abs(e["sdnn_ms"].median() - true_sdnn) < 0.3 * true_sdnn
    assert abs(e["R_red_ir_med"].median() - truth["R_ratio"]) < 0.03


def test_epochs_and_templates_shapes(drift):
    _, _, _, l2 = drift
    assert len(l2.epochs) == len(l2.templates)
    assert l2.templates.shape[1] == 100
    assert l2.waves.shape == (len(l2.beats), 100)
    assert l2.epochs["valid"].mean() > 0.8
    for c in ["dia_rel_med", "dia_rel_iqr", "pw50_s_med", "rmssd_ms", "n_good"]:
        assert c in l2.epochs


def test_outputs_written(drift, tmp_path):
    p, *_ = drift
    process_file_layer2(p, out_dir=tmp_path)
    for suf in ["_beats.csv", "_epochs.csv", "_waves.npz", "_layer2_meta.json",
                "_beats.png", "_features.png", "_feature_corr.png"]:
        assert (tmp_path / f"drift{suf}").exists(), suf
    meta = json.loads((tmp_path / "drift_layer2_meta.json").read_text())
    assert meta["summary"]["n_good_beats"] > 150
    z = np.load(tmp_path / "drift_waves.npz")
    assert z["beat_waves"].shape[1] == 100


@pytest.mark.parametrize("hr", [50, 120, 165])
def test_beats_across_heart_rates(hr, tmp_path):
    df, truth = make_recording(duration_s=60, hr_bpm=hr, motion=[], dropouts=[], seed=hr)
    p = tmp_path / "r.csv"; df.to_csv(p, index=False)
    l2 = run_layer2(process_file(p))
    g = l2.beats[l2.beats.beat_good]
    assert len(g) > 0.85 * len(truth["sys_peak_s"])
    assert abs(np.median(g["hr_bpm"]) - hr) < 3


def test_glasses_like_low_perfusion(tmp_path):
    """Weak, noisy signal without a visible notch should still yield beats."""
    df, _ = make_recording(duration_s=90, channels={"IR": (40_000, 0.003)}, noise_frac=0.08,
                           dia_amp=(0.15, 0.15), motion=[], dropouts=[], seed=5)
    p = tmp_path / "g.csv"; df.to_csv(p, index=False)
    l2 = run_layer2(process_file(p))
    g = l2.beats[l2.beats.beat_good]
    assert len(g) > 80
    assert abs(np.median(g["hr_bpm"]) - 72) < 3


def test_helpers():
    y = -(np.arange(10) - 4.3) ** 2
    assert abs(_vertex(y, 4) - 4.3) < 1e-9
    ibi = np.array([0.8, 0.85, 0.8, 0.85])
    h = hrv(ibi, np.array([True, True, True, False]))
    assert np.isclose(h["rmssd_ms"], 50.0)


def test_layer2_config_roundtrip(tmp_path):
    c = Layer2Config()
    c.epochs.epoch_s = 60
    c.to_json(tmp_path / "c.json")
    assert Layer2Config.from_json(tmp_path / "c.json") == c
