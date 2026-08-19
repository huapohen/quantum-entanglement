#!/usr/bin/env python3
# ruff: noqa: UP006, UP035, UP045
"""Run the local release baseline and emit a redacted canonical JSON record."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple, cast

_FORMAT = "quantum-entanglement.release-evidence"
_SCHEMA_VERSION = 1
_DEFAULT_TIMEOUT_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 10
_GIT_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PASSED = "passed"
_FAILED = "failed"
_TIMED_OUT = "timed_out"
_ERROR = "error"
_ALLOWED_CHILD_ENVIRONMENT = (
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)


@dataclass(frozen=True)
class Gate:
    """One fixed argv-based gate with a redacted, portable evidence command."""

    name: str
    argv: Tuple[str, ...]
    record_executable_basename: bool = False
    include_repository_source: bool = False


@dataclass(frozen=True)
class GitSnapshot:
    """Minimal source identity captured without persisting paths or filenames."""

    commit_sha: Optional[str]
    tree_sha: Optional[str]
    dirty: Optional[bool]


def default_gates(python_executable: str = sys.executable) -> Tuple[Gate, ...]:
    """Return the exact, non-network local baseline from RELEASE_GATES.md."""

    return (
        Gate(
            name="unit-tests",
            argv=(python_executable, "-m", "unittest", "discover", "-s", "tests", "-q"),
            record_executable_basename=True,
            include_repository_source=True,
        ),
        Gate(
            name="deterministic-demo",
            argv=(python_executable, "examples/group_chat_demo.py", "--compact"),
            record_executable_basename=True,
            include_repository_source=True,
        ),
        Gate(
            name="compileall",
            argv=(
                python_executable,
                "-m",
                "compileall",
                "-q",
                "src",
                "tests",
                "scripts",
            ),
            record_executable_basename=True,
        ),
        Gate(
            name="ruff",
            argv=("ruff", "check", "src", "tests", "scripts"),
        ),
        Gate(
            name="diff-check",
            argv=("git", "diff", "--check"),
        ),
    )


def _child_environment(repository_root: Path, include_repository_source: bool) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in _ALLOWED_CHILD_ENVIRONMENT if name in os.environ
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if include_repository_source:
        environment["PYTHONPATH"] = str(repository_root / "src")
    return environment


def _git_process(
    repository_root: Path,
    arguments: Sequence[str],
) -> Optional[subprocess.CompletedProcess[bytes]]:
    try:
        return subprocess.run(
            ("git", "-C", str(repository_root), *arguments),
            check=False,
            env=_child_environment(repository_root, False),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_hash(repository_root: Path, revision: str) -> Optional[str]:
    completed = _git_process(repository_root, ("rev-parse", "--verify", revision))
    if completed is None or completed.returncode != 0:
        return None
    try:
        value = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return value if _GIT_HASH_PATTERN.fullmatch(value) is not None else None


def _git_dirty(repository_root: Path) -> Optional[bool]:
    completed = _git_process(
        repository_root,
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignore-submodules=none",
        ),
    )
    if completed is None or completed.returncode != 0:
        return None
    return bool(completed.stdout)


def capture_git_snapshot(repository_root: Path) -> GitSnapshot:
    """Capture HEAD identity and cleanliness without recording repository-local names."""

    commit_sha = _git_hash(repository_root, "HEAD^{commit}")
    tree_sha = None if commit_sha is None else _git_hash(repository_root, f"{commit_sha}^{{tree}}")
    return GitSnapshot(
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        dirty=_git_dirty(repository_root),
    )


def run_gate(
    gate: Gate,
    repository_root: Path,
    timeout_seconds: int,
    *,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> dict[str, object]:
    """Run one gate without retaining its output, environment, paths, or exceptions."""

    started_ns = monotonic_ns()
    exit_code: Optional[int]
    failure_kind: Optional[str]
    try:
        completed = subprocess.run(
            gate.argv,
            check=False,
            cwd=repository_root,
            env=_child_environment(repository_root, gate.include_repository_source),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        status = _TIMED_OUT
        exit_code = None
        failure_kind = "timeout"
    except OSError:
        status = _ERROR
        exit_code = None
        failure_kind = "execution_error"
    else:
        exit_code = completed.returncode
        if completed.returncode == 0:
            status = _PASSED
            failure_kind = None
        else:
            status = _FAILED
            failure_kind = "nonzero_exit"
    elapsed_ns = max(0, monotonic_ns() - started_ns)
    duration_milliseconds = elapsed_ns // 1_000_000
    evidence_argv = list(gate.argv)
    if gate.record_executable_basename:
        evidence_argv[0] = Path(evidence_argv[0]).name
    return {
        "argv": evidence_argv,
        "durationMilliseconds": duration_milliseconds,
        "exitCode": exit_code,
        "failureKind": failure_kind,
        "name": gate.name,
        "repositorySourceImport": gate.include_repository_source,
        "status": status,
    }


def _combined_dirty(before: Optional[bool], after: Optional[bool]) -> Optional[bool]:
    if before is True or after is True:
        return True
    if before is False and after is False:
        return False
    return None


def _canonical_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("release evidence clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def generate_evidence(
    repository_root: Path,
    gates: Sequence[Gate],
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Run all supplied gates and return a fail-closed release evidence object."""

    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    gate_names = [gate.name for gate in gates]
    if any(not name or name.strip() != name for name in gate_names):
        raise ValueError("gate names must be non-blank canonical text")
    if len(set(gate_names)) != len(gate_names):
        raise ValueError("gate names must be unique")
    if any(not gate.argv or any(not token for token in gate.argv) for gate in gates):
        raise ValueError("gate argv must contain only non-blank tokens")

    root = repository_root.resolve(strict=True)
    before = capture_git_snapshot(root)
    gate_results = [run_gate(gate, root, timeout_seconds) for gate in gates]
    after = capture_git_snapshot(root)

    identity_available = all(
        value is not None
        for value in (
            before.commit_sha,
            before.tree_sha,
            after.commit_sha,
            after.tree_sha,
        )
    )
    identity_stable: Optional[bool]
    if identity_available:
        identity_stable = (
            before.commit_sha == after.commit_sha and before.tree_sha == after.tree_sha
        )
    else:
        identity_stable = None

    dirty = _combined_dirty(before.dirty, after.dirty)
    source_clean = before.dirty is False and after.dirty is False
    gate_count = len(gate_results)
    passed_count = sum(result["status"] == _PASSED for result in gate_results)
    failed_count = sum(result["status"] == _FAILED for result in gate_results)
    timed_out_count = sum(result["status"] == _TIMED_OUT for result in gate_results)
    error_count = sum(result["status"] == _ERROR for result in gate_results)
    all_gates_passed = gate_count > 0 and passed_count == gate_count

    reason_codes: list[str] = []
    if not identity_available:
        reason_codes.append("source_identity_unavailable")
    if before.dirty is None or after.dirty is None:
        reason_codes.append("source_cleanliness_unavailable")
    if before.dirty is True:
        reason_codes.append("source_dirty_before_gates")
    if after.dirty is True:
        reason_codes.append("source_dirty_after_gates")
    if identity_stable is False:
        reason_codes.append("source_identity_changed")
    if gate_count == 0:
        reason_codes.append("no_gates_executed")
    if failed_count:
        reason_codes.append("gate_failed")
    if timed_out_count:
        reason_codes.append("gate_timed_out")
    if error_count:
        reason_codes.append("gate_execution_error")

    releasable = (
        identity_available and identity_stable is True and source_clean and all_gates_passed
    )
    return {
        "format": _FORMAT,
        "gates": gate_results,
        "generatedAt": _canonical_utc(clock()),
        "runtime": {
            "machineArchitecture": platform.machine(),
            "operatingSystem": platform.system(),
            "operatingSystemRelease": platform.release(),
            "pythonImplementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
            "sqliteVersion": sqlite3.sqlite_version,
        },
        "schemaVersion": _SCHEMA_VERSION,
        "source": {
            "commitSha": before.commit_sha,
            "commitShaAfterGates": after.commit_sha,
            "dirty": dirty,
            "dirtyAfterGates": after.dirty,
            "dirtyBeforeGates": before.dirty,
            "identityStable": identity_stable,
            "treeSha": before.tree_sha,
            "treeShaAfterGates": after.tree_sha,
        },
        "summary": {
            "allGatesPassed": all_gates_passed,
            "errorCount": error_count,
            "failedCount": failed_count,
            "gateCount": gate_count,
            "passedCount": passed_count,
            "reasonCodes": reason_codes,
            "releasable": releasable,
            "sourceClean": source_clean,
            "timedOutCount": timed_out_count,
        },
    }


def canonical_json(evidence: Mapping[str, object]) -> str:
    """Serialize evidence with stable key order and no insignificant whitespace."""

    return (
        json.dumps(
            evidence,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run local release gates and emit redacted canonical JSON on stdout."
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help="per-gate timeout; defaults to %(default)s seconds",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        evidence = generate_evidence(
            repository_root,
            default_gates(),
            timeout_seconds=arguments.timeout_seconds,
        )
    except ValueError as exc:
        _argument_parser().error(str(exc))
    sys.stdout.write(canonical_json(evidence))
    summary = cast(Mapping[str, object], evidence["summary"])
    return 0 if summary["releasable"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
