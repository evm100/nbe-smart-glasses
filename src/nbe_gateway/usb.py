"""The board on USB: read envelopes/lines, append records to the log, pass commands down.

Works with both firmware kinds:
  - USB binary link (protocol v2, camera boards): envelopes of records, stored as they are
  - USB text link (protocol v1): "$...*CS" lines, stored as text records
so any board flashed for USB can sit behind a gateway.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from .store import RecordLog
from .wire import (REC_FRAME, REC_TEXT, EnvelopeDecoder, WireError, line, line_body, split_records, text_record)

ESPRESSIF_VID = 0x303A
SYNC_EVERY_S = 10.0


def find_ports() -> List[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover
        return []
    out = []
    for p in list_ports.comports():
        if p.vid is None and (p.description or "n/a") == "n/a":  # Linux lists every legacy ttyS* slot
            continue
        out.append({"port": p.device, "description": p.description or "", "vid": p.vid, "pid": p.pid,
                    "esp32": p.vid == ESPRESSIF_VID})
    return out


def auto_port() -> Optional[str]:
    hits = [p["port"] for p in find_ports() if p["esp32"]]
    return hits[0] if hits else None


class Board:
    """One USB board. run() loops forever: (re)connect, read, store."""

    def __init__(self, log: RecordLog, node_id: str, port: str = "auto", baud: int = 2000000,
                 on_change: Optional[Callable[[], None]] = None, open_serial=None):
        self.log, self.node_id, self.port_cfg, self.baud = log, node_id, port, baud
        self.on_change = on_change or (lambda: None)
        self._open_serial = open_serial or self._default_open
        self.ser = None
        self.port: Optional[str] = None
        self.connected = False
        self.stop_flag = threading.Event()
        self.wlock = threading.Lock()
        self.decoder = EnvelopeDecoder()
        # what the board said about itself (H / E / S lines)
        self.headers: Dict[str, dict] = {}   # stream name -> key/values of its H line
        self.streaming = False
        self.last_status: dict = {}
        self.last_status_t_us: Optional[int] = None
        self.last_sync = 0.0
        self.warnings: List[str] = []
        self.stats = {"bytes_in": 0, "records": 0, "frames": 0, "text_lines": 0, "log_lines": 0,
                      "reconnects": 0, "frame_seq_gaps": 0}
        self._last_frame_seq: Dict[int, int] = {}
        self.device_log: List[str] = []      # recent non-protocol text (boot messages)

    # ---------------------------------------------------------------- serial
    def _default_open(self, port: str):
        import serial

        try:
            return serial.Serial(port, self.baud, timeout=0.05, exclusive=True)
        except (ValueError, TypeError):
            return serial.Serial(port, self.baud, timeout=0.05)

    def write(self, cmd: str) -> bool:
        with self.wlock:
            if not self.ser:
                return False
            try:
                self.ser.write((cmd.strip() + "\n").encode())
                self.ser.flush()
                return True
            except Exception:
                return False

    def run(self) -> None:
        while not self.stop_flag.is_set():
            port = self.port_cfg if self.port_cfg not in ("", "auto") else auto_port()
            if not port:
                self._set_connected(False)
                time.sleep(1.0)
                continue
            try:
                ser = self._open_serial(port)
            except Exception as e:
                self._warn(f"cannot open {port}: {e}")
                self._set_connected(False)
                time.sleep(2.0)
                continue
            with self.wlock:
                self.ser, self.port = ser, port
            self.decoder = EnvelopeDecoder()
            self._set_connected(True)
            self.write("INFO")  # headers now, so the server knows the streams before START
            try:
                while not self.stop_flag.is_set():
                    data = ser.read(max(1, getattr(ser, "in_waiting", 0) or 0) or 1)
                    if data:
                        self.feed(data)
                    else:
                        self.log.flush()
            except Exception as e:  # unplugged, reset, ...
                self._warn(f"USB {port}: {type(e).__name__}: {e}")
            finally:
                with self.wlock:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    self.ser = None
                self.stats["reconnects"] += 1
                self._set_connected(False)
            time.sleep(0.5)

    def stop(self) -> None:
        self.stop_flag.set()

    def _set_connected(self, up: bool) -> None:
        if up != self.connected:
            self.connected = up
            if not up:
                self.streaming = False
            self.on_change()

    def _warn(self, msg: str) -> None:
        if not self.warnings or self.warnings[-1] != msg:
            self.warnings = (self.warnings + [msg])[-20:]

    # ---------------------------------------------------------------- data
    def feed(self, data: bytes) -> None:
        """Bytes from the board -> records in the log (also used by tests)."""
        self.stats["bytes_in"] += len(data)
        for kind, item in self.decoder.feed(data):
            if kind == "records":
                self._store_records(item)
            else:
                body = line_body(item)
                if body is None:  # boot banner / log text: keep a few for the console
                    self.stats["log_lines"] += 1
                    self.device_log = (self.device_log + [item])[-20:]
                    continue
                self._store_records(text_record(item))

    def _store_records(self, recs: bytes) -> None:
        try:
            parts = split_records(recs)
        except WireError:
            return
        ends = [p[0] for p in parts[1:]] + [len(recs)]  # each record ends where the next begins
        self.log.append(recs, ends)
        self.stats["records"] += len(parts)
        extra = b""
        extra_ends: List[int] = []
        for _off, kind, body in parts:
            if kind == REC_TEXT:
                self.stats["text_lines"] += 1
                sync = self._note_line(body.decode("ascii", "replace"))
                if sync:
                    r = text_record(sync)
                    extra += r
                    extra_ends.append(len(extra))
            elif kind == REC_FRAME and len(body) >= 5:
                self.stats["frames"] += 1
                sid, seq = body[0], int.from_bytes(body[1:5], "little")
                last = self._last_frame_seq.get(sid)
                if last is not None and seq > last + 1:
                    self.stats["frame_seq_gaps"] += seq - last - 1
                self._last_frame_seq[sid] = seq
        if extra:
            self.log.append(extra, extra_ends)

    def _note_line(self, raw: str) -> Optional[str]:
        """Track the board's state from its own lines; returns a SYNC line to add, if due."""
        body = line_body(raw)
        if body is None:
            return None
        parts = body.split(",")
        kind = parts[0]
        if kind == "H":
            kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            name = kv.get("stream", "ppg")
            self.headers[name] = kv
            dev = kv.get("dev")
            if dev and dev != self.node_id:
                self._warn(f"the board says it is {dev}, but this gateway is {self.node_id} - "
                           "wrong board plugged in? (data is sent as " + self.node_id + ")")
            self.on_change()
        elif kind == "E" and len(parts) >= 3:
            code = parts[2]
            if code == "START":
                self.streaming = True
                self._last_frame_seq.clear()
                self.on_change()
            elif code in ("STOP", "BOOT"):
                self.streaming = False
                self.on_change()
        elif kind == "S" and len(parts) >= 2:
            try:
                t_us = int(parts[1])
            except ValueError:
                return None
            self.last_status = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
            self.last_status_t_us = t_us
            now = time.time()
            # the board has no clock of its own: pair its time with this PC's (NTP-synced) clock,
            # like Wi-Fi nodes do with SNTP. USB latency is ~ms; the S line is written right away.
            if self.streaming and now - self.last_sync >= SYNC_EVERY_S:
                self.last_sync = now
                return line(f"E,{t_us},SYNC,{int(now * 1000)}")
        return None

    # ---------------------------------------------------------------- summary
    def describe(self) -> dict:
        ppg = self.headers.get("ppg", {})
        cam = self.headers.get("cam", {})
        sensors = [x for x in (ppg.get("sensor"), cam.get("sensor")) if x]
        return {"usb": "connected" if self.connected else "disconnected", "port": self.port,
                "fw": ppg.get("fw") or cam.get("fw"), "dev": ppg.get("dev") or cam.get("dev"),
                "sensor": "+".join(sensors) if sensors else None,
                "sensor_ok": ppg.get("sensor_ok") == "1" if ppg else None,
                "camera_ok": cam.get("sensor_ok") == "1" if cam else None,
                "fs": float(ppg["fs"]) if ppg.get("fs") else None,
                "streaming": self.streaming, "status": self.last_status}

