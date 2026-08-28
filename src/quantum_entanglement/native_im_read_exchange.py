# ruff: noqa: UP006, UP035
"""Canonical per-read exchange evidence kept outside stable native-IM events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict

from ._native_im_codec import (
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _id,
    _model_digest,
    _non_negative_integer,
    _plain_dict,
    _schema_version,
    _timestamp,
)
from .native_im import IMInboundReadRequestV1

_MAX_EXCHANGE_EVIDENCE_BYTES = 8 * 1_024
_EXCHANGE_EVIDENCE_FIELDS = {
    "afterCursor",
    "afterSequence",
    "eventSourceEvidenceDigest",
    "evidenceDigest",
    "exchangeSecurityEvidenceDigest",
    "readRequestDigest",
    "readRequestId",
    "receivedAt",
    "requestIntentDigest",
    "schemaVersion",
    "snapshotToken",
}


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _id(value, label)


def _optional_sequence(value: object) -> int | None:
    if value is None:
        return None
    return _non_negative_integer(value, "afterSequence")


def derive_native_im_read_exchange_evidence_digest_v1(
    *,
    read_request_id: str,
    read_request_digest: str,
    after_cursor: str | None,
    after_sequence: int | None,
    snapshot_token: str | None,
    received_at: str,
    request_intent_digest: str,
    exchange_security_evidence_digest: str,
    event_source_evidence_digest: str,
) -> str:
    """Bind one transient read exchange without changing canonical event identity."""

    _id(read_request_id, "readRequestId")
    _digest(read_request_digest, "readRequestDigest")
    cursor = _optional_id(after_cursor, "afterCursor")
    sequence = _optional_sequence(after_sequence)
    token = _optional_id(snapshot_token, "snapshotToken")
    if (cursor is None) != (sequence is None):
        raise ValueError("exchange afterCursor and afterSequence must be paired")
    if token is not None and cursor is None:
        raise ValueError("exchange snapshotToken requires continuation state")
    _timestamp(received_at, "receivedAt")
    for value, label in (
        (request_intent_digest, "requestIntentDigest"),
        (exchange_security_evidence_digest, "exchangeSecurityEvidenceDigest"),
        (event_source_evidence_digest, "eventSourceEvidenceDigest"),
    ):
        _digest(value, label)
    return _model_digest(
        "NativeIMInboundReadExchangeEvidenceV1",
        {
            "afterCursor": cursor,
            "afterSequence": sequence,
            "eventSourceEvidenceDigest": event_source_evidence_digest,
            "exchangeSecurityEvidenceDigest": exchange_security_evidence_digest,
            "readRequestDigest": read_request_digest,
            "readRequestId": read_request_id,
            "receivedAt": received_at,
            "requestIntentDigest": request_intent_digest,
            "schemaVersion": 1,
            "snapshotToken": token,
        },
    )


@dataclass(frozen=True, repr=False)
class NativeIMInboundReadExchangeEvidenceV1:
    """Request-correlated evidence for one provider exchange, never an event field."""

    schema_version: int
    read_request_id: str
    read_request_digest: str = field(repr=False)
    after_cursor: str | None
    after_sequence: int | None
    snapshot_token: str | None = field(repr=False)
    received_at: str
    request_intent_digest: str = field(repr=False)
    exchange_security_evidence_digest: str = field(repr=False)
    event_source_evidence_digest: str = field(repr=False)
    evidence_digest: str = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "NativeIMInboundReadExchangeEvidenceV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMInboundReadExchangeEvidenceV1:
            raise TypeError("read exchange evidence requires the exact V1 class")
        _schema_version(self.schema_version)
        expected = derive_native_im_read_exchange_evidence_digest_v1(
            read_request_id=self.read_request_id,
            read_request_digest=self.read_request_digest,
            after_cursor=self.after_cursor,
            after_sequence=self.after_sequence,
            snapshot_token=self.snapshot_token,
            received_at=self.received_at,
            request_intent_digest=self.request_intent_digest,
            exchange_security_evidence_digest=self.exchange_security_evidence_digest,
            event_source_evidence_digest=self.event_source_evidence_digest,
        )
        _digest(self.evidence_digest, "evidenceDigest")
        if self.evidence_digest != expected:
            raise ValueError("read exchange evidence digest does not bind its fields")
        self.canonical_bytes()

    def validate_request_binding(self, request: IMInboundReadRequestV1) -> None:
        if type(request) is not IMInboundReadRequestV1:
            raise TypeError("exchange evidence requires an exact inbound read request")
        if (
            self.read_request_id != request.read_request_id
            or self.read_request_digest != request.canonical_digest()
            or self.after_cursor != request.after_cursor
            or self.after_sequence != request.after_sequence
            or self.snapshot_token != request.snapshot_token
        ):
            raise ValueError("read exchange evidence does not bind its request")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "afterCursor": self.after_cursor,
            "afterSequence": self.after_sequence,
            "eventSourceEvidenceDigest": self.event_source_evidence_digest,
            "evidenceDigest": self.evidence_digest,
            "exchangeSecurityEvidenceDigest": self.exchange_security_evidence_digest,
            "readRequestDigest": self.read_request_digest,
            "readRequestId": self.read_request_id,
            "receivedAt": self.received_at,
            "requestIntentDigest": self.request_intent_digest,
            "schemaVersion": self.schema_version,
            "snapshotToken": self.snapshot_token,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMInboundReadExchangeEvidenceV1:
        if cls is not NativeIMInboundReadExchangeEvidenceV1:
            raise TypeError("read exchange evidence decoder requires the exact V1 class")
        body = _plain_dict(value, _EXCHANGE_EVIDENCE_FIELDS, "read exchange evidence")
        return cls(
            schema_version=body["schemaVersion"],
            read_request_id=body["readRequestId"],
            read_request_digest=body["readRequestDigest"],
            after_cursor=body["afterCursor"],
            after_sequence=body["afterSequence"],
            snapshot_token=body["snapshotToken"],
            received_at=body["receivedAt"],
            request_intent_digest=body["requestIntentDigest"],
            exchange_security_evidence_digest=body["exchangeSecurityEvidenceDigest"],
            event_source_evidence_digest=body["eventSourceEvidenceDigest"],
            evidence_digest=body["evidenceDigest"],
        )

    @classmethod
    def from_json_bytes(cls, encoded: object) -> NativeIMInboundReadExchangeEvidenceV1:
        if cls is not NativeIMInboundReadExchangeEvidenceV1:
            raise TypeError("read exchange evidence decoder requires the exact V1 class")
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                "read exchange evidence",
                maximum_bytes=_MAX_EXCHANGE_EVIDENCE_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_EXCHANGE_EVIDENCE_BYTES:
            raise NativeIMCodecTooLargeError("read exchange evidence exceeds its byte limit")
        return encoded

    def canonical_digest(self) -> str:
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def __repr__(self) -> str:
        return (
            "NativeIMInboundReadExchangeEvidenceV1("
            f"request={self.read_request_id!r}, evidence={self.evidence_digest[:12]!r})"
        )


__all__ = [
    "NativeIMInboundReadExchangeEvidenceV1",
    "derive_native_im_read_exchange_evidence_digest_v1",
]
