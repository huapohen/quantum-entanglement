import hashlib
import inspect
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from quantum_entanglement import SQLiteArtifactStore as PublicSQLiteArtifactStore
from quantum_entanglement import artifact_store as artifact_store_module
from quantum_entanglement.artifact_store import (
    ArtifactConcurrencyError,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactProcessMismatchError,
    ArtifactTooLargeError,
    ArtifactWrite,
    SQLiteArtifactStore,
)

T0 = "2026-08-20T00:00:00Z"


def artifact_write(**changes):
    values = {
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "session_id": "session-a",
        "task_id": "task-a",
        "name": "report.md",
        "content": b"version one",
        "created_by": "agent-a",
        "idempotency_key": "artifact:task-a:report",
        "media_type": "text/markdown",
        "metadata": {"source": "test"},
        "artifact_id": "artifact-a",
    }
    values.update(changes)
    return ArtifactWrite(**values)


def write_artifact_from_process(
    path,
    artifact_id,
    task_id,
    ready_queue,
    start_event,
    result_queue,
):
    store = SQLiteArtifactStore(path, clock=lambda: T0)
    try:
        ready_queue.put(artifact_id)
        if not start_event.wait(timeout=5):
            raise RuntimeError("artifact process barrier timed out")
        item = store.write(
            artifact_write(
                artifact_id=artifact_id,
                task_id=task_id,
                idempotency_key=f"artifact:{task_id}:report",
                content=task_id.encode("utf-8"),
            )
        )
        result_queue.put((artifact_id, item.version))
    finally:
        store.close()


class SQLiteArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.store = SQLiteArtifactStore(self.path, clock=lambda: T0)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def _write_versions(self, count, *, session_id="session-a", name="report.md", prefix="v"):
        for version in range(1, count + 1):
            self.store.write(
                artifact_write(
                    artifact_id=f"artifact-{prefix}-{version}",
                    session_id=session_id,
                    task_id=f"task-{prefix}-{version}",
                    name=name,
                    idempotency_key=f"artifact:{prefix}:{version}",
                    content=f"{prefix}-{version}".encode(),
                )
            )

    def _database_state(self):
        connection = sqlite3.connect(self.path)
        try:
            return (
                connection.execute(
                    "SELECT * FROM artifact_versions ORDER BY artifact_id"
                ).fetchall(),
                connection.execute("SELECT * FROM artifact_blobs ORDER BY digest").fetchall(),
            )
        finally:
            connection.close()

    def test_store_is_part_of_the_supported_package_api(self):
        self.assertIs(PublicSQLiteArtifactStore, SQLiteArtifactStore)

    def test_store_starts_with_immediately_previous_migration_runner_signature(self):
        path = str(Path(self.tempdir.name) / "legacy-runner.sqlite3")
        current_runner = artifact_store_module.apply_sqlite_migrations

        def previous_runner(connection, *, clock):
            if "target_versions" in inspect.signature(current_runner).parameters:
                return current_runner(connection, target_versions=(1, 2), clock=clock)
            return current_runner(connection, clock=clock)

        with patch(
            "quantum_entanglement.artifact_store.apply_sqlite_migrations",
            previous_runner,
        ):
            with SQLiteArtifactStore(path, clock=lambda: T0) as compatible:
                self.assertEqual(compatible.schema_version(), 2)

    def test_content_and_metadata_commit_atomically_with_contiguous_versions(self):
        first = self.store.write(artifact_write(), expected_head_version=0)
        second = self.store.write(
            artifact_write(
                artifact_id="artifact-b",
                task_id="task-b",
                idempotency_key="artifact:task-b:report",
            ),
            expected_head_version=1,
        )

        self.assertEqual((first.version, first.parent_version), (1, None))
        self.assertEqual((second.version, second.parent_version), (2, 1))
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.content, b"version one")
        self.assertEqual(
            self.store.head("tenant-a", "workspace-a", "session-a", "report.md"),
            second,
        )
        self.assertEqual(self.store.verify_scope("tenant-a", "workspace-a"), 2)

        connection = sqlite3.connect(self.path)
        try:
            blob_count = connection.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0]
            self.assertEqual(blob_count, 1)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

    def test_identical_retry_returns_original_but_changed_payload_conflicts(self):
        spec = artifact_write()
        first = self.store.write(spec, expected_head_version=0)
        retried = self.store.write(
            replace(spec, artifact_id="retry-generated-id"),
            expected_head_version=99,
        )

        self.assertEqual(retried, first)
        history = self.store.history("tenant-a", "workspace-a", "session-a", "report.md")
        self.assertEqual(len(history), 1)
        with self.assertRaises(ArtifactConflictError):
            self.store.write(replace(spec, artifact_id="new-id", content=b"changed"))
        with self.assertRaises(ArtifactConflictError):
            self.store.write(
                replace(
                    spec,
                    idempotency_key="different-key",
                    content=b"same-id-new-meaning",
                )
            )

    def test_expected_head_rejects_stale_writer_without_creating_a_blob(self):
        self.store.write(artifact_write(), expected_head_version=0)
        stale = artifact_write(
            artifact_id="artifact-stale",
            task_id="task-stale",
            idempotency_key="artifact:stale",
            content=b"must not persist",
        )
        with self.assertRaises(ArtifactConcurrencyError):
            self.store.write(stale, expected_head_version=0)

        self.assertIsNone(self.store.get("tenant-a", "workspace-a", "artifact-stale"))
        connection = sqlite3.connect(self.path)
        try:
            digest = "sha256:" + hashlib.sha256(stale.content).hexdigest()
            count = connection.execute(
                "SELECT COUNT(*) FROM artifact_blobs WHERE digest = ?", (digest,)
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            connection.close()

    def test_two_connections_allocate_unique_versions(self):
        second = SQLiteArtifactStore(self.path, clock=lambda: T0)
        try:
            specs = (
                artifact_write(
                    artifact_id="artifact-thread-a",
                    task_id="task-thread-a",
                    idempotency_key="artifact:thread-a",
                    content=b"thread-a",
                ),
                artifact_write(
                    artifact_id="artifact-thread-b",
                    task_id="task-thread-b",
                    idempotency_key="artifact:thread-b",
                    content=b"thread-b",
                ),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(self.store.write, specs[0]),
                    executor.submit(second.write, specs[1]),
                )
                versions = sorted(future.result(timeout=3).version for future in futures)
            self.assertEqual(versions, [1, 2])
        finally:
            second.close()

    def test_two_processes_allocate_unique_versions(self):
        context = multiprocessing.get_context("spawn")
        ready_queue = context.Queue()
        result_queue = context.Queue()
        start_event = context.Event()
        processes = [
            context.Process(
                target=write_artifact_from_process,
                args=(
                    self.path,
                    f"artifact-process-{index}",
                    f"task-process-{index}",
                    ready_queue,
                    start_event,
                    result_queue,
                ),
            )
            for index in (1, 2)
        ]
        for process in processes:
            process.start()
        for _ in processes:
            ready_queue.get(timeout=5)
        start_event.set()
        results = [result_queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(sorted(version for _artifact_id, version in results), [1, 2])

    def test_fork_inherited_store_rejects_before_touching_sqlite(self):
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                self.store.schema_version()
            except ArtifactProcessMismatchError:
                os.write(write_fd, b"ok")
            except BaseException:
                os.write(write_fd, b"unexpected")
            else:
                os.write(write_fd, b"missing")
            finally:
                os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        try:
            signal = os.read(read_fd, 32)
        finally:
            os.close(read_fd)
        _, status = os.waitpid(child_pid, 0)
        self.assertEqual(status, 0)
        self.assertEqual(signal, b"ok")

    def test_scope_is_mandatory_and_wrong_scope_is_non_enumerating(self):
        stored = self.store.write(artifact_write())

        self.assertIsNone(self.store.get("tenant-b", "workspace-a", stored.artifact_id))
        self.assertIsNone(self.store.get("tenant-a", "workspace-b", stored.artifact_id))
        self.assertIsNone(self.store.head("tenant-b", "workspace-a", "session-a", "report.md"))
        self.assertEqual(
            self.store.history("tenant-b", "workspace-a", "session-a", "report.md"),
            (),
        )
        with self.assertRaises(ValueError):
            self.store.get("", "workspace-a", stored.artifact_id)

    def test_read_detects_blob_and_metadata_tampering(self):
        stored = self.store.write(artifact_write(content=b"data"))
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE artifact_blobs SET content = ? WHERE digest = ?",
            (b"evil", stored.digest),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(ArtifactIntegrityError):
            self.store.get("tenant-a", "workspace-a", stored.artifact_id)

        metadata_path = str(Path(self.tempdir.name) / "metadata-tamper.sqlite3")
        with SQLiteArtifactStore(metadata_path, clock=lambda: T0) as metadata_store:
            stored = metadata_store.write(artifact_write(content=b"data"))
            connection = sqlite3.connect(metadata_path)
            connection.execute(
                "UPDATE artifact_versions SET metadata_json = ? WHERE artifact_id = ?",
                ('{"tampered":true}', stored.artifact_id),
            )
            connection.commit()
            connection.close()

            with self.assertRaises(ArtifactIntegrityError):
                metadata_store.get("tenant-a", "workspace-a", stored.artifact_id)

    def test_read_rejects_persisted_type_coercion_and_noncanonical_time(self):
        stored = self.store.write(artifact_write(content=b"x"))
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE artifact_blobs SET content = ?, byte_size = ? WHERE digest = ?",
                (7, 1, stored.digest),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ArtifactIntegrityError) as malformed_blob:
            self.store.get("tenant-a", "workspace-a", stored.artifact_id)
        self.assertIsInstance(malformed_blob.exception.__cause__, TypeError)

        time_path = str(Path(self.tempdir.name) / "noncanonical-time.sqlite3")
        with SQLiteArtifactStore(time_path, clock=lambda: T0) as time_store:
            stored = time_store.write(artifact_write())
            connection = sqlite3.connect(time_path)
            try:
                connection.execute(
                    "UPDATE artifact_versions SET created_at = ? WHERE artifact_id = ?",
                    (T0, stored.artifact_id),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ArtifactIntegrityError, "data contract") as bad_time:
                time_store.get("tenant-a", "workspace-a", stored.artifact_id)
            self.assertIsInstance(bad_time.exception.__cause__, ValueError)

    def test_get_and_head_preflight_oversized_blob_before_materialization(self):
        path = str(Path(self.tempdir.name) / "single-read-limit.sqlite3")
        with SQLiteArtifactStore(
            path,
            max_content_bytes=4,
            clock=lambda: T0,
        ) as limited:
            stored = limited.write(artifact_write(content=b"x"))
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute(
                    "UPDATE artifact_blobs SET content = zeroblob(100), byte_size = 100 "
                    "WHERE digest = ?",
                    (stored.digest,),
                )
                connection.execute(
                    "UPDATE artifact_versions SET byte_size = 100 WHERE artifact_id = ?",
                    (stored.artifact_id,),
                )
                connection.commit()
            finally:
                connection.close()

            readers = (
                lambda: limited.get("tenant-a", "workspace-a", stored.artifact_id),
                lambda: limited.head(
                    "tenant-a",
                    "workspace-a",
                    "session-a",
                    "report.md",
                ),
            )
            for reader in readers:
                with self.subTest(reader=reader):
                    with patch.object(
                        limited,
                        "_row_to_artifact",
                        side_effect=AssertionError("oversized BLOB was materialized"),
                    ):
                        with self.assertRaisesRegex(
                            ArtifactTooLargeError,
                            "single-content limit",
                        ):
                            reader()

    def test_get_and_head_reject_integer_blob_storage_before_decoding(self):
        stored = self.store.write(artifact_write(content=b"x"))
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE artifact_blobs SET content = ?, byte_size = ? WHERE digest = ?",
                (7, 1, stored.digest),
            )
            connection.commit()
        finally:
            connection.close()

        readers = (
            lambda: self.store.get("tenant-a", "workspace-a", stored.artifact_id),
            lambda: self.store.head(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
            ),
        )
        for reader in readers:
            with self.subTest(reader=reader):
                with patch.object(
                    self.store,
                    "_row_to_artifact",
                    side_effect=AssertionError("INTEGER content reached the BLOB decoder"),
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "blob metadata",
                    ) as malformed:
                        reader()
                self.assertIsInstance(malformed.exception.__cause__, TypeError)

    def test_get_and_head_reject_missing_joined_blob_as_integrity_failure(self):
        stored = self.store.write(artifact_write(content=b"x"))
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM artifact_blobs WHERE digest = ?",
                (stored.digest,),
            )
            connection.commit()
        finally:
            connection.close()

        readers = (
            lambda: self.store.get("tenant-a", "workspace-a", stored.artifact_id),
            lambda: self.store.head(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
            ),
        )
        for reader in readers:
            with self.subTest(reader=reader):
                with self.assertRaisesRegex(ArtifactIntegrityError, "blob metadata"):
                    reader()

    def test_get_and_head_return_the_same_stable_version(self):
        first = self.store.write(artifact_write(content=b"one"))
        second = self.store.write(
            artifact_write(
                artifact_id="artifact-b",
                task_id="task-b",
                idempotency_key="artifact:task-b:report",
                content=b"two",
            )
        )

        self.assertEqual(
            self.store.get("tenant-a", "workspace-a", second.artifact_id),
            self.store.head("tenant-a", "workspace-a", "session-a", "report.md"),
        )
        self.assertEqual(
            self.store.get("tenant-a", "workspace-a", first.artifact_id),
            first,
        )

    def test_single_read_duplicate_and_identity_drift_fail_stably(self):
        first = self.store.write(artifact_write(content=b"one"))
        second = self.store.write(
            artifact_write(
                artifact_id="artifact-b",
                task_id="task-b",
                idempotency_key="artifact:task-b:report",
                content=b"two",
            )
        )
        real_get_preflight = self.store._select_get_preflight_rows
        real_head_preflight = self.store._select_head_preflight_rows

        def duplicate_get_rows(*args):
            rows = real_get_preflight(*args)
            return rows + rows

        with patch.object(
            self.store,
            "_select_get_preflight_rows",
            side_effect=duplicate_get_rows,
        ):
            with self.assertRaisesRegex(ArtifactIntegrityError, "ambiguous"):
                self.store.get("tenant-a", "workspace-a", first.artifact_id)

        def duplicate_head_row(*args):
            rows = real_head_preflight(*args)
            return (rows[0], rows[0])

        with patch.object(
            self.store,
            "_select_head_preflight_rows",
            side_effect=duplicate_head_row,
        ):
            with self.assertRaisesRegex(ArtifactIntegrityError, "ambiguous"):
                self.store.head("tenant-a", "workspace-a", "session-a", "report.md")

        def return_different_record(connection, *_args):
            row = self.store._select_record(
                connection,
                "tenant-a",
                "workspace-a",
                second.artifact_id,
            )
            if row is None:
                self.fail("test artifact disappeared")
            return row

        with patch.object(
            self.store,
            "_select_materialized_record",
            side_effect=return_different_record,
        ):
            with self.assertRaisesRegex(ArtifactIntegrityError, "stable snapshot"):
                self.store.get("tenant-a", "workspace-a", first.artifact_id)

    def test_get_uses_one_snapshot_across_cross_connection_blob_change(self):
        stored = self.store.write(artifact_write(content=b"data"))
        writer = sqlite3.connect(self.path, isolation_level=None)
        real_preflight = self.store._preflight_record_identity
        changed = False

        def mutate_after_preflight(row):
            nonlocal changed
            identity = real_preflight(row)
            if not changed:
                changed = True
                writer.execute(
                    "UPDATE artifact_blobs SET content = ? WHERE digest = ?",
                    (b"evil", stored.digest),
                )
            return identity

        try:
            with patch.object(
                self.store,
                "_preflight_record_identity",
                side_effect=mutate_after_preflight,
            ):
                observed = self.store.get("tenant-a", "workspace-a", stored.artifact_id)
            self.assertIsNotNone(observed)
            self.assertEqual(observed.content, b"data")
            with self.assertRaisesRegex(ArtifactIntegrityError, "digest verification"):
                self.store.get("tenant-a", "workspace-a", stored.artifact_id)
        finally:
            writer.close()

    def test_head_uses_one_snapshot_when_another_connection_appends(self):
        first = self.store.write(artifact_write(content=b"one"))
        writer = SQLiteArtifactStore(self.path, clock=lambda: T0)
        real_preflight = self.store._preflight_record_identity
        appended = False

        def append_after_preflight(row):
            nonlocal appended
            identity = real_preflight(row)
            if not appended:
                appended = True
                writer.write(
                    artifact_write(
                        artifact_id="artifact-racing-head",
                        task_id="task-racing-head",
                        idempotency_key="artifact:racing-head",
                        content=b"two",
                    ),
                    expected_head_version=1,
                )
            return identity

        try:
            with patch.object(
                self.store,
                "_preflight_record_identity",
                side_effect=append_after_preflight,
            ):
                observed = self.store.head(
                    "tenant-a",
                    "workspace-a",
                    "session-a",
                    "report.md",
                )
            self.assertEqual(observed, first)
            current = self.store.head(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
            )
            self.assertIsNotNone(current)
            self.assertEqual(current.version, 2)
        finally:
            writer.close()

    def test_limits_and_untrusted_metadata_fail_before_state_change(self):
        limited_path = str(Path(self.tempdir.name) / "limited.sqlite3")
        with SQLiteArtifactStore(
            limited_path,
            max_content_bytes=4,
            max_metadata_bytes=16,
            clock=lambda: T0,
        ) as limited:
            with self.assertRaises(ArtifactTooLargeError):
                limited.write(artifact_write(content=b"12345"))
            with self.assertRaises(ArtifactTooLargeError):
                limited.write(artifact_write(metadata={"long": "x" * 32}))
            self.assertEqual(limited.verify_scope("tenant-a", "workspace-a"), 0)

        with self.assertRaises(ValueError):
            artifact_write(metadata={"value": float("nan")})
        with self.assertRaises(TypeError):
            artifact_write(metadata=UserDict({"source": "wrapped"}))
        with self.assertRaises(TypeError):
            artifact_write(content=bytearray(b"mutable"))
        with self.assertRaises(ValueError):
            artifact_write(name="bad\nname")

    def test_metadata_structure_limits_fail_closed_without_recursion(self):
        allowed = "leaf"
        for _ in range(63):
            allowed = [allowed]
        artifact_write(metadata={"nested": allowed})

        too_deep = "leaf"
        for _ in range(64):
            too_deep = [too_deep]
        with self.assertRaisesRegex(ArtifactTooLargeError, "nested JSON containers"):
            artifact_write(metadata={"nested": too_deep})

        with self.assertRaisesRegex(ArtifactTooLargeError, "JSON value nodes"):
            artifact_write(metadata={"wide": [None] * 10_000})
        with self.assertRaisesRegex(ArtifactTooLargeError, "key exceeds"):
            artifact_write(metadata={"k" * 513: "value"})
        with self.assertRaisesRegex(ArtifactTooLargeError, "characters"):
            artifact_write(metadata={"value": "x" * 65_537})
        with self.assertRaisesRegex(ArtifactTooLargeError, "integer bits"):
            artifact_write(metadata={"value": 1 << 4_096})

        cyclic = {}
        cyclic["self"] = cyclic
        with self.assertRaisesRegex(ValueError, "reference cycle"):
            artifact_write(metadata=cyclic)

        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_deep_persisted_metadata_is_reported_as_integrity_failure(self):
        stored = self.store.write(artifact_write())
        poisoned = '{"nested":' + ("[" * 64) + '"leaf"' + ("]" * 64) + "}"
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE artifact_versions SET metadata_json = ? WHERE artifact_id = ?",
                (poisoned, stored.artifact_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ArtifactIntegrityError, "data contract"):
            self.store.get("tenant-a", "workspace-a", stored.artifact_id)

    def test_history_is_bounded_and_cursor_based(self):
        for version in range(1, 5):
            self.store.write(
                artifact_write(
                    artifact_id=f"artifact-{version}",
                    task_id=f"task-{version}",
                    idempotency_key=f"artifact:{version}",
                    content=f"v{version}".encode(),
                )
            )

        page = self.store.history(
            "tenant-a",
            "workspace-a",
            "session-a",
            "report.md",
            after_version=1,
            limit=2,
        )
        self.assertEqual([item.version for item in page], [2, 3])
        self.assertEqual(
            self.store.history(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
                after_version=4,
            ),
            (),
        )
        self.assertEqual(
            self.store.history(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
                after_version=99,
            ),
            (),
        )
        with self.assertRaises(ValueError):
            self.store.history("tenant-a", "workspace-a", "session-a", "report.md", limit=0)
        with self.assertRaises(ValueError):
            self.store.history(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
                limit=1_001,
            )

    def test_history_enforces_a_content_byte_budget_before_materializing_blobs(self):
        path = str(Path(self.tempdir.name) / "bounded-history.sqlite3")
        with self.assertRaises(ValueError):
            SQLiteArtifactStore(path, max_history_bytes=0, clock=lambda: T0)
        with self.assertRaises(TypeError):
            SQLiteArtifactStore(path, max_history_bytes=True, clock=lambda: T0)
        with SQLiteArtifactStore(
            path,
            max_history_bytes=5,
            clock=lambda: T0,
        ) as bounded:
            for version in (1, 2):
                bounded.write(
                    artifact_write(
                        artifact_id=f"artifact-budget-{version}",
                        task_id=f"task-budget-{version}",
                        idempotency_key=f"artifact:budget:{version}",
                        content=b"abc",
                    )
                )

            first = bounded.history(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
                limit=1,
            )
            self.assertEqual([item.version for item in first], [1])
            with self.assertRaisesRegex(ArtifactTooLargeError, "smaller limit"):
                bounded.history(
                    "tenant-a",
                    "workspace-a",
                    "session-a",
                    "report.md",
                    limit=2,
                )
            self.assertEqual(bounded.verify_scope("tenant-a", "workspace-a"), 2)

    def test_history_and_scope_reject_a_missing_first_version_without_writing(self):
        self._write_versions(2, prefix="missing-first")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "DELETE FROM artifact_versions WHERE artifact_id = ?",
                ("artifact-missing-first-1",),
            )
            connection.commit()
        finally:
            connection.close()

        before_state = self._database_state()
        before_changes = self.store._connection.total_changes
        readers = (
            lambda: self.store.history("tenant-a", "workspace-a", "session-a", "report.md"),
            lambda: self.store.verify_scope("tenant-a", "workspace-a"),
        )
        for reader in readers:
            with self.subTest(reader=reader):
                with self.assertRaisesRegex(ArtifactIntegrityError, "lineage"):
                    reader()
        self.assertEqual(self.store._connection.total_changes, before_changes)
        self.assertEqual(self._database_state(), before_state)

    def test_history_and_scope_reject_a_middle_gap_before_serving_a_later_cursor(self):
        self._write_versions(4, prefix="middle-gap")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "DELETE FROM artifact_versions WHERE artifact_id = ?",
                ("artifact-middle-gap-2",),
            )
            connection.commit()
        finally:
            connection.close()

        before_state = self._database_state()
        before_changes = self.store._connection.total_changes
        readers = (
            lambda: self.store.history(
                "tenant-a",
                "workspace-a",
                "session-a",
                "report.md",
                after_version=2,
            ),
            lambda: self.store.verify_scope("tenant-a", "workspace-a"),
        )
        for reader in readers:
            with self.subTest(reader=reader):
                with self.assertRaisesRegex(ArtifactIntegrityError, "lineage"):
                    reader()
        self.assertEqual(self.store._connection.total_changes, before_changes)
        self.assertEqual(self._database_state(), before_state)

        # Schema v2 has no durable high-water mark, so a deleted terminal version
        # is indistinguishable from one that was never persisted and is out of scope.

    def test_lineage_validation_rejects_duplicate_and_order_drift(self):
        class LineageOnlyConnection:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, *_args, **_kwargs):
                return iter(self.rows)

        def row(session_id, version, parent_version):
            return {
                "session_id": session_id,
                "name": "report.md",
                "version": version,
                "parent_version": parent_version,
            }

        cases = {
            "duplicate": [row("session-a", 1, None), row("session-a", 1, None)],
            "version-order": [
                row("session-a", 1, None),
                row("session-a", 3, 2),
                row("session-a", 2, 1),
            ],
            "group-order": [
                row("session-a", 1, None),
                row("session-b", 1, None),
                row("session-a", 2, 1),
            ],
        }
        for case, rows in cases.items():
            with self.subTest(case=case):

                @contextmanager
                def read_snapshot(lineage_rows=rows):
                    yield LineageOnlyConnection(lineage_rows)

                with patch.object(self.store, "_read_snapshot", read_snapshot):
                    with self.assertRaises(ArtifactIntegrityError):
                        self.store.history(
                            "tenant-a",
                            "workspace-a",
                            "session-a",
                            "report.md",
                        )

    def test_scope_lineage_groups_are_isolated_by_session_and_name(self):
        self._write_versions(2, session_id="session-a", name="a.md", prefix="group-a")
        self._write_versions(2, session_id="session-b", name="b.md", prefix="group-b")
        self.assertEqual(self.store.verify_scope("tenant-a", "workspace-a"), 4)

        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "DELETE FROM artifact_versions WHERE artifact_id = ?",
                ("artifact-group-b-1",),
            )
            connection.commit()
        finally:
            connection.close()

        intact = self.store.history("tenant-a", "workspace-a", "session-a", "a.md")
        self.assertEqual([item.version for item in intact], [1, 2])
        with self.assertRaises(ArtifactIntegrityError):
            self.store.history("tenant-a", "workspace-a", "session-b", "b.md")
        with self.assertRaises(ArtifactIntegrityError):
            self.store.verify_scope("tenant-a", "workspace-a")

    def test_verify_scope_rejects_blob_metadata_before_loading_corrupt_content(self):
        stored = self.store.write(artifact_write(content=b"x"))
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE artifact_blobs SET content = zeroblob(100), byte_size = 100 "
                "WHERE digest = ?",
                (stored.digest,),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ArtifactIntegrityError, "byte size metadata"):
            self.store.verify_scope("tenant-a", "workspace-a")


if __name__ == "__main__":
    unittest.main()
