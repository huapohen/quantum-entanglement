from __future__ import annotations

import sqlite3
import tempfile
import unittest
from asyncio import CancelledError
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast
from unittest import mock

import quantum_entanglement.attempts as attempts_module
import quantum_entanglement.store as store_module
from quantum_entanglement.invocation_execution import (
    ScopedInvocationExecutionManifestV2,
    ScopedInvocationStartObservedV3,
    ScopedTaskInvocationAdmissionRequestV2,
    build_scoped_task_invocation_admission_request_v2,
)
from quantum_entanglement.protocol import TaskStatus
from quantum_entanglement.scheduler import TaskTransition
from quantum_entanglement.store import (
    EventStorePoisonedError,
    InvocationStartCommitAmbiguityError,
    InvocationStartTransactionError,
    SQLiteEventStore,
)

STORE_TIME = "2026-08-27T12:00:00Z"
REQUESTED_AT = "2026-08-27T12:00:00.000001Z"
RUNNING_AT = "2026-08-27T12:00:00.000002Z"
CLAIMED_AT = "2026-08-27T12:00:01.000000Z"
_REAL_CONNECT = sqlite3.connect


def scoped_request() -> ScopedTaskInvocationAdmissionRequestV2:
    manifest = ScopedInvocationExecutionManifestV2.from_dict(
        {
            "schemaVersion": 2,
            "tenantId": "tenant-scoped-faults-1",
            "workspaceId": "workspace-scoped-faults-1",
            "invocationId": "invocation-scoped-faults-1",
            "sessionId": "session-scoped-faults-1",
            "planId": "plan-scoped-faults-1",
            "taskId": "task-scoped-faults-1",
            "agentId": "agent-scoped-faults-1",
            "jobIdempotencyKey": "invoke:task-scoped-faults-1",
            "taskRevision": 7,
            "correlationId": "correlation-scoped-faults-1",
            "causationId": "task-scoped-faults-1",
            "envelopeDigest": "a" * 64,
            "contextDigest": "b" * 64,
            "authorizationDigest": "c" * 64,
            "runtimeRevision": "runtime:sha256:" + ("d" * 64),
            "effectClass": "pure",
            "retryClass": "never",
        }
    )
    return build_scoped_task_invocation_admission_request_v2(
        manifest,
        TaskTransition(
            task_id=manifest.task_id,
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=None,
            revision=manifest.task_revision,
        ),
        execution_requested_event_id="event-scoped-request-faults-1",
        execution_requested_timestamp=REQUESTED_AT,
        task_running_event_id="event-scoped-running-faults-1",
        task_running_timestamp=RUNNING_AT,
        job_priority=71,
    )


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


class TransactionFaultConnection(sqlite3.Connection):
    """SQLite connection whose transaction acknowledgement can be interrupted once."""

    fault_statement: str | None = None
    fault_kind: str | None = None
    signal_name: str | None = None
    signal_detail = "private transaction control detail"
    system_exit_code: object = None
    raised_signal: BaseException | None = None

    def arm(
        self,
        statement: str,
        *,
        kind: str,
        signal_name: str | None = None,
        signal_detail: str = "private transaction control detail",
        system_exit_code: object = None,
    ) -> None:
        self.fault_statement = " ".join(statement.split()).upper()
        self.fault_kind = kind
        self.signal_name = signal_name
        self.signal_detail = signal_detail
        self.system_exit_code = system_exit_code
        self.raised_signal = None

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        normalized = " ".join(statement.split()).upper()
        if self.fault_statement != normalized:
            return super().execute(statement, parameters)  # type: ignore[arg-type]

        fault_kind = self.fault_kind
        signal_name = self.signal_name
        signal_detail = self.signal_detail
        system_exit_code = self.system_exit_code
        self.fault_statement = None
        self.fault_kind = None
        self.signal_name = None
        super().execute(statement, parameters)  # type: ignore[arg-type]
        if fault_kind == "ack_loss":
            raise sqlite3.OperationalError("private transaction acknowledgement detail")
        if fault_kind != "control":  # pragma: no cover - the test table is closed.
            raise AssertionError("unknown transaction fault kind")
        if signal_name == "keyboard":
            signal: BaseException = KeyboardInterrupt(signal_detail)
        elif signal_name == "system-exit":
            signal = SystemExit(system_exit_code)
        elif signal_name == "generator-exit":
            signal = GeneratorExit(signal_detail)
        elif signal_name == "cancelled":
            signal = CancelledError(signal_detail)
        else:  # pragma: no cover - the test table is closed.
            raise AssertionError("unknown transaction control signal")
        self.raised_signal = signal
        raise signal


def connect_with_transaction_fault(database: str, **kwargs: Any) -> sqlite3.Connection:
    return cast(
        sqlite3.Connection,
        _REAL_CONNECT(database, factory=TransactionFaultConnection, **kwargs),
    )


def open_fault_store(path: str) -> SQLiteEventStore:
    with mock.patch.object(
        store_module.sqlite3,
        "connect",
        new=connect_with_transaction_fault,
    ):
        return SQLiteEventStore(path, clock=lambda: STORE_TIME)


class ScopedInvocationStartFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def path(self, name: str) -> str:
        return str(Path(self.tempdir.name) / f"{name}.sqlite3")

    def claim(
        self,
        store: SQLiteEventStore,
        request: ScopedTaskInvocationAdmissionRequestV2,
        *,
        worker_id: str = "worker-scoped-faults-1",
    ) -> object:
        return store.claim_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
            worker_id,
            lease_seconds=60,
            expected_version=2,
        )

    def assert_unstarted(
        self,
        store: SQLiteEventStore,
        request: ScopedTaskInvocationAdmissionRequestV2,
        *,
        raw_token: str | None = None,
    ) -> None:
        job = store._connection.execute(
            """
            SELECT status, attempts_started, lease_epoch, lease_owner, lease_token_digest
            FROM invocation_jobs WHERE invocation_id = ?
            """,
            (request.manifest.invocation_id,),
        ).fetchone()
        self.assertEqual(tuple(job), ("queued", 0, 0, None, None))
        self.assertEqual(store.stream_version(request.stream_id), 2)
        self.assertEqual(
            store._connection.execute(
                "SELECT COUNT(*) FROM events WHERE stream_id = ?",
                (request.stream_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            store._connection.execute("SELECT COUNT(*) FROM invocation_jobs").fetchone()[0],
            1,
        )
        self.assertEqual(
            store._connection.execute("SELECT COUNT(*) FROM invocation_admissions").fetchone()[0],
            1,
        )
        self.assertIsNone(
            store.read_scoped_invocation_start_v3(
                request.manifest.tenant_id,
                request.manifest.workspace_id,
                request.manifest.invocation_id,
            )
        )
        self.assertFalse(store._poisoned)
        if raw_token is not None:
            self.assertNotIn(raw_token, "\n".join(store._connection.iterdump()))

    def assert_started_receipt_only(
        self,
        store: SQLiteEventStore,
        request: ScopedTaskInvocationAdmissionRequestV2,
        *,
        raw_token: str,
    ) -> None:
        observed = store.read_scoped_invocation_start_v3(
            request.manifest.tenant_id,
            request.manifest.workspace_id,
            request.manifest.invocation_id,
        )
        self.assertIs(type(observed), ScopedInvocationStartObservedV3)
        observed = cast(ScopedInvocationStartObservedV3, observed)
        self.assertEqual(observed.receipt.sequence, 3)
        self.assertEqual(observed.receipt.evidence.schema_version, 3)
        self.assertEqual(observed.receipt.evidence.tenant_id, request.manifest.tenant_id)
        self.assertEqual(
            observed.receipt.evidence.workspace_id,
            request.manifest.workspace_id,
        )
        with (
            mock.patch.object(store, "_now", side_effect=AssertionError("clock must not run")),
            mock.patch.object(
                store_module,
                "new_id",
                side_effect=AssertionError("identifier provider must not run"),
            ),
            mock.patch(
                "quantum_entanglement.store.secrets.token_urlsafe",
                side_effect=AssertionError("lease provider must not run"),
            ),
        ):
            replay = self.claim(store, request, worker_id="worker-scoped-reconcile")
        self.assertIs(type(replay), ScopedInvocationStartObservedV3)
        self.assertEqual(replay, observed)
        self.assertFalse(hasattr(replay, "lease"))
        self.assertEqual(store.stream_version(request.stream_id), 3)
        self.assertEqual(
            store._connection.execute(
                "SELECT COUNT(*) FROM invocation_attempts WHERE invocation_id = ?",
                (request.manifest.invocation_id,),
            ).fetchone()[0],
            1,
        )
        self.assertNotIn(raw_token, "\n".join(store._connection.iterdump()))

    def test_every_scoped_post_mutation_body_fault_rolls_back_the_atomic_start(self) -> None:
        for stage in ("after-cas", "after-start-append", "after-fresh-readback"):
            with self.subTest(stage=stage):
                request = scoped_request()
                raw_token = f"scoped-body-rollback-token-{stage}"
                with SQLiteEventStore(
                    self.path(f"body-{stage}"),
                    clock=lambda: STORE_TIME,
                ) as store:
                    store.append_scoped_task_invocation_admission_v2(
                        request,
                        expected_version=0,
                    )
                    original_cas = attempts_module._claim_first_invocation_in_transaction
                    original_append = store._append_in_transaction
                    original_readback = store._read_scoped_invocation_start_in_transaction

                    def fail_after_cas(
                        *args: Any,
                        _original: Any = original_cas,
                        **kwargs: Any,
                    ) -> Any:
                        _original(*args, **kwargs)
                        raise RuntimeError("private post-CAS body detail")

                    def fail_after_append(
                        *args: Any,
                        _original: Any = original_append,
                        **kwargs: Any,
                    ) -> Any:
                        _original(*args, **kwargs)
                        raise RuntimeError("private post-append body detail")

                    def fail_after_readback(
                        *args: Any,
                        _original: Any = original_readback,
                        **kwargs: Any,
                    ) -> Any:
                        result = _original(*args, **kwargs)
                        if kwargs.get("fresh") is True and result is not None:
                            raise RuntimeError("private post-readback body detail")
                        return result

                    with ExitStack() as patches:
                        patches.enter_context(
                            mock.patch.object(store, "_now", return_value=CLAIMED_AT)
                        )
                        patches.enter_context(
                            mock.patch(
                                "quantum_entanglement.store.secrets.token_urlsafe",
                                return_value=raw_token,
                            )
                        )
                        if stage == "after-cas":
                            patches.enter_context(
                                mock.patch.object(
                                    store_module,
                                    "_claim_first_invocation_in_transaction",
                                    side_effect=fail_after_cas,
                                )
                            )
                        elif stage == "after-start-append":
                            patches.enter_context(
                                mock.patch.object(
                                    store,
                                    "_append_in_transaction",
                                    side_effect=fail_after_append,
                                )
                            )
                        else:
                            patches.enter_context(
                                mock.patch.object(
                                    store,
                                    "_read_scoped_invocation_start_in_transaction",
                                    side_effect=fail_after_readback,
                                )
                            )
                        with self.assertRaises(InvocationStartTransactionError) as caught:
                            self.claim(store, request)

                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertNotIn(
                        raw_token,
                        event_store_traceback_locals(caught.exception),
                    )
                    self.assert_unstarted(store, request, raw_token=raw_token)

    def test_begin_ack_loss_rolls_back_before_scoped_authority_allocation(self) -> None:
        request = scoped_request()
        path = self.path("begin-ack-loss")
        with SQLiteEventStore(path, clock=lambda: STORE_TIME) as seed_store:
            seed_store.append_scoped_task_invocation_admission_v2(
                request,
                expected_version=0,
            )

        with open_fault_store(path) as store:
            connection = cast(TransactionFaultConnection, store._connection)
            self.assertIsInstance(connection, TransactionFaultConnection)
            connection.arm("BEGIN IMMEDIATE", kind="ack_loss")
            with (
                mock.patch.object(store, "_now", side_effect=AssertionError("clock must not run")),
                mock.patch.object(
                    store_module,
                    "new_id",
                    side_effect=AssertionError("identifier provider must not run"),
                ),
                mock.patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    side_effect=AssertionError("lease provider must not run"),
                ),
            ):
                with self.assertRaises(InvocationStartTransactionError) as caught:
                    self.claim(store, request)

            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("acknowledgement", str(caught.exception).lower())
            self.assertNotIn(
                "private transaction acknowledgement detail",
                event_store_traceback_locals(caught.exception),
            )
            self.assertFalse(connection.in_transaction)
            self.assert_unstarted(store, request)

    def test_commit_ack_loss_is_ambiguous_and_reopens_receipt_only(self) -> None:
        request = scoped_request()
        raw_token = "scoped-commit-ack-loss-token"
        path = self.path("commit-ack-loss")
        with open_fault_store(path) as store:
            store.append_scoped_task_invocation_admission_v2(request, expected_version=0)
            connection = cast(TransactionFaultConnection, store._connection)
            connection.arm("COMMIT", kind="ack_loss")
            with (
                mock.patch.object(store, "_now", return_value=CLAIMED_AT),
                mock.patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    return_value=raw_token,
                ),
            ):
                with self.assertRaises(InvocationStartCommitAmbiguityError) as caught:
                    self.claim(store, request)

            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            traceback_locals = event_store_traceback_locals(caught.exception)
            self.assertNotIn(raw_token, traceback_locals)
            self.assertNotIn("private transaction acknowledgement detail", traceback_locals)
            with self.assertRaises(EventStorePoisonedError):
                store.stream_version(request.stream_id)

        with SQLiteEventStore(path, clock=lambda: STORE_TIME) as reopened:
            self.assert_started_receipt_only(
                reopened,
                request,
                raw_token=raw_token,
            )

    def test_rollback_ack_loss_is_ambiguous_and_reopens_unstarted(self) -> None:
        request = scoped_request()
        raw_token = "scoped-rollback-ack-loss-token"
        path = self.path("rollback-ack-loss")
        with open_fault_store(path) as store:
            store.append_scoped_task_invocation_admission_v2(request, expected_version=0)
            connection = cast(TransactionFaultConnection, store._connection)
            connection.arm("ROLLBACK", kind="ack_loss")

            def fail_after_lease(_lease_token: str) -> str:
                raise RuntimeError("private body failure before rollback acknowledgement")

            with (
                mock.patch.object(store, "_now", return_value=CLAIMED_AT),
                mock.patch.object(store, "_lease_token_digest", new=fail_after_lease),
                mock.patch(
                    "quantum_entanglement.store.secrets.token_urlsafe",
                    return_value=raw_token,
                ),
            ):
                with self.assertRaises(InvocationStartCommitAmbiguityError) as caught:
                    self.claim(store, request)

            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            traceback_locals = event_store_traceback_locals(caught.exception)
            self.assertNotIn(raw_token, traceback_locals)
            self.assertNotIn("private transaction acknowledgement detail", traceback_locals)
            with self.assertRaises(EventStorePoisonedError):
                store.stream_version(request.stream_id)

        with SQLiteEventStore(path, clock=lambda: STORE_TIME) as reopened:
            self.assert_unstarted(reopened, request, raw_token=raw_token)

    def test_begin_controls_are_clean_and_confirmed_rollback_is_not_ambiguous(self) -> None:
        cases: tuple[tuple[str, type[BaseException], object, str], ...] = (
            ("keyboard", KeyboardInterrupt, None, ""),
            ("system-exit", SystemExit, "private-begin-system-exit", "1"),
            ("generator-exit", GeneratorExit, None, ""),
            ("cancelled", CancelledError, None, ""),
        )
        request = scoped_request()
        for signal_name, signal_type, exit_code, public_text in cases:
            with self.subTest(signal=signal_name):
                path = self.path(f"begin-control-{signal_name}")
                with SQLiteEventStore(path, clock=lambda: STORE_TIME) as seed_store:
                    seed_store.append_scoped_task_invocation_admission_v2(
                        request,
                        expected_version=0,
                    )
                with open_fault_store(path) as store:
                    connection = cast(TransactionFaultConnection, store._connection)
                    connection.arm(
                        "BEGIN IMMEDIATE",
                        kind="control",
                        signal_name=signal_name,
                        signal_detail=f"private-begin-{signal_name}",
                        system_exit_code=exit_code,
                    )
                    with (
                        mock.patch.object(
                            store,
                            "_now",
                            side_effect=AssertionError("clock must not run"),
                        ),
                        mock.patch.object(
                            store_module,
                            "new_id",
                            side_effect=AssertionError("identifier provider must not run"),
                        ),
                        mock.patch(
                            "quantum_entanglement.store.secrets.token_urlsafe",
                            side_effect=AssertionError("lease provider must not run"),
                        ),
                    ):
                        with self.assertRaises(signal_type) as caught:
                            self.claim(store, request)

                    self.assertIsNot(caught.exception, connection.raised_signal)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertEqual(str(caught.exception), public_text)
                    self.assertNotIn(
                        "private-begin",
                        event_store_traceback_locals(caught.exception),
                    )
                    self.assertFalse(connection.in_transaction)
                    self.assert_unstarted(store, request)

    def test_commit_controls_are_clean_ambiguous_and_reopen_receipt_only(self) -> None:
        cases: tuple[tuple[str, type[BaseException], object, str], ...] = (
            ("keyboard", KeyboardInterrupt, None, ""),
            ("system-exit", SystemExit, 23, "23"),
            ("generator-exit", GeneratorExit, None, ""),
            ("cancelled", CancelledError, None, ""),
        )
        request = scoped_request()
        for signal_name, signal_type, exit_code, public_text in cases:
            with self.subTest(signal=signal_name):
                raw_token = f"scoped-commit-control-token-{signal_name}"
                path = self.path(f"commit-control-{signal_name}")
                with open_fault_store(path) as store:
                    store.append_scoped_task_invocation_admission_v2(
                        request,
                        expected_version=0,
                    )
                    connection = cast(TransactionFaultConnection, store._connection)
                    connection.arm(
                        "COMMIT",
                        kind="control",
                        signal_name=signal_name,
                        signal_detail=f"private-commit-{signal_name}",
                        system_exit_code=exit_code,
                    )
                    with (
                        mock.patch.object(store, "_now", return_value=CLAIMED_AT),
                        mock.patch(
                            "quantum_entanglement.store.secrets.token_urlsafe",
                            return_value=raw_token,
                        ),
                    ):
                        with self.assertRaises(signal_type) as caught:
                            self.claim(store, request)

                    self.assertIsNot(caught.exception, connection.raised_signal)
                    self.assertIsNone(caught.exception.__context__)
                    cause = caught.exception.__cause__
                    self.assertIs(type(cause), InvocationStartCommitAmbiguityError)
                    self.assertIsNone(cause.__traceback__)
                    self.assertEqual(str(caught.exception), public_text)
                    traceback_locals = event_store_traceback_locals(caught.exception)
                    self.assertNotIn(raw_token, traceback_locals)
                    self.assertNotIn("private-commit", traceback_locals)
                    with self.assertRaises(EventStorePoisonedError):
                        store.stream_version(request.stream_id)

                with SQLiteEventStore(path, clock=lambda: STORE_TIME) as reopened:
                    self.assert_started_receipt_only(
                        reopened,
                        request,
                        raw_token=raw_token,
                    )

    def test_rollback_controls_are_clean_ambiguous_and_reopen_unstarted(self) -> None:
        cases: tuple[tuple[str, type[BaseException], object, str], ...] = (
            ("keyboard", KeyboardInterrupt, None, ""),
            ("system-exit", SystemExit, 31, "31"),
            ("generator-exit", GeneratorExit, None, ""),
            ("cancelled", CancelledError, None, ""),
        )
        request = scoped_request()
        for signal_name, signal_type, exit_code, public_text in cases:
            with self.subTest(signal=signal_name):
                raw_token = f"scoped-rollback-control-token-{signal_name}"
                path = self.path(f"rollback-control-{signal_name}")
                with open_fault_store(path) as store:
                    store.append_scoped_task_invocation_admission_v2(
                        request,
                        expected_version=0,
                    )
                    connection = cast(TransactionFaultConnection, store._connection)
                    connection.arm(
                        "ROLLBACK",
                        kind="control",
                        signal_name=signal_name,
                        signal_detail=f"private-rollback-{signal_name}",
                        system_exit_code=exit_code,
                    )

                    def fail_after_lease(_lease_token: str) -> str:
                        raise RuntimeError("private body failure before controlled rollback")

                    with (
                        mock.patch.object(store, "_now", return_value=CLAIMED_AT),
                        mock.patch.object(
                            store,
                            "_lease_token_digest",
                            new=fail_after_lease,
                        ),
                        mock.patch(
                            "quantum_entanglement.store.secrets.token_urlsafe",
                            return_value=raw_token,
                        ),
                    ):
                        with self.assertRaises(signal_type) as caught:
                            self.claim(store, request)

                    self.assertIsNot(caught.exception, connection.raised_signal)
                    self.assertIsNone(caught.exception.__context__)
                    cause = caught.exception.__cause__
                    self.assertIs(type(cause), InvocationStartCommitAmbiguityError)
                    self.assertIsNone(cause.__traceback__)
                    self.assertEqual(str(caught.exception), public_text)
                    traceback_locals = event_store_traceback_locals(caught.exception)
                    self.assertNotIn(raw_token, traceback_locals)
                    self.assertNotIn("private-rollback", traceback_locals)
                    with self.assertRaises(EventStorePoisonedError):
                        store.stream_version(request.stream_id)

                with SQLiteEventStore(path, clock=lambda: STORE_TIME) as reopened:
                    self.assert_unstarted(reopened, request, raw_token=raw_token)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
