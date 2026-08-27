from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_native_im_zero_network_gate_passes_in_fresh_process() -> None:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = "src"
    completed = subprocess.run(
        [sys.executable, "scripts/verify_native_im_zero_network.py"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "native IM P0 zero-network gate verified"


def test_native_im_zero_network_gate_has_no_argument_driven_bypass() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/verify_native_im_zero_network.py", "--allow-network"],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "verified" not in completed.stdout
