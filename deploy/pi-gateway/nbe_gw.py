#!/usr/bin/env python3
"""nbe-gw: NBE lab gateway on a Raspberry Pi (campus Wi-Fi -> NAT -> Ethernet -> lab router).

    nbe-gw status [--json]   uplink, LAN, connected nodes, reachability of the NBE broker
    nbe-gw test              step-by-step connectivity test (DNS, gateway, broker, NTP)
    nbe-gw clients           DHCP leases on the lab side
    nbe-gw scan              2.4 GHz channel crowding -> which channel to set on the router
    nbe-gw mac               the Pi's Wi-Fi MAC (for UCSD-DEVICE registration)
    nbe-gw watchdog          one watchdog pass (run every minute by nbe-gw-watchdog.timer)
    nbe-gw serve [--port N]  status web page (nbe-gw-status.service)

Python 3 standard library + nmcli only. Config: /etc/nbe-gw/runtime.conf (written by setup.sh).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF_PATH = Path(os.environ.get("NBE_GW_CONF", "/etc/nbe-gw/runtime.conf"))
STATE_PATH = Path(os.environ.get("NBE_GW_STATE", "/var/lib/nbe-gw/watchdog.json"))
CON_UPLINK, CON_UPLINK2, CON_LAN = "nbe-uplink", "nbe-uplink2", "nbe-lan"

DEFAULTS = {
    "UPLINK_IFACE": "wlan0", "LAN_IFACE": "eth0", "UPLINK_SSID": "", "UPLINK2_SSID": "",
    "CHECK_HOST": "nbe.evm100.org", "CHECK_PORT": "8884", "REBOOT_AFTER_MIN": "30",
    "STATUS_PORT": "80", "LAN_ADDR": "192.168.77.1/24",
}


def load_conf() -> dict:
    conf = dict(DEFAULTS)
    try:
        for line in CONF_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return conf


# ---------------------------------------------------------------- helpers

def run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr if p.returncode else "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: timed out"


def nm_split(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped ':' (nmcli escapes ':' and '\\' with '\\')."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def read_sys(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def iface_counters(iface: str) -> dict:
    base = f"/sys/class/net/{iface}/statistics"
    return {"rx_bytes": int(read_sys(f"{base}/rx_bytes", "0") or 0),
            "tx_bytes": int(read_sys(f"{base}/tx_bytes", "0") or 0)}


def dev_show(iface: str) -> dict:
    rc, out = run(["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,GENERAL.HWADDR,"
                   "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "dev", "show", iface])
    info: dict = {"state": "unknown", "connection": "", "ip": "", "gateway": "", "dns": []}
    if rc:
        info["state"] = "missing"
        return info
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.replace("\\:", ":").replace("\\\\", "\\")
        if key == "GENERAL.STATE":
            info["state"] = val.split("(", 1)[-1].rstrip(")") if "(" in val else val
        elif key == "GENERAL.CONNECTION":
            info["connection"] = val
        elif key == "GENERAL.HWADDR":
            info["mac"] = val.lower()
        elif key.startswith("IP4.ADDRESS") and not info["ip"]:
            info["ip"] = val
        elif key == "IP4.GATEWAY":
            info["gateway"] = val
        elif key.startswith("IP4.DNS"):
            info["dns"].append(val)
    return info


def wifi_list(iface: str, rescan: bool = False) -> list[dict]:
    rc, out = run(["nmcli", "-t", "-f", "IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY",
                   "dev", "wifi", "list", "ifname", iface, "--rescan", "yes" if rescan else "no"],
                  timeout=30)
    aps = []
    if rc:
        return aps
    for line in out.splitlines():
        f = nm_split(line)
        if len(f) < 7:
            continue
        try:
            chan, sig = int(f[3]), int(f[5])
        except ValueError:
            continue
        aps.append({"in_use": f[0] == "*", "ssid": f[1], "bssid": f[2].lower(), "chan": chan,
                    "freq": f[4], "signal": sig, "security": f[6]})
    return aps


def iw_link(iface: str) -> dict:
    rc, out = run(["iw", "dev", iface, "link"], timeout=5)
    info: dict = {}
    if rc:
        return info
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("signal:"):
            try:
                info["signal_dbm"] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("tx bitrate:"):
            info["tx_bitrate"] = line.split(":", 1)[1].strip().split(" MBit")[0] + " Mbit/s"
        elif line.startswith("freq:"):
            info["freq_mhz"] = line.split()[1]
    return info


def tcp_check(host: str, port: int, timeout: float = 4.0) -> dict:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "target": f"{host}:{port}"}
    except OSError as e:
        return {"ok": False, "error": str(e) or type(e).__name__, "target": f"{host}:{port}"}


def ntp_check(host: str = "pool.ntp.org", timeout: float = 4.0) -> dict:
    t0 = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(b"\x1b" + 47 * b"\0", (host, 123))
            data, _ = s.recvfrom(256)
        secs = struct.unpack("!I", data[40:44])[0] - 2208988800
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "target": f"{host}:123/udp",
                "offset_s": round(secs - time.time(), 1)}
    except OSError as e:
        return {"ok": False, "error": str(e) or type(e).__name__, "target": f"{host}:123/udp"}


def leases(lan_iface: str) -> list[dict]:
    """DHCP leases of NetworkManager's shared-mode dnsmasq, plus whether the node answers ARP."""
    path = Path(os.environ.get("NBE_GW_LEASES", f"/var/lib/NetworkManager/dnsmasq-{lan_iface}.leases"))
    neigh: dict[str, str] = {}
    rc, out = run(["ip", "-4", "neigh", "show", "dev", lan_iface], timeout=5)
    if not rc:
        for line in out.splitlines():
            p = line.split()
            if p:
                neigh[p[0]] = p[-1]  # REACHABLE / STALE / DELAY / FAILED ...
    now = time.time()
    rows = []
    try:
        text = path.read_text()
    except OSError:
        text = ""
    for line in text.splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        exp = int(p[0])
        rows.append({"ip": p[2], "mac": p[1].lower(), "hostname": "" if p[3] == "*" else p[3],
                     "expires_in_s": (exp - int(now)) if exp else None,
                     "arp": neigh.get(p[2], "")})
    rows.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")))
    return rows


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def write_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.chmod(tmp, 0o644)
    tmp.replace(STATE_PATH)


def uptime_s() -> float:
    try:
        return float(read_sys("/proc/uptime", "0").split()[0])
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- status

def gather(conf: dict, checks: bool = True) -> dict:
    up_if, lan_if = conf["UPLINK_IFACE"], conf["LAN_IFACE"]
    up = dev_show(up_if)
    cur = next((a for a in wifi_list(up_if) if a["in_use"]), None)
    if cur:
        up.update(ssid=cur["ssid"], bssid=cur["bssid"], chan=cur["chan"], signal_pct=cur["signal"],
                  band="5 GHz" if cur["chan"] > 14 else "2.4 GHz")
    up.update(iw_link(up_if))
    up.update(iface_counters(up_if), iface=up_if)
    lan = dev_show(lan_if)
    lan.update(iface_counters(lan_if), iface=lan_if,
               carrier=read_sys(f"/sys/class/net/{lan_if}/carrier", "0") == "1")
    temp = read_sys("/sys/class/thermal/thermal_zone0/temp")
    st = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": socket.gethostname(),
        "uptime_s": round(uptime_s()),
        "temp_c": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "uplink": up, "lan": lan, "clients": leases(lan_if),
        "watchdog": read_state(),
    }
    if checks:
        st["checks"] = {
            "internet": tcp_check("1.1.1.1", 443),
            "broker": tcp_check(conf["CHECK_HOST"], int(conf["CHECK_PORT"])),
        }
    return st


def fmt_bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m {s}s"


def check_line(c: dict) -> str:
    return f"{c['target']} ({c['ms']} ms)" if c["ok"] else f"{c['target']}: {c.get('error')}"


def cmd_status(conf: dict, as_json: bool) -> int:
    st = gather(conf)
    if as_json:
        print(json.dumps(st, indent=1))
        return 0
    up, lan = st["uplink"], st["lan"]
    print(f"{st['hostname']}  up {fmt_dur(st['uptime_s'])}  {st['temp_c']} C")
    print(f"\nUPLINK  {up['iface']}  {up['state']}  ({up.get('connection') or 'no connection'})")
    if up.get("ssid"):
        print(f"  SSID {up['ssid']}  {up.get('band')} ch {up.get('chan')}  signal {up.get('signal_pct')}%"
              f" / {up.get('signal_dbm', '?')} dBm  rate {up.get('tx_bitrate', '?')}")
    print(f"  IP {up['ip'] or '-'}  gw {up['gateway'] or '-'}  DNS {' '.join(up['dns']) or '-'}")
    print(f"  MAC {up.get('mac', '?')}   rx {fmt_bytes(up['rx_bytes'])}  tx {fmt_bytes(up['tx_bytes'])}")
    print(f"\nLAN     {lan['iface']}  {lan['state']}  cable {'plugged' if lan['carrier'] else 'UNPLUGGED'}"
          f"  IP {lan['ip'] or '-'}  rx {fmt_bytes(lan['rx_bytes'])}  tx {fmt_bytes(lan['tx_bytes'])}")
    print(f"\nCLIENTS ({len(st['clients'])})")
    for c in st["clients"]:
        print(f"  {c['ip']:<16} {c['mac']}  {c['hostname'] or '-':<24} {c['arp'] or '-':<10}"
              f" lease {fmt_dur(c['expires_in_s'])}")
    print("\nCHECKS")
    for name, c in st["checks"].items():
        print(f"  {name:<9}{'OK  ' if c['ok'] else 'FAIL'} {check_line(c)}")
    wd = st["watchdog"]
    if wd.get("actions"):
        print("\nWATCHDOG (last actions)")
        for a in wd["actions"][-5:]:
            print(f"  {a}")
    return 0 if st["checks"]["broker"]["ok"] else 1


def cmd_test(conf: dict) -> int:
    up_if = conf["UPLINK_IFACE"]
    up = dev_show(up_if)
    ok = True

    def step(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'}  {name:<26} {detail}")

    step("uplink connected", up["state"].startswith("connected"),
         f"{up_if}: {up['state']} {up.get('connection', '')} {up['ip']}")
    gw = up["gateway"]
    if gw:
        rc, out = run(["ping", "-c", "3", "-W", "2", gw], timeout=10)
        step("campus gateway ping", rc == 0, gw if rc == 0 else f"{gw} (may just block ping)")
    try:
        t0 = time.monotonic()
        addr = socket.gethostbyname(conf["CHECK_HOST"])
        step("DNS", True, f"{conf['CHECK_HOST']} -> {addr} ({(time.monotonic() - t0) * 1000:.0f} ms)")
    except OSError as e:
        step("DNS", False, f"{conf['CHECK_HOST']}: {e}")
    step("internet (1.1.1.1:443)", (c := tcp_check("1.1.1.1", 443))["ok"], check_line(c))
    c = tcp_check(conf["CHECK_HOST"], int(conf["CHECK_PORT"]))
    step("NBE broker (nodes' MQTT)", c["ok"], check_line(c))
    c = tcp_check(conf["CHECK_HOST"], 443)
    step("NBE website / gateways", c["ok"], check_line(c))
    c = ntp_check()
    step("NTP (node clocks)", c["ok"],
         check_line(c) + (f"  Pi clock off by {c['offset_s']} s" if c.get("ok") else ""))
    lan_if = conf["LAN_IFACE"]
    carrier = read_sys(f"/sys/class/net/{lan_if}/carrier", "0") == "1"
    lan = dev_show(lan_if)
    step("LAN cable", carrier, lan_if)
    step("LAN sharing (nbe-lan)", lan.get("connection") == CON_LAN,
         f"{lan['state']} {lan.get('connection', '')} {lan['ip']}")
    fwd = read_sys("/proc/sys/net/ipv4/ip_forward") == "1"
    step("IPv4 forwarding", fwd, "net.ipv4.ip_forward=" + read_sys("/proc/sys/net/ipv4/ip_forward", "?"))
    rc, out = run(["nft", "list", "tables"], timeout=5)
    if rc == 0:
        step("NAT rules (NetworkManager)", f"nm-shared-{lan_if}" in out, f"table nm-shared-{lan_if}")
        step("uplink firewall", "nbe_gw" in out, "table inet nbe_gw")
    else:
        print(f"  ?   nft not readable (run with sudo for the firewall checks)")
    print("\nAll good." if ok else "\nSomething failed - see README.md 'Troubleshooting'.")
    return 0 if ok else 1


def cmd_clients(conf: dict) -> int:
    rows = leases(conf["LAN_IFACE"])
    if not rows:
        print("no DHCP leases (in router mode only the router itself shows up here)")
    for c in rows:
        print(f"{c['ip']:<16} {c['mac']}  {c['hostname'] or '-':<24} {c['arp'] or '-':<10}"
              f" lease {fmt_dur(c['expires_in_s'])}")
    return 0


def cmd_scan(conf: dict) -> int:
    aps = wifi_list(conf["UPLINK_IFACE"], rescan=True)
    if not aps:
        print("scan returned nothing (try with sudo)")
        return 1
    g24 = [a for a in aps if a["chan"] <= 14]
    print(f"{len(aps)} access points seen, {len(g24)} on 2.4 GHz\n")
    by_ch: dict[int, list[dict]] = {}
    for a in g24:
        by_ch.setdefault(a["chan"], []).append(a)
    for ch in sorted(by_ch):
        names = sorted({a["ssid"] or "(hidden)" for a in by_ch[ch]})
        print(f"  ch {ch:>2}: {len(by_ch[ch]):>2} APs, strongest {max(a['signal'] for a in by_ch[ch]):>3}%"
              f"   {', '.join(names)[:70]}")
    # interference score for the three non-overlapping channels: strong neighbours count more,
    # and APs on in-between channels (e.g. 3) overlap partially
    scores = {}
    for c in (1, 6, 11):
        scores[c] = sum((a["signal"] / 100) ** 2 * max(0, 5 - abs(a["chan"] - c)) / 5 for a in g24)
    best = min(scores, key=scores.get)
    print("\n  interference score (lower is better): "
          + "  ".join(f"ch {c}: {s:.2f}" for c, s in scores.items()))
    print(f"\n-> set the lab router's 2.4 GHz radio to channel {best}, 20 MHz width.")
    return 0


def cmd_mac(conf: dict) -> int:
    print(read_sys(f"/sys/class/net/{conf['UPLINK_IFACE']}/address", "?"))
    return 0


# ---------------------------------------------------------------- watchdog

def cmd_watchdog(conf: dict) -> int:
    st = read_state()
    st.setdefault("actions", [])
    now = time.time()

    def act(msg: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        print(msg, flush=True)
        st["actions"] = (st["actions"] + [line])[-30:]

    up_if, lan_if = conf["UPLINK_IFACE"], conf["LAN_IFACE"]

    # 1. LAN sharing up whenever the cable is in
    if read_sys(f"/sys/class/net/{lan_if}/carrier", "0") == "1":
        if dev_show(lan_if).get("connection") != CON_LAN:
            rc, out = run(["nmcli", "--wait", "20", "con", "up", CON_LAN], timeout=30)
            act(f"LAN sharing was down, starting {CON_LAN}: {'ok' if rc == 0 else out.strip()[:200]}")

    # 2. uplink health: internet reachable? (broker down != our problem, so check both)
    internet = tcp_check("1.1.1.1", 443)["ok"] or tcp_check(conf["CHECK_HOST"], int(conf["CHECK_PORT"]))["ok"]
    st["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["online"] = internet
    if internet:
        if st.get("fails"):
            act(f"uplink back after {st['fails']} failed checks")
        st["fails"], st["offline_since"] = 0, None
        # back from the fallback to the main uplink when it's visible again (every 10 min)
        up = dev_show(up_if)
        if (up.get("connection") == CON_UPLINK2 and conf.get("UPLINK_SSID")
                and now - st.get("last_prefer", 0) > 600):
            st["last_prefer"] = now
            if any(a["ssid"] == conf["UPLINK_SSID"] for a in wifi_list(up_if)):
                rc, out = run(["nmcli", "--wait", "45", "con", "up", CON_UPLINK], timeout=60)
                act(f"main uplink visible again, switched back: {'ok' if rc == 0 else out.strip()[:200]}")
        write_state(st)
        return 0

    st["fails"] = st.get("fails", 0) + 1
    st["offline_since"] = st.get("offline_since") or now
    offline_min = (now - st["offline_since"]) / 60
    n = st["fails"]
    if n in (2, 4, 8) or (n > 8 and n % 5 == 0):
        rc, out = run(["nmcli", "--wait", "45", "con", "up", CON_UPLINK], timeout=60)
        act(f"offline {offline_min:.0f} min: reconnect {CON_UPLINK}: {'ok' if rc == 0 else out.strip()[:200]}")
        if rc and conf.get("UPLINK2_SSID"):
            rc, out = run(["nmcli", "--wait", "45", "con", "up", CON_UPLINK2], timeout=60)
            act(f"  fallback {CON_UPLINK2}: {'ok' if rc == 0 else out.strip()[:200]}")
    if n == 6:
        act("offline: restarting NetworkManager")
        run(["systemctl", "restart", "NetworkManager"], timeout=60)
    reboot_min = float(conf.get("REBOOT_AFTER_MIN") or 0)
    if reboot_min and offline_min >= reboot_min and uptime_s() > 15 * 60:
        act(f"offline {offline_min:.0f} min >= {reboot_min:.0f}: rebooting")
        st["fails"], st["offline_since"] = 0, None
        write_state(st)
        run(["systemctl", "reboot"], timeout=30)
        return 0
    write_state(st)
    return 1


# ---------------------------------------------------------------- status page

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>NBE gateway</title><style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7385;--ok:#1a7f37;--bad:#c62828;--line:#e3e6ec}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--mut:#8b93a5;
--ok:#4cc26a;--bad:#ff6b6b;--line:#2a313d}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:920px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px}.sub{color:var(--mut);font-size:13px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}
.big{font-size:18px;font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:14px}.kv span:nth-child(odd){color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}.mono{font-family:ui-monospace,monospace;font-size:13px}
.wrap{overflow-x:auto}ul{margin:0;padding-left:18px;font-size:13px}
</style></head><body><main>
<h1>NBE lab gateway</h1><div class=sub id=sub>loading…</div>
<div class=grid>
 <div class=card><h2>Nodes → NBE server</h2><div id=broker class=big>…</div><div id=inet class=sub></div></div>
 <div class=card><h2>Campus uplink</h2><div id=up></div></div>
 <div class=card><h2>Lab side</h2><div id=lan></div></div>
</div>
<div class=card style="margin-top:12px"><h2>Connected devices</h2><div class=wrap><table id=cl></table></div></div>
<div class=card style="margin-top:12px"><h2>Watchdog</h2><ul id=wd></ul></div>
</main><script>
const e=s=>String(s??'-').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const B=n=>{const u=['B','kB','MB','GB','TB'];let i=0;while(n>=1000&&i<4){n/=1000;i++}return (i?n.toFixed(1):n)+' '+u[i]};
const D=s=>{if(s==null)return'-';const d=s/86400|0,h=s%86400/3600|0,m=s%3600/60|0;return d?d+'d '+h+'h':h?h+'h '+m+'m':m+'m'};
const kv=o=>'<div class=kv>'+Object.entries(o).map(([k,v])=>`<span>${e(k)}</span><span>${v}</span>`).join('')+'</div>';
async function tick(){try{const s=await (await fetch('api/status',{cache:'no-store'})).json();
 const c=s.checks||{},b=c.broker||{},i=c.internet||{};
 document.getElementById('sub').textContent=`${s.hostname} · up ${D(s.uptime_s)} · ${s.temp_c??'?'} °C · ${s.time}`;
 document.getElementById('broker').innerHTML=b.ok?`<span class=ok>● reachable</span> <span class=sub>${b.ms} ms</span>`:`<span class=bad>● unreachable</span>`;
 document.getElementById('inet').textContent=`${e(b.target)} · internet ${i.ok?'ok':'DOWN'}`;
 const u=s.uplink;document.getElementById('up').innerHTML=kv({State:e(u.state),SSID:e(u.ssid),Band:e(u.band?u.band+' ch '+u.chan:null),
  Signal:u.signal_pct!=null?`${u.signal_pct}% · ${u.signal_dbm??'?'} dBm`:'-',Rate:e(u.tx_bitrate),IP:e(u.ip),
  Traffic:`↓ ${B(u.rx_bytes)} ↑ ${B(u.tx_bytes)}`});
 const l=s.lan;document.getElementById('lan').innerHTML=kv({Cable:l.carrier?'<span class=ok>plugged</span>':'<span class=bad>unplugged</span>',
  State:e(l.state),IP:e(l.ip),Traffic:`↓ ${B(l.rx_bytes)} ↑ ${B(l.tx_bytes)}`,Devices:s.clients.length});
 document.getElementById('cl').innerHTML='<tr><th>IP<th>Name<th>MAC<th>Seen<th>Lease</tr>'+(s.clients.map(c=>
  `<tr><td class=mono>${e(c.ip)}<td>${e(c.hostname)}<td class=mono>${e(c.mac)}<td>${e(c.arp)}<td>${D(c.expires_in_s)}</tr>`).join('')
  ||'<tr><td colspan=5 class=sub>none yet (router mode: only the router shows here)</tr>');
 const w=s.watchdog||{};document.getElementById('wd').innerHTML=(w.actions||[]).slice(-8).reverse().map(a=>`<li>${e(a)}</li>`).join('')
  ||`<li class=sub>nothing to report${w.last_check?' · last check '+e(w.last_check):''}</li>`;
}catch(x){document.getElementById('sub').textContent='status unavailable: '+x}}
tick();setInterval(tick,5000);
</script></body></html>"""


def cmd_serve(conf: dict, port: int) -> int:
    lock = threading.Lock()
    cache: dict = {"t": 0.0, "checks_t": 0.0, "data": {}, "checks": {}}

    def snapshot() -> dict:
        with lock:
            now = time.monotonic()
            if now - cache["t"] > 4:
                fresh_checks = now - cache["checks_t"] > 15
                data = gather(conf, checks=fresh_checks)
                if fresh_checks:
                    cache["checks"], cache["checks_t"] = data["checks"], now
                data["checks"] = cache["checks"]
                cache["data"], cache["t"] = data, now
            return cache["data"]

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/status"):
                body, ctype = json.dumps(snapshot()).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    print(f"nbe-gw status page on :{port}", flush=True)
    srv.serve_forever()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="nbe-gw", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    for name in ("test", "clients", "scan", "mac", "watchdog"):
        sub.add_parser(name)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    a = ap.parse_args()
    conf = load_conf()
    if a.cmd == "status":
        return cmd_status(conf, a.json)
    if a.cmd == "serve":
        return cmd_serve(conf, a.port or int(conf.get("STATUS_PORT") or 80))
    fn = {"test": cmd_test, "clients": cmd_clients, "scan": cmd_scan, "mac": cmd_mac,
          "watchdog": cmd_watchdog}.get(a.cmd or "status")
    return fn(conf) if fn else cmd_status(conf, False)


if __name__ == "__main__":
    sys.exit(main())
