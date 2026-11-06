"""Live PPG analysis (sglab.liveppg) and model-based BP estimates (sglab.bpmodel)."""
import numpy as np

from ppg.config import Layer1Config, Layer2Config
from ppg.synthetic import make_recording
from sglab.liveppg import LivePPG, window_features


def _samples(duration=60.0, fs=100.0, hr=72.0, seed=0):
    df, _ = make_recording(duration_s=duration, fs=fs, hr_bpm=hr, motion=(), dropouts=(), seed=seed)
    t = df["timestamp_ms"].to_numpy() / 1000.0
    return [(float(a), float(b), i) for i, (a, b) in enumerate(zip(t, df["IR"].to_numpy()))]


def test_live_readout_and_beat_log():
    s = _samples(60.0, hr=72.0)
    lp = LivePPG("glasses", 100.0, "ir", Layer1Config(), Layer2Config())
    t_all = np.array([x[0] for x in s])
    # feed it like the background thread: every 1.5 s, the deque holds the last 20 s
    for t_now in np.arange(5.0, 60.0, 1.5):
        k = np.searchsorted(t_all, t_now)
        lp.step(s[max(0, k - 2000):k])
    r = lp.state()
    assert r["state"] == "ok"
    assert abs(r["hr_bpm"] - 72) < 4
    assert r["sqi_label"] in ("good", "fair")
    assert r["template"] and len(r["template"]) == 100
    assert 0.05 < r["fiducials"]["peak"] < 0.5
    assert r["markers"] and r["trend"]
    beats, waves = lp.beats()
    # every beat logged once, no duplicates, covering most of the minute
    tf = beats["t_foot"].to_numpy()
    assert np.all(np.diff(tf) > 0.3)
    assert 55 <= len(beats) <= 75
    f = window_features(beats, waves, 10, 55)
    assert f["n_good"] >= 30 and abs(f["hr_mean_bpm"] - 72) < 4
    assert np.isfinite(f["t_rise_s_med"]) and np.isfinite(f["rmssd_ms"])


def _model_run(s, role="glasses"):
    """A calibrated ridge run on a fake dataset, so the BP plumbing has something to load."""
    import json

    import pandas as pd
    from ppg.config import Layer4Config
    from sglab.model import train_run

    rng = np.random.default_rng(0)
    rows = []
    for subj in range(5):
        for k in range(2):
            hr = rng.normal(70, 6, 6)
            for i in range(6):
                sys_ = 120 + 0.8 * (hr[i] - 70) + rng.normal(0, 2)
                rows.append({"session_id": f"X_S{subj:03d}_T{k}", "subject": f"S{subj + 100:03d}", "protocol": "p",
                             "t_ref": 60.0 * i, "kind": "bp", "role": role, "valid": True, "sys_mmhg": sys_,
                             "dia_mmhg": 0.6 * sys_, "hr_mean_bpm": hr[i], "t_rise_s_med": rng.normal(0.15, 0.01)})
    ds = s.data_root / "datasets" / "bp"
    ds.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.insert(0, "row", np.arange(len(df)))
    df.to_csv(ds / "rows.csv", index=False)
    (ds / "manifest.json").write_text(json.dumps({"built_at": "2026-11-01T00:00:00+00:00"}))
    return train_run(s, "bp", Layer4Config(role=role, features=("hr_mean_bpm", "t_rise_s_med")), log=lambda *_: None)


def test_trial_insights_live_bp_and_session_prediction(tmp_path):
    import shutil
    import time
    from pathlib import Path

    import pytest
    pytest.importorskip("sklearn")
    from fastapi.testclient import TestClient

    from sglab.bpmodel import session_prediction, resolve
    from sglab.catalog import Catalog
    from sglab.processing import process_session
    from sglab.settings import load_settings
    from sglab.web.app import create_app

    repo = Path(__file__).resolve().parents[1]
    shutil.copytree(repo / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)
    s = load_settings(tmp_path)
    cat = Catalog(s.db_path, s.data_root)
    m = _model_run(s)
    c = TestClient(create_app(s))
    assert any(r["key"] == f"{m['name']}/{m['run_id']}" for r in c.get("/api/bp-models").json()["runs"])

    r = c.post("/trial/start", data={"devices": ["sim", "sim_glasses"], "subject_id": "__new__",
                                     "new_subject_id": "S050", "protocol": "rest", "trial": "1",
                                     "posture": "sitting", "operator": "EV"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[1]
    api = f"/api/trials/{tid}"
    assert c.post(api + "/event", json={"kind": "cuff_start"}).status_code == 200
    time.sleep(13.0)
    ins = c.get(api + "/insights").json()
    by = {p["role"]: p for p in ins["ppg"]}
    assert by["finger"]["state"] == "ok" and abs(by["finger"]["hr_bpm"] - 72) < 5
    assert by["finger"]["template"] and by["finger"]["markers"]
    assert ins["bp"]["role"] == "glasses" and ins["bp"]["state"] in ("needs_cal", "warming_up", "low_quality")
    assert c.post(api + "/event", json={"kind": "bp", "sys_mmhg": 118, "dia_mmhg": 76}).status_code == 200
    time.sleep(7.0)
    bp = c.get(api + "/insights").json()["bp"]
    assert bp["state"] == "ok", bp
    assert bp["cal"]["sys_mmhg"] == 118 and 90 < bp["sys_mmhg"] < 150 and bp["trend"]
    assert c.post(api + "/bp-model", json={"model": "nope/nope"}).status_code == 200
    time.sleep(2.0)
    assert c.get(api + "/insights").json()["bp"]["state"] == "no_model"
    assert "Stop" in c.get(f"/trials/{tid}").text
    c.post(api + "/stop")
    r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "notes": "", "keep": "on"}, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    process_session(s, cat, sid, plot=False, log=lambda *_: None)

    run = resolve(s, "auto", "glasses")
    p = session_prediction(s, cat, sid, run, step_s=2.0, window_s=12.0)
    assert p["stream"].startswith("glasses") and p["cal"]["sys_mmhg"] == 118 and not p["seen"]
    assert len(p["readings"]) == 1 and p["readings"][0]["is_cal"]
    vals = [v for v in p["trace"]["sys_mmhg"] if v is not None]
    assert vals and all(90 < v < 150 for v in vals)
    j = c.get(f"/api/sessions/{sid}/bp-predict", params={"model": run["key"], "window": 12}).json()
    assert j["model"] == run["key"] and j["trace"]["t"]
    assert c.get(f"/sessions/{sid}").status_code == 200
