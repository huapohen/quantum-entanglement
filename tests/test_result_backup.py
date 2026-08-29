from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.result_backup import (
    RESULT_BACKUP_FORMAT,
    ResultBackupExistsError,
    ResultBackupIntegrityError,
    ResultBackupManifest,
    create_result_backup,
    restore_result_backup,
    verify_result_backup,
)
from quantum_entanglement.result_backup_topology import derive_result_backup_topology
from quantum_entanglement.store import SQLiteEventStore
from tests.test_result_acceptance_durable_prerequisites import (
    ResultAcceptanceDurablePrerequisiteTests,
)


class ResultBackupTests(unittest.TestCase):
    def _create_active_source(self, path: Path) -> None:
        store = SQLiteEventStore(
            str(path),
            clock=lambda: "2026-08-27T10:00:00Z",
            enable_result_acceptance_schema=True,
        )
        helper = ResultAcceptanceDurablePrerequisiteTests(methodName="runTest")
        helper.store = store
        prepared = helper.fresh_prepared()
        store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt_result_backup",
                "event_result_backup",
                "event_terminal_backup",
            ),
        ):
            with store._result_artifact_transaction() as handle:
                with store._persist_result_acceptance_graph_in_owner_transaction(
                    handle,
                    prepared,
                ):
                    pass
        store.close()

    def test_create_verify_and_restore_nonempty_migration7_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"
            self._create_active_source(source)
            manifest = create_result_backup(
                source,
                backup,
                clock=lambda: "2026-08-29T12:00:00.000000Z",
            )
            self.assertEqual(manifest.to_dict()["format"], RESULT_BACKUP_FORMAT)
            self.assertGreater(
                next(
                    item.row_count
                    for item in manifest.topology.table_counts
                    if item.name == "invocation_result_receipts"
                ),
                0,
            )
            verified = verify_result_backup(backup)
            self.assertEqual(verified, manifest)
            restored_manifest = restore_result_backup(backup, restored)
            self.assertEqual(restored_manifest, manifest)
            connection = sqlite3.connect(restored)
            connection.row_factory = sqlite3.Row
            try:
                topology = derive_result_backup_topology(connection)
                self.assertEqual(topology, manifest.topology)
            finally:
                connection.close()

            restored_store = SQLiteEventStore(
                str(restored),
                enable_result_acceptance_schema=True,
            )
            try:
                identity = restored_store._connection.execute(
                    """
                    SELECT tenant_id, workspace_id, invocation_id
                    FROM invocation_result_receipts
                    """
                ).fetchone()
                self.assertIsNotNone(identity)
                assert identity is not None
                reconciled = restored_store.reconcile_scoped_invocation_result(
                    identity["tenant_id"],
                    identity["workspace_id"],
                    identity["invocation_id"],
                )
                self.assertIsNotNone(reconciled)
                assert reconciled is not None
                observed = restored_store.read_scoped_invocation_result_observed_v2(
                    identity["tenant_id"],
                    identity["workspace_id"],
                    identity["invocation_id"],
                )
                self.assertEqual(reconciled.observed, observed)
            finally:
                restored_store.close()

    def test_manifest_round_trip_is_canonical_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            backup = Path(directory) / "backup.sqlite3"
            self._create_active_source(source)
            manifest = create_result_backup(
                source,
                backup,
                clock=lambda: "2026-08-29T12:00:00.000000Z",
            )
            encoded = manifest.to_json_bytes()
            self.assertEqual(ResultBackupManifest.from_json_bytes(encoded), manifest)
            object.__setattr__(manifest.topology, "topology_sha256", "0" * 64)
            with self.assertRaises(ValueError):
                manifest.to_json_bytes()
            manifest_path = Path(str(backup) + ".manifest.json")
            raw = json.loads(manifest_path.read_text())
            raw["databaseSha256"] = "0" * 64
            manifest_path.write_bytes(json.dumps(raw).encode("utf-8"))
            with self.assertRaises(ResultBackupIntegrityError):
                verify_result_backup(backup)

    def test_legacy_source_is_rejected_without_creating_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            backup = root / "backup.sqlite3"
            store = SQLiteEventStore(str(source))
            store.close()
            with self.assertRaises(ResultBackupIntegrityError):
                create_result_backup(source, backup)
            self.assertFalse(backup.exists())
            self.assertFalse(Path(str(backup) + ".manifest.json").exists())

    def test_create_and_restore_never_overwrite_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"
            self._create_active_source(source)
            backup.write_bytes(b"sentinel")
            with self.assertRaises(ResultBackupExistsError):
                create_result_backup(source, backup)
            backup.unlink()
            create_result_backup(source, backup)
            restored.write_bytes(b"sentinel")
            with self.assertRaises(ResultBackupExistsError):
                restore_result_backup(backup, restored)
            self.assertEqual(restored.read_bytes(), b"sentinel")

    def test_database_bytes_tamper_fails_before_topology_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            self._create_active_source(source)
            create_result_backup(source, backup)
            with backup.open("r+b") as stream:
                stream.seek(0)
                first = stream.read(1)
                stream.seek(0)
                stream.write(bytes([first[0] ^ 1]))
            with self.assertRaises(ResultBackupIntegrityError):
                verify_result_backup(backup)


if __name__ == "__main__":
    unittest.main()
