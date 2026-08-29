from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from quantum_entanglement.store import (
    ResultReconciliationConflictError,
    ResultReconciliationOutcome,
    SQLiteEventStore,
)
from tests.test_result_acceptance_durable_prerequisites import (
    ResultAcceptanceDurablePrerequisiteTests,
)


class ResultReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reconciliation is an explicit migration-7 opt-in API.  The fixture must
        # exercise the same activation gate as a production caller instead of merely
        # installing the candidate tables behind a disabled feature flag.
        self.store = SQLiteEventStore(
            ":memory:",
            clock=lambda: "2026-08-27T10:00:00Z",
            enable_result_acceptance_schema=True,
        )

    def tearDown(self) -> None:
        self.store.close()

    def _persist_running_graph(self) -> tuple[str, str, str]:
        """Use the private candidate writer to model a crash after graph commit."""

        helper = ResultAcceptanceDurablePrerequisiteTests(methodName="runTest")
        helper.store = self.store
        prepared = helper.fresh_prepared()
        self.store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        generated = (
            "receipt_reconcile_1",
            "event_result_reconcile_1",
            "event_terminal_reconcile_1",
        )
        with patch("quantum_entanglement.store.new_id", side_effect=generated):
            with self.store._result_artifact_transaction() as handle:
                with self.store._persist_result_acceptance_graph_in_owner_transaction(
                    handle,
                    prepared,
                ) as persisted:
                    self.assertEqual(
                        persisted.__class__.__name__,
                        "_PersistedFreshResultAcceptancePlanV2",
                    )
        return (
            prepared.request.manifest.tenant_id,
            prepared.request.manifest.workspace_id,
            prepared.request.manifest.invocation_id,
        )

    def test_reconciles_committed_graph_to_running_owner_without_emitting(self) -> None:
        tenant_id, workspace_id, invocation_id = self._persist_running_graph()
        before_events = self.store._connection.execute("SELECT count(*) FROM events").fetchone()[0]
        before_outbox = self.store._connection.execute("SELECT count(*) FROM outbox").fetchone()[0]

        result = self.store.reconcile_scoped_invocation_result(
            tenant_id,
            workspace_id,
            invocation_id,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIs(result.outcome, ResultReconciliationOutcome.RECONCILED)
        self.assertEqual(result.observed.receipt.evidence.invocation_id, invocation_id)
        job = self.store._connection.execute(
            "SELECT status, result_ref, lease_owner, lease_token_digest, finished_at "
            "FROM invocation_jobs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result_ref"], result.observed.receipt.evidence.result_ref)
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(job["lease_token_digest"])
        self.assertEqual(job["finished_at"], result.observed.receipt.evidence.accepted_at)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status, result_ref FROM invocation_attempts WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()["status"],
            "succeeded",
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM events").fetchone()[0],
            before_events,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM outbox").fetchone()[0],
            before_outbox,
        )

        again = self.store.reconcile_scoped_invocation_result(
            tenant_id,
            workspace_id,
            invocation_id,
        )
        self.assertIsNotNone(again)
        assert again is not None
        self.assertIs(again.outcome, ResultReconciliationOutcome.ALREADY_RECONCILED)
        self.assertEqual(again.observed, result.observed)

    def test_reconciliation_returns_none_for_empty_scope(self) -> None:
        self.assertIsNone(
            self.store.reconcile_scoped_invocation_result(
                "tenant-empty",
                "workspace-empty",
                "invocation-empty",
            )
        )

    def test_reconciliation_rejects_stale_owner_without_mutation(self) -> None:
        tenant_id, workspace_id, invocation_id = self._persist_running_graph()
        self.store._connection.execute(
            "UPDATE invocation_jobs SET lease_token_digest = ? WHERE invocation_id = ?",
            ("f" * 64, invocation_id),
        )
        changed = tuple(
            tuple(self.store._connection.execute(f"SELECT * FROM {table}"))
            for table in ("invocation_jobs", "invocation_attempts")
        )
        with self.assertRaises(ResultReconciliationConflictError):
            self.store.reconcile_scoped_invocation_result(tenant_id, workspace_id, invocation_id)
        after = tuple(
            tuple(self.store._connection.execute(f"SELECT * FROM {table}"))
            for table in ("invocation_jobs", "invocation_attempts")
        )
        self.assertEqual(after, changed)

    def test_reconciliation_rolls_back_when_the_attempt_cas_loses_a_race(self) -> None:
        tenant_id, workspace_id, invocation_id = self._persist_running_graph()
        before_events = self.store._connection.execute("SELECT count(*) FROM events").fetchone()[0]
        before_outbox = self.store._connection.execute("SELECT count(*) FROM outbox").fetchone()[0]
        original = self.store._update_reconciliation_row_in_transaction

        def compete(
            connection: sqlite3.Connection,
            statement: str,
            parameters: tuple[object, ...],
            *,
            label: str,
        ) -> None:
            original(connection, statement, parameters, label=label)
            if label == "job terminal":
                # Model another writer winning the attempt CAS before the reconciler
                # reaches its second exact update.  The surrounding transaction must
                # roll back both the first CAS and this competing write.
                connection.execute(
                    """
                    UPDATE invocation_attempts
                    SET status = 'succeeded', finished_at = ?, result_ref = ?
                    WHERE invocation_id = ?
                    """,
                    (
                        "2026-08-27T10:00:02.000000Z",
                        "result:competing-writer",
                        invocation_id,
                    ),
                )

        with patch.object(
            self.store,
            "_update_reconciliation_row_in_transaction",
            side_effect=compete,
        ):
            with self.assertRaises(ResultReconciliationConflictError):
                self.store.reconcile_scoped_invocation_result(
                    tenant_id,
                    workspace_id,
                    invocation_id,
                )

        job = self.store._connection.execute(
            "SELECT status, result_ref FROM invocation_jobs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        attempt = self.store._connection.execute(
            "SELECT status, result_ref FROM invocation_attempts WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        self.assertEqual((job["status"], job["result_ref"]), ("running", None))
        self.assertEqual((attempt["status"], attempt["result_ref"]), ("running", None))
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM events").fetchone()[0],
            before_events,
        )
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM outbox").fetchone()[0],
            before_outbox,
        )

    def test_reconciliation_trigger_side_effect_rolls_back_the_complete_transaction(self) -> None:
        tenant_id, workspace_id, invocation_id = self._persist_running_graph()
        self.store._connection.executescript(
            """
            CREATE TABLE reconciliation_trigger_probe (value INTEGER NOT NULL);
            CREATE TRIGGER reconciliation_job_probe
            AFTER UPDATE OF status ON invocation_jobs
            BEGIN
                INSERT INTO reconciliation_trigger_probe(value) VALUES (1);
            END;
            """
        )
        with self.assertRaises(ResultReconciliationConflictError):
            self.store.reconcile_scoped_invocation_result(
                tenant_id,
                workspace_id,
                invocation_id,
            )
        job = self.store._connection.execute(
            "SELECT status, result_ref FROM invocation_jobs WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        attempt = self.store._connection.execute(
            "SELECT status, result_ref FROM invocation_attempts WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        self.assertEqual((job["status"], job["result_ref"]), ("running", None))
        self.assertEqual((attempt["status"], attempt["result_ref"]), ("running", None))
        self.assertEqual(
            self.store._connection.execute(
                "SELECT count(*) FROM reconciliation_trigger_probe"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
