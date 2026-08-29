from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement.result_backup import (
    RESULT_BACKUP_FORMAT,
    ResultBackupExistsError,
    ResultBackupIntegrityError,
    ResultBackupManifest,
    ResultBackupPublicationState,
    create_result_backup,
    recover_result_backup_publication,
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

    def test_nonempty_restore_reopens_and_reconciles_in_a_clean_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"
            self._create_active_source(source)
            create_result_backup(
                source,
                backup,
                clock=lambda: "2026-08-29T12:00:00.000000Z",
            )
            restore_result_backup(backup, restored)

            source_root = Path(__file__).resolve().parents[1]
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import json
import sys
from quantum_entanglement.store import SQLiteEventStore

store = SQLiteEventStore(sys.argv[1], enable_result_acceptance_schema=True)
try:
    identity = store._connection.execute(
        \"SELECT tenant_id, workspace_id, invocation_id FROM invocation_result_receipts\"
    ).fetchone()
    if identity is None:
        raise RuntimeError(\"restored result receipt is missing\")
    reconciliation = store.reconcile_scoped_invocation_result(
        identity[\"tenant_id\"], identity[\"workspace_id\"], identity[\"invocation_id\"]
    )
    if reconciliation is None:
        raise RuntimeError(\"clean-process reconciliation returned no result\")
    observed_after = store.read_scoped_invocation_result_observed_v2(
        identity[\"tenant_id\"], identity[\"workspace_id\"], identity[\"invocation_id\"]
    )
    if reconciliation.observed != observed_after:
        raise RuntimeError(\"clean-process reconciliation did not preserve observation\")
    print(json.dumps({\"reconciliation\": reconciliation.outcome.value, \"stable\": True}))
finally:
    store.close()
""",
                    str(restored),
                ],
                cwd=source_root,
                env={
                    **os.environ,
                    "PYTHONPATH": str(source_root / "src"),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(
                json.loads(child.stdout),
                {"reconciliation": "reconciled", "stable": True},
            )

    def test_backup_uses_a_consistent_snapshot_while_a_second_connection_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            backup = root / "backup.sqlite3"
            self._create_active_source(source)

            writer = sqlite3.connect(source, isolation_level=None, timeout=5)
            writer.row_factory = sqlite3.Row
            try:
                original = writer.execute(
                    "SELECT accepted_at FROM invocation_result_receipts"
                ).fetchone()
                self.assertIsNotNone(original)
                assert original is not None
                writer.execute("BEGIN IMMEDIATE")
                event_ids = writer.execute(
                    "SELECT result_event_id, terminal_event_id FROM invocation_result_receipts"
                ).fetchone()
                self.assertIsNotNone(event_ids)
                assert event_ids is not None
                writer.execute(
                    """
                    UPDATE invocation_result_receipts
                    SET accepted_at = ?, result_event_timestamp = ?, terminal_event_timestamp = ?
                    """,
                    (
                        "2026-08-29T12:34:56.000000Z",
                        "2026-08-29T12:34:56.000000Z",
                        "2026-08-29T12:34:56.000000Z",
                    ),
                )
                writer.execute(
                    "UPDATE events SET timestamp = ? WHERE event_id IN (?, ?)",
                    (
                        "2026-08-29T12:34:56.000000Z",
                        event_ids["result_event_id"],
                        event_ids["terminal_event_id"],
                    ),
                )

                # The backup reader must see the last committed snapshot, not the
                # uncommitted write held by this independent connection.
                create_result_backup(
                    source,
                    backup,
                    clock=lambda: "2026-08-29T12:00:00.000000Z",
                )
                writer.rollback()
            finally:
                writer.close()

            verify_result_backup(backup)
            backup_connection = sqlite3.connect(backup)
            try:
                copied = backup_connection.execute(
                    "SELECT accepted_at FROM invocation_result_receipts"
                ).fetchone()
            finally:
                backup_connection.close()
            self.assertEqual(copied[0], original["accepted_at"])

    @unittest.skipUnless(
        hasattr(os, "kill") and hasattr(signal, "SIGKILL"),
        "requires POSIX",
    )
    def test_sigkill_at_each_result_backup_publication_boundary_is_recoverable(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        child_code = """
import os
import signal
import sys
import quantum_entanglement.result_backup as result_backup

kill_after = int(sys.argv[3])
real_link = result_backup.os.link
calls = 0

def kill_after_boundary(source, target):
    global calls
    calls += 1
    result = real_link(source, target)
    if calls == kill_after:
        os.kill(os.getpid(), signal.SIGKILL)
    return result

result_backup.os.link = kill_after_boundary
result_backup.create_result_backup(sys.argv[1], sys.argv[2])
"""
        for boundary in (1, 2):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.sqlite3"
                backup = root / "backup.sqlite3"
                manifest = Path(str(backup) + ".manifest.json")
                self._create_active_source(source)
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        child_code,
                        str(source),
                        str(backup),
                        str(boundary),
                    ],
                    cwd=source_root,
                    env={**os.environ, "PYTHONPATH": str(source_root / "src")},
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
                private_temporaries = tuple(
                    path
                    for path in root.iterdir()
                    if path.name.startswith((
                        ".qe-result-backup-",
                        ".qe-result-manifest-",
                    ))
                )
                self.assertGreaterEqual(len(private_temporaries), 2)
                recovered = recover_result_backup_publication(
                    root,
                    backup_path=backup,
                    manifest_path=manifest,
                )
                self.assertEqual(recovered.preserved_temporary_paths, ())
                self.assertEqual(
                    recovered.state,
                    (
                        ResultBackupPublicationState.INCOMPLETE
                        if boundary == 1
                        else ResultBackupPublicationState.COMPLETE
                    ),
                )
                self.assertEqual(
                    tuple(
                        path
                        for path in root.iterdir()
                        if path.name.startswith((
                            ".qe-result-backup-",
                            ".qe-result-manifest-",
                        ))
                    ),
                    (),
                )
                if boundary == 1:
                    self.assertFalse(backup.exists())
                    self.assertTrue(manifest.exists())
                else:
                    verify_result_backup(backup, manifest_path=manifest)

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
