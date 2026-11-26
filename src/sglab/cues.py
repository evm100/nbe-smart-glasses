"""Cued sessions: scripted protocols the live trial page plays (instructions, beeps, gaze targets),
and the log of exactly when each cue happened, on the session clock.

Protocols are data: config/cue_protocols.json (see the file for the format). ``load_protocols``
validates them and ``expand`` turns each step's cue rule into an explicit list of cues with times
relative to the step start, which is what the browser plays.

Timing (why the numbers in cues.json can be trusted to a few ms)
----------------------------------------------------------------
Camera frames, PPG samples and events all use t_s = seconds since the recording started on the
server's monotonic clock (sglab.recorder; frame capture times via sglab.video.frame_times). The
browser has its own clock, performance.now(). To put browser times on t_s the page pings the
server (POST .../cues/ping) a few times at the start, every ~10 s while it runs and at the end.
Each ping is an NTP exchange:

    p0  browser sends          (performance.now(), ms)
    t1  server receives        (t_s, stamped as the request enters the app)
    t2  server replies         (t_s, stamped as the handler returns)
    p1  browser receives

    offset = ((t1 - p0) + (t2 - p1)) / 2      t_s = perf_s + offset
    delay  = (p1 - p0) - (t2 - t1)            |offset error| <= delay / 2

``fit_clock`` keeps the low-delay pings and fits offset = a + b·perf_s (b = drift between the
two computers' clocks, typically < 100 ppm = 60 ms per 10 min, which is why we keep pinging).

Each cue is logged with the browser time it reached the wearer: for a beep the moment the
sound card plays it (AudioContext.getOutputTimestamp maps audio-clock time to performance time),
for screen changes the requestAnimationFrame time of the frame that drew it. ``t_s`` is then
fit(perf). The server also stores when it received the log entry (t_recv_s, seconds later): a
cross-check only.

Expected error of a cue's t_s vs. the camera frames' t_s (details: docs/CUED_SESSIONS.md):
  clock offset              <= delay/2 of the best pings: ~1-3 ms on the lab LAN, ~5-20 ms over
                            the internet (bounded, usually much smaller)
  sound output              audio stack latency the browser does not report: ~0-10 ms on built-in
                            or wired speakers, 100-300 ms on Bluetooth (don't use Bluetooth)
  screen                    the frame appears ~1-2 refreshes after rAF: +15-35 ms on a 60 Hz screen
  frame times               device capture time + the least link delay seen (a few ms over USB,
                            5-20 ms over Wi-Fi), so frames sit slightly late on t_s
Net: beeps within ~±10 ms of the frame clock (wired audio, LAN); screen cues 15-35 ms late.
The wearer's reaction (~150-400 ms after a beep) is real behaviour, not clock error.
"""
from __future__ import annotations

import csv
import io
import json
import math
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
PROTOCOLS_FILE = Path("config") / "cue_protocols.json"
CUES_FILE = "cues.json"
CUES_CSV = "cues.csv"

GAZE = ("ahead", "up", "down", "left", "right")
TONES = ("beep", "release", "tick", "none")
STEP_KEYS = {"id", "title", "detail", "label", "negative", "lead_in_s", "duration_s", "cues", "same_as", "tone"}
CUE_TYPES = ("repeat", "sequence", "timed")
MAX_STEP_S = 3600.0

# what the browser may send per log entry (anything else is dropped)
ENTRY_FIELDS = {
    "key": str, "kind": str, "step": int, "step_id": str, "attempt": int, "cue": int, "action": str,
    "label": str, "text": str, "gaze": str, "tone": str, "status": str, "note": str,
    "perf_ms": float, "planned_perf_ms": float, "screen_perf_ms": float, "late_ms": float,
    "cancelled": bool, "hidden": bool,
}
ENTRY_KINDS = ("run_start", "step_start", "step_end", "cue", "pause", "resume", "skip", "abort", "run_end",
               "page_hidden", "page_visible", "note")
MAX_ENTRIES = 20000
MAX_SYNC = 2000
RUN_STATUSES = ("running", "completed", "aborted", "partial")


class CueProtocolError(ValueError):
    pass


# ====================================================================== protocol definitions
def _num(v, what: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise CueProtocolError(f"{what}: expected a number, got {v!r}")
    if not lo <= v <= hi:
        raise CueProtocolError(f"{what}: {v:g} is outside {lo:g}-{hi:g}")
    return float(v)


def _text(v, what: str, required: bool = True) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise CueProtocolError(f"{what}: expected text")
    return v.strip()


def _expand_cues(spec: dict, lead: float, duration: float | None, where: str, tone: str) -> tuple[list[dict], float]:
    """(cues with t relative to the step start, step duration)."""
    if not isinstance(spec, dict):
        raise CueProtocolError(f"{where}.cues: expected an object")
    typ = spec.get("type", "repeat")
    if typ not in CUE_TYPES:
        raise CueProtocolError(f"{where}.cues.type: one of {', '.join(CUE_TYPES)}")
    tone = spec.get("tone", tone)
    if tone not in TONES:
        raise CueProtocolError(f"{where}.cues.tone: one of {', '.join(TONES)}")
    out: list[dict] = []

    def item(x: dict, i: int, t: float) -> dict:
        if not isinstance(x, dict):
            raise CueProtocolError(f"{where}.cues.items[{i}]: expected an object")
        c = {"t": round(t, 6), "action": _text(x.get("action"), f"{where}.cues.items[{i}].action"),
             "text": _text(x.get("text"), f"{where}.cues.items[{i}].text", required=False) or None,
             "tone": x.get("tone", tone)}
        if c["tone"] not in TONES:
            raise CueProtocolError(f"{where}.cues.items[{i}].tone: one of {', '.join(TONES)}")
        if x.get("gaze") is not None:
            if x["gaze"] not in GAZE:
                raise CueProtocolError(f"{where}.cues.items[{i}].gaze: one of {', '.join(GAZE)}")
            c["gaze"] = x["gaze"]
        return c

    if typ == "repeat":
        every = _num(spec.get("every_s"), f"{where}.cues.every_s", 0.5, 600)
        count = spec.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
            raise CueProtocolError(f"{where}.cues.count: a whole number 1-1000")
        action = _text(spec.get("action"), f"{where}.cues.action")
        text = _text(spec.get("text"), f"{where}.cues.text", required=False) or None
        rel = spec.get("release_after_s")
        if rel is not None:
            rel = _num(rel, f"{where}.cues.release_after_s", 0.1, every - 0.2)
        for k in range(count):
            t = lead + k * every
            out.append({"t": round(t, 6), "action": action, "text": text, "tone": tone})
            if rel is not None:
                out.append({"t": round(t + rel, 6), "action": _text(spec.get("release_action", action + "_release"),
                                                                     f"{where}.cues.release_action"),
                            "text": _text(spec.get("release_text"), f"{where}.cues.release_text", required=False) or None,
                            "tone": "release"})
        need = lead + count * every
        if duration is None:
            duration = need
        elif duration + 1e-9 < need - every + (rel or 0) + 0.2:
            raise CueProtocolError(f"{where}: duration_s {duration:g} is too short for {count} cues every {every:g} s")
    elif typ == "sequence":
        every = _num(spec.get("every_s"), f"{where}.cues.every_s", 0.5, 600)
        items = spec.get("items")
        if not isinstance(items, list) or not items:
            raise CueProtocolError(f"{where}.cues.items: a non-empty list")
        loop = bool(spec.get("loop", True))
        if duration is None:
            if loop:
                raise CueProtocolError(f"{where}: a looping sequence needs duration_s")
            duration = lead + len(items) * every
        k = 0
        while lead + k * every < duration - 1e-9 and (loop or k < len(items)):
            out.append(item(items[k % len(items)], k % len(items), lead + k * every))
            k += 1
    else:  # timed
        items = spec.get("items")
        if not isinstance(items, list) or not items:
            raise CueProtocolError(f"{where}.cues.items: a non-empty list")
        if duration is None:
            raise CueProtocolError(f"{where}: timed cues need duration_s")
        last = -1.0
        for i, x in enumerate(items):
            at = _num((x or {}).get("at_s") if isinstance(x, dict) else None, f"{where}.cues.items[{i}].at_s", 0, MAX_STEP_S)
            if at <= last:
                raise CueProtocolError(f"{where}.cues.items: at_s must increase")
            if lead + at >= duration:
                raise CueProtocolError(f"{where}.cues.items[{i}]: at_s {at:g} is after the step ends")
            last = at
            out.append(item(x, i, lead + at))
    out.sort(key=lambda c: c["t"])
    return out, duration


def validate_protocol(p: dict) -> dict:
    """The protocol with every step expanded (raises CueProtocolError with a readable message)."""
    if not isinstance(p, dict):
        raise CueProtocolError("a protocol must be an object")
    pid = _text(p.get("id"), "protocol id")
    if not all(ch.isalnum() or ch in "_-" for ch in pid):
        raise CueProtocolError(f"protocol id {pid!r}: letters, digits, _ and - only")
    where0 = f"protocol {pid}"
    steps = p.get("steps")
    if not isinstance(steps, list) or not steps:
        raise CueProtocolError(f"{where0}: needs a non-empty list of steps")
    by_id: dict[str, dict] = {}
    out_steps = []
    for i, raw in enumerate(steps):
        where = f"{where0} step {i + 1}"
        if not isinstance(raw, dict):
            raise CueProtocolError(f"{where}: expected an object")
        unknown = set(raw) - STEP_KEYS
        if unknown:
            raise CueProtocolError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        s = dict(raw)
        if s.get("same_as") is not None:
            base = by_id.get(s["same_as"])
            if base is None:
                raise CueProtocolError(f"{where}: same_as {s['same_as']!r} must name an earlier step")
            s = {**{k: v for k, v in base.items() if k != "id"}, **{k: v for k, v in raw.items() if k != "same_as"}}
        sid = _text(s.get("id"), f"{where}.id")
        if sid in by_id:
            raise CueProtocolError(f"{where}: step id {sid!r} is used twice")
        by_id[sid] = {k: v for k, v in s.items() if k != "same_as"}
        lead = _num(s.get("lead_in_s", 3.0), f"{where}.lead_in_s", 0, 60)
        dur = s.get("duration_s")
        if dur is not None:
            dur = _num(dur, f"{where}.duration_s", 1, MAX_STEP_S)
            if dur <= lead:
                raise CueProtocolError(f"{where}: duration_s must be longer than lead_in_s")
        tone = s.get("tone", "beep")
        if s.get("cues") is not None:
            cues, dur = _expand_cues(s["cues"], lead, dur, where, tone)
        else:
            if dur is None:
                raise CueProtocolError(f"{where}: needs duration_s (or cues)")
            cues = []
        if dur > MAX_STEP_S:
            raise CueProtocolError(f"{where}: longer than {MAX_STEP_S:g} s")
        out_steps.append({
            "index": i, "id": sid, "title": _text(s.get("title"), f"{where}.title"),
            "detail": _text(s.get("detail"), f"{where}.detail", required=False) or None,
            "label": _text(s.get("label", sid), f"{where}.label"), "negative": bool(s.get("negative", False)),
            "lead_in_s": lead, "duration_s": round(dur, 6), "cues": cues})
    return {"id": pid, "name": _text(p.get("name"), f"{where0}.name"),
            "summary": _text(p.get("summary"), f"{where0}.summary", required=False) or None,
            "version": p.get("version", 1), "steps": out_steps,
            "total_s": round(sum(s["duration_s"] for s in out_steps), 3),
            "n_cues": sum(len(s["cues"]) for s in out_steps)}


def load_protocols(root: Path) -> list[dict]:
    """Expanded protocols from config/cue_protocols.json ([] if the file is missing)."""
    p = Path(root) / PROTOCOLS_FILE
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text())
    except ValueError as e:
        raise CueProtocolError(f"{PROTOCOLS_FILE}: not valid JSON ({e})") from None
    raw = doc.get("protocols") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        raise CueProtocolError(f"{PROTOCOLS_FILE}: expected {{\"protocols\": [...]}}")
    out = [validate_protocol(x) for x in raw]
    ids = [x["id"] for x in out]
    if len(set(ids)) != len(ids):
        raise CueProtocolError(f"{PROTOCOLS_FILE}: two protocols share an id")
    return out


# ====================================================================== browser clock -> t_s
def ping_sample(s: dict) -> tuple[float, float, float] | None:
    """(perf_mid_s, offset_s, delay_s) of one ping {p0_ms, p1_ms, t1_s, t2_s} (NTP formulas)."""
    try:
        p0, p1 = float(s["p0_ms"]) / 1e3, float(s["p1_ms"]) / 1e3
        t1 = float(s["t1_s"])
        t2 = float(s.get("t2_s", t1))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (p0, p1, t1, t2)) or p1 < p0 or t2 < t1:
        return None
    delay = (p1 - p0) - (t2 - t1)
    return (p0 + p1) / 2, ((t1 - p0) + (t2 - p1)) / 2, max(delay, 0.0)


def fit_clock(samples: list[dict], min_span_s: float = 60.0) -> dict | None:
    """t_s = perf_s + a + b·perf_s from ping samples. Only pings with a delay close to the best are
    used (a slow ping's offset can be off by up to half its delay). b (drift) is fitted only when
    the good pings span at least min_span_s, else 0. None without a usable ping."""
    pts = [x for x in (ping_sample(s) for s in samples or []) if x is not None]
    if not pts:
        return None
    dmin = min(d for _, _, d in pts)
    keep = [x for x in pts if x[2] <= max(2 * dmin, dmin + 0.004)]
    xs, ys = [x[0] for x in keep], [x[1] for x in keep]
    span = max(xs) - min(xs)
    if len(keep) >= 3 and span >= min_span_s:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        a = my - b * mx
    else:
        b = 0.0
        srt = sorted(ys)
        a = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    delays = sorted(d for _, _, d in keep)
    return {"a": a, "b": b, "drift_ppm": round(b * 1e6, 2), "n": len(pts), "n_used": len(keep),
            "span_s": round(span, 3), "delay_min_ms": round(dmin * 1e3, 3),
            "delay_used_max_ms": round(delays[-1] * 1e3, 3),
            "resid_ms_max": round(max(abs(r) for r in resid) * 1e3, 3),
            # offset error bound: half the (median) delay of the pings used, plus how far they scatter
            "err_ms": round(delays[len(delays) // 2] * 1e3 / 2 + max(abs(r) for r in resid) * 1e3, 3)}


def to_session_s(perf_ms: float | None, fit: dict | None) -> float | None:
    if perf_ms is None or fit is None:
        return None
    p = float(perf_ms) / 1e3
    return round(p + fit["a"] + fit["b"] * p, 6)


# ====================================================================== the log
def _clean_entry(e: dict) -> dict | None:
    if not isinstance(e, dict):
        return None
    out = {}
    for k, typ in ENTRY_FIELDS.items():
        v = e.get(k)
        if v is None:
            continue
        try:
            if typ is bool:
                v = bool(v)
            elif typ is int:
                v = int(v)
            elif typ is float:
                v = float(v)
                if not math.isfinite(v):
                    continue
            else:
                v = str(v)[:300]
        except (TypeError, ValueError):
            continue
        out[k] = v
    if out.get("kind") not in ENTRY_KINDS:
        return None
    return out


class CueLog:
    """Every cued run of one recording, kept in <session folder>/cues.json (rewritten after each
    change, so a crash or a closed page still leaves what was logged so far) + cues.csv (one row
    per entry, for spreadsheets / pandas)."""

    def __init__(self, folder_fn, now_fn):
        """folder_fn() -> the recording's current folder (it moves when the session is saved);
        now_fn() -> t_s now."""
        self.folder_fn, self.now = folder_fn, now_fn
        self.runs: list[dict] = []
        self.lock = threading.Lock()

    # ---- runs ---------------------------------------------------------------------
    def run(self, run_id: str) -> dict:
        r = next((x for x in self.runs if x["run_id"] == run_id), None)
        if r is None:
            raise KeyError(run_id)
        return r

    def open_run(self) -> dict | None:
        return next((x for x in self.runs if x["status"] == "running"), None)

    def start(self, protocol: dict, operator: str | None, client: dict | None = None,
              sync: list | None = None) -> dict:
        with self.lock:
            prev = self.open_run()
            if prev is not None:  # the page was reloaded / another tab started one
                self._close(prev, "partial", "a new cued run was started before this one ended")
            r = {"run_id": secrets.token_hex(4), "protocol_id": protocol["id"], "protocol_name": protocol["name"],
                 "protocol": protocol, "status": "running", "status_reason": None, "operator": operator,
                 "created_t_s": round(self.now(), 6),
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 "ended_t_s": None, "client": _client(client), "sync": [], "clock_fit": None, "entries": []}
            self.runs.append(r)
            self._add_sync(r, sync)
            self._save()
            return r

    def log(self, run_id: str, entries: list | None, sync: list | None = None) -> dict:
        with self.lock:
            r = self.run(run_id)
            if r["status"] != "running":
                raise ValueError(f"cued run {run_id} has already ended ({r['status']})")
            self._add(r, entries, sync)
            self._save()
            return r

    def end(self, run_id: str, status: str, reason: str | None = None, entries: list | None = None,
            sync: list | None = None) -> dict:
        if status not in ("completed", "aborted"):
            raise ValueError("status: completed or aborted")
        with self.lock:
            r = self.run(run_id)
            if r["status"] == "running":
                self._add(r, entries, sync)
                self._close(r, status, reason)
            self._save()
            return r

    def close_all(self, reason: str) -> None:
        """The recording stopped: a run still open is kept, marked partial."""
        with self.lock:
            r = self.open_run()
            if r is not None:
                self._close(r, "partial", reason)
                self._save()

    # ---- internals ----------------------------------------------------------------
    def _add_sync(self, r: dict, sync: list | None) -> None:
        for s in (sync or [])[:200]:
            if isinstance(s, dict) and ping_sample(s) is not None and len(r["sync"]) < MAX_SYNC:
                r["sync"].append({k: float(s[k]) for k in ("p0_ms", "p1_ms", "t1_s", "t2_s") if k in s})
        r["clock_fit"] = fit_clock(r["sync"])

    def _add(self, r: dict, entries: list | None, sync: list | None) -> None:
        self._add_sync(r, sync)
        t_recv = round(self.now(), 6)
        index = {e["key"]: e for e in r["entries"] if e.get("key")}
        for raw in (entries or [])[:2000]:
            e = _clean_entry(raw)
            if e is None:
                continue
            old = index.get(e.get("key")) if e.get("key") else None
            if old is not None:   # a later update of the same cue (screen time, cancelled...)
                old.update(e)
                old["t_update_s"] = t_recv
                continue
            if len(r["entries"]) >= MAX_ENTRIES:
                continue
            e["t_recv_s"] = t_recv
            r["entries"].append(e)
            if e.get("key"):
                index[e["key"]] = e

    def _close(self, r: dict, status: str, reason: str | None) -> None:
        r["status"], r["status_reason"] = status, (reason or None) and str(reason)[:300]
        r["ended_t_s"] = round(self.now(), 6)

    def _save(self) -> None:
        folder = self.folder_fn()
        if folder is None or not Path(folder).exists():
            return
        doc = build_doc(self.runs)
        tmp = Path(folder) / (CUES_FILE + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(Path(folder) / CUES_FILE)
        tmp = Path(folder) / (CUES_CSV + ".tmp")
        tmp.write_text(to_csv(doc))
        tmp.replace(Path(folder) / CUES_CSV)


def _client(c: dict | None) -> dict:
    c = c if isinstance(c, dict) else {}
    out = {}
    for k in ("user_agent", "audio_method", "audio_base_latency_s", "audio_output_latency_s", "audio_sample_rate",
              "time_origin_ms", "screen_hz"):
        v = c.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
        elif isinstance(v, str):
            out[k] = v[:300]
    return out


def _session_times(r: dict) -> list[dict]:
    """Entries with t_s / screen_t_s on the session clock (from the run's clock fit)."""
    fit = r.get("clock_fit")
    out = []
    for e in r["entries"]:
        e = dict(e)
        e["t_s"] = to_session_s(e.get("perf_ms"), fit)
        if e.get("screen_perf_ms") is not None:
            e["screen_t_s"] = to_session_s(e["screen_perf_ms"], fit)
        out.append(e)
    out.sort(key=lambda e: (e["t_s"] is None, e["t_s"] if e["t_s"] is not None else e.get("t_recv_s", 0)))
    return out


def _steps(entries: list[dict], protocol: dict) -> list[dict]:
    """One row per step attempt: when it ran and whether it finished (done / skipped / interrupted)."""
    rows: dict[tuple, dict] = {}
    for e in entries:
        if e.get("step") is None or e.get("kind") not in ("step_start", "step_end", "cue") or e.get("cancelled"):
            continue   # (a step whose start was cancelled - paused just before it began - never ran)
        k = (e["step"], e.get("attempt", 0))
        row = rows.get(k)
        if row is None:
            st = protocol["steps"][e["step"]] if 0 <= e["step"] < len(protocol["steps"]) else {}
            row = rows[k] = {"step": e["step"], "attempt": e.get("attempt", 0), "step_id": st.get("id", e.get("step_id")),
                             "label": st.get("label"), "negative": st.get("negative"), "title": st.get("title"),
                             "t_start": None, "t_end": None, "status": "interrupted", "n_cues": 0, "n_cues_planned": len(st.get("cues", []))}
        if e["kind"] == "step_start":
            row["t_start"] = e["t_s"]
        elif e["kind"] == "step_end":
            row["t_end"] = e["t_s"]
            row["status"] = e.get("status") or "done"
        elif e["kind"] == "cue" and not e.get("cancelled"):
            row["n_cues"] += 1
    return sorted(rows.values(), key=lambda r: (r["t_start"] is None, r["t_start"] or 0))


def build_doc(runs: list[dict]) -> dict:
    out = []
    for r in runs:
        entries = _session_times(r)
        out.append({**{k: v for k, v in r.items() if k != "entries"}, "entries": entries,
                    "steps": _steps(entries, r["protocol"]),
                    "t_start": next((e["t_s"] for e in entries if e["kind"] == "run_start"), None),
                    "t_end": next((e["t_s"] for e in reversed(entries) if e["kind"] in ("run_end", "abort")), None)})
    return {"schema": SCHEMA,
            "clock": "t_s, screen_t_s, t_recv_s: seconds since the recording started (server clock) - the clock of "
                     "events.csv and of camera frame times (sglab.video.frame_times). t_s = when the wearer got the "
                     "cue (beep played / screen changed); perf_ms = the same moment on the browser clock.",
            "runs": out}


CSV_COLUMNS = ["run_id", "protocol_id", "run_status", "kind", "step", "attempt", "step_id", "label", "negative", "cue",
               "action", "text", "gaze", "tone", "t_s", "screen_t_s", "t_recv_s", "perf_ms", "screen_perf_ms",
               "late_ms", "cancelled", "status", "note"]


def to_csv(doc: dict) -> str:
    f = io.StringIO()
    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in doc.get("runs", []):
        steps = r["protocol"]["steps"]
        for e in r["entries"]:
            st = steps[e["step"]] if isinstance(e.get("step"), int) and 0 <= e["step"] < len(steps) else {}
            w.writerow({**e, "run_id": r["run_id"], "protocol_id": r["protocol_id"], "run_status": r["status"],
                        "label": e.get("label") or st.get("label"), "negative": st.get("negative")})
    return f.getvalue()


def read_doc(folder: Path) -> dict | None:
    p = Path(folder) / CUES_FILE
    if not p.exists():
        return None
    return json.loads(p.read_text())
