from __future__ import annotations

import hashlib
import json
import unittest

import quantum_entanglement
import quantum_entanglement.invocation_results as invocation_results_module
from quantum_entanglement.invocation_execution import EffectClass
from quantum_entanglement.invocation_results import (
    EMPTY_ACTION_RECEIPT_SET_DIGEST,
    SCOPED_INVOCATION_RESULT_EVIDENCE_DOMAIN,
    SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION,
    SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
    TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    ScopedInvocationResultEventCoordinatesV2,
    ScopedInvocationResultEvidenceV2,
)


def valid_coordinates(
    *,
    event_id: str = "event-result-1",
    event_type: str = TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
    sequence: int = 10,
    global_position: int = 100,
) -> ScopedInvocationResultEventCoordinatesV2:
    return ScopedInvocationResultEventCoordinatesV2(
        event_id=event_id,
        stream_id="session:session-result-1",
        event_type=event_type,
        sequence=sequence,
        global_position=global_position,
        event_envelope_digest="a" * 64,
    )


def valid_evidence(**changes: object) -> ScopedInvocationResultEvidenceV2:
    values: dict[str, object] = {
        "schema_version": SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": "attempt_bound",
        "receipt_id": "receipt-result-1",
        "tenant_id": "tenant-result-1",
        "workspace_id": "workspace-result-1",
        "invocation_id": "invocation-result-1",
        "session_id": "session-result-1",
        "plan_id": "plan-result-1",
        "task_id": "task-result-1",
        "agent_id": "agent-result-1",
        "job_idempotency_key": "invoke:task-result-1",
        "running_task_revision": 7,
        "terminal_task_revision": 8,
        "attempt_id": "attempt-result-1",
        "attempt_number": 1,
        "lease_epoch": 1,
        "worker_id": "worker-result-1",
        "lease_token_digest": "b" * 64,
        "start_receipt_digest": "c" * 64,
        "execution_manifest_digest": "d" * 64,
        "result_manifest_schema_version": SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
        "result_manifest_digest": "e" * 64,
        "result_ref": "result:invocation-result-1",
        "effect_class": EffectClass.PURE,
        "action_receipt_set_digest": EMPTY_ACTION_RECEIPT_SET_DIGEST,
        "acceptance_idempotency_key": "accept:invocation-result-1",
        "request_digest": "f" * 64,
        "accepted_at": "2026-08-27T10:11:12.123456Z",
        "artifact_count": 1,
    }
    values.update(changes)
    return ScopedInvocationResultEvidenceV2(**values)  # type: ignore[arg-type]


class ScopedInvocationResultEventCoordinateTests(unittest.TestCase):
    def test_event_coordinates_round_trip_without_authority(self) -> None:
        coordinates = valid_coordinates()
        decoded = ScopedInvocationResultEventCoordinatesV2.from_dict(coordinates.to_dict())
        self.assertEqual(decoded, coordinates)
        self.assertIsNot(decoded, coordinates)

    def test_event_coordinates_reject_shape_type_order_and_subclass_drift(self) -> None:
        wire = valid_coordinates().to_dict()
        invalid = (
            {**wire, "future": True},
            {key: value for key, value in wire.items() if key != "eventEnvelopeDigest"},
            dict(wire, sequence=True),
            dict(wire, sequence=0),
            dict(wire, globalPosition=9),
            dict(wire, eventEnvelopeDigest="A" * 64),
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationResultEventCoordinatesV2.from_dict(changed)

        class CoordinateSubclass(ScopedInvocationResultEventCoordinatesV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact"):
            CoordinateSubclass.from_dict(wire)


class ScopedInvocationResultEvidenceTests(unittest.TestCase):
    def test_evidence_round_trip_and_domain_digest_are_deterministic(self) -> None:
        evidence = valid_evidence()
        wire = evidence.to_dict()
        decoded = ScopedInvocationResultEvidenceV2.from_dict(wire)
        canonical = json.dumps(
            wire,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected = hashlib.sha256(
            SCOPED_INVOCATION_RESULT_EVIDENCE_DOMAIN.encode("utf-8") + canonical
        ).hexdigest()

        self.assertEqual(decoded, evidence)
        self.assertEqual(evidence.canonical_bytes(), canonical)
        self.assertEqual(evidence.canonical_digest(), expected)
        self.assertNotIn("leaseToken", repr(evidence))

    def test_every_evidence_wire_field_is_required_and_future_fields_fail(self) -> None:
        wire = valid_evidence().to_dict()
        for field_name in tuple(wire):
            with self.subTest(field_name=field_name):
                changed = dict(wire)
                del changed[field_name]
                with self.assertRaisesRegex(ValueError, "exact schema"):
                    ScopedInvocationResultEvidenceV2.from_dict(changed)
        with self.assertRaisesRegex(ValueError, "exact schema"):
            ScopedInvocationResultEvidenceV2.from_dict({**wire, "future": True})

    def test_evidence_rejects_schema_kind_revision_attempt_and_count_drift(self) -> None:
        invalid = (
            {"schema_version": 1},
            {"schema_version": True},
            {"evidence_kind": "legacy"},
            {"running_task_revision": True},
            {"terminal_task_revision": 7},
            {"terminal_task_revision": 9},
            {"attempt_number": 0},
            {"lease_epoch": True},
            {"result_manifest_schema_version": 3},
            {"artifact_count": -1},
            {"artifact_count": 257},
            {"artifact_count": True},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    valid_evidence(**change)

    def test_evidence_is_pure_only_and_requires_canonical_digests_and_time(self) -> None:
        invalid = (
            {"effect_class": EffectClass.IDEMPOTENT},
            {"action_receipt_set_digest": "a" * 64},
            {"lease_token_digest": "A" * 64},
            {"request_digest": "short"},
            {"accepted_at": "2026-08-27T10:11:12Z"},
            {"accepted_at": "2026-08-27T10:11:12.123456+00:00"},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    valid_evidence(**change)

    def test_result_reference_matches_scoped_identity_bounds(self) -> None:
        boundary = valid_evidence(result_ref="r" * 4_096)
        self.assertEqual(len(boundary.result_ref.encode("utf-8")), 4_096)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            valid_evidence(result_ref="r" * 4_097)

    def test_evidence_and_coordinates_remain_internal_until_receipts_exist(self) -> None:
        for name in (
            "ScopedInvocationResultEventCoordinatesV2",
            "ScopedInvocationResultEvidenceV2",
        ):
            self.assertNotIn(name, invocation_results_module.__all__)
            self.assertNotIn(name, quantum_entanglement.__all__)
            self.assertFalse(hasattr(quantum_entanglement, name))

        terminal = valid_coordinates(
            event_id="event-terminal-1",
            event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
            sequence=11,
            global_position=101,
        )
        self.assertEqual(terminal.event_type, TASK_STATUS_CHANGED_EVENT_TYPE)

        class EvidenceSubclass(ScopedInvocationResultEvidenceV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact"):
            EvidenceSubclass.from_dict(valid_evidence().to_dict())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
