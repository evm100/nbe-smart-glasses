"""Check dual-PPG glasses (sim_dual*, protocol v2.2, docs/DUAL_PPG.md): two MAX30102s on two I2C buses.

    check_dual.py text   out.txt                USB text link: D lines (left) + M lines (right)
    check_dual.py mqtt   frames_mqtt.bin        MQTT frames: 0x02 (left) + 0x04 (right) records
    check_dual.py hybrid out_usb.bin            MQTT frames + USB envelopes (Wi-Fi -> gateway -> Wi-Fi)
    ... --no-right                              the right sensor is unplugged: the left must be unaffected

Both streams must be complete (seq 0..n without gaps), each with its own seq and strictly increasing
t_us on the one device clock, the right side's header kind=ppg sid=4 site=glasses_r, and its rate
1 % above the left's (the simulator's second sensor runs fast: its own oscillator)."""
import struct
import sys

from nbe_gateway.wire import EnvelopeDecoder, split_records
from sglab.framing import FrameReceiver, record_lines
from sglab.protocol import Header, Measure, Sample, Status, parse_line

mode, path = sys.argv[1], sys.argv[2]
no_right = "--no-right" in sys.argv


def mqtt_lines(fname):
    rx, out, raw, i = FrameReceiver(), [], open(fname, "rb").read(), 0
    while i < len(raw):
        n, = struct.unpack_from("<I", raw, i)
        i += 4
        lines, _ = rx.feed(raw[i:i + n], now=i)
        out += lines
        i += n
    assert rx.stats["bad_frames"] == 0 and rx.stats["lost_bytes"] == 0, rx.stats
    return out


def usb_lines(fname):
    dec, out = EnvelopeDecoder(), []
    for kind, item in dec.feed(open(fname, "rb").read()):
        assert kind == "records", f"text outside envelopes: {item!r}"
        for _off, k, body in split_records(item):
            out += record_lines(k, body)
    assert dec.stats["bad_crc"] == 0, dec.stats
    return out


if mode == "text":
    lines = open(path, newline="").read().splitlines(keepends=True)
elif mode == "mqtt":
    lines = mqtt_lines(path)
elif mode == "hybrid":
    lines = mqtt_lines("frames_mqtt.bin") + usb_lines(path)
else:
    sys.exit(f"unknown mode {mode}")
msgs = [m for m in (parse_line(x) for x in lines) if m is not None]

heads = [m for m in msgs if isinstance(m, Header)]
left_h = [h for h in heads if "sid" not in h.extra]
right_h = [h for h in heads if h.extra.get("kind") == "ppg"]
assert left_h and left_h[0].extra.get("site") == "glasses_l", left_h[:1]
assert right_h, "the right sensor declares itself on every START / INFO, present or not"
r = right_h[-1].extra
assert (r.get("sid"), r.get("stream"), r.get("site")) == ("4", "ppg_r", "glasses_r"), r
assert right_h[-1].channels == left_h[-1].channels and right_h[-1].fs == left_h[-1].fs, "same settings both sides"
assert r.get("sensor_ok") == ("0" if no_right else "1")


def check(name, seqs, ts):
    u = sorted(set(seqs))  # hybrid: a few samples are sent twice at the handover; the recorder drops them by seq
    assert u and u[0] == 0 and u == list(range(len(u))), f"{name}: seq complete from 0"
    by_seq = dict(zip(seqs, ts))
    t = [by_seq[s] for s in u]
    steps = [b - a for a, b in zip(t, t[1:])]
    assert min(steps) >= 0, f"{name}: t_us never goes back"
    # Samples are stamped with the nominal period (PROTOCOL.md timing rule 1), so a chip running 1 % fast
    # gets a repeated t_us about once per 100 samples (a slow one: an occasional double step). Existing
    # behaviour of every board, not of the dual build - noted in docs/DUAL_PPG.md "Open points".
    assert steps.count(0) <= 0.02 * len(steps), f"{name}: {steps.count(0)} repeated t_us"
    return len(u), (t[-1] - t[0]) / 1e6


left = [m for m in msgs if isinstance(m, Sample)]
right = [m for m in msgs if isinstance(m, Measure) and m.sid == 4]
nl, dl = check("left", [m.seq for m in left], [m.t_us for m in left])
assert all(len(m.values) == len(left_h[0].channels) for m in left)
if no_right:
    assert not right, "no samples from a missing sensor"
    print(f"dual {mode} (right unplugged): left {nl} samples, right none")
    sys.exit(0)
nr, dr = check("right", [m.seq for m in right], [m.t_us for m in right])
assert all(len(m.values) == len(right_h[0].channels) and all(v == int(v) for v in m.values) for m in right)
rate_l, rate_r = (nl - 1) / dl, (nr - 1) / dr
assert 1.005 < rate_r / rate_l < 1.015, f"right/left rate {rate_r / rate_l:.4f} (the simulator's right sensor is 1 % fast)"
# the two sensors' simulated values differ (right: 5i + 1000), so nothing was mixed up
assert left[1].values != [int(v) for v in right[1].values]
st = [m for m in msgs if isinstance(m, Status)]
assert st and "ppg_r_fs_meas" in st[-1].values and st[-1].values.get("ppg_r_ok") == "1", st[-1:]
print(f"dual {mode}: left {nl} samples at {rate_l:.1f} Hz, right {nr} at {rate_r:.1f} Hz (ratio {rate_r / rate_l:.4f})")
