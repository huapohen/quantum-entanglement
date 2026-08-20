# ruff: noqa: UP007, UP045 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Process-local issuance boundary for authenticated request scope.

CallerRequestContext is canonical but untrusted. AuthenticatedRequestBinding is trusted
only when it is the immediate return from the authenticator configured on a
RequestContextIssuer; constructing either value directly grants no authority.
"""

from __future__ import annotations

import re
import secrets
import threading
import traceback
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn, Optional, Protocol, SupportsIndex

from .service.secrets import SecretMaterial
from .tenancy import AccessRequest, ServerClock, SystemClock, TenantId, WorkspaceId

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_opaque_id(value: str, field_name: str) -> str:
    if type(value) is not str or _OPAQUE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical opaque identifier")
    return value


def _require_revision(value: str, field_name: str) -> str:
    if type(value) is not str or _REVISION.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical opaque revision")
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


def _strict_object(value: Any, *, required: frozenset[str], field_name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be an exact object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    actual = frozenset(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing:
        raise ValueError(f"{field_name} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{field_name} contains unknown fields: {', '.join(unknown)}")
    return value


def _string(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    return value


@dataclass(frozen=True, repr=False)
class CallerRequestContext:
    """Strictly parsed subject/scope claims that remain caller-controlled."""

    request_id: str
    subject_id: str
    tenant_id: TenantId
    workspace_id: Optional[WorkspaceId]

    def __post_init__(self) -> None:
        _require_opaque_id(self.request_id, "request_id")
        _require_opaque_id(self.subject_id, "subject_id")
        if type(self.tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        if self.workspace_id is not None and type(self.workspace_id) is not WorkspaceId:
            raise TypeError("workspace_id must be an exact WorkspaceId or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "subjectId": self.subject_id,
            "tenantId": str(self.tenant_id),
            "workspaceId": str(self.workspace_id) if self.workspace_id is not None else None,
        }

    def __str__(self) -> str:
        return "CallerRequestContext<untrusted>"

    def __repr__(self) -> str:
        return "CallerRequestContext(<untrusted>)"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CallerRequestContext:
        data = _strict_object(
            value,
            required=frozenset(("requestId", "subjectId", "tenantId", "workspaceId")),
            field_name="caller request context",
        )
        raw_workspace = data["workspaceId"]
        if raw_workspace is not None and type(raw_workspace) is not str:
            raise TypeError("caller request context.workspaceId must be a string or null")
        return cls(
            request_id=_string(data["requestId"], "caller request context.requestId"),
            subject_id=_string(data["subjectId"], "caller request context.subjectId"),
            tenant_id=TenantId(_string(data["tenantId"], "caller request context.tenantId")),
            workspace_id=WorkspaceId(raw_workspace) if raw_workspace is not None else None,
        )


@dataclass(frozen=True, repr=False)
class AuthenticatedRequestBinding:
    """Bounded authenticator output; it is not a free-standing authorization token."""

    authenticator_id: str
    audience: str
    request_id: str
    principal_id: str
    subject_id: str
    tenant_id: TenantId
    workspace_id: Optional[WorkspaceId]
    identity_revision: str
    scope_revision: str
    evidence_fingerprint: str
    authenticated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_opaque_id(self.authenticator_id, "authenticator_id")
        _require_text(self.audience, "audience")
        _require_opaque_id(self.request_id, "request_id")
        _require_opaque_id(self.principal_id, "principal_id")
        _require_opaque_id(self.subject_id, "subject_id")
        if type(self.tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        if self.workspace_id is not None and type(self.workspace_id) is not WorkspaceId:
            raise TypeError("workspace_id must be an exact WorkspaceId or None")
        _require_revision(self.identity_revision, "identity_revision")
        _require_revision(self.scope_revision, "scope_revision")
        if (
            type(self.evidence_fingerprint) is not str
            or _SHA256.fullmatch(self.evidence_fingerprint) is None
        ):
            raise ValueError("evidence_fingerprint must be a lower-case SHA-256 digest")
        authenticated_at = _as_utc(self.authenticated_at, "authenticated_at")
        expires_at = _as_utc(self.expires_at, "expires_at")
        if expires_at <= authenticated_at:
            raise ValueError("expires_at must be later than authenticated_at")
        object.__setattr__(self, "authenticated_at", authenticated_at)
        object.__setattr__(self, "expires_at", expires_at)

    def __str__(self) -> str:
        return "AuthenticatedRequestBinding<adapter-result>"

    def __repr__(self) -> str:
        return "AuthenticatedRequestBinding(<adapter-result>)"


class RequestAuthenticator(Protocol):
    """Trusted adapter called directly by a RequestContextIssuer."""

    def authenticate(
        self,
        claims: CallerRequestContext,
        credential: memoryview,
        *,
        audience: str,
        at: datetime,
    ) -> AuthenticatedRequestBinding:
        """Verify the credential and bind the exact caller claims or fail closed."""


class RequestContextError(RuntimeError):
    """Redacted request-context failure with one stable machine-readable code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        _require_opaque_id(code, "request context error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _ContextSnapshot:
    context_id: str
    authenticator_id: str
    audience: str
    request_id: str
    principal_id: str
    subject_id: str
    tenant_value: str
    workspace_value: Optional[str]
    identity_revision: str
    scope_revision: str
    evidence_fingerprint: str
    authenticated_at: datetime
    issued_at: datetime
    expires_at: datetime


_CONTEXT_CONSTRUCTION_TOKEN = object()


class RequestContext:
    """Opaque process-local handle; only its issuer can establish that it is trusted."""

    __slots__ = (
        "__authenticated_at",
        "__authenticator_id",
        "__audience",
        "__context_id",
        "__evidence_fingerprint",
        "__expires_at",
        "__identity_revision",
        "__issued_at",
        "__principal_id",
        "__request_id",
        "__scope_revision",
        "__subject_id",
        "__tenant_value",
        "__weakref__",
        "__workspace_value",
    )

    def __init__(self, snapshot: _ContextSnapshot, token: object) -> None:
        if token is not _CONTEXT_CONSTRUCTION_TOKEN or type(snapshot) is not _ContextSnapshot:
            raise TypeError("RequestContext instances are issued by RequestContextIssuer")
        self.__context_id = snapshot.context_id
        self.__authenticator_id = snapshot.authenticator_id
        self.__audience = snapshot.audience
        self.__request_id = snapshot.request_id
        self.__principal_id = snapshot.principal_id
        self.__subject_id = snapshot.subject_id
        self.__tenant_value = snapshot.tenant_value
        self.__workspace_value = snapshot.workspace_value
        self.__identity_revision = snapshot.identity_revision
        self.__scope_revision = snapshot.scope_revision
        self.__evidence_fingerprint = snapshot.evidence_fingerprint
        self.__authenticated_at = snapshot.authenticated_at
        self.__issued_at = snapshot.issued_at
        self.__expires_at = snapshot.expires_at

    @property
    def context_id(self) -> str:
        return self.__context_id

    @property
    def authenticator_id(self) -> str:
        return self.__authenticator_id

    @property
    def audience(self) -> str:
        return self.__audience

    @property
    def request_id(self) -> str:
        return self.__request_id

    @property
    def principal_id(self) -> str:
        return self.__principal_id

    @property
    def subject_id(self) -> str:
        return self.__subject_id

    @property
    def tenant_id(self) -> TenantId:
        return TenantId(self.__tenant_value)

    @property
    def workspace_id(self) -> Optional[WorkspaceId]:
        return WorkspaceId(self.__workspace_value) if self.__workspace_value is not None else None

    @property
    def identity_revision(self) -> str:
        return self.__identity_revision

    @property
    def scope_revision(self) -> str:
        return self.__scope_revision

    @property
    def evidence_fingerprint(self) -> str:
        return self.__evidence_fingerprint

    @property
    def authenticated_at(self) -> datetime:
        return self.__authenticated_at

    @property
    def issued_at(self) -> datetime:
        return self.__issued_at

    @property
    def expires_at(self) -> datetime:
        return self.__expires_at

    def __str__(self) -> str:
        return "RequestContext<opaque>"

    def __repr__(self) -> str:
        return "RequestContext(<opaque>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("RequestContext cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("RequestContext cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("RequestContext cannot be serialized")


@dataclass(frozen=True, repr=False)
class ReauthorizationBasis:
    """Non-authorizing identity evidence prepared for a current-state lookup."""

    context_id: str
    authenticator_id: str
    audience: str
    request_id: str
    principal_id: str
    subject_id: str
    tenant_id: TenantId
    workspace_id: Optional[WorkspaceId]
    identity_revision: str
    scope_revision: str
    evidence_fingerprint: str
    authenticated_at: datetime
    context_issued_at: datetime
    context_expires_at: datetime
    prepared_at: datetime

    def __post_init__(self) -> None:
        _require_opaque_id(self.context_id, "context_id")
        _require_opaque_id(self.authenticator_id, "authenticator_id")
        _require_text(self.audience, "audience")
        _require_opaque_id(self.request_id, "request_id")
        _require_opaque_id(self.principal_id, "principal_id")
        _require_opaque_id(self.subject_id, "subject_id")
        if type(self.tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        if self.workspace_id is not None and type(self.workspace_id) is not WorkspaceId:
            raise TypeError("workspace_id must be an exact WorkspaceId or None")
        _require_revision(self.identity_revision, "identity_revision")
        _require_revision(self.scope_revision, "scope_revision")
        if (
            type(self.evidence_fingerprint) is not str
            or _SHA256.fullmatch(self.evidence_fingerprint) is None
        ):
            raise ValueError("evidence_fingerprint must be a lower-case SHA-256 digest")
        authenticated_at = _as_utc(self.authenticated_at, "authenticated_at")
        context_issued_at = _as_utc(self.context_issued_at, "context_issued_at")
        context_expires_at = _as_utc(self.context_expires_at, "context_expires_at")
        prepared_at = _as_utc(self.prepared_at, "prepared_at")
        if context_expires_at <= context_issued_at:
            raise ValueError("context expiry must be later than issuance")
        if prepared_at >= context_expires_at:
            raise ValueError("reauthorization basis cannot be prepared after context expiry")
        object.__setattr__(self, "authenticated_at", authenticated_at)
        object.__setattr__(self, "context_issued_at", context_issued_at)
        object.__setattr__(self, "context_expires_at", context_expires_at)
        object.__setattr__(self, "prepared_at", prepared_at)

    def __str__(self) -> str:
        return "ReauthorizationBasis<non-authorizing>"

    def __repr__(self) -> str:
        return "ReauthorizationBasis(<non-authorizing>)"


class RequestContextIssuer:
    """Call one trusted authenticator and register bounded process-local contexts."""

    __slots__ = (
        "__active",
        "__audience",
        "__authenticator",
        "__authenticator_id",
        "__clock",
        "__closed",
        "__last_observed_at",
        "__lock",
        "__max_active_contexts",
        "__max_clock_skew",
        "__max_context_ttl",
        "__pending",
    )

    def __init__(
        self,
        *,
        authenticator: RequestAuthenticator,
        authenticator_id: str,
        audience: str,
        clock: Optional[ServerClock] = None,
        max_context_ttl: timedelta = timedelta(minutes=5),
        max_clock_skew: timedelta = timedelta(seconds=30),
        max_active_contexts: int = 10_000,
    ) -> None:
        _require_opaque_id(authenticator_id, "authenticator_id")
        _require_text(audience, "audience")
        self._validate_duration(
            max_context_ttl,
            "max_context_ttl",
            minimum=timedelta(microseconds=1),
            maximum=timedelta(days=1),
        )
        self._validate_duration(
            max_clock_skew,
            "max_clock_skew",
            minimum=timedelta(0),
            maximum=timedelta(minutes=5),
        )
        if isinstance(max_active_contexts, bool) or not isinstance(max_active_contexts, int):
            raise TypeError("max_active_contexts must be an integer")
        if not 1 <= max_active_contexts <= 1_000_000:
            raise ValueError("max_active_contexts is outside the supported range")
        self.__authenticator = authenticator
        self.__authenticator_id = authenticator_id
        self.__audience = audience
        self.__clock = clock if clock is not None else SystemClock()
        self.__max_context_ttl = max_context_ttl
        self.__max_clock_skew = max_clock_skew
        self.__max_active_contexts = max_active_contexts
        self.__active: dict[
            int, tuple[weakref.ReferenceType[RequestContext], _ContextSnapshot]
        ] = {}
        self.__lock = threading.RLock()
        self.__closed = False
        self.__last_observed_at: Optional[datetime] = None
        self.__pending = 0

    def issue(
        self,
        claims: CallerRequestContext,
        credential: SecretMaterial,
    ) -> RequestContext:
        """Authenticate exact caller claims and consume one credential lease."""

        if type(credential) is not SecretMaterial:
            raise TypeError("credential must be an exact SecretMaterial")
        try:
            return self._issue(claims, credential)
        except RequestContextError as error:
            failure_code = error.code
            internal_traceback = error.__traceback__
            error.__cause__ = None
            error.__context__ = None
            error.__traceback__ = None
            if internal_traceback is not None:
                # Python 3.9 skips the currently executing ``issue`` frame and clears
                # completed private frames, including any credential memoryview local.
                traceback.clear_frames(internal_traceback)
            del internal_traceback
        # Re-issue the bounded error after the internal exception and traceback have
        # left scope. A wipe failure may leave the private issue frame's memoryview
        # readable, so merely clearing ``__context__`` would not be a complete boundary.
        del self, claims, credential
        raise RequestContextError(failure_code)

    def _issue(
        self,
        claims: CallerRequestContext,
        credential: SecretMaterial,
    ) -> RequestContext:
        reserved = False
        try:
            expected = self._snapshot_claims(claims)
            with self.__lock:
                if self.__closed:
                    raise RequestContextError("request_context_issuer_closed")
                now = self._clock_now()
                self._prune(now)
                if len(self.__active) + self.__pending >= self.__max_active_contexts:
                    raise RequestContextError("request_context_capacity_exceeded")
                self.__pending += 1
                reserved = True
            adapter_claims = CallerRequestContext.from_dict(expected.to_dict())
            credential_view: Optional[memoryview] = None
            credential_unavailable = False
            try:
                credential_view = credential.view()
            except Exception:
                credential_unavailable = True
            if credential_unavailable or credential_view is None:
                raise RequestContextError("request_context_credential_unavailable")
            authentication_failed = False
            binding: Any = None
            try:
                binding = self.__authenticator.authenticate(
                    adapter_claims,
                    credential_view,
                    audience=self.__audience,
                    at=now,
                )
            except Exception:
                authentication_failed = True
            if authentication_failed:
                raise RequestContextError("request_authentication_failed")
            completed_at = self._clock_now()
            trusted = self._validate_binding(binding, expected, completed_at)
            return self._register(trusted, expected)
        finally:
            try:
                if reserved:
                    with self.__lock:
                        self.__pending -= 1
            finally:
                self._close_credential(credential)

    def prepare_reauthorization(
        self,
        context: RequestContext,
        request: AccessRequest,
    ) -> ReauthorizationBasis:
        """Validate exact local context/request scope and return non-authorizing evidence."""

        trusted_request = self._snapshot_access_request(request)
        with self.__lock:
            if self.__closed:
                raise RequestContextError("request_context_issuer_closed")
            now = self._clock_now()
            snapshot = self._trusted_snapshot(context)
            if snapshot.expires_at <= now:
                self.__active.pop(id(context), None)
                raise RequestContextError("request_context_expired")
            workspace = trusted_request.resource.workspace_id
            expected_scope = (
                snapshot.request_id,
                snapshot.subject_id,
                snapshot.tenant_value,
                snapshot.workspace_value,
            )
            actual_scope = (
                trusted_request.request_id,
                trusted_request.subject_id,
                str(trusted_request.tenant_id),
                str(workspace) if workspace is not None else None,
            )
            if (
                trusted_request.resource.tenant_id != trusted_request.tenant_id
                or actual_scope != expected_scope
            ):
                raise RequestContextError("request_context_scope_mismatch")
            return ReauthorizationBasis(
                context_id=snapshot.context_id,
                authenticator_id=snapshot.authenticator_id,
                audience=snapshot.audience,
                request_id=snapshot.request_id,
                principal_id=snapshot.principal_id,
                subject_id=snapshot.subject_id,
                tenant_id=TenantId(snapshot.tenant_value),
                workspace_id=(
                    WorkspaceId(snapshot.workspace_value)
                    if snapshot.workspace_value is not None
                    else None
                ),
                identity_revision=snapshot.identity_revision,
                scope_revision=snapshot.scope_revision,
                evidence_fingerprint=snapshot.evidence_fingerprint,
                authenticated_at=snapshot.authenticated_at,
                context_issued_at=snapshot.issued_at,
                context_expires_at=snapshot.expires_at,
                prepared_at=now,
            )

    def retire(self, context: RequestContext) -> None:
        """Invalidate one exact issued handle without disclosing registry membership."""

        with self.__lock:
            if self.__closed:
                raise RequestContextError("request_context_issuer_closed")
            self._trusted_snapshot(context)
            self.__active.pop(id(context), None)

    def close(self) -> None:
        """Invalidate every issued context. The operation is idempotent."""

        with self.__lock:
            self.__closed = True
            self.__active.clear()

    def __enter__(self) -> RequestContextIssuer:
        with self.__lock:
            if self.__closed:
                raise RequestContextError("request_context_issuer_closed")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "RequestContextIssuer(<configured>)"

    def __copy__(self) -> NoReturn:
        raise TypeError("RequestContextIssuer cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("RequestContextIssuer cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("RequestContextIssuer cannot be serialized")

    @staticmethod
    def _validate_duration(
        value: timedelta,
        field_name: str,
        *,
        minimum: timedelta,
        maximum: timedelta,
    ) -> None:
        if not isinstance(value, timedelta):
            raise TypeError(f"{field_name} must be timedelta")
        if value < minimum or value > maximum:
            raise ValueError(f"{field_name} is outside the supported range")

    def _clock_now(self) -> datetime:
        with self.__lock:
            normalized: Optional[datetime] = None
            clock_failed = False
            try:
                value = self.__clock.now()
                normalized = _as_utc(value, "clock.now()")
            except Exception:
                clock_failed = True
            if clock_failed or normalized is None:
                raise RequestContextError("request_context_clock_unavailable")
            previous = self.__last_observed_at
            if previous is not None and normalized < previous:
                if previous - normalized > self.__max_clock_skew:
                    raise RequestContextError("request_context_time_regressed")
                return previous
            self.__last_observed_at = normalized
            return normalized

    @staticmethod
    def _close_credential(credential: SecretMaterial) -> None:
        close_failed = False
        try:
            credential.close()
        except Exception:
            close_failed = True
        if close_failed:
            raise RequestContextError("request_context_credential_close_failed")

    @staticmethod
    def _snapshot_claims(claims: CallerRequestContext) -> CallerRequestContext:
        if type(claims) is not CallerRequestContext:
            raise TypeError("claims must be an exact CallerRequestContext")
        snapshot: Optional[CallerRequestContext] = None
        invalid = False
        try:
            snapshot = CallerRequestContext.from_dict(claims.to_dict())
        except Exception:
            invalid = True
        if invalid or snapshot is None:
            raise TypeError("claims must be a canonical CallerRequestContext")
        return snapshot

    @staticmethod
    def _snapshot_access_request(request: AccessRequest) -> AccessRequest:
        if type(request) is not AccessRequest:
            raise TypeError("request must be an exact AccessRequest")
        snapshot: Optional[AccessRequest] = None
        invalid = False
        try:
            snapshot = AccessRequest.from_dict(request.to_dict())
        except Exception:
            invalid = True
        if invalid or snapshot is None:
            raise TypeError("request must be a canonical AccessRequest")
        return snapshot

    def _validate_binding(
        self,
        binding: Any,
        expected: CallerRequestContext,
        now: datetime,
    ) -> AuthenticatedRequestBinding:
        if type(binding) is not AuthenticatedRequestBinding:
            raise RequestContextError("request_authentication_result_invalid")
        trusted: Optional[AuthenticatedRequestBinding] = None
        invalid = False
        try:
            trusted = AuthenticatedRequestBinding(
                authenticator_id=binding.authenticator_id,
                audience=binding.audience,
                request_id=binding.request_id,
                principal_id=binding.principal_id,
                subject_id=binding.subject_id,
                tenant_id=TenantId(str(binding.tenant_id)),
                workspace_id=(
                    WorkspaceId(str(binding.workspace_id))
                    if binding.workspace_id is not None
                    else None
                ),
                identity_revision=binding.identity_revision,
                scope_revision=binding.scope_revision,
                evidence_fingerprint=binding.evidence_fingerprint,
                authenticated_at=binding.authenticated_at,
                expires_at=binding.expires_at,
            )
        except Exception:
            invalid = True
        if invalid or trusted is None:
            raise RequestContextError("request_authentication_result_invalid")
        expected_scope = (
            self.__authenticator_id,
            self.__audience,
            expected.request_id,
            expected.subject_id,
            expected.tenant_id,
            expected.workspace_id,
        )
        actual_scope = (
            trusted.authenticator_id,
            trusted.audience,
            trusted.request_id,
            trusted.subject_id,
            trusted.tenant_id,
            trusted.workspace_id,
        )
        if actual_scope != expected_scope:
            raise RequestContextError("request_authentication_binding_mismatch")
        if (
            trusted.authenticated_at > now
            and trusted.authenticated_at - now > self.__max_clock_skew
        ):
            raise RequestContextError("request_authentication_time_invalid")
        if trusted.expires_at <= now:
            raise RequestContextError("request_authentication_expired")
        if trusted.expires_at - trusted.authenticated_at > self.__max_context_ttl:
            raise RequestContextError("request_authentication_ttl_exceeded")
        if trusted.expires_at - now > self.__max_context_ttl:
            raise RequestContextError("request_authentication_ttl_exceeded")
        return trusted

    def _register(
        self,
        binding: AuthenticatedRequestBinding,
        expected: CallerRequestContext,
    ) -> RequestContext:
        with self.__lock:
            if self.__closed:
                raise RequestContextError("request_context_issuer_closed")
            now = self._clock_now()
            self._prune(now)
            if len(self.__active) >= self.__max_active_contexts:
                raise RequestContextError("request_context_capacity_exceeded")
            trusted = self._validate_binding(binding, expected, now)
            context_id = self._new_context_id()
            snapshot = _ContextSnapshot(
                context_id=context_id,
                authenticator_id=trusted.authenticator_id,
                audience=trusted.audience,
                request_id=trusted.request_id,
                principal_id=trusted.principal_id,
                subject_id=trusted.subject_id,
                tenant_value=str(trusted.tenant_id),
                workspace_value=(
                    str(trusted.workspace_id) if trusted.workspace_id is not None else None
                ),
                identity_revision=trusted.identity_revision,
                scope_revision=trusted.scope_revision,
                evidence_fingerprint=trusted.evidence_fingerprint,
                authenticated_at=trusted.authenticated_at,
                issued_at=now,
                expires_at=trusted.expires_at,
            )
            context = RequestContext(snapshot, _CONTEXT_CONSTRUCTION_TOKEN)
            self.__active[id(context)] = (weakref.ref(context), snapshot)
            return context

    def _trusted_snapshot(self, context: RequestContext) -> _ContextSnapshot:
        if type(context) is not RequestContext:
            raise RequestContextError("request_context_untrusted")
        record = self.__active.get(id(context))
        if record is None or record[0]() is not context:
            raise RequestContextError("request_context_untrusted")
        snapshot = record[1]
        if not self._context_matches(context, snapshot):
            self.__active.pop(id(context), None)
            raise RequestContextError("request_context_tampered")
        return snapshot

    @staticmethod
    def _context_matches(context: RequestContext, snapshot: _ContextSnapshot) -> bool:
        try:
            actual = (
                context.context_id,
                context.authenticator_id,
                context.audience,
                context.request_id,
                context.principal_id,
                context.subject_id,
                str(context.tenant_id),
                str(context.workspace_id) if context.workspace_id is not None else None,
                context.identity_revision,
                context.scope_revision,
                context.evidence_fingerprint,
                context.authenticated_at,
                context.issued_at,
                context.expires_at,
            )
        except Exception:
            return False
        expected = (
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
            snapshot.evidence_fingerprint,
            snapshot.authenticated_at,
            snapshot.issued_at,
            snapshot.expires_at,
        )
        return actual == expected

    def _new_context_id(self) -> str:
        existing = {record[1].context_id for record in self.__active.values()}
        try:
            for _ in range(4):
                candidate = f"ctx_{secrets.token_hex(32)}"
                if candidate not in existing:
                    return candidate
        except Exception:
            pass
        raise RequestContextError("request_context_id_unavailable")

    def _prune(self, now: datetime) -> None:
        stale = [
            identifier
            for identifier, (reference, snapshot) in self.__active.items()
            if reference() is None or snapshot.expires_at <= now
        ]
        for identifier in stale:
            del self.__active[identifier]


__all__ = [
    "AuthenticatedRequestBinding",
    "CallerRequestContext",
    "RequestAuthenticator",
    "RequestContext",
    "RequestContextError",
    "RequestContextIssuer",
    "ReauthorizationBasis",
]
