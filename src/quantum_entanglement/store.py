# ruff: noqa: UP006, UP031, UP035, UP037, UP045
"""SQLite append-only event store with optimistic concurrency and idempotency."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import traceback as traceback_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NoReturn,
    Optional,
    SupportsIndex,
    Tuple,
    TypeVar,
    cast,
)

from . import process_identity as _process_identity
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


class EventStoreLifecycleError(RuntimeError):
    """Raised when an event-store instance cannot safely serve lifecycle work."""

    code = "event_store_process_mismatch"

    def __init__(self) -> None:
        super().__init__(self.code)


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
_EVENT_STORE_PROCESS_SIGNAL_TOKEN = object()
_EVENT_STORE_CHILD_CONNECTION_QUARANTINE: List[sqlite3.Connection] = []


class _EventStoreProcessMismatchSignal(BaseException):
    """Module-private ownership signal that must never escape a public boundary."""

    __slots__ = ("token",)

    def __init__(self, token: object) -> None:
        super().__init__("event store process mismatch")
        self.token = token


def _event_store_process_mismatch_signal() -> BaseException:
    return _EventStoreProcessMismatchSignal(_EVENT_STORE_PROCESS_SIGNAL_TOKEN)


def _trusted_event_store_process_signal(error: BaseException) -> bool:
    """Verify exact construction identity and the foundation guard's tail frames."""

    if type(error) is not _EventStoreProcessMismatchSignal:
        return False
    try:
        token = object.__getattribute__(error, "token")
    except AttributeError:
        return False
    if token is not _EVENT_STORE_PROCESS_SIGNAL_TOKEN:
        return False
    traceback_cursor = error.__traceback__
    codes = []
    while traceback_cursor is not None:
        codes.append(traceback_cursor.tb_frame.f_code)
        traceback_cursor = traceback_cursor.tb_next
    return len(codes) >= 3 and codes[-3:] == [
        SQLiteEventStore._require_current_process.__code__,
        _process_identity.require_current_process.__code__,
        _process_identity._raise_process_mismatch.__code__,
    ]


def _consume_event_store_process_signal(error: BaseException) -> bool:
    """Detach one trusted internal signal and clear every completed internal frame."""

    if not _trusted_event_store_process_signal(error):
        return False
    error_traceback = error.__traceback__
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    if error_traceback is not None:
        traceback_module.clear_frames(error_traceback)
    return True


def _raise_event_store_process_mismatch() -> NoReturn:
    """Create the one stable public error outside any internal exception handler."""

    try:
        raise EventStoreLifecycleError() from None
    except EventStoreLifecycleError as public_error:
        if type(public_error) is EventStoreLifecycleError:
            public_error.__context__ = None
        raise


def _quarantine_inherited_event_store_connection(connection: sqlite3.Connection) -> None:
    """Retain a child-inherited wrapper without close, rollback, or finalization."""

    _EVENT_STORE_CHILD_CONNECTION_QUARANTINE.append(connection)


_Method = TypeVar("_Method", bound=Callable[..., Any])


def _bind_event_store_process(method: _Method) -> _Method:
    """Reject a fork-inherited store before reading caller inputs or dependencies."""

    @wraps(method)
    def process_bound(*args: Any, **kwargs: Any) -> Any:
        store = args[0]
        try:
            store._require_current_process()
            return method(*args, **kwargs)
        except _EventStoreProcessMismatchSignal as error:
            trusted = _consume_event_store_process_signal(error)
            if not trusted:
                raise
        del args, kwargs, store
        _raise_event_store_process_mismatch()

    return cast(_Method, process_bound)


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


def _caller_text(value: object, field_name: str, *, required: bool = False) -> str:
    """Copy only an exact built-in string into a SQLite-safe caller snapshot."""

    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if required and not value.strip():
        raise ValueError(f"{field_name} is required")
    return value


def _caller_optional_text(value: object, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _caller_text(value, field_name)


def _caller_sqlite_integer(
    value: object,
    field_name: str,
    *,
    minimum: Optional[int] = None,
) -> int:
    """Copy an exact integer that SQLite can bind without caller adaptation."""

    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        if minimum == 0:
            raise ValueError(f"{field_name} cannot be negative")
        raise ValueError(f"{field_name} must be at least {minimum}")
    if not -_MAX_SQLITE_INTEGER - 1 <= value <= _MAX_SQLITE_INTEGER:
        raise ValueError(f"{field_name} exceeds SQLite's integer range")
    return value


def _caller_number(value: object, field_name: str, *, positive: bool = False) -> float:
    """Copy one finite exact built-in number without invoking caller comparison hooks."""

    if type(value) is int:
        snapshot = float(value)
    elif type(value) is float:
        snapshot = value
    else:
        raise TypeError(f"{field_name} must be a number")
    if not math.isfinite(snapshot):
        raise ValueError(f"{field_name} must be finite")
    if positive and snapshot <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return snapshot


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


@dataclass(frozen=True)
class _JsonObjectSnapshot:
    value: Dict[str, Any]
    encoded: str


@dataclass(frozen=True)
class _EventWriteSnapshot:
    event: DomainEvent
    payload_json: str


@dataclass(frozen=True)
class _OutboxWriteSnapshot:
    message: OutboxMessage
    payload_json: str
    headers_json: str


class _EventPageIterator:
    """One process-bound event page whose ownership is checked on every resume."""

    __slots__ = ("_limit", "_position", "_store")

    def __init__(self, store: "SQLiteEventStore", position: int, limit: int) -> None:
        self._store = store
        self._position = position
        self._limit = limit

    def __iter__(self) -> "_EventPageIterator":
        store = self._store
        try:
            store._require_current_process()
            return self
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
        del self, store
        _raise_event_store_process_mismatch()

    def __next__(self) -> StoredEvent:
        try:
            return self._next_internal()
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
        del self
        _raise_event_store_process_mismatch()

    def _next_internal(self) -> StoredEvent:
        store = self._store
        store._require_current_process()
        if self._limit <= 0:
            raise StopIteration
        position = self._position
        with store._locked():
            query = store._connection.execute(
                """
                SELECT * FROM events
                WHERE global_position > ?
                ORDER BY global_position LIMIT 1
                """,
                (position,),
            )
            try:
                row = query.fetchone()
            finally:
                query.close()
        if row is None:
            self._limit = 0
            raise StopIteration
        item = store._row_to_event(row)
        store._require_current_process()
        self._position = item.global_position
        self._limit -= 1
        return item

    def __copy__(self) -> NoReturn:
        raise TypeError("event store iterators cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("event store iterators cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("event store iterators cannot be serialized")


class _EventPageContext:
    """Context boundary that revalidates ownership independently of method call time."""

    __slots__ = ("_entered", "_iterator", "_limit", "_position", "_store")

    def __init__(self, store: "SQLiteEventStore", position: int, limit: int) -> None:
        self._store = store
        self._position = position
        self._limit = limit
        self._entered = False
        self._iterator: Optional[_EventPageIterator] = None

    def __enter__(self) -> Iterator[StoredEvent]:
        store = self._store
        try:
            store._require_current_process()
            if self._entered:
                raise RuntimeError("event page context cannot be re-entered")
            iterator = _EventPageIterator(store, self._position, self._limit)
            self._iterator = iterator
            self._entered = True
            return iterator
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
        del self, store
        _raise_event_store_process_mismatch()

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        store = self._store
        try:
            store._require_current_process()
            return None
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
        del self, store, exc_type, exc, traceback
        _raise_event_store_process_mismatch()

    def __copy__(self) -> NoReturn:
        raise TypeError("event store contexts cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("event store contexts cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("event store contexts cannot be serialized")


class SQLiteEventStore:
    """Small durable event log suitable for the kernel and local-first clients."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        clock: Callable[[], str] = utc_now,
        max_json_bytes: int = 1024 * 1024,
    ) -> None:
        self._process_owner = _process_identity.capture_process_owner()
        self._require_current_process()
        if type(path) is not str:
            raise TypeError("path must be a string")
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
            self._require_current_process()
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._require_current_process()
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clock = clock
        self._max_json_bytes = max_json_bytes
        process_mismatch = False
        try:
            self._initialize()
        except _EventStoreProcessMismatchSignal as error:
            trusted = _consume_event_store_process_signal(error)
            if not trusted:
                raise
            _quarantine_inherited_event_store_connection(self._connection)
            process_mismatch = True
        except BaseException:
            if self._process_is_current():
                self._connection.close()
                raise
            _quarantine_inherited_event_store_connection(self._connection)
            process_mismatch = True
        if process_mismatch:
            del self, path, clock, max_json_bytes
            _raise_event_store_process_mismatch()

    def _require_current_process(self) -> None:
        """Emit the exact private signal before any inherited dependency access."""

        _process_identity.require_current_process(
            self._process_owner,
            _event_store_process_mismatch_signal,
        )

    def _process_is_current(self) -> bool:
        """Check cleanup authority without allowing the private signal to escape."""

        try:
            self._require_current_process()
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
            return False
        return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire and release the RLock only while this process owns the store."""

        self._require_current_process()
        lock = self._lock
        lock.acquire()
        try:
            self._require_current_process()
            yield
            self._require_current_process()
        finally:
            if self._process_is_current():
                lock.release()

    def _initialize(self) -> None:
        with self._locked():
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
            self._require_current_process()
            apply_sqlite_migrations(
                self._connection,
                clock=self._now,
                _process_guard=self._require_current_process,
            )
            self._require_current_process()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._require_current_process()
        lock = self._lock
        lock.acquire()
        try:
            self._require_current_process()
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            self._require_current_process()
            try:
                yield connection
            except BaseException:
                if not self._process_is_current():
                    raise
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                    self._require_current_process()
                raise
            else:
                self._require_current_process()
                try:
                    connection.execute("COMMIT")
                    self._require_current_process()
                except BaseException:
                    if not self._process_is_current():
                        raise
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                        self._require_current_process()
                    raise
        finally:
            if self._process_is_current():
                lock.release()

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

    def _snapshot_json_object(
        self,
        value: Mapping[str, Any],
        field_name: str,
    ) -> _JsonObjectSnapshot:
        encoded = self._encode_json_object(value, field_name)
        decoded = json.loads(encoded, parse_constant=_reject_json_constant)
        if type(decoded) is not dict:
            raise EventStoreJsonTypeError(f"{field_name} must be a plain JSON object")
        return _JsonObjectSnapshot(decoded, encoded)

    def _snapshot_event(self, event: DomainEvent) -> _EventWriteSnapshot:
        if type(event) is not DomainEvent:
            raise TypeError("event must be an exact DomainEvent")
        stream_id = _caller_text(
            object.__getattribute__(event, "stream_id"),
            "event stream_id",
            required=True,
        )
        event_type = _caller_text(
            object.__getattribute__(event, "event_type"),
            "event event_type",
            required=True,
        )
        actor_id = _caller_text(
            object.__getattribute__(event, "actor_id"),
            "event actor_id",
            required=True,
        )
        event_id = _caller_text(object.__getattribute__(event, "event_id"), "event event_id")
        timestamp = _caller_text(
            object.__getattribute__(event, "timestamp"),
            "event timestamp",
        )
        correlation_id = _caller_optional_text(
            object.__getattribute__(event, "correlation_id"),
            "event correlation_id",
        )
        causation_id = _caller_optional_text(
            object.__getattribute__(event, "causation_id"),
            "event causation_id",
        )
        idempotency_key = _caller_optional_text(
            object.__getattribute__(event, "idempotency_key"),
            "event idempotency_key",
        )
        payload = self._snapshot_json_object(
            object.__getattribute__(event, "payload"),
            "event payload",
        )
        snapshot_event = DomainEvent(
            stream_id=stream_id,
            event_type=event_type,
            payload=payload.value,
            actor_id=actor_id,
            event_id=event_id,
            timestamp=timestamp,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        return _EventWriteSnapshot(snapshot_event, payload.encoded)

    def _snapshot_outbox_message(self, message: OutboxMessage) -> _OutboxWriteSnapshot:
        if type(message) is not OutboxMessage:
            raise TypeError("message must be an exact OutboxMessage")
        destination = _caller_text(
            object.__getattribute__(message, "destination"),
            "outbox destination",
            required=True,
        )
        message_id = _caller_text(
            object.__getattribute__(message, "message_id"),
            "outbox message_id",
            required=True,
        )
        idempotency_key = _caller_text(
            object.__getattribute__(message, "idempotency_key"),
            "outbox idempotency_key",
            required=True,
        )
        available_at = _caller_text(
            object.__getattribute__(message, "available_at"),
            "outbox available_at",
        )
        created_at = _caller_text(
            object.__getattribute__(message, "created_at"),
            "outbox created_at",
        )
        payload = self._snapshot_json_object(
            object.__getattribute__(message, "payload"),
            "outbox payload",
        )
        headers = self._snapshot_json_object(
            object.__getattribute__(message, "headers"),
            "outbox headers",
        )
        snapshot_message = OutboxMessage(
            destination=destination,
            payload=payload.value,
            headers=headers.value,
            message_id=message_id,
            idempotency_key=idempotency_key,
            available_at=available_at,
            created_at=created_at,
        )
        return _OutboxWriteSnapshot(snapshot_message, payload.encoded, headers.encoded)

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
        now_snapshot = _caller_text(now, "now")
        lease_seconds_snapshot = _caller_number(
            lease_seconds,
            "lease_seconds",
            positive=True,
        )
        parsed = datetime.fromisoformat(now_snapshot.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("now must include a timezone")
        deadline = parsed.astimezone(timezone.utc) + timedelta(seconds=lease_seconds_snapshot)
        return deadline.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_timestamp(value: str, field_name: str) -> str:
        value_snapshot = _caller_text(value, field_name)
        try:
            parsed = datetime.fromisoformat(value_snapshot.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _now(self) -> str:
        """Read the store-owned clock after the write transaction is acquired."""

        self._require_current_process()
        value = self._clock()
        self._require_current_process()
        normalized = self._normalize_timestamp(value, "clock")
        self._require_current_process()
        return normalized

    @staticmethod
    def _validate_page_cursor(value: int, field_name: str) -> int:
        return _caller_sqlite_integer(value, field_name, minimum=0)

    @staticmethod
    def _validate_page_limit(limit: int) -> int:
        if type(limit) is not int:
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        return limit

    @staticmethod
    def _lease_token_digest(lease_token: str) -> str:
        if type(lease_token) is not str or not lease_token:
            raise ValueError("lease_token is required")
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        event_snapshot: _EventWriteSnapshot,
        expected_version: Optional[int],
        expected_global_position: Optional[int] = None,
    ) -> Tuple[StoredEvent, bool]:
        """Append inside an existing transaction and report whether a row was inserted."""

        event = event_snapshot.event
        if event.idempotency_key is not None:
            existing = connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND idempotency_key = ?",
                (event.stream_id, event.idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._row_to_event(existing), False

        if expected_global_position is not None:
            global_row = connection.execute(
                "SELECT COALESCE(MAX(global_position), 0) AS position FROM events"
            ).fetchone()
            current_global_position = int(global_row["position"])
            if expected_global_position != current_global_position:
                raise ConcurrencyError("global event position changed during admission")

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
                event_snapshot.payload_json,
                event.correlation_id,
                event.causation_id,
                event.idempotency_key,
            ),
        )
        global_position = cursor.lastrowid
        if global_position is None:
            raise RuntimeError("SQLite did not return an event global position")
        return StoredEvent(event, sequence, int(global_position)), True

    @_bind_event_store_process
    def stream_version(self, stream_id: str) -> int:
        stream_id_snapshot = _caller_text(stream_id, "stream_id")
        with self._locked():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id_snapshot,),
            ).fetchone()
            return int(row["version"])

    @_bind_event_store_process
    def get_idempotent_event(
        self,
        stream_id: str,
        idempotency_key: str,
    ) -> Optional[StoredEvent]:
        """Return the exact event already admitted for one stream-local retry key."""

        stream_id_snapshot = _caller_text(stream_id, "stream_id", required=True)
        idempotency_key_snapshot = _caller_text(
            idempotency_key,
            "idempotency_key",
            required=True,
        )
        with self._locked():
            row = self._connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND idempotency_key = ?",
                (stream_id_snapshot, idempotency_key_snapshot),
            ).fetchone()
            return None if row is None else self._row_to_event(row)

    @_bind_event_store_process
    def append(
        self,
        event: DomainEvent,
        expected_version: Optional[int] = None,
        *,
        expected_global_position: Optional[int] = None,
    ) -> StoredEvent:
        """Append one event, returning the existing record for an idempotent retry."""

        event_snapshot = self._snapshot_event(event)
        expected_version_snapshot = (
            None
            if expected_version is None
            else _caller_sqlite_integer(expected_version, "expected_version")
        )
        if expected_global_position is not None:
            expected_global_position_snapshot = self._validate_page_cursor(
                expected_global_position,
                "expected_global_position",
            )
        else:
            expected_global_position_snapshot = None
        self._require_current_process()
        with self._transaction() as connection:
            stored, _inserted = self._append_in_transaction(
                connection,
                event_snapshot,
                expected_version_snapshot,
                expected_global_position_snapshot,
            )
            return stored

    @_bind_event_store_process
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

        raw_batch = tuple(messages)
        self._require_current_process()
        event_snapshot = self._snapshot_event(event)
        batch = tuple(self._snapshot_outbox_message(message) for message in raw_batch)
        expected_version_snapshot = (
            None
            if expected_version is None
            else _caller_sqlite_integer(expected_version, "expected_version")
        )
        self._require_current_process()
        with self._transaction() as connection:
            stored, inserted = self._append_in_transaction(
                connection,
                event_snapshot,
                expected_version_snapshot,
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
                        item.message.message_id,
                        item.message.destination,
                        item.message.idempotency_key,
                        dict(item.message.payload),
                        dict(item.message.headers),
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
                item = message.message
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
                        item.message_id,
                        item.destination,
                        message.payload_json,
                        message.headers_json,
                        item.idempotency_key,
                        stored.event.event_id,
                        stored.global_position,
                        OutboxStatus.PENDING.value,
                        item.available_at,
                        item.created_at,
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

    @_bind_event_store_process
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

        consumer_id_snapshot = _caller_text(consumer_id, "consumer_id", required=True)
        message_id_snapshot = _caller_text(message_id, "message_id", required=True)
        event_snapshot = self._snapshot_event(event)
        result_snapshot = self._snapshot_json_object(
            {} if result is None else result,
            "inbox result",
        )
        if received_at is None:
            received_at_snapshot = utc_now()
            self._require_current_process()
            received_at_snapshot = _caller_text(received_at_snapshot, "received_at")
        else:
            received_at_snapshot = _caller_text(received_at, "received_at")
        expected_version_snapshot = (
            None
            if expected_version is None
            else _caller_sqlite_integer(expected_version, "expected_version")
        )
        self._require_current_process()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM inbox_receipts
                WHERE consumer_id = ? AND message_id = ?
                """,
                (consumer_id_snapshot, message_id_snapshot),
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
                connection,
                event_snapshot,
                expected_version_snapshot,
            )
            receipt = InboxReceipt(
                consumer_id=consumer_id_snapshot,
                message_id=message_id_snapshot,
                received_at=received_at_snapshot,
                event_id=stored.event.event_id,
                event_global_position=stored.global_position,
                result=result_snapshot.value,
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
                    result_snapshot.encoded,
                ),
            )
            return InboxAppendResult(stored, receipt, False)

    @_bind_event_store_process
    def append_many(
        self,
        stream_id: str,
        events: Iterable[DomainEvent],
        expected_version: Optional[int] = None,
    ) -> Tuple[StoredEvent, ...]:
        """Atomically append a batch to one stream."""

        raw_batch = tuple(events)
        self._require_current_process()
        stream_id_snapshot = _caller_text(stream_id, "stream_id")
        batch = tuple(self._snapshot_event(event) for event in raw_batch)
        if any(item.event.stream_id != stream_id_snapshot for item in batch):
            raise ValueError("all batch events must use the declared stream_id")
        if not batch:
            return ()
        expected_version_snapshot = (
            None
            if expected_version is None
            else _caller_sqlite_integer(expected_version, "expected_version")
        )
        self._require_current_process()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id_snapshot,),
            ).fetchone()
            current_version = int(row["version"])
            if (
                expected_version_snapshot is not None
                and expected_version_snapshot != current_version
            ):
                raise ConcurrencyError(
                    "stream %s expected version %d but is %d"
                    % (stream_id_snapshot, expected_version_snapshot, current_version)
                )
            stored: List[StoredEvent] = []
            for offset, event_snapshot in enumerate(batch, start=1):
                event = event_snapshot.event
                sequence = current_version + offset
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        stream_id, sequence, event_id, event_type, actor_id, timestamp,
                        payload_json, correlation_id, causation_id, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stream_id_snapshot,
                        sequence,
                        event.event_id,
                        event.event_type,
                        event.actor_id,
                        event.timestamp,
                        event_snapshot.payload_json,
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

    @_bind_event_store_process
    def read_stream(self, stream_id: str, after_sequence: int = 0) -> Tuple[StoredEvent, ...]:
        stream_id_snapshot = _caller_text(stream_id, "stream_id")
        sequence_snapshot = _caller_sqlite_integer(after_sequence, "after_sequence")
        with self._locked():
            rows = self._connection.execute(
                "SELECT * FROM events WHERE stream_id = ? AND sequence > ? ORDER BY sequence",
                (stream_id_snapshot, sequence_snapshot),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    @_bind_event_store_process
    def read_stream_page(
        self,
        stream_id: str,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> Tuple[StoredEvent, ...]:
        """Read one bounded stream page ordered by its exclusive sequence cursor."""

        stream_id_snapshot = _caller_text(stream_id, "stream_id", required=True)
        cursor = self._validate_page_cursor(after_sequence, "after_sequence")
        page_limit = self._validate_page_limit(limit)
        with self._locked():
            rows = self._connection.execute(
                """
                SELECT * FROM events
                WHERE stream_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (stream_id_snapshot, cursor, page_limit),
            ).fetchall()
            return tuple(self._row_to_event(row) for row in rows)

    @_bind_event_store_process
    def read_all(self, after_position: int = 0, limit: int = 1000) -> Tuple[StoredEvent, ...]:
        with self.stream_all_page(after_position=after_position, limit=limit) as events:
            return tuple(events)

    @_bind_event_store_process
    def stream_all_page(
        self,
        after_position: int = 0,
        limit: int = 1000,
    ) -> ContextManager[Iterator[StoredEvent]]:
        """Decode one global-position page without holding a lock across a yielded row."""

        cursor = self._validate_page_cursor(after_position, "after_position")
        page_limit = self._validate_page_limit(limit)
        self._require_current_process()
        return _EventPageContext(self, cursor, page_limit)

    @_bind_event_store_process
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
        worker_id_snapshot = _caller_text(worker_id, "worker_id", required=True)
        limit_snapshot = _caller_sqlite_integer(limit, "limit", minimum=1)
        lease_seconds_snapshot = _caller_number(
            lease_seconds,
            "lease_seconds",
            positive=True,
        )
        self._require_current_process()
        with self._transaction() as connection:
            claimed_at = self._now()
            lease_expires_at = self._lease_deadline(claimed_at, lease_seconds_snapshot)
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
                    limit_snapshot,
                ),
            ).fetchall()
            claimed: List[StoredOutboxMessage] = []
            for row in rows:
                message_id_snapshot = _persisted_text(
                    row["message_id"],
                    "outbox message_id",
                    required=True,
                )
                lease_id = new_id("lease")
                self._require_current_process()
                lease_id_snapshot = _caller_text(lease_id, "lease id", required=True)
                lease_token = "%s:%s" % (worker_id_snapshot, lease_id_snapshot)
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
                        message_id_snapshot,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM outbox WHERE message_id = ?",
                    (message_id_snapshot,),
                ).fetchone()
                claimed.append(self._row_to_outbox(updated))
            return tuple(claimed)

    @_bind_event_store_process
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
        message_id_snapshot = _caller_text(message_id, "message_id")
        lease_token_snapshot = _caller_text(lease_token, "lease_token")
        published_at_snapshot = _caller_optional_text(published_at, "published_at")
        self._require_current_process()
        with self._transaction() as connection:
            checked_at = self._now()
            acknowledged_at = self._normalize_timestamp(
                checked_at if published_at_snapshot is None else published_at_snapshot,
                "published_at",
            )
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
                    message_id_snapshot,
                    OutboxStatus.IN_FLIGHT.value,
                    lease_token_snapshot,
                    checked_at,
                ),
            )
            return cursor.rowcount == 1

    @_bind_event_store_process
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
        message_id_snapshot = _caller_text(message_id, "message_id")
        lease_token_snapshot = _caller_text(lease_token, "lease_token")
        error_snapshot = _caller_text(error, "error")
        retry_at_snapshot = _caller_optional_text(retry_at, "retry_at")
        if type(dead_letter) is not bool:
            raise TypeError("dead_letter must be a boolean")
        status_value = OutboxStatus.DEAD_LETTER.value if dead_letter else OutboxStatus.PENDING.value
        self._require_current_process()
        with self._transaction() as connection:
            rejected_at = self._now()
            available_at = self._normalize_timestamp(
                rejected_at if retry_at_snapshot is None else retry_at_snapshot,
                "retry_at",
            )
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
                    status_value,
                    available_at,
                    error_snapshot,
                    message_id_snapshot,
                    OutboxStatus.IN_FLIGHT.value,
                    lease_token_snapshot,
                    rejected_at,
                ),
            )
            return cursor.rowcount == 1

    @_bind_event_store_process
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
        message_id_snapshot = _caller_text(message_id, "message_id")
        lease_token_snapshot = _caller_text(lease_token, "lease_token")
        reason_code_snapshot = _caller_text(reason_code, "reason_code")
        if reason_code_snapshot not in allowed_reasons:
            raise ValueError("unsupported outbox ambiguity reason")
        lease_token_digest = self._lease_token_digest(lease_token_snapshot)
        marked_at_snapshot = _caller_optional_text(marked_at, "marked_at")
        if marked_at_snapshot is None:
            marked_at_snapshot = utc_now()
            self._require_current_process()
            marked_at_snapshot = _caller_text(marked_at_snapshot, "marked_at")
        recorded_at = self._normalize_timestamp(marked_at_snapshot, "marked_at")
        self._require_current_process()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT status, lease_token, attempt_count FROM outbox
                WHERE message_id = ?
                """,
                (message_id_snapshot,),
            ).fetchone()
            if (
                row is None
                or row["status"] != OutboxStatus.IN_FLIGHT.value
                or row["lease_token"] != lease_token_snapshot
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
                    message_id_snapshot,
                    lease_token_digest,
                    reason_code_snapshot,
                    _persisted_integer(row["attempt_count"], "outbox attempt_count"),
                    recorded_at,
                ),
            )
            return True

    @_bind_event_store_process
    def read_outbox_ambiguities(self, *, open_only: bool = True) -> Tuple[OutboxAmbiguity, ...]:
        """Read durable reconciliation work in deterministic insertion order."""

        if type(open_only) is not bool:
            raise TypeError("open_only must be a boolean")
        open_only_snapshot = open_only
        with self._locked():
            if open_only_snapshot:
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

    @_bind_event_store_process
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
        open_only_snapshot = open_only
        with self._locked():
            if open_only_snapshot:
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

    @_bind_event_store_process
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

        message_id_snapshot = _caller_text(message_id, "message_id")
        lease_token_digest_snapshot = _caller_text(
            lease_token_digest,
            "lease_token_digest",
        )
        resolution_snapshot = _caller_text(resolution, "resolution")
        if resolution_snapshot not in {"published", "retry", "dead_letter"}:
            raise ValueError("unsupported outbox ambiguity resolution")
        if len(lease_token_digest_snapshot) != 64 or any(
            character not in "0123456789abcdef" for character in lease_token_digest_snapshot
        ):
            raise ValueError("lease_token_digest must be a lowercase SHA-256 digest")
        resolved_at_snapshot = _caller_optional_text(resolved_at, "resolved_at")
        if resolved_at_snapshot is None:
            resolved_at_snapshot = utc_now()
            self._require_current_process()
            resolved_at_snapshot = _caller_text(resolved_at_snapshot, "resolved_at")
        retry_at_snapshot = _caller_optional_text(retry_at, "retry_at")
        decided_at = self._normalize_timestamp(resolved_at_snapshot, "resolved_at")
        available_at = self._normalize_timestamp(
            decided_at if retry_at_snapshot is None else retry_at_snapshot,
            "retry_at",
        )
        self._require_current_process()
        with self._transaction() as connection:
            ambiguity = connection.execute(
                """
                SELECT 1 FROM outbox_ambiguities
                WHERE message_id = ? AND lease_token_digest = ? AND resolved_at IS NULL
                """,
                (message_id_snapshot, lease_token_digest_snapshot),
            ).fetchone()
            outbox = connection.execute(
                "SELECT status, lease_token FROM outbox WHERE message_id = ?",
                (message_id_snapshot,),
            ).fetchone()
            if (
                ambiguity is None
                or outbox is None
                or outbox["status"] != OutboxStatus.IN_FLIGHT.value
                or self._lease_token_digest(outbox["lease_token"]) != lease_token_digest_snapshot
            ):
                return False
            durable_lease_token = _persisted_text(
                outbox["lease_token"],
                "outbox lease_token",
                required=True,
            )
            if resolution_snapshot == "published":
                connection.execute(
                    """
                    UPDATE outbox SET status = ?, lease_token = NULL, lease_expires_at = NULL,
                        published_at = ?, last_error = NULL
                    WHERE message_id = ? AND status = ? AND lease_token = ?
                    """,
                    (
                        OutboxStatus.PUBLISHED.value,
                        decided_at,
                        message_id_snapshot,
                        OutboxStatus.IN_FLIGHT.value,
                        durable_lease_token,
                    ),
                )
            else:
                target = (
                    OutboxStatus.PENDING
                    if resolution_snapshot == "retry"
                    else OutboxStatus.DEAD_LETTER
                )
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
                        f"operator_reconciled:{resolution_snapshot}",
                        message_id_snapshot,
                        OutboxStatus.IN_FLIGHT.value,
                        durable_lease_token,
                    ),
                )
            connection.execute(
                """
                UPDATE outbox_ambiguities SET resolution = ?, resolved_at = ?
                WHERE message_id = ? AND lease_token_digest = ? AND resolved_at IS NULL
                """,
                (
                    resolution_snapshot,
                    decided_at,
                    message_id_snapshot,
                    lease_token_digest_snapshot,
                ),
            )
            return True

    @_bind_event_store_process
    def get_outbox(self, message_id: str) -> Optional[StoredOutboxMessage]:
        message_id_snapshot = _caller_text(message_id, "message_id")
        with self._locked():
            row = self._connection.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id_snapshot,)
            ).fetchone()
            return None if row is None else self._row_to_outbox(row)

    @_bind_event_store_process
    def read_outbox(self, status: Optional[OutboxStatus] = None) -> Tuple[StoredOutboxMessage, ...]:
        if status is not None and type(status) is not OutboxStatus:
            raise TypeError("status must be an OutboxStatus or None")
        status_value = None if status is None else _caller_text(status.value, "status")
        with self._locked():
            if status_value is None:
                rows = self._connection.execute(
                    "SELECT * FROM outbox ORDER BY outbox_position"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM outbox WHERE status = ? ORDER BY outbox_position",
                    (status_value,),
                ).fetchall()
            return tuple(self._row_to_outbox(row) for row in rows)

    @_bind_event_store_process
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
        status_value = None if status is None else _caller_text(status.value, "status")
        with self._locked():
            if status_value is None:
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
                    (cursor, status_value, page_limit),
                ).fetchall()
            return tuple(self._row_to_outbox_page_item(row) for row in rows)

    @_bind_event_store_process
    def get_inbox_receipt(self, consumer_id: str, message_id: str) -> Optional[InboxReceipt]:
        consumer_id_snapshot = _caller_text(consumer_id, "consumer_id")
        message_id_snapshot = _caller_text(message_id, "message_id")
        with self._locked():
            row = self._connection.execute(
                """
                SELECT * FROM inbox_receipts
                WHERE consumer_id = ? AND message_id = ?
                """,
                (consumer_id_snapshot, message_id_snapshot),
            ).fetchone()
            return None if row is None else self._row_to_inbox(row)

    @_bind_event_store_process
    def save_snapshot(
        self, stream_id: str, sequence: int, state: Dict[str, object], at: str
    ) -> None:
        stream_id_snapshot = _caller_text(stream_id, "stream_id")
        sequence_snapshot = _caller_sqlite_integer(sequence, "sequence")
        state_snapshot = self._snapshot_json_object(state, "snapshot state")
        at_snapshot = _caller_text(at, "at")
        self._require_current_process()
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
                    stream_id_snapshot,
                    sequence_snapshot,
                    state_snapshot.encoded,
                    at_snapshot,
                ),
            )

    @_bind_event_store_process
    def load_snapshot(self, stream_id: str) -> Optional[Tuple[int, Dict[str, object]]]:
        stream_id_snapshot = _caller_text(stream_id, "stream_id")
        with self._locked():
            row = self._connection.execute(
                "SELECT sequence, state_json FROM snapshots WHERE stream_id = ?",
                (stream_id_snapshot,),
            ).fetchone()
            if row is None:
                return None
            try:
                persisted_snapshot = (row["sequence"], row["state_json"])
            except (IndexError, KeyError) as exc:
                raise EventStoreIntegrityError("persisted snapshot row is malformed") from exc
        try:
            return _persisted_integer(
                persisted_snapshot[0], "snapshot sequence"
            ), self._decode_json_object(persisted_snapshot[1], "snapshot state")
        except EventStoreIntegrityError:
            raise
        except (TypeError, ValueError) as exc:
            raise EventStoreIntegrityError("persisted snapshot row is malformed") from exc

    @_bind_event_store_process
    def close(self) -> None:
        with self._locked():
            self._connection.close()

    @_bind_event_store_process
    def __enter__(self) -> "SQLiteEventStore":
        return self

    @_bind_event_store_process
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @_bind_event_store_process
    def __copy__(self) -> NoReturn:
        raise TypeError("event stores cannot be copied")

    @_bind_event_store_process
    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("event stores cannot be copied")

    @_bind_event_store_process
    def __reduce__(self) -> NoReturn:
        raise TypeError("event stores cannot be serialized")

    @_bind_event_store_process
    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("event stores cannot be serialized")
