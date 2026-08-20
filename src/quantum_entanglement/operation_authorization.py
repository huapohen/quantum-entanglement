# ruff: noqa: UP007, UP045 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Protected-operation authorization composition boundaries.

Values returned by ``CurrentAuthorizationStateProvider`` are trusted adapter
inputs, not authorization grants.  Only the operation composer introduced by
this module may turn a fresh, matching state into an opaque operation handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from .request_context import ReauthorizationBasis
from .tenancy import (
    AccessRequest,
    Member,
    RevocationSnapshot,
    TenantId,
    VerifiedCapability,
    WorkspaceId,
)

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
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


__all__ = [
    "CurrentAuthorizationState",
    "CurrentAuthorizationStateProvider",
]
