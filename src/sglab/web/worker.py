"""API for remote processing workers (`sglab worker`, see sglab.jobs). Role: worker (or admin).

    GET  /api/worker/ping                     token / role / mode check (setup scripts)
    POST /api/worker/claim?wait=25            204 = nothing to do, else the job (long poll)
    GET  /api/worker/jobs/{id}/raw/{stream}   the stream's raw CSV
    PUT  /api/worker/jobs/{id}/out/{stream}/{name}   one output file (raw body), staged
    POST /api/worker/jobs/{id}/runs/{stream}  {status, summary, pipeline_version, config_hash}:
                                              swaps the staged files in, records the run
    POST /api/worker/jobs/{id}/finish         {error: null | "..."}
    GET  /api/worker/jobs                     recent jobs (admin)

Every job call carries the header X-NBE-Worker: <name>, and only the worker holding the job
may touch it. Outputs land in data/processed/<session>/<stream>/ exactly as with local
processing, so the web UI doesn't know the difference.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..auth import has_role
from .security import require

router = APIRouter()
admin = Depends(require("admin"))
NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")


def C(request: Request):
    return request.app.state.ctx


def worker_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "login required")
    if user["role"] != "worker" and not has_role(user, "admin"):
        raise HTTPException(403, "needs the worker role")
    return user


worker = Depends(worker_user)


def _queue(ctx):
    if ctx.jobs is None:
        raise HTTPException(409, 'this server processes locally ([processing] mode = "local")')
    return ctx.jobs


def _worker_name(request: Request) -> str:
    name = request.headers.get("x-nbe-worker", "")
    if not NAME.match(name):
        raise HTTPException(400, "send X-NBE-Worker: <name> (letters, digits, . _ -)")
    return name


def _job(request: Request, job_id: int) -> dict:
    ctx = C(request)
    j = _queue(ctx).get(job_id)
    if j is None or j["state"] != "running" or j["worker"] != _worker_name(request):
        raise HTTPException(409, f"job {job_id} is not running on this worker (lease expired?)")
    return j


def _session(ctx, sid: str) -> tuple[dict, dict]:
    c = ctx.cat()
    try:
        s = c.session(sid)
        ok_runs = c.q("SELECT DISTINCT stream, config_hash FROM runs WHERE session_id=? AND status='ok'", (sid,)) \
            if s else []
    finally:
        c.close()
    if s is None:
        raise HTTPException(404, f"session {sid} not found")
    s["ok_runs"] = ok_runs
    return s, json.loads((ctx.settings.data_root / s["path"] / "session.json").read_text())


def _stream(ctx, sid: str, stream: str) -> tuple[dict, dict]:
    s, doc = _session(ctx, sid)
    st = next((x for x in doc.get("streams", []) if x.get("stream") == stream), None)
    if st is None:
        raise HTTPException(404, f"stream {stream} not in session {sid}")
    return s, st


def _staging(ctx, job_id: int, stream: str):
    if not NAME.match(stream):
        raise HTTPException(400, "bad stream name")
    return ctx.settings.processed_dir / "_staging" / str(job_id) / stream


@router.get("/api/worker/ping")
def ping(request: Request, user=worker):
    return {"ok": True, "user": user["username"], "pending": _queue(C(request)).pending()}


@router.post("/api/worker/claim")
async def claim(request: Request, wait: float = 0, user=worker):
    ctx = C(request)
    q, name = _queue(ctx), _worker_name(request)
    deadline = time.monotonic() + min(max(wait, 0), 55)
    while True:
        ctx.workers_seen[name] = time.time()
        j = q.claim(name)
        if j is not None:
            break
        if time.monotonic() >= deadline or await request.is_disconnected():
            return Response(status_code=204)
        await asyncio.sleep(1)
    from ..processing import pipeline_configs, site_overrides

    try:
        s, doc = _session(ctx, j["session_id"])
    except HTTPException as e:  # session gone (reindexed away): nothing to do
        q.finish(j["job_id"], str(e.detail))
        return Response(status_code=204)
    cfg1, cfg2 = pipeline_configs(ctx.settings)
    folder = ctx.settings.data_root / s["path"]
    return {"job_id": j["job_id"], "session_id": s["session_id"], "force": bool(j["force"]),
            "streams": [st for st in doc.get("streams", []) if st.get("file") and (folder / st["file"]).exists()],
            "configs": {"layer1": cfg1.to_dict(), "layer2": cfg2.to_dict(), "sites": site_overrides(ctx.settings)},
            "ok_runs": s["ok_runs"]}


@router.get("/api/worker/jobs/{job_id}/raw/{stream}")
def raw(request: Request, job_id: int, stream: str, user=worker):
    ctx = C(request)
    j = _job(request, job_id)
    s, st = _stream(ctx, j["session_id"], stream)
    folder = (ctx.settings.data_root / s["path"]).resolve()
    p = (folder / st.get("file", "")).resolve()
    if folder not in p.parents or not p.is_file():
        raise HTTPException(404, "no data file")
    ctx.jobs.touch(job_id)
    return FileResponse(p, media_type="text/csv")


@router.put("/api/worker/jobs/{job_id}/out/{stream}/{name}")
async def put_output(request: Request, job_id: int, stream: str, name: str, user=worker):
    ctx = C(request)
    _job(request, job_id)
    if not NAME.match(name):
        raise HTTPException(400, "bad file name")
    d = _staging(ctx, job_id, stream)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.part"
    n = 0
    with tmp.open("wb") as f:
        async for chunk in request.stream():
            n += len(chunk)
            f.write(chunk)
    tmp.replace(d / name)
    ctx.jobs.touch(job_id)
    return {"stored": name, "bytes": n}


@router.post("/api/worker/jobs/{job_id}/runs/{stream}")
async def commit_run(request: Request, job_id: int, stream: str, user=worker):
    ctx = C(request)
    j = _job(request, job_id)
    s, _ = _stream(ctx, j["session_id"], stream)
    body = await request.json()
    status = body.get("status")
    if status not in ("ok", "error"):
        raise HTTPException(400, "status must be ok or error")
    sid = s["session_id"]
    staged = _staging(ctx, job_id, stream)
    out = ctx.settings.processed_dir / sid / stream
    if status == "ok":
        if not staged.is_dir():
            raise HTTPException(400, "no output files uploaded for this stream")
        # same result as local processing: a fresh folder with exactly this run's files
        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(out)
    c = ctx.cat()
    try:
        c.add_run(sid, stream, str(body.get("pipeline_version", "")), str(body.get("config_hash", "")),
                  str(out.relative_to(ctx.settings.data_root)), status,
                  {**(body.get("summary") or {}), "worker": j["worker"]})
    finally:
        c.close()
    ctx.jobs.touch(job_id)
    return {"ok": True}


@router.post("/api/worker/jobs/{job_id}/finish")
async def finish(request: Request, job_id: int, user=worker):
    ctx = C(request)
    _job(request, job_id)
    body = await request.json()
    ctx.jobs.finish(job_id, body.get("error") or None)
    shutil.rmtree(ctx.settings.processed_dir / "_staging" / str(job_id), ignore_errors=True)
    return {"ok": True}


@router.get("/api/worker/jobs")
def jobs(request: Request, limit: int = 50, user=admin):
    ctx = C(request)
    return {"workers_seen": {k: round(time.time() - v) for k, v in ctx.workers_seen.items()},
            "jobs": _queue(ctx).recent(limit)}
