# ruff: noqa: UP007, UP045 -- PEP 604 syntax is not parseable on supported Python 3.9.
"""Tenant isolation, scoped RBAC, and verified attenuated capabilities.

Security boundaries in this module are intentionally explicit:

* access time comes from a service-owned :class:`ServerClock`, never a request;
* decoded :class:`CapabilityClaims` are untrusted and cannot authorize access;
* only a sealed :class:`VerifiedCapability` returned by
  :class:`CapabilityVerifier` can reach the authorizer;
* authorization revalidates the complete delegation chain and checks every
  ancestor against a typed, tenant-specific revocation snapshot;
* all parsers reject coercion and unknown fields.

Cryptographic proof verification is supplied by the service adapter through
``CapabilityProofVerifier``. This keeps key management out of the domain model
while preventing decoded claims from being mistaken for a trusted grant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol, Union

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTION = re.compile(r"^(?:\*|[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*(?:\.\*)?)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32,128}$")
_KID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALGORITHM = re.compile(r"^[A-Z][A-Z0-9_-]{1,31}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]{16,2048}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")


def _require_opaque_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ValueError(
            f"{field_name} must be 1-128 characters using letters, numbers, '.', '_', ':', or '-'"
        )
    return value


def _require_text(value: str, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and have no surrounding whitespace")
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return value


def _require_action(value: str, field_name: str = "action") -> str:
    if not isinstance(value, str) or not _ACTION.fullmatch(value):
        raise ValueError(
            f"{field_name} must be '*', a lower-case dotted action, or a trailing wildcard"
        )
    return value


def _require_requested_action(value: str) -> str:
    _require_action(value, "requested action")
    if value == "*" or value.endswith(".*"):
        raise ValueError("an access request cannot contain an action wildcard")
    return value


def _require_duration(value: timedelta, field_name: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be timedelta")
    if value <= timedelta(0):
        raise ValueError(f"{field_name} must be positive")
    return value


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_time(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be an RFC 3339 string")
    if not _RFC3339.fullmatch(value) or value.endswith("-00:00"):
        raise ValueError(f"{field_name} must be a strict RFC 3339 timestamp")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid RFC 3339 timestamp") from error
    return _as_utc(parsed, field_name)


def _strict_object(
    value: Any,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    required_keys = frozenset(required)
    allowed_keys = required_keys | frozenset(optional)
    actual_keys = frozenset(value)
    missing = sorted(required_keys - actual_keys)
    unknown = sorted(actual_keys - allowed_keys)
    if missing:
        raise ValueError(f"{field_name} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{field_name} contains unknown fields: {', '.join(unknown)}")
    return value


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return int(value)


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be an array")
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _encode_signature(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_signature(value: str) -> bytes:
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        raise ValueError("signature must be unpadded canonical base64url")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as error:
        raise ValueError("signature is not valid base64url") from error
    if _encode_signature(decoded) != value:
        raise ValueError("signature must use canonical base64url encoding")
    return decoded


def _proof_message(kid: str, algorithm: str, payload: bytes) -> bytes:
    """Bind JOSE-like proof headers and canonical payload into one MAC input."""

    return _canonical_bytes(
        {
            "kid": kid,
            "alg": algorithm,
            "payloadSha256": hashlib.sha256(payload).hexdigest(),
        }
    )


def _strict_tuple(value: Iterable[Any], field_name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{field_name} must be an iterable of typed values")
    try:
        return tuple(value)
    except TypeError as error:
        raise TypeError(f"{field_name} must be iterable") from error


def action_covers(granted: str, requested: str) -> bool:
    """Return whether an exact or trailing-wildcard grant covers an action."""

    _require_action(granted, "granted action")
    _require_requested_action(requested)
    if granted == "*" or granted == requested:
        return True
    if granted.endswith(".*"):
        return requested.startswith(granted[:-1])
    return False


def action_is_subset(parent: str, child: str) -> bool:
    """Return whether every action represented by ``child`` is in ``parent``."""

    _require_action(parent, "parent action")
    _require_action(child, "child action")
    if parent == "*":
        return True
    if child == "*":
        return False
    if parent == child:
        return True
    if not parent.endswith(".*"):
        return False
    parent_prefix = parent[:-2]
    if child.endswith(".*"):
        child_prefix = child[:-2]
        return child_prefix == parent_prefix or child_prefix.startswith(parent_prefix + ".")
    return child.startswith(parent_prefix + ".")


class ServerClock(Protocol):
    """Service-owned source of authorization time."""

    def now(self) -> datetime: ...


class SystemClock:
    """Production UTC wall clock implementation."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _clock_now(clock: ServerClock) -> datetime:
    try:
        value = clock.now()
    except AttributeError as error:
        raise TypeError("clock must implement now()") from error
    return _as_utc(value, "clock.now()")


@dataclass(frozen=True, order=True)
class TenantId:
    value: str

    def __post_init__(self) -> None:
        _require_opaque_id(self.value, "tenant_id")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class WorkspaceId:
    value: str

    def __post_init__(self) -> None:
        _require_opaque_id(self.value, "workspace_id")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class CapabilityNonce:
    """Canonical hex nonce with at least 128 bits of entropy capacity."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _NONCE.fullmatch(self.value):
            raise ValueError("nonce must be 16-64 bytes encoded as lower-case hexadecimal")
        if len(self.value) % 2:
            raise ValueError("nonce hexadecimal encoding must contain complete bytes")

    @classmethod
    def generate(cls, entropy_bytes: int = 32) -> CapabilityNonce:
        """Generate a CSPRNG nonce through the standard-library secrets API."""

        if isinstance(entropy_bytes, bool) or not isinstance(entropy_bytes, int):
            raise TypeError("entropy_bytes must be an integer")
        if not 16 <= entropy_bytes <= 64:
            raise ValueError("entropy_bytes must be between 16 and 64")
        return cls(secrets.token_hex(entropy_bytes))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ResourceRef:
    """Concrete resource targeted by an access request."""

    tenant_id: TenantId
    resource_type: str
    resource_id: str
    workspace_id: Optional[WorkspaceId] = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        if self.workspace_id is not None and not isinstance(self.workspace_id, WorkspaceId):
            raise TypeError("workspace_id must be WorkspaceId or None")
        _require_opaque_id(self.resource_type, "resource_type")
        _require_text(self.resource_id, "resource_id")
        if self.resource_id == "*":
            raise ValueError("a requested resource_id must be concrete")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": str(self.tenant_id),
            "workspaceId": str(self.workspace_id) if self.workspace_id else None,
            "resourceType": self.resource_type,
            "resourceId": self.resource_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResourceRef:
        data = _strict_object(
            value,
            required=("tenantId", "workspaceId", "resourceType", "resourceId"),
            field_name="resource",
        )
        raw_workspace = data["workspaceId"]
        if raw_workspace is not None and not isinstance(raw_workspace, str):
            raise TypeError("resource.workspaceId must be a string or null")
        return cls(
            tenant_id=TenantId(_string(data["tenantId"], "resource.tenantId")),
            workspace_id=WorkspaceId(raw_workspace) if raw_workspace is not None else None,
            resource_type=_string(data["resourceType"], "resource.resourceType"),
            resource_id=_string(data["resourceId"], "resource.resourceId"),
        )


@dataclass(frozen=True)
class ResourceScope:
    """Tenant, optional workspace, and resource selector.

    ``workspace_id=None`` is explicitly tenant-wide. Wildcards are permitted
    only in grants, never in a concrete ``ResourceRef``.
    """

    tenant_id: TenantId
    workspace_id: Optional[WorkspaceId] = None
    resource_type: str = "*"
    resource_id: str = "*"

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        if self.workspace_id is not None and not isinstance(self.workspace_id, WorkspaceId):
            raise TypeError("workspace_id must be WorkspaceId or None")
        if self.resource_type != "*":
            _require_opaque_id(self.resource_type, "resource_type")
        _require_text(self.resource_id, "resource_id")
        if self.resource_type == "*" and self.resource_id != "*":
            raise ValueError("a concrete resource_id requires a concrete resource_type")

    def contains(self, candidate: Union[ResourceScope, ResourceRef]) -> bool:
        if not isinstance(candidate, (ResourceScope, ResourceRef)):
            raise TypeError("candidate must be ResourceScope or ResourceRef")
        if self.tenant_id != candidate.tenant_id:
            return False
        if self.workspace_id is not None and self.workspace_id != candidate.workspace_id:
            return False
        if self.resource_type != "*" and self.resource_type != candidate.resource_type:
            return False
        if self.resource_id != "*" and self.resource_id != candidate.resource_id:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": str(self.tenant_id),
            "workspaceId": str(self.workspace_id) if self.workspace_id else None,
            "resourceType": self.resource_type,
            "resourceId": self.resource_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResourceScope:
        data = _strict_object(
            value,
            required=("tenantId", "workspaceId", "resourceType", "resourceId"),
            field_name="resource scope",
        )
        raw_workspace = data["workspaceId"]
        if raw_workspace is not None and not isinstance(raw_workspace, str):
            raise TypeError("resource scope.workspaceId must be a string or null")
        return cls(
            tenant_id=TenantId(_string(data["tenantId"], "resource scope.tenantId")),
            workspace_id=WorkspaceId(raw_workspace) if raw_workspace is not None else None,
            resource_type=_string(data["resourceType"], "resource scope.resourceType"),
            resource_id=_string(data["resourceId"], "resource scope.resourceId"),
        )


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"
    AGENT = "agent"


DEFAULT_ROLE_ACTIONS: Mapping[Role, tuple[str, ...]] = {
    Role.OWNER: ("*",),
    Role.ADMIN: (
        "approval.*",
        "artifact.*",
        "capability.delegate",
        "member.*",
        "resource.*",
        "workflow.*",
    ),
    Role.EDITOR: (
        "artifact.*",
        "resource.create",
        "resource.read",
        "resource.update",
        "workflow.read",
        "workflow.run",
    ),
    Role.VIEWER: ("artifact.read", "resource.read", "workflow.read"),
    Role.AGENT: (
        "artifact.create",
        "artifact.read",
        "artifact.update",
        "resource.read",
        "tool.execute",
        "workflow.read",
        "workflow.run",
    ),
}


@dataclass(frozen=True)
class RoleBinding:
    role: Role
    scope: ResourceScope

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            raise TypeError("role must be Role")
        if not isinstance(self.scope, ResourceScope):
            raise TypeError("scope must be ResourceScope")

    @property
    def audit_ref(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role.value, "scope": self.scope.to_dict()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RoleBinding:
        data = _strict_object(
            value,
            required=("role", "scope"),
            field_name="role binding",
        )
        raw_scope = data["scope"]
        if not isinstance(raw_scope, dict):
            raise TypeError("role binding.scope must be an object")
        return cls(
            role=Role(_string(data["role"], "role binding.role")),
            scope=ResourceScope.from_dict(raw_scope),
        )


class MemberStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


@dataclass(frozen=True)
class Member:
    member_id: str
    tenant_id: TenantId
    role_bindings: tuple[RoleBinding, ...] = ()
    status: MemberStatus = MemberStatus.ACTIVE

    def __post_init__(self) -> None:
        _require_opaque_id(self.member_id, "member_id")
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        if not isinstance(self.status, MemberStatus):
            raise TypeError("status must be MemberStatus")
        bindings = _strict_tuple(self.role_bindings, "role_bindings")
        for binding in bindings:
            if not isinstance(binding, RoleBinding):
                raise TypeError("role_bindings must contain RoleBinding values")
            if binding.scope.tenant_id != self.tenant_id:
                raise ValueError("a role binding cannot cross the member tenant")
        object.__setattr__(self, "role_bindings", bindings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memberId": self.member_id,
            "tenantId": str(self.tenant_id),
            "status": self.status.value,
            "roleBindings": [binding.to_dict() for binding in self.role_bindings],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Member:
        data = _strict_object(
            value,
            required=("memberId", "tenantId", "status", "roleBindings"),
            field_name="member",
        )
        bindings = _list(data["roleBindings"], "member.roleBindings")
        return cls(
            member_id=_string(data["memberId"], "member.memberId"),
            tenant_id=TenantId(_string(data["tenantId"], "member.tenantId")),
            status=MemberStatus(_string(data["status"], "member.status")),
            role_bindings=tuple(RoleBinding.from_dict(item) for item in bindings),
        )


@dataclass(frozen=True, order=True)
class RevocationId:
    """Nonce identity in its tenant-and-issuer uniqueness domain."""

    tenant_id: TenantId
    issuer_id: str
    nonce: CapabilityNonce

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        _require_opaque_id(self.issuer_id, "issuer_id")
        if not isinstance(self.nonce, CapabilityNonce):
            raise TypeError("nonce must be CapabilityNonce")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": str(self.tenant_id),
            "issuerId": self.issuer_id,
            "nonce": str(self.nonce),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RevocationId:
        data = _strict_object(
            value,
            required=("tenantId", "issuerId", "nonce"),
            field_name="revocation id",
        )
        return cls(
            tenant_id=TenantId(_string(data["tenantId"], "revocation id.tenantId")),
            issuer_id=_string(data["issuerId"], "revocation id.issuerId"),
            nonce=CapabilityNonce(_string(data["nonce"], "revocation id.nonce")),
        )


@dataclass(frozen=True)
class RevocationSnapshot:
    """Versioned, tenant-specific revocation state loaded by the service."""

    tenant_id: TenantId
    revision: int
    captured_at: datetime
    revoked_ids: frozenset[RevocationId] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        revision = _integer(self.revision, "revision")
        if revision < 0:
            raise ValueError("revision cannot be negative")
        object.__setattr__(self, "captured_at", _as_utc(self.captured_at, "captured_at"))
        values = _strict_tuple(self.revoked_ids, "revoked_ids")
        normalized = frozenset(values)
        for revocation_id in normalized:
            if not isinstance(revocation_id, RevocationId):
                raise TypeError("revoked_ids must contain RevocationId values")
            if revocation_id.tenant_id != self.tenant_id:
                raise ValueError("a revocation snapshot cannot contain another tenant")
        object.__setattr__(self, "revoked_ids", normalized)

    @classmethod
    def empty(
        cls,
        tenant_id: TenantId,
        captured_at: datetime,
        revision: int = 0,
    ) -> RevocationSnapshot:
        return cls(tenant_id, revision, captured_at, frozenset())

    def contains(self, revocation_id: RevocationId) -> bool:
        if not isinstance(revocation_id, RevocationId):
            raise TypeError("revocation_id must be RevocationId")
        return revocation_id in self.revoked_ids

    @property
    def state_digest(self) -> str:
        """Digest the revision's canonical revocation state, excluding freshness time.

        ``captured_at`` may be refreshed while the underlying revision is
        unchanged.  A different revoked-id set at the same revision is a state
        conflict and must never be accepted as a harmless refresh.
        """

        return _canonical_digest(
            {
                "tenantId": str(self.tenant_id),
                "revision": self.revision,
                "revokedIds": [
                    item.to_dict()
                    for item in sorted(
                        self.revoked_ids,
                        key=lambda item: (
                            str(item.tenant_id),
                            item.issuer_id,
                            str(item.nonce),
                        ),
                    )
                ],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": str(self.tenant_id),
            "revision": self.revision,
            "capturedAt": _format_time(self.captured_at),
            "revokedIds": [
                item.to_dict()
                for item in sorted(
                    self.revoked_ids,
                    key=lambda item: (str(item.tenant_id), item.issuer_id, str(item.nonce)),
                )
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RevocationSnapshot:
        data = _strict_object(
            value,
            required=("tenantId", "revision", "capturedAt", "revokedIds"),
            field_name="revocation snapshot",
        )
        raw_ids = _list(data["revokedIds"], "revocation snapshot.revokedIds")
        parsed_ids = tuple(RevocationId.from_dict(item) for item in raw_ids)
        if len(frozenset(parsed_ids)) != len(parsed_ids):
            raise ValueError("revocation snapshot cannot contain duplicate revoked ids")
        return cls(
            tenant_id=TenantId(_string(data["tenantId"], "revocation snapshot.tenantId")),
            revision=_integer(data["revision"], "revocation snapshot.revision"),
            captured_at=_parse_time(data["capturedAt"], "revocation snapshot.capturedAt"),
            revoked_ids=frozenset(parsed_ids),
        )


class RevocationRevisionGuard(Protocol):
    """Atomic, monotonic tenant revision high-water boundary.

    Production implementations must persist the high-water mark across process
    restarts (for example with a database compare-and-set). The in-memory
    implementation below is intended for tests and single-process deployments.
    """

    def check_and_advance(
        self,
        tenant_id: TenantId,
        revision: int,
        state_digest: str,
    ) -> bool: ...


class InMemoryRevocationRevisionGuard:
    """Thread-safe reference guard that can be shared by authorizer instances."""

    def __init__(self) -> None:
        self._high_water: dict[TenantId, tuple[int, str]] = {}
        self._lock = threading.RLock()

    def check_and_advance(
        self,
        tenant_id: TenantId,
        revision: int,
        state_digest: str,
    ) -> bool:
        if not isinstance(tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        value = _integer(revision, "revision")
        if value < 0:
            raise ValueError("revision cannot be negative")
        if not isinstance(state_digest, str) or not _SHA256.fullmatch(state_digest):
            raise ValueError("state_digest must be a lower-case SHA-256 digest")
        with self._lock:
            current = self._high_water.get(tenant_id)
            if current is None:
                self._high_water[tenant_id] = (value, state_digest)
                return True
            current_revision, current_digest = current
            if value < current_revision:
                return False
            if value == current_revision:
                return hmac.compare_digest(state_digest, current_digest)
            self._high_water[tenant_id] = (value, state_digest)
            return True

    def high_water(self, tenant_id: TenantId) -> Optional[int]:
        if not isinstance(tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        with self._lock:
            current = self._high_water.get(tenant_id)
            return current[0] if current is not None else None

    def state_digest(self, tenant_id: TenantId) -> Optional[str]:
        if not isinstance(tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        with self._lock:
            current = self._high_water.get(tenant_id)
            return current[1] if current is not None else None


class RevocationGuardIntegrityError(RuntimeError):
    """Raised when durable revision state or its owned schema is malformed."""


_REVOCATION_GUARD_SCHEMA = """
CREATE TABLE qe_revocation_high_water (
    tenant_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    state_digest TEXT NOT NULL CHECK(
        length(state_digest) = 64
        AND state_digest NOT GLOB '*[^0-9a-f]*'
    )
)
""".strip()


def _normalized_ddl(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split()).casefold()


class SQLiteRevocationRevisionGuard:
    """Durable cross-process revision-and-state high-water CAS.

    The table schema is owned by this component and validated exactly on every
    open. A pre-created weak table, custom trigger, malformed row, lower
    revision, or different state at an equal revision fails closed.
    """

    def __init__(self, path: str, *, busy_timeout_ms: int = 5_000) -> None:
        if type(path) is not str or not path:
            raise ValueError("path must be a non-empty string")
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise TypeError("busy_timeout_ms must be an integer")
        if not 1 <= busy_timeout_ms <= 300_000:
            raise ValueError("busy_timeout_ms must be between 1 and 300000")
        self.path = path
        if path != ":memory:":
            absolute_path = os.path.abspath(path)
            os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
            try:
                descriptor = os.open(
                    absolute_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._initialize(busy_timeout_ms)
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> SQLiteRevocationRevisionGuard:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _initialize(self, busy_timeout_ms: int) -> None:
        with self._lock:
            self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._connection.execute("PRAGMA trusted_schema=OFF")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=FULL")
        with self._transaction() as connection:
            owned_object = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?",
                ("qe_revocation_high_water",),
            ).fetchone()
            if owned_object is None:
                connection.execute(_REVOCATION_GUARD_SCHEMA)
            self._validate_owned_schema(connection)
            rows = connection.execute(
                "SELECT tenant_id, revision, state_digest FROM qe_revocation_high_water"
            ).fetchall()
            for row in rows:
                self._validated_row(row)

    @staticmethod
    def _validate_owned_schema(connection: sqlite3.Connection) -> None:
        owned_object = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?",
            ("qe_revocation_high_water",),
        ).fetchone()
        if (
            owned_object is None
            or owned_object["type"] != "table"
            or not isinstance(owned_object["sql"], str)
            or _normalized_ddl(owned_object["sql"]) != _normalized_ddl(_REVOCATION_GUARD_SCHEMA)
        ):
            raise RevocationGuardIntegrityError(
                "revocation high-water schema does not match the owned definition"
            )
        unexpected = connection.execute(
            """
            SELECT type, name FROM sqlite_master
            WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL
            ORDER BY type, name
            """,
            ("qe_revocation_high_water",),
        ).fetchall()
        if unexpected:
            raise RevocationGuardIntegrityError(
                "revocation high-water table cannot carry custom indexes or triggers"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                if self._connection.in_transaction:
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
    def _validated_row(row: sqlite3.Row) -> tuple[int, str]:
        tenant_id = row["tenant_id"]
        revision = row["revision"]
        state_digest = row["state_digest"]
        try:
            TenantId(tenant_id)
        except (TypeError, ValueError) as error:
            raise RevocationGuardIntegrityError(
                "revocation high-water row contains an invalid tenant id"
            ) from error
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RevocationGuardIntegrityError(
                "revocation high-water row contains an invalid revision"
            )
        if not isinstance(state_digest, str) or not _SHA256.fullmatch(state_digest):
            raise RevocationGuardIntegrityError(
                "revocation high-water row contains an invalid state digest"
            )
        return revision, state_digest

    def check_and_advance(
        self,
        tenant_id: TenantId,
        revision: int,
        state_digest: str,
    ) -> bool:
        if type(tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        value = _integer(revision, "revision")
        if value < 0:
            raise ValueError("revision cannot be negative")
        if type(state_digest) is not str or not _SHA256.fullmatch(state_digest):
            raise ValueError("state_digest must be a lower-case SHA-256 digest")
        with self._transaction() as connection:
            self._validate_owned_schema(connection)
            row = connection.execute(
                """
                SELECT tenant_id, revision, state_digest
                FROM qe_revocation_high_water WHERE tenant_id = ?
                """,
                (str(tenant_id),),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO qe_revocation_high_water (
                        tenant_id, revision, state_digest
                    ) VALUES (?, ?, ?)
                    """,
                    (str(tenant_id), value, state_digest),
                )
                self._assert_persisted_state(
                    connection,
                    tenant_id,
                    value,
                    state_digest,
                )
                return True
            current_revision, current_digest = self._validated_row(row)
            if value < current_revision:
                return False
            if value == current_revision:
                return hmac.compare_digest(state_digest, current_digest)
            cursor = connection.execute(
                """
                UPDATE qe_revocation_high_water
                SET revision = ?, state_digest = ?
                WHERE tenant_id = ? AND revision = ? AND state_digest = ?
                """,
                (
                    value,
                    state_digest,
                    str(tenant_id),
                    current_revision,
                    current_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise RevocationGuardIntegrityError(
                    "revocation high-water compare-and-set lost its locked row"
                )
            self._assert_persisted_state(
                connection,
                tenant_id,
                value,
                state_digest,
            )
            return True

    def _assert_persisted_state(
        self,
        connection: sqlite3.Connection,
        tenant_id: TenantId,
        revision: int,
        state_digest: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT tenant_id, revision, state_digest
            FROM qe_revocation_high_water WHERE tenant_id = ?
            """,
            (str(tenant_id),),
        ).fetchone()
        if row is None or self._validated_row(row) != (revision, state_digest):
            raise RevocationGuardIntegrityError(
                "revocation high-water write failed its persisted postcondition"
            )

    def high_water(self, tenant_id: TenantId) -> Optional[int]:
        row = self._read_row(tenant_id)
        return row[0] if row is not None else None

    def state_digest(self, tenant_id: TenantId) -> Optional[str]:
        row = self._read_row(tenant_id)
        return row[1] if row is not None else None

    def _read_row(self, tenant_id: TenantId) -> Optional[tuple[int, str]]:
        if type(tenant_id) is not TenantId:
            raise TypeError("tenant_id must be an exact TenantId")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT tenant_id, revision, state_digest
                FROM qe_revocation_high_water WHERE tenant_id = ?
                """,
                (str(tenant_id),),
            ).fetchone()
            return self._validated_row(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()


@dataclass(frozen=True)
class CapabilityClaims:
    """Decoded but untrusted capability facts that cannot authorize access."""

    issuer_id: str
    subject_id: str
    action: str
    resource: ResourceScope
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    audience: str
    nonce: CapabilityNonce
    parent_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        _require_opaque_id(self.issuer_id, "issuer_id")
        _require_opaque_id(self.subject_id, "subject_id")
        _require_action(self.action)
        if not isinstance(self.resource, ResourceScope):
            raise TypeError("resource must be ResourceScope")
        issued_at = _as_utc(self.issued_at, "issued_at")
        not_before = _as_utc(self.not_before, "not_before")
        expires_at = _as_utc(self.expires_at, "expires_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "not_before", not_before)
        object.__setattr__(self, "expires_at", expires_at)
        if not_before < issued_at:
            raise ValueError("not_before cannot precede issued_at")
        if expires_at <= not_before:
            raise ValueError("expires_at must be later than not_before")
        _require_text(self.audience, "audience", maximum=256)
        if not isinstance(self.nonce, CapabilityNonce):
            raise TypeError("nonce must be CapabilityNonce")
        if self.parent_fingerprint is not None:
            if not isinstance(self.parent_fingerprint, str) or not _SHA256.fullmatch(
                self.parent_fingerprint
            ):
                raise ValueError("parent_fingerprint must be a lower-case SHA-256 digest")

    @property
    def tenant_id(self) -> TenantId:
        return self.resource.tenant_id

    @property
    def revocation_id(self) -> RevocationId:
        return RevocationId(self.tenant_id, self.issuer_id, self.nonce)

    @property
    def fingerprint(self) -> str:
        return _canonical_digest(self._claim_payload())

    def _claim_payload(self) -> dict[str, Any]:
        return {
            "issuerId": self.issuer_id,
            "subjectId": self.subject_id,
            "action": self.action,
            "resource": self.resource.to_dict(),
            "issuedAt": _format_time(self.issued_at),
            "notBefore": _format_time(self.not_before),
            "expiresAt": _format_time(self.expires_at),
            "audience": self.audience,
            "nonce": str(self.nonce),
            "parentFingerprint": self.parent_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self._claim_payload()
        value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CapabilityClaims:
        data = _strict_object(
            value,
            required=(
                "issuerId",
                "subjectId",
                "action",
                "resource",
                "issuedAt",
                "notBefore",
                "expiresAt",
                "audience",
                "nonce",
                "parentFingerprint",
                "fingerprint",
            ),
            field_name="capability claims",
        )
        raw_resource = data["resource"]
        if not isinstance(raw_resource, dict):
            raise TypeError("capability claims.resource must be an object")
        parent = data["parentFingerprint"]
        if parent is not None and not isinstance(parent, str):
            raise TypeError("capability claims.parentFingerprint must be a string or null")
        claims = cls(
            issuer_id=_string(data["issuerId"], "capability claims.issuerId"),
            subject_id=_string(data["subjectId"], "capability claims.subjectId"),
            action=_string(data["action"], "capability claims.action"),
            resource=ResourceScope.from_dict(raw_resource),
            issued_at=_parse_time(data["issuedAt"], "capability claims.issuedAt"),
            not_before=_parse_time(data["notBefore"], "capability claims.notBefore"),
            expires_at=_parse_time(data["expiresAt"], "capability claims.expiresAt"),
            audience=_string(data["audience"], "capability claims.audience"),
            nonce=CapabilityNonce(_string(data["nonce"], "capability claims.nonce")),
            parent_fingerprint=parent,
        )
        fingerprint = _string(data["fingerprint"], "capability claims.fingerprint")
        if not _SHA256.fullmatch(fingerprint) or fingerprint != claims.fingerprint:
            raise ValueError("capability claims fingerprint does not match its contents")
        return claims

    def delegate(
        self,
        *,
        subject_id: str,
        action: str,
        resource: ResourceScope,
        issued_at: datetime,
        not_before: datetime,
        expires_at: datetime,
        nonce: CapabilityNonce,
    ) -> CapabilityClaims:
        child = CapabilityClaims(
            issuer_id=self.subject_id,
            subject_id=subject_id,
            action=action,
            resource=resource,
            issued_at=issued_at,
            not_before=not_before,
            expires_at=expires_at,
            audience=self.audience,
            nonce=nonce,
            parent_fingerprint=self.fingerprint,
        )
        validate_delegation(self, child)
        return child


class DelegationError(ValueError):
    """Raised when a child capability would amplify its parent."""


def validate_delegation(parent: CapabilityClaims, child: CapabilityClaims) -> None:
    """Validate every attenuation invariant for one delegation edge."""

    if not isinstance(parent, CapabilityClaims) or not isinstance(child, CapabilityClaims):
        raise TypeError("parent and child must be CapabilityClaims")
    if child.parent_fingerprint != parent.fingerprint:
        raise DelegationError("child does not reference the supplied parent")
    if child.issuer_id != parent.subject_id:
        raise DelegationError("only the parent subject may delegate")
    if child.audience != parent.audience:
        raise DelegationError("delegation cannot change audience")
    if not action_is_subset(parent.action, child.action):
        raise DelegationError("delegation cannot add actions")
    if not parent.resource.contains(child.resource):
        raise DelegationError("delegation cannot widen or cross resource scope")
    if child.issued_at < parent.issued_at:
        raise DelegationError("delegation cannot be issued before its parent")
    if child.not_before < parent.not_before:
        raise DelegationError("delegation cannot activate before its parent")
    if child.expires_at > parent.expires_at:
        raise DelegationError("delegation cannot outlive its parent")
    if child.revocation_id == parent.revocation_id:
        raise DelegationError("delegation must have a unique nonce in its issuer/tenant domain")


CAPABILITY_PROTOCOL_VERSION = "qe-capability/1"


@dataclass(frozen=True)
class CapabilityProof:
    """Detached proof metadata and signature for a root or delegation edge."""

    kid: str
    algorithm: str
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.kid, str) or not _KID.fullmatch(self.kid):
            raise ValueError("kid must be a canonical opaque key identifier")
        if not isinstance(self.algorithm, str) or not _ALGORITHM.fullmatch(self.algorithm):
            raise ValueError("algorithm must be a canonical upper-case identifier")
        _decode_signature(self.signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kid": self.kid,
            "alg": self.algorithm,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CapabilityProof:
        data = _strict_object(
            value,
            required=("kid", "alg", "signature"),
            field_name="capability proof",
        )
        return cls(
            kid=_string(data["kid"], "capability proof.kid"),
            algorithm=_string(data["alg"], "capability proof.alg"),
            signature=_string(data["signature"], "capability proof.signature"),
        )


class CapabilityProofVerifier(Protocol):
    """Trusted key resolver/verifier owned by the service composition root."""

    @property
    def trust_domain(self) -> str: ...

    @property
    def policy_version(self) -> str: ...

    def verify(
        self,
        proof: CapabilityProof,
        signer_id: str,
        payload: bytes,
        at: datetime,
    ) -> bool: ...

    def authorize_root(
        self,
        proof: CapabilityProof,
        claims: CapabilityClaims,
        at: datetime,
    ) -> bool: ...

    def authorize_delegation(
        self,
        proof: CapabilityProof,
        parent: CapabilityClaims,
        child: CapabilityClaims,
        at: datetime,
    ) -> bool: ...


class CapabilityProofSigner(CapabilityProofVerifier, Protocol):
    def sign(
        self,
        kid: str,
        signer_id: str,
        payload: bytes,
        at: datetime,
    ) -> CapabilityProof: ...


class KeyStatus(str, Enum):
    ACTIVE = "active"
    VERIFY_ONLY = "verify_only"
    REVOKED = "revoked"


class KeyUsage(str, Enum):
    ROOT = "root"
    DELEGATION = "delegation"


@dataclass(frozen=True)
class CapabilitySigningKey:
    """In-memory HS256 key record; production adapters may resolve keys from KMS."""

    kid: str
    principal_id: str
    secret: bytes = field(repr=False, compare=False)
    not_before: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=90)
    )
    status: KeyStatus = KeyStatus.ACTIVE
    usages: frozenset[KeyUsage] = field(default_factory=lambda: frozenset((KeyUsage.DELEGATION,)))
    root_tenants: frozenset[TenantId] = field(default_factory=frozenset)
    algorithm: str = "HS256"

    def __post_init__(self) -> None:
        if not isinstance(self.kid, str) or not _KID.fullmatch(self.kid):
            raise ValueError("kid must be a canonical opaque key identifier")
        _require_opaque_id(self.principal_id, "principal_id")
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise ValueError("HS256 key material must contain at least 32 bytes")
        object.__setattr__(self, "secret", bytes(self.secret))
        not_before = _as_utc(self.not_before, "key.not_before")
        expires_at = _as_utc(self.expires_at, "key.expires_at")
        object.__setattr__(self, "not_before", not_before)
        object.__setattr__(self, "expires_at", expires_at)
        if expires_at <= not_before:
            raise ValueError("key expires_at must follow not_before")
        if not isinstance(self.status, KeyStatus):
            raise TypeError("key status must be KeyStatus")
        usages = frozenset(_strict_tuple(self.usages, "key.usages"))
        if not usages or any(type(usage) is not KeyUsage for usage in usages):
            raise TypeError("key.usages must contain KeyUsage values")
        root_tenants = frozenset(_strict_tuple(self.root_tenants, "key.root_tenants"))
        if any(type(tenant_id) is not TenantId for tenant_id in root_tenants):
            raise TypeError("key.root_tenants must contain TenantId values")
        if KeyUsage.ROOT in usages and not root_tenants:
            raise ValueError("a root signing key requires at least one tenant authority")
        if KeyUsage.ROOT not in usages and root_tenants:
            raise ValueError("a delegation-only key cannot carry root tenant authority")
        object.__setattr__(self, "usages", usages)
        object.__setattr__(self, "root_tenants", root_tenants)
        if self.algorithm != "HS256":
            raise ValueError("the built-in key ring supports only HS256")


class RotatingHMACKeyRing:
    """Thread-safe proof adapter with monotonic in-process key lifecycle state.

    A ``kid`` has immutable principal, key material, validity, and algorithm.
    Status can move only ``active -> verify_only -> revoked``. Removing a key
    permanently tombstones that ``kid`` for this ring instance so a stale
    configuration cannot silently resurrect it.

    Production composition must persist the same registry and tombstones across
    restarts; this standard-library adapter deliberately does not claim durable
    KMS or configuration-registry semantics.
    """

    _STATUS_ORDER: Mapping[KeyStatus, int] = {
        KeyStatus.ACTIVE: 0,
        KeyStatus.VERIFY_ONLY: 1,
        KeyStatus.REVOKED: 2,
    }

    def __init__(
        self,
        *,
        trust_domain: str,
        policy_version: str,
        keys: Iterable[CapabilitySigningKey],
    ) -> None:
        self._trust_domain = _require_text(trust_domain, "trust_domain", maximum=256)
        self._policy_version = _require_text(policy_version, "policy_version", maximum=128)
        self._lock = threading.RLock()
        self._keys: dict[str, CapabilitySigningKey] = {}
        self._key_identities: dict[str, str] = {}
        self._status_high_water: dict[str, KeyStatus] = {}
        self._retired_kids: set[str] = set()
        self.replace_keys(keys)

    @property
    def trust_domain(self) -> str:
        return self._trust_domain

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def replace_keys(self, keys: Iterable[CapabilitySigningKey]) -> None:
        """Atomically install a rotation set without permitting key rollback."""

        values = _strict_tuple(keys, "capability signing keys")
        normalized: dict[str, CapabilitySigningKey] = {}
        for key in values:
            if type(key) is not CapabilitySigningKey:
                raise TypeError("keys must contain CapabilitySigningKey values")
            snapshot = CapabilitySigningKey(
                kid=key.kid,
                principal_id=key.principal_id,
                secret=key.secret,
                not_before=key.not_before,
                expires_at=key.expires_at,
                status=key.status,
                usages=key.usages,
                root_tenants=key.root_tenants,
                algorithm=key.algorithm,
            )
            if snapshot.kid in normalized:
                raise ValueError("duplicate capability signing kid")
            normalized[snapshot.kid] = snapshot
        with self._lock:
            identities = dict(self._key_identities)
            status_high_water = dict(self._status_high_water)
            retired_kids = set(self._retired_kids)
            retired_kids.update(set(self._keys) - set(normalized))
            for kid, key in normalized.items():
                if kid in retired_kids:
                    raise ValueError("a removed capability signing kid cannot be reused")
                identity = self._key_identity(key)
                known_identity = identities.get(kid)
                if known_identity is not None and not hmac.compare_digest(known_identity, identity):
                    raise ValueError("capability signing kid identity is immutable")
                previous_status = status_high_water.get(kid)
                if (
                    previous_status is not None
                    and self._STATUS_ORDER[key.status] < self._STATUS_ORDER[previous_status]
                ):
                    raise ValueError("capability signing key status cannot move backwards")
                identities[kid] = identity
                status_high_water[kid] = key.status

            self._keys = normalized
            self._key_identities = identities
            self._status_high_water = status_high_water
            self._retired_kids = retired_kids

    @staticmethod
    def _key_identity(key: CapabilitySigningKey) -> str:
        return _canonical_digest(
            {
                "kid": key.kid,
                "principalId": key.principal_id,
                "secretSha256": hashlib.sha256(key.secret).hexdigest(),
                "notBefore": _format_time(key.not_before),
                "expiresAt": _format_time(key.expires_at),
                "usages": sorted(usage.value for usage in key.usages),
                "rootTenants": sorted(str(tenant_id) for tenant_id in key.root_tenants),
                "algorithm": key.algorithm,
            }
        )

    @staticmethod
    def _payload_usage_allowed(key: CapabilitySigningKey, payload: bytes) -> bool:
        try:
            value = json.loads(payload.decode("utf-8"))
            data = _strict_object(
                value,
                required=(
                    "protocolVersion",
                    "trustDomain",
                    "policyVersion",
                    "proofType",
                    "claims",
                ),
                optional=("parentFingerprint",),
                field_name="proof payload",
            )
            proof_type = _string(data["proofType"], "proof payload.proofType")
            claims = data["claims"]
            if not isinstance(claims, dict):
                return False
            resource = claims.get("resource")
            if not isinstance(resource, dict):
                return False
            raw_tenant_id = resource.get("tenantId")
            if not isinstance(raw_tenant_id, str):
                return False
            tenant_id = TenantId(raw_tenant_id)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if proof_type == "root":
            return KeyUsage.ROOT in key.usages and tenant_id in key.root_tenants
        if proof_type == "delegation":
            return KeyUsage.DELEGATION in key.usages
        return False

    def sign(
        self,
        kid: str,
        signer_id: str,
        payload: bytes,
        at: datetime,
    ) -> CapabilityProof:
        now = _as_utc(at, "proof signing time")
        if not isinstance(payload, bytes):
            raise TypeError("proof payload must be bytes")
        with self._lock:
            key = self._keys.get(kid)
            if key is None:
                raise CapabilityVerificationError("signing kid is not trusted")
            if key.status is not KeyStatus.ACTIVE:
                raise CapabilityVerificationError("signing key is not active")
            if key.principal_id != signer_id:
                raise CapabilityVerificationError("signing key principal does not match issuer")
            if not key.not_before <= now < key.expires_at:
                raise CapabilityVerificationError("signing key is outside its validity interval")
            if not self._payload_usage_allowed(key, payload):
                raise CapabilityVerificationError(
                    "signing key is not authorized for this proof usage"
                )
            signature = hmac.new(
                key.secret,
                _proof_message(key.kid, key.algorithm, payload),
                hashlib.sha256,
            ).digest()
            return CapabilityProof(key.kid, key.algorithm, _encode_signature(signature))

    def verify(
        self,
        proof: CapabilityProof,
        signer_id: str,
        payload: bytes,
        at: datetime,
    ) -> bool:
        if not isinstance(proof, CapabilityProof) or not isinstance(payload, bytes):
            return False
        try:
            now = _as_utc(at, "proof verification time")
            supplied = _decode_signature(proof.signature)
        except (TypeError, ValueError):
            return False
        with self._lock:
            key = self._keys.get(proof.kid)
            if key is None:
                return False
            if key.status not in (KeyStatus.ACTIVE, KeyStatus.VERIFY_ONLY):
                return False
            if key.algorithm != proof.algorithm or key.principal_id != signer_id:
                return False
            if not key.not_before <= now < key.expires_at:
                return False
            if not self._payload_usage_allowed(key, payload):
                return False
            expected = hmac.new(
                key.secret,
                _proof_message(proof.kid, proof.algorithm, payload),
                hashlib.sha256,
            ).digest()
            return hmac.compare_digest(expected, supplied)

    def authorize_root(
        self,
        proof: CapabilityProof,
        claims: CapabilityClaims,
        at: datetime,
    ) -> bool:
        if type(proof) is not CapabilityProof or type(claims) is not CapabilityClaims:
            return False
        try:
            now = _as_utc(at, "root authorization time")
        except (TypeError, ValueError):
            return False
        with self._lock:
            key = self._keys.get(proof.kid)
            return bool(
                key is not None
                and key.status in (KeyStatus.ACTIVE, KeyStatus.VERIFY_ONLY)
                and key.principal_id == claims.issuer_id
                and key.algorithm == proof.algorithm
                and key.not_before <= now < key.expires_at
                and KeyUsage.ROOT in key.usages
                and claims.tenant_id in key.root_tenants
            )

    def authorize_delegation(
        self,
        proof: CapabilityProof,
        parent: CapabilityClaims,
        child: CapabilityClaims,
        at: datetime,
    ) -> bool:
        if (
            type(proof) is not CapabilityProof
            or type(parent) is not CapabilityClaims
            or type(child) is not CapabilityClaims
        ):
            return False
        try:
            now = _as_utc(at, "delegation authorization time")
        except (TypeError, ValueError):
            return False
        with self._lock:
            key = self._keys.get(proof.kid)
            return bool(
                key is not None
                and key.status in (KeyStatus.ACTIVE, KeyStatus.VERIFY_ONLY)
                and key.principal_id == child.issuer_id
                and key.algorithm == proof.algorithm
                and key.not_before <= now < key.expires_at
                and KeyUsage.DELEGATION in key.usages
                and parent.tenant_id == child.tenant_id
            )


@dataclass(frozen=True)
class CapabilityEnvelope:
    """Canonical claims plus root signature and one proof per delegation edge."""

    protocol_version: str
    trust_domain: str
    policy_version: str
    claims: tuple[CapabilityClaims, ...]
    root_proof: CapabilityProof
    delegation_proofs: tuple[CapabilityProof, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.protocol_version, "protocol_version", maximum=64)
        _require_text(self.trust_domain, "trust_domain", maximum=256)
        _require_text(self.policy_version, "policy_version", maximum=128)
        claims = _strict_tuple(self.claims, "capability envelope claims")
        if not claims or any(not isinstance(item, CapabilityClaims) for item in claims):
            raise TypeError("capability envelope claims must contain CapabilityClaims")
        if not isinstance(self.root_proof, CapabilityProof):
            raise TypeError("root_proof must be CapabilityProof")
        proofs = _strict_tuple(self.delegation_proofs, "delegation proofs")
        if any(not isinstance(item, CapabilityProof) for item in proofs):
            raise TypeError("delegation_proofs must contain CapabilityProof values")
        if len(proofs) != len(claims) - 1:
            raise ValueError("capability envelope requires exactly one proof per delegation edge")
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "delegation_proofs", proofs)

    @property
    def envelope_id(self) -> str:
        return _canonical_digest(self.to_dict())

    @staticmethod
    def root_payload(
        protocol_version: str,
        trust_domain: str,
        policy_version: str,
        claims: CapabilityClaims,
    ) -> bytes:
        return _canonical_bytes(
            {
                "protocolVersion": protocol_version,
                "trustDomain": trust_domain,
                "policyVersion": policy_version,
                "proofType": "root",
                "claims": claims.to_dict(),
            }
        )

    @staticmethod
    def delegation_payload(
        protocol_version: str,
        trust_domain: str,
        policy_version: str,
        parent: CapabilityClaims,
        child: CapabilityClaims,
    ) -> bytes:
        return _canonical_bytes(
            {
                "protocolVersion": protocol_version,
                "trustDomain": trust_domain,
                "policyVersion": policy_version,
                "proofType": "delegation",
                "parentFingerprint": parent.fingerprint,
                "claims": child.to_dict(),
            }
        )

    @classmethod
    def signed(
        cls,
        claims: Iterable[CapabilityClaims],
        *,
        signer: CapabilityProofSigner,
        root_kid: str,
        delegation_kids: Iterable[str] = (),
        clock: Optional[ServerClock] = None,
        protocol_version: str = CAPABILITY_PROTOCOL_VERSION,
    ) -> CapabilityEnvelope:
        values = _strict_tuple(claims, "capability claims")
        if not values or any(not isinstance(item, CapabilityClaims) for item in values):
            raise TypeError("claims must contain CapabilityClaims")
        kids = _strict_tuple(delegation_kids, "delegation_kids")
        if any(not isinstance(kid, str) for kid in kids):
            raise TypeError("delegation_kids must contain strings")
        if len(kids) != len(values) - 1:
            raise ValueError("delegation_kids must match delegation edge count")
        trust_domain = _require_text(signer.trust_domain, "signer.trust_domain")
        policy_version = _require_text(signer.policy_version, "signer.policy_version")
        now = _clock_now(clock if clock is not None else SystemClock())
        root_proof = signer.sign(
            root_kid,
            values[0].issuer_id,
            cls.root_payload(
                protocol_version,
                trust_domain,
                policy_version,
                values[0],
            ),
            now,
        )
        edge_proofs = tuple(
            signer.sign(
                kid,
                child.issuer_id,
                cls.delegation_payload(
                    protocol_version,
                    trust_domain,
                    policy_version,
                    parent,
                    child,
                ),
                now,
            )
            for kid, parent, child in zip(kids, values, values[1:])
        )
        return cls(
            protocol_version,
            trust_domain,
            policy_version,
            values,
            root_proof,
            edge_proofs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "trustDomain": self.trust_domain,
            "policyVersion": self.policy_version,
            "claims": [item.to_dict() for item in self.claims],
            "rootProof": self.root_proof.to_dict(),
            "delegationProofs": [item.to_dict() for item in self.delegation_proofs],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CapabilityEnvelope:
        data = _strict_object(
            value,
            required=(
                "protocolVersion",
                "trustDomain",
                "policyVersion",
                "claims",
                "rootProof",
                "delegationProofs",
            ),
            field_name="capability envelope",
        )
        raw_claims = _list(data["claims"], "capability envelope.claims")
        raw_root = data["rootProof"]
        raw_edges = _list(data["delegationProofs"], "capability envelope.delegationProofs")
        if not isinstance(raw_root, dict):
            raise TypeError("capability envelope.rootProof must be an object")
        return cls(
            protocol_version=_string(
                data["protocolVersion"], "capability envelope.protocolVersion"
            ),
            trust_domain=_string(data["trustDomain"], "capability envelope.trustDomain"),
            policy_version=_string(data["policyVersion"], "capability envelope.policyVersion"),
            claims=tuple(CapabilityClaims.from_dict(item) for item in raw_claims),
            root_proof=CapabilityProof.from_dict(raw_root),
            delegation_proofs=tuple(CapabilityProof.from_dict(item) for item in raw_edges),
        )


class CapabilityVerificationError(ValueError):
    """Raised when an envelope fails a trusted verification boundary."""


def _validated_envelope_snapshot(envelope: CapabilityEnvelope) -> CapabilityEnvelope:
    """Rebuild an exact immutable snapshot before any proof decision.

    Frozen dataclasses are convenient value objects, not a Python isolation
    boundary: reflection can still replace tuple fields after construction.
    Reconstructing closes both that mutation path and races with later caller
    mutation. Exact concrete types also prevent subclasses from overriding
    serialization behavior inside the trust boundary.
    """

    if type(envelope) is not CapabilityEnvelope:
        raise CapabilityVerificationError("capability must be an exact CapabilityEnvelope")
    try:
        if type(envelope.claims) is not tuple or any(
            type(item) is not CapabilityClaims for item in envelope.claims
        ):
            raise TypeError("capability claims must be an exact tuple of CapabilityClaims")
        if type(envelope.root_proof) is not CapabilityProof:
            raise TypeError("root proof must be an exact CapabilityProof")
        if type(envelope.delegation_proofs) is not tuple or any(
            type(item) is not CapabilityProof for item in envelope.delegation_proofs
        ):
            raise TypeError("delegation proofs must be an exact tuple of CapabilityProof")
        if len(envelope.delegation_proofs) != len(envelope.claims) - 1:
            raise CapabilityVerificationError(
                "capability envelope requires exactly one proof per delegation edge"
            )
        snapshot = CapabilityEnvelope.from_dict(envelope.to_dict())
    except CapabilityVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise CapabilityVerificationError(
            "capability envelope is not a canonical immutable snapshot"
        ) from error
    if len(snapshot.delegation_proofs) != len(snapshot.claims) - 1:
        raise CapabilityVerificationError(
            "capability envelope requires exactly one proof per delegation edge"
        )
    return snapshot


@dataclass(frozen=True)
class VerifiedCapability:
    """Verification result/cache hint, never an in-process security boundary.

    Python code in the same process is not an isolation domain: objects can be
    copied, pickled, or mutated through reflection. The authorizer therefore
    ignores this object's attestation as authority and re-verifies ``envelope``
    with its own trust-domain verifier for every decision.
    """

    envelope: CapabilityEnvelope
    trust_domain: str
    policy_version: str
    verified_at: datetime
    verification_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, CapabilityEnvelope):
            raise TypeError("envelope must be CapabilityEnvelope")
        _require_text(self.trust_domain, "trust_domain", maximum=256)
        _require_text(self.policy_version, "policy_version", maximum=128)
        object.__setattr__(self, "verified_at", _as_utc(self.verified_at, "verified_at"))
        object.__setattr__(
            self,
            "verification_id",
            _canonical_digest(
                {
                    "envelopeId": self.envelope.envelope_id,
                    "trustDomain": self.trust_domain,
                    "policyVersion": self.policy_version,
                    "verifiedAt": _format_time(self.verified_at),
                }
            ),
        )

    @property
    def chain(self) -> tuple[CapabilityClaims, ...]:
        return self.envelope.claims

    @property
    def claims(self) -> CapabilityClaims:
        return self.envelope.claims[-1]

    @property
    def chain_id(self) -> str:
        return self.envelope.envelope_id


def _validate_chain_structure(
    chain: tuple[CapabilityClaims, ...],
    *,
    audience: str,
    now: datetime,
    max_ttl: timedelta,
    max_clock_skew: timedelta,
    max_chain_depth: int,
) -> None:
    if not chain:
        raise CapabilityVerificationError("capability chain cannot be empty")
    if len(chain) > max_chain_depth:
        raise CapabilityVerificationError("capability chain exceeds maximum depth")
    seen_revocation_ids = set()
    for index, claims in enumerate(chain):
        if not isinstance(claims, CapabilityClaims):
            raise CapabilityVerificationError("chain must contain CapabilityClaims")
        if claims.audience != audience:
            raise CapabilityVerificationError("capability audience does not match this service")
        if claims.expires_at - claims.issued_at > max_ttl:
            raise CapabilityVerificationError("capability exceeds maximum TTL")
        if claims.issued_at > now + max_clock_skew:
            raise CapabilityVerificationError("capability issued_at is in the future")
        if claims.revocation_id in seen_revocation_ids:
            raise CapabilityVerificationError("capability nonce repeats in issuer/tenant domain")
        seen_revocation_ids.add(claims.revocation_id)
        if index == 0:
            if claims.parent_fingerprint is not None:
                raise CapabilityVerificationError("root capability cannot reference a parent")
        else:
            try:
                validate_delegation(chain[index - 1], claims)
            except DelegationError as error:
                raise CapabilityVerificationError(str(error)) from error


class CapabilityVerifier:
    """Verify a canonical envelope inside one explicit trust/policy domain."""

    def __init__(
        self,
        *,
        proof_verifier: CapabilityProofVerifier,
        trust_domain: str,
        policy_version: str,
        audience: str,
        clock: Optional[ServerClock] = None,
        max_ttl: timedelta = timedelta(hours=1),
        max_clock_skew: timedelta = timedelta(seconds=30),
        max_chain_depth: int = 16,
    ) -> None:
        if proof_verifier is None:
            raise TypeError("proof_verifier is required")
        for method_name in ("verify", "authorize_root", "authorize_delegation"):
            if not callable(getattr(proof_verifier, method_name, None)):
                raise TypeError(
                    "proof_verifier must implement signature and root/delegation authority checks"
                )
        self._proof_verifier = proof_verifier
        self._trust_domain = _require_text(trust_domain, "trust_domain", maximum=256)
        self._policy_version = _require_text(policy_version, "policy_version", maximum=128)
        if proof_verifier.trust_domain != self._trust_domain:
            raise ValueError("proof verifier trust domain does not match verifier policy")
        if proof_verifier.policy_version != self._policy_version:
            raise ValueError("proof verifier policy version does not match verifier policy")
        self._audience = _require_text(audience, "audience", maximum=256)
        self._clock = clock if clock is not None else SystemClock()
        self._max_ttl = _require_duration(max_ttl, "max_ttl")
        self._max_clock_skew = _require_duration(max_clock_skew, "max_clock_skew")
        if isinstance(max_chain_depth, bool) or not isinstance(max_chain_depth, int):
            raise TypeError("max_chain_depth must be an integer")
        if not 1 <= max_chain_depth <= 64:
            raise ValueError("max_chain_depth must be between 1 and 64")
        self._max_chain_depth = max_chain_depth

    @property
    def trust_domain(self) -> str:
        return self._trust_domain

    @property
    def policy_version(self) -> str:
        return self._policy_version

    @property
    def audience(self) -> str:
        return self._audience

    def verify(
        self,
        envelope: CapabilityEnvelope,
        *,
        at: Optional[datetime] = None,
        allow_expired: bool = False,
    ) -> VerifiedCapability:
        if not isinstance(allow_expired, bool):
            raise TypeError("allow_expired must be bool")
        envelope = _validated_envelope_snapshot(envelope)
        now = _clock_now(self._clock) if at is None else _as_utc(at, "verification time")
        if envelope.protocol_version != CAPABILITY_PROTOCOL_VERSION:
            raise CapabilityVerificationError("capability protocol version is not supported")
        if envelope.trust_domain != self._trust_domain:
            raise CapabilityVerificationError("capability trust domain is not accepted")
        if envelope.policy_version != self._policy_version:
            raise CapabilityVerificationError("capability policy version is not accepted")
        values = envelope.claims
        _validate_chain_structure(
            values,
            audience=self._audience,
            now=now,
            max_ttl=self._max_ttl,
            max_clock_skew=self._max_clock_skew,
            max_chain_depth=self._max_chain_depth,
        )
        if not allow_expired and now >= values[-1].expires_at:
            raise CapabilityVerificationError("cannot verify an expired capability")
        try:
            root_trusted = self._proof_verifier.verify(
                envelope.root_proof,
                values[0].issuer_id,
                CapabilityEnvelope.root_payload(
                    envelope.protocol_version,
                    envelope.trust_domain,
                    envelope.policy_version,
                    values[0],
                ),
                now,
            )
        except Exception as error:
            raise CapabilityVerificationError("root proof verifier failed closed") from error
        if root_trusted is not True:
            raise CapabilityVerificationError("root capability proof is not trusted")
        try:
            root_authorized = self._proof_verifier.authorize_root(
                envelope.root_proof,
                values[0],
                now,
            )
        except Exception as error:
            raise CapabilityVerificationError(
                "root capability authority check failed closed"
            ) from error
        if root_authorized is not True:
            raise CapabilityVerificationError(
                "root signer is not authorized for the capability tenant"
            )
        for proof, parent, child in zip(
            envelope.delegation_proofs,
            values,
            values[1:],
        ):
            try:
                edge_trusted = self._proof_verifier.verify(
                    proof,
                    child.issuer_id,
                    CapabilityEnvelope.delegation_payload(
                        envelope.protocol_version,
                        envelope.trust_domain,
                        envelope.policy_version,
                        parent,
                        child,
                    ),
                    now,
                )
            except Exception as error:
                raise CapabilityVerificationError(
                    "delegation proof verifier failed closed"
                ) from error
            if edge_trusted is not True:
                raise CapabilityVerificationError("delegation proof is not trusted")
            try:
                edge_authorized = self._proof_verifier.authorize_delegation(
                    proof,
                    parent,
                    child,
                    now,
                )
            except Exception as error:
                raise CapabilityVerificationError(
                    "delegation signer authority check failed closed"
                ) from error
            if edge_authorized is not True:
                raise CapabilityVerificationError(
                    "delegation signer is not authorized for delegation proofs"
                )
        return VerifiedCapability(
            envelope=envelope,
            trust_domain=self._trust_domain,
            policy_version=self._policy_version,
            verified_at=now,
        )


@dataclass(frozen=True)
class AccessRequest:
    """Authorization input with no caller-controlled evaluation timestamp."""

    request_id: str
    subject_id: str
    tenant_id: TenantId
    action: str
    resource: ResourceRef

    def __post_init__(self) -> None:
        _require_opaque_id(self.request_id, "request_id")
        _require_opaque_id(self.subject_id, "subject_id")
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be TenantId")
        _require_requested_action(self.action)
        if not isinstance(self.resource, ResourceRef):
            raise TypeError("resource must be ResourceRef")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "subjectId": self.subject_id,
            "tenantId": str(self.tenant_id),
            "action": self.action,
            "resource": self.resource.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccessRequest:
        data = _strict_object(
            value,
            required=("requestId", "subjectId", "tenantId", "action", "resource"),
            field_name="access request",
        )
        raw_resource = data["resource"]
        if not isinstance(raw_resource, dict):
            raise TypeError("access request.resource must be an object")
        return cls(
            request_id=_string(data["requestId"], "access request.requestId"),
            subject_id=_string(data["subjectId"], "access request.subjectId"),
            tenant_id=TenantId(_string(data["tenantId"], "access request.tenantId")),
            action=_string(data["action"], "access request.action"),
            resource=ResourceRef.from_dict(raw_resource),
        )


class AuthorizationOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class DecisionCode(str, Enum):
    ALLOW_RBAC = "allow_rbac"
    ALLOW_CAPABILITY = "allow_capability"
    CROSS_TENANT = "cross_tenant"
    SUBJECT_MISMATCH = "subject_mismatch"
    NOT_A_MEMBER = "not_a_member"
    MEMBER_INACTIVE = "member_inactive"
    CAPABILITY_REVOKED = "capability_revoked"
    CAPABILITY_EXPIRED = "capability_expired"
    CAPABILITY_NOT_YET_VALID = "capability_not_yet_valid"
    CAPABILITY_INVALID = "capability_invalid"
    REVOCATION_STATE_INVALID = "revocation_state_invalid"
    REVOCATION_STATE_STALE = "revocation_state_stale"
    REVOCATION_REVISION_ROLLBACK = "revocation_revision_rollback"
    OUTSIDE_SCOPE = "outside_scope"
    DEFAULT_DENY = "default_deny"


_ALLOW_DECISION_CODES = frozenset((DecisionCode.ALLOW_RBAC, DecisionCode.ALLOW_CAPABILITY))


@dataclass(frozen=True)
class AuthorizationDecision:
    """Canonical, order-independent audit decision."""

    outcome: AuthorizationOutcome
    code: DecisionCode
    reason: str
    request: AccessRequest
    evaluated_at: datetime
    evidence: tuple[str, ...] = field(default_factory=tuple)
    revocation_revision: Optional[int] = None
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AuthorizationOutcome):
            raise TypeError("outcome must be AuthorizationOutcome")
        if not isinstance(self.code, DecisionCode):
            raise TypeError("code must be DecisionCode")
        if (self.outcome is AuthorizationOutcome.ALLOW) != (self.code in _ALLOW_DECISION_CODES):
            raise ValueError("authorization outcome and decision code are inconsistent")
        _require_text(self.reason, "reason")
        if not isinstance(self.request, AccessRequest):
            raise TypeError("request must be AccessRequest")
        object.__setattr__(self, "evaluated_at", _as_utc(self.evaluated_at, "evaluated_at"))
        evidence = _strict_tuple(self.evidence, "evidence")
        if any(not isinstance(item, str) for item in evidence):
            raise TypeError("evidence must contain strings")
        object.__setattr__(self, "evidence", tuple(sorted(set(evidence))))
        if self.revocation_revision is not None:
            revision = _integer(self.revocation_revision, "revocation_revision")
            if revision < 0:
                raise ValueError("revocation_revision cannot be negative")
        object.__setattr__(self, "decision_id", _canonical_digest(self._audit_payload()))

    @property
    def allowed(self) -> bool:
        return self.outcome is AuthorizationOutcome.ALLOW

    def _audit_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "code": self.code.value,
            "reason": self.reason,
            "request": self.request.to_dict(),
            "evaluatedAt": _format_time(self.evaluated_at),
            "evidence": list(self.evidence),
            "revocationRevision": self.revocation_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self._audit_payload()
        value["decisionId"] = self.decision_id
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AuthorizationDecision:
        data = _strict_object(
            value,
            required=(
                "outcome",
                "code",
                "reason",
                "request",
                "evaluatedAt",
                "evidence",
                "revocationRevision",
                "decisionId",
            ),
            field_name="authorization decision",
        )
        raw_request = data["request"]
        if not isinstance(raw_request, dict):
            raise TypeError("authorization decision.request must be an object")
        raw_revision = data["revocationRevision"]
        if raw_revision is not None:
            raw_revision = _integer(raw_revision, "authorization decision.revocationRevision")
        decision = cls(
            outcome=AuthorizationOutcome(
                _string(data["outcome"], "authorization decision.outcome")
            ),
            code=DecisionCode(_string(data["code"], "authorization decision.code")),
            reason=_string(data["reason"], "authorization decision.reason"),
            request=AccessRequest.from_dict(raw_request),
            evaluated_at=_parse_time(data["evaluatedAt"], "authorization decision.evaluatedAt"),
            evidence=tuple(
                _string(item, "authorization decision.evidence item")
                for item in _list(data["evidence"], "authorization decision.evidence")
            ),
            revocation_revision=raw_revision,
        )
        supplied_id = _string(data["decisionId"], "authorization decision.decisionId")
        if not _SHA256.fullmatch(supplied_id) or supplied_id != decision.decision_id:
            raise ValueError("authorization decision id does not match its contents")
        return decision


def _snapshot_access_request(request: AccessRequest) -> AccessRequest:
    if type(request) is not AccessRequest:
        raise TypeError("request must be an exact AccessRequest")
    try:
        return AccessRequest.from_dict(request.to_dict())
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("request must be a canonical AccessRequest") from error


def _snapshot_member(member: Member) -> Member:
    if type(member) is not Member:
        raise TypeError("member must be an exact Member")
    try:
        return Member.from_dict(member.to_dict())
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("member must be a canonical Member") from error


def _snapshot_revocations(snapshot: RevocationSnapshot) -> RevocationSnapshot:
    if type(snapshot) is not RevocationSnapshot:
        raise TypeError("revocations must be an exact RevocationSnapshot")
    try:
        return RevocationSnapshot.from_dict(snapshot.to_dict())
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("revocations must be a canonical RevocationSnapshot") from error


class TenantAuthorizer:
    """Default-deny authorization composed before the effect policy engine."""

    _ERROR_PRECEDENCE: Mapping[DecisionCode, int] = {
        DecisionCode.CAPABILITY_INVALID: 0,
        DecisionCode.CAPABILITY_REVOKED: 1,
        DecisionCode.CAPABILITY_EXPIRED: 2,
        DecisionCode.CAPABILITY_NOT_YET_VALID: 3,
        DecisionCode.OUTSIDE_SCOPE: 4,
        DecisionCode.DEFAULT_DENY: 5,
    }

    def __init__(
        self,
        *,
        capability_verifier: CapabilityVerifier,
        trust_domain: str,
        policy_version: str,
        revision_guard: RevocationRevisionGuard,
        audience: str,
        clock: Optional[ServerClock] = None,
        role_actions: Optional[Mapping[Role, Iterable[str]]] = None,
        max_clock_skew: timedelta = timedelta(seconds=30),
        max_revocation_age: timedelta = timedelta(minutes=5),
    ) -> None:
        if not isinstance(capability_verifier, CapabilityVerifier):
            raise TypeError("capability_verifier must be CapabilityVerifier")
        if revision_guard is None or not callable(
            getattr(revision_guard, "check_and_advance", None)
        ):
            raise TypeError("revision_guard must implement check_and_advance")
        self._capability_verifier = capability_verifier
        self._trust_domain = _require_text(trust_domain, "trust_domain", maximum=256)
        self._policy_version = _require_text(policy_version, "policy_version", maximum=128)
        self._audience = _require_text(audience, "audience", maximum=256)
        if capability_verifier.trust_domain != self._trust_domain:
            raise ValueError("authorizer trust domain does not match capability verifier")
        if capability_verifier.policy_version != self._policy_version:
            raise ValueError("authorizer policy version does not match capability verifier")
        if capability_verifier.audience != self._audience:
            raise ValueError("authorizer audience does not match capability verifier")
        self._revision_guard = revision_guard
        self._clock = clock if clock is not None else SystemClock()
        self._max_clock_skew = _require_duration(max_clock_skew, "max_clock_skew")
        self._max_revocation_age = _require_duration(max_revocation_age, "max_revocation_age")

        configured = role_actions if role_actions is not None else DEFAULT_ROLE_ACTIONS
        normalized: dict[Role, tuple[str, ...]] = {}
        for role, actions in configured.items():
            if not isinstance(role, Role):
                raise TypeError("role_actions keys must be Role values")
            grants = _strict_tuple(actions, "role action grants")
            if any(not isinstance(action, str) for action in grants):
                raise TypeError("role action grants must contain strings")
            for action in grants:
                _require_action(action, "role grant")
            normalized[role] = tuple(sorted(set(grants)))
        self._role_actions = normalized

    def evaluate(
        self,
        request: AccessRequest,
        member: Optional[Member],
        revocations: RevocationSnapshot,
        verified_capabilities: Iterable[VerifiedCapability] = (),
    ) -> AuthorizationDecision:
        """Authorize at one service-clock instant using typed trusted inputs."""

        request = _snapshot_access_request(request)
        revocations = _snapshot_revocations(revocations)
        if member is not None:
            member = _snapshot_member(member)
        capabilities = _strict_tuple(verified_capabilities, "verified_capabilities")
        if any(type(item) is not VerifiedCapability for item in capabilities):
            raise TypeError("verified_capabilities must contain VerifiedCapability values")
        now = _clock_now(self._clock)
        revision = revocations.revision if revocations.tenant_id == request.tenant_id else None

        if request.resource.tenant_id != request.tenant_id:
            return self._deny(
                request,
                now,
                DecisionCode.CROSS_TENANT,
                "request tenant and resource tenant do not match",
                revision=revision,
            )
        if member is None:
            return self._deny(
                request,
                now,
                DecisionCode.NOT_A_MEMBER,
                "subject is not a tenant member",
                revision=revision,
            )
        if member.member_id != request.subject_id:
            return self._deny(
                request,
                now,
                DecisionCode.SUBJECT_MISMATCH,
                "loaded membership does not belong to the request subject",
                revision=revision,
            )
        if member.tenant_id != request.tenant_id:
            return self._deny(
                request,
                now,
                DecisionCode.CROSS_TENANT,
                "member and request belong to different tenants",
                revision=revision,
            )
        if member.status is not MemberStatus.ACTIVE:
            return self._deny(
                request,
                now,
                DecisionCode.MEMBER_INACTIVE,
                "member is not active",
                revision=revision,
            )
        if revocations.tenant_id != request.tenant_id:
            return self._deny(
                request,
                now,
                DecisionCode.REVOCATION_STATE_INVALID,
                "revocation snapshot belongs to a different tenant",
                revision=revision,
            )
        revocation_error = self._validate_revocation_snapshot(revocations, request, now)
        if revocation_error is not None:
            code, reason = revocation_error
            return self._deny(
                request,
                now,
                code,
                reason,
                revision=revision,
            )
        try:
            revision_accepted = self._revision_guard.check_and_advance(
                request.tenant_id,
                revocations.revision,
                revocations.state_digest,
            )
        except Exception:
            return self._deny(
                request,
                now,
                DecisionCode.REVOCATION_STATE_INVALID,
                "revocation revision guard failed closed",
                revision=revision,
            )
        if revision_accepted is not True:
            return self._deny(
                request,
                now,
                DecisionCode.REVOCATION_REVISION_ROLLBACK,
                "revocation snapshot revision or same-revision state conflicts with the "
                "tenant high-water mark",
                revision=revision,
            )

        role_evidence = []
        role_outside_scope = False
        for binding in member.role_bindings:
            grants = self._role_actions.get(binding.role, ())
            action_granted = any(action_covers(grant, request.action) for grant in grants)
            if action_granted and binding.scope.contains(request.resource):
                role_evidence.append(f"role:{binding.role.value}:{binding.audit_ref}")
            elif action_granted:
                role_outside_scope = True
        if role_evidence:
            return AuthorizationDecision(
                outcome=AuthorizationOutcome.ALLOW,
                code=DecisionCode.ALLOW_RBAC,
                reason="request is covered by a scoped role binding",
                request=request,
                evaluated_at=now,
                evidence=tuple(role_evidence),
                revocation_revision=revision,
            )

        if not capabilities:
            code = DecisionCode.OUTSIDE_SCOPE if role_outside_scope else DecisionCode.DEFAULT_DENY
            reason = (
                "action grant does not cover the requested resource scope"
                if role_outside_scope
                else "no scoped role or verified capability grants the requested action"
            )
            return self._deny(request, now, code, reason, revision=revision)

        capability_evidence = []
        failures = []
        for capability in capabilities:
            trusted, failure = self._reverify_capability(capability, now)
            if failure is not None or trusted is None:
                failures.append(
                    failure
                    if failure is not None
                    else (
                        DecisionCode.CAPABILITY_INVALID,
                        "capability re-verification failed closed",
                        "capability:invalid",
                    )
                )
                continue
            leaf = trusted.claims
            if leaf.subject_id != request.subject_id or leaf.tenant_id != request.tenant_id:
                continue
            failure = self._validate_for_authorization(trusted, request, revocations, now)
            if failure is not None:
                failures.append(failure)
                continue
            capability_evidence.append(f"capability:{trusted.chain_id}")

        if capability_evidence:
            return AuthorizationDecision(
                outcome=AuthorizationOutcome.ALLOW,
                code=DecisionCode.ALLOW_CAPABILITY,
                reason="request is covered by a fully verified capability chain",
                request=request,
                evaluated_at=now,
                evidence=tuple(capability_evidence),
                revocation_revision=revocations.revision,
            )
        if failures:
            code, reason, _ = min(
                failures,
                key=lambda item: (self._ERROR_PRECEDENCE[item[0]], item[2]),
            )
            return self._deny(
                request,
                now,
                code,
                reason,
                evidence=tuple(item[2] for item in failures if item[0] is code),
                revision=revocations.revision,
            )
        if role_outside_scope:
            return self._deny(
                request,
                now,
                DecisionCode.OUTSIDE_SCOPE,
                "action grant does not cover the requested resource scope",
                revision=revocations.revision,
            )
        return self._deny(
            request,
            now,
            DecisionCode.DEFAULT_DENY,
            "no scoped role or verified capability grants the requested action",
            revision=revocations.revision,
        )

    def decide(
        self,
        request: AccessRequest,
        member: Optional[Member],
        revocations: RevocationSnapshot,
        verified_capabilities: Iterable[VerifiedCapability] = (),
    ) -> AuthorizationDecision:
        return self.evaluate(request, member, revocations, verified_capabilities)

    def _validate_revocation_snapshot(
        self,
        snapshot: RevocationSnapshot,
        request: AccessRequest,
        now: datetime,
    ) -> Optional[tuple[DecisionCode, str]]:
        if snapshot.tenant_id != request.tenant_id:
            return (
                DecisionCode.REVOCATION_STATE_INVALID,
                "revocation snapshot belongs to a different tenant",
            )
        if snapshot.captured_at > now + self._max_clock_skew:
            return (
                DecisionCode.REVOCATION_STATE_INVALID,
                "revocation snapshot timestamp is in the future",
            )
        if now - snapshot.captured_at > self._max_revocation_age:
            return (
                DecisionCode.REVOCATION_STATE_STALE,
                "revocation snapshot is too old for capability authorization",
            )
        return None

    def _validate_for_authorization(
        self,
        capability: VerifiedCapability,
        request: AccessRequest,
        revocations: RevocationSnapshot,
        now: datetime,
    ) -> Optional[tuple[DecisionCode, str, str]]:
        evidence = f"capability:{capability.chain_id}"
        leaf = capability.chain[-1]
        if not action_covers(leaf.action, request.action):
            return (
                DecisionCode.DEFAULT_DENY,
                "verified capability does not grant the requested action",
                evidence,
            )
        if not leaf.resource.contains(request.resource):
            return (
                DecisionCode.OUTSIDE_SCOPE,
                "capability action does not cover the requested resource scope",
                evidence,
            )
        for index, claims in enumerate(capability.chain):
            if revocations.contains(claims.revocation_id):
                position = "leaf" if index == len(capability.chain) - 1 else "ancestor"
                return (
                    DecisionCode.CAPABILITY_REVOKED,
                    f"capability {position} has been revoked",
                    evidence,
                )
            if now < claims.not_before:
                return (
                    DecisionCode.CAPABILITY_NOT_YET_VALID,
                    "capability chain is not active yet",
                    evidence,
                )
            if now >= claims.expires_at:
                return (
                    DecisionCode.CAPABILITY_EXPIRED,
                    "capability chain has expired",
                    evidence,
                )
        return None

    def _reverify_capability(
        self,
        capability: VerifiedCapability,
        now: datetime,
    ) -> tuple[
        Optional[VerifiedCapability],
        Optional[tuple[DecisionCode, str, str]],
    ]:
        """Re-enter the authorizer-owned trust boundary for every decision."""

        evidence = "capability:invalid"
        try:
            envelope = capability.envelope
            if type(envelope) is not CapabilityEnvelope:
                raise TypeError("verified capability does not carry a typed envelope")
            trusted = self._capability_verifier.verify(envelope, at=now, allow_expired=True)
            if trusted.trust_domain != self._trust_domain:
                raise CapabilityVerificationError("authorizer trust domain mismatch")
            if trusted.policy_version != self._policy_version:
                raise CapabilityVerificationError("authorizer policy version mismatch")
            evidence = f"capability:{trusted.chain_id}"
            return trusted, None
        except Exception:
            return (
                None,
                (
                    DecisionCode.CAPABILITY_INVALID,
                    "capability envelope failed authorizer-owned proof verification",
                    evidence,
                ),
            )

    @staticmethod
    def _deny(
        request: AccessRequest,
        now: datetime,
        code: DecisionCode,
        reason: str,
        *,
        evidence: tuple[str, ...] = (),
        revision: Optional[int] = None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            outcome=AuthorizationOutcome.DENY,
            code=code,
            reason=reason,
            request=request,
            evaluated_at=now,
            evidence=evidence,
            revocation_revision=revision,
        )
