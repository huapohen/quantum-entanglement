"""Asynchronous, lease-aware publisher for the transactional outbox.

The publisher deliberately provides *at-least-once* delivery.  A downstream
transport must deduplicate ``PublishRequest.idempotency_key`` because a process
can crash after the transport accepts a message but before SQLite records the
acknowledgement.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .delivery import StoredOutboxMessage
from .service.logging import (
    LogEventSchema,
    LogField,
    LogFieldKind,
    SafeLogCatalog,
    SafeLogger,
)
from .store import SQLiteEventStore

_PUBLISHER_LOG_CATALOG = SafeLogCatalog(
    (
        LogEventSchema("qe.publisher.clock_failed", logging.ERROR),
        LogEventSchema(
            "qe.publisher.claim_failed",
            logging.ERROR,
            (LogField("worker_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema(
            "qe.publisher.ack_failed",
            logging.CRITICAL,
            (LogField("message_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema(
            "qe.publisher.lease_budget_validation_failed",
            logging.ERROR,
            (LogField("message_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema(
            "qe.publisher.lease_deadline_missing",
            logging.ERROR,
            (LogField("message_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema(
            "qe.publisher.lease_deadline_validation_failed",
            logging.ERROR,
            (LogField("message_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema(
            "qe.publisher.ambiguity_persist_failed",
            logging.CRITICAL,
            (LogField("message_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema(
            "qe.publisher.rejection_persist_failed",
            logging.ERROR,
            (LogField("message_id", LogFieldKind.IDENTIFIER_HASH),),
        ),
        LogEventSchema("qe.publisher.retry_timestamp_failed", logging.ERROR),
        LogEventSchema("qe.publisher.retry_jitter_failed", logging.ERROR),
        LogEventSchema("qe.publisher.retry_jitter_invalid", logging.ERROR),
        LogEventSchema("qe.publisher.error_classifier_failed", logging.ERROR),
        LogEventSchema("qe.publisher.error_classifier_rejected", logging.ERROR),
    )
)
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BUILTIN_ERROR_CODES = frozenset(
    {
        "connector_cancelled",
        "connector_failure",
        "connector_input_rejected",
        "connector_rejected",
        "invalid_publish_receipt",
        "lease_budget_exhausted",
        "transport_unavailable",
    }
)


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def _full_jitter(delay_seconds: float) -> float:
    return random.uniform(0.0, delay_seconds)


def _timestamp(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("publisher clock must return a timezone-aware datetime")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("lease deadline must include a timezone")
    return parsed.astimezone(timezone.utc)


def _thread_identifier(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


@dataclass(frozen=True)
class PublishRequest:
    """Transport-neutral message passed to the publishing callback.

    ``idempotency_key`` and ``message_id`` remain stable across retries.  The
    callback should propagate the idempotency key to the broker or perform its
    own durable deduplication before causing an external side effect.
    """

    message_id: str
    idempotency_key: str
    destination: str
    payload: Mapping[str, Any]
    headers: Mapping[str, Any]
    attempt_count: int
    triggering_event_id: str
    triggering_global_position: int
    lease_deadline: str | None

    @classmethod
    def from_stored(cls, stored: StoredOutboxMessage) -> PublishRequest:
        message = stored.message
        # OutboxMessage normalizes a missing key to message_id in __post_init__.
        return cls(
            message_id=message.message_id,
            idempotency_key=message.idempotency_key or message.message_id,
            destination=message.destination,
            payload=dict(message.payload),
            headers=dict(message.headers),
            attempt_count=stored.attempt_count,
            triggering_event_id=stored.triggering_event_id,
            triggering_global_position=stored.triggering_global_position,
            lease_deadline=stored.lease_expires_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "idempotencyKey": self.idempotency_key,
            "destination": self.destination,
            "payload": dict(self.payload),
            "headers": dict(self.headers),
            "attemptCount": self.attempt_count,
            "triggeringEventId": self.triggering_event_id,
            "triggeringGlobalPosition": self.triggering_global_position,
            "leaseDeadline": self.lease_deadline,
        }


class PublishResult(str, Enum):
    """Explicit transport outcome; truthy/falsey ad-hoc returns are invalid."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PublishReceipt:
    """Connector evidence used to decide whether the outbox lease may be ACKed."""

    result: PublishResult
    receipt_id: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, PublishResult):
            raise TypeError("publish receipt result must be a PublishResult")
        if self.result is PublishResult.ACCEPTED:
            if not isinstance(self.receipt_id, str) or not self.receipt_id.strip():
                raise ValueError("accepted publish receipt requires receipt_id")
            if self.reason_code is not None:
                raise ValueError("accepted publish receipt cannot include reason_code")
        if self.result is PublishResult.REJECTED:
            if not isinstance(self.reason_code, str) or not self.reason_code.strip():
                raise ValueError("rejected publish receipt requires reason_code")
            if self.receipt_id is not None:
                raise ValueError("rejected publish receipt cannot include receipt_id")

    @classmethod
    def accepted(cls, receipt_id: str) -> PublishReceipt:
        return cls(PublishResult.ACCEPTED, receipt_id=receipt_id)

    @classmethod
    def rejected(cls, reason_code: str) -> PublishReceipt:
        return cls(PublishResult.REJECTED, reason_code=reason_code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.value,
            "receiptId": self.receipt_id,
            "reasonCode": self.reason_code,
        }


@dataclass(frozen=True)
class AbandonedCallback:
    """A timed-out callback that may still produce a late external side effect."""

    message_id: str
    idempotency_key: str
    task_name: str
    lease_deadline: str | None
    abandoned_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "idempotencyKey": self.idempotency_key,
            "taskName": self.task_name,
            "leaseDeadline": self.lease_deadline,
            "abandonedAt": self.abandoned_at,
        }


@dataclass(frozen=True)
class PublishBatchStats:
    """Outcome of one claim-and-publish cycle."""

    claimed: int = 0
    published: int = 0
    publish_failures: int = 0
    timed_out: int = 0
    retried: int = 0
    dead_lettered: int = 0
    lease_conflicts: int = 0
    store_errors: int = 0
    accepted_unconfirmed: int = 0
    ack_failed: int = 0
    lease_expired: int = 0
    abandoned_callbacks: int = 0
    ambiguity_persisted: int = 0
    reconciliation_failed: int = 0
    admission_rejected: int = 0
    lease_budget_rejected: int = 0
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "published": self.published,
            "publishFailures": self.publish_failures,
            "timedOut": self.timed_out,
            "retried": self.retried,
            "deadLettered": self.dead_lettered,
            "leaseConflicts": self.lease_conflicts,
            "storeErrors": self.store_errors,
            "acceptedUnconfirmed": self.accepted_unconfirmed,
            "ackFailed": self.ack_failed,
            "leaseExpired": self.lease_expired,
            "abandonedCallbacks": self.abandoned_callbacks,
            "ambiguityPersisted": self.ambiguity_persisted,
            "reconciliationFailed": self.reconciliation_failed,
            "admissionRejected": self.admission_rejected,
            "leaseBudgetRejected": self.lease_budget_rejected,
            "durationSeconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class PublisherStats:
    """Immutable cumulative publisher statistics safe to expose to monitoring."""

    worker_id: str
    running: bool
    cycles: int
    empty_polls: int
    claimed: int
    published: int
    publish_failures: int
    timed_out: int
    retried: int
    dead_lettered: int
    lease_conflicts: int
    store_errors: int
    accepted_unconfirmed: int
    ack_failed: int
    lease_expired: int
    abandoned_callbacks: int
    ambiguity_persisted: int
    reconciliation_failed: int
    admission_rejected: int
    lease_budget_rejected: int
    cancelled_cycles: int
    active_cycles: int
    active_callbacks: int
    leaked_callbacks: int
    active_db_tasks: int
    lifecycle_state: str
    shutdown_clean: bool
    started_at: str | None
    stopped_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workerId": self.worker_id,
            "running": self.running,
            "cycles": self.cycles,
            "emptyPolls": self.empty_polls,
            "claimed": self.claimed,
            "published": self.published,
            "publishFailures": self.publish_failures,
            "timedOut": self.timed_out,
            "retried": self.retried,
            "deadLettered": self.dead_lettered,
            "leaseConflicts": self.lease_conflicts,
            "storeErrors": self.store_errors,
            "acceptedUnconfirmed": self.accepted_unconfirmed,
            "ackFailed": self.ack_failed,
            "leaseExpired": self.lease_expired,
            "abandonedCallbacks": self.abandoned_callbacks,
            "ambiguityPersisted": self.ambiguity_persisted,
            "reconciliationFailed": self.reconciliation_failed,
            "admissionRejected": self.admission_rejected,
            "leaseBudgetRejected": self.lease_budget_rejected,
            "cancelledCycles": self.cancelled_cycles,
            "activeCycles": self.active_cycles,
            "activeCallbacks": self.active_callbacks,
            "leakedCallbacks": self.leaked_callbacks,
            "activeDbTasks": self.active_db_tasks,
            "lifecycleState": self.lifecycle_state,
            "shutdownClean": self.shutdown_clean,
            "startedAt": self.started_at,
            "stoppedAt": self.stopped_at,
        }


@dataclass
class _Counters:
    cycles: int = 0
    empty_polls: int = 0
    claimed: int = 0
    published: int = 0
    publish_failures: int = 0
    timed_out: int = 0
    retried: int = 0
    dead_lettered: int = 0
    lease_conflicts: int = 0
    store_errors: int = 0
    accepted_unconfirmed: int = 0
    ack_failed: int = 0
    lease_expired: int = 0
    abandoned_callbacks: int = 0
    ambiguity_persisted: int = 0
    reconciliation_failed: int = 0
    admission_rejected: int = 0
    lease_budget_rejected: int = 0
    cancelled_cycles: int = 0


@dataclass(frozen=True)
class _AttemptOutcome:
    published: int = 0
    publish_failures: int = 0
    timed_out: int = 0
    retried: int = 0
    dead_lettered: int = 0
    lease_conflicts: int = 0
    store_errors: int = 0
    accepted_unconfirmed: int = 0
    ack_failed: int = 0
    lease_expired: int = 0
    abandoned_callbacks: int = 0
    ambiguity_persisted: int = 0
    reconciliation_failed: int = 0
    lease_budget_rejected: int = 0


class PublisherClosedError(RuntimeError):
    """Raised when new work is submitted while the publisher is closing/closed."""


class _InvalidPublishReceipt(TypeError):
    pass


class _ConnectorRejected(RuntimeError):
    pass


class _LeaseBudgetExhausted(RuntimeError):
    pass


class OutboxPublisher:
    """Continuously claim and publish due SQLite outbox messages.

    The connector callback must be a native async callable.  On every attempt it
    must pass ``PublishRequest.idempotency_key`` to the downstream broker or
    durably deduplicate that key itself.  The publisher is at-least-once: no
    local implementation can atomically combine an external broker write with
    the SQLite acknowledgement.

    Each connector invocation runs in its own daemon thread and private asyncio
    loop.  This isolation keeps even an accidentally blocking ``async def`` from
    blocking the publisher's caller deadline.  Connectors therefore must not
    capture asyncio objects bound to the publisher loop; they should create all
    transport clients inside their invocation or use a process-safe boundary.

    One claimed batch is published concurrently.  ``stop`` is graceful: it
    rejects new cycles and waits for every cycle accepted before closing.  A
    callback that misses its hard deadline is cancelled and tracked as
    abandoned, but is never assumed to have stopped.  In that case shutdown is
    explicitly reported as unconfirmed rather than falsely reported as clean.
    """

    def __init__(
        self,
        store: SQLiteEventStore,
        publish: Callable[[PublishRequest], Any],
        *,
        worker_id: str,
        batch_size: int = 32,
        lease_seconds: float = 30.0,
        publish_timeout: float = 10.0,
        poll_interval: float = 1.0,
        max_attempts: int = 5,
        max_callback_tasks: int = 64,
        base_retry_delay: float = 1.0,
        max_retry_delay: float = 60.0,
        jitter: Callable[[float], float] = _full_jitter,
        clock: Callable[[], datetime] = _utc_clock,
        monotonic: Callable[[], float] = time.monotonic,
        error_formatter: Callable[[BaseException], str] | None = None,
        error_code_allowlist: tuple[str, ...] = (),
        logger: logging.Logger | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        self._require_positive("lease_seconds", lease_seconds)
        self._require_positive("publish_timeout", publish_timeout)
        if lease_seconds <= publish_timeout:
            raise ValueError("lease_seconds must be greater than publish_timeout")
        self._require_nonnegative("poll_interval", poll_interval)
        if max_attempts <= 0:
            raise ValueError("max_attempts must be greater than zero")
        if max_callback_tasks <= 0:
            raise ValueError("max_callback_tasks must be greater than zero")
        self._require_nonnegative("base_retry_delay", base_retry_delay)
        self._require_nonnegative("max_retry_delay", max_retry_delay)
        if max_retry_delay < base_retry_delay:
            raise ValueError("max_retry_delay must be at least base_retry_delay")
        if not self._is_async_callable(publish):
            raise TypeError("publish must be a native async callable")
        if not callable(jitter) or not callable(clock):
            raise TypeError("jitter and clock must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if error_formatter is not None and not callable(error_formatter):
            raise TypeError("error_formatter must be callable")
        if type(error_code_allowlist) is not tuple or len(error_code_allowlist) > 64:
            raise TypeError("error_code_allowlist must be a bounded tuple")
        if any(
            type(code) is not str or _ERROR_CODE.fullmatch(code) is None
            for code in error_code_allowlist
        ):
            raise ValueError("error_code_allowlist contains an invalid code")
        if len(set(error_code_allowlist)) != len(error_code_allowlist):
            raise ValueError("error_code_allowlist contains a duplicate code")

        self._store = store
        self._publish = publish
        self.worker_id = worker_id
        self.batch_size = batch_size
        self.lease_seconds = float(lease_seconds)
        self.publish_timeout = float(publish_timeout)
        self.poll_interval = float(poll_interval)
        self.max_attempts = max_attempts
        self.max_callback_tasks = max_callback_tasks
        self.base_retry_delay = float(base_retry_delay)
        self.max_retry_delay = float(max_retry_delay)
        self._jitter = jitter
        self._clock = clock
        self._monotonic = monotonic
        self._error_formatter = error_formatter or self._default_error_formatter
        self._allowed_error_codes = _BUILTIN_ERROR_CODES | frozenset(error_code_allowlist)
        self._logger = SafeLogger(logger or logging.getLogger(__name__), _PUBLISHER_LOG_CATALOG)

        self._counters = _Counters()
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._cycle_lock: asyncio.Lock | None = None
        self._cycles_drained: asyncio.Event | None = None
        self._db_drained: asyncio.Event | None = None
        self._run_task: asyncio.Task[PublisherStats] | None = None
        self._callback_tasks: set[asyncio.Future[Any]] = set()
        self._abandoned_tasks: dict[asyncio.Future[Any], AbandonedCallback] = {}
        self._connector_names: dict[asyncio.Future[Any], str] = {}
        self._db_tasks: set[asyncio.Task[Any]] = set()
        self._active_cycles = 0
        self._running = False
        self._closing = False
        self._closed = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None

    @staticmethod
    def _is_async_callable(callback: Callable[[PublishRequest], Any]) -> bool:
        if not callable(callback):
            return False
        if inspect.iscoroutinefunction(callback):
            return True
        return inspect.iscoroutinefunction(callback.__call__)  # type: ignore[operator]

    def _ensure_primitives(self) -> None:
        """Lazily bind Python 3.9 asyncio primitives to one running loop."""

        loop = asyncio.get_running_loop()
        if self._bound_loop is not None and self._bound_loop is not loop:
            raise RuntimeError("outbox publisher cannot move between event loops")
        if self._bound_loop is None:
            self._bound_loop = loop
            self._stop_event = asyncio.Event()
            self._cycle_lock = asyncio.Lock()
            self._cycles_drained = asyncio.Event()
            self._cycles_drained.set()
            self._db_drained = asyncio.Event()
            self._db_drained.set()

    @staticmethod
    def _require_positive(name: str, value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite number greater than zero")

    @staticmethod
    def _require_nonnegative(name: str, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")

    @staticmethod
    def _default_error_formatter(error: BaseException) -> str:
        if isinstance(error, _InvalidPublishReceipt):
            return "invalid_publish_receipt"
        if isinstance(error, _ConnectorRejected):
            return "connector_rejected"
        if isinstance(error, _LeaseBudgetExhausted):
            return "lease_budget_exhausted"
        if isinstance(error, (ConnectionError, OSError)):
            return "transport_unavailable"
        if isinstance(error, (ValueError, TypeError)):
            return "connector_input_rejected"
        if isinstance(error, asyncio.CancelledError):
            return "connector_cancelled"
        return "connector_failure"

    @property
    def running(self) -> bool:
        return self._running

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def abandoned(self) -> tuple[AbandonedCallback, ...]:
        """Currently running callbacks whose deadline already expired."""

        return tuple(self._abandoned_tasks.values())

    def _lifecycle_state(self) -> str:
        if self._closed:
            if self._abandoned_tasks:
                return "closed_with_abandoned_callbacks"
            if self._counters.accepted_unconfirmed or self._counters.cancelled_cycles:
                return "closed_unconfirmed"
            return "closed"
        if self._closing:
            return "closing"
        if self._running:
            return "running"
        return "idle"

    @property
    def stats(self) -> PublisherStats:
        counters = self._counters
        return PublisherStats(
            worker_id=self.worker_id,
            running=self._running,
            cycles=counters.cycles,
            empty_polls=counters.empty_polls,
            claimed=counters.claimed,
            published=counters.published,
            publish_failures=counters.publish_failures,
            timed_out=counters.timed_out,
            retried=counters.retried,
            dead_lettered=counters.dead_lettered,
            lease_conflicts=counters.lease_conflicts,
            store_errors=counters.store_errors,
            accepted_unconfirmed=counters.accepted_unconfirmed,
            ack_failed=counters.ack_failed,
            lease_expired=counters.lease_expired,
            abandoned_callbacks=counters.abandoned_callbacks,
            ambiguity_persisted=counters.ambiguity_persisted,
            reconciliation_failed=counters.reconciliation_failed,
            admission_rejected=counters.admission_rejected,
            lease_budget_rejected=counters.lease_budget_rejected,
            cancelled_cycles=counters.cancelled_cycles,
            active_cycles=self._active_cycles,
            active_callbacks=len(self._callback_tasks),
            leaked_callbacks=len(self._abandoned_tasks),
            active_db_tasks=len(self._db_tasks),
            lifecycle_state=self._lifecycle_state(),
            shutdown_clean=(
                self._closed
                and counters.accepted_unconfirmed == 0
                and counters.cancelled_cycles == 0
                and not self._abandoned_tasks
                and not self._db_tasks
            ),
            started_at=self._started_at,
            stopped_at=self._stopped_at,
        )

    def start(self) -> asyncio.Task[PublisherStats]:
        """Start the service in the current event loop and return its task."""

        if self._closing or self._closed:
            raise PublisherClosedError("outbox publisher is closing or closed")
        if self._running or (self._run_task is not None and not self._run_task.done()):
            raise RuntimeError("outbox publisher is already running")
        self._ensure_primitives()
        assert self._stop_event is not None
        loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._started_at = self._safe_clock_timestamp()
        self._stopped_at = None
        self._running = True
        task = loop.create_task(self.run(), name=f"outbox-publisher:{self.worker_id}")
        self._run_task = task
        return task

    async def run(self) -> PublisherStats:
        """Run until ``request_stop``/``stop`` is called.

        Calling ``run`` directly is supported; ``start`` is convenient when the
        owner needs to do other work before awaiting graceful shutdown.
        """

        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("outbox publisher requires an asyncio task")
        if self._closed or (self._closing and not (self._running and self._run_task is current)):
            raise PublisherClosedError("outbox publisher is closing or closed")
        self._ensure_primitives()
        assert self._stop_event is not None
        assert self._cycles_drained is not None
        assert self._db_drained is not None
        if self._running:
            if self._run_task is not current:
                raise RuntimeError("outbox publisher is already running")
        else:
            self._stop_event.clear()
            self._started_at = self._safe_clock_timestamp()
            self._stopped_at = None
            self._running = True
            self._run_task = current

        try:
            while not self._stop_event.is_set():
                batch = await self.run_once()
                if self._stop_event.is_set():
                    break
                if batch.claimed == 0:
                    await self._wait_for_next_poll()
        finally:
            self._begin_closing()
            await self._cycles_drained.wait()
            await self._db_drained.wait()
            self._running = False
            self._mark_closed()
            if self._run_task is current:
                self._run_task = None
        return self.stats

    def request_stop(self) -> None:
        """Signal the service to stop without cancelling in-flight publishing."""

        self._begin_closing()

    async def stop(self) -> PublisherStats:
        """Close after all accepted cycles reach a terminal or unconfirmed state.

        Timed-out callbacks are not awaited.  If they ignore cancellation, the
        returned statistics explicitly report them as leaked and shutdown is
        not marked clean.
        """

        self._ensure_primitives()
        assert self._cycles_drained is not None
        assert self._db_drained is not None
        current = asyncio.current_task()
        if current in self._callback_tasks:
            raise RuntimeError("a publish callback cannot stop its own publisher")
        if self._closed:
            return self.stats
        self.request_stop()
        task = self._run_task
        if task is not None and task is not current:
            await asyncio.shield(task)
        await asyncio.shield(self._cycles_drained.wait())
        await asyncio.shield(self._db_drained.wait())
        self._mark_closed()
        return self.stats

    async def close(self) -> PublisherStats:
        """Alias for ``stop`` for service/container lifecycle integrations."""

        return await self.stop()

    async def __aenter__(self) -> OutboxPublisher:
        self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.stop()

    async def _wait_for_next_poll(self) -> None:
        assert self._stop_event is not None
        if self.poll_interval == 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            pass

    def _begin_closing(self) -> None:
        if not self._closed:
            self._closing = True
        if self._stop_event is not None:
            self._stop_event.set()

    def _mark_closed(self) -> None:
        if self._active_cycles:
            return
        self._running = False
        self._closing = False
        self._closed = True
        if self._stopped_at is None:
            self._stopped_at = self._safe_clock_timestamp()

    def _accept_cycle(self) -> None:
        # No await is allowed between the state check and increment.  This makes
        # acceptance atomic with respect to stop() on one asyncio event loop.
        if self._closing or self._closed:
            raise PublisherClosedError("outbox publisher is closing or closed")
        assert self._cycles_drained is not None
        self._active_cycles += 1
        self._cycles_drained.clear()

    def _release_cycle(self) -> None:
        assert self._cycles_drained is not None
        self._active_cycles -= 1
        if self._active_cycles == 0:
            self._cycles_drained.set()

    def _safe_clock_timestamp(self) -> str | None:
        try:
            return _timestamp(self._clock())
        except Exception:
            self._logger.emit("qe.publisher.clock_failed")
            return None

    async def run_once(self) -> PublishBatchStats:
        """Claim one batch, publish it concurrently, and persist every outcome."""

        self._ensure_primitives()
        assert self._cycle_lock is not None
        self._accept_cycle()
        try:
            started = self._monotonic()
            async with self._cycle_lock:
                available_slots = self.max_callback_tasks - len(self._callback_tasks)
                if available_slots <= 0:
                    batch = PublishBatchStats(
                        admission_rejected=1,
                        duration_seconds=max(0.0, self._monotonic() - started),
                    )
                    self._record_batch(batch)
                    return batch
                try:
                    records = await self._store_call(
                        self._store.claim_outbox,
                        self.worker_id,
                        limit=min(self.batch_size, available_slots),
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    self._logger.emit(
                        "qe.publisher.claim_failed",
                        {"worker_id": self.worker_id},
                    )
                    batch = PublishBatchStats(
                        store_errors=1,
                        duration_seconds=max(0.0, self._monotonic() - started),
                    )
                    self._record_batch(batch)
                    return batch

                if not records:
                    batch = PublishBatchStats(
                        duration_seconds=max(0.0, self._monotonic() - started)
                    )
                    self._record_batch(batch)
                    return batch

                attempts = [asyncio.create_task(self._publish_one(record)) for record in records]
                try:
                    outcomes = await asyncio.gather(*attempts)
                except asyncio.CancelledError:
                    for attempt in attempts:
                        attempt.cancel()
                    await self._drain_cancelled_tasks(attempts)
                    raise
                batch = PublishBatchStats(
                    claimed=len(records),
                    published=sum(item.published for item in outcomes),
                    publish_failures=sum(item.publish_failures for item in outcomes),
                    timed_out=sum(item.timed_out for item in outcomes),
                    retried=sum(item.retried for item in outcomes),
                    dead_lettered=sum(item.dead_lettered for item in outcomes),
                    lease_conflicts=sum(item.lease_conflicts for item in outcomes),
                    store_errors=sum(item.store_errors for item in outcomes),
                    accepted_unconfirmed=sum(item.accepted_unconfirmed for item in outcomes),
                    ack_failed=sum(item.ack_failed for item in outcomes),
                    lease_expired=sum(item.lease_expired for item in outcomes),
                    abandoned_callbacks=sum(item.abandoned_callbacks for item in outcomes),
                    ambiguity_persisted=sum(item.ambiguity_persisted for item in outcomes),
                    reconciliation_failed=sum(item.reconciliation_failed for item in outcomes),
                    lease_budget_rejected=sum(item.lease_budget_rejected for item in outcomes),
                    duration_seconds=max(0.0, self._monotonic() - started),
                )
                self._record_batch(batch)
                return batch
        except asyncio.CancelledError:
            self._counters.cancelled_cycles += 1
            raise
        finally:
            self._release_cycle()

    def _record_batch(self, batch: PublishBatchStats) -> None:
        counters = self._counters
        counters.cycles += 1
        if batch.claimed == 0 and batch.store_errors == 0:
            counters.empty_polls += 1
        counters.claimed += batch.claimed
        counters.published += batch.published
        counters.publish_failures += batch.publish_failures
        counters.timed_out += batch.timed_out
        counters.retried += batch.retried
        counters.dead_lettered += batch.dead_lettered
        counters.lease_conflicts += batch.lease_conflicts
        counters.store_errors += batch.store_errors
        counters.accepted_unconfirmed += batch.accepted_unconfirmed
        counters.ack_failed += batch.ack_failed
        counters.lease_expired += batch.lease_expired
        counters.abandoned_callbacks += batch.abandoned_callbacks
        counters.ambiguity_persisted += batch.ambiguity_persisted
        counters.reconciliation_failed += batch.reconciliation_failed
        counters.admission_rejected += batch.admission_rejected
        counters.lease_budget_rejected += batch.lease_budget_rejected

    async def _publish_one(self, stored: StoredOutboxMessage) -> _AttemptOutcome:
        request = PublishRequest.from_stored(stored)
        budget_checked_at = self._lease_budget_timestamp(request)
        if budget_checked_at is None:
            rejected = await self._reject_failed(
                stored,
                _LeaseBudgetExhausted(),
            )
            return replace(rejected, lease_budget_rejected=1)

        try:
            completed, receipt, error = await self._invoke_with_hard_deadline(request)
        except asyncio.CancelledError:
            persisted = await self._persist_ambiguity_before_cancel(stored, "caller_cancelled")
            self._record_cancelled_attempt(persisted)
            raise
        if not completed:
            persisted = await self._persist_ambiguity(stored, "callback_timeout")
            return _AttemptOutcome(
                timed_out=1,
                accepted_unconfirmed=1,
                abandoned_callbacks=1,
                ambiguity_persisted=int(persisted),
                reconciliation_failed=int(not persisted),
            )
        if error is not None:
            return await self._reject_failed(stored, error)
        assert receipt is not None

        published_at = self._valid_ack_timestamp(request)
        if published_at is None:
            persisted = await self._persist_ambiguity(stored, "lease_expired_after_accept")
            return _AttemptOutcome(
                accepted_unconfirmed=1,
                lease_expired=1,
                ambiguity_persisted=int(persisted),
                reconciliation_failed=int(not persisted),
            )
        try:
            acknowledged = await self._store_call(
                self._store.acknowledge_outbox,
                request.message_id,
                stored.lease_token or "",
                published_at=published_at,
            )
        except asyncio.CancelledError:
            persisted = await self._persist_ambiguity_before_cancel(stored, "caller_cancelled")
            self._record_cancelled_attempt(persisted)
            raise
        except Exception:
            self._logger.emit(
                "qe.publisher.ack_failed",
                {"message_id": request.message_id},
            )
            persisted = await self._persist_ambiguity(stored, "ack_failed")
            return _AttemptOutcome(
                store_errors=1,
                accepted_unconfirmed=1,
                ack_failed=1,
                ambiguity_persisted=int(persisted),
                reconciliation_failed=int(not persisted),
            )
        if acknowledged:
            return _AttemptOutcome(published=1)
        persisted = await self._persist_ambiguity(stored, "ack_failed")
        return _AttemptOutcome(
            lease_conflicts=1,
            accepted_unconfirmed=1,
            ack_failed=1,
            ambiguity_persisted=int(persisted),
            reconciliation_failed=int(not persisted),
        )

    async def _invoke_with_hard_deadline(
        self, request: PublishRequest
    ) -> tuple[bool, PublishReceipt | None, BaseException | None]:
        future = self._start_isolated_connector(request)
        try:
            done, _ = await asyncio.wait((future,), timeout=self.publish_timeout)
        except asyncio.CancelledError:
            self._mark_callback_abandoned(future, request)
            raise
        if future not in done:
            self._mark_callback_abandoned(future, request)
            return False, None, None
        try:
            result = future.result()
        except BaseException as caught:
            return True, None, caught
        if type(result) is not PublishReceipt:
            return True, None, _InvalidPublishReceipt()
        try:
            # Copy into a newly validated snapshot. This rejects instances forged
            # through object.__new__/object.__setattr__ and closes a mutation race.
            receipt = PublishReceipt(
                result=result.result,
                receipt_id=result.receipt_id,
                reason_code=result.reason_code,
            )
        except (AttributeError, TypeError, ValueError):
            return True, None, _InvalidPublishReceipt()
        if receipt.result is PublishResult.REJECTED:
            return True, receipt, _ConnectorRejected()
        if receipt.result is not PublishResult.ACCEPTED:
            return True, None, _InvalidPublishReceipt()
        return True, receipt, None

    def _start_isolated_connector(self, request: PublishRequest) -> asyncio.Future[Any]:
        """Run even blocking async connector code outside the publisher loop."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        thread_name = ":".join(
            (
                "outbox-connector",
                _thread_identifier(self.worker_id),
                _thread_identifier(request.message_id),
            )
        )
        self._callback_tasks.add(future)
        self._connector_names[future] = thread_name
        future.add_done_callback(self._callback_done)

        def runner() -> None:
            try:
                result = asyncio.run(self._publish(request))
            except BaseException as caught:
                result = None
                connector_error: BaseException | None = caught
            else:
                connector_error = None
            try:
                loop.call_soon_threadsafe(
                    self._settle_connector_future, future, result, connector_error
                )
            except RuntimeError:
                # The owner closed its loop while a declared leaked connector
                # was still running.  The daemon thread must not keep the process
                # alive and no late result is trusted.
                pass

        thread = threading.Thread(
            target=runner,
            name=thread_name,
            daemon=True,
        )
        try:
            thread.start()
        except Exception as caught:
            # Resource exhaustion must become a normal failed attempt. Leaving
            # this future pending would permanently consume callback capacity.
            self._settle_connector_future(future, None, caught)
        return future

    @staticmethod
    def _settle_connector_future(
        future: asyncio.Future[Any], result: Any, error: BaseException | None
    ) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    def _mark_callback_abandoned(
        self, future: asyncio.Future[Any], request: PublishRequest
    ) -> None:
        if future in self._abandoned_tasks:
            return
        if future.done():
            self._consume_future_result(future)
            return
        self._abandoned_tasks[future] = AbandonedCallback(
            message_id=request.message_id,
            idempotency_key=request.idempotency_key,
            task_name=self._connector_names.get(future, "outbox-connector"),
            lease_deadline=request.lease_deadline,
            abandoned_at=self._safe_clock_timestamp(),
        )

    def _callback_done(self, future: asyncio.Future[Any]) -> None:
        self._callback_tasks.discard(future)
        self._connector_names.pop(future, None)
        abandoned = self._abandoned_tasks.pop(future, None)
        if abandoned is not None:
            self._consume_future_result(future)

    @staticmethod
    def _consume_future_result(future: asyncio.Future[Any]) -> None:
        try:
            future.result()
        except BaseException:
            # A late result cannot safely be ACKed.  Retrieving it only prevents
            # an unobserved-task warning.
            pass

    def _lease_budget_timestamp(self, request: PublishRequest) -> str | None:
        if not request.lease_deadline:
            return None
        try:
            now = self._clock()
            deadline = _parse_timestamp(request.lease_deadline)
            if now.tzinfo is None or now.utcoffset() is None:
                return None
            remaining = (deadline - now.astimezone(timezone.utc)).total_seconds()
            if self.publish_timeout > remaining:
                return None
            return _timestamp(now)
        except Exception:
            self._logger.emit(
                "qe.publisher.lease_budget_validation_failed",
                {"message_id": request.message_id},
            )
            return None

    def _valid_ack_timestamp(self, request: PublishRequest) -> str | None:
        if not request.lease_deadline:
            self._logger.emit(
                "qe.publisher.lease_deadline_missing",
                {"message_id": request.message_id},
            )
            return None
        try:
            now = self._clock()
            deadline = _parse_timestamp(request.lease_deadline)
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("publisher clock returned a naive datetime")
            if now.astimezone(timezone.utc) >= deadline:
                return None
            return _timestamp(now)
        except Exception:
            self._logger.emit(
                "qe.publisher.lease_deadline_validation_failed",
                {"message_id": request.message_id},
            )
            return None

    async def _persist_ambiguity(self, stored: StoredOutboxMessage, reason_code: str) -> bool:
        try:
            return bool(
                await self._store_call(
                    self._store.mark_outbox_ambiguous,
                    stored.message.message_id,
                    stored.lease_token or "",
                    reason_code,
                    marked_at=self._safe_clock_timestamp(),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.emit(
                "qe.publisher.ambiguity_persist_failed",
                {"message_id": stored.message.message_id},
            )
            return False

    async def _persist_ambiguity_before_cancel(
        self, stored: StoredOutboxMessage, reason_code: str
    ) -> bool:
        """Finish durable quarantine despite repeated cancellation requests."""

        task = asyncio.create_task(self._persist_ambiguity(stored, reason_code))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            return bool(task.result())
        except BaseException:
            return False

    def _record_cancelled_attempt(self, persisted: bool) -> None:
        self._counters.accepted_unconfirmed += 1
        self._counters.abandoned_callbacks += 1
        self._counters.ambiguity_persisted += int(persisted)
        self._counters.reconciliation_failed += int(not persisted)

    @staticmethod
    async def _drain_cancelled_tasks(tasks: list[asyncio.Task[Any]]) -> None:
        pending = set(tasks)
        while pending:
            try:
                done, pending = await asyncio.wait(pending)
            except asyncio.CancelledError:
                continue
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass

    async def _reject_failed(
        self,
        stored: StoredOutboxMessage,
        error: BaseException,
    ) -> _AttemptOutcome:
        dead_letter = stored.attempt_count >= self.max_attempts
        retry_at = None if dead_letter else self._retry_timestamp(stored.attempt_count)
        try:
            rejected = await self._store_call(
                self._store.reject_outbox,
                stored.message.message_id,
                stored.lease_token or "",
                self._format_error_safely(error),
                retry_at=retry_at,
                dead_letter=dead_letter,
            )
        except Exception:
            self._logger.emit(
                "qe.publisher.rejection_persist_failed",
                {"message_id": stored.message.message_id},
            )
            return _AttemptOutcome(
                publish_failures=1,
                store_errors=1,
            )
        if not rejected:
            return _AttemptOutcome(
                publish_failures=1,
                lease_conflicts=1,
            )
        return _AttemptOutcome(
            publish_failures=1,
            retried=int(not dead_letter),
            dead_lettered=int(dead_letter),
        )

    async def _store_call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run SQLite work off-loop without abandoning it on caller cancellation."""

        self._ensure_primitives()
        assert self._db_drained is not None
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        self._db_tasks.add(task)
        self._db_drained.clear()
        cancellation_requested = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancellation_requested = True
                continue
            except BaseException:
                break
        self._db_tasks.discard(task)
        if not self._db_tasks:
            self._db_drained.set()
        if cancellation_requested:
            self._consume_future_result(task)
            raise asyncio.CancelledError
        return task.result()

    def _retry_timestamp(self, attempt_count: int) -> str | None:
        try:
            delay = self._retry_delay(attempt_count)
            return _timestamp(self._clock() + timedelta(seconds=delay))
        except Exception:
            # A broken injected clock must not strand the leased row.  Passing
            # None delegates to the store's UTC timestamp for immediate retry.
            self._logger.emit("qe.publisher.retry_timestamp_failed")
            return None

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = max(0, attempt_count - 1)
        if self.base_retry_delay == 0 or self.max_retry_delay == 0:
            capped = 0.0
        elif self.base_retry_delay >= self.max_retry_delay:
            capped = self.max_retry_delay
        elif exponent >= math.ceil(math.log2(self.max_retry_delay / self.base_retry_delay)):
            capped = self.max_retry_delay
        else:
            capped = self.base_retry_delay * (2.0**exponent)
        try:
            jittered = float(self._jitter(capped))
        except Exception:
            self._logger.emit("qe.publisher.retry_jitter_failed")
            return capped
        if not math.isfinite(jittered) or jittered < 0:
            self._logger.emit("qe.publisher.retry_jitter_invalid")
            return capped
        return min(capped, jittered)

    def _format_error_safely(self, error: BaseException) -> str:
        try:
            rendered = self._error_formatter(error)
        except asyncio.CancelledError:
            self._logger.emit("qe.publisher.error_classifier_failed")
            return "connector_failure"
        except Exception:
            self._logger.emit("qe.publisher.error_classifier_failed")
            return "connector_failure"
        if type(rendered) is not str or rendered not in self._allowed_error_codes:
            self._logger.emit("qe.publisher.error_classifier_rejected")
            return "connector_failure"
        return rendered
