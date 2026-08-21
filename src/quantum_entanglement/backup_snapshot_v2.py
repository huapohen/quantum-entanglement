# ruff: noqa: UP006, UP035, UP045
"""Single-transaction SQLite evidence derivation for inactive backup manifest v2.

This module reads an exact ``sqlite3.Connection`` supplied by a future descriptor-owning
backup boundary. It does not open paths, write files, publish manifests, migrate schema,
or make v2 reachable from the active v1 backup APIs.
"""

from __future__ import annotations

import sqlite3
import sys
from asyncio import CancelledError
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Dict, List, NoReturn, Optional, Set, Tuple, cast

from .backup_manifest_v2 import (
    BACKUP_MANIFEST_V2_FORMAT,
    BackupManifestV2,
    BackupManifestV2RegistryTopology,
    BackupManifestV2SchemaState,
)
from .backup_topology import (
    BACKUP_TOPOLOGY_REGISTRY,
    TrustedBackupSchemaObject,
    backup_schema_ddl_sha256,
)
from .domain_migrations import (
    MAX_DOMAIN_MIGRATIONS,
    DomainMigrationBridgeIntegrityError,
    inspect_schema_state,
)

_MAX_SQLITE_PAGE_COUNT = (2**32) - 2
_MAX_SQLITE_PAGE_SIZE = 65_536
_MAX_SQLITE_ROW_COUNT = (2**63) - 1
_CONTROL_SIGNAL_TYPES = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
_SQLITE_STAT1_NAME = "sqlite_stat1"
_SQLITE_STAT1_DDL_SHA256 = backup_schema_ddl_sha256("CREATE TABLE sqlite_stat1(tbl,idx,stat)")


class BackupManifestV2SnapshotError(RuntimeError):
    """Stable error raised when one exact v2 evidence snapshot cannot be derived."""


@dataclass(frozen=True)
class BackupManifestV2Snapshot:
    """Manifest-ready evidence observed inside one SQLite read transaction."""

    page_count: int
    page_size: int
    schema_state: BackupManifestV2SchemaState
    registry_topology: BackupManifestV2RegistryTopology

    def __post_init__(self) -> None:
        if type(self.page_count) is not int or not 0 < self.page_count <= _MAX_SQLITE_PAGE_COUNT:
            raise ValueError("snapshot page count is outside the supported SQLite range")
        if (
            type(self.page_size) is not int
            or self.page_size < 512
            or self.page_size > _MAX_SQLITE_PAGE_SIZE
            or self.page_size & (self.page_size - 1)
        ):
            raise ValueError("snapshot page size is not supported by SQLite")
        if type(self.schema_state) is not BackupManifestV2SchemaState:
            raise TypeError("snapshot schema state must be exact manifest v2 evidence")
        if type(self.registry_topology) is not BackupManifestV2RegistryTopology:
            raise TypeError("snapshot registry topology must be exact manifest v2 evidence")
        BackupManifestV2(
            format_version=BACKUP_MANIFEST_V2_FORMAT,
            backup_id="backup_" + ("0" * 32),
            created_at="1970-01-01T00:00:00.000000Z",
            database_sha256="0" * 64,
            byte_size=self.page_count * self.page_size,
            page_count=self.page_count,
            page_size=self.page_size,
            schema_state=self.schema_state,
            registry_topology=self.registry_topology,
        )


@dataclass(frozen=True)
class _CatalogObject:
    object_type: str
    name: str
    table_name: str
    ddl_sha256: Optional[str]


class _SnapshotFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _raise_snapshot_error(code: str) -> NoReturn:
    try:
        raise BackupManifestV2SnapshotError(code) from None
    except BackupManifestV2SnapshotError as error:
        error.__context__ = None
        raise


def _exact_row(raw_row: object, *, columns: int, label: str) -> Tuple[object, ...]:
    try:
        iterator = iter(cast(Iterable[object], raw_row))
        values: Tuple[object, ...] = tuple(islice(iterator, columns + 1))
    except (TypeError, ValueError) as error:
        raise _SnapshotFailure(f"{label}_malformed") from error
    if len(values) != columns:
        raise _SnapshotFailure(f"{label}_malformed")
    return values


def _exact_text(value: object, *, label: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise _SnapshotFailure(f"{label}_malformed")
    return value


def _exact_positive_integer(value: object, *, label: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise _SnapshotFailure(f"{label}_malformed")
    return value


def _fetch_bounded(
    cursor: sqlite3.Cursor,
    *,
    maximum: int,
    label: str,
) -> Tuple[object, ...]:
    try:
        rows = cursor.fetchmany(maximum + 1)
    except sqlite3.Error as error:
        raise _SnapshotFailure(f"{label}_read_failed") from error
    if len(rows) > maximum:
        raise _SnapshotFailure(f"{label}_limit_exceeded")
    return tuple(rows)


def _execute(connection: sqlite3.Connection, statement: str, *, label: str) -> sqlite3.Cursor:
    try:
        return connection.execute(statement)
    except sqlite3.Error as error:
        raise _SnapshotFailure(f"{label}_read_failed") from error


def _validate_integrity(connection: sqlite3.Connection) -> None:
    integrity_rows = _fetch_bounded(
        _execute(
            connection,
            "PRAGMA main.integrity_check(1)",
            label="integrity_check",
        ),
        maximum=1,
        label="integrity_check",
    )
    if len(integrity_rows) != 1:
        raise _SnapshotFailure("integrity_check_failed")
    integrity = _exact_row(integrity_rows[0], columns=1, label="integrity_check_row")
    if type(integrity[0]) is not str or integrity[0] != "ok":
        raise _SnapshotFailure("integrity_check_failed")

    foreign_key_rows = _fetch_bounded(
        _execute(
            connection,
            "PRAGMA main.foreign_key_check",
            label="foreign_key_check",
        ),
        maximum=1,
        label="foreign_key_check",
    )
    if foreign_key_rows:
        raise _SnapshotFailure("foreign_key_check_failed")


def _read_page_geometry(connection: sqlite3.Connection) -> Tuple[int, int]:
    page_count_rows = _fetch_bounded(
        _execute(connection, "PRAGMA main.page_count", label="page_count"),
        maximum=1,
        label="page_count",
    )
    page_size_rows = _fetch_bounded(
        _execute(connection, "PRAGMA main.page_size", label="page_size"),
        maximum=1,
        label="page_size",
    )
    if len(page_count_rows) != 1 or len(page_size_rows) != 1:
        raise _SnapshotFailure("page_geometry_malformed")
    page_count_value = _exact_row(
        page_count_rows[0],
        columns=1,
        label="page_count_row",
    )[0]
    page_size_value = _exact_row(
        page_size_rows[0],
        columns=1,
        label="page_size_row",
    )[0]
    page_count = _exact_positive_integer(
        page_count_value,
        label="page_count",
        maximum=_MAX_SQLITE_PAGE_COUNT,
    )
    page_size = _exact_positive_integer(
        page_size_value,
        label="page_size",
        maximum=_MAX_SQLITE_PAGE_SIZE,
    )
    if page_size < 512 or page_size & (page_size - 1):
        raise _SnapshotFailure("page_size_malformed")
    return page_count, page_size


def _read_applied_timestamps(connection: sqlite3.Connection) -> Dict[int, str]:
    rows = _fetch_bounded(
        _execute(
            connection,
            """
            SELECT version, applied_at
            FROM main.qe_schema_migrations
            ORDER BY version
            """,
            label="migration_timestamps",
        ),
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="migration_timestamps",
    )
    timestamps: Dict[int, str] = {}
    previous = 0
    for raw_row in rows:
        values = _exact_row(raw_row, columns=2, label="migration_timestamp_row")
        migration_id = _exact_positive_integer(
            values[0],
            label="migration_timestamp_id",
            maximum=(2**31) - 1,
        )
        if migration_id <= previous:
            raise _SnapshotFailure("migration_timestamps_not_ordered")
        timestamp = _exact_text(
            values[1],
            label="migration_timestamp",
            maximum=32,
        )
        timestamps[migration_id] = timestamp
        previous = migration_id
    return timestamps


def _read_catalog(connection: sqlite3.Connection) -> Tuple[_CatalogObject, ...]:
    trusted_objects = tuple(
        item for profile in BACKUP_TOPOLOGY_REGISTRY.profiles for item in profile.objects
    )
    rows = _fetch_bounded(
        _execute(
            connection,
            """
            SELECT type, name, tbl_name, sql
            FROM main.sqlite_schema
            ORDER BY type, name, tbl_name
            """,
            label="schema_catalog",
        ),
        maximum=len(trusted_objects) + 1,
        label="schema_catalog",
    )
    catalog: List[_CatalogObject] = []
    coordinates: Set[Tuple[str, str]] = set()
    for raw_row in rows:
        values = _exact_row(raw_row, columns=4, label="schema_catalog_row")
        object_type = _exact_text(values[0], label="schema_object_type", maximum=7)
        name = _exact_text(values[1], label="schema_object_name", maximum=128)
        table_name = _exact_text(values[2], label="schema_table_name", maximum=128)
        schema_sql = values[3]
        if schema_sql is not None and type(schema_sql) is not str:
            raise _SnapshotFailure("schema_object_sql_malformed")
        if name.startswith("sqlite_stat"):
            if (
                object_type != "table"
                or name != _SQLITE_STAT1_NAME
                or table_name != _SQLITE_STAT1_NAME
                or schema_sql is None
            ):
                raise _SnapshotFailure("schema_catalog_statistics_object_unsupported")
            try:
                statistics_digest = backup_schema_ddl_sha256(schema_sql)
            except (TypeError, ValueError) as error:
                raise _SnapshotFailure("schema_catalog_statistics_object_unsupported") from error
            if statistics_digest != _SQLITE_STAT1_DDL_SHA256:
                raise _SnapshotFailure("schema_catalog_statistics_object_unsupported")
            continue
        coordinate = (object_type, name)
        if coordinate in coordinates:
            raise _SnapshotFailure("schema_catalog_duplicate_coordinate")
        coordinates.add(coordinate)
        try:
            ddl_sha256 = None if schema_sql is None else backup_schema_ddl_sha256(schema_sql)
        except (TypeError, ValueError) as error:
            raise _SnapshotFailure("schema_object_sql_malformed") from error
        catalog.append(
            _CatalogObject(
                object_type=object_type,
                name=name,
                table_name=table_name,
                ddl_sha256=ddl_sha256,
            )
        )
    return tuple(catalog)


def _classify_profiles(catalog: Tuple[_CatalogObject, ...]) -> Tuple[str, ...]:
    trusted_by_coordinate: Dict[Tuple[str, str], TrustedBackupSchemaObject] = {
        (item.object_type, item.name): item
        for profile in BACKUP_TOPOLOGY_REGISTRY.profiles
        for item in profile.objects
    }
    actual_coordinates: Set[Tuple[str, str]] = set()
    for item in catalog:
        coordinate = (item.object_type, item.name)
        trusted = trusted_by_coordinate.get(coordinate)
        if trusted is None:
            raise _SnapshotFailure("schema_catalog_contains_unknown_object")
        if item.table_name != trusted.table_name or item.ddl_sha256 != trusted.ddl_sha256:
            raise _SnapshotFailure("schema_catalog_object_drift")
        actual_coordinates.add(coordinate)

    present: List[str] = []
    for profile in BACKUP_TOPOLOGY_REGISTRY.profiles:
        expected_coordinates = {(item.object_type, item.name) for item in profile.objects}
        observed = expected_coordinates & actual_coordinates
        if observed and observed != expected_coordinates:
            raise _SnapshotFailure("schema_catalog_contains_partial_profile")
        if observed:
            present.append(profile.name)
    expected_present_coordinates = {
        (item.object_type, item.name)
        for profile in BACKUP_TOPOLOGY_REGISTRY.profiles
        if profile.name in present
        for item in profile.objects
    }
    if actual_coordinates != expected_present_coordinates:
        raise _SnapshotFailure("schema_catalog_classification_failed")
    return tuple(sorted(present, key=lambda item: item.encode("utf-8")))


def _read_table_counts(
    connection: sqlite3.Connection,
    present_profile_names: Tuple[str, ...],
) -> Dict[str, int]:
    objects = BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(present_profile_names)
    table_names = tuple(
        sorted(
            (item.name for item in objects if item.object_type == "table"),
            key=lambda item: item.encode("utf-8"),
        )
    )
    counts: Dict[str, int] = {}
    for table_name in table_names:
        rows = _fetch_bounded(
            _execute(
                connection,
                f'SELECT COUNT(*) FROM main."{table_name}"',
                label="table_count",
            ),
            maximum=1,
            label="table_count",
        )
        if len(rows) != 1:
            raise _SnapshotFailure("table_count_malformed")
        value = _exact_row(rows[0], columns=1, label="table_count_row")[0]
        if type(value) is not int or value < 0 or value > _MAX_SQLITE_ROW_COUNT:
            raise _SnapshotFailure("table_count_malformed")
        counts[table_name] = value
    return counts


def _derive_inside_transaction(
    connection: sqlite3.Connection,
) -> BackupManifestV2Snapshot:
    _validate_integrity(connection)
    page_count, page_size = _read_page_geometry(connection)
    try:
        durable_state = inspect_schema_state(connection)
    except DomainMigrationBridgeIntegrityError as error:
        raise _SnapshotFailure("schema_state_invalid") from error
    timestamps: Dict[int, str] = {}
    if durable_state.applied_migrations:
        timestamps = _read_applied_timestamps(connection)
    try:
        schema_state = BackupManifestV2SchemaState.from_schema_state(
            durable_state,
            timestamps,
        )
    except (TypeError, ValueError) as error:
        raise _SnapshotFailure("schema_state_not_manifest_compatible") from error
    catalog = _read_catalog(connection)
    present_profile_names = _classify_profiles(catalog)
    counts = _read_table_counts(connection, present_profile_names)
    try:
        topology = BackupManifestV2RegistryTopology.from_trusted_registry(
            schema_state,
            present_profile_names,
            counts,
        )
        return BackupManifestV2Snapshot(
            page_count=page_count,
            page_size=page_size,
            schema_state=schema_state,
            registry_topology=topology,
        )
    except (TypeError, ValueError) as error:
        raise _SnapshotFailure("registry_topology_invalid") from error


def _connection_in_transaction(connection: sqlite3.Connection) -> bool:
    try:
        value = connection.in_transaction
    except sqlite3.Error as error:
        raise _SnapshotFailure("connection_state_unavailable") from error
    if type(value) is not bool:
        raise _SnapshotFailure("connection_state_malformed")
    return value


def _validate_connection_configuration(connection: sqlite3.Connection) -> None:
    try:
        row_factory = connection.row_factory
        text_factory = connection.text_factory
    except sqlite3.Error as error:
        raise _SnapshotFailure("connection_configuration_unavailable") from error
    if row_factory is not None and row_factory is not sqlite3.Row:
        raise _SnapshotFailure("connection_row_factory_unsupported")
    if text_factory is not str:
        raise _SnapshotFailure("connection_text_factory_unsupported")


def _rollback_owned_snapshot(connection: sqlite3.Connection) -> None:
    if not _connection_in_transaction(connection):
        raise _SnapshotFailure("read_snapshot_ended_early")
    try:
        connection.rollback()
    except sqlite3.Error as error:
        raise _SnapshotFailure("read_snapshot_rollback_failed") from error
    if _connection_in_transaction(connection):
        raise _SnapshotFailure("read_snapshot_rollback_incomplete")


def _is_exact_control_signal(error: BaseException) -> bool:
    return type(error) in _CONTROL_SIGNAL_TYPES


def _finish_owned_snapshot(connection: sqlite3.Connection) -> None:
    """End a conservatively owned transaction without replacing an exact control."""

    originating_error = sys.exc_info()[1]
    try:
        if _connection_in_transaction(connection):
            _rollback_owned_snapshot(connection)
    except BaseException as cleanup_error:
        if originating_error is not None and _is_exact_control_signal(originating_error):
            return
        if _is_exact_control_signal(cleanup_error):
            raise
        if isinstance(cleanup_error, _SnapshotFailure):
            raise
        raise _SnapshotFailure("read_snapshot_cleanup_failed") from cleanup_error


def _derive_inside_owned_snapshot(
    connection: sqlite3.Connection,
) -> BackupManifestV2Snapshot:
    """Establish cleanup before BEGIN and keep it active through derivation."""

    try:
        _execute(connection, "BEGIN", label="read_snapshot_begin")
        if not _connection_in_transaction(connection):
            raise _SnapshotFailure("read_snapshot_not_opened")
        return _derive_inside_transaction(connection)
    finally:
        _finish_owned_snapshot(connection)


def _derive_with_owned_snapshot(
    connection: sqlite3.Connection,
) -> BackupManifestV2Snapshot:
    """Retry cleanup after one control or failure interrupts the inner finalizer."""

    try:
        return _derive_inside_owned_snapshot(connection)
    finally:
        _finish_owned_snapshot(connection)


def derive_backup_manifest_v2_snapshot(
    connection: object,
) -> BackupManifestV2Snapshot:
    """Derive manifest-ready v2 evidence inside one owned SQLite read transaction.

    The caller must supply a fresh exact connection created in the current process. An
    existing transaction is rejected so this function can prove that it ends the exact
    snapshot it opened. Originating exact control signals take precedence over cleanup
    failures; all recognized data/SQLite failures become one stable public error code.
    """

    if type(connection) is not sqlite3.Connection:
        _raise_snapshot_error("connection_type_invalid")
    typed_connection = connection
    try:
        _validate_connection_configuration(typed_connection)
        if _connection_in_transaction(typed_connection):
            raise _SnapshotFailure("connection_transaction_already_active")
    except _SnapshotFailure as error:
        code = error.code
        _raise_snapshot_error(code)

    failure_code: Optional[str] = None
    try:
        return _derive_with_owned_snapshot(typed_connection)
    except BaseException as error:
        if _is_exact_control_signal(error):
            raise
        if not isinstance(error, _SnapshotFailure):
            raise
        failure_code = error.code
    if failure_code is None:
        _raise_snapshot_error("snapshot_result_missing")
    _raise_snapshot_error(failure_code)


__all__ = [
    "BackupManifestV2Snapshot",
    "BackupManifestV2SnapshotError",
    "derive_backup_manifest_v2_snapshot",
]
