from __future__ import annotations

import unittest
from dataclasses import replace

from quantum_entanglement.attempts import (
    AttemptStatus,
    InvocationAttempt,
    InvocationJob,
    InvocationRecoverySnapshot,
    InvocationStatus,
)
from quantum_entanglement.invocation_recovery import (
    InvocationRecoveryDecision,
    InvocationRecoveryIntegrityError,
    ScopedInvocationBinding,
    assess_scoped_invocation_recovery,
)
from quantum_entanglement.invocation_results import ScopedInvocationResultObservedV2
from quantum_entanglement.protocol import TaskStatus
from tests.test_invocation_result_receipt import receipt_for

CLAIMED_AT = "2026-08-27T09:00:01.000000Z"
HEARTBEAT_AT = "2026-08-27T12:34:55.123456Z"
FINISHED_AT = "2026-08-27T12:34:56.123456Z"
LEASE_EXPIRES_AT = "2026-08-27T13:00:00.000000Z"


def _binding_and_observed() -> tuple[ScopedInvocationBinding, ScopedInvocationResultObservedV2]:
    observed = ScopedInvocationResultObservedV2(receipt_for())
    evidence = observed.receipt.evidence
    return (
        ScopedInvocationBinding(
            tenant_id=evidence.tenant_id,
            workspace_id=evidence.workspace_id,
            invocation_id=evidence.invocation_id,
            session_id=evidence.session_id,
            plan_id=evidence.plan_id,
            task_id=evidence.task_id,
            agent_id=evidence.agent_id,
            idempotency_key=evidence.job_idempotency_key,
            payload_digest=evidence.execution_manifest_digest,
        ),
        observed,
    )


def _snapshot(
    binding: ScopedInvocationBinding,
    observed: ScopedInvocationResultObservedV2,
    *,
    job_status: InvocationStatus,
    attempt_status: AttemptStatus,
    result_ref: str | None = None,
) -> InvocationRecoverySnapshot:
    evidence = observed.receipt.evidence
    result_ref = evidence.result_ref if result_ref is None else result_ref
    job_finished = FINISHED_AT if job_status is InvocationStatus.SUCCEEDED else None
    job = InvocationJob(
        invocation_id=binding.invocation_id,
        session_id=binding.session_id,
        plan_id=binding.plan_id,
        task_id=binding.task_id,
        agent_id=binding.agent_id,
        idempotency_key=binding.idempotency_key,
        payload_digest=binding.payload_digest,
        priority=50,
        status=job_status,
        max_attempts=1,
        attempts_started=1,
        lease_epoch=evidence.lease_epoch,
        requested_available_at=None,
        available_at=CLAIMED_AT,
        created_at=CLAIMED_AT,
        updated_at=FINISHED_AT if job_status is InvocationStatus.SUCCEEDED else HEARTBEAT_AT,
        lease_owner="worker-scoped-1" if job_status is InvocationStatus.RUNNING else None,
        lease_token_digest=evidence.lease_token_digest
        if job_status is InvocationStatus.RUNNING
        else None,
        lease_expires_at=LEASE_EXPIRES_AT if job_status is InvocationStatus.RUNNING else None,
        heartbeat_at=HEARTBEAT_AT if job_status is InvocationStatus.RUNNING else None,
        result_ref=result_ref if job_status is InvocationStatus.SUCCEEDED else None,
        last_error=None,
        finished_at=job_finished,
    )
    attempt = InvocationAttempt(
        attempt_id=evidence.attempt_id,
        invocation_id=binding.invocation_id,
        attempt_number=evidence.attempt_number,
        lease_epoch=evidence.lease_epoch,
        worker_id=evidence.worker_id,
        lease_token_digest=evidence.lease_token_digest,
        status=attempt_status,
        started_at=CLAIMED_AT,
        heartbeat_at=HEARTBEAT_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        finished_at=FINISHED_AT if attempt_status is AttemptStatus.SUCCEEDED else None,
        error=None,
        result_ref=result_ref if attempt_status is AttemptStatus.SUCCEEDED else None,
    )
    return InvocationRecoverySnapshot(job, attempt, 1)


class ScopedInvocationRecoveryTests(unittest.TestCase):
    def test_missing_observation_preserves_blocked_legacy_decision(self) -> None:
        binding, observed = _binding_and_observed()
        snapshot = _snapshot(
            binding,
            observed,
            job_status=InvocationStatus.RUNNING,
            attempt_status=AttemptStatus.RUNNING,
        )
        self.assertEqual(
            assess_scoped_invocation_recovery(TaskStatus.RUNNING, binding, snapshot),
            InvocationRecoveryDecision.WAITING_ACTIVE_LEASE,
        )

    def test_running_attempt_with_durable_observation_is_reconcile_ready(self) -> None:
        binding, observed = _binding_and_observed()
        snapshot = _snapshot(
            binding,
            observed,
            job_status=InvocationStatus.RUNNING,
            attempt_status=AttemptStatus.RUNNING,
        )
        before = (snapshot, observed)
        self.assertEqual(
            assess_scoped_invocation_recovery(TaskStatus.RUNNING, binding, snapshot, observed),
            InvocationRecoveryDecision.RECEIPT_RECONCILIATION_READY,
        )
        self.assertEqual((snapshot, observed), before)

    def test_matching_succeeded_projection_is_already_projected(self) -> None:
        binding, observed = _binding_and_observed()
        snapshot = _snapshot(
            binding,
            observed,
            job_status=InvocationStatus.SUCCEEDED,
            attempt_status=AttemptStatus.SUCCEEDED,
        )
        self.assertEqual(
            assess_scoped_invocation_recovery(TaskStatus.RUNNING, binding, snapshot, observed),
            InvocationRecoveryDecision.RESULT_ALREADY_PROJECTED,
        )

    def test_observation_is_rejected_when_scope_or_attempt_drifts(self) -> None:
        binding, observed = _binding_and_observed()
        snapshot = _snapshot(
            binding,
            observed,
            job_status=InvocationStatus.RUNNING,
            attempt_status=AttemptStatus.RUNNING,
        )
        for forged in (
            replace(binding, tenant_id="tenant-other"),
            replace(binding, payload_digest="0" * 64),
        ):
            with self.subTest(forged=forged):
                with self.assertRaisesRegex(
                    InvocationRecoveryIntegrityError,
                    "(scoped binding|committed binding)",
                ):
                    assess_scoped_invocation_recovery(
                        TaskStatus.RUNNING,
                        forged,
                        snapshot,
                        observed,
                    )
        forged_attempt = replace(snapshot.current_attempt, attempt_id="attempt-other")
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "current attempt"):
            assess_scoped_invocation_recovery(
                TaskStatus.RUNNING,
                binding,
                replace(snapshot, current_attempt=forged_attempt),
                observed,
            )

    def test_observation_never_uses_plaintext_lease_or_accepts_orphan_job(self) -> None:
        binding, observed = _binding_and_observed()
        orphan = InvocationRecoverySnapshot(None, None, 0)
        with self.assertRaisesRegex(InvocationRecoveryIntegrityError, "owning attempt"):
            assess_scoped_invocation_recovery(TaskStatus.RUNNING, binding, orphan, observed)
        self.assertNotIn("scoped-start-secret-lease-canary", repr(observed))
        self.assertNotIn("leaseToken", observed.to_dict())


if __name__ == "__main__":
    unittest.main()
