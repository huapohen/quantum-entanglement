# ruff: noqa: UP006, UP035, UP045
"""Fail-closed decisions for reconciling durable invocation attempts.

This module validates one immutable workflow invocation binding, one transactionally
consistent attempt-store snapshot, and an optional durable result receipt.  It never claims
work, calls an Agent, changes a lease, accepts a result, or projects a task status.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol

from .attempts import (
    AttemptStatus,
    InvocationAttempt,
    InvocationJob,
    InvocationRecoverySnapshot,
    InvocationStatus,
)
from .protocol import TaskStatus

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_IDENTITY_BYTES = 4_096
_MAX_REFERENCE_BYTES = 16_384
_MAX_ERROR_BYTES = 16_384
_MAX_ERROR_LENGTH = 4_096
_SESSION_STREAM_PREFIX = "session:"
_MAX_SESSION_STREAM_BYTES = _MAX_IDENTITY_BYTES + len(_SESSION_STREAM_PREFIX.encode("utf-8"))


class InvocationRecoveryIntegrityError(RuntimeError):
    """Raised when recovery evidence is malformed, contradictory, or misbound."""


class InvocationRecoveryClosedError(RuntimeError):
    """Raised when a closed recovery coordinator is asked to read durable state."""


class InvocationRecoveryDecision(str, Enum):
    """A non-mutating recovery decision for one workflow task in ``RUNNING``."""

    BLOCKED_MISSING_JOB = "blocked_missing_job"
    FIRST_CLAIM_READY = "first_claim_ready"
    BLOCKED_EFFECT_UNKNOWN = "blocked_effect_unknown"
    WAITING_ACTIVE_LEASE = "waiting_active_lease"
    BLOCKED_RECEIPT_UNVERIFIED = "blocked_receipt_unverified"
    BLOCKED_RESULT_UNCOMMITTED = "blocked_result_uncommitted"
    TERMINAL_FAILURE_EFFECT_UNKNOWN = "terminal_failure_effect_unknown"


@dataclass(frozen=True)
class InvocationBinding:
    """Complete immutable identity decoded from committed invocation-start evidence."""

    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    payload_digest: str

    def __post_init__(self) -> None:
        _validate_binding(self, "binding")


@dataclass(frozen=True)
class InvocationResultReceipt:
    """Attempt-bound proof that a result manifest crossed its durable commit boundary."""

    binding: InvocationBinding
    attempt_id: str
    attempt_number: int
    lease_epoch: int
    lease_token_digest: str
    result_ref: str
    manifest_digest: str
    receipt_id: str
    stream_id: str
    stream_sequence: int

    def __post_init__(self) -> None:
        _validate_receipt_shape(self)


class InvocationRecoveryStore(Protocol):
    """Minimal durable snapshot source used by the read-only coordinator."""

    def recovery_snapshot_for_task(
        self,
        session_id: str,
        task_id: str,
    ) -> InvocationRecoverySnapshot:
        """Return one transactionally consistent job/attempt snapshot."""

    def close(self) -> None:
        """Release resources when ownership was explicitly transferred."""


def _text(
    value: object,
    field: str,
    *,
    maximum_bytes: int = _MAX_IDENTITY_BYTES,
) -> str:
    if type(value) is not str or not value.strip():
        raise InvocationRecoveryIntegrityError(f"{field} must be non-blank text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise InvocationRecoveryIntegrityError(f"{field} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise InvocationRecoveryIntegrityError(f"{field} exceeds its byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise InvocationRecoveryIntegrityError(f"{field} contains a control character")
    return value


def _optional_text(
    value: object,
    field: str,
    *,
    maximum_bytes: int = _MAX_IDENTITY_BYTES,
) -> Optional[str]:
    if value is None:
        return None
    return _text(value, field, maximum_bytes=maximum_bytes)


def _digest(value: object, field: str) -> str:
    text = _text(value, field, maximum_bytes=64)
    if _SHA256_PATTERN.fullmatch(text) is None:
        raise InvocationRecoveryIntegrityError(f"{field} must be canonical SHA-256")
    return text


def _optional_digest(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    return _digest(value, field)


def _integer(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvocationRecoveryIntegrityError(f"{field} is outside its supported integer range")
    return value


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field, maximum_bytes=27)
    if _CANONICAL_UTC_PATTERN.fullmatch(text) is None:
        raise InvocationRecoveryIntegrityError(f"{field} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvocationRecoveryIntegrityError(f"{field} must be a valid timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        != text
    ):
        raise InvocationRecoveryIntegrityError(f"{field} must be canonical UTC")
    return text


def _optional_timestamp(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    return _timestamp(value, field)


def _validate_binding(binding: InvocationBinding, label: str) -> None:
    if type(binding) is not InvocationBinding:
        raise InvocationRecoveryIntegrityError(f"{label} has an invalid type")
    for field in (
        "invocation_id",
        "session_id",
        "plan_id",
        "task_id",
        "agent_id",
        "idempotency_key",
    ):
        _text(getattr(binding, field), f"{label} {field}")
    _digest(binding.payload_digest, f"{label} payload_digest")


def _validate_job(job: InvocationJob) -> None:
    if type(job) is not InvocationJob:
        raise InvocationRecoveryIntegrityError("invocation job observation has an invalid type")
    for field in (
        "invocation_id",
        "session_id",
        "plan_id",
        "task_id",
        "agent_id",
        "idempotency_key",
    ):
        _text(getattr(job, field), f"job {field}")
    _digest(job.payload_digest, "job payload_digest")
    _integer(job.priority, "job priority", maximum=100)
    max_attempts = _integer(job.max_attempts, "job max_attempts", minimum=1)
    attempts_started = _integer(
        job.attempts_started,
        "job attempts_started",
        maximum=max_attempts,
    )
    lease_epoch = _integer(job.lease_epoch, "job lease_epoch")
    if lease_epoch < attempts_started or (attempts_started == 0 and lease_epoch != 0):
        raise InvocationRecoveryIntegrityError("job lease_epoch contradicts attempts_started")
    if type(job.status) is not InvocationStatus:
        raise InvocationRecoveryIntegrityError("job status has an invalid type")
    if job.status is InvocationStatus.FAILED and attempts_started != max_attempts:
        raise InvocationRecoveryIntegrityError("failed job has remaining attempt budget")
    if (
        job.status is InvocationStatus.QUEUED
        and attempts_started > 0
        and attempts_started >= max_attempts
    ):
        raise InvocationRecoveryIntegrityError("queued job exhausted its attempt budget")

    _optional_timestamp(job.requested_available_at, "job requested_available_at")
    _timestamp(job.available_at, "job available_at")
    created_at = _timestamp(job.created_at, "job created_at")
    updated_at = _timestamp(job.updated_at, "job updated_at")
    finished_at = _optional_timestamp(job.finished_at, "job finished_at")
    lease_expires_at = _optional_timestamp(job.lease_expires_at, "job lease_expires_at")
    heartbeat_at = _optional_timestamp(job.heartbeat_at, "job heartbeat_at")
    lease_owner = _optional_text(job.lease_owner, "job lease_owner")
    lease_digest = _optional_digest(job.lease_token_digest, "job lease_token_digest")
    result_ref = _optional_text(
        job.result_ref,
        "job result_ref",
        maximum_bytes=_MAX_REFERENCE_BYTES,
    )
    last_error = _optional_text(job.last_error, "job last_error", maximum_bytes=_MAX_ERROR_BYTES)
    if last_error is not None and len(last_error) > _MAX_ERROR_LENGTH:
        raise InvocationRecoveryIntegrityError("job last_error exceeds its supported length")
    if updated_at < created_at:
        raise InvocationRecoveryIntegrityError("job updated_at precedes creation")
    if attempts_started == 0 and last_error is not None:
        raise InvocationRecoveryIntegrityError("zero-attempt job carries a last_error")
    if (
        job.status is InvocationStatus.FAILED
        or (job.status is InvocationStatus.QUEUED and attempts_started > 0)
    ) and last_error is None:
        raise InvocationRecoveryIntegrityError("failed job state lacks a last_error")

    terminal = job.status in {
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.CANCELED,
    }
    if terminal != (finished_at is not None):
        raise InvocationRecoveryIntegrityError("job finished_at contradicts its status")
    if finished_at is not None and finished_at < created_at:
        raise InvocationRecoveryIntegrityError("job finished_at precedes creation")
    if finished_at is not None and updated_at < finished_at:
        raise InvocationRecoveryIntegrityError("job updated_at precedes finish")

    running_lease = all(
        value is not None for value in (lease_owner, lease_digest, lease_expires_at, heartbeat_at)
    )
    cleared_lease = all(
        value is None for value in (lease_owner, lease_digest, lease_expires_at, heartbeat_at)
    )
    if job.status is InvocationStatus.RUNNING:
        if not running_lease or attempts_started == 0:
            raise InvocationRecoveryIntegrityError("running job has incomplete lease ownership")
        if heartbeat_at is None or lease_expires_at is None:
            raise InvocationRecoveryIntegrityError("running job has incomplete lease timestamps")
        if heartbeat_at < created_at:
            raise InvocationRecoveryIntegrityError("job heartbeat_at precedes creation")
        if updated_at < heartbeat_at:
            raise InvocationRecoveryIntegrityError("job updated_at precedes heartbeat")
        if lease_expires_at <= heartbeat_at or lease_expires_at <= updated_at:
            raise InvocationRecoveryIntegrityError("job lease deadline does not follow activity")
    elif not cleared_lease:
        raise InvocationRecoveryIntegrityError("non-running job retains lease ownership")
    if job.status in {InvocationStatus.SUCCEEDED, InvocationStatus.FAILED} and (
        attempts_started == 0
    ):
        raise InvocationRecoveryIntegrityError("terminal job has no started attempt")
    if result_ref is not None and job.status is not InvocationStatus.SUCCEEDED:
        raise InvocationRecoveryIntegrityError("only a succeeded job may carry a result_ref")


def _validate_attempt(attempt: InvocationAttempt) -> None:
    if type(attempt) is not InvocationAttempt:
        raise InvocationRecoveryIntegrityError("current attempt observation has an invalid type")
    for field in ("attempt_id", "invocation_id", "worker_id"):
        _text(getattr(attempt, field), f"attempt {field}")
    _integer(attempt.attempt_number, "attempt number", minimum=1)
    _integer(attempt.lease_epoch, "attempt lease_epoch", minimum=1)
    _digest(attempt.lease_token_digest, "attempt lease_token_digest")
    if type(attempt.status) is not AttemptStatus:
        raise InvocationRecoveryIntegrityError("attempt status has an invalid type")
    started_at = _timestamp(attempt.started_at, "attempt started_at")
    heartbeat_at = _timestamp(attempt.heartbeat_at, "attempt heartbeat_at")
    lease_expires_at = _timestamp(attempt.lease_expires_at, "attempt lease_expires_at")
    finished_at = _optional_timestamp(attempt.finished_at, "attempt finished_at")
    error = _optional_text(attempt.error, "attempt error", maximum_bytes=_MAX_ERROR_BYTES)
    if error is not None and len(error) > _MAX_ERROR_LENGTH:
        raise InvocationRecoveryIntegrityError("attempt error exceeds its supported length")
    result_ref = _optional_text(
        attempt.result_ref,
        "attempt result_ref",
        maximum_bytes=_MAX_REFERENCE_BYTES,
    )
    if heartbeat_at < started_at:
        raise InvocationRecoveryIntegrityError("attempt timestamps violate start causality")
    if lease_expires_at <= heartbeat_at:
        raise InvocationRecoveryIntegrityError("attempt lease deadline does not follow heartbeat")
    if (attempt.status is AttemptStatus.RUNNING) != (finished_at is None):
        raise InvocationRecoveryIntegrityError("attempt finished_at contradicts its status")
    if attempt.status in {AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED} and error is not None:
        raise InvocationRecoveryIntegrityError("active/succeeded attempt carries an error")
    if attempt.status in {AttemptStatus.FAILED, AttemptStatus.EXPIRED} and error is None:
        raise InvocationRecoveryIntegrityError("failed/expired attempt lacks an error")
    if finished_at is not None and finished_at < started_at:
        raise InvocationRecoveryIntegrityError("attempt finished_at precedes its start")
    if finished_at is not None and finished_at < heartbeat_at:
        raise InvocationRecoveryIntegrityError("attempt finished_at precedes its heartbeat")
    if (
        attempt.status in {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED}
        and finished_at is not None
        and finished_at >= lease_expires_at
    ):
        raise InvocationRecoveryIntegrityError("owned attempt finished outside its lease")
    if (
        attempt.status is AttemptStatus.EXPIRED
        and finished_at is not None
        and finished_at < lease_expires_at
    ):
        raise InvocationRecoveryIntegrityError("expired attempt finished before lease expiry")
    if result_ref is not None and attempt.status is not AttemptStatus.SUCCEEDED:
        raise InvocationRecoveryIntegrityError("only a succeeded attempt may carry a result_ref")


def _validate_snapshot(snapshot: InvocationRecoverySnapshot) -> Optional[InvocationJob]:
    if type(snapshot) is not InvocationRecoverySnapshot:
        raise InvocationRecoveryIntegrityError("attempt recovery snapshot has an invalid type")
    attempt_count = _integer(snapshot.attempt_count, "snapshot attempt_count")
    job = snapshot.job
    current_attempt = snapshot.current_attempt
    if job is None:
        if current_attempt is not None or attempt_count != 0:
            raise InvocationRecoveryIntegrityError("missing job has attached attempt state")
        return None

    _validate_job(job)
    if attempt_count != job.attempts_started:
        raise InvocationRecoveryIntegrityError("snapshot attempt_count differs from the job")
    if attempt_count == 0:
        if current_attempt is not None:
            raise InvocationRecoveryIntegrityError("zero-attempt job has a current attempt")
        return job
    if current_attempt is None:
        raise InvocationRecoveryIntegrityError("attempted job has no current attempt")
    _validate_attempt(current_attempt)
    if (
        current_attempt.invocation_id != job.invocation_id
        or current_attempt.attempt_number != job.attempts_started
        or current_attempt.lease_epoch != job.lease_epoch
    ):
        raise InvocationRecoveryIntegrityError("current attempt identity differs from the job")
    if current_attempt.started_at < job.created_at:
        raise InvocationRecoveryIntegrityError("current attempt starts before its job")
    if current_attempt.finished_at is not None and current_attempt.finished_at > job.updated_at:
        raise InvocationRecoveryIntegrityError("job update precedes its current attempt")
    if job.finished_at is not None and job.finished_at != current_attempt.finished_at:
        raise InvocationRecoveryIntegrityError("job finish differs from its current attempt")

    if job.status is InvocationStatus.RUNNING:
        if (
            current_attempt.status is not AttemptStatus.RUNNING
            or current_attempt.worker_id != job.lease_owner
            or current_attempt.lease_token_digest != job.lease_token_digest
            or current_attempt.heartbeat_at != job.heartbeat_at
            or current_attempt.lease_expires_at != job.lease_expires_at
        ):
            raise InvocationRecoveryIntegrityError(
                "running job ownership differs from its current attempt"
            )
    elif current_attempt.status is AttemptStatus.RUNNING:
        raise InvocationRecoveryIntegrityError("non-running job has a running current attempt")
    elif job.status is InvocationStatus.QUEUED:
        if current_attempt.status not in {AttemptStatus.FAILED, AttemptStatus.EXPIRED}:
            raise InvocationRecoveryIntegrityError("queued job has an incompatible prior attempt")
        if current_attempt.error != job.last_error:
            raise InvocationRecoveryIntegrityError("queued job error differs from its attempt")
    elif job.status is InvocationStatus.SUCCEEDED:
        if (
            current_attempt.status is not AttemptStatus.SUCCEEDED
            or current_attempt.result_ref != job.result_ref
        ):
            raise InvocationRecoveryIntegrityError("succeeded job differs from its current attempt")
    elif job.status is InvocationStatus.FAILED:
        if current_attempt.status not in {AttemptStatus.FAILED, AttemptStatus.EXPIRED}:
            raise InvocationRecoveryIntegrityError("failed job has an incompatible attempt")
        if current_attempt.error != job.last_error:
            raise InvocationRecoveryIntegrityError("failed job error differs from its attempt")
    elif job.status is InvocationStatus.CANCELED:
        if current_attempt.status is not AttemptStatus.CANCELED:
            raise InvocationRecoveryIntegrityError("canceled job has an incompatible attempt")
    return job


def _validate_binding_match(binding: InvocationBinding, job: InvocationJob) -> None:
    mismatches = tuple(
        field
        for field in (
            "invocation_id",
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "idempotency_key",
            "payload_digest",
        )
        if getattr(binding, field) != getattr(job, field)
    )
    if mismatches:
        raise InvocationRecoveryIntegrityError(
            f"invocation job does not match committed binding: {', '.join(mismatches)}"
        )


def _validate_receipt_shape(receipt: InvocationResultReceipt) -> None:
    if type(receipt) is not InvocationResultReceipt:
        raise InvocationRecoveryIntegrityError("result receipt has an invalid type")
    _validate_binding(receipt.binding, "receipt binding")
    _text(receipt.attempt_id, "receipt attempt_id")
    _integer(receipt.attempt_number, "receipt attempt_number", minimum=1)
    _integer(receipt.lease_epoch, "receipt lease_epoch", minimum=1)
    _digest(receipt.lease_token_digest, "receipt lease_token_digest")
    _text(receipt.result_ref, "receipt result_ref", maximum_bytes=_MAX_REFERENCE_BYTES)
    _digest(receipt.manifest_digest, "receipt manifest_digest")
    _text(receipt.receipt_id, "receipt receipt_id")
    expected_stream_id = f"{_SESSION_STREAM_PREFIX}{receipt.binding.session_id}"
    if (
        _text(
            receipt.stream_id,
            "receipt stream_id",
            maximum_bytes=_MAX_SESSION_STREAM_BYTES,
        )
        != expected_stream_id
    ):
        raise InvocationRecoveryIntegrityError("receipt stream_id differs from its session")
    _integer(receipt.stream_sequence, "receipt stream_sequence", minimum=1)


def _validate_receipt_match(
    binding: InvocationBinding,
    snapshot: InvocationRecoverySnapshot,
    receipt: InvocationResultReceipt,
) -> None:
    _validate_receipt_shape(receipt)
    if receipt.binding != binding:
        raise InvocationRecoveryIntegrityError("result receipt binding does not match invocation")
    attempt = snapshot.current_attempt
    if attempt is None:
        raise InvocationRecoveryIntegrityError("result receipt has no owning attempt")
    mismatches = []
    for field in (
        "attempt_id",
        "attempt_number",
        "lease_epoch",
        "lease_token_digest",
    ):
        if getattr(receipt, field) != getattr(attempt, field):
            mismatches.append(field)
    if mismatches:
        raise InvocationRecoveryIntegrityError(
            f"result receipt does not match current attempt: {', '.join(mismatches)}"
        )


def assess_invocation_recovery(
    task_status: TaskStatus,
    binding: InvocationBinding,
    snapshot: InvocationRecoverySnapshot,
    receipt: Optional[InvocationResultReceipt] = None,
) -> InvocationRecoveryDecision:
    """Return a side-effect-free decision for a durably ``RUNNING`` workflow task."""

    if task_status is not TaskStatus.RUNNING:
        raise InvocationRecoveryIntegrityError("invocation recovery requires a RUNNING task")
    _validate_binding(binding, "binding")
    job = _validate_snapshot(snapshot)
    if job is None:
        if receipt is not None:
            _validate_receipt_shape(receipt)
            raise InvocationRecoveryIntegrityError("result receipt exists without invocation job")
        return InvocationRecoveryDecision.BLOCKED_MISSING_JOB
    _validate_binding_match(binding, job)
    if job.status is InvocationStatus.CANCELED:
        raise InvocationRecoveryIntegrityError(
            "canceled invocation recovery requires an authorized cancellation receipt"
        )

    if receipt is not None:
        _validate_receipt_match(binding, snapshot, receipt)
        if job.status is InvocationStatus.SUCCEEDED:
            if job.result_ref is not None and job.result_ref != receipt.result_ref:
                raise InvocationRecoveryIntegrityError(
                    "succeeded invocation result_ref differs from its receipt"
                )
        return InvocationRecoveryDecision.BLOCKED_RECEIPT_UNVERIFIED

    if job.status is InvocationStatus.QUEUED:
        if job.attempts_started == 0:
            return InvocationRecoveryDecision.FIRST_CLAIM_READY
        return InvocationRecoveryDecision.BLOCKED_EFFECT_UNKNOWN
    if job.status is InvocationStatus.RUNNING:
        return InvocationRecoveryDecision.WAITING_ACTIVE_LEASE
    if job.status is InvocationStatus.SUCCEEDED:
        return InvocationRecoveryDecision.BLOCKED_RESULT_UNCOMMITTED
    if job.status is InvocationStatus.FAILED:
        return InvocationRecoveryDecision.TERMINAL_FAILURE_EFFECT_UNKNOWN
    raise InvocationRecoveryIntegrityError("invocation job status is unsupported")


class InvocationRecoveryCoordinator:
    """Read durable attempt evidence without taking execution or projection authority."""

    def __init__(
        self,
        store: Optional[InvocationRecoveryStore] = None,
        *,
        owns_store: bool = False,
    ) -> None:
        if type(owns_store) is not bool:
            raise TypeError("owns_store must be a boolean")
        if owns_store and store is None:
            raise ValueError("owns_store requires an invocation recovery store")
        if store is not None:
            for method_name in ("recovery_snapshot_for_task", "close"):
                if not callable(getattr(store, method_name, None)):
                    raise TypeError("store must provide recovery_snapshot_for_task and close")
        self._store = store
        self._owns_store = owns_store
        self._closed = False
        self._store_cleanup_complete = not owns_store
        self._lock = threading.RLock()

    def __enter__(self) -> InvocationRecoveryCoordinator:
        with self._lock:
            self._require_open()
            return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise InvocationRecoveryClosedError("invocation recovery coordinator is closed")

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def assess(
        self,
        task_status: TaskStatus,
        binding: InvocationBinding,
        receipt: Optional[InvocationResultReceipt] = None,
    ) -> InvocationRecoveryDecision:
        """Read an optional durable snapshot and apply the pure recovery matrix."""

        with self._lock:
            self._require_open()
            if task_status is not TaskStatus.RUNNING:
                raise InvocationRecoveryIntegrityError(
                    "invocation recovery requires a RUNNING task"
                )
            _validate_binding(binding, "binding")
            if receipt is not None:
                _validate_receipt_shape(receipt)
            if self._store is None:
                snapshot = InvocationRecoverySnapshot(None, None, 0)
            else:
                snapshot = self._store.recovery_snapshot_for_task(
                    binding.session_id,
                    binding.task_id,
                )
            return assess_invocation_recovery(task_status, binding, snapshot, receipt)

    def close(self) -> None:
        """Stop reads immediately and retry owned-store cleanup until it succeeds."""

        with self._lock:
            self._closed = True
            if self._store_cleanup_complete:
                return
            if not self._owns_store or self._store is None:  # pragma: no cover - constructor fence.
                raise RuntimeError("owned recovery store cleanup invariant failed")
            self._store.close()
            self._store_cleanup_complete = True


__all__ = [
    "InvocationBinding",
    "InvocationRecoveryClosedError",
    "InvocationRecoveryCoordinator",
    "InvocationRecoveryDecision",
    "InvocationRecoveryIntegrityError",
    "InvocationRecoveryStore",
    "InvocationResultReceipt",
    "assess_invocation_recovery",
]
