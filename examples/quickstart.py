"""Layer 1 from Python (instead of the CLI).

    python examples/quickstart.py                 # uses synthetic data
    python examples/quickstart.py path/to/rec.csv # your own recording
"""
import sys
from pathlib import Path

from ppg import Layer1Config, process_file
from ppg.synthetic import make_recording

if len(sys.argv) > 1:
    csv = Path(sys.argv[1])
else:
    csv = Path("synthetic_demo.csv")
    df, truth = make_recording(duration_s=120, hr_bpm=72)
    df.to_csv(csv, index=False)
    print(f"Generated {csv} (true HR {truth['hr_bpm']} bpm)")

cfg = Layer1Config()
# Examples of overriding defaults:
# cfg.io.channels = {"ir": "IR", "red": "RED"}
# cfg.io.time_col = "timestamp_ms"; cfg.io.time_unit = "ms"
# cfg.filters.morph_band_hz = (0.5, 10.0)
# cfg.artifacts.min_dc = 20000   # no-contact threshold once you know your device

res = process_file(csv, cfg, out_dir="layer1_out")

print(res.meta["timing"])
print(res.meta["summary"])
good = res.quality[res.quality.good]
print(f"{len(good)}/{len(res.quality)} good windows")

# Arrays you will hand to Layer 2 (beat detection / features):
sig = res.signals
t, ppg, vpg, apg = sig["t"], sig["ir_ac_norm"], sig["ir_vpg"], sig["ir_apg"]

# ---- Layer 2: beats, fiducials, features ----------------------------------
from ppg.layer2 import run_layer2  # noqa: E402

l2 = run_layer2(res)
print(l2.meta["summary"])
beats = l2.beats[l2.beats.beat_good]          # one row per accepted beat
epochs = l2.epochs[l2.epochs.valid]           # one row per 30 s window -> ML features
print(epochs[["t_center", "hr_mean_bpm", "dia_rel_med", "pw50_s_med", "rmssd_ms"]].head())
