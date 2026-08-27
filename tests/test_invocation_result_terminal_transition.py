from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import fields, replace

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement.invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    EffectClass,
)
from quantum_entanglement.invocation_results import (
    SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_DOMAIN,
    SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_SCHEMA_VERSION,
    ScopedInvocationResultAcceptanceRequestV2,
    ScopedInvocationResultEvidenceV2,
    ScopedInvocationResultManifestV2,
    ScopedInvocationResultTerminalTransitionV2,
    build_scoped_invocation_result_terminal_transition_v2,
)
from quantum_entanglement.protocol import TaskStatus
from tests.test_invocation_result_acceptance_request import request_for


def evidence_for(
    request: ScopedInvocationResultAcceptanceRequestV2,
    **changes: object,
) -> ScopedInvocationResultEvidenceV2:
    manifest = request.manifest
    start = request.start_receipt.evidence
    values: dict[str, object] = {
        "schema_version": SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": "attempt_bound",
        "receipt_id": "receipt-result-scoped-1",
        "tenant_id": manifest.tenant_id,
        "workspace_id": manifest.workspace_id,
        "invocation_id": manifest.invocation_id,
        "session_id": manifest.session_id,
        "plan_id": manifest.plan_id,
        "task_id": manifest.task_id,
        "agent_id": manifest.agent_id,
        "job_idempotency_key": manifest.job_idempotency_key,
        "running_task_revision": manifest.task_revision,
        "terminal_task_revision": manifest.task_revision + 1,
        "attempt_id": start.attempt_id,
        "attempt_number": start.attempt_number,
        "lease_epoch": start.lease_epoch,
        "worker_id": start.worker_id,
        "lease_token_digest": start.lease_token_digest,
        "start_receipt_digest": request.start_receipt_digest,
        "execution_manifest_digest": manifest.execution_manifest_digest,
        "result_manifest_schema_version": manifest.schema_version,
        "result_manifest_digest": ScopedInvocationResultManifestV2.canonical_digest(manifest),
        "result_ref": manifest.result_ref,
        "effect_class": manifest.effect_class,
        "action_receipt_set_digest": manifest.action_receipt_set_digest,
        "acceptance_idempotency_key": request.acceptance_idempotency_key,
        "request_digest": ScopedInvocationResultAcceptanceRequestV2.canonical_digest(request),
        "accepted_at": "2026-08-27T12:34:56.123456Z",
        "artifact_count": len(request.artifact_candidates),
    }
    values.update(changes)
    return ScopedInvocationResultEvidenceV2(**values)  # type: ignore[arg-type]


def valid_transition() -> ScopedInvocationResultTerminalTransitionV2:
    request = request_for()
    return build_scoped_invocation_result_terminal_transition_v2(
        request,
        evidence_for(request),
        result_event_id="event-result-accepted-1",
    )


class ScopedInvocationResultTerminalTransitionTests(unittest.TestCase):
    def test_builder_creates_full_scoped_result_bound_payload(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id="event-result-accepted-1",
        )

        self.assertEqual(
            transition.schema_version,
            SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_SCHEMA_VERSION,
        )
        self.assertEqual(transition.tenant_id, request.manifest.tenant_id)
        self.assertEqual(transition.workspace_id, request.manifest.workspace_id)
        self.assertEqual(transition.result_receipt_id, evidence.receipt_id)
        self.assertEqual(
            transition.result_evidence_digest,
            ScopedInvocationResultEvidenceV2.canonical_digest(evidence),
        )
        self.assertIs(transition.previous, TaskStatus.RUNNING)
        self.assertIs(transition.current, TaskStatus.COMPLETED)
        self.assertIsNone(transition.reason)
        self.assertEqual(transition.stream_id, "session:" + request.manifest.session_id)
        self.assertEqual(transition.actor_id, CANONICAL_ORCHESTRATOR_ACTOR_ID)
        self.assertEqual(transition.causation_id, transition.result_event_id)
        self.assertEqual(
            transition.idempotency_key,
            f"task-status:{transition.task_id}:{transition.terminal_task_revision}",
        )
        self.assertNotIn(request.manifest.narration, repr(transition))
        self.assertNotIn(request.manifest.narration, json.dumps(transition.to_dict()))

    def test_wire_round_trip_and_domain_digest_are_canonical(self) -> None:
        transition = valid_transition()
        wire = transition.to_dict()
        canonical = json.dumps(
            wire,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        expected = hashlib.sha256(
            SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_DOMAIN.encode() + canonical
        ).hexdigest()
        decoded = ScopedInvocationResultTerminalTransitionV2.from_dict(wire)

        self.assertEqual(decoded, transition)
        self.assertIsNot(decoded, transition)
        self.assertEqual(transition.canonical_bytes(), canonical)
        self.assertEqual(transition.canonical_digest(), expected)

    def test_field_inventory_and_exact_wire_shape_are_closed(self) -> None:
        expected_fields = {
            "schema_version",
            "transition_kind",
            "tenant_id",
            "workspace_id",
            "invocation_id",
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "job_idempotency_key",
            "runtime_revision",
            "correlation_id",
            "previous",
            "current",
            "reason",
            "running_task_revision",
            "terminal_task_revision",
            "result_receipt_id",
            "result_event_id",
            "result_evidence_digest",
        }
        self.assertEqual(
            {item.name for item in fields(ScopedInvocationResultTerminalTransitionV2) if item.init},
            expected_fields,
        )

        wire = valid_transition().to_dict()
        for field_name in tuple(wire):
            with self.subTest(field=field_name):
                changed = dict(wire)
                del changed[field_name]
                with self.assertRaisesRegex(ValueError, "exact schema"):
                    ScopedInvocationResultTerminalTransitionV2.from_dict(changed)
        with self.assertRaisesRegex(ValueError, "exact schema"):
            ScopedInvocationResultTerminalTransitionV2.from_dict({**wire, "future": True})
        with self.assertRaises(TypeError):
            ScopedInvocationResultTerminalTransitionV2.from_dict(tuple(wire.items()))

    def test_kind_status_reason_and_revision_are_exact(self) -> None:
        transition = valid_transition()
        invalid = (
            {"schema_version": True},
            {"schema_version": 1},
            {"transition_kind": "legacy"},
            {"previous": TaskStatus.READY},
            {"previous": TaskStatus.RUNNING.value},
            {"current": TaskStatus.FAILED},
            {"current": TaskStatus.COMPLETED.value},
            {"reason": "done"},
            {"running_task_revision": True},
            {"running_task_revision": 0},
            {"terminal_task_revision": transition.running_task_revision},
            {"terminal_task_revision": transition.terminal_task_revision + 1},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    replace(transition, **change)

        maximum = invocation_results_module._MAX_SQLITE_INTEGER
        boundary = replace(
            transition,
            running_task_revision=maximum - 1,
            terminal_task_revision=maximum,
        )
        self.assertEqual(boundary.terminal_task_revision, maximum)
        with self.assertRaisesRegex(ValueError, "immediately follow"):
            replace(
                transition,
                running_task_revision=maximum,
                terminal_task_revision=maximum,
            )

    def test_identity_and_digest_values_are_strict(self) -> None:
        transition = valid_transition()
        identity_fields = (
            "tenant_id",
            "workspace_id",
            "invocation_id",
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "job_idempotency_key",
            "runtime_revision",
            "correlation_id",
            "result_receipt_id",
            "result_event_id",
        )
        for field_name in identity_fields:
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    replace(transition, **{field_name: " bad"})
        with self.assertRaises(ValueError):
            replace(transition, result_evidence_digest="A" * 64)

    def test_derived_envelope_identities_obey_the_canonical_byte_limit(self) -> None:
        transition = valid_transition()
        maximum = invocation_results_module._MAX_IDENTITY_BYTES
        stream_prefix = len(b"session:")
        accepted_session = "s" * (maximum - stream_prefix)
        self.assertEqual(
            len(replace(transition, session_id=accepted_session).stream_id.encode()),
            maximum,
        )
        with self.assertRaisesRegex(ValueError, "terminal streamId"):
            replace(transition, session_id=accepted_session + "s")

        idempotency_overhead = len(f"task-status::{transition.terminal_task_revision}".encode())
        accepted_task = "t" * (maximum - idempotency_overhead)
        self.assertEqual(
            len(replace(transition, task_id=accepted_task).idempotency_key.encode()),
            maximum,
        )
        with self.assertRaisesRegex(ValueError, "terminal idempotencyKey"):
            replace(transition, task_id=accepted_task + "t")

    def test_evidence_field_inventory_classifies_every_builder_input(self) -> None:
        builder_bound = {
            "tenant_id",
            "workspace_id",
            "invocation_id",
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "job_idempotency_key",
            "running_task_revision",
            "attempt_id",
            "attempt_number",
            "lease_epoch",
            "worker_id",
            "lease_token_digest",
            "start_receipt_digest",
            "execution_manifest_digest",
            "result_manifest_digest",
            "result_ref",
            "acceptance_idempotency_key",
            "request_digest",
            "artifact_count",
        }
        coherently_variable = {"receipt_id", "accepted_at"}
        locally_fixed_or_coupled = {
            "schema_version",
            "evidence_kind",
            "terminal_task_revision",
            "result_manifest_schema_version",
            "effect_class",
            "action_receipt_set_digest",
        }
        inventory = {item.name for item in fields(ScopedInvocationResultEvidenceV2) if item.init}
        self.assertEqual(
            builder_bound | coherently_variable | locally_fixed_or_coupled,
            inventory,
        )

        request = request_for()
        locally_rejected = (
            {"schema_version": 1},
            {"evidence_kind": "legacy"},
            {"terminal_task_revision": request.manifest.task_revision + 2},
            {"result_manifest_schema_version": 1},
            {"effect_class": EffectClass.IDEMPOTENT},
            {"action_receipt_set_digest": "0" * 64},
        )
        for change in locally_rejected:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    evidence_for(request, **change)

    def test_builder_rejects_every_evidence_request_binding_drift(self) -> None:
        request = request_for()
        manifest_revision = request.manifest.task_revision
        mismatches = {
            "tenant_id": {"tenant_id": "tenant-other"},
            "workspace_id": {"workspace_id": "workspace-other"},
            "invocation_id": {"invocation_id": "invocation-other"},
            "session_id": {"session_id": "session-other"},
            "plan_id": {"plan_id": "plan-other"},
            "task_id": {"task_id": "task-other"},
            "agent_id": {"agent_id": "agent-other"},
            "job_idempotency_key": {"job_idempotency_key": "invoke:other"},
            "attempt_id": {"attempt_id": "attempt-other"},
            "attempt_number": {"attempt_number": 2},
            "lease_epoch": {"lease_epoch": 2},
            "worker_id": {"worker_id": "worker-other"},
            "lease_token_digest": {"lease_token_digest": "0" * 64},
            "start_receipt_digest": {"start_receipt_digest": "0" * 64},
            "execution_manifest_digest": {"execution_manifest_digest": "0" * 64},
            "result_manifest_digest": {"result_manifest_digest": "0" * 64},
            "result_ref": {"result_ref": "result:other"},
            "acceptance_idempotency_key": {"acceptance_idempotency_key": "accept:other"},
            "request_digest": {"request_digest": "0" * 64},
            "artifact_count": {"artifact_count": 0},
            "running_task_revision": {
                "running_task_revision": manifest_revision + 1,
                "terminal_task_revision": manifest_revision + 2,
            },
        }
        for field_name, changes in mismatches.items():
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    build_scoped_invocation_result_terminal_transition_v2(
                        request,
                        evidence_for(request, **changes),
                        result_event_id="event-result-accepted-1",
                    )

    def test_coherent_unbound_evidence_fields_change_transition_digest(self) -> None:
        request = request_for()
        baseline = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence_for(request),
            result_event_id="event-result-accepted-1",
        )
        changed_receipt = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence_for(request, receipt_id="receipt-result-scoped-2"),
            result_event_id="event-result-accepted-1",
        )
        changed_time = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence_for(request, accepted_at="2026-08-27T12:34:57.123456Z"),
            result_event_id="event-result-accepted-1",
        )
        changed_event = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence_for(request),
            result_event_id="event-result-accepted-2",
        )
        for changed in (changed_receipt, changed_time, changed_event):
            self.assertNotEqual(changed.canonical_digest(), baseline.canonical_digest())

    def test_builder_and_digest_ignore_instance_method_shadowing(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        baseline = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id="event-result-accepted-1",
        )
        baseline_digest = baseline.canonical_digest()

        object.__setattr__(request, "canonical_digest", lambda: "0" * 64)
        object.__setattr__(request, "_identity_dict", lambda: {"forged": True})
        object.__setattr__(evidence, "canonical_digest", lambda: "0" * 64)
        object.__setattr__(evidence, "to_dict", lambda: {"forged": True})
        rebuilt = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id="event-result-accepted-1",
        )
        self.assertEqual(rebuilt.canonical_digest(), baseline_digest)

        object.__setattr__(rebuilt, "to_dict", lambda: {"forged": True})
        object.__setattr__(rebuilt, "canonical_bytes", lambda: b"forged")
        self.assertEqual(
            ScopedInvocationResultTerminalTransitionV2.canonical_digest(rebuilt),
            baseline_digest,
        )

    def test_builder_rejects_wrong_inputs_and_start_event_reuse(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        with self.assertRaises(TypeError):
            build_scoped_invocation_result_terminal_transition_v2(
                object(),
                evidence,
                result_event_id="event-result-accepted-1",
            )
        with self.assertRaises(TypeError):
            build_scoped_invocation_result_terminal_transition_v2(
                request,
                object(),
                result_event_id="event-result-accepted-1",
            )
        with self.assertRaisesRegex(ValueError, "differ from"):
            build_scoped_invocation_result_terminal_transition_v2(
                request,
                evidence,
                result_event_id=request.start_receipt.event_id,
            )

    def test_transition_remains_internal_until_receipt_and_recovery_support_exist(self) -> None:
        name = "ScopedInvocationResultTerminalTransitionV2"
        self.assertNotIn(name, invocation_results_module.__all__)
        self.assertNotIn(name, quantum_entanglement.__all__)
        self.assertFalse(hasattr(quantum_entanglement, name))

        class TransitionSubclass(ScopedInvocationResultTerminalTransitionV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            TransitionSubclass.from_dict(valid_transition().to_dict())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
