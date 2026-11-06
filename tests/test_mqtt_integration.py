"""End to end over a real mosquitto broker with mutual TLS and the per-node ACL from deploy/.

core (sglab web app, CN=nbe-core)  <--mTLS-->  mosquitto  <--mTLS-->  simulated node (CN=FNG02)

Skipped when mosquitto or paho-mqtt aren't installed.
"""
import getpass
import json
import shutil
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

paho = pytest.importorskip("paho.mqtt.client")
pytest.importorskip("fastapi")
MOSQUITTO = shutil.which("mosquitto") or ("/usr/sbin/mosquitto" if Path("/usr/sbin/mosquitto").exists() else None)
pytestmark = pytest.mark.skipif(MOSQUITTO is None, reason="mosquitto not installed")

REPO = Path(__file__).resolve().parents[1]


def _server_cert(d: Path):
    """A throwaway 'public' CA + broker certificate for localhost (stands in for Let's Encrypt)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Public CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - timedelta(1)).not_valid_after(now + timedelta(30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    crt = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
           .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
           .not_valid_before(now - timedelta(1)).not_valid_after(now + timedelta(30))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (d / "public_ca.crt").write_bytes(ca.public_bytes(pem))
    (d / "broker.crt").write_bytes(crt.public_bytes(pem))
    (d / "broker.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout=10.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


@pytest.fixture
def stack(tmp_path):
    from sglab.nodes import NodeRegistry
    from sglab.pki import PKI
    from sglab.settings import load_settings

    shutil.copytree(REPO / "config", tmp_path / "config")
    pki_dir, tls = tmp_path / "pki", tmp_path / "tls"
    tls.mkdir()
    _server_cert(tls)
    pki = PKI(pki_dir)
    pki.init_ca()
    pki.issue_to("nbe-core", pki_dir / "core.key", pki_dir / "core.crt")
    port = _free_port()
    (tmp_path / "config" / "server.toml").write_text(f'''
[web]
allow_local_devices = false
[pki]
dir = "{pki_dir}"
[mqtt]
host = "localhost"
port = {port}
ca_file = "{tls / 'public_ca.crt'}"
cert_file = "{pki_dir / 'core.crt'}"
key_file = "{pki_dir / 'core.key'}"
''')
    # broker config = the template from deploy/, pointed at the test files
    acl = (REPO / "deploy" / "mosquitto" / "nbe-acl").read_text()
    (tmp_path / "acl").write_text(acl)
    (tmp_path / "mosquitto.conf").write_text(f'''
per_listener_settings true
user {getpass.getuser()}
persistence false
log_dest file {tmp_path / 'mosquitto.log'}
log_type all
listener {port} 127.0.0.1
certfile {tls / 'broker.crt'}
keyfile {tls / 'broker.key'}
cafile {pki.ca_crt_path}
require_certificate true
use_identity_as_username true
allow_anonymous false
acl_file {tmp_path / 'acl'}
''')
    # Ubuntu 26.04's AppArmor profile pins /usr/sbin/mosquitto to /etc/mosquitto; a copy elsewhere runs unconfined
    broker = shutil.copy(MOSQUITTO, tmp_path / "mosquitto")
    proc = subprocess.Popen([broker, "-c", str(tmp_path / "mosquitto.conf")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, 5), "mosquitto didn't start"
    s = load_settings(tmp_path)
    reg = NodeRegistry(s.db_path, s.pki_dir)
    reg.register("FNG02", site="finger")
    reg.register("FNG03", site="glasses")
    reg.register("GLS03", site="glasses")  # camera glasses behind a USB gateway
    yield s, tls, port
    proc.terminate()
    proc.wait(5)


def _node(s, tls, port, node_id, **kw):
    from sglab.simnode import SimNode

    nd = s.pki_dir / "nodes"
    cfg = {"host": "localhost", "port": port, "ca_file": str(tls / "public_ca.crt"),
           "cert_file": str(nd / f"{node_id}.crt"), "key_file": str(nd / f"{node_id}.key")}
    return SimNode(node_id, cfg, status_every_s=2, **kw)


def test_trial_over_mqtt_mtls(stack):
    from fastapi.testclient import TestClient

    from sglab.catalog import Catalog
    from sglab.web.app import create_app

    s, tls, port = stack
    Catalog(s.db_path, s.data_root).upsert_subject({"subject_id": "S001"})
    node = _node(s, tls, port, "FNG02")
    node.start_thread()
    with TestClient(create_app(s)) as c:  # `with` runs the lifespan -> hub connects
        assert _wait(lambda: c.get("/healthz").json()["mqtt"]["connected"], 10)

        def dev():
            return {d["key"]: d for d in c.get("/api/devices").json()["devices"]}
        assert _wait(lambda: dev()["mqtt://FNG02"]["online"], 10), dev()
        assert dev()["mqtt://FNG03"]["online"] is False  # registered, never connected
        assert set(dev()) == {"mqtt://FNG02", "mqtt://FNG03", "mqtt://GLS03"}  # local devices hidden on this server

        r = c.post("/trial/start", data={"devices": ["mqtt://FNG02"], "subject_id": "S001", "protocol": "rest",
                                         "trial": "1", "posture": "sitting", "operator": "EV"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        tid = r.headers["location"].rsplit("/", 1)[1]
        assert _wait(lambda: c.get(f"/api/trials/{tid}").json()["streams"][0]["samples"] > 250, 10)
        assert dev()["mqtt://FNG02"]["busy"]["trial"] == tid

        # a different node's certificate can't speak for FNG02 (ACL pins CN -> topics)
        rogue = _node(s, tls, port, "FNG03")
        rogue.base = "nbe/dev/FNG02"
        rogue.client.connect("localhost", port)
        rogue.client.loop_start()
        rogue.client.publish("nbe/dev/FNG02/data", "$D,999999,1,1,1*00\r\n", qos=1).wait_for_publish(3)
        rogue.client.publish("nbe/dev/FNG02/status", json.dumps({"online": True, "fw": "evil"}), qos=1).wait_for_publish(3)
        time.sleep(0.5)
        rogue.client.loop_stop()
        rogue.client.disconnect()

        assert c.post(f"/api/trials/{tid}/stop").status_code == 200
        r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "keep": "on"}, follow_redirects=False)
        sid = r.headers["location"].rsplit("/", 1)[1]
        hub = c.app.state.ctx.hub
        assert hub.node_state("FNG02").get("fw") != "evil"
        assert _wait(lambda: not node.src._streaming, 3)  # STOP reached the node
    node.stop_flag.set()

    sess = Catalog(s.db_path, s.data_root).session(sid)
    st = sess["streams"][0]
    assert st["role"] == "finger" and st["n_samples"] > 250
    doc = json.loads((s.data_root / sess["path"] / "session.json").read_text())
    summ = doc["streams"][0]
    assert summ["dev"] == "FNG02" and summ["port"] == "mqtt://FNG02"
    assert summ["stats"]["n_checksum_err"] == 0  # the rogue line never arrived
    assert summ["stats"]["n_dropped_seq"] == 0
    fit = summ["clock_fit"]
    assert abs(fit["b"] - 1) < 0.01


def test_node_offline_via_last_will(stack):
    from sglab.mqtt_hub import MqttHub

    s, tls, port = stack
    hub = MqttHub(s.mqtt, allowed=lambda: {"FNG02"})
    hub.start()
    node = _node(s, tls, port, "FNG02")
    node.client.connect("localhost", port, keepalive=1)
    node.client.loop_start()
    assert _wait(lambda: hub.node_state("FNG02")["online"], 10)
    # simulate a power cut: drop the socket without DISCONNECT -> broker publishes the will
    node.client._sock.close()
    node.client.loop_stop()
    assert _wait(lambda: hub.node_state("FNG02")["state"] == "offline", 10), hub.node_state("FNG02")
    hub.stop()


class FakeCameraBoard:
    """A camera board (firmware 0.7, USB binary link) on a serial port: 240x240 gray frames at
    15 fps plus 200 sps PPG, streaming between START and STOP, in USB envelopes."""

    def __init__(self, dev="GLS03"):
        import threading

        self.dev, self.buf, self.lock = dev, bytearray(), threading.Lock()
        self.streaming, self.closed = False, False
        self.frames_sent = self.samples_sent = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _headers(self):
        from nbe_gateway.wire import envelope, text_record
        from sglab.protocol import encode

        lines = [encode(f"H,proto=1,dev={self.dev},fw=0.7.0-idf,sensor=MAX30102,fs=200,ch=ir|red,site=glasses,sensor_ok=1"),
                 encode(f"H,proto=2,dev={self.dev},fw=0.7.0-idf,sid=1,stream=cam,kind=video,sensor=OV2640,ch=frame,"
                        "fs=15,w=240,h=240,fmt=gray8,aec=0,exposure=300,gain=2,site=glasses,sensor_ok=1")]
        return envelope(b"".join(text_record(x) for x in lines))

    def _put(self, b):
        with self.lock:
            self.buf += b

    def _run(self):
        from nbe_gateway.wire import FMT_GRAY8, VideoFrame, envelope, frame_record, text_record
        from sglab.protocol import encode

        t0 = time.monotonic()
        next_frame = next_sample = 0.0
        while not self.closed:
            time.sleep(0.005)
            if not self.streaming:
                t0 = time.monotonic()
                next_frame = next_sample = 0.0
                continue
            now = time.monotonic() - t0
            recs = b""
            while next_sample <= now:
                t_us = 1_000_000 + int(next_sample * 1e6)
                recs += text_record(encode(f"D,{self.samples_sent},{t_us},{100000 + self.samples_sent % 50},90000"))
                self.samples_sent += 1
                next_sample += 0.005
            if self.samples_sent and self.samples_sent % 200 == 0:
                recs += text_record(encode(f"S,{1_000_000 + int(now * 1e6)},fs_meas=200.0,cam_fps=15.0,cam_drop=0"))
            if recs:
                self._put(envelope(recs))
            if next_frame <= now:
                k = self.frames_sent
                px = bytes((k + x + 3 * y) & 0xFF for y in range(240) for x in range(240))
                self._put(envelope(frame_record(VideoFrame(1, k, 1_000_000 + int(next_frame * 1e6), 240, 240,
                                                           FMT_GRAY8, 300, 2, px))))
                self.frames_sent += 1
                next_frame += 1 / 15

    # pyserial-like
    @property
    def in_waiting(self):
        return len(self.buf)

    def read(self, n=1):
        end = time.monotonic() + 0.05
        while not self.buf and time.monotonic() < end and not self.closed:
            time.sleep(0.002)
        if self.closed:
            raise OSError("port closed")
        with self.lock:
            out = bytes(self.buf[:max(n, 1)])
            del self.buf[:len(out)]
        return out

    def write(self, data):
        from nbe_gateway.wire import envelope, text_record
        from sglab.protocol import encode

        for cmd in data.decode().split():
            if cmd == "INFO":
                self._put(self._headers())
            elif cmd == "START":
                self.frames_sent = self.samples_sent = 0
                self._put(self._headers() + envelope(text_record(encode("E,1000000,START,"))))
                self.streaming = True
            elif cmd == "STOP":
                self.streaming = False
                self._put(envelope(text_record(encode("E,9000000,STOP,"))))

    def flush(self):
        pass

    def close(self):
        self.closed = True


def test_camera_trial_through_usb_gateway(stack, tmp_path):
    """Website trial on GLS03: fake board -> real gateway (paho, mTLS as GLS03) -> mosquitto with the
    deploy ACL -> sglab hub -> recorder; then the live preview, and every frame and sample saved."""
    from fastapi.testclient import TestClient

    from nbe_gateway.app import Gateway
    from sglab.catalog import Catalog
    from sglab.video import read_frames
    from sglab.web.app import create_app

    s, tls, port = stack
    Catalog(s.db_path, s.data_root).upsert_subject({"subject_id": "S001"})
    nd = s.pki_dir / "nodes"
    board = FakeCameraBoard()
    gw = Gateway({"node_id": "GLS03", "broker_host": "localhost", "broker_port": port, "topic_prefix": "nbe",
                  "cert_file": str(nd / "GLS03.crt"), "key_file": str(nd / "GLS03.key"),
                  "ca_file": str(tls / "public_ca.crt"), "serial_port": "FAKE", "data_dir": str(tmp_path / "gw")},
                 board_open=lambda port: board)
    gw.start()
    try:
        with TestClient(create_app(s)) as c:
            def dev():
                return {d["key"]: d for d in c.get("/api/devices").json()["devices"]}
            assert _wait(lambda: dev()["mqtt://GLS03"]["online"], 15), (
                dev()["mqtt://GLS03"], gw.uplink.connected, gw.uplink.last_error, gw.board.connected, gw.board.warnings,
                (tmp_path / "mosquitto.log").read_text()[-1500:])
            r = c.post("/trial/start", data={"devices": ["mqtt://GLS03"], "subject_id": "S001", "protocol": "rest",
                                             "trial": "1", "posture": "sitting", "operator": "EV"}, follow_redirects=False)
            assert r.status_code == 303, r.text
            tid = r.headers["location"].rsplit("/", 1)[1]

            def cam():
                v = c.get(f"/api/trials/{tid}").json()["streams"][0].get("video") or [{}]
                return v[0]
            assert _wait(lambda: cam().get("frames", 0) >= 40, 20), cam()
            img = c.get(f"/api/trials/{tid}/video/{cam()['stream']}.jpg")
            assert img.status_code == 200 and img.content[:2] == b"\xff\xd8" and int(img.headers["X-Frame-Count"]) >= 40
            assert c.get(f"/api/trials/{tid}/video/nope.jpg").status_code == 404
            assert c.post(f"/api/trials/{tid}/stop").status_code == 200
            assert _wait(lambda: not board.streaming, 5)  # STOP went server -> broker -> gateway -> USB
            r = c.post(f"/trials/{tid}/finish", data={"quality": "good", "keep": "on"}, follow_redirects=False)
            sid = r.headers["location"].rsplit("/", 1)[1]
            st = c.app.state.ctx.hub.node_state("GLS03")
            assert st.get("link") == "usb-gateway" and st.get("sensor") == "MAX30102+OV2640"
    finally:
        gw.stop()
        board.close()

    sess = Catalog(s.db_path, s.data_root).session(sid)
    doc = json.loads((s.data_root / sess["path"] / "session.json").read_text())
    (v,) = doc["video"]
    frames = list(read_frames(s.data_root / sess["path"] / v["file"]))
    n = len(frames)
    assert n >= 40 and v["stats"]["n_dropped_seq"] == 0 and [f.seq for f in frames] == list(range(n))
    assert all(f.data[:240] == bytes((f.data[0] + x) & 0xFF for x in range(240)) for f in frames)
    ppg = doc["streams"][0]
    assert ppg["stats"]["n_dropped_seq"] == 0 and ppg["stats"]["n_samples"] > 400
    assert gw.log.unacked() == 0  # the gateway's disk copy is all acknowledged
