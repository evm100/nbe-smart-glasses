"""Job queue for remote workers (`[processing] mode = "remote"`).

The lab server stays the single home of recordings and results; a stronger machine runs
`sglab worker`, which claims jobs over HTTPS (/api/worker/*). Kinds:
  process  download a session's raw CSVs, run Layers 1-2, upload the outputs
  sweep    download a Layer-3 dataset, run a Layer-4 sweep (sglab.sweep), upload the model run
Nothing has to reach the worker, so it can sit behind NAT. A worker only gets the kinds it
asks for, so an older worker never claims a sweep.

Stored in the catalog SQLite file (table jobs). A claimed job that hears nothing from its
worker for LEASE_S goes back to the queue (worker crashed or was rebooted).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEASE_S = 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,          -- process | sweep
    session_id   TEXT NOT NULL,          -- '' for sweeps
    force        INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL,          -- queued | running | done | error
    worker       TEXT,
    created_at   TEXT,
    claimed_at   TEXT,                   -- also refreshed by every upload (the lease)
    finished_at  TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id, job_id);
"""
# columns added after the first release: (name, type)
ADDED = [("payload", "TEXT"),    # sweep: JSON {dataset, name, layer4, spec}
         ("progress", "TEXT")]   # sweep: "34/420"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, db_path: Path):
        self.con = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        have = {r[1] for r in self.con.execute("PRAGMA table_info(jobs)")}
        for col, typ in ADDED:
            if col not in have:
                self.con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        self.lock = threading.Lock()

    def close(self):
        self.con.close()

    def _q(self, sql, args=()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def enqueue(self, session_id: str, force: bool = False) -> int:
        """Queue processing of a session; a job already waiting for it is reused (and upgraded to force)."""
        with self.lock:
            r = self._q("SELECT job_id FROM jobs WHERE session_id=? AND kind='process' AND state='queued'",
                        (session_id,))
            if r:
                if force:
                    self.con.execute("UPDATE jobs SET force=1 WHERE job_id=?", (r[0]["job_id"],))
                return r[0]["job_id"]
            return self.con.execute("INSERT INTO jobs (kind, session_id, force, state, created_at) "
                                    "VALUES ('process', ?, ?, 'queued', ?)",
                                    (session_id, int(force), _iso(_now()))).lastrowid

    def enqueue_sweep(self, payload: dict) -> int:
        with self.lock:
            return self.con.execute("INSERT INTO jobs (kind, session_id, state, created_at, payload) "
                                    "VALUES ('sweep', '', 'queued', ?, ?)",
                                    (_iso(_now()), json.dumps(payload))).lastrowid

    def claim(self, worker: str, kinds: tuple[str, ...] = ("process",)) -> dict | None:
        """Oldest queued job of these kinds (or one whose worker went silent), now running for `worker`."""
        marks = ",".join("?" * len(kinds))
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                stale = _iso(_now() - timedelta(seconds=LEASE_S))
                r = self._q(f"SELECT * FROM jobs WHERE kind IN ({marks}) AND "
                            "(state='queued' OR (state='running' AND claimed_at < ?)) "
                            "ORDER BY job_id LIMIT 1", (*kinds, stale))
                if r:
                    self.con.execute("UPDATE jobs SET state='running', worker=?, claimed_at=? WHERE job_id=?",
                                     (worker, _iso(_now()), r[0]["job_id"]))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return self.get(r[0]["job_id"]) if r else None

    def get(self, job_id: int) -> dict | None:
        r = self._q("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not r:
            return None
        r[0]["payload"] = json.loads(r[0]["payload"]) if r[0].get("payload") else None
        return r[0]

    def touch(self, job_id: int) -> None:
        with self.lock:
            self.con.execute("UPDATE jobs SET claimed_at=? WHERE job_id=?", (_iso(_now()), job_id))

    def set_progress(self, job_id: int, progress: str) -> None:
        """Also renews the lease: a long sweep uploads nothing until it is done."""
        with self.lock:
            self.con.execute("UPDATE jobs SET progress=?, claimed_at=? WHERE job_id=?",
                             (progress, _iso(_now()), job_id))

    def finish(self, job_id: int, error: str | None = None) -> None:
        with self.lock:
            self.con.execute("UPDATE jobs SET state=?, finished_at=?, error=? WHERE job_id=?",
                             ("error" if error else "done", _iso(_now()), error, job_id))

    def state(self, session_id: str) -> str | None:
        """What the session page shows: 'queued', 'running on <worker>' or 'error: ...' (latest job);
        None once it is done (the results are on the page then)."""
        r = self._q("SELECT state, worker, error FROM jobs WHERE session_id=? AND kind='process' "
                    "ORDER BY job_id DESC LIMIT 1", (session_id,))
        if not r:
            return None
        j = r[0]
        if j["state"] == "error":
            return f"error: {j['error']}"
        if j["state"] == "running":
            return f"running on {j['worker']}"
        return "queued" if j["state"] == "queued" else None

    def recent(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self._q("SELECT * FROM jobs WHERE kind=? ORDER BY job_id DESC LIMIT ?", (kind, limit))
        else:
            rows = self._q("SELECT * FROM jobs ORDER BY job_id DESC LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r["payload"]) if r.get("payload") else None
        return rows

    def pending(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued', 'running')")[0]["n"]
