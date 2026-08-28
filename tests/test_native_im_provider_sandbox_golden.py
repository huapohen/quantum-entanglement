from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "native_im" / "provider_sandbox" / "v1"
VERIFIER = REPOSITORY_ROOT / "scripts" / "verify_native_im_provider_sandbox_v1_golden.py"


def test_provider_sandbox_golden_verifier_is_read_only_and_exact() -> None:
    before = {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())}
    verifier_before = VERIFIER.read_bytes()
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = "src"
    completed = subprocess.run(
        [sys.executable, str(VERIFIER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == (
        "native IM provider sandbox V1 golden vectors verified: 4 vectors"
    )
    after = {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())}
    assert after == before
    assert VERIFIER.read_bytes() == verifier_before

    rejected_write = subprocess.run(
        [sys.executable, str(VERIFIER), "--write"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rejected_write.returncode == 2
    assert {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())} == before
    assert VERIFIER.read_bytes() == verifier_before
