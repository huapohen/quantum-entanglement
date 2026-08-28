"""Private metadata for the disabled invocation-results migration candidate.

Importing this module is side-effect free.  It does not register, plan, apply, inspect, or expose
migration 7 through the legacy bootstrap path.  The independent known registry exists only so M4
tests and backup-topology work can bind exact package metadata before a native executor exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .domain_migrations import (
    LEGACY_DOMAIN_MIGRATIONS,
    DomainMigrationDescriptor,
    DomainMigrationRegistry,
    OwnedSchemaObject,
    validate_domain_migration_registry,
)
from .migrations import (
    MIGRATIONS,
    Migration,
    _expected_schema_objects,
    migration_text,
)

_EVENT_STORE_CORE_COMPONENT = "qe.event-store-core/1"
_INVOCATION_RESULTS_MIGRATION = Migration(7, "0007_invocation_results.up.sql")
_INACTIVE_INVOCATION_RESULTS_MIGRATIONS = (_INVOCATION_RESULTS_MIGRATION,)
_KNOWN_INVOCATION_RESULTS_MIGRATIONS = (*MIGRATIONS, _INVOCATION_RESULTS_MIGRATION)


def _build_invocation_results_descriptor() -> DomainMigrationDescriptor:
    expected_schema = _expected_schema_objects(
        _KNOWN_INVOCATION_RESULTS_MIGRATIONS,
        (_INVOCATION_RESULTS_MIGRATION.version,),
    )
    expected_schema.pop(("table", "qe_schema_migrations"))
    owned_objects = tuple(
        OwnedSchemaObject(
            object_type=object_type,
            name=name,
            ddl_sha256=hashlib.sha256(canonical_sql.encode("utf-8")).hexdigest(),
        )
        for (object_type, name), canonical_sql in sorted(expected_schema.items())
    )
    sql = migration_text(_INVOCATION_RESULTS_MIGRATION.filename)
    return DomainMigrationDescriptor(
        migration_id=7,
        filename=_INVOCATION_RESULTS_MIGRATION.filename,
        sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        domain="invocation_results",
        domain_version=1,
        kind="native",
        dependencies=(1, 2, 4),
        owned_objects=owned_objects,
    )


_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR = _build_invocation_results_descriptor()
_KNOWN_INVOCATION_RESULTS_DOMAIN_REGISTRY: DomainMigrationRegistry = (
    validate_domain_migration_registry(
        (*LEGACY_DOMAIN_MIGRATIONS, _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR),
        packaged_migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
    )
)


@dataclass(frozen=True)
class _InactiveInvocationResultsCandidate:
    """Default-deny package identity; deliberately contains no apply callable."""

    migration: Migration
    descriptor: DomainMigrationDescriptor
    enabled: bool
    component_preconditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not _InactiveInvocationResultsCandidate:
            raise TypeError("inactive result candidate must use the exact private class")
        if type(self.migration) is not Migration:
            raise TypeError("inactive result candidate migration must be exact Migration")
        if type(self.descriptor) is not DomainMigrationDescriptor:
            raise TypeError(
                "inactive result candidate descriptor must be exact DomainMigrationDescriptor"
            )
        if self.descriptor.migration_id != self.migration.version:
            raise ValueError("inactive result candidate identities do not match")
        if self.migration != _INVOCATION_RESULTS_MIGRATION:
            raise ValueError("inactive result candidate migration is not exact")
        if self.descriptor != _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR:
            raise ValueError("inactive result candidate descriptor is not exact")
        if type(self.enabled) is not bool or self.enabled:
            raise ValueError("inactive result candidate must remain disabled")
        if type(self.component_preconditions) is not tuple:
            raise TypeError("inactive result component preconditions must be an exact tuple")
        if self.component_preconditions != (_EVENT_STORE_CORE_COMPONENT,):
            raise ValueError("inactive result component preconditions are not exact")

    @property
    def candidate_sha256(self) -> str:
        """Bind the disabled gate and component prerequisites to exact descriptor metadata."""

        self.__post_init__()
        body = json.dumps(
            {
                "componentPreconditions": list(self.component_preconditions),
                "descriptorSha256": self.descriptor.descriptor_sha256,
                "enabled": self.enabled,
                "filename": self.migration.filename,
                "format": "qe.inactive-domain-migration-candidate/1",
                "migrationId": self.migration.version,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()


_INACTIVE_INVOCATION_RESULTS_CANDIDATE = _InactiveInvocationResultsCandidate(
    migration=_INVOCATION_RESULTS_MIGRATION,
    descriptor=_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
    enabled=False,
    component_preconditions=(_EVENT_STORE_CORE_COMPONENT,),
)
