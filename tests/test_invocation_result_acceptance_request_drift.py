from __future__ import annotations

import unittest
from dataclasses import fields, replace

from quantum_entanglement.invocation_execution import (
    EffectClass,
    ScopedInvocationStartEvidenceV3,
    ScopedInvocationStartReceiptV3,
)
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultArtifactV2,
    ScopedInvocationResultManifestV2,
)
from tests.test_invocation_result_acceptance_request import (
    candidate_for,
    manifest_for,
    request_for,
)
from tests.test_scoped_invocation_execution import valid_scoped_start_receipt


def _init_field_names(value_type: type[object]) -> set[str]:
    return {item.name for item in fields(value_type) if item.init}


def _receipt_with_evidence_changes(
    receipt: ScopedInvocationStartReceiptV3,
    **changes: object,
) -> ScopedInvocationStartReceiptV3:
    evidence = replace(receipt.evidence, **changes)
    stream_id = receipt.stream_id
    if "session_id" in changes:
        stream_id = "session:" + evidence.session_id
    return replace(receipt, stream_id=stream_id, evidence=evidence)


class ScopedInvocationResultAcceptanceRequestDriftTests(unittest.TestCase):
    def test_field_inventory_requires_every_contract_field_in_the_matrix(self) -> None:
        self.assertEqual(
            _init_field_names(ScopedInvocationResultAcceptanceRequestV2),
            {
                "schema_version",
                "acceptance_idempotency_key",
                "start_receipt",
                "manifest",
                "artifact_candidates",
                "expected_stream_version",
            },
        )
        self.assertEqual(
            _init_field_names(ScopedInvocationStartReceiptV3),
            {"event_id", "stream_id", "sequence", "global_position", "evidence"},
        )
        self.assertEqual(
            _init_field_names(ScopedInvocationStartEvidenceV3),
            {
                "schema_version",
                "tenant_id",
                "workspace_id",
                "invocation_id",
                "session_id",
                "plan_id",
                "task_id",
                "agent_id",
                "job_idempotency_key",
                "attempt_id",
                "attempt_number",
                "lease_epoch",
                "worker_id",
                "lease_token_digest",
                "claimed_at",
                "lease_expires_at",
                "manifest_digest",
                "envelope_digest",
                "context_digest",
                "authorization_digest",
                "runtime_revision",
                "correlation_id",
                "causation_id",
            },
        )
        self.assertEqual(
            _init_field_names(ScopedInvocationResultManifestV2),
            {
                "schema_version",
                "tenant_id",
                "workspace_id",
                "invocation_id",
                "session_id",
                "plan_id",
                "task_id",
                "agent_id",
                "job_idempotency_key",
                "task_revision",
                "correlation_id",
                "causation_id",
                "runtime_revision",
                "execution_manifest_digest",
                "effect_class",
                "action_receipt_set_digest",
                "result_ref",
                "narration",
                "metadata",
                "primary_artifact_id",
                "artifacts",
            },
        )
        self.assertEqual(
            _init_field_names(ScopedInvocationResultArtifactCandidateV2),
            {
                "tenant_id",
                "workspace_id",
                "session_id",
                "task_id",
                "artifact_id",
                "name",
                "media_type",
                "content",
                "metadata_canonical_bytes",
                "created_by",
                "idempotency_key",
                "expected_head_version",
            },
        )
        self.assertEqual(
            _init_field_names(ScopedInvocationResultArtifactV2),
            {
                "artifact_id",
                "name",
                "version",
                "parent_version",
                "media_type",
                "blob_digest",
                "byte_size",
                "metadata_digest",
                "created_by",
                "idempotency_key",
                "request_digest",
            },
        )

    def test_request_top_level_drift_is_rejected_or_digest_bound(self) -> None:
        baseline = request_for(candidates=(), manifest=manifest_for(()))

        with self.assertRaisesRegex(ValueError, "schemaVersion is unsupported"):
            replace(baseline, schema_version=baseline.schema_version + 1)
        with self.assertRaisesRegex(TypeError, "schemaVersion must be an exact integer"):
            replace(baseline, schema_version=True)

        changed_key = request_for(
            candidates=(),
            manifest=manifest_for(()),
            acceptance_idempotency_key="accept:invocation-scoped-other",
        )
        changed_version = request_for(
            candidates=(),
            manifest=manifest_for(()),
            expected_stream_version=baseline.expected_stream_version + 1,
        )
        self.assertNotEqual(changed_key.canonical_digest(), baseline.canonical_digest())
        self.assertNotEqual(changed_version.canonical_digest(), baseline.canonical_digest())

    def test_start_receipt_coordinate_drift_is_digest_bound(self) -> None:
        receipt = valid_scoped_start_receipt()
        baseline = request_for(
            start_receipt=receipt,
            candidates=(),
            manifest=manifest_for(()),
        )
        coordinate_changes = {
            "event_id": {"event_id": "event-scoped-invocation-started-other"},
            "sequence": {"sequence": receipt.sequence + 1},
            "global_position": {"global_position": receipt.global_position + 1},
        }
        for field_name, changes in coordinate_changes.items():
            with self.subTest(field=field_name):
                changed_receipt = replace(receipt, **changes)
                changed = request_for(
                    start_receipt=changed_receipt,
                    candidates=(),
                    manifest=manifest_for(()),
                )
                self.assertNotEqual(changed.start_receipt_digest, baseline.start_receipt_digest)
                self.assertNotEqual(changed.canonical_digest(), baseline.canonical_digest())

        with self.assertRaisesRegex(ValueError, "streamId does not match"):
            replace(receipt, stream_id="session:other")

    def test_every_start_evidence_manifest_binding_rejects_drift(self) -> None:
        receipt = valid_scoped_start_receipt()
        bound_changes = {
            "tenant_id": {"tenant_id": "tenant-other"},
            "workspace_id": {"workspace_id": "workspace-other"},
            "invocation_id": {"invocation_id": "invocation-other"},
            "session_id": {"session_id": "session-other"},
            "plan_id": {"plan_id": "plan-other"},
            "task_id": {"task_id": "task-other", "causation_id": "task-other"},
            "agent_id": {"agent_id": "agent-other"},
            "job_idempotency_key": {"job_idempotency_key": "invoke:other"},
            "manifest_digest": {"manifest_digest": "0" * 64},
            "runtime_revision": {"runtime_revision": "runtime:other"},
            "correlation_id": {"correlation_id": "correlation-other"},
            "causation_id": {"task_id": "task-other", "causation_id": "task-other"},
        }
        for field_name, changes in bound_changes.items():
            with self.subTest(field=field_name):
                changed_receipt = _receipt_with_evidence_changes(receipt, **changes)
                with self.assertRaisesRegex(ValueError, "does not match the start receipt"):
                    request_for(
                        start_receipt=changed_receipt,
                        candidates=(),
                        manifest=manifest_for(()),
                    )

        with self.assertRaisesRegex(ValueError, "causationId must equal taskId"):
            replace(receipt.evidence, causation_id="task-other")

    def test_unbound_start_evidence_drift_remains_cryptographically_visible(self) -> None:
        receipt = valid_scoped_start_receipt()
        baseline = request_for(
            start_receipt=receipt,
            candidates=(),
            manifest=manifest_for(()),
        )
        unbound_changes = {
            "attempt_id": {"attempt_id": "attempt-scoped-other"},
            "attempt_number": {"attempt_number": receipt.evidence.attempt_number + 1},
            "lease_epoch": {"lease_epoch": receipt.evidence.lease_epoch + 1},
            "worker_id": {"worker_id": "worker-scoped-other"},
            "lease_token_digest": {"lease_token_digest": "0" * 64},
            "claimed_at": {"claimed_at": "2026-08-27T09:00:02.000000Z"},
            "lease_expires_at": {"lease_expires_at": "2026-08-27T09:02:01.000000Z"},
            "envelope_digest": {"envelope_digest": "e" * 64},
            "context_digest": {"context_digest": "f" * 64},
            "authorization_digest": {"authorization_digest": "0" * 64},
        }
        for field_name, changes in unbound_changes.items():
            with self.subTest(field=field_name):
                changed_receipt = _receipt_with_evidence_changes(receipt, **changes)
                changed = request_for(
                    start_receipt=changed_receipt,
                    candidates=(),
                    manifest=manifest_for(()),
                )
                self.assertNotEqual(changed.start_receipt_digest, baseline.start_receipt_digest)
                self.assertNotEqual(changed.canonical_digest(), baseline.canonical_digest())

        with self.assertRaisesRegex(ValueError, "schemaVersion"):
            replace(receipt.evidence, schema_version=receipt.evidence.schema_version + 1)

    def test_every_manifest_start_binding_rejects_drift(self) -> None:
        baseline_manifest = manifest_for(())
        bound_changes = {
            "tenant_id": {"tenant_id": "tenant-other"},
            "workspace_id": {"workspace_id": "workspace-other"},
            "invocation_id": {"invocation_id": "invocation-other"},
            "session_id": {"session_id": "session-other"},
            "plan_id": {"plan_id": "plan-other"},
            "task_id": {"task_id": "task-other", "causation_id": "task-other"},
            "agent_id": {"agent_id": "agent-other"},
            "job_idempotency_key": {"job_idempotency_key": "invoke:other"},
            "execution_manifest_digest": {"execution_manifest_digest": "0" * 64},
            "runtime_revision": {"runtime_revision": "runtime:other"},
            "correlation_id": {"correlation_id": "correlation-other"},
            "causation_id": {"task_id": "task-other", "causation_id": "task-other"},
        }
        for field_name, changes in bound_changes.items():
            with self.subTest(field=field_name):
                changed_manifest = replace(baseline_manifest, **changes)
                with self.assertRaisesRegex(ValueError, "does not match the start receipt"):
                    request_for(candidates=(), manifest=changed_manifest)

        with self.assertRaisesRegex(ValueError, "causationId must equal taskId"):
            replace(baseline_manifest, causation_id="task-other")

    def test_free_manifest_drift_is_accepted_and_digest_bound(self) -> None:
        baseline_manifest = manifest_for(())
        baseline = request_for(candidates=(), manifest=baseline_manifest)
        free_changes = {
            "task_revision": {"task_revision": baseline_manifest.task_revision + 1},
            "result_ref": {"result_ref": "result:invocation-scoped-other"},
            "narration": {"narration": "different narration"},
            "metadata": {"metadata": {"provider": "other"}},
        }
        for field_name, changes in free_changes.items():
            with self.subTest(field=field_name):
                changed = request_for(
                    candidates=(),
                    manifest=replace(baseline_manifest, **changes),
                )
                self.assertNotEqual(
                    changed.manifest.canonical_digest(), baseline.manifest.canonical_digest()
                )
                self.assertNotEqual(changed.canonical_digest(), baseline.canonical_digest())

        first = candidate_for(1)
        second = candidate_for(2)
        two_artifact_manifest = manifest_for((first, second))
        changed_primary = request_for(
            candidates=(first, second),
            manifest=replace(
                two_artifact_manifest,
                primary_artifact_id=second.artifact_id,
            ),
        )
        baseline_primary = request_for(
            candidates=(first, second),
            manifest=two_artifact_manifest,
        )
        self.assertNotEqual(changed_primary.canonical_digest(), baseline_primary.canonical_digest())

    def test_manifest_schema_and_effect_policy_drift_are_rejected(self) -> None:
        manifest = manifest_for(())
        with self.assertRaisesRegex(ValueError, "schemaVersion is unsupported"):
            replace(manifest, schema_version=manifest.schema_version + 1)
        with self.assertRaisesRegex(TypeError, "schemaVersion must be an exact integer"):
            replace(manifest, schema_version=True)

        effectful = replace(
            manifest,
            effect_class=EffectClass.IDEMPOTENT,
            action_receipt_set_digest="a" * 64,
        )
        for field_name in ("effect_class", "action_receipt_set_digest"):
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValueError, "only effectClass pure"):
                    request_for(candidates=(), manifest=effectful)

        with self.assertRaisesRegex(ValueError, "canonical empty action receipt set"):
            replace(manifest, action_receipt_set_digest="a" * 64)

    def test_every_candidate_field_drift_breaks_the_exact_descriptor_bijection(self) -> None:
        candidate = candidate_for()
        manifest = manifest_for((candidate,))
        candidate_changes = {
            "tenant_id": {"tenant_id": "tenant-other"},
            "workspace_id": {"workspace_id": "workspace-other"},
            "session_id": {"session_id": "session-other"},
            "task_id": {"task_id": "task-other"},
            "artifact_id": {"artifact_id": "artifact-result-other"},
            "name": {"name": "artifact-other.md"},
            "media_type": {"media_type": "text/plain"},
            "content": {"content": b"different content"},
            "metadata_canonical_bytes": {
                "metadata_canonical_bytes": candidate_for(2).metadata_canonical_bytes
            },
            "created_by": {"created_by": "agent-other"},
            "idempotency_key": {"idempotency_key": "result-artifact:other"},
            "expected_head_version": {"expected_head_version": 1},
        }
        self.assertEqual(set(candidate_changes), _init_field_names(type(candidate)))
        for field_name, changes in candidate_changes.items():
            with self.subTest(field=field_name):
                changed_candidate = replace(candidate, **changes)
                with self.assertRaisesRegex(ValueError, "artifact candidate"):
                    request_for(candidates=(changed_candidate,), manifest=manifest)

    def test_every_descriptor_field_drift_is_rejected(self) -> None:
        candidate = candidate_for()
        manifest = manifest_for((candidate,))
        descriptor = manifest.artifacts[0]
        descriptor_changes = {
            "artifact_id": {"artifact_id": "artifact-result-other"},
            "name": {"name": "artifact-other.md"},
            "version": {"version": 2, "parent_version": 1},
            "parent_version": {"version": 2, "parent_version": 1},
            "media_type": {"media_type": "text/plain"},
            "blob_digest": {"blob_digest": "sha256:" + ("0" * 64)},
            "byte_size": {"byte_size": descriptor.byte_size + 1},
            "metadata_digest": {"metadata_digest": "0" * 64},
            "created_by": {"created_by": "agent-other"},
            "idempotency_key": {"idempotency_key": "result-artifact:other"},
            "request_digest": {"request_digest": "0" * 64},
        }
        self.assertEqual(set(descriptor_changes), _init_field_names(type(descriptor)))
        for field_name, changes in descriptor_changes.items():
            with self.subTest(field=field_name):
                changed_descriptor = replace(descriptor, **changes)
                with self.assertRaises(ValueError):
                    changed_manifest = replace(manifest, artifacts=(changed_descriptor,))
                    request_for(candidates=(candidate,), manifest=changed_manifest)

        with self.assertRaisesRegex(ValueError, "parentVersion is required"):
            replace(descriptor, version=2)
        with self.assertRaisesRegex(ValueError, "immediately precede"):
            replace(descriptor, parent_version=1)

    def test_candidate_count_order_and_manifest_artifact_order_are_exact(self) -> None:
        first = candidate_for(1)
        second = candidate_for(2)
        manifest = manifest_for((first, second))
        invalid_candidate_sequences = {
            "missing": (first,),
            "extra": (first, second, candidate_for(3)),
            "reversed": (second, first),
            "duplicated": (first, first),
        }
        for drift, candidates in invalid_candidate_sequences.items():
            with self.subTest(drift=drift):
                with self.assertRaises(ValueError):
                    request_for(candidates=candidates, manifest=manifest)

        reversed_manifest = replace(
            manifest,
            primary_artifact_id=second.artifact_id,
            artifacts=tuple(reversed(manifest.artifacts)),
        )
        with self.assertRaisesRegex(ValueError, "ordered descriptor"):
            request_for(candidates=(first, second), manifest=reversed_manifest)

    def test_coherent_artifact_drift_is_accepted_and_digest_bound(self) -> None:
        candidate = candidate_for()
        baseline = request_for(candidates=(candidate,), manifest=manifest_for((candidate,)))
        coherent_changes = {
            "artifact_id": {"artifact_id": "artifact-result-other"},
            "name": {"name": "artifact-other.md"},
            "media_type": {"media_type": "text/plain"},
            "content": {"content": b"different content"},
            "metadata_canonical_bytes": {
                "metadata_canonical_bytes": candidate_for(2).metadata_canonical_bytes
            },
            "idempotency_key": {"idempotency_key": "result-artifact:other"},
            "expected_head_version": {"expected_head_version": 1},
        }
        for field_name, changes in coherent_changes.items():
            with self.subTest(field=field_name):
                changed_candidate = replace(candidate, **changes)
                changed = request_for(
                    candidates=(changed_candidate,),
                    manifest=manifest_for((changed_candidate,)),
                )
                self.assertNotEqual(
                    changed.artifact_candidates[0].canonical_digest(),
                    baseline.artifact_candidates[0].canonical_digest(),
                )
                self.assertNotEqual(changed.canonical_digest(), baseline.canonical_digest())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
