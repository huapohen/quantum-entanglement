"""Authority checks and the human `Needs You` inbox."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from .protocol import (
    ActionIntent,
    ApprovalDecision,
    Authority,
    RiskLevel,
    new_id,
    utc_now,
)


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str


class PolicyEngine:
    """Default-deny external effects; explicit delegation can lower friction."""

    SAFE_READ_ACTIONS = ("read", "search", "list", "summarize", "analyze", "draft")

    def __init__(self, forbidden_actions: Tuple[str, ...] = ()) -> None:
        self.forbidden_actions = forbidden_actions

    def evaluate(self, intent: ActionIntent, authority: Authority) -> PolicyDecision:
        if intent.action in self.forbidden_actions:
            return PolicyDecision(PolicyOutcome.DENY, "action is forbidden by workspace policy")
        if (
            intent.action in self.SAFE_READ_ACTIONS
            and not intent.external_side_effect
            and not intent.irreversible
            and intent.risk.rank <= RiskLevel.LOW.rank
        ):
            return PolicyDecision(PolicyOutcome.ALLOW, "read-only or draft action")
        if intent.irreversible or intent.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return PolicyDecision(PolicyOutcome.NEEDS_APPROVAL, "high-impact action needs a human")
        if authority.permits(intent.action, intent.risk, intent.external_side_effect):
            return PolicyDecision(PolicyOutcome.ALLOW, "covered by explicit delegated authority")
        return PolicyDecision(
            PolicyOutcome.NEEDS_APPROVAL,
            "action exceeds delegated authority or creates an external side effect",
        )


@dataclass
class ApprovalRequest:
    session_id: str
    task_id: str
    intent: ActionIntent
    reason: str
    request_id: str = field(default_factory=lambda: new_id("approval"))
    created_at: str = field(default_factory=utc_now)
    decision: Optional[ApprovalDecision] = None
    decided_by: Optional[str] = None
    comment: Optional[str] = None

    @property
    def pending(self) -> bool:
        return self.decision is None

    def to_dict(self) -> Dict[str, object]:
        return {
            "requestId": self.request_id,
            "sessionId": self.session_id,
            "taskId": self.task_id,
            "intent": self.intent.to_dict(),
            "reason": self.reason,
            "createdAt": self.created_at,
            "decision": self.decision.value if self.decision else None,
            "decidedBy": self.decided_by,
            "comment": self.comment,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalRequest":
        raw_decision = value.get("decision")
        return cls(
            session_id=str(value["sessionId"]),
            task_id=str(value["taskId"]),
            intent=ActionIntent.from_dict(value["intent"]),
            reason=str(value["reason"]),
            request_id=str(value["requestId"]),
            created_at=str(value["createdAt"]),
            decision=(ApprovalDecision(str(raw_decision)) if raw_decision else None),
            decided_by=(str(value["decidedBy"]) if value.get("decidedBy") else None),
            comment=(str(value["comment"]) if value.get("comment") else None),
        )


class NeedsYouQueue:
    def __init__(self) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            existing = next(
                (
                    item
                    for item in self._requests.values()
                    if item.session_id == request.session_id
                    and item.task_id == request.task_id
                    and item.pending
                ),
                None,
            )
            if existing:
                return existing
            self._requests[request.request_id] = request
            return request

    def restore(self, request: ApprovalRequest) -> ApprovalRequest:
        """Upsert a request reconstructed from the immutable event stream."""

        with self._lock:
            self._requests[request.request_id] = request
            return request

    def get(self, request_id: str) -> ApprovalRequest:
        with self._lock:
            return self._requests[request_id]

    def pending(self, session_id: Optional[str] = None) -> Tuple[ApprovalRequest, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._requests.values()
                if item.pending and (session_id is None or item.session_id == session_id)
            )

    def decide(
        self,
        request_id: str,
        decision: ApprovalDecision,
        actor_id: str,
        comment: Optional[str] = None,
    ) -> ApprovalRequest:
        with self._lock:
            request = self._requests[request_id]
            if not request.pending:
                if request.decision == decision and request.decided_by == actor_id:
                    return request
                raise ValueError("approval request is already decided")
            request.decision = decision
            request.decided_by = actor_id
            request.comment = comment
            return request
