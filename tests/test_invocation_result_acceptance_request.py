from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement.invocation_execution import EffectClass, ScopedInvocationStartReceiptV3
from quantum_entanglement.invocation_results import (
    EMPTY_ACTION_RECEIPT_SET_DIGEST,
    SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_DOMAIN,
    SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
    SCOPED_INVOCATION_START_RECEIPT_DIGEST_DOMAIN,
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultArtifactCandidateV2,
    ScopedInvocationResultManifestV2,
    scoped_invocation_start_receipt_digest_v3,
)
from tests.test_scoped_invocation_execution import valid_scoped_start_receipt


def candidate_for(
    index: int = 1,
    *,
    content: bytes | None = None,
    metadata: object | None = None,
) -> ScopedInvocationResultArtifactCandidateV2:
    evidence = valid_scoped_start_receipt().evidence
    return ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
        tenant_id=evidence.tenant_id,
        workspace_id=evidence.workspace_id,
        session_id=evidence.session_id,
        task_id=evidence.task_id,
        artifact_id=f"artifact-result-{index}",
        name=f"artifact-{index}.md",
        media_type="text/markdown",
        content=f"content-{index}".encode() if content is None else content,
        metadata={"index": index} if metadata is None else metadata,
        created_by=evidence.agent_id,
        idempotency_key=f"result-artifact:invocation-scoped-1:{index}",
        expected_head_version=0,
    )


def manifest_for(
    candidates: tuple[ScopedInvocationResultArtifactCandidateV2, ...],
    *,
    effect_class: EffectClass = EffectClass.PURE,
    action_receipt_set_digest: str = EMPTY_ACTION_RECEIPT_SET_DIGEST,
    narration: str = "completed result",
) -> ScopedInvocationResultManifestV2:
    evidence = valid_scoped_start_receipt().evidence
    return ScopedInvocationResultManifestV2(
        schema_version=SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
        tenant_id=evidence.tenant_id,
        workspace_id=evidence.workspace_id,
        invocation_id=evidence.invocation_id,
        session_id=evidence.session_id,
        plan_id=evidence.plan_id,
        task_id=evidence.task_id,
        agent_id=evidence.agent_id,
        job_idempotency_key=evidence.job_idempotency_key,
        task_revision=19,
        correlation_id=evidence.correlation_id,
        causation_id=evidence.causation_id,
        runtime_revision=evidence.runtime_revision,
        execution_manifest_digest=evidence.manifest_digest,
        effect_class=effect_class,
        action_receipt_set_digest=action_receipt_set_digest,
        result_ref="result:invocation-scoped-1",
        narration=narration,
        metadata={"provider": "fake"},
        primary_artifact_id=candidates[0].artifact_id if candidates else None,
        artifacts=tuple(candidate.to_descriptor() for candidate in candidates),
    )


def request_for(
    *,
    start_receipt: ScopedInvocationStartReceiptV3 | None = None,
    candidates: tuple[ScopedInvocationResultArtifactCandidateV2, ...] | None = None,
    manifest: ScopedInvocationResultManifestV2 | None = None,
    expected_stream_version: int | None = None,
    acceptance_idempotency_key: str = "accept:invocation-scoped-1",
) -> ScopedInvocationResultAcceptanceRequestV2:
    receipt = valid_scoped_start_receipt() if start_receipt is None else start_receipt
    selected_candidates = (candidate_for(),) if candidates is None else candidates
    selected_manifest = manifest_for(selected_candidates) if manifest is None else manifest
    return ScopedInvocationResultAcceptanceRequestV2(
        schema_version=SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION,
        acceptance_idempotency_key=acceptance_idempotency_key,
        start_receipt=receipt,
        manifest=selected_manifest,
        artifact_candidates=selected_candidates,
        expected_stream_version=(
            receipt.sequence if expected_stream_version is None else expected_stream_version
        ),
    )


class ScopedInvocationResultAcceptanceRequestTests(unittest.TestCase):
    def test_request_snapshots_all_values_and_has_no_raw_serialization(self) -> None:
        receipt = valid_scoped_start_receipt()
        candidate = candidate_for(content=b"request-content-secret")
        manifest = manifest_for((candidate,), narration="request-narration-secret")
        request = request_for(
            start_receipt=receipt,
            candidates=(candidate,),
            manifest=manifest,
        )

        self.assertIsNot(request.start_receipt, receipt)
        self.assertIsNot(request.manifest, manifest)
        self.assertIsNot(request.artifact_candidates[0], candidate)
        self.assertFalse(hasattr(request, "to_dict"))
        self.assertNotIn("request-content-secret", repr(request))
        self.assertNotIn("request-narration-secret", repr(request))
        self.assertEqual(len(request.canonical_digest()), 64)

    def test_start_receipt_digest_is_domain_separated_and_deterministic(self) -> None:
        receipt = valid_scoped_start_receipt()
        canonical = json.dumps(
            receipt.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected = hashlib.sha256(
            SCOPED_INVOCATION_START_RECEIPT_DIGEST_DOMAIN.encode("utf-8") + canonical
        ).hexdigest()
        self.assertEqual(scoped_invocation_start_receipt_digest_v3(receipt), expected)
        self.assertEqual(request_for().start_receipt_digest, expected)

    def test_request_digest_is_domain_separated_and_covers_top_level_fields(self) -> None:
        request = request_for()
        expected = hashlib.sha256(
            SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_DOMAIN.encode("utf-8")
            + json.dumps(
                ScopedInvocationResultAcceptanceRequestV2._identity_dict(request),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(request.canonical_digest(), expected)

        changed_requests = (
            request_for(acceptance_idempotency_key="accept:invocation-scoped-2"),
            request_for(expected_stream_version=request.expected_stream_version + 1),
            request_for(manifest=replace(request.manifest, narration="changed narration")),
            request_for(candidates=(replace(request.artifact_candidates[0], content=b"changed"),)),
        )
        for changed in changed_requests:
            with self.subTest(changed=changed.acceptance_idempotency_key):
                self.assertNotEqual(changed.canonical_digest(), request.canonical_digest())

    def test_digests_ignore_instance_method_shadowing(self) -> None:
        receipt = valid_scoped_start_receipt()
        receipt_baseline = scoped_invocation_start_receipt_digest_v3(receipt)
        other_receipt = replace(receipt, event_id="event-shadowed")
        object.__setattr__(receipt, "to_dict", other_receipt.to_dict)
        self.assertEqual(scoped_invocation_start_receipt_digest_v3(receipt), receipt_baseline)

        candidate = candidate_for()
        manifest = manifest_for((candidate,))
        baseline = request_for(candidates=(candidate,), manifest=manifest).canonical_digest()
        object.__setattr__(candidate, "_identity_dict", lambda: {"forged": True})
        object.__setattr__(
            manifest,
            "to_dict",
            replace(manifest, narration="forged narration").to_dict,
        )
        request = request_for(candidates=(candidate,), manifest=manifest)
        self.assertEqual(request.canonical_digest(), baseline)

        object.__setattr__(request, "_identity_dict", lambda: {"forged": True})
        self.assertEqual(
            ScopedInvocationResultAcceptanceRequestV2.canonical_digest(request),
            baseline,
        )

    def test_narration_only_result_is_a_valid_request(self) -> None:
        request = request_for(candidates=(), manifest=manifest_for(()))
        self.assertEqual(request.artifact_candidates, ())
        self.assertEqual(request.manifest.artifacts, ())
        self.assertIsNone(request.manifest.primary_artifact_id)

    def test_request_rejects_non_pure_manifest_even_when_codec_valid(self) -> None:
        candidates = (candidate_for(),)
        effectful = manifest_for(
            candidates,
            effect_class=EffectClass.IDEMPOTENT,
            action_receipt_set_digest="a" * 64,
        )
        self.assertIs(effectful.effect_class, EffectClass.IDEMPOTENT)
        with self.assertRaisesRegex(ValueError, "only effectClass pure"):
            request_for(candidates=candidates, manifest=effectful)

    def test_every_manifest_start_binding_is_revalidated(self) -> None:
        request = request_for()
        manifest = request.manifest
        changes = (
            {"tenant_id": "tenant-other"},
            {"workspace_id": "workspace-other"},
            {"invocation_id": "invocation-other"},
            {"session_id": "session-other"},
            {"plan_id": "plan-other"},
            {"job_idempotency_key": "invoke:other"},
            {"execution_manifest_digest": "a" * 64},
            {"runtime_revision": "runtime:other"},
            {"correlation_id": "correlation-other"},
        )
        for change in changes:
            with self.subTest(change=tuple(change)):
                with self.assertRaisesRegex(ValueError, "does not match the start"):
                    request_for(
                        candidates=request.artifact_candidates,
                        manifest=replace(manifest, **change),
                    )

        changed_agent_candidate = replace(
            request.artifact_candidates[0],
            created_by="agent-other",
        )
        changed_agent_manifest = replace(
            manifest,
            agent_id="agent-other",
            artifacts=(changed_agent_candidate.to_descriptor(),),
        )
        with self.assertRaisesRegex(ValueError, "agentId does not match the start"):
            request_for(
                candidates=(changed_agent_candidate,),
                manifest=changed_agent_manifest,
            )

        with self.assertRaisesRegex(ValueError, "taskId"):
            request_for(
                candidates=request.artifact_candidates,
                manifest=replace(manifest, task_id="task-other", causation_id="task-other"),
            )

    def test_candidate_scope_order_count_and_descriptor_are_exact(self) -> None:
        first = candidate_for(1)
        second = candidate_for(2)
        manifest = manifest_for((first, second))

        invalid_candidates = (
            (first,),
            (second, first),
            (replace(first, tenant_id="tenant-other"), second),
            (replace(first, content=b"changed"), second),
            (replace(first, expected_head_version=1), second),
        )
        for candidates in invalid_candidates:
            with self.subTest(count=len(candidates)):
                with self.assertRaises(ValueError):
                    request_for(candidates=candidates, manifest=manifest)

        reversed_manifest = manifest_for((second, first))
        reversed_request = request_for(
            candidates=(second, first),
            manifest=reversed_manifest,
        )
        forward_request = request_for(candidates=(first, second), manifest=manifest)
        self.assertNotEqual(reversed_request.canonical_digest(), forward_request.canonical_digest())

    def test_candidate_count_fails_before_any_candidate_snapshot(self) -> None:
        candidate = candidate_for()
        manifest = manifest_for((candidate,))
        with patch.object(invocation_results_module, "_artifact_candidate_snapshot") as snapshot:
            with self.assertRaisesRegex(ValueError, "at most 256"):
                request_for(
                    candidates=(candidate,) * 257,
                    manifest=manifest,
                )
        snapshot.assert_not_called()

    def test_forged_manifest_count_fails_before_any_descriptor_snapshot(self) -> None:
        candidate = candidate_for()
        manifest = manifest_for((candidate,))
        object.__setattr__(manifest, "artifacts", manifest.artifacts * 257)
        with patch.object(invocation_results_module, "_artifact_descriptor_snapshot") as snapshot:
            with self.assertRaisesRegex(ValueError, "at most 256 descriptors"):
                request_for(candidates=(candidate,), manifest=manifest)
        snapshot.assert_not_called()

    def test_every_artifact_identity_uses_the_persistence_character_limit(self) -> None:
        persistence_fields = {
            "tenant_id": "tenant_id",
            "workspace_id": "workspace_id",
            "session_id": "session_id",
            "task_id": "task_id",
            "artifact_id": None,
            "name": None,
            "created_by": "agent_id",
            "idempotency_key": None,
        }

        def coherent_request(field_name: str, value: str) -> object:
            receipt = valid_scoped_start_receipt()
            evidence_changes: dict[str, object] = {}
            manifest_changes: dict[str, object] = {}
            bound_field = persistence_fields[field_name]
            if bound_field is not None:
                evidence_changes[bound_field] = value
                manifest_changes[bound_field] = value
            if field_name == "task_id":
                evidence_changes["causation_id"] = value
                manifest_changes["causation_id"] = value
            evidence = replace(receipt.evidence, **evidence_changes)
            receipt = replace(
                receipt,
                stream_id=("session:" + value if field_name == "session_id" else receipt.stream_id),
                evidence=evidence,
            )

            baseline_candidate = candidate_for()
            candidate = replace(baseline_candidate, **{field_name: value})
            manifest_changes.update(
                artifacts=(candidate.to_descriptor(),),
                primary_artifact_id=candidate.artifact_id,
            )
            manifest = replace(manifest_for((baseline_candidate,)), **manifest_changes)
            return request_for(
                start_receipt=receipt,
                candidates=(candidate,),
                manifest=manifest,
            )

        maximum = invocation_results_module.MAX_ARTIFACT_IDENTITY_CHARACTERS
        for field_name in persistence_fields:
            with self.subTest(field=field_name, boundary="accepted"):
                coherent_request(field_name, "界" * maximum)
            with self.subTest(field=field_name, boundary="rejected"):
                with self.assertRaisesRegex(ValueError, "Artifact persistence limit"):
                    coherent_request(field_name, "界" * (maximum + 1))

    def test_expected_stream_version_is_bound_and_includes_start(self) -> None:
        receipt = valid_scoped_start_receipt()
        with self.assertRaisesRegex(ValueError, "include the stored start"):
            request_for(expected_stream_version=receipt.sequence - 1)
        with self.assertRaises(TypeError):
            request_for(expected_stream_version=True)  # type: ignore[arg-type]

    def test_request_reserves_terminal_event_and_task_revision_capacity(self) -> None:
        maximum = invocation_results_module._MAX_SQLITE_INTEGER
        boundary = request_for(expected_stream_version=maximum - 2)
        self.assertEqual(boundary.expected_stream_version, maximum - 2)
        with self.assertRaisesRegex(ValueError, "space for two terminal events"):
            request_for(expected_stream_version=maximum - 1)
        with self.assertRaisesRegex(ValueError, "space for two terminal events"):
            request_for(expected_stream_version=maximum)

        candidates = (candidate_for(),)
        boundary_manifest = replace(manifest_for(candidates), task_revision=maximum - 1)
        self.assertEqual(
            request_for(candidates=candidates, manifest=boundary_manifest).manifest.task_revision,
            maximum - 1,
        )
        with self.assertRaisesRegex(ValueError, "cannot allocate a terminal revision"):
            request_for(
                candidates=candidates,
                manifest=replace(boundary_manifest, task_revision=maximum),
            )

    def test_aggregate_artifact_limits_fail_before_acceptance(self) -> None:
        candidates = (
            candidate_for(1, content=b"123456"),
            candidate_for(2, content=b"abcdef"),
            candidate_for(3, content=b"unused"),
        )
        with (
            patch.object(invocation_results_module, "_MAX_RESULT_CONTENT_BYTES", 10),
            patch.object(
                invocation_results_module,
                "_artifact_candidate_snapshot",
                wraps=invocation_results_module._artifact_candidate_snapshot,
            ) as snapshot,
        ):
            with self.assertRaisesRegex(ValueError, "aggregate content"):
                request_for(candidates=candidates, manifest=manifest_for(candidates))
        self.assertEqual(snapshot.call_count, 2)

        metadata_candidates = tuple(candidate_for(index, metadata={}) for index in range(1, 7))
        with patch.object(invocation_results_module, "_MAX_RESULT_ARTIFACT_METADATA_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "aggregate metadata"):
                request_for(
                    candidates=metadata_candidates,
                    manifest=manifest_for(metadata_candidates),
                )

    def test_request_requires_exact_classes_and_stays_internal(self) -> None:
        request = request_for()
        with self.assertRaisesRegex(TypeError, "exact tuple"):
            replace(request, artifact_candidates=list(request.artifact_candidates))  # type: ignore[arg-type]

        class RequestSubclass(ScopedInvocationResultAcceptanceRequestV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact"):
            RequestSubclass(
                schema_version=request.schema_version,
                acceptance_idempotency_key=request.acceptance_idempotency_key,
                start_receipt=request.start_receipt,
                manifest=request.manifest,
                artifact_candidates=request.artifact_candidates,
                expected_stream_version=request.expected_stream_version,
            )

        name = "ScopedInvocationResultAcceptanceRequestV2"
        self.assertNotIn(name, invocation_results_module.__all__)
        self.assertNotIn(name, quantum_entanglement.__all__)
        self.assertFalse(hasattr(quantum_entanglement, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
