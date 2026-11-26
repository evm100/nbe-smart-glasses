#!/usr/bin/env python3
"""Check the firmware's record decoder (main/om_record.c) against omblepy's (deviceSpecific/hem-7150t.py).

Builds om_record.c for this PC with cc, feeds both the same random 16-byte records, compares every field.
    python3 host_test/check_records.py [N]
"""
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN = HERE.parent / "main"
OMBLEPY = HERE.parents[3] / "auto-cuff" / "omblepy"

HARNESS = r'''
#include <stdio.h>
#include "omron_ble.h"
int main(void) {
    char line[64];
    while (fgets(line, sizeof line, stdin)) {
        unsigned char r[16]; unsigned b;
        for (int i = 0; i < 16; i++) { sscanf(line + 2 * i, "%2x", &b); r[i] = b; }
        om_record_t o;
        if (!om_parse_record(r, &o)) { puts("-"); continue; }
        char t[24]; om_record_time(&o, t, sizeof t);
        printf("%s %d %d %d %d %d\n", t, o.sys, o.dia, o.bpm, o.mov, o.ihb);
    }
}
'''


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    sys.path.insert(0, str(OMBLEPY))
    sys.path.insert(0, str(OMBLEPY / "deviceSpecific"))
    import importlib
    drv = importlib.import_module("hem-7150t").deviceSpecificDriver()

    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "rec")
        src = os.path.join(d, "h.c")
        Path(src).write_text(HARNESS)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", f"-I{MAIN}", f"-I{HERE / 'stub'}", "-o", exe, src,
                        str(MAIN / "om_record.c")], check=True)
        rng = random.Random(1)
        recs = [bytes(rng.randrange(256) for _ in range(16)) for _ in range(n)]
        recs.append(b"\xff" * 16)
        out = subprocess.run([exe], input="".join(r.hex() + "\n" for r in recs), capture_output=True, text=True,
                             check=True).stdout.splitlines()

    compared = mismatched = 0
    for r, c in zip(recs, out):
        if r == b"\xff" * 16:
            assert c == "-", "empty slot not skipped"
            continue
        try:
            p = drv.deviceSpecific_ParseRecordFormat(bytearray(r))
        except Exception:
            continue  # omblepy drops records whose date is invalid
        want = f"{p['datetime']:%Y-%m-%d %H:%M:%S} {p['sys']} {p['dia']} {p['bpm']} {p['mov']} {p['ihb']}"
        compared += 1
        if c != want:
            mismatched += 1
            if mismatched <= 5:
                print(f"MISMATCH {r.hex()}\n  C:      {c}\n  python: {want}")
    print(f"{compared} valid records compared, {mismatched} mismatches")
    sys.exit(1 if mismatched or compared == 0 else 0)


if __name__ == "__main__":
    main()
