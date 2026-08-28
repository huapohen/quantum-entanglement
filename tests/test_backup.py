import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement.backup as backup_module
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
from quantum_entanglement.delivery import OutboxMessage, OutboxStatus
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.migrations import MIGRATIONS, current_schema_version, migration_text
from quantum_entanglement.projections import SQLiteProjectionOffsetStore
from quantum_entanglement.store import SQLiteEventStore
from quantum_entanglement.tenancy import SQLiteRevocationRevisionGuard, TenantId

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

    def seed_projection_receipt(self):
        projections = SQLiteProjectionOffsetStore(str(self.source), clock=lambda: T0)
        try:
            projections.claim("restore-read-model", "worker-1", lease_seconds=60)
        finally:
            projections.close()
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute(
                """
                INSERT INTO projection_receipts (
                    projection_name, event_id, global_position, applied_at
                ) VALUES (?, ?, ?, ?)
                """,
                ("restore-read-model", "event-1", 1, T0),
            )
            connection.execute(
                """
                UPDATE projection_offsets SET last_global_position = ?
                WHERE projection_name = ?
                """,
                (1, "restore-read-model"),
            )

    def seed_revocation_high_water(self):
        with SQLiteRevocationRevisionGuard(str(self.source)) as guard:
            self.assertTrue(guard.check_and_advance(TenantId("tenant-1"), 3, "a" * 64))

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

    def test_projection_offset_count_is_manifested_and_verified(self):
        projections = SQLiteProjectionOffsetStore(str(self.source), clock=lambda: T0)
        try:
            projections.claim("restore-read-model", "worker-1", lease_seconds=60)
        finally:
            projections.close()

        _backup, _manifest_path, created = self.create_backup()

        self.assertEqual(created.table_counts["projection_offsets"], 1)
        self.assertNotIn("projector_offsets", created.table_counts)

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

    def test_backup_path_replacement_during_verification_is_rejected(self):
        backup, _manifest_path, _created = self.create_backup()
        original_backup = self.root / "original-verified-backup.sqlite3"
        real_sha256_fd = backup_module._sha256_fd
        calls = 0

        def replace_after_initial_digest(descriptor):
            nonlocal calls
            digest = real_sha256_fd(descriptor)
            calls += 1
            if calls == 1:
                backup.rename(original_backup)
                backup.write_bytes(b"operator replacement")
            return digest

        with patch(
            "quantum_entanglement.backup._sha256_fd",
            side_effect=replace_after_initial_digest,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "backup path changed"):
                verify_sqlite_backup(backup)

        self.assertEqual(backup.read_bytes(), b"operator replacement")

    def test_manifest_path_replacement_during_verification_is_rejected(self):
        backup, manifest_path, _created = self.create_backup()
        original_manifest = self.root / "original-backup.manifest.json"
        real_read_fd_limited = backup_module._read_fd_limited
        calls = 0

        def replace_after_initial_read(descriptor, limit):
            nonlocal calls
            value = real_read_fd_limited(descriptor, limit)
            calls += 1
            if calls == 1:
                manifest_path.rename(original_manifest)
                manifest_path.write_text("{}", encoding="utf-8")
            return value

        with patch(
            "quantum_entanglement.backup._read_fd_limited",
            side_effect=replace_after_initial_read,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "manifest path changed"):
                verify_sqlite_backup(backup)

        self.assertEqual(manifest_path.read_text(encoding="utf-8"), "{}")

    def test_backup_in_place_change_during_verification_is_rejected(self):
        backup, _manifest_path, _created = self.create_backup()
        real_database_evidence = backup_module._database_evidence

        def mutate_after_sqlite_read(connection):
            evidence = real_database_evidence(connection)
            with backup.open("ab") as handle:
                handle.write(b"in-place mutation")
            return evidence

        with patch(
            "quantum_entanglement.backup._database_evidence",
            side_effect=mutate_after_sqlite_read,
        ):
            with self.assertRaisesRegex(
                BackupIntegrityError, "changed while it was being verified"
            ):
                verify_sqlite_backup(backup)

    def test_backup_parent_replacement_during_verification_is_rejected(self):
        backup, _manifest_path, _created = self.create_backup()
        backup_parent = backup.parent
        original_parent = self.root / "original-backup-parent"
        real_sha256_fd = backup_module._sha256_fd
        calls = 0

        def replace_parent_after_initial_digest(descriptor):
            nonlocal calls
            digest = real_sha256_fd(descriptor)
            calls += 1
            if calls == 1:
                backup_parent.rename(original_parent)
                backup_parent.mkdir()
            return digest

        with patch(
            "quantum_entanglement.backup._sha256_fd",
            side_effect=replace_parent_after_initial_digest,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "backup directory changed"):
                verify_sqlite_backup(backup)

    def test_verify_rejects_backup_symlink(self):
        backup, _manifest_path, _created = self.create_backup()
        regular_backup = self.root / "regular-backup.sqlite3"
        backup.rename(regular_backup)
        backup.symlink_to(regular_backup)

        with self.assertRaisesRegex(BackupIntegrityError, "symbolic link"):
            verify_sqlite_backup(backup)

    def test_verify_rejects_manifest_symlink(self):
        backup, manifest_path, _created = self.create_backup()
        regular_manifest = self.root / "regular-backup.manifest.json"
        manifest_path.rename(regular_manifest)
        manifest_path.symlink_to(regular_manifest)

        with self.assertRaisesRegex(BackupIntegrityError, "symbolic link"):
            verify_sqlite_backup(backup)

    def test_changed_manifest_counts_are_rejected(self):
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["tableCounts"]["artifact_versions"] = 999
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "table counts"):
            verify_sqlite_backup(backup)

    def test_projection_receipt_count_tampering_is_rejected(self):
        self.seed_projection_receipt()
        backup, manifest_path, created = self.create_backup()
        self.assertEqual(created.table_counts["projection_receipts"], 1)
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["tableCounts"]["projection_receipts"] = 2
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "table counts"):
            verify_sqlite_backup(backup)

    def test_projection_receipt_count_omission_is_rejected(self):
        self.seed_projection_receipt()
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["tableCounts"].pop("projection_receipts")
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "table counts"):
            verify_sqlite_backup(backup)

    def test_revocation_high_water_count_tampering_is_rejected(self):
        self.seed_revocation_high_water()
        backup, manifest_path, created = self.create_backup()
        self.assertEqual(created.table_counts["qe_revocation_high_water"], 1)
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["tableCounts"]["qe_revocation_high_water"] = 2
        manifest_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(BackupIntegrityError, "table counts"):
            verify_sqlite_backup(backup)

    def test_revocation_high_water_count_omission_is_rejected(self):
        self.seed_revocation_high_water()
        backup, manifest_path, _created = self.create_backup()
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["tableCounts"].pop("qe_revocation_high_water")
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

    def test_missing_migration_owned_table_is_rejected_before_backup_publication(self):
        connection = sqlite3.connect(self.source)
        connection.execute("DROP TABLE artifact_versions")
        connection.commit()
        connection.close()
        backup = self.root / "missing-artifact-schema.sqlite3"

        with self.assertRaisesRegex(BackupIntegrityError, "schema differs"):
            create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertFalse(backup.exists())
        self.assertFalse(default_manifest_path(backup).exists())

    def test_weakened_migration_owned_table_is_rejected_before_backup_publication(self):
        connection = sqlite3.connect(self.source)
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("artifact_versions",),
            ).fetchone()[0]
        )
        weakened_sql = table_sql.replace(
            "request_digest TEXT NOT NULL",
            "request_digest TEXT",
        )
        self.assertNotEqual(weakened_sql, table_sql)
        columns = ", ".join(
            f'"{row[1]}"' for row in connection.execute("PRAGMA table_info(artifact_versions)")
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE artifact_versions RENAME TO old_artifact_versions")
        connection.execute(weakened_sql)
        connection.execute(
            f"INSERT INTO artifact_versions ({columns}) SELECT {columns} FROM old_artifact_versions"
        )
        connection.execute("DROP TABLE old_artifact_versions")
        connection.commit()
        connection.close()
        backup = self.root / "weakened-artifact-schema.sqlite3"

        with self.assertRaisesRegex(BackupIntegrityError, "schema differs"):
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

            backup_parent = self.root / "backup-parent"
            backup_parent.mkdir()
            parent_link = self.root / "backup-parent-link"
            parent_link.symlink_to(backup_parent, target_is_directory=True)
            with self.assertRaisesRegex(BackupError, "directory must not be a symbolic link"):
                create_sqlite_backup(
                    self.source,
                    parent_link / "backup.sqlite3",
                    clock=lambda: T0,
                )

            dangling_backup = self.root / "dangling-backup.sqlite3"
            dangling_backup.symlink_to(self.root / "absent.sqlite3")
            with self.assertRaises(BackupExistsError):
                create_sqlite_backup(self.source, dangling_backup, clock=lambda: T0)

    def test_source_path_replacement_is_rejected_before_publication(self):
        backup = self.root / "source-race.sqlite3"
        original_source = self.root / "original-source.sqlite3"
        real_database_evidence = backup_module._database_evidence
        replaced = False

        def replace_source_after_copy(connection):
            nonlocal replaced
            evidence = real_database_evidence(connection)
            if not replaced:
                replaced = True
                self.source.rename(original_source)
                self.source.write_bytes(b"replacement database")
            return evidence

        with patch(
            "quantum_entanglement.backup._database_evidence",
            side_effect=replace_source_after_copy,
        ):
            with self.assertRaisesRegex(BackupError, "source SQLite database path changed"):
                create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertEqual(self.source.read_bytes(), b"replacement database")
        self.assertFalse(backup.exists())
        self.assertFalse(default_manifest_path(backup).exists())

    def test_failed_manifest_link_removes_new_database_link(self):
        backup = self.root / "backup.sqlite3"
        manifest = self.root / "backup.manifest.json"
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FileExistsError("simulated manifest publication race")
            return real_link(source, destination, **kwargs)

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

    def test_failed_publication_never_unlinks_a_replacement_target(self):
        backup = self.root / "replaced-backup.sqlite3"
        manifest = self.root / "replaced-backup.manifest.json"
        real_link = os.link
        calls = 0

        def replace_backup_before_manifest_failure(source, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                backup.unlink()
                backup.write_bytes(b"operator replacement")
                raise FileExistsError("simulated manifest publication race")
            return real_link(source, destination, **kwargs)

        with patch(
            "quantum_entanglement.backup.os.link",
            side_effect=replace_backup_before_manifest_failure,
        ):
            with self.assertRaises(BackupExistsError):
                create_sqlite_backup(
                    self.source,
                    backup,
                    manifest_path=manifest,
                    clock=lambda: T0,
                )

        self.assertEqual(backup.read_bytes(), b"operator replacement")
        self.assertFalse(manifest.exists())

    def test_backup_replaced_during_final_verification_is_rejected(self):
        backup = self.root / "verify-race.sqlite3"
        original_backup = self.root / "original-verify-race.sqlite3"
        manifest = default_manifest_path(backup)
        real_verify = backup_module.verify_sqlite_backup

        def replace_backup_after_verification(*args, **kwargs):
            verified = real_verify(*args, **kwargs)
            backup.rename(original_backup)
            backup.write_bytes(b"operator replacement")
            return verified

        with patch(
            "quantum_entanglement.backup.verify_sqlite_backup",
            side_effect=replace_backup_after_verification,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "published backup path changed"):
                create_sqlite_backup(self.source, backup, clock=lambda: T0)

        self.assertEqual(backup.read_bytes(), b"operator replacement")
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
        self.assertEqual(destination.read_bytes(), backup.read_bytes())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with SQLiteInvocationAttemptStore(str(destination), clock=lambda: T0) as attempts:
            self.assertIsNotNone(attempts.get("invocation-1"))
        with SQLiteArtifactStore(str(destination), clock=lambda: T0) as artifacts:
            item = artifacts.get("tenant-1", "workspace-1", "artifact-1")
            self.assertIsNotNone(item)
            self.assertEqual(item.content, b"# Result\n")

    def test_backup_restore_preserves_nonempty_invocation_admission_receipt(self):
        events = (
            DomainEvent(
                "session:session-2",
                "task.execution_requested",
                {"taskId": "task-2"},
                "actor-1",
                event_id="event-requested-2",
                timestamp=T0,
                idempotency_key="admission:requested:2",
            ),
            DomainEvent(
                "session:session-2",
                "task.status_changed",
                {"taskId": "task-2", "status": "running"},
                "orchestrator",
                event_id="event-running-2",
                timestamp=T0,
                causation_id="event-requested-2",
                idempotency_key="admission:running:2",
            ),
        )
        spec = InvocationJobSpec(
            invocation_id="invocation-2",
            session_id="session-2",
            plan_id="plan-2",
            task_id="task-2",
            agent_id="agent-2",
            idempotency_key="invoke:task-2",
            payload_digest=invocation_payload_digest({"task": "task-2"}),
        )
        with SQLiteEventStore(str(self.source), clock=lambda: T0) as event_store:
            admitted = event_store.append_invocation_admission(
                events,
                spec,
                expected_version=0,
            )

        backup, manifest_path, created = self.create_backup()
        self.assertEqual(created.table_counts["invocation_admissions"], 1)
        self.assertEqual(
            [item["version"] for item in created.migrations],
            list(range(1, len(MIGRATIONS) + 1)),
        )

        destination = self.root / "restore-admission" / "state.sqlite3"
        restored = restore_sqlite_backup(
            backup,
            destination,
            manifest_path=manifest_path,
        )
        self.assertEqual(restored, created)
        self.assertEqual(restored.table_counts["invocation_admissions"], 1)
        with SQLiteEventStore(str(destination), clock=lambda: T0) as restored_store:
            replay = restored_store.append_invocation_admission(
                events,
                spec,
                expected_version=0,
            )
            self.assertEqual(replay, admitted)
            self.assertEqual(
                restored_store._connection.execute(
                    "PRAGMA foreign_key_check('invocation_admissions')"
                ).fetchall(),
                [],
            )

    def test_v3_backup_restores_then_upgrades_to_invocation_admission_migration(self):
        with SQLiteEventStore(str(self.source), clock=lambda: T0):
            pass
        with closing(sqlite3.connect(self.source, isolation_level=None)) as connection:
            connection.executescript(migration_text("0006_native_im_sandbox_provenance.down.sql"))
            connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 6")
            connection.executescript(migration_text("0005_native_im_inbox.down.sql"))
            connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 5")
            connection.executescript(migration_text("0004_invocation_admissions.down.sql"))
            connection.execute("DELETE FROM main.qe_schema_migrations WHERE version = 4")

        backup, manifest_path, created = self.create_backup()
        self.assertEqual([item["version"] for item in created.migrations], [1, 2, 3])
        self.assertNotIn("invocation_admissions", created.table_counts)

        destination = self.root / "restore-v3" / "state.sqlite3"
        restored = restore_sqlite_backup(
            backup,
            destination,
            manifest_path=manifest_path,
        )
        self.assertEqual(restored, created)
        with SQLiteEventStore(str(destination), clock=lambda: T0) as upgraded:
            self.assertEqual(current_schema_version(upgraded._connection), len(MIGRATIONS))
            self.assertEqual(
                upgraded._connection.execute(
                    "SELECT COUNT(*) FROM invocation_admissions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                upgraded._connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_restore_rehearses_outbox_ambiguity_artifact_and_attempt_state(self):
        events = SQLiteEventStore(str(self.source), clock=lambda: T0)
        try:
            _stored_event, stored_messages = events.append_with_outbox(
                DomainEvent(
                    stream_id="session:restore-rehearsal",
                    event_type="task.dispatch.requested",
                    payload={"taskId": "task-restore"},
                    actor_id="orchestrator",
                    timestamp=T0,
                    idempotency_key="event:restore-rehearsal",
                ),
                (
                    OutboxMessage(
                        destination="fake-agent-runtime",
                        payload={"taskId": "task-restore"},
                        headers={"traceparent": "trace-restore"},
                        message_id="message-restore",
                        idempotency_key="outbox:restore-rehearsal",
                        available_at=T0,
                        created_at=T0,
                    ),
                ),
                expected_version=0,
            )
            self.assertEqual(len(stored_messages), 1)
            claimed = events.claim_outbox("publisher-restore", limit=1, lease_seconds=60)
            self.assertEqual(len(claimed), 1)
            lease_token = claimed[0].lease_token
            if lease_token is None:
                self.fail("claimed outbox row has no lease token")
            self.assertTrue(
                events.mark_outbox_ambiguous(
                    "message-restore",
                    lease_token,
                    "callback_timeout",
                    marked_at=T0,
                )
            )
            invocation_lease = self.attempts.claim(
                "invocation-1",
                "worker-restore",
                lease_seconds=60,
            )
            self.assertIsNotNone(invocation_lease)

            backup, manifest_path, created = self.create_backup()
        finally:
            events.close()

        destination = self.root / "restore-all" / "state.sqlite3"
        restored = restore_sqlite_backup(
            backup,
            destination,
            manifest_path=manifest_path,
        )

        self.assertEqual(restored, created)
        self.assertEqual(destination.read_bytes(), backup.read_bytes())
        self.assertEqual(restored.table_counts["outbox"], 1)
        self.assertEqual(restored.table_counts["outbox_ambiguities"], 1)
        self.assertEqual(restored.table_counts["invocation_attempts"], 1)
        self.assertEqual(restored.table_counts["artifact_versions"], 1)
        with SQLiteEventStore(str(destination), clock=lambda: T0) as restored_events:
            self.assertEqual(len(restored_events.read_stream("session:restore-rehearsal")), 1)
            outbox = restored_events.get_outbox("message-restore")
            self.assertIsNotNone(outbox)
            self.assertEqual(outbox.status, OutboxStatus.IN_FLIGHT)
            ambiguities = restored_events.read_outbox_ambiguities()
            self.assertEqual(len(ambiguities), 1)
            self.assertEqual(ambiguities[0].reason_code, "callback_timeout")
        with SQLiteInvocationAttemptStore(str(destination), clock=lambda: T0) as attempts:
            job = attempts.get("invocation-1")
            self.assertIsNotNone(job)
            self.assertEqual(len(attempts.attempts("invocation-1")), 1)
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

    def test_restore_rejects_backup_replacement_after_verification(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-backup-race.sqlite3"
        original_backup = self.root / "original-restore-backup.sqlite3"
        real_verify = backup_module.verify_sqlite_backup

        def replace_backup_after_verification(*args, **kwargs):
            verified = real_verify(*args, **kwargs)
            backup.rename(original_backup)
            backup.write_bytes(b"operator replacement")
            return verified

        with patch(
            "quantum_entanglement.backup.verify_sqlite_backup",
            side_effect=replace_backup_after_verification,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "backup path changed"):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertEqual(backup.read_bytes(), b"operator replacement")
        self.assertFalse(destination.exists())

    def test_restore_rejects_manifest_replacement_after_verification(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-manifest-race.sqlite3"
        original_manifest = self.root / "original-restore.manifest.json"
        real_verify = backup_module.verify_sqlite_backup

        def replace_manifest_after_verification(*args, **kwargs):
            verified = real_verify(*args, **kwargs)
            manifest_path.rename(original_manifest)
            manifest_path.write_text("{}", encoding="utf-8")
            return verified

        with patch(
            "quantum_entanglement.backup.verify_sqlite_backup",
            side_effect=replace_manifest_after_verification,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "manifest path changed"):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertEqual(manifest_path.read_text(encoding="utf-8"), "{}")
        self.assertFalse(destination.exists())

    def test_restore_destination_race_preserves_operator_file(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-destination-race.sqlite3"
        real_copy_fd = backup_module._copy_fd

        def create_destination_after_copy(source_descriptor, destination_descriptor):
            result = real_copy_fd(source_descriptor, destination_descriptor)
            destination.write_bytes(b"operator data")
            return result

        with patch(
            "quantum_entanglement.backup._copy_fd",
            side_effect=create_destination_after_copy,
        ):
            with self.assertRaises(BackupExistsError):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertEqual(destination.read_bytes(), b"operator data")

    def test_restore_cleanup_never_unlinks_replacement_after_publication(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-published-race.sqlite3"
        real_link = os.link

        def replace_destination_after_link(source, target, **kwargs):
            result = real_link(source, target, **kwargs)
            destination.unlink()
            destination.write_bytes(b"operator replacement")
            return result

        with patch(
            "quantum_entanglement.backup.os.link",
            side_effect=replace_destination_after_link,
        ):
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "restore destination path changed",
            ):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertEqual(destination.read_bytes(), b"operator replacement")

    def test_restore_cleanup_never_unlinks_replaced_temporary_file(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-temp-race" / "state.sqlite3"
        real_copy_fd = backup_module._copy_fd
        real_read_only_connection_fd = backup_module._read_only_connection_fd
        replacement_temp = None
        post_replacement_connection_attempts = 0

        def replace_temporary_file_after_copy(source_descriptor, destination_descriptor):
            nonlocal replacement_temp
            result = real_copy_fd(source_descriptor, destination_descriptor)
            candidates = tuple(destination.parent.glob(f".{destination.name}.*.restore-partial"))
            self.assertEqual(len(candidates), 1)
            replacement_temp = candidates[0]
            replacement_temp.unlink()
            replacement_temp.write_bytes(b"operator temporary file")
            return result

        def reject_reopening_unlinked_descriptor(descriptor):
            nonlocal post_replacement_connection_attempts
            if replacement_temp is not None:
                post_replacement_connection_attempts += 1
                raise sqlite3.OperationalError("unable to open database file")
            return real_read_only_connection_fd(descriptor)

        with (
            patch(
                "quantum_entanglement.backup._copy_fd",
                side_effect=replace_temporary_file_after_copy,
            ),
            patch(
                "quantum_entanglement.backup._read_only_connection_fd",
                side_effect=reject_reopening_unlinked_descriptor,
            ),
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "temporary file path changed"):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertIsNotNone(replacement_temp)
        self.assertEqual(replacement_temp.read_bytes(), b"operator temporary file")
        self.assertFalse(destination.exists())
        self.assertEqual(post_replacement_connection_attempts, 0)

    def test_restore_detects_backup_in_place_change_during_copy(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-in-place-race.sqlite3"
        real_copy_fd = backup_module._copy_fd

        def mutate_backup_after_copy(source_descriptor, destination_descriptor):
            result = real_copy_fd(source_descriptor, destination_descriptor)
            with backup.open("ab") as handle:
                handle.write(b"in-place mutation")
            return result

        with patch(
            "quantum_entanglement.backup._copy_fd",
            side_effect=mutate_backup_after_copy,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "backup changed"):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertFalse(destination.exists())

    def test_restore_detects_manifest_in_place_change_during_copy(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-manifest-in-place-race.sqlite3"
        real_copy_fd = backup_module._copy_fd

        def mutate_manifest_after_copy(source_descriptor, destination_descriptor):
            result = real_copy_fd(source_descriptor, destination_descriptor)
            with manifest_path.open("ab") as handle:
                handle.write(b"in-place mutation")
            return result

        with patch(
            "quantum_entanglement.backup._copy_fd",
            side_effect=mutate_manifest_after_copy,
        ):
            with self.assertRaisesRegex(BackupIntegrityError, "manifest changed"):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertFalse(destination.exists())

    def test_restore_detects_destination_parent_replacement_during_copy(self):
        backup, manifest_path, _created = self.create_backup()
        destination = self.root / "restore-parent-race" / "state.sqlite3"
        destination_parent = destination.parent
        original_parent = self.root / "original-restore-parent"
        real_copy_fd = backup_module._copy_fd

        def replace_destination_parent_after_copy(source_descriptor, destination_descriptor):
            result = real_copy_fd(source_descriptor, destination_descriptor)
            destination_parent.rename(original_parent)
            destination_parent.mkdir()
            return result

        with patch(
            "quantum_entanglement.backup._copy_fd",
            side_effect=replace_destination_parent_after_copy,
        ):
            with self.assertRaisesRegex(BackupError, "restore destination directory changed"):
                restore_sqlite_backup(
                    backup,
                    destination,
                    manifest_path=manifest_path,
                )

        self.assertFalse(destination.exists())
        self.assertEqual(tuple(original_parent.iterdir()), ())

    def test_restore_rejects_symlink_destination_parent(self):
        backup, manifest_path, _created = self.create_backup()
        real_parent = self.root / "real-restore-parent"
        real_parent.mkdir()
        parent_link = self.root / "restore-parent-link"
        parent_link.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaisesRegex(BackupError, "directory must not be a symbolic link"):
            restore_sqlite_backup(
                backup,
                parent_link / "state.sqlite3",
                manifest_path=manifest_path,
            )


if __name__ == "__main__":
    unittest.main()
