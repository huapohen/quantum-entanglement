# ruff: noqa: UP007, UP045 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Process-local issuance boundary for authenticated request scope.

CallerRequestContext is canonical but untrusted. AuthenticatedRequestBinding is trusted
only when it is the immediate return from the authenticator configured on a
RequestContextIssuer; constructing either value directly grants no authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from .tenancy import TenantId, WorkspaceId

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


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


__all__ = [
    "AuthenticatedRequestBinding",
    "CallerRequestContext",
    "RequestAuthenticator",
]
