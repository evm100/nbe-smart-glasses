#!/usr/bin/env python3
"""Try the team-server features on one computer: accounts + a local mosquitto with mTLS + a
simulated Wi-Fi node. Nothing here is for production (see deploy/DEPLOY.md).

    python scripts/dev_server.py            # creates ./dev-server/ and config/server.toml
    mosquitto -c dev-server/mosquitto.conf  # terminal 1 (apt install mosquitto / brew install mosquitto)
                                            # Ubuntu 26.04 confines /usr/sbin/mosquitto to /etc/mosquitto
                                            # (AppArmor): run a copy, e.g. cp /usr/sbin/mosquitto dev-server/
    sglab serve                             # terminal 2 -> http://127.0.0.1:8000, log in as admin
    sglab sim-node SIM01                    # terminal 3: a fake ESP32 appears on Devices

Delete dev-server/ and config/server.toml to go back to single-user lab-PC mode.
"""
from __future__ import annotations

import ipaddress
import secrets
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mosquitto_cmd() -> str:
    """AppArmor (Ubuntu 26.04) keeps /usr/sbin/mosquitto out of the repo; an unconfined copy can read dev-server/."""
    sysbin = Path("/usr/sbin/mosquitto")
    if not (sys.platform.startswith("linux") and sysbin.exists() and Path("/etc/apparmor.d/mosquitto").exists()):
        return "mosquitto"
    copy = DEV / "mosquitto"
    shutil.copy(sysbin, copy)
    return str(copy)
sys.path.insert(0, str(ROOT / "src"))
DEV = ROOT / "dev-server"
PORT = 8884


def broker_cert(d: Path):
    """A throwaway CA + localhost certificate standing in for Let's Encrypt."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dev broker CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(1))
          .not_valid_after(now + timedelta(365)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    crt = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
           .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - timedelta(1)).not_valid_after(now + timedelta(365))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
           .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (d / "broker_ca.crt").write_bytes(ca.public_bytes(pem))
    (d / "broker.crt").write_bytes(crt.public_bytes(pem))
    (d / "broker.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))


def main():
    from sglab.auth import AuthStore
    from sglab.nodes import NodeRegistry
    from sglab.pki import PKI
    from sglab.settings import load_settings

    cfg = ROOT / "config" / "server.toml"
    if cfg.exists():
        sys.exit(f"{cfg} exists - delete it (and dev-server/) first if you want a fresh dev setup")
    DEV.mkdir(exist_ok=True)
    broker_cert(DEV)
    pki = PKI(DEV / "pki")
    if not pki.exists():
        pki.init_ca()
    pki.issue_to("nbe-core", DEV / "pki" / "core.key", DEV / "pki" / "core.crt")
    (DEV / "mosquitto.conf").write_text(f"""# dev broker: mTLS on localhost:{PORT} with the NBE ACL
per_listener_settings true
persistence false
log_dest stdout
listener {PORT} 127.0.0.1
certfile {DEV / 'broker.crt'}
keyfile {DEV / 'broker.key'}
cafile {pki.ca_crt_path}
require_certificate true
use_identity_as_username true
allow_anonymous false
acl_file {ROOT / 'deploy' / 'mosquitto' / 'nbe-acl'}
""")
    cfg.write_text(f"""# dev setup from scripts/dev_server.py (delete to return to single-user mode)
[auth]
enabled = true
[web]
allow_local_devices = true
[pki]
dir = "{DEV / 'pki'}"
[mqtt]
host = "localhost"
port = {PORT}
ca_file = "{DEV / 'broker_ca.crt'}"
cert_file = "{DEV / 'pki' / 'core.crt'}"
key_file = "{DEV / 'pki' / 'core.key'}"
[provisioning]
broker_uri = "mqtts://localhost:{PORT}"
broker_ca = "{DEV / 'broker_ca.crt'}"
""")
    s = load_settings(ROOT)
    s.data_root.mkdir(parents=True, exist_ok=True)
    auth = AuthStore(s.db_path)
    pw = None
    if not auth.user("admin"):
        pw = secrets.token_urlsafe(9)
        auth.add_user("admin", pw, "admin")
    reg = NodeRegistry(s.db_path, s.pki_dir)
    if not reg.node("SIM01"):
        reg.register("SIM01", name="simulated finger", site="finger", by="dev_server")
    print(f"""dev server ready in {DEV}
  login:   admin / {pw or '(unchanged)'}   (change it under Account)
  1) {_mosquitto_cmd()} -c {DEV / 'mosquitto.conf'}
  2) sglab serve
  3) sglab sim-node SIM01       # appears online on /devices; record from it under New trial
Undo: rm -r dev-server config/server.toml   (the catalog keeps the dev users/nodes - harmless)""")


if __name__ == "__main__":
    main()
