"""Firmware images for over-the-air (OTA) node updates.

    <pki_dir>/firmware/<ID>.bin    the node's app image (build/ppg_stream.bin, not the merged flash image)
    <pki_dir>/firmware/<ID>.json   what is in it: version, build (ELF SHA-256 prefix), size, uploader

Images are per node: each one embeds that node's client key and the Wi-Fi password, so they are
stored next to the node keys (0600) and only ever handed out to the node itself.

Update flow (admin presses "Update" or runs `nbe_node.py ota <ID>`):

    server -> nbe/dev/<ID>/cmd   "OTA <token>"          token: random, single node, valid OTA_TOKEN_S
    node   -> GET /ota/<token>   HTTPS through nginx    no login: the token is the credential, and it
                                                        only travels over the node's mTLS MQTT link
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

IMAGE_MAGIC = 0xE9
APP_DESC_MAGIC = 0xABCD5432
APP_DESC_OFFSET = 0x20  # esp_image_header_t (24) + first segment header (8)
MAX_IMAGE = 0x3E0000  # app slot size in firmware/esp-idf/ppg_stream/partitions.csv
PROJECT = "ppg_stream"
OTA_TOKEN_S = 600


class FirmwareError(ValueError):
    pass


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def parse_image(data: bytes) -> dict:
    """Read esp_app_desc_t from an ESP-IDF app image."""
    o = APP_DESC_OFFSET
    if len(data) < o + 256 or data[0] != IMAGE_MAGIC or struct.unpack_from("<I", data, o)[0] != APP_DESC_MAGIC:
        raise FirmwareError("not an ESP-IDF app image - upload build/ppg_stream.bin (the merged flash image "
                            "is for USB flashing only)")
    return {"version": _cstr(data[o + 16:o + 48]), "project": _cstr(data[o + 48:o + 80]),
            "built": f"{_cstr(data[o + 96:o + 112])} {_cstr(data[o + 80:o + 96])}".strip(),
            "idf": _cstr(data[o + 112:o + 144]), "build": data[o + 144:o + 148].hex(),
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class FirmwareStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}  # token -> (node_id, expires)

    def _paths(self, node_id: str) -> tuple[Path, Path]:
        return self.dir / f"{node_id}.bin", self.dir / f"{node_id}.json"

    def put(self, node_id: str, data: bytes, client_cert_pem: str, by: str = "") -> dict:
        """Store a node's image after checking it really is that node's build."""
        meta = parse_image(data)
        if meta["project"] != PROJECT:
            raise FirmwareError(f"image is {meta['project']!r}, not {PROJECT}")
        if len(data) > MAX_IMAGE:
            raise FirmwareError(f"image is {len(data)} bytes, the app slot holds {MAX_IMAGE}")
        # the node certificate is embedded verbatim (EMBED_TXTFILES): catches another node's image,
        # and one built before the certificate was rotated (it could no longer reach the broker)
        if client_cert_pem.strip().encode() not in data:
            raise FirmwareError(f"this image doesn't contain {node_id}'s current certificate - it was built for "
                                "another node, or before the certificate was rotated")
        meta.update(node_id=node_id, uploaded_by=by,
                    uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.dir.mkdir(parents=True, exist_ok=True)
        bin_p, meta_p = self._paths(node_id)
        for p, payload in ((bin_p, data), (meta_p, json.dumps(meta, indent=2).encode())):
            tmp = p.with_suffix(p.suffix + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, p)
        return meta

    def info(self, node_id: str) -> dict | None:
        bin_p, meta_p = self._paths(node_id)
        if not bin_p.exists():
            return None
        try:
            return json.loads(meta_p.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def issue_token(self, node_id: str) -> str:
        tok = secrets.token_hex(16).upper()  # hex: the node's command parser upper-cases everything
        now = time.time()
        with self.lock:
            self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}
            self._tokens[tok] = (node_id, now + OTA_TOKEN_S)
        return tok

    def resolve(self, token: str) -> tuple[str, Path] | None:
        """(node_id, image) for a valid token. It stays valid until it expires so the node can retry."""
        with self.lock:
            hit = self._tokens.get(token.strip().upper())
        if not hit or hit[1] < time.time():
            return None
        p = self._paths(hit[0])[0]
        return (hit[0], p) if p.exists() else None
