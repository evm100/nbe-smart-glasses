"""Users, roles, login sessions, API tokens and the audit log.

Stored in the catalog SQLite file (tables users / login_sessions / api_tokens / audit).

Roles (each includes the ones below it):
  admin     manage users and nodes, issue node credentials, force re-processing
  operator  run trials (claim devices, start/stop, log readings)
  viewer    browse sessions, plots and QA figures
  worker    none of the above: only the processing-worker API (/api/worker/*, sglab worker)

Passwords: scrypt (stdlib hashlib) with a random salt.  Session cookies and API
tokens are random 256-bit strings; only their SHA-256 is stored, so a leaked
database does not leak usable credentials.  There is deliberately no API to
delete recordings.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROLES = {"worker": 0, "viewer": 1, "operator": 2, "admin": 3}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    display     TEXT,
    role        TEXT NOT NULL,
    pw_hash     TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS login_sessions (   -- 'sessions' = recordings (catalog)
    token_hash  TEXT PRIMARY KEY,
    username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at  TEXT,
    expires_at  TEXT,
    ip          TEXT,
    user_agent  TEXT
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash  TEXT UNIQUE NOT NULL,
    username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    label       TEXT,
    created_at  TEXT,
    last_used   TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    username    TEXT,
    action      TEXT,
    target      TEXT,
    detail      TEXT,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- password hashing --------------------------------------------------------
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b(salt)}${b(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


class AuthError(ValueError):
    pass


def _locked(fn):
    """Serialise access to the shared connection, and never leave a transaction open: a failed
    write (e.g. duplicate username) would otherwise keep the database write-locked for every
    other connection (catalog, node registry) until this process exits."""
    @wraps(fn)
    def w(self, *a, **k):
        with self.lock:
            try:
                return fn(self, *a, **k)
            except BaseException:
                self.con.rollback()
                raise
    return w


class AuthStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        self.lock = threading.RLock()  # one connection shared by the web worker threads

    def close(self):
        self.con.close()

    def _q(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    # ---- users ---------------------------------------------------------------
    @_locked
    def add_user(self, username: str, password: str, role: str = "operator", display: str | None = None) -> None:
        if role not in ROLES:
            raise AuthError(f"role must be one of {list(ROLES)}")
        if not username or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
            raise AuthError("username: letters, digits, . _ - only")
        if len(password) < 10:
            raise AuthError("password must be at least 10 characters")
        try:
            self.con.execute("INSERT INTO users VALUES (?,?,?,?,1,?)",
                             (username, display or username, role, hash_password(password), _iso(_now())))
        except sqlite3.IntegrityError:
            raise AuthError(f"user {username!r} already exists") from None
        self.con.commit()

    @_locked
    def set_password(self, username: str, password: str) -> None:
        if len(password) < 10:
            raise AuthError("password must be at least 10 characters")
        n = self.con.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_password(password), username)).rowcount
        if not n:
            raise AuthError(f"no user {username!r}")
        self.con.execute("DELETE FROM login_sessions WHERE username=?", (username,))  # log out everywhere
        self.con.commit()

    @_locked
    def set_role(self, username: str, role: str | None = None, active: bool | None = None) -> None:
        if role is not None:
            if role not in ROLES:
                raise AuthError(f"role must be one of {list(ROLES)}")
            self.con.execute("UPDATE users SET role=? WHERE username=?", (role, username))
        if active is not None:
            self.con.execute("UPDATE users SET active=? WHERE username=?", (1 if active else 0, username))
            if not active:
                self.con.execute("DELETE FROM login_sessions WHERE username=?", (username,))
                self.con.execute("UPDATE api_tokens SET revoked=1 WHERE username=?", (username,))
        self.con.commit()

    @_locked
    def users(self) -> list[dict]:
        return self._q("SELECT username, display, role, active, created_at FROM users ORDER BY username")

    @_locked
    def user(self, username: str) -> dict | None:
        r = self._q("SELECT username, display, role, active FROM users WHERE username=?", (username,))
        return r[0] if r else None

    @_locked
    def n_admins(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]

    # ---- login sessions -------------------------------------------------------
    @_locked
    def login(self, username: str, password: str, hours: float, ip: str = "", ua: str = "") -> str:
        r = self._q("SELECT * FROM users WHERE username=?", (username,))
        # always run a hash so timing doesn't reveal whether the user exists
        ok = verify_password(password, r[0]["pw_hash"] if r else hash_password("x" * 12))
        if not r or not ok or not r[0]["active"]:
            raise AuthError("wrong username or password")
        token = secrets.token_urlsafe(32)
        now = _now()
        self.con.execute("INSERT INTO login_sessions VALUES (?,?,?,?,?,?)",
                         (_sha(token), username, _iso(now), _iso(now + timedelta(hours=hours)), ip, ua[:200]))
        self.con.execute("DELETE FROM login_sessions WHERE expires_at < ?", (_iso(now),))
        self.con.commit()
        return token

    @_locked
    def logout(self, token: str) -> None:
        self.con.execute("DELETE FROM login_sessions WHERE token_hash=?", (_sha(token),))
        self.con.commit()

    @_locked
    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        r = self._q("""SELECT u.username, u.display, u.role FROM login_sessions s JOIN users u ON u.username = s.username
                       WHERE s.token_hash=? AND s.expires_at > ? AND u.active=1""", (_sha(token), _iso(_now())))
        return r[0] if r else None

    # ---- API tokens (for the nbe-node tool and scripts) --------------------------
    @_locked
    def create_token(self, username: str, label: str) -> str:
        token = "nbe_" + secrets.token_urlsafe(32)
        self.con.execute("INSERT INTO api_tokens (token_hash, username, label, created_at) VALUES (?,?,?,?)",
                         (_sha(token), username, label, _iso(_now())))
        self.con.commit()
        return token

    @_locked
    def token_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        r = self._q("""SELECT u.username, u.display, u.role, t.id AS token_id FROM api_tokens t
                       JOIN users u ON u.username = t.username
                       WHERE t.token_hash=? AND t.revoked=0 AND u.active=1""", (_sha(token),))
        if not r:
            return None
        self.con.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (_iso(_now()), r[0]["token_id"]))
        self.con.commit()
        return r[0]

    @_locked
    def tokens(self, username: str | None = None) -> list[dict]:
        sql = "SELECT id, username, label, created_at, last_used, revoked FROM api_tokens"
        return self._q(sql + (" WHERE username=?" if username else "") + " ORDER BY id DESC",
                       (username,) if username else ())

    @_locked
    def revoke_token(self, token_id: int, username: str | None = None) -> None:
        if username:
            self.con.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND username=?", (token_id, username))
        else:
            self.con.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))
        self.con.commit()

    # ---- audit -------------------------------------------------------------------
    @_locked
    def audit(self, username: str | None, action: str, target: str = "", detail: dict | None = None, ip: str = ""):
        self.con.execute("INSERT INTO audit (ts, username, action, target, detail, ip) VALUES (?,?,?,?,?,?)",
                         (_iso(_now()), username, action, target, json.dumps(detail or {}, default=str), ip))
        self.con.commit()

    @_locked
    def audit_log(self, limit: int = 200) -> list[dict]:
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


def has_role(user: dict | None, role: str) -> bool:
    return bool(user) and ROLES.get(user["role"], 0) >= ROLES[role]
