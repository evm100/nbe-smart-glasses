"""MQTT data frames (sglab.framing): decoding, de-duplication, gaps -> RESEND, reboots.

The encoder below mirrors firmware/esp-idf/ppg_stream/main/frame.c; host_test/run.sh checks the
real C encoder against sglab.framing end to end."""
import random
import struct
import zlib

from sglab.framing import GIVE_UP_RESENDS, HDR, FrameReceiver, decode_frame
from sglab.protocol import Sample, encode, parse_line


def uv(x):
    out = bytearray()
    while x >= 0x80:
        out.append(x & 0x7F | 0x80)
        x >>= 7
    out.append(x)
    return bytes(out)


def zz(v):
    return (v << 1) ^ (v >> 63)


def samples_record(seq, ts, vals, dt_nom):
    nch = len(vals[0])
    body = bytearray(struct.pack("<H", len(ts)) + uv(seq) + uv(ts[0]) + uv(dt_nom) + bytes([nch]))
    for v in vals[0]:
        body += uv(v)
    for k in range(1, len(ts)):
        body += uv(zz(ts[k] - ts[k - 1] - dt_nom))
        for c in range(nch):
            body += uv(zz(vals[k][c] - vals[k - 1][c]))
    return bytes([2]) + uv(len(body)) + bytes(body)


def text_record(line):
    b = line.rstrip("\r\n").encode()
    return bytes([1]) + uv(len(b)) + b


def frame(boot, off, recs):
    data = b"".join(recs)
    return HDR.pack(b"NB", 2, 0, boot, off, zlib.crc32(data)) + data


def make_stream(n=2000, nch=2, seed=1):
    """Records (as a byte stream) for a header + n samples in chunks of 3-40, like FIFO reads."""
    rng = random.Random(seed)
    recs = [text_record(encode("H,proto=1,dev=N1,fw=x,fs=400,ch=ir|red"))]
    seq, t = 0, 5_000_000_000  # big device time: multi-byte varints
    v = [rng.randrange(1, 262143) for _ in range(nch)]
    want = []
    while seq < n:
        k = min(rng.randrange(3, 40), n - seq)
        ts, vals = [], []
        for _ in range(k):
            t += 2500 + rng.randrange(-300, 300)
            v = [max(0, min(262143, x + rng.randrange(-5000, 5000))) for x in v]
            ts.append(t)
            vals.append(list(v))
            want.append((seq + len(ts) - 1, t, tuple(v)))
        recs.append(samples_record(seq, ts, vals, 2500))
        seq += k
        if rng.random() < 0.1:
            recs.append(text_record(encode(f"S,{t},fs_meas=400.0")))
    return recs, want


def frames_of(recs, boot=0xABCD0001, per=5):
    out, off = [], 0
    for i in range(0, len(recs), per):
        out.append(frame(boot, off, recs[i:i + per]))
        off += sum(len(r) for r in recs[i:i + per])
    return out


def samples(lines):
    return [(m.seq, m.t_us, tuple(m.values)) for m in map(parse_line, lines) if isinstance(m, Sample)]


def test_roundtrip_two_and_one_channel():
    for nch in (2, 1):
        recs, want = make_stream(nch=nch, seed=nch)
        rx, lines = FrameReceiver(), []
        for f in frames_of(recs):
            got, reply = rx.feed(f, now=0)
            lines += got
            assert reply.startswith("ACK abcd0001 ")
        assert samples(lines) == want
        assert int(reply.split()[-1]) == sum(len(r) for r in recs)  # ACK = whole stream


def test_duplicates_after_resend_are_dropped():
    recs, want = make_stream()
    fs = frames_of(recs)
    rx, lines = FrameReceiver(), []
    for f in fs[:6] + fs[3:]:  # the node went back 3 frames (reconnect)
        lines += rx.feed(f, now=0)[0]
    assert samples(lines) == want and rx.stats["dup_bytes"] > 0


def test_gap_asks_for_resend_then_fills():
    recs, want = make_stream()
    fs = frames_of(recs)
    rx, lines = FrameReceiver(), []
    for f in fs[:4]:
        lines += rx.feed(f, now=0)[0]
    got, reply = rx.feed(fs[5], now=1)  # fs[4] lost
    assert got == [] and reply.startswith("RESEND abcd0001 ") and int(reply.split()[-1]) == decode_frame(fs[4]).offset
    assert rx.feed(fs[6], now=1.2)[1] is None  # rate-limited: frames in flight all show the gap
    for f in fs[4:]:
        lines += rx.feed(f, now=2)[0]
    assert samples(lines) == want and rx.stats["lost_bytes"] == 0


def test_gap_accepted_when_node_no_longer_has_the_data():
    recs, _ = make_stream()
    fs = frames_of(recs)
    rx = FrameReceiver()
    rx.feed(fs[0], now=0)
    replies = [rx.feed(fs[3], now=t)[1] for t in range(1, GIVE_UP_RESENDS + 2)]
    assert sum(r.startswith("RESEND") for r in replies) == GIVE_UP_RESENDS
    assert replies[-1].startswith("ACK") and rx.stats["lost_bytes"] > 0


def test_corrupt_frame_and_reboot():
    recs, want = make_stream(n=300)
    fs = frames_of(recs)
    rx = FrameReceiver()
    lines = rx.feed(fs[0], now=0)[0]
    bad = bytearray(fs[1])
    bad[-1] ^= 0xFF
    got, reply = rx.feed(bytes(bad), now=1)
    assert got == [] and reply.startswith("RESEND") and rx.stats["bad_frames"] == 1
    for f in fs[1:]:
        lines += rx.feed(f, now=2)[0]
    assert samples(lines) == want
    # node rebooted: new boot id, offsets from 0 again -> a new stream, not duplicates
    lines2 = []
    for f in frames_of(recs, boot=0x77):
        lines2 += rx.feed(f, now=3)[0]
    assert samples(lines2) == want and rx.stats["boots"] == 2


def test_hub_acks_and_routes_frames():
    from sglab.mqtt_hub import MqttHub

    class Client:
        def __init__(self):
            self.pub = []

        def publish(self, topic, payload, qos=0, retain=False):
            self.pub.append((topic, payload))

    hub = MqttHub({"host": "x"}, client=Client())
    got = []
    hub.attach("N1", got.append)
    recs, want = make_stream(n=200)
    for f in frames_of(recs):
        hub.handle("nbe/dev/N1/data", f)
    lines = b"".join(got).decode().splitlines(keepends=True)
    assert samples(lines) == want
    assert hub.client.pub[-1][0] == "nbe/dev/N1/cmd" and hub.client.pub[-1][1].startswith("ACK ")


def test_python_sender_matches_receiver_through_loss():
    """sglab.framing.FrameSender (sim-node) with a lossy link: everything arrives exactly once."""
    from sglab.framing import FrameSender

    recs, want = make_stream(n=3000, seed=9)
    rx, tx = FrameReceiver(), FrameSender(boot=7, frame_max=1500, window=6000)
    lines_in = [l for r in recs for l in _lines_of(r)]
    got, now, k = [], 0.0, 0
    for i in range(0, len(lines_in), 50):
        tx.add_lines(lines_in[i:i + 50])
        now += 0.25
        if i == 1500:
            tx.go_back(now)  # reconnect
        for f in tx.frames(now):
            k += 1
            if k % 9 == 4:  # lost frame
                continue
            lines, reply = rx.feed(f, now)
            got += lines
            if reply and k % 5 != 2:  # some ACKs lost too
                tx.handle_cmd(reply)
    for _ in range(100):  # drain
        now += 1
        for f in tx.frames(now):
            lines, reply = rx.feed(f, now)
            got += lines
            if reply:
                tx.handle_cmd(reply)
    assert samples(got) == want and rx.stats["lost_bytes"] == 0 and tx.acked == tx.head


def _lines_of(rec):
    """Expand a test record back into protocol lines (input for the sender)."""
    from sglab.framing import record_lines

    i, n = 1, 0
    shift = 0
    while True:
        c = rec[i]
        n |= (c & 0x7F) << shift
        i += 1
        if c < 0x80:
            break
        shift += 7
    return record_lines(rec[0], rec[i:i + n])
