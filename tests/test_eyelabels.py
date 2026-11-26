"""Eye labeling: label store (sglab.eyelabels), pre-fill guesses (sglab.eyeprefill), pages and API (sglab.web.labels)."""
import shutil
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sglab import eyeprefill
from sglab.eyelabels import LabelError, LabelStore, agreement
from test_eyetrack import synth_eye

REPO = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://testserver"}
PW = "correct horse battery"


# ---------------------------------------------------------------- store
def test_store_tracks_ranges_and_progress(tmp_path):
    st = LabelStore(tmp_path / "catalog.sqlite")
    t = st.apply("S1", "cam", "olga", 10, [[0, 9, 5], [4, 6, 1], [5, 5, None]], labeler_name="Olga")
    assert t["levels"] == "55551.1555" and t["n_labeled"] == 9
    assert t["labeler_name"] == "Olga" and t["source"] == "human" and t["blind"] is False
    t = st.apply("S1", "cam", "olga", 10, [[9, 3, 0]])          # reversed ends are fine
    assert t["levels"] == "5550000000"
    st.apply("S1", "cam", "otto", 10, [[0, 4, 5]], blind=True)
    st.apply("S2", "cam", "olga", 8, [[0, 1, 4]])
    tr = st.tracks("S1", "cam")
    assert [x["labeler"] for x in tr] == ["olga", "otto"] and "levels" not in tr[0] and tr[1]["blind"] is True
    prog = {(r["session_id"], r["stream"]): r for r in st.progress()}
    assert set(prog) == {("S1", "cam"), ("S2", "cam")} and len(prog[("S1", "cam")]["tracks"]) == 2
    # a re-imported recording with more frames keeps the labels that still fit
    assert st.apply("S1", "cam", "olga", 12, [])["levels"] == "5550000000.."
    for bad in ([[0, 1, 7]], [[0, 1, True]], [["x", 1, 2]], [[0]]):
        with pytest.raises(LabelError):
            st.apply("S1", "cam", "olga", 10, bad)
    with pytest.raises(LabelError):
        st.apply("S1", "cam", "", 10, [])
    assert st.delete("S2", "cam", "olga") and st.get("S2", "cam", "olga") is None


def test_crop_and_agreement(tmp_path):
    st = LabelStore(tmp_path / "catalog.sqlite")
    c = st.set_crop("S1", "cam", {"x": 150, "y": -5, "w": 40, "h": 30}, 160, 160, by="Olga")
    assert (c["x"], c["y"], c["w"], c["h"]) == (150, 0, 10, 30) and c["set_by"] == "Olga"
    with pytest.raises(LabelError):
        st.set_crop("S1", "cam", {"x": 0, "y": 0, "w": 4, "h": 40}, 160, 160)
    assert st.set_crop("S1", "cam", None, 160, 160) is None and st.crop("S1", "cam") is None

    a = agreement("5554321.00", "5543321500")
    assert a["n_both"] == 9 and a["exact"] == round(7 / 9, 4) and a["within_one"] == 1.0
    assert agreement("555", "555") == {"n_both": 3, "exact": 1.0, "within_one": 1.0, "kappa": None}   # chance = 1
    assert agreement("5511", "5511")["kappa"] == 1.0 and agreement("0", "1")["within_one"] == 0.0
    assert agreement("...", "555")["n_both"] == 0


# ---------------------------------------------------------------- pre-fill
def test_prefill_finds_the_blinks():
    frames, t = synth_eye(n=750, blinks=(5.0, 11.0, 17.0, 23.0), gaze=False)
    r = eyeprefill.compute(frames, t)
    lev, op = r["level"], r["open"]
    for b in (5.0, 11.0, 17.0, 23.0):
        mid = int(round((b + 0.1) * 25))
        assert lev[mid] <= 2, (b, lev[mid - 3:mid + 4])
        assert any(c0 <= mid <= c1 for c0, c1 in r["candidates"])
    blinkish = np.zeros(len(t), bool)
    for b in (5.0, 11.0, 17.0, 23.0):
        blinkish[(t >= b - 0.2) & (t <= b + 0.4)] = True
    assert np.mean(lev[~blinkish] == 5) > 0.95          # open stretches: accept them in bulk
    assert len(r["candidates"]) == 4 and r["component"]["fps"] == 25.0


def test_prefill_with_gaze_shifts_and_crop():
    frames, t = synth_eye(n=750, blinks=(6.0, 14.0, 22.0), gaze=True, seed=3)
    r = eyeprefill.compute(frames, t, {"x": 30, "y": 20, "w": 100, "h": 120})
    for b in (6.0, 14.0, 22.0):
        assert r["level"][int(round((b + 0.1) * 25))] <= 2
    assert len(r["candidates"]) <= 6                    # the blinks, not every gaze shift


def test_prefill_unusable_and_no_blinks():
    frames, t = synth_eye(n=300, blinks=(), gaze=False)
    frames[100:120] = 255                                # washed out
    r = eyeprefill.compute(frames, t)
    assert (r["level"][100:120] == 0).all()
    assert np.mean(r["level"][:100] == 5) > 0.95 and r["candidates"] == []   # noise is not a blink
    dark = np.zeros((30, 64, 64), np.uint8)
    assert (eyeprefill.compute(dark, np.arange(30) / 25)["level"] == 0).all()


# ---------------------------------------------------------------- web
@pytest.fixture
def server(tmp_path, monkeypatch):
    """App with accounts (olga operator, vic viewer) and one fake session with a synthetic camera stream."""
    from sglab.auth import AuthStore
    from sglab.settings import load_settings
    from sglab.web import labels
    from sglab.web.app import create_app

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(f'[auth]\nenabled = true\n[pki]\ndir = "{tmp_path / "pki"}"\n')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    a = AuthStore(s.db_path)
    for u, role in (("olga", "operator"), ("vic", "viewer")):
        a.add_user(u, PW, role, display=u.capitalize())
    frames, t = synth_eye(n=300, blinks=(4.0, 9.0), gaze=False)
    fpath = tmp_path / "eye.frames"
    fpath.write_bytes(b"x")
    sess = {"session_id": "S001_eye", "path": "S001_eye", "runs": [], "streams": [], "subject_id": "S001"}
    monkeypatch.setattr(labels, "_session", lambda ctx, sid: dict(sess) if sid == "S001_eye" else
                        (_ for _ in ()).throw(labels.HTTPException(404, "no such session")))
    monkeypatch.setattr(labels, "_doc", lambda ctx, s: {"video": [{"stream": "eye", "fmt": "gray8"}]})
    monkeypatch.setattr(labels, "_stream_frames", lambda ctx, sid, stream: (labels._session(ctx, sid), fpath, frames, t)
                        if stream == "eye" else (_ for _ in ()).throw(labels.HTTPException(404, "no camera")))
    app = create_app(s)

    def login(u):
        c = TestClient(app, headers=ORIGIN)
        assert c.post("/login", data={"username": u, "password": PW, "next": "/"}, follow_redirects=False).status_code == 303
        return c
    return app, {"olga": login("olga"), "vic": login("vic")}, frames, t


def test_api_frames_labels_and_pages(server):
    app, u, frames, t = server
    o, v = u["olga"], u["vic"]
    tm = v.get("/api/sessions/S001_eye/video/eye/times").json()
    assert tm["n"] == 300 and tm["w"] == tm["h"] == 160 and tm["fps"] == 25.0 and len(tm["t"]) == 300

    r = v.get("/api/sessions/S001_eye/video/eye/chunk?i0=10&n=4")
    assert r.headers["x-frame-n"] == "4" and len(r.content) == 4 * 160 * 160
    assert np.array_equal(np.frombuffer(r.content, np.uint8).reshape(4, 160, 160), frames[10:14])
    r = v.get("/api/sessions/S001_eye/video/eye/chunk?i0=298&n=10&x=40&y=40&w=64&h=64&f=2")
    assert r.headers["x-frame-n"] == "2" and r.headers["x-frame-w"] == "32" and len(r.content) == 2 * 32 * 32

    assert v.post("/api/sessions/S001_eye/eye/labels", json={"stream": "eye", "ranges": [[0, 5, 5]]}).status_code == 403
    tr = o.post("/api/sessions/S001_eye/eye/labels", json={"stream": "eye", "ranges": [[0, 99, 5], [100, 104, 1]]}).json()
    assert tr["n_labeled"] == 105 and tr["labeler"] == "olga" and "levels" not in tr
    assert o.post("/api/sessions/S001_eye/eye/labels", json={"stream": "eye", "ranges": [[0, 1, 9]]}).status_code == 400
    d = o.get("/api/sessions/S001_eye/eye/labels?stream=eye").json()
    assert d["n"] == 300 and d["me"] == "olga" and d["mine"]["levels"].startswith("5" * 100 + "11111.")
    assert d["can_label"] is True and d["tracks"][0]["labeler_name"] == "Olga" and d["levels"]["0"] == "eye not visible"
    dv = v.get("/api/sessions/S001_eye/eye/labels?stream=eye&labeler=olga").json()
    assert dv["mine"] is None and dv["other"]["n_labeled"] == 105 and dv["can_label"] is False
    assert v.get("/api/sessions/S001_eye/eye/agreement?stream=eye&a=olga&b=nobody").status_code == 404

    assert o.put("/api/sessions/S001_eye/eye/crop", json={"stream": "eye", "box": {"x": 20, "y": 20, "w": 120, "h": 120}}).json()["crop"]["w"] == 120
    assert v.put("/api/sessions/S001_eye/eye/crop", json={"stream": "eye", "box": None}).status_code == 403
    pf = v.get("/api/sessions/S001_eye/eye/prefill?stream=eye").json()
    assert pf["n"] == 300 and len(pf["level"]) == 300 and pf["crop"]["w"] == 120 and len(pf["candidates"]) == 2
    assert pf["level"][int(4.1 * 25)] in "12" and pf["component"] is not None
    assert v.get("/api/sessions/S001_eye/eye/prefill?stream=eye").json()["level"] == pf["level"]   # cached

    page = o.get("/sessions/S001_eye/label").text
    assert "Label eye frames" in page and '"eye"' in page and "operator</b> account" not in page
    assert "operator</b> account" in v.get("/sessions/S001_eye/label").text
    lst = o.get("/labeling").text
    assert "S001_eye" in lst and "Olga" in lst and "35 %" in lst
    assert 'href="/labeling"' in o.get("/labeling").text and 'href="/labeling"' not in v.get("/labeling").text  # nav: operators
    assert v.get("/sessions/S001_eye/video/eye/chunk").status_code == 404
