from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tests.test_result_acceptance_durable_prerequisites as durable_prerequisites
from quantum_entanglement.events import StoredEvent
from quantum_entanglement.result_projection import (
    RESULT_PROJECTION_TABLE,
    ResultProjectionConflictError,
    ResultProjectionProcessMismatchError,
    ResultProjectionSchemaError,
    ResultProjectionStatus,
    SQLiteResultProjectionStore,
)
from quantum_entanglement.store import SQLiteEventStore


class _TupleSource:
    def __init__(self, events: Iterable[StoredEvent]) -> None:
        self.events = tuple(events)

    def read_all(self, after_position: int = 0, limit: int = 1000) -> tuple[StoredEvent, ...]:
        return tuple(
            event for event in self.events if event.global_position > after_position
        )[:limit]


class ResultProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "event-store.sqlite3")
        self.event_store = SQLiteEventStore(
            self.path,
            clock=lambda: "2026-08-27T10:00:00.000000Z",
            enable_result_acceptance_schema=True,
        )
        helper = durable_prerequisites.ResultAcceptanceDurablePrerequisiteTests(
            methodName="runTest"
        )
        helper.store = self.event_store
        prepared = helper.fresh_prepared()
        self.event_store._clock = lambda: "2026-08-27T10:00:02.000000Z"
        with patch(
            "quantum_entanglement.store.new_id",
            side_effect=(
                "receipt-projection-1",
                "event-result-projection-1",
                "event-terminal-projection-1",
            ),
        ):
            self.event_store.accept_scoped_invocation_result_v2(
                prepared.request,
                prepared.claimed,
            )
        self.events = self.event_store.read_all()
        self.result_event = next(
            event
            for event in self.events
            if event.event.event_type == "task.invocation.result.accepted"
        )
        self.terminal_event = next(
            event
            for event in self.events
            if event.event.event_type == "task.status.changed"
            and "resultReceiptId" in event.event.payload
        )
        self.scope = (
            self.result_event.event.payload["tenantId"],
            self.result_event.event.payload["workspaceId"],
            self.result_event.event.payload["invocationId"],
        )
        self.projection = SQLiteResultProjectionStore(self.event_store, self.path)

    def tearDown(self) -> None:
        self.projection.close()
        self.event_store.close()
        self.directory.cleanup()

    def test_complete_result_and_terminal_events_materialize_completed_view(self) -> None:
        run = self.projection.run_once()
        self.assertEqual(run.scanned_count, len(self.events))
        self.assertEqual(run.applied_count, len(self.events))
        view = self.projection.read(*self.scope)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.status, ResultProjectionStatus.COMPLETED)
        self.assertEqual(view.result_ref, "result:durable-prerequisite-1")
        self.assertEqual(view.artifact_count, 1)
        self.assertEqual(view.result_event_id, self.result_event.event.event_id)
        self.assertEqual(view.terminal_event_id, self.terminal_event.event.event_id)
        self.assertNotIn("durable result", repr(view))
        self.assertNotIn("lease-token", repr(view))

    def test_scope_isolation_does_not_make_projection_enumerable(self) -> None:
        self.projection.run_once()
        tenant_id, workspace_id, invocation_id = self.scope
        self.assertIsNone(self.projection.read("other-tenant", workspace_id, invocation_id))
        self.assertIsNone(self.projection.read(tenant_id, "other-workspace", invocation_id))
        self.assertIsNone(self.projection.read(tenant_id, workspace_id, "other-invocation"))

    def test_repeated_projector_run_is_idempotent(self) -> None:
        first = self.projection.run_once()
        second = self.projection.run_once()
        self.assertEqual(first.applied_count, len(self.events))
        self.assertEqual(second.scanned_count, 0)
        self.assertEqual(second.applied_count, 0)
        self.assertEqual(self.projection.read(*self.scope).status, ResultProjectionStatus.COMPLETED)

    def test_terminal_event_without_result_fails_closed(self) -> None:
        source = _TupleSource((replace(self.terminal_event, global_position=1, sequence=1),))
        candidate = SQLiteResultProjectionStore(
            self.event_store,
            self.path + ".terminal-only",
            owner_id="terminal-only",
        )
        try:
            with self.assertRaises(ResultProjectionConflictError):
                candidate._projector.event_source = source
                candidate.run_once()
            self.assertIsNone(candidate.read(*self.scope))
        finally:
            candidate.close()

    def test_result_identity_conflict_fails_closed(self) -> None:
        duplicate = replace(
            self.result_event,
            sequence=self.result_event.sequence + 10,
            global_position=len(self.events) + 1,
            event=replace(self.result_event.event, event_id="event-result-projection-duplicate"),
        )
        source = _TupleSource((*self.events, duplicate))
        candidate = SQLiteResultProjectionStore(
            self.event_store,
            self.path + ".conflict",
            owner_id="conflict",
        )
        try:
            candidate._projector.event_source = source
            with self.assertRaises(ResultProjectionConflictError):
                candidate.run_once()
            view = candidate.read(*self.scope)
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(view.result_event_id, self.result_event.event.event_id)
        finally:
            candidate.close()

    def test_projection_schema_drift_is_rejected(self) -> None:
        path = self.path + ".drift"
        connection = sqlite3.connect(path)
        try:
            connection.execute(f"CREATE TABLE {RESULT_PROJECTION_TABLE} (wrong TEXT)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ResultProjectionSchemaError):
            SQLiteResultProjectionStore(self.event_store, path, owner_id="schema-drift")

    def test_handler_only_writes_projection_owned_table(self) -> None:
        statements: list[str] = []
        self.projection._connection.set_trace_callback(statements.append)
        self.projection.run_once()
        forbidden = ("events", "invocation_jobs", "invocation_attempts", "outbox")
        handler_sql = tuple(statement.lower() for statement in statements)
        self.assertFalse(
            any(any(table in statement for table in forbidden) for statement in handler_sql)
        )
        self.assertTrue(any(RESULT_PROJECTION_TABLE in statement for statement in handler_sql))

    def test_fork_inherited_projection_rejects_before_touching_sqlite(self) -> None:
        self.projection.run_once()
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                self.projection.run_once()
            except ResultProjectionProcessMismatchError:
                os.write(write_fd, b"ok")
                os._exit(0)
            except BaseException:
                os.write(write_fd, b"bad")
                os._exit(1)
            os.write(write_fd, b"missing")
            os._exit(1)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 32)
        finally:
            os.close(read_fd)
        _, status = os.waitpid(child_pid, 0)
        self.assertEqual(result, b"ok")
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(self.projection.run_once().scanned_count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
