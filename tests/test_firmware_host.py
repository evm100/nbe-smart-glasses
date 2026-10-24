"""Runs the ESP-IDF firmware sources on the PC against a simulated MAX30102.

Skipped when gcc isn't available. Real hardware testing: firmware/esp-idf/ppg_stream/README.md
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HT = Path(__file__).resolve().parents[1] / "firmware" / "esp-idf" / "ppg_stream" / "host_test"


@pytest.mark.skipif(shutil.which("gcc") is None or shutil.which("bash") is None, reason="needs gcc + bash")
def test_firmware_host_simulation():
    r = subprocess.run(["bash", str(HT / "run.sh")], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHON": sys.executable})
    for f in HT.glob("out_*.txt"):
        f.unlink()
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count(": OK") == 6, r.stdout
