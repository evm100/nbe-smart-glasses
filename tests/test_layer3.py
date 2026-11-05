"""Layer 3: reference readings, session clock, pulse arrival, reading windows."""
import numpy as np
import pandas as pd
import pytest

from ppg.config import Layer3Config
from ppg.layer3 import pulse_arrival, reference_readings, window_rows
from sglab.dataset import subject_code
from sglab.timebase import device_clock, run_clock, session_t0_unix


def _ev(t, kind, **kw):
    return {"t_s": t, "kind": kind, **kw}


def test_cuff_pairing():
    ev = [
        _ev(10, "cuff_start"), _ev(11, "cuff_start"), _ev(12.5, "cuff_start"),   # repeated presses: last wins
        _ev(70, "bp", sys_mmhg=120, dia_mmhg=78, hr_bpm=66),
        _ev(100, "cuff_start"), _ev(150, "marker", label="bp error"),
        _ev(200, "cuff_start"), _ev(260, "bp", sys_mmhg=140, dia_mmhg=80),        # failed cuff ignored
        _ev(500, "bp", sys_mmhg=130, dia_mmhg=70),                                # no press at all
        _ev(520, "spo2", spo2_pct=98, hr_bpm=70),
        _ev(530, "bp", sys_mmhg="", dia_mmhg=""),                                 # incomplete: dropped
    ]
    r = reference_readings(pd.DataFrame(ev))
    assert list(r["kind"]) == ["bp", "bp", "bp", "spo2"]
    assert list(r["t_cuff_start"][:3]) == [12.5, 200, 440]
    assert list(r["cuff_found"]) == [True, True, False, False]
    assert r["t_win_start"][0] == 12.5 - 15 and r["t_win_end"][0] == 70
    assert r["map_mmhg"][0] == pytest.approx(78 + 42 / 3)
    assert r["t_win_start"][3] == 500 and r["spo2_pct"][3] == 98
    # a press before the previous result never belongs to the next one
    r2 = reference_readings([_ev(0, "cuff_start"), _ev(50, "bp", sys_mmhg=1, dia_mmhg=1),
                             _ev(90, "bp", sys_mmhg=2, dia_mmhg=2)])
    assert list(r2["cuff_found"]) == [True, False]
    assert reference_readings([]).empty


def test_session_clock_prefers_sntp():
    doc = {"started_at": "2026-10-24T02:38:20+00:00"}
    events = [{"t_s": "10.0", "wall_time": "2026-10-24T02:38:30.750+00:00"},
              {"t_s": "20.0", "wall_time": "2026-10-24T02:38:40.752+00:00"}]
    t0u = session_t0_unix(doc, events)
    start = pd.Timestamp("2026-10-24T02:38:20.751Z").timestamp()
    assert t0u == pytest.approx(start, abs=0.002)
    assert session_t0_unix(doc) == pytest.approx(start - 0.251)            # whole seconds + 0.5
    assert session_t0_unix({**doc, "t0_unix": 5.0}, events) == 5.0          # stored by the recorder

    # device clock 100 s behind unix-from-start; the PC fit is tilted by 0.5 % (bursty Wi-Fi)
    st = {"io": {"time_col": "t_dev_us"},
          "clock_fit": {"a": -100.0, "b": 1.005},
          "epoch_fit": {"a": start + 100.0, "b": 1.0, "resid_ms_max": 0.6}}
    a, b, src = device_clock(st, t0u)
    assert src == "sntp" and b == 1.0 and a == pytest.approx(100.0, abs=0.002)
    # old runs only stored t0_session_s (through clock_fit): it's inverted to device time
    t_dev0 = 900.0
    legacy = {"t0_session_s": -100.0 + 1.005 * t_dev0}
    t0, k, src = run_clock(st, legacy, t0u)
    assert src == "sntp" and t0 == pytest.approx(100.0 + t_dev0, abs=0.002) and k == 1.0
    assert run_clock(st, {"t0_dev_s": t_dev0, "t0_session_s": 0}, t0u)[0] == pytest.approx(t0)
    # a broken SNTP fit falls back to the PC fit
    bad = {**st, "epoch_fit": {**st["epoch_fit"], "resid_ms_max": 900}}
    assert device_clock(bad, t0u)[2] == "pc"
    assert run_clock({}, {"t0_session_s": 3.0}, t0u) == (3.0, 1.0, "t_s")


def test_pulse_arrival_tracks_change():
    rng = np.random.default_rng(0)
    ref = np.cumsum(np.full(400, 0.8)) + rng.normal(0, 0.002, 400)
    delay = np.where(ref < 150, 0.110, 0.095)                 # PTT shortens by 15 ms at t = 150 s
    tgt = ref + delay + rng.normal(0, 0.002, ref.size)
    good_r = rng.random(ref.size) > 0.1
    good_t = rng.random(ref.size) > 0.2
    offset, t, d = pulse_arrival(ref, good_r, tgt, good_t)
    assert 0.09 < offset < 0.115
    assert d.size > 0.6 * ref.size
    assert np.median(d[t < 150]) - np.median(d[t >= 150]) == pytest.approx(0.015, abs=0.002)


def _beats(t_foot, good):
    n = len(t_foot)
    ibi = np.r_[np.diff(t_foot), np.nan]
    return pd.DataFrame({"foot_idx": np.arange(n) * 100, "next_foot_idx": np.arange(1, n + 1) * 100,
                         "t_foot": t_foot, "ibi_s": ibi, "hr_bpm": 60 / ibi, "rise": np.linspace(0, 1, n),
                         "beat_good": good})


def test_window_rows():
    t = np.arange(0, 300, 1.0)
    beats = _beats(t, (t < 100) | (t > 120))
    waves = np.tile(np.linspace(0, 1, 100), (len(t), 1))
    readings = reference_readings([_ev(40, "cuff_start"), _ev(90, "bp", sys_mmhg=120, dia_mmhg=80),
                                   _ev(95, "cuff_start"), _ev(130, "bp", sys_mmhg=125, dia_mmhg=82)],
                                  Layer3Config())
    rows, tpl = window_rows(readings, beats, waves, t + 0.0)
    assert list(rows["n_beats"]) == [65, 50] and list(rows["n_good"]) == [65, 29]
    assert rows["hr_mean_bpm"][0] == pytest.approx(60)
    assert rows["rise_med"][0] == pytest.approx(np.median(beats["rise"][25:90]))
    assert rows["valid"].all() and tpl.shape == (2, 100)
    cfg = Layer3Config(min_good_beats=30)
    assert list(window_rows(readings, beats, waves, t, cfg)[0]["valid"]) == [True, False]
    # windows are on the session clock: beats 30 s later leave only t = 30..89 in [25, 90)
    assert window_rows(readings, beats, waves, t + 30)[0]["n_beats"][0] == 60
    empty, tp = window_rows(readings, pd.DataFrame(), np.empty((0, 100)), np.array([]))
    assert list(empty["n_good"]) == [0, 0] and not empty["valid"].any() and np.isnan(tp).all()


def test_subject_code():
    assert subject_code("S002 - someone") == "S002"
    assert subject_code("S013") == "S013"
    assert subject_code("pilot") == "pilot" and subject_code(None) is None
