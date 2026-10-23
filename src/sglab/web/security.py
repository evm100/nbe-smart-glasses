"""Who is asking, and are they allowed to?

Two modes (config/server.toml [auth] enabled):

  enabled   users / roles from the catalog database (see sglab.auth). Browsers get
            an HttpOnly session cookie after /login; scripts and the nbe-node tool
            send ``Authorization: Bearer nbe_...`` (an API token).
  disabled  single-user lab PC mode: everyone who can reach the server is the
            local admin, optionally behind the shared ``--token`` link.

Cookie-authenticated state changes must come from our own pages (Origin /
Referer check, plus SameSite=Lax cookies) so another website can't drive a
logged-in browser. Failed logins are rate-limited per IP and per username.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from ..auth import has_role

SESSION_COOKIE = "nbe_session"
LOCAL_ADMIN = {"username": "local", "display": "local", "role": "admin"}
# /ota/<token>: firmware download by a node, authorised by the one-off token (see sglab.firmware)
PUBLIC_PATHS = ("/login", "/static/", "/healthz", "/api/auth/token", "/ota/")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class LoginLimiter:
    """At most `per_user` failures per username and `per_ip` per address in `window_s`."""

    def __init__(self, per_user: int = 5, per_ip: int = 20, window_s: float = 900):
        self.per_user, self.per_ip, self.window = per_user, per_ip, window_s
        self.fail: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _recent(self, key: str) -> int:
        q, cut = self.fail[key], time.time() - self.window
        while q and q[0] < cut:
            q.popleft()
        return len(q)

    def check(self, ip: str, username: str) -> None:
        with self.lock:
            if self._recent("ip:" + ip) >= self.per_ip or self._recent("u:" + username.lower()) >= self.per_user:
                raise HTTPException(429, "too many failed logins – wait 15 minutes")

    def failed(self, ip: str, username: str) -> None:
        with self.lock:
            now = time.time()
            self.fail["ip:" + ip].append(now)
            self.fail["u:" + username.lower()].append(now)

    def ok(self, username: str) -> None:
        with self.lock:
            self.fail.pop("u:" + username.lower(), None)


def client_ip(request: Request) -> str:
    # behind nginx the app only listens on localhost / the LAN, and nginx sets X-Real-IP
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def same_origin(request: Request, public_url: str | None) -> bool:
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return False
    host = urlsplit(src).netloc.lower()
    allowed = {request.headers.get("host", "").lower()}
    if public_url:
        allowed.add(urlsplit(public_url).netloc.lower())
    return host in allowed


def identify(request: Request, ctx) -> tuple[dict | None, str]:
    """(user, how) where how is 'cookie' | 'bearer' | 'local' | ''."""
    if not ctx.auth:
        return LOCAL_ADMIN, "local"
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        u = ctx.auth.token_user(authz[7:].strip())
        return (u, "bearer") if u else (None, "")
    u = ctx.auth.session_user(request.cookies.get(SESSION_COOKIE))
    return (u, "cookie") if u else (None, "")


def shared_token_ok(request: Request, token: str) -> bool:
    return secrets.compare_digest(request.cookies.get("sglab_token", ""), token) or \
        secrets.compare_digest(request.headers.get("x-sglab-token", ""), token)


def require(role: str):
    """FastAPI dependency: the current user, if they have at least `role`."""

    def dep(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, "login required")
        if not has_role(user, role):
            raise HTTPException(403, f"needs the {role} role")
        return user

    return dep
