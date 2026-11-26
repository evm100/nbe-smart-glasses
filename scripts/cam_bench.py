#!/usr/bin/env python3
"""Measure the eye camera's frame rate, drops and timing jitter, straight from the board (USB) or
from a recording (any link, e.g. Wi-Fi).

USB, live (close the NBE gateway first: it holds the port). Works with the USB binary and the hybrid
firmware (it sends PING like the gateway, so a hybrid board moves its stream to USB first):

    python scripts/cam_bench.py --port /dev/ttyACM0 --seconds 30
    python scripts/cam_bench.py --port COM5 --cam "MODE=CIF WIN=160 CLK=1" --sweep-exp 100,200,320,400,600
    python scripts/cam_bench.py --port auto --cam "MODE=CIF CLK=1" --cam "MODE=SVGA WIN=480 CLK=1" --json out.jsonl

Each --cam is one run: "CAM <settings>" is sent, the stream restarts, the first --settle seconds are
ignored and the next --seconds measured. --sweep-exp repeats every --cam with each EXP= value.

A recording (Wi-Fi or USB; the server's session folder):

    python scripts/cam_bench.py --frames data/sessions/<id>/<role>_GLS03_cam.frames

What the numbers mean (per run):
    fps        frames received / time they span (device clock, t_us of the frames)
    period     median interval between received frames, ms; p99 / max interval
    jitter     standard deviation of the intervals that are ~1 period (no gap), ms
    missing    frames that should be there by the device timestamps: gaps > 1.5 periods, each worth
               round(gap / period) - 1 frames. Of these:
      link     ... had a seq but never arrived (seq gaps: the link / PC was too slow, PPG went first)
      sensor   ... never got a seq (the camera task was late and the driver replaced the frame)
    drop %     missing / (received + missing)
    brightness mean gray level of the frames and % of pixels >= 250 (exposure x gain x light)
    ppg        PPG samples/s received and samples missing (seq gaps) - must stay 0
    S line     the board's own counters at the end of the run (cam_sfps = sensor frames/s the driver
               delivered, cam_miss, cam_drop, tx_drop = PPG bytes lost, cam_tx_ms = slowest frame write)

Dependencies: pyserial (live mode) and this repo's nbe_gateway package (standard library only).
"""
from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nbe_gateway.wire import (REC_FRAME, REC_SAMPLES, REC_TEXT, EnvelopeDecoder, FRAME_HDR,  # noqa: E402
                              WireError, line_body, read_uvarint, split_records)

TARGET_FPS, ACCEPT_FPS, MAX_DROP_PCT = 50.0, 30.0, 1.0


# ---------------------------------------------------------------- analysis (pure, tested)
def analyze_frames(seq: list[int], t_us: list[int]) -> dict:
    """Frame timing from (seq, device t_us) of the frames received, in arrival order."""
    out = {"frames": len(seq)}
    if len(seq) < 3:
        return out
    pairs = sorted(set(zip(t_us, seq)))  # by capture time; duplicates (sent twice at a handover) once
    t = [p[0] for p in pairs]
    s = [p[1] for p in pairs]
    dts = [b - a for a, b in zip(t, t[1:]) if b > a]
    per = statistics.median(dts)
    missing = sum(max(0, round(d / per) - 1) for d in dts if d > 1.5 * per)
    link = sum(b - a - 1 for a, b in zip(s, s[1:]) if b > a + 1)
    near = [d for d in dts if d <= 1.5 * per]
    span = (t[-1] - t[0]) / 1e6
    dts_sorted = sorted(dts)
    out.update({
        "span_s": round(span, 2),
        "fps": round((len(t) - 1) / span, 2) if span > 0 else None,
        "period_ms": round(per / 1000, 2),
        "p99_ms": round(dts_sorted[min(len(dts_sorted) - 1, int(0.99 * len(dts_sorted)))] / 1000, 2),
        "max_ms": round(dts_sorted[-1] / 1000, 2),
        "jitter_ms": round(statistics.pstdev(near) / 1000, 3) if len(near) > 1 else 0.0,
        "missing": missing,
        "link_drops": min(link, missing) if missing else link,
        "sensor_miss": max(0, missing - link),
        "drop_pct": round(100.0 * missing / (len(t) + missing), 3),
    })
    return out


def analyze_ppg(seq: list[int], t_us: list[int]) -> dict:
    out = {"ppg_samples": len(seq)}
    if len(seq) < 2:
        return out
    s = sorted(set(seq))
    span = (max(t_us) - min(t_us)) / 1e6
    out["ppg_sps"] = round((len(s) - 1) / span, 1) if span > 0 else None
    out["ppg_missing"] = (s[-1] - s[0] + 1) - len(s)
    return out


def verdict(r: dict) -> str:
    fps, drop = r.get("fps") or 0, r.get("drop_pct", 100)
    if r.get("ppg_missing"):
        return "FAIL (PPG samples lost)"
    if fps >= TARGET_FPS * 0.98 and drop < MAX_DROP_PCT:
        return "target"
    if fps >= ACCEPT_FPS and drop < MAX_DROP_PCT:
        return "acceptable"
    return "below target"


# ---------------------------------------------------------------- record decoding
def decode_samples(body: bytes) -> tuple[list[int], list[int]]:
    """(seqs, t_us) of a samples record (frame.h srec; same as sglab.framing.decode_samples)."""
    n = body[0] | body[1] << 8
    seq, i = read_uvarint(body, 2)
    t, i = read_uvarint(body, i)
    dt_nom, i = read_uvarint(body, i)
    nch = body[i]
    i += 1
    for _ in range(nch):
        _, i = read_uvarint(body, i)
    seqs, ts = [seq], [t]
    for _ in range(n - 1):
        d, i = read_uvarint(body, i)
        t += dt_nom + ((d >> 1) ^ -(d & 1))
        for _c in range(nch):
            _, i = read_uvarint(body, i)
        seq += 1
        seqs.append(seq)
        ts.append(t)
    return seqs, ts


def kv(body: str) -> dict:
    return dict(p.split("=", 1) for p in body.split(",") if "=" in p)


class Collector:
    """Records from the USB byte stream -> what one run needs."""

    def __init__(self, save=None):
        self.dec = EnvelopeDecoder()
        self.save = save
        self.reset()
        self.headers: dict[str, dict] = {}
        self.events: list[str] = []
        self.status: dict = {}

    def reset(self):
        self.f_seq, self.f_t, self.f_bytes = [], [], 0
        self.lum, self.sat = [], []  # per gray8 frame: mean level, fraction of pixels >= 250
        self.p_seq, self.p_t = [], []

    def feed(self, data: bytes, keep: bool) -> None:
        for kind, item in self.dec.feed(data):
            if kind != "records":
                continue
            try:
                recs = split_records(item)
            except WireError:
                continue
            for _off, k, body in recs:
                if k == REC_FRAME and len(body) >= FRAME_HDR.size:
                    sid, seq, t_us, w, h, fmt, exp, gain = FRAME_HDR.unpack_from(body)
                    if keep:
                        self.f_seq.append(seq)
                        self.f_t.append(t_us)
                        self.f_bytes += len(body) - FRAME_HDR.size
                        if fmt == 0 and w * h:
                            px = body[FRAME_HDR.size:FRAME_HDR.size + w * h]
                            self.lum.append(sum(px) / len(px))
                            self.sat.append(sum(px.count(bytes([v])) for v in range(250, 256)) / len(px))
                        if self.save:
                            self.save.add(w, h, fmt, seq, t_us, exp, gain, body[FRAME_HDR.size:])
                elif k == REC_SAMPLES and keep:
                    try:
                        s, t = decode_samples(body)
                    except (IndexError, WireError):
                        continue
                    self.p_seq += s
                    self.p_t += t
                elif k == REC_TEXT:
                    b = line_body(body.decode("ascii", "replace"))
                    if not b:
                        continue
                    if b.startswith("H,"):
                        h = kv(b)
                        self.headers[h.get("stream", "ppg")] = h
                    elif b.startswith("E,"):
                        self.events.append(b)
                    elif b.startswith("S,"):
                        self.status = kv(b)


class FramesFile:
    """--save: the frames of the runs as an NBE .frames file (sglab.video format) for viewing."""

    def __init__(self, path: Path):
        self.path, self.f = path, None

    def add(self, w, h, fmt, seq, t_us, exp, gain, data):
        if self.f is None:
            self.f = open(self.path, "wb")
            self.f.write(struct.pack("<8sHHB7x", b"NBEVID01", w, h, fmt))
        self.f.write(struct.pack("<IIqdHBB", len(data), seq, t_us, time.time(), exp, gain, fmt) + data)

    def close(self):
        if self.f:
            self.f.close()


# ---------------------------------------------------------------- live (USB)
def open_port(port: str):
    import serial
    from serial.tools import list_ports

    if port == "auto":
        hits = [p.device for p in list_ports.comports() if p.vid == 0x303A]
        if not hits:
            sys.exit("no Espressif USB device found (is the board plugged in, the gateway closed?)")
        port = hits[0]
    return serial.Serial(port, 2000000, timeout=0.05), port


def pump(ser, col: Collector, seconds: float, keep: bool, ping: list) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if time.monotonic() - ping[0] >= 1.0:  # heartbeat: a hybrid board keeps its stream on USB
            ping[0] = time.monotonic()
            ser.write(b"PING\n")
        data = ser.read(max(1, ser.in_waiting or 0))
        if data:
            col.feed(data, keep)


def send(ser, cmd: str) -> None:
    ser.write((cmd + "\n").encode())
    ser.flush()


def live(args) -> list[dict]:
    ser, port = open_port(args.port)
    save = FramesFile(Path(args.save)) if args.save else None
    col, ping = Collector(save), [0.0]
    print(f"port {port}")
    send(ser, "PING")
    send(ser, "STOP")
    send(ser, "INFO")
    pump(ser, col, 2.0, False, ping)
    ppg = col.headers.get("ppg", {})
    if ppg.get("link") == "hybrid":
        print("hybrid board: waiting for it to hand its stream over to USB ...")
        t0 = time.monotonic()
        while not any(",LINK,usb" in e for e in col.events) and time.monotonic() - t0 < 15:
            pump(ser, col, 0.5, False, ping)
        if not any(",LINK,usb" in e for e in col.events):
            print("  no $E,LINK,usb within 15 s - measuring anyway (is it already on USB?)")
    if "cam" not in col.headers:
        print("warning: no camera H line yet (camera not found, or not a camera board?)")
    runs = [c for c in (args.cam or [""])]
    if args.sweep_exp:
        runs = [f"{c} EXP={e}".strip() for c in runs for e in args.sweep_exp.split(",")]
    results = []
    try:
        for cfg in runs:
            if cfg:
                send(ser, "CAM " + cfg)
            send(ser, "START")
            col.reset()
            pump(ser, col, args.settle, False, ping)
            col.status = {}
            pump(ser, col, args.seconds, True, ping)
            r = {"cam": cfg or "(as flashed)"}
            h = col.headers.get("cam", {})
            r["header"] = {k: h[k] for k in ("w", "h", "fmt", "mode", "win", "clkdiv", "fs", "fps_sensor", "line_us",
                                             "exp_max", "exposure", "gain", "ir") if k in h}
            r.update(analyze_frames(col.f_seq, col.f_t))
            r.update(analyze_ppg(col.p_seq, col.p_t))
            r["kB_s"] = round(col.f_bytes / 1000 / args.seconds, 1)
            if col.lum:
                r["mean_gray"] = round(statistics.mean(col.lum), 1)
                r["saturated_pct"] = round(100 * statistics.mean(col.sat), 2)
            r["S"] = {k: col.status[k] for k in ("cam_fps", "cam_sfps", "cam_miss", "cam_drop", "cam_capped", "cam_null",
                                                 "cam_wfail", "cam_wait_ms", "cam_tx_ms", "tx_drop") if k in col.status}
            r["verdict"] = verdict(r)
            results.append(r)
            report(r)
    finally:
        send(ser, "STOP")
        if save:
            save.close()
        ser.close()
    return results


# ---------------------------------------------------------------- recordings
def from_file(path: Path) -> dict:
    seq, t, nbytes = [], [], 0
    with open(path, "rb") as f:
        magic, w, h, fmt = struct.unpack("<8sHHB7x", f.read(struct.calcsize("<8sHHB7x")))
        if magic != b"NBEVID01":
            sys.exit(f"{path}: not an NBE .frames file")
        while True:
            hd = f.read(28)
            if len(hd) < 28:
                break
            n, s, t_us, _t_s, _exp, _gain, _fmt = struct.unpack("<IIqdHBB", hd)
            f.seek(n, 1)
            seq.append(s)
            t.append(t_us)
            nbytes += n
    r = {"file": str(path), "header": {"w": w, "h": h}}
    # a recording may hold several START epochs (seq restarts): analyse the longest
    epochs, cur = [], [0]
    for i in range(1, len(seq)):
        if seq[i] < seq[i - 1] - 1000 or t[i] < t[i - 1] - 1_000_000:
            epochs.append(cur)
            cur = []
        cur.append(i)
    epochs.append(cur)
    best = max(epochs, key=len)
    r.update(analyze_frames([seq[i] for i in best], [t[i] for i in best]))
    if r.get("span_s"):
        r["kB_s"] = round(nbytes * len(best) / max(1, len(seq)) / 1000 / r["span_s"], 1)
    r["verdict"] = verdict(r)
    report(r)
    return r


def report(r: dict) -> None:
    h = r.get("header", {})
    print(f"\n== {r.get('cam') or r.get('file')}  [{h.get('w')}x{h.get('h')} {h.get('mode', '')} win={h.get('win', '-')} "
          f"clk={h.get('clkdiv', '-')} exp={h.get('exposure', '-')} gain={h.get('gain', '-')}]")
    if r.get("frames", 0) < 3:
        print(f"   only {r.get('frames', 0)} frames received")
        return
    print(f"   {r['fps']} fps over {r['span_s']} s ({r['frames']} frames, {r.get('kB_s', '?')} kB/s) - {r['verdict']}")
    print(f"   period {r['period_ms']} ms, p99 {r['p99_ms']} ms, max {r['max_ms']} ms, jitter {r['jitter_ms']} ms")
    print(f"   missing {r['missing']} ({r['drop_pct']} %): link {r['link_drops']}, sensor/driver {r['sensor_miss']}")
    if "mean_gray" in r:
        print(f"   brightness: mean gray {r['mean_gray']} / 255, {r['saturated_pct']} % of pixels saturated")
    if "ppg_samples" in r:
        print(f"   ppg {r.get('ppg_sps')} samples/s, {r.get('ppg_missing', 0)} missing")
    if r.get("S"):
        print("   board: " + " ".join(f"{k}={v}" for k, v in r["S"].items()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", help="serial port, or 'auto' (first Espressif USB device)")
    ap.add_argument("--frames", type=Path, help="analyse a recorded .frames file instead")
    ap.add_argument("--cam", action="append", help='camera settings of one run, e.g. "MODE=CIF WIN=160 CLK=1 EXP=320"')
    ap.add_argument("--sweep-exp", help="comma-separated EXP values: every --cam run once per value")
    ap.add_argument("--seconds", type=float, default=20.0, help="measured time per run (default 20)")
    ap.add_argument("--settle", type=float, default=2.0, help="ignored time after each change (default 2)")
    ap.add_argument("--save", help="also write the measured frames to this .frames file")
    ap.add_argument("--json", help="append one JSON line per run to this file")
    a = ap.parse_args(argv)
    if a.frames:
        results = [from_file(a.frames)]
    elif a.port:
        results = live(a)
    else:
        ap.error("--port or --frames")
    if a.json:
        with open(a.json, "a") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
    return 0 if all(r["verdict"] in ("target", "acceptable") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
