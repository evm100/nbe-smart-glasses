#!/usr/bin/env python3
"""nbe-node: get a sensor node's credentials from the NBE server and prepare the ESP-IDF build.

Needs only Python 3.9+ (no packages). Run it from the ESP-IDF terminal if you want it to
build / flash for you; otherwise run `idf.py build flash` yourself afterwards.

  python nbe_node.py login https://ppg.example.org        (once; asks for your admin login)
  python nbe_node.py list                                 registered nodes + online state
  python nbe_node.py register FNG02 --site finger         new node (issues its certificate)
  python nbe_node.py configure FNG02                      certs + sdkconfig for that node (asks for Wi-Fi)
  python nbe_node.py configure FNG02 --build --flash -p /dev/ttyACM0
  python nbe_node.py configure FNG02 --wifi-ssid UCSD-PROTECTED --wifi-eap-user <login>   (WPA2-Enterprise)
  python nbe_node.py configure FNG02 --wifi2-ssid <hotspot> --wifi3-ssid <home>   fallbacks if #1 isn't found
  python nbe_node.py mac -p /dev/ttyACM0                  board MAC (for UCSD-DEVICE registration)
  python nbe_node.py upload FNG02 build/ppg_stream.bin    store a new firmware image on the server
  python nbe_node.py ota FNG02                            tell the node to install it over Wi-Fi

`configure` writes, in firmware/esp-idf/ppg_stream/:
  main/certs/client.crt, client.key (+ broker_ca.crt)   this node's identity - never commit
  sdkconfig.nbe                                         node ID, site, broker, Wi-Fi, link = MQTT
and updates an existing `sdkconfig` with the same values, so `idf.py build` uses them.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PROJECT = HERE.parent / "esp-idf" / "ppg_stream"
CONF_DIR = Path(os.environ.get("NBE_NODE_CONFIG") or Path.home() / ".config" / "nbe-node")
CONF = CONF_DIR / "config.json"


class Fail(Exception):
    pass


# ---------------------------------------------------------------- config + HTTP
def load_conf() -> dict:
    try:
        return json.loads(CONF.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_conf(c: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds an API token
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=2)


def api(method: str, path: str, body: dict | bytes | None = None, conf: dict | None = None, auth: bool = True):
    conf = conf if conf is not None else load_conf()
    server = conf.get("server")
    if not server:
        raise Fail("not logged in: python nbe_node.py login https://<server>")
    headers = {"Accept": "application/json", "User-Agent": "nbe-node/1"}
    data = None
    if isinstance(body, bytes):
        data = body
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = "Bearer " + conf.get("token", "")
    ctx = ssl.create_default_context(cafile=os.environ.get("NBE_CA_BUNDLE") or None)
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60 if isinstance(body, bytes) else 20) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = e.reason
        if e.code == 401:
            msg = f"{msg} - log in again: python nbe_node.py login {server}"
        elif e.code == 403:
            msg = f"{msg} - node management needs an admin account"
        raise Fail(f"server said {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise Fail(f"can't reach {server}: {e.reason}") from None


# ---------------------------------------------------------------- sdkconfig handling
def kconfig_lines(vals: dict) -> dict[str, str]:
    out = {}
    for k, v in vals.items():
        if v is True:
            out[k] = f"CONFIG_{k}=y"
        elif v is False:
            out[k] = f"# CONFIG_{k} is not set"
        else:
            esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
            out[k] = f'CONFIG_{k}="{esc}"'
    return out


def read_sdkconfig_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith(f"CONFIG_{key}="):
            v = line.split("=", 1)[1]
            return v[1:-1].replace('\\"', '"').replace("\\\\", "\\") if v.startswith('"') else v
    return None


def patch_sdkconfig(path: Path, vals: dict) -> int:
    """Set values in an existing sdkconfig (incl. choice members); returns lines changed."""
    lines = path.read_text().splitlines()
    want = kconfig_lines(vals)
    seen, changed = set(), 0
    for i, line in enumerate(lines):
        for k, new in want.items():
            if line.startswith(f"CONFIG_{k}=") or line == f"# CONFIG_{k} is not set":
                seen.add(k)
                if line != new:
                    lines[i], changed = new, changed + 1
    for k, new in want.items():
        if k not in seen:
            lines.append(new)
            changed += 1
    path.write_text("\n".join(lines) + "\n")
    return changed


def node_values(b: dict, ssid: str, password: str, eap_user: str = "", eap_identity: str = "",
                eap_domain: str = "", fallbacks: tuple = ()) -> dict:
    eap = {
        # WPA2-Enterprise PEAP (UCSD-PROTECTED, eduroam...): personal login, password = PPG_WIFI_PASSWORD
        "PPG_WIFI_AUTH_EAP": True, "PPG_WIFI_AUTH_PSK": False, "PPG_WIFI_EAP_USERNAME": eap_user,
        "PPG_WIFI_EAP_IDENTITY": eap_identity or eap_user, "PPG_WIFI_EAP_VALIDATE_SERVER": True,
        "PPG_WIFI_EAP_DOMAIN": eap_domain,
    } if eap_user else {"PPG_WIFI_AUTH_PSK": True, "PPG_WIFI_AUTH_EAP": False}
    return eap | {
        "PPG_DEVICE_ID": b["node_id"], "PPG_SITE": b.get("site") or "finger",
        "PPG_LINK_MQTT": True, "PPG_LINK_USB_SERIAL_JTAG": False, "PPG_LINK_UART": False,
        "PPG_MQTT_BROKER_URI": b["broker_uri"], "PPG_MQTT_TOPIC_PREFIX": b["topic_prefix"],
        "PPG_MQTT_CA_BUNDLE": b["broker_ca_mode"] == "bundle", "PPG_MQTT_CA_PEM": b["broker_ca_mode"] == "pem",
        "PPG_WIFI_SSID": ssid, "PPG_WIFI_PASSWORD": password,
        # fallback networks 2 and 3 (PSK/open), tried when the first isn't found; "" = none
        **{f"PPG_WIFI_{k}{i}": v for i, fb in enumerate((tuple(fallbacks) + (("", ""),) * 2)[:2], start=2)
           for k, v in zip(("SSID", "PASSWORD"), fb)},
        "PPG_OTA_URL": b.get("ota_url") or "",  # empty with an old server: no over-the-air updates
        # USB-C no longer carries data: put the log console there so `idf.py monitor` shows Wi-Fi/MQTT logs
        "ESP_CONSOLE_USB_SERIAL_JTAG": True, "ESP_CONSOLE_UART_DEFAULT": False,
    }


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


# ---------------------------------------------------------------- commands
def cmd_login(a):
    server = a.server.rstrip("/")
    if not server.startswith("https://") and not a.allow_http:
        raise Fail("use the https:// address of the server (or --allow-http for a local test server)")
    conf = {"server": server}
    if a.token:
        conf["token"] = a.token
    else:
        user = a.user or input("username: ").strip()
        pw = getpass.getpass("password: ")
        import socket
        r = api("POST", "/api/auth/token", {"username": user, "password": pw,
                                            "label": f"nbe-node on {socket.gethostname()}"}, conf=conf, auth=False)
        conf["token"] = r["token"]
    me = api("GET", "/api/me", conf=conf)
    conf["user"] = me["username"]
    save_conf(conf)
    print(f"logged in to {server} as {me['username']} ({me['role']}); token saved in {CONF}")
    if me["role"] != "admin":
        print("note: registering nodes and fetching credentials needs the admin role")


def cmd_whoami(a):
    c = load_conf()
    me = api("GET", "/api/me")
    print(f"{me['username']} ({me['role']}) @ {c['server']}")


def cmd_list(a):
    r = api("GET", "/api/nodes")
    rows = [("NODE", "SITE", "ENABLED", "STATE", "RSSI", "FW", "IMAGE", "CERT EXPIRES", "NAME")]
    for n in r["nodes"]:
        live = n.get("live") or {}
        state = "in trial" if n.get("busy") else live.get("state") or ("online" if live.get("online") else "offline")
        rows.append((n["node_id"], n["site"] or "", "yes" if n["enabled"] else "no", state, str(live.get("rssi", "")),
                     " ".join(x for x in (live.get("fw", ""), live.get("build", ""), live.get("ota", "")) if x),
                     " ".join((n.get("image") or {}).get(k, "") for k in ("version", "build")),
                     (n["cert_expires"] or "")[:10], n["name"] or ""))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(x).ljust(w[i]) for i, x in enumerate(row)))
    if r.get("unregistered_seen"):
        print("seen on the broker but not registered:", ", ".join(r["unregistered_seen"]))


def cmd_register(a):
    n = api("POST", "/api/nodes", {"node_id": a.node_id, "site": a.site, "name": a.name or "", "notes": a.notes or ""})
    print(f"registered {n['node_id']} (site {n['site']}); next: python nbe_node.py configure {n['node_id']}")


def cmd_rotate(a):
    n = api("POST", f"/api/nodes/{a.node_id.upper()}/rotate")
    print(f"new certificate for {n['node_id']} (expires {n['cert_expires'][:10]}); re-run configure and re-flash")


def cmd_configure(a):
    proj = Path(a.project).resolve()
    if not (proj / "main" / "Kconfig.projbuild").exists():
        raise Fail(f"{proj} is not the ppg_stream ESP-IDF project (use --project)")
    node = a.node_id.upper()
    try:
        b = api("GET", f"/api/nodes/{node}/credentials")
    except Fail as e:
        if "404" in str(e) and a.register:
            api("POST", "/api/nodes", {"node_id": node, "site": a.site or "finger"})
            print(f"registered {node}")
            b = api("GET", f"/api/nodes/{node}/credentials")
        else:
            raise
    if not b["broker_uri"]:
        raise Fail("the server has no broker address configured ([provisioning] broker_uri)")
    if a.site and a.site != b["site"]:
        print(f"note: the server has site={b['site']!r} for {node}; using that (change it on the Nodes page)")

    # Wi-Fi: flags > current values in sdkconfig > ask
    sdk = proj / "sdkconfig"
    cur_ssid = read_sdkconfig_value(sdk, "PPG_WIFI_SSID") or read_sdkconfig_value(proj / "sdkconfig.nbe", "PPG_WIFI_SSID")
    cur_pw = read_sdkconfig_value(sdk, "PPG_WIFI_PASSWORD") or read_sdkconfig_value(proj / "sdkconfig.nbe", "PPG_WIFI_PASSWORD")
    ssid = a.wifi_ssid
    if ssid is None:
        ssid = input(f"Wi-Fi SSID [{cur_ssid or 'UCSD-DEVICE'}]: ").strip() or cur_ssid or "UCSD-DEVICE"
    pw = a.wifi_password if a.wifi_password is not None else os.environ.get("NBE_WIFI_PASSWORD")
    if pw is None:
        hint = " (Enter = keep current)" if cur_pw and ssid == cur_ssid else " (Enter = open network)"
        pw = getpass.getpass(f"Wi-Fi password for {ssid}{hint}: ")
        if not pw and cur_pw and ssid == cur_ssid:
            pw = cur_pw
    # fallback networks: flags > current sdkconfig (--wifi2-ssid "" removes one)
    fallbacks = []
    for i in (2, 3):
        cur = [read_sdkconfig_value(sdk, f"PPG_WIFI_{k}{i}") or read_sdkconfig_value(proj / "sdkconfig.nbe", f"PPG_WIFI_{k}{i}")
               for k in ("SSID", "PASSWORD")]
        fs = getattr(a, f"wifi{i}_ssid")
        fs = cur[0] or "" if fs is None else fs
        fp = getattr(a, f"wifi{i}_password")
        fp = fp if fp is not None else os.environ.get(f"NBE_WIFI{i}_PASSWORD")
        if fp is None and fs:
            fp = cur[1] if fs == cur[0] and cur[1] else getpass.getpass(f"Wi-Fi password for {fs} (Enter = open network): ")
        fallbacks.append((fs, fp or "" if fs else ""))

    certs = proj / "main" / "certs"
    write_private(certs / "client.crt", b["client_cert"])
    write_private(certs / "client.key", b["client_key"])
    if b["broker_ca_mode"] == "pem":
        (certs / "broker_ca.crt").write_text(b["broker_ca_pem"])
    elif (certs / "broker_ca.crt").exists():
        (certs / "broker_ca.crt").unlink()
    vals = node_values(b, ssid, pw, a.wifi_eap_user or "", a.wifi_eap_identity or "", a.wifi_eap_domain or "",
                       tuple(fallbacks))
    text = ["# Written by firmware/tools/nbe_node.py for node " + node + " - do not commit (Wi-Fi password)."]
    text += kconfig_lines(vals).values()
    write_private(proj / "sdkconfig.nbe", "\n".join(text) + "\n")
    changed = patch_sdkconfig(sdk, vals) if sdk.exists() else None

    print(f"node {node}: site={b['site']} broker={b['broker_uri']} (CA: {b['broker_ca_mode']}) "
          f"wifi={ssid} ({'PEAP as ' + a.wifi_eap_user if a.wifi_eap_user else 'PSK/open'}) "
          + "".join(f"then {fs} " for fs, _ in fallbacks if fs) +
          f"cert expires {str(b['cert_expires'])[:10]}")
    print(f"  wrote main/certs/client.crt, client.key, sdkconfig.nbe in {proj}")
    print(f"  {'updated sdkconfig (' + str(changed) + ' lines)' if changed is not None else 'sdkconfig will be created from sdkconfig.defaults + sdkconfig.nbe'}")

    if a.build or a.flash:
        idf = shutil.which("idf.py")
        if not idf:
            raise Fail("idf.py not found: open the ESP-IDF terminal (or run export.sh / export.ps1) and run:\n"
                       f"  cd {proj}\n  idf.py build flash")
        cmd = ["idf.py"] + (["-p", a.port] if a.port else []) + ["build"] + (["flash"] if a.flash else [])
        print("+", " ".join(cmd))
        r = subprocess.run(cmd, cwd=proj, shell=os.name == "nt")
        if r.returncode:
            raise Fail("build/flash failed (see above)")
        if a.flash:
            print(f"flashed {node}. Watch it connect: idf.py {'-p ' + a.port + ' ' if a.port else ''}monitor  "
                  "(Ctrl-] to quit) - it should appear online on the Devices page within ~15 s")
    else:
        print(f"next:  cd {proj}  &&  idf.py build flash")


def cmd_upload(a):
    node = a.node_id.upper()
    data = Path(a.image).read_bytes()
    m = api("PUT", f"/api/nodes/{node}/firmware", data)
    print(f"{node}: stored {m['version']} build {m['build']} ({m['size']} bytes) on the server")
    if a.ota:
        cmd_ota(a)
    else:
        print(f"install it: Update on the Nodes page, or python nbe_node.py ota {node}")


def cmd_ota(a):
    node = a.node_id.upper()
    r = api("POST", f"/api/nodes/{node}/ota")
    img = r["image"]
    print(f"{node}: told to install {img['version']} build {img['build']}; it reboots in about a minute "
          "(progress: python nbe_node.py list)")


def cmd_mac(a):
    exe = shutil.which("esptool.py") or shutil.which("esptool")
    cmd = [exe] if exe else [sys.executable, "-m", "esptool"]
    cmd += (["--port", a.port] if a.port else []) + ["read_mac"]
    print("+", " ".join(cmd), "\n(for UCSD-DEVICE register the Wi-Fi MAC, i.e. the 'MAC:' line)")
    sys.exit(subprocess.call(cmd))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="nbe_node.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("login", help="log in to the NBE server (stores an API token)")
    s.add_argument("server"); s.add_argument("--user"); s.add_argument("--token", help="paste a token from the web Account page")
    s.add_argument("--allow-http", action="store_true")
    s.set_defaults(func=cmd_login)
    sub.add_parser("whoami").set_defaults(func=cmd_whoami)
    sub.add_parser("list", help="registered nodes").set_defaults(func=cmd_list)
    s = sub.add_parser("register", help="register a new node")
    s.add_argument("node_id"); s.add_argument("--site", default="finger"); s.add_argument("--name"); s.add_argument("--notes")
    s.set_defaults(func=cmd_register)
    s = sub.add_parser("rotate", help="issue a new certificate for a node (re-flash afterwards)")
    s.add_argument("node_id"); s.set_defaults(func=cmd_rotate)
    s = sub.add_parser("configure", help="write a node's credentials + settings into the ESP-IDF project")
    s.add_argument("node_id")
    s.add_argument("--project", default=str(DEFAULT_PROJECT))
    s.add_argument("--wifi-ssid"); s.add_argument("--wifi-password", help="or env NBE_WIFI_PASSWORD (else asked)")
    s.add_argument("--wifi-eap-user", help="WPA2-Enterprise PEAP username (UCSD-PROTECTED: your UCSD login)")
    s.add_argument("--wifi-eap-identity", help="PEAP outer identity (default: the username)")
    s.add_argument("--wifi-eap-domain", help="pin the RADIUS server certificate name (default: CA check only)")
    for i in (2, 3):
        s.add_argument(f"--wifi{i}-ssid", help=f"fallback network {i}, tried when the ones before it aren't found "
                                               "(PSK/open; default: keep current, \"\" = none)")
        s.add_argument(f"--wifi{i}-password", help=f"or env NBE_WIFI{i}_PASSWORD (else asked)")
    s.add_argument("--register", action="store_true", help="register the node first if it doesn't exist")
    s.add_argument("--site", help="with --register")
    s.add_argument("--build", action="store_true", help="run idf.py build afterwards")
    s.add_argument("--flash", action="store_true", help="run idf.py build flash afterwards")
    s.add_argument("-p", "--port", help="serial port for flashing, e.g. COM5 or /dev/ttyACM0")
    s.set_defaults(func=cmd_configure)
    s = sub.add_parser("upload", help="store a node's firmware image (build/ppg_stream.bin) for OTA")
    s.add_argument("node_id"); s.add_argument("image")
    s.add_argument("--ota", action="store_true", help="and install it right away")
    s.set_defaults(func=cmd_upload)
    s = sub.add_parser("ota", help="install the uploaded image on the node over Wi-Fi")
    s.add_argument("node_id"); s.set_defaults(func=cmd_ota)
    s = sub.add_parser("mac", help="print the board's MAC address (esptool)")
    s.add_argument("-p", "--port"); s.set_defaults(func=cmd_mac)
    a = p.parse_args(argv)
    try:
        a.func(a)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
