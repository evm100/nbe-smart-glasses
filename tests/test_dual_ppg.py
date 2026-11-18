"""Dual-PPG glasses (protocol v2.2, docs/DUAL_PPG.md): a second PPG stream from the same board."""
from __future__ import annotations

import csv
import struct
import zlib

import pytest

from sglab.framing import REC_SAMPLES, REC_SAMPLES_SID, FrameReceiver, _uv, _zze, record_lines
from sglab.processing import SITES, site_family, site_override, stream_configs, pipeline_configs
from sglab.protocol import Measure, Sample, parse_line
from sglab.recorder import Recording

from test_sglab import _record, lab  # noqa: F401  (fixture)


def _samples_body(seq0: int, t0: int, dt: int, rows: list[list[int]]) -> bytes:
    body = bytearray(struct.pack("<H", len(rows)) + _uv(seq0) + _uv(t0) + _uv(dt) + bytes([len(rows[0])])
                     + b"".join(_uv(v) for v in rows[0]))
    for a, b in zip(rows, rows[1:]):
        body += _uv(_zze(0)) + b"".join(_uv(_zze(y - x)) for x, y in zip(a, b))
    return bytes(body)


def _rec(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + _uv(len(body)) + body


def test_samples_sid_record_decodes_to_m_lines():
    rows = [[1000 + 5 * i, 2000 - 3 * i] for i in range(4)]
    lines = record_lines(REC_SAMPLES_SID, bytes([4]) + _samples_body(7, 1_000_000, 2475, rows))
    msgs = [parse_line(ln) for ln in lines]
    assert all(isinstance(m, Measure) and m.sid == 4 for m in msgs)
    assert [m.seq for m in msgs] == [7, 8, 9, 10]
    assert [m.t_us for m in msgs] == [1_000_000 + 2475 * i for i in range(4)]
    assert [[int(v) for v in m.values] for m in msgs] == rows
    # the primary stream's record is unchanged: D lines
    d = [parse_line(ln) for ln in record_lines(REC_SAMPLES, _samples_body(0, 5, 2500, rows))]
    assert all(isinstance(m, Sample) for m in d) and [m.values for m in d] == rows


def test_frame_with_both_sides():
    left = _rec(REC_SAMPLES, _samples_body(0, 100, 2500, [[1], [2], [3]]))
    right = _rec(REC_SAMPLES_SID, bytes([4]) + _samples_body(0, 900, 2475, [[9], [8]]))
    recs = left + right
    frame = struct.pack("<2sBBIQI", b"NB", 2, 0, 0xABCD, 0, zlib.crc32(recs)) + recs
    lines, reply = FrameReceiver().feed(frame, now=0)
    kinds = [type(parse_line(ln)).__name__ for ln in lines]
    assert kinds == ["Sample"] * 3 + ["Measure"] * 2
    assert reply == f"ACK {0xABCD:08x} {len(recs)}"


def test_site_fallback():
    assert site_family("glasses_r") == "glasses" and site_family("glasses_l") == "glasses"
    assert site_family("glasses_r_b") == "glasses" and site_family("finger") == "finger"
    sites = {"glasses": {"layer2": {"x": 1}}, "glasses_r": {"layer2": {"x": 2}}}
    assert site_override(sites, "glasses_l") == {"layer2": {"x": 1}}   # falls back
    assert site_override(sites, "glasses_r") == {"layer2": {"x": 2}}   # its own file wins
    assert site_override(sites, "finger") == {}
    assert {"glasses_l", "glasses_r", "head"} <= set(SITES)


def test_site_fallback_reaches_configs(lab):  # noqa: F811
    s, _ = lab
    from sglab.processing import site_overrides
    b1, b2 = pipeline_configs(s)
    sites = site_overrides(s)
    assert "glasses" in sites
    g = stream_configs(b1, b2, sites, {"role": "glasses"})
    r = stream_configs(b1, b2, sites, {"role": "glasses_r"})
    assert g[0].to_dict() == r[0].to_dict() and g[1].to_dict() == r[1].to_dict()


def test_record_dual_board(lab):  # noqa: F811
    s, cat = lab
    rec = _record(s, ["sim", "sim_dual"], 4.0)
    live = {x["role"]: x for x in rec.live_status()}
    assert set(live) == {"finger", "glasses_l", "glasses_r"}
    assert live["glasses_r"]["samples"] > 250 and live["glasses_r"]["alive"] == live["glasses_l"]["alive"]
    assert [w.role for w in rec.ppg_streams] == ["finger", "glasses_l", "glasses_r"]
    dest, doc = rec.finalize({"subject_id": "S001", "protocol": "rest", "trial": 1, "quality": "good", "keep": True})
    files = {p.name for p in dest.iterdir()}
    assert {"glasses_l_SIMD01.csv", "glasses_r_SIMD01.csv", "glasses_l_SIMD01.rawlog", "finger_SIMF01.csv"} <= files
    assert not any(f.startswith("glasses_r") and f.endswith(".rawlog") for f in files)  # one raw log per board
    by_role = {st["role"]: st for st in doc["streams"]}
    r, left = by_role["glasses_r"], by_role["glasses_l"]
    assert r["sid"] == 4 and r["parent"] == "glasses_l_SIMD01" and r["rawlog"] == "glasses_l_SIMD01.rawlog"
    assert r["header"]["kind"] == "ppg" and r["header"]["site"] == "glasses_r" and r["channels"] == ["ir", "red"]
    assert r["io"]["time_col"] == "t_dev_us"
    for st in (r, left):
        assert st["stats"]["n_samples"] > 250 and st["stats"]["n_dropped_seq"] == 0
    # its own clock: the right sensor runs 1 % fast in the simulator
    assert r["stats"]["n_samples"] > left["stats"]["n_samples"]
    assert r["clock_fit"] is not None
    with open(dest / r["file"]) as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["t_s", "seq", "t_dev_us", "ir", "red"]
    seqs = [int(x[1]) for x in rows[1:]]
    assert seqs == list(range(len(seqs)))
    t = [int(x[2]) for x in rows[1:]]
    assert all(b > a for a, b in zip(t, t[1:]))
    # the catalog and processing see it as an ordinary stream of the session
    sid = cat.upsert_session(dest)
    names = {st["stream"] for st in cat.session(sid)["streams"]}
    assert {"glasses_l_SIMD01", "glasses_r_SIMD01"} <= names


def test_two_dual_boards_get_distinct_roles(lab):  # noqa: F811
    s, _ = lab
    a, b = s.device("sim_dual"), dict(s.device("sim_dual"), port="sim://SIMD02")
    rec = Recording(s, [a, b])
    rec.start({"subject_id": "S001", "protocol": "rest"})
    import time
    time.sleep(1.5)
    rec.stop()
    roles = sorted(w.role for w in rec.ppg_streams)
    assert roles == ["glasses_l", "glasses_l_b", "glasses_r", "glasses_r_b"]


def test_dual_samples_before_header_ask_for_info(lab):  # noqa: F811
    s, _ = lab
    from sglab.recorder import StreamWorker
    from sglab.protocol import encode

    class Src:
        port = "test://x"
        def __init__(self): self.sent = []
        def write(self, c): self.sent.append(c)

    src = Src()
    w = StreamWorker(src, "glasses_l", s.incoming_dir, 0)
    s.incoming_dir.mkdir(parents=True, exist_ok=True)
    w._handle(encode("M,4,0,1000,5,6"), 0.1)           # header lost
    assert src.sent == ["INFO"] and not w.ppg_children
    w._handle(encode("H,proto=1,dev=GLS04,fw=x,fs=100,ch=red,site=glasses_l"), 0.2)
    w._handle(encode("H,proto=2,dev=GLS04,fw=x,sid=4,stream=ppg_r,kind=ppg,fs=100,ch=red,site=glasses_r"), 0.2)
    w._handle(encode("M,4,0,1000,5"), 0.3)
    w._handle(encode("M,4,1,11000,7"), 0.3)
    w._handle(encode("M,4,2,21000,"), 0.3)              # an empty field is not a PPG sample
    c = w.ppg_children[4]
    assert c.role == "glasses_r" and c.stats["n_samples"] == 2 and c.stats["n_malformed"] == 1
    assert c.last_values == [7]
    for x in (w, c):
        if x._csv:
            x._csv.close()


@pytest.mark.parametrize("role", ["glasses_l", "glasses_r"])
def test_known_sites_in_cli(role):
    from sglab.cli import build_parser
    a = build_parser().parse_args(["tune", "run", "--site", role, "--data", "x"])
    assert a.site == role
