"""Cued sessions (sglab.cues): protocol definitions, browser-clock -> session-clock maths, the cue log
and its API (live trial -> saved session)."""
import csv
import io
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from sglab import cues
from sglab.catalog import Catalog
from sglab.settings import load_settings

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- protocol definitions
def test_lab_protocol_file_is_valid():
    ps = {p["id"]: p for p in cues.load_protocols(REPO)}
    p = ps["cued_blink_v1"]
    ids = [s["id"] for s in p["steps"]]
    assert ids == ["spontaneous", "blink_cued", "long_blink", "half_blink", "gaze", "faces", "talk",
                   "glasses_off_on", "blink_cued_again"]
    st = {s["id"]: s for s in p["steps"]}
    # 60 s spontaneous, no cues
    assert st["spontaneous"]["duration_s"] - st["spontaneous"]["lead_in_s"] == 60 and not st["spontaneous"]["cues"]
    # 20 x every 3 s
    b = st["blink_cued"]["cues"]
    assert len(b) == 20 and np.allclose(np.diff([c["t"] for c in b]), 3.0) and {c["action"] for c in b} == {"blink"}
    assert b[0]["t"] == st["blink_cued"]["lead_in_s"] and b[-1]["t"] < st["blink_cued"]["duration_s"]
    # 10 x every 4 s, each with an "open" tone 1 s later
    lb = st["long_blink"]["cues"]
    close = [c for c in lb if c["action"] == "long_blink_close"]
    opened = [c for c in lb if c["action"] == "long_blink_open"]
    assert len(close) == len(opened) == 10 and np.allclose(np.diff([c["t"] for c in close]), 4.0)
    assert np.allclose([o["t"] - c["t"] for c, o in zip(close, opened)], 1.0) and {c["tone"] for c in opened} == {"release"}
    assert len(st["half_blink"]["cues"]) == 10
    # 90 s of gaze targets in every direction, flagged as negatives
    g = st["gaze"]
    assert g["negative"] and g["duration_s"] - g["lead_in_s"] == 90
    assert {c["gaze"] for c in g["cues"]} == set(cues.GAZE) and all(c["t"] < g["duration_s"] for c in g["cues"])
    assert [c["action"] for c in st["faces"]["cues"]] == ["squint", "smile", "frown"] and st["faces"]["negative"]
    assert st["talk"]["negative"] and st["talk"]["duration_s"] - st["talk"]["lead_in_s"] == 60
    # step 8 = 90 s: glasses off/on + step 2 again (same_as copies its cues)
    assert st["glasses_off_on"]["duration_s"] + st["blink_cued_again"]["duration_s"] - 2 * 3 == 90
    assert st["blink_cued_again"]["cues"] == st["blink_cued"]["cues"] and st["blink_cued_again"]["label"] == "cued_blink"
    assert 7 * 60 < p["total_s"] < 11 * 60
    assert [s["index"] for s in p["steps"]] == list(range(len(p["steps"])))
    assert "quick_check" in ps


def _proto(**step):
    return {"id": "t", "name": "T", "steps": [{"id": "a", "title": "A", **step}]}


@pytest.mark.parametrize("bad, msg", [
    ({"steps": []}, "non-empty list of steps"),
    (_proto(duration_s=10, colour="red"), "unknown field"),
    (_proto(), "needs duration_s"),
    (_proto(duration_s=2, lead_in_s=3), "longer than lead_in_s"),
    (_proto(cues={"type": "repeat", "every_s": 3, "count": 0, "action": "blink"}), "count"),
    (_proto(cues={"type": "repeat", "every_s": 3, "count": 10, "action": "blink"}, duration_s=12), "too short"),
    (_proto(cues={"type": "repeat", "every_s": 2, "count": 3, "action": "x", "release_after_s": 2}), "release_after_s"),
    (_proto(duration_s=20, cues={"type": "timed", "items": [{"at_s": 30, "action": "x"}]}), "after the step ends"),
    (_proto(duration_s=20, cues={"type": "timed", "items": [{"at_s": 5, "action": "x"}, {"at_s": 2, "action": "y"}]}), "increase"),
    (_proto(duration_s=20, cues={"type": "sequence", "every_s": 3, "items": [{"action": "x", "gaze": "sideways"}]}), "gaze"),
    (_proto(cues={"type": "sequence", "every_s": 3, "items": [{"action": "x"}]}), "needs duration_s"),
    (_proto(duration_s=20, cues={"type": "wiggle"}), "type"),
    (_proto(duration_s=20, same_as="nope"), "earlier step"),
])
def test_protocol_validation_errors(bad, msg):
    p = {"id": "t", "name": "T", **bad}
    with pytest.raises(cues.CueProtocolError, match=msg):
        cues.validate_protocol(p)


def test_duplicate_step_ids_and_same_as_override():
    p = {"id": "t", "name": "T", "steps": [
        {"id": "a", "title": "A", "cues": {"type": "repeat", "every_s": 2, "count": 3, "action": "blink"}},
        {"id": "a", "title": "again", "duration_s": 5}]}
    with pytest.raises(cues.CueProtocolError, match="used twice"):
        cues.validate_protocol(p)
    p["steps"][1] = {"id": "b", "same_as": "a", "title": "B", "lead_in_s": 1}
    out = cues.validate_protocol(p)
    assert out["steps"][1]["title"] == "B" and [c["t"] for c in out["steps"][1]["cues"]] == [1.0, 3.0, 5.0]
    assert out["steps"][1]["duration_s"] == 7.0 and out["n_cues"] == 6


def test_broken_protocol_file_gives_readable_error(tmp_path):
    (tmp_path / "config").mkdir()
    assert cues.load_protocols(tmp_path) == []
    (tmp_path / "config" / "cue_protocols.json").write_text("{not json")
    with pytest.raises(cues.CueProtocolError, match="not valid JSON"):
        cues.load_protocols(tmp_path)


# ---------------------------------------------------------------- clock maths
def test_ping_sample_ntp_formulas():
    # browser clock = session clock - 100 s; 4 ms out, 1 ms in the server, 2 ms back
    p0 = 1000.0
    t1 = p0 + 100 + 0.004
    t2 = t1 + 0.001
    p1 = t2 - 100 + 0.002
    s = {"p0_ms": p0 * 1e3, "p1_ms": p1 * 1e3, "t1_s": t1, "t2_s": t2}
    mid, off, delay = cues.ping_sample(s)
    assert abs(delay - 0.006) < 1e-9                   # round trip minus the server's own time
    assert abs(off - 100.001) < 1e-9                   # true 100 s + half the asymmetry (4 vs 2 ms)
    assert abs(off - 100) <= delay / 2                 # ...which the delay bounds
    assert cues.ping_sample({"p0_ms": 5, "p1_ms": 4, "t1_s": 1}) is None   # time running backwards
    assert cues.ping_sample({"p0_ms": 5}) is None


def _pings(true_off, drift, times, rng, slow_every=0):
    out = []
    for i, p0 in enumerate(times):
        up, down = rng.uniform(0.001, 0.003), rng.uniform(0.001, 0.003)
        if slow_every and i % slow_every == 1:   # a ping stuck behind other requests on the way out
            up += 0.150
        srv = 0.0005
        sess = lambda perf: perf + true_off + drift * perf
        t1 = sess(p0 + up)
        t2 = t1 + srv
        p1 = p0 + up + srv / (1 + drift) + down
        out.append({"p0_ms": p0 * 1e3, "p1_ms": p1 * 1e3, "t1_s": t1, "t2_s": t2})
    return out


def test_fit_clock_recovers_offset_and_drift():
    rng = np.random.default_rng(3)
    true_off, drift = -8123.25, 80e-6          # 80 ppm: 48 ms over 10 min if it were ignored
    times = list(np.arange(8) * 0.05 + 9000) + list(9000.5 + np.arange(1, 61) * 10.0)
    fit = cues.fit_clock(_pings(true_off, drift, times, rng, slow_every=7))
    assert fit["n"] == 68 and fit["n_used"] < fit["n"]       # slow pings left out
    assert abs(fit["drift_ppm"] - 80) < 5
    for perf_s in (9000.0, 9300.0, 9600.0):
        want = perf_s + true_off + drift * perf_s
        assert abs(cues.to_session_s(perf_s * 1e3, fit) - want) < 0.002
    assert 0 < fit["err_ms"] < 5


def test_fit_clock_short_run_uses_constant_offset():
    rng = np.random.default_rng(1)
    fit = cues.fit_clock(_pings(12.5, 0.0, [100 + 0.05 * i for i in range(8)], rng))
    assert fit["b"] == 0.0 and abs(fit["a"] - 12.5) < 0.002
    assert cues.fit_clock([]) is None and cues.to_session_s(1.0, None) is None
    assert cues.to_session_s(None, fit) is None


# ---------------------------------------------------------------- the log
def _log(tmp_path):
    clock = {"t": 10.0}
    log = cues.CueLog(lambda: tmp_path, lambda: clock["t"])
    return log, clock


def test_cue_log_merges_updates_and_marks_partial(tmp_path):
    p = cues.validate_protocol({"id": "t", "name": "T", "steps": [
        {"id": "a", "title": "A", "label": "cued_blink", "cues": {"type": "repeat", "every_s": 2, "count": 2, "action": "blink"}}]})
    log, clock = _log(tmp_path)
    sync = [{"p0_ms": 1000.0, "p1_ms": 1004.0, "t1_s": 0.002 + 5, "t2_s": 0.0021 + 5}]   # offset ~ +4 s
    r = log.start(p, "olga", {"user_agent": "x", "junk": {"a": 1}}, sync)
    assert r["status"] == "running" and r["client"] == {"user_agent": "x"} and r["clock_fit"]["n"] == 1
    clock["t"] = 12.0
    log.log(r["run_id"], [
        {"key": "s0.0.start", "kind": "step_start", "step": 0, "attempt": 0, "perf_ms": 2000.0},
        {"key": "s0.0.c0", "kind": "cue", "step": 0, "attempt": 0, "cue": 0, "action": "blink", "perf_ms": 5000.0},
        {"key": "s0.0.c0", "kind": "cue", "screen_perf_ms": 5012.0},          # same batch: merged
        {"kind": "bogus", "perf_ms": 1.0},                                    # dropped
        {"key": "s0.0.c1", "kind": "cue", "step": 0, "attempt": 0, "cue": 1, "action": "blink", "perf_ms": 7000.0,
         "evil": "<script>"}])
    clock["t"] = 13.0
    log.log(r["run_id"], [{"key": "s0.0.c1", "kind": "cue", "cancelled": True}])
    doc = cues.read_doc(tmp_path)
    run = doc["runs"][0]
    assert [e["key"] for e in run["entries"]] == ["s0.0.start", "s0.0.c0", "s0.0.c1"]
    c0 = run["entries"][1]
    off = run["clock_fit"]["a"]
    assert abs(off - 4.0) < 0.003 and abs(c0["t_s"] - (5.0 + off)) < 1e-6 and abs(c0["screen_t_s"] - (5.012 + off)) < 1e-6
    assert c0["t_recv_s"] == 12.0 and "evil" not in run["entries"][2] and run["entries"][2]["cancelled"]
    assert run["steps"][0]["status"] == "interrupted" and run["steps"][0]["n_cues"] == 1
    log.close_all("recording stopped")
    run = cues.read_doc(tmp_path)["runs"][0]
    assert run["status"] == "partial" and run["status_reason"] == "recording stopped" and run["ended_t_s"] == 13.0
    with pytest.raises(ValueError, match="already ended"):
        log.log(r["run_id"], [])
    rows = list(csv.DictReader(io.StringIO((tmp_path / cues.CUES_CSV).read_text())))
    assert rows[0].keys() >= {"run_id", "kind", "t_s", "action", "label", "run_status"}
    assert rows[1]["action"] == "blink" and rows[1]["label"] == "cued_blink" and rows[1]["run_status"] == "partial"


def test_new_run_closes_an_open_one(tmp_path):
    p = cues.validate_protocol({"id": "t", "name": "T", "steps": [{"id": "a", "title": "A", "duration_s": 5}]})
    log, _ = _log(tmp_path)
    a = log.start(p, None)
    b = log.start(p, None)
    assert a["status"] == "partial" and b["status"] == "running"
    log.end(b["run_id"], "completed", entries=[{"key": "s0.0.end", "kind": "step_end", "step": 0, "status": "done", "perf_ms": 1}])
    assert [r["status"] for r in cues.read_doc(tmp_path)["runs"]] == ["partial", "completed"]
    with pytest.raises(ValueError):
        log.end(b["run_id"], "finished")


# ---------------------------------------------------------------- live trial -> saved session
@pytest.fixture()
def app(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.web.app import create_app

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)
    s = load_settings(tmp_path)
    Catalog(s.db_path, s.data_root).upsert_subject({"subject_id": "S001"})
    return s, TestClient(create_app(s))


def _start_trial(c, device="sim_cam"):
    r = c.post("/trial/start", data={"devices": [device], "subject_id": "S001", "protocol": "rest", "trial": "1",
                                     "posture": "sitting", "operator": "ol"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


def _sync(c, tid, n=5):
    out = []
    for _ in range(n):
        p0 = time.perf_counter() * 1e3
        d = c.post(f"/api/trials/{tid}/cues/ping").json()
        out.append({"p0_ms": p0, "p1_ms": time.perf_counter() * 1e3, **d})
    return out


def test_cued_run_through_the_api(app):
    s, c = app
    protos = c.get("/api/cue-protocols").json()["protocols"]
    q = next(p for p in protos if p["id"] == "quick_check")
    tid = _start_trial(c)
    assert "Cued session" in c.get(f"/trials/{tid}").text
    sync = _sync(c, tid)
    assert all(x["t1_s"] <= x["t2_s"] for x in sync) and sync[-1]["t2_s"] > sync[0]["t1_s"]
    assert c.post(f"/api/trials/{tid}/cues/start", json={"protocol_id": "nope"}).status_code == 400
    r = c.post(f"/api/trials/{tid}/cues/start", json={"protocol_id": "quick_check", "sync": sync,
                                                       "client": {"user_agent": "pytest"}}).json()
    rid = r["run_id"]
    assert r["protocol"]["id"] == "quick_check" and r["clock_fit"]["n"] == 5
    # the "browser": perf_counter plays the role of performance.now(), so perf -> t_s is known exactly
    now = time.perf_counter() * 1e3
    entries = [{"key": "run.start", "kind": "run_start", "perf_ms": now},
               {"key": "s0.0.start", "kind": "step_start", "step": 0, "attempt": 0, "perf_ms": now}]
    for i, cue in enumerate(q["steps"][0]["cues"][:3]):
        entries.append({"key": f"s0.0.c{i}", "kind": "cue", "step": 0, "attempt": 0, "cue": i, "action": cue["action"],
                        "perf_ms": now + 300 * (i + 1)})
    assert c.post(f"/api/trials/{tid}/cues/{rid}/log", json={"entries": entries, "sync": _sync(c, tid, 2)}).json()["n_entries"] == 5
    assert c.post(f"/api/trials/{tid}/cues/zzz/log", json={"entries": []}).status_code == 409
    time.sleep(1.2)
    live = c.get(f"/api/trials/{tid}/cues").json()["runs"][0]
    assert live["status"] == "running" and live["steps"][0]["n_cues"] == 3
    # stopping the recording mid-run keeps the run, marked partial
    assert c.post(f"/api/trials/{tid}/stop").status_code == 200
    r = c.post(f"/api/trials/{tid}/cues/{rid}/end", json={"status": "aborted", "reason": "late"}).json()
    assert r["status"] == "partial"
    r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "keep": "on"}, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    sess = Catalog(s.db_path, s.data_root).session(sid)
    folder = s.data_root / sess["path"]
    assert (folder / "cues.json").exists() and (folder / "cues.csv").exists()
    assert any("cued session started" in (e.get("label") or "") for e in sess["events"])

    doc = c.get(f"/api/sessions/{sid}/cues").json()
    run = doc["runs"][0]
    assert run["status"] == "partial" and run["protocol"]["id"] == "quick_check" and run["operator"] == "local"
    cue_rows = [e for e in run["entries"] if e["kind"] == "cue"]
    assert len(cue_rows) == 3
    for e in cue_rows:
        assert 0 < e["t_s"] < sess["duration_s"] + 5 and e["t_recv_s"] >= 0
    gaps = np.diff([e["t_s"] for e in cue_rows])
    assert np.allclose(gaps, 0.3, atol=1e-6)
    assert run["clock_fit"]["delay_min_ms"] < 200
    # frame index next to every cue, for the labeling tool
    cam = json.loads((folder / "session.json").read_text())["video"][0]["stream"]
    doc = c.get(f"/api/sessions/{sid}/cues", params={"stream": cam}).json()
    assert doc["frames"]["n"] > 0 and all(isinstance(e["frame_i"], int) for e in doc["runs"][0]["entries"] if e["t_s"] is not None)
    rows = list(csv.DictReader(io.StringIO(c.get(f"/api/sessions/{sid}/cues", params={"format": "csv"}).text)))
    assert sum(r["kind"] == "cue" for r in rows) == 3
    page = c.get(f"/sessions/{sid}")
    assert page.status_code == 200 and "Cued sessions" in page.text


def test_cue_times_match_the_session_clock(app, monkeypatch):
    """Browser clock deliberately offset by a large amount: t_s still lands on the recorder's clock."""
    s, c = app
    tid = _start_trial(c, "sim")
    rec = c.app.state.manager.get(tid).rec
    shift_ms = 3_600_000.0 + 123.456          # the "browser" clock is 1 h and a bit ahead
    perf = lambda: time.monotonic() * 1e3 + shift_ms
    sync = []
    for _ in range(6):
        p0 = perf()
        d = c.post(f"/api/trials/{tid}/cues/ping").json()
        sync.append({"p0_ms": p0, "p1_ms": perf(), **d})
    rid = c.post(f"/api/trials/{tid}/cues/start", json={"protocol_id": "quick_check", "sync": sync}).json()["run_id"]
    p = perf()
    want = (p - shift_ms) / 1e3 - rec.t0_ns / 1e9     # the same moment on the session clock
    c.post(f"/api/trials/{tid}/cues/{rid}/log", json={"entries": [{"key": "k", "kind": "cue", "step": 0, "cue": 0, "perf_ms": p}]})
    r = c.post(f"/api/trials/{tid}/cues/{rid}/end", json={"status": "completed"}).json()
    assert r["status"] == "completed"
    e = c.get(f"/api/trials/{tid}/cues").json()["runs"][0]["entries"][0]
    fit = c.get(f"/api/trials/{tid}/cues").json()["runs"][0]["clock_fit"]
    assert abs(e["t_s"] - want) * 1e3 <= fit["err_ms"] + 1.0, (e["t_s"], want, fit)
    assert c.post(f"/api/trials/{tid}/stop").status_code == 200
