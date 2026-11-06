"""Check the USB binary link output of sim_usbbin with the PC-side decoders (nbe_gateway.wire +
sglab.framing): envelopes and CRCs, both stream headers, every PPG sample, every camera frame's
pixels, and that the simulated USB stall shows up only as counted, detectable loss."""
import re
import sys

from nbe_gateway.wire import EnvelopeDecoder, VideoFrame, split_records
from sglab.framing import record_items
from sglab.protocol import Header, Sample, parse_line

raw = open(sys.argv[1], "rb").read()
dec = EnvelopeDecoder()
items = []
for kind, item in dec.feed(raw):
    assert kind == "records", f"text outside envelopes: {item!r}"
    for _off, k, body in split_records(item):
        items += record_items(k, body)

lines = [x for x in items if isinstance(x, str)]
frames = [x for x in items if isinstance(x, VideoFrame)]
msgs = [parse_line(x) for x in lines]
heads = [m for m in msgs if isinstance(m, Header)]
cam_h = [h for h in heads if h.extra.get("kind") == "video"]
ppg_h = [h for h in heads if h.extra.get("kind") != "video"]
assert ppg_h and cam_h, "both stream headers"
c = cam_h[-1]
assert c.proto == 2 and c.extra["sid"] == "1" and c.extra["stream"] == "cam", c
assert (c.extra["w"], c.extra["h"], c.extra["fmt"], c.fs) == ("80", "80", "gray8", 15.0), c

# frames: the 80x80 centre of the fake 240x240 sensor, whose pixel (x, y) = native frame number
# + x + 3y; seq continuous except the stall
assert frames, "no frames"
C, X0 = 80, 80  # crop side, offset of the crop in the 240x240 frame
for f in frames:
    assert (f.sid, f.w, f.h, f.fmt, f.exposure, f.gain) == (1, C, C, 0, 300, 2), f[:8]
    k = (f.data[0] - X0 - 3 * X0) & 0xFF
    assert f.data == bytes(((k + X0 + x + 3 * (X0 + y)) & 0xFF) for y in range(C) for x in range(C)), "centre crop"
seqs = [f.seq for f in frames]
assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "frames in order, once"
missing = (seqs[-1] - seqs[0] + 1) - len(seqs)
ts = [f.t_us for f in frames]
gaps = [b - a for a, b in zip(ts, ts[1:])]
assert min(gaps) >= 60000 and max(gaps) <= 70000 * (missing + 1), (min(gaps), max(gaps))  # 15 fps cap

# samples: one continuous run apart from the stall
samples = [m for m in msgs if isinstance(m, Sample)]
sq = [s.seq for s in samples]
assert sq == sorted(sq)
lost_samples = (sq[-1] - sq[0] + 1) - len(sq)

log = open("usbbin_log.txt").read()
not_sent = int(re.search(r"(\d+) frames not sent", log).group(1))
assert missing == not_sent, f"frames missing on the PC ({missing}) != frames the board could not send ({not_sent})"
# (a frame cut mid-envelope needs a resync, one refused at its first write is lost whole; the
# torn-envelope resync itself is tested in tests/test_camera_gateway.py)
assert missing >= 1, "the USB stall must cost a frame"
assert dec.stats["bad_crc"] == 0 or dec.stats["skipped_bytes"] > 0
print(f"usb bin: {len(frames)} frames ({missing} lost in the stall, all counted by the board), "
      f"{len(samples)} samples ({lost_samples} lost), envelopes {dec.stats}")
