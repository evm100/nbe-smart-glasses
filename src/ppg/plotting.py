"""QA figure: one PNG per recording so you can eyeball every run."""
from __future__ import annotations

from pathlib import Path

import warnings

import matplotlib

matplotlib.use("Agg")
warnings.filterwarnings("ignore", message="No artists with labels")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402


def _shade_bad(ax, q, color="#d62728", alpha=0.12):
    # merge overlapping bad windows into spans
    if q.empty or "good" not in q:
        return
    bad = q.loc[~q["good"].astype(bool), ["t_start", "t_end"]].to_numpy()
    if bad.size == 0:
        return
    spans = [list(bad[0])]
    for a, b in bad[1:]:
        if a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    for a, b in spans:
        ax.axvspan(a, b, color=color, alpha=alpha, lw=0)


def _best_zoom(q, length=10.0):
    if q.empty or "good" not in q or not q["good"].any():
        return None
    # centre of the longest run of consecutive good windows
    from .filters import contiguous_runs
    runs = contiguous_runs(q["good"].to_numpy(dtype=bool))
    s, e = max(runs, key=lambda r: r[1] - r[0])
    c = 0.5 * (float(q["t_start"].iloc[s]) + float(q["t_end"].iloc[e - 1]))
    return c - length / 2, c + length / 2


def qa_figure(res, path: str | Path, title: str = "", channel: str | None = None):
    sig, q, meta = res.signals, res.quality, res.meta
    ch = channel or meta["summary"]["primary_channel"]
    t = sig["t"].to_numpy()

    fig, axes = plt.subplots(5, 1, figsize=(13, 13), constrained_layout=True)
    fig.suptitle(f"Layer 1 QA — {title}  [{ch}, fs={res.fs:g} Hz]", fontsize=12)

    ax = axes[0]
    ax.plot(t, sig[f"{ch}_raw"], lw=0.6, color="#7f7f7f", label="raw (resampled)")
    ax.plot(t, sig[f"{ch}_dc"], lw=1.2, color="#1f77b4", label="DC")
    masked = sig[f"{ch}_mask"].to_numpy() > 0
    if masked.any():
        y = sig[f"{ch}_raw"].to_numpy()
        ax.plot(t[masked], y[masked], ".", ms=1.5, color="#d62728", label="masked")
    ax.set_ylabel("counts"); ax.legend(loc="upper right", fontsize=8); ax.set_title("Raw + baseline")

    ax = axes[1]
    ax.plot(t, sig[f"{ch}_ac_norm"] * 100, lw=0.6, color="#2ca02c")
    _shade_bad(ax, q)
    ax.set_ylabel("AC/DC (%)"); ax.set_title("Morphology band, perfusion-normalised (red = rejected windows)")

    ax = axes[2]
    z = _best_zoom(q)
    if z:
        sel = (t >= z[0]) & (t <= z[1])
        def nz(v):
            v = v[sel]; s = np.nanstd(v)
            return (v - np.nanmean(v)) / s if s else v
        ax.plot(t[sel], nz(sig[f"{ch}_ac"].to_numpy()), lw=1.2, label="PPG")
        ax.plot(t[sel], nz(sig[f"{ch}_vpg"].to_numpy()) - 4, lw=0.9, label="VPG (1st deriv)")
        ax.plot(t[sel], nz(sig[f"{ch}_apg"].to_numpy()) - 8, lw=0.9, label="APG (2nd deriv)")
        ax.legend(loc="upper right", fontsize=8)
    ax.set_yticks([]); ax.set_title("10 s zoom on a good segment (z-scored, offset)")

    ax = axes[3]
    if not q.empty:
        ax.plot(q["t_center"], q[f"{ch}_hr_spec_bpm"], "o", ms=3, alpha=0.5, label="spectral")
        ax.plot(q["t_center"], q[f"{ch}_hr_acf_bpm"], "x", ms=4, alpha=0.7, label="autocorr")
        ax.plot(q["t_center"], q["hr_bpm"], "-", lw=1.5, color="k", label="accepted")
        _shade_bad(ax, q)
    ax.set_ylabel("bpm"); ax.legend(loc="upper right", fontsize=8); ax.set_title("Window heart rate")

    ax = axes[4]
    if not q.empty:
        for col, lab in [(f"{ch}_acf_peak", "ACF peak"), (f"{ch}_spec_purity", "spectral purity"),
                         (f"{ch}_valid_frac", "valid frac")]:
            if col in q:
                ax.plot(q["t_center"], q[col], lw=1, label=lab)
        _shade_bad(ax, q)
    ax.set_ylim(-0.05, 1.05); ax.set_xlabel("time (s)"); ax.legend(loc="lower right", fontsize=8)
    ax.set_title("Signal quality indices")

    for a in axes:
        if a is not axes[2]:
            a.set_xlim(t[0], t[-1])
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
