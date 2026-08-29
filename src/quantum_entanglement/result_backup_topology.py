"""Active migration-7 topology evidence for result backups.

The legacy backup registry intentionally stops at migration 6.  This module provides a
separate, explicit evidence boundary for an opt-in migration-7 database without changing
the legacy v1/v2 backup contracts.  It reads one caller-owned SQLite connection, validates
the active result migration and exact catalog, and returns immutable topology evidence.
It never opens a path, writes a database, or publishes a manifest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from ._inactive_invocation_results_backup_topology import (
    _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY,
)
from .backup_topology import TrustedBackupSchemaObject, backup_schema_ddl_sha256
from .result_migration_activation import (
    RESULT_ACCEPTANCE_DOMAIN_REGISTRY,
    RESULT_ACCEPTANCE_MIGRATION,
    ResultAcceptanceMigrationError,
    ResultAcceptanceMigrationState,
    read_result_acceptance_migration_state,
)

RESULT_BACKUP_TOPOLOGY_FORMAT = "qe.result-backup-topology/1"
RESULT_BACKUP_TOPOLOGY_PROFILE = "qe.domain-migration-0007/1"
RESULT_BACKUP_SCHEMA_VERSION = RESULT_ACCEPTANCE_MIGRATION.version

_TOPOLOGY_REGISTRY = _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY
_MAX_PROFILES = 64
_MAX_OBJECTS = 512
_MAX_TABLES = 128
_MAX_NAME_LENGTH = 128
_SHA256_HEX = frozenset("0123456789abcdef")
_SQLITE_STAT1_NAME = "sqlite_stat1"
_SQLITE_STAT1_DDL_SHA256 = backup_schema_ddl_sha256("CREATE TABLE sqlite_stat1(tbl,idx,stat)")


class ResultBackupTopologyError(RuntimeError):
    """Raised when an active migration-7 catalog cannot be trusted for backup."""


@dataclass(frozen=True, order=True)
class ResultBackupTableCount:
    name: str
    row_count: int

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name or len(self.name) > _MAX_NAME_LENGTH:
            raise ValueError("result backup table name is malformed")
        if type(self.row_count) is not int or self.row_count < 0:
            raise ValueError("result backup table row count is malformed")

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "rowCount": self.row_count}


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in _SHA256_HEX for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_unique_strings(values: Iterable[object], label: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if type(value) is not str or not value or len(value) > _MAX_NAME_LENGTH:
            raise ResultBackupTopologyError(f"{label} contains a malformed value")
        result.append(value)
    snapshot = tuple(result)
    if len(snapshot) > _MAX_PROFILES or len(set(snapshot)) != len(snapshot):
        raise ResultBackupTopologyError(f"{label} contains duplicates or exceeds its bound")
    if snapshot != tuple(sorted(snapshot, key=lambda item: item.encode("utf-8"))):
        raise ResultBackupTopologyError(f"{label} is not in canonical order")
    return snapshot


def _object_dict(item: TrustedBackupSchemaObject) -> dict[str, object]:
    return item.to_dict()


@dataclass(frozen=True)
class ResultBackupTopologyEvidence:
    """Immutable, canonical topology evidence for an active result database."""

    schema_version: int
    migration_state_sha256: str
    result_registry_sha256: str
    topology_registry_sha256: str
    present_profiles: tuple[str, ...]
    schema_objects: tuple[TrustedBackupSchemaObject, ...]
    table_counts: tuple[ResultBackupTableCount, ...]
    topology_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != RESULT_BACKUP_SCHEMA_VERSION
        ):
            raise ValueError("result backup schema version is unsupported")
        _sha256(self.migration_state_sha256, "migration state digest")
        if self.result_registry_sha256 != RESULT_ACCEPTANCE_DOMAIN_REGISTRY.registry_sha256:
            raise ValueError("result migration registry digest differs from the trusted registry")
        if self.topology_registry_sha256 != _TOPOLOGY_REGISTRY.registry_sha256:
            raise ValueError(
                "result backup topology registry digest differs from the trusted registry"
            )
        profiles = _ordered_unique_strings(self.present_profiles, "result backup profiles")
        objects = tuple(self.schema_objects)
        if len(objects) > _MAX_OBJECTS:
            raise ValueError("result backup schema objects exceed the hard limit")
        for item in objects:
            if type(item) is not TrustedBackupSchemaObject:
                raise TypeError("result backup schema objects must be exact trusted objects")
        expected_objects = _TOPOLOGY_REGISTRY.objects_for_profiles(profiles)
        if objects != expected_objects:
            raise ValueError("result backup schema objects differ from the trusted topology")
        counts = tuple(self.table_counts)
        if len(counts) > _MAX_TABLES:
            raise ValueError("result backup table counts exceed the hard limit")
        if counts != tuple(sorted(counts, key=lambda item: item.name.encode("utf-8"))):
            raise ValueError("result backup table counts are not in canonical order")
        table_names = tuple(item.name for item in objects if item.object_type == "table")
        if tuple(item.name for item in counts) != table_names:
            raise ValueError("result backup table counts do not match the exact catalog")
        object.__setattr__(self, "present_profiles", profiles)
        object.__setattr__(self, "schema_objects", objects)
        object.__setattr__(self, "table_counts", counts)
        expected_digest = _canonical_digest(self._digest_dict())
        object.__setattr__(self, "topology_sha256", expected_digest)

    def _digest_dict(self) -> dict[str, object]:
        return {
            "format": RESULT_BACKUP_TOPOLOGY_FORMAT,
            "migrationStateSha256": self.migration_state_sha256,
            "presentProfiles": list(self.present_profiles),
            "resultRegistrySha256": self.result_registry_sha256,
            "schemaObjects": [_object_dict(item) for item in self.schema_objects],
            "schemaVersion": self.schema_version,
            "tableCounts": [item.to_dict() for item in self.table_counts],
            "topologyRegistrySha256": self.topology_registry_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        value = dict(self._digest_dict())
        value["topologySha256"] = self.topology_sha256
        return value


@dataclass(frozen=True)
class _CatalogObject:
    object_type: str
    name: str
    table_name: str
    ddl_sha256: str | None


def _validate_integrity(connection: sqlite3.Connection) -> None:
    try:
        integrity_rows = connection.execute("PRAGMA main.integrity_check(1)").fetchall()
        foreign_key_rows = connection.execute("PRAGMA main.foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise ResultBackupTopologyError(
            "result backup integrity check could not be read"
        ) from error
    if len(integrity_rows) != 1 or integrity_rows[0][0] != "ok":
        raise ResultBackupTopologyError("result backup integrity check failed")
    if foreign_key_rows:
        raise ResultBackupTopologyError("result backup foreign key check failed")


def _read_catalog(connection: sqlite3.Connection) -> tuple[_CatalogObject, ...]:
    expected_objects = _TOPOLOGY_REGISTRY.objects_for_profiles(
        tuple(profile.name for profile in _TOPOLOGY_REGISTRY.profiles)
    )
    try:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM main.sqlite_schema
            ORDER BY type, name, tbl_name
            """
        ).fetchall()
    except sqlite3.Error as error:
        raise ResultBackupTopologyError("result backup schema catalog could not be read") from error
    if len(rows) > len(expected_objects) + 2:
        raise ResultBackupTopologyError("result backup schema catalog exceeds the trusted bound")
    catalog: list[_CatalogObject] = []
    coordinates: set[tuple[str, str]] = set()
    for row in rows:
        if type(row) is not sqlite3.Row or len(row) != 4:
            raise ResultBackupTopologyError("result backup schema catalog row is malformed")
        object_type, name, table_name, schema_sql = tuple(row)
        if type(object_type) is not str or type(name) is not str or type(table_name) is not str:
            raise ResultBackupTopologyError("result backup schema catalog scalar is malformed")
        if not name or len(name) > _MAX_NAME_LENGTH or not table_name:
            raise ResultBackupTopologyError("result backup schema catalog name is malformed")
        if name.startswith("sqlite_stat"):
            if (
                object_type != "table"
                or name != _SQLITE_STAT1_NAME
                or table_name != _SQLITE_STAT1_NAME
                or type(schema_sql) is not str
                or backup_schema_ddl_sha256(schema_sql) != _SQLITE_STAT1_DDL_SHA256
            ):
                raise ResultBackupTopologyError("result backup statistics object is unsupported")
            continue
        if schema_sql is not None and type(schema_sql) is not str:
            raise ResultBackupTopologyError("result backup schema SQL is malformed")
        coordinate = (object_type, name)
        if coordinate in coordinates:
            raise ResultBackupTopologyError(
                "result backup schema catalog has duplicate coordinates"
            )
        coordinates.add(coordinate)
        try:
            ddl_sha256 = None if schema_sql is None else backup_schema_ddl_sha256(schema_sql)
        except (TypeError, ValueError) as error:
            raise ResultBackupTopologyError("result backup schema SQL is malformed") from error
        catalog.append(_CatalogObject(object_type, name, table_name, ddl_sha256))
    return tuple(catalog)


def _classify_profiles(catalog: tuple[_CatalogObject, ...]) -> tuple[str, ...]:
    trusted = {
        (item.object_type, item.name): item
        for profile in _TOPOLOGY_REGISTRY.profiles
        for item in profile.objects
    }
    actual: set[tuple[str, str]] = set()
    for item in catalog:
        expected = trusted.get((item.object_type, item.name))
        if expected is None:
            raise ResultBackupTopologyError(
                "result backup schema catalog contains an unknown object"
            )
        if item.table_name != expected.table_name or item.ddl_sha256 != expected.ddl_sha256:
            raise ResultBackupTopologyError("result backup schema catalog object drifted")
        actual.add((item.object_type, item.name))
    present: list[str] = []
    for profile in _TOPOLOGY_REGISTRY.profiles:
        expected_coordinates = {(item.object_type, item.name) for item in profile.objects}
        observed = expected_coordinates & actual
        if observed and observed != expected_coordinates:
            raise ResultBackupTopologyError(
                "result backup schema catalog contains a partial profile"
            )
        if observed:
            present.append(profile.name)
    profile_names = tuple(sorted(present, key=lambda item: item.encode("utf-8")))
    expected_present = {
        (item.object_type, item.name)
        for profile in _TOPOLOGY_REGISTRY.profiles
        if profile.name in profile_names
        for item in profile.objects
    }
    if actual != expected_present:
        raise ResultBackupTopologyError("result backup schema catalog classification failed")
    if RESULT_BACKUP_TOPOLOGY_PROFILE not in profile_names:
        raise ResultBackupTopologyError("result backup migration-7 profile is not active")
    for name in profile_names:
        profile = _TOPOLOGY_REGISTRY.profile(name)
        if not set(profile.dependencies).issubset(profile_names):
            raise ResultBackupTopologyError("result backup profile dependency is missing")
    return profile_names


def _read_table_counts(
    connection: sqlite3.Connection,
    profile_names: tuple[str, ...],
) -> tuple[ResultBackupTableCount, ...]:
    objects = _TOPOLOGY_REGISTRY.objects_for_profiles(profile_names)
    table_names = tuple(item.name for item in objects if item.object_type == "table")
    counts: list[ResultBackupTableCount] = []
    for name in table_names:
        try:
            rows = connection.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchall()
        except sqlite3.Error as error:
            raise ResultBackupTopologyError(
                "result backup table count could not be read"
            ) from error
        if len(rows) != 1 or type(rows[0][0]) is not int:
            raise ResultBackupTopologyError("result backup table count is malformed")
        counts.append(ResultBackupTableCount(name, rows[0][0]))
    return tuple(counts)


def _build_evidence(
    state: ResultAcceptanceMigrationState,
    profile_names: tuple[str, ...],
    table_counts: tuple[ResultBackupTableCount, ...],
) -> ResultBackupTopologyEvidence:
    objects = _TOPOLOGY_REGISTRY.objects_for_profiles(profile_names)
    return ResultBackupTopologyEvidence(
        schema_version=state.schema_version,
        migration_state_sha256=state.state_sha256,
        result_registry_sha256=state.registry_sha256,
        topology_registry_sha256=_TOPOLOGY_REGISTRY.registry_sha256,
        present_profiles=profile_names,
        schema_objects=objects,
        table_counts=table_counts,
    )


def derive_result_backup_topology(connection: object) -> ResultBackupTopologyEvidence:
    """Read exact active migration-7 topology evidence in one owned transaction.

    The supplied connection must be fresh and exact, with no caller transaction active.  The
    function opens and rolls back its own read transaction; it performs no DML and leaves the
    connection outside a transaction on both success and recognized failure.
    """

    if type(connection) is not sqlite3.Connection:
        raise TypeError("result backup topology requires an exact sqlite3.Connection")
    typed_connection = connection
    if typed_connection.in_transaction:
        raise ResultBackupTopologyError(
            "result backup topology requires no active caller transaction"
        )
    started = False
    try:
        typed_connection.execute("BEGIN")
        started = typed_connection.in_transaction
        if not started:
            raise ResultBackupTopologyError("result backup topology transaction did not start")
        _validate_integrity(typed_connection)
        # Classify the raw catalog before reading the migration ledger so a dropped
        # migration-7 index is reported as a topology partial rather than being hidden
        # behind the generic active-schema validator.
        profile_names = _classify_profiles(_read_catalog(typed_connection))
        state = read_result_acceptance_migration_state(typed_connection)
        counts = _read_table_counts(typed_connection, profile_names)
        return _build_evidence(state, profile_names, counts)
    except ResultBackupTopologyError:
        raise
    except ResultAcceptanceMigrationError as error:
        raise ResultBackupTopologyError("result backup migration state is not active") from error
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise ResultBackupTopologyError("result backup topology evidence is not exact") from error
    finally:
        if started and typed_connection.in_transaction:
            try:
                typed_connection.execute("ROLLBACK")
            except sqlite3.Error as error:
                raise ResultBackupTopologyError("result backup topology rollback failed") from error
        if typed_connection.in_transaction:
            raise ResultBackupTopologyError("result backup topology transaction remained active")


__all__ = [
    "RESULT_BACKUP_SCHEMA_VERSION",
    "RESULT_BACKUP_TOPOLOGY_FORMAT",
    "RESULT_BACKUP_TOPOLOGY_PROFILE",
    "ResultBackupTableCount",
    "ResultBackupTopologyError",
    "ResultBackupTopologyEvidence",
    "derive_result_backup_topology",
]
