from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import quantum_entanglement.store as store_module
from quantum_entanglement.attempts import (
    InvocationJobSpec,
    InvocationStatus,
    SQLiteInvocationAttemptStore,
    invocation_payload_digest,
)
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.migrations import migration_text
from quantum_entanglement.store import (
    ConcurrencyError,
    EventStoreIntegrityError,
    EventStoreLifecycleError,
    EventStorePoisonedError,
    InvocationAdmissionCommitAmbiguityError,
    InvocationAdmissionConflictError,
    InvocationAdmissionTransactionError,
    SQLiteEventStore,
)

T0 = "2026-08-26T00:00:00Z"


def admission_spec(marker: str = "base", **changes: object) -> InvocationJobSpec:
    values: dict[str, object] = {
        "session_id": "session-1",
        "plan_id": "plan-1",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "idempotency_key": "invoke:task-1",
        "payload_digest": invocation_payload_digest({"marker": marker}),
        "invocation_id": "invocation-1",
        "max_attempts": 3,
    }
    values.update(changes)
    return InvocationJobSpec(**values)  # type: ignore[arg-type]


def admission_events(
    marker: str = "base",
    *,
    stream_id: str = "session:session-1",
) -> tuple[DomainEvent, ...]:
    return (
        DomainEvent(
            stream_id,
            "task.execution_requested",
            {"taskId": "task-1", "marker": marker},
            "actor-1",
            event_id="event-requested",
            timestamp=T0,
            idempotency_key="admission:requested",
        ),
        DomainEvent(
            stream_id,
            "task.status_changed",
            {"taskId": "task-1", "status": "running", "marker": marker},
            "orchestrator",
            event_id="event-running",
            timestamp=T0,
            causation_id="event-requested",
            idempotency_key="admission:running",
        ),
    )


def _process_admission_worker(
    path: str,
    marker: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    store = SQLiteEventStore(path, clock=lambda: T0)
    try:
        ready.put(marker)  # type: ignore[attr-defined]
        if not start.wait(timeout=10):  # type: ignore[attr-defined]
            raise RuntimeError("admission race start timed out")
        try:
            store.append_invocation_admission(
                admission_events(marker),
                admission_spec(marker),
                expected_version=0,
            )
        except InvocationAdmissionConflictError:
            outcome = "conflict"
        else:
            outcome = "won"
        results.put((marker, outcome))  # type: ignore[attr-defined]
    finally:
        store.close()


def _inherited_store_outcome(store: SQLiteEventStore) -> bytes:
    try:
        store.append_invocation_admission(
            admission_events(),
            admission_spec(),
            expected_version=0,
        )
    except EventStoreLifecycleError:
        return b"process-mismatch"
    return b"unexpected-success"


class InvocationAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "state.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _job_count(self) -> int:
        return int(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_jobs").fetchone()[0]
        )

    def test_admission_atomically_appends_running_transition_and_queued_job(self) -> None:
        result = self.store.append_invocation_admission(
            admission_events(),
            admission_spec(),
            expected_version=0,
        )

        self.assertEqual([item.sequence for item in result.events], [1, 2])
        self.assertEqual(result.events[-1].event.payload["status"], "running")
        self.assertIs(result.job.status, InvocationStatus.QUEUED)
        self.assertEqual(
            result.events[0].event.stream_id,
            f"session:{result.job.session_id}",
        )
        self.assertEqual(self.store.stream_version("session:session-1"), 2)
        self.assertEqual(self._job_count(), 1)

    def test_exact_full_retry_returns_original_events_and_job(self) -> None:
        first = self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        retried = self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )

        self.assertEqual(retried, first)
        self.assertEqual(self.store.stream_version("session:session-1"), 2)
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[
                0
            ],
            1,
        )

    def test_migration_down_removes_only_receipts_and_preserves_events_and_jobs(self) -> None:
        self.store.append_invocation_admission(
            admission_events(),
            admission_spec(),
            expected_version=0,
        )
        before = {
            table: self.store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("events", "invocation_jobs", "invocation_admissions")
        }
        self.assertEqual(before, {"events": 2, "invocation_jobs": 1, "invocation_admissions": 1})

        self.store.close()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.executescript(migration_text("0004_invocation_admissions.down.sql"))
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name = 'invocation_admissions'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name = 'idx_invocation_admissions_stream'"
                ).fetchone()
            )
            self.assertEqual(
                {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("events", "invocation_jobs")
                },
                {"events": 2, "invocation_jobs": 1},
            )
        finally:
            connection.close()

    def test_expected_version_is_checked_for_new_and_idempotent_admission(self) -> None:
        self.store.append(
            DomainEvent(
                "session:session-1",
                "session.started",
                {},
                "actor-1",
                event_id="event-seed",
                timestamp=T0,
            ),
            expected_version=0,
        )
        accepted = self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=1
        )
        self.assertEqual([item.sequence for item in accepted.events], [2, 3])
        self.assertEqual(
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=1
            ),
            accepted,
        )
        with self.assertRaises(ConcurrencyError):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

    def test_stream_event_job_and_order_variations_fail_closed(self) -> None:
        for invalid_stream in ("session-1", "session:other-session"):
            with self.subTest(invalid_stream=invalid_stream):
                with self.assertRaisesRegex(ValueError, "session:<spec.session_id>"):
                    self.store.append_invocation_admission(
                        admission_events(stream_id=invalid_stream),
                        admission_spec(),
                        expected_version=0,
                    )

        self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        cases = (
            (admission_events("changed"), admission_spec(), "event"),
            (admission_events(), admission_spec(agent_id="other-agent"), "job"),
            (
                admission_events(),
                admission_spec(invocation_id="other-invocation"),
                "invocation-id",
            ),
            (
                (
                    DomainEvent(
                        "session:session-1",
                        "task.execution_requested",
                        {"taskId": "task-1", "marker": "base"},
                        "actor-1",
                        event_id="other-event-id",
                        timestamp=T0,
                        idempotency_key="admission:requested",
                    ),
                    admission_events()[1],
                ),
                admission_spec(),
                "event-id",
            ),
            (tuple(reversed(admission_events())), admission_spec(), "order"),
        )
        for events, spec, label in cases:
            with self.subTest(label=label):
                with self.assertRaises(InvocationAdmissionConflictError):
                    self.store.append_invocation_admission(events, spec, expected_version=0)

    def test_partial_event_binding_cannot_be_healed_by_admission(self) -> None:
        self.store.append(admission_events()[0], expected_version=0)

        with self.assertRaisesRegex(InvocationAdmissionConflictError, "partial"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

        self.assertEqual(self.store.stream_version("session:session-1"), 1)
        self.assertEqual(self._job_count(), 0)

    def test_partial_job_binding_cannot_be_healed_by_admission(self) -> None:
        attempt_store = SQLiteInvocationAttemptStore(self.path, clock=lambda: T0)
        try:
            attempt_store.enqueue(admission_spec())
        finally:
            attempt_store.close()

        with self.assertRaisesRegex(InvocationAdmissionConflictError, "partial"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

        self.assertEqual(self.store.stream_version("session:session-1"), 0)
        self.assertEqual(self._job_count(), 1)

    def test_full_split_binding_without_receipt_cannot_be_mistaken_for_replay(self) -> None:
        self.store.append_many(
            "session:session-1",
            admission_events(),
            expected_version=0,
        )
        attempt_store = SQLiteInvocationAttemptStore(self.path, clock=lambda: T0)
        try:
            attempt_store.enqueue(admission_spec())
        finally:
            attempt_store.close()

        with self.assertRaisesRegex(InvocationAdmissionConflictError, "receipt"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

        self.assertEqual(self.store.stream_version("session:session-1"), 2)
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[
                0
            ],
            0,
        )

    def test_cross_stream_split_binding_without_receipt_cannot_be_replayed(self) -> None:
        first, second = admission_events()
        self.store.append(first, expected_version=0)
        self.store.append(
            DomainEvent(
                "session:other",
                "test.noise",
                {},
                "actor-1",
                event_id="event-noise",
                timestamp=T0,
            ),
            expected_version=0,
        )
        self.store.append(second, expected_version=1)
        attempt_store = SQLiteInvocationAttemptStore(self.path, clock=lambda: T0)
        try:
            attempt_store.enqueue(admission_spec())
        finally:
            attempt_store.close()

        with self.assertRaisesRegex(InvocationAdmissionConflictError, "receipt"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

        positions = [item.global_position for item in self.store.read_stream("session:session-1")]
        self.assertEqual(positions, [1, 3])
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[
                0
            ],
            0,
        )

    def test_missing_receipt_after_admission_is_an_unproven_conflict(self) -> None:
        self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        self.store._connection.execute(
            "DELETE FROM invocation_admissions WHERE invocation_id = ?",
            (admission_spec().invocation_id,),
        )

        # Once the receipt itself is gone, this state is intentionally
        # indistinguishable from independently assembled full split state. It must not
        # be healed or accepted; the stable classification is an unproven conflict.
        with self.assertRaisesRegex(InvocationAdmissionConflictError, "receipt"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

    def test_tampered_receipt_manifest_is_an_integrity_failure(self) -> None:
        self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        self.store._connection.execute(
            """
            UPDATE invocation_admissions
            SET event_manifest_sha256 = ?
            WHERE invocation_id = ?
            """,
            ("0" * 64, admission_spec().invocation_id),
        )

        with self.assertRaisesRegex(EventStoreIntegrityError, "manifest"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

    def test_receipt_with_missing_event_is_an_integrity_failure(self) -> None:
        self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        self.store._connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.store._connection.execute(
                "DELETE FROM events WHERE event_id = ?",
                (admission_events()[0].event_id,),
            )
        finally:
            self.store._connection.execute("PRAGMA foreign_keys=ON")

        with self.assertRaisesRegex(EventStoreIntegrityError, "missing event"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

    def test_receipt_with_missing_job_is_an_integrity_failure(self) -> None:
        self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        self.store._connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.store._connection.execute(
                "DELETE FROM invocation_jobs WHERE invocation_id = ?",
                (admission_spec().invocation_id,),
            )
        finally:
            self.store._connection.execute("PRAGMA foreign_keys=ON")

        with self.assertRaisesRegex(EventStoreIntegrityError, "missing job"):
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

    def test_fault_after_job_insert_rolls_back_events_and_job(self) -> None:
        real_enqueue = store_module._enqueue_invocation_job_in_transaction

        def insert_then_fail(*args: object, **kwargs: object) -> object:
            real_enqueue(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("fault after queued job insert")

        with mock.patch.object(
            store_module,
            "_enqueue_invocation_job_in_transaction",
            side_effect=insert_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "fault after queued job"):
                self.store.append_invocation_admission(
                    admission_events(), admission_spec(), expected_version=0
                )

        self.assertEqual(self.store.stream_version("session:session-1"), 0)
        self.assertEqual(self._job_count(), 0)

    def test_denied_commit_fails_closed_and_rolls_back_both_sides(self) -> None:
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
            with self.assertRaises(InvocationAdmissionTransactionError) as caught:
                self.store.append_invocation_admission(
                    admission_events(), admission_spec(), expected_version=0
                )
        finally:
            self.store._connection.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)

        self.assertFalse(self.store._connection.in_transaction)
        self.assertIsNone(caught.exception.__context__)
        self.assertFalse(self.store._poisoned)
        self.assertEqual(self.store.stream_version("session:session-1"), 0)
        self.assertEqual(self._job_count(), 0)

    def test_driver_error_is_sanitized_after_confirmed_body_rollback(self) -> None:
        self.store._connection.execute("DROP TABLE invocation_jobs")

        with self.assertRaises(InvocationAdmissionTransactionError) as caught:
            self.store.append_invocation_admission(
                admission_events(), admission_spec(), expected_version=0
            )

        self.assertEqual(caught.exception.code, "invocation_admission_transaction_failed")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertFalse(self.store._poisoned)
        self.assertEqual(self.store.stream_version("session:session-1"), 0)

    def test_begin_ack_loss_is_rolled_back_without_poisoning(self) -> None:
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

        def connect_with_fault(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=BeginAckLossConnection)  # type: ignore[arg-type]

        self.store.close()
        with mock.patch.object(store_module.sqlite3, "connect", connect_with_fault):
            begin_store = SQLiteEventStore(self.path, clock=lambda: T0)
        connection = begin_store._connection
        self.assertIsInstance(connection, BeginAckLossConnection)
        connection.fail_next_begin = True
        try:
            with self.assertRaises(InvocationAdmissionTransactionError):
                begin_store.append_invocation_admission(
                    admission_events(), admission_spec(), expected_version=0
                )
            self.assertFalse(connection.in_transaction)
            self.assertFalse(begin_store._poisoned)
            self.assertEqual(begin_store.stream_version("session:session-1"), 0)
        finally:
            begin_store.close()
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def test_rollback_failure_poisons_and_hides_driver_details(self) -> None:
        real_connect = sqlite3.connect

        class RollbackFailureConnection(sqlite3.Connection):
            fail_next_rollback = False

            def execute(
                connection_self,
                statement: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                if statement.strip().upper() == "ROLLBACK" and connection_self.fail_next_rollback:
                    connection_self.fail_next_rollback = False
                    raise sqlite3.OperationalError("private rollback driver detail")
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_fault(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=RollbackFailureConnection)  # type: ignore[arg-type]

        self.store.close()
        with mock.patch.object(store_module.sqlite3, "connect", connect_with_fault):
            fault_store = SQLiteEventStore(self.path, clock=lambda: T0)
        connection = fault_store._connection
        self.assertIsInstance(connection, RollbackFailureConnection)
        connection.fail_next_rollback = True
        try:
            with mock.patch.object(
                store_module,
                "_enqueue_invocation_job_in_transaction",
                side_effect=RuntimeError("body fault"),
            ):
                with self.assertRaises(InvocationAdmissionCommitAmbiguityError) as caught:
                    fault_store.append_invocation_admission(
                        admission_events(), admission_spec(), expected_version=0
                    )
            self.assertNotIn("rollback", str(caught.exception).lower())
            with self.assertRaises(EventStorePoisonedError):
                fault_store.stream_version("session:session-1")
        finally:
            fault_store.close()
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def test_rollback_control_signals_are_clean_and_carry_ambiguity(self) -> None:
        real_connect = sqlite3.connect

        class RollbackControlConnection(sqlite3.Connection):
            signal_name: str | None = None
            raised_signal: BaseException | None = None

            def execute(
                connection_self,
                statement: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                if statement.strip().upper() == "ROLLBACK" and connection_self.signal_name:
                    signal_name = connection_self.signal_name
                    connection_self.signal_name = None
                    super().execute(statement, parameters)  # type: ignore[arg-type]
                    if signal_name == "keyboard_interrupt":
                        signal: BaseException = KeyboardInterrupt()
                    elif signal_name == "system_exit":
                        signal = SystemExit(31)
                    elif signal_name == "generator_exit":
                        signal = GeneratorExit()
                    elif signal_name == "cancelled":
                        signal = CancelledError()
                    else:  # pragma: no cover - closed test table.
                        raise AssertionError("unknown rollback control signal")
                    connection_self.raised_signal = signal
                    raise signal
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_control(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=RollbackControlConnection)  # type: ignore[arg-type]

        cases: tuple[tuple[str, type[BaseException]], ...] = (
            ("keyboard_interrupt", KeyboardInterrupt),
            ("system_exit", SystemExit),
            ("generator_exit", GeneratorExit),
            ("cancelled", CancelledError),
        )
        self.store.close()
        for signal_name, signal_type in cases:
            with self.subTest(signal=signal_name):
                control_path = str(Path(self.tempdir.name) / f"rollback-{signal_name}.sqlite3")
                with mock.patch.object(store_module.sqlite3, "connect", connect_with_control):
                    control_store = SQLiteEventStore(control_path, clock=lambda: T0)
                connection = control_store._connection
                self.assertIsInstance(connection, RollbackControlConnection)
                connection.execute("DROP TABLE invocation_jobs")
                connection.signal_name = signal_name
                try:
                    with self.assertRaises(signal_type) as caught:
                        control_store.append_invocation_admission(
                            admission_events(), admission_spec(), expected_version=0
                        )
                    self.assertIsNot(caught.exception, connection.raised_signal)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertIs(
                        type(caught.exception.__cause__),
                        InvocationAdmissionCommitAmbiguityError,
                    )
                    if signal_type is SystemExit:
                        self.assertEqual(caught.exception.code, 31)
                    with self.assertRaises(EventStorePoisonedError):
                        control_store.stream_version("session:session-1")
                finally:
                    control_store.close()
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def test_commit_ack_loss_is_explicit_and_exact_retry_reconciles_after_reopen(self) -> None:
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

        def connect_with_fault(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=CommitAckLossConnection)  # type: ignore[arg-type]

        self.store.close()
        with mock.patch.object(store_module.sqlite3, "connect", connect_with_fault):
            fault_store = SQLiteEventStore(self.path, clock=lambda: T0)
        connection = fault_store._connection
        self.assertIsInstance(connection, CommitAckLossConnection)
        connection.fail_next_commit = True
        try:
            with self.assertRaises(InvocationAdmissionCommitAmbiguityError) as caught:
                fault_store.append_invocation_admission(
                    admission_events(), admission_spec(), expected_version=0
                )
            self.assertEqual(caught.exception.code, "invocation_admission_commit_ambiguous")
            with self.assertRaises(EventStorePoisonedError):
                fault_store.stream_version("session:session-1")
        finally:
            fault_store.close()

        self.store = SQLiteEventStore(self.path, clock=lambda: T0)
        reconciled = self.store.append_invocation_admission(
            admission_events(), admission_spec(), expected_version=0
        )
        self.assertEqual([item.sequence for item in reconciled.events], [1, 2])
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[
                0
            ],
            1,
        )

    def test_commit_control_signals_remain_clean_and_carry_ambiguity(self) -> None:
        real_connect = sqlite3.connect

        class CommitControlConnection(sqlite3.Connection):
            signal_name: str | None = None
            raised_signal: BaseException | None = None

            def execute(
                connection_self,
                statement: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                if (
                    statement.strip().upper() == "COMMIT"
                    and connection_self.signal_name is not None
                ):
                    signal_name = connection_self.signal_name
                    connection_self.signal_name = None
                    super().execute(statement, parameters)  # type: ignore[arg-type]
                    if signal_name == "keyboard_interrupt":
                        signal: BaseException = KeyboardInterrupt()
                    elif signal_name == "system_exit":
                        signal = SystemExit(23)
                    elif signal_name == "generator_exit":
                        signal = GeneratorExit()
                    elif signal_name == "cancelled":
                        signal = CancelledError()
                    else:  # pragma: no cover - test table is closed over exact names.
                        raise AssertionError("unknown test control signal")
                    connection_self.raised_signal = signal
                    raise signal
                return super().execute(statement, parameters)  # type: ignore[arg-type]

        def connect_with_control(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=CommitControlConnection)  # type: ignore[arg-type]

        cases: tuple[tuple[str, type[BaseException]], ...] = (
            ("keyboard_interrupt", KeyboardInterrupt),
            ("system_exit", SystemExit),
            ("generator_exit", GeneratorExit),
            ("cancelled", CancelledError),
        )
        for signal_name, signal_type in cases:
            with self.subTest(signal=signal_name):
                control_path = str(Path(self.tempdir.name) / f"{signal_name}.sqlite3")
                with mock.patch.object(
                    store_module.sqlite3,
                    "connect",
                    connect_with_control,
                ):
                    control_store = SQLiteEventStore(control_path, clock=lambda: T0)
                connection = control_store._connection
                self.assertIsInstance(connection, CommitControlConnection)
                connection.signal_name = signal_name
                try:
                    with self.assertRaises(signal_type) as caught:
                        control_store.append_invocation_admission(
                            admission_events(),
                            admission_spec(),
                            expected_version=0,
                        )
                    self.assertIsNot(caught.exception, connection.raised_signal)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertIs(
                        type(caught.exception.__cause__),
                        InvocationAdmissionCommitAmbiguityError,
                    )
                    self.assertIsNone(caught.exception.__cause__.__traceback__)
                    if signal_type is SystemExit:
                        self.assertEqual(caught.exception.code, 23)
                    with self.assertRaises(EventStorePoisonedError):
                        control_store.stream_version("session:session-1")
                finally:
                    control_store.close()

                reopened = SQLiteEventStore(control_path, clock=lambda: T0)
                try:
                    reconciled = reopened.append_invocation_admission(
                        admission_events(), admission_spec(), expected_version=0
                    )
                    self.assertEqual([item.sequence for item in reconciled.events], [1, 2])
                    self.assertEqual(
                        reopened._connection.execute(
                            "SELECT COUNT(*) FROM invocation_admissions"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    reopened.close()

    def test_two_connections_with_changed_binding_have_exactly_one_winner(self) -> None:
        peer = SQLiteEventStore(self.path, clock=lambda: T0)
        barrier = threading.Barrier(2)

        def compete(store: SQLiteEventStore, marker: str) -> str:
            barrier.wait(timeout=5)
            try:
                store.append_invocation_admission(
                    admission_events(marker),
                    admission_spec(marker),
                    expected_version=0,
                )
            except InvocationAdmissionConflictError:
                return "conflict"
            return "won"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(compete, self.store, "left"),
                    executor.submit(compete, peer, "right"),
                )
                outcomes = {future.result(timeout=10) for future in futures}
        finally:
            peer.close()

        self.assertEqual(outcomes, {"won", "conflict"})
        self.assertEqual(self.store.stream_version("session:session-1"), 2)
        self.assertEqual(self._job_count(), 1)

    def test_waiting_read_write_and_admission_recheck_poison_under_lock(self) -> None:
        class GatedLock:
            def __init__(self, inner: object) -> None:
                self.inner = inner
                self.waiting = threading.Event()
                self.proceed = threading.Event()

            def acquire(self, *args: object, **kwargs: object) -> object:
                self.waiting.set()
                if not self.proceed.wait(timeout=5):
                    raise RuntimeError("gated lock release timed out")
                return self.inner.acquire(*args, **kwargs)  # type: ignore[attr-defined]

            def release(self) -> None:
                self.inner.release()  # type: ignore[attr-defined]

        operations = (
            ("read", lambda store: store.stream_version("session:session-1")),
            (
                "write",
                lambda store: store.append(
                    DomainEvent(
                        "session:session-1",
                        "test.write",
                        {},
                        "actor-1",
                        event_id="event-write",
                        timestamp=T0,
                    ),
                    expected_version=0,
                ),
            ),
            (
                "admission",
                lambda store: store.append_invocation_admission(
                    admission_events(), admission_spec(), expected_version=0
                ),
            ),
        )
        self.store.close()
        for name, operation in operations:
            with self.subTest(operation=name):
                operation_path = str(Path(self.tempdir.name) / f"poison-{name}.sqlite3")
                store = SQLiteEventStore(operation_path, clock=lambda: T0)
                gated_lock = GatedLock(store._lock)
                store._lock = gated_lock  # type: ignore[assignment]

                def invoke(operation=operation, store=store) -> str:
                    try:
                        operation(store)
                    except EventStorePoisonedError:
                        return "poisoned"
                    return "unexpected-success"

                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(invoke)
                    self.assertTrue(gated_lock.waiting.wait(timeout=5))
                    store._poisoned = True
                    gated_lock.proceed.set()
                    self.assertEqual(future.result(timeout=5), "poisoned")
                store.close()

                reopened = SQLiteEventStore(operation_path, clock=lambda: T0)
                try:
                    self.assertEqual(reopened.stream_version("session:session-1"), 0)
                    self.assertEqual(
                        reopened._connection.execute(
                            "SELECT COUNT(*) FROM invocation_jobs"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    reopened.close()
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def test_two_processes_with_changed_binding_have_exactly_one_winner(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_process_admission_worker,
                args=(self.path, marker, ready, start, results),
            )
            for marker in ("left", "right")
        ]
        for process in processes:
            process.start()
        try:
            self.assertEqual({ready.get(timeout=15), ready.get(timeout=15)}, {"left", "right"})
            start.set()
            outcomes = {results.get(timeout=15)[1], results.get(timeout=15)[1]}
        finally:
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertEqual(outcomes, {"won", "conflict"})

        self.assertEqual(self.store.stream_version("session:session-1"), 2)
        self.assertEqual(self._job_count(), 1)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_inherited_store_rejects_admission_before_sqlite_access(self) -> None:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                os.write(write_fd, _inherited_store_outcome(self.store))
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
        self.assertEqual(self.store.stream_version("session:session-1"), 0)
        self.assertEqual(self._job_count(), 0)


if __name__ == "__main__":
    unittest.main()
