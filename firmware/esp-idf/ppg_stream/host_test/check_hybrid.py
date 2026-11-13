"""Check the hybrid link (sim_hybrid): one recording that went Wi-Fi -> USB gateway -> Wi-Fi.

The MQTT frames (frames_mqtt.bin, two boots) are reassembled as the server does (sglab.framing),
the USB envelopes (stdout) as the gateway does (nbe_gateway.wire). Together they must hold every
PPG sample and every camera frame, the LINK events in the right places, and nothing twice beyond
what the recorder drops by seq."""
import struct
import sys

from nbe_gateway.wire import EnvelopeDecoder, VideoFrame, split_records
from sglab.framing import FrameReceiver, record_items
from sglab.protocol import Event, Header, Sample, parse_line

# ---- MQTT part (server side)
rx, wifi = FrameReceiver(), []
raw = open("frames_mqtt.bin", "rb").read()
i = 0
boots = []
while i < len(raw):
    n, = struct.unpack_from("<I", raw, i)
    i += 4
    boot, = struct.unpack_from("<I", raw, i + 4)
    if not boots or boots[-1] != boot:
        boots.append(boot)
    items, _ = rx.feed_items(raw[i:i + n], now=i)
    wifi += items
    i += n
assert len(set(boots)) == 2, f"a new boot after the USB part: {boots}"
assert rx.stats["bad_frames"] == 0 and rx.stats["lost_bytes"] == 0, rx.stats

# ---- USB part (gateway side)
dec, usb = EnvelopeDecoder(), []
for kind, item in dec.feed(open(sys.argv[1], "rb").read()):
    assert kind == "records", f"text outside envelopes: {item!r}"
    for _off, k, body in split_records(item):
        usb += record_items(k, body)
assert dec.stats["bad_crc"] == 0, dec.stats


def split(items):
    msgs = [parse_line(x) for x in items if isinstance(x, str)]
    return msgs, [x for x in items if isinstance(x, VideoFrame)]


w_msgs, w_frames = split(wifi)
u_msgs, u_frames = split(usb)
links = lambda msgs: [m.text for m in msgs if isinstance(m, Event) and m.code == "LINK"]
assert links(u_msgs) == ["usb"], links(u_msgs)
assert links(w_msgs) == ["wifi"], links(w_msgs)
assert any(isinstance(m, Header) and m.extra.get("link") == "hybrid" for m in w_msgs), "H line says link=hybrid"

# ---- together: every sample and frame, in seq
samples = [m for m in w_msgs + u_msgs if isinstance(m, Sample)]
seqs = sorted({s.seq for s in samples})
assert seqs == list(range(seqs[0], seqs[-1] + 1)) and seqs[0] == 0, "PPG samples complete"
u_s = [m.seq for m in u_msgs if isinstance(m, Sample)]
assert u_s and min(u_s) > 0 and max(u_s) < seqs[-1], "the middle of the recording went over USB"
fseq = sorted({f.seq for f in w_frames + u_frames})
outage = "--outage" in sys.argv  # broker gone with a small ring: frames give way, samples don't
if outage:
    assert len(fseq) < fseq[-1] + 1, "the outage must have cost camera frames"
else:
    assert fseq == list(range(0, fseq[-1] + 1)), "camera frames complete"
assert w_frames and u_frames
for f in w_frames + u_frames:
    assert (f.w, f.h, len(f.data)) == (80, 80, 6400)
dups = len(samples) - len(seqs)
print(f"hybrid{' (outage)' if outage else ''}: {len(seqs)} samples ({len(u_s)} over USB, {dups} sent twice at the handover), "
      f"{len(fseq)} of {fseq[-1] + 1} frames ({len(u_frames)} over USB), boots {[hex(b) for b in boots]}, server {rx.stats}")
