# ruff: noqa: UP007, UP045 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Protected-operation authorization composition boundaries.

Values returned by ``CurrentAuthorizationStateProvider`` are trusted adapter
inputs, not authorization grants.  Only the operation composer introduced by
this module may turn a fresh, matching state into an opaque operation handle.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import sys
import threading
import traceback
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from types import TracebackType
from typing import Callable, NoReturn, Optional, Protocol, SupportsIndex, TypeVar, Union, cast

from .request_context import ReauthorizationBasis, RequestContext, RequestContextIssuer
from .tenancy import (
    AccessRequest,
    AuthorizationDecision,
    AuthorizationOutcome,
    Member,
    RevocationSnapshot,
    ServerClock,
    SystemClock,
    TenantAuthorizer,
    TenantId,
    VerifiedCapability,
    WorkspaceId,
)

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTION = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_VERIFIED_CAPABILITIES = 64
_KEYBOARD_INTERRUPT = "keyboard_interrupt"
_SYSTEM_EXIT = "system_exit"
_GENERATOR_EXIT = "generator_exit"
_CANCELLED_ERROR = "cancelled_error"
_PROCESS_PID = os.getpid()
_PROCESS_EPOCH = object()
_SystemExitStatus = Optional[Union[bool, int]]


def _refresh_process_epoch_after_fork() -> None:
    """Refresh process identity without consulting any possibly inherited lock."""

    global _PROCESS_EPOCH, _PROCESS_PID
    _PROCESS_PID = os.getpid()
    _PROCESS_EPOCH = object()


def _current_process_identity() -> tuple[int, object]:
    """Return a fork-sensitive identity, lazily refreshing when no hook is available."""

    global _PROCESS_EPOCH, _PROCESS_PID
    process_pid = os.getpid()
    if process_pid != _PROCESS_PID:
        # ``register_at_fork`` is not universal. PID drift is an independent
        # fail-closed fallback and requires no inherited synchronization primitive.
        _PROCESS_PID = process_pid
        _PROCESS_EPOCH = object()
    return process_pid, _PROCESS_EPOCH


_register_at_fork = getattr(os, "register_at_fork", None)
if callable(_register_at_fork):
    try:
        _register_at_fork(after_in_child=_refresh_process_epoch_after_fork)
    except (AttributeError, OSError, RuntimeError, TypeError):
        # PID drift remains a safe lazy fallback when hook registration is unavailable.
        pass


def _control_signal_snapshot(error: BaseException) -> tuple[Optional[str], _SystemExitStatus]:
    if type(error) is KeyboardInterrupt:
        return _KEYBOARD_INTERRUPT, None
    if type(error) is SystemExit:
        status = object.__getattribute__(error, "code")
        if status is None or type(status) is bool:
            return _SYSTEM_EXIT, status
        if type(status) is int and 0 <= status <= 255:
            return _SYSTEM_EXIT, status
        return _SYSTEM_EXIT, 1
    if type(error) is GeneratorExit:
        return _GENERATOR_EXIT, None
    if type(error) is asyncio.CancelledError:
        return _CANCELLED_ERROR, None
    return None, None


def _raise_control_signal(kind: str, system_exit_status: _SystemExitStatus = None) -> NoReturn:
    try:
        if kind == _KEYBOARD_INTERRUPT:
            raise KeyboardInterrupt() from None
        if kind == _SYSTEM_EXIT:
            if system_exit_status is None:
                raise SystemExit() from None
            raise SystemExit(system_exit_status) from None
        if kind == _GENERATOR_EXIT:
            raise GeneratorExit() from None
        if kind == _CANCELLED_ERROR:
            raise asyncio.CancelledError() from None
        raise RuntimeError("invalid internal control signal")
    except (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError) as signal:
        # ``from None`` suppresses display but still retains the caller's actively
        # handled exception in ``__context__``.  Clear it on the exact fresh signal
        # and use a bare re-raise so no caller body exception remains reachable.
        signal.__context__ = None
        raise


def _require_opaque_id(value: str, field_name: str) -> str:
    if type(value) is not str or _OPAQUE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical opaque identifier")
    return value


def _require_text(value: str, field_name: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty without surrounding whitespace")
    if len(value) > maximum or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} is outside the supported text boundary")
    return value


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_duration(
    value: timedelta,
    field_name: str,
    *,
    minimum: timedelta,
    maximum: timedelta,
) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be timedelta")
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} is outside the supported range")
    return value


def _snapshot_member(member: Member) -> Member:
    if type(member) is not Member:
        raise TypeError("member must be an exact Member")
    try:
        return Member.from_dict(member.to_dict())
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("member must be a canonical Member") from error


def _snapshot_revocations(revocations: RevocationSnapshot) -> RevocationSnapshot:
    if type(revocations) is not RevocationSnapshot:
        raise TypeError("revocations must be an exact RevocationSnapshot")
    try:
        return RevocationSnapshot.from_dict(revocations.to_dict())
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("revocations must be a canonical RevocationSnapshot") from error


def _snapshot_access_request(request: AccessRequest) -> AccessRequest:
    if type(request) is not AccessRequest:
        raise TypeError("request must be an exact AccessRequest")
    try:
        return AccessRequest.from_dict(request.to_dict())
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("request must be a canonical AccessRequest") from error


@dataclass(frozen=True, repr=False)
class CurrentAuthorizationState:
    """Exact point-in-time state loaded by one configured service adapter.

    Direct construction is deliberately non-authorizing.  The protected-operation
    composer independently checks this state against issuer-validated request evidence
    before invoking the tenant authorizer.
    """

    context_id: str
    authenticator_id: str
    audience: str
    request_id: str
    principal_id: str
    subject_id: str
    tenant_id: TenantId
    workspace_id: WorkspaceId
    identity_revision: str
    scope_revision: str
    observed_at: datetime
    member: Optional[Member]
    revocations: RevocationSnapshot
    verified_capabilities: tuple[VerifiedCapability, ...] = ()

    def __post_init__(self) -> None:
        _require_opaque_id(self.context_id, "context_id")
        _require_opaque_id(self.authenticator_id, "authenticator_id")
        _require_text(self.audience, "audience")
        _require_opaque_id(self.request_id, "request_id")
        _require_opaque_id(self.principal_id, "principal_id")
        _require_opaque_id(self.subject_id, "subject_id")
        if type(self.tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        if type(self.workspace_id) is not WorkspaceId:
            raise TypeError("workspace_id must be an exact WorkspaceId")
        _require_opaque_id(self.identity_revision, "identity_revision")
        _require_opaque_id(self.scope_revision, "scope_revision")
        object.__setattr__(self, "observed_at", _as_utc(self.observed_at, "observed_at"))

        member = self.member
        if member is not None:
            member = _snapshot_member(member)
            if member.member_id != self.subject_id or member.tenant_id != self.tenant_id:
                raise ValueError("member does not match the current identity scope")
            object.__setattr__(self, "member", member)

        revocations = _snapshot_revocations(self.revocations)
        if revocations.tenant_id != self.tenant_id:
            raise ValueError("revocations do not match the current tenant scope")
        object.__setattr__(self, "revocations", revocations)

        capabilities = self.verified_capabilities
        if type(capabilities) is not tuple:
            raise TypeError("verified_capabilities must be an exact tuple")
        if len(capabilities) > _MAX_VERIFIED_CAPABILITIES:
            raise ValueError("verified_capabilities exceeds the supported boundary")
        if any(type(capability) is not VerifiedCapability for capability in capabilities):
            raise TypeError("verified_capabilities must contain exact VerifiedCapability values")

    def __str__(self) -> str:
        return "CurrentAuthorizationState<non-authorizing>"

    def __repr__(self) -> str:
        return "CurrentAuthorizationState(<non-authorizing>)"


class CurrentAuthorizationStateProvider(Protocol):
    """Load current identity, membership, revocation, and capability state."""

    def load_current_state(
        self,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> CurrentAuthorizationState:
        """Return exact state for the issuer-validated basis and concrete request."""


class OperationAuthorizationError(RuntimeError):
    """Redacted protected-operation failure with one stable machine-readable code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        _require_opaque_id(code, "operation authorization error code")
        self.code = code
        super().__init__(code)


_PUBLIC_OPERATION_FAILURE_CODES = frozenset(
    (
        "protected_operation_authorizer_failed",
        "protected_operation_binding_invalid",
        "protected_operation_capacity_exceeded",
        "protected_operation_clock_unavailable",
        "protected_operation_composer_closed",
        "protected_operation_context_expired",
        "protected_operation_context_rejected",
        "protected_operation_context_time_invalid",
        "protected_operation_decision_invalid",
        "protected_operation_decision_time_invalid",
        "protected_operation_denied",
        "protected_operation_expired",
        "protected_operation_expiry_invalid",
        "protected_operation_id_unavailable",
        "protected_operation_identity_revision_stale",
        "protected_operation_internal_failure",
        "protected_operation_process_mismatch",
        "protected_operation_registry_closed",
        "protected_operation_request_invalid",
        "protected_operation_scope_mismatch",
        "protected_operation_scope_revision_stale",
        "protected_operation_state_invalid",
        "protected_operation_state_mismatch",
        "protected_operation_state_stale",
        "protected_operation_state_time_invalid",
        "protected_operation_state_unavailable",
        "protected_operation_tampered",
        "protected_operation_time_regressed",
        "protected_operation_untrusted",
        "protected_operation_workspace_required",
    )
)


def _require_process_identity(owner_pid: int, owner_epoch: object) -> None:
    process_pid, process_epoch = _current_process_identity()
    if process_pid != owner_pid or process_epoch is not owner_epoch:
        raise OperationAuthorizationError("protected_operation_process_mismatch")


@dataclass(frozen=True, repr=False)
class _BoundaryFailure:
    """Trusted descriptor that never retains a caught third-party exception."""

    operation_code: Optional[str]
    control_signal: Optional[str]
    system_exit_status: _SystemExitStatus


_BoundaryValue = TypeVar("_BoundaryValue")


class _RLockLike(Protocol):
    """Small structural surface used by the process-local registries."""

    def __enter__(self) -> bool: ...

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        trace: Optional[TracebackType],
    ) -> None: ...


def _invoke_boundary(
    callback: Callable[[], _BoundaryValue],
) -> tuple[Optional[_BoundaryValue], Optional[_BoundaryFailure]]:
    """Call one boundary and collapse a raw fault before returning to its caller.

    The raw exception exists only in this inner frame.  Completed callback and
    dependency frames are cleared through the interpreter-owned traceback, so no
    hostile exception attribute is read or mutated.  Before this frame returns,
    its callback and traceback references are explicitly discarded as well.
    """

    pending_callback = [callback]
    del callback
    value: Optional[_BoundaryValue] = None
    failure: Optional[_BoundaryFailure] = None
    try:
        value = pending_callback.pop()()
    except BaseException as error:
        control_signal, system_exit_status = _control_signal_snapshot(error)
        operation_code: Optional[str] = None
        if type(error) is OperationAuthorizationError:
            try:
                candidate = object.__getattribute__(error, "code")
            except AttributeError:
                candidate = None
            if type(candidate) is str and candidate in _PUBLIC_OPERATION_FAILURE_CODES:
                operation_code = candidate
        internal_traceback = sys.exc_info()[2]
        if internal_traceback is not None:
            traceback.clear_frames(internal_traceback)
        failure = _BoundaryFailure(
            operation_code=operation_code,
            control_signal=control_signal,
            system_exit_status=system_exit_status,
        )
        del internal_traceback
        return value, failure
    return value, None


def _raise_dependency_failure(failure: _BoundaryFailure, code: str) -> NoReturn:
    """Propagate only a clean control signal or one caller-selected stable code."""

    control_signal = failure.control_signal
    system_exit_status = failure.system_exit_status
    del failure
    if control_signal is not None:
        _raise_control_signal(control_signal, system_exit_status)
    raise OperationAuthorizationError(code) from None


def _require_callable_dependency(
    dependency: object,
    method_name: str,
    failure_code: str,
) -> None:
    """Probe one configured method without exposing descriptor failure state."""

    if dependency is None:
        raise OperationAuthorizationError(failure_code)
    candidate, failure = _invoke_boundary(lambda: getattr(dependency, method_name))
    if failure is not None:
        _raise_dependency_failure(failure, failure_code)
    if not callable(candidate):
        raise OperationAuthorizationError(failure_code)


def _operation_failure_details(
    failure: Optional[_BoundaryFailure],
    default_code: str,
) -> tuple[str, Optional[str], _SystemExitStatus]:
    """Return only bounded primitives suitable for a clean public rethrow frame."""

    if failure is None:
        return default_code, None, None
    return (
        failure.operation_code if failure.operation_code is not None else default_code,
        failure.control_signal,
        failure.system_exit_status,
    )


def _require_current_process_issuer(issuer: RequestContextIssuer) -> None:
    """Reject an issuer inherited from a different process before touching its state."""

    _, failure = _invoke_boundary(issuer._ensure_process)
    if failure is not None:
        _raise_dependency_failure(failure, "protected_operation_process_mismatch")


@dataclass(frozen=True, repr=False)
class _OperationBinding:
    """Canonical internal binding; construction alone grants no authority."""

    context_id: str
    authenticator_id: str
    audience: str
    request_id: str
    principal_id: str
    subject_id: str
    tenant_id: TenantId
    workspace_id: WorkspaceId
    action: str
    resource_type: str
    resource_id: str
    decision_id: str
    identity_revision: str
    scope_revision: str

    def __post_init__(self) -> None:
        _require_opaque_id(self.context_id, "context_id")
        _require_opaque_id(self.authenticator_id, "authenticator_id")
        _require_text(self.audience, "audience")
        _require_opaque_id(self.request_id, "request_id")
        _require_opaque_id(self.principal_id, "principal_id")
        _require_opaque_id(self.subject_id, "subject_id")
        if type(self.tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        if type(self.workspace_id) is not WorkspaceId:
            raise TypeError("workspace_id must be an exact WorkspaceId")
        if type(self.action) is not str or _ACTION.fullmatch(self.action) is None:
            raise ValueError("action must be one concrete canonical action")
        _require_opaque_id(self.resource_type, "resource_type")
        _require_text(self.resource_id, "resource_id", maximum=512)
        if type(self.decision_id) is not str or _SHA256.fullmatch(self.decision_id) is None:
            raise ValueError("decision_id must be a lower-case SHA-256 digest")
        _require_opaque_id(self.identity_revision, "identity_revision")
        _require_opaque_id(self.scope_revision, "scope_revision")

    def __str__(self) -> str:
        return "OperationBinding<non-authorizing>"

    def __repr__(self) -> str:
        return "OperationBinding(<non-authorizing>)"


@dataclass(frozen=True, repr=False)
class _OperationSnapshot:
    operation_id: str
    context_id: str
    authenticator_id: str
    audience: str
    request_id: str
    principal_id: str
    subject_id: str
    tenant_value: str
    workspace_value: str
    action: str
    resource_type: str
    resource_id: str
    decision_id: str
    identity_revision: str
    scope_revision: str
    issued_at: datetime
    expires_at: datetime


_OPERATION_CONSTRUCTION_TOKEN = object()


class AuthorizedOperation:
    """Opaque process-local handle accepted only by its issuing composer."""

    __slots__ = (
        "__action",
        "__audience",
        "__authenticator_id",
        "__context_id",
        "__decision_id",
        "__expires_at",
        "__identity_revision",
        "__issued_at",
        "__operation_id",
        "__principal_id",
        "__request_id",
        "__resource_id",
        "__resource_type",
        "__scope_revision",
        "__subject_id",
        "__tenant_value",
        "__weakref__",
        "__workspace_value",
    )

    def __init__(self, snapshot: _OperationSnapshot, token: object) -> None:
        if token is not _OPERATION_CONSTRUCTION_TOKEN or type(snapshot) is not _OperationSnapshot:
            raise TypeError("AuthorizedOperation instances are issued by a protected composer")
        self.__operation_id = snapshot.operation_id
        self.__context_id = snapshot.context_id
        self.__authenticator_id = snapshot.authenticator_id
        self.__audience = snapshot.audience
        self.__request_id = snapshot.request_id
        self.__principal_id = snapshot.principal_id
        self.__subject_id = snapshot.subject_id
        self.__tenant_value = snapshot.tenant_value
        self.__workspace_value = snapshot.workspace_value
        self.__action = snapshot.action
        self.__resource_type = snapshot.resource_type
        self.__resource_id = snapshot.resource_id
        self.__decision_id = snapshot.decision_id
        self.__identity_revision = snapshot.identity_revision
        self.__scope_revision = snapshot.scope_revision
        self.__issued_at = snapshot.issued_at
        self.__expires_at = snapshot.expires_at

    @property
    def operation_id(self) -> str:
        """Return the non-authorizing correlation identifier."""

        return self.__operation_id

    @property
    def issued_at(self) -> datetime:
        return self.__issued_at

    @property
    def expires_at(self) -> datetime:
        return self.__expires_at

    def __str__(self) -> str:
        return "AuthorizedOperation<opaque>"

    def __repr__(self) -> str:
        return "AuthorizedOperation(<opaque>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("AuthorizedOperation cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("AuthorizedOperation cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("AuthorizedOperation cannot be serialized")

    def _bound_values(self) -> tuple[object, ...]:
        return (
            self.__operation_id,
            self.__context_id,
            self.__authenticator_id,
            self.__audience,
            self.__request_id,
            self.__principal_id,
            self.__subject_id,
            self.__tenant_value,
            self.__workspace_value,
            self.__action,
            self.__resource_type,
            self.__resource_id,
            self.__decision_id,
            self.__identity_revision,
            self.__scope_revision,
            self.__issued_at,
            self.__expires_at,
        )


@dataclass(repr=False)
class _AuthorizedOperationRegistryState:
    """Fully built registry state published through one exact slot."""

    active: dict[int, tuple[weakref.ReferenceType[AuthorizedOperation], _OperationSnapshot]]
    clock: ServerClock
    closed: bool
    last_observed_at: Optional[datetime]
    lock: _RLockLike
    max_active_operations: int
    max_clock_skew: timedelta
    max_operation_ttl: timedelta
    owner_epoch: object
    owner_pid: int

    def __repr__(self) -> str:
        return "AuthorizedOperationRegistryState(<configured>)"


_REGISTRY_STATE_SLOT = "_AuthorizedOperationRegistry__state"


def _publish_constructor_state(instance: object, slot_name: str, state: object) -> None:
    """Publish one fully built constructor state with one exact slot write."""

    object.__setattr__(instance, slot_name, state)


def _discard_constructor_state(instance: object, slot_name: str) -> None:
    """Remove a constructor state without invoking instance-level attribute hooks."""

    try:
        object.__delattr__(instance, slot_name)
    except AttributeError:
        pass


class _AuthorizedOperationRegistry:
    """Thread-safe, bounded process-local registry owned by exactly one composer."""

    __slots__ = ("__state",)
    __state: _AuthorizedOperationRegistryState

    @property
    def __active(
        self,
    ) -> dict[int, tuple[weakref.ReferenceType[AuthorizedOperation], _OperationSnapshot]]:
        return self.__state.active

    @property
    def __clock(self) -> ServerClock:
        return self.__state.clock

    @property
    def __closed(self) -> bool:
        return self.__state.closed

    @__closed.setter
    def __closed(self, value: bool) -> None:
        self.__state.closed = value

    @property
    def __last_observed_at(self) -> Optional[datetime]:
        return self.__state.last_observed_at

    @__last_observed_at.setter
    def __last_observed_at(self, value: Optional[datetime]) -> None:
        self.__state.last_observed_at = value

    @property
    def __lock(self) -> _RLockLike:
        return self.__state.lock

    @property
    def __max_active_operations(self) -> int:
        return self.__state.max_active_operations

    @property
    def __max_clock_skew(self) -> timedelta:
        return self.__state.max_clock_skew

    @property
    def __max_operation_ttl(self) -> timedelta:
        return self.__state.max_operation_ttl

    @property
    def __owner_epoch(self) -> object:
        return self.__state.owner_epoch

    @__owner_epoch.setter
    def __owner_epoch(self, value: object) -> None:
        self.__state.owner_epoch = value

    @property
    def __owner_pid(self) -> int:
        return self.__state.owner_pid

    def __init__(
        self,
        *,
        clock: ServerClock,
        max_operation_ttl: timedelta = timedelta(seconds=30),
        max_clock_skew: timedelta = timedelta(seconds=30),
        max_active_operations: int = 10_000,
    ) -> None:
        initialized, failure = _invoke_boundary(
            partial(
                _AuthorizedOperationRegistry._initialize,
                self,
                clock=clock,
                max_operation_ttl=max_operation_ttl,
                max_clock_skew=max_clock_skew,
                max_active_operations=max_active_operations,
            )
        )
        if failure is None and initialized is True:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        _discard_constructor_state(self, _REGISTRY_STATE_SLOT)
        del (
            self,
            clock,
            max_operation_ttl,
            max_clock_skew,
            max_active_operations,
            initialized,
            failure,
        )
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _initialize(
        self,
        *,
        clock: ServerClock,
        max_operation_ttl: timedelta,
        max_clock_skew: timedelta,
        max_active_operations: int,
    ) -> bool:
        """Configure the registry inside one exception-containment boundary."""

        if type(self) is not _AuthorizedOperationRegistry:
            raise TypeError("registry must be an exact AuthorizedOperationRegistry")
        owner_pid, owner_epoch = _current_process_identity()
        _require_callable_dependency(
            clock,
            "now",
            "protected_operation_clock_unavailable",
        )
        validated_operation_ttl = _require_duration(
            max_operation_ttl,
            "max_operation_ttl",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(minutes=5),
        )
        validated_clock_skew = _require_duration(
            max_clock_skew,
            "max_clock_skew",
            minimum=timedelta(0),
            maximum=timedelta(minutes=5),
        )
        if isinstance(max_active_operations, bool) or not isinstance(max_active_operations, int):
            raise TypeError("max_active_operations must be an integer")
        if not 1 <= max_active_operations <= 1_000_000:
            raise ValueError("max_active_operations is outside the supported range")
        lock = threading.RLock()
        state = _AuthorizedOperationRegistryState(
            active={},
            clock=clock,
            closed=False,
            last_observed_at=None,
            lock=lock,
            max_active_operations=max_active_operations,
            max_clock_skew=validated_clock_skew,
            max_operation_ttl=validated_operation_ttl,
            owner_epoch=owner_epoch,
            owner_pid=owner_pid,
        )
        _publish_constructor_state(self, _REGISTRY_STATE_SLOT, state)
        return True

    def issue(
        self,
        binding: _OperationBinding,
        *,
        expires_at: datetime,
    ) -> AuthorizedOperation:
        operation, failure = _invoke_boundary(
            partial(_AuthorizedOperationRegistry._issue, self, binding, expires_at=expires_at)
        )
        if failure is None and type(operation) is AuthorizedOperation:
            return operation
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, binding, expires_at, operation, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _issue(
        self,
        binding: _OperationBinding,
        *,
        expires_at: datetime,
    ) -> AuthorizedOperation:
        self._ensure_process()
        trusted = self._snapshot_binding(binding)
        expiry = _as_utc(expires_at, "expires_at")
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            now = self._clock_now()
            self._prune(now)
            if expiry <= now or expiry - now > self.__max_operation_ttl:
                raise OperationAuthorizationError("protected_operation_expiry_invalid")
            if len(self.__active) >= self.__max_active_operations:
                raise OperationAuthorizationError("protected_operation_capacity_exceeded")
            snapshot = self._new_snapshot(trusted, now, expiry)
            operation = AuthorizedOperation(snapshot, _OPERATION_CONSTRUCTION_TOKEN)
            self.__active[id(operation)] = (weakref.ref(operation), snapshot)
            return operation

    def observe_now(self) -> datetime:
        """Advance and return the registry's service-clock high-water mark."""

        observed_at, failure = _invoke_boundary(
            partial(_AuthorizedOperationRegistry._observe_now, self)
        )
        if failure is None and isinstance(observed_at, datetime):
            return observed_at
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, observed_at, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _observe_now(self) -> datetime:
        """Private clock path whose completed failure frames are always cleared."""

        self._ensure_process()
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            return self._clock_now()

    def verify(self, operation: AuthorizedOperation, expected: _OperationBinding) -> None:
        _, failure = _invoke_boundary(
            partial(_AuthorizedOperationRegistry._verify, self, operation, expected)
        )
        if failure is None:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, operation, expected, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _verify(self, operation: AuthorizedOperation, expected: _OperationBinding) -> None:
        self._ensure_process()
        trusted_expected = self._snapshot_binding(expected)
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            now = self._clock_now()
            snapshot = self._trusted_snapshot(operation)
            if snapshot.expires_at <= now:
                self.__active.pop(id(operation), None)
                raise OperationAuthorizationError("protected_operation_expired")
            if not self._snapshot_matches_binding(snapshot, trusted_expected):
                raise OperationAuthorizationError("protected_operation_scope_mismatch")
            self._prune(now)

    def check_request(
        self,
        operation: AuthorizedOperation,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> None:
        """Verify a live exact actor/request handle without granting or consuming it."""

        _, failure = _invoke_boundary(
            partial(
                _AuthorizedOperationRegistry._check_request,
                self,
                operation,
                basis,
                request,
            )
        )
        if failure is None:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, operation, basis, request, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _check_request(
        self,
        operation: AuthorizedOperation,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> None:
        """Private preflight path whose completed failure frames are always cleared."""

        self._ensure_process()
        trusted_request = _snapshot_access_request(request)
        if type(basis) is not ReauthorizationBasis:
            raise OperationAuthorizationError("protected_operation_context_rejected")
        if trusted_request.resource.workspace_id is None:
            raise OperationAuthorizationError("protected_operation_workspace_required")
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            now = self._clock_now()
            snapshot = self._trusted_snapshot(operation)
            if snapshot.expires_at <= now:
                self.__active.pop(id(operation), None)
                raise OperationAuthorizationError("protected_operation_expired")
            if not self._snapshot_matches_request(snapshot, basis, trusted_request):
                raise OperationAuthorizationError("protected_operation_scope_mismatch")
            self._prune(now)

    def consume_request(
        self,
        operation: AuthorizedOperation,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> None:
        """Atomically verify exact actor/request scope and retire on success."""

        _, failure = _invoke_boundary(
            partial(
                _AuthorizedOperationRegistry._consume_request,
                self,
                operation,
                basis,
                request,
            )
        )
        if failure is None:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, operation, basis, request, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _consume_request(
        self,
        operation: AuthorizedOperation,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> None:
        """Private consume path whose completed failure frames are always cleared."""

        self._ensure_process()
        trusted_request = _snapshot_access_request(request)
        if type(basis) is not ReauthorizationBasis:
            raise OperationAuthorizationError("protected_operation_context_rejected")
        workspace = trusted_request.resource.workspace_id
        if workspace is None:
            raise OperationAuthorizationError("protected_operation_workspace_required")
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            now = self._clock_now()
            snapshot = self._trusted_snapshot(operation)
            if snapshot.expires_at <= now:
                self.__active.pop(id(operation), None)
                raise OperationAuthorizationError("protected_operation_expired")
            if not self._snapshot_matches_request(snapshot, basis, trusted_request):
                raise OperationAuthorizationError("protected_operation_scope_mismatch")
            self.__active.pop(id(operation), None)
            self._prune(now)

    def retire(self, operation: AuthorizedOperation) -> None:
        _, failure = _invoke_boundary(
            partial(_AuthorizedOperationRegistry._retire, self, operation)
        )
        if failure is None:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, operation, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _retire(self, operation: AuthorizedOperation) -> None:
        self._ensure_process()
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            self._trusted_snapshot(operation)
            self.__active.pop(id(operation), None)

    def close(self) -> None:
        _, failure = _invoke_boundary(partial(_AuthorizedOperationRegistry._close, self))
        if failure is None:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _close(self) -> None:
        self._ensure_process()
        with self.__lock:
            self.__closed = True
            self.__active.clear()

    def __repr__(self) -> str:
        return "AuthorizedOperationRegistry(<configured>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("AuthorizedOperationRegistry cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("AuthorizedOperationRegistry cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("AuthorizedOperationRegistry cannot be serialized")

    def _ensure_process(self) -> None:
        _require_process_identity(self.__owner_pid, self.__owner_epoch)

    def _clock_now(self) -> datetime:
        normalized, failure = _invoke_boundary(lambda: _as_utc(self.__clock.now(), "clock.now()"))
        if failure is not None:
            _raise_dependency_failure(failure, "protected_operation_clock_unavailable")
        if normalized is None:
            raise OperationAuthorizationError("protected_operation_clock_unavailable")
        previous = self.__last_observed_at
        if previous is not None and normalized < previous:
            if previous - normalized > self.__max_clock_skew:
                raise OperationAuthorizationError("protected_operation_time_regressed")
            return previous
        self.__last_observed_at = normalized
        return normalized

    @staticmethod
    def _snapshot_binding(binding: _OperationBinding) -> _OperationBinding:
        if type(binding) is not _OperationBinding:
            raise OperationAuthorizationError("protected_operation_binding_invalid")
        trusted, failure = _invoke_boundary(
            lambda: _OperationBinding(
                context_id=binding.context_id,
                authenticator_id=binding.authenticator_id,
                audience=binding.audience,
                request_id=binding.request_id,
                principal_id=binding.principal_id,
                subject_id=binding.subject_id,
                tenant_id=TenantId(str(binding.tenant_id)),
                workspace_id=WorkspaceId(str(binding.workspace_id)),
                action=binding.action,
                resource_type=binding.resource_type,
                resource_id=binding.resource_id,
                decision_id=binding.decision_id,
                identity_revision=binding.identity_revision,
                scope_revision=binding.scope_revision,
            )
        )
        if failure is not None:
            _raise_dependency_failure(failure, "protected_operation_binding_invalid")
        if trusted is None:
            raise OperationAuthorizationError("protected_operation_binding_invalid")
        return trusted

    def _new_snapshot(
        self,
        binding: _OperationBinding,
        issued_at: datetime,
        expires_at: datetime,
    ) -> _OperationSnapshot:
        operation_id = self._new_operation_id()
        return _OperationSnapshot(
            operation_id=operation_id,
            context_id=binding.context_id,
            authenticator_id=binding.authenticator_id,
            audience=binding.audience,
            request_id=binding.request_id,
            principal_id=binding.principal_id,
            subject_id=binding.subject_id,
            tenant_value=str(binding.tenant_id),
            workspace_value=str(binding.workspace_id),
            action=binding.action,
            resource_type=binding.resource_type,
            resource_id=binding.resource_id,
            decision_id=binding.decision_id,
            identity_revision=binding.identity_revision,
            scope_revision=binding.scope_revision,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def _new_operation_id(self) -> str:
        existing = {record[1].operation_id for record in self.__active.values()}
        for _ in range(4):
            candidate, failure = _invoke_boundary(lambda: f"op_{secrets.token_hex(32)}")
            if failure is not None:
                _raise_dependency_failure(failure, "protected_operation_id_unavailable")
            if candidate is not None and candidate not in existing:
                return candidate
        raise OperationAuthorizationError("protected_operation_id_unavailable")

    def _trusted_snapshot(self, operation: AuthorizedOperation) -> _OperationSnapshot:
        if type(operation) is not AuthorizedOperation:
            raise OperationAuthorizationError("protected_operation_untrusted")
        record = self.__active.get(id(operation))
        if record is None or record[0]() is not operation:
            raise OperationAuthorizationError("protected_operation_untrusted")
        snapshot = record[1]
        if not self._operation_matches_snapshot(operation, snapshot):
            self.__active.pop(id(operation), None)
            raise OperationAuthorizationError("protected_operation_tampered")
        return snapshot

    @staticmethod
    def _operation_matches_snapshot(
        operation: AuthorizedOperation,
        snapshot: _OperationSnapshot,
    ) -> bool:
        actual, failure = _invoke_boundary(lambda: operation._bound_values())
        if failure is not None:
            if failure.control_signal is not None:
                _raise_control_signal(
                    failure.control_signal,
                    failure.system_exit_status,
                )
            return False
        expected = (
            snapshot.operation_id,
            snapshot.context_id,
            snapshot.authenticator_id,
            snapshot.audience,
            snapshot.request_id,
            snapshot.principal_id,
            snapshot.subject_id,
            snapshot.tenant_value,
            snapshot.workspace_value,
            snapshot.action,
            snapshot.resource_type,
            snapshot.resource_id,
            snapshot.decision_id,
            snapshot.identity_revision,
            snapshot.scope_revision,
            snapshot.issued_at,
            snapshot.expires_at,
        )
        return actual == expected

    @staticmethod
    def _snapshot_matches_binding(
        snapshot: _OperationSnapshot,
        binding: _OperationBinding,
    ) -> bool:
        return (
            snapshot.context_id,
            snapshot.authenticator_id,
            snapshot.audience,
            snapshot.request_id,
            snapshot.principal_id,
            snapshot.subject_id,
            snapshot.tenant_value,
            snapshot.workspace_value,
            snapshot.action,
            snapshot.resource_type,
            snapshot.resource_id,
            snapshot.decision_id,
            snapshot.identity_revision,
            snapshot.scope_revision,
        ) == (
            binding.context_id,
            binding.authenticator_id,
            binding.audience,
            binding.request_id,
            binding.principal_id,
            binding.subject_id,
            str(binding.tenant_id),
            str(binding.workspace_id),
            binding.action,
            binding.resource_type,
            binding.resource_id,
            binding.decision_id,
            binding.identity_revision,
            binding.scope_revision,
        )

    @staticmethod
    def _snapshot_matches_request(
        snapshot: _OperationSnapshot,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> bool:
        workspace = request.resource.workspace_id
        return (
            workspace is not None
            and (
                snapshot.context_id,
                snapshot.authenticator_id,
                snapshot.audience,
                snapshot.request_id,
                snapshot.principal_id,
                snapshot.subject_id,
                snapshot.tenant_value,
                snapshot.workspace_value,
                snapshot.identity_revision,
                snapshot.scope_revision,
                snapshot.action,
                snapshot.resource_type,
                snapshot.resource_id,
            )
            == (
                basis.context_id,
                basis.authenticator_id,
                basis.audience,
                request.request_id,
                basis.principal_id,
                request.subject_id,
                str(request.tenant_id),
                str(workspace),
                basis.identity_revision,
                basis.scope_revision,
                request.action,
                request.resource.resource_type,
                request.resource.resource_id,
            )
            and (
                basis.request_id,
                basis.subject_id,
                basis.tenant_id,
                basis.workspace_id,
            )
            == (
                request.request_id,
                request.subject_id,
                request.tenant_id,
                workspace,
            )
            and request.resource.tenant_id == request.tenant_id
        )

    def _prune(self, now: datetime) -> None:
        stale = [
            identifier
            for identifier, (reference, snapshot) in self.__active.items()
            if reference() is None or snapshot.expires_at <= now
        ]
        for identifier in stale:
            del self.__active[identifier]


@dataclass(frozen=True, repr=False)
class _ProtectedOperationContextLease:
    """One interpreter-protocol candidate bound to an exact composer and thread."""

    active: bool
    consumed: bool
    entry_callback: weakref.ReferenceType[object]
    exit_bound: bool
    owner_thread: threading.Thread
    token: object

    def __repr__(self) -> str:
        return "ProtectedOperationContextLease(<opaque>)"


@dataclass(repr=False)
class _ProtectedOperationComposerState:
    """Fully built composer state published through one exact slot."""

    authorizer: TenantAuthorizer
    closed: bool
    context_lease: Optional[_ProtectedOperationContextLease]
    issuer: RequestContextIssuer
    lock: _RLockLike
    max_clock_skew: timedelta
    max_state_age: timedelta
    operation_ttl: timedelta
    owner_epoch: object
    owner_pid: int
    provider: CurrentAuthorizationStateProvider
    registry: _AuthorizedOperationRegistry

    def __repr__(self) -> str:
        return "ProtectedOperationComposerState(<configured>)"


_COMPOSER_STATE_SLOT = "_ProtectedOperationComposer__state"
_CONTEXT_EXIT_RECONCILIATION_ATTEMPTS = 2


class _ProtectedOperationEnterDescriptor:
    """Prepare one candidate only during special-method context lookup."""

    __slots__ = ()

    def __get__(
        self,
        instance: Optional[object],
        owner: Optional[type[object]] = None,
    ) -> Callable[..., object]:
        if instance is None:
            return __enter__
        composer = cast("ProtectedOperationComposer", instance)
        lease = object()
        enter_function = cast(Callable[..., object], __enter__)
        enter_callback = partial(
            enter_function,
            composer,
            _context_exit_lease=lease,
        )
        callback_reference = cast(weakref.ReferenceType[object], weakref.ref(enter_callback))
        prepared, failure = _invoke_boundary(
            partial(
                ProtectedOperationComposer._prepare_context_enter_lease,
                composer,
                lease,
                callback_reference,
            )
        )
        if failure is None and prepared is True:
            return enter_callback
        _invoke_boundary(
            partial(
                ProtectedOperationComposer._discard_context_exit_lease,
                composer,
                lease,
                True,
            )
        )
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del (
            instance,
            owner,
            composer,
            lease,
            enter_function,
            enter_callback,
            callback_reference,
            prepared,
            failure,
        )
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise


class _ProtectedOperationExitDescriptor:
    """Bind only the pending candidate prepared by context-manager lookup."""

    __slots__ = ()

    def __get__(
        self,
        instance: Optional[object],
        owner: Optional[type[object]] = None,
    ) -> Callable[..., object]:
        if instance is None:
            return __exit__
        composer = cast("ProtectedOperationComposer", instance)
        lease, failure = _invoke_boundary(
            partial(ProtectedOperationComposer._pending_context_exit_lease, composer)
        )
        if failure is None and lease is not None:
            exit_function = cast(Callable[..., object], __exit__)
            exit_callback = partial(
                exit_function,
                composer,
                _context_exit_lease=lease,
            )
            bound, bind_failure = _invoke_boundary(
                partial(
                    ProtectedOperationComposer._bind_context_exit_lease,
                    composer,
                    lease,
                )
            )
            if bind_failure is None and bound is True:
                return exit_callback
            _invoke_boundary(
                partial(
                    ProtectedOperationComposer._discard_context_exit_lease,
                    composer,
                    lease,
                    True,
                )
            )
            failure = bind_failure
            del exit_function, exit_callback, bound, bind_failure
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del instance, owner, composer, lease, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise


class ProtectedOperationComposer:
    """Compose request admission, current state, and tenant authorization exactly once."""

    __slots__ = ("__state",)
    __state: _ProtectedOperationComposerState
    __enter__ = _ProtectedOperationEnterDescriptor()
    __exit__ = _ProtectedOperationExitDescriptor()

    def __getattribute__(self, name: str) -> object:
        if name == "__enter__":
            return partial(__enter__, self)
        if name == "__exit__":
            return partial(__exit__, self)
        return object.__getattribute__(self, name)

    @property
    def __authorizer(self) -> TenantAuthorizer:
        return self.__state.authorizer

    @property
    def __closed(self) -> bool:
        return self.__state.closed

    @__closed.setter
    def __closed(self, value: bool) -> None:
        self.__state.closed = value

    @property
    def __context_lease(self) -> Optional[_ProtectedOperationContextLease]:
        return self.__state.context_lease

    @__context_lease.setter
    def __context_lease(self, value: Optional[_ProtectedOperationContextLease]) -> None:
        self.__state.context_lease = value

    @property
    def __issuer(self) -> RequestContextIssuer:
        return self.__state.issuer

    @property
    def __lock(self) -> _RLockLike:
        return self.__state.lock

    @property
    def __max_clock_skew(self) -> timedelta:
        return self.__state.max_clock_skew

    @property
    def __max_state_age(self) -> timedelta:
        return self.__state.max_state_age

    @property
    def __operation_ttl(self) -> timedelta:
        return self.__state.operation_ttl

    @property
    def __owner_epoch(self) -> object:
        return self.__state.owner_epoch

    @__owner_epoch.setter
    def __owner_epoch(self, value: object) -> None:
        self.__state.owner_epoch = value

    @property
    def __owner_pid(self) -> int:
        return self.__state.owner_pid

    @property
    def __provider(self) -> CurrentAuthorizationStateProvider:
        return self.__state.provider

    @property
    def __registry(self) -> _AuthorizedOperationRegistry:
        return self.__state.registry

    def __init__(
        self,
        *,
        issuer: RequestContextIssuer,
        state_provider: CurrentAuthorizationStateProvider,
        authorizer: TenantAuthorizer,
        clock: Optional[ServerClock] = None,
        operation_ttl: timedelta = timedelta(seconds=30),
        max_state_age: timedelta = timedelta(seconds=30),
        max_clock_skew: timedelta = timedelta(seconds=30),
        max_active_operations: int = 10_000,
    ) -> None:
        initialized, failure = _invoke_boundary(
            partial(
                ProtectedOperationComposer._initialize,
                self,
                issuer=issuer,
                state_provider=state_provider,
                authorizer=authorizer,
                clock=clock,
                operation_ttl=operation_ttl,
                max_state_age=max_state_age,
                max_clock_skew=max_clock_skew,
                max_active_operations=max_active_operations,
            )
        )
        if failure is None and initialized is True:
            return
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        _discard_constructor_state(self, _COMPOSER_STATE_SLOT)
        del (
            self,
            issuer,
            state_provider,
            authorizer,
            clock,
            operation_ttl,
            max_state_age,
            max_clock_skew,
            max_active_operations,
            initialized,
            failure,
        )
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _initialize(
        self,
        *,
        issuer: RequestContextIssuer,
        state_provider: CurrentAuthorizationStateProvider,
        authorizer: TenantAuthorizer,
        clock: Optional[ServerClock],
        operation_ttl: timedelta,
        max_state_age: timedelta,
        max_clock_skew: timedelta,
        max_active_operations: int,
    ) -> bool:
        """Configure the composer inside one exception-containment boundary."""

        if type(self) is not ProtectedOperationComposer:
            raise TypeError("composer must be an exact ProtectedOperationComposer")
        owner_pid, owner_epoch = _current_process_identity()
        if type(issuer) is not RequestContextIssuer:
            raise TypeError("issuer must be an exact RequestContextIssuer")
        _require_current_process_issuer(issuer)
        _require_callable_dependency(
            state_provider,
            "load_current_state",
            "protected_operation_state_unavailable",
        )
        if type(authorizer) is not TenantAuthorizer:
            raise TypeError("authorizer must be an exact TenantAuthorizer")
        validated_operation_ttl = _require_duration(
            operation_ttl,
            "operation_ttl",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(minutes=5),
        )
        validated_state_age = _require_duration(
            max_state_age,
            "max_state_age",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(minutes=5),
        )
        validated_clock_skew = _require_duration(
            max_clock_skew,
            "max_clock_skew",
            minimum=timedelta(0),
            maximum=timedelta(minutes=5),
        )
        configured_clock = clock if clock is not None else SystemClock()
        registry = _AuthorizedOperationRegistry(
            clock=configured_clock,
            max_operation_ttl=validated_operation_ttl,
            max_clock_skew=validated_clock_skew,
            max_active_operations=max_active_operations,
        )
        lock = threading.RLock()
        state = _ProtectedOperationComposerState(
            authorizer=authorizer,
            closed=False,
            context_lease=None,
            issuer=issuer,
            lock=lock,
            max_clock_skew=validated_clock_skew,
            max_state_age=validated_state_age,
            operation_ttl=validated_operation_ttl,
            owner_epoch=owner_epoch,
            owner_pid=owner_pid,
            provider=state_provider,
            registry=registry,
        )
        _publish_constructor_state(self, _COMPOSER_STATE_SLOT, state)
        return True

    def authorize(
        self,
        context: RequestContext,
        request: AccessRequest,
    ) -> AuthorizedOperation:
        """Return one bounded opaque handle only after an explicit ALLOW decision."""

        operation, failure = _invoke_boundary(
            partial(ProtectedOperationComposer._authorize, self, context, request)
        )
        if failure is None and type(operation) is AuthorizedOperation:
            return operation
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, context, request, operation, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _authorize(
        self,
        context: RequestContext,
        request: AccessRequest,
    ) -> AuthorizedOperation:
        self._ensure_open()
        basis, admission_failure = _invoke_boundary(
            lambda: self.__issuer.prepare_reauthorization(context, request)
        )
        if admission_failure is not None:
            _raise_dependency_failure(
                admission_failure,
                "protected_operation_context_rejected",
            )
        if type(basis) is not ReauthorizationBasis:
            raise OperationAuthorizationError("protected_operation_context_rejected")

        trusted_request, request_failure = _invoke_boundary(
            lambda: _snapshot_access_request(request)
        )
        if request_failure is not None:
            _raise_dependency_failure(
                request_failure,
                "protected_operation_request_invalid",
            )
        if trusted_request is None:
            raise OperationAuthorizationError("protected_operation_request_invalid")
        workspace = trusted_request.resource.workspace_id
        if basis.workspace_id is None or workspace is None:
            raise OperationAuthorizationError("protected_operation_workspace_required")
        if not self._basis_matches_request(basis, trusted_request):
            raise OperationAuthorizationError("protected_operation_context_rejected")

        state, decision, after_decision = self._evaluate_current_authorization(
            basis,
            trusted_request,
        )

        binding = self._operation_binding(basis, trusted_request, decision)
        expires_at = min(
            after_decision + self.__operation_ttl,
            basis.context_expires_at,
            state.observed_at + self.__max_state_age,
        )
        return self.__registry.issue(binding, expires_at=expires_at)

    def consume(
        self,
        operation: AuthorizedOperation,
        context: RequestContext,
        request: AccessRequest,
    ) -> None:
        """Atomically validate exact operation scope and retire the handle before an effect."""

        result, failure = _invoke_boundary(
            partial(ProtectedOperationComposer._consume, self, operation, context, request)
        )
        if failure is None:
            return result
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, operation, context, request, result, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _consume(
        self,
        operation: AuthorizedOperation,
        context: RequestContext,
        request: AccessRequest,
    ) -> None:
        self._ensure_open()
        basis, admission_failure = _invoke_boundary(
            lambda: self.__issuer.prepare_reauthorization(context, request)
        )
        if admission_failure is not None:
            _raise_dependency_failure(
                admission_failure,
                "protected_operation_context_rejected",
            )
        if type(basis) is not ReauthorizationBasis:
            raise OperationAuthorizationError("protected_operation_context_rejected")
        trusted_request, request_failure = _invoke_boundary(
            lambda: _snapshot_access_request(request)
        )
        if request_failure is not None:
            _raise_dependency_failure(
                request_failure,
                "protected_operation_request_invalid",
            )
        if trusted_request is None:
            raise OperationAuthorizationError("protected_operation_request_invalid")
        if not self._basis_matches_request(basis, trusted_request):
            raise OperationAuthorizationError("protected_operation_context_rejected")
        self.__registry.check_request(operation, basis, trusted_request)
        self._evaluate_current_authorization(basis, trusted_request)
        refreshed_basis, refresh_failure = _invoke_boundary(
            lambda: self.__issuer.prepare_reauthorization(context, trusted_request)
        )
        if refresh_failure is not None:
            _raise_dependency_failure(
                refresh_failure,
                "protected_operation_context_rejected",
            )
        if type(refreshed_basis) is not ReauthorizationBasis or not self._same_context_basis(
            basis, refreshed_basis
        ):
            raise OperationAuthorizationError("protected_operation_context_rejected")
        self.__registry.consume_request(operation, refreshed_basis, trusted_request)

    def retire(self, operation: AuthorizedOperation) -> None:
        """Invalidate one exact issued handle without disclosing registry membership."""

        result, failure = _invoke_boundary(
            partial(ProtectedOperationComposer._retire, self, operation)
        )
        if failure is None:
            return result
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, operation, result, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _retire(self, operation: AuthorizedOperation) -> None:
        self._ensure_open()
        self.__registry.retire(operation)

    def close(self) -> None:
        """Invalidate all handles and reject future composition. Idempotent."""

        result, failure = _invoke_boundary(partial(ProtectedOperationComposer._close, self))
        if failure is None:
            return result
        failure_code, control_signal, system_exit_status = _operation_failure_details(
            failure,
            "protected_operation_internal_failure",
        )
        del self, result, failure
        if control_signal is not None:
            _raise_control_signal(control_signal, system_exit_status)
        try:
            raise OperationAuthorizationError(failure_code) from None
        except OperationAuthorizationError as public_error:
            public_error.__context__ = None
            raise

    def _close(self) -> None:
        """Private close path whose completed failure frames are always cleared."""

        self._ensure_process()
        with self.__lock:
            self.__closed = True
            self.__registry.close()

    def _enter(self) -> ProtectedOperationComposer:
        self._ensure_process()
        _require_current_process_issuer(self.__issuer)
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is not None
                and context_lease.owner_thread is owner_thread
                and not context_lease.active
                and context_lease.entry_callback() is None
            ):
                self.__context_lease = None
                context_lease = None
            if context_lease is not None:
                if context_lease.owner_thread is owner_thread and not context_lease.active:
                    self.__context_lease = None
                raise OperationAuthorizationError("protected_operation_internal_failure")
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_composer_closed")
        return self

    def _prepare_context_enter_lease(
        self,
        lease: object,
        entry_callback: weakref.ReferenceType[object],
    ) -> bool:
        if type(self) is not ProtectedOperationComposer:
            raise TypeError("composer must be an exact ProtectedOperationComposer")
        self._ensure_process()
        _require_current_process_issuer(self.__issuer)
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is not None
                and context_lease.owner_thread is owner_thread
                and not context_lease.active
                and context_lease.entry_callback() is None
            ):
                self.__context_lease = None
                context_lease = None
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_composer_closed")
            if context_lease is not None:
                raise OperationAuthorizationError("protected_operation_internal_failure")
            self.__context_lease = _ProtectedOperationContextLease(
                active=False,
                consumed=False,
                entry_callback=entry_callback,
                exit_bound=False,
                owner_thread=owner_thread,
                token=lease,
            )
        return True

    def _pending_context_exit_lease(self) -> object:
        self._ensure_process()
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is None
                or context_lease.active
                or context_lease.exit_bound
                or context_lease.owner_thread is not owner_thread
                or context_lease.entry_callback() is None
            ):
                raise OperationAuthorizationError("protected_operation_internal_failure")
            return context_lease.token

    def _bind_context_exit_lease(self, lease: object) -> bool:
        self._ensure_process()
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is None
                or context_lease.active
                or context_lease.exit_bound
                or context_lease.owner_thread is not owner_thread
                or context_lease.token is not lease
                or context_lease.entry_callback() is None
            ):
                raise OperationAuthorizationError("protected_operation_internal_failure")
            self.__context_lease = _ProtectedOperationContextLease(
                active=False,
                consumed=False,
                entry_callback=context_lease.entry_callback,
                exit_bound=True,
                owner_thread=context_lease.owner_thread,
                token=context_lease.token,
            )
        return True

    def _activate_context_exit_lease(self, lease: object) -> bool:
        if type(self) is not ProtectedOperationComposer:
            raise TypeError("composer must be an exact ProtectedOperationComposer")
        self._ensure_process()
        _require_current_process_issuer(self.__issuer)
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_composer_closed")
            if (
                context_lease is None
                or context_lease.active
                or context_lease.consumed
                or not context_lease.exit_bound
                or context_lease.owner_thread is not owner_thread
                or context_lease.token is not lease
                or context_lease.entry_callback() is None
            ):
                raise OperationAuthorizationError("protected_operation_internal_failure")
            self.__context_lease = _ProtectedOperationContextLease(
                active=True,
                consumed=False,
                entry_callback=context_lease.entry_callback,
                exit_bound=True,
                owner_thread=context_lease.owner_thread,
                token=context_lease.token,
            )
        return True

    def _consume_context_exit_lease(self, lease: object) -> bool:
        self._ensure_process()
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is None
                or not context_lease.active
                or context_lease.consumed
                or not context_lease.exit_bound
                or context_lease.owner_thread is not owner_thread
                or context_lease.token is not lease
            ):
                raise OperationAuthorizationError("protected_operation_internal_failure")
            self.__context_lease = _ProtectedOperationContextLease(
                active=True,
                consumed=True,
                entry_callback=context_lease.entry_callback,
                exit_bound=True,
                owner_thread=context_lease.owner_thread,
                token=context_lease.token,
            )
        return True

    def _reconcile_context_exit_lease(self, lease: object) -> bool:
        """Read the exact consume commit after a helper acknowledgement failure."""

        self._ensure_process()
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            return bool(
                context_lease is not None
                and context_lease.active
                and context_lease.consumed
                and context_lease.exit_bound
                and context_lease.owner_thread is owner_thread
                and context_lease.token is lease
            )

    def _finalize_context_exit_lease(self, lease: object) -> bool:
        """Retire one consumed lease only after its cleanup attempt."""

        self._ensure_process()
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is None
                or not context_lease.active
                or not context_lease.consumed
                or not context_lease.exit_bound
                or context_lease.owner_thread is not owner_thread
                or context_lease.token is not lease
            ):
                return False
            self.__context_lease = None
        return True

    def _discard_context_exit_lease(self, lease: object, include_active: bool) -> None:
        self._ensure_process()
        owner_thread = threading.current_thread()
        with self.__lock:
            context_lease = self.__context_lease
            if (
                context_lease is not None
                and context_lease.owner_thread is owner_thread
                and context_lease.token is lease
                and (include_active or not context_lease.active)
            ):
                self.__context_lease = None

    def __repr__(self) -> str:
        return "ProtectedOperationComposer(<configured>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("ProtectedOperationComposer cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("ProtectedOperationComposer cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("ProtectedOperationComposer cannot be serialized")

    def _evaluate_current_authorization(
        self,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> tuple[CurrentAuthorizationState, AuthorizationDecision, datetime]:
        before_load = self.__registry.observe_now()
        if basis.prepared_at > before_load + self.__max_clock_skew:
            raise OperationAuthorizationError("protected_operation_context_time_invalid")
        if basis.context_expires_at <= before_load:
            raise OperationAuthorizationError("protected_operation_context_expired")

        loaded_state, provider_failure = _invoke_boundary(
            lambda: self.__provider.load_current_state(basis, request)
        )
        if provider_failure is not None:
            _raise_dependency_failure(
                provider_failure,
                "protected_operation_state_unavailable",
            )

        state, state_failure = _invoke_boundary(lambda: self._snapshot_state(loaded_state))
        if state_failure is not None:
            _raise_dependency_failure(
                state_failure,
                "protected_operation_state_invalid",
            )
        if state is None:
            raise OperationAuthorizationError("protected_operation_state_invalid")
        self._validate_state_binding(state, basis)
        if state.observed_at > before_load + self.__max_clock_skew:
            raise OperationAuthorizationError("protected_operation_state_time_invalid")
        if before_load - state.observed_at >= self.__max_state_age:
            raise OperationAuthorizationError("protected_operation_state_stale")

        raw_decision, authorizer_failure = _invoke_boundary(
            lambda: self.__authorizer.evaluate(
                request,
                state.member,
                state.revocations,
                state.verified_capabilities,
            )
        )
        if authorizer_failure is not None:
            _raise_dependency_failure(
                authorizer_failure,
                "protected_operation_authorizer_failed",
            )

        decision, decision_failure = _invoke_boundary(lambda: self._snapshot_decision(raw_decision))
        if decision_failure is not None:
            _raise_dependency_failure(
                decision_failure,
                "protected_operation_decision_invalid",
            )
        if decision is None or decision.request != request:
            raise OperationAuthorizationError("protected_operation_decision_invalid")

        after_decision = self.__registry.observe_now()
        if basis.context_expires_at <= after_decision:
            raise OperationAuthorizationError("protected_operation_context_expired")
        if after_decision - state.observed_at >= self.__max_state_age:
            raise OperationAuthorizationError("protected_operation_state_stale")
        if (
            decision.evaluated_at > after_decision + self.__max_clock_skew
            or after_decision - decision.evaluated_at > self.__max_clock_skew
        ):
            raise OperationAuthorizationError("protected_operation_decision_time_invalid")
        if decision.outcome is not AuthorizationOutcome.ALLOW or decision.allowed is not True:
            raise OperationAuthorizationError("protected_operation_denied")
        return state, decision, after_decision

    def _ensure_open(self) -> None:
        self._ensure_process()
        _require_current_process_issuer(self.__issuer)
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_composer_closed")

    def _ensure_process(self) -> None:
        _require_process_identity(self.__owner_pid, self.__owner_epoch)

    @staticmethod
    def _basis_matches_request(basis: ReauthorizationBasis, request: AccessRequest) -> bool:
        workspace = request.resource.workspace_id
        return (
            workspace is not None
            and (
                basis.request_id,
                basis.subject_id,
                basis.tenant_id,
                basis.workspace_id,
            )
            == (
                request.request_id,
                request.subject_id,
                request.tenant_id,
                workspace,
            )
            and request.resource.tenant_id == request.tenant_id
        )

    @staticmethod
    def _same_context_basis(
        first: ReauthorizationBasis,
        second: ReauthorizationBasis,
    ) -> bool:
        return (
            first.context_id,
            first.authenticator_id,
            first.audience,
            first.request_id,
            first.principal_id,
            first.subject_id,
            first.tenant_id,
            first.workspace_id,
            first.identity_revision,
            first.scope_revision,
            first.evidence_fingerprint,
            first.authenticated_at,
            first.context_issued_at,
            first.context_expires_at,
        ) == (
            second.context_id,
            second.authenticator_id,
            second.audience,
            second.request_id,
            second.principal_id,
            second.subject_id,
            second.tenant_id,
            second.workspace_id,
            second.identity_revision,
            second.scope_revision,
            second.evidence_fingerprint,
            second.authenticated_at,
            second.context_issued_at,
            second.context_expires_at,
        )

    @staticmethod
    def _snapshot_state(value: object) -> CurrentAuthorizationState:
        if type(value) is not CurrentAuthorizationState:
            raise TypeError("state must be an exact CurrentAuthorizationState")
        state = value
        return CurrentAuthorizationState(
            context_id=state.context_id,
            authenticator_id=state.authenticator_id,
            audience=state.audience,
            request_id=state.request_id,
            principal_id=state.principal_id,
            subject_id=state.subject_id,
            tenant_id=TenantId(str(state.tenant_id)),
            workspace_id=WorkspaceId(str(state.workspace_id)),
            identity_revision=state.identity_revision,
            scope_revision=state.scope_revision,
            observed_at=state.observed_at,
            member=state.member,
            revocations=state.revocations,
            verified_capabilities=state.verified_capabilities,
        )

    @staticmethod
    def _validate_state_binding(
        state: CurrentAuthorizationState,
        basis: ReauthorizationBasis,
    ) -> None:
        if (
            state.context_id,
            state.authenticator_id,
            state.audience,
            state.request_id,
            state.principal_id,
            state.subject_id,
            state.tenant_id,
            state.workspace_id,
        ) != (
            basis.context_id,
            basis.authenticator_id,
            basis.audience,
            basis.request_id,
            basis.principal_id,
            basis.subject_id,
            basis.tenant_id,
            basis.workspace_id,
        ):
            raise OperationAuthorizationError("protected_operation_state_mismatch")
        if state.identity_revision != basis.identity_revision:
            raise OperationAuthorizationError("protected_operation_identity_revision_stale")
        if state.scope_revision != basis.scope_revision:
            raise OperationAuthorizationError("protected_operation_scope_revision_stale")

    @staticmethod
    def _snapshot_decision(value: object) -> AuthorizationDecision:
        if type(value) is not AuthorizationDecision:
            raise TypeError("decision must be an exact AuthorizationDecision")
        decision = value
        return AuthorizationDecision.from_dict(decision.to_dict())

    @staticmethod
    def _operation_binding(
        basis: ReauthorizationBasis,
        request: AccessRequest,
        decision: AuthorizationDecision,
    ) -> _OperationBinding:
        workspace = request.resource.workspace_id
        if workspace is None:
            raise OperationAuthorizationError("protected_operation_workspace_required")
        return _OperationBinding(
            context_id=basis.context_id,
            authenticator_id=basis.authenticator_id,
            audience=basis.audience,
            request_id=request.request_id,
            principal_id=basis.principal_id,
            subject_id=request.subject_id,
            tenant_id=request.tenant_id,
            workspace_id=workspace,
            action=request.action,
            resource_type=request.resource.resource_type,
            resource_id=request.resource.resource_id,
            decision_id=decision.decision_id,
            identity_revision=basis.identity_revision,
            scope_revision=basis.scope_revision,
        )


def __enter__(
    composer: ProtectedOperationComposer,
    *,
    _context_exit_lease: Optional[object] = None,
) -> ProtectedOperationComposer:
    callback: Callable[[], object]
    if _context_exit_lease is None:
        callback = partial(ProtectedOperationComposer._enter, composer)
    else:
        callback = partial(
            ProtectedOperationComposer._activate_context_exit_lease,
            composer,
            _context_exit_lease,
        )
    entered, failure = _invoke_boundary(callback)
    if failure is None and (
        (
            _context_exit_lease is None
            and type(entered) is ProtectedOperationComposer
            and entered is composer
        )
        or (_context_exit_lease is not None and entered is True)
    ):
        return composer
    if _context_exit_lease is not None:
        _invoke_boundary(
            partial(
                ProtectedOperationComposer._discard_context_exit_lease,
                composer,
                _context_exit_lease,
                True,
            )
        )
    failure_code, control_signal, system_exit_status = _operation_failure_details(
        failure,
        "protected_operation_internal_failure",
    )
    del composer, _context_exit_lease, callback, entered, failure
    if control_signal is not None:
        _raise_control_signal(control_signal, system_exit_status)
    try:
        raise OperationAuthorizationError(failure_code) from None
    except OperationAuthorizationError as public_error:
        public_error.__context__ = None
        raise


def __exit__(
    composer: ProtectedOperationComposer,
    exc_type: object,
    exc_value: object,
    trace: object,
    *,
    _context_exit_lease: Optional[object] = None,
) -> None:
    authenticated_context_exit = False
    originating_control_signal = False
    cleanup_required = _context_exit_lease is None
    authenticated: Optional[object] = None
    authentication_failure: Optional[_BoundaryFailure] = None
    reconciled: Optional[object] = None
    reconciliation_failure: Optional[_BoundaryFailure] = None
    reconciliation_completed = False
    _reconciliation_attempt = 0
    pending_failure: Optional[_BoundaryFailure] = None
    cleanup_result: Optional[object] = None
    cleanup_failure: Optional[_BoundaryFailure] = None
    finalized: Optional[object] = None
    finalization_failure: Optional[_BoundaryFailure] = None

    try:
        if _context_exit_lease is not None:
            authenticated, authentication_failure = _invoke_boundary(
                partial(
                    ProtectedOperationComposer._consume_context_exit_lease,
                    composer,
                    _context_exit_lease,
                )
            )
            pending_failure = authentication_failure
            if authentication_failure is None and authenticated is True:
                authenticated_context_exit = True
                cleanup_required = True
            else:
                for _reconciliation_attempt in range(
                    1,
                    _CONTEXT_EXIT_RECONCILIATION_ATTEMPTS + 1,
                ):
                    reconciled, reconciliation_failure = _invoke_boundary(
                        partial(
                            ProtectedOperationComposer._reconcile_context_exit_lease,
                            composer,
                            _context_exit_lease,
                        )
                    )
                    if reconciliation_failure is not None:
                        pending_failure = reconciliation_failure
                        continue
                    reconciliation_completed = True
                    if reconciled is True:
                        authenticated_context_exit = True
                        cleanup_required = True
                    else:
                        cleanup_required = False
                    break
                if not reconciliation_completed:
                    cleanup_required = True

        if authenticated_context_exit:
            active_exc_type, active_exc_value, active_trace = sys.exc_info()
            originating_control_signal = (
                active_exc_type is exc_type
                and active_exc_value is exc_value
                and active_trace is trace
                and (
                    type(exc_value) is KeyboardInterrupt
                    or type(exc_value) is SystemExit
                    or type(exc_value) is GeneratorExit
                    or type(exc_value) is asyncio.CancelledError
                )
            )
            del active_exc_type, active_exc_value, active_trace
    finally:
        if cleanup_required:
            cleanup_result, cleanup_failure = _invoke_boundary(
                partial(ProtectedOperationComposer._close, composer)
            )
            if _context_exit_lease is not None:
                finalized, finalization_failure = _invoke_boundary(
                    partial(
                        ProtectedOperationComposer._finalize_context_exit_lease,
                        composer,
                        _context_exit_lease,
                    )
                )

    if originating_control_signal:
        del (
            composer,
            exc_type,
            exc_value,
            trace,
            _context_exit_lease,
            authenticated,
            authentication_failure,
            reconciled,
            reconciliation_failure,
            pending_failure,
            cleanup_result,
            cleanup_failure,
            finalized,
            finalization_failure,
        )
        return None

    selected_failure: Optional[_BoundaryFailure] = None
    internal_failure = False
    if _context_exit_lease is None:
        if cleanup_failure is None:
            return None
        selected_failure = cleanup_failure
    elif not authenticated_context_exit:
        selected_failure = pending_failure
        internal_failure = selected_failure is None
    elif finalization_failure is not None:
        selected_failure = finalization_failure
    elif finalized is not True:
        internal_failure = True
    elif cleanup_failure is not None:
        selected_failure = cleanup_failure
    elif pending_failure is not None:
        selected_failure = pending_failure
    else:
        return None

    failure_code, control_signal, system_exit_status = _operation_failure_details(
        selected_failure,
        "protected_operation_internal_failure",
    )
    if internal_failure:
        failure_code = "protected_operation_internal_failure"
    del (
        composer,
        exc_type,
        exc_value,
        trace,
        _context_exit_lease,
        authenticated,
        authentication_failure,
        reconciled,
        reconciliation_failure,
        pending_failure,
        cleanup_result,
        cleanup_failure,
        finalized,
        finalization_failure,
        selected_failure,
    )
    if control_signal is not None:
        _raise_control_signal(control_signal, system_exit_status)
    try:
        raise OperationAuthorizationError(failure_code) from None
    except OperationAuthorizationError as public_error:
        public_error.__context__ = None
        raise


__all__ = [
    "AuthorizedOperation",
    "CurrentAuthorizationState",
    "CurrentAuthorizationStateProvider",
    "OperationAuthorizationError",
    "ProtectedOperationComposer",
]
