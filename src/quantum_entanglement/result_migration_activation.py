"""Explicit migration-7 activation and rollback kernel.

The legacy store deliberately remains on migration 1--6 by default.  This module is the
reviewed, opt-in bridge for the invocation-results schema: it installs the domain sidecar,
bootstraps the legacy metadata, applies migration 7, records the native descriptor and
dependency edges, and verifies the complete post-state before acknowledging the transition.

It does not expose a result writer, an ``AcceptedV2`` capability, a worker, publication, or
an external connector.  The activation API is therefore useful for migration/reopen rehearsal
without turning the current private result graph into a production execution path.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ._inactive_invocation_results_migration import (
    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
    _KNOWN_INVOCATION_RESULTS_MIGRATIONS,
)
from .domain_migrations import (
    LEGACY_DOMAIN_MIGRATIONS,
    DomainMigrationBridgeIntegrityError,
    DomainMigrationRegistry,
    _bootstrap_legacy_domain_migration_metadata_locked,
    _install_domain_migration_sidecar_locked,
    read_domain_migration_bridge_state,
    validate_domain_migration_registry,
    validate_domain_migration_sidecar_schema,
)
from .migrations import (
    MIGRATIONS,
    MigrationDriftError,
    MigrationVersionError,
    _sql_statements,
    apply_sqlite_migrations,
    migration_text,
    validate_sqlite_schema,
)
from .protocol import utc_now

RESULT_ACCEPTANCE_MIGRATION = _KNOWN_INVOCATION_RESULTS_MIGRATIONS[-1]
RESULT_ACCEPTANCE_MIGRATIONS = tuple(_KNOWN_INVOCATION_RESULTS_MIGRATIONS)
RESULT_ACCEPTANCE_DESCRIPTOR = _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR

RESULT_ACCEPTANCE_DOMAIN_REGISTRY: DomainMigrationRegistry = validate_domain_migration_registry(
    (*LEGACY_DOMAIN_MIGRATIONS, RESULT_ACCEPTANCE_DESCRIPTOR),
    packaged_migrations=RESULT_ACCEPTANCE_MIGRATIONS,
)

_RESULT_ACCEPTANCE_OBJECT_NAMES = tuple(
    sorted(item.name for item in RESULT_ACCEPTANCE_DESCRIPTOR.owned_objects)
)
_RESULT_ACCEPTANCE_DEPENDENCIES = tuple(
    sorted(
        (RESULT_ACCEPTANCE_DESCRIPTOR.migration_id, dependency)
        for dependency in RESULT_ACCEPTANCE_DESCRIPTOR.dependencies
    )
)
_ACTIVE_MIGRATION_IDS = tuple(item.version for item in RESULT_ACCEPTANCE_MIGRATIONS)
_LEGACY_MIGRATION_IDS = tuple(item.version for item in MIGRATIONS)
_SHA256_HEX = frozenset("0123456789abcdef")
_MAX_TIMESTAMP_BYTES = 32


class ResultAcceptanceMigrationError(RuntimeError):
    """Base error for explicit migration-7 activation/rollback."""


class ResultAcceptanceMigrationIntegrityError(ResultAcceptanceMigrationError):
    """The source or target database is not an exact supported state."""


class ResultAcceptanceMigrationTransactionError(ResultAcceptanceMigrationError):
    """The activation/rollback transaction failed and rollback was confirmed."""


class ResultAcceptanceMigrationCommitAmbiguityError(ResultAcceptanceMigrationError):
    """COMMIT may have succeeded but its acknowledgement was not observed."""


@dataclass(frozen=True)
class ResultAcceptanceMigrationState:
    """Immutable, timestamp-free evidence for an activated migration-7 database."""

    schema_version: int
    registry_sha256: str
    applied_migration_ids: tuple[int, ...]
    native_metadata_id: int
    dependency_edges: tuple[tuple[int, int], ...]
    state_sha256: str


@dataclass(frozen=True)
class ResultAcceptanceMigrationRollbackState:
    """Immutable evidence that migration 7 was removed while the sidecar remained bridged."""

    schema_version: int
    legacy_registry_sha256: str
    applied_migration_ids: tuple[int, ...]
    sidecar_present: bool
    state_sha256: str


def _canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration state cannot be canonicalized"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _canonical_timestamp(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_TIMESTAMP_BYTES:
        raise ResultAcceptanceMigrationIntegrityError(f"{label} is not canonical")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResultAcceptanceMigrationIntegrityError(f"{label} is not canonical") from error
    if parsed.tzinfo is None:
        raise ResultAcceptanceMigrationIntegrityError(f"{label} is not canonical")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if normalized != value:
        raise ResultAcceptanceMigrationIntegrityError(f"{label} is not canonical")
    return value


def _canonical_digest_text(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in _SHA256_HEX for char in value):
        raise ResultAcceptanceMigrationIntegrityError(f"{label} is not a SHA-256 digest")
    return value


def _read_catalog_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    try:
        rows = connection.execute(
            """
            SELECT name
            FROM main.sqlite_master
            WHERE name IN ({placeholders})
            ORDER BY name
            """
            .replace("{placeholders}", ", ".join("?" for _ in _RESULT_ACCEPTANCE_OBJECT_NAMES)),
            _RESULT_ACCEPTANCE_OBJECT_NAMES,
        ).fetchall()
    except sqlite3.Error as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration schema catalog could not be inspected"
        ) from error
    names = tuple(row[0] for row in rows)
    if any(type(name) is not str for name in names):
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration schema catalog contains a malformed name"
        )
    return names


def _read_active_metadata(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, str, int, str, str, str, str], ...]:
    try:
        rows = connection.execute(
            """
            SELECT migration_version, domain, domain_version, metadata_kind,
                   descriptor_sha256, owned_schema_sha256, recorded_at
            FROM main.qe_schema_migration_metadata
            ORDER BY migration_version
            """
        ).fetchall()
    except (sqlite3.Error, AttributeError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration metadata could not be read"
        ) from error
    decoded = []
    for row in rows:
        if type(row) is not sqlite3.Row or tuple(row.keys()) != (
            "migration_version",
            "domain",
            "domain_version",
            "metadata_kind",
            "descriptor_sha256",
            "owned_schema_sha256",
            "recorded_at",
        ):
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration metadata row shape is not exact"
            )
        values = tuple(row)
        if type(values[0]) is not int or type(values[2]) is not int:
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration metadata integer is malformed"
            )
        for index, label in ((1, "domain"), (3, "metadata kind")):
            if type(values[index]) is not str or not values[index].strip():
                raise ResultAcceptanceMigrationIntegrityError(
                    f"result migration metadata {label} is malformed"
                )
        _canonical_digest_text(values[4], "result migration descriptor")
        _canonical_digest_text(values[5], "result migration owned schema")
        _canonical_timestamp(values[6], "result migration recorded_at")
        decoded.append(values)
    return tuple(decoded)


def _read_active_dependencies(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, int], ...]:
    try:
        rows = connection.execute(
            """
            SELECT migration_version, depends_on_version
            FROM main.qe_schema_migration_dependencies
            ORDER BY migration_version, depends_on_version
            """
        ).fetchall()
    except (sqlite3.Error, AttributeError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration dependency rows could not be read"
        ) from error
    decoded = []
    for row in rows:
        if type(row) is not sqlite3.Row or tuple(row.keys()) != (
            "migration_version",
            "depends_on_version",
        ):
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration dependency row shape is not exact"
            )
        values = tuple(row)
        if any(type(value) is not int or value <= 0 for value in values):
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration dependency row is malformed"
            )
        decoded.append((values[0], values[1]))
    return tuple(decoded)


def _expected_metadata_rows(
    recorded_rows: Sequence[tuple[int, str, int, str, str, str, str]],
) -> tuple[tuple[int, str, int, str, str, str, str], ...]:
    if len(recorded_rows) != len(RESULT_ACCEPTANCE_DOMAIN_REGISTRY.descriptors):
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration metadata does not cover the active registry"
        )
    expected = []
    descriptors = RESULT_ACCEPTANCE_DOMAIN_REGISTRY.descriptors
    for row, descriptor in zip(recorded_rows, descriptors):
        if row[0] != descriptor.migration_id:
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration metadata IDs are not a continuous active prefix"
            )
        expected.append(
            (
                descriptor.migration_id,
                descriptor.domain,
                descriptor.domain_version,
                descriptor.kind,
                descriptor.descriptor_sha256,
                descriptor.owned_object_manifest_sha256,
                row[6],
            )
        )
    return tuple(expected)


def _read_active_state(connection: sqlite3.Connection) -> ResultAcceptanceMigrationState:
    try:
        version = validate_sqlite_schema(
            connection,
            migrations=RESULT_ACCEPTANCE_MIGRATIONS,
        )
    except (MigrationDriftError, MigrationVersionError, sqlite3.Error, TypeError, ValueError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration active schema is not exact"
        ) from error
    if version != RESULT_ACCEPTANCE_MIGRATION.version:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration active schema is not at migration 7"
        )
    try:
        sidecar_state = validate_domain_migration_sidecar_schema(connection)
    except Exception as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration sidecar schema is not exact"
        ) from error
    if sidecar_state.value != "exact":
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration sidecar schema is not installed"
        )
    metadata = _read_active_metadata(connection)
    expected_metadata = _expected_metadata_rows(metadata)
    if metadata != expected_metadata:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration metadata differs from the active registry"
        )
    dependencies = _read_active_dependencies(connection)
    expected_dependencies = tuple(
        sorted(
            
                (descriptor.migration_id, dependency)
                for descriptor in RESULT_ACCEPTANCE_DOMAIN_REGISTRY.descriptors
                for dependency in descriptor.dependencies
            
        )
    )
    if dependencies != expected_dependencies:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration dependencies differ from the active registry"
        )
    state_digest = _canonical_digest(
        {
            "appliedMigrationIds": list(_ACTIVE_MIGRATION_IDS),
            "dependencies": [list(edge) for edge in dependencies],
            "format": "qe.result-migration-state/1",
            "registrySha256": RESULT_ACCEPTANCE_DOMAIN_REGISTRY.registry_sha256,
            "schemaVersion": version,
        }
    )
    return ResultAcceptanceMigrationState(
        schema_version=version,
        registry_sha256=RESULT_ACCEPTANCE_DOMAIN_REGISTRY.registry_sha256,
        applied_migration_ids=_ACTIVE_MIGRATION_IDS,
        native_metadata_id=RESULT_ACCEPTANCE_DESCRIPTOR.migration_id,
        dependency_edges=dependencies,
        state_sha256=state_digest,
    )


def read_result_acceptance_migration_state(
    connection: sqlite3.Connection,
) -> ResultAcceptanceMigrationState:
    """Read exact activated migration-7 evidence without changing the database."""

    return _read_active_state(connection)


def _insert_native_metadata_locked(
    connection: sqlite3.Connection,
    *,
    recorded_at: str,
) -> None:
    descriptor = RESULT_ACCEPTANCE_DESCRIPTOR
    connection.execute(
        """
        INSERT INTO main.qe_schema_migration_metadata (
            migration_version, domain, domain_version, metadata_kind,
            descriptor_sha256, owned_schema_sha256, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            descriptor.migration_id,
            descriptor.domain,
            descriptor.domain_version,
            descriptor.kind,
            descriptor.descriptor_sha256,
            descriptor.owned_object_manifest_sha256,
            recorded_at,
        ),
    )
    for dependency in descriptor.dependencies:
        connection.execute(
            """
            INSERT INTO main.qe_schema_migration_dependencies (
                migration_version, depends_on_version
            ) VALUES (?, ?)
            """,
            (descriptor.migration_id, dependency),
        )


def _migration7_already_applied(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT filename, sha256 FROM main.qe_schema_migrations WHERE version = ?",
            (RESULT_ACCEPTANCE_MIGRATION.version,),
        ).fetchone()
    except sqlite3.Error as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration ledger could not be read"
        ) from error
    if row is None:
        return False
    digest = hashlib.sha256(
        migration_text(RESULT_ACCEPTANCE_MIGRATION.filename).encode("utf-8")
    ).hexdigest()
    if row[0] != RESULT_ACCEPTANCE_MIGRATION.filename or row[1] != digest:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration ledger row differs from packaged SQL"
        )
    return True


def _validate_activation_source_locked(connection: sqlite3.Connection) -> None:
    try:
        version = validate_sqlite_schema(connection, migrations=MIGRATIONS)
    except (MigrationDriftError, MigrationVersionError, sqlite3.Error, TypeError, ValueError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration source legacy schema is not exact"
        ) from error
    if version != MIGRATIONS[-1].version or _migration7_already_applied(connection):
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration source must be an exact unapplied legacy version 6"
        )
    if _read_catalog_names(connection):
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration source contains a partial result schema"
        )


def _rollback_owned_transaction(connection: sqlite3.Connection, label: str) -> None:
    try:
        active = connection.in_transaction
    except sqlite3.Error as error:
        raise ResultAcceptanceMigrationTransactionError(
            f"{label} transaction state could not be inspected"
        ) from error
    if not active:
        return
    try:
        connection.execute("ROLLBACK")
    except BaseException as error:
        raise ResultAcceptanceMigrationTransactionError(
            f"{label} rollback failed"
        ) from error
    if connection.in_transaction:
        raise ResultAcceptanceMigrationTransactionError(
            f"{label} rollback did not release the transaction"
        )


def _sample_clock(clock: Callable[[], str]) -> str:
    try:
        value = clock()
    except BaseException as error:
        raise ResultAcceptanceMigrationTransactionError(
            "result migration clock failed"
        ) from error
    return _canonical_timestamp(value, "result migration clock")


def activate_result_acceptance_migration(
    connection: sqlite3.Connection,
    *,
    clock: Callable[[], str] = utc_now,
    _process_guard: Callable[[], None] | None = None,
) -> ResultAcceptanceMigrationState:
    """Atomically activate migration 7 on an exact v6 database.

    The operation is idempotent for an already activated database.  It never accepts a
    partial result schema, a newer ledger, or an altered sidecar; all DDL, ledger, metadata,
    dependency rows, and post-state validation happen under one writer transaction.
    """

    if not callable(clock):
        raise TypeError("result migration clock must be callable")
    if _process_guard is not None and not callable(_process_guard):
        raise TypeError("_process_guard must be callable or None")
    if _process_guard is not None:
        _process_guard()
    if connection.in_transaction:
        raise ResultAcceptanceMigrationTransactionError(
            "result migration activation requires no active caller transaction"
        )
    try:
        preflight_version = validate_sqlite_schema(
            connection,
            migrations=RESULT_ACCEPTANCE_MIGRATIONS,
        )
    except MigrationVersionError:
        # The legacy validator intentionally rejects an already activated v7 database;
        # use the active validator below to distinguish that supported state from drift.
        try:
            preflight_version = validate_sqlite_schema(connection, migrations=MIGRATIONS)
        except (MigrationDriftError, MigrationVersionError, sqlite3.Error, TypeError, ValueError) as error:
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration preflight schema is not exact"
            ) from error
    except (MigrationDriftError, sqlite3.Error, TypeError, ValueError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration preflight schema is not exact"
        ) from error
    if preflight_version < MIGRATIONS[-1].version:
        if _read_catalog_names(connection):
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration preflight found a partial result schema"
            )
        try:
            apply_sqlite_migrations(
                connection,
                clock=clock,
                _process_guard=_process_guard,
            )
            preflight_version = validate_sqlite_schema(
                connection,
                migrations=RESULT_ACCEPTANCE_MIGRATIONS,
            )
        except (MigrationDriftError, MigrationVersionError, sqlite3.Error, TypeError, ValueError) as error:
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration legacy prefix could not be prepared"
            ) from error
    if preflight_version == RESULT_ACCEPTANCE_MIGRATION.version:
        return _read_active_state(connection)
    if preflight_version != MIGRATIONS[-1].version:
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration activation requires legacy schema version 6"
        )
    if _read_catalog_names(connection):
        raise ResultAcceptanceMigrationIntegrityError(
            "result migration preflight found a partial result schema"
        )

    owns_transaction = False
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            owns_transaction = connection.in_transaction
        if not owns_transaction:
            raise ResultAcceptanceMigrationTransactionError(
                "result migration activation did not acquire a transaction"
            )
        _validate_activation_source_locked(connection)
        if _process_guard is not None:
            _process_guard()
        sidecar_state = validate_domain_migration_sidecar_schema(connection)
        if sidecar_state.value == "absent":
            _install_domain_migration_sidecar_locked(connection)
            if _process_guard is not None:
                _process_guard()
        try:
            _bootstrap_legacy_domain_migration_metadata_locked(
                connection,
                clock=clock,
            )
            if _process_guard is not None:
                _process_guard()
        except DomainMigrationBridgeIntegrityError as error:
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration legacy metadata bootstrap failed"
            ) from error

        sql = migration_text(RESULT_ACCEPTANCE_MIGRATION.filename)
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if digest != RESULT_ACCEPTANCE_DESCRIPTOR.sql_sha256:
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration packaged SQL digest is not exact"
            )
        for statement in _sql_statements(sql):
            if _process_guard is not None:
                _process_guard()
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO main.qe_schema_migrations (version, filename, sha256, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                RESULT_ACCEPTANCE_MIGRATION.version,
                RESULT_ACCEPTANCE_MIGRATION.filename,
                digest,
                _sample_clock(clock),
            ),
        )
        if _process_guard is not None:
            _process_guard()
        _insert_native_metadata_locked(connection, recorded_at=_sample_clock(clock))
        if _process_guard is not None:
            _process_guard()
        state = _read_active_state(connection)
        if _process_guard is not None:
            _process_guard()
        connection.execute("COMMIT")
        if _process_guard is not None:
            _process_guard()
        if connection.in_transaction:
            raise ResultAcceptanceMigrationTransactionError(
                "result migration activation COMMIT did not end the transaction"
            )
        owns_transaction = False
    except BaseException as error:
        if owns_transaction and connection.in_transaction:
            _rollback_owned_transaction(connection, "result migration activation")
        elif not connection.in_transaction and not isinstance(
            error,
            (
                ResultAcceptanceMigrationIntegrityError,
                ResultAcceptanceMigrationTransactionError,
            ),
        ):
            raise ResultAcceptanceMigrationCommitAmbiguityError(
                "result migration activation commit outcome is unknown; reopen and reconcile"
            ) from None
        if isinstance(error, ResultAcceptanceMigrationTransactionError):
            raise
        if isinstance(error, ResultAcceptanceMigrationIntegrityError):
            raise
        raise ResultAcceptanceMigrationTransactionError(
            "result migration activation transaction was rolled back"
        ) from error

    committed = _read_active_state(connection)
    if _process_guard is not None:
        _process_guard()
    if state != committed:
        raise ResultAcceptanceMigrationIntegrityError(
            "committed result migration state differs from the locked state"
        )
    return committed


def rollback_result_acceptance_migration(
    connection: sqlite3.Connection,
) -> ResultAcceptanceMigrationRollbackState:
    """Safely remove empty migration-7 result schema and retain the bridged legacy sidecar."""

    if connection.in_transaction:
        raise ResultAcceptanceMigrationTransactionError(
            "result migration rollback requires no active caller transaction"
        )
    _read_active_state(connection)
    owns_transaction = False
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            owns_transaction = connection.in_transaction
        if not owns_transaction:
            raise ResultAcceptanceMigrationTransactionError(
                "result migration rollback did not acquire a transaction"
            )
        _read_active_state(connection)
        down_filename = RESULT_ACCEPTANCE_MIGRATION.filename.replace(".up.sql", ".down.sql")
        for statement in _sql_statements(migration_text(down_filename)):
            connection.execute(statement)
        validate_sqlite_schema(connection, migrations=MIGRATIONS)
        sidecar = read_domain_migration_bridge_state(connection)
        if sidecar.shape.value != "bridged_prefix":
            raise ResultAcceptanceMigrationIntegrityError(
                "result migration rollback did not retain the bridged legacy sidecar"
            )
        connection.execute("COMMIT")
        if connection.in_transaction:
            raise ResultAcceptanceMigrationTransactionError(
                "result migration rollback COMMIT did not end the transaction"
            )
        owns_transaction = False
    except BaseException as error:
        if owns_transaction and connection.in_transaction:
            _rollback_owned_transaction(connection, "result migration rollback")
        elif not connection.in_transaction and not isinstance(
            error,
            (
                ResultAcceptanceMigrationIntegrityError,
                ResultAcceptanceMigrationTransactionError,
            ),
        ):
            raise ResultAcceptanceMigrationCommitAmbiguityError(
                "result migration rollback commit outcome is unknown; reopen and reconcile"
            ) from None
        if isinstance(error, ResultAcceptanceMigrationTransactionError):
            raise
        if isinstance(error, ResultAcceptanceMigrationIntegrityError):
            raise
        raise ResultAcceptanceMigrationTransactionError(
            "result migration rollback transaction was rolled back"
        ) from error

    try:
        validate_sqlite_schema(connection, migrations=MIGRATIONS)
        sidecar = read_domain_migration_bridge_state(connection)
    except (DomainMigrationBridgeIntegrityError, MigrationDriftError, MigrationVersionError) as error:
        raise ResultAcceptanceMigrationIntegrityError(
            "committed result migration rollback state is not exact"
        ) from error
    state_digest = _canonical_digest(
        {
            "appliedMigrationIds": list(_LEGACY_MIGRATION_IDS),
            "format": "qe.result-migration-rollback-state/1",
            "legacyRegistrySha256": sidecar.registry_sha256,
            "schemaVersion": MIGRATIONS[-1].version,
            "sidecarPresent": True,
        }
    )
    return ResultAcceptanceMigrationRollbackState(
        schema_version=MIGRATIONS[-1].version,
        legacy_registry_sha256=sidecar.registry_sha256,
        applied_migration_ids=_LEGACY_MIGRATION_IDS,
        sidecar_present=True,
        state_sha256=state_digest,
    )


__all__ = [
    "RESULT_ACCEPTANCE_DESCRIPTOR",
    "RESULT_ACCEPTANCE_DOMAIN_REGISTRY",
    "RESULT_ACCEPTANCE_MIGRATION",
    "RESULT_ACCEPTANCE_MIGRATIONS",
    "ResultAcceptanceMigrationCommitAmbiguityError",
    "ResultAcceptanceMigrationError",
    "ResultAcceptanceMigrationIntegrityError",
    "ResultAcceptanceMigrationRollbackState",
    "ResultAcceptanceMigrationState",
    "ResultAcceptanceMigrationTransactionError",
    "activate_result_acceptance_migration",
    "read_result_acceptance_migration_state",
    "rollback_result_acceptance_migration",
]
