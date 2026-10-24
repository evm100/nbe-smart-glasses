"""Synthetic MAX3010x-style recordings with known ground truth.

Lets you exercise the whole pipeline (and its tests) before real lab data
arrives. Models: two-Gaussian pulse (systolic + diastolic wave), HRV,
respiratory baseline wander, slow drift, sensor noise, motion bursts,
dropped packets, timestamp jitter, optional saturation, and the inverted
orientation of raw reflected-light counts.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _pulse_train(t: np.ndarray, beats: np.ndarray, rr: np.ndarray, dia_amp: np.ndarray,
                 late_amp: np.ndarray | None = None) -> np.ndarray:
    """Sum of Gaussian waves per beat: forward (systolic), optional late-systolic
    reflected wave, diastolic wave."""
    y = np.zeros_like(t)
    if late_amp is None:
        late_amp = np.zeros_like(dia_amp)
    for tb, r, da, la in zip(beats, rr, dia_amp, late_amp):
        s = np.sqrt(r)  # wave timings stretch ~sqrt(RR)
        sel = (t >= tb) & (t < tb + 1.6 * r)
        tt = t[sel] - tb
        y[sel] += np.exp(-0.5 * ((tt - 0.17 * s) / (0.055 * s)) ** 2) \
            + da * np.exp(-0.5 * ((tt - 0.42 * s) / (0.09 * s)) ** 2)
        if la:
            y[sel] += la * np.exp(-0.5 * ((tt - 0.27 * s) / (0.05 * s)) ** 2)
    return y


def make_recording(
    duration_s: float = 120.0,
    fs: float = 100.0,
    hr_bpm: float = 72.0,
    hrv_frac: float = 0.03,
    resp_hz: float = 0.25,
    channels: dict | None = None,
    noise_frac: float = 0.03,
    motion: list[tuple[float, float]] = ((50.0, 58.0),),
    dropouts: list[tuple[float, float]] = ((90.0, 90.6),),
    saturation: list[tuple[float, float]] = (),
    jitter_ms: float = 1.5,
    adc_max: int = 2**18 - 1,
    seed: int = 0,
    dia_amp: tuple[float, float] = (0.45, 0.45),
    late_amp: tuple[float, float] = (0.0, 0.0),
) -> tuple[pd.DataFrame, dict]:
    """Return (csv_like_df, truth).

    csv_like_df has columns ``timestamp_ms`` (int, like Arduino millis()),
    ``IR``, ``RED`` – mimicking a typical MAX30102 logger.
    channels maps column -> (dc_counts, perfusion_fraction).
    dia_amp: diastolic-wave amplitude relative to systolic, linearly ramped
    from start to end of the recording (simulates a changing reflected wave,
    e.g. vascular tone / BP change) so morphology features have a known trend.
    late_amp: optional late-systolic reflected wave (between the systolic peak and
    the notch), ramped the same way - the shape stiffer arteries produce.
    """
    rng = np.random.default_rng(seed)
    channels = channels or {"IR": (120_000, 0.020), "RED": (90_000, 0.010)}
    t = np.arange(int(duration_s * fs)) / fs

    rr_mean = 60.0 / hr_bpm
    beats, rr = [], []
    tb = rng.uniform(0, rr_mean)
    while tb < duration_s + 2:
        r = rr_mean * (1 + hrv_frac * rng.standard_normal())
        beats.append(tb); rr.append(r); tb += r
    beats, rr = np.array(beats) - 1.0, np.array(rr)
    da = np.interp(beats, [0, duration_s], dia_amp)
    la = np.interp(beats, [0, duration_s], late_amp)
    pulse = _pulse_train(t, beats, rr, da, la)
    pulse /= np.percentile(pulse, 99)

    motion_sig = np.zeros_like(t)
    for a, b in motion:
        sel = (t >= a) & (t < b)
        n = sel.sum()
        # band-limited random motion, ~0.5-5 Hz, several x pulse amplitude
        wn = rng.standard_normal(n)
        spec = np.fft.rfft(wn)
        f = np.fft.rfftfreq(n, 1 / fs)
        spec[(f < 0.5) | (f > 5)] = 0
        m = np.fft.irfft(spec, n)
        motion_sig[sel] = 4.0 * m / (np.std(m) + 1e-12)

    out = {}
    for col, (dc, perf) in channels.items():
        base = dc * (1 + 0.004 * np.sin(2 * np.pi * resp_hz * t) + 0.01 * t / duration_s)
        ac_amp = dc * perf
        sig = base - ac_amp * pulse  # more blood -> less light reaches PD
        sig += ac_amp * motion_sig
        sig += ac_amp * noise_frac * rng.standard_normal(t.size)
        for a, b in saturation:
            sig[(t >= a) & (t < b)] = adc_max
        out[col] = np.clip(np.round(sig), 0, adc_max).astype(np.int64)

    ts = t * 1000 + rng.normal(0, jitter_ms, t.size)
    ts = np.maximum.accumulate(ts)
    keep = np.ones(t.size, dtype=bool)
    for a, b in dropouts:
        keep &= ~((t >= a) & (t < b))

    df = pd.DataFrame({"timestamp_ms": np.round(ts).astype(np.int64), **out})[keep].reset_index(drop=True)
    inside = (beats >= 0) & (beats < duration_s)
    # exact systolic-peak time of each isolated beat waveform (fine grid)
    tt = np.arange(0, 0.8, 0.0005)
    sys_peak = np.array([
        b + tt[np.argmax(_pulse_train(tt, np.array([0.0]), np.array([r]), np.array([a]), np.array([lv])))]
        for b, r, a, lv in zip(beats[inside], rr[inside], da[inside], la[inside])
    ])
    truth = {
        "fs": fs, "hr_bpm": hr_bpm, "beat_onsets_s": beats[inside],
        "sys_peak_s": sys_peak, "rr_s": rr[inside], "dia_amp": da[inside], "late_amp": la[inside],
        "motion": list(motion), "dropouts": list(dropouts), "saturation": list(saturation),
        "R_ratio": channels.get("RED", (1, np.nan))[1] / channels.get("IR", (1, np.nan))[1]
        if "RED" in channels and "IR" in channels else None,
    }
    return df, truth
