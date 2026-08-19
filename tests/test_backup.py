import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.artifact_store import ArtifactWrite, SQLiteArtifactStore
from quantum_entanglement.attempts import (
    InvocationJobSpec,
    SQLiteInvocationAttemptStore,
    invocation_payload_digest,
)
from quantum_entanglement.backup import (
    BackupError,
    BackupExistsError,
    BackupIntegrityError,
    create_sqlite_backup,
    default_manifest_path,
    verify_sqlite_backup,
)

T0 = "2026-08-20T00:00:00Z"


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "state.sqlite3"
        self.attempts = SQLiteInvocationAttemptStore(str(self.source), clock=lambda: T0)
        self.artifacts = SQLiteArtifactStore(str(self.source), clock=lambda: T0)
        self.attempts.enqueue(
            InvocationJobSpec(
                invocation_id="invocation-1",
                session_id="session-1",
                plan_id="plan-1",
                task_id="task-1",
                agent_id="agent-1",
                idempotency_key="invoke:task-1",
                payload_digest=invocation_payload_digest({"task": "task-1"}),
            )
        )
        self.artifacts.write(
            ArtifactWrite(
                artifact_id="artifact-1",
                tenant_id="tenant-1",
                workspace_id="workspace-1",
                session_id="session-1",
                task_id="task-1",
                name="report.md",
                content=b"# Result\n",
                media_type="text/markdown",
                metadata={"sources": 3},
                created_by="agent-1",
                idempotency_key="artifact:task-1:report",
            )
        )

    def tearDown(self):
        self.artifacts.close()
        self.attempts.close()
        self.tempdir.cleanup()

    def create_backup(self):
        path = self.root / "backups" / "snapshot.sqlite3"
        manifest = create_sqlite_backup(self.source, path, clock=lambda: T0)
        return path, default_manifest_path(path), manifest

    def test_online_backup_captures_wal_state_and_verifies_manifest(self):
        backup, manifest_path, created = self.create_backup()
        verified = verify_sqlite_backup(backup)

        self.assertEqual(verified, created)
        self.assertEqual(created.created_at, "2026-08-20T00:00:00.000000Z")
        self.assertEqual(created.table_counts["invocation_jobs"], 1)
        self.assertEqual(created.table_counts["invocation_attempts"], 0)
        self.assertEqual(created.table_counts["artifact_versions"], 1)
        self.assertEqual(created.table_counts["artifact_blobs"], 1)
        self.assertEqual([item["version"] for item in created.migrations], [1, 2])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        self.assertFalse((backup.parent / (backup.name + "-wal")).exists())
        self.assertFalse((backup.parent / (backup.name + "-shm")).exists())

    def test_existing_target_is_never_overwritten(self):
        backup, manifest_path, first = self.create_backup()
        database_before = backup.read_bytes()
        manifest_before = manifest_path.read_bytes()

        with self.assertRaises(BackupExistsError):
            create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertEqual(backup.read_bytes(), database_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(verify_sqlite_backup(backup), first)

    def test_changed_database_bytes_are_rejected_before_sqlite_read(self):
        backup, _manifest_path, _created = self.create_backup()
        with backup.open("ab") as handle:
            handle.write(b"tamper")

        with self.assertRaisesRegex(BackupIntegrityError, "byte size"):
            verify_sqlite_backup(backup)

    def test_changed_manifest_counts_are_rejected(self):
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["tableCounts"]["artifact_versions"] = 999
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "table counts"):
            verify_sqlite_backup(backup)

    def test_unknown_or_malformed_manifest_fields_fail_closed(self):
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["unexpected"] = True
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "manifest is malformed"):
            verify_sqlite_backup(backup)

    def test_source_and_destination_path_guards(self):
        with self.assertRaises(BackupExistsError):
            create_sqlite_backup(self.source, self.source, clock=lambda: T0)

        missing = self.root / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            create_sqlite_backup(missing, self.root / "missing-backup.sqlite3")

        if hasattr(os, "symlink"):
            link = self.root / "source-link.sqlite3"
            link.symlink_to(self.source)
            with self.assertRaisesRegex(BackupError, "symbolic link"):
                create_sqlite_backup(link, self.root / "link-backup.sqlite3")

    def test_failed_manifest_link_removes_new_database_link(self):
        backup = self.root / "backup.sqlite3"
        manifest = self.root / "backup.manifest.json"
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FileExistsError("simulated manifest publication race")
            return real_link(source, destination)

        with patch("quantum_entanglement.backup.os.link", side_effect=fail_second_link):
            with self.assertRaises(BackupExistsError):
                create_sqlite_backup(
                    self.source,
                    backup,
                    manifest_path=manifest,
                    clock=lambda: T0,
                )

        self.assertFalse(backup.exists())
        self.assertFalse(manifest.exists())


if __name__ == "__main__":
    unittest.main()
