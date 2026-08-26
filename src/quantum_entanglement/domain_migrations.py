# ruff: noqa: UP006, UP035, UP045
"""Trusted descriptors and read-only schema checks for domain-scoped migrations.

This module contains trusted package metadata, bounded validators, exact bridge bootstrap
operations, and a pure bridge-only planner.  It never enables native or sparse migration
plans and does not change the legacy migration runner.  Inspection, validation, and
planning remain side-effect free so bridge code can reject untrusted state before opening
a write transaction.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    cast,
)

from .migrations import (
    MIGRATIONS,
    Migration,
    MigrationDriftError,
    MigrationVersionError,
    _expected_schema_objects,
    migration_text,
    validate_sqlite_schema,
)
from .protocol import utc_now

DomainMigrationKind = Literal["legacy_bootstrap", "native"]

MAX_DOMAIN_MIGRATIONS = 4096
MAX_MIGRATION_DEPENDENCIES = 256
MAX_OWNED_SCHEMA_OBJECTS = 1024
MAX_MIGRATION_DOMAINS = 256
MAX_DOMAIN_LENGTH = 64
MAX_MIGRATION_FILENAME_LENGTH = 255
MAX_SCHEMA_OBJECT_NAME_LENGTH = 128
MAX_MIGRATION_ID = (2**63) - 1
MAX_DOMAIN_VERSION = (2**63) - 1
MAX_DOMAIN_MIGRATION_SIDECAR_SCHEMA_OBJECTS = 16
MAX_BRIDGE_PLAN_ACTIONS = 2

DOMAIN_MIGRATION_METADATA_TABLE_NAME = "qe_schema_migration_metadata"
DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME = "qe_schema_migration_dependencies"

DOMAIN_MIGRATION_METADATA_TABLE_SQL = """
CREATE TABLE qe_schema_migration_metadata (
    migration_version INTEGER PRIMARY KEY,
    domain TEXT NOT NULL,
    domain_version INTEGER NOT NULL,
    metadata_kind TEXT NOT NULL,
    descriptor_sha256 TEXT NOT NULL,
    owned_schema_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(domain, domain_version),
    FOREIGN KEY(migration_version)
        REFERENCES qe_schema_migrations(version) ON DELETE RESTRICT,
    CHECK(migration_version > 0),
    CHECK(domain_version > 0),
    CHECK(
        length(domain) BETWEEN 1 AND 64
        AND substr(domain, 1, 1) GLOB '[a-z]'
        AND domain NOT GLOB '*[^a-z0-9_]*'
    ),
    CHECK(metadata_kind IN ('legacy_bootstrap', 'native')),
    CHECK(
        length(descriptor_sha256) = 64
        AND descriptor_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        length(owned_schema_sha256) = 64
        AND owned_schema_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);
""".strip()

DOMAIN_MIGRATION_DEPENDENCIES_TABLE_SQL = """
CREATE TABLE qe_schema_migration_dependencies (
    migration_version INTEGER NOT NULL,
    depends_on_version INTEGER NOT NULL,
    PRIMARY KEY(migration_version, depends_on_version),
    FOREIGN KEY(migration_version)
        REFERENCES qe_schema_migration_metadata(migration_version)
        ON DELETE RESTRICT,
    FOREIGN KEY(depends_on_version)
        REFERENCES qe_schema_migration_metadata(migration_version)
        ON DELETE RESTRICT,
    CHECK(migration_version <> depends_on_version)
);
""".strip()

DOMAIN_MIGRATION_SIDECAR_DDL = (
    DOMAIN_MIGRATION_METADATA_TABLE_SQL,
    DOMAIN_MIGRATION_DEPENDENCIES_TABLE_SQL,
)

_DOMAIN_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.up\.sql\Z")
_SCHEMA_OBJECT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339_UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SCHEMA_OBJECT_TYPES = frozenset(("index", "table", "trigger", "view"))
_MIGRATION_KINDS = frozenset(("legacy_bootstrap", "native"))
_LEDGER_OBJECT = ("table", "qe_schema_migrations")

_OWNED_MANIFEST_FORMAT = "qe.domain-migration-owned-objects/1"
_DESCRIPTOR_FORMAT = "qe.domain-migration-descriptor/1"
_REGISTRY_FORMAT = "qe.domain-migration-registry/1"
_SIDECAR_SCHEMA_FORMAT = "qe.domain-migration-sidecar-schema/1"
_SCHEMA_STATE_FORMAT = "qe.domain-migration-schema-state/1"
_BRIDGE_PLAN_FORMAT = "qe.domain-migration-bridge-plan/1"
_BRIDGE_RELEASE_MODE = "bridge_only"
_MAX_SIDECAR_CATALOG_NAME_LENGTH = 128
_MAX_SIDECAR_CATALOG_SQL_LENGTH = 8192

_METADATA_AUTO_INDEX_NAME = "sqlite_autoindex_qe_schema_migration_metadata_1"
_DEPENDENCIES_AUTO_INDEX_NAME = "sqlite_autoindex_qe_schema_migration_dependencies_1"
_LEGACY_LEDGER_TABLE_NAME = "qe_schema_migrations"
_MAX_DURABLE_TIMESTAMP_LENGTH = 32

_T = TypeVar("_T")


class DomainMigrationSidecarSchemaError(RuntimeError):
    """Raised when sidecar catalog state is partial, weak, corrupt, or unexpected."""


class DomainMigrationSidecarInstallError(DomainMigrationSidecarSchemaError):
    """Raised when the bridge-only sidecar installation contract is violated."""


class DomainMigrationBridgeIntegrityError(DomainMigrationSidecarSchemaError):
    """Raised when durable legacy and sidecar rows are not registry-congruent."""


class DomainMigrationLegacyBootstrapError(DomainMigrationBridgeIntegrityError):
    """Raised when legacy metadata cannot be bootstrapped atomically and exactly."""


class DomainMigrationPlanningError(DomainMigrationBridgeIntegrityError):
    """Raised when a schema state cannot produce an exact bridge-only plan."""


class DomainMigrationPlanApplyError(DomainMigrationPlanningError):
    """Raised when an exact bridge-only plan cannot be applied safely."""


class DomainMigrationSidecarSchemaState(str, Enum):
    """Supported sidecar catalog states during the bridge-only rollout."""

    ABSENT = "absent"
    EXACT = "exact"


class DomainMigrationBridgeShape(str, Enum):
    """Supported durable row shapes during the bridge-only release."""

    LEGACY_PREFIX = "legacy_prefix"
    SIDECAR_EMPTY_PREFIX = "sidecar_empty_prefix"
    BRIDGED_PREFIX = "bridged_prefix"


class SchemaShape(str, Enum):
    """Exact supported database classifications for the bridge-only release."""

    SIDECAR_ABSENT = "sidecar_absent"
    EMPTY = "empty"
    LEGACY_PREFIX = "legacy_prefix"
    BRIDGED_PREFIX = "bridged_prefix"


class BridgeMigrationActionKind(str, Enum):
    """The complete write-action allowlist for the bridge-only release."""

    INSTALL_SIDECAR = "install_sidecar"
    BOOTSTRAP_LEGACY_METADATA = "bootstrap_legacy_metadata"


@dataclass(frozen=True)
class DomainMigrationLedgerRow:
    migration_id: int
    filename: str
    sql_sha256: str
    applied_at: str


@dataclass(frozen=True)
class DomainMigrationMetadataRow:
    migration_id: int
    domain: str
    domain_version: int
    kind: DomainMigrationKind
    descriptor_sha256: str
    owned_schema_sha256: str
    recorded_at: str


@dataclass(frozen=True, order=True)
class DomainMigrationDependencyRow:
    migration_id: int
    depends_on_migration_id: int


@dataclass(frozen=True)
class DomainMigrationBridgeState:
    shape: DomainMigrationBridgeShape
    legacy_schema_version: int
    ledger_rows: Tuple[DomainMigrationLedgerRow, ...]
    metadata_rows: Tuple[DomainMigrationMetadataRow, ...]
    dependency_rows: Tuple[DomainMigrationDependencyRow, ...]
    registry_sha256: str


@dataclass(frozen=True, order=True)
class AppliedSchemaMigration:
    """Timestamp-free registry evidence for one applied legacy migration."""

    migration_id: int
    filename: str
    sql_sha256: str
    domain: str
    domain_version: int
    kind: DomainMigrationKind
    descriptor_sha256: str
    owned_schema_sha256: str
    metadata_recorded: bool


@dataclass(frozen=True, order=True)
class SchemaDomainHead:
    """The latest applied migration coordinate for one domain."""

    domain: str
    domain_version: int
    migration_id: int
    owned_schema_sha256: str
    metadata_recorded: bool


@dataclass(frozen=True)
class SchemaState:
    """Canonical immutable bridge-aware schema evidence.

    Timestamps are deliberately absent so equivalent durable history produces the same
    ``state_sha256``.  Every collection is snapshotted with a hard bound even when a
    caller bypasses static tuple annotations.
    """

    sidecar_format: int
    shape: SchemaShape
    legacy_schema_version: int
    applied_migrations: Tuple[AppliedSchemaMigration, ...]
    domain_heads: Tuple[SchemaDomainHead, ...]
    dependency_edges: Tuple[DomainMigrationDependencyRow, ...]
    owned_schema_digests: Tuple[Tuple[str, str], ...]
    registry_sha256: str
    state_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "applied_migrations",
            _bounded_tuple(
                self.applied_migrations,
                maximum=MAX_DOMAIN_MIGRATIONS,
                label="schema state applied migrations",
            ),
        )
        object.__setattr__(
            self,
            "domain_heads",
            _bounded_tuple(
                self.domain_heads,
                maximum=MAX_MIGRATION_DOMAINS,
                label="schema state domain heads",
            ),
        )
        object.__setattr__(
            self,
            "dependency_edges",
            _bounded_tuple(
                self.dependency_edges,
                maximum=MAX_DOMAIN_MIGRATIONS,
                label="schema state dependency edges",
            ),
        )
        object.__setattr__(
            self,
            "owned_schema_digests",
            _bounded_tuple(
                self.owned_schema_digests,
                maximum=MAX_MIGRATION_DOMAINS,
                label="schema state owned schema digests",
            ),
        )


@dataclass(frozen=True, order=True)
class BridgeMigrationAction:
    """One allowlisted state transition; it intentionally contains no SQL."""

    sequence: int
    kind: BridgeMigrationActionKind
    source_shape: SchemaShape
    result_shape: SchemaShape


@dataclass(frozen=True)
class BridgeMigrationPlan:
    """Deterministic registry- and source-bound bridge-only action plan."""

    release_mode: str
    source_state_sha256: str
    registry_sha256: str
    source_shape: SchemaShape
    target_shape: SchemaShape
    actions: Tuple[BridgeMigrationAction, ...]
    plan_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "actions",
            _bounded_tuple(
                self.actions,
                maximum=MAX_BRIDGE_PLAN_ACTIONS,
                label="bridge migration plan actions",
            ),
        )


def _bounded_tuple(values: Iterable[_T], *, maximum: int, label: str) -> Tuple[_T, ...]:
    """Snapshot at most ``maximum`` items without exhausting an untrusted iterable."""

    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError(f"{label} must be iterable") from error
    items = tuple(islice(iterator, maximum + 1))
    if len(items) > maximum:
        raise ValueError(f"{label} exceeds the hard limit of {maximum}")
    return items


def _canonical_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _SidecarSchemaObject:
    object_type: str
    name: str
    table_name: str
    schema_sql: Optional[str]


def _canonical_sidecar_schema_sql(sql: str) -> str:
    # SQLite removes a leading IF NOT EXISTS clause from sqlite_master itself.  Do not
    # rewrite text inside quoted identifiers or CHECK values: doing so could hide a
    # semantic schema change.  Only insignificant whitespace outside quoted regions and
    # one optional trailing statement terminator are normalized.
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    output: List[str] = []
    quote_end: Optional[str] = None
    index = 0
    while index < len(stripped):
        character = stripped[index]
        if quote_end is not None:
            output.append(character)
            if character == quote_end:
                if quote_end != "]" and index + 1 < len(stripped):
                    if stripped[index + 1] == quote_end:
                        output.append(stripped[index + 1])
                        index += 2
                        continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote_end = character
            output.append(character)
        elif character == "[":
            quote_end = "]"
            output.append(character)
        elif character.isspace():
            if output and output[-1] != " ":
                output.append(" ")
        else:
            output.append(character)
        index += 1
    return "".join(output).strip()


_EXPECTED_SIDECAR_SCHEMA_OBJECTS = tuple(
    sorted(
        (
            _SidecarSchemaObject(
                "index",
                _DEPENDENCIES_AUTO_INDEX_NAME,
                DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
                None,
            ),
            _SidecarSchemaObject(
                "index",
                _METADATA_AUTO_INDEX_NAME,
                DOMAIN_MIGRATION_METADATA_TABLE_NAME,
                None,
            ),
            _SidecarSchemaObject(
                "table",
                DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
                DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
                _canonical_sidecar_schema_sql(DOMAIN_MIGRATION_DEPENDENCIES_TABLE_SQL),
            ),
            _SidecarSchemaObject(
                "table",
                DOMAIN_MIGRATION_METADATA_TABLE_NAME,
                DOMAIN_MIGRATION_METADATA_TABLE_NAME,
                _canonical_sidecar_schema_sql(DOMAIN_MIGRATION_METADATA_TABLE_SQL),
            ),
        ),
        key=lambda item: (item.object_type, item.name, item.table_name),
    )
)

DOMAIN_MIGRATION_SIDECAR_SCHEMA_SHA256 = _canonical_sha256(
    {
        "format": _SIDECAR_SCHEMA_FORMAT,
        "objects": [
            {
                "name": item.name,
                "objectType": item.object_type,
                "schemaSql": item.schema_sql,
                "tableName": item.table_name,
            }
            for item in _EXPECTED_SIDECAR_SCHEMA_OBJECTS
        ],
    }
)


def _sidecar_catalog_text(value: object, *, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum:
        raise DomainMigrationSidecarSchemaError("domain migration sidecar catalog row is malformed")
    return value


def _parse_sidecar_catalog_row(raw_row: object) -> _SidecarSchemaObject:
    try:
        values = _bounded_tuple(
            cast(Iterable[object], raw_row),
            maximum=4,
            label="domain migration sidecar catalog columns",
        )
    except (TypeError, ValueError) as error:
        raise DomainMigrationSidecarSchemaError(
            "domain migration sidecar catalog row is malformed"
        ) from error
    if len(values) != 4:
        raise DomainMigrationSidecarSchemaError("domain migration sidecar catalog row is malformed")
    object_type = _sidecar_catalog_text(
        values[0],
        maximum=len("trigger"),
    )
    name = _sidecar_catalog_text(
        values[1],
        maximum=_MAX_SIDECAR_CATALOG_NAME_LENGTH,
    )
    table_name = _sidecar_catalog_text(
        values[2],
        maximum=_MAX_SIDECAR_CATALOG_NAME_LENGTH,
    )
    raw_sql = values[3]
    if raw_sql is None:
        schema_sql = None
    else:
        sql = _sidecar_catalog_text(
            raw_sql,
            maximum=_MAX_SIDECAR_CATALOG_SQL_LENGTH,
        )
        schema_sql = _canonical_sidecar_schema_sql(sql)
    return _SidecarSchemaObject(object_type, name, table_name, schema_sql)


def validate_domain_migration_sidecar_schema(
    connection: sqlite3.Connection,
) -> DomainMigrationSidecarSchemaState:
    """Validate exact sidecar catalog congruence without mutating SQLite.

    A legacy database with neither sidecar table is a supported ``ABSENT`` state.  Any
    partial pair, altered DDL, unexpected index/trigger/view attached to either sidecar,
    malformed catalog row, or over-limit catalog is rejected.  Unrelated application and
    legacy migration objects remain outside this validator's ownership boundary.
    """

    query = """
        SELECT type, name, tbl_name, sql
        FROM main.sqlite_master
        WHERE name IN (?, ?)
           OR tbl_name IN (?, ?)
        ORDER BY type, name, tbl_name
        LIMIT ?
    """
    parameters = (
        DOMAIN_MIGRATION_METADATA_TABLE_NAME,
        DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
        DOMAIN_MIGRATION_METADATA_TABLE_NAME,
        DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
        MAX_DOMAIN_MIGRATION_SIDECAR_SCHEMA_OBJECTS + 1,
    )
    try:
        raw_rows = connection.execute(query, parameters).fetchmany(
            MAX_DOMAIN_MIGRATION_SIDECAR_SCHEMA_OBJECTS + 1
        )
    except sqlite3.Error as error:
        raise DomainMigrationSidecarSchemaError(
            "domain migration sidecar catalog could not be inspected"
        ) from error
    if len(raw_rows) > MAX_DOMAIN_MIGRATION_SIDECAR_SCHEMA_OBJECTS:
        raise DomainMigrationSidecarSchemaError(
            "domain migration sidecar schema exceeds the inspection limit"
        )
    if not raw_rows:
        return DomainMigrationSidecarSchemaState.ABSENT

    actual_objects = tuple(
        sorted(
            (_parse_sidecar_catalog_row(row) for row in raw_rows),
            key=lambda item: (item.object_type, item.name, item.table_name),
        )
    )
    actual_tables = {item.name for item in actual_objects if item.object_type == "table"}
    expected_tables = {
        DOMAIN_MIGRATION_METADATA_TABLE_NAME,
        DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
    }
    if actual_tables != expected_tables:
        raise DomainMigrationSidecarSchemaError(
            "domain migration sidecar tables must be both absent or both exact"
        )
    if actual_objects != _EXPECTED_SIDECAR_SCHEMA_OBJECTS:
        raise DomainMigrationSidecarSchemaError(
            "domain migration sidecar schema differs from the exact packaged definition"
        )
    return DomainMigrationSidecarSchemaState.EXACT


def _rollback_sidecar_install(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        return
    try:
        connection.execute("ROLLBACK")
    except BaseException as error:
        raise DomainMigrationSidecarInstallError(
            "domain migration sidecar installation rollback failed"
        ) from error
    if connection.in_transaction:
        raise DomainMigrationSidecarInstallError(
            "domain migration sidecar installation rollback did not release its transaction"
        )


def _install_domain_migration_sidecar_locked(
    connection: sqlite3.Connection,
) -> DomainMigrationSidecarSchemaState:
    """Install and validate the exact sidecar while the caller holds a write lock."""

    locked_state = validate_domain_migration_sidecar_schema(connection)
    if locked_state is DomainMigrationSidecarSchemaState.ABSENT:
        for statement in DOMAIN_MIGRATION_SIDECAR_DDL:
            connection.execute(statement)

    installed_state = validate_domain_migration_sidecar_schema(connection)
    if installed_state is not DomainMigrationSidecarSchemaState.EXACT:
        raise DomainMigrationSidecarInstallError(
            "domain migration sidecar installation did not produce the exact schema"
        )
    return installed_state


def install_domain_migration_sidecar(
    connection: sqlite3.Connection,
) -> DomainMigrationSidecarSchemaState:
    """Atomically install the exact bridge-only sidecar, or validate an existing one.

    ``EXACT`` is an idempotent read-only fast path.  ``ABSENT`` is rechecked only after
    obtaining a SQLite write lock, then both tables are created and validated in one
    transaction.  This function intentionally writes no metadata/dependency rows and does
    not enable native or sparse migration application.
    """

    if connection.in_transaction:
        raise DomainMigrationSidecarInstallError(
            "domain migration sidecar installation requires no active caller transaction"
        )
    preflight = validate_domain_migration_sidecar_schema(connection)
    if preflight is DomainMigrationSidecarSchemaState.EXACT:
        return preflight

    owns_transaction = False
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            # A connection wrapper can fail after SQLite has acquired the lock.  The
            # caller was proven transaction-free above, so any live transaction here is
            # installer-owned and must be cleaned up by the outer handler.
            owns_transaction = connection.in_transaction
        if not connection.in_transaction:
            raise DomainMigrationSidecarInstallError(
                "domain migration sidecar installation did not acquire a transaction"
            )

        _install_domain_migration_sidecar_locked(connection)
        connection.execute("COMMIT")
        owns_transaction = False
    except BaseException:
        if owns_transaction:
            _rollback_sidecar_install(connection)
        raise

    committed_state = validate_domain_migration_sidecar_schema(connection)
    if committed_state is not DomainMigrationSidecarSchemaState.EXACT:
        raise DomainMigrationSidecarInstallError(
            "committed domain migration sidecar schema is not exact"
        )
    return committed_state


@dataclass(frozen=True, order=True)
class OwnedSchemaObject:
    """One canonically fingerprinted SQLite object owned by a domain."""

    object_type: str
    name: str
    ddl_sha256: str


@dataclass(frozen=True)
class DomainMigrationDescriptor:
    """Immutable package metadata for one globally identified domain migration."""

    migration_id: int
    filename: str
    sql_sha256: str
    domain: str
    domain_version: int
    kind: DomainMigrationKind
    dependencies: Tuple[int, ...]
    owned_objects: Tuple[OwnedSchemaObject, ...]

    def __post_init__(self) -> None:
        # Always take a bounded snapshot, including for tuple inputs.  Otherwise a caller
        # could construct an oversized tuple and invoke a digest property before the
        # registry validator gets a chance to enforce collection limits.
        dependencies = _bounded_tuple(
            self.dependencies,
            maximum=MAX_MIGRATION_DEPENDENCIES,
            label="migration dependencies",
        )
        object.__setattr__(self, "dependencies", dependencies)
        owned_objects = _bounded_tuple(
            self.owned_objects,
            maximum=MAX_OWNED_SCHEMA_OBJECTS,
            label="owned schema objects",
        )
        object.__setattr__(self, "owned_objects", owned_objects)

    @property
    def owned_object_manifest_sha256(self) -> str:
        """Digest the order-independent, canonical owned-object manifest."""

        migration_id = _require_positive_integer(
            self.migration_id,
            "migration ID",
            MAX_MIGRATION_ID,
        )
        objects = _validate_owned_objects(
            self.owned_objects,
            migration_id=migration_id,
        )
        return _owned_object_manifest_digest(objects)

    @property
    def descriptor_sha256(self) -> str:
        """Digest every execution-relevant descriptor field canonically."""

        return _descriptor_digest(_normalize_descriptor(self))


@dataclass(frozen=True)
class DomainMigrationRegistry:
    """A normalized registry plus its canonical content digest."""

    descriptors: Tuple[DomainMigrationDescriptor, ...]
    registry_sha256: str


def _require_plain_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    return value


def _require_positive_integer(value: object, label: str, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value <= 0 or value > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _require_sha256(value: object, label: str) -> str:
    digest = _require_plain_string(value, label)
    if len(digest) != 64 or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _validate_owned_objects(
    values: Iterable[OwnedSchemaObject],
    *,
    migration_id: int,
) -> Tuple[OwnedSchemaObject, ...]:
    objects = _bounded_tuple(
        values,
        maximum=MAX_OWNED_SCHEMA_OBJECTS,
        label=f"migration {migration_id} owned schema objects",
    )
    normalized: List[OwnedSchemaObject] = []
    coordinates: Set[Tuple[str, str]] = set()
    for item in objects:
        if type(item) is not OwnedSchemaObject:
            raise TypeError(
                f"migration {migration_id} owned schema objects must be OwnedSchemaObject"
            )
        object_type = _require_plain_string(item.object_type, "schema object type")
        if len(object_type) > len("trigger") or object_type not in _SCHEMA_OBJECT_TYPES:
            raise ValueError("schema object type must be one of index, table, trigger, or view")
        name = _require_plain_string(item.name, "schema object name")
        if (
            len(name) > MAX_SCHEMA_OBJECT_NAME_LENGTH
            or _SCHEMA_OBJECT_NAME_PATTERN.fullmatch(name) is None
        ):
            raise ValueError("schema object name must be a bounded ASCII SQLite identifier")
        digest = _require_sha256(item.ddl_sha256, "schema object DDL sha256")
        coordinate = (object_type, name)
        if coordinate in coordinates:
            raise ValueError(
                f"migration {migration_id} has duplicate owned object {object_type} {name!r}"
            )
        coordinates.add(coordinate)
        normalized.append(OwnedSchemaObject(object_type, name, digest))
    return tuple(
        sorted(
            normalized,
            key=lambda item: (item.object_type, item.name, item.ddl_sha256),
        )
    )


def _normalize_descriptor(
    descriptor: DomainMigrationDescriptor,
) -> DomainMigrationDescriptor:
    migration_id = _require_positive_integer(
        descriptor.migration_id,
        "migration ID",
        MAX_MIGRATION_ID,
    )
    filename = _require_plain_string(descriptor.filename, "migration filename")
    if (
        len(filename) > MAX_MIGRATION_FILENAME_LENGTH
        or _FILENAME_PATTERN.fullmatch(filename) is None
    ):
        raise ValueError("migration filename must be a bounded basename ending in .up.sql")
    sql_sha256 = _require_sha256(descriptor.sql_sha256, "migration SQL sha256")
    domain = _require_plain_string(descriptor.domain, "migration domain")
    if len(domain) > MAX_DOMAIN_LENGTH or _DOMAIN_PATTERN.fullmatch(domain) is None:
        raise ValueError("migration domain must be a bounded lower-snake-case identifier")
    domain_version = _require_positive_integer(
        descriptor.domain_version,
        "domain version",
        MAX_DOMAIN_VERSION,
    )
    kind = _require_plain_string(descriptor.kind, "migration kind")
    if len(kind) > len("legacy_bootstrap") or kind not in _MIGRATION_KINDS:
        raise ValueError("migration kind must be legacy_bootstrap or native")

    dependencies = _bounded_tuple(
        descriptor.dependencies,
        maximum=MAX_MIGRATION_DEPENDENCIES,
        label=f"migration {migration_id} dependencies",
    )
    normalized_dependencies: List[int] = []
    seen_dependencies: Set[int] = set()
    for dependency in dependencies:
        normalized_dependency = _require_positive_integer(
            dependency,
            f"migration {migration_id} dependency",
            MAX_MIGRATION_ID,
        )
        if normalized_dependency in seen_dependencies:
            raise ValueError(f"migration {migration_id} has duplicate dependencies")
        seen_dependencies.add(normalized_dependency)
        normalized_dependencies.append(normalized_dependency)

    owned_objects = _validate_owned_objects(
        descriptor.owned_objects,
        migration_id=migration_id,
    )
    return DomainMigrationDescriptor(
        migration_id=migration_id,
        filename=filename,
        sql_sha256=sql_sha256,
        domain=domain,
        domain_version=domain_version,
        kind=cast(DomainMigrationKind, kind),
        dependencies=tuple(sorted(normalized_dependencies)),
        owned_objects=owned_objects,
    )


def _owned_object_manifest_digest(
    owned_objects: Sequence[OwnedSchemaObject],
) -> str:
    return _canonical_sha256(
        {
            "format": _OWNED_MANIFEST_FORMAT,
            "objects": [
                {
                    "ddlSha256": item.ddl_sha256,
                    "name": item.name,
                    "objectType": item.object_type,
                }
                for item in owned_objects
            ],
        }
    )


def _descriptor_digest(descriptor: DomainMigrationDescriptor) -> str:
    return _canonical_sha256(
        {
            "dependencies": list(descriptor.dependencies),
            "domain": descriptor.domain,
            "domainVersion": descriptor.domain_version,
            "filename": descriptor.filename,
            "format": _DESCRIPTOR_FORMAT,
            "kind": descriptor.kind,
            "migrationId": descriptor.migration_id,
            "ownedObjectManifestSha256": _owned_object_manifest_digest(descriptor.owned_objects),
            "sqlSha256": descriptor.sql_sha256,
        }
    )


def _validate_unique_coordinates(
    descriptors: Sequence[DomainMigrationDescriptor],
) -> None:
    migration_ids: Set[int] = set()
    filenames: Set[str] = set()
    domain_coordinates: Set[Tuple[str, int]] = set()
    domain_versions: Dict[str, List[int]] = {}
    object_owners: Dict[str, str] = {}
    for descriptor in descriptors:
        if descriptor.migration_id in migration_ids:
            raise ValueError(f"duplicate migration ID {descriptor.migration_id}")
        migration_ids.add(descriptor.migration_id)
        if descriptor.filename in filenames:
            raise ValueError(f"duplicate migration filename {descriptor.filename!r}")
        filenames.add(descriptor.filename)
        coordinate = (descriptor.domain, descriptor.domain_version)
        if coordinate in domain_coordinates:
            raise ValueError(
                f"duplicate domain migration coordinate "
                f"{descriptor.domain}@{descriptor.domain_version}"
            )
        domain_coordinates.add(coordinate)
        domain_versions.setdefault(descriptor.domain, []).append(descriptor.domain_version)
        for owned_object in descriptor.owned_objects:
            previous_owner = object_owners.setdefault(owned_object.name, descriptor.domain)
            if previous_owner != descriptor.domain:
                raise ValueError(
                    f"schema object {owned_object.name!r} is claimed by multiple domains"
                )

    if len(domain_versions) > MAX_MIGRATION_DOMAINS:
        raise ValueError(
            f"migration registry exceeds the hard limit of {MAX_MIGRATION_DOMAINS} domains"
        )
    for domain, versions in domain_versions.items():
        ordered = sorted(versions)
        if ordered != list(range(1, len(ordered) + 1)):
            raise ValueError(
                f"domain {domain!r} versions must be a continuous prefix starting at one"
            )


def _validate_dependency_graph(
    descriptors: Sequence[DomainMigrationDescriptor],
) -> None:
    known_ids = {descriptor.migration_id for descriptor in descriptors}
    indegree: Dict[int, int] = {}
    dependents: Dict[int, List[int]] = {descriptor.migration_id: [] for descriptor in descriptors}
    for descriptor in descriptors:
        if descriptor.migration_id in descriptor.dependencies:
            raise ValueError(f"migration {descriptor.migration_id} cannot depend on itself")
        unknown = set(descriptor.dependencies) - known_ids
        if unknown:
            raise ValueError(
                f"migration {descriptor.migration_id} has unknown dependencies {sorted(unknown)}"
            )
        indegree[descriptor.migration_id] = len(descriptor.dependencies)
        for dependency in descriptor.dependencies:
            dependents[dependency].append(descriptor.migration_id)

    ready = [migration_id for migration_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    visited = 0
    while ready:
        migration_id = heapq.heappop(ready)
        visited += 1
        for dependent in sorted(dependents[migration_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if visited != len(descriptors):
        raise ValueError("migration dependency graph must be acyclic")


def _validate_packaged_migrations(
    descriptors: Sequence[DomainMigrationDescriptor],
    packaged_migrations: Iterable[Migration],
) -> None:
    packaged = _bounded_tuple(
        packaged_migrations,
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="packaged migrations",
    )
    by_id: Dict[int, Migration] = {}
    filenames: Set[str] = set()
    for migration in packaged:
        if type(migration) is not Migration:
            raise TypeError("packaged migrations must contain Migration values")
        if migration.version in by_id:
            raise ValueError(f"packaged migrations have duplicate ID {migration.version}")
        if migration.filename in filenames:
            raise ValueError(f"packaged migrations have duplicate filename {migration.filename!r}")
        by_id[migration.version] = migration
        filenames.add(migration.filename)

    descriptor_ids = {descriptor.migration_id for descriptor in descriptors}
    if descriptor_ids != set(by_id):
        missing_descriptors = sorted(set(by_id) - descriptor_ids)
        unknown_descriptors = sorted(descriptor_ids - set(by_id))
        raise ValueError(
            "domain migration descriptors must exactly cover packaged migrations; "
            f"missing={missing_descriptors}, unknown={unknown_descriptors}"
        )

    for descriptor in descriptors:
        packaged_migration = by_id[descriptor.migration_id]
        if descriptor.filename != packaged_migration.filename:
            raise ValueError(
                f"migration {descriptor.migration_id} filename differs from packaged migration"
            )
        try:
            sql = migration_text(packaged_migration.filename)
        except (FileNotFoundError, ModuleNotFoundError) as error:
            raise ValueError(
                f"migration {descriptor.migration_id} packaged SQL is missing"
            ) from error
        if type(sql) is not str:
            raise TypeError("packaged migration SQL must be text")
        sql_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if descriptor.sql_sha256 != sql_sha256:
            raise ValueError(
                f"migration {descriptor.migration_id} SQL digest differs from packaged migration"
            )


def _registry_digest(descriptors: Sequence[DomainMigrationDescriptor]) -> str:
    return _canonical_sha256(
        {
            "descriptors": [
                {
                    "descriptorSha256": descriptor.descriptor_sha256,
                    "migrationId": descriptor.migration_id,
                }
                for descriptor in descriptors
            ],
            "format": _REGISTRY_FORMAT,
        }
    )


def validate_domain_migration_registry(
    descriptors: Iterable[DomainMigrationDescriptor],
    *,
    packaged_migrations: Iterable[Migration] = MIGRATIONS,
) -> DomainMigrationRegistry:
    """Validate, normalize, and fingerprint a complete packaged registry.

    All collections are consumed with explicit upper bounds.  The returned descriptor
    order, nested dependencies, owned manifests, descriptor digests, and registry digest
    therefore do not depend on caller iteration order.
    """

    raw_descriptors = _bounded_tuple(
        descriptors,
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="domain migration descriptors",
    )
    normalized: List[DomainMigrationDescriptor] = []
    for descriptor in raw_descriptors:
        if type(descriptor) is not DomainMigrationDescriptor:
            raise TypeError(
                "domain migration descriptors must contain DomainMigrationDescriptor values"
            )
        normalized.append(_normalize_descriptor(descriptor))
    ordered = tuple(sorted(normalized, key=lambda item: item.migration_id))

    _validate_unique_coordinates(ordered)
    _validate_dependency_graph(ordered)
    _validate_packaged_migrations(ordered, packaged_migrations)

    trusted_by_id = {item.migration_id: item for item in LEGACY_DOMAIN_MIGRATIONS}
    for descriptor in ordered:
        trusted = trusted_by_id.get(descriptor.migration_id)
        if trusted is not None and descriptor != trusted:
            raise ValueError(
                f"legacy migration {descriptor.migration_id} descriptor differs from "
                "the immutable bootstrap mapping"
            )

    return DomainMigrationRegistry(
        descriptors=ordered,
        registry_sha256=_registry_digest(ordered),
    )


_LEGACY_COORDINATES: Mapping[int, Tuple[str, int]] = {
    1: ("attempts", 1),
    2: ("artifacts", 1),
    3: ("delivery", 1),
    4: ("admission", 1),
}

_LEGACY_OWNED_OBJECTS: Mapping[int, Tuple[Tuple[str, str], ...]] = {
    1: (
        ("index", "idx_invocation_attempts_job"),
        ("index", "idx_invocation_attempts_status"),
        ("index", "idx_invocation_jobs_claim"),
        ("index", "idx_invocation_jobs_lease_expiry"),
        ("index", "idx_invocation_jobs_session"),
        ("table", "invocation_attempts"),
        ("table", "invocation_jobs"),
    ),
    2: (
        ("index", "idx_artifact_versions_digest"),
        ("index", "idx_artifact_versions_head"),
        ("index", "idx_artifact_versions_task"),
        ("table", "artifact_blobs"),
        ("table", "artifact_versions"),
    ),
    3: (
        ("index", "idx_outbox_ambiguities_one_open"),
        ("index", "idx_outbox_ambiguities_opened"),
        ("table", "outbox_ambiguities"),
    ),
    4: (
        ("index", "idx_invocation_admissions_stream"),
        ("table", "invocation_admissions"),
    ),
}

_LEGACY_DEPENDENCIES: Mapping[int, Tuple[int, ...]] = {
    1: (),
    2: (),
    3: (),
    4: (1,),
}


def _build_legacy_descriptors() -> Tuple[DomainMigrationDescriptor, ...]:
    packaged_by_id = {migration.version: migration for migration in MIGRATIONS}
    if set(packaged_by_id) != set(_LEGACY_COORDINATES):
        raise RuntimeError("legacy packaged migration IDs differ from the bootstrap mapping")

    descriptors: List[DomainMigrationDescriptor] = []
    for migration_id in sorted(_LEGACY_COORDINATES):
        migration = packaged_by_id[migration_id]
        expected_schema = _expected_schema_objects(MIGRATIONS, (migration_id,))
        actual_coordinates = set(expected_schema) - {_LEDGER_OBJECT}
        declared_coordinates = set(_LEGACY_OWNED_OBJECTS[migration_id])
        if actual_coordinates != declared_coordinates:
            raise RuntimeError(
                f"legacy migration {migration_id} owned objects differ from the golden mapping"
            )
        owned_objects = tuple(
            OwnedSchemaObject(
                object_type=object_type,
                name=name,
                ddl_sha256=hashlib.sha256(
                    expected_schema[(object_type, name)].encode("utf-8")
                ).hexdigest(),
            )
            for object_type, name in sorted(declared_coordinates)
        )
        domain, domain_version = _LEGACY_COORDINATES[migration_id]
        sql = migration_text(migration.filename)
        descriptors.append(
            DomainMigrationDescriptor(
                migration_id=migration_id,
                filename=migration.filename,
                sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                domain=domain,
                domain_version=domain_version,
                kind="legacy_bootstrap",
                dependencies=_LEGACY_DEPENDENCIES[migration_id],
                owned_objects=owned_objects,
            )
        )
    return tuple(descriptors)


LEGACY_DOMAIN_MIGRATIONS = _build_legacy_descriptors()
DOMAIN_MIGRATION_REGISTRY = validate_domain_migration_registry(LEGACY_DOMAIN_MIGRATIONS)


def _durable_row_values(
    raw_row: object,
    *,
    expected_columns: int,
    label: str,
) -> Tuple[object, ...]:
    try:
        values = _bounded_tuple(
            cast(Iterable[object], raw_row),
            maximum=expected_columns,
            label=f"{label} columns",
        )
    except (TypeError, ValueError) as error:
        raise DomainMigrationBridgeIntegrityError(
            f"{label} has a malformed column shape"
        ) from error
    if len(values) != expected_columns:
        raise DomainMigrationBridgeIntegrityError(f"{label} has a malformed column shape")
    return values


def _durable_integer(value: object, *, field: str, maximum: int) -> int:
    if type(value) is not int:
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} must be an exact SQLite INTEGER"
        )
    if value <= 0 or value > maximum:
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} is outside the supported positive integer range"
        )
    return value


def _durable_text(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str:
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} must be an exact SQLite TEXT value"
        )
    if not value or len(value) > maximum or value != value.strip():
        raise DomainMigrationBridgeIntegrityError(f"durable {field} is not bounded canonical text")
    return value


def _durable_sha256(value: object, *, field: str) -> str:
    digest = _durable_text(value, field=field, maximum=64)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} must be a lowercase SHA-256 digest"
        )
    return digest


def _durable_timestamp(value: object, *, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_DURABLE_TIMESTAMP_LENGTH
        or value != value.strip()
        or _RFC3339_UTC_PATTERN.fullmatch(value) is None
    ):
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} must be a canonical RFC3339 UTC timestamp"
        )
    timestamp = value
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as error:
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} must be a canonical RFC3339 UTC timestamp"
        ) from error
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if timestamp != normalized:
        raise DomainMigrationBridgeIntegrityError(
            f"durable {field} must be a canonical RFC3339 UTC timestamp"
        )
    return timestamp


def _read_bounded_bridge_rows(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Tuple[object, ...],
    *,
    maximum: int,
    label: str,
) -> Tuple[object, ...]:
    try:
        cursor = connection.execute(statement, parameters)
        raw_rows = cursor.fetchmany(maximum + 1)
        return _bounded_tuple(raw_rows, maximum=maximum, label=label)
    except DomainMigrationBridgeIntegrityError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise DomainMigrationBridgeIntegrityError(
            f"{label} could not be read within its hard limit"
        ) from error


def _legacy_ledger_is_present(connection: sqlite3.Connection) -> bool:
    rows = _read_bounded_bridge_rows(
        connection,
        """
        SELECT type
        FROM main.sqlite_master
        WHERE name = ?
        ORDER BY type
        LIMIT ?
        """,
        (_LEGACY_LEDGER_TABLE_NAME, MAX_DOMAIN_MIGRATIONS + 1),
        maximum=1,
        label="legacy migration ledger catalog rows",
    )
    if not rows:
        return False
    values = _durable_row_values(
        rows[0],
        expected_columns=1,
        label="legacy migration ledger catalog row",
    )
    object_type = _durable_text(
        values[0],
        field="legacy migration ledger catalog type",
        maximum=len("table"),
    )
    if object_type != "table":
        raise DomainMigrationBridgeIntegrityError(
            "legacy migration ledger catalog object is not the exact table type"
        )
    return True


def _read_legacy_ledger_rows(
    connection: sqlite3.Connection,
) -> Tuple[DomainMigrationLedgerRow, ...]:
    raw_rows = _read_bounded_bridge_rows(
        connection,
        """
        SELECT version, filename, sha256, applied_at
        FROM main.qe_schema_migrations
        ORDER BY version
        LIMIT ?
        """,
        (MAX_DOMAIN_MIGRATIONS + 1,),
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="legacy migration ledger rows",
    )
    decoded: List[DomainMigrationLedgerRow] = []
    for raw_row in raw_rows:
        values = _durable_row_values(
            raw_row,
            expected_columns=4,
            label="legacy migration ledger row",
        )
        migration_id = _durable_integer(
            values[0],
            field="legacy migration ID",
            maximum=MAX_MIGRATION_ID,
        )
        filename = _durable_text(
            values[1],
            field="legacy migration filename",
            maximum=MAX_MIGRATION_FILENAME_LENGTH,
        )
        if _FILENAME_PATTERN.fullmatch(filename) is None:
            raise DomainMigrationBridgeIntegrityError(
                "durable legacy migration filename is not canonical"
            )
        decoded.append(
            DomainMigrationLedgerRow(
                migration_id=migration_id,
                filename=filename,
                sql_sha256=_durable_sha256(
                    values[2],
                    field="legacy migration SQL sha256",
                ),
                applied_at=_durable_timestamp(
                    values[3],
                    field="legacy migration applied_at",
                ),
            )
        )
    return tuple(decoded)


def _read_metadata_rows(
    connection: sqlite3.Connection,
) -> Tuple[DomainMigrationMetadataRow, ...]:
    raw_rows = _read_bounded_bridge_rows(
        connection,
        """
        SELECT migration_version, domain, domain_version, metadata_kind,
               descriptor_sha256, owned_schema_sha256, recorded_at
        FROM main.qe_schema_migration_metadata
        ORDER BY migration_version
        LIMIT ?
        """,
        (MAX_DOMAIN_MIGRATIONS + 1,),
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="domain migration metadata rows",
    )
    decoded: List[DomainMigrationMetadataRow] = []
    for raw_row in raw_rows:
        values = _durable_row_values(
            raw_row,
            expected_columns=7,
            label="domain migration metadata row",
        )
        domain = _durable_text(
            values[1],
            field="domain migration domain",
            maximum=MAX_DOMAIN_LENGTH,
        )
        if _DOMAIN_PATTERN.fullmatch(domain) is None:
            raise DomainMigrationBridgeIntegrityError(
                "durable domain migration domain is not canonical"
            )
        kind = _durable_text(
            values[3],
            field="domain migration kind",
            maximum=len("legacy_bootstrap"),
        )
        if kind not in _MIGRATION_KINDS:
            raise DomainMigrationBridgeIntegrityError(
                "durable domain migration kind is unsupported"
            )
        decoded.append(
            DomainMigrationMetadataRow(
                migration_id=_durable_integer(
                    values[0],
                    field="domain metadata migration ID",
                    maximum=MAX_MIGRATION_ID,
                ),
                domain=domain,
                domain_version=_durable_integer(
                    values[2],
                    field="domain migration version",
                    maximum=MAX_DOMAIN_VERSION,
                ),
                kind=cast(DomainMigrationKind, kind),
                descriptor_sha256=_durable_sha256(
                    values[4],
                    field="domain migration descriptor sha256",
                ),
                owned_schema_sha256=_durable_sha256(
                    values[5],
                    field="domain migration owned schema sha256",
                ),
                recorded_at=_durable_timestamp(
                    values[6],
                    field="domain migration recorded_at",
                ),
            )
        )
    return tuple(decoded)


def _read_dependency_rows(
    connection: sqlite3.Connection,
) -> Tuple[DomainMigrationDependencyRow, ...]:
    raw_rows = _read_bounded_bridge_rows(
        connection,
        """
        SELECT migration_version, depends_on_version
        FROM main.qe_schema_migration_dependencies
        ORDER BY migration_version, depends_on_version
        LIMIT ?
        """,
        (MAX_DOMAIN_MIGRATIONS + 1,),
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="domain migration dependency rows",
    )
    decoded: List[DomainMigrationDependencyRow] = []
    for raw_row in raw_rows:
        values = _durable_row_values(
            raw_row,
            expected_columns=2,
            label="domain migration dependency row",
        )
        decoded.append(
            DomainMigrationDependencyRow(
                migration_id=_durable_integer(
                    values[0],
                    field="dependency migration ID",
                    maximum=MAX_MIGRATION_ID,
                ),
                depends_on_migration_id=_durable_integer(
                    values[1],
                    field="dependency target migration ID",
                    maximum=MAX_MIGRATION_ID,
                ),
            )
        )
    return tuple(decoded)


def _validate_legacy_ledger_congruence(
    rows: Sequence[DomainMigrationLedgerRow],
) -> Tuple[DomainMigrationDescriptor, ...]:
    descriptors = DOMAIN_MIGRATION_REGISTRY.descriptors
    applied_descriptors = descriptors[: len(rows)]
    actual_ids = tuple(row.migration_id for row in rows)
    expected_ids = tuple(item.migration_id for item in applied_descriptors)
    if actual_ids != expected_ids:
        raise DomainMigrationBridgeIntegrityError(
            "legacy migration ledger is not a continuous supported registry prefix"
        )
    for row, descriptor in zip(rows, applied_descriptors):
        if row.filename != descriptor.filename or row.sql_sha256 != descriptor.sql_sha256:
            raise DomainMigrationBridgeIntegrityError(
                "legacy migration ledger filename or SQL digest differs from the registry"
            )
    return applied_descriptors


def _reject_unapplied_legacy_owned_objects(
    connection: sqlite3.Connection,
    applied_descriptors: Sequence[DomainMigrationDescriptor],
) -> None:
    applied_names = {
        owned.name for descriptor in applied_descriptors for owned in descriptor.owned_objects
    }
    known_names = {
        owned.name
        for descriptor in DOMAIN_MIGRATION_REGISTRY.descriptors
        for owned in descriptor.owned_objects
    }
    for name in sorted(known_names - applied_names):
        rows = _read_bounded_bridge_rows(
            connection,
            """
            SELECT type
            FROM main.sqlite_master
            WHERE name = ?
              AND type IN ('table', 'index', 'trigger', 'view')
            ORDER BY type
            LIMIT ?
            """,
            (name, MAX_DOMAIN_MIGRATIONS + 1),
            maximum=1,
            label="unapplied legacy owned schema catalog rows",
        )
        if rows:
            raise DomainMigrationBridgeIntegrityError(
                "schema object exists for an unapplied legacy registry migration"
            )


def _validate_metadata_congruence(
    rows: Sequence[DomainMigrationMetadataRow],
    applied_descriptors: Sequence[DomainMigrationDescriptor],
) -> None:
    expected_ids = tuple(item.migration_id for item in applied_descriptors)
    actual_ids = tuple(row.migration_id for row in rows)
    if actual_ids != expected_ids:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration metadata does not exactly cover the applied ledger prefix"
        )
    for row, descriptor in zip(rows, applied_descriptors):
        if (
            row.domain != descriptor.domain
            or row.domain_version != descriptor.domain_version
            or row.kind != "legacy_bootstrap"
            or row.descriptor_sha256 != descriptor.descriptor_sha256
            or row.owned_schema_sha256 != descriptor.owned_object_manifest_sha256
        ):
            raise DomainMigrationBridgeIntegrityError(
                "domain migration metadata differs from the exact registry descriptor"
            )


def _validate_dependency_congruence(
    rows: Sequence[DomainMigrationDependencyRow],
    applied_descriptors: Sequence[DomainMigrationDescriptor],
) -> None:
    applied_ids = {item.migration_id for item in applied_descriptors}
    for row in rows:
        if row.migration_id not in applied_ids or row.depends_on_migration_id not in applied_ids:
            raise DomainMigrationBridgeIntegrityError(
                "domain migration dependency refers to an unapplied ledger row"
            )
    expected = tuple(
        sorted(
            DomainMigrationDependencyRow(descriptor.migration_id, dependency)
            for descriptor in applied_descriptors
            for dependency in descriptor.dependencies
        )
    )
    if tuple(rows) != expected:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration dependencies differ from the exact registry edges"
        )


def _read_domain_migration_bridge_snapshot(
    connection: sqlite3.Connection,
) -> DomainMigrationBridgeState:
    ledger_present = _legacy_ledger_is_present(connection)
    ledger_rows = _read_legacy_ledger_rows(connection) if ledger_present else ()
    applied_descriptors = _validate_legacy_ledger_congruence(ledger_rows)
    _reject_unapplied_legacy_owned_objects(connection, applied_descriptors)

    try:
        legacy_schema_version = validate_sqlite_schema(connection)
    except (MigrationDriftError, MigrationVersionError, sqlite3.Error) as error:
        raise DomainMigrationBridgeIntegrityError(
            "legacy migration ledger or owned schema is not exact"
        ) from error
    expected_schema_version = ledger_rows[-1].migration_id if ledger_rows else 0
    if legacy_schema_version != expected_schema_version:
        raise DomainMigrationBridgeIntegrityError(
            "legacy migration schema version changed during snapshot validation"
        )

    try:
        sidecar_state = validate_domain_migration_sidecar_schema(connection)
    except DomainMigrationSidecarSchemaError as error:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration sidecar schema is not exact"
        ) from error
    if sidecar_state is DomainMigrationSidecarSchemaState.ABSENT:
        return DomainMigrationBridgeState(
            shape=DomainMigrationBridgeShape.LEGACY_PREFIX,
            legacy_schema_version=legacy_schema_version,
            ledger_rows=ledger_rows,
            metadata_rows=(),
            dependency_rows=(),
            registry_sha256=DOMAIN_MIGRATION_REGISTRY.registry_sha256,
        )

    metadata_rows = _read_metadata_rows(connection)
    dependency_rows = _read_dependency_rows(connection)
    if not metadata_rows:
        if dependency_rows:
            raise DomainMigrationBridgeIntegrityError(
                "empty domain migration metadata cannot have dependency rows"
            )
        shape = DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX
    else:
        _validate_metadata_congruence(metadata_rows, applied_descriptors)
        _validate_dependency_congruence(dependency_rows, applied_descriptors)
        shape = DomainMigrationBridgeShape.BRIDGED_PREFIX

    return DomainMigrationBridgeState(
        shape=shape,
        legacy_schema_version=legacy_schema_version,
        ledger_rows=ledger_rows,
        metadata_rows=metadata_rows,
        dependency_rows=dependency_rows,
        registry_sha256=DOMAIN_MIGRATION_REGISTRY.registry_sha256,
    )


def _rollback_bridge_snapshot(connection: sqlite3.Connection) -> None:
    try:
        snapshot_is_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration read snapshot state could not be inspected during rollback"
        ) from error
    if not snapshot_is_active:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration read snapshot ended unexpectedly"
        )
    try:
        connection.execute("ROLLBACK")
    except BaseException as error:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration read snapshot rollback failed"
        ) from error
    try:
        snapshot_remains_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration read snapshot state could not be inspected after rollback"
        ) from error
    if snapshot_remains_active:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration read snapshot rollback did not release its transaction"
        )


def read_domain_migration_bridge_state(
    connection: sqlite3.Connection,
) -> DomainMigrationBridgeState:
    """Read and validate one registry-congruent durable state without repairing it.

    A transaction-free caller gets one deferred read snapshot which is always ended with
    ``ROLLBACK``.  An existing caller transaction is reused and never committed or rolled
    back.  The function performs no DDL, DML, bootstrap, repair, or sparse migration write.
    """

    try:
        caller_owns_transaction = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationBridgeIntegrityError(
            "domain migration read snapshot could not be opened"
        ) from error
    owns_snapshot = False
    try:
        if not caller_owns_transaction:
            try:
                try:
                    connection.execute("BEGIN")
                finally:
                    # A wrapper may fail after SQLite has opened the transaction.
                    # Capture ownership before propagating any BaseException so the
                    # outer cleanup path cannot leak a read snapshot or lock.
                    owns_snapshot = connection.in_transaction
            except sqlite3.Error as error:
                if owns_snapshot:
                    _rollback_bridge_snapshot(connection)
                raise DomainMigrationBridgeIntegrityError(
                    "domain migration read snapshot could not be opened"
                ) from error
            if not connection.in_transaction:
                raise DomainMigrationBridgeIntegrityError(
                    "domain migration read snapshot was not opened"
                )
            owns_snapshot = True

        try:
            state = _read_domain_migration_bridge_snapshot(connection)
        except DomainMigrationBridgeIntegrityError:
            raise
        except (sqlite3.Error, TypeError, ValueError, IndexError) as error:
            raise DomainMigrationBridgeIntegrityError(
                "domain migration durable state could not be decoded"
            ) from error
    except BaseException:
        if owns_snapshot and connection.in_transaction:
            _rollback_bridge_snapshot(connection)
        raise

    if owns_snapshot:
        _rollback_bridge_snapshot(connection)
    return state


_LEGACY_METADATA_INSERT_SQL = """
    INSERT INTO main.qe_schema_migration_metadata (
        migration_version,
        domain,
        domain_version,
        metadata_kind,
        descriptor_sha256,
        owned_schema_sha256,
        recorded_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_LEGACY_DEPENDENCY_INSERT_SQL = """
    INSERT INTO main.qe_schema_migration_dependencies (
        migration_version,
        depends_on_version
    ) VALUES (?, ?)
"""


def _read_legacy_bootstrap_state(
    connection: sqlite3.Connection,
    *,
    phase: str,
) -> DomainMigrationBridgeState:
    try:
        return read_domain_migration_bridge_state(connection)
    except DomainMigrationBridgeIntegrityError as error:
        raise DomainMigrationLegacyBootstrapError(
            f"legacy domain migration bootstrap {phase} state is not exact"
        ) from error


def _legacy_bootstrap_descriptors(
    state: DomainMigrationBridgeState,
) -> Tuple[DomainMigrationDescriptor, ...]:
    descriptors = DOMAIN_MIGRATION_REGISTRY.descriptors[: len(state.ledger_rows)]
    if tuple(item.migration_id for item in descriptors) != tuple(
        row.migration_id for row in state.ledger_rows
    ):
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap state is not a registry prefix"
        )
    applied_ids = {item.migration_id for item in descriptors}
    for descriptor in descriptors:
        if descriptor.kind != "legacy_bootstrap":
            raise DomainMigrationLegacyBootstrapError(
                "legacy domain migration bootstrap refuses non-legacy descriptors"
            )
        if any(dependency not in applied_ids for dependency in descriptor.dependencies):
            raise DomainMigrationLegacyBootstrapError(
                "legacy domain migration bootstrap dependency is not already applied"
            )
    return descriptors


def _sample_legacy_bootstrap_timestamp(clock: Callable[[], str]) -> str:
    raw_timestamp = clock()
    try:
        return _durable_timestamp(
            raw_timestamp,
            field="legacy domain migration bootstrap clock",
        )
    except DomainMigrationBridgeIntegrityError as error:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap clock returned a non-canonical timestamp"
        ) from error


def _expected_legacy_bootstrap_state(
    source: DomainMigrationBridgeState,
    descriptors: Sequence[DomainMigrationDescriptor],
    *,
    recorded_at: str,
) -> DomainMigrationBridgeState:
    metadata_rows = tuple(
        DomainMigrationMetadataRow(
            migration_id=descriptor.migration_id,
            domain=descriptor.domain,
            domain_version=descriptor.domain_version,
            kind="legacy_bootstrap",
            descriptor_sha256=descriptor.descriptor_sha256,
            owned_schema_sha256=descriptor.owned_object_manifest_sha256,
            recorded_at=recorded_at,
        )
        for descriptor in descriptors
    )
    dependency_rows = tuple(
        sorted(
            DomainMigrationDependencyRow(descriptor.migration_id, dependency)
            for descriptor in descriptors
            for dependency in descriptor.dependencies
        )
    )
    return DomainMigrationBridgeState(
        shape=DomainMigrationBridgeShape.BRIDGED_PREFIX,
        legacy_schema_version=source.legacy_schema_version,
        ledger_rows=source.ledger_rows,
        metadata_rows=metadata_rows,
        dependency_rows=dependency_rows,
        registry_sha256=source.registry_sha256,
    )


def _rollback_legacy_bootstrap(connection: sqlite3.Connection) -> None:
    try:
        transaction_is_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap rollback state could not be inspected"
        ) from error
    if not transaction_is_active:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap transaction ended unexpectedly"
        )
    try:
        connection.execute("ROLLBACK")
    except BaseException as error:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap rollback failed"
        ) from error
    try:
        transaction_remains_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap rollback state could not be verified"
        ) from error
    if transaction_remains_active:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap rollback did not release its transaction"
        )


def _cleanup_failed_legacy_bootstrap(connection: sqlite3.Connection) -> None:
    try:
        transaction_is_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap cleanup state could not be inspected"
        ) from error
    if transaction_is_active:
        _rollback_legacy_bootstrap(connection)


def _bootstrap_legacy_domain_migration_metadata_locked(
    connection: sqlite3.Connection,
    *,
    clock: Callable[[], str],
) -> Tuple[DomainMigrationBridgeState, bool]:
    """Bootstrap exact legacy metadata while the caller owns the write transaction."""

    locked_state = _read_legacy_bootstrap_state(connection, phase="locked")
    if locked_state.shape is DomainMigrationBridgeShape.LEGACY_PREFIX:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap sidecar disappeared under lock"
        )
    if locked_state.shape is DomainMigrationBridgeShape.BRIDGED_PREFIX:
        return locked_state, False
    if locked_state.shape is not DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap encountered an unsupported state"
        )

    descriptors = _legacy_bootstrap_descriptors(locked_state)
    if not descriptors:
        return locked_state, False

    recorded_at = _sample_legacy_bootstrap_timestamp(clock)
    expected_state = _expected_legacy_bootstrap_state(
        locked_state,
        descriptors,
        recorded_at=recorded_at,
    )
    for descriptor in descriptors:
        connection.execute(
            _LEGACY_METADATA_INSERT_SQL,
            (
                descriptor.migration_id,
                descriptor.domain,
                descriptor.domain_version,
                "legacy_bootstrap",
                descriptor.descriptor_sha256,
                descriptor.owned_object_manifest_sha256,
                recorded_at,
            ),
        )
    for dependency in expected_state.dependency_rows:
        connection.execute(
            _LEGACY_DEPENDENCY_INSERT_SQL,
            (dependency.migration_id, dependency.depends_on_migration_id),
        )

    post_write_state = _read_legacy_bootstrap_state(connection, phase="post-write")
    if post_write_state != expected_state:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap post-write state differs from expectation"
        )
    return expected_state, True


def bootstrap_legacy_domain_migration_metadata(
    connection: sqlite3.Connection,
    *,
    clock: Callable[[], str] = utc_now,
) -> DomainMigrationBridgeState:
    """Atomically describe an applied legacy prefix in an existing exact sidecar.

    This bridge-only writer never creates schema, applies a migration, repairs drift, or
    enables native/sparse planning.  It samples one canonical UTC timestamp under a
    ``BEGIN IMMEDIATE`` lock, writes only trusted legacy metadata and dependency rows,
    validates the exact post-state before commit, and re-reads it after commit.
    """

    try:
        caller_has_transaction = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap connection state could not be inspected"
        ) from error
    if caller_has_transaction:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap requires no active caller transaction"
        )
    if not callable(clock):
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap clock must be callable"
        )

    preflight = _read_legacy_bootstrap_state(connection, phase="preflight")
    if preflight.shape is DomainMigrationBridgeShape.LEGACY_PREFIX:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap requires an existing exact sidecar"
        )

    owns_transaction = False
    expected_state: Optional[DomainMigrationBridgeState] = None
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            # A wrapper may fail after SQLite has acquired the writer lock.
            owns_transaction = connection.in_transaction
        if not owns_transaction:
            raise DomainMigrationLegacyBootstrapError(
                "legacy domain migration bootstrap did not acquire a transaction"
            )

        expected_state, changed = _bootstrap_legacy_domain_migration_metadata_locked(
            connection,
            clock=clock,
        )
        if not changed:
            _rollback_legacy_bootstrap(connection)
            owns_transaction = False
            return expected_state

        connection.execute("COMMIT")
        if connection.in_transaction:
            raise DomainMigrationLegacyBootstrapError(
                "legacy domain migration bootstrap commit did not end its transaction"
            )
        owns_transaction = False
    except BaseException:
        if owns_transaction:
            _cleanup_failed_legacy_bootstrap(connection)
        raise

    if expected_state is None:
        raise DomainMigrationLegacyBootstrapError(
            "legacy domain migration bootstrap did not construct an expected state"
        )
    committed_state = _read_legacy_bootstrap_state(connection, phase="committed")
    if committed_state != expected_state:
        raise DomainMigrationLegacyBootstrapError(
            "committed legacy domain migration bootstrap state differs from expectation"
        )
    return committed_state


def _schema_state_digest(
    *,
    sidecar_format: int,
    shape: SchemaShape,
    legacy_schema_version: int,
    applied_migrations: Sequence[AppliedSchemaMigration],
    domain_heads: Sequence[SchemaDomainHead],
    dependency_edges: Sequence[DomainMigrationDependencyRow],
    owned_schema_digests: Sequence[Tuple[str, str]],
    registry_sha256: str,
) -> str:
    return _canonical_sha256(
        {
            "appliedMigrations": [
                {
                    "descriptorSha256": item.descriptor_sha256,
                    "domain": item.domain,
                    "domainVersion": item.domain_version,
                    "filename": item.filename,
                    "kind": item.kind,
                    "metadataRecorded": item.metadata_recorded,
                    "migrationId": item.migration_id,
                    "ownedSchemaSha256": item.owned_schema_sha256,
                    "sqlSha256": item.sql_sha256,
                }
                for item in applied_migrations
            ],
            "dependencyEdges": [
                {
                    "dependsOnMigrationId": item.depends_on_migration_id,
                    "migrationId": item.migration_id,
                }
                for item in dependency_edges
            ],
            "domainHeads": [
                {
                    "domain": item.domain,
                    "domainVersion": item.domain_version,
                    "metadataRecorded": item.metadata_recorded,
                    "migrationId": item.migration_id,
                    "ownedSchemaSha256": item.owned_schema_sha256,
                }
                for item in domain_heads
            ],
            "format": _SCHEMA_STATE_FORMAT,
            "legacySchemaVersion": legacy_schema_version,
            "ownedSchemaDigests": [
                {"domain": domain, "ownedSchemaSha256": digest}
                for domain, digest in owned_schema_digests
            ],
            "registrySha256": registry_sha256,
            "shape": shape.value,
            "sidecarFormat": sidecar_format,
        }
    )


def _build_schema_state(
    shape: SchemaShape,
    descriptors: Sequence[DomainMigrationDescriptor],
    *,
    dependency_edges: Sequence[DomainMigrationDependencyRow],
) -> SchemaState:
    metadata_recorded = shape is SchemaShape.BRIDGED_PREFIX
    applied_migrations = tuple(
        AppliedSchemaMigration(
            migration_id=descriptor.migration_id,
            filename=descriptor.filename,
            sql_sha256=descriptor.sql_sha256,
            domain=descriptor.domain,
            domain_version=descriptor.domain_version,
            kind=descriptor.kind,
            descriptor_sha256=descriptor.descriptor_sha256,
            owned_schema_sha256=descriptor.owned_object_manifest_sha256,
            metadata_recorded=metadata_recorded,
        )
        for descriptor in descriptors
    )
    latest_by_domain: Dict[str, AppliedSchemaMigration] = {}
    for migration in applied_migrations:
        previous = latest_by_domain.get(migration.domain)
        if previous is None or migration.domain_version > previous.domain_version:
            latest_by_domain[migration.domain] = migration
    domain_heads = tuple(
        sorted(
            (
                SchemaDomainHead(
                    domain=migration.domain,
                    domain_version=migration.domain_version,
                    migration_id=migration.migration_id,
                    owned_schema_sha256=migration.owned_schema_sha256,
                    metadata_recorded=migration.metadata_recorded,
                )
                for migration in latest_by_domain.values()
            ),
            key=lambda item: (
                item.domain.encode("utf-8"),
                item.domain_version,
                item.migration_id,
            ),
        )
    )
    ordered_edges = tuple(sorted(dependency_edges))
    owned_schema_digests = tuple((head.domain, head.owned_schema_sha256) for head in domain_heads)
    sidecar_format = 0 if shape is SchemaShape.SIDECAR_ABSENT else 1
    legacy_schema_version = descriptors[-1].migration_id if descriptors else 0
    registry_sha256 = DOMAIN_MIGRATION_REGISTRY.registry_sha256
    state_sha256 = _schema_state_digest(
        sidecar_format=sidecar_format,
        shape=shape,
        legacy_schema_version=legacy_schema_version,
        applied_migrations=applied_migrations,
        domain_heads=domain_heads,
        dependency_edges=ordered_edges,
        owned_schema_digests=owned_schema_digests,
        registry_sha256=registry_sha256,
    )
    return SchemaState(
        sidecar_format=sidecar_format,
        shape=shape,
        legacy_schema_version=legacy_schema_version,
        applied_migrations=applied_migrations,
        domain_heads=domain_heads,
        dependency_edges=ordered_edges,
        owned_schema_digests=owned_schema_digests,
        registry_sha256=registry_sha256,
        state_sha256=state_sha256,
    )


def _planning_plain_string(value: object, label: str) -> str:
    try:
        return _require_plain_string(value, label)
    except (TypeError, ValueError) as error:
        raise DomainMigrationPlanningError(
            "schema state contains a non-canonical scalar"
        ) from error


def _planning_positive_integer(value: object, label: str, maximum: int) -> int:
    try:
        return _require_positive_integer(value, label, maximum)
    except (TypeError, ValueError) as error:
        raise DomainMigrationPlanningError(
            "schema state contains a non-canonical scalar"
        ) from error


def _planning_sha256(value: object, label: str) -> str:
    try:
        return _require_sha256(value, label)
    except (TypeError, ValueError) as error:
        raise DomainMigrationPlanningError(
            "schema state contains a non-canonical scalar"
        ) from error


def _validate_schema_state_models(state: SchemaState) -> None:
    if type(state.sidecar_format) is not int or state.sidecar_format not in {0, 1}:
        raise DomainMigrationPlanningError("schema state sidecar format is unsupported")
    if type(state.shape) is not SchemaShape:
        raise DomainMigrationPlanningError("schema state shape is unsupported")
    if (
        type(state.legacy_schema_version) is not int
        or state.legacy_schema_version < 0
        or state.legacy_schema_version > MAX_MIGRATION_ID
    ):
        raise DomainMigrationPlanningError("schema state legacy version is unsupported")
    _planning_sha256(state.registry_sha256, "schema state registry sha256")
    _planning_sha256(state.state_sha256, "schema state sha256")

    for migration in state.applied_migrations:
        if type(migration) is not AppliedSchemaMigration:
            raise DomainMigrationPlanningError(
                "schema state applied migrations have an unsupported model"
            )
        _planning_positive_integer(
            migration.migration_id,
            "schema state migration ID",
            MAX_MIGRATION_ID,
        )
        _planning_plain_string(migration.filename, "schema state filename")
        _planning_sha256(migration.sql_sha256, "schema state SQL sha256")
        domain = _planning_plain_string(migration.domain, "schema state domain")
        if len(domain) > MAX_DOMAIN_LENGTH or _DOMAIN_PATTERN.fullmatch(domain) is None:
            raise DomainMigrationPlanningError("schema state domain is not canonical")
        _planning_positive_integer(
            migration.domain_version,
            "schema state domain version",
            MAX_DOMAIN_VERSION,
        )
        kind = _planning_plain_string(migration.kind, "schema state migration kind")
        if kind != "legacy_bootstrap":
            raise DomainMigrationPlanningError(
                "bridge-only planning refuses non-legacy applied migrations"
            )
        _planning_sha256(
            migration.descriptor_sha256,
            "schema state descriptor sha256",
        )
        _planning_sha256(
            migration.owned_schema_sha256,
            "schema state owned schema sha256",
        )
        if type(migration.metadata_recorded) is not bool:
            raise DomainMigrationPlanningError(
                "schema state metadata evidence must be an exact boolean"
            )

    for head in state.domain_heads:
        if type(head) is not SchemaDomainHead:
            raise DomainMigrationPlanningError(
                "schema state domain heads have an unsupported model"
            )
        domain = _planning_plain_string(head.domain, "schema head domain")
        if len(domain) > MAX_DOMAIN_LENGTH or _DOMAIN_PATTERN.fullmatch(domain) is None:
            raise DomainMigrationPlanningError("schema head domain is not canonical")
        _planning_positive_integer(
            head.domain_version,
            "schema head domain version",
            MAX_DOMAIN_VERSION,
        )
        _planning_positive_integer(
            head.migration_id,
            "schema head migration ID",
            MAX_MIGRATION_ID,
        )
        _planning_sha256(
            head.owned_schema_sha256,
            "schema head owned schema sha256",
        )
        if type(head.metadata_recorded) is not bool:
            raise DomainMigrationPlanningError(
                "schema head metadata evidence must be an exact boolean"
            )

    for edge in state.dependency_edges:
        if type(edge) is not DomainMigrationDependencyRow:
            raise DomainMigrationPlanningError(
                "schema state dependency edges have an unsupported model"
            )
        _planning_positive_integer(
            edge.migration_id,
            "schema state dependency migration ID",
            MAX_MIGRATION_ID,
        )
        _planning_positive_integer(
            edge.depends_on_migration_id,
            "schema state dependency target ID",
            MAX_MIGRATION_ID,
        )

    for digest_row in state.owned_schema_digests:
        if type(digest_row) is not tuple or len(digest_row) != 2:
            raise DomainMigrationPlanningError(
                "schema state owned schema digest rows are malformed"
            )
        domain = _planning_plain_string(
            digest_row[0],
            "owned schema digest domain",
        )
        if len(domain) > MAX_DOMAIN_LENGTH or _DOMAIN_PATTERN.fullmatch(domain) is None:
            raise DomainMigrationPlanningError("owned schema digest domain is not canonical")
        _planning_sha256(digest_row[1], "owned schema digest sha256")


def _validate_schema_state(
    state: object,
) -> Tuple[DomainMigrationDescriptor, ...]:
    if type(state) is not SchemaState:
        raise DomainMigrationPlanningError("planner input must be an exact SchemaState")
    typed_state = state
    _validate_schema_state_models(typed_state)
    if typed_state.registry_sha256 != DOMAIN_MIGRATION_REGISTRY.registry_sha256:
        raise DomainMigrationPlanningError(
            "schema state registry digest differs from the trusted registry"
        )

    applied_ids = tuple(item.migration_id for item in typed_state.applied_migrations)
    descriptors = DOMAIN_MIGRATION_REGISTRY.descriptors[: len(applied_ids)]
    expected_ids = tuple(item.migration_id for item in descriptors)
    if applied_ids != expected_ids:
        raise DomainMigrationPlanningError(
            "bridge-only schema state must be a continuous legacy registry prefix"
        )
    applied_id_set = set(applied_ids)
    for descriptor in descriptors:
        if descriptor.kind != "legacy_bootstrap":
            raise DomainMigrationPlanningError(
                "bridge-only planning refuses non-legacy registry descriptors"
            )
        if any(dependency not in applied_id_set for dependency in descriptor.dependencies):
            raise DomainMigrationPlanningError(
                "bridge-only schema state has an unapplied dependency"
            )

    shape = typed_state.shape
    if shape is SchemaShape.EMPTY and descriptors:
        raise DomainMigrationPlanningError("empty schema state cannot have applied migrations")
    if shape in {SchemaShape.LEGACY_PREFIX, SchemaShape.BRIDGED_PREFIX} and not descriptors:
        raise DomainMigrationPlanningError(
            "non-empty schema state must have an applied legacy prefix"
        )
    expected_edges: Tuple[DomainMigrationDependencyRow, ...] = ()
    if shape is SchemaShape.BRIDGED_PREFIX:
        expected_edges = tuple(
            sorted(
                DomainMigrationDependencyRow(descriptor.migration_id, dependency)
                for descriptor in descriptors
                for dependency in descriptor.dependencies
            )
        )
    expected_state = _build_schema_state(
        shape,
        descriptors,
        dependency_edges=expected_edges,
    )
    if typed_state != expected_state:
        raise DomainMigrationPlanningError(
            "schema state differs from its canonical registry-bound representation"
        )
    return descriptors


def _schema_state_from_bridge_state(
    bridge_state: DomainMigrationBridgeState,
) -> SchemaState:
    descriptors = DOMAIN_MIGRATION_REGISTRY.descriptors[: len(bridge_state.ledger_rows)]
    if tuple(item.migration_id for item in descriptors) != tuple(
        row.migration_id for row in bridge_state.ledger_rows
    ):
        raise DomainMigrationBridgeIntegrityError(
            "bridge state cannot be represented as a registry-bound schema state"
        )
    if bridge_state.shape is DomainMigrationBridgeShape.LEGACY_PREFIX:
        shape = SchemaShape.SIDECAR_ABSENT
        dependency_edges: Tuple[DomainMigrationDependencyRow, ...] = ()
    elif bridge_state.shape is DomainMigrationBridgeShape.SIDECAR_EMPTY_PREFIX:
        shape = SchemaShape.LEGACY_PREFIX if descriptors else SchemaShape.EMPTY
        dependency_edges = ()
    elif bridge_state.shape is DomainMigrationBridgeShape.BRIDGED_PREFIX:
        shape = SchemaShape.BRIDGED_PREFIX
        dependency_edges = bridge_state.dependency_rows
    else:  # pragma: no cover - Enum exhaustiveness guard.
        raise DomainMigrationBridgeIntegrityError(
            "bridge state has an unsupported schema classification"
        )
    return _build_schema_state(
        shape,
        descriptors,
        dependency_edges=dependency_edges,
    )


def inspect_schema_state(connection: sqlite3.Connection) -> SchemaState:
    """Inspect one canonical bridge-aware state without database mutation.

    Partial sidecars, conflicting metadata, future/holey ledgers, dependency conflicts,
    and owned-schema drift are rejected by the underlying single-snapshot reader rather
    than represented as runnable states.
    """

    bridge_state = read_domain_migration_bridge_state(connection)
    state = _schema_state_from_bridge_state(bridge_state)
    _validate_schema_state(state)
    return state


def _bridge_plan_digest(
    *,
    source: SchemaState,
    target_shape: SchemaShape,
    actions: Sequence[BridgeMigrationAction],
) -> str:
    return _canonical_sha256(
        {
            "actions": [
                {
                    "kind": action.kind.value,
                    "resultShape": action.result_shape.value,
                    "sequence": action.sequence,
                    "sourceShape": action.source_shape.value,
                }
                for action in actions
            ],
            "format": _BRIDGE_PLAN_FORMAT,
            "registrySha256": source.registry_sha256,
            "releaseMode": _BRIDGE_RELEASE_MODE,
            "sourceShape": source.shape.value,
            "sourceStateSha256": source.state_sha256,
            "targetShape": target_shape.value,
        }
    )


def plan_bridge_migrations(state: SchemaState) -> BridgeMigrationPlan:
    """Return the only safe deterministic migration actions for bridge-only phase 1.

    The API deliberately accepts no domain target, SQL, native enablement, or sparse
    policy.  Consequently its closed action enum can express only exact sidecar install,
    exact legacy metadata bootstrap, or a no-op.  A future native/v4 planner requires a
    separate reviewed release contract.
    """

    _validate_schema_state(state)
    actions: Tuple[BridgeMigrationAction, ...]
    if state.shape is SchemaShape.SIDECAR_ABSENT:
        installed_shape = (
            SchemaShape.LEGACY_PREFIX if state.applied_migrations else SchemaShape.EMPTY
        )
        install = BridgeMigrationAction(
            sequence=1,
            kind=BridgeMigrationActionKind.INSTALL_SIDECAR,
            source_shape=SchemaShape.SIDECAR_ABSENT,
            result_shape=installed_shape,
        )
        if state.applied_migrations:
            actions = (
                install,
                BridgeMigrationAction(
                    sequence=2,
                    kind=BridgeMigrationActionKind.BOOTSTRAP_LEGACY_METADATA,
                    source_shape=SchemaShape.LEGACY_PREFIX,
                    result_shape=SchemaShape.BRIDGED_PREFIX,
                ),
            )
            target_shape = SchemaShape.BRIDGED_PREFIX
        else:
            actions = (install,)
            target_shape = SchemaShape.EMPTY
    elif state.shape is SchemaShape.LEGACY_PREFIX:
        actions = (
            BridgeMigrationAction(
                sequence=1,
                kind=BridgeMigrationActionKind.BOOTSTRAP_LEGACY_METADATA,
                source_shape=SchemaShape.LEGACY_PREFIX,
                result_shape=SchemaShape.BRIDGED_PREFIX,
            ),
        )
        target_shape = SchemaShape.BRIDGED_PREFIX
    elif state.shape in {SchemaShape.EMPTY, SchemaShape.BRIDGED_PREFIX}:
        actions = ()
        target_shape = state.shape
    else:  # pragma: no cover - Enum exhaustiveness guard.
        raise DomainMigrationPlanningError(
            "bridge-only planner encountered an unsupported schema state"
        )

    plan_sha256 = _bridge_plan_digest(
        source=state,
        target_shape=target_shape,
        actions=actions,
    )
    return BridgeMigrationPlan(
        release_mode=_BRIDGE_RELEASE_MODE,
        source_state_sha256=state.state_sha256,
        registry_sha256=state.registry_sha256,
        source_shape=state.shape,
        target_shape=target_shape,
        actions=actions,
        plan_sha256=plan_sha256,
    )


def _validate_bridge_migration_plan(plan: object) -> BridgeMigrationPlan:
    if type(plan) is not BridgeMigrationPlan:
        raise DomainMigrationPlanApplyError(
            "bridge migration applier requires an exact BridgeMigrationPlan"
        )
    typed_plan = plan
    if type(typed_plan.release_mode) is not str or typed_plan.release_mode != _BRIDGE_RELEASE_MODE:
        raise DomainMigrationPlanApplyError("bridge migration plan release mode is unsupported")
    try:
        _require_sha256(
            typed_plan.source_state_sha256,
            "bridge migration plan source state sha256",
        )
        _require_sha256(
            typed_plan.registry_sha256,
            "bridge migration plan registry sha256",
        )
        _require_sha256(
            typed_plan.plan_sha256,
            "bridge migration plan sha256",
        )
    except (TypeError, ValueError) as error:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan contains a non-canonical digest"
        ) from error
    if typed_plan.registry_sha256 != DOMAIN_MIGRATION_REGISTRY.registry_sha256:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan registry digest differs from the trusted registry"
        )
    if type(typed_plan.source_shape) is not SchemaShape:
        raise DomainMigrationPlanApplyError("bridge migration plan source shape is unsupported")
    if type(typed_plan.target_shape) is not SchemaShape:
        raise DomainMigrationPlanApplyError("bridge migration plan target shape is unsupported")
    if type(typed_plan.actions) is not tuple:
        raise DomainMigrationPlanApplyError("bridge migration plan actions must be an exact tuple")

    signatures: List[Tuple[int, BridgeMigrationActionKind, SchemaShape, SchemaShape]] = []
    for action in typed_plan.actions:
        if type(action) is not BridgeMigrationAction:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan contains an unsupported action model"
            )
        if type(action.sequence) is not int:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan action sequence is unsupported"
            )
        if type(action.kind) is not BridgeMigrationActionKind:
            raise DomainMigrationPlanApplyError("bridge migration plan action kind is unsupported")
        if type(action.source_shape) is not SchemaShape:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan action source shape is unsupported"
            )
        if type(action.result_shape) is not SchemaShape:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan action result shape is unsupported"
            )
        signatures.append(
            (
                action.sequence,
                action.kind,
                action.source_shape,
                action.result_shape,
            )
        )

    install_empty = (
        1,
        BridgeMigrationActionKind.INSTALL_SIDECAR,
        SchemaShape.SIDECAR_ABSENT,
        SchemaShape.EMPTY,
    )
    install_legacy = (
        1,
        BridgeMigrationActionKind.INSTALL_SIDECAR,
        SchemaShape.SIDECAR_ABSENT,
        SchemaShape.LEGACY_PREFIX,
    )
    bootstrap = (
        BridgeMigrationActionKind.BOOTSTRAP_LEGACY_METADATA,
        SchemaShape.LEGACY_PREFIX,
        SchemaShape.BRIDGED_PREFIX,
    )
    signature_tuple = tuple(signatures)
    valid_shape = False
    if typed_plan.source_shape is SchemaShape.SIDECAR_ABSENT:
        valid_shape = (
            typed_plan.target_shape is SchemaShape.EMPTY and signature_tuple == (install_empty,)
        ) or (
            typed_plan.target_shape is SchemaShape.BRIDGED_PREFIX
            and signature_tuple
            == (
                install_legacy,
                (2, bootstrap[0], bootstrap[1], bootstrap[2]),
            )
        )
    elif typed_plan.source_shape is SchemaShape.LEGACY_PREFIX:
        valid_shape = typed_plan.target_shape is SchemaShape.BRIDGED_PREFIX and signature_tuple == (
            (1, bootstrap[0], bootstrap[1], bootstrap[2]),
        )
    elif typed_plan.source_shape in {SchemaShape.EMPTY, SchemaShape.BRIDGED_PREFIX}:
        valid_shape = typed_plan.target_shape is typed_plan.source_shape and not signature_tuple
    if not valid_shape:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan action sequence or shape transition is unsupported"
        )
    return typed_plan


def _rollback_bridge_migration_plan_apply(connection: sqlite3.Connection) -> None:
    try:
        transaction_is_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan rollback state could not be inspected"
        ) from error
    if not transaction_is_active:
        raise DomainMigrationPlanApplyError("bridge migration plan transaction ended unexpectedly")
    try:
        connection.execute("ROLLBACK")
    except BaseException as error:
        raise DomainMigrationPlanApplyError("bridge migration plan rollback failed") from error
    try:
        transaction_remains_active = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan rollback state could not be verified"
        ) from error
    if transaction_remains_active:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan rollback did not release its transaction"
        )


def _inspect_bridge_migration_plan_state(
    connection: sqlite3.Connection,
    *,
    phase: str,
) -> SchemaState:
    try:
        return inspect_schema_state(connection)
    except DomainMigrationBridgeIntegrityError as error:
        raise DomainMigrationPlanApplyError(
            f"bridge migration plan {phase} schema state is not exact"
        ) from error


def _expected_bridge_migration_plan_target_state(
    source: SchemaState,
    target_shape: SchemaShape,
) -> SchemaState:
    descriptors = _validate_schema_state(source)
    dependency_edges: Tuple[DomainMigrationDependencyRow, ...] = ()
    if target_shape is SchemaShape.BRIDGED_PREFIX:
        dependency_edges = tuple(
            sorted(
                DomainMigrationDependencyRow(descriptor.migration_id, dependency)
                for descriptor in descriptors
                for dependency in descriptor.dependencies
            )
        )
    elif target_shape is not SchemaShape.EMPTY:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan target shape cannot be materialized"
        )
    return _build_schema_state(
        target_shape,
        descriptors,
        dependency_edges=dependency_edges,
    )


def apply_bridge_migration_plan(
    connection: sqlite3.Connection,
    plan: BridgeMigrationPlan,
    *,
    clock: Callable[[], str] = utc_now,
) -> SchemaState:
    """Apply one exact source-bound bridge plan in a single writer transaction.

    The plan is re-derived from the locked durable state before any DDL or DML.  Only
    the sidecar installer and legacy metadata bootstrap kernels are reachable, so native,
    v4, sparse, caller-supplied SQL, and partial target execution remain impossible.
    """

    exact_plan = _validate_bridge_migration_plan(plan)
    if not callable(clock):
        raise DomainMigrationPlanApplyError("bridge migration plan clock must be callable")
    try:
        caller_has_transaction = connection.in_transaction
    except sqlite3.Error as error:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan connection state could not be inspected"
        ) from error
    if caller_has_transaction:
        raise DomainMigrationPlanApplyError(
            "bridge migration plan requires no active caller transaction"
        )

    owns_transaction = False
    expected_state: Optional[SchemaState] = None
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            owns_transaction = connection.in_transaction
        if not owns_transaction:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan did not acquire a transaction"
            )

        locked_state = _inspect_bridge_migration_plan_state(
            connection,
            phase="locked",
        )
        if locked_state.state_sha256 != exact_plan.source_state_sha256:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan source state is stale under the write lock"
            )

        canonical_plan = plan_bridge_migrations(locked_state)
        if exact_plan.plan_sha256 != canonical_plan.plan_sha256:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan digest differs from the canonical locked plan"
            )
        if exact_plan != canonical_plan:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan differs from the canonical locked plan"
            )
        expected_state = _expected_bridge_migration_plan_target_state(
            locked_state,
            exact_plan.target_shape,
        )

        for action in exact_plan.actions:
            if action.kind is BridgeMigrationActionKind.INSTALL_SIDECAR:
                _install_domain_migration_sidecar_locked(connection)
            elif action.kind is BridgeMigrationActionKind.BOOTSTRAP_LEGACY_METADATA:
                _bootstrap_state, changed = _bootstrap_legacy_domain_migration_metadata_locked(
                    connection,
                    clock=clock,
                )
                if not changed:
                    raise DomainMigrationPlanApplyError(
                        "bridge migration plan bootstrap action made no state transition"
                    )
            else:  # pragma: no cover - Exact model validation is exhaustive.
                raise DomainMigrationPlanApplyError(
                    "bridge migration plan action kind is unsupported"
                )

        post_write_state = _inspect_bridge_migration_plan_state(
            connection,
            phase="post-write",
        )
        if post_write_state != expected_state:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan post-write state differs from expectation"
            )

        connection.execute("COMMIT")
        if connection.in_transaction:
            raise DomainMigrationPlanApplyError(
                "bridge migration plan commit did not end its transaction"
            )
        owns_transaction = False
    except BaseException:
        if owns_transaction:
            _rollback_bridge_migration_plan_apply(connection)
        raise

    if expected_state is None:  # pragma: no cover - Defensive control-flow guard.
        raise DomainMigrationPlanApplyError(
            "bridge migration plan did not construct an expected state"
        )
    committed_state = _inspect_bridge_migration_plan_state(
        connection,
        phase="committed",
    )
    if committed_state != expected_state:
        raise DomainMigrationPlanApplyError(
            "committed bridge migration plan state differs from expectation"
        )
    return committed_state
