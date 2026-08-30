from __future__ import annotations

import unittest

from quantum_entanglement.attempts import AttemptStatus, InvocationStatus
from quantum_entanglement.invocation_execution import (
    ScopedInvocationStartClaimedV3,
)
from quantum_entanglement.store import SQLiteEventStore
from tests.test_scoped_task_invocation_admission import (
    CLAIMED_AT,
    STORE_TIME,
    scoped_request,
)


class ScopedLeaseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteEventStore(":memory:", clock=lambda: STORE_TIME)
        request = scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(request, expected_version=0)
        self.store._clock = lambda: CLAIMED_AT
        claimed = self.store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-lifecycle-1",
            lease_seconds=1,
            expected_version=2,
        )
        self.assertIs(type(claimed), ScopedInvocationStartClaimedV3)
        assert type(claimed) is ScopedInvocationStartClaimedV3
        self.request = request
        self.claimed = claimed

    def tearDown(self) -> None:
        self.store.close()

    def test_heartbeat_advances_job_and_attempt_without_rewriting_start_event(self) -> None:
        stream_version = self.store.stream_version(self.claimed.receipt.stream_id)
        self.store._clock = lambda: "2026-08-27T10:00:01.500000Z"

        self.assertTrue(
            self.store.heartbeat_scoped_invocation_start_v3(
                self.claimed,
                lease_seconds=5,
            )
        )

        snapshot = self.store.recovery_snapshot_for_task(
            self.request.manifest.session_id,
            self.request.manifest.task_id,
        )
        self.assertIsNotNone(snapshot.job)
        self.assertIsNotNone(snapshot.current_attempt)
        assert snapshot.job is not None
        assert snapshot.current_attempt is not None
        self.assertEqual(snapshot.job.status, InvocationStatus.RUNNING)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.RUNNING)
        self.assertEqual(snapshot.job.heartbeat_at, "2026-08-27T10:00:01.500000Z")
        self.assertEqual(snapshot.current_attempt.heartbeat_at, snapshot.job.heartbeat_at)
        self.assertEqual(snapshot.current_attempt.lease_expires_at, snapshot.job.lease_expires_at)
        self.assertGreater(snapshot.job.lease_expires_at, self.claimed.lease.lease_expires_at)
        self.assertEqual(self.store.stream_version(self.claimed.receipt.stream_id), stream_version)
        observed = self.store.read_scoped_invocation_start_v3(
            self.request.manifest.tenant_id,
            self.request.manifest.workspace_id,
            self.request.manifest.invocation_id,
        )
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(observed.receipt, self.claimed.receipt)

    def test_exact_expiry_rejects_heartbeat_and_recovery_fences_both_rows(self) -> None:
        self.store._clock = lambda: self.claimed.lease.lease_expires_at
        before = self.store.stream_version(self.claimed.receipt.stream_id)
        self.assertFalse(
            self.store.heartbeat_scoped_invocation_start_v3(
                self.claimed,
                lease_seconds=5,
            )
        )
        self.assertEqual(self.store.stream_version(self.claimed.receipt.stream_id), before)
        running = self.store.recovery_snapshot_for_task(
            self.request.manifest.session_id,
            self.request.manifest.task_id,
        )
        self.assertIsNotNone(running.job)
        assert running.job is not None
        self.assertEqual(running.job.status, InvocationStatus.RUNNING)

        recovered = self.store.recover_expired_scoped_invocations()
        self.assertEqual(recovered.requeued, ())
        self.assertEqual(recovered.exhausted, (self.request.manifest.invocation_id,))
        terminal = self.store.recovery_snapshot_for_task(
            self.request.manifest.session_id,
            self.request.manifest.task_id,
        )
        self.assertIsNotNone(terminal.job)
        self.assertIsNotNone(terminal.current_attempt)
        assert terminal.job is not None
        assert terminal.current_attempt is not None
        self.assertEqual(terminal.job.status, InvocationStatus.FAILED)
        self.assertEqual(terminal.current_attempt.status, AttemptStatus.EXPIRED)
        self.assertEqual(
            terminal.job.last_error,
            "lease expired before terminal acknowledgement",
        )
        self.assertEqual(terminal.current_attempt.error, terminal.job.last_error)
        self.assertIsNone(terminal.job.lease_owner)
        self.assertIsNone(terminal.job.lease_token_digest)
        self.assertIsNone(terminal.job.lease_expires_at)
        self.assertIsNone(terminal.job.heartbeat_at)
        self.assertFalse(
            self.store.heartbeat_scoped_invocation_start_v3(
                self.claimed,
                lease_seconds=5,
            )
        )
        self.assertEqual(self.store.recover_expired_scoped_invocations().recovered_count, 0)

    def test_invalid_limit_and_claim_fail_before_durable_access(self) -> None:
        for value in (True, 0, 1_001, "1"):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.recover_expired_scoped_invocations(limit=value)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.heartbeat_scoped_invocation_start_v3(  # type: ignore[arg-type]
                object(),
                lease_seconds=5,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
