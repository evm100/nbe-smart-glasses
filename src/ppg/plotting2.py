"""Layer 2 QA figures.

  <stem>_beats.png         fiducials on a strip, beat ensemble, tachogram + beat SQI
  <stem>_features.png      key features over time (beats + epoch medians)
  <stem>_feature_corr.png  feature-feature correlation (redundancy check for ML)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

# reference categorical palette (fixed order) + neutrals
C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100",
     "magenta": "#e87ba4", "violet": "#4a3aa7"}
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#a3a29b", "#e4e3de"
FID_STYLE = {  # landmark -> (colour, marker, label)
    "foot": (C["blue"], "o", "foot"),
    "max_slope": (C["aqua"], "^", "max slope"),
    "peak": (C["orange"], "o", "systolic peak"),
    "notch": (C["violet"], "v", "dicrotic notch"),
    "dia": (C["magenta"], "s", "diastolic peak"),
}
DIVERGING = LinearSegmentedColormap.from_list("div", [C["blue"], "#f0efeb", C["orange"]])


def _style(ax):
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.title.set_color(INK)


def _strip_window(beats, length=8.0):
    g = beats["beat_good"].to_numpy(bool)
    if not g.any():
        return None
    # longest run of good beats -> centre
    from .filters import contiguous_runs
    s, e = max(contiguous_runs(g), key=lambda r: r[1] - r[0])
    c = 0.5 * (beats["t_foot"].iloc[s] + beats["t_foot"].iloc[e - 1])
    return c - length / 2, c + length / 2


def beats_figure(l1, l2, path, title=""):
    sig, b, fs = l1.signals, l2.beats, l1.fs
    ch = l2.meta["summary"]["channel"]
    t = sig["t"].to_numpy()
    x = sig[f"{ch}_ac_norm"].to_numpy() * 100
    apg = sig[f"{ch}_apg"].to_numpy()

    fig = plt.figure(figsize=(13, 12), constrained_layout=True)
    gs = fig.add_gridspec(4, 2, height_ratios=[1.2, 0.8, 1.2, 1.0])
    fig.suptitle(f"Layer 2 beats — {title}  [{ch}]", color=INK, fontsize=12)

    win = _strip_window(b) if len(b) else None
    ax1 = fig.add_subplot(gs[0, :]); ax2 = fig.add_subplot(gs[1, :], sharex=ax1)
    if win:
        sel = (t >= win[0]) & (t <= win[1])
        ax1.plot(t[sel], x[sel], color=INK2, lw=1.2)
        ax2.plot(t[sel], apg[sel], color=INK2, lw=1.0)
        inwin = b[(b["t_foot"] >= win[0]) & (b["t_foot"] <= win[1])]
        for k, (col, mk, lab) in FID_STYLE.items():
            idx = inwin[f"{k}_idx"].to_numpy()
            idx = idx[(idx >= 0)]
            ax1.plot(t[idx], x[idx], mk, color=col, ms=7, mec="white", mew=1, label=lab, ls="none")
        for k, col in zip("abcde", [C["blue"], C["orange"], C["aqua"], C["yellow"], C["magenta"]]):
            idx = inwin[f"{k}_idx"].to_numpy()
            idx = idx[idx >= 0]
            ax2.plot(t[idx], apg[idx], "o", color=col, ms=6, mec="white", mew=1, label=k, ls="none")
        ax1.set_xlim(*win)
    ax1.set_title("Fiducial points on the longest clean stretch", loc="left", fontsize=10)
    ax1.set_ylabel("AC/DC (%)", color=INK2)
    ax1.legend(loc="lower right", bbox_to_anchor=(1, 1.0), fontsize=8, ncol=5, frameon=False)
    ax2.set_title("2nd derivative (APG) with a–e waves", loc="left", fontsize=10)
    ax2.set_ylabel("APG (1/s²)", color=INK2); ax2.set_xlabel("time (s)", color=INK2)
    ax2.legend(loc="lower right", bbox_to_anchor=(1, 1.0), fontsize=8, ncol=5, frameon=False)

    # ensemble overlays
    g = b["beat_good"].to_numpy(bool) if len(b) else np.array([], bool)
    w = l2.waves
    ph = np.linspace(0, 1, w.shape[1]) if w.size else np.array([])
    ax3 = fig.add_subplot(gs[2, 0]); ax4 = fig.add_subplot(gs[2, 1], sharey=ax3)
    if g.any():
        for row in w[g]:
            ax3.plot(ph, row, color=C["blue"], lw=0.5, alpha=0.15)
        tmpl = np.nanmedian(w[g], axis=0)
        ax3.plot(ph, tmpl, color=INK, lw=2, label="median template")
        ax4.plot(ph, tmpl, color=INK, lw=2)
        gcfg = l2.meta.get("config", {}).get("gaussian", {})
        if gcfg.get("enabled", False) and np.isfinite(tmpl).all():
            from .config import GaussianConfig
            from .gaussian import gauss_sum
            from .gaussian import fit_template
            gc = GaussianConfig(**gcfg)
            p, r2 = fit_template(tmpl, gc)
            lo, hi = np.nanmin(tmpl), np.nanmax(tmpl)
            comp_colors = [C["orange"], C["aqua"], C["violet"], C["magenta"], C["yellow"]]
            for j, (a, mu, sd) in enumerate(p.reshape(-1, 3)):
                comp = a * np.exp(-0.5 * ((ph - mu) / sd) ** 2) * (hi - lo) + lo
                ax3.plot(ph, comp, "--", lw=1.6, color=comp_colors[j % 5], label=f"g{j + 1}")
            ax3.plot(ph, gauss_sum(ph, p) * (hi - lo) + lo, ":", lw=1.6, color=INK2,
                     label=f"Gaussian sum (R²={r2:.3f})")
        ax3.legend(loc="upper right", fontsize=8, frameon=False)
    if (~g).any():
        for row in w[~g]:
            ax4.plot(ph, row, color=MUTED, lw=0.6, alpha=0.5)
    ax3.set_title(f"Accepted beats (n={int(g.sum())})", loc="left", fontsize=10)
    ax4.set_title(f"Rejected beats (n={int((~g).sum())}) vs template", loc="left", fontsize=10)
    for a in (ax3, ax4):
        a.set_xlabel("fraction of beat (foot → next foot)", color=INK2)
    ax3.set_ylabel("normalised amplitude", color=INK2)

    # tachogram + template correlation
    ax5 = fig.add_subplot(gs[3, :])
    if len(b):
        ax5.plot(b.loc[g, "t_foot"], b.loc[g, "ibi_s"] * 1000, "o", ms=3.5, color=C["blue"], label="accepted")
        ax5.plot(b.loc[~g, "t_foot"], b.loc[~g, "ibi_s"] * 1000, "x", ms=5, color=MUTED, label="rejected")
        lo, hi = np.nanpercentile(b["ibi_s"] * 1000, [1, 99])
        ax5.set_ylim(lo - 0.15 * (hi - lo), hi + 0.15 * (hi - lo))
    ax5.set_title("Inter-beat interval (tachogram)", loc="left", fontsize=10)
    ax5.set_ylabel("IBI (ms)", color=INK2); ax5.set_xlabel("time (s)", color=INK2)
    ax5.legend(loc="lower right", bbox_to_anchor=(1, 1.0), fontsize=8, ncol=2, frameon=False)

    for a in fig.axes:
        _style(a)
    fig.savefig(path, dpi=110)
    plt.close(fig)


KEY_FEATURES = [
    ("hr_bpm", "Heart rate", "bpm"),
    ("t_rise_s", "Rise (crest) time", "s"),
    ("pw50_s", "Pulse width @50 %", "s"),
    ("dw50_s", "Diastolic width @50 %", "s"),
    ("dia_rel", "Reflection index (dia/sys)", "ratio"),
    ("ipa", "Inflection-point area ratio", "ratio"),
    ("max_slope_norm", "Normalised max upslope", "1/s"),
    ("amp", "Beat amplitude (AC/DC)", "ratio"),
    ("g2_amp_ratio", "Gaussian g2/g1 (early reflected wave)", "ratio"),
    ("g3_amp_ratio", "Gaussian g3/g1 (diastolic wave)", "ratio"),
]


def features_figure(l2, path, title=""):
    b, e = l2.beats, l2.epochs
    fig, axes = plt.subplots(5, 2, figsize=(13, 13.5), constrained_layout=True, sharex=True)
    fig.suptitle(f"Layer 2 features over time — {title}", color=INK, fontsize=12)
    g = b["beat_good"].to_numpy(bool) if len(b) else np.array([], bool)
    for ax, (col, name, unit) in zip(axes.flat, KEY_FEATURES):
        if col in b:
            ax.plot(b.loc[g, "t_foot"], b.loc[g, col], ".", ms=3, color=C["blue"], alpha=0.35, label="beat")
        if len(e) and f"{col}_med" in e:
            v = e["valid"].to_numpy(bool)
            ax.plot(e.loc[v, "t_center"], e.loc[v, f"{col}_med"], "-", lw=2, color=C["orange"], label="epoch median")
        ax.set_title(name, loc="left", fontsize=10)
        ax.set_ylabel(unit, color=INK2)
        _style(ax)
    axes.flat[0].legend(loc="upper right", fontsize=8, frameon=False)
    for ax in axes[-1]:
        ax.set_xlabel("time (s)", color=INK2)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def correlation_figure(l2, path, title=""):
    b = l2.beats
    if not len(b):
        return
    g = b["beat_good"].to_numpy(bool)
    skip = {"gauss_ok", "gauss_r2", "t_foot", "t_peak", "t_max_slope", "foot_tan_s", "notch_method", "ibi_ratio", "amp_ratio", "template_corr", "dc"}
    cols = [c for c in b.columns if not c.endswith("_idx") and c not in skip
            and pd.api.types.is_numeric_dtype(b[c]) and b.loc[g, c].notna().mean() > 0.5
            and b.loc[g, c].std() > 0]
    if len(cols) < 2:
        return
    corr = b.loc[g, cols].corr(method="spearman")
    fig, ax = plt.subplots(figsize=(0.38 * len(cols) + 3, 0.38 * len(cols) + 2), constrained_layout=True)
    im = ax.imshow(corr.to_numpy(), cmap=DIVERGING, vmin=-1, vmax=1)
    ax.set_xticks(range(len(cols)), cols, rotation=90, fontsize=8, color=INK2)
    ax.set_yticks(range(len(cols)), cols, fontsize=8, color=INK2)
    ax.set_title(f"Spearman correlation between beat features (accepted beats) — {title}", fontsize=10, color=INK)
    cb = fig.colorbar(im, ax=ax, shrink=0.6)
    cb.ax.tick_params(labelsize=8, colors=INK2)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def layer2_figures(l1, l2, out_dir, stem, title=""):
    out_dir = Path(out_dir)
    beats_figure(l1, l2, out_dir / f"{stem}_beats.png", title)
    features_figure(l2, out_dir / f"{stem}_features.png", title)
    correlation_figure(l2, out_dir / f"{stem}_feature_corr.png", title)
