"""Acquisition protocol, recorder, catalog, processing and web API."""
import json
import shutil
import time
from pathlib import Path

import pytest

from sglab.catalog import Catalog
from sglab.commands import CommandError, parse_command
from sglab.protocol import Event, Header, ProtocolError, Sample, encode, parse_line
from sglab.recorder import Recording
from sglab.settings import load_settings

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- protocol
def test_protocol_roundtrip():
    h = parse_line(encode("H,proto=1,dev=FNG01,fw=0.1,fs=400,ch=ir|red,led_ma=6.4|6.4"))
    assert isinstance(h, Header) and h.fs == 400 and h.channels == ["ir", "red"] and h.extra["led_ma"] == "6.4|6.4"
    d = parse_line(encode("D,7,123456,1000,2000"))
    assert isinstance(d, Sample) and d.seq == 7 and d.values == [1000, 2000]
    e = parse_line(encode("E,99,BTN,"))
    assert isinstance(e, Event) and e.code == "BTN"
    assert parse_line("hello from debug print") is None


def test_protocol_rejects_corruption():
    line = encode("D,7,123456,1000,2000")
    with pytest.raises(ProtocolError) as ei:
        parse_line(line.replace("1000", "1001"))
    assert ei.value.kind == "checksum"
    with pytest.raises(ProtocolError):
        parse_line("$D,7,123456,1000,2000")  # no checksum


def test_doc_examples_have_valid_checksums():
    text = (REPO / "firmware" / "PROTOCOL.md").read_text()
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("$") and "*" in l and "<" not in l]
    assert len(lines) >= 4
    for l in lines:
        parse_line(l)  # raises if a doc example is wrong


# ---------------------------------------------------------------- commands
@pytest.mark.parametrize("text,kind,vals", [
    ("bp 120/80", "bp", {"sys_mmhg": 120, "dia_mmhg": 80}),
    ("bp 118 76 70", "bp", {"sys_mmhg": 118, "dia_mmhg": 76, "hr_bpm": 70}),
    ("ox 97 64", "spo2", {"spo2_pct": 97, "hr_bpm": 64}),
    ("m start cold pressor", "marker", {"label": "start cold pressor"}),
    ("c", "cuff_start", {}),
])
def test_commands(text, kind, vals):
    action, p = parse_command(text)
    assert action == "event" and p["kind"] == kind
    for k, v in vals.items():
        assert p[k] == v


@pytest.mark.parametrize("text", ["bp 80/120", "bp 400/80", "ox 120", "m", "xyz"])
def test_commands_reject(text):
    with pytest.raises(CommandError):
        parse_command(text)


# ---------------------------------------------------------------- recorder + catalog
@pytest.fixture()
def lab(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)  # a developer's own deployment config
    s = load_settings(tmp_path)
    return s, Catalog(s.db_path, s.data_root)


def _record(s, devices, seconds, sim=None, events=()):
    rec = Recording(s, [s.device(d) for d in devices], sim_kw=sim or {})
    warnings = rec.start({"subject_id": "S001", "protocol": "rest"})
    assert not warnings
    t0 = time.monotonic()
    for t, kind, kw in events:
        time.sleep(max(0, t - (time.monotonic() - t0)))
        rec.add_event(kind, **kw)
    time.sleep(max(0, seconds - (time.monotonic() - t0)))
    rec.stop()
    return rec


def test_record_two_devices_and_catalog(lab):
    s, cat = lab
    rec = _record(s, ["sim", "sim_glasses"], 4.0, events=[(1.0, "bp", {"sys_mmhg": 120, "dia_mmhg": 80})])
    dest, doc = rec.finalize({"subject_id": "S001", "protocol": "rest", "trial": 1, "quality": "good", "keep": True})
    assert dest.name.endswith("_S001_rest_T01") and dest.parent == s.raw_dir
    assert not any(s.incoming_dir.iterdir())
    files = {p.name for p in dest.iterdir()}
    assert {"session.json", "events.csv", "finger_SIMF01.csv", "glasses_SIMG01.csv",
            "finger_SIMF01.rawlog", "glasses_SIMG01.rawlog"} <= files
    for st in doc["streams"]:
        assert st["stats"]["n_samples"] > 250            # ~100 Hz for ~3 s
        assert st["stats"]["n_dropped_seq"] == 0
        assert abs(st["clock_fit"]["b"] - 1) < 0.01      # device and PC clocks agree
    sid = cat.upsert_session(dest)
    x = cat.session(sid)
    assert len(x["streams"]) == 2
    assert [e["kind"] for e in x["events"] if e["kind"] == "bp"] == ["bp"]
    assert cat.next_trial("S001", "rest") == 2
    # reindex from files alone gives the same catalog
    s.db_path.unlink()
    cat2 = Catalog(s.db_path, s.data_root)
    assert cat2.reindex() == 1 and cat2.session(sid)["streams"][1]["n_samples"] > 250


def test_live_rate_ignores_burst_delivery(tmp_path):
    """MQTT bursts: 1 s of samples arrives at once; the live rate must still read ~400, and
    real loss must still show."""
    from sglab.recorder import StreamWorker

    class Src:
        port = "mqtt://N1"

        def write(self, cmd):
            pass

    w = StreamWorker(Src(), "finger", tmp_path, time.monotonic_ns())
    w._open_csv = lambda hdr: setattr(w, "_writer", type("W", (), {"writerow": lambda self, r: None})())
    w._handle(encode("H,proto=1,dev=N1,fw=x,fs=400,ch=ir"), 0.0)
    assert w._device_rate() is None
    q = 0
    for burst in range(8):                       # 8 bursts of 1 s, all lines of a burst at one PC time
        for _ in range(400):
            if not (burst == 6 and q % 4 == 0):  # burst 6 loses a quarter of its samples
                w._handle(encode(f"D,{q},{1_000_000 + q * 2500},{q}"), burst + 1.3)
            q += 1
        if burst == 5:
            assert w._device_rate() == pytest.approx(400)
    assert w._device_rate() == pytest.approx((4 * 400 + 300) / 5)


def test_link_faults_are_counted(lab):
    s, cat = lab
    rec = _record(s, ["sim"], 3.0, sim={"drop_prob": 0.05, "corrupt_prob": 0.05, "seed": 3})
    st = rec.workers[0].summary()["stats"]
    assert st["n_checksum_err"] > 0
    assert st["n_dropped_seq"] > 0  # dropped lines and corrupted lines both leave seq gaps


def test_process_and_web_api(lab):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.processing import process_session
    from sglab.web.app import create_app

    s, cat = lab
    rec = _record(s, ["sim"], 35.0, events=[(20, "bp", {"sys_mmhg": 121, "dia_mmhg": 79, "hr_bpm": 72})])
    dest, _ = rec.finalize({"subject_id": "S001", "protocol": "rest", "trial": 1, "keep": True})
    sid = cat.upsert_session(dest)
    res = process_session(s, cat, sid, log=lambda *_: None)
    assert res[0]["status"] == "ok"
    assert abs(res[0]["layer2"]["median_hr_bpm"] - 72) < 3
    assert process_session(s, cat, sid, log=lambda *_: None) == []  # up to date -> skipped

    c = TestClient(create_app(s))
    assert c.get("/").status_code == 200 and sid in c.get("/").text
    assert c.get(f"/sessions/{sid}").status_code == 200
    assert c.get("/subjects").status_code == 200
    stream = cat.session(sid)["streams"][0]["stream"]
    sig = c.get(f"/api/sessions/{sid}/signal", params={"stream": stream}).json()
    assert len(sig["t"]) > 1000 and sig["unit"] == "AC/DC (%)"
    # processed time is mapped onto the session clock (recording starts ~1 s in)
    assert 0 < sig["t"][0] < 5
    raw = c.get(f"/api/sessions/{sid}/signal", params={"stream": stream, "source": "raw", "max_points": 500}).json()
    assert len(raw["t"]) <= 500
    hr = c.get(f"/api/sessions/{sid}/hr").json()
    assert hr and len(hr[0]["beats_t"]) > 20
    ep = c.get(f"/api/sessions/{sid}/epochs", params={"stream": stream}).json()
    assert "dia_rel" in ep["features"]
    assert c.get("/files/../config/lab.toml").status_code == 404


# ---------------------------------------------------------------- web-controlled trial
def test_web_trial_lifecycle(lab):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.web.app import create_app

    s, cat = lab
    c = TestClient(create_app(s))
    assert c.get("/trial/new").status_code == 200
    assert c.get("/api/trials").json() == []
    devs = {d["key"]: d for d in c.get("/api/devices").json()["devices"]}
    assert devs["sim"]["available"] and devs["sim"]["kind"] == "sim"

    # validation errors re-render the form instead of starting
    r = c.post("/trial/start", data={"devices": ["sim"], "subject_id": "S404", "protocol": "rest",
                                     "trial": "1", "posture": "sitting", "operator": "EV"})
    assert r.status_code == 400 and "unknown subject" in r.text and c.get("/api/trials").json() == []

    r = c.post("/trial/start", data={"devices": ["sim", "sim_glasses"], "subject_id": "__new__",
                                     "new_subject_id": "S077", "subj_sex": "F", "subj_age_years": "30",
                                     "protocol": "cold_pressor", "trial": "1", "posture": "sitting",
                                     "operator": "EV"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/trials/")
    tid = r.headers["location"].rsplit("/", 1)[1]
    api = f"/api/trials/{tid}"
    assert cat.subject("S077")["age_years"] == 30
    # the devices are now busy: a second trial can't take them, but the form still loads
    busy = {d["key"]: d for d in c.get("/api/devices").json()["devices"]}
    assert busy["sim"]["busy"]["trial"] == tid and not busy["sim"]["available"]
    r2 = c.post("/trial/start", data={"devices": ["sim"], "subject_id": "S077", "protocol": "rest", "trial": "2",
                                      "posture": "sitting", "operator": "EV"})
    assert r2.status_code == 400 and "already in use" in r2.text
    assert "Recording" in c.get("/").text  # banner on every page

    time.sleep(2.0)
    st = c.get(api).json()
    assert st["state"] == "recording" and len(st["streams"]) == 2
    assert all(x["samples"] > 50 for x in st["streams"])
    live = c.get(api + "/live").json()
    assert len(live) == 2 and len(live[0]["t"]) > 50
    # incremental: only points after the cursors, none twice
    time.sleep(0.3)
    more = c.get(api + "/live", params={"after": ",".join(str(x["next"]) for x in live)}).json()
    assert all(m["next"] >= x["next"] for m, x in zip(more, live)) and len(more[0]["t"]) > 0
    assert more[0]["t"][0] > live[0]["t"][-1] - 0.05 and more[0]["now"] >= live[0]["now"]
    assert c.get(api + "/live", params={"after": "x"}).status_code == 400
    assert "Stop" in c.get(f"/trials/{tid}").text

    assert c.post(api + "/event", json={"kind": "bp", "sys_mmhg": 120, "dia_mmhg": 80}).status_code == 200
    bad = c.post(api + "/event", json={"kind": "bp", "sys_mmhg": 70, "dia_mmhg": 90})
    assert bad.status_code == 400 and "diastolic" in bad.json()["error"]
    assert c.post(api + "/event", json={"kind": "marker", "label": "x"}).status_code == 200

    assert c.post(api + "/stop").json()["state"] == "stopped"
    assert c.get("/api/devices").json()["devices"][0]["busy"] is None  # released at stop
    assert "Save session" in c.get(f"/trials/{tid}").text
    r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "notes": "api test", "keep": "on"},
               follow_redirects=False)
    assert r.status_code == 303
    sid = r.headers["location"].rsplit("/", 1)[1]
    x = cat.session(sid)
    assert x["subject_id"] == "S077" and x["notes"] == "api test" and x["status"] == "ok"
    assert len(x["streams"]) == 2
    assert [e["kind"] for e in x["events"] if e["source"] == "operator"] == ["bp", "marker"]
    assert c.get("/api/trials").json() == []
    assert c.get(f"/trials/{tid}", follow_redirects=False).headers["location"] == f"/sessions/{sid}"
    assert c.get(api).status_code == 410


def test_two_trials_at_once(lab):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.web.app import create_app

    s, cat = lab
    cat.upsert_subject({"subject_id": "S001"})
    cat.upsert_subject({"subject_id": "S002"})
    c = TestClient(create_app(s))
    base = {"protocol": "rest", "trial": "1", "posture": "sitting", "operator": "EV"}
    tids = []
    for dev, subj in (("sim", "S001"), ("sim_glasses", "S002")):
        r = c.post("/trial/start", data={**base, "devices": [dev], "subject_id": subj}, follow_redirects=False)
        assert r.status_code == 303, r.text
        tids.append(r.headers["location"].rsplit("/", 1)[1])
    assert len(c.get("/api/trials").json()) == 2
    time.sleep(1.5)
    sids = []
    for tid in tids:
        assert c.post(f"/api/trials/{tid}/stop").status_code == 200
        r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "keep": "on"}, follow_redirects=False)
        sids.append(r.headers["location"].rsplit("/", 1)[1])
    assert {cat.session(x)["subject_id"] for x in sids} == {"S001", "S002"}


def test_token_gate(lab):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sglab.web.app import create_app

    s, _ = lab
    c = TestClient(create_app(s, token="secret123"))
    assert c.get("/").status_code == 401
    assert c.get("/api/trials").status_code == 401
    r = c.get("/?token=secret123", follow_redirects=False)
    assert r.status_code == 303 and "sglab_token" in r.headers["set-cookie"]
    c.cookies.set("sglab_token", "secret123")
    assert c.get("/").status_code == 200
    assert TestClient(create_app(s, token="secret123")).get(
        "/api/trials", headers={"x-sglab-token": "secret123"}).status_code == 200


def test_lost_header_is_requested_again(tmp_path):
    """The H line got lost (node buffer overflowed): samples arrive without it, so keep asking."""
    from sglab import recorder
    from sglab.recorder import StreamWorker

    class Src:
        port = "mqtt://N1"
        sent = []

        def write(self, cmd):
            self.sent.append(cmd)

    w = StreamWorker(Src(), "finger", tmp_path, time.monotonic_ns())
    for q in range(3):
        w._handle(encode(f"D,{q},{q * 2500},{q}"), 0.0)
    assert Src.sent == ["INFO"]
    w._last_info_mono -= recorder.INFO_EVERY_S
    w._handle(encode("D,3,7500,3"), 0.0)
    assert Src.sent == ["INFO", "INFO"] and w.stats["n_pre_header"] == 4
