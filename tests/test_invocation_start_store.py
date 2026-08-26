from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import quantum_entanglement.attempts as attempts_module
import quantum_entanglement.store as store_module
from quantum_entanglement.attempts import InvocationLease, SQLiteInvocationAttemptStore
from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    TASK_INVOCATION_STARTED_EVENT_TYPE,
    InvocationExecutionManifest,
    InvocationStartClaimed,
    InvocationStartEvidenceV2,
    InvocationStartObserved,
    TaskInvocationAdmissionRequest,
    build_task_invocation_admission_request,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition
from quantum_entanglement.store import (
    ConcurrencyError,
    EventStorePoisonedError,
    InvocationStartCommitAmbiguityError,
    InvocationStartConflictError,
    InvocationStartTransactionError,
    SQLiteEventStore,
)

ADMITTED_AT = "2026-08-27T04:00:00Z"
REQUESTED_AT = "2026-08-27T04:00:00.000001Z"
RUNNING_AT = "2026-08-27T04:00:00.000002Z"
CLAIMED_AT = "2026-08-27T04:00:01.000000Z"


class TextSubclass(str):
    pass


def event_store_traceback_locals(error: BaseException) -> str:
    """Render only store-owned frames retained by one public exception chain."""

    pending = [error]
    seen: set[int] = set()
    values: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_code.co_filename == store_module.__file__:
                values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(values)


def canonical_request() -> TaskInvocationAdmissionRequest:
    manifest = InvocationExecutionManifest.from_dict(
        {
            "schemaVersion": 1,
            "invocationId": "invocation-start-store-1",
            "sessionId": "session-start-store-1",
            "planId": "plan-start-store-1",
            "taskId": "task-start-store-1",
            "agentId": "agent-start-store-1",
            "jobIdempotencyKey": "invoke:task-start-store-1",
            "taskRevision": 11,
            "correlationId": "correlation-start-store-1",
            "causationId": "task-start-store-1",
            "envelopeDigest": "a" * 64,
            "contextDigest": "b" * 64,
            "authorizationDigest": "c" * 64,
            "runtimeRevision": "runtime:sha256:" + "d" * 64,
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
        execution_requested_event_id="event-execution-requested-start-store-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-task-running-start-store-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=67,
    )


def canonical_start_event(
    request: TaskInvocationAdmissionRequest,
    lease: InvocationLease,
) -> DomainEvent:
    manifest = request.manifest
    evidence = InvocationStartEvidenceV2(
        schema_version=2,
        invocation_id=manifest.invocation_id,
        session_id=manifest.session_id,
        plan_id=manifest.plan_id,
        task_id=manifest.task_id,
        agent_id=manifest.agent_id,
        job_idempotency_key=manifest.job_idempotency_key,
        attempt_id=lease.attempt_id,
        attempt_number=lease.attempt_number,
        lease_epoch=lease.lease_epoch,
        worker_id=lease.worker_id,
        lease_token_digest=hashlib.sha256(lease.lease_token.encode("utf-8")).hexdigest(),
        claimed_at=lease.claimed_at,
        lease_expires_at=lease.lease_expires_at,
        manifest_digest=manifest.canonical_digest(),
        envelope_digest=manifest.envelope_digest,
        context_digest=manifest.context_digest,
        authorization_digest=manifest.authorization_digest,
        runtime_revision=manifest.runtime_revision,
        correlation_id=manifest.correlation_id,
        causation_id=manifest.causation_id,
    )
    return DomainEvent(
        stream_id=request.stream_id,
        event_type=TASK_INVOCATION_STARTED_EVENT_TYPE,
        payload=evidence.to_dict(),
        actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
        event_id="event-invocation-started-store-1",
        timestamp=evidence.claimed_at,
        correlation_id=manifest.correlation_id,
        causation_id=manifest.causation_id,
        idempotency_key=f"invocation-start:{manifest.invocation_id}:1",
    )


def seed_valid_start(
    store: SQLiteEventStore,
    path: str,
) -> tuple[TaskInvocationAdmissionRequest, InvocationLease, StoredEvent]:
    request = canonical_request()
    store.append_task_invocation_admission(request, expected_version=0)
    attempts = SQLiteInvocationAttemptStore(path, clock=lambda: CLAIMED_AT)
    try:
        lease = attempts.claim(
            request.manifest.invocation_id,
            "worker-start-store-1",
            lease_seconds=60,
        )
    finally:
        attempts.close()
    if lease is None:  # pragma: no cover - the fixture owns a fresh canonical queued job.
        raise AssertionError("canonical invocation was not claimable")
    stored = store.append(canonical_start_event(request, lease), expected_version=2)
    return request, lease, stored


class InvocationStartObservationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: ADMITTED_AT)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def assert_receipt_only_replay(
        self,
        store: SQLiteEventStore,
        invocation_id: str,
        worker_id: str,
        *,
        expected_version: int,
    ) -> InvocationStartObserved:
        with (
            patch.object(store, "_now", side_effect=AssertionError("clock must not run")),
            patch.object(
                store_module,
                "new_id",
                side_effect=AssertionError("ID provider must not run"),
            ),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                side_effect=AssertionError("token provider must not run"),
            ),
        ):
            replay = store.claim_invocation_start(
                invocation_id,
                worker_id,
                lease_seconds=60,
                expected_version=expected_version,
            )
        self.assertIs(type(replay), InvocationStartObserved)
        return cast(InvocationStartObserved, replay)

    def test_unknown_and_canonically_admitted_unstarted_invocations_return_none(self) -> None:
        request = canonical_request()

        self.assertIsNone(self.store.read_invocation_start("unknown-invocation"))
        self.store.append_task_invocation_admission(request, expected_version=0)
        self.assertIsNone(self.store.read_invocation_start(request.manifest.invocation_id))

        self.assertEqual(self.store.stream_version(request.stream_id), 2)
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_attempts").fetchone()[
                0
            ],
            0,
        )

    def test_first_claim_commits_attempt_and_start_event_as_one_unit(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)

        claimed = self.store.claim_invocation_start(
            request.manifest.invocation_id,
            "worker-start-store-1",
            lease_seconds=60,
            expected_version=2,
        )

        self.assertIs(type(claimed), InvocationStartClaimed)
        typed = cast(InvocationStartClaimed, claimed)
        self.assertEqual(typed.receipt.sequence, 3)
        self.assertEqual(typed.receipt.evidence.invocation_id, request.manifest.invocation_id)
        self.assertEqual(typed.receipt.evidence.attempt_id, typed.lease.attempt_id)
        self.assertEqual(typed.receipt.evidence.worker_id, typed.lease.worker_id)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT event_type FROM events WHERE event_id = ?",
                (typed.receipt.event_id,),
            ).fetchone()[0],
            TASK_INVOCATION_STARTED_EVENT_TYPE,
        )
        self.assertEqual(self.store.stream_version(request.stream_id), 3)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.read_invocation_start(request.manifest.invocation_id),
            InvocationStartObserved(typed.receipt),
        )

    def test_every_retry_peer_and_reopen_returns_receipt_only_without_providers(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        first = self.store.claim_invocation_start(
            request.manifest.invocation_id,
            "worker-start-store-1",
            lease_seconds=60,
            expected_version=2,
        )
        self.assertIs(type(first), InvocationStartClaimed)
        receipt = cast(InvocationStartClaimed, first).receipt

        same_worker = self.assert_receipt_only_replay(
            self.store,
            request.manifest.invocation_id,
            "worker-start-store-1",
            expected_version=2,
        )
        second_worker = self.assert_receipt_only_replay(
            self.store,
            request.manifest.invocation_id,
            "worker-start-store-2",
            expected_version=2,
        )
        peer = SQLiteEventStore(self.path, clock=lambda: CLAIMED_AT)
        try:
            peer_worker = self.assert_receipt_only_replay(
                peer,
                request.manifest.invocation_id,
                "worker-start-store-peer",
                expected_version=2,
            )
        finally:
            peer.close()
        self.store.close()
        self.store = SQLiteEventStore(self.path, clock=lambda: CLAIMED_AT)
        reopened = self.assert_receipt_only_replay(
            self.store,
            request.manifest.invocation_id,
            "worker-start-store-reopened",
            expected_version=2,
        )

        self.assertEqual(
            {same_worker.receipt, second_worker.receipt, peer_worker.receipt, reopened.receipt},
            {receipt},
        )
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            1,
        )

    def test_replay_version_is_anchored_before_start_not_at_current_stream_head(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        first = self.store.claim_invocation_start(
            request.manifest.invocation_id,
            "worker-start-store-1",
            lease_seconds=60,
            expected_version=2,
        )
        self.assertIs(type(first), InvocationStartClaimed)
        self.store.append(
            DomainEvent(
                stream_id=request.stream_id,
                event_type="task.audit.recorded",
                payload={"taskId": request.manifest.task_id},
                actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
                event_id="event-after-invocation-start",
                timestamp=CLAIMED_AT,
                correlation_id=request.manifest.correlation_id,
                causation_id=request.manifest.causation_id,
                idempotency_key="audit-after-invocation-start",
            ),
            expected_version=3,
        )

        observed = self.assert_receipt_only_replay(
            self.store,
            request.manifest.invocation_id,
            "worker-start-store-2",
            expected_version=2,
        )
        self.assertEqual(observed.receipt, cast(InvocationStartClaimed, first).receipt)
        with (
            patch.object(self.store, "_now", side_effect=AssertionError("clock must not run")),
            patch.object(
                store_module,
                "new_id",
                side_effect=AssertionError("ID provider must not run"),
            ),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                side_effect=AssertionError("token provider must not run"),
            ),
        ):
            with self.assertRaises(ConcurrencyError):
                self.store.claim_invocation_start(
                    request.manifest.invocation_id,
                    "worker-start-store-2",
                    lease_seconds=60,
                    expected_version=3,
                )

    def test_first_claim_samples_each_provider_before_its_immediate_pid_guard(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        calls: list[str] = []
        original_guard = self.store._require_current_process
        provided_ids = iter(("attempt-provider-order", "event-provider-order"))

        def guarded() -> None:
            calls.append("pid")
            original_guard()

        def clock() -> str:
            calls.append("clock")
            return CLAIMED_AT

        def id_provider(prefix: str = "evt") -> str:
            calls.append(f"id:{prefix}")
            return next(provided_ids)

        def token_provider(nbytes: int = 32) -> str:
            calls.append(f"token:{nbytes}")
            return "provider-order-lease-token"

        with (
            patch.object(self.store, "_require_current_process", new=guarded),
            patch.object(self.store, "_now", new=clock),
            patch.object(store_module, "new_id", new=id_provider),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                new=token_provider,
            ),
        ):
            claimed = self.store.claim_invocation_start(
                request.manifest.invocation_id,
                "worker-start-store-1",
                lease_seconds=60,
                expected_version=2,
            )

        self.assertIs(type(claimed), InvocationStartClaimed)
        provider_start = calls.index("clock")
        self.assertEqual(
            calls[provider_start : provider_start + 8],
            [
                "clock",
                "pid",
                "id:attempt",
                "pid",
                "id:evt",
                "pid",
                "token:32",
                "pid",
            ],
        )
        self.assertEqual(calls.count("clock"), 1)
        self.assertEqual(calls.count("id:attempt"), 1)
        self.assertEqual(calls.count("id:evt"), 1)
        self.assertEqual(calls.count("token:32"), 1)

    def test_claim_rejects_noncanonical_lease_numbers_before_any_provider(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        invalid_values = (True, "60", 0, -1, float("inf"), float("nan"), 10**400)

        for value in invalid_values:
            with (
                self.subTest(value_type=type(value).__name__),
                patch.object(
                    self.store,
                    "_now",
                    side_effect=AssertionError("clock must not run"),
                ),
                patch.object(
                    store_module,
                    "new_id",
                    side_effect=AssertionError("ID provider must not run"),
                ),
                patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    side_effect=AssertionError("token provider must not run"),
                ),
            ):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.claim_invocation_start(
                        request.manifest.invocation_id,
                        "worker-start-store-1",
                        lease_seconds=cast(float, value),
                        expected_version=2,
                    )

        self.assertEqual(self.store.stream_version(request.stream_id), 2)
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_attempts").fetchone()[
                0
            ],
            0,
        )

    def test_identifier_collisions_are_rejected_before_lease_token_generation(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        other_spec = replace(
            request.job_spec,
            invocation_id="invocation-start-store-collision-owner",
            task_id="task-start-store-collision-owner",
            idempotency_key="invoke:task-start-store-collision-owner",
            payload_digest="e" * 64,
        )
        attempts = SQLiteInvocationAttemptStore(self.path, clock=lambda: CLAIMED_AT)
        try:
            attempts.enqueue(other_spec)
            other_lease = attempts.claim(
                other_spec.invocation_id,
                "worker-collision-owner",
                lease_seconds=60,
            )
        finally:
            attempts.close()
        if other_lease is None:  # pragma: no cover - the fixture owns a fresh job.
            raise AssertionError("collision owner was not claimable")

        attempt_id_calls: list[str] = []

        def colliding_attempt_id(prefix: str = "evt") -> str:
            attempt_id_calls.append(prefix)
            if prefix != "attempt":
                raise AssertionError("event ID provider must not run")
            return other_lease.attempt_id

        with (
            patch.object(self.store, "_now", return_value=CLAIMED_AT),
            patch.object(store_module, "new_id", new=colliding_attempt_id),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                side_effect=AssertionError("token provider must not run"),
            ),
        ):
            with self.assertRaises(InvocationStartConflictError):
                self.store.claim_invocation_start(
                    request.manifest.invocation_id,
                    "worker-start-store-1",
                    lease_seconds=60,
                    expected_version=2,
                )
        self.assertEqual(attempt_id_calls, ["attempt"])

        admission_events, _job_spec = request.components()
        existing_event_id = admission_events[0].event_id
        event_id_calls: list[str] = []

        def colliding_event_id(prefix: str = "evt") -> str:
            event_id_calls.append(prefix)
            if prefix == "attempt":
                return "attempt-before-event-id-collision"
            if prefix == "evt":
                return existing_event_id
            raise AssertionError("unexpected ID provider prefix")

        with (
            patch.object(self.store, "_now", return_value=CLAIMED_AT),
            patch.object(store_module, "new_id", new=colliding_event_id),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                side_effect=AssertionError("token provider must not run"),
            ),
        ):
            with self.assertRaises(InvocationStartConflictError):
                self.store.claim_invocation_start(
                    request.manifest.invocation_id,
                    "worker-start-store-1",
                    lease_seconds=60,
                    expected_version=2,
                )
        self.assertEqual(event_id_calls, ["attempt", "evt"])
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM invocation_jobs WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            "queued",
        )
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            0,
        )

    def test_every_post_mutation_failure_rolls_back_job_attempt_and_start_together(self) -> None:
        for stage in ("after-cas", "after-start-append", "after-fresh-readback"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "state.sqlite3")
                store = SQLiteEventStore(path, clock=lambda: ADMITTED_AT)
                request = canonical_request()
                try:
                    store.append_task_invocation_admission(request, expected_version=0)
                    original_cas = attempts_module._claim_first_invocation_in_transaction
                    original_append = store._append_in_transaction
                    original_readback = store._read_invocation_start_in_transaction

                    def fail_after_cas(
                        *args: Any,
                        _original: Any = original_cas,
                        **kwargs: Any,
                    ) -> Any:
                        _original(*args, **kwargs)
                        raise RuntimeError("injected post-CAS failure")

                    def fail_after_append(
                        *args: Any,
                        _original: Any = original_append,
                        **kwargs: Any,
                    ) -> Any:
                        _original(*args, **kwargs)
                        raise RuntimeError("injected post-append failure")

                    def fail_after_readback(
                        *args: Any,
                        _original: Any = original_readback,
                        **kwargs: Any,
                    ) -> Any:
                        result = _original(*args, **kwargs)
                        if kwargs.get("fresh") is True and result is not None:
                            raise RuntimeError("injected post-readback failure")
                        return result

                    with ExitStack() as patches:
                        patches.enter_context(patch.object(store, "_now", return_value=CLAIMED_AT))
                        patches.enter_context(
                            patch(
                                "quantum_entanglement.store.secrets.token_urlsafe",
                                return_value=f"rollback-canary-{stage}",
                            )
                        )
                        if stage == "after-cas":
                            patches.enter_context(
                                patch(
                                    "quantum_entanglement.store."
                                    "_claim_first_invocation_in_transaction",
                                    side_effect=fail_after_cas,
                                )
                            )
                        elif stage == "after-start-append":
                            patches.enter_context(
                                patch.object(
                                    store,
                                    "_append_in_transaction",
                                    side_effect=fail_after_append,
                                )
                            )
                        else:
                            patches.enter_context(
                                patch.object(
                                    store,
                                    "_read_invocation_start_in_transaction",
                                    side_effect=fail_after_readback,
                                )
                            )
                        with self.assertRaises(InvocationStartTransactionError):
                            store.claim_invocation_start(
                                request.manifest.invocation_id,
                                "worker-start-store-1",
                                lease_seconds=60,
                                expected_version=2,
                            )

                    row = store._connection.execute(
                        "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                        (request.manifest.invocation_id,),
                    ).fetchone()
                    self.assertEqual(row["status"], "queued")
                    self.assertEqual(row["attempts_started"], 0)
                    self.assertEqual(row["lease_epoch"], 0)
                    self.assertIsNone(row["lease_owner"])
                    self.assertIsNone(row["lease_token_digest"])
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                            (request.manifest.invocation_id,),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(store.stream_version(request.stream_id), 2)
                    self.assertIsNone(store.read_invocation_start(request.manifest.invocation_id))
                    self.assertFalse(store._poisoned)
                finally:
                    store.close()

    def test_nonstandard_base_exception_cannot_escape_with_plaintext_lease_authority(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        canary = "raw-lease-hostile-base-exception-canary"

        class HostileFault(BaseException):
            pass

        def hostile_digest(_lease_token: str) -> str:
            raise HostileFault(canary)

        with (
            patch.object(self.store, "_now", return_value=CLAIMED_AT),
            patch.object(self.store, "_lease_token_digest", new=hostile_digest),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                return_value=canary,
            ),
        ):
            with self.assertRaises(InvocationStartTransactionError) as caught:
                self.store.claim_invocation_start(
                    request.manifest.invocation_id,
                    "worker-start-store-1",
                    lease_seconds=60,
                    expected_version=2,
                )

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(canary, str(caught.exception))
        self.assertNotIn(canary, repr(caught.exception))
        self.assertNotIn(canary, event_store_traceback_locals(caught.exception))
        self.assertNotIn(canary, "\n".join(self.store._connection.iterdump()))
        self.assertEqual(self.store.stream_version(request.stream_id), 2)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.store._poisoned)

    def test_begin_ack_loss_confirms_rollback_before_any_start_provider(self) -> None:
        real_connect = sqlite3.connect

        class BeginAckLossConnection(sqlite3.Connection):
            fail_next_begin = False

            def execute(
                connection_self,
                statement: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                if (
                    statement.strip().upper() == "BEGIN IMMEDIATE"
                    and connection_self.fail_next_begin
                ):
                    connection_self.fail_next_begin = False
                    super().execute(statement, parameters)  # type: ignore[arg-type]
                    raise sqlite3.OperationalError("begin acknowledgement lost")
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_fault(database: str, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection,
                real_connect(database, factory=BeginAckLossConnection, **kwargs),
            )

        self.store.close()
        with patch(
            "quantum_entanglement.store.sqlite3.connect",
            new=connect_with_fault,
        ):
            self.store = SQLiteEventStore(self.path, clock=lambda: ADMITTED_AT)
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        connection = cast(BeginAckLossConnection, self.store._connection)
        self.assertIsInstance(connection, BeginAckLossConnection)
        connection.fail_next_begin = True

        with (
            patch.object(
                self.store,
                "_now",
                side_effect=AssertionError("clock must not run"),
            ),
            patch.object(
                store_module,
                "new_id",
                side_effect=AssertionError("ID provider must not run"),
            ),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                side_effect=AssertionError("token provider must not run"),
            ),
        ):
            with self.assertRaises(InvocationStartTransactionError) as caught:
                self.store.claim_invocation_start(
                    request.manifest.invocation_id,
                    "worker-start-store-1",
                    lease_seconds=60,
                    expected_version=2,
                )

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertFalse(connection.in_transaction)
        self.assertFalse(self.store._poisoned)
        self.assertEqual(self.store.stream_version(request.stream_id), 2)
        self.assertIsNone(self.store.read_invocation_start(request.manifest.invocation_id))
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            0,
        )

    def test_denied_commit_rolls_back_start_without_poison_or_token_persistence(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        canary = "raw-lease-denied-commit-canary"

        def deny_commit(
            action_code: int,
            operation: object,
            _table: object,
            _database: object,
            _trigger: object,
        ) -> int:
            if action_code == sqlite3.SQLITE_TRANSACTION and str(operation).upper() == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store._connection.set_authorizer(deny_commit)
        try:
            with (
                patch.object(self.store, "_now", return_value=CLAIMED_AT),
                patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    return_value=canary,
                ),
            ):
                with self.assertRaises(InvocationStartTransactionError) as caught:
                    self.store.claim_invocation_start(
                        request.manifest.invocation_id,
                        "worker-start-store-1",
                        lease_seconds=60,
                        expected_version=2,
                    )
        finally:
            self.store._connection.set_authorizer(None)

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertFalse(self.store._connection.in_transaction)
        self.assertFalse(self.store._poisoned)
        self.assertEqual(self.store.stream_version(request.stream_id), 2)
        self.assertIsNone(self.store.read_invocation_start(request.manifest.invocation_id))
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            0,
        )
        self.assertNotIn(canary, "\n".join(self.store._connection.iterdump()))

    def test_commit_ack_loss_poisons_and_reopens_as_receipt_only_observation(self) -> None:
        real_connect = sqlite3.connect

        class CommitAckLossConnection(sqlite3.Connection):
            fail_next_commit = False

            def execute(
                connection_self,
                statement: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                if statement.strip().upper() == "COMMIT" and connection_self.fail_next_commit:
                    connection_self.fail_next_commit = False
                    super().execute(statement, parameters)  # type: ignore[arg-type]
                    raise sqlite3.OperationalError("commit acknowledgement lost")
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_fault(database: str, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection,
                real_connect(database, factory=CommitAckLossConnection, **kwargs),
            )

        self.store.close()
        with patch(
            "quantum_entanglement.store.sqlite3.connect",
            new=connect_with_fault,
        ):
            self.store = SQLiteEventStore(self.path, clock=lambda: ADMITTED_AT)
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        connection = cast(CommitAckLossConnection, self.store._connection)
        self.assertIsInstance(connection, CommitAckLossConnection)
        connection.fail_next_commit = True
        canary = "raw-lease-commit-ack-loss-canary"

        with (
            patch.object(self.store, "_now", return_value=CLAIMED_AT),
            patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                return_value=canary,
            ),
        ):
            with self.assertRaises(InvocationStartCommitAmbiguityError) as caught:
                self.store.claim_invocation_start(
                    request.manifest.invocation_id,
                    "worker-start-store-1",
                    lease_seconds=60,
                    expected_version=2,
                )

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(canary, event_store_traceback_locals(caught.exception))
        with self.assertRaises(EventStorePoisonedError):
            self.store.read_invocation_start(request.manifest.invocation_id)
        self.store.close()

        self.store = SQLiteEventStore(self.path, clock=lambda: ADMITTED_AT)
        observed = self.store.read_invocation_start(request.manifest.invocation_id)
        self.assertIs(type(observed), InvocationStartObserved)
        retried = self.assert_receipt_only_replay(
            self.store,
            request.manifest.invocation_id,
            "worker-start-store-reconcile",
            expected_version=2,
        )
        self.assertEqual(retried, observed)
        self.assertEqual(self.store.stream_version(request.stream_id), 3)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            1,
        )
        self.assertNotIn(canary, "\n".join(self.store._connection.iterdump()))

    def test_valid_start_is_observed_without_plaintext_lease_authority(self) -> None:
        request, lease, stored = seed_valid_start(self.store, self.path)

        observed = self.store.read_invocation_start(request.manifest.invocation_id)

        self.assertIs(type(observed), InvocationStartObserved)
        typed = cast(InvocationStartObserved, observed)
        self.assertEqual(typed.receipt.event_id, stored.event.event_id)
        self.assertEqual(typed.receipt.sequence, 3)
        self.assertEqual(typed.receipt.global_position, stored.global_position)
        self.assertEqual(typed.receipt.evidence.attempt_id, lease.attempt_id)
        self.assertFalse(hasattr(typed, "lease"))
        self.assertNotIn(lease.lease_token, repr(typed))
        self.assertNotIn(lease.lease_token, json.dumps(typed.to_dict(), sort_keys=True))

    def test_reopen_returns_the_same_capability_free_observation(self) -> None:
        request, lease, _stored = seed_valid_start(self.store, self.path)
        first = self.store.read_invocation_start(request.manifest.invocation_id)
        self.store.close()

        self.store = SQLiteEventStore(self.path, clock=lambda: ADMITTED_AT)
        reopened = self.store.read_invocation_start(request.manifest.invocation_id)

        self.assertEqual(reopened, first)
        self.assertNotIn(lease.lease_token, repr(reopened))

    def test_standalone_job_without_v4_receipt_is_rejected(self) -> None:
        request = canonical_request()
        attempts = SQLiteInvocationAttemptStore(self.path, clock=lambda: ADMITTED_AT)
        try:
            attempts.enqueue(request.job_spec)
        finally:
            attempts.close()

        with self.assertRaises(InvocationStartConflictError) as caught:
            self.store.read_invocation_start(request.manifest.invocation_id)

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_generic_or_semantically_forged_admission_is_rejected(self) -> None:
        request = canonical_request()
        events, job = request.components()
        forged = (replace(events[0], actor_id="legacy-caller"), events[1])
        self.store.append_invocation_admission(forged, job, expected_version=0)

        with self.assertRaises(InvocationStartConflictError):
            self.store.read_invocation_start(request.manifest.invocation_id)

    def test_database_wide_wrong_stream_start_markers_block_authority_minting(self) -> None:
        request = canonical_request()
        cases: tuple[tuple[str, str, str, dict[str, object]], ...] = (
            (
                "payload-match",
                TASK_INVOCATION_STARTED_EVENT_TYPE,
                "unrelated-start-key",
                {"invocationId": request.manifest.invocation_id},
            ),
            (
                "canonical-key",
                "legacy.start.marker",
                f"invocation-start:{request.manifest.invocation_id}:1",
                {},
            ),
            (
                "legacy-key",
                "legacy.start.marker",
                f"invocation-started:{request.manifest.task_id}",
                {},
            ),
        )
        for label, event_type, idempotency_key, payload in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "state.sqlite3")
                store = SQLiteEventStore(path, clock=lambda: ADMITTED_AT)
                try:
                    store.append_task_invocation_admission(request, expected_version=0)
                    store.append(
                        DomainEvent(
                            stream_id=f"session:wrong-{label}",
                            event_type=event_type,
                            payload=payload,
                            actor_id="forged-worker",
                            event_id=f"wrong-stream-start-{label}",
                            timestamp=CLAIMED_AT,
                            correlation_id=request.manifest.correlation_id,
                            causation_id=request.manifest.causation_id,
                            idempotency_key=idempotency_key,
                        ),
                        expected_version=0,
                    )
                    with (
                        patch.object(
                            store,
                            "_now",
                            side_effect=AssertionError("clock must not run"),
                        ),
                        patch.object(
                            store_module,
                            "new_id",
                            side_effect=AssertionError("ID provider must not run"),
                        ),
                        patch(
                            "quantum_entanglement.store.secrets.token_urlsafe",
                            side_effect=AssertionError("token provider must not run"),
                        ),
                    ):
                        with self.assertRaises(InvocationStartConflictError):
                            store.claim_invocation_start(
                                request.manifest.invocation_id,
                                "worker-start-store-1",
                                lease_seconds=60,
                                expected_version=2,
                            )
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT status FROM invocation_jobs WHERE invocation_id = ?",
                            (request.manifest.invocation_id,),
                        ).fetchone()[0],
                        "queued",
                    )
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT COUNT(*) FROM invocation_attempts"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    store.close()

    def test_legacy_runtime_start_is_never_upgraded_to_schema_two(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        self.store.append(
            DomainEvent(
                stream_id=request.stream_id,
                event_type=TASK_INVOCATION_STARTED_EVENT_TYPE,
                payload={
                    "taskId": request.manifest.task_id,
                    "agentId": request.manifest.agent_id,
                    "envelope": {},
                    "contextDigest": request.manifest.context_digest,
                },
                actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
                event_id="legacy-start-event",
                timestamp=CLAIMED_AT,
                correlation_id=request.manifest.correlation_id,
                causation_id=request.manifest.causation_id,
                idempotency_key=f"invocation-started:{request.manifest.task_id}",
            ),
            expected_version=2,
        )

        with self.assertRaises(InvocationStartConflictError):
            self.store.read_invocation_start(request.manifest.invocation_id)

    def test_attempt_without_start_and_start_without_attempt_are_rejected(self) -> None:
        request = canonical_request()
        self.store.append_task_invocation_admission(request, expected_version=0)
        attempts = SQLiteInvocationAttemptStore(self.path, clock=lambda: CLAIMED_AT)
        try:
            lease = attempts.claim(
                request.manifest.invocation_id,
                "worker-start-store-1",
                lease_seconds=60,
            )
        finally:
            attempts.close()
        if lease is None:  # pragma: no cover
            raise AssertionError("canonical invocation was not claimable")
        with self.assertRaises(InvocationStartConflictError):
            self.store.read_invocation_start(request.manifest.invocation_id)

        self.store.close()
        second_path = str(Path(self.tempdir.name) / "start-without-attempt.sqlite3")
        self.store = SQLiteEventStore(second_path, clock=lambda: ADMITTED_AT)
        second_request = canonical_request()
        self.store.append_task_invocation_admission(second_request, expected_version=0)
        self.store.append(canonical_start_event(second_request, lease), expected_version=2)
        with self.assertRaises(InvocationStartConflictError):
            self.store.read_invocation_start(second_request.manifest.invocation_id)

    def test_event_job_attempt_and_receipt_tampering_each_fail_closed(self) -> None:
        mutations = (
            (
                "event-envelope",
                "UPDATE events SET actor_id = 'forged' WHERE event_id = "
                "'event-invocation-started-store-1'",
            ),
            (
                "job-binding",
                "UPDATE invocation_jobs SET plan_id = 'forged-plan' WHERE invocation_id = "
                "'invocation-start-store-1'",
            ),
            (
                "attempt-binding",
                "UPDATE invocation_attempts SET worker_id = 'forged-worker' WHERE invocation_id = "
                "'invocation-start-store-1'",
            ),
            (
                "receipt-binding",
                "UPDATE invocation_admissions SET job_binding_sha256 = '" + "f" * 64 + "'",
            ),
        )
        for label, statement in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "state.sqlite3")
                store = SQLiteEventStore(path, clock=lambda: ADMITTED_AT)
                try:
                    request, _lease, _stored = seed_valid_start(store, path)
                    store._connection.execute(statement)
                    with self.assertRaises(InvocationStartConflictError):
                        store.read_invocation_start(request.manifest.invocation_id)
                finally:
                    store.close()

    def test_start_payload_and_partial_row_deletion_fail_closed(self) -> None:
        request, _lease, _stored = seed_valid_start(self.store, self.path)
        row = self.store._connection.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            ("event-invocation-started-store-1",),
        ).fetchone()
        payload = json.loads(row[0])
        payload["runtimeRevision"] = "forged-runtime"
        self.store._connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                "event-invocation-started-store-1",
            ),
        )
        with self.assertRaises(InvocationStartConflictError):
            self.store.read_invocation_start(request.manifest.invocation_id)

        self.store.close()
        second_path = str(Path(self.tempdir.name) / "deleted-start.sqlite3")
        self.store = SQLiteEventStore(second_path, clock=lambda: ADMITTED_AT)
        second_request, _second_lease, _second_stored = seed_valid_start(self.store, second_path)
        self.store._connection.execute(
            "DELETE FROM events WHERE event_id = ?",
            ("event-invocation-started-store-1",),
        )
        with self.assertRaises(InvocationStartConflictError):
            self.store.read_invocation_start(second_request.manifest.invocation_id)

    def test_identity_inputs_are_exact_canonical_and_poison_precedes_input_access(self) -> None:
        for value in (
            None,
            True,
            TextSubclass("invocation-start-store-1"),
            "",
            " invocation-start-store-1",
            "invocation\x00start",
            "invocatio\u0301n",
        ):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.read_invocation_start(cast(str, value))

        class HostileIdentity:
            def __str__(self) -> str:
                raise AssertionError("poisoned store must not touch caller input")

        self.store._poisoned = True
        with self.assertRaises(EventStorePoisonedError):
            self.store.read_invocation_start(cast(str, HostileIdentity()))


if __name__ == "__main__":
    unittest.main()
