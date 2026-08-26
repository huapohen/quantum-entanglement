import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from typing import Any, cast

from quantum_entanglement.attempts import InvocationLease
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    INVOCATION_EXECUTION_MANIFEST_DOMAIN,
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    TASK_INVOCATION_STARTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    EffectClass,
    InvocationExecutionManifest,
    InvocationStartClaimed,
    InvocationStartEvidenceV2,
    InvocationStartObserved,
    InvocationStartReceipt,
    RetryClass,
    TaskInvocationAdmissionRequest,
    build_task_invocation_admission_request,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
LEASE_TOKEN = "lease-token-secret-canary"
EVENT_TIME_A = "2026-08-27T01:02:03.000004Z"
EVENT_TIME_B = "2026-08-27T01:02:03.000005Z"


class DictSubclass(dict[str, Any]):
    pass


class TextSubclass(str):
    pass


class IntegerSubclass(int):
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


def valid_admission_request() -> TaskInvocationAdmissionRequest:
    manifest = valid_manifest()
    return build_task_invocation_admission_request(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=None,
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-execution-requested",
        execution_requested_timestamp=EVENT_TIME_A,
        task_running_event_id="event-task-running",
        task_running_timestamp=EVENT_TIME_B,
        job_priority=73,
    )


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
        "leaseTokenDigest": hashlib.sha256(LEASE_TOKEN.encode("utf-8")).hexdigest(),
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


def valid_start_receipt() -> InvocationStartReceipt:
    evidence = InvocationStartEvidenceV2.from_dict(valid_start_dict())
    return InvocationStartReceipt(
        event_id="event-invocation-started",
        stream_id="session:" + evidence.session_id,
        sequence=3,
        global_position=11,
        evidence=evidence,
    )


def valid_invocation_lease() -> InvocationLease:
    evidence = InvocationStartEvidenceV2.from_dict(valid_start_dict())
    return InvocationLease(
        invocation_id=evidence.invocation_id,
        session_id=evidence.session_id,
        plan_id=evidence.plan_id,
        task_id=evidence.task_id,
        agent_id=evidence.agent_id,
        idempotency_key=evidence.job_idempotency_key,
        payload_digest=evidence.manifest_digest,
        attempt_id=evidence.attempt_id,
        attempt_number=evidence.attempt_number,
        max_attempts=1,
        lease_epoch=evidence.lease_epoch,
        worker_id=evidence.worker_id,
        lease_token=LEASE_TOKEN,
        claimed_at=evidence.claimed_at,
        lease_expires_at=evidence.lease_expires_at,
    )


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


class TaskInvocationAdmissionRequestTests(unittest.TestCase):
    def test_builder_constructs_exact_ordered_events_and_manifest_bound_job(self) -> None:
        request = valid_admission_request()
        events, job = request.components()
        manifest = request.manifest

        self.assertEqual(
            [event.event_type for event in events],
            [TASK_EXECUTION_REQUESTED_EVENT_TYPE, TASK_STATUS_CHANGED_EVENT_TYPE],
        )
        self.assertEqual(events[0].payload, manifest.to_dict())
        self.assertEqual(
            events[1].payload,
            {
                "taskId": manifest.task_id,
                "previous": "ready",
                "current": "running",
                "reason": None,
                "revision": manifest.task_revision,
            },
        )
        self.assertEqual(events[0].payload["schemaVersion"], 1)
        self.assertEqual(request.stream_id, "session:" + manifest.session_id)
        self.assertEqual(
            (
                job.invocation_id,
                job.session_id,
                job.plan_id,
                job.task_id,
                job.agent_id,
                job.idempotency_key,
            ),
            (
                manifest.invocation_id,
                manifest.session_id,
                manifest.plan_id,
                manifest.task_id,
                manifest.agent_id,
                manifest.job_idempotency_key,
            ),
        )
        self.assertEqual(job.payload_digest, manifest.canonical_digest())
        self.assertEqual(job.priority, 73)
        self.assertEqual(job.max_attempts, 1)
        self.assertIsNone(job.available_at)
        request.validate_components(events, job)

    def test_event_envelopes_bind_actor_causality_revision_and_idempotency(self) -> None:
        request = valid_admission_request()
        manifest = request.manifest
        requested, running = request.events

        for event in (requested, running):
            self.assertEqual(event.stream_id, "session:" + manifest.session_id)
            self.assertEqual(event.actor_id, CANONICAL_ORCHESTRATOR_ACTOR_ID)
            self.assertEqual(event.correlation_id, manifest.correlation_id)
            self.assertEqual(event.causation_id, manifest.task_id)
        self.assertEqual(
            requested.idempotency_key,
            "execution-request:" + manifest.invocation_id,
        )
        self.assertEqual(
            running.idempotency_key,
            f"task-running:{manifest.task_id}:{manifest.task_revision}",
        )
        decoded = InvocationExecutionManifest.from_event_payload(
            requested.event_type,
            requested.payload,
        )
        self.assertEqual(decoded.canonical_digest(), request.job_spec.payload_digest)
        self.assertEqual(running.payload["revision"], decoded.task_revision)

    def test_from_components_round_trips_exact_durable_values_without_factories(self) -> None:
        expected = valid_admission_request()
        events, job = expected.components()

        decoded = TaskInvocationAdmissionRequest.from_components(events, job)

        self.assertEqual(decoded, expected)
        self.assertEqual(decoded.components(), expected.components())
        self.assertIsNot(decoded.manifest, expected.manifest)
        self.assertIsNot(decoded.transition, expected.transition)

        available = replace(expected, job_available_at="2026-08-27T01:04:03.000004Z")
        available_events, available_job = available.components()
        self.assertEqual(
            TaskInvocationAdmissionRequest.from_components(
                available_events,
                available_job,
            ),
            available,
        )

    def test_from_components_rejects_nonexact_or_noncanonical_envelopes(self) -> None:
        request = valid_admission_request()
        events, job = request.components()

        malformed_batches: tuple[object, ...] = (
            list(events),
            events[:1],
            events + (events[1],),
            (events[1], events[0]),
        )
        for batch in malformed_batches:
            with self.subTest(batch_type=type(batch).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    TaskInvocationAdmissionRequest.from_components(batch, job)

        replacements: tuple[tuple[int, str, Any], ...] = (
            (0, "stream_id", "session:other"),
            (0, "event_type", "task.execution_requested"),
            (0, "actor_id", "legacy-orchestrator"),
            (0, "event_id", TextSubclass(events[0].event_id)),
            (0, "timestamp", "2026-08-27T01:02:03Z"),
            (0, "correlation_id", "correlation-other"),
            (0, "causation_id", "task-other"),
            (0, "idempotency_key", "execution-request:other"),
            (1, "stream_id", "session:other"),
            (1, "event_type", "task.status_changed"),
            (1, "actor_id", "legacy-orchestrator"),
            (1, "correlation_id", "correlation-other"),
            (1, "causation_id", "task-other"),
            (1, "idempotency_key", "task-running:other:2"),
        )
        for index, field_name, value in replacements:
            with self.subTest(index=index, field_name=field_name):
                changed = list(events)
                changed[index] = replace(events[index], **{field_name: value})
                with self.assertRaises((TypeError, ValueError)):
                    TaskInvocationAdmissionRequest.from_components(tuple(changed), job)

        future_manifest = dict(events[0].payload)
        future_manifest["schemaVersion"] = 2
        with self.assertRaises(ValueError):
            TaskInvocationAdmissionRequest.from_components(
                (replace(events[0], payload=future_manifest), events[1]),
                job,
            )

    def test_from_components_strictly_snapshots_job_integers_and_bindings(self) -> None:
        request = valid_admission_request()
        events, job = request.components()
        replacements: dict[str, Any] = {
            "invocation_id": "inv-other",
            "session_id": "session-other",
            "plan_id": "plan-other",
            "task_id": "task-other",
            "agent_id": "agent-other",
            "idempotency_key": "invoke:other",
            "payload_digest": "f" * 64,
            "max_attempts": 2,
            "available_at": "2026-08-27T01:02:03Z",
        }
        for field_name, value in replacements.items():
            with self.subTest(field_name=field_name):
                changed = replace(job, **{field_name: value})
                with self.assertRaises((TypeError, ValueError)):
                    TaskInvocationAdmissionRequest.from_components(events, changed)

        changed_priority = replace(job, priority=72)
        decoded_priority = TaskInvocationAdmissionRequest.from_components(
            events,
            changed_priority,
        )
        self.assertEqual(decoded_priority.job_priority, 72)
        self.assertEqual(decoded_priority.job_spec, changed_priority)

        for field_name in ("priority", "max_attempts"):
            for value in (True, IntegerSubclass(1)):
                with self.subTest(integer_field=field_name, value_type=type(value).__name__):
                    forged = replace(job)
                    object.__setattr__(forged, field_name, value)
                    with self.assertRaises(TypeError):
                        TaskInvocationAdmissionRequest.from_components(events, forged)

    def test_from_components_rejects_duck_types_without_invoking_their_callbacks(self) -> None:
        request = valid_admission_request()
        events, job = request.components()

        class HostileDuck:
            @property
            def payload(self) -> object:
                raise AssertionError("caller callback must not execute")

            def __iter__(self) -> object:
                raise AssertionError("caller callback must not execute")

        for candidate_events, candidate_job in (
            (HostileDuck(), job),
            ((HostileDuck(), events[1]), job),
            (events, HostileDuck()),
        ):
            with self.subTest(candidate_type=type(candidate_events).__name__):
                with self.assertRaises(TypeError):
                    TaskInvocationAdmissionRequest.from_components(
                        candidate_events,
                        candidate_job,
                    )

    def test_caller_event_identity_and_time_are_stable_and_payloads_are_fresh(self) -> None:
        request = valid_admission_request()
        first_events, first_job = request.components()
        payload = cast(dict[str, Any], first_events[0].payload)
        payload["invocationId"] = "mutated-return-value"

        second_events, second_job = request.components()
        self.assertEqual(
            [(item.event_id, item.timestamp) for item in first_events],
            [
                ("event-execution-requested", EVENT_TIME_A),
                ("event-task-running", EVENT_TIME_B),
            ],
        )
        self.assertEqual(
            [(item.event_id, item.timestamp) for item in second_events],
            [
                ("event-execution-requested", EVENT_TIME_A),
                ("event-task-running", EVENT_TIME_B),
            ],
        )
        self.assertEqual(second_events[0].payload, request.manifest.to_dict())
        self.assertEqual(first_job, second_job)
        self.assertIsNot(first_job, second_job)
        self.assertIsNot(first_events[0], second_events[0])
        self.assertIsNot(first_events[0].payload, second_events[0].payload)

    def test_event_timestamps_allow_equal_or_ascending_but_reject_descending(self) -> None:
        ascending = valid_admission_request()
        ascending.validate_components(ascending.events, ascending.job_spec)

        equal = replace(ascending, task_running_timestamp=EVENT_TIME_A)
        equal.validate_components(equal.events, equal.job_spec)
        self.assertEqual(equal.events[0].timestamp, equal.events[1].timestamp)

        with self.assertRaisesRegex(ValueError, "must not precede"):
            replace(
                ascending,
                execution_requested_timestamp=EVENT_TIME_B,
                task_running_timestamp=EVENT_TIME_A,
            )

    def test_request_rejects_noncanonical_transition_and_event_identity(self) -> None:
        manifest = valid_manifest()
        valid_transition = TaskTransition(
            manifest.task_id,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            None,
            manifest.task_revision,
        )

        invalid_transitions = (
            replace(valid_transition, previous=TaskStatus.PENDING),
            replace(valid_transition, current=TaskStatus.COMPLETED),
            replace(valid_transition, task_id="another-task"),
            replace(valid_transition, revision=manifest.task_revision + 1),
            replace(valid_transition, revision=True),
        )
        for transition in invalid_transitions:
            with self.subTest(transition=transition):
                with self.assertRaises((TypeError, ValueError)):
                    build_task_invocation_admission_request(
                        manifest,
                        transition,
                        execution_requested_event_id="event-requested",
                        execution_requested_timestamp=EVENT_TIME_A,
                        task_running_event_id="event-running",
                        task_running_timestamp=EVENT_TIME_B,
                    )

        for requested_id, requested_at, running_id in (
            ("same-event", EVENT_TIME_A, "same-event"),
            ("event-requested", "2026-08-27T01:02:03Z", "event-running"),
        ):
            with self.subTest(requested_id=requested_id, requested_at=requested_at):
                with self.assertRaises(ValueError):
                    build_task_invocation_admission_request(
                        manifest,
                        valid_transition,
                        execution_requested_event_id=requested_id,
                        execution_requested_timestamp=requested_at,
                        task_running_event_id=running_id,
                        task_running_timestamp=EVENT_TIME_B,
                    )

    def test_component_validation_rejects_legacy_reordered_and_extra_events(self) -> None:
        request = valid_admission_request()
        events = request.events
        job = request.job_spec
        extra_event = DomainEvent(
            stream_id=request.stream_id,
            event_type="task.audit.observed",
            payload={},
            actor_id="orchestrator",
            event_id="event-extra",
            timestamp=EVENT_TIME_B,
        )
        invalid_batches: tuple[object, ...] = (
            list(events),
            events[:1],
            events + (extra_event,),
            (events[1], events[0]),
            (replace(events[0], event_type="task.execution_requested"), events[1]),
            (events[0], replace(events[1], event_type="task.status_changed")),
            (
                events[0],
                replace(events[1], payload={**events[1].payload, "schemaVersion": 1}),
            ),
        )
        for batch in invalid_batches:
            with self.subTest(batch=batch):
                with self.assertRaises((TypeError, ValueError)):
                    request.validate_components(batch, job)

    def test_component_validation_rejects_every_job_manifest_binding_mismatch(self) -> None:
        request = valid_admission_request()
        events = request.events
        job = request.job_spec
        replacements: dict[str, Any] = {
            "invocation_id": "invocation-other",
            "session_id": "session-other",
            "plan_id": "plan-other",
            "task_id": "task-other",
            "agent_id": "agent-other",
            "idempotency_key": "invoke:other",
            "payload_digest": "f" * 64,
            "priority": job.priority - 1,
            "max_attempts": 2,
            "available_at": EVENT_TIME_A,
        }
        for field_name, value in replacements.items():
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    request.validate_components(events, replace(job, **{field_name: value}))

    def test_component_validation_rejects_every_event_envelope_binding_mismatch(self) -> None:
        request = valid_admission_request()
        events = request.events
        replacements: tuple[tuple[int, str, Any], ...] = (
            (0, "stream_id", "session:other"),
            (0, "actor_id", "other-actor"),
            (0, "event_id", "other-event"),
            (0, "timestamp", EVENT_TIME_B),
            (0, "correlation_id", "other-correlation"),
            (0, "causation_id", "other-task"),
            (0, "idempotency_key", "execution-request:other"),
            (1, "stream_id", "session:other"),
            (1, "actor_id", "other-actor"),
            (1, "event_id", "other-event"),
            (1, "timestamp", EVENT_TIME_A),
            (1, "correlation_id", "other-correlation"),
            (1, "causation_id", "other-task"),
            (1, "idempotency_key", "task-running:other:1"),
        )
        for index, field_name, value in replacements:
            with self.subTest(index=index, field_name=field_name):
                changed = list(events)
                changed[index] = replace(events[index], **{field_name: value})
                with self.assertRaises(ValueError):
                    request.validate_components(tuple(changed), request.job_spec)

    def test_component_validation_rejects_manifest_and_revision_tampering(self) -> None:
        request = valid_admission_request()
        events = request.events
        payload_replacements: dict[str, Any] = {
            "invocationId": "invocation-other",
            "sessionId": "session-other",
            "planId": "plan-other",
            "taskId": "task-other",
            "agentId": "agent-other",
            "jobIdempotencyKey": "invoke:other",
            "taskRevision": request.manifest.task_revision + 1,
            "correlationId": "correlation-other",
            "envelopeDigest": "1" * 64,
            "contextDigest": "2" * 64,
            "authorizationDigest": "3" * 64,
            "runtimeRevision": "runtime-other",
            "effectClass": "idempotent",
        }
        for field_name, value in payload_replacements.items():
            with self.subTest(field_name=field_name):
                payload = dict(events[0].payload)
                payload[field_name] = value
                if field_name == "taskId":
                    payload["causationId"] = value
                changed = (replace(events[0], payload=payload), events[1])
                with self.assertRaises(ValueError):
                    request.validate_components(changed, request.job_spec)

        transition_payload = dict(events[1].payload)
        transition_payload["revision"] = request.manifest.task_revision + 1
        with self.assertRaises(ValueError):
            request.validate_components(
                (events[0], replace(events[1], payload=transition_payload)),
                request.job_spec,
            )


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


class InvocationStartResultTests(unittest.TestCase):
    def test_receipt_and_observed_are_capability_free_exact_round_trips(self) -> None:
        receipt = valid_start_receipt()
        expected = {
            "eventId": "event-invocation-started",
            "streamId": "session:session-1",
            "sequence": 3,
            "globalPosition": 11,
            "evidence": valid_start_dict(),
        }

        self.assertEqual(receipt.to_dict(), expected)
        self.assertEqual(InvocationStartReceipt.from_dict(expected), receipt)
        observed = InvocationStartObserved(receipt)
        self.assertEqual(observed.to_dict(), {"receipt": expected})
        self.assertEqual(InvocationStartObserved.from_dict(observed.to_dict()), observed)
        self.assertIsNot(observed.receipt, receipt)
        self.assertIsNot(observed.receipt.evidence, receipt.evidence)
        self.assertNotIn(LEASE_TOKEN, json.dumps(observed.to_dict(), sort_keys=True))

    def test_receipt_strictly_validates_type_text_positions_and_stream_binding(self) -> None:
        raw = valid_start_receipt().to_dict()
        missing = copy.deepcopy(raw)
        del missing["sequence"]
        extra = copy.deepcopy(raw)
        extra["leaseToken"] = "secret-extra-lease-canary"
        for candidate in (missing, extra):
            with self.subTest(keys=tuple(candidate)):
                with self.assertRaisesRegex(ValueError, "exact schema"):
                    InvocationStartReceipt.from_dict(candidate)
        with self.assertRaisesRegex(TypeError, "plain dictionary"):
            InvocationStartReceipt.from_dict(DictSubclass(raw))

        for field_name, value in (
            ("eventId", TextSubclass("event-invocation-started")),
            ("streamId", "session:other"),
            ("sequence", True),
            ("sequence", IntegerSubclass(3)),
            ("sequence", 0),
            ("sequence", 1 << 63),
            ("globalPosition", True),
            ("globalPosition", IntegerSubclass(11)),
            ("globalPosition", 2),
        ):
            with self.subTest(field_name=field_name, value=value):
                changed = copy.deepcopy(raw)
                changed[field_name] = value
                with self.assertRaises((TypeError, ValueError)):
                    InvocationStartReceipt.from_dict(changed)

    def test_result_types_are_exact_immutable_and_revalidate_low_level_forgery(self) -> None:
        class ReceiptSubclass(InvocationStartReceipt):
            pass

        class ObservedSubclass(InvocationStartObserved):
            pass

        class ClaimedSubclass(InvocationStartClaimed):
            pass

        receipt = valid_start_receipt()
        lease = valid_invocation_lease()
        for operation in (
            lambda: ReceiptSubclass(
                receipt.event_id,
                receipt.stream_id,
                receipt.sequence,
                receipt.global_position,
                receipt.evidence,
            ),
            lambda: ObservedSubclass(receipt),
            lambda: ClaimedSubclass(receipt, lease),
        ):
            with self.assertRaises(TypeError):
                operation()

        observed = InvocationStartObserved(receipt)
        with self.assertRaises(FrozenInstanceError):
            observed.receipt = receipt  # type: ignore[misc]

        forged_receipt = valid_start_receipt()
        object.__setattr__(forged_receipt, "sequence", True)
        with self.assertRaises(TypeError):
            forged_receipt.to_dict()
        with self.assertRaises(TypeError):
            InvocationStartObserved(forged_receipt)

    def test_claimed_snapshots_exact_lease_and_never_serializes_raw_authority(self) -> None:
        receipt = valid_start_receipt()
        lease = valid_invocation_lease()

        claimed = InvocationStartClaimed(receipt, lease)

        self.assertEqual(claimed.receipt, receipt)
        self.assertEqual(claimed.lease, lease)
        self.assertIsNot(claimed.receipt, receipt)
        self.assertIsNot(claimed.lease, lease)
        self.assertFalse(hasattr(claimed, "to_dict"))
        self.assertNotIn(LEASE_TOKEN, repr(claimed))
        self.assertNotIn(LEASE_TOKEN, str(claimed))
        self.assertNotIn(LEASE_TOKEN, repr(claimed.lease))
        with self.assertRaises(TypeError) as caught:
            json.dumps(claimed)
        self.assertNotIn(LEASE_TOKEN, exception_chain_text(caught.exception))
        with self.assertRaises(FrozenInstanceError):
            claimed.lease = lease  # type: ignore[misc]

    def test_claimed_rejects_every_lease_evidence_binding_mismatch(self) -> None:
        receipt = valid_start_receipt()
        lease = valid_invocation_lease()
        replacements: dict[str, Any] = {
            "invocation_id": "inv-other",
            "session_id": "session-other",
            "plan_id": "plan-other",
            "task_id": "task-other",
            "agent_id": "agent-other",
            "idempotency_key": "invoke:other",
            "payload_digest": "f" * 64,
            "attempt_id": "attempt-other",
            "attempt_number": 2,
            "max_attempts": 2,
            "lease_epoch": 2,
            "worker_id": "worker-other",
            "lease_token": "different-raw-lease-secret",
            "claimed_at": "2026-08-27T01:02:02.000004Z",
            "lease_expires_at": "2026-08-27T01:03:04.000004Z",
        }
        for field_name, value in replacements.items():
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    InvocationStartClaimed(receipt, replace(lease, **{field_name: value}))

        for field_name in ("attempt_number", "max_attempts", "lease_epoch"):
            for value in (True, IntegerSubclass(1)):
                with self.subTest(integer_field=field_name, value_type=type(value).__name__):
                    forged = replace(lease)
                    object.__setattr__(forged, field_name, value)
                    with self.assertRaises(TypeError):
                        InvocationStartClaimed(receipt, forged)

    def test_claimed_validation_errors_never_retain_raw_lease_canaries(self) -> None:
        receipt = valid_start_receipt()
        mismatched_canary = "secret-mismatched-raw-lease-canary"
        surrogate_canary = "secret-surrogate-lease-canary-\ud800"
        cases = (
            (mismatched_canary, replace(valid_invocation_lease(), lease_token=mismatched_canary)),
            (surrogate_canary, replace(valid_invocation_lease(), lease_token=surrogate_canary)),
        )
        for canary, lease in cases:
            with self.subTest(canary=canary.encode("utf-8", "backslashreplace")):
                with self.assertRaises((TypeError, ValueError)) as caught:
                    InvocationStartClaimed(receipt, lease)
                self.assertNotIn(
                    canary.replace("\ud800", ""),
                    exception_chain_text(caught.exception),
                )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)


if __name__ == "__main__":
    unittest.main()
