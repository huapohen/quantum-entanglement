from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantum_entanglement.attempts import AttemptStatus, InvocationStatus
from quantum_entanglement.invocation_recovery import (
    InvocationRecoveryCoordinator,
    InvocationRecoveryDecision,
    ScopedInvocationBinding,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.store import SQLiteEventStore
from tests.test_scoped_task_invocation_admission import scoped_request

STORE_TIME = "2026-08-29T00:00:00Z"


class EventStoreRecoverySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "event-store.sqlite3")
        self.store = SQLiteEventStore(
            self.path,
            clock=lambda: STORE_TIME,
            enable_result_acceptance_schema=True,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_snapshot_and_scoped_coordinator_share_the_store_lifecycle(self) -> None:
        request = scoped_request()
        admission = self.store.append_scoped_task_invocation_admission_v2(
            request,
            expected_version=0,
        )
        claimed = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-recovery-snapshot",
            lease_seconds=60,
            expected_version=admission.events[-1].sequence,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        evidence = claimed.receipt.evidence
        snapshot = self.store.recovery_snapshot_for_task(
            evidence.session_id,
            evidence.task_id,
        )
        self.assertIsNotNone(snapshot.job)
        self.assertIsNotNone(snapshot.current_attempt)
        assert snapshot.job is not None
        assert snapshot.current_attempt is not None
        self.assertEqual(snapshot.job.status, InvocationStatus.RUNNING)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.RUNNING)
        self.assertEqual(snapshot.attempt_count, 1)
        self.assertEqual(snapshot.job.lease_epoch, evidence.lease_epoch)
        self.assertEqual(snapshot.current_attempt.attempt_id, evidence.attempt_id)

        coordinator = InvocationRecoveryCoordinator(
            self.store,
            result_store=self.store,
        )
        try:
            decision = coordinator.assess_scoped(
                task_status=TaskStatus.RUNNING,
                binding=ScopedInvocationBinding(
                    tenant_id=evidence.tenant_id,
                    workspace_id=evidence.workspace_id,
                    invocation_id=evidence.invocation_id,
                    session_id=evidence.session_id,
                    plan_id=evidence.plan_id,
                    task_id=evidence.task_id,
                    agent_id=evidence.agent_id,
                    idempotency_key=evidence.job_idempotency_key,
                    payload_digest=evidence.manifest_digest,
                ),
            )
        finally:
            coordinator.close()
        self.assertEqual(decision, InvocationRecoveryDecision.WAITING_ACTIVE_LEASE)

    def test_missing_job_is_a_normal_empty_snapshot(self) -> None:
        snapshot = self.store.recovery_snapshot_for_task(
            "session-missing",
            "task-missing",
        )
        self.assertEqual(snapshot.attempt_count, 0)
        self.assertIsNone(snapshot.job)
        self.assertIsNone(snapshot.current_attempt)


if __name__ == "__main__":
    unittest.main()
