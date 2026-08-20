# ruff: noqa: UP006, UP035, UP045
"""Durable SQLite invocation leasing with heartbeat and fencing.

The coordination event log describes what should happen.  This module owns the
separate, mutable execution projection needed to decide which worker may perform an
invocation now.  Every ownership-changing operation uses ``BEGIN IMMEDIATE`` and a
compare-and-set over both an opaque lease token and a monotonically increasing lease
epoch.  An expired worker therefore cannot heartbeat or publish a terminal state after
the lease is fenced. Automatic invocation retry remains disabled until durable effect or
receipt reconciliation can authorize another attempt.
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
from asyncio import CancelledError
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from inspect import signature
from types import CodeType, FunctionType
from typing import Any, NoReturn, Optional, Tuple, Type, TypeVar, cast

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
_MAX_IDENTITY_BYTES = 4_096
_MAX_REFERENCE_BYTES = 16_384
_MAX_ERROR_BYTES = 16_384
_MAX_SQLITE_INTEGER = (1 << 63) - 1


class InvocationConflictError(RuntimeError):
    """Raised when an idempotency boundary is reused for different work."""


class InvocationIntegrityError(RuntimeError):
    """Raised when persisted invocation state violates its durable contract."""


class InvocationClockRegressionError(InvocationIntegrityError):
    """Raised when a mutation clock is earlier than durable invocation activity."""


class InvocationCommitAmbiguityError(InvocationIntegrityError):
    """Raised when a failed commit cannot be reconciled to one exact durable outcome."""

    code = "invocation_commit_ambiguous"

    def __init__(self) -> None:
        super().__init__("invocation mutation commit could not be reconciled")


class InvocationTransactionError(RuntimeError):
    """Raised when a write commit fails and its rollback is confirmed."""

    code = "invocation_transaction_failed"

    def __init__(self) -> None:
        super().__init__("invocation mutation transaction was rolled back")


class InvocationStoreClosedError(RuntimeError):
    """Raised when a closed invocation store is asked to access durable state."""

    code = "invocation_store_closed"

    def __init__(self) -> None:
        super().__init__("invocation attempt store is closed")


class InvocationStoreProcessMismatchError(RuntimeError):
    """Raised when a store inherited through fork is used by the child process."""

    code = "invocation_store_process_mismatch"

    def __init__(self) -> None:
        super().__init__("invocation attempt store belongs to another process")


class InvocationStorePoisonedError(InvocationIntegrityError):
    """Raised after a transaction failure permanently quarantines this store instance."""

    code = "invocation_store_poisoned"

    def __init__(self) -> None:
        super().__init__("invocation attempt store is poisoned and must be reopened")


class _ControlSignalKind(str, Enum):
    KEYBOARD_INTERRUPT = "keyboard_interrupt"
    SYSTEM_EXIT = "system_exit"
    GENERATOR_EXIT = "generator_exit"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class _ControlSignalDescriptor:
    kind: _ControlSignalKind
    system_exit_code: Optional[object] = None


class _InvocationControlSignal(BaseException):
    """Internal signal carrying only a sanitized control-flow descriptor."""

    def __init__(
        self,
        descriptor: _ControlSignalDescriptor,
        nonce: object,
        *,
        ambiguity: bool,
    ) -> None:
        super().__init__("invocation control flow interrupted")
        self.descriptor = descriptor
        self.nonce = nonce
        self.ambiguity = ambiguity


class _InvocationCloseControlSignal(BaseException):
    """Internal signal carrying a sanitized close interruption."""

    def __init__(self, descriptor: _ControlSignalDescriptor, nonce: object) -> None:
        super().__init__("invocation store close interrupted")
        self.descriptor = descriptor
        self.nonce = nonce


@dataclass(frozen=True)
class _SafeTransactionBodyError:
    error_type: Type[Exception]
    message: str


class _CommitOutcomeUnknown(RuntimeError):
    """Internal signal that COMMIT returned an error after ending the transaction."""

    def __init__(
        self,
        *,
        may_reconcile: bool,
        boundary_nonce: Optional[object],
        control_signal: Optional[_ControlSignalDescriptor] = None,
    ) -> None:
        super().__init__("invocation mutation transaction outcome is unknown")
        self.may_reconcile = may_reconcile
        self.boundary_nonce = boundary_nonce
        self.control_signal = control_signal


class _ConnectionCloseFailure(RuntimeError):
    """Sanitized internal cause for a failed connection close acknowledgement."""


class _ReadTransactionOutcomeUnknown(RuntimeError):
    """Sanitized internal cause for an unrecoverable read transaction failure."""

    def __init__(
        self,
        poison_nonce: object,
        boundary_nonce: Optional[object],
        control_signal: Optional[_ControlSignalDescriptor],
    ) -> None:
        super().__init__("invocation read transaction outcome is unknown")
        self.poison_nonce = poison_nonce
        self.boundary_nonce = boundary_nonce
        self.control_signal = control_signal


class InvocationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


def _control_signal_descriptor(error: BaseException) -> Optional[_ControlSignalDescriptor]:
    if type(error) is KeyboardInterrupt:
        return _ControlSignalDescriptor(_ControlSignalKind.KEYBOARD_INTERRUPT)
    if type(error) is GeneratorExit:
        return _ControlSignalDescriptor(_ControlSignalKind.GENERATOR_EXIT)
    if type(error) is CancelledError:
        return _ControlSignalDescriptor(_ControlSignalKind.CANCELLED)
    if type(error) is SystemExit:
        code = error.code
        safe_code: Optional[object]
        if code is None or type(code) is bool:
            safe_code = code
        elif type(code) is int and 0 <= code <= 255:
            safe_code = code
        else:
            safe_code = 1
        return _ControlSignalDescriptor(_ControlSignalKind.SYSTEM_EXIT, safe_code)
    return None


def _safe_transaction_body_error(
    error: BaseException,
) -> Optional[_SafeTransactionBodyError]:
    """Copy only exact library-authored validation and integrity failures."""

    if type(error) not in {
        InvocationClockRegressionError,
        InvocationConflictError,
        InvocationIntegrityError,
        TypeError,
        ValueError,
    }:
        return None
    traceback_cursor = error.__traceback__
    if traceback_cursor is None:
        return None
    while traceback_cursor.tb_next is not None:
        traceback_cursor = traceback_cursor.tb_next
    if traceback_cursor.tb_frame.f_code not in _TRUSTED_TRANSACTION_BODY_CODES:
        return None
    try:
        message = str(error)
    except BaseException:
        return None
    return _SafeTransactionBodyError(cast(Type[Exception], type(error)), message)


def _raise_safe_transaction_body_error(error: _SafeTransactionBodyError) -> NoReturn:
    raise error.error_type(error.message) from None


def _collect_trusted_module_code_objects() -> frozenset[CodeType]:
    """Freeze code provenance before configured providers can execute."""

    trusted: set[CodeType] = set()
    pending = list(globals().values())
    visited: set[int] = set()
    while pending:
        candidate = pending.pop()
        identity = id(candidate)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(candidate, FunctionType):
            if candidate.__globals__ is globals():
                trusted.add(candidate.__code__)
                if candidate.__closure__ is not None:
                    for cell in candidate.__closure__:
                        try:
                            pending.append(cell.cell_contents)
                        except ValueError:
                            continue
        elif isinstance(candidate, type) and candidate.__module__ == __name__:
            pending.extend(vars(candidate).values())
        elif isinstance(candidate, (classmethod, staticmethod)):
            pending.append(candidate.__func__)
        elif isinstance(candidate, property):
            pending.extend(
                value
                for value in (candidate.fget, candidate.fset, candidate.fdel)
                if value is not None
            )
    return frozenset(trusted)


def _normalized_control_signal_descriptor(
    descriptor: object,
) -> Optional[_ControlSignalDescriptor]:
    """Copy one internal descriptor while rejecting object-bearing forgeries."""

    if type(descriptor) is not _ControlSignalDescriptor:
        return None
    if descriptor.kind is _ControlSignalKind.KEYBOARD_INTERRUPT:
        return _ControlSignalDescriptor(_ControlSignalKind.KEYBOARD_INTERRUPT)
    if descriptor.kind is _ControlSignalKind.GENERATOR_EXIT:
        return _ControlSignalDescriptor(_ControlSignalKind.GENERATOR_EXIT)
    if descriptor.kind is _ControlSignalKind.CANCELLED:
        return _ControlSignalDescriptor(_ControlSignalKind.CANCELLED)
    if descriptor.kind is _ControlSignalKind.SYSTEM_EXIT:
        code = descriptor.system_exit_code
        safe_code: Optional[object]
        if code is None or type(code) is bool:
            safe_code = code
        elif type(code) is int and 0 <= code <= 255:
            safe_code = code
        else:
            safe_code = 1
        return _ControlSignalDescriptor(_ControlSignalKind.SYSTEM_EXIT, safe_code)
    return None


def _raise_clean_control_signal(
    descriptor: _ControlSignalDescriptor,
    *,
    ambiguity: bool = False,
    close_failure: bool = False,
) -> NoReturn:
    normalized = _normalized_control_signal_descriptor(descriptor)
    if (
        normalized is None
        or type(ambiguity) is not bool
        or type(close_failure) is not bool
        or (ambiguity and close_failure)
    ):
        raise InvocationTransactionError()
    if ambiguity:
        cause: Optional[BaseException] = InvocationCommitAmbiguityError()
    elif close_failure:
        cause = InvocationStoreClosedError()
    else:
        cause = None
    if normalized.kind is _ControlSignalKind.KEYBOARD_INTERRUPT:
        raise KeyboardInterrupt() from cause
    if normalized.kind is _ControlSignalKind.GENERATOR_EXIT:
        raise GeneratorExit() from cause
    if normalized.kind is _ControlSignalKind.CANCELLED:
        raise CancelledError() from cause
    if normalized.kind is _ControlSignalKind.SYSTEM_EXIT:
        raise SystemExit(normalized.system_exit_code) from cause
    raise RuntimeError("unsupported invocation control signal")


_Method = TypeVar("_Method", bound=Callable[..., Any])
_Value = TypeVar("_Value")


def _bind_store_process(method: _Method) -> _Method:
    """Reject a fork-inherited store before touching its lock or SQLite connection."""

    def process_bound(*args: Any, **kwargs: Any) -> Any:
        store = args[0]
        process_matches = store._creator_pid == os.getpid()
        if not process_matches:
            del args, kwargs, store
            raise InvocationStoreProcessMismatchError() from None
        return method(*args, **kwargs)

    process_bound.__name__ = method.__name__
    process_bound.__qualname__ = method.__qualname__
    process_bound.__doc__ = method.__doc__
    process_bound.__module__ = method.__module__
    process_bound.__annotations__ = dict(method.__annotations__)
    process_bound.__signature__ = signature(method)  # type: ignore[attr-defined]
    return cast(_Method, process_bound)


def _sanitize_control_signals(method: _Method) -> _Method:
    """Reissue control flow without retaining public call arguments or raw exceptions."""

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        descriptor: Optional[_ControlSignalDescriptor] = None
        invalid_base_exception = False
        transaction_failure = False
        commit_ambiguity = False
        control_ambiguity = False
        boundary_nonce = object()
        store = args[0]
        process_matches = store._creator_pid == os.getpid()
        if not process_matches:
            del args, kwargs, store
            raise InvocationStoreProcessMismatchError() from None
        store._push_control_signal_boundary(boundary_nonce)
        try:
            try:
                return method(*args, **kwargs)
            except BaseException as error:
                if isinstance(error, _InvocationControlSignal):
                    if type(error) is _InvocationControlSignal and error.nonce is boundary_nonce:
                        descriptor = _normalized_control_signal_descriptor(error.descriptor)
                        control_ambiguity = error.ambiguity
                    if descriptor is None:
                        invalid_base_exception = True
                elif isinstance(error, InvocationTransactionError):
                    transaction_failure = True
                elif isinstance(error, InvocationCommitAmbiguityError):
                    commit_ambiguity = True
                else:
                    descriptor = _control_signal_descriptor(error)
                    if descriptor is not None:
                        control_ambiguity = (
                            type(error.__cause__) is InvocationCommitAmbiguityError
                            and error.__cause__.__traceback__ is None
                        )
                    if descriptor is None:
                        if isinstance(error, Exception):
                            raise
                        invalid_base_exception = True
        finally:
            store._pop_control_signal_boundary(boundary_nonce)
        del args, kwargs, store
        if descriptor is not None:
            _raise_clean_control_signal(descriptor, ambiguity=control_ambiguity)
        if invalid_base_exception or transaction_failure:
            raise InvocationTransactionError() from None
        if commit_ambiguity:
            raise InvocationCommitAmbiguityError() from None
        raise RuntimeError("invocation control signal classification is missing")

    wrapped.__name__ = method.__name__
    wrapped.__qualname__ = method.__qualname__
    wrapped.__doc__ = method.__doc__
    wrapped.__module__ = method.__module__
    wrapped.__annotations__ = dict(method.__annotations__)
    wrapped.__signature__ = signature(method)  # type: ignore[attr-defined]

    return cast(_Method, wrapped)


def _sanitize_close_signals(method: _Method) -> _Method:
    """Publish close failures without retaining the store or raw close exception."""

    def wrapped_close(*args: Any, **kwargs: Any) -> Any:
        descriptor: Optional[_ControlSignalDescriptor] = None
        close_failure = False
        boundary_nonce = object()
        store = args[0]
        process_matches = store._creator_pid == os.getpid()
        if not process_matches:
            del args, kwargs, store
            raise InvocationStoreProcessMismatchError() from None
        store._push_control_signal_boundary(boundary_nonce)
        try:
            try:
                return method(*args, **kwargs)
            except BaseException as error:
                if isinstance(error, _InvocationCloseControlSignal):
                    if (
                        type(error) is _InvocationCloseControlSignal
                        and error.nonce is boundary_nonce
                    ):
                        descriptor = _normalized_control_signal_descriptor(error.descriptor)
                    if descriptor is None:
                        close_failure = True
                elif isinstance(error, InvocationStoreClosedError):
                    close_failure = True
                else:
                    descriptor = _control_signal_descriptor(error)
                    if descriptor is None:
                        close_failure = True
        finally:
            store._pop_control_signal_boundary(boundary_nonce)
        del args, kwargs, store
        if descriptor is not None:
            _raise_clean_control_signal(descriptor, close_failure=True)
        if close_failure:
            raise InvocationStoreClosedError() from None
        raise RuntimeError("invocation close signal classification is missing")

    wrapped_close.__name__ = method.__name__
    wrapped_close.__qualname__ = method.__qualname__
    wrapped_close.__doc__ = method.__doc__
    wrapped_close.__module__ = method.__module__
    wrapped_close.__annotations__ = dict(method.__annotations__)
    wrapped_close.__signature__ = signature(method)  # type: ignore[attr-defined]
    return cast(_Method, wrapped_close)


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


def _required(
    value: str,
    name: str,
    *,
    maximum_bytes: int = _MAX_IDENTITY_BYTES,
) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is required")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{name} exceeds its byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} contains a control character")
    return value


def _normalize_timestamp(value: str, name: str = "timestamp") -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an RFC 3339 string")
    if not _RFC3339_PATTERN.fullmatch(value) or value.endswith("-00:00"):
        raise ValueError(f"{name} must be a strict RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        normalized = parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _lease_deadline(now: str, lease_seconds: float) -> str:
    if not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool):
        raise TypeError("lease_seconds must be a number")
    try:
        seconds = float(lease_seconds)
    except OverflowError as exc:
        raise ValueError("lease_seconds exceeds the supported datetime range") from exc
    if not math.isfinite(seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be finite and greater than zero")
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    try:
        candidate = parsed + timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise ValueError("lease_seconds exceeds the supported datetime range") from exc
    deadline = _normalize_timestamp(candidate.isoformat(), "lease deadline")
    if deadline <= now:
        raise ValueError("lease_seconds is below the durable timestamp precision")
    return deadline


def _stored_error(error: str) -> str:
    value = _required(error, "error", maximum_bytes=_MAX_ERROR_BYTES).strip()
    return value[:_MAX_ERROR_LENGTH]


def _lease_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_non_regressing_clock(now: str, *durable_times: str) -> None:
    if any(now < durable_time for durable_time in durable_times):
        raise InvocationClockRegressionError(
            "invocation store clock precedes durable invocation activity"
        )


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


def _persisted_text(
    value: Any,
    name: str,
    *,
    required: bool = True,
    maximum_bytes: int = _MAX_IDENTITY_BYTES,
) -> str:
    if type(value) is not str:
        raise TypeError(f"persisted {name} must use SQLite TEXT storage")
    if required and not value.strip():
        raise ValueError(f"persisted {name} must not be blank")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"persisted {name} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"persisted {name} exceeds its byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"persisted {name} contains a control character")
    return value


def _persisted_optional_text(
    value: Any,
    name: str,
    *,
    maximum_bytes: int = _MAX_IDENTITY_BYTES,
) -> Optional[str]:
    if value is None:
        return None
    return _persisted_text(value, name, maximum_bytes=maximum_bytes)


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
    lease_token_digest: Optional[str]
    lease_expires_at: Optional[str]
    heartbeat_at: Optional[str]
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


@dataclass(frozen=True)
class InvocationRecoverySnapshot:
    """One transactionally consistent, allocation-bounded job/attempt observation."""

    job: Optional[InvocationJob]
    current_attempt: Optional[InvocationAttempt]
    attempt_count: int


class SQLiteInvocationAttemptStore:
    """Durable invocation queue shared by process-local stores on one host.

    Each process must construct its own store after process creation; a store or SQLite
    connection inherited through POSIX fork is rejected. Pass the same filesystem path as
    the event store so both projections live in one database. ``:memory:`` is useful for
    unit tests but cannot be shared between connections or processes.
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
        self._creator_pid = os.getpid()
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
        self._closed = False
        self._connection_closed = False
        self._poisoned = False
        self._poison_nonce = object()
        self._control_signal_boundaries = threading.local()
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

    @_bind_store_process
    def __enter__(self) -> SQLiteInvocationAttemptStore:
        with self._lock:
            self._require_usable()
            return self

    @_sanitize_close_signals
    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        try:
            self.close()
        except InvocationStoreClosedError:
            if _exc is not None:
                return
            raise
        except BaseException as close_error:
            descriptor = _control_signal_descriptor(close_error)
            cause = close_error.__cause__
            trusted_close_control = (
                descriptor is not None
                and type(cause) is InvocationStoreClosedError
                and cause.__traceback__ is None
            )
            if not trusted_close_control or _exc is None:
                raise
            return

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
            self._require_usable()
            began, begin_control = self._start_write_transaction(self._connection)
            if not began:
                if begin_control is not None:
                    self._raise_invocation_control_signal(begin_control)
                raise InvocationTransactionError()
            body_control: Optional[_ControlSignalDescriptor] = None
            safe_body_error: Optional[_SafeTransactionBodyError] = None
            try:
                yield self._connection
            except BaseException as body_error:
                body_control = _control_signal_descriptor(body_error)
                if body_control is None:
                    safe_body_error = _safe_transaction_body_error(body_error)
                try:
                    self._rollback_write_transaction(self._connection)
                except BaseException as rollback_error:
                    rollback_control = _control_signal_descriptor(rollback_error)
                    close_control = self._poison_store()
                    raise _CommitOutcomeUnknown(
                        may_reconcile=False,
                        boundary_nonce=self._active_control_signal_boundary(),
                        control_signal=close_control or rollback_control or body_control,
                    ) from rollback_error
            else:
                committed, commit_control = self._finish_write_transaction(self._connection)
                if committed:
                    return
                if commit_control is not None:
                    self._raise_invocation_control_signal(commit_control)
                raise InvocationTransactionError()
            if body_control is not None:
                self._raise_invocation_control_signal(body_control)
            if safe_body_error is not None:
                _raise_safe_transaction_body_error(safe_body_error)
            raise InvocationTransactionError()

    def _start_write_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> Tuple[bool, Optional[_ControlSignalDescriptor]]:
        """Start a write transaction without retaining an untrusted begin failure."""

        try:
            self._begin_write_transaction(connection)
        except BaseException as error:
            descriptor = _control_signal_descriptor(error)
            try:
                transaction_open = self._write_transaction_open(connection)
            except BaseException as state_error:
                state_control = _control_signal_descriptor(state_error)
                close_control = self._poison_store()
                raise _CommitOutcomeUnknown(
                    may_reconcile=False,
                    boundary_nonce=self._active_control_signal_boundary(),
                    control_signal=close_control or state_control or descriptor,
                ) from state_error
            if transaction_open:
                try:
                    self._rollback_write_transaction(connection)
                except BaseException as rollback_error:
                    rollback_control = _control_signal_descriptor(rollback_error)
                    close_control = self._poison_store()
                    raise _CommitOutcomeUnknown(
                        may_reconcile=False,
                        boundary_nonce=self._active_control_signal_boundary(),
                        control_signal=close_control or rollback_control or descriptor,
                    ) from rollback_error
            return False, descriptor
        return True, None

    def _finish_write_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> Tuple[bool, Optional[_ControlSignalDescriptor]]:
        """Finish COMMIT in a frame that does not retain a rolled-back driver Exception."""

        try:
            self._commit_write_transaction(connection)
        except BaseException as error:
            descriptor = _control_signal_descriptor(error)
            try:
                transaction_open = self._write_transaction_open(connection)
            except BaseException as state_error:
                state_control = _control_signal_descriptor(state_error)
                close_control = self._poison_store()
                raise _CommitOutcomeUnknown(
                    may_reconcile=False,
                    boundary_nonce=self._active_control_signal_boundary(),
                    control_signal=close_control or state_control or descriptor,
                ) from state_error
            if not transaction_open:
                raise _CommitOutcomeUnknown(
                    may_reconcile=True,
                    boundary_nonce=self._active_control_signal_boundary(),
                    control_signal=descriptor,
                ) from error
            try:
                self._rollback_write_transaction(connection)
            except BaseException as rollback_error:
                rollback_control = _control_signal_descriptor(rollback_error)
                close_control = self._poison_store()
                raise _CommitOutcomeUnknown(
                    may_reconcile=False,
                    boundary_nonce=self._active_control_signal_boundary(),
                    control_signal=close_control or rollback_control or descriptor,
                ) from rollback_error
            return False, descriptor
        return True, None

    @staticmethod
    def _begin_write_transaction(connection: sqlite3.Connection) -> None:
        """Begin one write transaction through a fault-injectable boundary."""

        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _write_transaction_open(connection: sqlite3.Connection) -> bool:
        """Inspect transaction state through a fault-injectable boundary."""

        return connection.in_transaction

    @staticmethod
    def _commit_write_transaction(connection: sqlite3.Connection) -> None:
        """Commit one write transaction through a fault-injectable boundary."""

        connection.execute("COMMIT")

    @staticmethod
    def _rollback_write_transaction(connection: sqlite3.Connection) -> None:
        """Roll back one write transaction through a fault-injectable boundary."""

        connection.execute("ROLLBACK")

    @staticmethod
    def _close_connection(connection: sqlite3.Connection) -> None:
        """Close a connection through a fault-injectable boundary."""

        connection.close()

    def _close_connection_outcome(
        self,
        connection: sqlite3.Connection,
    ) -> Tuple[bool, Optional[_ControlSignalDescriptor]]:
        """Return a sanitized close outcome without retaining an untrusted driver error."""

        try:
            self._close_connection(connection)
        except BaseException as error:
            return False, _control_signal_descriptor(error)
        return True, None

    @staticmethod
    def _begin_read_transaction(connection: sqlite3.Connection) -> None:
        """Begin a read transaction through a fault-injectable boundary."""

        connection.execute("BEGIN")

    @staticmethod
    def _commit_read_transaction(connection: sqlite3.Connection) -> None:
        """Commit a read transaction through a fault-injectable boundary."""

        connection.execute("COMMIT")

    @staticmethod
    def _rollback_read_transaction(connection: sqlite3.Connection) -> None:
        """Roll back a read transaction through a fault-injectable boundary."""

        connection.execute("ROLLBACK")

    def _poison_store(self) -> Optional[_ControlSignalDescriptor]:
        """Permanently quarantine this instance and best-effort close its connection."""

        self._poisoned = True
        self._closed = True
        close_control: Optional[_ControlSignalDescriptor] = None
        if not self._connection_closed:
            self._connection_closed, close_control = self._close_connection_outcome(
                self._connection
            )
        return close_control

    def _require_usable(self) -> None:
        if self._creator_pid != os.getpid():
            raise InvocationStoreProcessMismatchError()
        if self._poisoned:
            raise InvocationStorePoisonedError()
        if self._closed:
            raise InvocationStoreClosedError()

    def _push_control_signal_boundary(self, nonce: object) -> None:
        stack = getattr(self._control_signal_boundaries, "stack", None)
        if stack is None:
            stack = []
            self._control_signal_boundaries.stack = stack
        stack.append(nonce)

    def _pop_control_signal_boundary(self, nonce: object) -> None:
        stack = getattr(self._control_signal_boundaries, "stack", None)
        if not stack or stack[-1] is not nonce:
            raise RuntimeError("invocation control signal boundary is inconsistent")
        stack.pop()
        if not stack:
            del self._control_signal_boundaries.stack

    def _active_control_signal_boundary(self) -> Optional[object]:
        stack = getattr(self._control_signal_boundaries, "stack", None)
        return stack[-1] if stack else None

    def _raise_invocation_control_signal(
        self,
        descriptor: _ControlSignalDescriptor,
        *,
        ambiguity: bool = False,
    ) -> NoReturn:
        normalized = _normalized_control_signal_descriptor(descriptor)
        if normalized is None or type(ambiguity) is not bool:
            raise InvocationTransactionError()
        nonce = self._active_control_signal_boundary()
        if nonce is None:
            _raise_clean_control_signal(normalized, ambiguity=ambiguity)
        raise _InvocationControlSignal(
            normalized,
            nonce,
            ambiguity=ambiguity,
        ) from None

    def _raise_invocation_close_control_signal(
        self,
        descriptor: _ControlSignalDescriptor,
    ) -> NoReturn:
        normalized = _normalized_control_signal_descriptor(descriptor)
        nonce = self._active_control_signal_boundary()
        if normalized is None or nonce is None:
            raise InvocationStoreClosedError()
        raise _InvocationCloseControlSignal(normalized, nonce) from None

    @staticmethod
    def _close_failed() -> NoReturn:
        raise InvocationStoreClosedError() from _ConnectionCloseFailure(
            "invocation store connection close was not acknowledged"
        )

    def _trusted_commit_error(self, error: _CommitOutcomeUnknown) -> bool:
        return (
            type(error) is _CommitOutcomeUnknown
            and error.boundary_nonce is self._active_control_signal_boundary()
            and type(error.may_reconcile) is bool
        )

    def _unreconciled_commit(
        self,
        error: _CommitOutcomeUnknown,
        *,
        control_signal: Optional[_ControlSignalDescriptor] = None,
    ) -> NoReturn:
        trusted = self._trusted_commit_error(error)
        raw_descriptor: object = control_signal
        if raw_descriptor is None and trusted:
            raw_descriptor = error.control_signal
        descriptor = _normalized_control_signal_descriptor(raw_descriptor)
        if not trusted or raw_descriptor is not None:
            close_control = self._poison_store()
            normalized_close = _normalized_control_signal_descriptor(close_control)
            if normalized_close is not None:
                descriptor = normalized_close
        if trusted:
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
        if descriptor is not None:
            self._raise_invocation_control_signal(descriptor, ambiguity=True)
        raise InvocationCommitAmbiguityError() from None

    def _after_reconciled_commit(
        self,
        error: _CommitOutcomeUnknown,
        value: _Value,
    ) -> _Value:
        """Return an exact durable result or reissue a sanitized committed control signal."""

        if not self._trusted_commit_error(error):
            self._unreconciled_commit(error)
        descriptor = error.control_signal
        if descriptor is None:
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            return value
        normalized = _normalized_control_signal_descriptor(descriptor)
        if normalized is None:
            self._unreconciled_commit(error)
        error.__cause__ = None
        error.__context__ = None
        error.__traceback__ = None
        self._raise_invocation_control_signal(normalized)

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Pin multiple recovery queries to one SQLite snapshot without taking a write lock."""

        try:
            with self._read_transaction_inner() as connection:
                yield connection
        except _ReadTransactionOutcomeUnknown as error:
            if type(error) is not _ReadTransactionOutcomeUnknown:
                raise
            if error.poison_nonce is not self._poison_nonce or not self._poisoned:
                raise
            active_boundary = self._active_control_signal_boundary()
            control_signal = _normalized_control_signal_descriptor(error.control_signal)
            trusted_control = control_signal is not None and error.boundary_nonce is active_boundary
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if trusted_control and control_signal is not None:
                _raise_clean_control_signal(control_signal, ambiguity=True)
            raise InvocationStorePoisonedError() from error

    @contextmanager
    def _read_transaction_inner(self) -> Iterator[sqlite3.Connection]:
        """Own raw SQLite read faults inside a frame that never exposes their chain."""

        with self._lock:
            self._require_usable()
            try:
                self._begin_read_transaction(self._connection)
            except BaseException as begin_error:
                begin_control = _control_signal_descriptor(begin_error)
                try:
                    transaction_open = self._connection.in_transaction
                except BaseException as state_error:
                    state_control = _control_signal_descriptor(state_error)
                    close_control = self._poison_store()
                    raise _ReadTransactionOutcomeUnknown(
                        self._poison_nonce,
                        self._active_control_signal_boundary(),
                        close_control or state_control or begin_control,
                    ) from state_error
                if transaction_open:
                    try:
                        self._rollback_read_transaction(self._connection)
                    except BaseException as rollback_error:
                        rollback_control = _control_signal_descriptor(rollback_error)
                        close_control = self._poison_store()
                        raise _ReadTransactionOutcomeUnknown(
                            self._poison_nonce,
                            self._active_control_signal_boundary(),
                            close_control or rollback_control or begin_control,
                        ) from rollback_error
                raise

            try:
                yield self._connection
            except BaseException as body_error:
                body_control = _control_signal_descriptor(body_error)
                try:
                    self._rollback_read_transaction(self._connection)
                except BaseException as rollback_error:
                    rollback_control = _control_signal_descriptor(rollback_error)
                    close_control = self._poison_store()
                    raise _ReadTransactionOutcomeUnknown(
                        self._poison_nonce,
                        self._active_control_signal_boundary(),
                        close_control or rollback_control or body_control,
                    ) from rollback_error
                raise

            try:
                self._commit_read_transaction(self._connection)
            except BaseException as commit_error:
                commit_control = _control_signal_descriptor(commit_error)
                try:
                    transaction_open = self._connection.in_transaction
                except BaseException as state_error:
                    state_control = _control_signal_descriptor(state_error)
                    close_control = self._poison_store()
                    raise _ReadTransactionOutcomeUnknown(
                        self._poison_nonce,
                        self._active_control_signal_boundary(),
                        close_control or state_control or commit_control,
                    ) from state_error
                if transaction_open:
                    try:
                        self._rollback_read_transaction(self._connection)
                    except BaseException as rollback_error:
                        rollback_control = _control_signal_descriptor(rollback_error)
                        close_control = self._poison_store()
                        raise _ReadTransactionOutcomeUnknown(
                            self._poison_nonce,
                            self._active_control_signal_boundary(),
                            close_control or rollback_control or commit_control,
                        ) from rollback_error
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
            if lease_epoch < attempts_started or (attempts_started == 0 and lease_epoch != 0):
                raise ValueError("persisted invocation lease_epoch contradicts attempts_started")
            if status is InvocationStatus.FAILED and attempts_started != max_attempts:
                raise ValueError("persisted failed invocation has remaining attempt budget")
            if (
                status is InvocationStatus.QUEUED
                and attempts_started > 0
                and attempts_started >= max_attempts
            ):
                raise ValueError("persisted queued invocation exhausted its attempt budget")

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
            cleared_lease = (
                lease_owner is None
                and lease_digest is None
                and lease_expires_at is None
                and heartbeat_at is None
            )
            if status is InvocationStatus.RUNNING:
                valid_lease_shape = running_lease
            else:
                valid_lease_shape = cleared_lease
            if not valid_lease_shape:
                raise ValueError("persisted invocation lease fields contradict status")
            if status is InvocationStatus.RUNNING and attempts_started == 0:
                raise ValueError("persisted running invocation has no started attempt")

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
            if status in {InvocationStatus.SUCCEEDED, InvocationStatus.FAILED} and (
                attempts_started == 0
            ):
                raise ValueError("persisted terminal invocation has no started attempt")

            last_error = _persisted_optional_text(
                row["last_error"],
                "invocation last_error",
                maximum_bytes=_MAX_ERROR_BYTES,
            )
            if last_error is not None and len(last_error) > _MAX_ERROR_LENGTH:
                raise ValueError("persisted invocation last_error exceeds its supported length")
            if attempts_started == 0 and last_error is not None:
                raise ValueError("persisted zero-attempt invocation carries a last_error")
            if (
                status is InvocationStatus.FAILED
                or (status is InvocationStatus.QUEUED and attempts_started > 0)
            ) and last_error is None:
                raise ValueError("persisted failed invocation state lacks a last_error")
            requested_available_at = _persisted_optional_timestamp(
                row["requested_available_at"], "invocation requested_available_at"
            )
            available_at = _persisted_timestamp(row["available_at"], "invocation available_at")
            created_at = _persisted_timestamp(row["created_at"], "invocation created_at")
            updated_at = _persisted_timestamp(row["updated_at"], "invocation updated_at")
            if updated_at < created_at:
                raise ValueError("persisted invocation updated_at precedes creation")
            if finished_at is not None and finished_at < created_at:
                raise ValueError("persisted invocation finished_at precedes creation")
            if finished_at is not None and updated_at < finished_at:
                raise ValueError("persisted invocation updated_at precedes finish")
            if status is InvocationStatus.RUNNING:
                if heartbeat_at is None or lease_expires_at is None:
                    raise ValueError("persisted running invocation lacks lease timestamps")
                if heartbeat_at < created_at:
                    raise ValueError("persisted invocation heartbeat precedes creation")
                if updated_at < heartbeat_at:
                    raise ValueError("persisted invocation update precedes heartbeat")
                if lease_expires_at <= heartbeat_at or lease_expires_at <= updated_at:
                    raise ValueError("persisted invocation lease is not later than its activity")
            result_ref = _persisted_optional_text(
                row["result_ref"],
                "invocation result_ref",
                maximum_bytes=_MAX_REFERENCE_BYTES,
            )
            if result_ref is not None and status is not InvocationStatus.SUCCEEDED:
                raise ValueError("persisted non-succeeded invocation carries a result_ref")
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
                requested_available_at=requested_available_at,
                available_at=available_at,
                created_at=created_at,
                updated_at=updated_at,
                lease_owner=lease_owner,
                lease_token_digest=lease_digest,
                lease_expires_at=lease_expires_at,
                heartbeat_at=heartbeat_at,
                result_ref=result_ref,
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
            error = _persisted_optional_text(
                row["error"],
                "attempt error",
                maximum_bytes=_MAX_ERROR_BYTES,
            )
            if error is not None and len(error) > _MAX_ERROR_LENGTH:
                raise ValueError("persisted attempt error exceeds its supported length")
            if status in {AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED} and error is not None:
                raise ValueError("persisted active/succeeded attempt carries an error")
            if status in {AttemptStatus.FAILED, AttemptStatus.EXPIRED} and error is None:
                raise ValueError("persisted failed/expired attempt lacks an error")
            started_at = _persisted_timestamp(row["started_at"], "attempt started_at")
            heartbeat_at = _persisted_timestamp(row["heartbeat_at"], "attempt heartbeat_at")
            lease_expires_at = _persisted_timestamp(
                row["lease_expires_at"], "attempt lease_expires_at"
            )
            if heartbeat_at < started_at:
                raise ValueError("persisted attempt timestamps violate start causality")
            if lease_expires_at <= heartbeat_at:
                raise ValueError("persisted attempt lease does not follow its heartbeat")
            if finished_at is not None and finished_at < started_at:
                raise ValueError("persisted attempt finished_at precedes its start")
            if finished_at is not None and finished_at < heartbeat_at:
                raise ValueError("persisted attempt finished_at precedes its heartbeat")
            if (
                status in {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED}
                and finished_at is not None
                and finished_at >= lease_expires_at
            ):
                raise ValueError("persisted owned attempt finished outside its lease")
            if (
                status is AttemptStatus.EXPIRED
                and finished_at is not None
                and finished_at < lease_expires_at
            ):
                raise ValueError("persisted expired attempt finished before lease expiry")
            result_ref = _persisted_optional_text(
                row["result_ref"],
                "attempt result_ref",
                maximum_bytes=_MAX_REFERENCE_BYTES,
            )
            if result_ref is not None and status is not AttemptStatus.SUCCEEDED:
                raise ValueError("persisted non-succeeded attempt carries a result_ref")
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
                started_at=started_at,
                heartbeat_at=heartbeat_at,
                lease_expires_at=lease_expires_at,
                finished_at=finished_at,
                error=error,
                result_ref=result_ref,
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

    def _reconcile_enqueued_job(self, spec: InvocationJobSpec) -> Optional[InvocationJob]:
        with self._read_transaction() as connection:
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
            if len(rows) != 1 or not self._existing_matches(rows[0], spec):
                return None
            return self._row_to_job(rows[0])

    @_sanitize_control_signals
    def enqueue(self, spec: InvocationJobSpec) -> InvocationJob:
        """Persist one invocation, returning the original row for an identical retry."""

        try:
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
                            "invocation identity or idempotency key is already bound "
                            "to different work"
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
        except _CommitOutcomeUnknown as error:
            if not self._trusted_commit_error(error) or not error.may_reconcile:
                self._unreconciled_commit(error)
            readback_control: Optional[_ControlSignalDescriptor] = None
            try:
                reconciled = self._reconcile_enqueued_job(spec)
            except BaseException as readback_error:
                readback_control = _control_signal_descriptor(readback_error)
                reconciled = None
            if reconciled is None:
                self._unreconciled_commit(error, control_signal=readback_control)
            return self._after_reconciled_commit(error, reconciled)

    @_bind_store_process
    def get(self, invocation_id: str) -> Optional[InvocationJob]:
        _required(invocation_id, "invocation_id")
        with self._lock:
            self._require_usable()
            row = self._connection.execute(
                "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    @_bind_store_process
    def get_for_task(self, session_id: str, task_id: str) -> Optional[InvocationJob]:
        _required(session_id, "session_id")
        _required(task_id, "task_id")
        with self._lock:
            self._require_usable()
            row = self._connection.execute(
                """
                SELECT * FROM invocation_jobs
                WHERE session_id = ? AND task_id = ?
                """,
                (session_id, task_id),
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    @staticmethod
    def _validate_recovery_snapshot(
        job: InvocationJob,
        current_attempt: Optional[InvocationAttempt],
        *,
        attempt_count: int,
    ) -> None:
        if attempt_count != job.attempts_started:
            raise InvocationIntegrityError(
                "invocation attempt count does not match attempts_started"
            )
        if attempt_count == 0:
            if job.lease_epoch != 0:
                raise InvocationIntegrityError("zero-attempt invocation has a nonzero lease epoch")
            if current_attempt is not None:
                raise InvocationIntegrityError("zero-attempt invocation has attempt history")
            return
        if current_attempt is None:
            raise InvocationIntegrityError("invocation current attempt is missing")
        if (
            current_attempt.invocation_id != job.invocation_id
            or current_attempt.attempt_number != job.attempts_started
            or current_attempt.lease_epoch != job.lease_epoch
        ):
            raise InvocationIntegrityError("invocation current attempt identity is inconsistent")
        if current_attempt.started_at < job.created_at:
            raise InvocationIntegrityError("invocation attempt starts before its job")
        if current_attempt.finished_at is not None and current_attempt.finished_at > job.updated_at:
            raise InvocationIntegrityError("invocation job update precedes its current attempt")
        if job.finished_at is not None and job.finished_at != current_attempt.finished_at:
            raise InvocationIntegrityError("invocation finish differs from its current attempt")

        if job.status is InvocationStatus.RUNNING:
            if (
                current_attempt.status is not AttemptStatus.RUNNING
                or current_attempt.worker_id != job.lease_owner
                or current_attempt.lease_token_digest != job.lease_token_digest
                or current_attempt.heartbeat_at != job.heartbeat_at
                or current_attempt.lease_expires_at != job.lease_expires_at
            ):
                raise InvocationIntegrityError(
                    "running invocation ownership differs from its current attempt"
                )
        elif current_attempt.status is AttemptStatus.RUNNING:
            raise InvocationIntegrityError("non-running invocation has a running current attempt")
        elif job.status is InvocationStatus.SUCCEEDED:
            if (
                current_attempt.status is not AttemptStatus.SUCCEEDED
                or current_attempt.result_ref != job.result_ref
            ):
                raise InvocationIntegrityError(
                    "succeeded invocation differs from its current attempt"
                )
        elif job.status is InvocationStatus.FAILED:
            if current_attempt.status not in {AttemptStatus.FAILED, AttemptStatus.EXPIRED}:
                raise InvocationIntegrityError("failed invocation has an incompatible attempt")
            if current_attempt.error != job.last_error:
                raise InvocationIntegrityError("failed invocation error differs from its attempt")
        elif job.status is InvocationStatus.QUEUED:
            if current_attempt.status not in {AttemptStatus.FAILED, AttemptStatus.EXPIRED}:
                raise InvocationIntegrityError(
                    "queued invocation has an incompatible prior attempt"
                )
            if current_attempt.error != job.last_error:
                raise InvocationIntegrityError("queued invocation error differs from its attempt")
        elif job.status is InvocationStatus.CANCELED:
            if current_attempt.status is not AttemptStatus.CANCELED:
                raise InvocationIntegrityError("canceled invocation has an incompatible attempt")

    @_sanitize_control_signals
    def recovery_snapshot_for_task(
        self,
        session_id: str,
        task_id: str,
    ) -> InvocationRecoverySnapshot:
        """Read and validate one bounded attempt history in a single SQLite snapshot."""

        _required(session_id, "session_id")
        _required(task_id, "task_id")
        with self._read_transaction() as connection:
            job_rows = connection.execute(
                """
                SELECT * FROM invocation_jobs
                WHERE session_id = ? AND task_id = ?
                LIMIT 2
                """,
                (session_id, task_id),
            ).fetchall()
            if len(job_rows) > 1:
                raise InvocationIntegrityError("task has multiple invocation jobs")
            if not job_rows:
                return InvocationRecoverySnapshot(None, None, 0)
            job = self._row_to_job(job_rows[0])
            cursor = connection.execute(
                """
                SELECT * FROM invocation_attempts
                WHERE invocation_id = ?
                ORDER BY attempt_number
                LIMIT 1001
                """,
                (job.invocation_id,),
            )
            attempt_count = 0
            previous_lease_epoch = 0
            previous_finished_at: Optional[str] = None
            current_attempt: Optional[InvocationAttempt] = None
            try:
                for row in cursor:
                    attempt_count += 1
                    if attempt_count > 1_000:
                        raise InvocationIntegrityError(
                            "invocation attempt history exceeds the recovery limit"
                        )
                    attempt = self._row_to_attempt(row)
                    if attempt.invocation_id != job.invocation_id:
                        raise InvocationIntegrityError(
                            "invocation attempt crosses its job boundary"
                        )
                    if attempt.started_at < job.created_at:
                        raise InvocationIntegrityError("invocation attempt starts before its job")
                    if attempt.attempt_number != attempt_count:
                        raise InvocationIntegrityError(
                            "invocation attempt history is not contiguous"
                        )
                    if attempt.lease_epoch <= previous_lease_epoch:
                        raise InvocationIntegrityError(
                            "invocation attempt lease epochs are not strictly increasing"
                        )
                    previous_lease_epoch = attempt.lease_epoch
                    if (
                        previous_finished_at is not None
                        and attempt.started_at < previous_finished_at
                    ):
                        raise InvocationIntegrityError(
                            "invocation attempt history moves backward in time"
                        )
                    previous_finished_at = attempt.finished_at
                    if attempt.attempt_number < job.attempts_started:
                        if attempt.status not in {AttemptStatus.FAILED, AttemptStatus.EXPIRED}:
                            raise InvocationIntegrityError(
                                "historical invocation attempt is not safely terminal"
                            )
                    elif attempt.attempt_number == job.attempts_started:
                        current_attempt = attempt
                    else:
                        raise InvocationIntegrityError(
                            "invocation attempt history exceeds attempts_started"
                        )
            finally:
                cursor.close()
            self._validate_recovery_snapshot(
                job,
                current_attempt,
                attempt_count=attempt_count,
            )
            return InvocationRecoverySnapshot(job, current_attempt, attempt_count)

    @_bind_store_process
    def attempts(self, invocation_id: str) -> Tuple[InvocationAttempt, ...]:
        _required(invocation_id, "invocation_id")
        with self._lock:
            self._require_usable()
            rows = self._connection.execute(
                """
                SELECT * FROM invocation_attempts
                WHERE invocation_id = ? ORDER BY attempt_number
                """,
                (invocation_id,),
            ).fetchall()
            return tuple(self._row_to_attempt(row) for row in rows)

    @_bind_store_process
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
            self._require_usable()
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
        heartbeat_at: str,
        lease_expires_at: str,
        attempt_id: Optional[str] = None,
    ) -> None:
        if (
            attempt.invocation_id != invocation_id
            or attempt.attempt_number != attempt_number
            or attempt.lease_epoch != lease_epoch
            or attempt.worker_id != worker_id
            or attempt.lease_token_digest != lease_token_digest
            or attempt.heartbeat_at != heartbeat_at
            or attempt.lease_expires_at != lease_expires_at
            or (attempt_id is not None and attempt.attempt_id != attempt_id)
        ):
            raise InvocationIntegrityError(
                "running invocation and attempt ownership records disagree"
            )

    @staticmethod
    def _require_lease(lease: InvocationLease) -> None:
        if type(lease) is not InvocationLease:
            raise TypeError("lease must be an InvocationLease")
        for field_name in (
            "invocation_id",
            "session_id",
            "plan_id",
            "task_id",
            "agent_id",
            "idempotency_key",
            "attempt_id",
            "worker_id",
        ):
            _required(getattr(lease, field_name), f"lease {field_name}")
        if type(lease.payload_digest) is not str or not _SHA256_PATTERN.fullmatch(
            lease.payload_digest
        ):
            raise ValueError("lease payload_digest must be canonical SHA-256")
        for field_name in ("attempt_number", "max_attempts", "lease_epoch"):
            value = getattr(lease, field_name)
            if type(value) is not int or not 1 <= value <= _MAX_SQLITE_INTEGER:
                raise ValueError(f"lease {field_name} is outside its supported range")
        _required(lease.lease_token, "lease_token")
        claimed_at = _normalize_timestamp(lease.claimed_at, "lease claimed_at")
        expires_at = _normalize_timestamp(lease.lease_expires_at, "lease lease_expires_at")
        if claimed_at != lease.claimed_at or expires_at != lease.lease_expires_at:
            raise ValueError("lease timestamps must be canonical UTC")
        if expires_at <= claimed_at:
            raise ValueError("lease expiry must follow its claim")

    @staticmethod
    def _validate_lease_binding(
        lease: InvocationLease,
        job: InvocationJob,
        attempt: InvocationAttempt,
    ) -> None:
        if (
            lease.invocation_id != job.invocation_id
            or lease.session_id != job.session_id
            or lease.plan_id != job.plan_id
            or lease.task_id != job.task_id
            or lease.agent_id != job.agent_id
            or lease.idempotency_key != job.idempotency_key
            or lease.payload_digest != job.payload_digest
            or lease.attempt_id != attempt.attempt_id
            or lease.attempt_number != job.attempts_started
            or lease.attempt_number != attempt.attempt_number
            or lease.max_attempts != job.max_attempts
            or lease.lease_epoch != job.lease_epoch
            or lease.lease_epoch != attempt.lease_epoch
            or lease.worker_id != attempt.worker_id
            or lease.claimed_at != attempt.started_at
        ):
            raise InvocationIntegrityError(
                "invocation lease binding differs from durable ownership"
            )

    def _lease_snapshot(
        self,
        lease: InvocationLease,
    ) -> Optional[Tuple[InvocationJob, InvocationAttempt]]:
        snapshot = self.recovery_snapshot_for_task(lease.session_id, lease.task_id)
        if snapshot.job is None or snapshot.current_attempt is None:
            return None
        try:
            self._validate_lease_binding(lease, snapshot.job, snapshot.current_attempt)
        except InvocationIntegrityError:
            return None
        if snapshot.current_attempt.lease_token_digest != _lease_token_digest(lease.lease_token):
            return None
        return snapshot.job, snapshot.current_attempt

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
            if job.lease_owner is None or job.heartbeat_at is None or job.lease_expires_at is None:
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
                heartbeat_at=job.heartbeat_at,
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

    def _recovered_commit_matches(self, summary: RecoverySummary, recovered_at: str) -> bool:
        reason = "lease expired before terminal acknowledgement"
        for invocation_id in summary.requeued + summary.exhausted:
            job = self.get(invocation_id)
            attempts = self.attempts(invocation_id)
            if job is None or not attempts:
                return False
            attempt = attempts[-1]
            if (
                attempt.attempt_number != job.attempts_started
                or attempt.lease_epoch != job.lease_epoch
                or attempt.status is not AttemptStatus.EXPIRED
                or attempt.finished_at != recovered_at
                or attempt.error != reason
                or job.updated_at != recovered_at
                or job.last_error != reason
            ):
                return False
            if invocation_id in summary.requeued:
                if (
                    job.status is not InvocationStatus.QUEUED
                    or job.available_at != recovered_at
                    or job.finished_at is not None
                ):
                    return False
            elif job.status is not InvocationStatus.FAILED or job.finished_at != recovered_at:
                return False
        return True

    @_sanitize_control_signals
    def recover_expired(
        self,
        *,
        limit: int = 1_000,
    ) -> RecoverySummary:
        """Fence expired owners into an effect-unknown queued or terminal state."""

        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        normalized_now: Optional[str] = None
        summary: Optional[RecoverySummary] = None
        try:
            with self._transaction() as connection:
                normalized_now = self._now()
                summary = self._recover_expired_in_transaction(
                    connection,
                    normalized_now,
                    limit=limit,
                )
                return summary
        except _CommitOutcomeUnknown as error:
            if not self._trusted_commit_error(error) or not error.may_reconcile:
                self._unreconciled_commit(error)
            if summary is None or normalized_now is None:
                self._unreconciled_commit(error)
            if summary.recovered_count == 0:
                return self._after_reconciled_commit(error, summary)
            readback_control: Optional[_ControlSignalDescriptor] = None
            try:
                matches = self._recovered_commit_matches(summary, normalized_now)
            except BaseException as readback_error:
                readback_control = _control_signal_descriptor(readback_error)
                matches = False
            if not matches:
                self._unreconciled_commit(error, control_signal=readback_control)
            return self._after_reconciled_commit(error, summary)

    def _claim_in_transaction(
        self,
        worker_id: str,
        *,
        invocation_id: Optional[str],
        lease_seconds: float,
        commit_candidate: list[Optional[InvocationLease]],
        commit_recovery: list[Optional[Tuple[RecoverySummary, str]]],
    ) -> Optional[InvocationLease]:
        with self._transaction() as connection:
            normalized_now = self._now()
            deadline = _lease_deadline(normalized_now, lease_seconds)
            recovery = self._recover_expired_in_transaction(
                connection,
                normalized_now,
                invocation_id=invocation_id,
                limit=(None if invocation_id is not None else 1_000),
            )
            if recovery.recovered_count > 0:
                commit_recovery[0] = (recovery, normalized_now)
            parameters = [normalized_now]
            candidate_where = "status = 'queued' AND attempts_started = 0 AND available_at <= ?"
            if invocation_id is not None:
                candidate_where += " AND invocation_id = ?"
                parameters.append(invocation_id)
            candidate_row = connection.execute(
                f"""
                SELECT * FROM invocation_jobs WHERE {candidate_where}
                ORDER BY priority DESC, available_at, created_at, invocation_id
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if candidate_row is None:
                return None
            candidate = self._row_to_job(candidate_row)
            prior_attempt = connection.execute(
                """
                SELECT 1 FROM invocation_attempts
                WHERE invocation_id = ? LIMIT 1
                """,
                (candidate.invocation_id,),
            ).fetchone()
            if prior_attempt is not None:
                raise InvocationIntegrityError("first-claim candidate has attempt history")
            claimable_where = (
                candidate_where
                + " AND lease_epoch = 0 AND result_ref IS NULL AND last_error IS NULL"
            )
            row = connection.execute(
                f"""
                SELECT * FROM invocation_jobs WHERE {claimable_where}
                ORDER BY priority DESC, available_at, created_at, invocation_id
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                raise InvocationIntegrityError("first-claim candidate changed after validation")
            job = self._row_to_job(row)
            if job != candidate:
                raise InvocationIntegrityError("first-claim candidate changed after validation")
            _require_non_regressing_clock(normalized_now, job.created_at, job.updated_at)
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
                  AND attempts_started = 0 AND lease_epoch = 0
                  AND result_ref IS NULL AND last_error IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM invocation_attempts
                      WHERE invocation_attempts.invocation_id = invocation_jobs.invocation_id
                  )
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
                ),
            )
            if update.rowcount != 1:
                raise InvocationIntegrityError("first-claim CAS rejected contradictory state")
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
            lease = InvocationLease(
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
            commit_candidate[0] = lease
            return lease

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
        commit_candidate: list[Optional[InvocationLease]] = [None]
        commit_recovery: list[Optional[Tuple[RecoverySummary, str]]] = [None]
        try:
            return self._claim_in_transaction(
                worker_id,
                invocation_id=invocation_id,
                lease_seconds=lease_seconds,
                commit_candidate=commit_candidate,
                commit_recovery=commit_recovery,
            )
        except _CommitOutcomeUnknown as error:
            if not self._trusted_commit_error(error) or not error.may_reconcile:
                self._unreconciled_commit(error)
            lease = commit_candidate[0]
            if lease is None:
                recovered = commit_recovery[0]
                if recovered is None:
                    return self._after_reconciled_commit(error, None)
                summary, recovered_at = recovered
                readback_control: Optional[_ControlSignalDescriptor] = None
                try:
                    matches = self._recovered_commit_matches(summary, recovered_at)
                except BaseException as readback_error:
                    readback_control = _control_signal_descriptor(readback_error)
                    matches = False
                if not matches:
                    self._unreconciled_commit(error, control_signal=readback_control)
                return self._after_reconciled_commit(error, None)
            readback_control = None
            try:
                reconciled = self._lease_snapshot(lease)
            except BaseException as readback_error:
                readback_control = _control_signal_descriptor(readback_error)
                reconciled = None
            if reconciled is None:
                self._unreconciled_commit(error, control_signal=readback_control)
            job, attempt = reconciled
            if (
                job.status is not InvocationStatus.RUNNING
                or attempt.status is not AttemptStatus.RUNNING
                or job.lease_owner != lease.worker_id
                or job.heartbeat_at != lease.claimed_at
                or attempt.heartbeat_at != lease.claimed_at
                or job.updated_at != lease.claimed_at
                or job.lease_expires_at != lease.lease_expires_at
                or attempt.lease_expires_at != lease.lease_expires_at
            ):
                self._unreconciled_commit(error)
            return self._after_reconciled_commit(error, lease)

    @_sanitize_control_signals
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

    @_sanitize_control_signals
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

    def _heartbeat_in_transaction(
        self,
        lease: InvocationLease,
        *,
        lease_seconds: float,
        commit_state: list[Optional[Tuple[str, str]]],
    ) -> bool:
        with self._transaction() as connection:
            normalized_now = self._now()
            proposed_deadline = _lease_deadline(normalized_now, lease_seconds)
            job = self._active_owned_row(connection, lease, normalized_now)
            if job is None:
                return False
            if job.heartbeat_at is None or job.lease_expires_at is None:
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
                heartbeat_at=job.heartbeat_at,
                lease_expires_at=job.lease_expires_at,
                attempt_id=lease.attempt_id,
            )
            self._validate_lease_binding(lease, job, attempt)
            _require_non_regressing_clock(
                normalized_now,
                job.updated_at,
                attempt.started_at,
                attempt.heartbeat_at,
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
            commit_state[0] = (normalized_now, deadline)
            return True

    @_sanitize_control_signals
    def heartbeat(
        self,
        lease: InvocationLease,
        *,
        lease_seconds: float,
    ) -> bool:
        """Extend an active lease; returns false once ownership is stale or expired."""

        self._require_lease(lease)
        commit_state: list[Optional[Tuple[str, str]]] = [None]
        try:
            return self._heartbeat_in_transaction(
                lease,
                lease_seconds=lease_seconds,
                commit_state=commit_state,
            )
        except _CommitOutcomeUnknown as error:
            if not self._trusted_commit_error(error) or not error.may_reconcile:
                self._unreconciled_commit(error)
            expected = commit_state[0]
            if expected is None:
                return self._after_reconciled_commit(error, False)
            readback_control: Optional[_ControlSignalDescriptor] = None
            try:
                reconciled = self._lease_snapshot(lease)
            except BaseException as readback_error:
                readback_control = _control_signal_descriptor(readback_error)
                reconciled = None
            if reconciled is None:
                self._unreconciled_commit(error, control_signal=readback_control)
            job, attempt = reconciled
            heartbeat_at, deadline = expected
            if (
                job.status is not InvocationStatus.RUNNING
                or attempt.status is not AttemptStatus.RUNNING
                or job.heartbeat_at != heartbeat_at
                or attempt.heartbeat_at != heartbeat_at
                or job.updated_at != heartbeat_at
                or job.lease_expires_at != deadline
                or attempt.lease_expires_at != deadline
            ):
                self._unreconciled_commit(error)
            return self._after_reconciled_commit(error, True)

    def _complete_in_transaction(
        self,
        lease: InvocationLease,
        *,
        result_ref: Optional[str],
        commit_state: list[Optional[str]],
    ) -> bool:
        with self._transaction() as connection:
            normalized_now = self._now()
            job = self._active_owned_row(connection, lease, normalized_now)
            if job is None:
                return False
            if job.heartbeat_at is None or job.lease_expires_at is None:
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
                heartbeat_at=job.heartbeat_at,
                lease_expires_at=job.lease_expires_at,
                attempt_id=lease.attempt_id,
            )
            self._validate_lease_binding(lease, job, attempt)
            _require_non_regressing_clock(
                normalized_now,
                job.updated_at,
                attempt.started_at,
                attempt.heartbeat_at,
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
            commit_state[0] = normalized_now
            return True

    @_sanitize_control_signals
    def complete(
        self,
        lease: InvocationLease,
        *,
        result_ref: Optional[str] = None,
    ) -> bool:
        """CAS an active lease to success, rejecting stale or expired workers."""

        self._require_lease(lease)
        if result_ref is not None:
            _required(result_ref, "result_ref", maximum_bytes=_MAX_REFERENCE_BYTES)
        commit_state: list[Optional[str]] = [None]
        try:
            return self._complete_in_transaction(
                lease,
                result_ref=result_ref,
                commit_state=commit_state,
            )
        except _CommitOutcomeUnknown as error:
            if not self._trusted_commit_error(error) or not error.may_reconcile:
                self._unreconciled_commit(error)
            finished_at = commit_state[0]
            if finished_at is None:
                return self._after_reconciled_commit(error, False)
            readback_control: Optional[_ControlSignalDescriptor] = None
            try:
                reconciled = self._lease_snapshot(lease)
            except BaseException as readback_error:
                readback_control = _control_signal_descriptor(readback_error)
                reconciled = None
            if reconciled is None:
                self._unreconciled_commit(error, control_signal=readback_control)
            job, attempt = reconciled
            if (
                job.status is not InvocationStatus.SUCCEEDED
                or attempt.status is not AttemptStatus.SUCCEEDED
                or job.result_ref != result_ref
                or attempt.result_ref != result_ref
                or job.updated_at != finished_at
                or job.finished_at != finished_at
                or attempt.finished_at != finished_at
            ):
                self._unreconciled_commit(error)
            return self._after_reconciled_commit(error, True)

    def _fail_in_transaction(
        self,
        lease: InvocationLease,
        *,
        stored_error: str,
        retry_at: Optional[str],
        commit_state: list[Optional[Tuple[str, str, InvocationStatus]]],
    ) -> bool:
        with self._transaction() as connection:
            normalized_now = self._now()
            normalized_retry = _normalize_timestamp(retry_at or normalized_now, "retry_at")
            normalized_retry = max(normalized_now, normalized_retry)
            job = self._active_owned_row(connection, lease, normalized_now)
            if job is None:
                return False
            if job.heartbeat_at is None or job.lease_expires_at is None:
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
                heartbeat_at=job.heartbeat_at,
                lease_expires_at=job.lease_expires_at,
                attempt_id=lease.attempt_id,
            )
            self._validate_lease_binding(lease, job, attempt)
            _require_non_regressing_clock(
                normalized_now,
                job.updated_at,
                attempt.started_at,
                attempt.heartbeat_at,
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
                target_status = InvocationStatus.FAILED
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
                target_status = InvocationStatus.QUEUED
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
            commit_state[0] = (normalized_now, normalized_retry, target_status)
            return True

    @_sanitize_control_signals
    def fail(
        self,
        lease: InvocationLease,
        error: str,
        *,
        retry_at: Optional[str] = None,
    ) -> bool:
        """CAS an active lease to effect-unknown queued or terminal failure."""

        self._require_lease(lease)
        stored_error = _stored_error(error)
        commit_state: list[Optional[Tuple[str, str, InvocationStatus]]] = [None]
        try:
            return self._fail_in_transaction(
                lease,
                stored_error=stored_error,
                retry_at=retry_at,
                commit_state=commit_state,
            )
        except _CommitOutcomeUnknown as commit_error:
            if not self._trusted_commit_error(commit_error) or not commit_error.may_reconcile:
                self._unreconciled_commit(commit_error)
            expected = commit_state[0]
            if expected is None:
                return self._after_reconciled_commit(commit_error, False)
            readback_control: Optional[_ControlSignalDescriptor] = None
            try:
                reconciled = self._lease_snapshot(lease)
            except BaseException as readback_error:
                readback_control = _control_signal_descriptor(readback_error)
                reconciled = None
            if reconciled is None:
                self._unreconciled_commit(
                    commit_error,
                    control_signal=readback_control,
                )
            job, attempt = reconciled
            finished_at, normalized_retry, target_status = expected
            if (
                job.status is not target_status
                or attempt.status is not AttemptStatus.FAILED
                or job.last_error != stored_error
                or attempt.error != stored_error
                or job.updated_at != finished_at
                or attempt.finished_at != finished_at
                or (
                    target_status is InvocationStatus.QUEUED
                    and (job.available_at != normalized_retry or job.finished_at is not None)
                )
                or (target_status is InvocationStatus.FAILED and job.finished_at != finished_at)
            ):
                self._unreconciled_commit(commit_error)
            return self._after_reconciled_commit(commit_error, True)

    @_bind_store_process
    def schema_version(self) -> int:
        with self._lock:
            self._require_usable()
            return int(current_schema_version(self._connection))

    @_sanitize_close_signals
    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._connection_closed:
                return
            acknowledged, control_signal = self._close_connection_outcome(self._connection)
            if control_signal is not None:
                self._raise_invocation_close_control_signal(control_signal)
            if not acknowledged:
                self._close_failed()
            self._connection_closed = True


_TRUSTED_TRANSACTION_BODY_CODES = _collect_trusted_module_code_objects()


__all__ = [
    "AttemptStatus",
    "InvocationAttempt",
    "InvocationClockRegressionError",
    "InvocationCommitAmbiguityError",
    "InvocationConflictError",
    "InvocationIntegrityError",
    "InvocationJob",
    "InvocationJobSpec",
    "InvocationLease",
    "InvocationRecoverySnapshot",
    "InvocationStatus",
    "InvocationStoreClosedError",
    "InvocationStoreProcessMismatchError",
    "InvocationStorePoisonedError",
    "InvocationTransactionError",
    "MigrationDriftError",
    "RecoverySummary",
    "SQLiteInvocationAttemptStore",
    "invocation_payload_digest",
]
