# ruff: noqa: UP006, UP035, UP045
"""Durable SQLite invocation leasing with heartbeat and fencing.

The coordination event log describes what should happen.  This module owns the
separate, mutable execution projection needed to decide which worker may perform an
invocation now.  Every ownership-changing operation uses ``BEGIN IMMEDIATE`` and a
compare-and-set over both an opaque lease token and a monotonically increasing lease
epoch.  An expired worker therefore cannot heartbeat or publish a terminal state after
another worker has reclaimed the invocation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Tuple

from .migrations import (
    MigrationDriftError,
    apply_sqlite_migrations,
    current_schema_version,
)
from .protocol import new_id, utc_now

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_MAX_ERROR_LENGTH = 4_096
_MAX_SQLITE_INTEGER = (1 << 63) - 1


class InvocationConflictError(RuntimeError):
    """Raised when an idempotency boundary is reused for different work."""


class InvocationIntegrityError(RuntimeError):
    """Raised when persisted invocation state violates its durable contract."""


class InvocationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class AttemptStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELED = "canceled"


def invocation_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return a stable digest used to reject changed idempotent enqueues."""

    encoded = json.dumps(
        dict(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _normalize_timestamp(value: str, name: str = "timestamp") -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an RFC 3339 string")
    if not _RFC3339_PATTERN.fullmatch(value) or value.endswith("-00:00"):
        raise ValueError(f"{name} must be a strict RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _lease_deadline(now: str, lease_seconds: float) -> str:
    if not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool):
        raise TypeError("lease_seconds must be a number")
    if not math.isfinite(float(lease_seconds)) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be finite and greater than zero")
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return _normalize_timestamp(
        (parsed + timedelta(seconds=float(lease_seconds))).isoformat(),
        "lease deadline",
    )


def _stored_error(error: str) -> str:
    value = _required(error, "error").strip()
    return value[:_MAX_ERROR_LENGTH]


def _lease_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _persisted_integer(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int:
        raise TypeError(f"persisted {name} must use SQLite INTEGER storage")
    if not minimum <= value <= maximum:
        raise ValueError(f"persisted {name} is outside its supported range")
    return value


def _persisted_text(value: Any, name: str, *, required: bool = True) -> str:
    if type(value) is not str:
        raise TypeError(f"persisted {name} must use SQLite TEXT storage")
    if required and not value.strip():
        raise ValueError(f"persisted {name} must not be blank")
    return value


def _persisted_optional_text(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    return _persisted_text(value, name)


def _persisted_timestamp(value: Any, name: str) -> str:
    timestamp = _persisted_text(value, name)
    normalized = _normalize_timestamp(timestamp, f"persisted {name}")
    if timestamp != normalized:
        raise ValueError(f"persisted {name} is not canonical UTC")
    return timestamp


def _persisted_optional_timestamp(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    return _persisted_timestamp(value, name)


def _persisted_digest(value: Any, name: str) -> str:
    digest = _persisted_text(value, name)
    if not _SHA256_PATTERN.fullmatch(digest):
        raise ValueError(f"persisted {name} is not canonical SHA-256")
    return digest


@dataclass(frozen=True)
class InvocationJobSpec:
    """Immutable identity and retry policy for one logical task invocation."""

    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    payload_digest: str
    invocation_id: str = field(default_factory=lambda: new_id("inv"))
    priority: int = 50
    max_attempts: int = 3
    available_at: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "idempotency_key",
            "invocation_id",
        ):
            _required(getattr(self, name), name)
        if not _SHA256_PATTERN.fullmatch(self.payload_digest):
            raise ValueError("payload_digest must be a lowercase SHA-256 hex digest")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("priority must be an integer")
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool):
            raise TypeError("max_attempts must be an integer")
        if not 1 <= self.max_attempts <= _MAX_SQLITE_INTEGER:
            raise ValueError("max_attempts must fit a positive SQLite integer")
        if self.available_at is not None:
            _normalize_timestamp(self.available_at, "available_at")


@dataclass(frozen=True)
class InvocationJob:
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    payload_digest: str
    priority: int
    status: InvocationStatus
    max_attempts: int
    attempts_started: int
    lease_epoch: int
    requested_available_at: Optional[str]
    available_at: str
    created_at: str
    updated_at: str
    lease_owner: Optional[str]
    lease_expires_at: Optional[str]
    result_ref: Optional[str]
    last_error: Optional[str]
    finished_at: Optional[str]


@dataclass(frozen=True)
class InvocationAttempt:
    attempt_id: str
    invocation_id: str
    attempt_number: int
    lease_epoch: int
    worker_id: str
    lease_token_digest: str
    status: AttemptStatus
    started_at: str
    heartbeat_at: str
    lease_expires_at: str
    finished_at: Optional[str]
    error: Optional[str]
    result_ref: Optional[str]


@dataclass(frozen=True)
class InvocationLease:
    """Worker ownership proof; the opaque token is intentionally hidden from repr."""

    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    payload_digest: str
    attempt_id: str
    attempt_number: int
    max_attempts: int
    lease_epoch: int
    worker_id: str
    lease_token: str = field(repr=False)
    claimed_at: str = ""
    lease_expires_at: str = ""

    @property
    def fencing_token(self) -> int:
        """Return the monotonic token that downstream fenced resources should compare."""

        return self.lease_epoch


@dataclass(frozen=True)
class RecoverySummary:
    requeued: Tuple[str, ...] = ()
    exhausted: Tuple[str, ...] = ()

    @property
    def recovered_count(self) -> int:
        return len(self.requeued) + len(self.exhausted)


class SQLiteInvocationAttemptStore:
    """Durable invocation queue shared safely by multiple local processes.

    The store deliberately uses its own SQLite connection.  Pass the same filesystem
    path as the event store so both projections live in one database.  ``:memory:`` is
    useful for unit tests but cannot be shared between connections or processes.
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool):
            raise TypeError("busy_timeout_ms must be an integer")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.path = path
        self._clock = clock
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            with self._lock:
                self._connection.execute("PRAGMA foreign_keys=ON")
                self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                if path != ":memory:":
                    self._enable_wal(busy_timeout_ms)
                self._apply_migrations()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> SQLiteInvocationAttemptStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _enable_wal(self, busy_timeout_ms: int) -> None:
        """Enable WAL while tolerating another process performing the same startup."""

        deadline = time.monotonic() + (busy_timeout_ms / 1_000)
        delay = 0.001
        while True:
            try:
                row = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is None or str(row[0]).lower() != "wal":
                    raise RuntimeError("SQLite refused WAL journal mode")
                return
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                if not locked or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(0.05, delay * 2)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                try:
                    self._connection.execute("COMMIT")
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise

    def _now(self) -> str:
        """Read time from the store-owned clock, never from an individual work request."""

        return _normalize_timestamp(self._clock(), "clock")

    def _apply_migrations(self) -> None:
        # Attempt-only databases do not own the event-store outbox schema.
        # All registry entries are still checksum-validated on reopen, while
        # only migrations whose dependencies this store owns are installed.
        apply_sqlite_migrations(
            self._connection,
            target_versions=(1, 2),
            clock=self._now,
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> InvocationJob:
        try:
            status = InvocationStatus(_persisted_text(row["status"], "invocation status"))
            max_attempts = _persisted_integer(
                row["max_attempts"], "invocation max_attempts", minimum=1
            )
            attempts_started = _persisted_integer(
                row["attempts_started"],
                "invocation attempts_started",
                maximum=max_attempts,
            )
            lease_epoch = _persisted_integer(row["lease_epoch"], "invocation lease_epoch")
            if lease_epoch < attempts_started:
                raise ValueError("persisted invocation lease_epoch precedes attempts_started")

            lease_owner = _persisted_optional_text(row["lease_owner"], "invocation lease_owner")
            raw_lease_digest = row["lease_token_digest"]
            lease_digest = (
                None
                if raw_lease_digest is None
                else _persisted_digest(raw_lease_digest, "invocation lease_token_digest")
            )
            lease_expires_at = _persisted_optional_timestamp(
                row["lease_expires_at"], "invocation lease_expires_at"
            )
            heartbeat_at = _persisted_optional_timestamp(
                row["heartbeat_at"], "invocation heartbeat_at"
            )
            running_lease = (
                lease_owner is not None
                and lease_digest is not None
                and lease_expires_at is not None
                and heartbeat_at is not None
            )
            if (status is InvocationStatus.RUNNING) != running_lease:
                raise ValueError("persisted invocation lease fields contradict status")

            finished_at = _persisted_optional_timestamp(
                row["finished_at"], "invocation finished_at"
            )
            is_terminal = status in {
                InvocationStatus.SUCCEEDED,
                InvocationStatus.FAILED,
                InvocationStatus.CANCELED,
            }
            if is_terminal != (finished_at is not None):
                raise ValueError("persisted invocation finished_at contradicts status")

            last_error = _persisted_optional_text(row["last_error"], "invocation last_error")
            if last_error is not None and len(last_error) > _MAX_ERROR_LENGTH:
                raise ValueError("persisted invocation last_error exceeds its supported length")
            return InvocationJob(
                invocation_id=_persisted_text(row["invocation_id"], "invocation_id"),
                session_id=_persisted_text(row["session_id"], "invocation session_id"),
                plan_id=_persisted_text(row["plan_id"], "invocation plan_id"),
                task_id=_persisted_text(row["task_id"], "invocation task_id"),
                agent_id=_persisted_text(row["agent_id"], "invocation agent_id"),
                idempotency_key=_persisted_text(
                    row["idempotency_key"], "invocation idempotency_key"
                ),
                payload_digest=_persisted_digest(
                    row["payload_digest"], "invocation payload_digest"
                ),
                priority=_persisted_integer(row["priority"], "invocation priority", maximum=100),
                status=status,
                max_attempts=max_attempts,
                attempts_started=attempts_started,
                lease_epoch=lease_epoch,
                requested_available_at=_persisted_optional_timestamp(
                    row["requested_available_at"], "invocation requested_available_at"
                ),
                available_at=_persisted_timestamp(row["available_at"], "invocation available_at"),
                created_at=_persisted_timestamp(row["created_at"], "invocation created_at"),
                updated_at=_persisted_timestamp(row["updated_at"], "invocation updated_at"),
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                result_ref=_persisted_optional_text(row["result_ref"], "invocation result_ref"),
                last_error=last_error,
                finished_at=finished_at,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise InvocationIntegrityError("persisted invocation job is malformed") from exc

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> InvocationAttempt:
        try:
            status = AttemptStatus(_persisted_text(row["status"], "attempt status"))
            attempt_number = _persisted_integer(row["attempt_number"], "attempt number", minimum=1)
            lease_epoch = _persisted_integer(row["lease_epoch"], "attempt lease_epoch", minimum=1)
            if lease_epoch < attempt_number:
                raise ValueError("persisted attempt lease_epoch precedes attempt_number")
            finished_at = _persisted_optional_timestamp(row["finished_at"], "attempt finished_at")
            if (status is AttemptStatus.RUNNING) != (finished_at is None):
                raise ValueError("persisted attempt finished_at contradicts status")
            error = _persisted_optional_text(row["error"], "attempt error")
            if error is not None and len(error) > _MAX_ERROR_LENGTH:
                raise ValueError("persisted attempt error exceeds its supported length")
            return InvocationAttempt(
                attempt_id=_persisted_text(row["attempt_id"], "attempt_id"),
                invocation_id=_persisted_text(row["invocation_id"], "attempt invocation_id"),
                attempt_number=attempt_number,
                lease_epoch=lease_epoch,
                worker_id=_persisted_text(row["worker_id"], "attempt worker_id"),
                lease_token_digest=_persisted_digest(
                    row["lease_token_digest"], "attempt lease_token_digest"
                ),
                status=status,
                started_at=_persisted_timestamp(row["started_at"], "attempt started_at"),
                heartbeat_at=_persisted_timestamp(row["heartbeat_at"], "attempt heartbeat_at"),
                lease_expires_at=_persisted_timestamp(
                    row["lease_expires_at"], "attempt lease_expires_at"
                ),
                finished_at=finished_at,
                error=error,
                result_ref=_persisted_optional_text(row["result_ref"], "attempt result_ref"),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise InvocationIntegrityError("persisted invocation attempt is malformed") from exc

    @staticmethod
    def _existing_matches(row: sqlite3.Row, spec: InvocationJobSpec) -> bool:
        existing = SQLiteInvocationAttemptStore._row_to_job(row)
        requested_available_at = (
            _normalize_timestamp(spec.available_at, "available_at")
            if spec.available_at is not None
            else None
        )
        return (
            existing.session_id == spec.session_id
            and existing.plan_id == spec.plan_id
            and existing.task_id == spec.task_id
            and existing.agent_id == spec.agent_id
            and existing.idempotency_key == spec.idempotency_key
            and existing.payload_digest == spec.payload_digest
            and existing.priority == spec.priority
            and existing.max_attempts == spec.max_attempts
            and existing.requested_available_at == requested_available_at
        )

    def enqueue(self, spec: InvocationJobSpec) -> InvocationJob:
        """Persist one invocation, returning the original row for an identical retry."""

        with self._transaction() as connection:
            now = self._now()
            available_at = _normalize_timestamp(spec.available_at or now, "available_at")
            rows = connection.execute(
                """
                SELECT * FROM invocation_jobs
                WHERE invocation_id = ?
                   OR (session_id = ? AND task_id = ?)
                   OR (session_id = ? AND idempotency_key = ?)
                """,
                (
                    spec.invocation_id,
                    spec.session_id,
                    spec.task_id,
                    spec.session_id,
                    spec.idempotency_key,
                ),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._existing_matches(rows[0], spec):
                    raise InvocationConflictError(
                        "invocation identity or idempotency key is already bound to different work"
                    )
                return self._row_to_job(rows[0])
            connection.execute(
                """
                INSERT INTO invocation_jobs (
                    invocation_id, session_id, plan_id, task_id, agent_id,
                    idempotency_key, payload_digest, priority, status,
                    max_attempts, attempts_started, lease_epoch,
                    requested_available_at, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 0, 0, ?, ?, ?, ?)
                """,
                (
                    spec.invocation_id,
                    spec.session_id,
                    spec.plan_id,
                    spec.task_id,
                    spec.agent_id,
                    spec.idempotency_key,
                    spec.payload_digest,
                    spec.priority,
                    spec.max_attempts,
                    (
                        _normalize_timestamp(spec.available_at, "available_at")
                        if spec.available_at is not None
                        else None
                    ),
                    available_at,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                (spec.invocation_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the transaction.
                raise RuntimeError("enqueued invocation disappeared")
            return self._row_to_job(row)

    def get(self, invocation_id: str) -> Optional[InvocationJob]:
        _required(invocation_id, "invocation_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    def get_for_task(self, session_id: str, task_id: str) -> Optional[InvocationJob]:
        _required(session_id, "session_id")
        _required(task_id, "task_id")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM invocation_jobs
                WHERE session_id = ? AND task_id = ?
                """,
                (session_id, task_id),
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    def attempts(self, invocation_id: str) -> Tuple[InvocationAttempt, ...]:
        _required(invocation_id, "invocation_id")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM invocation_attempts
                WHERE invocation_id = ? ORDER BY attempt_number
                """,
                (invocation_id,),
            ).fetchall()
            return tuple(self._row_to_attempt(row) for row in rows)

    def attempts_page(
        self,
        invocation_id: str,
        after_attempt_number: int = 0,
        limit: int = 1_000,
    ) -> Tuple[InvocationAttempt, ...]:
        """Read one bounded attempt page after an exclusive attempt-number cursor."""

        _required(invocation_id, "invocation_id")
        if type(after_attempt_number) is not int:
            raise TypeError("after_attempt_number must be an integer")
        if not 0 <= after_attempt_number <= _MAX_SQLITE_INTEGER:
            raise ValueError("after_attempt_number is outside SQLite's integer range")
        if type(limit) is not int:
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM invocation_attempts
                WHERE invocation_id = ? AND attempt_number > ?
                ORDER BY attempt_number LIMIT ?
                """,
                (invocation_id, after_attempt_number, limit),
            ).fetchall()
            return tuple(self._row_to_attempt(row) for row in rows)

    @staticmethod
    def _load_running_attempt(
        connection: sqlite3.Connection,
        *,
        invocation_id: str,
        attempt_number: int,
        lease_epoch: int,
        attempt_id: Optional[str] = None,
    ) -> InvocationAttempt:
        parameters: list[Any] = [invocation_id, attempt_number, lease_epoch]
        attempt_filter = ""
        if attempt_id is not None:
            attempt_filter = " AND attempt_id = ?"
            parameters.append(attempt_id)
        rows = connection.execute(
            """
            SELECT * FROM invocation_attempts
            WHERE invocation_id = ? AND attempt_number = ?
              AND lease_epoch = ? AND status = 'running'
            """
            + attempt_filter
            + " LIMIT 2",
            tuple(parameters),
        ).fetchall()
        if len(rows) != 1:
            raise InvocationIntegrityError(
                "running invocation does not have exactly one owned attempt"
            )
        return SQLiteInvocationAttemptStore._row_to_attempt(rows[0])

    @staticmethod
    def _validate_running_attempt_owner(
        attempt: InvocationAttempt,
        *,
        invocation_id: str,
        attempt_number: int,
        lease_epoch: int,
        worker_id: str,
        lease_token_digest: str,
        lease_expires_at: str,
        attempt_id: Optional[str] = None,
    ) -> None:
        if (
            attempt.invocation_id != invocation_id
            or attempt.attempt_number != attempt_number
            or attempt.lease_epoch != lease_epoch
            or attempt.worker_id != worker_id
            or attempt.lease_token_digest != lease_token_digest
            or attempt.lease_expires_at != lease_expires_at
            or (attempt_id is not None and attempt.attempt_id != attempt_id)
        ):
            raise InvocationIntegrityError(
                "running invocation and attempt ownership records disagree"
            )

    def _recover_expired_in_transaction(
        self,
        connection: sqlite3.Connection,
        now: str,
        *,
        invocation_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> RecoverySummary:
        parameters: list[Any] = [now]
        where = "status = 'running' AND lease_expires_at <= ?"
        if invocation_id is not None:
            where += " AND invocation_id = ?"
            parameters.append(invocation_id)
        query = (
            "SELECT * FROM invocation_jobs WHERE "
            + where
            + " ORDER BY lease_expires_at, invocation_id"
        )
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = connection.execute(query, tuple(parameters)).fetchall()
        requeued = []
        exhausted = []
        reason = "lease expired before terminal acknowledgement"
        for row in rows:
            job = self._row_to_job(row)
            job_id = job.invocation_id
            epoch = job.lease_epoch
            attempt_number = job.attempts_started
            if job.lease_owner is None or job.lease_expires_at is None:
                raise InvocationIntegrityError("running invocation has incomplete lease ownership")
            attempt = self._load_running_attempt(
                connection,
                invocation_id=job_id,
                attempt_number=attempt_number,
                lease_epoch=epoch,
            )
            self._validate_running_attempt_owner(
                attempt,
                invocation_id=job_id,
                attempt_number=attempt_number,
                lease_epoch=epoch,
                worker_id=job.lease_owner,
                lease_token_digest=_persisted_digest(
                    row["lease_token_digest"], "invocation lease_token_digest"
                ),
                lease_expires_at=job.lease_expires_at,
            )
            attempt_update = connection.execute(
                """
                UPDATE invocation_attempts
                SET status = 'expired', finished_at = ?, error = ?
                WHERE invocation_id = ? AND attempt_number = ?
                  AND lease_epoch = ? AND status = 'running'
                """,
                (now, reason, job_id, attempt_number, epoch),
            )
            if attempt_update.rowcount != 1:
                raise RuntimeError("running invocation has no matching running attempt")
            if attempt_number >= job.max_attempts:
                update = connection.execute(
                    """
                    UPDATE invocation_jobs
                    SET status = 'failed', updated_at = ?, finished_at = ?,
                        last_error = ?, lease_owner = NULL, lease_token_digest = NULL,
                        lease_expires_at = NULL, heartbeat_at = NULL
                    WHERE invocation_id = ? AND status = 'running' AND lease_epoch = ?
                    """,
                    (now, now, reason, job_id, epoch),
                )
                exhausted.append(job_id)
            else:
                update = connection.execute(
                    """
                    UPDATE invocation_jobs
                    SET status = 'queued', available_at = ?, updated_at = ?,
                        last_error = ?, lease_owner = NULL, lease_token_digest = NULL,
                        lease_expires_at = NULL, heartbeat_at = NULL
                    WHERE invocation_id = ? AND status = 'running' AND lease_epoch = ?
                    """,
                    (now, now, reason, job_id, epoch),
                )
                requeued.append(job_id)
            if update.rowcount != 1:
                raise RuntimeError("invocation lease changed during expiration recovery")
        return RecoverySummary(tuple(requeued), tuple(exhausted))

    def recover_expired(
        self,
        *,
        limit: int = 1_000,
    ) -> RecoverySummary:
        """Fence expired owners and requeue work, exhausting its bounded retry policy."""

        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._transaction() as connection:
            normalized_now = self._now()
            return self._recover_expired_in_transaction(
                connection,
                normalized_now,
                limit=limit,
            )

    def _claim(
        self,
        worker_id: str,
        *,
        invocation_id: Optional[str],
        lease_seconds: float,
    ) -> Optional[InvocationLease]:
        _required(worker_id, "worker_id")
        if invocation_id is not None:
            _required(invocation_id, "invocation_id")
        with self._transaction() as connection:
            normalized_now = self._now()
            deadline = _lease_deadline(normalized_now, lease_seconds)
            self._recover_expired_in_transaction(
                connection,
                normalized_now,
                invocation_id=invocation_id,
                limit=(None if invocation_id is not None else 1_000),
            )
            parameters = [normalized_now]
            where = "status = 'queued' AND available_at <= ?"
            if invocation_id is not None:
                where += " AND invocation_id = ?"
                parameters.append(invocation_id)
            row = connection.execute(
                f"""
                SELECT * FROM invocation_jobs WHERE {where}
                ORDER BY priority DESC, available_at, created_at, invocation_id
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                return None
            job = self._row_to_job(row)
            attempt_number = job.attempts_started + 1
            if attempt_number > job.max_attempts:
                raise RuntimeError("queued invocation exceeded max_attempts invariant")
            if job.lease_epoch >= _MAX_SQLITE_INTEGER:
                raise InvocationIntegrityError("invocation lease epoch is exhausted")
            epoch = job.lease_epoch + 1
            attempt_id = new_id("attempt")
            lease_token = secrets.token_urlsafe(32)
            token_digest = _lease_token_digest(lease_token)
            update = connection.execute(
                """
                UPDATE invocation_jobs
                SET status = 'running', attempts_started = ?, lease_epoch = ?,
                    lease_owner = ?, lease_token_digest = ?, lease_expires_at = ?,
                    heartbeat_at = ?, updated_at = ?, finished_at = NULL
                WHERE invocation_id = ? AND status = 'queued'
                  AND attempts_started = ? AND lease_epoch = ?
                """,
                (
                    attempt_number,
                    epoch,
                    worker_id,
                    token_digest,
                    deadline,
                    normalized_now,
                    normalized_now,
                    job.invocation_id,
                    job.attempts_started,
                    job.lease_epoch,
                ),
            )
            if update.rowcount != 1:
                return None
            connection.execute(
                """
                INSERT INTO invocation_attempts (
                    attempt_id, invocation_id, attempt_number, lease_epoch,
                    worker_id, lease_token_digest, status, started_at,
                    heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    attempt_id,
                    job.invocation_id,
                    attempt_number,
                    epoch,
                    worker_id,
                    token_digest,
                    normalized_now,
                    normalized_now,
                    deadline,
                ),
            )
            return InvocationLease(
                invocation_id=job.invocation_id,
                session_id=job.session_id,
                plan_id=job.plan_id,
                task_id=job.task_id,
                agent_id=job.agent_id,
                idempotency_key=job.idempotency_key,
                payload_digest=job.payload_digest,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                max_attempts=job.max_attempts,
                lease_epoch=epoch,
                worker_id=worker_id,
                lease_token=lease_token,
                claimed_at=normalized_now,
                lease_expires_at=deadline,
            )

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> Optional[InvocationLease]:
        """Atomically claim the highest-priority available invocation."""

        return self._claim(
            worker_id,
            invocation_id=None,
            lease_seconds=lease_seconds,
        )

    def claim(
        self,
        invocation_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> Optional[InvocationLease]:
        """Atomically claim one known invocation if it is currently eligible."""

        return self._claim(
            worker_id,
            invocation_id=invocation_id,
            lease_seconds=lease_seconds,
        )

    def _active_owned_row(
        self,
        connection: sqlite3.Connection,
        lease: InvocationLease,
        now: str,
    ) -> Optional[InvocationJob]:
        row = connection.execute(
            """
            SELECT * FROM invocation_jobs
            WHERE invocation_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token_digest = ? AND lease_epoch = ?
              AND lease_expires_at > ?
            """,
            (
                lease.invocation_id,
                lease.worker_id,
                _lease_token_digest(lease.lease_token),
                lease.lease_epoch,
                now,
            ),
        ).fetchone()
        if row is not None and not isinstance(row, sqlite3.Row):
            raise TypeError("invocation store connection must return sqlite3.Row values")
        return None if row is None else self._row_to_job(row)

    def heartbeat(
        self,
        lease: InvocationLease,
        *,
        lease_seconds: float,
    ) -> bool:
        """Extend an active lease; returns false once ownership is stale or expired."""

        with self._transaction() as connection:
            normalized_now = self._now()
            proposed_deadline = _lease_deadline(normalized_now, lease_seconds)
            job = self._active_owned_row(connection, lease, normalized_now)
            if job is None:
                return False
            if job.lease_expires_at is None:  # protected by strict running-row decoding.
                raise InvocationIntegrityError("running invocation has no lease deadline")
            attempt = self._load_running_attempt(
                connection,
                invocation_id=lease.invocation_id,
                attempt_number=job.attempts_started,
                lease_epoch=lease.lease_epoch,
                attempt_id=lease.attempt_id,
            )
            self._validate_running_attempt_owner(
                attempt,
                invocation_id=lease.invocation_id,
                attempt_number=job.attempts_started,
                lease_epoch=lease.lease_epoch,
                worker_id=lease.worker_id,
                lease_token_digest=_lease_token_digest(lease.lease_token),
                lease_expires_at=job.lease_expires_at,
                attempt_id=lease.attempt_id,
            )
            deadline = max(job.lease_expires_at, proposed_deadline)
            update = connection.execute(
                """
                UPDATE invocation_jobs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE invocation_id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token_digest = ? AND lease_epoch = ?
                  AND lease_expires_at > ?
                """,
                (
                    normalized_now,
                    deadline,
                    normalized_now,
                    lease.invocation_id,
                    lease.worker_id,
                    _lease_token_digest(lease.lease_token),
                    lease.lease_epoch,
                    normalized_now,
                ),
            )
            if update.rowcount != 1:
                return False
            attempt_update = connection.execute(
                """
                UPDATE invocation_attempts
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE attempt_id = ? AND invocation_id = ?
                  AND lease_epoch = ? AND status = 'running'
                """,
                (
                    normalized_now,
                    deadline,
                    lease.attempt_id,
                    lease.invocation_id,
                    lease.lease_epoch,
                ),
            )
            if attempt_update.rowcount != 1:
                raise RuntimeError("owned invocation has no matching running attempt")
            return True

    def complete(
        self,
        lease: InvocationLease,
        *,
        result_ref: Optional[str] = None,
    ) -> bool:
        """CAS an active lease to success, rejecting stale or expired workers."""

        if result_ref is not None:
            _required(result_ref, "result_ref")
        with self._transaction() as connection:
            normalized_now = self._now()
            job = self._active_owned_row(connection, lease, normalized_now)
            if job is None:
                return False
            if job.lease_expires_at is None:
                raise InvocationIntegrityError("running invocation has no lease deadline")
            attempt = self._load_running_attempt(
                connection,
                invocation_id=lease.invocation_id,
                attempt_number=job.attempts_started,
                lease_epoch=lease.lease_epoch,
                attempt_id=lease.attempt_id,
            )
            self._validate_running_attempt_owner(
                attempt,
                invocation_id=lease.invocation_id,
                attempt_number=job.attempts_started,
                lease_epoch=lease.lease_epoch,
                worker_id=lease.worker_id,
                lease_token_digest=_lease_token_digest(lease.lease_token),
                lease_expires_at=job.lease_expires_at,
                attempt_id=lease.attempt_id,
            )
            update = connection.execute(
                """
                UPDATE invocation_jobs
                SET status = 'succeeded', result_ref = ?, updated_at = ?, finished_at = ?,
                    lease_owner = NULL, lease_token_digest = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL
                WHERE invocation_id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token_digest = ? AND lease_epoch = ?
                  AND lease_expires_at > ?
                """,
                (
                    result_ref,
                    normalized_now,
                    normalized_now,
                    lease.invocation_id,
                    lease.worker_id,
                    _lease_token_digest(lease.lease_token),
                    lease.lease_epoch,
                    normalized_now,
                ),
            )
            if update.rowcount != 1:
                return False
            attempt_update = connection.execute(
                """
                UPDATE invocation_attempts
                SET status = 'succeeded', finished_at = ?, result_ref = ?
                WHERE attempt_id = ? AND invocation_id = ?
                  AND lease_epoch = ? AND status = 'running'
                """,
                (
                    normalized_now,
                    result_ref,
                    lease.attempt_id,
                    lease.invocation_id,
                    lease.lease_epoch,
                ),
            )
            if attempt_update.rowcount != 1:
                raise RuntimeError("owned invocation has no matching running attempt")
            return True

    def fail(
        self,
        lease: InvocationLease,
        error: str,
        *,
        retry_at: Optional[str] = None,
    ) -> bool:
        """CAS an active lease to retry or terminal failure under its bounded policy."""

        stored_error = _stored_error(error)
        with self._transaction() as connection:
            normalized_now = self._now()
            normalized_retry = _normalize_timestamp(retry_at or normalized_now, "retry_at")
            normalized_retry = max(normalized_now, normalized_retry)
            job = self._active_owned_row(connection, lease, normalized_now)
            if job is None:
                return False
            if job.lease_expires_at is None:
                raise InvocationIntegrityError("running invocation has no lease deadline")
            attempt = self._load_running_attempt(
                connection,
                invocation_id=lease.invocation_id,
                attempt_number=job.attempts_started,
                lease_epoch=lease.lease_epoch,
                attempt_id=lease.attempt_id,
            )
            self._validate_running_attempt_owner(
                attempt,
                invocation_id=lease.invocation_id,
                attempt_number=job.attempts_started,
                lease_epoch=lease.lease_epoch,
                worker_id=lease.worker_id,
                lease_token_digest=_lease_token_digest(lease.lease_token),
                lease_expires_at=job.lease_expires_at,
                attempt_id=lease.attempt_id,
            )
            attempt_update = connection.execute(
                """
                UPDATE invocation_attempts
                SET status = 'failed', finished_at = ?, error = ?
                WHERE attempt_id = ? AND invocation_id = ?
                  AND lease_epoch = ? AND status = 'running'
                """,
                (
                    normalized_now,
                    stored_error,
                    lease.attempt_id,
                    lease.invocation_id,
                    lease.lease_epoch,
                ),
            )
            if attempt_update.rowcount != 1:
                raise RuntimeError("owned invocation has no matching running attempt")
            if job.attempts_started >= job.max_attempts:
                update = connection.execute(
                    """
                    UPDATE invocation_jobs
                    SET status = 'failed', last_error = ?, updated_at = ?, finished_at = ?,
                        lease_owner = NULL, lease_token_digest = NULL,
                        lease_expires_at = NULL, heartbeat_at = NULL
                    WHERE invocation_id = ? AND status = 'running'
                      AND lease_owner = ? AND lease_token_digest = ? AND lease_epoch = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        stored_error,
                        normalized_now,
                        normalized_now,
                        lease.invocation_id,
                        lease.worker_id,
                        _lease_token_digest(lease.lease_token),
                        lease.lease_epoch,
                        normalized_now,
                    ),
                )
            else:
                update = connection.execute(
                    """
                    UPDATE invocation_jobs
                    SET status = 'queued', available_at = ?, last_error = ?, updated_at = ?,
                        lease_owner = NULL, lease_token_digest = NULL,
                        lease_expires_at = NULL, heartbeat_at = NULL
                    WHERE invocation_id = ? AND status = 'running'
                      AND lease_owner = ? AND lease_token_digest = ? AND lease_epoch = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        normalized_retry,
                        stored_error,
                        normalized_now,
                        lease.invocation_id,
                        lease.worker_id,
                        _lease_token_digest(lease.lease_token),
                        lease.lease_epoch,
                        normalized_now,
                    ),
                )
            if update.rowcount != 1:
                raise RuntimeError("invocation lease changed during failure CAS")
            return True

    def schema_version(self) -> int:
        with self._lock:
            return int(current_schema_version(self._connection))

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "AttemptStatus",
    "InvocationAttempt",
    "InvocationConflictError",
    "InvocationIntegrityError",
    "InvocationJob",
    "InvocationJobSpec",
    "InvocationLease",
    "InvocationStatus",
    "MigrationDriftError",
    "RecoverySummary",
    "SQLiteInvocationAttemptStore",
    "invocation_payload_digest",
]
