"""SQLite append-only event store with optimistic concurrency and idempotency."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .delivery import (
    InboxAppendResult,
    InboxReceipt,
    OutboxMessage,
    OutboxStatus,
    StoredOutboxMessage,
)
from .events import DomainEvent, StoredEvent
from .protocol import new_id, utc_now


class ConcurrencyError(RuntimeError):
    """Raised when a stream changed after the caller read it."""


class SQLiteEventStore:
    """Small durable event log suitable for the kernel and local-first clients."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS events (
                    global_position INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    correlation_id TEXT,
                    causation_id TEXT,
                    idempotency_key TEXT,
                    UNIQUE(stream_id, sequence),
                    UNIQUE(stream_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_events_stream
                    ON events(stream_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_correlation
                    ON events(correlation_id, global_position);
                CREATE TABLE IF NOT EXISTS snapshots (
                    stream_id TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_position INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    destination TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    headers_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    triggering_event_id TEXT NOT NULL,
                    triggering_global_position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    last_error TEXT,
                    published_at TEXT,
                    UNIQUE(destination, idempotency_key),
                    FOREIGN KEY(triggering_global_position)
                        REFERENCES events(global_position) ON DELETE RESTRICT,
                    CHECK(status IN ('pending', 'in_flight', 'published', 'dead_letter')),
                    CHECK(attempt_count >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_delivery
                    ON outbox(status, available_at, outbox_position);
                CREATE INDEX IF NOT EXISTS idx_outbox_trigger
                    ON outbox(triggering_global_position, outbox_position);
                CREATE TABLE IF NOT EXISTS inbox_receipts (
                    consumer_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_global_position INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY(consumer_id, message_id),
                    FOREIGN KEY(event_global_position)
                        REFERENCES events(global_position) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_event
                    ON inbox_receipts(event_global_position);
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        event = DomainEvent(
            stream_id=row["stream_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            actor_id=row["actor_id"],
            event_id=row["event_id"],
            timestamp=row["timestamp"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            idempotency_key=row["idempotency_key"],
        )
        return StoredEvent(
            event=event,
            sequence=int(row["sequence"]),
            global_position=int(row["global_position"]),
        )

    @staticmethod
    def _row_to_outbox(row: sqlite3.Row) -> StoredOutboxMessage:
        message = OutboxMessage(
            destination=row["destination"],
            payload=json.loads(row["payload_json"]),
            headers=json.loads(row["headers_json"]),
            message_id=row["message_id"],
            idempotency_key=row["idempotency_key"],
            available_at=row["available_at"],
            created_at=row["created_at"],
        )
        return StoredOutboxMessage(
            message=message,
            triggering_event_id=row["triggering_event_id"],
            triggering_global_position=int(row["triggering_global_position"]),
            status=OutboxStatus(row["status"]),
            attempt_count=int(row["attempt_count"]),
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            published_at=row["published_at"],
        )

    @staticmethod
    def _row_to_inbox(row: sqlite3.Row) -> InboxReceipt:
        return InboxReceipt(
            consumer_id=row["consumer_id"],
            message_id=row["message_id"],
            received_at=row["received_at"],
            event_id=row["event_id"],
            event_global_position=int(row["event_global_position"]),
            result=json.loads(row["result_json"]),
        )

    @staticmethod
    def _lease_deadline(now: str, lease_seconds: float) -> str:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("now must include a timezone")
        deadline = parsed.astimezone(timezone.utc) + timedelta(seconds=lease_seconds)
        return deadline.isoformat().replace("+00:00", "Z")

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: DomainEvent,
        expected_version: Optional[int],
    ) -> Tuple[StoredEvent, bool]:
        """Append inside an existing transaction and report whether a row was inserted."""

        if event.idempotency_key is not None:
            existing = connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND idempotency_key = ?",
                (event.stream_id, event.idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._row_to_event(existing), False

        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
            (event.stream_id,),
        ).fetchone()
        current_version = int(row["version"])
        if expected_version is not None and expected_version != current_version:
            raise ConcurrencyError(
                "stream %s expected version %d but is %d"
                % (event.stream_id, expected_version, current_version)
            )
        sequence = current_version + 1
        cursor = connection.execute(
            """
            INSERT INTO events (
                stream_id, sequence, event_id, event_type, actor_id, timestamp,
                payload_json, correlation_id, causation_id, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.stream_id,
                sequence,
                event.event_id,
                event.event_type,
                event.actor_id,
                event.timestamp,
                json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                event.correlation_id,
                event.causation_id,
                event.idempotency_key,
            ),
        )
        return StoredEvent(event, sequence, int(cursor.lastrowid)), True

    def stream_version(self, stream_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            return int(row["version"])

    def append(self, event: DomainEvent, expected_version: Optional[int] = None) -> StoredEvent:
        """Append one event, returning the existing record for an idempotent retry."""

        with self._transaction() as connection:
            stored, _inserted = self._append_in_transaction(
                connection, event, expected_version
            )
            return stored

    def append_with_outbox(
        self,
        event: DomainEvent,
        messages: Iterable[OutboxMessage],
        expected_version: Optional[int] = None,
    ) -> Tuple[StoredEvent, Tuple[StoredOutboxMessage, ...]]:
        """Atomically append an event and the messages caused by that event.

        An idempotent event retry returns the original linked outbox rows. It rejects
        a changed message set instead of silently attaching new side effects to an old
        event, preserving the event-to-delivery transaction boundary.
        """

        batch = tuple(messages)
        with self._transaction() as connection:
            stored, inserted = self._append_in_transaction(
                connection, event, expected_version
            )
            if not inserted:
                rows = connection.execute(
                    """
                    SELECT * FROM outbox
                    WHERE triggering_global_position = ?
                    ORDER BY outbox_position
                    """,
                    (stored.global_position,),
                ).fetchall()
                existing = tuple(self._row_to_outbox(row) for row in rows)
                requested = tuple(
                    (
                        item.message_id,
                        item.destination,
                        item.idempotency_key,
                        dict(item.payload),
                        dict(item.headers),
                    )
                    for item in batch
                )
                persisted = tuple(
                    (
                        item.message.message_id,
                        item.message.destination,
                        item.message.idempotency_key,
                        dict(item.message.payload),
                        dict(item.message.headers),
                    )
                    for item in existing
                )
                if requested != persisted:
                    raise ValueError(
                        "idempotent event retry changed its transactional outbox messages"
                    )
                return stored, existing

            for message in batch:
                connection.execute(
                    """
                    INSERT INTO outbox (
                        message_id, destination, payload_json, headers_json,
                        idempotency_key, triggering_event_id,
                        triggering_global_position, status, attempt_count,
                        available_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        message.message_id,
                        message.destination,
                        json.dumps(dict(message.payload), ensure_ascii=False, sort_keys=True),
                        json.dumps(dict(message.headers), ensure_ascii=False, sort_keys=True),
                        message.idempotency_key,
                        stored.event.event_id,
                        stored.global_position,
                        OutboxStatus.PENDING.value,
                        message.available_at,
                        message.created_at,
                    ),
                )
            rows = connection.execute(
                """
                SELECT * FROM outbox
                WHERE triggering_global_position = ?
                ORDER BY outbox_position
                """,
                (stored.global_position,),
            ).fetchall()
            return stored, tuple(self._row_to_outbox(row) for row in rows)

    def append_inbox(
        self,
        consumer_id: str,
        message_id: str,
        event: DomainEvent,
        *,
        result: Optional[Mapping[str, Any]] = None,
        received_at: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> InboxAppendResult:
        """Admit one external message and append its event in one transaction.

        The `(consumer_id, message_id)` receipt is the deduplication boundary. A
        retry returns the original event and result without appending again.
        """

        if not consumer_id.strip() or not message_id.strip():
            raise ValueError("consumer_id and message_id are required")
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM inbox_receipts
                WHERE consumer_id = ? AND message_id = ?
                """,
                (consumer_id, message_id),
            ).fetchone()
            if existing is not None:
                receipt = self._row_to_inbox(existing)
                event_row = connection.execute(
                    "SELECT * FROM events WHERE global_position = ?",
                    (receipt.event_global_position,),
                ).fetchone()
                if event_row is None:
                    raise RuntimeError("inbox receipt references a missing event")
                return InboxAppendResult(self._row_to_event(event_row), receipt, True)

            stored, _inserted = self._append_in_transaction(
                connection, event, expected_version
            )
            receipt = InboxReceipt(
                consumer_id=consumer_id,
                message_id=message_id,
                received_at=received_at or utc_now(),
                event_id=stored.event.event_id,
                event_global_position=stored.global_position,
                result=dict(result or {}),
            )
            connection.execute(
                """
                INSERT INTO inbox_receipts (
                    consumer_id, message_id, received_at, event_id,
                    event_global_position, result_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.consumer_id,
                    receipt.message_id,
                    receipt.received_at,
                    receipt.event_id,
                    receipt.event_global_position,
                    json.dumps(dict(receipt.result), ensure_ascii=False, sort_keys=True),
                ),
            )
            return InboxAppendResult(stored, receipt, False)

    def append_many(
        self,
        stream_id: str,
        events: Iterable[DomainEvent],
        expected_version: Optional[int] = None,
    ) -> Tuple[StoredEvent, ...]:
        """Atomically append a batch to one stream."""

        batch = tuple(events)
        if any(item.stream_id != stream_id for item in batch):
            raise ValueError("all batch events must use the declared stream_id")
        if not batch:
            return ()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            current_version = int(row["version"])
            if expected_version is not None and expected_version != current_version:
                raise ConcurrencyError(
                    "stream %s expected version %d but is %d"
                    % (stream_id, expected_version, current_version)
                )
            stored: List[StoredEvent] = []
            for offset, event in enumerate(batch, start=1):
                sequence = current_version + offset
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        stream_id, sequence, event_id, event_type, actor_id, timestamp,
                        payload_json, correlation_id, causation_id, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stream_id,
                        sequence,
                        event.event_id,
                        event.event_type,
                        event.actor_id,
                        event.timestamp,
                        json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                        event.correlation_id,
                        event.causation_id,
                        event.idempotency_key,
                    ),
                )
                stored.append(StoredEvent(event, sequence, int(cursor.lastrowid)))
            return tuple(stored)

    def read_stream(self, stream_id: str, after_sequence: int = 0) -> Tuple[StoredEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND sequence > ? ORDER BY sequence",
                (stream_id, after_sequence),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    def read_all(self, after_position: int = 0, limit: int = 1000) -> Tuple[StoredEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE global_position > ? ORDER BY global_position LIMIT ?",
                (after_position, limit),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 100,
        lease_seconds: float = 30.0,
        now: Optional[str] = None,
    ) -> Tuple[StoredOutboxMessage, ...]:
        """Lease due messages, including work abandoned by a crashed publisher."""

        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        claimed_at = now or utc_now()
        lease_expires_at = self._lease_deadline(claimed_at, lease_seconds)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox
                WHERE (
                    status = ? AND julianday(available_at) <= julianday(?)
                ) OR (
                    status = ? AND lease_expires_at IS NOT NULL
                    AND julianday(lease_expires_at) <= julianday(?)
                )
                ORDER BY outbox_position
                LIMIT ?
                """,
                (
                    OutboxStatus.PENDING.value,
                    claimed_at,
                    OutboxStatus.IN_FLIGHT.value,
                    claimed_at,
                    limit,
                ),
            ).fetchall()
            claimed: List[StoredOutboxMessage] = []
            for row in rows:
                lease_token = "%s:%s" % (worker_id, new_id("lease"))
                connection.execute(
                    """
                    UPDATE outbox
                    SET status = ?, attempt_count = attempt_count + 1,
                        lease_token = ?, lease_expires_at = ?
                    WHERE message_id = ?
                    """,
                    (
                        OutboxStatus.IN_FLIGHT.value,
                        lease_token,
                        lease_expires_at,
                        row["message_id"],
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM outbox WHERE message_id = ?",
                    (row["message_id"],),
                ).fetchone()
                claimed.append(self._row_to_outbox(updated))
            return tuple(claimed)

    def acknowledge_outbox(
        self,
        message_id: str,
        lease_token: str,
        *,
        published_at: Optional[str] = None,
    ) -> bool:
        """Mark a leased message published; stale workers cannot acknowledge it."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, lease_token FROM outbox WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None or row["lease_token"] != lease_token:
                return False
            if row["status"] == OutboxStatus.PUBLISHED.value:
                return True
            if row["status"] != OutboxStatus.IN_FLIGHT.value:
                return False
            connection.execute(
                """
                UPDATE outbox
                SET status = ?, lease_expires_at = NULL, published_at = ?, last_error = NULL
                WHERE message_id = ? AND status = ? AND lease_token = ?
                """,
                (
                    OutboxStatus.PUBLISHED.value,
                    published_at or utc_now(),
                    message_id,
                    OutboxStatus.IN_FLIGHT.value,
                    lease_token,
                ),
            )
            return True

    def reject_outbox(
        self,
        message_id: str,
        lease_token: str,
        error: str,
        *,
        retry_at: Optional[str] = None,
        dead_letter: bool = False,
    ) -> bool:
        """NACK a lease for retry or move it to the terminal dead-letter state."""

        status = OutboxStatus.DEAD_LETTER if dead_letter else OutboxStatus.PENDING
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status = ?, available_at = ?, lease_token = NULL,
                    lease_expires_at = NULL, last_error = ?
                WHERE message_id = ? AND status = ? AND lease_token = ?
                """,
                (
                    status.value,
                    retry_at or utc_now(),
                    error,
                    message_id,
                    OutboxStatus.IN_FLIGHT.value,
                    lease_token,
                ),
            )
            return cursor.rowcount == 1

    def get_outbox(self, message_id: str) -> Optional[StoredOutboxMessage]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
            ).fetchone()
            return None if row is None else self._row_to_outbox(row)

    def read_outbox(
        self, status: Optional[OutboxStatus] = None
    ) -> Tuple[StoredOutboxMessage, ...]:
        with self._lock:
            if status is None:
                rows = self._connection.execute(
                    "SELECT * FROM outbox ORDER BY outbox_position"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM outbox WHERE status = ? ORDER BY outbox_position",
                    (status.value,),
                ).fetchall()
            return tuple(self._row_to_outbox(row) for row in rows)

    def get_inbox_receipt(
        self, consumer_id: str, message_id: str
    ) -> Optional[InboxReceipt]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM inbox_receipts
                WHERE consumer_id = ? AND message_id = ?
                """,
                (consumer_id, message_id),
            ).fetchone()
            return None if row is None else self._row_to_inbox(row)

    def save_snapshot(
        self, stream_id: str, sequence: int, state: Dict[str, object], at: str
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(stream_id, sequence, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    sequence = excluded.sequence,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                WHERE excluded.sequence >= snapshots.sequence
                """,
                (stream_id, sequence, json.dumps(state, ensure_ascii=False, sort_keys=True), at),
            )

    def load_snapshot(self, stream_id: str) -> Optional[Tuple[int, Dict[str, object]]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT sequence, state_json FROM snapshots WHERE stream_id = ?", (stream_id,)
            ).fetchone()
            if row is None:
                return None
            return int(row["sequence"]), json.loads(row["state_json"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteEventStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
