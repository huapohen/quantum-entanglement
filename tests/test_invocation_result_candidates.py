from __future__ import annotations

import unittest
from dataclasses import replace

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement._artifact_codec import canonical_artifact_metadata_v1
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

        maximum = valid_candidate(expected_head_version=(1 << 63) - 2)
        self.assertEqual(maximum.version, (1 << 63) - 1)
        for invalid in (True, -1, (1 << 63) - 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    valid_candidate(expected_head_version=invalid)  # type: ignore[arg-type]

    def test_candidate_factory_snapshots_metadata_and_returns_fresh_copies(self) -> None:
        values: list[object] = [{"value": "original"}]
        metadata: dict[str, object] = {"items": values}
        candidate = valid_candidate(metadata=metadata)

        values[0] = {"value": "mutated"}
        metadata["later"] = True
        first = candidate.metadata_dict()
        first["items"][0]["value"] = "copy-mutated"  # type: ignore[index]
        self.assertEqual(candidate.metadata_dict(), {"items": [{"value": "original"}]})

    def test_candidate_rejects_noncanonical_metadata_bytes_and_mutable_content(self) -> None:
        candidate = valid_candidate()
        for encoded in (
            bytearray(candidate.metadata_canonical_bytes),
            b' {"a":1}',
            b'{"b":2,"a":1}',
            b'{"a":1,"a":1}',
            b"[]",
        ):
            with self.subTest(encoded_type=type(encoded).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    replace(candidate, metadata_canonical_bytes=encoded)  # type: ignore[arg-type]

        for content in (bytearray(b"x"), memoryview(b"x"), "x"):
            with self.subTest(content_type=type(content).__name__):
                with self.assertRaises(TypeError):
                    replace(candidate, content=content)  # type: ignore[arg-type]

    def test_candidate_content_and_identity_boundaries_are_enforced_at_construction(self) -> None:
        empty = valid_candidate(content=b"")
        self.assertEqual(empty.byte_size, 0)
        boundary = valid_candidate(content=b"x" * (16 * 1024 * 1024))
        self.assertEqual(boundary.byte_size, 16 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "content exceeds"):
            valid_candidate(content=b"x" * ((16 * 1024 * 1024) + 1))

        candidate = valid_candidate()
        self.assertEqual(len(replace(candidate, artifact_id="a" * 4_096).artifact_id), 4_096)
        for field_name, value in (
            ("artifact_id", "a" * 4_097),
            ("name", "bad\nname"),
            ("media_type", "text / markdown"),
            ("created_by", "agent\x00bad"),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises((TypeError, ValueError)):
                    replace(candidate, **{field_name: value})

    def test_candidate_digest_covers_every_caller_field(self) -> None:
        candidate = valid_candidate()
        alternate_metadata = canonical_artifact_metadata_v1({"a": 2}).canonical_bytes
        changes = (
            {"tenant_id": "tenant-result-2"},
            {"workspace_id": "workspace-result-2"},
            {"session_id": "session-result-2"},
            {"task_id": "task-result-2"},
            {"artifact_id": "artifact-result-2"},
            {"name": "evidence.md"},
            {"media_type": "application/json"},
            {"content": b"changed"},
            {"metadata_canonical_bytes": alternate_metadata},
            {"created_by": "agent-result-2"},
            {"idempotency_key": "result-artifact:invocation-result-1:2"},
            {"expected_head_version": 1},
        )
        for change in changes:
            with self.subTest(change=tuple(change)):
                changed = replace(candidate, **change)
                self.assertNotEqual(changed.canonical_digest(), candidate.canonical_digest())

    def test_candidate_digest_closes_fields_omitted_by_legacy_artifact_request_digest(self) -> None:
        candidate = valid_candidate()
        for changed in (
            replace(candidate, artifact_id="artifact-result-2"),
            replace(candidate, idempotency_key="result-artifact:invocation-result-1:2"),
            replace(candidate, expected_head_version=1),
        ):
            with self.subTest(changed=changed.artifact_id):
                self.assertEqual(
                    changed.artifact_request_digest,
                    candidate.artifact_request_digest,
                )
                self.assertNotEqual(changed.canonical_digest(), candidate.canonical_digest())

    def test_public_derivations_revalidate_tampered_frozen_fields(self) -> None:
        operations = (
            lambda candidate: candidate.to_descriptor(),
            lambda candidate: candidate.canonical_digest(),
            lambda candidate: candidate.artifact_request_digest,
            lambda candidate: candidate.metadata_dict(),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                candidate = valid_candidate(content=b"content-secret")
                object.__setattr__(candidate, "artifact_id", "bad\nidentity")
                with self.assertRaises(ValueError) as failure:
                    operation(candidate)
                self.assertNotIn("content-secret", str(failure.exception))

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
