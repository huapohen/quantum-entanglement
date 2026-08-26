from __future__ import annotations

import unittest
from dataclasses import replace

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultArtifactV2,
)


def valid_candidate(
    *,
    content: bytes = b"hello\x00world",
    metadata: object | None = None,
    expected_head_version: int = 0,
) -> ScopedInvocationResultArtifactCandidateV2:
    return ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
        tenant_id="tenant-result-1",
        workspace_id="workspace-result-1",
        session_id="session-result-1",
        task_id="task-result-1",
        artifact_id="artifact-result-1",
        name="analysis.md",
        media_type="text/markdown",
        content=content,
        metadata={"β": [True, None], "a": 1} if metadata is None else metadata,
        created_by="agent-result-1",
        idempotency_key="result-artifact:invocation-result-1:1",
        expected_head_version=expected_head_version,
    )


class ScopedInvocationResultArtifactCandidateTests(unittest.TestCase):
    def test_candidate_derives_one_exact_descriptor_without_serializing_content(self) -> None:
        candidate = valid_candidate()
        descriptor = candidate.to_descriptor()

        self.assertIs(type(descriptor), ScopedInvocationResultArtifactV2)
        self.assertEqual((descriptor.version, descriptor.parent_version), (1, None))
        self.assertEqual(descriptor.blob_digest, candidate.blob_digest)
        self.assertEqual(descriptor.byte_size, len(b"hello\x00world"))
        self.assertEqual(descriptor.metadata_digest, candidate.metadata_digest)
        self.assertEqual(descriptor.request_digest, candidate.artifact_request_digest)
        self.assertNotIn("hello", repr(candidate))
        self.assertNotIn("β", repr(candidate))
        self.assertFalse(hasattr(candidate, "to_dict"))

    def test_candidate_allocates_lineage_only_from_expected_head(self) -> None:
        successor = valid_candidate(expected_head_version=7)
        self.assertEqual((successor.version, successor.parent_version), (8, 7))
        self.assertEqual(
            (successor.to_descriptor().version, successor.to_descriptor().parent_version),
            (8, 7),
        )

    def test_candidate_factory_snapshots_metadata_and_returns_fresh_copies(self) -> None:
        values: list[object] = [{"value": "original"}]
        metadata: dict[str, object] = {"items": values}
        candidate = valid_candidate(metadata=metadata)

        values[0] = {"value": "mutated"}
        metadata["later"] = True
        first = candidate.metadata_dict()
        first["items"][0]["value"] = "copy-mutated"  # type: ignore[index]
        self.assertEqual(candidate.metadata_dict(), {"items": [{"value": "original"}]})

    def test_candidate_remains_internal_until_acceptance_contracts_are_complete(self) -> None:
        self.assertNotIn(
            "ScopedInvocationResultArtifactCandidateV2",
            invocation_results_module.__all__,
        )
        self.assertNotIn(
            "ScopedInvocationResultArtifactCandidateV2",
            quantum_entanglement.__all__,
        )
        self.assertFalse(hasattr(quantum_entanglement, "ScopedInvocationResultArtifactCandidateV2"))

        class CandidateSubclass(ScopedInvocationResultArtifactCandidateV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact"):
            CandidateSubclass.from_content_metadata(
                tenant_id="tenant",
                workspace_id="workspace",
                session_id="session",
                task_id="task",
                artifact_id="artifact",
                name="name.txt",
                media_type="text/plain",
                content=b"content",
                metadata={},
                created_by="agent",
                idempotency_key="key",
                expected_head_version=0,
            )

        with self.assertRaisesRegex(TypeError, "exact"):
            replace(valid_candidate(), expected_head_version=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
