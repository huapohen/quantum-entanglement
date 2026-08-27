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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Set, Tuple, cast

from .attempts import InvocationJobSpec, InvocationLease
from .events import DomainEvent
from .protocol import TaskStatus
from .scheduler import TaskTransition

INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION = 1
SCOPED_INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION = 2
INVOCATION_START_EVIDENCE_SCHEMA_VERSION = 2
SCOPED_INVOCATION_START_EVIDENCE_SCHEMA_VERSION = 3
INVOCATION_EXECUTION_MANIFEST_DOMAIN = "quantum-entanglement.invocation-execution-manifest/1\n"
SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN = (
    "quantum-entanglement.invocation-execution-manifest/2\n"
)
CANONICAL_ORCHESTRATOR_ACTOR_ID = "orchestrator"
TASK_EXECUTION_REQUESTED_EVENT_TYPE = "task.execution.requested"
TASK_INVOCATION_STARTED_EVENT_TYPE = "task.invocation.started"
TASK_STATUS_CHANGED_EVENT_TYPE = "task.status.changed"

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

_SCOPED_MANIFEST_FIELDS = frozenset(
    (
        "schemaVersion",
        "tenantId",
        "workspaceId",
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

_SCOPED_START_EVIDENCE_FIELDS = frozenset(
    (
        "schemaVersion",
        "tenantId",
        "workspaceId",
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

_TASK_TRANSITION_FIELDS = frozenset(("taskId", "previous", "current", "reason", "revision"))

_START_RECEIPT_FIELDS = frozenset(("eventId", "streamId", "sequence", "globalPosition", "evidence"))
_START_OBSERVED_FIELDS = frozenset(("receipt",))


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


def _integer_in_range(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value < minimum or value > maximum:
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


def _task_transition_payload(transition: object) -> Dict[str, object]:
    if type(transition) is not TaskTransition:
        raise TypeError("transition must be an exact TaskTransition")
    typed = transition
    task_id = _text(typed.task_id, "transition taskId")
    if typed.previous is not TaskStatus.READY or typed.current is not TaskStatus.RUNNING:
        raise ValueError("transition must be exactly READY to RUNNING")
    reason = typed.reason
    if reason is not None:
        reason = _text(reason, "transition reason")
    revision = _positive_integer(typed.revision, "transition revision")
    return {
        "taskId": task_id,
        "previous": TaskStatus.READY.value,
        "current": TaskStatus.RUNNING.value,
        "reason": reason,
        "revision": revision,
    }


def _task_transition_from_payload(payload: object) -> TaskTransition:
    raw = _exact_dict(payload, set(_TASK_TRANSITION_FIELDS), "task transition")
    task_id = _text(raw["taskId"], "transition taskId")
    previous = raw["previous"]
    current = raw["current"]
    if type(previous) is not str or type(current) is not str:
        raise TypeError("transition statuses must be plain strings")
    if previous != TaskStatus.READY.value or current != TaskStatus.RUNNING.value:
        raise ValueError("transition must be exactly READY to RUNNING")
    reason = raw["reason"]
    if reason is not None:
        reason = _text(reason, "transition reason")
    revision = _positive_integer(raw["revision"], "transition revision")
    return TaskTransition(task_id, TaskStatus.READY, TaskStatus.RUNNING, reason, revision)


def _admission_event_pair(events: object) -> Tuple[DomainEvent, DomainEvent]:
    if type(events) is not tuple:
        raise TypeError("canonical admission events must be an exact tuple")
    typed_events = cast(Tuple[object, ...], events)
    if len(typed_events) != 2:
        raise ValueError("canonical admission requires exactly two events")
    if any(type(item) is not DomainEvent for item in typed_events):
        raise TypeError("canonical admission requires exact DomainEvent values")
    return cast(Tuple[DomainEvent, DomainEvent], typed_events)


def _invocation_job_spec_snapshot(job_spec: object) -> InvocationJobSpec:
    if type(job_spec) is not InvocationJobSpec:
        raise TypeError("job_spec must be an exact InvocationJobSpec")
    typed_spec = job_spec
    session_id = _text(object.__getattribute__(typed_spec, "session_id"), "job sessionId")
    plan_id = _text(object.__getattribute__(typed_spec, "plan_id"), "job planId")
    task_id = _text(object.__getattribute__(typed_spec, "task_id"), "job taskId")
    agent_id = _text(object.__getattribute__(typed_spec, "agent_id"), "job agentId")
    idempotency_key = _text(
        object.__getattribute__(typed_spec, "idempotency_key"),
        "job idempotencyKey",
    )
    invocation_id = _text(
        object.__getattribute__(typed_spec, "invocation_id"),
        "job invocationId",
    )
    payload_digest = _digest(
        object.__getattribute__(typed_spec, "payload_digest"),
        "job payloadDigest",
    )
    priority = _integer_in_range(
        object.__getattribute__(typed_spec, "priority"),
        "job priority",
        minimum=0,
        maximum=100,
    )
    max_attempts = _positive_integer(
        object.__getattribute__(typed_spec, "max_attempts"),
        "job maxAttempts",
    )
    available_at = object.__getattribute__(typed_spec, "available_at")
    if available_at is not None:
        available_at = _timestamp(available_at, "job availableAt")
    return InvocationJobSpec(
        session_id=session_id,
        plan_id=plan_id,
        task_id=task_id,
        agent_id=agent_id,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        invocation_id=invocation_id,
        priority=priority,
        max_attempts=max_attempts,
        available_at=cast(Optional[str], available_at),
    )


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
class ScopedInvocationExecutionManifestV2:
    """Immutable schema-2 execution input with explicit tenant/workspace scope."""

    schema_version: int
    tenant_id: str
    workspace_id: str
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
        if type(self) is not ScopedInvocationExecutionManifestV2:
            raise TypeError("scoped manifest must be an exact ScopedInvocationExecutionManifestV2")
        _schema_version(
            self.schema_version,
            SCOPED_INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION,
            "schemaVersion",
        )
        for label, value in (
            ("tenantId", self.tenant_id),
            ("workspaceId", self.workspace_id),
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
        """Return the exact camel-case scoped schema-2 wire object."""

        ScopedInvocationExecutionManifestV2.__post_init__(self)
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
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
    def from_dict(cls, value: object) -> ScopedInvocationExecutionManifestV2:
        """Decode one exact scoped schema-2 wire object without retaining its container."""

        if cls is not ScopedInvocationExecutionManifestV2:
            raise TypeError("scoped manifest decoder requires the exact schema-2 class")
        raw = _exact_dict(
            value,
            set(_SCOPED_MANIFEST_FIELDS),
            "scoped invocation execution manifest",
        )
        return cls(
            schema_version=raw["schemaVersion"],
            tenant_id=raw["tenantId"],
            workspace_id=raw["workspaceId"],
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
    ) -> ScopedInvocationExecutionManifestV2:
        """Decode only schema-2 payloads in the canonical execution-request vocabulary."""

        if cls is not ScopedInvocationExecutionManifestV2:
            raise TypeError("scoped manifest event decoder requires the exact schema-2 class")
        _event_type(event_type, TASK_EXECUTION_REQUESTED_EVENT_TYPE)
        return cls.from_dict(payload)

    def canonical_bytes(self) -> bytes:
        """Return the unique canonical JSON bytes covered by the scoped digest."""

        return _canonical_json_bytes(self.to_dict())

    def canonical_digest(self) -> str:
        """Return the schema-2 domain-separated queued-job payload digest."""

        return hashlib.sha256(
            SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN.encode("utf-8") + self.canonical_bytes()
        ).hexdigest()


@dataclass(frozen=True)
class TaskInvocationAdmissionRequest:
    """Pure canonical request for one task execution admission.

    Event IDs and timestamps are caller-owned inputs so an exact retry produces the same
    admission manifest.  Events and the job are rebuilt on access instead of retained as
    mutable nested payloads; this object does not write or authorize anything.
    """

    manifest: InvocationExecutionManifest
    transition: TaskTransition
    execution_requested_event_id: str
    execution_requested_timestamp: str
    task_running_event_id: str
    task_running_timestamp: str
    job_priority: int = 50
    job_available_at: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self) is not TaskInvocationAdmissionRequest:
            raise TypeError("request must be an exact TaskInvocationAdmissionRequest")
        if type(self.manifest) is not InvocationExecutionManifest:
            raise TypeError("manifest must be an exact InvocationExecutionManifest")
        manifest = InvocationExecutionManifest.from_dict(self.manifest.to_dict())
        transition_payload = _task_transition_payload(self.transition)
        if (
            transition_payload["taskId"] != manifest.task_id
            or transition_payload["revision"] != manifest.task_revision
        ):
            raise ValueError("transition identity and revision must equal the manifest")

        requested_event_id = _text(
            self.execution_requested_event_id,
            "execution-request eventId",
        )
        running_event_id = _text(self.task_running_event_id, "task-running eventId")
        if requested_event_id == running_event_id:
            raise ValueError("canonical admission event IDs must be distinct")
        requested_timestamp = _timestamp(
            self.execution_requested_timestamp,
            "execution-request timestamp",
        )
        running_timestamp = _timestamp(self.task_running_timestamp, "task-running timestamp")
        if running_timestamp < requested_timestamp:
            raise ValueError("task-running timestamp must not precede execution-request timestamp")

        _text("session:" + manifest.session_id, "canonical admission streamId")
        _text(
            "execution-request:" + manifest.invocation_id,
            "execution-request idempotencyKey",
        )
        _text(
            f"task-running:{manifest.task_id}:{manifest.task_revision}",
            "task-running idempotencyKey",
        )
        _integer_in_range(self.job_priority, "job priority", minimum=0, maximum=100)
        if self.job_available_at is not None:
            _timestamp(self.job_available_at, "job availableAt")
        InvocationJobSpec(
            session_id=manifest.session_id,
            plan_id=manifest.plan_id,
            task_id=manifest.task_id,
            agent_id=manifest.agent_id,
            idempotency_key=manifest.job_idempotency_key,
            payload_digest=manifest.canonical_digest(),
            invocation_id=manifest.invocation_id,
            priority=self.job_priority,
            max_attempts=1,
            available_at=self.job_available_at,
        )

        transition = TaskTransition(
            task_id=cast(str, transition_payload["taskId"]),
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=cast(Optional[str], transition_payload["reason"]),
            revision=cast(int, transition_payload["revision"]),
        )
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "transition", transition)

    @classmethod
    def from_components(
        cls,
        events: object,
        job_spec: object,
    ) -> TaskInvocationAdmissionRequest:
        """Reconstruct one canonical request from exact durable component values.

        This decoder reads only built-in fields from exact domain model types.  It never
        invokes a caller-provided factory, callback, instance method, mapping protocol or
        duck-typed adapter while reconstructing the admission boundary.
        """

        if cls is not TaskInvocationAdmissionRequest:
            raise TypeError("request decoder requires exact TaskInvocationAdmissionRequest")
        requested, running = _admission_event_pair(events)
        requested_manifest = InvocationExecutionManifest.from_event_payload(
            object.__getattribute__(requested, "event_type"),
            object.__getattribute__(requested, "payload"),
        )
        _event_type(
            object.__getattribute__(running, "event_type"),
            TASK_STATUS_CHANGED_EVENT_TYPE,
        )
        running_transition = _task_transition_from_payload(
            object.__getattribute__(running, "payload")
        )
        spec_snapshot = _invocation_job_spec_snapshot(job_spec)
        request = cls(
            manifest=requested_manifest,
            transition=running_transition,
            execution_requested_event_id=_text(
                object.__getattribute__(requested, "event_id"),
                "execution-request eventId",
            ),
            execution_requested_timestamp=_timestamp(
                object.__getattribute__(requested, "timestamp"),
                "execution-request timestamp",
            ),
            task_running_event_id=_text(
                object.__getattribute__(running, "event_id"),
                "task-running eventId",
            ),
            task_running_timestamp=_timestamp(
                object.__getattribute__(running, "timestamp"),
                "task-running timestamp",
            ),
            job_priority=spec_snapshot.priority,
            job_available_at=spec_snapshot.available_at,
        )
        TaskInvocationAdmissionRequest.validate_components(request, events, job_spec)
        return request

    @property
    def stream_id(self) -> str:
        """Return the one session stream accepted by the canonical admission."""

        TaskInvocationAdmissionRequest.__post_init__(self)
        return "session:" + self.manifest.session_id

    @property
    def job_spec(self) -> InvocationJobSpec:
        """Build a fresh single-attempt job bound to the complete manifest."""

        TaskInvocationAdmissionRequest.__post_init__(self)
        manifest = self.manifest
        return InvocationJobSpec(
            session_id=manifest.session_id,
            plan_id=manifest.plan_id,
            task_id=manifest.task_id,
            agent_id=manifest.agent_id,
            idempotency_key=manifest.job_idempotency_key,
            payload_digest=manifest.canonical_digest(),
            invocation_id=manifest.invocation_id,
            priority=self.job_priority,
            max_attempts=1,
            available_at=self.job_available_at,
        )

    @property
    def events(self) -> Tuple[DomainEvent, DomainEvent]:
        """Build the exact ordered execution-request and READY-to-RUNNING pair."""

        TaskInvocationAdmissionRequest.__post_init__(self)
        manifest = self.manifest
        common = {
            "stream_id": "session:" + manifest.session_id,
            "actor_id": CANONICAL_ORCHESTRATOR_ACTOR_ID,
            "correlation_id": manifest.correlation_id,
            "causation_id": manifest.causation_id,
        }
        return (
            DomainEvent(
                event_type=TASK_EXECUTION_REQUESTED_EVENT_TYPE,
                payload=manifest.to_dict(),
                event_id=self.execution_requested_event_id,
                timestamp=self.execution_requested_timestamp,
                idempotency_key="execution-request:" + manifest.invocation_id,
                **common,
            ),
            DomainEvent(
                event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
                payload=_task_transition_payload(self.transition),
                event_id=self.task_running_event_id,
                timestamp=self.task_running_timestamp,
                idempotency_key=f"task-running:{manifest.task_id}:{manifest.task_revision}",
                **common,
            ),
        )

    def components(self) -> Tuple[Tuple[DomainEvent, DomainEvent], InvocationJobSpec]:
        """Return fresh canonical inputs for the generic atomic admission primitive."""

        return self.events, self.job_spec

    def validate_components(self, events: object, job_spec: object) -> None:
        """Reject any non-canonical, legacy, reordered, extra or mismatched components."""

        TaskInvocationAdmissionRequest.__post_init__(self)
        requested, running = _admission_event_pair(events)

        requested_manifest = InvocationExecutionManifest.from_event_payload(
            object.__getattribute__(requested, "event_type"),
            object.__getattribute__(requested, "payload"),
        )
        running_transition = _task_transition_from_payload(
            object.__getattribute__(running, "payload")
        )
        _event_type(
            object.__getattribute__(running, "event_type"),
            TASK_STATUS_CHANGED_EVENT_TYPE,
        )
        if requested_manifest != self.manifest:
            raise ValueError("execution-request manifest does not match the request")
        if running_transition != self.transition:
            raise ValueError("task-running transition does not match the request")

        expected_events = self.events
        for index, (actual, expected) in enumerate(zip((requested, running), expected_events)):
            for field_name in (
                "stream_id",
                "event_type",
                "actor_id",
                "event_id",
                "correlation_id",
                "causation_id",
                "idempotency_key",
            ):
                value = object.__getattribute__(actual, field_name)
                _text(value, f"canonical event {index} {field_name}")
                if value != object.__getattribute__(expected, field_name):
                    raise ValueError("canonical admission event envelope does not match")
            timestamp = object.__getattribute__(actual, "timestamp")
            _timestamp(timestamp, f"canonical event {index} timestamp")
            if timestamp != expected.timestamp:
                raise ValueError("canonical admission event timestamp does not match")

        spec_snapshot = _invocation_job_spec_snapshot(job_spec)
        if spec_snapshot != self.job_spec:
            raise ValueError("invocation job does not match the canonical manifest binding")


def build_task_invocation_admission_request(
    manifest: InvocationExecutionManifest,
    transition: TaskTransition,
    *,
    execution_requested_event_id: str,
    execution_requested_timestamp: str,
    task_running_event_id: str,
    task_running_timestamp: str,
    job_priority: int = 50,
    job_available_at: Optional[str] = None,
) -> TaskInvocationAdmissionRequest:
    """Build one side-effect-free canonical admission request with stable event identity."""

    return TaskInvocationAdmissionRequest(
        manifest=manifest,
        transition=transition,
        execution_requested_event_id=execution_requested_event_id,
        execution_requested_timestamp=execution_requested_timestamp,
        task_running_event_id=task_running_event_id,
        task_running_timestamp=task_running_timestamp,
        job_priority=job_priority,
        job_available_at=job_available_at,
    )


@dataclass(frozen=True)
class ScopedTaskInvocationAdmissionRequestV2:
    """Pure canonical admission request for one scoped schema-2 execution."""

    manifest: ScopedInvocationExecutionManifestV2
    transition: TaskTransition
    execution_requested_event_id: str
    execution_requested_timestamp: str
    task_running_event_id: str
    task_running_timestamp: str
    job_priority: int = 50
    job_available_at: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self) is not ScopedTaskInvocationAdmissionRequestV2:
            raise TypeError(
                "scoped request must be an exact ScopedTaskInvocationAdmissionRequestV2"
            )
        if type(self.manifest) is not ScopedInvocationExecutionManifestV2:
            raise TypeError("manifest must be an exact ScopedInvocationExecutionManifestV2")
        manifest = ScopedInvocationExecutionManifestV2.from_dict(self.manifest.to_dict())
        transition_payload = _task_transition_payload(self.transition)
        if (
            transition_payload["taskId"] != manifest.task_id
            or transition_payload["revision"] != manifest.task_revision
        ):
            raise ValueError("transition identity and revision must equal the scoped manifest")

        requested_event_id = _text(
            self.execution_requested_event_id,
            "execution-request eventId",
        )
        running_event_id = _text(self.task_running_event_id, "task-running eventId")
        if requested_event_id == running_event_id:
            raise ValueError("canonical admission event IDs must be distinct")
        requested_timestamp = _timestamp(
            self.execution_requested_timestamp,
            "execution-request timestamp",
        )
        running_timestamp = _timestamp(self.task_running_timestamp, "task-running timestamp")
        if running_timestamp < requested_timestamp:
            raise ValueError("task-running timestamp must not precede execution-request timestamp")

        _text("session:" + manifest.session_id, "canonical admission streamId")
        _text(
            "execution-request:" + manifest.invocation_id,
            "execution-request idempotencyKey",
        )
        _text(
            f"task-running:{manifest.task_id}:{manifest.task_revision}",
            "task-running idempotencyKey",
        )
        _integer_in_range(self.job_priority, "job priority", minimum=0, maximum=100)
        if self.job_available_at is not None:
            _timestamp(self.job_available_at, "job availableAt")
        InvocationJobSpec(
            session_id=manifest.session_id,
            plan_id=manifest.plan_id,
            task_id=manifest.task_id,
            agent_id=manifest.agent_id,
            idempotency_key=manifest.job_idempotency_key,
            payload_digest=manifest.canonical_digest(),
            invocation_id=manifest.invocation_id,
            priority=self.job_priority,
            max_attempts=1,
            available_at=self.job_available_at,
        )

        transition = TaskTransition(
            task_id=cast(str, transition_payload["taskId"]),
            previous=TaskStatus.READY,
            current=TaskStatus.RUNNING,
            reason=cast(Optional[str], transition_payload["reason"]),
            revision=cast(int, transition_payload["revision"]),
        )
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "transition", transition)

    @classmethod
    def from_components(
        cls,
        events: object,
        job_spec: object,
    ) -> ScopedTaskInvocationAdmissionRequestV2:
        """Reconstruct a scoped canonical request from exact durable components."""

        if cls is not ScopedTaskInvocationAdmissionRequestV2:
            raise TypeError(
                "scoped request decoder requires exact ScopedTaskInvocationAdmissionRequestV2"
            )
        requested, running = _admission_event_pair(events)
        requested_manifest = ScopedInvocationExecutionManifestV2.from_event_payload(
            object.__getattribute__(requested, "event_type"),
            object.__getattribute__(requested, "payload"),
        )
        _event_type(
            object.__getattribute__(running, "event_type"),
            TASK_STATUS_CHANGED_EVENT_TYPE,
        )
        running_transition = _task_transition_from_payload(
            object.__getattribute__(running, "payload")
        )
        spec_snapshot = _invocation_job_spec_snapshot(job_spec)
        request = cls(
            manifest=requested_manifest,
            transition=running_transition,
            execution_requested_event_id=_text(
                object.__getattribute__(requested, "event_id"),
                "execution-request eventId",
            ),
            execution_requested_timestamp=_timestamp(
                object.__getattribute__(requested, "timestamp"),
                "execution-request timestamp",
            ),
            task_running_event_id=_text(
                object.__getattribute__(running, "event_id"),
                "task-running eventId",
            ),
            task_running_timestamp=_timestamp(
                object.__getattribute__(running, "timestamp"),
                "task-running timestamp",
            ),
            job_priority=spec_snapshot.priority,
            job_available_at=spec_snapshot.available_at,
        )
        ScopedTaskInvocationAdmissionRequestV2.validate_components(request, events, job_spec)
        return request

    @property
    def stream_id(self) -> str:
        """Return the exact session stream used by scoped admission."""

        ScopedTaskInvocationAdmissionRequestV2.__post_init__(self)
        return "session:" + self.manifest.session_id

    @property
    def job_spec(self) -> InvocationJobSpec:
        """Build a fresh single-attempt job bound to the scoped manifest digest."""

        ScopedTaskInvocationAdmissionRequestV2.__post_init__(self)
        manifest = self.manifest
        return InvocationJobSpec(
            session_id=manifest.session_id,
            plan_id=manifest.plan_id,
            task_id=manifest.task_id,
            agent_id=manifest.agent_id,
            idempotency_key=manifest.job_idempotency_key,
            payload_digest=manifest.canonical_digest(),
            invocation_id=manifest.invocation_id,
            priority=self.job_priority,
            max_attempts=1,
            available_at=self.job_available_at,
        )

    @property
    def events(self) -> Tuple[DomainEvent, DomainEvent]:
        """Build the exact scoped execution-request and READY-to-RUNNING pair."""

        ScopedTaskInvocationAdmissionRequestV2.__post_init__(self)
        manifest = self.manifest
        common = {
            "stream_id": "session:" + manifest.session_id,
            "actor_id": CANONICAL_ORCHESTRATOR_ACTOR_ID,
            "correlation_id": manifest.correlation_id,
            "causation_id": manifest.causation_id,
        }
        return (
            DomainEvent(
                event_type=TASK_EXECUTION_REQUESTED_EVENT_TYPE,
                payload=manifest.to_dict(),
                event_id=self.execution_requested_event_id,
                timestamp=self.execution_requested_timestamp,
                idempotency_key="execution-request:" + manifest.invocation_id,
                **common,
            ),
            DomainEvent(
                event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
                payload=_task_transition_payload(self.transition),
                event_id=self.task_running_event_id,
                timestamp=self.task_running_timestamp,
                idempotency_key=f"task-running:{manifest.task_id}:{manifest.task_revision}",
                **common,
            ),
        )

    def components(self) -> Tuple[Tuple[DomainEvent, DomainEvent], InvocationJobSpec]:
        """Return fresh scoped inputs for the generic atomic admission primitive."""

        return self.events, self.job_spec

    def validate_components(self, events: object, job_spec: object) -> None:
        """Reject non-canonical or mismatched scoped admission components."""

        ScopedTaskInvocationAdmissionRequestV2.__post_init__(self)
        requested, running = _admission_event_pair(events)
        requested_manifest = ScopedInvocationExecutionManifestV2.from_event_payload(
            object.__getattribute__(requested, "event_type"),
            object.__getattribute__(requested, "payload"),
        )
        running_transition = _task_transition_from_payload(
            object.__getattribute__(running, "payload")
        )
        _event_type(
            object.__getattribute__(running, "event_type"),
            TASK_STATUS_CHANGED_EVENT_TYPE,
        )
        if requested_manifest != self.manifest:
            raise ValueError("scoped execution-request manifest does not match the request")
        if running_transition != self.transition:
            raise ValueError("task-running transition does not match the scoped request")

        expected_events = self.events
        for index, (actual, expected) in enumerate(zip((requested, running), expected_events)):
            for field_name in (
                "stream_id",
                "event_type",
                "actor_id",
                "event_id",
                "correlation_id",
                "causation_id",
                "idempotency_key",
            ):
                value = object.__getattribute__(actual, field_name)
                _text(value, f"canonical event {index} {field_name}")
                if value != object.__getattribute__(expected, field_name):
                    raise ValueError("scoped admission event envelope does not match")
            timestamp = object.__getattribute__(actual, "timestamp")
            _timestamp(timestamp, f"canonical event {index} timestamp")
            if timestamp != expected.timestamp:
                raise ValueError("scoped admission event timestamp does not match")

        spec_snapshot = _invocation_job_spec_snapshot(job_spec)
        if spec_snapshot != self.job_spec:
            raise ValueError("invocation job does not match the scoped manifest binding")


def build_scoped_task_invocation_admission_request_v2(
    manifest: ScopedInvocationExecutionManifestV2,
    transition: TaskTransition,
    *,
    execution_requested_event_id: str,
    execution_requested_timestamp: str,
    task_running_event_id: str,
    task_running_timestamp: str,
    job_priority: int = 50,
    job_available_at: Optional[str] = None,
) -> ScopedTaskInvocationAdmissionRequestV2:
    """Build one side-effect-free scoped admission request with stable event identity."""

    return ScopedTaskInvocationAdmissionRequestV2(
        manifest=manifest,
        transition=transition,
        execution_requested_event_id=execution_requested_event_id,
        execution_requested_timestamp=execution_requested_timestamp,
        task_running_event_id=task_running_event_id,
        task_running_timestamp=task_running_timestamp,
        job_priority=job_priority,
        job_available_at=job_available_at,
    )


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


@dataclass(frozen=True)
class ScopedInvocationStartEvidenceV3:
    """Immutable schema-3 start binding with explicit tenant/workspace scope."""

    schema_version: int
    tenant_id: str
    workspace_id: str
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
        if type(self) is not ScopedInvocationStartEvidenceV3:
            raise TypeError(
                "scoped start evidence must be an exact ScopedInvocationStartEvidenceV3"
            )
        _schema_version(
            self.schema_version,
            SCOPED_INVOCATION_START_EVIDENCE_SCHEMA_VERSION,
            "schemaVersion",
        )
        for label, value in (
            ("tenantId", self.tenant_id),
            ("workspaceId", self.workspace_id),
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
        """Return the exact camel-case scoped schema-3 wire object."""

        ScopedInvocationStartEvidenceV3.__post_init__(self)
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
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
    def from_dict(cls, value: object) -> ScopedInvocationStartEvidenceV3:
        """Decode one exact scoped schema-3 wire object without retaining its container."""

        if cls is not ScopedInvocationStartEvidenceV3:
            raise TypeError("scoped start decoder requires the exact schema-3 class")
        raw = _exact_dict(
            value,
            set(_SCOPED_START_EVIDENCE_FIELDS),
            "scoped invocation start evidence",
        )
        return cls(
            schema_version=raw["schemaVersion"],
            tenant_id=raw["tenantId"],
            workspace_id=raw["workspaceId"],
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
    ) -> ScopedInvocationStartEvidenceV3:
        """Decode only scoped starts in the canonical invocation-start vocabulary."""

        if cls is not ScopedInvocationStartEvidenceV3:
            raise TypeError("scoped start event decoder requires the exact schema-3 class")
        _event_type(event_type, TASK_INVOCATION_STARTED_EVENT_TYPE)
        return cls.from_dict(payload)


@dataclass(frozen=True)
class InvocationStartReceipt:
    """Immutable stored-event coordinates and schema-2 invocation-start evidence."""

    event_id: str
    stream_id: str
    sequence: int
    global_position: int
    evidence: InvocationStartEvidenceV2

    def __post_init__(self) -> None:
        if type(self) is not InvocationStartReceipt:
            raise TypeError("start receipt must be an exact InvocationStartReceipt")
        event_id = _text(self.event_id, "start receipt eventId")
        stream_id = _text(self.stream_id, "start receipt streamId")
        sequence = _positive_integer(self.sequence, "start receipt sequence")
        global_position = _positive_integer(
            self.global_position,
            "start receipt globalPosition",
        )
        if global_position < sequence:
            raise ValueError("start receipt globalPosition must not precede its stream sequence")
        if type(self.evidence) is not InvocationStartEvidenceV2:
            raise TypeError("start receipt evidence must be exact InvocationStartEvidenceV2")
        evidence = InvocationStartEvidenceV2.from_dict(
            InvocationStartEvidenceV2.to_dict(self.evidence)
        )
        if stream_id != "session:" + evidence.session_id:
            raise ValueError("start receipt streamId does not match its evidence sessionId")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "global_position", global_position)
        object.__setattr__(self, "evidence", evidence)

    def to_dict(self) -> Dict[str, object]:
        """Return a capability-free wire snapshot of this durable receipt."""

        InvocationStartReceipt.__post_init__(self)
        return {
            "eventId": self.event_id,
            "streamId": self.stream_id,
            "sequence": self.sequence,
            "globalPosition": self.global_position,
            "evidence": InvocationStartEvidenceV2.to_dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, value: object) -> InvocationStartReceipt:
        """Decode one exact capability-free receipt wire object."""

        if cls is not InvocationStartReceipt:
            raise TypeError("receipt decoder requires exact InvocationStartReceipt")
        raw = _exact_dict(value, set(_START_RECEIPT_FIELDS), "invocation start receipt")
        return cls(
            event_id=raw["eventId"],
            stream_id=raw["streamId"],
            sequence=raw["sequence"],
            global_position=raw["globalPosition"],
            evidence=InvocationStartEvidenceV2.from_dict(raw["evidence"]),
        )


def _invocation_start_receipt_snapshot(receipt: object) -> InvocationStartReceipt:
    if type(receipt) is not InvocationStartReceipt:
        raise TypeError("receipt must be an exact InvocationStartReceipt")
    return InvocationStartReceipt.from_dict(InvocationStartReceipt.to_dict(receipt))


def _invocation_lease_snapshot(lease: object) -> InvocationLease:
    if type(lease) is not InvocationLease:
        raise TypeError("lease must be an exact InvocationLease")
    typed = lease
    invocation_id = _text(
        object.__getattribute__(typed, "invocation_id"),
        "lease invocationId",
    )
    session_id = _text(object.__getattribute__(typed, "session_id"), "lease sessionId")
    plan_id = _text(object.__getattribute__(typed, "plan_id"), "lease planId")
    task_id = _text(object.__getattribute__(typed, "task_id"), "lease taskId")
    agent_id = _text(object.__getattribute__(typed, "agent_id"), "lease agentId")
    idempotency_key = _text(
        object.__getattribute__(typed, "idempotency_key"),
        "lease idempotencyKey",
    )
    payload_digest = _digest(
        object.__getattribute__(typed, "payload_digest"),
        "lease payloadDigest",
    )
    attempt_id = _text(object.__getattribute__(typed, "attempt_id"), "lease attemptId")
    attempt_number = _positive_integer(
        object.__getattribute__(typed, "attempt_number"),
        "lease attemptNumber",
    )
    max_attempts = _positive_integer(
        object.__getattribute__(typed, "max_attempts"),
        "lease maxAttempts",
    )
    lease_epoch = _positive_integer(
        object.__getattribute__(typed, "lease_epoch"),
        "lease epoch",
    )
    worker_id = _text(object.__getattribute__(typed, "worker_id"), "lease workerId")
    lease_token = _text(
        object.__getattribute__(typed, "lease_token"),
        "lease token",
    )
    claimed_at = _timestamp(
        object.__getattribute__(typed, "claimed_at"),
        "lease claimedAt",
    )
    lease_expires_at = _timestamp(
        object.__getattribute__(typed, "lease_expires_at"),
        "lease leaseExpiresAt",
    )
    if lease_expires_at <= claimed_at:
        raise ValueError("lease leaseExpiresAt must follow claimedAt")
    return InvocationLease(
        invocation_id=invocation_id,
        session_id=session_id,
        plan_id=plan_id,
        task_id=task_id,
        agent_id=agent_id,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        lease_epoch=lease_epoch,
        worker_id=worker_id,
        lease_token=lease_token,
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
    )


def _validate_start_lease_binding(
    receipt: InvocationStartReceipt,
    lease: InvocationLease,
) -> None:
    evidence = receipt.evidence
    bindings = (
        (lease.invocation_id, evidence.invocation_id),
        (lease.session_id, evidence.session_id),
        (lease.plan_id, evidence.plan_id),
        (lease.task_id, evidence.task_id),
        (lease.agent_id, evidence.agent_id),
        (lease.idempotency_key, evidence.job_idempotency_key),
        (lease.payload_digest, evidence.manifest_digest),
        (lease.attempt_id, evidence.attempt_id),
        (lease.attempt_number, evidence.attempt_number),
        (lease.lease_epoch, evidence.lease_epoch),
        (lease.worker_id, evidence.worker_id),
        (lease.claimed_at, evidence.claimed_at),
        (lease.lease_expires_at, evidence.lease_expires_at),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValueError("claimed lease does not match its invocation-start evidence")
    if lease.max_attempts != 1:
        raise ValueError("claimed lease must preserve the canonical single-attempt policy")
    if hashlib.sha256(lease.lease_token.encode("utf-8")).hexdigest() != evidence.lease_token_digest:
        raise ValueError("claimed lease token does not match its evidence digest")


@dataclass(frozen=True)
class InvocationStartClaimed:
    """Newly claimed worker authority paired with its durable start receipt.

    This capability-bearing result intentionally has no ``to_dict`` method.  Its raw
    lease is excluded from repr and must be handed directly to fenced worker APIs.
    """

    receipt: InvocationStartReceipt
    lease: InvocationLease = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not InvocationStartClaimed:
            raise TypeError("claimed result must be an exact InvocationStartClaimed")
        receipt = _invocation_start_receipt_snapshot(self.receipt)
        lease = _invocation_lease_snapshot(self.lease)
        _validate_start_lease_binding(receipt, lease)
        object.__setattr__(self, "receipt", receipt)
        object.__setattr__(self, "lease", lease)


@dataclass(frozen=True)
class InvocationStartObserved:
    """Capability-free replay result for an already committed invocation start."""

    receipt: InvocationStartReceipt

    def __post_init__(self) -> None:
        if type(self) is not InvocationStartObserved:
            raise TypeError("observed result must be an exact InvocationStartObserved")
        object.__setattr__(self, "receipt", _invocation_start_receipt_snapshot(self.receipt))

    def to_dict(self) -> Dict[str, object]:
        """Return a capability-free replay observation."""

        InvocationStartObserved.__post_init__(self)
        return {"receipt": InvocationStartReceipt.to_dict(self.receipt)}

    @classmethod
    def from_dict(cls, value: object) -> InvocationStartObserved:
        """Decode one exact capability-free replay observation."""

        if cls is not InvocationStartObserved:
            raise TypeError("observed decoder requires exact InvocationStartObserved")
        raw = _exact_dict(value, set(_START_OBSERVED_FIELDS), "invocation start observation")
        return cls(receipt=InvocationStartReceipt.from_dict(raw["receipt"]))


@dataclass(frozen=True)
class ScopedInvocationStartReceiptV3:
    """Capability-free coordinates for one scoped schema-3 start event."""

    event_id: str
    stream_id: str
    sequence: int
    global_position: int
    evidence: ScopedInvocationStartEvidenceV3

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationStartReceiptV3:
            raise TypeError("scoped start receipt must be an exact ScopedInvocationStartReceiptV3")
        event_id = _text(self.event_id, "scoped start receipt eventId")
        stream_id = _text(self.stream_id, "scoped start receipt streamId")
        sequence = _positive_integer(self.sequence, "scoped start receipt sequence")
        global_position = _positive_integer(
            self.global_position,
            "scoped start receipt globalPosition",
        )
        if global_position < sequence:
            raise ValueError(
                "scoped start receipt globalPosition must not precede its stream sequence"
            )
        if type(self.evidence) is not ScopedInvocationStartEvidenceV3:
            raise TypeError(
                "scoped start receipt evidence must be exact ScopedInvocationStartEvidenceV3"
            )
        evidence = ScopedInvocationStartEvidenceV3.from_dict(
            ScopedInvocationStartEvidenceV3.to_dict(self.evidence)
        )
        if stream_id != "session:" + evidence.session_id:
            raise ValueError("scoped start receipt streamId does not match its evidence sessionId")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "global_position", global_position)
        object.__setattr__(self, "evidence", evidence)

    def to_dict(self) -> Dict[str, object]:
        """Return the exact capability-free scoped receipt wire object."""

        ScopedInvocationStartReceiptV3.__post_init__(self)
        return {
            "eventId": self.event_id,
            "streamId": self.stream_id,
            "sequence": self.sequence,
            "globalPosition": self.global_position,
            "evidence": ScopedInvocationStartEvidenceV3.to_dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationStartReceiptV3:
        """Decode one exact capability-free scoped receipt."""

        if cls is not ScopedInvocationStartReceiptV3:
            raise TypeError("scoped receipt decoder requires the exact schema-3 class")
        raw = _exact_dict(value, set(_START_RECEIPT_FIELDS), "scoped invocation start receipt")
        return cls(
            event_id=raw["eventId"],
            stream_id=raw["streamId"],
            sequence=raw["sequence"],
            global_position=raw["globalPosition"],
            evidence=ScopedInvocationStartEvidenceV3.from_dict(raw["evidence"]),
        )


def _scoped_invocation_start_receipt_snapshot(
    receipt: object,
) -> ScopedInvocationStartReceiptV3:
    if type(receipt) is not ScopedInvocationStartReceiptV3:
        raise TypeError("receipt must be an exact ScopedInvocationStartReceiptV3")
    return ScopedInvocationStartReceiptV3.from_dict(ScopedInvocationStartReceiptV3.to_dict(receipt))


def _validate_scoped_start_lease_binding(
    receipt: ScopedInvocationStartReceiptV3,
    lease: InvocationLease,
) -> None:
    evidence = receipt.evidence
    bindings = (
        (lease.invocation_id, evidence.invocation_id),
        (lease.session_id, evidence.session_id),
        (lease.plan_id, evidence.plan_id),
        (lease.task_id, evidence.task_id),
        (lease.agent_id, evidence.agent_id),
        (lease.idempotency_key, evidence.job_idempotency_key),
        (lease.payload_digest, evidence.manifest_digest),
        (lease.attempt_id, evidence.attempt_id),
        (lease.attempt_number, evidence.attempt_number),
        (lease.lease_epoch, evidence.lease_epoch),
        (lease.worker_id, evidence.worker_id),
        (lease.claimed_at, evidence.claimed_at),
        (lease.lease_expires_at, evidence.lease_expires_at),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValueError("claimed lease does not match its scoped invocation-start evidence")
    if lease.max_attempts != 1:
        raise ValueError("scoped claimed lease must preserve the single-attempt policy")
    if hashlib.sha256(lease.lease_token.encode("utf-8")).hexdigest() != evidence.lease_token_digest:
        raise ValueError("claimed lease token does not match its scoped evidence digest")


@dataclass(frozen=True)
class ScopedInvocationStartClaimedV3:
    """One non-replayable scoped start receipt paired with its plaintext lease."""

    receipt: ScopedInvocationStartReceiptV3
    lease: InvocationLease = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationStartClaimedV3:
            raise TypeError("scoped claimed result must be an exact ScopedInvocationStartClaimedV3")
        receipt = _scoped_invocation_start_receipt_snapshot(self.receipt)
        lease = _invocation_lease_snapshot(self.lease)
        _validate_scoped_start_lease_binding(receipt, lease)
        object.__setattr__(self, "receipt", receipt)
        object.__setattr__(self, "lease", lease)


@dataclass(frozen=True)
class ScopedInvocationStartObservedV3:
    """Capability-free replay observation for a scoped schema-3 start."""

    receipt: ScopedInvocationStartReceiptV3

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationStartObservedV3:
            raise TypeError(
                "scoped observed result must be an exact ScopedInvocationStartObservedV3"
            )
        object.__setattr__(
            self,
            "receipt",
            _scoped_invocation_start_receipt_snapshot(self.receipt),
        )

    def to_dict(self) -> Dict[str, object]:
        """Return the exact capability-free scoped replay observation."""

        ScopedInvocationStartObservedV3.__post_init__(self)
        return {"receipt": self.receipt.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationStartObservedV3:
        """Decode one exact capability-free scoped replay observation."""

        if cls is not ScopedInvocationStartObservedV3:
            raise TypeError("scoped observation decoder requires the exact schema-3 class")
        raw = _exact_dict(
            value,
            set(_START_OBSERVED_FIELDS),
            "scoped invocation start observation",
        )
        return cls(receipt=ScopedInvocationStartReceiptV3.from_dict(raw["receipt"]))


__all__ = [
    "CANONICAL_ORCHESTRATOR_ACTOR_ID",
    "INVOCATION_EXECUTION_MANIFEST_DOMAIN",
    "INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION",
    "INVOCATION_START_EVIDENCE_SCHEMA_VERSION",
    "SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN",
    "SCOPED_INVOCATION_EXECUTION_MANIFEST_SCHEMA_VERSION",
    "SCOPED_INVOCATION_START_EVIDENCE_SCHEMA_VERSION",
    "TASK_EXECUTION_REQUESTED_EVENT_TYPE",
    "TASK_INVOCATION_STARTED_EVENT_TYPE",
    "TASK_STATUS_CHANGED_EVENT_TYPE",
    "EffectClass",
    "InvocationExecutionManifest",
    "InvocationStartClaimed",
    "InvocationStartEvidenceV2",
    "InvocationStartObserved",
    "InvocationStartReceipt",
    "RetryClass",
    "ScopedInvocationExecutionManifestV2",
    "ScopedInvocationStartClaimedV3",
    "ScopedInvocationStartEvidenceV3",
    "ScopedInvocationStartObservedV3",
    "ScopedInvocationStartReceiptV3",
    "ScopedTaskInvocationAdmissionRequestV2",
    "TaskInvocationAdmissionRequest",
    "build_task_invocation_admission_request",
    "build_scoped_task_invocation_admission_request_v2",
]
