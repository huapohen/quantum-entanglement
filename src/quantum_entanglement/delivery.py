"""Durable delivery records for transactional inbox and outbox processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from .events import StoredEvent
from .protocol import new_id, utc_now


def _require_rfc3339(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("%s must be an RFC 3339 timestamp" % field_name) from exc
    if parsed.tzinfo is None:
        raise ValueError("%s must include a timezone" % field_name)


class OutboxStatus(str, Enum):
    """Lifecycle of one message waiting to cross a process boundary."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class OutboxMessage:
    """Message requested by the same transaction that appends a domain event."""

    destination: str
    payload: Mapping[str, Any]
    headers: Mapping[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: new_id("msg"))
    idempotency_key: Optional[str] = None
    available_at: str = field(default_factory=utc_now)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.destination.strip() or not self.message_id.strip():
            raise ValueError("destination and message_id are required")
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", self.message_id)
        elif not self.idempotency_key.strip():
            raise ValueError("idempotency_key cannot be blank")
        _require_rfc3339(self.available_at, "available_at")
        _require_rfc3339(self.created_at, "created_at")


@dataclass(frozen=True)
class StoredOutboxMessage:
    """Persisted outbox row including delivery ownership and retry state."""

    message: OutboxMessage
    triggering_event_id: str
    triggering_global_position: int
    status: OutboxStatus
    attempt_count: int = 0
    lease_token: Optional[str] = None
    lease_expires_at: Optional[str] = None
    last_error: Optional[str] = None
    published_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "messageId": self.message.message_id,
            "destination": self.message.destination,
            "payload": dict(self.message.payload),
            "headers": dict(self.message.headers),
            "idempotencyKey": self.message.idempotency_key,
            "availableAt": self.message.available_at,
            "createdAt": self.message.created_at,
            "triggeringEventId": self.triggering_event_id,
            "triggeringGlobalPosition": self.triggering_global_position,
            "status": self.status.value,
            "attemptCount": self.attempt_count,
            "leaseToken": self.lease_token,
            "leaseExpiresAt": self.lease_expires_at,
            "lastError": self.last_error,
            "publishedAt": self.published_at,
        }


@dataclass(frozen=True)
class InboxReceipt:
    """Durable proof that a named consumer admitted an external message."""

    consumer_id: str
    message_id: str
    received_at: str
    event_id: str
    event_global_position: int
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboxAppendResult:
    """Result of admitting an inbox message exactly once per consumer."""

    event: StoredEvent
    receipt: InboxReceipt
    duplicate: bool
