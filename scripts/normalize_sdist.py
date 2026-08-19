#!/usr/bin/env python3
"""Rewrite one setuptools source distribution with canonical archive metadata."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_TAR_BYTES = 512 * 1024 * 1024
_MAX_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_FILE_BYTES = 256 * 1024 * 1024
_MAX_MEMBER_COUNT = 10_000
_MAX_PATH_BYTES = 1_024
_MAX_EPOCH = (1 << 32) - 1
_EPOCH_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ARCHIVE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.tar\.gz$")


class SdistNormalizationError(ValueError):
    """A fixed-code normalization failure that is safe to expose in build logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _SourceIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _CanonicalMember:
    name: str
    is_directory: bool
    executable: bool
    body: bytes


def _fail(code: str) -> NoReturn:
    raise SdistNormalizationError(code)


def _identity(value: os.stat_result) -> _SourceIdentity:
    return _SourceIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _read_regular(path: Path) -> tuple[bytes, _SourceIdentity]:
    try:
        before = path.lstat()
    except OSError:
        _fail("archive_unreadable")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail("archive_not_regular")
    if before.st_size > _MAX_ARCHIVE_BYTES:
        _fail("archive_too_large")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("archive_unreadable")
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            _fail("archive_not_regular")
        if (opened_before.st_dev, opened_before.st_ino) != (before.st_dev, before.st_ino):
            _fail("archive_changed")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_ARCHIVE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_ARCHIVE_BYTES:
                _fail("archive_too_large")
        opened_after = os.fstat(descriptor)
    except OSError:
        _fail("archive_unreadable")
    finally:
        os.close(descriptor)

    source_identity = _identity(opened_before)
    if source_identity != _identity(opened_after) or size != opened_after.st_size:
        _fail("archive_changed")
    try:
        after = path.lstat()
    except OSError:
        _fail("archive_changed")
    if source_identity != _identity(after):
        _fail("archive_changed")
    return b"".join(chunks), source_identity


def _parse_epoch(value: object) -> int:
    if type(value) is not str or _EPOCH_PATTERN.fullmatch(value) is None:
        _fail("source_date_epoch_invalid")
    epoch = int(value)
    if epoch > _MAX_EPOCH:
        _fail("source_date_epoch_invalid")
    return epoch


def _safe_member_name(value: object) -> str:
    if type(value) is not str:
        _fail("member_name_invalid")
    name = value[:-1] if value.endswith("/") else value
    if (
        not name
        or not name.isascii()
        or len(name.encode("ascii")) > _MAX_PATH_BYTES
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
    ):
        _fail("member_name_invalid")
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in path.parts) or str(path) != name:
        _fail("member_name_invalid")
    return name


def _decompress(raw_archive: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw_archive), mode="rb") as compressed:
            raw_tar = compressed.read(_MAX_TAR_BYTES + 1)
            if len(raw_tar) > _MAX_TAR_BYTES or compressed.read(1):
                _fail("archive_expansion_limit")
    except (EOFError, OSError, gzip.BadGzipFile):
        _fail("archive_invalid")
    return raw_tar


def _load_members(raw_archive: bytes, expected_root: str) -> tuple[_CanonicalMember, ...]:
    raw_tar = _decompress(raw_archive)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > _MAX_MEMBER_COUNT:
                _fail("member_count_invalid")

            canonical = []
            seen = set()
            roots = set()
            total_file_bytes = 0
            regular_files = 0
            for member in members:
                name = _safe_member_name(member.name)
                if name in seen:
                    _fail("member_duplicate")
                seen.add(name)
                roots.add(PurePosixPath(name).parts[0])

                if member.isdir():
                    if member.size != 0:
                        _fail("member_invalid")
                    canonical.append(
                        _CanonicalMember(
                            name=name,
                            is_directory=True,
                            executable=True,
                            body=b"",
                        )
                    )
                    continue
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    _fail("member_type_forbidden")
                if member.size < 0 or member.size > _MAX_MEMBER_BYTES:
                    _fail("member_size_invalid")
                total_file_bytes += member.size
                if total_file_bytes > _MAX_TOTAL_FILE_BYTES:
                    _fail("archive_expansion_limit")
                extracted = archive.extractfile(member)
                if extracted is None:
                    _fail("member_invalid")
                body = extracted.read(_MAX_MEMBER_BYTES + 1)
                if len(body) != member.size:
                    _fail("member_invalid")
                regular_files += 1
                canonical.append(
                    _CanonicalMember(
                        name=name,
                        is_directory=False,
                        executable=bool(member.mode & 0o111),
                        body=body,
                    )
                )
    except (tarfile.TarError, EOFError, OSError):
        _fail("archive_invalid")

    if roots != {expected_root} or regular_files == 0:
        _fail("archive_root_invalid")
    root_members = [
        member for member in canonical if member.name == expected_root and member.is_directory
    ]
    if len(root_members) != 1:
        _fail("archive_root_invalid")
    return tuple(sorted(canonical, key=lambda item: item.name))


def _assert_source_unchanged(path: Path, expected: _SourceIdentity) -> None:
    try:
        current = path.lstat()
    except OSError:
        _fail("archive_changed")
    if stat.S_ISLNK(current.st_mode) or _identity(current) != expected:
        _fail("archive_changed")


def _write_canonical(
    path: Path,
    members: Sequence[_CanonicalMember],
    epoch: int,
    source_identity: _SourceIdentity,
) -> bytes:
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w:",
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    for member in members:
                        metadata = tarfile.TarInfo(member.name)
                        metadata.mtime = epoch
                        metadata.uid = 0
                        metadata.gid = 0
                        metadata.uname = ""
                        metadata.gname = ""
                        metadata.pax_headers = {}
                        if member.is_directory:
                            metadata.type = tarfile.DIRTYPE
                            metadata.mode = 0o755
                            metadata.size = 0
                            archive.addfile(metadata)
                        else:
                            metadata.type = tarfile.REGTYPE
                            metadata.mode = 0o755 if member.executable else 0o644
                            metadata.size = len(member.body)
                            archive.addfile(metadata, io.BytesIO(member.body))
            raw_output.flush()
            os.fsync(raw_output.fileno())
        _assert_source_unchanged(path, source_identity)
        os.replace(temporary_path, path)
        temporary_path = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except SdistNormalizationError:
        raise
    except (OSError, tarfile.TarError, ValueError):
        _fail("archive_write_failed")
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass

    normalized, _ = _read_regular(path)
    return normalized


def normalize_sdist(path: Path, source_date_epoch: str) -> dict[str, object]:
    """Normalize one regular .tar.gz source distribution in place."""

    if _ARCHIVE_NAME_PATTERN.fullmatch(path.name) is None:
        _fail("archive_name_invalid")
    epoch = _parse_epoch(source_date_epoch)
    raw_archive, source_identity = _read_regular(path)
    expected_root = path.name[: -len(".tar.gz")]
    members = _load_members(raw_archive, expected_root)
    normalized = _write_canonical(path, members, epoch, source_identity)
    return {
        "archive": path.name,
        "byteSize": len(normalized),
        "memberCount": len(members),
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "sourceDateEpoch": epoch,
    }


def normalize_distribution_directory(directory: Path, source_date_epoch: str) -> dict[str, object]:
    """Select and normalize the only sdist in a regular distribution directory."""

    try:
        directory_status = directory.lstat()
    except OSError:
        _fail("distribution_directory_invalid")
    if stat.S_ISLNK(directory_status.st_mode) or not stat.S_ISDIR(directory_status.st_mode):
        _fail("distribution_directory_invalid")
    try:
        candidates = sorted(
            (entry for entry in directory.iterdir() if entry.name.endswith(".tar.gz")),
            key=lambda entry: entry.name,
        )
    except OSError:
        _fail("distribution_directory_invalid")
    if len(candidates) != 1:
        _fail("sdist_set_invalid")
    return normalize_sdist(candidates[0], source_date_epoch)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Canonicalize one built source distribution for reproducible packaging."
    )
    parser.add_argument("--distribution-directory", default="dist")
    parser.add_argument(
        "--source-date-epoch",
        default=None,
        help="canonical epoch; defaults to the SOURCE_DATE_EPOCH environment variable",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
    try:
        summary = normalize_distribution_directory(Path(args.distribution_directory), epoch)
    except SdistNormalizationError as exc:
        print(f"normalize_sdist: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
