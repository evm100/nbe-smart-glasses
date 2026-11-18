"""Home (operator dashboard), Platform (what the system measures and where it is heading) and Help.

    GET /home                     start here: is everything ready, what happened recently, the board
    GET /platform                 sensors -> measures -> insights, from config/sensors.toml + the catalog
    GET /help                     how to run a recording, what to do when something looks wrong
    GET /api/platform             the same registry as JSON
    GET /api/trials/{tid}/aux     live values of the further sensors of a running trial
"""
from __future__ import annotations

import json
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import sensors
from ..control import TrialError
from .security import require

router = APIRouter()
viewer = Depends(require("viewer"))
operator = Depends(require("operator"))

_usage_cache: dict = {"t": 0.0, "v": None}


def C(request: Request):
    return request.app.state.ctx


def usage(ctx, reg: dict) -> dict:
    """sensor id -> {"sessions": n, "last": session id}: which recordings contain it (scan of the
    session.json files, cached for a minute)."""
    if _usage_cache["v"] is not None and time.time() - _usage_cache["t"] < 60 and _usage_cache.get("root") == str(ctx.settings.data_root):
        return _usage_cache["v"]
    out: dict[str, dict] = {}
    c = ctx.cat()
    try:
        rows = c.sessions()
    finally:
        c.close()
    for r in rows:   # newest first
        try:
            doc = json.loads((ctx.settings.data_root / r["path"] / "session.json").read_text())
        except (OSError, ValueError):
            continue
        for sid in sensors.seen_in_session(doc, reg):
            u = out.setdefault(sid, {"sessions": 0, "last": r["session_id"]})
            u["sessions"] += 1
    _usage_cache.update(t=time.time(), v=out, root=str(ctx.settings.data_root))
    return out


def registry(ctx) -> dict:
    reg = sensors.load(ctx.settings.root)
    use = usage(ctx, reg)
    by_id = {s["id"]: s for s in reg["sensors"]}
    for s in reg["sensors"]:
        s["usage"] = use.get(s["id"], {"sessions": 0, "last": None})
        s["status_label"] = sensors.STATUS.get(s["status"], s["status"])
        s["used_by"] = [i["name"] for i in reg["insights"] if s["id"] in i.get("uses", [])]
    for i in reg["insights"]:
        i["status_label"] = sensors.STATUS.get(i["status"], i["status"])
        i["uses_named"] = [{"id": u, "name": by_id[u]["name"] if u in by_id else u,
                            "status": by_id[u]["status"] if u in by_id else "planned"} for u in i.get("uses", [])]
    reg["status_help"] = sensors.STATUS_HELP
    reg["status_labels"] = sensors.STATUS
    return reg


@router.get("/platform", response_class=HTMLResponse)
def platform_page(request: Request, user=viewer):
    ctx = C(request)
    reg = registry(ctx)
    sim = [k for k, d in ctx.settings.lab.get("devices", {}).items() if (d.get("sim") or {}).get("aux")]
    return ctx.render(request, "platform.html", {"reg": reg, "sim_multi": sim[0] if sim and ctx.settings.allow_local_devices else None})


@router.get("/api/platform")
def api_platform(request: Request, user=viewer):
    return registry(C(request))


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, user=viewer):
    ctx = C(request)
    return ctx.render(request, "help.html", {"has_nodes": ctx.hub is not None,
                                             "local_devices": ctx.settings.allow_local_devices})


@router.get("/home", response_class=HTMLResponse)
def home_page(request: Request, user=viewer):
    ctx = C(request)
    c = ctx.cat()
    try:
        rows = c.sessions()
        n_subjects = len(c.subjects())
    finally:
        c.close()
    devices = ctx.directory.list(ctx.manager.busy())
    try:
        du = shutil.disk_usage(ctx.settings.data_root)
        disk = {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1), "free_frac": du.free / du.total}
    except OSError:
        disk = None
    workers = None
    if ctx.jobs is not None:
        workers = time.time() - max(ctx.workers_seen.values(), default=0) < 120
    checks = []
    real = [d for d in devices if d["kind"] != "sim"]
    online = [d for d in real if d["online"]]
    mq = ctx.mqtt_state()
    if mq is not None:
        checks.append({"ok": mq["connected"], "label": "Server ↔ devices link",
                       "text": "connected" if mq["connected"] else "down — Wi-Fi devices can't reach the server",
                       "fix": None if mq["connected"] else "Tell an admin: the message broker (mosquitto) or its connection needs a look."})
    checks.append({"ok": bool(online) or not real, "label": "Devices",
                   "text": (f"{len(online)} of {len(real)} online" + (": " + ", ".join(d["label"] for d in online) if online else ""))
                   if real else "only simulated devices are set up",
                   "fix": None if online or not real else "Switch a device on (or plug in the glasses and start the USB gateway), wait ~15 s. See Help → “My device is offline”."})
    if disk:
        checks.append({"ok": disk["free_gb"] > 5, "label": "Storage", "text": f"{disk['free_gb']} GB free of {disk['total_gb']} GB",
                       "fix": None if disk["free_gb"] > 5 else "Under 5 GB left: tell an admin before recording camera sessions."})
    if workers is not None:
        checks.append({"ok": workers, "label": "Processing computer", "text": "online" if workers else "not seen in the last 2 min",
                       "fix": None if workers else "Recordings are saved normally; they are processed when the processing computer is back."})
    counts = ctx.processing_counts()
    todo = [r for r in rows if r["n_processed"] < r["n_streams"] and r["status"] != "recording"]
    return ctx.render(request, "home.html", {
        "checks": checks, "recent": rows[:6], "n_sessions": len(rows), "n_subjects": n_subjects,
        "devices": devices, "counts": counts, "unprocessed": len(todo),
        "reg": sensors.load(ctx.settings.root), "status_labels": sensors.STATUS})


@router.get("/api/trials/{tid}/aux")
def api_trial_aux(request: Request, tid: str, after: str | None = None, user=operator):
    """Further sensors of a running trial: per stream its channels (named from config/sensors.toml),
    last values and the points newer than `after` ("stream:next,stream:next" from the last reply)."""
    ctx = C(request)
    cursors = {}
    for part in (after or "").split(","):
        name, _, n = part.rpartition(":")
        if name and n.lstrip("-").isdigit():
            cursors[name] = int(n)
    try:
        streams = ctx.manager.aux_live(tid, cursors)
    except TrialError as e:
        raise HTTPException(404, str(e)) from None
    reg = sensors.load(ctx.settings.root)
    for a in streams:
        a["labels"] = sensors.label_channels(reg, a)
    return {"streams": streams}
