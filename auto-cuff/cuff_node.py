#!/usr/bin/env python3
"""
auto-cuff MQTT node: the NBE server schedules readings, this Pi takes them.

The Pi connects to the lab's MQTT broker AS a registered node (client certificate,
CN = node ID, site "cuff"), exactly like the ESP32 sensor boards. The contract
(topics, commands, messages) is in PROTOCOL.md; an ESP32 version speaks the same one.

    status (retained, every 15 s)  {"online": true, "kind": "cuff", "cuff_state": "idle"|"measuring", ...}
    cmd                            MEASURE <req> | ABORT | INFO
    data (JSON, QoS 1)             {"type": "started"|"reading"|"failed"|"aborted", "req": ..., ...}

Credentials: on the server, Admin -> Nodes -> register e.g. CUF01 with site "cuff",
download "credentials.zip", unzip it into auto-cuff/node/ (keeps main/certs/ and
sdkconfig.nbe). Nothing else to configure: broker address and topic prefix come
from sdkconfig.nbe.

Run:      .venv/bin/python cuff_node.py            (systemd: systemd/auto-cuff.service)
Check:    .venv/bin/python cuff_node.py --check    (prints the config, connects, exits)
"""

import argparse
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

VERSION = "auto-cuff-pi 0.1.0"
NODE_DIR = Path(os.environ.get("CUFF_NODE_DIR", BASE / "node"))
STATUS_EVERY_S = 15.0
ALPN = "mqtt"

log = logging.getLogger("auto-cuff")


# --------------------------------------------------------------------------
# Config from the server's credentials zip
# --------------------------------------------------------------------------
def read_sdkconfig(path):
    vals = {}
    for line in path.read_text().splitlines():
        m = re.match(r'^CONFIG_(\w+)="(.*)"\s*$', line)
        if m:
            vals[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
    return vals


def load_config(node_dir=NODE_DIR):
    sdk = node_dir / "sdkconfig.nbe"
    certs = node_dir / "main" / "certs"
    missing = [p for p in (sdk, certs / "client.crt", certs / "client.key") if not p.exists()]
    if missing:
        raise SystemExit("missing " + ", ".join(str(p) for p in missing) +
                         f"\n-> unzip the node's credentials.zip from the server into {node_dir}")
    v = read_sdkconfig(sdk)
    uri = urlparse(v.get("PPG_MQTT_BROKER_URI", ""))
    if not uri.hostname:
        raise SystemExit(f"no PPG_MQTT_BROKER_URI in {sdk}")
    ca = certs / "broker_ca.crt"
    return {
        "node_id": v["PPG_DEVICE_ID"],
        "site": v.get("PPG_SITE", ""),
        "host": uri.hostname,
        "port": uri.port or 8883,
        "prefix": v.get("PPG_MQTT_TOPIC_PREFIX", "nbe").rstrip("/"),
        "cert_file": str(certs / "client.crt"),
        "key_file": str(certs / "client.key"),
        "ca_file": str(ca) if ca.exists() else None,
    }


def tls_context(cfg):
    ctx = ssl.create_default_context(cafile=cfg["ca_file"])
    # private device CAs lack some extensions Python >= 3.13 insists on (same as nbe_gateway)
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    ctx.load_cert_chain(cfg["cert_file"], cfg["key_file"])
    ctx.set_alpn_protocols([ALPN])
    return ctx


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------
class CuffNode:
    def __init__(self, cfg):
        import paho.mqtt.client as mqtt
        import cuff_server as cs  # servo + omblepy; imported here so --check works off the Pi

        self.cs = cs
        self.cfg = cfg
        self.node = cfg["node_id"]
        p = cfg["prefix"]
        self.t_status = f"{p}/dev/{self.node}/status"
        self.t_data = f"{p}/dev/{self.node}/data"
        self.t_cmd = f"{p}/dev/{self.node}/cmd"
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.state, self.phase, self.req = "idle", None, None
        self.last_error, self.last_reading = None, None
        self.abort = threading.Event()
        cs.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not (cs.OMBLEPY_DIR / "omblepy.py").exists():
            raise SystemExit(f"omblepy.py not found in {cs.OMBLEPY_DIR} (git submodule update --init)")
        self.seen = cs.SeenStore(cs.STATE_FILE)
        self.stop_flag = threading.Event()

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.node, clean_session=True)
        c.tls_set_context(tls_context(cfg))
        c.will_set(self.t_status, json.dumps({"online": False, "kind": "cuff"}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = lambda *a, **k: log.warning("MQTT disconnected")
        c.reconnect_delay_set(1, 30)
        self.client = c

    # ---- MQTT -------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            log.error("broker refused the connection: %s", reason_code)
            return
        log.info("connected to %s:%s as %s", self.cfg["host"], self.cfg["port"], self.node)
        client.subscribe(self.t_cmd, qos=1)
        self.publish_status()

    def _on_message(self, client, userdata, msg):
        text = msg.payload.decode("utf-8", "replace").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.upper()
        log.info("command: %s", text)
        if cmd == "MEASURE":
            self.start_measure(arg.strip() or None)
        elif cmd == "ABORT":
            if self.state == "measuring":
                self.abort.set()
        elif cmd == "INFO":
            self.publish_status()
        # anything else (ACK/START/STOP meant for sensor nodes) is ignored

    def send(self, msg):
        self.client.publish(self.t_data, json.dumps(msg), qos=1)

    def publish_status(self):
        with self.lock:
            st = {"online": True, "kind": "cuff", "fw": VERSION, "cuff_state": self.state, "phase": self.phase,
                  "req": self.req, "monitor": self.cs.DEVICE, "monitor_mac": self.cs.MAC,
                  "host": socket.gethostname(), "ts": int(time.time()),
                  "uptime_s": int(time.monotonic() - self.t0), "last_error": self.last_error,
                  "last_reading": self.last_reading}
        self.client.publish(self.t_status, json.dumps(st), qos=1, retain=True)

    # ---- measuring --------------------------------------------------------
    def start_measure(self, req):
        with self.lock:
            if self.state == "measuring":
                busy = True
            else:
                busy = False
                self.state, self.phase, self.req = "measuring", "pressing", req
                self.abort.clear()
        if busy:
            self.send({"type": "failed", "req": req, "error": "already measuring", "t_unix": time.time()})
            return
        self.publish_status()
        threading.Thread(target=self._measure, args=(req,), daemon=True).start()

    def _phase(self, req, phase, t_unix):
        with self.lock:
            self.phase = phase
        if phase == "started":
            self.send({"type": "started", "req": req, "t_unix": t_unix})
        self.publish_status()

    def _measure(self, req):
        cs = self.cs
        err = None
        try:
            entries = cs.measure(self.seen, on_phase=lambda ph, t: self._phase(req, ph, t), abort=self.abort)
            e = entries[-1]
            try:
                t_unix = datetime.fromisoformat(e["received_at"]).timestamp()
            except ValueError:
                t_unix = time.time()
            msg = {"type": "reading", "req": req, "t_unix": t_unix, "sys": e["sys"], "dia": e["dia"],
                   "bpm": e["bpm"], "mov": e["mov"], "ihb": e["ihb"], "device_time": str(e["device_time"]),
                   "n_new": len(entries)}
            self.send(msg)
            with self.lock:
                self.last_reading = {k: msg[k] for k in ("sys", "dia", "bpm", "mov", "ihb", "device_time")}
        except cs.Aborted as ex:
            err = str(ex)
            self.send({"type": "aborted", "req": req, "error": err, "t_unix": time.time()})
        except cs.MeasureError as ex:
            err = str(ex)
            self.send({"type": "failed", "req": req, "error": err, "t_unix": time.time()})
        except Exception as ex:  # servo / GPIO / anything unexpected
            log.exception("measurement crashed")
            err = f"{type(ex).__name__}: {ex}"
            self.send({"type": "failed", "req": req, "error": err, "t_unix": time.time()})
        finally:
            with self.lock:
                self.state, self.phase, self.req = "idle", None, None
                self.last_error = err
            self.publish_status()

    # ---- main loop --------------------------------------------------------
    def run(self):
        self.client.connect_async(self.cfg["host"], int(self.cfg["port"]), keepalive=30)
        self.client.loop_start()
        try:
            while not self.stop_flag.wait(STATUS_EVERY_S):
                if self.client.is_connected():
                    self.publish_status()
        finally:
            self.client.publish(self.t_status, json.dumps({"online": False, "kind": "cuff"}), qos=1, retain=True)
            time.sleep(0.5)
            self.client.loop_stop()
            self.client.disconnect()


def main():
    ap = argparse.ArgumentParser(description="auto-cuff MQTT node")
    ap.add_argument("--check", action="store_true", help="print the config, connect once, exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    if cfg["site"] and cfg["site"] != "cuff":
        log.warning("node %s has site %r on the server; set it to 'cuff' (Admin -> Nodes) or the "
                    "live page won't offer it", cfg["node_id"], cfg["site"])
    if a.check:
        print(json.dumps({k: v for k, v in cfg.items() if k != "key_file"}, indent=2))
        with socket.create_connection((cfg["host"], cfg["port"]), timeout=10) as raw:
            with tls_context(cfg).wrap_socket(raw, server_hostname=cfg["host"]):
                print("TLS to the broker OK (client certificate accepted for the handshake)")
        return
    import signal
    node = CuffNode(cfg)
    signal.signal(signal.SIGTERM, lambda *_: node.stop_flag.set())
    try:
        node.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
