from __future__ import annotations

import asyncio
import unittest

import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites
import tests.test_scoped_task_invocation_admission as scoped_admission
from quantum_entanglement.attempts import AttemptStatus, InvocationStatus
from quantum_entanglement.invocation_results import ScopedInvocationResultAcceptanceRequestV2
from quantum_entanglement.invocation_worker import (
    InvocationWorkerConfiguration,
    PureWorkerContext,
    PureWorkerOutcome,
)
from quantum_entanglement.invocation_worker_lifecycle import (
    PureWorkerLifecycleClosedError,
    PureWorkerLifecycleDrainingError,
    PureWorkerLifecycleState,
    ScopedPureWorkerLifecycle,
)
from quantum_entanglement.store import SQLiteEventStore


class ScopedPureWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = SQLiteEventStore(
            ":memory:",
            clock=lambda: "2026-08-27T10:00:00.000000Z",
            enable_result_acceptance_schema=True,
        )
        execution_request = scoped_admission.scoped_request()
        self.store.append_scoped_task_invocation_admission_v2(
            execution_request,
            expected_version=0,
        )
        self.store._clock = lambda: scoped_admission.CLAIMED_AT
        claimed = self.store.claim_scoped_invocation_start_v3(
            execution_request.manifest.tenant_id,
            execution_request.manifest.workspace_id,
            execution_request.manifest.invocation_id,
            "worker-scoped-lifecycle-1",
            lease_seconds=60,
            expected_version=2,
        )
        self.assertIs(type(claimed), scoped_admission.ScopedInvocationStartClaimedV3)
        assert type(claimed) is scoped_admission.ScopedInvocationStartClaimedV3
        self.claimed = claimed
        self.execution_request = execution_request
        self.result_request = durable_prerequisites.result_request_for_claim(claimed)
        self.lifecycle = ScopedPureWorkerLifecycle(self.store)

    async def asyncTearDown(self) -> None:
        await self.lifecycle.close()
        self.store.close()

    def configuration(self) -> InvocationWorkerConfiguration:
        return InvocationWorkerConfiguration(
            lease_seconds=0.30,
            heartbeat_interval_seconds=0.02,
            handler_timeout_seconds=0.10,
            drain_timeout_seconds=0.05,
        )

    async def test_store_heartbeat_and_acceptance_are_composed_as_one_run(self) -> None:
        heartbeats = 0

        async def handler(context: PureWorkerContext) -> object:
            self.assertFalse(context.cancelled)
            return self.result_request

        async def acceptor(
            request: ScopedInvocationResultAcceptanceRequestV2,
            claim: object,
        ) -> object:
            nonlocal heartbeats
            self.assertIs(request, self.result_request)
            # The lifecycle intentionally snapshots capabilities before admission;
            # acceptance receives an equal, exact-typed claim rather than the
            # caller's object identity.
            self.assertIs(type(claim), type(self.claimed))
            self.assertEqual(claim, self.claimed)
            self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
            return self.store.accept_scoped_invocation_result_v2(request, claim)

        original = self.store.heartbeat_scoped_invocation_start_v3

        def heartbeat(claim: object, *, lease_seconds: float) -> bool:
            nonlocal heartbeats
            heartbeats += 1
            return original(claim, lease_seconds=lease_seconds)

        self.store.heartbeat_scoped_invocation_start_v3 = heartbeat  # type: ignore[method-assign]
        result = await self.lifecycle.run_and_accept(
            self.claimed,
            self.execution_request.manifest,
            self.configuration(),
            handler,
            acceptor=acceptor,
        )
        self.assertEqual(result.outcome, PureWorkerOutcome.ACCEPTED)
        self.assertGreaterEqual(heartbeats, 1)
        snapshot = self.store.recovery_snapshot_for_task(
            self.execution_request.manifest.session_id,
            self.execution_request.manifest.task_id,
        )
        self.assertIsNotNone(snapshot.job)
        assert snapshot.job is not None
        self.assertEqual(snapshot.job.status, InvocationStatus.SUCCEEDED)

    async def test_close_stops_admission_cancels_and_relinquishes_active_run(self) -> None:
        started = asyncio.Event()
        canceled = asyncio.Event()

        async def handler(context: PureWorkerContext) -> object:
            started.set()
            await context.wait_cancelled()
            canceled.set()
            return self.result_request

        async def acceptor(
            _request: ScopedInvocationResultAcceptanceRequestV2,
            _claim: object,
        ) -> object:
            self.fail("canceled handler must never enter result acceptance")

        running = asyncio.create_task(
            self.lifecycle.run_and_accept(
                self.claimed,
                self.execution_request.manifest,
                self.configuration(),
                handler,
                acceptor=acceptor,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        closing = await self.lifecycle.close(timeout_seconds=0.5)
        result = await running
        self.assertEqual(result.outcome, PureWorkerOutcome.CANCELED)
        self.assertTrue(result.drained)
        self.assertTrue(canceled.is_set())
        self.assertEqual(closing.state, PureWorkerLifecycleState.CLOSED)
        self.assertEqual(closing.active_runs, 0)
        snapshot = self.store.recovery_snapshot_for_task(
            self.execution_request.manifest.session_id,
            self.execution_request.manifest.task_id,
        )
        self.assertIsNotNone(snapshot.job)
        self.assertIsNotNone(snapshot.current_attempt)
        assert snapshot.job is not None
        assert snapshot.current_attempt is not None
        self.assertEqual(snapshot.job.status, InvocationStatus.FAILED)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.EXPIRED)

    async def test_close_uses_hard_cancellation_after_bounded_drain(self) -> None:
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()

        async def handler(context: PureWorkerContext) -> object:
            started.set()
            await context.wait_cancelled()
            cancellation_seen.set()
            # Keep the handler alive past the drain window.  The supervisor must
            # hard-cancel this isolated pure coroutine and return a sanitized result.
            await asyncio.sleep(10)
            return self.result_request

        async def acceptor(
            _request: ScopedInvocationResultAcceptanceRequestV2,
            _claim: object,
        ) -> object:
            self.fail("hard-canceled handler must never enter result acceptance")

        running = asyncio.create_task(
            self.lifecycle.run_and_accept(
                self.claimed,
                self.execution_request.manifest,
                self.configuration(),
                handler,
                acceptor=acceptor,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        closing = await self.lifecycle.close(timeout_seconds=0.5)
        result = await running

        self.assertEqual(result.outcome, PureWorkerOutcome.CANCELED)
        self.assertFalse(result.drained)
        self.assertTrue(cancellation_seen.is_set())
        self.assertEqual(closing.state, PureWorkerLifecycleState.CLOSED)
        snapshot = self.store.recovery_snapshot_for_task(
            self.execution_request.manifest.session_id,
            self.execution_request.manifest.task_id,
        )
        self.assertIsNotNone(snapshot.job)
        self.assertIsNotNone(snapshot.current_attempt)
        assert snapshot.job is not None
        assert snapshot.current_attempt is not None
        self.assertEqual(snapshot.job.status, InvocationStatus.FAILED)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.EXPIRED)

    async def test_heartbeat_loss_cancels_handler_and_relinquishes_without_acceptance(self) -> None:
        started = asyncio.Event()
        canceled = asyncio.Event()
        heartbeat_calls = 0

        async def handler(context: PureWorkerContext) -> object:
            started.set()
            await context.wait_cancelled()
            canceled.set()
            return self.result_request

        async def acceptor(
            _request: ScopedInvocationResultAcceptanceRequestV2,
            _claim: object,
        ) -> object:
            self.fail("heartbeat loss must never enter result acceptance")

        original = self.store.heartbeat_scoped_invocation_start_v3

        def heartbeat(claim: object, *, lease_seconds: float) -> bool:
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            if heartbeat_calls == 1:
                return original(claim, lease_seconds=lease_seconds)
            return False

        self.store.heartbeat_scoped_invocation_start_v3 = heartbeat  # type: ignore[method-assign]
        running = asyncio.create_task(
            self.lifecycle.run_and_accept(
                self.claimed,
                self.execution_request.manifest,
                self.configuration(),
                handler,
                acceptor=acceptor,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        result = await asyncio.wait_for(running, timeout=1)

        self.assertEqual(result.outcome, PureWorkerOutcome.LEASE_LOST)
        self.assertTrue(result.drained)
        self.assertTrue(canceled.is_set())
        self.assertGreaterEqual(heartbeat_calls, 2)
        snapshot = self.store.recovery_snapshot_for_task(
            self.execution_request.manifest.session_id,
            self.execution_request.manifest.task_id,
        )
        self.assertIsNotNone(snapshot.job)
        self.assertIsNotNone(snapshot.current_attempt)
        assert snapshot.job is not None
        assert snapshot.current_attempt is not None
        self.assertEqual(snapshot.job.status, InvocationStatus.FAILED)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.EXPIRED)

    async def test_close_is_monotonic_and_new_admission_is_rejected(self) -> None:
        self.assertEqual(
            (await self.lifecycle.snapshot()).state,
            PureWorkerLifecycleState.ACCEPTING,
        )
        await self.lifecycle.close(timeout_seconds=0.1)
        self.assertEqual((await self.lifecycle.snapshot()).state, PureWorkerLifecycleState.CLOSED)

        async def handler(_context: PureWorkerContext) -> object:
            return self.result_request

        async def acceptor(
            _request: ScopedInvocationResultAcceptanceRequestV2,
            _claim: object,
        ) -> object:
            return object()

        with self.assertRaises(PureWorkerLifecycleClosedError):
            await self.lifecycle.run_and_accept(
                self.claimed,
                self.execution_request.manifest,
                self.configuration(),
                handler,
                acceptor=acceptor,
            )

        draining = ScopedPureWorkerLifecycle(self.store)
        async with draining._state_lock:
            draining._state = PureWorkerLifecycleState.DRAINING
        try:
            with self.assertRaises(PureWorkerLifecycleDrainingError):
                await draining.run_and_accept(
                    self.claimed,
                    self.execution_request.manifest,
                    self.configuration(),
                    handler,
                    acceptor=acceptor,
                )
        finally:
            await draining.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
