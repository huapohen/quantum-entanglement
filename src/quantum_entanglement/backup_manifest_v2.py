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
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Set, Tuple, TypeVar, cast

from .backup_topology import (
    BACKUP_TOPOLOGY_PROFILE,
    BACKUP_TOPOLOGY_REGISTRY,
    DOMAIN_MIGRATION_SIDECAR_PROFILE,
    EVENT_STORE_CORE_PROFILE,
    LEGACY_MIGRATION_LEDGER_PROFILE,
    PROJECTION_STORE_PROFILE,
    REVOCATION_GUARD_PROFILE,
    TrustedBackupSchemaObject,
)
from .domain_migrations import (
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
_MAX_TOPOLOGY_PROFILES = 64
_MAX_TOPOLOGY_SCHEMA_OBJECTS = 8192
_MAX_ROW_COUNT = (2**63) - 1
_MAX_JSON_INTEGER_DIGITS = 19

_BACKUP_ID_PATTERN = re.compile(r"backup_[0-9a-f]{32}\Z")
_DOMAIN_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.up\.sql\Z")
_OWNER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SCHEMA_OBJECT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CREATED_AT_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_APPLIED_AT_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SCHEMA_OBJECT_TYPES = frozenset(("index", "table", "trigger", "view"))
_SUPPORTED_MIGRATION_KIND = "legacy_bootstrap"
_LEGACY_LEDGER_TABLE_NAME = "qe_schema_migrations"
_SIDECAR_METADATA_TABLE_NAME = "qe_schema_migration_metadata"
_SIDECAR_DEPENDENCIES_TABLE_NAME = "qe_schema_migration_dependencies"
_OPTIONAL_CORE_PROFILES = frozenset(
    (
        EVENT_STORE_CORE_PROFILE,
        PROJECTION_STORE_PROFILE,
        REVOCATION_GUARD_PROFILE,
    )
)

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
class BackupManifestV2TopologyProfile:
    profile: str
    presence: Literal["present"]
    profile_sha256: str

    def __post_init__(self) -> None:
        profile = _plain_string(self.profile, "topology profile", maximum=128)
        try:
            trusted = BACKUP_TOPOLOGY_REGISTRY.profile(profile)
        except ValueError as error:
            raise ValueError("topology profile is not trusted by this binary") from error
        if type(self.presence) is not str or self.presence != "present":
            raise ValueError("topology profile presence must be exactly present")
        _sha256(self.profile_sha256, "topology profile sha256")
        if self.profile_sha256 != trusted.profile_sha256:
            raise ValueError("topology profile digest differs from the trusted registry")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "presence": self.presence,
            "profile": self.profile,
            "profileSha256": self.profile_sha256,
        }


@dataclass(frozen=True)
class BackupManifestV2SchemaObject:
    profile: str
    owner: str
    object_type: str
    name: str
    table_name: str
    ddl_sha256: Optional[str]

    def __post_init__(self) -> None:
        try:
            BACKUP_TOPOLOGY_REGISTRY.profile(self.profile)
        except ValueError as error:
            raise ValueError("schema object profile is not trusted by this binary") from error
        owner = _plain_string(self.owner, "schema object owner", maximum=64)
        if _OWNER_PATTERN.fullmatch(owner) is None:
            raise ValueError("schema object owner must be a canonical identifier")
        object_type = _plain_string(
            self.object_type,
            "schema object objectType",
            maximum=len("trigger"),
        )
        if object_type not in _SCHEMA_OBJECT_TYPES:
            raise ValueError("schema object objectType is unsupported")
        _schema_object_name(self.name, "schema object name")
        _schema_object_name(self.table_name, "schema object tableName")
        if object_type == "table" and self.name != self.table_name:
            raise ValueError("table schema object name must equal tableName")
        if self.ddl_sha256 is None:
            if object_type != "index" or not self.name.startswith("sqlite_autoindex_"):
                raise ValueError("only SQLite autoindexes may have a null DDL digest")
        else:
            _sha256(self.ddl_sha256, "schema object DDL sha256")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "owner": self.owner,
            "objectType": self.object_type,
            "name": self.name,
            "tableName": self.table_name,
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
    topology_profile: str
    topology_registry_sha256: str
    registry_sha256: str
    state_sha256: str
    present_profiles: Tuple[BackupManifestV2TopologyProfile, ...]
    schema_objects: Tuple[BackupManifestV2SchemaObject, ...]
    table_counts: Tuple[BackupManifestV2TableCount, ...]
    topology_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.topology_profile) is not str
            or self.topology_profile != BACKUP_TOPOLOGY_PROFILE
        ):
            raise ValueError("registryTopology topologyProfile is unsupported")
        _sha256(
            self.topology_registry_sha256,
            "registryTopology topologyRegistrySha256",
        )
        if self.topology_registry_sha256 != BACKUP_TOPOLOGY_REGISTRY.registry_sha256:
            raise ValueError("registryTopology registry differs from the trusted topology registry")
        _sha256(self.registry_sha256, "registryTopology registrySha256")
        _sha256(self.state_sha256, "registryTopology stateSha256")
        _sha256(self.topology_sha256, "registryTopology topologySha256")
        profiles = _bounded_tuple(
            self.present_profiles,
            maximum=_MAX_TOPOLOGY_PROFILES,
            label="registryTopology presentProfiles",
        )
        objects = _bounded_tuple(
            self.schema_objects,
            maximum=_MAX_TOPOLOGY_SCHEMA_OBJECTS,
            label="registryTopology schemaObjects",
        )
        counts = _bounded_tuple(
            self.table_counts,
            maximum=_MAX_TABLE_COUNTS,
            label="registryTopology tableCounts",
        )
        object.__setattr__(self, "present_profiles", profiles)
        object.__setattr__(self, "schema_objects", objects)
        object.__setattr__(self, "table_counts", counts)
        for profile in profiles:
            _require_exact_type(
                profile,
                BackupManifestV2TopologyProfile,
                "registryTopology present profile",
            )
        for schema_object in objects:
            _require_exact_type(
                schema_object,
                BackupManifestV2SchemaObject,
                "registryTopology schema object",
            )
        for table_count in counts:
            _require_exact_type(
                table_count,
                BackupManifestV2TableCount,
                "registryTopology table count",
            )
        _require_canonical_order(
            profiles,
            key=lambda item: item.profile.encode("utf-8"),
            coordinate=lambda item: item.profile,
            label="registryTopology presentProfiles",
        )
        _require_canonical_order(
            objects,
            key=lambda item: (
                item.object_type.encode("utf-8"),
                item.name.encode("utf-8"),
                item.table_name.encode("utf-8"),
                item.profile.encode("utf-8"),
            ),
            coordinate=lambda item: (item.object_type, item.name),
            label="registryTopology schemaObjects",
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
            "presentProfiles": [item.to_dict() for item in self.present_profiles],
            "registrySha256": self.registry_sha256,
            "schemaObjects": [item.to_dict() for item in self.schema_objects],
            "stateSha256": self.state_sha256,
            "tableCounts": [item.to_dict() for item in self.table_counts],
            "topologyProfile": self.topology_profile,
            "topologyRegistrySha256": self.topology_registry_sha256,
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
        return _manifest_model_dict(_snapshot_manifest_model(self))

    @classmethod
    def from_dict(cls, value: object) -> BackupManifestV2:
        return parse_backup_manifest_v2(value)

    def to_json_bytes(self) -> bytes:
        return encode_backup_manifest_v2(self)

    @classmethod
    def from_json_bytes(cls, value: object) -> BackupManifestV2:
        return decode_backup_manifest_v2(value)


def _snapshot_applied_migration(
    item: object,
) -> BackupManifestV2AppliedMigration:
    _require_exact_type(
        item,
        BackupManifestV2AppliedMigration,
        "schemaState applied migration",
    )
    typed = cast(BackupManifestV2AppliedMigration, item)
    return BackupManifestV2AppliedMigration(
        migration_id=typed.migration_id,
        filename=typed.filename,
        sql_sha256=typed.sql_sha256,
        domain=typed.domain,
        domain_version=typed.domain_version,
        kind=typed.kind,
        descriptor_sha256=typed.descriptor_sha256,
        owned_schema_sha256=typed.owned_schema_sha256,
        metadata_recorded=typed.metadata_recorded,
        applied_at=typed.applied_at,
    )


def _snapshot_domain_head(item: object) -> BackupManifestV2DomainHead:
    _require_exact_type(item, BackupManifestV2DomainHead, "schemaState domain head")
    typed = cast(BackupManifestV2DomainHead, item)
    return BackupManifestV2DomainHead(
        domain=typed.domain,
        domain_version=typed.domain_version,
        migration_id=typed.migration_id,
        owned_schema_sha256=typed.owned_schema_sha256,
        metadata_recorded=typed.metadata_recorded,
    )


def _snapshot_dependency_edge(
    item: object,
) -> BackupManifestV2DependencyEdge:
    _require_exact_type(
        item,
        BackupManifestV2DependencyEdge,
        "schemaState dependency edge",
    )
    typed = cast(BackupManifestV2DependencyEdge, item)
    return BackupManifestV2DependencyEdge(
        migration_id=typed.migration_id,
        depends_on_migration_id=typed.depends_on_migration_id,
    )


def _snapshot_owned_schema_digest(
    item: object,
) -> BackupManifestV2OwnedSchemaDigest:
    _require_exact_type(
        item,
        BackupManifestV2OwnedSchemaDigest,
        "schemaState owned schema digest",
    )
    typed = cast(BackupManifestV2OwnedSchemaDigest, item)
    return BackupManifestV2OwnedSchemaDigest(
        domain=typed.domain,
        owned_schema_sha256=typed.owned_schema_sha256,
    )


def _snapshot_schema_state(item: object) -> BackupManifestV2SchemaState:
    _require_exact_type(item, BackupManifestV2SchemaState, "schemaState")
    typed = cast(BackupManifestV2SchemaState, item)
    migrations = _bounded_tuple(
        typed.applied_migrations,
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="schemaState appliedMigrations",
    )
    heads = _bounded_tuple(
        typed.domain_heads,
        maximum=MAX_MIGRATION_DOMAINS,
        label="schemaState domainHeads",
    )
    edges = _bounded_tuple(
        typed.dependency_edges,
        maximum=MAX_DOMAIN_MIGRATIONS,
        label="schemaState dependencyEdges",
    )
    digests = _bounded_tuple(
        typed.owned_schema_digests,
        maximum=MAX_MIGRATION_DOMAINS,
        label="schemaState ownedSchemaDigests",
    )
    return BackupManifestV2SchemaState(
        sidecar_format=typed.sidecar_format,
        shape=typed.shape,
        legacy_schema_version=typed.legacy_schema_version,
        applied_migrations=tuple(_snapshot_applied_migration(value) for value in migrations),
        domain_heads=tuple(_snapshot_domain_head(value) for value in heads),
        dependency_edges=tuple(_snapshot_dependency_edge(value) for value in edges),
        owned_schema_digests=tuple(_snapshot_owned_schema_digest(value) for value in digests),
        registry_sha256=typed.registry_sha256,
        state_sha256=typed.state_sha256,
    )


def _snapshot_topology_profile(
    item: object,
) -> BackupManifestV2TopologyProfile:
    _require_exact_type(
        item,
        BackupManifestV2TopologyProfile,
        "registryTopology present profile",
    )
    typed = cast(BackupManifestV2TopologyProfile, item)
    return BackupManifestV2TopologyProfile(
        profile=typed.profile,
        presence=typed.presence,
        profile_sha256=typed.profile_sha256,
    )


def _snapshot_schema_object(item: object) -> BackupManifestV2SchemaObject:
    _require_exact_type(
        item,
        BackupManifestV2SchemaObject,
        "registryTopology schema object",
    )
    typed = cast(BackupManifestV2SchemaObject, item)
    return BackupManifestV2SchemaObject(
        profile=typed.profile,
        owner=typed.owner,
        object_type=typed.object_type,
        name=typed.name,
        table_name=typed.table_name,
        ddl_sha256=typed.ddl_sha256,
    )


def _snapshot_table_count(item: object) -> BackupManifestV2TableCount:
    _require_exact_type(
        item,
        BackupManifestV2TableCount,
        "registryTopology table count",
    )
    typed = cast(BackupManifestV2TableCount, item)
    return BackupManifestV2TableCount(
        name=typed.name,
        row_count=typed.row_count,
    )


def _snapshot_registry_topology(
    item: object,
) -> BackupManifestV2RegistryTopology:
    _require_exact_type(
        item,
        BackupManifestV2RegistryTopology,
        "registryTopology",
    )
    typed = cast(BackupManifestV2RegistryTopology, item)
    profiles = _bounded_tuple(
        typed.present_profiles,
        maximum=_MAX_TOPOLOGY_PROFILES,
        label="registryTopology presentProfiles",
    )
    schema_objects = _bounded_tuple(
        typed.schema_objects,
        maximum=_MAX_TOPOLOGY_SCHEMA_OBJECTS,
        label="registryTopology schemaObjects",
    )
    table_counts = _bounded_tuple(
        typed.table_counts,
        maximum=_MAX_TABLE_COUNTS,
        label="registryTopology tableCounts",
    )
    return BackupManifestV2RegistryTopology(
        topology_profile=typed.topology_profile,
        topology_registry_sha256=typed.topology_registry_sha256,
        registry_sha256=typed.registry_sha256,
        state_sha256=typed.state_sha256,
        present_profiles=tuple(_snapshot_topology_profile(value) for value in profiles),
        schema_objects=tuple(_snapshot_schema_object(value) for value in schema_objects),
        table_counts=tuple(_snapshot_table_count(value) for value in table_counts),
        topology_sha256=typed.topology_sha256,
    )


def _snapshot_manifest_model(item: object) -> BackupManifestV2:
    _require_exact_type(item, BackupManifestV2, "backup manifest")
    typed = cast(BackupManifestV2, item)
    return BackupManifestV2(
        format_version=typed.format_version,
        backup_id=typed.backup_id,
        created_at=typed.created_at,
        database_sha256=typed.database_sha256,
        byte_size=typed.byte_size,
        page_count=typed.page_count,
        page_size=typed.page_size,
        schema_state=_snapshot_schema_state(typed.schema_state),
        registry_topology=_snapshot_registry_topology(typed.registry_topology),
    )


def _manifest_model_dict(item: BackupManifestV2) -> Dict[str, Any]:
    return {
        "formatVersion": item.format_version,
        "backupId": item.backup_id,
        "createdAt": item.created_at,
        "databaseSha256": item.database_sha256,
        "byteSize": item.byte_size,
        "pageCount": item.page_count,
        "pageSize": item.page_size,
        "schemaState": item.schema_state.to_dict(),
        "registryTopology": item.registry_topology.to_dict(),
    }


def _topology_profile_model(profile_name: str) -> BackupManifestV2TopologyProfile:
    trusted = BACKUP_TOPOLOGY_REGISTRY.profile(profile_name)
    return BackupManifestV2TopologyProfile(
        profile=trusted.name,
        presence="present",
        profile_sha256=trusted.profile_sha256,
    )


def _schema_object_model(item: TrustedBackupSchemaObject) -> BackupManifestV2SchemaObject:
    return BackupManifestV2SchemaObject(
        profile=item.profile,
        owner=item.owner,
        object_type=item.object_type,
        name=item.name,
        table_name=item.table_name,
        ddl_sha256=item.ddl_sha256,
    )


def _validate_registry_topology_binding(
    schema_state: BackupManifestV2SchemaState,
    topology: BackupManifestV2RegistryTopology,
) -> None:
    if topology.registry_sha256 != schema_state.registry_sha256:
        raise ValueError("registryTopology registry digest differs from schemaState")
    if topology.state_sha256 != schema_state.state_sha256:
        raise ValueError("registryTopology state digest differs from schemaState")
    present_names = tuple(item.profile for item in topology.present_profiles)
    present_name_set = set(present_names)
    applied_profile_names = tuple(
        BACKUP_TOPOLOGY_REGISTRY.migration_profile(item.migration_id).name
        for item in schema_state.applied_migrations
    )
    allowed_names = set(_OPTIONAL_CORE_PROFILES) | set(applied_profile_names)
    allowed_names.add(LEGACY_MIGRATION_LEDGER_PROFILE)
    if schema_state.sidecar_format == 1:
        allowed_names.add(DOMAIN_MIGRATION_SIDECAR_PROFILE)
    if not present_name_set.issubset(allowed_names):
        raise ValueError("registryTopology contains a future or inapplicable topology profile")

    required_names = set(applied_profile_names)
    if schema_state.applied_migrations:
        required_names.add(LEGACY_MIGRATION_LEDGER_PROFILE)
    if schema_state.sidecar_format == 1:
        required_names.add(DOMAIN_MIGRATION_SIDECAR_PROFILE)
    if not required_names.issubset(present_name_set):
        raise ValueError("registryTopology omits a required schema-state topology profile")

    expected_profiles = tuple(_topology_profile_model(name) for name in present_names)
    if topology.present_profiles != expected_profiles:
        raise ValueError("registryTopology profile evidence differs from the trusted registry")
    for name in present_names:
        trusted = BACKUP_TOPOLOGY_REGISTRY.profile(name)
        if not set(trusted.dependencies).issubset(present_name_set):
            raise ValueError("registryTopology omits a required profile dependency")

    trusted_objects = BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(present_names)
    expected_objects = tuple(_schema_object_model(item) for item in trusted_objects)
    if topology.schema_objects != expected_objects:
        raise ValueError("registryTopology schemaObjects differ from exact trusted profiles")

    expected_table_names = tuple(
        item.name for item in expected_objects if item.object_type == "table"
    )
    actual_table_names = tuple(item.name for item in topology.table_counts)
    if actual_table_names != tuple(
        sorted(expected_table_names, key=lambda item: item.encode("utf-8"))
    ):
        raise ValueError("registryTopology tableCounts differ from present exact schema tables")

    counts = {item.name: item.row_count for item in topology.table_counts}
    migration_count = len(schema_state.applied_migrations)
    if LEGACY_MIGRATION_LEDGER_PROFILE in present_name_set:
        if counts[_LEGACY_LEDGER_TABLE_NAME] != migration_count:
            raise ValueError("migration ledger row count differs from applied migrations")
    if DOMAIN_MIGRATION_SIDECAR_PROFILE in present_name_set:
        expected_metadata_count = (
            migration_count if schema_state.shape == SchemaShape.BRIDGED_PREFIX.value else 0
        )
        if counts[_SIDECAR_METADATA_TABLE_NAME] != expected_metadata_count:
            raise ValueError("sidecar metadata row count differs from schemaState shape")
        if counts[_SIDECAR_DEPENDENCIES_TABLE_NAME] != len(schema_state.dependency_edges):
            raise ValueError("sidecar dependency row count differs from schemaState edges")


def _exact_dict(value: object, fields: Set[str], label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a plain dictionary")
    typed = cast(Dict[str, Any], value)
    keys = tuple(typed)
    if any(type(key) is not str for key in keys):
        raise TypeError(f"{label} keys must be plain strings")
    if set(keys) != fields:
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


def _parse_topology_profile(value: object) -> BackupManifestV2TopologyProfile:
    raw = _exact_dict(
        value,
        {"profile", "presence", "profileSha256"},
        "topology profile",
    )
    return BackupManifestV2TopologyProfile(
        profile=raw["profile"],
        presence=raw["presence"],
        profile_sha256=raw["profileSha256"],
    )


def _parse_schema_object(value: object) -> BackupManifestV2SchemaObject:
    raw = _exact_dict(
        value,
        {"profile", "owner", "objectType", "name", "tableName", "ddlSha256"},
        "schema object",
    )
    return BackupManifestV2SchemaObject(
        profile=raw["profile"],
        owner=raw["owner"],
        object_type=raw["objectType"],
        name=raw["name"],
        table_name=raw["tableName"],
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
            "topologyProfile",
            "topologyRegistrySha256",
            "registrySha256",
            "stateSha256",
            "presentProfiles",
            "schemaObjects",
            "tableCounts",
            "topologySha256",
        },
        "registryTopology",
    )
    if type(raw["format"]) is not str or raw["format"] != _REGISTRY_TOPOLOGY_FORMAT:
        raise ValueError("registryTopology format is unsupported")
    profiles = _exact_list(
        raw["presentProfiles"],
        maximum=_MAX_TOPOLOGY_PROFILES,
        label="registryTopology presentProfiles",
    )
    objects = _exact_list(
        raw["schemaObjects"],
        maximum=_MAX_TOPOLOGY_SCHEMA_OBJECTS,
        label="registryTopology schemaObjects",
    )
    counts = _exact_list(
        raw["tableCounts"],
        maximum=_MAX_TABLE_COUNTS,
        label="registryTopology tableCounts",
    )
    return BackupManifestV2RegistryTopology(
        topology_profile=raw["topologyProfile"],
        topology_registry_sha256=raw["topologyRegistrySha256"],
        registry_sha256=raw["registrySha256"],
        state_sha256=raw["stateSha256"],
        present_profiles=tuple(_parse_topology_profile(item) for item in profiles),
        schema_objects=tuple(_parse_schema_object(item) for item in objects),
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


def _bounded_python_int(value: str) -> int:
    """Small indirection used only after lexical integer admission succeeds."""

    return int(value)


def _parse_bounded_json_int(value: str) -> int:
    """Reject an oversized JSON integer lexically before Python bigint conversion."""

    if type(value) is not str or not value:
        raise ValueError("backup manifest v2 JSON integer token is malformed")
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise ValueError("backup manifest v2 JSON integer token is malformed")
    if len(digits) > 1 and digits.startswith("0"):
        raise ValueError("backup manifest v2 JSON integer token is non-canonical")
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError("backup manifest v2 JSON integer token exceeds the lexical limit")
    lexical_limit = "9223372036854775808" if negative else "9223372036854775807"
    if len(digits) == _MAX_JSON_INTEGER_DIGITS and digits > lexical_limit:
        raise ValueError("backup manifest v2 JSON integer token exceeds the supported range")
    return _bounded_python_int(value)


def _reject_json_float(_value: str) -> None:
    raise ValueError("backup manifest v2 JSON contains an unsupported decimal token")


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
            parse_float=_reject_json_float,
            parse_int=_parse_bounded_json_int,
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
    # Rebuild bounded exact models before walking any collection. This prevents a
    # low-level mutation of a frozen model from supplying an infinite iterable or a
    # subclassed nested value to the serializer.
    stable = _snapshot_manifest_model(typed)
    if stable != typed:
        raise ValueError("backup manifest v2 model differs from its canonical snapshot")
    encoded = _canonical_json_bytes(_manifest_model_dict(stable))
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
    "BackupManifestV2RegistryTopology",
    "BackupManifestV2SchemaObject",
    "BackupManifestV2SchemaState",
    "BackupManifestV2TableCount",
    "BackupManifestV2TopologyProfile",
    "decode_backup_manifest_v2",
    "encode_backup_manifest_v2",
    "parse_backup_manifest_v2",
]
