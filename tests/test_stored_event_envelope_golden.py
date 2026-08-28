from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "stored_event_envelope" / "v1"
VERIFIER = REPOSITORY_ROOT / "scripts" / "verify_stored_event_envelope_v1_golden.py"
PYTHON_INTERPRETERS = tuple(
    path
    for path in (Path("/usr/bin/python3"), Path(sys.executable))
    if path.is_file()
)


@pytest.mark.parametrize("interpreter", PYTHON_INTERPRETERS, ids=lambda path: str(path))
def test_golden_verifier_is_read_only_across_supported_interpreters(
    interpreter: Path,
) -> None:
    before = {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())}
    verifier_before = VERIFIER.read_bytes()
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = "src"

    completed = subprocess.run(
        [str(interpreter), str(VERIFIER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == (
        "stored event envelope V1 golden vectors verified: 1 vector"
    )
    assert {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())} == before
    assert VERIFIER.read_bytes() == verifier_before


def test_golden_verifier_has_no_write_mode() -> None:
    before = {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())}
    verifier_before = VERIFIER.read_bytes()
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = "src"

    completed = subprocess.run(
        [sys.executable, str(VERIFIER), "--write"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert {path.name: path.read_bytes() for path in sorted(FIXTURE_ROOT.iterdir())} == before
    assert VERIFIER.read_bytes() == verifier_before
