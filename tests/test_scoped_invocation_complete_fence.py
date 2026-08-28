from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from quantum_entanglement.attempts import (
    InvocationCompletionPathReservedError,
    InvocationIntegrityError,
    InvocationJobSpec,
    InvocationLease,
    InvocationStatus,
    SQLiteInvocationAttemptStore,
)
from quantum_entanglement.invocation_execution import (
    InvocationStartClaimed,
    ScopedInvocationStartClaimedV3,
)
from quantum_entanglement.store import SQLiteEventStore
from tests.test_scoped_task_invocation_admission import (
    CLAIMED_AT,
    STORE_TIME,
    legacy_request,
    scoped_request,
)

AFTER_EXPIRY = "2026-08-27T10:02:00.000000Z"


class CountingClock:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self.value


def durable_completion_state(
    store: SQLiteInvocationAttemptStore,
    invocation_id: str,
) -> tuple[tuple[Any, ...], tuple[tuple[Any, ...], ...]]:
    job = store._connection.execute(
        "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()
    attempts = store._connection.execute(
        """
        SELECT * FROM invocation_attempts
        WHERE invocation_id = ? ORDER BY attempt_number
        """,
        (invocation_id,),
    ).fetchall()
    if job is None:
        raise AssertionError("fixture job is missing")
    return tuple(job), tuple(tuple(row) for row in attempts)


class ScopedInvocationCompleteFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.events = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)

    def tearDown(self) -> None:
        self.events.close()
        self.tempdir.cleanup()

    def scoped_event_store_claim(self) -> tuple[object, ScopedInvocationStartClaimedV3]:
        request = scoped_request()
        self.events.append_scoped_task_invocation_admission_v2(request, expected_version=0)
        self.events._clock = lambda: CLAIMED_AT
        result = self.events.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            "worker-scoped-complete-fence",
            lease_seconds=60,
            expected_version=2,
        )
        self.assertIs(type(result), ScopedInvocationStartClaimedV3)
        return request, cast(ScopedInvocationStartClaimedV3, result)

    def assert_scoped_complete_rejected(
        self,
        lease: InvocationLease,
        *,
        expected_error: type[Exception] = InvocationCompletionPathReservedError,
        clock_value: str = AFTER_EXPIRY,
    ) -> None:
        clock = CountingClock(clock_value)
        with SQLiteInvocationAttemptStore(self.path, clock=clock) as attempts:
            clock.calls = 0
            before = durable_completion_state(attempts, lease.invocation_id)
            total_changes = attempts._connection.total_changes
            statements: list[str] = []
            attempts._connection.set_trace_callback(statements.append)
            try:
                with self.assertRaises(expected_error) as raised:
                    attempts.complete(
                        lease,
                        result_ref="result-ref-secret-canary",
                    )
            finally:
                attempts._connection.set_trace_callback(None)
            self.assertIs(type(raised.exception), expected_error)
            self.assertEqual(clock.calls, 0)
            self.assertEqual(attempts._connection.total_changes, total_changes)
            self.assertEqual(
                durable_completion_state(attempts, lease.invocation_id),
                before,
            )
            self.assertFalse(attempts._connection.in_transaction)
            self.assertFalse(
                any(
                    statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
                    for statement in statements
                ),
                statements,
            )
            rendered = repr(raised.exception) + str(raised.exception)
            self.assertNotIn("result-ref-secret-canary", rendered)
            self.assertNotIn(lease.lease_token, rendered)

    def test_event_store_scoped_claim_cannot_use_standalone_complete(self) -> None:
        request, claimed = self.scoped_event_store_claim()

        self.assert_scoped_complete_rejected(claimed.lease)

        result_events = self.events._connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            ("task.invocation.result.accepted",),
        ).fetchone()[0]
        self.assertEqual(result_events, 0)
        row = self.events._connection.execute(
            "SELECT status, result_ref FROM invocation_jobs WHERE invocation_id = ?",
            (request.manifest.invocation_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("running", None))

    def test_standalone_scoped_claim_cannot_use_standalone_complete(self) -> None:
        request = scoped_request()
        self.events.append_scoped_task_invocation_admission_v2(request, expected_version=0)
        clock = CountingClock(CLAIMED_AT)
        with SQLiteInvocationAttemptStore(self.path, clock=clock) as attempts:
            lease = attempts.claim(
                request.manifest.invocation_id,
                "worker-standalone-claim",
                lease_seconds=60,
            )
        self.assertIs(type(lease), InvocationLease)

        self.assert_scoped_complete_rejected(cast(InvocationLease, lease))

    def test_missing_admission_receipt_does_not_downgrade_scoped_event(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        self.events._connection.execute(
            "DELETE FROM invocation_admissions WHERE invocation_id = ?",
            (request.manifest.invocation_id,),
        )

        self.assert_scoped_complete_rejected(claimed.lease)

    def test_drifted_type_and_key_remain_bound_by_scoped_payload_identity(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        self.events._connection.execute(
            "DELETE FROM invocation_admissions WHERE invocation_id = ?",
            (request.manifest.invocation_id,),
        )
        self.events._connection.execute(
            """
            UPDATE events SET event_type = ?, idempotency_key = ?
            WHERE event_id = ?
            """,
            (
                "task.execution.requested.drift",
                "execution-request-drift",
                request.execution_requested_event_id,
            ),
        )

        self.assert_scoped_complete_rejected(
            claimed.lease,
            expected_error=InvocationIntegrityError,
            clock_value=CLAIMED_AT,
        )

    def test_receipt_digest_drift_fails_as_integrity_not_legacy(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        self.events._connection.execute(
            """
            UPDATE invocation_admissions SET event_manifest_sha256 = ?
            WHERE invocation_id = ?
            """,
            ("f" * 64, request.manifest.invocation_id),
        )

        self.assert_scoped_complete_rejected(
            claimed.lease,
            expected_error=InvocationIntegrityError,
        )

    def test_scoped_payload_shape_drift_fails_as_integrity_not_legacy(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        payload = request.manifest.to_dict()
        del payload["tenantId"]
        self.events._connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            (
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                request.execution_requested_event_id,
            ),
        )

        self.assert_scoped_complete_rejected(
            claimed.lease,
            expected_error=InvocationIntegrityError,
        )

    def test_stripped_scoped_markers_cannot_downgrade_to_legacy(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        payload = request.manifest.to_dict()
        for key in ("schemaVersion", "tenantId", "workspaceId"):
            del payload[key]
        self.events._connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            (
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                request.execution_requested_event_id,
            ),
        )

        self.assert_scoped_complete_rejected(
            claimed.lease,
            expected_error=InvocationIntegrityError,
        )

    def test_scoped_event_type_drift_fails_as_integrity_not_legacy(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        self.events._connection.execute(
            "UPDATE events SET event_type = ? WHERE event_id = ?",
            ("task.execution.requested.drift", request.execution_requested_event_id),
        )

        self.assert_scoped_complete_rejected(
            claimed.lease,
            expected_error=InvocationIntegrityError,
        )

    def test_scoped_job_digest_drift_fails_as_integrity_not_legacy(self) -> None:
        request, claimed = self.scoped_event_store_claim()
        self.events._connection.execute(
            "UPDATE invocation_jobs SET payload_digest = ? WHERE invocation_id = ?",
            ("f" * 64, request.manifest.invocation_id),
        )

        self.assert_scoped_complete_rejected(
            claimed.lease,
            expected_error=InvocationIntegrityError,
        )

    def test_legacy_canonical_admission_can_still_complete(self) -> None:
        request = legacy_request()
        self.events.append_task_invocation_admission(request, expected_version=0)
        self.events._clock = lambda: CLAIMED_AT
        result = self.events.claim_invocation_start(
            request.manifest.invocation_id,
            "worker-legacy-complete",
            lease_seconds=60,
            expected_version=2,
        )
        self.assertIs(type(result), InvocationStartClaimed)
        claimed = cast(InvocationStartClaimed, result)
        with SQLiteInvocationAttemptStore(self.path, clock=lambda: CLAIMED_AT) as attempts:
            self.assertTrue(attempts.complete(claimed.lease, result_ref="result:legacy"))
            job = attempts.get(request.manifest.invocation_id)
            self.assertIsNotNone(job)
            self.assertIs(cast(Any, job).status, InvocationStatus.SUCCEEDED)

    def test_unrelated_scoped_event_does_not_block_legacy_completion(self) -> None:
        scoped = scoped_request()
        legacy = legacy_request()
        self.events.append_scoped_task_invocation_admission_v2(scoped, expected_version=0)
        self.events.append_task_invocation_admission(legacy, expected_version=0)
        self.events._clock = lambda: CLAIMED_AT
        result = self.events.claim_invocation_start(
            legacy.manifest.invocation_id,
            "worker-unrelated-scoped",
            lease_seconds=60,
            expected_version=2,
        )
        claimed = cast(InvocationStartClaimed, result)
        with SQLiteInvocationAttemptStore(self.path, clock=lambda: CLAIMED_AT) as attempts:
            self.assertTrue(attempts.complete(claimed.lease))

    def test_attempt_only_shape_is_not_classified_by_heuristics(self) -> None:
        request = scoped_request()
        clock = CountingClock(CLAIMED_AT)
        with SQLiteInvocationAttemptStore(":memory:", clock=clock) as attempts:
            spec = InvocationJobSpec(
                invocation_id="invocation-attempt-only",
                session_id="session-attempt-only",
                plan_id="plan-attempt-only",
                task_id="task-attempt-only",
                agent_id="agent-attempt-only",
                idempotency_key="invoke:task-attempt-only",
                payload_digest=request.manifest.canonical_digest(),
                max_attempts=1,
            )
            attempts.enqueue(spec)
            lease = attempts.claim(
                spec.invocation_id,
                "worker-attempt-only",
                lease_seconds=60,
            )
            self.assertIs(type(lease), InvocationLease)
            self.assertTrue(
                attempts.complete(
                    cast(InvocationLease, lease),
                    result_ref="result:attempt-only",
                )
            )


if __name__ == "__main__":
    unittest.main()
