"""SQLite catalog of subjects, sessions, streams, events and processing runs.

Design rule: **the files are the source of truth, the database is an index.**
Every session folder carries a complete session.json; `sglab reindex`
rebuilds the sessions/streams/events tables from those files at any time.
Subjects are the one thing primarily stored here (and snapshotted into each
session.json, so they can be recovered too).
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subjects (
    subject_id   TEXT PRIMARY KEY,     -- de-identified code, e.g. S001 (no names here!)
    created_at   TEXT,
    sex          TEXT,
    age_years    REAL,
    height_cm    REAL,
    weight_kg    REAL,
    fitzpatrick  TEXT,                 -- skin type I-VI: affects PPG signal strength
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    subject_id   TEXT,
    protocol     TEXT,
    trial        INTEGER,
    started_at   TEXT,                 -- ISO 8601 UTC
    duration_s   REAL,
    status       TEXT,                 -- ok | discard | incomplete
    quality      TEXT,
    operator     TEXT,
    notes        TEXT,
    path         TEXT,                 -- relative to data root
    extra        TEXT,                 -- JSON: every other metadata field
    indexed_at   TEXT
);

CREATE TABLE IF NOT EXISTS streams (
    session_id     TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    stream         TEXT,
    role           TEXT,
    dev            TEXT,
    fw             TEXT,
    fs             REAL,
    channels       TEXT,
    n_samples      INTEGER,
    n_dropped      INTEGER,
    n_bad_lines    INTEGER,
    file           TEXT,
    PRIMARY KEY (session_id, stream)
);

CREATE TABLE IF NOT EXISTS events (
    session_id  TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    t_s         REAL,
    kind        TEXT,                  -- bp | cuff_start | spo2 | marker | device
    source      TEXT,
    sys_mmhg    REAL,
    dia_mmhg    REAL,
    hr_bpm      REAL,
    spo2_pct    REAL,
    label       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    stream            TEXT,
    pipeline_version  TEXT,
    config_hash       TEXT,
    created_at        TEXT,
    out_dir           TEXT,
    status            TEXT,            -- ok | error
    summary           TEXT             -- JSON
);

CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, stream);
"""

CORE = {"subject_id", "protocol", "trial", "quality", "operator", "notes"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


class Catalog:
    def __init__(self, db_path: Path, data_root: Path):
        self.db_path, self.data_root = Path(db_path), Path(data_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.db_path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def close(self):
        self.con.close()

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    # -- subjects -----------------------------------------------------------
    def subjects(self) -> list[dict]:
        return self.q("""SELECT s.*, COUNT(x.session_id) AS n_sessions
                         FROM subjects s LEFT JOIN sessions x
                           ON x.subject_id = s.subject_id AND x.status != 'discard'
                         GROUP BY s.subject_id ORDER BY s.subject_id""")

    def subject(self, sid: str) -> dict | None:
        r = self.q("SELECT * FROM subjects WHERE subject_id = ?", (sid,))
        return r[0] if r else None

    def upsert_subject(self, d: dict) -> None:
        cols = ["subject_id", "created_at", "sex", "age_years", "height_cm", "weight_kg", "fitzpatrick", "notes"]
        d = {**d}
        d.setdefault("created_at", _now())
        vals = [d.get(c) for c in cols]
        self.con.execute(
            f"INSERT INTO subjects ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            "ON CONFLICT(subject_id) DO UPDATE SET " +
            ",".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in cols[2:]), vals)
        self.con.commit()

    def next_subject_id(self) -> str:
        ids = [r["subject_id"] for r in self.q("SELECT subject_id FROM subjects")]
        nums = [int(i[1:]) for i in ids if i[:1] == "S" and i[1:].isdigit()]
        return f"S{(max(nums) + 1 if nums else 1):03d}"

    def next_trial(self, subject_id: str, protocol: str) -> int:
        r = self.q("SELECT MAX(trial) AS m FROM sessions WHERE subject_id=? AND protocol=?", (subject_id, protocol))
        return int(r[0]["m"] or 0) + 1

    # -- sessions -----------------------------------------------------------
    def upsert_session(self, folder: Path) -> str:
        doc = json.loads((folder / "session.json").read_text())
        meta = doc.get("metadata", {})
        sid = doc.get("session_id") or folder.name
        rel = str(folder.resolve().relative_to(self.data_root.resolve()))
        subj = meta.get("subject_id")
        if subj:
            snap = doc.get("subject") or {"subject_id": subj}
            if self.subject(subj) is None:
                self.upsert_subject(snap)
        extra = {k: v for k, v in meta.items() if k not in CORE}
        cur = self.con.cursor()
        cur.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
        cur.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, subj, meta.get("protocol"), meta.get("trial"), doc.get("started_at"), doc.get("duration_s"),
             doc.get("status", "ok"), meta.get("quality"), meta.get("operator"), meta.get("notes"), rel,
             json.dumps(extra, default=str), _now()))
        for s in doc.get("streams", []):
            st = s.get("stats", {})
            cur.execute("INSERT INTO streams VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, s.get("stream"), s.get("role"), s.get("dev"), s.get("fw"), s.get("fs"),
                         "|".join(s.get("channels") or []), st.get("n_samples"), st.get("n_dropped_seq"),
                         (st.get("n_checksum_err") or 0) + (st.get("n_malformed") or 0), s.get("file")))
        ev_path = folder / "events.csv"
        if ev_path.exists():
            with open(ev_path, newline="") as f:
                for e in csv.DictReader(f):
                    cur.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                                (sid, _num(e.get("t_s")), e.get("kind"), e.get("source"), _num(e.get("sys_mmhg")),
                                 _num(e.get("dia_mmhg")), _num(e.get("hr_bpm")), _num(e.get("spo2_pct")),
                                 e.get("label")))
        self.con.commit()
        return sid

    def reindex(self) -> int:
        raw = self.data_root / "raw"
        keep_runs = self.q("SELECT * FROM runs")
        self.con.execute("DELETE FROM events")
        self.con.execute("DELETE FROM streams")
        self.con.execute("DELETE FROM sessions")
        n = 0
        for sj in sorted(raw.glob("*/session.json")):
            if sj.parent.name.startswith("_"):
                continue
            self.upsert_session(sj.parent)
            n += 1
        # runs survive a reindex only if their session still exists
        existing = {r["session_id"] for r in self.q("SELECT session_id FROM sessions")}
        self.con.execute("DELETE FROM runs")
        for r in keep_runs:
            if r["session_id"] in existing:
                self.con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)", tuple(r.values()))
        self.con.commit()
        return n

    def sessions(self, subject: str | None = None, protocol: str | None = None,
                 include_discarded: bool = False) -> list[dict]:
        sql = """SELECT s.*,
                   (SELECT COUNT(*) FROM streams t WHERE t.session_id = s.session_id) AS n_streams,
                   (SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id AND e.kind = 'bp') AS n_bp,
                   (SELECT COUNT(DISTINCT r.stream) FROM runs r WHERE r.session_id = s.session_id AND r.status='ok') AS n_processed
                 FROM sessions s WHERE 1=1"""
        args = []
        if subject:
            sql += " AND s.subject_id = ?"; args.append(subject)
        if protocol:
            sql += " AND s.protocol = ?"; args.append(protocol)
        if not include_discarded:
            sql += " AND s.status != 'discard'"
        sql += " ORDER BY s.started_at DESC"
        return self.q(sql, args)

    def session(self, sid: str) -> dict | None:
        r = self.q("SELECT * FROM sessions WHERE session_id = ?", (sid,))
        if not r:
            # allow unique prefixes / suffixes for convenience on the command line
            r = self.q("SELECT * FROM sessions WHERE session_id LIKE ?", (f"%{sid}%",))
            if len(r) != 1:
                return None
        s = r[0]
        s["extra"] = json.loads(s["extra"] or "{}")
        s["streams"] = self.q("SELECT * FROM streams WHERE session_id=? ORDER BY stream", (s["session_id"],))
        s["events"] = self.q("SELECT * FROM events WHERE session_id=? ORDER BY t_s", (s["session_id"],))
        s["runs"] = self.latest_runs(s["session_id"])
        return s

    # -- processing runs ----------------------------------------------------
    def add_run(self, session_id, stream, version, config_hash, out_dir, status, summary) -> None:
        self.con.execute("INSERT INTO runs (session_id, stream, pipeline_version, config_hash, created_at, out_dir, status, summary) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (session_id, stream, version, config_hash, _now(), out_dir, status,
                          json.dumps(summary, default=str)))
        self.con.commit()

    def latest_runs(self, session_id: str) -> list[dict]:
        rows = self.q("""SELECT r.* FROM runs r
                         JOIN (SELECT stream, MAX(run_id) AS m FROM runs WHERE session_id=? GROUP BY stream) x
                           ON r.run_id = x.m ORDER BY r.stream""", (session_id,))
        for r in rows:
            r["summary"] = json.loads(r["summary"] or "{}")
        return rows

    def has_run(self, session_id, stream, config_hash) -> bool:
        return bool(self.q("SELECT 1 FROM runs WHERE session_id=? AND stream=? AND config_hash=? AND status='ok'",
                           (session_id, stream, config_hash)))
