from __future__ import annotations

import copy
import os
import pickle
import select
import signal
import sqlite3
import tempfile
import threading
import time
import unittest
import warnings
from asyncio import CancelledError
from collections.abc import Callable
from pathlib import Path
from types import FunctionType, GeneratorType, MethodType
from unittest import mock

import quantum_entanglement.store as store_module
from quantum_entanglement import EventStoreLifecycleError
from quantum_entanglement.delivery import OutboxMessage, OutboxStatus
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import SQLiteEventStore


def _wait_for_child(pid: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return status
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    raise AssertionError(f"fork child exceeded {timeout:.1f}s (status={status})")


def _run_fork_child(callback: Callable[[], bytes], timeout: float = 3.0) -> bytes:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            payload = callback()
            if type(payload) is not bytes:
                payload = b"invalid-payload"
        except BaseException:
            payload = b"child-error"
        try:
            os.write(write_fd, payload[:4096])
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        ready, _, _ = select.select((read_fd,), (), (), timeout)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise AssertionError(f"fork child produced no result within {timeout:.1f}s")
        payload = os.read(read_fd, 4096)
        status = _wait_for_child(pid, timeout)
    finally:
        os.close(read_fd)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise AssertionError(f"fork child failed with status {status}")
    return payload


def _lifecycle_outcome(
    action: Callable[[], object],
    forbidden: tuple[object, ...],
) -> bytes:
    try:
        action()
    except EventStoreLifecycleError as error:
        return _lifecycle_error_outcome(error, forbidden)
    except BaseException:
        return b"wrong-error"
    return b"accepted"


def _lifecycle_error_outcome(
    error: EventStoreLifecycleError,
    forbidden: tuple[object, ...],
) -> bytes:
    if (
        type(error) is not EventStoreLifecycleError
        or error.args != ("event_store_process_mismatch",)
        or error.code != "event_store_process_mismatch"
        or error.__cause__ is not None
        or error.__context__ is not None
        or getattr(error, "__notes__", None) is not None
    ):
        return b"unsafe-error"
    traceback_cursor = error.__traceback__
    while traceback_cursor is not None:
        frame = traceback_cursor.tb_frame
        if frame.f_globals.get("__name__") == "quantum_entanglement.store":
            local_values = tuple(frame.f_locals.values())
            if any(_object_reaches_forbidden(value, forbidden) for value in local_values):
                return b"leaked-local"
        traceback_cursor = traceback_cursor.tb_next
    return b"rejected"


def _object_reaches_forbidden(
    value: object,
    forbidden: tuple[object, ...],
    *,
    seen: set[int] | None = None,
    depth: int = 0,
) -> bool:
    if any(value is item for item in forbidden):
        return True
    if depth >= 8:
        return False
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if type(value) in (tuple, list, set, frozenset):
        return any(
            _object_reaches_forbidden(item, forbidden, seen=seen, depth=depth + 1) for item in value
        )
    if type(value) is dict:
        return any(
            _object_reaches_forbidden(item, forbidden, seen=seen, depth=depth + 1)
            for pair in value.items()
            for item in pair
        )
    if type(value) is FunctionType:
        closure = value.__closure__ or ()
        for cell in closure:
            try:
                item = cell.cell_contents
            except ValueError:
                continue
            if _object_reaches_forbidden(item, forbidden, seen=seen, depth=depth + 1):
                return True
        return False
    if type(value) is MethodType:
        return _object_reaches_forbidden(
            value.__self__,
            forbidden,
            seen=seen,
            depth=depth + 1,
        )
    if type(value) is GeneratorType:
        frame = value.gi_frame
        return frame is not None and any(
            _object_reaches_forbidden(item, forbidden, seen=seen, depth=depth + 1)
            for item in tuple(frame.f_locals.values())
        )
    return False


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
class SQLiteEventStoreProcessEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: "2026-08-21T00:00:00Z")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_all_ordinary_and_lifecycle_entry_points_reject_inherited_store(self) -> None:
        event = DomainEvent(
            "stream:owner",
            "owner.created",
            {},
            "actor:test",
            idempotency_key="owner-created",
        )
        actions: tuple[tuple[str, Callable[[], object]], ...] = (
            ("stream_version", lambda: self.store.stream_version("stream:owner")),
            (
                "get_idempotent_event",
                lambda: self.store.get_idempotent_event("stream:owner", "owner-created"),
            ),
            ("append", lambda: self.store.append(event)),
            ("append_with_outbox", lambda: self.store.append_with_outbox(event, ())),
            (
                "append_inbox",
                lambda: self.store.append_inbox("consumer", "message", event),
            ),
            ("append_many", lambda: self.store.append_many("stream:owner", (event,))),
            ("read_stream", lambda: self.store.read_stream("stream:owner")),
            ("read_stream_page", lambda: self.store.read_stream_page("stream:owner")),
            ("read_all", lambda: self.store.read_all()),
            ("stream_all_page_call", lambda: self.store.stream_all_page()),
            ("claim_outbox", lambda: self.store.claim_outbox("worker")),
            ("acknowledge_outbox", lambda: self.store.acknowledge_outbox("msg", "lease")),
            ("reject_outbox", lambda: self.store.reject_outbox("msg", "lease", "error")),
            (
                "mark_outbox_ambiguous",
                lambda: self.store.mark_outbox_ambiguous("msg", "lease", "ack_failed"),
            ),
            ("read_outbox_ambiguities", lambda: self.store.read_outbox_ambiguities()),
            (
                "read_outbox_ambiguities_page",
                lambda: self.store.read_outbox_ambiguities_page(),
            ),
            (
                "resolve_outbox_ambiguity",
                lambda: self.store.resolve_outbox_ambiguity("msg", "0" * 64, "dead_letter"),
            ),
            ("get_outbox", lambda: self.store.get_outbox("msg")),
            ("read_outbox", lambda: self.store.read_outbox(OutboxStatus.PENDING)),
            ("read_outbox_page", lambda: self.store.read_outbox_page()),
            (
                "get_inbox_receipt",
                lambda: self.store.get_inbox_receipt("consumer", "message"),
            ),
            (
                "save_snapshot",
                lambda: self.store.save_snapshot("stream:owner", 1, {}, "2026-08-21T00:00:00Z"),
            ),
            ("load_snapshot", lambda: self.store.load_snapshot("stream:owner")),
            ("close", self.store.close),
            ("enter", self.store.__enter__),
            ("exit", lambda: self.store.__exit__(None, None, None)),
        )

        for name, action in actions:
            with self.subTest(entry=name):
                outcome = _run_fork_child(
                    lambda action=action: _lifecycle_outcome(
                        action,
                        (self.store, event),
                    )
                )
                self.assertEqual(outcome, b"rejected")

        self.assertEqual(self.store.stream_version("stream:owner"), 0)
        self.store.append(event)
        self.assertEqual(self.store.stream_version("stream:owner"), 1)

    def test_clock_fork_after_begin_rejects_child_without_rollback_or_lock_release(self) -> None:
        event = DomainEvent(
            "stream:clock-fork",
            "clock.created",
            {},
            "actor:test",
            idempotency_key="clock-created",
        )
        message = OutboxMessage(
            "local:test",
            {},
            message_id="message:clock-fork",
            idempotency_key="message:clock-fork",
            available_at="2026-08-21T00:00:00Z",
            created_at="2026-08-21T00:00:00Z",
        )
        self.store.append_with_outbox(event, (message,))
        parent_pid = os.getpid()
        child_pids: list[int] = []
        read_fd, write_fd = os.pipe()

        def forking_clock() -> str:
            if child_pids:
                return "2026-08-21T00:00:00Z"
            pid = os.fork()
            if pid == 0:
                return "2026-08-21T00:00:01Z"
            child_pids.append(pid)
            return "2026-08-21T00:00:01Z"

        self.store._clock = forking_clock
        try:
            try:
                claimed = self.store.claim_outbox("worker:parent", limit=1)
            except EventStoreLifecycleError as error:
                if os.getpid() == parent_pid:
                    raise
                outcome = _lifecycle_error_outcome(error, (self.store,))
                os.write(write_fd, outcome)
                os.close(write_fd)
                os._exit(0)

            if os.getpid() != parent_pid:
                os.write(write_fd, b"accepted")
                os.close(write_fd)
                os._exit(0)

            self.assertEqual(len(claimed), 1)
            self.assertEqual(len(child_pids), 1)
            os.close(write_fd)
            ready, _, _ = select.select((read_fd,), (), (), 3.0)
            self.assertTrue(ready, "clock-fork child produced no lifecycle result")
            self.assertEqual(os.read(read_fd, 4096), b"rejected")
            status = _wait_for_child(child_pids[0], 3.0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
        finally:
            if os.getpid() == parent_pid:
                try:
                    os.close(read_fd)
                except OSError:
                    pass
                self.store._clock = lambda: "2026-08-21T00:00:02Z"

        persisted = self.store.get_outbox("message:clock-fork")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.attempt_count, 1)
        self.assertEqual(persisted.status, OutboxStatus.IN_FLIGHT)
        self.assertFalse(self.store._connection.in_transaction)

    def test_constructor_migration_clock_fork_quarantines_without_child_close(self) -> None:
        parent_pid = os.getpid()
        child_pids: list[int] = []
        read_fd, write_fd = os.pipe()
        real_connect = sqlite3.connect
        constructor_path = str(Path(self.tempdir.name) / "constructor-fork.sqlite3")

        class CloseTrackingConnection(sqlite3.Connection):
            def close(connection_self) -> None:
                if os.getpid() != parent_pid:
                    os.write(write_fd, b"closed:")
                super().close()

        def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=CloseTrackingConnection)

        def forking_clock() -> str:
            if child_pids:
                return "2026-08-21T00:00:00Z"
            pid = os.fork()
            if pid == 0:
                return "2026-08-21T00:00:00Z"
            child_pids.append(pid)
            return "2026-08-21T00:00:00Z"

        try:
            try:
                with mock.patch.object(store_module.sqlite3, "connect", tracking_connect):
                    constructed = SQLiteEventStore(constructor_path, clock=forking_clock)
            except EventStoreLifecycleError as error:
                if os.getpid() == parent_pid:
                    raise
                outcome = _lifecycle_error_outcome(error, ())
                os.write(write_fd, outcome)
                os.close(write_fd)
                os._exit(0)

            if os.getpid() != parent_pid:
                os.write(write_fd, b"accepted")
                os.close(write_fd)
                os._exit(0)

            self.assertEqual(len(child_pids), 1)
            os.close(write_fd)
            ready, _, _ = select.select((read_fd,), (), (), 3.0)
            self.assertTrue(ready, "constructor-fork child produced no lifecycle result")
            self.assertEqual(os.read(read_fd, 4096), b"rejected")
            status = _wait_for_child(child_pids[0], 3.0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
            self.assertEqual(constructed.stream_version("stream:constructor"), 0)
            constructed.close()
        finally:
            if os.getpid() == parent_pid:
                try:
                    os.close(read_fd)
                except OSError:
                    pass

    def test_inherited_entry_rejects_before_caller_input_is_touched(self) -> None:
        class HostileInput:
            def __getattribute__(self, _name: str) -> object:
                raise AssertionError("inherited entry touched caller input")

            def __bool__(self) -> bool:
                raise AssertionError("inherited entry tested caller truthiness")

            def __iter__(self) -> object:
                raise AssertionError("inherited entry materialized caller iterable")

        hostile = HostileInput()

        def action() -> object:
            return self.store.append_with_outbox(hostile, hostile)  # type: ignore[arg-type]

        self.assertEqual(
            _run_fork_child(lambda: _lifecycle_outcome(action, (self.store, hostile))),
            b"rejected",
        )
        self.assertEqual(self.store.stream_version("stream:owner"), 0)

    def test_read_parameters_reject_hostile_adapters_before_connection(self) -> None:
        adapter_calls = 0

        class HostileText(str):
            def __conform__(self, _protocol: object) -> str:
                nonlocal adapter_calls
                adapter_calls += 1
                return str(self)

        class HostileInteger(int):
            def __conform__(self, _protocol: object) -> int:
                nonlocal adapter_calls
                adapter_calls += 1
                return int(self)

        hostile_text = HostileText("caller-controlled")
        hostile_integer = HostileInteger(0)
        actions: tuple[Callable[[], object], ...] = (
            lambda: self.store.stream_version(hostile_text),
            lambda: self.store.get_idempotent_event(hostile_text, "key"),
            lambda: self.store.get_idempotent_event("stream", hostile_text),
            lambda: self.store.read_stream(hostile_text),
            lambda: self.store.read_stream("stream", hostile_integer),
            lambda: self.store.read_stream_page(hostile_text),
            lambda: self.store.get_outbox(hostile_text),
            lambda: self.store.get_inbox_receipt(hostile_text, "message"),
            lambda: self.store.get_inbox_receipt("consumer", hostile_text),
            lambda: self.store.load_snapshot(hostile_text),
        )

        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(TypeError):
                    action()
        self.assertEqual(adapter_calls, 0)
        self.assertEqual(self.store.stream_version("stream:owner"), 0)

    def test_event_batch_iterable_fork_rejects_child_before_begin(self) -> None:
        parent_pid = os.getpid()
        child_pids: list[int] = []
        read_fd, write_fd = os.pipe()
        event = DomainEvent(
            "stream:iterable-fork",
            "iterable.created",
            {},
            "actor:test",
            event_id="event:iterable-fork",
            timestamp="2026-08-21T00:00:00Z",
        )

        class ForkingIterable:
            def __iter__(iterable_self):
                pid = os.fork()
                if pid != 0:
                    child_pids.append(pid)
                yield event

        try:
            try:
                stored = self.store.append_many(
                    "stream:iterable-fork",
                    ForkingIterable(),
                    expected_version=0,
                )
            except EventStoreLifecycleError as error:
                if os.getpid() == parent_pid:
                    raise
                os.write(write_fd, _lifecycle_error_outcome(error, (self.store, event)))
                os.close(write_fd)
                os._exit(0)

            if os.getpid() != parent_pid:
                os.write(write_fd, b"accepted")
                os.close(write_fd)
                os._exit(0)

            self.assertEqual(len(stored), 1)
            self.assertEqual(len(child_pids), 1)
            os.close(write_fd)
            ready, _, _ = select.select((read_fd,), (), (), 3.0)
            self.assertTrue(ready, "iterable-fork child produced no lifecycle result")
            self.assertEqual(os.read(read_fd, 4096), b"rejected")
            status = _wait_for_child(child_pids[0], 3.0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
        finally:
            if os.getpid() == parent_pid:
                try:
                    os.close(read_fd)
                except OSError:
                    pass

        self.assertEqual(self.store.stream_version("stream:iterable-fork"), 1)
        self.assertFalse(self.store._connection.in_transaction)

    def test_stream_enter_resume_iter_and_exit_each_reject_inherited_state(self) -> None:
        events = tuple(
            DomainEvent(
                "stream:deferred",
                "deferred.created",
                {"index": index},
                "actor:test",
                event_id=f"event:deferred:{index}",
                timestamp="2026-08-21T00:00:00Z",
            )
            for index in range(3)
        )
        self.store.append_many("stream:deferred", events, expected_version=0)
        context = self.store.stream_all_page(limit=3)

        self.assertEqual(
            _run_fork_child(
                lambda: _lifecycle_outcome(
                    context.__enter__,
                    (self.store, context),
                )
            ),
            b"rejected",
        )

        iterator = context.__enter__()
        first = next(iterator)
        self.assertEqual(first.global_position, 1)
        self.assertEqual(
            _run_fork_child(
                lambda: _lifecycle_outcome(
                    lambda: iter(iterator),
                    (self.store, context, iterator),
                )
            ),
            b"rejected",
        )
        self.assertEqual(
            _run_fork_child(
                lambda: _lifecycle_outcome(
                    lambda: next(iterator),
                    (self.store, context, iterator),
                )
            ),
            b"rejected",
        )
        second = next(iterator)
        self.assertEqual(second.global_position, 2)

        def exit_during_active_exception() -> object:
            try:
                raise RuntimeError("caller sentinel")
            except RuntimeError as sentinel:
                return context.__exit__(RuntimeError, sentinel, sentinel.__traceback__)

        self.assertEqual(
            _run_fork_child(
                lambda: _lifecycle_outcome(
                    exit_during_active_exception,
                    (self.store, context, iterator),
                )
            ),
            b"rejected",
        )
        third = next(iterator)
        self.assertEqual(third.global_position, 3)
        with self.assertRaises(StopIteration):
            next(iterator)
        context.__exit__(None, None, None)

    def test_live_store_context_and_iterator_cannot_be_copied_or_serialized(self) -> None:
        context = self.store.stream_all_page()
        iterator = context.__enter__()
        try:
            for value in (self.store, context, iterator):
                with self.subTest(value=type(value).__name__):
                    with self.assertRaisesRegex(TypeError, "cannot be copied"):
                        copy.copy(value)
                    with self.assertRaisesRegex(TypeError, "cannot be copied"):
                        copy.deepcopy(value)
                    with self.assertRaisesRegex(TypeError, "cannot be serialized"):
                        pickle.dumps(value)
        finally:
            context.__exit__(None, None, None)

    def test_originating_exact_control_wins_over_transaction_cleanup_control(self) -> None:
        delegate = self.store._connection

        class CleanupInterruptingConnection:
            def __init__(connection_self, *, commit_error: BaseException | None = None) -> None:
                connection_self.commit_error = commit_error

            @property
            def in_transaction(connection_self) -> bool:
                return delegate.in_transaction

            def execute(
                connection_self,
                statement: str,
                parameters: tuple[object, ...] = (),
            ) -> object:
                if statement == "COMMIT" and connection_self.commit_error is not None:
                    raise connection_self.commit_error
                if statement == "ROLLBACK":
                    raise KeyboardInterrupt("cleanup control")
                return delegate.execute(statement, parameters)

        controls: tuple[BaseException, ...] = (
            KeyboardInterrupt("originating keyboard interrupt"),
            SystemExit(23),
            GeneratorExit("originating generator exit"),
            CancelledError("originating cancellation"),
        )
        for originating in controls:
            with self.subTest(control=type(originating).__name__):
                self.store._connection = CleanupInterruptingConnection()  # type: ignore[assignment]
                try:
                    with self.assertRaises(type(originating)) as caught:
                        with self.store._transaction():
                            raise originating
                    self.assertIs(caught.exception, originating)
                finally:
                    self.store._connection = delegate
                    if delegate.in_transaction:
                        delegate.execute("ROLLBACK")

        commit_origin = GeneratorExit("originating commit control")
        self.store._connection = CleanupInterruptingConnection(  # type: ignore[assignment]
            commit_error=commit_origin
        )
        try:
            with self.assertRaises(GeneratorExit) as caught_commit:
                with self.store._transaction():
                    pass
            self.assertIs(caught_commit.exception, commit_origin)
        finally:
            self.store._connection = delegate
            if delegate.in_transaction:
                delegate.execute("ROLLBACK")

    def test_non_process_provider_exception_is_not_rewritten_or_detached(self) -> None:
        class ProviderError(Exception):
            pass

        provider_error = ProviderError("provider sentinel")

        class FailingIterable:
            def __iter__(self):
                raise provider_error

        with self.assertRaises(ProviderError) as caught:
            self.store.append_many("stream:provider", FailingIterable())
        self.assertIs(caught.exception, provider_error)
        self.assertIsNone(provider_error.__cause__)
        self.assertIsNone(provider_error.__context__)
        self.assertEqual(self.store.stream_version("stream:provider"), 0)

    def test_public_mismatch_detaches_active_outer_exception_context(self) -> None:
        caller_sentinel = RuntimeError("caller sentinel")

        def active_exception_action() -> object:
            try:
                raise caller_sentinel
            except RuntimeError:
                return self.store.stream_version("stream:active-context")

        self.assertEqual(
            _run_fork_child(
                lambda: _lifecycle_outcome(
                    active_exception_action,
                    (self.store, caller_sentinel),
                )
            ),
            b"rejected",
        )
        self.assertIsNone(caller_sentinel.__cause__)
        self.assertIsNone(caller_sentinel.__context__)

    def test_child_does_not_wait_for_parent_thread_transaction_or_lock(self) -> None:
        transaction_open = threading.Event()
        release_transaction = threading.Event()

        def hold_transaction() -> None:
            with self.store._transaction():
                transaction_open.set()
                release_transaction.wait(5.0)

        holder = threading.Thread(target=hold_transaction)
        holder.start()
        self.assertTrue(transaction_open.wait(1.0))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                outcome = _run_fork_child(
                    lambda: _lifecycle_outcome(
                        self.store.close,
                        (self.store,),
                    ),
                    timeout=2.0,
                )
            self.assertEqual(outcome, b"rejected")
        finally:
            release_transaction.set()
            holder.join(2.0)

        self.assertFalse(holder.is_alive())
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(self.store.stream_version("stream:parent-continuity"), 0)

    def test_write_snapshots_reject_hostile_sqlite_adapters_before_begin(self) -> None:
        adapter_calls = 0

        class HostileText(str):
            def __conform__(self, _protocol: object) -> str:
                nonlocal adapter_calls
                adapter_calls += 1
                return str(self)

        class HostileInteger(int):
            def __conform__(self, _protocol: object) -> int:
                nonlocal adapter_calls
                adapter_calls += 1
                return int(self)

        hostile = HostileText("caller-controlled")
        event = DomainEvent(
            "stream:hostile",
            "hostile.created",
            {},
            "actor:test",
            event_id="event:hostile",
            timestamp="2026-08-21T00:00:00Z",
        )
        object.__setattr__(event, "event_id", hostile)

        with self.assertRaises(TypeError):
            self.store.append(event)

        safe_event = DomainEvent(
            "stream:safe",
            "safe.created",
            {},
            "actor:test",
            event_id="event:safe",
            timestamp="2026-08-21T00:00:00Z",
        )
        message = OutboxMessage(
            "local:test",
            {},
            message_id="message:safe",
            idempotency_key="message:safe",
            available_at="2026-08-21T00:00:00Z",
            created_at="2026-08-21T00:00:00Z",
        )
        object.__setattr__(message, "destination", hostile)
        hostile_integer = HostileInteger(1)
        actions: tuple[Callable[[], object], ...] = (
            lambda: self.store.append_with_outbox(safe_event, (message,)),
            lambda: self.store.append_inbox(hostile, "message", safe_event),
            lambda: self.store.append_many(hostile, (safe_event,)),
            lambda: self.store.save_snapshot(hostile, 1, {}, "2026-08-21T00:00:00Z"),
            lambda: self.store.save_snapshot("stream", hostile_integer, {}, hostile),
            lambda: self.store.claim_outbox(hostile),
            lambda: self.store.claim_outbox("worker", limit=hostile_integer),
            lambda: self.store.acknowledge_outbox(hostile, "lease"),
            lambda: self.store.acknowledge_outbox("message", hostile),
            lambda: self.store.reject_outbox("message", "lease", hostile),
            lambda: self.store.mark_outbox_ambiguous("message", hostile, "ack_failed"),
            lambda: self.store.resolve_outbox_ambiguity(hostile, "0" * 64, "retry"),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(TypeError):
                    action()

        self.assertEqual(adapter_calls, 0)
        self.assertEqual(self.store.stream_version("stream:hostile"), 0)
        self.assertFalse(self.store._connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
