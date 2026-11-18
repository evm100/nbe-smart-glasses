"""USB gateway downloads: one zip per node and operating system, ready to unzip and run.

    NBE-Gateway-<NODE>-<platform>/
        Start Gateway.bat | Start Gateway.command | start-gateway.sh     double-click / run this
        gateway.json            node ID + broker address (sglab.provision values)
        certs/client.crt|key    the node's own certificate: the gateway IS the node on MQTT
        certs/ca_bundle.pem     public CAs for the broker's TLS certificate (or the private broker CA)
        app/                    nbe_gateway + pyserial + paho-mqtt (pure Python)
        python/                 a standalone CPython (python-build-standalone); not in "linux-any"
        README.txt

Nothing has to be installed on the operator's computer. The Python runtimes are downloaded once
from GitHub (pinned release, SHA-256 checked against the release's SHA256SUMS) and cached in
[gateway] cache_dir; pyserial / paho-mqtt come from this server's own environment, or from PyPI
(hash-checked) if they aren't installed here.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import ssl
import stat
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import provision

PBS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{name}"
PLATFORMS = {
    # key: (label, python-build-standalone target triple or None = the computer's own python3, launcher)
    "windows-x64": ("Windows (64-bit)", "x86_64-pc-windows-msvc", "bat"),
    "macos-arm64": ("macOS, Apple silicon (M1/M2/M3/M4)", "aarch64-apple-darwin", "command"),
    "macos-x64": ("macOS, Intel", "x86_64-apple-darwin", "command"),
    "linux-x64": ("Linux / Ubuntu (64-bit PC)", "x86_64-unknown-linux-gnu", "sh"),
    "linux-arm64": ("Raspberry Pi (64-bit OS) / Linux ARM64", "aarch64-unknown-linux-gnu", "sh"),
    "linux-any": ("Any Linux with Python 3.8+ installed (smallest)", None, "sh"),
}
# vendored pure-Python packages: import name -> (PyPI project, version)
VENDOR = {"serial": ("pyserial", "3.5"), "paho": ("paho-mqtt", "2.1.0")}
# parts of the runtime the gateway never uses (about half its size)
SKIP_PARTS = {"test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip", "pydoc_data", "lib2to3",
              "pip", "setuptools", "__pycache__", "tcl", "tk", "share", "include"}
SKIP_PREFIXES = ("tcl8", "tk8", "libtcl", "libtk", "tcl86", "tk86", "_tkinter", "itcl", "thread2")


class BundleError(RuntimeError):
    pass


# ---------------------------------------------------------------- downloads (cached)
def _get(url: str, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sglab-gateway-bundle"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return r.read()


def runtime_archive(cfg: dict, triple: str) -> Path:
    """The standalone Python for `triple` (downloaded and SHA-256 checked once)."""
    rel, ver = cfg["python_release"], cfg["python_version"]
    name = f"cpython-{ver}+{rel}-{triple}-install_only_stripped.tar.gz"
    cache = Path(cfg["cache_dir"])
    path = cache / name
    if path.exists():
        return path
    cache.mkdir(parents=True, exist_ok=True)
    try:
        sums = _get(PBS_URL.format(release=rel, name="SHA256SUMS")).decode()
        want = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(name)), None)
        if not want:
            raise BundleError(f"{name} is not in release {rel} (check [gateway] python_release / python_version)")
        data = _get(PBS_URL.format(release=rel, name=urllib.parse.quote(name)), timeout=600)
    except OSError as e:
        raise BundleError(f"could not download the Python runtime {name}: {e}") from e
    if hashlib.sha256(data).hexdigest() != want:
        raise BundleError(f"{name}: SHA-256 mismatch - download corrupted or tampered with")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def vendor_dir(cfg: dict, import_name: str) -> Path:
    """Folder of a pure-Python package to copy into app/: installed here, else its PyPI wheel."""
    try:
        mod = __import__(import_name)
        return Path(mod.__file__).parent if getattr(mod, "__file__", None) else Path(list(mod.__path__)[0])
    except ImportError:
        pass
    project, version = VENDOR[import_name]
    out = Path(cfg["cache_dir"]) / f"{project}-{version}"
    if (out / import_name).is_dir():
        return out / import_name
    try:
        meta = json.loads(_get(f"https://pypi.org/pypi/{project}/{version}/json"))
        whl = next(u for u in meta["urls"] if u["filename"].endswith("py3-none-any.whl")
                   or u["filename"].endswith("py2.py3-none-any.whl"))
        data = _get(whl["url"])
    except (OSError, StopIteration, KeyError, ValueError) as e:
        raise BundleError(f"could not get {project} {version} from PyPI: {e}") from e
    if hashlib.sha256(data).hexdigest() != whl["digests"]["sha256"]:
        raise BundleError(f"{project} wheel: SHA-256 mismatch")
    tmp = out.with_name(out.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.startswith(import_name + "/"):
                z.extract(n, tmp)
    shutil.rmtree(out, ignore_errors=True)
    tmp.replace(out)
    return out / import_name


def ca_bundle_pem(b: dict) -> str | None:
    """The broker CA: the private PEM nodes get, else this server's public CA list (Python on macOS
    has none of its own; Windows and Linux would find theirs, but one file is simpler)."""
    if b.get("broker_ca_pem"):
        return b["broker_ca_pem"]
    paths = ssl.get_default_verify_paths()
    for p in (paths.cafile, paths.openssl_cafile, "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
              "/etc/ssl/cert.pem"):
        if p and Path(p).is_file():
            return Path(p).read_text()
    try:
        import certifi

        return Path(certifi.where()).read_text()
    except ImportError:
        return None


# ---------------------------------------------------------------- launchers
def _launchers(kind: str, node: str, runtime: bool) -> dict[str, tuple[str, bool]]:
    """{file name: (text, executable)}"""
    if kind == "bat":
        return {"Start Gateway.bat": ("@echo off\r\n"
                                      f"title NBE gateway {node}\r\n"
                                      "cd /d \"%~dp0\"\r\n"
                                      "set PYTHONPATH=%~dp0app\r\n"
                                      "\"%~dp0python\\python.exe\" -m nbe_gateway %*\r\n"
                                      "pause\r\n", False)}
    py = "./python/bin/python3" if runtime else "python3"
    body = ("#!/bin/bash\n"
            "# NBE USB gateway - keep this window open while recording\n"
            "cd \"$(dirname \"$0\")\" || exit 1\n")
    if kind == "command":
        # downloaded files are quarantined: clear it for the bundled Python once this script was allowed to run
        body += "xattr -dr com.apple.quarantine . 2>/dev/null\n"
    body += f"PYTHONPATH=\"$PWD/app\" exec {py} -m nbe_gateway \"$@\"\n"
    out = {("Start Gateway.command" if kind == "command" else "start-gateway.sh"): (body, True)}
    if kind == "sh":
        out["nbe-gateway.service"] = (
            "# Run the gateway at boot on a headless Linux box / Raspberry Pi:\n"
            "#   sudo cp nbe-gateway.service /etc/systemd/system/ && sudo nano /etc/systemd/system/nbe-gateway.service\n"
            "#   (fix User= and the paths), then: sudo systemctl enable --now nbe-gateway\n"
            "[Unit]\nDescription=NBE USB gateway\nAfter=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nUser=pi\nWorkingDirectory=/home/pi/NBE-Gateway\n"
            "ExecStart=/home/pi/NBE-Gateway/start-gateway.sh --every 60\nRestart=always\nRestartSec=5\n"
            "# 3 = shut down from the NBE website (another gateway needed the node): stay stopped\n"
            "RestartPreventExitStatus=3\n\n"
            "[Install]\nWantedBy=multi-user.target\n", False)
    return out


README = """NBE USB gateway for {node}
==========================

This program connects the {node} sensor board (plugged into this computer's USB port) to the NBE
server at {host}. The board can't reach the internet itself; this computer does it for it.

START IT
  {start}

Keep the window open while you record. You run the recording itself from the NBE website as usual:
{node} shows as online there whenever the gateway runs AND the board is plugged in.

The window prints a status line every few seconds:
  USB ...                the board was found (if not: plug it in / try another cable or port)
  STREAMING / idle       whether a recording is running
  waiting to send ...    data not yet confirmed by the server (it catches up by itself after an
                         internet drop - nothing is lost while the gateway keeps running)
  stored ...             the local copy (folder data/ next to this file, kept up to {keep} GB)

{platform_notes}
IF SOMETHING IS WRONG
  - "server: cannot reach ..." for long: check this computer's internet; firewalls must allow
    outgoing port {port}. Start it with --verbose to see the connection details.
  - "no board": unplug and replug the board; close other programs that use the serial port
    (Arduino IDE serial monitor, another gateway window).
  - "Shut down from the NBE server": only one gateway can run per node, and someone shut this
    one down from the website's Devices page because another computer needed {node}. Start it
    again when it's your turn to record. Please close the gateway when you finish recording.
  - Several boards on one computer: run one gateway per board with --port, e.g.
    {start_port}

KEEP THIS FOLDER PRIVATE
  certs/client.key identifies {node} to the server. Don't share it; if it leaks, an admin rotates
  the node's certificate on the server's Nodes page and you download a new gateway.

Built {built} by the NBE server for {platform_label}. Gateway version {version}.
"""

NOTES = {
    "bat": ("WINDOWS\n  If Windows SmartScreen warns about the file: More info -> Run anyway (first time only).\n"
            "  Unzip the folder first (right-click -> Extract All); don't run it from inside the zip.\n"),
    "command": ("MACOS\n  The first time, macOS blocks scripts from the internet: right-click 'Start Gateway.command' ->\n"
                "  Open -> Open. (macOS 15+: try to open it once, then System Settings -> Privacy & Security ->\n"
                "  'Open Anyway'.) Or in Terminal:  bash ~/Downloads/{folder}/'Start Gateway.command'\n"),
    "sh": ("LINUX / RASPBERRY PI\n  Your user needs access to serial ports, once:  sudo usermod -aG dialout $USER  (then log out\n"
           "  and back in). To start it at boot on a headless Pi, see nbe-gateway.service.\n"),
}


# ---------------------------------------------------------------- zip helpers
def _add(z: zipfile.ZipFile, name: str, data: bytes | str, mode: int = 0o644) -> None:
    zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (stat.S_IFREG | mode) << 16
    z.writestr(zi, data)


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in SKIP_PARTS for p in parts[:-1]) or parts[-1].lower().startswith(SKIP_PREFIXES) \
        or parts[-1].endswith((".pyc", ".a", ".lib", ".pdb"))


def _add_runtime(z: zipfile.ZipFile, top: str, archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as t:
        for m in t:
            if not m.name.startswith("python/") or _skip(m.name[len("python/"):]):
                continue
            name = f"{top}/{m.name}"
            if m.issym():  # macOS / Linux runtimes link python3 -> python3.12 etc.
                zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, m.linkname)
            elif m.isfile():
                f = t.extractfile(m)
                _add(z, name, f.read() if f else b"", 0o755 if m.mode & 0o111 else 0o644)


def _add_tree(z: zipfile.ZipFile, dest: str, src: Path) -> None:
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if p.is_file() and "__pycache__" not in rel and not rel.endswith(".pyc"):
            _add(z, f"{dest}/{rel}", p.read_bytes())


# ---------------------------------------------------------------- the bundle
def build(settings, registry, node_id: str, platform: str, vendor: dict[str, Path] | None = None,
          runtime: Path | None = None) -> tuple[str, bytes]:
    """(file name, zip bytes). `vendor` / `runtime` override the downloads (tests)."""
    if platform not in PLATFORMS:
        raise BundleError(f"unknown platform {platform!r} (one of {', '.join(PLATFORMS)})")
    import nbe_gateway
    from datetime import datetime, timezone

    label, triple, kind = PLATFORMS[platform]
    cfg = settings.gateway
    b = provision.bundle(settings, registry, node_id)
    uri = cfg["broker_uri"]
    u = urllib.parse.urlsplit(uri if "://" in uri else "mqtts://" + uri)
    if not u.hostname:
        raise BundleError("no broker address: set [gateway] or [provisioning] broker_uri on the server")
    port = u.port or 8883
    node = b["node_id"]
    top = f"NBE-Gateway-{node}-{platform}"
    ca = ca_bundle_pem(b)
    gw_cfg = {"node_id": node, "site": b["site"], "broker_host": u.hostname, "broker_port": port,
              "topic_prefix": b["topic_prefix"], "cert_file": "certs/client.crt", "key_file": "certs/client.key",
              "ca_file": "certs/ca_bundle.pem" if ca else None, "serial_port": "auto", "data_dir": "data",
              "keep_gb": cfg["keep_gb"]}
    launch = _launchers(kind, node, triple is not None)
    start_name = next(iter(launch))
    start = {"bat": f"Double-click '{start_name}'.", "command": f"Double-click '{start_name}' (see MACOS below).",
             "sh": f"In a terminal in this folder:  ./{start_name}"}[kind]
    start_port = {"bat": f"'{start_name}' --port COM5", "command": f"'./{start_name}' --port /dev/cu.usbmodem1101",
                  "sh": f"./{start_name} --port /dev/ttyACM1"}[kind]
    readme = README.format(node=node, host=u.hostname, port=port, start=start, start_port=start_port,
                           keep=cfg["keep_gb"], platform_notes=NOTES[kind].format(folder=top), platform_label=label,
                           built=datetime.now(timezone.utc).strftime("%Y-%m-%d"), version=nbe_gateway.__version__)
    if kind == "bat":
        readme = readme.replace("\n", "\r\n")

    vendor = vendor or {name: vendor_dir(cfg, name) for name in VENDOR}
    rt = runtime if runtime is not None else (runtime_archive(cfg, triple) if triple else None)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, (text, exe) in launch.items():
            _add(z, f"{top}/{name}", text, 0o755 if exe else 0o644)
        _add(z, f"{top}/README.txt", readme)
        _add(z, f"{top}/gateway.json", json.dumps(gw_cfg, indent=2) + "\n")
        _add(z, f"{top}/certs/client.crt", b["client_cert"])
        _add(z, f"{top}/certs/client.key", b["client_key"], 0o600)
        if ca:
            _add(z, f"{top}/certs/ca_bundle.pem", ca)
        _add_tree(z, f"{top}/app/nbe_gateway", Path(nbe_gateway.__file__).parent)
        for name, src in vendor.items():
            _add_tree(z, f"{top}/app/{name}", Path(src))
        if rt is not None:
            _add_runtime(z, top, rt)
    return f"{top}.zip", buf.getvalue()
