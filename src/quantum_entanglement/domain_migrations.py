# ruff: noqa: UP006, UP035
"""Trusted, deterministic descriptors for domain-scoped SQLite migrations.

This module deliberately contains registry primitives only.  It neither creates the
domain-migration sidecar nor changes the legacy migration runner.  Registry validation is
bounded and side-effect free so future planners and bridge code can reject untrusted
package metadata before opening a transaction.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass
from itertools import islice
from typing import Dict, Iterable, List, Literal, Mapping, Sequence, Set, Tuple, TypeVar, cast

from .migrations import MIGRATIONS, Migration, _expected_schema_objects, migration_text

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

_DOMAIN_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.up\.sql\Z")
_SCHEMA_OBJECT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_OBJECT_TYPES = frozenset(("index", "table", "trigger", "view"))
_MIGRATION_KINDS = frozenset(("legacy_bootstrap", "native"))
_LEDGER_OBJECT = ("table", "qe_schema_migrations")

_OWNED_MANIFEST_FORMAT = "qe.domain-migration-owned-objects/1"
_DESCRIPTOR_FORMAT = "qe.domain-migration-descriptor/1"
_REGISTRY_FORMAT = "qe.domain-migration-registry/1"

_T = TypeVar("_T")


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
                dependencies=(),
                owned_objects=owned_objects,
            )
        )
    return tuple(descriptors)


LEGACY_DOMAIN_MIGRATIONS = _build_legacy_descriptors()
DOMAIN_MIGRATION_REGISTRY = validate_domain_migration_registry(LEGACY_DOMAIN_MIGRATIONS)
