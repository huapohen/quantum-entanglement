import importlib.resources
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.attempts import SQLiteInvocationAttemptStore
from quantum_entanglement.migrations import MigrationVersionError


class SQLiteMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_artifact_schema_is_applied_and_rollback_is_replayable(self):
        with SQLiteInvocationAttemptStore(self.path) as store:
            self.assertEqual(store.schema_version(), 2)

        connection = sqlite3.connect(self.path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("artifact_blobs", tables)
            self.assertIn("artifact_versions", tables)
            down = (
                importlib.resources.files("quantum_entanglement.migrations")
                .joinpath("0002_artifacts.down.sql")
                .read_text(encoding="utf-8")
            )
            connection.executescript(down)
            remaining = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertNotIn("artifact_blobs", remaining)
            self.assertNotIn("artifact_versions", remaining)
        finally:
            connection.close()

        with SQLiteInvocationAttemptStore(self.path) as reopened:
            self.assertEqual(reopened.schema_version(), 2)

    def test_artifact_blob_constraints_reject_invalid_digest_and_size(self):
        with SQLiteInvocationAttemptStore(self.path):
            pass
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("sha256:" + ("g" * 64), b"data", 4, "2026-08-20T00:00:00Z"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("sha256:" + ("0" * 64), b"data", 3, "2026-08-20T00:00:00Z"),
                )
        finally:
            connection.close()

    def test_database_from_future_binary_is_rejected(self):
        with SQLiteInvocationAttemptStore(self.path):
            pass
        connection = sqlite3.connect(self.path)
        connection.execute(
            """
            INSERT INTO qe_schema_migrations(version, filename, sha256, applied_at)
            VALUES (999, '0999_future.up.sql', ?, '2026-08-20T00:00:00Z')
            """,
            ("0" * 64,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationVersionError):
            SQLiteInvocationAttemptStore(self.path)


if __name__ == "__main__":
    unittest.main()
