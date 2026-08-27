# ruff: noqa: UP006, UP035
"""Capability-free durable receipt values for native-IM E2 inbound observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Tuple, Type, TypeVar

from ._native_im_codec import (
    NATIVE_IM_SCHEMA_VERSION,
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _enum,
    _id,
    _model_digest,
    _non_negative_integer,
    _ordered_unique_text,
    _plain_dict,
    _plain_list,
    _positive_integer,
    _schema_version,
    _timestamp,
)

_MAX_RECEIPT_BYTES = 1 * 1_024 * 1_024
_MAX_EVENT_RECEIPTS = 1_000
_DISPOSITIONS = {"fresh_observation", "observed_replay"}
_READ_STATUSES = {"prepared", "admitted"}

_SCOPE_FIELDS = {"schemaVersion", "tenantId", "workspaceId", "provider", "channelId"}
_EVENT_RECEIPT_FIELDS = {
    "schemaVersion",
    "scope",
    "eventId",
    "eventDigest",
    "cursor",
    "sequenceNumber",
    "firstReceivedAt",
    "admittedAt",
}
_CHECKPOINT_FIELDS = {
    "schemaVersion",
    "scope",
    "afterCursor",
    "afterSequence",
    "continuationSnapshotToken",
    "checkpointRevision",
    "lastReadRequestDigest",
    "lastPageDigest",
    "updatedAt",
}
_PREPARATION_FIELDS = {
    "schemaVersion",
    "scope",
    "readRequestId",
    "readRequestDigest",
    "baseCheckpointRevision",
    "readStatus",
    "disposition",
    "preparedAt",
}
_ADMISSION_FIELDS = {
    "schemaVersion",
    "scope",
    "readRequestId",
    "readRequestDigest",
    "pageDigest",
    "disposition",
    "checkpoint",
    "eventReceipts",
    "admittedAt",
}


class NativeIMInboundConflictError(RuntimeError):
    """A durable native-IM identity was reused with different immutable facts."""


class NativeIMInboundCheckpointConflictError(NativeIMInboundConflictError):
    """A read no longer matches the scope's exact durable checkpoint revision."""


class NativeIMInboundTransactionError(RuntimeError):
    """A page transaction failed with a confirmed non-commit outcome."""


class NativeIMInboundCommitAmbiguityError(NativeIMInboundTransactionError):
    """SQLite may have committed a page but the caller did not receive its ACK."""


_InboxT = TypeVar("_InboxT", bound="_NativeIMInboxWireValue")


class _NativeIMInboxWireValue:
    _MODEL_NAME: ClassVar[str]
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_RECEIPT_BYTES

    def to_dict(self) -> Dict[str, Any]:  # pragma: no cover - abstract boundary
        raise NotImplementedError

    @classmethod
    def from_dict(cls: Type[_InboxT], value: object) -> _InboxT:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def from_json_bytes(cls: Type[_InboxT], encoded: object) -> _InboxT:
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                cls._MODEL_NAME,
                maximum_bytes=cls._MAX_CANONICAL_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > self._MAX_CANONICAL_BYTES:
            raise NativeIMCodecTooLargeError(f"{self._MODEL_NAME} exceeds its canonical byte limit")
        return encoded

    def canonical_digest(self) -> str:
        self.canonical_bytes()
        return _model_digest(self._MODEL_NAME, self.to_dict())


def _require_exact_model(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must use the exact native-IM inbox V1 model class")


def _require_exact_tuple(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    return value


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _id(value, label)


@dataclass(frozen=True)
class NativeIMScopeV1(_NativeIMInboxWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str

    _MODEL_NAME: ClassVar[str] = "NativeIMScopeV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, NativeIMScopeV1, "scope")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
        ):
            _id(value, label)
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMScopeV1:
        if cls is not NativeIMScopeV1:
            raise TypeError("scope decoder requires the exact V1 class")
        body = _plain_dict(value, _SCOPE_FIELDS, "scope")
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
        )


@dataclass(frozen=True)
class NativeIMInboxEventReceiptV1(_NativeIMInboxWireValue):
    schema_version: int
    scope: NativeIMScopeV1
    event_id: str
    event_digest: str
    cursor: str
    sequence_number: int
    first_received_at: str
    admitted_at: str

    _MODEL_NAME: ClassVar[str] = "NativeIMInboxEventReceiptV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, NativeIMInboxEventReceiptV1, "event receipt")
        _schema_version(self.schema_version)
        _require_exact_model(self.scope, NativeIMScopeV1, "scope")
        _id(self.event_id, "eventId")
        _digest(self.event_digest, "eventDigest")
        _id(self.cursor, "cursor")
        _non_negative_integer(self.sequence_number, "sequenceNumber")
        _timestamp(self.first_received_at, "firstReceivedAt")
        _timestamp(self.admitted_at, "admittedAt")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "scope": self.scope.to_dict(),
            "eventId": self.event_id,
            "eventDigest": self.event_digest,
            "cursor": self.cursor,
            "sequenceNumber": self.sequence_number,
            "firstReceivedAt": self.first_received_at,
            "admittedAt": self.admitted_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMInboxEventReceiptV1:
        if cls is not NativeIMInboxEventReceiptV1:
            raise TypeError("event receipt decoder requires the exact V1 class")
        body = _plain_dict(value, _EVENT_RECEIPT_FIELDS, "event receipt")
        return cls(
            schema_version=body["schemaVersion"],
            scope=NativeIMScopeV1.from_dict(body["scope"]),
            event_id=body["eventId"],
            event_digest=body["eventDigest"],
            cursor=body["cursor"],
            sequence_number=body["sequenceNumber"],
            first_received_at=body["firstReceivedAt"],
            admitted_at=body["admittedAt"],
        )


@dataclass(frozen=True)
class NativeIMInboundCheckpointV1(_NativeIMInboxWireValue):
    schema_version: int
    scope: NativeIMScopeV1
    after_cursor: str | None
    after_sequence: int | None
    continuation_snapshot_token: str | None
    checkpoint_revision: int
    last_read_request_digest: str
    last_page_digest: str
    updated_at: str

    _MODEL_NAME: ClassVar[str] = "NativeIMInboundCheckpointV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, NativeIMInboundCheckpointV1, "checkpoint")
        _schema_version(self.schema_version)
        _require_exact_model(self.scope, NativeIMScopeV1, "scope")
        _optional_id(self.after_cursor, "afterCursor")
        if self.after_sequence is not None:
            _non_negative_integer(self.after_sequence, "afterSequence")
        if (self.after_cursor is None) != (self.after_sequence is None):
            raise ValueError("after cursor and sequence must be null or present together")
        _optional_id(self.continuation_snapshot_token, "continuationSnapshotToken")
        if self.continuation_snapshot_token is not None and self.after_cursor is None:
            raise ValueError("continuation snapshot requires a durable resume pair")
        _positive_integer(self.checkpoint_revision, "checkpointRevision")
        _digest(self.last_read_request_digest, "lastReadRequestDigest")
        _digest(self.last_page_digest, "lastPageDigest")
        _timestamp(self.updated_at, "updatedAt")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "scope": self.scope.to_dict(),
            "afterCursor": self.after_cursor,
            "afterSequence": self.after_sequence,
            "continuationSnapshotToken": self.continuation_snapshot_token,
            "checkpointRevision": self.checkpoint_revision,
            "lastReadRequestDigest": self.last_read_request_digest,
            "lastPageDigest": self.last_page_digest,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMInboundCheckpointV1:
        if cls is not NativeIMInboundCheckpointV1:
            raise TypeError("checkpoint decoder requires the exact V1 class")
        body = _plain_dict(value, _CHECKPOINT_FIELDS, "checkpoint")
        return cls(
            schema_version=body["schemaVersion"],
            scope=NativeIMScopeV1.from_dict(body["scope"]),
            after_cursor=body["afterCursor"],
            after_sequence=body["afterSequence"],
            continuation_snapshot_token=body["continuationSnapshotToken"],
            checkpoint_revision=body["checkpointRevision"],
            last_read_request_digest=body["lastReadRequestDigest"],
            last_page_digest=body["lastPageDigest"],
            updated_at=body["updatedAt"],
        )


@dataclass(frozen=True)
class NativeIMInboundReadPreparationV1(_NativeIMInboxWireValue):
    schema_version: int
    scope: NativeIMScopeV1
    read_request_id: str
    read_request_digest: str
    base_checkpoint_revision: int
    read_status: str
    disposition: str
    prepared_at: str

    _MODEL_NAME: ClassVar[str] = "NativeIMInboundReadPreparationV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, NativeIMInboundReadPreparationV1, "read preparation")
        _schema_version(self.schema_version)
        _require_exact_model(self.scope, NativeIMScopeV1, "scope")
        _id(self.read_request_id, "readRequestId")
        _digest(self.read_request_digest, "readRequestDigest")
        _non_negative_integer(self.base_checkpoint_revision, "baseCheckpointRevision")
        _enum(self.read_status, _READ_STATUSES, "readStatus")
        _enum(self.disposition, _DISPOSITIONS, "disposition")
        if self.disposition == "fresh_observation" and self.read_status != "prepared":
            raise ValueError("fresh preparation must expose prepared status")
        _timestamp(self.prepared_at, "preparedAt")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "scope": self.scope.to_dict(),
            "readRequestId": self.read_request_id,
            "readRequestDigest": self.read_request_digest,
            "baseCheckpointRevision": self.base_checkpoint_revision,
            "readStatus": self.read_status,
            "disposition": self.disposition,
            "preparedAt": self.prepared_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMInboundReadPreparationV1:
        if cls is not NativeIMInboundReadPreparationV1:
            raise TypeError("read preparation decoder requires the exact V1 class")
        body = _plain_dict(value, _PREPARATION_FIELDS, "read preparation")
        return cls(
            schema_version=body["schemaVersion"],
            scope=NativeIMScopeV1.from_dict(body["scope"]),
            read_request_id=body["readRequestId"],
            read_request_digest=body["readRequestDigest"],
            base_checkpoint_revision=body["baseCheckpointRevision"],
            read_status=body["readStatus"],
            disposition=body["disposition"],
            prepared_at=body["preparedAt"],
        )


@dataclass(frozen=True)
class NativeIMInboundPageAdmissionResultV1(_NativeIMInboxWireValue):
    schema_version: int
    scope: NativeIMScopeV1
    read_request_id: str
    read_request_digest: str
    page_digest: str
    disposition: str
    checkpoint: NativeIMInboundCheckpointV1
    event_receipts: Tuple[NativeIMInboxEventReceiptV1, ...] = field(repr=False)
    admitted_at: str

    _MODEL_NAME: ClassVar[str] = "NativeIMInboundPageAdmissionResultV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, NativeIMInboundPageAdmissionResultV1, "page admission")
        _schema_version(self.schema_version)
        _require_exact_model(self.scope, NativeIMScopeV1, "scope")
        _id(self.read_request_id, "readRequestId")
        _digest(self.read_request_digest, "readRequestDigest")
        _digest(self.page_digest, "pageDigest")
        _enum(self.disposition, _DISPOSITIONS, "disposition")
        _require_exact_model(self.checkpoint, NativeIMInboundCheckpointV1, "checkpoint")
        if self.checkpoint.scope != self.scope:
            raise ValueError("checkpoint scope does not match page admission scope")
        if self.checkpoint.last_read_request_digest != self.read_request_digest:
            raise ValueError("checkpoint request digest does not match page admission")
        if self.checkpoint.last_page_digest != self.page_digest:
            raise ValueError("checkpoint page digest does not match page admission")
        _require_exact_tuple(self.event_receipts, "eventReceipts")
        if len(self.event_receipts) > _MAX_EVENT_RECEIPTS:
            raise NativeIMCodecTooLargeError("eventReceipts exceeds its item limit")
        event_ids: list[str] = []
        previous_sequence: int | None = None
        for index, receipt in enumerate(self.event_receipts):
            _require_exact_model(receipt, NativeIMInboxEventReceiptV1, f"eventReceipts[{index}]")
            if receipt.scope != self.scope:
                raise ValueError("event receipt scope does not match page admission scope")
            if previous_sequence is not None and receipt.sequence_number <= previous_sequence:
                raise ValueError("event receipt sequences must strictly increase")
            previous_sequence = receipt.sequence_number
            event_ids.append(receipt.event_id)
        _ordered_unique_text(
            tuple(sorted(event_ids, key=lambda value: value.encode("utf-8"))), "eventIds"
        )
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event receipt IDs must be unique")
        _timestamp(self.admitted_at, "admittedAt")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "scope": self.scope.to_dict(),
            "readRequestId": self.read_request_id,
            "readRequestDigest": self.read_request_digest,
            "pageDigest": self.page_digest,
            "disposition": self.disposition,
            "checkpoint": self.checkpoint.to_dict(),
            "eventReceipts": [receipt.to_dict() for receipt in self.event_receipts],
            "admittedAt": self.admitted_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMInboundPageAdmissionResultV1:
        if cls is not NativeIMInboundPageAdmissionResultV1:
            raise TypeError("page admission decoder requires the exact V1 class")
        body = _plain_dict(value, _ADMISSION_FIELDS, "page admission")
        receipts = _plain_list(
            body["eventReceipts"],
            "eventReceipts",
            maximum_items=_MAX_EVENT_RECEIPTS,
        )
        return cls(
            schema_version=body["schemaVersion"],
            scope=NativeIMScopeV1.from_dict(body["scope"]),
            read_request_id=body["readRequestId"],
            read_request_digest=body["readRequestDigest"],
            page_digest=body["pageDigest"],
            disposition=body["disposition"],
            checkpoint=NativeIMInboundCheckpointV1.from_dict(body["checkpoint"]),
            event_receipts=tuple(NativeIMInboxEventReceiptV1.from_dict(item) for item in receipts),
            admitted_at=body["admittedAt"],
        )


__all__ = [
    "NATIVE_IM_SCHEMA_VERSION",
    "NativeIMInboxEventReceiptV1",
    "NativeIMInboundCheckpointConflictError",
    "NativeIMInboundCheckpointV1",
    "NativeIMInboundCommitAmbiguityError",
    "NativeIMInboundConflictError",
    "NativeIMInboundPageAdmissionResultV1",
    "NativeIMInboundReadPreparationV1",
    "NativeIMInboundTransactionError",
    "NativeIMScopeV1",
]
