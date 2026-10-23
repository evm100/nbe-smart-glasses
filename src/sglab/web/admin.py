"""Login, account, users, nodes and the audit log.

Nodes can be managed here (web) or from a teammate's PC with
firmware/tools/nbe_node.py, which uses the /api/nodes endpoints with an API token.
Firmware updates (OTA): upload a node's image, then "Update" sends it the OTA command;
the node fetches the image from /ota/<token> (see sglab.firmware).
There is deliberately no way to delete recordings, users or nodes from the web:
users and nodes are disabled instead (kept for the audit trail).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import provision
from ..auth import ROLES, AuthError
from ..firmware import MAX_IMAGE, FirmwareError
from ..nodes import NodeError
from . import security as sec

router = APIRouter()
viewer = Depends(sec.require("viewer"))
admin = Depends(sec.require("admin"))
limiter = sec.LoginLimiter()


def C(request: Request):
    return request.app.state.ctx


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


# ------------------------------------------------------------------ login / logout
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    ctx = C(request)
    if not ctx.auth:
        return RedirectResponse("/", status_code=303)
    return ctx.render(request, "login.html", {"next": _safe_next(next), "error": None})


@router.post("/login")
async def login(request: Request):
    ctx = C(request)
    if not ctx.auth:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    username, password = str(form.get("username", "")).strip(), str(form.get("password", ""))
    nxt, ip = _safe_next(form.get("next")), sec.client_ip(request)
    # login is a public path, so the middleware's origin check didn't run: do it here
    if not sec.same_origin(request, ctx.settings.public_url):
        raise HTTPException(403, "cross-site login refused")
    limiter.check(ip, username)
    try:
        token = ctx.auth.login(username, password, ctx.settings.session_hours, ip, request.headers.get("user-agent", ""))
    except AuthError as e:
        limiter.failed(ip, username)
        ctx.auth.audit(username or None, "login.failed", "", {}, ip)
        return ctx.render(request, "login.html", {"next": nxt, "error": str(e)}, status_code=401)
    limiter.ok(username)
    ctx.auth.audit(username, "login", "", {}, ip)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(sec.SESSION_COOKIE, token, httponly=True, samesite="lax", secure=request.app.state.secure_cookie,
                    max_age=int(ctx.settings.session_hours * 3600), path="/")
    return resp


@router.post("/logout")
def logout(request: Request):
    ctx = C(request)
    if ctx.auth:
        ctx.auth.logout(request.cookies.get(sec.SESSION_COOKIE, ""))
    resp = RedirectResponse("/login" if ctx.auth else "/", status_code=303)
    resp.delete_cookie(sec.SESSION_COOKIE, path="/")
    return resp


@router.post("/api/auth/token")
async def api_token(request: Request):
    """Exchange username + password for an API token (nbe_node.py login). JSON:
    {"username", "password", "label"} -> {"token", "user"}. The token is shown once."""
    ctx = C(request)
    if not ctx.auth:
        raise HTTPException(400, "this server runs without user accounts ([auth] enabled = false)")
    body = await request.json()
    username, ip = str(body.get("username", "")).strip(), sec.client_ip(request)
    limiter.check(ip, username)
    try:
        # a short-lived login just to verify the password (and get the same timing protection)
        session = ctx.auth.login(username, str(body.get("password", "")), 0.01, ip, "api-token")
        ctx.auth.logout(session)
    except AuthError as e:
        limiter.failed(ip, username)
        ctx.auth.audit(username or None, "token.failed", "", {}, ip)
        raise HTTPException(401, str(e)) from None
    limiter.ok(username)
    label = str(body.get("label") or "nbe-node")[:80]
    token = ctx.auth.create_token(username, label)
    ctx.auth.audit(username, "token.create", label, {}, ip)
    return {"token": token, "user": ctx.auth.user(username)}


# ------------------------------------------------------------------ own account
@router.get("/account", response_class=HTMLResponse)
def account(request: Request, user=viewer):
    ctx = C(request)
    tokens = ctx.auth.tokens(user["username"]) if ctx.auth else []
    return ctx.render(request, "account.html", {"tokens": tokens, "new_token": None, "msg": None, "error": None})


@router.post("/account/password")
async def account_password(request: Request, user=viewer):
    ctx = C(request)
    if not ctx.auth:
        raise HTTPException(400, "no user accounts on this server")
    f = await request.form()
    tokens = ctx.auth.tokens(user["username"])
    try:
        s = ctx.auth.login(user["username"], str(f.get("current", "")), 0.01)
        ctx.auth.logout(s)
        if f.get("new") != f.get("new2"):
            raise AuthError("the new passwords don't match")
        ctx.auth.set_password(user["username"], str(f.get("new", "")))  # also logs out all sessions
    except AuthError as e:
        return ctx.render(request, "account.html", {"tokens": tokens, "new_token": None, "msg": None, "error": str(e)},
                          status_code=400)
    ctx.audit(request, "user.password", user["username"])
    return RedirectResponse("/login", status_code=303)


@router.post("/account/tokens")
async def account_token_new(request: Request, user=viewer):
    ctx = C(request)
    if not ctx.auth:
        raise HTTPException(400, "no user accounts on this server")
    f = await request.form()
    label = str(f.get("label") or "script")[:80]
    tok = ctx.auth.create_token(user["username"], label)
    ctx.audit(request, "token.create", label)
    return ctx.render(request, "account.html", {"tokens": ctx.auth.tokens(user["username"]), "new_token": tok,
                                                "msg": None, "error": None})


@router.post("/account/tokens/{token_id}/revoke")
def account_token_revoke(request: Request, token_id: int, user=viewer):
    ctx = C(request)
    if ctx.auth:
        ctx.auth.revoke_token(token_id, None if user["role"] == "admin" else user["username"])
        ctx.audit(request, "token.revoke", str(token_id))
    return RedirectResponse(request.headers.get("referer") or "/account", status_code=303)


# ------------------------------------------------------------------ users (admin)
@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, user=admin, error: str | None = None, msg: str | None = None):
    ctx = C(request)
    if not ctx.auth:
        raise HTTPException(400, "user accounts are disabled ([auth] enabled = false in config/server.toml)")
    return ctx.render(request, "admin_users.html", {"users": ctx.auth.users(), "roles": list(ROLES),
                                                    "tokens": ctx.auth.tokens(), "error": error})


@router.post("/admin/users")
async def users_add(request: Request, user=admin):
    ctx = C(request)
    f = await request.form()
    try:
        ctx.auth.add_user(str(f.get("username", "")).strip(), str(f.get("password", "")), str(f.get("role", "operator")),
                          str(f.get("display", "")).strip() or None)
    except AuthError as e:
        return users_page(request, user, error=str(e))
    ctx.audit(request, "user.add", str(f.get("username")), {"role": f.get("role")})
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}")
async def users_edit(request: Request, username: str, user=admin):
    ctx = C(request)
    f = await request.form()
    target = ctx.auth.user(username)
    if not target:
        raise HTTPException(404, f"no user {username}")
    role = str(f.get("role") or target["role"])
    active = f.get("active") == "on"
    losing_admin = target["role"] == "admin" and target["active"] and (role != "admin" or not active)
    if losing_admin and ctx.auth.n_admins() <= 1:
        return users_page(request, user, error="that would leave no active admin")
    try:
        ctx.auth.set_role(username, role=role, active=active)
        if f.get("password"):
            ctx.auth.set_password(username, str(f.get("password")))
    except AuthError as e:
        return users_page(request, user, error=str(e))
    ctx.audit(request, "user.edit", username, {"role": role, "active": active, "password_reset": bool(f.get("password"))})
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/admin/audit", response_class=HTMLResponse)
def audit_page(request: Request, user=admin, limit: int = 300):
    ctx = C(request)
    return ctx.render(request, "admin_audit.html", {"rows": ctx.auth.audit_log(min(limit, 5000)) if ctx.auth else []})


# ------------------------------------------------------------------ nodes (admin)
def _node_rows(ctx) -> list[dict]:
    busy = ctx.manager.busy()
    rows = []
    for n in ctx.registry.nodes():
        st = ctx.hub.node_state(n["node_id"]) if ctx.hub else {}
        rows.append({**n, "live": st, "busy": busy.get(f"mqtt://{n['node_id']}"),
                     "image": ctx.firmware.info(n["node_id"]) if ctx.firmware else None})
    # nodes that talk to the broker but aren't registered/enabled (or a typo'd ID)
    known = {n["node_id"] for n in rows}
    unknown = sorted(ctx.hub.known_ids() - known) if ctx.hub else []
    return rows, unknown


@router.get("/admin/nodes", response_class=HTMLResponse)
def nodes_page(request: Request, user=admin, error: str | None = None):
    ctx = C(request)
    rows, unknown = _node_rows(ctx)
    return ctx.render(request, "admin_nodes.html", {
        "nodes": rows, "unknown": unknown, "pki_ready": ctx.registry.pki.exists(), "prov": ctx.settings.provisioning,
        "ota_url": provision.ota_url(ctx.settings), "error": error}, status_code=400 if error else 200)


@router.post("/admin/nodes")
async def nodes_register(request: Request, user=admin):
    ctx = C(request)
    f = await request.form()
    try:
        n = ctx.registry.register(str(f.get("node_id", "")), str(f.get("name", "")).strip(), str(f.get("site", "")).strip(),
                                  str(f.get("notes", "")).strip(), by=user["username"])
    except (NodeError, FileNotFoundError) as e:
        return nodes_page(request, user, error=str(e))
    ctx.audit(request, "node.register", n["node_id"], {"site": n["site"]})
    return RedirectResponse("/admin/nodes", status_code=303)


@router.post("/admin/nodes/{node_id}")
async def nodes_edit(request: Request, node_id: str, user=admin):
    ctx = C(request)
    f = await request.form()
    try:
        ctx.registry.update(node_id, name=f.get("name"), site=f.get("site"), notes=f.get("notes"),
                            enabled=f.get("enabled") == "on")
    except NodeError as e:
        return nodes_page(request, user, error=str(e))
    ctx.audit(request, "node.edit", node_id.upper(), {"enabled": f.get("enabled") == "on"})
    return RedirectResponse("/admin/nodes", status_code=303)


@router.post("/admin/nodes/{node_id}/rotate")
def nodes_rotate(request: Request, node_id: str, user=admin):
    ctx = C(request)
    try:
        ctx.registry.rotate(node_id)
    except NodeError as e:
        return nodes_page(request, user, error=str(e))
    ctx.audit(request, "node.rotate", node_id.upper())
    return RedirectResponse("/admin/nodes", status_code=303)


@router.post("/admin/nodes/{node_id}/stop")
def nodes_stop(request: Request, node_id: str, user=admin):
    """Tell a node to stop streaming (it shouldn't be, unless something went wrong)."""
    ctx = C(request)
    if ctx.hub is None:
        raise HTTPException(400, "MQTT is not configured")
    if ctx.manager.busy().get(f"mqtt://{node_id.upper()}"):
        raise HTTPException(409, "node is in a running trial – stop the trial instead")
    ctx.hub.publish_cmd(node_id.upper(), "STOP")
    ctx.audit(request, "node.stop", node_id.upper())
    return RedirectResponse("/admin/nodes", status_code=303)


# ------------------------------------------------------------------ firmware updates (OTA)
def _store_image(ctx, node_id: str, data: bytes, by: str) -> dict:
    node_id = node_id.strip().upper()
    if len(data) > MAX_IMAGE:
        raise FirmwareError(f"image too large ({len(data)} bytes)")
    try:
        cert = ctx.registry.credentials(node_id)["client_cert"]
    except NodeError as e:
        raise FirmwareError(str(e)) from None
    return ctx.firmware.put(node_id, data, cert, by)


def _start_ota(ctx, node_id: str) -> dict:
    """Send the node its OTA command. Refuses when it can't work or would disturb a trial."""
    node_id = node_id.strip().upper()
    n = ctx.registry.node(node_id)
    if not n or not n["enabled"]:
        raise HTTPException(404, f"no enabled node {node_id}")
    if ctx.hub is None:
        raise HTTPException(400, "MQTT is not configured")
    img = ctx.firmware.info(node_id)
    if not img:
        raise HTTPException(409, f"no firmware image uploaded for {node_id} yet")
    if ctx.manager.busy().get(f"mqtt://{node_id}"):
        raise HTTPException(409, f"{node_id} is in a running trial - update it afterwards")
    if not ctx.hub.node_state(node_id).get("online"):
        raise HTTPException(409, f"{node_id} is offline")
    if not provision.ota_url(ctx.settings):
        raise HTTPException(400, "OTA needs [web] public_url = https://... in server.toml")
    ctx.hub.publish_cmd(node_id, f"OTA {ctx.firmware.issue_token(node_id)}")
    return img


@router.put("/api/nodes/{node_id}/firmware")
async def api_firmware_upload(request: Request, node_id: str, user=admin):
    """Raw body = build/ppg_stream.bin (nbe_node.py upload / build-firmware.sh)."""
    ctx = C(request)
    try:
        meta = _store_image(ctx, node_id, await request.body(), user["username"])
    except FirmwareError as e:
        raise HTTPException(400, str(e)) from None
    ctx.audit(request, "node.firmware", meta["node_id"], {k: meta[k] for k in ("version", "build", "size")})
    return meta


@router.post("/admin/nodes/{node_id}/firmware")
async def nodes_firmware_upload(request: Request, node_id: str, user=admin):
    ctx = C(request)
    f = await request.form()
    up = f.get("image")
    try:
        if not hasattr(up, "read"):
            raise FirmwareError("choose the image file (build/ppg_stream.bin)")
        meta = _store_image(ctx, node_id, await up.read(MAX_IMAGE + 1), user["username"])
    except FirmwareError as e:
        return nodes_page(request, user, error=f"{node_id.upper()}: {e}")
    ctx.audit(request, "node.firmware", meta["node_id"], {k: meta[k] for k in ("version", "build", "size")})
    return RedirectResponse("/admin/nodes", status_code=303)


@router.post("/api/nodes/{node_id}/ota")
def api_ota(request: Request, node_id: str, user=admin):
    ctx = C(request)
    img = _start_ota(ctx, node_id)
    ctx.audit(request, "node.ota", node_id.upper(), {"version": img["version"], "build": img["build"], "via": "api"})
    return {"node_id": node_id.upper(), "sent": True, "image": img}


@router.post("/admin/nodes/{node_id}/ota")
def nodes_ota(request: Request, node_id: str, user=admin):
    ctx = C(request)
    try:
        img = _start_ota(ctx, node_id)
    except HTTPException as e:
        return nodes_page(request, user, error=str(e.detail))
    ctx.audit(request, "node.ota", node_id.upper(), {"version": img["version"], "build": img["build"]})
    return RedirectResponse("/admin/nodes", status_code=303)


@router.get("/ota/{token}")
def ota_download(request: Request, token: str):
    """The node fetches its image here (public path: the token from the OTA command is the credential)."""
    ctx = C(request)
    hit = ctx.firmware.resolve(token) if ctx.firmware else None
    if not hit:
        raise HTTPException(404, "unknown or expired update link")
    node_id, path = hit
    ctx.audit(request, "node.ota.download", node_id)
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@router.get("/admin/nodes/{node_id}/credentials.zip")
def nodes_zip(request: Request, node_id: str, user=admin):
    ctx = C(request)
    try:
        b = provision.bundle(ctx.settings, ctx.registry, node_id)
    except NodeError as e:
        raise HTTPException(404, str(e)) from None
    ctx.audit(request, "node.credentials", b["node_id"], {"via": "zip"})
    return Response(provision.bundle_zip(b), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{b["node_id"]}_credentials.zip"'})


# ------------------------------------------------------------------ node API (nbe_node.py)
@router.get("/api/provisioning")
def api_provisioning(request: Request, user=admin):
    p = C(request).settings.provisioning
    return {"broker_uri": p["broker_uri"], "broker_ca": "bundle" if p["broker_ca"] in ("", "bundle") else "pem",
            "topic_prefix": p["topic_prefix"]}


@router.get("/api/nodes")
def api_nodes(request: Request, user=admin):
    rows, unknown = _node_rows(C(request))
    return {"nodes": rows, "unregistered_seen": unknown}


@router.post("/api/nodes")
async def api_nodes_register(request: Request, user=admin):
    ctx = C(request)
    b = await request.json()
    try:
        n = ctx.registry.register(str(b.get("node_id", "")), str(b.get("name") or ""), str(b.get("site") or ""),
                                  str(b.get("notes") or ""), by=user["username"])
    except NodeError as e:
        raise HTTPException(409 if "exists" in str(e) else 400, str(e)) from None
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from None
    ctx.audit(request, "node.register", n["node_id"], {"site": n["site"], "via": "api"})
    return n


@router.patch("/api/nodes/{node_id}")
async def api_nodes_edit(request: Request, node_id: str, user=admin):
    ctx = C(request)
    b = await request.json()
    try:
        n = ctx.registry.update(node_id, **{k: b.get(k) for k in ("name", "site", "notes", "enabled")})
    except NodeError as e:
        raise HTTPException(404, str(e)) from None
    ctx.audit(request, "node.edit", n["node_id"], {k: b[k] for k in b if k in ("enabled", "site")})
    return n


@router.post("/api/nodes/{node_id}/rotate")
def api_nodes_rotate(request: Request, node_id: str, user=admin):
    ctx = C(request)
    try:
        n = ctx.registry.rotate(node_id)
    except NodeError as e:
        raise HTTPException(404, str(e)) from None
    ctx.audit(request, "node.rotate", n["node_id"], {"via": "api"})
    return n


@router.get("/api/nodes/{node_id}/credentials")
def api_nodes_credentials(request: Request, node_id: str, user=admin):
    ctx = C(request)
    try:
        b = provision.bundle(ctx.settings, ctx.registry, node_id)
    except NodeError as e:
        raise HTTPException(404, str(e)) from None
    ctx.audit(request, "node.credentials", b["node_id"], {"via": "api"})
    return b
