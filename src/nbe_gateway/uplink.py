"""The gateway on MQTT: it IS the node (client certificate CN = node ID), so the server's broker
ACL, registry, recorder and web pages treat it exactly like a Wi-Fi board.

    <prefix>/dev/<ID>/data    NB frames from the record log (QoS 0, acknowledged end to end)
    <prefix>/dev/<ID>/status  retained JSON like the firmware's, + "link": "usb-gateway"; last will offline
    <prefix>/dev/<ID>/cmd     ACK / RESEND -> the log; INFO / START / STOP / CAM ... -> the board
    <prefix>/core/status      server offline -> STOP the board (as Wi-Fi nodes do)

Sending: frames of whole records from the first unsent byte, at most `window` bytes ahead of the
last ACK; go back to the last ACK after a reconnect, a RESEND, or ack_timeout_s without progress
(the same go-back-N as firmware/esp-idf/ppg_stream/main/link_mqtt.c).
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import time
from typing import Callable, Optional

from .store import RecordLog
from .wire import nb_frame

log = logging.getLogger("nbe_gateway")

STATUS_EVERY_S = 15.0
ALPN = "mqtt"   # lets the server run MQTT on :443 next to its websites (nginx ssl_preread routes on it)


def tls_context(cfg: dict) -> ssl.SSLContext:
    """mTLS as the node: client cert + key, server verified against ca_file or the system CAs."""
    ctx = ssl.create_default_context(cafile=cfg.get("ca_file") or None)
    if not cfg.get("ca_file"):
        ctx.load_default_certs()
    # Python >= 3.13 adds VERIFY_X509_STRICT, which rejects private CAs made without
    # keyUsage / key identifiers; verify like the server's own MQTT client (sglab.mqtt_hub) does
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    ctx.load_cert_chain(cfg["cert_file"], cfg["key_file"])
    ctx.set_alpn_protocols([ALPN])   # a broker that doesn't do ALPN (mosquitto on :8884) just ignores it
    return ctx


class Uplink:
    def __init__(self, cfg: dict, rlog: RecordLog, on_command: Callable[[str], None],
                 describe: Callable[[], dict], client=None, version: str = ""):
        self.cfg, self.log, self.on_command, self.describe, self.version = cfg, rlog, on_command, describe, version
        self.node = cfg["node_id"]
        prefix = cfg.get("topic_prefix", "nbe").rstrip("/")
        self.t_data = f"{prefix}/dev/{self.node}/data"
        self.t_status = f"{prefix}/dev/{self.node}/status"
        self.t_cmd = f"{prefix}/dev/{self.node}/cmd"
        self.t_core = f"{prefix}/core/status"
        self.frame_max = int(cfg.get("frame_max_kb", 512)) * 1024
        self.window = int(cfg.get("window_kb", 4096)) * 1024
        self.ack_timeout_s = float(cfg.get("ack_timeout_s", 10.0))
        self.sent = rlog.acked
        self.progress = time.monotonic()
        self.connected = False
        self.server_online: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self.status_dirty = True
        self.stats = {"frames": 0, "bytes": 0, "resends": 0, "acks": 0, "connects": 0}
        self._rate = (time.monotonic(), 0)
        self.rate_bps = 0.0
        self.client = client or self._make_client()

    # ---------------------------------------------------------------- connection
    def _make_client(self):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.node, clean_session=True)
        c.tls_set_context(tls_context(self.cfg))
        c.will_set(self.t_status, json.dumps({"online": False, "link": "usb-gateway"}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_connect_fail = self._on_connect_fail
        c.on_disconnect = self._on_disconnect
        c.enable_logger(logging.getLogger("nbe_gateway.paho"))  # quiet unless --verbose (app.py)
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 30)
        c.max_queued_messages_set(0)
        return c

    def start(self) -> None:
        self.client.connect_async(self.cfg["broker_host"], int(self.cfg.get("broker_port", 8883)), keepalive=20)
        self.client.loop_start()
        threading.Thread(target=self._sender, name="uplink-tx", daemon=True).start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.wake.set()
        try:
            st = {**self._status(), "online": False}
            self.client.publish(self.t_status, json.dumps(st), qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        try:
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.last_error = f"broker refused the connection: {reason_code} (certificate / ACL?)"
            log.warning(self.last_error)
            return
        self.connected, self.last_error = True, None
        self.stats["connects"] += 1
        client.subscribe([(self.t_cmd, 1), (self.t_core, 1)])
        with self.lock:  # what was in flight may be lost: send it again
            self._go_back()
        self.status_dirty = True
        self.wake.set()
        log.info("connected to %s", self.cfg["broker_host"])

    def _on_connect_fail(self, client, userdata):
        # TLS / DNS / socket errors (paho logs the details through enable_logger)
        self.last_error = f"cannot reach {self.cfg['broker_host']}:{self.cfg.get('broker_port', 8883)} (retrying)"

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        self.last_error = f"disconnected: {reason_code}"

    def _on_message(self, client, userdata, msg):
        self.handle(msg.topic, msg.payload)

    def handle(self, topic: str, payload: bytes) -> None:
        text = payload.decode("ascii", "replace").strip()
        if topic == self.t_cmd:
            parts = text.split()
            if len(parts) == 3 and parts[0] in ("ACK", "RESEND"):
                self._on_ack(parts[0] == "RESEND", parts[1], parts[2])
            elif text:
                self.on_command(text)
        elif topic == self.t_core:
            try:
                online = bool(json.loads(text or "{}").get("online"))
            except ValueError:
                return
            if self.server_online and not online:
                self.on_command("STOP")  # never stream into the void for hours
            self.server_online = online

    # ---------------------------------------------------------------- acknowledgement
    def _go_back(self) -> None:
        if self.sent > self.log.acked:
            self.stats["resends"] += 1
        self.sent = self.log.acked
        self.progress = time.monotonic()

    def _on_ack(self, resend: bool, boot_hex: str, off_txt: str) -> None:
        try:
            boot, off = int(boot_hex, 16), int(off_txt)
        except ValueError:
            return
        if boot != self.log.boot:
            return
        with self.lock:
            if off > self.log.acked and self.log.ack(off):
                self.progress = time.monotonic()
                self.stats["acks"] += 1
            if self.sent < self.log.acked:
                self.sent = self.log.acked
            if resend:
                self._go_back()
        self.wake.set()

    # ---------------------------------------------------------------- sending
    def _sender(self) -> None:
        last_status = 0.0
        while not self.stop_flag.is_set():
            self.wake.wait(0.05)
            self.wake.clear()
            now = time.monotonic()
            if self.connected:
                self._send_frames(now)
                if self.status_dirty or now - last_status >= STATUS_EVERY_S:
                    last_status = now
                    self.publish_status()
            m_t, m_b = self._rate
            if now - m_t >= 2.0:
                self.rate_bps = (self.stats["bytes"] - m_b) / (now - m_t)
                self._rate = (now, self.stats["bytes"])

    def _send_frames(self, now: float) -> None:
        with self.lock:
            if self.sent > self.log.acked and now - self.progress > self.ack_timeout_s:
                log.warning("no ACK for %.0f s - sending %d bytes again", self.ack_timeout_s, self.sent - self.log.acked)
                self._go_back()
            while self.connected and self.sent - self.log.acked < self.window:
                data, end = self.log.read(self.sent, self.frame_max)
                if not data:
                    break
                if self.sent == self.log.acked:
                    self.progress = now
                info = self.client.publish(self.t_data, nb_frame(self.log.boot, self.sent, data), qos=0)
                if getattr(info, "rc", 0) != 0:  # not connected: send it again later
                    break
                self.sent = end
                self.stats["frames"] += 1
                self.stats["bytes"] += len(data)

    def notify(self) -> None:
        """New data in the log / the board's state changed."""
        self.status_dirty = True
        self.wake.set()

    # ---------------------------------------------------------------- status
    def _status(self) -> dict:
        d = self.describe()
        st = {"online": d.get("usb") == "connected", "link": "usb-gateway", "gateway": self.version,
              "host": socket.gethostname(), "usb": d.get("usb"), "fw": d.get("fw"), "sensor": d.get("sensor"),
              "sensor_ok": bool(d.get("sensor_ok")), "camera_ok": d.get("camera_ok"), "streaming": d.get("streaming"),
              "fs": d.get("fs"), "ts": int(time.time()), "buf": self.log.unacked(), "unacked": self.sent - self.log.acked,
              "rtx": self.stats["resends"], "disk_mb": round(self.log.disk_bytes() / 1e6, 1),
              "up_kbps": round(self.rate_bps / 1e3, 1)}
        s = d.get("status") or {}
        for k in ("tx_drop", "cam_fps", "cam_drop", "fs_meas", "cam_grab", "cam_capped", "cam_null", "cam_wfail",
                  "cam_wait_ms", "cam_tx_ms", "log_lost"):
            if k in s:
                st[k] = s[k]
        return st

    def publish_status(self) -> None:
        self.status_dirty = False
        try:
            self.client.publish(self.t_status, json.dumps(self._status()), qos=1, retain=True)
        except Exception:
            pass
