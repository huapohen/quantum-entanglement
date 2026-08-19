import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from quantum_entanglement.admin_cli import main


class AdminCliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "state.sqlite3"
        connection = sqlite3.connect(self.source)
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('persisted')")
        connection.commit()
        connection.close()
        self.backup = self.root / "snapshot.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def create_backup(self):
        result, stdout, stderr = self.run_cli(
            "--compact",
            "backup",
            "--source",
            str(self.source),
            "--destination",
            str(self.backup),
        )
        self.assertEqual(result, 0, stderr)
        return json.loads(stdout)

    def test_backup_and_verify_emit_stable_json(self):
        created = self.create_backup()

        self.assertTrue(created["ok"])
        self.assertEqual(created["operation"], "backup")
        self.assertEqual(created["paths"]["backup"], str(self.backup))
        self.assertEqual(created["manifest"]["formatVersion"], "qe.sqlite-backup/1")
        result, stdout, stderr = self.run_cli(
            "--compact",
            "verify-backup",
            "--backup",
            str(self.backup),
        )
        self.assertEqual(result, 0, stderr)
        verified = json.loads(stdout)
        self.assertTrue(verified["ok"])
        self.assertEqual(
            verified["manifest"]["databaseSha256"],
            created["manifest"]["databaseSha256"],
        )

    def test_restore_creates_readable_new_database(self):
        self.create_backup()
        restored = self.root / "restored.sqlite3"

        result, stdout, stderr = self.run_cli(
            "--compact",
            "restore-backup",
            "--backup",
            str(self.backup),
            "--destination",
            str(restored),
        )

        self.assertEqual(result, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["paths"]["destination"], str(restored))
        connection = sqlite3.connect(restored)
        value = connection.execute("SELECT value FROM sample").fetchone()[0]
        connection.close()
        self.assertEqual(value, "persisted")

    def test_existing_target_returns_machine_readable_error(self):
        self.create_backup()

        result, stdout, stderr = self.run_cli(
            "--compact",
            "backup",
            "--source",
            str(self.source),
            "--destination",
            str(self.backup),
        )

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "TARGET_EXISTS")

    def test_tampered_backup_returns_integrity_error_without_traceback(self):
        self.create_backup()
        with self.backup.open("ab") as handle:
            handle.write(b"tamper")

        result, stdout, stderr = self.run_cli(
            "--compact",
            "verify-backup",
            "--backup",
            str(self.backup),
        )

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(
            json.loads(stderr)["error"]["code"],
            "BACKUP_INTEGRITY_FAILED",
        )

    def test_missing_source_returns_not_found(self):
        result, stdout, stderr = self.run_cli(
            "--compact",
            "backup",
            "--source",
            str(self.root / "missing.sqlite3"),
            "--destination",
            str(self.backup),
        )

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "FILE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
