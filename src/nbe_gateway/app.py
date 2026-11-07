"""NBE USB gateway: a sensor board on this computer's USB, streamed to the NBE server as its node.

    python -m nbe_gateway                    uses gateway.json next to this package's folder
    python -m nbe_gateway --config FILE
    python -m nbe_gateway --port COM5        instead of finding the board by its USB id
    python -m nbe_gateway --list-ports
    python -m nbe_gateway --verbose          MQTT / TLS details when it can't connect

Exit status 3: shut down from the server (GW-EXIT; another gateway for the node needed it).

gateway.json (written by the server's "USB gateway" download):
    {"node_id": "GLS03", "broker_host": "mqtt.example.org", "broker_port": 8884, "topic_prefix": "nbe",
     "cert_file": "certs/client.crt", "key_file": "certs/client.key", "ca_file": null,
     "serial_port": "auto", "data_dir": "data", "keep_gb": 20}
Relative paths are relative to the folder holding gateway.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .store import RecordLog
from .uplink import Uplink
from .usb import Board, find_ports


EXIT_SHUT_DOWN = 3  # nbe-gateway.service: RestartPreventExitStatus=3


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    base = path.parent
    for k in ("cert_file", "key_file", "ca_file", "data_dir"):
        v = cfg.get(k)
        if v and not os.path.isabs(v):
            cfg[k] = str(base / v)
    if not cfg.get("ca_file"):
        bundled = base / "certs" / "ca_bundle.pem"  # public CAs (Python on macOS has none of its own)
        if bundled.exists():
            cfg["ca_file"] = str(bundled)
    cfg.setdefault("data_dir", str(base / "data"))
    return cfg


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


class Gateway:
    def __init__(self, cfg: dict, board_open=None, mqtt_client=None):
        self.cfg = cfg
        self.done = threading.Event()  # set to quit (Ctrl+C, or the server's GW-EXIT)
        self.node = cfg["node_id"]
        self.log = RecordLog(Path(cfg["data_dir"]) / self.node, float(cfg.get("keep_gb", 20)))
        self.board = Board(self.log, self.node, cfg.get("serial_port", "auto"), on_change=self._changed,
                           open_serial=board_open)
        self.uplink = Uplink(cfg, self.log, self._command, self.board.describe, client=mqtt_client,
                             version=__version__, on_exit=self.done.set)

    def _changed(self) -> None:
        self.uplink.notify()

    def _command(self, cmd: str) -> None:
        c = cmd.strip()
        if c.upper().startswith("OTA"):
            logging.getLogger("nbe_gateway").warning("OTA over a USB gateway is not supported - flash over USB")
            return
        if not self.board.write(c):
            logging.getLogger("nbe_gateway").info("command %r: board not connected", c)

    def start(self) -> None:
        threading.Thread(target=self.board.run, name="usb", daemon=True).start()
        self.uplink.start()

    def stop(self) -> None:
        self.board.write("STOP")
        self.board.stop()
        self.uplink.stop()
        self.log.close()

    def status_line(self) -> str:
        b, u, d = self.board, self.uplink, self.board.describe()
        usb = f"USB {b.port}" if b.connected else "USB: no board (plug it in)"
        if b.connected and d.get("fw"):
            usb += f" fw {d['fw']}"
        stream = "STREAMING" if d.get("streaming") else "idle"
        st = d.get("status", {})
        cam = st.get("cam_fps")
        cam = f" cam {cam} fps" if cam is not None else ""
        if "cam_grab" in st:  # diagnostic firmware: where the frames go on the board
            cam += (f" (grabbed {st['cam_grab']}, capped {st.get('cam_capped')}, sent {st.get('cam_sent')},"
                    f" usb fail {st.get('cam_wfail')}, driver timeouts {st.get('cam_null')},"
                    f" wait max {st.get('cam_wait_ms')} ms, usb max {st.get('cam_tx_ms')} ms)")
        srv = "server: connected" if u.connected else f"server: {u.last_error or 'connecting...'}"
        return (f"[{time.strftime('%H:%M:%S')}] {self.node} | {usb} | {stream}{cam} | frames {b.stats['frames']}"
                f" | up {_fmt_bytes(u.rate_bps)}/s | waiting to send {_fmt_bytes(self.log.unacked())}"
                f" | stored {_fmt_bytes(self.log.disk_bytes())} | {srv}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nbe_gateway", description="NBE USB gateway")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--port", help="serial port (default: the first Espressif USB device)")
    ap.add_argument("--list-ports", action="store_true")
    ap.add_argument("--every", type=float, default=5.0, help="seconds between status lines")
    ap.add_argument("--verbose", action="store_true", help="show MQTT / TLS details (connection problems)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    # paho logs every failed reconnect as an error; the status line already says "cannot reach"
    logging.getLogger("nbe_gateway.paho").setLevel(logging.DEBUG if a.verbose else logging.CRITICAL)

    if a.list_ports:
        for p in find_ports():
            print(f"{p['port']:<20} {'ESP32  ' if p['esp32'] else '       '} {p['description']}")
        return 0
    path = a.config or Path(__file__).resolve().parent.parent / "gateway.json"
    if not path.exists():
        path = Path.cwd() / "gateway.json"
    if not path.exists():
        print("gateway.json not found - download the gateway for your node from the NBE server "
              "(Nodes page -> USB gateway) and run it from that folder.", file=sys.stderr)
        return 2
    cfg = load_config(path)
    if a.port:
        cfg["serial_port"] = a.port
    gw = Gateway(cfg)
    print(f"NBE USB gateway {__version__} for {cfg['node_id']} -> {cfg['broker_host']}:{cfg.get('broker_port', 8883)}")
    print(f"Keeping a copy of everything in {Path(cfg['data_dir']) / cfg['node_id']}")
    print("Leave this window open while recording. Ctrl+C (or close the window) to quit.\n")
    gw.start()
    done = gw.done
    signal.signal(signal.SIGINT, lambda *_: done.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: done.set())
    shown = 0
    logged = 0
    try:
        while not done.wait(a.every):
            print(gw.status_line(), flush=True)
            for w in gw.board.warnings[shown:]:
                print("  ! " + w, flush=True)
            shown = len(gw.board.warnings)
            b = gw.board
            new = b.board_log_n - logged
            if new > len(b.board_log):
                print(f"  board: ({new - len(b.board_log)} older log lines not shown)", flush=True)
            for ln in b.board_log[-new:] if new > 0 else []:
                print("  board " + ln, flush=True)
            logged = b.board_log_n
    finally:
        if gw.uplink.exit_requested:
            print("\nShut down from the NBE server: another gateway for this node needed it "
                  "(only one can run per node). Start it again when you record next.", flush=True)
        print("stopping...")
        gw.stop()
    return EXIT_SHUT_DOWN if gw.uplink.exit_requested else 0
