"""Auto-cuff: server scheduler (sglab.autocuff) + the Pi's MQTT node (auto-cuff/cuff_node.py), wired together."""
import csv
import json
import shutil
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from sglab import autocuff  # noqa: E402
from sglab.auth import AuthStore  # noqa: E402
from sglab.catalog import Catalog  # noqa: E402
from sglab.mqtt_hub import MqttHub  # noqa: E402
from sglab.pki import PKI  # noqa: E402
from sglab.settings import load_settings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PW = "correct horse battery"


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))


@pytest.fixture
def lab(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(
        f'[auth]\nenabled = true\n[web]\nallow_local_devices = true\n[pki]\ndir = "{tmp_path / "pki"}"\n'
        '[provisioning]\nbroker_uri = "mqtts://broker.example:8884"\n')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    PKI(s.pki_dir).init_ca()
    a = AuthStore(s.db_path)
    a.add_user("ada", PW, "admin")
    a.add_user("olga", PW, "operator")
    Catalog(s.db_path, s.data_root).upsert_subject({"subject_id": "S001"})
    fake = FakeClient()
    hub = MqttHub({"topic_prefix": "nbe"}, client=fake)
    hub.connected = True
    from sglab.web.app import create_app
    app = create_app(s, hub=hub)
    c = TestClient(app)  # no `with`: lifespan (and the scheduler thread) don't run; tests call tick()
    tok = {u: {"Authorization": "Bearer " + c.post("/api/auth/token", json={"username": u, "password": PW}).json()["token"]}
           for u in ("ada", "olga")}
    assert c.post("/api/nodes", json={"node_id": "CUF01", "site": "cuff"}, headers=tok["ada"]).status_code == 200
    return types.SimpleNamespace(s=s, app=app, c=c, tok=tok, hub=hub, fake=fake, cm=app.state.ctx.cuffs)


def cuff_status(hub, **kw):
    st = {"online": True, "kind": "cuff", "cuff_state": "idle", "ts": time.time(), **kw}
    hub.handle("nbe/dev/CUF01/status", json.dumps(st).encode())


def start_trial(lab):
    r = lab.c.post("/trial/start", data={"devices": ["sim"], "subject_id": "S001", "protocol": "rest", "trial": "1",
                                         "posture": "sitting"}, headers=lab.tok["olga"], follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


def cmds(lab):
    return [p for t, p in lab.fake.published if t == "nbe/dev/CUF01/cmd"]


def test_cuff_is_not_a_stream_and_shows_on_the_live_page(lab):
    cuff_status(lab.hub)
    devs = lab.c.get("/api/devices", headers=lab.tok["olga"]).json()["devices"]
    assert not any(d.get("node_id") == "CUF01" for d in devs)
    tid = start_trial(lab)
    st = lab.c.get(f"/api/trials/{tid}/cuff", headers=lab.tok["olga"]).json()
    assert st["binding"] is None and [x["node_id"] for x in st["cuffs"]] == ["CUF01"] and st["cuffs"][0]["online"]
    assert "Automatic cuff" in lab.c.get(f"/trials/{tid}", headers=lab.tok["olga"]).text
    lab.c.post(f"/api/trials/{tid}/stop", headers=lab.tok["olga"])


def test_scheduled_readings_end_up_as_cuff_events(lab):
    cuff_status(lab.hub)
    tid = start_trial(lab)
    H = lab.tok["olga"]
    api = f"/api/trials/{tid}/cuff"
    assert lab.c.post(api, json={"node_id": "CUF01", "interval_s": 30}, headers=H).status_code == 400  # too short
    r = lab.c.post(api, json={"node_id": "CUF01", "interval_s": 120, "enabled": True}, headers=H)
    assert r.status_code == 200 and r.json()["enabled"]

    lab.cm.tick()  # switched on -> first reading right away
    sent = cmds(lab)
    assert len(sent) == 1 and sent[0].startswith("MEASURE ")
    req = sent[0].split()[1]
    lab.cm.tick()
    assert len(cmds(lab)) == 1  # not twice while the first is in flight

    cuff_status(lab.hub, cuff_state="measuring")
    time.sleep(2.5)  # (a press from before the trial started would be clamped to t = 0)
    t_press = time.time() - 2.0  # device says it pressed 2 s ago
    lab.hub.handle("nbe/dev/CUF01/data", json.dumps({"type": "started", "req": req, "t_unix": t_press}).encode())
    assert lab.c.get(api, headers=H).json()["binding"]["phase"] == "measuring"
    time.sleep(0.3)
    lab.hub.handle("nbe/dev/CUF01/data", json.dumps(
        {"type": "reading", "req": req, "t_unix": time.time(), "sys": 121, "dia": 79, "bpm": 66, "mov": 1, "ihb": 0,
         "device_time": "2026-11-17 14:03:22"}).encode())
    cuff_status(lab.hub)
    b = lab.c.get(api, headers=H).json()["binding"]
    assert b["phase"] == "idle" and b["n_ok"] == 1 and b["last_reading"]["sys"] == 121
    assert 55 <= b["next_in_s"] <= 120  # 120 s start to start, at least 60 s rest

    # a stale message for an old request is ignored
    lab.hub.handle("nbe/dev/CUF01/data", json.dumps({"type": "reading", "req": "OLD", "sys": 1, "dia": 1}).encode())
    assert lab.c.get(api, headers=H).json()["binding"]["n_ok"] == 1

    # an implausible value is a failure, not a bp event
    assert lab.c.post(api + "/measure", headers=H).status_code == 200
    req2 = cmds(lab)[-1].split()[1]
    lab.hub.handle("nbe/dev/CUF01/data", json.dumps({"type": "started", "req": req2, "t_unix": time.time()}).encode())
    lab.hub.handle("nbe/dev/CUF01/data", json.dumps({"type": "reading", "req": req2, "sys": 80, "dia": 90}).encode())
    b = lab.c.get(api, headers=H).json()["binding"]
    assert b["n_fail"] == 1 and "implausible" in b["last_error"]

    assert lab.c.post(api, json={"enabled": False}, headers=H).json()["enabled"] is False
    lab.c.post(f"/api/trials/{tid}/stop", headers=H)
    r = lab.c.post(f"/trials/{tid}/finish", data={"quality": "good", "keep": "on"}, headers=H, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    sess = Catalog(lab.s.db_path, lab.s.data_root).session(sid)
    with open(lab.s.data_root / sess["path"] / "events.csv") as f:
        ev = list(csv.DictReader(f))
    auto = [e for e in ev if e["source"] == "auto-cuff:CUF01"]
    assert [e["kind"] for e in auto] == ["cuff_start", "bp", "cuff_start"]
    start, bp = float(auto[0]["t_s"]), auto[1]
    assert bp["sys_mmhg"] == "121.0" and "mov=1" in bp["label"] and "monitor 2026-11-17" in bp["label"]
    assert 1.5 <= float(bp["t_s"]) - start <= 4.0  # placed by the device's timestamps, not arrival order
    assert any("reading failed" in e["label"] for e in ev if e["kind"] == "marker")

    from ppg.layer3 import reference_readings
    rr = reference_readings(ev)
    assert len(rr) == 1 and bool(rr.iloc[0]["cuff_found"]) and rr.iloc[0]["t_cuff_start"] == pytest.approx(start)
    assert lab.cm.bindings == {}


def test_no_answer_and_abort(lab, monkeypatch):
    cuff_status(lab.hub)
    tid = start_trial(lab)
    H = lab.tok["olga"]
    api = f"/api/trials/{tid}/cuff"
    lab.c.post(api, json={"node_id": "CUF01", "enabled": True}, headers=H)
    lab.cm.tick()
    b = lab.cm.bindings["CUF01"]
    b.sent_at -= autocuff.ACK_TIMEOUT_S + 1
    lab.cm.tick()
    st = lab.c.get(api, headers=H).json()["binding"]
    assert st["n_fail"] == 1 and "no answer" in st["last_error"] and st["phase"] == "idle"

    # offline cuff: the scheduler waits and says why
    b.next_due = 0
    lab.hub.handle("nbe/dev/CUF01/status", b'{"online": false, "kind": "cuff"}', retain=True)
    n = len(cmds(lab))
    lab.cm.tick()
    assert len(cmds(lab)) == n and "offline" in lab.cm.bindings["CUF01"].last_error

    cuff_status(lab.hub)
    assert lab.c.post(api + "/abort", headers=H).status_code == 400  # nothing running
    assert lab.c.post(api + "/measure", headers=H).status_code == 200
    assert lab.c.post(api + "/abort", headers=H).status_code == 200 and cmds(lab)[-1] == "ABORT"
    req = cmds(lab)[-2].split()[1]
    lab.hub.handle("nbe/dev/CUF01/data", json.dumps({"type": "aborted", "req": req, "error": "aborted by the operator"}).encode())
    assert lab.cm.bindings["CUF01"].phase == "idle"
    lab.c.post(f"/api/trials/{tid}/stop", headers=H)


# ---------------------------------------------------------------- the Pi's node, against the real hub
def fake_cuff_server(results):
    """Stand-in for auto-cuff/cuff_server.py (no servo, no Bluetooth)."""
    m = types.ModuleType("cuff_server")

    class MeasureError(Exception):
        pass

    class Aborted(MeasureError):
        pass

    m.MeasureError, m.Aborted = MeasureError, Aborted
    m.DEVICE, m.MAC = "HEM-7150T", "28:FF:B2:31:CA:D5"
    m.DATA_DIR = Path("/tmp")
    m.OMBLEPY_DIR = Path(__file__)  # anything with omblepy.py "inside" is checked; patched below
    m.STATE_FILE = None
    m.SeenStore = lambda path: None

    def measure(seen, on_phase=None, abort=None):
        on_phase("started", time.time())
        on_phase("downloading", time.time())
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return [r]

    m.measure = measure
    return m


def test_pi_node_speaks_the_servers_protocol(lab, tmp_path, monkeypatch):
    pytest.importorskip("paho.mqtt.client")
    sys.path.insert(0, str(REPO / "auto-cuff"))
    try:
        import cuff_node
    finally:
        sys.path.pop(0)
    results = [{"received_at": "2026-11-17T14:04:00+00:00", "device_time": "2026-11-17 14:03:22",
                "sys": 117, "dia": 75, "bpm": 71, "mov": 0, "ihb": 0}]
    fake_cs = fake_cuff_server(results)
    (tmp_path / "omb").mkdir()
    (tmp_path / "omb" / "omblepy.py").write_text("")
    fake_cs.OMBLEPY_DIR, fake_cs.DATA_DIR = tmp_path / "omb", tmp_path / "data"
    monkeypatch.setitem(sys.modules, "cuff_server", fake_cs)

    # credentials as the server hands them out (credentials.zip)
    from sglab import provision
    ctx = lab.app.state.ctx
    b = provision.bundle(lab.s, ctx.registry, "CUF01")
    import io
    import zipfile
    zipfile.ZipFile(io.BytesIO(provision.bundle_zip(b))).extractall(tmp_path / "node")
    cfg = cuff_node.load_config(tmp_path / "node")
    assert (cfg["node_id"], cfg["site"], cfg["host"], cfg["port"], cfg["prefix"]) == \
        ("CUF01", "cuff", "broker.example", 8884, "nbe")

    node = cuff_node.CuffNode(cfg)
    hub = lab.hub

    class Wire:  # node -> hub
        def publish(self, topic, payload, qos=0, retain=False):
            hub.handle(topic, payload.encode(), retain=False)

        def is_connected(self):
            return True

    node.client = Wire()
    lab.fake.publish = lambda topic, payload, qos=0, retain=False: (  # hub -> node
        node._on_message(None, None, types.SimpleNamespace(payload=payload.encode())) if topic.endswith("/cmd") else None)
    node.publish_status()
    assert hub.node_state("CUF01")["online"] and hub.node_state("CUF01")["cuff_state"] == "idle"

    tid = start_trial(lab)
    H = lab.tok["olga"]
    lab.c.post(f"/api/trials/{tid}/cuff", json={"node_id": "CUF01"}, headers=H)
    assert lab.c.post(f"/api/trials/{tid}/cuff/measure", headers=H).status_code == 200
    for _ in range(50):  # the node measures in its own thread
        if lab.cm.bindings["CUF01"].n_ok:
            break
        time.sleep(0.05)
    b = lab.c.get(f"/api/trials/{tid}/cuff", headers=H).json()["binding"]
    assert b["n_ok"] == 1 and b["last_reading"]["sys"] == 117
    ev = lab.c.get(f"/api/trials/{tid}", headers=H).json()["events"]
    assert [e["kind"] for e in ev if e["source"] == "auto-cuff:CUF01"] == ["cuff_start", "bp"]
    assert hub.node_state("CUF01")["cuff_state"] == "idle"
    assert threading.active_count() >= 1
    lab.c.post(f"/api/trials/{tid}/stop", headers=H)
