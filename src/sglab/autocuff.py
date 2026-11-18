"""Automatic BP cuff ("auto-cuff") nodes: the server schedules readings during a trial.

An auto-cuff is a registered node with site = "cuff" (a Raspberry Pi or an ESP32 that presses
the monitor's START button with a servo and downloads the result over Bluetooth). It is not a
stream: it never appears in the trial's device list. Instead the operator attaches one to a
running trial on the live page, turns it on, and sets the interval. Contract: auto-cuff/PROTOCOL.md

    <prefix>/dev/<ID>/status  device -> server, retained JSON {"online", "kind": "cuff", "state", "phase", ...}
    <prefix>/dev/<ID>/cmd     server -> device: MEASURE <req> | ABORT | INFO
    <prefix>/dev/<ID>/data    device -> server, JSON {"type": "started" | "reading" | "failed" | "aborted", "req", ...}

The scheduler lives here, not on the device, so the device stays simple (same contract for the
Pi and the ESP32) and nothing is measured outside a trial. Readings go into the trial's events as
ordinary `cuff_start` / `bp` events (source "auto-cuff:<ID>"), so Layer 3 pairs them exactly like
typed ones.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

from .commands import CommandError, validate_event

log = logging.getLogger("sglab.autocuff")

CUFF_SITE = "cuff"
MIN_INTERVAL_S = 90.0     # a reading takes ~40-60 s plus the Bluetooth download
MAX_INTERVAL_S = 3600.0
DEFAULT_INTERVAL_S = 300.0
MIN_REST_S = 60.0         # at least this long between the end of one reading and the next press
ACK_TIMEOUT_S = 20.0      # MEASURE sent, no "started" back -> give up on this one
RESULT_TIMEOUT_S = 330.0  # "started", no result -> give up (the device's own timeout is 240 s)
MAX_CLOCK_SKEW_S = 600.0  # device timestamps further off than this are ignored (arrival time used)
TICK_S = 1.0


def is_cuff(node: dict) -> bool:
    return (node.get("site") or "").strip().lower() == CUFF_SITE


class Binding:
    """One cuff attached to one trial."""

    def __init__(self, tid: str, node_id: str, interval_s: float):
        self.tid, self.node_id, self.interval_s = tid, node_id, interval_s
        self.enabled = False
        self.next_due: float | None = None     # monotonic time of the next scheduled press
        self.req: str | None = None            # request in flight
        self.issued: list[str] = []            # every request sent (a late result of a timed-out one still counts)
        self.sent_at = 0.0                     # monotonic, MEASURE published
        self.started_at: float | None = None   # monotonic, device said "started"
        self.last_start: float | None = None   # monotonic press time of the latest reading
        self.phase = "idle"                    # idle | requested | measuring
        self.last_reading: dict | None = None
        self.last_error: str | None = None
        self.n_ok = self.n_fail = 0
        self.trial_open = True                 # False once the trial stopped (late results still logged)

    def busy(self) -> bool:
        return self.req is not None

    def view(self, now: float) -> dict:
        return {"node_id": self.node_id, "enabled": self.enabled, "interval_s": self.interval_s,
                "phase": self.phase, "req": self.req, "n_ok": self.n_ok, "n_fail": self.n_fail,
                "next_in_s": round(max(0.0, self.next_due - now), 1) if self.enabled and self.next_due and not self.busy() else None,
                "measuring_for_s": round(now - self.started_at, 1) if self.started_at and self.busy() else None,
                "last_reading": self.last_reading, "last_error": self.last_error}


class CuffManager:
    def __init__(self, hub, registry, manager):
        """hub: MqttHub (or None: no cuffs), registry: NodeRegistry, manager: TrialManager."""
        self.hub, self.registry, self.manager = hub, registry, manager
        self.lock = threading.RLock()
        self.bindings: dict[str, Binding] = {}  # node_id -> binding
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if hub is not None:
            hub.on_json = self.on_message

    # ---- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="autocuff", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # pragma: no cover - never let the scheduler die
                log.exception("auto-cuff tick")

    # ---- queries --------------------------------------------------------------------
    def cuffs(self) -> list[dict]:
        """Every enabled cuff node with its live state and which trial (if any) has it."""
        out = []
        with self.lock:
            owner = {n: b.tid for n, b in self.bindings.items()}
        for n in self.registry.nodes():
            if not n["enabled"] or not is_cuff(n):
                continue
            st = self.hub.node_state(n["node_id"]) if self.hub else {"online": False, "state": "MQTT off"}
            out.append({"node_id": n["node_id"], "label": n["name"] or n["node_id"], "online": bool(st.get("online")),
                        "state": st.get("state"), "cuff_state": st.get("cuff_state"),
                        "phase": st.get("phase"), "fw": st.get("fw"), "monitor": st.get("monitor"),
                        "last_error": st.get("last_error"), "trial": owner.get(n["node_id"])})
        return out

    def trial_state(self, tid: str) -> dict:
        now = time.monotonic()
        with self.lock:
            b = next((b for b in self.bindings.values() if b.tid == tid), None)
            view = b.view(now) if b else None
        cuffs = self.cuffs()
        if view:
            dev = next((c for c in cuffs if c["node_id"] == view["node_id"]), None)
            view["device"] = dev
        return {"binding": view, "cuffs": cuffs, "limits": {"min_interval_s": MIN_INTERVAL_S,
                "max_interval_s": MAX_INTERVAL_S, "default_interval_s": DEFAULT_INTERVAL_S, "min_rest_s": MIN_REST_S}}

    # ---- operator actions -------------------------------------------------------------
    def configure(self, tid: str, node_id: str | None = None, enabled: bool | None = None,
                  interval_s: float | None = None) -> dict:
        """Attach a cuff to a trial (node_id), switch automatic readings on/off, change the interval."""
        from .control import TrialError

        t = self.manager.get(tid)
        if t.state != "recording":
            raise TrialError("not recording")
        if interval_s is not None:
            try:
                interval_s = float(interval_s)
            except (TypeError, ValueError):
                raise TrialError("interval must be a number of seconds") from None
            if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
                raise TrialError(f"interval must be {MIN_INTERVAL_S / 60:g}-{MAX_INTERVAL_S / 60:g} min")
        with self.lock:
            b = next((b for b in self.bindings.values() if b.tid == tid), None)
            if node_id:
                node_id = node_id.strip().upper()
                if b is None or b.node_id != node_id:
                    if b is not None and b.busy():
                        raise TrialError(f"{b.node_id} is measuring - wait for it before switching cuffs")
                    n = self.registry.node(node_id)
                    if not n or not n["enabled"] or not is_cuff(n):
                        raise TrialError(f"{node_id} is not an enabled auto-cuff (site 'cuff')")
                    other = self.bindings.get(node_id)
                    if other is not None and other.tid != tid:
                        raise TrialError(f"{node_id} is in use by another trial")
                    if b is not None:
                        self.bindings.pop(b.node_id, None)
                    b = Binding(tid, node_id, interval_s or DEFAULT_INTERVAL_S)
                    self.bindings[node_id] = b
            if b is None:
                raise TrialError("choose an auto-cuff first")
            if interval_s is not None and interval_s != b.interval_s:
                b.interval_s = interval_s
                if b.enabled and not b.busy():
                    b.next_due = self._due_after(b, time.monotonic())
            if enabled is not None and bool(enabled) != b.enabled:
                b.enabled = bool(enabled)
                if b.enabled:
                    b.next_due = self._due_after(b, time.monotonic(), first=True)
                    self._note(tid, f"auto-cuff {b.node_id} on, every {b.interval_s / 60:g} min")
                else:
                    b.next_due = None
                    self._note(tid, f"auto-cuff {b.node_id} off")
            return b.view(time.monotonic())

    def measure_now(self, tid: str) -> dict:
        from .control import TrialError

        with self.lock:
            b = self._binding(tid)
            if b.busy():
                raise TrialError(f"{b.node_id} is already measuring")
            err = self._send_measure(b, time.monotonic())
            if err:
                raise TrialError(err)
            return b.view(time.monotonic())

    def abort(self, tid: str) -> dict:
        """Stop the reading in progress (the device presses STOP if the cuff is inflating)."""
        from .control import TrialError

        with self.lock:
            b = self._binding(tid)
            if not b.busy():
                raise TrialError("no reading in progress")
            self.hub.publish_cmd(b.node_id, "ABORT")
            self._note(tid, f"auto-cuff {b.node_id}: abort requested")
            return b.view(time.monotonic())

    def detach(self, tid: str) -> None:
        with self.lock:
            b = next((b for b in self.bindings.values() if b.tid == tid), None)
            if b is not None:
                self.bindings.pop(b.node_id, None)

    # ---- TrialManager hooks -----------------------------------------------------------
    def trial_stopped(self, tid: str) -> None:
        """No more presses; a reading already running is still logged until the trial is saved."""
        with self.lock:
            for b in list(self.bindings.values()):
                if b.tid == tid:
                    b.enabled, b.next_due, b.trial_open = False, None, False
                    if not b.busy():
                        self.bindings.pop(b.node_id, None)

    def trial_closed(self, tid: str) -> None:
        self.detach(tid)

    # ---- scheduler --------------------------------------------------------------------
    def _binding(self, tid: str) -> Binding:
        from .control import TrialError

        b = next((b for b in self.bindings.values() if b.tid == tid), None)
        if b is None:
            raise TrialError("no auto-cuff attached to this trial")
        return b

    @staticmethod
    def _due_after(b: Binding, now: float, first: bool = False) -> float:
        """Readings are spaced start to start; switching on with no reading yet measures right away."""
        if b.last_start is None:
            return now if first else now + b.interval_s
        return max(b.last_start + b.interval_s, now)

    def _send_measure(self, b: Binding, now: float) -> str | None:
        if self.hub is None or not self.hub.connected:
            return "the server is not connected to the MQTT broker"
        st = self.hub.node_state(b.node_id)
        if not st.get("online"):
            return f"{b.node_id} is {st.get('state', 'offline')}"
        if st.get("cuff_state") == "measuring":
            return f"{b.node_id} reports it is already measuring"
        b.req = secrets.token_hex(3).upper()
        b.issued = (b.issued + [b.req])[-20:]
        b.sent_at, b.started_at, b.phase = now, None, "requested"
        self.hub.publish_cmd(b.node_id, f"MEASURE {b.req}")
        return None

    def tick(self) -> None:
        now = time.monotonic()
        with self.lock:
            for b in list(self.bindings.values()):
                if b.busy():
                    if b.started_at is None and now - b.sent_at > ACK_TIMEOUT_S:
                        self._finish(b, now, error="no answer from the cuff (offline? check its power / network)")
                    elif b.started_at is not None and now - b.started_at > RESULT_TIMEOUT_S:
                        self._finish(b, now, error="no result within "
                                     f"{RESULT_TIMEOUT_S / 60:.1f} min")
                    continue
                if not b.trial_open:
                    self.bindings.pop(b.node_id, None)
                    continue
                if b.enabled and b.next_due is not None and now >= b.next_due:
                    err = self._send_measure(b, now)
                    if err:
                        b.last_error = err
                        b.next_due = now + 30.0  # try again shortly; the operator sees why
                        b.phase = "waiting"

    def _finish(self, b: Binding, now: float, error: str | None = None, reading: dict | None = None) -> None:
        if error:
            b.n_fail += 1
            b.last_error = error
            self._note(b.tid, f"auto-cuff {b.node_id}: reading failed - {error}", late_ok=True)
        if reading:
            b.n_ok += 1
            b.last_reading, b.last_error = reading, None
        b.req, b.started_at, b.phase = None, None, "idle"
        if b.enabled:
            b.next_due = max((b.last_start or now) + b.interval_s, now + MIN_REST_S)
        if not b.trial_open:
            self.bindings.pop(b.node_id, None)

    # ---- device messages ----------------------------------------------------------------
    def on_message(self, node_id: str, msg: dict) -> None:
        """A JSON payload on <prefix>/dev/<ID>/data (MqttHub.on_json)."""
        kind = msg.get("type")
        now = time.monotonic()
        with self.lock:
            b = self.bindings.get(node_id)
            if b is None:
                log.info("auto-cuff %s: %s outside a trial, ignored", node_id, kind)
                return
            req = str(msg.get("req") or "").upper()
            if req != (b.req or "") and req not in b.issued:
                log.info("auto-cuff %s: %s for unknown request %r (now %s), ignored", node_id, kind, req, b.req)
                return
            if b.req is not None and req and req != b.req:
                log.info("auto-cuff %s: %s for old request %s (now %s), ignored", node_id, kind, req, b.req)
                return
            if kind == "started":
                if b.req is None:  # a request we had given up on is running after all: follow it
                    b.req, b.sent_at = req, now
                b.started_at, b.phase = now, "measuring"
                b.last_start = now - self._age(msg)
                self._event(b, "cuff_start", msg, {"label": f"auto-cuff {node_id}"})
            elif kind == "reading":
                values = {"sys_mmhg": msg.get("sys"), "dia_mmhg": msg.get("dia"), "hr_bpm": msg.get("bpm")}
                try:
                    ev = validate_event("bp", values)
                except CommandError as e:
                    self._finish(b, now, error=f"implausible reading {values}: {e}")
                    return
                extra = [f"auto-cuff {node_id}"]
                if msg.get("device_time"):
                    extra.append(f"monitor {msg['device_time']}")
                for k in ("mov", "ihb"):
                    if msg.get(k) is not None:
                        extra.append(f"{k}={int(msg[k])}")
                ev.pop("kind", None)
                logged = self._event(b, "bp", msg, {**ev, "label": " · ".join(extra)})
                reading = {"sys": ev["sys_mmhg"], "dia": ev["dia_mmhg"], "hr": ev.get("hr_bpm"),
                           "mov": msg.get("mov"), "ihb": msg.get("ihb"), "t_s": logged.get("t_s") if logged else None}
                self._finish(b, now, reading=reading)
            elif kind in ("failed", "aborted"):
                self._finish(b, now, error=str(msg.get("error") or kind))
            else:
                log.info("auto-cuff %s: unknown message %r", node_id, kind)

    @staticmethod
    def _age(msg: dict) -> float:
        """Seconds since the device's t_unix (when it happened), if its clock looks right."""
        try:
            age = time.time() - float(msg["t_unix"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        return age if 0.0 <= age <= MAX_CLOCK_SKEW_S else 0.0

    def _event(self, b: Binding, kind: str, msg: dict, values: dict) -> dict | None:
        try:
            t = self.manager.get(b.tid)
        except Exception:
            return None
        with t.lock:
            if t.rec is None or t.state not in ("recording", "stopped"):
                return None
            ev = t.rec.add_event(kind, **values, source=f"auto-cuff:{b.node_id}")
            age = self._age(msg)
            if age:
                ev["t_s"] = round(max(0.0, ev["t_s"] - age), 3)
            return ev

    def _note(self, tid: str, label: str, late_ok: bool = False) -> None:
        try:
            t = self.manager.get(tid)
        except Exception:
            return
        with t.lock:
            if t.rec is not None and (t.state == "recording" or (late_ok and t.state == "stopped")):
                t.rec.add_event("marker", label=label, source="auto-cuff")
