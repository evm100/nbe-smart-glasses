"""Further sensors (protocol v2.1 auxiliary streams), the session review API and the new pages."""
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from sglab.catalog import Catalog
from sglab.protocol import Measure, ProtocolError, encode, parse_line
from sglab.recorder import Recording
from sglab.settings import load_settings

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def lab(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)
    s = load_settings(tmp_path)
    return s, Catalog(s.db_path, s.data_root)


def test_measure_line():
    m = parse_line(encode("M,2,7,123456,84.5,,3"))
    assert isinstance(m, Measure) and (m.sid, m.seq, m.t_us) == (2, 7, 123456)
    assert m.values == [84.5, None, 3.0]
    with pytest.raises(ProtocolError):
        parse_line(encode("M,2,x,1,2"))


def test_minute_table_and_frame_times():
    from sglab.eyetrack import minute_table
    from sglab.video import frame_times

    mins = {0: [58.0, 2.0, 1400], 1: [10.0, 0.0, 250], 2: [60.0, 6.0, 1500]}
    blinks = [{"t": 5.0, "dur": 0.2}, {"t": 30.0, "dur": 0.4}, {"t": 130.0, "dur": None}]
    shifts = [{"t": 10.0, "dur": 0.04, "amp": 6.0, "speed": 150.0, "peak": 200.0}, {"t": 125.0, "dur": 0.08, "amp": 4.0, "speed": 50.0, "peak": 80.0}]
    t = minute_table(mins, blinks, shifts, now=170.0)
    assert [r["minute"] for r in t] == [0, 1, 2]
    assert t[0]["blinks"] == 2 and t[0]["blink_dur_ms"] == 300 and abs(t[0]["blinks_per_min"] - 2 * 60 / 58) < 0.06
    assert t[0]["gaze_speed"] == 150.0 and t[0]["perclos_pct"] == round(100 * 2 / 58, 1)
    assert t[1]["blinks_per_min"] is None and t[1]["gaze_speed"] is None      # 10 s tracked: no rate
    assert t[2]["partial"] and t[2]["blink_dur_ms"] is None and not t[0]["partial"]
    # frames captured every 40 ms but delivered in bursts: capture times get the device spacing back
    dev = np.arange(500) * 40_000 + 5_000_000
    arrive = 1.0 + (dev - dev[0]) / 1e6 + 0.02 + (np.arange(500) % 10) * 0.03
    ft = frame_times(arrive, dev)
    assert np.allclose(np.diff(ft), 0.04, atol=1e-6) and abs(ft[0] - 1.02) < 1e-6


def test_aux_streams_recorded_and_served(lab):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.web.app import create_app

    s, cat = lab
    rec = Recording(s, [s.device("sim_multi")], sim_kw={"realtime": True})
    assert not rec.start({"subject_id": "S001", "protocol": "rest"})
    time.sleep(6.5)
    live = rec.live_status()[0]["aux"]
    assert {a["name"] for a in live} == {"sweat", "skin", "cam_feat"}
    sweat = next(a for a in live if a["name"] == "sweat")
    assert sweat["channels"] == ["glucose", "lactate", "cortisol"] and sweat["units"] == ["uM", "mM", "ng/mL"]
    assert sweat["last"][2] is not None       # cortisol reads every 5th sample: its last value is kept
    rec.stop()
    dest, doc = rec.finalize({"subject_id": "S001", "protocol": "rest", "trial": 1, "keep": True})
    aux = {a["name"]: a for a in doc["aux"]}
    assert aux["sweat"]["stats"]["n_samples"] >= 6 and aux["sweat"]["stats"]["n_dropped_seq"] == 0
    assert aux["cam_feat"]["kind"] == "features" and aux["cam_feat"]["src"] == "cam" and aux["cam_feat"]["role"] == "glasses"
    rows = (dest / aux["sweat"]["file"]).read_text().splitlines()
    assert rows[0] == "t_s,seq,t_dev_us,glucose,lactate,cortisol" and rows[2].endswith(",")   # empty cell = no value
    assert [st["stream"] for st in doc["streams"]] == ["glasses_SIMX01"]    # PPG processing never sees aux streams
    assert rec.workers[0].stats["n_malformed"] == 0

    sid = cat.upsert_session(dest)
    c = TestClient(create_app(s))
    ov = c.get(f"/api/sessions/{sid}/overview").json()
    assert {"ppg", "eye_camera", "glucose", "cortisol", "skin_temp"} <= set(ov["sensors"])
    lab_ = next(a for a in ov["aux"] if a["name"] == "sweat")["labels"]
    assert lab_[0]["name"] == "Glucose" and lab_[0]["unit"] == "uM" and lab_[0]["family"] == "biochemical"
    d = c.get(f"/api/sessions/{sid}/aux", params={"stream": aux["sweat"]["stream"]}).json()
    by = {ch["channel"]: ch for ch in d["channels"]}
    assert by["glucose"]["n"] >= 6 and 1 <= by["cortisol"]["n"] < by["glucose"]["n"]
    assert c.get(f"/api/sessions/{sid}/aux", params={"stream": "nope"}).status_code == 404
    page = c.get(f"/sessions/{sid}")
    assert page.status_code == 200 and "Other sensors" in page.text
    for path in ("/home", "/platform", "/help"):
        assert c.get(path).status_code == 200, path
    plat = c.get("/api/platform").json()
    assert next(x for x in plat["sensors"] if x["id"] == "glucose")["usage"]["sessions"] == 1


def test_signal_zoom_and_quality(lab):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.processing import process_session
    from sglab.web.app import create_app

    s, cat = lab
    rec = Recording(s, [s.device("sim")], sim_kw={})
    assert not rec.start({"subject_id": "S001", "protocol": "rest"})
    time.sleep(30)
    rec.stop()
    dest, _ = rec.finalize({"subject_id": "S001", "protocol": "rest", "trial": 1, "keep": True})
    sid = cat.upsert_session(dest)
    process_session(s, cat, sid, log=lambda *_: None, plot=False)
    c = TestClient(create_app(s))
    stream = cat.session(sid)["streams"][0]["stream"]
    whole = c.get(f"/api/sessions/{sid}/signal", params={"stream": stream, "max_points": 400}).json()
    assert whole["decimated"] and len(whole["t"]) <= 400 and whole["beats"] is not None   # 30 s <= 3 min: beats sent
    z = c.get(f"/api/sessions/{sid}/signal", params={"stream": stream, "t0": 10, "t1": 14}).json()
    assert not z["decimated"] and 350 <= z["n_in_range"] <= 420            # every sample of 4 s at 100 Hz
    assert 9.9 <= z["t"][0] <= 10.05 and 13.95 <= z["t"][-1] <= 14.1
    assert 3 <= len(z["beats"]["t"]) <= 7 and all(10 <= t <= 14 for t in z["beats"]["t"])
    assert z["full_range"][1] > 25
    q = c.get(f"/api/sessions/{sid}/quality", params={"stream": stream}).json()
    assert q["windows"]["n"] > 5 and 0 <= q["windows"]["good_frac"] <= 1 and q["beats"]["good_frac"] > 0.5
    assert len(q["windows"]["good"]) == len(q["windows"]["t0"]) == len(q["windows"]["metrics"]["acf_peak"])
    assert q["limits"]["skew"][0] is not None
    assert c.get(f"/api/sessions/{sid}/quality", params={"stream": "nope"}).status_code == 409
