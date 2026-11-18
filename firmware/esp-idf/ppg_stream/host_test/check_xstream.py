"""Check the auxiliary streams (protocol v2.1, main/xstream.c) in the output of the TEST_AUX builds.

  check_xstream.py text   out_aux.txt          USB text link: demo stream "sweat"
  check_xstream.py usbbin out_aux_usbbin.bin   USB binary link + camera: "sweat" + "cam_feat"
  check_xstream.py hybrid out_aux_hybrid.bin   hybrid link (+ frames_mqtt.bin): both, Wi-Fi -> USB -> Wi-Fi

H and M lines are parsed here, not with sglab.protocol (the checker pins the wire format on its
own): checksum, the H line's fields, seq from 0 after START, t_us increasing, the demo's values
recomputed from t_us, and the camera's frame statistics recomputed from the pixels of the frames
that arrived, the way the server will."""
import re
import struct
import sys

import numpy as np

mode, path = sys.argv[1], sys.argv[2]


# ---------------------------------------------------------------- the wire format
def body_of(line: str) -> str:
    """'$<body>*CS' -> body; the checksum is the XOR of the body's bytes."""
    line = line.rstrip("\r\n")
    assert line.startswith("$") and line[-3] == "*", f"not a protocol line: {line!r}"
    body = line[1:-3]
    cs = 0
    for b in body.encode():
        cs ^= b
    assert f"{cs:02X}" == line[-2:], f"checksum: {line!r}"
    return body


NUM = re.compile(r"-?\d+(\.\d+)?")


def fixed(text: str) -> tuple[int, int] | None:
    """'12.34' -> (1234, 2): the integer the board formatted and its decimals. '' -> None (no value)."""
    if text == "":
        return None
    assert NUM.fullmatch(text), f"not a decimal number: {text!r}"
    whole, _, frac = text.partition(".")
    return int(whole + frac), len(frac)


def read_lines() -> tuple[list[str], list]:
    """All text lines the board sent, in order, and the camera frames (binary links)."""
    if mode == "text":
        return [x for x in open(path, newline="").read().split("\r\n") if x], []
    from nbe_gateway.wire import EnvelopeDecoder, VideoFrame, split_records
    from sglab.framing import FrameReceiver, record_items
    items = []
    if mode == "hybrid":  # the Wi-Fi part first (the recording starts on Wi-Fi), as the server reassembles it
        rx, raw, i = FrameReceiver(), open("frames_mqtt.bin", "rb").read(), 0
        while i < len(raw):
            n, = struct.unpack_from("<I", raw, i)
            i += 4
            got, _ = rx.feed_items(raw[i:i + n], now=i)
            items += got
            i += n
        assert rx.stats["bad_frames"] == 0 and rx.stats["lost_bytes"] == 0, rx.stats
    dec = EnvelopeDecoder()
    for kind, item in dec.feed(open(path, "rb").read()):
        assert kind == "records", f"text outside envelopes: {item!r}"
        for _off, k, rec in split_records(item):
            items += record_items(k, rec)
    return [x for x in items if isinstance(x, str)], [x for x in items if isinstance(x, VideoFrame)]


lines, frames = read_lines()
lossless = mode != "usbbin"  # sim_usbbin's host stops reading for a moment: lines and frames are lost there

# ---------------------------------------------------------------- H lines
heads: dict[int, dict] = {}   # sid -> the stream's last H line
m: dict[int, list] = {}       # sid -> [(seq, t_us, [fields])]
status = []
started = stopped = False
n_d = 0
for line in lines:
    body = body_of(line)
    typ, _, rest = body.partition(",")
    if typ == "H":
        kv = dict(x.split("=", 1) for x in rest.split(","))
        if "sid" in kv and kv.get("kind") in ("samples", "features"):
            heads[int(kv["sid"])] = kv
    elif typ == "E":
        code = rest.split(",")[1]
        started |= code == "START"
        stopped |= code == "STOP"
    elif typ == "S":
        status.append(dict(x.split("=", 1) for x in rest.split(",")[1:]))
    elif typ == "D":
        n_d += 1
    elif typ == "M":
        f = rest.split(",")
        sid, seq, t_us = int(f[0]), int(f[1]), int(f[2])
        assert sid in heads, f"M line of a stream that has not declared itself: {line!r}"
        if lossless:  # (hybrid: the two links' parts are read one after the other, not in time order)
            assert started and (not stopped or mode == "hybrid"), f"M line outside START..STOP: {line!r}"
        assert len(f) - 3 == len(heads[sid]["ch"].split("|")), f"one field per channel: {line!r}"
        m.setdefault(sid, []).append((seq, t_us, f[3:]))

for sid, h in heads.items():
    assert 2 <= sid <= 255 and h["proto"] == "2" and re.fullmatch(r"[a-z0-9_]+", h["stream"]), h
    assert h["dev"] and h["fw"] and float(h["fs"]) > 0 and h["sensor_ok"] == "1", h
    assert len(h["ch"].split("|")) == len(h["unit"].split("|")), h
    if h["kind"] == "features":
        assert h["src"] and re.fullmatch(r"[a-z0-9_]+@\d+", h["algo"]), h
    else:
        assert "src" not in h and "algo" not in h, h


def series(sid: int) -> list:
    """The stream's M lines once each, by seq: complete from 0 on the lossless links."""
    rows = sorted({r[0]: r for r in m.get(sid, [])}.values())
    assert rows, f"no M lines of sid {sid}"
    seqs = [r[0] for r in rows]
    if lossless:
        assert seqs == list(range(len(seqs))), f"sid {sid}: seq not 0, 1, 2, ..: {seqs[:20]}"
    if mode != "hybrid":  # one link: also in order and once on the wire
        assert [r[0] for r in m[sid]] == seqs, f"sid {sid}: M lines out of order or repeated"
    ts = [r[1] for r in rows]
    assert all(b > a for a, b in zip(ts, ts[1:])), f"sid {sid}: t_us not increasing"
    return rows


# ---------------------------------------------------------------- demo stream "sweat"
h = heads[2]
want = {"stream": "sweat", "kind": "samples", "sensor": "DEMO", "fs": "1", "ch": "glucose|lactate", "unit": "uM|mM"}
assert {k: h[k] for k in want} == want and "scale" not in h, h


def tri(sec: int, period: int) -> int:
    x = sec % period
    return x if x <= period // 2 else period - x


sweat = series(2)
empty = 0
for (seq, t_us, f), nxt in zip(sweat, sweat[1:] + [None]):
    sec = t_us // 1_000_000
    assert fixed(f[0]) == (800 + 5 * tri(sec, 120), 1), (seq, t_us, f)        # glucose, uM
    assert 80.0 <= float(f[0]) <= 110.0
    if sec % 10 == 9:
        assert f[1] == "", f"lactate has no value in this second: {(seq, t_us, f)}"
        empty += 1
    else:
        assert fixed(f[1]) == (100 + 5 * tri(sec, 60), 2), (seq, t_us, f)    # lactate, mM
        assert 1.0 <= float(f[1]) <= 2.5
    if nxt and nxt[0] == seq + 1:
        assert abs(nxt[1] - t_us - 1_000_000) <= 10_000, f"1 Hz: {t_us} -> {nxt[1]}"
assert empty >= 1, "the run must include a sample without a lactate value"
assert len(sweat) >= 8, len(sweat)
st = [s for s in status if "sweat_n" in s]
assert st and all(s["sweat_err"] == "0" for s in st), "S line: sweat_n / sweat_err"
ns = [int(s["sweat_n"]) for s in st]
assert max(ns) >= len(sweat) - 2 and (not lossless or mode == "hybrid" or ns == sorted(ns)), ns
out = f"xstream {mode}: sweat {len(sweat)} M lines ({empty} without lactate), seq 0..{sweat[-1][0]}"

# ---------------------------------------------------------------- camera frame statistics "cam_feat"
if mode == "text":
    assert 3 not in heads and n_d > 1000, "text build: no camera, PPG D lines as before"
else:
    h = heads[3]
    want = {"stream": "cam_feat", "kind": "features", "src": "cam", "algo": "framestats@1", "sensor": "OV2640",
            "fs": "15", "ch": "brightness|contrast|motion", "unit": "-|-|-", "step": "2"}
    assert {k: h[k] for k in want} == want, h
    feat = {r[0]: r for r in series(3)}
    by_seq = {f.seq: f for f in frames}
    assert feat[0][2][2] == "", "the first frame after START has no motion value"

    def grid(f) -> np.ndarray:
        return np.frombuffer(f.data, np.uint8).reshape(f.h, f.w)[::2, ::2].astype(np.int64)

    checked = with_motion = 0
    for seq, f in by_seq.items():
        if seq not in feat:
            assert not lossless, f"frame {seq} has no feature line"
            continue
        _, t_us, v = feat[seq]
        assert t_us == f.t_us, f"feature line {seq} carries its frame's time"
        g = grid(f)
        n = g.size
        cum = np.cumsum(np.bincount(g.ravel(), minlength=256))
        lo = int(np.argmax(cum >= max(1, n // 50)))
        hi = int(np.argmax(cum >= n - n // 50))
        assert fixed(v[0]) == (int(g.sum()) * 100 // n, 2), (seq, v)
        assert fixed(v[1]) == (hi - lo, 0), (seq, v)
        assert 0 <= float(v[0]) <= 255 and 0 <= int(v[1]) <= 255
        checked += 1
        if seq and seq - 1 in by_seq:  # the frame before it arrived too: the motion value can be recomputed
            assert fixed(v[2]) == (int(np.abs(g - grid(by_seq[seq - 1])).sum()) * 1000 // n, 3), (seq, v)
            assert float(v[2]) > 0
            with_motion += 1
        else:
            assert seq == 0 or v[2] != "", (seq, v)
    assert checked >= 30 and with_motion >= 25, (checked, with_motion)
    # a feature line per frame that got a seq, also for frames the link could not send
    only_feat = sorted(set(feat) - set(by_seq))
    if mode == "usbbin":
        assert only_feat or len(feat) < max(feat) + 1, "the USB stall must show (a frame or a line lost)"
    assert [s for s in status if "cam_feat_n" in s], "S line: cam_feat_n"
    out += (f"; cam_feat {len(feat)} M lines for {len(by_seq)} frames, {checked} recomputed from the pixels "
            f"({with_motion} with motion), {len(only_feat)} lines whose frame was lost")
print(out)
