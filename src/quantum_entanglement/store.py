# ruff: noqa: UP006, UP031, UP035, UP037, UP045
"""SQLite append-only event store with optimistic concurrency and idempotency."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .delivery import (
    InboxAppendResult,
    InboxReceipt,
    OutboxMessage,
    OutboxStatus,
    StoredOutboxMessage,
)
from .events import DomainEvent, StoredEvent
from .migrations import apply_sqlite_migrations
from .protocol import new_id, utc_now


class ConcurrencyError(RuntimeError):
    """Raised when a stream changed after the caller read it."""


class EventStoreJsonError(ValueError):
    """Raised when caller JSON cannot be represented by the durable contract."""


class EventStoreJsonTooLargeError(EventStoreJsonError):
    """Raised before a JSON field exceeds a structural or encoded-size limit."""


_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 10_000
_MAX_JSON_KEY_LENGTH = 512
_MAX_JSON_STRING_LENGTH = 65_536
_MAX_JSON_INTEGER_BITS = 4_096
_MAPPING_PROXY_TYPE: type[Any] = type(MappingProxyType({}))


class _JsonTraversalState:
    __slots__ = ("active_container_ids", "nodes")

    def __init__(self) -> None:
        self.active_container_ids: set[int] = set()
        self.nodes = 0


@dataclass(frozen=True)
class OutboxAmbiguity:
    """Durable operator-reconciliation record for an uncertain external write."""

    message_id: str
    lease_token_digest: str
    reason_code: str
    attempt_count: int
    marked_at: str
    resolution: Optional[str] = None
    resolved_at: Optional[str] = None


class SQLiteEventStore:
    """Small durable event log suitable for the kernel and local-first clients."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        clock: Callable[[], str] = utc_now,
        max_json_bytes: int = 1024 * 1024,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(max_json_bytes) is not int:
            raise TypeError("max_json_bytes must be an integer")
        if max_json_bytes <= 0:
            raise ValueError("max_json_bytes must be greater than zero")
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clock = clock
        self._max_json_bytes = max_json_bytes
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

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
            apply_sqlite_migrations(self._connection, clock=self._now)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            else:
                try:
                    self._connection.execute("COMMIT")
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise

    @classmethod
    def _copy_json_value(
        cls,
        value: Any,
        *,
        path: str,
        depth: int,
        state: _JsonTraversalState,
    ) -> Any:
        if depth > _MAX_JSON_DEPTH:
            raise EventStoreJsonTooLargeError(f"{path} exceeds {_MAX_JSON_DEPTH} levels")
        state.nodes += 1
        if state.nodes > _MAX_JSON_NODES:
            raise EventStoreJsonTooLargeError(f"JSON field exceeds {_MAX_JSON_NODES} value nodes")

        value_type = type(value)
        if value is None or value_type is bool:
            return value
        if value_type is str:
            if len(value) > _MAX_JSON_STRING_LENGTH:
                raise EventStoreJsonTooLargeError(
                    f"{path} exceeds {_MAX_JSON_STRING_LENGTH} characters"
                )
            return value
        if value_type is int:
            if value.bit_length() > _MAX_JSON_INTEGER_BITS:
                raise EventStoreJsonTooLargeError(
                    f"{path} exceeds {_MAX_JSON_INTEGER_BITS} integer bits"
                )
            return value
        if value_type is float:
            if not math.isfinite(value):
                raise EventStoreJsonError(f"{path} contains a non-finite number")
            return value

        if value_type in (dict, _MAPPING_PROXY_TYPE):
            identity = id(value)
            if identity in state.active_container_ids:
                raise EventStoreJsonError(f"{path} contains a reference cycle")
            state.active_container_ids.add(identity)
            copied: Dict[str, Any] = {}
            try:
                for key, item in value.items():
                    if type(key) is not str:
                        raise EventStoreJsonError(f"{path} keys must be strings")
                    if len(key) > _MAX_JSON_KEY_LENGTH:
                        raise EventStoreJsonTooLargeError(
                            f"{path} key exceeds {_MAX_JSON_KEY_LENGTH} characters"
                        )
                    copied[key] = cls._copy_json_value(
                        item,
                        path=f"{path}.{key}",
                        depth=depth + 1,
                        state=state,
                    )
            except EventStoreJsonError:
                raise
            except Exception as exc:
                raise EventStoreJsonError(f"{path} mapping traversal failed") from exc
            finally:
                state.active_container_ids.discard(identity)
            return copied

        if value_type in (list, tuple):
            identity = id(value)
            if identity in state.active_container_ids:
                raise EventStoreJsonError(f"{path} contains a reference cycle")
            state.active_container_ids.add(identity)
            try:
                return [
                    cls._copy_json_value(
                        item,
                        path=f"{path}[{index}]",
                        depth=depth + 1,
                        state=state,
                    )
                    for index, item in enumerate(value)
                ]
            except EventStoreJsonError:
                raise
            except Exception as exc:
                raise EventStoreJsonError(f"{path} sequence traversal failed") from exc
            finally:
                state.active_container_ids.discard(identity)

        raise EventStoreJsonError(f"{path} contains unsupported type {value_type.__name__}")

    def _encode_json_object(self, value: Mapping[str, Any], field_name: str) -> str:
        copied = self._copy_json_value(
            value,
            path=field_name,
            depth=0,
            state=_JsonTraversalState(),
        )
        if type(copied) is not dict:
            raise EventStoreJsonError(f"{field_name} must be a plain JSON object")
        encoded = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > self._max_json_bytes:
            raise EventStoreJsonTooLargeError(
                f"{field_name} exceeds {self._max_json_bytes} encoded bytes"
            )
        return encoded

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
    def _row_to_ambiguity(row: sqlite3.Row) -> OutboxAmbiguity:
        return OutboxAmbiguity(
            message_id=row["message_id"],
            lease_token_digest=row["lease_token_digest"],
            reason_code=row["reason_code"],
            attempt_count=int(row["attempt_count"]),
            marked_at=row["marked_at"],
            resolution=row["resolution"],
            resolved_at=row["resolved_at"],
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

    @staticmethod
    def _normalize_timestamp(value: str, field_name: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _now(self) -> str:
        """Read the store-owned clock after the write transaction is acquired."""

        return self._normalize_timestamp(self._clock(), "clock")

    @staticmethod
    def _lease_token_digest(lease_token: str) -> str:
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token is required")
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

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
                self._encode_json_object(event.payload, "event payload"),
                event.correlation_id,
                event.causation_id,
                event.idempotency_key,
            ),
        )
        global_position = cursor.lastrowid
        if global_position is None:
            raise RuntimeError("SQLite did not return an event global position")
        return StoredEvent(event, sequence, int(global_position)), True

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
            stored, _inserted = self._append_in_transaction(connection, event, expected_version)
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
            stored, inserted = self._append_in_transaction(connection, event, expected_version)
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
                        self._encode_json_object(message.payload, "outbox payload"),
                        self._encode_json_object(message.headers, "outbox headers"),
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

            stored, _inserted = self._append_in_transaction(connection, event, expected_version)
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
                    self._encode_json_object(receipt.result, "inbox result"),
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
                        self._encode_json_object(event.payload, "event payload"),
                        event.correlation_id,
                        event.causation_id,
                        event.idempotency_key,
                    ),
                )
                global_position = cursor.lastrowid
                if global_position is None:
                    raise RuntimeError("SQLite did not return an event global position")
                stored.append(StoredEvent(event, sequence, int(global_position)))
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

        # Rolling-upgrade compatibility only.  Caller time is deliberately
        # ignored; the authoritative value is sampled after the write lock.
        _ = now
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        with self._transaction() as connection:
            claimed_at = self._now()
            lease_expires_at = self._lease_deadline(claimed_at, lease_seconds)
            rows = connection.execute(
                """
                SELECT * FROM outbox
                WHERE (
                    (
                        status = ? AND julianday(available_at) <= julianday(?)
                    ) OR (
                        status = ? AND lease_expires_at IS NOT NULL
                        AND julianday(lease_expires_at) <= julianday(?)
                    )
                ) AND NOT EXISTS (
                    SELECT 1 FROM outbox_ambiguities ambiguity
                    WHERE ambiguity.message_id = outbox.message_id
                    AND ambiguity.resolved_at IS NULL
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
        now: Optional[str] = None,
    ) -> bool:
        """Atomically ACK a live lease; stale or expired workers always lose."""

        # Rolling-upgrade compatibility only; never trust caller time for CAS.
        _ = now
        with self._transaction() as connection:
            checked_at = self._now()
            acknowledged_at = self._normalize_timestamp(published_at or checked_at, "published_at")
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status = ?, lease_token = NULL, lease_expires_at = NULL,
                    published_at = ?, last_error = NULL
                WHERE message_id = ? AND status = ? AND lease_token = ?
                AND lease_expires_at IS NOT NULL
                AND julianday(lease_expires_at) > julianday(?)
                AND NOT EXISTS (
                    SELECT 1 FROM outbox_ambiguities ambiguity
                    WHERE ambiguity.message_id = outbox.message_id
                    AND ambiguity.resolved_at IS NULL
                )
                """,
                (
                    OutboxStatus.PUBLISHED.value,
                    acknowledged_at,
                    message_id,
                    OutboxStatus.IN_FLIGHT.value,
                    lease_token,
                    checked_at,
                ),
            )
            return cursor.rowcount == 1

    def reject_outbox(
        self,
        message_id: str,
        lease_token: str,
        error: str,
        *,
        retry_at: Optional[str] = None,
        dead_letter: bool = False,
        now: Optional[str] = None,
    ) -> bool:
        """Atomically NACK a live lease or move it to dead letter."""

        # Rolling-upgrade compatibility only; never trust caller time for CAS.
        _ = now
        status = OutboxStatus.DEAD_LETTER if dead_letter else OutboxStatus.PENDING
        with self._transaction() as connection:
            rejected_at = self._now()
            available_at = self._normalize_timestamp(retry_at or rejected_at, "retry_at")
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status = ?, available_at = ?, lease_token = NULL,
                    lease_expires_at = NULL, last_error = ?
                WHERE message_id = ? AND status = ? AND lease_token = ?
                AND lease_expires_at IS NOT NULL
                AND julianday(lease_expires_at) > julianday(?)
                AND NOT EXISTS (
                    SELECT 1 FROM outbox_ambiguities ambiguity
                    WHERE ambiguity.message_id = outbox.message_id
                    AND ambiguity.resolved_at IS NULL
                )
                """,
                (
                    status.value,
                    available_at,
                    error,
                    message_id,
                    OutboxStatus.IN_FLIGHT.value,
                    lease_token,
                    rejected_at,
                ),
            )
            return cursor.rowcount == 1

    def mark_outbox_ambiguous(
        self,
        message_id: str,
        lease_token: str,
        reason_code: str,
        *,
        marked_at: Optional[str] = None,
    ) -> bool:
        """Durably quarantine an uncertain external write for operator review."""

        allowed_reasons = {
            "callback_timeout",
            "caller_cancelled",
            "ack_failed",
            "lease_expired_after_accept",
        }
        if reason_code not in allowed_reasons:
            raise ValueError("unsupported outbox ambiguity reason")
        lease_token_digest = self._lease_token_digest(lease_token)
        recorded_at = self._normalize_timestamp(marked_at or utc_now(), "marked_at")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT status, lease_token, attempt_count FROM outbox
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != OutboxStatus.IN_FLIGHT.value
                or row["lease_token"] != lease_token
            ):
                return False
            connection.execute(
                """
                INSERT INTO outbox_ambiguities (
                    message_id, lease_token_digest, reason_code, attempt_count, marked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(message_id, lease_token_digest) DO NOTHING
                """,
                (
                    message_id,
                    lease_token_digest,
                    reason_code,
                    int(row["attempt_count"]),
                    recorded_at,
                ),
            )
            return True

    def read_outbox_ambiguities(self, *, open_only: bool = True) -> Tuple[OutboxAmbiguity, ...]:
        """Read durable reconciliation work in deterministic insertion order."""

        with self._lock:
            if open_only:
                rows = self._connection.execute(
                    """
                    SELECT * FROM outbox_ambiguities
                    WHERE resolved_at IS NULL ORDER BY rowid
                    """
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM outbox_ambiguities ORDER BY rowid"
                ).fetchall()
            return tuple(self._row_to_ambiguity(row) for row in rows)

    def resolve_outbox_ambiguity(
        self,
        message_id: str,
        lease_token_digest: str,
        resolution: str,
        *,
        resolved_at: Optional[str] = None,
        retry_at: Optional[str] = None,
    ) -> bool:
        """Apply an operator's evidence-backed resolution and unblock delivery."""

        if resolution not in {"published", "retry", "dead_letter"}:
            raise ValueError("unsupported outbox ambiguity resolution")
        if (
            not isinstance(lease_token_digest, str)
            or len(lease_token_digest) != 64
            or any(character not in "0123456789abcdef" for character in lease_token_digest)
        ):
            raise ValueError("lease_token_digest must be a lowercase SHA-256 digest")
        decided_at = self._normalize_timestamp(resolved_at or utc_now(), "resolved_at")
        available_at = self._normalize_timestamp(retry_at or decided_at, "retry_at")
        with self._transaction() as connection:
            ambiguity = connection.execute(
                """
                SELECT 1 FROM outbox_ambiguities
                WHERE message_id = ? AND lease_token_digest = ? AND resolved_at IS NULL
                """,
                (message_id, lease_token_digest),
            ).fetchone()
            outbox = connection.execute(
                "SELECT status, lease_token FROM outbox WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if (
                ambiguity is None
                or outbox is None
                or outbox["status"] != OutboxStatus.IN_FLIGHT.value
                or self._lease_token_digest(outbox["lease_token"]) != lease_token_digest
            ):
                return False
            lease_token = outbox["lease_token"]
            if resolution == "published":
                connection.execute(
                    """
                    UPDATE outbox SET status = ?, lease_token = NULL, lease_expires_at = NULL,
                        published_at = ?, last_error = NULL
                    WHERE message_id = ? AND status = ? AND lease_token = ?
                    """,
                    (
                        OutboxStatus.PUBLISHED.value,
                        decided_at,
                        message_id,
                        OutboxStatus.IN_FLIGHT.value,
                        lease_token,
                    ),
                )
            else:
                target = OutboxStatus.PENDING if resolution == "retry" else OutboxStatus.DEAD_LETTER
                connection.execute(
                    """
                    UPDATE outbox SET status = ?, available_at = ?,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error = ?
                    WHERE message_id = ? AND status = ? AND lease_token = ?
                    """,
                    (
                        target.value,
                        available_at,
                        f"operator_reconciled:{resolution}",
                        message_id,
                        OutboxStatus.IN_FLIGHT.value,
                        lease_token,
                    ),
                )
            connection.execute(
                """
                UPDATE outbox_ambiguities SET resolution = ?, resolved_at = ?
                WHERE message_id = ? AND lease_token_digest = ? AND resolved_at IS NULL
                """,
                (resolution, decided_at, message_id, lease_token_digest),
            )
            return True

    def get_outbox(self, message_id: str) -> Optional[StoredOutboxMessage]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
            ).fetchone()
            return None if row is None else self._row_to_outbox(row)

    def read_outbox(self, status: Optional[OutboxStatus] = None) -> Tuple[StoredOutboxMessage, ...]:
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

    def get_inbox_receipt(self, consumer_id: str, message_id: str) -> Optional[InboxReceipt]:
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
                (
                    stream_id,
                    sequence,
                    self._encode_json_object(state, "snapshot state"),
                    at,
                ),
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
