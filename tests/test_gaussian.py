"""Gaussian pulse decomposition (ppg.gaussian) and its use in Layer 2."""
import numpy as np
import pytest

from ppg import process_file
from ppg.config import GaussianConfig, Layer2Config
from ppg.gaussian import fit_beat_multistart, gauss_sum, _initial, _canonical
from ppg.layer2 import run_layer2
from ppg.synthetic import make_recording


def test_fit_recovers_known_components():
    t = np.linspace(0, 1, 100)
    true = np.array([1.0, 0.20, 0.06, 0.30, 0.31, 0.05, 0.45, 0.48, 0.10])
    y = gauss_sum(t, true)
    y = (y - y.min()) / (y.max() - y.min())
    cfg = GaussianConfig()
    p, r2 = fit_beat_multistart(y, cfg, [_initial(3, 0.2, 0.38, 0.48), _canonical(3, 0.2)], t)
    assert r2 > 0.999
    p = p.reshape(3, 3)
    assert np.allclose(p[:, 1], true.reshape(3, 3)[:, 1], atol=0.03)            # centres
    ratios = p[1:, 0] / p[0, 0]
    assert np.allclose(ratios, [0.30, 0.45], atol=0.08)                         # relative heights


def _run(tmp_path, **kw):
    df, truth = make_recording(duration_s=180, motion=[], dropouts=[], seed=5, **kw)
    path = tmp_path / "r.csv"
    df.to_csv(path, index=False)
    return run_layer2(process_file(path)), truth


def _corr(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    return np.corrcoef(a[m], b[m])[0, 1]


def test_g3_tracks_diastolic_wave(tmp_path):
    l2, truth = _run(tmp_path, dia_amp=(0.6, 0.25))
    e = l2.epochs[l2.epochs.valid]
    true_da = np.interp(e.t_center, truth["beat_onsets_s"], truth["dia_amp"])
    assert l2.beats["gauss_ok"].mean() > 0.9
    assert _corr(e["g3_amp_ratio_med"].to_numpy(), true_da) > 0.95
    # g3/g1 is on the same scale as the true diastolic/systolic height ratio
    assert abs(e["g3_amp_ratio_med"].mean() - true_da.mean()) < 0.1


def test_g2_tracks_late_systolic_wave(tmp_path):
    l2, truth = _run(tmp_path, dia_amp=(0.4, 0.4), late_amp=(0.1, 0.5))
    e = l2.epochs[l2.epochs.valid]
    true_la = np.interp(e.t_center, truth["beat_onsets_s"], truth["late_amp"])
    assert _corr(e["g2_amp_ratio_med"].to_numpy(), true_la) > 0.85


def test_notch_does_not_jump_to_trough(tmp_path):
    """Regression: when a late-systolic wave fills in the notch, the fiducial
    reflection index used to collapse to ~0 (notch found in the end-diastolic trough)."""
    l2, _ = _run(tmp_path, dia_amp=(0.6, 0.3), late_amp=(0.1, 0.45))
    b = l2.beats[l2.beats.beat_good]
    late = b[b.t_foot > 120]
    assert late["dia_rel"].min() > 0.15


def test_gaussian_can_be_disabled(tmp_path):
    df, _ = make_recording(duration_s=40, motion=[], dropouts=[], seed=2)
    path = tmp_path / "r.csv"
    df.to_csv(path, index=False)
    cfg = Layer2Config()
    cfg.gaussian.enabled = False
    l2 = run_layer2(process_file(path), cfg)
    assert not any(c.startswith("g1_") or c.startswith("gauss") for c in l2.beats.columns)
    assert Layer2Config.from_dict(Layer2Config().to_dict()).gaussian.n_components == 3


@pytest.mark.parametrize("k", [2, 4])
def test_other_component_counts(tmp_path, k):
    df, _ = make_recording(duration_s=40, motion=[], dropouts=[], seed=3)
    path = tmp_path / "r.csv"
    df.to_csv(path, index=False)
    cfg = Layer2Config()
    cfg.gaussian.n_components = k
    l2 = run_layer2(process_file(path), cfg)
    assert f"g{k}_amp_ratio" in l2.beats.columns
    assert l2.beats["gauss_ok"].mean() > 0.8


def test_weak_glasses_like_signal(tmp_path):
    df, _ = make_recording(duration_s=90, channels={"IR": (40_000, 0.003)}, noise_frac=0.08,
                           dia_amp=(0.3, 0.3), motion=[], dropouts=[], seed=5)
    path = tmp_path / "g.csv"
    df.to_csv(path, index=False)
    l2 = run_layer2(process_file(path))
    good = l2.beats[l2.beats.beat_good]
    assert good["gauss_ok"].mean() > 0.8
