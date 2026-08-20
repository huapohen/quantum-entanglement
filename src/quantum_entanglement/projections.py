# ruff: noqa: UP045
"""Durable, version-aware read-model projection primitives.

The module deliberately treats event payloads as untrusted persisted input.  A
projection may consume an event only after its schema type and version have been
validated and every required one-version upcast has completed.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from time import monotonic, sleep
from types import MappingProxyType
from typing import Any, Callable, Optional, Protocol

from .events import StoredEvent
from .protocol import utc_now

SCHEMA_VERSION_FIELD = "_schemaVersion"
"""Reserved payload metadata used until schema version has its own event column."""

MAX_EVENT_PAYLOAD_DEPTH = 64
"""Maximum value depth, counting the root payload mapping as depth zero."""

MAX_EVENT_PAYLOAD_NODES = 10_000
"""Maximum values and containers visited while copying one event payload."""

MAX_PROJECTION_IDENTIFIER_LENGTH = 255
"""Maximum stored length for projection names, owner IDs, and event IDs."""

MAX_PROJECTION_BATCH_SIZE = 1000
"""Maximum events a durable projector may admit from one source read."""

MAX_PROJECTION_LEASE_SECONDS = 86_400
"""Maximum duration of one projection lease, in seconds."""

MAX_PROJECTION_BUSY_TIMEOUT_SECONDS = 300
"""Maximum SQLite lock wait for a projection store, in seconds."""

MIN_PROJECTION_SQLITE_VERSION = (3, 8, 9)
"""Oldest SQLite release whose ``index_xinfo`` contract this store validates."""

_PROJECTION_LEASE_SECONDS_ERROR = (
    "lease_seconds must be an exact finite int or float greater than zero "
    f"and at most {MAX_PROJECTION_LEASE_SECONDS}"
)
_PROJECTION_BUSY_TIMEOUT_SECONDS_ERROR = (
    "busy_timeout_seconds must be an exact finite int or float greater than zero "
    f"and at most {MAX_PROJECTION_BUSY_TIMEOUT_SECONDS}"
)
_MAPPING_PROXY_TYPE: type[Any] = type(MappingProxyType({}))


class EventSchemaError(RuntimeError):
    """Base class for events that cannot safely be presented to a projection."""


class UnknownEventTypeError(EventSchemaError):
    """Raised when a projection has no declared schema for an event type."""


class InvalidEventSchemaVersionError(EventSchemaError):
    """Raised when persisted schema-version metadata is not a positive integer."""


class InvalidEventPayloadError(EventSchemaError):
    """Raised when persisted payload structure cannot be decoded safely."""


class FutureEventSchemaVersionError(EventSchemaError):
    """Raised when persisted data is newer than the running projection code."""


class MissingUpcasterError(EventSchemaError):
    """Raised when there is a gap in the required one-version upcast chain."""


class InvalidUpcastResultError(EventSchemaError):
    """Raised when an upcaster violates the payload contract."""


class InvalidDecoderResultError(EventSchemaError):
    """Raised when a current-schema decoder violates the payload contract."""


class EventSchemaDecoderError(EventSchemaError):
    """Raised when a current-schema decoder fails on persisted event data."""


class EventSchemaRegistrySealedError(EventSchemaError):
    """Raised when code tries to mutate an already sealed schema registry."""


class UnsealedEventSchemaRegistryError(EventSchemaError):
    """Raised when projection code tries to use an unsealed schema registry."""


Upcaster = Callable[[Mapping[str, Any]], Mapping[str, Any]]
CurrentSchemaDecoder = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class _PayloadStructureError(ValueError):
    """Internal bounded-copy failure wrapped at the relevant schema boundary."""


class _PayloadTraversalState:
    __slots__ = ("active_container_ids", "nodes")

    def __init__(self) -> None:
        self.active_container_ids: set[int] = set()
        self.nodes = 0


class ProjectionOffsetError(RuntimeError):
    """Base class for durable projection ownership or offset failures."""


class ProjectionIntegrityError(ProjectionOffsetError):
    """Raised when persisted projection state violates its strict data contract."""


class ProjectionSchemaError(ProjectionIntegrityError):
    """Raised when projection-owned SQLite schema is absent in part or has drifted."""


class ProjectionSourceIntegrityError(ProjectionIntegrityError):
    """Raised when an event source violates the bounded contiguous batch contract."""


class ProjectionLeaseConflictError(ProjectionOffsetError):
    """Raised when another owner still holds the projection lease."""


class ProjectionLeaseLostError(ProjectionOffsetError):
    """Raised when a worker's owner epoch is stale or its lease has expired."""


class ProjectionOffsetConflictError(ProjectionOffsetError):
    """Raised when compare-and-swap observes an unexpected persisted offset."""


class ProjectionReceiptConflictError(ProjectionOffsetError):
    """Raised when durable deduplication metadata contradicts an event."""


class ProjectionTransactionClosedError(RuntimeError):
    """Raised when a handler uses its transaction capability outside its scope."""


class ProjectionTransactionThreadError(RuntimeError):
    """Raised when a handler transaction is used from a non-owner thread."""


class _ProjectionSchemaState(str, Enum):
    """The only projection schema states accepted during startup."""

    ABSENT = "absent"
    EXACT = "exact"


_PROJECTION_OFFSETS_TABLE_NAME = "projection_offsets"
_PROJECTION_RECEIPTS_TABLE_NAME = "projection_receipts"
_PROJECTION_RECEIPTS_POSITION_INDEX_NAME = "idx_projection_receipts_position"
_PROJECTION_OFFSETS_AUTO_INDEX_NAME = "sqlite_autoindex_projection_offsets_1"
_PROJECTION_RECEIPTS_PRIMARY_INDEX_NAME = "sqlite_autoindex_projection_receipts_1"
_PROJECTION_RECEIPTS_UNIQUE_INDEX_NAME = "sqlite_autoindex_projection_receipts_2"
_MAX_PROJECTION_SCHEMA_OBJECTS = 16
_MAX_PROJECTION_SCHEMA_NAME_LENGTH = 255
_MAX_PROJECTION_SCHEMA_SQL_LENGTH = 8192

_PROJECTION_OFFSETS_TABLE_SQL = """
CREATE TABLE projection_offsets (
    projection_name TEXT PRIMARY KEY,
    last_global_position INTEGER NOT NULL DEFAULT 0,
    owner_id TEXT NOT NULL,
    owner_epoch INTEGER NOT NULL,
    lease_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(last_global_position >= 0),
    CHECK(owner_epoch > 0)
);
""".strip()

_PROJECTION_RECEIPTS_TABLE_SQL = """
CREATE TABLE projection_receipts (
    projection_name TEXT NOT NULL,
    event_id TEXT NOT NULL,
    global_position INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY(projection_name, event_id),
    UNIQUE(projection_name, global_position),
    CHECK(global_position > 0)
);
""".strip()

_PROJECTION_RECEIPTS_POSITION_INDEX_SQL = """
CREATE INDEX idx_projection_receipts_position
    ON projection_receipts(projection_name, global_position);
""".strip()

_PROJECTION_SCHEMA_DDL = (
    _PROJECTION_OFFSETS_TABLE_SQL,
    _PROJECTION_RECEIPTS_TABLE_SQL,
    _PROJECTION_RECEIPTS_POSITION_INDEX_SQL,
)


@dataclass(frozen=True)
class _ProjectionSchemaObject:
    object_type: str
    name: str
    table_name: str
    schema_sql: Optional[str]


def _canonical_projection_schema_sql(sql: str) -> str:
    """Normalize only insignificant SQL whitespace outside quoted regions."""

    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    output: list[str] = []
    quote_end: Optional[str] = None
    index = 0
    while index < len(stripped):
        character = stripped[index]
        if quote_end is not None:
            output.append(character)
            if character == quote_end:
                if quote_end != "]" and index + 1 < len(stripped):
                    if stripped[index + 1] == quote_end:
                        output.append(stripped[index + 1])
                        index += 2
                        continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote_end = character
            output.append(character)
        elif character == "[":
            quote_end = "]"
            output.append(character)
        elif character.isspace():
            if output and output[-1] != " ":
                output.append(" ")
        else:
            output.append(character)
        index += 1
    return "".join(output).strip()


_EXPECTED_PROJECTION_SCHEMA_OBJECTS = tuple(
    sorted(
        (
            _ProjectionSchemaObject(
                "index",
                _PROJECTION_RECEIPTS_POSITION_INDEX_NAME,
                _PROJECTION_RECEIPTS_TABLE_NAME,
                _canonical_projection_schema_sql(_PROJECTION_RECEIPTS_POSITION_INDEX_SQL),
            ),
            _ProjectionSchemaObject(
                "index",
                _PROJECTION_OFFSETS_AUTO_INDEX_NAME,
                _PROJECTION_OFFSETS_TABLE_NAME,
                None,
            ),
            _ProjectionSchemaObject(
                "index",
                _PROJECTION_RECEIPTS_PRIMARY_INDEX_NAME,
                _PROJECTION_RECEIPTS_TABLE_NAME,
                None,
            ),
            _ProjectionSchemaObject(
                "index",
                _PROJECTION_RECEIPTS_UNIQUE_INDEX_NAME,
                _PROJECTION_RECEIPTS_TABLE_NAME,
                None,
            ),
            _ProjectionSchemaObject(
                "table",
                _PROJECTION_OFFSETS_TABLE_NAME,
                _PROJECTION_OFFSETS_TABLE_NAME,
                _canonical_projection_schema_sql(_PROJECTION_OFFSETS_TABLE_SQL),
            ),
            _ProjectionSchemaObject(
                "table",
                _PROJECTION_RECEIPTS_TABLE_NAME,
                _PROJECTION_RECEIPTS_TABLE_NAME,
                _canonical_projection_schema_sql(_PROJECTION_RECEIPTS_TABLE_SQL),
            ),
        ),
        key=lambda item: (item.object_type, item.name, item.table_name),
    )
)

_EXPECTED_PROJECTION_TABLE_INFO: dict[str, tuple[tuple[Any, ...], ...]] = {
    _PROJECTION_OFFSETS_TABLE_NAME: (
        (0, "projection_name", "TEXT", 0, None, 1),
        (1, "last_global_position", "INTEGER", 1, "0", 0),
        (2, "owner_id", "TEXT", 1, None, 0),
        (3, "owner_epoch", "INTEGER", 1, None, 0),
        (4, "lease_expires_at", "TEXT", 1, None, 0),
        (5, "updated_at", "TEXT", 1, None, 0),
    ),
    _PROJECTION_RECEIPTS_TABLE_NAME: (
        (0, "projection_name", "TEXT", 1, None, 1),
        (1, "event_id", "TEXT", 1, None, 2),
        (2, "global_position", "INTEGER", 1, None, 0),
        (3, "applied_at", "TEXT", 1, None, 0),
    ),
}

_EXPECTED_PROJECTION_INDEX_LISTS: dict[str, tuple[tuple[Any, ...], ...]] = {
    _PROJECTION_OFFSETS_TABLE_NAME: ((_PROJECTION_OFFSETS_AUTO_INDEX_NAME, 1, "pk", 0),),
    _PROJECTION_RECEIPTS_TABLE_NAME: (
        (_PROJECTION_RECEIPTS_POSITION_INDEX_NAME, 0, "c", 0),
        (_PROJECTION_RECEIPTS_PRIMARY_INDEX_NAME, 1, "pk", 0),
        (_PROJECTION_RECEIPTS_UNIQUE_INDEX_NAME, 1, "u", 0),
    ),
}

_EXPECTED_PROJECTION_INDEX_INFO: dict[str, tuple[tuple[Any, ...], ...]] = {
    _PROJECTION_OFFSETS_AUTO_INDEX_NAME: ((0, 0, "projection_name"),),
    _PROJECTION_RECEIPTS_POSITION_INDEX_NAME: (
        (0, 0, "projection_name"),
        (1, 2, "global_position"),
    ),
    _PROJECTION_RECEIPTS_PRIMARY_INDEX_NAME: (
        (0, 0, "projection_name"),
        (1, 1, "event_id"),
    ),
    _PROJECTION_RECEIPTS_UNIQUE_INDEX_NAME: (
        (0, 0, "projection_name"),
        (1, 2, "global_position"),
    ),
}

# SQLite exposes the rowid as the one legitimate negative ``cid`` auxiliary row.
_EXPECTED_PROJECTION_INDEX_XINFO: dict[str, tuple[tuple[Any, ...], ...]] = {
    _PROJECTION_OFFSETS_AUTO_INDEX_NAME: (
        (0, 0, "projection_name", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    _PROJECTION_RECEIPTS_POSITION_INDEX_NAME: (
        (0, 0, "projection_name", 0, "BINARY", 1),
        (1, 2, "global_position", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    _PROJECTION_RECEIPTS_PRIMARY_INDEX_NAME: (
        (0, 0, "projection_name", 0, "BINARY", 1),
        (1, 1, "event_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    _PROJECTION_RECEIPTS_UNIQUE_INDEX_NAME: (
        (0, 0, "projection_name", 0, "BINARY", 1),
        (1, 2, "global_position", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
}


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
class _ProjectionReceipt:
    """Strictly decoded durable deduplication receipt."""

    projection_name: str
    event_id: str
    global_position: int
    applied_at: str


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
        """Return an exact tuple whose positions start after the cursor and are contiguous."""


class ProjectionTransaction:
    """Restricted, scope-bound SQLite capability for one projection handler."""

    __slots__ = ("__state",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.__state: Optional[tuple[sqlite3.Connection, int]] = (
            connection,
            threading.get_ident(),
        )

    def __copy__(self) -> ProjectionTransaction:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> ProjectionTransaction:
        return self

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
        state = self.__state
        if state is None:
            raise ProjectionTransactionClosedError("projection transaction is no longer active")
        connection, owner_thread_id = state
        if threading.get_ident() != owner_thread_id:
            raise ProjectionTransactionThreadError(
                "projection transaction may only be used by its handler thread"
            )
        return self._copy_result(connection.execute(sql, parameters))

    def executemany(self, sql: str, parameters: Any) -> ProjectionStatementResult:
        state = self.__state
        if state is None:
            raise ProjectionTransactionClosedError("projection transaction is no longer active")
        connection, owner_thread_id = state
        if threading.get_ident() != owner_thread_id:
            raise ProjectionTransactionThreadError(
                "projection transaction may only be used by its handler thread"
            )
        return self._copy_result(connection.executemany(sql, parameters))

    def _revoke(self) -> None:
        self._ensure_revoked()

    def _ensure_revoked(self) -> None:
        self.__state = None


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
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_REINDEX,
    }
)
_HANDLER_DEFERRED_SCHEMA_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_VTABLE,
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
    """Sealed registry of current-schema decoders and consecutive upcasters.

    Schema versions start at one.  An upcaster always moves exactly one version
    forward; the registry owns the version counter so upcasters cannot skip a
    validation boundary.  ``seal`` verifies every registered chain before events
    may be decoded. Legacy payloads without ``SCHEMA_VERSION_FIELD`` are version one.
    """

    def __init__(self) -> None:
        self._current_versions: dict[str, int] = {}
        self._decoders: dict[str, CurrentSchemaDecoder] = {}
        self._upcasters: dict[tuple[str, int], Upcaster] = {}
        self._sealed = False
        self._lock = threading.RLock()

    @property
    def is_sealed(self) -> bool:
        """Whether registration is closed and every upcaster chain is complete."""

        with self._lock:
            return self._sealed

    def require_sealed(self) -> None:
        """Reject use before the registry has validated and sealed its contracts."""

        with self._lock:
            if not self._sealed:
                raise UnsealedEventSchemaRegistryError(
                    "event schema registry must be sealed before projection"
                )

    def _require_open(self) -> None:
        if self._sealed:
            raise EventSchemaRegistrySealedError(
                "event schema registry is sealed and cannot be mutated"
            )

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

    def register_event_type(
        self,
        event_type: str,
        *,
        current_version: int,
        decoder: CurrentSchemaDecoder,
    ) -> None:
        """Declare a current version and its mandatory strict payload decoder."""

        with self._lock:
            self._require_open()
            event_type = self._validate_event_type(event_type)
            current_version = self._validate_version(current_version, "current_version")
            if not callable(decoder):
                raise TypeError("decoder must be callable")
            if event_type in self._current_versions:
                raise ValueError(f"event type is already registered: {event_type}")
            self._current_versions[event_type] = current_version
            self._decoders[event_type] = decoder

    def register_upcaster(
        self,
        event_type: str,
        *,
        from_version: int,
        upcaster: Upcaster,
    ) -> None:
        """Register one deterministic ``N -> N + 1`` payload transformation."""

        with self._lock:
            self._require_open()
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

    def seal(self) -> None:
        """Atomically verify all one-version chains and reject future mutation."""

        with self._lock:
            if self._sealed:
                return
            for event_type, current_version in self._current_versions.items():
                for version in range(1, current_version):
                    if (event_type, version) not in self._upcasters:
                        raise MissingUpcasterError(
                            "missing consecutive upcaster during registry seal: "
                            f"{event_type} v{version} -> v{version + 1}"
                        )
            self._sealed = True

    @classmethod
    def _copy_payload_value(
        cls,
        value: Any,
        *,
        readonly: bool,
        depth: int,
        state: _PayloadTraversalState,
    ) -> Any:
        if depth > MAX_EVENT_PAYLOAD_DEPTH:
            raise _PayloadStructureError(f"payload depth exceeds {MAX_EVENT_PAYLOAD_DEPTH}")
        state.nodes += 1
        if state.nodes > MAX_EVENT_PAYLOAD_NODES:
            raise _PayloadStructureError(f"payload node count exceeds {MAX_EVENT_PAYLOAD_NODES}")

        value_type = type(value)
        if value is None or value_type in (bool, int, str):
            return value
        if value_type is float:
            if not math.isfinite(value):
                raise _PayloadStructureError("payload numbers must be finite")
            return value

        if value_type in (dict, _MAPPING_PROXY_TYPE):
            identity = id(value)
            if identity in state.active_container_ids:
                raise _PayloadStructureError("payload containers must not contain cycles")
            state.active_container_ids.add(identity)
            try:
                copied_mapping: dict[str, Any] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise _PayloadStructureError("payload mapping keys must be strings")
                    copied_mapping[key] = cls._copy_payload_value(
                        item,
                        readonly=readonly,
                        depth=depth + 1,
                        state=state,
                    )
            except _PayloadStructureError:
                raise
            except Exception as exc:
                raise _PayloadStructureError("payload mapping traversal failed") from exc
            finally:
                state.active_container_ids.discard(identity)
            if readonly:
                return MappingProxyType(copied_mapping)
            return copied_mapping

        if value_type in (list, tuple):
            identity = id(value)
            if identity in state.active_container_ids:
                raise _PayloadStructureError("payload containers must not contain cycles")
            state.active_container_ids.add(identity)
            try:
                copied_items = [
                    cls._copy_payload_value(
                        item,
                        readonly=readonly,
                        depth=depth + 1,
                        state=state,
                    )
                    for item in value
                ]
            except _PayloadStructureError:
                raise
            except Exception as exc:
                raise _PayloadStructureError("payload sequence traversal failed") from exc
            finally:
                state.active_container_ids.discard(identity)
            if readonly or value_type is tuple:
                return tuple(copied_items)
            return copied_items

        raise _PayloadStructureError(
            "payload values must be JSON scalars, dicts, mapping proxies, lists, or tuples; "
            f"received {value_type.__name__}"
        )

    @classmethod
    def _copy_payload_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        readonly: bool,
    ) -> Mapping[str, Any]:
        """Copy one bounded tree of string-keyed built-in JSON-style containers.

        The root is depth zero and counts as one node. Dicts, mapping proxies,
        lists and tuples are the only containers; cycles and non-string keys fail.
        """

        copied = cls._copy_payload_value(
            payload,
            readonly=readonly,
            depth=0,
            state=_PayloadTraversalState(),
        )
        if not isinstance(copied, Mapping):  # pragma: no cover - root type is declared
            raise _PayloadStructureError("event payload root must be a mapping")
        return copied

    @classmethod
    def _extract_payload(cls, stored_event: StoredEvent) -> tuple[dict[str, Any], int]:
        try:
            copied = cls._copy_payload_mapping(
                stored_event.event.payload,
                readonly=False,
            )
        except _PayloadStructureError as exc:
            raise InvalidEventPayloadError(
                "persisted event payload is structurally invalid"
            ) from exc
        payload = dict(copied)
        raw_version = payload.pop(SCHEMA_VERSION_FIELD, 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
            raise InvalidEventSchemaVersionError(
                f"{SCHEMA_VERSION_FIELD} must be a positive integer"
            )
        return payload, raw_version

    @classmethod
    def _decode_current_payload(
        cls,
        event_type: str,
        decoder: CurrentSchemaDecoder,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            mutable_decoder_input = cls._copy_payload_mapping(payload, readonly=False)
        except _PayloadStructureError as exc:
            raise InvalidEventPayloadError(
                f"current-schema decoder input for {event_type} is structurally invalid"
            ) from exc
        decoder_input = MappingProxyType(dict(mutable_decoder_input))
        try:
            candidate = decoder(decoder_input)
        except Exception as exc:
            raise EventSchemaDecoderError(
                f"current-schema decoder failed for event type {event_type}"
            ) from exc
        if not isinstance(candidate, Mapping):
            raise InvalidDecoderResultError(
                f"current-schema decoder for {event_type} must return a mapping"
            )
        try:
            decoded = cls._copy_payload_mapping(candidate, readonly=True)
        except _PayloadStructureError as exc:
            raise InvalidDecoderResultError(
                f"current-schema decoder for {event_type} returned an invalid payload structure"
            ) from exc
        if SCHEMA_VERSION_FIELD in decoded:
            raise InvalidDecoderResultError(
                f"current-schema decoders must not emit reserved field {SCHEMA_VERSION_FIELD}"
            )
        return decoded

    def upcast(self, stored_event: StoredEvent) -> UpcastedEvent:
        """Validate and normalize a stored event, rejecting all ambiguous input."""

        self.require_sealed()
        event_type = stored_event.event.event_type
        with self._lock:
            current_version = self._current_versions.get(event_type)
            if current_version is None:
                raise UnknownEventTypeError(f"unregistered event type: {event_type}")
            decoder = self._decoders[event_type]
            upcasters = self._upcasters

        payload, source_version = self._extract_payload(stored_event)
        if source_version > current_version:
            raise FutureEventSchemaVersionError(
                f"{event_type} v{source_version} is newer than supported v{current_version}"
            )

        version = source_version
        while version < current_version:
            upcaster = upcasters.get((event_type, version))
            if upcaster is None:
                raise MissingUpcasterError(
                    f"missing consecutive upcaster: {event_type} v{version} -> v{version + 1}"
                )
            try:
                mutable_upcaster_input = self._copy_payload_mapping(payload, readonly=False)
            except _PayloadStructureError as exc:
                raise InvalidEventPayloadError(
                    f"upcaster input for {event_type} v{version} is structurally invalid"
                ) from exc
            candidate = upcaster(MappingProxyType(dict(mutable_upcaster_input)))
            if not isinstance(candidate, Mapping):
                raise InvalidUpcastResultError(
                    f"upcaster {event_type} v{version} must return a mapping"
                )
            try:
                copied_candidate = self._copy_payload_mapping(candidate, readonly=False)
            except _PayloadStructureError as exc:
                raise InvalidUpcastResultError(
                    f"upcaster {event_type} v{version} returned an invalid payload structure"
                ) from exc
            payload = dict(copied_candidate)
            if SCHEMA_VERSION_FIELD in payload:
                raise InvalidUpcastResultError(
                    f"upcasters must not emit reserved field {SCHEMA_VERSION_FIELD}"
                )
            version += 1

        decoded_payload = self._decode_current_payload(event_type, decoder, payload)

        return UpcastedEvent(
            stored_event=stored_event,
            payload=decoded_payload,
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
        busy_timeout_ms = self._validate_busy_timeout_seconds(busy_timeout_seconds)
        self._require_supported_sqlite()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=self._sqlite_connect_timeout_seconds(busy_timeout_ms),
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.set_authorizer(self._allow_all_authorizer)
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            if path != ":memory:":
                self._enable_wal(self._connection, busy_timeout_ms)
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

    @staticmethod
    def _validate_busy_timeout_seconds(value: object) -> int:
        """Return a 1-300000ms ceiling for one exact finite positive duration."""

        if type(value) is int:
            if not 0 < value <= MAX_PROJECTION_BUSY_TIMEOUT_SECONDS:
                raise ValueError(_PROJECTION_BUSY_TIMEOUT_SECONDS_ERROR)
            normalized_seconds = float(value)
        elif type(value) is float:
            if (
                not math.isfinite(value)
                or value <= 0
                or value > MAX_PROJECTION_BUSY_TIMEOUT_SECONDS
            ):
                raise ValueError(_PROJECTION_BUSY_TIMEOUT_SECONDS_ERROR)
            normalized_seconds = value
        else:
            raise ValueError(_PROJECTION_BUSY_TIMEOUT_SECONDS_ERROR)
        return max(1, math.ceil(normalized_seconds * 1000))

    @staticmethod
    def _sqlite_connect_timeout_seconds(busy_timeout_ms: int) -> float:
        """Preserve the normalized milliseconds through SQLite's truncation."""

        return math.nextafter(busy_timeout_ms / 1000, math.inf)

    @staticmethod
    def _require_supported_sqlite() -> None:
        if sqlite3.sqlite_version_info < MIN_PROJECTION_SQLITE_VERSION:
            minimum = ".".join(str(part) for part in MIN_PROJECTION_SQLITE_VERSION)
            raise ProjectionSchemaError(f"projection storage requires SQLite {minimum} or newer")

    @staticmethod
    def _enable_wal(
        connection: sqlite3.Connection,
        busy_timeout_ms: int,
    ) -> None:
        """Enable WAL despite SQLite's non-blocking journal-mode transition race."""

        deadline = monotonic() + (busy_timeout_ms / 1000)
        retry_delay = 0.001
        while True:
            try:
                cursor = connection.execute("PRAGMA journal_mode=WAL")
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
            except sqlite3.OperationalError as exc:
                message = str(exc).casefold()
                remaining = deadline - monotonic()
                if remaining <= 0 or ("locked" not in message and "busy" not in message):
                    raise
                sleep(min(retry_delay, remaining))
                retry_delay = min(retry_delay * 2, 0.05)
                continue
            if row is None or type(row[0]) is not str or row[0].casefold() != "wal":
                raise ProjectionSchemaError("projection database could not enable WAL mode")
            return

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
        # Views, triggers, and virtual tables retain executable behavior after the
        # restricted callback is removed. Projection handlers may not persist or
        # replace that deferred SQL/program boundary under any object name.
        if action_code in _HANDLER_DEFERRED_SCHEMA_ACTIONS:
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
        transaction: Optional[ProjectionTransaction] = None
        try:
            # Keep installation inside the cleanup boundary. A connection wrapper can
            # install the callback and then raise before returning to Python.
            connection.set_authorizer(cls._projection_handler_authorizer)
            transaction = ProjectionTransaction(connection)
            yield transaction
        finally:
            try:
                if transaction is not None:
                    try:
                        transaction._revoke()
                    finally:
                        transaction._ensure_revoked()
            finally:
                # Python 3.9 cannot reliably clear an authorizer with ``None``.
                # Reinstalling an explicit allow callback also invalidates cached
                # statements that were compiled under the restricted callback.
                connection.set_authorizer(cls._allow_all_authorizer)

    @staticmethod
    def _read_schema_rows(
        connection: sqlite3.Connection,
        sql: str,
        parameters: tuple[Any, ...],
        *,
        columns: tuple[str, ...],
        maximum: int,
        label: str,
    ) -> tuple[tuple[Any, ...], ...]:
        """Read one bounded, explicitly versioned SQLite result shape.

        New columns fail closed until this contract is deliberately reviewed.
        """

        try:
            cursor = connection.execute(sql, parameters)
            try:
                description = cursor.description
                if type(description) is not tuple or len(description) != len(columns):
                    raise ProjectionSchemaError(
                        f"projection schema {label} returned malformed columns"
                    )
                for index, column in enumerate(description):
                    if (
                        type(column) is not tuple
                        or len(column) != 7
                        or type(column[0]) is not str
                        or column[0] != columns[index]
                    ):
                        raise ProjectionSchemaError(
                            f"projection schema {label} returned malformed columns"
                        )
                raw_rows = cursor.fetchmany(maximum + 1)
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            raise ProjectionSchemaError(
                f"projection schema {label} could not be inspected"
            ) from exc
        if type(raw_rows) is not list:
            raise ProjectionSchemaError(f"projection schema {label} returned malformed rows")
        if len(raw_rows) > maximum:
            raise ProjectionSchemaError(f"projection schema {label} exceeds the inspection limit")
        rows: list[tuple[Any, ...]] = []
        for raw_row in raw_rows:
            if type(raw_row) is not tuple and type(raw_row) is not sqlite3.Row:
                raise ProjectionSchemaError(f"projection schema {label} returned malformed rows")
            row = tuple(raw_row)
            if len(row) != len(columns):
                raise ProjectionSchemaError(f"projection schema {label} returned malformed rows")
            rows.append(row)
        return tuple(rows)

    @staticmethod
    def _catalog_text(value: object, *, maximum: int) -> str:
        if type(value) is not str or not value or len(value) > maximum:
            raise ProjectionSchemaError("projection schema catalog row is malformed")
        return value

    @classmethod
    def _parse_schema_object(cls, raw_row: tuple[Any, ...]) -> _ProjectionSchemaObject:
        if len(raw_row) != 4:
            raise ProjectionSchemaError("projection schema catalog row is malformed")
        object_type = cls._catalog_text(raw_row[0], maximum=len("trigger"))
        name = cls._catalog_text(
            raw_row[1],
            maximum=_MAX_PROJECTION_SCHEMA_NAME_LENGTH,
        )
        table_name = cls._catalog_text(
            raw_row[2],
            maximum=_MAX_PROJECTION_SCHEMA_NAME_LENGTH,
        )
        raw_sql = raw_row[3]
        if raw_sql is None:
            schema_sql = None
        else:
            schema_sql = _canonical_projection_schema_sql(
                cls._catalog_text(
                    raw_sql,
                    maximum=_MAX_PROJECTION_SCHEMA_SQL_LENGTH,
                )
            )
        return _ProjectionSchemaObject(object_type, name, table_name, schema_sql)

    @staticmethod
    def _require_exact_schema_rows(
        raw_rows: tuple[tuple[Any, ...], ...],
        *,
        expected_rows: tuple[tuple[Any, ...], ...],
        label: str,
    ) -> None:
        if len(raw_rows) != len(expected_rows):
            raise ProjectionSchemaError(f"projection schema {label} rows are not exact")
        for raw_row, expected_row in zip(raw_rows, expected_rows):
            if len(raw_row) != len(expected_row):
                raise ProjectionSchemaError(f"projection schema {label} rows are not exact")
            for value, expected_value in zip(raw_row, expected_row):
                if type(value) is not type(expected_value):
                    raise ProjectionSchemaError(
                        f"projection schema {label} values have non-exact types"
                    )
            if raw_row != expected_row:
                raise ProjectionSchemaError(f"projection schema {label} rows are not exact")

    @classmethod
    def _require_exact_index_list_rows(
        cls,
        raw_rows: tuple[tuple[Any, ...], ...],
        *,
        expected_rows: tuple[tuple[Any, ...], ...],
        label: str,
    ) -> None:
        """Validate stable index metadata without assigning meaning to SQLite's seq."""

        if len(raw_rows) != len(expected_rows):
            raise ProjectionSchemaError(f"projection schema {label} rows are not exact")
        sequence_numbers: set[int] = set()
        stable_rows: list[tuple[Any, ...]] = []
        for raw_row in raw_rows:
            if len(raw_row) != 5:
                raise ProjectionSchemaError(f"projection schema {label} rows are not exact")
            sequence_number = raw_row[0]
            stable_row = raw_row[1:]
            if type(sequence_number) is not int or not 0 <= sequence_number < len(raw_rows):
                raise ProjectionSchemaError(
                    f"projection schema {label} sequence numbers are invalid"
                )
            if sequence_number in sequence_numbers:
                raise ProjectionSchemaError(
                    f"projection schema {label} sequence numbers are duplicated"
                )
            sequence_numbers.add(sequence_number)
            expected_types = (str, int, str, int)
            if any(
                type(value) is not expected for value, expected in zip(stable_row, expected_types)
            ):
                raise ProjectionSchemaError(
                    f"projection schema {label} values have non-exact types"
                )
            stable_rows.append(stable_row)
        cls._require_exact_schema_rows(
            tuple(sorted(stable_rows)),
            expected_rows=expected_rows,
            label=label,
        )

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection) -> _ProjectionSchemaState:
        """Inspect exact projection-owned catalog and stable PRAGMA fields."""

        catalog_rows = cls._read_schema_rows(
            connection,
            """
            SELECT type, name, tbl_name, sql
            FROM main.sqlite_master
            WHERE name COLLATE NOCASE IN (?, ?, ?)
               OR tbl_name COLLATE NOCASE IN (?, ?)
            ORDER BY type, name, tbl_name
            LIMIT ?
            """,
            (
                _PROJECTION_OFFSETS_TABLE_NAME,
                _PROJECTION_RECEIPTS_TABLE_NAME,
                _PROJECTION_RECEIPTS_POSITION_INDEX_NAME,
                _PROJECTION_OFFSETS_TABLE_NAME,
                _PROJECTION_RECEIPTS_TABLE_NAME,
                _MAX_PROJECTION_SCHEMA_OBJECTS + 1,
            ),
            columns=("type", "name", "tbl_name", "sql"),
            maximum=_MAX_PROJECTION_SCHEMA_OBJECTS,
            label="catalog",
        )
        if not catalog_rows:
            return _ProjectionSchemaState.ABSENT

        actual_objects = tuple(cls._parse_schema_object(row) for row in catalog_rows)
        actual_tables = {item.name for item in actual_objects if item.object_type == "table"}
        expected_tables = {
            _PROJECTION_OFFSETS_TABLE_NAME,
            _PROJECTION_RECEIPTS_TABLE_NAME,
        }
        if actual_tables != expected_tables:
            raise ProjectionSchemaError("projection tables must be both absent or both exact")
        if actual_objects != _EXPECTED_PROJECTION_SCHEMA_OBJECTS:
            raise ProjectionSchemaError(
                "projection schema differs from the exact packaged definition"
            )

        for table_name, expected_rows in _EXPECTED_PROJECTION_TABLE_INFO.items():
            raw_rows = cls._read_schema_rows(
                connection,
                f'PRAGMA main.table_info("{table_name}")',
                (),
                columns=("cid", "name", "type", "notnull", "dflt_value", "pk"),
                maximum=len(expected_rows),
                label=f"table_info for {table_name}",
            )
            cls._require_exact_schema_rows(
                raw_rows,
                expected_rows=expected_rows,
                label=f"table_info for {table_name}",
            )

        for table_name, expected_rows in _EXPECTED_PROJECTION_INDEX_LISTS.items():
            raw_rows = cls._read_schema_rows(
                connection,
                f'PRAGMA main.index_list("{table_name}")',
                (),
                columns=("seq", "name", "unique", "origin", "partial"),
                maximum=len(expected_rows),
                label=f"index_list for {table_name}",
            )
            cls._require_exact_index_list_rows(
                raw_rows,
                expected_rows=expected_rows,
                label=f"index_list for {table_name}",
            )

        for index_name, expected_rows in _EXPECTED_PROJECTION_INDEX_INFO.items():
            raw_rows = cls._read_schema_rows(
                connection,
                f'PRAGMA main.index_info("{index_name}")',
                (),
                columns=("seqno", "cid", "name"),
                maximum=len(expected_rows),
                label=f"index_info for {index_name}",
            )
            cls._require_exact_schema_rows(
                raw_rows,
                expected_rows=expected_rows,
                label=f"index_info for {index_name}",
            )

        for index_name, expected_rows in _EXPECTED_PROJECTION_INDEX_XINFO.items():
            raw_rows = cls._read_schema_rows(
                connection,
                f'PRAGMA main.index_xinfo("{index_name}")',
                (),
                columns=("seqno", "cid", "name", "desc", "coll", "key"),
                maximum=len(expected_rows),
                label=f"index_xinfo for {index_name}",
            )
            cls._require_exact_schema_rows(
                raw_rows,
                expected_rows=expected_rows,
                label=f"index_xinfo for {index_name}",
            )

        return _ProjectionSchemaState.EXACT

    def _initialize(self) -> None:
        with self._lock:
            if self._connection.in_transaction:
                raise ProjectionSchemaError(
                    "projection schema installation requires no active transaction"
                )
            preflight = self._validate_schema(self._connection)
            if preflight is _ProjectionSchemaState.EXACT:
                return

            owns_transaction = False
            try:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                finally:
                    # A wrapper may raise after SQLite acquires the lock.  Startup had
                    # no caller transaction, so every live transaction here is ours.
                    owns_transaction = self._connection.in_transaction
                if not self._connection.in_transaction:
                    raise ProjectionSchemaError(
                        "projection schema installation did not acquire a transaction"
                    )

                locked_state = self._validate_schema(self._connection)
                if locked_state is _ProjectionSchemaState.ABSENT:
                    for statement in _PROJECTION_SCHEMA_DDL:
                        self._connection.execute(statement)

                installed_state = self._validate_schema(self._connection)
                if installed_state is not _ProjectionSchemaState.EXACT:
                    raise ProjectionSchemaError(
                        "projection schema installation did not produce the exact schema"
                    )
                self._connection.execute("COMMIT")
                owns_transaction = False
            except BaseException:
                if owns_transaction and self._connection.in_transaction:
                    try:
                        self._connection.execute("ROLLBACK")
                    except BaseException:
                        # Closing the constructor-owned connection below is the final
                        # rollback boundary; preserve the original installation error.
                        self._connection.close()
                raise

            committed_state = self._validate_schema(self._connection)
            if committed_state is not _ProjectionSchemaState.EXACT:
                raise ProjectionSchemaError("committed projection schema is not exact")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection.in_transaction:
                raise ProjectionOffsetError("projection operation requires no active transaction")
            owns_transaction: bool = False
            try:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                except BaseException:
                    # A wrapper may raise after SQLite acquires the write lock.  If
                    # the ownership probe also fails, conservatively clean up the
                    # possible transaction without replacing the BEGIN failure.
                    owns_transaction = True
                    try:
                        owns_transaction = self._connection.in_transaction
                    except BaseException:
                        pass
                    raise
                owns_transaction = True
                owns_transaction = self._connection.in_transaction
                if not owns_transaction:
                    raise ProjectionOffsetError(
                        "projection operation did not acquire a transaction"
                    )
                yield self._connection
                self._connection.execute("COMMIT")
                owns_transaction = False
            except BaseException:
                if owns_transaction:
                    transaction_is_open: bool
                    try:
                        transaction_is_open = self._connection.in_transaction
                    except BaseException:
                        transaction_is_open = True
                    if transaction_is_open:
                        try:
                            self._connection.execute("ROLLBACK")
                        except BaseException:
                            # Closing is SQLite's final rollback boundary. Cleanup
                            # must never replace the operation's primary exception.
                            try:
                                self._connection.close()
                            except BaseException:
                                pass
                raise

    @staticmethod
    def _validate_name(value: str, field_name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError(f"{field_name} must be non-empty text without edge whitespace")
        if len(value) > MAX_PROJECTION_IDENTIFIER_LENGTH:
            raise ValueError(f"{field_name} exceeds {MAX_PROJECTION_IDENTIFIER_LENGTH} characters")
        return value

    @classmethod
    def _validate_position(cls, value: int, field_name: str) -> int:
        if type(value) is not int or not 0 <= value <= cls._MAX_SQLITE_INTEGER:
            raise ValueError(f"{field_name} must be a non-negative 64-bit SQLite integer")
        return value

    @staticmethod
    def _validate_lease_seconds(value: object) -> float:
        if type(value) is int:
            if not 0 < value <= MAX_PROJECTION_LEASE_SECONDS:
                raise ValueError(_PROJECTION_LEASE_SECONDS_ERROR)
            return float(value)
        if type(value) is float:
            if not math.isfinite(value) or value <= 0 or value > MAX_PROJECTION_LEASE_SECONDS:
                raise ValueError(_PROJECTION_LEASE_SECONDS_ERROR)
            return value
        raise ValueError(_PROJECTION_LEASE_SECONDS_ERROR)

    @classmethod
    def _validate_lease(cls, lease: ProjectionLease) -> ProjectionLease:
        if type(lease) is not ProjectionLease:
            raise TypeError("lease must be a ProjectionLease")
        cls._validate_name(lease.projection_name, "lease projection_name")
        cls._validate_name(lease.owner_id, "lease owner_id")
        if type(lease.owner_epoch) is not int or not (
            1 <= lease.owner_epoch <= cls._MAX_SQLITE_INTEGER
        ):
            raise ValueError("lease owner_epoch must be a positive 64-bit SQLite integer")
        normalized_deadline = cls._normalize_timestamp(
            lease.lease_expires_at,
            "lease lease_expires_at",
        )
        if normalized_deadline != lease.lease_expires_at:
            raise ValueError("lease lease_expires_at must be canonical UTC")
        return lease

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
    def _lease_deadline(cls, now: str, lease_seconds: object) -> str:
        normalized_seconds = cls._validate_lease_seconds(lease_seconds)
        try:
            parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
            duration = timedelta(seconds=normalized_seconds)
            if duration <= timedelta(0):
                # Positive sub-microsecond floats are valid input but must still
                # produce a live lease at the persistence timestamp's resolution.
                duration = timedelta.resolution
            deadline = parsed + duration
            return deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except OverflowError as exc:
            raise ValueError("lease deadline exceeds the supported datetime range") from exc

    @staticmethod
    def _is_after(left: str, right: str) -> bool:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) > datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )

    def _now(self) -> str:
        return self._normalize_timestamp(self._clock(), "clock")

    @staticmethod
    def _persisted_text(value: object, field_name: str, *, maximum: int) -> str:
        if type(value) is not str:
            raise ProjectionIntegrityError(f"persisted {field_name} must be text")
        if not value or value != value.strip():
            raise ProjectionIntegrityError(
                f"persisted {field_name} must be non-empty without edge whitespace"
            )
        if len(value) > maximum:
            raise ProjectionIntegrityError(f"persisted {field_name} exceeds {maximum} characters")
        return value

    @classmethod
    def _persisted_identifier(cls, value: object, field_name: str) -> str:
        return cls._persisted_text(
            value,
            field_name,
            maximum=MAX_PROJECTION_IDENTIFIER_LENGTH,
        )

    @classmethod
    def _persisted_integer(
        cls,
        value: object,
        field_name: str,
        *,
        minimum: int,
    ) -> int:
        if type(value) is not int:
            raise ProjectionIntegrityError(f"persisted {field_name} must be an integer")
        if not minimum <= value <= cls._MAX_SQLITE_INTEGER:
            raise ProjectionIntegrityError(
                f"persisted {field_name} must be between {minimum} and {cls._MAX_SQLITE_INTEGER}"
            )
        return value

    @classmethod
    def _persisted_timestamp(cls, value: object, field_name: str) -> str:
        timestamp = cls._persisted_text(value, field_name, maximum=32)
        try:
            normalized = cls._normalize_timestamp(timestamp, field_name)
        except ValueError as exc:
            raise ProjectionIntegrityError(
                f"persisted {field_name} must be a valid canonical UTC timestamp"
            ) from exc
        if timestamp != normalized:
            raise ProjectionIntegrityError(
                f"persisted {field_name} must be a canonical UTC timestamp"
            )
        return timestamp

    @classmethod
    def _row_to_offset(cls, row: sqlite3.Row) -> ProjectionOffset:
        try:
            projection_name = cls._persisted_identifier(
                row["projection_name"],
                "projection_name",
            )
            last_global_position = cls._persisted_integer(
                row["last_global_position"],
                "last_global_position",
                minimum=0,
            )
            owner_id = cls._persisted_identifier(row["owner_id"], "owner_id")
            owner_epoch = cls._persisted_integer(
                row["owner_epoch"],
                "owner_epoch",
                minimum=1,
            )
            lease_expires_at = cls._persisted_timestamp(
                row["lease_expires_at"],
                "lease_expires_at",
            )
            updated_at = cls._persisted_timestamp(row["updated_at"], "updated_at")
        except ProjectionIntegrityError:
            raise
        except (IndexError, KeyError, TypeError) as exc:
            raise ProjectionIntegrityError("persisted projection offset row is incomplete") from exc
        return ProjectionOffset(
            projection_name=projection_name,
            last_global_position=last_global_position,
            owner_id=owner_id,
            owner_epoch=owner_epoch,
            lease_expires_at=lease_expires_at,
            updated_at=updated_at,
        )

    @classmethod
    def _row_to_receipt(cls, row: sqlite3.Row) -> _ProjectionReceipt:
        try:
            projection_name = cls._persisted_identifier(
                row["projection_name"],
                "receipt projection_name",
            )
            event_id = cls._persisted_identifier(row["event_id"], "receipt event_id")
            global_position = cls._persisted_integer(
                row["global_position"],
                "receipt global_position",
                minimum=1,
            )
            applied_at = cls._persisted_timestamp(
                row["applied_at"],
                "receipt applied_at",
            )
        except ProjectionIntegrityError:
            raise
        except (IndexError, KeyError, TypeError) as exc:
            raise ProjectionIntegrityError(
                "persisted projection receipt row is incomplete"
            ) from exc
        return _ProjectionReceipt(
            projection_name=projection_name,
            event_id=event_id,
            global_position=global_position,
            applied_at=applied_at,
        )

    @classmethod
    def _offset_for_projection(
        cls,
        row: sqlite3.Row,
        projection_name: str,
    ) -> ProjectionOffset:
        offset = cls._row_to_offset(row)
        if offset.projection_name != projection_name:
            raise ProjectionIntegrityError(
                "persisted projection offset identity contradicts its lookup"
            )
        return offset

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
        return self._offset_for_projection(row, projection_name)

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
        normalized_lease_seconds = self._validate_lease_seconds(lease_seconds)
        with self._transaction() as connection:
            now = self._now()
            deadline = self._lease_deadline(now, normalized_lease_seconds)
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
                offset = self._offset_for_projection(row, projection_name)
                persisted_owner = offset.owner_id
                persisted_deadline = offset.lease_expires_at
                if persisted_owner is None or persisted_deadline is None:
                    raise ProjectionIntegrityError(
                        "persisted projection lease metadata is incomplete"
                    )
                if self._is_after(persisted_deadline, now) and persisted_owner != owner_id:
                    raise ProjectionLeaseConflictError(
                        f"projection {projection_name} is leased by another owner"
                    )
                previous_epoch = offset.owner_epoch
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

        normalized_lease_seconds = self._validate_lease_seconds(lease_seconds)
        lease = self._validate_lease(lease)
        with self._transaction() as connection:
            now = self._now()
            deadline = self._lease_deadline(now, normalized_lease_seconds)
            row = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if row is None:
                raise ProjectionLeaseLostError("projection lease does not exist")
            offset = self._offset_for_projection(row, lease.projection_name)
            persisted_deadline = offset.lease_expires_at
            if (
                offset.owner_id != lease.owner_id
                or offset.owner_epoch != lease.owner_epoch
                or persisted_deadline is None
                or not self._is_after(persisted_deadline, now)
            ):
                raise ProjectionLeaseLostError("projection lease is stale or expired")
            cursor = connection.execute(
                """
                UPDATE projection_offsets
                SET lease_expires_at = ?, updated_at = ?
                WHERE projection_name = ? AND owner_id = ? AND owner_epoch = ?
                  AND lease_expires_at = ?
                """,
                (
                    deadline,
                    now,
                    lease.projection_name,
                    lease.owner_id,
                    lease.owner_epoch,
                    persisted_deadline,
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

        lease = self._validate_lease(lease)
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
            offset = self._offset_for_projection(row, lease.projection_name)
            persisted_deadline = offset.lease_expires_at
            if (
                offset.owner_id != lease.owner_id
                or offset.owner_epoch != lease.owner_epoch
                or persisted_deadline is None
                or not self._is_after(persisted_deadline, now)
            ):
                raise ProjectionLeaseLostError("projection lease is stale or expired")
            if offset.last_global_position != expected_position:
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
            return self._offset_for_projection(updated, lease.projection_name)

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

        lease = self._validate_lease(lease)
        expected_position = self._validate_position(expected_position, "expected_position")
        if not callable(handler):
            raise TypeError("handler must be callable")
        stored_event = event.stored_event
        position = stored_event.global_position
        if type(position) is not int or not 1 <= position <= self._MAX_SQLITE_INTEGER:
            raise ValueError("event global_position must be a positive 64-bit SQLite integer")
        event_id = self._validate_name(stored_event.event.event_id, "event_id")

        with self._transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if row is None:
                raise ProjectionLeaseLostError("projection lease does not exist")
            offset = self._offset_for_projection(row, lease.projection_name)
            persisted_deadline = offset.lease_expires_at
            if (
                offset.owner_id != lease.owner_id
                or offset.owner_epoch != lease.owner_epoch
                or persisted_deadline is None
                or not self._is_after(persisted_deadline, now)
            ):
                raise ProjectionLeaseLostError("projection lease is stale or expired")

            persisted_position = offset.last_global_position
            receipt = connection.execute(
                """
                SELECT * FROM projection_receipts
                WHERE projection_name = ? AND event_id = ?
                """,
                (lease.projection_name, event_id),
            ).fetchone()
            if receipt is not None:
                decoded_receipt = self._row_to_receipt(receipt)
                if (
                    decoded_receipt.projection_name != lease.projection_name
                    or decoded_receipt.event_id != event_id
                ):
                    raise ProjectionIntegrityError(
                        "persisted projection receipt identity contradicts its lookup"
                    )
                if decoded_receipt.global_position != position or persisted_position < position:
                    raise ProjectionReceiptConflictError(
                        "projection receipt contradicts the event log or checkpoint"
                    )
                return ProjectionApplyResult(False, offset)

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
            return ProjectionApplyResult(
                True,
                self._offset_for_projection(updated, lease.projection_name),
            )

    def release(self, lease: ProjectionLease) -> None:
        """Expire a live lease immediately; stale releases fail closed."""

        lease = self._validate_lease(lease)
        with self._transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM projection_offsets WHERE projection_name = ?",
                (lease.projection_name,),
            ).fetchone()
            if row is None:
                raise ProjectionLeaseLostError("projection lease does not exist")
            offset = self._offset_for_projection(row, lease.projection_name)
            persisted_deadline = offset.lease_expires_at
            if (
                offset.owner_id != lease.owner_id
                or offset.owner_epoch != lease.owner_epoch
                or persisted_deadline is None
                or not self._is_after(persisted_deadline, now)
            ):
                raise ProjectionLeaseLostError("projection lease is stale or expired")
            cursor = connection.execute(
                """
                UPDATE projection_offsets
                SET lease_expires_at = ?, updated_at = ?
                WHERE projection_name = ? AND owner_id = ? AND owner_epoch = ?
                  AND lease_expires_at = ?
                """,
                (
                    now,
                    now,
                    lease.projection_name,
                    lease.owner_id,
                    lease.owner_epoch,
                    persisted_deadline,
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
        normalized_lease_seconds = offset_store._validate_lease_seconds(  # noqa: SLF001
            lease_seconds
        )
        self.projection_name = offset_store._validate_name(  # noqa: SLF001
            projection_name, "projection_name"
        )
        self.owner_id = offset_store._validate_name(owner_id, "owner_id")  # noqa: SLF001
        if not callable(getattr(event_source, "read_all", None)):
            raise TypeError("event_source must provide read_all")
        if not isinstance(registry, EventUpcasterRegistry):
            raise TypeError("registry must be an EventUpcasterRegistry")
        registry.require_sealed()
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.event_source = event_source
        self.offset_store = offset_store
        self.registry = registry
        self.handler = handler
        self.lease_seconds = normalized_lease_seconds

    @staticmethod
    def _validate_source_batch(
        events: object,
        *,
        after_position: int,
        limit: int,
    ) -> tuple[StoredEvent, ...]:
        """Validate a complete source result before any event can be processed."""

        if type(events) is not tuple:
            raise ProjectionSourceIntegrityError("event source must return an exact tuple batch")
        if len(events) > limit:
            raise ProjectionSourceIntegrityError("event source batch exceeds the requested limit")

        expected_position = after_position + 1
        for batch_index, candidate in enumerate(events):
            if type(candidate) is not StoredEvent:
                raise ProjectionSourceIntegrityError(
                    f"event source item {batch_index} must be an exact StoredEvent"
                )
            position = candidate.global_position
            if type(position) is not int or not (
                1 <= position <= SQLiteProjectionOffsetStore._MAX_SQLITE_INTEGER
            ):
                raise ProjectionSourceIntegrityError(
                    f"event source item {batch_index} has an invalid global position"
                )
            if position != expected_position:
                raise ProjectionSourceIntegrityError(
                    f"event source item {batch_index} is not globally contiguous"
                )
            expected_position += 1
        return events

    def run_once(self, *, limit: int = 100) -> ProjectionRunResult:
        """Validate then project one bounded batch and release ownership when still live."""

        if type(limit) is not int or not 1 <= limit <= MAX_PROJECTION_BATCH_SIZE:
            raise ValueError(
                f"limit must be an exact integer between 1 and {MAX_PROJECTION_BATCH_SIZE}"
            )
        primary_error: Optional[BaseException] = None
        lease = self.offset_store.claim(
            self.projection_name,
            self.owner_id,
            lease_seconds=self.lease_seconds,
        )
        try:
            scanned = 0
            applied = 0
            deduplicated = 0
            last_position = self.offset_store.load(self.projection_name).last_global_position
            source_result = self.event_source.read_all(
                after_position=last_position,
                limit=limit,
            )
            events = self._validate_source_batch(
                source_result,
                after_position=last_position,
                limit=limit,
            )
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
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.offset_store.release(lease)
            except ProjectionLeaseLostError:
                # A long handler may outlive its lease. The transactional apply either
                # committed under the SQLite write lock or rolled back before takeover.
                pass
            except BaseException:
                if primary_error is None:
                    raise
