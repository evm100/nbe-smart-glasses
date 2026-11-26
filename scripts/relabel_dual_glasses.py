"""Relabel recorded streams to the dual-glasses lineup (finger, glasses_l, glasses_r).

Rules (decided 2026-11-25):
  * HED01 recorded with the new sensor config (125 Hz)  -> glasses_l
  * GLS03 in S005's sessions                            -> glasses_r
Old-config HED01 sessions (400 / 250 Hz) stay "head".

Only session.json changes: the stream's "role" (its old one is kept in "role_orig"); file and
stream names stay as recorded. A copy of the original is written next to it as
session.json.pre-relabel (once), and the catalog row is refreshed. Re-process the listed
sessions afterwards (Process button, or `sglab process <id>`); glasses_l / glasses_r use
config/sites/glasses.json.

Run from the repo root (on the server: as the data owner, with SGLAB_ROOT set like the service):
    python scripts/relabel_dual_glasses.py            dry run: lists what would change
    python scripts/relabel_dual_glasses.py --apply
"""
import argparse
import json
import shutil

from sglab.catalog import Catalog
from sglab.settings import load_settings


def new_role(doc: dict, st: dict) -> str | None:
    dev = st.get("dev") or ""
    fs = float((st.get("header") or {}).get("fs") or st.get("fs") or 0)
    subject = (doc.get("metadata") or {}).get("subject_id") or (doc.get("subject") or {}).get("subject_id")
    if dev.startswith("HED") and st.get("role") == "head" and fs == 125.0:
        return "glasses_l"
    if dev == "GLS03" and subject == "S005" and st.get("role") == "glasses":
        return "glasses_r"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args()
    s = load_settings()
    cat = Catalog(s.db_path, s.data_root) if a.apply else None
    n = 0
    for sj in sorted(s.raw_dir.glob("*/session.json")):
        if sj.parent.name.startswith("_"):
            continue
        doc = json.loads(sj.read_text())
        changes = [(st, r) for st in doc.get("streams") or [] if (r := new_role(doc, st))]
        if not changes:
            continue
        n += 1
        print(f"{doc.get('session_id', sj.parent.name)}: "
              + ", ".join(f"{st['stream']} {st['role']} -> {r}" for st, r in changes))
        if not a.apply:
            continue
        backup = sj.with_name("session.json.pre-relabel")
        if not backup.exists():
            shutil.copy2(sj, backup)
        for st, r in changes:
            st["role_orig"], st["role"] = st["role"], r
        sj.write_text(json.dumps(doc, indent=2))
        cat.upsert_session(sj.parent)
    print(f"{n} session(s) {'relabelled' if a.apply else 'would change (dry run; add --apply)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
