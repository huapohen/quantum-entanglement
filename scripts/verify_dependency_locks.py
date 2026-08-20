#!/usr/bin/env python3
"""Strictly verify the repository's pinned and hashed Python toolchain locks."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

_FORMAT = "quantum-entanglement.dependency-locks"
_SCHEMA_VERSION = 1
_POLICY_PATH = Path("requirements/lock-policy.json")
_MAX_POLICY_BYTES = 64 * 1024
_MAX_INPUT_BYTES = 64 * 1024
_MAX_LOCK_BYTES = 2 * 1024 * 1024
_MAX_PACKAGES = 256
_MAX_HASHES_PER_PACKAGE = 512
_MAX_LINE_BYTES = 512
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.!+_-]{0,126}[A-Za-z0-9])?$")
_PIN_PATTERN = re.compile(
    r"^([a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?)"
    r"==([A-Za-z0-9](?:[A-Za-z0-9.!+_-]{0,126}[A-Za-z0-9])?)$"
)
_LOCK_HEADER_PATTERN = re.compile(
    r"^([a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?)"
    r"==([A-Za-z0-9](?:[A-Za-z0-9.!+_-]{0,126}[A-Za-z0-9])?) \\$$"
)
_LOCK_HASH_PATTERN = re.compile(r"^    --hash=sha256:([0-9a-f]{64})( \\)?$")
_EXPECTED_GENERATOR = {"name": "uv", "version": "0.9.27"}
_EXPECTED_CUTOFF = "2026-08-20T00:00:00Z"
_EXPECTED_TARGETS = (
    (
        "build",
        "3.12",
        "x86_64-unknown-linux-gnu",
        "requirements/build.in",
        "requirements/build-py312.lock",
    ),
    (
        "dev",
        "3.9",
        "x86_64-unknown-linux-gnu",
        "requirements/dev.in",
        "requirements/dev-py39.lock",
    ),
    (
        "dev",
        "3.12",
        "x86_64-unknown-linux-gnu",
        "requirements/dev.in",
        "requirements/dev-py312.lock",
    ),
    (
        "release",
        "3.12",
        "x86_64-unknown-linux-gnu",
        "requirements/release.in",
        "requirements/release-py312.lock",
    ),
)
_TOP_LEVEL_KEYS = frozenset(
    {"format", "generatedBy", "locks", "resolutionCutoff", "schemaVersion"}
)
_TARGET_KEYS = frozenset(
    {
        "input",
        "inputSha256",
        "lock",
        "lockSha256",
        "platform",
        "pythonVersion",
        "scope",
    }
)


class DependencyLockError(ValueError):
    """A fixed-code dependency-lock failure that is safe to emit in CI logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    sha256: tuple[str, ...]


@dataclass(frozen=True)
class LockTarget:
    scope: str
    python_version: str
    platform: str
    input_path: str
    input_sha256: str
    lock_path: str
    lock_sha256: str
    roots: tuple[LockedPackage, ...]
    packages: tuple[LockedPackage, ...]


def _fail(code: str) -> NoReturn:
    raise DependencyLockError(code)


def _read_regular(path: Path, limit: int, code: str) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _fail(code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail(code)
    if before.st_size > limit:
        _fail(code)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(code)
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            _fail(code)
        if (opened_before.st_dev, opened_before.st_ino) != (before.st_dev, before.st_ino):
            _fail(code)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                _fail(code)
        opened_after = os.fstat(descriptor)
    except OSError:
        _fail(code)
    finally:
        os.close(descriptor)
    identity_before = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
        opened_before.st_ctime_ns,
    )
    identity_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_ctime_ns,
    )
    if identity_before != identity_after or size != opened_after.st_size:
        _fail(code)
    try:
        after = path.lstat()
    except OSError:
        _fail(code)
    if (after.st_dev, after.st_ino) != (opened_after.st_dev, opened_after.st_ino):
        _fail(code)
    return b"".join(chunks)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("ascii")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("lock_policy_invalid")
        result[key] = value
    return result


def _load_policy(value: bytes) -> dict[str, Any]:
    try:
        policy = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except (DependencyLockError, json.JSONDecodeError, UnicodeDecodeError):
        _fail("lock_policy_invalid")
    if type(policy) is not dict or _canonical_json(policy) != value:
        _fail("lock_policy_noncanonical")
    if frozenset(policy) != _TOP_LEVEL_KEYS:
        _fail("lock_policy_invalid")
    if (
        policy["format"] != _FORMAT
        or type(policy["schemaVersion"]) is not int
        or policy["schemaVersion"] != _SCHEMA_VERSION
        or policy["generatedBy"] != _EXPECTED_GENERATOR
        or policy["resolutionCutoff"] != _EXPECTED_CUTOFF
        or type(policy["locks"]) is not list
    ):
        _fail("lock_policy_invalid")
    return policy


def _decode_lines(value: bytes, code: str) -> list[str]:
    if not value or not value.endswith(b"\n") or b"\r" in value or b"\x00" in value:
        _fail(code)
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        _fail(code)
    lines = text.splitlines()
    if any(len(line.encode("ascii")) > _MAX_LINE_BYTES for line in lines):
        _fail(code)
    return lines


def _parse_input(value: bytes) -> tuple[LockedPackage, ...]:
    packages: list[LockedPackage] = []
    for line in _decode_lines(value, "lock_input_invalid"):
        if not line:
            continue
        match = _PIN_PATTERN.fullmatch(line)
        if match is None:
            _fail("lock_input_invalid")
        name, version = match.groups()
        packages.append(LockedPackage(name=name, version=version, sha256=()))
    if not packages or len(packages) > _MAX_PACKAGES:
        _fail("lock_input_invalid")
    names = [package.name for package in packages]
    if names != sorted(names) or len(names) != len(set(names)):
        _fail("lock_input_invalid")
    return tuple(packages)


def _parse_lock(value: bytes) -> tuple[LockedPackage, ...]:
    packages: list[LockedPackage] = []
    only_binary_seen = False
    current_name: str | None = None
    current_version: str | None = None
    current_hashes: list[str] = []

    for line in _decode_lines(value, "lock_file_invalid"):
        if not line or line.lstrip().startswith("#"):
            continue
        if line == "--only-binary :all:":
            if only_binary_seen or packages or current_name is not None:
                _fail("lock_file_invalid")
            only_binary_seen = True
            continue
        if current_name is None:
            match = _LOCK_HEADER_PATTERN.fullmatch(line)
            if match is None:
                _fail("lock_file_invalid")
            current_name, current_version = match.groups()
            current_hashes = []
            continue
        match = _LOCK_HASH_PATTERN.fullmatch(line)
        if match is None:
            _fail("lock_file_invalid")
        digest, continuation = match.groups()
        current_hashes.append(digest)
        if len(current_hashes) > _MAX_HASHES_PER_PACKAGE:
            _fail("lock_file_invalid")
        if continuation:
            continue
        if current_version is None:
            _fail("lock_file_invalid")
        if current_hashes != sorted(current_hashes) or len(current_hashes) != len(
            set(current_hashes)
        ):
            _fail("lock_file_invalid")
        packages.append(
            LockedPackage(
                name=current_name,
                version=current_version,
                sha256=tuple(current_hashes),
            )
        )
        current_name = None
        current_version = None
        current_hashes = []

    if not only_binary_seen or current_name is not None or not packages:
        _fail("lock_file_invalid")
    if len(packages) > _MAX_PACKAGES:
        _fail("lock_file_invalid")
    names = [package.name for package in packages]
    if names != sorted(names) or len(names) != len(set(names)):
        _fail("lock_file_invalid")
    return tuple(packages)


def _parse_pyproject_list(value: bytes, section: str, key: str) -> list[str]:
    lines = _decode_lines(value, "pyproject_lock_mismatch")
    in_section = False
    candidates: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line == f"[{section}]"
            continue
        if not in_section or not line.startswith(f"{key} ="):
            continue
        candidates.append(line.split("=", 1)[1].strip())
    if len(candidates) != 1:
        _fail("pyproject_lock_mismatch")
    try:
        parsed = ast.literal_eval(candidates[0])
    except (SyntaxError, ValueError):
        _fail("pyproject_lock_mismatch")
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        _fail("pyproject_lock_mismatch")
    return parsed


def _pins_from_strings(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        match = _PIN_PATTERN.fullmatch(value)
        if match is None:
            _fail("pyproject_lock_mismatch")
        name, version = match.groups()
        if name in result:
            _fail("pyproject_lock_mismatch")
        result[name] = version
    return result


def _validate_pyproject(repository_root: Path, targets: Sequence[LockTarget]) -> None:
    pyproject = _read_regular(
        repository_root / "pyproject.toml", _MAX_INPUT_BYTES, "pyproject_lock_mismatch"
    )
    build_requires = _pins_from_strings(
        _parse_pyproject_list(pyproject, "build-system", "requires")
    )
    dev_requires = _pins_from_strings(
        _parse_pyproject_list(pyproject, "project.optional-dependencies", "dev")
    )
    roots_by_scope = {
        target.scope: {package.name: package.version for package in target.roots}
        for target in targets
    }
    if not build_requires or any(
        roots_by_scope["build"].get(name) != version for name, version in build_requires.items()
    ):
        _fail("pyproject_lock_mismatch")
    if dev_requires != roots_by_scope["dev"]:
        _fail("pyproject_lock_mismatch")
    if not set(roots_by_scope["build"].items()).issubset(set(roots_by_scope["release"].items())):
        _fail("pyproject_lock_mismatch")
    if "cyclonedx-bom" not in roots_by_scope["release"]:
        _fail("pyproject_lock_mismatch")


def verify_dependency_locks(repository_root: Path) -> tuple[LockTarget, ...]:
    """Verify the canonical policy, every input/lock digest, pins, hashes, and source roots."""

    try:
        root_stat = repository_root.lstat()
    except OSError:
        _fail("repository_root_invalid")
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _fail("repository_root_invalid")

    policy_bytes = _read_regular(
        repository_root / _POLICY_PATH, _MAX_POLICY_BYTES, "lock_policy_invalid"
    )
    policy = _load_policy(policy_bytes)
    records = policy["locks"]
    if len(records) != len(_EXPECTED_TARGETS):
        _fail("lock_inventory_invalid")

    targets: list[LockTarget] = []
    for record, expected in zip(records, _EXPECTED_TARGETS):
        if type(record) is not dict or frozenset(record) != _TARGET_KEYS:
            _fail("lock_inventory_invalid")
        scope, python_version, platform_name, input_path, lock_path = expected
        expected_identity = {
            "scope": scope,
            "pythonVersion": python_version,
            "platform": platform_name,
            "input": input_path,
            "lock": lock_path,
        }
        if any(record.get(key) != value for key, value in expected_identity.items()):
            _fail("lock_inventory_invalid")
        input_digest = record.get("inputSha256")
        lock_digest = record.get("lockSha256")
        if (
            type(input_digest) is not str
            or _HASH_PATTERN.fullmatch(input_digest) is None
            or type(lock_digest) is not str
            or _HASH_PATTERN.fullmatch(lock_digest) is None
        ):
            _fail("lock_inventory_invalid")
        input_bytes = _read_regular(
            repository_root / input_path, _MAX_INPUT_BYTES, "lock_input_invalid"
        )
        lock_bytes = _read_regular(
            repository_root / lock_path, _MAX_LOCK_BYTES, "lock_file_invalid"
        )
        if _sha256(input_bytes) != input_digest or _sha256(lock_bytes) != lock_digest:
            _fail("lock_digest_mismatch")
        roots = _parse_input(input_bytes)
        packages = _parse_lock(lock_bytes)
        locked = {package.name: package.version for package in packages}
        if any(locked.get(root.name) != root.version for root in roots):
            _fail("lock_root_mismatch")
        targets.append(
            LockTarget(
                scope=scope,
                python_version=python_version,
                platform=platform_name,
                input_path=input_path,
                input_sha256=input_digest,
                lock_path=lock_path,
                lock_sha256=lock_digest,
                roots=roots,
                packages=packages,
            )
        )

    _validate_pyproject(repository_root, targets)
    return tuple(targets)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing pyproject.toml and requirements/",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        targets = verify_dependency_locks(args.repository_root)
    except DependencyLockError as exc:
        print(f"dependency lock verification failed: {exc.code}", file=sys.stderr)
        return 1
    package_records = sum(len(target.packages) for target in targets)
    print(
        json.dumps(
            {"lockTargets": len(targets), "packageRecords": package_records, "verified": True},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
