import sqlite3
import unittest

from quantum_entanglement.migrations import (
    MIGRATIONS,
    apply_sqlite_migrations,
    current_schema_version,
)


class MigrationTargetTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row

    def tearDown(self):
        self.connection.close()

    def test_continuous_prefix_can_initialize_a_component_schema(self):
        version = apply_sqlite_migrations(
            self.connection,
            target_versions=(1,),
            clock=lambda: "2026-08-20T00:00:00Z",
        )

        self.assertEqual(version, 1)
        self.assertEqual(current_schema_version(self.connection), 1)
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'invocation_jobs'"
            ).fetchone()
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'artifact_blobs'"
            ).fetchone()
        )

    def test_target_must_be_a_strict_continuous_registry_prefix(self):
        invalid_targets = ((2,), (1, 1), (2, 1), (True,), (1.0,))
        for target in invalid_targets:
            with self.subTest(target=target):
                with self.assertRaises((TypeError, ValueError)):
                    apply_sqlite_migrations(
                        self.connection,
                        target_versions=target,
                        clock=lambda: "2026-08-20T00:00:00Z",
                    )

        self.assertEqual(tuple(item.version for item in MIGRATIONS[:2]), (1, 2))


if __name__ == "__main__":
    unittest.main()
