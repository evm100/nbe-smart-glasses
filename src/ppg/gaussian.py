"""Layer 2e: Gaussian decomposition of each pulse.

Each beat (foot -> next foot, baseline-removed, scaled 0..1, time scaled 0..1)
is modelled as a sum of K Gaussians:

    y(t) = sum_i a_i * exp(-(t - mu_i)^2 / (2 sigma_i^2)),   mu_1 < mu_2 < ... < mu_K

With K = 3 the components are usually read as
  g1  forward (systolic) wave from ventricular ejection
  g2  early reflected / late-systolic wave
  g3  diastolic wave (later reflections + aortic valve closure)
Their relative heights, spacings and widths describe how the reflected wave
interferes with the forward wave, which changes with arterial stiffness and
vascular tone. Finnegan et al. (Sci. Rep. 2023) found Gaussian components
among the top PPG features for estimating changes in systolic BP.

Fitting: bounded non-linear least squares (scipy.optimize.least_squares) with
an ordered, g1-dominant parameterisation (see below), initialised from the
beat's fiducials (systolic peak, notch, diastolic peak) and warm-started from
the previous beat. Fits with R^2 below `min_r2` give NaN features (and
`gauss_ok = False`) instead of misleading numbers.

Features (per beat; all shape-based, so independent of signal amplitude):
  g{i}_amp, g{i}_mu, g{i}_sigma     height (beat peak = 1), centre and width (fraction of beat)
  g{i}_t_s                          centre in seconds after the foot
  g{i}_area                         a * sigma * sqrt(2 pi)
  g{i}_amp_ratio, g{i}_area_ratio   relative to g1 (reflection strength)
  g{i}_dt_s                         delay after g1 in seconds (reflection timing)
  gauss_r2, gauss_ok                fit quality
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from .config import GaussianConfig

SQRT_2PI = np.sqrt(2 * np.pi)


def gauss_sum(t: np.ndarray, p: np.ndarray) -> np.ndarray:
    """p = [a1, mu1, s1, a2, mu2, s2, ...]."""
    y = np.zeros_like(t)
    for a, mu, s in p.reshape(-1, 3):
        y += a * np.exp(-0.5 * ((t - mu) / s) ** 2)
    return y


# --- parameterisation ---------------------------------------------------------
# Fitting K free Gaussians is ill-posed: one wave can be split into two
# near-identical ones and the labels swap from beat to beat. So we fit
#   theta = [a1, mu1, s1,  r2, d2, s2,  r3, d3, s3, ...]
# with a_i = r_i * a1 (r_i <= 1: g1 is the dominant forward wave) and
# mu_i = mu_{i-1} + d_i (d_i >= min_sep: ordered, never on top of each other),
# plus a small ridge penalty on the middle components' r_i so a component the
# beat doesn't need shrinks to ~0 instead of soaking up noise.

def _theta_bounds(k: int, cfg: GaussianConfig):
    lo = [0.2, 0.02, cfg.sigma_min]
    hi = [1.5, 0.45, cfg.sigma_max]
    for _ in range(1, k):
        lo += [0.0, cfg.min_sep, cfg.sigma_min]
        hi += [1.0, 0.6, cfg.sigma_max]
    return np.array(lo), np.array(hi)


def theta_to_params(th: np.ndarray) -> np.ndarray:
    th = th.reshape(-1, 3)
    p = np.empty_like(th)
    p[0] = th[0]
    for i in range(1, len(th)):
        p[i, 0] = th[i, 0] * th[0, 0]
        p[i, 1] = p[i - 1, 1] + th[i, 1]
        p[i, 2] = th[i, 2]
    return p.ravel()


def params_to_theta(p: np.ndarray) -> np.ndarray:
    p = p.reshape(-1, 3)
    th = np.empty_like(p)
    th[0] = p[0]
    for i in range(1, len(p)):
        th[i, 0] = p[i, 0] / p[0, 0] if p[0, 0] > 0 else 0.0
        th[i, 1] = p[i, 1] - p[i - 1, 1]
        th[i, 2] = p[i, 2]
    return th.ravel()


def _initial(k: int, peak: float, notch: float | None, dia: float | None) -> np.ndarray:
    """Initial guess (in params form) from fiducials, positions as fraction of the beat."""
    peak = float(np.clip(peak, 0.05, 0.4))
    dia = float(np.clip(dia if dia is not None and np.isfinite(dia) else peak + 0.3, peak + 0.12, 0.9))
    notch = float(np.clip(notch if notch is not None and np.isfinite(notch) else (peak + dia) / 2,
                          peak + 0.06, dia - 0.04))
    if k == 2:
        return np.array([1.0, peak, 0.06, 0.4, dia, 0.10])
    if k == 3:
        return np.array([1.0, peak, 0.06, 0.25, min(peak + 0.08, notch - 0.01), 0.06, 0.4, dia, 0.10])
    mus = np.linspace(peak, dia, k)
    return np.concatenate([[1.0 if i == 0 else 0.4, m, 0.07] for i, m in enumerate(mus)])


def fit_beat(y: np.ndarray, cfg: GaussianConfig, x0: np.ndarray, t: np.ndarray | None = None):
    """Fit one normalised beat. x0 and the result are in params form
    [a1, mu1, s1, a2, mu2, s2, ...] (ordered by mu). Returns (params, r2)."""
    t = np.linspace(0, 1, y.size) if t is None else t
    k = x0.size // 3
    lo, hi = _theta_bounds(k, cfg)
    th0 = np.clip(params_to_theta(x0), lo + 1e-9, hi - 1e-9)
    lam = np.sqrt(cfg.ridge_middle)
    mid = slice(3, 3 * (k - 1), 3)  # r_i of the components between first and last

    def resid(th):
        r = gauss_sum(t, theta_to_params(th)) - y
        return np.concatenate([r, lam * th[mid]]) if k > 2 and lam > 0 else r

    res = least_squares(resid, th0, bounds=(lo, hi), method="trf", x_scale="jac", max_nfev=cfg.max_nfev)
    p = theta_to_params(res.x)
    fit_err = gauss_sum(t, p) - y
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - float(np.sum(fit_err ** 2)) / ss_tot if ss_tot > 0 else np.nan
    return p, r2, float(res.cost)


def _canonical(k: int, peak: float) -> np.ndarray:
    """Textbook layout relative to the systolic peak (third start point)."""
    peak = float(np.clip(peak, 0.05, 0.4))
    if k == 3:
        return np.array([1.0, peak, 0.06, 0.3, peak + 0.10, 0.05, 0.4, peak + 0.27, 0.10])
    if k == 2:
        return np.array([1.0, peak, 0.06, 0.4, peak + 0.27, 0.10])
    return _initial(k, peak, None, None)


def fit_beat_multistart(y: np.ndarray, cfg: GaussianConfig, starts: list[np.ndarray], t: np.ndarray):
    """Fit from several start points and keep the lowest-cost solution.

    A single start sometimes lands in a local minimum (e.g. the middle Gaussian
    swallowing the diastolic wave and the last one fitting a tiny tail bump).
    """
    best = None
    for x0 in starts:
        if x0 is None:
            continue
        p, r2, cost = fit_beat(y, cfg, x0, t)
        if np.isfinite(cost) and (best is None or cost < best[2]):
            best = (p, r2, cost)
        if best is not None and best[1] >= cfg.r2_good_enough:
            break  # already an excellent fit: skip the remaining starts (speed)
    return best[0], best[1]


def gaussian_features(waves: np.ndarray, fid: pd.DataFrame, ibi_s: np.ndarray, cfg: GaussianConfig) -> pd.DataFrame:
    """One row of Gaussian features per beat (rows aligned with `waves` / `fid`)."""
    k = cfg.n_components
    n = waves.shape[0]
    cols = {}
    names = [f"g{i + 1}" for i in range(k)]
    for g in names:
        for q in ("amp", "mu", "sigma", "t_s", "area"):
            cols[f"{g}_{q}"] = np.full(n, np.nan)
    for g in names[1:]:
        cols[f"{g}_amp_ratio"] = np.full(n, np.nan)    # a_i / a_1
        cols[f"{g}_area_ratio"] = np.full(n, np.nan)   # area_i / area_1
        cols[f"{g}_dt_s"] = np.full(n, np.nan)         # mu_i - mu_1 in seconds
    cols["gauss_r2"] = np.full(n, np.nan)
    cols["gauss_ok"] = np.zeros(n, dtype=bool)

    if n == 0:
        return pd.DataFrame(cols)

    t = np.linspace(0, 1, waves.shape[1])
    span = (fid["next_foot"] - fid["foot"]).to_numpy(dtype=float)
    frac = lambda col: np.where(fid[col].to_numpy() >= 0, (fid[col] - fid["foot"]).to_numpy() / span, np.nan)  # noqa: E731
    peak_f, notch_f, dia_f = frac("peak"), frac("notch"), frac("dia")

    prev = None
    for i in range(n):
        y = waves[i]
        if not np.all(np.isfinite(y)):
            continue
        starts = [_initial(k, peak_f[i], notch_f[i], dia_f[i]),   # from this beat's fiducials
                  prev,                                          # warm start: previous good beat
                  _canonical(k, peak_f[i] if np.isfinite(peak_f[i]) else 0.2)]
        best_p, best_r2 = fit_beat_multistart(y, cfg, starts, t)
        cols["gauss_r2"][i] = best_r2
        if not np.isfinite(best_r2) or best_r2 < cfg.min_r2:
            continue
        prev = best_p
        cols["gauss_ok"][i] = True
        p = best_p.reshape(-1, 3)
        area = p[:, 0] * p[:, 2] * SQRT_2PI
        for j, g in enumerate(names):
            cols[f"{g}_amp"][i] = p[j, 0]
            cols[f"{g}_mu"][i] = p[j, 1]
            cols[f"{g}_sigma"][i] = p[j, 2]
            cols[f"{g}_t_s"][i] = p[j, 1] * ibi_s[i]
            cols[f"{g}_area"][i] = area[j]
            if j > 0:
                cols[f"{g}_amp_ratio"][i] = p[j, 0] / p[0, 0] if p[0, 0] > 0 else np.nan
                cols[f"{g}_area_ratio"][i] = area[j] / area[0] if area[0] > 0 else np.nan
                cols[f"{g}_dt_s"][i] = (p[j, 1] - p[0, 1]) * ibi_s[i]
    return pd.DataFrame(cols)


def fit_template(template: np.ndarray, cfg: GaussianConfig) -> tuple[np.ndarray, float]:
    """Fit an ensemble-average beat (used for the QA figure)."""
    t = np.linspace(0, 1, template.size)
    y = template - template.min()
    if y.max() > 0:
        y = y / y.max()
    peak = t[int(np.argmax(y))]
    k = cfg.n_components
    return fit_beat_multistart(y, cfg, [_initial(k, peak, None, None), _canonical(k, peak)], t)
