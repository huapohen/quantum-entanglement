#!/usr/bin/env python3
"""Fail closed unless two independently built distribution sets are byte-identical."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_FILE_COUNT = 8
_MAX_NAME_BYTES = 512
_DISTRIBUTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}(?:\.tar\.gz|\.whl)$")


class ReproducibilityVerificationError(ValueError):
    """A fixed-code comparison failure safe for release logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ReproducibilityVerificationError(code)


def _directory_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        status = path.lstat()
    except OSError:
        _fail("distribution_directory_invalid")
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        _fail("distribution_directory_invalid")
    return status.st_dev, status.st_ino, status.st_mtime_ns, status.st_ctime_ns


def _read_regular(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _fail("distribution_unreadable")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail("distribution_not_regular")
    if before.st_size > _MAX_FILE_BYTES:
        _fail("distribution_too_large")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("distribution_unreadable")
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            _fail("distribution_not_regular")
        if (opened_before.st_dev, opened_before.st_ino) != (before.st_dev, before.st_ino):
            _fail("distribution_changed")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_FILE_BYTES:
                _fail("distribution_too_large")
        opened_after = os.fstat(descriptor)
    except OSError:
        _fail("distribution_unreadable")
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
        _fail("distribution_changed")
    try:
        after = path.lstat()
    except OSError:
        _fail("distribution_changed")
    final_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if final_identity != after_identity:
        _fail("distribution_changed")
    return b"".join(chunks)


def _load_distribution_set(directory: Path) -> Mapping[str, bytes]:
    identity_before = _directory_identity(directory)
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        _fail("distribution_directory_invalid")
    if not entries or len(entries) > _MAX_FILE_COUNT:
        _fail("distribution_set_invalid")

    files = {}
    total_bytes = 0
    for entry in entries:
        name = entry.name
        if (
            not name.isascii()
            or len(name.encode("ascii")) > _MAX_NAME_BYTES
            or _DISTRIBUTION_NAME_PATTERN.fullmatch(name) is None
        ):
            _fail("distribution_name_invalid")
        if name in files:
            _fail("distribution_set_invalid")
        body = _read_regular(entry)
        total_bytes += len(body)
        if total_bytes > _MAX_TOTAL_BYTES:
            _fail("distribution_set_too_large")
        files[name] = body

    wheels = [name for name in files if name.endswith(".whl")]
    sdists = [name for name in files if name.endswith(".tar.gz")]
    if len(files) != 2 or len(wheels) != 1 or len(sdists) != 1:
        _fail("distribution_set_invalid")
    if _directory_identity(directory) != identity_before:
        _fail("distribution_set_changed")
    return files


def verify_reproducible_distributions(reference: Path, candidate: Path) -> dict[str, object]:
    """Verify exact filenames and bytes across two distinct distribution directories."""

    reference_identity = _directory_identity(reference)
    candidate_identity = _directory_identity(candidate)
    if reference_identity[:2] == candidate_identity[:2]:
        _fail("distribution_directories_not_independent")
    reference_files = _load_distribution_set(reference)
    candidate_files = _load_distribution_set(candidate)
    if reference_files.keys() != candidate_files.keys():
        _fail("distribution_set_mismatch")

    artifacts = []
    for name in sorted(reference_files):
        expected = reference_files[name]
        actual = candidate_files[name]
        if len(expected) != len(actual) or expected != actual:
            _fail("distribution_bytes_mismatch")
        artifacts.append(
            {
                "byteSize": len(expected),
                "filename": name,
                "sha256": hashlib.sha256(expected).hexdigest(),
            }
        )
    return {"artifacts": artifacts, "byteIdentical": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare two independently built wheel/sdist sets byte for byte."
    )
    parser.add_argument("--reference-directory", required=True)
    parser.add_argument("--candidate-directory", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_reproducible_distributions(
            Path(args.reference_directory), Path(args.candidate_directory)
        )
    except ReproducibilityVerificationError as exc:
        print(f"verify_reproducible_distributions: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
