import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement import create_sqlite_backup as public_create_sqlite_backup
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
    restore_sqlite_backup,
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

    def test_backup_creation_is_part_of_the_supported_package_api(self):
        self.assertIs(public_create_sqlite_backup, create_sqlite_backup)

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

    def test_manifest_rejects_non_string_identity_and_oversized_input(self):
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["backupId"] = True
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "manifest is malformed"):
            verify_sqlite_backup(backup)

        manifest_path.write_text("{}" + (" " * (1024 * 1024)), encoding="utf-8")
        with self.assertRaisesRegex(BackupIntegrityError, "manifest is malformed"):
            verify_sqlite_backup(backup)

    def test_future_migration_is_rejected_before_backup_publication(self):
        connection = sqlite3.connect(self.source)
        connection.execute(
            """
            INSERT INTO qe_schema_migrations(version, filename, sha256, applied_at)
            VALUES (999, '0999_future.up.sql', ?, ?)
            """,
            ("0" * 64, T0),
        )
        connection.commit()
        connection.close()
        backup = self.root / "future.sqlite3"

        with self.assertRaisesRegex(BackupIntegrityError, "migration ledger"):
            create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertFalse(backup.exists())
        self.assertFalse(default_manifest_path(backup).exists())

    def test_migration_ledger_gap_is_rejected_before_backup_publication(self):
        connection = sqlite3.connect(self.source)
        connection.execute("DELETE FROM qe_schema_migrations WHERE version = 1")
        connection.commit()
        connection.close()
        backup = self.root / "ledger-gap.sqlite3"

        with self.assertRaisesRegex(BackupIntegrityError, "migration ledger"):
            create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertFalse(backup.exists())
        self.assertFalse(default_manifest_path(backup).exists())

    def test_migration_checksum_drift_is_rejected_before_backup_publication(self):
        connection = sqlite3.connect(self.source)
        connection.execute(
            "UPDATE qe_schema_migrations SET sha256 = ? WHERE version = 2",
            ("0" * 64,),
        )
        connection.commit()
        connection.close()
        backup = self.root / "checksum-drift.sqlite3"

        with self.assertRaisesRegex(BackupIntegrityError, "migration ledger"):
            create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertFalse(backup.exists())
        self.assertFalse(default_manifest_path(backup).exists())

    def test_manifest_cannot_claim_a_future_migration(self):
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["migrations"].append(
            {
                "version": 999,
                "filename": "0999_future.up.sql",
                "sha256": "0" * 64,
                "appliedAt": T0,
            }
        )
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

    def test_verified_backup_restores_to_new_database(self):
        backup, manifest_path, created = self.create_backup()
        destination = self.root / "restore" / "state.sqlite3"

        restored = restore_sqlite_backup(
            backup,
            destination,
            manifest_path=manifest_path,
        )

        self.assertEqual(restored, created)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with SQLiteInvocationAttemptStore(str(destination), clock=lambda: T0) as attempts:
            self.assertIsNotNone(attempts.get("invocation-1"))
        with SQLiteArtifactStore(str(destination), clock=lambda: T0) as artifacts:
            item = artifacts.get("tenant-1", "workspace-1", "artifact-1")
            self.assertIsNotNone(item)
            self.assertEqual(item.content, b"# Result\n")

    def test_restore_never_overwrites_existing_destination(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "occupied.sqlite3"
        destination.write_bytes(b"operator data")

        with self.assertRaises(BackupExistsError):
            restore_sqlite_backup(
                backup,
                destination,
                manifest_path=manifest_path,
            )

        self.assertEqual(destination.read_bytes(), b"operator data")

    def test_restore_publication_race_cleans_partial_destination(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "raced.sqlite3"

        with patch(
            "quantum_entanglement.backup.os.link",
            side_effect=FileExistsError("simulated restore race"),
        ):
            with self.assertRaises(BackupExistsError):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
