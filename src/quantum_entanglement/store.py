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


class EventStoreIntegrityError(RuntimeError):
    """Raised when persisted event-store data violates its durable contract."""


class EventStoreJsonError(Exception):
    """Raised when caller JSON cannot be represented by the durable contract."""


class EventStoreJsonValueError(EventStoreJsonError, ValueError):
    """JSON value has an invalid scalar, cycle, or other value-level defect."""


class EventStoreJsonTypeError(EventStoreJsonError, TypeError):
    """JSON input uses a type outside the durable object contract."""


class EventStoreJsonTooLargeError(EventStoreJsonValueError):
    """Raised before a JSON field exceeds a structural or encoded-size limit."""


_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 10_000
_MAX_JSON_KEY_LENGTH = 512
_MAX_JSON_STRING_LENGTH = 65_536
_MAX_JSON_INTEGER_BITS = 4_096
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAPPING_PROXY_TYPE: type[Any] = type(MappingProxyType({}))


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def _persisted_integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise TypeError(f"persisted {field_name} must use SQLite INTEGER storage")
    if value < minimum or value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"persisted {field_name} is outside its supported range")
    return value


def _persisted_text(value: Any, field_name: str, *, required: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"persisted {field_name} must use SQLite TEXT storage")
    if required and not value.strip():
        raise ValueError(f"persisted {field_name} must not be blank")
    return value


def _persisted_optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _persisted_text(value, field_name)


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


@dataclass(frozen=True)
class OutboxPageItem:
    """One outbox record paired with its durable pagination cursor."""

    position: int
    message: StoredOutboxMessage


@dataclass(frozen=True)
class OutboxAmbiguityPageItem:
    """One ambiguity record paired with a table-incarnation-local SQLite cursor."""

    rowid: int
    """Never persist this cursor across VACUUM or an ambiguity-table rebuild."""
    ambiguity: OutboxAmbiguity


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
                raise EventStoreJsonValueError(f"{path} contains a non-finite number")
            return value

        if value_type in (dict, _MAPPING_PROXY_TYPE):
            identity = id(value)
            if identity in state.active_container_ids:
                raise EventStoreJsonValueError(f"{path} contains a reference cycle")
            state.active_container_ids.add(identity)
            copied: Dict[str, Any] = {}
            try:
                for key, item in value.items():
                    if type(key) is not str:
                        raise EventStoreJsonTypeError(f"{path} keys must be strings")
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
                raise EventStoreJsonTypeError(f"{path} mapping traversal failed") from exc
            finally:
                state.active_container_ids.discard(identity)
            return copied

        if value_type in (list, tuple):
            identity = id(value)
            if identity in state.active_container_ids:
                raise EventStoreJsonValueError(f"{path} contains a reference cycle")
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
                raise EventStoreJsonTypeError(f"{path} sequence traversal failed") from exc
            finally:
                state.active_container_ids.discard(identity)

        raise EventStoreJsonTypeError(f"{path} contains unsupported type {value_type.__name__}")

    def _encode_json_object(self, value: Mapping[str, Any], field_name: str) -> str:
        copied = self._copy_json_value(
            value,
            path=field_name,
            depth=0,
            state=_JsonTraversalState(),
        )
        if type(copied) is not dict:
            raise EventStoreJsonTypeError(f"{field_name} must be a plain JSON object")
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

    def _decode_json_object(self, encoded: Any, field_name: str) -> Dict[str, Any]:
        """Decode one persisted JSON object without trusting SQLite affinity."""

        try:
            if type(encoded) is not str:
                raise TypeError(f"persisted {field_name} must use SQLite TEXT storage")
            if len(encoded.encode("utf-8")) > self._max_json_bytes:
                raise EventStoreJsonTooLargeError(
                    f"persisted {field_name} exceeds {self._max_json_bytes} encoded bytes"
                )
            decoded = json.loads(encoded, parse_constant=_reject_json_constant)
            copied = self._copy_json_value(
                decoded,
                path=f"persisted {field_name}",
                depth=0,
                state=_JsonTraversalState(),
            )
            if type(copied) is not dict:
                raise TypeError(f"persisted {field_name} must be a JSON object")
            return copied
        except (EventStoreJsonError, TypeError, ValueError, RecursionError) as exc:
            raise EventStoreIntegrityError(
                f"persisted {field_name} violates its JSON contract"
            ) from exc

    def _row_to_event(self, row: sqlite3.Row) -> StoredEvent:
        try:
            timestamp = _persisted_text(row["timestamp"], "event timestamp", required=True)
            self._normalize_timestamp(timestamp, "persisted event timestamp")
            event = DomainEvent(
                stream_id=_persisted_text(row["stream_id"], "stream_id", required=True),
                event_type=_persisted_text(row["event_type"], "event_type", required=True),
                payload=self._decode_json_object(row["payload_json"], "event payload"),
                actor_id=_persisted_text(row["actor_id"], "actor_id", required=True),
                event_id=_persisted_text(row["event_id"], "event_id", required=True),
                timestamp=timestamp,
                correlation_id=_persisted_optional_text(row["correlation_id"], "correlation_id"),
                causation_id=_persisted_optional_text(row["causation_id"], "causation_id"),
                idempotency_key=_persisted_optional_text(
                    row["idempotency_key"], "event idempotency_key"
                ),
            )
            return StoredEvent(
                event=event,
                sequence=_persisted_integer(row["sequence"], "event sequence", minimum=1),
                global_position=_persisted_integer(
                    row["global_position"], "event global_position", minimum=1
                ),
            )
        except EventStoreIntegrityError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError("persisted event row is malformed") from exc

    def _row_to_outbox(self, row: sqlite3.Row) -> StoredOutboxMessage:
        try:
            available_at = _persisted_text(
                row["available_at"], "outbox available_at", required=True
            )
            created_at = _persisted_text(row["created_at"], "outbox created_at", required=True)
            self._normalize_timestamp(available_at, "persisted outbox available_at")
            self._normalize_timestamp(created_at, "persisted outbox created_at")
            lease_expires_at = _persisted_optional_text(
                row["lease_expires_at"], "outbox lease_expires_at"
            )
            if lease_expires_at is not None:
                self._normalize_timestamp(lease_expires_at, "persisted outbox lease_expires_at")
            published_at = _persisted_optional_text(row["published_at"], "outbox published_at")
            if published_at is not None:
                self._normalize_timestamp(published_at, "persisted outbox published_at")
            message = OutboxMessage(
                destination=_persisted_text(
                    row["destination"], "outbox destination", required=True
                ),
                payload=self._decode_json_object(row["payload_json"], "outbox payload"),
                headers=self._decode_json_object(row["headers_json"], "outbox headers"),
                message_id=_persisted_text(row["message_id"], "outbox message_id", required=True),
                idempotency_key=_persisted_text(
                    row["idempotency_key"], "outbox idempotency_key", required=True
                ),
                available_at=available_at,
                created_at=created_at,
            )
            return StoredOutboxMessage(
                message=message,
                triggering_event_id=_persisted_text(
                    row["triggering_event_id"], "outbox triggering_event_id", required=True
                ),
                triggering_global_position=_persisted_integer(
                    row["triggering_global_position"],
                    "outbox triggering_global_position",
                    minimum=1,
                ),
                status=OutboxStatus(_persisted_text(row["status"], "outbox status", required=True)),
                attempt_count=_persisted_integer(row["attempt_count"], "outbox attempt_count"),
                lease_token=_persisted_optional_text(row["lease_token"], "outbox lease_token"),
                lease_expires_at=lease_expires_at,
                last_error=_persisted_optional_text(row["last_error"], "outbox last_error"),
                published_at=published_at,
            )
        except EventStoreIntegrityError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError("persisted outbox row is malformed") from exc

    def _row_to_outbox_page_item(self, row: sqlite3.Row) -> OutboxPageItem:
        try:
            position = _persisted_integer(
                row["outbox_position"],
                "outbox position",
                minimum=1,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError("persisted outbox cursor is malformed") from exc
        return OutboxPageItem(position=position, message=self._row_to_outbox(row))

    def _row_to_inbox(self, row: sqlite3.Row) -> InboxReceipt:
        try:
            received_at = _persisted_text(row["received_at"], "inbox received_at", required=True)
            self._normalize_timestamp(received_at, "persisted inbox received_at")
            return InboxReceipt(
                consumer_id=_persisted_text(row["consumer_id"], "inbox consumer_id", required=True),
                message_id=_persisted_text(row["message_id"], "inbox message_id", required=True),
                received_at=received_at,
                event_id=_persisted_text(row["event_id"], "inbox event_id", required=True),
                event_global_position=_persisted_integer(
                    row["event_global_position"], "inbox event_global_position", minimum=1
                ),
                result=self._decode_json_object(row["result_json"], "inbox result"),
            )
        except EventStoreIntegrityError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError("persisted inbox row is malformed") from exc

    def _row_to_ambiguity(self, row: sqlite3.Row) -> OutboxAmbiguity:
        try:
            marked_at = _persisted_text(
                row["marked_at"], "outbox ambiguity marked_at", required=True
            )
            self._normalize_timestamp(marked_at, "persisted outbox ambiguity marked_at")
            resolved_at = _persisted_optional_text(
                row["resolved_at"], "outbox ambiguity resolved_at"
            )
            if resolved_at is not None:
                self._normalize_timestamp(resolved_at, "persisted outbox ambiguity resolved_at")
            resolution = _persisted_optional_text(row["resolution"], "outbox ambiguity resolution")
            if resolution is not None and resolution not in {
                "published",
                "retry",
                "dead_letter",
            }:
                raise ValueError("persisted outbox ambiguity resolution is unsupported")
            reason_code = _persisted_text(
                row["reason_code"], "outbox ambiguity reason_code", required=True
            )
            if reason_code not in {
                "callback_timeout",
                "caller_cancelled",
                "ack_failed",
                "lease_expired_after_accept",
            }:
                raise ValueError("persisted outbox ambiguity reason_code is unsupported")
            lease_token_digest = _persisted_text(
                row["lease_token_digest"],
                "outbox ambiguity lease_token_digest",
                required=True,
            )
            if len(lease_token_digest) != 64 or any(
                character not in "0123456789abcdef" for character in lease_token_digest
            ):
                raise ValueError("persisted lease_token_digest is not canonical SHA-256")
            return OutboxAmbiguity(
                message_id=_persisted_text(
                    row["message_id"], "outbox ambiguity message_id", required=True
                ),
                lease_token_digest=lease_token_digest,
                reason_code=reason_code,
                attempt_count=_persisted_integer(
                    row["attempt_count"], "outbox ambiguity attempt_count", minimum=1
                ),
                marked_at=marked_at,
                resolution=resolution,
                resolved_at=resolved_at,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError("persisted outbox ambiguity row is malformed") from exc

    def _row_to_ambiguity_page_item(self, row: sqlite3.Row) -> OutboxAmbiguityPageItem:
        try:
            rowid = _persisted_integer(
                row["ambiguity_rowid"],
                "outbox ambiguity rowid",
                minimum=1,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError(
                "persisted outbox ambiguity cursor is malformed"
            ) from exc
        return OutboxAmbiguityPageItem(rowid=rowid, ambiguity=self._row_to_ambiguity(row))

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
    def _validate_page_cursor(value: int, field_name: str) -> int:
        if type(value) is not int:
            raise TypeError(f"{field_name} must be an integer")
        if value < 0:
            raise ValueError(f"{field_name} cannot be negative")
        if value > _MAX_SQLITE_INTEGER:
            raise ValueError(f"{field_name} exceeds SQLite's integer range")
        return value

    @staticmethod
    def _validate_page_limit(limit: int) -> int:
        if type(limit) is not int:
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        return limit

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

    def get_idempotent_event(
        self,
        stream_id: str,
        idempotency_key: str,
    ) -> Optional[StoredEvent]:
        """Return the exact event already admitted for one stream-local retry key."""

        if type(stream_id) is not str or type(idempotency_key) is not str:
            raise TypeError("stream_id and idempotency_key must be strings")
        if not stream_id.strip() or not idempotency_key.strip():
            raise ValueError("stream_id and idempotency_key are required")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND idempotency_key = ?",
                (stream_id, idempotency_key),
            ).fetchone()
            return None if row is None else self._row_to_event(row)

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

    def read_stream_page(
        self,
        stream_id: str,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> Tuple[StoredEvent, ...]:
        """Read one bounded stream page ordered by its exclusive sequence cursor."""

        if type(stream_id) is not str:
            raise TypeError("stream_id must be a string")
        if not stream_id.strip():
            raise ValueError("stream_id is required")
        cursor = self._validate_page_cursor(after_sequence, "after_sequence")
        page_limit = self._validate_page_limit(limit)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM events
                WHERE stream_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (stream_id, cursor, page_limit),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    def read_all(self, after_position: int = 0, limit: int = 1000) -> Tuple[StoredEvent, ...]:
        with self.stream_all_page(after_position=after_position, limit=limit) as events:
            return tuple(events)

    @contextmanager
    def stream_all_page(
        self,
        after_position: int = 0,
        limit: int = 1000,
    ) -> Iterator[Iterator[StoredEvent]]:
        """Decode one global-position page without holding a lock across a yielded row."""

        cursor = self._validate_page_cursor(after_position, "after_position")
        page_limit = self._validate_page_limit(limit)

        def events() -> Iterator[StoredEvent]:
            position = cursor
            for _index in range(page_limit):
                with self._lock:
                    row = self._connection.execute(
                        """
                        SELECT * FROM events
                        WHERE global_position > ?
                        ORDER BY global_position LIMIT 1
                        """,
                        (position,),
                    ).fetchone()
                if row is None:
                    return
                item = self._row_to_event(row)
                position = item.global_position
                yield item

        yield events()

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

    def read_outbox_ambiguities_page(
        self,
        after_rowid: int = 0,
        open_only: bool = True,
        limit: int = 1_000,
    ) -> Tuple[OutboxAmbiguityPageItem, ...]:
        """Read one bounded page using a table-incarnation-local SQLite cursor.

        ``after_rowid`` must not be persisted across VACUUM or an ambiguity-table
        rebuild because the current schema has no durable integer position.
        """

        cursor = self._validate_page_cursor(after_rowid, "after_rowid")
        page_limit = self._validate_page_limit(limit)
        if type(open_only) is not bool:
            raise TypeError("open_only must be a boolean")
        with self._lock:
            if open_only:
                rows = self._connection.execute(
                    """
                    SELECT rowid AS ambiguity_rowid, * FROM outbox_ambiguities
                    WHERE rowid > ? AND resolved_at IS NULL
                    ORDER BY rowid LIMIT ?
                    """,
                    (cursor, page_limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT rowid AS ambiguity_rowid, * FROM outbox_ambiguities
                    WHERE rowid > ? ORDER BY rowid LIMIT ?
                    """,
                    (cursor, page_limit),
                ).fetchall()
            return tuple(self._row_to_ambiguity_page_item(row) for row in rows)

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

    def read_outbox_page(
        self,
        after_position: int = 0,
        status: Optional[OutboxStatus] = None,
        limit: int = 1_000,
    ) -> Tuple[OutboxPageItem, ...]:
        """Read one bounded outbox page after an exclusive durable position."""

        cursor = self._validate_page_cursor(after_position, "after_position")
        page_limit = self._validate_page_limit(limit)
        if status is not None and type(status) is not OutboxStatus:
            raise TypeError("status must be an OutboxStatus or None")
        with self._lock:
            if status is None:
                rows = self._connection.execute(
                    """
                    SELECT * FROM outbox WHERE outbox_position > ?
                    ORDER BY outbox_position LIMIT ?
                    """,
                    (cursor, page_limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM outbox
                    WHERE outbox_position > ? AND status = ?
                    ORDER BY outbox_position LIMIT ?
                    """,
                    (cursor, status.value, page_limit),
                ).fetchall()
            return tuple(self._row_to_outbox_page_item(row) for row in rows)

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
            try:
                return _persisted_integer(
                    row["sequence"], "snapshot sequence"
                ), self._decode_json_object(row["state_json"], "snapshot state")
            except EventStoreIntegrityError:
                raise
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise EventStoreIntegrityError("persisted snapshot row is malformed") from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteEventStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
