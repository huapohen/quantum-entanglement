"""Private backup topology for the disabled invocation-results migration candidate.

Importing this module opens no database and changes no active registry.  It freezes the exact
explicit objects and SQLite autoindexes that an isolated migration-7 rehearsal produces so backup
work can be reviewed before registration or a public backup-v2 path exists.
"""

from __future__ import annotations

from ._inactive_invocation_results_migration import (
    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
)
from .backup_topology import (
    BACKUP_TOPOLOGY_REGISTRY,
    EVENT_STORE_CORE_PROFILE,
    TrustedBackupSchemaObject,
    TrustedBackupTopologyProfile,
    TrustedBackupTopologyRegistry,
)

_INVOCATION_RESULTS_BACKUP_PROFILE = "qe.domain-migration-0007/1"
_INVOCATION_RESULTS_BACKUP_OWNER = "invocation_results"
_DESCRIPTOR_DEPENDENCY_PROFILES = tuple(
    BACKUP_TOPOLOGY_REGISTRY.migration_profile(migration_id).name
    for migration_id in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.dependencies
)
_INVOCATION_RESULTS_BACKUP_DEPENDENCIES = tuple(
    sorted(
        (
            *_DESCRIPTOR_DEPENDENCY_PROFILES,
            EVENT_STORE_CORE_PROFILE,
        ),
        key=lambda item: item.encode("utf-8"),
    )
)

_EXPLICIT_OBJECT_TABLES = {
    ("index", "idx_invocation_result_artifacts_reverse"): "invocation_result_artifacts",
    ("index", "idx_invocation_result_publications_trigger"): "invocation_result_publications",
    ("index", "idx_invocation_result_receipts_attempt"): "invocation_result_receipts",
    ("index", "idx_invocation_result_receipts_manifest"): "invocation_result_receipts",
    ("index", "idx_invocation_result_receipts_request"): "invocation_result_receipts",
    ("index", "idx_invocation_result_receipts_scope"): "invocation_result_receipts",
    ("index", "idx_invocation_result_requests_manifest"): "invocation_result_requests",
    ("index", "idx_invocation_result_requests_scope"): "invocation_result_requests",
    ("table", "invocation_result_artifacts"): "invocation_result_artifacts",
    ("table", "invocation_result_event_bindings"): "invocation_result_event_bindings",
    ("table", "invocation_result_manifests"): "invocation_result_manifests",
    ("table", "invocation_result_publications"): "invocation_result_publications",
    ("table", "invocation_result_receipts"): "invocation_result_receipts",
    ("table", "invocation_result_requests"): "invocation_result_requests",
}

_AUTO_INDEX_TABLES = (
    *((f"sqlite_autoindex_invocation_result_artifacts_{number}", "invocation_result_artifacts")
      for number in range(1, 5)),
    *((
        f"sqlite_autoindex_invocation_result_event_bindings_{number}",
        "invocation_result_event_bindings",
    ) for number in range(1, 5)),
    *((f"sqlite_autoindex_invocation_result_manifests_{number}", "invocation_result_manifests")
      for number in range(1, 3)),
    *((
        f"sqlite_autoindex_invocation_result_publications_{number}",
        "invocation_result_publications",
    ) for number in range(1, 5)),
    *((f"sqlite_autoindex_invocation_result_receipts_{number}", "invocation_result_receipts")
      for number in range(1, 12)),
    *((f"sqlite_autoindex_invocation_result_requests_{number}", "invocation_result_requests")
      for number in range(1, 7)),
)


def _build_invocation_results_backup_profile() -> TrustedBackupTopologyProfile:
    explicit = _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects
    if {(item.object_type, item.name) for item in explicit} != set(_EXPLICIT_OBJECT_TABLES):
        raise RuntimeError("inactive result backup tables differ from the migration descriptor")
    objects = tuple(
        sorted(
            (
                *(
                    TrustedBackupSchemaObject(
                        profile=_INVOCATION_RESULTS_BACKUP_PROFILE,
                        owner=_INVOCATION_RESULTS_BACKUP_OWNER,
                        object_type=item.object_type,
                        name=item.name,
                        table_name=_EXPLICIT_OBJECT_TABLES[(item.object_type, item.name)],
                        ddl_sha256=item.ddl_sha256,
                    )
                    for item in explicit
                ),
                *(
                    TrustedBackupSchemaObject(
                        profile=_INVOCATION_RESULTS_BACKUP_PROFILE,
                        owner=_INVOCATION_RESULTS_BACKUP_OWNER,
                        object_type="index",
                        name=name,
                        table_name=table_name,
                        ddl_sha256=None,
                    )
                    for name, table_name in _AUTO_INDEX_TABLES
                ),
            ),
            key=lambda item: (
                item.object_type.encode("utf-8"),
                item.name.encode("utf-8"),
                item.table_name.encode("utf-8"),
            ),
        )
    )
    return TrustedBackupTopologyProfile(
        name=_INVOCATION_RESULTS_BACKUP_PROFILE,
        migration_id=7,
        dependencies=_INVOCATION_RESULTS_BACKUP_DEPENDENCIES,
        objects=objects,
    )


_INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY = _build_invocation_results_backup_profile()
_KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY = TrustedBackupTopologyRegistry(
    profiles=tuple(
        sorted(
            (*BACKUP_TOPOLOGY_REGISTRY.profiles, _INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY),
            key=lambda item: item.name.encode("utf-8"),
        )
    )
)
