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
        assert set(dev()) == {"mqtt://FNG02", "mqtt://FNG03"}  # local devices hidden on this server

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
