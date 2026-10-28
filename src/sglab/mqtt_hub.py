"""MQTT connection of the sglab service: node presence, commands, data.

Topics (prefix default "nbe"; one set per node ID):

    nbe/dev/<ID>/status   node -> server, retained JSON, plus last-will {"online": false}
    nbe/dev/<ID>/data     node -> server, QoS 1: a batch of PPG protocol v1 lines
                          ($H/$D/$E/$S ... *CS\\r\\n) exactly as on USB serial
    nbe/dev/<ID>/cmd      server -> node, QoS 1: INFO | START | STOP | OTA <token> (see sglab.firmware)
    nbe/core/status       server -> nodes, retained {"online": true} + last-will {"online": false};
                          nodes stop streaming when the server disappears

Because the data payload is the same line protocol as USB, the recorder,
checksums, gap detection and everything downstream are shared by both links.
mosquitto's ACL pins each node certificate (CN = node ID) to its own topics.
"""
from __future__ import annotations

import json
import logging
import queue
import ssl
import threading
import time
from typing import Callable

from .framing import FrameReceiver, is_frame

log = logging.getLogger("sglab.mqtt")

STALE_AFTER_S = 45.0  # nodes publish status every ~15 s
ORPHAN_GRACE_S = 10.0  # after startup, a node streaming into no trial is told to STOP


class MqttHub:
    def __init__(self, cfg: dict, allowed: Callable[[], set[str]] | None = None, client=None):
        """cfg = Settings.mqtt. `allowed` returns the enabled node IDs (others are ignored).
        `client` lets tests inject a fake paho client."""
        self.cfg = cfg
        self.prefix = cfg.get("topic_prefix", "nbe").rstrip("/")
        self.allowed = allowed
        self.lock = threading.Lock()
        self.status: dict[str, dict] = {}
        self.sinks: dict[str, Callable[[bytes], None]] = {}
        self.idle_cbs: dict[str, Callable[[bool], None]] = {}  # node reports streaming true/false
        self.receivers: dict[str, FrameReceiver] = {}  # binary frame reassembly + ACKs per node
        self.on_mac: Callable[[str, str], None] | None = None  # node reported a (new) MAC -> registry
        self.connected = False
        self.last_error: str | None = None
        self.stats = {"data_msgs": 0, "data_bytes": 0, "unrouted": 0, "ignored": 0, "orphan_stops": 0}
        self.t_start = time.time()
        self._orphan_stop: dict[str, float] = {}
        self.client = client or self._make_client()

    # ---- connection ---------------------------------------------------------------
    def _make_client(self):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg.get("client_id", "nbe-core"),
                        clean_session=True)
        if self.cfg.get("tls", True):
            c.tls_set(ca_certs=self.cfg.get("ca_file") or None,  # None = system CAs (e.g. Let's Encrypt)
                      certfile=self.cfg.get("cert_file"), keyfile=self.cfg.get("key_file"),
                      tls_version=ssl.PROTOCOL_TLS_CLIENT)
        if self.cfg.get("username"):
            c.username_pw_set(self.cfg["username"], self.cfg.get("password"))
        c.will_set(f"{self.prefix}/core/status", json.dumps({"online": False}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 30)
        return c

    def start(self):
        self.client.connect_async(self.cfg["host"], int(self.cfg.get("port", 8883)), keepalive=30)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # pragma: no cover
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.last_error = f"connect refused: {reason_code}"
            log.warning(self.last_error)
            return
        self.connected, self.last_error = True, None
        client.subscribe([(f"{self.prefix}/dev/+/status", 1), (f"{self.prefix}/dev/+/data", 1)])
        client.publish(f"{self.prefix}/core/status", json.dumps({"online": True, "ts": int(time.time())}),
                       qos=1, retain=True)
        log.info("MQTT connected")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        self.last_error = f"disconnected: {reason_code}"

    # ---- messages -----------------------------------------------------------------
    def _on_message(self, client, userdata, msg):
        self.handle(msg.topic, msg.payload, getattr(msg, "retain", False))

    def handle(self, topic: str, payload: bytes, retain: bool = False) -> None:
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != self.prefix or parts[1] != "dev":
            return
        node_id, kind = parts[2], parts[3]
        if self.allowed is not None and node_id not in self.allowed():
            self.stats["ignored"] += 1
            return
        if kind == "status":
            try:
                st = json.loads(payload.decode() or "{}")
            except (ValueError, UnicodeDecodeError):
                return
            with self.lock:
                prev = self.status.get(node_id, {})
                st["_received"] = time.time()
                st["_retained"] = retain
                self.status[node_id] = {**prev, **st}
                idle_cb = self.idle_cbs.get(node_id)
            if self.on_mac and st.get("mac") and st["mac"] != prev.get("mac"):
                try:
                    self.on_mac(node_id, st["mac"])
                except Exception:
                    log.exception("storing MAC of %s", node_id)
            if idle_cb and isinstance(st.get("streaming"), bool):
                # retained too: after the hub reconnects, that is how it learns a node went idle
                idle_cb(not st["streaming"])
            if st.get("streaming") and not retain:
                self._maybe_stop_orphan(node_id)
        elif kind == "data":
            self.stats["data_msgs"] += 1
            self.stats["data_bytes"] += len(payload)
            if is_frame(payload):  # firmware >= 0.6: acknowledged binary frames -> protocol lines
                with self.lock:
                    rx = self.receivers.setdefault(node_id, FrameReceiver())
                lines, reply = rx.feed(payload)
                if reply:
                    self.client.publish(f"{self.prefix}/dev/{node_id}/cmd", reply, qos=0)
                payload = "".join(lines).encode()
                if not payload:
                    return
            with self.lock:
                sink = self.sinks.get(node_id)
                if node_id in self.status:
                    self.status[node_id]["_last_data"] = time.time()
            if sink:
                sink(payload)
            else:
                self.stats["unrouted"] += 1
                if b"$D," in payload:  # samples, not just a boot banner / INFO answer
                    self._maybe_stop_orphan(node_id)

    def _maybe_stop_orphan(self, node_id: str) -> None:
        """A node streams but no trial owns it (server restarted mid-trial, a STOP was lost...)."""
        now = time.time()
        if node_id in self.sinks or now - self.t_start < ORPHAN_GRACE_S:
            return
        if now - self._orphan_stop.get(node_id, 0) < ORPHAN_GRACE_S:
            return
        self._orphan_stop[node_id] = now
        self.stats["orphan_stops"] += 1
        log.info("node %s streams outside a trial - sending STOP", node_id)
        self.publish_cmd(node_id, "STOP")

    # ---- API used by the web app / recorder -----------------------------------------
    def node_state(self, node_id: str) -> dict:
        with self.lock:
            st = dict(self.status.get(node_id, {}))
        now = time.time()
        if not st:
            return {"node_id": node_id, "online": False, "state": "never seen"}
        # a retained status may be old: trust it only if the node's own timestamp (SNTP) is recent
        heard = 0.0 if st.get("_retained") else st.get("_received", 0)
        heard = max(heard, float(st.get("ts", 0) or 0), st.get("_last_data", 0))
        claimed = bool(st.get("online"))
        online = claimed and (now - heard) < STALE_AFTER_S
        rx = self.receivers.get(node_id)
        if rx:
            st["rx"] = dict(rx.stats)
        st.update(node_id=node_id, online=online, claimed_online=claimed,
                  age_s=round(now - heard, 1) if heard else None, in_use=node_id in self.sinks,
                  state="online" if online else ("stale" if claimed else "offline"))
        return st

    def known_ids(self) -> set[str]:
        with self.lock:
            return set(self.status)

    def publish_cmd(self, node_id: str, cmd: str) -> None:
        self.client.publish(f"{self.prefix}/dev/{node_id}/cmd", cmd.strip().upper(), qos=1)

    def attach(self, node_id: str, sink: Callable[[bytes], None],
               on_idle: Callable[[bool], None] | None = None) -> None:
        with self.lock:
            if node_id in self.sinks:
                raise RuntimeError(f"node {node_id} is already recording")
            self.sinks[node_id] = sink
            if on_idle:
                self.idle_cbs[node_id] = on_idle

    def detach(self, node_id: str) -> None:
        with self.lock:
            self.sinks.pop(node_id, None)
            self.idle_cbs.pop(node_id, None)


class MqttSource:
    """Recorder source for a node reached over MQTT (port string `mqtt://<ID>`).

    Same interface as SerialSource: readline() / write() / close(), plus `idle`: set while the
    node says it is not streaming (rebooted, reconnected after the server's last will stopped
    it, ...), so the recorder can send START again right away."""

    def __init__(self, hub: MqttHub, node_id: str):
        self.hub, self.node_id = hub, node_id
        self.port = f"mqtt://{node_id}"
        self.q: queue.Queue[str] = queue.Queue()
        self.idle = threading.Event()
        hub.attach(node_id, self._feed, self._on_idle)

    def _on_idle(self, idle: bool) -> None:
        if idle:
            self.idle.set()
        else:
            self.idle.clear()

    def _feed(self, payload: bytes) -> None:
        for line in payload.decode("ascii", "replace").splitlines():
            if line:
                self.q.put(line)

    def node_streaming(self) -> bool:
        """The node's recent status says it is streaming (online = heard within STALE_AFTER_S)."""
        st = self.hub.node_state(self.node_id)
        return bool(st.get("online")) and st.get("streaming") is True

    def write(self, cmd: str) -> None:
        self.hub.publish_cmd(self.node_id, cmd)

    def readline(self) -> str:
        try:
            return self.q.get(timeout=0.2)
        except queue.Empty:
            return ""

    def close(self) -> None:
        self.hub.detach(self.node_id)


# a process-wide hub (set by the web app when MQTT is configured)
_HUB: MqttHub | None = None


def set_hub(hub: MqttHub | None) -> None:
    global _HUB
    _HUB = hub


def get_hub() -> MqttHub | None:
    return _HUB
