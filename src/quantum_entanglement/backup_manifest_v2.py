# ruff: noqa: UP006, UP035, UP045
"""Pure, exact codec for the inactive SQLite backup manifest v2 format.

This module deliberately contains no filesystem or SQLite access.  The active backup
creator, verifier, and restore path continue to use :class:`backup.BackupManifest` and
format ``qe.sqlite-backup/1`` exclusively.  V2 values can therefore be modelled and
round-tripped for compatibility work without making v2 backup or restore reachable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Dict, Iterable, List, Literal, Mapping, Set, Tuple, TypeVar, cast

from .domain_migrations import (
    DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
    DOMAIN_MIGRATION_METADATA_TABLE_NAME,
    DOMAIN_MIGRATION_REGISTRY,
    MAX_DOMAIN_LENGTH,
    MAX_DOMAIN_MIGRATIONS,
    MAX_DOMAIN_VERSION,
    MAX_MIGRATION_DOMAINS,
    MAX_MIGRATION_FILENAME_LENGTH,
    MAX_MIGRATION_ID,
    MAX_SCHEMA_OBJECT_NAME_LENGTH,
    AppliedSchemaMigration,
    DomainMigrationDependencyRow,
    DomainMigrationKind,
    DomainMigrationPlanningError,
    SchemaDomainHead,
    SchemaShape,
    SchemaState,
    plan_bridge_migrations,
)

BACKUP_MANIFEST_V2_FORMAT = "qe.sqlite-backup/2"
MAX_BACKUP_MANIFEST_V2_BYTES = 1024 * 1024

_REGISTRY_TOPOLOGY_FORMAT = "qe.sqlite-backup-registry-topology/1"
_MAX_SQLITE_PAGE_COUNT = (2**32) - 2
_MAX_SQLITE_FILE_BYTES = (2**63) - 1
_MAX_TABLE_COUNTS = 4096
_MAX_REGISTRY_TOPOLOGY_OBJECTS = 8192
_MAX_ROW_COUNT = (2**63) - 1

_BACKUP_ID_PATTERN = re.compile(r"backup_[0-9a-f]{32}\Z")
_DOMAIN_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.up\.sql\Z")
_SCHEMA_OBJECT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CREATED_AT_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_APPLIED_AT_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SCHEMA_OBJECT_TYPES = frozenset(("index", "table", "trigger", "view"))
_SUPPORTED_MIGRATION_KIND = "legacy_bootstrap"

# These tables predate the domain registry.  They remain a bounded compatibility
# vocabulary until their schemas are moved under trusted domain descriptors.  Applied
# descriptor-owned tables are added separately; an unapplied registry table is not
# accepted merely because this binary knows its future descriptor.
_KNOWN_UNREGISTRY_TABLES = frozenset(
    (
        "action_receipts",
        "events",
        "inbox_receipts",
        "outbox",
        "projection_offsets",
        "projection_receipts",
        "qe_revocation_high_water",
        "snapshots",
    )
)
_LEGACY_LEDGER_TABLE_NAME = "qe_schema_migrations"

_T = TypeVar("_T")


def _bounded_tuple(values: Iterable[_T], *, maximum: int, label: str) -> Tuple[_T, ...]:
    """Take a bounded snapshot without exhausting a hostile or infinite iterable."""

    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError(f"{label} must be iterable") from error
    snapshot = tuple(islice(iterator, maximum + 1))
    if len(snapshot) > maximum:
        raise ValueError(f"{label} exceeds the hard limit of {maximum}")
    return snapshot


def _plain_string(value: object, label: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    if not value or len(value) > maximum:
        raise ValueError(f"{label} must contain between 1 and {maximum} characters")
    return value


def _sha256(value: object, label: str) -> str:
    digest = _plain_string(value, label, maximum=64)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be lowercase SHA-256 hex")
    return digest


def _positive_integer(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value <= 0 or value > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _nonnegative_integer(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value < 0 or value > maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return value


def _domain(value: object, label: str) -> str:
    domain = _plain_string(value, label, maximum=MAX_DOMAIN_LENGTH)
    if _DOMAIN_PATTERN.fullmatch(domain) is None:
        raise ValueError(f"{label} must be a canonical lower-snake-case identifier")
    return domain


def _filename(value: object, label: str) -> str:
    filename = _plain_string(value, label, maximum=MAX_MIGRATION_FILENAME_LENGTH)
    if _FILENAME_PATTERN.fullmatch(filename) is None:
        raise ValueError(f"{label} must be a bounded basename ending in .up.sql")
    return filename


def _schema_object_name(value: object, label: str) -> str:
    name = _plain_string(value, label, maximum=MAX_SCHEMA_OBJECT_NAME_LENGTH)
    if _SCHEMA_OBJECT_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"{label} must be a bounded ASCII SQLite identifier")
    return name


def _canonical_timestamp(value: object, label: str, *, created_at: bool) -> str:
    timestamp = _plain_string(value, label, maximum=32)
    pattern = _CREATED_AT_PATTERN if created_at else _APPLIED_AT_PATTERN
    if pattern.fullmatch(timestamp) is None:
        raise ValueError(f"{label} must be a canonical RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical RFC 3339 UTC timestamp") from error
    if created_at:
        normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    else:
        normalized = parsed.astimezone(timezone.utc).isoformat()
    if timestamp != normalized.replace("+00:00", "Z"):
        raise ValueError(f"{label} must be a canonical RFC 3339 UTC timestamp")
    return timestamp


def _require_exact_type(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must be an exact {expected.__name__}")


def _require_canonical_order(
    values: Tuple[_T, ...],
    *,
    key: Any,
    coordinate: Any,
    label: str,
) -> None:
    coordinates = tuple(coordinate(item) for item in values)
    if len(set(coordinates)) != len(coordinates):
        raise ValueError(f"{label} must not contain duplicates")
    if values != tuple(sorted(values, key=key)):
        raise ValueError(f"{label} must use canonical order")


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)[:-1]).hexdigest()


@dataclass(frozen=True)
class BackupManifestV2AppliedMigration:
    migration_id: int
    filename: str
    sql_sha256: str
    domain: str
    domain_version: int
    kind: Literal["legacy_bootstrap"]
    descriptor_sha256: str
    owned_schema_sha256: str
    metadata_recorded: bool
    applied_at: str

    def __post_init__(self) -> None:
        _positive_integer(self.migration_id, "migrationId", maximum=MAX_MIGRATION_ID)
        _filename(self.filename, "migration filename")
        _sha256(self.sql_sha256, "migration SQL sha256")
        _domain(self.domain, "migration domain")
        _positive_integer(
            self.domain_version,
            "migration domainVersion",
            maximum=MAX_DOMAIN_VERSION,
        )
        if type(self.kind) is not str or self.kind != _SUPPORTED_MIGRATION_KIND:
            raise ValueError("migration kind must be legacy_bootstrap while v2 is inactive")
        _sha256(self.descriptor_sha256, "migration descriptor sha256")
        _sha256(self.owned_schema_sha256, "migration owned schema sha256")
        if type(self.metadata_recorded) is not bool:
            raise TypeError("migration metadataRecorded must be an exact boolean")
        _canonical_timestamp(self.applied_at, "migration appliedAt", created_at=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "migrationId": self.migration_id,
            "filename": self.filename,
            "sqlSha256": self.sql_sha256,
            "domain": self.domain,
            "domainVersion": self.domain_version,
            "kind": self.kind,
            "descriptorSha256": self.descriptor_sha256,
            "ownedSchemaSha256": self.owned_schema_sha256,
            "metadataRecorded": self.metadata_recorded,
            "appliedAt": self.applied_at,
        }


@dataclass(frozen=True)
class BackupManifestV2DomainHead:
    domain: str
    domain_version: int
    migration_id: int
    owned_schema_sha256: str
    metadata_recorded: bool

    def __post_init__(self) -> None:
        _domain(self.domain, "domain head domain")
        _positive_integer(
            self.domain_version,
            "domain head domainVersion",
            maximum=MAX_DOMAIN_VERSION,
        )
        _positive_integer(
            self.migration_id,
            "domain head migrationId",
            maximum=MAX_MIGRATION_ID,
        )
        _sha256(self.owned_schema_sha256, "domain head owned schema sha256")
        if type(self.metadata_recorded) is not bool:
            raise TypeError("domain head metadataRecorded must be an exact boolean")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "domainVersion": self.domain_version,
            "migrationId": self.migration_id,
            "ownedSchemaSha256": self.owned_schema_sha256,
            "metadataRecorded": self.metadata_recorded,
        }


@dataclass(frozen=True)
class BackupManifestV2DependencyEdge:
    migration_id: int
    depends_on_migration_id: int

    def __post_init__(self) -> None:
        migration_id = _positive_integer(
            self.migration_id,
            "dependency migrationId",
            maximum=MAX_MIGRATION_ID,
        )
        dependency_id = _positive_integer(
            self.depends_on_migration_id,
            "dependency dependsOnMigrationId",
            maximum=MAX_MIGRATION_ID,
        )
        if migration_id == dependency_id:
            raise ValueError("dependency edge cannot refer to itself")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "migrationId": self.migration_id,
            "dependsOnMigrationId": self.depends_on_migration_id,
        }


@dataclass(frozen=True)
class BackupManifestV2OwnedSchemaDigest:
    domain: str
    owned_schema_sha256: str

    def __post_init__(self) -> None:
        _domain(self.domain, "owned schema digest domain")
        _sha256(self.owned_schema_sha256, "owned schema digest sha256")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "ownedSchemaSha256": self.owned_schema_sha256,
        }


@dataclass(frozen=True)
class BackupManifestV2RegistryObject:
    migration_id: int
    domain: str
    object_type: str
    name: str
    ddl_sha256: str

    def __post_init__(self) -> None:
        _positive_integer(
            self.migration_id,
            "registry object migrationId",
            maximum=MAX_MIGRATION_ID,
        )
        _domain(self.domain, "registry object domain")
        object_type = _plain_string(
            self.object_type,
            "registry object objectType",
            maximum=len("trigger"),
        )
        if object_type not in _SCHEMA_OBJECT_TYPES:
            raise ValueError("registry object objectType is unsupported")
        _schema_object_name(self.name, "registry object name")
        _sha256(self.ddl_sha256, "registry object DDL sha256")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "migrationId": self.migration_id,
            "domain": self.domain,
            "objectType": self.object_type,
            "name": self.name,
            "ddlSha256": self.ddl_sha256,
        }


@dataclass(frozen=True)
class BackupManifestV2TableCount:
    name: str
    row_count: int

    def __post_init__(self) -> None:
        _schema_object_name(self.name, "table count name")
        _nonnegative_integer(self.row_count, "table rowCount", maximum=_MAX_ROW_COUNT)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "rowCount": self.row_count}


@dataclass(frozen=True)
class BackupManifestV2SchemaState:
    sidecar_format: int
    shape: str
    legacy_schema_version: int
    applied_migrations: Tuple[BackupManifestV2AppliedMigration, ...]
    domain_heads: Tuple[BackupManifestV2DomainHead, ...]
    dependency_edges: Tuple[BackupManifestV2DependencyEdge, ...]
    owned_schema_digests: Tuple[BackupManifestV2OwnedSchemaDigest, ...]
    registry_sha256: str
    state_sha256: str

    def __post_init__(self) -> None:
        if type(self.sidecar_format) is not int or self.sidecar_format not in {0, 1}:
            raise ValueError("schemaState sidecarFormat must be exact integer 0 or 1")
        if type(self.shape) is not str:
            raise TypeError("schemaState shape must be a plain string")
        try:
            shape = SchemaShape(self.shape)
        except ValueError as error:
            raise ValueError("schemaState shape is unsupported while v2 is inactive") from error
        _nonnegative_integer(
            self.legacy_schema_version,
            "schemaState legacySchemaVersion",
            maximum=MAX_MIGRATION_ID,
        )
        _sha256(self.registry_sha256, "schemaState registrySha256")
        _sha256(self.state_sha256, "schemaState stateSha256")

        migrations = _bounded_tuple(
            self.applied_migrations,
            maximum=MAX_DOMAIN_MIGRATIONS,
            label="schemaState appliedMigrations",
        )
        heads = _bounded_tuple(
            self.domain_heads,
            maximum=MAX_MIGRATION_DOMAINS,
            label="schemaState domainHeads",
        )
        edges = _bounded_tuple(
            self.dependency_edges,
            maximum=MAX_DOMAIN_MIGRATIONS,
            label="schemaState dependencyEdges",
        )
        digests = _bounded_tuple(
            self.owned_schema_digests,
            maximum=MAX_MIGRATION_DOMAINS,
            label="schemaState ownedSchemaDigests",
        )
        object.__setattr__(self, "applied_migrations", migrations)
        object.__setattr__(self, "domain_heads", heads)
        object.__setattr__(self, "dependency_edges", edges)
        object.__setattr__(self, "owned_schema_digests", digests)

        for migration in migrations:
            _require_exact_type(
                migration,
                BackupManifestV2AppliedMigration,
                "schemaState applied migration",
            )
        for head in heads:
            _require_exact_type(head, BackupManifestV2DomainHead, "schemaState domain head")
        for edge in edges:
            _require_exact_type(
                edge,
                BackupManifestV2DependencyEdge,
                "schemaState dependency edge",
            )
        for digest in digests:
            _require_exact_type(
                digest,
                BackupManifestV2OwnedSchemaDigest,
                "schemaState owned schema digest",
            )

        _require_canonical_order(
            migrations,
            key=lambda item: item.migration_id,
            coordinate=lambda item: item.migration_id,
            label="schemaState appliedMigrations",
        )
        _require_canonical_order(
            heads,
            key=lambda item: (
                item.domain.encode("utf-8"),
                item.domain_version,
                item.migration_id,
            ),
            coordinate=lambda item: item.domain,
            label="schemaState domainHeads",
        )
        _require_canonical_order(
            edges,
            key=lambda item: (item.migration_id, item.depends_on_migration_id),
            coordinate=lambda item: (item.migration_id, item.depends_on_migration_id),
            label="schemaState dependencyEdges",
        )
        _require_canonical_order(
            digests,
            key=lambda item: item.domain.encode("utf-8"),
            coordinate=lambda item: item.domain,
            label="schemaState ownedSchemaDigests",
        )

        state = SchemaState(
            sidecar_format=self.sidecar_format,
            shape=shape,
            legacy_schema_version=self.legacy_schema_version,
            applied_migrations=tuple(
                AppliedSchemaMigration(
                    migration_id=item.migration_id,
                    filename=item.filename,
                    sql_sha256=item.sql_sha256,
                    domain=item.domain,
                    domain_version=item.domain_version,
                    kind=cast(DomainMigrationKind, item.kind),
                    descriptor_sha256=item.descriptor_sha256,
                    owned_schema_sha256=item.owned_schema_sha256,
                    metadata_recorded=item.metadata_recorded,
                )
                for item in migrations
            ),
            domain_heads=tuple(
                SchemaDomainHead(
                    domain=item.domain,
                    domain_version=item.domain_version,
                    migration_id=item.migration_id,
                    owned_schema_sha256=item.owned_schema_sha256,
                    metadata_recorded=item.metadata_recorded,
                )
                for item in heads
            ),
            dependency_edges=tuple(
                DomainMigrationDependencyRow(
                    migration_id=item.migration_id,
                    depends_on_migration_id=item.depends_on_migration_id,
                )
                for item in edges
            ),
            owned_schema_digests=tuple((item.domain, item.owned_schema_sha256) for item in digests),
            registry_sha256=self.registry_sha256,
            state_sha256=self.state_sha256,
        )
        try:
            plan_bridge_migrations(state)
        except DomainMigrationPlanningError as error:
            raise ValueError(
                "schemaState differs from the current canonical bridge-only registry state"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sidecarFormat": self.sidecar_format,
            "shape": self.shape,
            "legacySchemaVersion": self.legacy_schema_version,
            "appliedMigrations": [item.to_dict() for item in self.applied_migrations],
            "domainHeads": [item.to_dict() for item in self.domain_heads],
            "dependencyEdges": [item.to_dict() for item in self.dependency_edges],
            "ownedSchemaDigests": [item.to_dict() for item in self.owned_schema_digests],
            "registrySha256": self.registry_sha256,
            "stateSha256": self.state_sha256,
        }


@dataclass(frozen=True)
class BackupManifestV2RegistryTopology:
    registry_sha256: str
    state_sha256: str
    owned_objects: Tuple[BackupManifestV2RegistryObject, ...]
    table_counts: Tuple[BackupManifestV2TableCount, ...]
    topology_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.registry_sha256, "registryTopology registrySha256")
        _sha256(self.state_sha256, "registryTopology stateSha256")
        _sha256(self.topology_sha256, "registryTopology topologySha256")
        objects = _bounded_tuple(
            self.owned_objects,
            maximum=_MAX_REGISTRY_TOPOLOGY_OBJECTS,
            label="registryTopology ownedObjects",
        )
        counts = _bounded_tuple(
            self.table_counts,
            maximum=_MAX_TABLE_COUNTS,
            label="registryTopology tableCounts",
        )
        object.__setattr__(self, "owned_objects", objects)
        object.__setattr__(self, "table_counts", counts)
        for owned_object in objects:
            _require_exact_type(
                owned_object,
                BackupManifestV2RegistryObject,
                "registryTopology owned object",
            )
        for table_count in counts:
            _require_exact_type(
                table_count,
                BackupManifestV2TableCount,
                "registryTopology table count",
            )
        _require_canonical_order(
            objects,
            key=lambda item: (
                item.migration_id,
                item.object_type.encode("utf-8"),
                item.name.encode("utf-8"),
                item.ddl_sha256,
            ),
            coordinate=lambda item: (item.migration_id, item.object_type, item.name),
            label="registryTopology ownedObjects",
        )
        _require_canonical_order(
            counts,
            key=lambda item: item.name.encode("utf-8"),
            coordinate=lambda item: item.name,
            label="registryTopology tableCounts",
        )
        expected_digest = _canonical_sha256(self._digest_dict())
        if self.topology_sha256 != expected_digest:
            raise ValueError("registryTopology digest differs from its canonical evidence")

    def _digest_dict(self) -> Dict[str, object]:
        return {
            "format": _REGISTRY_TOPOLOGY_FORMAT,
            "ownedObjects": [item.to_dict() for item in self.owned_objects],
            "registrySha256": self.registry_sha256,
            "stateSha256": self.state_sha256,
            "tableCounts": [item.to_dict() for item in self.table_counts],
        }

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = dict(self._digest_dict())
        value["topologySha256"] = self.topology_sha256
        return value


@dataclass(frozen=True)
class BackupManifestV2:
    """An immutable exact ``qe.sqlite-backup/2`` value.

    This is a codec model only.  Passing it to the active backup verifier or restore API
    is unsupported; those APIs deliberately continue to accept ``BackupManifest`` v1.
    """

    format_version: str
    backup_id: str
    created_at: str
    database_sha256: str
    byte_size: int
    page_count: int
    page_size: int
    schema_state: BackupManifestV2SchemaState
    registry_topology: BackupManifestV2RegistryTopology

    def __post_init__(self) -> None:
        if type(self.format_version) is not str or self.format_version != BACKUP_MANIFEST_V2_FORMAT:
            raise ValueError("formatVersion must be exactly qe.sqlite-backup/2")
        backup_id = _plain_string(self.backup_id, "backupId", maximum=39)
        if _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
            raise ValueError("backupId does not match format version 2")
        _canonical_timestamp(self.created_at, "createdAt", created_at=True)
        _sha256(self.database_sha256, "databaseSha256")
        byte_size = _positive_integer(
            self.byte_size,
            "byteSize",
            maximum=_MAX_SQLITE_FILE_BYTES,
        )
        page_count = _positive_integer(
            self.page_count,
            "pageCount",
            maximum=_MAX_SQLITE_PAGE_COUNT,
        )
        page_size = _positive_integer(self.page_size, "pageSize", maximum=65_536)
        if page_size < 512 or page_size & (page_size - 1):
            raise ValueError("pageSize is not supported by SQLite")
        if byte_size != page_count * page_size:
            raise ValueError("backup byteSize does not match its page geometry")
        _require_exact_type(
            self.schema_state,
            BackupManifestV2SchemaState,
            "schemaState",
        )
        _require_exact_type(
            self.registry_topology,
            BackupManifestV2RegistryTopology,
            "registryTopology",
        )
        _validate_registry_topology_binding(self.schema_state, self.registry_topology)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "formatVersion": self.format_version,
            "backupId": self.backup_id,
            "createdAt": self.created_at,
            "databaseSha256": self.database_sha256,
            "byteSize": self.byte_size,
            "pageCount": self.page_count,
            "pageSize": self.page_size,
            "schemaState": self.schema_state.to_dict(),
            "registryTopology": self.registry_topology.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> BackupManifestV2:
        return parse_backup_manifest_v2(value)

    def to_json_bytes(self) -> bytes:
        return encode_backup_manifest_v2(self)

    @classmethod
    def from_json_bytes(cls, value: object) -> BackupManifestV2:
        return decode_backup_manifest_v2(value)


def _expected_registry_objects(
    schema_state: BackupManifestV2SchemaState,
) -> Tuple[BackupManifestV2RegistryObject, ...]:
    descriptors = DOMAIN_MIGRATION_REGISTRY.descriptors[: len(schema_state.applied_migrations)]
    return tuple(
        BackupManifestV2RegistryObject(
            migration_id=descriptor.migration_id,
            domain=descriptor.domain,
            object_type=owned.object_type,
            name=owned.name,
            ddl_sha256=owned.ddl_sha256,
        )
        for descriptor in descriptors
        for owned in descriptor.owned_objects
    )


def _validate_registry_topology_binding(
    schema_state: BackupManifestV2SchemaState,
    topology: BackupManifestV2RegistryTopology,
) -> None:
    if topology.registry_sha256 != schema_state.registry_sha256:
        raise ValueError("registryTopology registry digest differs from schemaState")
    if topology.state_sha256 != schema_state.state_sha256:
        raise ValueError("registryTopology state digest differs from schemaState")
    expected_objects = _expected_registry_objects(schema_state)
    if topology.owned_objects != expected_objects:
        raise ValueError("registryTopology ownedObjects differ from the applied trusted registry")

    applied_table_names = {item.name for item in expected_objects if item.object_type == "table"}
    allowed_table_names = set(_KNOWN_UNREGISTRY_TABLES) | applied_table_names
    required_table_names = set(applied_table_names)
    if schema_state.applied_migrations:
        allowed_table_names.add(_LEGACY_LEDGER_TABLE_NAME)
        required_table_names.add(_LEGACY_LEDGER_TABLE_NAME)
    else:
        # An empty ledger table is valid evidence for an initialized but unapplied store.
        allowed_table_names.add(_LEGACY_LEDGER_TABLE_NAME)
    sidecar_names = {
        DOMAIN_MIGRATION_METADATA_TABLE_NAME,
        DOMAIN_MIGRATION_DEPENDENCIES_TABLE_NAME,
    }
    if schema_state.sidecar_format == 1:
        allowed_table_names.update(sidecar_names)
        required_table_names.update(sidecar_names)

    actual_table_names = {item.name for item in topology.table_counts}
    unexpected = actual_table_names - allowed_table_names
    if unexpected:
        raise ValueError("registryTopology contains unsupported table count evidence")
    missing = required_table_names - actual_table_names
    if missing:
        raise ValueError("registryTopology omits required registry-derived table evidence")


def _exact_dict(value: object, fields: Set[str], label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a plain dictionary")
    typed = cast(Dict[str, Any], value)
    if set(typed) != fields:
        raise ValueError(f"{label} fields do not match format version 2")
    return typed


def _exact_list(value: object, *, maximum: int, label: str) -> List[Any]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a list")
    typed = value
    if len(typed) > maximum:
        raise ValueError(f"{label} exceeds the hard limit of {maximum}")
    return typed


def _parse_applied_migration(value: object) -> BackupManifestV2AppliedMigration:
    raw = _exact_dict(
        value,
        {
            "migrationId",
            "filename",
            "sqlSha256",
            "domain",
            "domainVersion",
            "kind",
            "descriptorSha256",
            "ownedSchemaSha256",
            "metadataRecorded",
            "appliedAt",
        },
        "applied migration",
    )
    return BackupManifestV2AppliedMigration(
        migration_id=raw["migrationId"],
        filename=raw["filename"],
        sql_sha256=raw["sqlSha256"],
        domain=raw["domain"],
        domain_version=raw["domainVersion"],
        kind=raw["kind"],
        descriptor_sha256=raw["descriptorSha256"],
        owned_schema_sha256=raw["ownedSchemaSha256"],
        metadata_recorded=raw["metadataRecorded"],
        applied_at=raw["appliedAt"],
    )


def _parse_domain_head(value: object) -> BackupManifestV2DomainHead:
    raw = _exact_dict(
        value,
        {
            "domain",
            "domainVersion",
            "migrationId",
            "ownedSchemaSha256",
            "metadataRecorded",
        },
        "domain head",
    )
    return BackupManifestV2DomainHead(
        domain=raw["domain"],
        domain_version=raw["domainVersion"],
        migration_id=raw["migrationId"],
        owned_schema_sha256=raw["ownedSchemaSha256"],
        metadata_recorded=raw["metadataRecorded"],
    )


def _parse_dependency_edge(value: object) -> BackupManifestV2DependencyEdge:
    raw = _exact_dict(
        value,
        {"migrationId", "dependsOnMigrationId"},
        "dependency edge",
    )
    return BackupManifestV2DependencyEdge(
        migration_id=raw["migrationId"],
        depends_on_migration_id=raw["dependsOnMigrationId"],
    )


def _parse_owned_schema_digest(value: object) -> BackupManifestV2OwnedSchemaDigest:
    raw = _exact_dict(
        value,
        {"domain", "ownedSchemaSha256"},
        "owned schema digest",
    )
    return BackupManifestV2OwnedSchemaDigest(
        domain=raw["domain"],
        owned_schema_sha256=raw["ownedSchemaSha256"],
    )


def _parse_schema_state(value: object) -> BackupManifestV2SchemaState:
    raw = _exact_dict(
        value,
        {
            "sidecarFormat",
            "shape",
            "legacySchemaVersion",
            "appliedMigrations",
            "domainHeads",
            "dependencyEdges",
            "ownedSchemaDigests",
            "registrySha256",
            "stateSha256",
        },
        "schemaState",
    )
    migrations = _exact_list(
        raw["appliedMigrations"],
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="schemaState appliedMigrations",
    )
    heads = _exact_list(
        raw["domainHeads"],
        maximum=MAX_MIGRATION_DOMAINS,
        label="schemaState domainHeads",
    )
    edges = _exact_list(
        raw["dependencyEdges"],
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="schemaState dependencyEdges",
    )
    digests = _exact_list(
        raw["ownedSchemaDigests"],
        maximum=MAX_MIGRATION_DOMAINS,
        label="schemaState ownedSchemaDigests",
    )
    return BackupManifestV2SchemaState(
        sidecar_format=raw["sidecarFormat"],
        shape=raw["shape"],
        legacy_schema_version=raw["legacySchemaVersion"],
        applied_migrations=tuple(_parse_applied_migration(item) for item in migrations),
        domain_heads=tuple(_parse_domain_head(item) for item in heads),
        dependency_edges=tuple(_parse_dependency_edge(item) for item in edges),
        owned_schema_digests=tuple(_parse_owned_schema_digest(item) for item in digests),
        registry_sha256=raw["registrySha256"],
        state_sha256=raw["stateSha256"],
    )


def _parse_registry_object(value: object) -> BackupManifestV2RegistryObject:
    raw = _exact_dict(
        value,
        {"migrationId", "domain", "objectType", "name", "ddlSha256"},
        "registry object",
    )
    return BackupManifestV2RegistryObject(
        migration_id=raw["migrationId"],
        domain=raw["domain"],
        object_type=raw["objectType"],
        name=raw["name"],
        ddl_sha256=raw["ddlSha256"],
    )


def _parse_table_count(value: object) -> BackupManifestV2TableCount:
    raw = _exact_dict(value, {"name", "rowCount"}, "table count")
    return BackupManifestV2TableCount(name=raw["name"], row_count=raw["rowCount"])


def _parse_registry_topology(value: object) -> BackupManifestV2RegistryTopology:
    raw = _exact_dict(
        value,
        {
            "format",
            "registrySha256",
            "stateSha256",
            "ownedObjects",
            "tableCounts",
            "topologySha256",
        },
        "registryTopology",
    )
    if type(raw["format"]) is not str or raw["format"] != _REGISTRY_TOPOLOGY_FORMAT:
        raise ValueError("registryTopology format is unsupported")
    objects = _exact_list(
        raw["ownedObjects"],
        maximum=_MAX_REGISTRY_TOPOLOGY_OBJECTS,
        label="registryTopology ownedObjects",
    )
    counts = _exact_list(
        raw["tableCounts"],
        maximum=_MAX_TABLE_COUNTS,
        label="registryTopology tableCounts",
    )
    return BackupManifestV2RegistryTopology(
        registry_sha256=raw["registrySha256"],
        state_sha256=raw["stateSha256"],
        owned_objects=tuple(_parse_registry_object(item) for item in objects),
        table_counts=tuple(_parse_table_count(item) for item in counts),
        topology_sha256=raw["topologySha256"],
    )


def parse_backup_manifest_v2(value: object) -> BackupManifestV2:
    """Parse one already-decoded exact v2 dictionary without retaining its containers."""

    raw = _exact_dict(
        value,
        {
            "formatVersion",
            "backupId",
            "createdAt",
            "databaseSha256",
            "byteSize",
            "pageCount",
            "pageSize",
            "schemaState",
            "registryTopology",
        },
        "backup manifest",
    )
    manifest = BackupManifestV2(
        format_version=raw["formatVersion"],
        backup_id=raw["backupId"],
        created_at=raw["createdAt"],
        database_sha256=raw["databaseSha256"],
        byte_size=raw["byteSize"],
        page_count=raw["pageCount"],
        page_size=raw["pageSize"],
        schema_state=_parse_schema_state(raw["schemaState"]),
        registry_topology=_parse_registry_topology(raw["registryTopology"]),
    )
    if len(_canonical_json_bytes(manifest.to_dict())) > MAX_BACKUP_MANIFEST_V2_BYTES:
        raise ValueError("backup manifest v2 exceeds the format size limit")
    return manifest


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError("backup manifest v2 JSON contains duplicate keys")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"backup manifest v2 JSON contains unsupported constant {value}")


def decode_backup_manifest_v2(value: object) -> BackupManifestV2:
    """Decode canonical UTF-8 JSON bytes for v2; alternate encodings fail closed."""

    if type(value) is not bytes:
        raise TypeError("backup manifest v2 JSON must be exact bytes")
    encoded = value
    if not encoded or len(encoded) > MAX_BACKUP_MANIFEST_V2_BYTES:
        raise ValueError("backup manifest v2 JSON has an unsupported byte size")
    try:
        text = encoded.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("backup manifest v2 JSON is malformed") from error
    manifest = parse_backup_manifest_v2(raw)
    canonical = _canonical_json_bytes(manifest.to_dict())
    if encoded != canonical:
        raise ValueError("backup manifest v2 JSON is not canonical")
    return manifest


def encode_backup_manifest_v2(manifest: object) -> bytes:
    """Serialize one exact v2 model to its unique canonical UTF-8 JSON bytes."""

    if type(manifest) is not BackupManifestV2:
        raise TypeError("backup manifest v2 serializer requires an exact BackupManifestV2")
    typed = manifest
    # Reparse a detached dictionary so even hostile low-level mutation of a frozen model
    # cannot be serialized as apparently valid evidence.
    stable = parse_backup_manifest_v2(typed.to_dict())
    if stable != typed:
        raise ValueError("backup manifest v2 model differs from its canonical snapshot")
    encoded = _canonical_json_bytes(stable.to_dict())
    if len(encoded) > MAX_BACKUP_MANIFEST_V2_BYTES:
        raise ValueError("backup manifest v2 exceeds the format size limit")
    return encoded


__all__ = [
    "BACKUP_MANIFEST_V2_FORMAT",
    "MAX_BACKUP_MANIFEST_V2_BYTES",
    "BackupManifestV2",
    "BackupManifestV2AppliedMigration",
    "BackupManifestV2DependencyEdge",
    "BackupManifestV2DomainHead",
    "BackupManifestV2OwnedSchemaDigest",
    "BackupManifestV2RegistryObject",
    "BackupManifestV2RegistryTopology",
    "BackupManifestV2SchemaState",
    "BackupManifestV2TableCount",
    "decode_backup_manifest_v2",
    "encode_backup_manifest_v2",
    "parse_backup_manifest_v2",
]
