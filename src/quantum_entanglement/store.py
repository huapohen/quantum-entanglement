# ruff: noqa: UP006, UP031, UP035, UP037, UP045
"""SQLite append-only event store with optimistic concurrency and idempotency."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import traceback as traceback_module
import unicodedata
from asyncio import CancelledError
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
    Union,
    cast,
)

from . import process_identity as _process_identity
from ._result_acceptance import (
    _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
    _build_scoped_invocation_result_events_from_plan_v2,
    _build_scoped_invocation_result_evidence_v2,
    _build_scoped_invocation_result_terminal_transition_from_plan_v2,
    _EventedFreshResultAcceptancePlanV2,
    _EvidencedFreshResultAcceptancePlanV2,
    _ExistingResultAcceptanceGraphCandidateV2,
    _FreshResultAcceptancePrerequisitesV2,
    _FreshResultAcceptanceWritePlanV2,
    _IdentifiedFreshResultAcceptancePlanV2,
    _MaterializedFreshResultAcceptancePlanV2,
    _PreparedScopedInvocationResultAcceptanceV2,
    _ResultAcceptanceConflictError,
    _ResultAcceptanceIntegrityError,
    _ResultAcceptanceQuarantineCategory,
    _ResultAcceptanceQuarantineError,
    _ResultAcceptanceSchemaUnavailableError,
    _TransitionedFreshResultAcceptancePlanV2,
)
from ._result_artifact_transaction import (
    _RESULT_ARTIFACT_TRANSACTION_TOKEN,
    _materialize_prepared_result_artifacts_in_transaction,
    _preflight_prepared_result_artifacts_in_transaction,
    _prepare_result_artifact_batch,
    _PreparedResultArtifactBatch,
    _ResultArtifactCommitAmbiguityError,
    _ResultArtifactConcurrencyError,
    _ResultArtifactConflictError,
    _ResultArtifactIntegrityError,
    _ResultArtifactTransactionContinuityError,
    _ResultArtifactTransactionError,
    _ResultArtifactTransactionHandle,
    _validated_result_artifact_savepoint_suffix,
    _write_prepared_result_artifacts_in_transaction,
)
from ._stored_event_envelope_codec import (
    StoredEventEnvelopeError as _StoredEventEnvelopeError,
)
from ._stored_event_envelope_codec import (
    _stored_event_envelope_from_raw_row,
    _stored_event_envelope_from_values,
    _StoredEventEnvelopeV1,
)
from .attempts import (
    AttemptStatus,
    InvocationAttempt,
    InvocationIntegrityError,
    InvocationJob,
    InvocationJobSpec,
    InvocationStatus,
    SQLiteInvocationAttemptStore,
    _claim_first_invocation_in_transaction,
    _enqueue_invocation_job_in_transaction,
    _InvocationClaimRequest,
    _select_first_claim_candidate_in_transaction,
)
from .attempts import (
    _lease_deadline as _invocation_lease_deadline,
)
from .attempts import (
    _normalize_timestamp as _normalize_invocation_timestamp,
)
from .delivery import (
    InboxAppendResult,
    InboxReceipt,
    OutboxMessage,
    OutboxStatus,
    StoredOutboxMessage,
)
from .events import DomainEvent, StoredEvent
from .invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    TASK_INVOCATION_STARTED_EVENT_TYPE,
    InvocationStartClaimed,
    InvocationStartEvidenceV2,
    InvocationStartObserved,
    InvocationStartReceipt,
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartEvidenceV3,
    ScopedInvocationStartObservedV3,
    ScopedInvocationStartReceiptV3,
    ScopedTaskInvocationAdmissionRequestV2,
    TaskInvocationAdmissionRequest,
)
from .invocation_execution import EffectClass as _EffectClass
from .invocation_execution import RetryClass as _RetryClass
from .invocation_results import (
    TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE as _TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
)
from .invocation_results import (
    TASK_STATUS_CHANGED_EVENT_TYPE as _TASK_STATUS_CHANGED_EVENT_TYPE,
)
from .invocation_results import (
    ScopedInvocationResultAcceptanceRequestV2 as _ScopedInvocationResultAcceptanceRequestV2,
)
from .invocation_results import (
    ScopedInvocationResultArtifactCandidateV2 as _ScopedInvocationResultArtifactCandidateV2,
)
from .invocation_results import (
    ScopedInvocationResultArtifactV2 as _ScopedInvocationResultArtifactV2,
)
from .invocation_results import (
    ScopedInvocationResultEventCoordinatesV2 as _ScopedInvocationResultEventCoordinatesV2,
)
from .invocation_results import (
    ScopedInvocationResultEvidenceV2 as _ScopedInvocationResultEvidenceV2,
)
from .invocation_results import (
    ScopedInvocationResultManifestV2 as _ScopedInvocationResultManifestV2,
)
from .invocation_results import (
    ScopedInvocationResultObservedV2 as _ScopedInvocationResultObservedV2,
)
from .invocation_results import (
    ScopedInvocationResultReceiptV2 as _ScopedInvocationResultReceiptV2,
)
from .invocation_results import (
    ScopedInvocationResultTerminalTransitionV2 as _ScopedInvocationResultTerminalTransitionV2,
)
from .invocation_results import (
    build_scoped_invocation_result_receipt_v2 as _build_scoped_invocation_result_receipt_v2,
)
from .invocation_results import (
    scoped_invocation_start_receipt_digest_v3 as _scoped_invocation_start_receipt_digest_v3,
)
from .migrations import _apply_sqlite_migrations_unregistered, apply_sqlite_migrations
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


class ReservedResultEventError(ValueError):
    """Raised when a generic append tries to write result-authority vocabulary."""

    code = "reserved_result_event"

    def __init__(self) -> None:
        super().__init__("generic event append cannot write reserved result authority")


class _ResultEventWriteContractError(ValueError):
    """Raised when the private event adapter receives non-canonical result vocabulary."""

    code = "result_event_write_contract_invalid"

    def __init__(self) -> None:
        super().__init__("private result event append requires an exact typed payload")


class InvocationAdmissionConflictError(RuntimeError):
    """Raised when an atomic event/job admission boundary is reused differently."""


class InvocationAdmissionCommitAmbiguityError(RuntimeError):
    """Raised when admission may be durable but its COMMIT was not acknowledged."""

    code = "invocation_admission_commit_ambiguous"

    def __init__(self) -> None:
        super().__init__(
            "invocation admission commit outcome is unknown; reopen the store and "
            "reconcile the exact request"
        )


class InvocationAdmissionTransactionError(RuntimeError):
    """Raised when admission COMMIT failed and rollback was confirmed."""

    code = "invocation_admission_transaction_failed"

    def __init__(self) -> None:
        super().__init__("invocation admission transaction was rolled back")


class InvocationStartConflictError(RuntimeError):
    """Raised when invocation start evidence is missing, partial, or contradictory."""

    code = "invocation_start_conflict"

    def __init__(self) -> None:
        super().__init__("invocation start is not bound to one canonical durable state")


class InvocationStartCommitAmbiguityError(RuntimeError):
    """Raised when start may be durable but its COMMIT was not acknowledged."""

    code = "invocation_start_commit_ambiguous"

    def __init__(self) -> None:
        super().__init__(
            "invocation start commit outcome is unknown; reopen the store and "
            "observe the durable start receipt"
        )


class InvocationStartTransactionError(RuntimeError):
    """Raised when an invocation-start transaction was confirmed rolled back."""

    code = "invocation_start_transaction_failed"

    def __init__(self) -> None:
        super().__init__("invocation start transaction was rolled back")


class ResultAcceptanceDisabledError(RuntimeError):
    """Raised when the private result-schema opt-in is not enabled for this store."""

    code = "result_acceptance_disabled"

    def __init__(self) -> None:
        super().__init__(
            "result acceptance observation is disabled until the result schema is explicitly "
            "enabled"
        )


class EventStorePoisonedError(EventStoreIntegrityError):
    """Raised after an ambiguous transaction quarantines this store instance."""

    code = "event_store_poisoned"

    def __init__(self) -> None:
        super().__init__("event store is poisoned and must be reopened")


_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 10_000
_MAX_JSON_KEY_LENGTH = 512
_MAX_JSON_STRING_LENGTH = 65_536
_MAX_JSON_INTEGER_BITS = 4_096
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAPPING_PROXY_TYPE: type[Any] = type(MappingProxyType({}))
_EVENT_STORE_PROCESS_SIGNAL_TOKEN = object()
_EVENT_STORE_ADMISSION_CONTROL_TOKEN = object()
_EVENT_STORE_START_CONTROL_TOKEN = object()
_BASE_EXCEPTION_CAUSE_DESCRIPTOR: Any = BaseException.__dict__["__cause__"]
_BASE_EXCEPTION_CONTEXT_DESCRIPTOR: Any = BaseException.__dict__["__context__"]
_BASE_EXCEPTION_SUPPRESS_CONTEXT_DESCRIPTOR: Any = BaseException.__dict__["__suppress_context__"]
_BASE_EXCEPTION_TRACEBACK_DESCRIPTOR: Any = BaseException.__dict__["__traceback__"]
_EVENT_STORE_CHILD_GRAPH_QUARANTINE: List[object] = []
_INVOCATION_ADMISSION_RECEIPT_FORMAT = "qe.invocation-admission-receipt/1"
_RESULT_ACCEPTANCE_TABLE_NAMES = (
    "invocation_result_artifacts",
    "invocation_result_event_bindings",
    "invocation_result_manifests",
    "invocation_result_publications",
    "invocation_result_receipts",
    "invocation_result_requests",
)
_RESERVED_RESULT_EVENT_TYPE = _TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE
_RESERVED_RESULT_TERMINAL_EVENT_TYPE = _TASK_STATUS_CHANGED_EVENT_TYPE
_RESERVED_RESULT_TERMINAL_KEY_TOKENS = frozenset(
    (
        "transitionkind",
        "resultreceiptid",
        "resulteventid",
        "resultevidencedigest",
        "runningtaskrevision",
        "terminaltaskrevision",
    )
)


class _EventStoreProcessMismatchSignal(BaseException):
    """Module-private ownership signal that must never escape a public boundary."""

    __slots__ = ("token",)

    def __init__(self, token: object) -> None:
        super().__init__("event store process mismatch")
        self.token = token


@dataclass(frozen=True)
class _EventStoreControlDescriptor:
    kind: str
    system_exit_code: Optional[object] = None


class _EventStoreAdmissionControlSignal(BaseException):
    """Private trampoline signal that cannot retain caller-owned exception state."""

    __slots__ = ("ambiguity", "descriptor", "token")

    def __init__(
        self,
        descriptor: _EventStoreControlDescriptor,
        *,
        ambiguity: bool,
        token: object,
    ) -> None:
        super().__init__("event store admission control signal")
        self.descriptor = descriptor
        self.ambiguity = ambiguity
        self.token = token


class _EventStoreAdmissionTransactionSignal(BaseException):
    """Trusted classification emitted by the admission-aware transaction boundary."""

    __slots__ = ("control", "outcome", "token")

    def __init__(
        self,
        outcome: str,
        *,
        control: Optional[_EventStoreControlDescriptor],
        token: object,
    ) -> None:
        super().__init__("event store admission transaction signal")
        self.outcome = outcome
        self.control = control
        self.token = token


class _EventStoreStartControlSignal(BaseException):
    """Private start control signal that never retains caller or lease state."""

    __slots__ = ("ambiguity", "descriptor", "token")

    def __init__(
        self,
        descriptor: _EventStoreControlDescriptor,
        *,
        ambiguity: bool,
        token: object,
    ) -> None:
        super().__init__("event store invocation-start control signal")
        self.descriptor = descriptor
        self.ambiguity = ambiguity
        self.token = token


class _EventStoreStartErrorSignal(BaseException):
    """Private fixed-error trampoline used after plaintext lease authority exists."""

    __slots__ = ("kind", "token")

    def __init__(self, kind: str, *, token: object) -> None:
        super().__init__("event store invocation-start error signal")
        self.kind = kind
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


def _trusted_event_store_public_mismatch(error: object) -> bool:
    """Recognize a public mismatch previously created by the clean trampoline."""

    if type(error) is not EventStoreLifecycleError:
        return False
    public_error = error
    if (
        public_error.args != (EventStoreLifecycleError.code,)
        or public_error.__cause__ is not None
        or public_error.__context__ is not None
        or getattr(public_error, "__notes__", None) is not None
    ):
        return False
    traceback_cursor = public_error.__traceback__
    last_code = None
    while traceback_cursor is not None:
        last_code = traceback_cursor.tb_frame.f_code
        traceback_cursor = traceback_cursor.tb_next
    return last_code is _raise_event_store_process_mismatch.__code__


def _consume_event_store_public_mismatch(error: EventStoreLifecycleError) -> bool:
    """Detach a trusted nested public mismatch before publishing a fresh outer error."""

    if not _trusted_event_store_public_mismatch(error):
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


def _quarantine_inherited_event_store_graph(root: object) -> None:
    """Retain one inherited graph without invoking its equality, cleanup, or finalizers."""

    if any(retained is root for retained in _EVENT_STORE_CHILD_GRAPH_QUARANTINE):
        return
    _EVENT_STORE_CHILD_GRAPH_QUARANTINE.append(root)


def _is_exact_control_signal(error: BaseException) -> bool:
    return type(error) in (KeyboardInterrupt, SystemExit, GeneratorExit, CancelledError)


def _event_store_control_descriptor(
    error: BaseException,
) -> Optional[_EventStoreControlDescriptor]:
    if type(error) is KeyboardInterrupt:
        return _EventStoreControlDescriptor("keyboard_interrupt")
    if type(error) is GeneratorExit:
        return _EventStoreControlDescriptor("generator_exit")
    if type(error) is CancelledError:
        return _EventStoreControlDescriptor("cancelled")
    if type(error) is SystemExit:
        code = error.code
        if code is None or type(code) is bool:
            safe_code: Optional[object] = code
        elif type(code) is int and 0 <= code <= 255:
            safe_code = code
        else:
            safe_code = 1
        return _EventStoreControlDescriptor("system_exit", safe_code)
    return None


def _result_artifact_continuity_control(
    error: BaseException,
) -> Tuple[Optional[_EventStoreControlDescriptor], Tuple[BaseException, ...]]:
    """Find one exact displaced control in a bounded internal continuity graph."""

    if type(error) is not _ResultArtifactTransactionContinuityError:
        return None, ()
    pending: List[BaseException] = [error]
    observed: List[BaseException] = []
    controls: List[_EventStoreControlDescriptor] = []
    while pending and len(observed) < 8:
        current = pending.pop()
        if any(current is item for item in observed):
            continue
        observed.append(current)
        descriptor = _event_store_control_descriptor(current)
        if descriptor is not None:
            controls.append(descriptor)
        cause = _BASE_EXCEPTION_CAUSE_DESCRIPTOR.__get__(current, BaseException)
        context = _BASE_EXCEPTION_CONTEXT_DESCRIPTOR.__get__(current, BaseException)
        suppress_context = _BASE_EXCEPTION_SUPPRESS_CONTEXT_DESCRIPTOR.__get__(
            current,
            BaseException,
        )
        linked_errors: Tuple[BaseException, ...]
        if isinstance(cause, BaseException):
            linked_errors = (cause,)
        elif suppress_context:
            linked_errors = ()
        elif isinstance(context, BaseException):
            linked_errors = (context,)
        else:
            linked_errors = ()
        for linked in linked_errors:
            if not any(linked is item for item in observed):
                pending.append(linked)
    if pending or len(controls) != 1:
        return None, tuple(observed)
    return controls[0], tuple(observed)


def _normalized_event_store_control_descriptor(
    descriptor: object,
) -> Optional[_EventStoreControlDescriptor]:
    if type(descriptor) is not _EventStoreControlDescriptor:
        return None
    if descriptor.kind in {"keyboard_interrupt", "generator_exit", "cancelled"}:
        if descriptor.system_exit_code is not None:
            return None
        return _EventStoreControlDescriptor(descriptor.kind)
    if descriptor.kind == "system_exit":
        code = descriptor.system_exit_code
        if code is None or type(code) is bool:
            safe_code: Optional[object] = code
        elif type(code) is int and 0 <= code <= 255:
            safe_code = code
        else:
            safe_code = 1
        return _EventStoreControlDescriptor("system_exit", safe_code)
    return None


def _event_store_ambiguity_error(scope: str) -> BaseException:
    if scope == "admission":
        return InvocationAdmissionCommitAmbiguityError()
    if scope == "start":
        return InvocationStartCommitAmbiguityError()
    raise RuntimeError("unsupported event store ambiguity scope")


def _raise_clean_event_store_control_for_scope(
    descriptor: _EventStoreControlDescriptor,
    *,
    ambiguity: bool,
    scope: str,
) -> NoReturn:
    if scope not in {"admission", "start"}:
        raise RuntimeError("unsupported event store control scope") from None
    normalized = _normalized_event_store_control_descriptor(descriptor)
    if normalized is None or type(ambiguity) is not bool:
        raise _event_store_ambiguity_error(scope) from None
    cause: Optional[BaseException] = _event_store_ambiguity_error(scope) if ambiguity else None
    expected_type: type[BaseException]
    try:
        if normalized.kind == "keyboard_interrupt":
            expected_type = KeyboardInterrupt
            raise KeyboardInterrupt() from cause
        if normalized.kind == "generator_exit":
            expected_type = GeneratorExit
            raise GeneratorExit() from cause
        if normalized.kind == "cancelled":
            expected_type = CancelledError
            raise CancelledError() from cause
        if normalized.kind == "system_exit":
            expected_type = SystemExit
            raise SystemExit(normalized.system_exit_code) from cause
        raise RuntimeError("unsupported event store control signal")
    except BaseException as public_error:
        if type(public_error) is expected_type:
            public_error.__context__ = None
        raise


def _raise_clean_event_store_control(
    descriptor: _EventStoreControlDescriptor,
    *,
    ambiguity: bool,
) -> NoReturn:
    """Preserve the existing admission-scoped clean control contract."""

    _raise_clean_event_store_control_for_scope(
        descriptor,
        ambiguity=ambiguity,
        scope="admission",
    )


def _raise_clean_invocation_start_control(
    descriptor: _EventStoreControlDescriptor,
    *,
    ambiguity: bool,
) -> NoReturn:
    """Reissue one clean control with a start-scoped ambiguity cause."""

    _raise_clean_event_store_control_for_scope(
        descriptor,
        ambiguity=ambiguity,
        scope="start",
    )


def _raise_clean_invocation_start_error(kind: str) -> NoReturn:
    error_type: type[BaseException]
    if kind == "conflict":
        error_type = InvocationStartConflictError
    elif kind == "transaction":
        error_type = InvocationStartTransactionError
    elif kind == "ambiguous":
        error_type = InvocationStartCommitAmbiguityError
    else:
        raise RuntimeError("unsupported invocation-start error kind") from None
    try:
        raise error_type() from None
    except BaseException as public_error:
        if type(public_error) is error_type:
            public_error.__context__ = None
        raise


def _detach_exception(error: BaseException) -> None:
    error_traceback = _BASE_EXCEPTION_TRACEBACK_DESCRIPTOR.__get__(error, BaseException)
    _BASE_EXCEPTION_CAUSE_DESCRIPTOR.__set__(error, None)
    _BASE_EXCEPTION_CONTEXT_DESCRIPTOR.__set__(error, None)
    _BASE_EXCEPTION_TRACEBACK_DESCRIPTOR.__set__(error, None)
    if error_traceback is not None:
        traceback_module.clear_frames(error_traceback)


def _raise_clean_stored_event_envelope_error(kind: str) -> NoReturn:
    if kind == "contract":
        error_type: type[BaseException] = _ResultEventWriteContractError
    elif kind == "integrity":
        error_type = EventStoreIntegrityError
    elif kind == "concurrency":
        error_type = ConcurrencyError
    else:
        raise RuntimeError("unsupported stored event envelope error kind") from None
    try:
        if error_type is _ResultEventWriteContractError:
            raise _ResultEventWriteContractError() from None
        if error_type is ConcurrencyError:
            raise ConcurrencyError("verified stored event append concurrency conflict") from None
        raise EventStoreIntegrityError("stored event envelope verification failed") from None
    except BaseException as public_error:
        if type(public_error) is error_type:
            public_error.__context__ = None
        raise


def _raise_clean_result_artifact_error(kind: str) -> NoReturn:
    error: BaseException
    if kind == "conflict":
        error = _ResultArtifactConflictError("result Artifact identity is already bound")
    elif kind == "concurrency":
        error = _ResultArtifactConcurrencyError("result Artifact head changed")
    elif kind == "integrity":
        error = _ResultArtifactIntegrityError("result Artifact transaction integrity failed")
    elif kind == "type":
        error = TypeError("result Artifact write input is invalid")
    elif kind == "value":
        error = ValueError("result Artifact write input is invalid")
    elif kind == "transaction":
        error = RuntimeError("result Artifact owner transaction is unavailable")
    elif kind == "rolled_back":
        error = _ResultArtifactTransactionError("result Artifact transaction was rolled back")
    elif kind == "ambiguous":
        error = _ResultArtifactCommitAmbiguityError(
            "result Artifact commit outcome is unknown; reopen and reconcile"
        )
    else:
        raise RuntimeError("unsupported result Artifact error kind") from None
    try:
        raise error from None
    except BaseException as public_error:
        if public_error is error:
            public_error.__context__ = None
        raise


def _raise_clean_result_artifact_control(
    descriptor: _EventStoreControlDescriptor,
    *,
    ambiguity: bool,
) -> NoReturn:
    normalized = _normalized_event_store_control_descriptor(descriptor)
    if normalized is None or type(ambiguity) is not bool:
        raise _ResultArtifactIntegrityError(
            "result Artifact transaction integrity failed"
        ) from None
    cause: Optional[BaseException] = (
        _ResultArtifactCommitAmbiguityError(
            "result Artifact commit outcome is unknown; reopen and reconcile"
        )
        if ambiguity
        else None
    )
    expected_type: type[BaseException]
    try:
        if normalized.kind == "keyboard_interrupt":
            expected_type = KeyboardInterrupt
            raise KeyboardInterrupt() from cause
        if normalized.kind == "generator_exit":
            expected_type = GeneratorExit
            raise GeneratorExit() from cause
        if normalized.kind == "cancelled":
            expected_type = CancelledError
            raise CancelledError() from cause
        if normalized.kind == "system_exit":
            expected_type = SystemExit
            raise SystemExit(normalized.system_exit_code) from cause
        raise RuntimeError("unsupported result Artifact control signal") from None
    except BaseException as public_error:
        if type(public_error) is expected_type:
            public_error.__context__ = None
        raise


def _take_classified_event_store_transaction_signal(
    error: BaseException,
) -> Optional[Tuple[str, Optional[_EventStoreControlDescriptor]]]:
    """Copy and detach one trusted rollback/ambiguity outcome for scoped callers."""

    if type(error) is not _EventStoreAdmissionTransactionSignal:
        return None
    signal = error
    if signal.token is not _EVENT_STORE_ADMISSION_CONTROL_TOKEN or signal.outcome not in {
        "rolled_back",
        "ambiguous",
    }:
        return None
    control = _normalized_event_store_control_descriptor(signal.control)
    if signal.control is not None and control is None:
        return None
    outcome = signal.outcome
    _detach_exception(signal)
    return outcome, control


_Method = TypeVar("_Method", bound=Callable[..., Any])


def _sanitize_stored_event_envelope_errors(method: _Method) -> _Method:
    """Reissue fixed adapter errors after payload-bearing frames and arguments unwind."""

    @wraps(method)
    def sanitized(*args: Any, **kwargs: Any) -> Any:
        kind: Optional[str] = None
        try:
            return method(*args, **kwargs)
        except _ResultEventWriteContractError as error:
            kind = "contract"
            _detach_exception(error)
        except EventStoreIntegrityError as error:
            kind = "integrity"
            _detach_exception(error)
        except ConcurrencyError as error:
            kind = "concurrency"
            _detach_exception(error)
        del args, kwargs
        if kind is None:
            raise RuntimeError("stored event envelope sanitizer classification is missing")
        _raise_clean_stored_event_envelope_error(kind)

    return cast(_Method, sanitized)


def _sanitize_result_artifact_errors(method: _Method) -> _Method:
    """Reissue fixed errors only after content-bearing write frames have unwound."""

    @wraps(method)
    def sanitized(*args: Any, **kwargs: Any) -> Any:
        kind: Optional[str] = None
        descriptor: Optional[_EventStoreControlDescriptor] = None
        process_mismatch = False
        try:
            return method(*args, **kwargs)
        except _ResultArtifactConflictError as error:
            kind = "conflict" if type(error) is _ResultArtifactConflictError else "integrity"
            _detach_exception(error)
        except _ResultArtifactConcurrencyError as error:
            kind = "concurrency" if type(error) is _ResultArtifactConcurrencyError else "integrity"
            _detach_exception(error)
        except _ResultArtifactIntegrityError as error:
            kind = "integrity"
            _detach_exception(error)
        except EventStoreLifecycleError as error:
            if _consume_event_store_public_mismatch(error):
                process_mismatch = True
            else:
                kind = "integrity"
                _detach_exception(error)
        except BaseException as error:
            descriptor = _event_store_control_descriptor(error)
            if descriptor is not None:
                _detach_exception(error)
            elif type(error) is TypeError:
                kind = "type"
                _detach_exception(error)
            elif type(error) is ValueError:
                kind = "value"
                _detach_exception(error)
            elif type(error) is RuntimeError:
                kind = "transaction"
                _detach_exception(error)
            elif isinstance(error, Exception):
                kind = "integrity"
                _detach_exception(error)
            else:
                raise
        del args, kwargs
        if process_mismatch:
            _raise_event_store_process_mismatch()
        if descriptor is not None:
            _raise_clean_result_artifact_control(descriptor, ambiguity=False)
        if kind is None:
            raise RuntimeError("result Artifact sanitizer classification is missing")
        _raise_clean_result_artifact_error(kind)

    return cast(_Method, sanitized)


def _sanitize_invocation_admission_controls(method: _Method) -> _Method:
    """Reissue exact control flow after admission frames and caller arguments unwind."""

    @wraps(method)
    def sanitized(*args: Any, **kwargs: Any) -> Any:
        descriptor: Optional[_EventStoreControlDescriptor] = None
        ambiguity = False
        try:
            return method(*args, **kwargs)
        except _EventStoreAdmissionControlSignal as error:
            if (
                type(error) is not _EventStoreAdmissionControlSignal
                or error.token is not _EVENT_STORE_ADMISSION_CONTROL_TOKEN
                or type(error.ambiguity) is not bool
            ):
                raise
            descriptor = _normalized_event_store_control_descriptor(error.descriptor)
            ambiguity = error.ambiguity
            if descriptor is None:
                raise
            _detach_exception(error)
        except BaseException as error:
            descriptor = _event_store_control_descriptor(error)
            if descriptor is None:
                raise
            _detach_exception(error)
        del args, kwargs
        _raise_clean_event_store_control(descriptor, ambiguity=ambiguity)

    return cast(_Method, sanitized)


def _sanitize_invocation_start_controls(method: _Method) -> _Method:
    """Unwind capability-bearing start frames before publishing controls or errors."""

    @wraps(method)
    def sanitized(*args: Any, **kwargs: Any) -> Any:
        descriptor: Optional[_EventStoreControlDescriptor] = None
        ambiguity = False
        fixed_error_kind: Optional[str] = None
        process_mismatch = False
        try:
            return method(*args, **kwargs)
        except _EventStoreStartControlSignal as error:
            if (
                type(error) is not _EventStoreStartControlSignal
                or error.token is not _EVENT_STORE_START_CONTROL_TOKEN
                or type(error.ambiguity) is not bool
            ):
                raise
            descriptor = _normalized_event_store_control_descriptor(error.descriptor)
            ambiguity = error.ambiguity
            if descriptor is None:
                raise
            _detach_exception(error)
        except _EventStoreStartErrorSignal as error:
            if (
                type(error) is not _EventStoreStartErrorSignal
                or error.token is not _EVENT_STORE_START_CONTROL_TOKEN
                or type(error.kind) is not str
                or error.kind not in {"conflict", "transaction", "ambiguous"}
            ):
                raise
            fixed_error_kind = error.kind
            _detach_exception(error)
        except (
            InvocationStartConflictError,
            InvocationStartTransactionError,
            InvocationStartCommitAmbiguityError,
        ) as error:
            if type(error) is InvocationStartConflictError:
                fixed_error_kind = "conflict"
            elif type(error) is InvocationStartTransactionError:
                fixed_error_kind = "transaction"
            elif type(error) is InvocationStartCommitAmbiguityError:
                fixed_error_kind = "ambiguous"
            else:  # pragma: no cover - subclasses are re-raised by the exact checks.
                raise
            _detach_exception(error)
        except EventStoreLifecycleError as error:
            trusted = _consume_event_store_public_mismatch(error)
            if not trusted:
                raise
            process_mismatch = True
        except BaseException as error:
            descriptor = _event_store_control_descriptor(error)
            if descriptor is None:
                raise
            _detach_exception(error)
        del args, kwargs
        if process_mismatch:
            _raise_event_store_process_mismatch()
        if descriptor is not None:
            _raise_clean_invocation_start_control(descriptor, ambiguity=ambiguity)
        if fixed_error_kind is not None:
            _raise_clean_invocation_start_error(fixed_error_kind)
        raise RuntimeError("invocation-start sanitizer classification is missing")

    return cast(_Method, sanitized)


def _bind_event_store_process(method: _Method) -> _Method:
    """Reject a fork-inherited store before reading caller inputs or dependencies."""

    @wraps(method)
    def process_bound(*args: Any, **kwargs: Any) -> Any:
        store = args[0]
        quarantine_required = True
        try:
            store._require_current_process()
            if store._poisoned and method.__name__ not in {"close", "__exit__"}:
                del args, kwargs, store
                raise EventStorePoisonedError() from None
            return method(*args, **kwargs)
        except _EventStoreProcessMismatchSignal as error:
            trusted = _consume_event_store_process_signal(error)
            if not trusted:
                raise
        except EventStoreLifecycleError as error:
            trusted = _consume_event_store_public_mismatch(error)
            if not trusted:
                raise
            quarantine_required = not store._process_is_current()
        if quarantine_required:
            _quarantine_inherited_event_store_graph(store)
        if method.__name__ == "__exit__" and len(args) >= 3 and _is_exact_control_signal(args[2]):
            del args, kwargs, store
            return False
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


def _persisted_result_acceptance_digest(value: Any, field_name: str) -> str:
    digest = _persisted_text(value, field_name, required=True)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"persisted {field_name} is not a canonical SHA-256 digest")
    return digest


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


def _caller_invocation_identity(value: object, field_name: str) -> str:
    """Snapshot one canonical identity before a start transaction touches SQLite."""

    snapshot = _caller_text(value, field_name, required=True)
    if snapshot != snapshot.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    try:
        encoded = snapshot.encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        raise ValueError(f"{field_name} must be valid UTF-8") from None
    if len(encoded) > 4_096:
        raise ValueError(f"{field_name} exceeds its UTF-8 byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in snapshot):
        raise ValueError(f"{field_name} contains a C0 or DEL control character")
    if unicodedata.normalize("NFC", snapshot) != snapshot:
        raise ValueError(f"{field_name} must use Unicode NFC")
    return snapshot


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
        try:
            snapshot = float(value)
        except OverflowError:
            raise ValueError(f"{field_name} exceeds the supported numeric range") from None
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


_RESULT_READBACK_MANIFEST_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "manifest_digest",
    "schema_version",
    "canonical_bytes",
    "byte_size",
    "created_at",
)
_RESULT_READBACK_REQUEST_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "request_digest",
    "schema_version",
    "acceptance_idempotency_key",
    "request_identity_bytes",
    "request_identity_byte_size",
    "invocation_id",
    "session_id",
    "plan_id",
    "task_id",
    "agent_id",
    "job_idempotency_key",
    "start_receipt_digest",
    "execution_manifest_digest",
    "result_manifest_digest",
    "expected_stream_version",
    "running_task_revision",
    "terminal_task_revision",
    "correlation_id",
    "causation_id",
    "runtime_revision",
    "effect_class",
    "action_receipt_set_digest",
    "result_ref",
    "primary_artifact_id",
    "artifact_count",
    "created_at",
)
_RESULT_READBACK_BINDING_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "receipt_id",
    "event_role",
    "event_id",
    "event_type",
    "global_position",
)
_RESULT_READBACK_RECEIPT_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "receipt_id",
    "schema_version",
    "request_digest",
    "invocation_id",
    "session_id",
    "plan_id",
    "task_id",
    "agent_id",
    "job_idempotency_key",
    "acceptance_idempotency_key",
    "attempt_id",
    "attempt_number",
    "lease_epoch",
    "worker_id",
    "lease_token_digest",
    "start_receipt_digest",
    "execution_manifest_digest",
    "result_manifest_schema_version",
    "result_manifest_digest",
    "result_ref",
    "effect_class",
    "action_receipt_set_digest",
    "expected_stream_version",
    "running_task_revision",
    "terminal_task_revision",
    "accepted_at",
    "artifact_count",
    "result_evidence_digest",
    "terminal_transition_digest",
    "receipt_digest",
    "result_event_id",
    "result_event_stream_id",
    "result_event_type",
    "result_event_timestamp",
    "result_event_sequence",
    "result_event_global_position",
    "result_event_envelope_digest",
    "terminal_event_id",
    "terminal_event_stream_id",
    "terminal_event_type",
    "terminal_event_timestamp",
    "terminal_event_sequence",
    "terminal_event_global_position",
    "terminal_event_envelope_digest",
)
_RESULT_READBACK_ARTIFACT_COLUMNS = (
    "tenant_id",
    "workspace_id",
    "receipt_id",
    "ordinal",
    "session_id",
    "task_id",
    "artifact_id",
    "name",
    "version",
    "parent_version",
    "media_type",
    "blob_digest",
    "byte_size",
    "metadata_digest",
    "created_by",
    "idempotency_key",
    "artifact_request_digest",
    "candidate_digest",
)
_RESULT_READBACK_EVENT_COLUMNS = (
    "global_position",
    "stream_id",
    "sequence",
    "event_id",
    "event_type",
    "actor_id",
    "timestamp",
    "payload_json",
    "correlation_id",
    "causation_id",
    "idempotency_key",
)
_RESULT_READBACK_BLOB_COLUMNS = (
    "digest",
    "content",
    "byte_size",
    "created_at",
    "digest_storage",
    "content_storage",
    "byte_size_storage",
    "created_at_storage",
    "content_length",
)
_RESULT_READBACK_VERSION_COLUMNS = (
    "artifact_id",
    "tenant_id",
    "workspace_id",
    "session_id",
    "task_id",
    "name",
    "version",
    "parent_version",
    "media_type",
    "blob_digest",
    "byte_size",
    "metadata_json",
    "created_by",
    "created_at",
    "idempotency_key",
    "request_digest",
)
_RESULT_READBACK_JOB_COLUMNS = (
    "invocation_id",
    "session_id",
    "plan_id",
    "task_id",
    "agent_id",
    "idempotency_key",
    "payload_digest",
    "priority",
    "status",
    "max_attempts",
    "attempts_started",
    "lease_epoch",
    "requested_available_at",
    "available_at",
    "created_at",
    "updated_at",
    "lease_owner",
    "lease_token_digest",
    "lease_expires_at",
    "heartbeat_at",
    "result_ref",
    "last_error",
    "finished_at",
)
_RESULT_READBACK_ATTEMPT_COLUMNS = (
    "attempt_id",
    "invocation_id",
    "attempt_number",
    "lease_epoch",
    "worker_id",
    "lease_token_digest",
    "status",
    "started_at",
    "heartbeat_at",
    "lease_expires_at",
    "finished_at",
    "error",
    "result_ref",
)


def _result_readback_row(row: object, columns: tuple[str, ...], label: str) -> sqlite3.Row:
    if type(row) is not sqlite3.Row:
        raise _ResultAcceptanceIntegrityError(f"result acceptance {label} row is not exact")
    try:
        keys = tuple(row.keys())
    except (AttributeError, TypeError, ValueError) as error:
        raise _ResultAcceptanceIntegrityError(
            f"result acceptance {label} row shape is invalid"
        ) from error
    if keys != columns:
        raise _ResultAcceptanceIntegrityError(f"result acceptance {label} columns are not exact")
    return row


def _result_readback_timestamp(value: object, label: str) -> str:
    timestamp = _persisted_text(value, label, required=True)
    try:
        normalized = _normalize_invocation_timestamp(timestamp, label)
    except (TypeError, ValueError):
        raise _ResultAcceptanceIntegrityError(
            f"result acceptance {label} timestamp is not canonical"
        ) from None
    if normalized != timestamp:
        raise _ResultAcceptanceIntegrityError(
            f"result acceptance {label} timestamp is not canonical"
        )
    return timestamp


class _InsertedFreshResultAcceptancePlanV2:
    """Two privately verified event rows produced by one still-open owner transaction."""

    __slots__ = (
        "__active",
        "__evented",
        "__result_envelope",
        "__result_stored",
        "__terminal_envelope",
        "__terminal_stored",
    )

    def __init__(
        self,
        *,
        evented: _EventedFreshResultAcceptancePlanV2,
        result_stored: StoredEvent,
        result_envelope: _StoredEventEnvelopeV1,
        terminal_stored: StoredEvent,
        terminal_envelope: _StoredEventEnvelopeV1,
        token: object,
    ) -> None:
        if type(self) is not _InsertedFreshResultAcceptancePlanV2:
            raise TypeError("inserted result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("inserted result acceptance plan constructor is private")
        self.__evented = evented
        self.__result_stored = result_stored
        self.__result_envelope = result_envelope
        self.__terminal_stored = terminal_stored
        self.__terminal_envelope = terminal_envelope
        self.__active = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @staticmethod
    def _verify_stored_event_and_envelope(
        stored: StoredEvent,
        envelope: _StoredEventEnvelopeV1,
        expected_event: DomainEvent,
    ) -> None:
        if type(stored) is not StoredEvent or type(envelope) is not _StoredEventEnvelopeV1:
            raise TypeError("inserted result event proof values are not exact")
        if type(expected_event) is not DomainEvent or stored.event != expected_event:
            raise ValueError("inserted result event differs from its canonical snapshot")
        event = stored.event
        body = _StoredEventEnvelopeV1.to_dict(envelope)
        expected = {
            "schemaVersion": body["schemaVersion"],
            "eventId": event.event_id,
            "streamId": event.stream_id,
            "eventType": event.event_type,
            "actorId": event.actor_id,
            "timestamp": event.timestamp,
            "correlationId": event.correlation_id,
            "causationId": event.causation_id,
            "idempotencyKey": event.idempotency_key,
            "payload": dict(event.payload),
            "sequence": stored.sequence,
            "globalPosition": stored.global_position,
        }
        if body != expected:
            raise ValueError("inserted result event envelope differs from its canonical snapshot")

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[
        _EventedFreshResultAcceptancePlanV2,
        StoredEvent,
        _StoredEventEnvelopeV1,
        StoredEvent,
        _StoredEventEnvelopeV1,
    ]:
        if type(self) is not _InsertedFreshResultAcceptancePlanV2:
            raise TypeError("inserted result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("inserted result acceptance plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("inserted result acceptance plan is no longer active")
        evented = self.__evented
        if type(evented) is not _EventedFreshResultAcceptancePlanV2:
            raise TypeError("inserted result acceptance event plan is not exact")
        _transitioned, expected_result, expected_terminal = evented._validated(
            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
        )
        result_stored = self.__result_stored
        result_envelope = self.__result_envelope
        terminal_stored = self.__terminal_stored
        terminal_envelope = self.__terminal_envelope
        self._verify_stored_event_and_envelope(
            result_stored,
            result_envelope,
            expected_result,
        )
        self._verify_stored_event_and_envelope(
            terminal_stored,
            terminal_envelope,
            expected_terminal,
        )
        if (
            terminal_stored.sequence != result_stored.sequence + 1
            or terminal_stored.global_position != result_stored.global_position + 1
            or terminal_stored.event.timestamp != result_stored.event.timestamp
            or terminal_stored.event.stream_id != result_stored.event.stream_id
            or terminal_stored.event.correlation_id != result_stored.event.correlation_id
            or terminal_stored.event.causation_id != result_stored.event.event_id
        ):
            raise ValueError("inserted result event pair coordinates are not consecutive")
        return (
            evented,
            result_stored,
            result_envelope,
            terminal_stored,
            terminal_envelope,
        )

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("inserted result acceptance plan invalidation is private")
        self.__active = False
        object.__setattr__(self, "_InsertedFreshResultAcceptancePlanV2__evented", None)
        object.__setattr__(self, "_InsertedFreshResultAcceptancePlanV2__result_stored", None)
        object.__setattr__(self, "_InsertedFreshResultAcceptancePlanV2__result_envelope", None)
        object.__setattr__(self, "_InsertedFreshResultAcceptancePlanV2__terminal_stored", None)
        object.__setattr__(self, "_InsertedFreshResultAcceptancePlanV2__terminal_envelope", None)

    def __copy__(self) -> NoReturn:
        raise TypeError("inserted result acceptance plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("inserted result acceptance plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("inserted result acceptance plans cannot be serialized")


class _ReceiptedFreshResultAcceptancePlanV2:
    """One exact receipt reconstructed from freshly verified event rows."""

    __slots__ = ("__active", "__inserted", "__receipt")

    def __init__(
        self,
        *,
        inserted: _InsertedFreshResultAcceptancePlanV2,
        receipt: _ScopedInvocationResultReceiptV2,
        token: object,
    ) -> None:
        if type(self) is not _ReceiptedFreshResultAcceptancePlanV2:
            raise TypeError("receipted result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("receipted result acceptance plan constructor is private")
        self.__inserted = inserted
        self.__receipt = receipt
        self.__active = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[_InsertedFreshResultAcceptancePlanV2, _ScopedInvocationResultReceiptV2]:
        if type(self) is not _ReceiptedFreshResultAcceptancePlanV2:
            raise TypeError("receipted result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("receipted result acceptance plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("receipted result acceptance plan is no longer active")
        inserted = self.__inserted
        if type(inserted) is not _InsertedFreshResultAcceptancePlanV2:
            raise TypeError("receipted result acceptance insert plan is not exact")
        evented, result_stored, result_envelope, terminal_stored, terminal_envelope = (
            inserted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        )
        transitioned, result_event, terminal_event = evented._validated(
            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
        )
        evidenced, terminal_transition = transitioned._validated(
            token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
        )
        identified, evidence = evidenced._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        materialized, _, _, _ = identified._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        prepared, _, _, _ = materialized._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        result_coordinates = _ScopedInvocationResultEventCoordinatesV2(
            event_id=result_stored.event.event_id,
            stream_id=result_stored.event.stream_id,
            event_type=result_stored.event.event_type,
            sequence=result_stored.sequence,
            global_position=result_stored.global_position,
            event_envelope_digest=_StoredEventEnvelopeV1.digest(result_envelope),
        )
        terminal_coordinates = _ScopedInvocationResultEventCoordinatesV2(
            event_id=terminal_stored.event.event_id,
            stream_id=terminal_stored.event.stream_id,
            event_type=terminal_stored.event.event_type,
            sequence=terminal_stored.sequence,
            global_position=terminal_stored.global_position,
            event_envelope_digest=_StoredEventEnvelopeV1.digest(terminal_envelope),
        )
        expected = _build_scoped_invocation_result_receipt_v2(
            prepared.request,
            evidence,
            result_event=result_coordinates,
            terminal_event=terminal_coordinates,
            terminal_transition=terminal_transition,
        )
        receipt = self.__receipt
        if type(receipt) is not _ScopedInvocationResultReceiptV2:
            raise TypeError("result acceptance receipt is not exact")
        receipt_snapshot = _ScopedInvocationResultReceiptV2.from_dict(
            _ScopedInvocationResultReceiptV2.to_dict(receipt)
        )
        if receipt_snapshot != expected:
            raise ValueError("result acceptance receipt differs from its inserted event graph")
        if result_event != result_stored.event or terminal_event != terminal_stored.event:
            raise ValueError("result acceptance inserted events differ from their canonical pair")
        return inserted, receipt_snapshot

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("receipted result acceptance plan invalidation is private")
        self.__active = False
        object.__setattr__(self, "_ReceiptedFreshResultAcceptancePlanV2__inserted", None)
        object.__setattr__(self, "_ReceiptedFreshResultAcceptancePlanV2__receipt", None)

    def __copy__(self) -> NoReturn:
        raise TypeError("receipted result acceptance plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("receipted result acceptance plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("receipted result acceptance plans cannot be serialized")


class _PersistedFreshResultAcceptancePlanV2:
    """One private receipt whose immutable graph rows were inserted in the owner transaction."""

    __slots__ = ("__active", "__receipted", "__receipt")

    def __init__(
        self,
        *,
        receipted: _ReceiptedFreshResultAcceptancePlanV2,
        receipt: _ScopedInvocationResultReceiptV2,
        token: object,
    ) -> None:
        if type(self) is not _PersistedFreshResultAcceptancePlanV2:
            raise TypeError("persisted result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("persisted result acceptance plan constructor is private")
        self.__receipted = receipted
        self.__receipt = receipt
        self.__active = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[_ReceiptedFreshResultAcceptancePlanV2, _ScopedInvocationResultReceiptV2]:
        if type(self) is not _PersistedFreshResultAcceptancePlanV2:
            raise TypeError("persisted result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("persisted result acceptance plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("persisted result acceptance plan is no longer active")
        receipted = self.__receipted
        if type(receipted) is not _ReceiptedFreshResultAcceptancePlanV2:
            raise TypeError("persisted result acceptance receipt plan is not exact")
        _inserted, expected = receipted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        receipt = self.__receipt
        if type(receipt) is not _ScopedInvocationResultReceiptV2:
            raise TypeError("persisted result acceptance receipt is not exact")
        receipt_snapshot = _ScopedInvocationResultReceiptV2.from_dict(
            _ScopedInvocationResultReceiptV2.to_dict(receipt)
        )
        if receipt_snapshot != expected:
            raise ValueError("persisted result acceptance receipt differs from its receipt plan")
        return receipted, receipt_snapshot

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("persisted result acceptance plan invalidation is private")
        self.__active = False
        object.__setattr__(self, "_PersistedFreshResultAcceptancePlanV2__receipted", None)
        object.__setattr__(self, "_PersistedFreshResultAcceptancePlanV2__receipt", None)

    def __copy__(self) -> NoReturn:
        raise TypeError("persisted result acceptance plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("persisted result acceptance plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("persisted result acceptance plans cannot be serialized")


class _CompletedFreshResultAcceptancePlanV2:
    """One private receipt after the owning job and attempt have reached succeeded."""

    __slots__ = ("__active", "__persisted", "__receipt")

    def __init__(
        self,
        *,
        persisted: _PersistedFreshResultAcceptancePlanV2,
        receipt: _ScopedInvocationResultReceiptV2,
        token: object,
    ) -> None:
        if type(self) is not _CompletedFreshResultAcceptancePlanV2:
            raise TypeError("completed result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("completed result acceptance plan constructor is private")
        self.__persisted = persisted
        self.__receipt = receipt
        self.__active = True
        self._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _validated(
        self,
        *,
        token: object,
    ) -> tuple[_PersistedFreshResultAcceptancePlanV2, _ScopedInvocationResultReceiptV2]:
        if type(self) is not _CompletedFreshResultAcceptancePlanV2:
            raise TypeError("completed result acceptance plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("completed result acceptance plan validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("completed result acceptance plan is no longer active")
        persisted = self.__persisted
        if type(persisted) is not _PersistedFreshResultAcceptancePlanV2:
            raise TypeError("completed result acceptance persistence plan is not exact")
        _receipted, expected = persisted._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        receipt = self.__receipt
        if type(receipt) is not _ScopedInvocationResultReceiptV2:
            raise TypeError("completed result acceptance receipt is not exact")
        receipt_snapshot = _ScopedInvocationResultReceiptV2.from_dict(
            _ScopedInvocationResultReceiptV2.to_dict(receipt)
        )
        if receipt_snapshot != expected:
            raise ValueError("completed result acceptance receipt differs from its persisted plan")
        return persisted, receipt_snapshot

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("completed result acceptance plan invalidation is private")
        self.__active = False
        object.__setattr__(self, "_CompletedFreshResultAcceptancePlanV2__persisted", None)
        object.__setattr__(self, "_CompletedFreshResultAcceptancePlanV2__receipt", None)

    def __copy__(self) -> NoReturn:
        raise TypeError("completed result acceptance plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("completed result acceptance plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("completed result acceptance plans cannot be serialized")


class _ReadbackFreshResultAcceptancePlanV2:
    """One private receipt proven against the complete fresh durable graph."""

    __slots__ = ("__active", "__receipt")

    def __init__(
        self,
        *,
        receipt: _ScopedInvocationResultReceiptV2,
        token: object,
    ) -> None:
        if type(self) is not _ReadbackFreshResultAcceptancePlanV2:
            raise TypeError("result acceptance readback plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("result acceptance readback plan constructor is private")
        if type(receipt) is not _ScopedInvocationResultReceiptV2:
            raise TypeError("result acceptance readback receipt is not exact")
        self.__receipt = _ScopedInvocationResultReceiptV2.from_dict(
            _ScopedInvocationResultReceiptV2.to_dict(receipt)
        )
        self.__active = True

    def _validated(
        self,
        *,
        token: object,
    ) -> _ScopedInvocationResultReceiptV2:
        if type(self) is not _ReadbackFreshResultAcceptancePlanV2:
            raise TypeError("result acceptance readback plan must be exact")
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("result acceptance readback validation is private")
        if type(self.__active) is not bool or not self.__active:
            raise RuntimeError("result acceptance readback plan is no longer active")
        receipt = self.__receipt
        if type(receipt) is not _ScopedInvocationResultReceiptV2:
            raise TypeError("result acceptance readback receipt is not exact")
        return _ScopedInvocationResultReceiptV2.from_dict(
            _ScopedInvocationResultReceiptV2.to_dict(receipt)
        )

    def _invalidate(self, *, token: object) -> None:
        if token is not _RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN:
            raise TypeError("result acceptance readback plan invalidation is private")
        self.__active = False
        object.__setattr__(self, "_ReadbackFreshResultAcceptancePlanV2__receipt", None)

    def __copy__(self) -> NoReturn:
        raise TypeError("result acceptance readback plans cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("result acceptance readback plans cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("result acceptance readback plans cannot be serialized")


@dataclass(frozen=True)
class _ResultAcceptanceObservationInputV2:
    """Exact capability-free inputs used by the read-only observation verifier."""

    request: _ScopedInvocationResultAcceptanceRequestV2
    artifact_batch: _PreparedResultArtifactBatch

    def __post_init__(self) -> None:
        if type(self) is not _ResultAcceptanceObservationInputV2:
            raise TypeError("result acceptance observation input must be exact")
        if type(self.request) is not _ScopedInvocationResultAcceptanceRequestV2:
            raise TypeError("result acceptance observation request must be exact")
        if type(self.artifact_batch) is not _PreparedResultArtifactBatch:
            raise TypeError("result acceptance observation Artifact batch must be exact")


@dataclass(frozen=True)
class InvocationAdmissionResult:
    """The events and queued job committed by one atomic invocation admission."""

    events: Tuple[StoredEvent, ...]
    job: InvocationJob


@dataclass(frozen=True)
class _InvocationAdmissionReceipt:
    """Validated durable proof that one event/job unit was admitted atomically."""

    invocation_id: str
    session_id: str
    task_id: str
    stream_id: str
    job_idempotency_key: str
    original_version: int
    event_ids: Tuple[str, ...]
    first_sequence: int
    last_sequence: int
    first_global_position: int
    last_global_position: int
    event_manifest_sha256: str
    job_binding_sha256: str
    admitted_at: str


@dataclass(frozen=True)
class _InvocationStartAdmission:
    """Canonical admitted job that has not yet minted start authority."""

    admission: _InvocationAdmissionReceipt
    request: TaskInvocationAdmissionRequest
    job: InvocationJob


@dataclass(frozen=True)
class _InvocationStartReadback:
    """Validated durable start state, with no plaintext lease authority."""

    admission: _InvocationAdmissionReceipt
    request: TaskInvocationAdmissionRequest
    job: InvocationJob
    attempt: InvocationAttempt
    event: StoredEvent
    receipt: InvocationStartReceipt


_InvocationStartState = Union[  # noqa: UP007 -- strict Python 3.9 mypy gate.
    _InvocationStartAdmission,
    _InvocationStartReadback,
]
_InvocationStartResult = Union[  # noqa: UP007 -- strict Python 3.9 mypy gate.
    InvocationStartClaimed,
    InvocationStartObserved,
]


@dataclass(frozen=True)
class _ScopedInvocationStartAdmission:
    """Scoped canonical admission that has not yet minted start authority."""

    admission: _InvocationAdmissionReceipt
    request: ScopedTaskInvocationAdmissionRequestV2
    job: InvocationJob


@dataclass(frozen=True)
class _ScopedInvocationStartReadback:
    """Validated scoped start state without plaintext lease authority."""

    admission: _InvocationAdmissionReceipt
    request: ScopedTaskInvocationAdmissionRequestV2
    job: InvocationJob
    attempt: InvocationAttempt
    event: StoredEvent
    receipt: ScopedInvocationStartReceiptV3


_ScopedInvocationStartState = Union[  # noqa: UP007 -- strict Python 3.9 mypy gate.
    _ScopedInvocationStartAdmission,
    _ScopedInvocationStartReadback,
]
_ScopedInvocationStartResult = Union[  # noqa: UP007 -- strict Python 3.9 mypy gate.
    ScopedInvocationStartClaimedV3,
    ScopedInvocationStartObservedV3,
]


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
            _quarantine_inherited_event_store_graph(self)
        del self, store
        _raise_event_store_process_mismatch()

    def __next__(self) -> StoredEvent:
        try:
            return self._next_internal()
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
            _quarantine_inherited_event_store_graph(self)
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
            _quarantine_inherited_event_store_graph(self)
        del self, store
        _raise_event_store_process_mismatch()

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> Optional[bool]:
        store = self._store
        try:
            store._require_current_process()
            return None
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
            _quarantine_inherited_event_store_graph(self)
        if _is_exact_control_signal(cast(BaseException, exc)):
            del self, store, exc_type, exc, traceback
            return False
        del self, store, exc_type, exc, traceback
        _raise_event_store_process_mismatch()

    def __copy__(self) -> NoReturn:
        raise TypeError("event store contexts cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("event store contexts cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("event store contexts cannot be serialized")


class _EventStoreTransactionContext:
    """Non-transferable wrapper around one private transaction generator."""

    __slots__ = ("_inner", "_store")

    def __init__(
        self,
        store: "SQLiteEventStore",
        inner: ContextManager[sqlite3.Connection],
    ) -> None:
        self._store = store
        self._inner = inner

    def __enter__(self) -> sqlite3.Connection:
        store = self._store
        try:
            store._require_current_process()
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
            _quarantine_inherited_event_store_graph(self)
        else:
            return self._inner.__enter__()
        del self, store
        _raise_event_store_process_mismatch()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        store = self._store
        if type(exc) is _EventStoreProcessMismatchSignal:
            # The surrounding public wrapper must consume the originating signal after
            # its method frame unwinds. Retain this suspended generator instead of
            # resuming child-inherited transaction cleanup or translating too early.
            _quarantine_inherited_event_store_graph(self)
            return False
        if _trusted_event_store_public_mismatch(exc):
            if not store._process_is_current():
                _quarantine_inherited_event_store_graph(self)
                return False
            return self._inner.__exit__(exc_type, exc, traceback)
        if _is_exact_control_signal(exc):
            try:
                store._require_current_process()
            except _EventStoreProcessMismatchSignal as error:
                if not _consume_event_store_process_signal(error):
                    raise
                _quarantine_inherited_event_store_graph(self)
                del self, store, exc_type, exc, traceback
                return False
        try:
            store._require_current_process()
        except _EventStoreProcessMismatchSignal as error:
            if not _consume_event_store_process_signal(error):
                raise
            _quarantine_inherited_event_store_graph(self)
        else:
            return self._inner.__exit__(exc_type, exc, traceback)
        del self, store, exc_type, exc, traceback
        _raise_event_store_process_mismatch()

    def __copy__(self) -> NoReturn:
        raise TypeError("event store transactions cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("event store transactions cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("event store transactions cannot be serialized")


class SQLiteEventStore:
    """Small durable event log suitable for the kernel and local-first clients."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        clock: Callable[[], str] = utc_now,
        max_json_bytes: int = 1024 * 1024,
        enable_result_acceptance_schema: bool = False,
    ) -> None:
        self._process_owner = _process_identity.capture_process_owner()
        self._poisoned = False
        self._result_artifact_transaction_generation = 0
        self._active_result_artifact_transaction_generation: Optional[int] = None
        self._result_artifact_transaction_rollback_only = False
        process_mismatch = False
        connection: Optional[sqlite3.Connection] = None
        parent: Optional[str] = None
        try:
            self._require_current_process()
            if type(path) is not str:
                raise TypeError("path must be a string")
            if not callable(clock):
                raise TypeError("clock must be callable")
            if type(max_json_bytes) is not int:
                raise TypeError("max_json_bytes must be an integer")
            if max_json_bytes <= 0:
                raise ValueError("max_json_bytes must be greater than zero")
            if type(enable_result_acceptance_schema) is not bool:
                raise TypeError("enable_result_acceptance_schema must be a boolean")
            result_artifact_savepoint_secret = secrets.token_bytes(32)
            self._require_current_process()
            if (
                type(result_artifact_savepoint_secret) is not bytes
                or len(result_artifact_savepoint_secret) != 32
            ):
                raise RuntimeError("result Artifact savepoint secret allocation is invalid")
            self._result_artifact_savepoint_secret = result_artifact_savepoint_secret
            self.path = path
            if path != ":memory:":
                parent = os.path.dirname(os.path.abspath(path))
                os.makedirs(parent, exist_ok=True)
                self._require_current_process()
            connection = sqlite3.connect(
                path,
                check_same_thread=False,
                isolation_level=None,
            )
            self._connection = connection
            self._require_current_process()
            connection.row_factory = sqlite3.Row
            self._lock = threading.RLock()
            self._clock = clock
            self._max_json_bytes = max_json_bytes
            self._result_acceptance_schema_enabled = enable_result_acceptance_schema
            self._initialize()
        except _EventStoreProcessMismatchSignal as error:
            trusted = _consume_event_store_process_signal(error)
            if not trusted:
                raise
            _quarantine_inherited_event_store_graph(self)
            process_mismatch = True
        except BaseException as initialization_error:
            if self._process_is_current():
                if connection is not None:
                    try:
                        connection.close()
                    except BaseException:
                        if not _is_exact_control_signal(initialization_error):
                            raise
                raise
            _quarantine_inherited_event_store_graph(self)
            if _is_exact_control_signal(initialization_error):
                raise
            process_mismatch = True
        if process_mismatch:
            del self, path, clock, max_json_bytes, parent, connection
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

    def __del__(self) -> None:
        """Resurrect an inherited graph so ordinary child GC cannot finalize its resources."""

        try:
            if not self._process_is_current():
                _quarantine_inherited_event_store_graph(self)
        except BaseException:
            try:
                _quarantine_inherited_event_store_graph(self)
            except BaseException:
                # Interpreter teardown may already have cleared module globals. The worker
                # contract therefore still requires mismatch children to use _exit or exec.
                pass

    @contextmanager
    def _locked(self, *, allow_poisoned: bool = False) -> Iterator[None]:
        """Acquire and release the RLock only while this process owns the store."""

        self._require_current_process()
        if type(allow_poisoned) is not bool:
            raise TypeError("allow_poisoned must be a boolean")
        lock = self._lock
        lock.acquire()
        try:
            self._require_current_process()
            if self._poisoned and not allow_poisoned:
                raise EventStorePoisonedError() from None
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
            if self._result_acceptance_schema_enabled:
                from ._inactive_invocation_results_migration import (
                    _KNOWN_INVOCATION_RESULTS_MIGRATIONS,
                )

                _apply_sqlite_migrations_unregistered(
                    self._connection,
                    migrations=_KNOWN_INVOCATION_RESULTS_MIGRATIONS,
                    clock=self._now,
                    _process_guard=self._require_current_process,
                )
            else:
                apply_sqlite_migrations(
                    self._connection,
                    clock=self._now,
                    _process_guard=self._require_current_process,
                )
            self._require_current_process()

    def _transaction(
        self,
        *,
        classify_admission: bool = False,
    ) -> ContextManager[sqlite3.Connection]:
        self._require_current_process()
        if type(classify_admission) is not bool:
            raise TypeError("classify_admission must be a boolean")
        return _EventStoreTransactionContext(
            self,
            self._transaction_inner(classify_admission=classify_admission),
        )

    @_bind_event_store_process
    def _result_artifact_transaction(
        self,
    ) -> ContextManager[_ResultArtifactTransactionHandle]:
        """Open one private owner transaction and yield a non-transferable handle."""

        return self._result_artifact_transaction_inner()

    @contextmanager
    def _result_artifact_transaction_inner(
        self,
    ) -> Iterator[_ResultArtifactTransactionHandle]:
        self._require_current_process()
        if self._active_result_artifact_transaction_generation is not None:
            raise RuntimeError("a result Artifact owner transaction is already active")
        transaction_outcome: Optional[str] = None
        control: Optional[_EventStoreControlDescriptor] = None
        try:
            with self._locked():
                current_generation = self._result_artifact_transaction_generation
                if (
                    type(current_generation) is not int
                    or current_generation < 0
                    or current_generation >= (1 << 128) - 1
                ):
                    raise _ResultArtifactIntegrityError(
                        "result Artifact transaction generation is exhausted"
                    )
                generation = current_generation + 1
                savepoint_secret = self._result_artifact_savepoint_secret
                if type(savepoint_secret) is not bytes or len(savepoint_secret) != 32:
                    raise _ResultArtifactIntegrityError(
                        "result Artifact savepoint secret is invalid"
                    )
                generation_bytes = generation.to_bytes(16, byteorder="big", signed=False)
                savepoint_suffix = _validated_result_artifact_savepoint_suffix(
                    hashlib.sha256(
                        b"quantum-entanglement/result-artifact-savepoint/v1\x00"
                        + savepoint_secret
                        + generation_bytes
                    ).hexdigest()[:32]
                )
                self._result_artifact_transaction_generation = generation
                with self._transaction(classify_admission=True) as connection:
                    self._require_current_process()
                    if self._active_result_artifact_transaction_generation is not None:
                        raise RuntimeError(
                            "a result Artifact owner transaction became active concurrently"
                        )
                    self._active_result_artifact_transaction_generation = generation
                    self._result_artifact_transaction_rollback_only = False
                    handle = _ResultArtifactTransactionHandle(
                        store=self,
                        connection=connection,
                        process_owner=self._process_owner,
                        generation=generation,
                        savepoint_suffix=savepoint_suffix,
                        token=_RESULT_ARTIFACT_TRANSACTION_TOKEN,
                    )
                    try:
                        yield handle
                        self._require_current_process()
                        if self._result_artifact_transaction_rollback_only is not False:
                            raise RuntimeError("result Artifact owner transaction is rollback-only")
                    finally:
                        handle._invalidate(token=_RESULT_ARTIFACT_TRANSACTION_TOKEN)
                        self._active_result_artifact_transaction_generation = None
                        self._result_artifact_transaction_rollback_only = False
        except _EventStoreAdmissionTransactionSignal as error:
            classified = _take_classified_event_store_transaction_signal(error)
            if classified is None:
                raise
            transaction_outcome, control = classified
        if control is not None:
            _raise_clean_result_artifact_control(
                control,
                ambiguity=transaction_outcome == "ambiguous",
            )
        if transaction_outcome == "rolled_back":
            _raise_clean_result_artifact_error("rolled_back")
        if transaction_outcome == "ambiguous":
            _raise_clean_result_artifact_error("ambiguous")
        if transaction_outcome is not None:
            raise RuntimeError("result Artifact transaction classification is invalid") from None

    @_bind_event_store_process
    def _connection_for_result_artifact_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
    ) -> sqlite3.Connection:
        self._require_current_process()
        generation = self._active_result_artifact_transaction_generation
        if type(generation) is not int or generation <= 0:
            raise RuntimeError("no result Artifact owner transaction is active")
        return handle._validated_connection(
            store=self,
            process_owner=self._process_owner,
            generation=generation,
            token=_RESULT_ARTIFACT_TRANSACTION_TOKEN,
        )

    @_bind_event_store_process
    def _savepoint_suffix_for_result_artifact_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
    ) -> str:
        self._require_current_process()
        generation = self._active_result_artifact_transaction_generation
        if type(generation) is not int or generation <= 0:
            raise RuntimeError("no result Artifact owner transaction is active")
        return handle._validated_savepoint_suffix(
            store=self,
            process_owner=self._process_owner,
            generation=generation,
            token=_RESULT_ARTIFACT_TRANSACTION_TOKEN,
        )

    @_sanitize_result_artifact_errors
    @_bind_event_store_process
    def _write_result_artifacts_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        batch: _PreparedResultArtifactBatch,
    ) -> Tuple[_ScopedInvocationResultArtifactV2, ...]:
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = self._connection_for_result_artifact_transaction(handle)
            if type(batch) is not _PreparedResultArtifactBatch:
                raise TypeError("result Artifact write requires an exact prepared batch")
            batch.verify()
            self._require_current_process()
            result = _write_prepared_result_artifacts_in_transaction(
                connection,
                batch,
                clock=self._now,
                process_guard=self._require_current_process,
                savepoint_suffix=self._savepoint_suffix_for_result_artifact_transaction(handle),
            )
            self._require_current_process()
            return result
        except BaseException as error:
            if self._process_is_current():
                continuity_control: Optional[_EventStoreControlDescriptor] = None
                continuity_graph: Tuple[BaseException, ...] = ()
                if isinstance(error, _ResultArtifactTransactionContinuityError):
                    self._poisoned = True
                    continuity_control, continuity_graph = _result_artifact_continuity_control(
                        error
                    )
                if type(connection) is sqlite3.Connection:
                    try:
                        transaction_open = connection.in_transaction
                    except BaseException:
                        self._poisoned = True
                    else:
                        if type(transaction_open) is not bool or not transaction_open:
                            # A dependency that closes the private owner transaction can
                            # have committed work outside this writer's rollback authority.
                            self._poisoned = True
                generation = self._active_result_artifact_transaction_generation
                if type(generation) is int and generation > 0:
                    self._result_artifact_transaction_rollback_only = True
                if continuity_control is not None:
                    for graph_error in continuity_graph:
                        _detach_exception(graph_error)
                    raise _EventStoreAdmissionTransactionSignal(
                        "ambiguous",
                        control=continuity_control,
                        token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                    ) from None
            raise

    def _require_result_acceptance_candidate_schema_in_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Require the exact six-table inactive M5 namespace without registering it."""

        self._require_current_process()
        if (
            type(connection) is not sqlite3.Connection
            or connection is not self._connection
            or not connection.in_transaction
        ):
            raise RuntimeError(
                "result acceptance prerequisites require the owning open transaction"
            )
        try:
            main_rows = connection.execute(
                """
                SELECT name
                FROM main.sqlite_master
                WHERE type = 'table'
                  AND name IN (?, ?, ?, ?, ?, ?)
                ORDER BY name
                """,
                _RESULT_ACCEPTANCE_TABLE_NAMES,
            ).fetchall()
            temp_row = connection.execute(
                """
                SELECT 1
                FROM temp.sqlite_temp_master
                WHERE name IN (?, ?, ?, ?, ?, ?)
                   OR tbl_name IN (?, ?, ?, ?, ?, ?)
                LIMIT 1
                """,
                _RESULT_ACCEPTANCE_TABLE_NAMES + _RESULT_ACCEPTANCE_TABLE_NAMES,
            ).fetchone()
        except sqlite3.Error:
            raise _ResultAcceptanceSchemaUnavailableError(
                "inactive result acceptance schema cannot be inspected"
            ) from None
        self._require_current_process()
        try:
            names = tuple(
                _persisted_text(row["name"], "result table name", required=True)
                for row in main_rows
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise _ResultAcceptanceSchemaUnavailableError(
                "inactive result acceptance schema is malformed"
            ) from None
        if names != tuple(sorted(_RESULT_ACCEPTANCE_TABLE_NAMES)) or temp_row is not None:
            raise _ResultAcceptanceSchemaUnavailableError(
                "inactive result acceptance schema is unavailable or shadowed"
            )

    def _existing_result_acceptance_graph_candidate_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Optional[_ExistingResultAcceptanceGraphCandidateV2]:
        """Classify structural existing/partial state before inspecting fresh lease state."""

        request = prepared.request
        manifest = request.manifest
        claim_evidence = prepared.claimed.receipt.evidence
        request_digest = request.canonical_digest()
        result_manifest_digest = manifest.canonical_digest()
        try:
            request_rows = connection.execute(
                """
                SELECT
                    request_digest,
                    invocation_id,
                    result_manifest_digest,
                    artifact_count
                FROM main.invocation_result_requests
                WHERE request_digest = ?
                   OR invocation_id = ?
                   OR (
                        tenant_id = ? AND workspace_id = ?
                        AND session_id = ? AND task_id = ?
                   )
                   OR (
                        tenant_id = ? AND workspace_id = ?
                        AND session_id = ? AND acceptance_idempotency_key = ?
                   )
                   OR (
                        tenant_id = ? AND workspace_id = ? AND result_ref = ?
                   )
                LIMIT 2
                """,
                (
                    request_digest,
                    manifest.invocation_id,
                    manifest.tenant_id,
                    manifest.workspace_id,
                    manifest.session_id,
                    manifest.task_id,
                    manifest.tenant_id,
                    manifest.workspace_id,
                    manifest.session_id,
                    request.acceptance_idempotency_key,
                    manifest.tenant_id,
                    manifest.workspace_id,
                    manifest.result_ref,
                ),
            ).fetchall()
            receipt_rows = connection.execute(
                """
                SELECT
                    receipt_id,
                    receipt_digest,
                    request_digest,
                    invocation_id,
                    result_manifest_digest,
                    artifact_count,
                    result_event_id,
                    terminal_event_id
                FROM main.invocation_result_receipts
                WHERE request_digest = ?
                   OR invocation_id = ?
                   OR attempt_id = ?
                   OR (
                        tenant_id = ? AND workspace_id = ?
                        AND session_id = ? AND acceptance_idempotency_key = ?
                   )
                   OR (
                        tenant_id = ? AND workspace_id = ? AND result_ref = ?
                   )
                LIMIT 2
                """,
                (
                    request_digest,
                    manifest.invocation_id,
                    claim_evidence.attempt_id,
                    manifest.tenant_id,
                    manifest.workspace_id,
                    manifest.session_id,
                    request.acceptance_idempotency_key,
                    manifest.tenant_id,
                    manifest.workspace_id,
                    manifest.result_ref,
                ),
            ).fetchall()
        except sqlite3.Error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance candidate identities cannot be read"
            ) from None
        self._require_current_process()

        if len(request_rows) > 1 or len(receipt_rows) > 1:
            raise _ResultAcceptanceQuarantineError(
                "result acceptance identities resolve to multiple durable graphs",
                category=_ResultAcceptanceQuarantineCategory.DRIFT,
            )
        if not request_rows and not receipt_rows:
            try:
                partial_row = connection.execute(
                    """
                    SELECT 1
                    FROM main.invocation_result_manifests
                    WHERE tenant_id = ? AND workspace_id = ? AND manifest_digest = ?
                    UNION ALL
                    SELECT 1
                    FROM main.invocation_result_artifacts
                    WHERE tenant_id = ? AND workspace_id = ?
                      AND session_id = ? AND task_id = ?
                    LIMIT 1
                    """,
                    (
                        manifest.tenant_id,
                        manifest.workspace_id,
                        result_manifest_digest,
                        manifest.tenant_id,
                        manifest.workspace_id,
                        manifest.session_id,
                        manifest.task_id,
                    ),
                ).fetchone()
                orphan_row = connection.execute(
                    """
                    SELECT 1
                    FROM main.invocation_result_event_bindings AS binding
                    LEFT JOIN main.invocation_result_receipts AS receipt
                      ON receipt.tenant_id = binding.tenant_id
                     AND receipt.workspace_id = binding.workspace_id
                     AND receipt.receipt_id = binding.receipt_id
                    WHERE receipt.receipt_id IS NULL
                    UNION ALL
                    SELECT 1
                    FROM main.invocation_result_publications AS publication
                    LEFT JOIN main.invocation_result_receipts AS receipt
                      ON receipt.tenant_id = publication.tenant_id
                     AND receipt.workspace_id = publication.workspace_id
                     AND receipt.receipt_id = publication.receipt_id
                    WHERE receipt.receipt_id IS NULL
                    LIMIT 1
                    """
                ).fetchone()
            except sqlite3.Error:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance partial-graph guard cannot be read"
                ) from None
            self._require_current_process()
            if orphan_row is not None:
                raise _ResultAcceptanceQuarantineError(
                    "result acceptance has an orphan durable graph",
                    category=_ResultAcceptanceQuarantineCategory.ORPHAN,
                )
            if partial_row is not None:
                raise _ResultAcceptanceQuarantineError(
                    "result acceptance has a partial durable graph",
                    category=_ResultAcceptanceQuarantineCategory.PARTIAL,
                )
            return None
        if len(request_rows) != 1 or len(receipt_rows) != 1:
            raise _ResultAcceptanceQuarantineError(
                "result acceptance has a partial request/receipt graph",
                category=_ResultAcceptanceQuarantineCategory.PARTIAL,
            )

        request_row = request_rows[0]
        receipt_row = receipt_rows[0]
        try:
            durable_request_digest = _persisted_result_acceptance_digest(
                request_row["request_digest"],
                "result request digest",
            )
            durable_invocation_id = _persisted_text(
                request_row["invocation_id"],
                "result invocation identity",
                required=True,
            )
            durable_manifest_digest = _persisted_result_acceptance_digest(
                request_row["result_manifest_digest"],
                "result manifest digest",
            )
            artifact_count = _persisted_integer(
                request_row["artifact_count"],
                "result artifact count",
            )
            receipt_id = _persisted_text(
                receipt_row["receipt_id"],
                "result receipt identity",
                required=True,
            )
            receipt_digest = _persisted_result_acceptance_digest(
                receipt_row["receipt_digest"],
                "result receipt digest",
            )
            receipt_request_digest = _persisted_result_acceptance_digest(
                receipt_row["request_digest"],
                "receipt request digest",
            )
            receipt_invocation_id = _persisted_text(
                receipt_row["invocation_id"],
                "receipt invocation identity",
                required=True,
            )
            receipt_manifest_digest = _persisted_result_acceptance_digest(
                receipt_row["result_manifest_digest"],
                "receipt manifest digest",
            )
            receipt_artifact_count = _persisted_integer(
                receipt_row["artifact_count"],
                "receipt artifact count",
            )
            result_event_id = _persisted_text(
                receipt_row["result_event_id"],
                "result event identity",
                required=True,
            )
            terminal_event_id = _persisted_text(
                receipt_row["terminal_event_id"],
                "terminal event identity",
                required=True,
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise _ResultAcceptanceQuarantineError(
                "result acceptance request/receipt rows are malformed",
                category=_ResultAcceptanceQuarantineCategory.DRIFT,
            ) from None
        if (
            durable_request_digest != receipt_request_digest
            or durable_invocation_id != receipt_invocation_id
            or durable_manifest_digest != receipt_manifest_digest
            or artifact_count != receipt_artifact_count
            or artifact_count > 256
            or result_event_id == terminal_event_id
        ):
            raise _ResultAcceptanceQuarantineError(
                "result acceptance request/receipt bindings are contradictory",
                category=_ResultAcceptanceQuarantineCategory.DRIFT,
            )

        try:
            manifest_count = connection.execute(
                """
                SELECT count(*) AS row_count
                FROM main.invocation_result_manifests
                WHERE manifest_digest = ?
                """,
                (durable_manifest_digest,),
            ).fetchone()
            artifact_counts = connection.execute(
                """
                SELECT
                    count(*) AS row_count,
                    min(ordinal) AS minimum_ordinal,
                    max(ordinal) AS maximum_ordinal,
                    count(DISTINCT ordinal) AS distinct_ordinals
                FROM main.invocation_result_artifacts
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            binding_counts = connection.execute(
                """
                SELECT
                    count(*) AS row_count,
                    sum(CASE WHEN event_role = 'result' THEN 1 ELSE 0 END) AS result_rows,
                    sum(CASE WHEN event_role = 'terminal' THEN 1 ELSE 0 END) AS terminal_rows
                FROM main.invocation_result_event_bindings
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            event_count = connection.execute(
                """
                SELECT count(*) AS row_count
                FROM main.events
                WHERE event_id IN (?, ?)
                """,
                (result_event_id, terminal_event_id),
            ).fetchone()
            publication_count = connection.execute(
                """
                SELECT count(*) AS row_count
                FROM main.invocation_result_publications AS publication
                JOIN main.outbox AS message ON message.message_id = publication.message_id
                WHERE publication.receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            counts = (
                _persisted_integer(manifest_count["row_count"], "result manifest rows"),
                _persisted_integer(artifact_counts["row_count"], "result artifact rows"),
                artifact_counts["minimum_ordinal"],
                artifact_counts["maximum_ordinal"],
                _persisted_integer(
                    artifact_counts["distinct_ordinals"],
                    "result distinct artifact ordinals",
                ),
                _persisted_integer(binding_counts["row_count"], "result event bindings"),
                _persisted_integer(binding_counts["result_rows"], "result event-role rows"),
                _persisted_integer(
                    binding_counts["terminal_rows"],
                    "terminal event-role rows",
                ),
                _persisted_integer(event_count["row_count"], "result durable events"),
                _persisted_integer(
                    publication_count["row_count"],
                    "result publication rows",
                ),
            )
        except (IndexError, KeyError, TypeError, ValueError, sqlite3.Error):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance structural graph cannot be verified"
            ) from None
        self._require_current_process()
        expected_ordinals: tuple[Optional[int], Optional[int]] = (
            (None, None) if artifact_count == 0 else (0, artifact_count - 1)
        )
        if (
            counts[0] != 1
            or counts[1] != artifact_count
            or (counts[2], counts[3]) != expected_ordinals
            or counts[4] != artifact_count
            or counts[5:] != (2, 1, 1, 2, 1)
        ):
            raise _ResultAcceptanceQuarantineError(
                "result acceptance durable graph is partial",
                category=_ResultAcceptanceQuarantineCategory.PARTIAL,
            )
        return _ExistingResultAcceptanceGraphCandidateV2(
            invocation_id=durable_invocation_id,
            request_digest=durable_request_digest,
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            artifact_count=artifact_count,
        )

    def _fresh_result_acceptance_prerequisites_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> _FreshResultAcceptancePrerequisitesV2:
        """Validate one active scoped start without reading clock or minting authority."""

        request = prepared.request
        result_manifest = request.manifest
        claimed = prepared.claimed
        try:
            state = SQLiteEventStore._load_scoped_invocation_start_in_transaction(
                self,
                connection,
                result_manifest.invocation_id,
                fresh=False,
            )
        except InvocationStartConflictError:
            raise _ResultAcceptanceConflictError(
                "result acceptance scoped start is not durable and exact"
            ) from None
        if type(state) is not _ScopedInvocationStartReadback:
            raise _ResultAcceptanceConflictError(
                "result acceptance requires a durable scoped start"
            )
        if state.receipt != request.start_receipt or state.receipt != claimed.receipt:
            raise _ResultAcceptanceConflictError(
                "result acceptance start receipt differs from durable state"
            )
        execution_manifest = state.request.manifest
        bindings = (
            (result_manifest.tenant_id, execution_manifest.tenant_id),
            (result_manifest.workspace_id, execution_manifest.workspace_id),
            (result_manifest.invocation_id, execution_manifest.invocation_id),
            (result_manifest.session_id, execution_manifest.session_id),
            (result_manifest.plan_id, execution_manifest.plan_id),
            (result_manifest.task_id, execution_manifest.task_id),
            (result_manifest.agent_id, execution_manifest.agent_id),
            (result_manifest.job_idempotency_key, execution_manifest.job_idempotency_key),
            (result_manifest.task_revision, execution_manifest.task_revision),
            (result_manifest.correlation_id, execution_manifest.correlation_id),
            (result_manifest.causation_id, execution_manifest.causation_id),
            (result_manifest.runtime_revision, execution_manifest.runtime_revision),
            (
                result_manifest.execution_manifest_digest,
                execution_manifest.canonical_digest(),
            ),
        )
        if (
            any(actual != expected for actual, expected in bindings)
            or execution_manifest.effect_class is not _EffectClass.PURE
            or execution_manifest.retry_class is not _RetryClass.NEVER
        ):
            raise _ResultAcceptanceConflictError(
                "result acceptance manifest differs from durable execution"
            )

        lease = claimed.lease
        evidence = state.receipt.evidence
        job = state.job
        attempt = state.attempt
        lease_token_digest = SQLiteEventStore._lease_token_digest(lease.lease_token)
        if (
            job.status is not InvocationStatus.RUNNING
            or attempt.status is not AttemptStatus.RUNNING
            or job.invocation_id != lease.invocation_id
            or job.session_id != lease.session_id
            or job.plan_id != lease.plan_id
            or job.task_id != lease.task_id
            or job.agent_id != lease.agent_id
            or job.idempotency_key != lease.idempotency_key
            or job.payload_digest != lease.payload_digest
            or job.max_attempts != 1
            or job.attempts_started != lease.attempt_number
            or job.lease_epoch != lease.lease_epoch
            or job.lease_owner != lease.worker_id
            or job.lease_token_digest != lease_token_digest
            or job.heartbeat_at is None
            or job.lease_expires_at is None
            or job.updated_at != job.heartbeat_at
            or job.result_ref is not None
            or job.last_error is not None
            or job.finished_at is not None
            or attempt.attempt_id != lease.attempt_id
            or attempt.invocation_id != lease.invocation_id
            or attempt.attempt_number != lease.attempt_number
            or attempt.lease_epoch != lease.lease_epoch
            or attempt.worker_id != lease.worker_id
            or attempt.lease_token_digest != lease_token_digest
            or attempt.heartbeat_at != job.heartbeat_at
            or attempt.lease_expires_at != job.lease_expires_at
            or attempt.finished_at is not None
            or attempt.error is not None
            or attempt.result_ref is not None
            or lease_token_digest != evidence.lease_token_digest
            or job.heartbeat_at < evidence.claimed_at
            or job.lease_expires_at < evidence.lease_expires_at
            or job.lease_expires_at <= job.heartbeat_at
        ):
            raise _ResultAcceptanceConflictError("result acceptance fresh lease is no longer exact")

        try:
            version_row = connection.execute(
                """
                SELECT coalesce(max(sequence), 0) AS stream_version
                FROM main.events
                WHERE stream_id = ?
                """,
                (state.receipt.stream_id,),
            ).fetchone()
            current_version = _persisted_integer(
                version_row["stream_version"],
                "result acceptance stream version",
            )
            later_status = connection.execute(
                """
                SELECT 1
                FROM main.events
                WHERE stream_id = ?
                  AND event_type = ?
                  AND sequence > (
                      SELECT sequence FROM main.events WHERE event_id = ?
                  )
                  AND (
                      json_extract(payload_json, '$.taskId') = ?
                      OR json_extract(payload_json, '$.task_id') = ?
                  )
                LIMIT 1
                """,
                (
                    state.receipt.stream_id,
                    _TASK_STATUS_CHANGED_EVENT_TYPE,
                    state.request.task_running_event_id,
                    result_manifest.task_id,
                    result_manifest.task_id,
                ),
            ).fetchone()
        except (IndexError, KeyError, TypeError, ValueError, sqlite3.Error):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance running task state cannot be verified"
            ) from None
        self._require_current_process()
        if current_version != request.expected_stream_version or later_status is not None:
            raise _ResultAcceptanceConflictError(
                "result acceptance task is no longer at the expected RUNNING revision"
            )
        return _FreshResultAcceptancePrerequisitesV2(
            invocation_id=result_manifest.invocation_id,
            request_digest=request.canonical_digest(),
            start_receipt_digest=_scoped_invocation_start_receipt_digest_v3(state.receipt),
            attempt_id=attempt.attempt_id,
            lease_epoch=attempt.lease_epoch,
            worker_id=attempt.worker_id,
            lease_token_digest=attempt.lease_token_digest,
            heartbeat_at=attempt.heartbeat_at,
            lease_expires_at=attempt.lease_expires_at,
            expected_stream_version=current_version,
            running_task_revision=result_manifest.task_revision,
        )

    @_bind_event_store_process
    def _validate_result_acceptance_durable_prerequisites_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> _ExistingResultAcceptanceGraphCandidateV2 | _FreshResultAcceptancePrerequisitesV2:
        """Classify existing graph first, otherwise validate fresh durable ownership."""

        self._require_current_process()
        if type(prepared) is not _PreparedScopedInvocationResultAcceptanceV2:
            raise TypeError("result acceptance prerequisites require exact prepared inputs")
        prepared.verify()
        self._require_result_acceptance_candidate_schema_in_transaction(connection)
        existing = self._existing_result_acceptance_graph_candidate_in_transaction(
            connection,
            prepared,
        )
        if existing is not None:
            self._require_current_process()
            return existing
        fresh = self._fresh_result_acceptance_prerequisites_in_transaction(
            connection,
            prepared,
        )
        self._require_current_process()
        return fresh

    @_bind_event_store_process
    def _preflight_result_acceptance_write_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _FreshResultAcceptanceWritePlanV2
    ]:
        """Create no durable prefix; yield existing candidate or one fresh opaque plan."""

        return self._preflight_result_acceptance_write_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _preflight_result_acceptance_write_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[_ExistingResultAcceptanceGraphCandidateV2 | _FreshResultAcceptanceWritePlanV2]:
        connection = self._connection_for_result_artifact_transaction(handle)
        prerequisites = self._validate_result_acceptance_durable_prerequisites_in_transaction(
            connection,
            prepared,
        )
        if type(prerequisites) is _ExistingResultAcceptanceGraphCandidateV2:
            yield prerequisites
            return
        if type(prerequisites) is not _FreshResultAcceptancePrerequisitesV2:
            raise RuntimeError("result acceptance prerequisite classification is not closed")
        with _preflight_prepared_result_artifacts_in_transaction(
            connection,
            prepared.artifact_batch,
            process_guard=self._require_current_process,
            savepoint_suffix=self._savepoint_suffix_for_result_artifact_transaction(handle),
        ) as artifact_plan:
            plan = _FreshResultAcceptanceWritePlanV2(
                prepared=prepared,
                prerequisites=prerequisites,
                artifact_plan=artifact_plan,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            try:
                yield plan
            finally:
                plan._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @_bind_event_store_process
    def _consume_result_acceptance_artifact_plan_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        plan: _FreshResultAcceptanceWritePlanV2,
    ) -> Tuple[str, Tuple[_ScopedInvocationResultArtifactV2, ...]]:
        """Sample acceptedAt once, reject an expired lease, then consume Artifact heads."""

        connection: Optional[sqlite3.Connection] = None
        try:
            connection = self._connection_for_result_artifact_transaction(handle)
            if type(plan) is not _FreshResultAcceptanceWritePlanV2:
                raise TypeError("result acceptance Artifact materialization requires an exact plan")
            prepared, prerequisites, artifact_plan = plan._begin_artifact_materialization(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            before_clock_changes = connection.total_changes
            if type(before_clock_changes) is not int or before_clock_changes < 0:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance SQLite change counter is invalid"
                )
            self._require_current_process()
            accepted_at = _normalize_invocation_timestamp(
                self._clock(),
                "result acceptance clock",
            )
            self._require_current_process()
            after_clock_changes = connection.total_changes
            if type(after_clock_changes) is not int or after_clock_changes != before_clock_changes:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance clock changed durable state"
                )
            if accepted_at < prerequisites.heartbeat_at:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance clock precedes durable lease activity"
                )
            if accepted_at >= prerequisites.lease_expires_at:
                raise _ResultAcceptanceConflictError(
                    "result acceptance lease expired before acceptedAt"
                )
            artifacts = _materialize_prepared_result_artifacts_in_transaction(
                artifact_plan,
                accepted_at,
            )
            if artifacts != tuple(item.descriptor for item in prepared.artifact_batch.items):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance materialized Artifact order changed"
                )
            self._require_current_process()
            return accepted_at, artifacts
        except BaseException as error:
            if self._process_is_current():
                continuity_control: Optional[_EventStoreControlDescriptor] = None
                continuity_graph: Tuple[BaseException, ...] = ()
                if isinstance(error, _ResultArtifactTransactionContinuityError):
                    self._poisoned = True
                    continuity_control, continuity_graph = _result_artifact_continuity_control(
                        error
                    )
                if type(connection) is sqlite3.Connection:
                    try:
                        transaction_open = connection.in_transaction
                    except BaseException:
                        self._poisoned = True
                    else:
                        if type(transaction_open) is not bool or not transaction_open:
                            self._poisoned = True
                generation = self._active_result_artifact_transaction_generation
                if type(generation) is int and generation > 0:
                    self._result_artifact_transaction_rollback_only = True
                if continuity_control is not None:
                    for graph_error in continuity_graph:
                        _detach_exception(graph_error)
                    raise _EventStoreAdmissionTransactionSignal(
                        "ambiguous",
                        control=continuity_control,
                        token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                    ) from None
            raise

    @_bind_event_store_process
    def _materialize_result_acceptance_artifacts_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _MaterializedFreshResultAcceptancePlanV2
    ]:
        """Materialize fresh Artifacts, then revalidate every durable prerequisite."""

        return self._materialize_result_acceptance_artifacts_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _materialize_result_acceptance_artifacts_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _MaterializedFreshResultAcceptancePlanV2
    ]:
        connection: Optional[sqlite3.Connection] = None
        materialized: Optional[_MaterializedFreshResultAcceptancePlanV2] = None
        try:
            connection = self._connection_for_result_artifact_transaction(handle)
            prerequisites: Optional[_FreshResultAcceptancePrerequisitesV2] = None
            accepted_at: Optional[str] = None
            artifacts: Optional[Tuple[_ScopedInvocationResultArtifactV2, ...]] = None
            with self._preflight_result_acceptance_write_in_owner_transaction(
                handle,
                prepared,
            ) as candidate:
                if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                    yield candidate
                    return
                if type(candidate) is not _FreshResultAcceptanceWritePlanV2:
                    raise RuntimeError("result acceptance Artifact classification is not closed")
                _, prerequisites, _ = candidate._validated(
                    token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                )
                accepted_at, artifacts = (
                    self._consume_result_acceptance_artifact_plan_in_owner_transaction(
                        handle,
                        candidate,
                    )
                )
            if prerequisites is None or accepted_at is None or artifacts is None:
                raise RuntimeError("result acceptance Artifact materialization state is incomplete")
            after_clock = self._validate_result_acceptance_durable_prerequisites_in_transaction(
                connection,
                prepared,
            )
            if type(after_clock) is not _FreshResultAcceptancePrerequisitesV2:
                raise _ResultAcceptanceConflictError(
                    "result acceptance durable graph changed during clock sampling"
                )
            if after_clock != prerequisites:
                raise _ResultAcceptanceConflictError(
                    "result acceptance durable prerequisites changed during clock sampling"
                )
            materialized = _MaterializedFreshResultAcceptancePlanV2(
                prepared=prepared,
                prerequisites=after_clock,
                accepted_at=accepted_at,
                artifacts=artifacts,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            try:
                yield materialized
            finally:
                materialized._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        except BaseException as error:
            if self._process_is_current():
                continuity_control: Optional[_EventStoreControlDescriptor] = None
                continuity_graph: Tuple[BaseException, ...] = ()
                if isinstance(error, _ResultArtifactTransactionContinuityError):
                    self._poisoned = True
                    continuity_control, continuity_graph = _result_artifact_continuity_control(
                        error
                    )
                generation = self._active_result_artifact_transaction_generation
                if type(generation) is int and generation > 0:
                    self._result_artifact_transaction_rollback_only = True
                if type(connection) is sqlite3.Connection:
                    try:
                        transaction_open = connection.in_transaction
                    except BaseException:
                        self._poisoned = True
                    else:
                        if type(transaction_open) is not bool or not transaction_open:
                            self._poisoned = True
                if continuity_control is not None:
                    for graph_error in continuity_graph:
                        _detach_exception(graph_error)
                    raise _EventStoreAdmissionTransactionSignal(
                        "ambiguous",
                        control=continuity_control,
                        token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                    ) from None
            raise

    @_bind_event_store_process
    def _identify_result_acceptance_write_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _IdentifiedFreshResultAcceptancePlanV2
    ]:
        """Allocate three distinct store-owned IDs only for one fresh materialized path."""

        return self._identify_result_acceptance_write_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _identify_result_acceptance_write_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _IdentifiedFreshResultAcceptancePlanV2
    ]:
        connection: Optional[sqlite3.Connection] = None
        materialized: Optional[_MaterializedFreshResultAcceptancePlanV2] = None
        identified: Optional[_IdentifiedFreshResultAcceptancePlanV2] = None
        try:
            connection = self._connection_for_result_artifact_transaction(handle)
            prerequisites: Optional[_FreshResultAcceptancePrerequisitesV2] = None
            accepted_at: Optional[str] = None
            artifacts: Optional[Tuple[_ScopedInvocationResultArtifactV2, ...]] = None
            with self._preflight_result_acceptance_write_in_owner_transaction(
                handle,
                prepared,
            ) as candidate:
                if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                    yield candidate
                    return
                if type(candidate) is not _FreshResultAcceptanceWritePlanV2:
                    raise RuntimeError("result acceptance identity classification is not closed")
                frozen, prerequisites, artifact_plan = candidate._begin_artifact_materialization(
                    token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
                )
                before_dependency_changes = connection.total_changes
                if type(before_dependency_changes) is not int or before_dependency_changes < 0:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance SQLite change counter is invalid"
                    )
                self._require_current_process()
                accepted_at = _normalize_invocation_timestamp(
                    self._clock(),
                    "result acceptance clock",
                )
                self._require_current_process()
                after_clock_changes = connection.total_changes
                if (
                    type(after_clock_changes) is not int
                    or after_clock_changes != before_dependency_changes
                ):
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance clock changed durable state"
                    )
                if accepted_at < prerequisites.heartbeat_at:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance clock precedes durable lease activity"
                    )
                if accepted_at >= prerequisites.lease_expires_at:
                    raise _ResultAcceptanceConflictError(
                        "result acceptance lease expired before acceptedAt"
                    )
                raw_receipt_id = new_id("result_receipt")
                self._require_current_process()
                receipt_id = _caller_invocation_identity(
                    raw_receipt_id,
                    "result receipt ID provider result",
                )
                self._require_current_process()
                raw_result_event_id = new_id("evt")
                self._require_current_process()
                result_event_id = _caller_invocation_identity(
                    raw_result_event_id,
                    "result event ID provider result",
                )
                self._require_current_process()
                raw_terminal_event_id = new_id("evt")
                self._require_current_process()
                terminal_event_id = _caller_invocation_identity(
                    raw_terminal_event_id,
                    "terminal event ID provider result",
                )
                self._require_current_process()
                after_dependency_changes = connection.total_changes
                if (
                    type(after_dependency_changes) is not int
                    or after_dependency_changes != before_dependency_changes
                ):
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance identity provider changed durable state"
                    )
                frozen.verify()
                _FreshResultAcceptancePrerequisitesV2.__post_init__(prerequisites)
                if len({receipt_id, result_event_id, terminal_event_id}) != 3:
                    raise _ResultAcceptanceConflictError(
                        "result acceptance store-generated identities are not distinct"
                    )
                if (
                    result_event_id == frozen.request.start_receipt.event_id
                    or terminal_event_id == frozen.request.start_receipt.event_id
                ):
                    raise _ResultAcceptanceConflictError(
                        "result acceptance event identity reuses the start event"
                    )
                artifacts = _materialize_prepared_result_artifacts_in_transaction(
                    artifact_plan,
                    accepted_at,
                )
                if artifacts != tuple(item.descriptor for item in frozen.artifact_batch.items):
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance materialized Artifact order changed"
                    )
            if prerequisites is None or accepted_at is None or artifacts is None:
                raise RuntimeError(
                    "result acceptance identified materialization state is incomplete"
                )
            after_dependencies = (
                self._validate_result_acceptance_durable_prerequisites_in_transaction(
                    connection,
                    prepared,
                )
            )
            if type(after_dependencies) is not _FreshResultAcceptancePrerequisitesV2:
                raise _ResultAcceptanceConflictError(
                    "result acceptance durable graph changed during identity allocation"
                )
            if after_dependencies != prerequisites:
                raise _ResultAcceptanceConflictError(
                    "result acceptance durable prerequisites changed during identity allocation"
                )
            receipt_collision = connection.execute(
                """
                SELECT 1
                FROM main.invocation_result_receipts
                WHERE receipt_id = ?
                LIMIT 1
                """,
                (receipt_id,),
            ).fetchone()
            event_collision = connection.execute(
                """
                SELECT 1
                FROM main.events
                WHERE event_id IN (?, ?)
                LIMIT 1
                """,
                (result_event_id, terminal_event_id),
            ).fetchone()
            self._require_current_process()
            if receipt_collision is not None or event_collision is not None:
                raise _ResultAcceptanceConflictError(
                    "result acceptance store-generated identity is already durable"
                )
            materialized = _MaterializedFreshResultAcceptancePlanV2(
                prepared=prepared,
                prerequisites=after_dependencies,
                accepted_at=accepted_at,
                artifacts=artifacts,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            materialized._begin_identity_allocation(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            identified = _IdentifiedFreshResultAcceptancePlanV2(
                materialized=materialized,
                receipt_id=receipt_id,
                result_event_id=result_event_id,
                terminal_event_id=terminal_event_id,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            try:
                yield identified
            finally:
                identified._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
                materialized._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        except BaseException as error:
            if self._process_is_current():
                continuity_control: Optional[_EventStoreControlDescriptor] = None
                continuity_graph: Tuple[BaseException, ...] = ()
                if isinstance(error, _ResultArtifactTransactionContinuityError):
                    self._poisoned = True
                    continuity_control, continuity_graph = _result_artifact_continuity_control(
                        error
                    )
                generation = self._active_result_artifact_transaction_generation
                if type(generation) is int and generation > 0:
                    self._result_artifact_transaction_rollback_only = True
                if type(connection) is sqlite3.Connection:
                    try:
                        transaction_open = connection.in_transaction
                    except BaseException:
                        self._poisoned = True
                    else:
                        if type(transaction_open) is not bool or not transaction_open:
                            self._poisoned = True
                if continuity_control is not None:
                    for graph_error in continuity_graph:
                        _detach_exception(graph_error)
                    raise _EventStoreAdmissionTransactionSignal(
                        "ambiguous",
                        control=continuity_control,
                        token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                    ) from None
            raise

    @_bind_event_store_process
    def _construct_result_acceptance_evidence_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _EvidencedFreshResultAcceptancePlanV2
    ]:
        """Construct exact canonical result evidence without appending its event."""

        return self._construct_result_acceptance_evidence_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _construct_result_acceptance_evidence_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _EvidencedFreshResultAcceptancePlanV2
    ]:
        evidenced: Optional[_EvidencedFreshResultAcceptancePlanV2] = None
        with self._identify_result_acceptance_write_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _IdentifiedFreshResultAcceptancePlanV2:
                raise RuntimeError("result acceptance evidence classification is not closed")
            materialized, receipt_id, _, _ = candidate._begin_evidence_construction(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            self._require_current_process()
            evidence = _build_scoped_invocation_result_evidence_v2(
                materialized,
                receipt_id=receipt_id,
            )
            self._require_current_process()
            evidenced = _EvidencedFreshResultAcceptancePlanV2(
                identified=candidate,
                evidence=evidence,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            try:
                yield evidenced
            finally:
                evidenced._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @_bind_event_store_process
    def _construct_result_acceptance_terminal_transition_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _TransitionedFreshResultAcceptancePlanV2
    ]:
        """Construct the exact result-bound terminal payload without appending it."""

        return self._construct_result_acceptance_terminal_transition_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _construct_result_acceptance_terminal_transition_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _TransitionedFreshResultAcceptancePlanV2
    ]:
        transitioned: Optional[_TransitionedFreshResultAcceptancePlanV2] = None
        with self._construct_result_acceptance_evidence_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _EvidencedFreshResultAcceptancePlanV2:
                raise RuntimeError(
                    "result acceptance terminal transition classification is not closed"
                )
            candidate._begin_terminal_transition_construction(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            self._require_current_process()
            terminal_transition = _build_scoped_invocation_result_terminal_transition_from_plan_v2(
                candidate
            )
            self._require_current_process()
            transitioned = _TransitionedFreshResultAcceptancePlanV2(
                evidenced=candidate,
                terminal_transition=terminal_transition,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            self._require_current_process()
            try:
                yield transitioned
            finally:
                transitioned._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @_bind_event_store_process
    def _construct_result_acceptance_event_pair_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _EventedFreshResultAcceptancePlanV2
    ]:
        """Construct the exact canonical event pair without appending either row."""

        return self._construct_result_acceptance_event_pair_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _construct_result_acceptance_event_pair_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[_ExistingResultAcceptanceGraphCandidateV2 | _EventedFreshResultAcceptancePlanV2]:
        evented: Optional[_EventedFreshResultAcceptancePlanV2] = None
        with self._construct_result_acceptance_terminal_transition_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _TransitionedFreshResultAcceptancePlanV2:
                raise RuntimeError("result acceptance event classification is not closed")
            candidate._begin_event_construction(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            self._require_current_process()
            result_event, terminal_event = _build_scoped_invocation_result_events_from_plan_v2(
                candidate
            )
            self._require_current_process()
            evented = _EventedFreshResultAcceptancePlanV2(
                transitioned=candidate,
                result_event=result_event,
                terminal_event=terminal_event,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            self._require_current_process()
            try:
                yield evented
            finally:
                evented._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @_bind_event_store_process
    def _insert_result_acceptance_event_pair_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _InsertedFreshResultAcceptancePlanV2
    ]:
        """Insert and independently verify both canonical events in the owner transaction."""

        return self._insert_result_acceptance_event_pair_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _insert_result_acceptance_event_pair_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[_ExistingResultAcceptanceGraphCandidateV2 | _InsertedFreshResultAcceptancePlanV2]:
        inserted: Optional[_InsertedFreshResultAcceptancePlanV2] = None
        with self._construct_result_acceptance_event_pair_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _EventedFreshResultAcceptancePlanV2:
                raise RuntimeError("result acceptance insertion classification is not closed")
            transitioned_plan, result_event, terminal_event = candidate._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            evidenced_plan, _transition = transitioned_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            identified_plan, _evidence = evidenced_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            materialized, _, _, _ = identified_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            prepared_snapshot, prerequisites, _, _ = materialized._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            connection = self._connection_for_result_artifact_transaction(handle)
            result_snapshot = SQLiteEventStore._snapshot_event(self, result_event)
            result_stored, result_inserted, result_envelope = (
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    self,
                    connection,
                    result_snapshot,
                    prepared_snapshot.request.expected_stream_version,
                )
            )
            if result_inserted is not True:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance result event was not freshly inserted"
                )
            self._require_current_process()
            terminal_snapshot = SQLiteEventStore._snapshot_event(self, terminal_event)
            terminal_stored, terminal_inserted, terminal_envelope = (
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    self,
                    connection,
                    terminal_snapshot,
                    result_stored.sequence,
                )
            )
            if terminal_inserted is not True:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance terminal event was not freshly inserted"
                )
            self._require_current_process()
            if prerequisites.expected_stream_version != result_stored.sequence - 1:
                raise _ResultAcceptanceConflictError(
                    "result acceptance result event sequence changed during insertion"
                )
            inserted = _InsertedFreshResultAcceptancePlanV2(
                evented=candidate,
                result_stored=result_stored,
                result_envelope=result_envelope,
                terminal_stored=terminal_stored,
                terminal_envelope=terminal_envelope,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            self._require_current_process()
            try:
                yield inserted
            finally:
                inserted._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @_bind_event_store_process
    def _construct_result_acceptance_receipt_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _ReceiptedFreshResultAcceptancePlanV2
    ]:
        """Construct one exact receipt from the freshly verified event pair."""

        return self._construct_result_acceptance_receipt_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _construct_result_acceptance_receipt_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _ReceiptedFreshResultAcceptancePlanV2
    ]:
        receipted: Optional[_ReceiptedFreshResultAcceptancePlanV2] = None
        with self._insert_result_acceptance_event_pair_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _InsertedFreshResultAcceptancePlanV2:
                raise RuntimeError("result acceptance receipt classification is not closed")
            evented, result_stored, result_envelope, terminal_stored, terminal_envelope = (
                candidate._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            )
            transitioned_plan, _result_event, _terminal_event = evented._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            evidenced_plan, terminal_transition = transitioned_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            identified, evidence = evidenced_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            materialized, _, _, _ = identified._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            prepared_snapshot, _, _, _ = materialized._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            result_coordinates = _ScopedInvocationResultEventCoordinatesV2(
                event_id=result_stored.event.event_id,
                stream_id=result_stored.event.stream_id,
                event_type=result_stored.event.event_type,
                sequence=result_stored.sequence,
                global_position=result_stored.global_position,
                event_envelope_digest=_StoredEventEnvelopeV1.digest(result_envelope),
            )
            terminal_coordinates = _ScopedInvocationResultEventCoordinatesV2(
                event_id=terminal_stored.event.event_id,
                stream_id=terminal_stored.event.stream_id,
                event_type=terminal_stored.event.event_type,
                sequence=terminal_stored.sequence,
                global_position=terminal_stored.global_position,
                event_envelope_digest=_StoredEventEnvelopeV1.digest(terminal_envelope),
            )
            receipt = _build_scoped_invocation_result_receipt_v2(
                prepared_snapshot.request,
                evidence,
                result_event=result_coordinates,
                terminal_event=terminal_coordinates,
                terminal_transition=terminal_transition,
            )
            self._require_current_process()
            receipted = _ReceiptedFreshResultAcceptancePlanV2(
                inserted=candidate,
                receipt=receipt,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            self._require_current_process()
            try:
                yield receipted
            finally:
                receipted._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _insert_exact_result_acceptance_row_in_owner_transaction(
        self,
        connection: sqlite3.Connection,
        sql: str,
        values: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        """Require one top-level INSERT with no trigger or auxiliary row side effects."""

        self._require_current_process()
        if type(connection) is not sqlite3.Connection or connection is not self._connection:
            raise RuntimeError("result acceptance row insertion requires the owning connection")
        if type(sql) is not str or not sql or type(values) is not tuple:
            raise TypeError("result acceptance row insertion inputs are not exact")
        before = connection.total_changes
        if type(before) is not int or before < 0:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance SQLite change counter is invalid"
            )
        cursor = connection.execute(sql, values)
        self._require_current_process()
        after = connection.total_changes
        if (
            type(cursor) is not sqlite3.Cursor
            or type(cursor.rowcount) is not int
            or cursor.rowcount != 1
            or type(after) is not int
            or after != before + 1
        ):
            raise _ResultAcceptanceIntegrityError(
                f"result acceptance {label} insertion changed an unexpected row count"
            )

    def _update_exact_result_acceptance_row_in_owner_transaction(
        self,
        connection: sqlite3.Connection,
        sql: str,
        values: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        """Require one owner-scoped CAS UPDATE with no trigger or auxiliary row side effects."""

        self._require_current_process()
        if type(connection) is not sqlite3.Connection or connection is not self._connection:
            raise RuntimeError("result acceptance row update requires the owning connection")
        if type(sql) is not str or not sql or type(values) is not tuple:
            raise TypeError("result acceptance row update inputs are not exact")
        before = connection.total_changes
        if type(before) is not int or before < 0:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance SQLite change counter is invalid"
            )
        cursor = connection.execute(sql, values)
        self._require_current_process()
        after = connection.total_changes
        if (
            type(cursor) is not sqlite3.Cursor
            or type(cursor.rowcount) is not int
            or cursor.rowcount != 1
            or type(after) is not int
            or after != before + 1
        ):
            raise _ResultAcceptanceIntegrityError(
                f"result acceptance {label} update changed an unexpected row count"
            )

    @_bind_event_store_process
    def _persist_result_acceptance_graph_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _PersistedFreshResultAcceptancePlanV2
    ]:
        """Persist manifest, request, receipt, event bindings and Artifact bindings."""

        return self._persist_result_acceptance_graph_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _persist_result_acceptance_graph_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _PersistedFreshResultAcceptancePlanV2
    ]:
        persisted: Optional[_PersistedFreshResultAcceptancePlanV2] = None
        with self._construct_result_acceptance_receipt_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _ReceiptedFreshResultAcceptancePlanV2:
                raise RuntimeError("result acceptance persistence classification is not closed")
            inserted_plan, receipt = candidate._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            evented_plan, _, _, _, _ = inserted_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            transitioned_plan, _, _ = evented_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            evidenced_plan, _ = transitioned_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            identified_plan, evidence = evidenced_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            materialized_plan, _, _, _ = identified_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            prepared_snapshot, prerequisites, accepted_at, artifacts = materialized_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            request = prepared_snapshot.request
            manifest = request.manifest
            if receipt.evidence != evidence or receipt.evidence.accepted_at != accepted_at:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance receipt differs from its materialized evidence"
                )
            if tuple(artifacts) != manifest.artifacts:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance materialized Artifacts differ from the manifest"
                )
            connection = self._connection_for_result_artifact_transaction(handle)
            manifest_bytes = _ScopedInvocationResultManifestV2.canonical_bytes(manifest)
            manifest_digest = _ScopedInvocationResultManifestV2.canonical_digest(manifest)
            request_identity_bytes = json.dumps(
                _ScopedInvocationResultAcceptanceRequestV2._identity_dict(request),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            request_digest = _ScopedInvocationResultAcceptanceRequestV2.canonical_digest(request)
            self._insert_exact_result_acceptance_row_in_owner_transaction(
                connection,
                """
                INSERT INTO invocation_result_manifests (
                    tenant_id, workspace_id, manifest_digest, schema_version,
                    canonical_bytes, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.tenant_id,
                    manifest.workspace_id,
                    manifest_digest,
                    manifest.schema_version,
                    sqlite3.Binary(manifest_bytes),
                    len(manifest_bytes),
                    accepted_at,
                ),
                label="manifest",
            )
            self._insert_exact_result_acceptance_row_in_owner_transaction(
                connection,
                """
                INSERT INTO invocation_result_requests (
                    tenant_id, workspace_id, request_digest, schema_version,
                    acceptance_idempotency_key, request_identity_bytes,
                    request_identity_byte_size, invocation_id, session_id, plan_id,
                    task_id, agent_id, job_idempotency_key, start_receipt_digest,
                    execution_manifest_digest, result_manifest_digest,
                    expected_stream_version, running_task_revision,
                    terminal_task_revision, correlation_id, causation_id,
                    runtime_revision, effect_class, action_receipt_set_digest,
                    result_ref, primary_artifact_id, artifact_count, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    manifest.tenant_id,
                    manifest.workspace_id,
                    request_digest,
                    request.schema_version,
                    request.acceptance_idempotency_key,
                    sqlite3.Binary(request_identity_bytes),
                    len(request_identity_bytes),
                    manifest.invocation_id,
                    manifest.session_id,
                    manifest.plan_id,
                    manifest.task_id,
                    manifest.agent_id,
                    manifest.job_idempotency_key,
                    request.start_receipt_digest,
                    manifest.execution_manifest_digest,
                    manifest_digest,
                    request.expected_stream_version,
                    evidence.running_task_revision,
                    evidence.terminal_task_revision,
                    manifest.correlation_id,
                    manifest.causation_id,
                    manifest.runtime_revision,
                    manifest.effect_class.value,
                    manifest.action_receipt_set_digest,
                    manifest.result_ref,
                    manifest.primary_artifact_id,
                    len(artifacts),
                    accepted_at,
                ),
                label="request",
            )
            for event_role, coordinates in (
                ("result", receipt.result_event),
                ("terminal", receipt.terminal_event),
            ):
                self._insert_exact_result_acceptance_row_in_owner_transaction(
                    connection,
                    """
                    INSERT INTO invocation_result_event_bindings (
                        tenant_id, workspace_id, receipt_id, event_role,
                        event_id, event_type, global_position
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.tenant_id,
                        evidence.workspace_id,
                        receipt.receipt_id,
                        event_role,
                        coordinates.event_id,
                        coordinates.event_type,
                        coordinates.global_position,
                    ),
                    label=f"{event_role} event binding",
                )
            terminal_transition = receipt.terminal_transition
            self._insert_exact_result_acceptance_row_in_owner_transaction(
                connection,
                """
                INSERT INTO invocation_result_receipts (
                    tenant_id, workspace_id, receipt_id, schema_version,
                    request_digest, invocation_id, session_id, plan_id, task_id,
                    agent_id, job_idempotency_key, acceptance_idempotency_key,
                    attempt_id, attempt_number, lease_epoch, worker_id,
                    lease_token_digest, start_receipt_digest,
                    execution_manifest_digest, result_manifest_schema_version,
                    result_manifest_digest, result_ref, effect_class,
                    action_receipt_set_digest, expected_stream_version,
                    running_task_revision, terminal_task_revision, accepted_at,
                    artifact_count, result_evidence_digest,
                    terminal_transition_digest, receipt_digest, result_event_id,
                    result_event_stream_id, result_event_type,
                    result_event_timestamp, result_event_sequence,
                    result_event_global_position, result_event_envelope_digest,
                    terminal_event_id, terminal_event_stream_id,
                    terminal_event_type, terminal_event_timestamp,
                    terminal_event_sequence, terminal_event_global_position,
                    terminal_event_envelope_digest
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    evidence.tenant_id,
                    evidence.workspace_id,
                    receipt.receipt_id,
                    receipt.schema_version,
                    evidence.request_digest,
                    evidence.invocation_id,
                    evidence.session_id,
                    evidence.plan_id,
                    evidence.task_id,
                    evidence.agent_id,
                    evidence.job_idempotency_key,
                    evidence.acceptance_idempotency_key,
                    evidence.attempt_id,
                    evidence.attempt_number,
                    evidence.lease_epoch,
                    evidence.worker_id,
                    evidence.lease_token_digest,
                    evidence.start_receipt_digest,
                    evidence.execution_manifest_digest,
                    evidence.result_manifest_schema_version,
                    evidence.result_manifest_digest,
                    evidence.result_ref,
                    evidence.effect_class.value,
                    evidence.action_receipt_set_digest,
                    request.expected_stream_version,
                    evidence.running_task_revision,
                    evidence.terminal_task_revision,
                    accepted_at,
                    evidence.artifact_count,
                    _ScopedInvocationResultEvidenceV2.canonical_digest(evidence),
                    _ScopedInvocationResultTerminalTransitionV2.canonical_digest(
                        terminal_transition
                    ),
                    receipt.receipt_digest,
                    receipt.result_event.event_id,
                    receipt.result_event.stream_id,
                    receipt.result_event.event_type,
                    accepted_at,
                    receipt.result_event.sequence,
                    receipt.result_event.global_position,
                    receipt.result_event.event_envelope_digest,
                    receipt.terminal_event.event_id,
                    receipt.terminal_event.stream_id,
                    receipt.terminal_event.event_type,
                    accepted_at,
                    receipt.terminal_event.sequence,
                    receipt.terminal_event.global_position,
                    receipt.terminal_event.event_envelope_digest,
                ),
                label="receipt",
            )
            if len(request.artifact_candidates) != len(artifacts):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance Artifact candidate count changed during persistence"
                )
            for ordinal, (artifact_candidate, artifact) in enumerate(
                zip(request.artifact_candidates, artifacts)
            ):
                if type(artifact_candidate) is not _ScopedInvocationResultArtifactCandidateV2:
                    raise TypeError("result acceptance Artifact candidate is not exact")
                if type(artifact) is not _ScopedInvocationResultArtifactV2:
                    raise TypeError("result acceptance Artifact descriptor is not exact")
                if artifact_candidate.to_descriptor() != artifact:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance Artifact candidate differs from its descriptor"
                    )
                self._insert_exact_result_acceptance_row_in_owner_transaction(
                    connection,
                    """
                    INSERT INTO invocation_result_artifacts (
                        tenant_id, workspace_id, receipt_id, ordinal, session_id,
                        task_id, artifact_id, name, version, parent_version,
                        media_type, blob_digest, byte_size, metadata_digest,
                        created_by, idempotency_key, artifact_request_digest,
                        candidate_digest
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        artifact_candidate.tenant_id,
                        artifact_candidate.workspace_id,
                        receipt.receipt_id,
                        ordinal,
                        artifact_candidate.session_id,
                        artifact_candidate.task_id,
                        artifact.artifact_id,
                        artifact.name,
                        artifact.version,
                        artifact.parent_version,
                        artifact.media_type,
                        artifact.blob_digest,
                        artifact.byte_size,
                        artifact.metadata_digest,
                        artifact.created_by,
                        artifact.idempotency_key,
                        artifact.request_digest,
                        _ScopedInvocationResultArtifactCandidateV2.canonical_digest(
                            artifact_candidate
                        ),
                    ),
                    label=f"Artifact binding {ordinal}",
                )
            self._require_current_process()
            if prerequisites.request_digest != request_digest:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance request digest changed during persistence"
                )
            persisted = _PersistedFreshResultAcceptancePlanV2(
                receipted=candidate,
                receipt=receipt,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            self._require_current_process()
            try:
                yield persisted
            finally:
                persisted._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    @_bind_event_store_process
    def _readback_result_acceptance_graph_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
        receipt: _ScopedInvocationResultReceiptV2,
    ) -> ContextManager[_ReadbackFreshResultAcceptancePlanV2]:
        """Re-read every fresh result graph row before the owner transaction can commit."""

        return self._readback_result_acceptance_graph_in_owner_transaction_inner(
            handle,
            prepared,
            receipt,
        )

    @contextmanager
    def _readback_result_acceptance_graph_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
        receipt: _ScopedInvocationResultReceiptV2,
    ) -> Iterator[_ReadbackFreshResultAcceptancePlanV2]:
        connection = self._connection_for_result_artifact_transaction(handle)
        if type(prepared) is not _PreparedScopedInvocationResultAcceptanceV2:
            raise TypeError("result acceptance readback requires exact prepared inputs")
        if type(receipt) is not _ScopedInvocationResultReceiptV2:
            raise TypeError("result acceptance readback requires an exact receipt")
        prepared.verify()
        receipt_snapshot = _ScopedInvocationResultReceiptV2.from_dict(
            _ScopedInvocationResultReceiptV2.to_dict(receipt)
        )
        plan: Optional[_ReadbackFreshResultAcceptancePlanV2] = None
        before_changes = connection.total_changes
        if type(before_changes) is not int or before_changes < 0:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback SQLite change counter is invalid"
            )
        try:
            self._readback_result_acceptance_graph_body(
                connection,
                prepared,
                receipt_snapshot,
            )
            self._require_current_process()
            after_changes = connection.total_changes
            if type(after_changes) is not int or after_changes != before_changes:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback changed durable state"
                )
            if not connection.in_transaction:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback lost the owner transaction"
                )
            plan = _ReadbackFreshResultAcceptancePlanV2(
                receipt=receipt_snapshot,
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
            )
            try:
                yield plan
            finally:
                plan._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
        except _ResultAcceptanceQuarantineError:
            raise
        except _ResultAcceptanceIntegrityError as error:
            raise _ResultAcceptanceQuarantineError(
                str(error),
                category=_ResultAcceptanceQuarantineCategory.DRIFT,
            ) from error
        except (InvocationIntegrityError, sqlite3.Error, TypeError, ValueError) as error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance complete graph readback failed"
            ) from error

    def _readback_result_acceptance_graph_body(
        self,
        connection: sqlite3.Connection,
        prepared: _PreparedScopedInvocationResultAcceptanceV2 | _ResultAcceptanceObservationInputV2,
        receipt: _ScopedInvocationResultReceiptV2,
    ) -> None:
        """Fixed-projection, read-only verification for one just-written fresh graph."""

        self._require_current_process()
        request = prepared.request
        manifest = request.manifest
        evidence = receipt.evidence
        transition = receipt.terminal_transition
        if receipt.start_receipt != request.start_receipt:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback start receipt differs from request"
            )

        def one_row(
            sql: str,
            parameters: tuple[object, ...],
            columns: tuple[str, ...],
            label: str,
        ) -> sqlite3.Row:
            self._require_current_process()
            rows = connection.execute(sql, parameters).fetchall()
            self._require_current_process()
            if type(rows) is not list or len(rows) != 1:
                raise _ResultAcceptanceIntegrityError(
                    f"result acceptance readback {label} row count is not one"
                )
            return _result_readback_row(rows[0], columns, label)

        def compare_text(row: sqlite3.Row, name: str, expected: object) -> None:
            actual = _persisted_text(row[name], f"result {name}", required=expected is not None)
            if actual != expected:
                raise _ResultAcceptanceIntegrityError(f"result acceptance readback {name} differs")

        def compare_digest(row: sqlite3.Row, name: str, expected: object) -> None:
            actual = _persisted_result_acceptance_digest(row[name], f"result {name}")
            if actual != expected:
                raise _ResultAcceptanceIntegrityError(
                    f"result acceptance readback {name} digest differs"
                )

        def compare_integer(
            row: sqlite3.Row, name: str, expected: object, *, minimum: int = 0
        ) -> None:
            actual = _persisted_integer(row[name], f"result {name}", minimum=minimum)
            if actual != expected:
                raise _ResultAcceptanceIntegrityError(f"result acceptance readback {name} differs")

        def compare_optional_text(row: sqlite3.Row, name: str, expected: object) -> None:
            actual = _persisted_optional_text(row[name], f"result {name}")
            if actual != expected:
                raise _ResultAcceptanceIntegrityError(f"result acceptance readback {name} differs")

        accepted_at = _result_readback_timestamp(evidence.accepted_at, "accepted_at")
        manifest_digest = manifest.canonical_digest()
        manifest_bytes = manifest.canonical_bytes()
        manifest_row = one_row(
            """
            SELECT tenant_id, workspace_id, manifest_digest, schema_version,
                   canonical_bytes, byte_size, created_at
            FROM main.invocation_result_manifests
            WHERE tenant_id = ? AND workspace_id = ? AND manifest_digest = ?
            """,
            (manifest.tenant_id, manifest.workspace_id, manifest_digest),
            _RESULT_READBACK_MANIFEST_COLUMNS,
            "manifest",
        )
        compare_text(manifest_row, "tenant_id", manifest.tenant_id)
        compare_text(manifest_row, "workspace_id", manifest.workspace_id)
        compare_digest(manifest_row, "manifest_digest", manifest_digest)
        compare_integer(manifest_row, "schema_version", manifest.schema_version)
        canonical_manifest = manifest_row["canonical_bytes"]
        if type(canonical_manifest) is not bytes or canonical_manifest != manifest_bytes:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback manifest canonical bytes differ"
            )
        compare_integer(manifest_row, "byte_size", len(manifest_bytes), minimum=1)
        if (
            _result_readback_timestamp(manifest_row["created_at"], "manifest created_at")
            != accepted_at
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback manifest timestamp differs"
            )
        try:
            decoded_manifest = _ScopedInvocationResultManifestV2.from_dict(
                json.loads(canonical_manifest.decode("utf-8"), parse_constant=_reject_json_constant)
            )
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback manifest cannot be decoded"
            ) from error
        if decoded_manifest != manifest or decoded_manifest.canonical_bytes() != canonical_manifest:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback manifest decode differs"
            )

        request_digest = request.canonical_digest()
        request_identity_bytes = json.dumps(
            _ScopedInvocationResultAcceptanceRequestV2._identity_dict(request),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request_row = one_row(
            """
            SELECT tenant_id, workspace_id, request_digest, schema_version,
                   acceptance_idempotency_key, request_identity_bytes,
                   request_identity_byte_size, invocation_id, session_id, plan_id,
                   task_id, agent_id, job_idempotency_key, start_receipt_digest,
                   execution_manifest_digest, result_manifest_digest,
                   expected_stream_version, running_task_revision,
                   terminal_task_revision, correlation_id, causation_id,
                   runtime_revision, effect_class, action_receipt_set_digest,
                   result_ref, primary_artifact_id, artifact_count, created_at
            FROM main.invocation_result_requests
            WHERE tenant_id = ? AND workspace_id = ? AND request_digest = ?
            """,
            (manifest.tenant_id, manifest.workspace_id, request_digest),
            _RESULT_READBACK_REQUEST_COLUMNS,
            "request",
        )
        for name, expected in (
            ("tenant_id", manifest.tenant_id),
            ("workspace_id", manifest.workspace_id),
            ("acceptance_idempotency_key", request.acceptance_idempotency_key),
            ("invocation_id", manifest.invocation_id),
            ("session_id", manifest.session_id),
            ("plan_id", manifest.plan_id),
            ("task_id", manifest.task_id),
            ("agent_id", manifest.agent_id),
            ("job_idempotency_key", manifest.job_idempotency_key),
            ("correlation_id", manifest.correlation_id),
            ("causation_id", manifest.causation_id),
            ("runtime_revision", manifest.runtime_revision),
            ("effect_class", manifest.effect_class.value),
            ("result_ref", manifest.result_ref),
        ):
            compare_text(request_row, name, expected)
        for name, expected in (
            ("request_digest", request_digest),
            ("start_receipt_digest", request.start_receipt_digest),
            ("execution_manifest_digest", manifest.execution_manifest_digest),
            ("result_manifest_digest", manifest_digest),
            ("action_receipt_set_digest", manifest.action_receipt_set_digest),
        ):
            compare_digest(request_row, name, expected)
        if type(request_row["request_identity_bytes"]) is not bytes:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback request identity bytes are not BLOB"
            )
        if request_row["request_identity_bytes"] != request_identity_bytes:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback request identity bytes differ"
            )
        compare_integer(
            request_row, "request_identity_byte_size", len(request_identity_bytes), minimum=1
        )
        compare_integer(request_row, "schema_version", request.schema_version)
        compare_integer(
            request_row, "expected_stream_version", request.expected_stream_version, minimum=1
        )
        compare_integer(
            request_row, "running_task_revision", evidence.running_task_revision, minimum=1
        )
        compare_integer(
            request_row, "terminal_task_revision", evidence.terminal_task_revision, minimum=1
        )
        compare_optional_text(request_row, "primary_artifact_id", manifest.primary_artifact_id)
        compare_integer(request_row, "artifact_count", len(manifest.artifacts))
        if (
            _result_readback_timestamp(request_row["created_at"], "request created_at")
            != accepted_at
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback request timestamp differs"
            )

        receipt_row = one_row(
            """
            SELECT tenant_id, workspace_id, receipt_id, schema_version,
                   request_digest, invocation_id, session_id, plan_id, task_id,
                   agent_id, job_idempotency_key, acceptance_idempotency_key,
                   attempt_id, attempt_number, lease_epoch, worker_id,
                   lease_token_digest, start_receipt_digest,
                   execution_manifest_digest, result_manifest_schema_version,
                   result_manifest_digest, result_ref, effect_class,
                   action_receipt_set_digest, expected_stream_version,
                   running_task_revision, terminal_task_revision, accepted_at,
                   artifact_count, result_evidence_digest,
                   terminal_transition_digest, receipt_digest, result_event_id,
                   result_event_stream_id, result_event_type,
                   result_event_timestamp, result_event_sequence,
                   result_event_global_position, result_event_envelope_digest,
                   terminal_event_id, terminal_event_stream_id,
                   terminal_event_type, terminal_event_timestamp,
                   terminal_event_sequence, terminal_event_global_position,
                   terminal_event_envelope_digest
            FROM main.invocation_result_receipts
            WHERE tenant_id = ? AND workspace_id = ? AND receipt_id = ?
            """,
            (evidence.tenant_id, evidence.workspace_id, receipt.receipt_id),
            _RESULT_READBACK_RECEIPT_COLUMNS,
            "receipt",
        )
        for name, expected in (
            ("tenant_id", evidence.tenant_id),
            ("workspace_id", evidence.workspace_id),
            ("receipt_id", receipt.receipt_id),
            ("invocation_id", evidence.invocation_id),
            ("session_id", evidence.session_id),
            ("plan_id", evidence.plan_id),
            ("task_id", evidence.task_id),
            ("agent_id", evidence.agent_id),
            ("job_idempotency_key", evidence.job_idempotency_key),
            ("acceptance_idempotency_key", evidence.acceptance_idempotency_key),
            ("attempt_id", evidence.attempt_id),
            ("worker_id", evidence.worker_id),
            ("result_ref", evidence.result_ref),
            ("effect_class", evidence.effect_class.value),
        ):
            compare_text(receipt_row, name, expected)
        for name, expected in (
            ("request_digest", evidence.request_digest),
            ("lease_token_digest", evidence.lease_token_digest),
            ("start_receipt_digest", evidence.start_receipt_digest),
            ("execution_manifest_digest", evidence.execution_manifest_digest),
            ("result_manifest_digest", evidence.result_manifest_digest),
            ("action_receipt_set_digest", evidence.action_receipt_set_digest),
            ("result_evidence_digest", evidence.canonical_digest()),
            ("terminal_transition_digest", transition.canonical_digest()),
            ("receipt_digest", receipt.receipt_digest),
            ("result_event_envelope_digest", receipt.result_event.event_envelope_digest),
            ("terminal_event_envelope_digest", receipt.terminal_event.event_envelope_digest),
        ):
            compare_digest(receipt_row, name, expected)
        for integer_name, integer_expected in (
            ("schema_version", receipt.schema_version),
            ("result_manifest_schema_version", manifest.schema_version),
            ("expected_stream_version", request.expected_stream_version),
            ("running_task_revision", evidence.running_task_revision),
            ("terminal_task_revision", evidence.terminal_task_revision),
            ("artifact_count", evidence.artifact_count),
            ("attempt_number", evidence.attempt_number),
            ("lease_epoch", evidence.lease_epoch),
            ("result_event_sequence", receipt.result_event.sequence),
            ("result_event_global_position", receipt.result_event.global_position),
            ("terminal_event_sequence", receipt.terminal_event.sequence),
            ("terminal_event_global_position", receipt.terminal_event.global_position),
        ):
            compare_integer(
                receipt_row,
                integer_name,
                integer_expected,
                minimum=0 if integer_name == "artifact_count" else 1,
            )
        if (
            _result_readback_timestamp(receipt_row["accepted_at"], "receipt accepted_at")
            != accepted_at
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback receipt timestamp differs"
            )
        if (
            _result_readback_timestamp(
                receipt_row["result_event_timestamp"], "result event timestamp"
            )
            != accepted_at
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback result event timestamp differs"
            )
        if (
            _result_readback_timestamp(
                receipt_row["terminal_event_timestamp"], "terminal event timestamp"
            )
            != accepted_at
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback terminal event timestamp differs"
            )
        for name, expected in (
            ("result_event_id", receipt.result_event.event_id),
            ("result_event_stream_id", receipt.result_event.stream_id),
            ("result_event_type", receipt.result_event.event_type),
            ("terminal_event_id", receipt.terminal_event.event_id),
            ("terminal_event_stream_id", receipt.terminal_event.stream_id),
            ("terminal_event_type", receipt.terminal_event.event_type),
        ):
            compare_text(receipt_row, name, expected)

        binding_rows = connection.execute(
            """
            SELECT tenant_id, workspace_id, receipt_id, event_role,
                   event_id, event_type, global_position
            FROM main.invocation_result_event_bindings
            WHERE tenant_id = ? AND workspace_id = ? AND receipt_id = ?
            ORDER BY event_role
            """,
            (evidence.tenant_id, evidence.workspace_id, receipt.receipt_id),
        ).fetchall()
        if type(binding_rows) is not list or len(binding_rows) != 2:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback event binding count differs"
            )
        expected_bindings = {
            "result": receipt.result_event,
            "terminal": receipt.terminal_event,
        }
        seen_roles: set[str] = set()
        for raw_binding in binding_rows:
            row = _result_readback_row(
                raw_binding, _RESULT_READBACK_BINDING_COLUMNS, "event binding"
            )
            role = _persisted_text(row["event_role"], "result event role", required=True)
            if role in seen_roles or role not in expected_bindings:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback event binding role differs"
                )
            seen_roles.add(role)
            coordinate = expected_bindings[role]
            for name, expected in (
                ("tenant_id", evidence.tenant_id),
                ("workspace_id", evidence.workspace_id),
                ("receipt_id", receipt.receipt_id),
                ("event_id", coordinate.event_id),
                ("event_type", coordinate.event_type),
            ):
                compare_text(row, name, expected)
            compare_integer(row, "global_position", coordinate.global_position, minimum=1)
        if seen_roles != set(expected_bindings):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback event binding roles are incomplete"
            )

        event_rows = connection.execute(
            """
            SELECT global_position, stream_id, sequence, event_id, event_type,
                   actor_id, timestamp, payload_json, correlation_id,
                   causation_id, idempotency_key
            FROM main.events
            WHERE event_id IN (?, ?)
            ORDER BY global_position
            """,
            (receipt.result_event.event_id, receipt.terminal_event.event_id),
        ).fetchall()
        if type(event_rows) is not list or len(event_rows) != 2:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback event row count differs"
            )
        observed_event_ids: list[str] = []
        for raw_event in event_rows:
            event_row = _result_readback_row(raw_event, _RESULT_READBACK_EVENT_COLUMNS, "event")
            try:
                envelope = _stored_event_envelope_from_raw_row(event_row)
                envelope_body = _StoredEventEnvelopeV1.to_dict(envelope)
            except (_StoredEventEnvelopeError, TypeError, ValueError) as error:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback event envelope is invalid"
                ) from error
            event_id = envelope_body["eventId"]
            observed_event_ids.append(event_id)
            if event_id == receipt.result_event.event_id:
                coordinate = receipt.result_event
                expected_values = {
                    "event_id": coordinate.event_id,
                    "stream_id": coordinate.stream_id,
                    "event_type": coordinate.event_type,
                    "actor_id": CANONICAL_ORCHESTRATOR_ACTOR_ID,
                    "timestamp": accepted_at,
                    "correlation_id": manifest.correlation_id,
                    "causation_id": request.start_receipt.event_id,
                    "idempotency_key": request.acceptance_idempotency_key,
                    "payload_json": evidence.canonical_bytes().decode("utf-8"),
                    "sequence": coordinate.sequence,
                    "global_position": coordinate.global_position,
                }
                try:
                    decoded_payload = _ScopedInvocationResultEvidenceV2.from_dict(
                        envelope_body["payload"]
                    )
                except (TypeError, ValueError) as error:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance readback result evidence is invalid"
                    ) from error
                if decoded_payload != evidence:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance readback result evidence differs"
                    )
            elif event_id == receipt.terminal_event.event_id:
                coordinate = receipt.terminal_event
                expected_values = {
                    "event_id": coordinate.event_id,
                    "stream_id": coordinate.stream_id,
                    "event_type": coordinate.event_type,
                    "actor_id": transition.actor_id,
                    "timestamp": accepted_at,
                    "correlation_id": transition.correlation_id,
                    "causation_id": transition.causation_id,
                    "idempotency_key": transition.idempotency_key,
                    "payload_json": transition.canonical_bytes().decode("utf-8"),
                    "sequence": coordinate.sequence,
                    "global_position": coordinate.global_position,
                }
                try:
                    decoded_terminal_payload = (
                        _ScopedInvocationResultTerminalTransitionV2.from_dict(
                            envelope_body["payload"]
                        )
                    )
                except (TypeError, ValueError) as error:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance readback terminal transition is invalid"
                    ) from error
                if decoded_terminal_payload != transition:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance readback terminal transition differs"
                    )
            else:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback returned an unexpected event"
                )
            raw_payload_json = event_row["payload_json"]
            if (
                type(raw_payload_json) is not str
                or raw_payload_json != expected_values["payload_json"]
            ):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback event payload bytes differ"
                )
            try:
                expected_envelope = _stored_event_envelope_from_values(
                    event_id=expected_values["event_id"],
                    stream_id=expected_values["stream_id"],
                    event_type=expected_values["event_type"],
                    actor_id=expected_values["actor_id"],
                    timestamp=expected_values["timestamp"],
                    correlation_id=expected_values["correlation_id"],
                    causation_id=expected_values["causation_id"],
                    idempotency_key=expected_values["idempotency_key"],
                    payload_json=expected_values["payload_json"],
                    sequence=expected_values["sequence"],
                    global_position=expected_values["global_position"],
                )
            except (_StoredEventEnvelopeError, TypeError, ValueError) as error:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback expected event envelope is invalid"
                ) from error
            if (
                envelope_body != _StoredEventEnvelopeV1.to_dict(expected_envelope)
                or _StoredEventEnvelopeV1.canonical_bytes(envelope)
                != _StoredEventEnvelopeV1.canonical_bytes(expected_envelope)
                or envelope.digest() != coordinate.event_envelope_digest
                or envelope.digest() != expected_envelope.digest()
            ):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback event envelope differs"
                )
        if tuple(observed_event_ids) != (
            receipt.result_event.event_id,
            receipt.terminal_event.event_id,
        ):
            raise _ResultAcceptanceIntegrityError("result acceptance readback event order differs")

        try:
            rebuilt_receipt = _build_scoped_invocation_result_receipt_v2(
                request,
                evidence,
                result_event=receipt.result_event,
                terminal_event=receipt.terminal_event,
                terminal_transition=transition,
            )
        except (TypeError, ValueError) as error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback receipt cannot be rebuilt"
            ) from error
        if rebuilt_receipt != receipt:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback receipt differs from its event graph"
            )

        artifact_rows = connection.execute(
            """
            SELECT tenant_id, workspace_id, receipt_id, ordinal, session_id,
                   task_id, artifact_id, name, version, parent_version,
                   media_type, blob_digest, byte_size, metadata_digest,
                   created_by, idempotency_key, artifact_request_digest,
                   candidate_digest
            FROM main.invocation_result_artifacts
            WHERE tenant_id = ? AND workspace_id = ? AND receipt_id = ?
            ORDER BY ordinal
            """,
            (evidence.tenant_id, evidence.workspace_id, receipt.receipt_id),
        ).fetchall()
        expected_items = prepared.artifact_batch.items
        if type(artifact_rows) is not list or len(artifact_rows) != len(expected_items):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback Artifact row count differs"
            )
        for ordinal, raw_artifact in enumerate(artifact_rows):
            artifact_row = _result_readback_row(
                raw_artifact,
                _RESULT_READBACK_ARTIFACT_COLUMNS,
                "Artifact binding",
            )
            item = expected_items[ordinal]
            descriptor = item.descriptor
            if _persisted_integer(artifact_row["ordinal"], "result Artifact ordinal") != ordinal:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback Artifact ordinal is not contiguous"
                )
            for name, expected in (
                ("tenant_id", item.tenant_id),
                ("workspace_id", item.workspace_id),
                ("receipt_id", receipt.receipt_id),
                ("session_id", item.session_id),
                ("task_id", item.task_id),
                ("artifact_id", descriptor.artifact_id),
                ("name", descriptor.name),
                ("media_type", descriptor.media_type),
                ("blob_digest", descriptor.blob_digest),
                ("metadata_digest", descriptor.metadata_digest),
                ("created_by", descriptor.created_by),
                ("idempotency_key", descriptor.idempotency_key),
                ("artifact_request_digest", descriptor.request_digest),
                ("candidate_digest", item.candidate_sha256),
            ):
                compare_text(artifact_row, name, expected)
            for integer_name, integer_expected in (
                ("version", descriptor.version),
                ("byte_size", descriptor.byte_size),
            ):
                compare_integer(artifact_row, integer_name, integer_expected)
            parent = artifact_row["parent_version"]
            if parent != descriptor.parent_version or (
                parent is not None and type(parent) is not int
            ):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback Artifact parent differs"
                )

            version_row = one_row(
                """
                SELECT artifact_id, tenant_id, workspace_id, session_id, task_id,
                       name, version, parent_version, media_type, blob_digest,
                       byte_size, metadata_json, created_by, created_at,
                       idempotency_key, request_digest
                FROM main.artifact_versions
                WHERE artifact_id = ?
                """,
                (descriptor.artifact_id,),
                _RESULT_READBACK_VERSION_COLUMNS,
                "Artifact version",
            )
            for name, expected in (
                ("artifact_id", item.artifact_id),
                ("tenant_id", item.tenant_id),
                ("workspace_id", item.workspace_id),
                ("session_id", item.session_id),
                ("task_id", item.task_id),
                ("name", item.name),
                ("media_type", item.media_type),
                ("blob_digest", descriptor.blob_digest),
                ("metadata_json", item.metadata_json),
                ("created_by", item.created_by),
                ("idempotency_key", item.idempotency_key),
                ("request_digest", descriptor.request_digest),
            ):
                compare_text(version_row, name, expected)
            compare_integer(version_row, "version", descriptor.version, minimum=1)
            compare_integer(version_row, "byte_size", descriptor.byte_size)
            version_parent = version_row["parent_version"]
            if version_parent != descriptor.parent_version or (
                version_parent is not None and type(version_parent) is not int
            ):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback Artifact version parent differs"
                )
            if (
                _result_readback_timestamp(version_row["created_at"], "Artifact created_at")
                != accepted_at
            ):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback Artifact timestamp differs"
                )
            blob_row = one_row(
                """
                SELECT digest, content, byte_size, created_at,
                       typeof(digest) AS digest_storage,
                       typeof(content) AS content_storage,
                       typeof(byte_size) AS byte_size_storage,
                       typeof(created_at) AS created_at_storage,
                       length(content) AS content_length
                FROM main.artifact_blobs
                WHERE digest = ?
                """,
                (descriptor.blob_digest,),
                _RESULT_READBACK_BLOB_COLUMNS,
                "Artifact blob",
            )
            for name, expected in (
                ("digest_storage", "text"),
                ("content_storage", "blob"),
                ("created_at_storage", "text"),
            ):
                compare_text(blob_row, name, expected)
            compare_text(blob_row, "byte_size_storage", "integer")
            compare_text(blob_row, "digest", descriptor.blob_digest)
            if type(blob_row["content"]) is not bytes or blob_row["content"] != item.content:
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance readback Artifact blob content differs"
                )
            compare_integer(blob_row, "byte_size", len(item.content))
            compare_integer(blob_row, "content_length", len(item.content))
            _result_readback_timestamp(blob_row["created_at"], "Artifact blob created_at")

        job_rows = connection.execute(
            """
            SELECT invocation_id, session_id, plan_id, task_id, agent_id,
                   idempotency_key, payload_digest, priority, status,
                   max_attempts, attempts_started, lease_epoch,
                   requested_available_at, available_at, created_at, updated_at,
                   lease_owner, lease_token_digest, lease_expires_at, heartbeat_at,
                   result_ref, last_error, finished_at
            FROM main.invocation_jobs
            WHERE invocation_id = ?
            """,
            (manifest.invocation_id,),
        ).fetchall()
        if type(job_rows) is not list or len(job_rows) != 1:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback invocation job count differs"
            )
        job_row = _result_readback_row(job_rows[0], _RESULT_READBACK_JOB_COLUMNS, "job")
        try:
            job = SQLiteInvocationAttemptStore._row_to_job(job_row)
        except InvocationIntegrityError as error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback invocation job is malformed"
            ) from error
        if (
            job.status is not InvocationStatus.SUCCEEDED
            or job.invocation_id != manifest.invocation_id
            or job.session_id != manifest.session_id
            or job.plan_id != manifest.plan_id
            or job.task_id != manifest.task_id
            or job.agent_id != manifest.agent_id
            or job.idempotency_key != manifest.job_idempotency_key
            or job.payload_digest != evidence.execution_manifest_digest
            or job.max_attempts != 1
            or job.attempts_started != evidence.attempt_number
            or job.lease_epoch != evidence.lease_epoch
            or job.result_ref != evidence.result_ref
            or job.updated_at != accepted_at
            or job.finished_at != accepted_at
            or job.lease_owner is not None
            or job.lease_token_digest is not None
            or job.lease_expires_at is not None
            or job.heartbeat_at is not None
            or job.last_error is not None
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback invocation job terminal state differs"
            )

        attempt_rows = connection.execute(
            """
            SELECT attempt_id, invocation_id, attempt_number, lease_epoch,
                   worker_id, lease_token_digest, status, started_at,
                   heartbeat_at, lease_expires_at, finished_at, error, result_ref
            FROM main.invocation_attempts
            WHERE invocation_id = ?
            ORDER BY attempt_number
            """,
            (manifest.invocation_id,),
        ).fetchall()
        if type(attempt_rows) is not list or len(attempt_rows) != 1:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback invocation attempt count differs"
            )
        attempt_row = _result_readback_row(
            attempt_rows[0], _RESULT_READBACK_ATTEMPT_COLUMNS, "attempt"
        )
        try:
            attempt = SQLiteInvocationAttemptStore._row_to_attempt(attempt_row)
        except InvocationIntegrityError as error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback invocation attempt is malformed"
            ) from error
        if (
            attempt.status is not AttemptStatus.SUCCEEDED
            or attempt.attempt_id != evidence.attempt_id
            or attempt.invocation_id != evidence.invocation_id
            or attempt.attempt_number != evidence.attempt_number
            or attempt.lease_epoch != evidence.lease_epoch
            or attempt.worker_id != evidence.worker_id
            or attempt.lease_token_digest != evidence.lease_token_digest
            or attempt.finished_at != accepted_at
            or attempt.result_ref != evidence.result_ref
            or attempt.error is not None
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback invocation attempt terminal state differs"
            )
        try:
            start_state = SQLiteEventStore._load_scoped_invocation_start_in_transaction(
                self,
                connection,
                manifest.invocation_id,
                fresh=False,
            )
        except (
            InvocationStartConflictError,
            InvocationIntegrityError,
            TypeError,
            ValueError,
        ) as error:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback scoped start is no longer exact"
            ) from error
        if (
            type(start_state) is not _ScopedInvocationStartReadback
            or start_state.receipt != request.start_receipt
            or start_state.job != job
            or start_state.attempt != attempt
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback scoped start binding differs"
            )

        publication_rows = connection.execute(
            """
            SELECT receipt_id
            FROM main.invocation_result_publications
            WHERE tenant_id = ? AND workspace_id = ? AND receipt_id = ?
            """,
            (evidence.tenant_id, evidence.workspace_id, receipt.receipt_id),
        ).fetchall()
        outbox_rows = connection.execute(
            """
            SELECT message_id
            FROM main.outbox
            WHERE triggering_event_id IN (?, ?)
               OR triggering_global_position IN (?, ?)
            """,
            (
                receipt.result_event.event_id,
                receipt.terminal_event.event_id,
                receipt.result_event.global_position,
                receipt.terminal_event.global_position,
            ),
        ).fetchall()
        if publication_rows or outbox_rows:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback found premature publication or outbox rows"
            )
        orphan_rows = connection.execute(
            """
            SELECT 1
            FROM main.invocation_result_event_bindings AS binding
            LEFT JOIN main.invocation_result_receipts AS receipt
              ON receipt.tenant_id = binding.tenant_id
             AND receipt.workspace_id = binding.workspace_id
             AND receipt.receipt_id = binding.receipt_id
            WHERE receipt.receipt_id IS NULL
            UNION ALL
            SELECT 1
            FROM main.invocation_result_publications AS publication
            LEFT JOIN main.invocation_result_receipts AS receipt
              ON receipt.tenant_id = publication.tenant_id
             AND receipt.workspace_id = publication.workspace_id
             AND receipt.receipt_id = publication.receipt_id
            WHERE receipt.receipt_id IS NULL
            LIMIT 1
            """
        ).fetchall()
        if orphan_rows:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback found an orphan result graph row"
            )
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback foreign-key check failed"
            )
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if type(integrity_rows) is not list or len(integrity_rows) != 1:
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback integrity-check result is not exact"
            )
        integrity_row = integrity_rows[0]
        if type(integrity_row) is not sqlite3.Row or tuple(integrity_row.keys()) != (
            "integrity_check",
        ):
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback integrity-check row is not exact"
            )
        if integrity_row["integrity_check"] != "ok":
            raise _ResultAcceptanceIntegrityError(
                "result acceptance readback SQLite integrity check failed"
            )
        self._require_current_process()

    @_bind_event_store_process
    def read_scoped_invocation_result_observed_v2(
        self,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
    ) -> Optional[_ScopedInvocationResultObservedV2]:
        """Read one committed result as a capability-free observation.

        The public read is available only on an explicitly result-enabled rehearsal store. It
        never exposes a lease or a write capability; the default store remains feature-off until
        migration 7 is promoted through the production migration gate.
        """

        tenant_snapshot = _caller_invocation_identity(tenant_id, "tenant_id")
        workspace_snapshot = _caller_invocation_identity(workspace_id, "workspace_id")
        invocation_snapshot = _caller_invocation_identity(invocation_id, "invocation_id")
        if not self._result_acceptance_schema_enabled:
            raise ResultAcceptanceDisabledError() from None
        return self._read_scoped_invocation_result_observed_v2(
            tenant_snapshot,
            workspace_snapshot,
            invocation_snapshot,
        )

    @_bind_event_store_process
    def _read_scoped_invocation_result_observed_v2(
        self,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
    ) -> Optional[_ScopedInvocationResultObservedV2]:
        """Reopen and verify one committed result as a capability-free observation.

        This path never reads a plaintext lease, never performs DML, and never returns a
        fresh-commit plan.  It is intentionally private while migration 7 and the public
        result writer remain disabled.
        """

        tenant_snapshot = _caller_invocation_identity(tenant_id, "tenant_id")
        workspace_snapshot = _caller_invocation_identity(workspace_id, "workspace_id")
        invocation_snapshot = _caller_invocation_identity(invocation_id, "invocation_id")
        with self._transaction() as connection:
            return self._read_scoped_invocation_result_observed_v2_in_transaction(
                connection,
                tenant_snapshot,
                workspace_snapshot,
                invocation_snapshot,
            )

    def _read_scoped_invocation_result_observed_v2_in_transaction(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
    ) -> Optional[_ScopedInvocationResultObservedV2]:
        """Read and validate a result graph without requiring the original lease token."""

        self._require_current_process()
        self._require_result_acceptance_candidate_schema_in_transaction(connection)

        def quarantine(
            detail: str,
            category: _ResultAcceptanceQuarantineCategory = (
                _ResultAcceptanceQuarantineCategory.DRIFT
            ),
        ) -> NoReturn:
            raise _ResultAcceptanceQuarantineError(detail, category=category)

        def one_row(
            sql: str,
            parameters: tuple[object, ...],
            columns: tuple[str, ...],
            label: str,
        ) -> sqlite3.Row:
            try:
                rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.Error:
                quarantine(f"result observation {label} cannot be read")
            if type(rows) is not list:
                quarantine(f"result observation {label} row collection is invalid")
            if len(rows) == 0:
                quarantine(
                    f"result observation {label} row is missing",
                    _ResultAcceptanceQuarantineCategory.PARTIAL,
                )
            if len(rows) != 1:
                quarantine(f"result observation {label} resolves to multiple rows")
            return _result_readback_row(rows[0], columns, label)

        receipt_columns = _RESULT_READBACK_RECEIPT_COLUMNS
        receipt_rows = connection.execute(
            "SELECT " + ", ".join(receipt_columns) + " FROM main.invocation_result_receipts "
            "WHERE tenant_id = ? AND workspace_id = ? AND invocation_id = ? LIMIT 2",
            (tenant_id, workspace_id, invocation_id),
        ).fetchall()
        self._require_current_process()
        if type(receipt_rows) is not list:
            quarantine("result observation receipt row collection is invalid")
        if not receipt_rows:
            partial_row = connection.execute(
                """
                SELECT 1
                FROM main.invocation_result_requests
                WHERE tenant_id = ? AND workspace_id = ? AND invocation_id = ?
                UNION ALL
                SELECT 1
                FROM main.invocation_result_artifacts AS artifact
                JOIN main.invocation_result_receipts AS receipt
                  ON receipt.tenant_id = artifact.tenant_id
                 AND receipt.workspace_id = artifact.workspace_id
                 AND receipt.receipt_id = artifact.receipt_id
                WHERE artifact.tenant_id = ? AND artifact.workspace_id = ?
                  AND receipt.invocation_id = ?
                LIMIT 1
                """,
                (tenant_id, workspace_id, invocation_id, tenant_id, workspace_id, invocation_id),
            ).fetchone()
            orphan_row = connection.execute(
                """
                SELECT 1
                FROM main.invocation_result_event_bindings AS binding
                LEFT JOIN main.invocation_result_receipts AS receipt
                  ON receipt.tenant_id = binding.tenant_id
                 AND receipt.workspace_id = binding.workspace_id
                 AND receipt.receipt_id = binding.receipt_id
                WHERE receipt.receipt_id IS NULL
                UNION ALL
                SELECT 1
                FROM main.invocation_result_publications AS publication
                LEFT JOIN main.invocation_result_receipts AS receipt
                  ON receipt.tenant_id = publication.tenant_id
                 AND receipt.workspace_id = publication.workspace_id
                 AND receipt.receipt_id = publication.receipt_id
                WHERE receipt.receipt_id IS NULL
                LIMIT 1
                """
            ).fetchone()
            if orphan_row is not None:
                quarantine(
                    "result observation found an orphan durable graph",
                    _ResultAcceptanceQuarantineCategory.ORPHAN,
                )
            if partial_row is not None:
                quarantine(
                    "result observation found a partial durable graph",
                    _ResultAcceptanceQuarantineCategory.PARTIAL,
                )
            return None
        if len(receipt_rows) != 1:
            quarantine("result observation receipt resolves to multiple durable graphs")
        receipt_row = _result_readback_row(receipt_rows[0], receipt_columns, "receipt")
        try:
            receipt_id = _persisted_text(
                receipt_row["receipt_id"], "result observation receipt_id", required=True
            )
            receipt_digest = _persisted_result_acceptance_digest(
                receipt_row["receipt_digest"], "result observation receipt_digest"
            )
            request_digest = _persisted_result_acceptance_digest(
                receipt_row["request_digest"], "result observation request_digest"
            )
            manifest_digest = _persisted_result_acceptance_digest(
                receipt_row["result_manifest_digest"],
                "result observation manifest_digest",
            )
            result_event_id = _persisted_text(
                receipt_row["result_event_id"],
                "result observation result_event_id",
                required=True,
            )
            terminal_event_id = _persisted_text(
                receipt_row["terminal_event_id"],
                "result observation terminal_event_id",
                required=True,
            )
        except (TypeError, ValueError, KeyError, IndexError):
            quarantine("result observation receipt row is malformed")

        manifest_row = one_row(
            "SELECT "
            + ", ".join(_RESULT_READBACK_MANIFEST_COLUMNS)
            + " FROM main.invocation_result_manifests "
            "WHERE tenant_id = ? AND workspace_id = ? AND manifest_digest = ? LIMIT 2",
            (tenant_id, workspace_id, manifest_digest),
            _RESULT_READBACK_MANIFEST_COLUMNS,
            "manifest",
        )
        try:
            canonical_manifest = manifest_row["canonical_bytes"]
            if type(canonical_manifest) is not bytes:
                quarantine("result observation manifest is not a BLOB")
            manifest_byte_size = _persisted_integer(
                manifest_row["byte_size"], "result observation manifest byte_size", minimum=1
            )
            if manifest_byte_size != len(canonical_manifest):
                quarantine("result observation manifest byte size differs")
            manifest = _ScopedInvocationResultManifestV2.from_dict(
                json.loads(
                    canonical_manifest.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
            )
            if (
                manifest.tenant_id != tenant_id
                or manifest.workspace_id != workspace_id
                or manifest.canonical_digest() != manifest_digest
                or manifest.canonical_bytes() != canonical_manifest
            ):
                quarantine("result observation manifest canonical identity differs")
            _result_readback_timestamp(manifest_row["created_at"], "manifest created_at")
        except _ResultAcceptanceQuarantineError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, UnicodeError, json.JSONDecodeError):
            quarantine("result observation manifest cannot be decoded")

        request_row = one_row(
            "SELECT "
            + ", ".join(_RESULT_READBACK_REQUEST_COLUMNS)
            + " FROM main.invocation_result_requests "
            "WHERE tenant_id = ? AND workspace_id = ? AND request_digest = ? LIMIT 2",
            (tenant_id, workspace_id, request_digest),
            _RESULT_READBACK_REQUEST_COLUMNS,
            "request",
        )
        try:
            acceptance_key = _persisted_text(
                request_row["acceptance_idempotency_key"],
                "result observation acceptance_idempotency_key",
                required=True,
            )
            expected_stream_version = _persisted_integer(
                request_row["expected_stream_version"],
                "result observation expected_stream_version",
                minimum=1,
            )
            request_schema_version = _persisted_integer(
                request_row["schema_version"],
                "result observation request schema_version",
            )
        except (TypeError, ValueError, KeyError, IndexError):
            quarantine("result observation request row is malformed")

        try:
            start_state = SQLiteEventStore._load_scoped_invocation_start_in_transaction(
                self,
                connection,
                invocation_id,
                fresh=False,
            )
        except (InvocationIntegrityError, InvocationStartConflictError, TypeError, ValueError):
            quarantine("result observation scoped start is not exact")
        if type(start_state) is not _ScopedInvocationStartReadback:
            quarantine(
                "result observation scoped start is missing",
                _ResultAcceptanceQuarantineCategory.PARTIAL,
            )

        artifact_rows = connection.execute(
            "SELECT "
            + ", ".join(_RESULT_READBACK_ARTIFACT_COLUMNS)
            + " FROM main.invocation_result_artifacts "
            "WHERE tenant_id = ? AND workspace_id = ? AND receipt_id = ? ORDER BY ordinal",
            (tenant_id, workspace_id, receipt_id),
        ).fetchall()
        self._require_current_process()
        if type(artifact_rows) is not list:
            quarantine("result observation Artifact row collection is invalid")
        if len(artifact_rows) != len(manifest.artifacts):
            quarantine(
                "result observation Artifact rows are partial",
                _ResultAcceptanceQuarantineCategory.PARTIAL,
            )
        candidates: list[_ScopedInvocationResultArtifactCandidateV2] = []
        for ordinal, raw_binding in enumerate(artifact_rows):
            binding = _result_readback_row(
                raw_binding,
                _RESULT_READBACK_ARTIFACT_COLUMNS,
                "Artifact binding",
            )
            try:
                if _persisted_integer(binding["ordinal"], "result observation ordinal") != ordinal:
                    quarantine(
                        "result observation Artifact ordinals are not contiguous",
                        _ResultAcceptanceQuarantineCategory.PARTIAL,
                    )
                binding_tenant = _persisted_text(
                    binding["tenant_id"], "result observation Artifact tenant", required=True
                )
                binding_workspace = _persisted_text(
                    binding["workspace_id"],
                    "result observation Artifact workspace",
                    required=True,
                )
                binding_receipt = _persisted_text(
                    binding["receipt_id"], "result observation Artifact receipt", required=True
                )
                artifact_id = _persisted_text(
                    binding["artifact_id"], "result observation Artifact id", required=True
                )
                artifact_name = _persisted_text(
                    binding["name"], "result observation Artifact name", required=True
                )
                artifact_media_type = _persisted_text(
                    binding["media_type"],
                    "result observation Artifact media_type",
                    required=True,
                )
                artifact_created_by = _persisted_text(
                    binding["created_by"],
                    "result observation Artifact created_by",
                    required=True,
                )
                artifact_idempotency_key = _persisted_text(
                    binding["idempotency_key"],
                    "result observation Artifact idempotency_key",
                    required=True,
                )
                binding_version = _persisted_integer(
                    binding["version"], "result observation Artifact version", minimum=1
                )
                binding_parent = binding["parent_version"]
                if binding_parent is not None:
                    binding_parent = _persisted_integer(
                        binding_parent,
                        "result observation Artifact parent_version",
                        minimum=1,
                    )
                binding_blob_digest = _persisted_text(
                    binding["blob_digest"],
                    "result observation Artifact blob_digest",
                    required=True,
                )
                binding_byte_size = _persisted_integer(
                    binding["byte_size"], "result observation Artifact byte_size"
                )
                binding_metadata_digest = _persisted_result_acceptance_digest(
                    binding["metadata_digest"],
                    "result observation Artifact metadata_digest",
                )
            except (TypeError, ValueError, KeyError, IndexError):
                quarantine("result observation Artifact binding is malformed")

            version_row = one_row(
                "SELECT "
                + ", ".join(_RESULT_READBACK_VERSION_COLUMNS)
                + " FROM main.artifact_versions WHERE artifact_id = ? LIMIT 2",
                (artifact_id,),
                _RESULT_READBACK_VERSION_COLUMNS,
                "Artifact version",
            )
            blob_digest = _persisted_text(
                version_row["blob_digest"],
                "result observation Artifact version blob_digest",
                required=True,
            )
            metadata_json = version_row["metadata_json"]
            if type(metadata_json) is not str:
                quarantine("result observation Artifact metadata is not TEXT")
            version = _persisted_integer(
                version_row["version"], "result observation version", minimum=1
            )
            version_parent = version_row["parent_version"]
            if version_parent is not None:
                version_parent = _persisted_integer(
                    version_parent,
                    "result observation version parent_version",
                    minimum=1,
                )
            expected_head_version = 0 if version_parent is None else version_parent
            blob_row = one_row(
                """
                SELECT digest, content, byte_size, created_at,
                       typeof(digest) AS digest_storage,
                       typeof(content) AS content_storage,
                       typeof(byte_size) AS byte_size_storage,
                       typeof(created_at) AS created_at_storage,
                       length(content) AS content_length
                FROM main.artifact_blobs WHERE digest = ? LIMIT 2
                """,
                (blob_digest,),
                _RESULT_READBACK_BLOB_COLUMNS,
                "Artifact blob",
            )
            content = blob_row["content"]
            if type(content) is not bytes:
                quarantine("result observation Artifact blob is not a BLOB")
            try:
                metadata = json.loads(metadata_json, parse_constant=_reject_json_constant)
                if type(metadata) is not dict:
                    quarantine("result observation Artifact metadata is not an object")
                candidate = _ScopedInvocationResultArtifactCandidateV2.from_content_metadata(
                    tenant_id=binding_tenant,
                    workspace_id=binding_workspace,
                    session_id=_persisted_text(
                        binding["session_id"], "result observation Artifact session", required=True
                    ),
                    task_id=_persisted_text(
                        binding["task_id"], "result observation Artifact task", required=True
                    ),
                    artifact_id=artifact_id,
                    name=artifact_name,
                    media_type=artifact_media_type,
                    content=content,
                    metadata=metadata,
                    created_by=artifact_created_by,
                    idempotency_key=artifact_idempotency_key,
                    expected_head_version=expected_head_version,
                )
            except _ResultAcceptanceQuarantineError:
                raise
            except (
                TypeError,
                ValueError,
                KeyError,
                IndexError,
                UnicodeError,
                json.JSONDecodeError,
            ):
                quarantine("result observation Artifact content cannot be decoded")
            if metadata_json.encode("utf-8") != candidate.metadata_canonical_bytes:
                quarantine("result observation Artifact metadata bytes differ")
            if (
                binding_tenant != tenant_id
                or binding_workspace != workspace_id
                or binding_receipt != receipt_id
                or binding_version != version
                or binding_parent != version_parent
                or binding_blob_digest != blob_digest
                or binding_byte_size != candidate.byte_size
                or binding_metadata_digest != candidate.metadata_digest
                or candidate.to_descriptor() != manifest.artifacts[ordinal]
            ):
                quarantine("result observation Artifact descriptor differs")
            candidates.append(candidate)

        event_rows = connection.execute(
            "SELECT "
            + ", ".join(_RESULT_READBACK_EVENT_COLUMNS)
            + " FROM main.events WHERE event_id IN (?, ?) ORDER BY global_position",
            (result_event_id, terminal_event_id),
        ).fetchall()
        self._require_current_process()
        if type(event_rows) is not list or len(event_rows) != 2:
            quarantine(
                "result observation event rows are partial",
                _ResultAcceptanceQuarantineCategory.PARTIAL,
            )
        result_coordinate: Optional[_ScopedInvocationResultEventCoordinatesV2] = None
        terminal_coordinate: Optional[_ScopedInvocationResultEventCoordinatesV2] = None
        result_payload: Optional[object] = None
        terminal_payload: Optional[object] = None
        for raw_event in event_rows:
            event_row = _result_readback_row(raw_event, _RESULT_READBACK_EVENT_COLUMNS, "event")
            try:
                envelope = _stored_event_envelope_from_raw_row(event_row)
                body = _StoredEventEnvelopeV1.to_dict(envelope)
                event_body_id = _persisted_text(
                    body["eventId"], "result observation event_id", required=True
                )
                event_body_stream = _persisted_text(
                    body["streamId"], "result observation stream_id", required=True
                )
                event_body_type = _persisted_text(
                    body["eventType"], "result observation event_type", required=True
                )
                event_body_sequence = _persisted_integer(
                    body["sequence"], "result observation event sequence", minimum=1
                )
                event_body_global_position = _persisted_integer(
                    body["globalPosition"],
                    "result observation event global_position",
                    minimum=1,
                )
                event_coordinate = _ScopedInvocationResultEventCoordinatesV2(
                    event_id=event_body_id,
                    stream_id=event_body_stream,
                    event_type=event_body_type,
                    sequence=event_body_sequence,
                    global_position=event_body_global_position,
                    event_envelope_digest=_StoredEventEnvelopeV1.digest(envelope),
                )
                payload = body["payload"]
                if event_body_id == result_event_id:
                    if result_coordinate is not None:
                        quarantine("result observation has duplicate result events")
                    result_coordinate = event_coordinate
                    result_payload = payload
                elif event_body_id == terminal_event_id:
                    if terminal_coordinate is not None:
                        quarantine("result observation has duplicate terminal events")
                    terminal_coordinate = event_coordinate
                    terminal_payload = payload
                else:
                    quarantine("result observation returned an unexpected event identity")
            except _ResultAcceptanceQuarantineError:
                raise
            except (
                _StoredEventEnvelopeError,
                TypeError,
                ValueError,
                KeyError,
                IndexError,
            ):
                quarantine("result observation event envelope is malformed")
        if (
            result_coordinate is None
            or terminal_coordinate is None
            or type(result_payload) is not dict
            or type(terminal_payload) is not dict
        ):
            quarantine(
                "result observation event payloads are partial",
                _ResultAcceptanceQuarantineCategory.PARTIAL,
            )
        try:
            evidence = _ScopedInvocationResultEvidenceV2.from_dict(result_payload)
            terminal_transition = _ScopedInvocationResultTerminalTransitionV2.from_dict(
                terminal_payload
            )
            if evidence.receipt_id != receipt_id:
                quarantine("result observation evidence receipt identity differs")
            request = _ScopedInvocationResultAcceptanceRequestV2(
                schema_version=request_schema_version,
                acceptance_idempotency_key=acceptance_key,
                start_receipt=start_state.receipt,
                manifest=manifest,
                artifact_candidates=tuple(candidates),
                expected_stream_version=expected_stream_version,
            )
            receipt = _build_scoped_invocation_result_receipt_v2(
                request,
                evidence,
                result_event=result_coordinate,
                terminal_event=terminal_coordinate,
                terminal_transition=terminal_transition,
            )
            if receipt.receipt_digest != receipt_digest:
                quarantine("result observation receipt_digest differs")
        except _ResultAcceptanceQuarantineError:
            raise
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            UnicodeError,
        ):
            quarantine("result observation receipt graph cannot be decoded")

        try:
            observation_input = _ResultAcceptanceObservationInputV2(
                request=request,
                artifact_batch=_prepare_result_artifact_batch(tuple(candidates)),
            )
        except (
            _ResultArtifactConflictError,
            _ResultArtifactConcurrencyError,
            _ResultArtifactIntegrityError,
            _ResultArtifactTransactionContinuityError,
            _ResultArtifactTransactionError,
            TypeError,
            ValueError,
        ):
            quarantine("result observation Artifact batch cannot be rebuilt")
        try:
            self._readback_result_acceptance_graph_body(
                connection,
                observation_input,
                receipt,
            )
        except _ResultAcceptanceQuarantineError:
            raise
        except _ResultAcceptanceIntegrityError as error:
            raise _ResultAcceptanceQuarantineError(
                str(error),
                category=_ResultAcceptanceQuarantineCategory.DRIFT,
            ) from error
        except (
            InvocationIntegrityError,
            _ResultAcceptanceConflictError,
            sqlite3.Error,
            TypeError,
            ValueError,
            KeyError,
            IndexError,
        ) as error:
            raise _ResultAcceptanceQuarantineError(
                "result observation complete graph verification failed",
                category=_ResultAcceptanceQuarantineCategory.DRIFT,
            ) from error
        self._require_current_process()
        return _ScopedInvocationResultObservedV2(receipt=receipt)

    @_bind_event_store_process
    def _complete_result_acceptance_job_and_attempt_in_owner_transaction(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> ContextManager[
        _ExistingResultAcceptanceGraphCandidateV2 | _CompletedFreshResultAcceptancePlanV2
    ]:
        """CAS the exact running job and attempt to succeeded after graph persistence."""

        return self._complete_result_acceptance_job_and_attempt_in_owner_transaction_inner(
            handle,
            prepared,
        )

    @contextmanager
    def _complete_result_acceptance_job_and_attempt_in_owner_transaction_inner(
        self,
        handle: _ResultArtifactTransactionHandle,
        prepared: _PreparedScopedInvocationResultAcceptanceV2,
    ) -> Iterator[
        _ExistingResultAcceptanceGraphCandidateV2 | _CompletedFreshResultAcceptancePlanV2
    ]:
        completed: Optional[_CompletedFreshResultAcceptancePlanV2] = None
        with self._persist_result_acceptance_graph_in_owner_transaction(
            handle,
            prepared,
        ) as candidate:
            if type(candidate) is _ExistingResultAcceptanceGraphCandidateV2:
                yield candidate
                return
            if type(candidate) is not _PersistedFreshResultAcceptancePlanV2:
                raise RuntimeError("result acceptance completion classification is not closed")
            persisted_plan, receipt = candidate._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            receipted_plan, _ = persisted_plan._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)
            evented_plan, _, _, _, _ = receipted_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            transitioned_plan, _, _ = evented_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            evidenced_plan, _ = transitioned_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            identified_plan, evidence = evidenced_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            materialized_plan, _, _, _ = identified_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            prepared_snapshot, prerequisites, accepted_at, _ = materialized_plan._validated(
                token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN
            )
            connection = self._connection_for_result_artifact_transaction(handle)
            lease_digest = evidence.lease_token_digest
            self._update_exact_result_acceptance_row_in_owner_transaction(
                connection,
                """
                UPDATE invocation_jobs
                SET status = 'succeeded', result_ref = ?, updated_at = ?, finished_at = ?,
                    lease_owner = NULL, lease_token_digest = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL
                WHERE invocation_id = ? AND session_id = ? AND task_id = ?
                  AND agent_id = ? AND status = 'running'
                  AND attempts_started = ? AND lease_epoch = ?
                  AND lease_owner = ? AND lease_token_digest = ?
                  AND lease_expires_at > ? AND heartbeat_at <= ?
                """,
                (
                    evidence.result_ref,
                    accepted_at,
                    accepted_at,
                    evidence.invocation_id,
                    evidence.session_id,
                    evidence.task_id,
                    evidence.agent_id,
                    evidence.attempt_number,
                    evidence.lease_epoch,
                    evidence.worker_id,
                    lease_digest,
                    accepted_at,
                    accepted_at,
                ),
                label="job terminal CAS",
            )
            self._update_exact_result_acceptance_row_in_owner_transaction(
                connection,
                """
                UPDATE invocation_attempts
                SET status = 'succeeded', finished_at = ?, result_ref = ?, error = NULL
                WHERE attempt_id = ? AND invocation_id = ? AND attempt_number = ?
                  AND lease_epoch = ? AND worker_id = ? AND lease_token_digest = ?
                  AND status = 'running' AND heartbeat_at <= ? AND lease_expires_at > ?
                """,
                (
                    accepted_at,
                    evidence.result_ref,
                    evidence.attempt_id,
                    evidence.invocation_id,
                    evidence.attempt_number,
                    evidence.lease_epoch,
                    evidence.worker_id,
                    lease_digest,
                    accepted_at,
                    accepted_at,
                ),
                label="attempt terminal CAS",
            )
            self._require_current_process()
            if (
                prerequisites.running_task_revision + 1
                != receipt.terminal_transition.terminal_task_revision
                or receipt.evidence.result_ref != prepared_snapshot.request.manifest.result_ref
            ):
                raise _ResultAcceptanceIntegrityError(
                    "result acceptance terminal CAS bindings changed"
                )
            with self._readback_result_acceptance_graph_in_owner_transaction(
                handle,
                prepared,
                receipt,
            ) as readback:
                if readback._validated(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN) != receipt:
                    raise _ResultAcceptanceIntegrityError(
                        "result acceptance complete readback receipt differs"
                    )
                completed = _CompletedFreshResultAcceptancePlanV2(
                    persisted=candidate,
                    receipt=receipt,
                    token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN,
                )
                self._require_current_process()
                try:
                    yield completed
                finally:
                    completed._invalidate(token=_RESULT_ACCEPTANCE_WRITE_PLAN_TOKEN)

    def _execute_transaction_control(
        self,
        connection: sqlite3.Connection,
        statement: str,
    ) -> None:
        """Execute one exact transaction-control statement under the process fence.

        Keeping the control boundary in one private method gives fault-injection and
        recovery evidence a stable seam without exposing the SQLite connection or
        allowing callers to issue arbitrary control SQL.
        """

        self._require_current_process()
        if connection is not self._connection:
            raise RuntimeError("transaction control requires the owning connection")
        if statement not in {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}:
            raise ValueError("transaction control statement is not allowlisted")
        execute = getattr(connection, "execute", None)
        if not callable(execute):
            raise RuntimeError("transaction control connection has no execute operation")
        execute(statement)
        self._require_current_process()

    @contextmanager
    def _transaction_inner(
        self,
        *,
        classify_admission: bool,
    ) -> Iterator[sqlite3.Connection]:
        self._require_current_process()
        lock = self._lock
        lock.acquire()
        try:
            self._require_current_process()
            if self._poisoned:
                raise EventStorePoisonedError() from None
            connection = self._connection
            try:
                self._execute_transaction_control(connection, "BEGIN IMMEDIATE")
                self._require_current_process()
            except BaseException as begin_error:
                if not self._process_is_current() or not classify_admission:
                    raise
                try:
                    transaction_open = connection.in_transaction
                    if type(transaction_open) is not bool:
                        raise RuntimeError("SQLite returned a non-boolean transaction state")
                    if transaction_open:
                        self._execute_transaction_control(connection, "ROLLBACK")
                        self._require_current_process()
                        rollback_state = connection.in_transaction
                        if type(rollback_state) is not bool or rollback_state:
                            raise RuntimeError("SQLite did not confirm BEGIN rollback")
                except BaseException as rollback_error:
                    control = _event_store_control_descriptor(
                        begin_error
                    ) or _event_store_control_descriptor(rollback_error)
                    self._poisoned = True
                    _detach_exception(rollback_error)
                    _detach_exception(begin_error)
                    raise _EventStoreAdmissionTransactionSignal(
                        "ambiguous",
                        control=control,
                        token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                    ) from None
                control = _event_store_control_descriptor(begin_error)
                _detach_exception(begin_error)
                raise _EventStoreAdmissionTransactionSignal(
                    "rolled_back",
                    control=control,
                    token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                ) from None
            try:
                yield connection
            except BaseException as body_error:
                if not self._process_is_current():
                    raise
                try:
                    transaction_open = connection.in_transaction
                    if type(transaction_open) is not bool:
                        raise RuntimeError("SQLite returned a non-boolean transaction state")
                    if transaction_open:
                        self._execute_transaction_control(connection, "ROLLBACK")
                        self._require_current_process()
                        rollback_state = connection.in_transaction
                        if type(rollback_state) is not bool or rollback_state:
                            raise RuntimeError("SQLite did not confirm body rollback")
                except BaseException as rollback_error:
                    if classify_admission:
                        classified_body = _take_classified_event_store_transaction_signal(
                            body_error
                        )
                        control = (
                            classified_body[1]
                            if classified_body is not None
                            else _event_store_control_descriptor(body_error)
                        ) or _event_store_control_descriptor(rollback_error)
                        self._poisoned = True
                        _detach_exception(rollback_error)
                        _detach_exception(body_error)
                        raise _EventStoreAdmissionTransactionSignal(
                            "ambiguous",
                            control=control,
                            token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                        ) from None
                    if not _is_exact_control_signal(body_error):
                        raise
                if classify_admission:
                    control = _event_store_control_descriptor(body_error)
                    if control is not None:
                        _detach_exception(body_error)
                        raise _EventStoreAdmissionTransactionSignal(
                            "rolled_back",
                            control=control,
                            token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                        ) from None
                raise
            else:
                self._require_current_process()
                try:
                    self._execute_transaction_control(connection, "COMMIT")
                    self._require_current_process()
                except BaseException as commit_error:
                    if not self._process_is_current():
                        raise
                    transaction_was_open = False
                    rollback_confirmed = False
                    try:
                        transaction_was_open = connection.in_transaction
                        if type(transaction_was_open) is not bool:
                            raise RuntimeError("SQLite returned a non-boolean transaction state")
                        if transaction_was_open:
                            self._execute_transaction_control(connection, "ROLLBACK")
                            self._require_current_process()
                            rollback_state = connection.in_transaction
                            if type(rollback_state) is not bool:
                                raise RuntimeError(
                                    "SQLite returned a non-boolean transaction state"
                                )
                            rollback_confirmed = not rollback_state
                    except BaseException as rollback_error:
                        if classify_admission:
                            control = _event_store_control_descriptor(
                                commit_error
                            ) or _event_store_control_descriptor(rollback_error)
                            self._poisoned = True
                            _detach_exception(rollback_error)
                            _detach_exception(commit_error)
                            raise _EventStoreAdmissionTransactionSignal(
                                "ambiguous",
                                control=control,
                                token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                            ) from None
                        if not _is_exact_control_signal(commit_error):
                            raise
                    if classify_admission:
                        outcome = (
                            "rolled_back"
                            if transaction_was_open and rollback_confirmed
                            else "ambiguous"
                        )
                        control = _event_store_control_descriptor(commit_error)
                        if outcome == "ambiguous":
                            self._poisoned = True
                        _detach_exception(commit_error)
                        raise _EventStoreAdmissionTransactionSignal(
                            outcome,
                            control=control,
                            token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
                        ) from None
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

    def _snapshot_generic_event(self, event: DomainEvent) -> _EventWriteSnapshot:
        """Freeze caller state and enforce the generic result-vocabulary fence."""

        snapshot = SQLiteEventStore._snapshot_event(self, event)
        SQLiteEventStore._reject_generic_reserved_result_event(snapshot.event)
        return snapshot

    @staticmethod
    def _stored_event_envelope_from_write_snapshot(
        snapshot: _EventWriteSnapshot,
        *,
        sequence: int,
        global_position: int,
    ) -> _StoredEventEnvelopeV1:
        """Derive a capability-free envelope from one exact store-owned write snapshot."""

        if type(snapshot) is not _EventWriteSnapshot:
            raise TypeError("event write snapshot must use its exact class")
        event = object.__getattribute__(snapshot, "event")
        if type(event) is not DomainEvent:
            raise TypeError("event write snapshot must contain an exact DomainEvent")
        return _stored_event_envelope_from_values(
            event_id=object.__getattribute__(event, "event_id"),
            stream_id=object.__getattribute__(event, "stream_id"),
            event_type=object.__getattribute__(event, "event_type"),
            actor_id=object.__getattribute__(event, "actor_id"),
            timestamp=object.__getattribute__(event, "timestamp"),
            correlation_id=object.__getattribute__(event, "correlation_id"),
            causation_id=object.__getattribute__(event, "causation_id"),
            idempotency_key=object.__getattribute__(event, "idempotency_key"),
            payload_json=object.__getattribute__(snapshot, "payload_json"),
            sequence=sequence,
            global_position=global_position,
        )

    @staticmethod
    def _freeze_event_write_snapshot(
        snapshot: _EventWriteSnapshot,
    ) -> _EventWriteSnapshot:
        """Detach the exact immutable values used by the private INSERT path."""

        preflight = SQLiteEventStore._stored_event_envelope_from_write_snapshot(
            snapshot,
            sequence=1,
            global_position=1,
        )
        body = _StoredEventEnvelopeV1.to_dict(preflight)
        payload_json = object.__getattribute__(snapshot, "payload_json")
        frozen_event = DomainEvent(
            stream_id=cast(str, body["streamId"]),
            event_type=cast(str, body["eventType"]),
            payload=cast(Dict[str, Any], body["payload"]),
            actor_id=cast(str, body["actorId"]),
            event_id=cast(str, body["eventId"]),
            timestamp=cast(str, body["timestamp"]),
            correlation_id=cast(Optional[str], body["correlationId"]),
            causation_id=cast(Optional[str], body["causationId"]),
            idempotency_key=cast(Optional[str], body["idempotencyKey"]),
        )
        return _EventWriteSnapshot(frozen_event, payload_json)

    @staticmethod
    def _freeze_typed_result_event_write_snapshot(
        snapshot: _EventWriteSnapshot,
    ) -> _EventWriteSnapshot:
        """Freeze and type-check the two reserved result-authority event payloads."""

        try:
            frozen = SQLiteEventStore._freeze_event_write_snapshot(snapshot)
            event = object.__getattribute__(frozen, "event")
            payload_json = object.__getattribute__(frozen, "payload_json")
            payload = object.__getattribute__(event, "payload")
            event_type = object.__getattribute__(event, "event_type")
            if event_type == _TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE:
                evidence = _ScopedInvocationResultEvidenceV2.from_dict(payload)
                typed_bytes = _ScopedInvocationResultEvidenceV2.canonical_bytes(evidence)
                if (
                    object.__getattribute__(event, "stream_id") != "session:" + evidence.session_id
                    or object.__getattribute__(event, "actor_id") != CANONICAL_ORCHESTRATOR_ACTOR_ID
                    or object.__getattribute__(event, "timestamp") != evidence.accepted_at
                    or object.__getattribute__(event, "correlation_id") is None
                    or object.__getattribute__(event, "causation_id") is None
                    or object.__getattribute__(event, "idempotency_key")
                    != evidence.acceptance_idempotency_key
                ):
                    raise _ResultEventWriteContractError()
            elif event_type == _TASK_STATUS_CHANGED_EVENT_TYPE:
                transition = _ScopedInvocationResultTerminalTransitionV2.from_dict(payload)
                typed_bytes = _ScopedInvocationResultTerminalTransitionV2.canonical_bytes(
                    transition
                )
                if (
                    object.__getattribute__(event, "stream_id")
                    != "session:" + transition.session_id
                    or object.__getattribute__(event, "actor_id") != CANONICAL_ORCHESTRATOR_ACTOR_ID
                    or object.__getattribute__(event, "correlation_id") != transition.correlation_id
                    or object.__getattribute__(event, "causation_id") != transition.result_event_id
                    or object.__getattribute__(event, "idempotency_key")
                    != f"task-status:{transition.task_id}:{transition.terminal_task_revision}"
                ):
                    raise _ResultEventWriteContractError()
            else:
                raise _ResultEventWriteContractError()
            if typed_bytes != payload_json.encode("utf-8"):
                raise _ResultEventWriteContractError()
            return frozen
        except _ResultEventWriteContractError:
            raise
        except (_StoredEventEnvelopeError, TypeError, ValueError, UnicodeError):
            raise _ResultEventWriteContractError() from None

    def _verify_stored_event_envelope_in_transaction(
        self,
        connection: sqlite3.Connection,
        snapshot: _EventWriteSnapshot,
        stored: StoredEvent,
    ) -> _StoredEventEnvelopeV1:
        """Compare the frozen INSERT values with the exact durable row before commit."""

        self._require_current_process()
        if type(connection) is not sqlite3.Connection or connection is not self._connection:
            raise RuntimeError("stored event envelope verification requires the owning connection")
        transaction_open = connection.in_transaction
        if type(transaction_open) is not bool or not transaction_open:
            raise RuntimeError("stored event envelope verification requires an open transaction")
        if type(stored) is not StoredEvent:
            raise TypeError("stored event must use its exact class")
        try:
            write_envelope = SQLiteEventStore._stored_event_envelope_from_write_snapshot(
                snapshot,
                sequence=object.__getattribute__(stored, "sequence"),
                global_position=object.__getattribute__(stored, "global_position"),
            )
            row = connection.execute(
                """
                SELECT
                    global_position,
                    stream_id,
                    sequence,
                    event_id,
                    event_type,
                    actor_id,
                    timestamp,
                    payload_json,
                    correlation_id,
                    causation_id,
                    idempotency_key
                FROM events
                WHERE global_position = ?
                """,
                (object.__getattribute__(stored, "global_position"),),
            ).fetchone()
            self._require_current_process()
            if row is None:
                raise EventStoreIntegrityError("stored event envelope readback row is missing")
            raw_envelope = _stored_event_envelope_from_raw_row(row)
            if (
                _StoredEventEnvelopeV1.to_dict(write_envelope)
                != _StoredEventEnvelopeV1.to_dict(raw_envelope)
                or _StoredEventEnvelopeV1.canonical_bytes(write_envelope)
                != _StoredEventEnvelopeV1.canonical_bytes(raw_envelope)
                or _StoredEventEnvelopeV1.digest(write_envelope)
                != _StoredEventEnvelopeV1.digest(raw_envelope)
            ):
                raise EventStoreIntegrityError("stored event envelope readback mismatch")
            return raw_envelope
        except EventStoreIntegrityError:
            raise
        except _StoredEventEnvelopeError:
            raise EventStoreIntegrityError("stored event envelope readback is invalid") from None

    @_sanitize_stored_event_envelope_errors
    def _insert_with_verified_envelope_in_transaction(
        self,
        connection: sqlite3.Connection,
        snapshot: _EventWriteSnapshot,
        expected_version: Optional[int],
        expected_global_position: Optional[int] = None,
    ) -> Tuple[StoredEvent, bool, _StoredEventEnvelopeV1]:
        """Fresh-insert and verify one strict event without exposing a public writer."""

        self._require_current_process()
        if type(connection) is not sqlite3.Connection or connection is not self._connection:
            raise RuntimeError("verified event append requires the owning connection")
        transaction_open = connection.in_transaction
        if type(transaction_open) is not bool or not transaction_open:
            raise RuntimeError("verified event append requires an open transaction")
        if expected_version is not None:
            expected_version = _caller_sqlite_integer(
                expected_version,
                "expected_version",
            )
        if expected_global_position is not None:
            expected_global_position = _caller_sqlite_integer(
                expected_global_position,
                "expected_global_position",
            )
        frozen = SQLiteEventStore._freeze_typed_result_event_write_snapshot(snapshot)
        changes_before = connection.total_changes
        if type(changes_before) is not int or changes_before < 0:
            raise RuntimeError("SQLite returned an invalid total change count")
        stored, inserted = SQLiteEventStore._append_in_transaction(
            self,
            connection,
            frozen,
            expected_version,
            expected_global_position,
        )
        if not inserted:
            raise EventStoreIntegrityError("verified stored event append requires a fresh row")
        statement_changes_row = connection.execute("SELECT changes() AS change_count").fetchone()
        if (
            statement_changes_row is None
            or type(statement_changes_row["change_count"]) is not int
            or statement_changes_row["change_count"] != 1
        ):
            raise EventStoreIntegrityError(
                "verified stored event append did not perform one fresh insert"
            )
        changes_after = connection.total_changes
        if type(changes_after) is not int or changes_after != changes_before + 1:
            raise EventStoreIntegrityError(
                "verified stored event append changed an unexpected row count"
            )
        verified = SQLiteEventStore._verify_stored_event_envelope_in_transaction(
            self,
            connection,
            frozen,
            stored,
        )
        return stored, inserted, verified

    @staticmethod
    def _reject_generic_reserved_result_event(event: DomainEvent) -> None:
        """Keep result authority vocabulary out of every generic append path."""

        if event.event_type == _RESERVED_RESULT_EVENT_TYPE:
            raise ReservedResultEventError()
        if event.event_type != _RESERVED_RESULT_TERMINAL_EVENT_TYPE:
            return
        for key in event.payload:
            normalized = unicodedata.normalize(
                "NFKD",
                unicodedata.normalize("NFKC", key).casefold(),
            )
            token = "".join(
                character
                for character in normalized
                if "a" <= character <= "z" or "0" <= character <= "9"
            )
            if token in _RESERVED_RESULT_TERMINAL_KEY_TOKENS:
                raise ReservedResultEventError()

    @staticmethod
    def _snapshot_invocation_job_spec(spec: InvocationJobSpec) -> InvocationJobSpec:
        """Copy an exact immutable job request before acquiring the write transaction."""

        if type(spec) is not InvocationJobSpec:
            raise TypeError("spec must be an exact InvocationJobSpec")
        return InvocationJobSpec(
            session_id=object.__getattribute__(spec, "session_id"),
            plan_id=object.__getattribute__(spec, "plan_id"),
            task_id=object.__getattribute__(spec, "task_id"),
            agent_id=object.__getattribute__(spec, "agent_id"),
            idempotency_key=object.__getattribute__(spec, "idempotency_key"),
            payload_digest=object.__getattribute__(spec, "payload_digest"),
            invocation_id=object.__getattribute__(spec, "invocation_id"),
            priority=object.__getattribute__(spec, "priority"),
            max_attempts=object.__getattribute__(spec, "max_attempts"),
            available_at=object.__getattribute__(spec, "available_at"),
        )

    @staticmethod
    def _invocation_event_manifest_sha256(
        batch: Tuple[_EventWriteSnapshot, ...],
    ) -> str:
        """Bind the full ordered immutable event request without storing its payload twice."""

        manifest = [
            [
                item.event.stream_id,
                item.event.event_type,
                item.event.actor_id,
                item.event.event_id,
                item.event.timestamp,
                item.event.correlation_id,
                item.event.causation_id,
                item.event.idempotency_key,
                item.payload_json,
            ]
            for item in batch
        ]
        encoded = json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _invocation_job_binding_sha256(spec: InvocationJobSpec) -> str:
        """Bind every immutable enqueue field using the enqueue timestamp semantics."""

        requested_available_at = (
            _normalize_invocation_timestamp(spec.available_at, "available_at")
            if spec.available_at is not None
            else None
        )
        encoded = json.dumps(
            [
                spec.invocation_id,
                spec.session_id,
                spec.plan_id,
                spec.task_id,
                spec.agent_id,
                spec.idempotency_key,
                spec.payload_digest,
                spec.priority,
                spec.max_attempts,
                requested_available_at,
            ],
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _invocation_job_spec_from_job(job: InvocationJob) -> InvocationJobSpec:
        """Recover the immutable portion of a validated persisted job."""

        return InvocationJobSpec(
            invocation_id=job.invocation_id,
            session_id=job.session_id,
            plan_id=job.plan_id,
            task_id=job.task_id,
            agent_id=job.agent_id,
            idempotency_key=job.idempotency_key,
            payload_digest=job.payload_digest,
            priority=job.priority,
            max_attempts=job.max_attempts,
            available_at=job.requested_available_at,
        )

    @staticmethod
    def _row_to_invocation_admission_receipt(
        row: sqlite3.Row,
    ) -> _InvocationAdmissionReceipt:
        """Decode one receipt strictly; durable defects are integrity failures."""

        try:
            receipt_format = _persisted_text(
                row["receipt_format"],
                "invocation admission receipt format",
                required=True,
            )
            if receipt_format != _INVOCATION_ADMISSION_RECEIPT_FORMAT:
                raise ValueError("persisted invocation admission receipt format is unsupported")
            invocation_id = _persisted_text(
                row["invocation_id"], "invocation admission invocation_id", required=True
            )
            session_id = _persisted_text(
                row["session_id"], "invocation admission session_id", required=True
            )
            task_id = _persisted_text(row["task_id"], "invocation admission task_id", required=True)
            stream_id = _persisted_text(
                row["stream_id"], "invocation admission stream_id", required=True
            )
            job_idempotency_key = _persisted_text(
                row["job_idempotency_key"],
                "invocation admission job idempotency key",
                required=True,
            )
            original_version = _persisted_integer(
                row["original_version"],
                "invocation admission original version",
            )
            event_count = _persisted_integer(
                row["event_count"],
                "invocation admission event count",
                minimum=1,
            )
            event_ids_json = _persisted_text(
                row["event_ids_json"],
                "invocation admission event IDs",
                required=True,
            )
            decoded_event_ids = json.loads(
                event_ids_json,
                parse_constant=_reject_json_constant,
            )
            if type(decoded_event_ids) is not list or len(decoded_event_ids) != event_count:
                raise ValueError("persisted invocation admission event IDs are malformed")
            event_ids = tuple(
                _persisted_text(
                    event_id,
                    "invocation admission event ID",
                    required=True,
                )
                for event_id in decoded_event_ids
            )
            if len(set(event_ids)) != len(event_ids):
                raise ValueError("persisted invocation admission repeats an event ID")
            canonical_event_ids = json.dumps(
                list(event_ids),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if canonical_event_ids != event_ids_json:
                raise ValueError("persisted invocation admission event IDs are not canonical")
            first_sequence = _persisted_integer(
                row["first_sequence"],
                "invocation admission first sequence",
                minimum=1,
            )
            last_sequence = _persisted_integer(
                row["last_sequence"],
                "invocation admission last sequence",
                minimum=1,
            )
            first_global_position = _persisted_integer(
                row["first_global_position"],
                "invocation admission first global position",
                minimum=1,
            )
            last_global_position = _persisted_integer(
                row["last_global_position"],
                "invocation admission last global position",
                minimum=1,
            )
            event_manifest_sha256 = _persisted_text(
                row["event_manifest_sha256"],
                "invocation admission event manifest digest",
                required=True,
            )
            job_binding_sha256 = _persisted_text(
                row["job_binding_sha256"],
                "invocation admission job binding digest",
                required=True,
            )
            for digest in (event_manifest_sha256, job_binding_sha256):
                if len(digest) != 64 or any(
                    character not in "0123456789abcdef" for character in digest
                ):
                    raise ValueError("persisted invocation admission digest is not canonical")
            admitted_at = _persisted_text(
                row["admitted_at"],
                "invocation admission admitted_at",
                required=True,
            )
            if _normalize_invocation_timestamp(admitted_at, "admitted_at") != admitted_at:
                raise ValueError("persisted invocation admission timestamp is not canonical")
            if stream_id != "session:%s" % session_id:
                raise ValueError("persisted invocation admission stream binding is malformed")
            if first_sequence != original_version + 1:
                raise ValueError("persisted invocation admission first sequence is malformed")
            if last_sequence != original_version + event_count:
                raise ValueError("persisted invocation admission last sequence is malformed")
            if last_global_position != first_global_position + event_count - 1:
                raise ValueError("persisted invocation admission global range is malformed")
            return _InvocationAdmissionReceipt(
                invocation_id=invocation_id,
                session_id=session_id,
                task_id=task_id,
                stream_id=stream_id,
                job_idempotency_key=job_idempotency_key,
                original_version=original_version,
                event_ids=event_ids,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
                first_global_position=first_global_position,
                last_global_position=last_global_position,
                event_manifest_sha256=event_manifest_sha256,
                job_binding_sha256=job_binding_sha256,
                admitted_at=admitted_at,
            )
        except EventStoreIntegrityError:
            raise
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EventStoreIntegrityError(
                "persisted invocation admission receipt is malformed"
            ) from exc

    def _validate_invocation_admission_receipt(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Tuple[_InvocationAdmissionReceipt, Tuple[StoredEvent, ...], InvocationJob]:
        """Prove that a receipt still binds the exact durable event/job state."""

        receipt = self._row_to_invocation_admission_receipt(row)
        job_row = connection.execute(
            "SELECT * FROM invocation_jobs WHERE invocation_id = ?",
            (receipt.invocation_id,),
        ).fetchone()
        if job_row is None:
            raise EventStoreIntegrityError("invocation admission receipt refers to a missing job")
        try:
            job = SQLiteInvocationAttemptStore._row_to_job(job_row)
            durable_job_digest = self._invocation_job_binding_sha256(
                self._invocation_job_spec_from_job(job)
            )
        except (InvocationIntegrityError, TypeError, ValueError) as exc:
            raise EventStoreIntegrityError(
                "invocation admission receipt refers to a malformed job"
            ) from exc
        if (
            job.session_id != receipt.session_id
            or job.task_id != receipt.task_id
            or job.idempotency_key != receipt.job_idempotency_key
            or job.created_at != receipt.admitted_at
            or durable_job_digest != receipt.job_binding_sha256
        ):
            raise EventStoreIntegrityError(
                "invocation admission receipt job binding is inconsistent"
            )

        stored_events: List[StoredEvent] = []
        stored_snapshots: List[_EventWriteSnapshot] = []
        for offset, event_id in enumerate(receipt.event_ids):
            event_row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if event_row is None:
                raise EventStoreIntegrityError(
                    "invocation admission receipt refers to a missing event"
                )
            try:
                stored = self._row_to_event(event_row)
                snapshot = self._snapshot_event(stored.event)
            except (EventStoreJsonError, TypeError, ValueError) as exc:
                raise EventStoreIntegrityError(
                    "invocation admission receipt refers to a malformed event"
                ) from exc
            if (
                stored.event.stream_id != receipt.stream_id
                or stored.sequence != receipt.first_sequence + offset
                or stored.global_position != receipt.first_global_position + offset
            ):
                raise EventStoreIntegrityError(
                    "invocation admission receipt event range is inconsistent"
                )
            stored_events.append(stored)
            stored_snapshots.append(snapshot)
        if (
            stored_events[-1].sequence != receipt.last_sequence
            or stored_events[-1].global_position != receipt.last_global_position
            or self._invocation_event_manifest_sha256(tuple(stored_snapshots))
            != receipt.event_manifest_sha256
        ):
            raise EventStoreIntegrityError(
                "invocation admission receipt event manifest is inconsistent"
            )
        return receipt, tuple(stored_events), job

    def _canonical_invocation_admission_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        invocation_id: str,
    ) -> Tuple[_InvocationAdmissionReceipt, TaskInvocationAdmissionRequest, InvocationJob]:
        """Prove one v4 receipt is also the exact canonical semantic admission."""

        try:
            admission, stored_events, job = self._validate_invocation_admission_receipt(
                connection,
                row,
            )
            if admission.invocation_id != invocation_id:
                raise ValueError("invocation admission identity does not match the request")
            request = TaskInvocationAdmissionRequest.from_components(
                tuple(stored.event for stored in stored_events),
                self._invocation_job_spec_from_job(job),
            )
            if request.manifest.invocation_id != invocation_id:
                raise ValueError("canonical admission manifest identity is inconsistent")
            if request.manifest.canonical_digest() != job.payload_digest:
                raise ValueError("canonical admission manifest digest is inconsistent")
        except (EventStoreIntegrityError, InvocationIntegrityError, TypeError, ValueError):
            raise InvocationStartConflictError() from None
        return admission, request, job

    def _canonical_scoped_invocation_admission_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        invocation_id: str,
    ) -> Tuple[
        _InvocationAdmissionReceipt,
        ScopedTaskInvocationAdmissionRequestV2,
        InvocationJob,
    ]:
        """Prove one v4 receipt is the exact scoped schema-2 semantic admission."""

        try:
            admission, stored_events, job = self._validate_invocation_admission_receipt(
                connection,
                row,
            )
            if admission.invocation_id != invocation_id:
                raise ValueError("scoped invocation admission identity does not match")
            request = ScopedTaskInvocationAdmissionRequestV2.from_components(
                tuple(stored.event for stored in stored_events),
                self._invocation_job_spec_from_job(job),
            )
            if request.manifest.invocation_id != invocation_id:
                raise ValueError("scoped admission manifest identity is inconsistent")
            if request.manifest.canonical_digest() != job.payload_digest:
                raise ValueError("scoped admission manifest digest is inconsistent")
        except (EventStoreIntegrityError, InvocationIntegrityError, TypeError, ValueError):
            raise InvocationStartConflictError() from None
        return admission, request, job

    def _invocation_start_candidates_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
        task_id: str,
    ) -> Tuple[StoredEvent, ...]:
        """Find every database-wide start-like row relevant to this task."""

        canonical_key = "invocation-start:%s:1" % invocation_id
        legacy_key = "invocation-started:%s" % task_id
        rows = connection.execute(
            """
            SELECT * FROM events
            WHERE event_type = ?
               OR idempotency_key = ?
               OR idempotency_key = ?
            ORDER BY global_position
            """,
            (
                TASK_INVOCATION_STARTED_EVENT_TYPE,
                canonical_key,
                legacy_key,
            ),
        ).fetchall()
        relevant: List[StoredEvent] = []
        try:
            for row in rows:
                stored = self._row_to_event(row)
                event = stored.event
                payload = event.payload
                payload_matches = (
                    payload.get("invocationId") == invocation_id
                    or payload.get("invocation_id") == invocation_id
                    or payload.get("taskId") == task_id
                    or payload.get("task_id") == task_id
                )
                if event.idempotency_key in {canonical_key, legacy_key} or payload_matches:
                    relevant.append(stored)
        except (EventStoreIntegrityError, TypeError, ValueError):
            raise InvocationStartConflictError() from None
        if len(relevant) > 1:
            raise InvocationStartConflictError() from None
        return tuple(relevant)

    @staticmethod
    def _validate_unstarted_invocation_in_transaction(
        connection: sqlite3.Connection,
        job: InvocationJob,
    ) -> None:
        """Require the exact zero-attempt queued state before authority allocation."""

        rows = connection.execute(
            """
            SELECT * FROM invocation_attempts
            WHERE invocation_id = ?
            ORDER BY attempt_number
            LIMIT 2
            """,
            (job.invocation_id,),
        ).fetchall()
        try:
            SQLiteInvocationAttemptStore._validate_recovery_snapshot(
                job,
                None,
                attempt_count=len(rows),
            )
        except InvocationIntegrityError:
            raise InvocationStartConflictError() from None
        if (
            rows
            or job.status is not InvocationStatus.QUEUED
            or job.max_attempts != 1
            or job.attempts_started != 0
            or job.lease_epoch != 0
            or job.lease_owner is not None
            or job.lease_token_digest is not None
            or job.lease_expires_at is not None
            or job.heartbeat_at is not None
            or job.result_ref is not None
            or job.last_error is not None
            or job.finished_at is not None
        ):
            raise InvocationStartConflictError() from None

    def _validate_invocation_start_readback(
        self,
        connection: sqlite3.Connection,
        admission: _InvocationAdmissionReceipt,
        request: TaskInvocationAdmissionRequest,
        job: InvocationJob,
        stored: StoredEvent,
        *,
        fresh: bool,
    ) -> _InvocationStartReadback:
        """Validate one schema-2 event against admission, job, and exactly one attempt."""

        if type(fresh) is not bool:
            raise TypeError("fresh must be a boolean")
        manifest = request.manifest
        event = stored.event
        canonical_key = "invocation-start:%s:1" % manifest.invocation_id
        try:
            evidence = InvocationStartEvidenceV2.from_event_payload(
                event.event_type,
                event.payload,
            )
            if (
                event.stream_id != admission.stream_id
                or event.actor_id != CANONICAL_ORCHESTRATOR_ACTOR_ID
                or event.idempotency_key != canonical_key
                or event.timestamp != evidence.claimed_at
                or event.correlation_id != manifest.correlation_id
                or event.causation_id != manifest.causation_id
                or stored.sequence <= admission.last_sequence
                or stored.global_position <= admission.last_global_position
            ):
                raise ValueError("invocation-start event envelope is inconsistent")
            if (
                evidence.schema_version != 2
                or evidence.invocation_id != manifest.invocation_id
                or evidence.session_id != manifest.session_id
                or evidence.plan_id != manifest.plan_id
                or evidence.task_id != manifest.task_id
                or evidence.agent_id != manifest.agent_id
                or evidence.job_idempotency_key != manifest.job_idempotency_key
                or evidence.attempt_number != 1
                or evidence.lease_epoch != 1
                or evidence.manifest_digest != manifest.canonical_digest()
                or evidence.manifest_digest != job.payload_digest
                or evidence.envelope_digest != manifest.envelope_digest
                or evidence.context_digest != manifest.context_digest
                or evidence.authorization_digest != manifest.authorization_digest
                or evidence.runtime_revision != manifest.runtime_revision
                or evidence.correlation_id != manifest.correlation_id
                or evidence.causation_id != manifest.causation_id
            ):
                raise ValueError("invocation-start evidence is inconsistent")

            attempt_rows = connection.execute(
                """
                SELECT * FROM invocation_attempts
                WHERE invocation_id = ?
                ORDER BY attempt_number
                LIMIT 2
                """,
                (manifest.invocation_id,),
            ).fetchall()
            if len(attempt_rows) != 1:
                raise ValueError("invocation start does not have exactly one attempt")
            attempt = SQLiteInvocationAttemptStore._row_to_attempt(attempt_rows[0])
            SQLiteInvocationAttemptStore._validate_recovery_snapshot(
                job,
                attempt,
                attempt_count=1,
            )
            if (
                job.max_attempts != 1
                or job.attempts_started != 1
                or job.lease_epoch != 1
                or attempt.attempt_id != evidence.attempt_id
                or attempt.invocation_id != evidence.invocation_id
                or attempt.attempt_number != evidence.attempt_number
                or attempt.lease_epoch != evidence.lease_epoch
                or attempt.worker_id != evidence.worker_id
                or attempt.lease_token_digest != evidence.lease_token_digest
                or attempt.started_at != evidence.claimed_at
                or attempt.heartbeat_at < evidence.claimed_at
                or attempt.lease_expires_at < evidence.lease_expires_at
                or job.updated_at < evidence.claimed_at
            ):
                raise ValueError("invocation-start attempt binding is inconsistent")
            if fresh and (
                job.status is not InvocationStatus.RUNNING
                or attempt.status is not AttemptStatus.RUNNING
                or job.lease_owner != evidence.worker_id
                or job.lease_token_digest != evidence.lease_token_digest
                or job.heartbeat_at != evidence.claimed_at
                or job.lease_expires_at != evidence.lease_expires_at
                or job.updated_at != evidence.claimed_at
                or attempt.heartbeat_at != evidence.claimed_at
                or attempt.lease_expires_at != evidence.lease_expires_at
            ):
                raise ValueError("fresh invocation-start ownership is inconsistent")
            receipt = InvocationStartReceipt(
                event_id=event.event_id,
                stream_id=event.stream_id,
                sequence=stored.sequence,
                global_position=stored.global_position,
                evidence=evidence,
            )
        except (
            EventStoreIntegrityError,
            InvocationIntegrityError,
            TypeError,
            ValueError,
        ):
            raise InvocationStartConflictError() from None
        return _InvocationStartReadback(
            admission=admission,
            request=request,
            job=job,
            attempt=attempt,
            event=stored,
            receipt=receipt,
        )

    def _load_invocation_start_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
        *,
        fresh: bool = False,
    ) -> Optional[_InvocationStartState]:
        """Load an unknown, canonically unstarted, or validated started state."""

        receipt_rows = connection.execute(
            """
            SELECT * FROM invocation_admissions
            WHERE invocation_id = ?
            LIMIT 2
            """,
            (invocation_id,),
        ).fetchall()
        if not receipt_rows:
            job_row = connection.execute(
                "SELECT 1 FROM invocation_jobs WHERE invocation_id = ? LIMIT 1",
                (invocation_id,),
            ).fetchone()
            event_row = connection.execute(
                """
                SELECT 1 FROM events
                WHERE idempotency_key IN (?, ?)
                LIMIT 1
                """,
                (
                    "execution-request:%s" % invocation_id,
                    "invocation-start:%s:1" % invocation_id,
                ),
            ).fetchone()
            if job_row is not None or event_row is not None:
                raise InvocationStartConflictError() from None
            return None
        if len(receipt_rows) != 1:
            raise InvocationStartConflictError() from None
        admission, request, job = self._canonical_invocation_admission_in_transaction(
            connection,
            receipt_rows[0],
            invocation_id=invocation_id,
        )
        candidates = self._invocation_start_candidates_in_transaction(
            connection,
            request.manifest.invocation_id,
            request.manifest.task_id,
        )
        if not candidates:
            self._validate_unstarted_invocation_in_transaction(connection, job)
            return _InvocationStartAdmission(admission, request, job)
        return self._validate_invocation_start_readback(
            connection,
            admission,
            request,
            job,
            candidates[0],
            fresh=fresh,
        )

    def _validate_scoped_invocation_start_readback(
        self,
        connection: sqlite3.Connection,
        admission: _InvocationAdmissionReceipt,
        request: ScopedTaskInvocationAdmissionRequestV2,
        job: InvocationJob,
        stored: StoredEvent,
        *,
        fresh: bool,
    ) -> _ScopedInvocationStartReadback:
        """Validate one schema-3 start against scoped admission, job and attempt."""

        if type(fresh) is not bool:
            raise TypeError("fresh must be a boolean")
        manifest = request.manifest
        event = stored.event
        canonical_key = "invocation-start:%s:1" % manifest.invocation_id
        try:
            evidence = ScopedInvocationStartEvidenceV3.from_event_payload(
                event.event_type,
                event.payload,
            )
            if (
                event.stream_id != admission.stream_id
                or event.actor_id != CANONICAL_ORCHESTRATOR_ACTOR_ID
                or event.idempotency_key != canonical_key
                or event.timestamp != evidence.claimed_at
                or event.correlation_id != manifest.correlation_id
                or event.causation_id != manifest.causation_id
                or stored.sequence <= admission.last_sequence
                or stored.global_position <= admission.last_global_position
            ):
                raise ValueError("scoped invocation-start event envelope is inconsistent")
            if (
                evidence.schema_version != 3
                or evidence.tenant_id != manifest.tenant_id
                or evidence.workspace_id != manifest.workspace_id
                or evidence.invocation_id != manifest.invocation_id
                or evidence.session_id != manifest.session_id
                or evidence.plan_id != manifest.plan_id
                or evidence.task_id != manifest.task_id
                or evidence.agent_id != manifest.agent_id
                or evidence.job_idempotency_key != manifest.job_idempotency_key
                or evidence.attempt_number != 1
                or evidence.lease_epoch != 1
                or evidence.manifest_digest != manifest.canonical_digest()
                or evidence.manifest_digest != job.payload_digest
                or evidence.envelope_digest != manifest.envelope_digest
                or evidence.context_digest != manifest.context_digest
                or evidence.authorization_digest != manifest.authorization_digest
                or evidence.runtime_revision != manifest.runtime_revision
                or evidence.correlation_id != manifest.correlation_id
                or evidence.causation_id != manifest.causation_id
            ):
                raise ValueError("scoped invocation-start evidence is inconsistent")

            attempt_rows = connection.execute(
                """
                SELECT * FROM invocation_attempts
                WHERE invocation_id = ?
                ORDER BY attempt_number
                LIMIT 2
                """,
                (manifest.invocation_id,),
            ).fetchall()
            if len(attempt_rows) != 1:
                raise ValueError("scoped invocation start does not have exactly one attempt")
            attempt = SQLiteInvocationAttemptStore._row_to_attempt(attempt_rows[0])
            SQLiteInvocationAttemptStore._validate_recovery_snapshot(
                job,
                attempt,
                attempt_count=1,
            )
            if (
                job.max_attempts != 1
                or job.attempts_started != 1
                or job.lease_epoch != 1
                or attempt.attempt_id != evidence.attempt_id
                or attempt.invocation_id != evidence.invocation_id
                or attempt.attempt_number != evidence.attempt_number
                or attempt.lease_epoch != evidence.lease_epoch
                or attempt.worker_id != evidence.worker_id
                or attempt.lease_token_digest != evidence.lease_token_digest
                or attempt.started_at != evidence.claimed_at
                or attempt.heartbeat_at < evidence.claimed_at
                or attempt.lease_expires_at < evidence.lease_expires_at
                or job.updated_at < evidence.claimed_at
            ):
                raise ValueError("scoped invocation-start attempt binding is inconsistent")
            if fresh and (
                job.status is not InvocationStatus.RUNNING
                or attempt.status is not AttemptStatus.RUNNING
                or job.lease_owner != evidence.worker_id
                or job.lease_token_digest != evidence.lease_token_digest
                or job.heartbeat_at != evidence.claimed_at
                or job.lease_expires_at != evidence.lease_expires_at
                or job.updated_at != evidence.claimed_at
                or attempt.heartbeat_at != evidence.claimed_at
                or attempt.lease_expires_at != evidence.lease_expires_at
            ):
                raise ValueError("fresh scoped invocation-start ownership is inconsistent")
            receipt = ScopedInvocationStartReceiptV3(
                event_id=event.event_id,
                stream_id=event.stream_id,
                sequence=stored.sequence,
                global_position=stored.global_position,
                evidence=evidence,
            )
        except (
            EventStoreIntegrityError,
            InvocationIntegrityError,
            TypeError,
            ValueError,
        ):
            raise InvocationStartConflictError() from None
        return _ScopedInvocationStartReadback(
            admission=admission,
            request=request,
            job=job,
            attempt=attempt,
            event=stored,
            receipt=receipt,
        )

    def _read_invocation_start_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
        *,
        fresh: bool = False,
    ) -> Optional[_InvocationStartReadback]:
        """Read only validated start evidence from one transaction snapshot."""

        state = self._load_invocation_start_in_transaction(
            connection,
            invocation_id,
            fresh=fresh,
        )
        if type(state) is _InvocationStartReadback:
            return state
        return None

    def _load_scoped_invocation_start_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
        *,
        fresh: bool = False,
    ) -> Optional[_ScopedInvocationStartState]:
        """Load an unknown, scoped-unstarted, or validated scoped-started state."""

        receipt_rows = connection.execute(
            """
            SELECT * FROM invocation_admissions
            WHERE invocation_id = ?
            LIMIT 2
            """,
            (invocation_id,),
        ).fetchall()
        if not receipt_rows:
            job_row = connection.execute(
                "SELECT 1 FROM invocation_jobs WHERE invocation_id = ? LIMIT 1",
                (invocation_id,),
            ).fetchone()
            event_row = connection.execute(
                """
                SELECT 1 FROM events
                WHERE idempotency_key IN (?, ?)
                LIMIT 1
                """,
                (
                    "execution-request:%s" % invocation_id,
                    "invocation-start:%s:1" % invocation_id,
                ),
            ).fetchone()
            if job_row is not None or event_row is not None:
                raise InvocationStartConflictError() from None
            return None
        if len(receipt_rows) != 1:
            raise InvocationStartConflictError() from None
        admission, request, job = self._canonical_scoped_invocation_admission_in_transaction(
            connection,
            receipt_rows[0],
            invocation_id=invocation_id,
        )
        candidates = self._invocation_start_candidates_in_transaction(
            connection,
            request.manifest.invocation_id,
            request.manifest.task_id,
        )
        if not candidates:
            self._validate_unstarted_invocation_in_transaction(connection, job)
            return _ScopedInvocationStartAdmission(admission, request, job)
        return self._validate_scoped_invocation_start_readback(
            connection,
            admission,
            request,
            job,
            candidates[0],
            fresh=fresh,
        )

    def _read_scoped_invocation_start_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
        *,
        fresh: bool = False,
    ) -> Optional[_ScopedInvocationStartReadback]:
        """Read only validated scoped start evidence from one transaction snapshot."""

        state = self._load_scoped_invocation_start_in_transaction(
            connection,
            invocation_id,
            fresh=fresh,
        )
        if type(state) is _ScopedInvocationStartReadback:
            return state
        return None

    def _claim_invocation_start_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        expected_version: int,
    ) -> _InvocationStartResult:
        """Mint and persist first-start authority, or observe an existing receipt."""

        state = self._load_invocation_start_in_transaction(connection, invocation_id)
        if state is None:
            raise InvocationStartConflictError() from None
        if type(state) is _InvocationStartReadback:
            if expected_version != state.event.sequence - 1:
                raise ConcurrencyError(
                    "invocation start expected stream version %d but began at %d"
                    % (expected_version, state.event.sequence - 1)
                )
            return InvocationStartObserved(state.receipt)
        if type(state) is not _InvocationStartAdmission:  # pragma: no cover - closed union.
            raise InvocationStartConflictError() from None

        version_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
            (state.admission.stream_id,),
        ).fetchone()
        try:
            current_version = _persisted_integer(
                version_row["version"],
                "invocation-start stream version",
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise InvocationStartConflictError() from None
        if current_version != expected_version:
            raise ConcurrencyError(
                "stream %s expected version %d but is %d"
                % (state.admission.stream_id, expected_version, current_version)
            )

        normalized_now = _normalize_invocation_timestamp(self._now(), "clock")
        self._require_current_process()
        deadline = _invocation_lease_deadline(normalized_now, lease_seconds)
        claim_request = _InvocationClaimRequest(
            worker_id=worker_id,
            invocation_id=invocation_id,
        )
        try:
            candidate = _select_first_claim_candidate_in_transaction(
                connection,
                claim_request,
                now=normalized_now,
            )
        except InvocationIntegrityError:
            raise InvocationStartConflictError() from None
        if candidate is None or candidate != state.job:
            raise InvocationStartConflictError() from None

        try:
            attempt_id = new_id("attempt")
            self._require_current_process()
            attempt_id = _caller_invocation_identity(attempt_id, "attempt_id provider result")
            if (
                connection.execute(
                    "SELECT 1 FROM invocation_attempts WHERE attempt_id = ? LIMIT 1",
                    (attempt_id,),
                ).fetchone()
                is not None
            ):
                raise InvocationStartConflictError() from None

            start_event_id = new_id("evt")
            self._require_current_process()
            start_event_id = _caller_invocation_identity(
                start_event_id,
                "start event_id provider result",
            )
            if (
                connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ? LIMIT 1",
                    (start_event_id,),
                ).fetchone()
                is not None
            ):
                raise InvocationStartConflictError() from None

            lease_token = secrets.token_urlsafe(32)
            self._require_current_process()
            lease_token = _caller_invocation_identity(
                lease_token,
                "lease token provider result",
            )
            lease = _claim_first_invocation_in_transaction(
                connection,
                claim_request,
                now=normalized_now,
                deadline=deadline,
                attempt_id=attempt_id,
                lease_token=lease_token,
            )
            if lease is None or (
                lease.invocation_id != candidate.invocation_id
                or lease.session_id != candidate.session_id
                or lease.plan_id != candidate.plan_id
                or lease.task_id != candidate.task_id
                or lease.agent_id != candidate.agent_id
                or lease.idempotency_key != candidate.idempotency_key
                or lease.payload_digest != candidate.payload_digest
                or lease.attempt_id != attempt_id
                or lease.attempt_number != 1
                or lease.max_attempts != 1
                or lease.lease_epoch != 1
                or lease.worker_id != worker_id
                or lease.lease_token != lease_token
                or lease.claimed_at != normalized_now
                or lease.lease_expires_at != deadline
            ):
                raise InvocationStartConflictError() from None

            manifest = state.request.manifest
            evidence = InvocationStartEvidenceV2(
                schema_version=2,
                invocation_id=manifest.invocation_id,
                session_id=manifest.session_id,
                plan_id=manifest.plan_id,
                task_id=manifest.task_id,
                agent_id=manifest.agent_id,
                job_idempotency_key=manifest.job_idempotency_key,
                attempt_id=lease.attempt_id,
                attempt_number=lease.attempt_number,
                lease_epoch=lease.lease_epoch,
                worker_id=lease.worker_id,
                lease_token_digest=self._lease_token_digest(lease_token),
                claimed_at=lease.claimed_at,
                lease_expires_at=lease.lease_expires_at,
                manifest_digest=manifest.canonical_digest(),
                envelope_digest=manifest.envelope_digest,
                context_digest=manifest.context_digest,
                authorization_digest=manifest.authorization_digest,
                runtime_revision=manifest.runtime_revision,
                correlation_id=manifest.correlation_id,
                causation_id=manifest.causation_id,
            )
            event = DomainEvent(
                stream_id=state.admission.stream_id,
                event_type=TASK_INVOCATION_STARTED_EVENT_TYPE,
                payload=evidence.to_dict(),
                actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
                event_id=start_event_id,
                timestamp=lease.claimed_at,
                correlation_id=manifest.correlation_id,
                causation_id=manifest.causation_id,
                idempotency_key="invocation-start:%s:1" % invocation_id,
            )
            stored, inserted = self._append_in_transaction(
                connection,
                self._snapshot_event(event),
                expected_version,
            )
            if not inserted or stored.sequence != expected_version + 1:
                raise InvocationStartConflictError() from None
            readback = self._read_invocation_start_in_transaction(
                connection,
                invocation_id,
                fresh=True,
            )
            if (
                readback is None
                or readback.event != stored
                or readback.receipt.evidence.lease_token_digest
                != self._lease_token_digest(lease_token)
            ):
                raise InvocationStartConflictError() from None
            return InvocationStartClaimed(readback.receipt, lease)
        except sqlite3.Error:
            raise
        except (
            ConcurrencyError,
            EventStoreIntegrityError,
            InvocationIntegrityError,
            InvocationStartConflictError,
            TypeError,
            ValueError,
        ) as error:
            _detach_exception(error)
            raise _EventStoreStartErrorSignal(
                "conflict",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except BaseException as error:
            if _trusted_event_store_process_signal(error) or _is_exact_control_signal(error):
                raise
            _detach_exception(error)
            raise _EventStoreStartErrorSignal(
                "transaction",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None

    def _claim_scoped_invocation_start_in_transaction(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        expected_version: int,
    ) -> _ScopedInvocationStartResult:
        """Mint scoped first-start authority, or observe an existing schema-3 receipt."""

        state = self._load_scoped_invocation_start_in_transaction(connection, invocation_id)
        if state is None:
            raise InvocationStartConflictError() from None
        if (
            state.request.manifest.tenant_id != tenant_id
            or state.request.manifest.workspace_id != workspace_id
        ):
            raise InvocationStartConflictError() from None
        if type(state) is _ScopedInvocationStartReadback:
            if expected_version != state.event.sequence - 1:
                raise ConcurrencyError(
                    "scoped invocation start expected stream version %d but began at %d"
                    % (expected_version, state.event.sequence - 1)
                )
            return ScopedInvocationStartObservedV3(state.receipt)
        if type(state) is not _ScopedInvocationStartAdmission:  # pragma: no cover
            raise InvocationStartConflictError() from None

        version_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS version FROM events WHERE stream_id = ?",
            (state.admission.stream_id,),
        ).fetchone()
        try:
            current_version = _persisted_integer(
                version_row["version"],
                "scoped invocation-start stream version",
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise InvocationStartConflictError() from None
        if current_version != expected_version:
            raise ConcurrencyError(
                "stream %s expected version %d but is %d"
                % (state.admission.stream_id, expected_version, current_version)
            )

        normalized_now = _normalize_invocation_timestamp(self._now(), "clock")
        self._require_current_process()
        deadline = _invocation_lease_deadline(normalized_now, lease_seconds)
        claim_request = _InvocationClaimRequest(
            worker_id=worker_id,
            invocation_id=invocation_id,
        )
        try:
            candidate = _select_first_claim_candidate_in_transaction(
                connection,
                claim_request,
                now=normalized_now,
            )
        except InvocationIntegrityError:
            raise InvocationStartConflictError() from None
        if candidate is None or candidate != state.job:
            raise InvocationStartConflictError() from None

        try:
            attempt_id = new_id("attempt")
            self._require_current_process()
            attempt_id = _caller_invocation_identity(attempt_id, "attempt_id provider result")
            if (
                connection.execute(
                    "SELECT 1 FROM invocation_attempts WHERE attempt_id = ? LIMIT 1",
                    (attempt_id,),
                ).fetchone()
                is not None
            ):
                raise InvocationStartConflictError() from None

            start_event_id = new_id("evt")
            self._require_current_process()
            start_event_id = _caller_invocation_identity(
                start_event_id,
                "start event_id provider result",
            )
            if (
                connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ? LIMIT 1",
                    (start_event_id,),
                ).fetchone()
                is not None
            ):
                raise InvocationStartConflictError() from None

            lease_token = secrets.token_urlsafe(32)
            self._require_current_process()
            lease_token = _caller_invocation_identity(
                lease_token,
                "lease token provider result",
            )
            lease = _claim_first_invocation_in_transaction(
                connection,
                claim_request,
                now=normalized_now,
                deadline=deadline,
                attempt_id=attempt_id,
                lease_token=lease_token,
            )
            if lease is None or (
                lease.invocation_id != candidate.invocation_id
                or lease.session_id != candidate.session_id
                or lease.plan_id != candidate.plan_id
                or lease.task_id != candidate.task_id
                or lease.agent_id != candidate.agent_id
                or lease.idempotency_key != candidate.idempotency_key
                or lease.payload_digest != candidate.payload_digest
                or lease.attempt_id != attempt_id
                or lease.attempt_number != 1
                or lease.max_attempts != 1
                or lease.lease_epoch != 1
                or lease.worker_id != worker_id
                or lease.lease_token != lease_token
                or lease.claimed_at != normalized_now
                or lease.lease_expires_at != deadline
            ):
                raise InvocationStartConflictError() from None

            manifest = state.request.manifest
            evidence = ScopedInvocationStartEvidenceV3(
                schema_version=3,
                tenant_id=manifest.tenant_id,
                workspace_id=manifest.workspace_id,
                invocation_id=manifest.invocation_id,
                session_id=manifest.session_id,
                plan_id=manifest.plan_id,
                task_id=manifest.task_id,
                agent_id=manifest.agent_id,
                job_idempotency_key=manifest.job_idempotency_key,
                attempt_id=lease.attempt_id,
                attempt_number=lease.attempt_number,
                lease_epoch=lease.lease_epoch,
                worker_id=lease.worker_id,
                lease_token_digest=self._lease_token_digest(lease_token),
                claimed_at=lease.claimed_at,
                lease_expires_at=lease.lease_expires_at,
                manifest_digest=manifest.canonical_digest(),
                envelope_digest=manifest.envelope_digest,
                context_digest=manifest.context_digest,
                authorization_digest=manifest.authorization_digest,
                runtime_revision=manifest.runtime_revision,
                correlation_id=manifest.correlation_id,
                causation_id=manifest.causation_id,
            )
            event = DomainEvent(
                stream_id=state.admission.stream_id,
                event_type=TASK_INVOCATION_STARTED_EVENT_TYPE,
                payload=evidence.to_dict(),
                actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
                event_id=start_event_id,
                timestamp=lease.claimed_at,
                correlation_id=manifest.correlation_id,
                causation_id=manifest.causation_id,
                idempotency_key="invocation-start:%s:1" % invocation_id,
            )
            stored, inserted = self._append_in_transaction(
                connection,
                self._snapshot_event(event),
                expected_version,
            )
            if not inserted or stored.sequence != expected_version + 1:
                raise InvocationStartConflictError() from None
            readback = self._read_scoped_invocation_start_in_transaction(
                connection,
                invocation_id,
                fresh=True,
            )
            if (
                readback is None
                or readback.event != stored
                or readback.receipt.evidence.tenant_id != tenant_id
                or readback.receipt.evidence.workspace_id != workspace_id
                or readback.receipt.evidence.lease_token_digest
                != self._lease_token_digest(lease_token)
            ):
                raise InvocationStartConflictError() from None
            return ScopedInvocationStartClaimedV3(readback.receipt, lease)
        except sqlite3.Error:
            raise
        except (
            ConcurrencyError,
            EventStoreIntegrityError,
            InvocationIntegrityError,
            InvocationStartConflictError,
            TypeError,
            ValueError,
        ) as error:
            _detach_exception(error)
            raise _EventStoreStartErrorSignal(
                "conflict",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except BaseException as error:
            if _trusted_event_store_process_signal(error) or _is_exact_control_signal(error):
                raise
            _detach_exception(error)
            raise _EventStoreStartErrorSignal(
                "transaction",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None

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

        event_snapshot = SQLiteEventStore._snapshot_generic_event(self, event)
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

        self._require_current_process()
        event_snapshot = SQLiteEventStore._snapshot_generic_event(self, event)
        raw_batch = tuple(messages)
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
        event_snapshot = SQLiteEventStore._snapshot_generic_event(self, event)
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
        batch = tuple(SQLiteEventStore._snapshot_generic_event(self, event) for event in raw_batch)
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

    @_sanitize_invocation_admission_controls
    @_bind_event_store_process
    def append_invocation_admission(
        self,
        events: Iterable[DomainEvent],
        spec: InvocationJobSpec,
        expected_version: Optional[int] = None,
    ) -> InvocationAdmissionResult:
        """Atomically append one stream batch and enqueue its invocation job.

        The event stream is bound to ``session:<spec.session_id>``. Exact UoW retries
        return the original rows, while partial or changed event/job bindings fail
        closed. Unlike standalone enqueue, a retry must reuse the same invocation ID,
        event IDs, event order, and event content. This method only admits durable work;
        it never claims or executes the queued job.

        If transaction exit fails after the complete body was assembled, the current
        transaction layer cannot safely distinguish a rejected COMMIT from a committed
        write whose acknowledgement was lost. The method therefore raises
        ``InvocationAdmissionCommitAmbiguityError`` and requires reopen/readback retry.
        """

        raw_batch = tuple(events)
        self._require_current_process()
        batch = tuple(SQLiteEventStore._snapshot_generic_event(self, event) for event in raw_batch)
        spec_snapshot = self._snapshot_invocation_job_spec(spec)
        expected_version_snapshot = (
            None
            if expected_version is None
            else _caller_sqlite_integer(expected_version, "expected_version")
        )
        if not batch:
            raise ValueError("invocation admission requires at least one event")
        session_stream_id = "session:%s" % spec_snapshot.session_id
        if any(item.event.stream_id != session_stream_id for item in batch):
            raise ValueError("all admission events must use session:<spec.session_id> as stream_id")

        event_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        for item in batch:
            event = item.event
            if event.event_id in event_ids:
                raise InvocationAdmissionConflictError("invocation admission repeats an event_id")
            event_ids.add(event.event_id)
            if event.idempotency_key is not None:
                if event.idempotency_key in idempotency_keys:
                    raise InvocationAdmissionConflictError(
                        "invocation admission repeats an event idempotency_key"
                    )
                idempotency_keys.add(event.idempotency_key)

        requested_event_ids = tuple(item.event.event_id for item in batch)
        requested_event_manifest_sha256 = self._invocation_event_manifest_sha256(batch)
        requested_job_binding_sha256 = self._invocation_job_binding_sha256(spec_snapshot)

        self._require_current_process()
        completed_body = False
        result: Optional[InvocationAdmissionResult] = None
        commit_exit_failed = False
        transaction_failed = False
        pending_control: Optional[_EventStoreControlDescriptor] = None
        pending_control_ambiguity = False
        try:
            with self._transaction(classify_admission=True) as connection:
                receipt_rows = connection.execute(
                    """
                    SELECT * FROM invocation_admissions
                    WHERE invocation_id = ?
                       OR (session_id = ? AND task_id = ?)
                       OR (session_id = ? AND job_idempotency_key = ?)
                    """,
                    (
                        spec_snapshot.invocation_id,
                        spec_snapshot.session_id,
                        spec_snapshot.task_id,
                        spec_snapshot.session_id,
                        spec_snapshot.idempotency_key,
                    ),
                ).fetchall()
                if len(receipt_rows) > 1:
                    raise InvocationAdmissionConflictError(
                        "invocation admission identities are bound to different receipts"
                    )
                if receipt_rows:
                    if receipt_rows[0]["invocation_id"] != spec_snapshot.invocation_id:
                        raise InvocationAdmissionConflictError(
                            "invocation admission identity is already bound differently"
                        )
                    receipt, existing_events, job = self._validate_invocation_admission_receipt(
                        connection,
                        receipt_rows[0],
                    )
                    if (
                        receipt.session_id != spec_snapshot.session_id
                        or receipt.task_id != spec_snapshot.task_id
                        or receipt.stream_id != session_stream_id
                        or receipt.job_idempotency_key != spec_snapshot.idempotency_key
                        or receipt.event_ids != requested_event_ids
                        or receipt.event_manifest_sha256 != requested_event_manifest_sha256
                        or receipt.job_binding_sha256 != requested_job_binding_sha256
                        or tuple(stored.event for stored in existing_events)
                        != tuple(item.event for item in batch)
                    ):
                        raise InvocationAdmissionConflictError(
                            "invocation admission receipt is bound to different work"
                        )
                    if (
                        expected_version_snapshot is not None
                        and expected_version_snapshot != receipt.original_version
                    ):
                        raise ConcurrencyError(
                            "stream %s expected version %d but admission began at %d"
                            % (
                                session_stream_id,
                                expected_version_snapshot,
                                receipt.original_version,
                            )
                        )
                    result = InvocationAdmissionResult(existing_events, job)
                else:
                    for item in batch:
                        event = item.event
                        if event.idempotency_key is None:
                            rows = connection.execute(
                                "SELECT * FROM events WHERE event_id = ?",
                                (event.event_id,),
                            ).fetchall()
                        else:
                            rows = connection.execute(
                                """
                                SELECT * FROM events
                                WHERE event_id = ?
                                   OR (stream_id = ? AND idempotency_key = ?)
                                """,
                                (event.event_id, event.stream_id, event.idempotency_key),
                            ).fetchall()
                        if rows:
                            raise InvocationAdmissionConflictError(
                                "invocation admission has partial or unproven events without "
                                "a durable receipt"
                            )

                    job_rows = connection.execute(
                        """
                        SELECT * FROM invocation_jobs
                        WHERE invocation_id = ?
                           OR (session_id = ? AND task_id = ?)
                           OR (session_id = ? AND idempotency_key = ?)
                        """,
                        (
                            spec_snapshot.invocation_id,
                            spec_snapshot.session_id,
                            spec_snapshot.task_id,
                            spec_snapshot.session_id,
                            spec_snapshot.idempotency_key,
                        ),
                    ).fetchall()
                    if job_rows:
                        raise InvocationAdmissionConflictError(
                            "invocation admission has a partial or unproven job without "
                            "a durable receipt"
                        )

                    row = connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence), 0) AS version
                        FROM events WHERE stream_id = ?
                        """,
                        (session_stream_id,),
                    ).fetchone()
                    current_version = int(row["version"])
                    if (
                        expected_version_snapshot is not None
                        and expected_version_snapshot != current_version
                    ):
                        raise ConcurrencyError(
                            "stream %s expected version %d but is %d"
                            % (
                                session_stream_id,
                                expected_version_snapshot,
                                current_version,
                            )
                        )
                    stored_events: List[StoredEvent] = []
                    for offset, item in enumerate(batch):
                        stored, inserted = self._append_in_transaction(
                            connection,
                            item,
                            current_version + offset,
                        )
                        if not inserted:  # pragma: no cover - guarded by identity reads.
                            raise InvocationAdmissionConflictError(
                                "invocation admission event appeared during its transaction"
                            )
                        stored_events.append(stored)
                    if any(
                        stored.global_position != stored_events[0].global_position + offset
                        for offset, stored in enumerate(stored_events)
                    ):
                        raise EventStoreIntegrityError(
                            "invocation admission events did not receive a contiguous range"
                        )
                    now = _normalize_invocation_timestamp(self._now(), "clock")
                    job = _enqueue_invocation_job_in_transaction(
                        connection,
                        spec_snapshot,
                        now=now,
                    )
                    event_ids_json = json.dumps(
                        list(requested_event_ids),
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    connection.execute(
                        """
                        INSERT INTO invocation_admissions (
                            invocation_id, receipt_format, session_id, task_id,
                            stream_id, job_idempotency_key, original_version,
                            event_count, event_ids_json, first_sequence, last_sequence,
                            first_global_position, last_global_position,
                            event_manifest_sha256, job_binding_sha256, admitted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            spec_snapshot.invocation_id,
                            _INVOCATION_ADMISSION_RECEIPT_FORMAT,
                            spec_snapshot.session_id,
                            spec_snapshot.task_id,
                            session_stream_id,
                            spec_snapshot.idempotency_key,
                            current_version,
                            len(stored_events),
                            event_ids_json,
                            stored_events[0].sequence,
                            stored_events[-1].sequence,
                            stored_events[0].global_position,
                            stored_events[-1].global_position,
                            requested_event_manifest_sha256,
                            requested_job_binding_sha256,
                            now,
                        ),
                    )
                    receipt_row = connection.execute(
                        "SELECT * FROM invocation_admissions WHERE invocation_id = ?",
                        (spec_snapshot.invocation_id,),
                    ).fetchone()
                    if receipt_row is None:  # pragma: no cover - same transaction insert.
                        raise RuntimeError("invocation admission receipt disappeared")
                    _receipt, verified_events, verified_job = (
                        self._validate_invocation_admission_receipt(
                            connection,
                            receipt_row,
                        )
                    )
                    result = InvocationAdmissionResult(verified_events, verified_job)
                completed_body = True
        except _EventStoreAdmissionTransactionSignal as error:
            classified = _take_classified_event_store_transaction_signal(error)
            if classified is None:
                raise
            outcome, control = classified
            if control is not None:
                pending_control = control
                pending_control_ambiguity = outcome == "ambiguous"
            elif outcome == "rolled_back":
                transaction_failed = True
            else:
                commit_exit_failed = True
        except sqlite3.Error as error:
            _detach_exception(error)
            transaction_failed = True
        except BaseException as error:
            if completed_body and self._process_is_current():
                descriptor = _event_store_control_descriptor(error)
                if descriptor is not None:
                    _detach_exception(error)
                    pending_control = descriptor
                    pending_control_ambiguity = True
                else:
                    commit_exit_failed = True
            else:
                raise
        if pending_control is not None:
            raise _EventStoreAdmissionControlSignal(
                pending_control,
                ambiguity=pending_control_ambiguity,
                token=_EVENT_STORE_ADMISSION_CONTROL_TOKEN,
            ) from None
        if transaction_failed:
            raise InvocationAdmissionTransactionError() from None
        if commit_exit_failed:
            raise InvocationAdmissionCommitAmbiguityError() from None
        if result is None:  # pragma: no cover - every completed body assigns a result.
            raise RuntimeError("invocation admission completed without a result")
        return result

    @_bind_event_store_process
    def append_task_invocation_admission(
        self,
        request: TaskInvocationAdmissionRequest,
        *,
        expected_version: int,
    ) -> InvocationAdmissionResult:
        """Admit one exact canonical task invocation through the atomic v4 boundary.

        The process guard runs before this method inspects caller-owned request state. The
        canonical request then builds a fresh event/job component snapshot and revalidates
        that snapshot before the existing admission primitive owns persistence, replay and
        commit-outcome handling.
        """

        if type(request) is not TaskInvocationAdmissionRequest:
            raise TypeError("request must be an exact TaskInvocationAdmissionRequest")
        expected_version_snapshot = _caller_sqlite_integer(
            expected_version,
            "expected_version",
            minimum=0,
        )
        events, spec = TaskInvocationAdmissionRequest.components(request)
        TaskInvocationAdmissionRequest.validate_components(request, events, spec)
        return self.append_invocation_admission(
            events,
            spec,
            expected_version=expected_version_snapshot,
        )

    @_bind_event_store_process
    def append_scoped_task_invocation_admission_v2(
        self,
        request: ScopedTaskInvocationAdmissionRequestV2,
        *,
        expected_version: int,
    ) -> InvocationAdmissionResult:
        """Admit one scope-bearing schema-2 execution through the atomic v4 boundary.

        Scope is covered by the execution-manifest payload digest stored on the queued job.
        This method does not enable claim/start or worker dispatch; those require the future
        schema-3 scoped start boundary.
        """

        if type(request) is not ScopedTaskInvocationAdmissionRequestV2:
            raise TypeError("request must be an exact ScopedTaskInvocationAdmissionRequestV2")
        expected_version_snapshot = _caller_sqlite_integer(
            expected_version,
            "expected_version",
            minimum=0,
        )
        events, spec = ScopedTaskInvocationAdmissionRequestV2.components(request)
        ScopedTaskInvocationAdmissionRequestV2.validate_components(request, events, spec)
        return self.append_invocation_admission(
            events,
            spec,
            expected_version=expected_version_snapshot,
        )

    @_sanitize_invocation_start_controls
    @_bind_event_store_process
    def claim_invocation_start(
        self,
        invocation_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        expected_version: int,
    ) -> _InvocationStartResult:
        """Atomically mint one first-start lease or return a receipt-only replay.

        Plaintext lease authority is returned only after the transaction that created
        its attempt and schema-2 start event receives a normal COMMIT acknowledgement.
        Any ambiguous exit poisons this store instance; reconciliation after reopen can
        produce only :class:`InvocationStartObserved`.
        """

        invocation_id_snapshot = _caller_invocation_identity(
            invocation_id,
            "invocation_id",
        )
        worker_id_snapshot = _caller_invocation_identity(worker_id, "worker_id")
        lease_seconds_snapshot = _caller_number(
            lease_seconds,
            "lease_seconds",
            positive=True,
        )
        expected_version_snapshot = _caller_sqlite_integer(
            expected_version,
            "expected_version",
            minimum=0,
        )
        self._require_current_process()
        result: Optional[_InvocationStartResult] = None
        completed_body = False
        try:
            with self._transaction(classify_admission=True) as connection:
                result = self._claim_invocation_start_in_transaction(
                    connection,
                    invocation_id_snapshot,
                    worker_id_snapshot,
                    lease_seconds=lease_seconds_snapshot,
                    expected_version=expected_version_snapshot,
                )
                completed_body = True
        except _EventStoreAdmissionTransactionSignal as error:
            classified = _take_classified_event_store_transaction_signal(error)
            if classified is None:
                raise
            outcome, control = classified
            if control is not None:
                raise _EventStoreStartControlSignal(
                    control,
                    ambiguity=outcome == "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise _EventStoreStartErrorSignal(
                "transaction" if outcome == "rolled_back" else "ambiguous",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except sqlite3.Error as error:
            _detach_exception(error)
            if completed_body:
                self._poisoned = True
                kind = "ambiguous"
            else:
                kind = "transaction"
            raise _EventStoreStartErrorSignal(
                kind,
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except BaseException as error:
            if completed_body and self._process_is_current():
                descriptor = _event_store_control_descriptor(error)
                self._poisoned = True
                _detach_exception(error)
                if descriptor is not None:
                    raise _EventStoreStartControlSignal(
                        descriptor,
                        ambiguity=True,
                        token=_EVENT_STORE_START_CONTROL_TOKEN,
                    ) from None
                raise _EventStoreStartErrorSignal(
                    "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise
        if result is None:  # pragma: no cover - every completed body assigns a result.
            raise RuntimeError("invocation start completed without a result")
        return result

    @_sanitize_invocation_start_controls
    @_bind_event_store_process
    def claim_scoped_invocation_start_v3(
        self,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        expected_version: int,
    ) -> _ScopedInvocationStartResult:
        """Atomically mint one scoped schema-3 start lease or receipt-only replay."""

        tenant_id_snapshot = _caller_invocation_identity(tenant_id, "tenant_id")
        workspace_id_snapshot = _caller_invocation_identity(workspace_id, "workspace_id")
        invocation_id_snapshot = _caller_invocation_identity(
            invocation_id,
            "invocation_id",
        )
        worker_id_snapshot = _caller_invocation_identity(worker_id, "worker_id")
        lease_seconds_snapshot = _caller_number(
            lease_seconds,
            "lease_seconds",
            positive=True,
        )
        expected_version_snapshot = _caller_sqlite_integer(
            expected_version,
            "expected_version",
            minimum=0,
        )
        self._require_current_process()
        result: Optional[_ScopedInvocationStartResult] = None
        completed_body = False
        try:
            with self._transaction(classify_admission=True) as connection:
                result = self._claim_scoped_invocation_start_in_transaction(
                    connection,
                    tenant_id_snapshot,
                    workspace_id_snapshot,
                    invocation_id_snapshot,
                    worker_id_snapshot,
                    lease_seconds=lease_seconds_snapshot,
                    expected_version=expected_version_snapshot,
                )
                completed_body = True
        except _EventStoreAdmissionTransactionSignal as error:
            classified = _take_classified_event_store_transaction_signal(error)
            if classified is None:
                raise
            outcome, control = classified
            if control is not None:
                raise _EventStoreStartControlSignal(
                    control,
                    ambiguity=outcome == "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise _EventStoreStartErrorSignal(
                "transaction" if outcome == "rolled_back" else "ambiguous",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except sqlite3.Error as error:
            _detach_exception(error)
            if completed_body:
                self._poisoned = True
                kind = "ambiguous"
            else:
                kind = "transaction"
            raise _EventStoreStartErrorSignal(
                kind,
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except BaseException as error:
            if completed_body and self._process_is_current():
                descriptor = _event_store_control_descriptor(error)
                self._poisoned = True
                _detach_exception(error)
                if descriptor is not None:
                    raise _EventStoreStartControlSignal(
                        descriptor,
                        ambiguity=True,
                        token=_EVENT_STORE_START_CONTROL_TOKEN,
                    ) from None
                raise _EventStoreStartErrorSignal(
                    "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise
        if result is None:  # pragma: no cover - every completed body assigns a result.
            raise RuntimeError("scoped invocation start completed without a result")
        return result

    @_sanitize_invocation_start_controls
    @_bind_event_store_process
    def read_invocation_start(
        self,
        invocation_id: str,
    ) -> Optional[InvocationStartObserved]:
        """Return a capability-free observation of one canonical durable start.

        Unknown invocation identities and canonically admitted but unstarted jobs return
        ``None``. Any partial, legacy, or contradictory durable state fails closed instead
        of being upgraded into schema-2 start evidence.
        """

        invocation_id_snapshot = _caller_invocation_identity(
            invocation_id,
            "invocation_id",
        )
        self._require_current_process()
        result: Optional[InvocationStartObserved] = None
        completed_body = False
        try:
            with self._transaction(classify_admission=True) as connection:
                readback = self._read_invocation_start_in_transaction(
                    connection,
                    invocation_id_snapshot,
                )
                if readback is not None:
                    try:
                        result = InvocationStartObserved(readback.receipt)
                    except (TypeError, ValueError):
                        raise InvocationStartConflictError() from None
                completed_body = True
        except _EventStoreAdmissionTransactionSignal as error:
            classified = _take_classified_event_store_transaction_signal(error)
            if classified is None:
                raise
            outcome, control = classified
            if control is not None:
                raise _EventStoreStartControlSignal(
                    control,
                    ambiguity=outcome == "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise _EventStoreStartErrorSignal(
                "transaction" if outcome == "rolled_back" else "ambiguous",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except sqlite3.Error as error:
            _detach_exception(error)
            if completed_body:
                self._poisoned = True
                kind = "ambiguous"
            else:
                kind = "transaction"
            raise _EventStoreStartErrorSignal(
                kind,
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except BaseException as error:
            if completed_body and self._process_is_current():
                descriptor = _event_store_control_descriptor(error)
                self._poisoned = True
                _detach_exception(error)
                if descriptor is not None:
                    raise _EventStoreStartControlSignal(
                        descriptor,
                        ambiguity=True,
                        token=_EVENT_STORE_START_CONTROL_TOKEN,
                    ) from None
                raise _EventStoreStartErrorSignal(
                    "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise
        return result

    @_sanitize_invocation_start_controls
    @_bind_event_store_process
    def read_scoped_invocation_start_v3(
        self,
        tenant_id: str,
        workspace_id: str,
        invocation_id: str,
    ) -> Optional[ScopedInvocationStartObservedV3]:
        """Return a capability-free scoped observation of one schema-3 start.

        A well-formed invocation owned by a different requested scope is indistinguishable
        from an unknown invocation. Legacy unscoped or contradictory durable state fails closed.
        """

        tenant_id_snapshot = _caller_invocation_identity(tenant_id, "tenant_id")
        workspace_id_snapshot = _caller_invocation_identity(workspace_id, "workspace_id")
        invocation_id_snapshot = _caller_invocation_identity(
            invocation_id,
            "invocation_id",
        )
        self._require_current_process()
        result: Optional[ScopedInvocationStartObservedV3] = None
        completed_body = False
        try:
            with self._transaction(classify_admission=True) as connection:
                readback = self._read_scoped_invocation_start_in_transaction(
                    connection,
                    invocation_id_snapshot,
                )
                if readback is not None and (
                    readback.request.manifest.tenant_id == tenant_id_snapshot
                    and readback.request.manifest.workspace_id == workspace_id_snapshot
                ):
                    try:
                        result = ScopedInvocationStartObservedV3(readback.receipt)
                    except (TypeError, ValueError):
                        raise InvocationStartConflictError() from None
                completed_body = True
        except _EventStoreAdmissionTransactionSignal as error:
            classified = _take_classified_event_store_transaction_signal(error)
            if classified is None:
                raise
            outcome, control = classified
            if control is not None:
                raise _EventStoreStartControlSignal(
                    control,
                    ambiguity=outcome == "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise _EventStoreStartErrorSignal(
                "transaction" if outcome == "rolled_back" else "ambiguous",
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except sqlite3.Error as error:
            _detach_exception(error)
            if completed_body:
                self._poisoned = True
                kind = "ambiguous"
            else:
                kind = "transaction"
            raise _EventStoreStartErrorSignal(
                kind,
                token=_EVENT_STORE_START_CONTROL_TOKEN,
            ) from None
        except BaseException as error:
            if completed_body and self._process_is_current():
                descriptor = _event_store_control_descriptor(error)
                self._poisoned = True
                _detach_exception(error)
                if descriptor is not None:
                    raise _EventStoreStartControlSignal(
                        descriptor,
                        ambiguity=True,
                        token=_EVENT_STORE_START_CONTROL_TOKEN,
                    ) from None
                raise _EventStoreStartErrorSignal(
                    "ambiguous",
                    token=_EVENT_STORE_START_CONTROL_TOKEN,
                ) from None
            raise
        return result

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
        with self._locked(allow_poisoned=True):
            self._connection.close()
            self._result_artifact_savepoint_secret = b""

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
