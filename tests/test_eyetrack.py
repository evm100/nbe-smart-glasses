"""Eye tracker port (sglab.eyetrack) on synthetic eye videos."""
import numpy as np

from sglab.eyetrack import EyeTracker, track_frames


def synth_eye(n=750, fps=25.0, size=160, blinks=(5.0, 11.0, 17.0, 23.0), blink_s=0.2, seed=0, gaze=True):
    """A bright skin background with a dark iris + darker pupil; blinks = an eyelid sweeping down
    over the eye and back up. Returns (frames n x size x size uint8, t)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(float)
    t = np.arange(n) / fps
    frames = np.empty((n, size, size), np.uint8)
    for i, ti in enumerate(t):
        cx = size / 2 + (6 * np.sign(np.sin(2 * np.pi * ti / 4)) if gaze else 0)
        cy = size / 2
        img = 170 + 10 * (yy / size) + rng.normal(0, 3, (size, size))
        d = np.hypot(xx - cx, yy - cy)
        img = np.where(d < 0.2 * size, 90, img)
        img = np.where(d < 0.08 * size, 35, img)
        # eyelid: closes over blink_s/2, opens over blink_s/2
        lid = 0.0
        for b in blinks:
            ph = (ti - b) / blink_s
            if 0 <= ph <= 1:
                lid = 1 - abs(2 * ph - 1)
        if lid > 0:
            edge = (cy - 0.3 * size) + lid * 0.65 * size
            img = np.where(yy < edge, 160 + rng.normal(0, 3, (size, size)), img)
        frames[i] = np.clip(img, 0, 255).astype(np.uint8)
    return frames, t


def test_finds_eye_and_counts_blinks():
    frames, t = synth_eye()
    rows, s = track_frames(frames, t)
    states = [r["state"] for r in rows]
    assert "calibrating" in states and states[-1] == "open"
    # tracked position near the iris centre (gaze moves +-6 px around 80)
    # (the lid's first frame drags the blob before the closure is called, as in MATLAB)
    near_blink = lambda ti: any(-0.01 <= ti - b <= 0.3 for b in (5.0, 11.0, 17.0, 23.0))
    open_rows = [r for r in rows if r["state"] == "open" and r["x"] is not None and not near_blink(r["t"])]
    assert open_rows
    assert all(abs(r["y"] - 80) < 8 for r in open_rows)
    assert all(abs(r["x"] - 80) < 14 for r in open_rows)
    closes = [r["t"] for r in rows if r["event"] == "close"]
    assert len(closes) == 4, closes
    for c, b in zip(closes, (5.0, 11.0, 17.0, 23.0)):
        assert b - 0.05 <= c <= b + 0.2
    assert 50 <= s["blink_dur_ms"] <= 300
    assert s["gaze_shifts_total"] >= 4


def test_no_eye_when_dark():
    frames = np.full((60, 96, 96), 4, np.uint8)
    rows, _ = track_frames(frames, np.arange(60) / 25)
    assert rows[-1]["state"] == "no_eye"


def test_live_summary_and_cursor():
    frames, t = synth_eye(n=400, blinks=(5.0, 9.0, 13.0))
    trk = EyeTracker(160, 160)
    for f, ti in zip(frames, t):
        trk.update(f, ti)
    s = trk.summary()
    assert s["blinks_total"] == 3 and s["perclos_pct"] is not None
    pts = trk.since(after=390)
    assert [p["n"] for p in pts] == list(range(391, 400))


def test_sim_camera_trial_tracks_the_eye(tmp_path):
    """A trial with the simulated camera glasses: frames carry the tracker's overlay, the eye API
    reports blinks, and the recording keeps every frame."""
    import json
    import shutil
    import time
    from pathlib import Path

    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.catalog import Catalog
    from sglab.settings import load_settings
    from sglab.web.app import create_app

    repo = Path(__file__).resolve().parents[1]
    shutil.copytree(repo / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)
    s = load_settings(tmp_path)
    cat = Catalog(s.db_path, s.data_root)
    c = TestClient(create_app(s))
    r = c.post("/trial/start", data={"devices": ["sim_cam"], "subject_id": "__new__", "new_subject_id": "S060",
                                     "protocol": "rest", "trial": "1", "posture": "sitting", "operator": "EV"},
               follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[1]
    api = f"/api/trials/{tid}"
    time.sleep(9.0)
    st = c.get(api).json()
    v = st["streams"][0]["video"][0]
    assert v["tracking"] and v["frames"] > 150
    f = c.get(f"{api}/video/{v['stream']}/frame")
    assert f.status_code == 200 and f.headers["content-type"] == "image/png"
    ov = json.loads(f.headers["X-Eye"])
    assert ov["state"] in ("open", "closed") and abs(ov["x"] - 80) < 25
    e = c.get(f"{api}/eye/{v['stream']}").json()
    assert e["summary"]["blinks_total"] >= 1 and e["points"]
    assert c.post(f"{api}/eye/{v['stream']}/recalibrate").json()["ok"]
    c.post(api + "/stop")
    r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "notes": "", "keep": "on"}, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    off = c.get(f"/api/sessions/{sid}/eye", params={"stream": v["stream"]}).json()
    assert off["n_frames"] >= v["frames"] and off["summary"]["blinks_total"] >= 1
    assert c.get(f"/sessions/{sid}").status_code == 200
