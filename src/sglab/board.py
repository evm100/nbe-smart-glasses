"""Lab board: the team's shared notebook / bulletin (notes, questions, requests, issues, ...).

Stored in the catalog SQLite file (tables board_posts / board_replies), next to the
users and the session catalog, so one backup covers everything.

A post has a kind:
  announcement  something everyone should read
  note          an observation or a lab-notebook entry
  doc           a longer write-up (Markdown): findings, a conversation summary, a proposal
  question      needs an answer            (open -> done = answered)
  request       somebody please do this    (open -> done = done)
  issue         something is broken/worrying (open -> done = fixed)
  status        work in progress           (open -> done = finished)
Announcements, notes and write-ups have no open/done meaning; their state stays 'open'.
Bodies and replies are Markdown (plain text is valid Markdown); rendering is the web layer's job.

A post can point at a recording (session_id) and/or a device (node id / device name)
and carry free tags. Replies form one flat thread under the post. Anybody who can read
can react to a post with one click (a fixed set of REACTIONS, one of each per person).

Site feedback (table board_feedback) is separate from posts: a mood (1 hate .. 5 love) and/or a
few words about one page of the web UI, sent from the "Feedback" button in the menu. It is
anonymous unless the sender ticks "add my name" (then `who` is filled in).

Ordering everywhere: pinned first, then newest activity first, where
activity = the later of "post written / edited" and "last reply".

Who may do what is decided by the web layer (sglab.web.board); this module only stores.
A new connection is opened per call (the web server is multi-threaded, calls are short).
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("announcement", "note", "doc", "question", "request", "issue", "status")
STATEFUL = ("question", "request", "issue", "status")  # kinds where open/done means something
COUNTED = ("question", "request", "issue")             # open ones of these = "needs attention"
STATES = ("open", "done")
MAX_TITLE, MAX_BODY, MAX_REPLY, MAX_NAME, MAX_TAGS = 200, 200_000, 20_000, 80, 8
# key -> (emoji, what it means); the order is the order of the buttons
REACTIONS = {"up": ("👍", "agree / useful"), "down": ("👎", "disagree / not useful"), "love": ("❤️", "love it"),
             "idea": ("💡", "learned something"), "seen": ("👀", "read it"), "confused": ("😕", "confusing")}
MOODS = {1: ("😠", "hate it"), 2: ("😕", "confusing"), 3: ("😐", "meh"), 4: ("🙂", "good"), 5: ("😍", "love it")}
MAX_FEEDBACK = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,          -- last edit of the text (not pin / state changes)
    author      TEXT NOT NULL,          -- username (accounts) or the typed name (single-user mode)
    author_name TEXT,                   -- name to show
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    session_id  TEXT,
    device      TEXT,
    tags        TEXT NOT NULL DEFAULT '',   -- comma separated, lower case
    pinned      INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'open',
    closed_by   TEXT,
    closed_at   TEXT
);
CREATE TABLE IF NOT EXISTS board_replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES board_posts(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    author      TEXT NOT NULL,
    author_name TEXT,
    body        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_reactions (
    post_id     INTEGER NOT NULL REFERENCES board_posts(id) ON DELETE CASCADE,
    who         TEXT NOT NULL,          -- username, or a name / browser id in single-user mode
    who_name    TEXT,
    reaction    TEXT NOT NULL,          -- a key of REACTIONS
    created_at  TEXT NOT NULL,
    PRIMARY KEY (post_id, who, reaction)
);
CREATE TABLE IF NOT EXISTS board_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    token       TEXT NOT NULL,          -- lets the (anonymous) sender add words right after the click
    page        TEXT NOT NULL,          -- path, e.g. /sessions/S001_rest_1
    area        TEXT NOT NULL,          -- page grouped, e.g. /sessions/…
    page_title  TEXT NOT NULL DEFAULT '',
    part        TEXT NOT NULL DEFAULT '',   -- which part of the page they pointed at (optional)
    mood        INTEGER,                -- 1..5 (MOODS) or NULL
    text        TEXT NOT NULL DEFAULT '',
    who         TEXT,                   -- NULL = anonymous
    who_name    TEXT
);
CREATE INDEX IF NOT EXISTS idx_board_replies_post ON board_replies(post_id);
CREATE INDEX IF NOT EXISTS idx_board_posts_session ON board_posts(session_id);
"""

# activity = max(post text changed, last reply); ISO UTC strings of equal format sort as text
_ACTIVITY = ("MAX(p.updated_at, COALESCE((SELECT MAX(r.created_at) FROM board_replies r "
             "WHERE r.post_id = p.id), ''))")
_SELECT = f"""SELECT p.*, {_ACTIVITY} AS activity_at,
    (SELECT COUNT(*) FROM board_replies r WHERE r.post_id = p.id) AS n_replies
    FROM board_posts p"""


class BoardError(ValueError):
    """Bad input (empty text, unknown kind, ...); the message is shown to the user."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(v, limit: int, what: str) -> str:
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    if len(s) > limit:
        raise BoardError(f"{what} is too long ({len(s)} characters, the limit is {limit})")
    return s


def clean_tags(tags) -> list[str]:
    """'Firmware, #GLS03  bp' or a list -> ['firmware', 'gls03', 'bp'] (unique, max MAX_TAGS)."""
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(t) for t in tags)
    out: list[str] = []
    for t in re.split(r"[,\s]+", str(tags or "")):
        t = re.sub(r"[^\w.\-]", "", t.lower())[:30]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class BoardStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)

    @contextmanager
    def _con(self):
        """One short-lived connection per call: commit on success, roll back on error, always close
        (an open failed transaction would keep the shared database write-locked)."""
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _post(row) -> dict:
        d = dict(row)
        d["pinned"] = bool(d["pinned"])
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        d["author_name"] = d.get("author_name") or d["author"]
        d["edited"] = d["updated_at"] != d["created_at"]
        return d

    @staticmethod
    def _reply(row) -> dict:
        d = dict(row)
        d["author_name"] = d.get("author_name") or d["author"]
        return d

    def _replies(self, con, ids: list[int]) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {i: [] for i in ids}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in con.execute(f"SELECT * FROM board_replies WHERE post_id IN ({marks}) ORDER BY id", ids):
                out[r["post_id"]].append(self._reply(r))
        return out

    # ---- posts -----------------------------------------------------------------
    @staticmethod
    def _fields(kind, title, body) -> tuple[str, str, str]:
        if kind not in KINDS:
            raise BoardError(f"kind must be one of: {', '.join(KINDS)}")
        title, body = _text(title, MAX_TITLE, "the title"), _text(body, MAX_BODY, "the text")
        if not title and body:  # quick note without a title: use its first line
            first = body.split("\n", 1)[0].strip()
            title = first if len(first) <= 80 else first[:77].rstrip() + "..."
        if not title:
            raise BoardError("write a title or some text first")
        return kind, title, body

    def create(self, author: str, kind: str, title: str, body: str = "", *, author_name: str | None = None,
               session_id: str | None = None, device: str | None = None, tags=None, pinned: bool = False) -> dict:
        author = _text(author, MAX_NAME, "the name")
        if not author:
            raise BoardError("who is writing? (name missing)")
        kind, title, body = self._fields(kind, title, body)
        now = _now()
        with self._con() as con:
            pid = con.execute(
                "INSERT INTO board_posts (created_at, updated_at, author, author_name, kind, title, body,"
                " session_id, device, tags, pinned) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, author, _text(author_name, MAX_NAME, "the name") or author, kind, title, body,
                 _text(session_id, 120, "the session") or None, _text(device, 120, "the device") or None,
                 ",".join(clean_tags(tags)), 1 if pinned else 0)).lastrowid
        return self.get(pid)

    def edit(self, post_id: int, **changes) -> dict:
        """Change any of kind / title / body / session_id / device / tags (only the ones given)."""
        cur = self.get(post_id)
        if cur is None:
            raise KeyError(post_id)
        unknown = set(changes) - {"kind", "title", "body", "session_id", "device", "tags"}
        if unknown:
            raise BoardError(f"cannot change: {', '.join(sorted(unknown))}")
        kind, title, body = self._fields(changes.get("kind", cur["kind"]), changes.get("title", cur["title"]),
                                         changes.get("body", cur["body"]))
        sid = _text(changes["session_id"], 120, "the session") or None if "session_id" in changes else cur["session_id"]
        dev = _text(changes["device"], 120, "the device") or None if "device" in changes else cur["device"]
        tags = clean_tags(changes["tags"]) if "tags" in changes else cur["tags"]
        with self._con() as con:
            con.execute("UPDATE board_posts SET kind=?, title=?, body=?, session_id=?, device=?, tags=?, updated_at=?"
                        " WHERE id=?", (kind, title, body, sid, dev, ",".join(tags), _now(), post_id))
            if kind not in STATEFUL:  # a question turned into a note has nothing left to be "done"
                con.execute("UPDATE board_posts SET state='open', closed_by=NULL, closed_at=NULL WHERE id=?", (post_id,))
        return self.get(post_id)

    def delete(self, post_id: int) -> bool:
        with self._con() as con:
            con.execute("DELETE FROM board_replies WHERE post_id=?", (post_id,))
            con.execute("DELETE FROM board_reactions WHERE post_id=?", (post_id,))
            return con.execute("DELETE FROM board_posts WHERE id=?", (post_id,)).rowcount > 0

    def set_pinned(self, post_id: int, pinned: bool) -> dict:
        with self._con() as con:
            n = con.execute("UPDATE board_posts SET pinned=? WHERE id=?", (1 if pinned else 0, post_id)).rowcount
        if not n:
            raise KeyError(post_id)
        return self.get(post_id)

    def set_state(self, post_id: int, state: str, by: str = "") -> dict:
        if state not in STATES:
            raise BoardError("state must be 'open' or 'done'")
        cur = self.get(post_id)
        if cur is None:
            raise KeyError(post_id)
        if cur["kind"] not in STATEFUL:
            raise BoardError(f"{cur['kind']}s cannot be marked done – only questions, requests, issues and status updates")
        done = state == "done"
        with self._con() as con:
            con.execute("UPDATE board_posts SET state=?, closed_by=?, closed_at=? WHERE id=?",
                        (state, (by or None) if done else None, _now() if done else None, post_id))
        return self.get(post_id)

    # ---- replies ---------------------------------------------------------------
    def add_reply(self, post_id: int, author: str, body: str, author_name: str | None = None) -> dict:
        author, body = _text(author, MAX_NAME, "the name"), _text(body, MAX_REPLY, "the reply")
        if not author:
            raise BoardError("who is writing? (name missing)")
        if not body:
            raise BoardError("the reply is empty")
        with self._con() as con:
            if not con.execute("SELECT 1 FROM board_posts WHERE id=?", (post_id,)).fetchone():
                raise KeyError(post_id)
            rid = con.execute("INSERT INTO board_replies (post_id, created_at, author, author_name, body)"
                              " VALUES (?,?,?,?,?)", (post_id, _now(), author,
                                                      _text(author_name, MAX_NAME, "the name") or author, body)).lastrowid
        return self.reply(rid)

    def reply(self, reply_id: int) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM board_replies WHERE id=?", (reply_id,)).fetchone()
        return self._reply(r) if r else None

    def delete_reply(self, reply_id: int) -> bool:
        with self._con() as con:
            return con.execute("DELETE FROM board_replies WHERE id=?", (reply_id,)).rowcount > 0

    # ---- reading ---------------------------------------------------------------
    @staticmethod
    def _where(kind=None, state=None, session_id=None, device=None, q=None) -> tuple[str, list]:
        conds, args = [], []
        for col, val in (("p.kind", kind), ("p.state", state), ("p.session_id", session_id)):
            if val:
                conds.append(f"{col} = ?"); args.append(val)
        if device:
            conds.append("p.device = ? COLLATE NOCASE"); args.append(device)
        for word in (q or "").split()[:6]:  # every word must appear somewhere in the post or its replies
            conds.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in (
                "p.title", "p.body", "p.author_name", "p.author", "p.tags", "p.device", "p.session_id")) +
                " OR EXISTS (SELECT 1 FROM board_replies r WHERE r.post_id = p.id AND r.body LIKE ? ESCAPE '\\'))")
            args += [_like(word)] * 8
        return (" WHERE " + " AND ".join(conds)) if conds else "", args

    def list(self, kind: str | None = None, state: str | None = None, session_id: str | None = None,
             device: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0,
             replies: bool = True) -> list[dict]:
        """Posts, pinned first, then newest activity first. Each has n_replies, activity_at and
        (replies=True) its reply thread."""
        where, args = self._where(kind, state, session_id, device, q)
        with self._con() as con:
            rows = [self._post(r) for r in con.execute(
                f"{_SELECT}{where} ORDER BY p.pinned DESC, activity_at DESC, p.id DESC LIMIT ? OFFSET ?",
                (*args, max(1, min(int(limit), 500)), max(0, int(offset))))]
            if replies:
                rep = self._replies(con, [p["id"] for p in rows])
                for p in rows:
                    p["replies"] = rep[p["id"]]
            reac = self._reactions(con, [p["id"] for p in rows])
            for p in rows:
                p["reactions"] = reac[p["id"]]
        return rows

    def count(self, kind: str | None = None, state: str | None = None, session_id: str | None = None,
              device: str | None = None, q: str | None = None) -> int:
        """How many posts match (for paging)."""
        where, args = self._where(kind, state, session_id, device, q)
        with self._con() as con:
            return con.execute(f"SELECT COUNT(*) FROM board_posts p{where}", args).fetchone()[0]

    def get(self, post_id: int) -> dict | None:
        with self._con() as con:
            r = con.execute(f"{_SELECT} WHERE p.id=?", (post_id,)).fetchone()
            if r is None:
                return None
            p = self._post(r)
            p["replies"] = self._replies(con, [p["id"]])[p["id"]]
            p["reactions"] = self._reactions(con, [p["id"]])[p["id"]]
        return p

    def recent(self, n: int = 5) -> list[dict]:
        """The n posts with the newest activity, ignoring pins (home page widget); no reply threads."""
        with self._con() as con:
            return [self._post(r) for r in con.execute(
                f"{_SELECT} ORDER BY activity_at DESC, p.id DESC LIMIT ?", (max(1, min(int(n), 100)),))]

    def counts(self) -> dict:
        """Things still waiting for somebody: {"question": n, "request": n, "issue": n, "open": sum}."""
        out = {k: 0 for k in COUNTED}
        with self._con() as con:
            marks = ",".join("?" * len(COUNTED))
            for r in con.execute(f"SELECT kind, COUNT(*) AS n FROM board_posts WHERE state='open' AND kind IN ({marks})"
                                 " GROUP BY kind", COUNTED):
                out[r["kind"]] = r["n"]
        return {**out, "open": sum(out.values())}

    def kind_counts(self, state: str | None = None) -> dict:
        """Posts per kind (every kind present, 0 if none) plus "all"; optionally only one state."""
        out = {k: 0 for k in KINDS}
        with self._con() as con:
            for r in con.execute("SELECT kind, COUNT(*) AS n FROM board_posts" +
                                 (" WHERE state=?" if state else "") + " GROUP BY kind", (state,) if state else ()):
                if r["kind"] in out:
                    out[r["kind"]] = r["n"]
        return {**out, "all": sum(out.values())}

    # ---- reactions -------------------------------------------------------------
    def _reactions(self, con, ids: list[int]) -> dict[int, dict[str, list[dict]]]:
        """{post_id: {reaction key: [{"who", "name"}, ...] in the order they were given}}; keys with nobody left out."""
        out: dict[int, dict[str, list[dict]]] = {i: {} for i in ids}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in con.execute(f"SELECT * FROM board_reactions WHERE post_id IN ({marks}) ORDER BY created_at, rowid", ids):
                out[r["post_id"]].setdefault(r["reaction"], []).append({"who": r["who"], "name": r["who_name"] or r["who"]})
        return out

    def react(self, post_id: int, who: str, reaction: str, on: bool | None = None, who_name: str | None = None) -> bool:
        """Give (on=True), take back (on=False) or toggle (None) one reaction; returns whether it is now given."""
        if reaction not in REACTIONS:
            raise BoardError(f"reaction must be one of: {', '.join(REACTIONS)}")
        who = _text(who, MAX_NAME, "the name")
        if not who:
            raise BoardError("who is reacting? (name missing)")
        with self._con() as con:
            if not con.execute("SELECT 1 FROM board_posts WHERE id=?", (post_id,)).fetchone():
                raise KeyError(post_id)
            key = (post_id, who, reaction)
            have = con.execute("SELECT 1 FROM board_reactions WHERE post_id=? AND who=? AND reaction=?", key).fetchone()
            want = (not have) if on is None else bool(on)
            if want and not have:
                con.execute("INSERT INTO board_reactions (post_id, who, who_name, reaction, created_at) VALUES (?,?,?,?,?)",
                            (*key[:2], _text(who_name, MAX_NAME, "the name") or who, reaction, _now()))
            elif have and not want:
                con.execute("DELETE FROM board_reactions WHERE post_id=? AND who=? AND reaction=?", key)
        return want

    # ---- site feedback ---------------------------------------------------------
    @staticmethod
    def area(page: str) -> str:
        """Group pages: '/' -> '/', '/sessions/S001' -> '/sessions/…', '/board?x' -> '/board'."""
        segs = [s for s in str(page or "/").split("?")[0].split("#")[0].split("/") if s]
        return "/" + (segs[0] + ("/…" if len(segs) > 1 else "") if segs else "")

    def add_feedback(self, page: str, mood=None, text: str = "", *, page_title: str = "", part: str = "",
                     who: str | None = None, who_name: str | None = None) -> dict:
        import secrets
        page = _text(page, 300, "the page") or "/"
        mood = self._mood(mood)
        text = _text(text, MAX_FEEDBACK, "the feedback")
        if mood is None and not text:
            raise BoardError("pick a face or write a few words")
        now = _now()
        with self._con() as con:
            fid = con.execute(
                "INSERT INTO board_feedback (created_at, updated_at, token, page, area, page_title, part, mood, text,"
                " who, who_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, secrets.token_urlsafe(16), page, self.area(page), _text(page_title, 200, "the title"),
                 _text(part, 300, "the part"), mood, text, _text(who, MAX_NAME, "the name") or None,
                 _text(who_name, MAX_NAME, "the name") or None)).lastrowid
        return self.feedback_item(fid, with_token=True)

    def update_feedback(self, fid: int, token: str, **changes) -> dict:
        """The sender adds / changes words, mood, part or name (only with the token they got back)."""
        cur = self.feedback_item(fid, with_token=True)
        if cur is None:
            raise KeyError(fid)
        if not token or token != cur["token"]:
            raise PermissionError("not your feedback")
        mood = self._mood(changes["mood"]) if "mood" in changes else cur["mood"]
        text = _text(changes["text"], MAX_FEEDBACK, "the feedback") if "text" in changes else cur["text"]
        part = _text(changes["part"], 300, "the part") if "part" in changes else cur["part"]
        who = (_text(changes["who"], MAX_NAME, "the name") or None) if "who" in changes else cur["who"]
        name = (_text(changes.get("who_name"), MAX_NAME, "the name") or who) if "who" in changes else cur["who_name"]
        if mood is None and not text:
            raise BoardError("pick a face or write a few words")
        with self._con() as con:
            con.execute("UPDATE board_feedback SET mood=?, text=?, part=?, who=?, who_name=?, updated_at=? WHERE id=?",
                        (mood, text, part, who, name if who else None, _now(), fid))
        return self.feedback_item(fid)

    @staticmethod
    def _mood(m):
        if m in (None, "", 0):
            return None
        try:
            m = int(m)
        except (TypeError, ValueError):
            m = 0
        if m not in MOODS:
            raise BoardError("mood must be 1 (hate it) .. 5 (love it)")
        return m

    def feedback_item(self, fid: int, with_token: bool = False) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM board_feedback WHERE id=?", (fid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        if not with_token:
            d.pop("token")
        return d

    def delete_feedback(self, fid: int) -> bool:
        with self._con() as con:
            return con.execute("DELETE FROM board_feedback WHERE id=?", (fid,)).rowcount > 0

    def feedback(self, area: str | None = None, limit: int = 200) -> list[dict]:
        """Newest first, without tokens."""
        with self._con() as con:
            rows = con.execute("SELECT * FROM board_feedback" + (" WHERE area=?" if area else "") +
                               " ORDER BY id DESC LIMIT ?", ((area,) if area else ()) + (max(1, min(int(limit), 1000)),))
            return [{k: r[k] for k in r.keys() if k != "token"} for r in rows]

    def feedback_summary(self) -> list[dict]:
        """Per area: {"area", "title" (latest page title), "n", "moods": {1: n, ..5}, "avg", "last_at"};
        the areas people feel worst about first."""
        out: dict[str, dict] = {}
        with self._con() as con:
            for r in con.execute("SELECT area, page_title, mood, created_at FROM board_feedback ORDER BY id"):
                a = out.setdefault(r["area"], {"area": r["area"], "title": "", "n": 0, "moods": {m: 0 for m in MOODS},
                                               "last_at": ""})
                a["n"] += 1
                a["title"] = r["page_title"] or a["title"]
                a["last_at"] = r["created_at"]
                if r["mood"] in MOODS:
                    a["moods"][r["mood"]] += 1
        for a in out.values():
            k = sum(a["moods"].values())
            a["avg"] = round(sum(m * n for m, n in a["moods"].items()) / k, 2) if k else None
        return sorted(out.values(), key=lambda a: (a["avg"] if a["avg"] is not None else 3, -a["n"]))
