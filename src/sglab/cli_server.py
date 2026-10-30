"""Server-side admin commands (run on the machine that hosts the data + PKI).

  sglab user add <name> --role admin|operator|viewer      (asks for the password)
  sglab user list | sglab user passwd <name> | sglab user set <name> --role R [--inactive|--active]
  sglab pki init                     create the NBE Device CA (once)
  sglab pki core-cert                client cert for the server's own MQTT connection (CN = nbe-core)
  sglab pki show                     CA + certificate overview
  sglab node list | register <ID> [--site finger] | rotate <ID> | enable/disable <ID>
  sglab node zip <ID> [-o file.zip]  credentials bundle for flashing (same as the web download)
  sglab sim-node <ID> [...]          simulated ESP32 that connects to the broker like a real node
  sglab doctor                       check config, paths, PKI and broker connection
  sglab snapshot [--keep 14]         consistent copy of the catalog DB for backups (run nightly)
"""
from __future__ import annotations

import getpass
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .settings import load_settings

console = Console()


def _auth(s):
    from .auth import AuthStore

    s.data_root.mkdir(parents=True, exist_ok=True)
    return AuthStore(s.db_path)


def _registry(s):
    from .nodes import NodeRegistry

    s.data_root.mkdir(parents=True, exist_ok=True)
    return NodeRegistry(s.db_path, s.pki_dir)


def _ask_password(confirm: bool = True) -> str:
    pw = getpass.getpass("password (10+ characters): ")
    if confirm and getpass.getpass("again: ") != pw:
        raise ValueError("passwords don't match")
    return pw


# ---------------------------------------------------------------- users
def cmd_user(a):
    s = load_settings()
    st = _auth(s)
    if a.action == "list":
        t = Table("user", "name", "role", "active", "created")
        for u in st.users():
            t.add_row(u["username"], u["display"] or "", u["role"], "yes" if u["active"] else "no", (u["created_at"] or "")[:10])
        console.print(t)
        if not s.auth_enabled:
            console.print("[yellow]note:[/] [auth] enabled = false in config/server.toml – logins are not enforced yet")
        return 0
    if not a.name:
        raise ValueError("give a username")
    if a.action == "add":
        st.add_user(a.name, a.password or _ask_password(), a.role, a.display)
        st.audit("cli", "user.add", a.name, {"role": a.role})
        console.print(f"added [b]{a.name}[/] ({a.role})")
    elif a.action == "passwd":
        st.set_password(a.name, a.password or _ask_password())
        st.audit("cli", "user.password", a.name)
        console.print("password changed (all sessions of that user were logged out)")
    elif a.action == "set":
        active = True if a.active else (False if a.inactive else None)
        st.set_role(a.name, role=a.role_set, active=active)
        st.audit("cli", "user.edit", a.name, {"role": a.role_set, "active": active})
        console.print(st.user(a.name))
    elif a.action == "token":
        if st.user(a.name) is None:
            raise ValueError(f"no user {a.name!r}")
        tok = st.create_token(a.name, a.label or "cli")
        st.audit("cli", "token.create", a.name, {"label": a.label})
        print(tok)  # plain stdout, so it can be piped into a file
    return 0


# ---------------------------------------------------------------- PKI
def cmd_pki(a):
    from .pki import PKI, fingerprint

    s = load_settings()
    pki = PKI(s.pki_dir)
    if a.action == "init":
        p = pki.init_ca(a.name)
        console.print(f"created CA [b]{p}[/] (+ ca.key, mode 600). Back up {s.pki_dir} – losing ca.key means "
                      "re-flashing every node.")
        console.print(f"mosquitto: cafile for the node listener = {p}")
    elif a.action == "core-cert":
        key, crt = s.pki_dir / "core.key", s.pki_dir / "core.crt"
        if crt.exists() and not a.force:
            raise ValueError(f"{crt} exists (use --force to replace)")
        cert = pki.issue_to(a.cn, key, crt, days=a.days)
        console.print(f"issued {crt} (CN={a.cn}, expires {cert.not_valid_after_utc:%Y-%m-%d})")
        console.print("config/server.toml [mqtt]: cert_file / key_file = these two files")
    elif a.action == "show":
        from cryptography import x509

        if not pki.exists():
            console.print(f"no CA in {s.pki_dir} – run `sglab pki init`")
            return 1
        ca = x509.load_pem_x509_certificate(pki.ca_crt_path.read_bytes())
        console.print(f"CA  {ca.subject.rfc4514_string()}  expires {ca.not_valid_after_utc:%Y-%m-%d}  sha256 {fingerprint(ca)[:16]}…")
        for p in sorted([*s.pki_dir.glob("*.crt"), *(s.pki_dir / "nodes").glob("*.crt")]):
            if p.name == "ca.crt":
                continue
            c = x509.load_pem_x509_certificate(p.read_bytes())
            console.print(f"    {p.relative_to(s.pki_dir)}  {c.subject.rfc4514_string()}  expires {c.not_valid_after_utc:%Y-%m-%d}")
    return 0


# ---------------------------------------------------------------- nodes
def cmd_node(a):
    from . import provision

    s = load_settings()
    reg = _registry(s)
    if a.action == "list":
        t = Table("node", "name", "site", "enabled", "cert expires", "notes")
        for n in reg.nodes():
            t.add_row(n["node_id"], n["name"] or "", n["site"] or "", "yes" if n["enabled"] else "no",
                      (n["cert_expires"] or "")[:10], n["notes"] or "")
        console.print(t)
        return 0
    if not a.node_id:
        raise ValueError("give a node ID")
    if a.action == "register":
        n = reg.register(a.node_id, a.label or "", a.site, a.notes or "", by="cli")
        console.print(f"registered [b]{n['node_id']}[/] – flash it with firmware/tools/nbe_node.py configure {n['node_id']}")
    elif a.action == "rotate":
        n = reg.rotate(a.node_id)
        console.print(f"new certificate for {n['node_id']} (expires {n['cert_expires'][:10]}) – re-flash the board")
    elif a.action in ("enable", "disable"):
        reg.update(a.node_id, enabled=a.action == "enable")
        console.print(f"{a.node_id.upper()} {a.action}d")
    elif a.action == "zip":
        b = provision.bundle(s, reg, a.node_id)
        out = Path(a.output or f"{b['node_id']}_credentials.zip")
        out.write_bytes(provision.bundle_zip(b))
        out.chmod(0o600)
        console.print(f"wrote {out} (contains the node's private key)")
    return 0


# ---------------------------------------------------------------- simulated node
def cmd_sim_node(a):
    from .simnode import SimNode

    s = load_settings()
    m = s.mqtt
    node = a.node_id.upper()
    cfg = {"host": a.host or (m.get("host") if m else None) or "localhost",
           "port": a.port or (m.get("port") if m else 8883), "tls": not a.no_tls}
    if not a.no_tls:
        nodes_dir = s.pki_dir / "nodes"
        cfg.update(ca_file=a.ca or (m.get("ca_file") if m else None),
                   cert_file=a.cert or str(nodes_dir / f"{node}.crt"), key_file=a.key or str(nodes_dir / f"{node}.key"))
        for k in ("cert_file", "key_file"):
            if not Path(cfg[k]).exists():
                raise FileNotFoundError(f"{cfg[k]} – register the node first or pass --cert/--key")
    prefix = a.prefix or (m.get("topic_prefix") if m else "nbe")
    sim = SimNode(node, cfg, prefix=prefix, site=a.site, sim_kw={"hr_bpm": a.hr, "seed": a.seed})
    console.print(f"sim node [b]{node}[/] -> {cfg['host']}:{cfg['port']} ({'TLS' if cfg['tls'] else 'plain'}), "
                  f"topics {prefix}/dev/{node}/… – Ctrl-C to stop")
    try:
        sim.run(a.duration)
    except KeyboardInterrupt:
        pass
    console.print(f"sent {sim.sent['batches']} batches / {sim.sent['lines']} lines")
    return 0


# ---------------------------------------------------------------- doctor
def cmd_doctor(a):
    s = load_settings()
    ok = True

    from rich.markup import escape

    def line(good, what, hint=""):
        nonlocal ok
        what, hint = escape(str(what)), escape(str(hint))
        ok &= bool(good) or good is None
        mark = "[green]ok[/]  " if good else ("[yellow]--[/]  " if good is None else "[red]FAIL[/]")
        console.print(f"{mark} {what}" + (f"  [dim]{hint}[/]" if hint and not good else ""))

    console.print(f"repo {s.root}")
    line(True, f"data root {s.data_root}")
    try:
        s.data_root.mkdir(parents=True, exist_ok=True)
        probe = s.data_root / ".write_test"
        probe.write_text("x")
        probe.unlink()
        line(True, "data root writable")
    except OSError as e:
        line(False, f"data root writable ({e})", "check ownership: chown -R nbe: <data root>")
    line(s.auth_enabled or None, f"user accounts {'on' if s.auth_enabled else 'off (single-user mode)'}",
         "set [auth] enabled = true for a shared server")
    if s.auth_enabled:
        st = _auth(s)
        line(st.n_admins() > 0, f"{st.n_admins()} active admin(s)", "sglab user add <you> --role admin")
    line(s.public_url or None, f"public URL {s.public_url or '(not set)'}")
    from .pki import PKI
    line(PKI(s.pki_dir).exists() or None, f"device CA in {s.pki_dir}", "sglab pki init")
    m = s.mqtt
    if not m:
        line(None, "MQTT not configured ([mqtt] in config/server.toml)")
    else:
        for k in ("ca_file", "cert_file", "key_file"):
            if m.get(k):
                line(Path(m[k]).exists(), f"mqtt {k} {m[k]}")
        from .mqtt_hub import MqttHub
        hub = MqttHub(m, allowed=lambda: set())
        hub.start()
        t_end = time.time() + 8
        while time.time() < t_end and not hub.connected and not hub.last_error:
            time.sleep(0.2)
        line(hub.connected, f"broker {m['host']}:{m['port']} as {m.get('client_id')}",
             hub.last_error or "no answer – firewall, hostname, or TLS (check ca_file / broker cert name)")
        time.sleep(1.5)  # collect retained node statuses
        if hub.connected:
            console.print(f"     nodes with a retained status: {', '.join(sorted(hub.known_ids())) or 'none yet'}")
        hub.stop()
    p = s.provisioning
    line(bool(p["broker_uri"]) or (False if m else None), f"nodes will connect to {p['broker_uri'] or '?'} (CA: {p['broker_ca']})",
         "set [provisioning] broker_uri")
    return 0 if ok else 1


# ---------------------------------------------------------------- snapshot
def cmd_snapshot(a):
    """SQLite online backup: a consistent copy even while the server writes (WAL). Backups
    should copy the snapshot, not the live catalog.sqlite."""
    import sqlite3
    from datetime import datetime

    s = load_settings()
    out = Path(a.out) if a.out else s.data_root / "snapshots"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"catalog-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    src = sqlite3.connect(s.db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    old = sorted(out.glob("catalog-*.sqlite"))[:-a.keep] if a.keep > 0 else []
    for p in old:
        p.unlink()
    console.print(f"snapshot {dest} ({dest.stat().st_size / 1e6:.1f} MB); kept {min(a.keep, len(list(out.glob('catalog-*.sqlite'))))}")
    return 0


# ---------------------------------------------------------------- jobs
def cmd_jobs(a):
    from .jobs import JobQueue

    s = load_settings()
    q = JobQueue(s.db_path)
    t = Table("job", "session / sweep", "state", "worker", "created", "finished", "error")
    for j in q.recent(30):
        what = j["session_id"] if j["kind"] == "process" else f"sweep {(j['payload'] or {}).get('name', '')}"
        state = j["state"] + (" (force)" if j["force"] else "") + (f" {j['progress']}" if j.get("progress") else "")
        t.add_row(str(j["job_id"]), what, state, j["worker"] or "",
                  (j["created_at"] or "")[:19], (j["finished_at"] or "")[:19], (j["error"] or "")[:60])
    console.print(t)
    console.print(f"processing mode: {s.processing_mode}; {q.pending()} queued/running")
    return 0


# ---------------------------------------------------------------- parser
def add_parsers(sub) -> None:
    u = sub.add_parser("user", help="manage user accounts (server)")
    u.add_argument("action", choices=["add", "list", "passwd", "set", "token"])
    u.add_argument("name", nargs="?")
    u.add_argument("--role", default="operator", choices=["worker", "viewer", "operator", "admin"], help="for add")
    u.add_argument("--display", help="display name (add)")
    u.add_argument("--password", help="non-interactive (avoid: ends up in shell history)")
    u.add_argument("--set-role", dest="role_set", choices=["worker", "viewer", "operator", "admin"], help="for set")
    u.add_argument("--label", help="for token: what it is for (shown in the token list)")
    u.add_argument("--active", action="store_true"); u.add_argument("--inactive", action="store_true")
    u.set_defaults(func=cmd_user)

    k = sub.add_parser("pki", help="device CA and certificates (server)")
    k.add_argument("action", choices=["init", "core-cert", "show"])
    k.add_argument("--name", default="NBE Device CA")
    k.add_argument("--cn", default="nbe-core", help="core-cert common name (= MQTT username in the ACL)")
    k.add_argument("--days", type=int, default=3 * 365)
    k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_pki)

    n = sub.add_parser("node", help="register / list sensor nodes (server)")
    n.add_argument("action", choices=["list", "register", "rotate", "enable", "disable", "zip"])
    n.add_argument("node_id", nargs="?")
    n.add_argument("--site", default="finger"); n.add_argument("--name", dest="label"); n.add_argument("--notes")
    n.add_argument("-o", "--output")
    n.set_defaults(func=cmd_node)

    sn = sub.add_parser("sim-node", help="simulated ESP32 node over MQTT (testing)")
    sn.add_argument("node_id")
    sn.add_argument("--host"); sn.add_argument("--port", type=int)
    sn.add_argument("--ca", help="broker CA (default: [mqtt] ca_file, else system CAs)")
    sn.add_argument("--cert"); sn.add_argument("--key")
    sn.add_argument("--no-tls", action="store_true", help="plain MQTT (local test broker only)")
    sn.add_argument("--prefix"); sn.add_argument("--site", default="finger")
    sn.add_argument("--hr", type=float, default=72.0); sn.add_argument("--seed", type=int, default=0)
    sn.add_argument("--duration", type=float)
    sn.set_defaults(func=cmd_sim_node)

    sub.add_parser("doctor", help="check server config, paths, PKI, broker").set_defaults(func=cmd_doctor)

    sub.add_parser("jobs", help="remote processing queue ([processing] mode = remote)").set_defaults(func=cmd_jobs)

    sp = sub.add_parser("snapshot", help="consistent copy of the catalog database (for backups)")
    sp.add_argument("--out", help="folder (default <data root>/snapshots)")
    sp.add_argument("--keep", type=int, default=14)
    sp.set_defaults(func=cmd_snapshot)
