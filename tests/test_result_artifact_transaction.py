from __future__ import annotations

import copy
import itertools
import pickle
import sqlite3
import unittest
from dataclasses import replace

import quantum_entanglement
from quantum_entanglement._result_artifact_transaction import (
    _MAX_RESULT_ARTIFACTS,
    _RESULT_ARTIFACT_TRANSACTION_TOKEN,
    _prepare_result_artifact_batch,
    _PreparedResultArtifact,
    _PreparedResultArtifactBatch,
    _ResultArtifactTransactionHandle,
)
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultArtifactCandidateV2,
)
from quantum_entanglement.store import SQLiteEventStore


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


if __name__ == "__main__":
    unittest.main()
