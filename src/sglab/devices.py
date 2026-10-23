"""Everything a trial can record from, in one list.

    local devices   [devices.*] in config/lab.toml: USB serial ports of the machine
                    running the server, and sim:// simulators (hidden on a headless
                    server: [web] allow_local_devices = false)
    nodes           ESP32 boards registered in the node registry, reached over MQTT;
                    live state from their retained status messages

A device's key is what the trial form submits: the lab.toml name for local
devices, ``mqtt://<NODE_ID>`` for nodes. Busy-ness is tracked by port, so two
aliases of the same serial port can't be recorded twice.
"""
from __future__ import annotations

from pathlib import Path


class DeviceError(ValueError):
    pass


class DeviceDirectory:
    def __init__(self, settings, registry=None, hub=None):
        self.settings, self.registry, self.hub = settings, registry, hub

    # ---- local (serial / simulated) ----------------------------------------------
    def _local(self) -> list[dict]:
        if not self.settings.allow_local_devices:
            return []
        from .sources import list_ports

        present = {p["port"] for p in list_ports()}
        out = []
        for name, d in self.settings.lab.get("devices", {}).items():
            port = d.get("port", "")
            sim = port.startswith("sim://")
            ok = sim or port in present or Path(port).exists() or "://" in port
            out.append({"key": name, "label": name, "kind": "sim" if sim else "serial", "role": d.get("role", name),
                        "port": port, "online": ok, "state": "simulated" if sim else ("port found" if ok else "port not found"),
                        "details": {}})
        return out

    # ---- MQTT nodes -----------------------------------------------------------------
    def _nodes(self) -> list[dict]:
        if self.registry is None:
            return []
        out = []
        for n in self.registry.nodes():
            if not n["enabled"]:
                continue
            st = self.hub.node_state(n["node_id"]) if self.hub else {"online": False, "state": "MQTT off"}
            if self.hub and not self.hub.connected:
                st = {**st, "online": False, "state": "server not connected to broker"}
            state = st.get("state") or ("online" if st.get("online") else "offline")
            details = {k: st[k] for k in ("fw", "sensor", "rssi", "txp", "uptime_s", "reset", "ip", "batt_mv", "buf", "unacked", "rtx", "tx_drop", "fs")
                       if k in st}
            if st.get("age_s") is not None:
                details["last_seen_s"] = st["age_s"]
            out.append({"key": f"mqtt://{n['node_id']}", "label": n["name"] or n["node_id"], "node_id": n["node_id"],
                        "kind": "node", "role": n["site"] or "node", "port": f"mqtt://{n['node_id']}",
                        "online": bool(st.get("online")), "state": state, "streaming": bool(st.get("streaming")),
                        "details": details})
        return out

    def list(self, busy: dict[str, dict] | None = None) -> list[dict]:
        """busy: port -> {"trial": id, "owner": username} for devices in a running trial."""
        busy = busy or {}
        out = self._nodes() + self._local()
        for d in out:
            b = busy.get(d["port"])
            d["busy"] = b
            d["available"] = d["online"] and not b
            d["connected"] = d["online"]  # older templates / API users
        return out

    def resolve(self, key: str) -> dict:
        """Device dict for recorder.Recording ({"port", "role", ...}); DeviceError if unusable."""
        for d in self.list():
            if d["key"] == key:
                if d["kind"] == "node" and not d["online"]:
                    raise DeviceError(f"node {d['node_id']} is {d['state']} – check its power / Wi-Fi")
                if d["kind"] == "node":
                    return {"port": d["port"], "role": d["role"], "label": d["label"], "kind": "node"}
                return {**self.settings.device(key), "label": d["label"], "kind": d["kind"]}
        raise DeviceError(f"unknown or unavailable device {key!r}")
