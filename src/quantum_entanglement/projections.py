# ruff: noqa: UP045
"""Durable, version-aware read-model projection primitives.

The module deliberately treats event payloads as untrusted persisted input.  A
projection may consume an event only after its schema type and version have been
validated and every required one-version upcast has completed.
"""

from __future__ import annotations

import copy
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Optional, Protocol

from .events import StoredEvent
from .protocol import utc_now

SCHEMA_VERSION_FIELD = "_schemaVersion"
"""Reserved payload metadata used until schema version has its own event column."""


class EventSchemaError(RuntimeError):
    """Base class for events that cannot safely be presented to a projection."""


class UnknownEventTypeError(EventSchemaError):
    """Raised when a projection has no declared schema for an event type."""


class InvalidEventSchemaVersionError(EventSchemaError):
    """Raised when persisted schema-version metadata is not a positive integer."""


class FutureEventSchemaVersionError(EventSchemaError):
    """Raised when persisted data is newer than the running projection code."""


class MissingUpcasterError(EventSchemaError):
    """Raised when there is a gap in the required one-version upcast chain."""


class InvalidUpcastResultError(EventSchemaError):
    """Raised when an upcaster violates the payload contract."""


Upcaster = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class ProjectionOffsetError(RuntimeError):
    """Base class for durable projection ownership or offset failures."""


class ProjectionLeaseConflictError(ProjectionOffsetError):
    """Raised when another owner still holds the projection lease."""


class ProjectionLeaseLostError(ProjectionOffsetError):
    """Raised when a worker's owner epoch is stale or its lease has expired."""


class ProjectionOffsetConflictError(ProjectionOffsetError):
    """Raised when compare-and-swap observes an unexpected persisted offset."""


class ProjectionReceiptConflictError(ProjectionOffsetError):
    """Raised when durable deduplication metadata contradicts an event."""


@dataclass(frozen=True)
class UpcastedEvent:
    """A stored event paired with data normalized to the registered schema."""

    stored_event: StoredEvent
    payload: Mapping[str, Any]
    source_schema_version: int
    schema_version: int


@dataclass(frozen=True)
class ProjectionLease:
    """Fencing token for exactly one projection-owner epoch."""

    projection_name: str
    owner_id: str
    owner_epoch: int
    lease_expires_at: str


@dataclass(frozen=True)
class ProjectionOffset:
    """Operator-readable durable checkpoint and its current ownership metadata."""

    projection_name: str
    last_global_position: int
    owner_id: Optional[str]
    owner_epoch: int
    lease_expires_at: Optional[str]
    updated_at: Optional[str]


@dataclass(frozen=True)
class ProjectionApplyResult:
    """Outcome of one transactional, receipt-deduplicated event application."""

    applied: bool
    offset: ProjectionOffset


@dataclass(frozen=True)
class ProjectionRunResult:
    """Bounded projector-run telemetry suitable for metrics and tests."""

    scanned_count: int
    applied_count: int
    deduplicated_count: int
    last_global_position: int


@dataclass(frozen=True)
class ProjectionStatementResult:
    """Connection-free result copied from a projection handler SQL statement."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    rowcount: int
    lastrowid: Optional[int]


class EventSource(Protocol):
    """Minimal append-only source contract required by ``DurableProjector``."""

    def read_all(self, after_position: int = 0, limit: int = 1000) -> tuple[StoredEvent, ...]:
        """Return events ordered by increasing global position."""


class ProjectionTransaction:
    """Restricted SQLite surface that never exposes a cursor or connection."""

    __slots__ = ("__execute_many", "__execute_statement")

    def __init__(self, connection: sqlite3.Connection) -> None:
        def execute_statement(sql: str, parameters: Any) -> ProjectionStatementResult:
            return ProjectionTransaction._copy_result(connection.execute(sql, parameters))

        def execute_many(sql: str, parameters: Any) -> ProjectionStatementResult:
            return ProjectionTransaction._copy_result(connection.executemany(sql, parameters))

        self.__execute_statement = execute_statement
        self.__execute_many = execute_many

    @staticmethod
    def _copy_result(cursor: sqlite3.Cursor) -> ProjectionStatementResult:
        try:
            description = cursor.description
            if description is None:
                columns: tuple[str, ...] = ()
                rows: tuple[tuple[Any, ...], ...] = ()
            else:
                columns = tuple(str(column[0]) for column in description)
                rows = tuple(tuple(row) for row in cursor.fetchall())
            return ProjectionStatementResult(
                columns=columns,
                rows=rows,
                rowcount=cursor.rowcount,
                lastrowid=cursor.lastrowid,
            )
        finally:
            cursor.close()

    def execute(self, sql: str, parameters: Any = ()) -> ProjectionStatementResult:
        return self.__execute_statement(sql, parameters)

    def executemany(self, sql: str, parameters: Any) -> ProjectionStatementResult:
        return self.__execute_many(sql, parameters)


ProjectionHandler = Callable[[ProjectionTransaction, UpcastedEvent], None]

_HANDLER_DENIED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_TRANSACTION,
    }
)
_HANDLER_DATA_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_UPDATE,
    }
)
_HANDLER_SCHEMA_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ANALYZE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_REINDEX,
    }
)
_FRAMEWORK_TABLES = frozenset(
    {
        "artifact_blobs",
        "artifact_versions",
        "events",
        "inbox_receipts",
        "invocation_attempts",
        "invocation_jobs",
        "outbox",
        "outbox_ambiguities",
        "projection_offsets",
        "projection_receipts",
        "qe_revocation_high_water",
        "qe_schema_migrations",
        "snapshots",
        "sqlite_sequence",
    }
)
_FRAMEWORK_INDEXES = frozenset(
    {
        "idx_artifact_versions_digest",
        "idx_artifact_versions_head",
        "idx_artifact_versions_task",
        "idx_events_correlation",
        "idx_events_stream",
        "idx_inbox_event",
        "idx_invocation_attempts_job",
        "idx_invocation_attempts_status",
        "idx_invocation_jobs_claim",
        "idx_invocation_jobs_lease_expiry",
        "idx_invocation_jobs_session",
        "idx_outbox_ambiguities_one_open",
        "idx_outbox_ambiguities_opened",
        "idx_outbox_delivery",
        "idx_outbox_trigger",
        "idx_projection_receipts_position",
    }
)


class EventUpcasterRegistry:
    """Fail-closed registry of current schemas and consecutive upcasters.

    Schema versions start at one.  An upcaster always moves exactly one version
    forward; the registry owns the version counter so upcasters cannot skip a
    validation boundary.  Legacy payloads without ``SCHEMA_VERSION_FIELD`` are
    interpreted as version one.
    """

    def __init__(self) -> None:
        self._current_versions: dict[str, int] = {}
        self._upcasters: dict[tuple[str, int], Upcaster] = {}

    @staticmethod
    def _validate_event_type(event_type: str) -> str:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type is required")
        return event_type

    @staticmethod
    def _validate_version(version: int, field_name: str) -> int:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError(f"{field_name} must be a positive integer")
        return version

    def register_event_type(self, event_type: str, *, current_version: int) -> None:
        """Declare the only schema version this process may project as current."""

        event_type = self._validate_event_type(event_type)
        current_version = self._validate_version(current_version, "current_version")
        if event_type in self._current_versions:
            raise ValueError(f"event type is already registered: {event_type}")
        self._current_versions[event_type] = current_version

    def register_upcaster(
        self,
        event_type: str,
        *,
        from_version: int,
        upcaster: Upcaster,
    ) -> None:
        """Register one deterministic ``N -> N + 1`` payload transformation."""

        event_type = self._validate_event_type(event_type)
        from_version = self._validate_version(from_version, "from_version")
        if not callable(upcaster):
            raise TypeError("upcaster must be callable")
        current_version = self._current_versions.get(event_type)
        if current_version is None:
            raise UnknownEventTypeError(f"unregistered event type: {event_type}")
        if from_version >= current_version:
            raise ValueError("from_version must be lower than the current version")
        key = (event_type, from_version)
        if key in self._upcasters:
            raise ValueError(f"upcaster is already registered: {event_type} v{from_version}")
        self._upcasters[key] = upcaster

    @staticmethod
    def _extract_payload(stored_event: StoredEvent) -> tuple[dict[str, Any], int]:
        payload = copy.deepcopy(dict(stored_event.event.payload))
        raw_version = payload.pop(SCHEMA_VERSION_FIELD, 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
            raise InvalidEventSchemaVersionError(
                f"{SCHEMA_VERSION_FIELD} must be a positive integer"
            )
        return payload, raw_version

    def upcast(self, stored_event: StoredEvent) -> UpcastedEvent:
        """Validate and normalize a stored event, rejecting all ambiguous input."""

        event_type = stored_event.event.event_type
        current_version = self._current_versions.get(event_type)
        if current_version is None:
            raise UnknownEventTypeError(f"unregistered event type: {event_type}")

        payload, source_version = self._extract_payload(stored_event)
        if source_version > current_version:
            raise FutureEventSchemaVersionError(
                f"{event_type} v{source_version} is newer than supported v{current_version}"
            )

        version = source_version
        while version < current_version:
            upcaster = self._upcasters.get((event_type, version))
            if upcaster is None:
                raise MissingUpcasterError(
                    f"missing consecutive upcaster: {event_type} v{version} -> v{version + 1}"
                )
            candidate = upcaster(MappingProxyType(copy.deepcopy(payload)))
            if not isinstance(candidate, Mapping):
                raise InvalidUpcastResultError(
                    f"upcaster {event_type} v{version} must return a mapping"
                )
            payload = copy.deepcopy(dict(candidate))
            if SCHEMA_VERSION_FIELD in payload:
                raise InvalidUpcastResultError(
                    f"upcasters must not emit reserved field {SCHEMA_VERSION_FIELD}"
                )
            version += 1

        return UpcastedEvent(
            stored_event=stored_event,
            payload=MappingProxyType(payload),
            source_schema_version=source_version,
            schema_version=current_version,
        )


class SQLiteProjectionOffsetStore:
    """Durable projection offsets with leased, epoch-fenced ownership.

    Point this store at the same SQLite file as :class:`SQLiteEventStore` when
    projection tables and offsets must commit together.  It uses a separate
    connection and self-initializes only its two projection-owned tables.
    """

    _MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], str] = utc_now,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(path, str) or not path:
            raise ValueError("path is required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be greater than zero")
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_seconds,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.set_authorizer(self._allow_all_authorizer)
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_seconds * 1000)}")
            if path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    @staticmethod
    def _allow_all_authorizer(
        _action_code: int,
        _argument_one: Optional[str],
        _argument_two: Optional[str],
        _database_name: Optional[str],
        _trigger_or_view: Optional[str],
    ) -> int:
        return sqlite3.SQLITE_OK

    @staticmethod
    def _is_framework_table(identifier: Optional[str]) -> bool:
        if identifier is None:
            return False
        normalized = identifier.casefold()
        return normalized in _FRAMEWORK_TABLES or normalized.startswith("qe_")

    @staticmethod
    def _is_framework_schema_object(identifier: Optional[str]) -> bool:
        if identifier is None:
            return False
        normalized = identifier.casefold()
        return (
            normalized in _FRAMEWORK_TABLES
            or normalized in _FRAMEWORK_INDEXES
            or normalized.startswith("qe_")
        )

    @classmethod
    def _projection_handler_authorizer(
        cls,
        action_code: int,
        argument_one: Optional[str],
        argument_two: Optional[str],
        _database_name: Optional[str],
        _trigger_or_view: Optional[str],
    ) -> int:
        if action_code in _HANDLER_DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action_code in _HANDLER_DATA_ACTIONS and cls._is_framework_table(argument_one):
            return sqlite3.SQLITE_DENY
        if action_code in _HANDLER_SCHEMA_ACTIONS and (
            cls._is_framework_schema_object(argument_one)
            or cls._is_framework_schema_object(argument_two)
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @classmethod
    @contextmanager
    def _handler_transaction(
        cls,
        connection: sqlite3.Connection,
    ) -> Iterator[ProjectionTransaction]:
        connection.set_authorizer(cls._projection_handler_authorizer)
        try:
            yield ProjectionTransaction(connection)
        finally:
            # Python 3.9 cannot reliably clear an authorizer with ``None``.
            # Reinstalling an explicit allow callback also invalidates cached
            # statements that were compiled under the restricted callback.
            connection.set_authorizer(cls._allow_all_authorizer)

    def _initialize(self) -> None:
        with self._lock:
            try:
                self._connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS projection_offsets (
                    projection_name TEXT PRIMARY KEY,
                    last_global_position INTEGER NOT NULL DEFAULT 0,
                    owner_id TEXT NOT NULL,
                    owner_epoch INTEGER NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(last_global_position >= 0),
                    CHECK(owner_epoch > 0)
                );
                CREATE TABLE IF NOT EXISTS projection_receipts (
                    projection_name TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    global_position INTEGER NOT NULL,
                    applied_at TEXT NOT NULL,
                    PRIMARY KEY(projection_name, event_id),
                    UNIQUE(projection_name, global_position),
                    CHECK(global_position > 0)
                );
                CREATE INDEX IF NOT EXISTS idx_projection_receipts_position
                    ON projection_receipts(projection_name, global_position);
                COMMIT;
                """
                )
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

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

    @staticmethod
    def _validate_name(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} is required")
        return value

    @staticmethod
    def _validate_position(value: int, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return value

    @staticmethod
    def _normalize_timestamp(value: str, field_name: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _lease_deadline(cls, now: str, lease_seconds: float) -> str:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        deadline = parsed + timedelta(seconds=lease_seconds)
        return deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _is_after(left: str, right: str) -> bool:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) > datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )

    def _now(self) -> str:
        return self._normalize_timestamp(self._clock(), "clock")

    @staticmethod
    def _row_to_offset(row: sqlite3.Row) -> ProjectionOffset:
        return ProjectionOffset(
            projection_name=str(row["projection_name"]),
            last_global_position=int(row["last_global_position"]),
            owner_id=str(row["owner_id"]),
            owner_epoch=int(row["owner_epoch"]),
            lease_expires_at=str(row["lease_expires_at"]),
            updated_at=str(row["updated_at"]),
        )

    def load(self, projection_name: str) -> ProjectionOffset:
        """Read a checkpoint; an unseen projection has the virtual offset zero."""

        projection_name = self._validate_name(projection_name, "projection_name")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (projection_name,),
            ).fetchone()
        if row is None:
            return ProjectionOffset(projection_name, 0, None, 0, None, None)
        return self._row_to_offset(row)

    def claim(
        self,
        projection_name: str,
        owner_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> ProjectionLease:
        """Acquire ownership or fence an older incarnation of the same owner."""

        projection_name = self._validate_name(projection_name, "projection_name")
        owner_id = self._validate_name(owner_id, "owner_id")
        with self._transaction() as connection:
            now = self._now()
            deadline = self._lease_deadline(now, lease_seconds)
            row = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (projection_name,),
            ).fetchone()
            if row is None:
                epoch = 1
                connection.execute(
                    """
                    INSERT INTO projection_offsets (
                        projection_name, last_global_position, owner_id, owner_epoch,
                        lease_expires_at, updated_at
                    ) VALUES (?, 0, ?, ?, ?, ?)
                    """,
                    (projection_name, owner_id, epoch, deadline, now),
                )
            else:
                persisted_owner = str(row["owner_id"])
                persisted_deadline = str(row["lease_expires_at"])
                if self._is_after(persisted_deadline, now) and persisted_owner != owner_id:
                    raise ProjectionLeaseConflictError(
                        f"projection {projection_name} is leased by another owner"
                    )
                previous_epoch = int(row["owner_epoch"])
                if previous_epoch >= self._MAX_SQLITE_INTEGER:
                    raise ProjectionOffsetError("projection owner epoch is exhausted")
                epoch = previous_epoch + 1
                connection.execute(
                    """
                    UPDATE projection_offsets
                    SET owner_id = ?, owner_epoch = ?, lease_expires_at = ?, updated_at = ?
                    WHERE projection_name = ?
                    """,
                    (owner_id, epoch, deadline, now, projection_name),
                )
        return ProjectionLease(projection_name, owner_id, epoch, deadline)

    def renew(
        self,
        lease: ProjectionLease,
        *,
        lease_seconds: float = 30.0,
    ) -> ProjectionLease:
        """Extend a still-live lease without changing its fencing epoch."""

        with self._transaction() as connection:
            now = self._now()
            deadline = self._lease_deadline(now, lease_seconds)
            cursor = connection.execute(
                """
                UPDATE projection_offsets
                SET lease_expires_at = ?, updated_at = ?
                WHERE projection_name = ? AND owner_id = ? AND owner_epoch = ?
                  AND julianday(lease_expires_at) > julianday(?)
                """,
                (
                    deadline,
                    now,
                    lease.projection_name,
                    lease.owner_id,
                    lease.owner_epoch,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectionLeaseLostError("projection lease is stale or expired")
        return ProjectionLease(
            lease.projection_name,
            lease.owner_id,
            lease.owner_epoch,
            deadline,
        )

    def advance(
        self,
        lease: ProjectionLease,
        *,
        expected_position: int,
        new_position: int,
    ) -> ProjectionOffset:
        """Monotonically advance an offset using owner-epoch and position CAS."""

        expected_position = self._validate_position(expected_position, "expected_position")
        new_position = self._validate_position(new_position, "new_position")
        if new_position <= expected_position:
            raise ValueError("new_position must be greater than expected_position")
        with self._transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if row is None:
                raise ProjectionLeaseLostError("projection lease does not exist")
            if (
                str(row["owner_id"]) != lease.owner_id
                or int(row["owner_epoch"]) != lease.owner_epoch
                or not self._is_after(str(row["lease_expires_at"]), now)
            ):
                raise ProjectionLeaseLostError("projection lease is stale or expired")
            if int(row["last_global_position"]) != expected_position:
                raise ProjectionOffsetConflictError("projection offset compare-and-swap conflict")
            connection.execute(
                """
                UPDATE projection_offsets
                SET last_global_position = ?, updated_at = ?
                WHERE projection_name = ? AND owner_id = ? AND owner_epoch = ?
                  AND last_global_position = ?
                """,
                (
                    new_position,
                    now,
                    lease.projection_name,
                    lease.owner_id,
                    lease.owner_epoch,
                    expected_position,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if updated is None:  # pragma: no cover - protected by the transaction
                raise ProjectionOffsetError("projection offset disappeared")
            return self._row_to_offset(updated)

    def apply_event(
        self,
        lease: ProjectionLease,
        *,
        expected_position: int,
        event: UpcastedEvent,
        handler: ProjectionHandler,
    ) -> ProjectionApplyResult:
        """Apply, receipt and checkpoint one event in the same SQLite transaction.

        The handler can mutate read-model tables only through ``ProjectionTransaction``.
        If it raises (or the process dies before SQLite commits), its writes, receipt and
        offset all roll back. Re-presenting an already receipted event is a no-op.
        """

        expected_position = self._validate_position(expected_position, "expected_position")
        if not callable(handler):
            raise TypeError("handler must be callable")
        stored_event = event.stored_event
        position = stored_event.global_position
        if isinstance(position, bool) or not isinstance(position, int) or position <= 0:
            raise ValueError("event global_position must be a positive integer")
        event_id = stored_event.event.event_id
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id is required")

        with self._transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if row is None:
                raise ProjectionLeaseLostError("projection lease does not exist")
            if (
                str(row["owner_id"]) != lease.owner_id
                or int(row["owner_epoch"]) != lease.owner_epoch
                or not self._is_after(str(row["lease_expires_at"]), now)
            ):
                raise ProjectionLeaseLostError("projection lease is stale or expired")

            persisted_position = int(row["last_global_position"])
            receipt = connection.execute(
                """
                SELECT global_position FROM projection_receipts
                WHERE projection_name = ? AND event_id = ?
                """,
                (lease.projection_name, event_id),
            ).fetchone()
            if receipt is not None:
                receipt_position = int(receipt["global_position"])
                if receipt_position != position or persisted_position < position:
                    raise ProjectionReceiptConflictError(
                        "projection receipt contradicts the event log or checkpoint"
                    )
                return ProjectionApplyResult(False, self._row_to_offset(row))

            if persisted_position != expected_position:
                raise ProjectionOffsetConflictError("projection offset compare-and-swap conflict")
            if position <= expected_position:
                raise ProjectionOffsetConflictError(
                    "unreceipted event does not advance the projection offset"
                )

            with self._handler_transaction(connection) as transaction:
                handler(transaction, event)
            try:
                connection.execute(
                    """
                    INSERT INTO projection_receipts (
                        projection_name, event_id, global_position, applied_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (lease.projection_name, event_id, position, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectionReceiptConflictError(
                    "event id or global position has a conflicting projection receipt"
                ) from exc
            cursor = connection.execute(
                """
                UPDATE projection_offsets
                SET last_global_position = ?, updated_at = ?
                WHERE projection_name = ? AND owner_id = ? AND owner_epoch = ?
                  AND last_global_position = ?
                """,
                (
                    position,
                    now,
                    lease.projection_name,
                    lease.owner_id,
                    lease.owner_epoch,
                    expected_position,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - write lock preserves the CAS
                raise ProjectionOffsetConflictError("projection offset compare-and-swap conflict")
            updated = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if updated is None:  # pragma: no cover - protected by the transaction
                raise ProjectionOffsetError("projection offset disappeared")
            return ProjectionApplyResult(True, self._row_to_offset(updated))

    def release(self, lease: ProjectionLease) -> None:
        """Expire a live lease immediately; stale releases fail closed."""

        with self._transaction() as connection:
            now = self._now()
            cursor = connection.execute(
                """
                UPDATE projection_offsets
                SET lease_expires_at = ?, updated_at = ?
                WHERE projection_name = ? AND owner_id = ? AND owner_epoch = ?
                  AND julianday(lease_expires_at) > julianday(?)
                """,
                (
                    now,
                    now,
                    lease.projection_name,
                    lease.owner_id,
                    lease.owner_epoch,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectionLeaseLostError("projection lease is stale or expired")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class DurableProjector:
    """Bounded at-least-once reader with transactional SQLite materialization."""

    def __init__(
        self,
        projection_name: str,
        owner_id: str,
        event_source: EventSource,
        offset_store: SQLiteProjectionOffsetStore,
        registry: EventUpcasterRegistry,
        handler: ProjectionHandler,
        *,
        lease_seconds: float = 30.0,
    ) -> None:
        self.projection_name = offset_store._validate_name(  # noqa: SLF001
            projection_name, "projection_name"
        )
        self.owner_id = offset_store._validate_name(owner_id, "owner_id")  # noqa: SLF001
        if not callable(getattr(event_source, "read_all", None)):
            raise TypeError("event_source must provide read_all")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self.event_source = event_source
        self.offset_store = offset_store
        self.registry = registry
        self.handler = handler
        self.lease_seconds = lease_seconds

    def run_once(self, *, limit: int = 100) -> ProjectionRunResult:
        """Project at most ``limit`` events and release ownership when still live."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        lease = self.offset_store.claim(
            self.projection_name,
            self.owner_id,
            lease_seconds=self.lease_seconds,
        )
        scanned = 0
        applied = 0
        deduplicated = 0
        last_position = self.offset_store.load(self.projection_name).last_global_position
        try:
            events = self.event_source.read_all(after_position=last_position, limit=limit)
            for stored_event in events:
                lease = self.offset_store.renew(
                    lease,
                    lease_seconds=self.lease_seconds,
                )
                normalized = self.registry.upcast(stored_event)
                result = self.offset_store.apply_event(
                    lease,
                    expected_position=last_position,
                    event=normalized,
                    handler=self.handler,
                )
                scanned += 1
                if result.applied:
                    applied += 1
                else:
                    deduplicated += 1
                last_position = result.offset.last_global_position
            return ProjectionRunResult(scanned, applied, deduplicated, last_position)
        finally:
            try:
                self.offset_store.release(lease)
            except ProjectionLeaseLostError:
                # A long handler may outlive its lease. The transactional apply either
                # committed under the SQLite write lock or rolled back before takeover.
                pass
