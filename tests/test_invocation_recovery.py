# ruff: noqa: UP045
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Optional

import quantum_entanglement
from quantum_entanglement.attempts import (
    AttemptStatus,
    InvocationJobSpec,
    InvocationRecoverySnapshot,
    InvocationStatus,
    SQLiteInvocationAttemptStore,
    invocation_payload_digest,
)
from quantum_entanglement.invocation_recovery import (
    InvocationBinding,
    InvocationRecoveryClosedError,
    InvocationRecoveryCoordinator,
    InvocationRecoveryDecision,
    InvocationRecoveryIntegrityError,
    InvocationResultReceipt,
    assess_invocation_recovery,
)
from quantum_entanglement.protocol import TaskStatus

T0 = "2026-08-20T00:00:00Z"


class MutableClock:
    def __init__(self, value: str = T0) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


def job_spec(
    label: str,
    *,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
) -> InvocationJobSpec:
    return InvocationJobSpec(
        invocation_id=f"invocation-{label}",
        session_id="session-1",
        plan_id="plan-1",
        task_id=f"task-{label}",
        agent_id=f"agent-{label}",
        idempotency_key=f"invoke:task-{label}",
        payload_digest=invocation_payload_digest(
            {
                "schemaVersion": 1,
                "taskId": f"task-{label}",
                "contextDigest": f"context-{label}",
            }
        ),
        max_attempts=max_attempts,
        available_at=available_at,
    )


def binding_for(spec: InvocationJobSpec) -> InvocationBinding:
    return InvocationBinding(
        invocation_id=spec.invocation_id,
        session_id=spec.session_id,
        plan_id=spec.plan_id,
        task_id=spec.task_id,
        agent_id=spec.agent_id,
        idempotency_key=spec.idempotency_key,
        payload_digest=spec.payload_digest,
    )


def receipt_for(
    binding: InvocationBinding,
    snapshot: InvocationRecoverySnapshot,
    *,
    result_ref: str = "result:1",
) -> InvocationResultReceipt:
    attempt = snapshot.current_attempt
    if attempt is None:
        raise AssertionError("receipt helper requires an attempt")
    return InvocationResultReceipt(
        binding=binding,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        lease_epoch=attempt.lease_epoch,
        lease_token_digest=attempt.lease_token_digest,
        result_ref=result_ref,
        manifest_digest=invocation_payload_digest(
            {"resultRef": result_ref, "artifacts": ["artifact:1"]}
        ),
        receipt_id=f"receipt:{attempt.attempt_id}",
        stream_id=f"session:{binding.session_id}",
        stream_sequence=12,
    )


class InvocationRecoveryDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "attempts.sqlite3")
        self.clock = MutableClock()
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def snapshot(self, spec: InvocationJobSpec) -> InvocationRecoverySnapshot:
        return self.store.recovery_snapshot_for_task(spec.session_id, spec.task_id)

    def enqueue(self, label: str, *, max_attempts: int = 3) -> InvocationJobSpec:
        spec = job_spec(label, max_attempts=max_attempts)
        self.store.enqueue(spec)
        return spec

    def running(
        self,
        label: str,
        *,
        max_attempts: int = 3,
    ) -> tuple[InvocationJobSpec, object, InvocationRecoverySnapshot]:
        spec = self.enqueue(label, max_attempts=max_attempts)
        lease = self.store.claim(spec.invocation_id, f"worker-{label}", lease_seconds=30)
        self.assertIsNotNone(lease)
        return spec, lease, self.snapshot(spec)

    def assess(
        self,
        binding: InvocationBinding,
        snapshot: InvocationRecoverySnapshot,
        receipt: Optional[InvocationResultReceipt] = None,
    ) -> InvocationRecoveryDecision:
        return assess_invocation_recovery(TaskStatus.RUNNING, binding, snapshot, receipt)

    def test_missing_first_claim_retry_and_active_lease_are_distinct(self) -> None:
        missing_spec = job_spec("missing")
        self.assertEqual(
            self.assess(
                binding_for(missing_spec),
                InvocationRecoverySnapshot(None, None, 0),
            ),
            InvocationRecoveryDecision.BLOCKED_MISSING_JOB,
        )

        first_spec = self.enqueue("first")
        self.assertEqual(
            self.assess(binding_for(first_spec), self.snapshot(first_spec)),
            InvocationRecoveryDecision.FIRST_CLAIM_READY,
        )

        retry_spec, retry_lease, _running = self.running("retry")
        self.assertTrue(self.store.fail(retry_lease, "retryable", retry_at=T0))  # type: ignore[arg-type]
        retry_snapshot = self.snapshot(retry_spec)
        self.assertEqual(retry_snapshot.job.status, InvocationStatus.QUEUED)
        self.assertEqual(
            self.assess(binding_for(retry_spec), retry_snapshot),
            InvocationRecoveryDecision.BLOCKED_EFFECT_UNKNOWN,
        )

        running_spec, _lease, running_snapshot = self.running("active")
        self.assertEqual(
            self.assess(binding_for(running_spec), running_snapshot),
            InvocationRecoveryDecision.WAITING_ACTIVE_LEASE,
        )

    def test_running_and_queued_candidate_result_reference_remains_unverified(self) -> None:
        spec, lease, running_snapshot = self.running("receipt-split")
        binding = binding_for(spec)
        receipt = receipt_for(binding, running_snapshot)
        self.assertEqual(
            self.assess(binding, running_snapshot, receipt),
            InvocationRecoveryDecision.BLOCKED_RECEIPT_UNVERIFIED,
        )

        self.assertTrue(self.store.fail(lease, "crash recovery", retry_at=T0))  # type: ignore[arg-type]
        queued_snapshot = self.snapshot(spec)
        self.assertEqual(queued_snapshot.job.status, InvocationStatus.QUEUED)
        self.assertEqual(
            self.assess(binding, queued_snapshot, receipt),
            InvocationRecoveryDecision.BLOCKED_RECEIPT_UNVERIFIED,
        )

    def test_unverified_receipt_never_authorizes_terminal_failure_recovery(self) -> None:
        spec, lease, running_snapshot = self.running("receipt-failed", max_attempts=1)
        binding = binding_for(spec)
        receipt = receipt_for(binding, running_snapshot)
        self.assertTrue(self.store.fail(lease, "terminal"))  # type: ignore[arg-type]
        failed_snapshot = self.snapshot(spec)
        self.assertEqual(failed_snapshot.job.status, InvocationStatus.FAILED)
        self.assertEqual(
            self.assess(binding, failed_snapshot, receipt),
            InvocationRecoveryDecision.BLOCKED_RECEIPT_UNVERIFIED,
        )

    def test_success_remains_blocked_with_an_unverified_result_receipt(self) -> None:
        spec, lease, running_snapshot = self.running("success")
        binding = binding_for(spec)
        receipt = receipt_for(binding, running_snapshot, result_ref="result:success")
        self.assertTrue(
            self.store.complete(lease, result_ref=receipt.result_ref)  # type: ignore[arg-type]
        )
        succeeded = self.snapshot(spec)
        self.assertEqual(
            self.assess(binding, succeeded),
            InvocationRecoveryDecision.BLOCKED_RESULT_UNCOMMITTED,
        )
        self.assertEqual(
            self.assess(binding, succeeded, receipt),
            InvocationRecoveryDecision.BLOCKED_RECEIPT_UNVERIFIED,
        )

        mismatched_ref = replace(receipt, result_ref="result:other")
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "result_ref"):
            self.assess(binding, succeeded, mismatched_ref)

    def test_success_without_result_reference_blocks_unverified_receipt(self) -> None:
        spec, lease, running_snapshot = self.running("success-no-reference")
        binding = binding_for(spec)
        receipt = receipt_for(binding, running_snapshot)
        self.assertTrue(self.store.complete(lease))  # type: ignore[arg-type]
        succeeded = self.snapshot(spec)
        self.assertIsNone(succeeded.job.result_ref)
        self.assertEqual(
            self.assess(binding, succeeded),
            InvocationRecoveryDecision.BLOCKED_RESULT_UNCOMMITTED,
        )
        self.assertEqual(
            self.assess(binding, succeeded, receipt),
            InvocationRecoveryDecision.BLOCKED_RECEIPT_UNVERIFIED,
        )

    def test_failure_without_receipt_preserves_effect_unknown(self) -> None:
        spec, lease, _running = self.running("failed", max_attempts=1)
        self.assertTrue(self.store.fail(lease, "terminal"))  # type: ignore[arg-type]
        self.assertEqual(
            self.assess(binding_for(spec), self.snapshot(spec)),
            InvocationRecoveryDecision.TERMINAL_FAILURE_EFFECT_UNKNOWN,
        )

    def test_orphan_receipt_and_canceled_state_fail_closed(self) -> None:
        receipt_spec, _lease, running_snapshot = self.running("orphan")
        binding = binding_for(receipt_spec)
        receipt = receipt_for(binding, running_snapshot)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "without invocation job"):
            self.assess(binding, InvocationRecoverySnapshot(None, None, 0), receipt)

        canceled_spec, canceled_lease, _running = self.running("canceled", max_attempts=1)
        self.assertTrue(self.store.fail(canceled_lease, "terminal"))  # type: ignore[arg-type]
        failed = self.snapshot(canceled_spec)
        canceled_job = replace(
            failed.job,
            status=InvocationStatus.CANCELED,
            last_error=None,
        )
        canceled_attempt = replace(
            failed.current_attempt,
            status=AttemptStatus.CANCELED,
            error=None,
        )
        canceled = InvocationRecoverySnapshot(canceled_job, canceled_attempt, 1)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "cancellation receipt"):
            self.assess(binding_for(canceled_spec), canceled)

    def test_every_job_identity_and_payload_field_is_bound(self) -> None:
        spec = self.enqueue("binding")
        binding = binding_for(spec)
        snapshot = self.snapshot(spec)
        cases = {
            "invocation_id": "other-invocation",
            "session_id": "other-session",
            "plan_id": "other-plan",
            "task_id": "other-task",
            "agent_id": "other-agent",
            "idempotency_key": "invoke:other",
            "payload_digest": "0" * 64,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                forged = replace(snapshot, job=replace(snapshot.job, **{field: value}))
                with self.assertRaisesRegex(InvocationRecoveryIntegrityError, field):
                    self.assess(binding, forged)

    def test_receipt_is_bound_to_current_attempt_and_invocation(self) -> None:
        spec, _lease, snapshot = self.running("receipt-binding")
        binding = binding_for(spec)
        receipt = receipt_for(binding, snapshot)
        cases = (
            (replace(receipt, attempt_id="attempt-other"), "attempt_id"),
            (replace(receipt, attempt_number=2), "attempt_number"),
            (replace(receipt, lease_epoch=2), "lease_epoch"),
            (replace(receipt, lease_token_digest="0" * 64), "lease_token_digest"),
            (
                replace(
                    receipt,
                    binding=replace(binding, plan_id="plan-other"),
                ),
                "binding",
            ),
        )
        for forged, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(InvocationRecoveryIntegrityError, message):
                    self.assess(binding, snapshot, forged)

    def test_receipt_manifest_and_event_position_shapes_are_revalidated(self) -> None:
        spec, _lease, snapshot = self.running("receipt-shape")
        binding = binding_for(spec)
        receipt = receipt_for(binding, snapshot)
        mutations = (
            ("manifest_digest", "not-a-digest", "manifest_digest"),
            ("stream_id", "session:other", "stream_id"),
            ("stream_sequence", 0, "stream_sequence"),
            ("receipt_id", "receipt\nforged", "control character"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                forged = replace(receipt)
                object.__setattr__(forged, field, value)
                with self.assertRaisesRegex(InvocationRecoveryIntegrityError, message):
                    self.assess(binding, snapshot, forged)

    def test_receipt_stream_allows_every_valid_session_identity_length(self) -> None:
        for session_bytes in (4_088, 4_089, 4_096):
            with self.subTest(session_bytes=session_bytes):
                session_id = "s" * session_bytes
                binding = InvocationBinding(
                    invocation_id="invocation-boundary",
                    session_id=session_id,
                    plan_id="plan-boundary",
                    task_id="task-boundary",
                    agent_id="agent-boundary",
                    idempotency_key="invoke:task-boundary",
                    payload_digest="0" * 64,
                )
                receipt = InvocationResultReceipt(
                    binding=binding,
                    attempt_id="attempt-boundary",
                    attempt_number=1,
                    lease_epoch=1,
                    lease_token_digest="1" * 64,
                    result_ref="result:boundary",
                    manifest_digest="2" * 64,
                    receipt_id="receipt:boundary",
                    stream_id=f"session:{session_id}",
                    stream_sequence=1,
                )
                self.assertEqual(receipt.stream_id, f"session:{session_id}")

    def test_snapshot_shape_and_cross_row_ownership_are_revalidated(self) -> None:
        queued_spec = self.enqueue("malformed")
        queued = self.snapshot(queued_spec)
        cases = (
            (
                replace(queued, job=replace(queued.job, status="queued")),  # type: ignore[arg-type]
                "status",
            ),
            (replace(queued, job=replace(queued.job, priority=True)), "priority"),
            (replace(queued, job=replace(queued.job, lease_epoch=1)), "lease_epoch"),
            (replace(queued, attempt_count=1), "attempt_count"),
            (
                replace(queued, job=replace(queued.job, last_error="orphan prior failure")),
                "zero-attempt job carries a last_error",
            ),
            (
                replace(queued, job=replace(queued.job, result_ref="result:forged")),
                "result_ref",
            ),
        )
        for forged, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(InvocationRecoveryIntegrityError, message):
                    self.assess(binding_for(queued_spec), forged)

        running_spec, _lease, running = self.running("owner-drift")
        forged_attempt = replace(running.current_attempt, lease_token_digest="0" * 64)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "ownership differs"):
            self.assess(
                binding_for(running_spec),
                replace(running, current_attempt=forged_attempt),
            )

    def test_failed_snapshot_error_semantics_are_revalidated(self) -> None:
        spec, lease, _running = self.running("error-binding", max_attempts=1)
        self.assertTrue(self.store.fail(lease, "expected failure"))  # type: ignore[arg-type]
        failed = self.snapshot(spec)

        missing = replace(failed.current_attempt, error=None)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "lacks an error"):
            self.assess(binding_for(spec), replace(failed, current_attempt=missing))

        mismatched = replace(failed.current_attempt, error="different failure")
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "error differs"):
            self.assess(binding_for(spec), replace(failed, current_attempt=mismatched))

    def test_snapshot_error_length_matches_the_attempt_store_contract(self) -> None:
        spec, lease, _running = self.running("error-length", max_attempts=1)
        self.assertTrue(self.store.fail(lease, "expected failure"))  # type: ignore[arg-type]
        failed = self.snapshot(spec)

        for exact in ("e" * 4_096, "界" * 4_096):
            with self.subTest(boundary="exact", character=exact[0]):
                candidate = replace(
                    failed,
                    job=replace(failed.job, last_error=exact),
                    current_attempt=replace(failed.current_attempt, error=exact),
                )
                self.assertEqual(
                    self.assess(binding_for(spec), candidate),
                    InvocationRecoveryDecision.TERMINAL_FAILURE_EFFECT_UNKNOWN,
                )

        for oversized in ("e" * 4_097, "界" * 4_097):
            with self.subTest(boundary="character", character=oversized[0]):
                candidate = replace(
                    failed,
                    job=replace(failed.job, last_error=oversized),
                    current_attempt=replace(failed.current_attempt, error=oversized),
                )
                with self.assertRaisesRegex(
                    InvocationRecoveryIntegrityError,
                    "exceeds its supported length",
                ):
                    self.assess(binding_for(spec), candidate)

        byte_oversized = "界" * 5_462
        candidate = replace(
            failed,
            job=replace(failed.job, last_error=byte_oversized),
            current_attempt=replace(failed.current_attempt, error=byte_oversized),
        )
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "exceeds its byte limit"):
            self.assess(binding_for(spec), candidate)

    def test_job_status_must_match_the_attempt_budget(self) -> None:
        queued_spec, queued_lease, _running = self.running("budget-queued", max_attempts=3)
        self.assertTrue(  # type: ignore[arg-type]
            self.store.fail(queued_lease, "retry", retry_at=T0)
        )
        queued = self.snapshot(queued_spec)
        forged_failed = replace(
            queued,
            job=replace(
                queued.job,
                status=InvocationStatus.FAILED,
                finished_at=queued.current_attempt.finished_at,
            ),
        )
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "remaining attempt budget"):
            self.assess(binding_for(queued_spec), forged_failed)

        failed_spec, failed_lease, _running = self.running("budget-failed", max_attempts=1)
        self.assertTrue(self.store.fail(failed_lease, "terminal"))  # type: ignore[arg-type]
        failed = self.snapshot(failed_spec)
        forged_queued = replace(
            failed,
            job=replace(
                failed.job,
                status=InvocationStatus.QUEUED,
                finished_at=None,
            ),
        )
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "exhausted its attempt"):
            self.assess(binding_for(failed_spec), forged_queued)

    def test_snapshot_timestamp_causality_is_revalidated(self) -> None:
        spec, _lease, snapshot = self.running("timestamp-causality")
        previous = "2026-08-19T23:59:59.000000Z"
        later = "2026-08-20T00:00:01.000000Z"
        cases = (
            (
                replace(
                    snapshot,
                    job=replace(snapshot.job, heartbeat_at=previous),
                    current_attempt=replace(snapshot.current_attempt, heartbeat_at=previous),
                ),
                "heartbeat_at precedes creation",
            ),
            (
                replace(
                    snapshot,
                    job=replace(snapshot.job, heartbeat_at=later),
                    current_attempt=replace(snapshot.current_attempt, heartbeat_at=later),
                ),
                "updated_at precedes heartbeat",
            ),
            (
                replace(
                    snapshot,
                    job=replace(
                        snapshot.job,
                        lease_expires_at=snapshot.job.heartbeat_at,
                    ),
                    current_attempt=replace(
                        snapshot.current_attempt,
                        lease_expires_at=snapshot.current_attempt.heartbeat_at,
                    ),
                ),
                "lease deadline",
            ),
            (
                replace(
                    snapshot,
                    current_attempt=replace(
                        snapshot.current_attempt,
                        started_at=previous,
                    ),
                ),
                "starts before its job",
            ),
        )
        for forged, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(InvocationRecoveryIntegrityError, message):
                    self.assess(binding_for(spec), forged)

        terminal_spec, terminal_lease, _running = self.running("terminal-causality")
        self.clock.set("2026-08-20T00:00:05Z")
        self.assertTrue(
            self.store.heartbeat(terminal_lease, lease_seconds=10)  # type: ignore[arg-type]
        )
        self.assertTrue(self.store.fail(terminal_lease, "failed"))  # type: ignore[arg-type]
        terminal = self.snapshot(terminal_spec)
        forged_terminal = replace(
            terminal,
            current_attempt=replace(
                terminal.current_attempt,
                finished_at="2026-08-20T00:00:04.000000Z",
            ),
        )
        with self.assertRaisesRegex(
            InvocationRecoveryIntegrityError,
            "finished_at precedes its heartbeat",
        ):
            self.assess(binding_for(terminal_spec), forged_terminal)

        late_attempt = replace(
            terminal,
            current_attempt=replace(
                terminal.current_attempt,
                finished_at="2026-08-20T00:00:06.000000Z",
            ),
        )
        with self.assertRaisesRegex(
            InvocationRecoveryIntegrityError,
            "job update precedes its current attempt",
        ):
            self.assess(binding_for(terminal_spec), late_attempt)

        failed_spec, failed_lease, _running = self.running(
            "terminal-finish-divergence",
            max_attempts=1,
        )
        self.assertTrue(self.store.fail(failed_lease, "terminal"))  # type: ignore[arg-type]
        failed = self.snapshot(failed_spec)
        divergent_finish = replace(
            failed,
            job=replace(
                failed.job,
                updated_at="2026-08-20T00:00:06.000000Z",
                finished_at="2026-08-20T00:00:06.000000Z",
            ),
        )
        with self.assertRaisesRegex(
            InvocationRecoveryIntegrityError,
            "job finish differs from its current attempt",
        ):
            self.assess(binding_for(failed_spec), divergent_finish)

    def test_schema_compatible_epoch_gap_is_not_coerced_or_rejected(self) -> None:
        spec, _lease, snapshot = self.running("epoch-gap")
        job = replace(snapshot.job, lease_epoch=2)
        attempt = replace(snapshot.current_attempt, lease_epoch=2)
        compatible = InvocationRecoverySnapshot(job, attempt, snapshot.attempt_count)
        self.assertEqual(
            self.assess(binding_for(spec), compatible),
            InvocationRecoveryDecision.WAITING_ACTIVE_LEASE,
        )

    def test_past_requested_availability_remains_a_valid_first_claim(self) -> None:
        spec = job_spec("past", available_at="2020-01-01T00:00:00Z")
        self.store.enqueue(spec)
        self.assertEqual(
            self.assess(binding_for(spec), self.snapshot(spec)),
            InvocationRecoveryDecision.FIRST_CLAIM_READY,
        )

    def test_non_running_task_projection_is_rejected(self) -> None:
        spec = self.enqueue("wrong-task-state")
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "RUNNING task"):
            assess_invocation_recovery(
                TaskStatus.COMPLETED,
                binding_for(spec),
                self.snapshot(spec),
            )

    def test_assessment_never_mutates_snapshot_or_receipt(self) -> None:
        spec, _lease, snapshot = self.running("immutable")
        binding = binding_for(spec)
        receipt = receipt_for(binding, snapshot)
        snapshot_before = replace(snapshot)
        receipt_before = replace(receipt)
        self.assess(binding, snapshot, receipt)
        self.assertEqual(snapshot, snapshot_before)
        self.assertEqual(receipt, receipt_before)

    def test_mutated_frozen_inputs_are_revalidated_at_the_boundary(self) -> None:
        spec = self.enqueue("mutated")
        binding = binding_for(spec)
        object.__setattr__(binding, "payload_digest", "not-a-digest")
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "payload_digest"):
            self.assess(binding, self.snapshot(spec))


class FakeRecoveryStore:
    def __init__(
        self,
        snapshot: object,
        *,
        read_error: Optional[BaseException] = None,
        close_failures: int = 0,
    ) -> None:
        self.snapshot = snapshot
        self.read_error = read_error
        self.close_failures = close_failures
        self.reads = 0
        self.closes = 0

    def recovery_snapshot_for_task(
        self,
        _session_id: str,
        _task_id: str,
    ) -> InvocationRecoverySnapshot:
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self.snapshot  # type: ignore[return-value]

    def close(self) -> None:
        self.closes += 1
        if self.closes <= self.close_failures:
            raise RuntimeError("close failed")


class InvocationRecoveryCoordinatorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "coordinator.sqlite3")
        self.store = SQLiteInvocationAttemptStore(self.path, clock=MutableClock())
        self.spec = job_spec("coordinator")
        self.binding = binding_for(self.spec)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_optional_store_defaults_to_a_blocked_missing_job(self) -> None:
        coordinator = InvocationRecoveryCoordinator()
        self.assertEqual(
            coordinator.assess(TaskStatus.RUNNING, self.binding),
            InvocationRecoveryDecision.BLOCKED_MISSING_JOB,
        )
        coordinator.close()
        self.assertTrue(coordinator.closed)
        with self.assertRaisesRegex(InvocationRecoveryClosedError, "closed"):
            coordinator.assess(TaskStatus.RUNNING, self.binding)

    def test_borrowed_store_remains_open_after_coordinator_shutdown(self) -> None:
        self.store.enqueue(self.spec)
        coordinator = InvocationRecoveryCoordinator(self.store)
        self.assertEqual(
            coordinator.assess(TaskStatus.RUNNING, self.binding),
            InvocationRecoveryDecision.FIRST_CLAIM_READY,
        )
        coordinator.close()
        coordinator.close()
        self.assertIsNotNone(self.store.get(self.spec.invocation_id))
        with self.assertRaises(InvocationRecoveryClosedError):
            coordinator.__enter__()

    def test_owned_store_closes_once_and_context_manager_uses_the_same_rule(self) -> None:
        fake = FakeRecoveryStore(InvocationRecoverySnapshot(None, None, 0))
        coordinator = InvocationRecoveryCoordinator(fake, owns_store=True)
        with coordinator as entered:
            self.assertIs(entered, coordinator)
            self.assertFalse(coordinator.closed)
        self.assertTrue(coordinator.closed)
        self.assertEqual(fake.closes, 1)
        coordinator.close()
        self.assertEqual(fake.closes, 1)

    def test_failed_owned_store_close_is_retryable(self) -> None:
        fake = FakeRecoveryStore(
            InvocationRecoverySnapshot(None, None, 0),
            close_failures=1,
        )
        coordinator = InvocationRecoveryCoordinator(fake, owns_store=True)
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            coordinator.close()
        self.assertTrue(coordinator.closed)
        with self.assertRaisesRegex(InvocationRecoveryClosedError, "closed"):
            coordinator.assess(TaskStatus.RUNNING, self.binding)
        with self.assertRaisesRegex(InvocationRecoveryClosedError, "closed"):
            coordinator.__enter__()
        coordinator.close()
        self.assertTrue(coordinator.closed)
        self.assertEqual(fake.closes, 2)

    def test_store_failures_are_not_coerced_into_missing_job(self) -> None:
        fake = FakeRecoveryStore(
            InvocationRecoverySnapshot(None, None, 0),
            read_error=RuntimeError("storage offline"),
        )
        coordinator = InvocationRecoveryCoordinator(fake)
        with self.assertRaisesRegex(RuntimeError, "storage offline"):
            coordinator.assess(TaskStatus.RUNNING, self.binding)
        self.assertEqual(fake.reads, 1)
        self.assertFalse(coordinator.closed)

    def test_invalid_inputs_fail_before_any_store_read(self) -> None:
        fake = FakeRecoveryStore(InvocationRecoverySnapshot(None, None, 0))
        coordinator = InvocationRecoveryCoordinator(fake)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "RUNNING task"):
            coordinator.assess(TaskStatus.COMPLETED, self.binding)

        mutated_binding = replace(self.binding)
        object.__setattr__(mutated_binding, "payload_digest", "invalid")
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "payload_digest"):
            coordinator.assess(TaskStatus.RUNNING, mutated_binding)

        self.store.enqueue(self.spec)
        lease = self.store.claim(self.spec.invocation_id, "worker", lease_seconds=30)
        snapshot = self.store.recovery_snapshot_for_task(
            self.spec.session_id,
            self.spec.task_id,
        )
        receipt = receipt_for(self.binding, snapshot)
        object.__setattr__(receipt, "stream_sequence", 0)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "stream_sequence"):
            coordinator.assess(TaskStatus.RUNNING, self.binding, receipt)
        self.assertIsNotNone(lease)
        self.assertEqual(fake.reads, 0)

    def test_malformed_store_result_is_an_integrity_error(self) -> None:
        fake = FakeRecoveryStore("not-a-snapshot")
        coordinator = InvocationRecoveryCoordinator(fake)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "snapshot"):
            coordinator.assess(TaskStatus.RUNNING, self.binding)
        self.assertEqual(fake.reads, 1)

    def test_constructor_requires_explicit_valid_ownership(self) -> None:
        with self.assertRaisesRegex(TypeError, "owns_store"):
            InvocationRecoveryCoordinator(owns_store=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "requires"):
            InvocationRecoveryCoordinator(owns_store=True)
        with self.assertRaisesRegex(TypeError, "must provide"):
            InvocationRecoveryCoordinator(object())  # type: ignore[arg-type]

    def test_recovery_contract_is_available_from_the_public_package(self) -> None:
        expected = {
            "InvocationBinding": InvocationBinding,
            "InvocationRecoveryClosedError": InvocationRecoveryClosedError,
            "InvocationRecoveryCoordinator": InvocationRecoveryCoordinator,
            "InvocationRecoveryDecision": InvocationRecoveryDecision,
            "InvocationRecoveryIntegrityError": InvocationRecoveryIntegrityError,
            "InvocationRecoverySnapshot": InvocationRecoverySnapshot,
            "InvocationResultReceipt": InvocationResultReceipt,
            "assess_invocation_recovery": assess_invocation_recovery,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, quantum_entanglement.__all__)
                self.assertIs(getattr(quantum_entanglement, name), value)


if __name__ == "__main__":
    unittest.main()
