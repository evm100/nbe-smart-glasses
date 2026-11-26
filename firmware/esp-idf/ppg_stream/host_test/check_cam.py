"""Check the camera controls (sim_cam_miss = sim_hybrid + TEST_CAM_MISS).

At 4.0 s the fake gateway sends "CAM WIN=280 MODE=CIF CLK=1 EXP=200" (WIN before MODE on purpose:
the window must be checked against the mode it will be used in). The fake OV2640 skips every 23rd
sensor frame (the driver "replaced" it before the camera task took it).

    python check_cam.py <usb envelopes> <sim stderr>

Checks: the window registers written (CIF mode 2, centred in 400x296, CLKRC = doubler | 1), the new
camera H line (mode=cif, win=280, clkdiv=1, fps_sensor=50.0, line_us=59.5, exp_max=336, exposure=200),
and that the S line's cam_miss counts the skipped frames that fell inside the stream.
"""
import re
import struct
import sys

from nbe_gateway.wire import REC_TEXT, EnvelopeDecoder, split_records, line_body
from sglab.framing import FrameReceiver

usb_path, log_path = sys.argv[1], sys.argv[2]
log = open(log_path).read().splitlines()

# ---- register writes
raw_calls = [ln for ln in log if ln.startswith("cam: set_res_raw")]
assert raw_calls, "no set_res_raw call after the CAM command"
assert raw_calls[-1] == "cam: set_res_raw mode=2 off=60,8 win=280x280 out=240x240", raw_calls[-1]
regs = [ln for ln in log if ln.startswith("cam: set_reg 0x111")]
assert regs and regs[-1] == "cam: set_reg 0x111 0xff 0x81", regs

# ---- text lines from both links
lines = []
dec = EnvelopeDecoder()
for kind, item in dec.feed(open(usb_path, "rb").read()):
    if kind != "records":
        continue
    for _off, k, body in split_records(item):
        if k == REC_TEXT:
            lines.append(body.decode())
rx, raw, i = FrameReceiver(), open("frames_mqtt.bin", "rb").read(), 0
while i < len(raw):
    n, = struct.unpack_from("<I", raw, i)
    i += 4
    out, _ = rx.feed(raw[i:i + n], now=i)
    lines += [ln.strip() for ln in out]
    i += n
bodies = [b for b in (line_body(ln) for ln in lines) if b]


def kv(body):
    return dict(p.split("=", 1) for p in body.split(",") if "=" in p)


cam_h = [kv(b) for b in bodies if b.startswith("H,") and "stream=cam," in b]
assert cam_h, "no camera H line"
# lines of the two links are not in time order here: look for the header the command produced
want = {"mode": "cif", "win": "280", "clkdiv": "1", "fps_sensor": "50.0", "line_us": "59.5",
        "exp_max": "336", "exposure": "200", "win_min": "240", "win_max": "296"}
after = [h for h in cam_h if h.get("win") == "280"]
assert after, f"no camera H line with the new window: {cam_h}"
last = after[-1]
for k, v in want.items():
    assert last.get(k) == v, f"camera H line after the command: {k}={last.get(k)} (want {v}): {last}"
# before it: the stock 240x240 framing = CIF mode at the driver's divider 3 (~25 fps, as measured on GLS03)
stock = [h for h in cam_h if "win" not in h]
assert stock and stock[0]["mode"] == "cif" and stock[0]["fps_sensor"] == "25.0", stock[:1]

# ---- missed sensor frames
skips = [int(m.group(1)) for m in (re.match(r"cam: skipped frame at (\d+) us", ln) for ln in log) if m]
starts = [int(b.split(",")[1]) for b in bodies if re.match(r"E,\d+,START", b)]
stats = [(int(b.split(",")[1]), kv(b)) for b in bodies if b.startswith("S,")]
assert starts and stats and skips, (len(starts), len(stats), len(skips))
t_s, last_s = max(stats, key=lambda x: x[0])
# a skip at tick t delays the next frame: counted when that one is grabbed (t + 66.7 ms), if streaming
expect = sum(1 for t in skips if starts[0] < t < t_s - 70000)
got = int(last_s["cam_miss"])
assert abs(got - expect) <= 1, f"cam_miss={got}, expected ~{expect} (skips {skips}, start {starts[0]}, S at {t_s})"
assert "cam_sfps" in last_s, last_s
print(f"camera: CIF window 280 -> 240x240 at clkdiv 1 (H: fps_sensor={last['fps_sensor']}, line_us={last['line_us']}), "
      f"cam_miss={got} of {expect} skipped sensor frames, cam_sfps={last_s['cam_sfps']}")
