"""`sglab tune score`: compare site settings on the locked test set, champion vs. challengers.

    sglab tune split --data tune-data                    subjects and which ones are test
    sglab tune split --data tune-data --add S003         lock all of S003's sessions into the test set
    sglab tune score --site glasses                      the current settings alone
    sglab tune score --site glasses --config current runs/tune/glasses-beats runs/tune/glasses-beats:269

--config entries (the first one is the champion, the others are compared to it):
  current            config/sites/<site>.json as processing uses it now
  default            the base pipeline settings, no site overrides
  git:<rev>          config/sites/<site>.json at a git revision (on today's base settings)
  <study>[:<trial>]  a tune study's recommended setting, or one of its trials
  <file>.json        a site overlay file

The test set (config/test_sessions.json) holds whole subjects: `tune run` never sees them, so
these scores say how a setting does on people it was not tuned on. Every setting is scored on
the same sessions against the same finger reference (the current finger settings).

Scores, pooled over sessions (see ppg.tuning):
  f1, sensitivity, ppv   good target beats vs. good finger beats
  ibi_mae_ms             interval error on all matched intervals of that setting
  ibi_shared_ms          interval error on the finger intervals that *every* setting matched:
                         a strict gate keeps only easy beats, so its plain ibi_mae_ms looks low
                         for the wrong reason; this one compares like with like
  good_beats_per_min     good target beats per minute of recording
  epoch_frac             share of 30 s epochs (Layer-2 epoch settings) with enough good beats,
                         i.e. what Layer 3 gets to use

95 % intervals: bootstrap over subjects (over sessions when there are < 3 subjects, which only
says the result holds for these people). Challenger deltas use the same resamples (paired).

A challenger replaces the champion when the F1 gain's interval is above 0, PPV drops by at
most --max-ppv-drop and the shared-interval error rises by at most --max-ibi-rise-ms.
"""
from __future__ import annotations

import copy
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ppg.config import Layer1Config, Layer2Config, with_overrides

from .processing import pipeline_configs, site_overrides, stream_configs
from .settings import load_settings
from .tune import REF_ROLE, TEST_FILE, Evaluator, find_pairs, is_test, load_test_split, study_overlay

METRICS = ["f1", "sensitivity", "ppv", "ibi_mae_ms", "ibi_shared_ms", "good_beats_per_min", "epoch_frac"]
COUNTS = ["tp", "n_ref", "n_tgt", "ibi_sum", "n_ibi", "shared_sum", "n_shared", "n_good", "minutes",
          "n_usable", "n_windows"]


@dataclass
class Candidate:
    label: str
    cfg1: Layer1Config
    cfg2: Layer2Config
    source: str


def resolve(spec: str, site: str, root: Path) -> Candidate:
    settings = load_settings(root)
    b1, b2 = pipeline_configs(settings)
    role = {"role": site}
    if spec == "current":
        c1, c2 = stream_configs(b1, b2, site_overrides(settings), role)
        return Candidate(spec, c1, c2, f"config/sites/{site}.json")
    if spec == "default":
        c1, c2 = stream_configs(b1, b2, {}, role)
        return Candidate(spec, c1, c2, "pipeline_layer1/2.json")
    if spec.startswith("git:"):
        rev = spec[4:]
        r = subprocess.run(["git", "show", f"{rev}:config/sites/{site}.json"], cwd=root, capture_output=True, text=True)
        if r.returncode and "exists on disk, but not in" not in r.stderr and "does not exist" not in r.stderr:
            raise ValueError(f"{spec}: {r.stderr.strip()}")
        overlay = json.loads(r.stdout) if r.returncode == 0 else {}
        c1, c2 = stream_configs(b1, b2, {site: overlay}, role)
        return Candidate(spec, c1, c2, f"{rev}:config/sites/{site}.json")
    path, trial = spec, None
    head, _, tail = spec.rpartition(":")
    if head and tail.isdigit():
        path, trial = head, int(tail)
    path = Path(path)
    if (path / "meta.json").exists():
        meta, overlay = study_overlay(path, trial)
        if meta["site"] != site:
            raise ValueError(f"{spec} is a {meta['site']} study, not {site}")
        # on the settings the study started from: what its scores were measured with
        c1 = with_overrides(Layer1Config.from_dict(meta["start"]["layer1"]), overlay.get("layer1"))
        c2 = with_overrides(Layer2Config.from_dict(meta["start"]["layer2"]), overlay.get("layer2"))
        return Candidate(spec, c1, c2, f"{path} trial {trial if trial is not None else 'recommended'}")
    if path.suffix == ".json" and path.is_file():
        c1, c2 = stream_configs(b1, b2, {site: json.loads(path.read_text())}, role)
        return Candidate(spec, c1, c2, str(path))
    raise ValueError(f"--config {spec}: not current/default/git:<rev>, a tune study folder or a .json file")


def _usable_epochs(t_good: np.ndarray, span: tuple[float, float], cfg2: Layer2Config) -> tuple[int, int]:
    e = cfg2.epochs
    dur = span[1] - span[0]
    starts = span[0] + np.arange(0, max(dur - e.epoch_s, 0) + 1e-9, e.step_s)
    n = np.searchsorted(t_good, starts + e.epoch_s) - np.searchsorted(t_good, starts)
    return int((n >= e.min_good_beats).sum()), int(starts.size)


def _metrics(c: dict) -> dict:
    """Scores from summed counts (scalars, or arrays: one per config / bootstrap sample)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        sens = c["tp"] / c["n_ref"]
        ppv = c["tp"] / c["n_tgt"]
        return {"f1": np.nan_to_num(2 * sens * ppv / (sens + ppv)), "sensitivity": sens, "ppv": ppv,
                "ibi_mae_ms": 1000 * c["ibi_sum"] / c["n_ibi"],
                "ibi_shared_ms": 1000 * c["shared_sum"] / c["n_shared"],
                "good_beats_per_min": c["n_good"] / c["minutes"], "epoch_frac": c["n_usable"] / c["n_windows"]}


def _boot_weights(groups: list[np.ndarray], n_sessions: int, n_boot: int, seed: int) -> np.ndarray:
    """(n_boot, n_sessions) counts: each sample draws len(groups) groups with replacement."""
    rng = np.random.default_rng(seed)
    w = np.zeros((n_boot, n_sessions))
    for b in range(n_boot):
        for g in rng.integers(0, len(groups), len(groups)):
            w[b, groups[g]] += 1
    return w


def _ci(boot: np.ndarray) -> tuple[float, float]:
    lo, hi = np.nanpercentile(boot, [2.5, 97.5]) if np.isfinite(boot).any() else (np.nan, np.nan)
    return float(lo), float(hi)


def _git_rev(root: Path) -> str:
    r = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=root, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def score(data: Path, site: str, specs: list[str], root: Path, split: str = "test",
          sessions: list[str] | None = None, skip_bad: bool = True, n_boot: int = 2000, seed: int = 0,
          max_ppv_drop: float = 0.05, max_ibi_rise_ms: float = 0.5, out: Path | None = None,
          log=print) -> dict:
    cands = [resolve(s, site, root) for s in (specs or ["current"])]
    pairs = find_pairs(data, site, sessions, skip_bad)
    if split == "test":
        pairs = [p for p in pairs if is_test(p, load_test_split(root))]
        if not pairs:
            raise ValueError(f"no {site} session of the test set ({TEST_FILE}) in {data}. Lock subjects with "
                             f"`sglab tune split --data {data} --add <subject>`, or use --split all (which "
                             f"includes sessions the studies were tuned on)")
    elif not pairs:
        raise ValueError(f"no session in {data} has both a '{REF_ROLE}' and a '{site}' stream")
    settings = load_settings(root)
    b1, b2 = pipeline_configs(settings)
    r1, r2 = stream_configs(b1, b2, site_overrides(settings), {"role": REF_ROLE})
    ev = Evaluator(pairs, site, cands[0].cfg1, cands[0].cfg2, r1, r2).load(log)

    nc, ns = len(cands), len(pairs)
    C = {k: np.zeros((nc, ns)) for k in COUNTS}
    ibi = [[{} for _ in range(ns)] for _ in range(nc)]  # finger interval -> error, per config and session
    for ci, c in enumerate(cands):
        cfg2 = copy.deepcopy(c.cfg2)
        cfg2.gaussian.enabled = False
        for si, (p, _, t_good, span, a) in enumerate(ev.sessions(c.cfg1, cfg2)):
            for k, v in (("tp", a["tp"]), ("n_ref", a["n_ref"]), ("n_tgt", a["n_tgt"]),
                         ("ibi_sum", a["ibi_abs_err_sum"]), ("n_ibi", a["n_ibi"]), ("n_good", t_good.size),
                         ("minutes", (span[1] - span[0]) / 60)):
                C[k][ci, si] = v
            C["n_usable"][ci, si], C["n_windows"][ci, si] = _usable_epochs(t_good, span, c.cfg2)
            ibi[ci][si] = dict(zip(a["ibi_ref_idx"], np.abs(a["ibi_err"])))
        log(f"  scored {c.label}")
    for si in range(ns):
        shared = set.intersection(*(set(ibi[ci][si]) for ci in range(nc)))
        C["n_shared"][:, si] = len(shared)
        for ci in range(nc):
            C["shared_sum"][ci, si] = sum(ibi[ci][si][k] for k in shared)

    subjects = sorted({p.subject.split()[0] if p.subject.strip() else p.session_id for p in pairs})
    by = "subject" if len(subjects) >= 3 else "session"
    keys = [(p.subject.split()[0] if p.subject.strip() else p.session_id) if by == "subject" else p.session_id
            for p in pairs]
    groups = [np.flatnonzero(np.array(keys) == k) for k in dict.fromkeys(keys)]
    W = _boot_weights(groups, ns, n_boot, seed)
    point = _metrics({k: v.sum(axis=1) for k, v in C.items()})        # each: (nc,)
    boot = _metrics({k: v @ W.T for k, v in C.items()})               # each: (nc, n_boot)
    per_s = _metrics(C)                                               # each: (nc, ns)

    summary = [{"label": c.label, "source": c.source,
                **{m: {"value": float(point[m][ci]), "ci": _ci(boot[m][ci])} for m in METRICS}}
               for ci, c in enumerate(cands)]
    compare = []
    for ci in range(1, nc):
        d = {m: {"value": float(point[m][ci] - point[m][0]), "ci": _ci(boot[m][ci] - boot[m][0])} for m in METRICS}
        f1s = per_s["f1"][ci] - per_s["f1"][0]
        why = []
        if not d["f1"]["ci"][0] > 0:
            why.append("F1 gain not clear of 0 (95 % interval {:+.3f} to {:+.3f})".format(*d["f1"]["ci"]))
        if d["ppv"]["value"] < -max_ppv_drop:
            why.append(f"PPV drops {-d['ppv']['value']:.3f} (> {max_ppv_drop})")
        if np.isfinite(d["ibi_shared_ms"]["value"]) and d["ibi_shared_ms"]["value"] > max_ibi_rise_ms:
            why.append(f"shared interval error +{d['ibi_shared_ms']['value']:.2f} ms (> {max_ibi_rise_ms})")
        compare.append({"label": cands[ci].label, "delta": d, "wins": int((f1s > 1e-9).sum()),
                        "losses": int((f1s < -1e-9).sum()), "replace": not why, "reasons": why})

    rows = []
    for si, p in enumerate(pairs):
        for ci, c in enumerate(cands):
            rows.append({"session": p.session_id, "subject": p.subject, "config": c.label,
                         **{m: float(per_s[m][ci, si]) for m in METRICS},
                         **{k: float(C[k][ci, si]) for k in COUNTS}})
    per_session = pd.DataFrame(rows)

    res = {"site": site, "split": split, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "git": _git_rev(root),
           "data": str(Path(data).resolve()), "sessions": [p.session_id for p in pairs], "subjects": subjects,
           "bootstrap_by": by, "n_boot": n_boot,
           "rule": {"max_ppv_drop": max_ppv_drop, "max_ibi_rise_ms": max_ibi_rise_ms},
           "n_shared_intervals": int(C["n_shared"][0].sum()), "summary": summary, "compare": compare,
           "configs": {c.label: {"source": c.source, "layer1": c.cfg1.to_dict(), "layer2": c.cfg2.to_dict()}
                       for c in cands}}
    out = Path(out or f"runs/score/{site}-{time.strftime('%Y%m%d-%H%M%S')}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(json.dumps(res, indent=2, default=list))
    per_session.to_csv(out / "per_session.csv", index=False)
    res["per_session"], res["out"] = per_session, out
    return res
