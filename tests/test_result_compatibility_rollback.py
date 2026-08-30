"""Migration-7 compatibility and rollback composition evidence.

This file intentionally exercises only local SQLite primitives.  It composes the
non-empty result backup/restore path with the migration-7 down-migration guard so a
restored production candidate cannot be accidentally downgraded while result rows
are present.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.result_backup import create_result_backup, restore_result_backup
from quantum_entanglement.result_migration_activation import (
    ResultAcceptanceMigrationTransactionError,
    rollback_result_acceptance_migration,
)


class ResultCompatibilityRollbackTests(unittest.TestCase):
    def test_restored_nonempty_migration7_database_keeps_downgrade_guard(self) -> None:
        """A restored non-empty v7 graph stays at v7 and remains rollback-protected."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"

            # Reuse the canonical deterministic non-empty result graph fixture.  It
            # is local-only and does not start a worker or contact an external service.
            from tests.test_result_backup import ResultBackupTests

            fixture = ResultBackupTests(methodName="runTest")
            fixture._create_active_source(source)
            create_result_backup(
                source,
                backup,
                clock=lambda: "2026-08-30T00:00:00.000000Z",
            )
            restore_result_backup(backup, restored)

            connection = sqlite3.connect(restored)
            connection.row_factory = sqlite3.Row
            try:
                result_count = connection.execute(
                    "SELECT count(*) FROM invocation_result_receipts"
                ).fetchone()[0]
                self.assertGreater(result_count, 0)

                with self.assertRaises(ResultAcceptanceMigrationTransactionError):
                    rollback_result_acceptance_migration(connection)

                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM qe_schema_migrations "
                        "WHERE version = 7"
                    ).fetchone()[0],
                    7,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM invocation_result_receipts"
                    ).fetchone()[0],
                    result_count,
                )
                self.assertEqual(
                    connection.execute("PRAGMA foreign_key_check").fetchall(), []
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
