"""Validate simulated firmware output with the PC-side parser (sglab.protocol)."""
import sys
import numpy as np
from sglab.protocol import Header, Sample, Event, Status, parse_line

def check(path, expect_fs, expect_ch, lossless=True):
    hdr, samples, events, status = None, [], [], []
    for line in open(path, newline=""):
        m = parse_line(line)            # raises on bad checksum / malformed
        if isinstance(m, Header): hdr = m
        elif isinstance(m, Sample): samples.append(m)
        elif isinstance(m, Event):
            if m.code == "SYNC":
                assert int(m.text) > 1.7e12, m  # unix ms
                continue
            events.append(m.code)
        elif isinstance(m, Status): status.append(m.values)
    assert hdr and hdr.fs == expect_fs and hdr.channels == expect_ch, hdr
    seq = np.array([s.seq for s in samples]); t = np.array([s.t_us for s in samples])
    assert seq[0] == 0 and np.all(np.diff(seq) == 1), "seq not continuous"
    dt = np.diff(t)
    period = 1e6 / expect_fs
    red = np.array([s.values[-1] for s in samples])          # red is last channel
    if lossless:
        assert np.allclose(dt, period, atol=1), (dt.min(), dt.max())
        assert np.all(np.diff(red) == 1), "sample order/content wrong"
        if len(expect_ch) == 2:
            ir = np.array([s.values[0] for s in samples])
            assert np.all(ir == (red * 3 + 1) & 0x3FFFF), "ir/red swapped or corrupt"
    else:
        gaps = np.diff(red) > 1
        assert gaps.any() and "FIFO_OVF" in events
    assert events[0] == "BOOT" and "START" in events and events[-1] == "STOP"
    fs_meas = [float(s["fs_meas"]) for s in status]
    print(f"{path}: OK  {len(samples)} samples, fs={hdr.fs}, ch={hdr.channels}, "
          f"dt={dt.min():.0f}-{dt.max():.0f} us, fs_meas={fs_meas}, events={sorted(set(events))}")

check("out_default.txt", 400, ["ir", "red"])
check("out_int.txt", 400, ["ir", "red"])
check("out_uart.txt", 400, ["ir", "red"])
check("out_hr.txt", 25, ["red"])
check("out_slow.txt", 400, ["ir", "red"], lossless=False)
check("out_mqtt.txt", 400, ["ir", "red"])
