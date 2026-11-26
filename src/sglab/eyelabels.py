"""Eye-openness labels: what people (and, later, reference devices) say each camera frame shows.

Ground truth for training and scoring the eye-openness detector. Every frame of a camera stream
gets one of six levels:

    0  eye not visible (glasses off, eye out of frame, picture unusable)
    1  closed      2  1/4 open      3  1/2 open      4  3/4 open      5  open

openness = (level - 1) / 4 for levels 1..5. A frame nobody has labeled is '.'.

Several people can label the same stream; each keeps their own track, so their agreement can be
measured (the ceiling any model can reach). `source` separates human labels from labels made by
a reference device (e.g. a slow-motion phone video of the cued blinks), which may come later.
A labeler can work "blind" (the server's guesses hidden), which shows how much the guesses sway people.

The eye box (crop) is shared per stream: it defines the region the detector is trained on.

Stored in the catalog SQLite file next to the board. A track is one string with one character per
frame ('.', '0'..'5'), so a 30 000-frame session is 30 kB and an edit is one read-modify-write.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LEVELS = {0: "eye not visible", 1: "closed", 2: "¼ open", 3: "½ open", 4: "¾ open", 5: "open"}
UNLABELED = "."
SOURCES = ("human", "reference")
MAX_FRAMES = 2_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS eye_labels (
    session_id   TEXT NOT NULL,
    stream       TEXT NOT NULL,
    labeler      TEXT NOT NULL,         -- username, or the typed name in single-user mode
    source       TEXT NOT NULL DEFAULT 'human',
    labeler_name TEXT,
    levels       TEXT NOT NULL,         -- one char per frame: '.' unlabeled, '0'..'5'
    n_labeled    INTEGER NOT NULL DEFAULT 0,
    blind        INTEGER NOT NULL DEFAULT 0,   -- labeled with the guesses hidden
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, stream, labeler, source)
);
CREATE TABLE IF NOT EXISTS eye_crops (
    session_id   TEXT NOT NULL,
    stream       TEXT NOT NULL,
    x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
    set_by       TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, stream)
);
"""


class LabelError(ValueError):
    """Bad input; the message is shown to the user."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def openness(level: int) -> float | None:
    return None if level < 1 else (level - 1) / 4


class LabelStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)

    @contextmanager
    def _con(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _track(r, with_levels: bool = True) -> dict:
        d = {"labeler": r["labeler"], "labeler_name": r["labeler_name"] or r["labeler"], "source": r["source"],
             "n_frames": len(r["levels"]), "n_labeled": r["n_labeled"], "blind": bool(r["blind"]),
             "created_at": r["created_at"], "updated_at": r["updated_at"]}
        if with_levels:
            d["levels"] = r["levels"]
        return d

    # ---- tracks ------------------------------------------------------------------------------
    def get(self, sid: str, stream: str, labeler: str, source: str = "human") -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM eye_labels WHERE session_id=? AND stream=? AND labeler=? AND source=?",
                            (sid, stream, labeler, source)).fetchone()
        return self._track(r) if r else None

    def apply(self, sid: str, stream: str, labeler: str, n_frames: int, ranges, *, labeler_name: str | None = None,
              source: str = "human", blind: bool | None = None) -> dict:
        """Set frames i0..i1 (inclusive) to a level (0..5), or clear them (level None), for each
        (i0, i1, level) in `ranges`, in order. Creates the track on first use."""
        if source not in SOURCES:
            raise LabelError(f"source must be one of: {', '.join(SOURCES)}")
        labeler = str(labeler or "").strip()[:80]
        if not labeler:
            raise LabelError("who is labeling? (name missing)")
        if not 0 < int(n_frames) <= MAX_FRAMES:
            raise LabelError("the stream has no frames")
        n = int(n_frames)
        parsed = []
        for rg in ranges or []:
            try:
                i0, i1, lev = int(rg[0]), int(rg[1]), rg[2]
            except (TypeError, ValueError, IndexError):
                raise LabelError("each change is [first frame, last frame, level]") from None
            if lev is not None and (not isinstance(lev, int) or isinstance(lev, bool) or lev not in LEVELS):
                raise LabelError("level must be 0..5 or null")
            i0, i1 = max(0, min(i0, i1)), min(n - 1, max(i0, i1))
            if i0 <= i1:
                parsed.append((i0, i1, UNLABELED if lev is None else str(lev)))
        now = _now()
        with self._con() as con:
            con.execute("BEGIN IMMEDIATE")   # read-modify-write of one track: no lost updates between tabs
            r = con.execute("SELECT * FROM eye_labels WHERE session_id=? AND stream=? AND labeler=? AND source=?",
                            (sid, stream, labeler, source)).fetchone()
            cur = list(r["levels"]) if r else [UNLABELED] * n
            if len(cur) != n:   # the recording was re-imported with another frame count: keep what still fits
                cur = (cur + [UNLABELED] * n)[:n]
            for i0, i1, ch in parsed:
                cur[i0:i1 + 1] = ch * (i1 - i0 + 1)
            s = "".join(cur)
            n_lab = n - s.count(UNLABELED)
            if r:
                con.execute("UPDATE eye_labels SET levels=?, n_labeled=?, updated_at=?, labeler_name=COALESCE(?, labeler_name),"
                            " blind=COALESCE(?, blind) WHERE session_id=? AND stream=? AND labeler=? AND source=?",
                            (s, n_lab, now, labeler_name, None if blind is None else int(bool(blind)),
                             sid, stream, labeler, source))
            else:
                con.execute("INSERT INTO eye_labels (session_id, stream, labeler, source, labeler_name, levels, n_labeled,"
                            " blind, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (sid, stream, labeler, source, labeler_name or labeler, s, n_lab, int(bool(blind)), now, now))
        return self.get(sid, stream, labeler, source)

    def tracks(self, sid: str, stream: str) -> list[dict]:
        """Everybody's tracks of one stream (without the per-frame levels), most labeled first."""
        with self._con() as con:
            rows = con.execute("SELECT * FROM eye_labels WHERE session_id=? AND stream=? ORDER BY n_labeled DESC",
                               (sid, stream)).fetchall()
        return [self._track(r, False) for r in rows]

    def progress(self) -> list[dict]:
        """One row per labeled stream: who labeled how much (for the labeling overview page)."""
        with self._con() as con:
            rows = con.execute("SELECT * FROM eye_labels ORDER BY updated_at DESC").fetchall()
        out: dict[tuple, dict] = {}
        for r in rows:
            k = (r["session_id"], r["stream"])
            o = out.setdefault(k, {"session_id": k[0], "stream": k[1], "n_frames": len(r["levels"]),
                                   "updated_at": r["updated_at"], "tracks": []})
            o["tracks"].append(self._track(r, False))
        return list(out.values())

    def delete(self, sid: str, stream: str, labeler: str, source: str = "human") -> bool:
        with self._con() as con:
            return con.execute("DELETE FROM eye_labels WHERE session_id=? AND stream=? AND labeler=? AND source=?",
                               (sid, stream, labeler, source)).rowcount > 0

    # ---- eye box -----------------------------------------------------------------------------
    def crop(self, sid: str, stream: str) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM eye_crops WHERE session_id=? AND stream=?", (sid, stream)).fetchone()
        return {k: r[k] for k in ("x", "y", "w", "h", "set_by", "updated_at")} if r else None

    def set_crop(self, sid: str, stream: str, box, frame_w: int, frame_h: int, by: str = "") -> dict | None:
        """box = {x, y, w, h} in frame pixels (clipped to the frame), or None to forget it."""
        with self._con() as con:
            if box is None:
                con.execute("DELETE FROM eye_crops WHERE session_id=? AND stream=?", (sid, stream))
                return None
            try:
                x, y, w, h = (int(round(float(box[k]))) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                raise LabelError("the eye box needs x, y, w and h") from None
            x, y = max(0, min(x, frame_w - 1)), max(0, min(y, frame_h - 1))
            w, h = min(w, frame_w - x), min(h, frame_h - y)
            if w < 8 or h < 8:
                raise LabelError("the eye box is too small (at least 8 × 8 pixels)")
            con.execute("INSERT OR REPLACE INTO eye_crops (session_id, stream, x, y, w, h, set_by, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?)", (sid, stream, x, y, w, h, by or None, _now()))
        return self.crop(sid, stream)


def agreement(a: str, b: str) -> dict:
    """How two tracks of the same stream agree, on the frames both labeled.
    exact: same level; within_one: levels 1..5 at most one step apart (0 must match exactly);
    kappa: Cohen's kappa on the six levels (chance-corrected exact agreement)."""
    pairs = [(int(x), int(y)) for x, y in zip(a, b) if x != UNLABELED and y != UNLABELED]
    n = len(pairs)
    if not n:
        return {"n_both": 0, "exact": None, "within_one": None, "kappa": None}
    exact = sum(x == y for x, y in pairs) / n
    within = sum((x == y) if 0 in (x, y) else abs(x - y) <= 1 for x, y in pairs) / n
    pa = [sum(x == k for x, _ in pairs) / n for k in LEVELS]
    pb = [sum(y == k for _, y in pairs) / n for k in LEVELS]
    pe = sum(p * q for p, q in zip(pa, pb))
    kappa = None if pe >= 1 else (exact - pe) / (1 - pe)
    return {"n_both": n, "exact": round(exact, 4), "within_one": round(within, 4),
            "kappa": None if kappa is None else round(kappa, 4)}


def crop_key(crop: dict | None) -> str:
    return json.dumps([crop[k] for k in ("x", "y", "w", "h")]) if crop else "full"
