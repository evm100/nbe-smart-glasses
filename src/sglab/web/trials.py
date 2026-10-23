"""Devices page, trial pages and the trial API (operator role)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..commands import CommandError
from ..control import TrialError, form_to_meta
from ..metadata import SUBJECT_FIELDS
from .security import require

router = APIRouter()
viewer = Depends(require("viewer"))
operator = Depends(require("operator"))


def C(request: Request):
    return request.app.state.ctx


def _devices(ctx) -> list[dict]:
    return ctx.directory.list(ctx.manager.busy())


def _skip_fields(ctx) -> set[str]:
    """With real user accounts the operator is the logged-in user, not a typed field."""
    return {"operator"} if ctx.auth else set()


def before_fields(ctx, user: dict, form: dict | None = None) -> list[dict]:
    """session_fields.toml 'before' fields with their current default values."""
    c = ctx.cat()
    try:
        last = ctx.manager.last_values(user["username"] if ctx.auth else None)
        subjects = [r["subject_id"] for r in c.subjects()]
        out = []
        for f in ctx.settings.fields:
            if f.get("ask", "before") != "before" or f["name"] in _skip_fields(ctx):
                continue
            f = dict(f)
            val = f.get("default")
            if f.get("remember") and f["name"] in last:
                val = last[f["name"]]
            if f.get("type") == "subject":
                f["options"] = subjects
                if val not in subjects:
                    val = subjects[-1] if subjects else "__new__"
            out.append({**f, "value": val})
        vals = {x["name"]: x["value"] for x in out}
        for f in out:
            if form and f["name"] in form:
                f["value"] = form[f["name"]]
            elif f["name"] == "trial" and f["value"] == "auto":
                f["value"] = c.next_trial(vals.get("subject_id") or "", vals.get("protocol") or "")
        return out
    finally:
        c.close()


def after_fields(ctx) -> list[dict]:
    return [{**f, "value": f.get("default")} for f in ctx.settings.fields if f.get("ask") == "after"]


def _trial_or_404(ctx, tid: str):
    try:
        return ctx.manager.get(tid)
    except TrialError:
        sid = ctx.manager.finished.get(tid)
        if sid:
            raise HTTPException(410, f"this trial was saved as {sid}") from None
        raise HTTPException(404, f"no active trial {tid} (finished, or the server restarted)") from None


# ------------------------------------------------------------------ devices
@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, user=viewer):
    ctx = C(request)
    return ctx.render(request, "devices.html", {"devices": _devices(ctx)})


@router.get("/api/devices")
def api_devices(request: Request, user=viewer):
    ctx = C(request)
    return {"devices": _devices(ctx), "mqtt": ctx.mqtt_state(), "trials": ctx.manager.active()}


# ------------------------------------------------------------------ trial pages
def _new_form(request, ctx, user, error=None, form=None, chosen=None):
    c = ctx.cat()
    try:
        next_id = c.next_subject_id()
    finally:
        c.close()
    last = ctx.manager.last_values(user["username"] if ctx.auth else None).get("_devices", [])
    return ctx.render(request, "trial_new.html", {
        "devices": _devices(ctx), "fields": before_fields(ctx, user, form), "last_devices": chosen or last,
        "subject_fields": SUBJECT_FIELDS, "next_subject_id": next_id, "error": error},
        status_code=400 if error else 200)


@router.get("/trial/new", response_class=HTMLResponse)
def trial_new(request: Request, user=operator):
    return _new_form(request, C(request), user)


@router.post("/trial/start")
async def trial_start(request: Request, user=operator):
    ctx = C(request)
    fd = await request.form()
    form, devices = dict(fd), fd.getlist("devices")
    try:
        new_subject = None
        if form.get("subject_id") == "__new__":
            form["subject_id"] = (form.get("new_subject_id") or "").strip()
            if not form["subject_id"]:
                raise TrialError("enter an ID for the new subject")
            new_subject = {}
            for f in SUBJECT_FIELDS:
                raw = (form.get(f"subj_{f['name']}") or "").strip()
                try:
                    new_subject[f["name"]] = float(raw) if f.get("type") == "float" and raw else (raw or None)
                except ValueError:
                    raise TrialError(f"{f['prompt'].strip()}: expected a number") from None
        pre = form_to_meta(ctx.settings.fields, "before", form, skip=_skip_fields(ctx))
        if ctx.auth:
            pre["operator"] = user["username"]
        trial = ctx.manager.start(devices, pre, new_subject, owner=user["username"])
    except TrialError as e:
        return _new_form(request, ctx, user, error=str(e), form=form, chosen=devices)
    ctx.audit(request, "trial.start", trial.id, {"devices": devices, "subject": pre.get("subject_id"),
                                                 "protocol": pre.get("protocol")})
    return RedirectResponse(f"/trials/{trial.id}", status_code=303)


@router.get("/trials/{tid}", response_class=HTMLResponse)
def trial_page(request: Request, tid: str, user=operator):
    ctx = C(request)
    try:
        t = ctx.manager.get(tid)
    except TrialError:
        sid = ctx.manager.finished.get(tid)
        if sid:
            return RedirectResponse(f"/sessions/{sid}", status_code=303)
        raise HTTPException(404, f"no active trial {tid} (finished, or the server restarted)") from None
    st = ctx.manager.status(tid)
    if t.state == "stopped":
        return ctx.render(request, "trial_finish.html", {"t": st, "fields": after_fields(ctx), "error": None})
    return ctx.render(request, "trial_live.html", {"t": st, "quick_markers": ctx.settings.lab.get("ui", {}).get(
        "quick_markers", ["start task", "end task", "subject moved", "talking", "glasses adjusted"])})


@router.post("/trials/{tid}/finish")
async def trial_finish(request: Request, tid: str, user=operator):
    ctx = C(request)
    _trial_or_404(ctx, tid)
    form = dict(await request.form())
    try:
        post = form_to_meta(ctx.settings.fields, "after", form)
        sid = ctx.manager.finalize(tid, post, user["username"])
    except TrialError as e:
        return ctx.render(request, "trial_finish.html", {"t": ctx.manager.status(tid), "fields": after_fields(ctx),
                                                         "error": str(e)}, status_code=400)
    ctx.audit(request, "trial.save", sid, {"trial": tid, "keep": post.get("keep", True)})
    if form.get("process") and post.get("keep", True) is not False:
        ctx.start_processing(sid)
    return RedirectResponse(f"/sessions/{sid}", status_code=303)


# ------------------------------------------------------------------ trial API
@router.get("/api/trials")
def api_trials(request: Request, user=operator):
    return C(request).manager.active()


@router.get("/api/trials/{tid}")
def api_trial(request: Request, tid: str, user=operator):
    ctx = C(request)
    _trial_or_404(ctx, tid)
    return ctx.manager.status(tid)


@router.get("/api/trials/{tid}/live")
def api_trial_live(request: Request, tid: str, seconds: float = 8.0, after: str | None = None, user=operator):
    """after = comma-separated `next` cursors from the previous reply (one per stream): only newer
    points come back, undecimated (the live page polls a few times per second and animates)."""
    ctx = C(request)
    _trial_or_404(ctx, tid)
    try:
        cursors = [int(x) for x in after.split(",")] if after else None
    except ValueError:
        raise HTTPException(400, "after: comma-separated integers") from None
    return ctx.manager.live(tid, min(max(seconds, 1.0), 30.0), after=cursors)


@router.post("/api/trials/{tid}/event")
async def api_trial_event(request: Request, tid: str, user=operator):
    ctx = C(request)
    _trial_or_404(ctx, tid)
    body = await request.json()
    try:
        return ctx.manager.add_event(tid, body.get("kind", ""), body)
    except (TrialError, CommandError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/trials/{tid}/stop")
def api_trial_stop(request: Request, tid: str, user=operator):
    ctx = C(request)
    _trial_or_404(ctx, tid)
    try:
        ctx.manager.stop(tid)
    except TrialError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ctx.audit(request, "trial.stop", tid)
    return {"state": "stopped"}


@router.get("/api/next_trial")
def api_next_trial(request: Request, subject: str = "", protocol: str = "", user=operator):
    c = C(request).cat()
    try:
        return {"trial": c.next_trial(subject, protocol)}
    finally:
        c.close()
