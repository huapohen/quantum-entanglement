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
import importlib.resources
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

from .protocol import new_id, utc_now

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_MAX_ERROR_LENGTH = 4_096
_MIGRATIONS = ((1, "0001_invocation_attempts.up.sql"),)


class InvocationConflictError(RuntimeError):
    """Raised when an idempotency boundary is reused for different work."""


class MigrationDriftError(RuntimeError):
    """Raised when a packaged migration differs from the recorded migration."""


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
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be greater than zero")
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

    @staticmethod
    def _migration_text(filename: str) -> str:
        package = "quantum_entanglement.migrations"
        return importlib.resources.files(package).joinpath(filename).read_text(encoding="utf-8")

    def _now(self) -> str:
        """Read time from the store-owned clock, never from an individual work request."""

        return _normalize_timestamp(self._clock(), "clock")

    def _apply_migrations(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS qe_schema_migrations (
                version INTEGER PRIMARY KEY,
                filename TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        for version, filename in _MIGRATIONS:
            sql = self._migration_text(filename)
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            row = self._connection.execute(
                "SELECT filename, sha256 FROM qe_schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            if row is not None:
                if row["filename"] != filename or row["sha256"] != digest:
                    raise MigrationDriftError(
                        f"migration {version} checksum or filename differs from the applied schema"
                    )
                continue
            applied_at = self._now()
            quoted_filename = filename.replace("'", "''")
            quoted_digest = digest.replace("'", "''")
            quoted_applied_at = applied_at.replace("'", "''")
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{sql}\n"
                "INSERT INTO qe_schema_migrations "
                "(version, filename, sha256, applied_at) VALUES "
                f"({version}, '{quoted_filename}', '{quoted_digest}', "
                f"'{quoted_applied_at}');\nCOMMIT;"
            )
            try:
                self._connection.executescript(script)
            except sqlite3.IntegrityError:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                # A concurrent initializer may have installed the identical migration.
                concurrent = self._connection.execute(
                    "SELECT filename, sha256 FROM qe_schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if (
                    concurrent is None
                    or concurrent["filename"] != filename
                    or concurrent["sha256"] != digest
                ):
                    raise

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> InvocationJob:
        return InvocationJob(
            invocation_id=row["invocation_id"],
            session_id=row["session_id"],
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            agent_id=row["agent_id"],
            idempotency_key=row["idempotency_key"],
            payload_digest=row["payload_digest"],
            priority=int(row["priority"]),
            status=InvocationStatus(row["status"]),
            max_attempts=int(row["max_attempts"]),
            attempts_started=int(row["attempts_started"]),
            lease_epoch=int(row["lease_epoch"]),
            requested_available_at=row["requested_available_at"],
            available_at=row["available_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            result_ref=row["result_ref"],
            last_error=row["last_error"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> InvocationAttempt:
        return InvocationAttempt(
            attempt_id=row["attempt_id"],
            invocation_id=row["invocation_id"],
            attempt_number=int(row["attempt_number"]),
            lease_epoch=int(row["lease_epoch"]),
            worker_id=row["worker_id"],
            lease_token_digest=row["lease_token_digest"],
            status=AttemptStatus(row["status"]),
            started_at=row["started_at"],
            heartbeat_at=row["heartbeat_at"],
            lease_expires_at=row["lease_expires_at"],
            finished_at=row["finished_at"],
            error=row["error"],
            result_ref=row["result_ref"],
        )

    @staticmethod
    def _existing_matches(row: sqlite3.Row, spec: InvocationJobSpec) -> bool:
        requested_available_at = (
            _normalize_timestamp(spec.available_at, "available_at")
            if spec.available_at is not None
            else None
        )
        return (
            row["session_id"] == spec.session_id
            and row["plan_id"] == spec.plan_id
            and row["task_id"] == spec.task_id
            and row["agent_id"] == spec.agent_id
            and row["idempotency_key"] == spec.idempotency_key
            and row["payload_digest"] == spec.payload_digest
            and int(row["priority"]) == spec.priority
            and int(row["max_attempts"]) == spec.max_attempts
            and row["requested_available_at"] == requested_available_at
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
            job_id = row["invocation_id"]
            epoch = int(row["lease_epoch"])
            attempt_number = int(row["attempts_started"])
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
            if attempt_number >= int(row["max_attempts"]):
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
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
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
            attempt_number = int(row["attempts_started"]) + 1
            if attempt_number > int(row["max_attempts"]):
                raise RuntimeError("queued invocation exceeded max_attempts invariant")
            epoch = int(row["lease_epoch"]) + 1
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
                    row["invocation_id"],
                    int(row["attempts_started"]),
                    int(row["lease_epoch"]),
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
                    row["invocation_id"],
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
                invocation_id=row["invocation_id"],
                session_id=row["session_id"],
                plan_id=row["plan_id"],
                task_id=row["task_id"],
                agent_id=row["agent_id"],
                idempotency_key=row["idempotency_key"],
                payload_digest=row["payload_digest"],
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                max_attempts=int(row["max_attempts"]),
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

    @staticmethod
    def _active_owned_row(
        connection: sqlite3.Connection,
        lease: InvocationLease,
        now: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
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
            row = self._active_owned_row(connection, lease, normalized_now)
            if row is None:
                return False
            deadline = max(row["lease_expires_at"], proposed_deadline)
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
            row = self._active_owned_row(connection, lease, normalized_now)
            if row is None:
                return False
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
            row = self._active_owned_row(connection, lease, normalized_now)
            if row is None:
                return False
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
            if int(row["attempts_started"]) >= int(row["max_attempts"]):
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
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM qe_schema_migrations"
            ).fetchone()
            return int(row["version"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "AttemptStatus",
    "InvocationAttempt",
    "InvocationConflictError",
    "InvocationJob",
    "InvocationJobSpec",
    "InvocationLease",
    "InvocationStatus",
    "MigrationDriftError",
    "RecoverySummary",
    "SQLiteInvocationAttemptStore",
    "invocation_payload_digest",
]
