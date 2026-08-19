"""Stable domain messages shared by chat, orchestration, and protocol adapters.

The envelope is an internal coordination contract, not a replacement for A2A,
ACP, or MCP. Adapters preserve its causation, idempotency, authority, and trace
metadata when crossing an external protocol boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """Create a sortable-enough opaque identifier with a human-readable prefix."""

    return "%s_%s" % (prefix, uuid.uuid4().hex)


class ActorKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"


class EnvelopeKind(str, Enum):
    CHAT = "chat"
    TASK_ASSIGN = "task.assign"
    TASK_PROGRESS = "task.progress"
    TASK_RESULT = "task.result"
    HANDOFF = "handoff"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_DECISION = "approval.decision"
    CONTROL = "control"
    ERROR = "error"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {
            RiskLevel.NONE: 0,
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }[self]


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


@dataclass(frozen=True)
class ActorRef:
    actor_id: str
    name: str
    kind: ActorKind
    role: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.name.strip():
            raise ValueError("actor_id and name are required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "actorId": self.actor_id,
            "name": self.name,
            "kind": self.kind.value,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActorRef":
        return cls(
            actor_id=str(value["actorId"]),
            name=str(value["name"]),
            kind=ActorKind(str(value["kind"])),
            role=value.get("role"),
        )


@dataclass(frozen=True)
class Authority:
    """Delegated authority attached to a handoff, never inferred from identity."""

    allowed_actions: Tuple[str, ...] = ()
    data_scopes: Tuple[str, ...] = ()
    max_risk: RiskLevel = RiskLevel.LOW
    external_side_effects: bool = False

    def permits(self, action: str, risk: RiskLevel, external_side_effect: bool = False) -> bool:
        action_allowed = "*" in self.allowed_actions or action in self.allowed_actions
        return (
            action_allowed
            and risk.rank <= self.max_risk.rank
            and (not external_side_effect or self.external_side_effects)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowedActions": list(self.allowed_actions),
            "dataScopes": list(self.data_scopes),
            "maxRisk": self.max_risk.value,
            "externalSideEffects": self.external_side_effects,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Authority":
        return cls(
            allowed_actions=tuple(str(x) for x in value.get("allowedActions", [])),
            data_scopes=tuple(str(x) for x in value.get("dataScopes", [])),
            max_risk=RiskLevel(str(value.get("maxRisk", RiskLevel.LOW.value))),
            external_side_effects=bool(value.get("externalSideEffects", False)),
        )


@dataclass(frozen=True)
class ArtifactOutput:
    name: str
    content: str
    media_type: str = "text/markdown"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("artifact name is required")


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    name: str
    version: int
    media_type: str
    uri: str
    digest: str
    created_by: str
    task_id: str
    parent_version: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "version": self.version,
            "mediaType": self.media_type,
            "uri": self.uri,
            "digest": self.digest,
            "createdBy": self.created_by,
            "taskId": self.task_id,
            "parentVersion": self.parent_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=str(value["artifactId"]),
            name=str(value["name"]),
            version=int(value["version"]),
            media_type=str(value["mediaType"]),
            uri=str(value["uri"]),
            digest=str(value["digest"]),
            created_by=str(value["createdBy"]),
            task_id=str(value["taskId"]),
            parent_version=(
                int(value["parentVersion"]) if value.get("parentVersion") is not None else None
            ),
        )


@dataclass(frozen=True)
class ContextRef:
    """A versioned pointer used instead of copying an unbounded context blob."""

    ref_id: str
    category: str
    version: str
    digest: str
    required: bool = False
    relevance: float = 0.5
    provenance: Optional[str] = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.relevance <= 1.0:
            raise ValueError("relevance must be between 0 and 1")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "refId": self.ref_id,
            "category": self.category,
            "version": self.version,
            "digest": self.digest,
            "required": self.required,
            "relevance": self.relevance,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContextRef":
        return cls(
            ref_id=str(value["refId"]),
            category=str(value["category"]),
            version=str(value["version"]),
            digest=str(value["digest"]),
            required=bool(value.get("required", False)),
            relevance=float(value.get("relevance", 0.5)),
            provenance=value.get("provenance"),
        )


@dataclass(frozen=True)
class HandoffContract:
    """Explicit producer-consumer agreement between two agents."""

    goal: str
    acceptance_criteria: Tuple[str, ...]
    deliverables: Tuple[str, ...]
    inputs: Tuple[ArtifactRef, ...] = ()
    context_refs: Tuple[ContextRef, ...] = ()
    constraints: Tuple[str, ...] = ()
    authority: Authority = field(default_factory=Authority)
    parent_task_id: Optional[str] = None
    token_budget: Optional[int] = None
    cost_budget: Optional[float] = None
    deadline: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("handoff goal is required")
        if not self.acceptance_criteria:
            raise ValueError("at least one acceptance criterion is required")
        if not self.deliverables:
            raise ValueError("at least one deliverable is required")
        if self.token_budget is not None and self.token_budget <= 0:
            raise ValueError("token_budget must be positive")
        if self.cost_budget is not None and self.cost_budget < 0:
            raise ValueError("cost_budget cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "acceptanceCriteria": list(self.acceptance_criteria),
            "deliverables": list(self.deliverables),
            "inputs": [item.to_dict() for item in self.inputs],
            "contextRefs": [item.to_dict() for item in self.context_refs],
            "constraints": list(self.constraints),
            "authority": self.authority.to_dict(),
            "parentTaskId": self.parent_task_id,
            "tokenBudget": self.token_budget,
            "costBudget": self.cost_budget,
            "deadline": self.deadline,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffContract":
        return cls(
            goal=str(value["goal"]),
            acceptance_criteria=tuple(str(x) for x in value["acceptanceCriteria"]),
            deliverables=tuple(str(x) for x in value["deliverables"]),
            inputs=tuple(ArtifactRef.from_dict(x) for x in value.get("inputs", [])),
            context_refs=tuple(ContextRef.from_dict(x) for x in value.get("contextRefs", [])),
            constraints=tuple(str(x) for x in value.get("constraints", [])),
            authority=Authority.from_dict(value.get("authority", {})),
            parent_task_id=value.get("parentTaskId"),
            token_budget=(int(value["tokenBudget"]) if value.get("tokenBudget") else None),
            cost_budget=(
                float(value["costBudget"]) if value.get("costBudget") is not None else None
            ),
            deadline=value.get("deadline"),
        )


@dataclass(frozen=True)
class ActionIntent:
    action: str
    target: str
    risk: RiskLevel = RiskLevel.LOW
    external_side_effect: bool = False
    irreversible: bool = False
    data_classes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "risk": self.risk.value,
            "externalSideEffect": self.external_side_effect,
            "irreversible": self.irreversible,
            "dataClasses": list(self.data_classes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionIntent":
        return cls(
            action=str(value["action"]),
            target=str(value["target"]),
            risk=RiskLevel(str(value.get("risk", RiskLevel.LOW.value))),
            external_side_effect=bool(value.get("externalSideEffect", False)),
            irreversible=bool(value.get("irreversible", False)),
            data_classes=tuple(str(item) for item in value.get("dataClasses", ())),
        )


@dataclass(frozen=True)
class CoordinationEnvelope:
    """Traceable message passed among humans, agents, tools, and the orchestrator."""

    schema_version: str
    message_id: str
    session_id: str
    thread_id: str
    sender: ActorRef
    recipients: Tuple[ActorRef, ...]
    kind: EnvelopeKind
    payload: Mapping[str, Any]
    timestamp: str
    correlation_id: str
    causation_id: Optional[str]
    idempotency_key: str
    traceparent: Optional[str] = None
    reply_to: Optional[str] = None
    ttl_seconds: Optional[int] = None
    priority: int = 50
    authority: Authority = field(default_factory=Authority)

    CURRENT_SCHEMA = "qe.agent-envelope/0.1"

    def __post_init__(self) -> None:
        if self.schema_version != self.CURRENT_SCHEMA:
            raise ValueError("unsupported envelope schema: %s" % self.schema_version)
        for field_name in ("message_id", "session_id", "thread_id", "idempotency_key"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError("%s is required" % field_name)
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

    @classmethod
    def create(
        cls,
        session_id: str,
        thread_id: str,
        sender: ActorRef,
        recipients: Iterable[ActorRef],
        kind: EnvelopeKind,
        payload: Mapping[str, Any],
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        authority: Optional[Authority] = None,
        **kwargs: Any,
    ) -> "CoordinationEnvelope":
        message_id = new_id("msg")
        return cls(
            schema_version=cls.CURRENT_SCHEMA,
            message_id=message_id,
            session_id=session_id,
            thread_id=thread_id,
            sender=sender,
            recipients=tuple(recipients),
            kind=kind,
            payload=dict(payload),
            timestamp=utc_now(),
            correlation_id=correlation_id or message_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key or message_id,
            authority=authority or Authority(),
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "messageId": self.message_id,
            "sessionId": self.session_id,
            "threadId": self.thread_id,
            "sender": self.sender.to_dict(),
            "recipients": [item.to_dict() for item in self.recipients],
            "kind": self.kind.value,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "idempotencyKey": self.idempotency_key,
            "traceparent": self.traceparent,
            "replyTo": self.reply_to,
            "ttlSeconds": self.ttl_seconds,
            "priority": self.priority,
            "authority": self.authority.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CoordinationEnvelope":
        return cls(
            schema_version=str(value["schemaVersion"]),
            message_id=str(value["messageId"]),
            session_id=str(value["sessionId"]),
            thread_id=str(value["threadId"]),
            sender=ActorRef.from_dict(value["sender"]),
            recipients=tuple(ActorRef.from_dict(x) for x in value.get("recipients", [])),
            kind=EnvelopeKind(str(value["kind"])),
            payload=dict(value.get("payload", {})),
            timestamp=str(value["timestamp"]),
            correlation_id=str(value["correlationId"]),
            causation_id=value.get("causationId"),
            idempotency_key=str(value["idempotencyKey"]),
            traceparent=value.get("traceparent"),
            reply_to=value.get("replyTo"),
            ttl_seconds=(int(value["ttlSeconds"]) if value.get("ttlSeconds") else None),
            priority=int(value.get("priority", 50)),
            authority=Authority.from_dict(value.get("authority", {})),
        )
