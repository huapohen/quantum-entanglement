from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.migrations import current_schema_version
from quantum_entanglement.result_migration_activation import (
    RESULT_ACCEPTANCE_DOMAIN_REGISTRY,
    ResultAcceptanceMigrationIntegrityError,
    ResultAcceptanceMigrationTransactionError,
    activate_result_acceptance_migration,
    read_result_acceptance_migration_state,
    rollback_result_acceptance_migration,
)
from quantum_entanglement.store import SQLiteEventStore

STORE_TIME = "2026-08-29T00:00:00Z"


class ResultMigrationActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "event-store.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_activation_installs_sidecar_metadata_and_native_dependency_edges(self) -> None:
        before_changes = self.store._connection.total_changes
        state = activate_result_acceptance_migration(
            self.store._connection,
            clock=lambda: STORE_TIME,
        )
        self.assertEqual(state.schema_version, 7)
        self.assertEqual(state.applied_migration_ids, tuple(range(1, 8)))
        self.assertEqual(state.registry_sha256, RESULT_ACCEPTANCE_DOMAIN_REGISTRY.registry_sha256)
        self.assertEqual(state.native_metadata_id, 7)
        self.assertEqual(state.dependency_edges, ((4, 1), (6, 5), (7, 1), (7, 2), (7, 4)))
        self.assertGreater(self.store._connection.total_changes, before_changes)
        self.assertEqual(current_schema_version(self.store._connection), 7)
        self.assertEqual(
            tuple(
                tuple(row)
                for row in self.store._connection.execute(
                    "SELECT migration_version, metadata_kind "
                    "FROM qe_schema_migration_metadata ORDER BY migration_version"
                ).fetchall()
            ),
            ((1, "legacy_bootstrap"), (2, "legacy_bootstrap"), (3, "legacy_bootstrap"),
             (4, "legacy_bootstrap"), (5, "legacy_bootstrap"), (6, "legacy_bootstrap"),
             (7, "native")),
        )
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM qe_schema_migration_dependencies"
            ).fetchone()[0],
            5,
        )
        self.assertEqual(
            self.store._connection.execute(
                "SELECT name FROM main.sqlite_master WHERE name = ?",
                ("qe_schema_migration_metadata",),
            ).fetchone()[0],
            "qe_schema_migration_metadata",
        )

    def test_activation_is_idempotent_and_readback_is_timestamp_free(self) -> None:
        first = activate_result_acceptance_migration(
            self.store._connection,
            clock=lambda: STORE_TIME,
        )
        before = self.store._connection.total_changes
        second = activate_result_acceptance_migration(
            self.store._connection,
            clock=lambda: "2026-08-29T00:00:01Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(read_result_acceptance_migration_state(self.store._connection), first)
        self.assertEqual(self.store._connection.total_changes, before)

    def test_activation_rejects_partial_result_catalog_without_repair(self) -> None:
        self.store._connection.execute(
            "CREATE TABLE invocation_result_manifests (sentinel TEXT NOT NULL)"
        )
        with self.assertRaises(ResultAcceptanceMigrationIntegrityError):
            activate_result_acceptance_migration(
                self.store._connection,
                clock=lambda: STORE_TIME,
            )
        self.assertEqual(current_schema_version(self.store._connection), 6)
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT name FROM main.sqlite_master WHERE name = ?",
                ("qe_schema_migration_metadata",),
            ).fetchone()
        )

    def test_activation_rejects_invalid_clock_and_leaves_source_unchanged(self) -> None:
        with self.assertRaises(ResultAcceptanceMigrationIntegrityError):
            activate_result_acceptance_migration(
                self.store._connection,
                clock=lambda: "not-a-timestamp",
            )
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(current_schema_version(self.store._connection), 6)

    def test_empty_rollback_removes_result_schema_and_retains_bridged_sidecar(self) -> None:
        activate_result_acceptance_migration(
            self.store._connection,
            clock=lambda: STORE_TIME,
        )
        state = rollback_result_acceptance_migration(self.store._connection)
        self.assertEqual(state.schema_version, 6)
        self.assertEqual(state.applied_migration_ids, tuple(range(1, 7)))
        self.assertTrue(state.sidecar_present)
        self.assertEqual(current_schema_version(self.store._connection), 6)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM qe_schema_migration_metadata "
                "WHERE migration_version = 7"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM main.sqlite_master "
                "WHERE name LIKE 'invocation_result_%'"
            ).fetchone()[0],
            0,
        )

    def test_nonempty_rollback_is_fail_closed(self) -> None:
        activate_result_acceptance_migration(
            self.store._connection,
            clock=lambda: STORE_TIME,
        )
        self.store._connection.execute(
            "INSERT INTO invocation_result_manifests "
            "(tenant_id, workspace_id, manifest_digest, schema_version, canonical_bytes, "
            "byte_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant-rollback",
                "workspace-rollback",
                "a" * 64,
                2,
                sqlite3.Binary(b"{}"),
                2,
                "2026-08-29T00:00:00.000001Z",
            ),
        )
        with self.assertRaises(ResultAcceptanceMigrationTransactionError):
            rollback_result_acceptance_migration(self.store._connection)
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(current_schema_version(self.store._connection), 7)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM invocation_result_manifests"
            ).fetchone()[0],
            1,
        )

    def test_active_state_rejects_metadata_drift(self) -> None:
        activate_result_acceptance_migration(
            self.store._connection,
            clock=lambda: STORE_TIME,
        )
        self.store._connection.execute(
            "UPDATE qe_schema_migration_metadata SET domain = ? WHERE migration_version = 7",
            ("wrong_domain",),
        )
        with self.assertRaises(ResultAcceptanceMigrationIntegrityError):
            read_result_acceptance_migration_state(self.store._connection)


if __name__ == "__main__":
    unittest.main()
