from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from quantum_entanglement.attempts import InvocationStatus
from quantum_entanglement.invocation_execution import (
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    InvocationExecutionManifest,
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartObservedV3,
    ScopedTaskInvocationAdmissionRequestV2,
    TaskInvocationAdmissionRequest,
    build_scoped_task_invocation_admission_request_v2,
    build_task_invocation_admission_request,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition
from quantum_entanglement.store import (
    InvocationStartConflictError,
    SQLiteEventStore,
)

STORE_TIME = "2026-08-27T10:00:00Z"
REQUESTED_AT = "2026-08-27T10:00:00.000001Z"
RUNNING_AT = "2026-08-27T10:00:00.000002Z"
CLAIMED_AT = "2026-08-27T10:00:01.000000Z"


def scoped_request() -> ScopedTaskInvocationAdmissionRequestV2:
    manifest = ScopedInvocationExecutionManifestV2.from_dict(
        {
            "schemaVersion": 2,
            "tenantId": "tenant-scoped-store-1",
            "workspaceId": "workspace-scoped-store-1",
            "invocationId": "invocation-scoped-store-1",
            "sessionId": "session-scoped-store-1",
            "planId": "plan-scoped-store-1",
            "taskId": "task-scoped-store-1",
            "agentId": "agent-scoped-store-1",
            "jobIdempotencyKey": "invoke:task-scoped-store-1",
            "taskRevision": 5,
            "correlationId": "correlation-scoped-store-1",
            "causationId": "task-scoped-store-1",
            "envelopeDigest": "a" * 64,
            "contextDigest": "b" * 64,
            "authorizationDigest": "c" * 64,
            "runtimeRevision": "runtime:sha256:" + ("d" * 64),
            "effectClass": "pure",
            "retryClass": "never",
        }
    )
    return build_scoped_task_invocation_admission_request_v2(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=None,
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-scoped-request-store-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-scoped-running-store-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=61,
    )


def legacy_request() -> TaskInvocationAdmissionRequest:
    manifest = InvocationExecutionManifest.from_dict(
        {
            "schemaVersion": 1,
            "invocationId": "invocation-legacy-store-1",
            "sessionId": "session-legacy-store-1",
            "planId": "plan-legacy-store-1",
            "taskId": "task-legacy-store-1",
            "agentId": "agent-legacy-store-1",
            "jobIdempotencyKey": "invoke:task-legacy-store-1",
            "taskRevision": 5,
            "correlationId": "correlation-legacy-store-1",
            "causationId": "task-legacy-store-1",
            "envelopeDigest": "a" * 64,
            "contextDigest": "b" * 64,
            "authorizationDigest": "c" * 64,
            "runtimeRevision": "runtime:sha256:" + ("d" * 64),
            "effectClass": "pure",
            "retryClass": "never",
        }
    )
    return build_task_invocation_admission_request(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=None,
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-legacy-request-store-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-legacy-running-store-1",
        task_running_timestamp=RUNNING_AT,
    )


def table_counts(store: SQLiteEventStore) -> tuple[int, int, int, int]:
    connection = store._connection
    return (
        int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM invocation_jobs").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM invocation_attempts").fetchone()[0]),
    )


class ScopedTaskInvocationAdmissionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_scoped_wrapper_commits_event_job_and_receipt_as_one_unit(self) -> None:
        request = scoped_request()

        result = self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )

        self.assertEqual(table_counts(self.store), (2, 1, 1, 0))
        self.assertEqual([item.sequence for item in result.events], [1, 2])
        self.assertEqual(
            [item.event.event_type for item in result.events],
            [TASK_EXECUTION_REQUESTED_EVENT_TYPE, TASK_STATUS_CHANGED_EVENT_TYPE],
        )
        self.assertEqual(result.events[0].event.payload, request.manifest.to_dict())
        self.assertEqual(result.job.status, InvocationStatus.QUEUED)
        self.assertEqual(result.job.max_attempts, 1)
        self.assertEqual(result.job.payload_digest, request.manifest.canonical_digest())

    def test_exact_replay_returns_original_atomic_rows(self) -> None:
        request = scoped_request()
        first = self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )

        replay = self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )

        self.assertEqual(replay, first)
        self.assertEqual(table_counts(self.store), (2, 1, 1, 0))

    def test_generic_atomic_admission_can_be_revalidated_by_scoped_wrapper(self) -> None:
        request = scoped_request()
        events, job = request.components()
        generic = self.store.append_invocation_admission(
            events,
            job,
            expected_version=0,
        )

        scoped = self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )

        self.assertEqual(scoped, generic)
        self.assertEqual(table_counts(self.store), (2, 1, 1, 0))

    def test_scoped_and_legacy_semantic_wrappers_never_upcast_each_other(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "exact ScopedTaskInvocationAdmissionRequestV2",
        ):
            self.store.append_scoped_task_invocation_admission_v2(
                cast(ScopedTaskInvocationAdmissionRequestV2, legacy_request()),
                expected_version=0,
            )
        with self.assertRaisesRegex(TypeError, "exact TaskInvocationAdmissionRequest"):
            self.store.append_task_invocation_admission(
                cast(TaskInvocationAdmissionRequest, scoped_request()),
                expected_version=0,
            )
        self.assertEqual(table_counts(self.store), (0, 0, 0, 0))

    def test_exact_type_rejects_subclass_duck_type_and_component_tuple(self) -> None:
        class RequestSubclass(ScopedTaskInvocationAdmissionRequestV2):
            pass

        class DuckRequest:
            def components(self) -> object:
                raise AssertionError("duck request callback must not run")

        candidates = (
            object.__new__(RequestSubclass),
            DuckRequest(),
            scoped_request().components(),
        )
        for candidate in candidates:
            with self.subTest(candidate_type=type(candidate).__name__):
                with self.assertRaisesRegex(
                    TypeError,
                    "exact ScopedTaskInvocationAdmissionRequestV2",
                ):
                    self.store.append_scoped_task_invocation_admission_v2(
                        cast(ScopedTaskInvocationAdmissionRequestV2, candidate),
                        expected_version=0,
                    )
        self.assertEqual(table_counts(self.store), (0, 0, 0, 0))

    def test_store_uses_class_owned_component_and_validation_paths(self) -> None:
        request = scoped_request()

        def spoofed(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("instance method spoof must not run")

        object.__setattr__(request, "components", spoofed)
        object.__setattr__(request, "validate_components", spoofed)

        result = self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )

        self.assertEqual(len(result.events), 2)
        self.assertEqual(table_counts(self.store), (2, 1, 1, 0))

    def test_class_owned_tampered_components_fail_before_write(self) -> None:
        request = scoped_request()
        events, job = request.components()
        changed_payload = dict(events[0].payload)
        changed_payload["workspaceId"] = "workspace-confused-deputy"
        tampered = (replace(events[0], payload=changed_payload), events[1])

        with mock.patch.object(
            ScopedTaskInvocationAdmissionRequestV2,
            "components",
            return_value=(tampered, job),
        ):
            with self.assertRaises(ValueError):
                self.store.append_scoped_task_invocation_admission_v2(
                    request,
                    expected_version=0,
                )

        self.assertEqual(table_counts(self.store), (0, 0, 0, 0))

    def test_expected_version_is_an_exact_nonnegative_integer(self) -> None:
        class IntegerSubclass(int):
            pass

        for value in (None, True, IntegerSubclass(0), -1):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.append_scoped_task_invocation_admission_v2(
                        scoped_request(),
                        expected_version=cast(int, value),
                    )
        self.assertEqual(table_counts(self.store), (0, 0, 0, 0))

    def test_scoped_admission_is_not_legacy_start_authority(self) -> None:
        request = scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )
        self.store._clock = lambda: CLAIMED_AT

        with self.assertRaises(InvocationStartConflictError):
            self.store.claim_invocation_start(
                request.manifest.invocation_id,
                "worker-must-not-run",
                lease_seconds=60,
                expected_version=2,
            )

        self.assertEqual(table_counts(self.store), (2, 1, 1, 0))

    def test_scoped_first_claim_commits_attempt_and_schema3_start_atomically(self) -> None:
        request = scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )
        self.store._clock = lambda: CLAIMED_AT

        result = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-store-1",
            lease_seconds=60,
            expected_version=2,
        )

        self.assertIs(type(result), ScopedInvocationStartClaimedV3)
        claimed = cast(ScopedInvocationStartClaimedV3, result)
        self.assertEqual(table_counts(self.store), (3, 1, 1, 1))
        self.assertEqual(claimed.receipt.sequence, 3)
        self.assertEqual(claimed.receipt.evidence.schema_version, 3)
        self.assertEqual(claimed.receipt.evidence.tenant_id, request.manifest.tenant_id)
        self.assertEqual(
            claimed.receipt.evidence.workspace_id,
            request.manifest.workspace_id,
        )
        self.assertEqual(
            claimed.receipt.evidence.manifest_digest,
            request.manifest.canonical_digest(),
        )
        self.assertEqual(claimed.lease.attempt_id, claimed.receipt.evidence.attempt_id)
        self.assertNotIn(claimed.lease.lease_token, repr(claimed))

    def test_scoped_claim_replay_returns_observation_without_lease(self) -> None:
        request = scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )
        self.store._clock = lambda: CLAIMED_AT
        first = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-store-1",
            lease_seconds=60,
            expected_version=2,
        )

        replay = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-store-other",
            lease_seconds=60,
            expected_version=2,
        )

        self.assertIs(type(first), ScopedInvocationStartClaimedV3)
        self.assertIs(type(replay), ScopedInvocationStartObservedV3)
        self.assertEqual(replay.receipt, first.receipt)
        self.assertFalse(hasattr(replay, "lease"))
        self.assertEqual(table_counts(self.store), (3, 1, 1, 1))

    def test_scoped_read_is_capability_free_and_wrong_scope_is_not_found(self) -> None:
        request = scoped_request()
        self.assertIsNone(
            self.store.read_scoped_invocation_start_v3(
                request.manifest.tenant_id,
                request.manifest.workspace_id,
                request.manifest.invocation_id,
            )
        )
        self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )
        self.assertIsNone(
            self.store.read_scoped_invocation_start_v3(
                request.manifest.tenant_id,
                request.manifest.workspace_id,
                request.manifest.invocation_id,
            )
        )
        self.store._clock = lambda: CLAIMED_AT
        claimed = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-store-1",
            lease_seconds=60,
            expected_version=2,
        )

        observed = self.store.read_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
        )
        wrong_tenant = self.store.read_scoped_invocation_start_v3(
            "tenant-other",
            request.manifest.workspace_id,
            request.manifest.invocation_id,
        )
        wrong_workspace = self.store.read_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            "workspace-other",
            request.manifest.invocation_id,
        )

        self.assertIs(type(observed), ScopedInvocationStartObservedV3)
        self.assertEqual(observed.receipt, claimed.receipt)
        self.assertFalse(hasattr(observed, "lease"))
        self.assertIsNone(wrong_tenant)
        self.assertIsNone(wrong_workspace)

    def test_reopen_can_observe_but_never_reissue_scoped_lease(self) -> None:
        request = scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )
        self.store._clock = lambda: CLAIMED_AT
        claimed = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-store-1",
            lease_seconds=60,
            expected_version=2,
        )
        self.store.close()

        reopened = SQLiteEventStore(self.path, clock=lambda: CLAIMED_AT)
        try:
            observed = reopened.read_scoped_invocation_start_v3(
                request.manifest.tenant_id,
                request.manifest.workspace_id,
                request.manifest.invocation_id,
            )
            replay = reopened.claim_scoped_invocation_start_v3(
                request.manifest.tenant_id,
                request.manifest.workspace_id,
                request.manifest.invocation_id,
                "worker-scoped-store-other",
                lease_seconds=60,
                expected_version=2,
            )
        finally:
            reopened.close()

        self.assertIs(type(observed), ScopedInvocationStartObservedV3)
        self.assertIs(type(replay), ScopedInvocationStartObservedV3)
        self.assertEqual(observed.receipt, claimed.receipt)
        self.assertEqual(replay.receipt, claimed.receipt)
        self.assertFalse(hasattr(replay, "lease"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
