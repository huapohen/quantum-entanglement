from __future__ import annotations

import sqlite3
import unittest

from quantum_entanglement.result_backup_topology import (
    RESULT_BACKUP_SCHEMA_VERSION,
    RESULT_BACKUP_TOPOLOGY_PROFILE,
    ResultBackupTopologyError,
    derive_result_backup_topology,
)
from quantum_entanglement.store import SQLiteEventStore


class ResultBackupTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteEventStore(
            ":memory:",
            enable_result_acceptance_schema=True,
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_active_opt_in_catalog_derives_exact_topology_without_dml(self) -> None:
        connection = self.store._connection
        before = connection.total_changes
        evidence = derive_result_backup_topology(connection)
        self.assertEqual(connection.total_changes, before)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(evidence.schema_version, RESULT_BACKUP_SCHEMA_VERSION)
        self.assertIn(RESULT_BACKUP_TOPOLOGY_PROFILE, evidence.present_profiles)
        self.assertEqual(
            tuple(item.name for item in evidence.table_counts),
            tuple(item.name for item in evidence.schema_objects if item.object_type == "table"),
        )
        self.assertEqual(
            evidence.to_dict()["topologySha256"],
            evidence.topology_sha256,
        )
        self.assertGreaterEqual(
            next(
                item.row_count
                for item in evidence.table_counts
                if item.name == "invocation_result_receipts"
            ),
            0,
        )

    def test_sqlite_stat1_created_by_analyze_is_ignored_as_statistics(self) -> None:
        connection = self.store._connection
        connection.execute("ANALYZE")
        connection.commit()
        evidence = derive_result_backup_topology(connection)
        self.assertIn(RESULT_BACKUP_TOPOLOGY_PROFILE, evidence.present_profiles)

    def test_requires_an_exact_opt_in_migration_seven_catalog(self) -> None:
        legacy = SQLiteEventStore(":memory:")
        try:
            with self.assertRaises(ResultBackupTopologyError):
                derive_result_backup_topology(legacy._connection)
        finally:
            legacy.close()

    def test_rejects_an_existing_caller_transaction(self) -> None:
        connection = self.store._connection
        connection.execute("BEGIN")
        try:
            with self.assertRaisesRegex(ResultBackupTopologyError, "no active caller transaction"):
                derive_result_backup_topology(connection)
        finally:
            connection.execute("ROLLBACK")

    def test_rejects_unknown_catalog_object_without_repair(self) -> None:
        connection = self.store._connection
        connection.execute("CREATE TABLE rogue_result_backup_object (value TEXT NOT NULL)")
        connection.commit()
        before = connection.total_changes
        with self.assertRaisesRegex(ResultBackupTopologyError, "unknown object"):
            derive_result_backup_topology(connection)
        self.assertEqual(connection.total_changes, before)
        self.assertFalse(connection.in_transaction)

    def test_rejects_partial_trusted_profile(self) -> None:
        connection = self.store._connection
        connection.execute("DROP INDEX idx_invocation_result_receipts_scope")
        connection.commit()
        with self.assertRaisesRegex(ResultBackupTopologyError, "partial profile"):
            derive_result_backup_topology(connection)

    def test_exact_connection_type_is_required(self) -> None:
        class ConnectionSubclass(sqlite3.Connection):
            pass

        with self.assertRaises(TypeError):
            derive_result_backup_topology(ConnectionSubclass(":memory:"))


if __name__ == "__main__":
    unittest.main()
