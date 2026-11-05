"""Models page (Layer 4): datasets, model runs with plain-language verdicts, train / sweep.

    GET  /models                          runs, score history, dataset, jobs, buttons
    GET  /models/{name}/{run_id}          one run: verdicts, report figure, sweep trials, config
    POST /models/dataset                  rebuild the Layer-3 dataset (operator, on this server)
    POST /models/train                    train config/pipeline_layer4.json (operator, on this server)
    POST /models/sweep                    sweep: queued for `sglab worker` in remote mode, else a thread
    GET  /api/models                      every run's metrics.json

Datasets and single trainings are light and run here; sweeps are the heavy part.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import model as mdl
from .security import require

router = APIRouter()
viewer = Depends(require("viewer"))
operator = Depends(require("operator"))


def C(request: Request):
    return request.app.state.ctx


def _task(ctx, what: str, fn) -> None:
    """Run fn(task) in a thread; the task dict is what the Models page shows."""
    task = {"what": what, "state": "running", "progress": None, "error": None,
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ctx.model_tasks.insert(0, task)
    del ctx.model_tasks[20:]

    def job():
        try:
            fn(task)
            task["state"] = "done"
        except Exception as e:  # shown on the page
            task["state"], task["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=job, daemon=True).start()


def _num(v):
    """JSON-safe for the page's script (NaN is not valid JSON)."""
    return v if isinstance(v, (int, float)) and v == v else None


def _busy(ctx, what: str) -> bool:
    return any(t["what"].startswith(what) and t["state"] == "running" for t in ctx.model_tasks)


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, user=viewer):
    from ..dataset import list_datasets
    from ..sweep import sweep_spec

    ctx = C(request)
    s = ctx.settings
    runs = mdl.list_runs(s)
    history = [{"name": m["name"], "run_id": m["run_id"], "t": m["trained_at"],
                **{f"{t}_{k}": _num(sp["cv"][k]["mae"]) for t, sp in m["metrics"].items() for k in ("model", "baseline")},
                **({f"{t}_nested": _num(v["model"]["mae"]) for t, v in m["sweep"]["nested"]["metrics"].items()}
                   if (m.get("sweep") or {}).get("nested") else {})}
               for m in runs]
    rows = [{"m": m, "x": mdl.explain(m)} for m in reversed(runs)]
    jobs = []
    if ctx.jobs is not None:
        online = time.time() - max(ctx.workers_seen.values(), default=0) < 120
        for j in ctx.jobs.recent(10, kind="sweep"):
            st = j["state"] + (f" on {j['worker']}" if j["state"] == "running" else "")
            if j["state"] == "queued" and not online:
                st += " (no worker online – it runs when one connects)"
            jobs.append({"what": f"sweep {(j['payload'] or {}).get('name', '')}", "state": st,
                         "progress": j.get("progress"), "error": j["error"], "started": j["created_at"]})
    jobs = [dict(t) for t in ctx.model_tasks] + jobs
    cfg = mdl.layer4_config(s)
    return ctx.render(request, "models.html", {
        "rows": rows, "history": history, "datasets": list_datasets(s), "jobs": jobs, "cfg": cfg,
        "spec": sweep_spec(s), "remote": ctx.jobs is not None, "msg": request.query_params.get("msg")})


@router.get("/models/{name}/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, name: str, run_id: str, user=viewer):
    ctx = C(request)
    m = mdl.get_run(ctx.settings, name, run_id)
    if m is None:
        raise HTTPException(404, f"model run {name}/{run_id} not found")
    return ctx.render(request, "model_run.html", {
        "m": m, "x": mdl.explain(m), "report": f"/files/models/{name}/{run_id}/report.png",
        "cfg": (ctx.settings.data_root / "models" / name / run_id / "config.json").read_text()})


@router.post("/models/dataset")
def build_dataset(request: Request, user=operator):
    ctx = C(request)
    if not _busy(ctx, "dataset"):
        def fn(task):
            from ..dataset import build
            c = ctx.cat()
            try:
                m = build(ctx.settings, c, "bp", log=lambda *_: None)
            finally:
                c.close()
            task["progress"] = f"{m['summary'].get('n_rows', 0)} rows"
        _task(ctx, "dataset bp (rebuild)", fn)
        ctx.audit(request, "model.dataset", "bp")
    return RedirectResponse("/models?msg=dataset", status_code=303)


@router.post("/models/train")
def train(request: Request, user=operator):
    ctx = C(request)

    def fn(task):
        m = mdl.train_run(ctx.settings, "bp", log=lambda *_: None)
        task["progress"], task["link"] = m["run_id"], f"/models/{m['name']}/{m['run_id']}"
    _task(ctx, "train " + mdl.default_name("bp", mdl.layer4_config(ctx.settings)), fn)
    ctx.audit(request, "model.train", "bp")
    return RedirectResponse("/models?msg=train", status_code=303)


@router.post("/models/sweep")
async def sweep(request: Request, user=operator):
    from ..sweep import sweep_run, sweep_spec

    ctx = C(request)
    f = await request.form()
    s = ctx.settings
    base = mdl.layer4_config(s)
    role = str(f.get("role") or base.role)
    if role not in ("finger", "glasses", "head"):
        raise HTTPException(400, "role: finger, glasses or head")
    base = type(base).from_dict({**base.to_dict(), "role": role})
    spec = sweep_spec(s)
    try:
        spec["trials"] = max(5, min(int(f.get("trials") or spec["trials"]), 500))
    except ValueError:
        raise HTTPException(400, "trials: a number") from None
    name = mdl.default_name("bp", base, sweep=True)
    if ctx.jobs is not None:
        ctx.jobs.enqueue_sweep({"dataset": "bp", "name": name, "layer4": base.to_dict(), "spec": spec})
    else:
        def fn(task):
            m = sweep_run(s, "bp", spec, name, base, log=lambda *_: None,
                          progress=lambda d, n: task.update(progress=f"{d}/{n}"))
            task["link"] = f"/models/{m['name']}/{m['run_id']}"
        _task(ctx, f"sweep {name}", fn)
    ctx.audit(request, "model.sweep", name, {"trials": spec["trials"]})
    return RedirectResponse("/models?msg=sweep", status_code=303)


def _json_safe(v):
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    return None if isinstance(v, float) and v != v else v


@router.get("/api/models")
def api_models(request: Request, user=viewer):
    return _json_safe(mdl.list_runs(C(request).settings))
