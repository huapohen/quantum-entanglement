from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from quantum_entanglement.delivery import OutboxMessage, OutboxStatus
from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.store import (
    OutboxAmbiguityPageItem,
    OutboxPageItem,
    SQLiteEventStore,
)

T0 = "2026-08-20T00:00:00Z"
T1 = "2026-08-20T00:01:00Z"


class SQLiteEventStoreBoundedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "events.sqlite3")
        self.store = SQLiteEventStore(self.path, clock=lambda: T0)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _append_stream(self, stream_id: str, count: int) -> None:
        key_prefix = stream_id.replace(":", "-")
        for index in range(1, count + 1):
            self.store.append(
                DomainEvent(
                    stream_id=stream_id,
                    event_type="test.event",
                    payload={"index": index},
                    actor_id="test",
                    event_id=f"event-{key_prefix}-{index}",
                    timestamp=T0,
                    idempotency_key=f"event:{key_prefix}:{index}",
                )
            )

    def _seed_outbox(self, count: int = 5) -> None:
        messages = tuple(
            OutboxMessage(
                destination="test-runtime",
                payload={"index": index},
                message_id=f"message-{index}",
                idempotency_key=f"outbox:{index}",
                available_at=T0,
                created_at=T0,
            )
            for index in range(1, count + 1)
        )
        self.store.append_with_outbox(
            DomainEvent(
                stream_id="stream:outbox",
                event_type="outbox.seeded",
                payload={"count": count},
                actor_id="test",
                event_id="event-outbox",
                timestamp=T0,
                idempotency_key="event:outbox",
            ),
            messages,
        )
        self.store._connection.execute(
            """
            UPDATE outbox SET status = ?, published_at = ?
            WHERE message_id IN (?, ?)
            """,
            (
                OutboxStatus.PUBLISHED.value,
                T1,
                "message-2",
                "message-4",
            ),
        )

    def _seed_ambiguities(self) -> None:
        self._seed_outbox()
        for index in range(1, 6):
            resolved = index in {2, 4}
            lease_digest = hashlib.sha256(f"lease-{index}".encode()).hexdigest()
            self.store._connection.execute(
                """
                INSERT INTO outbox_ambiguities (
                    message_id, lease_token_digest, reason_code, attempt_count,
                    marked_at, resolution, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"message-{index}",
                    lease_digest,
                    "callback_timeout",
                    index,
                    T0,
                    "published" if resolved else None,
                    T1 if resolved else None,
                ),
            )

    def _collect_stream(self, stream_id: str, *, limit: int) -> list[StoredEvent]:
        cursor = 0
        collected: list[StoredEvent] = []
        while True:
            page = self.store.read_stream_page(stream_id, cursor, limit)
            if not page:
                return collected
            self.assertGreater(page[-1].sequence, cursor)
            collected.extend(page)
            cursor = page[-1].sequence

    def _collect_outbox(
        self,
        *,
        status: OutboxStatus | None,
        limit: int,
    ) -> list[OutboxPageItem]:
        cursor = 0
        collected: list[OutboxPageItem] = []
        while True:
            page = self.store.read_outbox_page(cursor, status, limit)
            if not page:
                return collected
            self.assertGreater(page[-1].position, cursor)
            collected.extend(page)
            cursor = page[-1].position

    def _collect_ambiguities(
        self,
        *,
        open_only: bool,
        limit: int,
    ) -> list[OutboxAmbiguityPageItem]:
        cursor = 0
        collected: list[OutboxAmbiguityPageItem] = []
        while True:
            page = self.store.read_outbox_ambiguities_page(cursor, open_only, limit)
            if not page:
                return collected
            self.assertGreater(page[-1].rowid, cursor)
            collected.extend(page)
            cursor = page[-1].rowid

    def test_stream_pages_are_bounded_cursor_ordered_and_have_no_gaps(self) -> None:
        self._append_stream("stream:target", 5)
        self._append_stream("stream:other", 2)

        collected = self._collect_stream("stream:target", limit=2)
        self.assertEqual([item.sequence for item in collected], [1, 2, 3, 4, 5])
        self.assertEqual(len({item.event.event_id for item in collected}), 5)
        self.assertEqual(len(self.store.read_stream_page("stream:target")), 5)
        self.assertEqual(len(self.store.read_stream_page("stream:target", 0, 1)), 1)
        self.assertEqual(len(self.store.read_stream_page("stream:target", 0, 1_000)), 5)
        self.assertEqual(self.store.read_stream_page("stream:target", 5, 2), ())
        self.assertEqual(self.store.read_stream_page("stream:target", 99, 2), ())

    def test_read_all_keeps_bounded_global_position_semantics(self) -> None:
        self._append_stream("stream:target", 3)

        first = self.store.read_all(after_position=0, limit=2)
        second = self.store.read_all(after_position=first[-1].global_position, limit=2)
        self.assertEqual([item.global_position for item in first], [1, 2])
        self.assertEqual([item.global_position for item in second], [3])
        self.assertEqual(self.store.read_all(after_position=3, limit=1), ())
        self.assertEqual(len(self.store.read_all(limit=1_000)), 3)

    def test_outbox_pages_expose_durable_positions_and_filter_status(self) -> None:
        self._seed_outbox()

        all_items = self._collect_outbox(status=None, limit=2)
        self.assertEqual([item.position for item in all_items], [1, 2, 3, 4, 5])
        self.assertEqual(len(self.store.read_outbox_page()), 5)
        self.assertEqual(
            [item.message.message.message_id for item in all_items],
            [f"message-{index}" for index in range(1, 6)],
        )
        published = self._collect_outbox(status=OutboxStatus.PUBLISHED, limit=1)
        self.assertEqual([item.position for item in published], [2, 4])
        self.assertTrue(all(item.message.status is OutboxStatus.PUBLISHED for item in published))
        pending = self._collect_outbox(status=OutboxStatus.PENDING, limit=2)
        self.assertEqual([item.position for item in pending], [1, 3, 5])
        self.assertEqual(self.store.read_outbox_page(5, None, 2), ())

    def test_ambiguity_pages_expose_rowids_and_filter_open_records(self) -> None:
        self._seed_ambiguities()

        open_items = self._collect_ambiguities(open_only=True, limit=2)
        self.assertEqual([item.rowid for item in open_items], [1, 3, 5])
        self.assertEqual(len(self.store.read_outbox_ambiguities_page()), 3)
        self.assertTrue(all(item.ambiguity.resolved_at is None for item in open_items))
        all_items = self._collect_ambiguities(open_only=False, limit=2)
        self.assertEqual([item.rowid for item in all_items], [1, 2, 3, 4, 5])
        self.assertEqual(self.store.read_outbox_ambiguities_page(5, False, 2), ())

    def test_page_cursors_and_limits_use_exact_bounded_integers(self) -> None:
        with self.assertRaises(TypeError):
            self.store.read_stream_page(True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.read_stream_page("  ")

        cursor_calls: tuple[Callable[[Any], object], ...] = (
            lambda value: self.store.read_stream_page("stream:target", value, 1),
            lambda value: self.store.read_all(value, 1),
            lambda value: self.store.read_outbox_page(value, None, 1),
            lambda value: self.store.read_outbox_ambiguities_page(value, True, 1),
        )
        for call in cursor_calls:
            for invalid in (True, False, 1.0, "0", None):
                with self.subTest(call=call, invalid=invalid):
                    with self.assertRaises(TypeError):
                        call(invalid)
            for invalid in (-1, 1 << 63):
                with self.subTest(call=call, invalid=invalid):
                    with self.assertRaises(ValueError):
                        call(invalid)
            call(0)
            call((1 << 63) - 1)

        limit_calls: tuple[Callable[[Any], object], ...] = (
            lambda value: self.store.read_stream_page("stream:target", 0, value),
            lambda value: self.store.read_all(0, value),
            lambda value: self.store.read_outbox_page(0, None, value),
            lambda value: self.store.read_outbox_ambiguities_page(0, True, value),
        )
        for call in limit_calls:
            for invalid in (True, False, 1.0, "1", None):
                with self.subTest(call=call, invalid=invalid):
                    with self.assertRaises(TypeError):
                        call(invalid)
            for invalid in (-1, 0, 1_001):
                with self.subTest(call=call, invalid=invalid):
                    with self.assertRaises(ValueError):
                        call(invalid)
            call(1)
            call(1_000)

        with self.assertRaises(TypeError):
            self.store.read_outbox_page(status="pending")
        for invalid in (None, 0, 1, "true"):
            with self.subTest(open_only=invalid):
                with self.assertRaises(TypeError):
                    self.store.read_outbox_ambiguities_page(
                        open_only=invalid  # type: ignore[arg-type]
                    )

    def test_read_all_rejects_invalid_bounds_before_executing_sql(self) -> None:
        statements: list[str] = []
        invalid_calls: tuple[tuple[type[BaseException], Callable[[], object]], ...] = (
            (TypeError, lambda: self.store.read_all(after_position=True)),
            (TypeError, lambda: self.store.read_all(limit=False)),
            (
                TypeError,
                lambda: self.store.read_all(limit=1.0),  # type: ignore[arg-type]
            ),
            (ValueError, lambda: self.store.read_all(after_position=-1)),
            (ValueError, lambda: self.store.read_all(after_position=1 << 63)),
            (ValueError, lambda: self.store.read_all(limit=-1)),
            (ValueError, lambda: self.store.read_all(limit=0)),
            (ValueError, lambda: self.store.read_all(limit=1_001)),
        )
        self.store._connection.set_trace_callback(statements.append)
        try:
            for error_type, call in invalid_calls:
                with self.subTest(error_type=error_type, call=call):
                    with self.assertRaises(error_type):
                        call()
        finally:
            self.store._connection.set_trace_callback(None)

        self.assertEqual(statements, [])

    def test_each_page_query_has_a_sql_limit(self) -> None:
        self._append_stream("stream:target", 3)
        self._seed_ambiguities()
        statements: list[str] = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            self.store.read_stream_page("stream:target", limit=2)
            self.store.read_all(limit=2)
            self.store.read_outbox_page(limit=2)
            self.store.read_outbox_ambiguities_page(limit=2)
        finally:
            self.store._connection.set_trace_callback(None)

        normalized = [" ".join(statement.upper().split()) for statement in statements]
        self.assertTrue(
            any("FROM EVENTS" in statement and "LIMIT" in statement for statement in normalized)
        )
        self.assertTrue(
            any(
                "GLOBAL_POSITION >" in statement and "LIMIT" in statement
                for statement in normalized
            )
        )
        self.assertTrue(
            any(
                "FROM OUTBOX WHERE" in statement and "LIMIT" in statement
                for statement in normalized
            )
        )
        self.assertTrue(
            any(
                "FROM OUTBOX_AMBIGUITIES" in statement and "LIMIT" in statement
                for statement in normalized
            )
        )


if __name__ == "__main__":
    unittest.main()
