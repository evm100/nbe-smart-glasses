"""Locate the repository and load config/lab.toml + config/session_fields.toml."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def subject_code(subject_id: str | None) -> str | None:
    """The S### code: some subject ids carry more than the code (e.g. 'S002 - name')."""
    m = re.match(r"\s*(S\d+)", subject_id or "")
    return m.group(1) if m else subject_id


def _when(x) -> datetime:
    d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()  # naive = this machine's local time


@dataclass(frozen=True)
class Selection:
    """Which sessions tuning and model training may use ([selection] in lab.toml).

    Recording, processing and the web UI still see everything; only `sglab tune` (Layer 1-2
    search, scorecard) and `sglab dataset build` (and so every model / sweep) skip these."""
    exclude_subjects: frozenset = frozenset()
    not_before: str | None = None  # ISO date/time; sessions that started earlier are skipped

    @classmethod
    def from_dict(cls, d: dict | None) -> "Selection":
        d = d or {}
        nb = d.get("not_before") or None
        if nb is not None:
            _when(nb)  # fail early on a typo
        return cls(frozenset(subject_code(str(s)) for s in d.get("exclude_subjects", [])),
                   str(nb) if nb is not None else None)

    def reason(self, subject_id: str | None, started_at) -> str | None:
        """Why a session is left out (None = use it)."""
        code = subject_code(subject_id)
        if code in self.exclude_subjects:
            return f"subject {code} excluded"
        if self.not_before and started_at and _when(started_at) < _when(self.not_before):
            return f"before {self.not_before}"
        return None

    def to_dict(self) -> dict:
        return {"exclude_subjects": sorted(self.exclude_subjects), "not_before": self.not_before}


def find_root(start: Path | None = None) -> Path:
    """Repo root = first parent containing config/lab.toml (or $SGLAB_ROOT)."""
    env = os.environ.get("SGLAB_ROOT")
    if env:
        return Path(env).resolve()
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / "config" / "lab.toml").exists():
            return d
    raise FileNotFoundError(
        "Could not find config/lab.toml. Run sglab inside the smart-glasses repo or set SGLAB_ROOT."
    )


@dataclass
class Settings:
    root: Path
    lab: dict = field(default_factory=dict)
    fields: list[dict] = field(default_factory=list)
    # config/server.toml (deployment-specific, not committed): auth, MQTT, PKI, public URL
    srv: dict = field(default_factory=dict)

    @property
    def data_root(self) -> Path:
        override = os.environ.get("SGLAB_DATA_ROOT") or self.srv.get("paths", {}).get("data_root")
        if override:
            return Path(override).resolve()
        return (self.root / self.lab.get("paths", {}).get("data_root", "data")).resolve()

    @property
    def selection(self) -> Selection:
        return Selection.from_dict(self.lab.get("selection"))

    # ---- server / deployment ------------------------------------------------
    @property
    def auth_enabled(self) -> bool:
        return bool(self.srv.get("auth", {}).get("enabled", False))

    @property
    def session_hours(self) -> float:
        return float(self.srv.get("auth", {}).get("session_hours", 12))

    @property
    def public_url(self) -> str | None:
        return self.srv.get("web", {}).get("public_url")

    @property
    def allow_local_devices(self) -> bool:
        """Serial / simulated devices from lab.toml (False on a headless server)."""
        return bool(self.srv.get("web", {}).get("allow_local_devices", True))

    @property
    def pki_dir(self) -> Path:
        p = self.srv.get("pki", {}).get("dir")
        return Path(p).resolve() if p else (self.data_root.parent / "pki").resolve()

    @property
    def firmware_dir(self) -> Path:
        """OTA images; they embed node keys and the Wi-Fi password, so they live with the PKI."""
        p = self.srv.get("paths", {}).get("firmware_dir")
        return Path(p).resolve() if p else self.pki_dir / "firmware"

    @property
    def mqtt(self) -> dict:
        """[mqtt] section; empty dict = MQTT disabled."""
        m = dict(self.srv.get("mqtt", {}))
        if not m.get("enabled", bool(m.get("host"))):
            return {}
        m.setdefault("port", 8883)
        m.setdefault("topic_prefix", "nbe")
        m.setdefault("client_id", "nbe-core")
        return m

    @property
    def provisioning(self) -> dict:
        """What the nbe-node tool writes into a node's sdkconfig (broker address as the
        *nodes* see it, which is usually the public hostname, not the core's localhost)."""
        p = dict(self.srv.get("provisioning", {}))
        m = self.mqtt
        p.setdefault("broker_uri", f"mqtts://{m['host']}:{m['port']}" if m else "")
        p.setdefault("broker_ca", "bundle")  # "bundle" = ESP-IDF CA bundle (Let's Encrypt etc.) | path to a PEM
        p.setdefault("topic_prefix", m.get("topic_prefix", "nbe") if m else "nbe")
        return p

    @property
    def processing_mode(self) -> str:
        """[processing] mode: "local" = in this process, "remote" = queue for `sglab worker` (sglab.jobs)."""
        m = self.srv.get("processing", {}).get("mode", "local")
        if m not in ("local", "remote"):
            raise ValueError(f'[processing] mode must be "local" or "remote", not {m!r}')
        return m

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def incoming_dir(self) -> Path:
        return self.data_root / "raw" / "_incoming"

    @property
    def processed_dir(self) -> Path:
        return self.data_root / "processed"

    @property
    def db_path(self) -> Path:
        return self.data_root / "catalog.sqlite"

    @property
    def state_path(self) -> Path:
        return self.data_root / ".sglab_state.json"

    @property
    def baud(self) -> int:
        return int(self.lab.get("serial", {}).get("baud", 921600))

    @property
    def start_timeout_s(self) -> float:
        return float(self.lab.get("serial", {}).get("start_timeout_s", 3.0))

    def device(self, name_or_port: str) -> dict:
        """Resolve a device alias from lab.toml, or treat the argument as a port."""
        devs = self.lab.get("devices", {})
        if name_or_port in devs:
            d = dict(devs[name_or_port])
            d.setdefault("role", name_or_port)
            return d
        role = "sim" if name_or_port.startswith("sim") else Path(name_or_port).name.lower()
        return {"port": name_or_port, "role": role}


def load_settings(root: Path | None = None) -> Settings:
    root = root or find_root()
    lab = tomllib.loads((root / "config" / "lab.toml").read_text())
    fp = root / "config" / "session_fields.toml"
    fields = tomllib.loads(fp.read_text()).get("field", []) if fp.exists() else []
    sp = Path(os.environ.get("SGLAB_SERVER_CONFIG", root / "config" / "server.toml"))
    srv = tomllib.loads(sp.read_text()) if sp.exists() else {}
    return Settings(root=root, lab=lab, fields=fields, srv=srv)
