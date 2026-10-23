"""Command line entry points.

  ppg-layer1 run  <file-or-folder> -o out/ [--config cfg.json] [overrides]
  ppg-layer1 synth demo.csv [--duration 120 --hr 72]
  ppg-layer1 config > layer1_config.json

  ppg-layer2 run  <file-or-folder> -o out/ [--config l1.json] [--config2 l2.json]
  ppg-layer2 config > layer2_config.json
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .config import Layer1Config, Layer2Config


def _parse_channels(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--channel expects name=COLUMN, got '{it}'")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cmd_run(a):
    from .pipeline import process_file

    layer2 = getattr(a, "layer2", False)
    cfg2 = None
    if layer2:
        from .layer2 import process_file_layer2
        cfg2 = Layer2Config.from_json(a.config2) if a.config2 else Layer2Config()
    cfg = Layer1Config.from_json(a.config) if a.config else Layer1Config()
    if a.time_col: cfg.io.time_col = a.time_col
    if a.time_unit: cfg.io.time_unit = a.time_unit
    if a.channel: cfg.io.channels = _parse_channels(a.channel)
    if a.fs: cfg.timing.fs_target = a.fs
    if a.fs_nominal: cfg.io.fs_nominal = a.fs_nominal
    if a.primary: cfg.quality.primary_channel = a.primary

    src = Path(a.input)
    files = sorted(src.glob(a.pattern)) if src.is_dir() else [src]
    if not files:
        raise SystemExit(f"No files matching {a.pattern} in {src}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg.to_json(out / "layer1_config_used.json")
    if cfg2 is not None:
        cfg2.to_json(out / "layer2_config_used.json")

    failures = 0
    for f in files:
        try:
            if cfg2 is not None:
                res, l2 = process_file_layer2(f, cfg, cfg2, out, plot=not a.no_plot)
                s2 = l2.meta["summary"]
                extra = (f" | beats {s2.get('n_good_beats', 0)}/{s2['n_beats']} good, "
                         f"epochs {s2.get('n_valid_epochs', 0)}/{s2.get('n_epochs', 0)} valid")
            else:
                res = process_file(f, cfg, out, plot=not a.no_plot)
                extra = ""
            s = res.meta["summary"]
            hr = s.get("median_hr_bpm")
            print((f"[ok]  {f.name}: fs={res.fs:g} Hz, {s['n_windows']} windows, "
                   f"good={s.get('good_window_frac', 0):.0%}, HR={hr:.1f} bpm" if hr else
                   f"[ok]  {f.name}: fs={res.fs:g} Hz, no good windows") + extra)
        except Exception as e:  # keep batch going
            failures += 1
            print(f"[err] {f.name}: {e}", file=sys.stderr)
            if a.verbose:
                traceback.print_exc()
    print(f"Done: {len(files) - failures}/{len(files)} processed -> {out}")
    return 1 if failures else 0


def cmd_synth(a):
    from .synthetic import make_recording

    df, truth = make_recording(duration_s=a.duration, fs=a.fs, hr_bpm=a.hr, seed=a.seed,
                               dia_amp=tuple(a.dia_amp))
    df.to_csv(a.output, index=False)
    print(f"Wrote {a.output}: {len(df)} rows, true HR {truth['hr_bpm']} bpm, "
          f"motion {truth['motion']}, dropouts {truth['dropouts']}")
    return 0


def cmd_config(a):
    print(Layer1Config().to_json())
    return 0


def _add_run(sub, layer2=False):
    r = sub.add_parser("run", help="process a CSV or a folder of CSVs")
    r.add_argument("input")
    r.add_argument("-o", "--out", default="layer1_out")
    r.add_argument("--config")
    r.add_argument("--pattern", default="*.csv")
    r.add_argument("--time-col")
    r.add_argument("--time-unit", choices=["auto", "s", "ms", "us", "ns", "datetime"])
    r.add_argument("--channel", action="append", help="name=COLUMN, repeatable (e.g. ir=IR)")
    r.add_argument("--fs", type=float, help="target sample rate (Hz)")
    r.add_argument("--fs-nominal", type=float, help="sample rate if file has no timestamps")
    r.add_argument("--primary", help="channel used for good/bad decision")
    r.add_argument("--no-plot", action="store_true")
    r.add_argument("-v", "--verbose", action="store_true")
    if layer2:
        r.add_argument("--config2", help="Layer 2 config JSON")
    r.set_defaults(func=cmd_run, layer2=layer2)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ppg-layer1", description="PPG Layer 1: signal conditioning + quality")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add_run(sub)

    s = sub.add_parser("synth", help="write a synthetic MAX3010x-like CSV")
    s.add_argument("output")
    s.add_argument("--duration", type=float, default=120)
    s.add_argument("--fs", type=float, default=100)
    s.add_argument("--hr", type=float, default=72)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--dia-amp", type=float, nargs=2, default=[0.45, 0.45], metavar=("START", "END"),
                   help="diastolic wave height ramp (simulated morphology change)")
    s.set_defaults(func=cmd_synth)

    c = sub.add_parser("config", help="print default config JSON")
    c.set_defaults(func=cmd_config)

    a = p.parse_args(argv)
    return a.func(a)


def main2(argv=None):
    p = argparse.ArgumentParser(prog="ppg-layer2",
                                description="PPG Layer 1 + 2: conditioning, beats, fiducials, features")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add_run(sub, layer2=True)
    c = sub.add_parser("config", help="print default Layer 2 config JSON")
    c.set_defaults(func=lambda a: print(Layer2Config().to_json()) or 0)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
