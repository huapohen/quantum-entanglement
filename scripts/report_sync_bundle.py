#!/usr/bin/env python3
"""Build and verify a deterministic, local-only report synchronization bundle.

The bundle is an inventory, not a synchronizer.  It never contacts Notion,
Yuque, Feishu, WeCom, or any other service.  Historical manifest claims are
reported only as historical claims whose recorded digest still matches local
bytes; they are never presented as a current remote readback.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import struct
import sys
import unicodedata
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FORMAT = "quantum-entanglement.report-sync-bundle"
_SCHEMA_VERSION = 3
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_COUNT = 512
_MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
_MAX_DECODED_IMAGE_BYTES = 256 * 1024 * 1024
_MAX_DIRECTORY_ENTRIES = 1_024
_MAX_IMAGE_CHUNKS = 65_536
_MAX_JSON_NESTING_DEPTH = 64
_MAX_PATH_BYTES = 1_024
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,255}$")
_SOURCE_FILENAME_PATTERN = re.compile(r"^[0-9]{2}_[a-z0-9][a-z0-9_]*\.md$")
_IMAGE_FILENAME_PATTERN = re.compile(r"^[0-9]{2}_[a-z0-9][a-z0-9_]*\.(?:jpe?g|png)$")
_OUTPUT_FILENAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}\.json$")
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
_IMAGE_EXTENSIONS = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
_REDACTION_STATUSES = frozenset(
    {
        "not-redacted-synthetic-local-ui",
        "not-applicable-public-webpage-in-internal-evidence-set",
        "reviewed-no-credential-model-output-restricted",
        "unredacted-restricted-original",
    }
)
_YUQUE_VERIFICATION_STATES = frozenset(
    {
        "verified_10_images_ordered",
        "verified_19_rows",
        "verified_8_sections_3_tables_1_codeblock_4_notion_links",
        "verified_readback",
        "verified_unchanged",
    }
)
_TARGET_STATUSES = frozenset({"historical_manifest_claim_digest_match", "local_pending"})
_BUNDLE_DIRECTORY = "analysis_report/report_sync_bundles"
_SCREENSHOT_CLASSIFICATION = "restricted-internal"
_CREDENTIAL_TOKEN_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bntn_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-(?:ws-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}"),
)
_NAMED_CREDENTIAL_PATTERNS = (
    re.compile(
        r"(?im)^[ \t]*(?:export[ \t]+)?[\"'`]?"
        r"(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret|secret[_-]?key)[\"'`]?"
        r"[ \t]*(?:=|:)[ \t]*[\"'`]?([^\s\"'`]+)"
    ),
    re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:[A-Z][A-Z0-9]*_)*"
        r"(?:TOKEN|PASSWORD|PASSWD|SECRET)[ \t]*(?:=|:)[ \t]*"
        r"[\"'`]?([^\s\"'`]+)"
    ),
    re.compile(
        r"(?m)^[ \t]*(?:token|password|passwd|secret)[ \t]*="
        r"[ \t]*[\"'`]?([^\s\"'`]+)"
    ),
    re.compile(
        r"(?im)^[ \t]*[\"'](?:token|password|passwd|secret)[\"']"
        r"[ \t]*:[ \t]*[\"'`]?([^\s\"'`]+)"
    ),
    re.compile(
        r"(?im)^[ \t]*(?:api[ _-]?key|access[ _-]?token|client[ _-]?secret|"
        r"refresh[ _-]?token|secret[ _-]?key|token|password|passwd|secret)"
        r"[ \t]*:[ \t]*"
        r"[\"'`]?([^\s\"'`]+)[\"'`]?[ \t]*$"
    ),
    re.compile(
        r"(?im)^[ \t]*(?:[-*+]|>|[0-9]+[.)])[ \t]+[\"'`]?"
        r"(?:[A-Za-z0-9]+[_-])*(?:api[ _-]?key|access[ _-]?token|"
        r"refresh[ _-]?token|client[ _-]?secret|secret[ _-]?key|token|"
        r"password|passwd|secret)[\"'`]?[ \t]*(?:=|:)[ \t]*"
        r"[\"'`]?([^\s\"'`]+)"
    ),
    re.compile(
        r"(?im)^[ \t]*\|[ \t]*(?:[A-Za-z0-9]+[_-])*(?:api[ _-]?key|"
        r"access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|"
        r"secret[ _-]?key|token|password|passwd|secret)[ \t]*\|"
        r"[ \t]*[\"'`]?([^|\s\"'`]+)"
    ),
    re.compile(
        r"(?i)[\"'](?:[A-Za-z0-9]+[_ -])*(?:api[ _-]?key|access[ _-]?token|"
        r"refresh[ _-]?token|client[ _-]?secret|secret[ _-]?key|token|"
        r"password|passwd|secret)[\"'][ \t\r\n]*:[ \t\r\n]*"
        r"[\"'`]?((?:\$\{[A-Z][A-Z0-9_]*\}|\$[A-Z][A-Z0-9_]*|"
        r"[^\s\"'`,}\]]+))"
    ),
    re.compile(
        r"(?im)^[ \t]*(?:(?:[-*+]|[0-9]+[.)])[ \t]+|>[ \t]*)?"
        r"(?:authorization|cookie|set-cookie)"
        r"[ \t]*:[ \t]*([^\r\n]+)"
    ),
    re.compile(
        r"(?im)^[ \t]*\|[ \t]*(?:authorization|cookie|set-cookie)[ \t]*\|"
        r"[ \t]*([^|\r\n]+)"
    ),
)
_EXPLICIT_CREDENTIAL_PLACEHOLDERS = frozenset(
    {
        "<redacted>",
        "redacted",
        "sk-placeholder",
        "sk-your-api-key",
        "sk-your-example-placeholder",
        "your_access_token",
        "your_api_key",
        "your_client_secret",
        "your_password",
        "your_secret_key",
        "your_token",
    }
)
_ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"(?:\$[A-Z][A-Z0-9_]*|\$\{[A-Z][A-Z0-9_]*\})")
_CANONICAL_EXACT_PATHS = frozenset(
    {
        "analysis_report/README.md",
        "analysis_report/multi_agent_collaboration_report.md",
        "analysis_report/screenshots/README.md",
        "analysis_report/screenshots/manifest.json",
    }
)
_NOTION_MANIFEST_PATH = "analysis_report/notion_sync_manifest.json"
_YUQUE_MAPPING_PATH = "analysis_report/yuque_sync/mapping.json"
_SCREENSHOT_MANIFEST_PATH = "analysis_report/screenshots/manifest.json"


class ReportSyncBundleError(ValueError):
    """A fixed-code validation failure that is safe to emit in logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceFile:
    category: str
    path: str
    raw: bytes
    raw_sha256: str
    normalized_sha256: str


@dataclass(frozen=True)
class PreviousReference:
    expected_sha256: str
    target_page_key: str
    manifest_claimed_readback: bool


@dataclass(frozen=True)
class DescriptorSnapshot:
    device: int
    inode: int
    mode: int
    link_count: int
    user_id: int
    group_id: int
    byte_size: int
    modified_ns: int
    changed_ns: int
    sha256: str


@dataclass(frozen=True)
class DirectoryBinding:
    relative: str
    descriptor: int
    parent_relative: str | None
    entry_name: str | None
    device: int
    inode: int
    mode: int
    link_count: int
    user_id: int
    group_id: int


@dataclass(frozen=True)
class DirectoryEntrySnapshot:
    name: str
    device: int
    inode: int
    mode: int
    link_count: int
    user_id: int
    group_id: int
    byte_size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class PinnedRegularFile:
    relative: str
    descriptor: int
    parent_relative: str
    entry_name: str
    raw: bytes
    snapshot: DescriptorSnapshot


def _fail(code: str) -> NoReturn:
    raise ReportSyncBundleError(code)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: object) -> str:
    """Return the one accepted JSON representation, including one final LF."""

    _validate_json_tree(value, "bundle_json_invalid")
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except RecursionError:
        _fail("json_nesting_too_deep")
    except (TypeError, ValueError):
        _fail("bundle_json_invalid")
    return rendered + "\n"


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("json_duplicate_key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    _fail("json_non_finite_number")


def _parse_json(raw: bytes, code: str) -> Any:
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except ReportSyncBundleError:
        raise
    except RecursionError:
        _fail("json_nesting_too_deep")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _fail(code)
    _validate_json_tree(value, code)
    _scan_json_credential_fields(value)
    return value


def _validate_json_tree(value: object, code: str) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > _MAX_JSON_NESTING_DEPTH:
            _fail("json_nesting_too_deep")
        if type(item) is float:
            _fail(code)
        if type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is dict:
            stack.extend((child, depth + 1) for child in item.values())


def _repository_root(value: os.PathLike[str] | str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    try:
        metadata = path.lstat()
    except OSError:
        _fail("repository_root_invalid")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("repository_root_invalid")
    return path


def _is_sensitive_component(value: str) -> bool:
    lowered = value.casefold()
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or "secret" in lowered
        or lowered in {"credential", "credentials"}
        or lowered.endswith((".key", ".pem", ".p12", ".pfx"))
    )


def _is_credential_placeholder(value: str) -> bool:
    return (
        value.casefold() in _EXPLICIT_CREDENTIAL_PLACEHOLDERS
        or _ENVIRONMENT_REFERENCE_PATTERN.fullmatch(value) is not None
        or (len(value) >= 3 and set(value) == {"*"})
    )


def _is_credential_field_name(value: str) -> bool:
    canonical = re.sub(r"[^a-z0-9]", "", value.casefold())
    return canonical.endswith(
        (
            "apikey",
            "accesstoken",
            "refreshtoken",
            "clientsecret",
            "secretkey",
            "password",
            "passwd",
            "token",
            "secret",
            "authorization",
            "cookie",
            "setcookie",
        )
    )


def _scan_json_credential_fields(value: object) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is list:
            stack.extend(item)
        elif type(item) is dict:
            for key, child in item.items():
                if _is_credential_field_name(key):
                    if type(child) is not str or not _is_credential_placeholder(child):
                        _fail("credential_content_forbidden")
                stack.append(child)


def _scan_credentials(path: str, text: str) -> None:
    """Reject likely credentials while allowing explicit environment placeholders."""

    for pattern in _CREDENTIAL_TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            candidate = match.group(0)
            if candidate.casefold().startswith("bearer"):
                candidate = candidate.split(None, 1)[1]
            if not _is_credential_placeholder(candidate):
                _fail("credential_content_forbidden")
    for pattern in _NAMED_CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            candidate = match.group(1).rstrip("),.;")
            if not candidate or not _is_credential_placeholder(candidate):
                _fail("credential_content_forbidden")

    # Keep ``path`` in the API so a future audited diagnostic can identify a
    # controlled source without ever returning the matched credential.
    if not path:
        _fail("path_invalid")


def _safe_relative(value: object) -> str:
    if type(value) is not str:
        _fail("path_invalid")
    path = value
    if (
        not path
        or len(path.encode("utf-8")) > _MAX_PATH_BYTES
        or "\\" in path
        or "\x00" in path
        or path.startswith("/")
        or path.endswith("/")
    ):
        _fail("path_invalid")
    raw_parts = path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        _fail("path_invalid")
    pure = PurePosixPath(path)
    if pure.is_absolute() or pure.as_posix() != path:
        _fail("path_invalid")
    if any(_is_sensitive_component(part) for part in pure.parts):
        _fail("sensitive_path_forbidden")
    return path


def _metadata_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _descriptor_snapshot_matches(left: DescriptorSnapshot, right: DescriptorSnapshot) -> bool:
    return left == right


def _input_directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _input_file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


class _PinnedReadSession:
    """Hold an openat-rooted, revalidated view of all consumed local inputs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._descriptors: list[int] = []
        self._directories: dict[str, DirectoryBinding] = {}
        self._directory_scans: dict[str, tuple[DirectoryEntrySnapshot, ...]] = {}
        self._files: dict[str, PinnedRegularFile] = {}

    def _abort_enter(self, descriptor: int | None) -> None:
        """Release a root fd exactly once when ``__enter__`` never returns."""

        if descriptor is None:
            return
        self._directories.pop("", None)
        try:
            self._descriptors.remove(descriptor)
        except ValueError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            # A failed close must not replace the exception that prevented the
            # context from being established, and close must never be retried.
            pass

    def __enter__(self) -> _PinnedReadSession:
        descriptor: int | None = None
        try:
            before = self.root.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                _fail("repository_root_invalid")
            descriptor = os.open(self.root, _input_directory_flags())
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or (
                before.st_dev,
                before.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                _fail("repository_root_invalid")
            visible_after = self.root.lstat()
            if _metadata_identity(visible_after) != _metadata_identity(opened):
                _fail("repository_root_invalid")
            self._directories[""] = DirectoryBinding(
                relative="",
                descriptor=descriptor,
                parent_relative=None,
                entry_name=None,
                device=opened.st_dev,
                inode=opened.st_ino,
                mode=opened.st_mode,
                link_count=opened.st_nlink,
                user_id=opened.st_uid,
                group_id=opened.st_gid,
            )
            self._descriptors.append(descriptor)
            return self
        except OSError as error:
            self._abort_enter(descriptor)
            raise ReportSyncBundleError("repository_root_invalid") from error
        except BaseException:
            self._abort_enter(descriptor)
            raise

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        close_error: OSError | None = None
        for descriptor in reversed(self._descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                if close_error is None:
                    close_error = error
        self._descriptors.clear()
        if close_error is not None and exception_type is None:
            raise ReportSyncBundleError("source_close_failed") from close_error

    def _open_directory(self, relative: str, *, missing_code: str) -> DirectoryBinding:
        safe = _safe_relative(relative + "/placeholder").rsplit("/", 1)[0]
        if safe in self._directories:
            return self._directories[safe]
        current_relative = ""
        current = self._directories[current_relative]
        for part in PurePosixPath(safe).parts:
            child_relative = f"{current_relative}/{part}".lstrip("/")
            existing = self._directories.get(child_relative)
            if existing is not None:
                current_relative = child_relative
                current = existing
                continue
            try:
                before = os.stat(part, dir_fd=current.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                _fail(missing_code)
            except OSError:
                _fail("source_unreadable")
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                _fail("unsafe_symlink")
            try:
                descriptor = os.open(
                    part,
                    _input_directory_flags(),
                    dir_fd=current.descriptor,
                )
            except FileNotFoundError:
                _fail("source_changed_during_read")
            except OSError:
                _fail("source_unreadable")
            self._descriptors.append(descriptor)
            try:
                opened = os.fstat(descriptor)
            except OSError:
                _fail("source_unreadable")
            if not stat.S_ISDIR(opened.st_mode):
                _fail("unsafe_symlink")
            if _metadata_identity(before) != _metadata_identity(opened):
                _fail("source_changed_during_read")
            binding = DirectoryBinding(
                relative=child_relative,
                descriptor=descriptor,
                parent_relative=current_relative,
                entry_name=part,
                device=opened.st_dev,
                inode=opened.st_ino,
                mode=opened.st_mode,
                link_count=opened.st_nlink,
                user_id=opened.st_uid,
                group_id=opened.st_gid,
            )
            self._directories[child_relative] = binding
            current_relative = child_relative
            current = binding
        return current

    @staticmethod
    def _entry_snapshot(name: str, metadata: os.stat_result) -> DirectoryEntrySnapshot:
        return DirectoryEntrySnapshot(
            name=name,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            link_count=metadata.st_nlink,
            user_id=metadata.st_uid,
            group_id=metadata.st_gid,
            byte_size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
        )

    def _scan_open_directory(
        self,
        binding: DirectoryBinding,
        *,
        error_code: str,
    ) -> tuple[DirectoryEntrySnapshot, ...]:
        try:
            before = os.fstat(binding.descriptor)
            with os.scandir(binding.descriptor) as iterator:
                names = [entry.name for entry in iterator]
            if len(names) > _MAX_DIRECTORY_ENTRIES:
                _fail("source_inventory_too_large")
            if any(
                type(name) is not str or not name or "/" in name or "\x00" in name for name in names
            ):
                _fail(error_code)
            if len(set(names)) != len(names):
                _fail(error_code)
            entries = tuple(
                sorted(
                    (
                        self._entry_snapshot(
                            name,
                            os.stat(
                                name,
                                dir_fd=binding.descriptor,
                                follow_symlinks=False,
                            ),
                        )
                        for name in names
                    ),
                    key=lambda item: item.name,
                )
            )
            after = os.fstat(binding.descriptor)
        except ReportSyncBundleError:
            raise
        except OSError:
            _fail(error_code)
        if _metadata_identity(before) != _metadata_identity(after):
            _fail("source_changed_during_read")
        return entries

    def scan_directory(self, relative: str) -> tuple[DirectoryEntrySnapshot, ...]:
        safe = _safe_relative(relative + "/placeholder").rsplit("/", 1)[0]
        existing = self._directory_scans.get(safe)
        if existing is not None:
            return existing
        binding = self._open_directory(safe, missing_code="controlled_directory_missing")
        entries = self._scan_open_directory(binding, error_code="source_unreadable")
        self._directory_scans[safe] = entries
        return entries

    @staticmethod
    def _read_open_regular(
        descriptor: int,
        *,
        limit: int,
        unreadable_code: str,
        too_large_code: str,
    ) -> tuple[bytes, DescriptorSnapshot]:
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                _fail("unsafe_symlink")
            if before.st_size < 0 or before.st_size > limit:
                _fail(too_large_code)
            chunks: list[bytes] = []
            offset = 0
            while True:
                chunk = os.pread(descriptor, min(65_536, limit + 1 - offset), offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                if offset > limit:
                    _fail(too_large_code)
            after = os.fstat(descriptor)
        except ReportSyncBundleError:
            raise
        except OSError:
            _fail(unreadable_code)
        if _metadata_identity(before) != _metadata_identity(after) or offset != after.st_size:
            _fail("source_changed_during_read")
        raw = b"".join(chunks)
        return raw, DescriptorSnapshot(
            device=after.st_dev,
            inode=after.st_ino,
            mode=after.st_mode,
            link_count=after.st_nlink,
            user_id=after.st_uid,
            group_id=after.st_gid,
            byte_size=after.st_size,
            modified_ns=after.st_mtime_ns,
            changed_ns=after.st_ctime_ns,
            sha256=_sha256(raw),
        )

    def read_regular(
        self,
        relative: str,
        *,
        limit: int,
        missing_code: str = "source_missing",
    ) -> bytes:
        safe = _safe_relative(relative)
        cached = self._files.get(safe)
        if cached is not None:
            if len(cached.raw) > limit:
                _fail("source_too_large")
            return cached.raw
        parts = PurePosixPath(safe).parts
        parent_relative = "/".join(parts[:-1])
        parent = (
            self._directories[""]
            if not parent_relative
            else self._open_directory(parent_relative, missing_code=missing_code)
        )
        entry_name = parts[-1]
        try:
            before = os.stat(entry_name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            _fail(missing_code)
        except OSError:
            _fail("source_unreadable")
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            _fail("unsafe_symlink")
        if before.st_size > limit:
            _fail("source_too_large")
        try:
            descriptor = os.open(
                entry_name,
                _input_file_flags(),
                dir_fd=parent.descriptor,
            )
        except FileNotFoundError:
            _fail("source_changed_during_read")
        except OSError:
            _fail("source_unreadable")
        self._descriptors.append(descriptor)
        raw, snapshot = self._read_open_regular(
            descriptor,
            limit=limit,
            unreadable_code="source_unreadable",
            too_large_code="source_too_large",
        )
        if _metadata_identity(before) != (
            snapshot.device,
            snapshot.inode,
            snapshot.mode,
            snapshot.link_count,
            snapshot.user_id,
            snapshot.group_id,
            snapshot.byte_size,
            snapshot.modified_ns,
            snapshot.changed_ns,
        ):
            _fail("source_changed_during_read")
        try:
            visible = os.stat(entry_name, dir_fd=parent.descriptor, follow_symlinks=False)
        except OSError:
            _fail("source_changed_during_read")
        if _metadata_identity(visible) != _metadata_identity(before):
            _fail("source_changed_during_read")
        self._files[safe] = PinnedRegularFile(
            relative=safe,
            descriptor=descriptor,
            parent_relative=parent_relative,
            entry_name=entry_name,
            raw=raw,
            snapshot=snapshot,
        )
        return raw

    def _revalidate_file(self, pinned: PinnedRegularFile) -> None:
        raw, current = self._read_open_regular(
            pinned.descriptor,
            limit=max(pinned.snapshot.byte_size, 1),
            unreadable_code="source_changed_during_read",
            too_large_code="source_changed_during_read",
        )
        if raw != pinned.raw or not _descriptor_snapshot_matches(pinned.snapshot, current):
            _fail("source_changed_during_read")
        parent = self._directories[pinned.parent_relative]
        try:
            visible = os.stat(
                pinned.entry_name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except OSError:
            _fail("source_changed_during_read")
        if _metadata_identity(visible) != (
            current.device,
            current.inode,
            current.mode,
            current.link_count,
            current.user_id,
            current.group_id,
            current.byte_size,
            current.modified_ns,
            current.changed_ns,
        ):
            _fail("source_changed_during_read")

    def revalidate(self) -> None:
        bundle_paths = sorted(
            relative for relative in self._files if relative.startswith(_BUNDLE_DIRECTORY + "/")
        )
        for relative in sorted(set(self._files) - set(bundle_paths)):
            self._revalidate_file(self._files[relative])

        for relative in sorted(self._directory_scans):
            binding = self._directories[relative]
            current_entries = self._scan_open_directory(
                binding,
                error_code="source_changed_during_read",
            )
            if current_entries != self._directory_scans[relative]:
                _fail("source_changed_during_read")

        for relative in sorted(self._directories, key=lambda item: item.count("/"), reverse=True):
            binding = self._directories[relative]
            try:
                opened = os.fstat(binding.descriptor)
            except OSError:
                _fail("source_changed_during_read")
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_nlink,
                opened.st_uid,
                opened.st_gid,
            ) != (
                binding.device,
                binding.inode,
                binding.mode,
                binding.link_count,
                binding.user_id,
                binding.group_id,
            ):
                _fail("source_changed_during_read")
            if binding.parent_relative is None:
                try:
                    visible = self.root.lstat()
                except OSError:
                    _fail("source_changed_during_read")
            else:
                parent = self._directories[binding.parent_relative]
                try:
                    visible = os.stat(
                        str(binding.entry_name),
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    _fail("source_changed_during_read")
            if stat.S_ISLNK(visible.st_mode) or not stat.S_ISDIR(visible.st_mode):
                _fail("source_changed_during_read")
            if (
                visible.st_dev,
                visible.st_ino,
                visible.st_mode,
                visible.st_nlink,
                visible.st_uid,
                visible.st_gid,
            ) != (
                binding.device,
                binding.inode,
                binding.mode,
                binding.link_count,
                binding.user_id,
                binding.group_id,
            ):
                _fail("source_changed_during_read")

        # A verifier's saved bundle is re-read and name-bound after every source,
        # control, image, inventory, and intermediate directory has been checked.
        for relative in bundle_paths:
            self._revalidate_file(self._files[relative])


def _markdown_files(session: _PinnedReadSession, relative: str) -> list[str]:
    entries = session.scan_directory(relative)
    result: list[str] = []
    for entry in entries:
        if stat.S_ISLNK(entry.mode):
            _fail("unsafe_symlink")
        if not stat.S_ISREG(entry.mode):
            _fail("controlled_directory_entry_forbidden")
        if _SOURCE_FILENAME_PATTERN.fullmatch(entry.name) is None:
            _fail("source_filename_forbidden")
        candidate = f"{relative}/{entry.name}"
        result.append(_safe_relative(candidate))
    return sorted(result)


def _is_canonical_path(path: str) -> bool:
    return path in _CANONICAL_EXACT_PATHS or (
        path.startswith("analysis_report/research/")
        and path.endswith(".md")
        and len(PurePosixPath(path).parts) == 3
    )


def _is_mirror_path(path: str) -> bool:
    return (
        path.startswith("analysis_report/yuque_sync/source/")
        and path.endswith(".md")
        and len(PurePosixPath(path).parts) == 4
    )


def _normalized_bytes(path: str, raw: bytes) -> bytes:
    if path.endswith(".json"):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("source_text_invalid")
        _scan_credentials(path, text)
        value = _parse_json(raw, "source_json_invalid")
        return canonical_json(value).encode("utf-8")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        _fail("source_text_invalid")
    if "\x00" in text:
        _fail("source_text_invalid")
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    _scan_credentials(path, normalized)
    return normalized.encode("utf-8")


def _collect_sources(
    session: _PinnedReadSession,
) -> tuple[list[SourceFile], dict[str, SourceFile]]:
    paths: list[tuple[str, str]] = [
        ("canonical-source", "analysis_report/README.md"),
        ("canonical-source", "analysis_report/multi_agent_collaboration_report.md"),
    ]
    paths.extend(
        ("canonical-source", path) for path in _markdown_files(session, "analysis_report/research")
    )
    paths.extend(
        [
            ("canonical-source", "analysis_report/screenshots/README.md"),
            ("canonical-source", _SCREENSHOT_MANIFEST_PATH),
        ]
    )
    paths.extend(
        ("mirror-source", path)
        for path in _markdown_files(session, "analysis_report/yuque_sync/source")
    )
    if len(paths) > _MAX_SOURCE_COUNT:
        _fail("source_inventory_too_large")

    seen: set[str] = set()
    records: list[SourceFile] = []
    total_size = 0
    for category, path in paths:
        if path in seen:
            _fail("duplicate_source_path")
        seen.add(path)
        raw = session.read_regular(path, limit=_MAX_DOCUMENT_BYTES)
        total_size += len(raw)
        if total_size > _MAX_TOTAL_SOURCE_BYTES:
            _fail("source_inventory_too_large")
        normalized = _normalized_bytes(path, raw)
        records.append(
            SourceFile(
                category=category,
                path=path,
                raw=raw,
                raw_sha256=_sha256(raw),
                normalized_sha256=_sha256(normalized),
            )
        )
    records.sort(key=lambda item: (item.category, item.path))
    return records, {record.path: record for record in records}


def _require_object(value: object, code: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail(code)
    return value


def _require_list(value: object, code: str) -> list[object]:
    if type(value) is not list:
        _fail(code)
    return value


def _require_hash(value: object, code: str) -> str:
    if type(value) is not str or _HASH_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _require_string(value: object, code: str) -> str:
    if type(value) is not str:
        _fail(code)
    return value


def _require_integer(value: object, code: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(code)
    return value


def _require_boolean(value: object, code: str) -> bool:
    if type(value) is not bool:
        _fail(code)
    return value


def _manifest_metadata(path: str, raw: bytes) -> dict[str, object]:
    return {"byteSize": len(raw), "path": path, "rawSha256": _sha256(raw)}


def _load_notion_references(
    session: _PinnedReadSession,
) -> tuple[bytes, dict[str, PreviousReference]]:
    raw = session.read_regular(_NOTION_MANIFEST_PATH, limit=_MAX_MANIFEST_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("notion_manifest_invalid")
    _scan_credentials(_NOTION_MANIFEST_PATH, text)
    payload = _require_object(
        _parse_json(raw, "notion_manifest_invalid"), "notion_manifest_invalid"
    )
    if (
        payload.get("format") != "quantum-entanglement.notion-sync-manifest"
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
    ):
        _fail("notion_manifest_invalid")
    pages = _require_list(payload.get("pages"), "notion_manifest_invalid")
    if len(pages) > _MAX_SOURCE_COUNT:
        _fail("notion_manifest_invalid")
    references: dict[str, PreviousReference] = {}
    seen_keys: set[str] = set()
    for page_value in pages:
        page = _require_object(page_value, "notion_manifest_invalid")
        key_value = page.get("key")
        if type(key_value) is not str or _KEY_PATTERN.fullmatch(key_value) is None:
            _fail("notion_page_key_invalid")
        key = key_value
        if key in seen_keys:
            _fail("duplicate_page_key")
        seen_keys.add(key)
        readback_value = page.get("readback")
        readback = _require_object(readback_value, "notion_manifest_invalid")
        manifest_claimed_readback = _require_boolean(
            readback.get("verified"), "notion_manifest_invalid"
        )
        local_files = _require_list(page.get("localFiles"), "notion_manifest_invalid")
        if not local_files:
            _fail("notion_manifest_invalid")
        for file_value in local_files:
            local_file = _require_object(file_value, "notion_manifest_invalid")
            path = _safe_relative(local_file.get("path"))
            if not _is_canonical_path(path):
                _fail("manifest_source_outside_controlled_scope")
            if path in references:
                _fail("duplicate_manifest_path")
            references[path] = PreviousReference(
                expected_sha256=_require_hash(local_file.get("sha256"), "notion_hash_invalid"),
                target_page_key=key,
                manifest_claimed_readback=manifest_claimed_readback,
            )
    return raw, references


def _load_yuque_references(
    session: _PinnedReadSession,
) -> tuple[bytes, dict[str, PreviousReference]]:
    raw = session.read_regular(_YUQUE_MAPPING_PATH, limit=_MAX_MANIFEST_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("yuque_mapping_invalid")
    _scan_credentials(_YUQUE_MAPPING_PATH, text)
    payload = _require_object(_parse_json(raw, "yuque_mapping_invalid"), "yuque_mapping_invalid")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 2:
        _fail("yuque_mapping_invalid")
    objects = _require_list(payload.get("objects"), "yuque_mapping_invalid")
    if len(objects) > _MAX_SOURCE_COUNT:
        _fail("yuque_mapping_invalid")
    references: dict[str, PreviousReference] = {}
    seen_target_page_keys: set[str] = set()
    for object_value in objects:
        item = _require_object(object_value, "yuque_mapping_invalid")
        path = _safe_relative(item.get("source_path"))
        if not (_is_canonical_path(path) or _is_mirror_path(path)):
            _fail("manifest_source_outside_controlled_scope")
        if path in references:
            _fail("duplicate_manifest_path")
        verification = item.get("verification")
        if type(verification) is not str or verification not in _YUQUE_VERIFICATION_STATES:
            _fail("yuque_verification_state_invalid")
        target_page_key = item.get("yuque_slug")
        if (
            type(target_page_key) is not str
            or _KEY_PATTERN.fullmatch(target_page_key) is None
            or target_page_key in seen_target_page_keys
        ):
            _fail("yuque_page_key_invalid")
        seen_target_page_keys.add(target_page_key)
        references[path] = PreviousReference(
            expected_sha256=_require_hash(item.get("normalized_sha256"), "yuque_hash_invalid"),
            target_page_key=target_page_key,
            manifest_claimed_readback=True,
        )
    return raw, references


def _diagnostics(
    actual: dict[str, SourceFile],
    expected: dict[str, PreviousReference],
    *,
    category: str,
    normalized: bool,
) -> dict[str, object]:
    if category == "canonical-source":
        actual_paths = {path for path, item in actual.items() if item.category == category}
    else:
        mirror_paths = {path for path, item in actual.items() if item.category == category}
        explicitly_mapped = {path for path in expected if path in actual}
        actual_paths = mirror_paths | explicitly_mapped
    expected_paths = set(expected)
    missing = sorted(expected_paths - set(actual))
    extra = sorted(actual_paths - expected_paths)
    stale: list[dict[str, str]] = []
    for path in sorted(expected_paths & set(actual)):
        item = actual[path]
        actual_hash = item.normalized_sha256 if normalized else item.raw_sha256
        expected_hash = expected[path].expected_sha256
        if actual_hash != expected_hash:
            stale.append(
                {
                    "actualSha256": actual_hash,
                    "expectedSha256": expected_hash,
                    "path": path,
                }
            )
    return {"extra": extra, "missing": missing, "stale": stale}


def _generated_key(prefix: str, path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.casefold()).strip("-")
    slug = slug[:160].rstrip("-") or "source"
    return f"{prefix}-v1-{slug}-{_sha256(path.encode('utf-8'))[:12]}"


def _source_target_inventory(
    sources: list[SourceFile],
    notion: dict[str, PreviousReference],
    yuque: dict[str, PreviousReference],
) -> list[dict[str, object]]:
    source_targets: list[dict[str, object]] = []
    entry_keys: set[str] = set()
    source_by_path = {source.path: source for source in sources}
    notion_page_current: dict[str, bool] = {}
    for path, notion_reference in notion.items():
        source = source_by_path.get(path)
        matches = (
            notion_reference.manifest_claimed_readback
            and source is not None
            and notion_reference.expected_sha256 == source.raw_sha256
        )
        notion_page_current[notion_reference.target_page_key] = (
            notion_page_current.get(notion_reference.target_page_key, True) and matches
        )
    for source in sources:
        targets: list[tuple[str, PreviousReference | None]] = []
        if source.category == "canonical-source":
            targets.append(("notion", notion.get(source.path)))
        if source.category == "mirror-source" or source.path in yuque:
            targets.append(("yuque", yuque.get(source.path)))

        for target, source_reference in targets:
            entry_key = _generated_key(f"{target}-entry", source.path)
            target_page_key = (
                source_reference.target_page_key if source_reference is not None else None
            )
            proposed_target_page_key = (
                None if source_reference is not None else _generated_key(target, source.path)
            )
            if target == "notion":
                historical_claim_matches = source_reference is not None and notion_page_current.get(
                    source_reference.target_page_key, False
                )
            else:
                historical_claim_matches = (
                    source_reference is not None
                    and source_reference.manifest_claimed_readback
                    and source_reference.expected_sha256 == source.normalized_sha256
                )

            status = (
                "historical_manifest_claim_digest_match"
                if historical_claim_matches
                else "local_pending"
            )
            if entry_key in entry_keys:
                _fail("duplicate_source_target_key")
            entry_keys.add(entry_key)
            source_targets.append(
                {
                    "byteSize": len(source.raw),
                    "category": source.category,
                    "entryKey": entry_key,
                    "liveReadbackPerformed": False,
                    "normalizedSha256": source.normalized_sha256,
                    "path": source.path,
                    "proposedTargetPageKey": proposed_target_page_key,
                    "rawSha256": source.raw_sha256,
                    "target": target,
                    "targetPageKey": target_page_key,
                    "targetStatus": status,
                }
            )
    return sorted(
        source_targets,
        key=lambda item: (
            str(item["category"]),
            str(item["path"]),
            str(item["target"]),
        ),
    )


def _png_pass_dimensions(width: int, height: int, interlace_method: int) -> list[tuple[int, int]]:
    if interlace_method == 0:
        return [(width, height)]
    passes: list[tuple[int, int]] = []
    for x_start, y_start, x_step, y_step in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        pass_width = 0 if width <= x_start else (width - x_start + x_step - 1) // x_step
        pass_height = 0 if height <= y_start else (height - y_start + y_step - 1) // y_step
        if pass_width > 0 and pass_height > 0:
            passes.append((pass_width, pass_height))
    return passes


class _PngScanlineValidator:
    def __init__(
        self,
        passes: list[tuple[int, int]],
        bits_per_pixel: int,
        expected_size: int,
    ) -> None:
        self._passes = passes
        self._bits_per_pixel = bits_per_pixel
        self._expected_size = expected_size
        self._pass_index = 0
        self._rows_remaining = 0
        self._row_size = 0
        self._row_offset = 0
        self._total_size = 0
        self._advance_pass()

    def _advance_pass(self) -> None:
        while self._pass_index < len(self._passes):
            pass_width, pass_height = self._passes[self._pass_index]
            self._pass_index += 1
            if pass_height > 0:
                self._rows_remaining = pass_height
                self._row_size = (pass_width * self._bits_per_pixel + 7) // 8 + 1
                return
        self._rows_remaining = 0
        self._row_size = 0

    def consume(self, value: bytes) -> None:
        offset = 0
        while offset < len(value):
            if self._rows_remaining == 0 or self._row_size == 0:
                _fail("image_content_invalid")
            if self._row_offset == 0 and value[offset] > 4:
                _fail("image_content_invalid")
            consumed = min(len(value) - offset, self._row_size - self._row_offset)
            offset += consumed
            self._row_offset += consumed
            self._total_size += consumed
            if self._total_size > self._expected_size:
                _fail("image_content_invalid")
            if self._row_offset == self._row_size:
                self._row_offset = 0
                self._rows_remaining -= 1
                if self._rows_remaining == 0:
                    self._advance_pass()

    def finish(self) -> None:
        if (
            self._total_size != self._expected_size
            or self._rows_remaining != 0
            or self._row_offset != 0
        ):
            _fail("image_content_invalid")


def _validate_bounded_zlib_stream(
    parts: list[bytes],
    expected_size: int,
    scanlines: _PngScanlineValidator,
) -> None:
    if expected_size < 0 or expected_size > _MAX_DECODED_IMAGE_BYTES:
        _fail("image_content_invalid")
    decoder = zlib.decompressobj()
    decoded_size = 0
    try:
        for index, part in enumerate(parts):
            pending = part
            while pending:
                output_limit = min(65_536, expected_size - decoded_size + 1)
                if output_limit <= 0:
                    _fail("image_content_invalid")
                output = decoder.decompress(pending, output_limit)
                scanlines.consume(output)
                decoded_size += len(output)
                if decoded_size > expected_size or decoder.unused_data:
                    _fail("image_content_invalid")
                next_pending = decoder.unconsumed_tail
                if not next_pending:
                    break
                if not output and len(next_pending) >= len(pending):
                    _fail("image_content_invalid")
                pending = next_pending
            if decoder.eof and any(
                parts[remaining_index] for remaining_index in range(index + 1, len(parts))
            ):
                _fail("image_content_invalid")
            if decoder.eof:
                break
    except zlib.error:
        _fail("image_content_invalid")
    if (
        decoded_size != expected_size
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        _fail("image_content_invalid")
    scanlines.finish()


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    signature = b"\x89PNG\r\n\x1a\n"
    if not raw.startswith(signature):
        _fail("image_content_invalid")

    offset = len(signature)
    chunk_count = 0
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    color_type: int | None = None
    interlace_method: int | None = None
    palette_entries: int | None = None
    saw_idat = False
    idat_closed = False
    compressed_parts: list[bytes] = []

    while offset < len(raw):
        chunk_count += 1
        if chunk_count > _MAX_IMAGE_CHUNKS or offset + 12 > len(raw):
            _fail("image_content_invalid")
        chunk_size = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_end = offset + 12 + chunk_size
        if chunk_end > len(raw) or len(chunk_type) != 4:
            _fail("image_content_invalid")
        if any(not (65 <= value <= 90 or 97 <= value <= 122) for value in chunk_type):
            _fail("image_content_invalid")
        if chunk_type[2] & 0x20:
            _fail("image_content_invalid")
        chunk_data = raw[offset + 8 : offset + 8 + chunk_size]
        expected_crc = int.from_bytes(raw[offset + 8 + chunk_size : chunk_end], "big")
        actual_crc = zlib.crc32(chunk_data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            _fail("image_content_invalid")
        offset = chunk_end

        if chunk_type == b"IHDR":
            if width is not None or chunk_count != 1 or chunk_size != 13:
                _fail("image_content_invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace_method = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            valid_depths = {
                0: frozenset({1, 2, 4, 8, 16}),
                2: frozenset({8, 16}),
                3: frozenset({1, 2, 4, 8}),
                4: frozenset({8, 16}),
                6: frozenset({8, 16}),
            }
            if (
                width <= 0
                or height <= 0
                or color_type not in valid_depths
                or bit_depth not in valid_depths[color_type]
                or compression != 0
                or filtering != 0
                or interlace_method not in {0, 1}
            ):
                _fail("image_content_invalid")
        elif width is None:
            _fail("image_content_invalid")
        elif chunk_type == b"PLTE":
            if (
                saw_idat
                or palette_entries is not None
                or color_type in {0, 4}
                or chunk_size == 0
                or chunk_size % 3 != 0
            ):
                _fail("image_content_invalid")
            palette_entries = chunk_size // 3
            if palette_entries > 256 or (
                color_type == 3 and bit_depth is not None and palette_entries > 1 << bit_depth
            ):
                _fail("image_content_invalid")
        elif chunk_type == b"IDAT":
            if idat_closed or (color_type == 3 and palette_entries is None):
                _fail("image_content_invalid")
            saw_idat = True
            compressed_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            if chunk_size != 0 or not saw_idat or offset != len(raw):
                _fail("image_content_invalid")
            break
        else:
            if saw_idat:
                idat_closed = True
            if not chunk_type[0] & 0x20:
                _fail("image_content_invalid")
    else:
        _fail("image_content_invalid")

    if (
        width is None
        or height is None
        or bit_depth is None
        or color_type is None
        or interlace_method is None
        or (color_type == 3 and palette_entries is None)
    ):
        _fail("image_content_invalid")
    if color_type == 3 and palette_entries is not None and palette_entries > 1 << bit_depth:
        _fail("image_content_invalid")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bits_per_pixel = channels * bit_depth
    passes = _png_pass_dimensions(width, height, interlace_method)
    expected_decoded_size = 0
    for pass_width, pass_height in passes:
        row_size = (pass_width * bits_per_pixel + 7) // 8
        expected_decoded_size += pass_height * (row_size + 1)
        if expected_decoded_size > _MAX_DECODED_IMAGE_BYTES:
            _fail("image_content_invalid")

    _validate_bounded_zlib_stream(
        compressed_parts,
        expected_decoded_size,
        _PngScanlineValidator(passes, bits_per_pixel, expected_decoded_size),
    )
    return width, height


def _jpeg_dimensions(raw: bytes) -> tuple[int, int]:
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        _fail("image_content_invalid")
    offset = 2
    pending_marker: int | None = None
    dimensions: tuple[int, int] | None = None
    frame_marker: int | None = None
    frame_components: set[int] = set()
    frame_quantization_tables: dict[int, int] = {}
    quantization_tables: set[int] = set()
    huffman_tables: set[tuple[int, int]] = set()
    restart_interval: int | None = None
    saw_scan = False
    marker_count = 0
    supported_frames = frozenset({0xC0, 0xC1, 0xC2})
    all_frames = frozenset(
        {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    )

    while True:
        marker_count += 1
        if marker_count > _MAX_IMAGE_CHUNKS:
            _fail("image_content_invalid")
        if pending_marker is None:
            if offset >= len(raw) or raw[offset] != 0xFF:
                _fail("image_content_invalid")
            while offset < len(raw) and raw[offset] == 0xFF:
                offset += 1
            if offset >= len(raw):
                _fail("image_content_invalid")
            marker = raw[offset]
            offset += 1
        else:
            marker = pending_marker
            pending_marker = None

        if marker == 0xD9:
            if dimensions is None or not saw_scan or offset != len(raw):
                _fail("image_content_invalid")
            return dimensions
        if marker in {0x00, 0x01, 0xD8, *range(0xD0, 0xD9)}:
            _fail("image_content_invalid")
        if offset + 2 > len(raw):
            _fail("image_content_invalid")
        segment_size = int.from_bytes(raw[offset : offset + 2], "big")
        if segment_size < 2 or offset + segment_size > len(raw):
            _fail("image_content_invalid")
        segment = raw[offset + 2 : offset + segment_size]
        offset += segment_size

        if marker in all_frames:
            if marker not in supported_frames or dimensions is not None or len(segment) < 6:
                _fail("image_content_invalid")
            precision = segment[0]
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
            component_count = segment[5]
            if (
                precision != 8
                or width <= 0
                or height <= 0
                or component_count <= 0
                or len(segment) != 6 + 3 * component_count
                or width * height * component_count > _MAX_DECODED_IMAGE_BYTES
            ):
                _fail("image_content_invalid")
            components: set[int] = set()
            component_quantization_tables: dict[int, int] = {}
            for index in range(component_count):
                component_offset = 6 + index * 3
                component = segment[component_offset]
                sampling = segment[component_offset + 1]
                if (
                    component in components
                    or sampling >> 4 == 0
                    or sampling & 0x0F == 0
                    or segment[component_offset + 2] > 3
                ):
                    _fail("image_content_invalid")
                components.add(component)
                component_quantization_tables[component] = segment[component_offset + 2]
            dimensions = (width, height)
            frame_components = components
            frame_quantization_tables = component_quantization_tables
            frame_marker = marker
        elif marker == 0xDB:
            table_offset = 0
            while table_offset < len(segment):
                table_specification = segment[table_offset]
                table_offset += 1
                precision = table_specification >> 4
                table_id = table_specification & 0x0F
                if precision not in {0, 1} or table_id > 3:
                    _fail("image_content_invalid")
                table_size = 64 * (precision + 1)
                table_end = table_offset + table_size
                if table_end > len(segment):
                    _fail("image_content_invalid")
                table = segment[table_offset:table_end]
                if precision == 0:
                    if 0 in table:
                        _fail("image_content_invalid")
                elif any(
                    int.from_bytes(table[index : index + 2], "big") == 0
                    for index in range(0, len(table), 2)
                ):
                    _fail("image_content_invalid")
                quantization_tables.add(table_id)
                table_offset = table_end
            if table_offset == 0:
                _fail("image_content_invalid")
        elif marker == 0xC4:
            table_offset = 0
            while table_offset < len(segment):
                if table_offset + 17 > len(segment):
                    _fail("image_content_invalid")
                table_specification = segment[table_offset]
                table_class = table_specification >> 4
                table_id = table_specification & 0x0F
                code_counts = segment[table_offset + 1 : table_offset + 17]
                symbol_count = sum(code_counts)
                if table_class > 1 or table_id > 3:
                    _fail("image_content_invalid")
                symbols_start = table_offset + 17
                table_offset = symbols_start + symbol_count
                if symbol_count == 0 or table_offset > len(segment):
                    _fail("image_content_invalid")
                available_codes = 1
                for count in code_counts:
                    available_codes = available_codes * 2 - count
                    if available_codes < 0:
                        _fail("image_content_invalid")
                symbols = segment[symbols_start:table_offset]
                if table_class == 0:
                    if any(symbol > 11 for symbol in symbols):
                        _fail("image_content_invalid")
                elif any(
                    (symbol & 0x0F) > 10 or ((symbol & 0x0F) == 0 and symbol >> 4 not in {0, 15})
                    for symbol in symbols
                ):
                    _fail("image_content_invalid")
                huffman_tables.add((table_class, table_id))
            if table_offset == 0:
                _fail("image_content_invalid")
        elif marker == 0xDD:
            if len(segment) != 2:
                _fail("image_content_invalid")
            restart_interval = int.from_bytes(segment, "big")
        elif marker == 0xDA:
            if dimensions is None or frame_marker is None or len(segment) < 4:
                _fail("image_content_invalid")
            scan_component_count = segment[0]
            if scan_component_count <= 0 or len(segment) != 4 + 2 * scan_component_count:
                _fail("image_content_invalid")
            scan_components: set[int] = set()
            scan_table_selectors: dict[int, tuple[int, int]] = {}
            for index in range(scan_component_count):
                component = segment[1 + index * 2]
                table_selectors = segment[2 + index * 2]
                if (
                    component not in frame_components
                    or component in scan_components
                    or table_selectors >> 4 > 3
                    or table_selectors & 0x0F > 3
                ):
                    _fail("image_content_invalid")
                scan_components.add(component)
                scan_table_selectors[component] = (
                    table_selectors >> 4,
                    table_selectors & 0x0F,
                )
            spectral_start, spectral_end, approximation = segment[-3:]
            if frame_marker in {0xC0, 0xC1} and (
                spectral_start != 0 or spectral_end != 63 or approximation != 0
            ):
                _fail("image_content_invalid")
            if frame_marker == 0xC2 and (
                spectral_start > spectral_end
                or spectral_end > 63
                or approximation >> 4 > 13
                or approximation & 0x0F > 13
            ):
                _fail("image_content_invalid")
            approximation_high = approximation >> 4
            approximation_low = approximation & 0x0F
            if frame_marker == 0xC2 and approximation_high not in {
                0,
                approximation_low + 1,
            }:
                _fail("image_content_invalid")
            for component in scan_components:
                quantization_table = frame_quantization_tables.get(component)
                if quantization_table not in quantization_tables:
                    _fail("image_content_invalid")
                dc_table, ac_table = scan_table_selectors[component]
                if spectral_start == 0 and (0, dc_table) not in huffman_tables:
                    _fail("image_content_invalid")
                if (frame_marker in {0xC0, 0xC1} or spectral_start > 0) and (
                    1,
                    ac_table,
                ) not in huffman_tables:
                    _fail("image_content_invalid")

            entropy_bytes = 0
            expected_restart_marker = 0
            while offset < len(raw):
                if raw[offset] != 0xFF:
                    entropy_bytes += 1
                    offset += 1
                    continue
                offset += 1
                if offset >= len(raw):
                    _fail("image_content_invalid")
                while offset < len(raw) and raw[offset] == 0xFF:
                    offset += 1
                if offset >= len(raw):
                    _fail("image_content_invalid")
                entropy_marker = raw[offset]
                offset += 1
                if entropy_marker == 0x00:
                    entropy_bytes += 1
                    continue
                if 0xD0 <= entropy_marker <= 0xD7:
                    if not restart_interval or entropy_marker != 0xD0 + expected_restart_marker:
                        _fail("image_content_invalid")
                    expected_restart_marker = (expected_restart_marker + 1) % 8
                    continue
                pending_marker = entropy_marker
                break
            if pending_marker is None or entropy_bytes == 0:
                _fail("image_content_invalid")
            saw_scan = True


def _actual_image_metadata(raw: bytes) -> tuple[str, int, int]:
    png_signature = b"\x89PNG\r\n\x1a\n"
    if raw.startswith(png_signature):
        width, height = _png_dimensions(raw)
        return "image/png", width, height
    if raw.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(raw)
        return "image/jpeg", width, height
    _fail("image_content_invalid")


def _image_inventory(
    session: _PinnedReadSession, screenshot_manifest_raw: bytes
) -> list[dict[str, object]]:
    payload = _require_object(
        _parse_json(screenshot_manifest_raw, "screenshot_manifest_invalid"),
        "screenshot_manifest_invalid",
    )
    if (
        payload.get("format") != "quantum-entanglement.research-screenshot-manifest"
        or payload.get("accessClassification") != _SCREENSHOT_CLASSIFICATION
    ):
        _fail("screenshot_policy_invalid")
    items = _require_list(payload.get("items"), "screenshot_manifest_invalid")
    if len(items) > _MAX_SOURCE_COUNT:
        _fail("image_inventory_too_large")
    inventory: list[dict[str, object]] = []
    seen: set[str] = set()
    total_size = 0
    for item_value in items:
        item = _require_object(item_value, "screenshot_manifest_invalid")
        filename_value = item.get("filename")
        if type(filename_value) is not str:
            _fail("image_path_invalid")
        filename = filename_value
        if _IMAGE_FILENAME_PATTERN.fullmatch(filename) is None:
            _fail("image_path_invalid")
        if filename in seen:
            _fail("duplicate_image_path")
        seen.add(filename)
        media_type = item.get("mediaType")
        if type(media_type) is not str or media_type not in _IMAGE_MEDIA_TYPES:
            _fail("image_metadata_invalid")
        expected_media_type = _IMAGE_EXTENSIONS.get(Path(filename).suffix.casefold())
        if media_type != expected_media_type:
            _fail("image_mime_drift")
        byte_size = item.get("byteSize")
        width = item.get("width")
        height = item.get("height")
        if (
            type(byte_size) is not int
            or byte_size <= 0
            or type(width) is not int
            or width <= 0
            or type(height) is not int
            or height <= 0
        ):
            _fail("image_metadata_invalid")
        if (
            _require_boolean(item.get("notForPublicDistribution"), "screenshot_policy_invalid")
            is not True
        ):
            _fail("screenshot_policy_invalid")
        redaction_status = item.get("redactionStatus")
        if type(redaction_status) is not str or redaction_status not in _REDACTION_STATUSES:
            _fail("screenshot_policy_invalid")
        expected_hash = _require_hash(item.get("sha256"), "image_hash_invalid")
        path = f"analysis_report/screenshots/{filename}"
        raw = session.read_regular(path, limit=_MAX_IMAGE_BYTES, missing_code="image_missing")
        total_size += len(raw)
        if total_size > _MAX_TOTAL_IMAGE_BYTES:
            _fail("image_inventory_too_large")
        if len(raw) != byte_size:
            _fail("image_size_drift")
        actual_hash = _sha256(raw)
        if actual_hash != expected_hash:
            _fail("image_hash_drift")
        actual_media_type, actual_width, actual_height = _actual_image_metadata(raw)
        if actual_media_type != media_type:
            _fail("image_mime_drift")
        if (actual_width, actual_height) != (width, height):
            _fail("image_dimension_drift")
        inventory.append(
            {
                "accessClassification": _SCREENSHOT_CLASSIFICATION,
                "byteSize": len(raw),
                "height": height,
                "mediaType": media_type,
                "notForPublicDistribution": True,
                "path": path,
                "redactionStatus": redaction_status,
                "sha256": actual_hash,
                "width": width,
            }
        )

    entries = session.scan_directory("analysis_report/screenshots")
    actual_names: set[str] = set()
    for entry in entries:
        if stat.S_ISLNK(entry.mode):
            _fail("unsafe_symlink")
        if not stat.S_ISREG(entry.mode):
            _fail("controlled_directory_entry_forbidden")
        if entry.name in {"README.md", "manifest.json"}:
            continue
        if _IMAGE_FILENAME_PATTERN.fullmatch(entry.name) is None:
            _fail("screenshot_filename_forbidden")
        if Path(entry.name).suffix.casefold() in _IMAGE_EXTENSIONS:
            actual_names.add(entry.name)
    extra = sorted(actual_names - seen)
    if extra:
        _fail("unmanifested_image_forbidden")
    inventory.sort(key=lambda image: str(image["path"]))
    return inventory


def _generate_report_sync_bundle(session: _PinnedReadSession) -> dict[str, object]:
    sources, source_by_path = _collect_sources(session)
    notion_raw, notion_references = _load_notion_references(session)
    yuque_raw, yuque_references = _load_yuque_references(session)
    screenshot_manifest = source_by_path[_SCREENSHOT_MANIFEST_PATH]
    images = _image_inventory(session, screenshot_manifest.raw)
    source_targets = _source_target_inventory(
        sources,
        notion_references,
        yuque_references,
    )

    return {
        "accessPolicy": {
            "classification": _SCREENSHOT_CLASSIFICATION,
            "liveRemoteReadbackPerformed": False,
            "notForPublicDistribution": True,
            "screenshotManifest": _SCREENSHOT_MANIFEST_PATH,
        },
        "controls": {
            "notion": _manifest_metadata(_NOTION_MANIFEST_PATH, notion_raw),
            "screenshots": _manifest_metadata(_SCREENSHOT_MANIFEST_PATH, screenshot_manifest.raw),
            "yuque": _manifest_metadata(_YUQUE_MAPPING_PATH, yuque_raw),
        },
        "format": _FORMAT,
        "imageDiagnostics": {"unmanifestedPolicy": "fail-closed"},
        "images": images,
        "normalization": {
            "json": "sorted-utf8-json-lf-v1",
            "markdown": "utf8-no-bom-nfc-lf-v1",
        },
        "sourceTargets": source_targets,
        "previousManifestDiagnostics": {
            "notion": _diagnostics(
                source_by_path,
                notion_references,
                category="canonical-source",
                normalized=False,
            ),
            "yuque": _diagnostics(
                source_by_path,
                yuque_references,
                category="mirror-source",
                normalized=True,
            ),
        },
        "schemaVersion": _SCHEMA_VERSION,
        "sourceSummary": {
            "count": len(sources),
            "notionTargetCount": sum(1 for item in source_targets if item["target"] == "notion"),
            "sourceTargetCount": len(source_targets),
            "totalByteSize": sum(len(source.raw) for source in sources),
            "yuqueTargetCount": sum(1 for item in source_targets if item["target"] == "yuque"),
        },
        "statusSemantics": {
            "historical_manifest_claim_digest_match": (
                "A local historical manifest claims a readback and its recorded digest "
                "matches current local bytes; no live remote readback was performed."
            ),
            "local_pending": (
                "The source is inventoried locally but has no matching historical "
                "manifest claim; no live remote readback was performed."
            ),
        },
    }


def generate_report_sync_bundle(repository_root: os.PathLike[str] | str) -> dict[str, object]:
    """Generate a deterministic inventory without making any external call."""

    root = _repository_root(repository_root)
    with _PinnedReadSession(root) as session:
        payload = _generate_report_sync_bundle(session)
        session.revalidate()
        return payload


def _user_relative_path(root: Path, value: os.PathLike[str] | str) -> str:
    supplied = Path(value)
    candidate = supplied if supplied.is_absolute() else root / supplied
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError:
        _fail("path_escape")
    return _safe_relative(relative)


def _bundle_relative_path(root: Path, value: os.PathLike[str] | str) -> tuple[str, str]:
    relative = _user_relative_path(root, value)
    pure = PurePosixPath(relative)
    if pure.parent.as_posix() != _BUNDLE_DIRECTORY:
        _fail("bundle_location_forbidden")
    filename = pure.name
    if _OUTPUT_FILENAME_PATTERN.fullmatch(filename) is None:
        _fail("bundle_filename_forbidden")
    return relative, filename


def _require_exact_keys(value: dict[str, object], expected: frozenset[str], code: str) -> None:
    if frozenset(value) != expected:
        _fail(code)


def _validate_manifest_metadata(value: object, *, expected_path: str, code: str) -> None:
    metadata = _require_object(value, code)
    _require_exact_keys(metadata, frozenset({"byteSize", "path", "rawSha256"}), code)
    _require_integer(metadata.get("byteSize"), code)
    if metadata.get("path") != expected_path:
        _fail(code)
    _require_hash(metadata.get("rawSha256"), code)


def _validate_diagnostic_paths(value: object, *, allow_mirror: bool, code: str) -> None:
    diagnostics = _require_object(value, code)
    _require_exact_keys(diagnostics, frozenset({"extra", "missing", "stale"}), code)
    for field in ("extra", "missing"):
        paths = _require_list(diagnostics.get(field), code)
        seen: set[str] = set()
        for item in paths:
            path = _safe_relative(item)
            if not _is_canonical_path(path) and not (allow_mirror and _is_mirror_path(path)):
                _fail(code)
            if path in seen:
                _fail(code)
            seen.add(path)
    stale = _require_list(diagnostics.get("stale"), code)
    stale_paths: set[str] = set()
    for item in stale:
        record = _require_object(item, code)
        _require_exact_keys(
            record,
            frozenset({"actualSha256", "expectedSha256", "path"}),
            code,
        )
        _require_hash(record.get("actualSha256"), code)
        _require_hash(record.get("expectedSha256"), code)
        path = _safe_relative(record.get("path"))
        if not _is_canonical_path(path) and not (allow_mirror and _is_mirror_path(path)):
            _fail(code)
        if path in stale_paths:
            _fail(code)
        stale_paths.add(path)


def _validate_bundle_schema(payload: dict[str, object]) -> None:
    code = "bundle_schema_invalid"
    _require_exact_keys(
        payload,
        frozenset(
            {
                "accessPolicy",
                "controls",
                "format",
                "imageDiagnostics",
                "images",
                "normalization",
                "previousManifestDiagnostics",
                "schemaVersion",
                "sourceSummary",
                "sourceTargets",
                "statusSemantics",
            }
        ),
        code,
    )
    if payload.get("format") != _FORMAT:
        _fail(code)
    if (
        type(payload.get("schemaVersion")) is not int
        or payload.get("schemaVersion") != _SCHEMA_VERSION
    ):
        _fail(code)

    access_policy = _require_object(payload.get("accessPolicy"), code)
    _require_exact_keys(
        access_policy,
        frozenset(
            {
                "classification",
                "liveRemoteReadbackPerformed",
                "notForPublicDistribution",
                "screenshotManifest",
            }
        ),
        code,
    )
    if (
        access_policy.get("classification") != _SCREENSHOT_CLASSIFICATION
        or access_policy.get("liveRemoteReadbackPerformed") is not False
        or access_policy.get("notForPublicDistribution") is not True
        or access_policy.get("screenshotManifest") != _SCREENSHOT_MANIFEST_PATH
    ):
        _fail(code)

    controls = _require_object(payload.get("controls"), code)
    _require_exact_keys(controls, frozenset({"notion", "screenshots", "yuque"}), code)
    _validate_manifest_metadata(
        controls.get("notion"), expected_path=_NOTION_MANIFEST_PATH, code=code
    )
    _validate_manifest_metadata(
        controls.get("screenshots"),
        expected_path=_SCREENSHOT_MANIFEST_PATH,
        code=code,
    )
    _validate_manifest_metadata(controls.get("yuque"), expected_path=_YUQUE_MAPPING_PATH, code=code)

    normalization = _require_object(payload.get("normalization"), code)
    if normalization != {
        "json": "sorted-utf8-json-lf-v1",
        "markdown": "utf8-no-bom-nfc-lf-v1",
    }:
        _fail(code)

    source_summary = _require_object(payload.get("sourceSummary"), code)
    _require_exact_keys(
        source_summary,
        frozenset(
            {
                "count",
                "notionTargetCount",
                "sourceTargetCount",
                "totalByteSize",
                "yuqueTargetCount",
            }
        ),
        code,
    )
    summary_count = _require_integer(source_summary.get("count"), code, minimum=1)
    summary_notion_count = _require_integer(source_summary.get("notionTargetCount"), code)
    summary_source_target_count = _require_integer(
        source_summary.get("sourceTargetCount"),
        code,
        minimum=1,
    )
    summary_total_bytes = _require_integer(
        source_summary.get("totalByteSize"),
        code,
        minimum=1,
    )
    summary_yuque_count = _require_integer(source_summary.get("yuqueTargetCount"), code)

    semantics = _require_object(payload.get("statusSemantics"), code)
    if frozenset(semantics) != _TARGET_STATUSES:
        _fail(code)
    for explanation in semantics.values():
        _require_string(explanation, code)

    source_targets = _require_list(payload.get("sourceTargets"), code)
    if len(source_targets) > _MAX_SOURCE_COUNT * 2:
        _fail(code)
    entry_keys: set[str] = set()
    path_targets: set[tuple[str, str]] = set()
    yuque_target_page_keys: set[str] = set()
    notion_page_statuses: dict[str, str] = {}
    source_metadata: dict[str, tuple[str, int, str, str]] = {}
    for item in source_targets:
        source_target = _require_object(item, code)
        _require_exact_keys(
            source_target,
            frozenset(
                {
                    "byteSize",
                    "category",
                    "entryKey",
                    "liveReadbackPerformed",
                    "normalizedSha256",
                    "path",
                    "proposedTargetPageKey",
                    "rawSha256",
                    "target",
                    "targetPageKey",
                    "targetStatus",
                }
            ),
            code,
        )
        byte_size = _require_integer(source_target.get("byteSize"), code)
        category = _require_string(source_target.get("category"), code)
        path = _safe_relative(source_target.get("path"))
        target = _require_string(source_target.get("target"), code)
        if category == "canonical-source":
            if not _is_canonical_path(path) or target not in {"notion", "yuque"}:
                _fail(code)
        elif category == "mirror-source":
            if not _is_mirror_path(path) or target != "yuque":
                _fail(code)
        else:
            _fail(code)
        entry_key = _require_string(source_target.get("entryKey"), code)
        if (
            _KEY_PATTERN.fullmatch(entry_key) is None
            or entry_key != _generated_key(f"{target}-entry", path)
            or entry_key in entry_keys
        ):
            _fail(code)
        entry_keys.add(entry_key)
        if (path, target) in path_targets:
            _fail(code)
        path_targets.add((path, target))
        if source_target.get("liveReadbackPerformed") is not False:
            _fail(code)
        normalized_sha256 = _require_hash(source_target.get("normalizedSha256"), code)
        raw_sha256 = _require_hash(source_target.get("rawSha256"), code)
        metadata = (category, byte_size, raw_sha256, normalized_sha256)
        previous_metadata = source_metadata.setdefault(path, metadata)
        if previous_metadata != metadata:
            _fail(code)
        target_status = _require_string(source_target.get("targetStatus"), code)
        if target_status not in _TARGET_STATUSES:
            _fail(code)

        target_page_key_value = source_target.get("targetPageKey")
        proposed_page_key_value = source_target.get("proposedTargetPageKey")
        if target_page_key_value is None:
            target_page_key = None
        else:
            target_page_key = _require_string(target_page_key_value, code)
            if _KEY_PATTERN.fullmatch(target_page_key) is None:
                _fail(code)
        if proposed_page_key_value is None:
            proposed_page_key = None
        else:
            proposed_page_key = _require_string(proposed_page_key_value, code)
            if _KEY_PATTERN.fullmatch(proposed_page_key) is None:
                _fail(code)
        if (target_page_key is None) == (proposed_page_key is None):
            _fail(code)
        if target_page_key is None:
            if (
                proposed_page_key != _generated_key(target, path)
                or target_status != "local_pending"
            ):
                _fail(code)
        elif target == "yuque":
            if target_page_key in yuque_target_page_keys:
                _fail(code)
            yuque_target_page_keys.add(target_page_key)
        else:
            previous_status = notion_page_statuses.setdefault(target_page_key, target_status)
            if previous_status != target_status:
                _fail(code)

    if not source_targets:
        _fail(code)
    if any(
        (metadata[0] == "canonical-source" and (path, "notion") not in path_targets)
        or (metadata[0] == "mirror-source" and (path, "yuque") not in path_targets)
        for path, metadata in source_metadata.items()
    ):
        _fail(code)
    notion_count = sum(1 for _path, target in path_targets if target == "notion")
    yuque_count = sum(1 for _path, target in path_targets if target == "yuque")
    if (
        summary_count != len(source_metadata)
        or summary_source_target_count != len(source_targets)
        or summary_notion_count != notion_count
        or summary_yuque_count != yuque_count
        or summary_total_bytes != sum(metadata[1] for metadata in source_metadata.values())
    ):
        _fail(code)

    image_diagnostics = _require_object(payload.get("imageDiagnostics"), code)
    if image_diagnostics != {"unmanifestedPolicy": "fail-closed"}:
        _fail(code)

    images = _require_list(payload.get("images"), code)
    if len(images) > _MAX_SOURCE_COUNT:
        _fail(code)
    image_paths: set[str] = set()
    for item in images:
        image = _require_object(item, code)
        _require_exact_keys(
            image,
            frozenset(
                {
                    "accessClassification",
                    "byteSize",
                    "height",
                    "mediaType",
                    "notForPublicDistribution",
                    "path",
                    "redactionStatus",
                    "sha256",
                    "width",
                }
            ),
            code,
        )
        media_type = _require_string(image.get("mediaType"), code)
        redaction_status = _require_string(image.get("redactionStatus"), code)
        if (
            image.get("accessClassification") != _SCREENSHOT_CLASSIFICATION
            or image.get("notForPublicDistribution") is not True
            or media_type not in _IMAGE_MEDIA_TYPES
            or redaction_status not in _REDACTION_STATUSES
        ):
            _fail(code)
        _require_integer(image.get("byteSize"), code, minimum=1)
        _require_integer(image.get("height"), code, minimum=1)
        _require_integer(image.get("width"), code, minimum=1)
        _require_hash(image.get("sha256"), code)
        path = _safe_relative(image.get("path"))
        if not path.startswith("analysis_report/screenshots/") or path in image_paths:
            _fail(code)
        filename = PurePosixPath(path).name
        if _IMAGE_FILENAME_PATTERN.fullmatch(filename) is None:
            _fail(code)
        image_paths.add(path)

    previous = _require_object(payload.get("previousManifestDiagnostics"), code)
    _require_exact_keys(previous, frozenset({"notion", "yuque"}), code)
    _validate_diagnostic_paths(previous.get("notion"), allow_mirror=False, code=code)
    _validate_diagnostic_paths(previous.get("yuque"), allow_mirror=True, code=code)


def verify_report_sync_bundle(
    repository_root: os.PathLike[str] | str,
    bundle_path: os.PathLike[str] | str,
) -> dict[str, object]:
    """Verify canonical encoding and bind a saved bundle to current local bytes."""

    root = _repository_root(repository_root)
    relative, _ = _bundle_relative_path(root, bundle_path)
    with _PinnedReadSession(root) as session:
        raw = session.read_regular(
            relative,
            limit=_MAX_MANIFEST_BYTES,
            missing_code="bundle_missing",
        )
        payload = _require_object(_parse_json(raw, "bundle_json_invalid"), "bundle_json_invalid")
        if raw != canonical_json(payload).encode("utf-8"):
            _fail("bundle_non_canonical")
        _validate_bundle_schema(payload)
        current = _generate_report_sync_bundle(session)
        if payload != current:
            _fail("bundle_hash_drift")
        session.revalidate()
        return payload


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _close_descriptor(descriptor: int, *, code: str, active_error: bool) -> None:
    """Close exactly once; never mask an active exception or retry EINTR."""

    try:
        os.close(descriptor)
    except OSError as error:
        if not active_error:
            raise ReportSyncBundleError(code) from error


def _open_checked_directory(path: Path) -> int:
    try:
        before = path.lstat()
    except OSError:
        _fail("output_write_failed")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        _fail("unsafe_symlink")
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError:
        _fail("output_write_failed")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file(before, opened):
            _fail("unsafe_symlink")
        return descriptor
    except BaseException:
        _close_descriptor(descriptor, code="output_write_failed", active_error=True)
        raise


def _open_child_directory(parent_descriptor: int, name: str) -> int:
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        _fail("output_write_failed")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        _fail("unsafe_symlink")
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
    except OSError:
        _fail("output_write_failed")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file(before, opened):
            _fail("unsafe_symlink")
        return descriptor
    except BaseException:
        _close_descriptor(descriptor, code="output_write_failed", active_error=True)
        raise


def _open_bundle_directory(root: Path) -> int:
    root_descriptor: int | None = None
    analysis_descriptor: int | None = None
    bundle_descriptor: int | None = None
    try:
        root_descriptor = _open_checked_directory(root)
        analysis_descriptor = _open_child_directory(root_descriptor, "analysis_report")
        try:
            os.mkdir("report_sync_bundles", mode=0o700, dir_fd=analysis_descriptor)
        except FileExistsError:
            pass
        except OSError:
            _fail("output_write_failed")
        bundle_descriptor = _open_child_directory(analysis_descriptor, "report_sync_bundles")
        try:
            os.fsync(analysis_descriptor)
        except OSError:
            _fail("output_write_failed")

        descriptor_to_close = analysis_descriptor
        analysis_descriptor = None
        _close_descriptor(
            descriptor_to_close,
            code="output_write_failed",
            active_error=False,
        )
        descriptor_to_close = root_descriptor
        root_descriptor = None
        _close_descriptor(
            descriptor_to_close,
            code="output_write_failed",
            active_error=False,
        )
        result = bundle_descriptor
        bundle_descriptor = None
        return result
    except BaseException:
        for descriptor in (bundle_descriptor, analysis_descriptor, root_descriptor):
            if descriptor is not None:
                _close_descriptor(
                    descriptor,
                    code="output_write_failed",
                    active_error=True,
                )
        raise


def _bundle_directory_is_bound(root: Path, descriptor: int) -> bool:
    try:
        visible = root.joinpath(*PurePosixPath(_BUNDLE_DIRECTORY).parts).lstat()
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(visible.st_mode)
        and stat.S_ISDIR(visible.st_mode)
        and _same_file(visible, opened)
    )


def _new_output_entry_name(prefix: str) -> str:
    return f".{prefix}-{secrets.token_hex(16)}"


def _create_temporary_output(directory_descriptor: int) -> tuple[int, str]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(128):
        name = _new_output_entry_name("report-sync")
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_descriptor), name
        except FileExistsError:
            continue
        except OSError:
            _fail("output_write_failed")
    _fail("output_write_failed")


def _descriptor_snapshot(
    descriptor: int,
    *,
    limit: int,
    code: str,
) -> DescriptorSnapshot:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > limit:
            _fail(code)
        digest = hashlib.sha256()
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(descriptor, min(65_536, before.st_size - offset), offset)
            if not chunk:
                _fail(code)
            digest.update(chunk)
            offset += len(chunk)
        if os.pread(descriptor, 1, offset):
            _fail(code)
        after = os.fstat(descriptor)
    except OSError:
        _fail(code)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity or offset != after.st_size:
        _fail(code)
    return DescriptorSnapshot(
        device=after.st_dev,
        inode=after.st_ino,
        mode=after.st_mode,
        link_count=after.st_nlink,
        user_id=after.st_uid,
        group_id=after.st_gid,
        byte_size=after.st_size,
        modified_ns=after.st_mtime_ns,
        changed_ns=after.st_ctime_ns,
        sha256=digest.hexdigest(),
    )


def _same_descriptor_content(left: DescriptorSnapshot, right: DescriptorSnapshot) -> bool:
    return (
        left.device,
        left.inode,
        left.mode,
        left.link_count,
        left.user_id,
        left.group_id,
        left.byte_size,
        left.modified_ns,
        left.sha256,
    ) == (
        right.device,
        right.inode,
        right.mode,
        right.link_count,
        right.user_id,
        right.group_id,
        right.byte_size,
        right.modified_ns,
        right.sha256,
    )


def _rename_with_flags(
    directory_descriptor: int,
    source: str,
    destination: str,
    *,
    exchange: bool,
) -> None:
    """Rename atomically without deleting an independently created directory entry."""

    if sys.platform == "darwin":
        function_name = "renameatx_np"
        flag = 0x00000002 if exchange else 0x00000004  # RENAME_SWAP / RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
        flag = 0x00000002 if exchange else 0x00000001  # EXCHANGE / NOREPLACE
    else:
        _fail("output_atomic_publish_unsupported")

    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(library, function_name)
    except AttributeError as error:
        raise ReportSyncBundleError("output_atomic_publish_unsupported") from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        directory_descriptor,
        os.fsencode(source),
        directory_descriptor,
        os.fsencode(destination),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        if error_number in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
            _fail("output_atomic_publish_unsupported")
        raise OSError(error_number, os.strerror(error_number))


def _rename_no_replace(directory_descriptor: int, source: str, destination: str) -> None:
    _rename_with_flags(
        directory_descriptor,
        source,
        destination,
        exchange=False,
    )


def _rename_exchange(directory_descriptor: int, left: str, right: str) -> None:
    _rename_with_flags(
        directory_descriptor,
        left,
        right,
        exchange=True,
    )


def _optional_opened_entry_metadata(
    directory_descriptor: int,
    name: str,
    *,
    code: str,
) -> os.stat_result | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError:
        _fail(code)
    try:
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            _fail(code)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(code)
        return metadata
    finally:
        _close_descriptor(
            descriptor,
            code=code,
            active_error=sys.exc_info()[0] is not None,
        )


def _opened_entry_metadata(
    directory_descriptor: int,
    name: str,
    *,
    code: str,
) -> os.stat_result:
    metadata = _optional_opened_entry_metadata(
        directory_descriptor,
        name,
        code=code,
    )
    if metadata is None:
        _fail(code)
    return metadata


def _open_optional_output_entry(
    directory_descriptor: int,
    name: str,
) -> tuple[int, os.stat_result] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("output_invalid")
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        _close_descriptor(descriptor, code="output_invalid", active_error=True)
        _fail("output_invalid")
    except BaseException:
        _close_descriptor(descriptor, code="output_invalid", active_error=True)
        raise
    if not stat.S_ISREG(metadata.st_mode):
        _close_descriptor(descriptor, code="output_invalid", active_error=True)
        _fail("output_invalid")
    return descriptor, metadata


def _write_output(
    root: Path,
    value: os.PathLike[str] | str,
    raw: bytes,
    *,
    overwrite: bool = False,
) -> None:
    if len(raw) > _MAX_MANIFEST_BYTES:
        _fail("output_write_failed")
    _, filename = _bundle_relative_path(root, value)
    directory_descriptor = _open_bundle_directory(root)
    destination_descriptor: int | None = None
    temporary_descriptor: int | None = None
    destination_snapshot_initial: DescriptorSnapshot | None = None
    displaced_snapshot_before: DescriptorSnapshot | None = None
    commit_started = False
    commit_completed = False
    try:
        if not _bundle_directory_is_bound(root, directory_descriptor):
            _fail("unsafe_symlink")
        destination_entry = _open_optional_output_entry(directory_descriptor, filename)
        if destination_entry is None:
            destination_before = None
        else:
            destination_descriptor, destination_before = destination_entry
            if not overwrite:
                _fail("output_exists")
            destination_snapshot_initial = _descriptor_snapshot(
                destination_descriptor,
                limit=_MAX_MANIFEST_BYTES,
                code="output_invalid",
            )

        temporary_descriptor, temporary_name = _create_temporary_output(directory_descriptor)
        written = 0
        while written < len(raw):
            count = os.write(temporary_descriptor, raw[written:])
            if count <= 0:
                _fail("output_write_failed")
            written += count
        os.fchmod(temporary_descriptor, 0o400)
        os.fsync(temporary_descriptor)
        temporary_metadata = os.fstat(temporary_descriptor)
        temporary_snapshot = _descriptor_snapshot(
            temporary_descriptor,
            limit=_MAX_MANIFEST_BYTES,
            code="output_write_failed",
        )
        if temporary_snapshot.byte_size != len(raw) or temporary_snapshot.sha256 != _sha256(raw):
            _fail("output_write_failed")
        os.fsync(directory_descriptor)
        if not _bundle_directory_is_bound(root, directory_descriptor):
            _fail("unsafe_symlink")

        if overwrite and destination_before is not None:
            if destination_descriptor is None or destination_snapshot_initial is None:
                _fail("output_concurrent_change")
            displaced_snapshot_before = _descriptor_snapshot(
                destination_descriptor,
                limit=_MAX_MANIFEST_BYTES,
                code="output_concurrent_change",
            )
            if not _same_descriptor_content(
                destination_snapshot_initial,
                displaced_snapshot_before,
            ):
                _fail("output_concurrent_change")
            destination_current = _opened_entry_metadata(
                directory_descriptor,
                filename,
                code="output_concurrent_change",
            )
            if not _same_file(destination_before, destination_current):
                _fail("output_concurrent_change")
            commit_started = True
            _rename_exchange(directory_descriptor, temporary_name, filename)
        else:
            commit_started = True
            try:
                _rename_no_replace(directory_descriptor, temporary_name, filename)
            except FileExistsError:
                if overwrite:
                    _fail("output_concurrent_change")
                _fail("output_exists")
        commit_completed = True

        destination_after = _opened_entry_metadata(
            directory_descriptor,
            filename,
            code="output_commit_uncertain",
        )
        if not _same_file(temporary_metadata, destination_after):
            _fail("output_concurrent_change")
        temporary_snapshot_after = _descriptor_snapshot(
            temporary_descriptor,
            limit=_MAX_MANIFEST_BYTES,
            code="output_commit_uncertain",
        )
        if not _same_descriptor_content(temporary_snapshot, temporary_snapshot_after):
            _fail("output_concurrent_change")
        if destination_before is not None:
            displaced = _opened_entry_metadata(
                directory_descriptor,
                temporary_name,
                code="output_commit_uncertain",
            )
            if not _same_file(destination_before, displaced):
                _fail("output_concurrent_change")
            if destination_descriptor is None or displaced_snapshot_before is None:
                _fail("output_concurrent_change")
            displaced_snapshot_after = _descriptor_snapshot(
                destination_descriptor,
                limit=_MAX_MANIFEST_BYTES,
                code="output_commit_uncertain",
            )
            if not _same_descriptor_content(
                displaced_snapshot_before,
                displaced_snapshot_after,
            ):
                _fail("output_concurrent_change")
        try:
            os.fsync(directory_descriptor)
        except OSError:
            _fail("output_commit_uncertain")
        if not _bundle_directory_is_bound(root, directory_descriptor):
            _fail("output_commit_uncertain")

        final_candidate_snapshot = _descriptor_snapshot(
            temporary_descriptor,
            limit=_MAX_MANIFEST_BYTES,
            code="output_commit_uncertain",
        )
        if not _same_descriptor_content(temporary_snapshot, final_candidate_snapshot):
            _fail("output_concurrent_change")
        if destination_before is not None:
            final_recovery = _opened_entry_metadata(
                directory_descriptor,
                temporary_name,
                code="output_commit_uncertain",
            )
            if not _same_file(destination_before, final_recovery):
                _fail("output_concurrent_change")
            if destination_descriptor is None or displaced_snapshot_before is None:
                _fail("output_concurrent_change")
            final_displaced_snapshot = _descriptor_snapshot(
                destination_descriptor,
                limit=_MAX_MANIFEST_BYTES,
                code="output_commit_uncertain",
            )
            if not _same_descriptor_content(
                displaced_snapshot_before,
                final_displaced_snapshot,
            ):
                _fail("output_concurrent_change")
        final_destination = _opened_entry_metadata(
            directory_descriptor,
            filename,
            code="output_commit_uncertain",
        )
        if not _same_file(temporary_metadata, final_destination):
            _fail("output_concurrent_change")
        if not _bundle_directory_is_bound(root, directory_descriptor):
            _fail("output_commit_uncertain")
    except BaseException as error:
        if isinstance(error, OSError):
            code = "output_commit_uncertain" if commit_started else "output_write_failed"
            raise ReportSyncBundleError(code) from error
        raise
    finally:
        had_active_error = sys.exc_info()[0] is not None
        close_error: OSError | None = None
        for open_descriptor in (
            temporary_descriptor,
            destination_descriptor,
            directory_descriptor,
        ):
            if open_descriptor is None:
                continue
            try:
                os.close(open_descriptor)
            except OSError as error:
                if close_error is None:
                    close_error = error
        if close_error is not None and not had_active_error:
            code = "output_commit_uncertain" if commit_completed else "output_write_failed"
            raise ReportSyncBundleError(code) from close_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify a deterministic, local-only report sync inventory."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
        help="repository root (defaults to the script's repository)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "explicit .json path directly under analysis_report/report_sync_bundles; "
            "otherwise write JSON to stdout"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="explicitly replace an existing bundle output (generation only)",
    )
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="BUNDLE",
        help="verify an existing bundle inside the repository instead of generating one",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.verify is not None and arguments.output is not None:
        parser.error("--verify and --output cannot be combined")
    if arguments.overwrite and arguments.output is None:
        parser.error("--overwrite requires --output")
    try:
        root = _repository_root(arguments.repository_root)
        if arguments.verify is not None:
            verify_report_sync_bundle(root, arguments.verify)
            sys.stdout.write("report-sync bundle verified\n")
            return 0
        bundle = generate_report_sync_bundle(root)
        rendered = canonical_json(bundle)
        if arguments.output is None:
            sys.stdout.write(rendered)
        else:
            _write_output(
                root,
                arguments.output,
                rendered.encode("utf-8"),
                overwrite=arguments.overwrite,
            )
        return 0
    except ReportSyncBundleError as error:
        sys.stderr.write(f"report-sync bundle error: {error.code}\n")
        return 2
    except OSError:
        sys.stderr.write("report-sync bundle error: output_write_failed\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
