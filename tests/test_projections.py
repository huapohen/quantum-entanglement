import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping
from operator import attrgetter
from pathlib import Path
from typing import Any

from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.projections import (
    SCHEMA_VERSION_FIELD,
    DurableProjector,
    EventUpcasterRegistry,
    FutureEventSchemaVersionError,
    InvalidEventSchemaVersionError,
    InvalidUpcastResultError,
    MissingUpcasterError,
    ProjectionLeaseConflictError,
    ProjectionLeaseLostError,
    ProjectionOffsetConflictError,
    ProjectionStatementResult,
    ProjectionTransaction,
    SQLiteProjectionOffsetStore,
    UnknownEventTypeError,
    UpcastedEvent,
)
from quantum_entanglement.store import SQLiteEventStore


def stored(payload: Mapping[str, Any], *, event_type: str = "task.created") -> StoredEvent:
    return StoredEvent(
        DomainEvent("session:s1", event_type, payload, "user", event_id="evt-1"),
        sequence=1,
        global_position=1,
    )


class EventUpcasterRegistryTests(unittest.TestCase):
    def test_contiguous_chain_upcasts_legacy_v1_without_mutating_event(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=3)

        def one_to_two(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"title": payload["name"], "metadata": payload["metadata"]}

        def two_to_three(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            payload["metadata"]["normalized"] = True
            return {"title": payload["title"], "metadata": payload["metadata"]}

        registry.register_upcaster("task.created", from_version=1, upcaster=one_to_two)
        registry.register_upcaster("task.created", from_version=2, upcaster=two_to_three)
        original = stored({"name": "ship", "metadata": {"source": "human"}})

        result = registry.upcast(original)

        self.assertEqual(result.source_schema_version, 1)
        self.assertEqual(result.schema_version, 3)
        self.assertEqual(
            dict(result.payload),
            {"title": "ship", "metadata": {"source": "human", "normalized": True}},
        )
        self.assertEqual(
            original.event.payload,
            {"name": "ship", "metadata": {"source": "human"}},
        )

    def test_current_payload_strips_reserved_schema_metadata(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=2)

        result = registry.upcast(stored({SCHEMA_VERSION_FIELD: 2, "title": "ship"}))

        self.assertEqual(dict(result.payload), {"title": "ship"})
        self.assertEqual(result.source_schema_version, 2)

    def test_unknown_event_type_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=1)

        with self.assertRaisesRegex(UnknownEventTypeError, "unregistered event type"):
            registry.upcast(stored({}, event_type="task.deleted"))

    def test_future_schema_version_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=2)

        with self.assertRaisesRegex(FutureEventSchemaVersionError, "newer than supported"):
            registry.upcast(stored({SCHEMA_VERSION_FIELD: 3}))

    def test_non_integer_schema_version_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=2)

        for invalid in (True, 0, "2", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidEventSchemaVersionError):
                    registry.upcast(stored({SCHEMA_VERSION_FIELD: invalid}))

    def test_missing_middle_upcaster_fails_closed(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=3)
        registry.register_upcaster("task.created", from_version=2, upcaster=lambda payload: payload)

        with self.assertRaisesRegex(MissingUpcasterError, "v1 -> v2"):
            registry.upcast(stored({"name": "ship"}))

    def test_upcaster_must_return_mapping_without_reserved_metadata(self) -> None:
        registry = EventUpcasterRegistry()
        registry.register_event_type("task.created", current_version=2)
        registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda _payload: [],  # type: ignore[arg-type, return-value]
        )
        with self.assertRaisesRegex(InvalidUpcastResultError, "must return a mapping"):
            registry.upcast(stored({}))

        other = EventUpcasterRegistry()
        other.register_event_type("task.created", current_version=2)
        other.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda _payload: {SCHEMA_VERSION_FIELD: 2},
        )
        with self.assertRaisesRegex(InvalidUpcastResultError, "reserved field"):
            other.upcast(stored({}))

    def test_registration_rejects_invalid_or_duplicate_contracts(self) -> None:
        registry = EventUpcasterRegistry()
        with self.assertRaises(ValueError):
            registry.register_event_type("", current_version=1)
        with self.assertRaises(ValueError):
            registry.register_event_type("task.created", current_version=True)

        registry.register_event_type("task.created", current_version=2)
        with self.assertRaises(ValueError):
            registry.register_event_type("task.created", current_version=2)
        with self.assertRaises(UnknownEventTypeError):
            registry.register_upcaster("task.deleted", from_version=1, upcaster=dict)
        registry.register_upcaster("task.created", from_version=1, upcaster=dict)
        with self.assertRaises(ValueError):
            registry.register_upcaster("task.created", from_version=1, upcaster=dict)


class MutableClock:
    def __init__(self, value: str = "2026-08-20T00:00:00Z") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class SQLiteProjectionOffsetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.clock = MutableClock()
        self.store = SQLiteProjectionOffsetStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_offset_survives_close_and_reopen_on_existing_database(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        advanced = self.store.advance(lease, expected_position=0, new_position=7)
        self.assertEqual(advanced.last_global_position, 7)
        self.store.close()

        self.store = SQLiteProjectionOffsetStore(self.path, clock=self.clock)

        self.assertEqual(self.store.load("task-list").last_global_position, 7)

    def test_unseen_projection_has_virtual_zero_checkpoint(self) -> None:
        offset = self.store.load("new-view")
        self.assertEqual(offset.last_global_position, 0)
        self.assertEqual(offset.owner_epoch, 0)
        self.assertIsNone(offset.owner_id)

    def test_active_owner_blocks_takeover_then_expiry_increments_epoch(self) -> None:
        first = self.store.claim("task-list", "worker-a", lease_seconds=30)
        with self.assertRaises(ProjectionLeaseConflictError):
            self.store.claim("task-list", "worker-b", lease_seconds=30)

        self.clock.value = "2026-08-20T00:00:31Z"
        second = self.store.claim("task-list", "worker-b", lease_seconds=30)

        self.assertEqual(second.owner_epoch, first.owner_epoch + 1)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.advance(first, expected_position=0, new_position=1)
        advanced = self.store.advance(second, expected_position=0, new_position=1)
        self.assertEqual(advanced.last_global_position, 1)

    def test_reclaim_by_same_owner_fences_its_old_incarnation(self) -> None:
        first = self.store.claim("task-list", "worker-a", lease_seconds=30)
        second = self.store.claim("task-list", "worker-a", lease_seconds=30)

        self.assertGreater(second.owner_epoch, first.owner_epoch)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.renew(first)
        renewed = self.store.renew(second)
        self.assertEqual(renewed.owner_epoch, second.owner_epoch)

    def test_offset_advance_is_monotonic_compare_and_swap(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        self.store.advance(lease, expected_position=0, new_position=5)

        with self.assertRaises(ProjectionOffsetConflictError):
            self.store.advance(lease, expected_position=0, new_position=6)
        with self.assertRaises(ValueError):
            self.store.advance(lease, expected_position=5, new_position=5)
        self.assertEqual(self.store.load("task-list").last_global_position, 5)

    def test_expired_lease_cannot_renew_advance_or_release(self) -> None:
        lease = self.store.claim("task-list", "worker-a", lease_seconds=30)
        self.clock.value = "2026-08-20T00:00:30Z"

        with self.assertRaises(ProjectionLeaseLostError):
            self.store.renew(lease)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.advance(lease, expected_position=0, new_position=1)
        with self.assertRaises(ProjectionLeaseLostError):
            self.store.release(lease)

    def test_concurrent_claims_have_exactly_one_owner(self) -> None:
        contender = SQLiteProjectionOffsetStore(self.path, clock=self.clock)
        barrier = threading.Barrier(2)
        winners: list[str] = []
        conflicts: list[str] = []
        failures: list[BaseException] = []

        def claim(store: SQLiteProjectionOffsetStore, owner: str) -> None:
            try:
                barrier.wait()
                store.claim("task-list", owner, lease_seconds=30)
                winners.append(owner)
            except ProjectionLeaseConflictError:
                conflicts.append(owner)
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        threads = (
            threading.Thread(target=claim, args=(self.store, "worker-a")),
            threading.Thread(target=claim, args=(contender, "worker-b")),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        contender.close()

        self.assertFalse(failures)
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertFalse(any(thread.is_alive() for thread in threads))


class DurableProjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.clock = MutableClock()
        self.events = SQLiteEventStore(self.path)
        self.offsets = SQLiteProjectionOffsetStore(self.path, clock=self.clock)
        self.registry = EventUpcasterRegistry()
        self.registry.register_event_type("task.created", current_version=2)
        self.registry.register_upcaster(
            "task.created",
            from_version=1,
            upcaster=lambda payload: {"title": payload["name"]},
        )
        self.events.append(
            DomainEvent(
                "session:s1",
                "task.created",
                {"name": "research"},
                "user",
                event_id="evt-1",
            )
        )
        self.events.append(
            DomainEvent(
                "session:s1",
                "task.created",
                {SCHEMA_VERSION_FIELD: 2, "title": "ship"},
                "user",
                event_id="evt-2",
            )
        )

    def tearDown(self) -> None:
        self.offsets.close()
        self.events.close()
        self.tempdir.cleanup()

    @staticmethod
    def create_view(transaction: ProjectionTransaction) -> None:
        transaction.execute(
            """
            CREATE TABLE IF NOT EXISTS task_view (
                event_id TEXT PRIMARY KEY,
                title TEXT NOT NULL
            )
            """
        )

    def read_view(self) -> list[tuple[str, str]]:
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                "SELECT event_id, title FROM task_view ORDER BY event_id"
            ).fetchall()
            return [(str(row[0]), str(row[1])) for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def test_projector_upcasts_and_checkpoints_a_bounded_batch(self) -> None:
        def handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )

        projector = DurableProjector(
            "task-list",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        )

        first = projector.run_once(limit=1)
        second = projector.run_once(limit=10)
        empty = projector.run_once(limit=10)

        self.assertEqual((first.scanned_count, first.applied_count), (1, 1))
        self.assertEqual((second.scanned_count, second.applied_count), (1, 1))
        self.assertEqual((empty.scanned_count, empty.applied_count), (0, 0))
        self.assertEqual(first.last_global_position, 1)
        self.assertEqual(second.last_global_position, 2)
        self.assertEqual(self.read_view(), [("evt-1", "research"), ("evt-2", "ship")])

    def test_handler_failure_rolls_back_view_receipt_and_offset_then_replays(self) -> None:
        failed_once = False

        def flaky_handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            nonlocal failed_once
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )
            if event.stored_event.event.event_id == "evt-2" and not failed_once:
                failed_once = True
                raise RuntimeError("simulated crash after read-model write")

        projector = DurableProjector(
            "task-list",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            flaky_handler,
        )

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            projector.run_once(limit=10)

        self.assertEqual(self.offsets.load("task-list").last_global_position, 1)
        self.assertEqual(self.read_view(), [("evt-1", "research")])

        recovered = DurableProjector(
            "task-list",
            "worker-b",
            self.events,
            self.offsets,
            self.registry,
            flaky_handler,
        ).run_once(limit=10)

        self.assertEqual(recovered.applied_count, 1)
        self.assertEqual(recovered.last_global_position, 2)
        self.assertEqual(self.read_view(), [("evt-1", "research"), ("evt-2", "ship")])

    def test_handler_cannot_commit_partial_view_writes(self) -> None:
        def handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            self.create_view(transaction)
            event_id = event.stored_event.event.event_id
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event_id, event.payload["title"]),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                transaction.execute("COMMIT")
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (f"{event_id}-after-commit", event.payload["title"]),
            )
            raise RuntimeError("simulated failure after rejected handler commit")

        projector = DurableProjector(
            "commit-boundary",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        )

        with self.assertRaisesRegex(RuntimeError, "rejected handler commit"):
            projector.run_once(limit=1)

        self.assertEqual(self.read_view(), [])
        self.assertEqual(self.offsets.load("commit-boundary").last_global_position, 0)
        connection = sqlite3.connect(self.path)
        try:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM projection_receipts WHERE projection_name = ?",
                ("commit-boundary",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(receipt_count, 0)

    def test_statement_result_does_not_expose_cursor_connection(self) -> None:
        observed: list[ProjectionStatementResult] = []

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            result = transaction.execute("SELECT ? AS answer", (42,))
            self.assertIsInstance(result, ProjectionStatementResult)
            self.assertNotIsInstance(result, sqlite3.Cursor)
            with self.assertRaises(AttributeError):
                attrgetter("connection")(result)
            observed.append(result)

        result = DurableProjector(
            "connection-free-result",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(result.applied_count, 1)
        self.assertEqual(observed[0].columns, ("answer",))
        self.assertEqual(observed[0].rows, ((42,),))

    def test_handler_sql_control_statements_are_denied_without_blocking_finalize(self) -> None:
        statements = (
            "ROLLBACK",
            "SAVEPOINT handler_scope",
            "PRAGMA user_version",
            "ATTACH DATABASE ':memory:' AS handler_db",
            "DETACH DATABASE handler_db",
        )

        for index, statement in enumerate(statements):
            with self.subTest(statement=statement):
                projection_name = f"sql-control-{index}"

                def handler(
                    transaction: ProjectionTransaction,
                    _event: UpcastedEvent,
                    sql: str = statement,
                ) -> None:
                    with self.assertRaises(sqlite3.DatabaseError):
                        transaction.execute(sql)

                run = DurableProjector(
                    projection_name,
                    "worker-a",
                    self.events,
                    self.offsets,
                    self.registry,
                    handler,
                ).run_once(limit=1)

                self.assertEqual(run.applied_count, 1)
                self.assertEqual(
                    self.offsets.load(projection_name).last_global_position,
                    1,
                )

    def test_handler_cannot_access_or_change_framework_tables(self) -> None:
        statements = (
            "SELECT * FROM projection_offsets",
            "UPDATE projection_offsets SET last_global_position = 99",
            "DELETE FROM projection_receipts",
            "DROP TABLE projection_receipts",
            "SELECT * FROM qe_schema_migrations",
            "CREATE INDEX handler_index ON projection_offsets(last_global_position)",
        )

        def handler(transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            for statement in statements:
                with self.assertRaises(sqlite3.DatabaseError):
                    transaction.execute(statement)

        result = DurableProjector(
            "framework-boundary",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        ).run_once(limit=1)

        self.assertEqual(result.applied_count, 1)
        self.assertEqual(self.offsets.load("framework-boundary").last_global_position, 1)

    def test_handler_authorizer_is_restored_after_base_exception(self) -> None:
        def interrupted_handler(
            transaction: ProjectionTransaction,
            _event: UpcastedEvent,
        ) -> None:
            transaction.execute(
                "CREATE TABLE IF NOT EXISTS interrupted_view (event_id TEXT PRIMARY KEY)"
            )
            raise KeyboardInterrupt("simulated handler interruption")

        interrupted = DurableProjector(
            "base-exception",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            interrupted_handler,
        )
        with self.assertRaisesRegex(KeyboardInterrupt, "handler interruption"):
            interrupted.run_once(limit=1)

        calls = 0

        def recovered_handler(
            transaction: ProjectionTransaction,
            event: UpcastedEvent,
        ) -> None:
            nonlocal calls
            calls += 1
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )

        recovered = DurableProjector(
            "base-exception",
            "worker-b",
            self.events,
            self.offsets,
            self.registry,
            recovered_handler,
        ).run_once(limit=1)

        self.assertEqual(calls, 1)
        self.assertEqual(recovered.last_global_position, 1)
        self.assertEqual(self.read_view(), [("evt-1", "research")])

    def test_receipt_makes_direct_concurrent_replay_idempotent(self) -> None:
        calls = 0

        def handler(transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
            nonlocal calls
            calls += 1
            self.create_view(transaction)
            transaction.execute(
                "INSERT INTO task_view (event_id, title) VALUES (?, ?)",
                (event.stored_event.event.event_id, event.payload["title"]),
            )

        lease = self.offsets.claim("task-list", "worker-a", lease_seconds=30)
        event = self.registry.upcast(self.events.read_all(limit=1)[0])
        barrier = threading.Barrier(2)
        results: list[bool] = []
        failures: list[BaseException] = []

        def apply() -> None:
            try:
                barrier.wait()
                result = self.offsets.apply_event(
                    lease,
                    expected_position=0,
                    event=event,
                    handler=handler,
                )
                results.append(result.applied)
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        threads = (threading.Thread(target=apply), threading.Thread(target=apply))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(failures)
        self.assertCountEqual(results, [True, False])
        self.assertEqual(calls, 1)
        self.assertEqual(self.read_view(), [("evt-1", "research")])
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_future_schema_never_invokes_handler_or_advances(self) -> None:
        future = self.events.append(
            DomainEvent(
                "session:s1",
                "task.created",
                {SCHEMA_VERSION_FIELD: 3, "title": "future"},
                "user",
                event_id="evt-3",
            )
        )
        calls = 0

        def handler(_transaction: ProjectionTransaction, _event: UpcastedEvent) -> None:
            nonlocal calls
            calls += 1

        projector = DurableProjector(
            "future-view",
            "worker-a",
            self.events,
            self.offsets,
            self.registry,
            handler,
        )
        projector.run_once(limit=2)
        with self.assertRaises(FutureEventSchemaVersionError):
            projector.run_once(limit=10)

        self.assertEqual(calls, 2)
        self.assertEqual(
            self.offsets.load("future-view").last_global_position,
            future.global_position - 1,
        )


if __name__ == "__main__":
    unittest.main()
