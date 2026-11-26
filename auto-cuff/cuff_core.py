#!/usr/bin/env python3
"""
auto-cuff core: take one Omron BP7250 (HEM-7150T) reading and return the new record.

Flow for one measurement:
    1. Servo presses START (servo_cuff.ButtonPusher)
    2. Wait for the cuff to inflate/deflate
    3. Run omblepy repeatedly until the monitor (which advertises for a while
       after a reading) is reachable and the records download
    4. Compare against records already seen -> return only the new one(s)
    5. Press START again to switch the monitor off

If no new record has been downloaded NO_DATA_TIMEOUT_S (1.4 min) after the first
press, the reading is given up and START is pressed anyway. A cuff error (E on the
display) stores no record and leaves the monitor on; without this press the NEXT
reading's press would only switch it off instead of starting a measurement.

cuff_node.py (the MQTT node the server talks to) calls measure(). Settings are
environment variables (CUFF_*, below), e.g. in /etc/default/auto-cuff.

Layout expected:
    auto-cuff/
        .venv/
        servo_cuff.py
        cuff_core.py        <- this file
        cuff_node.py
        omblepy/            <- omblepy checkout (omblepy.py, deviceSpecific/)
        data/               <- created automatically (seen.json, readings.csv)

Bench test without the server (one reading, printed as JSON):
    .venv/bin/python cuff_core.py --once
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
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from servo_cuff import ButtonPusher  # noqa: E402

# ---- config (override with environment variables in the systemd unit) ----
OMBLEPY_DIR = Path(os.environ.get("CUFF_OMBLEPY_DIR", BASE / "omblepy"))
DATA_DIR = Path(os.environ.get("CUFF_DATA_DIR", BASE / "data"))
MAC = os.environ.get("CUFF_MAC", "28:FF:B2:31:CA:D5")
DEVICE = os.environ.get("CUFF_DEVICE", "HEM-7150T")
USER_SLOT = int(os.environ.get("CUFF_USER", "1"))           # omblepy writes user<N>.csv
INITIAL_WAIT_S = float(os.environ.get("CUFF_INITIAL_WAIT", "35"))  # cuff cycle before first read
# No new record this long after the first press -> give up and press START to switch the monitor
# off (it stays on after a cuff error). Counted from the press, so it includes INITIAL_WAIT_S.
NO_DATA_TIMEOUT_S = float(os.environ.get("CUFF_NO_DATA_TIMEOUT", "84"))  # 1.4 min
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


def download_records(abort=None, deadline=None):
    """Run omblepy once. Returns list of dict rows, raises on failure.

    abort: optional threading.Event; setting it kills omblepy and raises Aborted.
    deadline: optional time.monotonic() value; omblepy is killed then even if its own
    ATTEMPT_TIMEOUT_S has not run out."""
    _clean_omblepy_outputs()
    cmd = [sys.executable, "omblepy.py", "-d", DEVICE, "-m", MAC]
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(cmd, cwd=OMBLEPY_DIR, stdout=out, stderr=subprocess.STDOUT, text=True)
        attempt_deadline = time.monotonic() + ATTEMPT_TIMEOUT_S
        if deadline is not None:
            attempt_deadline = min(attempt_deadline, deadline)
        while proc.poll() is None:
            if abort is not None and abort.is_set():
                proc.kill()
                proc.wait()
                raise Aborted("aborted")
            if time.monotonic() > attempt_deadline:
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
        _press()
    except Exception:
        log.exception("power-off press failed (reading was still saved)")


def _reset_press(t0):
    """No new record: wait until NO_DATA_TIMEOUT_S after the first press (the cuff has finished
    or errored by then and the monitor is still on), then press START to switch it off.

    Returns a note for the error message. Always pressed, even with CUFF_POWER_OFF=0: a monitor
    left on would turn the next reading's press into a power-off."""
    remaining = t0 + NO_DATA_TIMEOUT_S - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)  # not abortable: pressing early could hit a cuff still measuring
    # Same settle time as the power-off press: omblepy was usually just killed mid-connection, and
    # the BLE teardown (radio + servo on the same supply) made an immediate press misbehave.
    time.sleep(POWER_OFF_DELAY_S)
    try:
        log.info("no new record %.0fs after the press: pressing START to switch the monitor off",
                 time.monotonic() - t0)
        _press()
        return "pressed START to switch the monitor off"
    except Exception as e:
        log.exception("reset press failed")
        return f"the press to switch the monitor off FAILED ({type(e).__name__}: {e}); check the monitor"


def _press():
    with ButtonPusher() as pusher:
        pusher.press()


def _download_new(seen, abort, deadline):
    """Download until a new record shows up. Returns its entries; raises MeasureError when the
    deadline passes (or the monitor answers twice with nothing new), Aborted on abort."""
    last_err = "no attempt made"
    empty_reads = 0
    attempt = 0
    while time.monotonic() < deadline:
        if abort.is_set():
            raise Aborted("aborted by the operator during the Bluetooth download")
        attempt += 1
        try:
            rows = download_records(abort, deadline)
        except Aborted:
            raise Aborted("aborted by the operator during the Bluetooth download") from None
        except MeasureError as e:
            last_err = str(e)
            log.warning("attempt %d: %s", attempt, last_err)
            abort.wait(min(3, max(0, deadline - time.monotonic())))
            continue

        new = seen.take_new(rows)
        if new:
            received_at = datetime.now().astimezone().isoformat(timespec="seconds")
            _append_log(new, received_at)
            entries = [_to_entry(r, received_at) for r in new]
            log.info("new reading(s): %s", entries)
            return entries

        empty_reads += 1
        last_err = "monitor read OK but no new record (measurement error on the cuff?)"
        log.warning("attempt %d: %s", attempt, last_err)
        if empty_reads >= MAX_EMPTY_READS:
            break
        abort.wait(min(5, max(0, deadline - time.monotonic())))

    if abort.is_set():
        raise Aborted("aborted by the operator during the Bluetooth download")
    raise MeasureError(f"no new reading within {NO_DATA_TIMEOUT_S:.0f} s of the press ({last_err})")


def measure(seen, on_phase=None, abort=None):
    """One reading. on_phase(phase, t_unix) is told "started" right after the press (the cuff is
    inflating), then "downloading" / "powering_off". abort: threading.Event -> raises Aborted.

    Whatever goes wrong after the press, the monitor is switched off before this returns: by the
    STOP press of an early abort, the power-off press after a good read, or the reset press at
    NO_DATA_TIMEOUT_S."""
    abort = abort or threading.Event()
    phase = on_phase or (lambda *_: None)
    t0 = time.monotonic()
    deadline = t0 + NO_DATA_TIMEOUT_S
    log.info("pressing START")
    _press()
    phase("started", time.time())

    log.info("waiting %.0fs for the cuff cycle", INITIAL_WAIT_S)
    if abort.wait(INITIAL_WAIT_S) and time.monotonic() - t0 <= ABORT_PRESS_WINDOW_S:
        log.info("abort: pressing START to stop the cuff")
        _press()  # this press stops and deflates the cuff; no reset press after it
        raise Aborted("aborted by the operator (STOP pressed, cuff deflating)")

    try:
        if abort.is_set():
            raise Aborted("aborted by the operator (too late to stop the cuff; it finished on its own)")
        phase("downloading", time.time())
        entries = _download_new(seen, abort, deadline)
    except Exception as e:  # Aborted, MeasureError, or anything unexpected (disk, omblepy crash, ...)
        failure = e
    else:
        phase("powering_off", time.time())
        _power_off()
        return entries

    phase("powering_off", time.time())
    note = _reset_press(t0)
    if isinstance(failure, MeasureError):  # Aborted included
        raise type(failure)(f"{failure}; {note}") from None
    raise MeasureError(f"{type(failure).__name__}: {failure}; {note}") from failure


def main():
    parser = argparse.ArgumentParser(description="auto-cuff: take one reading (bench test)")
    parser.add_argument("--once", action="store_true", help="take one reading, print it, exit")
    args = parser.parse_args()
    if not args.once:
        parser.error("this module only does --once; the service is cuff_node.py")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not (OMBLEPY_DIR / "omblepy.py").exists():
        sys.exit(f"omblepy.py not found in {OMBLEPY_DIR}")
    try:
        print(json.dumps(measure(SeenStore(STATE_FILE)), indent=2))
    except MeasureError as e:
        sys.exit(f"failed: {e}")


if __name__ == "__main__":
    main()
