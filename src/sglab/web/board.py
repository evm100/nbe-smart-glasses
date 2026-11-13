"""Lab board: pages and JSON API (storage: sglab.board.BoardStore, in the catalog database).

Pages
    GET /board            the board: composer, filters (?kind= &state=open|done|all &q= &session= &device= &page=)
    GET /board/{id}       one post with its whole thread (also reachable as /board#post-{id})

JSON API (errors are {"error": "..."} like the rest of the server)
    GET    /api/board                 list; filters kind, state (open|done), session, device, q, limit, offset
    GET    /api/board/summary         counts + the 5 most recent posts (nav badge, home page)
    GET    /api/board/{id}            one post
    POST   /api/board                 {kind, title, body, session_id?, device?, tags?, author?}  -> 201, the post
    PATCH  /api/board/{id}            any of {kind, title, body, session_id, device, tags, state, pinned} -> the post
    DELETE /api/board/{id}
    POST   /api/board/{id}/replies    {body, author?}  -> 201, the post (with the new reply)
    DELETE /api/board/replies/{rid}   -> the post

Stable shapes (used by the session page, the home page and the nav badge):

GET /api/board?session=<sid>  ->
    {"posts": [POST, ...],          # pinned first, then newest activity first
     "total": 12,                   # how many match the filters (posts is one page of them)
     "limit": 50, "offset": 0,
     "counts": COUNTS,
     "can_post": true}              # may the caller write posts / replies
POST =
    {"id": 7, "url": "/board/7",
     "kind": "question",            # announcement | note | question | request | issue | status
     "kind_label": "Question",
     "title": "...", "body": "... plain text ...",
     "author": "olga",              # username (accounts) or the typed name (single-user mode)
     "author_name": "Olga",         # name to show
     "created_at": "2026-11-13T18:04:05.123+00:00", "updated_at": "...", "edited": false,
     "activity_at": "...",          # later of updated_at and the last reply
     "session_id": "S001_rest_..." | null, "device": "GLS03" | null, "tags": ["firmware"],
     "pinned": false,
     "state": "open",               # open | done
     "state_label": "needs an answer" | null,   # null for announcements and notes (no open/done meaning)
     "closed_by": null, "closed_at": null,
     "n_replies": 1,
     "replies": [{"id": 3, "post_id": 7, "created_at": "...", "author": "ada", "author_name": "Ada",
                  "body": "...", "can_delete": false}],
     "can": {"edit": false, "delete": false, "pin": false, "state": true, "reply": true}}

GET /api/board/summary  ->
    {"counts": COUNTS, "recent": [POST without "replies", ...up to 5, newest activity first]}
COUNTS = {"question": 2, "request": 0, "issue": 1, "open": 3}   # still open; "open" is their sum

Who may do what
    read                     viewer and above
    post, reply              POST_ROLE and above (operator: viewer accounts only browse, see sglab.auth)
    edit / delete own text   the author (while they may still post)
    mark done / reopen       operator and above
    pin, edit / delete any   admin
Single-user mode (no accounts): everybody is the local admin and types a name with each post.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markupsafe import Markup, escape

from ..auth import has_role
from ..board import KINDS, STATEFUL, BoardError, BoardStore
from .security import require

router = APIRouter()
viewer = Depends(require("viewer"))

POST_ROLE = "operator"  # lowest role that may write posts and replies ("viewer" would open it to everyone)
PAGE = 30

# label, plural (filter chips), one-line hint, title placeholder, text placeholder, (open label, done label, button)
KIND_INFO = {
    "note": ("Note", "Notes", "Something you noticed or want written down, like a lab-notebook entry.",
             "Left temple sensor reads low after cleaning", "What happened, what you tried, what to remember…", None),
    "question": ("Question", "Questions", "You need an answer from someone on the team.",
                 "Does GLS03 need to be re-flashed before Friday's session?",
                 "Add anything that helps someone answer…", ("needs an answer", "answered", "Mark answered")),
    "request": ("Request", "Requests", "You need somebody to do something.",
                "Please charge both glasses before Thursday 9 am", "What is needed, by when, and for whom…",
                ("to do", "done", "Mark done")),
    "issue": ("Problem", "Problems", "Something is broken, looks wrong or worries you.",
              "Cuff monitor shows an error after the second reading", "What you saw, on which device, and when…",
              ("not fixed yet", "fixed", "Mark fixed")),
    "status": ("Update", "Updates", "What you are working on right now, so nobody has to ask.",
               "Re-recording subject S012 this week", "Where things stand and what comes next…",
               ("in progress", "finished", "Mark finished")),
    "announcement": ("Announcement", "Announcements", "Something everyone in the lab should read.",
                     "Lab closed Monday – no recordings", "The details everyone needs…", None),
}
KIND_ORDER = ("note", "question", "request", "issue", "status", "announcement")  # composer / chip order
assert set(KIND_INFO) == set(KINDS)

_URL = re.compile(r"https?://[^\s<>\"']+")


def C(request: Request):
    return request.app.state.ctx


def store(ctx) -> BoardStore:
    """The BoardStore, created on first use and kept on the shared Ctx."""
    b = getattr(ctx, "board", None)
    if b is None:
        b = ctx.board = BoardStore(ctx.settings.db_path)
    return b


def rich(text: str) -> Markup:
    """Plain text -> safe HTML: everything escaped, http(s) addresses turned into links.
    Line breaks are kept by CSS (white-space: pre-wrap)."""
    out, pos = [], 0
    for m in _URL.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?)]}")
        out.append(escape(text[pos:m.start()]))
        out.append(Markup('<a href="{0}" target="_blank" rel="noopener noreferrer">{0}</a>').format(url))
        pos = m.start() + len(url)
    out.append(escape((text or "")[pos:]))
    return Markup("").join(out)


def ago(iso: str | None) -> str:
    """'just now', '5 min ago', '2 h ago', '3 days ago', then the date."""
    try:
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    if s < 14 * 86400:
        d = int(s // 86400)
        return "yesterday" if d == 1 else f"{d} days ago"
    return iso[:10]


# ------------------------------------------------------------------ permissions
def _is_author(ctx, user: dict, author: str) -> bool:
    # without accounts everyone is the same 'local' admin; names are typed, not proof of identity
    return bool(ctx.auth) and user["username"] == author


def can_post(user: dict) -> bool:
    return has_role(user, POST_ROLE)


def _can(ctx, user: dict, p: dict) -> dict:
    admin, own = has_role(user, "admin"), _is_author(ctx, user, p["author"]) and can_post(user)
    return {"edit": admin or own, "delete": admin or own, "pin": admin,
            "state": p["kind"] in STATEFUL and has_role(user, "operator"), "reply": can_post(user)}


def decorate(ctx, user: dict, p: dict) -> dict:
    """Add what pages / API clients need: url, labels and what this user may do."""
    info = KIND_INFO[p["kind"]] if p["kind"] in KIND_INFO else KIND_INFO["note"]
    p["url"] = f"/board/{p['id']}"
    p["kind_label"] = info[0]
    p["state_label"] = info[5][0 if p["state"] == "open" else 1] if info[5] else None
    p["can"] = _can(ctx, user, p)
    admin = has_role(user, "admin")
    for r in p.get("replies", []):
        r["can_delete"] = admin or (_is_author(ctx, user, r["author"]) and can_post(user))
    return p


def _author(ctx, user: dict, data: dict) -> tuple[str, str]:
    """(author, name to show): the account, or in single-user mode the name typed in the form."""
    if ctx.auth:
        return user["username"], user.get("display") or user["username"]
    name = str(data.get("author") or "").strip()
    if not name:
        raise HTTPException(400, "Please type your name or initials first.")
    return name, name


def _post_or_404(ctx, pid: int) -> dict:
    p = store(ctx).get(pid)
    if p is None:
        raise HTTPException(404, "This post does not exist (it may have been deleted).")
    return p


def _session_id(ctx, sid) -> str | None:
    """Check a typed session id against the catalog (unique part of the id is enough)."""
    sid = str(sid or "").strip()
    if not sid:
        return None
    c = ctx.cat()
    try:
        s = c.session(sid)
    finally:
        c.close()
    if not s:
        raise HTTPException(400, f"There is no recording called “{sid}”. Copy the name from the Sessions page, "
                                 "or leave the field empty.")
    return s["session_id"]


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON object") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object")
    return data


# ------------------------------------------------------------------ pages
def _state_arg(state: str | None) -> str | None:
    return state if state in ("open", "done") else None


def _page_ctx(ctx, user: dict) -> dict:
    c = ctx.cat()
    try:
        sessions = [r["session_id"] for r in c.sessions()[:40]]
    finally:
        c.close()
    try:
        devices = sorted({d["label"] for d in ctx.directory.list() if d.get("label")})
    except Exception:  # the board must open even when the device list can't be built
        devices = []
    return {"kinds": [{"key": k, "label": KIND_INFO[k][0], "plural": KIND_INFO[k][1], "hint": KIND_INFO[k][2],
                       "ph_title": KIND_INFO[k][3], "ph_body": KIND_INFO[k][4],
                       "done_button": KIND_INFO[k][5][2] if KIND_INFO[k][5] else None} for k in KIND_ORDER],
            "can_post": can_post(user), "post_role": POST_ROLE, "ask_name": not ctx.auth,
            "session_options": sessions, "device_options": devices, "rich": rich, "ago": ago}


@router.get("/board", response_class=HTMLResponse)
def board_page(request: Request, kind: str | None = None, state: str = "open", q: str = "",
               session: str | None = None, device: str | None = None, page: int = 1, user=viewer):
    ctx, b = C(request), store(C(request))
    kind = kind if kind in KINDS else None
    st, page = _state_arg(state), max(1, page)
    f = dict(kind=kind, state=st, session_id=session or None, device=device or None, q=q.strip() or None)
    posts = [decorate(ctx, user, p) for p in b.list(**f, limit=PAGE, offset=(page - 1) * PAGE)]
    total = b.count(**f)
    # chip counts follow the other filters, so a chip never promises posts the click won't show
    chip = {k: b.count(**{**f, "kind": k}) for k in KINDS}
    chip["all"] = sum(chip.values())
    cur = {"kind": kind or "", "state": st or "all", "q": q.strip(), "session": session or "", "device": device or "",
           "page": 1}

    def link(**change) -> str:
        """/board with the current filters, some of them changed (changing a filter goes back to page 1)."""
        a = {**cur, **change}
        qs = urlencode({k: v for k, v in a.items() if v not in ("", None) and (k, v) not in (("state", "open"), ("page", 1))})
        return "/board" + (f"?{qs}" if qs else "")

    return ctx.render(request, "board.html", {
        **_page_ctx(ctx, user), "single": None, "link": link,
        "pinned": [p for p in posts if p["pinned"]], "posts": [p for p in posts if not p["pinned"]],
        "total": total, "page": page, "pages": max(1, -(-total // PAGE)), "chip": chip, "counts": b.counts(),
        "f_kind": kind or "", "f_state": st or "all", "f_q": q.strip(), "f_session": session or "",
        "f_device": device or "", "empty_board": not b.kind_counts()["all"]})


@router.get("/board/{pid}", response_class=HTMLResponse)
def board_post_page(request: Request, pid: int, user=viewer):
    ctx = C(request)
    p = decorate(ctx, user, _post_or_404(ctx, pid))
    return ctx.render(request, "board.html", {**_page_ctx(ctx, user), "single": p, "pinned": [], "posts": [p],
                                              "counts": store(ctx).counts()})


# ------------------------------------------------------------------ API
@router.get("/api/board/summary")
def api_summary(request: Request, user=viewer):
    ctx, b = C(request), store(C(request))
    return {"counts": b.counts(), "recent": [decorate(ctx, user, p) for p in b.recent(5)]}


@router.get("/api/board")
def api_list(request: Request, kind: str | None = None, state: str | None = None, session: str | None = None,
             device: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0, user=viewer):
    ctx, b = C(request), store(C(request))
    if kind and kind not in KINDS:
        raise HTTPException(400, f"kind must be one of: {', '.join(KINDS)}")
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    f = dict(kind=kind or None, state=_state_arg(state), session_id=session or None, device=device or None,
             q=(q or "").strip() or None)
    return {"posts": [decorate(ctx, user, p) for p in b.list(**f, limit=limit, offset=offset)],
            "total": b.count(**f), "limit": limit, "offset": offset, "counts": b.counts(),
            "can_post": can_post(user)}


@router.get("/api/board/{pid}")
def api_get(request: Request, pid: int, user=viewer):
    ctx = C(request)
    return decorate(ctx, user, _post_or_404(ctx, pid))


@router.post("/api/board")
async def api_create(request: Request, user=Depends(require(POST_ROLE))):
    ctx, data = C(request), await _json(request)
    author, name = _author(ctx, user, data)
    try:
        p = store(ctx).create(author, str(data.get("kind") or "note"), data.get("title"), data.get("body"),
                              author_name=name, session_id=_session_id(ctx, data.get("session_id")),
                              device=data.get("device"), tags=data.get("tags"))
    except BoardError as e:
        raise HTTPException(400, str(e)) from None
    ctx.audit(request, "board.post", str(p["id"]), {"kind": p["kind"], "title": p["title"]})
    return JSONResponse(decorate(ctx, user, p), status_code=201)


@router.patch("/api/board/{pid}")
async def api_patch(request: Request, pid: int, user=viewer):
    """Edit the text (author / admin), mark done or reopen (operator), pin (admin) – any mix in one call;
    nothing is changed unless every part is allowed."""
    ctx, data, b = C(request), await _json(request), store(C(request))
    p = _post_or_404(ctx, pid)
    can = _can(ctx, user, p)
    text = {k: data[k] for k in ("kind", "title", "body", "session_id", "device", "tags") if k in data}
    if text and not can["edit"]:
        raise HTTPException(403, "Only the person who wrote this post (or an admin) can change it.")
    if "pinned" in data and not can["pin"]:
        raise HTTPException(403, "Only an admin can pin posts.")
    if "state" in data and not has_role(user, "operator"):
        raise HTTPException(403, "Marking posts done needs the operator role.")
    if "session_id" in text:
        text["session_id"] = _session_id(ctx, text["session_id"]) if text["session_id"] != p["session_id"] else p["session_id"]
    try:
        if text:
            p = b.edit(pid, **text)
            ctx.audit(request, "board.edit", str(pid), {"fields": sorted(text)})
        if "state" in data:
            if data["state"] != p["state"]:
                p = b.set_state(pid, str(data["state"]), by=user.get("display") or user["username"]
                                if ctx.auth else str(data.get("author") or "").strip())
                ctx.audit(request, "board.state", str(pid), {"state": p["state"]})
        if "pinned" in data:
            p = b.set_pinned(pid, bool(data["pinned"]))
            ctx.audit(request, "board.pin", str(pid), {"pinned": p["pinned"]})
    except BoardError as e:
        raise HTTPException(400, str(e)) from None
    except KeyError:
        raise HTTPException(404, "This post does not exist (it may have been deleted).") from None
    return decorate(ctx, user, p)


@router.delete("/api/board/{pid}")
def api_delete(request: Request, pid: int, user=viewer):
    ctx = C(request)
    p = _post_or_404(ctx, pid)
    if not _can(ctx, user, p)["delete"]:
        raise HTTPException(403, "Only the person who wrote this post (or an admin) can delete it.")
    store(ctx).delete(pid)
    ctx.audit(request, "board.delete", str(pid), {"kind": p["kind"], "title": p["title"], "author": p["author"],
                                                  "replies": p["n_replies"]})
    return {"ok": True}


@router.post("/api/board/{pid}/replies")
async def api_reply(request: Request, pid: int, user=Depends(require(POST_ROLE))):
    ctx, data = C(request), await _json(request)
    _post_or_404(ctx, pid)
    author, name = _author(ctx, user, data)
    try:
        r = store(ctx).add_reply(pid, author, data.get("body"), author_name=name)
    except BoardError as e:
        raise HTTPException(400, str(e)) from None
    except KeyError:
        raise HTTPException(404, "This post does not exist (it may have been deleted).") from None
    ctx.audit(request, "board.reply", str(pid), {"reply": r["id"]})
    return JSONResponse(decorate(ctx, user, _post_or_404(ctx, pid)), status_code=201)


@router.delete("/api/board/replies/{rid}")
def api_delete_reply(request: Request, rid: int, user=viewer):
    ctx, b = C(request), store(C(request))
    r = b.reply(rid)
    if r is None:
        raise HTTPException(404, "This reply does not exist (it may have been deleted).")
    if not (has_role(user, "admin") or (_is_author(ctx, user, r["author"]) and can_post(user))):
        raise HTTPException(403, "Only the person who wrote this reply (or an admin) can delete it.")
    b.delete_reply(rid)
    ctx.audit(request, "board.reply_delete", str(r["post_id"]), {"reply": rid, "author": r["author"]})
    return decorate(ctx, user, _post_or_404(ctx, r["post_id"]))
