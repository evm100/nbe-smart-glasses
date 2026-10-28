"""Multi-user server: auth, roles, CSRF, node registry / PKI, provisioning API, MQTT hub logic."""
import io
import json
import shutil
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from sglab.auth import AuthError, AuthStore, hash_password, verify_password  # noqa: E402
from sglab.catalog import Catalog  # noqa: E402
from sglab.mqtt_hub import MqttHub, MqttSource  # noqa: E402
from sglab.nodes import NodeError, NodeRegistry  # noqa: E402
from sglab.pki import PKI  # noqa: E402
from sglab.protocol import encode  # noqa: E402
from sglab.settings import load_settings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://testserver"}
PW = "correct horse battery"


@pytest.fixture
def server(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(f'''
[auth]
enabled = true
[web]
allow_local_devices = true
[pki]
dir = "{tmp_path / 'pki'}"
[provisioning]
broker_uri = "mqtts://mqtt.example.org:8883"
''')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    PKI(s.pki_dir).init_ca()
    a = AuthStore(s.db_path)
    for u, role in (("ada", "admin"), ("olga", "operator"), ("vic", "viewer")):
        a.add_user(u, PW, role)
    from sglab.web.app import create_app
    return s, create_app(s)


def login(app, user):
    c = TestClient(app)
    r = c.post("/login", data={"username": user, "password": PW, "next": "/"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


# ---------------------------------------------------------------- auth store
def test_password_hashing():
    h = hash_password("hunter22hunter22")
    assert h.startswith("scrypt$") and verify_password("hunter22hunter22", h)
    assert not verify_password("hunter22hunter23", h) and not verify_password("x", "garbage")


def test_auth_store(tmp_path):
    a = AuthStore(tmp_path / "c.sqlite")
    with pytest.raises(AuthError):
        a.add_user("x", "short", "viewer")
    with pytest.raises(AuthError):
        a.add_user("bad name!", PW, "viewer")
    a.add_user("ev", PW, "admin")
    with pytest.raises(AuthError):
        a.login("ev", "wrong password!", 1)
    tok = a.login("ev", PW, 1)
    assert a.session_user(tok)["role"] == "admin"
    api = a.create_token("ev", "tool")
    assert api.startswith("nbe_") and a.token_user(api)["username"] == "ev"
    a.set_password("ev", PW + "!")  # logs out sessions, keeps API tokens
    assert a.session_user(tok) is None and a.token_user(api)
    a.set_role("ev", active=False)  # deactivation kills tokens too
    assert a.token_user(api) is None
    with pytest.raises(AuthError):
        a.login("ev", PW + "!", 1)


# ---------------------------------------------------------------- web: login, roles, CSRF
def test_login_required_and_roles(server):
    s, app = server
    anon = TestClient(app)
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert anon.get("/api/sessions").status_code == 401
    assert anon.get("/healthz").status_code == 200
    bad = anon.post("/login", data={"username": "ada", "password": "nope nope nope"}, headers=ORIGIN)
    assert bad.status_code == 401 and "wrong username or password" in bad.text

    vic = login(app, "vic")
    assert vic.get("/").status_code == 200 and vic.get("/devices").status_code == 200
    assert vic.get("/trial/new").status_code == 403
    assert vic.get("/admin/nodes").status_code == 403
    assert vic.get("/api/me").json()["role"] == "viewer"

    olga = login(app, "olga")
    assert olga.get("/trial/new").status_code == 200
    assert "Operator (your initials)" not in olga.get("/trial/new").text  # operator = the logged-in user
    assert olga.get("/admin/users").status_code == 403

    ada = login(app, "ada")
    for page in ("/admin/nodes", "/admin/users", "/admin/audit", "/account"):
        assert ada.get(page).status_code == 200, page


def test_csrf_origin_check(server):
    s, app = server
    olga = login(app, "olga")
    r = olga.post("/api/trials/x/stop")  # no Origin header -> refused before reaching the route
    assert r.status_code == 403 and "cross-site" in r.json()["error"]
    r = olga.post("/api/trials/x/stop", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert olga.post("/api/trials/x/stop", headers=ORIGIN).status_code == 404
    assert TestClient(app).post("/login", data={"username": "ada", "password": PW},
                                headers={"Origin": "https://evil.example"}).status_code == 403


def test_login_rate_limit(server):
    s, app = server
    from sglab.web.admin import limiter
    limiter.fail.clear()
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/login", data={"username": "vic", "password": "wrong wrong"}, headers=ORIGIN).status_code == 401
    assert c.post("/login", data={"username": "vic", "password": PW}, headers=ORIGIN).status_code == 429
    limiter.fail.clear()


def test_operator_trial_records_user(server):
    s, app = server
    Catalog(s.db_path, s.data_root).upsert_subject({"subject_id": "S001"})
    olga = login(app, "olga")
    r = olga.post("/trial/start", data={"devices": ["sim"], "subject_id": "S001", "protocol": "rest", "trial": "1",
                                        "posture": "sitting"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = r.headers["location"].rsplit("/", 1)[1]
    time.sleep(1.0)
    assert olga.post(f"/api/trials/{tid}/stop", headers=ORIGIN).status_code == 200
    r = olga.post(f"/trials/{tid}/finish", data={"quality": "good", "keep": "on"}, headers=ORIGIN, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    sess = Catalog(s.db_path, s.data_root).session(sid)
    assert sess["operator"] == "olga"
    doc = json.loads((s.data_root / sess["path"] / "session.json").read_text())
    assert doc["metadata"]["recorded_by"] == "olga"
    ada = login(app, "ada")
    actions = [r["action"] for r in AuthStore(s.db_path).audit_log()]
    assert "trial.start" in actions and "trial.save" in actions and "login" in actions
    assert "recorded by me" in olga.get("/").text and sid in olga.get("/?mine=true").text
    assert sid not in ada.get("/?mine=true").text


def test_files_only_serves_data(server):
    s, app = server
    vic = login(app, "vic")
    assert vic.get("/files/catalog.sqlite").status_code == 404
    assert vic.get("/files/../pki/ca.key").status_code == 404
    (s.raw_dir / "x").mkdir(parents=True)
    (s.raw_dir / "x" / "a.txt").write_text("hi")
    assert vic.get("/files/raw/x/a.txt").text == "hi"


# ---------------------------------------------------------------- nodes + provisioning API
def test_node_api_with_token(server):
    s, app = server
    c = TestClient(app)
    r = c.post("/api/auth/token", json={"username": "ada", "password": PW, "label": "laptop"})
    assert r.status_code == 200
    H = {"Authorization": "Bearer " + r.json()["token"]}
    assert c.get("/api/provisioning", headers=H).json()["broker_uri"] == "mqtts://mqtt.example.org:8883"
    n = c.post("/api/nodes", json={"node_id": "fng02", "site": "finger", "name": "finger #2"}, headers=H).json()
    assert n["node_id"] == "FNG02" and n["enabled"] == 1
    assert c.post("/api/nodes", json={"node_id": "FNG02"}, headers=H).status_code == 409
    assert c.post("/api/nodes", json={"node_id": "x"}, headers=H).status_code == 400
    b = c.get("/api/nodes/FNG02/credentials", headers=H).json()
    assert "BEGIN CERTIFICATE" in b["client_cert"] and "PRIVATE KEY" in b["client_key"]
    assert b["broker_ca_mode"] == "bundle" and b["topic_prefix"] == "nbe"
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(b["client_cert"].encode())
    assert cert.subject.rfc4514_string() == "CN=FNG02"
    old_fp = b["cert_fp"]
    assert c.post("/api/nodes/FNG02/rotate", headers=H).json()["cert_fp"] != old_fp
    assert c.patch("/api/nodes/FNG02", json={"enabled": False}, headers=H).json()["enabled"] == 0
    # a bearer token needs no Origin header, but still needs the role
    r = c.post("/api/auth/token", json={"username": "olga", "password": PW})
    assert c.get("/api/nodes", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 403
    assert c.get("/api/nodes", headers={"Authorization": "Bearer nbe_forged"}).status_code == 401


def test_node_web_admin_and_zip(server):
    s, app = server
    ada = login(app, "ada")
    r = ada.post("/admin/nodes", data={"node_id": "GLS01", "site": "glasses", "name": "glasses v1"}, headers=ORIGIN,
                 follow_redirects=False)
    assert r.status_code == 303
    assert "GLS01" in ada.get("/admin/nodes").text
    app.state.ctx.registry.set_mac("GLS01", "dc:da:0c:12:34:56")  # as reported by firmware >= 0.6.2
    assert "dc:da:0c:12:34:56" in ada.get("/admin/nodes").text
    z = zipfile.ZipFile(io.BytesIO(ada.get("/admin/nodes/GLS01/credentials.zip").content))
    names = set(z.namelist())
    assert {"main/certs/client.crt", "main/certs/client.key", "sdkconfig.nbe"} <= names
    cfg = z.read("sdkconfig.nbe").decode()
    assert 'CONFIG_PPG_DEVICE_ID="GLS01"' in cfg and "CONFIG_PPG_LINK_MQTT=y" in cfg
    assert 'CONFIG_PPG_SITE="glasses"' in cfg and "# CONFIG_PPG_LINK_USB_SERIAL_JTAG is not set" in cfg
    # node shows up as an (offline) device; no MQTT configured on this server
    devs = {d["key"]: d for d in ada.get("/api/devices").json()["devices"]}
    assert devs["mqtt://GLS01"]["online"] is False and devs["mqtt://GLS01"]["role"] == "glasses"
    r = ada.post("/admin/nodes/GLS01", data={"name": "glasses v1", "site": "glasses", "notes": ""}, headers=ORIGIN,
                 follow_redirects=False)  # enabled checkbox unticked -> disabled
    assert "mqtt://GLS01" not in {d["key"] for d in ada.get("/api/devices").json()["devices"]}


def test_node_registry_rules(tmp_path):
    PKI(tmp_path / "pki").init_ca()
    reg = NodeRegistry(tmp_path / "c.sqlite", tmp_path / "pki")
    for bad in ("A", "core", "NBE-CORE", "bad id", "x" * 40):
        with pytest.raises(NodeError):
            reg.register(bad)
    reg.register("fng01")
    assert (tmp_path / "pki" / "nodes" / "FNG01.key").stat().st_mode & 0o777 == 0o600
    with pytest.raises(NodeError):
        reg.register("FNG01")


def test_node_mac_is_remembered(tmp_path):
    import sqlite3
    PKI(tmp_path / "pki").init_ca()
    db = tmp_path / "c.sqlite"
    # a DB from before 0.6.2 (no mac column) gets the column added
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, name TEXT, site TEXT, notes TEXT, enabled INTEGER NOT NULL"
                " DEFAULT 1, created_by TEXT, created_at TEXT, cert_fp TEXT, cert_expires TEXT)")
    con.close()
    reg = NodeRegistry(db, tmp_path / "pki")
    reg.register("FNG01")
    assert reg.node("FNG01")["mac"] is None
    hub = MqttHub({"topic_prefix": "nbe"}, allowed=lambda: {"FNG01"}, client=FakeClient())
    hub.on_mac = reg.set_mac
    hub.handle("nbe/dev/FNG01/status", b'{"online": true, "mac": "DC:DA:0C:12:34:56"}')
    assert reg.node("FNG01")["mac"] == "dc:da:0c:12:34:56"
    hub.handle("nbe/dev/FNG01/status", b'{"online": false}', retain=True)  # last will: MAC kept
    assert reg.node("FNG01")["mac"] == "dc:da:0c:12:34:56"
    reg.set_mac("FNG01", "<script>")  # not a MAC -> ignored
    assert reg.node("FNG01")["mac"] == "dc:da:0c:12:34:56"


# ---------------------------------------------------------------- MQTT hub logic (no broker)
class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))


def test_hub_presence_routing_and_orphans(monkeypatch):
    fake = FakeClient()
    hub = MqttHub({"topic_prefix": "nbe"}, allowed=lambda: {"FNG01", "FNG02"}, client=fake)
    hub.connected = True
    now = time.time()
    # retained status from a node that died long ago (its own ts is old) -> not online
    hub.handle("nbe/dev/FNG01/status", json.dumps({"online": True, "ts": now - 3600}).encode(), retain=True)
    assert hub.node_state("FNG01")["online"] is False and hub.node_state("FNG01")["state"] == "stale"
    hub.handle("nbe/dev/FNG01/status", json.dumps({"online": True, "ts": now, "rssi": -60}).encode())
    st = hub.node_state("FNG01")
    assert st["online"] and st["rssi"] == -60 and not st["in_use"]
    hub.handle("nbe/dev/FNG01/status", b'{"online": false}', retain=True)  # last will
    assert hub.node_state("FNG01")["online"] is False
    hub.handle("nbe/dev/EVIL/status", b'{"online": true}')  # not registered -> ignored
    assert hub.node_state("EVIL")["state"] == "never seen" and hub.stats["ignored"] == 1

    src = MqttSource(hub, "FNG02")
    with pytest.raises(RuntimeError):
        MqttSource(hub, "FNG02")  # one trial per node
    src.write("start")
    assert fake.published[-1] == ("nbe/dev/FNG02/cmd", "START")
    lines = encode("D,0,1000,5,6") + encode("D,1,11000,7,8")
    hub.handle("nbe/dev/FNG02/data", lines.encode())
    assert src.readline().startswith("$D,0") and src.readline().startswith("$D,1") and src.readline() == ""
    src.close()

    # data from a node nobody is recording -> told to STOP (after the startup grace period)
    hub.t_start -= 60
    fake.published.clear()
    hub.handle("nbe/dev/FNG02/data", lines.encode())
    hub.handle("nbe/dev/FNG02/data", lines.encode())  # rate-limited: one STOP
    assert fake.published == [("nbe/dev/FNG02/cmd", "STOP")] and hub.stats["orphan_stops"] == 1


def test_recorder_tolerates_mqtt_reordering(tmp_path):
    """Redelivered / late batches: duplicates skipped, reorders counted, loss = real gaps only."""
    from sglab.recorder import StreamWorker

    class Src:
        port = "test"
        def write(self, c): pass
    w = StreamWorker(Src(), "finger", tmp_path, time.monotonic_ns())
    w._handle(encode("H,proto=1,dev=N1,fw=x,fs=100,ch=ir"), 0.0)
    order = list(range(0, 50)) + list(range(60, 80)) + list(range(50, 60)) + list(range(70, 75)) + [85]
    for i, q in enumerate(order):
        w._handle(encode(f"D,{q},{q * 10000},{q}"), i * 0.01)
    st = w.stats
    assert st["n_duplicates"] == 5 and st["n_out_of_order"] >= 1
    assert st["n_samples"] == 81 and st["n_dropped_seq"] == 5  # 80..84 missing


def test_failed_write_does_not_lock_the_database(tmp_path):
    """Regression: a duplicate-user INSERT left a transaction open -> 'database is locked' elsewhere."""
    db = tmp_path / "c.sqlite"
    a = AuthStore(db)
    a.add_user("ev", PW, "admin")
    with pytest.raises(AuthError):
        a.add_user("ev", PW, "admin")
    PKI(tmp_path / "pki").init_ca()
    reg = NodeRegistry(db, tmp_path / "pki")
    reg.con.execute("PRAGMA busy_timeout = 500")
    reg.register("FNG01")  # needs the write lock
    cat = Catalog(db, tmp_path)
    cat.upsert_subject({"subject_id": "S001"})


def test_hub_tells_source_when_node_goes_idle():
    hub = MqttHub({"topic_prefix": "nbe"}, allowed=lambda: {"FNG01"}, client=FakeClient())
    src = MqttSource(hub, "FNG01")
    hub.handle("nbe/dev/FNG01/status", b'{"online": true, "streaming": false}')
    assert src.idle.is_set()
    hub.handle("nbe/dev/FNG01/status", b'{"online": true, "streaming": true}')
    assert not src.idle.is_set()
    hub.handle("nbe/dev/FNG01/status", b'{"online": true, "streaming": false}', retain=True)  # after a hub reconnect
    assert src.idle.is_set()
    src.close()
    assert "FNG01" not in hub.idle_cbs


def test_recorder_restarts_quiet_stream_and_resumes(tmp_path, monkeypatch):
    """Node stops sending (rebooted): the worker sends START again; the new stream (seq from 0,
    device clock from ~0) continues the old one without false duplicates or a backwards clock."""
    import threading
    from sglab import recorder
    from sglab.recorder import StreamWorker

    class Src:
        port = "mqtt://N1"
        def __init__(self):
            self.sent, self.idle = [], threading.Event()
        def write(self, c):
            self.sent.append(c)
    src = Src()
    w = StreamWorker(src, "finger", tmp_path, time.monotonic_ns())
    w._open_csv = lambda hdr: setattr(w, "_writer", type("W", (), {"writerow": lambda self, r: rows.append(r)})())
    rows = []
    w._handle(encode("H,proto=1,dev=N1,fw=x,fs=100,ch=ir"), 0.0)
    w._handle(encode("E,50000000,START,"), 0.0)
    for q in range(300):  # 3 s, device clock at 50 s
        w._handle(encode(f"D,{q},{50_000_000 + q * 10000},{q}"), 0.5 + q * 0.01)

    # quiet, but the node has not said it is idle: no START until STALL_RESTART_S
    w._last_start_mono = 0
    w._last_sample_mono = time.monotonic() - recorder.IDLE_RESTART_S - 1
    w._maybe_restart()
    assert src.sent == []
    src.idle.set()  # node reconnected after a reboot and reports streaming=false
    w._maybe_restart()
    assert src.sent == ["START"] and w.stats["n_auto_restarts"] == 1 and not src.idle.is_set()
    w._maybe_restart()
    assert src.sent == ["START"]  # rate limited

    # quiet past STALL_RESTART_S, but the node's live status says streaming: late data, no START
    src.node_streaming = lambda: True
    w._last_start_mono = 0
    w._last_sample_mono = time.monotonic() - recorder.STALL_RESTART_S - 1
    w._maybe_restart()
    assert src.sent == ["START"]
    w._last_sample_mono = time.monotonic() - recorder.BACKLOG_RESTART_S - 1
    w._maybe_restart()
    assert src.sent == ["START", "START"]
    src.node_streaming = lambda: False

    # rebooted node: clock restarts near 0, seq restarts at 0
    w._handle(encode("E,2000000,BOOT,0.3.1"), 10.0)
    w._handle(encode("E,4000000,START,"), 10.0)
    for q in range(100):
        w._handle(encode(f"D,{q},{4_000_000 + q * 10000},{q}"), 10.0 + q * 0.01)
    st = w.stats
    assert st["n_auto_restarts"] == 2
    assert st["n_device_restarts"] == 1 and st["n_device_reboots"] == 1
    assert st["n_samples"] == 400 and st["n_duplicates"] == 0 and st["n_dropped_seq"] == 0
    t_dev = [r[2] for r in rows]
    assert all(b > a for a, b in zip(t_dev, t_dev[1:]))  # still monotonic
    # resumed device time follows the PC clock: last old sample at 3.49 s, new START at 10.0 s
    assert abs((t_dev[300] - t_dev[299]) / 1e6 - (10.0 - 3.49)) < 0.02
    fit = w.clock_fit()
    assert fit and abs(fit["b"] - 1) < 1e-3


def test_clock_fit_ignores_partial_edge_seconds(tmp_path):
    """MQTT bursts of ~1 s: the stream starts at device 4.99 s, so second 4 holds one sample that
    arrives with second 5's burst. That partial second's minimum sits ~0.4 s high and tilted b to ~0.92."""
    from sglab.recorder import StreamWorker

    class Src:
        port = "test"
        def write(self, c): pass
    w = StreamWorker(Src(), "finger", tmp_path, time.monotonic_ns())
    w._handle(encode("H,proto=1,dev=N1,fw=x,fs=100,ch=ir"), 0.0)
    for q in range(442):  # device 4.99 .. 9.40 s
        t_us = 4_990_000 + q * 10000
        burst = 0 if t_us < 5_410_000 else 1 + (t_us - 5_410_000) // 1_000_000
        w._handle(encode(f"D,{q},{t_us},{q}"), 0.83 + burst)
    fit = w.clock_fit()
    assert fit["n_bins"] == 4  # seconds 5..8; 4 and 9 are partial
    assert abs(fit["b"] - 1) < 0.01, fit


def _app_image(cert_pem: str, version: str = "0.4.0-idf", project: str = "ppg_stream") -> bytes:
    """Minimal ESP-IDF app image: header, esp_app_desc_t at 0x20, then the embedded node cert."""
    import struct
    desc = struct.pack("<II8x", 0xABCD5432, 0) + version.encode().ljust(32, b"\0") + project.encode().ljust(32, b"\0")
    desc += b"12:00:00".ljust(16, b"\0") + b"Oct  8 2026".ljust(16, b"\0") + b"v6.1".ljust(32, b"\0")
    desc += bytes.fromhex("deadbeef") + bytes(28)
    return bytes([0xE9]) + bytes(0x1F) + desc.ljust(256, b"\0") + cert_pem.encode() + b"\0" + bytes(1000)


def test_firmware_upload_and_ota(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(f'''
[auth]
enabled = true
[web]
public_url = "https://nbe.example.org"
[pki]
dir = "{tmp_path / 'pki'}"
''')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    PKI(s.pki_dir).init_ca()
    AuthStore(s.db_path).add_user("ada", PW, "admin")
    fake = FakeClient()
    hub = MqttHub({"topic_prefix": "nbe"}, client=fake)
    from sglab.web.app import create_app
    app = create_app(s, hub=hub)
    c = TestClient(app)
    H = {"Authorization": "Bearer " + c.post("/api/auth/token", json={"username": "ada", "password": PW}).json()["token"]}
    for nid in ("GLS01", "FNG01"):
        c.post("/api/nodes", json={"node_id": nid, "site": "glasses"}, headers=H)
    b = c.get("/api/nodes/GLS01/credentials", headers=H).json()
    assert b["ota_url"] == "https://nbe.example.org/ota"
    from sglab.provision import sdkconfig_text, sdkconfig_values
    assert 'CONFIG_PPG_OTA_URL="https://nbe.example.org/ota"' in sdkconfig_text(sdkconfig_values(b))

    img = _app_image(b["client_cert"])
    # wrong node / not an app image / another project are refused
    assert c.put("/api/nodes/FNG01/firmware", content=img, headers=H).status_code == 400
    assert c.put("/api/nodes/GLS01/firmware", content=b"\xe9" + bytes(500), headers=H).status_code == 400
    assert c.put("/api/nodes/GLS01/firmware", content=_app_image(b["client_cert"], project="blink"),
                 headers=H).status_code == 400
    m = c.put("/api/nodes/GLS01/firmware", content=img, headers=H).json()
    assert m["version"] == "0.4.0-idf" and m["build"] == "deadbeef" and m["size"] == len(img)
    assert (s.pki_dir / "firmware" / "GLS01.bin").stat().st_mode & 0o077 == 0

    # offline node -> refused; online -> OTA command with a token that downloads the image
    assert c.post("/api/nodes/GLS01/ota", headers=H).status_code == 409
    hub.handle("nbe/dev/GLS01/status", json.dumps({"online": True, "ts": time.time(), "build": "0badc0de"}).encode())
    r = c.post("/api/nodes/GLS01/ota", headers=H)
    assert r.status_code == 200, r.text
    topic, cmd = fake.published[-1]
    assert topic == "nbe/dev/GLS01/cmd" and cmd.startswith("OTA ") and len(cmd) == 36
    anon = TestClient(app)
    d = anon.get(f"/ota/{cmd[4:]}")
    assert d.status_code == 200 and d.content == img
    assert anon.get("/ota/" + "0" * 32).status_code == 404
    assert anon.get("/ota/").status_code in (401, 404)  # no listing
    row = next(n for n in c.get("/api/nodes", headers=H).json()["nodes"] if n["node_id"] == "GLS01")
    assert row["image"]["build"] == "deadbeef"
    # web page shows the button (bearer: the https public_url makes the session cookie Secure)
    page = c.get("/admin/nodes", headers=H).text
    assert "/admin/nodes/GLS01/ota" in page and "deadbeef" in page
    # a node in a trial is not updated
    app.state.ctx.manager.busy = lambda: {"mqtt://GLS01": {"owner": "x"}}
    assert c.post("/api/nodes/GLS01/ota", headers=H).status_code == 409
