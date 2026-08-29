from __future__ import annotations

import copy
import pickle
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement
from quantum_entanglement._result_artifact_transaction import _ResultArtifactCommitAmbiguityError
from quantum_entanglement.invocation_results import (
    ScopedInvocationResultAcceptedV2,
    ScopedInvocationResultObservedV2,
)
from quantum_entanglement.store import (
    ResultAcceptanceDisabledError,
    SQLiteEventStore,
)
from tests.test_result_acceptance_durable_prerequisites import (
    ResultAcceptanceDurablePrerequisiteTests,
)


class ResultAcceptanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteEventStore(
            ":memory:",
            clock=lambda: "2026-08-27T10:00:00.000000Z",
            enable_result_acceptance_schema=True,
        )

    def tearDown(self) -> None:
        self.store.close()

    def fresh_prepared(self):
        helper = ResultAcceptanceDurablePrerequisiteTests(methodName="runTest")
        helper.store = self.store
        return helper.fresh_prepared()

    def test_disabled_store_rejects_before_preparing_caller_inputs(self) -> None:
        disabled = SQLiteEventStore(":memory:")
        try:
            with self.assertRaises(ResultAcceptanceDisabledError):
                disabled.accept_scoped_invocation_result_v2(object(), object())
        finally:
            disabled.close()

    def test_fresh_commit_returns_process_bound_accepted_proof(self) -> None:
        prepared = self.fresh_prepared()
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=("receipt-api-1", "event-result-api-1", "event-terminal-api-1"),
        ):
            accepted = self.store.accept_scoped_invocation_result_v2(
                prepared.request,
                prepared.claimed,
            )

        self.assertIs(type(accepted), ScopedInvocationResultAcceptedV2)
        assert type(accepted) is ScopedInvocationResultAcceptedV2
        self.assertEqual(accepted.receipt.receipt_id, "receipt-api-1")
        self.assertNotIn(prepared.claimed.lease.lease_token, repr(accepted))
        self.assertNotIn(prepared.claimed.lease.lease_token, repr(accepted.receipt))
        self.assertIs(quantum_entanglement.ScopedInvocationResultAcceptedV2, type(accepted))
        for operation in (
            lambda: copy.copy(accepted),
            lambda: copy.deepcopy(accepted),
            lambda: pickle.dumps(accepted),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(TypeError, "cannot be (copied|serialized)"):
                    operation()

    def test_replay_returns_observed_and_never_upgrades_to_accepted(self) -> None:
        prepared = self.fresh_prepared()
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt-api-replay",
                "event-result-api-replay",
                "event-terminal-api-replay",
            ),
        ):
            first = self.store.accept_scoped_invocation_result_v2(
                prepared.request,
                prepared.claimed,
            )
        replay = self.store.accept_scoped_invocation_result_v2(
            prepared.request,
            prepared.claimed,
        )

        self.assertIs(type(first), ScopedInvocationResultAcceptedV2)
        self.assertIs(type(replay), ScopedInvocationResultObservedV2)
        assert type(first) is ScopedInvocationResultAcceptedV2
        assert type(replay) is ScopedInvocationResultObservedV2
        self.assertEqual(first.receipt, replay.receipt)

    def test_public_ack_loss_requires_reopen_and_returns_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "event-store.sqlite3")
            store = SQLiteEventStore(
                path,
                clock=lambda: "2026-08-27T10:00:00.000000Z",
                enable_result_acceptance_schema=True,
            )
            try:
                helper = ResultAcceptanceDurablePrerequisiteTests(methodName="runTest")
                helper.store = store
                prepared = helper.fresh_prepared()
                store._clock = lambda: "2026-08-27T10:00:02.000000Z"
                original_control = store._execute_transaction_control

                def commit_then_raise(connection: sqlite3.Connection, statement: str) -> None:
                    original_control(connection, statement)
                    if statement == "COMMIT":
                        raise RuntimeError("private result commit acknowledgement lost")

                with patch.object(
                    store,
                    "_execute_transaction_control",
                    side_effect=commit_then_raise,
                ):
                    with self.assertRaises(_ResultArtifactCommitAmbiguityError):
                        store.accept_scoped_invocation_result_v2(
                            prepared.request,
                            prepared.claimed,
                        )
                self.assertTrue(store._poisoned)
            finally:
                store.close()

            reopened = SQLiteEventStore(
                path,
                clock=lambda: "2026-08-27T10:00:03.000000Z",
                enable_result_acceptance_schema=True,
            )
            try:
                observed = reopened.read_scoped_invocation_result_observed_v2(
                    prepared.request.manifest.tenant_id,
                    prepared.request.manifest.workspace_id,
                    prepared.request.manifest.invocation_id,
                )
                replay = reopened.accept_scoped_invocation_result_v2(
                    prepared.request,
                    prepared.claimed,
                )
            finally:
                reopened.close()
            self.assertIs(type(observed), ScopedInvocationResultObservedV2)
            self.assertIs(type(replay), ScopedInvocationResultObservedV2)
            assert type(observed) is ScopedInvocationResultObservedV2
            assert type(replay) is ScopedInvocationResultObservedV2
            self.assertEqual(observed.receipt, replay.receipt)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
