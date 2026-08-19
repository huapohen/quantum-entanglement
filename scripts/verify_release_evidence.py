#!/usr/bin/env python3
# ruff: noqa: UP006, UP035, UP045
"""Strictly verify one canonical, releasable local evidence document."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, NoReturn, Optional, Sequence, Tuple, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.generate_release_evidence import (  # noqa: E402
    Gate,
    canonical_json,
    capture_git_snapshot,
    default_gates,
    gate_evidence_argv,
)

_FORMAT = "quantum-entanglement.release-evidence"
_SCHEMA_VERSION = 1
_MAX_EVIDENCE_BYTES = 1024 * 1024
_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_TOP_LEVEL_KEYS = frozenset(
    {"format", "gates", "generatedAt", "runtime", "schemaVersion", "source", "summary"}
)
_RUNTIME_KEYS = frozenset(
    {
        "machineArchitecture",
        "operatingSystem",
        "operatingSystemRelease",
        "pythonImplementation",
        "pythonVersion",
        "sqliteVersion",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "commitSha",
        "commitShaAfterGates",
        "dirty",
        "dirtyAfterGates",
        "dirtyBeforeGates",
        "identityStable",
        "treeSha",
        "treeShaAfterGates",
    }
)
_GATE_KEYS = frozenset(
    {
        "argv",
        "durationMilliseconds",
        "exitCode",
        "failureKind",
        "name",
        "repositorySourceImport",
        "status",
    }
)
_SUMMARY_KEYS = frozenset(
    {
        "allGatesPassed",
        "errorCount",
        "failedCount",
        "gateCount",
        "passedCount",
        "reasonCodes",
        "releasable",
        "sourceClean",
        "timedOutCount",
    }
)


class EvidenceVerificationError(ValueError):
    """A fixed-code verification failure safe to emit without source data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise EvidenceVerificationError(code)


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _fail("non_finite_json_value")


def _read_regular_file(path: Path) -> bytes:
    try:
        path_before = path.lstat()
    except OSError:
        _fail("evidence_unreadable")
    if stat.S_ISLNK(path_before.st_mode):
        _fail("evidence_symlink")
    if not stat.S_ISREG(path_before.st_mode):
        _fail("evidence_not_regular")
    if path_before.st_size > _MAX_EVIDENCE_BYTES:
        _fail("evidence_too_large")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("evidence_unreadable")
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            _fail("evidence_not_regular")
        if (opened_before.st_dev, opened_before.st_ino) != (
            path_before.st_dev,
            path_before.st_ino,
        ):
            _fail("evidence_path_changed")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_EVIDENCE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_EVIDENCE_BYTES:
                _fail("evidence_too_large")
        opened_after = os.fstat(descriptor)
    except OSError:
        _fail("evidence_unreadable")
    finally:
        os.close(descriptor)

    before_identity = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
        opened_before.st_ctime_ns,
    )
    after_identity = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_ctime_ns,
    )
    if before_identity != after_identity or size != opened_after.st_size:
        _fail("evidence_changed_during_read")
    try:
        path_after = path.lstat()
    except OSError:
        _fail("evidence_path_changed")
    if (path_after.st_dev, path_after.st_ino) != (
        opened_after.st_dev,
        opened_after.st_ino,
    ):
        _fail("evidence_path_changed")
    return b"".join(chunks)


def load_canonical_evidence(path: Path) -> Dict[str, object]:
    """Read one bounded regular file, reject ambiguous JSON, and prove canonical bytes."""

    raw = _read_regular_file(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("evidence_not_utf8")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EvidenceVerificationError:
        raise
    except (RecursionError, TypeError, ValueError):
        _fail("evidence_invalid_json")
    if type(decoded) is not dict:
        _fail("evidence_not_object")
    evidence = cast(Dict[str, object], decoded)
    try:
        encoded = canonical_json(evidence).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError):
        _fail("evidence_not_canonical")
    if encoded != raw:
        _fail("evidence_not_canonical")
    return evidence


def _object(value: object, keys: frozenset[str], code: str) -> Dict[str, object]:
    if type(value) is not dict:
        _fail(code)
    result = cast(Dict[str, object], value)
    if frozenset(result) != keys:
        _fail(code)
    return result


def _text(value: object, code: str) -> str:
    if type(value) is not str:
        _fail(code)
    result = value
    if not result or len(result) > 512 or any(item in result for item in ("\x00", "\r", "\n")):
        _fail(code)
    return result


def _hash(value: object, code: str) -> str:
    result = _text(value, code)
    if _HASH_PATTERN.fullmatch(result) is None:
        _fail(code)
    return result


def _nonnegative_integer(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        _fail(code)
    return value


def _validate_timestamp(value: object) -> None:
    timestamp = _text(value, "generated_at_invalid")
    if _UTC_PATTERN.fullmatch(timestamp) is None:
        _fail("generated_at_invalid")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        _fail("generated_at_invalid")
    canonical = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if canonical != timestamp:
        _fail("generated_at_invalid")


def verify_releasable_evidence(
    evidence: Mapping[str, object],
    *,
    expected_commit_sha: str,
    expected_tree_sha: str,
    expected_gates: Optional[Sequence[Gate]] = None,
) -> None:
    """Strictly validate schema v1 and every condition behind ``releasable: true``."""

    root = _object(dict(evidence), _TOP_LEVEL_KEYS, "top_level_shape_invalid")
    if root["format"] != _FORMAT or type(root["format"]) is not str:
        _fail("format_invalid")
    if type(root["schemaVersion"]) is not int or root["schemaVersion"] != _SCHEMA_VERSION:
        _fail("schema_version_invalid")
    _validate_timestamp(root["generatedAt"])

    runtime = _object(root["runtime"], _RUNTIME_KEYS, "runtime_shape_invalid")
    for value in runtime.values():
        _text(value, "runtime_value_invalid")

    expected_commit = _hash(expected_commit_sha, "expected_commit_invalid")
    expected_tree = _hash(expected_tree_sha, "expected_tree_invalid")
    source = _object(root["source"], _SOURCE_KEYS, "source_shape_invalid")
    if _hash(source["commitSha"], "source_commit_invalid") != expected_commit:
        _fail("source_commit_mismatch")
    if _hash(source["commitShaAfterGates"], "source_commit_invalid") != expected_commit:
        _fail("source_commit_mismatch")
    if _hash(source["treeSha"], "source_tree_invalid") != expected_tree:
        _fail("source_tree_mismatch")
    if _hash(source["treeShaAfterGates"], "source_tree_invalid") != expected_tree:
        _fail("source_tree_mismatch")
    for key in ("dirty", "dirtyAfterGates", "dirtyBeforeGates"):
        if source[key] is not False:
            _fail("source_not_clean")
    if source["identityStable"] is not True:
        _fail("source_identity_unstable")

    gates = default_gates() if expected_gates is None else tuple(expected_gates)
    if not gates:
        _fail("gate_set_invalid")
    raw_gates = root["gates"]
    if type(raw_gates) is not list or len(cast(list[object], raw_gates)) != len(gates):
        _fail("gate_set_invalid")
    for raw_gate, expected_gate in zip(cast(list[object], raw_gates), gates):
        gate = _object(raw_gate, _GATE_KEYS, "gate_shape_invalid")
        if gate["name"] != expected_gate.name or type(gate["name"]) is not str:
            _fail("gate_name_invalid")
        raw_argv = gate["argv"]
        if type(raw_argv) is not list or raw_argv != gate_evidence_argv(expected_gate):
            _fail("gate_argv_invalid")
        if gate["repositorySourceImport"] is not expected_gate.include_repository_source:
            _fail("gate_source_import_invalid")
        _nonnegative_integer(gate["durationMilliseconds"], "gate_duration_invalid")
        if type(gate["exitCode"]) is not int or gate["exitCode"] != 0:
            _fail("gate_exit_invalid")
        if gate["failureKind"] is not None or gate["status"] != "passed":
            _fail("gate_result_invalid")

    gate_count = len(gates)
    summary = _object(root["summary"], _SUMMARY_KEYS, "summary_shape_invalid")
    if summary["allGatesPassed"] is not True:
        _fail("summary_not_passed")
    if summary["sourceClean"] is not True or summary["releasable"] is not True:
        _fail("summary_not_releasable")
    if type(summary["reasonCodes"]) is not list or summary["reasonCodes"] != []:
        _fail("summary_reasons_invalid")
    expected_counts = {
        "errorCount": 0,
        "failedCount": 0,
        "gateCount": gate_count,
        "passedCount": gate_count,
        "timedOutCount": 0,
    }
    for key, expected in expected_counts.items():
        if type(summary[key]) is not int or summary[key] != expected:
            _fail("summary_counts_invalid")


def verify_file_against_repository(
    evidence_path: Path,
    repository_root: Path,
    *,
    expected_commit_sha: Optional[str] = None,
    expected_gates: Optional[Sequence[Gate]] = None,
) -> None:
    """Verify canonical evidence against the current clean Git checkout."""

    try:
        root = repository_root.resolve(strict=True)
    except OSError:
        _fail("repository_unavailable")
    try:
        resolved_evidence_path = evidence_path.resolve(strict=True)
    except OSError:
        _fail("evidence_unreadable")
    try:
        resolved_evidence_path.relative_to(root)
    except ValueError:
        pass
    else:
        _fail("evidence_inside_repository")
    snapshot = capture_git_snapshot(root)
    if snapshot.commit_sha is None or snapshot.tree_sha is None:
        _fail("repository_identity_unavailable")
    if snapshot.dirty is not False:
        _fail("repository_not_clean")
    if expected_commit_sha is not None:
        expected = _hash(expected_commit_sha, "expected_commit_invalid")
        if snapshot.commit_sha != expected:
            _fail("repository_commit_mismatch")
    evidence = load_canonical_evidence(evidence_path)
    verify_releasable_evidence(
        evidence,
        expected_commit_sha=snapshot.commit_sha,
        expected_tree_sha=snapshot.tree_sha,
        expected_gates=expected_gates,
    )
    snapshot_after = capture_git_snapshot(root)
    if snapshot_after.commit_sha is None or snapshot_after.tree_sha is None:
        _fail("repository_identity_unavailable")
    if snapshot_after.dirty is not False:
        _fail("repository_changed_during_verification")
    if (
        snapshot_after.commit_sha != snapshot.commit_sha
        or snapshot_after.tree_sha != snapshot.tree_sha
    ):
        _fail("repository_changed_during_verification")
    if expected_commit_sha is not None and snapshot_after.commit_sha != expected_commit_sha:
        _fail("repository_commit_mismatch")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify canonical release evidence against a clean Git checkout."
    )
    parser.add_argument("evidence", type=Path, help="canonical JSON evidence file")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="checkout to bind; defaults to this script's repository",
    )
    parser.add_argument(
        "--expected-commit",
        help="optional full commit SHA required in addition to the checkout identity",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    expected_gates: Optional[Sequence[Gate]] = None,
) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        verify_file_against_repository(
            arguments.evidence,
            arguments.repository_root,
            expected_commit_sha=arguments.expected_commit,
            expected_gates=expected_gates,
        )
    except EvidenceVerificationError as exc:
        print(f"release evidence verification failed: {exc.code}", file=sys.stderr)
        return 1
    print("release evidence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
