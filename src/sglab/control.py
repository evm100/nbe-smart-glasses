"""Trials controlled from the web UI (or any Python code). Several can run at once.

Per trial:   recording --stop()--> stopped --finalize()--> saved (removed from the manager)
                                               (keep = no  -> saved with status=discard; nothing is deleted)

Each device (serial port, simulator or MQTT node) can be in at most one
running trial; the claim is taken before the device is opened and released
when the trial stops.
"""
from __future__ import annotations

import contextlib
import secrets
import threading
import time

import numpy as np

from .catalog import SUBJECT_ID_RE, Catalog
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
        self.analysis = None  # liveppg.LiveAnalysis while recording
        self.cues = None      # cues.CueLog once a cued run was started on the live page

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
        self.cuffs = None  # autocuff.CuffManager, set by the web app when MQTT is on

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
                if not SUBJECT_ID_RE.fullmatch(pre["subject_id"]):
                    raise TrialError(f"subject ID must be a code like {cat.next_subject_id()} – no names")
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
            from .liveppg import LiveAnalysis
            trial.analysis = LiveAnalysis(trial, self.settings)
            trial.analysis.start()
        except Exception as e:
            self._release(trial, drop=True)
            if isinstance(e, TrialError):
                raise
            raise TrialError(f"could not start: {type(e).__name__}: {e}") from e
        return trial

    def _release(self, trial: Trial, drop: bool = False) -> None:
        if drop and self.cuffs is not None:
            self.cuffs.trial_closed(trial.id)
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
            if t.cues is not None:  # a cued run still playing is kept, marked partial
                t.cues.close_all("the recording was stopped before the cued run ended")
            if t.analysis is not None:
                t.analysis.stop_flag.set()
        if self.cuffs is not None:
            self.cuffs.trial_stopped(tid)
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
                if t.cues is not None:
                    t.cues.close_all("the recording was saved before the cued run ended")
                with t.cues.lock if t.cues is not None else contextlib.nullcontext():  # no cue write mid-move
                    dest, _ = t.rec.finalize(meta)
                sid = cat.upsert_session(dest)
            finally:
                cat.close()
            t.state = "saved"
        if self.cuffs is not None:
            self.cuffs.trial_closed(tid)
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

    # ---- cued sessions (sglab.cues) -------------------------------------------------
    def cue_clock(self, tid: str, arrived_ns: int | None = None) -> dict:
        """One clock ping for the live page: t1_s = when the request arrived, t2_s = now (t_s)."""
        t = self.get(tid)
        rec = t.rec
        if rec is None or t.state != "recording":
            raise TrialError("not recording")
        t2 = rec.now()
        t1 = (arrived_ns - rec.t0_ns) / 1e9 if arrived_ns else t2
        return {"t1_s": round(min(t1, t2), 6), "t2_s": round(t2, 6)}

    def _cue_log(self, t: Trial):
        from .cues import CueLog
        if t.cues is None:
            t.cues = CueLog(lambda: t.rec.folder if t.rec else None, lambda: t.rec.now())
        return t.cues

    def cue_start(self, tid: str, protocol: dict, operator: str | None, client: dict | None,
                  sync: list | None) -> dict:
        t = self.get(tid)
        with t.lock:
            if t.state != "recording":
                raise TrialError("not recording")
            run = self._cue_log(t).start(protocol, operator, client, sync)
            t.rec.add_event("marker", label=f"cued session started: {protocol['name']}")
        return run

    def _cue_call(self, t: Trial, run_id: str, fn):
        if t.cues is None:
            raise TrialError(f"no cued run {run_id}")
        try:
            return fn(t.cues)
        except KeyError:
            raise TrialError(f"no cued run {run_id}") from None
        except ValueError as e:
            raise TrialError(str(e)) from None

    def cue_log(self, tid: str, run_id: str, entries: list | None, sync: list | None) -> dict:
        t = self.get(tid)
        return self._cue_call(t, run_id, lambda c: c.log(run_id, entries, sync))

    def cue_end(self, tid: str, run_id: str, status: str, reason: str | None, entries: list | None,
                sync: list | None) -> dict:
        t = self.get(tid)
        was = self._cue_call(t, run_id, lambda c: c.run(run_id)["status"])
        run = self._cue_call(t, run_id, lambda c: c.end(run_id, status, reason, entries, sync))
        with t.lock:
            if was == "running" and t.state == "recording":
                t.rec.add_event("marker", label=f"cued session {run['status']}: {run['protocol_name']}")
        return run

    def cue_doc(self, tid: str) -> dict:
        from .cues import build_doc
        t = self.get(tid)
        if t.cues is None:
            return build_doc([])
        with t.cues.lock:
            return build_doc(t.cues.runs)

    # ---- live data ------------------------------------------------------------------
    def status(self, tid: str) -> dict:
        t = self.get(tid)
        with t.lock:
            out = {**t.brief(), "metadata": t.pre, "warnings": t.warnings}
            if t.rec is not None:
                out.update(folder=t.rec.folder.name, streams=t.rec.live_status(), events=t.rec.all_events())
            return out

    def _video_writer(self, tid: str, stream: str):
        t = self.get(tid)
        with t.lock:
            workers = list(t.rec.workers) if t.rec else []
        for w in workers:
            for vw in list(w.video.values()):
                if vw.name == stream:
                    return vw
        raise TrialError(f"no camera stream {stream!r} in trial {tid}")

    def eye(self, tid: str, stream: str, after: int = -1) -> dict | None:
        return self._video_writer(tid, stream).eye(after)

    def eye_overlay(self, tid: str, stream: str, count: int) -> dict | None:
        return self._video_writer(tid, stream).overlay(count)

    def eye_recalibrate(self, tid: str, stream: str) -> bool:
        return self._video_writer(tid, stream).recalibrate_eye()

    def aux_live(self, tid: str, after: dict[str, int] | None = None) -> list[dict]:
        """Live state of every auxiliary stream (auxstream.AuxWriter.live_state); `after` = stream -> cursor."""
        t = self.get(tid)
        with t.lock:
            workers = list(t.rec.workers) if t.rec else []
        after = after or {}
        return [{**aw.live_state(after.get(aw.name, -1)), "role": w.role} for w in workers for aw in list(w.aux.values())]

    def insights(self, tid: str) -> dict:
        """Live features: per PPG stream (HR, HRV, quality, beats, template) + the BP estimate."""
        t = self.get(tid)
        a = t.analysis
        if a is None:
            return {"ppg": [], "bp": {"state": "waiting"}}
        return {"ppg": [lp.state() for lp in list(a.ppg.values())], "bp": a.bp_state()}

    def set_bp_model(self, tid: str, key: str | None, role: str | None = None) -> None:
        t = self.get(tid)
        if t.analysis is None:
            raise TrialError("not recording")
        t.analysis.set_model(key, role)

    def video_preview(self, tid: str, stream: str, after: int = -1,
                      wait_s: float = 0.0) -> tuple[int, bytes, str] | None:
        """(frame count, image, media type) of a camera stream's newest frame, None before the
        first; waits up to wait_s for one newer than `after` (VideoWriter.preview)."""
        t = self.get(tid)
        with t.lock:
            workers = list(t.rec.workers) if t.rec else []
        for w in workers:
            for vw in list(w.video.values()):
                if vw.name == stream:
                    return vw.preview(after, wait_s)
        raise TrialError(f"no camera stream {stream!r} in trial {tid}")

    def camera_settings(self, tid: str, stream: str, req: dict) -> str:
        """Change a camera while recording: {"exposure", "gain", "aec", "mode", "win", "fps", "ir"} (any subset)
        -> the board's "CAM ..." command (protocol v2). The board answers with fresh H lines, so the
        recording keeps the new settings; the change is also logged as an event. Returns the command."""
        t = self.get(tid)
        with t.lock:
            if t.state != "recording" or t.rec is None:
                raise TrialError("not recording")
            owner = next(((w, vw) for w in t.rec.workers for vw in list(w.video.values()) if vw.name == stream), None)
            if owner is None:
                raise TrialError(f"no camera stream {stream!r} in trial {tid}")
            w, vw = owner
            cur = vw.camera_settings()

            def num(k, lo, hi):
                try:
                    v = int(req[k])
                except (TypeError, ValueError):
                    raise TrialError(f"{k} must be a whole number") from None
                if isinstance(req[k], bool) or not lo <= v <= hi:
                    raise TrialError(f"{k} must be {lo}-{hi}")
                return v

            parts, label = [], []
            if req.get("aec") is True:
                parts.append("AEC=1")
                label.append("auto exposure")
            else:  # EXP / GAIN switch the board to fixed exposure by themselves
                if req.get("exposure") is not None:
                    v = num("exposure", 0, 1200)
                    parts.append(f"EXP={v}")
                    label.append(f"exposure {v}")
                if req.get("gain") is not None:
                    v = num("gain", 0, 30)
                    parts.append(f"GAIN={v}")
                    label.append(f"gain {v}")
                if req.get("aec") is False and not parts:
                    parts.append("AEC=0")
                    label.append("fixed exposure")
            if req.get("mode") is not None:  # sensor mode of the window (firmware >= 2026-11): cif ~50 fps, svga ~25
                m = str(req["mode"]).lower()
                if m not in ("cif", "svga") or not cur["win_ok"]:
                    raise TrialError("mode must be cif or svga (grayscale streams only)")
                parts.append(f"MODE={m.upper()}")
                label.append(f"{m.upper()} mode")
                if m != cur.get("mode"):  # the window limits are the other mode's (the board checks them too)
                    cur = dict(cur, win_max=296 if m == "cif" else 600)
            if req.get("win") is not None:
                if not cur["win_ok"]:
                    raise TrialError("the window (FOV) can only be changed on grayscale streams")
                v = num("win", 0, cur["win_max"])
                if v and v < cur["win_min"]:
                    raise TrialError(f"window must be 0 (full view) or {cur['win_min']}-{cur['win_max']}")
                v &= ~3  # the OV2640 window registers count in 4s (the board rounds the same way)
                parts.append(f"WIN={v}")
                label.append("full view" if v == 0 else f"window {v}")
            if req.get("fps") is not None:
                v = num("fps", 0, 120)
                parts.append(f"FPS={v}")
                label.append(f"{v} fps cap" if v else "no fps cap")
            if req.get("ir") is not None:  # IR illuminator brightness, % (firmware CONFIG_PPG_CAM_IR_GPIO)
                v = num("ir", 0, 100)
                parts.append(f"IR={v}")
                label.append(f"IR LED {v} %")
            if not parts:
                raise TrialError("nothing to change")
            cmd = "CAM " + " ".join(parts)
            w.src.write(cmd)
            t.rec.add_event("camera", label=f"camera {stream}: " + ", ".join(label), stream=stream, command=cmd)
            return cmd

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
