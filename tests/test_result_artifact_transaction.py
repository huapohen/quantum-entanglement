from __future__ import annotations

import copy
import itertools
import multiprocessing
import os
import pickle
import select
import signal
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement
import quantum_entanglement._result_artifact_transaction as result_artifact_transaction_module
from quantum_entanglement._result_artifact_transaction import (
    _MAX_RESULT_ARTIFACTS,
    _RESULT_ARTIFACT_TRANSACTION_TOKEN,
    _prepare_result_artifact_batch,
    _PreparedResultArtifact,
    _PreparedResultArtifactBatch,
    _ResultArtifactCommitAmbiguityError,
    _ResultArtifactConcurrencyError,
    _ResultArtifactConflictError,
    _ResultArtifactIntegrityError,
    _ResultArtifactTransactionError,
    _ResultArtifactTransactionHandle,
    _write_prepared_result_artifacts_in_transaction,
)
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultArtifactCandidateV2,
)
from quantum_entanglement.store import EventStoreLifecycleError, SQLiteEventStore


def candidate(
    ordinal: int = 0,
    *,
    content: bytes = b"result-content",
) -> ScopedInvocationResultArtifactCandidateV2:
    return ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
        tenant_id="tenant-result",
        workspace_id="workspace-result",
        session_id="session-result",
        task_id="task-result",
        artifact_id=f"artifact-{ordinal}",
        name=f"result-{ordinal}.md",
        media_type="text/markdown",
        content=content,
        metadata={"ordinal": ordinal},
        created_by="agent-result",
        idempotency_key=f"artifact-key-{ordinal}",
        expected_head_version=0,
    )


def crash_result_artifact_owner_transaction(
    path: str,
    channel: object,
    mode: str,
) -> None:
    store = SQLiteEventStore(path, clock=lambda: "2026-08-29T00:00:00.123456Z")
    batch = _prepare_result_artifact_batch((candidate(content=b"crash-rollback-content"),))
    with store._result_artifact_transaction() as handle:
        store._write_result_artifacts_in_owner_transaction(handle, batch)
        channel.send(("written", mode))
        channel.close()
        if mode == "exit":
            os._exit(73)
        if mode == "kill":
            while True:
                signal.pause()
        raise ValueError("unsupported crash probe mode")


class PreparedResultArtifactBatchTests(unittest.TestCase):
    def test_preparation_freezes_exact_order_content_metadata_and_derivations(self) -> None:
        first = candidate(0)
        second = candidate(1, content=b"second")
        batch = _prepare_result_artifact_batch(item for item in (first, second))

        self.assertIs(type(batch), _PreparedResultArtifactBatch)
        self.assertEqual(tuple(item.ordinal for item in batch.items), (0, 1))
        self.assertEqual(
            tuple(item.content for item in batch.items),
            (b"result-content", b"second"),
        )
        self.assertEqual(
            tuple(item.metadata_canonical_bytes for item in batch.items),
            (first.metadata_canonical_bytes, second.metadata_canonical_bytes),
        )
        self.assertEqual(
            tuple(item.descriptor for item in batch.items),
            (first.to_descriptor(), second.to_descriptor()),
        )
        self.assertEqual(
            tuple(item.candidate_sha256 for item in batch.items),
            (first.canonical_digest(), second.canonical_digest()),
        )
        self.assertEqual(batch.total_content_bytes, len(first.content) + len(second.content))
        self.assertNotIn("result-content", repr(batch))
        batch.verify()

    def test_empty_batch_is_exact_and_supported(self) -> None:
        batch = _prepare_result_artifact_batch(())
        self.assertEqual(batch.items, ())
        self.assertEqual((batch.total_content_bytes, batch.total_metadata_bytes), (0, 0))
        batch.verify()

    def test_preparation_rejects_non_iterable_non_exact_and_unbounded_inputs(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be iterable"):
            _prepare_result_artifact_batch(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "exact schema-2"):
            _prepare_result_artifact_batch((object(),))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "batch item limit"):
            _prepare_result_artifact_batch(itertools.repeat(candidate(), _MAX_RESULT_ARTIFACTS + 1))

    def test_batch_rejects_duplicate_identity_idempotency_and_head_coordinates(self) -> None:
        first = candidate(0)
        cases = (
            (replace(candidate(1), artifact_id=first.artifact_id), "IDs"),
            (
                replace(candidate(1), idempotency_key=first.idempotency_key),
                "idempotency keys",
            ),
            (replace(candidate(1), name=first.name), "head coordinates"),
        )
        for duplicate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _prepare_result_artifact_batch((first, duplicate))

    def test_batch_rejects_mixed_scope_and_aggregate_content_overflow(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope is not exact"):
            _prepare_result_artifact_batch(
                (candidate(0), replace(candidate(1), task_id="task-other"))
            )

        shared = b"x" * (16 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "content exceeds"):
            _prepare_result_artifact_batch(
                tuple(candidate(index, content=shared) for index in range(5))
            )

    def test_every_prepared_field_is_revalidated_after_hostile_mutation(self) -> None:
        valid = _prepare_result_artifact_batch((candidate(),))
        mutations = (
            ("item-order", lambda: object.__setattr__(valid.items[0], "ordinal", 1)),
            ("content", lambda: object.__setattr__(valid.items[0], "content", b"changed")),
            (
                "metadata",
                lambda: object.__setattr__(valid.items[0], "metadata_json", '{"changed":true}'),
            ),
            (
                "descriptor",
                lambda: object.__setattr__(
                    valid.items[0],
                    "descriptor",
                    replace(valid.items[0].descriptor, artifact_id="changed"),
                ),
            ),
            (
                "candidate-digest",
                lambda: object.__setattr__(valid.items[0], "candidate_sha256", "0" * 64),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                batch = _prepare_result_artifact_batch((candidate(),))
                object.__setattr__(valid, "items", batch.items)
                object.__setattr__(valid, "total_content_bytes", batch.total_content_bytes)
                object.__setattr__(valid, "total_metadata_bytes", batch.total_metadata_bytes)
                mutate()
                with self.assertRaises((TypeError, ValueError)):
                    valid.verify()

    def test_private_types_and_factory_are_not_package_exports(self) -> None:
        for name in (
            "_PreparedResultArtifact",
            "_PreparedResultArtifactBatch",
            "_prepare_result_artifact_batch",
        ):
            self.assertNotIn(name, quantum_entanglement.__all__)
            self.assertFalse(hasattr(quantum_entanglement, name))
        with self.assertRaisesRegex(TypeError, "exact private class"):
            class PreparedSubclass(_PreparedResultArtifact):
                pass

            PreparedSubclass(
                **_prepare_result_artifact_batch((candidate(),)).items[0].__dict__
            ).verify()


class ResultArtifactTransactionHandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteEventStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_owner_context_yields_exact_handle_only_while_transaction_is_open(self) -> None:
        handle: _ResultArtifactTransactionHandle
        with self.store._result_artifact_transaction() as handle:
            self.assertIs(type(handle), _ResultArtifactTransactionHandle)
            connection = self.store._connection_for_result_artifact_transaction(handle)
            self.assertIs(connection, self.store._connection)
            self.assertTrue(connection.in_transaction)
        self.assertFalse(self.store._connection.in_transaction)
        with self.assertRaisesRegex(RuntimeError, "no result Artifact owner transaction"):
            self.store._connection_for_result_artifact_transaction(handle)

    def test_handle_is_bound_to_store_and_one_active_generation(self) -> None:
        other = SQLiteEventStore(":memory:")
        try:
            with self.store._result_artifact_transaction() as first:
                with other._result_artifact_transaction() as second:
                    self.assertIs(
                        other._connection_for_result_artifact_transaction(second),
                        other._connection,
                    )
                    with self.assertRaisesRegex(RuntimeError, "foreign owner"):
                        other._connection_for_result_artifact_transaction(first)
                with self.assertRaisesRegex(RuntimeError, "no result Artifact owner transaction"):
                    other._connection_for_result_artifact_transaction(second)
                self.assertIs(
                    self.store._connection_for_result_artifact_transaction(first),
                    self.store._connection,
                )
        finally:
            other.close()

    def test_manual_begin_and_nested_context_cannot_forge_or_replace_owner(self) -> None:
        with self.assertRaisesRegex(TypeError, "constructor is private"):
            _ResultArtifactTransactionHandle(
                store=self.store,
                connection=self.store._connection,
                process_owner=self.store._process_owner,
                generation=1,
                token=object(),
            )

        with self.store._result_artifact_transaction() as handle:
            with self.assertRaisesRegex(RuntimeError, "already active"):
                with self.store._result_artifact_transaction():
                    pass
            self.assertIs(
                self.store._connection_for_result_artifact_transaction(handle),
                self.store._connection,
            )

        self.store._connection.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(RuntimeError, "no result Artifact owner transaction"):
                self.store._connection_for_result_artifact_transaction(handle)
        finally:
            self.store._connection.execute("ROLLBACK")

    def test_handle_cannot_be_copied_serialized_or_reused_after_body_rollback(self) -> None:
        handle: _ResultArtifactTransactionHandle
        with self.assertRaisesRegex(RuntimeError, "body failure"):
            with self.store._result_artifact_transaction() as handle:
                for operation in (
                    lambda: copy.copy(handle),
                    lambda: copy.deepcopy(handle),
                    lambda: pickle.dumps(handle),
                ):
                    with self.assertRaises(TypeError):
                        operation()
                connection = self.store._connection_for_result_artifact_transaction(handle)
                connection.execute(
                    """
                    INSERT INTO snapshots(stream_id, sequence, state_json, updated_at)
                    VALUES ('result-test', 1, '{}', '2026-08-29T00:00:00Z')
                    """
                )
                raise RuntimeError("body failure")
        self.assertFalse(self.store._connection.in_transaction)
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT stream_id FROM snapshots WHERE stream_id = 'result-test'"
            ).fetchone()
        )
        with self.assertRaisesRegex(RuntimeError, "no result Artifact owner transaction"):
            self.store._connection_for_result_artifact_transaction(handle)

    def test_connection_type_is_exact_at_private_constructor_boundary(self) -> None:
        class ConnectionSubclass(sqlite3.Connection):
            pass

        foreign = sqlite3.connect(":memory:", factory=ConnectionSubclass)
        try:
            with self.assertRaisesRegex(TypeError, "exact SQLite connection"):
                _ResultArtifactTransactionHandle(
                    store=self.store,
                    connection=foreign,
                    process_owner=self.store._process_owner,
                    generation=1,
                    token=_RESULT_ARTIFACT_TRANSACTION_TOKEN,
                )
        finally:
            foreign.close()

    def test_missing_owner_fails_before_inspecting_the_prepared_batch(self) -> None:
        class HostileBatch:
            def __getattribute__(self, _name: str) -> object:
                raise AssertionError("prepared batch was inspected")

        with self.assertRaisesRegex(RuntimeError, "owner transaction is unavailable"):
            self.store._write_result_artifacts_in_owner_transaction(
                None,  # type: ignore[arg-type]
                HostileBatch(),  # type: ignore[arg-type]
            )

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_real_fork_cannot_use_an_inherited_active_owner_handle(self) -> None:
        batch = _prepare_result_artifact_batch((candidate(),))
        read_fd, write_fd = os.pipe()
        child_pid = -1
        try:
            with self.store._result_artifact_transaction() as handle:
                child_pid = os.fork()
                if child_pid == 0:
                    try:
                        self.store._write_result_artifacts_in_owner_transaction(handle, batch)
                    except EventStoreLifecycleError:
                        os.write(write_fd, b"rejected")
                    else:
                        os.write(write_fd, b"accepted")
                    os._exit(0)

                os.close(write_fd)
                ready, _, _ = select.select((read_fd,), (), (), 3.0)
                self.assertTrue(ready, "fork child did not publish an owner-handle outcome")
                self.assertEqual(os.read(read_fd, 32), b"rejected")
                _, status = os.waitpid(child_pid, 0)
                child_pid = -1
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 0)
                self.assertEqual(
                    self.store._write_result_artifacts_in_owner_transaction(handle, batch),
                    (candidate().to_descriptor(),),
                )
        finally:
            for descriptor in (read_fd, write_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if child_pid > 0:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)

        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM artifact_versions").fetchone()[0],
            1,
        )

    def test_begin_and_commit_failures_never_leak_private_transaction_signals(self) -> None:
        transaction_code = sqlite3.SQLITE_TRANSACTION

        def deny_begin(action: int, first: object, *_args: object) -> int:
            if action == transaction_code and first == "BEGIN":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store._connection.set_authorizer(deny_begin)
        try:
            with self.assertRaises(_ResultArtifactTransactionError) as begin_failure:
                with self.store._result_artifact_transaction():
                    pass
            self.assertIsInstance(begin_failure.exception, Exception)
        finally:
            self.store._connection.set_authorizer(None)

        def deny_commit(action: int, first: object, *_args: object) -> int:
            if action == transaction_code and first == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store._connection.set_authorizer(deny_commit)
        try:
            with self.assertRaises(_ResultArtifactTransactionError) as commit_failure:
                with self.store._result_artifact_transaction() as handle:
                    batch = _prepare_result_artifact_batch((candidate(),))
                    self.store._write_result_artifacts_in_owner_transaction(handle, batch)
            self.assertIsInstance(commit_failure.exception, Exception)
        finally:
            self.store._connection.set_authorizer(None)
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM artifact_versions").fetchone()[0],
            0,
        )

    def test_unconfirmed_commit_failure_is_fixed_and_poisons_owner_store(self) -> None:
        transaction_code = sqlite3.SQLITE_TRANSACTION

        def deny_commit_and_rollback(action: int, first: object, *_args: object) -> int:
            if action == transaction_code and first in {"COMMIT", "ROLLBACK"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store._connection.set_authorizer(deny_commit_and_rollback)
        try:
            with self.assertRaises(_ResultArtifactCommitAmbiguityError) as failure:
                with self.store._result_artifact_transaction() as handle:
                    batch = _prepare_result_artifact_batch((candidate(),))
                    self.store._write_result_artifacts_in_owner_transaction(handle, batch)
            self.assertIsInstance(failure.exception, Exception)
            self.assertTrue(self.store._poisoned)
        finally:
            self.store._connection.set_authorizer(None)

    def test_keyboard_interrupt_after_confirmed_rollback_is_clean(self) -> None:
        with self.assertRaises(KeyboardInterrupt) as failure:
            with self.store._result_artifact_transaction():
                raise KeyboardInterrupt("private-control-payload")

        self.assertIs(type(failure.exception), KeyboardInterrupt)
        self.assertEqual(failure.exception.args, ())
        self.assertIsNone(failure.exception.__cause__)
        self.assertIsNone(failure.exception.__context__)
        self.assertFalse(self.store._poisoned)
        self.assertFalse(self.store._connection.in_transaction)

    def test_keyboard_interrupt_with_failed_rollback_retains_ambiguity_cause(self) -> None:
        transaction_code = sqlite3.SQLITE_TRANSACTION

        def deny_rollback(action: int, first: object, *_args: object) -> int:
            if action == transaction_code and first == "ROLLBACK":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store._connection.set_authorizer(deny_rollback)
        try:
            with self.assertRaises(KeyboardInterrupt) as failure:
                with self.store._result_artifact_transaction():
                    raise KeyboardInterrupt("private-control-payload")
        finally:
            self.store._connection.set_authorizer(None)

        self.assertIs(type(failure.exception), KeyboardInterrupt)
        self.assertEqual(failure.exception.args, ())
        self.assertIs(
            type(failure.exception.__cause__),
            _ResultArtifactCommitAmbiguityError,
        )
        self.assertIsNone(failure.exception.__context__)
        self.assertTrue(self.store._poisoned)


class ResultArtifactOwnerWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = "2026-08-29T00:00:00.123456Z"
        self.store = SQLiteEventStore(":memory:", clock=lambda: self.now)

    def tearDown(self) -> None:
        self.store.close()

    def write(
        self,
        *candidates: ScopedInvocationResultArtifactCandidateV2,
    ) -> tuple[object, ...]:
        batch = _prepare_result_artifact_batch(candidates)
        with self.store._result_artifact_transaction() as handle:
            return self.store._write_result_artifacts_in_owner_transaction(handle, batch)

    def counts(self) -> tuple[int, int]:
        connection = self.store._connection
        return (
            connection.execute("SELECT count(*) FROM artifact_blobs").fetchone()[0],
            connection.execute("SELECT count(*) FROM artifact_versions").fetchone()[0],
        )

    def test_ordered_batch_commits_exact_descriptors_and_deduplicated_blob(self) -> None:
        shared = b"shared-content"
        first = candidate(0, content=shared)
        second = candidate(1, content=shared)
        descriptors = self.write(first, second)

        self.assertEqual(descriptors, (first.to_descriptor(), second.to_descriptor()))
        self.assertEqual(self.counts(), (1, 2))
        rows = self.store._connection.execute(
            """
            SELECT
                artifact_id,
                version,
                parent_version,
                metadata_json,
                typeof(metadata_json) AS metadata_storage,
                created_at
            FROM artifact_versions
            ORDER BY artifact_id
            """
        ).fetchall()
        self.assertEqual(tuple(row["artifact_id"] for row in rows), ("artifact-0", "artifact-1"))
        self.assertEqual(tuple(row["version"] for row in rows), (1, 1))
        self.assertEqual(tuple(row["parent_version"] for row in rows), (None, None))
        self.assertEqual(tuple(row["metadata_storage"] for row in rows), ("text", "text"))
        self.assertEqual(tuple(row["created_at"] for row in rows), (self.now, self.now))
        self.assertEqual(
            tuple(row["metadata_json"].encode("utf-8") for row in rows),
            (first.metadata_canonical_bytes, second.metadata_canonical_bytes),
        )

    def test_successor_uses_exact_existing_head_and_parent(self) -> None:
        first = candidate(0)
        self.write(first)
        successor = replace(
            candidate(2, content=b"successor-content"),
            name=first.name,
            expected_head_version=1,
        )
        descriptor = self.write(successor)[0]
        self.assertEqual((descriptor.version, descriptor.parent_version), (2, 1))
        self.assertEqual(self.counts(), (2, 2))

    def test_entire_existing_lineage_is_verified_before_a_successor_write(self) -> None:
        first = candidate(0)
        self.write(first)
        second = replace(candidate(1), name=first.name, expected_head_version=1)
        self.write(second)
        third = replace(candidate(2), name=first.name, expected_head_version=2)
        self.write(third)

        connection = self.store._connection
        connection.execute("PRAGMA ignore_check_constraints=ON")
        try:
            connection.execute(
                "UPDATE artifact_versions SET parent_version = 99 WHERE artifact_id = ?",
                (second.artifact_id,),
            )
        finally:
            connection.execute("PRAGMA ignore_check_constraints=OFF")

        successor = replace(
            candidate(3, content=b"lineage-successor"),
            name=first.name,
            expected_head_version=3,
        )
        before = self.counts()
        with self.assertRaisesRegex(_ResultArtifactIntegrityError, "transaction integrity failed"):
            self.write(successor)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(
            connection.execute(
                "SELECT digest FROM artifact_blobs WHERE digest = ?",
                (successor.blob_digest,),
            ).fetchone()
        )

    def test_existing_history_timestamp_and_storage_drift_block_successors(self) -> None:
        first = candidate(0)
        self.write(first)
        successor = replace(
            candidate(1, content=b"history-contract-successor"),
            name=first.name,
            expected_head_version=1,
        )
        connection = self.store._connection
        mutations = (
            (
                "timestamp",
                "UPDATE artifact_versions SET created_at = 'not-a-timestamp' "
                "WHERE artifact_id = ?",
            ),
            (
                "metadata-storage",
                "UPDATE artifact_versions SET metadata_json = CAST(metadata_json AS BLOB) "
                "WHERE artifact_id = ?",
            ),
        )
        for label, statement in mutations:
            with self.subTest(label=label):
                connection.execute(statement, (first.artifact_id,))
                before = self.counts()
                with self.assertRaises(_ResultArtifactIntegrityError):
                    self.write(successor)
                self.assertEqual(self.counts(), before)
                connection.execute(
                    """
                    UPDATE artifact_versions
                    SET metadata_json = ?, created_at = ?
                    WHERE artifact_id = ?
                    """,
                    (
                        first.metadata_canonical_bytes.decode("utf-8"),
                        self.now,
                        first.artifact_id,
                    ),
                )

    def test_existing_history_is_read_in_fixed_size_batches(self) -> None:
        first = candidate(0)
        self.write(first)
        for version in range(1, 5):
            self.write(
                replace(
                    candidate(version),
                    name=first.name,
                    expected_head_version=version,
                )
            )
        successor = replace(
            candidate(5, content=b"streamed-history-successor"),
            name=first.name,
            expected_head_version=5,
        )

        with (
            patch.object(
                result_artifact_transaction_module,
                "_RESULT_ARTIFACT_HISTORY_FETCH_BATCH_SIZE",
                2,
            ),
            patch.object(
                result_artifact_transaction_module,
                "_guarded_fetchmany",
                wraps=result_artifact_transaction_module._guarded_fetchmany,
            ) as fetchmany,
        ):
            self.write(successor)

        self.assertEqual(fetchmany.call_count, 4)
        self.assertEqual(self.counts(), (2, 6))

    def test_oversized_history_metadata_is_rejected_before_materialization(self) -> None:
        first = candidate(0)
        self.write(first)
        connection = self.store._connection
        connection.execute(
            "UPDATE artifact_versions SET metadata_json = ? WHERE artifact_id = ?",
            (
                "x" * (result_artifact_transaction_module.MAX_ARTIFACT_METADATA_BYTES + 1),
                first.artifact_id,
            ),
        )
        successor = replace(
            candidate(1, content=b"oversized-history-successor"),
            name=first.name,
            expected_head_version=1,
        )
        batch = _prepare_result_artifact_batch((successor,))
        real_fetchone = result_artifact_transaction_module._guarded_fetchone
        materialized_history = False

        def observe_fetchone(
            connection: sqlite3.Connection,
            process_guard: object,
            sql: str,
            parameters: tuple[object, ...] = (),
        ) -> object:
            nonlocal materialized_history
            if "FROM artifact_versions" in sql and "WHERE rowid = ?" in sql:
                materialized_history = True
            return real_fetchone(
                connection,
                process_guard,  # type: ignore[arg-type]
                sql,
                parameters,
            )

        with (
            patch.object(
                result_artifact_transaction_module,
                "_guarded_fetchone",
                side_effect=observe_fetchone,
            ),
            patch.object(
                result_artifact_transaction_module,
                "decode_canonical_artifact_metadata_v1",
                wraps=result_artifact_transaction_module.decode_canonical_artifact_metadata_v1,
            ) as decode_metadata,
            self.assertRaises(_ResultArtifactIntegrityError),
        ):
            with self.store._result_artifact_transaction() as handle:
                self.store._write_result_artifacts_in_owner_transaction(handle, batch)

        self.assertFalse(materialized_history)
        decode_metadata.assert_not_called()
        self.assertEqual(self.counts(), (1, 1))

    def test_stale_head_fails_before_blob_or_version_dml(self) -> None:
        first = candidate(0)
        self.write(first)
        stale = replace(candidate(2, content=b"never-written"), name=first.name)
        before = self.counts()

        with self.assertRaisesRegex(_ResultArtifactConcurrencyError, "head changed"):
            self.write(stale)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT digest FROM artifact_blobs WHERE digest = ?",
                (stale.blob_digest,),
            ).fetchone()
        )

    def test_entire_batch_preflights_before_the_first_blob_dml(self) -> None:
        existing = candidate(9)
        self.write(existing)
        first = candidate(0, content=b"must-not-be-written")
        stale_second = replace(
            candidate(1, content=b"also-not-written"),
            name=existing.name,
        )
        batch = _prepare_result_artifact_batch((first, stale_second))
        before = self.counts()

        with self.assertRaises(_ResultArtifactConcurrencyError):
            with self.store._result_artifact_transaction() as handle:
                self.store._write_result_artifacts_in_owner_transaction(handle, batch)
        self.assertEqual(self.counts(), before)
        for item in (first, stale_second):
            self.assertIsNone(
                self.store._connection.execute(
                    "SELECT digest FROM artifact_blobs WHERE digest = ?",
                    (item.blob_digest,),
                ).fetchone()
            )

    def test_identity_and_idempotency_reuse_are_never_result_replay(self) -> None:
        first = candidate(0)
        self.write(first)
        for conflict in (
            replace(candidate(2), artifact_id=first.artifact_id),
            replace(candidate(2), idempotency_key=first.idempotency_key),
        ):
            with self.subTest(artifact_id=conflict.artifact_id):
                with self.assertRaisesRegex(_ResultArtifactConflictError, "already bound"):
                    self.write(conflict)
        self.assertEqual(self.counts(), (1, 1))

    def test_crossed_artifact_identity_and_idempotency_collisions_fail_closed(self) -> None:
        first = candidate(0)
        second = candidate(1)
        self.write(first, second)
        crossed = replace(
            candidate(2, content=b"crossed-collision"),
            artifact_id=first.artifact_id,
            idempotency_key=second.idempotency_key,
        )
        before = self.counts()
        with self.assertRaises(_ResultArtifactConflictError):
            self.write(crossed)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT digest FROM artifact_blobs WHERE digest = ?",
                (crossed.blob_digest,),
            ).fetchone()
        )

    def test_deleted_middle_version_gap_fails_before_successor_dml(self) -> None:
        first = candidate(0)
        self.write(first)
        second = replace(candidate(1), name=first.name, expected_head_version=1)
        self.write(second)
        third = replace(candidate(2), name=first.name, expected_head_version=2)
        self.write(third)
        self.store._connection.execute(
            "DELETE FROM artifact_versions WHERE artifact_id = ?",
            (second.artifact_id,),
        )
        successor = replace(
            candidate(3, content=b"gap-successor"),
            name=first.name,
            expected_head_version=3,
        )
        before = self.counts()
        with self.assertRaises(_ResultArtifactIntegrityError):
            self.write(successor)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT digest FROM artifact_blobs WHERE digest = ?",
                (successor.blob_digest,),
            ).fetchone()
        )

    def test_fixed_write_error_detaches_content_bearing_internal_traceback_frames(self) -> None:
        first = candidate(0, content=b"private-content-canary")
        self.write(first)
        conflict = replace(candidate(1), artifact_id=first.artifact_id)
        try:
            self.write(conflict)
        except _ResultArtifactConflictError as error:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            function_names: list[str] = []
            cursor = error.__traceback__
            while cursor is not None:
                function_names.append(cursor.tb_frame.f_code.co_name)
                cursor = cursor.tb_next
            self.assertNotIn("_write_prepared_result_artifacts_in_transaction_body", function_names)
            self.assertNotIn("_preflight_result_artifact_identity", function_names)
            self.assertNotIn("_write_result_artifacts_in_owner_transaction", function_names)
        else:
            self.fail("conflicting result Artifact identity unexpectedly committed")

    def test_existing_blob_is_byte_verified_before_any_version_dml(self) -> None:
        item = candidate(0)
        connection = self.store._connection
        connection.execute("PRAGMA ignore_check_constraints=ON")
        try:
            connection.execute(
                """
                INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (item.blob_digest, "not-a-blob", len(item.content), self.now),
            )
        finally:
            connection.execute("PRAGMA ignore_check_constraints=OFF")

        with self.assertRaises(_ResultArtifactIntegrityError):
            self.write(item)
        self.assertEqual(self.counts(), (1, 0))

    def test_noncanonical_existing_blob_timestamp_is_rejected_before_version_dml(self) -> None:
        item = candidate(0)
        self.store._connection.execute(
            """
            INSERT INTO artifact_blobs(digest, content, byte_size, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                item.blob_digest,
                sqlite3.Binary(item.content),
                len(item.content),
                "2026-08-29T00:00:00Z",
            ),
        )
        with self.assertRaisesRegex(_ResultArtifactIntegrityError, "transaction integrity failed"):
            self.write(item)
        self.assertEqual(self.counts(), (1, 0))

    def test_clock_timestamp_is_persisted_as_exact_utc_microseconds(self) -> None:
        self.store._clock = lambda: "2026-08-29T00:00:00Z"
        self.write(candidate())
        timestamps = self.store._connection.execute(
            """
            SELECT created_at FROM artifact_blobs
            UNION ALL
            SELECT created_at FROM artifact_versions
            """
        ).fetchall()
        self.assertEqual(
            tuple(row["created_at"] for row in timestamps),
            ("2026-08-29T00:00:00.000000Z", "2026-08-29T00:00:00.000000Z"),
        )

    def test_hostile_sqlite_row_and_text_factories_fail_before_dml(self) -> None:
        connection = self.store._connection
        cases = (
            (
                "row",
                lambda: setattr(connection, "row_factory", lambda _cursor, row: {"x": row}),
            ),
            ("text", lambda: setattr(connection, "text_factory", bytes)),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                mutate()
                try:
                    with self.assertRaisesRegex(
                        _ResultArtifactIntegrityError,
                        "transaction integrity failed",
                    ):
                        self.write(candidate())
                finally:
                    connection.row_factory = sqlite3.Row
                    connection.text_factory = str
                self.assertEqual(self.counts(), (0, 0))

    def test_unexpected_artifact_trigger_fails_before_dml(self) -> None:
        self.store._connection.execute(
            """
            CREATE TRIGGER unexpected_result_artifact_trigger
            AFTER INSERT ON artifact_versions
            BEGIN
                SELECT 1;
            END
            """
        )
        with self.assertRaisesRegex(_ResultArtifactIntegrityError, "transaction integrity failed"):
            self.write(candidate())
        self.assertEqual(self.counts(), (0, 0))

    def test_unexpected_temp_artifact_trigger_fails_before_dml(self) -> None:
        self.store._connection.execute(
            """
            CREATE TEMP TRIGGER unexpected_temp_result_artifact_trigger
            AFTER INSERT ON main.artifact_versions
            BEGIN
                SELECT 1;
            END
            """
        )
        with self.assertRaisesRegex(_ResultArtifactIntegrityError, "transaction integrity failed"):
            self.write(candidate())
        self.assertEqual(self.counts(), (0, 0))

    def test_sqlite_trace_boundary_process_cut_aborts_before_first_dml(self) -> None:
        current = True
        cut_seen = False

        def guarded() -> None:
            if not current:
                raise RuntimeError("simulated process-epoch cut")

        def cut_on_first_insert(statement: str) -> None:
            nonlocal current, cut_seen
            if not cut_seen and statement.lstrip().startswith("INSERT INTO artifact_blobs"):
                cut_seen = True
                current = False

        connection = self.store._connection
        batch = _prepare_result_artifact_batch((candidate(),))
        connection.execute("BEGIN IMMEDIATE")
        connection.set_trace_callback(cut_on_first_insert)
        try:
            with self.assertRaisesRegex(RuntimeError, "simulated process-epoch cut"):
                _write_prepared_result_artifacts_in_transaction(
                    connection,
                    batch,
                    clock=lambda: self.now,
                    process_guard=guarded,
                )
        finally:
            current = True
            connection.set_trace_callback(None)
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.execute("ROLLBACK")

        self.assertTrue(cut_seen)
        self.assertEqual(self.counts(), (0, 0))

    def test_later_owner_body_failure_rolls_back_every_blob_and_version(self) -> None:
        first = candidate(0)
        second = candidate(1, content=b"second-content")
        batch = _prepare_result_artifact_batch((first, second))
        with self.assertRaisesRegex(RuntimeError, "later result graph failure"):
            with self.store._result_artifact_transaction() as handle:
                descriptors = self.store._write_result_artifacts_in_owner_transaction(handle, batch)
                self.assertEqual(descriptors, (first.to_descriptor(), second.to_descriptor()))
                self.assertEqual(self.counts(), (2, 2))
                raise RuntimeError("later result graph failure")
        self.assertEqual(self.counts(), (0, 0))

    def test_caught_second_item_failure_marks_owner_rollback_only(self) -> None:
        first = candidate(0, content=b"rollback-only-first")
        second = candidate(1, content=b"rollback-only-second")
        batch = _prepare_result_artifact_batch((first, second))
        real_execute = result_artifact_transaction_module._guarded_execute
        version_inserts = 0

        def fail_second_version_insert(
            connection: sqlite3.Connection,
            process_guard: object,
            sql: str,
            parameters: tuple[object, ...] = (),
        ) -> None:
            nonlocal version_inserts
            if "INSERT INTO artifact_versions" in sql:
                version_inserts += 1
                if version_inserts == 2:
                    raise sqlite3.OperationalError("injected second version failure")
            real_execute(connection, process_guard, sql, parameters)  # type: ignore[arg-type]

        with self.assertRaisesRegex(RuntimeError, "rollback-only"):
            with self.store._result_artifact_transaction() as handle:
                with patch.object(
                    result_artifact_transaction_module,
                    "_guarded_execute",
                    side_effect=fail_second_version_insert,
                ):
                    with self.assertRaises(_ResultArtifactIntegrityError):
                        self.store._write_result_artifacts_in_owner_transaction(handle, batch)
                self.assertEqual(self.counts(), (2, 1))

        self.assertEqual(version_inserts, 2)
        self.assertEqual(self.counts(), (0, 0))

    def test_empty_batch_has_no_clock_or_dml_effect(self) -> None:
        batch = _prepare_result_artifact_batch(())
        self.store._clock = lambda: (_ for _ in ()).throw(AssertionError("clock was read"))
        with self.store._result_artifact_transaction() as handle:
            self.assertEqual(
                self.store._write_result_artifacts_in_owner_transaction(handle, batch),
                (),
            )
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(
            self.store._connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )
        self.assertEqual(
            self.store._connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )


class ResultArtifactConcurrentWriterTests(unittest.TestCase):
    def test_two_connections_with_the_same_expected_head_commit_exactly_one(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = str(Path(tempdir) / "result-artifacts.sqlite3")
            now = "2026-08-29T00:00:00.123456Z"
            stores = (
                SQLiteEventStore(path, clock=lambda: now),
                SQLiteEventStore(path, clock=lambda: now),
            )
            first = candidate(0, content=b"writer-one")
            second = replace(
                candidate(1, content=b"writer-two"),
                name=first.name,
            )
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def write(
                store: SQLiteEventStore,
                item: ScopedInvocationResultArtifactCandidateV2,
            ) -> None:
                batch = _prepare_result_artifact_batch((item,))
                barrier.wait()
                try:
                    with store._result_artifact_transaction() as handle:
                        store._write_result_artifacts_in_owner_transaction(handle, batch)
                except _ResultArtifactConcurrencyError:
                    outcomes.append("concurrency")
                else:
                    outcomes.append("committed")

            threads = (
                threading.Thread(target=write, args=(stores[0], first)),
                threading.Thread(target=write, args=(stores[1], second)),
            )
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(sorted(outcomes), ["committed", "concurrency"])
                self.assertEqual(
                    stores[0]._connection.execute(
                        "SELECT count(*) FROM artifact_versions"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    stores[0]._connection.execute("SELECT count(*) FROM artifact_blobs").fetchone()[
                        0
                    ],
                    1,
                )
            finally:
                for store in stores:
                    store.close()


@unittest.skipUnless(hasattr(os, "kill") and hasattr(signal, "SIGKILL"), "requires POSIX")
class ResultArtifactCrashRollbackTests(unittest.TestCase):
    def test_process_exit_and_sigkill_leave_no_partial_artifact_transaction(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tempdir:
            for mode in ("exit", "kill"):
                with self.subTest(mode=mode):
                    path = str(Path(tempdir) / f"result-artifact-{mode}.sqlite3")
                    parent_channel, child_channel = context.Pipe(duplex=False)
                    process = context.Process(
                        target=crash_result_artifact_owner_transaction,
                        args=(path, child_channel, mode),
                    )
                    process.start()
                    child_channel.close()
                    try:
                        self.assertTrue(parent_channel.poll(10.0), "crash probe timed out")
                        self.assertEqual(parent_channel.recv(), ("written", mode))
                        if mode == "kill":
                            os.kill(process.pid, signal.SIGKILL)
                        process.join(10.0)
                        self.assertFalse(process.is_alive(), "crash probe did not exit")
                        expected_exitcode = 73 if mode == "exit" else -signal.SIGKILL
                        self.assertEqual(process.exitcode, expected_exitcode)
                    finally:
                        parent_channel.close()
                        if process.is_alive():
                            process.kill()
                            process.join(2.0)

                    recovered = SQLiteEventStore(path)
                    try:
                        self.assertEqual(
                            recovered._connection.execute(
                                "SELECT count(*) FROM artifact_blobs"
                            ).fetchone()[0],
                            0,
                        )
                        self.assertEqual(
                            recovered._connection.execute(
                                "SELECT count(*) FROM artifact_versions"
                            ).fetchone()[0],
                            0,
                        )
                        self.assertEqual(
                            recovered._connection.execute("PRAGMA integrity_check").fetchone()[0],
                            "ok",
                        )
                    finally:
                        recovered.close()


if __name__ == "__main__":
    unittest.main()
