from __future__ import annotations

import itertools
import unittest
from dataclasses import replace

import quantum_entanglement
from quantum_entanglement._result_artifact_transaction import (
    _MAX_RESULT_ARTIFACTS,
    _prepare_result_artifact_batch,
    _PreparedResultArtifact,
    _PreparedResultArtifactBatch,
)
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultArtifactCandidateV2,
)


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


if __name__ == "__main__":
    unittest.main()
