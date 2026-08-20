# ruff: noqa: UP007, UP045 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Protected-operation authorization composition boundaries.

Values returned by ``CurrentAuthorizationStateProvider`` are trusted adapter
inputs, not authorization grants.  Only the operation composer introduced by
this module may turn a fresh, matching state into an opaque operation handle.
"""

from __future__ import annotations

import re
import secrets
import threading
import traceback
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NoReturn, Optional, Protocol, SupportsIndex

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


class _AuthorizedOperationRegistry:
    """Thread-safe, bounded process-local registry owned by exactly one composer."""

    __slots__ = (
        "__active",
        "__clock",
        "__closed",
        "__last_observed_at",
        "__lock",
        "__max_active_operations",
        "__max_clock_skew",
        "__max_operation_ttl",
    )

    def __init__(
        self,
        *,
        clock: ServerClock,
        max_operation_ttl: timedelta = timedelta(seconds=30),
        max_clock_skew: timedelta = timedelta(seconds=30),
        max_active_operations: int = 10_000,
    ) -> None:
        if clock is None or not callable(getattr(clock, "now", None)):
            raise TypeError("clock must implement now()")
        self.__max_operation_ttl = _require_duration(
            max_operation_ttl,
            "max_operation_ttl",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(minutes=5),
        )
        self.__max_clock_skew = _require_duration(
            max_clock_skew,
            "max_clock_skew",
            minimum=timedelta(0),
            maximum=timedelta(minutes=5),
        )
        if isinstance(max_active_operations, bool) or not isinstance(max_active_operations, int):
            raise TypeError("max_active_operations must be an integer")
        if not 1 <= max_active_operations <= 1_000_000:
            raise ValueError("max_active_operations is outside the supported range")
        self.__clock = clock
        self.__max_active_operations = max_active_operations
        self.__active: dict[
            int, tuple[weakref.ReferenceType[AuthorizedOperation], _OperationSnapshot]
        ] = {}
        self.__lock = threading.RLock()
        self.__closed = False
        self.__last_observed_at: Optional[datetime] = None

    def issue(
        self,
        binding: _OperationBinding,
        *,
        expires_at: datetime,
    ) -> AuthorizedOperation:
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

        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            return self._clock_now()

    def verify(self, operation: AuthorizedOperation, expected: _OperationBinding) -> None:
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

    def consume_request(
        self,
        operation: AuthorizedOperation,
        basis: ReauthorizationBasis,
        request: AccessRequest,
    ) -> None:
        """Atomically verify exact actor/request scope and retire on success."""

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
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_registry_closed")
            self._trusted_snapshot(operation)
            self.__active.pop(id(operation), None)

    def close(self) -> None:
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

    def _clock_now(self) -> datetime:
        normalized: Optional[datetime] = None
        failed = False
        try:
            normalized = _as_utc(self.__clock.now(), "clock.now()")
        except Exception:
            failed = True
        if failed or normalized is None:
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
        try:
            return _OperationBinding(
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
        except Exception:
            raise OperationAuthorizationError("protected_operation_binding_invalid") from None

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
        try:
            for _ in range(4):
                candidate = f"op_{secrets.token_hex(32)}"
                if candidate not in existing:
                    return candidate
        except Exception:
            pass
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
        try:
            actual = operation._bound_values()
        except Exception:
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
        return workspace is not None and (
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
        ) == (
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
        ) and (
            basis.request_id,
            basis.subject_id,
            basis.tenant_id,
            basis.workspace_id,
        ) == (
            request.request_id,
            request.subject_id,
            request.tenant_id,
            workspace,
        ) and request.resource.tenant_id == request.tenant_id

    def _prune(self, now: datetime) -> None:
        stale = [
            identifier
            for identifier, (reference, snapshot) in self.__active.items()
            if reference() is None or snapshot.expires_at <= now
        ]
        for identifier in stale:
            del self.__active[identifier]


class ProtectedOperationComposer:
    """Compose request admission, current state, and tenant authorization exactly once."""

    __slots__ = (
        "__authorizer",
        "__closed",
        "__issuer",
        "__lock",
        "__max_clock_skew",
        "__max_state_age",
        "__operation_ttl",
        "__provider",
        "__registry",
    )

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
        if type(issuer) is not RequestContextIssuer:
            raise TypeError("issuer must be an exact RequestContextIssuer")
        if state_provider is None or not callable(
            getattr(state_provider, "load_current_state", None)
        ):
            raise TypeError("state_provider must implement load_current_state")
        if type(authorizer) is not TenantAuthorizer:
            raise TypeError("authorizer must be an exact TenantAuthorizer")
        self.__operation_ttl = _require_duration(
            operation_ttl,
            "operation_ttl",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(minutes=5),
        )
        self.__max_state_age = _require_duration(
            max_state_age,
            "max_state_age",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(minutes=5),
        )
        self.__max_clock_skew = _require_duration(
            max_clock_skew,
            "max_clock_skew",
            minimum=timedelta(0),
            maximum=timedelta(minutes=5),
        )
        configured_clock = clock if clock is not None else SystemClock()
        self.__registry = _AuthorizedOperationRegistry(
            clock=configured_clock,
            max_operation_ttl=self.__operation_ttl,
            max_clock_skew=self.__max_clock_skew,
            max_active_operations=max_active_operations,
        )
        self.__issuer = issuer
        self.__provider = state_provider
        self.__authorizer = authorizer
        self.__lock = threading.RLock()
        self.__closed = False

    def authorize(
        self,
        context: RequestContext,
        request: AccessRequest,
    ) -> AuthorizedOperation:
        """Return one bounded opaque handle only after an explicit ALLOW decision."""

        try:
            return self._authorize(context, request)
        except OperationAuthorizationError as error:
            failure_code = error.code
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        except Exception as error:
            failure_code = "protected_operation_internal_failure"
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        del self, context, request
        raise OperationAuthorizationError(failure_code)

    def _authorize(
        self,
        context: RequestContext,
        request: AccessRequest,
    ) -> AuthorizedOperation:
        self._ensure_open()
        basis: Optional[ReauthorizationBasis] = None
        admission_failed = False
        try:
            basis = self.__issuer.prepare_reauthorization(context, request)
        except Exception:
            admission_failed = True
        if admission_failed or type(basis) is not ReauthorizationBasis:
            raise OperationAuthorizationError("protected_operation_context_rejected")

        trusted_request: Optional[AccessRequest] = None
        request_invalid = False
        try:
            trusted_request = _snapshot_access_request(request)
        except Exception:
            request_invalid = True
        if request_invalid or trusted_request is None:
            raise OperationAuthorizationError("protected_operation_request_invalid")
        workspace = trusted_request.resource.workspace_id
        if basis.workspace_id is None or workspace is None:
            raise OperationAuthorizationError("protected_operation_workspace_required")
        if not self._basis_matches_request(basis, trusted_request):
            raise OperationAuthorizationError("protected_operation_context_rejected")

        before_load = self.__registry.observe_now()
        if basis.prepared_at > before_load + self.__max_clock_skew:
            raise OperationAuthorizationError("protected_operation_context_time_invalid")
        if basis.context_expires_at <= before_load:
            raise OperationAuthorizationError("protected_operation_context_expired")

        loaded_state: Optional[CurrentAuthorizationState] = None
        provider_failed = False
        try:
            loaded_state = self.__provider.load_current_state(basis, trusted_request)
        except Exception:
            provider_failed = True
        if provider_failed:
            raise OperationAuthorizationError("protected_operation_state_unavailable")

        state: Optional[CurrentAuthorizationState] = None
        state_invalid = False
        try:
            state = self._snapshot_state(loaded_state)
        except Exception:
            state_invalid = True
        if state_invalid or state is None:
            raise OperationAuthorizationError("protected_operation_state_invalid")
        self._validate_state_binding(state, basis)
        if state.observed_at > before_load + self.__max_clock_skew:
            raise OperationAuthorizationError("protected_operation_state_time_invalid")
        if before_load - state.observed_at >= self.__max_state_age:
            raise OperationAuthorizationError("protected_operation_state_stale")

        raw_decision: Optional[AuthorizationDecision] = None
        authorizer_failed = False
        try:
            raw_decision = self.__authorizer.evaluate(
                trusted_request,
                state.member,
                state.revocations,
                state.verified_capabilities,
            )
        except Exception:
            authorizer_failed = True
        if authorizer_failed:
            raise OperationAuthorizationError("protected_operation_authorizer_failed")

        decision: Optional[AuthorizationDecision] = None
        decision_invalid = False
        try:
            decision = self._snapshot_decision(raw_decision)
        except Exception:
            decision_invalid = True
        if decision_invalid or decision is None or decision.request != trusted_request:
            raise OperationAuthorizationError("protected_operation_decision_invalid")

        after_decision = self.__registry.observe_now()
        if (
            decision.evaluated_at > after_decision + self.__max_clock_skew
            or after_decision - decision.evaluated_at > self.__max_clock_skew
        ):
            raise OperationAuthorizationError("protected_operation_decision_time_invalid")
        if decision.outcome is not AuthorizationOutcome.ALLOW or decision.allowed is not True:
            raise OperationAuthorizationError("protected_operation_denied")

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

        try:
            return self._consume(operation, context, request)
        except OperationAuthorizationError as error:
            failure_code = error.code
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        except Exception as error:
            failure_code = "protected_operation_internal_failure"
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        del self, operation, context, request
        raise OperationAuthorizationError(failure_code)

    def _consume(
        self,
        operation: AuthorizedOperation,
        context: RequestContext,
        request: AccessRequest,
    ) -> None:
        self._ensure_open()
        basis: Optional[ReauthorizationBasis] = None
        admission_failed = False
        try:
            basis = self.__issuer.prepare_reauthorization(context, request)
        except Exception:
            admission_failed = True
        if admission_failed or type(basis) is not ReauthorizationBasis:
            raise OperationAuthorizationError("protected_operation_context_rejected")
        trusted_request: Optional[AccessRequest] = None
        request_invalid = False
        try:
            trusted_request = _snapshot_access_request(request)
        except Exception:
            request_invalid = True
        if request_invalid or trusted_request is None:
            raise OperationAuthorizationError("protected_operation_request_invalid")
        if not self._basis_matches_request(basis, trusted_request):
            raise OperationAuthorizationError("protected_operation_context_rejected")
        self.__registry.consume_request(operation, basis, trusted_request)

    def retire(self, operation: AuthorizedOperation) -> None:
        """Invalidate one exact issued handle without disclosing registry membership."""

        try:
            self._ensure_open()
            self.__registry.retire(operation)
            return
        except OperationAuthorizationError as error:
            failure_code = error.code
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        except Exception as error:
            failure_code = "protected_operation_internal_failure"
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        del self, operation
        raise OperationAuthorizationError(failure_code)

    def close(self) -> None:
        """Invalidate all handles and reject future composition. Idempotent."""

        with self.__lock:
            self.__closed = True
            self.__registry.close()

    def __enter__(self) -> ProtectedOperationComposer:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, trace: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "ProtectedOperationComposer(<configured>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("ProtectedOperationComposer cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("ProtectedOperationComposer cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("ProtectedOperationComposer cannot be serialized")

    def _ensure_open(self) -> None:
        with self.__lock:
            if self.__closed:
                raise OperationAuthorizationError("protected_operation_composer_closed")

    @staticmethod
    def _basis_matches_request(basis: ReauthorizationBasis, request: AccessRequest) -> bool:
        workspace = request.resource.workspace_id
        return workspace is not None and (
            basis.request_id,
            basis.subject_id,
            basis.tenant_id,
            basis.workspace_id,
        ) == (
            request.request_id,
            request.subject_id,
            request.tenant_id,
            workspace,
        ) and request.resource.tenant_id == request.tenant_id

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


__all__ = [
    "AuthorizedOperation",
    "CurrentAuthorizationState",
    "CurrentAuthorizationStateProvider",
    "OperationAuthorizationError",
    "ProtectedOperationComposer",
]
