from __future__ import annotations

import os
import select
import signal
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from quantum_entanglement import EventStoreLifecycleError
from quantum_entanglement.delivery import OutboxStatus
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
                if any(value is item for value in local_values for item in forbidden):
                    return b"leaked-local"
            traceback_cursor = traceback_cursor.tb_next
        return b"rejected"
    except BaseException:
        return b"wrong-error"
    return b"accepted"


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


if __name__ == "__main__":
    unittest.main()
