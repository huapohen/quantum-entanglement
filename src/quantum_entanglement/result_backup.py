"""Explicit migration-7 SQLite backup and restore boundary.

The legacy backup API remains feature-off for migration 7.  This module is an opt-in,
result-specific path: it creates a consistent SQLite backup, binds the bytes to exact
active-topology evidence, and verifies a restored copy before returning.  It never starts
workers, publishes an outbox message, connects to an IM, or reads credentials.
"""

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
from typing import Any, Callable

from .backup_topology import TrustedBackupSchemaObject
from .protocol import new_id, utc_now
from .result_backup_topology import (
    RESULT_BACKUP_SCHEMA_VERSION,
    RESULT_BACKUP_TOPOLOGY_FORMAT,
    ResultBackupTableCount,
    ResultBackupTopologyError,
    ResultBackupTopologyEvidence,
    derive_result_backup_topology,
)

RESULT_BACKUP_FORMAT = "qe.result-backup/1"
MAX_RESULT_BACKUP_MANIFEST_BYTES = 1024 * 1024
_BACKUP_ID_PATTERN = re.compile(r"backup_[0-9a-f]{32}\Z")
_SHA256_HEX = frozenset("0123456789abcdef")


class ResultBackupError(RuntimeError):
    """Base error for migration-7 backup and restore operations."""


class ResultBackupExistsError(ResultBackupError):
    """Raised when a backup, manifest or restore target already exists."""


class ResultBackupIntegrityError(ResultBackupError):
    """Raised when bytes, manifest evidence or restored topology differ."""


def _canonical_timestamp(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical UTC timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    normalized = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if normalized != value:
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in _SHA256_HEX for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_exact_dict(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields are not exact")
    return value


def _parse_topology(value: object) -> ResultBackupTopologyEvidence:
    raw = _require_exact_dict(
        value,
        {
            "format",
            "migrationStateSha256",
            "presentProfiles",
            "resultRegistrySha256",
            "schemaObjects",
            "schemaVersion",
            "tableCounts",
            "topologyRegistrySha256",
            "topologySha256",
        },
        "result backup topology",
    )
    if raw["format"] != RESULT_BACKUP_TOPOLOGY_FORMAT:
        raise ValueError("result backup topology format is unsupported")
    if type(raw["presentProfiles"]) is not list:
        raise ValueError("result backup topology profiles must be a list")
    if type(raw["schemaObjects"]) is not list:
        raise ValueError("result backup topology schema objects must be a list")
    objects: list[TrustedBackupSchemaObject] = []
    for item in raw["schemaObjects"]:
        object_value = _require_exact_dict(
            item,
            {"ddlSha256", "name", "objectType", "owner", "profile", "tableName"},
            "result backup schema object",
        )
        objects.append(
            TrustedBackupSchemaObject(
                profile=object_value["profile"],
                owner=object_value["owner"],
                object_type=object_value["objectType"],
                name=object_value["name"],
                table_name=object_value["tableName"],
                ddl_sha256=object_value["ddlSha256"],
            )
        )
    if type(raw["tableCounts"]) is not list:
        raise ValueError("result backup table counts must be a list")
    counts: list[ResultBackupTableCount] = []
    for item in raw["tableCounts"]:
        count_value = _require_exact_dict(item, {"name", "rowCount"}, "result backup table count")
        counts.append(ResultBackupTableCount(count_value["name"], count_value["rowCount"]))
    topology = ResultBackupTopologyEvidence(
        schema_version=raw["schemaVersion"],
        migration_state_sha256=raw["migrationStateSha256"],
        result_registry_sha256=raw["resultRegistrySha256"],
        topology_registry_sha256=raw["topologyRegistrySha256"],
        present_profiles=tuple(raw["presentProfiles"]),
        schema_objects=tuple(objects),
        table_counts=tuple(counts),
    )
    if raw["topologySha256"] != topology.topology_sha256:
        raise ValueError("result backup topology digest differs from canonical evidence")
    return topology


@dataclass(frozen=True)
class ResultBackupManifest:
    """Canonical manifest binding one backup file to active migration-7 topology."""

    backup_id: str
    created_at: str
    database_sha256: str
    byte_size: int
    page_count: int
    page_size: int
    topology: ResultBackupTopologyEvidence

    def __post_init__(self) -> None:
        if type(self.backup_id) is not str or _BACKUP_ID_PATTERN.fullmatch(self.backup_id) is None:
            raise ValueError("result backup ID is malformed")
        _canonical_timestamp(self.created_at, "result backup createdAt")
        _digest(self.database_sha256, "result backup database digest")
        if type(self.byte_size) is not int or self.byte_size <= 0:
            raise ValueError("result backup byte size is malformed")
        if type(self.page_count) is not int or self.page_count <= 0:
            raise ValueError("result backup page count is malformed")
        if (
            type(self.page_size) is not int
            or self.page_size < 512
            or self.page_size > 65_536
            or self.page_size & (self.page_size - 1)
        ):
            raise ValueError("result backup page size is unsupported")
        if self.byte_size != self.page_count * self.page_size:
            raise ValueError("result backup byte geometry differs")
        if type(self.topology) is not ResultBackupTopologyEvidence:
            raise TypeError("result backup topology must be exact evidence")
        if self.topology.schema_version != RESULT_BACKUP_SCHEMA_VERSION:
            raise ValueError("result backup topology schema version differs")

    def to_dict(self) -> dict[str, object]:
        return {
            "backupId": self.backup_id,
            "byteSize": self.byte_size,
            "createdAt": self.created_at,
            "databaseSha256": self.database_sha256,
            "format": RESULT_BACKUP_FORMAT,
            "pageCount": self.page_count,
            "pageSize": self.page_size,
            "topology": self.topology.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        stable_topology = ResultBackupTopologyEvidence(
            schema_version=self.topology.schema_version,
            migration_state_sha256=self.topology.migration_state_sha256,
            result_registry_sha256=self.topology.result_registry_sha256,
            topology_registry_sha256=self.topology.topology_registry_sha256,
            present_profiles=self.topology.present_profiles,
            schema_objects=self.topology.schema_objects,
            table_counts=self.topology.table_counts,
        )
        if stable_topology != self.topology:
            raise ValueError("result backup topology differs from its canonical snapshot")
        stable = ResultBackupManifest(
            backup_id=self.backup_id,
            created_at=self.created_at,
            database_sha256=self.database_sha256,
            byte_size=self.byte_size,
            page_count=self.page_count,
            page_size=self.page_size,
            topology=stable_topology,
        )
        encoded = _canonical_json(stable.to_dict())
        if len(encoded) > MAX_RESULT_BACKUP_MANIFEST_BYTES:
            raise ValueError("result backup manifest exceeds the size limit")
        return encoded

    @classmethod
    def from_dict(cls, value: object) -> ResultBackupManifest:
        raw = _require_exact_dict(
            value,
            {
                "backupId",
                "byteSize",
                "createdAt",
                "databaseSha256",
                "format",
                "pageCount",
                "pageSize",
                "topology",
            },
            "result backup manifest",
        )
        if raw["format"] != RESULT_BACKUP_FORMAT:
            raise ValueError("result backup manifest format is unsupported")
        return cls(
            backup_id=raw["backupId"],
            created_at=raw["createdAt"],
            database_sha256=raw["databaseSha256"],
            byte_size=raw["byteSize"],
            page_count=raw["pageCount"],
            page_size=raw["pageSize"],
            topology=_parse_topology(raw["topology"]),
        )

    @classmethod
    def from_json_bytes(cls, value: object) -> ResultBackupManifest:
        if type(value) is not bytes or not value or len(value) > MAX_RESULT_BACKUP_MANIFEST_BYTES:
            raise ValueError("result backup manifest bytes are malformed")
        try:
            decoded = json.loads(
                value.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
                parse_float=_reject_float,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("result backup manifest JSON is malformed") from error
        manifest = cls.from_dict(decoded)
        if manifest.to_json_bytes() != value:
            raise ValueError("result backup manifest JSON is not canonical")
        return manifest


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("result backup manifest contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"result backup manifest contains unsupported constant {value}")


def _reject_float(_value: str) -> None:
    raise ValueError("result backup manifest does not accept decimal values")


def _regular_path(path: Path, label: str, *, must_exist: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise ResultBackupIntegrityError(f"{label} does not exist") from None
        return
    if stat.S_ISLNK(info.st_mode):
        raise ResultBackupIntegrityError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise ResultBackupIntegrityError(f"{label} must be a regular file")


def _target_absent(path: Path, label: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ResultBackupExistsError(f"{label} already exists")


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return total, digest.hexdigest()


def _page_geometry(connection: sqlite3.Connection) -> tuple[int, int]:
    page_count = connection.execute("PRAGMA main.page_count").fetchone()[0]
    page_size = connection.execute("PRAGMA main.page_size").fetchone()[0]
    if type(page_count) is not int or type(page_size) is not int:
        raise ResultBackupIntegrityError("result backup page geometry is malformed")
    return page_count, page_size


def _open_readonly(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ResultBackupIntegrityError("result backup SQLite file could not be opened") from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as error:
        connection.close()
        raise ResultBackupIntegrityError("result backup SQLite configuration failed") from error
    return connection


def _read_manifest(path: Path) -> ResultBackupManifest:
    _regular_path(path, "result backup manifest", must_exist=True)
    try:
        payload = path.read_bytes()
        return ResultBackupManifest.from_json_bytes(payload)
    except (OSError, TypeError, ValueError) as error:
        if isinstance(error, ResultBackupError):
            raise
        raise ResultBackupIntegrityError("result backup manifest is not canonical") from error


def _compare_topology(
    expected: ResultBackupTopologyEvidence, actual: ResultBackupTopologyEvidence
) -> None:
    if expected != actual:
        raise ResultBackupIntegrityError("result backup topology differs from manifest")


def _verify_file(path: Path, manifest: ResultBackupManifest) -> None:
    _regular_path(path, "result backup database", must_exist=True)
    byte_size, digest = _file_digest(path)
    if byte_size != manifest.byte_size or digest != manifest.database_sha256:
        raise ResultBackupIntegrityError("result backup database bytes differ from manifest")
    connection = _open_readonly(path)
    try:
        actual_page_count, actual_page_size = _page_geometry(connection)
        if (actual_page_count, actual_page_size) != (manifest.page_count, manifest.page_size):
            raise ResultBackupIntegrityError("result backup page geometry differs from manifest")
        try:
            actual_topology = derive_result_backup_topology(connection)
        except (ResultBackupTopologyError, TypeError, ValueError) as error:
            raise ResultBackupIntegrityError("result backup topology cannot be verified") from error
        _compare_topology(manifest.topology, actual_topology)
    finally:
        connection.close()


def _publish_link(source: Path, target: Path, label: str) -> None:
    _target_absent(target, label)
    try:
        os.link(source, target)
    except FileExistsError:
        raise ResultBackupExistsError(f"{label} already exists") from None
    except OSError as error:
        raise ResultBackupError(f"{label} could not be published") from error


def _remove_owned(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _copy_file(source: Path, target_directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=target_directory)
    os.close(descriptor)
    temporary = target_directory / name
    try:
        with source.open("rb") as source_stream, temporary.open("wb") as target_stream:
            while True:
                block = source_stream.read(1024 * 1024)
                if not block:
                    break
                target_stream.write(block)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        return temporary
    except BaseException:
        _remove_owned(temporary)
        raise


def create_result_backup(
    source_path: os.PathLike[str] | str,
    backup_path: os.PathLike[str] | str,
    *,
    manifest_path: os.PathLike[str] | str | None = None,
    clock: Callable[[], str] = utc_now,
) -> ResultBackupManifest:
    """Create and verify one active migration-7 backup without overwriting targets."""

    if not callable(clock):
        raise TypeError("result backup clock must be callable")
    source = Path(source_path)
    backup = Path(backup_path)
    manifest = (
        Path(manifest_path) if manifest_path is not None else Path(str(backup) + ".manifest.json")
    )
    _regular_path(source, "result backup source", must_exist=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    _target_absent(backup, "result backup")
    _target_absent(manifest, "result backup manifest")
    source_connection = _open_readonly(source)
    backup_temporary: Path | None = None
    manifest_temporary: Path | None = None
    published_manifest = False
    published_backup = False
    try:
        try:
            source_topology = derive_result_backup_topology(source_connection)
        except (ResultBackupTopologyError, TypeError, ValueError) as error:
            raise ResultBackupIntegrityError(
                "result backup source is not active migration 7"
            ) from error
        descriptor, name = tempfile.mkstemp(prefix=".qe-result-backup-", dir=backup.parent)
        os.close(descriptor)
        backup_temporary = backup.parent / name
        destination_connection = sqlite3.connect(backup_temporary)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        except sqlite3.Error as error:
            raise ResultBackupError("result backup SQLite copy failed") from error
        finally:
            destination_connection.close()
        byte_size, digest = _file_digest(backup_temporary)
        backup_connection = _open_readonly(backup_temporary)
        try:
            page_count, page_size = _page_geometry(backup_connection)
            copied_topology = derive_result_backup_topology(backup_connection)
        except (ResultBackupTopologyError, TypeError, ValueError) as error:
            raise ResultBackupIntegrityError("result backup copy topology is not exact") from error
        finally:
            backup_connection.close()
        _compare_topology(source_topology, copied_topology)
        created_at = _canonical_timestamp(clock(), "result backup clock")
        result = ResultBackupManifest(
            backup_id=new_id("backup"),
            created_at=created_at,
            database_sha256=digest,
            byte_size=byte_size,
            page_count=page_count,
            page_size=page_size,
            topology=copied_topology,
        )
        descriptor, name = tempfile.mkstemp(prefix=".qe-result-manifest-", dir=manifest.parent)
        os.close(descriptor)
        manifest_temporary = manifest.parent / name
        with manifest_temporary.open("wb") as stream:
            stream.write(result.to_json_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        _publish_link(manifest_temporary, manifest, "result backup manifest")
        published_manifest = True
        _publish_link(backup_temporary, backup, "result backup")
        published_backup = True
        _verify_file(backup, result)
        return result
    except BaseException:
        if published_manifest:
            _remove_owned(manifest)
        if published_backup:
            _remove_owned(backup)
        raise
    finally:
        source_connection.close()
        if backup_temporary is not None:
            _remove_owned(backup_temporary)
        if manifest_temporary is not None:
            _remove_owned(manifest_temporary)


def verify_result_backup(
    backup_path: os.PathLike[str] | str,
    *,
    manifest_path: os.PathLike[str] | str | None = None,
) -> ResultBackupManifest:
    """Verify one active migration-7 backup and return its canonical manifest."""

    backup = Path(backup_path)
    manifest = (
        Path(manifest_path) if manifest_path is not None else Path(str(backup) + ".manifest.json")
    )
    result = _read_manifest(manifest)
    _verify_file(backup, result)
    return result


def restore_result_backup(
    backup_path: os.PathLike[str] | str,
    destination_path: os.PathLike[str] | str,
    *,
    manifest_path: os.PathLike[str] | str | None = None,
) -> ResultBackupManifest:
    """Restore a verified backup to a new file and verify the restored topology before return."""

    backup = Path(backup_path)
    destination = Path(destination_path)
    manifest = verify_result_backup(backup, manifest_path=manifest_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _target_absent(destination, "result backup restore target")
    temporary: Path | None = None
    try:
        temporary = _copy_file(backup, destination.parent, ".qe-result-restore-")
        _publish_link(temporary, destination, "result backup restore target")
        _verify_file(destination, manifest)
        return manifest
    except BaseException:
        if destination.exists() and temporary is not None:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    finally:
        if temporary is not None:
            _remove_owned(temporary)


__all__ = [
    "MAX_RESULT_BACKUP_MANIFEST_BYTES",
    "RESULT_BACKUP_FORMAT",
    "ResultBackupError",
    "ResultBackupExistsError",
    "ResultBackupIntegrityError",
    "ResultBackupManifest",
    "create_result_backup",
    "restore_result_backup",
    "verify_result_backup",
]
