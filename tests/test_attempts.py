import builtins
import hashlib
import importlib.resources
import inspect
import multiprocessing
import sqlite3
import tempfile
import threading
import traceback
import unittest
from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

import quantum_entanglement.attempts as attempts_module
from quantum_entanglement.attempts import (
    AttemptStatus,
    InvocationClockRegressionError,
    InvocationCommitAmbiguityError,
    InvocationConflictError,
    InvocationIntegrityError,
    InvocationJobSpec,
    InvocationLease,
    InvocationStatus,
    InvocationStoreClosedError,
    InvocationStorePoisonedError,
    InvocationStoreProcessMismatchError,
    InvocationTransactionError,
    MigrationDriftError,
    SQLiteInvocationAttemptStore,
    invocation_payload_digest,
)
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import SQLiteEventStore

T0 = "2026-08-20T00:00:00Z"


def timestamp(seconds):
    return f"2026-08-20T00:00:{seconds:02d}Z"


def persisted_timestamp(seconds):
    return f"2026-08-20T00:00:{seconds:02d}.000000Z"


def exception_graph_text(error):
    pending = [error]
    visited = set()
    details = []
    while pending and len(visited) < 100:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        details.extend(
            (
                str(current),
                repr(current),
                repr(current.args),
                repr(getattr(current, "__dict__", {})),
            )
        )
        notes = getattr(current, "__notes__", ())
        details.extend(str(note) for note in notes)
        for related in (current.__cause__, current.__context__):
            if related is not None:
                pending.append(related)
    return " ".join(details)


class HostileFault(RuntimeError):
    def __init__(self, marker):
        super().__init__(marker)
        self.secret = marker
        self.armed = True

    def __setattr__(self, name, value):
        if getattr(self, "armed", False) and name in {
            "args",
            "__cause__",
            "__context__",
            "__notes__",
            "__traceback__",
        }:
            raise RuntimeError(f"hostile mutation attempted: {self.secret}")
        super().__setattr__(name, value)


class HostileBaseFault(BaseException):
    def __init__(self, marker):
        super().__init__(marker)
        self.secret = marker
        self.armed = True

    def __setattr__(self, name, value):
        if getattr(self, "armed", False) and name in {
            "args",
            "__cause__",
            "__context__",
            "__notes__",
            "__traceback__",
        }:
            raise RuntimeError(f"hostile mutation attempted: {self.secret}")
        super().__setattr__(name, value)


class MutableClock:
    def __init__(self, value=T0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def job_spec(**changes):
    values = {
        "session_id": "session-1",
        "plan_id": "plan-1",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "idempotency_key": "invoke:task-1",
        "payload_digest": invocation_payload_digest({"taskId": "task-1", "context": "abc"}),
        "invocation_id": "invocation-1",
        "max_attempts": 3,
    }
    values.update(changes)
    return InvocationJobSpec(**values)


def claim_from_process(path, worker_id, ready_queue, start_event, result_queue):
    store = SQLiteInvocationAttemptStore(path, clock=lambda: T0)
    try:
        ready_queue.put(worker_id)
        if not start_event.wait(timeout=5):
            raise RuntimeError("process claim start barrier timed out")
        lease = store.claim("invocation-1", worker_id, lease_seconds=10)
        result_queue.put((worker_id, lease is not None))
    finally:
        store.close()


def probe_fork_inherited_attempt_store(store, result_connection):
    operations = (
        store.__enter__,
        partial(store.get, "invocation-1"),
        partial(store.get_for_task, "session-1", "task-1"),
        partial(store.recovery_snapshot_for_task, "session-1", "task-1"),
        partial(store.attempts, "invocation-1"),
        partial(store.attempts_page, "invocation-1"),
        partial(store.enqueue, job_spec()),
        store.recover_expired,
        partial(store.claim_next, "worker", lease_seconds=5),
        partial(store.claim, "invocation-1", "worker", lease_seconds=5),
        store.schema_version,
        store.close,
    )
    outcomes = []
    for operation in operations:
        try:
            operation()
        except BaseException as error:
            outcomes.append((type(error).__name__, getattr(error, "code", None)))
        else:
            outcomes.append(("returned", None))
    result_connection.send(tuple(outcomes))
    result_connection.close()


class InvocationAttemptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.clock = MutableClock()
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def _seed_second_running_attempt(self):
        token_digest = "2" * 64
        self.store._connection.execute(
            """
            INSERT INTO invocation_attempts (
                attempt_id, invocation_id, attempt_number, lease_epoch,
                worker_id, lease_token_digest, status, started_at,
                heartbeat_at, lease_expires_at
            ) VALUES (
                'attempt-2', 'invocation-1', 2, 2,
                'worker-2', ?, 'running', ?, ?, ?
            )
            """,
            (
                token_digest,
                persisted_timestamp(0),
                persisted_timestamp(0),
                persisted_timestamp(10),
            ),
        )
        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'running', attempts_started = 2, lease_epoch = 2,
                lease_owner = 'worker-2', lease_token_digest = ?,
                lease_expires_at = ?, heartbeat_at = ?, updated_at = ?,
                finished_at = NULL
            WHERE invocation_id = 'invocation-1'
            """,
            (
                token_digest,
                persisted_timestamp(10),
                persisted_timestamp(0),
                persisted_timestamp(0),
            ),
        )

    @contextmanager
    def _commit_then_raise(self):
        commit = self.store._commit_write_transaction
        injected = False

        def committed_failure(connection):
            nonlocal injected
            commit(connection)
            injected = True
            raise RuntimeError("injected commit acknowledgement failure")

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=committed_failure,
        ):
            yield
        self.assertTrue(injected)

    @contextmanager
    def _rollback_then_raise(self):
        injected = False

        def rolled_back_failure(connection):
            nonlocal injected
            connection.execute("ROLLBACK")
            injected = True
            raise RuntimeError("injected ambiguous rolled-back commit")

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=rolled_back_failure,
        ):
            yield
        self.assertTrue(injected)

    def _capture_base_exception(self, operation):
        try:
            operation()
        except BaseException as error:
            return error
        self.fail("operation did not raise a BaseException")

    def _capture_while_exception_active(self, operation, marker):
        try:
            raise HostileFault(marker)
        except HostileFault:
            return self._capture_base_exception(operation)

    def _assert_clean_control_signal(
        self,
        error,
        expected_type,
        *,
        marker,
        system_exit_code=None,
        ambiguity=False,
        close_failure=False,
        module_frame_names=("wrapped", "_raise_clean_control_signal"),
    ):
        self.assertIs(type(error), expected_type)
        if expected_type is SystemExit:
            self.assertIs(type(error.code), type(system_exit_code))
            self.assertEqual(error.code, system_exit_code)
        else:
            self.assertEqual(error.args, ())
        self.assertEqual(getattr(error, "__dict__", {}), {})
        self.assertFalse(getattr(error, "__notes__", ()))
        self.assertNotIn(marker, exception_graph_text(error))

        if ambiguity:
            self.assertIs(type(error.__cause__), InvocationCommitAmbiguityError)
            self.assertEqual(error.__cause__.code, "invocation_commit_ambiguous")
            self.assertIsNone(error.__cause__.__traceback__)
        elif close_failure:
            self.assertIs(type(error.__cause__), InvocationStoreClosedError)
            self.assertEqual(error.__cause__.code, "invocation_store_closed")
            self.assertIsNone(error.__cause__.__traceback__)
        else:
            self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

        module_frames = []
        cursor = error.__traceback__
        while cursor is not None:
            if cursor.tb_frame.f_globals.get("__name__") == attempts_module.__name__:
                module_frames.append(cursor.tb_frame)
            cursor = cursor.tb_next
        self.assertEqual(
            [frame.f_code.co_name for frame in module_frames],
            list(module_frame_names),
        )
        wrapper_locals = module_frames[0].f_locals
        self.assertFalse({"args", "kwargs", "store", "error"} & set(wrapper_locals))
        self.assertNotIn(marker, repr(wrapper_locals))

    @staticmethod
    def _hostile_control_signal(error, marker):
        error.secret = marker
        error.__notes__ = [marker]
        error.__cause__ = RuntimeError(marker)
        error.__context__ = HostileFault(marker)
        try:
            raise error
        except BaseException:
            pass
        return error

    @staticmethod
    def _raise_context_body(store, body_error):
        with store:
            raise body_error

    @staticmethod
    def _exit_empty_context(store):
        with store:
            pass

    def _public_mutator_case(self, kind, index, prefix):
        clock = MutableClock()
        path = str(Path(self.tempdir.name) / f"{prefix}-{index}.sqlite3")
        store = SQLiteInvocationAttemptStore(path, clock=clock)
        lease = None
        if kind != "enqueue":
            maximum = 1 if kind == "fail" else 3
            store.enqueue(job_spec(max_attempts=maximum))
        if kind in {"recover", "heartbeat", "complete", "fail"}:
            lease = store.claim("invocation-1", "worker", lease_seconds=5)
            assert lease is not None
        if kind == "enqueue":
            operation = partial(store.enqueue, job_spec())
        elif kind == "claim":
            operation = partial(store.claim, "invocation-1", "worker", lease_seconds=5)
        elif kind == "claim-next":
            operation = partial(store.claim_next, "worker", lease_seconds=5)
        elif kind == "recover":
            clock.set(timestamp(5))
            operation = store.recover_expired
        elif kind == "heartbeat":
            clock.set(timestamp(1))
            operation = partial(store.heartbeat, lease, lease_seconds=5)
        elif kind == "complete":
            operation = partial(store.complete, lease, result_ref="result:controlled")
        else:
            operation = partial(store.fail, lease, "controlled failure")
        return store, operation, lease

    def _assert_committed_mutator_state(self, kind, store):
        job = store.get("invocation-1")
        self.assertIsNotNone(job)
        attempts = store.attempts("invocation-1")
        if kind == "enqueue":
            self.assertIs(job.status, InvocationStatus.QUEUED)
            self.assertEqual(attempts, ())
        elif kind in {"claim", "claim-next"}:
            self.assertIs(job.status, InvocationStatus.RUNNING)
            self.assertIs(attempts[-1].status, AttemptStatus.RUNNING)
        elif kind == "recover":
            self.assertIs(job.status, InvocationStatus.QUEUED)
            self.assertIs(attempts[-1].status, AttemptStatus.EXPIRED)
        elif kind == "heartbeat":
            self.assertEqual(job.heartbeat_at, persisted_timestamp(1))
            self.assertEqual(attempts[-1].heartbeat_at, persisted_timestamp(1))
        elif kind == "complete":
            self.assertIs(job.status, InvocationStatus.SUCCEEDED)
            self.assertIs(attempts[-1].status, AttemptStatus.SUCCEEDED)
        else:
            self.assertIs(job.status, InvocationStatus.FAILED)
            self.assertIs(attempts[-1].status, AttemptStatus.FAILED)

    def test_default_in_memory_store_is_usable_without_wal(self):
        with SQLiteInvocationAttemptStore(clock=self.clock) as memory_store:
            self.assertEqual(memory_store.schema_version(), 2)
            queued = memory_store.enqueue(job_spec(invocation_id="memory-invocation"))
            self.assertEqual(queued.status, InvocationStatus.QUEUED)

    def test_transaction_errors_are_exported_from_the_attempt_module(self):
        expected = {
            "InvocationCommitAmbiguityError": InvocationCommitAmbiguityError,
            "InvocationStoreClosedError": InvocationStoreClosedError,
            "InvocationStoreProcessMismatchError": InvocationStoreProcessMismatchError,
            "InvocationStorePoisonedError": InvocationStorePoisonedError,
            "InvocationTransactionError": InvocationTransactionError,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, attempts_module.__all__)
                self.assertIs(getattr(attempts_module, name), value)

    def test_public_write_signatures_have_no_sanitizer_bypass(self):
        public_writes = (
            "enqueue",
            "claim",
            "claim_next",
            "recover_expired",
            "heartbeat",
            "complete",
            "fail",
        )
        for name in public_writes:
            with self.subTest(name=name):
                method = getattr(SQLiteInvocationAttemptStore, name)
                self.assertFalse(hasattr(method, "__wrapped__"))
                self.assertNotEqual(str(inspect.signature(method)), "(*args, **kwargs)")

    def test_public_errors_do_not_retain_a_caller_active_exception(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        assert lease is not None
        self.store.close()
        closed_operations = (
            self.store.__enter__,
            partial(self.store.get, "invocation-1"),
            partial(self.store.get_for_task, "session-1", "task-1"),
            partial(self.store.recovery_snapshot_for_task, "session-1", "task-1"),
            partial(self.store.attempts, "invocation-1"),
            partial(self.store.attempts_page, "invocation-1"),
            partial(self.store.enqueue, job_spec()),
            self.store.recover_expired,
            partial(self.store.claim_next, "worker", lease_seconds=5),
            partial(self.store.claim, "invocation-1", "worker", lease_seconds=5),
            partial(self.store.heartbeat, lease, lease_seconds=5),
            partial(self.store.complete, lease),
            partial(self.store.fail, lease, "closed"),
            self.store.schema_version,
        )
        for index, operation in enumerate(closed_operations):
            with self.subTest(kind="closed", index=index):
                marker = f"private caller exception for closed operation {index}"
                captured = self._capture_while_exception_active(operation, marker)
                self.assertIs(type(captured), InvocationStoreClosedError)
                self.assertIsNone(captured.__cause__)
                self.assertIsNone(captured.__context__)
                self.assertNotIn(marker, exception_graph_text(captured))

        poison_store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        poison_store._poison_store()
        try:
            for index, operation in enumerate(
                (
                    poison_store.schema_version,
                    partial(poison_store.enqueue, job_spec()),
                )
            ):
                with self.subTest(kind="poisoned", index=index):
                    marker = f"private caller exception for poisoned operation {index}"
                    captured = self._capture_while_exception_active(operation, marker)
                    self.assertIs(type(captured), InvocationStorePoisonedError)
                    self.assertIsNone(captured.__cause__)
                    self.assertIsNone(captured.__context__)
                    self.assertNotIn(marker, exception_graph_text(captured))
        finally:
            poison_store.close()

        mismatch_store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        mismatch_store._creator_pid += 1
        for index, operation in enumerate(
            (
                mismatch_store.schema_version,
                partial(mismatch_store.enqueue, job_spec()),
                mismatch_store.close,
            )
        ):
            with self.subTest(kind="process-mismatch", index=index):
                marker = f"private caller exception for mismatched operation {index}"
                captured = self._capture_while_exception_active(operation, marker)
                self.assertIs(type(captured), InvocationStoreProcessMismatchError)
                self.assertIsNone(captured.__cause__)
                self.assertIsNone(captured.__context__)
                self.assertNotIn(marker, exception_graph_text(captured))
        mismatch_store._creator_pid -= 1
        mismatch_store.close()

        transaction_store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        try:
            marker = "private caller exception for transaction failure"
            with patch.object(
                transaction_store,
                "_begin_write_transaction",
                side_effect=HostileFault("private transaction driver failure"),
            ):
                captured = self._capture_while_exception_active(
                    partial(transaction_store.enqueue, job_spec()),
                    marker,
                )
            self.assertIs(type(captured), InvocationTransactionError)
            self.assertIsNone(captured.__cause__)
            self.assertIsNone(captured.__context__)
            self.assertNotIn(marker, exception_graph_text(captured))
        finally:
            transaction_store.close()

        ambiguity_store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        commit = ambiguity_store._commit_write_transaction

        def committed_failure(connection):
            commit(connection)
            raise HostileFault("private commit acknowledgement")

        try:
            marker = "private caller exception for commit ambiguity"
            with (
                patch.object(
                    ambiguity_store,
                    "_commit_write_transaction",
                    side_effect=committed_failure,
                ),
                patch.object(
                    ambiguity_store,
                    "_reconcile_enqueued_job",
                    return_value=None,
                ),
            ):
                captured = self._capture_while_exception_active(
                    partial(ambiguity_store.enqueue, job_spec()),
                    marker,
                )
            self.assertIs(type(captured), InvocationCommitAmbiguityError)
            self.assertIsNone(captured.__cause__)
            self.assertIsNone(captured.__context__)
            self.assertNotIn(marker, exception_graph_text(captured))
        finally:
            ambiguity_store.close()

        close_store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        marker = "private caller exception for close failure"
        with patch.object(
            close_store,
            "_close_connection",
            side_effect=HostileFault("private close driver failure"),
        ):
            captured = self._capture_while_exception_active(close_store.close, marker)
        self.assertIs(type(captured), InvocationStoreClosedError)
        self.assertIsNone(captured.__cause__)
        self.assertIsNone(captured.__context__)
        self.assertNotIn(marker, exception_graph_text(captured))
        close_store.close()

    def test_public_validation_and_control_errors_clear_caller_active_exception(self):
        self.store.enqueue(job_spec())
        validation_cases = (
            (partial(self.store.get, ""), ValueError),
            (
                partial(
                    self.store.claim,
                    "invocation-1",
                    "worker",
                    lease_seconds=0,
                ),
                ValueError,
            ),
            (
                partial(
                    self.store.claim,
                    "invocation-1",
                    "worker",
                    lease_seconds="private",
                ),
                TypeError,
            ),
            (
                partial(
                    self.store.enqueue,
                    job_spec(payload_digest="f" * 64),
                ),
                InvocationConflictError,
            ),
        )
        for index, (operation, error_type) in enumerate(validation_cases):
            with self.subTest(kind="validation", index=index):
                marker = f"private caller exception for validation {index}"
                captured = self._capture_while_exception_active(operation, marker)
                self.assertIs(type(captured), error_type)
                self.assertIsNone(captured.__cause__)
                self.assertIsNone(captured.__context__)
                self.assertNotIn(marker, exception_graph_text(captured))

        for index, error_type in enumerate((KeyboardInterrupt, SystemExit, GeneratorExit)):
            with self.subTest(kind="control", error_type=error_type.__name__):
                marker = f"private caller exception for control {error_type.__name__}"
                control_marker = f"private originating {error_type.__name__}"
                with patch.object(
                    self.store,
                    "_begin_write_transaction",
                    side_effect=error_type(control_marker),
                ):
                    captured = self._capture_while_exception_active(
                        partial(
                            self.store.enqueue,
                            job_spec(
                                invocation_id=f"control-{index}",
                                task_id=f"control-task-{index}",
                                idempotency_key=f"control-key-{index}",
                            ),
                        ),
                        marker,
                    )
                self.assertIs(type(captured), error_type)
                self.assertIsNone(captured.__cause__)
                self.assertIsNone(captured.__context__)
                self.assertNotIn(marker, exception_graph_text(captured))
                self.assertNotIn(control_marker, exception_graph_text(captured))

    def test_lease_duration_must_advance_durable_timestamp(self):
        self.store.enqueue(job_spec())
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(ValueError, "durable timestamp precision"):
            self.store.claim("invocation-1", "worker", lease_seconds=0.0000001)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

        lease = self.store.claim("invocation-1", "worker", lease_seconds=0.000001)
        self.assertIsNotNone(lease)
        self.assertGreater(lease.lease_expires_at, lease.claimed_at)

    def test_lease_duration_outside_datetime_range_fails_without_mutation(self):
        self.store.enqueue(job_spec())
        queued = tuple(self.store._connection.iterdump())

        for lease_seconds in (1e20, 10**400):
            with self.subTest(operation="claim", lease_seconds=type(lease_seconds).__name__):
                with self.assertRaisesRegex(ValueError, "supported datetime range"):
                    self.store.claim(
                        "invocation-1",
                        "worker",
                        lease_seconds=lease_seconds,
                    )
                self.assertEqual(tuple(self.store._connection.iterdump()), queued)

        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        running = tuple(self.store._connection.iterdump())
        for lease_seconds in (1e20, 10**400):
            with self.subTest(operation="heartbeat", lease_seconds=type(lease_seconds).__name__):
                with self.assertRaisesRegex(ValueError, "supported datetime range"):
                    self.store.heartbeat(lease, lease_seconds=lease_seconds)
                self.assertEqual(tuple(self.store._connection.iterdump()), running)

    def test_enqueue_reconciles_committed_acknowledgement_failure(self):
        with self._commit_then_raise():
            queued = self.store.enqueue(job_spec())

        self.assertEqual(queued, self.store.get("invocation-1"))
        self.assertEqual(queued.status, InvocationStatus.QUEUED)

    def test_claim_reconciles_committed_acknowledgement_failure(self):
        self.store.enqueue(job_spec())

        with self._commit_then_raise():
            lease = self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertIsNotNone(lease)
        assert lease is not None
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(snapshot.job.status, InvocationStatus.RUNNING)
        self.assertEqual(snapshot.current_attempt.attempt_id, lease.attempt_id)
        self.assertEqual(
            snapshot.current_attempt.lease_token_digest,
            hashlib.sha256(lease.lease_token.encode("utf-8")).hexdigest(),
        )

    def test_claim_reconciles_committed_base_exception_acknowledgement(self):
        class CommitAcknowledgementAbort(BaseException):
            pass

        self.store.enqueue(job_spec())
        commit = self.store._commit_write_transaction

        def commit_then_abort(connection):
            commit(connection)
            raise CommitAcknowledgementAbort("must not escape after durable claim")

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=commit_then_abort,
        ):
            lease = self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertIsNotNone(lease)
        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.RUNNING)

    def test_claim_readback_rejects_postcommit_heartbeat_and_deadline_drift(self):
        for index, control in enumerate((False, True)):
            with self.subTest(control=control):
                path = str(Path(self.tempdir.name) / f"claim-readback-drift-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                store.enqueue(job_spec())
                marker = f"private drifted claim acknowledgement at {path}"
                commit = store._commit_write_transaction

                def commit_drift_then_fail(
                    connection,
                    commit=commit,
                    control=control,
                    marker=marker,
                ):
                    commit(connection)
                    connection.execute(
                        """
                        UPDATE invocation_jobs
                        SET heartbeat_at = ?, updated_at = ?, lease_expires_at = ?
                        WHERE invocation_id = 'invocation-1'
                        """,
                        (
                            persisted_timestamp(1),
                            persisted_timestamp(1),
                            persisted_timestamp(11),
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE invocation_attempts
                        SET heartbeat_at = ?, lease_expires_at = ?
                        WHERE invocation_id = 'invocation-1'
                        """,
                        (persisted_timestamp(1), persisted_timestamp(11)),
                    )
                    if control:
                        raise KeyboardInterrupt(marker)
                    raise HostileFault(marker)

                try:
                    with patch.object(
                        store,
                        "_commit_write_transaction",
                        side_effect=commit_drift_then_fail,
                    ):
                        captured = self._capture_base_exception(
                            partial(
                                store.claim,
                                "invocation-1",
                                "worker",
                                lease_seconds=5,
                            )
                        )

                    if control:
                        self._assert_clean_control_signal(
                            captured,
                            KeyboardInterrupt,
                            marker=marker,
                            ambiguity=True,
                        )
                        with self.assertRaises(InvocationStorePoisonedError):
                            store.schema_version()
                    else:
                        self.assertIs(type(captured), InvocationCommitAmbiguityError)
                        self.assertNotIn(marker, exception_graph_text(captured))
                        self.assertEqual(store.schema_version(), 2)
                    reopened = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                    try:
                        job = reopened.get("invocation-1")
                        attempt = reopened.attempts("invocation-1")[-1]
                        self.assertEqual(job.heartbeat_at, persisted_timestamp(1))
                        self.assertEqual(job.lease_expires_at, persisted_timestamp(11))
                        self.assertEqual(attempt.heartbeat_at, persisted_timestamp(1))
                        self.assertEqual(attempt.lease_expires_at, persisted_timestamp(11))
                    finally:
                        reopened.close()
                finally:
                    store.close()

    def test_heartbeat_reconciles_committed_acknowledgement_failure(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.clock.set(timestamp(5))

        with self._commit_then_raise():
            accepted = self.store.heartbeat(lease, lease_seconds=20)

        self.assertTrue(accepted)
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(snapshot.job.heartbeat_at, persisted_timestamp(5))
        self.assertEqual(snapshot.current_attempt.heartbeat_at, persisted_timestamp(5))
        self.assertEqual(snapshot.job.lease_expires_at, persisted_timestamp(25))

    def test_complete_reconciles_committed_acknowledgement_failure(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.clock.set(timestamp(5))

        with self._commit_then_raise():
            accepted = self.store.complete(lease, result_ref="result:committed")

        self.assertTrue(accepted)
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(snapshot.job.status, InvocationStatus.SUCCEEDED)
        self.assertEqual(snapshot.job.result_ref, "result:committed")
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.SUCCEEDED)
        self.assertEqual(snapshot.current_attempt.finished_at, persisted_timestamp(5))

    def test_fail_reconciles_committed_acknowledgement_failure(self):
        self.store.enqueue(job_spec(max_attempts=1))
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.clock.set(timestamp(5))

        with self._commit_then_raise():
            accepted = self.store.fail(lease, "terminal committed failure")

        self.assertTrue(accepted)
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(snapshot.job.status, InvocationStatus.FAILED)
        self.assertEqual(snapshot.job.last_error, "terminal committed failure")
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.FAILED)
        self.assertEqual(snapshot.current_attempt.finished_at, persisted_timestamp(5))

    def test_recovery_reconciles_committed_acknowledgement_failure(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=5)
        self.clock.set(timestamp(5))

        with self._commit_then_raise():
            summary = self.store.recover_expired()

        self.assertEqual(summary.requeued, ("invocation-1",))
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(snapshot.job.status, InvocationStatus.QUEUED)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.EXPIRED)
        self.assertEqual(snapshot.current_attempt.finished_at, persisted_timestamp(5))

    def test_noop_claim_acknowledgement_failure_returns_none(self):
        with self._commit_then_raise():
            lease = self.store.claim_next("worker", lease_seconds=10)

        self.assertIsNone(lease)

    def test_claim_reconciles_expiry_without_a_new_owner(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker-1", lease_seconds=5)
        self.clock.set(timestamp(5))

        with self._commit_then_raise():
            lease = self.store.claim("invocation-1", "worker-2", lease_seconds=10)

        self.assertIsNone(lease)
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(snapshot.job.status, InvocationStatus.QUEUED)
        self.assertEqual(snapshot.current_attempt.status, AttemptStatus.EXPIRED)

    def test_uncommitted_claim_raises_stable_commit_ambiguity(self):
        self.store.enqueue(job_spec())
        queued = tuple(self.store._connection.iterdump())

        with self._rollback_then_raise():
            with self.assertRaisesRegex(
                InvocationCommitAmbiguityError,
                "commit could not be reconciled",
            ) as captured:
                self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(captured.exception.code, "invocation_commit_ambiguous")
        chain = []
        current = captured.exception
        while current is not None:
            chain.append(str(current))
            current = current.__cause__ or current.__context__
        self.assertNotIn("ambiguous rolled-back commit", " ".join(chain))
        self.assertEqual(tuple(self.store._connection.iterdump()), queued)
        self.assertEqual(self.store.attempts("invocation-1"), ())

    def test_uncommitted_owned_mutations_never_return_false_success(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        running = tuple(self.store._connection.iterdump())

        for operation in (
            lambda: self.store.heartbeat(lease, lease_seconds=20),
            lambda: self.store.complete(lease, result_ref="result:rolled-back"),
            lambda: self.store.fail(lease, "rolled-back failure"),
        ):
            with self.subTest(operation=operation):
                with self._rollback_then_raise():
                    with self.assertRaisesRegex(
                        InvocationCommitAmbiguityError,
                        "commit could not be reconciled",
                    ):
                        operation()
                self.assertEqual(tuple(self.store._connection.iterdump()), running)

    def test_commit_failure_with_confirmed_rollback_is_typed_and_sanitized(self):
        marker = f"private precommit SQL/path {self.path}"
        empty = tuple(self.store._connection.iterdump())

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=HostileFault(marker),
        ):
            with self.assertRaises(InvocationTransactionError) as captured:
                self.store.enqueue(job_spec())

        rendered = "".join(
            traceback.TracebackException.from_exception(captured.exception).format(chain=True)
        )
        self.assertNotIn(marker, rendered)
        self.assertNotIn(marker, exception_graph_text(captured.exception))
        self.assertEqual(captured.exception.code, "invocation_transaction_failed")
        self.assertEqual(tuple(self.store._connection.iterdump()), empty)
        self.assertIsNone(self.store.get("invocation-1"))

    def test_confirmed_rollback_sanitizes_every_public_mutator(self):
        cases = (
            "enqueue",
            "claim",
            "claim-next",
            "recover",
            "heartbeat",
            "complete",
            "fail",
        )
        for index, kind in enumerate(cases):
            with self.subTest(kind=kind):
                clock = MutableClock()
                path = str(Path(self.tempdir.name) / f"precommit-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=clock)
                lease = None
                try:
                    if kind != "enqueue":
                        maximum = 1 if kind == "fail" else 3
                        store.enqueue(job_spec(max_attempts=maximum))
                    if kind in {"recover", "heartbeat", "complete", "fail"}:
                        lease = store.claim("invocation-1", "worker", lease_seconds=5)
                        assert lease is not None
                    if kind == "enqueue":
                        operation = partial(store.enqueue, job_spec())
                    elif kind == "claim":
                        operation = partial(
                            store.claim,
                            "invocation-1",
                            "worker",
                            lease_seconds=5,
                        )
                    elif kind == "claim-next":
                        operation = partial(store.claim_next, "worker", lease_seconds=5)
                    elif kind == "recover":
                        clock.set(timestamp(5))
                        operation = store.recover_expired
                    elif kind == "heartbeat":
                        clock.set(timestamp(1))
                        operation = partial(store.heartbeat, lease, lease_seconds=5)
                    elif kind == "complete":
                        operation = partial(store.complete, lease, result_ref="result:rollback")
                    else:
                        operation = partial(store.fail, lease, "rollback failure")

                    marker = f"private {kind} driver state at {path}"
                    if lease is not None:
                        marker = f"{marker} token={lease.lease_token}"
                    before = tuple(store._connection.iterdump())
                    with patch.object(
                        store,
                        "_commit_write_transaction",
                        side_effect=HostileFault(marker),
                    ):
                        with self.assertRaises(InvocationTransactionError) as captured:
                            operation()

                    rendered = "".join(
                        traceback.TracebackException.from_exception(captured.exception).format(
                            chain=True
                        )
                    )
                    self.assertNotIn(marker, rendered)
                    graph = exception_graph_text(captured.exception)
                    self.assertNotIn(marker, graph)
                    if lease is not None:
                        self.assertNotIn(lease.lease_token, graph)
                    self.assertEqual(tuple(store._connection.iterdump()), before)
                    self.assertEqual(store.schema_version(), 2)
                finally:
                    store.close()

    def test_every_public_mutator_reissues_clean_controls_from_precommit_stages(self):
        kinds = (
            "enqueue",
            "claim",
            "claim-next",
            "recover",
            "heartbeat",
            "complete",
            "fail",
        )
        stages = (
            ("begin", "_begin_write_transaction", KeyboardInterrupt),
            ("body", "_clock", GeneratorExit),
            ("commit", "_commit_write_transaction", CancelledError),
        )
        case_index = 0
        for kind in kinds:
            for stage, fault_name, error_type in stages:
                with self.subTest(kind=kind, stage=stage):
                    store, operation, lease = self._public_mutator_case(
                        kind,
                        case_index,
                        "control-precommit",
                    )
                    case_index += 1
                    try:
                        marker = f"private {kind} {stage} path={store.path}"
                        if lease is not None:
                            marker = f"{marker} token={lease.lease_token}"
                        failure = self._hostile_control_signal(error_type(marker), marker)
                        before = tuple(store._connection.iterdump())
                        with patch.object(store, fault_name, side_effect=failure):
                            cleaned = self._capture_base_exception(operation)
                        self._assert_clean_control_signal(
                            cleaned,
                            error_type,
                            marker=marker,
                        )
                        self.assertEqual(tuple(store._connection.iterdump()), before)
                        self.assertEqual(store.schema_version(), 2)
                    finally:
                        store.close()

    def test_every_public_mutator_sanitizes_ordinary_begin_and_body_faults(self):
        kinds = (
            "enqueue",
            "claim",
            "claim-next",
            "recover",
            "heartbeat",
            "complete",
            "fail",
        )
        case_index = 0
        for kind in kinds:
            for stage, fault_name in (
                ("begin", "_begin_write_transaction"),
                ("body", "_clock"),
            ):
                with self.subTest(kind=kind, stage=stage):
                    store, operation, lease = self._public_mutator_case(
                        kind,
                        case_index,
                        "ordinary-precommit",
                    )
                    case_index += 1
                    try:
                        marker = f"private {kind} {stage} SQL/path={store.path}"
                        if lease is not None:
                            marker = f"{marker} token={lease.lease_token}"
                        before = tuple(store._connection.iterdump())
                        with patch.object(
                            store,
                            fault_name,
                            side_effect=HostileFault(marker),
                        ):
                            captured = self._capture_base_exception(operation)
                        self.assertIs(type(captured), InvocationTransactionError)
                        self.assertEqual(captured.code, "invocation_transaction_failed")
                        self.assertNotIn(marker, exception_graph_text(captured))
                        module_frames = []
                        cursor = captured.__traceback__
                        while cursor is not None:
                            if (
                                cursor.tb_frame.f_globals.get("__name__")
                                == attempts_module.__name__
                            ):
                                module_frames.append(cursor.tb_frame)
                            cursor = cursor.tb_next
                        self.assertEqual(
                            [frame.f_code.co_name for frame in module_frames],
                            ["wrapped", "_raise_clean_fixed_public_error"],
                        )
                        self.assertFalse(
                            {"args", "kwargs", "store", "error"} & set(module_frames[0].f_locals)
                        )
                        self.assertEqual(tuple(store._connection.iterdump()), before)
                        self.assertEqual(store.schema_version(), 2)
                    finally:
                        store.close()

    def test_spoofed_module_name_cannot_authorize_a_body_error_message(self):
        marker = "private spoofed module ValueError"
        namespace = {"__name__": attempts_module.__name__}
        exec(
            compile(
                "def hostile_clock():\n    raise ValueError(secret_marker)\n",
                "<hostile-provider>",
                "exec",
            ),
            namespace,
        )
        namespace["secret_marker"] = marker
        store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        store._clock = namespace["hostile_clock"]
        try:
            captured = self._capture_base_exception(partial(store.enqueue, job_spec()))
            self.assertIs(type(captured), InvocationTransactionError)
            self.assertNotIn(marker, exception_graph_text(captured))
            self.assertIsNone(store.get("invocation-1"))
        finally:
            store.close()

    def test_grafted_trusted_traceback_cannot_authorize_external_validation_error(self):
        marker = "private grafted validation object"
        external = RuntimeError(marker)
        external.secret = marker
        external.__notes__ = [marker]

        def hostile_clock():
            try:
                attempts_module._normalize_timestamp("not-a-timestamp")
            except ValueError as trusted:
                forged = ValueError(external)
                forged.secret = external
                forged.__notes__ = [marker]
                forged.__cause__ = external
                forged.__traceback__ = trusted.__traceback__
                raise forged from external

        store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
        store._clock = hostile_clock
        try:
            captured = self._capture_base_exception(partial(store.enqueue, job_spec()))

            self.assertIs(type(captured), InvocationTransactionError)
            self.assertEqual(
                captured.args,
                ("invocation mutation transaction was rolled back",),
            )
            self.assertEqual(getattr(captured, "__dict__", {}), {})
            self.assertFalse(getattr(captured, "__notes__", ()))
            self.assertIsNone(captured.__cause__)
            self.assertIsNone(captured.__context__)
            self.assertNotIn(marker, exception_graph_text(captured))

            frames = []
            cursor = captured.__traceback__
            while cursor is not None:
                frames.append(cursor.tb_frame)
                cursor = cursor.tb_next
            self.assertNotIn(hostile_clock.__code__, {frame.f_code for frame in frames})
            self.assertNotIn(marker, " ".join(repr(frame.f_locals) for frame in frames))
            self.assertEqual(
                [
                    frame.f_code.co_name
                    for frame in frames
                    if frame.f_globals.get("__name__") == attempts_module.__name__
                ],
                ["wrapped", "_raise_clean_fixed_public_error"],
            )
            self.assertEqual(store.schema_version(), 2)
            self.assertIsNone(store.get("invocation-1"))
        finally:
            store.close()

    def test_library_validation_is_reissued_outside_catch_with_a_clean_graph(self):
        self.store.enqueue(job_spec())
        captured = self._capture_base_exception(
            partial(
                self.store.claim,
                "invocation-1",
                "worker",
                lease_seconds=0,
            )
        )

        self.assertIs(type(captured), ValueError)
        self.assertEqual(
            captured.args,
            ("lease_seconds must be finite and greater than zero",),
        )
        self.assertEqual(getattr(captured, "__dict__", {}), {})
        self.assertFalse(getattr(captured, "__notes__", ()))
        self.assertIsNone(captured.__cause__)
        self.assertIsNone(captured.__context__)

        module_frames = []
        cursor = captured.__traceback__
        while cursor is not None:
            if cursor.tb_frame.f_globals.get("__name__") == attempts_module.__name__:
                module_frames.append(cursor.tb_frame)
            cursor = cursor.tb_next
        self.assertEqual(
            [frame.f_code.co_name for frame in module_frames],
            ["wrapped", "_raise_clean_transaction_body_error"],
        )
        self.assertFalse({"args", "kwargs", "store", "error"} & set(module_frames[0].f_locals))
        self.assertEqual(self.store.attempts("invocation-1"), ())

    def test_every_public_mutator_reissues_control_after_exact_commit_readback(self):
        kinds = (
            "enqueue",
            "claim",
            "claim-next",
            "recover",
            "heartbeat",
            "complete",
            "fail",
        )
        for index, kind in enumerate(kinds):
            with self.subTest(kind=kind):
                store, operation, _lease = self._public_mutator_case(
                    kind,
                    index,
                    "control-postcommit",
                )
                try:
                    marker = f"private {kind} committed acknowledgement at {store.path}"
                    failure = self._hostile_control_signal(GeneratorExit(marker), marker)
                    commit = store._commit_write_transaction

                    def committed_control(connection, commit=commit, failure=failure):
                        commit(connection)
                        raise failure

                    with patch.object(
                        store,
                        "_commit_write_transaction",
                        side_effect=committed_control,
                    ):
                        cleaned = self._capture_base_exception(operation)
                    self._assert_clean_control_signal(
                        cleaned,
                        GeneratorExit,
                        marker=marker,
                    )
                    self._assert_committed_mutator_state(kind, store)
                    self.assertEqual(store.schema_version(), 2)
                finally:
                    store.close()

    def test_postcommit_controls_are_reissued_for_every_noop_return_path(self):
        kinds = (
            "enqueue",
            "claim",
            "claim-next",
            "recover",
            "heartbeat",
            "complete",
            "fail",
        )
        for index, kind in enumerate(kinds):
            with self.subTest(kind=kind):
                path = str(Path(self.tempdir.name) / f"control-noop-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                try:
                    if kind == "enqueue":
                        store.enqueue(job_spec())
                        operation = partial(store.enqueue, job_spec())
                    elif kind == "claim":
                        operation = partial(
                            store.claim,
                            "missing-invocation",
                            "worker",
                            lease_seconds=5,
                        )
                    elif kind == "claim-next":
                        operation = partial(store.claim_next, "worker", lease_seconds=5)
                    elif kind == "recover":
                        operation = store.recover_expired
                    else:
                        store.enqueue(job_spec())
                        lease = store.claim("invocation-1", "worker", lease_seconds=5)
                        assert lease is not None
                        stale = replace(lease, lease_token="stale-token")
                        if kind == "heartbeat":
                            operation = partial(store.heartbeat, stale, lease_seconds=5)
                        elif kind == "complete":
                            operation = partial(store.complete, stale)
                        else:
                            operation = partial(store.fail, stale, "stale failure")

                    before = tuple(store._connection.iterdump())
                    marker = f"private no-op {kind} acknowledgement at {path}"
                    failure = self._hostile_control_signal(KeyboardInterrupt(marker), marker)
                    commit = store._commit_write_transaction

                    def committed_control(connection, commit=commit, failure=failure):
                        commit(connection)
                        raise failure

                    with patch.object(
                        store,
                        "_commit_write_transaction",
                        side_effect=committed_control,
                    ):
                        cleaned = self._capture_base_exception(operation)
                    self._assert_clean_control_signal(
                        cleaned,
                        KeyboardInterrupt,
                        marker=marker,
                    )
                    self.assertEqual(tuple(store._connection.iterdump()), before)
                    self.assertEqual(store.schema_version(), 2)
                finally:
                    store.close()

    def test_noop_claim_next_commit_error_is_typed_after_confirmed_rollback(self):
        marker = "private no-op claim-next commit error"
        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=HostileFault(marker),
        ):
            with self.assertRaises(InvocationTransactionError) as captured:
                self.store.claim_next("worker", lease_seconds=5)

        self.assertNotIn(marker, exception_graph_text(captured.exception))
        self.assertIsNone(self.store.get("invocation-1"))
        self.assertEqual(self.store.schema_version(), 2)

    def test_direct_transaction_boundary_sanitizes_a_hostile_commit_error(self):
        marker = "private direct transaction driver error"
        before = tuple(self.store._connection.iterdump())
        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=HostileFault(marker),
        ):
            with self.assertRaises(InvocationTransactionError) as captured:
                with self.store._transaction():
                    pass

        self.assertNotIn(marker, exception_graph_text(captured.exception))
        self.assertEqual(tuple(self.store._connection.iterdump()), before)

    def test_owned_and_recovery_committed_controls_reconcile_then_reissue(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=5)
        assert lease is not None
        self.clock.set(timestamp(1))
        heartbeat_commit = self.store._commit_write_transaction
        heartbeat_marker = "private heartbeat acknowledgement interruption"

        def committed_heartbeat_interrupt(connection):
            heartbeat_commit(connection)
            raise self._hostile_control_signal(SystemExit(heartbeat_marker), heartbeat_marker)

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=committed_heartbeat_interrupt,
        ):
            heartbeat_error = self._capture_base_exception(
                partial(self.store.heartbeat, lease, lease_seconds=5)
            )

        self._assert_clean_control_signal(
            heartbeat_error,
            SystemExit,
            marker=heartbeat_marker,
            system_exit_code=1,
        )

        heartbeat = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(heartbeat.job.heartbeat_at, persisted_timestamp(1))
        self.assertEqual(heartbeat.job.lease_expires_at, persisted_timestamp(6))

        self.clock.set(timestamp(6))
        recovery_commit = self.store._commit_write_transaction
        recovery_marker = "private recovery acknowledgement interruption"

        def committed_recovery_interrupt(connection):
            recovery_commit(connection)
            raise self._hostile_control_signal(
                KeyboardInterrupt(recovery_marker),
                recovery_marker,
            )

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=committed_recovery_interrupt,
        ):
            recovery_error = self._capture_base_exception(self.store.recover_expired)

        self._assert_clean_control_signal(
            recovery_error,
            KeyboardInterrupt,
            marker=recovery_marker,
        )
        recovered = self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(recovered.job.status, InvocationStatus.QUEUED)
        self.assertEqual(recovered.current_attempt.status, AttemptStatus.EXPIRED)

    def test_non_control_base_exception_is_sanitized_after_confirmed_rollback(self):
        marker = "private non-control BaseException"
        failure = HostileBaseFault(marker)
        empty = tuple(self.store._connection.iterdump())
        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=failure,
        ):
            with self.assertRaises(InvocationTransactionError) as captured:
                self.store.enqueue(job_spec())

        self.assertNotIn(marker, exception_graph_text(captured.exception))
        self.assertEqual(tuple(self.store._connection.iterdump()), empty)
        self.assertIsNone(self.store.get("invocation-1"))

    def test_control_base_exceptions_are_reissued_clean_after_confirmed_rollback(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=5)
        assert lease is not None
        before = tuple(self.store._connection.iterdump())
        control_errors = (
            (KeyboardInterrupt, KeyboardInterrupt("keyboard cancellation"), None),
            (SystemExit, SystemExit("system exit"), 1),
            (GeneratorExit, GeneratorExit("generator exit"), None),
            (CancelledError, CancelledError("async cancellation"), None),
        )
        for error_type, failure, safe_code in control_errors:
            with self.subTest(error_type=error_type.__name__):
                marker = f"private {error_type.__name__} token={lease.lease_token}"
                failure = self._hostile_control_signal(failure, marker)
                with patch.object(
                    self.store,
                    "_commit_write_transaction",
                    side_effect=failure,
                ):
                    cleaned = self._capture_base_exception(
                        partial(self.store.heartbeat, lease, lease_seconds=5)
                    )

                self.assertIsNot(cleaned, failure)
                self._assert_clean_control_signal(
                    cleaned,
                    error_type,
                    marker=marker,
                    system_exit_code=safe_code,
                )
                self.assertNotIn(lease.lease_token, exception_graph_text(cleaned))
                self.assertEqual(tuple(self.store._connection.iterdump()), before)

    def test_originating_exact_controls_outrank_cleanup_controls(self):
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit)

        def assert_origin(cleaned, error_type, markers):
            self._assert_clean_control_signal(
                cleaned,
                error_type,
                marker=markers[0],
                system_exit_code=(1 if error_type is SystemExit else None),
                ambiguity=True,
            )
            graph = exception_graph_text(cleaned)
            for marker in markers:
                self.assertNotIn(marker, graph)

        for index, error_type in enumerate(control_types):
            cleanup_type = control_types[(index + 1) % len(control_types)]
            close_type = control_types[(index + 2) % len(control_types)]

            with self.subTest(stage="write-rollback-close", error_type=error_type.__name__):
                markers = (
                    f"private originating write {error_type.__name__}",
                    f"private write rollback {cleanup_type.__name__}",
                    f"private write close {close_type.__name__}",
                )
                store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
                try:
                    with (
                        patch.object(
                            store,
                            "_commit_write_transaction",
                            side_effect=error_type(markers[0]),
                        ),
                        patch.object(
                            store,
                            "_rollback_write_transaction",
                            side_effect=cleanup_type(markers[1]),
                        ),
                        patch.object(
                            store,
                            "_close_connection",
                            side_effect=close_type(markers[2]),
                        ),
                    ):
                        cleaned = self._capture_base_exception(partial(store.enqueue, job_spec()))
                    assert_origin(cleaned, error_type, markers)
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                finally:
                    store.close()

            with self.subTest(stage="state-inspection-close", error_type=error_type.__name__):
                markers = (
                    f"private originating begin {error_type.__name__}",
                    f"private state inspection {cleanup_type.__name__}",
                    f"private state close {close_type.__name__}",
                )
                store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
                try:
                    with (
                        patch.object(
                            store,
                            "_begin_write_transaction",
                            side_effect=error_type(markers[0]),
                        ),
                        patch.object(
                            store,
                            "_write_transaction_open",
                            side_effect=cleanup_type(markers[1]),
                        ),
                        patch.object(
                            store,
                            "_close_connection",
                            side_effect=close_type(markers[2]),
                        ),
                    ):
                        cleaned = self._capture_base_exception(partial(store.enqueue, job_spec()))
                    assert_origin(cleaned, error_type, markers)
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                finally:
                    store.close()

            with self.subTest(stage="readback-close", error_type=error_type.__name__):
                markers = (
                    f"private originating commit {error_type.__name__}",
                    f"private readback {cleanup_type.__name__}",
                    f"private readback close {close_type.__name__}",
                )
                store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
                commit = store._commit_write_transaction

                def committed_control(
                    connection,
                    commit=commit,
                    marker=markers[0],
                    error_type=error_type,
                ):
                    commit(connection)
                    raise error_type(marker)

                try:
                    with (
                        patch.object(
                            store,
                            "_commit_write_transaction",
                            side_effect=committed_control,
                        ),
                        patch.object(
                            store,
                            "_reconcile_enqueued_job",
                            side_effect=cleanup_type(markers[1]),
                        ),
                        patch.object(
                            store,
                            "_close_connection",
                            side_effect=close_type(markers[2]),
                        ),
                    ):
                        cleaned = self._capture_base_exception(partial(store.enqueue, job_spec()))
                    assert_origin(cleaned, error_type, markers)
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                finally:
                    store.close()

            with self.subTest(stage="read-rollback-close", error_type=error_type.__name__):
                markers = (
                    f"private originating read {error_type.__name__}",
                    f"private read rollback {cleanup_type.__name__}",
                    f"private read close {close_type.__name__}",
                )
                store = SQLiteInvocationAttemptStore(":memory:", clock=MutableClock())
                store.enqueue(job_spec())
                try:
                    with (
                        patch.object(
                            store,
                            "_row_to_job",
                            side_effect=error_type(markers[0]),
                        ),
                        patch.object(
                            store,
                            "_rollback_read_transaction",
                            side_effect=cleanup_type(markers[1]),
                        ),
                        patch.object(
                            store,
                            "_close_connection",
                            side_effect=close_type(markers[2]),
                        ),
                    ):
                        cleaned = self._capture_base_exception(
                            partial(
                                store.recovery_snapshot_for_task,
                                "session-1",
                                "task-1",
                            )
                        )
                    assert_origin(cleaned, error_type, markers)
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                finally:
                    store.close()

    def test_system_exit_codes_are_reduced_to_safe_exact_scalars(self):
        class IntSubclass(int):
            pass

        cases = (
            (None, None),
            (False, False),
            (True, True),
            (0, 0),
            (255, 255),
            ("private exit text", 1),
            (-1, 1),
            (256, 1),
            (10**100, 1),
            (IntSubclass(7), 1),
        )
        for index, (code, expected) in enumerate(cases):
            with self.subTest(index=index, code_type=type(code).__name__):
                marker = f"private SystemExit code case {index}"
                failure = self._hostile_control_signal(SystemExit(code), marker)
                with patch.object(
                    self.store,
                    "_begin_write_transaction",
                    side_effect=failure,
                ):
                    cleaned = self._capture_base_exception(partial(self.store.enqueue, job_spec()))
                self.assertIs(type(cleaned.code), type(expected))
                self._assert_clean_control_signal(
                    cleaned,
                    SystemExit,
                    marker=marker,
                    system_exit_code=expected,
                )
                self.assertIsNone(self.store.get("invocation-1"))

    def test_control_subclasses_are_not_treated_as_trusted_control_signals(self):
        subclass_types = (
            type("KeyboardInterruptSubclass", (KeyboardInterrupt,), {}),
            type("SystemExitSubclass", (SystemExit,), {}),
            type("GeneratorExitSubclass", (GeneratorExit,), {}),
            type("CancelledErrorSubclass", (CancelledError,), {}),
        )
        for index, error_type in enumerate(subclass_types):
            with self.subTest(error_type=error_type.__name__):
                path = str(Path(self.tempdir.name) / f"control-subclass-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                marker = f"private control subclass {error_type.__name__} at {path}"
                try:
                    with patch.object(
                        store,
                        "_begin_write_transaction",
                        side_effect=error_type(marker),
                    ):
                        transaction_error = self._capture_base_exception(
                            partial(store.enqueue, job_spec())
                        )
                    self.assertIs(type(transaction_error), InvocationTransactionError)
                    self.assertNotIn(marker, exception_graph_text(transaction_error))

                    with patch.object(
                        store,
                        "_close_connection",
                        side_effect=error_type(marker),
                    ):
                        close_error = self._capture_base_exception(store.close)
                    self.assertIs(type(close_error), InvocationStoreClosedError)
                    self.assertNotIn(marker, exception_graph_text(close_error))
                finally:
                    store.close()

    def test_base_exception_group_is_not_a_trusted_control_signal(self):
        group_type = getattr(builtins, "BaseExceptionGroup", None)
        if group_type is None:
            self.skipTest("BaseExceptionGroup requires Python 3.11+")
        marker = "private grouped KeyboardInterrupt"
        failure = group_type(marker, [KeyboardInterrupt(marker)])
        with patch.object(
            self.store,
            "_begin_write_transaction",
            side_effect=failure,
        ):
            captured = self._capture_base_exception(partial(self.store.enqueue, job_spec()))
        self.assertIs(type(captured), InvocationTransactionError)
        self.assertNotIn(marker, exception_graph_text(captured))

    def test_transaction_state_inspection_faults_fail_closed_without_leakage(self):
        stages = ("begin", "commit")
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                path = str(Path(self.tempdir.name) / f"state-inspection-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                driver_marker = f"private {stage} driver state at {path}"
                inspection_marker = f"private {stage} transaction inspection at {path}"
                fault_name = (
                    "_begin_write_transaction" if stage == "begin" else "_commit_write_transaction"
                )
                try:
                    with (
                        patch.object(
                            store,
                            fault_name,
                            side_effect=HostileFault(driver_marker),
                        ),
                        patch.object(
                            store,
                            "_write_transaction_open",
                            side_effect=HostileFault(inspection_marker),
                        ),
                    ):
                        captured = self._capture_base_exception(partial(store.enqueue, job_spec()))

                    self.assertIs(type(captured), InvocationCommitAmbiguityError)
                    graph = exception_graph_text(captured)
                    self.assertNotIn(driver_marker, graph)
                    self.assertNotIn(inspection_marker, graph)
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                finally:
                    store.close()

                reopened = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                try:
                    self.assertIsNone(reopened.get("invocation-1"))
                finally:
                    reopened.close()

    def test_non_exact_transaction_state_is_never_truth_tested_and_poisons(self):
        class TruthBomb:
            def __init__(self, marker):
                self.marker = marker
                self.evaluated = False

            def __bool__(self):
                self.evaluated = True
                raise HostileFault(self.marker)

            def __repr__(self):
                return f"TruthBomb({self.marker})"

        case_index = 0
        for stage in ("begin", "commit"):
            for state_kind in ("none", "zero", "one", "truth-bomb"):
                with self.subTest(stage=stage, state_kind=state_kind):
                    path = str(
                        Path(self.tempdir.name) / f"invalid-transaction-state-{case_index}.sqlite3"
                    )
                    case_index += 1
                    marker = f"private {stage} {state_kind} state at {path}"
                    driver_marker = f"private {stage} driver fault at {path}"
                    if state_kind == "none":
                        state = None
                    elif state_kind == "zero":
                        state = 0
                    elif state_kind == "one":
                        state = 1
                    else:
                        state = TruthBomb(marker)
                    store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                    fault_name = (
                        "_begin_write_transaction"
                        if stage == "begin"
                        else "_commit_write_transaction"
                    )
                    try:
                        with (
                            patch.object(
                                store,
                                fault_name,
                                side_effect=HostileFault(driver_marker),
                            ),
                            patch.object(
                                store,
                                "_write_transaction_open",
                                return_value=state,
                            ),
                        ):
                            captured = self._capture_base_exception(
                                partial(store.enqueue, job_spec())
                            )

                        self.assertIs(type(captured), InvocationCommitAmbiguityError)
                        self.assertEqual(
                            captured.args,
                            ("invocation mutation commit could not be reconciled",),
                        )
                        self.assertEqual(getattr(captured, "__dict__", {}), {})
                        self.assertFalse(getattr(captured, "__notes__", ()))
                        self.assertIsNone(captured.__cause__)
                        self.assertIsNone(captured.__context__)
                        graph = exception_graph_text(captured)
                        self.assertNotIn(marker, graph)
                        self.assertNotIn(driver_marker, graph)
                        if isinstance(state, TruthBomb):
                            self.assertFalse(state.evaluated)
                        with self.assertRaises(InvocationStorePoisonedError):
                            store.schema_version()
                    finally:
                        store.close()

                    reopened = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                    try:
                        self.assertIsNone(reopened.get("invocation-1"))
                    finally:
                        reopened.close()

    def test_exact_bool_transaction_state_preserves_confirmed_outcomes(self):
        cases = (
            ("begin-closed", "begin", False, False),
            ("begin-open", "begin", True, False),
            ("commit-open", "commit", True, False),
            ("commit-ended", "commit", False, True),
        )
        for index, (name, stage, transaction_open, durable) in enumerate(cases):
            with self.subTest(name=name):
                path = str(Path(self.tempdir.name) / f"exact-bool-state-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                marker = f"private exact bool {name} at {path}"

                def fail_boundary(connection, name=name, marker=marker):
                    if name == "begin-open":
                        connection.execute("BEGIN IMMEDIATE")
                    elif name == "commit-ended":
                        connection.execute("COMMIT")
                    raise HostileFault(marker)

                fault_name = (
                    "_begin_write_transaction" if stage == "begin" else "_commit_write_transaction"
                )
                try:
                    with (
                        patch.object(store, fault_name, side_effect=fail_boundary),
                        patch.object(
                            store,
                            "_write_transaction_open",
                            return_value=transaction_open,
                        ),
                    ):
                        if durable:
                            result = store.enqueue(job_spec())
                        else:
                            captured = self._capture_base_exception(
                                partial(store.enqueue, job_spec())
                            )

                    if durable:
                        self.assertEqual(result.invocation_id, "invocation-1")
                        self.assertEqual(store.get("invocation-1"), result)
                    else:
                        self.assertIs(type(captured), InvocationTransactionError)
                        self.assertNotIn(marker, exception_graph_text(captured))
                        self.assertIsNone(store.get("invocation-1"))
                    self.assertEqual(store.schema_version(), 2)
                finally:
                    store.close()

    def test_forged_internal_transaction_signals_cannot_cross_public_boundary(self):
        descriptor = attempts_module._ControlSignalDescriptor(
            attempts_module._ControlSignalKind.KEYBOARD_INTERRUPT
        )
        cases = (
            (
                "validation",
                attempts_module._InvocationValidationSignal(
                    attempts_module._SafeTransactionBodyError(ValueError, "forged validation"),
                    object(),
                ),
                InvocationTransactionError,
                False,
            ),
            (
                "control",
                attempts_module._InvocationControlSignal(
                    descriptor,
                    object(),
                    ambiguity=False,
                ),
                InvocationTransactionError,
                False,
            ),
            (
                "commit",
                attempts_module._CommitOutcomeUnknown(
                    may_reconcile=True,
                    boundary_nonce=object(),
                    control_signal=descriptor,
                ),
                InvocationCommitAmbiguityError,
                True,
            ),
        )
        for index, (name, forged, expected_type, poisoned) in enumerate(cases):
            with self.subTest(name=name):
                path = str(Path(self.tempdir.name) / f"forged-signal-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=MutableClock())
                marker = f"private forged {name} sentinel at {path}"
                forged.secret = marker

                @contextmanager
                def forged_transaction(forged=forged, store=store):
                    raise forged
                    yield store._connection

                try:
                    with patch.object(store, "_transaction", forged_transaction):
                        captured = self._capture_base_exception(partial(store.enqueue, job_spec()))

                    self.assertIs(type(captured), expected_type)
                    self.assertNotIn(marker, exception_graph_text(captured))
                    if poisoned:
                        with self.assertRaises(InvocationStorePoisonedError):
                            store.schema_version()
                    else:
                        self.assertEqual(store.schema_version(), 2)
                finally:
                    store.close()

    def test_post_end_rollback_base_exception_is_sanitized(self):
        marker = "private rolled-back transaction details"

        def rolled_back_interrupt(connection):
            connection.execute("ROLLBACK")
            raise SystemExit(marker)

        with patch.object(
            self.store,
            "_commit_write_transaction",
            side_effect=rolled_back_interrupt,
        ):
            cleaned = self._capture_base_exception(partial(self.store.enqueue, job_spec()))

        self._assert_clean_control_signal(
            cleaned,
            SystemExit,
            marker=marker,
            system_exit_code=1,
            ambiguity=True,
        )
        with self.assertRaises(InvocationStorePoisonedError):
            self.store.get("invocation-1")
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        try:
            self.assertIsNone(reopened.get("invocation-1"))
        finally:
            reopened.close()

    def test_readback_base_exception_matrix_fails_closed_without_leakage(self):
        cases = (
            "enqueue",
            "claim",
            "heartbeat",
            "complete",
            "fail",
            "recover",
            "claim-recovery-only",
        )
        for index, kind in enumerate(cases):
            with self.subTest(kind=kind):
                clock = MutableClock()
                path = str(Path(self.tempdir.name) / f"readback-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=clock)
                try:
                    lease = None
                    if kind != "enqueue":
                        maximum = 1 if kind == "fail" else 3
                        store.enqueue(job_spec(max_attempts=maximum))
                    if kind in {
                        "heartbeat",
                        "complete",
                        "fail",
                        "recover",
                        "claim-recovery-only",
                    }:
                        lease = store.claim("invocation-1", "worker", lease_seconds=5)
                        assert lease is not None
                    if kind == "enqueue":
                        operation = partial(store.enqueue, job_spec())
                        readback_name = "_reconcile_enqueued_job"
                    elif kind == "claim":
                        operation = partial(
                            store.claim,
                            "invocation-1",
                            "worker",
                            lease_seconds=5,
                        )
                        readback_name = "_lease_snapshot"
                    elif kind == "heartbeat":
                        clock.set(timestamp(1))
                        operation = partial(store.heartbeat, lease, lease_seconds=5)
                        readback_name = "_lease_snapshot"
                    elif kind == "complete":
                        operation = partial(
                            store.complete,
                            lease,
                            result_ref="result:readback",
                        )
                        readback_name = "_lease_snapshot"
                    elif kind == "fail":
                        operation = partial(store.fail, lease, "readback failure")
                        readback_name = "_lease_snapshot"
                    elif kind == "recover":
                        clock.set(timestamp(5))
                        operation = store.recover_expired
                        readback_name = "_recovered_commit_matches"
                    else:
                        clock.set(timestamp(5))
                        operation = partial(
                            store.claim,
                            "invocation-1",
                            "worker-2",
                            lease_seconds=5,
                        )
                        readback_name = "_recovered_commit_matches"

                    commit_marker = f"private {kind} commit acknowledgement"
                    readback_marker = f"private {kind} readback interruption"
                    commit = store._commit_write_transaction

                    def committed_interrupt(connection, commit=commit, marker=commit_marker):
                        commit(connection)
                        raise SystemExit(marker)

                    with (
                        patch.object(
                            store,
                            "_commit_write_transaction",
                            side_effect=committed_interrupt,
                        ),
                        patch.object(
                            store,
                            readback_name,
                            side_effect=KeyboardInterrupt(readback_marker),
                        ),
                    ):
                        cleaned = self._capture_base_exception(operation)

                    self._assert_clean_control_signal(
                        cleaned,
                        SystemExit,
                        marker=commit_marker,
                        system_exit_code=1,
                        ambiguity=True,
                    )
                    graph = exception_graph_text(cleaned)
                    self.assertNotIn(commit_marker, graph)
                    self.assertNotIn(readback_marker, graph)
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                    reopened = SQLiteInvocationAttemptStore(path, clock=clock)
                    try:
                        state_kind = "recover" if kind == "claim-recovery-only" else kind
                        self._assert_committed_mutator_state(state_kind, reopened)
                    finally:
                        reopened.close()
                finally:
                    store.close()

    def test_rollback_failure_poisons_closes_and_sanitizes_the_store(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.assertIsInstance(lease, InvocationLease)
        assert lease is not None
        connection = self.store._connection
        secret_markers = (
            "sensitive-precommit-path",
            f"sensitive-rollback-token-{lease.lease_token}",
        )
        second_spec = job_spec(
            invocation_id="invocation-2",
            session_id="session-2",
            plan_id="plan-2",
            task_id="task-2",
            agent_id="agent-2",
            idempotency_key="invoke:task-2",
        )

        with (
            patch.object(
                self.store,
                "_commit_write_transaction",
                side_effect=KeyboardInterrupt(secret_markers[0]),
            ),
            patch.object(
                self.store,
                "_rollback_write_transaction",
                side_effect=SystemExit(secret_markers[1]),
            ),
        ):
            cleaned = self._capture_base_exception(partial(self.store.enqueue, second_spec))

        self._assert_clean_control_signal(
            cleaned,
            KeyboardInterrupt,
            marker=secret_markers[0],
            ambiguity=True,
        )
        for marker in secret_markers:
            self.assertNotIn(marker, exception_graph_text(cleaned))
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

        rejected_operations = (
            lambda: self.store.__enter__(),
            lambda: self.store.enqueue(second_spec),
            lambda: self.store.get("invocation-1"),
            lambda: self.store.get_for_task("session-1", "task-1"),
            lambda: self.store.recovery_snapshot_for_task("session-1", "task-1"),
            lambda: self.store.attempts("invocation-1"),
            lambda: self.store.attempts_page("invocation-1"),
            lambda: self.store.recover_expired(),
            lambda: self.store.claim_next("worker-2", lease_seconds=10),
            lambda: self.store.claim("invocation-1", "worker-2", lease_seconds=10),
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must be rejected"),
            lambda: self.store.schema_version(),
        )
        for operation in rejected_operations:
            with self.subTest(operation=operation):
                with self.assertRaises(InvocationStorePoisonedError) as poisoned:
                    operation()
                self.assertEqual(poisoned.exception.code, "invocation_store_poisoned")

        self.store.close()
        self.store.close()
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        try:
            self.assertIsNone(reopened.get("invocation-2"))
            self.assertEqual(reopened.get("invocation-1").status, InvocationStatus.RUNNING)
        finally:
            reopened.close()

    def test_poison_close_failure_is_sanitized_and_close_can_retry(self):
        markers = ("private-commit-canary", "private-rollback-canary", "private-close-canary")
        with (
            patch.object(
                self.store,
                "_commit_write_transaction",
                side_effect=KeyboardInterrupt(markers[0]),
            ),
            patch.object(
                self.store,
                "_rollback_write_transaction",
                side_effect=RuntimeError(markers[1]),
            ),
            patch.object(
                self.store,
                "_close_connection",
                side_effect=SystemExit(markers[2]),
            ),
        ):
            poisoned_control = self._capture_base_exception(partial(self.store.enqueue, job_spec()))
            close_control = self._capture_base_exception(self.store.close)

            self._assert_clean_control_signal(
                poisoned_control,
                KeyboardInterrupt,
                marker=markers[0],
                ambiguity=True,
            )
            self._assert_clean_control_signal(
                close_control,
                SystemExit,
                marker=markers[2],
                system_exit_code=1,
                close_failure=True,
                module_frame_names=("wrapped_close", "_raise_clean_control_signal"),
            )
            close_graph = exception_graph_text(close_control)
            for marker in markers:
                self.assertNotIn(marker, close_graph)
            self.assertTrue(self.store._connection.in_transaction)
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteInvocationAttemptStore(
                    self.path,
                    busy_timeout_ms=0,
                    clock=self.clock,
                )

        for marker in markers:
            self.assertNotIn(marker, exception_graph_text(poisoned_control))
        with self.assertRaises(InvocationStorePoisonedError):
            self.store.get("invocation-1")

        self.store.close()
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.store._connection.execute("SELECT 1")
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        try:
            self.assertIsNone(reopened.get("invocation-1"))
        finally:
            reopened.close()

    def test_rollback_failure_never_hides_behind_a_noop_result(self):
        def no_setup(_store):
            return None

        def stale_setup(store):
            store.enqueue(job_spec())
            lease = store.claim("invocation-1", "worker", lease_seconds=10)
            assert lease is not None
            return replace(lease, lease_token="stale-token")

        cases = (
            (
                "no-op claim",
                no_setup,
                lambda store, _lease: store.claim_next("worker", lease_seconds=10),
            ),
            ("no-op recovery", no_setup, lambda store, _lease: store.recover_expired()),
            (
                "stale heartbeat",
                stale_setup,
                lambda store, lease: store.heartbeat(lease, lease_seconds=10),
            ),
            ("stale complete", stale_setup, lambda store, lease: store.complete(lease)),
            (
                "stale fail",
                stale_setup,
                lambda store, lease: store.fail(lease, "stale"),
            ),
        )
        for index, (name, setup, operation) in enumerate(cases):
            with self.subTest(name=name):
                path = str(Path(self.tempdir.name) / f"rollback-noop-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=self.clock)
                lease = setup(store)
                try:
                    marker = f"hidden {name} precommit"
                    with (
                        patch.object(
                            store,
                            "_commit_write_transaction",
                            side_effect=KeyboardInterrupt(marker),
                        ),
                        patch.object(
                            store,
                            "_rollback_write_transaction",
                            side_effect=RuntimeError("hidden rollback"),
                        ),
                    ):
                        cleaned = self._capture_base_exception(partial(operation, store, lease))
                    self._assert_clean_control_signal(
                        cleaned,
                        KeyboardInterrupt,
                        marker=marker,
                        ambiguity=True,
                    )
                    with self.assertRaises(InvocationStorePoisonedError):
                        store.schema_version()
                finally:
                    store.close()

    def test_explicit_close_is_idempotent_and_stably_rejects_access(self):
        self.store.close()
        self.store.close()

        with self.assertRaises(InvocationStoreClosedError) as captured:
            self.store.schema_version()
        self.assertEqual(captured.exception.code, "invocation_store_closed")

    def test_close_failure_is_typed_sanitized_and_retryable(self):
        marker = f"private sqlite close failure SELECT secret FROM ledger AT {self.path}"
        with patch.object(
            self.store,
            "_close_connection",
            side_effect=HostileFault(marker),
        ):
            with self.assertRaises(InvocationStoreClosedError) as captured:
                self.store.close()

        rendered = "".join(
            traceback.TracebackException.from_exception(captured.exception).format(chain=True)
        )
        self.assertNotIn(marker, rendered)
        self.assertEqual(captured.exception.code, "invocation_store_closed")
        with self.assertRaises(InvocationStoreClosedError):
            self.store.get("invocation-1")

        self.store.close()
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.store._connection.execute("SELECT 1")

    def test_explicit_close_reissues_every_exact_control_with_stable_cause(self):
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
        for index, error_type in enumerate(control_types):
            with self.subTest(error_type=error_type.__name__):
                path = str(Path(self.tempdir.name) / f"close-control-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=self.clock)
                marker = f"private close {error_type.__name__} at {path}"
                failure = self._hostile_control_signal(error_type(marker), marker)
                try:
                    with patch.object(
                        store,
                        "_close_connection",
                        side_effect=failure,
                    ):
                        cleaned = self._capture_base_exception(store.close)
                    self._assert_clean_control_signal(
                        cleaned,
                        error_type,
                        marker=marker,
                        system_exit_code=(1 if error_type is SystemExit else None),
                        close_failure=True,
                        module_frame_names=(
                            "wrapped_close",
                            "_raise_clean_control_signal",
                        ),
                    )
                    with self.assertRaises(InvocationStoreClosedError):
                        store.schema_version()
                finally:
                    store.close()

    def test_context_body_has_priority_over_every_close_control(self):
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
        for index, error_type in enumerate(control_types):
            with self.subTest(error_type=error_type.__name__):
                path = str(Path(self.tempdir.name) / f"context-close-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=self.clock)
                body_marker = f"private body {index}"
                close_marker = f"private close control {error_type.__name__} at {path}"
                body_error = HostileFault(body_marker)
                close_error = self._hostile_control_signal(
                    error_type(close_marker),
                    close_marker,
                )
                try:
                    with patch.object(
                        store,
                        "_close_connection",
                        side_effect=close_error,
                    ):
                        observed = self._capture_base_exception(
                            partial(self._raise_context_body, store, body_error)
                        )
                    self.assertIs(observed, body_error)
                    self.assertNotIn(close_marker, exception_graph_text(observed))
                    module_frames = []
                    cursor = observed.__traceback__
                    while cursor is not None:
                        if cursor.tb_frame.f_globals.get("__name__") == attempts_module.__name__:
                            module_frames.append(cursor.tb_frame.f_code.co_name)
                        cursor = cursor.tb_next
                    self.assertEqual(module_frames, [])
                    with self.assertRaises(InvocationStoreClosedError):
                        store.schema_version()
                finally:
                    store.close()

    def test_empty_context_reissues_every_close_control_without_store_frames(self):
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)
        for index, error_type in enumerate(control_types):
            with self.subTest(error_type=error_type.__name__):
                path = str(Path(self.tempdir.name) / f"empty-context-close-{index}.sqlite3")
                store = SQLiteInvocationAttemptStore(path, clock=self.clock)
                marker = f"private empty close {error_type.__name__} at {path}"
                failure = self._hostile_control_signal(error_type(marker), marker)
                try:
                    with patch.object(
                        store,
                        "_close_connection",
                        side_effect=failure,
                    ):
                        cleaned = self._capture_base_exception(
                            partial(self._exit_empty_context, store)
                        )
                    self._assert_clean_control_signal(
                        cleaned,
                        error_type,
                        marker=marker,
                        system_exit_code=(1 if error_type is SystemExit else None),
                        close_failure=True,
                        module_frame_names=(
                            "wrapped_close",
                            "_raise_clean_control_signal",
                        ),
                    )
                    with self.assertRaises(InvocationStoreClosedError):
                        store.schema_version()
                finally:
                    store.close()

    def test_context_exit_preserves_a_hostile_body_over_a_close_failure(self):
        body_marker = "private context body"
        close_marker = "private context close"
        body_error = HostileFault(body_marker)
        with patch.object(
            self.store,
            "_close_connection",
            side_effect=HostileFault(close_marker),
        ):
            with self.assertRaises(HostileFault) as captured:
                with self.store:
                    raise body_error

        self.assertIs(captured.exception, body_error)
        self.assertEqual(captured.exception.secret, body_marker)
        self.assertNotIn(close_marker, exception_graph_text(captured.exception))
        with self.assertRaises(InvocationStoreClosedError):
            self.store.schema_version()
        self.store.close()

    def test_read_body_rollback_and_close_faults_poison_without_detail_leakage(self):
        self.store.enqueue(job_spec())
        markers = ("private read body", "private read rollback", "private read close")
        with (
            patch.object(
                self.store,
                "_row_to_job",
                side_effect=KeyboardInterrupt(markers[0]),
            ),
            patch.object(
                self.store,
                "_rollback_read_transaction",
                side_effect=HostileFault(markers[1]),
            ),
            patch.object(
                self.store,
                "_close_connection",
                side_effect=SystemExit(markers[2]),
            ),
        ):
            cleaned = self._capture_base_exception(
                partial(
                    self.store.recovery_snapshot_for_task,
                    "session-1",
                    "task-1",
                )
            )

        self._assert_clean_control_signal(
            cleaned,
            KeyboardInterrupt,
            marker=markers[0],
            ambiguity=True,
        )
        for marker in markers:
            self.assertNotIn(marker, exception_graph_text(cleaned))
        with self.assertRaises(InvocationStorePoisonedError):
            self.store.schema_version()
        self.store.close()

    def test_reconciliation_read_begin_and_rollback_faults_poison(self):
        markers = ("private write commit", "private read begin", "private read rollback")
        write_commit = self.store._commit_write_transaction
        read_begin = self.store._begin_read_transaction

        def committed_write_failure(connection):
            write_commit(connection)
            raise RuntimeError(markers[0])

        def started_read_failure(connection):
            read_begin(connection)
            raise KeyboardInterrupt(markers[1])

        with (
            patch.object(
                self.store,
                "_commit_write_transaction",
                side_effect=committed_write_failure,
            ),
            patch.object(
                self.store,
                "_begin_read_transaction",
                side_effect=started_read_failure,
            ),
            patch.object(
                self.store,
                "_rollback_read_transaction",
                side_effect=SystemExit(markers[2]),
            ),
        ):
            cleaned = self._capture_base_exception(partial(self.store.enqueue, job_spec()))

        self._assert_clean_control_signal(
            cleaned,
            KeyboardInterrupt,
            marker=markers[1],
            ambiguity=True,
        )
        for marker in markers:
            self.assertNotIn(marker, exception_graph_text(cleaned))
        with self.assertRaises(InvocationStorePoisonedError):
            self.store.schema_version()
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        try:
            self.assertEqual(reopened.get("invocation-1").status, InvocationStatus.QUEUED)
        finally:
            reopened.close()

    def test_reconciliation_read_commit_and_rollback_faults_poison(self):
        markers = ("private write ack", "private read commit", "private read rollback")
        write_commit = self.store._commit_write_transaction

        def committed_write_failure(connection):
            write_commit(connection)
            raise RuntimeError(markers[0])

        with (
            patch.object(
                self.store,
                "_commit_write_transaction",
                side_effect=committed_write_failure,
            ),
            patch.object(
                self.store,
                "_commit_read_transaction",
                side_effect=KeyboardInterrupt(markers[1]),
            ),
            patch.object(
                self.store,
                "_rollback_read_transaction",
                side_effect=RuntimeError(markers[2]),
            ),
        ):
            cleaned = self._capture_base_exception(partial(self.store.enqueue, job_spec()))

        self._assert_clean_control_signal(
            cleaned,
            KeyboardInterrupt,
            marker=markers[1],
            ambiguity=True,
        )
        for marker in markers:
            self.assertNotIn(marker, exception_graph_text(cleaned))
        with self.assertRaises(InvocationStorePoisonedError):
            self.store.get("invocation-1")

    def test_post_end_read_commit_interrupt_is_preserved_without_poison(self):
        self.store.enqueue(job_spec())
        read_commit = self.store._commit_read_transaction
        marker = "private read cancellation remains observable"

        def committed_read_interrupt(connection):
            read_commit(connection)
            raise self._hostile_control_signal(KeyboardInterrupt(marker), marker)

        with patch.object(
            self.store,
            "_commit_read_transaction",
            side_effect=committed_read_interrupt,
        ):
            cleaned = self._capture_base_exception(
                partial(
                    self.store.recovery_snapshot_for_task,
                    "session-1",
                    "task-1",
                )
            )

        self._assert_clean_control_signal(
            cleaned,
            KeyboardInterrupt,
            marker=marker,
        )
        self.assertEqual(self.store.schema_version(), 2)

    def test_waiting_callers_observe_poison_instead_of_a_closed_connection(self):
        self.store._lock.acquire()
        ready = threading.Barrier(3)

        def read_after_barrier():
            ready.wait(timeout=2)
            return self.store.get("invocation-1")

        def write_after_barrier():
            ready.wait(timeout=2)
            return self.store.enqueue(job_spec())

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                read = executor.submit(read_after_barrier)
                write = executor.submit(write_after_barrier)
                ready.wait(timeout=2)
                self.store._poison_store()
                self.store._lock.release()

                for future in (read, write):
                    with self.assertRaises(InvocationStorePoisonedError):
                        future.result(timeout=2)
        finally:
            # RLock does not expose ownership portably; release is needed only if
            # an assertion or barrier error interrupted the normal handoff above.
            try:
                self.store._lock.release()
            except RuntimeError:
                pass

    def test_readback_poison_waits_for_an_inflight_store_lock(self):
        reader_may_enter = threading.Event()
        reader_inside = threading.Event()
        release_reader = threading.Event()
        readback_interrupted = threading.Event()
        mutator_finished = threading.Event()
        reader_errors = []
        mutator_errors = []

        def read_after_usable_check():
            if not reader_may_enter.wait(timeout=2):
                reader_errors.append(AssertionError("reader start timed out"))
                return
            with self.store._lock:
                self.store._require_usable()
                reader_inside.set()
                if not release_reader.wait(timeout=2):
                    reader_errors.append(AssertionError("reader release timed out"))
                    return
                try:
                    self.store._connection.execute("SELECT 1").fetchone()
                except BaseException as error:
                    reader_errors.append(error)

        real_commit = self.store._commit_write_transaction
        real_reconcile = self.store._reconcile_enqueued_job

        def committed_ack_loss(connection):
            real_commit(connection)
            raise RuntimeError("commit acknowledgement lost")

        def interrupted_readback(spec):
            real_reconcile(spec)
            reader_may_enter.set()
            if not reader_inside.wait(timeout=2):
                raise AssertionError("inflight reader did not acquire the store lock")
            readback_interrupted.set()
            raise KeyboardInterrupt("readback interrupted after releasing the store lock")

        def mutate():
            try:
                self.store.enqueue(job_spec())
            except BaseException as error:
                mutator_errors.append(error)
            finally:
                mutator_finished.set()

        reader = threading.Thread(target=read_after_usable_check)
        mutator = threading.Thread(target=mutate)
        reader.start()
        try:
            with (
                patch.object(
                    self.store,
                    "_commit_write_transaction",
                    side_effect=committed_ack_loss,
                ),
                patch.object(
                    self.store,
                    "_reconcile_enqueued_job",
                    side_effect=interrupted_readback,
                ),
            ):
                mutator.start()
                self.assertTrue(readback_interrupted.wait(timeout=2))
                self.assertFalse(mutator_finished.wait(timeout=0.1))
                release_reader.set()
                reader.join(timeout=2)
                mutator.join(timeout=2)
        finally:
            reader_may_enter.set()
            release_reader.set()
            reader.join(timeout=2)
            if mutator.ident is not None:
                mutator.join(timeout=2)

        self.assertFalse(reader.is_alive())
        self.assertFalse(mutator.is_alive())
        self.assertEqual(reader_errors, [])
        self.assertEqual(len(mutator_errors), 1)
        self._assert_clean_control_signal(
            mutator_errors[0],
            KeyboardInterrupt,
            marker="readback interrupted after releasing the store lock",
            ambiguity=True,
        )
        with self.assertRaises(InvocationStorePoisonedError):
            self.store.schema_version()

        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        try:
            self.assertEqual(reopened.get("invocation-1").status, InvocationStatus.QUEUED)
        finally:
            reopened.close()

    def test_owned_mutations_reject_mixed_lease_binding(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        forged = replace(
            lease,
            session_id="other-session",
            plan_id="other-plan",
            task_id="other-task",
            agent_id="other-agent",
            idempotency_key="invoke:other-task",
            payload_digest="0" * 64,
            attempt_number=2,
            max_attempts=4,
            claimed_at=persisted_timestamp(1),
        )
        running = tuple(self.store._connection.iterdump())

        for operation in (
            lambda: self.store.heartbeat(forged, lease_seconds=10),
            lambda: self.store.complete(forged, result_ref="result:forged"),
            lambda: self.store.fail(forged, "forged failure"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "lease binding differs from durable ownership",
                ):
                    operation()
                self.assertEqual(tuple(self.store._connection.iterdump()), running)

    def test_fencing_epoch_is_scoped_to_one_invocation(self):
        self.store.enqueue(job_spec())
        self.store.enqueue(
            job_spec(
                session_id="session-2",
                task_id="task-2",
                idempotency_key="invoke:task-2",
                invocation_id="invocation-2",
            )
        )

        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        second = self.store.claim("invocation-2", "worker-2", lease_seconds=10)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertEqual(first.fencing_token, 1)
        self.assertEqual(second.fencing_token, 1)
        self.assertNotEqual(
            (first.invocation_id, first.fencing_token),
            (second.invocation_id, second.fencing_token),
        )

    def test_claim_rejects_clock_before_job_mutation_floor(self):
        self.clock.set(timestamp(10))
        self.store.enqueue(job_spec(available_at=T0))
        self.clock.set(timestamp(5))
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(
            InvocationClockRegressionError,
            "clock precedes durable invocation activity",
        ):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

        self.clock.set(timestamp(10))
        self.assertIsNotNone(self.store.claim("invocation-1", "worker", lease_seconds=10))

    def test_owned_mutations_reject_clock_before_activity_floor(self):
        self.store.enqueue(job_spec())
        self.clock.set(timestamp(10))
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        before = tuple(self.store._connection.iterdump())
        self.clock.set(timestamp(5))

        operations = (
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must not persist"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    InvocationClockRegressionError,
                    "clock precedes durable invocation activity",
                ):
                    operation()

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.clock.set(timestamp(10))
        self.assertTrue(self.store.heartbeat(lease, lease_seconds=10))

    def test_second_connection_cannot_mutate_with_a_regressed_clock(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=20)
        self.clock.set(timestamp(10))
        self.assertTrue(self.store.heartbeat(lease, lease_seconds=20))
        before = tuple(self.store._connection.iterdump())

        second = SQLiteInvocationAttemptStore(
            self.path,
            clock=MutableClock(timestamp(5)),
        )
        try:
            operations = (
                lambda: second.heartbeat(lease, lease_seconds=10),
                lambda: second.complete(lease),
                lambda: second.fail(lease, "must not persist"),
            )
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(InvocationClockRegressionError):
                        operation()
        finally:
            second.close()

        self.assertEqual(tuple(self.store._connection.iterdump()), before)

    def test_migration_is_versioned_reopenable_and_coexists_with_event_store(self):
        self.assertEqual(self.store.schema_version(), 2)
        self.store.close()

        event_store = SQLiteEventStore(self.path)
        stored = event_store.append(DomainEvent("session:s1", "created", {}, "actor"))
        reopened = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

        self.assertEqual(reopened.schema_version(), 3)
        self.assertEqual(stored.sequence, 1)
        self.assertEqual(len(event_store.read_stream("session:s1")), 1)
        reopened.close()
        event_store.close()
        # Keep tearDown idempotent after this test explicitly reopens the store.
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def test_two_connections_can_initialize_the_same_new_database_concurrently(self):
        path = str(Path(self.tempdir.name) / "concurrent-initialize.sqlite3")
        barrier = threading.Barrier(2)

        def initialize():
            barrier.wait(timeout=2)
            store = SQLiteInvocationAttemptStore(path, clock=self.clock)
            try:
                return store.schema_version()
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = [
                future.result(timeout=3)
                for future in (executor.submit(initialize), executor.submit(initialize))
            ]

        self.assertEqual(versions, [2, 2])

    def test_migration_checksum_drift_fails_closed(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE qe_schema_migrations SET sha256 = ? WHERE version = 1",
            ("0" * 64,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationDriftError):
            SQLiteInvocationAttemptStore(self.path, clock=self.clock)

        # Restore the disposable database in strict reverse dependency order, then
        # let the normal runner rebuild a continuous ledger.
        connection = sqlite3.connect(self.path)
        for filename in (
            "0002_artifacts.down.sql",
            "0001_invocation_attempts.down.sql",
        ):
            down = (
                importlib.resources.files("quantum_entanglement.migrations")
                .joinpath(filename)
                .read_text(encoding="utf-8")
            )
            connection.executescript(down)
        connection.close()
        self.store = SQLiteInvocationAttemptStore(self.path, clock=self.clock)

    def test_enqueue_is_idempotent_but_rejects_changed_identity_or_payload(self):
        spec = job_spec()
        first = self.store.enqueue(spec)
        self.clock.set(timestamp(1))
        retried = self.store.enqueue(
            replace(spec, invocation_id="retry-generated-id"),
        )

        self.assertEqual(first, retried)
        self.assertEqual(first.status, InvocationStatus.QUEUED)
        self.assertEqual(first.created_at, "2026-08-20T00:00:00.000000Z")
        with self.assertRaises(InvocationConflictError):
            self.store.enqueue(replace(spec, agent_id="different-agent"))
        with self.assertRaises(InvocationConflictError):
            self.store.enqueue(replace(spec, available_at=timestamp(10)))
        with self.assertRaises(InvocationConflictError):
            self.store.enqueue(
                job_spec(
                    invocation_id="other-invocation",
                    task_id="other-task",
                    payload_digest=invocation_payload_digest({"changed": True}),
                ),
            )

    def test_claim_next_obeys_availability_then_priority(self):
        self.store.enqueue(
            job_spec(
                invocation_id="low",
                task_id="low",
                idempotency_key="invoke:low",
                priority=1,
            ),
        )
        self.store.enqueue(
            job_spec(
                invocation_id="high",
                task_id="high",
                idempotency_key="invoke:high",
                priority=100,
                available_at=timestamp(5),
            ),
        )

        low = self.store.claim_next("worker", lease_seconds=10)
        self.assertEqual(low.invocation_id, "low")
        self.clock.set(timestamp(1))
        self.assertTrue(self.store.complete(low))
        self.clock.set(timestamp(4))
        self.assertIsNone(self.store.claim_next("worker", lease_seconds=10))
        self.clock.set(timestamp(5))
        high = self.store.claim_next("worker", lease_seconds=10)
        self.assertEqual(high.invocation_id, "high")

    def test_two_independent_connections_have_one_atomic_claim_winner(self):
        self.store.enqueue(job_spec())
        second = SQLiteInvocationAttemptStore(self.path, clock=self.clock)
        barrier = threading.Barrier(2)

        def claim(store, worker):
            barrier.wait(timeout=2)
            return store.claim("invocation-1", worker, lease_seconds=10)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(claim, self.store, "worker-a"),
                    executor.submit(claim, second, "worker-b"),
                ]
                leases = [future.result(timeout=3) for future in futures]
        finally:
            second.close()

        winners = [lease for lease in leases if lease is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get("invocation-1").attempts_started, 1)
        self.assertEqual(len(self.store.attempts("invocation-1")), 1)

    def test_two_processes_have_one_atomic_claim_winner(self):
        self.store.enqueue(job_spec())
        context = multiprocessing.get_context("spawn")
        ready_queue = context.Queue()
        result_queue = context.Queue()
        start_event = context.Event()
        processes = [
            context.Process(
                target=claim_from_process,
                args=(self.path, worker_id, ready_queue, start_event, result_queue),
            )
            for worker_id in ("process-a", "process-b")
        ]

        for process in processes:
            process.start()
        for _ in processes:
            ready_queue.get(timeout=5)
        start_event.set()
        results = [result_queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(sum(won for _worker_id, won in results), 1)
        self.assertEqual(self.store.get("invocation-1").attempts_started, 1)
        self.assertEqual(len(self.store.attempts("invocation-1")), 1)

    def test_fork_inherited_store_is_rejected_before_any_public_access(self):
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            self.skipTest("POSIX fork is unavailable")

        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=probe_fork_inherited_attempt_store,
            args=(self.store, sender),
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(5):
                process.terminate()
                self.fail("fork-inherited store probe did not fail closed")
            outcomes = receiver.recv()
        finally:
            receiver.close()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

        self.assertEqual(process.exitcode, 0)
        self.assertEqual(
            outcomes,
            (
                (
                    "InvocationStoreProcessMismatchError",
                    "invocation_store_process_mismatch",
                ),
            )
            * 12,
        )
        self.assertEqual(self.store.schema_version(), 2)
        self.assertEqual(self.store.enqueue(job_spec()).status, InvocationStatus.QUEUED)

    def test_heartbeat_recovery_fences_stale_worker_and_quarantines_retry(self):
        self.store.enqueue(job_spec())
        old = self.store.claim("invocation-1", "worker-a", lease_seconds=10)
        self.assertNotIn(old.lease_token, repr(old))
        connection = sqlite3.connect(self.path)
        stored_digest = connection.execute(
            "SELECT lease_token_digest FROM invocation_jobs WHERE invocation_id = ?",
            (old.invocation_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(
            stored_digest,
            hashlib.sha256(old.lease_token.encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(stored_digest, old.lease_token)
        observed = self.store.get(old.invocation_id)
        self.assertEqual(observed.lease_token_digest, stored_digest)
        self.assertEqual(observed.heartbeat_at, "2026-08-20T00:00:00.000000Z")
        self.assertNotIn(old.lease_token, repr(observed))
        self.clock.set(timestamp(5))
        self.assertTrue(self.store.heartbeat(old, lease_seconds=20))
        heartbeat_observation = self.store.get(old.invocation_id)
        self.assertEqual(heartbeat_observation.heartbeat_at, "2026-08-20T00:00:05.000000Z")
        self.assertEqual(heartbeat_observation.lease_token_digest, stored_digest)

        self.clock.set(timestamp(24))
        before_expiry = self.store.recover_expired()
        self.assertEqual(before_expiry.recovered_count, 0)
        self.clock.set(timestamp(25))
        recovered = self.store.recover_expired()
        self.assertEqual(recovered.requeued, ("invocation-1",))
        self.assertFalse(self.store.heartbeat(old, lease_seconds=10))
        self.assertFalse(self.store.complete(old, result_ref="event:old"))

        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))
        self.assertIsNone(self.store.claim_next("worker-b", lease_seconds=10))

        job = self.store.get("invocation-1")
        attempts = self.store.attempts("invocation-1")
        self.assertEqual(job.status, InvocationStatus.QUEUED)
        self.assertIsNone(job.result_ref)
        self.assertEqual([item.status for item in attempts], [AttemptStatus.EXPIRED])
        self.assertNotEqual(attempts[0].lease_token_digest, old.lease_token)

    def test_terminal_cas_rejects_completion_at_exact_expiry(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.clock.set(timestamp(10))
        self.assertFalse(self.store.complete(lease))
        recovered = self.store.recover_expired()
        self.assertEqual(recovered.requeued, ("invocation-1",))

    def test_claim_fences_expired_candidate_without_reclaiming_it(self):
        self.store.enqueue(job_spec(max_attempts=3))
        expired = self.store.claim("invocation-1", "worker-a", lease_seconds=1)
        self.clock.set(timestamp(1))

        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))

        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.QUEUED)
        self.assertEqual(
            [attempt.status for attempt in self.store.attempts("invocation-1")],
            [AttemptStatus.EXPIRED],
        )
        self.assertFalse(self.store.complete(expired, result_ref="event:stale"))

    def test_owned_row_query_rejects_incompatible_connection_factory(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.row_factory = None
        try:
            with self.assertRaisesRegex(TypeError, "must return sqlite3.Row"):
                self.store._active_owned_row(
                    self.store._connection,
                    lease,
                    timestamp(1),
                )
        finally:
            self.store._connection.row_factory = sqlite3.Row

    def test_lease_clock_is_sampled_only_after_write_transaction_begins(self):
        self.store.enqueue(job_spec())
        original_transaction = self.store._transaction

        @contextmanager
        def transaction_at_claim_time():
            with original_transaction() as connection:
                self.clock.set(timestamp(5))
                yield connection

        with patch.object(self.store, "_transaction", transaction_at_claim_time):
            lease = self.store.claim("invocation-1", "worker", lease_seconds=1)

        self.assertEqual(lease.claimed_at, "2026-08-20T00:00:05.000000Z")
        self.assertEqual(lease.lease_expires_at, "2026-08-20T00:00:06.000000Z")

        @contextmanager
        def transaction_at_expiry():
            with original_transaction() as connection:
                self.clock.set(timestamp(6))
                yield connection

        with patch.object(self.store, "_transaction", transaction_at_expiry):
            self.assertFalse(self.store.complete(lease, result_ref="event:late"))

        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.RUNNING)

    def test_explicit_failure_schedule_does_not_authorize_retry(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-a", lease_seconds=20)
        self.clock.set(timestamp(1))
        self.assertTrue(self.store.fail(first, "transient", retry_at=timestamp(10)))

        queued = self.store.get("invocation-1")
        self.assertEqual(queued.status, InvocationStatus.QUEUED)
        self.clock.set(timestamp(9))
        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))
        self.clock.set(timestamp(10))
        self.assertIsNone(self.store.claim("invocation-1", "worker-b", lease_seconds=10))

        quarantined = self.store.get("invocation-1")
        self.assertEqual(quarantined.status, InvocationStatus.QUEUED)
        self.assertEqual(quarantined.last_error, "transient")
        self.clock.set(timestamp(12))
        self.assertIsNone(self.store.claim("invocation-1", "worker-c", lease_seconds=10))
        self.assertEqual(
            [item.status for item in self.store.attempts("invocation-1")],
            [AttemptStatus.FAILED],
        )

    def test_claim_next_skips_high_priority_effect_unknown_job(self):
        self.store.enqueue(job_spec(max_attempts=3, priority=100))
        attempted = self.store.claim("invocation-1", "worker-a", lease_seconds=10)
        self.assertTrue(self.store.fail(attempted, "unknown effect", retry_at=T0))
        self.store.enqueue(
            job_spec(
                invocation_id="invocation-fresh",
                task_id="task-fresh",
                idempotency_key="invoke:fresh",
                priority=1,
            )
        )

        fresh = self.store.claim_next("worker-b", lease_seconds=10)

        self.assertEqual(fresh.invocation_id, "invocation-fresh")
        self.assertEqual(self.store.get("invocation-1").attempts_started, 1)
        self.assertEqual(len(self.store.attempts("invocation-1")), 1)

    def test_orphan_lease_epoch_cannot_be_observed_or_claimed_as_first_attempt(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET lease_epoch = 1 WHERE invocation_id = ?",
            ("invocation-1",),
        )
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

    def test_first_claim_rejects_any_preexisting_attempt_history(self):
        for attempt_number in (1, 2):
            with self.subTest(attempt_number=attempt_number):
                self.store.enqueue(job_spec())
                self.store._connection.execute(
                    """
                    INSERT INTO invocation_attempts (
                        attempt_id, invocation_id, attempt_number, lease_epoch,
                        worker_id, lease_token_digest, status, started_at,
                        heartbeat_at, lease_expires_at, finished_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)
                    """,
                    (
                        f"orphan-attempt-{attempt_number}",
                        "invocation-1",
                        attempt_number,
                        attempt_number,
                        "orphan-worker",
                        f"{attempt_number}" * 64,
                        persisted_timestamp(0),
                        persisted_timestamp(0),
                        persisted_timestamp(10),
                        persisted_timestamp(5),
                        "orphan failure",
                    ),
                )
                poisoned = tuple(self.store._connection.iterdump())

                for operation in (
                    lambda: self.store.claim("invocation-1", "worker", lease_seconds=10),
                    lambda: self.store.claim_next("worker", lease_seconds=10),
                ):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(
                            InvocationIntegrityError,
                            "first-claim candidate has attempt history",
                        ):
                            operation()
                        self.assertEqual(tuple(self.store._connection.iterdump()), poisoned)

                self.store._connection.execute(
                    "DELETE FROM invocation_attempts WHERE invocation_id = ?",
                    ("invocation-1",),
                )
                self.store._connection.execute(
                    "DELETE FROM invocation_jobs WHERE invocation_id = ?",
                    ("invocation-1",),
                )

    def test_zero_attempt_last_error_cannot_cross_first_claim(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET last_error = ? WHERE invocation_id = ?",
            ("orphan prior failure", "invocation-1"),
        )
        poisoned = tuple(self.store._connection.iterdump())

        operations = (
            lambda: self.store.get("invocation-1"),
            lambda: self.store.recovery_snapshot_for_task("session-1", "task-1"),
            lambda: self.store.claim("invocation-1", "worker", lease_seconds=10),
            lambda: self.store.claim_next("worker", lease_seconds=10),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "persisted invocation job is malformed",
                ):
                    operation()
                self.assertEqual(tuple(self.store._connection.iterdump()), poisoned)

    def test_queued_partial_lease_cannot_be_observed_recovered_or_claimed(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute("PRAGMA ignore_check_constraints = ON")
        try:
            self.store._connection.execute(
                "UPDATE invocation_jobs SET lease_owner = ? WHERE invocation_id = ?",
                ("orphan-worker", "invocation-1"),
            )
        finally:
            self.store._connection.execute("PRAGMA ignore_check_constraints = OFF")
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

    def test_non_succeeded_result_reference_cannot_cross_first_claim(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET result_ref = ? WHERE invocation_id = ?",
            ("result:unexpected", "invocation-1"),
        )
        before = tuple(self.store._connection.iterdump())

        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertEqual(tuple(self.store._connection.iterdump()), before)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)

    def test_first_claim_cas_rejects_epoch_change_after_candidate_read(self):
        self.store.enqueue(job_spec())
        original = SQLiteInvocationAttemptStore._row_to_job
        changed = False

        def change_epoch_after_decode(row):
            nonlocal changed
            job = original(row)
            if not changed:
                changed = True
                self.store._connection.execute(
                    "UPDATE invocation_jobs SET lease_epoch = 1 WHERE invocation_id = ?",
                    ("invocation-1",),
                )
            return job

        with patch.object(
            SQLiteInvocationAttemptStore,
            "_row_to_job",
            side_effect=change_epoch_after_decode,
        ):
            with self.assertRaisesRegex(InvocationIntegrityError, "candidate changed"):
                self.store.claim("invocation-1", "worker", lease_seconds=10)

        self.assertTrue(changed)
        self.assertEqual(len(self.store.attempts("invocation-1")), 0)
        raw = self.store._connection.execute(
            "SELECT attempts_started, lease_epoch FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()
        self.assertEqual(tuple(raw), (0, 0))

    def test_crashed_final_attempt_is_terminally_exhausted(self):
        self.store.enqueue(job_spec(max_attempts=1))
        lease = self.store.claim("invocation-1", "worker-a", lease_seconds=5)

        self.clock.set(timestamp(5))
        summary = self.store.recover_expired()

        self.assertEqual(summary.exhausted, ("invocation-1",))
        self.assertFalse(self.store.complete(lease))
        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.FAILED)
        self.assertEqual(
            self.store.attempts("invocation-1")[0].status,
            AttemptStatus.EXPIRED,
        )

    def test_invalid_retry_and_lease_inputs_fail_before_state_change(self):
        with self.assertRaises(ValueError):
            job_spec(payload_digest="not-a-digest")
        with self.assertRaises(ValueError):
            job_spec(max_attempts=1 << 63)
        with self.assertRaises(ValueError):
            job_spec(available_at="2026-08-20 00:00:00Z")
        with self.assertRaises(ValueError):
            job_spec(available_at="2026-08-20T00:00:00-00:00")
        with self.assertRaises(ValueError):
            invocation_payload_digest({"notFinite": float("nan")})
        with self.assertRaises(ValueError):
            job_spec(agent_id="agent\ncontrol")
        with self.assertRaises(ValueError):
            job_spec(task_id="t" * 4_097)
        self.store.enqueue(job_spec())
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.store.claim("invocation-1", "worker", lease_seconds=seconds)
        for invalid_limit in (True, False, 1.0, "1", None):
            with self.subTest(recovery_limit=invalid_limit):
                with self.assertRaises(TypeError):
                    self.store.recover_expired(limit=invalid_limit)  # type: ignore[arg-type]
        for invalid_limit in (-1, 0, 1_001):
            with self.subTest(recovery_limit=invalid_limit):
                with self.assertRaises(ValueError):
                    self.store.recover_expired(limit=invalid_limit)
        self.assertEqual(self.store.get("invocation-1").status, InvocationStatus.QUEUED)

    def test_utc_year_boundary_overflow_is_stable_and_never_mutates(self):
        out_of_range = (
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59-14:00",
        )
        for available_at in out_of_range:
            with self.subTest(operation="job_spec", value=available_at):
                with self.assertRaisesRegex(ValueError, "RFC 3339 timestamp"):
                    job_spec(available_at=available_at)

        empty = tuple(self.store._connection.iterdump())
        for clock_value in out_of_range:
            with self.subTest(operation="clock", value=clock_value):
                self.clock.set(clock_value)
                with self.assertRaisesRegex(ValueError, "RFC 3339 timestamp"):
                    self.store.enqueue(job_spec())
                self.assertEqual(tuple(self.store._connection.iterdump()), empty)

        self.clock.set(T0)
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        running = tuple(self.store._connection.iterdump())
        for retry_at in out_of_range:
            with self.subTest(operation="retry_at", value=retry_at):
                with self.assertRaisesRegex(ValueError, "RFC 3339 timestamp"):
                    self.store.fail(lease, "must roll back", retry_at=retry_at)
                self.assertEqual(tuple(self.store._connection.iterdump()), running)

    def test_write_text_contract_rejects_recovery_poison_before_state_change(self):
        self.store.enqueue(job_spec())
        queued = tuple(self.store._connection.iterdump())
        for worker_id in ("worker\ncontrol", "w" * 4_097):
            with self.subTest(worker_id_length=len(worker_id)):
                with self.assertRaises(ValueError):
                    self.store.claim("invocation-1", worker_id, lease_seconds=10)
                self.assertEqual(tuple(self.store._connection.iterdump()), queued)

        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        running = tuple(self.store._connection.iterdump())
        for result_ref in ("result\ncontrol", "r" * 16_385):
            with self.subTest(result_ref_length=len(result_ref)):
                with self.assertRaises(ValueError):
                    self.store.complete(lease, result_ref=result_ref)
                self.assertEqual(tuple(self.store._connection.iterdump()), running)
        for error in ("error\ncontrol", "e" * 16_385):
            with self.subTest(error_length=len(error)):
                with self.assertRaises(ValueError):
                    self.store.fail(lease, error)
                self.assertEqual(tuple(self.store._connection.iterdump()), running)

    def test_exact_text_boundaries_round_trip_through_recovery_snapshot(self):
        self.store.enqueue(job_spec())
        worker_id = "w" * 4_096
        result_ref = "r" * 16_384

        lease = self.store.claim("invocation-1", worker_id, lease_seconds=10)
        self.assertTrue(self.store.complete(lease, result_ref=result_ref))
        snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")

        self.assertEqual(snapshot.job.result_ref, result_ref)
        self.assertEqual(snapshot.current_attempt.worker_id, worker_id)
        self.assertEqual(snapshot.current_attempt.result_ref, result_ref)

    def test_persisted_job_types_and_timestamps_fail_closed(self):
        self.store.enqueue(job_spec())
        corruptions = (
            ("priority", 50.5),
            ("available_at", T0),
            ("payload_digest", b"0" * 64),
        )
        for column, value in corruptions:
            with self.subTest(column=column):
                self.store._connection.execute(
                    f"UPDATE invocation_jobs SET {column} = ? WHERE invocation_id = ?",
                    (value, "invocation-1"),
                )
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "persisted invocation job is malformed",
                ):
                    self.store.get("invocation-1")
                self.store._connection.execute(
                    f"UPDATE invocation_jobs SET {column} = ? WHERE invocation_id = ?",
                    (
                        {
                            "priority": 50,
                            "available_at": "2026-08-20T00:00:00.000000Z",
                            "payload_digest": job_spec().payload_digest,
                        }[column],
                        "invocation-1",
                    ),
                )

    def test_persisted_job_cross_field_semantics_fail_closed(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET updated_at = ? WHERE invocation_id = ?",
            ("2026-08-19T23:59:59.000000Z", "invocation-1"),
        )
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        self.store._connection.execute(
            "UPDATE invocation_jobs SET updated_at = ? WHERE invocation_id = ?",
            (persisted_timestamp(0), "invocation-1"),
        )

        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET status = 'failed', finished_at = ?
            WHERE invocation_id = ?
            """,
            (persisted_timestamp(0), "invocation-1"),
        )
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")
        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET status = 'queued', finished_at = NULL
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )

        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'running', lease_owner = 'worker', lease_token_digest = ?,
                lease_expires_at = ?, heartbeat_at = ?
            WHERE invocation_id = ?
            """,
            ("0" * 64, persisted_timestamp(10), persisted_timestamp(0), "invocation-1"),
        )
        with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
            self.store.get("invocation-1")

    def test_persisted_running_job_time_causality_fails_closed(self):
        self.store.enqueue(job_spec())
        self.clock.set(timestamp(5))
        self.store.claim("invocation-1", "worker", lease_seconds=10)
        original = self.store._connection.execute(
            """
            SELECT heartbeat_at, updated_at, lease_expires_at
            FROM invocation_jobs WHERE invocation_id = ?
            """,
            ("invocation-1",),
        ).fetchone()
        corruptions = (
            ("heartbeat_at", "2026-08-19T23:59:59.000000Z"),
            ("updated_at", persisted_timestamp(4)),
            ("updated_at", persisted_timestamp(15)),
            ("lease_expires_at", persisted_timestamp(5)),
        )

        for column, value in corruptions:
            with self.subTest(column=column, value=value):
                self.store._connection.execute(
                    f"UPDATE invocation_jobs SET {column} = ? WHERE invocation_id = ?",
                    (value, "invocation-1"),
                )
                with self.assertRaisesRegex(InvocationIntegrityError, "persisted invocation job"):
                    self.store.get("invocation-1")
                self.store._connection.execute(
                    """
                    UPDATE invocation_jobs
                    SET heartbeat_at = ?, updated_at = ?, lease_expires_at = ?
                    WHERE invocation_id = ?
                    """,
                    (*tuple(original), "invocation-1"),
                )

    def test_persisted_attempt_time_causality_fails_closed(self):
        self.store.enqueue(job_spec())
        self.clock.set(timestamp(5))
        self.store.claim("invocation-1", "worker", lease_seconds=10)
        original = self.store._connection.execute(
            """
            SELECT status, heartbeat_at, lease_expires_at, finished_at
            FROM invocation_attempts WHERE invocation_id = ?
            """,
            ("invocation-1",),
        ).fetchone()
        corruptions = (
            (
                "UPDATE invocation_attempts SET heartbeat_at = ? WHERE invocation_id = ?",
                ("2026-08-19T23:59:59.000000Z", "invocation-1"),
            ),
            (
                "UPDATE invocation_attempts SET lease_expires_at = ? WHERE invocation_id = ?",
                (persisted_timestamp(5), "invocation-1"),
            ),
            (
                """
                UPDATE invocation_attempts SET status = 'failed', finished_at = ?
                WHERE invocation_id = ?
                """,
                (persisted_timestamp(4), "invocation-1"),
            ),
            (
                """
                UPDATE invocation_attempts SET status = 'succeeded', finished_at = ?
                WHERE invocation_id = ?
                """,
                (persisted_timestamp(15), "invocation-1"),
            ),
            (
                """
                UPDATE invocation_attempts SET status = 'expired', finished_at = ?
                WHERE invocation_id = ?
                """,
                (persisted_timestamp(14), "invocation-1"),
            ),
        )

        for statement, parameters in corruptions:
            with self.subTest(statement=" ".join(statement.split())):
                self.store._connection.execute(statement, parameters)
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "persisted invocation attempt",
                ):
                    self.store.attempts("invocation-1")
                self.store._connection.execute(
                    """
                    UPDATE invocation_attempts
                    SET status = ?, heartbeat_at = ?, lease_expires_at = ?, finished_at = ?
                    WHERE invocation_id = ?
                    """,
                    (*tuple(original), "invocation-1"),
                )

    def test_persisted_attempt_types_fail_with_stable_integrity_error(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET attempt_number = ? WHERE invocation_id = ?",
            (b"1", "invocation-1"),
        )

        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation attempt is malformed",
        ):
            self.store.attempts("invocation-1")

    def test_failed_attempt_error_must_match_its_job(self):
        self.store.enqueue(job_spec(max_attempts=1))
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET error = ? WHERE invocation_id = ?",
            ("unexpected active error", "invocation-1"),
        )
        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation attempt is malformed",
        ):
            self.store.attempts("invocation-1")
        self.store._connection.execute(
            "UPDATE invocation_attempts SET error = NULL WHERE invocation_id = ?",
            ("invocation-1",),
        )

        self.assertTrue(self.store.fail(lease, "expected failure"))
        valid = tuple(self.store._connection.iterdump())
        self.store._connection.execute(
            "UPDATE invocation_attempts SET error = NULL WHERE invocation_id = ?",
            ("invocation-1",),
        )
        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation attempt is malformed",
        ):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

        self.store._connection.execute(
            "UPDATE invocation_attempts SET error = ? WHERE invocation_id = ?",
            ("different failure", "invocation-1"),
        )
        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "error differs from its attempt",
        ):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

        self.store._connection.execute(
            "UPDATE invocation_attempts SET error = ? WHERE invocation_id = ?",
            ("expected failure", "invocation-1"),
        )
        self.assertEqual(tuple(self.store._connection.iterdump()), valid)

    def test_job_status_must_match_its_attempt_budget(self):
        self.store.enqueue(job_spec(max_attempts=3))
        first = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        queued = tuple(self.store._connection.iterdump())

        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'failed', finished_at = updated_at
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation job is malformed",
        ):
            self.store.get("invocation-1")

        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'queued', finished_at = NULL, max_attempts = 1
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        exhausted_queued = tuple(self.store._connection.iterdump())
        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation job is malformed",
        ):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(tuple(self.store._connection.iterdump()), exhausted_queued)

        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET max_attempts = 3
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        self.assertEqual(tuple(self.store._connection.iterdump()), queued)

    def test_mutations_reject_corrupt_job_scalars_without_state_change(self):
        self.store.enqueue(job_spec())
        self.store._connection.execute(
            "UPDATE invocation_jobs SET lease_epoch = ? WHERE invocation_id = ?",
            (0.5, "invocation-1"),
        )
        before = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()

        with self.assertRaisesRegex(
            InvocationIntegrityError,
            "persisted invocation job is malformed",
        ):
            self.store.claim("invocation-1", "worker", lease_seconds=10)

        after = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_attempts").fetchone()[
                0
            ],
            0,
        )

    def test_active_lease_mutations_validate_the_complete_owned_row(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_jobs SET priority = ? WHERE invocation_id = ?",
            (50.5, "invocation-1"),
        )
        before = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()

        operations = (
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must not persist"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(InvocationIntegrityError):
                    operation()

        after = self.store._connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            ("invocation-1",),
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()[0],
            AttemptStatus.RUNNING.value,
        )

    def test_expiry_recovery_validates_rows_before_changing_attempt_state(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=1)
        self.clock.set(timestamp(1))
        self.store._connection.execute(
            "UPDATE invocation_jobs SET max_attempts = ? WHERE invocation_id = ?",
            (3.5, "invocation-1"),
        )
        before_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        before_attempt = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )

        with self.assertRaises(InvocationIntegrityError):
            self.store.recover_expired()

        after_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        after_attempt = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        self.assertEqual(after_job, before_job)
        self.assertEqual(after_attempt, before_attempt)

    def test_owned_mutations_reject_attempt_ownership_drift_before_writing(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET worker_id = ? WHERE invocation_id = ?",
            ("different-worker", "invocation-1"),
        )
        before_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )
        before_attempt = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )

        operations = (
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must not persist"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(InvocationIntegrityError, "ownership records disagree"):
                    operation()

        self.assertEqual(
            tuple(
                self.store._connection.execute(
                    "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            before_job,
        )
        self.assertEqual(
            tuple(
                self.store._connection.execute(
                    "SELECT * FROM invocation_attempts WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            before_attempt,
        )

    def test_owned_mutations_reject_attempt_heartbeat_drift_before_writing(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET heartbeat_at = ? WHERE invocation_id = ?",
            (persisted_timestamp(1), "invocation-1"),
        )
        before = tuple(self.store._connection.iterdump())

        operations = (
            lambda: self.store.heartbeat(lease, lease_seconds=10),
            lambda: self.store.complete(lease),
            lambda: self.store.fail(lease, "must not persist"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(InvocationIntegrityError, "ownership records disagree"):
                    operation()

        self.clock.set(timestamp(10))
        with self.assertRaisesRegex(InvocationIntegrityError, "ownership records disagree"):
            self.store.recover_expired()

        self.assertEqual(tuple(self.store._connection.iterdump()), before)

    def test_recovery_rejects_missing_owned_attempt_without_partial_state_change(self):
        self.store.enqueue(job_spec())
        self.store.claim("invocation-1", "worker", lease_seconds=1)
        self.store._connection.execute(
            "DELETE FROM invocation_attempts WHERE invocation_id = ?",
            ("invocation-1",),
        )
        self.clock.set(timestamp(1))
        before_job = tuple(
            self.store._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                ("invocation-1",),
            ).fetchone()
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "exactly one owned attempt"):
            self.store.recover_expired()

        self.assertEqual(
            tuple(
                self.store._connection.execute(
                    "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                    ("invocation-1",),
                ).fetchone()
            ),
            before_job,
        )

    def test_attempt_pages_are_bounded_ordered_and_cursor_based(self):
        self.store.enqueue(job_spec(max_attempts=4))
        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET attempts_started = 3, lease_epoch = 3
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        self.store._connection.executemany(
            """
            INSERT INTO invocation_attempts (
                attempt_id, invocation_id, attempt_number, lease_epoch,
                worker_id, lease_token_digest, status, started_at,
                heartbeat_at, lease_expires_at, finished_at, error
            ) VALUES (?, 'invocation-1', ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)
            """,
            (
                (
                    f"attempt-{number}",
                    number,
                    number,
                    f"worker-{number}",
                    f"{number:064x}",
                    persisted_timestamp(0),
                    persisted_timestamp(0),
                    persisted_timestamp(10),
                    persisted_timestamp(number),
                    f"failure-{number}",
                )
                for number in range(1, 4)
            ),
        )

        first = self.store.attempts_page("invocation-1", limit=2)
        second = self.store.attempts_page(
            "invocation-1",
            after_attempt_number=first[-1].attempt_number,
            limit=2,
        )
        self.assertEqual([item.attempt_number for item in first], [1, 2])
        self.assertEqual([item.attempt_number for item in second], [3])
        self.assertEqual(self.store.attempts_page("invocation-1", 3, 2), ())

    def test_attempt_page_bounds_reject_bool_negative_and_unbounded_limits(self):
        for invalid in (True, False, 1.0, "0", None):
            with self.subTest(cursor=invalid):
                with self.assertRaises(TypeError):
                    self.store.attempts_page(
                        "invocation-1",
                        invalid,  # type: ignore[arg-type]
                        1,
                    )
        for invalid in (-1, 1 << 63):
            with self.subTest(cursor=invalid):
                with self.assertRaises(ValueError):
                    self.store.attempts_page("invocation-1", invalid, 1)
        for invalid in (True, False, 1.0, "1", None):
            with self.subTest(limit=invalid):
                with self.assertRaises(TypeError):
                    self.store.attempts_page(
                        "invocation-1",
                        0,
                        invalid,  # type: ignore[arg-type]
                    )
        for invalid in (-1, 0, 1_001):
            with self.subTest(limit=invalid):
                with self.assertRaises(ValueError):
                    self.store.attempts_page("invocation-1", 0, invalid)

    def test_attempt_page_query_contains_a_sql_limit(self):
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            self.store.attempts_page("invocation-1", limit=7)
        finally:
            self.store._connection.set_trace_callback(None)

        self.assertTrue(
            any(
                "FROM INVOCATION_ATTEMPTS" in statement.upper() and "LIMIT 7" in statement.upper()
                for statement in statements
            )
        )

    def test_recovery_snapshot_is_bounded_and_cross_validates_current_attempt(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        finally:
            self.store._connection.set_trace_callback(None)

        self.assertEqual(snapshot.job.status, InvocationStatus.RUNNING)
        self.assertEqual(snapshot.current_attempt.attempt_id, lease.attempt_id)
        self.assertEqual(snapshot.attempt_count, 1)
        normalized = tuple(" ".join(statement.upper().split()) for statement in statements)
        self.assertIn("BEGIN", normalized)
        self.assertIn("COMMIT", normalized)
        decoded_queries = tuple(
            statement
            for statement in normalized
            if statement.startswith("SELECT * FROM INVOCATION_")
        )
        self.assertEqual(len(decoded_queries), 2)
        self.assertIn("LIMIT 2", decoded_queries[0])
        self.assertIn("ORDER BY ATTEMPT_NUMBER LIMIT 1001", decoded_queries[1])
        self.assertFalse(any("COUNT(*) AS ATTEMPT_COUNT" in statement for statement in normalized))

    def test_recovery_snapshot_rejects_attempt_started_before_job(self):
        self.store.enqueue(job_spec())
        self.clock.set(timestamp(5))
        self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.store._connection.execute(
            "UPDATE invocation_attempts SET started_at = ? WHERE invocation_id = ?",
            ("2026-08-19T23:59:59.000000Z", "invocation-1"),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "starts before its job"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_rejects_job_attempt_finish_divergence(self):
        self.store.enqueue(job_spec(max_attempts=1))
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        self.clock.set(timestamp(5))
        self.assertTrue(self.store.fail(lease, "terminal"))
        self.store._connection.execute(
            """
            UPDATE invocation_jobs SET updated_at = ?, finished_at = ?
            WHERE invocation_id = ?
            """,
            (persisted_timestamp(6), persisted_timestamp(6), "invocation-1"),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "finish differs"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_rejects_backward_attempt_history(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.clock.set(timestamp(1))
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()

        with self.assertRaisesRegex(InvocationIntegrityError, "moves backward in time"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_rejects_historical_attempt_started_before_job(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()
        self.store._connection.execute(
            """
            UPDATE invocation_attempts SET started_at = ?
            WHERE invocation_id = ? AND attempt_number = 1
            """,
            ("2026-08-19T23:59:59.000000Z", "invocation-1"),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "starts before its job"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_decodes_and_rejects_unsafe_historical_attempts(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()

        self.store._connection.execute(
            """
            UPDATE invocation_attempts
            SET status = 'running', finished_at = NULL, error = NULL
            WHERE invocation_id = ? AND attempt_number = 1
            """,
            ("invocation-1",),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "historical invocation attempt"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_strictly_decodes_historical_attempt_semantics(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()
        corruptions = (
            ("result_ref", "result:unexpected", None),
            ("heartbeat_at", "2026-08-19T23:59:59.000000Z", persisted_timestamp(0)),
            ("lease_expires_at", "2026-08-19T23:59:59.000000Z", persisted_timestamp(10)),
            ("finished_at", "2026-08-19T23:59:59.000000Z", persisted_timestamp(0)),
        )
        for column, corrupted, restored in corruptions:
            with self.subTest(column=column):
                self.store._connection.execute(
                    f"""
                    UPDATE invocation_attempts SET {column} = ?
                    WHERE invocation_id = ? AND attempt_number = 1
                    """,
                    (corrupted, "invocation-1"),
                )
                with self.assertRaisesRegex(
                    InvocationIntegrityError,
                    "persisted invocation attempt is malformed",
                ):
                    self.store.recovery_snapshot_for_task("session-1", "task-1")
                self.store._connection.execute(
                    f"""
                    UPDATE invocation_attempts SET {column} = ?
                    WHERE invocation_id = ? AND attempt_number = 1
                    """,
                    (restored, "invocation-1"),
                )

    def test_recovery_snapshot_rejects_non_monotonic_historical_epoch(self):
        self.store.enqueue(job_spec(max_attempts=2))
        first = self.store.claim("invocation-1", "worker-1", lease_seconds=10)
        self.assertTrue(self.store.fail(first, "retry", retry_at=T0))
        self._seed_second_running_attempt()

        self.store._connection.execute(
            """
            UPDATE invocation_attempts SET lease_epoch = 3
            WHERE invocation_id = ? AND attempt_number = 1
            """,
            ("invocation-1",),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "strictly increasing"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_rejects_histories_above_the_supported_limit(self):
        self.store.enqueue(job_spec(max_attempts=1_001))
        self.store._connection.execute(
            """
            UPDATE invocation_jobs
            SET status = 'failed', attempts_started = 1001, lease_epoch = 1001,
                last_error = 'seeded', finished_at = updated_at
            WHERE invocation_id = ?
            """,
            ("invocation-1",),
        )
        self.store._connection.executemany(
            """
            INSERT INTO invocation_attempts (
                attempt_id, invocation_id, attempt_number, lease_epoch,
                worker_id, lease_token_digest, status, started_at,
                heartbeat_at, lease_expires_at, finished_at, error
            ) VALUES (?, 'invocation-1', ?, ?, 'worker', ?, 'failed', ?, ?, ?, ?, 'seeded')
            """,
            (
                (
                    f"attempt-{number}",
                    number,
                    number,
                    "0" * 64,
                    "2026-08-20T00:00:00.000000Z",
                    "2026-08-20T00:00:00.000000Z",
                    "2026-08-20T00:00:01.000000Z",
                    "2026-08-20T00:00:00.000000Z",
                )
                for number in range(1, 1_002)
            ),
        )

        with self.assertRaisesRegex(InvocationIntegrityError, "recovery limit"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")

    def test_recovery_snapshot_remains_consistent_while_wal_writer_advances_heartbeat(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        second_clock = MutableClock(timestamp(5))
        second = SQLiteInvocationAttemptStore(self.path, clock=second_clock)
        original = SQLiteInvocationAttemptStore._row_to_job
        advanced = False

        def advance_after_job_read(row):
            nonlocal advanced
            job = original(row)
            if not advanced:
                advanced = True
                self.assertTrue(second.heartbeat(lease, lease_seconds=20))
            return job

        try:
            with patch.object(
                SQLiteInvocationAttemptStore,
                "_row_to_job",
                side_effect=advance_after_job_read,
            ):
                snapshot = self.store.recovery_snapshot_for_task("session-1", "task-1")
        finally:
            second.close()

        self.assertTrue(advanced)
        self.assertEqual(snapshot.job.heartbeat_at, "2026-08-20T00:00:00.000000Z")
        self.assertEqual(
            snapshot.current_attempt.heartbeat_at,
            "2026-08-20T00:00:00.000000Z",
        )
        self.assertEqual(
            self.store.get("invocation-1").heartbeat_at,
            "2026-08-20T00:00:05.000000Z",
        )

    def test_recovery_snapshot_rejects_cross_row_drift_without_mutation(self):
        self.store.enqueue(job_spec())
        lease = self.store.claim("invocation-1", "worker", lease_seconds=10)
        before = tuple(self.store._connection.iterdump())
        self.store._connection.execute(
            """
            UPDATE invocation_attempts SET heartbeat_at = ?
            WHERE attempt_id = ?
            """,
            ("2026-08-20T00:00:01.000000Z", lease.attempt_id),
        )
        drifted = tuple(self.store._connection.iterdump())
        with self.assertRaisesRegex(InvocationIntegrityError, "ownership differs"):
            self.store.recovery_snapshot_for_task("session-1", "task-1")
        self.assertEqual(tuple(self.store._connection.iterdump()), drifted)
        self.assertNotEqual(before, drifted)

    def test_recovery_snapshot_distinguishes_a_missing_job(self):
        snapshot = self.store.recovery_snapshot_for_task("session-1", "missing-task")
        self.assertIsNone(snapshot.job)
        self.assertIsNone(snapshot.current_attempt)
        self.assertEqual(snapshot.attempt_count, 0)


if __name__ == "__main__":
    unittest.main()
