"""Record realistic SIMULATED sessions (real time, ~3 min) to try the catalog + web UI.

Run from the repo root:  python scripts/demo_sessions.py   then   sglab process --all && sglab serve
"""
import sys, time, threading
from sglab.settings import load_settings
from sglab.catalog import Catalog
from sglab.recorder import Recording

s = load_settings(); cat = Catalog(s.db_path, s.data_root)

def session(subj, proto, devices, dur, events, sim):
    cat.upsert_subject({"subject_id": subj, "sex": "na", "fitzpatrick": "III"}) if not cat.subject(subj) else None
    rec = Recording(s, [s.device(d) for d in devices], sim_kw=sim)
    rec.start({"subject_id": subj, "protocol": proto})
    t0 = time.monotonic()
    for t, kind, kw in events:
        time.sleep(max(0, t - (time.monotonic() - t0)))
        rec.add_event(kind, **kw)
    time.sleep(max(0, dur - (time.monotonic() - t0)))
    rec.stop()
    rec.subject_snapshot = cat.subject(subj)
    meta = {"subject_id": subj, "protocol": proto, "trial": cat.next_trial(subj, proto), "posture": "sitting",
            "operator": "EV", "quality": "good", "notes": "simulated demo session", "keep": True}
    dest, doc = rec.finalize(meta)
    print("saved", cat.upsert_session(dest), flush=True)

ev = [(20, "bp", dict(sys_mmhg=118, dia_mmhg=76, hr_bpm=71)), (25, "spo2", dict(spo2_pct=98, hr_bpm=72)),
      (40, "marker", dict(label="hand into ice water")), (60, "cuff_start", dict(label="cuff")),
      (95, "bp", dict(sys_mmhg=134, dia_mmhg=88, hr_bpm=78)), (100, "marker", dict(label="hand out")),
      (140, "cuff_start", dict(label="cuff")), (175, "bp", dict(sys_mmhg=122, dia_mmhg=79, hr_bpm=73)),
      (178, "spo2", dict(spo2_pct=98, hr_bpm=73))]
th = [threading.Thread(target=session, args=("S003", "cold_pressor", ["sim", "sim_glasses"], 185, ev,
                                             {"dia_amp": (0.6, 0.3), "motion": [(70, 76)]}))]
th.append(threading.Thread(target=session, args=("S004", "rest", ["sim"], 120,
          [(30, "bp", dict(sys_mmhg=115, dia_mmhg=74, hr_bpm=66)), (110, "bp", dict(sys_mmhg=114, dia_mmhg=73, hr_bpm=65))],
          {"hr_bpm": 66, "dia_amp": (0.45, 0.45), "seed": 4})))
[t.start() for t in th]; [t.join() for t in th]
