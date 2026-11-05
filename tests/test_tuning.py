"""Label-free tuning: beat matching scores, per-site config overrides, and `sglab tune` end to end."""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from ppg.config import Layer1Config, Layer2Config, deep_merge, with_overrides
from ppg.synthetic import make_recording
from ppg.tuning import match_beats, pool_agreement, pulse_offset, reference_agreement
from sglab.processing import config_hash, site_overrides, stream_configs
from sglab.settings import load_settings

REPO = Path(__file__).resolve().parents[1]


def _beats(n=300, ibi=0.85, seed=0):
    rng = np.random.default_rng(seed)
    return np.cumsum(ibi * (1 + 0.03 * rng.standard_normal(n)))


def test_pulse_offset_and_matching():
    ref = _beats()
    tgt = ref + 0.137 + np.random.default_rng(1).normal(0, 0.003, ref.size)
    assert pulse_offset(ref, tgt) == pytest.approx(0.137, abs=0.004)
    ri, ti = match_beats(ref, tgt - 0.137, 0.1)
    assert ri.size == ref.size and (ri == ti).all()


def test_reference_agreement_counts_missed_and_extra_beats():
    ref = _beats()
    good = np.ones(ref.size, bool)
    tgt = np.sort(np.r_[np.delete(ref, np.arange(0, 300, 10)) - 0.08, ref[5:25] + 0.4])  # 30 missed, 20 extra
    tgood = np.ones(tgt.size, bool)
    m = pool_agreement([reference_agreement(ref, good, tgt, tgood)])
    assert m["sensitivity"] == pytest.approx(270 / 300)
    assert m["ppv"] == pytest.approx(270 / 290)
    assert m["ibi_mae_ms"] < 1e-6  # exact copies -> no interval error
    # a target that keeps only half its beats as good: sensitivity halves, PPV stays
    tgood2 = np.zeros(ref.size, bool)
    tgood2[::2] = True
    m2 = pool_agreement([reference_agreement(ref, good, ref + 0.05, tgood2)])
    assert m2["sensitivity"] == pytest.approx(0.5, abs=0.01) and m2["ppv"] == pytest.approx(1.0)
    assert m2["n_ibi"] == 0  # no consecutive good pairs


def test_with_overrides_merges_partially():
    cfg = with_overrides(Layer1Config(), {"quality": {"min_skew": -0.5}, "filters": {"morph_band_hz": [0.5, 10]}})
    assert cfg.quality.min_skew == -0.5 and cfg.quality.min_acf_peak == Layer1Config().quality.min_acf_peak
    assert cfg.filters.morph_band_hz == (0.5, 10)
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}


@pytest.fixture
def root(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.rmtree(tmp_path / "config" / "sites", ignore_errors=True)
    return tmp_path


def test_site_overrides_change_only_that_sites_hash(root):
    s = load_settings(root)
    b1, b2 = Layer1Config(), Layer2Config()
    before = {r: config_hash(*stream_configs(b1, b2, site_overrides(s), {"role": r})) for r in ("finger", "glasses")}
    (root / "config" / "sites").mkdir()
    (root / "config" / "sites" / "glasses.json").write_text(json.dumps({"layer1": {"quality": {"min_skew": -1}}}))
    sites = site_overrides(s)
    c1, _ = stream_configs(b1, b2, sites, {"role": "glasses"})
    assert c1.quality.min_skew == -1
    after = {r: config_hash(*stream_configs(b1, b2, sites, {"role": r})) for r in ("finger", "glasses")}
    assert after["finger"] == before["finger"] and after["glasses"] != before["glasses"]


def _session(folder: Path, sid: str, seed: int):
    """Finger + glasses recorded together: same beats; the glasses stream has a broad, diastolic-heavy
    beat (negative skew) and low perfusion, like the real head sites."""
    d = folder / sid
    d.mkdir(parents=True)
    common = dict(duration_s=90, fs=100.0, seed=seed, motion=(), dropouts=())
    fing, _ = make_recording(**common)
    gls, _ = make_recording(channels={"IR": (26_000, 0.004), "RED": (15_000, 0.002)}, dia_amp=(0.9, 0.9),
                            noise_frac=0.05, **common)
    fing.to_csv(d / "finger_FNG01.csv", index=False)
    gls.to_csv(d / "glasses_GLS01.csv", index=False)
    streams = [{"stream": "finger_FNG01", "role": "finger", "file": "finger_FNG01.csv", "fs": 100.0},
               {"stream": "glasses_GLS01", "role": "glasses", "file": "glasses_GLS01.csv", "fs": 100.0}]
    (d / "session.json").write_text(json.dumps({"session_id": sid, "metadata": {"quality": "good"},
                                                "streams": streams}))


def test_tune_end_to_end(root, tmp_path):
    pytest.importorskip("optuna")
    from sglab import tune

    data = tmp_path / "data"
    for i in range(3):
        _session(data, f"2026010{i}_S001_rest_T0{i}", seed=i)
    pairs = tune.find_pairs(data, "glasses")
    assert len(pairs) == 3
    t1, t2, r1, r2 = tune.starting_configs(root, "glasses", None)
    ev = tune.Evaluator(pairs, "glasses", t1, t2, r1, r2).load(log=lambda m: None)
    m = ev({})
    assert m["n_ref"] > 250 and 0 <= m["f1"] <= 1
    loose = ev({"layer1": {"quality": {"min_skew": -2.0}}})
    assert loose["sensitivity"] >= m["sensitivity"] and loose["ppv"] > 0.9

    out = tmp_path / "study"
    tune.run(data, "glasses", "beats", out, trials=4, hours=0, jobs=1, holdout_every=3, root=root,
             log=lambda m: None)
    meta = json.loads((out / "meta.json").read_text())
    assert len(meta["tune"]) == 2 and len(meta["holdout"]) == 1
    r = tune.report(out, jobs=1, log=lambda m: None)
    assert r["n_trials"] >= 4 and bool(r["table"]["baseline"].iloc[0])
    assert {"hold_f1", "tune_f1"} <= set(r["table"].columns)
    assert (out / "pareto.png").exists() and (out / "recommended.json").exists()
    path, new = tune.apply(out, root)
    assert path == root / "config" / "sites" / "glasses.json" and "layer1" in new
    assert site_overrides(load_settings(root))["glasses"] == json.loads(path.read_text())
