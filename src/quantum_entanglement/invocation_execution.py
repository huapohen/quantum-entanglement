# ruff: noqa: UP006, UP035, UP045
"""Strict, side-effect-free codecs for invocation execution evidence.

The values in this module are immutable wire models.  They neither authorize a worker nor
claim an invocation.  Durable authority is established only when the EventStore validates
and commits the corresponding admission/start transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Set, cast

INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION = 1
INVOCATION_START_EVIDENCE_SCHEMA_VERSION = 2
INVOCATION_EXECUTION_MANIFEST_DOMAIN = "quantum-entanglement.invocation-execution-manifest/1\n"
TASK_EXECUTION_REQUESTED_EVENT_TYPE = "task.execution.requested"
TASK_INVOCATION_STARTED_EVENT_TYPE = "task.invocation.started"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_IDENTITY_BYTES = 4_096

_MANIFEST_FIELDS = frozenset(
    (
        "schemaVersion",
        "invocationId",
        "sessionId",
        "planId",
        "taskId",
        "agentId",
        "jobIdempotencyKey",
        "taskRevision",
        "correlationId",
        "causationId",
        "envelopeDigest",
        "contextDigest",
        "authorizationDigest",
        "runtimeRevision",
        "effectClass",
        "retryClass",
    )
)

_START_EVIDENCE_FIELDS = frozenset(
    (
        "schemaVersion",
        "invocationId",
        "sessionId",
        "planId",
        "taskId",
        "agentId",
        "jobIdempotencyKey",
        "attemptId",
        "attemptNumber",
        "leaseEpoch",
        "workerId",
        "leaseTokenDigest",
        "claimedAt",
        "leaseExpiresAt",
        "manifestDigest",
        "envelopeDigest",
        "contextDigest",
        "authorizationDigest",
        "runtimeRevision",
        "correlationId",
        "causationId",
    )
)


class EffectClass(str, Enum):
    """External-effect classification frozen by the execution admission manifest."""

    PURE = "pure"
    IDEMPOTENT = "idempotent"
    RECEIPT_RECONCILED = "receipt_reconciled"
    NON_RETRIABLE = "non_retriable"


class RetryClass(str, Enum):
    """Retry classifications supported by this first execution-evidence schema."""

    NEVER = "never"


def _exact_dict(value: object, fields: Set[str], label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a plain dictionary")
    typed = cast(Dict[str, Any], value)
    keys = tuple(typed)
    if any(type(key) is not str for key in keys):
        raise TypeError(f"{label} keys must be plain strings")
    if set(keys) != fields:
        raise ValueError(f"{label} fields do not match its exact schema version")
    return typed


def _schema_version(value: object, expected: int, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value != expected:
        raise ValueError(f"{label} is unsupported")
    return value


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value <= 0 or value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"{label} is outside its supported range")
    return value


def _text(value: object, label: str, *, maximum_bytes: int = _MAX_IDENTITY_BYTES) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty without surrounding whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        # Raise outside the handler so the codec does not retain an attacker-controlled
        # Unicode exception as ``__context__`` or ``__cause__``.
        raise ValueError(f"{label} must be valid UTF-8") from None
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds its UTF-8 byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} contains a C0 or DEL control character")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use Unicode NFC")
    return value


def _digest(value: object, label: str) -> str:
    digest = _text(value, label, maximum_bytes=64)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be canonical lowercase SHA-256")
    return digest


def _timestamp(value: object, label: str) -> str:
    timestamp = _text(value, label, maximum_bytes=27)
    if _CANONICAL_UTC_PATTERN.fullmatch(timestamp) is None:
        raise ValueError(f"{label} must be canonical UTC with microseconds")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValueError(f"{label} must be a valid UTC timestamp") from None
    canonical = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if canonical != timestamp:
        raise ValueError(f"{label} must be canonical UTC with microseconds")
    return timestamp


def _effect_class(value: object) -> EffectClass:
    if type(value) is not str:
        raise TypeError("effectClass must be a plain string")
    try:
        return EffectClass(value)
    except ValueError:
        pass
    raise ValueError("effectClass is unsupported") from None


def _retry_class(value: object) -> RetryClass:
    if type(value) is not str:
        raise TypeError("retryClass must be a plain string")
    try:
        return RetryClass(value)
    except ValueError:
        pass
    raise ValueError("retryClass is unsupported") from None


def _event_type(value: object, expected: str) -> str:
    if type(value) is not str:
        raise TypeError("event type must be a plain string")
    if value != expected:
        raise ValueError(f"event type must be exactly {expected}")
    return value


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class InvocationExecutionManifest:
    """Immutable schema-1 input binding for one logical invocation."""

    schema_version: int
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    job_idempotency_key: str
    task_revision: int
    correlation_id: str
    causation_id: str
    envelope_digest: str
    context_digest: str
    authorization_digest: str
    runtime_revision: str
    effect_class: EffectClass
    retry_class: RetryClass

    def __post_init__(self) -> None:
        if type(self) is not InvocationExecutionManifest:
            raise TypeError("manifest must be an exact InvocationExecutionManifest")
        _schema_version(
            self.schema_version,
            INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION,
            "schemaVersion",
        )
        for label, value in (
            ("invocationId", self.invocation_id),
            ("sessionId", self.session_id),
            ("planId", self.plan_id),
            ("taskId", self.task_id),
            ("agentId", self.agent_id),
            ("jobIdempotencyKey", self.job_idempotency_key),
            ("correlationId", self.correlation_id),
            ("causationId", self.causation_id),
            ("runtimeRevision", self.runtime_revision),
        ):
            _text(value, label)
        _positive_integer(self.task_revision, "taskRevision")
        _digest(self.envelope_digest, "envelopeDigest")
        _digest(self.context_digest, "contextDigest")
        _digest(self.authorization_digest, "authorizationDigest")
        if self.causation_id != self.task_id:
            raise ValueError("causationId must equal taskId")
        if type(self.effect_class) is not EffectClass:
            raise TypeError("effect_class must be an exact EffectClass")
        if type(self.retry_class) is not RetryClass:
            raise TypeError("retry_class must be an exact RetryClass")

    def to_dict(self) -> Dict[str, object]:
        """Return the exact camel-case schema-1 wire object."""

        InvocationExecutionManifest.__post_init__(self)
        return {
            "schemaVersion": self.schema_version,
            "invocationId": self.invocation_id,
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "taskId": self.task_id,
            "agentId": self.agent_id,
            "jobIdempotencyKey": self.job_idempotency_key,
            "taskRevision": self.task_revision,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "envelopeDigest": self.envelope_digest,
            "contextDigest": self.context_digest,
            "authorizationDigest": self.authorization_digest,
            "runtimeRevision": self.runtime_revision,
            "effectClass": self.effect_class.value,
            "retryClass": self.retry_class.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> InvocationExecutionManifest:
        """Decode one exact schema-1 wire object without retaining its container."""

        raw = _exact_dict(value, set(_MANIFEST_FIELDS), "invocation execution manifest")
        return cls(
            schema_version=raw["schemaVersion"],
            invocation_id=raw["invocationId"],
            session_id=raw["sessionId"],
            plan_id=raw["planId"],
            task_id=raw["taskId"],
            agent_id=raw["agentId"],
            job_idempotency_key=raw["jobIdempotencyKey"],
            task_revision=raw["taskRevision"],
            correlation_id=raw["correlationId"],
            causation_id=raw["causationId"],
            envelope_digest=raw["envelopeDigest"],
            context_digest=raw["contextDigest"],
            authorization_digest=raw["authorizationDigest"],
            runtime_revision=raw["runtimeRevision"],
            effect_class=_effect_class(raw["effectClass"]),
            retry_class=_retry_class(raw["retryClass"]),
        )

    @classmethod
    def from_event_payload(
        cls,
        event_type: object,
        payload: object,
    ) -> InvocationExecutionManifest:
        """Decode only the canonical execution-request event vocabulary."""

        _event_type(event_type, TASK_EXECUTION_REQUESTED_EVENT_TYPE)
        return cls.from_dict(payload)

    def canonical_bytes(self) -> bytes:
        """Return the unique canonical JSON bytes covered by the manifest digest."""

        return _canonical_json_bytes(self.to_dict())

    def canonical_digest(self) -> str:
        """Return the domain-separated manifest digest used by the queued job."""

        return hashlib.sha256(
            INVOCATION_EXECUTION_MANIFEST_DOMAIN.encode("utf-8") + self.canonical_bytes()
        ).hexdigest()


@dataclass(frozen=True)
class InvocationStartEvidenceV2:
    """Immutable schema-2 attempt binding; never a dispatch capability."""

    schema_version: int
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    job_idempotency_key: str
    attempt_id: str
    attempt_number: int
    lease_epoch: int
    worker_id: str
    lease_token_digest: str
    claimed_at: str
    lease_expires_at: str
    manifest_digest: str
    envelope_digest: str
    context_digest: str
    authorization_digest: str
    runtime_revision: str
    correlation_id: str
    causation_id: str

    def __post_init__(self) -> None:
        if type(self) is not InvocationStartEvidenceV2:
            raise TypeError("start evidence must be an exact InvocationStartEvidenceV2")
        _schema_version(
            self.schema_version,
            INVOCATION_START_EVIDENCE_SCHEMA_VERSION,
            "schemaVersion",
        )
        for label, value in (
            ("invocationId", self.invocation_id),
            ("sessionId", self.session_id),
            ("planId", self.plan_id),
            ("taskId", self.task_id),
            ("agentId", self.agent_id),
            ("jobIdempotencyKey", self.job_idempotency_key),
            ("attemptId", self.attempt_id),
            ("workerId", self.worker_id),
            ("runtimeRevision", self.runtime_revision),
            ("correlationId", self.correlation_id),
            ("causationId", self.causation_id),
        ):
            _text(value, label)
        _positive_integer(self.attempt_number, "attemptNumber")
        _positive_integer(self.lease_epoch, "leaseEpoch")
        _digest(self.lease_token_digest, "leaseTokenDigest")
        claimed_at = _timestamp(self.claimed_at, "claimedAt")
        lease_expires_at = _timestamp(self.lease_expires_at, "leaseExpiresAt")
        if lease_expires_at <= claimed_at:
            raise ValueError("leaseExpiresAt must follow claimedAt")
        _digest(self.manifest_digest, "manifestDigest")
        _digest(self.envelope_digest, "envelopeDigest")
        _digest(self.context_digest, "contextDigest")
        _digest(self.authorization_digest, "authorizationDigest")
        if self.causation_id != self.task_id:
            raise ValueError("causationId must equal taskId")

    def to_dict(self) -> Dict[str, object]:
        """Return the exact camel-case schema-2 wire object."""

        InvocationStartEvidenceV2.__post_init__(self)
        return {
            "schemaVersion": self.schema_version,
            "invocationId": self.invocation_id,
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "taskId": self.task_id,
            "agentId": self.agent_id,
            "jobIdempotencyKey": self.job_idempotency_key,
            "attemptId": self.attempt_id,
            "attemptNumber": self.attempt_number,
            "leaseEpoch": self.lease_epoch,
            "workerId": self.worker_id,
            "leaseTokenDigest": self.lease_token_digest,
            "claimedAt": self.claimed_at,
            "leaseExpiresAt": self.lease_expires_at,
            "manifestDigest": self.manifest_digest,
            "envelopeDigest": self.envelope_digest,
            "contextDigest": self.context_digest,
            "authorizationDigest": self.authorization_digest,
            "runtimeRevision": self.runtime_revision,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> InvocationStartEvidenceV2:
        """Decode one exact schema-2 wire object without retaining its container."""

        raw = _exact_dict(value, set(_START_EVIDENCE_FIELDS), "invocation start evidence")
        return cls(
            schema_version=raw["schemaVersion"],
            invocation_id=raw["invocationId"],
            session_id=raw["sessionId"],
            plan_id=raw["planId"],
            task_id=raw["taskId"],
            agent_id=raw["agentId"],
            job_idempotency_key=raw["jobIdempotencyKey"],
            attempt_id=raw["attemptId"],
            attempt_number=raw["attemptNumber"],
            lease_epoch=raw["leaseEpoch"],
            worker_id=raw["workerId"],
            lease_token_digest=raw["leaseTokenDigest"],
            claimed_at=raw["claimedAt"],
            lease_expires_at=raw["leaseExpiresAt"],
            manifest_digest=raw["manifestDigest"],
            envelope_digest=raw["envelopeDigest"],
            context_digest=raw["contextDigest"],
            authorization_digest=raw["authorizationDigest"],
            runtime_revision=raw["runtimeRevision"],
            correlation_id=raw["correlationId"],
            causation_id=raw["causationId"],
        )

    @classmethod
    def from_event_payload(
        cls,
        event_type: object,
        payload: object,
    ) -> InvocationStartEvidenceV2:
        """Decode only the canonical invocation-start event vocabulary."""

        _event_type(event_type, TASK_INVOCATION_STARTED_EVENT_TYPE)
        return cls.from_dict(payload)


__all__ = [
    "INVOCATION_EXECUTION_MANIFEST_DOMAIN",
    "INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION",
    "INVOCATION_START_EVIDENCE_SCHEMA_VERSION",
    "TASK_EXECUTION_REQUESTED_EVENT_TYPE",
    "TASK_INVOCATION_STARTED_EVENT_TYPE",
    "EffectClass",
    "InvocationExecutionManifest",
    "InvocationStartEvidenceV2",
    "RetryClass",
]
