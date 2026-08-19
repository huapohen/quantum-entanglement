# ruff: noqa: UP006, UP035, UP045
"""Transactionally consistent SQLite backup creation and verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, cast

from .migrations import (
    MIGRATIONS,
    MigrationDriftError,
    MigrationVersionError,
    migration_text,
    validate_sqlite_schema,
)
from .protocol import new_id, utc_now

_FORMAT = "qe.sqlite-backup/1"
_MAX_MANIFEST_BYTES = 1024 * 1024
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_CORE_TABLES = (
    "events",
    "snapshots",
    "outbox",
    "outbox_ambiguities",
    "inbox_receipts",
    "invocation_jobs",
    "invocation_attempts",
    "artifact_blobs",
    "artifact_versions",
    "projector_offsets",
    "action_receipts",
)


class BackupError(RuntimeError):
    """Base class for backup failures safe to present to an operator."""


class BackupExistsError(BackupError):
    """Raised when a backup would overwrite an existing path."""


class BackupIntegrityError(BackupError):
    """Raised when a database or manifest fails verification."""


def _plain_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_migration_evidence(
    migrations: Tuple[Mapping[str, Any], ...],
) -> None:
    if len(migrations) > len(MIGRATIONS):
        raise ValueError("backup schema is newer than this binary")
    for index, evidence in enumerate(migrations):
        expected = MIGRATIONS[index]
        expected_digest = hashlib.sha256(
            migration_text(expected.filename).encode("utf-8")
        ).hexdigest()
        if evidence["version"] != expected.version:
            raise ValueError("backup migrations must be a continuous supported prefix")
        if evidence["filename"] != expected.filename:
            raise ValueError("backup migration filename is not supported by this binary")
        if evidence["sha256"] != expected_digest:
            raise ValueError("backup migration checksum differs from this binary")


def _normalize_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("backup clock must return an RFC 3339 string")
    if not _RFC3339_PATTERN.fullmatch(value) or value.endswith("-00:00"):
        raise ValueError("backup clock must return a strict RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("backup clock returned an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("backup timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileIdentity:
        return cls(device=int(value.st_dev), inode=int(value.st_ino))

    def matches(self, value: os.stat_result) -> bool:
        return self.device == int(value.st_dev) and self.inode == int(value.st_ino)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_fd(handle.fileno())


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("write returned no progress")
        view = view[written:]


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@dataclass(frozen=True)
class _DatabaseEvidence:
    page_count: int
    page_size: int
    table_counts: Mapping[str, int]
    migrations: Tuple[Mapping[str, Any], ...]


def _database_evidence(connection: sqlite3.Connection) -> _DatabaseEvidence:
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    if len(integrity_rows) != 1 or integrity_rows[0][0] != "ok":
        raise BackupIntegrityError("SQLite integrity_check did not return ok")
    foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_rows:
        raise BackupIntegrityError(
            f"SQLite foreign_key_check found {len(foreign_key_rows)} violation(s)"
        )
    try:
        validate_sqlite_schema(connection)
    except (MigrationDriftError, MigrationVersionError, sqlite3.DatabaseError) as exc:
        raise BackupIntegrityError(
            "SQLite schema differs from its supported migration ledger"
        ) from exc
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    table_counts: Dict[str, int] = {}
    for table in _CORE_TABLES:
        if table in existing_tables:
            row = connection.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()
            table_counts[table] = int(row["count"])
    migrations: Tuple[Mapping[str, Any], ...] = ()
    if "qe_schema_migrations" in existing_tables:
        rows = connection.execute(
            """
            SELECT version, filename, sha256, applied_at
            FROM qe_schema_migrations ORDER BY version
            """
        ).fetchall()
        try:
            migrations = tuple(
                {
                    "version": int(row["version"]),
                    "filename": _plain_string(row["filename"], "migration filename"),
                    "sha256": _plain_string(row["sha256"], "migration sha256"),
                    "appliedAt": _normalize_timestamp(
                        _plain_string(row["applied_at"], "migration appliedAt")
                    ),
                }
                for row in rows
            )
            _validate_migration_evidence(migrations)
        except (TypeError, ValueError) as exc:
            raise BackupIntegrityError(
                "SQLite migration ledger is not supported by this binary"
            ) from exc
    page_count_row = connection.execute("PRAGMA page_count").fetchone()
    page_size_row = connection.execute("PRAGMA page_size").fetchone()
    return _DatabaseEvidence(
        page_count=int(page_count_row[0]),
        page_size=int(page_size_row[0]),
        table_counts=table_counts,
        migrations=migrations,
    )


@dataclass(frozen=True)
class BackupManifest:
    format_version: str
    backup_id: str
    created_at: str
    database_sha256: str
    byte_size: int
    page_count: int
    page_size: int
    table_counts: Mapping[str, int]
    migrations: Tuple[Mapping[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "formatVersion": self.format_version,
            "backupId": self.backup_id,
            "createdAt": self.created_at,
            "databaseSha256": self.database_sha256,
            "byteSize": self.byte_size,
            "pageCount": self.page_count,
            "pageSize": self.page_size,
            "tableCounts": dict(self.table_counts),
            "migrations": [dict(item) for item in self.migrations],
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> BackupManifest:
        if not isinstance(value, dict):
            raise TypeError("backup manifest must be a plain dictionary")
        expected = {
            "formatVersion",
            "backupId",
            "createdAt",
            "databaseSha256",
            "byteSize",
            "pageCount",
            "pageSize",
            "tableCounts",
            "migrations",
        }
        if set(value) != expected:
            raise ValueError("backup manifest fields do not match format version 1")
        if value["formatVersion"] != _FORMAT:
            raise ValueError("unsupported backup manifest format")
        backup_id = _plain_string(value["backupId"], "backupId")
        if not re.fullmatch(r"backup_[0-9a-f]{32}", backup_id):
            raise ValueError("backupId does not match format version 1")
        created_at = _normalize_timestamp(_plain_string(value["createdAt"], "createdAt"))
        database_sha256 = _plain_string(value["databaseSha256"], "databaseSha256")
        if len(database_sha256) != 64 or re.search(r"[^0-9a-f]", database_sha256):
            raise ValueError("databaseSha256 must be lowercase SHA-256 hex")
        for name in ("byteSize", "pageCount", "pageSize"):
            item = value[name]
            if type(item) is not int or item <= 0:
                raise ValueError(f"{name} must be a positive integer")
        page_size = value["pageSize"]
        if page_size < 512 or page_size > 65_536 or page_size & (page_size - 1):
            raise ValueError("pageSize is not supported by SQLite")
        if value["byteSize"] != value["pageCount"] * page_size:
            raise ValueError("backup byteSize does not match its page geometry")
        raw_counts = value["tableCounts"]
        if type(raw_counts) is not dict:
            raise TypeError("tableCounts must be a plain dictionary")
        counts: Dict[str, int] = {}
        for name, count in raw_counts.items():
            if name not in _CORE_TABLES:
                raise ValueError(f"unsupported table count: {name}")
            if type(count) is not int or count < 0:
                raise ValueError("table counts must be non-negative integers")
            counts[name] = count
        raw_migrations = value["migrations"]
        if type(raw_migrations) is not list:
            raise TypeError("migrations must be a list")
        migrations = []
        previous = 0
        for raw in raw_migrations:
            if type(raw) is not dict or set(raw) != {
                "version",
                "filename",
                "sha256",
                "appliedAt",
            }:
                raise ValueError("malformed migration evidence")
            version = raw["version"]
            if type(version) is not int or version <= previous:
                raise ValueError("migration versions must be positive and ordered")
            checksum = _plain_string(raw["sha256"], "migration sha256")
            if len(checksum) != 64 or re.search(r"[^0-9a-f]", checksum):
                raise ValueError("migration checksum must be lowercase SHA-256 hex")
            filename = _plain_string(raw["filename"], "migration filename")
            if not filename.endswith(".up.sql"):
                raise ValueError("migration filename must end with .up.sql")
            migrations.append(
                {
                    "version": version,
                    "filename": filename,
                    "sha256": checksum,
                    "appliedAt": _normalize_timestamp(
                        _plain_string(raw["appliedAt"], "migration appliedAt")
                    ),
                }
            )
            previous = version
        normalized_migrations = tuple(migrations)
        _validate_migration_evidence(normalized_migrations)
        return cls(
            format_version=_FORMAT,
            backup_id=backup_id,
            created_at=created_at,
            database_sha256=database_sha256,
            byte_size=int(value["byteSize"]),
            page_count=int(value["pageCount"]),
            page_size=int(page_size),
            table_counts=counts,
            migrations=normalized_migrations,
        )


def _verify_evidence(manifest: BackupManifest, evidence: _DatabaseEvidence) -> None:
    if evidence.page_count != manifest.page_count or evidence.page_size != manifest.page_size:
        raise BackupIntegrityError("backup page geometry differs from manifest")
    if dict(evidence.table_counts) != dict(manifest.table_counts):
        raise BackupIntegrityError("backup table counts differ from manifest")
    if tuple(evidence.migrations) != tuple(manifest.migrations):
        raise BackupIntegrityError("backup migration evidence differs from manifest")


def default_manifest_path(backup_path: os.PathLike[str] | str) -> Path:
    path = Path(backup_path)
    return path.with_name(path.name + ".manifest.json")


def _open_regular_readonly(
    path: Path,
    name: str,
    *,
    integrity_error: bool,
) -> Tuple[int, _FileIdentity]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    error_type = BackupIntegrityError if integrity_error else BackupError
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if path.is_symlink():
            raise error_type(f"{name} file must not be a symbolic link") from exc
        raise
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise error_type(f"{name} path must identify a regular file")
        identity = _FileIdentity.from_stat(opened_stat)
        _require_path_identity(
            path,
            identity,
            name,
            integrity_error=integrity_error,
        )
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory(path: Path, name: str) -> Tuple[int, _FileIdentity]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        if path.is_symlink():
            raise BackupError(f"{name} directory must not be a symbolic link") from exc
        raise
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise BackupError(f"{name} parent must identify a directory")
        identity = _FileIdentity.from_stat(opened_stat)
        try:
            current_stat = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BackupError(f"{name} directory changed while it was opened") from exc
        if not stat.S_ISDIR(current_stat.st_mode) or not identity.matches(current_stat):
            raise BackupError(f"{name} directory changed while it was opened")
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _require_path_identity(
    path: Path,
    identity: _FileIdentity,
    name: str,
    *,
    integrity_error: bool,
) -> None:
    error_type = BackupIntegrityError if integrity_error else BackupError
    try:
        current_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise error_type(f"{name} path changed during the operation") from exc
    if not stat.S_ISREG(current_stat.st_mode) or not identity.matches(current_stat):
        raise error_type(f"{name} path changed during the operation")


def _require_directory_identity(
    path: Path,
    identity: _FileIdentity,
    name: str,
) -> None:
    try:
        current_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise BackupError(f"{name} directory changed during the operation") from exc
    if not stat.S_ISDIR(current_stat.st_mode) or not identity.matches(current_stat):
        raise BackupError(f"{name} directory changed during the operation")


def _require_entry_identity(
    directory_descriptor: int,
    filename: str,
    identity: _FileIdentity,
    name: str,
) -> None:
    try:
        current_stat = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise BackupIntegrityError(f"{name} path changed during the operation") from exc
    if not stat.S_ISREG(current_stat.st_mode) or not identity.matches(current_stat):
        raise BackupIntegrityError(f"{name} path changed during the operation")


def _ensure_entry_absent(directory_descriptor: int, filename: str, name: str) -> None:
    try:
        os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise BackupExistsError(f"{name} target already exists")


def _create_owned_temp(
    directory_descriptor: int,
    *,
    prefix: str,
    suffix: str,
) -> Tuple[int, str, _FileIdentity]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(128):
        filename = f"{prefix}{new_id('partial')}{suffix}"
        try:
            descriptor = os.open(
                filename,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        opened_stat = os.fstat(descriptor)
        identity = _FileIdentity.from_stat(opened_stat)
        return descriptor, filename, identity
    raise BackupError("could not allocate a unique temporary file")


def _unlink_owned_entry(
    directory_descriptor: int,
    filename: str,
    identity: _FileIdentity,
) -> bool:
    try:
        current_stat = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(current_stat.st_mode) or not identity.matches(current_stat):
        return False
    os.unlink(filename, dir_fd=directory_descriptor)
    return True


def _fsync_directory_fd(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_sqlite_backup(
    source_path: os.PathLike[str] | str,
    backup_path: os.PathLike[str] | str,
    *,
    manifest_path: Optional[os.PathLike[str] | str] = None,
    clock: Callable[[], str] = utc_now,
) -> BackupManifest:
    """Create a consistent, verified backup without overwriting existing files."""

    if not callable(clock):
        raise TypeError("clock must be callable")
    source = Path(source_path)
    backup = Path(backup_path)
    manifest = Path(manifest_path) if manifest_path is not None else default_manifest_path(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    source_fd = -1
    backup_directory_fd = -1
    manifest_directory_fd = -1
    database_fd = -1
    manifest_fd = -1
    database_temp_name = ""
    manifest_temp_name = ""
    database_identity: Optional[_FileIdentity] = None
    manifest_identity: Optional[_FileIdentity] = None
    database_linked = False
    manifest_linked = False
    try:
        source_fd, source_identity = _open_regular_readonly(
            source,
            "source SQLite database",
            integrity_error=False,
        )
        backup_directory_fd, backup_directory_identity = _open_directory(
            backup.parent,
            "backup",
        )
        manifest_directory_fd, manifest_directory_identity = _open_directory(
            manifest.parent,
            "manifest",
        )
        _ensure_entry_absent(backup_directory_fd, backup.name, "backup")
        _ensure_entry_absent(manifest_directory_fd, manifest.name, "manifest")
        if (
            backup_directory_identity == manifest_directory_identity
            and backup.name == manifest.name
        ):
            raise BackupError("backup and manifest paths must be distinct")

        database_fd, database_temp_name, database_identity = _create_owned_temp(
            backup_directory_fd,
            prefix=f".{backup.name}.",
            suffix=".partial",
        )
        manifest_fd, manifest_temp_name, manifest_identity = _create_owned_temp(
            manifest_directory_fd,
            prefix=f".{manifest.name}.",
            suffix=".partial",
        )
        database_temp = backup.parent / database_temp_name
        os.fchmod(database_fd, 0o600)
        _require_directory_identity(
            backup.parent,
            backup_directory_identity,
            "backup",
        )
        _require_entry_identity(
            backup_directory_fd,
            database_temp_name,
            database_identity,
            "backup temporary file",
        )
        _require_path_identity(
            source,
            source_identity,
            "source SQLite database",
            integrity_error=False,
        )
        source_connection = _read_only_connection(source)
        destination_connection = sqlite3.connect(str(database_temp))
        destination_connection.row_factory = sqlite3.Row
        try:
            _require_path_identity(
                source,
                source_identity,
                "source SQLite database",
                integrity_error=False,
            )
            _require_entry_identity(
                backup_directory_fd,
                database_temp_name,
                database_identity,
                "backup temporary file",
            )
            source_connection.backup(destination_connection, pages=256, sleep=0.01)
            destination_connection.execute("PRAGMA journal_mode=DELETE")
            destination_connection.commit()
            evidence = _database_evidence(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()

        _require_path_identity(
            source,
            source_identity,
            "source SQLite database",
            integrity_error=False,
        )
        _require_directory_identity(
            backup.parent,
            backup_directory_identity,
            "backup",
        )
        _require_entry_identity(
            backup_directory_fd,
            database_temp_name,
            database_identity,
            "backup temporary file",
        )
        os.fsync(database_fd)
        database_stat = os.fstat(database_fd)
        manifest_value = BackupManifest.from_dict(
            BackupManifest(
                format_version=_FORMAT,
                backup_id=new_id("backup"),
                created_at=_normalize_timestamp(clock()),
                database_sha256=_sha256_fd(database_fd),
                byte_size=int(database_stat.st_size),
                page_count=evidence.page_count,
                page_size=evidence.page_size,
                table_counts=evidence.table_counts,
                migrations=evidence.migrations,
            ).to_dict()
        )
        os.fchmod(manifest_fd, 0o600)
        manifest_bytes = (
            json.dumps(
                manifest_value.to_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        _write_all(manifest_fd, manifest_bytes)
        os.fsync(manifest_fd)

        _require_entry_identity(
            backup_directory_fd,
            database_temp_name,
            database_identity,
            "backup temporary file",
        )
        try:
            os.link(
                database_temp_name,
                backup.name,
                src_dir_fd=backup_directory_fd,
                dst_dir_fd=backup_directory_fd,
                follow_symlinks=False,
            )
            database_linked = True
        except FileExistsError as exc:
            raise BackupExistsError(f"backup target already exists: {backup}") from exc
        _require_entry_identity(
            backup_directory_fd,
            backup.name,
            database_identity,
            "published backup",
        )
        _require_entry_identity(
            manifest_directory_fd,
            manifest_temp_name,
            manifest_identity,
            "manifest temporary file",
        )
        try:
            os.link(
                manifest_temp_name,
                manifest.name,
                src_dir_fd=manifest_directory_fd,
                dst_dir_fd=manifest_directory_fd,
                follow_symlinks=False,
            )
            manifest_linked = True
        except FileExistsError as exc:
            raise BackupExistsError(f"manifest target already exists: {manifest}") from exc
        _require_entry_identity(
            manifest_directory_fd,
            manifest.name,
            manifest_identity,
            "published manifest",
        )
        _fsync_directory_fd(backup_directory_fd)
        if backup_directory_identity != manifest_directory_identity:
            _fsync_directory_fd(manifest_directory_fd)
        _require_directory_identity(
            backup.parent,
            backup_directory_identity,
            "backup",
        )
        _require_directory_identity(
            manifest.parent,
            manifest_directory_identity,
            "manifest",
        )
        verified = verify_sqlite_backup(backup, manifest_path=manifest)
        _require_entry_identity(
            backup_directory_fd,
            backup.name,
            database_identity,
            "published backup",
        )
        _require_entry_identity(
            manifest_directory_fd,
            manifest.name,
            manifest_identity,
            "published manifest",
        )
        _require_directory_identity(
            backup.parent,
            backup_directory_identity,
            "backup",
        )
        _require_directory_identity(
            manifest.parent,
            manifest_directory_identity,
            "manifest",
        )
        return verified
    except BaseException:
        if manifest_linked and manifest_identity is not None:
            _unlink_owned_entry(manifest_directory_fd, manifest.name, manifest_identity)
        if database_linked and database_identity is not None:
            _unlink_owned_entry(backup_directory_fd, backup.name, database_identity)
        raise
    finally:
        if manifest_identity is not None:
            _unlink_owned_entry(
                manifest_directory_fd,
                manifest_temp_name,
                manifest_identity,
            )
        if database_identity is not None:
            _unlink_owned_entry(
                backup_directory_fd,
                database_temp_name,
                database_identity,
            )
        if database_fd >= 0:
            os.close(database_fd)
        if manifest_fd >= 0:
            os.close(manifest_fd)
        if source_fd >= 0:
            os.close(source_fd)
        if backup_directory_fd >= 0:
            os.close(backup_directory_fd)
        if manifest_directory_fd >= 0:
            os.close(manifest_directory_fd)


def verify_sqlite_backup(
    backup_path: os.PathLike[str] | str,
    *,
    manifest_path: Optional[os.PathLike[str] | str] = None,
) -> BackupManifest:
    """Verify manifest, file digest, SQLite integrity, schema, and core counts."""

    backup = Path(backup_path)
    manifest = Path(manifest_path) if manifest_path is not None else default_manifest_path(backup)
    for path, name in ((backup, "backup"), (manifest, "manifest")):
        if not path.is_file():
            raise FileNotFoundError(f"{name} file does not exist: {path}")
        if path.is_symlink():
            raise BackupIntegrityError(f"{name} file must not be a symbolic link")
    try:
        with manifest.open("rb") as manifest_handle:
            manifest_bytes = manifest_handle.read(_MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            raise ValueError("backup manifest exceeds the format size limit")
        raw = json.loads(manifest_bytes.decode("utf-8"))
        parsed = BackupManifest.from_dict(cast(Dict[str, Any], raw))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup manifest is malformed") from exc
    if backup.stat().st_size != parsed.byte_size:
        raise BackupIntegrityError("backup byte size differs from manifest")
    if _sha256_file(backup) != parsed.database_sha256:
        raise BackupIntegrityError("backup SHA-256 differs from manifest")
    connection = _read_only_connection(backup)
    try:
        evidence = _database_evidence(connection)
    finally:
        connection.close()
    _verify_evidence(parsed, evidence)
    return parsed


def restore_sqlite_backup(
    backup_path: os.PathLike[str] | str,
    destination_path: os.PathLike[str] | str,
    *,
    manifest_path: Optional[os.PathLike[str] | str] = None,
) -> BackupManifest:
    """Verify and atomically restore a backup without overwriting any destination."""

    backup = Path(backup_path)
    manifest_path_value = (
        Path(manifest_path) if manifest_path is not None else default_manifest_path(backup)
    )
    destination = Path(destination_path)
    if destination.exists() or destination.is_symlink():
        raise BackupExistsError(f"restore destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    if resolved_destination in {backup.resolve(), manifest_path_value.resolve()}:
        raise BackupError("restore destination must differ from backup and manifest")
    verified = verify_sqlite_backup(backup, manifest_path=manifest_path_value)

    destination_fd, destination_temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".restore-partial",
        dir=destination.parent,
    )
    destination_temp = Path(destination_temp_name)
    destination_linked = False
    try:
        os.fchmod(destination_fd, 0o600)
        os.close(destination_fd)
        destination_fd = -1
        source_connection = _read_only_connection(backup)
        destination_connection = sqlite3.connect(str(destination_temp))
        destination_connection.row_factory = sqlite3.Row
        try:
            source_connection.backup(destination_connection, pages=256, sleep=0.01)
            destination_connection.execute("PRAGMA journal_mode=DELETE")
            destination_connection.commit()
            restored_evidence = _database_evidence(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        if _sha256_file(backup) != verified.database_sha256:
            raise BackupIntegrityError("backup changed while it was being restored")
        _verify_evidence(verified, restored_evidence)
        with destination_temp.open("rb") as restored_handle:
            os.fsync(restored_handle.fileno())
        try:
            os.link(destination_temp, destination)
            destination_linked = True
        except FileExistsError as exc:
            raise BackupExistsError(f"restore destination already exists: {destination}") from exc
        _fsync_directory(destination.parent)
        restored_connection = _read_only_connection(destination)
        try:
            _verify_evidence(verified, _database_evidence(restored_connection))
        finally:
            restored_connection.close()
        return verified
    except BaseException:
        if destination_linked:
            destination.unlink()
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        destination_temp.unlink(missing_ok=True)


__all__ = [
    "BackupError",
    "BackupExistsError",
    "BackupIntegrityError",
    "BackupManifest",
    "create_sqlite_backup",
    "default_manifest_path",
    "restore_sqlite_backup",
    "verify_sqlite_backup",
]
