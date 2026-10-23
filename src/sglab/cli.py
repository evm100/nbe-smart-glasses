"""sglab – lab tool for the smart-glasses PPG project.

  sglab init                         create data folders, catalog, pipeline configs
  sglab ports                        list serial ports
  sglab record finger glasses        record from devices named in config/lab.toml (or ports)
  sglab record sim                   practice run with a simulated ESP32
  sglab subject add | sglab subjects
  sglab list [--subject S001] [--protocol rest] [--all]
  sglab show <session>
  sglab process <session>... | --all [--force]
  sglab import file.csv --subject S001 --protocol rest --role finger
  sglab reindex | sglab recover
  sglab serve [--lan] [--token auto]  web UI (run trials + browse) at http://<ip>:8000
  sglab worker URL --token-file F    process the server's queued jobs on this computer

Server administration (multi-user deployment, MQTT nodes – see deploy/DEPLOY.md):
  sglab user add|list|passwd|set|token   sglab pki init|core-cert|show   sglab jobs
  sglab node list|register|rotate|enable|disable|zip       sglab sim-node <ID>       sglab doctor
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .catalog import Catalog
from .settings import load_settings

console = Console()


def _ctx():
    s = load_settings()
    s.raw_dir.mkdir(parents=True, exist_ok=True)
    s.processed_dir.mkdir(parents=True, exist_ok=True)
    return s, Catalog(s.db_path, s.data_root)


def _preset(items) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--meta expects key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------
def cmd_init(a):
    s, cat = _ctx()
    from ppg.config import Layer1Config, Layer2Config

    for name, cfg in [("pipeline_layer1.json", Layer1Config()), ("pipeline_layer2.json", Layer2Config())]:
        p = s.root / "config" / name
        if not p.exists():
            cfg.to_json(p)
            console.print(f"wrote {p.relative_to(s.root)}")
    (s.data_root / "datasets").mkdir(parents=True, exist_ok=True)
    console.print(f"data root: {s.data_root}\ncatalog:   {s.db_path}")
    return 0


def cmd_ports(a):
    from .sources import list_ports

    ports = list_ports()
    t = Table("port", "description", "hwid")
    for p in ports:
        t.add_row(p["port"], p["description"], p["hwid"])
    console.print(t if ports else "No serial ports found.")
    return 0


def _status_table(rec) -> Table:
    t = Table("stream", "device", "rate (Hz)", "fs", "samples", "dropped", "bad lines", "last values", "state")
    for r in rec.live_status():
        state = "[red]" + r["error"] + "[/]" if r["error"] else ("ok" if r["alive"] else "done")
        rate_ok = r["fs"] and abs(r["rate_hz"] - r["fs"]) < 0.1 * r["fs"]
        rate = f"{r['rate_hz']:.0f}" if rate_ok else f"[yellow]{r['rate_hz']:.0f}[/]"
        t.add_row(r["role"], r["dev"], rate, f"{r['fs'] or '?'}", str(r["samples"]),
                  str(r["dropped"]) if not r["dropped"] else f"[yellow]{r['dropped']}[/]",
                  str(r["bad_lines"]), " ".join(map(str, r["last"])), state)
    return t


def cmd_record(a):
    from .commands import HELP, CommandError, parse_command
    from .metadata import ask_fields, ask_subject, load_state, save_state
    from .recorder import Recording

    s, cat = _ctx()
    devices = [s.device(d) for d in a.devices]
    preset = _preset(a.meta)
    state = load_state(s.state_path)
    ni = a.yes

    console.rule("[bold]New recording")
    console.print("Devices: " + ", ".join(f"{d['role']} ({d['port']})" for d in devices))
    pre = ask_fields(s.fields, "before", cat, state, preset, non_interactive=ni)
    subj = pre.get("subject_id")
    if subj and cat.subject(subj) is None:
        console.print(f"[yellow]{subj} is a new subject[/] – a few details (de-identified only):")
        ask_subject(subj, cat, non_interactive=ni)

    if not ni:
        console.input("\n[bold]Press Enter to START[/] (sensor on, subject settled) ")

    rec = Recording(s, devices, sim_kw={"realtime": True})
    for w in rec.start(pre):
        console.print(f"[red]WARNING[/] {w}")
    console.print(f"[green]Recording[/] → {rec.folder}")
    time.sleep(1.2)
    console.print(_status_table(rec))

    try:
        if a.duration:
            end = time.monotonic() + a.duration
            while time.monotonic() < end:
                time.sleep(min(5, max(0.1, end - time.monotonic())))
                console.print(_status_table(rec))
        else:
            console.print(HELP, markup=False)
            while True:
                try:
                    text = console.input(f"[dim]{rec.now():7.1f}s[/] rec> ")
                except EOFError:
                    break
                try:
                    action, payload = parse_command(text)
                except CommandError as e:
                    console.print(f"[red]{e}[/]")
                    continue
                if action == "stop":
                    break
                if action == "help":
                    console.print(HELP, markup=False)
                elif action == "status":
                    console.print(_status_table(rec))
                else:
                    ev = rec.add_event(**payload)
                    console.print(f"  logged {ev['kind']} at t={ev['t_s']:.1f}s")
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl-C – stopping[/]")
    rec.stop()

    console.rule("[bold]Stopped")
    console.print(_status_table(rec))
    post = ask_fields(s.fields, "after", cat, state, preset, non_interactive=ni, context=pre)
    meta = {**pre, **post}
    last = state.setdefault("last", {})
    for f in s.fields:
        if f.get("remember") and meta.get(f["name"]) is not None:
            last[f["name"]] = meta[f["name"]]
    save_state(s.state_path, state)

    rec.subject_snapshot = cat.subject(meta.get("subject_id")) if meta.get("subject_id") else None
    dest, doc = rec.finalize(meta)
    sid = cat.upsert_session(dest)
    console.print(f"[green]Saved[/] {sid}\n  {dest}")
    if doc["status"] == "discard":
        console.print("  (marked as discard – kept on disk, hidden from lists)")
    elif a.process:
        from .processing import process_session
        process_session(s, cat, sid, log=console.print)
    else:
        console.print(f"  next: sglab process {sid}")
    return 0


def cmd_subjects(a):
    s, cat = _ctx()
    if a.sub == "add":
        from .metadata import ask_subject
        sid = a.id or cat.next_subject_id()
        if cat.subject(sid):
            raise SystemExit(f"{sid} already exists")
        ask_subject(sid, cat)
        console.print(f"added {sid}")
        return 0
    t = Table("subject", "sex", "age", "height", "weight", "skin", "sessions", "notes")
    for r in cat.subjects():
        t.add_row(r["subject_id"], str(r["sex"] or ""), str(r["age_years"] or ""), str(r["height_cm"] or ""),
                  str(r["weight_kg"] or ""), str(r["fitzpatrick"] or ""), str(r["n_sessions"]), r["notes"] or "")
    console.print(t)
    return 0


def cmd_list(a):
    s, cat = _ctx()
    rows = cat.sessions(a.subject, a.protocol, include_discarded=a.all)
    t = Table("session", "subject", "protocol", "trial", "dur (s)", "streams", "BP", "processed", "quality", "status")
    for r in rows:
        t.add_row(r["session_id"], r["subject_id"] or "", r["protocol"] or "", str(r["trial"] or ""),
                  f"{r['duration_s'] or 0:.0f}", str(r["n_streams"]), str(r["n_bp"]),
                  f"{r['n_processed']}/{r['n_streams']}", r["quality"] or "", r["status"])
    console.print(t)
    console.print(f"{len(rows)} sessions")
    return 0


def cmd_show(a):
    s, cat = _ctx()
    x = cat.session(a.session)
    if not x:
        raise SystemExit(f"no unique session matching {a.session!r}")
    console.rule(x["session_id"])
    meta = {k: x[k] for k in ("subject_id", "protocol", "trial", "started_at", "duration_s", "status", "quality",
                              "operator", "notes", "path")}
    meta.update(x["extra"])
    t = Table(show_header=False)
    for k, v in meta.items():
        t.add_row(k, str(v))
    console.print(t)
    t = Table("stream", "device", "fw", "fs", "channels", "samples", "dropped", "bad lines")
    for r in x["streams"]:
        t.add_row(r["stream"], r["dev"] or "", r["fw"] or "", str(r["fs"]), r["channels"], str(r["n_samples"]),
                  str(r["n_dropped"]), str(r["n_bad_lines"]))
    console.print(t)
    if x["events"]:
        t = Table("t (s)", "kind", "source", "SBP", "DBP", "HR", "SpO2", "label")
        for e in x["events"]:
            f = lambda v: "" if v is None else f"{v:g}"  # noqa: E731
            t.add_row(f"{e['t_s']:.1f}", e["kind"], e["source"] or "", f(e["sys_mmhg"]), f(e["dia_mmhg"]),
                      f(e["hr_bpm"]), f(e["spo2_pct"]), e["label"] or "")
        console.print(t)
    for r in x["runs"]:
        sm = r["summary"]
        l2 = sm.get("layer2", {})
        console.print(f"run {r['run_id']} [{r['status']}] {r['stream']}: v{r['pipeline_version']} "
                      f"cfg {r['config_hash']} → {r['out_dir']}  "
                      + (f"beats {l2.get('n_good_beats')}/{l2.get('n_beats')}, HR {l2.get('median_hr_bpm')}"
                         if r["status"] == "ok" else sm.get("error", "")))
    return 0


def cmd_process(a):
    from .processing import process_session

    s, cat = _ctx()
    ids = [r["session_id"] for r in cat.sessions()] if a.all else a.sessions
    if not ids:
        raise SystemExit("give session ids or --all")
    for sid in ids:
        console.print(f"[bold]{sid}[/]")
        process_session(s, cat, sid, force=a.force, plot=not a.no_plot, log=console.print)
    return 0


def cmd_reindex(a):
    s, cat = _ctx()
    n = cat.reindex()
    console.print(f"indexed {n} sessions from {s.raw_dir}")
    return 0


def cmd_recover(a):
    """Finalize sessions left in _incoming by a crash / closed terminal."""
    import pandas as pd

    s, cat = _ctx()
    found = sorted(p for p in s.incoming_dir.glob("*") if p.is_dir()) if s.incoming_dir.exists() else []
    for folder in found:
        doc = json.loads((folder / "session.json").read_text()) if (folder / "session.json").exists() else {}
        streams = []
        for csvp in sorted(folder.glob("*.csv")):
            if csvp.name == "events.csv":
                continue
            df = pd.read_csv(csvp)
            chans = [c for c in df.columns if c not in ("t_s", "seq", "t_dev_us")]
            fs = 1e6 / df["t_dev_us"].diff().median() if len(df) > 2 else None
            streams.append({"stream": csvp.stem, "role": csvp.stem.split("_")[0], "file": csvp.name,
                            "fs": round(fs) if fs else None, "channels": chans,
                            "stats": {"n_samples": len(df)},
                            "io": {"time_col": "t_dev_us", "time_unit": "us", "channels": {c: c for c in chans}}})
        for tmp in folder.glob("*.rawlog.tmp"):
            tmp.rename(tmp.with_suffix(""))
        meta = doc.get("metadata", {})
        sid = (f"{folder.name}_{meta.get('subject_id', 'UNK')}_{meta.get('protocol', 'na')}_recovered")
        doc.update(session_id=sid, status="incomplete", streams=streams, metadata=meta)
        if not (folder / "events.csv").exists():
            (folder / "events.csv").write_text("t_s,wall_time,kind,source,sys_mmhg,dia_mmhg,hr_bpm,spo2_pct,label\n")
        (folder / "session.json").write_text(json.dumps(doc, indent=2, default=str))
        dest = s.raw_dir / sid
        shutil.move(str(folder), dest)
        cat.upsert_session(dest)
        console.print(f"recovered {sid} (status=incomplete; operator events typed during the run are lost)")
    if not found:
        console.print("nothing to recover")
    return 0


def cmd_import(a):
    """Bring an existing CSV (e.g. from the old recorder) into the catalog."""
    from ppg.io import load_csv
    from ppg.config import IOConfig

    s, cat = _ctx()
    src = Path(a.csv)
    io = {}
    if a.time_col:
        io["time_col"] = a.time_col
    if a.time_unit:
        io["time_unit"] = a.time_unit
    if a.channel:
        io["channels"] = dict(c.split("=", 1) for c in a.channel)
    df = load_csv(src, IOConfig(**io))  # validates that the pipeline can read it
    started = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc)
    meta = {"subject_id": a.subject, "protocol": a.protocol,
            "trial": a.trial or cat.next_trial(a.subject, a.protocol), "imported_from": str(src)}
    meta.update(_preset(a.meta))
    from .recorder import make_session_id
    sid = make_session_id(started, meta) + "_imp"
    dest = s.raw_dir / sid
    dest.mkdir(parents=True)
    name = f"{a.role}_{src.stem}"
    shutil.copy2(src, dest / f"{name}.csv")
    fs = 1 / df["t"].diff().median()
    doc = {"schema": 1, "session_id": sid, "started_at": started.isoformat(timespec="seconds"),
           "duration_s": float(df["t"].iloc[-1]), "metadata": meta, "status": "ok",
           "subject": cat.subject(a.subject),
           "streams": [{"stream": name, "role": a.role, "file": f"{name}.csv", "fs": round(fs, 3),
                        "channels": df.attrs["channels"], "stats": {"n_samples": len(df)}, "io": io}]}
    (dest / "session.json").write_text(json.dumps(doc, indent=2, default=str))
    (dest / "events.csv").write_text("t_s,wall_time,kind,source,sys_mmhg,dia_mmhg,hr_bpm,spo2_pct,label\n")
    if cat.subject(a.subject) is None:
        cat.upsert_subject({"subject_id": a.subject})
    cat.upsert_session(dest)
    console.print(f"imported → {sid}")
    return 0


def _lan_ips() -> list[str]:
    import socket

    ips = set()
    try:  # the address the OS would use to reach the internet (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("8.8.8.8", 80))
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def cmd_serve(a):
    import secrets

    import uvicorn

    from .web.app import create_app

    s = load_settings()
    host = "0.0.0.0" if a.lan else (a.host or s.srv.get("web", {}).get("host", "127.0.0.1"))
    port = a.port or int(s.srv.get("web", {}).get("port", 8000))
    token = None if s.auth_enabled else a.token
    if token == "auto":
        token = secrets.token_urlsafe(9)
    app = create_app(s, token=token)
    q = f"/?token={token}" if token else "/"
    console.print(f"[bold]NBE PPG lab[/] – data: {s.data_root}")
    console.print(f"  accounts: {'on' if s.auth_enabled else 'off (single-user)'} · MQTT: "
                  f"{s.mqtt['host'] + ':' + str(s.mqtt['port']) if s.mqtt else 'off'}")
    console.print(f"  this computer:  http://127.0.0.1:{port}{q}")
    if s.public_url:
        console.print(f"  public URL:     {s.public_url}")
    if host in ("0.0.0.0", "::"):
        for ip in _lan_ips():
            console.print(f"  local network:  http://{ip}:{port}{q}")
        if not token and not s.auth_enabled:
            console.print("  [yellow]note:[/] anyone on this network can start/stop recordings – "
                          "consider --token auto, or user accounts (deploy/DEPLOY.md)")
    console.print("  Ctrl-C to stop the server (running trials are stopped; `sglab recover` saves them).")
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=True,
                forwarded_allow_ips=s.srv.get("web", {}).get("trusted_proxies", "127.0.0.1"))
    return 0


def cmd_worker(a):
    from .worker import serve

    tok = Path(a.token_file).read_text().strip() if a.token_file else os.environ.get("NBE_WORKER_TOKEN")
    if not tok:
        raise ValueError("give --token-file (or set NBE_WORKER_TOKEN)")
    return serve(a.url, tok, a.name, once=a.once, log=lambda m: print(m, flush=True))


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sglab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("ports").set_defaults(func=cmd_ports)

    r = sub.add_parser("record", help="record from one or more devices")
    r.add_argument("devices", nargs="+", help="device names from lab.toml, serial ports, or sim")
    r.add_argument("--meta", action="append", help="pre-fill a field: key=value (repeatable)")
    r.add_argument("--duration", type=float, help="stop automatically after N seconds")
    r.add_argument("--yes", action="store_true", help="non-interactive: use defaults/--meta for every field")
    r.add_argument("--process", action="store_true", help="run Layers 1-2 right after saving")
    r.set_defaults(func=cmd_record)

    sj = sub.add_parser("subjects", help="list subjects")
    sj.set_defaults(func=cmd_subjects, sub="list", id=None)
    sa = sub.add_parser("subject", help="sglab subject add [--id S004]")
    sa.add_argument("sub", choices=["add"])
    sa.add_argument("--id")
    sa.set_defaults(func=cmd_subjects)

    ls = sub.add_parser("list", help="list sessions")
    ls.add_argument("--subject"); ls.add_argument("--protocol")
    ls.add_argument("--all", action="store_true", help="include discarded")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="details of one session (id or unique part of it)")
    sh.add_argument("session"); sh.set_defaults(func=cmd_show)

    pr = sub.add_parser("process", help="run Layers 1-2")
    pr.add_argument("sessions", nargs="*"); pr.add_argument("--all", action="store_true")
    pr.add_argument("--force", action="store_true"); pr.add_argument("--no-plot", action="store_true")
    pr.set_defaults(func=cmd_process)

    im = sub.add_parser("import", help="import an existing CSV recording")
    im.add_argument("csv"); im.add_argument("--subject", required=True); im.add_argument("--protocol", required=True)
    im.add_argument("--role", default="finger"); im.add_argument("--trial", type=int)
    im.add_argument("--time-col"); im.add_argument("--time-unit")
    im.add_argument("--channel", action="append", help="name=COLUMN")
    im.add_argument("--meta", action="append")
    im.set_defaults(func=cmd_import)

    sub.add_parser("reindex", help="rebuild catalog from session folders").set_defaults(func=cmd_reindex)
    sub.add_parser("recover", help="finalize interrupted recordings").set_defaults(func=cmd_recover)

    sv = sub.add_parser("serve", help="start the web UI")
    sv.add_argument("--host", help="interface to listen on (0.0.0.0 = all; default [web] host or 127.0.0.1)")
    sv.add_argument("--lan", action="store_true", help="shortcut for --host 0.0.0.0 (reachable on your network)")
    sv.add_argument("--port", type=int, help="default [web] port or 8000")
    sv.add_argument("--token", help="require this access token in the URL once ('auto' = random); "
                                    "or set SGLAB_TOKEN")
    sv.set_defaults(func=cmd_serve)

    wk = sub.add_parser("worker", help="run queued processing jobs for a lab server (remote worker)")
    wk.add_argument("url", help="lab server, e.g. https://nbe.evm100.org")
    wk.add_argument("--token-file", help="file with an API token of a worker-role user")
    wk.add_argument("--name", help="worker name shown on the server (default: hostname)")
    wk.add_argument("--once", action="store_true", help="exit when the queue is empty (testing)")
    wk.set_defaults(func=cmd_worker)

    from .cli_server import add_parsers
    add_parsers(sub)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a) or 0
    except (ValueError, KeyError, FileNotFoundError, FileExistsError) as e:
        console.print(f"[red]error:[/] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
