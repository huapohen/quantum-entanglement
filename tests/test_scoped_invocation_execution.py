from __future__ import annotations

import hashlib
import json
import unicodedata
import unittest
from dataclasses import replace

import quantum_entanglement
import quantum_entanglement.invocation_execution as invocation_execution_module
from quantum_entanglement.attempts import InvocationJobSpec
from quantum_entanglement.invocation_execution import (
    SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN,
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    EffectClass,
    InvocationExecutionManifest,
    ScopedInvocationExecutionManifestV2,
    ScopedTaskInvocationAdmissionRequestV2,
    TaskInvocationAdmissionRequest,
    build_scoped_task_invocation_admission_request_v2,
    build_task_invocation_admission_request,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition

REQUESTED_AT = "2026-08-27T09:00:00.000001Z"
RUNNING_AT = "2026-08-27T09:00:00.000002Z"


def valid_manifest_dict() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "tenantId": "tenant-scoped-1",
        "workspaceId": "workspace-scoped-1",
        "invocationId": "invocation-scoped-1",
        "sessionId": "session-scoped-1",
        "planId": "plan-scoped-1",
        "taskId": "task-scoped-1",
        "agentId": "agent-scoped-1",
        "jobIdempotencyKey": "invoke:task-scoped-1",
        "taskRevision": 19,
        "correlationId": "correlation-scoped-1",
        "causationId": "task-scoped-1",
        "envelopeDigest": "a" * 64,
        "contextDigest": "b" * 64,
        "authorizationDigest": "c" * 64,
        "runtimeRevision": "runtime:sha256:" + ("d" * 64),
        "effectClass": "pure",
        "retryClass": "never",
    }


def valid_legacy_manifest_dict() -> dict[str, object]:
    scoped = valid_manifest_dict()
    del scoped["tenantId"]
    del scoped["workspaceId"]
    scoped["schemaVersion"] = 1
    return scoped


def valid_scoped_request() -> ScopedTaskInvocationAdmissionRequestV2:
    manifest = ScopedInvocationExecutionManifestV2.from_dict(valid_manifest_dict())
    return build_scoped_task_invocation_admission_request_v2(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason="scoped canonical admission",
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-scoped-execution-requested-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-scoped-task-running-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=73,
    )


def valid_legacy_request() -> TaskInvocationAdmissionRequest:
    manifest = InvocationExecutionManifest.from_dict(valid_legacy_manifest_dict())
    return build_task_invocation_admission_request(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason="legacy admission",
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-legacy-execution-requested-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-legacy-task-running-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=51,
    )


class ScopedInvocationExecutionManifestTests(unittest.TestCase):
    def test_scoped_manifest_contract_is_exported_from_both_surfaces(self) -> None:
        expected = {
            "SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN": (
                SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN
            ),
            "SCOPED_INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION": 2,
            "ScopedInvocationExecutionManifestV2": ScopedInvocationExecutionManifestV2,
            "ScopedTaskInvocationAdmissionRequestV2": ScopedTaskInvocationAdmissionRequestV2,
            "build_scoped_task_invocation_admission_request_v2": (
                build_scoped_task_invocation_admission_request_v2
            ),
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, invocation_execution_module.__all__)
                self.assertIn(name, quantum_entanglement.__all__)
                self.assertIs(getattr(quantum_entanglement, name), value)

    def test_exact_round_trip_and_domain_separated_digest(self) -> None:
        raw = valid_manifest_dict()

        manifest = ScopedInvocationExecutionManifestV2.from_dict(raw)

        self.assertEqual(manifest.to_dict(), raw)
        expected_bytes = json.dumps(
            raw,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(manifest.canonical_bytes(), expected_bytes)
        self.assertEqual(
            manifest.canonical_digest(),
            hashlib.sha256(
                SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN.encode("utf-8") + expected_bytes
            ).hexdigest(),
        )
        self.assertEqual(
            ScopedInvocationExecutionManifestV2.from_event_payload(
                TASK_EXECUTION_REQUESTED_EVENT_TYPE,
                raw,
            ),
            manifest,
        )

    def test_scope_is_covered_by_the_manifest_digest(self) -> None:
        manifest = ScopedInvocationExecutionManifestV2.from_dict(valid_manifest_dict())

        other_tenant = replace(manifest, tenant_id="tenant-scoped-2")
        other_workspace = replace(manifest, workspace_id="workspace-scoped-2")

        self.assertNotEqual(manifest.canonical_digest(), other_tenant.canonical_digest())
        self.assertNotEqual(manifest.canonical_digest(), other_workspace.canonical_digest())
        self.assertNotEqual(other_tenant.canonical_digest(), other_workspace.canonical_digest())

    def test_legacy_and_scoped_schemas_cannot_upcast_each_other(self) -> None:
        legacy = valid_legacy_manifest_dict()
        scoped = valid_manifest_dict()

        legacy_model = InvocationExecutionManifest.from_dict(legacy)
        scoped_model = ScopedInvocationExecutionManifestV2.from_dict(scoped)

        with self.assertRaises(ValueError):
            ScopedInvocationExecutionManifestV2.from_dict(legacy)
        with self.assertRaises(ValueError):
            InvocationExecutionManifest.from_dict(scoped)
        with self.assertRaises(TypeError):
            ScopedInvocationExecutionManifestV2.from_dict(legacy_model)
        with self.assertRaises(TypeError):
            InvocationExecutionManifest.from_dict(scoped_model)

    def test_exact_field_set_and_schema_version_fail_closed(self) -> None:
        baseline = valid_manifest_dict()

        missing = dict(baseline)
        del missing["workspaceId"]
        extra = dict(baseline)
        extra["scope"] = "forbidden"
        future = dict(baseline)
        future["schemaVersion"] = 3
        bool_version = dict(baseline)
        bool_version["schemaVersion"] = True

        for label, raw in (
            ("missing", missing),
            ("extra", extra),
            ("future", future),
            ("bool", bool_version),
        ):
            with self.subTest(case=label):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)

        with self.assertRaises(TypeError):
            ScopedInvocationExecutionManifestV2.from_dict(tuple(baseline.items()))
        with self.assertRaises(TypeError):
            ScopedInvocationExecutionManifestV2.from_dict({1: "non-string-key"})

    def test_all_identity_fields_require_bounded_nfc_plain_text(self) -> None:
        identity_fields = (
            "tenantId",
            "workspaceId",
            "invocationId",
            "sessionId",
            "planId",
            "taskId",
            "agentId",
            "jobIdempotencyKey",
            "correlationId",
            "runtimeRevision",
        )
        invalid_values = (
            "",
            " padded",
            "padded ",
            "line\nfeed",
            "delete\x7f",
            "x" * 4_097,
            "surrogate-\ud800",
            unicodedata.normalize("NFD", "é"),
            7,
        )
        for field_name in identity_fields:
            for value in invalid_values:
                with self.subTest(field=field_name, value=repr(value)[:40]):
                    raw = valid_manifest_dict()
                    raw[field_name] = value
                    if field_name == "taskId":
                        raw["causationId"] = value
                    with self.assertRaises((TypeError, ValueError, UnicodeError)):
                        ScopedInvocationExecutionManifestV2.from_dict(raw)

    def test_causation_is_exactly_the_scoped_task(self) -> None:
        raw = valid_manifest_dict()
        raw["causationId"] = "task-other"

        with self.assertRaisesRegex(ValueError, "causationId"):
            ScopedInvocationExecutionManifestV2.from_dict(raw)

    def test_revision_digests_and_enums_are_strict(self) -> None:
        invalid_revision_values = (True, 0, -1, (1 << 63), "1")
        for value in invalid_revision_values:
            with self.subTest(revision=value):
                raw = valid_manifest_dict()
                raw["taskRevision"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)

        for field_name in ("envelopeDigest", "contextDigest", "authorizationDigest"):
            for value in ("A" * 64, "a" * 63, "g" * 64, 1):
                with self.subTest(field=field_name, digest=value):
                    raw = valid_manifest_dict()
                    raw[field_name] = value
                    with self.assertRaises((TypeError, ValueError)):
                        ScopedInvocationExecutionManifestV2.from_dict(raw)

        for value in ("unknown", 1, True):
            with self.subTest(effect=value):
                raw = valid_manifest_dict()
                raw["effectClass"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)
            with self.subTest(retry=value):
                raw = valid_manifest_dict()
                raw["retryClass"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)

        for effect_class in EffectClass:
            raw = valid_manifest_dict()
            raw["effectClass"] = effect_class.value
            self.assertIs(
                ScopedInvocationExecutionManifestV2.from_dict(raw).effect_class,
                effect_class,
            )

    def test_event_decoder_rejects_every_other_vocabulary(self) -> None:
        for event_type in (
            "task.execution_requested",
            "task.invocation.started",
            "task.result.received",
            " task.execution.requested",
            1,
        ):
            with self.subTest(event_type=event_type):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_event_payload(
                        event_type,
                        valid_manifest_dict(),
                    )

    def test_decoder_snapshots_input_and_rejects_subclass_dispatch(self) -> None:
        raw = valid_manifest_dict()
        manifest = ScopedInvocationExecutionManifestV2.from_dict(raw)
        raw["tenantId"] = "mutated-after-decode"

        self.assertEqual(manifest.tenant_id, "tenant-scoped-1")

        class ScopedSubclass(ScopedInvocationExecutionManifestV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            ScopedSubclass.from_dict(valid_manifest_dict())
        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            ScopedSubclass.from_event_payload(
                TASK_EXECUTION_REQUESTED_EVENT_TYPE,
                valid_manifest_dict(),
            )

    def test_canonical_bytes_are_stable_across_input_key_order(self) -> None:
        baseline = valid_manifest_dict()
        reversed_input = dict(reversed(tuple(baseline.items())))

        first = ScopedInvocationExecutionManifestV2.from_dict(baseline)
        second = ScopedInvocationExecutionManifestV2.from_dict(reversed_input)

        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.canonical_digest(), second.canonical_digest())


class ScopedTaskInvocationAdmissionRequestTests(unittest.TestCase):
    def test_components_bind_scope_into_event_and_queued_job_digest(self) -> None:
        request = valid_scoped_request()

        events, job = request.components()

        self.assertIs(type(events), tuple)
        self.assertEqual(len(events), 2)
        requested, running = events
        self.assertEqual(requested.event_type, TASK_EXECUTION_REQUESTED_EVENT_TYPE)
        self.assertEqual(requested.payload, request.manifest.to_dict())
        self.assertEqual(requested.payload["tenantId"], request.manifest.tenant_id)
        self.assertEqual(requested.payload["workspaceId"], request.manifest.workspace_id)
        self.assertEqual(running.event_type, "task.status.changed")
        self.assertEqual(request.stream_id, "session:" + request.manifest.session_id)
        self.assertEqual(job.payload_digest, request.manifest.canonical_digest())
        self.assertEqual(job.max_attempts, 1)
        self.assertEqual(job.priority, 73)

    def test_components_are_fresh_and_round_trip_through_exact_decoder(self) -> None:
        request = valid_scoped_request()

        first_events, first_job = request.components()
        second_events, second_job = request.components()
        decoded = ScopedTaskInvocationAdmissionRequestV2.from_components(
            first_events,
            first_job,
        )

        self.assertEqual(decoded, request)
        self.assertEqual(first_events, second_events)
        self.assertEqual(first_job, second_job)
        self.assertIsNot(first_events, second_events)
        self.assertIsNot(first_events[0], second_events[0])
        self.assertIsNot(first_job, second_job)

    def test_legacy_and_scoped_admission_decoders_never_upcast(self) -> None:
        scoped_components = valid_scoped_request().components()
        legacy_components = valid_legacy_request().components()

        with self.assertRaises(ValueError):
            TaskInvocationAdmissionRequest.from_components(*scoped_components)
        with self.assertRaises(ValueError):
            ScopedTaskInvocationAdmissionRequestV2.from_components(*legacy_components)

    def test_reordered_extra_and_tampered_events_fail_closed(self) -> None:
        request = valid_scoped_request()
        events, job = request.components()

        changed_scope = dict(events[0].payload)
        changed_scope["tenantId"] = "tenant-confused-deputy"
        tampered_requested = replace(events[0], payload=changed_scope)
        cases = (
            (tuple(reversed(events)), job),
            (events + (events[0],), job),
            ((tampered_requested, events[1]), job),
            (
                events,
                replace(job, payload_digest="f" * 64),
            ),
        )
        for index, (changed_events, changed_job) in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)):
                    request.validate_components(changed_events, changed_job)

    def test_every_event_envelope_coordinate_is_bound(self) -> None:
        request = valid_scoped_request()
        events, job = request.components()
        requested, running = events
        changes = {
            "stream_id": "session:other",
            "actor_id": "other-actor",
            "event_id": "event-other",
            "correlation_id": "correlation-other",
            "causation_id": "task-other",
            "idempotency_key": "other-idempotency",
            "timestamp": "2026-08-27T09:00:00.000003Z",
        }
        for event_index, event in enumerate((requested, running)):
            for field_name, value in changes.items():
                with self.subTest(event=event_index, field=field_name):
                    changed = replace(event, **{field_name: value})
                    changed_events = (
                        (changed, running) if event_index == 0 else (requested, changed)
                    )
                    with self.assertRaises(ValueError):
                        request.validate_components(changed_events, job)

    def test_transition_identity_revision_and_shape_are_exact(self) -> None:
        manifest = ScopedInvocationExecutionManifestV2.from_dict(valid_manifest_dict())
        transitions = (
            TaskTransition(
                "task-other",
                TaskStatus.READY,
                TaskStatus.RUNNING,
                None,
                manifest.task_revision,
            ),
            TaskTransition(
                manifest.task_id,
                TaskStatus.READY,
                TaskStatus.RUNNING,
                None,
                manifest.task_revision + 1,
            ),
            TaskTransition(
                manifest.task_id,
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
                None,
                manifest.task_revision,
            ),
        )
        for transition in transitions:
            with self.subTest(transition=transition):
                with self.assertRaises(ValueError):
                    build_scoped_task_invocation_admission_request_v2(
                        manifest,
                        transition,
                        execution_requested_event_id="event-one",
                        execution_requested_timestamp=REQUESTED_AT,
                        task_running_event_id="event-two",
                        task_running_timestamp=RUNNING_AT,
                    )

    def test_ids_timestamps_priority_and_availability_fail_before_components(self) -> None:
        manifest = ScopedInvocationExecutionManifestV2.from_dict(valid_manifest_dict())
        transition = TaskTransition(
            manifest.task_id,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            None,
            manifest.task_revision,
        )
        baseline = {
            "execution_requested_event_id": "event-one",
            "execution_requested_timestamp": REQUESTED_AT,
            "task_running_event_id": "event-two",
            "task_running_timestamp": RUNNING_AT,
            "job_priority": 50,
            "job_available_at": None,
        }
        invalid_changes = (
            {"task_running_event_id": "event-one"},
            {"task_running_timestamp": "2026-08-27T08:59:59.999999Z"},
            {"job_priority": True},
            {"job_priority": -1},
            {"job_priority": 101},
            {"job_available_at": "not-a-time"},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                values = {**baseline, **changes}
                with self.assertRaises((TypeError, ValueError)):
                    ScopedTaskInvocationAdmissionRequestV2(
                        manifest=manifest,
                        transition=transition,
                        **values,  # type: ignore[arg-type]
                    )

    def test_decoder_requires_exact_domain_types(self) -> None:
        request = valid_scoped_request()
        events, job = request.components()

        with self.assertRaises(TypeError):
            ScopedTaskInvocationAdmissionRequestV2.from_components(list(events), job)
        with self.assertRaises(TypeError):
            ScopedTaskInvocationAdmissionRequestV2.from_components(
                ({"event": "not-domain"}, events[1]),
                job,
            )
        with self.assertRaises(TypeError):
            ScopedTaskInvocationAdmissionRequestV2.from_components(
                events,
                {"invocation_id": job.invocation_id},
            )
        with self.assertRaises(TypeError):
            ScopedTaskInvocationAdmissionRequestV2.from_components(events, object())

    def test_direct_component_forgery_cannot_change_the_scoped_job_binding(self) -> None:
        request = valid_scoped_request()
        events, job = request.components()
        forged_job = InvocationJobSpec(
            session_id=job.session_id,
            plan_id=job.plan_id,
            task_id=job.task_id,
            agent_id=job.agent_id,
            idempotency_key=job.idempotency_key,
            payload_digest=job.payload_digest,
            invocation_id=job.invocation_id,
            priority=job.priority,
            max_attempts=2,
            available_at=job.available_at,
        )

        with self.assertRaisesRegex(ValueError, "scoped manifest binding"):
            request.validate_components(events, forged_job)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
