from __future__ import annotations

import copy
import gc
import multiprocessing
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
from quantum_entanglement.migrations import validate_sqlite_schema
from quantum_entanglement.store import ConcurrencyError, SQLiteEventStore


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


def _fresh_event_store_worker(path: str, mode: str, connection: object) -> None:
    channel = connection
    store: SQLiteEventStore | None = None
    try:
        store = SQLiteEventStore(path, clock=lambda: "2026-08-21T00:00:10Z")
        channel.send(("ready",))  # type: ignore[attr-defined]
        if channel.recv() != ("go",):  # type: ignore[attr-defined]
            raise RuntimeError("fresh process start protocol failed")
        if mode == "global_cas":
            event = DomainEvent(
                "stream:child-cas",
                "cas.created",
                {},
                "actor:child",
                event_id="event:child-cas",
                timestamp="2026-08-21T00:00:00Z",
            )
            try:
                stored = store.append(event, expected_version=0, expected_global_position=1)
            except ConcurrencyError:
                result: object = ("conflict",)
            else:
                result = ("stored", stored.global_position)
        elif mode == "idempotent_outbox":
            event = DomainEvent(
                "stream:idempotent-race",
                "race.created",
                {"value": "same"},
                "actor:shared",
                event_id="event:idempotent-race",
                timestamp="2026-08-21T00:00:00Z",
                idempotency_key="admission:idempotent-race",
            )
            message = OutboxMessage(
                "local:test",
                {"value": "same"},
                message_id="message:idempotent-race",
                idempotency_key="message:idempotent-race",
                available_at="2026-08-21T00:00:00Z",
                created_at="2026-08-21T00:00:00Z",
            )
            stored, messages = store.append_with_outbox(event, (message,), expected_version=0)
            result = ("stored", stored.global_position, len(messages))
        elif mode == "claim":
            claimed = store.claim_outbox("worker:child", limit=1)
            result = ("claimed", len(claimed))
        elif mode == "ambiguity":
            ambiguity = store.read_outbox_ambiguities()[0]
            resolved = store.resolve_outbox_ambiguity(
                ambiguity.message_id,
                ambiguity.lease_token_digest,
                "dead_letter",
                resolved_at="2026-08-21T00:00:20Z",
                retry_at="2026-08-21T00:00:20Z",
            )
            result = ("resolved", resolved)
        else:
            raise ValueError("unsupported fresh-process mode")
        channel.send(("result", result))  # type: ignore[attr-defined]
    except BaseException as error:
        try:
            channel.send(("error", type(error).__name__))  # type: ignore[attr-defined]
        except BaseException:
            pass
    finally:
        if store is not None:
            store.close()
        channel.close()  # type: ignore[attr-defined]


def _fork_before_connection_probe(path: str, mode: str, connection: object) -> None:
    """Run one real fork before either side creates any SQLite wrapper."""

    channel = connection
    context = multiprocessing.get_context("fork")
    parent_channel, child_channel = context.Pipe()
    child = context.Process(
        target=_fresh_event_store_worker,
        args=(path, mode, child_channel),
    )
    parent_store: SQLiteEventStore | None = None
    try:
        child.start()
        child_channel.close()
        if not parent_channel.poll(15.0) or parent_channel.recv() != ("ready",):
            raise RuntimeError("fork child did not become ready")
        parent_store = SQLiteEventStore(path, clock=lambda: "2026-08-21T00:00:10Z")

        if mode == "global_cas":
            parent_store.append(
                DomainEvent(
                    "stream:seed",
                    "seed.created",
                    {},
                    "actor:parent",
                    event_id="event:seed",
                    timestamp="2026-08-21T00:00:00Z",
                ),
                expected_version=0,
            )
        elif mode == "claim":
            parent_store.append_with_outbox(
                DomainEvent(
                    "stream:lease-race",
                    "lease.created",
                    {},
                    "actor:parent",
                    event_id="event:lease-race",
                    timestamp="2026-08-21T00:00:00Z",
                ),
                (
                    OutboxMessage(
                        "local:test",
                        {},
                        message_id="message:lease-race",
                        idempotency_key="message:lease-race",
                        available_at="2026-08-21T00:00:00Z",
                        created_at="2026-08-21T00:00:00Z",
                    ),
                ),
                expected_version=0,
            )
        elif mode == "ambiguity":
            parent_store.append_with_outbox(
                DomainEvent(
                    "stream:ambiguity-race",
                    "ambiguity.created",
                    {},
                    "actor:parent",
                    event_id="event:ambiguity-race",
                    timestamp="2026-08-21T00:00:00Z",
                ),
                (
                    OutboxMessage(
                        "local:test",
                        {},
                        message_id="message:ambiguity-race",
                        idempotency_key="message:ambiguity-race",
                        available_at="2026-08-21T00:00:00Z",
                        created_at="2026-08-21T00:00:00Z",
                    ),
                ),
                expected_version=0,
            )
            claimed = parent_store.claim_outbox("worker:setup", limit=1)[0]
            if claimed.lease_token is None:
                raise RuntimeError("setup lease token is missing")
            parent_store.mark_outbox_ambiguous(
                claimed.message.message_id,
                claimed.lease_token,
                "ack_failed",
                marked_at="2026-08-21T00:00:11Z",
            )

        parent_channel.send(("go",))
        if mode == "global_cas":
            try:
                stored = parent_store.append(
                    DomainEvent(
                        "stream:parent-cas",
                        "cas.created",
                        {},
                        "actor:parent",
                        event_id="event:parent-cas",
                        timestamp="2026-08-21T00:00:00Z",
                    ),
                    expected_version=0,
                    expected_global_position=1,
                )
            except ConcurrencyError:
                parent_result: object = ("conflict",)
            else:
                parent_result = ("stored", stored.global_position)
        elif mode == "idempotent_outbox":
            stored, messages = parent_store.append_with_outbox(
                DomainEvent(
                    "stream:idempotent-race",
                    "race.created",
                    {"value": "same"},
                    "actor:shared",
                    event_id="event:idempotent-race",
                    timestamp="2026-08-21T00:00:00Z",
                    idempotency_key="admission:idempotent-race",
                ),
                (
                    OutboxMessage(
                        "local:test",
                        {"value": "same"},
                        message_id="message:idempotent-race",
                        idempotency_key="message:idempotent-race",
                        available_at="2026-08-21T00:00:00Z",
                        created_at="2026-08-21T00:00:00Z",
                    ),
                ),
                expected_version=0,
            )
            parent_result = ("stored", stored.global_position, len(messages))
        elif mode == "claim":
            parent_result = ("claimed", len(parent_store.claim_outbox("worker:parent", limit=1)))
        elif mode == "ambiguity":
            ambiguity = parent_store.read_outbox_ambiguities()[0]
            parent_result = (
                "resolved",
                parent_store.resolve_outbox_ambiguity(
                    ambiguity.message_id,
                    ambiguity.lease_token_digest,
                    "retry",
                    resolved_at="2026-08-21T00:00:20Z",
                    retry_at="2026-08-21T00:00:20Z",
                ),
            )
        else:
            raise ValueError("unsupported fork probe mode")

        if not parent_channel.poll(15.0):
            raise RuntimeError("fork child did not publish a result")
        message = parent_channel.recv()
        if message[0] != "result":
            raise RuntimeError("fork child returned an error")
        child_result = message[1]
        child.join(15.0)
        if child.is_alive() or child.exitcode != 0:
            raise RuntimeError("fork child did not exit cleanly")

        if mode == "global_cas":
            valid = {parent_result[0], child_result[0]} == {"stored", "conflict"}
            valid = valid and len(parent_store.read_all(limit=10)) == 2
        elif mode == "idempotent_outbox":
            valid = parent_result == child_result == ("stored", 1, 1)
            valid = valid and len(parent_store.read_all(limit=10)) == 1
            valid = valid and len(parent_store.read_outbox()) == 1
        elif mode == "claim":
            valid = sorted((parent_result[1], child_result[1])) == [0, 1]
            durable = parent_store.get_outbox("message:lease-race")
            valid = (
                valid
                and durable is not None
                and durable.status is OutboxStatus.IN_FLIGHT
                and durable.attempt_count == 1
            )
        else:
            valid = sorted((parent_result[1], child_result[1])) == [False, True]
            ambiguity = parent_store.read_outbox_ambiguities(open_only=False)[0]
            valid = valid and ambiguity.resolution in {"retry", "dead_letter"}
        valid = (
            valid
            and parent_store._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
        valid = (
            valid and not parent_store._connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        valid = valid and validate_sqlite_schema(parent_store._connection) == 3
        channel.send(("result", mode, valid))  # type: ignore[attr-defined]
    except BaseException as error:
        try:
            channel.send(("error", mode, type(error).__name__))  # type: ignore[attr-defined]
        except BaseException:
            pass
    finally:
        parent_channel.close()
        if child.is_alive():
            child.terminate()
        child.join(2.0)
        if parent_store is not None:
            parent_store.close()
        channel.close()  # type: ignore[attr-defined]


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

    def _run_fresh_process_race(
        self,
        start_method: str,
        path: str,
        mode: str,
        parent_action: Callable[[], object],
    ) -> tuple[object, object]:
        context = multiprocessing.get_context(start_method)
        parent_channel, child_channel = context.Pipe()
        process = context.Process(
            target=_fresh_event_store_worker,
            args=(path, mode, child_channel),
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            process.start()
        child_channel.close()
        try:
            self.assertTrue(parent_channel.poll(15.0), "fresh child did not become ready")
            self.assertEqual(parent_channel.recv(), ("ready",))
            parent_channel.send(("go",))
            parent_result = parent_action()
            self.assertTrue(parent_channel.poll(15.0), "fresh child did not publish a result")
            message = parent_channel.recv()
            self.assertEqual(message[0], "result", message)
            child_result = message[1]
            process.join(15.0)
            self.assertFalse(process.is_alive(), "fresh child did not exit")
            self.assertEqual(process.exitcode, 0)
            return parent_result, child_result
        finally:
            parent_channel.close()
            if process.is_alive():
                process.terminate()
                process.join(2.0)

    def _assert_fresh_store_integrity(self, store: SQLiteEventStore) -> None:
        self.assertEqual(store._connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(store._connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(validate_sqlite_schema(store._connection), 3)

    def _run_fork_before_init_probe(self, path: str, mode: str) -> None:
        context = multiprocessing.get_context("spawn")
        parent_channel, child_channel = context.Pipe()
        process = context.Process(
            target=_fork_before_connection_probe,
            args=(path, mode, child_channel),
        )
        process.start()
        child_channel.close()
        try:
            self.assertTrue(parent_channel.poll(30.0), "fork-before-init probe timed out")
            message = parent_channel.recv()
            self.assertEqual(message, ("result", mode, True), message)
            process.join(15.0)
            self.assertFalse(process.is_alive(), "fork-before-init supervisor did not exit")
            self.assertEqual(process.exitcode, 0)
        finally:
            parent_channel.close()
            if process.is_alive():
                process.terminate()
                process.join(2.0)

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

    def test_inherited_store_gc_quarantines_graph_before_connection_finalizer(self) -> None:
        parent_pid = os.getpid()
        real_connect = sqlite3.connect
        write_fd = -1
        inherited_path = str(Path(self.tempdir.name) / "inherited-finalizer.sqlite3")

        class FinalizerTrackingConnection(sqlite3.Connection):
            def __del__(connection_self) -> None:
                if os.getpid() != parent_pid:
                    try:
                        os.write(write_fd, b"connection-finalized:")
                    except OSError:
                        pass

        def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=FinalizerTrackingConnection)

        with mock.patch.object(store_module.sqlite3, "connect", tracking_connect):
            inherited = SQLiteEventStore(inherited_path)
        try:
            for mode in ("mismatch_then_gc", "gc_without_call"):
                with self.subTest(mode=mode):
                    read_fd, write_fd = os.pipe()
                    inherited_identity = id(inherited)
                    pid = os.fork()
                    if pid == 0:
                        os.close(read_fd)
                        if mode == "mismatch_then_gc":
                            for _ in range(2):
                                outcome = _lifecycle_outcome(
                                    lambda: inherited.stream_version("stream:gc"),
                                    (inherited,),
                                )
                                if outcome != b"rejected":
                                    os.write(write_fd, outcome + b":")
                        del inherited
                        gc.collect()
                        retained = sum(
                            id(root) == inherited_identity
                            for root in store_module._EVENT_STORE_CHILD_GRAPH_QUARANTINE
                        )
                        os.write(write_fd, f"alive:retained={retained}".encode("ascii"))
                        os.close(write_fd)
                        os._exit(0)

                    os.close(write_fd)
                    try:
                        ready, _, _ = select.select((read_fd,), (), (), 3.0)
                        self.assertTrue(ready, "inherited-store GC child produced no result")
                        outcome = os.read(read_fd, 4096)
                        status = _wait_for_child(pid, 3.0)
                    finally:
                        os.close(read_fd)
                    self.assertTrue(os.WIFEXITED(status), status)
                    self.assertEqual(os.WEXITSTATUS(status), 0)
                    self.assertEqual(outcome, b"alive:retained=1")
        finally:
            inherited.close()

    def test_transaction_context_enter_and_exit_reject_inherited_graph(self) -> None:
        unentered = self.store._transaction()
        self.assertEqual(
            _run_fork_child(
                lambda: _lifecycle_outcome(
                    unentered.__enter__,
                    (self.store, unentered),
                )
            ),
            b"rejected",
        )
        with unentered as connection:
            self.assertTrue(connection.in_transaction)
        self.assertFalse(self.store._connection.in_transaction)

        entered = self.store._transaction()
        connection = entered.__enter__()
        self.assertTrue(connection.in_transaction)
        try:
            self.assertEqual(
                _run_fork_child(
                    lambda: _lifecycle_outcome(
                        lambda: entered.__exit__(None, None, None),
                        (self.store, entered, connection),
                    )
                ),
                b"rejected",
            )
            self.assertTrue(connection.in_transaction)
        finally:
            entered.__exit__(None, None, None)
        self.assertFalse(self.store._connection.in_transaction)

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
        transaction = self.store._transaction()
        try:
            for value in (self.store, context, iterator, transaction):
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

    def test_fresh_fork_spawn_and_forkserver_connections_preserve_all_cas(self) -> None:
        if {"fork", "spawn"}.issubset(multiprocessing.get_all_start_methods()):
            for mode in ("global_cas", "idempotent_outbox", "claim", "ambiguity"):
                with self.subTest(start_method="fork-before-init", mode=mode):
                    path = str(Path(self.tempdir.name) / f"fresh-fork-{mode}.sqlite3")
                    self._run_fork_before_init_probe(path, mode)

        methods = tuple(
            method
            for method in ("spawn", "forkserver")
            if method in multiprocessing.get_all_start_methods()
        )
        self.assertTrue(methods)
        for start_method in methods:
            with self.subTest(start_method=start_method, mode="global_cas"):
                path = str(Path(self.tempdir.name) / f"fresh-{start_method}-global.sqlite3")
                with SQLiteEventStore(path, clock=lambda: "2026-08-21T00:00:10Z") as parent:
                    parent.append(
                        DomainEvent(
                            "stream:seed",
                            "seed.created",
                            {},
                            "actor:parent",
                            event_id="event:seed",
                            timestamp="2026-08-21T00:00:00Z",
                        ),
                        expected_version=0,
                    )

                    def parent_global_cas() -> object:
                        try:
                            stored = parent.append(
                                DomainEvent(
                                    "stream:parent-cas",
                                    "cas.created",
                                    {},
                                    "actor:parent",
                                    event_id="event:parent-cas",
                                    timestamp="2026-08-21T00:00:00Z",
                                ),
                                expected_version=0,
                                expected_global_position=1,
                            )
                        except ConcurrencyError:
                            return ("conflict",)
                        return ("stored", stored.global_position)

                    parent_result, child_result = self._run_fresh_process_race(
                        start_method,
                        path,
                        "global_cas",
                        parent_global_cas,
                    )
                    self.assertEqual(
                        {parent_result[0], child_result[0]},  # type: ignore[index]
                        {"stored", "conflict"},
                    )
                    self.assertEqual(len(parent.read_all(limit=10)), 2)
                    self._assert_fresh_store_integrity(parent)

            with self.subTest(start_method=start_method, mode="idempotent_outbox"):
                path = str(Path(self.tempdir.name) / f"fresh-{start_method}-idem.sqlite3")
                with SQLiteEventStore(path, clock=lambda: "2026-08-21T00:00:10Z") as parent:
                    shared_event = DomainEvent(
                        "stream:idempotent-race",
                        "race.created",
                        {"value": "same"},
                        "actor:shared",
                        event_id="event:idempotent-race",
                        timestamp="2026-08-21T00:00:00Z",
                        idempotency_key="admission:idempotent-race",
                    )
                    shared_message = OutboxMessage(
                        "local:test",
                        {"value": "same"},
                        message_id="message:idempotent-race",
                        idempotency_key="message:idempotent-race",
                        available_at="2026-08-21T00:00:00Z",
                        created_at="2026-08-21T00:00:00Z",
                    )

                    def parent_idempotent(
                        shared_event: DomainEvent = shared_event,
                        shared_message: OutboxMessage = shared_message,
                    ) -> object:
                        stored, messages = parent.append_with_outbox(
                            shared_event,
                            (shared_message,),
                            expected_version=0,
                        )
                        return ("stored", stored.global_position, len(messages))

                    parent_result, child_result = self._run_fresh_process_race(
                        start_method,
                        path,
                        "idempotent_outbox",
                        parent_idempotent,
                    )
                    self.assertEqual(parent_result, ("stored", 1, 1))
                    self.assertEqual(child_result, ("stored", 1, 1))
                    self.assertEqual(len(parent.read_all(limit=10)), 1)
                    self.assertEqual(len(parent.read_outbox()), 1)
                    self._assert_fresh_store_integrity(parent)

            with self.subTest(start_method=start_method, mode="lease"):
                path = str(Path(self.tempdir.name) / f"fresh-{start_method}-lease.sqlite3")
                with SQLiteEventStore(path, clock=lambda: "2026-08-21T00:00:10Z") as parent:
                    parent.append_with_outbox(
                        DomainEvent(
                            "stream:lease-race",
                            "lease.created",
                            {},
                            "actor:parent",
                            event_id="event:lease-race",
                            timestamp="2026-08-21T00:00:00Z",
                        ),
                        (
                            OutboxMessage(
                                "local:test",
                                {},
                                message_id="message:lease-race",
                                idempotency_key="message:lease-race",
                                available_at="2026-08-21T00:00:00Z",
                                created_at="2026-08-21T00:00:00Z",
                            ),
                        ),
                        expected_version=0,
                    )

                    def parent_claim() -> object:
                        return ("claimed", len(parent.claim_outbox("worker:parent", limit=1)))

                    parent_result, child_result = self._run_fresh_process_race(
                        start_method,
                        path,
                        "claim",
                        parent_claim,
                    )
                    self.assertEqual(
                        sorted((parent_result[1], child_result[1])),  # type: ignore[index]
                        [0, 1],
                    )
                    durable = parent.get_outbox("message:lease-race")
                    self.assertIsNotNone(durable)
                    assert durable is not None
                    self.assertEqual(durable.status, OutboxStatus.IN_FLIGHT)
                    self.assertEqual(durable.attempt_count, 1)
                    self._assert_fresh_store_integrity(parent)

            with self.subTest(start_method=start_method, mode="ambiguity"):
                path = str(Path(self.tempdir.name) / f"fresh-{start_method}-amb.sqlite3")
                with SQLiteEventStore(path, clock=lambda: "2026-08-21T00:00:10Z") as parent:
                    parent.append_with_outbox(
                        DomainEvent(
                            "stream:ambiguity-race",
                            "ambiguity.created",
                            {},
                            "actor:parent",
                            event_id="event:ambiguity-race",
                            timestamp="2026-08-21T00:00:00Z",
                        ),
                        (
                            OutboxMessage(
                                "local:test",
                                {},
                                message_id="message:ambiguity-race",
                                idempotency_key="message:ambiguity-race",
                                available_at="2026-08-21T00:00:00Z",
                                created_at="2026-08-21T00:00:00Z",
                            ),
                        ),
                        expected_version=0,
                    )
                    claimed = parent.claim_outbox("worker:setup", limit=1)[0]
                    assert claimed.lease_token is not None
                    self.assertTrue(
                        parent.mark_outbox_ambiguous(
                            claimed.message.message_id,
                            claimed.lease_token,
                            "ack_failed",
                            marked_at="2026-08-21T00:00:11Z",
                        )
                    )
                    ambiguity = parent.read_outbox_ambiguities()[0]

                    def parent_resolve(
                        ambiguity_message_id: str = ambiguity.message_id,
                        ambiguity_digest: str = ambiguity.lease_token_digest,
                    ) -> object:
                        return (
                            "resolved",
                            parent.resolve_outbox_ambiguity(
                                ambiguity_message_id,
                                ambiguity_digest,
                                "retry",
                                resolved_at="2026-08-21T00:00:20Z",
                                retry_at="2026-08-21T00:00:20Z",
                            ),
                        )

                    parent_result, child_result = self._run_fresh_process_race(
                        start_method,
                        path,
                        "ambiguity",
                        parent_resolve,
                    )
                    self.assertEqual(
                        sorted((parent_result[1], child_result[1])),  # type: ignore[index]
                        [False, True],
                    )
                    resolved = parent.read_outbox_ambiguities(open_only=False)[0]
                    self.assertIn(resolved.resolution, {"retry", "dead_letter"})
                    durable = parent.get_outbox("message:ambiguity-race")
                    self.assertIsNotNone(durable)
                    assert durable is not None
                    self.assertIn(
                        durable.status,
                        {OutboxStatus.PENDING, OutboxStatus.DEAD_LETTER},
                    )
                    self._assert_fresh_store_integrity(parent)

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
