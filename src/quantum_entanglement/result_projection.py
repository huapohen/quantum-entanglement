# ruff: noqa: UP006, UP035, UP045
"""A tenant-scoped, result-only business read model.

The result authority remains in :mod:`quantum_entanglement.store`.  This module is a
deliberately narrow consumer of the committed result and terminal events.  It uses the
existing leased projector so a read model can be rebuilt or replayed without becoming a
second source of truth.  No result body, lease token, credential, connector or outbox
authority is copied into the projection.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Optional, Tuple

from . import process_identity as _process_identity
from .invocation_execution import (
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    TASK_INVOCATION_STARTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
)
from .invocation_results import (
    TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
    ScopedInvocationResultEvidenceV2,
    ScopedInvocationResultTerminalTransitionV2,
)
from .projections import (
    DurableProjector,
    EventUpcasterRegistry,
    ProjectionRunResult,
    ProjectionTransaction,
    SQLiteProjectionOffsetStore,
    UpcastedEvent,
)
from .protocol import utc_now

RESULT_PROJECTION_TABLE = "task_result_projection_v1"
RESULT_PROJECTION_NAME = "task-result-v1"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_MAX_TEXT_BYTES = 4_096
_MAX_SQLITE_INTEGER = (1 << 63) - 1

_RESULT_PROJECTION_PROCESS_QUARANTINE: list[object] = []

_RESULT_PROJECTION_TABLE_SQL = """
CREATE TABLE task_result_projection_v1 (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    result_event_id TEXT NOT NULL,
    terminal_event_id TEXT,
    running_task_revision INTEGER NOT NULL,
    terminal_task_revision INTEGER,
    accepted_at TEXT NOT NULL,
    artifact_count INTEGER NOT NULL,
    result_manifest_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, invocation_id),
    UNIQUE (tenant_id, workspace_id, receipt_id),
    CHECK (running_task_revision > 0),
    CHECK (terminal_task_revision IS NULL OR terminal_task_revision = running_task_revision + 1),
    CHECK (artifact_count >= 0),
    CHECK (status IN ('result_accepted', 'completed'))
)
""".strip()

_RESULT_PROJECTION_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "invocation_id",
    "session_id",
    "plan_id",
    "task_id",
    "agent_id",
    "result_ref",
    "receipt_id",
    "result_event_id",
    "terminal_event_id",
    "running_task_revision",
    "terminal_task_revision",
    "accepted_at",
    "artifact_count",
    "result_manifest_digest",
    "status",
    "updated_at",
)
_TERMINAL_MARKERS = frozenset(
    {
        "transitionKind",
        "resultReceiptId",
        "resultEventId",
        "resultEvidenceDigest",
        "runningTaskRevision",
        "terminalTaskRevision",
    }
)


class ResultProjectionError(RuntimeError):
    """Base error for a malformed or conflicting result read model."""


class ResultProjectionConflictError(ResultProjectionError):
    """Raised when two committed result events claim one projection key differently."""


class ResultProjectionSchemaError(ResultProjectionError):
    """Raised when the projection-owned table is absent in part or has drifted."""


class ResultProjectionProcessMismatchError(ResultProjectionError):
    """Raised when a projection instance is used after a fork or PID drift."""


class ResultProjectionStatus(str, Enum):
    """The only business states materialized by this result-only projection."""

    RESULT_ACCEPTED = "result_accepted"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ProjectedResultTask:
    """Minimal tenant-scoped task view derived from a complete result event pair."""

    tenant_id: str
    workspace_id: str
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    result_ref: str
    receipt_id: str
    result_event_id: str
    terminal_event_id: Optional[str]
    running_task_revision: int
    terminal_task_revision: Optional[int]
    accepted_at: str
    artifact_count: int
    result_manifest_digest: str
    status: ResultProjectionStatus
    updated_at: str

    def __post_init__(self) -> None:
        if type(self) is not ProjectedResultTask:
            raise TypeError("projected result task must be exact")
        for field_name in (
            "tenant_id",
            "workspace_id",
            "invocation_id",
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "result_ref",
            "receipt_id",
            "result_event_id",
        ):
            _text(getattr(self, field_name), field_name)
        if self.terminal_event_id is not None:
            _text(self.terminal_event_id, "terminal_event_id")
        if type(self.running_task_revision) is not int or not (
            0 < self.running_task_revision < _MAX_SQLITE_INTEGER
        ):
            raise ValueError("running_task_revision is outside its supported range")
        if self.terminal_task_revision is not None and (
            type(self.terminal_task_revision) is not int
            or self.terminal_task_revision != self.running_task_revision + 1
        ):
            raise ValueError("terminal_task_revision must follow running_task_revision")
        if type(self.artifact_count) is not int or not 0 <= self.artifact_count <= 256:
            raise ValueError("artifact_count is outside its supported range")
        _digest(self.result_manifest_digest, "result_manifest_digest")
        _timestamp(self.accepted_at, "accepted_at")
        _timestamp(self.updated_at, "updated_at")
        if type(self.status) is not ResultProjectionStatus:
            raise TypeError("status must be an exact ResultProjectionStatus")
        if self.status is ResultProjectionStatus.COMPLETED:
            if self.terminal_event_id is None or self.terminal_task_revision is None:
                raise ValueError("completed projection requires terminal coordinates")
        elif self.terminal_event_id is not None or self.terminal_task_revision is not None:
            raise ValueError("result-accepted projection cannot have terminal coordinates")


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ResultProjectionError(f"{field_name} must be non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ResultProjectionError(f"{field_name} must be valid UTF-8") from None
    if len(encoded) > _MAX_TEXT_BYTES:
        raise ResultProjectionError(f"{field_name} exceeds its UTF-8 byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ResultProjectionError(f"{field_name} contains a control character")
    return value


def _digest(value: object, field_name: str) -> str:
    value = _text(value, field_name)
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ResultProjectionError(f"{field_name} must be canonical SHA-256")
    return value


def _timestamp(value: object, field_name: str) -> str:
    value = _text(value, field_name)
    if _CANONICAL_UTC_PATTERN.fullmatch(value) is None:
        raise ResultProjectionError(f"{field_name} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ResultProjectionError(f"{field_name} must be a valid timestamp") from None
    if (
        parsed.tzinfo is None
        or parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        != value
    ):
        raise ResultProjectionError(f"{field_name} must be canonical UTC")
    return value


def _decode_passthrough(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Register known non-result events without giving them projection authority."""

    return dict(payload)


def _decode_result_evidence(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = ScopedInvocationResultEvidenceV2.from_dict(dict(payload))
    return evidence.to_dict()


def _decode_status(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if _TERMINAL_MARKERS.intersection(payload):
        transition = ScopedInvocationResultTerminalTransitionV2.from_dict(dict(payload))
        return transition.to_dict()
    return dict(payload)


def build_result_projection_registry() -> EventUpcasterRegistry:
    """Build and seal the known event vocabulary for the result projection."""

    registry = EventUpcasterRegistry()
    for event_type in (
        "workflow.plan.created",
        "task.created",
        "context.compiled",
        "approval.requested",
        "approval.decided",
        "task.result.received",
        "artifact.versioned",
        TASK_EXECUTION_REQUESTED_EVENT_TYPE,
        TASK_INVOCATION_STARTED_EVENT_TYPE,
    ):
        registry.register_event_type(event_type, current_version=1, decoder=_decode_passthrough)
    registry.register_event_type(
        TASK_STATUS_CHANGED_EVENT_TYPE,
        current_version=1,
        decoder=_decode_status,
    )
    registry.register_event_type(
        TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
        current_version=1,
        decoder=_decode_result_evidence,
    )
    registry.seal()
    return registry


def _canonical_table_sql(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())


def _expected_table_info() -> Tuple[Tuple[Any, ...], ...]:
    rows = []
    for index, column in enumerate(_RESULT_PROJECTION_COLUMNS):
        if column in {
            "running_task_revision",
            "terminal_task_revision",
            "artifact_count",
        }:
            sqlite_type = "INTEGER"
        else:
            sqlite_type = "TEXT"
        notnull = 0 if column in {"terminal_event_id", "terminal_task_revision"} else 1
        primary_key = {
            "tenant_id": 1,
            "workspace_id": 2,
            "invocation_id": 3,
        }.get(column, 0)
        rows.append((index, column, sqlite_type, notnull, None, primary_key))
    return tuple(rows)


class SQLiteResultProjectionStore:
    """Candidate result view backed by the existing fenced durable projector.

    The event source and projection database must be the same durable SQLite file for a
    rebuild to observe one event history.  The class is intentionally not wired into a
    service composition root; it is a local, authenticated-scope candidate until the
    remaining process and compatibility gates are closed.
    """

    def __init__(
        self,
        event_source: Any,
        path: str,
        *,
        owner_id: str = "result-projector-1",
        projection_name: str = RESULT_PROJECTION_NAME,
        clock: Any = utc_now,
        lease_seconds: float = 30.0,
    ) -> None:
        self._process_owner = _process_identity.capture_process_owner()
        self._require_current_process()
        if type(path) is not str or not path:
            raise ValueError("projection path is required")
        if not callable(getattr(event_source, "read_all", None)):
            raise TypeError("event_source must provide read_all")
        self._lock = RLock()
        self._offset_store = SQLiteProjectionOffsetStore(path, clock=clock)
        self._connection = self._offset_store._connection  # noqa: SLF001
        self._initialize_table()
        self._projector = DurableProjector(
            projection_name,
            owner_id,
            event_source,
            self._offset_store,
            build_result_projection_registry(),
            self._handle_event,
            lease_seconds=lease_seconds,
        )

    def _require_current_process(self) -> None:
        _process_identity.require_current_process(
            self._process_owner,
            ResultProjectionProcessMismatchError,
        )

    def _process_is_current(self) -> bool:
        try:
            self._require_current_process()
        except ResultProjectionProcessMismatchError:
            return False
        return True

    def __del__(self) -> None:
        try:
            if not self._process_is_current():
                _RESULT_PROJECTION_PROCESS_QUARANTINE.append(self)
        except BaseException:
            # Interpreter teardown may clear module globals; inherited resources are
            # still intentionally not closed by a child finalizer.
            pass

    def _initialize_table(self) -> None:
        with self._lock:
            with self._offset_store._transaction() as connection:  # noqa: SLF001
                row = connection.execute(
                    "SELECT type, name, tbl_name, sql FROM main.sqlite_master WHERE name = ?",
                    (RESULT_PROJECTION_TABLE,),
                ).fetchone()
                if row is None:
                    connection.execute(_RESULT_PROJECTION_TABLE_SQL)
                else:
                    if tuple(row) != (
                        "table",
                        RESULT_PROJECTION_TABLE,
                        RESULT_PROJECTION_TABLE,
                        _RESULT_PROJECTION_TABLE_SQL,
                    ):
                        if _canonical_table_sql(str(row[3])) != _canonical_table_sql(
                            _RESULT_PROJECTION_TABLE_SQL
                        ):
                            raise ResultProjectionSchemaError(
                                "result projection table SQL differs from the packaged contract"
                            )
                rows = connection.execute(
                    f'PRAGMA main.table_info("{RESULT_PROJECTION_TABLE}")'
                ).fetchall()
                actual = tuple(tuple(item) for item in rows)
                expected = _expected_table_info()
                if actual != expected:
                    raise ResultProjectionSchemaError(
                        "result projection table columns differ from the packaged contract"
                    )

    @staticmethod
    def _result_row_to_view(row: sqlite3.Row) -> ProjectedResultTask:
        try:
            values = tuple(row[column] for column in _RESULT_PROJECTION_COLUMNS)
        except (IndexError, KeyError, TypeError) as error:
            raise ResultProjectionSchemaError("result projection row is malformed") from error
        try:
            status = ResultProjectionStatus(values[16])
            return ProjectedResultTask(
                tenant_id=values[0],
                workspace_id=values[1],
                invocation_id=values[2],
                session_id=values[3],
                plan_id=values[4],
                task_id=values[5],
                agent_id=values[6],
                result_ref=values[7],
                receipt_id=values[8],
                result_event_id=values[9],
                terminal_event_id=values[10],
                running_task_revision=values[11],
                terminal_task_revision=values[12],
                accepted_at=values[13],
                artifact_count=values[14],
                result_manifest_digest=values[15],
                status=status,
                updated_at=values[17],
            )
        except (ResultProjectionError, TypeError, ValueError) as error:
            raise ResultProjectionSchemaError(
                "result projection row violates its contract"
            ) from error

    @classmethod
    def _handle_result_event(
        cls,
        transaction: ProjectionTransaction,
        event: UpcastedEvent,
    ) -> None:
        evidence = ScopedInvocationResultEvidenceV2.from_dict(dict(event.payload))
        result_event_id = _text(event.stored_event.event.event_id, "result_event_id")
        result_event_timestamp = _timestamp(
            event.stored_event.event.timestamp,
            "result_event_timestamp",
        )
        if result_event_timestamp != evidence.accepted_at:
            raise ResultProjectionConflictError(
                "result event timestamp does not match accepted timestamp"
            )
        identity_collision = transaction.execute(
            f"SELECT tenant_id, workspace_id, invocation_id FROM {RESULT_PROJECTION_TABLE} "
            "WHERE receipt_id = ? OR result_event_id = ?",
            (evidence.receipt_id, result_event_id),
        )
        if identity_collision.rows:
            raise ResultProjectionConflictError(
                "result receipt or event identity is already projected"
            )
        existing = transaction.execute(
            f"SELECT {_RESULT_PROJECTION_COLUMNS[0]}, {_RESULT_PROJECTION_COLUMNS[1]}, "
            f"{_RESULT_PROJECTION_COLUMNS[2]} FROM {RESULT_PROJECTION_TABLE} "
            "WHERE tenant_id = ? AND workspace_id = ? AND invocation_id = ?",
            (evidence.tenant_id, evidence.workspace_id, evidence.invocation_id),
        )
        if existing.rows:
            raise ResultProjectionConflictError(
                "result event conflicts with an existing projection identity"
            )
        transaction.execute(
            f"INSERT INTO {RESULT_PROJECTION_TABLE} ("
            "tenant_id, workspace_id, invocation_id, session_id, plan_id, task_id, agent_id, "
            "result_ref, receipt_id, result_event_id, terminal_event_id, running_task_revision, "
            "terminal_task_revision, accepted_at, artifact_count, result_manifest_digest, status, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)",
            (
                evidence.tenant_id,
                evidence.workspace_id,
                evidence.invocation_id,
                evidence.session_id,
                evidence.plan_id,
                evidence.task_id,
                evidence.agent_id,
                evidence.result_ref,
                evidence.receipt_id,
                result_event_id,
                evidence.running_task_revision,
                evidence.accepted_at,
                evidence.artifact_count,
                evidence.result_manifest_digest,
                ResultProjectionStatus.RESULT_ACCEPTED.value,
                evidence.accepted_at,
            ),
        )

    @staticmethod
    def _handle_terminal_event(
        transaction: ProjectionTransaction,
        event: UpcastedEvent,
    ) -> None:
        transition = ScopedInvocationResultTerminalTransitionV2.from_dict(dict(event.payload))
        terminal_event_id = _text(event.stored_event.event.event_id, "terminal_event_id")
        terminal_event_timestamp = _timestamp(
            event.stored_event.event.timestamp,
            "terminal_event_timestamp",
        )
        result = transaction.execute(
            f"SELECT {_RESULT_PROJECTION_COLUMNS[3]}, {_RESULT_PROJECTION_COLUMNS[4]}, "
            f"{_RESULT_PROJECTION_COLUMNS[5]}, {_RESULT_PROJECTION_COLUMNS[6]}, "
            f"{_RESULT_PROJECTION_COLUMNS[8]}, {_RESULT_PROJECTION_COLUMNS[9]}, "
            f"{_RESULT_PROJECTION_COLUMNS[11]}, {_RESULT_PROJECTION_COLUMNS[13]}, "
            f"{_RESULT_PROJECTION_COLUMNS[16]} "
            f"FROM {RESULT_PROJECTION_TABLE} WHERE tenant_id = ? AND workspace_id = ? "
            "AND invocation_id = ?",
            (transition.tenant_id, transition.workspace_id, transition.invocation_id),
        )
        if len(result.rows) != 1:
            raise ResultProjectionConflictError(
                "terminal result event has no unique result projection"
            )
        (
            session_id,
            plan_id,
            task_id,
            agent_id,
            receipt_id,
            result_event_id,
            running_revision,
            accepted_at,
            status,
        ) = result.rows[0]
        if (
            session_id != transition.session_id
            or plan_id != transition.plan_id
            or task_id != transition.task_id
            or agent_id != transition.agent_id
            or receipt_id != transition.result_receipt_id
            or result_event_id != transition.result_event_id
            or running_revision != transition.running_task_revision
            or status != ResultProjectionStatus.RESULT_ACCEPTED.value
        ):
            raise ResultProjectionConflictError("terminal event does not match result projection")
        if terminal_event_timestamp != accepted_at:
            raise ResultProjectionConflictError(
                "terminal event timestamp does not match accepted timestamp"
            )
        terminal_identity_collision = transaction.execute(
            f"SELECT invocation_id FROM {RESULT_PROJECTION_TABLE} "
            "WHERE terminal_event_id = ?",
            (terminal_event_id,),
        )
        if terminal_identity_collision.rows:
            raise ResultProjectionConflictError("terminal event identity is already projected")
        transaction.execute(
            f"UPDATE {RESULT_PROJECTION_TABLE} SET terminal_event_id = ?, "
            "terminal_task_revision = ?, status = ?, updated_at = ? "
            "WHERE tenant_id = ? AND workspace_id = ? AND invocation_id = ? "
            "AND status = ?",
            (
                terminal_event_id,
                transition.terminal_task_revision,
                ResultProjectionStatus.COMPLETED.value,
                event.stored_event.event.timestamp,
                transition.tenant_id,
                transition.workspace_id,
                transition.invocation_id,
                ResultProjectionStatus.RESULT_ACCEPTED.value,
            ),
        )

    @classmethod
    def _handle_event(cls, transaction: ProjectionTransaction, event: UpcastedEvent) -> None:
        if event.stored_event.event.event_type == TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE:
            cls._handle_result_event(transaction, event)
        elif event.stored_event.event.event_type == TASK_STATUS_CHANGED_EVENT_TYPE and (
            _TERMINAL_MARKERS.intersection(event.payload)
        ):
            cls._handle_terminal_event(transaction, event)

    def run_once(self, *, limit: int = 100) -> ProjectionRunResult:
        """Project one bounded, leased event page and return sanitized run telemetry."""

        self._require_current_process()
        return self._projector.run_once(limit=limit)

    def read(
        self,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
    ) -> Optional[ProjectedResultTask]:
        """Read one scope-bound task view without returning result body or lease data."""

        self._require_current_process()
        tenant_id = _text(tenant_id, "tenant_id")
        workspace_id = _text(workspace_id, "workspace_id")
        invocation_id = _text(invocation_id, "invocation_id")
        with self._lock:
            row = self._connection.execute(
                f"SELECT {', '.join(_RESULT_PROJECTION_COLUMNS)} FROM {RESULT_PROJECTION_TABLE} "
                "WHERE tenant_id = ? AND workspace_id = ? AND invocation_id = ?",
                (tenant_id, workspace_id, invocation_id),
            ).fetchone()
        if row is None:
            return None
        return self._result_row_to_view(row)

    def close(self) -> None:
        """Close the offset and projection connection."""

        self._require_current_process()
        with self._lock:
            self._offset_store.close()


__all__ = [
    "RESULT_PROJECTION_NAME",
    "RESULT_PROJECTION_TABLE",
    "ProjectedResultTask",
    "ResultProjectionConflictError",
    "ResultProjectionError",
    "ResultProjectionProcessMismatchError",
    "ResultProjectionSchemaError",
    "ResultProjectionStatus",
    "SQLiteResultProjectionStore",
    "build_result_projection_registry",
]
