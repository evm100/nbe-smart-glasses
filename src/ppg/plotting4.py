"""Layer 4 report figure: one row per target.

  predicted vs cuff     out-of-fold predictions (model) and the baseline, identity line
  Bland-Altman          model error vs mean of cuff and prediction, bias +- 1.96 SD
  weights               ridge weights per standardised feature (mmHg per SD of the feature)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .plotting2 import C, INK, INK2, MUTED, _style  # noqa: E402

LABEL = {"sys_mmhg": "systolic", "dia_mmhg": "diastolic", "map_mmhg": "mean arterial"}


def plot_report(preds: pd.DataFrame, metrics: dict, coefs: dict, title: str, out: str | Path) -> Path:
    targets = list(metrics)
    fig, axes = plt.subplots(len(targets), 3, figsize=(13, 3.9 * len(targets)), squeeze=False)
    for i, t in enumerate(targets):
        p = preds[preds["target"] == t]
        cv, ho = p[p["split"] == "cv"], p[p["split"] == "holdout"]
        mm, mb = metrics[t]["cv"]["model"], metrics[t]["cv"]["baseline"]
        name = LABEL.get(t, t)

        ax = axes[i, 0]
        ax.scatter(cv["y"], cv["baseline"], s=26, facecolors="none", edgecolors=C["orange"], lw=1.1,
                   label=f"baseline  MAE {mb['mae']:.1f}")
        ax.scatter(cv["y"], cv["pred"], s=22, color=C["blue"], label=f"model  MAE {mm['mae']:.1f}")
        if len(ho):
            ax.scatter(ho["y"], ho["pred"], s=34, marker="D", color=C["aqua"],
                       label=f"holdout  MAE {metrics[t]['holdout']['model']['mae']:.1f}")
        v = np.concatenate([p["y"], p["pred"], p["baseline"]])
        lo, hi = np.nanmin(v) - 3, np.nanmax(v) + 3
        ax.plot([lo, hi], [lo, hi], color=MUTED, lw=0.8)
        ax.set(xlim=(lo, hi), ylim=(lo, hi), xlabel=f"cuff {name} (mmHg)", ylabel="predicted (mmHg)")
        ax.set_title(f"{name}: out-of-fold predictions", fontsize=10, color=INK, loc="left")
        ax.legend(fontsize=7.5, frameon=False, loc="upper left")
        _style(ax)

        ax = axes[i, 1]
        mean, err = (cv["y"] + cv["pred"]) / 2, cv["pred"] - cv["y"]
        ax.scatter(mean, err, s=22, color=C["blue"])
        if np.isfinite(mm["sd"]):
            for y, ls in ((mm["me"], "-"), (mm["me"] + 1.96 * mm["sd"], "--"), (mm["me"] - 1.96 * mm["sd"], "--")):
                ax.axhline(y, color=INK2, lw=0.9, ls=ls)
        ax.axhline(0, color=MUTED, lw=0.6)
        ax.set(xlabel="mean of cuff and model (mmHg)", ylabel="model - cuff (mmHg)")
        ax.set_title(f"Bland-Altman: {mm['me']:+.1f} ± {mm['sd']:.1f} mmHg (n={mm['n']})",
                     fontsize=10, color=INK, loc="left")
        _style(ax)

        ax = axes[i, 2]
        w = coefs.get(t)
        if w:
            names = list(w)[::-1]
            vals = np.array([w[n] for n in names])
            ax.barh(names, vals, color=[C["orange"] if x > 0 else C["blue"] for x in vals])
            ax.axvline(0, color=MUTED, lw=0.6)
            ax.set_xlabel("mmHg per SD of feature")
            ax.tick_params(axis="y", labelsize=7.5)
        else:
            ax.text(0.5, 0.5, "no linear weights for this model", ha="center", va="center",
                    color=INK2, transform=ax.transAxes)
            ax.set_xticks([]); ax.set_yticks([])
        ax.set_title("final model weights", fontsize=10, color=INK, loc="left")
        _style(ax)
    fig.suptitle(title, fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    out = Path(out)
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out
