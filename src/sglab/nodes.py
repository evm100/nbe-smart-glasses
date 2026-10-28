"""Node (ESP32 sensor board) registry.

A node is registered once by an admin (web UI or `nbe-node register`): the
server issues its MQTT client certificate (CN = node ID) and keeps the key in
<pki_dir>/nodes/, so any admin can later pull the credentials again to
re-flash a board.  Live state (online, Wi-Fi, sensor, streaming) comes from
the node's retained MQTT status message, see mqtt_hub.py.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509

from .pki import PKI, fingerprint

NODE_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
RESERVED = {"NBE-CORE", "CORE", "ADMIN"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id      TEXT PRIMARY KEY,
    name         TEXT,
    site         TEXT,          -- finger | glasses | ...
    notes        TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT,
    created_at   TEXT,
    cert_fp      TEXT,          -- SHA-256 fingerprint of the current client cert
    cert_expires TEXT,
    mac          TEXT           -- Wi-Fi MAC as last reported in the node's status (firmware >= 0.6.2)
);
"""
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class NodeError(ValueError):
    pass


class NodeRegistry:
    def __init__(self, db_path: Path, pki_dir: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        if "mac" not in {r[1] for r in self.con.execute("PRAGMA table_info(nodes)")}:  # DBs from before 0.6.2
            self.con.execute("ALTER TABLE nodes ADD COLUMN mac TEXT")
            self.con.commit()
        self.pki = PKI(pki_dir)
        self.lock = threading.RLock()
        self._enabled: tuple[float, set[str]] = (0.0, set())

    def close(self):
        self.con.close()

    def _q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def _w(self, sql: str, args=()) -> None:
        with self.lock:
            try:
                self.con.execute(sql, args)
                self.con.commit()
            except BaseException:
                self.con.rollback()  # don't leave the DB write-locked
                raise
            self._enabled = (0.0, set())

    def nodes(self) -> list[dict]:
        return self._q("SELECT * FROM nodes ORDER BY node_id")

    def node(self, node_id: str) -> dict | None:
        r = self._q("SELECT * FROM nodes WHERE node_id=?", (str(node_id).strip().upper(),))
        return r[0] if r else None

    def enabled_ids(self) -> set[str]:
        """Called for every MQTT message, so cached for a few seconds (writes invalidate it)."""
        t, ids = self._enabled
        if time.monotonic() - t > 5:
            ids = {r["node_id"] for r in self._q("SELECT node_id FROM nodes WHERE enabled=1")}
            self._enabled = (time.monotonic(), ids)
        return ids

    def _paths(self, node_id: str) -> tuple[Path, Path]:
        d = self.pki.dir / "nodes"
        return d / f"{node_id}.key", d / f"{node_id}.crt"

    def register(self, node_id: str, name: str = "", site: str = "", notes: str = "", by: str = "") -> dict:
        node_id = node_id.strip().upper()
        if not NODE_ID_RE.match(node_id) or node_id in RESERVED:
            raise NodeError("node ID: 2-32 characters, A-Z 0-9 _ -, starting with a letter or digit (e.g. FNG02)")
        if self.node(node_id):
            raise NodeError(f"node {node_id} already exists (use credentials / rotate)")
        key_p, crt_p = self._paths(node_id)
        cert = self.pki.issue_to(node_id, key_p, crt_p)
        self._w("INSERT INTO nodes (node_id, name, site, notes, enabled, created_by, created_at, cert_fp, cert_expires)"
                " VALUES (?,?,?,?,1,?,?,?,?)",
                (node_id, name or node_id, site, notes, by, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 fingerprint(cert), cert.not_valid_after_utc.isoformat(timespec="seconds")))
        return self.node(node_id)

    def rotate(self, node_id: str) -> dict:
        node_id = str(node_id).strip().upper()
        """Issue a fresh key + certificate (the old cert stays valid until it expires;
        disable the node if the old key may have leaked)."""
        if not self.node(node_id):
            raise NodeError(f"unknown node {node_id}")
        key_p, crt_p = self._paths(node_id)
        cert = self.pki.issue_to(node_id, key_p, crt_p)
        self._w("UPDATE nodes SET cert_fp=?, cert_expires=? WHERE node_id=?",
                (fingerprint(cert), cert.not_valid_after_utc.isoformat(timespec="seconds"), node_id))
        return self.node(node_id)

    def update(self, node_id: str, **fields) -> dict:
        node_id = str(node_id).strip().upper()
        if not self.node(node_id):
            raise NodeError(f"unknown node {node_id}")
        allowed = {"name", "site", "notes", "enabled"}
        sets = {k: (int(bool(v)) if k == "enabled" else str(v)[:200]) for k, v in fields.items()
                if k in allowed and v is not None}
        if sets:
            self._w(f"UPDATE nodes SET {', '.join(f'{k}=?' for k in sets)} WHERE node_id=?", (*sets.values(), node_id))
        return self.node(node_id)

    def set_mac(self, node_id: str, mac: str) -> None:
        """Remember the MAC a node reports (MqttHub.on_mac), so it is known while the node is offline."""
        mac = str(mac).strip().lower()
        if MAC_RE.match(mac):
            self._w("UPDATE nodes SET mac=? WHERE node_id=? AND mac IS NOT ?", (mac, str(node_id).strip().upper(), mac))

    def credentials(self, node_id: str) -> dict:
        node_id = str(node_id).strip().upper()
        n = self.node(node_id)
        if not n:
            raise NodeError(f"unknown node {node_id}")
        key_p, crt_p = self._paths(node_id)
        if not key_p.exists():
            raise NodeError(f"no key stored for {node_id}; rotate to issue a new one")
        cert = x509.load_pem_x509_certificate(crt_p.read_bytes())
        return {"node": n, "client_cert": crt_p.read_text(), "client_key": key_p.read_text(),
                "device_ca": self.pki.ca_crt_path.read_text(), "cert_fp": fingerprint(cert)}
