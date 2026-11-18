#!/usr/bin/env python3
"""
auto-cuff: trigger an Omron BP7250 (HEM-7150T) reading over HTTP and get the
new record back as JSON.

Flow for one measurement:
    1. Servo presses START (servo_cuff.ButtonPusher)
    2. Wait for the cuff to inflate/deflate
    3. Run omblepy repeatedly until the monitor (which advertises for a while
       after a reading) is reachable and the records download
    4. Compare against records already seen -> return only the new one(s)

Layout expected:
    auto-cuff/
        .venv/
        servo_cuff.py
        cuff_server.py      <- this file
        omblepy/            <- omblepy checkout (omblepy.py, deviceSpecific/)
        data/               <- created automatically (seen.json, readings.csv)

HTTP API (default port 8080):
    GET  /health                -> {"ok": true, "busy": false}
    POST /measure               -> blocks ~1-2 min, returns the new reading
    GET  /readings?limit=20     -> most recent logged readings

Test without the server:
    .venv/bin/python cuff_server.py --once
"""

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from servo_cuff import ButtonPusher  # noqa: E402

# ---- config (override with environment variables in the systemd unit) ----
OMBLEPY_DIR = Path(os.environ.get("CUFF_OMBLEPY_DIR", BASE / "omblepy"))
DATA_DIR = Path(os.environ.get("CUFF_DATA_DIR", BASE / "data"))
MAC = os.environ.get("CUFF_MAC", "28:FF:B2:31:CA:D5")
DEVICE = os.environ.get("CUFF_DEVICE", "HEM-7150T")
USER_SLOT = int(os.environ.get("CUFF_USER", "1"))           # omblepy writes user<N>.csv
HOST = os.environ.get("CUFF_HOST", "0.0.0.0")
PORT = int(os.environ.get("CUFF_PORT", "8080"))
TOKEN = os.environ.get("CUFF_TOKEN", "")                     # optional bearer token
INITIAL_WAIT_S = float(os.environ.get("CUFF_INITIAL_WAIT", "35"))  # cuff cycle before first read
OVERALL_TIMEOUT_S = float(os.environ.get("CUFF_TIMEOUT", "240"))   # give up after this
ATTEMPT_TIMEOUT_S = float(os.environ.get("CUFF_ATTEMPT_TIMEOUT", "90"))
MAX_EMPTY_READS = 2   # successful downloads with no new record before giving up
POWER_OFF = os.environ.get("CUFF_POWER_OFF", "1") == "1"         # press START again after a good read
POWER_OFF_DELAY_S = float(os.environ.get("CUFF_POWER_OFF_DELAY", "2"))  # let BLE disconnect settle
# An abort presses START again (which stops an inflating cuff) only this soon after the first press.
# Later the measurement may already be over, and a press would start a NEW one instead.
ABORT_PRESS_WINDOW_S = float(os.environ.get("CUFF_ABORT_PRESS_WINDOW", "25"))

STATE_FILE = DATA_DIR / "seen.json"
LOG_CSV = DATA_DIR / "readings.csv"
LOG_FIELDS = ["received_at", "device_time", "sys", "dia", "bpm", "mov", "ihb"]

log = logging.getLogger("auto-cuff")


class MeasureError(Exception):
    pass


class Aborted(MeasureError):
    pass


# --------------------------------------------------------------------------
# Duplicate filtering
# --------------------------------------------------------------------------
def record_key(row):
    return f"{row['datetime']}|{row['sys']}|{row['dia']}|{row['bpm']}"


class SeenStore:
    """Keys of every record already downloaded from the monitor."""

    def __init__(self, path):
        self.path = path
        self.initialized = path.exists()
        self.keys = set()
        if self.initialized:
            self.keys = set(json.loads(path.read_text()).get("keys", []))

    def take_new(self, rows):
        """Return rows not seen before, and mark everything as seen.

        On the very first run there is no history, so every stored record would
        look new. In that case only the most recent record is returned.
        """
        new = [r for r in rows if record_key(r) not in self.keys]
        if not self.initialized and new:
            new = [max(new, key=lambda r: r["datetime"])]
        self.keys.update(record_key(r) for r in rows)
        self.initialized = True
        self._save()
        return sorted(new, key=lambda r: r["datetime"])

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": sorted(self.keys)}))
        tmp.replace(self.path)


# --------------------------------------------------------------------------
# omblepy wrapper
# --------------------------------------------------------------------------
def _clean_omblepy_outputs():
    # omblepy merges into an existing user<N>.csv and writes a backup each run;
    # deleting first means the CSV holds exactly what is on the monitor.
    for pattern in ("user*.csv", "backup_user*.csv", "ubpm.json"):
        for p in OMBLEPY_DIR.glob(pattern):
            p.unlink(missing_ok=True)


def download_records(abort=None):
    """Run omblepy once. Returns list of dict rows, raises on failure.

    abort: optional threading.Event; setting it kills omblepy and raises Aborted."""
    _clean_omblepy_outputs()
    cmd = [sys.executable, "omblepy.py", "-d", DEVICE, "-m", MAC]
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(cmd, cwd=OMBLEPY_DIR, stdout=out, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + ATTEMPT_TIMEOUT_S
        while proc.poll() is None:
            if abort is not None and abort.is_set():
                proc.kill()
                proc.wait()
                raise Aborted("aborted")
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise MeasureError("omblepy timed out")
            time.sleep(0.3)
        out.seek(0)
        output = out.read()

    csv_path = OMBLEPY_DIR / f"user{USER_SLOT}.csv"
    if proc.returncode != 0 or not csv_path.exists():
        tail = output.strip().splitlines()[-1:] or ["no output"]
        raise MeasureError(f"omblepy failed (rc={proc.returncode}): {tail[0]}")

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    _clean_omblepy_outputs()
    return rows


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
_busy = threading.Lock()


def _append_log(rows, received_at):
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(_to_entry(r, received_at))


def _to_entry(row, received_at):
    return {
        "received_at": received_at,       # Pi clock (NTP-synced)
        "device_time": row["datetime"],   # monitor clock (may drift)
        "sys": int(row["sys"]),
        "dia": int(row["dia"]),
        "bpm": int(row["bpm"]),
        "mov": int(row["mov"]),
        "ihb": int(row["ihb"]),
    }


def _power_off():
    """Press START once more to switch the monitor off.

    Only called after a successful download, when the monitor is known to be on.
    A failure here is logged but never loses the reading.
    """
    if not POWER_OFF:
        return
    try:
        time.sleep(POWER_OFF_DELAY_S)
        log.info("pressing START to power off")
        with ButtonPusher() as pusher:
            pusher.press()
    except Exception:
        log.exception("power-off press failed (reading was still saved)")


def _press():
    with ButtonPusher() as pusher:
        pusher.press()


def measure(seen, on_phase=None, abort=None):
    """One reading. on_phase(phase, t_unix) is told "started" right after the press (the cuff is
    inflating), then "downloading" / "powering_off". abort: threading.Event -> raises Aborted."""
    abort = abort or threading.Event()
    phase = on_phase or (lambda *_: None)
    t0 = time.monotonic()
    log.info("pressing START")
    _press()
    phase("started", time.time())

    log.info("waiting %.0fs for the cuff cycle", INITIAL_WAIT_S)
    if abort.wait(INITIAL_WAIT_S):
        if time.monotonic() - t0 <= ABORT_PRESS_WINDOW_S:
            log.info("abort: pressing START to stop the cuff")
            _press()
            raise Aborted("aborted by the operator (STOP pressed, cuff deflating)")
        raise Aborted("aborted by the operator (too late to stop the cuff safely; it finishes on its own)")

    last_err = "no attempt made"
    empty_reads = 0
    attempt = 0
    phase("downloading", time.time())
    while time.monotonic() - t0 < OVERALL_TIMEOUT_S:
        if abort.is_set():
            raise Aborted("aborted by the operator during the Bluetooth download")
        attempt += 1
        try:
            rows = download_records(abort)
        except Aborted:
            raise Aborted("aborted by the operator during the Bluetooth download") from None
        except MeasureError as e:
            last_err = str(e)
            log.warning("attempt %d: %s", attempt, last_err)
            abort.wait(3)
            continue

        new = seen.take_new(rows)
        if new:
            received_at = datetime.now().astimezone().isoformat(timespec="seconds")
            _append_log(new, received_at)
            entries = [_to_entry(r, received_at) for r in new]
            log.info("new reading(s): %s", entries)
            phase("powering_off", time.time())
            _power_off()
            return entries

        empty_reads += 1
        last_err = "monitor read OK but no new record (measurement error on the cuff?)"
        log.warning("attempt %d: %s", attempt, last_err)
        if empty_reads >= MAX_EMPTY_READS:
            break
        abort.wait(5)

    raise MeasureError(last_err)


def recent_readings(limit):
    if not LOG_CSV.exists():
        return []
    with LOG_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[-limit:]


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
def make_handler(seen):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not TOKEN:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {TOKEN}":
                return True
            self._send(401, {"ok": False, "error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._authorized():
                return
            url = urlparse(self.path)
            if url.path == "/health":
                self._send(200, {"ok": True, "busy": _busy.locked()})
            elif url.path == "/readings":
                limit = int(parse_qs(url.query).get("limit", ["20"])[0])
                self._send(200, {"ok": True, "readings": recent_readings(limit)})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            if urlparse(self.path).path != "/measure":
                self._send(404, {"ok": False, "error": "not found"})
                return
            if not _busy.acquire(blocking=False):
                self._send(409, {"ok": False, "error": "measurement already running"})
                return
            try:
                entries = measure(seen)
                self._send(200, {"ok": True, "reading": entries[-1], "new_records": entries})
            except MeasureError as e:
                self._send(504, {"ok": False, "error": str(e)})
            except Exception as e:  # servo/GPIO or anything unexpected
                log.exception("measurement crashed")
                self._send(500, {"ok": False, "error": repr(e)})
            finally:
                _busy.release()

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="auto-cuff BP service")
    parser.add_argument("--once", action="store_true", help="take one reading, print it, exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not (OMBLEPY_DIR / "omblepy.py").exists():
        sys.exit(f"omblepy.py not found in {OMBLEPY_DIR}")

    seen = SeenStore(STATE_FILE)

    if args.once:
        try:
            print(json.dumps(measure(seen), indent=2))
        except MeasureError as e:
            sys.exit(f"failed: {e}")
        return

    server = ThreadingHTTPServer((HOST, PORT), make_handler(seen))
    log.info("listening on %s:%d (device %s, mac %s)", HOST, PORT, DEVICE, MAC)
    server.serve_forever()


if __name__ == "__main__":
    main()
