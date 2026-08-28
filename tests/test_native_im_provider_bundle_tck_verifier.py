from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = REPOSITORY_ROOT / "scripts" / "verify_native_im_provider_bundle_tck_v1.py"
EXPECTED_DIGEST = "a14ef986ec7ffcfa2ed59ab0fa748d3e2181e29c067e09ffef66325b28a50368"


def _run(seed: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": "src:.",
        }
    )
    return subprocess.run(
        [sys.executable, str(VERIFIER), *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_provider_bundle_tck_verifier_is_cross_process_stable_and_read_only() -> None:
    before = VERIFIER.read_bytes()
    first = _run("1")
    second = _run("987654321")

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert first.stdout.strip() == (f"native IM provider bundle TCK verified: {EXPECTED_DIGEST}")
    assert VERIFIER.read_bytes() == before

    rejected = _run("1", "--write")
    assert rejected.returncode == 2
    assert "usage:" in rejected.stderr
    assert VERIFIER.read_bytes() == before
