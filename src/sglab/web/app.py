"""FastAPI lab server: trials, devices / nodes, users, browsing, processing.

    sglab serve                      -> http://127.0.0.1:8000 (this computer only)
    sglab serve --lan                -> reachable from the local network
    /docs                            -> auto-generated API docs

Deployment (multi-user, MQTT nodes, behind nginx): see deploy/DEPLOY.md.
Run a single server process (no --workers): trials, the MQTT connection and
live buffers live in this process.

Module layout
    app.py       factory, auth middleware, browsing + processing routes
    trials.py    devices page, trial pages and trial API
    admin.py     login/logout, account, users, nodes (registration, credentials), audit
    models.py    Models page (Layer 4): datasets, model runs, train / sweep
    security.py  identity, roles, CSRF origin check, login rate limiting
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..auth import AuthStore, has_role
from ..catalog import Catalog
from ..control import TrialManager
from ..devices import DeviceDirectory
from ..firmware import FirmwareStore
from ..jobs import JobQueue
from ..nodes import NodeRegistry
from ..settings import Settings, load_settings
from ..timebase import device_clock, read_events, run_clock, session_clocks, session_t0_unix
from . import security as sec

HERE = Path(__file__).parent


def minmax_decimate(t: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Keep each bucket's min and max so peaks survive downsampling for the browser."""
    n = t.size
    if n <= max_points:
        return t, y
    nb = max_points // 2
    edges = np.linspace(0, n, nb + 1).astype(int)
    ti, yi = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = y[a:b]
        if not np.isfinite(seg).any():
            ti += [t[a]]; yi += [np.nan]
            continue
        i0, i1 = a + int(np.nanargmin(seg)), a + int(np.nanargmax(seg))
        for i in sorted((i0, i1)):
            ti.append(t[i]); yi.append(y[i])
    return np.array(ti), np.array(yi)


def _clean(v):
    """JSON-safe lists (NaN -> None)."""
    a = np.asarray(v, dtype=float)
    return [None if not np.isfinite(x) else round(float(x), 6) for x in a]


@dataclass
class Ctx:
    """Shared server state, reachable from routes as request.app.state.ctx."""
    settings: Settings
    templates: Jinja2Templates
    auth: AuthStore | None
    registry: NodeRegistry
    hub: object | None
    directory: DeviceDirectory
    manager: TrialManager
    token: str | None = None
    firmware: FirmwareStore | None = None
    processing: dict = field(default_factory=dict)  # session_id -> state (local mode)
    jobs: JobQueue | None = None                    # remote mode: queue for `sglab worker`
    workers_seen: dict = field(default_factory=dict)  # worker name -> time.time() of its last poll
    model_tasks: list = field(default_factory=list)   # Models page: dataset builds, trainings, local sweeps

    def cat(self) -> Catalog:
        return Catalog(self.settings.db_path, self.settings.data_root)

    def render(self, request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
        user = getattr(request.state, "user", None)
        trials = self.manager.active() if has_role(user, "operator") else []
        return self.templates.TemplateResponse(request, name, {
            **(ctx or {}), "user": user, "active_trials": trials, "auth_enabled": bool(self.auth),
            "is_admin": has_role(user, "admin"), "is_operator": has_role(user, "operator"),
            "mqtt": self.mqtt_state()}, status_code=status_code)

    def mqtt_state(self) -> dict | None:
        if self.hub is None:
            return None
        return {"connected": self.hub.connected, "error": self.hub.last_error}

    def audit(self, request: Request, action: str, target: str = "", detail: dict | None = None):
        if self.auth:
            user = getattr(request.state, "user", None)
            self.auth.audit(user["username"] if user else None, action, target, detail, sec.client_ip(request))

    def processing_state(self, sid: str) -> str | None:
        if self.jobs is None:
            return self.processing.get(sid)
        st = self.jobs.state(sid)
        if st == "queued" and time.time() - max(self.workers_seen.values(), default=0) > 120:
            st = "queued (no processing worker online – it runs when one connects)"
        return st

    def start_processing(self, sid: str, force: bool = False):
        if self.jobs is not None:
            self.jobs.enqueue(sid, force)
            return

        def job():
            from ..processing import process_session
            self.processing[sid] = "running"
            c = self.cat()
            try:
                process_session(self.settings, c, sid, force=force, log=lambda *_: None)
                self.processing[sid] = "done"
            except Exception as e:  # pragma: no cover
                self.processing[sid] = f"error: {e}"
            finally:
                c.close()

        threading.Thread(target=job, daemon=True).start()

    def start_processing_many(self, sids: list[str]):
        """Queue several sessions (remote mode), or process them one after another in one thread."""
        if self.jobs is not None:
            for sid in sids:
                self.jobs.enqueue(sid)
            return
        for sid in sids:
            self.processing[sid] = "queued"

        def job():
            from ..processing import process_session
            c = self.cat()
            try:
                for sid in sids:
                    if self.processing.get(sid) != "queued":  # cancelled meanwhile
                        continue
                    self.processing[sid] = "running"
                    try:
                        process_session(self.settings, c, sid, log=lambda *_: None)
                        self.processing[sid] = "done"
                    except Exception as e:  # pragma: no cover
                        self.processing[sid] = f"error: {e}"
            finally:
                c.close()

        threading.Thread(target=job, daemon=True).start()

    def cancel_processing(self) -> int:
        """Drop queued processing; in remote mode running jobs too (their workers stop after the
        current stream). Local mode can't interrupt the session being processed."""
        if self.jobs is not None:
            ids = self.jobs.cancel("process")
            for jid in ids:
                shutil.rmtree(self.settings.processed_dir / "_staging" / str(jid), ignore_errors=True)
            return len(ids)
        sids = [sid for sid, st in self.processing.items() if st == "queued"]
        for sid in sids:
            self.processing.pop(sid, None)
        return len(sids)

    def processing_counts(self) -> dict:
        if self.jobs is not None:
            return self.jobs.counts("process")
        sts = list(self.processing.values())
        return {"queued": sts.count("queued"), "running": sts.count("running")}


def create_app(settings: Settings | None = None, token: str | None = None, hub=None) -> FastAPI:
    """token: shared access token for single-user mode (ignored when [auth] is enabled).
    hub: an MqttHub to use instead of creating one from [mqtt] (tests)."""
    import os

    settings = settings or load_settings()
    token = token if token is not None else os.environ.get("SGLAB_TOKEN") or None
    settings.data_root.mkdir(parents=True, exist_ok=True)
    auth = AuthStore(settings.db_path) if settings.auth_enabled else None
    registry = NodeRegistry(settings.db_path, settings.pki_dir)
    own_hub = False
    if hub is None and settings.mqtt:
        from ..mqtt_hub import MqttHub
        hub, own_hub = MqttHub(settings.mqtt, allowed=registry.enabled_ids), True
    if hub is not None:
        from ..mqtt_hub import set_hub
        set_hub(hub)
        hub.on_mac = registry.set_mac
    directory = DeviceDirectory(settings, registry, hub)
    ctx = Ctx(settings=settings, templates=Jinja2Templates(directory=str(HERE / "templates")), auth=auth,
              registry=registry, hub=hub, directory=directory, manager=TrialManager(settings, directory),
              token=None if auth else token, firmware=FirmwareStore(settings.firmware_dir),
              jobs=JobQueue(settings.db_path) if settings.processing_mode == "remote" else None)

    @asynccontextmanager
    async def lifespan(app):
        if own_hub:
            hub.start()
        yield
        ctx.manager.shutdown()
        if own_hub:
            hub.stop()

    app = FastAPI(title="NBE smart-glasses lab", version="0.4.0", lifespan=lifespan,
                  docs_url="/docs", redoc_url=None)
    app.state.ctx = ctx
    app.state.manager = ctx.manager
    # plotly.js is vendored so the UI works without internet
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ---------------------------------------------------------------- middleware
    secure_cookie = bool(settings.public_url and settings.public_url.startswith("https"))
    app.state.secure_cookie = secure_cookie

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.url.path
        public = path.startswith(sec.PUBLIC_PATHS)
        if ctx.token and not auth:  # single-user mode behind a shared link token
            given = request.query_params.get("token")
            if given and sec.secrets.compare_digest(given, ctx.token):
                resp = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
                resp.set_cookie("sglab_token", ctx.token, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
                return resp
            if not public and not sec.shared_token_ok(request, ctx.token):
                return HTMLResponse("<h3>Smart-glasses lab</h3><p>Access token required: open the link "
                                    "printed by <code>sglab serve</code> (it ends in <code>?token=…</code>).</p>",
                                    status_code=401)
        user, how = sec.identify(request, ctx)
        request.state.user, request.state.auth_via = user, how
        if user is None and not public:
            if path.startswith("/api/") or request.method != "GET":
                return JSONResponse({"error": "login required"}, status_code=401)
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={nxt}", status_code=303)
        if auth and how == "cookie" and request.method in sec.UNSAFE and not sec.same_origin(request, settings.public_url):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if path.startswith("/api/") or path in ("/login", "/account"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return ctx.render(request, "error.html", {"code": exc.status_code, "message": exc.detail},
                          status_code=exc.status_code)

    viewer = Depends(sec.require("viewer"))
    admin = Depends(sec.require("admin"))

    # ---------------------------------------------------------------- helpers
    def get_session(sid: str) -> dict:
        c = ctx.cat()
        try:
            s = c.session(sid)
        finally:
            c.close()
        if not s:
            raise HTTPException(404, f"session {sid} not found")
        return s

    def session_doc(s: dict) -> dict:
        return json.loads((settings.data_root / s["path"] / "session.json").read_text())

    def clock_for(s: dict, r: dict) -> tuple[float, float]:
        """(t0, k) for a run: session time = t0 + k * pipeline time (see sglab.timebase)."""
        doc, t0_unix = session_clocks(settings.data_root / s["path"])
        st = next((x for x in doc.get("streams", []) if x.get("stream") == r["stream"]), {})
        t0, k, _ = run_clock(st, r["summary"], t0_unix)
        return t0, k

    def run_for(s: dict, stream: str) -> dict | None:
        for r in s["runs"]:
            if r["stream"] == stream and r["status"] == "ok":
                return r
        return None

    # ------------------------------------------------------------------ pages
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mqtt": ctx.mqtt_state()}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, subject: str | None = None, protocol: str | None = None, all: bool = False,
              mine: bool = False, queued: int | None = None, user=viewer):
        c = ctx.cat()
        try:
            rows = c.sessions(subject or None, protocol or None, include_discarded=all)
            subjects = [r["subject_id"] for r in c.subjects()]
            protocols = sorted({r["protocol"] for r in c.sessions(include_discarded=True) if r["protocol"]})
        finally:
            c.close()
        if mine:
            rows = [r for r in rows if r.get("operator") == user["username"]]
        return ctx.render(request, "index.html", {
            "rows": rows, "subjects": subjects, "protocols": protocols,
            "f_subject": subject or "", "f_protocol": protocol or "", "f_all": all, "f_mine": mine,
            "queued": queued, "remote": ctx.jobs is not None, "counts": ctx.processing_counts(),
            "cancelled": request.query_params.get("cancelled")})

    @app.post("/sessions/process-all")
    async def process_all(request: Request, user=Depends(sec.require("operator"))):
        """Process (new/changed streams only) every non-discarded session matching the list's filters."""
        f = await request.form()
        subject, protocol = f.get("subject", ""), f.get("protocol", "")
        c = ctx.cat()
        try:
            sids = [r["session_id"] for r in c.sessions(subject or None, protocol or None)
                    if r["status"] != "recording"]
        finally:
            c.close()
        ctx.start_processing_many(sids)
        ctx.audit(request, "session.process_all", "", {"subject": subject, "protocol": protocol, "n": len(sids)})
        q = urllib.parse.urlencode({k: v for k, v in
                                    {"subject": subject, "protocol": protocol, "queued": len(sids)}.items() if v != ""})
        return RedirectResponse(f"/?{q}", status_code=303)

    @app.post("/sessions/cancel-processing")
    def cancel_processing(request: Request, user=Depends(sec.require("operator"))):
        """Empty the processing queue and stop running processing jobs (not Layer-4 sweeps)."""
        n = ctx.cancel_processing()
        ctx.audit(request, "session.cancel_processing", "", {"n": n})
        return RedirectResponse(f"/?cancelled={n}", status_code=303)

    @app.get("/subjects", response_class=HTMLResponse)
    def subjects_page(request: Request, user=viewer):
        c = ctx.cat()
        try:
            rows = c.subjects()
        finally:
            c.close()
        return ctx.render(request, "subjects.html", {"rows": rows})

    @app.get("/sessions/{sid}", response_class=HTMLResponse)
    def session_page(request: Request, sid: str, user=viewer):
        s = get_session(sid)
        doc = session_doc(s)
        images = {}
        for r in s["runs"]:
            if r["status"] != "ok":
                continue
            stem = r["summary"].get("stem", r["stream"])
            imgs = []
            for suffix, title in [("_qa.png", "Layer 1 QA"), ("_beats.png", "Beats"),
                                  ("_features.png", "Features"), ("_feature_corr.png", "Feature correlation")]:
                p = Path(r["out_dir"]) / f"{stem}{suffix}"
                if (settings.data_root / p).exists():
                    imgs.append({"title": title, "url": f"/files/{p.as_posix()}"})
            images[r["stream"]] = imgs
        return ctx.render(request, "session.html", {
            "s": s, "doc": doc, "images": images, "processing": ctx.processing_state(s["session_id"]),
            "streams": [st["stream"] for st in s["streams"]]})

    @app.post("/sessions/{sid}/process")
    def process(request: Request, sid: str, force: bool = False, user=Depends(sec.require("operator"))):
        if force and not has_role(user, "admin"):
            raise HTTPException(403, "re-processing from scratch needs the admin role")
        s = get_session(sid)
        sid = s["session_id"]
        ctx.start_processing(sid, force)
        ctx.audit(request, "session.process", sid, {"force": force})
        return RedirectResponse(f"/sessions/{sid}", status_code=303)

    @app.get("/files/{path:path}")
    def files(path: str, user=viewer):
        # only recordings, processing outputs and model runs; never the catalog DB, PKI or state files
        root = settings.data_root.resolve()
        p = (root / path).resolve()
        if not any(d in p.parents for d in (root / "raw", root / "processed", root / "models")) or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    # ------------------------------------------------------------------ API
    @app.get("/api/me")
    def api_me(request: Request, user=viewer):
        return {**user, "via": request.state.auth_via}

    @app.get("/api/sessions")
    def api_sessions(subject: str | None = None, protocol: str | None = None, all: bool = False, user=viewer):
        c = ctx.cat()
        try:
            return c.sessions(subject, protocol, include_discarded=all)
        finally:
            c.close()

    @app.get("/api/sessions/{sid}")
    def api_session(sid: str, user=viewer):
        s = get_session(sid)
        s["processing"] = ctx.processing_state(s["session_id"])
        return s

    @app.get("/api/sessions/{sid}/signal")
    def api_signal(sid: str, stream: str, source: str = Query("filtered", pattern="^(raw|filtered)$"),
                   channel: str | None = None, max_points: int = 12000, user=viewer):
        s = get_session(sid)
        doc = session_doc(s)
        st = next((x for x in doc["streams"] if x["stream"] == stream), None)
        if st is None:
            raise HTTPException(404, f"stream {stream} not in session")
        ch = channel or (st.get("channels") or ["ir"])[0]
        if ch not in (st.get("channels") or [ch]):
            raise HTTPException(404, f"no channel {ch}")
        bad = []
        if source == "filtered":
            r = run_for(s, stream)
            if r is None:
                raise HTTPException(409, "not processed yet")
            out = settings.data_root / r["out_dir"]
            stem = r["summary"]["stem"]
            sig = pd.read_csv(out / f"{stem}_signals.csv", usecols=["t", f"{ch}_ac_norm"])
            t0, k = clock_for(s, r)
            t = t0 + k * sig["t"].to_numpy()
            y = sig[f"{ch}_ac_norm"].to_numpy() * 100
            qp = out / f"{stem}_quality.csv"
            if qp.exists():
                q = pd.read_csv(qp)
                if "good" in q:
                    b = q[~q["good"].astype(bool)]
                    for a, e in zip(t0 + k * b["t_start"], t0 + k * b["t_end"]):  # merge overlapping windows
                        if bad and a <= bad[-1][1]:
                            bad[-1][1] = max(bad[-1][1], float(e))
                        else:
                            bad.append([float(a), float(e)])
            unit = "AC/DC (%)"
        else:
            df = pd.read_csv(settings.data_root / s["path"] / st["file"])
            fit = device_clock(st, session_t0_unix(doc, read_events(settings.data_root / s["path"])))
            if fit and "t_dev_us" in df:
                t = fit[0] + fit[1] * df["t_dev_us"].to_numpy() / 1e6
            else:
                t = df["t_s"].to_numpy() if "t_s" in df else np.arange(len(df)) / float(st.get("fs") or 100)
            y = df[ch].to_numpy(dtype=float)
            unit = "ADC counts"
        td, yd = minmax_decimate(t, y, max(100, min(max_points, 50000)))
        return {"stream": stream, "channel": ch, "source": source, "unit": unit,
                "t": _clean(td), "y": _clean(yd), "bad_windows": bad, "n_original": int(t.size)}

    @app.get("/api/sessions/{sid}/hr")
    def api_hr(sid: str, user=viewer):
        s = get_session(sid)
        out = []
        for st in s["streams"]:
            r = run_for(s, st["stream"])
            if r is None:
                continue
            d = settings.data_root / r["out_dir"]
            stem, (t0, k) = r["summary"]["stem"], clock_for(s, r)
            item = {"stream": st["stream"], "role": st["role"], "beats_t": [], "beats_hr": [], "epoch_t": [], "epoch_hr": []}
            bp = d / f"{stem}_beats.csv"
            if bp.exists():
                b = pd.read_csv(bp)
                if len(b):
                    b = b[b["beat_good"].astype(bool)]
                    item["beats_t"], item["beats_hr"] = _clean(t0 + k * b["t_foot"]), _clean(b["hr_bpm"])
            ep = d / f"{stem}_epochs.csv"
            if ep.exists():
                e = pd.read_csv(ep)
                if len(e):
                    e = e[e["valid"].astype(bool)]
                    item["epoch_t"], item["epoch_hr"] = _clean(t0 + k * e["t_center"]), _clean(e["hr_mean_bpm"])
            out.append(item)
        return out

    @app.get("/api/sessions/{sid}/epochs")
    def api_epochs(sid: str, stream: str, user=viewer):
        s = get_session(sid)
        r = run_for(s, stream)
        if r is None:
            raise HTTPException(409, "not processed yet")
        p = settings.data_root / r["out_dir"] / f"{r['summary']['stem']}_epochs.csv"
        if not p.exists():
            return {"columns": [], "rows": []}
        e = pd.read_csv(p)
        t0, k = clock_for(s, r)
        for c in ("t_start", "t_end", "t_center"):
            if c in e:
                e[c] = t0 + k * e[c]
        e = e.replace([np.inf, -np.inf], np.nan).astype(object).where(e.notna(), None)
        return {"columns": list(e.columns), "rows": e.to_dict(orient="records"),
                "features": [c[:-4] for c in e.columns if c.endswith("_med")]}

    from .admin import router as admin_router
    from .models import router as models_router
    from .trials import router as trials_router
    from .worker import router as worker_router
    app.include_router(trials_router)
    app.include_router(admin_router)
    app.include_router(worker_router)
    app.include_router(models_router)
    return app
