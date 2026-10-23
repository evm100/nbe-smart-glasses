"""A simulated ESP32 node that talks MQTT exactly like the firmware.

    sglab sim-node FNG02 --host mqtt.example.org --cert FNG02.crt --key FNG02.key --ca ca.crt

Publishes a retained status (+ last-will), answers INFO/START/STOP on its cmd
topic and, while streaming, publishes acknowledged binary frames every 250 ms like firmware >= 0.6
(sglab.framing.FrameSender; frames=False sends text lines like older firmware),
plus a SYNC event (device clock <-> UTC) every 10 s like an SNTP-synced node.
Use it to test the server, the broker ACL and the web UI before boards exist.
"""
from __future__ import annotations

import json
import ssl
import threading
import time

from .framing import FrameSender
from .protocol import encode
from .sources import SimSource


class SimNode:
    def __init__(self, node_id: str, cfg: dict, prefix: str = "nbe", site: str = "finger",
                 status_every_s: float = 15.0, batch_ms: int = 250, sim_kw: dict | None = None, client=None,
                 frames: bool = True):
        self.node_id, self.cfg, self.prefix, self.site = node_id, cfg, prefix.rstrip("/"), site
        self.status_every_s, self.batch_s = status_every_s, batch_ms / 1000
        self.src = SimSource(dev=node_id, realtime=True, **(sim_kw or {}))
        self.base = f"{self.prefix}/dev/{node_id}"
        self.t0 = time.monotonic()
        self.stop_flag = threading.Event()
        self.connected = threading.Event()
        self.sent = {"batches": 0, "lines": 0}
        self._last_sync = 0.0
        self.sender = FrameSender() if frames else None
        self._lock = threading.Lock()  # sender: ACKs arrive on paho's thread
        self.client = client or self._make_client()

    # ---- MQTT ----------------------------------------------------------------
    def _make_client(self):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.node_id, clean_session=True)
        if self.cfg.get("tls", True):
            c.tls_set(ca_certs=self.cfg.get("ca_file") or None, certfile=self.cfg.get("cert_file"),
                      keyfile=self.cfg.get("key_file"), tls_version=ssl.PROTOCOL_TLS_CLIENT)
        if self.cfg.get("username"):
            c.username_pw_set(self.cfg["username"], self.cfg.get("password"))
        c.will_set(f"{self.base}/status", json.dumps({"online": False}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = lambda *a, **k: self.connected.clear()
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 30)
        return c

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            print(f"[{self.node_id}] connect refused: {reason_code}")
            return
        client.subscribe([(f"{self.base}/cmd", 1), (f"{self.prefix}/core/status", 1)])
        if self.sender:
            with self._lock:
                self.sender.go_back()  # what was in flight may be lost
        self.connected.set()
        self.publish_status()

    def _on_message(self, client, userdata, msg):
        if msg.topic.endswith("/core/status"):
            try:
                if not json.loads(msg.payload or b"{}").get("online", True) and self.src._streaming:
                    self.src.write("STOP")  # server gone: don't stream into the void
            except ValueError:
                pass
            return
        cmd = msg.payload.decode(errors="replace").strip().upper()
        if self.sender:
            with self._lock:
                if self.sender.handle_cmd(cmd):
                    return
        if cmd in ("INFO", "START", "STOP"):
            self.src.write(cmd)
            if cmd != "INFO":
                self.publish_status()

    def publish_status(self):
        st = {"online": True, "fw": "sim-0.2", "sensor": "SIM", "site": self.site, "streaming": self.src._streaming,
              "fs": self.src.fs, "rssi": -55, "uptime_s": int(time.monotonic() - self.t0), "ts": int(time.time()),
              "link": "mqtt"}
        self.client.publish(f"{self.base}/status", json.dumps(st), qos=1, retain=True)

    # ---- main loop -------------------------------------------------------------
    def step(self):
        """Collect everything the simulated sensor produced since the last call and publish it."""
        if self.src._streaming and time.time() - self._last_sync >= 10:
            self._last_sync = time.time()
            self.src._push(encode(f"E,{self.src._now_us()},SYNC,{int(time.time() * 1000)}"))
        lines = []
        while True:
            ln = self.src.readline()
            if not ln:
                break
            lines.append(ln if ln.endswith("\n") else ln + "\r\n")
        if self.sender:
            with self._lock:
                self.sender.add_lines(lines)
                frames = self.sender.frames()
            for f in frames:
                self.client.publish(f"{self.base}/data", f, qos=0)
            self.sent["batches"] += len(frames)
            self.sent["lines"] += len(lines)
        elif lines:
            self.client.publish(f"{self.base}/data", "".join(lines).encode(), qos=1)
            self.sent["batches"] += 1
            self.sent["lines"] += len(lines)

    def run(self, duration_s: float | None = None):
        self.client.connect_async(self.cfg["host"], int(self.cfg.get("port", 8883)), keepalive=30)
        self.client.loop_start()
        t_end = time.monotonic() + duration_s if duration_s else None
        last_status = time.monotonic()
        try:
            while not self.stop_flag.is_set() and (t_end is None or time.monotonic() < t_end):
                time.sleep(self.batch_s)
                if not self.connected.is_set():
                    continue
                self.step()
                if time.monotonic() - last_status >= self.status_every_s:
                    last_status = time.monotonic()
                    self.publish_status()
        finally:
            if self.connected.is_set():  # else the broker's last-will already says offline
                try:
                    self.client.publish(f"{self.base}/status", json.dumps({"online": False}), qos=1,
                                        retain=True).wait_for_publish(2)
                except RuntimeError:  # connection dropped meanwhile
                    pass
            self.client.disconnect()
            self.client.loop_stop()

    def start_thread(self, duration_s: float | None = None) -> threading.Thread:
        th = threading.Thread(target=self.run, args=(duration_s,), daemon=True, name=f"simnode-{self.node_id}")
        th.start()
        return th
