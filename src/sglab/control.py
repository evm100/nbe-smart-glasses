"""Trials controlled from the web UI (or any Python code). Several can run at once.

Per trial:   recording --stop()--> stopped --finalize()--> saved (removed from the manager)
                                               (keep = no  -> saved with status=discard; nothing is deleted)

Each device (serial port, simulator or MQTT node) can be in at most one
running trial; the claim is taken before the device is opened and released
when the trial stops.
"""
from __future__ import annotations

import secrets
import threading
import time

import numpy as np

from .catalog import Catalog
from .commands import validate_event
from .devices import DeviceError
from .metadata import _convert, load_state, save_state
from .recorder import Recording


class TrialError(RuntimeError):
    pass


def form_to_meta(fields: list[dict], phase: str, form: dict, skip: set[str] = frozenset()) -> dict:
    """Convert submitted form values for one phase; raise TrialError on bad/missing values."""
    out, errors = {}, []
    for f in fields:
        if f.get("ask", "before") != phase or f["name"] in skip:
            continue
        name = f["name"]
        raw = form.get(name)
        if f.get("type") == "bool":
            raw = "y" if str(raw).lower() in ("y", "yes", "true", "on", "1") else "n"
        try:
            val = _convert(f, raw.strip() if isinstance(raw, str) else raw)
        except ValueError:
            errors.append(f"{f.get('prompt', name)}: expected {f.get('type')}")
            continue
        if val is None and f.get("required", True) and f.get("type") != "bool":
            errors.append(f"{f.get('prompt', name)} is required")
        if f.get("type") == "choice" and val is not None and val not in f.get("choices", []):
            errors.append(f"{f.get('prompt', name)}: {val!r} is not an option")
        out[name] = val
    if errors:
        raise TrialError("; ".join(errors))
    return out


class Trial:
    def __init__(self, tid: str, owner: str, device_keys: list[str], devices: list[dict], pre: dict):
        self.id, self.owner, self.device_keys, self.devices, self.pre = tid, owner, device_keys, devices, pre
        self.ports = [d["port"] for d in devices]
        self.lock = threading.RLock()
        self.rec: Recording | None = None
        self.state = "starting"
        self.warnings: list[str] = []
        self.started_wall = time.time()

    def brief(self) -> dict:
        return {"id": self.id, "owner": self.owner, "state": self.state, "device_keys": self.device_keys,
                "devices": [d.get("label") or d["port"] for d in self.devices],
                "subject_id": self.pre.get("subject_id"), "protocol": self.pre.get("protocol"),
                "trial": self.pre.get("trial"), "started": self.started_wall,
                "elapsed_s": round(self.rec.now(), 1) if self.rec and self.state == "recording" else None}


class TrialManager:
    def __init__(self, settings, directory):
        """directory: devices.DeviceDirectory (resolves form keys to recorder devices)."""
        self.settings, self.directory = settings, directory
        self.lock = threading.Lock()  # guards trials + claims only; recording I/O happens outside it
        self.trials: dict[str, Trial] = {}
        self.claims: dict[str, str] = {}  # port -> trial id
        self.finished: dict[str, str] = {}  # trial id -> session id (so a late page load can redirect)

    def _cat(self) -> Catalog:
        return Catalog(self.settings.db_path, self.settings.data_root)

    # ---- queries ------------------------------------------------------------------
    def busy(self) -> dict[str, dict]:
        with self.lock:
            return {p: {"trial": t, "owner": self.trials[t].owner} for p, t in self.claims.items() if t in self.trials}

    def active(self) -> list[dict]:
        with self.lock:
            trials = list(self.trials.values())
        return [t.brief() for t in sorted(trials, key=lambda t: t.started_wall)]

    def get(self, tid: str) -> Trial:
        with self.lock:
            t = self.trials.get(tid)
        if t is None:
            raise TrialError(f"no active trial {tid}")
        return t

    def last_values(self, user: str | None) -> dict:
        st = load_state(self.settings.state_path)
        return st.get("last_by_user", {}).get(user, {}) if user else st.get("last", {})

    # ---- lifecycle ------------------------------------------------------------------
    def start(self, device_keys: list[str], pre: dict, new_subject: dict | None = None, owner: str = "local") -> Trial:
        if not device_keys:
            raise TrialError("select at least one device")
        try:
            devices = [self.directory.resolve(k) for k in dict.fromkeys(device_keys)]
        except DeviceError as e:
            raise TrialError(str(e)) from None
        cat = self._cat()
        try:
            if new_subject and pre.get("subject_id"):
                if cat.subject(pre["subject_id"]) is not None:
                    raise TrialError(f"subject {pre['subject_id']} already exists – pick it from the list")
            elif pre.get("subject_id") and cat.subject(pre["subject_id"]) is None:
                raise TrialError(f"unknown subject {pre['subject_id']} – choose 'new subject' to create it")
        finally:
            cat.close()
        trial = Trial(secrets.token_hex(4), owner, list(dict.fromkeys(device_keys)), devices, pre)
        with self.lock:
            taken = [d["label"] for d in devices if d["port"] in self.claims]
            if taken:
                raise TrialError(f"already in use by another trial: {', '.join(taken)}")
            for p in trial.ports:
                self.claims[p] = trial.id
            self.trials[trial.id] = trial
        try:
            if new_subject:
                cat = self._cat()
                try:
                    cat.upsert_subject({**new_subject, "subject_id": pre["subject_id"]})
                finally:
                    cat.close()
            rec = Recording(self.settings, devices, sim_kw={"realtime": True})
            with trial.lock:
                trial.warnings = rec.start({**pre, "recorded_by": owner})
                trial.rec, trial.state = rec, "recording"
        except Exception as e:
            self._release(trial, drop=True)
            if isinstance(e, TrialError):
                raise
            raise TrialError(f"could not start: {type(e).__name__}: {e}") from e
        return trial

    def _release(self, trial: Trial, drop: bool = False) -> None:
        with self.lock:
            for p in trial.ports:
                if self.claims.get(p) == trial.id:
                    del self.claims[p]
            if drop:
                self.trials.pop(trial.id, None)

    def add_event(self, tid: str, kind: str, values: dict) -> dict:
        t = self.get(tid)
        with t.lock:
            if t.state != "recording":
                raise TrialError("not recording")
            return t.rec.add_event(**validate_event(kind, values))

    def stop(self, tid: str) -> None:
        t = self.get(tid)
        with t.lock:
            if t.state != "recording":
                raise TrialError("not recording")
            t.rec.stop()
            t.state = "stopped"
        self._release(t)  # devices are free for the next trial while this one is being saved

    def finalize(self, tid: str, post: dict, user: str | None = None) -> str:
        t = self.get(tid)
        with t.lock:
            if t.state != "stopped":
                raise TrialError("stop the recording first")
            meta = {**t.pre, **post, "recorded_by": t.owner}
            if user and user != t.owner:
                meta["finished_by"] = user
            state = load_state(self.settings.state_path)
            last = state.setdefault("last_by_user", {}).setdefault(t.owner, {})
            for f in self.settings.fields:
                if f.get("remember") and meta.get(f["name"]) is not None:
                    last[f["name"]] = meta[f["name"]]
            last["_devices"] = t.device_keys
            save_state(self.settings.state_path, state)
            cat = self._cat()
            try:
                t.rec.subject_snapshot = cat.subject(meta.get("subject_id")) if meta.get("subject_id") else None
                dest, _ = t.rec.finalize(meta)
                sid = cat.upsert_session(dest)
            finally:
                cat.close()
            t.state = "saved"
        with self.lock:
            self.trials.pop(tid, None)
            self.finished[tid] = sid
        return sid

    def shutdown(self) -> None:
        """Server stopping: stop running trials so files are flushed (they stay in raw/_incoming
        for `sglab recover`)."""
        for b in self.active():
            try:
                self.stop(b["id"])
            except TrialError:
                pass

    # ---- live data ------------------------------------------------------------------
    def status(self, tid: str) -> dict:
        t = self.get(tid)
        with t.lock:
            out = {**t.brief(), "metadata": t.pre, "warnings": t.warnings}
            if t.rec is not None:
                out.update(folder=t.rec.folder.name, streams=t.rec.live_status(), events=t.rec.all_events())
            return out

    def live(self, tid: str, seconds: float = 8.0, max_points: int = 800, after: list[int] | None = None) -> list[dict]:
        """Last few seconds of each stream's first channel for the live plot: decimated to
        max_points, or with `after` (each stream's `next` from the previous call) every newer point."""
        t = self.get(tid)
        with t.lock:
            if t.rec is None:
                return []
            out, now = [], t.rec.now()
            for i, w in enumerate(t.rec.workers):
                buf = list(w.live)
                base = {"stream": w.stream_name or w.role, "role": w.role, "now": round(now, 3),
                        "next": int(buf[-1][2]) + 1 if buf else (after[i] if after and i < len(after) else 0),
                        "fs": w.header.fs if w.header else None,
                        "channel": w.header.channels[0] if w.header else ""}
                if not buf:
                    out.append({**base, "t": [], "y": []})
                    continue
                a = np.array(buf, dtype=float)
                if after is not None and i < len(after):
                    a = a[a[:, 2] >= after[i]]
                a = a[a[:, 0] >= now - seconds]
                if after is None:
                    a = a[:: max(1, len(a) // max_points)]
                out.append({**base, "t": [round(float(x), 4) for x in a[:, 0]], "y": [float(x) for x in a[:, 1]]})
            return out
