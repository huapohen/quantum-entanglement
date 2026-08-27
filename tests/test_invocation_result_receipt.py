from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import fields, replace

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement.invocation_results import (
    SCOPED_INVOCATION_RESULT_RECEIPT_DOMAIN,
    SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION,
    TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    ScopedInvocationResultEventCoordinatesV2,
    ScopedInvocationResultReceiptV2,
    build_scoped_invocation_result_receipt_v2,
    build_scoped_invocation_result_terminal_transition_v2,
)
from tests.test_invocation_result_acceptance_request import request_for
from tests.test_invocation_result_terminal_transition import evidence_for


def coordinates_for(
    request: object,
    *,
    event_id: str = "event-result-accepted-1",
    terminal_event_id: str = "event-task-completed-1",
    result_sequence: int | None = None,
    result_global_position: int | None = None,
    result_envelope_digest: str = "1" * 64,
    terminal_envelope_digest: str = "2" * 64,
) -> tuple[ScopedInvocationResultEventCoordinatesV2, ScopedInvocationResultEventCoordinatesV2]:
    typed = invocation_results_module._acceptance_request_snapshot(request)
    sequence = typed.expected_stream_version + 1 if result_sequence is None else result_sequence
    global_position = (
        max(typed.start_receipt.global_position + 1, sequence)
        if result_global_position is None
        else result_global_position
    )
    result = ScopedInvocationResultEventCoordinatesV2(
        event_id=event_id,
        stream_id=typed.start_receipt.stream_id,
        event_type=TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
        sequence=sequence,
        global_position=global_position,
        event_envelope_digest=result_envelope_digest,
    )
    terminal = ScopedInvocationResultEventCoordinatesV2(
        event_id=terminal_event_id,
        stream_id=typed.start_receipt.stream_id,
        event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
        sequence=sequence + 1,
        global_position=global_position + 1,
        event_envelope_digest=terminal_envelope_digest,
    )
    return result, terminal


def receipt_for() -> ScopedInvocationResultReceiptV2:
    request = request_for()
    evidence = evidence_for(request)
    result_event, terminal_event = coordinates_for(request)
    transition = build_scoped_invocation_result_terminal_transition_v2(
        request,
        evidence,
        result_event_id=result_event.event_id,
    )
    return build_scoped_invocation_result_receipt_v2(
        request,
        evidence,
        result_event=result_event,
        terminal_event=terminal_event,
        terminal_transition=transition,
    )


class ScopedInvocationResultReceiptTests(unittest.TestCase):
    def test_builder_creates_one_exact_self_verifying_graph(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        result_event, terminal_event = coordinates_for(request)
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id=result_event.event_id,
        )
        receipt = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=result_event,
            terminal_event=terminal_event,
            terminal_transition=transition,
        )

        self.assertEqual(receipt.schema_version, SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION)
        self.assertEqual(receipt.receipt_id, evidence.receipt_id)
        self.assertEqual(receipt.start_receipt, request.start_receipt)
        self.assertEqual(receipt.evidence, evidence)
        self.assertEqual(receipt.result_event, result_event)
        self.assertEqual(receipt.terminal_event, terminal_event)
        self.assertEqual(receipt.terminal_transition, transition)
        self.assertEqual(receipt.canonical_digest(), receipt.receipt_digest)
        for original, snapshot in (
            (request.start_receipt, receipt.start_receipt),
            (evidence, receipt.evidence),
            (result_event, receipt.result_event),
            (terminal_event, receipt.terminal_event),
            (transition, receipt.terminal_transition),
        ):
            self.assertIsNot(original, snapshot)

    def test_wire_round_trip_and_domain_digest_cover_the_full_graph(self) -> None:
        receipt = receipt_for()
        wire = receipt.to_dict()
        body = dict(wire)
        provided_digest = body.pop("receiptDigest")
        canonical = json.dumps(
            body,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_digest = hashlib.sha256(
            SCOPED_INVOCATION_RESULT_RECEIPT_DOMAIN.encode("utf-8") + canonical
        ).hexdigest()
        decoded = ScopedInvocationResultReceiptV2.from_dict(wire)

        self.assertEqual(decoded, receipt)
        self.assertIsNot(decoded, receipt)
        self.assertEqual(receipt.canonical_bytes(), canonical)
        self.assertEqual(provided_digest, expected_digest)
        self.assertEqual(receipt.canonical_digest(), expected_digest)

    def test_field_inventory_and_wire_shape_are_closed(self) -> None:
        self.assertEqual(
            {item.name for item in fields(ScopedInvocationResultReceiptV2) if item.init},
            {
                "schema_version",
                "receipt_id",
                "start_receipt",
                "evidence",
                "result_event",
                "terminal_event",
                "terminal_transition",
                "receipt_digest",
            },
        )
        wire = receipt_for().to_dict()
        for field_name in tuple(wire):
            with self.subTest(field=field_name):
                changed = dict(wire)
                del changed[field_name]
                with self.assertRaisesRegex(ValueError, "exact schema"):
                    ScopedInvocationResultReceiptV2.from_dict(changed)
        with self.assertRaisesRegex(ValueError, "exact schema"):
            ScopedInvocationResultReceiptV2.from_dict({**wire, "future": True})
        with self.assertRaises(TypeError):
            ScopedInvocationResultReceiptV2.from_dict(tuple(wire.items()))

    def test_receipt_rejects_top_level_identity_schema_and_digest_drift(self) -> None:
        receipt = receipt_for()
        invalid = (
            {"schema_version": True},
            {"schema_version": 1},
            {"receipt_id": "receipt-other"},
            {"receipt_id": " receipt-invalid"},
            {"receipt_digest": "0" * 64},
            {"receipt_digest": "A" * 64},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    replace(receipt, **change)

    def test_every_start_evidence_scope_attempt_and_fence_binding_is_checked(self) -> None:
        receipt = receipt_for()
        mismatches: tuple[dict[str, object], ...] = (
            {"tenant_id": "tenant-other"},
            {"workspace_id": "workspace-other"},
            {"invocation_id": "invocation-other"},
            {"session_id": "session-other"},
            {"plan_id": "plan-other"},
            {"task_id": "task-other"},
            {"agent_id": "agent-other"},
            {"job_idempotency_key": "invoke:other"},
            {"attempt_id": "attempt-other"},
            {"attempt_number": 2},
            {"lease_epoch": 2},
            {"worker_id": "worker-other"},
            {"lease_token_digest": "0" * 64},
            {"execution_manifest_digest": "0" * 64},
            {"start_receipt_digest": "0" * 64},
            {"accepted_at": "2026-08-27T08:59:59.999999Z"},
        )
        for change in mismatches:
            with self.subTest(change=change):
                changed_evidence = replace(receipt.evidence, **change)
                with self.assertRaises(ValueError):
                    replace(receipt, evidence=changed_evidence)

    def test_every_transition_scope_revision_and_result_binding_is_checked(self) -> None:
        receipt = receipt_for()
        mismatches: tuple[dict[str, object], ...] = (
            {"tenant_id": "tenant-other"},
            {"workspace_id": "workspace-other"},
            {"invocation_id": "invocation-other"},
            {"session_id": "session-other"},
            {"plan_id": "plan-other"},
            {"task_id": "task-other"},
            {"agent_id": "agent-other"},
            {"job_idempotency_key": "invoke:other"},
            {"runtime_revision": "runtime:other"},
            {"correlation_id": "correlation-other"},
            {"running_task_revision": 20, "terminal_task_revision": 21},
            {"result_receipt_id": "receipt-other"},
            {"result_event_id": "event-result-other"},
            {"result_evidence_digest": "0" * 64},
        )
        for change in mismatches:
            with self.subTest(change=change):
                changed_transition = replace(receipt.terminal_transition, **change)
                with self.assertRaises(ValueError):
                    replace(receipt, terminal_transition=changed_transition)

    def test_event_identity_stream_type_and_coordinates_are_exact(self) -> None:
        receipt = receipt_for()
        start = receipt.start_receipt
        result = receipt.result_event
        terminal = receipt.terminal_event
        cases = (
            {"result_event": replace(result, stream_id="session:other")},
            {"terminal_event": replace(terminal, stream_id="session:other")},
            {
                "result_event": replace(
                    result,
                    event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
                )
            },
            {
                "terminal_event": replace(
                    terminal,
                    event_type=TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
                )
            },
            {"terminal_event": replace(terminal, event_id=result.event_id)},
            {"result_event": replace(result, sequence=start.sequence)},
            {"result_event": replace(result, global_position=start.global_position)},
            {"terminal_event": replace(terminal, sequence=result.sequence + 2)},
            {"terminal_event": replace(terminal, global_position=result.global_position + 2)},
            {
                "terminal_event": replace(
                    terminal,
                    event_envelope_digest=result.event_envelope_digest,
                )
            },
        )
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    replace(receipt, **change)

        duplicate_result = replace(result, event_id=start.event_id)
        matching_transition = replace(
            receipt.terminal_transition,
            result_event_id=start.event_id,
        )
        with self.assertRaisesRegex(ValueError, "distinct"):
            replace(
                receipt,
                result_event=duplicate_result,
                terminal_transition=matching_transition,
            )

    def test_builder_binds_both_sequences_to_the_exact_request_version(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        result_event, terminal_event = coordinates_for(request)
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id=result_event.event_id,
        )
        with self.assertRaisesRegex(ValueError, "result event sequence"):
            build_scoped_invocation_result_receipt_v2(
                request,
                evidence,
                result_event=replace(result_event, sequence=result_event.sequence + 1),
                terminal_event=replace(
                    terminal_event,
                    sequence=terminal_event.sequence + 1,
                ),
                terminal_transition=transition,
            )
        with self.assertRaisesRegex(ValueError, "terminal event sequence"):
            build_scoped_invocation_result_receipt_v2(
                request,
                evidence,
                result_event=result_event,
                terminal_event=replace(
                    terminal_event,
                    sequence=terminal_event.sequence + 1,
                ),
                terminal_transition=transition,
            )

    def test_builder_uses_the_final_two_sqlite_event_coordinates_exactly(self) -> None:
        maximum = invocation_results_module._MAX_SQLITE_INTEGER
        request = request_for(expected_stream_version=maximum - 2)
        evidence = evidence_for(request)
        result_event, terminal_event = coordinates_for(
            request,
            result_global_position=maximum - 1,
        )
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id=result_event.event_id,
        )
        receipt = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=result_event,
            terminal_event=terminal_event,
            terminal_transition=transition,
        )

        self.assertEqual(receipt.result_event.sequence, maximum - 1)
        self.assertEqual(receipt.terminal_event.sequence, maximum)
        self.assertEqual(receipt.result_event.global_position, maximum - 1)
        self.assertEqual(receipt.terminal_event.global_position, maximum)
        with self.assertRaisesRegex(ValueError, "supported range"):
            replace(terminal_event, sequence=maximum + 1)
        with self.assertRaisesRegex(ValueError, "supported range"):
            replace(terminal_event, global_position=maximum + 1)

    def test_envelope_digests_remain_store_verified_coordinates_not_codec_authority(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        first_result, first_terminal = coordinates_for(request)
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id=first_result.event_id,
        )
        first = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=first_result,
            terminal_event=first_terminal,
            terminal_transition=transition,
        )
        second_result, second_terminal = coordinates_for(
            request,
            result_envelope_digest="3" * 64,
            terminal_envelope_digest="4" * 64,
        )
        second = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=second_result,
            terminal_event=second_terminal,
            terminal_transition=transition,
        )

        self.assertNotEqual(first.receipt_digest, second.receipt_digest)
        self.assertEqual(second.result_event.event_envelope_digest, "3" * 64)

    def test_builder_and_receipt_digest_ignore_instance_method_shadowing(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        result_event, terminal_event = coordinates_for(request)
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id=result_event.event_id,
        )
        baseline = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=result_event,
            terminal_event=terminal_event,
            terminal_transition=transition,
        )

        object.__setattr__(evidence, "to_dict", lambda: {"forged": True})
        object.__setattr__(evidence, "canonical_digest", lambda: "0" * 64)
        object.__setattr__(result_event, "to_dict", lambda: {"forged": True})
        object.__setattr__(terminal_event, "to_dict", lambda: {"forged": True})
        object.__setattr__(transition, "to_dict", lambda: {"forged": True})
        rebuilt = build_scoped_invocation_result_receipt_v2(
            request,
            evidence,
            result_event=result_event,
            terminal_event=terminal_event,
            terminal_transition=transition,
        )
        self.assertEqual(rebuilt.receipt_digest, baseline.receipt_digest)

        object.__setattr__(rebuilt, "to_dict", lambda: {"forged": True})
        object.__setattr__(rebuilt, "canonical_bytes", lambda: b"forged")
        self.assertEqual(
            ScopedInvocationResultReceiptV2.canonical_digest(rebuilt),
            baseline.receipt_digest,
        )

    def test_wrong_inputs_subclasses_and_public_exports_fail_closed(self) -> None:
        request = request_for()
        evidence = evidence_for(request)
        result_event, terminal_event = coordinates_for(request)
        transition = build_scoped_invocation_result_terminal_transition_v2(
            request,
            evidence,
            result_event_id=result_event.event_id,
        )
        for field_name, invalid in (
            ("request", object()),
            ("evidence", object()),
            ("result_event", object()),
            ("terminal_event", object()),
            ("terminal_transition", object()),
        ):
            arguments: dict[str, object] = {
                "request": request,
                "evidence": evidence,
                "result_event": result_event,
                "terminal_event": terminal_event,
                "terminal_transition": transition,
            }
            arguments[field_name] = invalid
            with self.subTest(field=field_name):
                with self.assertRaises(TypeError):
                    build_scoped_invocation_result_receipt_v2(
                        arguments.pop("request"),
                        arguments.pop("evidence"),
                        **arguments,
                    )

        class ReceiptSubclass(ScopedInvocationResultReceiptV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            ReceiptSubclass.from_dict(receipt_for().to_dict())
        for name in (
            "SCOPED_INVOCATION_RESULT_RECEIPT_DOMAIN",
            "SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION",
            "ScopedInvocationResultReceiptV2",
            "build_scoped_invocation_result_receipt_v2",
        ):
            with self.subTest(export=name):
                self.assertNotIn(name, invocation_results_module.__all__)
                self.assertNotIn(name, quantum_entanglement.__all__)
                self.assertFalse(hasattr(quantum_entanglement, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
