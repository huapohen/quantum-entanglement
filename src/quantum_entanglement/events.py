"""Immutable domain events used as the collaboration source of truth."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .protocol import new_id, utc_now


@dataclass(frozen=True)
class DomainEvent:
    stream_id: str
    event_type: str
    payload: Mapping[str, Any]
    actor_id: str
    event_id: str = field(default_factory=lambda: new_id("evt"))
    timestamp: str = field(default_factory=utc_now)
    correlation_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.stream_id.strip() or not self.event_type.strip() or not self.actor_id.strip():
            raise ValueError("stream_id, event_type, and actor_id are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "streamId": self.stream_id,
            "eventType": self.event_type,
            "payload": dict(self.payload),
            "actorId": self.actor_id,
            "timestamp": self.timestamp,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "idempotencyKey": self.idempotency_key,
        }


@dataclass(frozen=True)
class StoredEvent:
    event: DomainEvent
    sequence: int
    global_position: int

    def to_dict(self) -> dict[str, Any]:
        value = self.event.to_dict()
        value.update({"sequence": self.sequence, "globalPosition": self.global_position})
        return value
