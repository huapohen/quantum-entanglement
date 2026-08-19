"""Quantum Entanglement multi-agent coordination kernel."""

from .artifacts import ArtifactLedger, ArtifactVersion
from .events import DomainEvent, StoredEvent
from .protocol import (
    ActionIntent,
    ActorKind,
    ActorRef,
    ApprovalDecision,
    ArtifactOutput,
    ArtifactRef,
    Authority,
    ContextRef,
    CoordinationEnvelope,
    EnvelopeKind,
    HandoffContract,
    RiskLevel,
    TaskStatus,
)
from .store import ConcurrencyError, SQLiteEventStore

__all__ = [
    "ActionIntent",
    "ActorKind",
    "ActorRef",
    "ApprovalDecision",
    "ArtifactLedger",
    "ArtifactOutput",
    "ArtifactRef",
    "ArtifactVersion",
    "Authority",
    "ConcurrencyError",
    "ContextRef",
    "CoordinationEnvelope",
    "DomainEvent",
    "EnvelopeKind",
    "HandoffContract",
    "RiskLevel",
    "SQLiteEventStore",
    "StoredEvent",
    "TaskStatus",
]

__version__ = "0.1.0"

