"""Layer 4: calibration, grouped CV, baselines, run folders."""
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from ppg.config import Layer4Config  # noqa: E402
from ppg.layer4 import calibrate, error_metrics, model_rows, train  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _dataset(n_subjects=6, sessions=2, readings=8, seed=0) -> pd.DataFrame:
    """Glasses rows where SBP rises 1 mmHg per ms of shorter pulse arrival, on top of a per-subject
    level the PPG can't see. Plus an invalid row and a finger row that must be ignored."""
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_subjects):
        level = rng.normal(120, 12)
        for k in range(sessions):
            pad = rng.normal(0, 10, readings)
            for i in range(readings):
                sys_ = level - pad[i] + rng.normal(0, 2)
                rows.append({"session_id": f"2026_S{s:03d}_T{k}", "subject": f"S{s:03d}", "protocol": "cold",
                             "t_ref": 60.0 * i, "kind": "bp", "role": "glasses", "valid": True,
                             "sys_mmhg": sys_, "dia_mmhg": sys_ * 0.6 + rng.normal(0, 2),
                             "pad_ms_rel": pad[i], "hr_mean_bpm": rng.normal(70, 5), "t_rise_s_med": np.nan})
    rows.append({**rows[0], "valid": False})
    rows.append({**rows[0], "role": "finger"})
    df = pd.DataFrame(rows)
    df.insert(0, "row", np.arange(len(df)))
    return df


def test_rows_and_calibration():
    cfg = Layer4Config(role="glasses")
    rows = model_rows(_dataset(), cfg)
    assert len(rows) == 6 * 2 * 8 and (rows["role"] == "glasses").all()
    c = calibrate(rows, cfg, ["pad_ms_rel"])
    assert len(c) == 6 * 2 * 7  # each session's first reading is its calibration
    first = rows.iloc[0]
    assert c.iloc[0]["cal_sys_mmhg"] == first["sys_mmhg"]
    assert c.iloc[0]["pad_ms_rel"] == pytest.approx(rows.iloc[1]["pad_ms_rel"] - first["pad_ms_rel"])
    assert calibrate(rows.iloc[:1], cfg, ["pad_ms_rel"]).empty  # one reading: nothing to predict


def test_calibrated_model_beats_no_change():
    cfg = Layer4Config(role="glasses", features=("pad_ms_rel", "hr_mean_bpm", "t_rise_s_med", "missing"))
    log = []
    res = train(_dataset(), cfg, log=log.append)
    info = res["info"]
    assert info["cv_unit"] == "subject" and info["n_folds"] == 6
    assert info["features"] == ["pad_ms_rel", "hr_mean_bpm"]
    assert info["features_skipped"] == ["t_rise_s_med", "missing"] and "skipped" in log[0]
    m = res["metrics"]["sys_mmhg"]["cv"]
    assert m["model"]["mae"] < 3 < 0.5 * m["baseline"]["mae"]
    assert m["model"]["r_change"] > 0.9
    # predictions are in mmHg, every reading predicted exactly once out of fold
    p = res["preds"][(res["preds"]["target"] == "sys_mmhg")]
    assert len(p) == 84 and p["row"].is_unique
    assert (p["baseline"] == p["cal"]).all()
    assert res["models"]["sys_mmhg"].steps[-1][1].coef_[0] < -5  # shorter arrival -> higher BP


def test_one_subject_falls_back_to_sessions_and_holdout():
    df = _dataset()
    one = df[df["subject"] == "S000"]
    log = []
    res = train(pd.concat([one, one.assign(session_id=one["session_id"] + "b")]),
                Layer4Config(role="glasses"), log=log.append)
    assert res["info"]["cv_unit"] == "session" and any("new day of a known person" in x for x in log)

    res = train(df, Layer4Config(role="glasses", holdout_subjects=("S005",), calibrated=False))
    assert res["info"]["holdout_subjects"] == ["S005"] and "S005" not in res["info"]["subjects"]
    assert res["metrics"]["sys_mmhg"]["holdout"]["model"]["n"] == 16
    assert res["info"]["n_folds"] == 5

    with pytest.raises(ValueError, match="2\\+ sessions"):
        train(df[df["session_id"] == df["session_id"][0]], Layer4Config(role="glasses"))
    with pytest.raises(ValueError, match="none of the features"):
        train(df, Layer4Config(role="glasses", features=("nope",)))


def test_error_metrics():
    m = error_metrics(np.array([100.0, 110, 120]), np.array([102.0, 112, 122]))
    assert m["me"] == pytest.approx(2) and m["sd"] == pytest.approx(0) and m["mae"] == pytest.approx(2)


def test_train_run_writes_and_lists(tmp_path):
    from sglab.model import list_runs, train_run
    from sglab.settings import load_settings

    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "lab.toml", tmp_path / "config")
    s = load_settings(tmp_path)
    ds = s.data_root / "datasets" / "bp"
    ds.mkdir(parents=True)
    _dataset().to_csv(ds / "rows.csv", index=False)
    (ds / "manifest.json").write_text(json.dumps({"built_at": "2026-10-30T00:00:00+00:00"}))
    cfg = Layer4Config(role="glasses")
    m1 = train_run(s, "bp", cfg, log=lambda *_: None)
    m2 = train_run(s, "bp", cfg, log=lambda *_: None)
    assert m1["name"] == "bp-glasses-ridge-cal" and m1["run_id"] != m2["run_id"]
    out = s.data_root / "models" / m1["name"] / m1["run_id"]
    for f in ("config.json", "metrics.json", "predictions.csv", "model.joblib", "report.png"):
        assert (out / f).stat().st_size > 0
    assert m1["dataset"]["rows_sha256"] == m2["dataset"]["rows_sha256"]
    assert m1["coefficients"]["sys_mmhg"]["pad_ms_rel"] < 0
    assert [r["run_id"] for r in list_runs(s)] == [m1["run_id"], m2["run_id"]]
    with pytest.raises(FileNotFoundError, match="dataset build"):
        train_run(s, "nope", cfg)


FAST = {"trials": 12, "nested": True, "seed": 0, "features": ["hr_mean_bpm", "pad_ms_rel", "t_rise_s_med"],
        "space": [["model.kind", "categorical", ["ridge"]], ["model.alpha", "float", [0.01, 100, "log"]]]}


def test_sweep_finds_the_signal_and_scores_itself():
    pytest.importorskip("optuna")
    from sglab.sweep import run

    ticks = []
    r = run(_dataset(n_subjects=4), Layer4Config(role="glasses"), FAST, log=lambda *_: None,
            progress=lambda d, n: ticks.append((d, n)))
    assert "pad_ms_rel" in r["best"].features
    assert ticks[-1] == (12 * 5, 12 * 5)  # the search + one per left-out subject
    n = r["nested"]
    assert n["unit"] == "subject" and n["n_outer"] == 4 and len(n["chosen"]) == 4
    m = n["metrics"]["sys_mmhg"]
    assert m["model"]["mae"] < 0.5 * m["baseline"]["mae"]
    # two sessions only: no nested score, still a best config
    one = _dataset(n_subjects=1)
    assert run(one, Layer4Config(role="glasses"), FAST, log=lambda *_: None)["nested"] is None


def test_sweep_run_writes_a_model_run(tmp_path):
    pytest.importorskip("optuna")
    from sglab.model import explain, list_runs
    from sglab.settings import load_settings
    from sglab.sweep import sweep_run

    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "lab.toml", tmp_path / "config")
    s = load_settings(tmp_path)
    ds = s.data_root / "datasets" / "bp"
    ds.mkdir(parents=True)
    _dataset(n_subjects=4).to_csv(ds / "rows.csv", index=False)
    m = sweep_run(s, "bp", FAST, base=Layer4Config(role="glasses"), log=lambda *_: None)
    assert m["name"] == "bp-glasses-sweep-cal" and 8 < m["sweep"]["trials"] <= 12  # no-feature trials are skipped
    assert (s.data_root / "models" / m["name"] / m["run_id"] / "trials.csv").exists()
    assert list_runs(s)[0]["sweep"]["nested"]["n_outer"] == 4
    x = explain(m)
    assert x["targets"][0]["verdict"] == "good" and "beats the baseline" in x["targets"][0]["text"]
    assert "honest check" in x["sweep"]
