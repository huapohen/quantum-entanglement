from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

from quantum_entanglement.attempts import InvocationStatus
from quantum_entanglement.invocation_execution import (
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    InvocationExecutionManifest,
    TaskInvocationAdmissionRequest,
    build_task_invocation_admission_request,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition
from quantum_entanglement.store import (
    EventStoreLifecycleError,
    EventStorePoisonedError,
    InvocationAdmissionCommitAmbiguityError,
    SQLiteEventStore,
)

STORE_TIME = "2026-08-27T02:00:00Z"
REQUESTED_AT = "2026-08-27T02:00:00.000001Z"
RUNNING_AT = "2026-08-27T02:00:00.000002Z"


def canonical_request() -> TaskInvocationAdmissionRequest:
    manifest = InvocationExecutionManifest.from_dict(
        {
            "schemaVersion": 1,
            "invocationId": "invocation-canonical-1",
            "sessionId": "session-canonical-1",
            "planId": "plan-canonical-1",
            "taskId": "task-canonical-1",
            "agentId": "agent-canonical-1",
            "jobIdempotencyKey": "invoke:task-canonical-1",
            "taskRevision": 7,
            "correlationId": "correlation-canonical-1",
            "causationId": "task-canonical-1",
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
        execution_requested_event_id="event-execution-requested-canonical-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-task-running-canonical-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=71,
    )


def table_counts(store: SQLiteEventStore) -> tuple[int, int, int]:
    return (
        int(store._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        int(store._connection.execute("SELECT COUNT(*) FROM invocation_jobs").fetchone()[0]),
        int(store._connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[0]),
    )


def inherited_wrapper_outcome(
    store: SQLiteEventStore,
    request: TaskInvocationAdmissionRequest,
) -> bytes:
    try:
        store.append_task_invocation_admission(request, expected_version=0)
    except EventStoreLifecycleError:
        return b"process-mismatch"
    except (TypeError, ValueError, AttributeError):
        return b"request-was-touched"
    return b"unexpected-success"


class TaskInvocationAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_canonical_wrapper_commits_exact_event_job_receipt_unit(self) -> None:
        request = canonical_request()

        result = self.store.append_task_invocation_admission(request, expected_version=0)

        self.assertEqual(table_counts(self.store), (2, 1, 1))
        self.assertEqual([item.sequence for item in result.events], [1, 2])
        self.assertEqual(
            [item.event.event_type for item in result.events],
            [TASK_EXECUTION_REQUESTED_EVENT_TYPE, TASK_STATUS_CHANGED_EVENT_TYPE],
        )
        self.assertEqual(result.events[0].event.payload, request.manifest.to_dict())
        self.assertEqual(
            result.events[1].event.payload,
            {
                "taskId": request.manifest.task_id,
                "previous": "ready",
                "current": "running",
                "reason": None,
                "revision": request.manifest.task_revision,
            },
        )
        self.assertEqual(result.job.status, InvocationStatus.QUEUED)
        self.assertEqual(result.job.max_attempts, 1)
        self.assertEqual(result.job.payload_digest, request.manifest.canonical_digest())

    def test_exact_replay_returns_original_rows_without_duplicate_work(self) -> None:
        request = canonical_request()
        first = self.store.append_task_invocation_admission(request, expected_version=0)

        replay = self.store.append_task_invocation_admission(request, expected_version=0)

        self.assertEqual(replay, first)
        self.assertEqual(table_counts(self.store), (2, 1, 1))

    def test_exact_type_boundary_rejects_subclass_and_legacy_duck_type(self) -> None:
        class RequestSubclass(TaskInvocationAdmissionRequest):
            pass

        class LegacyRequest:
            def components(self) -> object:
                raise AssertionError("legacy request callbacks must not run")

        derived = object.__new__(RequestSubclass)
        for candidate in (derived, LegacyRequest(), canonical_request().components()):
            with self.subTest(candidate_type=type(candidate).__name__):
                with self.assertRaisesRegex(
                    TypeError,
                    "exact TaskInvocationAdmissionRequest",
                ):
                    self.store.append_task_invocation_admission(
                        cast(TaskInvocationAdmissionRequest, candidate),
                        expected_version=0,
                    )
        self.assertEqual(table_counts(self.store), (0, 0, 0))

    def test_low_level_request_forgery_and_component_tampering_fail_before_write(self) -> None:
        forged = canonical_request()
        object.__setattr__(
            forged,
            "task_running_event_id",
            forged.execution_requested_event_id,
        )
        with self.assertRaises(ValueError):
            self.store.append_task_invocation_admission(forged, expected_version=0)

        request = canonical_request()
        events, job = request.components()
        tampered = (events[0], replace(events[1], actor_id="legacy-caller"))
        with mock.patch.object(
            TaskInvocationAdmissionRequest,
            "components",
            return_value=(tampered, job),
        ):
            with self.assertRaises(ValueError):
                self.store.append_task_invocation_admission(request, expected_version=0)

        self.assertEqual(table_counts(self.store), (0, 0, 0))

    def test_instance_method_spoofing_cannot_replace_store_owned_component_path(self) -> None:
        request = canonical_request()

        def spoofed(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("instance method spoof was invoked")

        object.__setattr__(request, "components", spoofed)
        object.__setattr__(request, "validate_components", spoofed)

        result = self.store.append_task_invocation_admission(request, expected_version=0)

        self.assertEqual(len(result.events), 2)
        self.assertEqual(table_counts(self.store), (2, 1, 1))

    def test_expected_version_is_a_required_nonnegative_exact_integer(self) -> None:
        class IntegerSubclass(int):
            pass

        for value in (None, True, IntegerSubclass(0), -1):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.append_task_invocation_admission(
                        canonical_request(),
                        expected_version=cast(int, value),
                    )
        self.assertEqual(table_counts(self.store), (0, 0, 0))

    def test_poisoned_store_rejects_before_revalidating_forged_request(self) -> None:
        forged = canonical_request()
        object.__setattr__(
            forged,
            "task_running_event_id",
            forged.execution_requested_event_id,
        )
        self.store._poisoned = True

        with self.assertRaises(EventStorePoisonedError):
            self.store.append_task_invocation_admission(forged, expected_version=0)

        self.assertEqual(table_counts(self.store), (0, 0, 0))

    def test_commit_ack_loss_is_ambiguous_poisoned_and_exactly_reconcilable(self) -> None:
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
                    raise sqlite3.OperationalError("private commit acknowledgement detail")
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_fault(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection,
                real_connect(*args, **kwargs, factory=CommitAckLossConnection),
            )

        request = canonical_request()
        self.store.close()
        with mock.patch.object(sqlite3, "connect", connect_with_fault):
            fault_store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)
        connection = cast(CommitAckLossConnection, fault_store._connection)
        self.assertIsInstance(connection, CommitAckLossConnection)
        connection.fail_next_commit = True
        try:
            with self.assertRaises(InvocationAdmissionCommitAmbiguityError) as caught:
                fault_store.append_task_invocation_admission(request, expected_version=0)
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("private", str(caught.exception))
            with self.assertRaises(EventStorePoisonedError):
                fault_store.append_task_invocation_admission(request, expected_version=0)
        finally:
            fault_store.close()

        self.store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)
        reconciled = self.store.append_task_invocation_admission(request, expected_version=0)
        self.assertEqual([item.sequence for item in reconciled.events], [1, 2])
        self.assertEqual(table_counts(self.store), (2, 1, 1))

    def test_commit_control_keeps_fresh_signal_and_direct_ambiguity_cause(self) -> None:
        real_connect = sqlite3.connect

        class CommitControlConnection(sqlite3.Connection):
            fail_next_commit = False
            raised_signal: BaseException | None = None

            def execute(
                connection_self,
                statement: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                if statement.strip().upper() == "COMMIT" and connection_self.fail_next_commit:
                    connection_self.fail_next_commit = False
                    super().execute(statement, parameters)  # type: ignore[arg-type]
                    signal = KeyboardInterrupt("private caller control detail")
                    connection_self.raised_signal = signal
                    raise signal
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_control(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection,
                real_connect(*args, **kwargs, factory=CommitControlConnection),
            )

        request = canonical_request()
        self.store.close()
        with mock.patch.object(sqlite3, "connect", connect_with_control):
            control_store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)
        connection = cast(CommitControlConnection, control_store._connection)
        self.assertIsInstance(connection, CommitControlConnection)
        connection.fail_next_commit = True
        try:
            with self.assertRaises(KeyboardInterrupt) as caught:
                control_store.append_task_invocation_admission(request, expected_version=0)
            self.assertIsNot(caught.exception, connection.raised_signal)
            self.assertEqual(str(caught.exception), "")
            self.assertIsNone(caught.exception.__context__)
            cause = caught.exception.__cause__
            self.assertIs(type(cause), InvocationAdmissionCommitAmbiguityError)
            ambiguity = cast(InvocationAdmissionCommitAmbiguityError, cause)
            self.assertIsNone(ambiguity.__traceback__)
            with self.assertRaises(EventStorePoisonedError):
                control_store.append_task_invocation_admission(request, expected_version=0)
        finally:
            control_store.close()

        self.store = SQLiteEventStore(self.path, clock=lambda: STORE_TIME)
        reconciled = self.store.append_task_invocation_admission(request, expected_version=0)
        self.assertEqual([item.sequence for item in reconciled.events], [1, 2])
        self.assertEqual(table_counts(self.store), (2, 1, 1))

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_inherited_store_pid_guard_runs_before_request_validation(self) -> None:
        forged = canonical_request()
        object.__setattr__(
            forged,
            "task_running_event_id",
            forged.execution_requested_event_id,
        )
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                os.write(write_fd, inherited_wrapper_outcome(self.store, forged))
            finally:
                os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        try:
            outcome = os.read(read_fd, 128)
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(read_fd)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(outcome, b"process-mismatch")
        self.assertEqual(table_counts(self.store), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
