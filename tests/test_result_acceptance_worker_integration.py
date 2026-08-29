from __future__ import annotations

import unittest

import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites
import tests.test_scoped_task_invocation_admission as scoped_admission
from quantum_entanglement.invocation_worker import (
    HeartbeatPureWorkerGate,
    HeartbeatPureWorkerSupervisor,
    InvocationWorkerConfiguration,
    PureWorkerContext,
    PureWorkerOutcome,
)
from quantum_entanglement.store import SQLiteEventStore


class ResultAcceptanceWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_accepts_exact_request_through_store_owned_acceptor(self) -> None:
        store = SQLiteEventStore(
            ":memory:",
            clock=lambda: scoped_admission.STORE_TIME,
            enable_result_acceptance_schema=True,
        )
        helper = durable_prerequisites.ResultAcceptanceDurablePrerequisiteTests(
            methodName="runTest"
        )
        helper.store = store
        prepared = helper.fresh_prepared()
        execution_request = scoped_admission.scoped_request()
        worker_admission = HeartbeatPureWorkerGate.prepare_scoped_v3(
            prepared.claimed,
            execution_request.manifest,
            InvocationWorkerConfiguration(
                lease_seconds=0.30,
                heartbeat_interval_seconds=0.02,
                handler_timeout_seconds=0.08,
                drain_timeout_seconds=0.05,
            ),
            handler_revision=execution_request.manifest.runtime_revision,
        )
        heartbeats = 0

        async def heartbeat(_lease_seconds: float) -> bool:
            nonlocal heartbeats
            heartbeats += 1
            return True

        async def handler(context: PureWorkerContext) -> object:
            self.assertFalse(context.cancelled)
            return prepared.request

        async def acceptor(request: object, claim: object) -> object:
            self.assertIs(request, prepared.request)
            self.assertIs(type(claim), type(prepared.claimed))
            store._clock = lambda: "2026-08-27T10:00:02.000000Z"
            return store.accept_scoped_invocation_result_v2(request, claim)

        try:
            result = await HeartbeatPureWorkerSupervisor(
                worker_admission,
                heartbeat=heartbeat,
            ).run_and_accept(handler, acceptor=acceptor)
        finally:
            store.close()

        self.assertEqual(result.outcome, PureWorkerOutcome.ACCEPTED)
        self.assertIsNotNone(result.value)
        self.assertGreaterEqual(heartbeats, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
