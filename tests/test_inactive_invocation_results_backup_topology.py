from __future__ import annotations

import unittest

import quantum_entanglement
from quantum_entanglement._inactive_invocation_results_backup_topology import (
    _DESCRIPTOR_DEPENDENCY_PROFILES,
    _INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY,
    _INVOCATION_RESULTS_BACKUP_DEPENDENCIES,
    _INVOCATION_RESULTS_BACKUP_OWNER,
    _INVOCATION_RESULTS_BACKUP_PROFILE,
    _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY,
)
from quantum_entanglement._inactive_invocation_results_migration import (
    _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR,
    _KNOWN_INVOCATION_RESULTS_MIGRATIONS,
)
from quantum_entanglement.backup_topology import (
    BACKUP_TOPOLOGY_REGISTRY,
    EVENT_STORE_CORE_PROFILE,
    backup_schema_ddl_sha256,
)
from quantum_entanglement.domain_migrations import (
    bootstrap_legacy_domain_migration_metadata,
    install_domain_migration_sidecar,
)
from quantum_entanglement.migrations import (
    _sql_statements,
    migration_text,
    validate_sqlite_schema,
)
from quantum_entanglement.store import SQLiteEventStore


def rehearse_inactive_candidate(store: SQLiteEventStore) -> None:
    connection = store._connection
    recorded_at = "2026-08-29T00:00:00Z"
    install_domain_migration_sidecar(connection)
    bootstrap_legacy_domain_migration_metadata(connection, clock=lambda: recorded_at)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _sql_statements(
            migration_text(_INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.filename)
        ):
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO qe_schema_migrations (
                version, filename, sha256, applied_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                7,
                _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.filename,
                _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.sql_sha256,
                recorded_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO qe_schema_migration_metadata (
                migration_version,
                domain,
                domain_version,
                metadata_kind,
                descriptor_sha256,
                owned_schema_sha256,
                recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                "invocation_results",
                1,
                "native",
                _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.descriptor_sha256,
                _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_object_manifest_sha256,
                recorded_at,
            ),
        )
        connection.executemany(
            """
            INSERT INTO qe_schema_migration_dependencies (
                migration_version, depends_on_version
            ) VALUES (7, ?)
            """,
            ((dependency,) for dependency in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.dependencies),
        )
        if (
            validate_sqlite_schema(
                connection,
                migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
            )
            != 7
        ):
            raise AssertionError("inactive candidate rehearsal did not reach schema seven")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


class InactiveInvocationResultsBackupTopologyTests(unittest.TestCase):
    def test_private_profile_is_exact_disabled_candidate_metadata(self) -> None:
        profile = _INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY
        self.assertEqual(_INVOCATION_RESULTS_BACKUP_PROFILE, "qe.domain-migration-0007/1")
        self.assertEqual(_INVOCATION_RESULTS_BACKUP_OWNER, "invocation_results")
        self.assertEqual(profile.name, _INVOCATION_RESULTS_BACKUP_PROFILE)
        self.assertEqual(profile.migration_id, 7)
        self.assertEqual(
            _INVOCATION_RESULTS_BACKUP_DEPENDENCIES,
            tuple(
                sorted(
                    (
                        "qe.domain-migration-0001/1",
                        "qe.domain-migration-0002/1",
                        "qe.domain-migration-0004/1",
                        EVENT_STORE_CORE_PROFILE,
                    ),
                    key=lambda item: item.encode("utf-8"),
                )
            ),
        )
        self.assertEqual(profile.dependencies, _INVOCATION_RESULTS_BACKUP_DEPENDENCIES)
        self.assertEqual(
            _DESCRIPTOR_DEPENDENCY_PROFILES,
            tuple(
                BACKUP_TOPOLOGY_REGISTRY.migration_profile(migration_id).name
                for migration_id in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.dependencies
            ),
        )
        self.assertEqual(len(profile.objects), 45)
        self.assertEqual(sum(item.ddl_sha256 is None for item in profile.objects), 31)
        self.assertEqual(
            profile.profile_sha256,
            "402707d9ef31ce878b0556d85173de26b773b67259381ce7342298fe2ece8ffb",
        )
        self.assertNotIn(
            "_INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY",
            quantum_entanglement.__all__,
        )

    def test_explicit_profile_objects_are_exact_descriptor_objects(self) -> None:
        actual = {
            (item.object_type, item.name, item.ddl_sha256)
            for item in _INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY.objects
            if item.ddl_sha256 is not None
        }
        expected = {
            (item.object_type, item.name, item.ddl_sha256)
            for item in _INACTIVE_INVOCATION_RESULTS_DESCRIPTOR.owned_objects
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 14)

    def test_known_registry_adds_only_inactive_seven_and_preserves_active_identity(self) -> None:
        active_names = tuple(profile.name for profile in BACKUP_TOPOLOGY_REGISTRY.profiles)
        known_names = tuple(
            profile.name for profile in _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY.profiles
        )
        self.assertEqual(len(active_names), 11)
        self.assertEqual(len(known_names), 12)
        self.assertNotIn(_INVOCATION_RESULTS_BACKUP_PROFILE, active_names)
        self.assertEqual(set(known_names), {*active_names, _INVOCATION_RESULTS_BACKUP_PROFILE})
        self.assertIs(
            _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY.migration_profile(7),
            _INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY,
        )
        self.assertEqual(
            BACKUP_TOPOLOGY_REGISTRY.registry_sha256,
            "39be33b24cdc79e6bd92ef4fdb5271963be724cf1a4762091d3336aa16e9a495",
        )
        self.assertEqual(
            _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY.registry_sha256,
            "2995f74bc5f5765fd4c75d283a2819c2856b191699e14c86857d146a8fb9548e",
        )
        self.assertEqual(
            len(BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(active_names)),
            88,
        )
        self.assertEqual(
            len(
                _KNOWN_INVOCATION_RESULTS_BACKUP_TOPOLOGY_REGISTRY.objects_for_profiles(known_names)
            ),
            133,
        )

    def test_isolated_rehearsal_catalog_matches_exact_private_profile(self) -> None:
        store = SQLiteEventStore(":memory:")
        try:
            rehearse_inactive_candidate(store)
            catalog_rows = store._connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_schema
                WHERE tbl_name LIKE 'invocation_result_%'
                ORDER BY type, name
                """
            ).fetchall()
        finally:
            store.close()

        actual = {
            (object_type, name): (
                table_name,
                None if schema_sql is None else backup_schema_ddl_sha256(schema_sql),
            )
            for object_type, name, table_name, schema_sql in catalog_rows
        }
        expected = {
            (item.object_type, item.name): (item.table_name, item.ddl_sha256)
            for item in _INACTIVE_INVOCATION_RESULTS_BACKUP_TOPOLOGY.objects
        }
        self.assertEqual(len(actual), 45)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
