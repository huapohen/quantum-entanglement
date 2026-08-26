import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, fields
from typing import Any

from quantum_entanglement.invocation_execution import (
    INVOCATION_EXECUTION_MANIFEST_DOMAIN,
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    TASK_INVOCATION_STARTED_EVENT_TYPE,
    EffectClass,
    InvocationExecutionManifest,
    InvocationStartEvidenceV2,
    RetryClass,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


class DictSubclass(dict[str, Any]):
    pass


class TextSubclass(str):
    pass


def valid_manifest_dict() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "invocationId": "inv-1",
        "sessionId": "session-1",
        "planId": "plan-1",
        "taskId": "task-1",
        "agentId": "agent-1",
        "jobIdempotencyKey": "invoke:task-1",
        "taskRevision": 2,
        "correlationId": "correlation-1",
        "causationId": "task-1",
        "envelopeDigest": SHA_A,
        "contextDigest": SHA_B,
        "authorizationDigest": SHA_C,
        "runtimeRevision": "runtime:sha256:" + SHA_D,
        "effectClass": "pure",
        "retryClass": "never",
    }


def valid_manifest() -> InvocationExecutionManifest:
    return InvocationExecutionManifest.from_dict(valid_manifest_dict())


def valid_start_dict() -> dict[str, Any]:
    manifest = valid_manifest()
    return {
        "schemaVersion": 2,
        "invocationId": manifest.invocation_id,
        "sessionId": manifest.session_id,
        "planId": manifest.plan_id,
        "taskId": manifest.task_id,
        "agentId": manifest.agent_id,
        "jobIdempotencyKey": manifest.job_idempotency_key,
        "attemptId": "attempt-1",
        "attemptNumber": 1,
        "leaseEpoch": 1,
        "workerId": "worker-1",
        "leaseTokenDigest": SHA_E,
        "claimedAt": "2026-08-27T01:02:03.000004Z",
        "leaseExpiresAt": "2026-08-27T01:03:03.000004Z",
        "manifestDigest": manifest.canonical_digest(),
        "envelopeDigest": manifest.envelope_digest,
        "contextDigest": manifest.context_digest,
        "authorizationDigest": manifest.authorization_digest,
        "runtimeRevision": manifest.runtime_revision,
        "correlationId": manifest.correlation_id,
        "causationId": manifest.causation_id,
    }


def exception_chain_text(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    parts: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.extend((type(current).__name__, str(current), repr(current), repr(current.args)))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(parts)


class InvocationExecutionManifestTests(unittest.TestCase):
    def test_exact_round_trip_enums_and_immutability(self) -> None:
        raw = valid_manifest_dict()
        manifest = InvocationExecutionManifest.from_dict(raw)

        self.assertEqual(manifest.to_dict(), raw)
        self.assertIs(manifest.effect_class, EffectClass.PURE)
        self.assertIs(manifest.retry_class, RetryClass.NEVER)
        raw["invocationId"] = "mutated"
        self.assertEqual(manifest.invocation_id, "inv-1")
        with self.assertRaises(FrozenInstanceError):
            manifest.invocation_id = "mutated"  # type: ignore[misc]

    def test_digest_uses_exact_domain_separator_and_canonical_json(self) -> None:
        manifest = valid_manifest()
        expected_json = json.dumps(
            manifest.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected = hashlib.sha256(
            INVOCATION_EXECUTION_MANIFEST_DOMAIN.encode("utf-8") + expected_json
        ).hexdigest()

        self.assertEqual(manifest.canonical_bytes(), expected_json)
        self.assertEqual(manifest.canonical_digest(), expected)
        self.assertEqual(
            manifest.canonical_digest(),
            "90d9542bcfbf430efdcdca1370651fec90fce311cdbb2dde47c10ac2ddb2162e",
        )
        self.assertNotEqual(manifest.canonical_digest(), hashlib.sha256(expected_json).hexdigest())

    def test_every_manifest_field_is_digest_bound(self) -> None:
        baseline = valid_manifest()
        replacements: dict[str, Any] = {
            "invocationId": "inv-2",
            "sessionId": "session-2",
            "planId": "plan-2",
            "taskId": "task-2",
            "agentId": "agent-2",
            "jobIdempotencyKey": "invoke:task-2",
            "taskRevision": 3,
            "correlationId": "correlation-2",
            "causationId": "task-2",
            "envelopeDigest": "1" * 64,
            "contextDigest": "2" * 64,
            "authorizationDigest": "3" * 64,
            "runtimeRevision": "runtime:revision-2",
            "effectClass": "idempotent",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                raw = valid_manifest_dict()
                raw[field] = replacement
                if field == "taskId":
                    raw["causationId"] = replacement
                elif field == "causationId":
                    raw["taskId"] = replacement
                changed = InvocationExecutionManifest.from_dict(raw)
                self.assertNotEqual(changed.canonical_digest(), baseline.canonical_digest())

    def test_effect_classes_are_exact_and_retry_is_fail_closed(self) -> None:
        self.assertEqual(
            {item.value for item in EffectClass},
            {"pure", "idempotent", "receipt_reconciled", "non_retriable"},
        )
        self.assertEqual(tuple(item.value for item in RetryClass), ("never",))
        for effect_class in EffectClass:
            raw = valid_manifest_dict()
            raw["effectClass"] = effect_class.value
            self.assertIs(
                InvocationExecutionManifest.from_dict(raw).effect_class,
                effect_class,
            )
        for field, value in (
            ("effectClass", "unknown"),
            ("effectClass", TextSubclass("pure")),
            ("retryClass", "automatic"),
            ("retryClass", TextSubclass("never")),
        ):
            with self.subTest(field=field, value=value):
                raw = valid_manifest_dict()
                raw[field] = value
                with self.assertRaises((TypeError, ValueError)):
                    InvocationExecutionManifest.from_dict(raw)

    def test_exact_fields_container_and_schema_version(self) -> None:
        missing = valid_manifest_dict()
        del missing["agentId"]
        extra = valid_manifest_dict()
        extra["leaseToken"] = "must-never-be-accepted"
        for value in (missing, extra):
            with self.subTest(keys=tuple(value)):
                with self.assertRaisesRegex(ValueError, "exact schema"):
                    InvocationExecutionManifest.from_dict(value)
        with self.assertRaisesRegex(TypeError, "plain dictionary"):
            InvocationExecutionManifest.from_dict(DictSubclass(valid_manifest_dict()))
        for version in (0, 2, 99):
            raw = valid_manifest_dict()
            raw["schemaVersion"] = version
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    InvocationExecutionManifest.from_dict(raw)
        raw = valid_manifest_dict()
        raw["schemaVersion"] = True
        with self.assertRaisesRegex(TypeError, "exact integer"):
            InvocationExecutionManifest.from_dict(raw)

    def test_integer_fields_reject_booleans_and_invalid_ranges(self) -> None:
        for field in ("schemaVersion", "taskRevision"):
            raw = valid_manifest_dict()
            raw[field] = True
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    InvocationExecutionManifest.from_dict(raw)
        for revision in (0, -1, 1 << 63):
            raw = valid_manifest_dict()
            raw["taskRevision"] = revision
            with self.subTest(revision=revision):
                with self.assertRaises(ValueError):
                    InvocationExecutionManifest.from_dict(raw)

    def test_digest_text_and_causal_invariants_are_strict(self) -> None:
        cases: tuple[tuple[str, Any], ...] = (
            ("envelopeDigest", "A" * 64),
            ("contextDigest", "a" * 63),
            ("authorizationDigest", "g" * 64),
            ("runtimeRevision", " runtime-1"),
            ("runtimeRevision", "runtime\n1"),
            ("runtimeRevision", "cafe\u0301"),
            ("runtimeRevision", "x" * 4_097),
            ("invocationId", TextSubclass("inv-1")),
            ("causationId", "another-task"),
        )
        for field, value in cases:
            raw = valid_manifest_dict()
            raw[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises((TypeError, ValueError)):
                    InvocationExecutionManifest.from_dict(raw)

    def test_event_decoder_rejects_legacy_and_near_miss_names(self) -> None:
        decoded = InvocationExecutionManifest.from_event_payload(
            TASK_EXECUTION_REQUESTED_EVENT_TYPE,
            valid_manifest_dict(),
        )
        self.assertEqual(decoded, valid_manifest())
        for event_type in (
            "task.execution_requested",
            "task.status_changed",
            "task.status.changed",
            TextSubclass(TASK_EXECUTION_REQUESTED_EVENT_TYPE),
        ):
            with self.subTest(event_type=event_type):
                with self.assertRaises((TypeError, ValueError)):
                    InvocationExecutionManifest.from_event_payload(
                        event_type,
                        valid_manifest_dict(),
                    )

    def test_low_level_forgery_is_revalidated_before_serialization_or_digest(self) -> None:
        manifest = valid_manifest()
        object.__setattr__(manifest, "task_revision", True)
        with self.assertRaises(TypeError):
            manifest.to_dict()
        with self.assertRaises(TypeError):
            manifest.canonical_digest()


class InvocationStartEvidenceTests(unittest.TestCase):
    def test_exact_round_trip_is_attempt_bound(self) -> None:
        raw = valid_start_dict()
        evidence = InvocationStartEvidenceV2.from_dict(raw)

        self.assertEqual(evidence.to_dict(), raw)
        self.assertEqual(evidence.attempt_number, 1)
        self.assertEqual(evidence.lease_epoch, 1)
        raw["attemptId"] = "mutated"
        self.assertEqual(evidence.attempt_id, "attempt-1")

    def test_exact_fields_future_legacy_and_bool_as_int_fail_closed(self) -> None:
        missing = valid_start_dict()
        del missing["manifestDigest"]
        extra = valid_start_dict()
        extra["leaseToken"] = "raw-secret"
        for value in (missing, extra):
            with self.subTest(keys=tuple(value)):
                with self.assertRaisesRegex(ValueError, "exact schema"):
                    InvocationStartEvidenceV2.from_dict(value)
        with self.assertRaisesRegex(TypeError, "plain dictionary"):
            InvocationStartEvidenceV2.from_dict(DictSubclass(valid_start_dict()))
        for version in (1, 3, 99, True):
            raw = valid_start_dict()
            raw["schemaVersion"] = version
            with self.subTest(version=version):
                with self.assertRaises((TypeError, ValueError)):
                    InvocationStartEvidenceV2.from_dict(raw)
        for field in ("attemptNumber", "leaseEpoch"):
            raw = valid_start_dict()
            raw[field] = True
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    InvocationStartEvidenceV2.from_dict(raw)

    def test_attempt_fence_and_time_shapes_are_strict(self) -> None:
        cases: tuple[tuple[str, Any], ...] = (
            ("attemptNumber", 0),
            ("leaseEpoch", -1),
            ("leaseTokenDigest", "E" * 64),
            ("leaseTokenDigest", "e" * 63),
            ("claimedAt", "2026-08-27T01:02:03Z"),
            ("claimedAt", "2026-08-27T01:02:03.000004+00:00"),
            ("claimedAt", "2026-02-30T01:02:03.000004Z"),
            ("leaseExpiresAt", "2026-08-27T01:02:03.000004Z"),
            ("leaseExpiresAt", "2026-08-27T01:02:02.000004Z"),
        )
        for field, value in cases:
            raw = valid_start_dict()
            raw[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises((TypeError, ValueError)):
                    InvocationStartEvidenceV2.from_dict(raw)

    def test_all_digests_causation_and_text_are_strict(self) -> None:
        for field in (
            "leaseTokenDigest",
            "manifestDigest",
            "envelopeDigest",
            "contextDigest",
            "authorizationDigest",
        ):
            raw = valid_start_dict()
            raw[field] = "F" * 64
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    InvocationStartEvidenceV2.from_dict(raw)
        for field, value in (
            ("workerId", "worker\x00secret"),
            ("runtimeRevision", "cafe\u0301"),
            ("causationId", "another-task"),
            ("attemptId", TextSubclass("attempt-1")),
        ):
            raw = valid_start_dict()
            raw[field] = value
            with self.subTest(field=field):
                with self.assertRaises((TypeError, ValueError)):
                    InvocationStartEvidenceV2.from_dict(raw)

    def test_raw_lease_token_is_neither_a_field_wire_value_nor_repr_value(self) -> None:
        raw_token = "lease-token-secret-canary"
        raw = valid_start_dict()
        raw["leaseTokenDigest"] = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        evidence = InvocationStartEvidenceV2.from_dict(raw)

        self.assertNotIn("lease_token", {field.name for field in fields(evidence)})
        self.assertNotIn("leaseToken", evidence.to_dict())
        self.assertNotIn(raw_token, repr(evidence))
        self.assertNotIn(raw_token, json.dumps(evidence.to_dict(), sort_keys=True))

        hostile = copy.deepcopy(raw)
        hostile["leaseToken"] = raw_token
        with self.assertRaisesRegex(ValueError, "exact schema"):
            InvocationStartEvidenceV2.from_dict(hostile)

    def test_event_decoder_rejects_legacy_event_names_and_v1_payloads(self) -> None:
        decoded = InvocationStartEvidenceV2.from_event_payload(
            TASK_INVOCATION_STARTED_EVENT_TYPE,
            valid_start_dict(),
        )
        self.assertEqual(decoded, InvocationStartEvidenceV2.from_dict(valid_start_dict()))
        for event_type in (
            "task.invocation_started",
            "task.invocation.started.v1",
            TextSubclass(TASK_INVOCATION_STARTED_EVENT_TYPE),
        ):
            with self.subTest(event_type=event_type):
                with self.assertRaises((TypeError, ValueError)):
                    InvocationStartEvidenceV2.from_event_payload(event_type, valid_start_dict())

        legacy_payload = {
            "taskId": "task-1",
            "agentId": "agent-1",
            "envelope": {},
            "contextDigest": SHA_B,
        }
        with self.assertRaisesRegex(ValueError, "exact schema"):
            InvocationStartEvidenceV2.from_event_payload(
                TASK_INVOCATION_STARTED_EVENT_TYPE,
                legacy_payload,
            )

    def test_validation_failures_do_not_retain_secret_canaries_in_exception_chain(self) -> None:
        enum_canary = "secret-enum-canary"
        timestamp_canary = "9999-99-99T99:99:99.999999Z"
        surrogate_canary = "secret-surrogate-canary-\ud800"
        raw_lease_canary = "secret-raw-lease-canary"

        manifest_raw = valid_manifest_dict()
        manifest_raw["effectClass"] = enum_canary
        start_timestamp = valid_start_dict()
        start_timestamp["claimedAt"] = timestamp_canary
        start_surrogate = valid_start_dict()
        start_surrogate["workerId"] = surrogate_canary
        start_raw_lease = valid_start_dict()
        start_raw_lease["leaseToken"] = raw_lease_canary
        cases = (
            (enum_canary, lambda: InvocationExecutionManifest.from_dict(manifest_raw)),
            (timestamp_canary, lambda: InvocationStartEvidenceV2.from_dict(start_timestamp)),
            (
                "secret-surrogate-canary",
                lambda: InvocationStartEvidenceV2.from_dict(start_surrogate),
            ),
            (raw_lease_canary, lambda: InvocationStartEvidenceV2.from_dict(start_raw_lease)),
        )
        for canary, operation in cases:
            with self.subTest(canary=canary):
                with self.assertRaises((TypeError, ValueError)) as caught:
                    operation()
                self.assertNotIn(canary, exception_chain_text(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_low_level_start_forgery_is_revalidated_before_serialization(self) -> None:
        evidence = InvocationStartEvidenceV2.from_dict(valid_start_dict())
        object.__setattr__(evidence, "lease_epoch", True)
        with self.assertRaises(TypeError):
            evidence.to_dict()


if __name__ == "__main__":
    unittest.main()
