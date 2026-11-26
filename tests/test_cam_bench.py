"""scripts/cam_bench.py: frame-rate / drop / jitter numbers from frame timestamps and the USB stream."""
import importlib.util
import struct
from pathlib import Path

from nbe_gateway.wire import VideoFrame, envelope, frame_record, line, text_record

_spec = importlib.util.spec_from_file_location("cam_bench", Path(__file__).resolve().parents[1] / "scripts" / "cam_bench.py")
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)


def frames_at(n=500, per_us=20000, jitter_us=0, skip_t=(), skip_seq=()):
    """Sensor frames every per_us; skip_t = never got a seq (driver replaced them), skip_seq = had a seq
    but the link lost them."""
    seq, t, s = [], [], 0
    for k in range(n):
        if k in skip_t:
            continue
        if s not in skip_seq:
            seq.append(s)
            t.append(1_000_000 + k * per_us + (jitter_us if k % 2 else -jitter_us))
        s += 1
    return seq, t


def test_clean_50fps():
    r = cb.analyze_frames(*frames_at())
    assert r["fps"] == 50.0 and r["period_ms"] == 20.0 and r["missing"] == 0 and r["drop_pct"] == 0
    assert r["jitter_ms"] == 0 and cb.verdict(r) == "target"


def test_sensor_and_link_drops_are_told_apart():
    r = cb.analyze_frames(*frames_at(skip_t={10, 11, 200}, skip_seq={50}))
    assert r["missing"] == 4 and r["link_drops"] == 1 and r["sensor_miss"] == 3
    assert r["drop_pct"] == round(100 * 4 / 500, 3)
    assert cb.verdict(r) == "target"  # below 1 %
    r = cb.analyze_frames(*frames_at(skip_t=set(range(5, 500, 50)) | set(range(6, 500, 50))))
    assert r["drop_pct"] > 1 and cb.verdict(r) == "below target"


def test_jitter_and_duplicates():
    seq, t = frames_at(jitter_us=500)
    r = cb.analyze_frames(seq + seq[:20], t + t[:20])  # sent twice at a hybrid handover: counted once
    assert r["frames"] == 520 and r["missing"] == 0 and 0.9 < r["jitter_ms"] < 1.1


def test_ppg_gaps_fail_the_run():
    r = cb.analyze_frames(*frames_at())
    r.update(cb.analyze_ppg([0, 1, 2, 4, 5], [0, 8000, 16000, 32000, 40000]))
    assert r["ppg_missing"] == 1 and cb.verdict(r).startswith("FAIL")


def test_collector_reads_usb_envelopes(tmp_path):
    hdr = line("H,proto=2,dev=GLS03,sid=1,stream=cam,kind=video,w=4,h=4,fmt=gray8,mode=cif,fps_sensor=50.0")
    stat = line("S,123,cam_fps=50.0,cam_sfps=50.0,cam_miss=0")
    recs = text_record(hdr) + text_record(stat)
    for s in range(10):
        recs += frame_record(VideoFrame(1, s, 1_000_000 + 20000 * s, 4, 4, 0, 320, 4, bytes(16)))
    save = cb.FramesFile(tmp_path / "x.frames")
    col = cb.Collector(save)
    raw = b"boot banner\n" + envelope(recs)
    for i in range(0, len(raw), 7):  # byte stream in arbitrary pieces
        col.feed(raw[i:i + 7], True)
    save.close()
    assert col.headers["cam"]["mode"] == "cif" and col.status["cam_sfps"] == "50.0"
    assert col.f_seq == list(range(10)) and col.f_bytes == 160 and col.lum == [0.0] * 10
    r = cb.from_file(tmp_path / "x.frames")
    assert r["frames"] == 10 and r["fps"] == 50.0 and r["missing"] == 0
    assert struct.unpack("<8sHH", (tmp_path / "x.frames").read_bytes()[:12]) == (b"NBEVID01", 4, 4)
