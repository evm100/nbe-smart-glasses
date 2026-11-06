"""Protocol v2 camera streams and the USB gateway (nbe_gateway), end to end:

    board bytes (USB envelopes) -> gateway Board -> RecordLog (disk) -> Uplink (NB frames, lossy link)
    -> server MqttHub / FrameReceiver -> MqttSource -> Recording -> .frames + .csv + session.json
"""
import io
import json
import random
import shutil
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

from nbe_gateway.store import RecordLog
from nbe_gateway.uplink import Uplink
from nbe_gateway.usb import Board
from nbe_gateway.wire import (FMT_GRAY8, EnvelopeDecoder, VideoFrame, envelope, frame_record, split_records,
                              text_record)
from sglab.framing import record_items
from sglab.protocol import encode
from sglab.video import gray_array, read_frames

REPO = Path(__file__).resolve().parents[1]
W, H = 32, 24


def _px(seq: int) -> bytes:
    return bytes(((seq * 5 + x + 2 * y) & 0xFF) for y in range(H) for x in range(W))


def board_stream(n_frames=40, samples_per_frame=13, dev="GLS03", seed=1) -> tuple[bytes, int]:
    """What a camera board sends after START: envelopes of text, D lines and frames."""
    rnd = random.Random(seed)
    lines = [encode(f"H,proto=1,dev={dev},fw=0.7.0-idf,sensor=MAX30102,fs=200,ch=ir|red,site=glasses,sensor_ok=1"),
             encode(f"H,proto=2,dev={dev},fw=0.7.0-idf,sid=1,stream=cam,kind=video,sensor=OV2640,ch=frame,fs=15,"
                    f"w={W},h={H},fmt=gray8,aec=0,exposure=300,gain=2,site=glasses,sensor_ok=1"),
             encode("E,1000000,START,")]
    out = [envelope(b"".join(text_record(l) for l in lines))]
    seq = 0
    t = 1_000_000
    for f in range(n_frames):
        recs = b""
        for _ in range(samples_per_frame):
            recs += text_record(encode(f"D,{seq},{t},{100000 + rnd.randint(-50, 50)},{90000 + seq % 7}"))
            seq += 1
            t += 5000
        if f % 15 == 14:
            recs += text_record(encode(f"S,{t},fs_meas=200.0,cam_fps=15.0,cam_drop=0"))
        out.append(envelope(recs))
        out.append(envelope(frame_record(VideoFrame(1, f, t, W, H, FMT_GRAY8, 300, 2, _px(f)))))
    return b"".join(out), seq


def chunks(data: bytes, rnd: random.Random):
    i = 0
    while i < len(data):
        n = rnd.randint(1, 5000)
        yield data[i:i + n]
        i += n


# ---------------------------------------------------------------- wire
def test_envelope_decoder_chunks_text_and_torn_envelopes():
    data, _ = board_stream(n_frames=10)
    ref = [r for _k, r in EnvelopeDecoder().feed(data)]
    cut = sum(10 + len(r) for r in ref[:5])  # between two envelopes
    torn = envelope(frame_record(VideoFrame(1, 99, 5, W, H, 0, 0, 0, _px(99))))[:300]  # the board gave up mid-frame
    stream = b"ESP-ROM:esp32s3 boot\r\n" + data[:cut] + torn + data[cut:] + b"\n\x01\x02junk\xff\n"
    dec, recs, text = EnvelopeDecoder(), [], []
    for c in chunks(stream, random.Random(3)):
        for kind, item in dec.feed(c):
            (recs if kind == "records" else text).append(item)
    assert recs == ref  # only the torn envelope is gone; nothing else lost or duplicated
    assert text == ["ESP-ROM:esp32s3 boot"]  # binary leftovers are not "text"
    assert dec.stats["skipped_bytes"] > 0


def test_frame_records_decode():
    fr = VideoFrame(1, 7, 123456789, W, H, FMT_GRAY8, 300, 2, _px(7))
    (_, kind, body), = split_records(frame_record(fr))
    assert record_items(kind, body) == [fr]


# ---------------------------------------------------------------- disk log
def test_record_log_survives_restart_and_prunes(tmp_path):
    log = RecordLog(tmp_path / "GLS03", keep_gb=0)
    recs = [text_record(encode(f"D,{i},{i},1,2")) for i in range(50)]
    blob = b"".join(recs)
    ends, e = [], 0
    for r in recs:
        e += len(r)
        ends.append(e)
    log.append(blob, ends)
    data, end = log.read(0, 100)
    assert end in ends and end <= 100 and data == blob[:end]  # whole records only
    assert log.ack(end)
    boot = log.boot
    log.close()
    # a crash tore the last record: it is cut off on reopen, the rest is still unacknowledged
    seg = next((tmp_path / "GLS03").glob("seg_*.bin"))
    with open(seg, "ab") as f:
        f.write(recs[0][:3])
    log2 = RecordLog(tmp_path / "GLS03", keep_gb=0)
    assert log2.boot == boot and log2.acked == end and log2.head == len(blob)
    rest, end2 = log2.read(log2.acked, 10 ** 9)
    assert rest == blob[end:] and end2 == len(blob)
    log2.ack(end2)
    assert log2.unacked() == 0
    log2.close()


# ---------------------------------------------------------------- end to end
class HubClient:
    """The server's paho client: cmd replies go back to the gateway."""

    def __init__(self):
        self.out = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.out.append((topic, payload if isinstance(payload, bytes) else str(payload).encode()))


class GwClient:
    """The gateway's paho client: data frames go to the server (some lost)."""

    class Info:
        rc = 0

        def wait_for_publish(self, t=None):
            return True

    def __init__(self):
        self.out = []
        self.lock = threading.Lock()

    def publish(self, topic, payload, qos=0, retain=False):
        with self.lock:
            self.out.append((topic, payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()))
        return self.Info()


@pytest.fixture()
def lab(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").unlink(missing_ok=True)
    from sglab.settings import load_settings

    return load_settings(tmp_path)


def test_gateway_to_recording_nothing_lost(lab, tmp_path):
    from sglab.mqtt_hub import MqttHub, set_hub
    from sglab.recorder import Recording

    hub = MqttHub({"host": "x"}, client=HubClient())
    set_hub(hub)
    data, n_samples = board_stream(n_frames=60)
    log = RecordLog(tmp_path / "gw" / "GLS03")
    board = Board(log, "GLS03")
    cfg = {"node_id": "GLS03", "topic_prefix": "nbe", "frame_max_kb": 8, "window_kb": 64, "ack_timeout_s": 0.5}
    gw = GwClient()
    up = Uplink(cfg, log, board.write, board.describe, client=gw, version="test")
    up.connected = True

    stop = threading.Event()
    dropped = set()
    rnd = random.Random(5)

    def bridge():
        while not stop.is_set():
            up._send_frames(time.monotonic())
            with gw.lock:
                out, gw.out = gw.out, []
            for topic, payload in out:
                if topic.endswith("/data"):
                    off = int.from_bytes(payload[8:16], "little")
                    if off not in dropped and rnd.random() < 0.15:  # lose 15 % of first sends
                        dropped.add(off)
                        continue
                    hub.handle(topic, bytes(payload))
            replies, hub.client.out = hub.client.out, []
            for topic, payload in replies:
                up.handle(topic, payload)
            time.sleep(0.002)

    th = threading.Thread(target=bridge, daemon=True)
    th.start()
    try:
        rec = Recording(lab, [{"port": "mqtt://GLS03", "role": "glasses"}])
        t = threading.Thread(target=lambda: setattr(rec, "warn", rec.start({"subject_id": "S001", "protocol": "rest"})))
        t.start()
        time.sleep(0.2)
        for c in chunks(data, random.Random(9)):  # the board on USB, while the trial runs
            board.feed(c)
        t.join(10)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            w = rec.workers[0]
            if w.stats["n_samples"] == n_samples and w.video and next(iter(w.video.values())).stats["n_frames"] == 60:
                break
            time.sleep(0.05)
        live = rec.live_status()[0]["video"][0]
        prev = rec.workers[0].video[1].preview()
        rec.stop()
        dest, doc = rec.finalize({"subject_id": "S001", "protocol": "rest"})
    finally:
        stop.set()
        th.join(2)
        set_hub(None)

    assert dropped, "the test link must have lost frames"
    rx = hub.receivers["GLS03"].stats
    assert rx["gaps"] >= 1 and rx["lost_bytes"] == 0
    assert log.unacked() == 0
    # PPG stream: every sample once
    ppg = (dest / "glasses_GLS03.csv").read_text().splitlines()
    assert len(ppg) - 1 == n_samples
    # camera: every frame, every pixel, on the device clock
    (v,) = doc["video"]
    assert v["stream"] == "glasses_GLS03_cam" and v["w"] == W and v["h"] == H and v["fmt"] == "gray8"
    assert v["stats"]["n_frames"] == 60 and v["stats"]["n_dropped_seq"] == 0
    frames = list(read_frames(dest / v["file"]))
    assert [f.seq for f in frames] == list(range(60))
    assert all(f.data == _px(f.seq) for f in frames)
    arr, idx = gray_array(dest / v["file"])
    assert arr.shape == (60, H, W) and bytes(arr[17].tobytes()) == _px(17) and list(idx["seq"]) == list(range(60))
    assert len((dest / v["index"]).read_text().splitlines()) == 61
    assert doc["streams"][0]["stream"] == "glasses_GLS03" and all(s.get("kind") != "video" for s in doc["streams"])
    assert live["frames"] == 60 and live["dropped"] == 0 and live["w"] == W
    assert prev and prev[1][:2] == b"\xff\xd8"  # a JPEG preview
    # the gateway added a SYNC (device clock <-> this PC's UTC), as Wi-Fi nodes do with SNTP
    assert ",SYNC," in (dest / "glasses_GLS03.rawlog").read_text()


def test_board_tracks_state_and_wrong_board_warning(tmp_path):
    log = RecordLog(tmp_path / "GLS03")
    b = Board(log, "GLS03")
    data, _ = board_stream(n_frames=3, dev="GLS09")
    b.feed(data)
    d = b.describe()
    assert d["streaming"] and d["sensor"] == "MAX30102+OV2640" and d["camera_ok"] and d["fs"] == 200.0
    assert any("GLS09" in w for w in b.warnings)
    assert b.stats["frames"] == 3 and b.stats["frame_seq_gaps"] == 0


# ---------------------------------------------------------------- download bundle
def _fake_runtime(tmp_path: Path, windows: bool) -> Path:
    p = tmp_path / ("rt-win.tar.gz" if windows else "rt-mac.tar.gz")
    with tarfile.open(p, "w:gz") as t:
        def add(name, data=b"x", mode=0o644, link=None):
            ti = tarfile.TarInfo(name)
            if link:
                ti.type, ti.linkname = tarfile.SYMTYPE, link
                t.addfile(ti)
                return
            ti.size, ti.mode = len(data), mode
            t.addfile(ti, io.BytesIO(data))
        if windows:
            add("python/python.exe", b"MZ", 0o755)
            add("python/Lib/os.py")
            add("python/Lib/tkinter/__init__.py")
            add("python/Lib/test/test_x.py")
        else:
            add("python/bin/python3.12", b"\x7fELF", 0o755)
            add("python/bin/python3", link="python3.12")
            add("python/lib/python3.12/os.py")
            add("python/lib/python3.12/idlelib/x.py")
    return p


@pytest.mark.parametrize("platform", ["windows-x64", "macos-arm64", "linux-any"])
def test_gateway_bundle(tmp_path, platform):
    pytest.importorskip("cryptography")
    from sglab import gateway_bundle
    from sglab.nodes import NodeRegistry
    from sglab.pki import PKI

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(f'[pki]\ndir = "{tmp_path / "pki"}"\n[provisioning]\n'
                                                       'broker_uri = "mqtts://mqtt.example.org:8884"\n')
    from sglab.settings import load_settings

    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    PKI(s.pki_dir).init_ca()
    reg = NodeRegistry(s.db_path, s.pki_dir)
    reg.register("GLS03", site="glasses")
    vend = tmp_path / "vendor"
    for pkg in ("serial", "paho/mqtt"):
        (vend / pkg).mkdir(parents=True)
        (vend / pkg / "__init__.py").write_text("# fake\n")
    rt = None if platform == "linux-any" else _fake_runtime(tmp_path, platform.startswith("windows"))
    name, data = gateway_bundle.build(s, reg, "GLS03", platform, vendor={"serial": vend / "serial", "paho": vend / "paho"},
                                      runtime=rt)
    z = zipfile.ZipFile(io.BytesIO(data))
    top = f"NBE-Gateway-GLS03-{platform}"
    assert name == top + ".zip"
    names = set(z.namelist())
    cfg = json.loads(z.read(f"{top}/gateway.json"))
    assert cfg["node_id"] == "GLS03" and cfg["broker_host"] == "mqtt.example.org" and cfg["broker_port"] == 8884
    assert {f"{top}/certs/client.crt", f"{top}/certs/client.key", f"{top}/app/nbe_gateway/app.py",
            f"{top}/app/serial/__init__.py", f"{top}/app/paho/mqtt/__init__.py", f"{top}/README.txt"} <= names
    assert b"BEGIN CERTIFICATE" in z.read(f"{top}/certs/client.crt")
    if platform == "windows-x64":
        assert f"{top}/Start Gateway.bat" in names and f"{top}/python/python.exe" in names
        assert not any("tkinter" in n or "/test/" in n for n in names)  # trimmed
    elif platform == "macos-arm64":
        launcher = z.getinfo(f"{top}/Start Gateway.command")
        assert stat.S_IMODE(launcher.external_attr >> 16) & 0o111
        link = z.getinfo(f"{top}/python/bin/python3")
        assert stat.S_ISLNK(link.external_attr >> 16) and z.read(link) == b"python3.12"
        assert stat.S_IMODE(z.getinfo(f"{top}/python/bin/python3.12").external_attr >> 16) & 0o111
        assert not any("idlelib" in n for n in names)
    else:
        assert f"{top}/start-gateway.sh" in names and not any("/python/" in n for n in names)
        assert b"python3 -m nbe_gateway" in z.read(f"{top}/start-gateway.sh")
    with pytest.raises(gateway_bundle.BundleError):
        gateway_bundle.build(s, reg, "GLS03", "amiga")
